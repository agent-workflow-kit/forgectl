"""`forgectl approve` on GitHub (#28), and the state step it shares with `review`: the
self-approval check, the already-approved rule with the retarget walk, exact `head_verified`.

The real CLI runs in process against the fake `gh`; nothing reaches a host. Every answer is
hand-built from the documented shapes and stays UNVERIFIED until the live suite (#34).
"""

from __future__ import annotations

import hashlib
import io
import sys
from typing import Any

import pytest

from forgectl.cli import main

from .harness import golden, schemas
from .test_github_show import HEAD, recording
from .test_post_github import ME as GH_ME
from .test_post_github import (
    NO_TTY,
    Result,
    Rig,
    hook_config,
    rig,  # noqa: F401  (the fixture)
)
from .test_review_github import (
    MARKER,
    ReviewRig,
    _hit,
    my_review,
)
from .test_review_github import PR as GH_PR

NEWER = "2026-10-02T11:00:00Z"  # after my_review's submission at 10:00
EARLIER = "2026-10-02T09:30:00Z"


@pytest.fixture
def rv(rig: Rig) -> ReviewRig:  # noqa: F811
    return ReviewRig(rig)


def run_gh(
    rig_: ReviewRig,
    *extra: str,
    body: str | None = None,
    config: str = "",
    actor: str = "auto:bot",
) -> Result:
    argv = ["approve", GH_PR, "--json", "--config", str(rig_.base.config(config))]
    argv += ["--expected-head", HEAD, "--expected-base", "main", "--actor", actor, "--yes"]
    if body is not None:
        path = rig_.tmp / "approve-body.md"
        path.write_text(body, "utf-8")
        argv += ["--body-file", str(path)]
    argv += list(extra)
    rig_.base.monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code = main(argv, tty=NO_TTY)
    captured = rig_.base.capsys.readouterr()
    return Result(code, captured.out, captured.err)


# -- GitHub -------------------------------------------------------------------------------------


def arm_gh(rv_: ReviewRig) -> None:
    rv_.input({"event": "approve"})  # only so `posted()` answers with an APPROVED review
    rv_.posted()


def with_pr(rv_: ReviewRig, reviews: list[dict[str, Any]], **pr_fields: Any) -> None:
    doc = recording()
    pull = doc["data"]["repository"]["pullRequest"]
    pull["reviews"]["nodes"].extend(reviews)
    pull.update(pr_fields)
    rv_.base.set_pr(doc)


def retarget(at: str) -> dict[str, Any]:
    return {"timelineItems": {"nodes": [{"__typename": "BaseRefChangedEvent", "createdAt": at}]}}


def test_a_bodyless_github_approve_sends_the_event_and_the_commit_only(rv: ReviewRig) -> None:
    arm_gh(rv)
    done = run_gh(rv)
    assert done.code == 0, done.err
    assert rv.sent() == [{"commit_id": HEAD, "event": "APPROVE"}]
    out = done.json
    assert out["action"] == "approve" and out["head_verified"] is None
    assert out["already_posted"] is False and out["review_id"] is not None
    schemas.validate_cli_output("approve", out)
    golden.assert_golden("approve/github-posted", out, schema_id="forgectl/cli/approve.json")
    records = rv.audit()
    assert [r["result"] for r in records] == ["attempted", "approved"]
    assert records[0]["action"] == "approve" and records[0]["inputs"]["event"] is None


def test_a_github_approve_body_is_the_reviews_body_with_the_marker(rv: ReviewRig) -> None:
    arm_gh(rv)
    rv.comments_empty()
    done = run_gh(rv, "--dedupe-key", "key-1", body="looks good\n")
    assert done.code == 0, done.err
    sent = rv.sent()[0]
    assert sent["body"] == f"looks good\n{MARKER}" and sent["event"] == "APPROVE"
    schemas.validate_cli_output("approve", done.json)


def test_a_dry_run_plan_has_the_body_hash_and_no_event(rv: ReviewRig) -> None:
    done = run_gh(rv, "--dry-run", body="looks good\n")
    assert done.code == 0, done.err
    plan = done.json["plan"]
    assert plan["event"] is None and plan["inline_comments"] == 0
    assert plan["body_sha256"] == hashlib.sha256(b"looks good\n").hexdigest()
    golden.assert_golden("approve/github-dry-run", done.json, schema_id="forgectl/cli/approve.json")
    assert rv.posts() == [] and rv.audit() == []


def test_an_approval_at_this_head_is_already_approved_and_verified(rv: ReviewRig) -> None:
    with_pr(rv, [my_review("APPROVED")])
    done = run_gh(rv)
    assert done.code == 0, done.err
    assert done.json["already_posted"] is True and done.json["head_verified"] is True
    assert rv.posts() == [] and rv.audit() == []


def test_an_approval_older_than_the_latest_retarget_is_approved_again(rv: ReviewRig) -> None:
    with_pr(rv, [my_review("APPROVED")], **retarget(NEWER))
    arm_gh(rv)
    done = run_gh(rv)
    assert done.code == 0, done.err
    assert rv.sent() == [{"commit_id": HEAD, "event": "APPROVE"}]
    assert done.json["head_verified"] is None and done.json["already_posted"] is False


def test_an_approval_after_the_latest_retarget_counts(rv: ReviewRig) -> None:
    with_pr(rv, [my_review("APPROVED")], **retarget(EARLIER))
    done = run_gh(rv)
    assert done.code == 0, done.err
    assert done.json["already_posted"] is True and done.json["head_verified"] is True
    assert rv.posts() == []


def test_an_approval_with_no_time_does_not_count_once_a_retarget_exists(rv: ReviewRig) -> None:
    review = my_review("APPROVED")
    review["submittedAt"] = None
    with_pr(rv, [review], **retarget(EARLIER))
    arm_gh(rv)
    done = run_gh(rv)
    assert done.code == 0, done.err
    assert rv.sent()[0]["event"] == "APPROVE" and done.json["head_verified"] is None


def test_an_approval_at_an_old_head_is_none(rv: ReviewRig) -> None:
    with_pr(rv, [my_review("APPROVED", oid="4" * 40)])
    arm_gh(rv)
    done = run_gh(rv)
    assert done.code == 0, done.err
    assert done.json["head_verified"] is None and rv.sent()[0]["event"] == "APPROVE"


def test_a_later_changes_requested_review_is_not_an_approval(rv: ReviewRig) -> None:
    older = my_review("APPROVED")
    newer = {**my_review("CHANGES_REQUESTED"), "submittedAt": "2026-10-02T10:30:00Z"}
    with_pr(rv, [older, newer])
    arm_gh(rv)
    done = run_gh(rv)
    assert done.code == 0, done.err
    assert rv.sent()[0]["event"] == "APPROVE"


def test_approving_your_own_pull_request_is_self_approval(rv: ReviewRig) -> None:
    author = {"__typename": "User", "databaseId": GH_ME, "login": "user-4"}
    with_pr(rv, [], author=author)
    done = run_gh(rv)
    assert done.code == 20 and done.error["code"] == "refused"
    assert done.error["reason"] == "self_approval"
    assert done.error["details"]["basis"] == "authenticated"
    assert rv.posts() == [] and rv.audit() == []


def test_the_configured_me_is_self_approval_with_its_own_basis(rv: ReviewRig) -> None:
    done = run_gh(rv, config="me:\n  github: USER-1\n")  # the recording's author, in other case
    assert done.code == 20
    assert done.error["reason"] == "self_approval"
    assert done.error["details"]["basis"] == "configured_me"
    assert rv.posts() == []


def test_a_self_approval_dry_run_reports_the_refusal(rv: ReviewRig) -> None:
    author = {"__typename": "User", "databaseId": GH_ME, "login": "user-4"}
    with_pr(rv, [], author=author)
    done = run_gh(rv, "--dry-run", body="x\n")
    assert done.code == 20, done.err
    assert done.json["would_refuse"]["reason"] == "self_approval"
    assert done.json["action"] == "approve" and done.json["dry_run"] is True
    golden.assert_golden(
        "approve/github-dry-run-would-refuse", done.json, schema_id="forgectl/cli/approve.json"
    )


def test_review_with_an_approve_event_checks_self_approval_too(rv: ReviewRig) -> None:
    author = {"__typename": "User", "databaseId": GH_ME, "login": "user-4"}
    with_pr(rv, [], author=author)
    rv.input({"event": "approve", "body": "x"})
    done = rv.run()
    assert done.code == 20 and done.error["reason"] == "self_approval"
    assert rv.posts() == []


def test_a_dismissed_approval_is_none_and_the_approval_is_sent_again(rv: ReviewRig) -> None:
    older = my_review("APPROVED")
    dismissed = {**my_review("DISMISSED"), "submittedAt": "2026-10-02T10:30:00Z"}
    with_pr(rv, [older, dismissed])
    arm_gh(rv)
    done = run_gh(rv)
    assert done.code == 0, done.err
    assert rv.sent() == [{"commit_id": HEAD, "event": "APPROVE"}]
    assert done.json["already_posted"] is False and done.json["head_verified"] is None


def own_pr(rv_: ReviewRig) -> None:
    author = {"__typename": "User", "databaseId": GH_ME, "login": "user-4"}
    with_pr(rv_, [], author=author)


def test_allow_self_on_my_own_pull_request_is_audited_as_an_override(rv: ReviewRig) -> None:
    own_pr(rv)
    arm_gh(rv)
    done = run_gh(rv, "--allow-self", actor="human:me")
    assert done.code == 0, done.err
    assert [r["override"] for r in rv.audit()] == [["allow_self"]] * 2


def test_allow_self_on_someone_elses_pull_request_is_not_an_override(rv: ReviewRig) -> None:
    arm_gh(rv)
    done = run_gh(rv, "--allow-self", actor="human:me")
    assert done.code == 0, done.err
    assert [r["override"] for r in rv.audit()] == [[]] * 2


def test_allow_self_on_a_review_with_an_approve_event_is_audited(rv: ReviewRig) -> None:
    own_pr(rv)
    rv.input({"event": "approve", "body": "x"})
    rv.posted()
    done = rv.run("--allow-self", actor="human:me")
    assert done.code == 0, done.err
    assert rv.audit()[0]["override"] == ["allow_self"]


def test_a_dedupe_key_needs_a_body_file(rv: ReviewRig) -> None:
    done = run_gh(rv, "--dedupe-key", "key-1")
    assert done.code == 2
    assert rv.posts() == []


@pytest.mark.parametrize("text", ["", " \n\t"])
def test_an_empty_body_file_is_a_usage_error(rv: ReviewRig, text: str) -> None:
    done = run_gh(rv, body=text)
    assert done.code == 2 and rv.posts() == []


def test_a_github_hit_with_the_approval_in_effect_is_exit_0(rv: ReviewRig) -> None:
    _hit(rv)
    with_pr(rv, [my_review("APPROVED")])
    done = run_gh(rv, "--dedupe-key", "key-1", body="words\n")
    assert done.code == 0, done.err
    assert done.json["already_posted"] is True and done.json["head_verified"] is True
    assert done.json["ids"] == ["gh:r:PRR_old"] and rv.posts() == []


def test_a_github_hit_with_the_approval_gone_is_outcome_partial_with_no_write(
    rv: ReviewRig,
) -> None:
    _hit(rv)
    with_pr(rv, [my_review("DISMISSED")])
    done = run_gh(rv, "--dedupe-key", "key-1", body="words\n")
    assert done.code == 12 and done.error["code"] == "outcome_partial"
    assert done.error["details"]["failed"] == "approve" and rv.posts() == []


def test_a_pre_post_veto_stops_an_approve_before_the_host_call(rv: ReviewRig) -> None:
    arm_gh(rv)
    done = run_gh(rv, config=hook_config("pre-post", "exit 11"))
    assert done.code == 11, done.err
    assert rv.posts() == []
