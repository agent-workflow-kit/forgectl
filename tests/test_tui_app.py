"""Textual pilot tests for `browse`: behavior, keys, and hostile forge text."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from textual.content import Content
from textual.pilot import Pilot
from textual.widgets import OptionList, Static

from forgectl.errors import ForgectlError
from forgectl.tui.app import BrowseApp

from .cli_schema_check import validate
from .tui_fakes import (
    FakeOpener,
    FakeSource,
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
    opener: FakeOpener | None = None,
    focus: str | None = None,
) -> BrowseApp:
    app = BrowseApp(source, opener or FakeOpener(), focus=focus)

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await settle(app, pilot)
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
        facts = content_of(app, "#facts").plain
        assert "Mine" in facts and "the body" in facts and "CI: failing" in facts
        checks = app.query_one("#checks", OptionList)
        assert "build" in as_content(checks.get_option_at_index(0).prompt).plain
        rest = content_of(app, "#rest").plain
        assert "please rename" in rest and "+new line" in rest and "Reviews" in rest

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
    assert [c for c in source.calls if c[0] == "snapshot"] == [("snapshot", PR1)] * 2
    assert len([c for c in source.calls if c[0] == "prs"]) == 4


def test_there_is_no_write_action() -> None:
    source = basic_source()
    opener = FakeOpener()

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        for key in ("m", "p", "c", "a", "d", "x", "M"):
            await pilot.press(key)
        await settle(app, pilot)

    app = drive(source, script, opener=opener)
    bound = {b.key if hasattr(b, "key") else b[0] for b in BrowseApp.BINDINGS}  # type: ignore[index]
    assert bound == {"q", "question_mark", "slash", "escape", "o", "r", "R"}
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
    "[@click=app.quit]click me[/]",
    "[bold red]loud[/bold red] [link=https://evil.example]x[/link]",
    "[on red]x[/] [@click=app.bell]y[/] [/]",
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
            content_of(app, "#facts"),
            content_of(app, "#rest"),
            as_content(app.query_one("#checks", OptionList).get_option_at_index(0).prompt),
        ]
        for content in contents:
            assert content.spans == []  # no styles, no @click actions
            for ch in content.plain:
                assert ch in "\t\n" or ord(ch) >= 0x20 and not 0x7F <= ord(ch) < 0xA0
            assert "‮" not in content.plain and "\x1b" not in content.plain
        screen = app.export_screenshot()
        assert "\x1b" not in screen
        assert app.is_running  # nothing in the text ran an action such as app.quit

    app = drive(source, script)
    assert app.result()["intent"] == "back"


def test_the_markup_title_is_shown_literally() -> None:
    title = "[@click=app.quit]click me[/]"
    source = FakeSource(authored=[make_item(1, title=title)])

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        assert title in list_prompts(app)[0].plain
        await pilot.click("#list")  # a click on the text runs nothing
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
        assert len(content_of(app, "#rest").plain) < 2_000_000

    drive(source, script)


def test_errors_from_the_source_reach_the_status_line_inert() -> None:
    class Failing(FakeSource):
        def prs(self, **kwargs: Any) -> dict[str, Any]:
            raise ForgectlError("upstream_error", "[@click=app.quit]boom\x1b[31m")

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
        assert "gitlab.com" in status and "auth_required" in status
        assert len(list_prompts(app)) == 2  # the rest is still shown

    drive(source, script)


def test_a_failing_detail_read_keeps_the_list_usable() -> None:
    source = basic_source()
    source.snapshots.pop(PR2)

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")  # PR2 has no snapshot: the fake raises KeyError
        await settle(app, pilot)
        assert "failed" in content_of(app, "#status").plain
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
