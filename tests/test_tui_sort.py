"""`browse` sorting by any column: the sort state and keys (pure), the header, click and cursor.

The unit tests drive ``columns`` with hand-built row states; the pilot tests drive the app and read
the header, the rows and the status bar.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import OptionList

from forgectl.tui import columns
from forgectl.tui.app import BrowseApp
from forgectl.tui.source import Row

from .test_tui_app import content_of, list_prompts, settle, wait_until
from .test_tui_columns import GH, GL, activity, snap
from .tui_fakes import FakeOpener, FakeSource, make_item

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
ASC = columns.SortKey
PR_W = 24


def loaded(**kw: Any) -> columns.RowState:
    return columns.RowState(columns.LOADED, columns.Summary(**kw))


def make_row(n: int, repo: str = GH, **item: Any) -> Row:
    return Row(make_item(n, repo=repo, **item), {"authored"})


def sorted_prs(rows: list[Row], states: dict[str, columns.RowState], *keys: columns.SortKey) -> Any:
    return [r.pr for r in columns.order(rows, states, columns.SORT_REPO, tuple(keys))]


def both_ways(
    rows: list[Row], states: dict[str, columns.RowState], column: str
) -> tuple[list[str], list[str]]:
    return (
        sorted_prs(rows, states, ASC(column)),
        sorted_prs(rows, states, ASC(column, descending=True)),
    )


# -- the sort state ----------------------------------------------------------------------------


def test_toggle_sort_picks_a_column_then_flips_then_switches() -> None:
    first = columns.toggle_sort((), "ci")
    assert first == (ASC("ci"),)
    flipped = columns.toggle_sort(first, "ci")
    assert flipped == (ASC("ci", descending=True),)
    assert columns.toggle_sort(flipped, "ci") == first
    # another column starts ascending and replaces the old key
    assert columns.toggle_sort(flipped, "age") == (ASC("age"),)


def test_the_note_column_is_not_sortable_and_leaves_the_sort_as_it_is() -> None:
    current = (ASC("ci", descending=True),)
    assert "note" not in columns.SORTABLE
    assert columns.toggle_sort(current, "note") == current
    assert columns.toggle_sort(current, "nonsense") == current
    assert set(columns.SORTABLE) == {c.id for c in columns.COLUMNS} - {"note"}


def test_sort_text_names_every_key() -> None:
    assert columns.sort_text(()) == ""
    assert columns.sort_text((ASC("ci", descending=True),)) == "ci desc"
    assert columns.sort_text((ASC("age"), ASC("ci", descending=True))) == "age asc, ci desc"


# -- sorting by parsed values ------------------------------------------------------------------


def test_pr_sorts_by_repo_then_number_as_an_integer() -> None:
    rows = [make_row(10), make_row(9), make_row(2, GL), make_row(100), make_row(1, GL)]
    ascending, descending = both_ways(rows, {}, "pr")
    assert ascending == [f"{GH}#9", f"{GH}#10", f"{GH}#100", f"{GL}#1", f"{GL}#2"]
    assert descending == [f"{GL}#2", f"{GL}#1", f"{GH}#100", f"{GH}#10", f"{GH}#9"]


def test_ci_sorts_by_rank_not_by_glyph_or_name() -> None:
    rows = [make_row(n) for n in (1, 2, 3, 4)]
    states = {
        rows[0].pr: loaded(ci_state="passing"),
        rows[1].pr: loaded(ci_state="none"),
        rows[2].pr: loaded(ci_state="failing"),
        rows[3].pr: loaded(ci_state="pending"),
    }
    ascending, descending = both_ways(rows, states, "ci")
    order = [3, 4, 1, 2]  # failing, pending, passing, none
    assert ascending == [rows[n - 1].pr for n in order]
    assert descending == [rows[n - 1].pr for n in reversed(order)]


def test_review_sorts_by_rank() -> None:
    rows = [make_row(n) for n in (1, 2, 3, 4)]
    states = {
        rows[0].pr: loaded(review_decision="approved"),
        rows[1].pr: loaded(review_decision="none"),
        rows[2].pr: loaded(review_decision="changes_requested"),
        rows[3].pr: loaded(review_decision="review_required"),
    }
    ascending, _ = both_ways(rows, states, "review")
    assert ascending == [rows[n - 1].pr for n in (3, 4, 1, 2)]


def test_approvals_are_integers_and_unknown_is_not_zero() -> None:
    rows = [make_row(n) for n in (1, 2, 3, 4)]
    states = {
        rows[0].pr: loaded(approvals=10),
        rows[1].pr: loaded(approvals=2),
        rows[2].pr: loaded(approvals=None),  # shown as `?`
        rows[3].pr: loaded(approvals=0),
    }
    ascending, descending = both_ways(rows, states, "approvals")
    assert ascending == [rows[n - 1].pr for n in (4, 2, 1, 3)]  # 0, 2, 10 (not "10" < "2"), ?
    assert descending == [rows[n - 1].pr for n in (1, 2, 4, 3)]  # unknown stays last


def test_comments_sort_by_unresolved_then_new_then_awaiting_then_total() -> None:
    rows = [make_row(n) for n in range(1, 6)]
    states = {
        rows[0].pr: loaded(
            comments_total=50, unresolved_threads=1, others_since_push=0, awaiting_my_reply=0
        ),
        rows[1].pr: loaded(
            comments_total=2, unresolved_threads=1, others_since_push=3, awaiting_my_reply=0
        ),
        rows[2].pr: loaded(
            comments_total=9, unresolved_threads=0, others_since_push=9, awaiting_my_reply=9
        ),
        rows[3].pr: loaded(
            comments_total=3, unresolved_threads=1, others_since_push=3, awaiting_my_reply=1
        ),
        rows[4].pr: loaded(),  # nothing reported: unknown
    }
    ascending, descending = both_ways(rows, states, "comments")
    assert ascending == [rows[n - 1].pr for n in (3, 1, 2, 4, 5)]
    assert descending == [rows[n - 1].pr for n in (4, 2, 1, 3, 5)]


def test_age_compares_instants_in_different_units_and_puts_a_null_time_last() -> None:
    rows = [
        make_row(1, updated_at="2026-11-03T09:00:00Z"),  # 3h
        make_row(2, updated_at="2026-09-22T12:00:00Z"),  # 6w
        make_row(3, updated_at="2026-11-01T12:00:00Z"),  # 2d
        make_row(4, updated_at="2026-11-03T11:55:00+00:00"),  # 5m, an offset form
        make_row(5, updated_at="not a time"),
    ]
    ascending, descending = both_ways(rows, {}, "age")
    assert ascending == [rows[n - 1].pr for n in (4, 1, 3, 2, 5)]  # youngest first
    assert descending == [rows[n - 1].pr for n in (2, 3, 1, 4, 5)]


def test_age_uses_the_snapshots_fresher_update_and_breaks_ties_on_the_push() -> None:
    rows = [make_row(1, updated_at="2026-11-01T00:00:00Z"), make_row(2), make_row(3)]
    states = {
        rows[0].pr: loaded(updated_at="2026-11-03T11:00:00Z"),  # fresher than the list's
        rows[1].pr: loaded(updated_at="2026-11-03T11:00:00Z", pushed_at="2026-11-03T10:00:00Z"),
        rows[2].pr: loaded(updated_at="2026-11-03T11:00:00Z", pushed_at="2026-11-03T08:00:00Z"),
    }
    ascending, _ = both_ways(rows, states, "age")
    # same update; the later push is younger; a push that is unknown is after the known ones
    assert ascending == [rows[1].pr, rows[2].pr, rows[0].pr]


def test_an_unknown_push_time_stays_after_known_ones_in_both_directions() -> None:
    rows = [make_row(1), make_row(2), make_row(3)]
    same = "2026-11-03T11:00:00Z"
    states = {
        rows[0].pr: loaded(updated_at=same, pushed_at="2026-11-03T10:00:00Z"),
        rows[1].pr: loaded(updated_at=same, pushed_at=None),
        rows[2].pr: loaded(updated_at=same, pushed_at="2026-11-03T08:00:00Z"),
    }
    ascending, descending = both_ways(rows, states, "age")
    assert ascending == [rows[0].pr, rows[2].pr, rows[1].pr]
    assert descending == [rows[2].pr, rows[0].pr, rows[1].pr]


def test_text_columns_compare_case_folded() -> None:
    rows = [
        make_row(1, title="banana", login="Zed"),
        make_row(2, title="Apple", login="amy"),
        make_row(3, title="cherry", login="Bob"),
        make_row(4, title="[@click=app.quit]x[/]", login="Ünal"),
    ]
    assert sorted_prs(rows, {}, ASC("title")) == [rows[n - 1].pr for n in (4, 2, 1, 3)]
    assert sorted_prs(rows, {}, ASC("author")) == [
        rows[n - 1].pr for n in (2, 3, 1, 4)
    ]  # code point order, no locale


def test_badge_and_roles_sort_as_text() -> None:
    rows = [make_row(1, draft=True), make_row(2), make_row(3)]
    rows[1].roles = {"requested", "authored"}
    rows[2].roles = {"requested"}
    rows[0].roles = {"authored"}
    assert sorted_prs(rows, {}, ASC("badge"))[0] == rows[0].pr  # "draft" before "open"
    assert sorted_prs(rows, {}, ASC("roles")) == [rows[n - 1].pr for n in (1, 2, 3)]


@pytest.mark.parametrize(
    ("column", "known_ascending"),
    [("ci", (0, 1)), ("approvals", (1, 0)), ("review", (0, 1)), ("comments", (1, 0))],
)
def test_rows_without_a_snapshot_sort_last_in_both_directions(
    column: str, known_ascending: tuple[int, int]
) -> None:
    rows = [make_row(n) for n in range(1, 6)]
    states = {
        rows[0].pr: loaded(
            ci_state="failing", approvals=3, review_decision="approved", unresolved_threads=2
        ),
        rows[1].pr: loaded(
            ci_state="passing", approvals=1, review_decision="none", unresolved_threads=0
        ),
        rows[2].pr: columns.RowState(),  # loading
        rows[3].pr: columns.RowState(columns.FAILED, error="upstream_error"),
        # rows[4] has no state at all
    }
    first, second = (rows[i].pr for i in known_ascending)
    unknown = [rows[2].pr, rows[3].pr, rows[4].pr]  # end on pr, ascending in both directions
    ascending, descending = both_ways(rows, states, column)
    assert ascending == [first, second, *unknown]
    assert descending == [second, first, *unknown]


def test_age_sorts_rows_with_null_times_last_in_both_directions() -> None:
    rows = [
        make_row(1, updated_at="2026-11-03T09:00:00Z"),
        make_row(2, updated_at=""),
        make_row(3, updated_at="2026-11-01T09:00:00Z"),
        make_row(4, updated_at="not a time"),
    ]
    ascending, descending = both_ways(rows, {}, "age")
    assert ascending == [rows[n - 1].pr for n in (1, 3, 2, 4)]
    assert descending == [rows[n - 1].pr for n in (3, 1, 2, 4)]


def test_a_loading_row_sorts_by_its_list_data_in_item_columns_and_last_in_snapshot_columns() -> (
    None
):
    rows = [make_row(1, title="bravo"), make_row(2, title="alpha"), make_row(3, title="charlie")]
    states = {
        rows[0].pr: loaded(ci_state="passing"),
        rows[1].pr: columns.RowState(),  # loading
        rows[2].pr: loaded(ci_state="failing"),
    }
    ascending, descending = both_ways(rows, states, "title")
    assert ascending == [rows[n - 1].pr for n in (2, 1, 3)]  # the loading row, by its title
    assert descending == [rows[n - 1].pr for n in (3, 1, 2)]
    ascending, descending = both_ways(rows, states, "ci")
    assert ascending == [rows[2].pr, rows[0].pr, rows[1].pr]  # failing, passing, then loading
    assert descending == [rows[0].pr, rows[2].pr, rows[1].pr]


def test_ties_end_on_the_pr_and_the_sort_is_deterministic() -> None:
    rows = [make_row(n, login="same") for n in (12, 3, 7)]
    assert sorted_prs(rows, {}, ASC("author", descending=True)) == [
        rows[1].pr,
        rows[2].pr,
        rows[0].pr,
    ]
    assert sorted_prs(rows, {}, ASC("author")) == sorted_prs(
        list(reversed(rows)), {}, ASC("author")
    )


def test_a_sort_replaces_the_preset_and_an_empty_sort_keeps_it() -> None:
    rows = [make_row(1, GL), make_row(2, GH)]
    preset = [r.pr for r in columns.order(rows, {}, columns.SORT_REPO)]
    assert preset == [f"{GH}#2", f"{GL}#1"]
    assert [r.pr for r in columns.order(rows, {}, columns.SORT_REPO, ())] == preset
    # a column sort ignores the preset mode
    by_pr_desc = columns.order(rows, {}, columns.SORT_ATTENTION, (ASC("pr", descending=True),))
    assert [r.pr for r in by_pr_desc] == [f"{GL}#1", f"{GH}#2"]


def test_sort_values_change_only_when_the_sorted_cell_changes() -> None:
    row = make_row(1)
    a = loaded(ci_state="failing", approvals=1)
    b = loaded(ci_state="failing", approvals=5)
    assert columns.sort_values((ASC("ci"),), row, a) == columns.sort_values((ASC("ci"),), row, b)
    assert columns.sort_values((ASC("approvals"),), row, a) != columns.sort_values(
        (ASC("approvals"),), row, b
    )
    assert columns.sort_values((ASC("ci"),), row, a) != columns.sort_values(
        (ASC("ci"),), row, columns.RowState()
    )


# -- the header --------------------------------------------------------------------------------


def test_the_active_column_shows_an_arrow_and_the_line_keeps_its_width() -> None:
    plain = columns.header_line(PR_W)
    for column in columns.COLUMNS:
        if column.id == "note":
            continue
        for descending, glyph in ((False, "▲"), (True, "▼")):
            line = columns.header_line(PR_W, sort=(ASC(column.id, descending=descending),))
            assert len(line) == len(plain)
            assert line.count(glyph) == 1
            _, start, width = next(x for x in columns.layout(PR_W) if x[0].id == column.id)
            cell = line[start : start + width]
            assert cell.startswith(column.header + glyph), (column.id, cell)
    assert "▲" not in plain and "▼" not in plain


def test_column_span_agrees_with_column_at_at_every_offset() -> None:
    view = 80
    for offset in (0, 17, 10_000):
        for column in columns.COLUMNS:
            span = columns.column_span(column.id, PR_W, offset, view)
            if span is None:
                assert all(
                    columns.column_at(x, PR_W, offset, view) != column.id for x in range(view)
                )
                continue
            start, end = span
            assert 0 <= start < end <= view
            for x in range(view):
                inside = start <= x < end
                assert (columns.column_at(x, PR_W, offset, view) == column.id) == inside


def test_reveal_offset_scrolls_a_hidden_column_into_view() -> None:
    view = 60
    for column in columns.COLUMNS[columns.PINNED :]:
        for start in (0, 10_000):
            offset = columns.reveal_offset(column.id, PR_W, start, view)
            span = columns.column_span(column.id, PR_W, offset, view)
            assert span is not None and span[0] < span[1]
            assert offset == columns.clamp_offset(offset, PR_W, view)
    # a pinned column never moves the scroll
    assert columns.reveal_offset("pr", PR_W, 33, view) == 33


@pytest.mark.parametrize("width", [8, 14, 20])
def test_reveal_offset_does_not_move_when_no_scrolling_column_fits(width: int) -> None:
    assert columns._scroll_room(PR_W, width) == 0
    for column in columns.COLUMNS[columns.PINNED :]:
        for start in (0, 7):
            assert columns.reveal_offset(column.id, PR_W, start, width) == start


# -- pilot -------------------------------------------------------------------------------------


def mixed_source() -> FakeSource:
    items = [
        make_item(1, repo=GH, title="one", login="zed", updated_at="2026-11-03T09:00:00Z"),
        make_item(2, repo=GL, title="Two", login="amy", updated_at="2026-11-01T09:00:00Z"),
        make_item(10, repo=GH, title="ten", login="bob", updated_at="2026-11-03T11:00:00Z"),
        make_item(9, repo=GH, title="nine", login="Cat", updated_at="2026-09-01T11:00:00Z"),
    ]
    snaps = {
        f"{GH}#1": snap(
            1, ci="failing", approvals=2, decision="approved", updated_at="2026-11-03T09:00:00Z"
        ),
        f"{GL}#2": snap(
            2,
            repo=GL,
            ci="passing",
            approvals=0,
            decision="changes_requested",
            updated_at="2026-11-01T09:00:00Z",
        ),
        f"{GH}#10": snap(
            10,
            ci="pending",
            approvals=7,
            act=activity(unresolved=4),
            updated_at="2026-11-03T11:00:00Z",
        ),
        f"{GH}#9": snap(
            9,
            ci="none",
            approvals=1,
            decision="review_required",
            updated_at="2026-09-01T11:00:00Z",
        ),
    }
    return FakeSource(authored=items, snapshots=snaps)


def run(
    script: Callable[[BrowseApp, Pilot[None]], Awaitable[None]],
    source: FakeSource | None = None,
    *,
    width: int = 160,
    height: int = 20,
    concurrency: int = 4,
) -> BrowseApp:
    app = BrowseApp(source or mixed_source(), FakeOpener(), concurrency=concurrency)

    async def go() -> None:
        async with app.run_test(size=(width, height)) as pilot:
            await settle(app, pilot)
            await script(app, pilot)

    asyncio.run(go())
    return app


def shown(app: BrowseApp) -> list[str]:
    return [p.plain.split()[0] for p in list_prompts(app)]


def header_text(app: BrowseApp) -> str:
    return content_of(app, "#header").plain


def status_bar(app: BrowseApp) -> str:
    return content_of(app, "#bar").plain


async def click_column(app: BrowseApp, pilot: Pilot[None], column: str) -> None:
    """Click the middle of ``column``'s header cell at the current scroll offset."""
    view = app._view_width()
    if columns.column_span(column, app._pr_width, app._hoffset, view) is None:
        app._hoffset = columns.reveal_offset(column, app._pr_width, app._hoffset, view)
        app._redraw()
        await pilot.pause()
    span = columns.column_span(column, app._pr_width, app._hoffset, view)
    assert span is not None, column
    x = (span[0] + span[1] - 1) // 2
    await pilot.click("#header", offset=(x + 2, 0))  # +2: the header's padding
    await pilot.pause()


def test_a_click_on_a_header_cell_sorts_and_a_second_click_flips() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await click_column(app, pilot, "ci")
        assert shown(app) == [f"{GH}#1", f"{GH}#10", f"{GL}#2", f"{GH}#9"]
        assert "CI▲" in header_text(app) and "▼" not in header_text(app)
        assert "order: ci asc" in status_bar(app)
        await click_column(app, pilot, "ci")
        assert shown(app) == [f"{GH}#9", f"{GL}#2", f"{GH}#10", f"{GH}#1"]
        assert "CI▼" in header_text(app) and "▲" not in header_text(app)
        assert "order: ci desc" in status_bar(app)
        await click_column(app, pilot, "pr")  # another column: ascending, the arrow moves
        assert shown(app) == [f"{GH}#1", f"{GH}#9", f"{GH}#10", f"{GL}#2"]
        assert "PR▲" in header_text(app) and "CI▼" not in header_text(app)
        assert app.query_one("#list").has_focus  # the list keeps the keyboard

    run(script)


def test_each_column_sorts_both_ways_on_a_mixed_fixture() -> None:
    expected_asc = {
        "pr": [f"{GH}#1", f"{GH}#9", f"{GH}#10", f"{GL}#2"],  # 9 before 10
        "ci": [f"{GH}#1", f"{GH}#10", f"{GL}#2", f"{GH}#9"],
        "approvals": [f"{GL}#2", f"{GH}#9", f"{GH}#1", f"{GH}#10"],
        "review": [f"{GL}#2", f"{GH}#9", f"{GH}#1", f"{GH}#10"],
        "comments": [f"{GH}#1", f"{GH}#9", f"{GL}#2", f"{GH}#10"],  # three ties end on the pr
        "age": [f"{GH}#10", f"{GH}#1", f"{GL}#2", f"{GH}#9"],
        "title": [f"{GH}#9", f"{GH}#1", f"{GH}#10", f"{GL}#2"],  # nine, one, ten, Two
        "author": [f"{GL}#2", f"{GH}#10", f"{GH}#9", f"{GH}#1"],
    }

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        for column, order in expected_asc.items():
            app.set_sort((ASC(column),))
            await pilot.pause()
            assert shown(app) == order, column
            app.set_sort((ASC(column, descending=True),))
            await pilot.pause()
            if column == "comments":  # the tie keeps its pr order in both directions
                order = [order[3], *order[:3]]
            else:
                order = list(reversed(order))
            assert shown(app) == order, column

    run(script)


def test_the_keyboard_path_matches_the_mouse() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("H")
        assert app.query_one("#header").has_focus
        await pilot.press("right", "right")  # pr, badge, ci
        await pilot.press("enter")
        assert shown(app) == [f"{GH}#1", f"{GH}#10", f"{GL}#2", f"{GH}#9"]
        assert "CI▲" in header_text(app)
        await pilot.press("space")  # again: reverse
        assert "CI▼" in header_text(app)
        assert shown(app) == [f"{GH}#9", f"{GL}#2", f"{GH}#10", f"{GH}#1"]
        await pilot.press("left")
        await pilot.press("enter")  # badge: ascending, replaces ci
        assert "badge▲" in header_text(app) and "CI" in header_text(app)
        assert "▼" not in header_text(app)
        await pilot.press("escape")
        assert app.query_one("#list").has_focus
        await pilot.press("H")
        assert app._cursor == "badge"  # the cursor starts on the sorted column
        await pilot.press("escape")

    run(script)


def test_the_header_cursor_is_drawn_only_while_the_header_has_focus() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        def reversed_cells() -> list[tuple[int, int]]:
            content = content_of(app, "#header")
            return [(s.start, s.end) for s in content.spans if "reverse" in str(s.style)]

        assert reversed_cells() == []
        await pilot.press("H")
        span = columns.column_span("pr", app._pr_width, app._hoffset, app._view_width())
        # the cursor is painted from a call_after_refresh once the header has focus
        await wait_until(pilot, lambda: reversed_cells() == [span])
        assert reversed_cells() == [span]
        await pilot.press("right")
        span = columns.column_span("badge", app._pr_width, app._hoffset, app._view_width())
        assert reversed_cells() == [span]
        await pilot.press("escape")
        await pilot.pause()
        assert reversed_cells() == []

    run(script)


def test_the_cursor_scrolls_a_hidden_column_into_view() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert columns.column_span("roles", app._pr_width, app._hoffset, app._view_width()) is None
        await pilot.press("H")
        for _ in range(10):
            await pilot.press("right")
        assert app._cursor == "roles"
        assert app._hoffset > 0
        assert columns.column_span("roles", app._pr_width, app._hoffset, app._view_width())
        await pilot.press("right")  # the last column: stays
        assert app._cursor == "roles"
        await pilot.press("enter")
        assert "roles▲" in header_text(app)

    run(script, width=70)


@pytest.mark.parametrize("width", [60, 80, 120, 200])
def test_a_click_maps_to_the_right_column_at_every_width_and_offset(width: int) -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        for steps in range(0, 9):
            app._hoffset = 0
            for _ in range(steps):
                app.scroll_columns(1)
            await pilot.pause()
            view = app._view_width()
            for column in columns.COLUMNS:
                if column.id == "note":
                    continue
                if columns.column_span(column.id, app._pr_width, app._hoffset, view) is None:
                    continue
                app.set_sort(())
                await click_column(app, pilot, column.id)
                assert app._sort == (ASC(column.id),), (width, steps, column.id)
        assert app._sort

    run(script, width=width)


def test_clicking_a_separator_or_the_note_column_does_not_sort() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        note = columns.column_span("note", app._pr_width, 0, app._view_width())
        assert note is not None
        status = content_of(app, "#status").plain
        await pilot.click("#header", offset=(note[0] + 2, 0))
        pinned = columns.pinned_width(app._pr_width)
        await pilot.click("#header", offset=(pinned + 2, 0))  # the separator after note
        await pilot.pause()
        assert app._sort == ()
        assert content_of(app, "#status").plain == status  # nothing is said either
        assert "▲" not in header_text(app)

    run(script)


def test_s_returns_to_the_preset_cycle_and_leaves_no_stale_order() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert "order: repo" in status_bar(app)
        app.set_sort((ASC("ci", descending=True),))
        await pilot.pause()
        assert "order: ci desc" in status_bar(app) and "order: repo" not in status_bar(app)
        await pilot.press("s")  # drops the column sort, back on the preset it replaced
        assert app._sort == ()
        assert "order: repo" in status_bar(app) and "ci desc" not in status_bar(app)
        assert "▲" not in header_text(app) and "▼" not in header_text(app)
        await pilot.press("s")
        assert "order: attention" in status_bar(app)
        app.set_sort((ASC("age"),))
        await pilot.pause()
        assert "order: age asc" in status_bar(app) and "attention" not in status_bar(app)
        await pilot.press("s")
        assert "order: attention" in status_bar(app)
        await pilot.press("s")
        assert "order: repo" in status_bar(app)

    run(script)


def test_an_arriving_snapshot_moves_only_its_row_and_keeps_the_highlight() -> None:
    items = [make_item(n, login="u") for n in range(1, 9)]
    snaps = {i["pr"]: snap(i["number"], ci="passing") for i in items}
    source = FakeSource(authored=items, snapshots=snaps)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        # nothing loaded: every row is unknown for ci, so the order is the pr tiebreak
        app.set_sort((ASC("ci"),))
        await pilot.pause()
        before = shown(app)
        assert before == [f"{GH}#{n}" for n in range(1, 9)]
        olist = app.query_one("#list", OptionList)
        olist.highlighted = 5  # #6
        await pilot.pause()
        arrived = f"{GH}#8"
        app._snapshot_arrived(
            arrived, columns.RowState(columns.LOADED, columns.summarize(snaps[arrived]))
        )
        await pilot.pause()
        after = shown(app)
        assert after[0] == arrived  # it moves to its place among the known rows
        assert after[1:] == [p for p in before if p != arrived]  # the others do not reorder
        highlighted = olist.highlighted
        assert highlighted is not None and after[highlighted] == f"{GH}#6"

    run(script, source, concurrency=0)


def test_a_snapshot_that_does_not_change_the_sorted_cell_redraws_in_place() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        app.set_sort((ASC("ci"),))
        await pilot.pause()
        redraws: list[int] = []
        original = app._apply_filter
        app._apply_filter = lambda: (redraws.append(1), original())[1]  # type: ignore[method-assign]
        pr = f"{GH}#1"
        state = app._states[pr]
        assert state.summary is not None
        same_ci = columns.Summary(**{**vars(state.summary), "approvals": 99})
        app._snapshot_arrived(pr, columns.RowState(columns.LOADED, same_ci))
        await pilot.pause()
        assert redraws == []

    run(script)


def test_the_sort_survives_a_refresh_with_the_selection() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        app.set_sort((ASC("author", descending=True),))
        await pilot.pause()
        olist = app.query_one("#list", OptionList)
        olist.highlighted = 2
        await pilot.pause()
        pr = shown(app)[2]
        order = shown(app)
        await pilot.press("R")
        await settle(app, pilot)
        assert shown(app) == order
        assert olist.highlighted is not None and shown(app)[olist.highlighted] == pr
        assert app._sort == (ASC("author", descending=True),)

    run(script)


def test_forge_text_never_reaches_the_header_or_the_status_through_a_sort() -> None:
    hostile = "[@click=app.quit]x[/] \x1b[31m"
    source = FakeSource(authored=[make_item(1, title=hostile, login=hostile)])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        for column in ("title", "author"):
            await click_column(app, pilot, column)
            assert "@click" not in header_text(app) and "\x1b" not in header_text(app)
            assert "@click" not in status_bar(app)
        assert app.is_running

    run(script, source, concurrency=0)


def test_the_help_lists_the_header_key_and_the_arrows() -> None:
    from forgectl.tui.app import HELP_KEYS, HELP_LEGEND

    keys = dict(HELP_KEYS)
    assert "H" in keys and "sort" in keys["H"]
    assert any("▲" in key for key, _ in HELP_LEGEND)


def test_reaching_the_header_by_focus_scrolls_the_cursor_column_into_view() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("H")
        for _ in range(8):
            await pilot.press("right")
        assert app._cursor == "author"
        await pilot.press("escape")
        app._hoffset = 0  # scrolled back by the list's own keys
        app._redraw()
        await pilot.pause()
        assert columns.column_span("author", app._pr_width, 0, app._view_width()) is None
        app.query_one("#header").focus()  # what tab does
        # the header's on_focus reveals the column from a call_after_refresh, which one pause
        # does not always wait for: wait for its effect instead
        await wait_until(
            pilot,
            lambda: (
                columns.column_span("author", app._pr_width, app._hoffset, app._view_width())
                is not None
            ),
        )
        span = columns.column_span("author", app._pr_width, app._hoffset, app._view_width())
        assert span is not None and span[0] < span[1]

    run(script, width=70)


def test_many_arrivals_in_one_tick_rebuild_the_list_once_and_keep_the_selection() -> None:
    items = [make_item(n, login="u") for n in range(1, 41)]
    states = ["failing", "pending", "passing", "none"]
    snaps = {i["pr"]: snap(i["number"], ci=states[i["number"] % 4]) for i in items}
    source = FakeSource(authored=items, snapshots=snaps)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        app.set_sort((ASC("ci"),))
        await pilot.pause()
        olist = app.query_one("#list", OptionList)
        olist.highlighted = 20
        await pilot.pause()
        kept = shown(app)[20]
        rebuilds: list[int] = []
        original = app._apply_filter
        app._apply_filter = lambda: (rebuilds.append(1), original())[1]  # type: ignore[method-assign]
        for pr, data in snaps.items():
            app._snapshot_arrived(pr, columns.RowState(columns.LOADED, columns.summarize(data)))
        await pilot.pause()
        await pilot.pause()
        assert len(rebuilds) == 1
        highlighted = olist.highlighted
        assert highlighted is not None and shown(app)[highlighted] == kept

    run(script, source, concurrency=0)
