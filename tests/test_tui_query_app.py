"""The query panel and the query in the app: the form and the line in step, applying, the quick
filters as presets, the shared sort state, the default and the history, and unloaded rows."""

from __future__ import annotations

import asyncio
import stat
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from textual.content import Content
from textual.pilot import Pilot
from textual.widgets import Button, Input, OptionList, Select, Static

from forgectl.tui import columns, filtering, query
from forgectl.tui.app import BrowseApp
from forgectl.tui.query_screen import QueryScreen
from forgectl.tui.query_store import QueryStore
from forgectl.tui.source import Row

from .test_tui_app import content_of, list_prompts, settle, wait_until
from .test_tui_columns import GH, GL, activity, snap
from .tui_fakes import FakeOpener, FakeSource, GatedSource, make_item

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)


def mixed_source() -> FakeSource:
    """Four PRs on a GitHub and a GitLab host; ages at NOW: #1 3h, #2 2d3h, #10 1h, #9 63d."""
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


def drive(
    script: Callable[[BrowseApp, Pilot[None]], Awaitable[None]],
    source: FakeSource | None = None,
    *,
    concurrency: int = 4,
    wait: bool = True,
    **app_args: Any,
) -> BrowseApp:
    app = BrowseApp(
        source or mixed_source(),
        FakeOpener(),
        concurrency=concurrency,
        clock=lambda: NOW,
        **app_args,
    )

    async def go() -> None:
        async with app.run_test(size=(150, 60)) as pilot:
            if wait:
                await settle(app, pilot)
            else:
                await pilot.pause()
            await script(app, pilot)

    asyncio.run(go())
    return app


def n(number: int, repo: str = GH) -> str:
    return f"{repo}#{number}"


def shown(app: BrowseApp) -> list[str]:
    return [p.plain.split()[0] for p in list_prompts(app)]


def bar(app: BrowseApp) -> str:
    return content_of(app, "#bar").plain


def status(app: BrowseApp) -> str:
    return content_of(app, "#status").plain


async def pause(pilot: Pilot[None], times: int = 4) -> None:
    for _ in range(times):
        await pilot.pause()


async def open_panel(app: BrowseApp, pilot: Pilot[None]) -> QueryScreen:
    await pilot.press("F")
    await pause(pilot)
    assert isinstance(app.screen, QueryScreen)
    return app.screen


def panel(app: BrowseApp) -> QueryScreen:
    assert isinstance(app.screen, QueryScreen)
    return app.screen


def line_of(app: BrowseApp) -> str:
    return app.screen.query_one("#q-line", Input).value


async def type_line(app: BrowseApp, pilot: Pilot[None], text: str) -> None:
    app.screen.query_one("#q-line", Input).value = text
    await pause(pilot)


def panel_status(app: BrowseApp) -> str:
    return content_of_screen(app, "#q-status")


def content_of_screen(app: BrowseApp, selector: str) -> str:
    content = app.screen.query_one(selector, Static).content
    assert isinstance(content, Content)
    return content.plain


def selects(app: BrowseApp, prefix: str, suffix: str) -> list[Select[str]]:
    found = [
        s
        for s in app.screen.query(Select)
        if (s.id or "").startswith(prefix) and (s.id or "").endswith(suffix)
    ]
    return sorted(found, key=lambda s: int((s.id or "").split("-")[1]))


def values(app: BrowseApp, prefix: str, suffix: str) -> list[str]:
    return [str(s.value) for s in selects(app, prefix, suffix)]


def value_inputs(app: BrowseApp) -> list[Input]:
    found = [i for i in app.screen.query(Input) if (i.id or "").startswith("f")]
    return sorted(found, key=lambda i: int((i.id or "").split("-")[1]))


async def press(app: BrowseApp, pilot: Pilot[None], button_id: str) -> None:
    app.screen.query_one(f"#{button_id}", Button).press()
    await pause(pilot)


# -- the form and the line ---------------------------------------------------------------------


def test_the_form_builds_a_two_key_sort_and_two_filters_and_the_line_follows() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_panel(app, pilot)
        assert line_of(app) == ""
        await press(app, pilot, "q-add-sort")
        assert line_of(app) == "ORDER BY pr"
        selects(app, "s", "-col")[0].value = "ci"
        await pause(pilot)
        assert line_of(app) == "ORDER BY ci"
        await press(app, pilot, "q-add-sort")
        assert line_of(app) == "ORDER BY ci, pr"
        selects(app, "s", "-dir")[1].value = "desc"
        await pause(pilot)
        assert line_of(app) == "ORDER BY ci, pr DESC"
        # first filter: approvals >= 1
        await press(app, pilot, "q-add-filter")
        assert line_of(app) == "ci = failing ORDER BY ci, pr DESC"
        selects(app, "f", "-field")[0].value = "approvals"
        await pause(pilot)
        assert (
            line_of(app) == "ci = failing ORDER BY ci, pr DESC"
        )  # `approvals = ` is not valid yet
        assert "form:" in panel_status(app)
        selects(app, "f", "-op")[0].value = ">="
        await pause(pilot)
        value_inputs(app)[0].value = "1"
        await pause(pilot)
        assert line_of(app) == "approvals >= 1 ORDER BY ci, pr DESC"
        assert panel_status(app).startswith("valid")
        # second filter: author != bob
        await press(app, pilot, "q-add-filter")
        selects(app, "f", "-field")[1].value = "author"
        await pause(pilot)
        selects(app, "f", "-op")[1].value = "!="
        await pause(pilot)
        value_inputs(app)[1].value = "bob"
        await pause(pilot)
        assert line_of(app) == "approvals >= 1 AND author != bob ORDER BY ci, pr DESC"
        assert shown(app) == [n(10), n(1), n(9), n(2, GL)]  # nothing is applied yet
        await press(app, pilot, "q-apply")
        assert not isinstance(app.screen, QueryScreen)
        # approvals >= 1 and not bob: #1 (2), #9 (1); by ci: failing, none
        assert shown(app) == [n(1), n(9)]
        assert app.query_text() == "approvals >= 1 AND author != bob ORDER BY ci, pr DESC"
        assert "query: approvals >= 1 AND author != bob ORDER BY ci, pr DESC" in bar(app)

    drive(script)


def test_editing_the_line_rewrites_the_form() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_panel(app, pilot)
        await type_line(
            app, pilot, "ci IN (failing, pending) AND NOT draft AND age > 1w ORDER BY age DESC, ci"
        )
        assert values(app, "f", "-field") == ["ci", "draft", "age"]
        assert values(app, "f", "-op") == ["IN", "FALSE", ">"]
        assert [i.value for i in value_inputs(app)] == ["failing, pending", "", "1w"]
        assert values(app, "s", "-col") == ["age", "ci"]
        assert values(app, "s", "-dir") == ["desc", "asc"]
        assert panel_status(app).startswith("valid")
        # a row removed in the form rewrites the line
        await press(app, pilot, "f" + str(panel(app)._gen) + "-1-del")
        assert line_of(app) == "ci IN (failing, pending) AND age > 1w ORDER BY age DESC, ci"
        # and a sort key moved
        await press(app, pilot, "s" + str(panel(app)._gen) + "-1-up")
        assert line_of(app) == "ci IN (failing, pending) AND age > 1w ORDER BY ci, age DESC"

    drive(script)


def test_a_filter_the_form_cannot_show_stays_in_the_line() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_panel(app, pilot)
        await type_line(app, pilot, "(draft OR authored) AND ci = failing ORDER BY age")
        assert selects(app, "f", "-field") == []
        assert "query line" in content_of_screen(app, "#q-filters Static")
        # the sort keys are still the form's, and a change keeps the filter
        selects(app, "s", "-dir")[0].value = "desc"
        await pause(pilot)
        assert line_of(app) == "(draft OR authored) AND ci = failing ORDER BY age DESC"
        await press(app, pilot, "q-add-filter")  # cannot add a row to a filter it cannot show
        assert line_of(app) == "(draft OR authored) AND ci = failing ORDER BY age DESC"

    drive(script)


def test_the_table_shows_the_expected_rows_and_order_on_a_mixed_fixture() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert shown(app) == [n(10), n(1), n(9), n(2, GL)]
        await open_panel(app, pilot)
        await type_line(
            app,
            pilot,
            "ci IN (failing, pending, none) AND repo LIKE '*/acme/*' AND age < 30d "
            "ORDER BY age DESC, ci",
        )
        await press(app, pilot, "q-apply")
        # #1 (3h, failing) and #10 (1h, pending) match; #9 (63d) is too old, #2 is not passing-free
        assert shown(app) == [n(1), n(10)]  # oldest first (age DESC)
        await open_panel(app, pilot)
        await type_line(app, pilot, "author IN (amy, 'Cat') OR approvals >= 7 ORDER BY pr DESC")
        await press(app, pilot, "q-apply")
        assert shown(app) == [n(2, GL), n(10), n(9)]

    drive(script)


# -- invalid input -----------------------------------------------------------------------------


def test_an_invalid_query_keeps_the_last_valid_state_and_is_not_applied_saved_or_remembered() -> (
    None
):
    store = QueryStore()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_panel(app, pilot)
        await type_line(app, pilot, "ci = failing")
        await press(app, pilot, "q-apply")
        assert shown(app) == [n(1)]
        assert store.entries == ["ci = failing"]
        await open_panel(app, pilot)
        assert line_of(app) == "ci = failing"
        await type_line(app, pilot, "ci = failing AND")  # ends in the middle of a condition
        assert "column 17" in panel_status(app) and "column name" in panel_status(app)
        assert values(app, "f", "-field") == ["ci"]  # the form still shows the last valid state
        await press(app, pilot, "q-apply")
        assert isinstance(app.screen, QueryScreen)  # refused: still open
        assert "not applied" in panel_status(app)
        await press(app, pilot, "q-default")  # nor saved as the default
        assert "not applied or saved" in panel_status(app)
        await pilot.press("escape")
        await pause(pilot)
        assert not isinstance(app.screen, QueryScreen)
        assert app.query_text() == "ci = failing"  # the active query did not change
        assert shown(app) == [n(1)]
        assert store.entries == ["ci = failing"]  # nothing invalid in the history

    drive(script, store=store)


def test_a_form_row_with_a_bad_value_is_an_error_and_never_reaches_the_line() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_panel(app, pilot)
        await press(app, pilot, "q-add-filter")
        value_inputs(app)[0].value = "bogus"  # not a CI state
        await pause(pilot)
        assert "form:" in panel_status(app) and "one of" in panel_status(app)
        assert line_of(app) == "ci = failing"
        await press(app, pilot, "q-apply")
        assert isinstance(app.screen, QueryScreen)
        value_inputs(app)[0].value = "x) OR (1 = 1"  # injection into the row stays a parse error
        await pause(pilot)
        assert "form:" in panel_status(app)
        assert line_of(app) == "ci = failing"

    drive(script)


def test_escape_cancels_and_leaves_the_active_query() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("a")
        assert app.query_text() == "authored"
        await open_panel(app, pilot)
        await type_line(app, pilot, "draft")
        await press(app, pilot, "q-cancel")
        assert app.query_text() == "authored"
        await open_panel(app, pilot)
        assert line_of(app) == "authored"
        await type_line(app, pilot, "draft")
        await pilot.press("escape")
        await pause(pilot)
        assert app.query_text() == "authored"
        assert len(shown(app)) == 4

    drive(script)


def test_reset_clears_the_query_and_the_bar_shows_an_indicator_only_while_one_is_active() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert "query:" not in bar(app)
        await open_panel(app, pilot)
        await type_line(app, pilot, "ci = failing ORDER BY age")
        await press(app, pilot, "q-apply")
        assert "query: ci = failing ORDER BY age" in bar(app)
        await open_panel(app, pilot)
        await press(app, pilot, "q-reset")
        assert not isinstance(app.screen, QueryScreen)
        assert app.query_text() == ""
        assert "query:" not in bar(app)
        assert len(shown(app)) == 4
        assert status(app) == "query cleared"
        # a sort alone is a query too
        app.set_sort((columns.SortKey("ci"),))
        assert "query: ORDER BY ci" in bar(app)

    drive(script)


# -- quick filters are presets -----------------------------------------------------------------


def test_the_quick_keys_write_the_equivalent_query_and_the_panel_shows_it() -> None:
    items = [
        make_item(1, title="mine failing"),
        make_item(2, title="mine new"),
        make_item(3, title="theirs failing"),
        make_item(4, title="theirs both"),
    ]
    snaps = {
        n(1): snap(1, ci="failing"),
        n(2): snap(2, act=activity(others=1)),
        n(3): snap(3, ci="failing"),
        n(4): snap(4, ci="failing", act=activity(others=3)),
    }
    source = FakeSource(authored=items[:2], requested=items[2:], snapshots=snaps)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("a", "f")
        assert app.query_text() == "authored AND ci = failing"
        assert shown(app) == [n(1)]
        assert "query: authored AND ci = failing" in bar(app)
        await open_panel(app, pilot)
        assert line_of(app) == "authored AND ci = failing"
        assert values(app, "f", "-field") == ["authored", "ci"]
        await pilot.press("escape")
        await pause(pilot)
        await pilot.press("a", "v", "n")
        assert app.query_text() == "ci = failing AND requested AND others_since_push"
        assert shown(app) == [n(4)]
        await pilot.press("v", "f", "n")
        assert app.query_text() == ""

    drive(script, source)


def test_a_quick_key_keeps_the_sort_and_groups_an_or_filter() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_panel(app, pilot)
        await type_line(app, pilot, "draft OR requested ORDER BY age")
        await press(app, pilot, "q-apply")
        await pilot.press("a")
        assert app.query_text() == "(draft OR requested) AND authored ORDER BY age"

    drive(script)


# -- one sort state ----------------------------------------------------------------------------


def test_the_header_sort_and_order_by_are_one_state() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        app.set_sort(columns.toggle_sort(app._sort, "ci"))  # what a header click does
        assert app.query_text() == "ORDER BY ci"
        await open_panel(app, pilot)
        assert line_of(app) == "ORDER BY ci"
        assert values(app, "s", "-col") == ["ci"]
        await type_line(app, pilot, "ORDER BY age DESC, ci")
        await press(app, pilot, "q-apply")
        assert app._sort == (columns.SortKey("age", True), columns.SortKey("ci"))
        header = content_of(app, "#header").plain
        assert "age▼1" in header and "CI▲2" in header  # the position beside each arrow
        # header activation still makes a single key, replacing the query's keys
        app.set_sort(columns.toggle_sort(app._sort, "title"))
        assert app.query_text() == "ORDER BY title"
        assert "▲1" not in content_of(app, "#header").plain

    drive(script)


def test_order_by_alone_keeps_the_filter_and_a_filter_keeps_the_order() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        app.set_sort((columns.SortKey("title"),))
        await pilot.press("f")
        assert app.query_text() == "ci = failing ORDER BY title"

    drive(script)


# -- unloaded rows -----------------------------------------------------------------------------


def test_a_row_without_a_snapshot_matches_no_value_filter_and_the_panel_counts_it() -> None:
    base = mixed_source()
    source = GatedSource(gated=("snapshot",), authored=base.authored, snapshots=base.snapshots)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        # the panel's report follows the line a tick or more later: wait for it, never a fixed pause
        async def report_is(check: Callable[[str], bool]) -> None:
            await wait_until(pilot, lambda: check(content_of_screen(app, "#q-report")))

        # the list is in; the snapshots are held back by the gate
        await wait_until(pilot, lambda: len(shown(app)) == 4)  # loading rows, no filter
        await open_panel(app, pilot)
        await type_line(app, pilot, "ci = failing")
        await report_is(
            lambda r: r == "0 of 4 PRs match; 4 hidden because their snapshot has not loaded"
        )
        await type_line(app, pilot, "NOT ci = failing")  # unknown stays unknown under NOT
        await report_is(lambda r: "4 hidden because" in r)
        await type_line(app, pilot, "ci IS NULL")
        await report_is(lambda r: r == "4 of 4 PRs match")
        await type_line(app, pilot, "title LIKE '*e*' AND ci = failing")  # the list data is known
        await report_is(lambda r: "hidden because" in r)
        await type_line(app, pilot, "title = nothing AND ci = failing")  # false dominates
        await report_is(lambda r: r == "0 of 4 PRs match")
        await type_line(app, pilot, "ci = failing")
        await report_is(lambda r: "hidden because" in r)
        await press(app, pilot, "q-apply")
        assert shown(app) == []
        source.gate.set()
        await app.workers.wait_for_complete()
        await wait_until(pilot, lambda: shown(app) == [n(1)])  # appears on arrival
        assert shown(app) == [n(1)]

    drive(script, source, wait=False)


def test_unknown_values_sort_last_in_both_directions_through_order_by() -> None:
    rows = [Row(make_item(k)) for k in (1, 2, 3)]
    states = {
        rows[0].pr: columns.RowState(columns.LOADED, columns.Summary(ci_state="passing")),
        rows[1].pr: columns.RowState(),  # loading
        rows[2].pr: columns.RowState(columns.LOADED, columns.Summary(ci_state="failing")),
    }
    for text, expected in (("ORDER BY ci", [3, 1, 2]), ("ORDER BY ci DESC", [1, 3, 2])):
        parsed = query.parse(text)
        assert isinstance(parsed, query.Query)
        got = columns.order(rows, states, columns.SORT_REPO, filtering.sort_of(parsed.order))
        assert [r.pr for r in got] == [n(k) for k in expected]


# -- start, default and history ----------------------------------------------------------------


def test_the_configured_query_applies_at_start() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert shown(app) == [n(10), n(1)]
        assert app.query_text() == "ci IN (failing, pending) ORDER BY age"
        assert "query: ci IN (failing, pending) ORDER BY age" in bar(app)

    drive(script, query="ci in (failing,pending) order by age")


def test_set_default_writes_only_the_drop_in_and_shows_its_path(tmp_path: Path) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    main = config / "config.yaml"
    main.write_text("# comment\nme: {name: jo}\n")
    before = main.read_bytes()
    store = QueryStore(config_dir=config)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("f")
        await pilot.press("D")
        await pause(pilot)
        path = config / "config.d" / "90-browse-query.yaml"
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert str(path) in status(app)
        assert main.read_bytes() == before
        assert sorted(p.name for p in (config / "config.d").iterdir()) == [path.name]
        assert "ci = failing" in path.read_text()
        # from the panel: the line's text is what is saved
        await open_panel(app, pilot)
        await type_line(app, pilot, "authored ORDER BY age DESC")
        await press(app, pilot, "q-default")
        assert str(path) in panel_status(app)
        assert "authored ORDER BY age DESC" in path.read_text()
        await press(app, pilot, "q-cancel")
        assert app.query_text() == "ci = failing"  # saving a default did not apply it

    drive(script, store=store)


def test_a_failed_default_write_is_a_status_note() -> None:
    store = QueryStore()  # no config directory

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("D")
        await pause(pilot)
        assert status(app).startswith("default not saved")

    drive(script, store=store)


def test_reset_to_default_returns_to_the_configured_query() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("v")
        assert app.query_text() == "authored AND requested ORDER BY age"
        await open_panel(app, pilot)
        await press(app, pilot, "q-reset-default")
        assert app.query_text() == "authored ORDER BY age"
        assert status(app) == "query reset to the default"

    drive(script, query="authored ORDER BY age")


def test_history_is_remembered_newest_first_and_up_down_step_through_it(tmp_path: Path) -> None:
    history = tmp_path / "state" / "browse-history"
    store = QueryStore(history_path=history)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        for text in ("ci = failing", "authored", "ci = failing"):
            await open_panel(app, pilot)
            await type_line(app, pilot, text)
            await press(app, pilot, "q-apply")
        assert store.entries == ["ci = failing", "authored"]
        assert history.read_text() == "ci = failing\nauthored\n"
        assert stat.S_IMODE(history.stat().st_mode) == 0o600
        await open_panel(app, pilot)
        olist = app.screen.query_one("#q-history", OptionList)
        assert olist.option_count == 2
        await type_line(app, pilot, "draft")  # typed text, not applied
        await pilot.press("up")
        await pause(pilot)
        assert line_of(app) == "ci = failing"
        await pilot.press("up")
        await pause(pilot)
        assert line_of(app) == "authored"
        await pilot.press("up")  # the oldest stays
        await pause(pilot)
        assert line_of(app) == "authored"
        await pilot.press("down")
        await pause(pilot)
        assert line_of(app) == "ci = failing"
        await pilot.press("down")
        await pause(pilot)
        assert line_of(app) == "draft"  # the typed text comes back
        # a history entry fills the line and the form
        olist.highlighted = 1
        olist.action_select()
        await pause(pilot)
        assert line_of(app) == "authored"
        assert values(app, "f", "-field") == ["authored"]

    drive(script, store=store)


# -- hostile input in the app ------------------------------------------------------------------


def test_markup_and_hostile_text_in_a_query_is_inert() -> None:
    hostile = [
        "title = '[bold red]x[/] [@click=app.quit()]y'",
        "title LIKE '*[link=https://x]*'",
        "title = '\\u001b[2J'",
        "text LIKE '(a+)+$'",
        "(" * 30 + "draft" + ")" * 30,
        "x" * 5000,
        "title = '" + "é" * 150 + "'",
    ]

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        for text in hostile:
            await open_panel(app, pilot)
            await type_line(app, pilot, text)
            await press(app, pilot, "q-apply")
        assert app.is_running
        await open_panel(app, pilot)
        await type_line(app, pilot, "title = '[bold red]x[/]'")
        await press(app, pilot, "q-apply")
        assert "query: title = '[bold red]x[/]'" in bar(app)
        assert shown(app) == []

    drive(script)


def test_selection_and_scroll_survive_applying_a_query() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        olist = app.query_one("#list", OptionList)
        olist.highlighted = shown(app).index(n(10))
        await open_panel(app, pilot)
        await type_line(app, pilot, "ci IN (failing, pending)")
        await press(app, pilot, "q-apply")
        assert shown(app)[olist.highlighted or 0] == n(10)
        await app.workers.wait_for_complete()
        await pilot.press("R")  # a refresh keeps the query and the highlight
        await settle(app, pilot)
        assert app.query_text() == "ci IN (failing, pending)"
        assert shown(app)[olist.highlighted or 0] == n(10)

    drive(script)


def test_the_panel_names_the_fixed_host_query_and_shows_no_hidden_repo_count() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_panel(app, pilot)
        host = content_of_screen(app, "#q-host")
        assert "authored by me" in host and "review requested" in host
        everything = " ".join(content_of_screen(app, f"#{i}") for i in ("q-host", "q-report"))
        assert "hidden repo" not in everything.lower()

    drive(script)


# -- round 1 fixes -----------------------------------------------------------------------------


def drive_sized(
    script: Callable[[BrowseApp, Pilot[None]], Awaitable[None]],
    size: tuple[int, int],
    source: FakeSource | None = None,
    **app_args: Any,
) -> None:
    app = BrowseApp(source or mixed_source(), FakeOpener(), clock=lambda: NOW, **app_args)

    async def go() -> None:
        async with app.run_test(size=size) as pilot:
            await settle(app, pilot)
            await script(app, pilot)

    asyncio.run(go())


def test_a_quick_key_that_would_not_fit_leaves_the_query_as_it_was() -> None:
    full = " AND ".join(f"approvals > {k}" for k in range(50))

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        parsed = query.parse(full)
        assert isinstance(parsed, query.Query)
        app.set_query(parsed)
        before = app.query_text()
        await pilot.press("a")
        assert app.query_text() == before
        assert "not toggled" in status(app)

    drive(script)


def test_the_panel_controls_stay_on_the_screen_on_a_small_terminal() -> None:
    for size in ((80, 10), (60, 15), (40, 12)):

        async def script(app: BrowseApp, pilot: Pilot[None], size: tuple[int, int] = size) -> None:
            width = size[0]
            parsed = query.parse("ci = failing AND title LIKE 'a*' ORDER BY age DESC, ci")
            assert isinstance(parsed, query.Query)
            app.set_query(parsed)
            await open_panel(app, pilot)
            await pause(pilot)
            controls = [*app.screen.query(Button), *app.screen.query(Select)]
            assert len(controls) > 10
            for widget in controls:
                region = widget.region
                assert region.x >= 0 and region.right <= width, (width, widget.id, region)
            panel = app.screen.query_one("#qpanel")
            assert panel.max_scroll_y > 0, size  # the controls do not all fit: it must scroll
            for widget in controls:
                widget.scroll_visible(animate=False, immediate=True)
                await pause(pilot)
                inside = panel.scrollable_content_region.contains_region(widget.region)
                assert inside, (size, widget.id, widget.region, panel.region)

        drive_sized(script, size)


def test_a_form_edit_over_an_invalid_line_says_the_line_was_replaced() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_panel(app, pilot)
        await type_line(app, pilot, "ci =")
        assert "query line:" in panel_status(app)
        await press(app, pilot, "q-add-filter")
        assert line_of(app) == "ci = failing"
        assert "line replaced by the form" in panel_status(app)

    drive(script)


def test_the_panel_shows_the_default_and_follows_set_as_default(tmp_path: Path) -> None:
    store = QueryStore(config_dir=tmp_path / "cfg")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_panel(app, pilot)
        assert content_of_screen(app, "#q-default-text").endswith(": draft")
        await type_line(app, pilot, "ci = failing")
        await press(app, pilot, "q-default")
        assert content_of_screen(app, "#q-default-text").endswith(": ci = failing")

    drive(script, store=store, query="draft")


def test_the_panel_count_includes_the_text_filter_and_follows_arriving_snapshots() -> None:
    base = mixed_source()
    source = GatedSource(gated=("snapshot",), authored=base.authored, snapshots=base.snapshots)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        for _ in range(100):
            if len(shown(app)) == 4:
                break
            await pilot.pause(0.05)
        await pilot.press("/", "t", "e", "n", "enter")
        await pause(pilot)
        await open_panel(app, pilot)
        await type_line(app, pilot, "ci = pending")
        report = content_of_screen(app, "#q-report")
        assert report.startswith("0 of 1 PRs match (with the / text filter); 1 hidden")
        source.gate.set()
        await app.workers.wait_for_complete()
        await pause(pilot)
        assert content_of_screen(app, "#q-report") == "1 of 1 PRs match (with the / text filter)"

    drive(script, source, wait=False)
