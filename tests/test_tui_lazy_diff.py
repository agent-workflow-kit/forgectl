"""`browse` reads the diff only while Changes is open, and caches and prefetches details (#120)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import OptionList

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
from .tui_fakes import FakeOpener, make_check, make_diff, make_item, make_snapshot

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


def rows_of(n: int) -> DetailSource:
    items = [
        make_item(k, title=f"PR {k}", updated_at=f"2026-11-03T1{k}:00:00Z") for k in range(1, n + 1)
    ]
    return DetailSource(
        authored=items,
        snapshots={f"github.com/acme/widgets#{k}": make_snapshot(k) for k in range(1, n + 1)},
    )


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
        assert app._stopped_hosts == {"github.com"}

    run(source, script)
    assert len([c for c in source.calls if c[0] == "detail"]) <= 2


def test_a_read_for_a_row_the_highlight_left_is_cancelled() -> None:
    source = rows_of(8)
    seen: dict[str, bool] = {}
    moved = threading.Event()

    def left(pr: str, *, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        source.calls.append(("detail", pr))
        assert cancelled is not None
        moved.wait(10.0)  # the host call is under way until the highlight has moved
        seen.setdefault(pr, cancelled())  # asked before its next host call
        return source.snapshots[pr]

    source.detail = left  # type: ignore[method-assign]

    app = BrowseApp(source, FakeOpener(), concurrency=2)

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            # no settle: the read ahead of the first neighbor is still in flight
            await wait_until(pilot, lambda: any(c[0] == "detail" for c in source.calls))
            first = next(pr for name, pr in source.calls if name == "detail")
            assert app._visible.index(app._row_by_pr[first]) <= 2
            app.query_one("#list", OptionList).highlighted = len(app._visible) - 1  # far away
            await wait_until(pilot, lambda: first not in app._prefetch_keep)
            moved.set()
            await wait_until(pilot, lambda: first in seen)
            await settle(app, pilot)
            assert seen[first] is True

    asyncio.run(go())


def test_prefetch_in_flight_stays_within_the_default_bound() -> None:
    source = rows_of(5)
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
    assert app_module.PREFETCH_MAX == 2

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "down")
        await settle(app, pilot)
        await pilot.pause(0.5)
        await settle(app, pilot)

    run(source, script)
    assert state["max"] == 2
    reads = [pr for name, pr in source.calls if name == "detail"]
    assert len(reads) == len(set(reads)) >= 4  # each neighbor of each stop once, never twice


def _neighbors_get_one_read_each(source: DetailSource, *, after_refresh: bool) -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down")  # the middle row: both others are its neighbors
        await settle(app, pilot)
        await pilot.pause(0.5)
        await settle(app, pilot)
        middle = app._highlighted_row()
        assert middle is not None
        others = [pr for pr in (PR1, PR2, PR3) if pr != middle.pr]
        assert [count(source, "detail", pr) for pr in others] == [1, 1]
        if after_refresh:
            await pilot.press("R")  # the next cycle may try again, once
            await settle(app, pilot)
            await pilot.pause(0.5)
            await settle(app, pilot)
            assert [count(source, "detail", pr) for pr in others] == [2, 2]

    run(source, script)


def test_a_failing_neighbor_is_read_once_until_the_next_refresh_cycle() -> None:
    source = three_rows()

    def failing(pr: str, **kwargs: Any) -> dict[str, Any]:
        source.calls.append(("detail", pr))
        raise RuntimeError("down")

    source.detail = failing  # type: ignore[method-assign]
    _neighbors_get_one_read_each(source, after_refresh=True)


def test_an_incomplete_neighbor_is_read_once_until_the_next_refresh_cycle() -> None:
    source = three_rows()
    original = source.detail

    def cut(pr: str, **kwargs: Any) -> dict[str, Any]:
        return {**original(pr, **kwargs), "complete": False}

    source.detail = cut  # type: ignore[method-assign]
    _neighbors_get_one_read_each(source, after_refresh=True)


def test_a_neighbor_whose_ci_drifted_from_the_row_is_read_once() -> None:
    source = three_rows()
    for n in (1, 2, 3):
        source.fresh[f"github.com/acme/widgets#{n}"] = make_snapshot(
            n, checks=[make_check(state="failing")]
        )
    _neighbors_get_one_read_each(source, after_refresh=False)


def test_a_changed_row_is_prefetched_again_without_a_refresh_cycle() -> None:
    source = three_rows()

    def failing(pr: str, **kwargs: Any) -> dict[str, Any]:
        source.calls.append(("detail", pr))
        raise RuntimeError("down")

    source.detail = failing  # type: ignore[method-assign]

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down")
        await settle(app, pilot)
        await pilot.pause(0.3)
        row = app._row_by_pr[PR1 if app._highlighted_row().pr != PR1 else PR3]  # type: ignore[union-attr]
        before = count(source, "detail", row.pr)
        row.item = {**row.item, "updated_at": "2026-11-09T10:00:00Z"}  # its key moved on
        app._maybe_prefetch()
        await pilot.pause(0.3)
        assert count(source, "detail", row.pr) == before + 1

    run(source, script)


def slow_frames(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """Make the pane's frame build take a while while ``slow["on"]``."""
    slow = {"on": False}
    original = BrowseApp._frame

    def slow_frame(*args: Any) -> Any:
        if slow["on"]:
            threading.Event().wait(0.4)
        return original(*args)

    monkeypatch.setattr(BrowseApp, "_frame", staticmethod(slow_frame))
    return slow


def test_changes_opened_while_another_pr_loads_starts_that_prs_diff_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = three_rows()
    slow = slow_frames(monkeypatch)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        app._start_detail(PR1)
        await settle(app, pilot)
        assert app._detail_pr == PR1
        slow["on"] = True
        app._start_detail(PR2)  # the pane still names PR1 until PR2's first frame is painted
        await wait_until(pilot, lambda: app._detail_loading == PR2)
        assert app._detail_pr == PR1
        await pilot.press("3")
        await wait_until(pilot, lambda: ("diff", PR2) in source.calls)
        slow["on"] = False
        await settle(app, pilot)
        assert app._detail_pr == PR2

    run(source, script)
    assert count(source, "diff", PR1) == 0
    assert count(source, "diff", PR2) == 1
    assert count(source, "detail", PR2) <= 1


def test_changes_opened_while_the_first_frame_paints_still_reads_the_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = three_rows()
    slow = slow_frames(monkeypatch)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        app._start_detail(PR1)
        await settle(app, pilot)
        await pilot.press("down")  # PR2 is prefetched: its detail needs no read
        await wait_until(pilot, lambda: ("detail", PR2) in source.calls)
        await settle(app, pilot)
        slow["on"] = True
        app._start_detail(PR2)
        await wait_until(pilot, lambda: app._detail_wake is not None)
        await pilot.press("3")  # while nothing is pending: the load is about to end
        await wait_until(pilot, lambda: ("diff", PR2) in source.calls)
        slow["on"] = False
        await settle(app, pilot)
        assert count(source, "detail", PR2) == 1

    run(source, script)
    assert count(source, "diff", PR2) == 1


def test_light_mode_keeps_the_cached_detail_under_the_adopted_items_key() -> None:
    source = source_of()
    # the detail read answers with a newer update time than the list item the row holds
    source.fresh[PR1] = {**make_snapshot(1, title="Mine"), "updated_at": "2026-11-04T10:00:00Z"}

    def light(app: BrowseApp) -> None:
        app._light = True  # pyright: ignore[reportAttributeAccessIssue]
        app._maybe_prefetch = lambda: None  # type: ignore[method-assign]  # the open itself reads

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        await pilot.press("escape")
        await pilot.press("enter")
        await settle(app, pilot)

    run(source, script, setup=light)
    assert count(source, "detail", PR1) == 1


def test_a_poll_that_moves_the_row_during_a_read_does_not_cache_the_content_under_the_new_key() -> (
    None
):
    source = source_of()
    # the read carries no list item (no update time), so the row keeps whatever a poll gave it
    fresh = make_snapshot(1, title="Mine")
    fresh.pop("updated_at", None)
    source.fresh[PR1] = fresh
    hold(source, "detail")

    def light(app: BrowseApp) -> None:
        app._light = True  # pyright: ignore[reportAttributeAccessIssue]
        app._maybe_prefetch = lambda: None  # type: ignore[method-assign]  # the open itself reads

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await wait_until(pilot, lambda: ("detail", PR1) in source.calls)
        row = app._row_by_pr[PR1]
        row.item = {**row.item, "updated_at": "2026-11-05T10:00:00Z"}  # a light poll moved it
        release(source, "detail")
        await settle(app, pilot)
        row.item = {**row.item, "updated_at": "2026-11-05T10:00:00Z"}  # the next poll: still T2
        await pilot.press("escape")
        await pilot.press("enter")
        await settle(app, pilot)

    run(source, script, setup=light)
    assert count(source, "detail", PR1) == 2  # the older content is not served as current


def test_changes_opened_while_another_pr_loads_ignores_the_shown_prs_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = three_rows()
    block = {"on": False}
    release_frame = threading.Event()
    original = BrowseApp._frame

    def blocked_frame(*args: Any) -> Any:
        if block["on"]:
            release_frame.wait(10.0)  # the first frame of the load stays unpainted
        return original(*args)

    monkeypatch.setattr(BrowseApp, "_frame", staticmethod(blocked_frame))

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        app._start_detail(PR1)
        await settle(app, pilot)
        assert app._diff_in_pane  # PR1 is shown with its diff
        await pilot.press("3")  # collapse Changes
        block["on"] = True
        app._start_detail(PR2)
        await wait_until(pilot, lambda: app._detail_wake is not None)
        assert app._detail_pr == PR1 and app._diff_in_pane
        await pilot.press("3")  # open Changes while PR2 loads
        wake = app._detail_wake
        assert wake is not None
        try:
            await wait_until(pilot, wake.done, timeout=5.0)  # PR2 is told, whatever PR1 shows
            assert not wake.cancelled()
            await wait_until(pilot, lambda: ("diff", PR2) in source.calls)
        finally:
            block["on"] = False
            release_frame.set()
        await settle(app, pilot)

    run(source, script, collapsed=())
    assert count(source, "diff", PR2) == 1
