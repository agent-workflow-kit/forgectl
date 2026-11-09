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
        fake.pages(DISCUSSIONS, [snap_rec("discussions")])
        fake.on(VERSIONS, snap_rec("versions"))
        fake.on(COMMIT, snap_rec("commit"))
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
    return {"id": note_id, "system": system, "body": body, "created_at": at, "updated_at": at}


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
        ("requested review from @a", ("a",)),
        ("requested review from @a and @b", ("a", "b")),
        ("requested review from @a, @b and @c", ("a", "b", "c")),
        ("requested review from @a, @b, and @c", ("a", "b", "c")),
        ("requested review from @a, @b and removed approval", ("a", "b")),
        ("requested review from @a and removed review request for @b", ("a",)),
        ("requested review from @a.b-c_d", ("a.b-c_d",)),
        ("requested review from @a!x", ()),
        ("requested review from @", ()),
        ("requested review from a", ()),
        ("Requested review from @a", ()),
        (" requested review from @a", ()),
        ("removed review request for @a", ()),
        ("asked @a to review; requested review from @b", ()),
        ("", ()),
    ],
)
def test_requested_note_parsing(body: str, logins: tuple[str, ...]) -> None:
    assert gs.parse_requested(body) == logins


def test_a_huge_note_is_cut_and_parsed_without_blowing_up() -> None:
    body = "requested review from " + ", ".join(f"@u{i}" for i in range(5000))
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
                    7, "requested review from @USER-2 and @user-30", "2026-10-02T10:00:00.000Z"
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
    note["body"] = f"hi \x1b[31mred\x1b[0m ‮ rtl {TOKEN} \x00 end"
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


@pytest.mark.parametrize("status", [404, 403, 500])
def test_unavailable_versions_fall_back_to_the_commit_date(
    fake: FakeGitLab, tmp_path: Path, status: int
) -> None:
    rig = Rig(fake, tmp_path)
    fake.on(VERSIONS, {"message": "x"}, status=status)
    assert push_of(rig.snapshot()) == ("2026-10-01T08:59:00Z", "commit_date")


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


def test_a_table_that_changed_between_show_and_the_parts_is_read_again(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    rig = Rig(fake, tmp_path)
    base = recording("approvals")
    changed = copy.deepcopy(base)
    changed["approved_by"] = []
    changed["approvals_left"] = 1
    approvals_sequence(fake, [base, changed])  # show sees one, the parts see another, then stable
    out = rig.snapshot()
    assert out["latest_by_reviewer"] == []
    assert out["approvals"] == 0


def test_a_table_that_never_settles_is_an_upstream_error(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    base = recording("approvals")
    changed = copy.deepcopy(base)
    changed["approved_by"] = []
    approvals_sequence(fake, [base, changed, base, changed])
    with pytest.raises(ForgectlError) as raised:
        rig.snapshot()
    assert raised.value.code == "upstream_error"
    assert "changed while" in raised.value.message
    assert fake.count(mr_path()) >= 2


def test_reviews_repeats_once_on_a_moved_table_too(fake: FakeGitLab, tmp_path: Path) -> None:
    rig = Rig(fake, tmp_path)
    base = recording("approvals")
    changed = copy.deepcopy(base)
    changed["approved_by"] = []
    approvals_sequence(fake, [base, changed, base, changed])
    with pytest.raises(ForgectlError) as raised:
        rig.reviews()
    assert raised.value.code == "upstream_error"


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
    # show (1), the parts' read (2), the closing check (3) sees another head: repeat; then again
    mr_sequence(fake, [first, first, moved, first, first, moved])
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
