"""Colors for the browse detail pane (#102): markdown, diffs, code and status words."""

from __future__ import annotations

import time

import pytest
from textual.content import Content
from textual.pilot import Pilot
from textual.widgets import OptionList

from forgectl.tui import render, safe, style
from forgectl.tui.app import BrowseApp

from .test_tui_app import PR1, as_content, content_of, drive, settle
from .tui_fakes import (
    FakeSource,
    make_check,
    make_comment,
    make_diff,
    make_item,
    make_snapshot,
    safe_style,
)

HOSTILE = [
    "[red]x[/red] [bold]y[/]",
    "[link=https://evil.example]click[/link]",
    "[" + "@click=app.quit]go[/]",
    "\x1b[31mred\x1b[0m \x1b]8;;https://evil.example\x1b\\link\x1b]8;;\x1b\\",
    "[x](https://evil.example/a?b=c) <https://auto.example> ![img](https://evil.example/i.png)",
    "<script>alert(1)</script> <b>raw</b> &amp;",
    "```[red]\n[bold]x[/]\n```",
    "# [red]h[/red]\n> [@click=app.quit]q[/]\n- [link=x]l[/link]\n",
]


def assert_inert(content: Content) -> None:
    for span in content.spans:
        assert safe_style(span.style), span.style
        assert 0 <= span.start <= span.end <= len(content.plain)
    assert "\x1b" not in content.plain


def has_style(content: Content, text: str, needle: str) -> bool:
    start = content.plain.index(text)
    return any(
        s.start <= start and s.end >= start + len(text) and needle in str(s.style)
        for s in content.spans
    )


@pytest.mark.parametrize("dark", [True, False])
@pytest.mark.parametrize("body", HOSTILE)
def test_hostile_markdown_is_inert_text(body: str, dark: bool) -> None:
    body = safe.text(body)  # the pane's input is always sanitized first
    content = style.markdown(body, dark)
    assert_inert(content)
    for literal in ("[red]", "[link=", "@click=", "<script>"):
        if literal in body and "```" not in body[: body.find(literal)]:
            assert literal in content.plain  # shown, not interpreted
    assert "evil.example" not in "".join(str(s.style) for s in content.spans)


def test_markup_in_markdown_text_is_literal_and_unstyled() -> None:
    content = style.markdown("[red]x[/red]", True)
    assert content.plain == "[red]x[/red]"
    assert content.spans == []


def test_a_link_shows_its_target_and_carries_no_link_style() -> None:
    content = style.markdown("see [the docs](https://example.com/a)", True)
    assert content.plain == "see the docs <https://example.com/a>"
    assert_inert(content)


def test_an_autolink_is_not_shown_twice() -> None:
    content = style.markdown("<https://example.com/a>", True)
    assert content.plain == "https://example.com/a"


def test_an_image_is_text_with_its_target() -> None:
    assert style.markdown("![logo](https://example.com/i.png)", True).plain == (
        "[image: logo] <https://example.com/i.png>"
    )


def test_markdown_styles_headings_emphasis_code_lists_and_quotes() -> None:
    content = style.markdown("# Head\n\n**b** *i* `c`\n\n- one\n- two\n\n> q\n", True)
    assert content.plain == "Head\n\nb i c\n\n• one\n• two\n\n│ q"
    assert has_style(content, "Head", "bold")
    assert has_style(content, "b", "bold")
    assert has_style(content, "i", "italic")
    assert has_style(content, "c", " on #")
    assert has_style(content, "•", "#")


def test_indent_applies_to_every_line() -> None:
    content = style.markdown("a\n\n- b\n\n```\nc\n```", True, indent=6)
    assert all(not x or x.startswith("      ") for x in content.plain.split("\n"))


def test_a_fenced_block_is_highlighted_by_its_language() -> None:
    for language, code in (("json", '{"a": 1}'), ("java", "int x = 1;"), ("python", "x = 'a'")):
        content = style.markdown(f"```{language}\n{code}\n```", True)
        assert content.plain == code
        colored = [s for s in content.spans if "#" in str(s.style) and " on " not in str(s.style)]
        assert len(colored) >= 2, language  # tokens, beyond the block's background


def test_an_unknown_or_missing_language_stays_plain() -> None:
    for fence in ("```\nx = 1\n```", "```nonesuch-lang\nx = 1\n```", "```[red]\nx = 1\n```"):
        content = style.markdown(fence, True)
        assert content.plain.endswith("x = 1")
        assert all(str(s.style).startswith("on ") for s in content.spans)


def test_an_unclosed_fence_runs_to_the_end() -> None:
    content = style.markdown("text\n\n```python\nx = 1\ny = 2", True)
    assert content.plain.endswith("x = 1\ny = 2")


def test_light_and_dark_use_different_palettes_and_both_are_readable() -> None:
    dark = style.markdown("```python\ndef f():\n    return 'x'  # c\n```", True)
    light = style.markdown("```python\ndef f():\n    return 'x'  # c\n```", False)
    assert {str(s.style) for s in dark.spans} != {str(s.style) for s in light.spans}
    for pal, background in ((style.DARK, "#121212"), (style.LIGHT, "#f4f4f4")):
        for color in (pal.ok, pal.bad, pal.warn, pal.merged, pal.quiet, pal.accent, pal.heading):
            assert contrast(color, background) >= 3.0, (pal.pygments, color)
        for token_style in token_styles(pal.pygments):
            assert contrast(token_style, background) >= 2.5, (pal.pygments, token_style)


def luminance(color: str) -> float:
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def token_styles(name: str) -> set[str]:
    from pygments.styles import get_style_by_name
    from pygments.token import Whitespace

    sheet = get_style_by_name(name)
    colors = set()
    for token, _ in sheet:
        if token in Whitespace:
            continue  # never styled: whitespace shows no color
        found = sheet.style_for_token(token)["color"]
        if found:
            colors.add("#" + found)
    return colors


DIFF = (
    "diff --git a/src/a.py b/src/a.py\n"
    "index 1..2 100644\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1,3 +1,3 @@ def f\n"
    " def f():\n"
    '-    return "old"\n'
    '+    return "new"\n'
    " # end\n"
    "diff --git a/notes.zzz b/notes.zzz\n"
    "--- a/notes.zzz\n"
    "+++ b/notes.zzz\n"
    "@@ -1 +1 @@\n"
    "-gone\n"
    "+here\n"
    "\\ No newline at end of file\n"
)


def test_a_diff_marks_files_hunks_signs_and_context_distinctly() -> None:
    content = style.diff(DIFF, True)
    assert content.plain == DIFF
    assert_inert(content)
    assert has_style(content, "diff --git a/src/a.py", "bold #79c0ff")
    assert has_style(content, "@@ -1,3 +1,3 @@ def f", "bold #d2a8ff")
    assert has_style(content, '-    return "old"', f"on {style.DARK.del_bg}")
    assert has_style(content, '+    return "new"', f"on {style.DARK.add_bg}")
    sign = content.plain.index('\n-    return "old"') + 1
    assert any(
        sp.start <= sign < sp.end and f"bold {style.DARK.bad}" == sp.style for sp in content.spans
    )
    assert has_style(content, " def f():", "dim")
    assert has_style(content, "\\ No newline", "italic")


def test_a_diff_is_highlighted_by_language_and_an_unknown_extension_is_plain() -> None:
    content = style.diff(DIFF, False)
    py = content.plain.index('"new"')
    zzz = content.plain.index("+here")
    token = [
        s
        for s in content.spans
        if s.start <= py < s.end and "#" in str(s.style) and "on" not in str(s.style)
    ]
    assert any(str(s.style).endswith("#ba2121") for s in token)  # a string token color
    plain_line = [s for s in content.spans if s.start <= zzz + 1 < s.end]
    assert {str(s.style) for s in plain_line} <= {
        f"on {style.LIGHT.add_bg}",
        f"{style.LIGHT.ok}",
    }


def test_a_triple_dash_content_line_inside_a_hunk_is_a_removal_not_a_header() -> None:
    text = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1 @@\n--- a/x.py\n-b\n"
    content = style.diff(text, True)
    assert content.plain == text
    start = text.rindex("--- a/x.py")
    assert any(
        s.start == start and f"on {style.DARK.del_bg}" in str(s.style) for s in content.spans
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "@@",
        "@@ -1 +1 @@",
        "@@ -a,b +c,d @@\n+x",
        "+++ \n--- \n@@ -1 +1 @@\n",
        "@@ -1,99999999 +1,99999999 @@\n" + "+x\n" * 50,
        "diff --git a/" + "a" * 5000 + " b/" + "b" * 5000 + "\n",
        "[red]x[/red]\n[link=x]y[/link]\n\x1b[31m\n",
    ],
)
def test_odd_diffs_never_raise_and_keep_their_text(text: str) -> None:
    text = safe.diff(text)
    content = style.diff(text, True)
    assert content.plain == text
    assert_inert(content)


def test_a_huge_diff_is_fast_and_falls_back_to_plain_coloring() -> None:
    line = "+x = 1\n"
    text = "--- a/x.py\n+++ b/x.py\n@@ -1,0 +1,50000 @@\n" + line * 14_000
    started = time.monotonic()
    content = style.diff(text, True)
    assert time.monotonic() - started < 2.0
    assert content.plain == text
    assert has_style(content, "+x = 1", f"on {style.DARK.add_bg}")  # still tinted
    tail = [s for s in content.spans if s.start >= len(text) - 7]  # the last line: past the budget
    assert {str(s.style) for s in tail} == {
        f"on {style.DARK.add_bg}",
        f"bold {style.DARK.ok}",
        style.DARK.ok,
    }


def test_the_highlight_budget_is_spent_once_per_paint() -> None:
    effort = style.Effort(chars=10, lines=100)
    assert effort.allows(8, 1)
    assert not effort.allows(8, 1)
    assert not style.Effort(seconds=-1).allows(1, 1)


def test_a_long_line_is_not_lexed() -> None:
    long = "x = '" + "a" * 600 + "'"
    content = style.markdown(f"```python\n{long}\n```", True)
    assert all(str(s.style).startswith("on ") for s in content.spans)


@pytest.mark.parametrize(
    "body",
    [
        "*a" * 10_000,
        "> " * 5_000 + "x",
        "[" * 5_000 + "x" + "](" * 2_000,
        "`" * 9_999,
        "- " * 8_000,
    ],
)
def test_pathological_markdown_finishes_quickly_and_stays_inert(body: str) -> None:
    started = time.monotonic()
    content = style.markdown(body, True)
    assert time.monotonic() - started < 3.0
    assert_inert(content)


def test_status_words_are_colored_consistently() -> None:
    for dark in (True, False):
        pal = style.palette(dark)
        assert pal.ok in str(style.status_style("passing", dark))
        assert style.status_style("passing", dark) == style.status_style("approved", dark)
        assert pal.bad in str(style.status_style("failing", dark))
        assert pal.warn in str(style.status_style("pending", dark))
        assert style.status_style("[red]", dark) is None
        assert style.status_style("failing  x", dark) is None


def test_check_labels_color_only_a_known_state_word() -> None:
    assert has_style(
        style.check_label("failing  build  https://x.example", True), "failing", style.DARK.bad
    )
    assert style.check_label("[red]  build", True).spans == []
    assert style.check_label("  ... 3 more not shown", True).spans == []


def test_facts_color_states_and_the_title_is_bold() -> None:
    snap = make_snapshot(1, title="[red]t[/red]")
    content = style.facts(render.facts_text(snap), True)
    assert_inert(content)
    assert has_style(content, "[red]t[/red]", "bold")
    assert content.plain.startswith("[red]t[/red]")


def test_comment_and_review_bodies_are_markdown_in_the_detail() -> None:
    snap = make_snapshot(1, comments=[make_comment("**loud** `x`")])
    detail = render.detail_of(snap, None)
    assert "loud" in detail.texts["comments"]
    content = style.body("comments", detail.texts, detail.lines, True)
    assert "**" not in content.plain and "loud" in content.plain
    assert has_style(content, "loud", "bold")
    assert content.plain.split("\n")[-1].startswith("      loud")


def test_a_section_text_equals_its_lines_joined() -> None:
    snap = make_snapshot(1, body="hello **x**", comments=[make_comment("c")])
    detail = render.detail_of(snap, None)
    for name, lines in detail.lines.items():
        assert "\n".join(lines) == detail.texts[name]


def test_the_pane_paints_styled_sections_and_repaints_for_a_theme() -> None:
    hostile = "[red]x[/red] [link=https://evil.example]y[/link] ```"
    source = FakeSource(
        authored=[make_item(1)],
        snapshots={
            PR1: make_snapshot(
                1,
                body='# Title\n\n```json\n{"a": 1}\n```\n' + hostile,
                checks=[make_check("build", "failing", None)],
                comments=[make_comment("**x** " + hostile)],
            )
        },
        diffs={PR1: make_diff(PR1, DIFF)},
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("3")
        await settle(app, pilot)
        for name in ("facts", "description", "changes", "ci", "comments", "reviews"):
            assert_inert(content_of(app, f"#{name}"))
        label = as_content(app.query_one("#checks", OptionList).get_option_at_index(0).prompt)
        assert_inert(label)
        assert has_style(label, "failing", "bold")
        assert has_style(content_of(app, "#changes"), '+    return "new"', style.DARK.add_bg)
        app.theme = "textual-light"
        await settle(app, pilot)
        assert has_style(content_of(app, "#changes"), '+    return "new"', style.LIGHT.add_bg)
        assert app.is_running

    drive(source, script)
