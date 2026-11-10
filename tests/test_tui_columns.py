"""`browse` list columns, ordering, filters and progressive per-row snapshot loading.

The snapshots here carry the `activity` block and `head.pushed_at` of docs/interface.md (#57,
#56) as plain fields, built by ``snap``; the base schemas of this branch may not list them yet,
so these fixtures are not schema-validated (the ones in test_tui_app.py are).
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from typing import Any

from textual.pilot import Pilot
from textual.widgets import OptionList

from forgectl.errors import ForgectlError
from forgectl.tui import columns
from forgectl.tui.app import BrowseApp
from forgectl.tui.source import Row

from .test_tui_app import content_of, drive, list_prompts, settle
from .tui_fakes import FakeOpener, FakeSource, make_check, make_item, make_snapshot

NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)
GH = "github.com/acme/widgets"
GL = "gitlab.example/team/proj"


def activity(
    total: int = 0, unresolved: int = 0, others: int | None = 0, awaiting: int = 0
) -> dict[str, Any]:
    return {
        "comments_total": total,
        "unresolved_threads": unresolved,
        "others_since_push": others,
        "awaiting_my_reply": awaiting,
    }


def snap(
    number: int,
    *,
    repo: str = GH,
    ci: str = "passing",
    act: dict[str, Any] | None = None,
    decision: str = "none",
    approvals: int = 0,
    left: int | None = None,
    pushed_at: str | None = "2026-11-03T09:00:00Z",
    blocked: list[dict[str, Any]] | None = None,
    updated_at: str = "2026-11-03T11:30:00Z",
    title: str = "A change",
) -> dict[str, Any]:
    extra: dict[str, Any] = {
        "activity": act if act is not None else activity(),
        "review_decision": decision,
        "approvals": approvals,
        "blocked_by": blocked or [],
        "updated_at": updated_at,
    }
    if left is not None:
        extra["approvals_left"] = left
    s = make_snapshot(number, repo=repo, title=title, checks=[make_check("b", ci)], extra=extra)
    s["head"] = {**s["head"], "pushed_at": pushed_at, "pushed_at_source": "push_event"}
    return s


def prefix(repo: str, n: int) -> str:
    return f"{repo}#{n}"


def make_source(
    items: list[dict[str, Any]], snaps: dict[str, dict[str, Any]], **kw: Any
) -> FakeSource:
    return FakeSource(authored=items, snapshots=snaps, **kw)


def driven(source: FakeSource, script: Any, **kw: Any) -> BrowseApp:
    return drive(source, script, concurrency=4, **kw)


def prompt_of(app: BrowseApp, pr: str) -> str:
    for p in list_prompts(app):
        if p.plain.startswith(pr):
            return p.plain
    raise AssertionError(f"{pr} not listed")


def order_of(app: BrowseApp) -> list[str]:
    return [p.plain.split()[0] for p in list_prompts(app)]


# -- columns ---------------------------------------------------------------------------------


def test_every_column_renders_for_a_mixed_github_and_gitlab_fixture() -> None:
    items = [
        make_item(1, repo=GH, title="gh change", updated_at="2026-11-03T11:30:00Z"),
        make_item(2, repo=GL, title="gl change", draft=True, updated_at="2026-11-01T12:00:00Z"),
    ]
    snaps = {
        prefix(GH, 1): snap(
            1,
            repo=GH,
            ci="failing",
            act=activity(total=12, unresolved=3, others=2, awaiting=1),
            decision="changes_requested",
            approvals=1,
            blocked=[{"code": "ci_failing", "detail": None}],
        ),
        prefix(GL, 2): snap(
            2,
            repo=GL,
            ci="passing",
            act=activity(total=5),
            decision="review_required",
            approvals=1,
            left=1,
            pushed_at=None,  # unknown push time: the push age is blank
            updated_at="2026-11-01T12:00:00Z",
        ),
    }

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        gh = prompt_of(app, prefix(GH, 1))
        assert "ci_failing" in gh and "✗" in gh  # badge with the first blocker, CI glyph
        assert "c12 u3 n2 r1" in gh
        assert "chg" in gh
        assert "3h/30m" in gh  # since push / since update
        assert "gh change" in gh
        gl = prompt_of(app, prefix(GL, 2))
        assert "draft" in gl and "✓" in gl
        assert " 1/2 " in gl  # approvals with the required count
        assert "c5 u0 n0 r0" in gl and "req" in gl
        assert "-/2d" in gl  # no push time: blank (dash); updated two days ago

    driven(make_source(items, snaps), script) if False else None
    app = BrowseApp(make_source(items, snaps), FakeOpener(), clock=lambda: NOW)

    async def go() -> None:
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(app, pilot)
            await script(app, pilot)

    asyncio.run(go())


def test_github_approvals_show_the_bare_count() -> None:
    assert columns.approvals_cell(columns.Summary(approvals=2)) == "2"
    assert columns.approvals_cell(columns.Summary(approvals=1, approvals_left=1)) == "1/2"
    assert columns.approvals_cell(columns.Summary()) == "?"


def test_ages_parse_times_and_tolerate_junk() -> None:
    assert columns.age("2026-11-03T11:59:30Z", NOW) == "0m"
    assert columns.age("2026-11-03T11:00:00.250000Z", NOW) == "59m"  # fractional form
    assert columns.age("2026-11-03T12:00:00Z", NOW) == "0m"
    assert columns.age("2026-11-04T12:00:00Z", NOW) == "0m"  # in the future: clamped
    assert columns.age("2026-10-20T12:00:00Z", NOW) == "2w"
    assert columns.age("2025-01-01T00:00:00Z", NOW) == "1y"
    assert columns.age(None, NOW) == ""
    assert columns.age("yesterday", NOW) == ""
    assert columns.age("2026-11-03T11:00:00", NOW) == ""  # no offset: not trusted
    assert columns.age(12345, NOW) == ""


def test_type_confused_snapshot_fields_become_unknown_not_a_crash() -> None:
    s = columns.summarize(
        {
            "ci": "oops",
            "approvals": True,
            "activity": {"comments_total": "3", "others_since_push": -1},
            "blocked_by": ["x"],
            "head": [],
        }
    )
    assert s == columns.Summary()
    assert columns.summarize({"activity": None}) == columns.Summary()


# -- ordering --------------------------------------------------------------------------------


def attention_source() -> FakeSource:
    n = {"quiet": 1, "failing": 2, "others": 3, "reply": 4, "changes": 5, "bot": 6}
    items = [
        make_item(num, title=name, updated_at=f"2026-11-03T0{num}:00:00Z")
        for name, num in n.items()
    ]
    snaps = {
        prefix(GH, 1): snap(1),
        prefix(GH, 2): snap(2, ci="failing"),
        prefix(GH, 3): snap(3, act=activity(total=2, unresolved=1, others=2)),
        prefix(GH, 4): snap(4, act=activity(total=2, unresolved=1, awaiting=1)),
        prefix(GH, 5): snap(5, decision="changes_requested"),
        # a bot's comments: forgectl already left them out of others_since_push (ignore_authors)
        prefix(GH, 6): snap(6, act=activity(total=9, unresolved=0, others=0)),
    }
    return make_source(items, snaps)


def test_default_order_groups_by_repo_then_newest_first() -> None:
    items = [
        make_item(1, repo=GL, updated_at="2026-11-03T10:00:00Z"),
        make_item(2, repo=GH, updated_at="2026-11-03T08:00:00Z"),
        make_item(3, repo=GH, updated_at="2026-11-03T09:00:00Z"),
        make_item(4, repo=GL, updated_at="2026-11-03T11:00:00Z"),
    ]
    snaps = {i["pr"]: snap(i["number"], repo=i["repo"]) for i in items}

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert order_of(app) == [prefix(GH, 3), prefix(GH, 2), prefix(GL, 4), prefix(GL, 1)]

    driven(make_source(items, snaps), script)


def test_needs_attention_order_puts_failing_new_comments_reply_changes_first() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("s")
        # failing CI, new comments from others, awaiting my reply, changes requested, the rest
        # (the rest by newest update: the bot's row has no more rank than the quiet one)
        assert order_of(app) == [prefix(GH, n) for n in (2, 3, 4, 5, 6, 1)]
        assert "order: attention" in content_of(app, "#bar").plain
        await pilot.press("s")
        assert "order: repo" in content_of(app, "#bar").plain

    driven(attention_source(), script)


def test_a_row_without_a_loaded_snapshot_is_in_the_rest() -> None:
    row = Row(item=make_item(1))
    assert columns.attention_rank(columns.RowState()) == 4
    assert columns.attention_rank(columns.RowState(columns.FAILED, error="x")) == 4
    assert columns.order([row], {}, columns.SORT_ATTENTION) == [row]


# -- filters ---------------------------------------------------------------------------------


def filter_source() -> FakeSource:
    mine_failing = make_item(1, title="mine failing")
    mine_new = make_item(2, title="mine new")
    theirs_failing = make_item(3, title="theirs failing")
    theirs_new_failing = make_item(4, title="theirs both")
    snaps = {
        prefix(GH, 1): snap(1, ci="failing"),
        prefix(GH, 2): snap(2, act=activity(others=1)),
        prefix(GH, 3): snap(3, ci="failing"),
        prefix(GH, 4): snap(4, ci="failing", act=activity(others=3)),
    }
    return FakeSource(
        authored=[mine_failing, mine_new],
        requested=[theirs_failing, theirs_new_failing],
        snapshots=snaps,
    )


def test_each_filter_narrows_and_combinations_intersect() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        bar = lambda: content_of(app, "#bar").plain  # noqa: E731
        assert len(order_of(app)) == 4
        await pilot.press("a")
        assert order_of(app) == [prefix(GH, 1), prefix(GH, 2)]
        assert "filters: authored" in bar()
        await pilot.press("f")  # authored AND ci failing
        assert order_of(app) == [prefix(GH, 1)]
        assert "filters: authored, ci-failing" in bar()
        await pilot.press("a", "v")  # review requested AND ci failing
        assert order_of(app) == [prefix(GH, 3), prefix(GH, 4)]
        await pilot.press("n")  # + new comments from others
        assert order_of(app) == [prefix(GH, 4)]
        assert "filters: requested, ci-failing, new-comments" in bar()
        await pilot.press("v", "f")  # new comments only
        assert order_of(app) == [prefix(GH, 2), prefix(GH, 4)]
        await pilot.press("n")
        assert len(order_of(app)) == 4
        assert "filters" not in bar()

    driven(filter_source(), script)


def test_authored_and_requested_together_keep_only_prs_that_are_both() -> None:
    both = make_item(1, title="both")
    source = FakeSource(
        authored=[both, make_item(2)],
        requested=[both, make_item(3)],
        snapshots={prefix(GH, n): snap(n) for n in (1, 2, 3)},
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("a", "v")
        assert order_of(app) == [prefix(GH, 1)]

    driven(source, script)


def test_snapshot_filters_hide_rows_until_their_snapshot_arrives() -> None:
    rows = [Row(item=make_item(1))]
    states = {prefix(GH, 1): columns.RowState()}
    assert not columns.matches(rows[0], states[rows[0].pr], {columns.FILTER_CI_FAILING})
    assert columns.matches(rows[0], states[rows[0].pr], {columns.FILTER_AUTHORED} - {"authored"})
    failed = columns.RowState(columns.FAILED, error="x")
    assert not columns.matches(rows[0], failed, {columns.FILTER_NEW_COMMENTS})


# -- progressive loading ---------------------------------------------------------------------


class CountingSource(FakeSource):
    """Records the peak number of snapshots running at once and the order they started in."""

    def __init__(self, *, hold: float = 0.05, **kw: Any) -> None:
        super().__init__(**kw)
        self.hold = hold
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.started: list[str] = []
        self.failing: set[str] = set()

    def snapshot(self, pr: str) -> dict[str, Any]:
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.started.append(pr)
        try:
            time.sleep(self.hold)
            if pr in self.failing:
                raise ForgectlError("upstream_error", "boom <b>x</b>")
            return super().snapshot(pr)
        finally:
            with self.lock:
                self.running -= 1


def thirty() -> CountingSource:
    items = [
        make_item(n, title=f"pr {n}", updated_at=f"2026-11-03T{n % 24:02d}:00:00Z")
        for n in range(1, 31)
    ]
    return CountingSource(authored=items, snapshots={i["pr"]: snap(i["number"]) for i in items})


def test_no_more_snapshots_run_at_once_than_the_concurrency() -> None:
    source = thirty()
    app = BrowseApp(source, FakeOpener(), concurrency=4)

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await settle(app, pilot)

    asyncio.run(go())
    assert len(source.started) == 30
    assert 2 <= source.peak <= 4


def test_visible_rows_are_fetched_before_off_screen_ones() -> None:
    source = thirty()
    app = BrowseApp(source, FakeOpener(), concurrency=2)

    async def go() -> None:
        async with app.run_test(size=(140, 12)) as pilot:  # about ten rows fit
            await settle(app, pilot)
            height = app.query_one("#list", OptionList).size.height
            assert 3 <= height < 15
            shown = [p.plain.split()[0] for p in list_prompts(app)]
            assert set(source.started[:height]) == set(shown[:height])

    asyncio.run(go())


def test_rows_render_as_each_snapshot_arrives_with_a_loading_state_and_progress() -> None:
    items = [make_item(1, title="fast"), make_item(2, title="slow")]
    gate = threading.Event()

    class Gated(FakeSource):
        def snapshot(self, pr: str) -> dict[str, Any]:
            if pr == prefix(GH, 2):
                gate.wait(10)
            return super().snapshot(pr)

    source = Gated(authored=items, snapshots={prefix(GH, n): snap(n, ci="failing") for n in (1, 2)})
    app = BrowseApp(source, FakeOpener(), concurrency=2)

    async def go() -> None:
        async with app.run_test(size=(140, 20)) as pilot:
            for _ in range(100):
                await pilot.pause(0.02)
                if "✗" in prompt_of(app, prefix(GH, 1)):
                    break
            assert "✗" in prompt_of(app, prefix(GH, 1))
            slow = prompt_of(app, prefix(GH, 2))
            assert "loading" in slow and "✗" not in slow
            assert "snapshots 1/2" in content_of(app, "#bar").plain
            gate.set()
            await settle(app, pilot)
            assert "✗" in prompt_of(app, prefix(GH, 2))
            assert "snapshots 2/2" in content_of(app, "#bar").plain

    asyncio.run(go())


def test_one_failing_snapshot_marks_its_row_and_the_rest_load() -> None:
    source = thirty()
    bad = prefix(GH, 7)
    source.failing.add(bad)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        row = prompt_of(app, bad)
        assert "ERROR upstream_error" in row and "<b>" not in row
        assert row.count("loading") == 0
        others = [p.plain for p in list_prompts(app) if not p.plain.startswith(bad + " ")]
        assert len(others) == 29
        assert all("✓" in p for p in others)
        assert "snapshots 30/30 (1 failed)" in content_of(app, "#bar").plain

    driven(source, script)


def test_a_source_bug_marks_only_its_row() -> None:
    source = make_source(
        [make_item(1), make_item(2)],
        {prefix(GH, 1): "not a dict", prefix(GH, 2): snap(2)},  # type: ignore[dict-item]
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert "ERROR internal" in prompt_of(app, prefix(GH, 1))
        assert "✓" in prompt_of(app, prefix(GH, 2))

    driven(source, script)


def test_a_reload_starts_a_fresh_round_of_snapshots() -> None:
    source = make_source([make_item(1)], {prefix(GH, 1): snap(1)})

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("R")
        await settle(app, pilot)
        assert [c for c in source.calls if c[0] == "snapshot"] == [("snapshot", prefix(GH, 1))] * 2
        assert "snapshots 1/1" in content_of(app, "#bar").plain

    driven(source, script)


# -- hostile text ----------------------------------------------------------------------------


def test_forge_text_in_titles_and_blockers_cannot_act_as_markup() -> None:
    hostile = "[@click=app.quit]x[/] [b]bold[/b] \x1b[31mred"
    item = make_item(1, title=hostile)
    source = make_source(
        [item], {prefix(GH, 1): snap(1, blocked=[{"code": "[@click=app.quit]x", "detail": None}])}
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        for prompt in list_prompts(app):
            assert prompt.spans == []
            assert "\x1b" not in prompt.plain
        assert "[@click=app.quit]" in list_prompts(app)[0].plain  # shown, not interpreted
        assert app.is_running

    driven(source, script)


def test_the_help_lists_the_sort_and_filter_keys() -> None:
    from forgectl.tui.app import HELP

    for key in ("s ", "a ", "v ", "f ", "n "):
        assert any(line.startswith(key) for line in HELP.splitlines()), key
