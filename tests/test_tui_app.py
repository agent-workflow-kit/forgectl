"""Textual pilot tests for `browse`: behavior, keys, and hostile forge text."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from textual.content import Content
from textual.pilot import Pilot
from textual.widgets import Input, OptionList, Static

from forgectl.errors import ForgectlError
from forgectl.tui import render, sections
from forgectl.tui.app import BrowseApp
from forgectl.tui.opener import BrowserOpener

from .harness.schemas import validate
from .tui_fakes import (
    FakeOpener,
    FakeSource,
    GatedSource,
    make_check,
    make_comment,
    make_diff,
    make_item,
    make_snapshot,
)

PR1 = "github.com/acme/widgets#1"
PR2 = "github.com/acme/widgets#2"
PR3 = "github.com/acme/widgets#3"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))


def drive(
    source: FakeSource,
    script: Callable[[BrowseApp, Pilot[None]], Awaitable[None]],
    *,
    opener: Any = None,
    focus: str | None = None,
    wait: bool = True,
    concurrency: int = 0,  # 0: no per-row snapshots, so these tests count only their own reads
    **app_args: Any,
) -> BrowseApp:
    app = BrowseApp(
        source, opener or FakeOpener(), focus=focus, concurrency=concurrency, **app_args
    )

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            if wait:
                await settle(app, pilot)
            else:
                await pilot.pause()
            await script(app, pilot)

    asyncio.run(go())
    return app


async def settle(app: BrowseApp, pilot: Pilot[None]) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def as_content(value: object) -> Content:
    assert isinstance(value, Content)
    return value


def content_of(app: BrowseApp, selector: str) -> Content:
    return as_content(app.query_one(selector, Static).content)


def list_prompts(app: BrowseApp) -> list[Content]:
    olist = app.query_one("#list", OptionList)
    return [as_content(olist.get_option_at_index(i).prompt) for i in range(olist.option_count)]


def basic_source() -> FakeSource:
    return FakeSource(
        authored=[make_item(1, title="Mine", updated_at="2026-11-03T10:00:00Z")],
        requested=[
            make_item(2, title="Theirs", login="carol", updated_at="2026-11-03T11:00:00Z"),
            make_item(1, title="Mine", updated_at="2026-11-03T10:00:00Z"),
        ],
        snapshots={
            PR1: make_snapshot(
                1,
                title="Mine",
                body="the body",
                checks=[make_check("build", "failing", "https://ci.example/b/1")],
                comments=[make_comment("please rename", "bob")],
            ),
            PR2: make_snapshot(2, title="Theirs"),
        },
        diffs={PR1: make_diff(PR1, "@@ -1 +1 @@\n-old line\n+new line\n")},
    )


def test_fixtures_are_schema_valid() -> None:
    source = basic_source()
    validate(source.prs(author="me"), "forgectl/cli/prs.json")
    validate(source.snapshot(PR1), "forgectl/cli/snapshot.json")
    validate(source.diff(PR1), "forgectl/cli/diff.json")


def test_list_is_the_union_of_two_prs_calls_newest_first() -> None:
    source = basic_source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        prompts = [p.plain for p in list_prompts(app)]
        assert len(prompts) == 2  # the PR in both lists appears once
        assert PR2 in prompts[0] and "Theirs" in prompts[0]  # updated_at descending
        assert PR1 in prompts[1] and "authored+requested" in prompts[1]

    drive(source, script)
    assert source.calls[:2] == [
        ("prs", {"author": None, "review_requested_from": "me"}),
        ("prs", {"author": "me", "review_requested_from": None}),
    ]


def test_enter_opens_the_detail_pane_with_facts_ci_reviews_comments_and_diff() -> None:
    source = basic_source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert app.query_one("#detail").display is False
        await pilot.press("down", "enter")  # second row: PR1
        await settle(app, pilot)
        assert app.query_one("#detail").display is True
        assert "Mine" in content_of(app, "#facts").plain
        assert "the body" in content_of(app, "#description").plain
        assert "CI: failing" in content_of(app, "#ci").plain
        checks = app.query_one("#checks", OptionList)
        assert "build" in as_content(checks.get_option_at_index(0).prompt).plain
        assert "please rename" in content_of(app, "#comments").plain
        assert "Reviews" in content_of(app, "#head-reviews").plain
        await pilot.press("3")  # the diff starts collapsed
        assert "+new line" in content_of(app, "#changes").plain

    app = drive(source, script)
    assert app.result() == {"intent": "back", "selected": [PR1], "merged": []}


def test_quit_without_enter_selects_nothing() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("q")

    app = drive(basic_source(), script)
    assert app.result() == {"intent": "back", "selected": [], "merged": []}


def test_filter_narrows_the_list_and_escape_clears_it() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("slash")
        await pilot.press(*"carol")  # q-less on purpose: typing goes to the filter box
        await pilot.pause()
        assert len(list_prompts(app)) == 1
        assert "Theirs" in list_prompts(app)[0].plain
        await pilot.press("escape")
        await pilot.pause()
        assert len(list_prompts(app)) == 2

    drive(basic_source(), script)


def test_q_typed_into_the_filter_does_not_quit() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("slash", "q")
        await pilot.pause()
        assert app.is_running
        assert len(list_prompts(app)) == 0

    drive(basic_source(), script)


def test_help_lists_the_keys_and_any_key_closes_it() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("question_mark")
        await pilot.pause()
        help_text = as_content(app.screen.query_one("#help", Static).content).plain
        for key in ("q ", "? ", "/ ", "enter", "o ", "r ", "R "):
            assert key in help_text
        assert "refresh selected" in help_text and "refresh all" in help_text
        await pilot.press("x")
        await pilot.pause()
        assert not app.screen.query("#help")

    drive(basic_source(), script)


def test_refresh_keys_refetch() -> None:
    source = basic_source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        await pilot.press("r")
        await settle(app, pilot)
        await pilot.press("R")
        await settle(app, pilot)

    drive(source, script)
    # r refetches the open PR; R reloads both lists and then the visible detail pane
    assert [c for c in source.calls if c[0] == "snapshot"] == [("snapshot", PR1)] * 3
    assert len([c for c in source.calls if c[0] == "prs"]) == 4


def test_there_is_no_write_action() -> None:
    source = basic_source()
    opener = FakeOpener()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        for key in ("m", "p", "c", "d", "x", "M"):
            await pilot.press(key)
        await settle(app, pilot)

    app = drive(source, script, opener=opener)
    bound = {b.key if hasattr(b, "key") else b[0] for b in BrowseApp.BINDINGS}  # type: ignore[index]
    assert bound == {
        "q",
        "question_mark",
        "slash",
        "escape",
        "o",
        "r",
        "R",
        "s",
        "a",
        "v",
        "f",
        "n",
        *"123456",  # the detail sections: view only
    }
    assert not hasattr(app, "action_merge")
    assert opener.opened == []
    assert {c[0] for c in source.calls} <= {"prs", "snapshot", "diff"}
    assert app.result()["merged"] == []


def test_o_on_the_list_opens_the_built_url_and_sets_the_intent() -> None:
    opener = FakeOpener()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("o")
        await settle(app, pilot)

    app = drive(basic_source(), script, opener=opener)
    assert opener.opened == ["https://github.com/acme/widgets/pull/2"]
    result = app.result()
    assert result == {"intent": "open", "selected": [PR2], "merged": []}
    validate(result, "forgectl/cli/browse-result.json")


def test_o_in_the_detail_pane_opens_the_pr_url() -> None:
    opener = FakeOpener()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        await pilot.press("o")
        await settle(app, pilot)

    drive(basic_source(), script, opener=opener)
    assert opener.opened == ["https://github.com/acme/widgets/pull/1"]


def test_o_on_a_check_opens_only_that_checks_http_url() -> None:
    opener = FakeOpener()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        app.query_one("#checks", OptionList).focus()
        app.query_one("#checks", OptionList).highlighted = 0
        await pilot.pause()
        await pilot.press("o")
        await settle(app, pilot)

    app = drive(basic_source(), script, opener=opener)
    assert opener.opened == ["https://ci.example/b/1"]
    assert app.result()["intent"] == "open"


@pytest.mark.parametrize(
    "bad_url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com/x",
        "ssh://host/x",
        "//example.com/x",
        "https://a b/",
        "https://example.com/\x1b]0;x\x07",
        "https://example.com/\nfoo",
        "https://exämple.com/",
        "http:/x",
        "",
        "https://" + "a" * 5000,
    ],
)
def test_a_check_url_with_another_scheme_is_shown_but_never_opened(bad_url: str) -> None:
    opener = FakeOpener()
    source = FakeSource(
        authored=[make_item(1)],
        snapshots={PR1: make_snapshot(1, checks=[make_check("lint", "failing", bad_url)])},
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        checks = app.query_one("#checks", OptionList)
        label = as_content(checks.get_option_at_index(0).prompt)
        assert "lint" in label.plain
        assert label.spans == []  # never markup
        checks.focus()
        checks.highlighted = 0
        await pilot.pause()
        await pilot.press("o")
        await settle(app, pilot)
        assert "not opened" in content_of(app, "#status").plain

    app = drive(source, script, opener=opener)
    assert opener.opened == []
    assert app.result()["intent"] == "back"


@pytest.mark.parametrize("url", ["http://github.com/a/b/pull/1", "javascript:x", "file:///x"])
def test_the_pr_url_must_be_https(url: str) -> None:
    opener = FakeOpener()
    item = make_item(1)
    item["url"] = url
    source = FakeSource(authored=[item])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("o")
        await settle(app, pilot)

    drive(source, script, opener=opener)
    assert opener.opened == []


HOSTILE_TITLES = [
    "[" + "@click=app.quit]click me[/]",
    "[bold red]loud[/bold red] [link=https://evil.example]x[/link]",
    "[on red]x[/] [" + "@click=app.bell]y[/] [/]",
    "\x1b]0;pwned\x07\x1b[31mred\x1b[0m \x1b]8;;https://evil.example\x1b\\link\x1b]8;;\x1b\\",
    "abc‮def⁦ghi⁩",
    "tab\there\nnewline\rcr",
    "\ud800 lone surrogate",
]


@pytest.mark.parametrize("title", HOSTILE_TITLES)
def test_hostile_titles_render_inert_in_the_list_the_detail_and_the_screen(title: str) -> None:
    source = FakeSource(
        authored=[make_item(1, title=title, login=title, labels=[title])],
        snapshots={
            PR1: make_snapshot(
                1,
                title=title,
                body=title,
                login=title,
                branch=title,
                checks=[make_check(title, "pending", None)],
                comments=[make_comment(title, title)],
            )
        },
        diffs={PR1: make_diff(PR1, title)},
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        contents = [
            list_prompts(app)[0],
            *[content_of(app, f"#{name}") for name in sections.NAMES],
            *[content_of(app, f"#head-{name}") for name in sections.NAMES],
            as_content(app.query_one("#checks", OptionList).get_option_at_index(0).prompt),
        ]
        for content in contents:
            assert content.spans == []  # no styles, no click actions
            for ch in content.plain:
                assert ch in "\t\n" or ord(ch) >= 0x20 and not 0x7F <= ord(ch) < 0xA0
            assert "‮" not in content.plain and "\x1b" not in content.plain
        screen = app.export_screenshot()
        assert "\x1b" not in screen
        assert app.is_running  # nothing in the text ran an action such as app.quit

    app = drive(source, script)
    assert app.result()["intent"] == "back"


def test_the_markup_title_is_shown_literally() -> None:
    title = "[" + "@click=app.quit]click me[/]"
    source = FakeSource(authored=[make_item(1, title=title)])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert title in list_prompts(app)[0].plain
        olist = app.query_one("#list", OptionList)
        line0 = olist.render_line(0).text  # the first content line of the list, as drawn
        column = line0.index(title[:9]) + 3  # three characters into the title
        assert line0[column - 3 : column + 6] == title[:9]  # the offset is on the title text
        inner = olist.content_region
        x = inner.x - olist.region.x + column  # widget-relative: past border and padding
        y = inner.y - olist.region.y
        widget, _ = app.screen.get_widget_at(inner.x + column, inner.y)
        assert widget is olist
        await pilot.click("#list", offset=(x, y))  # a click on the text runs nothing
        await pilot.pause()
        assert app.is_running

    drive(source, script)


def test_control_and_bidi_characters_become_visible_markers() -> None:
    source = FakeSource(authored=[make_item(1, title="a‮b\x1b[31mc")])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        text = list_prompts(app)[0].plain
        assert "<U+202E>" in text and "<U+001B>" in text

    drive(source, script)


def test_huge_fields_are_bounded() -> None:
    huge = "x" * 5_000_000
    source = FakeSource(
        authored=[make_item(1, title=huge)],
        snapshots={
            PR1: make_snapshot(
                1,
                title=huge,
                body=huge,
                comments=[make_comment(huge)] * 300,
                checks=[make_check(huge, "passing", "https://e.example/" + "a" * 10_000)],
            )
        },
        diffs={PR1: make_diff(PR1, "+" + "y" * 20_000_000)},
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        assert len(list_prompts(app)[0].plain) < 1_000
        assert len(content_of(app, "#facts").plain) < 25_000
        await pilot.press("3")
        for name in ("description", "comments", "changes"):
            assert len(content_of(app, f"#{name}").plain) < 2_000_000

    drive(source, script)


def test_errors_from_the_source_reach_the_status_line_inert() -> None:
    class Failing(FakeSource):
        def prs(self, **kwargs: Any) -> dict[str, Any]:
            raise ForgectlError("upstream_error", "[" + "@click=app.quit]boom\x1b[31m")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        status = content_of(app, "#status")
        assert status.spans == []
        assert "boom" in status.plain and "\x1b" not in status.plain
        assert app.is_running

    drive(Failing(), script)


def test_partial_errors_and_incomplete_lists_show_in_the_status_line() -> None:
    source = basic_source()
    source.errors = [{"host": "gitlab.com", "code": "auth_required", "message": "no token"}]

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        status = content_of(app, "#status").plain
        assert re.search(r"(?<![\w.])gitlab\.com(?![\w.-])", status) and "auth_required" in status
        assert len(list_prompts(app)) == 2  # the rest is still shown

    drive(source, script)


def test_a_failing_detail_read_keeps_the_list_usable() -> None:
    source = basic_source()
    source.snapshots.pop(PR2)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")  # PR2 has no snapshot: the fake raises KeyError
        await settle(app, pilot)
        assert "internal: KeyError" in content_of(app, "#status").plain
        assert app.is_running
        assert len(list_prompts(app)) == 2

    app = drive(source, script)
    assert app.result()["selected"] == []


def test_focus_opens_the_detail_even_when_the_pr_is_not_listed() -> None:
    source = FakeSource(snapshots={PR3: make_snapshot(3, title="Elsewhere")})

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert app.query_one("#detail").display is True
        assert "Elsewhere" in content_of(app, "#facts").plain

    app = drive(source, script, focus=PR3)
    assert app.result() == {"intent": "back", "selected": [PR3], "merged": []}


def test_every_result_validates_against_the_schema() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)

    app = drive(basic_source(), script)
    validate(app.result(), "forgectl/cli/browse-result.json")


async def wait_until(pilot: Pilot[None], check: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "condition never held"
        await pilot.pause(0.02)


def status_of(app: BrowseApp) -> str:
    return content_of(app, "#status").plain


@pytest.mark.parametrize("bad", [{"labels": 5}, {"ci": {"checks": 5}}, {"threads": 7}])
def test_type_confused_data_reaches_the_status_line_and_the_app_keeps_running(
    bad: dict[str, Any],
) -> None:
    source = basic_source()
    source.snapshots[PR1].update(bad)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert app.is_running
        assert "internal: TypeError" in status_of(app)

    app = drive(source, script, focus=PR1)
    assert not app.return_code  # run_test re-raises a recorded crash; none happened


def test_a_list_item_of_the_wrong_type_reaches_the_status_line() -> None:
    source = basic_source()
    source.authored = [{"pr": PR1, "title": 5, "labels": 5, "author": "x"}]  # type: ignore[list-item]

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert app.is_running
        await pilot.press("slash", "a")
        await pilot.pause()
        assert app.is_running

    app = drive(source, script)
    assert not app.return_code  # run_test re-raises a recorded crash; none happened


def test_R_during_a_load_is_ignored_with_a_note() -> None:
    source = gated_basic("prs")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await wait_until(pilot, lambda: len(source.calls) == 1)
        await pilot.press("R", "R", "R")
        await pilot.pause()
        assert "already running" in status_of(app)
        source.gate.set()
        await settle(app, pilot)

    drive(source, script, wait=False)
    assert [c[0] for c in source.calls] == ["prs", "prs"]


def gated_basic(*gated: str) -> GatedSource:
    base = basic_source()
    return GatedSource(
        gated=gated,
        authored=base.authored,
        requested=base.requested,
        snapshots=base.snapshots,
        diffs=base.diffs,
    )


def test_repeated_enter_and_r_on_the_same_pr_load_it_once() -> None:
    source = gated_basic("snapshot")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await wait_until(pilot, lambda: ("snapshot", PR1) in source.calls)
        await pilot.press("enter", "r", "enter", "r")
        await pilot.pause()
        assert "already running" in status_of(app)
        source.gate.set()
        await settle(app, pilot)

    drive(source, script)
    assert [c for c in source.calls if c[0] == "snapshot"] == [("snapshot", PR1)]


def test_a_load_for_another_pr_replaces_the_stale_one() -> None:
    source = gated_basic("snapshot")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await wait_until(pilot, lambda: ("snapshot", PR1) in source.calls)
        await pilot.press("up", "enter")
        await wait_until(pilot, lambda: ("snapshot", PR2) in source.calls)
        source.gate.set()
        await settle(app, pilot)
        assert "Theirs" in content_of(app, "#facts").plain

    drive(source, script)


def test_a_diff_error_stays_in_the_final_status() -> None:
    class DiffFails(FakeSource):
        def diff(self, pr: str) -> dict[str, Any]:
            raise ForgectlError("not_found", "no diff\x1b[31m")

    base = basic_source()
    source = DiffFails(authored=base.authored, snapshots=base.snapshots)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        status = status_of(app)
        assert "1 checks" in status and "diff: not_found: no diff" in status
        assert "\x1b" not in status

    drive(source, script)


def test_a_list_error_survives_the_detail_load() -> None:
    class PrsFails(FakeSource):
        def prs(self, **kwargs: Any) -> dict[str, Any]:
            if kwargs.get("author"):
                raise ForgectlError("upstream_error", "authored list down")
            return super().prs(**kwargs)

    base = basic_source()
    source = PrsFails(requested=base.requested, snapshots=base.snapshots)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        status = status_of(app)
        assert "authored list down" in status and "list incomplete" in status
        assert "checks" in status

    drive(source, script, focus=PR1)


def test_the_rest_of_the_pane_has_one_total_budget() -> None:
    hostile = "\x1b" * 4_000
    threads = [
        {
            "id": f"gh:rt:T{i}",
            "resolved": False,
            "path": "f.py",
            "line": 1,
            "comments": [make_comment(hostile, "bob", n) for n in range(50)],
        }
        for i in range(200)
    ]
    snap = make_snapshot(1)
    snap["threads"] = threads
    started = time.monotonic()
    body = render.comments_text(snap)
    assert time.monotonic() - started < 2.0
    footer = body.rsplit("\n", 1)[-1]
    assert footer.strip().startswith("...") and footer.endswith("more not shown")
    assert len(body) <= render.REST_BUDGET // 2 + len(footer) + 1

    source = FakeSource(authored=[make_item(1)], snapshots={PR1: snap})

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        shown = content_of(app, "#comments").plain
        assert "more not shown" in shown and len(shown) < render.REST_BUDGET + 1_000

    started = time.monotonic()
    drive(source, script)
    assert time.monotonic() - started < 15.0


def test_a_detail_arriving_does_not_steal_focus_from_the_filter() -> None:
    source = gated_basic("snapshot")
    opener = FakeOpener()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await wait_until(pilot, lambda: ("snapshot", PR2) in source.calls)
        await pilot.press("slash", *"ca")
        source.gate.set()
        await settle(app, pilot)
        assert app.query_one("#detail").display is True
        await pilot.press("o", "space", "q")
        await pilot.pause()
        assert app.query_one("#filter", Input).value == "cao q"
        assert app.is_running

    drive(source, script, opener=opener)
    assert opener.opened == []


def test_selected_is_the_pr_the_last_o_opened() -> None:
    opener = FakeOpener()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("o")  # row 0 is PR2
        await settle(app, pilot)
        await pilot.press("down", "enter")  # PR1's detail
        await settle(app, pilot)

    app = drive(basic_source(), script, opener=opener)
    assert opener.opened == ["https://github.com/acme/widgets/pull/2"]
    assert app.result() == {"intent": "open", "selected": [PR2], "merged": []}


def test_o_on_the_checks_list_opens_the_first_check_without_arrowing() -> None:
    opener = FakeOpener()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        await pilot.press("tab")
        assert app.focused is app.query_one("#checks")
        await pilot.press("o")
        await settle(app, pilot)

    drive(basic_source(), script, opener=opener)
    assert opener.opened == ["https://ci.example/b/1"]


def test_o_with_nothing_to_open_says_so() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("o")
        await settle(app, pilot)
        assert "not opened" in status_of(app)

    drive(FakeSource(), script)


def test_quit_does_not_wait_for_a_slow_read() -> None:
    source = gated_basic("snapshot")
    source.timeout = 5.0

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await wait_until(pilot, lambda: ("snapshot", PR2) in source.calls)
        await pilot.press("q")

    started = time.monotonic()
    app = BrowseApp(source, FakeOpener(), concurrency=0)

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await settle(app, pilot)
            await script(app, pilot)

    # A fresh loop with its default executor shut down at the end, as App.run does.
    asyncio.run(go())
    elapsed = time.monotonic() - started
    source.gate.set()
    assert elapsed < 3.0


def test_the_status_says_the_url_was_handed_to_the_opener() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("o")
        await settle(app, pilot)
        assert "launched fake-opener for https://github.com/acme/widgets/pull/2" in status_of(app)

    drive(basic_source(), script)


@pytest.mark.skipif(os.name != "posix", reason="a shell script stands in for the opener")
def test_an_opener_exiting_non_zero_is_noted_and_the_intent_stays_open(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("xdg-open", "open"):
        prog = bindir / name
        prog.write_text("#!/bin/sh\nexit 4\n")
        prog.chmod(prog.stat().st_mode | stat.S_IXUSR)
    opener = BrowserOpener({"PATH": str(bindir), "HOME": os.environ["HOME"]})

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("o")
        await wait_until(pilot, lambda: "opener exited 4" in status_of(app))

    app = drive(basic_source(), script, opener=opener)
    assert app.result()["intent"] == "open"


def test_the_command_palette_is_disabled() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert app.is_running

    drive(basic_source(), script)


def test_r_refreshes_the_pr_in_the_visible_pane_not_the_highlighted_row() -> None:
    source = basic_source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")  # PR1
        await settle(app, pilot)
        app.query_one("#list", OptionList).highlighted = 0  # PR2 highlighted, PR1 shown
        await pilot.press("r")
        await settle(app, pilot)

    drive(source, script)
    assert [c for c in source.calls if c[0] == "snapshot"] == [("snapshot", PR1)] * 2


def test_r_with_the_pane_closed_refreshes_only_the_row() -> None:
    source = basic_source()
    seen: dict[str, Any] = {}

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down")
        await settle(app, pilot)
        olist = app.query_one("#list", OptionList)
        seen["highlighted"] = olist.highlighted
        seen["mark"] = len(source.calls)
        await pilot.press("r")
        await settle(app, pilot)
        seen["after"] = list(source.calls[seen["mark"] :])
        seen["shown"] = app.query_one("#detail").display
        seen["focus"] = app.focused.id if app.focused else None
        seen["highlighted_after"] = olist.highlighted

    drive(source, script, concurrency=2)
    assert len(seen["after"]) == 1
    assert seen["after"][0][0] == "snapshot"
    assert seen["shown"] is False
    assert seen["focus"] == "list"
    assert seen["highlighted_after"] == seen["highlighted"]


def test_r_with_the_pane_open_reloads_the_row_and_the_detail() -> None:
    source = basic_source()
    seen: dict[str, Any] = {}

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await settle(app, pilot)
        seen["mark"] = len(source.calls)
        await pilot.press("r")
        await settle(app, pilot)
        seen["after"] = list(source.calls[seen["mark"] :])
        seen["shown"] = app.query_one("#detail").display

    drive(source, script, concurrency=2)
    assert seen["after"] == [("snapshot", PR1), ("diff", PR1)]
    assert seen["shown"] is True


def test_r_after_escape_refreshes_the_highlighted_row_and_keeps_the_pane_closed() -> None:
    source = basic_source()
    seen: dict[str, Any] = {}

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")  # PR1
        await settle(app, pilot)
        await pilot.press("escape", "up")  # pane hidden, PR2 highlighted
        seen["mark"] = len(source.calls)
        await pilot.press("r")
        await settle(app, pilot)
        seen["after"] = list(source.calls[seen["mark"] :])
        seen["shown"] = app.query_one("#detail").display

    drive(source, script, concurrency=2)
    assert seen["after"] == [("snapshot", PR2)]
    assert seen["shown"] is False


def test_r_with_an_empty_list_does_nothing_and_says_so() -> None:
    source = FakeSource()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("r")
        await settle(app, pilot)
        assert "nothing to refresh" in status_of(app)
        assert app.query_one("#detail").display is False

    drive(source, script, concurrency=2)
    assert [c[0] for c in source.calls] == ["prs"] * 2


class _FailingRefresh(FakeSource):
    """Snapshots answer until ``fail`` is set, then fail."""

    fail = False

    def snapshot(self, pr: str) -> dict[str, Any]:
        if self.fail:
            self.calls.append(("snapshot", pr))
            raise ForgectlError("upstream_error", "down")
        return super().snapshot(pr)


def test_a_failed_row_refresh_marks_the_row_stale_and_does_not_open_the_pane() -> None:
    base = basic_source()
    source = _FailingRefresh(
        authored=base.authored,
        requested=base.requested,
        snapshots=base.snapshots,
        diffs=base.diffs,
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down")
        await settle(app, pilot)
        source.fail = True
        await pilot.press("r")
        await settle(app, pilot)
        assert any("[stale: upstream_error]" in p.plain for p in list_prompts(app))
        assert app.query_one("#detail").display is False
        assert app.focused is not None and app.focused.id == "list"

    drive(source, script, concurrency=2)


_SENTINEL_TIME = datetime(2000, 1, 2, 3, 4, 5)


def test_row_only_r_does_not_move_the_refreshed_time() -> None:
    source = basic_source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down")
        await settle(app, pilot)
        app._last_refresh = _SENTINEL_TIME
        await pilot.press("r")
        await settle(app, pilot)
        assert app._last_refresh == _SENTINEL_TIME

    drive(source, script, concurrency=2)


def test_a_failing_row_only_r_does_not_move_the_refreshed_time() -> None:
    base = basic_source()
    source = _FailingRefresh(
        authored=base.authored,
        requested=base.requested,
        snapshots=base.snapshots,
        diffs=base.diffs,
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down")
        await settle(app, pilot)
        source.fail = True
        app._last_refresh = _SENTINEL_TIME
        await pilot.press("r")
        await settle(app, pilot)
        assert app._last_refresh == _SENTINEL_TIME

    drive(source, script, concurrency=2)


def test_r_says_so_when_snapshots_are_off() -> None:
    source = basic_source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "r")
        await settle(app, pilot)
        assert "not refreshed" in status_of(app)

    drive(source, script, concurrency=0)


def test_r_says_so_when_the_row_is_not_listed() -> None:
    source = basic_source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down")
        await settle(app, pilot)
        app._unlisted = {PR1, PR2}
        await pilot.press("r")
        await settle(app, pilot)
        assert "not refreshed" in status_of(app)

    drive(source, script, concurrency=2)


def test_r_with_the_pane_closed_while_its_detail_loads_reads_the_pr_once() -> None:
    source = gated_basic("snapshot")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "enter")
        await wait_until(pilot, lambda: ("snapshot", PR1) in source.calls)
        await pilot.press("escape", "r")
        await pilot.pause()
        assert "already running" in status_of(app)
        source.gate.set()
        await settle(app, pilot)

    drive(source, script, concurrency=0)
    assert source.calls.count(("snapshot", PR1)) == 1


def test_r_during_a_running_refresh_does_not_start_another_read() -> None:
    source = gated_basic("snapshot")

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await wait_until(pilot, lambda: any(c[0] == "snapshot" for c in source.calls))
        await pilot.press("r")
        await pilot.pause()
        assert "already running" in status_of(app)
        source.gate.set()
        await settle(app, pilot)

    drive(source, script, wait=False, concurrency=1)
    snaps = [c for c in source.calls if c[0] == "snapshot"]
    assert sorted(snaps) == [("snapshot", PR1), ("snapshot", PR2)]


def test_R_without_a_pane_reloads_only_the_lists() -> None:
    source = basic_source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("R")
        await settle(app, pilot)

    drive(source, script)
    assert [c[0] for c in source.calls] == ["prs"] * 4


def test_R_keeps_the_highlighted_pr_when_rows_shift() -> None:
    source = basic_source()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down")  # PR1 at index 1
        source.requested.insert(0, make_item(3, title="Newest", updated_at="2026-11-03T12:00:00Z"))
        await pilot.press("R")
        await settle(app, pilot)
        olist = app.query_one("#list", OptionList)
        assert olist.option_count == 3
        assert olist.highlighted == 2
        assert PR1 in list_prompts(app)[2].plain

    drive(source, script)


def test_filtering_keeps_the_highlighted_pr() -> None:
    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("down", "slash", *"acme")
        await pilot.pause()
        olist = app.query_one("#list", OptionList)
        assert olist.option_count == 2
        assert olist.highlighted == 1

    drive(basic_source(), script)


def test_check_labels_stay_within_one_budget_and_unopened_urls_are_cut_at_500() -> None:
    hostile = "x:" + "\x1b" * 2046
    checks = [make_check(f"c{i}", "failing", hostile) for i in range(500)]
    snap = make_snapshot(1, checks=checks)
    started = time.monotonic()
    detail = render.detail_of(snap, None)
    assert time.monotonic() - started < 2.0
    shown, footer = detail.check_labels[:-1], detail.check_labels[-1]
    assert footer.strip().startswith("...") and footer.endswith("more not shown")
    assert str(500 - len(shown)) in footer
    assert len(detail.checks) == len(shown) < 500  # indexes still line up with the labels
    assert sum(len(label) for label in shown) <= render.CHECK_BUDGET

    plain = render.check_label(make_check("lint", "failing", "x:" + "a" * 3000))
    assert "a" * 498 + "<truncated>" in plain and "a" * 499 not in plain
    openable = "https://ci.example/" + "a" * 2000
    assert openable in render.check_label(make_check("ok", "passing", openable))


def test_few_checks_get_no_not_shown_entry() -> None:
    detail = render.detail_of(make_snapshot(1, checks=[make_check()]), None)
    assert len(detail.check_labels) == len(detail.checks) == 1


def test_the_opener_exit_note_is_dropped_when_the_status_line_is_gone() -> None:
    import threading

    errors: list[BaseException] = []

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await app.query_one("#status", Static).remove()
        await pilot.pause()

        def reaper() -> None:
            try:
                app._opener_exited(4)
            except BaseException as exc:  # noqa: BLE001 - the test records anything raised
                errors.append(exc)

        thread = threading.Thread(target=reaper)
        thread.start()
        while thread.is_alive():
            await pilot.pause(0.01)
        thread.join()

    app = drive(basic_source(), script)
    assert errors == []
    app._opener_exited(4)  # after the app exited: nothing raised either
