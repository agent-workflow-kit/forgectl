"""`browse` list layout: sticky header, every cell cut to its column, horizontal scrolling.

The unit tests drive ``columns`` (a pure column table); the pilot tests drive the app at 80, 120
and 200 cells wide and read what the header and the rows show.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from rich.cells import cell_len
from textual.pilot import Pilot
from textual.widgets import Input, OptionList, Static

from forgectl.tui import columns
from forgectl.tui.app import BrowseApp
from forgectl.tui.source import Row

from .test_tui_app import content_of, list_prompts, settle
from .test_tui_columns import order_of, snap
from .tui_fakes import FakeOpener, FakeSource, make_item

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
PR_W = 24
TABLE = columns.layout(PR_W)[-1][1] + columns.layout(PR_W)[-1][2]


def parts_for(
    item: dict,
    roles: set[str] | None = None,
    *,
    refreshing: bool = False,
    changed: bool = False,
) -> list[str]:
    row = Row(item, {"authored"} if roles is None else roles)
    summary = columns.summarize(snap(item["number"]))
    state = columns.RowState(columns.LOADED, summary, refreshing=refreshing, changed=changed)
    return columns.cells(row, state, NOW)


# -- #99: every cell is cut to its column ----------------------------------------------------


def test_a_long_author_is_cut_and_later_columns_stay_aligned() -> None:
    short = parts_for(make_item(1, login="al"), {"authored"})
    long = parts_for(make_item(2, login="x" * 60), {"authored", "requested"}, refreshing=True)
    wide = parts_for(make_item(3, login="日本語" * 15), set(), changed=True)
    lines = [columns.row_line(p, PR_W) for p in (short, long, wide)]
    assert len({cell_len(line) for line in lines}) == 1
    layout = {c.id: (start, width) for c, start, width in columns.layout(PR_W)}
    a_start, a_width = layout["author"]
    r_start, _ = layout["roles"]
    assert r_start == a_start + a_width + len(columns.SEPARATOR)
    cut = cut_cells(lines[1], a_start, a_width)
    assert cut.rstrip().endswith("…") and cell_len(cut) == a_width
    wide_cell = cut_cells(lines[2], a_start, a_width)
    assert wide_cell.rstrip().endswith("…") and cell_len(wide_cell) == a_width
    # the roles start at the same cell whether or not the row has a marker or roles
    for line, roles in zip(lines, ("authored", "authored+requested", ""), strict=True):
        assert cut_cells(line, r_start, 18).strip() == roles


def cut_cells(line: str, start: int, width: int) -> str:
    return columns._slice_cells(line, start, width)


def test_the_marker_is_cut_to_its_cell_and_wide_chars_count_as_two() -> None:
    stale = parts_for(make_item(1))
    stale[10] = "[stale: " + "日" * 30 + "]"
    line = columns.row_line(stale, PR_W)
    _, start, width = columns.layout(PR_W)[1]
    note = cut_cells(line, start, width)
    assert note.endswith("…") and cell_len(note) == width
    assert cell_len(line) == TABLE


def test_a_long_pr_column_is_capped_and_keeps_the_number() -> None:
    parts = parts_for(make_item(12345, repo="github.com/" + "o" * 80 + "/proj"))
    line = columns.row_line(parts, columns.PR_MAX_WIDTH)
    first = cut_cells(line, 0, columns.PR_MAX_WIDTH)
    assert first.startswith("…") and first.rstrip().endswith("#12345")


def test_the_header_uses_the_row_widths() -> None:
    header = columns.header_line(PR_W)
    row = columns.row_line(parts_for(make_item(1)), PR_W)
    assert cell_len(header) == cell_len(row)
    for column, start, width in columns.layout(PR_W):
        cell = cut_cells(header, start, width)
        assert cell.rstrip() == columns.fit(column.header, width).rstrip()
        assert column.header in cell  # the names are not cut to nothing


# -- #100 / #104 seams: offsets and hit testing ----------------------------------------------


def test_the_offset_is_clamped_to_the_table() -> None:
    view = 80
    pinned = columns.pinned_width(PR_W)
    assert columns.clamp_offset(-5, PR_W, view) == 0
    furthest = columns.scroll_extent(PR_W) - (view - pinned - 2)
    assert columns.clamp_offset(10_000, PR_W, view) == furthest
    assert columns.clamp_offset(10_000, PR_W, 500) == 0  # the table fits: nothing scrolls


@pytest.mark.parametrize("view", [1, 10, 20])
def test_steps_do_not_move_when_there_is_no_room_to_scroll(view: int) -> None:
    assert columns._scroll_room(PR_W, view) == 0
    for direction in (1, -1):
        assert columns.step_offset(0, direction, PR_W, view) == 0
        assert columns.step_offset(7, direction, PR_W, view) == 7  # the offset is left as it is


def test_steps_move_one_column_and_stop_at_the_ends() -> None:
    view = 80
    offset = 0
    seen = []
    for _ in range(20):
        new = columns.step_offset(offset, 1, PR_W, view)
        if new == offset:
            break
        seen.append(new)
        offset = new
    first = columns.layout(PR_W)[columns.PINNED][1]
    assert seen[0] == columns.layout(PR_W)[columns.PINNED + 1][1] - first  # exactly one column
    assert offset == columns.clamp_offset(10_000, PR_W, view)
    for _ in range(20):
        offset = columns.step_offset(offset, -1, PR_W, view)
    assert offset == 0
    assert columns.step_offset(0, 1, PR_W, 500) == 0


def test_column_at_maps_x_through_the_pinned_columns_and_the_offset() -> None:
    view = 80
    assert columns.column_at(0, PR_W, 0, view) == "pr"
    note_start = columns.layout(PR_W)[1][1]
    assert columns.column_at(note_start, PR_W, 0, view) == "note"
    pinned = columns.pinned_width(PR_W)
    assert columns.column_at(pinned, PR_W, 0, view) is None  # the separator
    assert columns.column_at(pinned + 2, PR_W, 0, view) == "badge"
    assert columns.column_at(view, PR_W, 0, view) is None  # outside the view
    # after scrolling, the pinned cells do not move and the others shift
    off = columns.step_offset(0, 1, PR_W, view)
    assert columns.column_at(0, PR_W, off, view) == "pr"
    assert columns.column_at(pinned + 2, PR_W, off, view) != "badge"
    # the visible text agrees with the mapping, at every x and offset
    for offset in (0, off, columns.clamp_offset(10_000, PR_W, view)):
        header = columns.header_line(PR_W, offset, view)
        for x in range(view):
            col = columns.column_at(x, PR_W, offset, view)
            if col is None:
                assert header[x : x + 1] in (" ", "")
        names = {c.id: c.header for c in columns.COLUMNS}
        for x, ch in enumerate(header):
            col = columns.column_at(x, PR_W, offset, view)
            if ch != " " and col is not None:
                assert ch in names[col] or ch == "…"


def test_row_line_viewport_keeps_the_pinned_cells() -> None:
    parts = parts_for(make_item(1, login="zed"), {"authored"}, refreshing=True)
    full = columns.row_line(parts, PR_W)
    view = columns.row_line(parts, PR_W, 10_000, 80)
    pinned = columns.pinned_width(PR_W)
    assert view[:pinned] == full[:pinned]
    assert cell_len(view) == 80
    assert "@zed" in view and "title" not in view  # at the far end the title is scrolled out


# -- pilot: header and scrolling -------------------------------------------------------------


def long_source(count: int = 40) -> FakeSource:
    items = [
        make_item(n, title=f"title {n} " + "日本" * 5, login=("y" * 50 if n % 3 == 0 else "bob"))
        for n in range(1, count + 1)
    ]
    return FakeSource(authored=items, snapshots={i["pr"]: snap(i["number"]) for i in items})


def run_at(
    width: int,
    script: Callable[[BrowseApp, Pilot[None]], Awaitable[None]],
    source: FakeSource | None = None,
    height: int = 14,
) -> BrowseApp:
    app = BrowseApp(source or long_source(), FakeOpener(), concurrency=4)

    async def go() -> None:
        async with app.run_test(size=(width, height)) as pilot:
            await settle(app, pilot)
            await script(app, pilot)

    asyncio.run(go())
    return app


def header_text(app: BrowseApp) -> str:
    return content_of(app, "#header").plain


@pytest.mark.parametrize("width", [80, 120, 200])
def test_the_header_stays_on_the_first_line_while_rows_scroll(width: int) -> None:

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        olist = app.query_one("#list", OptionList)
        header = app.query_one("#header", Static)
        top = header.region.y
        assert olist.region.y == top + 1
        before = header_text(app)
        assert before.startswith("PR") and "badge" in before
        for _ in range(30):
            await pilot.press("down")
        await pilot.pause()
        assert olist.scroll_y > 0
        assert header.region.y == top and header_text(app) == before
        assert olist.highlighted == 30  # the header took no index

    run_at(width, script)


@pytest.mark.parametrize("width", [80, 120, 200])
def test_header_cells_line_up_with_the_row_cells_at_each_width(width: int) -> None:

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        header = header_text(app)
        prompts = [p.plain for p in list_prompts(app)]
        assert len({cell_len(p) for p in prompts}) == 1  # wide titles and authors included
        assert cell_len(prompts[0]) == cell_len(header)
        if width >= 200:
            assert "roles" in header and "author" in header

    run_at(width, script)


def test_a_long_author_pilot_keeps_the_roles_aligned() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        prompts = [p.plain for p in list_prompts(app)]
        assert len({cell_len(p) for p in prompts}) == 1
        author = [p for p in prompts if "y" * 5 in p]
        assert author and all("…" in p for p in author)  # a 50-char login is cut
        assert not any("y" * 21 in p for p in prompts)

    run_at(200, script)


def test_left_and_right_scroll_the_columns_and_keep_the_selection() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        olist = app.query_one("#list", OptionList)
        for _ in range(5):
            await pilot.press("down")
        picked = olist.highlighted
        assert picked == 5
        first = header_text(app)
        assert "author" not in first  # at 80 cells the author is out of view
        for _ in range(12):
            await pilot.press("right")
        await pilot.pause()
        scrolled = header_text(app)
        assert "roles" in scrolled and "badge" not in scrolled
        assert scrolled.startswith("PR") and "note" in scrolled  # pinned
        rows = [p.plain for p in list_prompts(app)]
        assert all(r.startswith("github.com/acme/widgets#") for r in rows)  # the PR stays
        assert "yyyy" in "".join(rows)  # the cut author is in view
        assert olist.highlighted == picked
        assert app._highlighted_row() is not None
        for _ in range(12):
            await pilot.press("left")
        await pilot.pause()
        assert header_text(app) == first and olist.highlighted == picked
        await pilot.press("l", "l")
        assert header_text(app) != first
        await pilot.press("h", "h")
        assert header_text(app) == first

    run_at(80, script)


def test_nothing_scrolls_when_the_terminal_is_wider_than_the_table() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        before = header_text(app)
        await pilot.press("right", "right", "l")
        await pilot.pause()
        assert header_text(app) == before and app._hoffset == 0
        assert "roles" in before

    run_at(220, script)


def test_the_offset_survives_a_refresh_and_is_clamped_after_a_resize() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        for _ in range(3):
            await pilot.press("right")
        offset = app._hoffset
        assert offset > 0
        await pilot.press("R")
        await settle(app, pilot)
        assert app._hoffset == offset
        for _ in range(20):
            await pilot.press("right")
        furthest = columns.clamp_offset(10_000, app._pr_width, app._view_width())
        assert furthest > offset and app._hoffset == furthest  # the far end is reached exactly
        await pilot.resize_terminal(100, 14)  # still narrower than the table: clamped, not zero
        await pilot.pause()
        smaller = columns.clamp_offset(10_000, app._pr_width, app._view_width())
        assert 0 < smaller < furthest and app._hoffset == smaller
        await pilot.resize_terminal(220, 14)
        await pilot.pause()
        assert app._hoffset == 0  # a table that fits does not scroll
        assert "roles" in header_text(app)

    run_at(80, script)


def test_a_right_press_keeps_the_highlight_and_the_vertical_scroll() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        olist = app.query_one("#list", OptionList)
        for _ in range(25):
            await pilot.press("down")
        await pilot.pause()
        highlighted, scroll_y = olist.highlighted, olist.scroll_y
        assert highlighted == 25 and scroll_y > 0
        await pilot.press("right")
        await pilot.pause()
        assert app._hoffset > 0
        assert olist.highlighted == highlighted and olist.scroll_y == scroll_y

    run_at(80, script)


def test_arrows_in_the_filter_box_do_not_scroll_the_table() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("slash", "l", "h", "left", "right")
        assert app._hoffset == 0 and app.query_one("#filter", Input).value == "lh"

    run_at(80, script)


def test_the_filter_keeps_the_scrolled_columns_and_order() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("right", "right")
        offset = app._hoffset
        await pilot.press("slash", "5")
        await pilot.pause()
        assert app._hoffset == offset
        assert header_text(app).split()[0] == "PR"
        assert any(pr.endswith("#5") for pr in order_of(app))

    run_at(80, script)


def test_a_header_is_plain_text_even_beside_markup_shaped_rows() -> None:
    source = long_source(3)
    source.authored[0]["title"] = "[@click=app.quit]x[/]"

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert "[@click" not in header_text(app)
        assert any("[@click=app.quit]" in p.plain for p in list_prompts(app))

    run_at(200, script, source)


# -- geometry on the screen: header and rows start at the same x, nothing is clipped ----------


def screen_lines(app: BrowseApp) -> list[str]:
    return [strip.text for strip in app.screen._compositor.render_strips()]


CELL_PREFIX = {"badge": "open", "ci": "✓", "title": "title ", "author": "@", "roles": "authored"}


def assert_header_over_cells(app: BrowseApp) -> int:
    """Every column name in view starts at the screen x where its cells start; returns how many
    columns were checked."""
    lines = screen_lines(app)
    header_y = app.query_one("#header", Static).region.y
    row_y = app.query_one("#list", OptionList).content_region.y
    header, row = lines[header_y], lines[row_y]
    checked = 0
    for column in columns.COLUMNS:
        prefix = CELL_PREFIX.get(column.id)
        x = header.find(column.header)
        if prefix is None or x < 0:
            continue
        assert row[x : x + len(prefix)] == prefix, (column.id, header, row)
        checked += 1
    return checked


@pytest.mark.parametrize("width", [80, 120, 220])
def test_header_names_start_at_the_same_screen_x_as_their_cells(width: int) -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert assert_header_over_cells(app) >= 1
        olist = app.query_one("#list", OptionList)
        assert app.query_one("#header", Static).content_region.x == olist.content_region.x
        for _ in range(6):  # title, author and roles are in view after these steps at 80
            await pilot.press("right")
        await pilot.pause()
        if app._hoffset:
            assert assert_header_over_cells(app) >= 1

    run_at(width, script, simple_source())


def simple_source(count: int = 40) -> FakeSource:
    """Short logins, a PR on both lists (so the roles cell is full), nothing a cell cuts."""
    items = [make_item(n, title=f"title {n}", login="bob") for n in range(1, count + 1)]
    return FakeSource(
        authored=items,
        requested=items,
        snapshots={i["pr"]: snap(i["number"]) for i in items},
    )


@pytest.mark.parametrize("width", [80, 200])
def test_the_last_column_is_fully_readable_with_a_vertical_scrollbar(width: int) -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        olist = app.query_one("#list", OptionList)
        assert olist.show_vertical_scrollbar  # more rows than the height
        for _ in range(20):
            await pilot.press("right")
        await pilot.pause()
        assert app._hoffset == columns.clamp_offset(10_000, app._pr_width, app._view_width())
        lines = screen_lines(app)[olist.content_region.y : olist.content_region.bottom]
        assert all("…" not in line for line in lines), lines  # no cut by forgectl or Textual
        assert all("authored+requested" in line for line in lines), lines

    run_at(width, script, simple_source())


def assert_rows_fill_the_view(app: BrowseApp) -> None:
    olist = app.query_one("#list", OptionList)
    view = app._view_width()
    assert app._painted_view == view
    for prompt in list_prompts(app):
        assert cell_len(prompt.plain) == view, (cell_len(prompt.plain), view)
    lines = screen_lines(app)[olist.content_region.y : olist.content_region.bottom]
    assert any("authored+requested" in line for line in lines if line.strip()), lines


def test_rows_and_header_keep_one_width_when_a_filter_drops_the_scrollbar() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        olist = app.query_one("#list", OptionList)
        assert olist.show_vertical_scrollbar
        for _ in range(20):
            await pilot.press("right")
        await pilot.pause()
        await pilot.press("slash", "1", "1")
        for _ in range(4):
            await pilot.pause()
        assert len(list_prompts(app)) == 1
        assert not olist.show_vertical_scrollbar
        assert_rows_fill_the_view(app)

    run_at(80, script, simple_source())


def test_rows_and_header_keep_one_width_when_clearing_a_filter_brings_the_scrollbar() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        olist = app.query_one("#list", OptionList)
        await pilot.press("slash", "1", "1")
        for _ in range(4):
            await pilot.pause()
        assert not olist.show_vertical_scrollbar
        for _ in range(20):
            await pilot.press("right")
        await pilot.pause()
        await pilot.press("escape")
        for _ in range(4):
            await pilot.pause()
        assert olist.show_vertical_scrollbar
        assert len(list_prompts(app)) == 40
        for _ in range(20):
            await pilot.press("right")
        await pilot.pause()
        assert_rows_fill_the_view(app)

    run_at(80, script, simple_source())


# -- scrolling redraws the rows; it does not refilter or reorder them -------------------------


def test_a_horizontal_scroll_does_not_run_the_filter_and_order_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    real = columns.order

    def counting(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(1)
        return real(*args, **kwargs)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        monkeypatch.setattr(columns, "order", counting)
        olist = app.query_one("#list", OptionList)
        for _ in range(4):
            await pilot.press("down")
        picked, scroll_y, before = olist.highlighted, olist.scroll_y, header_text(app)
        await pilot.press("right", "right", "l")
        await pilot.pause()
        assert app._hoffset > 0 and header_text(app) != before
        assert calls == []
        rows = [p.plain for p in list_prompts(app)]
        expected = columns.row_line(
            columns.cells(app._visible[0], app._state(app._visible[0]), app.clock()),
            app._pr_width,
            app._hoffset,
            app._view_width(),
        )
        assert rows[0] == expected and all(cell_len(r) == cell_len(rows[0]) for r in rows)
        assert olist.highlighted == picked and olist.scroll_y == scroll_y
        await pilot.resize_terminal(80, 20)  # a height-only resize
        await pilot.pause()
        assert calls == []
        await pilot.resize_terminal(100, 20)  # a width change redraws, still without a refilter
        await pilot.pause()
        assert calls == []
        assert cell_len(list_prompts(app)[0].plain) == app._view_width()

    run_at(80, script)
