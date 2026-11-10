"""Colors for the browse detail pane (#102): markdown, diffs, code and status words."""

from __future__ import annotations

import time

import pytest
from pygments.token import Number
from textual.content import Content
from textual.pilot import Pilot
from textual.widgets import OptionList

from forgectl.tui import render, safe, style
from forgectl.tui.app import BrowseApp

from .test_tui_app import PR1, PR2, as_content, content_of, drive, settle
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


# -- round 1 review fixes ---------------------------------------------------------------------


DIFF_STYLES = {
    f"on {style.DARK.add_bg}",
    f"on {style.DARK.del_bg}",
    f"bold {style.DARK.ok}",
    f"bold {style.DARK.bad}",
    style.DARK.ok,
    style.DARK.bad,
    style.DARK.quiet,
    "dim",
}


def lexed(content: Content, start: int) -> bool:
    """Whether a Pygments token style (not a diff style) is on the content after ``start``."""
    return any(s.start >= start and str(s.style) not in DIFF_STYLES for s in content.spans)


def only_background(content: Content) -> bool:
    return all(str(s.style).startswith("on ") for s in content.spans)


@pytest.mark.parametrize(
    ("lang", "opener"),
    [("perl", "s{"), ("html", "<!--"), ("lua", "[=["), ("cpp", 'R"('), ("rust", 'r#"')],
)
def test_a_hostile_fence_over_the_lex_cap_is_plain_and_fast(lang: str, opener: str) -> None:
    code = "\n".join([opener * 3] * 150)  # over the line cap
    started = time.monotonic()
    content = style.markdown(f"```{lang}\n{code}\n```", True)
    assert time.monotonic() - started < 1.5
    assert only_background(content)
    big = "x = 1  # " + "a" * 100 + "\n"
    content = style.markdown(f"```python\n{big * 60}```", True)  # over the character cap
    assert only_background(content)


def test_a_small_fence_under_the_caps_is_still_highlighted() -> None:
    assert not only_background(style.markdown("```python\ndef f():\n    return 1\n```", True))


def test_a_hostile_diff_is_fast_and_the_oversize_hunk_stays_plain() -> None:
    body = "".join(f"+s{{{i}\n" for i in range(150))
    text = f"--- a/x.perl\n+++ b/x.perl\n@@ -0,0 +1,150 @@\n{body}"
    started = time.monotonic()
    content = style.diff(text, True)
    assert time.monotonic() - started < 1.5
    assert content.plain == text
    assert style.DARK.pygments not in str(content.spans)
    assert all(
        str(s.style) in {f"on {style.DARK.add_bg}", f"bold {style.DARK.ok}", style.DARK.ok}
        for s in content.spans
        if s.start > text.index("@@ -0,0") + 20
    )


def test_one_lex_is_capped_in_characters_and_lines() -> None:
    lexer = style.lexer_for_language("python")
    assert lexer is not None
    assert style.highlight_lines("x\n" * 101, lexer, style.DARK) is None
    assert style.highlight_lines("x" * (style.MAX_LEX_CHARS + 1), lexer, style.DARK) is None
    assert style.highlight_lines("x = 1\n" * 99 + "x = 1", lexer, style.DARK) is not None
    effort = style.Effort()
    assert not effort.allows(style.MAX_LEX_CHARS + 1, 1)
    assert not effort.allows(1, style.MAX_LEX_LINES + 1)
    assert effort.chars == style.HIGHLIGHT_CHARS  # a refused block spends nothing


def test_many_hostile_comments_fall_back_to_plain_text_within_the_budget() -> None:
    hostile = ("![[***[" * 700)[:4000]
    entries = [style.Markdown(hostile, 4) for _ in range(45)]
    started = time.monotonic()
    content = style.threaded(entries, True)  # type: ignore[arg-type]
    assert time.monotonic() - started < 1.5
    assert_inert(content)
    assert content.plain.split("\n")[-1] == "    " + hostile  # past the budget: shown as is
    assert content.plain.split("\n")[0] != "    " + hostile  # the first ones were rendered


def test_the_markdown_budget_is_spent_once_per_paint() -> None:
    effort = style.Effort(markdown_chars=10)
    assert effort.allows_markdown(8)
    assert not effort.allows_markdown(8)
    assert not style.Effort(markdown_seconds=-1).allows_markdown(1)
    plain = style.markdown("**x**", True, 2, style.Effort(markdown_chars=1))
    assert plain.plain == "  **x**" and plain.spans == []


@pytest.mark.parametrize(
    "body",
    [
        "> " * 25 + "a",
        "- " * 10 + "x",
        "- " * 10 + "x\n\nafter the deep item",
        "".join("  " * i + "- n\n" for i in range(12)) + "\nend",
        "> " * 30 + "- " * 10 + "text",
    ],
)
def test_content_past_the_parsers_nesting_limit_is_kept_as_text(body: str) -> None:
    content = style.markdown(body, True, 2)
    assert content.plain == "\n".join("  " + x for x in body.split("\n"))
    assert_inert(content)


@pytest.mark.parametrize(
    "entity",
    [
        "&#x202E;",
        "&#x200B;",
        "&#x2028;",
        "&NewLine;",
        "&Tab;",
        "&ZeroWidthSpace;",
        "&lrm;",
        "&shy;",
        "&NoBreak;",
    ],
)
def test_an_entity_cannot_bring_back_an_unsafe_character(entity: str) -> None:
    body = (
        f"a {entity} b\n\n[x{entity}y](http://e.example/p{entity}q)\n\n| h |\n|---|\n| c{entity}d |"
    )
    for dark in (True, False):
        content = style.markdown(body, dark)
        assert "<U+" in content.plain
        for ch in content.plain:
            if ch != "\n":
                assert ch.isprintable() or ch == " ", repr(ch)
        assert_inert(content)
        table_line = next(x for x in content.plain.split("\n") if x.startswith("c"))
        assert table_line.startswith("c<U+") and table_line.endswith("d")


def line_spans(content: Content, n: int) -> list[str]:
    rows = content.plain.split("\n")
    start = sum(len(r) + 1 for r in rows[:n])
    return [str(s.style) for s in content.spans if start <= s.start < start + len(rows[n])]


def test_status_words_are_colored_only_on_the_real_fact_lines() -> None:
    snap = make_snapshot(
        1,
        title="review: approved",
        branch="x review: approved",
        extra={"labels": ["review: approved", "state: merged", "mergeable: true"]},
    )
    snap["review_decision"] = "changes_requested"
    content = style.facts(render.facts_text(snap), True)
    rows = content.plain.split("\n")
    assert rows[3].startswith("branch: x review: approved") and rows[7].startswith("labels:")
    assert line_spans(content, 3) == [style.DARK.quiet]  # only the label
    assert line_spans(content, 7) == [style.DARK.quiet]
    assert has_style(content, "changes_requested", style.DARK.bad)
    assert has_style(content, "open", style.DARK.ok)


def test_an_inflated_hunk_count_does_not_swallow_the_next_files_headers() -> None:
    text = (
        "--- a/x.py\n+++ b/x.py\n@@ -1,99 +1,99 @@\n+x = 1\n"
        "diff --git a/y.zzz b/y.zzz\n--- a/y.zzz\n+++ b/y.zzz\n@@ -1 +1 @@\n+def z(): pass\n"
    )
    content = style.diff(text, True)
    assert has_style(content, "diff --git a/y.zzz b/y.zzz", f"bold {style.DARK.file_head}")
    assert has_style(content, "+++ b/y.zzz", f"bold {style.DARK.file_head}")
    assert not lexed(content, content.plain.index("def z"))  # y.zzz has no lexer


def test_old_and_new_sides_are_lexed_apart() -> None:
    text = '--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = """old\n+x = 1\n'
    content = style.diff(text, True)
    one = content.plain.index("+x = 1") + 5
    assert any(
        s.start <= one < s.end and str(s.style) == style._token_style("monokai", Number.Integer)
        for s in content.spans
    )  # the 1 is a number, not the rest of the removed line's string


def test_crlf_diffs_are_highlighted() -> None:
    text = "--- a/x.py\r\n+++ b/x.py\r\n@@ -1 +1 @@\r\n-x = 1\r\n+def f(): return 2\r\n"
    content = style.diff(text, True)
    assert content.plain == text.replace("\r\n", "\n")
    assert lexed(content, content.plain.index("+def"))
    assert has_style(content, "+def f(): return 2", f"on {style.DARK.add_bg}")


def test_a_quoted_git_path_selects_its_lexer() -> None:
    text = (
        'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
        '--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -1 +1 @@\n+def f(): return 2\n'
    )
    content = style.diff(text, True)
    assert lexed(content, text.index("+def"))
    assert style._unquote('"a\\tb\\"c\\\\d"') == 'a\tb"c\\d'
    assert style._unquote("plain.py") == "plain.py"


def test_split_is_linear_and_keeps_span_order() -> None:
    body = "\n".join("*a* `b`" for _ in range(10_000))[:20_000]
    started = time.monotonic()
    style.markdown(body, True)
    assert time.monotonic() - started < 0.6
    many = "\n".join(["a"] * 10_000)
    started = time.monotonic()
    out = style._split(many, [(2 * i, 2 * i + 1, "b") for i in range(10_000)])
    assert (
        time.monotonic() - started < 0.2 and len(out) == 10_000 and out[9_999][1] == [(0, 1, "b")]
    )
    pieces = style._split("ab\ncd", [(0, 5, "outer"), (1, 2, "inner"), (3, 4, "x")])
    assert pieces == [
        ("ab", [(0, 2, "outer"), (1, 2, "inner")]),
        ("cd", [(0, 2, "outer"), (0, 1, "x")]),
    ]


def test_nested_lists_and_quotes_keep_every_marker_and_no_extra_blank() -> None:
    assert style.markdown("- - a\n  - b", True).plain == "• • a\n  • b"
    assert style.markdown("-\n- x", True).plain.split("\n")[0].strip() == "•"
    assert style.markdown("-\n- x", True).plain.split("\n")[1] == "• x"
    assert style.markdown("- > quote\n- b", True).plain == "• │ quote\n• b"
    assert style.markdown("> - a\n> - b", True).plain == "│ • a\n│ • b"


def test_theme_change_keeps_the_selected_check() -> None:
    source = FakeSource(
        authored=[make_item(1)],
        snapshots={
            PR1: make_snapshot(1, checks=[make_check(f"c{i}", "passing", None) for i in range(4)])
        },
        diffs={PR1: make_diff(PR1, DIFF)},
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("3")
        await settle(app, pilot)
        checks = app.query_one("#checks", OptionList)
        checks.highlighted = 2
        app.theme = "textual-light"
        await settle(app, pilot)
        assert checks.highlighted == 2

    drive(source, script)


def test_opening_another_pull_request_starts_on_its_first_check() -> None:
    def checks() -> list:
        return [make_check(f"c{i}", "passing", None) for i in range(4)]

    source = FakeSource(
        authored=[make_item(1), make_item(2)],
        snapshots={PR1: make_snapshot(1, checks=checks()), PR2: make_snapshot(2, checks=checks())},
        diffs={PR1: make_diff(PR1, DIFF), PR2: make_diff(PR2, DIFF)},
    )

    async def script(app: BrowseApp, pilot: Pilot[None]) -> None:
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("3")
        await settle(app, pilot)
        first = app._detail_pr
        checks_list = app.query_one("#checks", OptionList)
        checks_list.highlighted = 2
        await pilot.press("escape", "down", "enter")
        await settle(app, pilot)
        assert app._detail_pr != first
        assert checks_list.highlighted == 0

    drive(source, script)


@pytest.mark.parametrize("language", ["mcschema", "tnt"])
def test_lexers_that_never_return_are_not_used(language: str) -> None:
    assert style.lexer_for_language(language) is None
    started = time.monotonic()
    content = style.markdown(f"```{language}\n<!--\n<\n```", True)
    assert time.monotonic() - started < 1.0
    assert only_background(content)


def test_the_worst_block_under_the_caps_lexes_in_well_under_a_second() -> None:
    code = "\n".join(["s{" * 15] * 100)[: style.MAX_LEX_CHARS]
    started = time.monotonic()
    style.markdown(f"```perl\n{code}\n```", True)
    assert time.monotonic() - started < 1.0
