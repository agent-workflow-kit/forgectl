"""`browse` reads the diff only while Changes is open, and caches and prefetches details (#120)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from textual.pilot import Pilot

from forgectl.tui import app as app_module
from forgectl.tui.app import BrowseApp

from .test_tui_app import settle, wait_until
from .test_tui_detail import (  # noqa: F401 - the autouse fixture isolates HOME
    PR1,
    PR2,
    DetailSource,
    _isolated_home,
    hold,
    release,
    source_of,
    text_of,
)
from .tui_fakes import FakeOpener, make_diff, make_item, make_snapshot

PR3 = "github.com/acme/widgets#3"


def run(
    source: DetailSource,
    script: Callable[[BrowseApp, Pilot[None]], Awaitable[None]],
    *,
    collapsed: tuple[str, ...] = ("changes",),
    setup: Callable[[BrowseApp], None] | None = None,
) -> BrowseApp:
    app = BrowseApp(source, FakeOpener(), concurrency=2, detail_collapsed=collapsed)
    if setup is not None:
        setup(app)

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await settle(app, pilot)
            await script(app, pilot)

    asyncio.run(go())
    return app


def count(source: DetailSource, name: str, pr: str) -> int:
    return sum(1 for c in source.calls if c == (name, pr))


def three_rows() -> DetailSource:
    items = [make_item(n, title=f"PR {n}", updated_at=f"2026-11-03T1{n}:00:00Z") for n in (1, 2, 3)]
    return DetailSource(
        authored=items,
        snapshots={f"github.com/acme/widgets#{n}": make_snapshot(n) for n in (1, 2, 3)},
    )


def test_collapsed_changes_issue_no_diff_request() -> None:
    source = source_of()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)

    run(source, script)
    assert [c for c in source.calls if c[0] == "diff"] == []


def test_expanding_changes_issues_one_diff_and_re_expanding_none() -> None:
    source = source_of()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        await pilot.press("3")
        await settle(app, pilot)
        assert "old line" in text_of(app, "changes")
        await pilot.press("3", "3")  # collapse, expand
        await settle(app, pilot)
        assert "old line" in text_of(app, "changes")

    run(source, script)
    assert count(source, "diff", PR1) == 1
    assert count(source, "detail", PR1) == 1  # expanding reread only the diff, not the detail


def test_expanding_while_the_detail_is_still_loading_starts_the_diff_once() -> None:
    source = source_of()
    hold(source, "detail")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await wait_until(pilot, lambda: ("detail", PR1) in source.calls)
        await pilot.press("3")
        await wait_until(pilot, lambda: ("diff", PR1) in source.calls)
        release(source, "detail")
        await settle(app, pilot)
        assert "old line" in text_of(app, "changes")

    run(source, script)
    assert count(source, "diff", PR1) == 1
    assert count(source, "detail", PR1) == 1


def test_an_already_open_changes_section_fetches_on_open() -> None:
    source = source_of()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)

    run(source, script, collapsed=())
    assert count(source, "diff", PR1) == 1


def test_reopening_an_unchanged_pr_issues_no_detail_or_diff_request() -> None:
    source = source_of()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        await pilot.press("3")
        await settle(app, pilot)
        await pilot.press("escape")
        await pilot.press("enter")
        await settle(app, pilot)
        assert "old line" in text_of(app, "changes")
        assert app.query_one("#detail").display is True

    run(source, script)
    assert count(source, "detail", PR1) == 1
    assert count(source, "diff", PR1) == 1


def test_a_changed_head_rereads_the_detail_and_the_diff_and_shows_the_last_good_meanwhile() -> None:
    source = source_of()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        await pilot.press("3", "escape")
        await settle(app, pilot)
        # the PR moves on: new head sha and update time in the list
        item = source.authored[0]
        item["head"] = {**item["head"], "sha": "b" * 40}
        item["updated_at"] = "2026-11-04T10:00:00Z"
        source.snapshots[PR1] = make_snapshot(1, title="Mine", body="newer body")
        source.diffs[PR1] = make_diff(PR1, "@@ -1 +1 @@\n-x\n+newer line\n")
        await pilot.press("R")
        await settle(app, pilot)
        await pilot.press("escape")
        hold(source, "detail", "diff")
        mark = (count(source, "detail", PR1), count(source, "diff", PR1))
        await pilot.press("enter")
        await wait_until(pilot, lambda: count(source, "detail", PR1) > mark[0])
        await pilot.pause()
        assert count(source, "diff", PR1) > mark[1]
        assert "loading" not in text_of(app, "description")  # the last good detail, no flash
        release(source, "detail", "diff")
        await settle(app, pilot)
        assert "newer line" in text_of(app, "changes")

    run(source, script)


def test_prefetch_reads_the_neighbors_detail_never_the_diff_and_opening_uses_it() -> None:
    source = three_rows()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "down")  # the middle row: both others are its neighbors
        await settle(app, pilot)
        middle = app._highlighted_row()
        assert middle is not None
        others = [pr for pr in (PR1, PR2, PR3) if pr != middle.pr]
        await wait_until(pilot, lambda: all(count(source, "detail", pr) for pr in others))
        await settle(app, pilot)
        before = list(source.calls)
        app._start_detail(others[0])  # opening a prefetched PR reads no detail
        await settle(app, pilot)
        assert source.calls == before
        assert app.query_one("#detail").display is True

    run(source, script)
    assert [c for c in source.calls if c[0] == "diff"] == []


def test_prefetch_waits_for_the_list_to_settle() -> None:
    source = three_rows()
    hold(source, "snapshot", pr=PR3)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        pass

    app = BrowseApp(source, FakeOpener(), concurrency=2)

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await wait_until(pilot, lambda: ("snapshot", PR3) in source.calls)
            await pilot.pause(0.3)
            assert [c for c in source.calls if c[0] == "detail"] == []
            release(source, "snapshot", pr=PR3)
            await settle(app, pilot)
            await wait_until(pilot, lambda: any(c[0] == "detail" for c in source.calls))

    asyncio.run(go())


def test_prefetch_never_has_more_in_flight_than_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    source = three_rows()
    lock = threading.Lock()
    state = {"now": 0, "max": 0}
    original = source.detail

    def counting(pr: str, **kwargs: Any) -> dict[str, Any]:
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        try:
            threading.Event().wait(0.1)
            return original(pr, **kwargs)
        finally:
            with lock:
                state["now"] -= 1

    source.detail = counting  # type: ignore[method-assign]
    monkeypatch.setattr(app_module, "PREFETCH_MAX", 1)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down")
        await settle(app, pilot)
        await pilot.pause(0.5)
        await settle(app, pilot)

    run(source, script)
    assert state["max"] == 1
    assert count(source, "detail", PR1) + count(source, "detail", PR3) == 2  # both, one at a time


def test_prefetch_skips_a_rate_limited_host() -> None:
    source = three_rows()

    def limited(app: BrowseApp) -> None:
        app._rates.low = lambda host, now: True  # type: ignore[method-assign]

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down")
        await settle(app, pilot)
        await pilot.pause(0.3)

    run(source, script, setup=limited)
    assert [c for c in source.calls if c[0] == "detail"] == []


def test_a_host_that_stopped_a_prefetch_is_not_asked_again_until_the_next_cycle() -> None:
    from forgectl.tui.source_live import HostStopped

    source = three_rows()

    def stopping(pr: str, **kwargs: Any) -> dict[str, Any]:
        source.calls.append(("detail", pr))
        raise HostStopped("rate_limited", "the host is out of calls")

    source.detail = stopping  # type: ignore[method-assign]

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down")
        await settle(app, pilot)
        await pilot.pause(0.3)
        assert "github.com" in app._stopped_hosts

    run(source, script)
    assert len([c for c in source.calls if c[0] == "detail"]) <= 2


def test_a_read_for_a_row_the_highlight_left_is_cancelled() -> None:
    source = three_rows()
    seen: dict[str, Any] = {}

    def left(pr: str, *, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        source.calls.append(("detail", pr))
        assert cancelled is not None
        deadline = 100
        while not cancelled() and deadline:
            threading.Event().wait(0.05)
            deadline -= 1
        seen[pr] = cancelled()
        raise RuntimeError("stop")

    source.detail = left  # type: ignore[method-assign]

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await wait_until(pilot, lambda: ("detail", PR2) in source.calls)
        app._prefetch_keep = set()  # as when the highlight is far away
        await settle(app, pilot)

    run(source, script)
    assert seen[PR2] is True
