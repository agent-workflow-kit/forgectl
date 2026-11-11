"""The `ready` and `attention` query fields and sort keys (pure: no widgets)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from forgectl.tui import columns, filtering, query
from forgectl.tui.columns import LOADED, RowState, Summary
from forgectl.tui.query import ParseError, Query, parse
from forgectl.tui.source import Row

from .tui_fakes import make_item

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
GH = "github.com/acme/widgets"


def ok(text: str) -> Query:
    result = parse(text)
    assert isinstance(result, Query), f"{text!r}: {result}"
    return result


def ready_summary(**changes: Any) -> Summary:
    base: dict[str, Any] = {
        "ci_state": "passing",
        "review_decision": "approved",
        "mergeable": True,
        "blocked_count": 0,
        "complete": True,
    }
    base.update(changes)
    return Summary(**base)


def loaded(**changes: Any) -> RowState:
    return RowState(LOADED, ready_summary(**changes))


def mkrow(n: int, **item: Any) -> Row:
    return Row(make_item(n, repo=GH, **item), {"authored"})


def get(state: RowState, name: str, n: int = 1) -> Any:
    return filtering.getter(mkrow(n), state, NOW)(name)


# -- summarize ---------------------------------------------------------------------------------


def test_summarize_carries_mergeable_and_the_blocker_count() -> None:
    s = columns.summarize({"mergeable": True, "blocked_by": [{"code": "a"}, {"code": "b"}]})
    assert (s.mergeable, s.blocked_count, s.blocker) == (True, 2, "a")
    s = columns.summarize({"mergeable": None, "blocked_by": []})
    assert (s.mergeable, s.blocked_count) == (None, 0)
    s = columns.summarize({"mergeable": "yes", "blocked_by": "x"})  # wrong types are unknown
    assert (s.mergeable, s.blocked_count) == (None, None)
    s = columns.summarize({})
    assert (s.mergeable, s.blocked_count) == (None, None)


def test_summarize_counts_each_malformed_blocker_entry_as_one() -> None:
    s = columns.summarize({"mergeable": True, "blocked_by": [1, {}]})
    assert s.blocked_count == 2
    s = ready_summary(blocked_count=s.blocked_count)
    assert columns.is_ready(s) is False


# -- ready -------------------------------------------------------------------------------------


def test_ready_is_true_only_when_every_input_is_reported_and_ready() -> None:
    assert columns.is_ready(ready_summary()) is True
    assert columns.is_ready(ready_summary(review_decision="none")) is True


@pytest.mark.parametrize(
    "changes",
    [
        {"blocked_count": 1},
        {"mergeable": False},
        {"ci_state": "failing"},
        {"ci_state": "pending"},
        {"review_decision": "changes_requested"},
        {"review_decision": "review_required"},
    ],
)
def test_ready_is_false_when_a_reported_input_rules_it_out(changes: dict[str, Any]) -> None:
    assert columns.is_ready(ready_summary(**changes)) is False
    # an incomplete snapshot still knows what it did report
    assert columns.is_ready(ready_summary(complete=False, **changes)) is False


@pytest.mark.parametrize(
    "changes",
    [
        {"mergeable": None},
        {"ci_state": None},
        {"ci_state": "bogus"},
        {"review_decision": None},
        {"review_decision": "bogus"},
        {"blocked_count": None},
        {"complete": False},
    ],
)
def test_ready_is_unknown_never_true_when_an_input_is_missing(changes: dict[str, Any]) -> None:
    assert columns.is_ready(ready_summary(**changes)) is None


def test_ready_ci_none_is_false_only_on_a_complete_snapshot() -> None:
    # as the ``ci`` field: a "none" from an incomplete snapshot is unknown, not "no CI"
    assert columns.is_ready(ready_summary(ci_state="none")) is False
    assert columns.is_ready(ready_summary(ci_state="none", complete=False)) is None


def test_ready_is_unknown_without_a_loaded_snapshot() -> None:
    for state in (
        RowState(),
        RowState(columns.FAILED, error="x"),
        RowState(columns.LOADING, ready_summary()),
    ):
        assert get(state, "ready") is None


def test_ready_in_filters() -> None:
    f = filtering.matches
    row = mkrow(1)
    yes, no, unknown = loaded(), loaded(ci_state="failing"), RowState()
    assert f(ok("ready").where, row, yes, NOW)
    assert not f(ok("ready").where, row, no, NOW)
    assert not f(ok("ready").where, row, unknown, NOW)
    assert f(ok("NOT ready").where, row, no, NOW)
    assert not f(ok("NOT ready").where, row, unknown, NOW)  # unknown stays unknown
    assert f(ok("ready IS NULL").where, row, unknown, NOW)
    assert f(ok("ready IS NULL").where, row, loaded(complete=False), NOW)
    assert not f(ok("ready IS NULL").where, row, yes, NOW)
    assert f(ok("ready = true").where, row, yes, NOW)


def test_ready_desc_puts_ready_first_and_unknown_last_in_both_directions() -> None:
    rows = [mkrow(n) for n in range(1, 5)]
    states = {
        rows[0].pr: loaded(ci_state="failing"),  # false
        rows[1].pr: RowState(),  # unknown
        rows[2].pr: loaded(),  # true
        rows[3].pr: loaded(complete=False),  # unknown
    }

    def order(text: str) -> list[int]:
        sort = filtering.sort_of(ok(text).order)
        return [columns.number_of(r) for r in columns.order(rows, states, "repo", sort)]

    assert order("ORDER BY ready DESC") == [3, 1, 2, 4]  # true, false, then unknown by PR
    assert order("ORDER BY ready") == [1, 3, 2, 4]  # false, true, then unknown by PR


# -- attention ---------------------------------------------------------------------------------

TIERS = [
    (4, {"ci_state": "failing", "others_since_push": 2, "review_decision": "changes_requested"}),
    (3, {"others_since_push": 1, "awaiting_my_reply": 1, "review_decision": "changes_requested"}),
    (2, {"awaiting_my_reply": 1, "review_decision": "changes_requested"}),
    (1, {"review_decision": "changes_requested"}),
    (0, {"review_decision": "approved"}),
]


@pytest.mark.parametrize(("level", "changes"), TIERS)
def test_each_attention_tier(level: int, changes: dict[str, Any]) -> None:
    state = loaded(**changes)
    assert columns.attention_level(state) == level
    assert columns.attention_rank(state) == 4 - level
    assert get(state, "attention") == level


def test_unresolved_threads_alone_do_not_raise_attention() -> None:
    state = RowState(LOADED, ready_summary(unresolved_threads=5, comments_total=9))
    assert columns.attention_level(state) == 0


def test_a_row_without_a_loaded_snapshot_has_attention_zero_not_unknown() -> None:
    for state in (RowState(), RowState(columns.FAILED, error="x")):
        assert get(state, "attention") == 0
    row = mkrow(1)
    assert not filtering.matches(ok("attention > 0").where, row, RowState(), NOW)
    assert filtering.matches(ok("attention = 0").where, row, RowState(), NOW)


def test_attention_in_filters() -> None:
    row = mkrow(1)
    failing = loaded(ci_state="failing")
    assert filtering.matches(ok("attention > 0").where, row, failing, NOW)
    assert filtering.matches(ok("attention >= 4").where, row, failing, NOW)
    assert not filtering.matches(ok("attention > 0").where, row, loaded(), NOW)
    assert filtering.matches(ok("attention").where, row, failing, NOW)  # bare: non-zero
    assert not filtering.matches(ok("attention IS NULL").where, row, failing, NOW)


def test_the_preset_equals_order_by_attention_desc_with_its_tie_breaks() -> None:
    rows = [
        mkrow(1, updated_at="2026-11-03T09:00:00Z"),
        mkrow(2, updated_at="2026-11-03T11:00:00Z"),
        mkrow(3, updated_at="2026-11-01T11:00:00Z"),
        mkrow(4, updated_at="2026-11-02T11:00:00Z"),
        mkrow(5, updated_at="2026-11-03T10:00:00Z"),
        mkrow(6, updated_at="2026-11-03T08:00:00Z"),
    ]
    states = {
        rows[0].pr: loaded(review_decision="changes_requested"),
        rows[1].pr: loaded(),
        rows[2].pr: loaded(ci_state="failing"),
        rows[3].pr: loaded(awaiting_my_reply=1),
        rows[4].pr: RowState(),
        rows[5].pr: loaded(others_since_push=1),
    }
    preset = columns.order(rows, states, columns.SORT_ATTENTION)
    sort = filtering.sort_of(ok("ORDER BY attention DESC").order)
    by_query = columns.order(rows, states, columns.SORT_REPO, sort)

    def level(r: Row) -> int:
        return columns.attention_level(states[r.pr])

    # the same grouping, as one definition
    assert [level(r) for r in preset] == [level(r) for r in by_query] == [4, 3, 2, 1, 0, 0]
    # within a level the preset breaks ties by repo and newest update; the query by the PR
    assert [r.pr for r in preset[4:]] == [rows[1].pr, rows[4].pr]  # newest update first
    assert [r.pr for r in by_query[4:]] == sorted(r.pr for r in (rows[1], rows[4]))


def test_attention_sorts_in_both_directions() -> None:
    rows = [mkrow(n) for n in range(1, 4)]
    states = {
        rows[0].pr: loaded(),
        rows[1].pr: loaded(ci_state="failing"),
        rows[2].pr: loaded(awaiting_my_reply=1),
    }

    def order(text: str) -> list[int]:
        sort = filtering.sort_of(ok(text).order)
        return [columns.number_of(r) for r in columns.order(rows, states, "repo", sort)]

    assert order("ORDER BY attention DESC") == [2, 3, 1]
    assert order("ORDER BY attention") == [1, 3, 2]


def test_the_issue_example_parses_and_orders_ready_before_attention() -> None:
    q = ok("authored AND state = open ORDER BY ready DESC, attention DESC")
    assert q.order == (("ready", True), ("attention", True))
    rows = [mkrow(n) for n in range(1, 4)]
    states = {
        rows[0].pr: loaded(ci_state="failing"),
        rows[1].pr: loaded(),
        rows[2].pr: loaded(review_decision="changes_requested"),
    }
    sort = filtering.sort_of(q.order)
    got = [columns.number_of(r) for r in columns.order(rows, states, "repo", sort)]
    assert got == [2, 1, 3]  # ready; then failing CI (4); then changes requested (1)


# -- the names ---------------------------------------------------------------------------------


def test_order_by_errors_and_the_field_list_name_the_new_keys() -> None:
    err = parse("ORDER BY bogus")
    assert isinstance(err, ParseError)
    assert "ready" in err.message and "attention" in err.message
    err = parse("bogus = 1")
    assert isinstance(err, ParseError)
    assert "ready" in err.message and "attention" in err.message


def test_max_order_is_unchanged_and_counts_the_new_keys() -> None:
    assert query.MAX_ORDER == 8
    assert isinstance(
        parse("ORDER BY ready, attention, ci, age, pr, title, author, roles, badge"), ParseError
    )
    assert isinstance(parse("ORDER BY ready, attention, ci, age, pr, title, author, roles"), Query)


def test_the_new_keys_are_not_header_columns() -> None:
    assert "ready" not in columns.SORTABLE and "attention" not in columns.SORTABLE
    assert columns.toggle_sort((), "ready") == ()


@pytest.mark.parametrize(
    "text",
    [
        "ready ORDER BY ready, ready",  # a key twice
        "attention > 'x'",
        "attention LIKE 'a*'",
        "ready = 'x'",
        "ready > 1",
        "ORDER BY ready sideways",
        "attention ORDER BY \x00ready",
    ],
)
def test_hostile_uses_are_parse_errors(text: str) -> None:
    assert isinstance(parse(text), ParseError)


def test_unknown_count_counts_an_unloaded_row_for_ready_but_not_for_attention() -> None:
    rows = [mkrow(n) for n in (1, 2, 3)]
    states = {
        rows[0].pr: loaded(),
        rows[1].pr: loaded(complete=False),  # loaded, ready unknown: its answer, not hidden
        # rows[2] never started
    }
    count = filtering.unknown_count
    assert count(ok("ready").where, rows, states, NOW) == 1
    assert count(ok("NOT ready").where, rows, states, NOW) == 1
    assert count(ok("attention > 0").where, rows, states, NOW) == 0
    loaded_row = rows[1]
    assert not filtering.matches(ok("ready").where, loaded_row, states[loaded_row.pr], NOW)
    assert not filtering.matches(ok("NOT ready").where, loaded_row, states[loaded_row.pr], NOW)
    assert filtering.matches(ok("ready IS NULL").where, loaded_row, states[loaded_row.pr], NOW)


def test_a_loading_state_with_a_stale_failing_summary_has_attention_zero() -> None:
    state = RowState(columns.LOADING, ready_summary(ci_state="failing"))
    assert get(state, "attention") == 0
    assert not filtering.matches(ok("attention IS NULL").where, mkrow(1), state, NOW)
    assert not filtering.matches(ok("attention > 0").where, mkrow(1), state, NOW)


@pytest.mark.parametrize("direction", ["ASC", "DESC"])
def test_equal_ready_rows_tie_on_the_pr_in_both_directions(direction: str) -> None:
    rows = [mkrow(n) for n in (3, 1, 2)]
    states = {r.pr: loaded() for r in rows}
    sort = filtering.sort_of(ok(f"ORDER BY ready {direction}").order)
    ordered = columns.order(rows, states, columns.SORT_REPO, sort)
    assert [r.pr for r in ordered] == sorted(r.pr for r in rows)
