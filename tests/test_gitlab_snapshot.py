"""GitLab `snapshot`, `comments` and `reviews` (issue #16), and the head push time (#56).

Answers come from hand-built recordings (`tests/recordings/gitlab`, the `snapshot` scenario adds
discussions, versions and the head commit), served by an in-process `glab` through the real
gateway. Every GitLab behavior these recordings assume is UNVERIFIED until the live suite (#31,
#35) runs; the issue report lists them.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from forgectl.backends import gitlab_snapshot as gs
from forgectl.backends.gitlab import GitLabBackend
from forgectl.errors import ForgectlError

from .gitlab_support import (
    HEAD,
    MR_ID,
    TOKEN,
    Answer,
    FakeGitLab,
    make_backend,
    mr_path,
    recording,
    ref,
    standard,
)
from .harness import golden, schemas

NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
DISCUSSIONS = f"{MR_ID}/discussions"
NOTES = f"{MR_ID}/notes"
VERSIONS = f"{MR_ID}/versions"
COMMIT = f"projects/101/repository/commits/{HEAD}"
REVIEWERS = f"{MR_ID}/reviewers"
APPROVALS = f"{MR_ID}/approvals"
MR_NUMERIC_ID = 90001  # the `id` of the `mr` recording
OWNED = {"noteable_id": MR_NUMERIC_ID, "noteable_type": "MergeRequest"}


REQUESTED = "requested review from "


def snap_rec(name: str) -> Any:
    return recording(name, "snapshot")


@pytest.fixture
def fake() -> FakeGitLab:
    return FakeGitLab()


class Rig:
    def __init__(self, fake: FakeGitLab, tmp_path: Path, **overrides: Any) -> None:
        self.fake = fake
        self.warnings: list[str] = []
        self.data = standard(fake, **overrides)
        # the notes API names each note's merge request (the base recording predates that check)
        fake.pages(NOTES, [[{**note, **OWNED} for note in self.data["notes"]]])
        fake.pages(DISCUSSIONS, [snap_rec("discussions")])
        fake.on(VERSIONS, snap_rec("versions"))
        fake.on(COMMIT, snap_rec("commit"))
        fake.on("user", self.data["user"])  # me, for the `activity` block (user-1, id 11)
        for account in (12, 13):  # the commenters' bot lookups
            fake.on(
                f"users/{account}",
                {**recording("user"), "id": account, "username": f"user-{account - 10}"},
            )
        self.backend: GitLabBackend = make_backend(fake, tmp_path, warn=self.warnings.append)

    def snapshot(self) -> dict[str, Any]:
        return gs.snapshot(self.backend, ref(), now=lambda: NOW).to_dict()

    def comments(self) -> dict[str, Any]:
        return gs.comments(self.backend, ref(), now=lambda: NOW).to_dict()

    def reviews(self) -> dict[str, Any]:
        return gs.reviews(self.backend, ref(), now=lambda: NOW).to_dict()


@pytest.fixture
def rig(fake: FakeGitLab, tmp_path: Path) -> Rig:
    return Rig(fake, tmp_path)


def reset_on(fake: FakeGitLab) -> None:
    fake.on("projects/101/approvals", {"reset_approvals_on_push": True})


def request_note(note_id: int, body: str, at: str, *, system: bool = True) -> dict[str, Any]:
    return {
        "id": note_id,
        "system": system,
        "body": body,
        "created_at": at,
        "updated_at": at,
        **OWNED,
    }


def request_of(out: dict[str, Any], login: str) -> dict[str, Any]:
    return next(r for r in out["review_requests"] if r["reviewer"]["login"] == login)


# -- goldens -------------------------------------------------------------------------------------


def test_golden_snapshot(rig: Rig) -> None:
    golden.assert_golden(
        "gitlab/snapshot-open", rig.snapshot(), schema_id="forgectl/cli/snapshot.json"
    )


def test_golden_comments(rig: Rig) -> None:
    golden.assert_golden(
        "gitlab/comments-open", rig.comments(), schema_id="forgectl/cli/comments.json"
    )


def test_golden_reviews_is_empty_by_decision(rig: Rig) -> None:
    out = rig.reviews()
    assert out["reviews"] == []
    golden.assert_golden("gitlab/reviews-open", out, schema_id="forgectl/cli/reviews.json")


def test_golden_snapshot_without_a_push_time(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    fake.on(VERSIONS, [])
    fake.on(COMMIT, {"message": "404 Not found"}, status=404)
    out = rig.snapshot()
    assert out["head"]["pushed_at"] is None and out["head"]["pushed_at_source"] is None
    golden.assert_golden(
        "gitlab/snapshot-no-push-time", out, schema_id="forgectl/cli/snapshot.json"
    )


def test_the_output_carries_no_token_and_no_unknown_keys(rig: Rig) -> None:
    out = rig.snapshot()
    assert TOKEN not in json.dumps(out)
    schemas.validate_cli_output("snapshot", out)
    assert out["teams_visible"] is True
    assert out["reviews"] == []
    assert out["complete"] is True and out["incomplete"] == []


# -- the reviewer-note wording -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "logins"),
    [
        ("requested review from @user-4", ("user-4",)),
        ("requested review from @user-4 and @user-5", ("user-4", "user-5")),
        ("requested review from @user-4, @user-5 and @user-6", ("user-4", "user-5", "user-6")),
        ("requested review from @user-4, @user-5, and @user-6", ("user-4", "user-5", "user-6")),
        ("requested review from @user-4, @user-5 and removed approval", ("user-4", "user-5")),
        ("requested review from @user-4 and removed review request for @user-5", ("user-4",)),
        ("requested review from @user-8.x_y", ("user-8.x_y",)),
        # GitLab usernames may begin with `.` and end with `-`; one character is a username too
        (REQUESTED + "@user-4-", ("user-4-",)),
        (REQUESTED + "@.user-4", (".user-4",)),
        (REQUESTED + "@user-4- and @user-5", ("user-4-", "user-5")),
        (REQUESTED + "@u", ("u",)),
        (REQUESTED + "@user-4.", ()),
        ("requested review from @user-4!x", ()),
        ("requested review from @", ()),
        ("requested review from a", ()),
        ("Requested review from @user-4", ()),
        (" requested review from @user-4", ()),
        ("removed review request for @user-4", ()),
        ("asked @user-4 to review; requested review from @user-5", ()),
        ("", ()),
    ],
)
def test_requested_note_parsing(body: str, logins: tuple[str, ...]) -> None:
    assert gs.parse_requested(body) == logins


def test_a_huge_note_is_cut_and_parsed_without_blowing_up() -> None:
    body = "requested review from " + ", ".join(f"@user-{i}" for i in range(5000))
    assert len(gs.parse_requested(body)) <= 200


def test_one_and_several_reviewers_take_instance_and_time_from_the_system_note(rig: Rig) -> None:
    out = rig.snapshot()
    assert [r["reviewer"]["login"] for r in out["review_requests"]] == ["user-2", "user-3"]
    for login in ("user-2", "user-3"):
        request = request_of(out, login)
        assert request["instance"] == "801"
        assert request["requested_at"] == "2026-10-02T10:00:00.000Z"
        assert request["reviewer"]["kind"] == "user" and request["code_owner"] is None
    assert rig.warnings == []


def test_a_re_request_gives_the_newer_instance_and_only_for_that_reviewer(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    fake.pages(
        NOTES,
        [
            [
                request_note(850, "requested review from @user-3", "2026-10-04T10:00:00.000Z"),
                request_note(
                    801, "requested review from @user-2 and @user-3", "2026-10-02T10:00:00.000Z"
                ),
            ]
        ],
    )
    out = rig.snapshot()
    assert request_of(out, "user-2")["instance"] == "801"
    assert request_of(out, "user-3")["instance"] == "850"


def test_an_unmatched_wording_gives_a_null_instance_and_a_warning(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    fake.pages(
        NOTES, [[request_note(5, "asked user-2 and user-3 to review", "2026-10-02T10:00:00.000Z")]]
    )
    out = rig.snapshot()
    for request in out["review_requests"]:
        assert request["instance"] is None and request["requested_at"] is None
    assert any("no review request note" in w for w in rig.warnings)
    assert out["incomplete"] == []  # the list was read to its end: null is the answer


def test_a_users_comment_with_the_wording_never_creates_a_request(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    fake.pages(
        NOTES,
        [
            [
                request_note(
                    900,
                    "requested review from @user-2 and @user-3",
                    "2026-10-09T10:00:00.000Z",
                    system=False,
                )
            ]
        ],
    )
    out = rig.snapshot()
    assert all(r["instance"] is None for r in out["review_requests"])


def test_a_note_of_another_merge_request_is_a_wrong_host_answer(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    note = request_note(5, "requested review from @user-2", "2026-10-02T10:00:00.000Z")
    note["noteable_id"] = 424242
    fake.pages(NOTES, [[note]])
    with pytest.raises(ForgectlError) as raised:
        rig.snapshot()
    assert raised.value.code == "wrong_host_response"


def test_the_names_in_a_note_match_whole_and_ignoring_ascii_case(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    fake.pages(
        NOTES,
        [
            [
                request_note(
                    7, "requested review from @" + "USER-2 and @user-30", "2026-10-02T10:00:00.000Z"
                )
            ]
        ],
    )
    out = rig.snapshot()
    assert request_of(out, "user-2")["instance"] == "7"
    assert request_of(out, "user-3")["instance"] is None  # `@user-30` is another account


def test_the_walk_stops_when_every_reviewer_is_found_and_reads_on_otherwise(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    first = [
        request_note(30, "requested review from @user-2 and @user-3", "2026-10-02T10:00:00.000Z")
    ]
    fake.pages(NOTES, [first, [request_note(20, "added 1 commit", "2026-10-01T10:00:00.000Z")]])
    rig.snapshot()
    pages = [
        c.query["page"] for c in fake.calls if c.path == NOTES and c.query.get("sort") == "desc"
    ]
    assert pages == ["1"]  # the retarget walk is not made: approvals do not reset on push here


def test_a_page_out_of_order_keeps_the_walk_going_to_the_end(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    newer = request_note(
        60, "requested review from @user-2 and @user-3", "2026-10-02T10:00:00.000Z"
    )
    newest = request_note(61, "requested review from @user-3", "2026-10-05T10:00:00.000Z")
    fake.pages(NOTES, [[request_note(10, "x", "2026-10-01T00:00:00.000Z"), newer], [newest]])
    out = rig.snapshot()
    assert request_of(out, "user-3")["instance"] == "61"  # found on page 2, newer than page 1's


def test_a_walk_cut_by_the_page_cap_marks_review_requests_incomplete(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    fake.on_handler(
        NOTES,
        lambda q: Answer(
            200,
            [request_note(1, "x", "2026-10-01T00:00:00.000Z")],
            {"x-next-page": str(int(q["page"]) + 1)},
        ),
    )
    out = rig.snapshot()
    assert out["incomplete"] == ["review_requests"] and out["complete"] is False
    assert any("page cap" in w for w in rig.warnings)


def test_no_reviewers_means_no_note_walk(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    fake.on(REVIEWERS, [])
    fake.on(APPROVALS, {**recording("approvals"), "approved_by": [], "approvals_left": 1})
    out = rig.snapshot()
    assert out["review_requests"] == [] and out["latest_by_reviewer"] == []
    assert fake.count(NOTES) == 0


# -- request states and the table ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "state"),
    [
        ("unreviewed", "outstanding"),
        ("review_started", "outstanding"),
        ("approved", "reviewed"),
        ("unapproved", "reviewed"),
        ("reviewed", "reviewed"),
        ("requested_changes", "reviewed"),
    ],
)
def test_reviewer_states_map_to_request_states(
    fake: FakeGitLab, tmp_path: Path, raw: str, state: str
) -> None:
    rig = Rig(fake, tmp_path)
    reviewers = recording("reviewers")
    reviewers[1]["state"] = raw
    fake.on(REVIEWERS, reviewers)
    out = rig.snapshot()
    assert request_of(out, "user-3")["state"] == state
    assert rig.warnings == []


@pytest.mark.parametrize("raw", ["brand_new_state", "", 7, None])
def test_an_unknown_reviewer_state_is_outstanding_with_a_warning(
    fake: FakeGitLab, tmp_path: Path, raw: Any
) -> None:
    rig = Rig(fake, tmp_path)
    reviewers = recording("reviewers")
    reviewers[1]["state"] = raw
    fake.on(REVIEWERS, reviewers)
    out = rig.snapshot()
    assert request_of(out, "user-3")["state"] == "outstanding"
    assert any("not one forgectl knows" in w for w in rig.warnings)


def test_a_draft_review_reads_outstanding_and_a_request_stays_after_a_comment_only_review(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    reviewers = recording("reviewers")
    reviewers[1]["state"] = "review_started"
    fake.on(REVIEWERS, reviewers)
    out = rig.snapshot()
    assert request_of(out, "user-3")["state"] == "outstanding"
    # the comment-only review is a discussion note: no entry in the table, request unchanged
    assert [e["login"] for e in out["latest_by_reviewer"]] == ["user-2"]


def test_the_table_is_the_one_show_computes_with_the_documented_precedence(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    reviewers = recording("reviewers")
    reviewers[0]["state"] = "requested_changes"  # beats approved_by
    reviewers[1]["state"] = "unapproved"
    fake.on(REVIEWERS, reviewers)
    out = rig.snapshot()
    states = {e["login"]: e["state"] for e in out["latest_by_reviewer"]}
    assert states == {"user-2": "changes_requested", "user-3": "dismissed"}
    assert out["review_decision"] == "changes_requested"
    reviews = rig.reviews()
    assert reviews["latest_by_reviewer"] == out["latest_by_reviewer"]


def test_approvals_without_reset_on_push_have_no_head_count_and_no_can_push(rig: Rig) -> None:
    out = rig.reviews()
    assert out["approvals"] == 1 and out["approvals_at_head"] is None
    assert out["latest_by_reviewer"][0]["can_push"] is None
    assert rig.fake.count("projects/101/members/all/12") == 0


def test_approvals_with_reset_on_push_count_at_head_through_the_member_read(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    reset_on(fake)
    out = rig.reviews()
    assert out["approvals_at_head"] == 1
    assert out["latest_by_reviewer"][0]["can_push"] is True
    assert fake.count("projects/101/members/all/12") == 1  # cached across show and the table


@pytest.mark.parametrize("status", [401, 403])
def test_an_unreadable_member_is_a_null_can_push_not_a_failure(
    fake: FakeGitLab, tmp_path: Path, status: int
) -> None:
    rig = Rig(fake, tmp_path)
    reset_on(fake)
    fake.on("projects/101/members/all/12", {"message": "no"}, status=status)
    out = rig.snapshot()
    assert out["latest_by_reviewer"][0]["can_push"] is None
    assert out["approvals"] == 1 and out["approvals_at_head"] == 0


def test_a_retarget_note_newer_than_an_approval_discounts_it(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    approvals = recording("approvals")
    approvals["approved_by"][0]["approved_at"] = "2026-10-03T00:00:00.000Z"
    rig = Rig(fake, tmp_path, approvals=approvals)
    reset_on(fake)
    fake.pages(
        NOTES,
        [[request_note(900, "changed target branch from `a` to `b`", "2026-10-04T00:00:00.000Z")]],
    )
    assert rig.reviews()["approvals_at_head"] == 0


def test_a_failed_retarget_walk_counts_no_approval(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    reset_on(fake)
    fake.on(NOTES, {"message": "x"}, status=502)
    out = rig.reviews()
    assert out["approvals_at_head"] is None


# -- threads -------------------------------------------------------------------------------------


def thread_of(out: dict[str, Any], key: str) -> dict[str, Any]:
    return next(t for t in out["threads"] if t["id"] == f"gl:d:{key * 20}")


def test_threads_map_positions_resolution_and_replies(rig: Rig) -> None:
    out = rig.comments()
    assert out["head_sha"] == HEAD and out["pr"] == "gitlab.com/user-9/proj#7" and out["complete"]
    assert len(out["threads"]) == 4  # the system-only discussion is left out
    general = thread_of(out, "d1")
    assert general["resolvable"] is False and general["resolved"] is False
    assert general["path"] is None and general["line"] is None and general["side"] is None
    assert general["comments"][0]["id"] == "gl:n:901" and general["comments"][0]["reply_to"] is None
    ranged = thread_of(out, "d2")
    assert ranged["resolvable"] is True and ranged["resolved"] is False
    assert ranged["outdated"] is None
    assert (ranged["path"], ranged["line"], ranged["side"], ranged["start_line"]) == (
        "src/widget.py",
        12,
        "right",
        10,
    )
    assert ranged["commit_sha"] == HEAD
    assert [c["reply_to"] for c in ranged["comments"]] == [None, "gl:n:902"]
    assert all(c["minimized"] is False and c["review_id"] is None for c in ranged["comments"])
    old = thread_of(out, "d3")
    assert old["resolved"] is True and (old["line"], old["side"]) == (4, "left")
    file_level = thread_of(out, "d4")
    assert file_level["path"] == "src/widget.py" and file_level["line"] is None
    assert file_level["side"] is None and file_level["resolvable"] is True


def test_a_range_start_that_is_not_before_the_line_is_null(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    docs[1]["notes"][0]["position"]["line_range"]["start"]["new_line"] = 12
    fake.pages(DISCUSSIONS, [docs])
    assert thread_of(rig.comments(), "d2")["start_line"] is None


def test_a_thread_is_resolved_only_when_every_resolvable_note_says_exactly_true(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    for note in docs[1]["notes"]:
        note["resolved"] = True
    fake.pages(DISCUSSIONS, [docs])
    assert thread_of(rig.comments(), "d2")["resolved"] is True
    docs[1]["notes"][1]["resolved"] = "true"
    fake.pages(DISCUSSIONS, [docs])
    assert thread_of(rig.comments(), "d2")["resolved"] is False
    docs[1]["notes"][1]["resolved"] = True
    docs[1]["notes"][0]["resolvable"] = "yes"
    docs[1]["notes"][1]["resolvable"] = "yes"
    fake.pages(DISCUSSIONS, [docs])
    out = thread_of(rig.comments(), "d2")
    assert out["resolvable"] is False and out["resolved"] is False


def test_system_notes_and_drafts_are_left_out_and_the_draft_endpoint_is_never_read(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    extra = copy.deepcopy(docs[1]["notes"][1])
    extra.update(id=950, draft=True, body="my unsent thought")
    system = copy.deepcopy(docs[1]["notes"][1])
    system.update(id=951, system=True, body="resolved all threads")
    docs[1]["notes"].extend([extra, system])
    fake.pages(DISCUSSIONS, [docs])
    out = rig.comments()
    assert [c["id"] for c in thread_of(out, "d2")["comments"]] == ["gl:n:902", "gl:n:903"]
    assert "unsent" not in json.dumps(out)
    assert not any("draft_notes" in c.path for c in fake.calls)


def test_a_discussion_with_no_notes_is_left_out_with_a_warning(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    docs.append({"id": "e5" * 20, "individual_note": False, "notes": []})
    fake.pages(DISCUSSIONS, [docs])
    assert len(rig.comments()["threads"]) == 4
    assert any("no notes" in w for w in rig.warnings)


def test_host_text_is_sanitized_per_field_and_never_becomes_a_key(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    note = docs[0]["notes"][0]
    note["body"] = f"hi \x1b[31mred\x1b[0m \u202e rtl {TOKEN} \x00 end"
    docs[1]["notes"][0]["position"]["new_path"] = "src/\x1b]0;title\x07evil\n.py"
    docs[1]["notes"][0]["position"]["__proto__"] = "x"
    fake.pages(DISCUSSIONS, [docs])
    out = rig.comments()
    text = json.dumps(out, ensure_ascii=False)
    assert "\x1b" not in text and "\x00" not in text and TOKEN not in text
    assert thread_of(out, "d2")["path"] is not None and "\n" not in thread_of(out, "d2")["path"]
    schemas.validate_cli_output("comments", out)


def test_an_empty_oversized_and_unicode_body_all_serialize(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    docs[0]["notes"][0]["body"] = ""
    docs[2]["notes"][0]["body"] = "x" * 200_000
    docs[3]["notes"][0]["body"] = "café \U0001f600 م"
    fake.pages(DISCUSSIONS, [docs])
    out = rig.comments()
    assert thread_of(out, "d1")["comments"][0]["body"] == ""
    assert thread_of(out, "d4")["comments"][0]["body"] == "café \U0001f600 م"
    schemas.validate_cli_output("comments", out)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d[0].__setitem__("id", "not-hex"),
        lambda d: d[0].__setitem__("id", 7),
        lambda d: d[0].__setitem__("notes", "x"),
        lambda d: d[0]["notes"][0].__setitem__("id", "901"),
        lambda d: d[0]["notes"][0].__setitem__("id", True),
        lambda d: d[0]["notes"][0].__setitem__("created_at", "yesterday"),
        lambda d: d[0]["notes"][0].__setitem__("body", None),
        lambda d: d[0]["notes"][0].__setitem__("author", None),
        lambda d: d[0]["notes"][0].__setitem__("author", {"id": 5, "username": "bad name"}),
        lambda d: d[1]["notes"][0].__setitem__("position", "x"),
        lambda d: d.__setitem__(0, "x"),
    ],
)
def test_a_malformed_discussion_is_an_upstream_error(
    fake: FakeGitLab, tmp_path: Path, mutate: Any
) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    mutate(docs)
    fake.pages(DISCUSSIONS, [docs])
    with pytest.raises(ForgectlError) as raised:
        rig.comments()
    assert raised.value.code == "upstream_error"
    schemas.validate_error_envelope(raised.value.envelope())


def test_a_discussion_of_another_merge_request_is_wrong_host(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    docs[0]["notes"][0]["noteable_id"] = 12345
    fake.pages(DISCUSSIONS, [docs])
    with pytest.raises(ForgectlError) as raised:
        rig.comments()
    assert raised.value.code == "wrong_host_response"


def test_a_bad_position_line_is_null_never_one(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    docs[1]["notes"][0]["position"]["new_line"] = 0
    docs[1]["notes"][0]["position"]["old_line"] = -3
    docs[2]["notes"][0]["position"]["old_line"] = True
    fake.pages(DISCUSSIONS, [docs])
    out = rig.comments()
    assert thread_of(out, "d2")["line"] is None and thread_of(out, "d2")["side"] is None
    assert thread_of(out, "d3")["line"] is None


def test_a_discussion_repeated_across_pages_repeats_the_reading_then_fails(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    fake.pages(DISCUSSIONS, [docs, docs[:1]])
    with pytest.raises(ForgectlError) as raised:
        rig.comments()
    assert raised.value.code == "upstream_error"
    assert fake.count(DISCUSSIONS) == 4  # two readings of two pages


def test_discussions_cut_by_the_page_cap_make_threads_incomplete(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    counter = {"n": 0}

    def handler(query: dict[str, str]) -> Answer:
        counter["n"] += 1
        doc = {
            "id": f"{counter['n']:040x}",
            "individual_note": True,
            "notes": [
                {
                    **snap_rec("discussions")[0]["notes"][0],
                    "id": 10_000 + counter["n"],
                }
            ],
        }
        return Answer(200, [doc], {"x-next-page": str(int(query["page"]) + 1)})

    fake.on_handler(DISCUSSIONS, handler)
    comments = rig.comments()
    assert comments["complete"] is False and len(comments["threads"]) == 20
    out = rig.snapshot()
    assert "threads" in out["incomplete"] and out["complete"] is False


def test_a_next_page_header_that_skips_ahead_is_refused(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    fake.on(DISCUSSIONS, snap_rec("discussions"), headers={"x-next-page": "5"})
    with pytest.raises(ForgectlError) as raised:
        rig.comments()
    assert raised.value.code == "upstream_error"


def test_comments_are_ordered_and_stable_between_two_readings(rig: Rig) -> None:
    first = rig.snapshot()
    second = rig.snapshot()
    from forgectl.model import comparable

    assert comparable(first) == comparable(second)
    created = [t["comments"][0]["created_at"] for t in first["threads"]]
    assert created == sorted(created)


# -- the push time -------------------------------------------------------------------------------


def push_of(out: dict[str, Any]) -> tuple[Any, Any]:
    return out["head"]["pushed_at"], out["head"]["pushed_at_source"]


def test_the_newest_version_for_the_head_is_the_push_event(rig: Rig) -> None:
    assert push_of(rig.snapshot()) == ("2026-10-02T11:35:00Z", "push_event")


def test_several_versions_report_the_newest(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    versions = snap_rec("versions")
    extra = copy.deepcopy(versions[0])
    extra.update(id=303, created_at="2026-10-02T13:00:00+02:00")  # 11:00Z, older than 11:35Z
    fake.on(VERSIONS, [extra, *versions])
    assert push_of(rig.snapshot())[0] == "2026-10-02T11:35:00Z"
    extra["created_at"] = "2026-10-02T14:00:00+02:00"  # 12:00Z
    assert push_of(rig.snapshot())[0] == "2026-10-02T12:00:00Z"


def test_a_new_push_gives_a_later_time_on_the_next_snapshot(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    before = datetime.fromisoformat(push_of(rig.snapshot())[0])
    versions = snap_rec("versions")
    versions[0]["created_at"] = "2026-10-02T20:00:00.000Z"
    fake.on(VERSIONS, versions)
    assert datetime.fromisoformat(push_of(rig.snapshot())[0]) > before


@pytest.mark.parametrize("answer", [[], [{"x": 1}]])
def test_no_usable_versions_fall_back_to_the_head_commit_date(
    fake: FakeGitLab, tmp_path: Path, answer: Any
) -> None:
    rig = Rig(fake, tmp_path)
    fake.on(VERSIONS, answer)
    expected = ("2026-10-01T08:59:00Z", "commit_date")
    assert push_of(rig.snapshot()) == expected


def test_versions_of_other_heads_only_fall_back_to_the_commit_date(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    versions = snap_rec("versions")[1:]  # only the old head's version
    fake.on(VERSIONS, versions)
    assert push_of(rig.snapshot()) == ("2026-10-01T08:59:00Z", "commit_date")


@pytest.mark.parametrize("status", [404, 403])
def test_unavailable_versions_fall_back_to_the_commit_date(
    fake: FakeGitLab, tmp_path: Path, status: int
) -> None:
    rig = Rig(fake, tmp_path)
    fake.on(VERSIONS, {"message": "x"}, status=status)
    assert push_of(rig.snapshot()) == ("2026-10-01T08:59:00Z", "commit_date")


@pytest.mark.parametrize("path", [VERSIONS, COMMIT])
def test_a_failed_push_time_read_fails_the_snapshot(
    fake: FakeGitLab, tmp_path: Path, path: str
) -> None:
    rig = Rig(fake, tmp_path)
    if path == COMMIT:
        fake.on(VERSIONS, [])
    fake.on(path, {"message": "x"}, status=500)
    with pytest.raises(ForgectlError) as raised:
        rig.snapshot()
    assert raised.value.code == "upstream_error"


def test_a_versions_answer_that_is_not_a_list_warns_and_falls_back(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    fake.on(VERSIONS, {"versions": []})
    assert push_of(rig.snapshot()) == ("2026-10-01T08:59:00Z", "commit_date")
    assert any("not readable" in w for w in rig.warnings)


def test_malformed_versions_warn_and_fall_back(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    versions = snap_rec("versions")
    versions[0]["created_at"] = "soon"
    fake.on(VERSIONS, versions)
    assert push_of(rig.snapshot())[1] == "commit_date"
    assert any("not readable" in w for w in rig.warnings)


def test_a_version_of_another_merge_request_is_wrong_host(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    versions = snap_rec("versions")
    versions[0]["merge_request_id"] = 777
    fake.on(VERSIONS, versions)
    with pytest.raises(ForgectlError) as raised:
        rig.snapshot()
    assert raised.value.code == "wrong_host_response"


def test_neither_source_readable_is_null_null(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    fake.on(VERSIONS, [])
    fake.on(COMMIT, {"id": "b2" * 20, "committed_date": "2026-10-01T08:59:00Z"})  # another commit
    assert push_of(rig.snapshot()) == (None, None)
    assert any("commit date" in w for w in rig.warnings)


@pytest.mark.parametrize("when", ["2026-10-03T12:06:00.000Z", "2999-01-01T00:00:00.000Z"])
def test_a_time_more_than_five_minutes_ahead_is_not_trusted(
    fake: FakeGitLab, tmp_path: Path, when: str
) -> None:
    rig = Rig(fake, tmp_path)
    versions = snap_rec("versions")
    versions[0]["created_at"] = when
    fake.on(VERSIONS, versions)
    assert push_of(rig.snapshot()) == (None, None)
    assert any("future" in w for w in rig.warnings)


def test_a_time_within_the_skew_allowance_is_kept(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    versions = snap_rec("versions")
    versions[0]["created_at"] = "2026-10-03T12:04:00.000Z"
    fake.on(VERSIONS, versions)
    assert push_of(rig.snapshot()) == ("2026-10-03T12:04:00Z", "push_event")


# -- rate limit ----------------------------------------------------------------------------------


def test_rate_limit_takes_the_lowest_remaining_from_the_headers(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    fake.on(
        VERSIONS,
        snap_rec("versions"),
        headers={"ratelimit-remaining": "40", "ratelimit-reset": "1790000000"},
    )
    fake.on(COMMIT, snap_rec("commit"), headers={"ratelimit-remaining": "900"})
    fake.pages(DISCUSSIONS, [snap_rec("discussions")])
    fake.on(
        REVIEWERS,
        recording("reviewers"),
        headers={"ratelimit-remaining": "35", "ratelimit-reset": "1790000100"},
    )
    out = rig.snapshot()
    assert out["rate_limit"] == {
        "host": "gitlab.com",
        "remaining": 35,
        "reset_at": datetime.fromtimestamp(1790000100, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@pytest.mark.parametrize("value", ["-1", "abc", "", "1e3", "99999999999999999999", "٣"])
def test_bad_rate_limit_headers_are_ignored(fake: FakeGitLab, tmp_path: Path, value: str) -> None:
    rig = Rig(fake, tmp_path)
    fake.on(VERSIONS, snap_rec("versions"), headers={"ratelimit-remaining": value})
    out = rig.snapshot()
    assert out["rate_limit"]["remaining"] is None and out["rate_limit"]["reset_at"] is None
    schemas.validate_cli_output("snapshot", out)


# -- consistency and identity --------------------------------------------------------------------


def approvals_sequence(fake: FakeGitLab, answers: list[Any]) -> dict[str, int]:
    state = {"n": 0}

    def handler(_q: dict[str, str]) -> Answer:
        body = answers[min(state["n"], len(answers) - 1)] if len(answers) else None
        state["n"] += 1
        return Answer(200, body)

    fake.on_handler(APPROVALS, handler)
    return state


def test_the_table_is_read_once_and_the_snapshot_uses_show_s(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    """#117: the approvals are no longer read a second time to compare with `show`'s table."""
    rig = Rig(fake, tmp_path)
    base = recording("approvals")
    changed = copy.deepcopy(base)
    changed["approved_by"] = []
    changed["approvals_left"] = 1
    state = approvals_sequence(fake, [base, changed])
    out = rig.snapshot()
    assert state["n"] == 1
    assert [e["login"] for e in out["latest_by_reviewer"]] == ["user-2"]  # the base table
    assert out["approvals"] == 1  # `show`'s reading, not the later `changed` one


def test_reviews_reads_the_table_once_too(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    state = approvals_sequence(fake, [recording("approvals"), {"approved_by": []}])
    out = rig.reviews()
    assert state["n"] == 1
    assert out["latest_by_reviewer"] != []


def mr_sequence(fake: FakeGitLab, docs: list[dict[str, Any]]) -> None:
    state = {"n": 0}

    def handler(_q: dict[str, str]) -> Answer:
        doc = docs[min(state["n"], len(docs) - 1)]
        state["n"] += 1
        return Answer(200, doc)

    fake.on_handler(mr_path(), handler)


def test_a_head_that_moved_during_the_reading_repeats_it_then_fails(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    first = recording("mr")
    moved = copy.deepcopy(first)
    moved["sha"] = "d4" * 20
    moved["head_pipeline"]["sha"] = "d4" * 20
    # show (1), the closing check (2) sees another head: repeat; then again
    mr_sequence(fake, [first, moved, first, moved])
    with pytest.raises(ForgectlError) as raised:
        rig.snapshot()
    assert raised.value.code == "upstream_error" and "changed while" in raised.value.message


def test_an_mr_that_answers_for_another_number_is_wrong_host(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    other = recording("mr")
    other["iid"] = 8
    other["web_url"] = "https://gitlab.com/user-9/proj/-/merge_requests/8"
    fake.on(mr_path(), other)
    with pytest.raises(ForgectlError) as raised:
        rig.comments()
    assert raised.value.code == "wrong_host_response"


def test_a_missing_merge_request_is_not_found(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    fake.on(mr_path(), {"message": "404 Not found"}, status=404)
    with pytest.raises(ForgectlError) as raised:
        rig.comments()
    assert raised.value.code == "not_found"


def test_a_failed_part_fails_the_read(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    fake.on(DISCUSSIONS, {"message": "x"}, status=502)
    with pytest.raises(ForgectlError) as raised:
        rig.snapshot()
    assert raised.value.code == "upstream_error"


def test_every_read_goes_through_the_gateway_with_encoded_paths(rig: Rig) -> None:
    rig.snapshot()
    assert rig.fake.unrouted == []
    for call in rig.fake.calls:
        assert call.argv[0].endswith("glab") or call.argv[0] == "glab"
        assert "\n" not in call.endpoint


def test_the_comments_command_reads_no_ci_and_no_approvals(rig: Rig) -> None:
    rig.comments()
    assert rig.fake.count(APPROVALS) == 0
    assert not any("statuses" in c.path for c in rig.fake.calls)


def test_the_backend_methods_delegate(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    assert rig.backend.snapshot(ref()).pr.number == 7
    assert rig.backend.comments(ref()).head_sha == HEAD
    assert rig.backend.reviews(ref()).reviews == ()


def test_an_mr_that_became_another_object_during_the_reading_is_moved(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    first = recording("mr")
    other = copy.deepcopy(first)
    other["id"] = 90002
    mr_sequence(fake, [first, other, first, other])
    with pytest.raises(ForgectlError) as raised:
        rig.comments()
    assert raised.value.code == "upstream_error" and "changed while" in raised.value.message


# -- parse caps, thread roots, identity and flags ------------------------------------------------


def cut_note(before: str, after: str) -> str:
    """A request note whose scanned part (4096 characters) ends exactly after ``before``;
    ``after`` lies past the cut. Filler logins name nobody the tests know."""
    head = "requested review from "
    parts: list[str] = []

    def length() -> int:
        return len(head + ", ".join([*parts, before]))

    while 4096 - length() > 60:
        parts.append("@f" + "x" * 50)
    parts[-1] += "y" * (4096 - length())
    text = head + ", ".join([*parts, before])
    assert len(text) == 4096
    return text + after


def test_a_login_split_by_the_character_cut_is_never_taken() -> None:
    logins, cut = gs._parse_note(cut_note("@user-2", "x and @user-3"))
    assert "user-2" not in logins and cut is True


def test_the_token_cap_reports_a_cut_only_when_another_login_follows() -> None:
    names = [f"@user-{i}" for i in range(200)]
    exact = "requested review from " + ", ".join(names)
    assert gs._parse_note(exact) == (tuple(n[1:] for n in names), False)
    assert gs._parse_note(exact + " and @user-200")[1] is True
    assert gs._parse_note(exact + " and removed approval")[1] is False


def test_a_login_split_by_the_cut_gives_no_forged_instance(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    split = request_note(900, cut_note("@user-2", "x"), "2026-10-05T10:00:00.000Z")
    fake.pages(NOTES, [[split]])
    out = rig.snapshot()
    assert request_of(out, "user-2")["instance"] != "900"
    assert out["incomplete"] == ["review_requests"]
    assert any("too long to parse whole" in w for w in rig.warnings)


def test_a_reviewer_past_the_cut_never_takes_an_older_notes_instance(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    long_note = request_note(900, cut_note("@user-2", ", @user-3"), "2026-10-05T10:00:00.000Z")
    older = request_note(801, "requested review from @user-2 and @user-3", "2026-10-02T10:00:00Z")
    fake.pages(NOTES, [[long_note, older]])
    out = rig.snapshot()
    assert request_of(out, "user-3")["instance"] is None  # not 801: the walk stopped
    assert out["incomplete"] == ["review_requests"] and out["complete"] is False
    assert any("too long to parse whole" in w for w in rig.warnings)
    assert not any("no review request note matches" in w for w in rig.warnings)


def test_reviewers_found_before_a_cut_note_keep_their_instance(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    newer = request_note(950, "requested review from @user-3", "2026-10-06T10:00:00.000Z")
    long_note = request_note(900, cut_note("@user-9", ", @user-2"), "2026-10-05T10:00:00.000Z")
    fake.pages(NOTES, [[newer, long_note]])
    out = rig.snapshot()
    assert request_of(out, "user-3")["instance"] == "950"
    assert request_of(out, "user-2")["instance"] is None
    assert out["incomplete"] == ["review_requests"]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("2026-10-02T11:05:00.000Z", 10), ("2026-10-02T11:05:00.000Z", 9)),
        (("2026-10-02T11:05:00.500Z", 5), ("2026-10-02T11:05:00Z", 6)),
        (("2026-10-02T11:05:00Z", 6), ("2026-10-02T13:05:00.000+02:00", 5)),
    ],
)
def test_the_root_of_a_thread_is_the_first_comment_emitted(
    fake: FakeGitLab, tmp_path: Path, first: tuple[str, int], second: tuple[str, int]
) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    for note, (at, note_id) in zip(docs[1]["notes"], (first, second), strict=True):
        note.update(id=note_id, created_at=at, updated_at=at)
    fake.pages(DISCUSSIONS, [docs])
    emitted = thread_of(rig.comments(), "d2")["comments"]
    assert emitted[0]["reply_to"] is None
    assert all(c["reply_to"] == emitted[0]["id"] for c in emitted[1:])


def test_a_note_id_in_two_discussions_is_an_upstream_error(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    docs[1]["notes"][1]["id"] = docs[0]["notes"][0]["id"]
    fake.pages(DISCUSSIONS, [docs])
    with pytest.raises(ForgectlError) as raised:
        rig.comments()
    assert raised.value.code == "upstream_error" and "note id" in raised.value.message
    assert fake.count(DISCUSSIONS) == 1  # a malformed answer, not a moving list: no repeat


@pytest.mark.parametrize(
    "mutate",
    [
        lambda n: n.pop("noteable_id"),
        lambda n: n.__setitem__("noteable_id", None),
        lambda n: n.__setitem__("noteable_id", str(MR_NUMERIC_ID)),
        lambda n: n.__setitem__("noteable_id", True),
        lambda n: n.pop("noteable_type"),
        lambda n: n.__setitem__("noteable_type", None),
    ],
)
def test_a_discussion_note_without_a_usable_noteable_is_an_upstream_error(
    fake: FakeGitLab, tmp_path: Path, mutate: Any
) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    mutate(docs[0]["notes"][0])
    fake.pages(DISCUSSIONS, [docs])
    with pytest.raises(ForgectlError) as raised:
        rig.comments()
    assert raised.value.code == "upstream_error"


def test_a_discussion_note_of_another_kind_of_object_is_wrong_host(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    docs[0]["notes"][0]["noteable_type"] = "Issue"
    fake.pages(DISCUSSIONS, [docs])
    with pytest.raises(ForgectlError) as raised:
        rig.comments()
    assert raised.value.code == "wrong_host_response"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda n: n.pop("noteable_id"),
        lambda n: n.__setitem__("noteable_id", "x"),
        lambda n: n.pop("noteable_type"),
    ],
)
def test_a_request_note_without_a_usable_noteable_is_an_upstream_error(
    fake: FakeGitLab, tmp_path: Path, mutate: Any
) -> None:
    rig = Rig(fake, tmp_path)
    note = request_note(5, "requested review from @user-2", "2026-10-02T10:00:00.000Z")
    mutate(note)
    fake.pages(NOTES, [[note]])
    with pytest.raises(ForgectlError) as raised:
        rig.snapshot()
    assert raised.value.code == "upstream_error"


@pytest.mark.parametrize("value", [None, "90001", True, 1.5])
def test_a_version_without_a_usable_merge_request_id_warns_and_falls_back(
    fake: FakeGitLab, tmp_path: Path, value: Any
) -> None:
    rig = Rig(fake, tmp_path)
    versions = snap_rec("versions")
    versions[0]["merge_request_id"] = value
    fake.on(VERSIONS, versions)
    assert push_of(rig.snapshot()) == ("2026-10-01T08:59:00Z", "commit_date")
    assert any("not readable" in w for w in rig.warnings)


def test_a_version_with_no_merge_request_id_warns_and_falls_back(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    versions = snap_rec("versions")
    del versions[0]["merge_request_id"]
    fake.on(VERSIONS, versions)
    assert push_of(rig.snapshot())[1] == "commit_date"


@pytest.mark.parametrize(
    ("flag", "value"), [("system", "true"), ("system", None), ("draft", "false"), ("draft", 1)]
)
def test_a_note_flag_that_is_not_a_bool_is_an_upstream_error(
    fake: FakeGitLab, tmp_path: Path, flag: str, value: Any
) -> None:
    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    docs[1]["notes"][1][flag] = value
    fake.pages(DISCUSSIONS, [docs])
    with pytest.raises(ForgectlError) as raised:
        rig.comments()
    assert raised.value.code == "upstream_error"


def test_a_long_path_of_marked_characters_is_never_cut_inside_a_marker(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    from forgectl.sanitize import sanitize_line

    rig = Rig(fake, tmp_path)
    docs = snap_rec("discussions")
    raw = "\u202e" * 3000
    docs[1]["notes"][0]["position"]["new_path"] = raw
    fake.pages(DISCUSSIONS, [docs])
    out = rig.comments()
    assert thread_of(out, "d2")["path"] == sanitize_line(raw)
    schemas.validate_cli_output("comments", out)


def test_a_version_time_with_no_utc_form_warns_and_falls_back(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    versions = snap_rec("versions")
    versions[0]["created_at"] = "0001-01-01T00:00:00+14:00"
    fake.on(VERSIONS, versions)
    assert push_of(rig.snapshot()) == ("2026-10-01T08:59:00Z", "commit_date")
    assert any("not readable" in w for w in rig.warnings)


def test_a_commit_date_with_no_utc_form_is_null_with_a_warning(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    fake.on(VERSIONS, [])
    fake.on(COMMIT, {**snap_rec("commit"), "committed_date": "0001-01-01T00:00:00+14:00"})
    assert push_of(rig.snapshot()) == (None, None)
    assert any("commit date" in w for w in rig.warnings)


def test_warnings_of_a_discarded_attempt_are_not_written(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    bad = snap_rec("versions")
    bad[0]["created_at"] = "soon"
    answers = iter([bad])

    def versions(_q: dict[str, str]) -> Answer:
        return Answer(200, next(answers, snap_rec("versions")))

    fake.on_handler(VERSIONS, versions)
    first = recording("mr")
    moved = copy.deepcopy(first)
    moved["sha"] = "d4" * 20
    moved["head_pipeline"]["sha"] = "d4" * 20
    mr_sequence(fake, [first, moved, first, first])
    out = rig.snapshot()
    assert push_of(out) == ("2026-10-02T11:35:00Z", "push_event")
    assert fake.count(VERSIONS) == 2
    assert not any("not readable" in w for w in rig.warnings)


def test_warnings_of_the_final_failing_attempt_are_written(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    reviewers = recording("reviewers")
    reviewers[1]["state"] = "brand_new_state"
    fake.on(REVIEWERS, reviewers)
    fake.on(DISCUSSIONS, {"message": "x"}, status=502)
    with pytest.raises(ForgectlError):
        rig.snapshot()
    assert any("not one forgectl knows" in w for w in rig.warnings)


def test_reviews_fails_when_the_head_keeps_moving_at_the_closing_check(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    first = recording("mr")
    moved = copy.deepcopy(first)
    moved["sha"] = "d4" * 20
    moved["head_pipeline"]["sha"] = "d4" * 20
    mr_sequence(fake, [first, moved, first, moved])
    with pytest.raises(ForgectlError) as raised:
        rig.reviews()
    assert raised.value.code == "upstream_error" and "changed while" in raised.value.message


def test_reviews_reads_again_after_one_move_at_the_closing_check(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    first = recording("mr")
    moved = copy.deepcopy(first)
    moved["sha"] = "d4" * 20
    moved["head_pipeline"]["sha"] = "d4" * 20
    mr_sequence(fake, [first, moved, first, first])
    out = rig.reviews()
    assert out["head_sha"] == HEAD and fake.count(mr_path()) == 4


# -- activity (#57) -------------------------------------------------------------------------------
# me is user-1 (id 11). The recorded threads: user-3 (11:00), user-2 then me (11:05, 11:10),
# a resolved one by user-3, and user-2 (11:30); the head was pushed at 11:35Z.


def discussion(
    login_id: tuple[str, int], at: str, *, note_id: int, discussion_id: str
) -> dict[str, Any]:
    login, account = login_id
    return {
        "id": discussion_id,
        "individual_note": True,
        "notes": [
            {
                "id": note_id,
                "type": None,
                "body": "text",
                "author": {"id": account, "username": login, "name": login, "state": "active"},
                "created_at": at,
                "updated_at": at,
                "system": False,
                "resolvable": False,
                **OWNED,
            }
        ],
    }


def test_activity_counts_the_recorded_threads(rig: Rig) -> None:
    assert rig.snapshot()["activity"] == {
        "comments_total": 5,
        "unresolved_threads": 3,
        "others_since_push": 0,  # every recorded comment is older than the push
        "awaiting_my_reply": 2,  # user-3's open thread and user-2's last; I wrote last in another
    }


def test_activity_agrees_with_the_threads_of_the_same_snapshot(rig: Rig) -> None:
    out = rig.snapshot()
    assert out["activity"]["comments_total"] == sum(len(t["comments"]) for t in out["threads"])
    assert out["activity"]["unresolved_threads"] == sum(not t["resolved"] for t in out["threads"])


def with_comments(fake: FakeGitLab, tmp_path: Path, extra: list[dict[str, Any]]) -> Rig:
    rig = Rig(fake, tmp_path)
    fake.pages(DISCUSSIONS, [[*snap_rec("discussions"), *extra]])
    return rig


def test_comments_of_others_after_the_push_count_and_my_own_do_not(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = with_comments(
        fake,
        tmp_path,
        [
            discussion(
                ("user-3", 13), "2026-10-02T11:36:00.000Z", note_id=9001, discussion_id="a" * 40
            ),
            discussion(
                ("user-1", 11), "2026-10-02T11:40:00.000Z", note_id=9002, discussion_id="b" * 40
            ),
            discussion(
                ("user-2", 12), "2026-10-02T11:35:00.000Z", note_id=9003, discussion_id="c" * 40
            ),
        ],
    )
    # only user-3's: mine never counts, and one made at the very instant of the push is not after
    assert rig.snapshot()["activity"]["others_since_push"] == 1


def test_an_ignored_author_is_not_counted_whatever_the_account(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = with_comments(
        fake,
        tmp_path,
        [
            discussion(
                ("user-3", 13), "2026-10-02T11:36:00.000Z", note_id=9001, discussion_id="a" * 40
            )
        ],
    )
    rig.backend = make_backend(fake, tmp_path, ignore_authors=("USER-3",))
    out = rig.snapshot()["activity"]
    assert out["others_since_push"] == 0
    assert out["awaiting_my_reply"] == 1  # user-3's last word is ignored; user-2's still waits


def test_others_since_push_is_null_when_the_push_time_is_unknown(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = with_comments(
        fake,
        tmp_path,
        [
            discussion(
                ("user-3", 13), "2026-10-02T11:36:00.000Z", note_id=9001, discussion_id="a" * 40
            )
        ],
    )
    fake.on(VERSIONS, [])
    fake.on(COMMIT, {"message": "404 Not found"}, status=404)
    out = rig.snapshot()
    assert out["head"]["pushed_at"] is None
    assert out["activity"]["others_since_push"] is None
    assert out["activity"]["comments_total"] == 6  # the other counts do not need a push time


def test_the_commit_date_fallback_is_used_like_a_push_event(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = with_comments(
        fake,
        tmp_path,
        [
            discussion(
                ("user-3", 13), "2026-10-02T11:36:00.000Z", note_id=9001, discussion_id="a" * 40
            )
        ],
    )
    fake.on(VERSIONS, [])
    out = rig.snapshot()
    assert out["head"]["pushed_at_source"] == "commit_date"
    assert out["head"]["pushed_at"] == "2026-10-01T08:59:00Z"  # the commit's own date
    # so the four recorded comments of others and the new one are all after it
    assert out["activity"]["others_since_push"] == 5


def approved_at(fake: FakeGitLab, tmp_path: Path, *, account: int, at: str | None) -> Rig:
    rig = Rig(fake, tmp_path)
    approvals = recording("approvals")
    user = approvals["approved_by"][0]["user"]
    approvals["approved_by"][0] = {
        "user": {**user, "id": account, "username": f"user-{account - 10}"},
        **({} if at is None else {"approved_at": at}),
    }
    fake.on(APPROVALS, approvals)
    fake.on(f"projects/101/members/all/{account}", recording("member"))
    return rig


@pytest.mark.parametrize(
    ("at", "counted"),
    [
        ("2026-10-02T11:36:00.000Z", 1),  # after the push at 11:35Z
        ("2026-10-02T13:36:00.000+02:00", 1),  # the same instant written with an offset
        ("2026-10-02T11:35:00.000Z", 0),  # at the instant of the push is not after it
        ("2026-10-02T11:00:00.000Z", 0),  # before it
        (None, 0),  # GitLab gave no approval time
    ],
)
def test_an_approval_of_another_counts_only_after_the_push(
    fake: FakeGitLab, tmp_path: Path, at: str | None, counted: int
) -> None:
    rig = approved_at(fake, tmp_path, account=12, at=at)
    out = rig.snapshot()
    assert (out["latest_by_reviewer"][0]["at"] is None) == (at is None)
    assert out["activity"]["others_since_push"] == counted


def test_my_own_approval_after_the_push_is_not_activity(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = approved_at(fake, tmp_path, account=11, at="2026-10-02T12:00:00.000Z")
    assert rig.snapshot()["activity"]["others_since_push"] == 0


def test_an_ignored_approver_is_not_counted(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = approved_at(fake, tmp_path, account=12, at="2026-10-02T12:00:00.000Z")
    rig.backend = make_backend(fake, tmp_path, ignore_authors=("user-*",))
    assert rig.snapshot()["activity"]["others_since_push"] == 0


def test_a_failed_read_of_me_fails_the_snapshot(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    fake.on("user", {"message": "401 Unauthorized"}, status=401)
    with pytest.raises(ForgectlError):
        rig.snapshot()


def test_me_is_read_once_for_the_run(rig: Rig) -> None:
    rig.snapshot()
    rig.snapshot()
    assert rig.fake.count("user") == 1


# -- one read per resource (#117) ----------------------------------------------------------------

PROJECT_APPROVALS = "projects/101/approvals"


def test_a_snapshot_reads_each_resource_once(rig: Rig) -> None:
    """Before #117 a snapshot made 21 reads: the MR 3 times, its reviewers 3 times and its
    approvals and the project's approval settings twice each."""
    rig.snapshot()
    calls = rig.fake
    assert calls.count(REVIEWERS) == 1
    assert calls.count(APPROVALS) == 1
    assert calls.count(PROJECT_APPROVALS) == 1
    assert calls.count(NOTES) == 1 and calls.count(DISCUSSIONS) == 1
    # the MR is read to open the snapshot and once more to close it: the closing read is kept to
    # catch a push that landed while the pages were read (the documented freshness check)
    assert calls.count(mr_path()) == 2
    assert len(calls.calls) == 16


def test_reviews_reads_each_resource_once_and_the_mr_twice(rig: Rig) -> None:
    rig.reviews()
    assert rig.fake.count(REVIEWERS) == 1
    assert rig.fake.count(APPROVALS) == 1
    assert rig.fake.count(PROJECT_APPROVALS) == 1
    assert rig.fake.count(mr_path()) == 2


def test_the_readiness_recheck_still_rereads_the_mr(fake: FakeGitLab, tmp_path: Path) -> None:
    """The single-MR GET that waits for `detailed_merge_status` is repeated by the readiness loop:
    that repeat stays, and nothing else is read again because of it."""
    rig = Rig(fake, tmp_path)

    def until_known(fetch: Any, unknown: Any) -> tuple[Any, bool]:
        response = fetch()
        while unknown(response):
            response = fetch()
        return response, False

    rig.backend._until_known = until_known
    computing = recording("mr")
    computing["detailed_merge_status"] = "checking"
    ready = recording("mr")
    mr_sequence(fake, [computing, ready])
    out = rig.snapshot()
    assert out["pr"] == "gitlab.com/user-9/proj#7"
    assert fake.count(REVIEWERS) == 1 and fake.count(APPROVALS) == 1
    assert fake.count(mr_path()) == 3  # computing read, ready read, closing read
