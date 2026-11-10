"""The collapsible sections of the browse detail pane (#101)."""

from __future__ import annotations

import re

from textual.pilot import Pilot
from textual.widgets import Input, OptionList, Static

from forgectl.tui import render, sections
from forgectl.tui.app import HELP_KEYS, BrowseApp

from .test_tui_app import (
    PR1,
    PR2,
    basic_source,
    content_of,
    drive,
    settle,
)
from .tui_fakes import (
    FakeOpener,
    FakeSource,
    make_check,
    make_comment,
    make_diff,
    make_item,
    make_snapshot,
)

BODIES = {
    "facts": "#facts",
    "description": "#description",
    "changes": "#changes",
    "ci": "#ci",
    "comments": "#comments",
    "reviews": "#reviews",
}


def shown(app: BrowseApp, name: str) -> bool:
    return bool(app.query_one(BODIES[name]).display)


def head(app: BrowseApp, name: str) -> str:
    return content_of(app, f"#head-{name}").plain


async def open_pr1(app: BrowseApp, pilot: Pilot[None]) -> None:
    await pilot.press("down", "enter")  # the second row is PR1
    await settle(app, pilot)


def test_the_sections_are_numbered_in_the_order_they_are_rendered() -> None:
    assert sections.NAMES == ("facts", "description", "changes", "ci", "comments", "reviews")
    assert [sections.name_of(str(i)) for i in range(0, 8)] == [
        None,
        "facts",
        "description",
        "changes",
        "ci",
        "comments",
        "reviews",
        None,
    ]
    assert sections.name_of("١") is None  # a non-ASCII digit is no key


def test_headers_show_the_mark_the_number_and_a_count() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_pr1(app, pilot)
        assert head(app, "facts") == "▾ 1 Facts"
        assert head(app, "description") == "▾ 2 Description"
        assert head(app, "changes") == "▸ 3 Changes (+3 -1, 1 files)"
        assert head(app, "ci") == "▾ 4 CI (1, 1 failing)"
        assert head(app, "comments") == "▾ 5 Comments (1)"
        assert head(app, "reviews") == "▾ 6 Reviews (0)"
        assert [shown(app, n) for n in sections.NAMES] == [True, True, False, True, True, True]

    drive(basic_source(), script)


def test_each_number_key_toggles_only_its_section() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_pr1(app, pilot)
        for number, name in enumerate(sections.NAMES, 1):
            before = [shown(app, n) for n in sections.NAMES]
            await pilot.press(str(number))
            after = [shown(app, n) for n in sections.NAMES]
            index = number - 1
            assert after[index] is (not before[index]), name
            assert after[:index] + after[index + 1 :] == before[:index] + before[index + 1 :]
            await pilot.press(str(number))
            assert [shown(app, n) for n in sections.NAMES] == before

    drive(basic_source(), script)


def test_an_opened_section_shows_its_text_and_a_collapsed_one_keeps_its_count() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_pr1(app, pilot)
        await pilot.press("3")
        assert "+new line" in content_of(app, "#changes").plain
        assert head(app, "changes").startswith("▾ 3 Changes (")
        await pilot.press("5")
        assert shown(app, "comments") is False
        assert head(app, "comments") == "▸ 5 Comments (1)"

    drive(basic_source(), script)


def test_the_state_survives_selecting_another_pr_and_r() -> None:
    source = basic_source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_pr1(app, pilot)
        await pilot.press("3", "1")  # diff open, facts collapsed
        await pilot.press("escape")
        await pilot.press("up", "enter")  # PR2
        await settle(app, pilot)
        assert (shown(app, "changes"), shown(app, "facts")) == (True, False)
        assert head(app, "facts") == "▸ 1 Facts"
        await pilot.press("r")
        await settle(app, pilot)
        assert (shown(app, "changes"), shown(app, "facts")) == (True, False)

    drive(source, script)


def test_toggling_never_reads_the_forge_again() -> None:
    source = basic_source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_pr1(app, pilot)
        calls = list(source.calls)
        await pilot.press("3", "3", "4", "4", "1", "1")
        await settle(app, pilot)
        assert source.calls == calls
        assert [c for c in calls if c[0] == "diff"] == [("diff", PR1)]

    drive(source, script)


def test_the_collapsed_config_decides_what_starts_open() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_pr1(app, pilot)
        assert [shown(app, n) for n in sections.NAMES] == [True, True, True, False, False, True]

    drive(basic_source(), script, detail_collapsed=["ci", "comments"])


def test_an_empty_collapsed_list_opens_everything() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_pr1(app, pilot)
        assert all(shown(app, n) for n in sections.NAMES)
        assert "+new line" in content_of(app, "#changes").plain

    drive(basic_source(), script, detail_collapsed=[])


def test_number_keys_do_nothing_unless_the_detail_pane_has_focus() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_pr1(app, pilot)
        before = [shown(app, n) for n in sections.NAMES]
        app.query_one("#list", OptionList).focus()
        await pilot.press("1", "3", "4")
        assert [shown(app, n) for n in sections.NAMES] == before
        await pilot.press("slash", "1", "3")  # the filter takes the digits as text
        assert app.query_one("#filter", Input).value == "13"
        assert [shown(app, n) for n in sections.NAMES] == before

    drive(basic_source(), script)


def test_number_keys_work_with_the_checks_list_focused() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_pr1(app, pilot)
        await pilot.press("tab")
        assert app.focused is app.query_one("#checks")
        await pilot.press("1")
        assert shown(app, "facts") is False

    drive(basic_source(), script)


def test_o_on_a_check_works_when_ci_is_open_and_is_skipped_when_collapsed() -> None:
    opener = FakeOpener()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_pr1(app, pilot)
        await pilot.press("tab")
        await pilot.press("4")  # collapse CI while the list has focus
        assert app.query_one("#checks").display is False
        assert app.focused is not app.query_one("#checks")
        await pilot.press("o")  # opens the PR, not the check
        await settle(app, pilot)
        assert opener.opened == ["https://github.com/acme/widgets/pull/1"]
        await pilot.press("4", "tab", "o")
        await settle(app, pilot)
        assert opener.opened[-1] == "https://ci.example/b/1"

    drive(basic_source(), script, opener=opener)


def test_headers_hold_only_numbers_whatever_the_forge_sends() -> None:
    hostile = "[bold red]x[/]\x1b[2J"
    snap = make_snapshot(
        1,
        title=hostile,
        body=hostile,
        checks=[make_check(hostile, "failing", None)],
        comments=[make_comment(hostile)],
    )
    snap["additions"] = hostile
    snap["deletions"] = {"a": 1}
    snap["changed_files"] = True
    source = FakeSource(
        authored=[make_item(1)], snapshots={PR1: snap}, diffs={PR1: make_diff(PR1, hostile)}
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        for name in sections.NAMES:
            text = head(app, name)
            assert hostile not in text and "\x1b" not in text and "bold" not in text
            assert re.fullmatch(r"[▾▸] [1-6] [A-Za-z]+( \([0-9a-z+ ,?-]+\))?", text), text
            assert app.query_one(f"#head-{name}", Static).content.spans == []  # type: ignore[union-attr]
        assert head(app, "changes") == "▸ 3 Changes (+? -?, ? files)"

    drive(source, script)


def test_counts_are_computed_without_the_bodies() -> None:
    snap = make_snapshot(1, checks=[make_check("a", "failing"), make_check("b", "passing")])
    snap["threads"] = [{"comments": [{}, {}]}, {"comments": [{}]}, "junk"]
    snap["reviews"] = [{"state": "approved"}, "junk"]
    counts = render.counts_of(snap)
    assert counts["ci"] == "2, 1 failing"
    assert counts["comments"] == "3" and counts["reviews"] == "1"
    one = make_snapshot(1, checks=[make_check()])
    assert render.counts_of(one)["ci"] == "1"  # no failing: no failing part


def test_a_collapsed_section_is_not_painted_until_opened() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_pr1(app, pilot)
        assert content_of(app, "#changes").plain == ""  # collapsed: never painted
        await pilot.press("3")
        assert "+new line" in content_of(app, "#changes").plain

    drive(basic_source(), script)


def test_a_refresh_repaints_an_open_section_with_the_new_data() -> None:
    source = basic_source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_pr1(app, pilot)
        await pilot.press("3")
        source.snapshots[PR1] = make_snapshot(1, title="Mine", comments=[make_comment("later")])
        source.diffs[PR1] = make_diff(PR1, "@@ -1 +1 @@\n-a\n+fresh\n")
        await pilot.press("r")
        await settle(app, pilot)
        assert "later" in content_of(app, "#comments").plain
        assert "+fresh" in content_of(app, "#changes").plain

    drive(source, script)


def test_the_help_lists_the_number_keys() -> None:
    rows = dict(HELP_KEYS)
    assert "toggle a detail section" in rows["1-6"]


def test_the_pane_keeps_its_scroll_position_when_a_section_below_collapses() -> None:
    source = basic_source()
    source.snapshots[PR1] = make_snapshot(1, title="Mine", body="\n".join(["line"] * 200))

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_pr1(app, pilot)
        await pilot.press("3")
        pane = app.query_one("#detail")
        assert pane.max_scroll_y > 0
        pane.scroll_to(y=3, animate=False)
        await pilot.pause()
        y = pane.scroll_y
        assert y > 0
        await pilot.press("6")  # the last section: nothing above it moves
        await pilot.pause()
        assert pane.scroll_y == y

    drive(source, script)


def test_a_pr_without_data_still_has_all_six_headers() -> None:
    bare = make_snapshot(2, title="Theirs", body="", checks=[], comments=[], reviews=[])
    bare["additions"] = bare["deletions"] = bare["changed_files"] = 0
    source = basic_source()
    source.snapshots[PR2] = bare

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        assert [head(app, n)[2] for n in sections.NAMES] == list("123456")
        assert head(app, "ci") == "▾ 4 CI (0)"

    drive(source, script)


def test_the_ci_count_is_not_capped_with_the_check_list() -> None:
    checks = [make_check(f"c{i}", "failing") for i in range(600)]
    detail = render.detail_of(make_snapshot(1, checks=checks), None)
    assert detail.counts["ci"] == "600, 600 failing"
    assert len(detail.checks) < 600


def test_a_count_that_is_not_a_small_number_shows_a_question_mark() -> None:
    snap = make_snapshot(1)
    snap["additions"], snap["deletions"], snap["changed_files"] = -1, 10**9 + 1, True
    assert render.counts_of(snap)["changes"] == "+? -?, ? files"
    snap["additions"], snap["deletions"], snap["changed_files"] = 0, 10**9, 7
    assert render.counts_of(snap)["changes"] == f"+0 -{10**9}, 7 files"


def test_comments_may_use_the_budget_the_reviews_leave() -> None:
    body = "x" * 3_000
    many = [make_comment(body) for _ in range(60)]  # 180k chars: more than half the budget
    text = render.comments_text(make_snapshot(1, comments=many))
    assert len(text) > render.REST_BUDGET // 2
    assert "more not shown" not in text


def test_reviews_still_keep_half_the_budget_and_comments_get_the_rest() -> None:
    body = "y" * 3_000
    snap = make_snapshot(1, comments=[make_comment("c")])
    snap["reviews"] = [{"state": "commented", "author": {"login": "r"}, "body": body}] * 100
    assert len(render.reviews_text(snap)) <= render.REST_BUDGET // 2
    assert "more not shown" in render.reviews_text(snap)
    assert "more not shown" not in render.comments_text(snap)


def test_number_keys_work_after_a_refresh_hides_the_focused_check_list() -> None:
    source = basic_source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await open_pr1(app, pilot)
        await pilot.press("tab")
        assert app.focused is app.query_one("#checks")
        source.snapshots[PR1] = make_snapshot(1, title="Mine", checks=[])
        await pilot.press("r")
        await settle(app, pilot)
        assert app.query_one("#checks").display is False
        assert app.focused is not None
        await pilot.press("1")
        assert shown(app, "facts") is False

    drive(source, script)
