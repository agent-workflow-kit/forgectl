"""`m` in `browse` (#60): the dialog, the guards, the merge command's refusals, the result file."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Static

from forgectl.errors import ForgectlError, refused
from forgectl.tui.app import BrowseApp
from forgectl.tui.merge_screen import MergeScreen

from .harness.schemas import validate
from .test_tui_app import PR1, PR2, _isolated_home, settle  # noqa: F401
from .tui_fakes import (
    SHA_A,
    FakeOpener,
    FakeSource,
    MergeSource,
    make_item,
    make_snapshot,
    merge_plan,
)


def source(script: list[Any] | None = None, **snap: Any) -> MergeSource:
    return MergeSource(
        script=script,
        authored=[make_item(1, title="Mine")],
        requested=[],
        snapshots={PR1: make_snapshot(1, title="Mine", **snap), PR2: make_snapshot(2)},
    )


def drive(
    src: MergeSource, script: Callable[[BrowseApp, Pilot[None]], Awaitable[None]]
) -> BrowseApp:
    app = BrowseApp(src, FakeOpener(), concurrency=2)

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await settle(app, pilot)
            await script(app, pilot)

    asyncio.run(go())
    return app


def status(app: BrowseApp) -> str:
    return str(app.query_one("#status", Static).content)


def dialog(app: BrowseApp) -> str:
    assert isinstance(app.screen, MergeScreen)
    return "\n".join(str(w.content) for w in app.screen.query(Static))


async def press_m(app: BrowseApp, pilot: Pilot[None]) -> None:
    await pilot.press("m")
    await pilot.pause()
    await app.workers.wait_for_complete() if not isinstance(app.screen, MergeScreen) else None
    await pilot.pause()


def writes(src: MergeSource) -> list[dict[str, Any]]:
    return [m for m in src.merges if not m["dry_run"]]


def test_m_checks_then_shows_what_will_be_merged_and_writes_nothing_until_y() -> None:
    src = source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        text = dialog(app)
        assert PR1 in text
        assert "github.com/acme/widgets" in text and "number:  1" in text
        assert f"head:    {SHA_A}" in text
        assert "base:    main" in text and "method:  squash" in text
        assert writes(src) == []  # the check was a dry run, with the head and base shown
        assert src.merges == [
            {"pr": PR1, "head": SHA_A, "base": "main", "method": None, "dry_run": True}
        ]
        await pilot.press("n")
        await settle(app, pilot)
        assert writes(src) == []
        assert "merge cancelled" in status(app)

    app = drive(src, script)
    assert app.result()["merged"] == []


def test_y_merges_with_the_shown_head_base_and_method_and_lists_it_in_the_result() -> None:
    src = source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        await pilot.press("y")
        await settle(app, pilot)
        assert writes(src) == [
            {"pr": PR1, "head": SHA_A, "base": "main", "method": "squash", "dry_run": False}
        ]
        assert f"merged {PR1} (squash)" in status(app)
        assert ("snapshot", PR1) in src.calls[src.calls.index(("merge", writes(src)[0])) :]

    app = drive(src, script)
    result = app.result()
    assert result["merged"] == [PR1]
    validate(result, "forgectl/cli/browse-result.json")


def test_other_keys_do_not_confirm_and_escape_cancels() -> None:
    src = source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        for key in ("enter", "o", "space", "Y", "m"):
            await pilot.press(key)
        await pilot.pause()
        assert isinstance(app.screen, MergeScreen)
        assert writes(src) == []
        await pilot.press("escape")
        await settle(app, pilot)
        assert not isinstance(app.screen, MergeScreen)
        assert writes(src) == []

    assert drive(src, script).result()["merged"] == []


def test_a_stale_head_is_refused_by_the_command_and_nothing_is_written() -> None:
    stale = refused(
        "stale_head", "the head is not the expected head", details={"head_sha": "d" * 40}
    )
    src = source([stale])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        assert not isinstance(app.screen, MergeScreen)  # no dialog for a refused merge
        assert "stale_head" in status(app)
        assert writes(src) == []

    assert drive(src, script).result()["merged"] == []


def test_a_refusal_from_the_write_shows_and_is_not_listed_as_merged() -> None:
    late = refused("stale_base", "the base is not the expected base", details={"base_ref": "x"})
    src = source([None, late])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        await pilot.press("y")
        await settle(app, pilot)
        assert "stale_base" in status(app)
        assert len(writes(src)) == 1

    assert drive(src, script).result()["merged"] == []


@pytest.mark.parametrize("code", ["outcome_unknown", "merge_unverified", "audit_failed"])
def test_an_unknown_outcome_says_so_is_not_listed_and_the_row_is_read_again(code: str) -> None:
    details: dict[str, Any] | None = None
    if code == "merge_unverified":
        details = {"observed": {}, "head_sha": SHA_A}
    elif code == "audit_failed":
        details = {"outcome": "merged"}
    src = source([None, ForgectlError(code, "it may have landed", details=details)])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        before = [c for c in src.calls if c == ("snapshot", PR1)]
        await press_m(app, pilot)
        await pilot.press("y")
        await settle(app, pilot)
        assert code in status(app) and "not known" in status(app)
        assert len([c for c in src.calls if c == ("snapshot", PR1)]) > len(before)

    assert drive(src, script).result()["merged"] == []


def test_a_veto_and_an_escalation_show_in_the_status_line() -> None:
    veto = ForgectlError("vetoed", "blocked by a hook", hook="pre-merge", hook_stderr="no")
    src = source([None, veto])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        await pilot.press("y")
        await settle(app, pilot)
        assert "vetoed" in status(app)

    assert drive(src, script).result()["merged"] == []


def test_a_blocked_pr_gets_no_merge_and_no_override() -> None:
    src = source(extra={"blocked_by": [{"code": "checks_failing", "message": "x"}]})

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        assert not isinstance(app.screen, MergeScreen)
        assert "not_ready: checks_failing" in status(app)
        assert f"forgectl merge {PR1}" in status(app)
        assert src.merges == []  # not even a check

    assert drive(src, script).result()["merged"] == []


def test_a_blocker_that_appears_after_the_snapshot_is_refused_by_the_command() -> None:
    not_ready = refused(
        "not_ready", "the pull request is not ready: draft", details={"blocked_by": []}
    )
    src = source([not_ready])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        assert "not_ready" in status(app)
        assert writes(src) == []

    drive(src, script)


def test_no_call_ever_carries_an_override() -> None:
    src = source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        await pilot.press("y")
        await settle(app, pilot)

    drive(src, script)
    assert all(set(m) == {"pr", "head", "base", "method", "dry_run"} for m in src.merges)


def test_a_closed_pr_is_not_merged_and_asks_nothing() -> None:
    src = source(extra={"state": "closed"})

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        assert "the PR is closed" in status(app)

    drive(src, script)
    assert src.merges == []


def test_m_without_a_loaded_snapshot_asks_nothing() -> None:
    src = source()
    app = BrowseApp(src, FakeOpener(), concurrency=0)  # no per-row snapshots

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await settle(app, pilot)
            await pilot.press("m")
            await pilot.pause()
            assert "no loaded snapshot" in status(app)

    asyncio.run(go())
    assert src.merges == []


def test_a_source_without_merge_says_so() -> None:
    plain = FakeSource(authored=[make_item(1)], snapshots={PR1: make_snapshot(1)}, requested=[])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("m")
        await pilot.pause()
        assert "cannot merge" in status(app)

    drive(plain, script)  # type: ignore[arg-type]


def test_an_already_merged_pr_is_not_listed_as_merged_here() -> None:
    done = merge_plan(PR1, SHA_A, "main", None)
    done["already_merged"] = True
    src = source([done])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        assert not isinstance(app.screen, MergeScreen)
        assert "already merged" in status(app)

    assert drive(src, script).result()["merged"] == []
    assert writes(src) == []


def test_an_unresolved_method_is_picked_from_the_allowed_ones() -> None:
    needed = ForgectlError(
        "method_required", "name a method", details={"allowed": ["squash", "merge"]}
    )
    src = source([needed, None])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        text = dialog(app)
        assert "1 squash" in text and "2 merge" in text and "rebase" not in text
        await pilot.press("y")  # no pick yet: nothing happens
        await pilot.pause()
        assert isinstance(app.screen, MergeScreen)
        await pilot.press("3")  # not offered
        await pilot.press("2")
        await pilot.pause()
        assert "chosen: merge" in dialog(app)
        await pilot.press("y")
        await pilot.pause()
        await app.workers.wait_for_complete() if not isinstance(app.screen, MergeScreen) else None
        await pilot.pause()
        # the check runs again with the pick, and the dialog is shown again
        assert isinstance(app.screen, MergeScreen)
        assert "method:  merge" in dialog(app)
        await pilot.press("y")
        await settle(app, pilot)
        assert [m["method"] for m in src.merges] == [None, "merge", "merge"]
        assert writes(src)[0]["method"] == "merge"

    assert drive(src, script).result()["merged"] == [PR1]


def test_no_pick_cancels_an_unresolved_method() -> None:
    needed = ForgectlError("method_required", "name a method", details={"allowed": []})
    src = source([needed])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        assert "1 squash  2 merge  3 rebase" in dialog(app)  # an unknown list offers all three
        await pilot.press("escape")
        await settle(app, pilot)
        assert "merge cancelled" in status(app)

    drive(src, script)
    assert writes(src) == []


def test_the_pane_pr_is_the_one_merged_not_the_highlighted_row() -> None:
    src = MergeSource(
        authored=[make_item(1, title="Mine"), make_item(2, title="Other")],
        requested=[],
        snapshots={PR1: make_snapshot(1), PR2: make_snapshot(2)},
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")  # opens the pane for the highlighted row
        await settle(app, pilot)
        shown = app._detail_pr
        await pilot.press("m")
        await pilot.pause()
        assert isinstance(app.screen, MergeScreen)
        assert src.merges[-1]["pr"] == shown

    drive(src, script)


def test_hostile_forge_text_in_the_dialog_is_plain() -> None:
    base = "[b]x[/b][@click=app.quit]"
    src = MergeSource(
        authored=[make_item(1)],
        requested=[],
        snapshots={PR1: make_snapshot(1, extra={"base": {"ref": base, "sha": "b" * 40}})},
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        assert f"base:    {base}" in dialog(app)
        assert app.is_running

    drive(src, script)
    assert src.merges[0]["base"] == base


def test_quit_waits_for_a_merge_in_flight() -> None:
    src = source()
    src.gate = threading.Event()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        await pilot.press("y")
        for _ in range(100):  # the write has started on its worker thread
            if src.in_write.is_set():
                break
            await pilot.pause(0.02)
        assert src.in_write.is_set()
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running
        assert "merge is in progress" in status(app)
        src.gate.set()  # type: ignore[union-attr]
        await settle(app, pilot)
        assert f"merged {PR1}" in status(app)

    assert drive(src, script).result()["merged"] == [PR1]


def test_the_result_stays_on_the_status_line_until_the_next_key() -> None:
    src = source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await press_m(app, pilot)
        await pilot.press("y")
        await settle(app, pilot)
        await app.workers.wait_for_complete()
        assert f"merged {PR1}" in status(app)
        await pilot.press("down")
        await pilot.pause()
        assert f"merged {PR1}" not in status(app)

    drive(src, script)


def test_merging_twice_lists_the_pr_once() -> None:
    src = source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        for _ in range(2):
            await press_m(app, pilot)
            await pilot.press("y")
            await settle(app, pilot)

    assert drive(src, script).result()["merged"] == [PR1]


def test_the_help_lists_m() -> None:
    from forgectl.tui.app import HELP_KEYS

    assert "m" in dict(HELP_KEYS)
