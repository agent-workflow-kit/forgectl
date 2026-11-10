"""`browse` in a very narrow terminal with the detail pane open must not crash (#107)."""

from __future__ import annotations

import asyncio

import pytest
from textual.pilot import Pilot
from textual.widgets import OptionList

from forgectl.tui.app import BrowseApp

from .test_tui_app import settle
from .test_tui_columns import snap
from .tui_fakes import FakeOpener, FakeSource, make_item

PANE_MIN_WIDTH = 12  # docs/interface.md: the detail pane never gets narrower


def source() -> FakeSource:
    items = [make_item(n, title=f"Change number {n}") for n in range(1, 41)]
    return FakeSource(authored=items, snapshots={i["pr"]: snap(i["number"]) for i in items})


def run(width: int, script, height: int = 14) -> BrowseApp:  # type: ignore[no-untyped-def]
    app = BrowseApp(source(), FakeOpener(), concurrency=4)

    async def go() -> None:
        async with app.run_test(size=(width, height)) as pilot:
            await settle(app, pilot)
            await script(app, pilot)

    asyncio.run(go())
    return app


@pytest.mark.parametrize("width", [40, 30, 20, 10, 5, 2, 1])
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
        assert app.query_one("#detail").region.width > 0
        assert app.query_one("#list", OptionList).region.width > 0

    run(80, script)


def test_the_pane_keeps_its_minimum_width_in_a_narrow_terminal() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        for width in (30, 20, 10, 5):
            await pilot.resize_terminal(width, 14)
            await pilot.pause()
            assert app.query_one("#detail").region.width == PANE_MIN_WIDTH
        await pilot.resize_terminal(200, 14)
        await pilot.pause()
        assert app.query_one("#detail").region.width > PANE_MIN_WIDTH

    run(80, script)
