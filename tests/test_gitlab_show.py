"""GitLab `show` (issue #15): the MR object, readiness, approvals, bots, renames and CI.

Answers come from hand-built recordings (``tests/recordings/gitlab``), served by an in-process
`glab` through the real gateway. Every GitLab behavior these recordings assume is UNVERIFIED until
the live suite (#31, #35) runs; the issue report lists them.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from forgectl.backends import gitlab as gl
from forgectl.backends.base import Backend
from forgectl.backends.gitlab import GitLabBackend
from forgectl.errors import ForgectlError
from forgectl.model import PrRead

from .gitlab_support import (
    BASE,
    HEAD,
    MR_ID,
    OLD,
    FakeGitLab,
    make_backend,
    mr_path,
    recording,
    ref,
    standard,
)
from .harness import golden, schemas

STATUSES = f"projects/101/repository/commits/{HEAD}/statuses"


@pytest.fixture
def fake() -> FakeGitLab:
    return FakeGitLab()


def setup(fake: FakeGitLab, tmp_path: Path, **overrides: Any) -> tuple[GitLabBackend, dict]:
    data = standard(fake, **overrides)
    return make_backend(fake, tmp_path), data


def show(fake: FakeGitLab, tmp_path: Path, mutate: Any = None, **overrides: Any) -> PrRead:
    mr = recording("mr")
    if mutate is not None:
        mutate(mr)
    backend, _ = setup(fake, tmp_path, mr=mr, **overrides)
    return backend.get_pr(ref())


def codes(read: PrRead) -> list[str]:
    return [b.code.value + (f":{b.detail}" if b.detail else "") for b in read.pr.blocked_by]


# -- the happy path and the protocol ---------------------------------------------------------


def test_the_backend_satisfies_the_protocol_and_names_itself(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = make_backend(fake, tmp_path)
    assert isinstance(backend, Backend)
    assert backend.name == "gitlab"
    assert gl.BACKEND is GitLabBackend


def test_an_open_ready_mr_is_normalized(fake: FakeGitLab, tmp_path: Path) -> None:
    read = show(fake, tmp_path)
    pr = read.pr.to_dict()
    assert pr["pr"] == "gitlab.com/user-9/proj#7"
    assert pr["repo"] == "gitlab.com/user-9/proj"
    assert pr["url"] == "https://gitlab.com/user-9/proj/-/merge_requests/7"
    assert pr["backend"] == "gitlab"
    assert pr["pr_id"] == "gid://gitlab/MergeRequest/90001"
    assert pr["author"] == {"login": "user-1", "id": "11", "is_bot": False}
    assert (pr["state"], pr["draft"], pr["mergeable"]) == ("open", False, True)
    assert pr["head"] == {
        "sha": HEAD,
        "ref": "feature/widget",
        "repo": "gitlab.com/user-9/proj",
        "is_fork": False,
    }
    assert pr["base"] == {"ref": "main", "sha": BASE, "is_default": True}
    assert pr["head_fetch_ref"] == "refs/merge-requests/7/head"
    assert pr["labels"] == ["needs review", "type::feature"]
    assert (pr["merged_head_sha"], pr["merge_commit_sha"], pr["merged_by"]) == (None, None, None)
    assert pr["author_association"] is None
    assert (pr["additions"], pr["deletions"], pr["changed_files"]) == (None, None, 3)
    assert pr["merge_queue"] is False
    assert pr["merge_methods_allowed"] == ["merge", "squash"]
    assert pr["auto_merge"] == {"enabled": False, "by": None}
    assert pr["blocked_by"] == []
    assert (pr["review_decision"], pr["approvals"], pr["approvals_at_head"]) == (
        "approved",
        1,
        None,
    )
    assert read.ci.state == "passing"


def test_show_output_matches_the_schema_and_golden(fake: FakeGitLab, tmp_path: Path) -> None:
    golden.assert_golden(
        "gitlab/show-open-ready",
        show(fake, tmp_path).pr.to_dict(),
        schema_id="forgectl/cli/show.json",
    )


def test_golden_merged_fork_head(fake: FakeGitLab, tmp_path: Path) -> None:
    def mutate(mr: dict) -> None:
        merged(mr)
        mr["source_project_id"] = 202

    golden.assert_golden(
        "gitlab/show-merged-fork",
        show(fake, tmp_path, mutate).pr.to_dict(),
        schema_id="forgectl/cli/show.json",
    )


def test_golden_blocked(fake: FakeGitLab, tmp_path: Path) -> None:
    def mutate(mr: dict) -> None:
        mr.update(draft=True, has_conflicts=True, detailed_merge_status="need_rebase")
        mr["labels"] = ["z", "a"]

    statuses = recording("statuses")
    statuses[1]["status"] = "failed"
    reviewers = recording("reviewers")
    reviewers[1]["state"] = "requested_changes"
    read = show(fake, tmp_path, mutate, statuses=statuses, reviewers=reviewers)
    golden.assert_golden(
        "gitlab/show-blocked", read.pr.to_dict(), schema_id="forgectl/cli/show.json"
    )


def test_golden_reset_approvals_on_push(fake: FakeGitLab, tmp_path: Path) -> None:
    backend, _ = setup(fake, tmp_path)
    fake.on("projects/101/approvals", {"reset_approvals_on_push": True})
    golden.assert_golden(
        "gitlab/show-reset-approvals",
        backend.get_pr(ref()).pr.to_dict(),
        schema_id="forgectl/cli/show.json",
    )


def test_every_read_goes_through_the_gateway_with_encoded_paths(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    show(fake, tmp_path)
    assert fake.unrouted == []
    first = fake.calls[0]
    assert first.path == "projects/user-9%2Fproj/merge_requests/7"
    for call in fake.calls:
        assert call.argv[-2] == "--"
        assert call.cwd  # a private cwd the gateway made
    # later reads use the numeric project id, never the (renamable) path
    assert all("user-9" not in c.path for c in fake.calls[1:])


# -- renames, identity ------------------------------------------------------------------------


def test_a_renamed_project_is_read_through_its_old_path_and_reported_canonically(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    mr = recording("mr")
    fake.on(mr_path("user-7/old-proj"), mr)  # glab follows the 301 and shows the final answer
    standard(fake, mr=mr)
    backend = make_backend(fake, tmp_path)
    read = backend.get_pr(ref("user-7/old-proj"))
    assert read.pr.pr == "gitlab.com/user-9/proj#7"
    assert read.pr.repo == "gitlab.com/user-9/proj"
    assert read.pr.pr_id == "gid://gitlab/MergeRequest/90001"  # stable across renames


def test_an_answer_from_another_host_is_wrong_host_response(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    def other(mr: dict) -> None:
        mr["web_url"] = "https://evil.example.test/user-9/proj/-/merge_requests/7"

    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, other)
    assert raised.value.code == "wrong_host_response"
    assert "rotated" in raised.value.message


def test_an_answer_for_another_number_is_wrong_host_response(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    def other(mr: dict) -> None:
        mr["iid"] = 8
        mr["web_url"] = "https://gitlab.com/user-9/proj/-/merge_requests/8"

    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, other)
    assert raised.value.code == "wrong_host_response"


def test_a_project_answer_for_another_path_is_wrong_host_response(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    project = recording("project")
    project["web_url"] = "https://gitlab.com/elsewhere/proj"
    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, project=project)
    assert raised.value.code == "wrong_host_response"


def test_a_project_answer_for_another_id_is_wrong_host_response(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    project = recording("project")
    project["id"] = 999
    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, project=project)
    assert raised.value.code == "wrong_host_response"


@pytest.mark.parametrize(
    "web_url",
    [
        "https://gitlab.com/user-9/../proj/-/merge_requests/7",
        "https://gitlab.com/user-9/%2e%2e/proj/-/merge_requests/7",
        "https://gitlab.com/user-9/proj/-/merge_requests/7?x=1",
        "https://gitlab.com?/user-9/proj/-/merge_requests/7",
        "https://gitlab.com#/user-9/proj/-/merge_requests/7",
        "https://gitlab.com/user-9/proj/-/merge_requests/7#",
        "https://gitlab.com/user-9;x/proj/-/merge_requests/7",
        "https://gitlab.com/user-9/merge_requests/proj/-/merge_requests/7",
        "https://gitlab.com/merge_requests/proj/-/merge_requests/7",
        "https://gitlab.com/-/proj/-/merge_requests/7",
        "https://gitlab.com/user-9/pr oj/-/merge_requests/7",
        "https://gitlab.com/user-9/proj/-/issues/7",
        "https://u" + "@" + "gitlab.com/user-9/proj/-/merge_requests/7",
        "http://gitlab.com/user-9/proj/-/merge_requests/7",
        "gitlab.com/user-9/proj/-/merge_requests/7",
        "",
        None,
        7,
    ],
)
def test_an_unusable_web_url_is_a_typed_error_never_a_path(
    fake: FakeGitLab, tmp_path: Path, web_url: Any
) -> None:
    def hostile(mr: dict) -> None:
        mr["web_url"] = web_url

    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, hostile)
    assert raised.value.code in ("wrong_host_response", "upstream_error")
    assert len(fake.calls) == 1  # nothing was built from it


# -- state and readiness ---------------------------------------------------------------------


def merged(mr: dict) -> None:
    mr.update(
        state="merged",
        merged_at="2026-10-06T08:00:00.000Z",
        merge_user=recording("user") | {"id": 12, "username": "user-2"},
        merge_commit_sha="d4" * 20,
        detailed_merge_status="not_open",
        merge_status="can_be_merged",
        merge_when_pipeline_succeeds=True,
    )


def test_a_merged_mr_has_the_merged_fields_and_no_readiness(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    pr = show(fake, tmp_path, merged).pr.to_dict()
    assert pr["state"] == "merged"
    assert pr["merged_head_sha"] == HEAD
    assert pr["merge_commit_sha"] == "d4" * 20
    assert pr["merged_by"] == "user-2"
    assert pr["merged_at"] == "2026-10-06T08:00:00.000Z"
    assert pr["mergeable"] is None and pr["blocked_by"] == []
    assert pr["auto_merge"] == {"enabled": False, "by": None}  # the host's flag is stale


@pytest.mark.parametrize(
    ("merge_commit", "squash_commit", "expected"),
    [("d4" * 20, "e5" * 20, "d4" * 20), (None, "e5" * 20, "e5" * 20), (None, None, HEAD)],
)
def test_merge_commit_sha_falls_back_to_squash_then_sha(
    fake: FakeGitLab, tmp_path: Path, merge_commit: Any, squash_commit: Any, expected: str
) -> None:
    def mutate(mr: dict) -> None:
        merged(mr)
        mr["merge_commit_sha"], mr["squash_commit_sha"] = merge_commit, squash_commit

    assert show(fake, tmp_path, mutate).pr.merge_commit_sha == expected


def test_a_closed_mr_has_no_readiness(fake: FakeGitLab, tmp_path: Path) -> None:
    def closed(mr: dict) -> None:
        mr.update(
            state="closed", closed_at="2026-10-06T08:00:00.000Z", detailed_merge_status="not_open"
        )

    pr = show(fake, tmp_path, closed).pr.to_dict()
    assert (pr["state"], pr["mergeable"], pr["blocked_by"]) == ("closed", None, [])
    assert pr["closed_at"] == "2026-10-06T08:00:00.000Z"
    assert pr["merged_head_sha"] is None


def test_a_locked_mr_reads_open_with_a_locked_blocker(fake: FakeGitLab, tmp_path: Path) -> None:
    def locked(mr: dict) -> None:
        mr["state"] = "locked"

    read = show(fake, tmp_path, locked)
    assert read.pr.state == "open"
    assert "other:locked" in codes(read)


def test_an_unknown_state_fails_closed(fake: FakeGitLab, tmp_path: Path) -> None:
    def odd(mr: dict) -> None:
        mr["state"] = "frozen"

    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, odd)
    assert raised.value.code == "upstream_error"


STATUS_TABLE = [
    ("mergeable", True, []),
    ("not_open", False, ["other:not_open"]),
    ("draft_status", False, ["draft"]),
    ("conflict", False, ["conflicts"]),
    ("need_rebase", False, ["branch_behind"]),
    ("checking", None, ["mergeability_unknown"]),
    ("unchecked", None, ["mergeability_unknown"]),
    ("preparing", None, ["mergeability_unknown"]),
    ("not_approved", False, ["reviews_required"]),
    ("requested_changes", False, ["changes_requested"]),
    ("discussions_not_resolved", False, ["discussions_unresolved"]),
    ("ci_still_running", False, ["checks_pending"]),
    ("ci_must_pass", False, ["checks_pending"]),
    ("merge_time", False, ["other:merge_time"]),
    ("commits_status", False, ["other:commits_status"]),
    ("merge_request_blocked", False, ["other:merge_request_blocked"]),
    ("a_status_from_the_future", False, ["other:a_status_from_the_future"]),
]


@pytest.mark.parametrize(("status", "mergeable", "expected"), STATUS_TABLE)
def test_detailed_merge_status_maps_to_blockers(
    fake: FakeGitLab, tmp_path: Path, status: str, mergeable: Any, expected: list[str]
) -> None:
    def mutate(mr: dict) -> None:
        # the flags stay false, so each row tests the status mapping alone
        mr.update(detailed_merge_status=status, draft=False, has_conflicts=False)

    read = show(fake, tmp_path, mutate)
    assert read.pr.mergeable is mergeable
    assert codes(read) == expected


def test_has_conflicts_wins_over_need_rebase(fake: FakeGitLab, tmp_path: Path) -> None:
    def mutate(mr: dict) -> None:
        mr.update(detailed_merge_status="need_rebase", has_conflicts=True)

    assert codes(show(fake, tmp_path, mutate)) == ["branch_behind", "conflicts"]


def test_the_draft_flag_blocks_whatever_the_status_says(fake: FakeGitLab, tmp_path: Path) -> None:
    def mutate(mr: dict) -> None:
        mr["draft"] = True

    read = show(fake, tmp_path, mutate)
    assert read.pr.draft is True and "draft" in codes(read)


def test_a_missing_detailed_status_is_unsupported_host_version_naming_the_version(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    def old(mr: dict) -> None:
        del mr["detailed_merge_status"]

    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, old, version={"version": "15.4.0"})
    assert raised.value.code == "unsupported_host_version"
    assert "15.4" in raised.value.message
    assert fake.count("version") == 1


def test_a_missing_status_with_an_unreadable_version_is_still_unsupported(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    def old(mr: dict) -> None:
        del mr["detailed_merge_status"]

    mr = recording("mr")
    old(mr)
    standard(fake, mr=mr)
    fake.on("version", {"version": "not-a-version"})
    with pytest.raises(ForgectlError) as raised:
        make_backend(fake, tmp_path).get_pr(ref())
    assert raised.value.code == "unsupported_host_version"


def test_without_a_readiness_hook_the_mr_is_read_once(fake: FakeGitLab, tmp_path: Path) -> None:
    def checking(mr: dict) -> None:
        mr["detailed_merge_status"] = "checking"

    assert gl.READINESS_UNKNOWN == frozenset({"checking", "unchecked", "preparing"})
    read = show(fake, tmp_path, checking)
    assert codes(read) == ["mergeability_unknown"] and read.pr.mergeable is None
    assert fake.count(mr_path()) == 1


def _recheck_once(fetch: Any, unknown: Any) -> tuple[Any, bool]:
    answer = fetch()
    if unknown(answer):
        answer = fetch()
    return answer, unknown(answer)


def test_the_readiness_hook_rereads_only_the_mr_get(fake: FakeGitLab, tmp_path: Path) -> None:
    from .gitlab_support import Answer

    first = recording("mr")
    first["detailed_merge_status"] = "checking"
    answers = [first, recording("mr")]
    standard(fake)
    seen: list[int] = []

    def handler(_query: dict[str, str]) -> Answer:
        seen.append(1)
        return Answer(200, answers[min(len(seen), len(answers)) - 1])

    fake.on_handler(mr_path(), handler)
    read = make_backend(fake, tmp_path, until_known=_recheck_once).get_pr(ref())
    assert fake.count(mr_path()) == 2
    assert fake.count("projects/101") == 1
    assert fake.count(f"{MR_ID}/approvals") == 1 and fake.count(f"{MR_ID}/reviewers") == 1
    assert fake.count(STATUSES) == 1
    assert read.pr.mergeable is True and read.pr.blocked_by == ()


def test_a_status_still_unknown_after_the_hook_is_mergeability_unknown(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    mr = recording("mr")
    mr["detailed_merge_status"] = "unchecked"
    standard(fake, mr=mr)
    read = make_backend(fake, tmp_path, until_known=_recheck_once).get_pr(ref())
    assert fake.count(mr_path()) == 2
    assert codes(read) == ["mergeability_unknown"]


def test_every_other_protocol_method_is_a_typed_not_yet_error(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = make_backend(fake, tmp_path)
    for call in (
        lambda: backend.snapshot(ref()),
        lambda: backend.comments(ref()),
        lambda: backend.reviews(ref()),
        lambda: backend.ci(ref()),
        lambda: backend.files(ref()),
        lambda: backend.diff(ref()),
        lambda: backend.compare(ref(), HEAD, BASE),
    ):
        with pytest.raises(ForgectlError) as raised:
            call()
        assert raised.value.code == "internal"
    assert fake.calls == []


# -- approvals and reviewers -----------------------------------------------------------------


def test_approvals_left_gives_review_required(fake: FakeGitLab, tmp_path: Path) -> None:
    approvals = recording("approvals")
    approvals.update(approvals_left=1, approved=False)
    pr = show(fake, tmp_path, approvals=approvals).pr
    assert pr.review_decision == "review_required" and pr.approvals == 1
    assert "reviews_required" not in codes(PrRead(pr, show(FakeGitLab(), tmp_path / "b").ci))


def test_a_null_approvals_left_reads_none_unless_a_change_was_requested(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    approvals = recording("approvals")
    approvals.update(approvals_left=None, approved_by=[])
    assert show(fake, tmp_path, approvals=approvals).pr.review_decision == "none"
    reviewers = recording("reviewers")
    reviewers[1]["state"] = "requested_changes"
    pr = show(FakeGitLab(), tmp_path / "b", approvals=approvals, reviewers=reviewers).pr
    assert pr.review_decision == "changes_requested"
    assert "changes_requested" in [b.code.value for b in pr.blocked_by]


def test_requested_changes_blocks_even_when_the_host_status_says_mergeable(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    reviewers = recording("reviewers")
    reviewers[1]["state"] = "requested_changes"
    assert "changes_requested" in codes(show(fake, tmp_path, reviewers=reviewers))


def test_the_host_status_requested_changes_blocks_without_a_reviewer_entry(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    def mutate(mr: dict) -> None:
        mr["detailed_merge_status"] = "requested_changes"

    reviewers = recording("reviewers")  # no reviewer is in requested_changes
    assert "changes_requested" in codes(show(fake, tmp_path, mutate, reviewers=reviewers))


def test_one_account_in_approved_by_and_reviewers_is_one_entry(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    # user-2 is both in approved_by and a reviewer in state approved: counted once
    assert show(fake, tmp_path).pr.approvals == 1


def test_precedence_requested_changes_over_approved_by_over_unapproved(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    reviewers = recording("reviewers")
    reviewers[0]["state"] = "requested_changes"  # user-2 also appears in approved_by
    pr = show(fake, tmp_path, reviewers=reviewers).pr
    assert pr.approvals == 0 and pr.review_decision == "changes_requested"
    reviewers[0]["state"] = "unapproved"  # approved_by wins over unapproved
    assert show(FakeGitLab(), tmp_path / "b", reviewers=reviewers).pr.approvals == 1


def test_the_authors_own_approval_never_counts(fake: FakeGitLab, tmp_path: Path) -> None:
    approvals = recording("approvals")
    approvals["approved_by"] = [{"user": recording("mr")["author"]}]
    assert show(fake, tmp_path, approvals=approvals).pr.approvals == 0


@pytest.mark.parametrize("status", [404, 403])
def test_an_unreadable_reviewers_endpoint_fails_the_read(
    fake: FakeGitLab, tmp_path: Path, status: int
) -> None:
    backend, _ = setup(fake, tmp_path)
    fake.on(f"{MR_ID}/reviewers", {"message": "x"}, status=status)
    with pytest.raises(ForgectlError) as raised:
        backend.get_pr(ref())
    assert raised.value.code == "upstream_error"
    schemas.validate_error_envelope(raised.value.envelope())


# -- reset_approvals_on_push -----------------------------------------------------------------


def reset_on(fake: FakeGitLab) -> None:
    fake.on("projects/101/approvals", {"reset_approvals_on_push": True})


def test_approvals_at_head_is_null_unless_approvals_reset_on_push(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    read = show(fake, tmp_path)
    assert read.pr.approvals_at_head is None
    assert fake.count(f"{MR_ID}/notes") == 0  # no walk, no member read
    assert fake.count("projects/101/members/all/12") == 0


def test_with_reset_on_push_a_pusher_approval_counts_at_head(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend, _ = setup(fake, tmp_path)
    reset_on(fake)
    pr = backend.get_pr(ref()).pr
    assert pr.approvals_at_head == 1 and pr.approvals == 1
    assert fake.count("projects/101/members/all/12") == 1
    backend.get_pr(ref())
    assert fake.count("projects/101/members/all/12") == 1  # cached per process


@pytest.mark.parametrize(
    ("member", "status", "counted"),
    [
        ({"id": 12, "access_level": 20, "state": "active", "expires_at": None}, 200, False),
        ({"id": 12, "access_level": 30, "state": "blocked", "expires_at": None}, 200, False),
        ({"id": 12, "access_level": 30, "state": "active", "expires_at": "2020-01-01"}, 200, False),
        ({"id": 12, "access_level": 30, "state": "active", "expires_at": "2999-01-01"}, 200, True),
        ({"id": 12, "access_level": 50, "state": "active", "expires_at": None}, 200, True),
        ({"id": 12, "access_level": 30}, 200, False),
        ({"id": 12, "access_level": "30", "state": "active"}, 200, False),
        (
            {"id": 12, "access_level": 30, "state": "active", "membership_state": "active"},
            200,
            True,
        ),
        (
            {"id": 12, "access_level": 30, "state": "active", "membership_state": "awaiting"},
            200,
            False,
        ),
        ({"id": 12, "access_level": 30, "state": "active", "membership_state": None}, 200, False),
        ({"message": "404 Not found"}, 404, False),
    ],
)
def test_can_push_follows_the_access_rule(
    fake: FakeGitLab, tmp_path: Path, member: dict, status: int, counted: bool
) -> None:
    backend, _ = setup(fake, tmp_path)
    reset_on(fake)
    fake.on("projects/101/members/all/12", member, status=status)
    pr = backend.get_pr(ref()).pr
    assert pr.approvals_at_head == (1 if counted else 0)


@pytest.mark.parametrize("status", [401, 403])
def test_an_unreadable_membership_is_null_can_push_not_a_failure(
    fake: FakeGitLab, tmp_path: Path, status: int
) -> None:
    backend, _ = setup(fake, tmp_path)
    reset_on(fake)
    fake.on("projects/101/members/all/12", {"message": "no"}, status=status)
    pr = backend.get_pr(ref()).pr
    assert pr.approvals == 1  # unknown can_push still counts for the display count
    assert pr.approvals_at_head == 0  # and never for the gate


def test_an_approval_older_than_the_latest_retarget_does_not_count(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    approvals = recording("approvals")
    approvals["approved_by"][0]["approved_at"] = "2026-10-03T00:00:00.000Z"
    backend, _ = setup(fake, tmp_path, approvals=approvals)
    reset_on(fake)
    notes = [
        {
            "id": 900,
            "system": True,
            "body": "changed target branch from `a` to `b`",
            "created_at": "2026-10-04T00:00:00.000Z",
        },
        {
            "id": 899,
            "system": False,
            "body": "changed target branch from `a` to `z`",
            "created_at": "2026-10-09T00:00:00.000Z",
        },  # a human's note is not a retarget
    ]
    fake.pages(f"{MR_ID}/notes", [notes])
    assert backend.get_pr(ref()).pr.approvals_at_head == 0
    notes[0]["created_at"] = "2026-10-02T00:00:00.000Z"
    assert backend.get_pr(ref()).pr.approvals_at_head == 1


def test_an_approval_with_no_time_does_not_count_when_a_retarget_exists(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend, _ = setup(fake, tmp_path)
    reset_on(fake)
    fake.pages(
        f"{MR_ID}/notes",
        [
            [
                {
                    "id": 900,
                    "system": True,
                    "body": "changed target branch from `a` to `b`",
                    "created_at": "2026-10-04T00:00:00.000Z",
                }
            ]
        ],
    )
    assert backend.get_pr(ref()).pr.approvals_at_head == 0


def test_the_retarget_walk_covers_every_page_and_takes_the_newest(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    approvals = recording("approvals")
    approvals["approved_by"][0]["approved_at"] = "2026-10-05T00:00:00.000Z"
    backend, _ = setup(fake, tmp_path, approvals=approvals)
    reset_on(fake)
    old = {
        "id": 1,
        "system": True,
        "body": "changed target branch from `a` to `b`",
        "created_at": "2026-10-01T00:00:00.000Z",
    }
    new = {
        "id": 2,
        "system": True,
        "body": "changed target branch from `b` to `c`",
        "created_at": "2026-10-06T00:00:00.000Z",
    }
    fake.pages(f"{MR_ID}/notes", [[old], [], [new]])  # an empty middle page does not end it
    assert backend.get_pr(ref()).pr.approvals_at_head == 0


@pytest.mark.parametrize("failure", [("status", 502), ("status", 429), ("junk", None)])
def test_a_failed_retarget_walk_counts_no_approval_and_warns(
    fake: FakeGitLab, tmp_path: Path, failure: tuple[str, Any]
) -> None:
    warnings: list[str] = []
    standard(fake)
    reset_on(fake)
    if failure[0] == "status":
        fake.on(f"{MR_ID}/notes", {"message": "x"}, status=failure[1])
    else:
        fake.on(
            f"{MR_ID}/notes",
            [
                {
                    "id": 1,
                    "system": True,
                    "body": "changed target branch from `a` to `b`",
                    "created_at": "yesterday",
                }
            ],
        )
    backend = make_backend(fake, tmp_path, warn=warnings.append)
    pr = backend.get_pr(ref()).pr
    assert pr.approvals_at_head is None
    assert warnings


def test_the_walk_stops_at_a_page_cap_and_counts_no_approval(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    standard(fake)
    reset_on(fake)
    page = [
        {
            "id": i,
            "system": True,
            "body": "added 1 commit",
            "created_at": "2026-10-01T00:00:00.000Z",
        }
        for i in range(3)
    ]
    fake.pages(f"{MR_ID}/notes", [page] * (gl.MAX_PAGES + 5))
    backend = make_backend(fake, tmp_path)
    assert backend.get_pr(ref()).pr.approvals_at_head is None
    assert fake.count(f"{MR_ID}/notes") == gl.MAX_PAGES


def test_an_unreadable_project_approval_setting_is_null(fake: FakeGitLab, tmp_path: Path) -> None:
    backend, _ = setup(fake, tmp_path)
    fake.on("projects/101/approvals", {"message": "no"}, status=403)
    assert backend.get_pr(ref()).pr.approvals_at_head is None


@pytest.mark.parametrize("value", [None, "true", 1, "yes", [], {}])
def test_a_reset_flag_that_is_not_a_boolean_is_unknown(
    fake: FakeGitLab, tmp_path: Path, value: Any
) -> None:
    backend, _ = setup(fake, tmp_path)
    fake.on("projects/101/approvals", {"reset_approvals_on_push": value})
    assert backend.get_pr(ref()).pr.approvals_at_head is None


# -- CI ---------------------------------------------------------------------------------------


def test_checks_come_from_the_head_pipelines_statuses(fake: FakeGitLab, tmp_path: Path) -> None:
    read = show(fake, tmp_path)
    ci = read.ci.to_dict()
    assert ci["state"] == "passing" and ci["for_current_head"] and ci["complete"]
    assert ci["head_sha"] == HEAD
    by_name = {c["name"]: c for c in ci["checks"]}
    assert by_name["lint"] == {
        "name": "lint",
        "state": "failing",
        "required": False,
        "counted": False,
        "url": "https://gitlab.com/user-9/proj/-/jobs/7005",
        "finished_at": "2026-10-05T12:05:00.000Z",
    }
    assert by_name["build"]["counted"] is True and by_name["build"]["required"] is None
    call = next(c for c in fake.calls if c.path == STATUSES)
    assert call.query == {"pipeline_id": "5001", "per_page": "100", "page": "1"}


def test_no_head_pipeline_is_none_with_no_read(fake: FakeGitLab, tmp_path: Path) -> None:
    def none(mr: dict) -> None:
        mr["head_pipeline"] = None

    read = show(fake, tmp_path, none)
    assert read.ci.state == "none" and read.ci.checks == ()
    assert read.ci.head_sha == HEAD and read.ci.for_current_head
    assert not any(c.path.endswith("/statuses") for c in fake.calls)
    assert read.pr.blocked_by == ()  # no CI is not a blocker


def test_a_pipeline_of_another_commit_is_pending_and_its_failures_are_not_checks_failing(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    def old(mr: dict) -> None:
        mr["head_pipeline"]["sha"] = OLD

    statuses = recording("statuses")
    statuses[0]["status"] = "failed"
    standard(fake)
    fake.pages(f"projects/101/repository/commits/{OLD}/statuses", [statuses])
    mr = recording("mr")
    old(mr)
    fake.on(mr_path(), mr)
    read = make_backend(fake, tmp_path).get_pr(ref())
    assert read.ci.state == "pending" and read.ci.for_current_head is False
    assert read.ci.head_sha == OLD
    assert [b.code.value for b in read.pr.blocked_by] == ["checks_pending"]


def test_a_failing_counted_check_blocks(fake: FakeGitLab, tmp_path: Path) -> None:
    statuses = recording("statuses")
    statuses[1]["status"] = "failed"
    read = show(fake, tmp_path, statuses=statuses)
    assert read.ci.state == "failing"
    assert "checks_failing" in codes(read)


def test_a_skipped_only_pipeline_is_none(fake: FakeGitLab, tmp_path: Path) -> None:
    statuses = recording("statuses")
    for s in statuses:
        s["status"] = "skipped"

    def skipped(mr: dict) -> None:
        mr["head_pipeline"]["status"] = "skipped"

    read = show(fake, tmp_path, skipped, statuses=statuses)
    assert read.ci.state == "none"


@pytest.mark.parametrize(
    ("status", "allow_failure", "state", "counted"),
    [
        ("manual", False, "pending", True),
        ("manual", None, "pending", True),
        ("manual", True, "skipped", False),
        ("created", False, "pending", True),
        ("running", False, "pending", True),
        ("canceled", False, "failing", True),
        ("canceling", False, "pending", True),
        ("waiting_for_callback", False, "pending", True),
        ("Failed", False, "pending", True),
        ("brand_new", False, "pending", True),
    ],
)
def test_check_states_follow_the_table_and_unmapped_is_pending(
    fake: FakeGitLab, tmp_path: Path, status: str, allow_failure: Any, state: str, counted: bool
) -> None:
    statuses = recording("statuses")[:1]
    statuses[0].update(status=status, allow_failure=allow_failure)
    warnings: list[str] = []
    standard(fake, statuses=statuses)
    read = make_backend(fake, tmp_path, warn=warnings.append).get_pr(ref())
    check = read.ci.checks[0]
    assert (check.state, check.counted) == (state, counted)
    if status in ("Failed", "brand_new"):
        assert warnings
    else:
        assert warnings == []


def test_the_latest_status_per_name_wins(fake: FakeGitLab, tmp_path: Path) -> None:
    statuses = recording("statuses")
    retry = dict(statuses[0], id=7100, status="failed")
    read = show(fake, tmp_path, statuses=[retry, *statuses])  # the newer id fails
    assert {c.name: c.state for c in read.ci.checks}["build"] == "failing"
    read = show(FakeGitLab(), tmp_path / "b", statuses=[dict(retry, id=1), *statuses])
    assert {c.name: c.state for c in read.ci.checks}["build"] == "passing"


def test_statuses_are_paged_and_a_failed_page_fails_the_read(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    statuses = recording("statuses")
    standard(fake)
    fake.pages(STATUSES, [statuses[:1], statuses[1:2], statuses[2:]])
    read = make_backend(fake, tmp_path).get_pr(ref())
    assert len(read.ci.checks) == 3
    assert fake.count(STATUSES) == 3

    def second_fails(query: dict[str, str]) -> Any:
        from .gitlab_support import Answer

        if query["page"] == "1":
            return Answer(200, statuses[:1], {"x-next-page": "2"})
        return Answer(502, {"message": "bad"})

    fake.on_handler(STATUSES, second_fails)
    with pytest.raises(ForgectlError) as raised:
        make_backend(fake, tmp_path / "b").get_pr(ref())
    assert raised.value.code == "upstream_error"


def test_a_status_list_cut_at_the_page_cap_is_incomplete_so_never_passing(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    statuses = recording("statuses")[:2]
    standard(fake)
    fake.pages(STATUSES, [statuses] * (gl.MAX_PAGES + 3))
    read = make_backend(fake, tmp_path).get_pr(ref())
    assert read.ci.complete is False and read.ci.state == "pending"
    assert fake.count(STATUSES) == gl.MAX_PAGES


def test_an_unfinished_pipeline_with_finished_jobs_stays_pending(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    def running(mr: dict) -> None:
        mr["head_pipeline"]["status"] = "running"  # e.g. child pipelines still to come

    read = show(fake, tmp_path, running)
    assert read.ci.state == "pending"
    assert "checks_pending" in codes(read)


def test_a_fork_pipeline_is_read_from_the_pipelines_project(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    def fork(mr: dict) -> None:
        mr["source_project_id"] = 202
        mr["head_pipeline"]["project_id"] = 202

    standard(fake)
    mr = recording("mr")
    fork(mr)
    fake.on(mr_path(), mr)
    fake.pages(f"projects/202/repository/commits/{HEAD}/statuses", [recording("statuses")])
    read = make_backend(fake, tmp_path).get_pr(ref())
    assert read.ci.state == "passing"
    assert read.pr.head.repo == "gitlab.com/user-8/proj" and read.pr.head.is_fork is True


def test_a_check_url_other_than_http_is_dropped_and_names_are_one_line(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    statuses = recording("statuses")
    statuses[0]["target_url"] = "javascript:alert(1)"
    statuses[1]["name"] = "te\nst‮"
    statuses[2]["target_url"] = "https://gitlab.com/x y"
    read = show(fake, tmp_path, statuses=statuses)
    urls = {c.name: c.url for c in read.ci.checks}
    assert urls["build"] is None
    assert any(c.name == "te<U+000A>st<U+202E>" for c in read.ci.checks)
    assert all(c.url is None for c in read.ci.checks if c.name == "lint")


# -- the head project, forks ---------------------------------------------------------------


def test_a_fork_head_names_the_source_project(fake: FakeGitLab, tmp_path: Path) -> None:
    def fork(mr: dict) -> None:
        mr["source_project_id"] = 202

    pr = show(fake, tmp_path, fork).pr
    assert pr.head.repo == "gitlab.com/user-8/proj" and pr.head.is_fork is True


@pytest.mark.parametrize("status", [404, 403])
def test_a_deleted_or_hidden_head_project_is_null_repo(
    fake: FakeGitLab, tmp_path: Path, status: int
) -> None:
    def fork(mr: dict) -> None:
        mr["source_project_id"] = 202

    standard(fake)
    mr = recording("mr")
    fork(mr)
    fake.on(mr_path(), mr)
    fake.on("projects/202", {"message": "x"}, status=status)
    head = make_backend(fake, tmp_path).get_pr(ref()).pr.head
    assert head.repo is None and head.is_fork is True  # a differing project id proves a fork


def test_a_null_source_project_is_a_deleted_head_project(fake: FakeGitLab, tmp_path: Path) -> None:
    def gone(mr: dict) -> None:
        mr["source_project_id"] = None

    head = show(fake, tmp_path, gone).pr.head
    assert head.repo is None and head.is_fork is None and head.sha == HEAD


# -- bots -------------------------------------------------------------------------------------


def test_a_bot_field_on_the_author_is_used_without_a_lookup(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    def bot(mr: dict) -> None:
        mr["author"]["bot"] = True

    read = show(fake, tmp_path, bot)
    assert read.pr.author.is_bot is True
    assert fake.count("users/11") == 0


def test_a_missing_bot_field_falls_back_to_the_user_read_once_per_process(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend, _ = setup(fake, tmp_path)
    assert backend.get_pr(ref()).pr.author.is_bot is False
    assert backend.get_pr(ref()).pr.author.is_bot is False
    assert fake.count("users/11") == 1


def test_a_user_read_that_says_bot_true(fake: FakeGitLab, tmp_path: Path) -> None:
    user = recording("user")
    user["bot"] = True
    assert show(fake, tmp_path, user=user).pr.author.is_bot is True


@pytest.mark.parametrize(
    ("username", "user_body", "expected"),
    [
        ("project_5_bot_ab12cd", {"id": 11}, True),
        ("group_77_bot", {"id": 11}, True),
        ("group_77_bot_0f", {"id": 11}, True),
        ("project_5_robot", {"id": 11}, None),
        ("user-1", {"id": 11}, None),
        ("project_5_bot", {"id": 11, "bot": False}, False),
        ("user-1", {"id": 11, "bot": "yes"}, None),
        ("project_5_bot_ab12cd", {"id": 12}, True),
    ],
)
def test_bot_name_patterns_and_unsettled_is_null(
    fake: FakeGitLab, tmp_path: Path, username: str, user_body: dict, expected: Any
) -> None:
    def named(mr: dict) -> None:
        mr["author"]["username"] = username

    standard(fake)
    mr = recording("mr")
    named(mr)
    fake.on(mr_path(), mr)
    fake.on("users/11", user_body)
    pr = make_backend(fake, tmp_path).get_pr(ref()).pr
    assert pr.author.is_bot is expected


@pytest.mark.parametrize("status", [404, 403])
def test_an_unreadable_user_is_null_not_a_failure(
    fake: FakeGitLab, tmp_path: Path, status: int
) -> None:
    standard(fake)
    fake.on("users/11", {"message": "x"}, status=status)
    assert make_backend(fake, tmp_path).get_pr(ref()).pr.author.is_bot is None


def test_a_rate_limited_user_read_fails_the_read(fake: FakeGitLab, tmp_path: Path) -> None:
    standard(fake)
    fake.on("users/11", {"message": "slow down"}, status=429)
    with pytest.raises(ForgectlError) as raised:
        make_backend(fake, tmp_path).get_pr(ref())
    assert raised.value.code == "rate_limited"


# -- self-managed hosts ----------------------------------------------------------------------


def test_a_self_managed_host_with_a_port_and_prefix(fake: FakeGitLab, tmp_path: Path) -> None:
    host = "git.example.test:8443"
    data = standard(fake)
    del data
    mr = recording("mr")
    mr["web_url"] = f"https://{host}/gl/user-9/proj/-/merge_requests/7"
    mr["author"]["web_url"] = f"https://{host}/gl/user-1"
    project = recording("project")
    project["web_url"] = f"https://{host}/gl/user-9/proj"
    fake.on(mr_path(), mr)
    fake.on("projects/101", project)
    backend = make_backend(fake, tmp_path, host=host, prefix="/gl")
    read = backend.get_pr(ref(host=host))
    assert read.pr.pr == f"{host}/user-9/proj#7"
    assert read.pr.url == f"https://{host}/gl/user-9/proj/-/merge_requests/7"
    for call in fake.calls:
        assert call.env["GITLAB_HOST"] == f"{host}/gl"
        assert "--hostname" not in call.argv


def test_a_web_url_without_the_prefix_is_wrong_host_response(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    host = "git.example.test:8443"
    standard(fake)
    mr = recording("mr")
    mr["web_url"] = f"https://{host}/user-9/proj/-/merge_requests/7"
    fake.on(mr_path(), mr)
    backend = make_backend(fake, tmp_path, host=host, prefix="/gl")
    with pytest.raises(ForgectlError) as raised:
        backend.get_pr(ref(host=host))
    assert raised.value.code == "wrong_host_response"


def test_a_web_url_on_another_port_is_wrong_host_response(fake: FakeGitLab, tmp_path: Path) -> None:
    host = "git.example.test:8443"
    standard(fake)
    mr = recording("mr")
    mr["web_url"] = "https://git.example.test/user-9/proj/-/merge_requests/7"
    fake.on(mr_path(), mr)
    with pytest.raises(ForgectlError) as raised:
        make_backend(fake, tmp_path, host=host).get_pr(ref(host=host))
    assert raised.value.code == "wrong_host_response"


# -- sanitizing -------------------------------------------------------------------------------


def test_untrusted_text_is_sanitized_per_field(fake: FakeGitLab, tmp_path: Path) -> None:
    def hostile(mr: dict) -> None:
        mr["title"] = "fix‮\nthe\tthing"
        mr["description"] = "a\r\nb\rc\x1b[31m"
        mr["source_branch"] = "x​y\n"
        mr["target_branch"] = "m ain"
        mr["labels"] = ["a\nb", "c‮"]

    pr = show(fake, tmp_path, hostile).pr
    assert pr.title == "fix<U+202E><U+000A>the<U+0009>thing"
    assert pr.body == "a\nb<U+000D>c<U+001B>[31m"
    assert pr.head.ref == "x<U+200B>y<U+000A>"
    assert pr.base.ref == "m<U+2028>ain"
    assert sorted(pr.labels) == ["a<U+000A>b", "c<U+202E>"]


@pytest.mark.parametrize(
    "login",
    [
        "",
        "a b",
        "x\n",
        "-lead",
        "a/b",
        "user[bot]",
        "../x",
        "ü",
        "a" * 300,
        "x;rm",
        "$(id)",
        7,
        None,
    ],
)
def test_a_hostile_author_login_is_a_typed_error(
    fake: FakeGitLab, tmp_path: Path, login: Any
) -> None:
    def hostile(mr: dict) -> None:
        mr["author"]["username"] = login

    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, hostile)
    assert raised.value.code == "upstream_error"
    assert str(login) not in raised.value.message or login in ("", None)


def test_a_hostile_approver_or_merger_login_is_a_typed_error(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    approvals = recording("approvals")
    approvals["approved_by"][0]["user"]["username"] = "x y"
    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, approvals=approvals)
    assert raised.value.code == "upstream_error"

    def merger(mr: dict) -> None:
        merged(mr)
        mr["merge_user"]["username"] = "$(x)"

    with pytest.raises(ForgectlError) as raised:
        show(FakeGitLab(), tmp_path / "b", merger)
    assert raised.value.code == "upstream_error"


def test_an_odd_detailed_status_is_sanitized_into_the_blocker_detail(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    def odd(mr: dict) -> None:
        mr["detailed_merge_status"] = "x‮y" + "z" * 200

    detail = show(fake, tmp_path, odd).pr.blocked_by[0].detail
    assert detail is not None and "‮" not in detail and len(detail) <= 64


# -- hostile answers --------------------------------------------------------------------------


WEIRD = [None, True, False, 0, -1, 2**70, 1.5, "", "x", [], {}, [1], {"a": 1}, "\u0000", "9" * 30]


@pytest.mark.parametrize("key", sorted(recording("mr")))
def test_no_mr_field_of_any_type_gives_a_traceback(
    fake: FakeGitLab, tmp_path: Path, key: str
) -> None:
    for weird in WEIRD:
        mr = recording("mr")
        mr[key] = copy.deepcopy(weird)
        local = FakeGitLab()
        standard(local, mr=mr)
        local.on(mr_path(), mr)
        backend = make_backend(local, tmp_path / f"w{WEIRD.index(weird)}")
        try:
            read = backend.get_pr(ref())
        except ForgectlError:
            continue
        json.dumps(read.pr.to_dict())  # whatever came out is serializable
        assert local.unrouted == [], (key, weird, local.unrouted)


@pytest.mark.parametrize(
    "name",
    [
        "approvals",
        "project",
        "reviewers",
        "statuses",
        "notes",
        "member",
        "user",
        "project_approvals",
    ],
)
def test_no_other_answer_of_any_shape_gives_a_traceback(
    fake: FakeGitLab, tmp_path: Path, name: str
) -> None:
    shapes: list[Any] = [
        None,
        [],
        {},
        "x",
        7,
        True,
        [None],
        [[]],
        ["x"],
        {"a": None},
        [{"user": None}],
    ]
    shapes += [
        {"approved_by": "x"},
        {"approved_by": [None]},
        {"approved_by": [{"user": {"id": True, "username": "a"}}]},
    ]
    for index, shape in enumerate(shapes):
        local = FakeGitLab()
        standard(local)
        local.on(
            {
                "approvals": f"{MR_ID}/approvals",
                "project": "projects/101",
                "reviewers": f"{MR_ID}/reviewers",
                "statuses": STATUSES,
                "notes": f"{MR_ID}/notes",
                "member": "projects/101/members/all/12",
                "user": "users/11",
                "project_approvals": "projects/101/approvals",
            }[name],
            shape,
        )
        local.on("projects/101/approvals", {"reset_approvals_on_push": True}) if name in (
            "notes",
            "member",
        ) else None
        backend = make_backend(local, tmp_path / f"s{index}")
        try:
            read = backend.get_pr(ref())
        except ForgectlError:
            continue
        json.dumps(read.pr.to_dict())


@pytest.mark.parametrize("raw", [b"", b"not json", b"\xff\xfe", b'{"a":1,"a":2}', b"NaN", b"[1,2"])
def test_undecodable_answers_are_typed_errors(fake: FakeGitLab, tmp_path: Path, raw: bytes) -> None:
    fake.on(mr_path(), raw=raw)
    with pytest.raises(ForgectlError) as raised:
        make_backend(fake, tmp_path).get_pr(ref())
    assert raised.value.code == "upstream_error"


def test_a_missing_mr_is_not_found_and_a_missing_part_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    with pytest.raises(ForgectlError) as raised:
        make_backend(fake, tmp_path).get_pr(ref())
    assert raised.value.code == "not_found"
    fake2 = FakeGitLab()
    standard(fake2)
    fake2.on("projects/101", {"message": "404"}, status=404)
    with pytest.raises(ForgectlError) as raised:
        make_backend(fake2, tmp_path / "b").get_pr(ref())
    assert raised.value.code == "upstream_error"  # not "the PR was not found"
    schemas.validate_error_envelope(raised.value.envelope())


@pytest.mark.parametrize("pid", ["101/../x", "1e3", -4, 0, True, 2**70, "x", None, [101]])
def test_a_project_id_from_the_answer_is_checked_before_it_builds_a_path(
    fake: FakeGitLab, tmp_path: Path, pid: Any
) -> None:
    def hostile(mr: dict) -> None:
        mr["project_id"] = pid
        mr["target_project_id"] = pid

    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, hostile)
    assert raised.value.code == "upstream_error"
    assert len(fake.calls) == 1


def test_the_token_never_appears_in_the_output_or_a_warning(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    from .gitlab_support import TOKEN

    warnings: list[str] = []
    standard(fake)
    statuses = recording("statuses")
    statuses[0]["status"] = "brand_new"
    fake.pages(STATUSES, [statuses])
    read = make_backend(fake, tmp_path, warn=warnings.append).get_pr(ref())
    assert TOKEN not in json.dumps(read.pr.to_dict()) + " ".join(warnings)


def test_the_retry_hook_wraps_every_read(fake: FakeGitLab, tmp_path: Path) -> None:
    standard(fake)
    seen: list[int] = []

    def retry(call: Any) -> Any:
        seen.append(1)
        return call()

    make_backend(fake, tmp_path, retry=retry).get_pr(ref())
    assert len(seen) == len(fake.calls)


def test_the_version_is_read_once_and_parsed(fake: FakeGitLab, tmp_path: Path) -> None:
    standard(fake)
    backend = make_backend(fake, tmp_path)
    assert backend.host_version("gitlab.com") == (17, 9)
    assert backend.host_version("gitlab.com") == (17, 9)
    assert fake.count("version") == 1


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"version": "19.5.0-pre"}, (19, 5)),
        ({"version": "15.6"}, (15, 6)),
        ({"version": "v17.1"}, None),
        ({"version": " 17.1"}, None),
        ({"version": "17"}, None),
        ({"version": 17.1}, None),
        ({"version": None}, None),
        ({}, None),
        ([], None),
        ({"version": "0017.0001"}, (17, 1)),
        ({"version": "9" * 5000 + ".1"}, None),
        ({"version": "17." + "9" * 5000}, (17, 999999)),
        ({"version": "١٧.٩"}, None),
    ],
)
def test_the_version_parse_and_an_unreadable_version_is_none(
    fake: FakeGitLab, tmp_path: Path, body: Any, expected: Any
) -> None:
    fake.on("version", body)
    assert make_backend(fake, tmp_path).host_version("gitlab.com") == expected


def test_a_failed_version_read_is_none_for_a_missing_route_and_raises_otherwise(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    assert make_backend(fake, tmp_path).host_version("gitlab.com") is None  # 404
    fake.on("version", {"message": "x"}, status=401)
    with pytest.raises(ForgectlError) as raised:
        make_backend(fake, tmp_path / "b").host_version("gitlab.com")
    assert raised.value.code == "auth_required"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("sha", HEAD + "\n"),
        ("sha", HEAD.upper()),
        ("created_at", "2026-10-01T09:00:00Z\n"),
        ("created_at", "2026-10-01T09:00:00"),
        ("created_at", "٢٠٢٦-10-01T09:00:00Z"),
        ("updated_at", "2026-13-01T09:00:00Z"),
        ("web_url", "https://gitlab.com/user-9/proj/-/merge_requests/7\n"),
        ("web_url", "https://gitlab.com/user-9/proj/-/merge_requests/٧"),
    ],
)
def test_anchored_patterns_refuse_a_trailing_newline_and_non_ascii_digits(
    fake: FakeGitLab, tmp_path: Path, key: str, value: str
) -> None:
    def hostile(mr: dict) -> None:
        mr[key] = value

    with pytest.raises(ForgectlError):
        show(fake, tmp_path, hostile)


@pytest.mark.parametrize(
    "header", ["abc", "1", "0", "-2", "2.5", "٢", "99999999999999999999 ", "02", "+2", "9" * 5000]
)
def test_a_hostile_next_page_header_is_a_typed_error_never_a_loop(
    fake: FakeGitLab, tmp_path: Path, header: str
) -> None:
    from .gitlab_support import Answer

    standard(fake)
    fake.on_handler(
        STATUSES, lambda _q: Answer(200, recording("statuses")[:1], {"x-next-page": header})
    )
    with pytest.raises(ForgectlError) as raised:
        make_backend(fake, tmp_path).get_pr(ref())
    assert raised.value.code == "upstream_error"
    assert fake.count(STATUSES) == 1


def test_a_missing_next_page_header_ends_a_short_page_and_continues_a_full_one(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    from .gitlab_support import Answer

    statuses = recording("statuses")
    full = [dict(statuses[0], id=9000 + i, name=f"job-{i}") for i in range(100)]

    def handler(query: dict[str, str]) -> Answer:
        return Answer(200, full if query["page"] == "1" else statuses[:1])

    standard(fake)
    fake.on_handler(STATUSES, handler)
    read = make_backend(fake, tmp_path).get_pr(ref())
    assert fake.count(STATUSES) == 2 and len(read.ci.checks) == 101


# -- review round 1 ---------------------------------------------------------------------------


BRIDGES = "projects/101/pipelines/5001/bridges"


def pipeline_status(value: Any) -> Any:
    def mutate(mr: dict) -> None:
        if value is _ABSENT:
            del mr["head_pipeline"]["status"]
        else:
            mr["head_pipeline"]["status"] = value

    return mutate


_ABSENT = object()


@pytest.mark.parametrize(
    ("status", "ci_state", "pipeline_check", "warned"),
    [
        ("success", "passing", None, False),
        ("skipped", "passing", None, False),
        ("manual", "passing", None, False),
        ("failed", "failing", "failing", False),
        ("canceled", "failing", "failing", False),
        ("created", "pending", "pending", False),
        ("waiting_for_resource", "pending", "pending", False),
        ("preparing", "pending", "pending", False),
        ("pending", "pending", "pending", False),
        ("running", "pending", "pending", False),
        ("scheduled", "pending", "pending", False),
        ("canceling", "pending", "pending", False),
        ("waiting_for_callback", "pending", "pending", False),
        (None, "pending", "pending", True),
        (_ABSENT, "pending", "pending", True),
        (7, "pending", "pending", True),
        ("Failed", "pending", "pending", True),
        ("garbage", "pending", "pending", True),
    ],
)
def test_the_head_pipeline_status_follows_its_table(
    fake: FakeGitLab,
    tmp_path: Path,
    status: Any,
    ci_state: str,
    pipeline_check: str | None,
    warned: bool,
) -> None:
    mr = recording("mr")
    pipeline_status(status)(mr)
    warnings: list[str] = []
    standard(fake, mr=mr)
    read = make_backend(fake, tmp_path, warn=warnings.append).get_pr(ref())
    assert read.ci.state == ci_state
    found = [c for c in read.ci.checks if c.name == "(pipeline)"]
    if pipeline_check is None:
        assert found == []
    else:
        assert [(c.state, c.counted) for c in found] == [(pipeline_check, True)]
    assert bool(warnings) is warned


def test_an_unknown_pipeline_status_warning_is_capped(fake: FakeGitLab, tmp_path: Path) -> None:
    mr = recording("mr")
    mr["head_pipeline"]["status"] = "x" * 1_000_000
    warnings: list[str] = []
    standard(fake, mr=mr)
    make_backend(fake, tmp_path, warn=warnings.append).get_pr(ref())
    assert warnings and all(len(w) < 400 for w in warnings)


def bridge(name: str, status: Any, *, allow_failure: bool = False, downstream: Any = "") -> dict:
    row: dict[str, Any] = {"id": 8001, "name": name, "allow_failure": allow_failure}
    if downstream == "":
        downstream = {
            "id": 6001,
            "project_id": 303,
            "sha": HEAD,
            "status": status,
            "web_url": "https://gitlab.com/user-9/child/-/pipelines/6001",
        }
    row["downstream_pipeline"] = downstream
    return row


@pytest.mark.parametrize(
    ("row", "ci_state", "check"),
    [
        (bridge("deploy", "running"), "pending", ("pending", True)),
        (bridge("deploy", "failed"), "failing", ("failing", True)),
        (bridge("deploy", "canceled"), "failing", ("failing", True)),
        (bridge("deploy", "success"), "passing", ("passing", True)),
        (bridge("deploy", "manual"), "pending", ("pending", True)),
        (bridge("deploy", "skipped"), "passing", ("skipped", False)),
        (bridge("deploy", "failed", allow_failure=True), "passing", ("failing", False)),
        (bridge("deploy", "running", downstream=None), "pending", ("pending", True)),
        (bridge("deploy", "brand_new"), "pending", ("pending", True)),
    ],
)
def test_child_pipelines_are_checks_named_after_their_bridge(
    fake: FakeGitLab, tmp_path: Path, row: dict, ci_state: str, check: tuple[str, bool]
) -> None:
    read = show(fake, tmp_path, bridges=[row])
    assert read.ci.state == ci_state
    child = {c.name: c for c in read.ci.checks}["(child) deploy"]
    assert (child.state, child.counted) == check
    call = next(c for c in fake.calls if c.path == BRIDGES)
    assert call.query == {"per_page": "100", "page": "1"}


@pytest.mark.parametrize("status", [404, 403, 500, 429])
def test_an_unreadable_bridges_list_makes_ci_incomplete(
    fake: FakeGitLab, tmp_path: Path, status: int
) -> None:
    warnings: list[str] = []
    standard(fake)
    fake.on(BRIDGES, {"message": "x"}, status=status)
    read = make_backend(fake, tmp_path, warn=warnings.append).get_pr(ref())
    assert read.ci.complete is False and read.ci.state == "pending"
    assert any("child" in w for w in warnings)


def test_a_bridges_auth_failure_fails_the_read(fake: FakeGitLab, tmp_path: Path) -> None:
    standard(fake)
    fake.on(BRIDGES, {"message": "x"}, status=401)
    with pytest.raises(ForgectlError) as raised:
        make_backend(fake, tmp_path).get_pr(ref())
    assert raised.value.code == "auth_required"


def test_a_bridges_list_cut_at_the_page_cap_is_incomplete(fake: FakeGitLab, tmp_path: Path) -> None:
    standard(fake)
    fake.pages(BRIDGES, [[bridge("deploy", "success")]] * (gl.MAX_PAGES + 2))
    read = make_backend(fake, tmp_path).get_pr(ref())
    assert read.ci.complete is False and read.ci.state == "pending"
    assert fake.count(BRIDGES) == gl.MAX_PAGES


@pytest.mark.parametrize("identifier", [None, "7003", True, 0, 1.5])
def test_a_status_without_an_integer_id_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path, identifier: Any
) -> None:
    statuses = recording("statuses")
    if identifier is None:
        del statuses[0]["id"]
    else:
        statuses[0]["id"] = identifier
    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, statuses=statuses)
    assert raised.value.code == "upstream_error"


def test_equal_status_ids_keep_the_worse_state(fake: FakeGitLab, tmp_path: Path) -> None:
    statuses = recording("statuses")
    twin_failed = dict(statuses[0], status="failed")
    twin_running = dict(statuses[0], status="running")
    for order in ([statuses[0], twin_failed], [twin_failed, statuses[0]]):
        read = show(FakeGitLab(), tmp_path / str(id(order)), statuses=[*order, *statuses[1:]])
        assert {c.name: c.state for c in read.ci.checks}["build"] == "failing"
    read = show(fake, tmp_path, statuses=[statuses[0], twin_running, *statuses[1:]])
    assert {c.name: c.state for c in read.ci.checks}["build"] == "pending"


@pytest.mark.parametrize("project", [999, True, "101", None])
def test_a_pipeline_of_an_unrelated_project_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path, project: Any
) -> None:
    def mutate(mr: dict) -> None:
        mr["head_pipeline"]["project_id"] = project

    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, mutate)
    assert raised.value.code == "upstream_error"
    assert not any(c.path.endswith(("/statuses", "/bridges")) for c in fake.calls)


def test_a_fork_pipeline_reads_its_bridges_from_the_fork(fake: FakeGitLab, tmp_path: Path) -> None:
    def fork(mr: dict) -> None:
        mr["source_project_id"] = 202
        mr["head_pipeline"]["project_id"] = 202

    show(fake, tmp_path, fork)
    assert fake.count("projects/202/pipelines/5001/bridges") == 1
    assert fake.count(BRIDGES) == 0


@pytest.mark.parametrize("status", [404, 403])
def test_an_unreadable_fork_pipeline_is_incomplete_not_a_failure(
    fake: FakeGitLab, tmp_path: Path, status: int
) -> None:
    def fork(mr: dict) -> None:
        mr["source_project_id"] = 202
        mr["head_pipeline"]["project_id"] = 202

    mr = recording("mr")
    fork(mr)
    warnings: list[str] = []
    standard(fake, mr=mr)
    fake.on(f"projects/202/repository/commits/{HEAD}/statuses", {"message": "x"}, status=status)
    read = make_backend(fake, tmp_path, warn=warnings.append).get_pr(ref())
    assert read.ci.complete is False and read.ci.state != "passing"
    assert warnings


@pytest.mark.parametrize("status", [404, 403])
def test_an_unreadable_target_pipeline_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path, status: int
) -> None:
    standard(fake)
    fake.on(STATUSES, {"message": "x"}, status=status)
    with pytest.raises(ForgectlError) as raised:
        make_backend(fake, tmp_path).get_pr(ref())
    assert raised.value.code == "upstream_error"
    schemas.validate_error_envelope(raised.value.envelope())


@pytest.mark.parametrize(
    ("version", "status", "complete"),
    [
        ({"version": "17.8.4"}, 200, False),
        ({"version": "16.11.0"}, 200, False),
        ({"version": "17.9.0"}, 200, True),
        ({"version": "18.0.0"}, 200, True),
        ({"version": "unknown"}, 200, False),
        ({"message": "x"}, 403, False),
        ({"message": "x"}, 500, False),
        ({"message": "x"}, 404, False),
    ],
)
def test_the_pipeline_filter_needs_gitlab_17_9(
    fake: FakeGitLab, tmp_path: Path, version: dict, status: int, complete: bool
) -> None:
    warnings: list[str] = []
    standard(fake)
    fake.on("version", version, status=status)
    read = make_backend(fake, tmp_path, warn=warnings.append).get_pr(ref())
    assert read.ci.complete is complete
    assert read.ci.state == ("passing" if complete else "pending")
    assert bool(warnings) is not complete
    assert fake.count("version") == 1


def test_a_1mb_job_status_is_capped_in_the_warning(fake: FakeGitLab, tmp_path: Path) -> None:
    statuses = recording("statuses")
    statuses[0]["status"] = "q" * 1_000_000
    warnings: list[str] = []
    standard(fake, statuses=statuses)
    read = make_backend(fake, tmp_path, warn=warnings.append).get_pr(ref())
    assert {c.name: c.state for c in read.ci.checks}["build"] == "pending"
    assert warnings and all(len(w) < 400 for w in warnings)


def test_the_automatic_retarget_note_counts_as_a_retarget(fake: FakeGitLab, tmp_path: Path) -> None:
    approvals = recording("approvals")
    approvals["approved_by"][0]["approved_at"] = "2026-10-03T00:00:00.000Z"
    backend, _ = setup(fake, tmp_path, approvals=approvals)
    reset_on(fake)
    note = {
        "id": 900,
        "system": True,
        "body": "deleted the `a` branch. This merge request now targets the `main` branch",
        "created_at": "2026-10-04T00:00:00.000Z",
    }
    fake.pages(f"{MR_ID}/notes", [[note]])
    assert backend.get_pr(ref()).pr.approvals_at_head == 0
    note["system"] = False  # a human's comment with the same words is no retarget
    assert backend.get_pr(ref()).pr.approvals_at_head == 1


@pytest.mark.parametrize("member_id", [13, True, None, "12"])
def test_a_member_answer_for_another_user_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path, member_id: Any
) -> None:
    backend, _ = setup(fake, tmp_path)
    reset_on(fake)
    member = recording("member")
    if member_id is None:
        del member["id"]
    else:
        member["id"] = member_id
    fake.on("projects/101/members/all/12", member)
    with pytest.raises(ForgectlError) as raised:
        backend.get_pr(ref())
    assert raised.value.code == "upstream_error"


@pytest.mark.parametrize("status", [403, 500])
def test_a_missing_detailed_status_with_a_failed_version_read_is_unsupported(
    fake: FakeGitLab, tmp_path: Path, status: int
) -> None:
    def old(mr: dict) -> None:
        del mr["detailed_merge_status"]

    mr = recording("mr")
    old(mr)
    standard(fake, mr=mr)
    fake.on("version", {"message": "x"}, status=status)
    with pytest.raises(ForgectlError) as raised:
        make_backend(fake, tmp_path).get_pr(ref())
    assert raised.value.code == "unsupported_host_version"
    assert "unreadable" in raised.value.message


@pytest.mark.parametrize("value", [None, "true", 1, [], {}])
def test_a_has_conflicts_that_is_not_a_boolean_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path, value: Any
) -> None:
    def mutate(mr: dict) -> None:
        mr["has_conflicts"] = value

    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, mutate)
    assert raised.value.code == "upstream_error"


def test_an_absent_has_conflicts_is_false(fake: FakeGitLab, tmp_path: Path) -> None:
    def mutate(mr: dict) -> None:
        del mr["has_conflicts"]

    read = show(fake, tmp_path, mutate)
    assert read.pr.mergeable is True and read.pr.blocked_by == ()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(_ABSENT, False), (True, True), (False, False), (None, None), ("yes", None), (1, None)],
)
def test_merge_trains_enabled_absent_is_false_and_unclear_is_null(
    fake: FakeGitLab, tmp_path: Path, value: Any, expected: Any
) -> None:
    project = recording("project")
    if value is _ABSENT:
        del project["merge_trains_enabled"]
    else:
        project["merge_trains_enabled"] = value
    pr = show(fake, tmp_path, project=project).pr
    assert pr.merge_queue is expected
    schemas.validate(pr.to_dict(), "forgectl/cli/show.json")


@pytest.mark.parametrize("value", [None, "yesterday", 7])
def test_a_merged_mr_without_a_usable_merged_at_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path, value: Any
) -> None:
    def mutate(mr: dict) -> None:
        merged(mr)
        mr["merged_at"] = value

    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, mutate)
    assert raised.value.code == "upstream_error"
    assert str(value) not in raised.value.message or value is None


def test_closed_at_is_null_unless_the_mr_is_closed(fake: FakeGitLab, tmp_path: Path) -> None:
    def mutate(mr: dict) -> None:
        mr["closed_at"] = "2026-10-06T08:00:00.000Z"

    pr = show(fake, tmp_path, mutate).pr.to_dict()
    assert pr["closed_at"] is None
    schemas.validate(pr, "forgectl/cli/show.json")

    def merged_with_closed(mr: dict) -> None:
        merged(mr)
        mr["closed_at"] = "2026-10-06T08:00:00.000Z"

    pr = show(FakeGitLab(), tmp_path / "b", merged_with_closed).pr.to_dict()
    assert pr["closed_at"] is None
    schemas.validate(pr, "forgectl/cli/show.json")


@pytest.mark.parametrize("number", [0, -1, 10**10, True, 2**70])
def test_a_number_outside_the_range_is_invalid_pr_before_any_read(
    fake: FakeGitLab, tmp_path: Path, number: Any
) -> None:
    standard(fake)
    with pytest.raises(ForgectlError) as raised:
        make_backend(fake, tmp_path).get_pr(ref(number=number))
    assert raised.value.code == "invalid_pr"
    assert fake.calls == []


def test_a_source_project_answer_for_another_path_is_wrong_host_response(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    def fork(mr: dict) -> None:
        mr["source_project_id"] = 202

    source = recording("source_project")
    source["web_url"] = "https://gitlab.com/elsewhere/proj"
    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, fork, source_project=source)
    assert raised.value.code == "wrong_host_response"


@pytest.mark.parametrize("which", ["project", "source_project"])
def test_a_project_read_over_http_is_refused(fake: FakeGitLab, tmp_path: Path, which: str) -> None:
    def fork(mr: dict) -> None:
        mr["source_project_id"] = 202

    answer = recording(which)
    answer["web_url"] = answer["web_url"].replace("https://", "http://")
    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, fork, **{which: answer})
    assert raised.value.code == "upstream_error"


def test_a_token_echoed_by_the_host_is_redacted_in_every_field(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    from .gitlab_support import TOKEN

    def hostile(mr: dict) -> None:
        mr["title"] = f"title {TOKEN}"
        mr["description"] = f"body {TOKEN}\nmore"
        mr["source_branch"] = f"b-{TOKEN}"
        mr["target_branch"] = f"t-{TOKEN}"
        mr["labels"] = [f"l-{TOKEN}"]
        mr["detailed_merge_status"] = f"s-{TOKEN}"

    statuses = recording("statuses")
    statuses[0]["name"] = f"job {TOKEN}"
    statuses[1]["target_url"] = f"https://gitlab.com/x?t={TOKEN}"
    statuses[2]["status"] = f"z-{TOKEN}"
    bridges = [bridge(f"child {TOKEN}", "success")]
    bridges[0]["downstream_pipeline"]["web_url"] = f"https://gitlab.com/c?t={TOKEN}"
    warnings: list[str] = []
    mr = recording("mr")
    hostile(mr)
    standard(fake, mr=mr, statuses=statuses, bridges=bridges)
    read = make_backend(fake, tmp_path, warn=warnings.append).get_pr(ref())
    text = json.dumps(read.pr.to_dict()) + json.dumps(read.ci.to_dict()) + " ".join(warnings)
    assert TOKEN not in text
    assert "[redacted]" in read.pr.title and "[redacted]" in (read.pr.body or "")
    assert any("[redacted]" in b.detail for b in read.pr.blocked_by if b.detail)


def test_wrong_host_response_shows_the_observed_url_without_the_token(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    from .gitlab_support import TOKEN

    project = recording("project")
    project["id"] = 999
    project["web_url"] = f"https://gitlab.com/user-9/proj?t={TOKEN}"
    with pytest.raises(ForgectlError) as raised:
        show(fake, tmp_path, project=project)
    error = raised.value
    assert error.code == "wrong_host_response"
    observed = (error.details or {})["observed"]
    assert observed and TOKEN not in observed
    schemas.validate_error_envelope(error.envelope())

    def prefixless(mr: dict) -> None:
        mr["iid"] = 8

    with pytest.raises(ForgectlError) as raised:
        show(FakeGitLab(), tmp_path / "b", prefixless)
    assert raised.value.code == "wrong_host_response"
    assert (raised.value.details or {})["observed"]
