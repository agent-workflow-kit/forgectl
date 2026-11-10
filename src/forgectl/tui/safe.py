"""The one place the TUI gets its text sanitizer and its inert text type.

Every piece of forge text on screen goes through ``text`` or ``line`` and is wrapped by
``plain``, which builds a ``Content`` that is never parsed as markup (no ``[@click=...]``
actions, no styles). Neither the sanitizer nor the markup switch is imported anywhere else in
``forgectl.tui``, so changing the sanitizer is a change to this file alone.
"""

from __future__ import annotations

from textual.content import Content

from forgectl.sanitize import sanitize_diff as _sanitize_diff
from forgectl.sanitize import sanitize_line as _sanitize_line
from forgectl.sanitize import sanitize_text as _sanitize_text

# Bounds that keep a hostile or huge field from freezing the UI. Applied before sanitizing, so
# the work is bounded too.
LINE_LIMIT = 300
TEXT_LIMIT = 20_000
DIFF_LIMIT = 100_000


def _cut(value: object, limit: int) -> str:
    s = value if isinstance(value, str) else ("" if value is None else str(value))
    return s if len(s) <= limit else s[:limit] + "<truncated>"


def line(value: object, limit: int = LINE_LIMIT) -> str:
    """A one-line field: bounded, then sanitized (tab and LF become markers)."""
    return _sanitize_line(_cut(value, limit))


def text(value: object, limit: int = TEXT_LIMIT) -> str:
    """A multi-line field: bounded, then sanitized."""
    return _sanitize_text(_cut(value, limit))


def diff(value: object, limit: int = DIFF_LIMIT) -> str:
    """A diff: bounded, then sanitized in diff mode (a CR before LF stays)."""
    return _sanitize_diff(_cut(value, limit))


def plain(value: str) -> Content:
    """``value`` as display content with no markup parsing."""
    return Content(value)
