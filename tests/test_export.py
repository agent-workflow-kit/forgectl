"""``export``, the log, and their wiring into the CLI."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from forgectl import jsonlog
from forgectl.cli import main
from forgectl.humanactions import Tty

from .harness.schemas import validate

NO_TTY = Tty(stdin=False, stderr=False)
SHA = "a" * 40


@pytest.fixture
def state() -> Path:
    return Path(os.environ["XDG_STATE_HOME"]) / "forgectl"


def record(ts: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ts": ts,
        "attempt_id": "abcdefgh12",
        "actor": "auto:bot",
        "decision_id": [],
        "target": "github.com/o/r#7",
        "action": "merge",
        "inputs": {
            "head_sha": SHA,
            "base_ref": "main",
            "event": None,
            "body_sha256": None,
            "method": None,
            "thread_id": None,
            "reply_to": None,
            "config_hash": "b" * 64,
            "config_path": None,
            "host_login": None,
            "token_source": "none",
            "stdin_tty": False,
            "actor_source": "default",
        },
        "result": "attempted",
        "override": [],
    }
    return {**base, **over}


def put(state: Path, *lines: bytes | dict[str, Any]) -> None:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(state / "audit.jsonl", "ab") as handle:
        for line in lines:
            raw = line if isinstance(line, bytes) else json.dumps(line).encode()
            handle.write(raw + b"\n")


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv), tty=NO_TTY)
    out = capsys.readouterr()
    return code, out.out, out.err


# --- export ----


def test_a_missing_file_is_empty_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(capsys, "export") == (0, "", "")
    assert run(capsys, "--json", "export") == (0, "[]\n", "")


def test_records_come_out_as_valid_jsonl_in_file_order(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = record("2026-01-01T00:00:00Z", attempt_id="aaaaaaaa11")
    second = record("2026-01-02T00:00:00Z", attempt_id="bbbbbbbb22", result="failed")
    second.update(ids=[], observed=None)
    put(state, first, second)
    code, out, err = run(capsys, "export")
    assert (code, err) == (0, "")
    lines = [json.loads(x) for x in out.splitlines()]
    assert [x["attempt_id"] for x in lines] == ["aaaaaaaa11", "bbbbbbbb22"]
    for item in lines:
        validate(item, "forgectl/audit.json")


def test_json_prints_one_array_valid_against_the_export_schema(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(state, record("2026-01-01T00:00:00Z"), record("2026-01-02T00:00:00Z"))
    code, out, _ = run(capsys, "--json", "export")
    assert code == 0 and out.count("\n") == 1
    validate(json.loads(out), "forgectl/cli/export.json")
    assert len(json.loads(out)) == 2


def test_since_takes_an_rfc3339_time(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    put(
        state,
        record("2026-01-01T00:00:00Z", attempt_id="old00000"),
        record("2026-01-02T00:00:00Z", attempt_id="edge0000"),
        record("2026-01-03T00:00:00+00:00", attempt_id="new00000"),
        record("2026-01-02T05:00:00+09:00", attempt_id="zone0000"),  # 2026-01-01T20:00Z: before
    )
    _, out, _ = run(capsys, "export", "--since", "2026-01-02T00:00:00Z")
    assert [json.loads(x)["attempt_id"] for x in out.splitlines()] == ["edge0000", "new00000"]


def test_since_takes_a_duration(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    now = datetime.now(UTC)

    def stamp(delta: timedelta) -> str:
        return (now - delta).isoformat(timespec="seconds").replace("+00:00", "Z")

    put(
        state,
        record(stamp(timedelta(days=3)), attempt_id="old00000"),
        record(stamp(timedelta(hours=2)), attempt_id="new00000"),
    )
    _, out, _ = run(capsys, "export", "--since", "1d")
    assert [json.loads(x)["attempt_id"] for x in out.splitlines()] == ["new00000"]
    _, out, _ = run(capsys, "export", "--since", "36500d")
    assert len(out.splitlines()) == 2


def test_since_in_the_future_is_empty(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    put(state, record("2026-01-01T00:00:00Z"))
    assert run(capsys, "export", "--since", "2999-01-01T00:00:00Z") == (0, "", "")


def test_malformed_lines_warn_and_are_skipped(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_schema = record("2026-01-01T00:00:00Z", extra="x")
    dup = b'{"ts":"2026-01-01T00:00:00Z","ts":"2026-01-01T00:00:00Z"}'
    put(
        state,
        b"not json at all SECRET-TEXT",
        b"[1,2]",
        b'{"ts": NaN}',
        dup,
        b"\xff\xfe\x00 bytes",
        bad_schema,
        record("2026-01-05T00:00:00Z", attempt_id="good0000"),
    )
    code, out, err = run(capsys, "export")
    assert code == 0
    assert [json.loads(x)["attempt_id"] for x in out.splitlines()] == ["good0000"]
    assert err.count("warning:") == 6
    assert "SECRET-TEXT" not in err
    assert "line 1 skipped" in err


def test_a_torn_last_line_is_skipped(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    put(state, record("2026-01-01T00:00:00Z", attempt_id="good0000"))
    with open(state / "audit.jsonl", "ab") as handle:
        handle.write(json.dumps(record("2026-01-02T00:00:00Z")).encode()[:40])  # torn, no newline
    code, out, err = run(capsys, "export")
    assert code == 0 and len(out.splitlines()) == 1
    assert err.count("warning:") == 1
    assert "line 2 skipped: incomplete last line" in err


def test_a_valid_last_line_without_a_newline_is_exported(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state.mkdir(parents=True, mode=0o700)
    (state / "audit.jsonl").write_bytes(json.dumps(record("2026-01-01T00:00:00Z")).encode())
    code, out, err = run(capsys, "export")
    assert (code, err) == (0, "") and len(out.splitlines()) == 1


def test_a_malformed_last_line_without_a_newline_does_not_repeat_its_content(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state.mkdir(parents=True, mode=0o700)
    (state / "audit.jsonl").write_bytes(b'{"SECRET-TEXT":')
    code, out, err = run(capsys, "export")
    assert (code, out) == (0, "")
    assert "line 1 skipped: incomplete last line" in err and "SECRET-TEXT" not in err


def test_an_oversized_line_is_skipped_without_losing_the_next(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    huge = b'{"x":"' + b"a" * (1024 * 1024) + b'"}'
    put(state, huge, record("2026-01-01T00:00:00Z", attempt_id="good0000"))
    code, out, err = run(capsys, "export")
    assert code == 0 and [json.loads(x)["attempt_id"] for x in out.splitlines()] == ["good0000"]
    assert err.count("warning:") == 1


def test_warnings_are_capped_with_a_count_of_the_rest(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(state, *([b"junk"] * 50))
    _, _, err = run(capsys, "export")
    assert err.count("warning:") == 21
    assert "30 more lines skipped" in err


def test_empty_lines_are_ignored_quietly(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    put(state, b"", record("2026-01-01T00:00:00Z"), b"")
    code, out, err = run(capsys, "export")
    assert (code, err) == (0, "") and len(out.splitlines()) == 1


def test_a_whitespace_only_line_warns_as_malformed(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(state, b"   ", b"\t", record("2026-01-01T00:00:00Z"))
    code, out, err = run(capsys, "export")
    assert code == 0 and len(out.splitlines()) == 1
    assert err.count("warning:") == 2 and "line 1 skipped: malformed" in err


def test_output_is_sanitized_for_untrusted_fields(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evil = "x\n\x1b[31m‮⁦y\r\ud800"
    rec = record("2026-01-01T00:00:00Z")
    rec["inputs"]["base_ref"] = evil
    rec["inputs"]["host_login"] = evil
    rec["result"] = "merged_unexpected"
    rec.update(ids=[], observed={"head_sha": SHA, "base_ref": evil})
    put(state, rec)
    for flags in ((), ("--json",)):
        code, out, _ = run(capsys, *flags, "export")
        assert code == 0
        assert out.isascii()
        assert "\\u202e" not in out and "\\u001b" not in out and "\\ud800" not in out
        parsed = json.loads(out)
        item = parsed[0] if flags else parsed
        assert (
            item["inputs"]["base_ref"] == "x<U+000A><U+001B>[31m<U+202E><U+2066>y<U+000D><U+D800>"
        )
        assert item["observed"]["base_ref"] == item["inputs"]["base_ref"]
        validate(item, "forgectl/audit.json")


def test_a_symlinked_audit_file_is_refused_not_followed(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state.mkdir(parents=True, mode=0o700)
    target = tmp_path / "elsewhere.jsonl"
    target.write_text(json.dumps(record("2026-01-01T00:00:00Z")) + "\n")
    (state / "audit.jsonl").symlink_to(target)
    code, out, err = run(capsys, "--json", "export")
    assert code == 12
    assert json.loads(out)["error"]["code"] == "audit_unavailable"
    assert "audit_unavailable" in err


def test_a_fifo_audit_file_is_refused_without_hanging(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state.mkdir(parents=True, mode=0o700)
    os.mkfifo(state / "audit.jsonl")
    code, _, err = run(capsys, "export")
    assert code == 12 and "audit_unavailable" in err


def test_a_state_directory_others_can_write_is_refused(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    put(state, record("2026-01-01T00:00:00Z"))
    state.chmod(0o777)
    code, _, err = run(capsys, "export")
    assert code == 12 and "audit_unavailable" in err


@pytest.mark.parametrize("bad", [0o666, 0o620, 0o602])
def test_an_audit_file_others_can_write_is_refused(
    state: Path, capsys: pytest.CaptureFixture[str], bad: int
) -> None:
    put(state, record("2026-01-01T00:00:00Z"))
    (state / "audit.jsonl").chmod(bad)
    code, out, err = run(capsys, "--json", "export")
    assert code == 12
    assert json.loads(out)["error"]["code"] == "audit_unavailable"
    assert "audit_unavailable" in err


@pytest.mark.parametrize("good", [0o600, 0o644])
def test_an_audit_file_others_cannot_write_is_exported(
    state: Path, capsys: pytest.CaptureFixture[str], good: int
) -> None:
    put(state, record("2026-01-01T00:00:00Z"))
    (state / "audit.jsonl").chmod(good)
    code, out, _ = run(capsys, "export")
    assert code == 0 and out.count("\n") == 1


def test_export_does_not_create_the_state_directory(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "export")
    assert not state.exists() or not (state / "audit.jsonl").exists()


def test_a_closed_stdout_does_not_change_the_result_code(
    state: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put(state, record("2026-01-01T00:00:00Z"))

    class Broken:
        def write(self, _: str) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise BrokenPipeError

        def fileno(self) -> int:
            raise OSError

    monkeypatch.setattr("sys.stdout", Broken())
    assert main(["export"], tty=NO_TTY) == 0


# --- log.jsonl ----


def logged(state: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (state / "log.jsonl").read_text().splitlines()]


def test_a_log_event_validates_against_the_log_schema(state: Path) -> None:
    warning = jsonlog.log_event(
        state, "warn", "stale_head", pr="github.com/o/r#7", host="github.com", details={"a": 1}
    )
    assert warning is None
    [item] = logged(state)
    validate(item, "forgectl/log.json")
    assert (item["level"], item["event"], item["pr"], item["host"]) == (
        "warn",
        "stale_head",
        "github.com/o/r#7",
        "github.com",
    )


def test_a_pr_or_host_that_is_not_in_schema_form_is_recorded_as_null(state: Path) -> None:
    jsonlog.log_event(state, "info", "usage", pr="not\na pr", host="Bad Host\n")
    [item] = logged(state)
    assert item["pr"] is None and item["host"] is None
    validate(item, "forgectl/log.json")


def test_urls_lose_userinfo_and_token_shapes_are_masked(state: Path) -> None:
    secrets = [
        "hunter2",
        "ghp_abcdefghijklmnop",
        "glpat" + "-abcdefghijklmnop",
        "github_pat_AAAAAAAAAAA",
    ]
    jsonlog.log_event(
        state,
        "warn",
        "upstream_error",
        details={
            "url": "https://bob:hunter2" + "@github.com/o/r?x=1",
            "nested": [{"m": "token ghp_abcdefghijklmnop and glpat" + "-abcdefghijklmnop"}],
            "pat": "github_pat_AAAAAAAAAAA",
        },
    )
    text = (state / "log.jsonl").read_text()
    for secret in secrets:
        assert secret not in text
    assert "https://github.com/o/r?x=1" in text


def test_details_are_bounded(state: Path) -> None:
    jsonlog.log_event(state, "info", "big", details={"s": "x" * 10**6, "l": list(range(10**5))})
    [item] = logged(state)
    assert len(item["details"]["s"]) < 3000 and len(item["details"]["l"]) == 64


def test_hostile_details_stay_one_line_and_unserializable_values_are_replaced(
    state: Path,
) -> None:
    jsonlog.log_event(state, "info", "odd", details={"a": "x\ny\x00‮", "b": object(), "c": nan()})
    assert len((state / "log.jsonl").read_text().splitlines()) == 1
    [item] = logged(state)
    assert item["details"]["b"] == "<unserializable>" and item["details"]["c"] is None


def nan() -> float:
    return float("nan")


def test_a_log_failure_returns_a_warning_and_never_raises(state: Path, tmp_path: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    (state / "log.jsonl").symlink_to(tmp_path / "x")
    text = jsonlog.log_event(state, "error", "internal")
    assert text is not None and "log not written" in text
    assert not (tmp_path / "x").exists()


def test_the_validator_rejects_bad_log_records() -> None:
    good = {
        "ts": "2026-01-01T00:00:00Z",
        "level": "info",
        "event": "x",
        "pr": None,
        "host": None,
        "details": None,
    }
    assert jsonlog.log_problem(good) is None
    for key, value in [
        ("level", "fatal"),
        ("event", "Bad"),
        ("event", "x\n"),
        ("ts", "yesterday"),
        ("pr", "x"),
        ("details", []),
    ]:
        assert jsonlog.log_problem({**good, key: value}) is not None, key
    assert jsonlog.log_problem({**good, "extra": 1}) is not None


def test_the_hand_patterns_match_the_schema_files() -> None:
    defs = json.loads(Path("schemas/defs.json").read_text())["$defs"]
    for name in ("host", "pr", "ts", "id", "actor", "decision_id", "sha", "sha256"):
        assert jsonlog.PATTERNS[name] == defs[name]["pattern"], name
    audit_schema = json.loads(Path("schemas/audit.json").read_text())
    assert jsonlog.PATTERNS["attempt_id"] == audit_schema["properties"]["attempt_id"]["pattern"]
    log_schema = json.loads(Path("schemas/log.json").read_text())
    assert jsonlog.PATTERNS["event"] == log_schema["properties"]["event"]["pattern"]


# --- the CLI writes the log ----

PR = "github.com/o/r#7"


def test_an_internal_failure_is_logged_at_error_level(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = run(
        capsys,
        "approve",
        PR,
        "--expected-head",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--expected-base",
        "main",
    )  # no handler yet: exit 1
    assert code == 1
    [item] = logged(state)
    validate(item, "forgectl/log.json")
    assert (item["level"], item["event"]) == ("error", "internal")
    assert item["details"]["command"] == "approve" and item["details"]["exit_code"] == 1


def test_a_usage_error_is_logged_at_info_level(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = run(capsys, "show")
    assert code == 2
    [item] = logged(state)
    assert (item["level"], item["event"]) == ("info", "usage")


def test_a_successful_read_logs_nothing_unless_verbose(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "export")
    assert not (state / "log.jsonl").exists()
    run(capsys, "--verbose", "export")
    [item] = logged(state)
    assert (item["level"], item["event"]) == ("debug", "command_done")


@pytest.mark.parametrize(
    ("code", "reason", "level"),
    [
        ("not_found", None, "info"),
        ("auth_required", None, "warn"),
        ("refused", "stale_head", "warn"),
    ],
)
def test_failure_levels_follow_the_interface(code: str, reason: str | None, level: str) -> None:
    from forgectl.cli import _failure_level
    from forgectl.errors import ForgectlError

    err = ForgectlError(code, "m", reason=reason, details={"head_sha": SHA} if reason else None)
    assert _failure_level(err) == level


def test_an_unwritable_log_warns_and_keeps_the_result_code(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state.mkdir(parents=True, mode=0o700)
    (state / "log.jsonl").symlink_to(tmp_path / "x")
    code, _, err = run(
        capsys,
        "approve",
        PR,
        "--expected-head",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--expected-base",
        "main",
    )
    assert code == 1
    assert "warning: log not written" in err
    assert not (tmp_path / "x").exists()


# --- review round 1 ----------


@pytest.mark.parametrize("json_flag", [False, True])
def test_wrong_typed_string_fields_are_skipped_with_a_warning_each(
    state: Path, capsys: pytest.CaptureFixture[str], json_flag: bool
) -> None:
    put(
        state,
        record("2026-01-01T00:00:00Z", attempt_id="good0001"),
        record("2026-01-01T00:00:00Z", action=["merge"]),
        record("2026-01-01T00:00:00Z", action={}),
        record("2026-01-01T00:00:00Z", result=[]),
        record("2026-01-01T00:00:00Z", result={}),
        record("2026-01-02T00:00:00Z", attempt_id="good0002"),
    )
    code, out, err = run(capsys, *(["--json"] if json_flag else []), "export")
    assert code == 0
    items = json.loads(out) if json_flag else [json.loads(x) for x in out.splitlines()]
    assert [x["attempt_id"] for x in items] == ["good0001", "good0002"]
    assert err.count("warning:") == 4


def test_any_exception_while_validating_a_line_is_treated_as_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from forgectl import audit, exportcmd

    def broken(_: object) -> str | None:
        raise TypeError("validator bug")

    monkeypatch.setattr(audit, "audit_problem", broken)
    assert exportcmd.parse_line(json.dumps(record("2026-01-01T00:00:00Z")).encode()) is None


def test_a_producer_error_while_streaming_is_not_taken_for_a_broken_pipe() -> None:
    from forgectl.output import _write_stream

    def records() -> Any:
        yield {"a": 1}
        raise ValueError("producer bug")

    class Sink:
        text = ""

        def write(self, data: str) -> int:
            self.text += data
            return len(data)

        def flush(self) -> None:
            pass

    with pytest.raises(ValueError, match="producer bug"):
        _write_stream(Sink(), records(), array=False)  # type: ignore[arg-type]


def test_an_append_after_a_torn_fragment_is_exported_and_the_fragment_warns_once(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state.mkdir(parents=True, mode=0o700)
    (state / "audit.jsonl").write_bytes(b'{"ts":"2026-01-01T00:00:00Z","attem')
    jsonlog.append(state, "audit.jsonl", record("2026-01-02T00:00:00Z", attempt_id="good0000"))
    code, out, err = run(capsys, "export")
    assert code == 0 and [json.loads(x)["attempt_id"] for x in out.splitlines()] == ["good0000"]
    assert err.count("warning:") == 1 and "line 1 skipped: malformed" in err


def test_a_hard_linked_audit_file_is_refused_for_export(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state.mkdir(parents=True, mode=0o700)
    other = tmp_path / "other.jsonl"
    other.write_text(json.dumps(record("2026-01-01T00:00:00Z")) + "\n")
    os.link(other, state / "audit.jsonl")
    code, out, err = run(capsys, "--json", "export")
    assert code == 12
    assert json.loads(out)["error"]["code"] == "audit_unavailable"


def test_an_unexpected_exception_is_logged_at_error_level(
    state: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from forgectl import registry

    def boom(_: object) -> dict[str, Any]:
        raise RuntimeError("handler bug with ghp_abcdefghijklmnop")

    registry.load_handlers()
    monkeypatch.setitem(registry.HANDLERS, "export", boom)
    code, _, err = run(capsys, "export")
    assert code == 1 and "internal" in err
    [item] = logged(state)
    validate(item, "forgectl/log.json")
    assert (item["level"], item["event"]) == ("error", "internal")
    assert item["details"]["command"] == "export" and item["details"]["exit_code"] == 1
    assert "ghp_" not in (state / "log.jsonl").read_text()


@pytest.mark.parametrize("verbose", [False, True])
def test_a_successful_write_logs_command_done_at_info(
    state: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, verbose: bool
) -> None:
    from forgectl import registry

    registry.load_handlers()
    monkeypatch.setitem(registry.HANDLERS, "resolve", lambda _: {"resolved": True})
    argv = ["resolve", PR, "--thread", "gh:rt:abc", "--actor", "auto:bot"]
    code, _, _ = run(capsys, *(["--verbose"] if verbose else []), *argv)
    assert code == 0
    [item] = logged(state)
    validate(item, "forgectl/log.json")
    assert (item["level"], item["event"]) == ("info", "command_done")
    assert item["details"]["command"] == "resolve"


def test_success_log_levels() -> None:
    from forgectl.cli import _success_level

    assert _success_level("resolve", verbose=False) == "info"
    assert _success_level("merge", verbose=True) == "info"
    assert _success_level("show", verbose=True) == "debug"
    assert _success_level("export", verbose=False) is None
