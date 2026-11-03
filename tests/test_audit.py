"""Audit records: attempt pairing, schema validity, failure mapping, the hand validator."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from forgectl import audit
from forgectl.audit import Attempt, AuditContext, AuditInputs
from forgectl.cli import SignalExit
from forgectl.errors import ForgectlError

from .cli_schema_check import validate  # TODO(#3): the shared schema registry replaces this

SHA = "a" * 40
HASH = "b" * 64
PR = "github.com/o/r#7"


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state" / "forgectl"


def inputs(**over: Any) -> AuditInputs:
    base: dict[str, Any] = {
        "head_sha": SHA,
        "base_ref": "main",
        "config_hash": HASH,
        "token_source": "token_env",
        "stdin_tty": False,
        "actor_source": "flag",
    }
    return AuditInputs(**{**base, **over})


def context(action: str = "merge", **over: Any) -> AuditContext:
    base: dict[str, Any] = {
        "actor": "auto:bot",
        "decision_ids": ["d-1"],
        "target": PR,
        "action": action,
        "inputs": inputs(),
    }
    return AuditContext(**{**base, **over})


def records(state: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (state / "audit.jsonl").read_text().splitlines()]


# --- pairing and the schema ---------------------------------------------------------------------


def test_a_write_has_an_attempted_record_then_an_outcome_with_one_attempt_id(state: Path) -> None:
    with Attempt(state, context()) as attempt:
        attempt.begin()
        attempt.finish("merged")
    first, second = records(state)
    assert (first["result"], second["result"]) == ("attempted", "merged")
    assert first["attempt_id"] == second["attempt_id"]
    assert "ids" not in first and "observed" not in first
    assert second["ids"] == [] and second["observed"] is None
    for record in (first, second):
        validate(record, "forgectl/audit.json")


def test_attempt_ids_are_distinct_and_match_the_schema_pattern() -> None:
    ids = {audit.new_attempt_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{8,64}", i) for i in ids)


def test_a_merged_unexpected_outcome_carries_what_was_observed(state: Path) -> None:
    with Attempt(state, context()) as attempt:
        attempt.begin()
        attempt.finish("merged_unexpected", observed={"head_sha": "c" * 40, "base_ref": "dev"})
    out = records(state)[1]
    assert out["observed"] == {"head_sha": "c" * 40, "base_ref": "dev"}
    validate(out, "forgectl/audit.json")


def test_a_post_outcome_lists_the_ids_and_overrides_are_recorded(state: Path) -> None:
    ctx = context("comment", override=("allow_self",), inputs=inputs(event="comment"))
    with Attempt(state, ctx) as attempt:
        attempt.begin()
        attempt.finish("posted", ids=["gh:ic:abc"])
    first, second = records(state)
    assert second["ids"] == ["gh:ic:abc"]
    assert first["override"] == ["allow_self"]
    validate(second, "forgectl/audit.json")


@pytest.mark.parametrize(
    ("action", "result"),
    [
        ("merge", "merged"),
        ("merge", "auto_merge_enabled"),
        ("merge", "failed"),
        ("review", "posted"),
        ("review", "outcome_partial"),
        ("comment", "quick_action_ran"),
        ("reply", "outcome_unknown"),
        ("approve", "approved"),
        ("resolve", "resolved"),
        ("unresolve", "unresolved"),
    ],
)
def test_every_documented_outcome_validates_against_the_schema(
    state: Path, action: str, result: str
) -> None:
    with Attempt(state, context(action)) as attempt:
        attempt.begin()
        attempt.finish(result)
    for record in records(state):
        validate(record, "forgectl/audit.json")


def test_an_outcome_the_schema_forbids_for_the_action_is_refused_before_writing(
    state: Path,
) -> None:
    with pytest.raises(ValueError, match="resolved"), Attempt(state, context("merge")) as attempt:
        attempt.begin()
        attempt.finish("resolved")
    # The invalid outcome never landed; the run still ends in a record.
    assert [r["result"] for r in records(state)] == ["attempted", "outcome_unknown"]


def test_begin_writes_once_per_run_however_often_it_is_called(state: Path) -> None:
    with Attempt(state, context()) as attempt:
        attempt.begin()
        attempt.begin()
        attempt.finish("merged")
    assert len(records(state)) == 2


def test_finish_twice_writes_one_outcome(state: Path) -> None:
    with Attempt(state, context()) as attempt:
        attempt.begin()
        attempt.finish("merged")
        attempt.finish("failed")
    assert [r["result"] for r in records(state)] == ["attempted", "merged"]


def test_finish_before_begin_is_a_bug(state: Path) -> None:
    with pytest.raises(RuntimeError), Attempt(state, context()) as attempt:
        attempt.finish("merged")


def test_two_runs_pair_up_by_attempt_id(state: Path) -> None:
    a, b = Attempt(state, context()), Attempt(state, context())
    a.begin()
    b.begin()
    b.finish("failed")
    a.finish("merged")
    by_id: dict[str, list[str]] = {}
    for record in records(state):
        by_id.setdefault(record["attempt_id"], []).append(record["result"])
    assert sorted(by_id.values()) == [["attempted", "failed"], ["attempted", "merged"]]


# --- every attempt gets an outcome even when the command fails ------------------------------------


def test_an_exception_after_begin_still_records_an_outcome_as_unknown(state: Path) -> None:
    with pytest.raises(RuntimeError, match="boom"), Attempt(state, context()) as attempt:
        attempt.begin()
        raise RuntimeError("boom")
    assert [r["result"] for r in records(state)] == ["attempted", "outcome_unknown"]
    validate(records(state)[1], "forgectl/audit.json")


def test_a_signal_after_begin_records_an_unknown_outcome_and_propagates(state: Path) -> None:
    with pytest.raises(SignalExit), Attempt(state, context()) as attempt:
        attempt.begin()
        raise SignalExit(15)
    assert [r["result"] for r in records(state)] == ["attempted", "outcome_unknown"]


def test_an_exception_before_begin_writes_nothing(state: Path) -> None:
    with pytest.raises(RuntimeError), Attempt(state, context()):
        raise RuntimeError("refused early")
    assert not (state / "audit.jsonl").exists()


def test_leaving_a_with_block_without_finish_records_an_unknown_outcome(state: Path) -> None:
    with Attempt(state, context()) as attempt:
        attempt.begin()
    assert [r["result"] for r in records(state)] == ["attempted", "outcome_unknown"]


# --- failed appends ----------


def test_a_failed_first_append_is_audit_unavailable_and_nothing_follows(
    state: Path, tmp_path: Path
) -> None:
    state.mkdir(parents=True, mode=0o700)
    (state / "audit.jsonl").symlink_to(tmp_path / "x")
    attempt = Attempt(state, context())
    with pytest.raises(ForgectlError) as caught:
        attempt.begin()
    assert caught.value.code == "audit_unavailable" and caught.value.exit_code == 12
    assert not (tmp_path / "x").exists()


def test_a_failed_second_append_is_audit_failed_with_the_outcome_in_details(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = Attempt(state, context())
    attempt.begin()

    def refuse(*_: object) -> None:
        raise audit.jsonlog.JsonlError("disk full")

    monkeypatch.setattr(audit.jsonlog, "append", refuse)
    with pytest.raises(ForgectlError) as caught:
        attempt.finish("merged")
    assert caught.value.code == "audit_failed" and caught.value.exit_code == 12
    assert caught.value.details is not None
    assert caught.value.details["outcome"]["result"] == "merged"
    assert caught.value.details["outcome"]["attempt_id"] == records(state)[0]["attempt_id"]


def test_a_failed_outcome_append_while_a_signal_propagates_keeps_the_signal(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run() -> None:
        with Attempt(state, context()) as attempt:
            attempt.begin()
            monkeypatch.setattr(
                audit.jsonlog,
                "append",
                lambda *_: (_ for _ in ()).throw(audit.jsonlog.JsonlError("full")),
            )
            raise SignalExit(2)

    with pytest.raises(SignalExit):
        run()


def test_check_writable_maps_failure_to_audit_unavailable(state: Path, tmp_path: Path) -> None:
    audit.check_writable(state)
    assert (state / "audit.jsonl").exists()
    other = tmp_path / "s2"
    other.mkdir(mode=0o700)
    (other / "audit.jsonl").symlink_to(tmp_path / "nowhere")
    with pytest.raises(ForgectlError) as caught:
        audit.check_writable(other)
    assert caught.value.code == "audit_unavailable"


# --- no secrets, hostile fields ----------


def test_the_record_holds_only_schema_fields_and_the_body_only_as_a_hash(state: Path) -> None:
    ctx = context("comment", inputs=inputs(body_sha256="d" * 64))
    with Attempt(state, ctx) as attempt:
        attempt.begin()
        attempt.finish("posted")
    text = (state / "audit.jsonl").read_text()
    for record in records(state):
        assert set(record) <= set(json.load(open("schemas/audit.json"))["properties"])
        assert record["inputs"]["body_sha256"] == "d" * 64
    assert "ghp_" not in text


def test_an_oversized_base_ref_is_truncated_not_refused(state: Path) -> None:
    ctx = context(inputs=inputs(base_ref="é" * 5000))
    with Attempt(state, ctx) as attempt:
        attempt.begin()
        attempt.finish("merged_unexpected", observed={"head_sha": SHA, "base_ref": "x" * 100000})
    first, second = records(state)
    assert len(first["inputs"]["base_ref"]) <= audit.MAX_REF_CHARS
    assert len(second["observed"]["base_ref"]) <= audit.MAX_REF_CHARS
    for record in (first, second):
        validate(record, "forgectl/audit.json")


def test_hostile_text_in_untrusted_fields_stays_on_one_line(state: Path) -> None:
    evil = "a\nb\r\x1b[2J‮ "
    ctx = context(inputs=inputs(base_ref=evil, host_login=evil))
    Attempt(state, ctx).begin()
    assert len((state / "audit.jsonl").read_text().splitlines()) == 1
    assert records(state)[0]["inputs"]["base_ref"] == evil  # faithful on disk, ASCII-escaped


def test_a_context_that_violates_the_schema_is_a_bug_not_a_record(state: Path) -> None:
    with pytest.raises(ValueError):
        Attempt(state, context(target="not a pr")).begin()
    with pytest.raises(ValueError):
        Attempt(state, context(actor="root")).begin()
    assert not (state / "audit.jsonl").exists()


# --- the hand validator agrees with the schema ----------


def good() -> dict[str, Any]:
    return {
        "ts": "2026-01-01T00:00:00.000Z",
        "attempt_id": "abcdefgh12",
        "actor": "auto:bot",
        "decision_id": [],
        "target": PR,
        "action": "merge",
        "inputs": {
            "head_sha": SHA,
            "base_ref": "main",
            "event": None,
            "body_sha256": None,
            "method": "squash",
            "thread_id": None,
            "reply_to": None,
            "config_hash": HASH,
            "config_path": None,
            "host_login": None,
            "token_source": "none",
            "stdin_tty": True,
            "actor_source": "default",
        },
        "result": "merged",
        "override": [],
        "ids": [],
        "observed": None,
    }


def mutations() -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = [("good", good())]

    def mut(name: str, fn: Any) -> None:
        record = copy.deepcopy(good())
        fn(record)
        out.append((name, record))

    mut("extra key", lambda r: r.update(x=1))
    mut("extra input", lambda r: r["inputs"].update(x=1))
    mut("missing ts", lambda r: r.pop("ts"))
    mut("missing override", lambda r: r.pop("override"))
    mut("missing ids", lambda r: r.pop("ids"))
    mut("missing observed", lambda r: r.pop("observed"))
    mut("missing input", lambda r: r["inputs"].pop("method"))
    mut("ts no zone", lambda r: r.update(ts="2026-01-01T00:00:00"))
    mut("ts lower", lambda r: r.update(ts="2026-01-01t00:00:00z"))
    mut("attempt short", lambda r: r.update(attempt_id="abc"))
    mut("actor bad", lambda r: r.update(actor="bob"))
    mut("decision str", lambda r: r.update(decision_id="x"))
    mut("decision 33", lambda r: r.update(decision_id=["a"] * 33))
    mut("decision bad", lambda r: r.update(decision_id=["a b"]))
    mut("target bad", lambda r: r.update(target="github.com/o/r#0"))
    mut("action bad", lambda r: r.update(action="delete"))
    mut("sha short", lambda r: r["inputs"].update(head_sha="abc"))
    mut("sha upper", lambda r: r["inputs"].update(head_sha="A" * 40))
    mut("base_ref int", lambda r: r["inputs"].update(base_ref=3))
    mut("event bad", lambda r: r["inputs"].update(event="nope"))
    mut("method bad", lambda r: r["inputs"].update(method="ff"))
    mut("thread bad", lambda r: r["inputs"].update(thread_id="zzz"))
    mut("thread ok", lambda r: r["inputs"].update(thread_id="gh:rt:abc"))
    mut("hash short", lambda r: r["inputs"].update(config_hash="abc"))
    mut("hash null", lambda r: r["inputs"].update(config_hash=None))
    mut("path str", lambda r: r["inputs"].update(config_path="/c.yaml"))
    mut("login empty", lambda r: r["inputs"].update(host_login=""))
    mut("login ok", lambda r: r["inputs"].update(host_login="bob"))
    mut("token bad", lambda r: r["inputs"].update(token_source="x"))
    mut("tty int", lambda r: r["inputs"].update(stdin_tty=1))
    mut("source bad", lambda r: r["inputs"].update(actor_source="x"))
    mut("result bad", lambda r: r.update(result="x"))
    mut("merge posted", lambda r: r.update(result="posted"))
    mut("override dup", lambda r: r.update(override=["allow_self", "allow_self"]))
    mut("override bad", lambda r: r.update(override=["x"]))
    mut("ids bad", lambda r: r.update(ids=["x"]))
    mut("observed bad", lambda r: r.update(observed={"head_sha": SHA}))
    mut("observed extra", lambda r: r.update(observed={"head_sha": SHA, "base_ref": "a", "x": 1}))
    mut("unexpected null observed", lambda r: r.update(result="merged_unexpected"))
    mut("attempted with ids", lambda r: r.update(result="attempted"))
    mut(
        "attempted ok",
        lambda r: (r.update(result="attempted"), r.pop("ids"), r.pop("observed")),
    )
    return out


@pytest.mark.parametrize(("name", "record"), mutations(), ids=[n for n, _ in mutations()])
def test_the_validator_accepts_exactly_what_the_schema_accepts(
    name: str, record: dict[str, Any]
) -> None:
    try:
        validate(record, "forgectl/audit.json")
        schema_ok = True
    except Exception:
        schema_ok = False
    assert (audit.audit_problem(record) is None) is schema_ok, name


@pytest.mark.parametrize("value", [None, [], "x", 3, [1]])
def test_the_validator_rejects_a_non_object(value: Any) -> None:
    assert audit.audit_problem(value) is not None


def test_the_hand_patterns_match_the_schema_files() -> None:
    defs = json.loads(Path("schemas/defs.json").read_text())["$defs"]
    assert audit.PATTERNS["pr"] == defs["pr"]["pattern"]
    assert audit.PATTERNS["host"] == defs["host"]["pattern"]
    assert audit.PATTERNS["ts"] == defs["ts"]["pattern"]
    assert audit.PATTERNS["id"] == defs["id"]["pattern"]
    assert audit.PATTERNS["actor"] == defs["actor"]["pattern"]
    assert audit.PATTERNS["decision_id"] == defs["decision_id"]["pattern"]
    assert audit.PATTERNS["sha"] == defs["sha"]["pattern"]
    assert audit.PATTERNS["sha256"] == defs["sha256"]["pattern"]
    schema = json.loads(Path("schemas/audit.json").read_text())
    assert audit.PATTERNS["attempt_id"] == schema["properties"]["attempt_id"]["pattern"]


@pytest.mark.parametrize(
    ("key", "value"),
    [("ts", "2026-01-01T00:00:00Z\n"), ("attempt_id", "abcdefgh\n"), ("actor", "auto:bob\n")],
)
def test_the_validator_is_stricter_than_the_schema_on_a_trailing_newline(
    key: str, value: str
) -> None:
    record = good()
    record[key] = value
    assert audit.audit_problem(record) is not None
