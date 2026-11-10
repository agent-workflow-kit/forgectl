"""`browse` in a very narrow terminal with the detail pane open must not crash (#107)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from textual.pilot import Pilot
from textual.widgets import OptionList

from forgectl.tui import sections
from forgectl.tui.app import BrowseApp

from .test_tui_app import settle
from .test_tui_columns import snap
from .tui_fakes import (
    FakeOpener,
    FakeSource,
    make_check,
    make_comment,
    make_diff,
    make_item,
    make_snapshot,
)

PANE_MIN_WIDTH = 12  # docs/interface.md: the detail pane never gets narrower


def source() -> FakeSource:
    items = [make_item(n, title=f"Change number {n}") for n in range(1, 41)]
    return FakeSource(authored=items, snapshots={i["pr"]: snap(i["number"]) for i in items})


def run(
    width: int,
    script: Callable[[BrowseApp, Pilot[None]], Awaitable[None]],
    height: int = 14,
    src: FakeSource | None = None,
) -> BrowseApp:
    app = BrowseApp(src or source(), FakeOpener(), concurrency=4)

    async def go() -> None:
        async with app.run_test(size=(width, height)) as pilot:
            await settle(app, pilot)
            await script(app, pilot)

    asyncio.run(go())
    return app


@pytest.mark.parametrize("width", [40, 30, 20, 14, 13, 12, 10, 5, 2, 1])
def test_starting_narrow_with_the_pane_open_does_not_crash(width: int) -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("down")
        await pilot.pause()
        assert app.is_running

    run(width, script)


def test_resizing_through_narrow_widths_with_the_pane_open_does_not_crash() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        assert app._detail_shown()
        for width in (200, 80, 30, 20, 10, 5, 1, 10, 20, 80, 200):
            await pilot.resize_terminal(width, 14)
            await pilot.pause()
            assert app.is_running
        # wide again: the pane is back with the list beside it
        assert app._detail_shown()
        assert app.query_one("#left").region.width == 120
        assert app.query_one("#detail").region.width == 80
        assert app.query_one("#list", OptionList).region.width > 0

    run(80, script)


def test_the_pane_keeps_its_minimum_width_in_a_narrow_terminal() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        assert app._detail_shown()
        for width in (30, 20, 13, 12, 10, 5):
            await pilot.resize_terminal(width, 14)
            await pilot.pause()
            assert app.query_one("#detail").region.width == PANE_MIN_WIDTH
        for width in (10, 5):
            await pilot.resize_terminal(width, 14)
            await pilot.pause()
            assert app.query_one("#left").region.width == 0
            assert app.query_one("#detail").region.x == 0
        await pilot.resize_terminal(200, 14)
        await pilot.pause()
        assert app.query_one("#detail").region.width > PANE_MIN_WIDTH

    run(80, script)


def rich_source() -> FakeSource:
    pr = make_item(1)["pr"]
    title = "漢字 e\u0301e\u0301 " + "x" * 80
    snapshot = make_snapshot(
        1,
        title=title,
        body="u" * 400 + "\n" + "漢字" * 60,
        comments=[make_comment("c" * 300 + " 漢字", "e\u0301" * 20)],
        checks=[make_check("k" * 100, "failing", None)],
    )
    diff = make_diff(pr, "@@ -1 +1 @@\n-" + "o" * 300 + "\n+" + "漢" * 100 + "\n")
    items = [make_item(1, title=title)]
    return FakeSource(authored=items, snapshots={pr: snapshot}, diffs={pr: diff})


@pytest.mark.parametrize("height", [1, 3])
@pytest.mark.parametrize("width", [1, 12, 13, 14])
def test_rich_content_in_every_section_does_not_crash_when_narrow(width: int, height: int) -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        for i, name in enumerate(sections.NAMES, 1):
            if name not in app._open_sections:
                await pilot.press(str(i))
        await pilot.pause()
        assert set(sections.NAMES) <= app._open_sections
        assert app.is_running

    run(width, script, height=height, src=rich_source())


def test_help_and_filter_bars_at_width_one_with_the_pane_open_do_not_crash() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("question_mark")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.is_running

    run(1, script)
