"""`forgectl merge` on GitHub (#21): the fixed order, the checks, the write and its verification.

The real CLI runs in process against the fake `gh` (tests/fakebin), which logs every call, so each
test asserts what reached the host (the PUT in particular) as well as what was printed. The
recordings are hand-built from GitHub's documented GraphQL and REST shapes and scrubbed. Every
assumed GitHub behavior (the merge route's statuses and wordings) is UNVERIFIED until the live
suite (#31, #32) re-checks it.
"""

from __future__ import annotations

import copy
import io
import json
import os
import signal
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from forgectl import merge as merge_module
from forgectl.cli import main
from forgectl.humanactions import Tty

from .harness import schemas
from .harness.fixtures import forgectl_env
from .harness.forges import Call, FakeForges
from .harness.procs import ProcessRegistry
from .harness.safety import assert_call_safe
from .test_github_show import HEAD, OTHER, TOKEN, recording

PR = "github.com/example-org/example-repo#7"
REPO = "example-org/example-repo"
MERGE_ENDPOINT = f"repos/{REPO}/pulls/7/merge"
MERGE_SHA = "7777777777777777777777777777777777777777"
BASE = "main"
HUMAN = Tty(stdin=True, stderr=True)
NO_TTY = Tty(stdin=False, stderr=False)


# -- recordings --------------------------------------------------------------------------------


def doc(
    *,
    state: str = "OPEN",
    head: str = HEAD,
    base: str = BASE,
    ci: str = "passing",
    merge_state: str = "CLEAN",
    queue: bool = False,
    methods: tuple[bool, bool, bool] = (True, False, False),  # squash, merge, rebase
) -> dict[str, Any]:
    """The open-clean recording, changed: the PR state, head, base, CI, host status, queue and the
    repository's allowed merge methods."""
    d = recording()
    repo = d["data"]["repository"]
    pr = repo["pullRequest"]
    pr["state"] = state
    pr["headRefOid"] = head
    pr["baseRefName"] = base
    pr["isMergeQueueEnabled"] = queue
    pr["mergeStateStatus"] = merge_state
    commit = pr["commits"]["nodes"][-1]["commit"]
    commit["oid"] = head
    contexts = commit["statusCheckRollup"]["contexts"]["nodes"]
    if ci == "failing":
        contexts[0]["conclusion"] = "FAILURE"
    elif ci == "pending":
        contexts[0]["conclusion"] = None
        contexts[0]["status"] = "IN_PROGRESS"
        contexts[0]["completedAt"] = None
    elif ci == "none":
        commit["statusCheckRollup"] = None
    repo["squashMergeAllowed"], repo["mergeCommitAllowed"], repo["rebaseMergeAllowed"] = methods
    if state == "MERGED":
        pr["mergedAt"] = "2026-10-02T11:00:00Z"
        pr["closedAt"] = "2026-10-02T11:00:00Z"
        pr["mergedBy"] = {"__typename": "User", "databaseId": 1002, "login": "user-2"}
        pr["mergeCommit"] = {"oid": MERGE_SHA}
        pr["mergeStateStatus"] = "UNKNOWN"
        pr["mergeable"] = "UNKNOWN"
    if state == "CLOSED":
        pr["closedAt"] = "2026-10-02T11:00:00Z"
    return d


def merged(**kw: Any) -> dict[str, Any]:
    return doc(state="MERGED", **kw)


MERGE_OK = {"sha": MERGE_SHA, "merged": True, "message": "Pull Request successfully merged"}


# -- the rig -----------------------------------------------------------------------------------


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def json(self) -> Any:
        return json.loads(self.out)

    @property
    def error(self) -> dict[str, Any]:
        body = self.json["error"]
        schemas.validate_error_envelope(self.json)
        assert isinstance(body, dict)
        return body


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        forges: FakeForges,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self.tmp = tmp_path
        self.forges = forges
        self.monkeypatch = monkeypatch
        self.capsys = capsys
        self.config_text = "hosts:\n  github.com:\n    token_env: MERGE_TOKEN\n"
        self.sleeps: list[float] = []
        for key, value in forgectl_env(forges, {"MERGE_TOKEN": TOKEN}).items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(merge_module, "_sleep", self.sleeps.append)

    # scripting
    def reads(self, *docs: dict[str, Any]) -> None:
        """One answer per read, in order; the last one repeats."""
        for index, d in enumerate(docs):
            last = index == len(docs) - 1
            self.forges.answer("gh", "graphql", method="POST", body=d, times=None if last else 1)

    def put(self, status: int = 200, body: Any = MERGE_OK, **kw: Any) -> None:
        self.forges.answer("gh", MERGE_ENDPOINT, method="PUT", status=status, body=body, **kw)

    # running
    def config(self, extra: str = "") -> Path:
        path = self.tmp / "config.yaml"
        path.write_text(self.config_text + extra, encoding="utf-8")
        return path

    def run(
        self,
        *args: str,
        tty: Tty = NO_TTY,
        stdin: str = "",
        extra_config: str = "",
        expected: bool = True,
        method: str | None = "squash",
        actor: str | None = "auto:bot",
        yes: bool = True,
    ) -> Result:
        argv = ["merge", PR, "--json", "--config", str(self.config(extra_config))]
        if expected:
            argv += ["--expected-head", HEAD, "--expected-base", BASE]
        if method:
            argv += ["--method", method]
        if actor:
            argv += ["--actor", actor]
        if yes:
            argv += ["--yes"]
        argv += list(args)
        self.monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = main(argv, tty=tty)
        captured = self.capsys.readouterr()
        return Result(code, captured.out, captured.err)

    # observing
    def calls(self) -> list[Call]:
        return self.forges.calls("gh")

    def puts(self) -> list[Call]:
        return [c for c in self.calls() if c.argv[-1] == MERGE_ENDPOINT]

    def state_file(self, name: str) -> list[dict[str, Any]]:
        path = Path(os.environ["XDG_STATE_HOME"]) / "forgectl" / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def audit(self) -> list[dict[str, Any]]:
        records = self.state_file("audit.jsonl")
        for record in records:
            schemas.validate_audit_record(record)
        return records

    def log(self) -> list[dict[str, Any]]:
        return self.state_file("log.jsonl")


@pytest.fixture
def rig(
    tmp_path: Path,
    fake_forges: FakeForges,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Rig:
    return Rig(tmp_path, fake_forges, monkeypatch, capsys)


def results(records: list[dict[str, Any]]) -> list[str]:
    return [r["result"] for r in records]


def assert_no_write(rig: Rig) -> None:
    assert rig.puts() == []
    assert rig.audit() == []


# -- the happy path ----------------------------------------------------------------------------


def test_a_merge_sends_the_expected_head_as_sha_and_verifies_by_reading(rig: Rig) -> None:
    rig.reads(doc(), merged())
    rig.put()
    done = rig.run()
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("merge", out)
    assert out == {
        "pr": PR,
        "merged": True,
        "already_merged": False,
        "auto_merge_enabled": False,
        "merged_head_sha": HEAD,
        "merge_commit_sha": MERGE_SHA,
        "method": "squash",
        "dry_run": False,
    }
    (put,) = rig.puts()
    assert json.loads(put.stdin or "") == {"sha": HEAD, "merge_method": "squash"}
    assert [c.argv[-1] for c in rig.calls()] == ["graphql", MERGE_ENDPOINT, "graphql"]
    for call in rig.calls():
        assert_call_safe(call, host="github.com")
        assert call.token_sent == TOKEN


def test_no_merge_message_is_ever_sent(rig: Rig) -> None:
    rig.reads(doc(), merged())
    rig.put()
    rig.run()
    (put,) = rig.puts()
    body = json.loads(put.stdin or "")
    assert set(body) <= {"sha", "merge_method"}


def test_the_audit_records_attempted_before_the_write_and_one_outcome_after(rig: Rig) -> None:
    rig.reads(doc(), merged())
    rig.put()
    rig.run("--decision-id", "d-1")
    first, second = rig.audit()
    assert first["attempt_id"] == second["attempt_id"]
    assert (first["result"], second["result"]) == ("attempted", "merged")
    assert first["action"] == "merge" and first["target"] == PR
    assert first["inputs"]["head_sha"] == HEAD
    assert first["inputs"]["method"] == "squash"
    assert first["decision_id"] == ["d-1"]
    assert first["actor"] == "auto:bot" and first["inputs"]["actor_source"] == "flag"
    assert first["inputs"]["token_source"] == "token_env"
    assert TOKEN not in json.dumps(rig.audit()) + json.dumps(rig.log())


def test_attempted_is_on_disk_before_the_put_reaches_the_host(rig: Rig) -> None:
    seen: list[list[str]] = []

    def spy(answered: Any) -> None:
        seen.append(results(rig.audit()))

    rig.reads(doc(), merged())
    original = merge_module.MergeRun._merge_and_verify

    def wrapped(self: Any, current: Any, method: Any) -> Any:
        spy(None)
        return original(self, current, method)

    rig.monkeypatch.setattr(merge_module.MergeRun, "_merge_and_verify", wrapped)
    rig.put()
    rig.run()
    assert seen == [["attempted"]]


# -- refusals before any write -----------------------------------------------------------------


def test_a_stale_head_is_refused_with_no_write(rig: Rig) -> None:
    rig.reads(doc(head=OTHER))
    done = rig.run()
    assert done.code == 20
    err = done.error
    assert (err["code"], err["reason"]) == ("refused", "stale_head")
    assert err["details"] == {"head_sha": OTHER}
    assert_no_write(rig)


def test_a_stale_base_is_refused_with_no_write(rig: Rig) -> None:
    rig.reads(doc(base="release"))
    done = rig.run()
    assert done.code == 20
    assert done.error["reason"] == "stale_base"
    assert done.error["details"] == {"base_ref": "release"}
    assert_no_write(rig)


def test_a_base_with_a_marked_character_always_refuses(rig: Rig) -> None:
    marked = "ma\x07in"  # sanitized to ma<U+0007>in by the read
    rig.reads(doc(base=marked))
    done = rig.run()
    assert done.code == 20 and done.error["reason"] == "stale_base"
    assert_no_write(rig)


def test_a_caller_who_types_the_marker_text_does_not_name_the_character(
    rig: Rig, tmp_path: Path
) -> None:
    rig.reads(doc(base="ma\x07in"))
    argv = [
        "merge", PR, "--json", "--config", str(rig.config()), "--expected-head", HEAD,
        "--expected-base", "ma<U+0007>in", "--method", "squash", "--actor", "auto:bot", "--yes",
    ]  # fmt: skip
    code = main(argv, tty=NO_TTY)
    assert code == 20
    assert rig.puts() == []


def test_a_closed_pr_is_not_open(rig: Rig) -> None:
    rig.reads(doc(state="CLOSED"))
    done = rig.run()
    assert done.code == 20
    assert (done.error["reason"], done.error["details"]) == ("not_open", {"state": "closed"})
    assert_no_write(rig)


def test_blockers_refuse_as_not_ready(rig: Rig) -> None:
    rig.reads(doc(merge_state="BEHIND"))
    done = rig.run()
    assert done.code == 20 and done.error["reason"] == "not_ready"
    assert {"code": "branch_behind"} in done.error["details"]["blocked_by"]
    assert_no_write(rig)


def test_already_merged_at_the_expected_head_and_base_is_success_without_audit(rig: Rig) -> None:
    rig.reads(merged())
    done = rig.run()
    assert done.code == 0
    out = done.json
    schemas.validate_cli_output("merge", out)
    assert out["already_merged"] is True and out["merged"] is True
    assert out["merged_head_sha"] == HEAD and out["merge_commit_sha"] == MERGE_SHA
    assert_no_write(rig)


def test_merged_at_another_head_is_stale_head_not_success(rig: Rig) -> None:
    rig.reads(merged(head=OTHER))
    done = rig.run()
    assert done.code == 20 and done.error["reason"] == "stale_head"
    assert_no_write(rig)


def test_merged_into_another_base_is_stale_base(rig: Rig) -> None:
    rig.reads(merged(base="release"))
    done = rig.run()
    assert done.code == 20 and done.error["reason"] == "stale_base"
    assert done.error["details"] == {"base_ref": "release"}
    assert_no_write(rig)


def test_an_already_merged_pr_runs_no_hooks(rig: Rig, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    rig.reads(merged())
    done = rig.run(extra_config=hook_config("pre-merge", f"touch {marker}"))
    assert done.code == 0
    assert not marker.exists()


# -- merge method ------------------------------------------------------------------------------


def test_the_only_allowed_method_is_used(rig: Rig) -> None:
    rig.reads(doc(methods=(False, True, False)), merged())
    rig.put()
    done = rig.run(method=None)
    assert done.code == 0 and done.json["method"] == "merge"
    assert json.loads(rig.puts()[0].stdin or "")["merge_method"] == "merge"


def test_two_allowed_methods_and_none_named_is_method_required(rig: Rig) -> None:
    rig.reads(doc(methods=(True, True, False)))
    done = rig.run(method=None)
    assert done.code == 2
    assert (done.error["code"], done.error["details"]) == (
        "method_required",
        {"allowed": ["squash", "merge"]},
    )
    assert_no_write(rig)


def test_a_repo_override_names_the_method(rig: Rig) -> None:
    rig.reads(doc(methods=(True, True, False)), merged())
    rig.put()
    done = rig.run(
        method=None,
        extra_config=f"repos:\n  github.com/{REPO}:\n    merge_method: merge\n",
    )
    assert done.code == 0 and done.json["method"] == "merge"


def test_the_flag_beats_the_repo_override(rig: Rig) -> None:
    rig.reads(doc(methods=(True, True, False)), merged())
    rig.put()
    done = rig.run(
        method="squash",
        extra_config=f"repos:\n  github.com/{REPO}:\n    merge_method: merge\n",
    )
    assert done.json["method"] == "squash"


def test_a_method_the_repo_disallows_is_refused_before_the_call(rig: Rig) -> None:
    rig.reads(doc(methods=(True, False, False)))
    done = rig.run(method="rebase")
    assert done.code == 20
    assert (done.error["reason"], done.error["details"]) == (
        "method_not_allowed",
        {"allowed": ["squash"]},
    )
    assert_no_write(rig)


# -- who may merge -----------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--override-blocks", "--override-escalation"])
def test_a_human_only_flag_without_a_human_is_refused_before_any_read(rig: Rig, flag: str) -> None:
    done = rig.run(flag)
    assert done.code == 2
    assert rig.calls() == []
    assert rig.audit() == []


def test_a_human_only_flag_under_an_auto_actor_on_a_tty_is_refused(rig: Rig) -> None:
    done = rig.run("--override-blocks", tty=HUMAN, actor="auto:cron")
    assert done.code == 2 and rig.calls() == []


def test_an_auto_actor_is_never_interactive(rig: Rig) -> None:
    rig.reads(doc())
    done = rig.run(tty=HUMAN, actor="auto:cron", yes=False)
    assert done.code == 13
    assert done.error["code"] == "confirmation_required"
    assert rig.puts() == []


def test_missing_expected_values_are_usage_errors_before_any_read(rig: Rig) -> None:
    done = rig.run(expected=False)
    assert done.code == 2 and rig.calls() == []


def test_a_non_human_needs_passing_ci(rig: Rig) -> None:
    rig.reads(doc(ci="failing"))
    done = rig.run()
    assert done.code == 20 and done.error["reason"] == "not_ready"
    assert {"code": "checks_failing"} in done.error["details"]["blocked_by"]
    assert_no_write(rig)


def test_a_non_human_with_no_ci_evidence_gets_checks_absent(rig: Rig) -> None:
    rig.reads(doc(ci="none"))
    done = rig.run()
    assert done.code == 20 and done.error["reason"] == "not_ready"
    assert done.error["details"]["blocked_by"] == [{"code": "checks_absent"}]
    assert_no_write(rig)


def test_a_human_may_merge_a_pr_with_no_ci(rig: Rig) -> None:
    rig.reads(doc(ci="none"), merged())
    rig.put()
    done = rig.run(actor="human:me")
    assert done.code == 0, done.err


@pytest.mark.parametrize("queue", [True, None])
def test_a_merge_queue_refuses_a_non_human(rig: Rig, queue: bool | None) -> None:
    d = doc(queue=bool(queue))
    if queue is None:
        del d["data"]["repository"]["pullRequest"]["isMergeQueueEnabled"]
    rig.reads(d)
    done = rig.run()
    assert done.code == 20 and done.error["reason"] == "no_head_match"
    assert_no_write(rig)


def test_a_human_may_merge_on_a_queue_and_the_host_gets_sha(rig: Rig) -> None:
    rig.reads(doc(queue=True), merged())
    rig.put()
    done = rig.run(actor="human:me")
    assert done.code == 0
    assert json.loads(rig.puts()[0].stdin or "")["sha"] == HEAD


def test_override_blocks_skips_not_ready_and_is_audited(rig: Rig) -> None:
    rig.reads(doc(ci="failing"), merged())
    rig.put()
    done = rig.run("--override-blocks", actor="human:me")
    assert done.code == 0, done.err
    assert rig.audit()[0]["override"] == ["override_blocks"]


def test_override_blocks_without_blockers_changes_nothing_and_is_not_recorded(rig: Rig) -> None:
    rig.reads(doc(), merged())
    rig.put()
    rig.run("--override-blocks", actor="human:me")
    assert rig.audit()[0]["override"] == []


@pytest.mark.parametrize(
    ("first", "reason"),
    [
        (doc(head=OTHER, ci="failing"), "stale_head"),
        (doc(base="release", ci="failing"), "stale_base"),
        (doc(state="CLOSED"), "not_open"),
        (doc(head=OTHER, state="MERGED"), "stale_head"),
    ],
)
def test_override_blocks_never_skips_the_other_refusals(
    rig: Rig, first: dict[str, Any], reason: str
) -> None:
    rig.reads(first)
    done = rig.run("--override-blocks", actor="human:me")
    assert done.code == 20 and done.error["reason"] == reason
    assert_no_write(rig)


# -- dry run and confirmation ------------------------------------------------------------------


def test_a_dry_run_prints_the_plan_and_never_writes(rig: Rig) -> None:
    rig.reads(doc())
    done = rig.run("--dry-run", yes=False)
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("merge", out)
    assert out["dry_run"] is True and out["would_refuse"] is None and out["merged"] is False
    assert out["plan"] == {
        "head_sha": HEAD,
        "expected_head": HEAD,
        "expected_base": BASE,
        "base_ref": BASE,
        "auto": False,
        "override_blocks": False,
        "blocked_by": [],
        "method": "squash",
    }
    assert [c.argv[-1] for c in rig.calls()] == ["graphql"]
    assert rig.audit() == []


def test_a_dry_run_reports_a_refusal_as_would_refuse_with_the_plan(rig: Rig) -> None:
    rig.reads(doc(head=OTHER))
    done = rig.run("--dry-run", yes=False)
    assert done.code == 20
    out = done.json
    schemas.validate_cli_output("merge", out)
    assert out["would_refuse"]["reason"] == "stale_head"
    assert out["plan"]["head_sha"] == OTHER and out["plan"]["expected_head"] == HEAD
    assert rig.puts() == []


def test_a_dry_run_runs_no_hooks(rig: Rig, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    rig.reads(doc())
    rig.run("--dry-run", extra_config=hook_config("pre-merge", f"touch {marker}"))
    assert not marker.exists()


def test_without_yes_a_non_interactive_run_needs_confirmation(rig: Rig) -> None:
    rig.reads(doc())
    done = rig.run(yes=False)
    assert done.code == 13 and done.error["code"] == "confirmation_required"
    assert rig.puts() == []
    assert rig.audit() == []


def test_an_interactive_run_pins_the_first_read_and_asks(rig: Rig) -> None:
    rig.reads(doc(), doc(), merged())  # the dialog is followed by a re-read
    rig.put()
    done = rig.run(expected=False, actor=None, yes=False, tty=HUMAN, stdin="y\n")
    assert done.code == 0, done.err
    assert "merge github.com" in done.err and HEAD in done.err
    assert json.loads(rig.puts()[0].stdin or "")["sha"] == HEAD
    assert rig.audit()[0]["inputs"]["actor_source"] == "tty"
    assert rig.audit()[0]["inputs"]["stdin_tty"] is True


def test_a_declined_dialog_is_exit_13_with_no_audit_record(rig: Rig) -> None:
    rig.reads(doc())
    done = rig.run(expected=False, actor=None, yes=False, tty=HUMAN, stdin="n\n")
    assert done.code == 13 and done.error["message"] == "declined"
    assert_no_write(rig)


def test_the_dialog_says_when_there_are_no_checks(rig: Rig) -> None:
    rig.reads(doc(ci="none"))
    done = rig.run(expected=False, actor=None, yes=False, tty=HUMAN, stdin="n\n")
    assert "no checks on this head" in done.err


def test_a_head_that_moves_during_the_dialog_is_refused_on_the_re_read(rig: Rig) -> None:
    rig.reads(doc(), doc(head=OTHER))
    done = rig.run(expected=False, actor=None, yes=False, tty=HUMAN, stdin="y\n")
    assert done.code == 20 and done.error["reason"] == "stale_head"
    assert rig.puts() == []
    assert rig.audit() == []


def test_a_given_expected_head_wins_over_pinning(rig: Rig) -> None:
    rig.reads(doc(head=OTHER))
    argv = ["merge", PR, "--json", "--config", str(rig.config()), "--expected-head", HEAD,
            "--method", "squash"]  # fmt: skip
    rig.monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    assert main(argv, tty=HUMAN) == 20
    assert rig.puts() == []


# -- hooks -------------------------------------------------------------------------------------


def hook_config(name: str, cmd: str) -> str:
    return f"hooks:\n  {name}:\n    - cmd: {json.dumps(cmd)}\n"


def test_a_pre_merge_veto_stops_everything(rig: Rig) -> None:
    rig.reads(doc())
    done = rig.run(extra_config=hook_config("pre-merge", "echo no >&2; exit 3"))
    assert done.code == 11 and done.error["code"] == "vetoed"
    assert rig.puts() == [] and rig.audit() == []


def test_a_pre_merge_escalation_is_exit_13(rig: Rig) -> None:
    rig.reads(doc())
    done = rig.run(extra_config=hook_config("pre-merge", "exit 13"))
    assert done.code == 13 and done.error["code"] == "escalated"
    assert rig.puts() == [] and rig.audit() == []


def test_override_escalation_lets_a_human_through_and_is_audited(rig: Rig) -> None:
    rig.reads(doc(), doc(), merged())
    rig.put()
    done = rig.run(
        "--override-escalation",
        actor="human:me",
        extra_config=hook_config("pre-merge", "exit 13"),
    )
    assert done.code == 0, done.err
    assert rig.audit()[0]["override"] == ["override_escalation"]


def test_override_escalation_never_overrides_a_veto(rig: Rig) -> None:
    rig.reads(doc())
    done = rig.run(
        "--override-escalation", actor="human:me", extra_config=hook_config("pre-merge", "exit 1")
    )
    assert done.code == 11 and rig.puts() == []


def test_a_hook_gets_the_schema_payload_and_its_decision_ids_are_audited(
    rig: Rig, tmp_path: Path
) -> None:
    out = tmp_path / "payload.json"
    rig.reads(doc(), doc(), merged())
    rig.put()
    cmd = f'cat > {out}; echo \'{{"decision_id": "policy-1"}}\''
    done = rig.run("--decision-id", "d-1", extra_config=hook_config("pre-merge", cmd))
    assert done.code == 0, done.err
    payload = json.loads(out.read_text())
    schemas.validate_hook_payload("pre-merge", payload)
    assert payload["expected_head"] == HEAD and payload["expected_base"] == BASE
    assert payload["ci"]["state"] == "passing" and payload["method"] == "squash"
    assert payload["decision_ids"] == ["d-1"]
    assert rig.audit()[0]["decision_id"] == ["d-1", "policy-1"]


def test_a_retarget_during_a_slow_hook_refuses_at_step_7(rig: Rig) -> None:
    rig.reads(doc(), doc(base="release"))
    done = rig.run(extra_config=hook_config("pre-merge", "true"))
    assert done.code == 20 and done.error["reason"] == "stale_base"
    assert rig.puts() == [] and rig.audit() == []


def test_a_head_push_during_a_hook_refuses_at_step_7(rig: Rig) -> None:
    rig.reads(doc(), doc(head=OTHER))
    done = rig.run(extra_config=hook_config("pre-merge", "true"))
    assert done.code == 20 and done.error["reason"] == "stale_head"
    assert rig.puts() == []


def test_a_new_blocker_during_a_hook_refuses_even_under_override_blocks(rig: Rig) -> None:
    rig.reads(doc(ci="failing"), doc(ci="failing", merge_state="BEHIND"))
    done = rig.run(
        "--override-blocks", actor="human:me", extra_config=hook_config("pre-merge", "true")
    )
    assert done.code == 20 and done.error["reason"] == "not_ready"
    assert rig.puts() == []


def test_an_old_blocker_stays_overridden_after_the_hook(rig: Rig) -> None:
    rig.reads(doc(ci="failing"), doc(ci="failing"), merged())
    rig.put()
    done = rig.run(
        "--override-blocks", actor="human:me", extra_config=hook_config("pre-merge", "true")
    )
    assert done.code == 0, done.err


def test_a_pr_merged_during_the_hook_at_the_expected_head_is_success_without_a_write(
    rig: Rig,
) -> None:
    rig.reads(doc(), merged())
    done = rig.run(extra_config=hook_config("pre-merge", "true"))
    assert done.code == 0 and done.json["already_merged"] is True
    assert rig.puts() == [] and rig.audit() == []


def test_a_pr_merged_during_the_hook_at_another_head_is_refused(rig: Rig) -> None:
    rig.reads(doc(), merged(head=OTHER))
    done = rig.run(extra_config=hook_config("pre-merge", "true"))
    assert done.code == 20 and done.error["reason"] == "stale_head"


def test_post_merge_fires_after_a_verified_merge_with_the_schema_payload(
    rig: Rig, tmp_path: Path
) -> None:
    out = tmp_path / "post.json"
    rig.reads(doc(), merged())
    rig.put()
    done = rig.run(extra_config=hook_config("post-merge", f"cat > {out}"))
    assert done.code == 0, done.err
    payload = json.loads(out.read_text())
    schemas.validate_hook_payload("post-merge", payload)
    assert payload["result"] == "merged" and payload["observed"] is None


def test_post_merge_fires_on_a_failed_attempt(rig: Rig, tmp_path: Path) -> None:
    out = tmp_path / "post.json"
    rig.reads(doc())
    rig.put(409, {"message": "Head branch was modified. Review and try the merge again."})
    done = rig.run(extra_config=hook_config("post-merge", f"cat > {out}"))
    assert done.code == 20
    payload = json.loads(out.read_text())
    schemas.validate_hook_payload("post-merge", payload)
    assert payload["result"] == "failed"


def test_post_merge_does_not_fire_on_a_refusal_before_any_attempt(rig: Rig, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    rig.reads(doc(head=OTHER))
    rig.run(extra_config=hook_config("post-merge", f"touch {marker}"))
    assert not marker.exists()


def test_a_failing_post_merge_hook_never_changes_the_result(rig: Rig) -> None:
    rig.reads(doc(), merged())
    rig.put()
    done = rig.run(extra_config=hook_config("post-merge", "exit 9"))
    assert done.code == 0
    assert "post-merge" in done.err


# -- the audit file ----------------------------------------------------------------------------


def test_an_unwritable_audit_file_sends_nothing_and_reads_nothing(rig: Rig) -> None:
    state = Path(os.environ["XDG_STATE_HOME"]) / "forgectl"
    state.mkdir(parents=True, mode=0o700)
    (state / "audit.jsonl").symlink_to(rig.tmp / "elsewhere")
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "audit_unavailable"
    assert rig.calls() == []


def test_a_failed_attempted_append_sends_nothing(rig: Rig) -> None:
    def refuse(self: Any) -> None:
        from forgectl.errors import ForgectlError

        raise ForgectlError("audit_unavailable", "no")

    rig.reads(doc())
    rig.monkeypatch.setattr("forgectl.audit.Attempt.begin", refuse)
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "audit_unavailable"
    assert rig.puts() == []


def test_an_audit_failure_after_the_write_is_audit_failed_and_post_hooks_still_run(
    rig: Rig, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    rig.reads(doc(), merged())
    rig.put()

    def broken(self: Any, result: str, **kw: Any) -> None:
        from forgectl.errors import ForgectlError

        self._finished = True
        raise ForgectlError("audit_failed", "no", details={"outcome": result})

    rig.monkeypatch.setattr("forgectl.audit.Attempt.finish", broken)
    done = rig.run(extra_config=hook_config("post-merge", f"touch {marker}"))
    assert done.code == 12 and done.error["code"] == "audit_failed"
    assert marker.exists()


# -- answers to the write ----------------------------------------------------------------------


def test_a_409_is_stale_head_with_the_head_from_one_read(rig: Rig) -> None:
    rig.reads(doc(), doc(head=OTHER))
    rig.put(409, {"message": "Head branch was modified. Review and try the merge again."})
    done = rig.run()
    assert done.code == 20 and done.error["reason"] == "stale_head"
    assert done.error["details"] == {"head_sha": OTHER}
    assert results(rig.audit()) == ["attempted", "failed"]
    assert len(rig.puts()) == 1


def test_a_409_whose_read_fails_omits_the_head(rig: Rig) -> None:
    rig.reads(doc(), {"errors": [{"type": "NOT_FOUND", "message": "x"}], "data": None})
    rig.put(409, {"message": "Head branch was modified."})
    done = rig.run()
    assert done.code == 20 and done.error["reason"] == "stale_head"
    assert "details" not in done.error


def test_a_405_whose_re_read_shows_merged_at_the_expected_head_is_already_merged(
    rig: Rig, tmp_path: Path
) -> None:
    out = tmp_path / "post.json"
    rig.reads(doc(), merged())
    rig.put(405, {"message": "Pull Request is not mergeable"})
    done = rig.run(extra_config=hook_config("post-merge", f"cat > {out}"))
    assert done.code == 0, done.err
    assert done.json["already_merged"] is True and done.json["merged"] is True
    schemas.validate_cli_output("merge", done.json)
    assert results(rig.audit()) == ["attempted", "merged"]
    assert json.loads(out.read_text())["result"] == "merged"


def test_a_405_re_read_merged_elsewhere_is_merge_unverified(rig: Rig) -> None:
    rig.reads(doc(), merged(head=OTHER))
    rig.put(405, {"message": "Pull Request is not mergeable"})
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "merge_unverified"
    assert results(rig.audit()) == ["attempted", "merged_unexpected"]
    assert rig.audit()[1]["observed"]["head_sha"] == OTHER


def test_a_405_with_blockers_is_not_ready(rig: Rig) -> None:
    rig.reads(doc(), doc(merge_state="BEHIND"))
    rig.put(405, {"message": "Pull Request is not mergeable"})
    done = rig.run()
    assert done.code == 20 and done.error["reason"] == "not_ready"
    assert results(rig.audit()) == ["attempted", "failed"]


def test_a_405_naming_the_method_is_method_not_allowed(rig: Rig) -> None:
    rig.reads(doc(), doc())
    rig.put(405, {"message": "Squash merges are not allowed on this repository."})
    done = rig.run()
    assert done.code == 20 and done.error["reason"] == "method_not_allowed"
    assert done.error["details"] == {"allowed": ["squash"]}


def test_base_branch_was_modified_is_host_rejected_even_with_blockers(rig: Rig) -> None:
    rig.reads(doc(), doc(merge_state="BEHIND"))
    rig.put(405, {"message": "Base branch was modified. Review and try the merge again."})
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "host_rejected"
    assert done.error["details"]["status"] == 405
    assert results(rig.audit()) == ["attempted", "failed"]


def test_any_other_405_with_a_clean_re_read_is_host_rejected(rig: Rig) -> None:
    rig.reads(doc(), doc())
    rig.put(405, {"message": "something else"})
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "host_rejected"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, "permission_denied"),
        (404, "not_found"),
        (401, "auth_required"),
        (422, "host_rejected"),
    ],
)
def test_other_failure_answers_are_failed_and_never_retried(
    rig: Rig, status: int, code: str
) -> None:
    rig.reads(doc())
    rig.put(status, {"message": "nope"})
    done = rig.run()
    assert done.error["code"] == code
    assert results(rig.audit()) == ["attempted", "failed"]
    assert len(rig.puts()) == 1


def test_a_rate_limit_carries_retry_after_and_is_not_retried(rig: Rig) -> None:
    rig.reads(doc())
    rig.put(429, {"message": "slow down"}, headers={"retry-after": "30"})
    done = rig.run()
    assert done.error["code"] == "rate_limited" and done.error["retry_after"] == 30
    assert len(rig.puts()) == 1


def test_a_5xx_whose_re_read_shows_merged_is_success(rig: Rig) -> None:
    rig.reads(doc(), merged())
    rig.put(502, {"message": "bad gateway"})
    done = rig.run()
    assert done.code == 0, done.err
    assert results(rig.audit()) == ["attempted", "merged"]
    assert len(rig.puts()) == 1


def test_a_5xx_whose_pr_stays_open_is_outcome_unknown_after_the_window(rig: Rig) -> None:
    rig.reads(doc())
    rig.put(502, {"message": "bad gateway"})
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "outcome_unknown"
    assert results(rig.audit()) == ["attempted", "outcome_unknown"]
    assert rig.sleeps == [1.0, 2.0, 3.0, 4.0]
    assert len(rig.puts()) == 1


def test_a_timeout_whose_re_read_shows_merged_is_success(rig: Rig) -> None:
    rig.reads(doc(), merged())
    rig.put(200, MERGE_OK, hang=30)
    start = time.monotonic()
    done = rig.run("--timeout", "2")
    assert time.monotonic() - start < 25
    assert done.code == 0, done.err
    assert results(rig.audit()) == ["attempted", "merged"]


def test_a_failed_verification_read_is_outcome_unknown(rig: Rig) -> None:
    rig.forges.answer("gh", "graphql", method="POST", body=doc(), times=1)
    rig.forges.answer("gh", "graphql", method="POST", status=404, body={"message": "x"})
    rig.put(502, {"message": "x"})
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "outcome_unknown"
    assert results(rig.audit()) == ["attempted", "outcome_unknown"]


def test_a_200_with_a_pr_still_open_is_merge_unverified(rig: Rig) -> None:
    rig.reads(doc())
    rig.put()  # accepted, but nothing merged (a queue, or a lie)
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "merge_unverified"
    assert done.error["details"]["head_sha"] == HEAD
    assert results(rig.audit()) == ["attempted", "outcome_unknown"]


def test_a_200_with_a_closed_pr_is_merge_unverified(rig: Rig) -> None:
    rig.reads(doc(), doc(state="CLOSED"))
    rig.put()
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "merge_unverified"


def test_a_merge_that_lands_at_another_head_is_merged_unexpected(rig: Rig, tmp_path: Path) -> None:
    out = tmp_path / "post.json"
    rig.reads(doc(), merged(head=OTHER))
    rig.put()
    done = rig.run(extra_config=hook_config("post-merge", f"cat > {out}"))
    assert done.code == 12 and done.error["code"] == "merge_unverified"
    assert done.error["details"]["head_sha"] == OTHER
    audit = rig.audit()
    assert results(audit) == ["attempted", "merged_unexpected"]
    assert audit[1]["observed"] == {"head_sha": OTHER, "base_ref": BASE}
    payload = json.loads(out.read_text())
    schemas.validate_hook_payload("post-merge", payload)
    assert payload["result"] == "merged_unexpected"
    assert payload["observed"] == {"head_sha": OTHER, "base_ref": BASE}


def test_a_merge_that_lands_on_another_base_is_merged_unexpected(rig: Rig) -> None:
    rig.reads(doc(), merged(base="release"))
    rig.put()
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "merge_unverified"
    assert rig.audit()[1]["observed"]["base_ref"] == "release"


def test_a_redirected_write_is_answered_by_a_read_and_verification_decides(rig: Rig) -> None:
    # gh follows a 302 as a body-less GET and shows the GET's 200 PR object: nothing merged
    rig.reads(doc())
    rig.forges.answer(
        "gh", MERGE_ENDPOINT, method="PUT", redirect=f"/repos/{REPO}/pulls/7", status=302
    )
    rig.forges.answer("gh", f"repos/{REPO}/pulls/7", method="GET", body={"number": 7})
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "merge_unverified"
    assert results(rig.audit()) == ["attempted", "outcome_unknown"]


def test_a_307_is_host_rejected_and_nothing_landed(rig: Rig) -> None:
    rig.reads(doc())
    rig.forges.answer(
        "gh", MERGE_ENDPOINT, method="PUT", redirect=f"/repos/{REPO}/pulls/7", status=307
    )
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "host_rejected"
    assert results(rig.audit()) == ["attempted", "failed"]


def test_the_budget_running_out_before_the_write_leaves_no_attempted_record(rig: Rig) -> None:
    rig.reads(doc())
    original = merge_module.MergeRun._read

    def drain(self: Any, *a: Any, **k: Any) -> Any:
        result = original(self, *a, **k)
        self.env.gateway.budget.spent = self.env.gateway.budget.total
        return result

    rig.monkeypatch.setattr(merge_module.MergeRun, "_read", drain)
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "network_error"
    assert rig.puts() == [] and rig.audit() == []


# -- TOCTOU, hostile text, secrets -------------------------------------------------------------


def test_the_host_enforces_the_head_when_it_moves_between_read_and_write(rig: Rig) -> None:
    # the read said HEAD; by the write the head moved, so the host answers 409 and nothing merges
    rig.reads(doc(), doc(head=OTHER))
    rig.put(409, {"message": "Head branch was modified."})
    done = rig.run()
    assert done.code == 20
    assert json.loads(rig.puts()[0].stdin or "")["sha"] == HEAD  # never the new head


def test_a_hostile_host_message_is_one_marked_line_and_holds_no_token(rig: Rig) -> None:
    rig.reads(doc(), doc())
    rig.put(405, {"message": f"x\nforged: yes‮ {TOKEN}"})
    done = rig.run()
    assert TOKEN not in done.out + done.err
    assert "\n" not in done.error["message"]
    assert TOKEN not in json.dumps(rig.audit()) + json.dumps(rig.log())


def test_a_hostile_branch_name_cannot_forge_output_lines(rig: Rig) -> None:
    rig.reads(doc(base="a\nb‮"))
    done = rig.run()
    assert done.code == 20
    assert "‮" not in done.out and "‮" not in done.err


def test_no_token_reaches_any_output_on_success(rig: Rig) -> None:
    rig.reads(doc(), merged())
    rig.put()
    done = rig.run()
    assert TOKEN not in done.out + done.err
    assert TOKEN not in json.dumps(rig.audit()) + json.dumps(rig.log())


# -- unit: resolution --------------------------------------------------------------------------


def test_auto_is_not_implemented_yet(rig: Rig) -> None:
    done = rig.run("--auto", actor="human:me")
    assert done.code == 1
    assert rig.calls() == []


# -- signals (a real process) ------------------------------------------------------------------


def _wait_for_call(forges: FakeForges, count: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and len(forges.calls("gh")) < count:
        time.sleep(0.05)
    time.sleep(0.4)


@pytest.fixture
def signal_env(fake_forges: FakeForges, tmp_path: Path) -> Iterator[dict[str, str]]:
    cfg = tmp_path / "config.yaml"
    marker = tmp_path / "post-ran"
    cfg.write_text(
        "hosts:\n  github.com:\n    token_env: MERGE_TOKEN\n"
        f"hooks:\n  post-merge:\n    - cmd: {json.dumps(f'touch {marker}')}\n",
        encoding="utf-8",
    )
    env = forgectl_env(fake_forges, {"MERGE_TOKEN": TOKEN})
    env["_CFG"] = str(cfg)
    env["_MARK"] = str(marker)
    yield env


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_during_the_write_is_outcome_unknown_exit_12_and_no_post_hook(
    fake_forges: FakeForges, procs: ProcessRegistry, signal_env: dict[str, str], signum: int
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", body=doc())
    fake_forges.answer("gh", MERGE_ENDPOINT, method="PUT", hang=60)
    argv = [sys.executable, "-m", "forgectl", "merge", PR, "--json", "--config",
            signal_env["_CFG"], "--expected-head", HEAD, "--expected-base", BASE,
            "--method", "squash", "--actor", "auto:bot", "--yes"]  # fmt: skip
    proc = procs.popen(argv, env=signal_env, stdout=-1, stderr=-1, text=True)
    _wait_for_call(fake_forges, 2)
    proc.send_signal(signum)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 12, out
    body = json.loads(out)
    assert body["error"]["code"] == "outcome_unknown"
    audit_file = Path(signal_env["XDG_STATE_HOME"]) / "forgectl" / "audit.jsonl"
    records = [json.loads(x) for x in audit_file.read_text().splitlines()]
    assert [r["result"] for r in records] == ["attempted", "outcome_unknown"]
    assert records[0]["attempt_id"] == records[1]["attempt_id"]
    assert not Path(signal_env["_MARK"]).exists()


def test_a_signal_before_any_write_is_128_plus_n_with_no_audit(
    fake_forges: FakeForges, procs: ProcessRegistry, signal_env: dict[str, str]
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", hang=60)
    argv = [sys.executable, "-m", "forgectl", "merge", PR, "--json", "--config",
            signal_env["_CFG"], "--expected-head", HEAD, "--expected-base", BASE,
            "--method", "squash", "--actor", "auto:bot", "--yes"]  # fmt: skip
    proc = procs.popen(argv, env=signal_env, stdout=-1, stderr=-1, text=True)
    _wait_for_call(fake_forges, 1)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=30)
    assert proc.returncode == 143
    audit_file = Path(signal_env["XDG_STATE_HOME"]) / "forgectl" / "audit.jsonl"
    assert not audit_file.exists() or audit_file.read_text() == ""


# -- unit: method resolution and base matching -------------------------------------------------


def test_base_matches_rejects_a_marker_even_when_the_text_is_equal() -> None:
    from forgectl.merge import base_matches

    class B:
        ref = "a<U+000A>b"

    class P:
        base = B()

    assert base_matches(P(), "main") is False  # type: ignore[arg-type]
    assert base_matches(P(), "a<U+000A>b") is False  # type: ignore[arg-type]


def test_copy_of_recordings_is_independent() -> None:
    a, b = doc(), doc()
    a["data"]["repository"]["pullRequest"]["title"] = "x"
    assert b["data"]["repository"]["pullRequest"]["title"] != "x"
    assert copy.deepcopy(a) == a
