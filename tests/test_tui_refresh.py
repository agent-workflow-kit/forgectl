"""`browse` manual and automatic refresh (#59): what is re-read, what stays on screen, backoff.

The cycle logic is driven with ``app._tick()`` and a fake clock; one test lets the real timer
fire to show that the interval starts it and that 0 does not.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import OptionList

from forgectl.errors import ForgectlError
from forgectl.tui import columns
from forgectl.tui import run as tui_run
from forgectl.tui.app import BrowseApp
from forgectl.tui.refresh import RateLimits, host_of
from forgectl.tui.source_live import Cancelled

from .test_tui_app import content_of, list_prompts, settle
from .test_tui_columns import GH, GL, order_of, prefix, prompt_of, snap
from .tui_fakes import FakeOpener, FakeSource, make_item

T0 = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)


class RefreshSource(FakeSource):
    """Switches: ``failing`` PRs raise, ``hold`` blocks snapshots until ``gate`` is set."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.failing: set[str] = set()
        self.prs_failing = False
        self.partial = False
        self.partial_hosts = ["h.example", "i.example"]  # the hosts a partial answer names
        self.hold = False
        self.gate = threading.Event()
        self.cancel_probes: list[Callable[[], bool]] = []
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.nap = 0.0

    def prs(self, **kw: Any) -> dict[str, Any]:
        if self.prs_failing:
            self.calls.append(("prs", kw))
            raise ForgectlError("upstream_error", "boom")
        answer = super().prs(**kw)
        if self.partial:
            answer["complete"] = False
            answer["errors"] = [
                {"host": host, "code": "upstream_error", "message": "x"}
                for host in self.partial_hosts
            ]
        return answer

    def snapshot(self, pr: str, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        if cancelled is not None:
            self.cancel_probes.append(cancelled)
            if cancelled():  # as ``LiveSource`` does: refuse a call a newer round replaced
                raise Cancelled(pr)
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            if self.nap:
                time.sleep(self.nap)
            if self.hold:
                self.gate.wait(10)
            if pr in self.failing:
                self.calls.append(("snapshot", pr))
                raise ForgectlError("upstream_error", "boom")
            return super().snapshot(pr)
        finally:
            with self.lock:
                self.running -= 1

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)

    def snapshotted(self) -> list[str]:
        return sorted(str(c[1]) for c in self.calls if c[0] == "snapshot")


def three() -> RefreshSource:
    items = [make_item(n, title=f"pr {n}") for n in (1, 2, 3)]
    return RefreshSource(authored=items, snapshots={i["pr"]: snap(i["number"]) for i in items})


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def run(
    source: FakeSource,
    script: Callable[[BrowseApp, Pilot[None]], Any],
    *,
    interval: float = 0,
    concurrency: int = 4,
    full_every: int = 5,
    clock: Callable[[], datetime] | None = None,
    size: tuple[int, int] = (140, 50),
    wait: bool = True,
) -> BrowseApp:
    extra: dict[str, Any] = {"clock": clock} if clock else {}
    app = BrowseApp(
        source,
        FakeOpener(),
        concurrency=concurrency,
        refresh_interval=interval,
        full_refresh_every=full_every,
        **extra,
    )

    async def go() -> None:
        async with app.run_test(size=size) as pilot:
            if wait:
                await settle(app, pilot)
            await script(app, pilot)
            await quiesce(app, source)

    asyncio.run(go())
    return app


async def quiesce(app: BrowseApp, source: FakeSource) -> None:
    """Leave no refresh behind: a timer tick that starts a worker just as the test app closes
    would paint onto a torn-down screen (`quit` stops the timer first; leaving the test context
    does not). Stop the timer, release any held snapshot, and wait the workers out.

    The timer is stopped explicitly here, so exit paths other than `q` are not covered here."""
    if app.refresh_timer is not None:
        app.refresh_timer.stop()
    if isinstance(source, RefreshSource):
        source.gate.set()
    await app.workers.wait_for_complete()


async def until(pilot: Pilot[None], cond: Callable[[], bool], tries: int = 200) -> None:
    for _ in range(tries):
        if cond():
            return
        await pilot.pause(0.02)
    raise AssertionError("condition not reached")


async def settle_prs(source: RefreshSource) -> None:
    before = -1
    while before != source.count("prs"):
        before = source.count("prs")
        await asyncio.sleep(0.15)


async def cycle(app: BrowseApp, pilot: Pilot[None]) -> None:
    """One automatic cycle, run to its end."""
    app._tick()
    await settle(app, pilot)


def bar(app: BrowseApp) -> str:
    return content_of(app, "#bar").plain


# -- manual refresh ----------------------------------------------------------------------------


def test_r_refetches_only_the_selected_pr_and_R_refetches_all() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.calls.clear()
        await pilot.press("enter")  # the pane for the first row, then r on it
        await settle(app, pilot)
        source.calls.clear()
        await pilot.press("r")
        await settle(app, pilot)
        assert source.count("prs") == 0
        assert len(set(source.snapshotted())) == 1
        source.calls.clear()
        await pilot.press("R")
        await settle(app, pilot)
        assert source.count("prs") == 2
        assert {prefix(GH, n) for n in (1, 2, 3)} <= set(source.snapshotted())

    run(source, script)


def test_refresh_keeps_the_last_good_row_and_marks_it_refreshing() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.hold = True
        await pilot.press("R")
        await until(pilot, lambda: "[refreshing]" in prompt_of(app, prefix(GH, 1)))
        row = prompt_of(app, prefix(GH, 1))
        assert "✓" in row and "loading" not in row
        source.gate.set()
        await settle(app, pilot)
        row = prompt_of(app, prefix(GH, 1))
        assert "✓" in row and "[refreshing]" not in row and "[stale" not in row

    run(source, script)


def test_a_failed_snapshot_refresh_keeps_the_data_and_marks_the_row_stale() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.failing.add(prefix(GH, 2))
        await pilot.press("R")
        await settle(app, pilot)
        bad = prompt_of(app, prefix(GH, 2))
        assert "✓" in bad and "[stale: ups…" in bad and "ERROR" not in bad
        assert "[stale" not in prompt_of(app, prefix(GH, 1))
        assert "1 stale" in bar(app)
        source.failing.clear()  # the next good refresh clears it
        await pilot.press("R")
        await settle(app, pilot)
        assert "[stale" not in prompt_of(app, prefix(GH, 2))

    run(source, script)


def test_a_row_that_never_loaded_stays_failed_not_stale() -> None:
    source = three()
    source.failing.add(prefix(GH, 2))

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        row = prompt_of(app, prefix(GH, 2))
        assert "ERROR upstream_error" in row and "[stale" not in row

    run(source, script)


def test_a_failed_list_refresh_keeps_the_rows_and_shows_the_error() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.prs_failing = True
        await pilot.press("R")
        await settle(app, pilot)
        assert len(list_prompts(app)) == 3
        assert all("✓" in p.plain for p in list_prompts(app))
        assert "upstream_error" in content_of(app, "#status").plain

    run(source, script)


def test_an_incomplete_list_keeps_the_rows_it_did_not_return_as_stale() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.partial = True
        source.partial_hosts = ["github.com"]
        del source.authored[2]  # the failing host's PR is missing from the answer
        await pilot.press("R")
        await settle(app, pilot)
        assert len(list_prompts(app)) == 3
        assert "[stale: lis…" in prompt_of(app, prefix(GH, 3))
        source.partial = False  # a complete answer closes it
        await pilot.press("R")
        await settle(app, pilot)
        assert len(list_prompts(app)) == 2

    run(source, script)


def test_a_refresh_during_a_refresh_is_ignored_with_a_note() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        before = source.count("prs")
        source.hold = True
        await pilot.press("R")
        await until(pilot, lambda: source.running > 0)
        await pilot.press("R")
        await pilot.pause()
        assert "already running" in content_of(app, "#status").plain
        source.gate.set()
        await settle(app, pilot)
        assert source.count("prs") == before + 2  # one reload: two `prs` calls

    run(source, script)


def test_selection_and_scroll_survive_a_refresh_even_when_the_order_changes() -> None:
    items = [
        make_item(n, title=f"pr {n}", updated_at=f"2026-11-03T{n % 24:02d}:00:00Z")
        for n in range(1, 31)
    ]
    source = RefreshSource(authored=items, snapshots={i["pr"]: snap(i["number"]) for i in items})

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        olist = app.query_one("#list", OptionList)
        for _ in range(20):
            await pilot.press("down")
        await pilot.pause()
        chosen = order_of(app)[olist.highlighted or 0]
        scroll = olist.scroll_y
        assert scroll > 0
        assert order_of(app)[0].endswith("#23")
        source.authored[21]["updated_at"] = "2026-11-04T23:00:00Z"  # the order changes above
        await pilot.press("R")
        await settle(app, pilot)
        assert order_of(app)[0].endswith("#22")
        assert order_of(app)[olist.highlighted or 0] == chosen
        assert olist.scroll_y == scroll

    run(source, script, size=(140, 14))


# -- automatic refresh: when it fires ----------------------------------------------------------


def test_the_interval_fires_a_refresh_and_zero_never_does() -> None:
    source = three()

    async def ticking(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert app.refresh_timer is not None
        await until(pilot, lambda: source.count("prs") >= 4, tries=400)

    run(source, ticking, interval=0.1)

    off = three()

    async def quiet(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.pause(0.4)
        assert off.count("prs") == 2  # only the first load
        assert app.refresh_timer is None

    run(off, quiet, interval=0)


@pytest.mark.parametrize("bad", [-1.0, float("nan")])
def test_a_non_positive_or_nan_interval_means_off(bad: float) -> None:
    assert BrowseApp(three(), FakeOpener(), refresh_interval=bad).refresh_interval == 0


def test_ticks_do_not_overlap_a_refresh_in_flight() -> None:
    source = three()
    source.hold = True  # the first round blocks, and the timer keeps ticking meanwhile

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await until(pilot, lambda: source.running > 0, tries=400)
        await pilot.pause(0.5)
        assert source.count("prs") == 2  # the first load only
        source.gate.set()
        await until(pilot, lambda: source.count("prs") >= 4, tries=400)

    run(source, script, interval=0.1, wait=False)


def test_ticks_wait_while_a_modal_is_open_and_resume_after() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("question_mark")
        await pilot.pause()
        await until(pilot, lambda: source.running == 0, tries=400)
        await settle_prs(source)  # a cycle that began before the modal is allowed to end
        base = source.count("prs")
        await pilot.pause(0.4)
        assert source.count("prs") == base
        await pilot.press("x")  # closes the help
        await until(pilot, lambda: source.count("prs") > base, tries=400)

    run(source, script, interval=0.1)


def test_ticks_wait_while_a_detail_load_is_in_flight() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.hold = True
        await pilot.press("enter")
        await until(pilot, lambda: source.running > 0)
        await settle_prs(source)  # a cycle that began before the load is allowed to end
        base = source.count("prs")
        await pilot.pause(0.4)
        assert source.count("prs") == base
        source.gate.set()
        await settle(app, pilot)

    run(source, script, interval=0.1)


def test_quit_stops_the_timer() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert app.refresh_timer is not None
        await pilot.press("q")
        await pilot.pause()
        assert app.refresh_timer is None

    run(source, script, interval=0.1)


# -- automatic refresh: what it re-reads -------------------------------------------------------


def test_an_unchanged_pr_with_settled_ci_is_not_resnapshotted_in_an_ordinary_cycle() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.calls.clear()
        await cycle(app, pilot)
        assert source.count("prs") == 2  # `prs` always runs
        assert source.snapshotted() == []

    run(source, script, interval=1000)


def test_a_pr_whose_updated_at_or_head_sha_changed_is_resnapshotted() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.calls.clear()
        source.authored[0]["updated_at"] = "2026-11-03T13:00:00Z"
        source.authored[1]["head"]["sha"] = "c" * 40
        await cycle(app, pilot)
        assert source.snapshotted() == [prefix(GH, 1), prefix(GH, 2)]

    run(source, script, interval=1000)


def test_a_pr_with_pending_ci_is_resnapshotted_every_cycle() -> None:
    source = three()
    source.snapshots[prefix(GH, 2)] = snap(2, ci="pending")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        for _ in range(2):
            source.calls.clear()
            await cycle(app, pilot)
            assert source.snapshotted() == [prefix(GH, 2)]
        source.snapshots[prefix(GH, 2)] = snap(2, ci="passing")
        await cycle(app, pilot)  # it settles...
        source.calls.clear()
        await cycle(app, pilot)  # ...and is left alone
        assert source.snapshotted() == []

    run(source, script, interval=1000)


def test_an_incomplete_or_failed_row_is_retried_in_the_next_cycle() -> None:
    source = three()
    source.snapshots[prefix(GH, 1)] = snap(1, complete=False)
    source.failing.add(prefix(GH, 3))

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.calls.clear()
        await cycle(app, pilot)
        assert source.snapshotted() == [prefix(GH, 1), prefix(GH, 3)]

    run(source, script, interval=1000)


def test_every_nth_cycle_resnapshots_all() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        seen = []
        for _ in range(6):
            source.calls.clear()
            await cycle(app, pilot)
            seen.append(len(source.snapshotted()))
        assert seen == [0, 0, 3, 0, 0, 3]

    run(source, script, interval=1000, full_every=3)


def test_a_new_pr_is_snapshotted_and_highlighted_but_the_first_load_highlights_nothing() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert not any("[changed]" in p.plain for p in list_prompts(app))
        source.authored.append(make_item(4, title="pr 4"))
        source.snapshots[prefix(GH, 4)] = snap(4)
        source.calls.clear()
        await cycle(app, pilot)
        assert source.snapshotted() == [prefix(GH, 4)]
        assert "[changed]" in prompt_of(app, prefix(GH, 4))

    run(source, script, interval=1000)


def test_a_manual_R_resnapshots_all_and_restarts_the_count() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await cycle(app, pilot)
        await cycle(app, pilot)
        await pilot.press("R")
        await settle(app, pilot)
        source.calls.clear()
        await cycle(app, pilot)  # the first after R: ordinary, not the third
        assert source.snapshotted() == []

    run(source, script, interval=1000, full_every=3)


def test_a_total_prs_failure_does_not_stamp_a_refresh_time() -> None:
    source = three()
    clock = Clock()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert "refreshed 12:00:00" in bar(app)
        source.prs_failing = True
        clock.now = T0 + timedelta(minutes=5)
        await pilot.press("R")
        await settle(app, pilot)
        assert "refreshed 12:00:00" in bar(app) and "12:05" not in bar(app)

    run(source, script, clock=clock)


def two_hosts() -> RefreshSource:
    gh_item, gl_item = make_item(1, repo=GH), make_item(1, repo=GL)
    return RefreshSource(
        authored=[gh_item, gl_item],
        snapshots={prefix(GH, 1): snap(1), prefix(GL, 1): snap(1, repo=GL)},
    )


def test_an_incomplete_list_keeps_only_the_unlisted_prs_of_the_failed_host() -> None:
    source = two_hosts()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.partial = True
        source.partial_hosts = ["gitlab.example"]
        source.authored.clear()  # both missing: one was closed, one is hidden by the failure
        await pilot.press("R")
        await settle(app, pilot)
        assert len(list_prompts(app)) == 1
        assert "[stale: lis…" in prompt_of(app, prefix(GL, 1))
        source.partial = False
        await pilot.press("R")
        await settle(app, pilot)
        assert len(list_prompts(app)) == 0

    run(source, script)


def test_an_incomplete_list_naming_no_host_keeps_every_unlisted_pr() -> None:
    source = two_hosts()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.prs_failing = True  # the call failed whole: no host is named
        await pilot.press("R")
        await settle(app, pilot)
        assert len(list_prompts(app)) == 2

    run(source, script)


def test_a_never_loaded_row_keeps_its_error_when_the_list_goes_incomplete() -> None:
    source = three()
    source.failing.add(prefix(GH, 3))

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert "ERROR upstream_error" in prompt_of(app, prefix(GH, 3))
        source.partial = True
        source.partial_hosts = ["github.com"]
        del source.authored[2]
        await pilot.press("R")
        await settle(app, pilot)
        row = prompt_of(app, prefix(GH, 3))
        assert "ERROR upstream_error" in row and "list incomplete" not in row

    run(source, script)


# -- changed rows ------------------------------------------------------------------------------


def test_a_changed_row_is_highlighted_until_the_next_cycle() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.authored[0]["updated_at"] = "2026-11-03T13:00:00Z"
        source.snapshots[prefix(GH, 1)] = snap(1, ci="failing")
        await cycle(app, pilot)
        changed = prompt_of(app, prefix(GH, 1))
        assert "[changed]" in changed and "✗" in changed
        assert "[changed]" not in prompt_of(app, prefix(GH, 2))
        assert any(p.plain.startswith(prefix(GH, 1)) and p.spans for p in list_prompts(app)), (
            "the changed row is styled, not only labelled"
        )
        await cycle(app, pilot)  # nothing changed this time
        assert "[changed]" not in prompt_of(app, prefix(GH, 1))

    run(source, script, interval=1000)


def test_a_changed_row_stays_highlighted_while_its_detail_is_open_and_is_read_once() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        assert app._detail_pr == prefix(GH, 1)
        source.authored[0]["updated_at"] = "2026-11-03T13:00:00Z"
        source.snapshots[prefix(GH, 1)] = snap(1, ci="failing")
        source.calls.clear()
        await cycle(app, pilot)
        assert "[changed]" in prompt_of(app, prefix(GH, 1))
        assert source.snapshotted() == [prefix(GH, 1)]  # one read serves the row and the detail
        assert [c for c in source.calls if c[0] == "diff"]  # the detail still gets its diff

    run(source, script, interval=1000)


def test_a_keypress_clears_the_highlight() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.authored[0]["updated_at"] = "2026-11-03T13:00:00Z"
        source.snapshots[prefix(GH, 1)] = snap(1, ci="failing")
        await cycle(app, pilot)
        assert "[changed]" in prompt_of(app, prefix(GH, 1))
        await pilot.press("down")
        await pilot.pause()
        assert not any("[changed]" in p.plain for p in list_prompts(app))

    run(source, script, interval=1000)


def test_a_resnapshot_that_changes_nothing_shown_does_not_highlight() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.authored[0]["updated_at"] = "2026-11-03T13:00:00Z"  # same snapshot as before
        await cycle(app, pilot)
        assert not any("[changed]" in p.plain for p in list_prompts(app))

    run(source, script, interval=1000)


def test_the_detail_pane_snapshot_also_refreshes_its_row() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        source.snapshots[prefix(GH, 1)] = snap(1, ci="failing")
        await pilot.press("r")
        await settle(app, pilot)
        assert "✗" in prompt_of(app, prefix(GH, 1))

    run(source, script)


def test_a_superseded_detail_read_still_refreshes_its_row() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        a, b = prefix(GH, 1), prefix(GH, 2)
        source.snapshots[a] = snap(1, ci="failing")
        source.hold = True
        app._start_detail(a)
        await until(pilot, lambda: source.running > 0)
        app._start_detail(b)  # replaces A's request before its read returns
        source.gate.set()
        await settle(app, pilot)
        assert "✗" in prompt_of(app, a)

    run(source, script)


def test_a_superseded_detail_read_that_fails_marks_its_row_stale() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        a, b = prefix(GH, 1), prefix(GH, 2)
        source.failing.add(a)
        source.hold = True
        app._start_detail(a)
        await until(pilot, lambda: source.running > 0)
        app._start_detail(b)
        source.gate.set()
        await settle(app, pilot)
        assert "[stale: ups…" in prompt_of(app, a)

    run(source, script)


def test_a_detail_read_that_fails_on_a_still_loading_row_marks_it_failed() -> None:
    source = three()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        a = prefix(GH, 1)
        app._states[a] = columns.RowState(columns.LOADING)
        app._row_failed(a, "upstream_error")
        assert app._states[a].status == columns.FAILED
        assert app._states[a].error == "upstream_error"

    run(source, script)


# -- status bar and rate limits ----------------------------------------------------------------


def test_the_bar_shows_the_refresh_time_the_rate_limit_and_the_partial_error_count() -> None:
    source = three()
    clock = Clock()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        text = bar(app)
        assert "refreshed 12:00:00" in text
        assert "github.com 4000" in text
        assert "partial" not in text
        source.partial = True
        clock.now = T0 + timedelta(minutes=5)
        await pilot.press("R")
        await settle(app, pilot)
        text = bar(app)
        assert "refreshed 12:05:00" in text and "partial: 2 errors" in text

    run(source, script, clock=clock)


def test_the_rate_parts_come_before_the_ones_a_narrow_bar_may_clip() -> None:
    source = gl_source(remaining=5, reset_at="2026-11-03T12:30:00Z")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("a")  # any filter adds its part to the bar
        text = bar(app)
        assert "slow:" in text and "refreshed" in text and "auto" in text
        for later in ("refreshed", "auto 1000s"):
            assert text.index("rate:") < text.index(later)
            assert text.index("slow:") < text.index(later)
        if "filters:" in text:
            assert text.index("slow:") < text.index("filters:")

    run(source, script, interval=1000, clock=Clock(), size=(80, 30))


def test_hostile_host_names_in_the_rate_limit_are_sanitized() -> None:
    source = three()
    source.snapshots[prefix(GH, 1)]["rate_limit"] = {
        "host": "evil\x1b[31m.example\n",
        "remaining": 10,
        "reset_at": "2026-11-03T13:00:00Z",
    }

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert "\x1b" not in bar(app) and "\n" not in bar(app)

    run(source, script)


def gl_source(remaining: int, reset_at: str) -> RefreshSource:
    gh_item = make_item(1, repo=GH)
    gl_item = make_item(1, repo=GL)
    source = RefreshSource(
        authored=[gh_item, gl_item],
        snapshots={prefix(GH, 1): snap(1), prefix(GL, 1): snap(1, repo=GL)},
    )
    source.snapshots[prefix(GL, 1)]["rate_limit"] = {
        "host": "gitlab.example",
        "remaining": remaining,
        "reset_at": reset_at,
    }
    return source


def test_a_host_below_the_threshold_is_left_alone_then_lengthened_then_recovers() -> None:
    reset = "2026-11-04T12:00:00Z"  # a day after the fake clock
    source = gl_source(remaining=5, reset_at=reset)
    clock = Clock()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert "slow" in bar(app) and "gitlab.example" in bar(app)
        for src in (source.authored[0], source.authored[1]):
            src["updated_at"] = "2026-11-03T13:00:00Z"  # both PRs changed
        source.calls.clear()
        await cycle(app, pilot)  # an ordinary cycle at once: only the healthy host
        assert source.snapshotted() == [prefix(GH, 1)]
        source.calls.clear()
        clock.now = T0 + timedelta(seconds=1000 * 4 + 1)  # the lengthened interval has passed
        await cycle(app, pilot)
        assert source.snapshotted() == [prefix(GL, 1)]
        source.authored[1]["updated_at"] = "2026-11-03T14:00:00Z"
        source.snapshots[prefix(GL, 1)]["rate_limit"]["remaining"] = 4000
        clock.now = T0 + timedelta(days=1, seconds=1)  # past reset_at: the normal interval
        source.calls.clear()
        await cycle(app, pilot)
        assert source.snapshotted() == [prefix(GL, 1)]
        assert "slow" not in bar(app)

    run(source, script, interval=1000, clock=clock)


def test_a_manual_R_ignores_the_backoff() -> None:
    source = gl_source(remaining=5, reset_at="2026-11-04T12:00:00Z")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.calls.clear()
        await pilot.press("R")
        await settle(app, pilot)
        assert prefix(GL, 1) in source.snapshotted()

    run(source, script, interval=1000, clock=Clock())


def test_the_rate_limit_tracker() -> None:
    limits = RateLimits(threshold=100)
    limits.observe({"host": "a.example", "remaining": 99, "reset_at": "2026-11-03T13:00:00Z"}, T0)
    limits.observe({"host": "b.example", "remaining": 100, "reset_at": "2026-11-03T13:00:00Z"}, T0)
    assert limits.low("a.example", T0) and not limits.low("b.example", T0)
    assert not limits.low("a.example", T0 + timedelta(hours=1, seconds=1))  # after reset_at
    assert not limits.low("never.example", T0)
    limits.observe({"host": "a.example", "remaining": 500, "reset_at": None}, T0)
    assert not limits.low("a.example", T0)  # a newer reading replaces the older one


def test_a_far_future_reset_holds_for_an_hour_at_most() -> None:
    limits = RateLimits(threshold=100)
    limits.observe({"host": "a.example", "remaining": 1, "reset_at": "9999-12-31T00:00:00Z"}, T0)
    assert limits.low("a.example", T0 + timedelta(minutes=59))
    assert not limits.low("a.example", T0 + timedelta(hours=1, seconds=1))
    assert "until 13:00:00" in limits.text(T0)


def test_the_slow_until_time_is_shown_in_utc() -> None:
    limits = RateLimits(threshold=100)
    limits.observe(
        {"host": "a.example", "remaining": 1, "reset_at": "2026-11-03T17:30:00+05:00"}, T0
    )
    assert "until 12:30:00" in limits.text(T0)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "x",
        [],
        {},
        {"host": None, "remaining": 1, "reset_at": None},
        {"host": "h", "remaining": None, "reset_at": None},
        {"host": "h", "remaining": True, "reset_at": None},
        {"host": "h", "remaining": -1, "reset_at": None},
        {"host": "h", "remaining": "5", "reset_at": None},
        {"host": 5, "remaining": 5, "reset_at": None},
    ],
)
def test_the_tracker_ignores_malformed_rate_limits(bad: Any) -> None:
    limits = RateLimits()
    limits.observe(bad, T0)
    assert limits.text(T0) == ""
    assert not limits.low("h", T0)


def test_an_unknown_reset_ends_the_backoff_after_a_while() -> None:
    limits = RateLimits(threshold=100)
    limits.observe({"host": "a", "remaining": 1, "reset_at": None}, T0)
    assert limits.low("a", T0 + timedelta(minutes=5))
    assert not limits.low("a", T0 + timedelta(hours=1))


def test_host_of() -> None:
    assert host_of("github.com/acme/widgets#7") == "github.com"
    assert host_of("nohost") == "nohost"


# -- tokens and bounded threads ----------------------------------------------------------------


def test_an_older_round_is_cancelled_and_never_exceeds_the_thread_bound() -> None:
    items = [make_item(n) for n in range(1, 13)]
    source = RefreshSource(authored=items, snapshots={i["pr"]: snap(i["number"]) for i in items})
    source.nap = 0.05

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.cancel_probes.clear()
        source.peak = 0
        app._start_snapshots(None)  # a round...
        await pilot.pause(0.02)
        app._start_snapshots(None)  # ...replaced by a newer one while its calls run
        await settle(app, pilot)
        assert source.peak <= 2  # the one shared bound holds across the two rounds
        assert any(probe() for probe in source.cancel_probes)
        assert not all(probe() for probe in source.cancel_probes)
        assert "snapshots 12/12" in bar(app)
        assert not any("ERROR" in p.plain for p in list_prompts(app))  # Cancelled is no failure

    run(source, script, concurrency=2)


# -- settings: flag over config ----------------------------------------------------------------


class Recorder:
    kwargs: dict[str, Any] = {}
    return_code = 0

    def __init__(self, *a: Any, **kw: Any) -> None:
        Recorder.kwargs = kw

    def run(self) -> None:
        return None

    def result(self) -> dict[str, Any]:
        return {"intent": "back", "selected": [], "merged": []}


def run_with(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config_text: str | None,
    interval: float | None,
    source: Any = None,
) -> dict[str, Any]:
    monkeypatch.setattr(tui_run, "BrowseApp", Recorder)
    config = None
    if config_text is not None:
        config = str(tmp_path / "c.yaml")
        Path(config).write_text(config_text, encoding="utf-8")
    tui_run.run_browse(focus=None, config_path=config, interval=interval, source=source)
    return Recorder.kwargs


def test_the_config_decides_when_there_is_no_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text = "browse: {refresh_interval: 30s, full_refresh_every: 7, concurrency: 2}\n"
    kw = run_with(monkeypatch, tmp_path, text, None)
    assert (kw["refresh_interval"], kw["full_refresh_every"], kw["concurrency"]) == (30.0, 7, 2)


def test_the_default_is_two_minutes_five_cycles_four_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kw = run_with(monkeypatch, tmp_path, "", None)
    assert (kw["refresh_interval"], kw["full_refresh_every"], kw["concurrency"]) == (120.0, 5, 4)


@pytest.mark.parametrize("flag", [0.0, 5.0])
def test_the_flag_wins_over_the_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: float
) -> None:
    kw = run_with(monkeypatch, tmp_path, "browse: {refresh_interval: 30s}\n", flag)
    assert kw["refresh_interval"] == flag


def test_the_config_can_turn_the_refresh_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert (
        run_with(monkeypatch, tmp_path, "browse: {refresh_interval: 0}\n", None)["refresh_interval"]
        == 0
    )


def test_an_invalid_config_stops_before_the_app_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ForgectlError) as caught:
        run_with(monkeypatch, tmp_path, "browse: {refresh_interval: soon}\n", None)
    assert caught.value.code == "config_invalid"


def test_a_given_source_does_not_read_the_config_and_does_not_poll_unless_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kw = run_with(monkeypatch, tmp_path, "browse: {refresh_interval: soon}\n", None, three())
    assert kw["refresh_interval"] == 0
