"""The derived `activity` block (#57): the rules of interface.md "Activity", on normalized inputs.

The module knows no host: these tests build threads, reviews and approvals directly, the way
either backend hands them over.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from forgectl import model as m
from forgectl.activity import Viewer, derive, ignored

PUSH = "2026-10-02T08:30:00Z"
ME = Viewer(login="user-1", id="1001")


def author(login: str | None, id_: str | None = None, *, bot: bool | None = False) -> m.Author:
    return m.Author(login=login, id=id_, is_bot=bot)


def comment(n: int, who: m.Author, at: str, *, review_id: str | None = None) -> m.Comment:
    return m.Comment(
        id=f"gh:ic:{n}",
        author=who,
        body="text",
        created_at=at,
        updated_at=at,
        minimized=False,
        reply_to=None,
        review_id=review_id,
    )


def thread(
    n: int, *comments: m.Comment, resolved: bool = False, resolvable: bool = True
) -> m.Thread:
    return m.Thread(
        id=f"gh:rt:{n}",
        resolvable=resolvable or resolved,
        resolved=resolved,
        outdated=False,
        path=None,
        line=None,
        side=None,
        start_line=None,
        commit_sha=None,
        comments=comments,
    )


def review(n: int, who: m.Author, at: str) -> m.Review:
    return m.Review(
        id=f"gh:r:{n}", author=who, state="commented", commit_sha=None, submitted_at=at, body=""
    )


def approval(
    login: str, at: str | None, *, state: m.LatestState = "approved"
) -> m.LatestByReviewer:
    return m.LatestByReviewer(
        login=login, id=None, state=state, commit_sha=None, at=at, can_push=True
    )


def run(
    threads: Sequence[m.Thread] = (),
    reviews: Sequence[m.Review] = (),
    approvals: Sequence[m.LatestByReviewer] = (),
    pushed_at: str | None = PUSH,
    me: Viewer = ME,
    ignore: Sequence[str] = (),
) -> m.Activity:
    return derive(
        threads=threads,
        reviews=reviews,
        approvals=approvals,
        pushed_at=pushed_at,
        me=me,
        ignore_authors=ignore,
    )


OTHER = author("user-3", "1003")
BOT = author("dependabot[bot]", "9001", bot=True)
LATER = "2026-10-02T09:00:00Z"
EARLIER = "2026-10-02T08:00:00Z"


# -- comments_total and unresolved_threads -------------------------------------------------


def test_comments_total_counts_every_comment_including_mine_and_ignored() -> None:
    threads = [
        thread(1, comment(1, OTHER, LATER), comment(2, author("user-1", "1001"), LATER)),
        thread(2, comment(3, BOT, LATER), resolved=True),
    ]
    out = run(threads, ignore=["dependabot[bot]"])
    assert out.comments_total == 3


def test_unresolved_threads_counts_threads_that_are_not_resolved() -> None:
    threads = [
        thread(1, comment(1, OTHER, LATER)),
        thread(2, comment(2, OTHER, LATER), resolved=True),
        thread(3, comment(3, OTHER, LATER), resolvable=False),  # a general comment
    ]
    assert run(threads).unresolved_threads == 2


def test_an_empty_snapshot_has_zero_counts() -> None:
    out = run()
    assert (out.comments_total, out.unresolved_threads) == (0, 0)
    assert (out.others_since_push, out.awaiting_my_reply) == (0, 0)


# -- others_since_push ---------------------------------------------------------------------


def test_a_comment_before_the_push_is_not_counted_and_one_after_it_is() -> None:
    threads = [thread(1, comment(1, OTHER, EARLIER)), thread(2, comment(2, OTHER, LATER))]
    assert run(threads).others_since_push == 1


def test_a_comment_at_exactly_the_push_time_is_not_after_it() -> None:
    assert run([thread(1, comment(1, OTHER, PUSH))]).others_since_push == 0


def test_others_since_push_is_null_when_pushed_at_is_null() -> None:
    out = run([thread(1, comment(1, OTHER, LATER))], pushed_at=None)
    assert out.others_since_push is None
    assert out.awaiting_my_reply == 1  # the other answers do not depend on the push time


def test_times_compare_as_instants_not_as_text() -> None:
    # "…00.250000Z" sorts before "…00Z" as text, yet is later as a time
    threads = [thread(1, comment(1, OTHER, "2026-10-02T08:30:00.250000Z"))]
    assert run(threads, pushed_at="2026-10-02T08:30:00Z").others_since_push == 1
    threads = [thread(1, comment(1, OTHER, "2026-10-02T08:30:00Z"))]
    assert run(threads, pushed_at="2026-10-02T08:30:00.250000Z").others_since_push == 0


def test_offsets_are_converted_before_comparing() -> None:
    # 10:00+02:00 is 08:00Z, before the push; 11:00+02:00 is 09:00Z, after it
    early = [thread(1, comment(1, OTHER, "2026-10-02T10:00:00+02:00"))]
    late = [thread(1, comment(1, OTHER, "2026-10-02T11:00:00+02:00"))]
    assert run(early).others_since_push == 0
    assert run(late).others_since_push == 1


def test_lowercase_t_and_z_are_accepted() -> None:
    assert run([thread(1, comment(1, OTHER, "2026-10-02t09:00:00z"))]).others_since_push == 1


@pytest.mark.parametrize("bad", ["", "yesterday", "2026-10-02T09:00:00", "2026-13-40T00:00:00Z"])
def test_a_time_that_is_not_rfc3339_with_an_offset_is_an_error_not_a_silent_miss(bad: str) -> None:
    with pytest.raises(ValueError, match="time"):
        run([thread(1, comment(1, OTHER, bad))])


def test_my_own_comment_is_never_others() -> None:
    mine = author("user-1", "1001")
    assert run([thread(1, comment(1, mine, LATER))]).others_since_push == 0


def test_my_login_is_matched_case_insensitively_when_an_id_is_missing() -> None:
    mine = author("USER-1", None)
    assert run([thread(1, comment(1, mine, LATER))]).others_since_push == 0


def test_an_id_decides_over_a_login() -> None:
    # same id, another spelling of the login: still me; same login, other id: someone else
    assert run([thread(1, comment(1, author("renamed", "1001"), LATER))]).others_since_push == 0
    assert run([thread(1, comment(1, author("user-1", "7777"), LATER))]).others_since_push == 1


def test_my_own_login_is_excluded_even_when_the_config_ignores_nothing_of_it() -> None:
    mine = author("user-1", "1001")
    assert run([thread(1, comment(1, mine, LATER))], ignore=["other*"]).others_since_push == 0


def test_a_comment_by_a_missing_account_is_someone_else() -> None:
    assert run([thread(1, comment(1, author(None), LATER))]).others_since_push == 1


def test_reviews_count_at_their_submitted_time() -> None:
    reviews = [review(1, OTHER, LATER), review(2, OTHER, EARLIER), review(3, ME_AUTHOR, LATER)]
    assert run(reviews=reviews).others_since_push == 1


ME_AUTHOR = author("user-1", "1001")


def test_comments_and_reviews_add_up() -> None:
    threads = [thread(1, comment(1, OTHER, LATER)), thread(2, comment(2, OTHER, LATER))]
    assert run(threads, reviews=[review(1, OTHER, LATER)]).others_since_push == 3


# -- ignore_authors ------------------------------------------------------------------------


def test_an_ignored_login_is_not_counted_whatever_is_bot_says() -> None:
    for flag in (True, False, None):
        who = author("dependabot[bot]", "9001", bot=flag)
        out = run(
            [thread(1, comment(1, who, LATER))],
            reviews=[review(1, who, LATER)],
            ignore=["dependabot[bot]"],
        )
        assert (out.others_since_push, out.awaiting_my_reply) == (0, 0)


def test_a_bot_that_is_not_listed_is_counted() -> None:
    out = run([thread(1, comment(1, BOT, LATER))])
    assert (out.others_since_push, out.awaiting_my_reply) == (1, 1)


def test_a_glob_ignores_matching_logins() -> None:
    renovate = author("renovate-runner", "9002", bot=False)
    out = run([thread(1, comment(1, renovate, LATER))], ignore=["renovate*"])
    assert out.others_since_push == 0


def test_a_glob_that_does_not_match_ignores_nothing() -> None:
    out = run([thread(1, comment(1, OTHER, LATER))], ignore=["renovate*", "gitlab-bot"])
    assert out.others_since_push == 1


def test_brackets_in_a_pattern_are_literal_not_a_character_class() -> None:
    # as a class, `dependabot[bot]` would match `dependabotb`, and not `dependabot[bot]` itself
    assert ignored("dependabot[bot]", ["dependabot[bot]"])
    assert not ignored("dependabotb", ["dependabot[bot]"])
    assert not ignored("dependabotbot", ["dependabot[bot]"])


def test_question_mark_matches_one_character_and_star_any_run() -> None:
    assert ignored("bot-1", ["bot-?"])
    assert not ignored("bot-12", ["bot-?"])
    assert ignored("bot-12", ["bot-*"])
    assert ignored("bot-", ["bot-*"])


def test_patterns_match_the_whole_login_case_insensitively() -> None:
    assert ignored("Renovate-Bot", ["renovate*"])
    assert ignored("renovate", ["RENOVATE"])
    assert not ignored("my-renovate", ["renovate*"])


def test_regex_metacharacters_in_a_pattern_are_literal() -> None:
    assert not ignored("abc", ["a.c"])
    assert ignored("a.c", ["a.c"])
    assert not ignored("ab", ["(a|b)"])


def test_a_missing_login_is_never_ignored_and_an_empty_list_ignores_nothing() -> None:
    assert not ignored(None, ["*"])
    assert not ignored("anyone", [])


def test_my_account_counts_as_mine_even_if_the_config_lists_it() -> None:
    # I answered last: my reply must not be skipped as "ignored", or an older comment would wait
    threads = [thread(1, comment(1, OTHER, EARLIER), comment(2, ME_AUTHOR, LATER))]
    assert run(threads, ignore=["user-1"]).awaiting_my_reply == 0
    assert run(threads, ignore=["*"]).awaiting_my_reply == 0


# -- awaiting_my_reply ---------------------------------------------------------------------


def test_someone_replying_after_me_leaves_the_thread_awaiting_my_reply() -> None:
    threads = [
        thread(1, comment(1, OTHER, EARLIER), comment(2, ME_AUTHOR, LATER), comment(3, OTHER, PUSH))
    ]
    # comment 3 is dated before comment 2 but sorts by time, so mine is last: not awaiting
    assert run(threads).awaiting_my_reply == 0
    threads = [
        thread(
            1,
            comment(1, OTHER, EARLIER),
            comment(2, ME_AUTHOR, PUSH),
            comment(3, OTHER, LATER),
        )
    ]
    assert run(threads).awaiting_my_reply == 1


def test_a_thread_whose_last_comment_is_mine_is_not_awaiting() -> None:
    threads = [thread(1, comment(1, OTHER, EARLIER), comment(2, ME_AUTHOR, LATER))]
    assert run(threads).awaiting_my_reply == 0


def test_a_resolved_thread_is_never_awaiting() -> None:
    threads = [thread(1, comment(1, OTHER, LATER), resolved=True)]
    assert run(threads).awaiting_my_reply == 0


def test_an_ignored_last_comment_is_skipped_to_the_last_non_ignored_one() -> None:
    threads = [thread(1, comment(1, ME_AUTHOR, EARLIER), comment(2, BOT, LATER))]
    assert run(threads, ignore=["dependabot[bot]"]).awaiting_my_reply == 0
    assert run(threads).awaiting_my_reply == 1  # not listed: the bot counts


def test_a_thread_only_ignored_authors_wrote_is_not_awaiting() -> None:
    threads = [thread(1, comment(1, BOT, LATER))]
    assert run(threads, ignore=["dependabot[bot]"]).awaiting_my_reply == 0


def test_a_general_comment_by_someone_else_awaits_a_reply() -> None:
    threads = [thread(1, comment(1, OTHER, EARLIER), resolvable=False)]
    assert run(threads).awaiting_my_reply == 1


def test_comments_are_ordered_by_time_not_by_position() -> None:
    threads = [thread(1, comment(2, ME_AUTHOR, LATER), comment(1, OTHER, EARLIER))]
    assert run(threads).awaiting_my_reply == 0


def test_a_fractional_second_orders_after_the_whole_second_it_follows() -> None:
    # as text "…05.5Z" < "…05Z"; as time the fraction is later, so my comment is the last word
    mine = comment(1, ME_AUTHOR, "2026-10-02T09:00:05.5Z")
    theirs = comment(2, OTHER, "2026-10-02T09:00:05Z")
    assert run([thread(1, mine, theirs)]).awaiting_my_reply == 0


def test_an_offset_comment_is_ordered_by_its_instant() -> None:
    # 10:00+05:00 is 05:00Z, earlier than 06:00Z although "10…" sorts after "06…"
    theirs = comment(1, OTHER, "2026-10-02T10:00:00+05:00")
    mine = comment(2, ME_AUTHOR, "2026-10-02T06:00:00Z")
    assert run([thread(1, theirs, mine)]).awaiting_my_reply == 0


def test_comments_at_the_same_instant_keep_the_order_the_host_gave() -> None:
    mine = comment(9, ME_AUTHOR, LATER)
    theirs = comment(1, OTHER, LATER)
    assert run([thread(1, mine, theirs)]).awaiting_my_reply == 1
    assert run([thread(1, theirs, mine)]).awaiting_my_reply == 0


@pytest.mark.parametrize("pushed", [PUSH, None])
def test_a_bad_time_on_my_own_comment_is_an_error(pushed: str | None) -> None:
    with pytest.raises(ValueError, match="time"):
        run([thread(1, comment(1, ME_AUTHOR, "garbage"))], pushed_at=pushed)


def test_a_bad_time_on_an_ignored_authors_comment_is_an_error() -> None:
    with pytest.raises(ValueError, match="time"):
        run([thread(1, comment(1, BOT, "garbage"))], ignore=["dependabot[bot]"])


def test_a_bad_time_on_any_comment_is_an_error_when_there_is_no_push_time() -> None:
    with pytest.raises(ValueError, match="time"):
        run([thread(1, comment(1, OTHER, "garbage"))], pushed_at=None)


def test_a_bad_review_time_is_an_error_even_for_my_review_and_without_a_push_time() -> None:
    with pytest.raises(ValueError, match="time"):
        run(reviews=[review(1, ME_AUTHOR, "garbage")], pushed_at=None)


def test_a_bad_approval_time_is_an_error_even_for_me_and_without_a_push_time() -> None:
    with pytest.raises(ValueError, match="time"):
        run(approvals=[approval("user-1", "garbage")], pushed_at=None)


def test_a_fraction_beyond_microseconds_still_counts_as_after() -> None:
    late = [thread(1, comment(1, OTHER, "2026-10-02T08:30:00.0000009Z"))]
    assert run(late, pushed_at="2026-10-02T08:30:00Z").others_since_push == 1


@pytest.mark.parametrize("fraction", ["000000000", "000000000000"])
def test_a_long_fraction_equal_to_the_push_time_is_not_after_it(fraction: str) -> None:
    same = [thread(1, comment(1, OTHER, f"2026-10-02T08:30:00.{fraction}Z"))]
    assert run(same, pushed_at="2026-10-02T08:30:00Z").others_since_push == 0


def test_awaiting_does_not_depend_on_the_push_time() -> None:
    threads = [thread(1, comment(1, OTHER, EARLIER))]
    assert run(threads, pushed_at=LATER).awaiting_my_reply == 1


def test_a_thread_counts_once_however_many_others_wrote() -> None:
    threads = [thread(1, comment(1, OTHER, EARLIER), comment(2, OTHER, LATER))]
    assert run(threads).awaiting_my_reply == 1


# -- approvals (GitLab) --------------------------------------------------------------------


def test_an_approval_by_another_user_after_the_push_counts_once() -> None:
    assert run(approvals=[approval("user-2", LATER)]).others_since_push == 1


def test_an_approval_before_the_push_does_not_count() -> None:
    assert run(approvals=[approval("user-2", EARLIER)]).others_since_push == 0


def test_my_own_approval_does_not_count() -> None:
    assert run(approvals=[approval("User-1", LATER)]).others_since_push == 0


def test_an_ignored_approver_does_not_count() -> None:
    out = run(approvals=[approval("gitlab-bot", LATER)], ignore=["gitlab-bot"])
    assert out.others_since_push == 0


@pytest.mark.parametrize("state", ["changes_requested", "dismissed"])
def test_only_approved_entries_count(state: m.LatestState) -> None:
    assert run(approvals=[approval("user-2", LATER, state=state)]).others_since_push == 0


def test_an_approval_without_a_time_does_not_count() -> None:
    assert run(approvals=[approval("user-2", None)]).others_since_push == 0


def test_an_approval_is_null_with_the_rest_when_there_is_no_push_time() -> None:
    assert run(approvals=[approval("user-2", LATER)], pushed_at=None).others_since_push is None


def test_an_approval_by_an_id_matching_me_is_mine() -> None:
    entry = m.LatestByReviewer(
        login="renamed", id="1001", state="approved", commit_sha=None, at=LATER, can_push=True
    )
    assert run(approvals=[entry]).others_since_push == 0


# -- the serialized block ------------------------------------------------------------------


def test_to_dict_has_the_four_documented_fields() -> None:
    out = run([thread(1, comment(1, OTHER, LATER))])
    assert out.to_dict() == {
        "comments_total": 1,
        "unresolved_threads": 1,
        "others_since_push": 1,
        "awaiting_my_reply": 1,
    }


def test_a_null_push_time_serializes_as_null() -> None:
    assert run(pushed_at=None).to_dict()["others_since_push"] is None


def test_an_absurdly_long_time_fraction_is_a_malformed_time() -> None:
    long = "2026-10-02T08:30:00." + "1" * 5000 + "Z"
    with pytest.raises(ValueError, match="is not an RFC 3339 time with an offset"):
        run([thread(1, comment(1, OTHER, long))], pushed_at=PUSH)
