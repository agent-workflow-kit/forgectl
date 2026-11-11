"""The detail pane of `browse` opens from loaded data (#119): the first frame is what the list
and the row's snapshot hold, then the detail read and the diff, started together, fill it in.
A fake source with a `detail` read, each read gated by its own event."""

from __future__ import annotations

import asyncio
import copy
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

from forgectl.backends.github_bulk import read_bulk
from forgectl.errors import ForgectlError
from forgectl.tui import render
from forgectl.tui.app import BrowseApp
from forgectl.tui.source_live import HostStopped

from .bulk_support import BulkRouter
from .harness.forges import FakeForges
from .test_bulk_github import per_pr_without_required, rig
from .test_github_snapshot import NOW, REF
from .test_tui_app import settle, status_of, wait_until
from .tui_fakes import FakeOpener, FakeSource, make_diff, make_item, make_snapshot

PR1 = "github.com/acme/widgets#1"
PR2 = "github.com/acme/widgets#2"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))


class DetailSource(FakeSource):
    """A source with a ``detail`` read. ``fresh`` is what ``detail`` answers (default: the
    snapshot); each read waits for its gate in ``gates`` (an event per read name and PR)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fresh: dict[str, dict[str, Any]] = {}
        self.fail: dict[str, ForgectlError] = {}
        self.gates: dict[tuple[str, str], threading.Event] = {}
        self.lock = threading.Lock()

    def gate(self, name: str, pr: str) -> threading.Event:
        return self.gates.setdefault((name, pr), threading.Event())

    def _wait(self, name: str, pr: str) -> None:
        event = self.gates.get((name, pr))
        if event is not None:
            event.wait(10.0)

    def _note(self, name: str, pr: str) -> None:
        with self.lock:
            self.calls.append((name, pr))

    def detail(self, pr: str, *, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        self._note("detail", pr)
        self._wait("detail", pr)
        if ("detail", pr) in self.fail_map():
            raise self.fail[f"detail:{pr}"]
        return copy.deepcopy(self.fresh.get(pr) or self.snapshots[pr])

    def fail_map(self) -> set[tuple[str, str]]:
        return {(k.split(":", 1)[0], k.split(":", 1)[1]) for k in self.fail}

    def snapshot(self, pr: str) -> dict[str, Any]:
        self._note("snapshot", pr)
        self._wait("snapshot", pr)
        if ("snapshot", pr) in self.fail_map():
            raise self.fail[f"snapshot:{pr}"]
        return copy.deepcopy(self.snapshots[pr])

    def diff(self, pr: str) -> dict[str, Any]:
        self._note("diff", pr)
        self._wait("diff", pr)
        if ("diff", pr) in self.fail_map():
            raise self.fail[f"diff:{pr}"]
        return copy.deepcopy(self.diffs.get(pr) or make_diff(pr))


def source_of(**kwargs: Any) -> DetailSource:
    return DetailSource(
        authored=[make_item(1, title="Mine", updated_at="2026-11-03T10:00:00Z")],
        requested=[make_item(2, title="Theirs", login="carol", updated_at="2026-11-03T11:00:00Z")],
        snapshots={
            PR1: make_snapshot(1, title="Mine", body="old body"),
            PR2: make_snapshot(2, title="Theirs", body="their body"),
        },
        diffs={PR1: make_diff(PR1, "@@ -1 +1 @@\n-old line\n+new line\n")},
        **kwargs,
    )


def drive(
    source: FakeSource,
    script: Callable[[BrowseApp, Pilot[None]], Awaitable[None]],
    *,
    concurrency: int = 2,
    open_sections: bool = True,
) -> BrowseApp:
    app = BrowseApp(
        source,
        FakeOpener(),
        concurrency=concurrency,
        detail_collapsed=() if open_sections else ("changes",),
    )

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await settle(app, pilot)
            await script(app, pilot)

    asyncio.run(go())
    return app


def text_of(app: BrowseApp, name: str) -> str:
    return str(app.query_one(f"#{name}", Static).content)


def release(source: DetailSource, *names: str, pr: str = PR1) -> None:
    for name in names:
        source.gate(name, pr).set()


def hold(source: DetailSource, *names: str, pr: str = PR1) -> None:
    for name in names:
        source.gate(name, pr)


def calls_after(source: FakeSource, mark: int) -> list[tuple[str, Any]]:
    return list(source.calls[mark:])


def test_opening_reads_detail_and_diff_together_and_never_a_snapshot_for_a_loaded_row() -> None:
    source = source_of()
    seen: dict[str, Any] = {}

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert ("snapshot", PR1) in source.calls  # the row's own load, before the pane
        seen["mark"] = len(source.calls)
        await pilot.press("down", "enter")
        await settle(app, pilot)

    drive(source, script)
    assert sorted(calls_after(source, seen["mark"])) == [("detail", PR1), ("diff", PR1)]


def test_the_detail_read_and_the_diff_are_both_in_flight_before_either_answers() -> None:
    source = source_of()
    hold(source, "detail", "diff")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await wait_until(
            pilot, lambda: ("detail", PR1) in source.calls and ("diff", PR1) in source.calls
        )
        release(source, "detail", "diff")
        await settle(app, pilot)

    drive(source, script)


def test_the_first_frame_is_the_rows_snapshot_and_each_section_fills_in_as_it_arrives() -> None:
    source = source_of()
    source.fresh[PR1] = make_snapshot(1, title="Mine", body="fresh body")
    hold(source, "detail", "diff")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await wait_until(pilot, lambda: ("detail", PR1) in source.calls)
        await pilot.pause()
        # first frame: what the row already holds; the diff is on its way
        assert app.query_one("#detail").display is True
        assert "old body" in text_of(app, "description")
        assert "loading" in text_of(app, "changes")
        assert "loading" in status_of(app)
        release(source, "diff")
        await wait_until(pilot, lambda: "old line" in text_of(app, "changes"))
        assert "old body" in text_of(app, "description")  # the detail read has not arrived
        release(source, "detail")
        await settle(app, pilot)
        assert "fresh body" in text_of(app, "description")
        assert "old body" not in text_of(app, "description")
        assert "old line" in text_of(app, "changes")

    drive(source, script)


def test_a_row_without_a_snapshot_opens_on_the_lists_facts_with_loading_markers() -> None:
    source = source_of()
    hold(source, "detail", "diff")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await wait_until(pilot, lambda: ("detail", PR1) in source.calls)
        await pilot.pause()
        assert "Mine" in text_of(app, "facts")
        for name in ("description", "ci", "comments", "reviews", "changes"):
            assert "loading" in text_of(app, name), name
        release(source, "detail", "diff")
        await settle(app, pilot)
        assert "old body" in text_of(app, "description")
        assert "loading" not in text_of(app, "ci")

    drive(source, script, concurrency=0)


def test_the_pane_from_a_real_bulk_item_equals_the_pane_from_the_real_snapshot(
    fake_forges: FakeForges,
) -> None:
    per_pr = per_pr_without_required(fake_forges)  # the real per-PR snapshot of the recordings
    bulk = (
        read_bulk(rig(fake_forges, BulkRouter()).env, "github.com", [REF], now=lambda: NOW)
        .snapshots[REF]
        .to_dict()
    )  # the real bulk item of the same facts
    pr = per_pr["pr"]
    diff = make_diff(pr, "@@ -1 +1 @@\n-a\x1b[2J\n+b\n")
    source = DetailSource(
        authored=[make_item(7, repo="github.com/user-7/example-repo")],
        snapshots={pr: per_pr},
        diffs={pr: diff},
    )
    source.fresh[pr] = bulk

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        shown = app._detail
        assert shown is not None
        wanted = render.detail_of(per_pr, diff)
        assert shown.texts == wanted.texts
        assert shown.counts == wanted.counts
        assert "\x1b" not in "".join(shown.texts.values())

    drive(source, script)


def test_a_failing_diff_leaves_the_rest_of_the_pane_usable() -> None:
    source = source_of()
    source.fail["diff:" + PR1] = ForgeError("not_found", "no diff\x1b[31m")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert "old body" in text_of(app, "description")
        assert "failed: not_found" in text_of(app, "changes")
        status = status_of(app)
        assert "diff: not_found" in status and "\x1b" not in status
        assert "checks" in status

    drive(source, script)


def test_a_failing_detail_read_falls_back_to_snapshot_and_the_pane_has_the_data() -> None:
    source = source_of()
    source.fail["detail:" + PR1] = ForgeError("upstream_error", "bulk gave up")
    seen: dict[str, Any] = {}

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        seen["mark"] = len(source.calls)
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert "old body" in text_of(app, "description")
        assert "old line" in text_of(app, "changes")

    drive(source, script)
    assert sorted(calls_after(source, seen["mark"])) == [
        ("detail", PR1),
        ("diff", PR1),
        ("snapshot", PR1),
    ]


def test_when_the_detail_and_the_snapshot_both_fail_the_held_data_stays_and_the_row_is_stale() -> (
    None
):
    source = source_of()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        # the rows loaded; now both reads of PR1 fail
        source.fail["detail:" + PR1] = ForgeError("upstream_error", "x")
        source.fail["snapshot:" + PR1] = ForgeError("network_error", "down")
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert "old body" in text_of(app, "description")  # the last good data
        assert "network_error" in status_of(app)
        assert app._states[PR1].stale is True

    drive(source, script)


def test_a_row_without_data_whose_reads_fail_shows_failed_sections_not_loading_ones() -> None:
    source = source_of()
    source.fail["detail:" + PR1] = ForgeError("upstream_error", "x")
    source.fail["snapshot:" + PR1] = ForgeError("network_error", "down")
    source.fail["diff:" + PR1] = ForgeError("network_error", "down")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert "Mine" in text_of(app, "facts")
        for name in ("description", "ci", "comments", "reviews", "changes"):
            assert "failed: network_error" in text_of(app, name), name
        assert app.is_running

    drive(source, script, concurrency=0)


def test_a_fast_navigation_discards_the_stale_response_and_shows_no_mixed_data() -> None:
    source = source_of()
    source.fresh[PR1] = make_snapshot(1, title="Mine", body="fresh body of one")
    hold(source, "detail", "diff", pr=PR1)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")  # PR1, its reads gated
        await wait_until(pilot, lambda: ("detail", PR1) in source.calls)
        await pilot.press("escape", "up", "enter")  # PR2, not gated
        await wait_until(pilot, lambda: ("diff", PR2) in source.calls)
        await wait_until(pilot, lambda: "their body" in text_of(app, "description"))
        release(source, "detail", "diff", pr=PR1)  # PR1 answers after the user left it
        await pilot.pause()
        await asyncio.sleep(0.3)
        await settle(app, pilot)
        assert "Theirs" in text_of(app, "facts") and "Mine" not in text_of(app, "facts")
        assert "their body" in text_of(app, "description")
        assert "fresh body of one" not in text_of(app, "description")
        assert "old line" not in text_of(app, "changes")
        assert app._detail_pr == PR2

    drive(source, script)


def test_the_detail_read_refreshes_the_row_and_the_held_snapshot() -> None:
    source = source_of()
    source.fresh[PR1] = make_snapshot(1, title="Mine", body="newer body")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert app._snaps[PR1]["body"] == "newer body"  # the list and the pane agree
        await pilot.press("escape", "enter")
        await settle(app, pilot)
        assert "newer body" in text_of(app, "description")

    drive(source, script)


def test_a_source_without_a_detail_read_uses_snapshot_and_diff_together() -> None:
    base = source_of()
    source = FakeSource(authored=base.authored, snapshots=base.snapshots, diffs=base.diffs)
    seen: dict[str, Any] = {}

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        seen["mark"] = len(source.calls)
        await pilot.press("enter")
        await settle(app, pilot)
        assert "old body" in text_of(app, "description")

    drive(source, script, concurrency=0)
    assert sorted(calls_after(source, seen["mark"])) == [("diff", PR1), ("snapshot", PR1)]


def ForgeError(code: str, message: str) -> ForgectlError:  # noqa: N802 - reads as a constructor
    return ForgectlError(code, message)


# -- round 1 of the review of #119 ---------------------------------------------------------


def test_a_diff_that_arrives_before_any_snapshot_shows_in_the_changes_section() -> None:
    source = source_of()
    hold(source, "detail")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await wait_until(pilot, lambda: "old line" in text_of(app, "changes"))
        assert "loading" in text_of(app, "description")
        release(source, "detail")
        await settle(app, pilot)

    drive(source, script, concurrency=0)


def test_a_diff_that_arrives_when_the_snapshot_half_failed_is_not_marked_failed() -> None:
    source = source_of()
    source.fail["detail:" + PR1] = ForgeError("upstream_error", "x")
    source.fail["snapshot:" + PR1] = ForgeError("network_error", "down")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert "old line" in text_of(app, "changes")
        assert "failed: network_error" in text_of(app, "description")

    drive(source, script, concurrency=0)


def test_a_pr_that_leaves_the_list_loses_its_held_snapshot() -> None:
    source = source_of()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert PR1 in app._snaps
        source.authored = []
        await pilot.press("escape", "R")
        await settle(app, pilot)
        assert PR1 not in app._snaps

    drive(source, script)


def test_a_row_whose_last_refresh_failed_opens_with_the_stale_marker_on_the_first_frame() -> None:
    source = source_of()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        app._states[PR1] = app._failed_state(app._states[PR1], "network_error")
        hold(source, "detail", "diff")
        await pilot.press("down", "enter")
        await wait_until(pilot, lambda: ("detail", PR1) in source.calls)
        await pilot.pause()
        assert "old body" in text_of(app, "description")
        assert "stale: network_error" in status_of(app)
        release(source, "detail", "diff")
        await settle(app, pilot)

    drive(source, script)


def test_escape_during_a_load_keeps_the_pane_closed_when_the_reads_arrive() -> None:
    source = source_of()
    source.fresh[PR1] = make_snapshot(1, title="Mine", body="fresh body")
    hold(source, "detail", "diff")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await wait_until(pilot, lambda: ("detail", PR1) in source.calls)
        await pilot.press("escape")
        assert app.query_one("#detail").display is False
        release(source, "detail", "diff")
        await settle(app, pilot)
        await asyncio.sleep(0.2)
        await settle(app, pilot)
        assert app.query_one("#detail").display is False
        assert app.focused is not None and app.focused.id == "list"
        await pilot.press("enter")  # a new Enter still loads
        await settle(app, pilot)
        assert app.query_one("#detail").display is True
        assert "fresh body" in text_of(app, "description")

    drive(source, script)


def test_a_stop_code_from_the_bulk_reader_is_shown_and_never_asked_again_by_snapshot() -> None:
    source = source_of()
    source.fail["detail:" + PR1] = HostStopped("rate_limited", "the host is out of calls")
    seen: dict[str, Any] = {}

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        seen["mark"] = len(source.calls)
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert "rate_limited" in status_of(app)
        assert "old body" in text_of(app, "description")  # the held data stays
        assert app._states[PR1].stale is True

    drive(source, script)
    assert ("snapshot", PR1) not in calls_after(source, seen["mark"])


def test_a_detail_result_older_than_the_held_snapshot_does_not_replace_it() -> None:
    source = source_of()
    older = make_snapshot(1, title="Mine", body="older body")
    older["fetched_at"] = "2026-01-01T00:00:00Z"
    source.fresh[PR1] = older
    hold(source, "detail")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await wait_until(pilot, lambda: ("detail", PR1) in source.calls)
        newer = make_snapshot(1, title="Mine", body="newest body")
        newer["fetched_at"] = "2026-12-31T00:00:00Z"
        app._snaps[PR1] = newer  # a refresh of the row landed while the detail read was out
        release(source, "detail")
        await settle(app, pilot)
        assert app._snaps[PR1]["body"] == "newest body"
        assert "newest body" in text_of(app, "description")

    drive(source, script)
