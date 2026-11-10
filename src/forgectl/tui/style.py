"""Colors for the browse detail pane: markdown, diffs, code and status words.

Everything here takes text that ``safe`` has already sanitized and returns ``Content`` whose
text is that same text (plus a few fixed decoration characters) and whose spans carry only
styles built from this file's constants and Pygments' hex colors. Forge text is never parsed
as Rich or Textual markup, is never a style, and no span carries a link or an action: link
targets are shown as ``text <target>``. Markdown is parsed by ``markdown-it-py`` (the parser
Rich's own Markdown uses) with raw HTML off, and its tokens are mapped to styles here.

Highlighting is bounded: one lex is at most ``MAX_LEX_CHARS`` characters and ``MAX_LEX_LINES``
lines (a bigger block stays plain, because a Pygments lexer can take seconds on a few thousand
hostile characters; two lexers that never return on short input are not used at all), a
paint has a character, line and time budget on top, and code with a very long line stays
plain. Markdown rendering has its own per-paint character and time budget; a
body past it is shown as plain sanitized text. Text a parser decodes (entities) is sanitized
again, so no entity can bring back a control, bidi or invisible character.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token
from pygments.lexer import Lexer
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.style import Style as PygmentsStyle
from pygments.styles import get_style_by_name
from pygments.token import Whitespace, _TokenType
from pygments.util import ClassNotFound
from textual.content import Content, Span

from . import safe

# -- bounds -------------------------------------------------------------------------------

HIGHLIGHT_CHARS = 60_000  # of code lexed per paint
HIGHLIGHT_LINES = 2_500  # of diff lines highlighted per paint
HIGHLIGHT_SECONDS = 0.4  # no new lexing starts after this long
MAX_LEX_CHARS = 3_000  # one lex: a bigger block (or diff side) stays plain
MAX_LEX_LINES = 100  # one lex: more lines than this stay plain
MARKDOWN_CHARS = 30_000  # of markdown body rendered per paint
MARKDOWN_SECONDS = 0.5  # no new markdown body is rendered after this long
# Lexers that never return on some short inputs (found by scanning every Pygments lexer with
# repeated openers; MCSchema loops on any ``<!--``, Typographic Number Theory on a final ``<``).
NEVER_LEXED = frozenset({"MCSchema", "Typographic Number Theory"})
LONG_LINE = 400  # code with a longer line is not lexed
HREF_LIMIT = 300  # shown link targets are cut here
LANG_PATTERN = re.compile(r"[A-Za-z0-9_+#.\-]{1,32}")
HEX_PATTERN = re.compile(r"[0-9a-fA-F]{6}")


@dataclass(frozen=True)
class Palette:
    pygments: str  # the Pygments style the code tokens use
    ok: str
    bad: str
    warn: str
    merged: str
    quiet: str
    accent: str
    heading: str
    code_bg: str
    add_bg: str
    del_bg: str
    file_head: str
    hunk_head: str


DARK = Palette(
    pygments="monokai",
    ok="#3fb950",
    bad="#f85149",
    warn="#d29922",
    merged="#bc8cff",
    quiet="#8b949e",
    accent="#58a6ff",
    heading="#79c0ff",
    code_bg="#262c36",
    add_bg="#12361d",
    del_bg="#3d1a1d",
    file_head="#79c0ff",
    hunk_head="#d2a8ff",
)
LIGHT = Palette(
    pygments="default",
    ok="#1a7f37",
    bad="#cf222e",
    warn="#9a6700",
    merged="#8250df",
    quiet="#57606a",
    accent="#0550ae",
    heading="#0550ae",
    code_bg="#e6e9ee",
    add_bg="#ccf0d3",
    del_bg="#fbd5d3",
    file_head="#0550ae",
    hunk_head="#6639ba",
)


def palette(dark: bool) -> Palette:
    return DARK if dark else LIGHT


@dataclass
class Effort:
    """What one paint may spend on syntax highlighting."""

    chars: int = HIGHLIGHT_CHARS
    lines: int = HIGHLIGHT_LINES
    seconds: float = HIGHLIGHT_SECONDS
    markdown_chars: int = MARKDOWN_CHARS
    markdown_seconds: float = MARKDOWN_SECONDS
    started: float = field(default_factory=time.monotonic)

    def allows(self, chars: int, lines: int) -> bool:
        """Whether one lex of this size may run now (and if so, spends its share)."""
        if chars > MAX_LEX_CHARS or lines > MAX_LEX_LINES:
            return False
        if time.monotonic() - self.started > self.seconds:
            return False
        if chars > self.chars or lines > self.lines:
            return False
        self.chars -= chars
        self.lines -= lines
        return True

    def allows_markdown(self, chars: int) -> bool:
        """Whether a markdown body of this size may be rendered now (spends its share)."""
        if time.monotonic() - self.started > self.markdown_seconds or chars > self.markdown_chars:
            return False
        self.markdown_chars -= chars
        return True


# -- the content builder --------------------------------------------------------------------

_Spans = list[tuple[int, int, str]]  # (start, end, style) within one line


class _Doc:
    """Lines and their spans, joined into one ``Content`` without ever parsing markup."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.spans: list[Span] = []
        self.pos = 0
        self.count = 0

    def line(self, text: str, spans: Iterable[tuple[int, int, str]] = ()) -> None:
        if self.count:
            self.parts.append("\n")
            self.pos += 1
        base = self.pos
        self.parts.append(text)
        self.pos += len(text)
        self.spans.extend(Span(base + a, base + b, s) for a, b, s in spans if a < b)
        self.count += 1

    def add(self, content: Content) -> None:
        """Append already-built content as the next line(s)."""
        if self.count:
            self.parts.append("\n")
            self.pos += 1
        base = self.pos
        self.parts.append(content.plain)
        self.pos += len(content.plain)
        self.spans.extend(Span(base + sp.start, base + sp.end, sp.style) for sp in content.spans)
        self.count += 1

    def content(self) -> Content:
        return Content("".join(self.parts), spans=self.spans)


# -- Pygments -------------------------------------------------------------------------------

_style_cache: dict[str, type[PygmentsStyle]] = {}
_token_cache: dict[tuple[str, _TokenType], str | None] = {}


def _token_style(name: str, token: _TokenType) -> str | None:
    key = (name, token)
    if key not in _token_cache:
        style = _style_cache.get(name)
        if style is None:
            style = _style_cache[name] = get_style_by_name(name)
        found: Mapping[str, Any] = {} if token in Whitespace else style.style_for_token(token)
        parts: list[str] = []
        if found.get("bold"):
            parts.append("bold")
        if found.get("italic"):
            parts.append("italic")
        color = found.get("color")
        if isinstance(color, str) and HEX_PATTERN.fullmatch(color):
            parts.append("#" + color.lower())
        _token_cache[key] = " ".join(parts) or None
    return _token_cache[key]


def lexer_for_language(language: str) -> Lexer | None:
    """A lexer for a fence's language word, or None (no word, odd word, unknown language)."""
    match = LANG_PATTERN.match(language.strip())
    if not match:
        return None
    try:
        lexer = get_lexer_by_name(
            match.group(0), stripnl=False, ensurenl=False, stripall=False, tabsize=0
        )
    except ClassNotFound:
        return None
    return None if lexer.name in NEVER_LEXED else lexer


def lexer_for_file(name: str) -> Lexer | None:
    """A lexer chosen from a file name or extension, or None."""
    if not name or len(name) > 300:
        return None
    try:
        lexer = get_lexer_for_filename(
            name, stripnl=False, ensurenl=False, stripall=False, tabsize=0
        )
    except ClassNotFound:
        return None
    return None if lexer.name in NEVER_LEXED else lexer


def highlight_lines(code: str, lexer: Lexer, pal: Palette) -> list[_Spans] | None:
    """Per-line spans for ``code``, or None when it cannot be lexed faithfully."""
    if len(code) > MAX_LEX_CHARS or code.count("\n") >= MAX_LEX_LINES:
        return None
    if any(len(x) > LONG_LINE for x in code.split("\n")):
        return None
    out: list[_Spans] = [[]]
    col = 0
    seen = 0
    try:
        for token, value in lexer.get_tokens(code):
            seen += len(value)
            style = _token_style(pal.pygments, token)
            for i, piece in enumerate(value.split("\n")):
                if i:
                    out.append([])
                    col = 0
                if piece and style:
                    out[-1].append((col, col + len(piece), style))
                col += len(piece)
    except Exception:  # noqa: BLE001 - a lexer that fails leaves the code plain
        return None
    return out if seen == len(code) else None  # the tokens must be exactly the text


# -- markdown -------------------------------------------------------------------------------

_MD = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable(["table", "strikethrough"])
# markdown-it silently drops blocks nested deeper than its ``maxNesting``; a body whose parse
# gets within one block of that limit is shown as plain text instead.
_DEEP = int(_MD.options["maxNesting"]) - 1


class _Markdown:
    def __init__(self, pal: Palette, indent: int, effort: Effort) -> None:
        self.pal = pal
        self.indent = indent
        self.effort = effort
        self.doc = _Doc()
        # The open quotes and list items, outermost first: ["q"] or ["i", width, pending marker].
        self.stack: list[list[Any]] = []
        self.lists: list[list[Any]] = []  # [ordered, next number]
        self.gap = False
        self.row: list[str] | None = None
        self.in_head = False

    # prefixes -------------------------------------------------------------------------------

    def _prefix(self, first: bool) -> tuple[str, _Spans]:
        """The line's lead in nesting order (quote bars and list markers); on the first line of
        a block every pending list marker is shown, else the items are blank padding."""
        text = " " * self.indent
        spans: _Spans = []
        for entry in self.stack:
            if entry[0] == "q":
                spans.append((len(text), len(text) + 1, self.pal.quiet))
                text += "│ "
            elif first and entry[2] is not None:
                spans.append((len(text), len(text) + len(entry[2]), self.pal.accent))
                text += entry[2]
                entry[2] = None
            else:
                text += " " * entry[1]
        return text, spans

    def _emit(self, text: str, spans: _Spans, first: bool = False) -> None:
        prefix, pspans = self._prefix(first)
        shift = len(prefix)
        self.doc.line(prefix + text, pspans + [(a + shift, b + shift, s) for a, b, s in spans])

    def _blank(self) -> None:
        text, spans = self._prefix(False)
        text = text.rstrip()
        self.doc.line(text, [sp for sp in spans if sp[1] <= len(text)])

    def _block(self, lines: list[tuple[str, _Spans]]) -> None:
        if self.gap:
            self._blank()
            self.gap = False
        for i, (text, spans) in enumerate(lines or [("", [])]):
            self._emit(text, spans, first=i == 0)

    # inline ---------------------------------------------------------------------------------

    def _inline(self, children: list[Token], base: str = "") -> list[tuple[str, _Spans]]:
        pal = self.pal
        text = ""
        spans: _Spans = []
        open_at: list[tuple[int, str]] = []
        hrefs: list[str] = []
        link_from: list[int] = []

        def push(style: str) -> None:
            open_at.append((len(text), style))

        def pop() -> None:
            start, style = open_at.pop()
            spans.append((start, len(text), style))

        if base:
            push(base)
        for tok in children:
            kind = tok.type
            if kind == "text" or kind == "html_inline":
                text += safe.inline(tok.content)
            elif kind in ("softbreak", "hardbreak"):
                text += "\n"
            elif kind == "code_inline":
                start = len(text)
                text += safe.inline(tok.content)
                spans.append((start, len(text), f"{pal.heading} on {pal.code_bg}"))
            elif kind == "em_open":
                push("italic")
            elif kind == "strong_open":
                push("bold")
            elif kind == "s_open":
                push("strike")
            elif kind in ("em_close", "strong_close", "s_close"):
                pop()
            elif kind == "link_open":
                hrefs.append(safe.inline(str(tok.attrGet("href") or "")))
                push(f"underline {pal.accent}")
                link_from.append(len(text))
            elif kind == "link_close":
                pop()
                href = hrefs.pop() if hrefs else ""
                label = text[link_from.pop() :] if link_from else ""
                shown = href if len(href) <= HREF_LIMIT else href[:HREF_LIMIT] + "<truncated>"
                if shown and label != href:  # an autolink already shows its target
                    start = len(text)
                    text += f" <{shown}>"
                    spans.append((start, len(text), pal.quiet))
            elif kind == "image":
                alt = safe.inline(tok.content) or "image"
                src = safe.inline(str(tok.attrGet("src") or ""))
                shown = src if len(src) <= HREF_LIMIT else src[:HREF_LIMIT] + "<truncated>"
                start = len(text)
                text += f"[image: {alt}]" + (f" <{shown}>" if shown else "")
                spans.append((start, len(text), pal.quiet))
            else:  # anything else a parser might add: its text, inert
                text += safe.inline(tok.content)
        while open_at:
            pop()
        return _split(text, spans)

    # blocks ---------------------------------------------------------------------------------

    def _code(self, code: str, lexer: Lexer | None) -> list[tuple[str, _Spans]]:
        code = safe.block(code)
        lines = code.rstrip("\n").split("\n")
        found: list[_Spans] | None = None
        if lexer is not None and self.effort.allows(len(code), len(lines)):
            found = highlight_lines("\n".join(lines), lexer, self.pal)
        bg = f"on {self.pal.code_bg}"
        out: list[tuple[str, _Spans]] = []
        for i, line in enumerate(lines):
            spans: _Spans = [(0, len(line), bg)]
            if found is not None and i < len(found):
                spans += found[i]
            out.append((line, spans))
        return out

    def run(self, body: str) -> Content:
        pal = self.pal
        tokens = _MD.parse(body)
        if any(tok.level >= _DEEP for tok in tokens):  # past the parser's nesting limit
            return _plain(body, self.indent)  # content may be dropped, so show the text
        heading = ""
        for tok in tokens:
            kind = tok.type
            if kind == "blockquote_open":
                if self.gap:
                    self._blank()
                    self.gap = False
                self.stack.append(["q"])
            elif kind == "blockquote_close":
                self.stack.pop()
                self.gap = not self.stack
            elif kind in ("bullet_list_open", "ordered_list_open"):
                start = tok.attrGet("start")
                self.lists.append([kind == "ordered_list_open", int(start) if start else 1])
            elif kind in ("bullet_list_close", "ordered_list_close"):
                self.lists.pop()
                self.gap = not self.lists
            elif kind == "list_item_open":
                ordered, number = self.lists[-1] if self.lists else (False, 1)
                marker = f"{number}. " if ordered else "• "
                if self.lists:
                    self.lists[-1][1] += 1
                self.stack.append(["i", len(marker), marker])
            elif kind == "list_item_close":
                if self.stack[-1][2] is not None:  # an item with no content still shows its marker
                    self._block([("", [])])
                self.stack.pop()
            elif kind == "heading_open":
                heading = f"bold {pal.heading}" + (" underline" if tok.tag == "h1" else "")
            elif kind == "heading_close":
                heading = ""
                self.gap = True
            elif kind == "paragraph_close":
                self.gap = not tok.hidden
            elif kind == "inline":
                if self.row is not None:
                    self.row.append(" ".join(t for t, _ in self._inline(tok.children or [])))
                else:
                    self._block(self._inline(tok.children or [], heading))
            elif kind in ("fence", "code_block"):
                info = tok.info.strip() if kind == "fence" else ""
                language = safe.inline(info).split(None, 1)[0] if info.strip() else ""
                lexer = lexer_for_language(language) if language else None
                self._block(self._code(tok.content, lexer))
                self.gap = True
            elif kind == "hr":
                self._block([("─" * 24, [(0, 24, pal.quiet)])])
                self.gap = True
            elif kind == "thead_open":
                self.in_head = True
            elif kind == "thead_close":
                self.in_head = False
            elif kind == "tr_open":
                self.row = []
            elif kind == "tr_close":
                cells, self.row = self.row or [], None
                line = " │ ".join(cells)
                self._block([(line, [(0, len(line), "bold")] if self.in_head else [])])
            elif kind == "table_close":
                self.gap = True
            elif kind in ("html_block",):  # html is off, so this does not occur; stay inert
                self._block(_split(safe.block(tok.content.rstrip("\n")), []))
                self.gap = True
        return self.doc.content()


def _split(text: str, spans: _Spans) -> list[tuple[str, _Spans]]:
    """Cut ``text`` at newlines, clipping and shifting the spans onto each piece.

    Linear in the text plus the spans (and the overlaps): spans are taken in order of start
    and the ones still open are carried from line to line, each line keeping the original
    span order.
    """
    order = sorted(range(len(spans)), key=lambda i: spans[i][0])
    out: list[tuple[str, _Spans]] = []
    active: list[int] = []
    nxt = 0
    start = 0
    for piece in text.split("\n"):
        end = start + len(piece)
        while nxt < len(order) and spans[order[nxt]][0] < end:
            active.append(order[nxt])
            nxt += 1
        active = [i for i in active if spans[i][1] > start]
        active.sort()
        line: _Spans = []
        for i in active:
            a, b, s = spans[i]
            lo, hi = max(a, start), min(b, end)
            if lo < hi:
                line.append((lo - start, hi - start, s))
        out.append((piece, line))
        start = end + 1
    return out


def _plain(body: str, indent: int) -> Content:
    """``body`` as it is, every line indented, no styles."""
    pad = " " * indent
    return Content("\n".join(pad + x for x in body.split("\n")))


def markdown(body: str, dark: bool, indent: int = 0, effort: Effort | None = None) -> Content:
    """Sanitized markdown ``body`` as styled content, every line indented by ``indent``."""
    if not body.strip():
        return Content(body)
    effort = effort or Effort()
    if not effort.allows_markdown(len(body)):
        return _plain(body, indent)
    return _Markdown(palette(dark), indent, effort).run(body)


# -- diffs ----------------------------------------------------------------------------------

_HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_GIT_HEAD = re.compile(r"diff --git a/(.+) b/(.+)")
_GIT_QUOTED = re.compile(r'diff --git .*? "b/((?:[^"\\]|\\.)*)"')
_ESCAPE = re.compile(r"\\([0-7]{3}|.)", re.DOTALL)
_C_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11}
HEADER_LIMIT = 1_000  # a longer header line names no file (and is not matched at all)


def _unquote(name: str) -> str:
    """A git C-quoted path (``"caf\\303\\251.py"``) as plain text; other names unchanged."""
    if len(name) < 2 or name[0] != '"' or name[-1] != '"':
        return name
    raw = bytearray()
    last = 1
    for m in _ESCAPE.finditer(name, 1, len(name) - 1):
        raw += name[last : m.start()].encode()
        code = m.group(1)
        raw.append(
            int(code, 8) & 255 if len(code) == 3 else _C_ESCAPES.get(code, ord(code[0])) & 255
        )
        last = m.end()
    raw += name[last : len(name) - 1].encode()
    return raw.decode("utf-8", errors="replace")


def _file_of(line: str) -> str | None:
    """The file name a ``+++``/``---``/``diff --git`` header line names, or None."""
    if len(line) > HEADER_LIMIT:
        return None
    if line.startswith("diff --git "):
        q = _GIT_QUOTED.fullmatch(line)
        if q:
            return _unquote('"b/' + q.group(1) + '"')[2:]
        m = _GIT_HEAD.fullmatch(line)
        return m.group(2) if m else None
    if line.startswith(("+++ ", "--- ")):
        name = _unquote(line[4:].split("\t", 1)[0])
        if name == "/dev/null":
            return None
        return name[2:] if name[:2] in ("a/", "b/") else name
    return None


def _hunk_line(doc: _Doc, line: str, spans: _Spans, pal: Palette) -> None:
    """One diff body line: sign, tint and the given token spans (columns of ``line[1:]``)."""
    sign = line[:1]
    n = len(line)
    out: _Spans = []
    if sign == "+":
        out.append((0, n, f"on {pal.add_bg}"))
        out.append((0, 1, f"bold {pal.ok}"))
    elif sign == "-":
        out.append((0, n, f"on {pal.del_bg}"))
        out.append((0, 1, f"bold {pal.bad}"))
    elif sign == "\\":
        out.append((0, n, f"italic {pal.quiet}"))
    else:
        out.append((0, n, "dim"))
    out += [(a + 1, b + 1, s) for a, b, s in spans]
    if not spans and sign == "+":
        out.append((1, n, pal.ok))
    elif not spans and sign == "-":
        out.append((1, n, pal.bad))
    doc.line(line, out)


def _hunk_end(lines: list[str], i: int, old: int, new: int) -> int:
    """The index after the hunk whose ``@@`` line is ``lines[i]``: its counts say how long it
    is, but a ``diff --git`` or ``@@`` line (never a body line) ends it early."""
    j = i + 1
    while j < len(lines) and (old > 0 or new > 0 or lines[j].startswith("\\")):
        row = lines[j]
        if row.startswith(("diff --git ", "@@ ")):
            break
        sign = row[:1]
        if sign == "+":
            new -= 1
        elif sign == "-":
            old -= 1
        elif sign != "\\":
            old -= 1
            new -= 1
        j += 1
    return j


def _side(rows: list[str], lexer: Lexer, effort: Effort, pal: Palette) -> list[_Spans] | None:
    """Spans of one side of a hunk (its rows without the sign), or None."""
    code = [x[1:] for x in rows]
    if not effort.allows(sum(map(len, code)), len(code)):
        return None
    return highlight_lines("\n".join(code), lexer, pal)


def diff(text: str, dark: bool, effort: Effort | None = None) -> Content:
    """A sanitized unified diff as styled content.

    File and hunk headers are styled; ``+``/``-`` lines get a foreground and a background
    tint; context is dimmed. Each hunk is lexed alone (by the file name of its section) while
    the effort budget lasts: the old side (context and ``-`` lines) and the new side (context
    and ``+`` lines) separately, so a removed line cannot color the added ones. A construct
    that began outside the hunk may be styled imperfectly, and past the budget (or the caps of
    one lex) the lines keep only the +/- coloring.
    """
    pal = palette(dark)
    effort = effort or Effort()
    doc = _Doc()
    # A CR before LF is dropped: Content drops it too, and the spans must line up.
    lines = [x[:-1] if x.endswith("\r") else x for x in text.split("\n")]
    name = ""
    lexer: Lexer | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _HUNK.match(line)
        if m:
            doc.line(line, [(0, len(line), f"bold {pal.hunk_head}")])
            old = int(m.group(1)) if m.group(1) is not None else 1
            new = int(m.group(2)) if m.group(2) is not None else 1
            j = _hunk_end(lines, i, old, new)
            body = lines[i + 1 : j]
            real = [x for x in body if x[:1] != "\\"]
            found_old: list[_Spans] | None = None
            found_new: list[_Spans] | None = None
            if lexer is not None and real:
                found_old = _side([x for x in real if x[:1] != "+"], lexer, effort, pal)
                found_new = _side([x for x in real if x[:1] != "-"], lexer, effort, pal)
            at_old = at_new = 0
            for row in body:
                sign = row[:1]
                if sign == "\\":
                    _hunk_line(doc, row, [], pal)
                    continue
                spans: _Spans = []
                if sign != "+":
                    if sign != "-" and found_new is not None and at_new < len(found_new):
                        spans = found_new[at_new]
                    elif found_old is not None and at_old < len(found_old):
                        spans = found_old[at_old]
                    at_old += 1
                if sign != "-":
                    if sign == "+" and found_new is not None and at_new < len(found_new):
                        spans = found_new[at_new]
                    at_new += 1
                _hunk_line(doc, row, spans, pal)
            i = j
            continue
        if line.startswith("diff --git ") or line.startswith(("+++ ", "--- ")):
            file = _file_of(line)
            if file is not None and (line.startswith(("diff --git ", "+++ ")) or not name):
                name, lexer = file, lexer_for_file(file)
            elif line.startswith("diff --git "):
                name, lexer = "", None
            doc.line(line, [(0, len(line), f"bold {pal.file_head}")])
        else:
            doc.line(line, [(0, len(line), pal.quiet)] if line else [])
        i += 1
    return doc.content()


# -- status words ---------------------------------------------------------------------------


def status_style(word: str, dark: bool) -> str | None:
    """The style of a status word (CI state, review state, PR state), or None if not one."""
    pal = palette(dark)
    ok = {"passing", "approved", "open", "true", "success", "resolved"}
    bad = {"failing", "changes_requested", "false", "closed", "failed", "error"}
    warn = {"pending", "review_required", "in_progress", "queued"}
    if word in ok:
        return f"bold {pal.ok}"
    if word in bad:
        return f"bold {pal.bad}"
    if word in warn:
        return f"bold {pal.warn}"
    if word == "merged":
        return f"bold {pal.merged}"
    if word in ("none", "unknown", "skipped", "null", "None"):
        return pal.quiet
    return None


def _word_spans(text: str, pattern: re.Pattern[str], dark: bool) -> _Spans:
    out: _Spans = []
    for m in pattern.finditer(text):
        style = status_style(m.group(1), dark)
        if style:
            out.append((m.start(1), m.end(1), style))
    return out


# The status words of the facts, found only on their own line (by position) and anchored to the
# real fields, so a label, branch or title that spells "review: approved" is never colored.
_STATE_LINE = re.compile(r"^state: (\S+)(?:  draft)?   author: ")
_REVIEW_LINE_FACT = re.compile(r"^review: (\S+)   approvals: ")
_MERGEABLE = re.compile(r"   mergeable: (\S+)$")
_LABEL = re.compile(r"^(state|branch|review|blocked by|size|labels|updated):")
_SIZE = re.compile(r"[ ]([+-])\d+")
_CI_WORD = re.compile(r"^CI: (\S+)")
_REVIEW_LINE = re.compile(r"^  (?:review )?(\S+?)(?:  @| by )")
_THREAD = re.compile(r"^  thread\b.*\((open|resolved)\)$")
_AUTHOR = re.compile(r"^    (@.*):$")


def facts(text: str, dark: bool) -> Content:
    pal = palette(dark)
    doc = _Doc()
    for n, line in enumerate(text.split("\n")):
        spans: _Spans = []
        if n == 0:
            spans.append((0, len(line), "bold"))
        else:
            m = _LABEL.match(line)
            if m:
                spans.append((0, m.end(), pal.quiet))
            for at, pattern in ((2, _STATE_LINE), (4, _REVIEW_LINE_FACT), (4, _MERGEABLE)):
                spans += _word_spans(line, pattern, dark) if n == at else []
            if line.startswith("blocked by: ") and not line.endswith("nothing known"):
                spans.append((len("blocked by: "), len(line), f"bold {pal.bad}"))
            if line.startswith("size: "):
                for sm in _SIZE.finditer(line):
                    spans.append((sm.start(1), sm.end(), pal.ok if sm.group(1) == "+" else pal.bad))
        doc.line(line, spans)
    return doc.content()


def ci(text: str, dark: bool) -> Content:
    doc = _Doc()
    for line in text.split("\n"):
        doc.line(line, _word_spans(line, _CI_WORD, dark))
    return doc.content()


def check_label(label: str, dark: bool) -> Content:
    """A check's label with its state word colored (the word before the first double space)."""
    state = label.split("  ", 1)[0]
    style = status_style(state, dark)
    return Content(label, spans=[Span(0, len(state), style)] if style and state else [])


def threaded(lines: list[str], dark: bool, effort: Effort | None = None) -> Content:
    """Review or comment lines; ``Markdown`` entries are rendered as markdown."""
    pal = palette(dark)
    effort = effort or Effort()
    doc = _Doc()
    for entry in lines:
        if isinstance(entry, Markdown):
            if entry.body.strip() and effort.allows_markdown(len(entry.body)):
                doc.add(_Markdown(pal, entry.indent, effort).run(entry.body))
            else:  # blank, or past the budget: the sanitized text, indented, unstyled
                for row in entry.split("\n"):
                    doc.line(row)
            continue
        for row in entry.split("\n"):
            spans: _Spans = []
            m = _AUTHOR.match(row)
            if m:
                spans.append((m.start(1), m.end(1), f"bold {pal.accent}"))
            else:
                m = _REVIEW_LINE.match(row)
                if m:
                    style = status_style(m.group(1), dark)
                    if style:
                        spans.append((m.start(1), m.end(1), style))
                    at = row.find("@")
                    if at >= 0 and row.startswith("  ") and "  @" in row:
                        spans.append((at, len(row), f"bold {pal.accent}"))
                m = _THREAD.match(row)
                if m:
                    style = status_style(m.group(1), dark)
                    spans.append((2, 8, "bold"))
                    if style:
                        spans.append((m.start(1) - 1, m.end(1) + 1, style))
            doc.line(row, spans)
    return doc.content()


class Markdown(str):
    """Sanitized markdown text of a comment or review body, kept apart from its indent.

    As a ``str`` it is the body indented (the plain text the pane has always shown); the
    styler renders ``body`` as markdown and indents the result itself.
    """

    body: str
    indent: int

    def __new__(cls, body: str, indent: int) -> Markdown:
        pad = " " * indent
        self = super().__new__(cls, "\n".join(pad + x for x in body.split("\n")))
        self.body = body
        self.indent = indent
        return self


def body(name: str, texts: dict[str, str], lines: dict[str, list[str]], dark: bool) -> Content:
    """The styled body of the detail section ``name`` (one paint, one effort budget)."""
    effort = Effort()
    text = texts[name]
    if name == "facts":
        return facts(text, dark)
    if name == "ci":
        return ci(text, dark)
    if name == "changes":
        if text.startswith("  (not loaded)"):
            return Content(text, spans=[Span(0, len(text), palette(dark).quiet)])
        return diff(text, dark, effort)
    if name in lines:
        return threaded(lines[name], dark, effort)
    return Content(text)
