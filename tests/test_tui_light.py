"""The light poll of a `browse` refresh (#118): what is asked, what is re-read, what stays."""

from __future__ import annotations

import asyncio
import copy
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import OptionList

from forgectl.errors import ForgectlError
from forgectl.tui import columns, light
from forgectl.tui.app import BrowseApp
from forgectl.tui.source import Row
from forgectl.tui.source_live import Cancelled

from .test_tui_app import content_of, list_prompts, settle
from .test_tui_columns import GH, GL, prefix, snap
from .test_tui_refresh import T0, Clock, RefreshSource, bar, cycle, run
from .tui_fakes import SHA_A, TS, make_item

NEW = "2026-11-03T13:00:00Z"


def light_of(item: dict[str, Any], **extra: Any) -> dict[str, Any]:
    gitlab = item["repo"].startswith("gitlab")
    base: dict[str, Any] = {
        "pr": item["pr"],
        "pr_id": item["pr_id"],
        "repo": item["repo"],
        "number": item["number"],
        "url": item["url"],
        "title": item["title"],
        "author": "alice",
        "draft": False,
        "head_sha": item["head"]["sha"],
        "updated_at": item["updated_at"],
        "ci_state": "running" if gitlab else "SUCCESS",
        "review_decision": None if gitlab else "REVIEW_REQUIRED",
        "approvals_left": 1 if gitlab else None,
        "approved": False if gitlab else None,
        "merge_status": "mergeable" if gitlab else None,
        "notes_count": 3 if gitlab else None,
        "roles": ["authored"],
    }
    return {**base, **extra}


class Shots(dict[str, dict[str, Any]]):
    """The fake's snapshot answers by PR, which is also the callable ``snapshots`` (the batch
    read): the base fake keeps its answers in an attribute of that name."""

    def __init__(self, owner: LightSource, data: dict[str, dict[str, Any]]) -> None:
        super().__init__(data)
        self.owner = owner

    def __call__(self, prs: list[str], *, cancelled: Any = None) -> dict[str, Any]:
        return self.owner.batch(prs, cancelled=cancelled)


class LightSource(RefreshSource):
    """A source with `light` and `snapshots`: ``lights`` is what the poll answers (by PR)."""

    def __init__(self, items: list[dict[str, Any]], **kw: Any) -> None:
        shots = {
            i["pr"]: snap(i["number"], repo=i["repo"], title=i["title"], updated_at=i["updated_at"])
            for i in items
        }
        super().__init__(authored=items, snapshots=shots, **kw)
        self.snapshots = Shots(self, self.snapshots)
        self.lights = {i["pr"]: light_of(i) for i in items}
        self.answer: dict[str, Any] = {}
        self.light_error: Exception | None = None
        self.light_gate: threading.Event | None = None
        self.batches: list[list[str]] = []

    def light(self) -> dict[str, Any]:
        self.calls.append(("light", None))
        if self.light_gate is not None:
            self.light_gate.wait(10)
        if self.light_error is not None:
            raise self.light_error
        hosts = sorted({v["repo"].split("/", 1)[0] for v in self.lights.values()}) or ["h"]
        out: dict[str, Any] = {
            "items": [copy.deepcopy(v) for v in self.lights.values()],
            "complete": True,
            "errors": [],
            "hosts_queried": hosts,
            "unsupported": [],
            "rate_limit": {"host": hosts[0], "remaining": 4000, "reset_at": TS},
        }
        out.update(self.answer)
        return out

    def batch(self, prs: list[str], *, cancelled: Any = None) -> dict[str, Any]:  # noqa: ARG002
        self.calls.append(("snapshots", list(prs)))
        self.batches.append(list(prs))
        return {
            "items": [
                {**copy.deepcopy(self.snapshots[p]), "complete": True}
                for p in prs
                if p not in self.failing
            ],
            "failed": [
                {"pr": p, "error": {"code": "upstream_error", "message": "boom"}}
                for p in prs
                if p in self.failing
            ],
            "complete": True,
            "errors": [],
        }

    def reads(self) -> list[str]:
        """Every PR read after the load, by either path."""
        out: list[str] = []
        for name, arg in self.calls:
            if name == "snapshot":
                out.append(str(arg))
            elif name == "snapshots":
                out.extend(arg)
        return sorted(out)

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)


def trio() -> LightSource:
    return LightSource([make_item(n, title=f"pr {n}") for n in (1, 2, 3)])


def change(source: LightSource, number: int, **fields: Any) -> str:
    """The PR changed on the host: its poll tuple, and its snapshot when the title moved."""
    pr = prefix(GH, number)
    source.lights[pr].update(fields)
    return pr


async def seed(app: BrowseApp, pilot: Pilot[None], source: LightSource) -> None:
    """The first poll after a load keeps the tuples; nothing is read for it."""
    source.calls.clear()
    await cycle(app, pilot)
    assert source.reads() == [], (source.calls, app._list_note)
    source.calls.clear()
    source.batches.clear()


def go(
    source: LightSource, script: Callable[[BrowseApp, Pilot[None]], Any], **kw: Any
) -> BrowseApp:
    kw.setdefault("interval", 1000)
    kw.setdefault("full_every", 1000)
    return run(source, script, **kw)


# -- the tuple -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("pr_id", "other"),
        ("head_sha", "c" * 40),
        ("updated_at", NEW),
        ("ci_state", "FAILURE"),
        ("review_decision", "APPROVED"),
        ("approvals_left", 0),
        ("approved", True),
        ("merge_status", "ci_must_pass"),
        ("notes_count", 4),
    ],
)
def test_each_component_of_the_tuple_alone_changes_the_key(field: str, value: Any) -> None:
    item = light_of(make_item(1, repo=GL))
    assert light.key_of(item) != light.key_of({**item, field: value})


@pytest.mark.parametrize("field", ["title", "url", "author", "draft", "roles", "number"])
def test_what_the_tuple_leaves_out_does_not_change_the_key(field: str) -> None:
    item = light_of(make_item(1))
    assert light.key_of(item) == light.key_of({**item, field: "x"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "c" * 40),
        ("updated_at", NEW),
        ("ci_state", "FAILURE"),
        ("review_decision", "APPROVED"),
        ("approvals_left", 0),
        ("approved", True),
        ("merge_status", "ci_must_pass"),
        ("notes_count", 9),
    ],
)
def test_each_component_alone_triggers_a_re_read(field: str, value: Any) -> None:
    source = LightSource([make_item(1, title="a"), make_item(2, title="b", repo=GL)])
    # GitHub and GitLab each have the components of their host; the other host's PR stays put
    target = (
        prefix(GL, 2)
        if field in ("approvals_left", "approved", "merge_status", "notes_count")
        else prefix(GH, 1)
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        source.lights[target][field] = value
        await cycle(app, pilot)
        assert source.reads() == [target]
        assert source.count("prs") == 0

    go(source, script)


# -- what a refresh asks -------------------------------------------------------------------------


def test_k_of_n_changed_asks_the_poll_and_one_batch_for_the_k_rows() -> None:
    source = LightSource([make_item(n, title=f"pr {n}") for n in range(1, 7)])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        change(source, 2, ci_state="FAILURE")
        change(source, 5, updated_at=NEW)
        await cycle(app, pilot)
        assert [c[0] for c in source.calls] == ["light", "snapshots"]
        assert [sorted(b) for b in source.batches] == [sorted([prefix(GH, 2), prefix(GH, 5)])]
        assert source.count("snapshot") == 0 and source.count("prs") == 0

    go(source, script)


def test_no_change_reads_nothing_and_leaves_every_row_object_alone() -> None:
    source = trio()
    seen: dict[str, list[Row]] = {}

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        seen["rows"] = list(app._rows)
        states = dict(app._states)
        await cycle(app, pilot)
        await cycle(app, pilot)
        assert [c[0] for c in source.calls] == ["light", "light"]
        assert all(a is b for a, b in zip(app._rows, seen["rows"], strict=True))
        assert all(app._states[pr] is st for pr, st in states.items())

    go(source, script)


def test_the_first_poll_after_a_load_reads_a_row_whose_update_time_moved() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        change(source, 2, updated_at=NEW)
        source.calls.clear()
        await cycle(app, pilot)
        assert source.reads() == [prefix(GH, 2)]

    go(source, script)


def test_a_row_that_failed_is_read_again_by_the_next_poll() -> None:
    source = trio()
    source.failing = {prefix(GH, 2)}

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.failing = set()
        source.calls.clear()
        await cycle(app, pilot)
        assert prefix(GH, 2) in source.reads()
        assert app._states[prefix(GH, 2)].status == columns.LOADED

    go(source, script)


def test_a_new_pr_is_listed_and_read_and_a_removed_one_leaves() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        gone = prefix(GH, 1)
        del source.lights[gone]
        new = make_item(9, title="brand new")
        source.snapshots[new["pr"]] = snap(9, title="brand new", updated_at=TS)
        source.lights[new["pr"]] = light_of(new)
        await cycle(app, pilot)
        assert source.reads() == [new["pr"]]
        assert sorted(r.pr for r in app._rows) == sorted([prefix(GH, 2), prefix(GH, 3), new["pr"]])
        assert app._row_by_pr[new["pr"]].item["title"] == "brand new"
        assert gone not in app._states and gone not in app._light_keys

    go(source, script)


def test_a_read_row_takes_the_fresh_item_of_its_snapshot() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        pr = change(source, 2, updated_at=NEW)
        source.snapshots[pr] = snap(2, title="renamed", updated_at=NEW)
        await cycle(app, pilot)
        assert app._row_by_pr[pr].item["title"] == "renamed"
        assert any("renamed" in p.plain for p in list_prompts(app))

    go(source, script)


def test_roles_follow_the_poll_without_replacing_the_row() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        row = app._row_by_pr[prefix(GH, 1)]
        source.lights[prefix(GH, 1)]["roles"] = ["authored", "requested"]
        await cycle(app, pilot)
        assert app._row_by_pr[prefix(GH, 1)] is row
        assert row.roles == {"authored", "requested"}
        assert source.reads() == []

    go(source, script)


# -- fallback ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setup,reason",
    [
        (
            lambda s: setattr(s, "light_error", ForgectlError("upstream_error", "boom")),
            "upstream_error",
        ),
        (lambda s: s.answer.update(unsupported=["gitlab.example"], complete=False), "lightly"),
        (lambda s: s.answer.update(complete=False), "cut"),
        (
            lambda s: s.answer.update(
                complete=False,
                hosts_queried=["github.com"],
                errors=[{"host": "github.com", "code": "auth_required", "message": "no"}],
            ),
            "every host",
        ),
        (lambda s: s.answer.update(items=[{"title": "no pr"}]), "item"),
        (lambda s: s.answer.update(items="nope"), "no list"),
    ],
)
def test_a_poll_that_cannot_stand_in_falls_back_to_prs_and_says_why(
    setup: Callable[[LightSource], None], reason: str
) -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        setup(source)
        change(source, 2, updated_at=NEW)
        source.authored[1]["updated_at"] = NEW
        await cycle(app, pilot)
        assert source.count("prs") == 2  # the old path
        assert prefix(GH, 2) in source.reads()
        note = app._list_note or ""
        assert "light poll" in note and reason in note
        assert "light poll" in content_of(app, "#status").plain

    go(source, script)


def test_a_source_without_light_refreshes_as_before() -> None:
    plain = RefreshSource(authored=[make_item(1)], snapshots={prefix(GH, 1): snap(1)})

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert app._light is False
        plain.calls.clear()
        await cycle(app, pilot)
        assert plain.count("prs") == 2

    go(plain, script)  # type: ignore[arg-type]


def test_the_nth_timed_cycle_reads_every_row_the_old_way() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await cycle(app, pilot)  # cycle 1: the poll
        assert source.count("light") == 1
        source.calls.clear()
        await cycle(app, pilot)  # cycle 2 is the full one
        assert source.count("light") == 0 and source.count("prs") == 2
        assert source.reads() == sorted(prefix(GH, n) for n in (1, 2, 3))

    go(source, script, full_every=2)


def test_a_partial_poll_keeps_the_rows_of_the_failed_host_stale() -> None:
    source = LightSource([make_item(1, repo=GH), make_item(1, repo=GL)])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        gone = source.lights.pop(prefix(GL, 1))
        source.answer.update(
            complete=False,
            hosts_queried=["github.com", "gitlab.example"],
            errors=[{"host": "gitlab.example", "code": "upstream_error", "message": "x"}],
        )
        await cycle(app, pilot)
        assert prefix(GL, 1) in app._row_by_pr  # hidden by the failure, not closed
        assert app._states[prefix(GL, 1)].stale is True
        assert "upstream_error" in (app._list_note or "")
        assert gone  # the host's PR is still there on the host

    go(source, script)


# -- backoff, manual refresh ---------------------------------------------------------------------


def test_a_host_below_the_threshold_is_not_read_by_a_timed_cycle_until_the_interval_passes() -> (
    None
):
    source = LightSource([make_item(1, repo=GH), make_item(1, repo=GL)])
    source.snapshots[prefix(GL, 1)]["rate_limit"] = {
        "host": "gitlab.example",
        "remaining": 5,
        "reset_at": "2026-11-04T12:00:00Z",
    }
    clock = Clock()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.calls.clear()
        await cycle(app, pilot)  # seeds
        source.lights[prefix(GH, 1)]["ci_state"] = "FAILURE"
        source.lights[prefix(GL, 1)]["ci_state"] = "failed"
        source.calls.clear()
        await cycle(app, pilot)
        assert source.reads() == [prefix(GH, 1)]  # the slow host waits
        source.calls.clear()
        clock.now = T0 + timedelta(seconds=1000 * 4 + 1)
        await cycle(app, pilot)
        assert source.reads() == [prefix(GL, 1)]

    go(source, script, clock=clock)


def test_a_manual_reload_polls_once_reads_only_what_changed_and_ignores_the_backoff() -> None:
    source = LightSource([make_item(1, repo=GH), make_item(1, repo=GL)])
    source.snapshots[prefix(GL, 1)]["rate_limit"] = {
        "host": "gitlab.example",
        "remaining": 5,
        "reset_at": "2026-11-04T12:00:00Z",
    }

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        source.lights[prefix(GL, 1)]["notes_count"] = 99
        await pilot.press("R")
        await settle(app, pilot)
        assert [c[0] for c in source.calls] == ["light", "snapshots"]
        assert source.reads() == [prefix(GL, 1)]

    go(source, script, clock=Clock())


def test_R_during_a_refresh_does_not_start_a_second_poll() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        source.light_gate = threading.Event()
        await pilot.press("R")
        await pilot.pause(0.1)
        await pilot.press("R")
        await pilot.press("R")
        await pilot.pause(0.1)
        assert source.count("light") == 1
        assert "already running" in content_of(app, "#status").plain
        source.light_gate.set()
        await settle(app, pilot)
        assert source.count("light") == 1

    go(source, script)


# -- the screen ----------------------------------------------------------------------------------


def test_selection_scroll_and_the_open_detail_survive_a_refresh_in_which_one_row_changed() -> None:
    items = [
        make_item(n, title=f"pr {n:02d}", updated_at=f"2026-11-03T10:{n:02d}:00Z")
        for n in range(1, 41)
    ]
    source = LightSource(items)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        olist = app.query_one("#list", OptionList)
        for _ in range(12):
            await pilot.press("down")
        await settle(app, pilot)
        await pilot.press("enter")  # the detail pane of the highlighted row
        await settle(app, pilot)
        await seed(app, pilot, source)  # the first poll: the pane's read left no tuple to distrust
        shown = app._detail_pr
        assert shown is not None
        highlighted = olist.highlighted
        scroll = olist.scroll_y
        pane_before = app._detail
        other = next(r.pr for r in app._rows if r.pr != shown)
        source.lights[other]["ci_state"] = "FAILURE"
        source.calls.clear()
        app._tick()
        await settle(app, pilot)
        assert source.reads() == [other]
        assert app._detail_pr == shown and app._detail is pane_before
        assert olist.highlighted == highlighted and olist.scroll_y == scroll
        row = app._highlighted_row()
        assert highlighted is not None and row is not None
        assert row.pr == app._shown_prs[highlighted]

    go(source, script, size=(140, 20))


def test_the_status_bar_still_shows_the_refresh_time() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await cycle(app, pilot)
        assert "refreshed" in bar(app)

    go(source, script)


def test_a_manual_reload_reads_the_open_detail_even_when_its_row_did_not_change() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        await pilot.press("enter")
        await settle(app, pilot)
        source.calls.clear()
        await pilot.press("R")
        await settle(app, pilot)
        assert source.count("light") == 1
        assert source.reads() == [app._detail_pr]  # read once, by the detail load

    go(source, script)


# -- review round 1 ------------------------------------------------------------------------------


def test_the_first_poll_reads_a_row_whose_snapshot_shows_pending_ci() -> None:
    source = trio()
    source.snapshots[prefix(GH, 2)] = snap(2, ci="pending", title="pr 2")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        source.calls.clear()
        await cycle(app, pilot)
        assert source.reads() == [prefix(GH, 2)]

    go(source, script)


def test_the_first_poll_does_not_see_a_ci_only_change_of_a_settled_row() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        change(source, 2, ci_state="FAILURE")  # the documented limit: caught by a later full read
        source.calls.clear()
        await cycle(app, pilot)
        assert source.reads() == []

    go(source, script)


def test_a_change_that_reverts_after_a_read_outside_the_poll_is_read_again() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        row = app._highlighted_row()
        assert row is not None
        pr = row.pr
        source.lights[pr]["ci_state"] = "FAILURE"
        await pilot.press("r")  # a read outside the poll sees the failure
        await settle(app, pilot)
        source.lights[pr]["ci_state"] = "SUCCESS"  # and the host reverts to the tuple kept before
        source.calls.clear()
        await cycle(app, pilot)
        assert source.reads() == [pr]

    go(source, script)


def test_a_full_cycle_forgets_every_kept_tuple() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await cycle(app, pilot)
        assert len(app._light_keys) == 3
        await cycle(app, pilot)  # the full one, the old way
        assert app._light_keys == {}

    go(source, script, full_every=2)


class CancellingSource(LightSource):
    """A detail read that a newer request replaced: ``snapshot`` of ``cancel_pr`` is cancelled."""

    cancel_pr: str | None = None

    def snapshot(self, pr: str, cancelled: Any = None) -> dict[str, Any]:
        if pr == self.cancel_pr:
            raise Cancelled(pr)
        return super().snapshot(pr, cancelled)


def test_a_change_of_the_open_detail_row_is_not_lost_when_its_detail_load_is_cancelled() -> None:
    source = CancellingSource([make_item(n, title=f"pr {n}") for n in (1, 2, 3)])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        await pilot.press("enter")
        await settle(app, pilot)
        shown = app._detail_pr
        assert shown is not None
        source.lights[shown]["ci_state"] = "FAILURE"
        source.cancel_pr = shown
        await pilot.press("R")  # the poll skips the row from its batch; the detail load dies
        await settle(app, pilot)
        source.cancel_pr = None
        source.calls.clear()
        await pilot.press("R")
        await settle(app, pilot)
        assert shown in source.reads()

    go(source, script)


def test_a_row_of_a_batch_that_never_arrived_is_read_by_the_next_poll() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        pr = prefix(GH, 2)
        app._poll_batch = {pr}  # a round cancelled by a newer one never adopted it
        source.calls.clear()
        await cycle(app, pilot)
        assert source.reads() == [pr]

    go(source, script)


def test_a_host_that_cannot_be_polled_is_remembered_for_the_session() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        source.answer.update(unsupported=["gitlab.example"], complete=False)
        await cycle(app, pilot)
        assert source.count("light") == 1 and source.count("prs") == 2
        source.calls.clear()
        await cycle(app, pilot)
        await pilot.press("R")
        await settle(app, pilot)
        assert source.count("light") == 0 and source.count("prs") == 4

    go(source, script)


def test_a_failing_poll_keeps_falling_back_each_cycle() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        source.light_error = ForgectlError("upstream_error", "boom")
        await cycle(app, pilot)
        source.calls.clear()
        await cycle(app, pilot)
        assert source.count("light") == 1 and source.count("prs") == 2

    go(source, script)


@pytest.mark.parametrize("raised", [False, True])
def test_a_rate_limited_poll_is_not_followed_by_prs_and_pauses_the_host(raised: bool) -> None:
    source = trio()
    clock = Clock()
    reset = (T0 + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        if raised:
            source.light_error = ForgectlError("rate_limited", "slow down")
        else:
            source.answer.update(
                complete=False,
                hosts_queried=["github.com"],
                errors=[{"host": "github.com", "code": "rate_limited", "message": "slow down"}],
                rate_limit={"host": "github.com", "remaining": 0, "reset_at": reset},
            )
        rows = list(app._rows)
        await cycle(app, pilot)
        assert source.count("prs") == 0 and source.reads() == []
        assert "rate_limited" in content_of(app, "#status").plain
        assert app._rates.low("github.com", clock.now)
        assert app._rows == rows  # the list on screen stays

    go(source, script, clock=clock)


def test_a_poll_names_a_cut_host_and_falls_back_whatever_else_failed() -> None:
    source = LightSource([make_item(1, repo=GH), make_item(1, repo=GL)])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        source.answer.update(
            complete=False,
            hosts_queried=["github.com", "gitlab.example"],
            truncated=["github.com"],
            errors=[{"host": "gitlab.example", "code": "upstream_error", "message": "x"}],
        )
        await cycle(app, pilot)
        assert source.count("prs") == 2
        assert "cut" in (app._list_note or "")

    go(source, script)


def test_the_kept_tuples_of_a_failed_host_survive_its_failure() -> None:
    source = LightSource([make_item(1, repo=GH), make_item(1, repo=GL)])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await seed(app, pilot, source)
        source.lights.pop(prefix(GL, 1))
        source.answer.update(
            complete=False,
            hosts_queried=["github.com", "gitlab.example"],
            errors=[{"host": "gitlab.example", "code": "upstream_error", "message": "x"}],
        )
        await cycle(app, pilot)
        assert prefix(GL, 1) in app._light_keys
        assert prefix(GH, 1) in app._light_keys

    go(source, script)


def test_a_manual_reload_under_the_light_path_keeps_the_every_nth_count() -> None:
    source = trio()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await cycle(app, pilot)
        assert app._auto_cycles == 1
        await pilot.press("R")
        await settle(app, pilot)
        assert app._auto_cycles == 1 and source.count("light") == 2

    go(source, script)


def test_the_poll_time_sorts_a_row_it_does_not_read() -> None:
    source = trio()
    current = {i["pr"]: Row(item=copy.deepcopy(i)) for i in source.authored}
    untouched = current[prefix(GH, 1)].item
    pr = prefix(GH, 3)
    source.lights[pr].update(updated_at=NEW, title="renamed", draft=True)
    listing = light.load_light(source, current)
    assert listing.rows[0].pr == pr and listing.rows[0] is current[pr]
    assert listing.rows[0].item["updated_at"] == NEW
    assert (listing.rows[0].item["title"], listing.rows[0].item["draft"]) == ("renamed", True)
    assert current[prefix(GH, 1)].item is untouched


_ = (asyncio, SHA_A)
