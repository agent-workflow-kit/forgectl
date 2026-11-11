"""The first load of `browse` through `snapshots` (#116), with a fake source: rows paint per
batch, the hosts load in parallel, and whatever the bulk path does not settle is read one by one
without losing a row."""

from __future__ import annotations

import asyncio
import copy
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot

from forgectl.errors import ForgectlError
from forgectl.tui import bulk_load, columns
from forgectl.tui.app import BrowseApp

from .harness.schemas import validate
from .tui_fakes import FakeOpener, FakeSource, make_check, make_item, make_snapshot

GH = "github.com/acme/widgets"
GL = "gitlab.example.test/team/proj"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))


def pr(repo: str, n: int) -> str:
    return f"{repo}#{n}"


class BulkSource(FakeSource):
    """A source that also has `snapshots`; ``answer`` decides what a batch gives."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        answer: Callable[[list[str]], dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(authored=rows)
        del self.snapshots  # the fake keeps its answers there; here `snapshots` is the method
        self.snaps = {r["pr"]: snap_of(r) for r in rows}
        self.batches: list[list[str]] = []
        self.threads: list[str] = []
        self.answer = answer or self.whole
        self.lock = threading.Lock()

    def whole(self, batch: list[str]) -> dict[str, Any]:
        items = [copy.deepcopy(self.snaps[p]) for p in batch]
        return {"items": items, "failed": [], "complete": True, "errors": []}

    def snapshot(self, pr: str) -> dict[str, Any]:
        self.calls.append(("snapshot", pr))
        return copy.deepcopy(self.snaps[pr])

    def snapshots(  # type: ignore[override]
        self, prs: list[str], *, cancelled: Callable[[], bool] | None = None
    ) -> dict[str, Any]:
        with self.lock:
            self.batches.append(list(prs))
            self.threads.append(threading.current_thread().name)
        return self.answer(list(prs))

    def per_row(self) -> list[str]:
        return sorted(str(c[1]) for c in self.calls if c[0] == "snapshot")


def snap_of(item: dict[str, Any]) -> dict[str, Any]:
    repo, _, number = item["pr"].rpartition("#")
    return make_snapshot(int(number), repo=repo, checks=[make_check("build", "failing")])


def drive(
    source: FakeSource,
    script: Callable[[BrowseApp, Pilot[None]], Awaitable[None]],
    *,
    wait: bool = True,
) -> BrowseApp:
    app = BrowseApp(source, FakeOpener(), concurrency=2)

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            if wait:
                await app.workers.wait_for_complete()
                await pilot.pause()
            await script(app, pilot)

    asyncio.run(go())
    return app


async def nothing(app: BrowseApp, pilot: Pilot[None]) -> None:
    return None


def rows(repo: str, count: int, start: int = 1) -> list[dict[str, Any]]:
    return [make_item(n, repo=repo) for n in range(start, start + count)]


def loaded(app: BrowseApp) -> set[str]:
    return {p for p, s in app._states.items() if s.status == columns.LOADED}


@pytest.fixture(autouse=True)
def small_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bulk_load, "BATCH", 2)


def test_the_first_load_reads_every_row_in_batches_and_no_row_one_by_one() -> None:
    source = BulkSource(rows(GH, 5))
    app = drive(source, nothing)
    assert [len(b) for b in source.batches] == [2, 2, 1]
    assert source.per_row() == []
    assert loaded(app) == {pr(GH, n) for n in range(1, 6)}
    assert app._snap_done == 5


def test_a_batch_holds_the_rows_of_one_host_only() -> None:
    source = BulkSource(rows(GH, 3) + rows(GL, 3))
    drive(source, nothing)
    assert source.per_row() == []
    for batch in source.batches:
        assert len({p.split("/", 1)[0] for p in batch}) == 1


def test_the_two_hosts_load_in_parallel() -> None:
    barrier = threading.Barrier(2, timeout=8)
    source = BulkSource(rows(GH, 2) + rows(GL, 2))

    def meet(batch: list[str]) -> dict[str, Any]:
        barrier.wait()  # both hosts' first batch must be in flight at the same time
        return source.whole(batch)

    source.answer = meet
    app = drive(source, nothing)
    assert source.per_row() == [] and len(loaded(app)) == 4


def test_a_batch_paints_its_rows_before_the_next_batch_arrives() -> None:
    release = threading.Event()
    source = BulkSource(rows(GH, 4))

    def answer(batch: list[str]) -> dict[str, Any]:
        if batch[0].endswith("#3"):
            release.wait(10)  # the second batch is held back
        return source.whole(batch)

    source.answer = answer
    seen: dict[str, Any] = {}

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        deadline = time.monotonic() + 8
        while pr(GH, 1) not in loaded(app) and time.monotonic() < deadline:
            await pilot.pause(0.05)
        seen["first"] = loaded(app) & {pr(GH, n) for n in (1, 2)}
        seen["second"] = loaded(app) & {pr(GH, n) for n in (3, 4)}
        release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        seen["all"] = loaded(app)

    drive(source, script, wait=False)
    assert seen["first"] == {pr(GH, 1), pr(GH, 2)} and seen["second"] == set()
    assert seen["all"] == {pr(GH, n) for n in range(1, 5)}


def test_a_failed_batch_sends_its_rows_to_the_per_row_pool_and_loses_none() -> None:
    source = BulkSource(rows(GH, 5))

    def answer(batch: list[str]) -> dict[str, Any]:
        if batch[0].endswith("#3"):
            raise ForgectlError("upstream_error", "boom")
        return source.whole(batch)

    source.answer = answer
    app = drive(source, nothing)
    assert source.per_row() == [pr(GH, 3), pr(GH, 4)]
    assert loaded(app) == {pr(GH, n) for n in range(1, 6)}


def test_an_unexpected_exception_in_the_source_is_a_failed_batch_not_a_crash() -> None:
    source = BulkSource(rows(GH, 2))

    def answer(batch: list[str]) -> dict[str, Any]:
        raise RuntimeError("a source bug")

    source.answer = answer
    app = drive(source, nothing)
    assert source.per_row() == [pr(GH, 1), pr(GH, 2)] and len(loaded(app)) == 2


def test_a_row_the_batch_reports_failed_is_read_one_by_one() -> None:
    source = BulkSource(rows(GH, 3))

    def answer(batch: list[str]) -> dict[str, Any]:
        items = [copy.deepcopy(source.snaps[p]) for p in batch if not p.endswith("#2")]
        failed = [
            {"pr": p, "error": {"code": "bulk_unavailable", "message": "cut"}}
            for p in batch
            if p.endswith("#2")
        ]
        return {"items": items, "failed": failed, "complete": False, "errors": []}

    source.answer = answer
    app = drive(source, nothing)
    assert source.per_row() == [pr(GH, 2)]
    assert loaded(app) == {pr(GH, n) for n in (1, 2, 3)}


def test_an_incomplete_item_is_shown_and_read_again_by_the_pool() -> None:
    source = BulkSource(rows(GH, 2))

    def answer(batch: list[str]) -> dict[str, Any]:
        items = [copy.deepcopy(source.snaps[p]) for p in batch]
        items[0]["complete"] = False
        items[0]["incomplete"] = ["threads"]
        return {"items": items, "failed": [], "complete": False, "errors": []}

    source.answer = answer
    app = drive(source, nothing)
    assert source.per_row() == [pr(GH, 1)]
    assert loaded(app) == {pr(GH, 1), pr(GH, 2)}


def test_an_item_for_a_pr_that_was_not_asked_is_ignored() -> None:
    source = BulkSource(rows(GH, 2))

    def answer(batch: list[str]) -> dict[str, Any]:
        out = source.whole(batch)
        out["items"].append(make_snapshot(99, repo=GH))
        out["items"].append({"pr": 7})
        out["items"].append("garbage")
        return out

    source.answer = answer
    app = drive(source, nothing)
    assert source.per_row() == [] and pr(GH, 99) not in app._states


def test_a_stopped_host_sends_the_rest_of_its_rows_to_the_pool() -> None:
    source = BulkSource(rows(GH, 4) + rows(GL, 2))

    def answer(batch: list[str]) -> dict[str, Any]:
        if batch[0].split("/", 1)[0] == "github.com" and batch[0].endswith("#1"):
            return {
                "items": [],
                "failed": [
                    {"pr": p, "error": {"code": "rate_limited", "message": "x"}} for p in batch
                ],
                "complete": False,
                "errors": [{"host": "github.com", "code": "rate_limited", "message": "x"}],
            }
        return source.whole(batch)

    source.answer = answer
    app = drive(source, nothing)
    assert [b for b in source.batches if b[0].split("/", 1)[0] == "github.com"] == [
        [pr(GH, 1), pr(GH, 2)]
    ]
    assert source.per_row() == [pr(GH, n) for n in range(1, 5)]
    assert len(loaded(app)) == 6


def test_a_source_without_snapshots_uses_the_per_row_pool_as_before() -> None:
    items = rows(GH, 3)
    source = FakeSource(authored=items, snapshots={i["pr"]: snap_of(i) for i in items})
    app = drive(source, nothing)
    assert sorted(str(c[1]) for c in source.calls if c[0] == "snapshot") == sorted(
        i["pr"] for i in items
    )
    assert len(loaded(app)) == 3


def test_a_reload_reads_the_rows_one_by_one_not_in_bulk() -> None:
    source = BulkSource(rows(GH, 2))

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("R")
        await app.workers.wait_for_complete()
        await pilot.pause()

    drive(source, script)
    assert len(source.batches) == 1  # the first load only (the refresh is #118's)
    assert source.per_row() == [pr(GH, 1), pr(GH, 2)]


def test_bulk_items_are_schema_valid() -> None:
    for snap in BulkSource(rows(GH, 2)).whole([pr(GH, 1)])["items"]:
        validate(snap, "forgectl/cli/snapshot.json")


def test_a_type_error_inside_a_batch_read_does_not_run_the_read_twice() -> None:
    calls: list[list[str]] = []

    class NoCancel(BulkSource):
        def snapshots(self, prs: list[str]) -> dict[str, Any]:  # type: ignore[override]
            calls.append(list(prs))
            raise TypeError("bug inside the read")

    source = NoCancel(rows(GH, 2))
    app = drive(source, nothing)
    assert calls == [[pr(GH, 1), pr(GH, 2)]]
    assert loaded(app) == {pr(GH, 1), pr(GH, 2)}  # the pool read them one by one


def test_a_source_whose_snapshots_takes_no_cancelled_is_still_read_in_bulk() -> None:
    class NoCancel(BulkSource):
        def snapshots(self, prs: list[str]) -> dict[str, Any]:  # type: ignore[override]
            return self.whole(prs)

    source = NoCancel(rows(GH, 3))
    app = drive(source, nothing)
    assert source.per_row() == [] and len(loaded(app)) == 3
