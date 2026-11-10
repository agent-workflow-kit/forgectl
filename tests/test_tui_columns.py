"""`browse` list columns, ordering, filters and progressive per-row snapshot loading.

The snapshots are built by ``snap`` on ``make_snapshot`` with the real ``activity`` block and
``head.pushed_at`` fields and no unknown keys; ``test_every_snap_shape_is_schema_valid``
validates each shape it produces against ``forgectl/cli/snapshot.json``. The ``ignore_authors``
rule is forgectl's (``forgectl.activity``); the list shows the counts as given, and
``test_an_ignored_bot_comment_after_the_push_does_not_rank_a_row_first`` runs a real derivation
through it.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from rich.cells import cell_len
from textual.pilot import Pilot
from textual.widgets import OptionList

from forgectl import model as m
from forgectl.activity import Viewer, derive
from forgectl.errors import ForgectlError
from forgectl.tui import columns
from forgectl.tui.app import BrowseApp
from forgectl.tui.source import Row

from .harness.schemas import validate
from .test_activity import author, comment, thread
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
    pushed_at: str | None = "2026-11-03T09:00:00Z",
    blocked: list[dict[str, Any]] | None = None,
    updated_at: str = "2026-11-03T11:30:00Z",
    title: str = "A change",
    complete: bool = True,
) -> dict[str, Any]:
    extra: dict[str, Any] = {
        "activity": act if act is not None else activity(),
        "review_decision": decision,
        "approvals": approvals,
        "blocked_by": blocked or [],
        "updated_at": updated_at,
    }
    if not complete:
        extra["complete"] = False
        extra["incomplete"] = ["threads"]
    if repo == GL:
        extra["backend"] = "gitlab"
    checks = [make_check("b", ci)] if ci != "none" else []
    s = make_snapshot(number, repo=repo, title=title, checks=checks, extra=extra)
    source = "push_event" if pushed_at is not None else None
    s["head"] = {**s["head"], "pushed_at": pushed_at, "pushed_at_source": source}
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
            blocked=[{"code": "checks_failing"}],
        ),
        prefix(GL, 2): snap(
            2,
            repo=GL,
            ci="passing",
            act=activity(total=5),
            decision="review_required",
            approvals=1,
            pushed_at=None,  # unknown push time: the push age is blank
            updated_at="2026-11-01T12:00:00Z",
        ),
    }

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        gh = prompt_of(app, prefix(GH, 1))
        assert "checks_failing" in gh and "✗" in gh  # badge with the first blocker, CI glyph
        assert "c12 u3 n2 r1" in gh
        assert "chg" in gh
        assert "3h/30m" in gh  # since push / since update
        assert "gh change" in gh
        gl = prompt_of(app, prefix(GL, 2))
        assert "draft" in gl and "✓" in gl
        assert " 1 " in gl and "1/" not in gl  # the bare approvals count on GitLab too
        assert "c5 u0 n0 r0" in gl and "req" in gl
        assert "-/2d" in gl  # no push time: blank (dash); updated two days ago

    app = BrowseApp(make_source(items, snaps), FakeOpener(), clock=lambda: NOW)

    async def go() -> None:
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(app, pilot)
            await script(app, pilot)

    asyncio.run(go())


def test_approvals_show_the_bare_count_on_both_hosts() -> None:
    # todo(#86): `1/2` once the snapshot exposes the required count; until then only the count
    assert columns.approvals_cell(columns.Summary(approvals=2)) == "2"
    assert columns.approvals_cell(columns.Summary()) == "?"
    for repo in (GH, GL):
        summary = columns.summarize(snap(1, repo=repo, approvals=1))
        assert columns.approvals_cell(summary) == "1"
    # a stray `approvals_left` is not a documented field: it changes nothing
    assert columns.summarize({"approvals": 1, "approvals_left": 1}).approvals == 1
    assert columns.approvals_cell(columns.summarize({"approvals": 1, "approvals_left": 1})) == "1"


def test_every_snap_shape_is_schema_valid() -> None:
    shapes = [
        snap(1),
        snap(2, act=activity(others=None), pushed_at=None),  # unknown push: others_since_push null
        snap(3, repo=GL, approvals=1),
        snap(4, complete=False),
        snap(5, ci="none"),
        snap(6, blocked=[{"code": "checks_failing"}], decision="changes_requested"),
    ]
    for shape in shapes:
        validate(shape, "forgectl/cli/snapshot.json")


def loaded(item: dict[str, Any], s: dict[str, Any]) -> list[str]:
    state = columns.RowState(columns.LOADED, columns.summarize(s))
    return columns.cells(Row(item=item, roles={"authored"}), state, NOW)


def test_a_snapshot_that_is_not_complete_shows_question_marks() -> None:
    item = make_item(1)
    done = loaded(item, snap(1, ci="none"))
    cut = loaded(item, snap(1, ci="none", complete=False))
    assert done[2] == "-" and not done[5].endswith("?")
    assert cut[2] == "?"  # a "none" answer from a partial read is not "no CI"
    assert cut[5] == "c0 u0 n0 r0?"
    # a known state is shown as it is, complete or not
    assert loaded(item, snap(1, ci="failing", complete=False))[2] == "✗"
    assert loaded(item, snap(1, ci="passing", complete=False))[2] == "✓"


def test_fixed_cells_never_overflow_so_the_later_columns_stay_aligned() -> None:
    wide_repo = "github.com/" + "日本語" * 20 + "/proj"
    cases = [
        (make_item(1, title="TITLE"), snap(1)),
        (make_item(2, title="TITLE"), snap(2, blocked=[{"code": "x" * 80}])),
        (
            make_item(3, title="TITLE"),
            snap(3, act=activity(100000, 100000, 100000, 100000), complete=False),
        ),
        (make_item(4, repo=wide_repo, title="TITLE"), snap(4, repo=wide_repo)),
        (make_item(5, title="題名" * 40), snap(5)),
        (make_item(6, title="emoji 🚀🚀🚀 " * 10), snap(6, approvals=123456789)),
    ]
    lines = [columns.row_line(loaded(i, s), 30) for i, s in cases]
    # the title is cut and padded too, so author and roles line up as well
    assert len({cell_len(line) for line in lines}) == 1, [cell_len(x) for x in lines]
    marked = [line for line, (i, _) in zip(lines, cases, strict=True) if i["title"] == "TITLE"]
    # every column before the title starts at the same cell offset, whatever the content
    assert len({cell_len(line[: line.index("TITLE")]) for line in marked}) == 1


def test_the_pr_column_is_cut_by_terminal_cell_width() -> None:
    wide = "github.com/" + "日" * 40 + "/x#1"
    parts = loaded(make_item(1, repo=wide), snap(1, repo=wide))
    assert cell_len(columns.row_line(parts, 20).split("  ")[0]) == 20
    assert columns.fit("日本語", 5) == "日本…"  # cells, not characters: 2 + 2 + the marker
    assert columns.fit("日本語", 4) == "日 …"  # a wide char that does not fit is padded
    assert columns.fit("abcdef", 4) == "abc…"
    assert columns.fit("ab", 4) == "ab  "


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


def test_order_sorts_updated_at_by_instant_and_puts_unparsable_last() -> None:
    def row(n: int, updated: Any) -> Row:
        item = make_item(n)
        item["updated_at"] = updated
        return Row(item=item)

    rows = [
        row(1, "2026-11-03T12:00:00+02:00"),  # 10:00Z: as text it would sort first
        row(2, "2026-11-03T11:00:00Z"),
        row(3, "2026-11-03T10:30:00.5z"),  # lowercase z and a fraction
        row(4, None),
        row(5, "last tuesday"),
        row(6, "2026-11-03T10:00:00"),  # no offset: not trusted
    ]
    ordered = columns.order(rows, {}, columns.SORT_REPO)
    assert [r.item["number"] for r in ordered] == [2, 3, 1, 4, 5, 6]
    assert columns.parse_time("2026-11-03T10:30:00z") is not None


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


def test_an_ignored_bot_comment_after_the_push_does_not_rank_a_row_first() -> None:
    push = "2026-11-03T09:00:00Z"
    later = "2026-11-03T10:00:00Z"
    bot = author("dependabot[bot]", "9001", bot=True)
    human = author("user-3", "1003")
    me = Viewer(login="user-1", id="1001")

    def derived(who: m.Author) -> dict[str, Any]:
        found = derive(
            threads=[thread(1, comment(1, who, later))],
            reviews=[],
            approvals=[],
            pushed_at=push,
            me=me,
            ignore_authors=["dependabot[bot]"],
        )
        return {
            "comments_total": found.comments_total,
            "unresolved_threads": found.unresolved_threads,
            "others_since_push": found.others_since_push,
            "awaiting_my_reply": found.awaiting_my_reply,
        }

    items = [make_item(1, title="bot", updated_at=later), make_item(2, title="human")]
    snaps = {prefix(GH, 1): snap(1, act=derived(bot)), prefix(GH, 2): snap(2, act=derived(human))}
    assert snaps[prefix(GH, 1)]["activity"]["others_since_push"] == 0

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("s")
        assert order_of(app) == [prefix(GH, 2), prefix(GH, 1)]  # the bot's row, newer, is not first

    driven(make_source(items, snaps), script)


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
    # a loading row has no roles here, so the role filters do not match it either
    assert not columns.matches(rows[0], states[rows[0].pr], {columns.FILTER_AUTHORED})
    rows[0].roles.add("authored")
    assert columns.matches(rows[0], states[rows[0].pr], {columns.FILTER_AUTHORED})
    assert not columns.matches(rows[0], states[rows[0].pr], {columns.FILTER_REQUESTED})
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


def wait_for(app: BrowseApp, check: Any, timeout: float = 10.0) -> Any:
    async def go(pilot: Pilot[None]) -> None:
        end = time.monotonic() + timeout
        while not check():
            assert time.monotonic() < end, "timed out"
            await pilot.pause(0.02)

    return go


def test_a_reload_mid_load_never_runs_more_snapshots_than_the_concurrency() -> None:
    source = thirty()
    app = BrowseApp(source, FakeOpener(), concurrency=4)

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await wait_for(app, lambda: len(source.started) >= 8)(pilot)
            # R is ignored while a refresh runs (#59), so start the newer round directly: the
            # old one still has reads in flight, and the one shared bound must hold.
            app._start_snapshots(None)
            await settle(app, pilot)

    asyncio.run(go())
    assert len(source.started) > 30  # the second round did run
    assert source.peak <= 4


def test_visible_rows_are_fetched_before_off_screen_ones_when_titles_are_long() -> None:
    source = thirty()
    for item in source.authored:
        item["title"] = "a very long title " * 20
    app = BrowseApp(source, FakeOpener(), concurrency=1)

    async def go() -> None:
        async with app.run_test(size=(80, 14)) as pilot:
            await settle(app, pilot)
            olist = app.query_one("#list", OptionList)
            assert olist.virtual_size.height == olist.option_count  # one line per row
            for _ in range(20):
                await pilot.press("down")
            wanted = highlighted_pr(app)
            first_round = len(source.started)
            await pilot.press("R")
            await settle(app, pilot)
            height = olist.size.height
            assert wanted in source.started[first_round : first_round + height]

    asyncio.run(go())


def highlighted_pr(app: BrowseApp) -> str:
    row = app._highlighted_row()
    assert row is not None
    return row.pr


def test_with_a_snapshot_filter_the_load_order_follows_the_display_order() -> None:
    items = [
        make_item(1, repo=GH, updated_at="2026-11-03T01:00:00Z"),
        make_item(2, repo=GL, updated_at="2026-11-03T02:00:00Z"),
        make_item(3, repo=GH, updated_at="2026-11-03T03:00:00Z"),
        make_item(4, repo=GL, updated_at="2026-11-03T04:00:00Z"),
    ]
    source = CountingSource(
        hold=0.01,
        authored=items,
        snapshots={i["pr"]: snap(i["number"], repo=i["repo"]) for i in items},
    )
    app = BrowseApp(source, FakeOpener(), concurrency=1)

    async def go() -> None:
        async with app.run_test(size=(140, 20)) as pilot:
            await settle(app, pilot)
            await pilot.press("f")  # ci failing: every row is hidden until a snapshot says so
            assert order_of(app) == []
            first = len(source.started)
            await pilot.press("R")
            await settle(app, pilot)
            expected = [r.pr for r in columns.order([Row(item=i) for i in items], {}, "repo")]
            assert source.started[first:] == expected
            assert expected != [i["pr"] for i in reversed(items)]  # not the listing order

    asyncio.run(go())


def test_an_arriving_snapshot_does_not_reorder_the_whole_list_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = thirty()
    calls = []
    real = columns.order

    def counting(*args: Any, **kw: Any) -> Any:
        calls.append(1)
        return real(*args, **kw)

    monkeypatch.setattr(columns, "order", counting)
    app = BrowseApp(source, FakeOpener(), concurrency=4)

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await settle(app, pilot)
            assert "snapshots 30/30" in content_of(app, "#bar").plain

    asyncio.run(go())
    assert len(calls) <= 3  # the list load and the odd mode switch, not one per snapshot


def test_the_highlighted_pr_survives_reorders_while_snapshots_arrive() -> None:
    items = [
        make_item(n, title=f"pr {n}", updated_at=f"2026-11-03T{n % 24:02d}:00:00Z")
        for n in range(1, 31)
    ]
    snaps = {
        i["pr"]: snap(i["number"], ci="failing" if i["number"] % 3 == 0 else "passing")
        for i in items
    }
    source = CountingSource(hold=0.02, authored=items, snapshots=snaps)
    app = BrowseApp(source, FakeOpener(), concurrency=2)

    async def go() -> None:
        async with app.run_test(size=(140, 14)) as pilot:
            await settle(app, pilot)
            await pilot.press("s")  # needs-attention order
            for _ in range(12):
                await pilot.press("down")
            wanted = highlighted_pr(app)
            await pilot.press("R")
            await wait_for(app, lambda: len(source.started) >= 60 - 10)(pilot)
            assert highlighted_pr(app) == wanted
            await settle(app, pilot)
            assert highlighted_pr(app) == wanted
            olist = app.query_one("#list", OptionList)
            index = [r.pr for r in app._visible].index(wanted)
            assert olist.scroll_y <= index < olist.scroll_y + olist.size.height

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
    source = make_source([item], {prefix(GH, 1): snap(1, blocked=[{"code": "[@click=app.quit]x"}])})

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        for prompt in list_prompts(app):
            assert prompt.spans == []
            assert "\x1b" not in prompt.plain
        assert "[@click=app.quit]" in list_prompts(app)[0].plain  # shown, not interpreted
        assert app.is_running

    driven(source, script)


def test_the_help_lists_the_sort_and_filter_keys() -> None:
    from forgectl.tui.app import HELP_KEYS

    for key in ("s", "a", "v", "f", "n"):
        assert key in [k for k, _ in HELP_KEYS], key
