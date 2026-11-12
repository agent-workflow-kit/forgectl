"""`forgectl resolve` and `unresolve` on GitHub (#29): the thread lookup within the PR, the state
read, "already there", confirmation, hooks, the mutation proof, audit and the `changed` field.

The real CLI runs in process against the fake `gh`. Nothing here reaches a host. The GraphQL
answers are hand-built from GitHub's documented shapes; every assumed field name is UNVERIFIED
until the live suite (#34).
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from forgectl import resolve as resolve_module
from forgectl.cli import main
from forgectl.humanactions import Tty
from forgectl.reads import make_env

from .harness import golden, schemas
from .harness.safety import assert_call_safe
from .test_github_show import HEAD, TOKEN
from .test_post_github import (
    HUMAN,
    NO_TTY,
    PR,
    REPO,
    Result,
    Rig,
    hook_config,
)
from .test_post_github import rig as rig  # the fixture

THREAD = "gh:rt:PRRT_thread01"
NODE = "PRRT_thread01"
READ = r"node\(id"
RESOLVE = r"\{ resolveReviewThread"
UNRESOLVE = r"\{ unresolveReviewThread"


def thread_doc(
    *,
    resolved: bool = False,
    number: int = 7,
    repo: str = REPO,
    typename: str = "PullRequestReviewThread",
    author: str | None = "user-4",
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "__typename": typename,
        "isResolved": resolved,
        "pullRequest": {"number": number},
        "repository": {"nameWithOwner": repo},
        "comments": {"nodes": [{"author": None if author is None else {"login": author}}]},
    }
    return {"data": {"node": node}}


def no_sleep(inv: Any, **kw: Any) -> Any:
    return make_env(inv, sleep=lambda _seconds: None, **kw)


def answer_read(rig: Rig, doc: dict[str, Any], **kw: Any) -> None:
    rig.forges.answer("gh", "graphql", method="POST", stdin_re=READ, body=doc, **kw)


def answer_write(rig: Rig, want: bool, *, resolved: bool | None = None, **kw: Any) -> None:
    field = "resolveReviewThread" if want else "unresolveReviewThread"
    state = want if resolved is None else resolved
    rig.forges.answer(
        "gh",
        "graphql",
        method="POST",
        stdin_re=RESOLVE if want else UNRESOLVE,
        body={"data": {field: {"thread": {"id": NODE, "isResolved": state}}}},
        **kw,
    )


def run(
    rig: Rig,
    command: str = "resolve",
    *args: str,
    thread: str = THREAD,
    yes: bool = True,
    tty: Tty = NO_TTY,
    actor: str | None = "auto:bot",
    stdin: str = "",
    extra_config: str = "",
) -> Result:
    argv = [command, PR, "--json", "--config", str(rig.config(extra_config)), "--thread", thread]
    if actor:
        argv += ["--actor", actor]
    if yes:
        argv += ["--yes"]
    argv += list(args)
    rig.monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = main(argv, tty=tty)
    captured = rig.capsys.readouterr()
    return Result(code, captured.out, captured.err)


def writes(rig: Rig) -> list[dict[str, Any]]:
    """The mutations sent, as their request documents."""
    docs = [json.loads(c.stdin or "{}") for c in rig.calls() if c.stdin]
    return [d for d in docs if "mutation" in d.get("query", "")]


def assert_nothing_written(rig: Rig) -> None:
    assert writes(rig) == []
    assert rig.audit() == []


# -- the happy path ----------------------------------------------------------------------------


def test_resolve_sends_the_mutation_and_reports_changed(rig: Rig) -> None:
    answer_read(rig, thread_doc(), times=None)
    answer_write(rig, True)
    done = run(rig)
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("resolve", out)
    assert out == {
        "pr": PR,
        "action": "resolve",
        "thread_id": THREAD,
        "resolved": True,
        "changed": True,
        "dry_run": False,
    }
    golden.assert_golden("resolve/github-resolved", out, schema_id="forgectl/cli/resolve.json")
    (sent,) = writes(rig)
    assert "resolveReviewThread" in sent["query"] and sent["variables"] == {"id": NODE}
    for call in rig.calls():
        assert_call_safe(call, host="github.com")
        assert call.token_sent == TOKEN


def test_unresolve_sends_the_other_mutation(rig: Rig) -> None:
    answer_read(rig, thread_doc(resolved=True), times=None)
    answer_write(rig, False)
    done = run(rig, "unresolve")
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("unresolve", out)
    assert out["resolved"] is False and out["changed"] is True
    golden.assert_golden(
        "unresolve/github-unresolved", out, schema_id="forgectl/cli/unresolve.json"
    )
    (sent,) = writes(rig)
    assert "unresolveReviewThread" in sent["query"]


def test_the_audit_records_attempted_then_the_result_with_the_thread(rig: Rig) -> None:
    answer_read(rig, thread_doc(), times=None)
    answer_write(rig, True)
    run(rig)
    attempted, result = rig.audit()
    assert [attempted["result"], result["result"]] == ["attempted", "resolved"]
    assert attempted["action"] == "resolve" and attempted["target"] == PR
    assert attempted["inputs"]["thread_id"] == THREAD and attempted["inputs"]["body_sha256"] is None
    assert result["ids"] == []


def test_unresolve_audits_unresolved(rig: Rig) -> None:
    answer_read(rig, thread_doc(resolved=True), times=None)
    answer_write(rig, False)
    run(rig, "unresolve")
    assert [r["result"] for r in rig.audit()] == ["attempted", "unresolved"]


# -- already there -----------------------------------------------------------------------------


@pytest.mark.parametrize(("command", "state"), [("resolve", True), ("unresolve", False)])
def test_already_there_is_exit_0_with_no_call_hook_audit_or_confirmation(
    rig: Rig, tmp_path: Path, command: str, state: bool
) -> None:
    ran = tmp_path / "hook-ran"
    answer_read(rig, thread_doc(resolved=state), times=None)
    done = run(
        rig,
        command,
        yes=False,
        extra_config=hook_config("pre-post", f"touch {ran}")
        + hook_config("post-post", f"touch {ran}").replace("hooks:\n", ""),
    )
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output(command, out)
    assert out["resolved"] is state and out["changed"] is False
    assert not ran.exists()
    assert_nothing_written(rig)


def test_already_there_golden(rig: Rig) -> None:
    answer_read(rig, thread_doc(resolved=True), times=None)
    done = run(rig)
    golden.assert_golden("resolve/github-already", done.json, schema_id="forgectl/cli/resolve.json")


def test_a_thread_resolved_during_the_hook_is_changed_false_and_not_written(
    rig: Rig, tmp_path: Path
) -> None:
    ran = tmp_path / "post-ran"
    answer_read(rig, thread_doc(resolved=False), times=1)
    answer_read(rig, thread_doc(resolved=True), times=None)
    done = run(
        rig,
        extra_config=hook_config("pre-post", "exit 0")
        + "  post-post:\n    - cmd: "
        + json.dumps(f"touch {ran}")
        + "\n",
    )
    assert done.code == 0, done.err
    assert done.json["changed"] is False and done.json["resolved"] is True
    assert_nothing_written(rig)
    assert not ran.exists()  # no write was attempted: no post-post hook


# -- ids ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("thread", ["gh:ic:IC_x0000001", "gh:rc:PRRC_x0000001", "gh:r:PRR_x00001"])
def test_a_comment_or_review_id_is_not_resolvable_before_any_host_call(
    rig: Rig, thread: str
) -> None:
    done = run(rig, thread=thread)
    assert done.code == 20, done.out
    assert done.error["code"] == "refused" and done.error["reason"] == "not_resolvable"
    assert rig.calls() == [] and rig.audit() == []


def test_a_gitlab_note_id_is_not_resolvable(rig: Rig) -> None:
    done = run(rig, thread="gl:n:5")
    assert done.code == 20 and done.error["reason"] == "not_resolvable"
    assert rig.calls() == []


@pytest.mark.parametrize("thread", ["", "nope", "gh:rt:", "gh:rt:a b", "gh:zz:abc", "gl:d:xyz"])
def test_a_malformed_id_is_exit_2(rig: Rig, thread: str) -> None:
    done = run(rig, thread=thread)
    assert done.code == 2, done.out
    assert rig.calls() == []


def test_a_gitlab_discussion_id_on_a_github_pr_is_exit_10(rig: Rig) -> None:
    done = run(rig, thread="gl:d:" + "a" * 40)
    assert done.code == 10 and done.error["code"] == "not_found"
    assert_nothing_written(rig)


@pytest.mark.parametrize(
    "doc",
    [
        {"data": {"node": None}},
        thread_doc(number=8),
        thread_doc(repo="user-7/other"),
        thread_doc(typename="IssueComment"),
        thread_doc(typename="PullRequestReviewComment"),
        {"data": {"node": {"__typename": "PullRequestReviewThread", "isResolved": False}}},
    ],
)
def test_an_id_that_is_absent_or_from_another_pr_is_exit_10_before_any_write(
    rig: Rig, doc: dict[str, Any]
) -> None:
    answer_read(rig, doc, times=None)
    done = run(rig)
    assert done.code == 10 and done.error["code"] == "not_found"
    assert_nothing_written(rig)


def test_the_lookup_runs_before_dry_run_and_confirmation(rig: Rig) -> None:
    answer_read(rig, {"data": {"node": None}}, times=None)
    assert run(rig, "resolve", "--dry-run", yes=False).code == 10
    assert run(rig, yes=False).code == 10


def test_a_thread_with_no_state_is_upstream_error(rig: Rig) -> None:
    doc = thread_doc()
    del doc["data"]["node"]["isResolved"]
    answer_read(rig, doc, times=None)
    done = run(rig)
    assert done.code == 12 and done.error["code"] == "upstream_error"
    assert_nothing_written(rig)


# -- dry run and confirmation ------------------------------------------------------------------


def test_dry_run_reports_the_requested_state_and_writes_nothing(rig: Rig) -> None:
    answer_read(rig, thread_doc(), times=None)
    done = run(rig, "resolve", "--dry-run", yes=False)
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("resolve", out)
    assert out["dry_run"] is True and out["would_refuse"] is None
    assert out["resolved"] is True and out["changed"] is False
    golden.assert_golden("resolve/github-dry-run", out, schema_id="forgectl/cli/resolve.json")
    assert_nothing_written(rig)


def test_dry_run_of_a_refusal_is_would_refuse_with_exit_20(rig: Rig) -> None:
    done = run(rig, "resolve", "--dry-run", yes=False, thread="gh:ic:IC_x0000001")
    assert done.code == 20, done.out
    out = done.json
    schemas.validate_cli_output("resolve", out)
    assert out["would_refuse"]["reason"] == "not_resolvable"
    assert out["resolved"] is None and out["changed"] is False and out["pr"] == PR
    golden.assert_golden("resolve/github-would-refuse", out, schema_id="forgectl/cli/resolve.json")


def test_no_yes_and_no_tty_is_exit_13(rig: Rig) -> None:
    answer_read(rig, thread_doc(), times=None)
    done = run(rig, yes=False)
    assert done.code == 13 and done.error["code"] == "confirmation_required"
    assert_nothing_written(rig)


def test_a_declined_dialog_writes_nothing(rig: Rig) -> None:
    answer_read(rig, thread_doc(), times=None)
    done = run(rig, yes=False, tty=HUMAN, actor="human:me", stdin="n\n")
    assert done.code == 13
    assert "Proceed?" in done.err
    assert_nothing_written(rig)


def test_an_accepted_dialog_writes_after_a_reread(rig: Rig) -> None:
    answer_read(rig, thread_doc(), times=None)
    answer_write(rig, True)
    done = run(rig, yes=False, tty=HUMAN, actor="human:me", stdin="y\n")
    assert done.code == 0, done.err
    reads = [c for c in rig.calls() if "node(id" in (c.stdin or "")]
    assert len(reads) == 2


# -- hooks -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("author", "mine"), [("user-4", True), ("User-4", True), ("other", False)])
def test_the_pre_post_hook_carries_the_thread_and_whether_it_is_mine(
    rig: Rig, tmp_path: Path, author: str, mine: bool
) -> None:
    out = tmp_path / "payload.json"
    answer_read(rig, thread_doc(author=author), times=None)
    answer_write(rig, True)
    done = run(rig, extra_config=hook_config("pre-post", f"cat > {out}"))
    assert done.code == 0, done.err
    payload = json.loads(out.read_text())
    schemas.validate_hook_payload("pre-post", payload)
    assert payload["action"] == "resolve" and payload["head_sha"] == HEAD
    assert payload["thread_id"] == THREAD and payload["thread_author_is_self"] is mine
    assert payload["body_sha256"] is None and payload["reply_to"] is None


def test_a_thread_whose_author_is_unknown_is_null(rig: Rig, tmp_path: Path) -> None:
    out = tmp_path / "payload.json"
    answer_read(rig, thread_doc(author=None), times=None)
    answer_write(rig, True)
    assert run(rig, extra_config=hook_config("pre-post", f"cat > {out}")).code == 0
    assert json.loads(out.read_text())["thread_author_is_self"] is None


def test_a_pre_post_veto_stops_the_write(rig: Rig) -> None:
    answer_read(rig, thread_doc(), times=None)
    done = run(rig, extra_config=hook_config("pre-post", "echo no >&2; exit 3"))
    assert done.code == 11 and done.error["code"] == "vetoed"
    assert_nothing_written(rig)


def test_a_pre_post_escalation_is_exit_13_and_an_override_is_audited(rig: Rig) -> None:
    answer_read(rig, thread_doc(), times=None)
    done = run(rig, extra_config=hook_config("pre-post", "exit 13"))
    assert done.code == 13 and writes(rig) == []
    answer_write(rig, True)
    done = run(rig, "resolve", "--override-escalation", actor="human:me",
               extra_config=hook_config("pre-post", "exit 13"))  # fmt: skip
    assert done.code == 0, done.err
    assert rig.audit()[0]["override"] == ["override_escalation"]


def test_post_post_fires_with_the_result_and_no_ids(rig: Rig, tmp_path: Path) -> None:
    out = tmp_path / "post.json"
    answer_read(rig, thread_doc(), times=None)
    answer_write(rig, True)
    done = run(rig, extra_config=hook_config("post-post", f"cat > {out}"))
    assert done.code == 0, done.err
    payload = json.loads(out.read_text())
    schemas.validate_hook_payload("post-post", payload)
    assert payload["result"] == "resolved" and payload["ids"] == []
    assert payload["thread_id"] == THREAD


def test_unresolve_post_post_result(rig: Rig, tmp_path: Path) -> None:
    out = tmp_path / "post.json"
    answer_read(rig, thread_doc(resolved=True), times=None)
    answer_write(rig, False)
    run(rig, "unresolve", extra_config=hook_config("post-post", f"cat > {out}"))
    assert json.loads(out.read_text())["result"] == "unresolved"


def test_post_post_fires_with_the_failed_result(rig: Rig, tmp_path: Path) -> None:
    out = tmp_path / "post.json"
    answer_read(rig, thread_doc(), times=None)
    rig.forges.answer("gh", "graphql", method="POST", stdin_re=RESOLVE, status=403, body={})
    done = run(rig, extra_config=hook_config("post-post", f"cat > {out}"))
    assert done.code == 12
    payload = json.loads(out.read_text())
    schemas.validate_hook_payload("post-post", payload)
    assert payload["result"] == "failed"


def test_a_failing_post_post_hook_never_changes_the_result(rig: Rig) -> None:
    answer_read(rig, thread_doc(), times=None)
    answer_write(rig, True)
    done = run(rig, extra_config=hook_config("post-post", "exit 9"))
    assert done.code == 0 and "warning" in done.err


# -- the write's proof -------------------------------------------------------------------------


def test_an_answer_with_the_wrong_state_is_outcome_unknown(rig: Rig) -> None:
    answer_read(rig, thread_doc(), times=None)
    answer_write(rig, True, resolved=False)
    done = run(rig)
    assert done.code == 12 and done.error["code"] == "outcome_unknown"
    assert [r["result"] for r in rig.audit()] == ["attempted", "outcome_unknown"]


def test_an_answer_for_another_thread_is_outcome_unknown(rig: Rig) -> None:
    answer_read(rig, thread_doc(), times=None)
    rig.forges.answer(
        "gh",
        "graphql",
        method="POST",
        stdin_re=RESOLVE,
        body={
            "data": {"resolveReviewThread": {"thread": {"id": "PRRT_other", "isResolved": True}}}
        },
    )
    done = run(rig)
    assert done.code == 12 and done.error["code"] == "outcome_unknown"


@pytest.mark.parametrize(
    "body",
    [
        {"data": {"resolveReviewThread": None}},
        {"data": {"resolveReviewThread": {"thread": None}}},
        {"data": {"resolveReviewThread": {}}},
        {"data": {}},
    ],
)
def test_a_null_or_empty_mutation_result_is_outcome_unknown(rig: Rig, body: dict[str, Any]) -> None:
    answer_read(rig, thread_doc(), times=None)
    rig.forges.answer("gh", "graphql", method="POST", stdin_re=RESOLVE, body=body)
    done = run(rig)
    assert done.code == 12 and done.error["code"] == "outcome_unknown"
    assert [r["result"] for r in rig.audit()] == ["attempted", "outcome_unknown"]


@pytest.mark.parametrize(
    ("status", "code", "result"),
    [
        (403, "permission_denied", "failed"),
        (401, "auth_required", "failed"),
        (422, "host_rejected", "failed"),
        (429, "rate_limited", "failed"),
        (500, "outcome_unknown", "outcome_unknown"),
        (202, "outcome_unknown", "outcome_unknown"),
    ],
)
def test_the_write_table_maps_each_answer(rig: Rig, status: int, code: str, result: str) -> None:
    answer_read(rig, thread_doc(), times=None)
    rig.forges.answer(
        "gh", "graphql", method="POST", stdin_re=RESOLVE, status=status, body={"message": "x"}
    )
    done = run(rig)
    assert done.code == 12, done.out
    assert done.error["code"] == code
    assert [r["result"] for r in rig.audit()] == ["attempted", result]


def test_a_graphql_error_in_the_answer_is_not_resolved(rig: Rig) -> None:
    answer_read(rig, thread_doc(), times=None)
    rig.forges.answer(
        "gh",
        "graphql",
        method="POST",
        stdin_re=RESOLVE,
        body={"data": None, "errors": [{"type": "FORBIDDEN", "message": "no"}]},
    )
    done = run(rig)
    assert done.code == 12 and done.error["code"] == "permission_denied"
    assert [r["result"] for r in rig.audit()] == ["attempted", "failed"]


def test_a_write_is_never_retried(rig: Rig) -> None:
    answer_read(rig, thread_doc(), times=None)
    rig.forges.answer("gh", "graphql", method="POST", stdin_re=RESOLVE, status=500, times=None)
    run(rig)
    assert len(writes(rig)) == 1


def test_a_failed_audit_file_stops_before_any_call(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = Path(rig.tmp) / "state-file"
    state.write_text("x")
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    done = run(rig)
    assert done.code == 12 and done.error["code"] == "audit_unavailable"
    assert writes(rig) == []


def test_a_persistent_503_on_the_thread_read_makes_retries_plus_one_requests(rig: Rig) -> None:
    rig.monkeypatch.setattr(resolve_module, "make_env", no_sleep)
    answer_read(rig, {}, status=503, times=None)
    done = run(rig, extra_config="retries: 1\n")
    assert done.code == 12, done.err
    assert len([c for c in rig.calls() if c.stdin and "node(id" in c.stdin]) == 2


def test_a_persistent_503_on_the_reread_makes_retries_plus_one_requests(rig: Rig) -> None:
    rig.monkeypatch.setattr(resolve_module, "make_env", no_sleep)
    answer_read(rig, thread_doc(), times=1)
    answer_read(rig, {}, status=503, times=None)
    done = run(rig, extra_config="retries: 1\n" + hook_config("pre-post", "exit 0"))
    assert done.code == 12, done.err
    assert len([c for c in rig.calls() if c.stdin and "node(id" in c.stdin]) == 1 + 2
