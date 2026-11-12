"""`forgectl resolve` and `unresolve` on GitLab (#29): the discussion read within the MR, the PUT
and its proof, "already there", hooks, audit and the `changed` field.

The real CLI runs in process against the fake `glab`. Every answer is hand-built from GitLab's
documented REST shapes and is UNVERIFIED against a real host until the live suite (#34).
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from forgectl.cli import main

from .gitlab_support import TOKEN
from .harness import golden, schemas
from .harness.safety import assert_call_safe
from .test_post_gitlab import (
    DISC,
    DISCUSSIONS,
    ME,
    NO_TTY,
    OTHER_DISC,
    PR,
    Result,
    Rig,
    get,
    note,
)
from .test_post_gitlab import rig as rig  # the fixture

THREAD = f"gl:d:{DISC}"
URL = f"{DISCUSSIONS}/{DISC}"


def disc(
    resolved: bool = False,
    *,
    ident: str = DISC,
    individual: bool = False,
    resolvable: bool = True,
    author: int = ME,
    notes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    items = notes or [
        note(5, "a", author, resolvable=resolvable, resolved=resolved and resolvable),
        note(6, "b", 77, resolvable=resolvable, resolved=resolved and resolvable),
    ]
    return {"id": ident, "individual_note": individual, "notes": items}


def put(rig: Rig, body: Any, status: int = 200, **kw: Any) -> None:
    rig.forges.answer("glab", URL, method="PUT", status=status, body=body, **kw)


def run(
    rig: Rig, command: str = "resolve", *args: str, thread: str = THREAD, yes: bool = True,
    extra_config: str = "",
) -> Result:  # fmt: skip
    config = rig.tmp / "config.yaml"
    config.write_text(rig.config.read_text("utf-8") + extra_config, "utf-8")
    argv = [command, PR, "--json", "--config", str(config), "--thread", thread]
    argv += ["--actor", "auto:bot"]
    if yes:
        argv += ["--yes"]
    argv += list(args)
    rig.monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code = main(argv, tty=NO_TTY)
    captured = rig.capsys.readouterr()
    return Result(code, captured.out, captured.err)


def puts(rig: Rig) -> list[Any]:
    return [c for c in rig.calls() if "PUT" in c.argv]


def hook(name: str, cmd: str) -> str:
    return f"hooks:\n  {name}:\n    - cmd: {json.dumps(cmd)}\n"


def nothing_written(rig: Rig) -> None:
    assert puts(rig) == [] and rig.audit() == []


def test_resolve_puts_resolved_true_and_reports_changed(rig: Rig) -> None:
    get(rig.forges, URL, disc(False))
    put(rig, disc(True))
    done = run(rig)
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("resolve", out)
    golden.assert_golden("resolve/gitlab-resolved", out, schema_id="forgectl/cli/resolve.json")
    assert out["changed"] is True and out["resolved"] is True and out["thread_id"] == THREAD
    (call,) = puts(rig)
    assert call.argv[-1] == URL and json.loads(call.stdin or "") == {"resolved": True}
    for seen in rig.calls():
        assert_call_safe(seen, host="gitlab.com", mode="token")
    assert [r["result"] for r in rig.audit()] == ["attempted", "resolved"]
    assert rig.audit()[0]["inputs"]["thread_id"] == THREAD


def test_unresolve_puts_resolved_false(rig: Rig) -> None:
    get(rig.forges, URL, disc(True))
    put(rig, disc(False))
    done = run(rig, "unresolve")
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("unresolve", out)
    golden.assert_golden(
        "unresolve/gitlab-unresolved", out, schema_id="forgectl/cli/unresolve.json"
    )
    assert json.loads(puts(rig)[0].stdin or "") == {"resolved": False}
    assert [r["result"] for r in rig.audit()] == ["attempted", "unresolved"]


@pytest.mark.parametrize(("command", "state"), [("resolve", True), ("unresolve", False)])
def test_already_there_is_exit_0_with_no_write_hook_or_audit(
    rig: Rig, command: str, state: bool
) -> None:
    ran = rig.tmp / "ran"
    get(rig.forges, URL, disc(state), times=None)
    done = run(rig, command, yes=False, extra_config=hook("pre-post", f"touch {ran}"))
    assert done.code == 0, done.err
    schemas.validate_cli_output(command, done.json)
    assert done.json["changed"] is False and done.json["resolved"] is state
    assert not ran.exists()
    nothing_written(rig)


def test_a_discussion_is_resolved_only_when_every_resolvable_note_is(rig: Rig) -> None:
    half = disc(notes=[note(5, "a", resolvable=True, resolved=True),
                       note(6, "b", resolvable=True, resolved=False)])  # fmt: skip
    get(rig.forges, URL, half, times=None)
    put(rig, disc(True))
    assert run(rig).json["changed"] is True  # not already resolved: one note is open


def test_a_discussion_of_system_notes_is_not_resolvable(rig: Rig) -> None:
    get(rig.forges, URL, disc(resolvable=False), times=None)
    done = run(rig)
    assert done.code == 20 and done.error["reason"] == "not_resolvable"
    nothing_written(rig)


def test_an_individual_note_is_not_resolvable(rig: Rig) -> None:
    get(rig.forges, URL, disc(individual=True), times=None)
    done = run(rig, "unresolve")
    assert done.code == 20 and done.error["reason"] == "not_resolvable"
    nothing_written(rig)


def test_a_dry_run_of_a_not_resolvable_thread_is_would_refuse(rig: Rig) -> None:
    get(rig.forges, URL, disc(individual=True), times=None)
    done = run(rig, "resolve", "--dry-run", yes=False)
    assert done.code == 20
    schemas.validate_cli_output("resolve", done.json)
    assert done.json["would_refuse"]["reason"] == "not_resolvable"


def test_dry_run_writes_nothing(rig: Rig) -> None:
    get(rig.forges, URL, disc(False), times=None)
    done = run(rig, "resolve", "--dry-run", yes=False)
    assert done.code == 0, done.err
    schemas.validate_cli_output("resolve", done.json)
    assert done.json["would_refuse"] is None and done.json["changed"] is False
    nothing_written(rig)


def test_no_yes_and_no_tty_is_exit_13(rig: Rig) -> None:
    get(rig.forges, URL, disc(False), times=None)
    assert run(rig, yes=False).code == 13
    nothing_written(rig)


@pytest.mark.parametrize("thread", ["gl:n:5", "gh:ic:IC_x0000001", "gh:rc:PRRC_x0000001"])
def test_other_well_formed_ids_are_not_resolvable_before_any_host_call(
    rig: Rig, thread: str
) -> None:
    done = run(rig, thread=thread)
    assert done.code == 20 and done.error["reason"] == "not_resolvable"
    assert rig.calls() == []


def test_a_github_thread_id_on_a_gitlab_mr_is_exit_10(rig: Rig) -> None:
    done = run(rig, thread="gh:rt:PRRT_thread01")
    assert done.code == 10 and done.error["code"] == "not_found"
    nothing_written(rig)


def test_a_discussion_of_another_mr_is_exit_10_before_any_write(rig: Rig) -> None:
    get(rig.forges, URL, {"message": "404 Not found"}, status=404, times=None)
    done = run(rig)
    assert done.code == 10 and done.error["code"] == "not_found"
    nothing_written(rig)


def test_an_answer_for_another_discussion_is_upstream_error(rig: Rig) -> None:
    get(rig.forges, URL, disc(ident=OTHER_DISC), times=None)
    done = run(rig)
    assert done.code == 12 and done.error["code"] == "upstream_error"
    nothing_written(rig)


def test_the_hooks_carry_the_thread_and_whether_it_is_mine(rig: Rig) -> None:
    pre, post = rig.tmp / "pre.json", rig.tmp / "post.json"
    get(rig.forges, URL, disc(False), times=None)
    put(rig, disc(True))
    config = (
        hook("pre-post", f"cat > {pre}")
        + "  post-post:\n    - cmd: "
        + json.dumps(f"cat > {post}")
        + "\n"
    )
    done = run(rig, extra_config=config)
    assert done.code == 0, done.err
    before = json.loads(pre.read_text())
    schemas.validate_hook_payload("pre-post", before)
    assert before["thread_id"] == THREAD and before["thread_author_is_self"] is True
    after = json.loads(post.read_text())
    schemas.validate_hook_payload("post-post", after)
    assert after["result"] == "resolved" and after["ids"] == []


def test_a_thread_started_by_someone_else_is_not_mine(rig: Rig) -> None:
    pre = rig.tmp / "pre.json"
    get(rig.forges, URL, disc(False, author=ME + 1), times=None)
    put(rig, disc(True))
    run(rig, extra_config=hook("pre-post", f"cat > {pre}"))
    assert json.loads(pre.read_text())["thread_author_is_self"] is False


def test_a_veto_stops_the_write(rig: Rig) -> None:
    get(rig.forges, URL, disc(False), times=None)
    done = run(rig, extra_config=hook("pre-post", "echo no >&2; exit 3"))
    assert done.code == 11
    nothing_written(rig)


def test_a_thread_resolved_during_the_hook_is_changed_false(rig: Rig) -> None:
    get(rig.forges, URL, disc(False), times=1)
    get(rig.forges, URL, disc(True), times=None)
    done = run(rig, extra_config=hook("pre-post", "exit 0"))
    assert done.code == 0 and done.json["changed"] is False
    nothing_written(rig)


@pytest.mark.parametrize(
    "answer",
    [
        disc(False),  # the old state: a redirected write or a no-op
        {"id": OTHER_DISC, "notes": disc(True)["notes"]},
        {"id": DISC},
        {"id": DISC, "notes": []},
        {"id": DISC, "notes": ["x"]},
        disc(True, individual=True),
    ],
)
def test_a_200_without_the_requested_state_is_outcome_unknown(rig: Rig, answer: Any) -> None:
    get(rig.forges, URL, disc(False), times=None)
    put(rig, answer)
    done = run(rig)
    assert done.code == 12 and done.error["code"] == "outcome_unknown"
    assert [r["result"] for r in rig.audit()] == ["attempted", "outcome_unknown"]


def test_a_redirected_put_that_answers_a_list_is_never_resolved(rig: Rig) -> None:
    get(rig.forges, URL, disc(False), times=None)
    put(rig, [])
    done = run(rig)
    assert done.code == 12 and done.error["code"] == "host_rejected"
    assert "resolved" not in [r["result"] for r in rig.audit()]


@pytest.mark.parametrize(
    ("status", "code", "result"),
    [
        (403, "permission_denied", "failed"),
        (401, "auth_required", "failed"),
        (400, "host_rejected", "failed"),
        (429, "rate_limited", "failed"),
        (500, "outcome_unknown", "outcome_unknown"),
        (201, "outcome_unknown", "outcome_unknown"),
        (204, "outcome_unknown", "outcome_unknown"),
    ],
)
def test_the_write_table_maps_each_answer(rig: Rig, status: int, code: str, result: str) -> None:
    get(rig.forges, URL, disc(False), times=None)
    put(rig, disc(True), status=status)
    done = run(rig)
    assert done.code == 12 and done.error["code"] == code, done.out
    assert [r["result"] for r in rig.audit()] == ["attempted", result]


def test_a_write_is_never_retried(rig: Rig) -> None:
    get(rig.forges, URL, disc(False), times=None)
    put(rig, {}, status=500, times=None)
    run(rig)
    assert len(puts(rig)) == 1


def test_the_token_is_never_in_the_output(rig: Rig) -> None:
    get(rig.forges, URL, disc(False), times=None)
    put(rig, disc(True))
    done = run(rig)
    assert TOKEN not in done.out + done.err
    assert Path(rig.tmp).exists()
