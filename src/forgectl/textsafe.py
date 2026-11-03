"""Stand-in for the sanitizer until ``sanitize.py`` lands (interface: Sanitizing).

Everything the CLI prints that may hold untrusted text goes through ``sanitize_text`` (or
``sanitize_line`` for a one-line field), so the call sites are already in place. The rules here
are the interface's character rules: a visible ``<U+XXXX>`` marker for every control (tab and LF
excepted), format, surrogate, private-use, unassigned, line and paragraph separator character,
and for the explicit list (all variation selectors, U+034F, U+17B4, U+17B5, U+180B to U+180F,
U+115F, U+1160, U+3164, U+FFA0, U+2800); CRLF becomes LF; a lone CR is a marker; bytes that are
not UTF-8 become U+FFFD (``sanitize_bytes``).

TODO(#6): replace the body with ``sanitize.py``'s rules (diff mode where a CR before LF stays,
the per-field one-line list from the schemas).
"""

from __future__ import annotations

import unicodedata

_MARKED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_MARKED_CODE_POINTS = frozenset(
    {0x034F, 0x17B4, 0x17B5, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800}
    | set(range(0x180B, 0x1810))
    | set(range(0xFE00, 0xFE10))
    | set(range(0xE0100, 0xE01F0))
)


def _marked(ch: str, keep: str) -> bool:
    if ch in keep:
        return False
    return unicodedata.category(ch) in _MARKED_CATEGORIES or ord(ch) in _MARKED_CODE_POINTS


def _sanitize(text: str, keep: str) -> str:
    text = text.replace("\r\n", "\n")
    return "".join(f"<U+{ord(ch):04X}>" if _marked(ch, keep) else ch for ch in text)


def sanitize_text(text: str) -> str:
    """A text field: markers everywhere except tab and LF."""
    return _sanitize(text, "\t\n")


def sanitize_line(text: str) -> str:
    """A one-line field: tab and LF become markers as well."""
    return _sanitize(text, "")


def sanitize_bytes(data: bytes) -> str:
    """Bytes from a child or a file: invalid UTF-8 becomes U+FFFD, then ``sanitize_text``."""
    return sanitize_text(data.decode("utf-8", errors="replace"))
