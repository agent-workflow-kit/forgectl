"""`forgectl approve` on GitLab (#28): the note, the approval with `sha`, `user_has_approved`,
the 401 re-read, exact `head_verified` and the self-approval check.

The real CLI runs in process against the fake `glab`; nothing reaches a host. Every answer is
hand-built from the documented shapes and stays UNVERIFIED until the live suite (#34).
"""

from __future__ import annotations

import io
import json
import sys
from typing import Any

import pytest

from forgectl.cli import main

from .gitlab_support import HEAD as GL_HEAD
from .gitlab_support import mr_path
from .gitlab_support import recording as gl_recording
from .harness import schemas
from .test_post_gitlab import (
    NO_TTY,
    Result,
    Rig,
    get,
    rig,  # noqa: F401  (the fixture)
    stored,
)
from .test_post_gitlab import PR as GL_PR
from .test_review_gitlab import (
    APPROVE,
    DRAFTS,
    MARKER,
    MR,
    NOTES,
    Gl,
    mine,
)

OLD = "2026-10-01T08:00:00Z"


@pytest.fixture
def gl(rig: Rig) -> Gl:  # noqa: F811
    return Gl(rig)


def run_gl(gl_: Gl, *extra: str, body: str | None = None) -> Result:
    argv = ["approve", GL_PR, "--json", "--config", str(gl_.base.config)]
    argv += ["--expected-head", GL_HEAD, "--expected-base", "main", "--actor", "auto:bot", "--yes"]
    if body is not None:
        path = gl_.base.tmp / "approve-body.md"
        path.write_text(body, "utf-8")
        argv += ["--body-file", str(path)]
    argv += list(extra)
    gl_.base.monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code = main(argv, tty=NO_TTY)
    captured = gl_.base.capsys.readouterr()
    return Result(code, captured.out, captured.err)


# -- GitLab -------------------------------------------------------------------------------------


def note_ok(gl_: Gl, text: str, **extra: Any) -> None:
    gl_.base.post(NOTES, body=stored(text, **extra))


def approvals_doc(*, has: bool, approved_by: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    doc = gl_recording("approvals")
    doc["user_has_approved"] = has
    if approved_by is not None:
        doc["approved_by"] = approved_by
    return doc


def set_approvals(gl_: Gl, doc: dict[str, Any], **kw: Any) -> None:
    get(gl_.forges, f"{MR}/approvals", doc, **kw)
    _front(gl_)


def _front(gl_: Gl) -> None:
    rules = gl_.forges._rules
    rules.insert(0, rules.pop())
    gl_.forges._write_script()


def i_approved(at: str) -> dict[str, Any]:
    user = {"id": 31, "username": "user-4", "name": "User 4", "state": "active"}
    return {"user": {**user, "web_url": "https://gitlab.com/user-4"}, "approved_at": at}


def test_a_bodyless_gitlab_approve_sends_only_the_approval_with_the_expected_head(gl: Gl) -> None:
    gl.approve_ok()
    done = run_gl(gl)
    assert done.code == 0, done.err
    assert gl.endpoints("POST") == [APPROVE] and DRAFTS not in gl.endpoints("GET")
    approve = next(c for c in gl.writes() if c.argv[-1] == APPROVE)
    assert json.loads(approve.stdin or "") == {"sha": GL_HEAD}
    out = done.json
    assert out["action"] == "approve" and out["head_verified"] is None
    schemas.validate_cli_output("approve", out)
    assert [r["result"] for r in gl.audit()] == ["attempted", "approved"]
    assert gl.audit()[0]["action"] == "approve"


def test_a_gitlab_approve_body_is_a_note_then_the_approval_never_a_draft(gl: Gl) -> None:
    note_ok(gl, "looks good\n")
    gl.approve_ok()
    done = run_gl(gl, body="looks good\n")
    assert done.code == 0, done.err
    posts = gl.endpoints("POST")
    assert posts == [NOTES, APPROVE]
    assert not any("draft_notes" in e for _, e in gl.seen())
    assert done.json["ids"] == ["gl:n:8001"] and done.json["review_id"] is None
    schemas.validate_cli_output("approve", done.json)


def test_user_has_approved_means_already_approved_and_nothing_is_written(gl: Gl) -> None:
    set_approvals(gl, approvals_doc(has=True))
    done = run_gl(gl)
    assert done.code == 0, done.err
    assert done.json["already_posted"] is True and done.json["head_verified"] is False
    assert gl.writes() == [] and gl.audit() == []


def test_an_already_approved_gitlab_pr_still_gets_the_note_but_no_approval(gl: Gl) -> None:
    set_approvals(gl, approvals_doc(has=True))
    note_ok(gl, "again\n")
    done = run_gl(gl, body="again\n")
    assert done.code == 0, done.err
    assert gl.endpoints("POST") == [NOTES]
    assert done.json["already_posted"] is False and done.json["head_verified"] is False


def reset_on_push(gl_: Gl, value: bool) -> None:
    me_routes(gl_)
    doc = gl_recording("project_approvals")
    doc["reset_approvals_on_push"] = value
    get(gl_.forges, "projects/101/approvals", doc)
    _front(gl_)


def test_head_verified_is_true_with_reset_on_push_and_no_newer_retarget(gl: Gl) -> None:
    reset_on_push(gl, True)
    set_approvals(gl, approvals_doc(has=True, approved_by=[i_approved(OLD)]))
    done = run_gl(gl)
    assert done.code == 0, done.err
    assert done.json["already_posted"] is True and done.json["head_verified"] is True


def retarget_note(at: str) -> dict[str, Any]:
    return {
        "id": 400,
        "body": "changed target branch from `a` to `main`",
        "system": True,
        "created_at": at,
        "author": {"id": 77},
    }


def test_a_retarget_after_the_approval_makes_head_verified_false(gl: Gl) -> None:
    reset_on_push(gl, True)
    set_approvals(gl, approvals_doc(has=True, approved_by=[i_approved(OLD)]))
    gl.notes([retarget_note("2026-10-02T09:00:00Z")])
    done = run_gl(gl)
    assert done.code == 0, done.err
    assert done.json["already_posted"] is True and done.json["head_verified"] is False


def test_a_retarget_before_the_approval_keeps_head_verified_true(gl: Gl) -> None:
    reset_on_push(gl, True)
    set_approvals(gl, approvals_doc(has=True, approved_by=[i_approved("2026-10-03T09:00:00Z")]))
    gl.notes([retarget_note("2026-10-02T09:00:00Z")])
    done = run_gl(gl)
    assert done.code == 0, done.err
    assert done.json["head_verified"] is True


def test_head_verified_is_false_when_reset_on_push_is_off(gl: Gl) -> None:
    reset_on_push(gl, False)
    set_approvals(gl, approvals_doc(has=True, approved_by=[i_approved(OLD)]))
    done = run_gl(gl)
    assert done.json["head_verified"] is False


def test_a_missing_user_has_approved_fails_closed_before_any_write(gl: Gl) -> None:
    doc = approvals_doc(has=False)
    del doc["user_has_approved"]
    set_approvals(gl, doc)
    done = run_gl(gl, body="x\n")
    assert done.code == 12 and done.error["code"] == "upstream_error"
    assert gl.writes() == [] and gl.audit() == []


def test_a_401_on_approve_with_the_approval_in_place_is_success(gl: Gl) -> None:
    set_approvals(
        gl, approvals_doc(has=True), nth=3
    )  # reads: the PR's, the state step's, then ours
    gl.forges.answer(
        "glab", APPROVE, method="POST", status=401, body={"message": "401 Unauthorized"}
    )
    done = run_gl(gl)
    assert done.code == 0, done.err
    assert [r["result"] for r in gl.audit()] == ["attempted", "approved"]


def test_a_401_on_approve_without_the_approval_is_auth_required(gl: Gl) -> None:
    gl.forges.answer(
        "glab", APPROVE, method="POST", status=401, body={"message": "401 Unauthorized"}
    )
    done = run_gl(gl)
    assert done.code == 12 and done.error["code"] == "auth_required"
    assert [r["result"] for r in gl.audit()] == ["attempted", "failed"]


def test_a_failed_approve_after_the_note_is_outcome_partial(gl: Gl) -> None:
    note_ok(gl, "words\n")
    gl.forges.answer("glab", APPROVE, method="POST", status=500, body={"message": "boom"})
    done = run_gl(gl, body="words\n")
    assert done.code == 12 and done.error["code"] == "outcome_partial"
    assert done.error["details"] == {
        "landed": {"ids": ["gl:n:8001"], "thread_ids": []},
        "failed": "approve",
    }
    assert [r["result"] for r in gl.audit()] == ["attempted", "outcome_partial"]
    assert gl.audit()[-1]["ids"] == ["gl:n:8001"]


def test_a_409_on_approve_is_stale_head(gl: Gl) -> None:
    gl.forges.answer("glab", APPROVE, method="POST", status=409, body={"message": "SHA differs"})
    done = run_gl(gl)
    assert done.code == 20 and done.error["reason"] == "stale_head"


def test_a_note_that_ran_a_quick_action_stops_before_the_approval(gl: Gl) -> None:
    gl.base.post(NOTES, status=202, body={"id": 8002})
    done = run_gl(gl, body="/approve\n")
    assert done.code == 12 and done.error["code"] == "quick_action_ran"
    assert gl.endpoints("POST") == [NOTES]
    assert [r["result"] for r in gl.audit()] == ["attempted", "quick_action_ran"]


def test_a_gitlab_hit_with_the_approval_missing_only_approves(gl: Gl) -> None:
    gl.notes([mine(700, "words\n" + MARKER)])
    gl.approve_ok()
    done = run_gl(gl, "--dedupe-key", "key-1", body="words\n")
    assert done.code == 0, done.err
    assert done.json["already_posted"] is True and done.json["follow_up_done"] is True
    assert gl.endpoints("POST") == [APPROVE]


def test_a_gitlab_hit_with_the_approval_in_place_writes_nothing(gl: Gl) -> None:
    set_approvals(gl, approvals_doc(has=True))
    gl.notes([mine(700, "words\n" + MARKER)])
    done = run_gl(gl, "--dedupe-key", "key-1", body="words\n")
    assert done.code == 0, done.err
    assert done.json["already_posted"] is True and gl.writes() == []


def me_routes(gl_: Gl) -> None:
    """The user and member reads for this account (id 31), when it is an author or an approver."""
    user = {"bot": False, "id": 31, "name": "User 4", "state": "active", "username": "user-4"}
    get(gl_.forges, "users/31", {**user, "web_url": "https://gitlab.com/user-4"})
    member = {"access_level": 30, "expires_at": None, **user}
    get(gl_.forges, "projects/101/members/all/31", {**member, "web_url": "https://gitlab.com/x"})


def set_mr_author(gl_: Gl, user_id: int, username: str) -> None:
    doc = gl_recording("mr")
    doc["author"] = {**doc["author"], "id": user_id, "username": username}
    me_routes(gl_)
    gl_.forges.answer("glab", mr_path(), method="GET", body=doc)
    _front(gl_)


def test_approving_your_own_merge_request_is_self_approval(gl: Gl) -> None:
    set_mr_author(gl, 31, "user-4")
    done = run_gl(gl, body="x\n")
    assert done.code == 20 and done.error["reason"] == "self_approval"
    assert done.error["details"]["basis"] == "authenticated"
    assert gl.writes() == [] and gl.audit() == []


def test_a_gitlab_dry_run_of_a_self_approval_reports_the_refusal(gl: Gl) -> None:
    set_mr_author(gl, 31, "user-4")
    done = run_gl(gl, "--dry-run")
    assert done.code == 20
    assert done.json["would_refuse"]["reason"] == "self_approval"
    assert gl.writes() == []


def test_a_gitlab_dry_run_reads_and_writes_nothing(gl: Gl) -> None:
    done = run_gl(gl, "--dry-run", body="x\n")
    assert done.code == 0, done.err
    assert done.json["dry_run"] is True and gl.writes() == []
