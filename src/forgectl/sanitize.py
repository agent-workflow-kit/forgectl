"""The shared sanitizer (interface: Sanitizing).

Everything forgectl prints that may hold untrusted text goes through this module, so a reader never
sees a raw control, bidi or invisible character. A replacement is a visible ``<U+XXXX>`` marker that
stays on its line, so line numbers and positions hold. Bodies forgectl *posts* are not sanitized;
they are neutralized (``neutralize.py``).

Three entry points differ only in what they keep:

- ``sanitize_text``: a text field. Tab and LF stay, CRLF becomes LF, a lone CR is a marker.
- ``sanitize_line``: a one-line field (``x-one-line``). Tab and LF are markers as well.
- ``sanitize_diff``: a diff. As ``sanitize_text``, except a CR before LF stays as it is.

``sanitize_bytes`` decodes first: bytes that are not valid UTF-8 become U+FFFD. Sanitizing runs
after
JSON decoding, so a lone surrogate arrives as a ``str`` code point and is marked, never raised on.

The marked set is the Unicode categories ``Cc``, ``Cf``, ``Cs``, ``Co``, ``Cn``, ``Zl``, ``Zp`` plus
the interface's explicit code points. ``Cn`` follows the running Python's Unicode database, so a
character assigned only in a newer version is marked on an older runtime.
"""

from __future__ import annotations

import unicodedata

_MARKED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# All variation selectors (U+FE00 to U+FE0F, U+E0100 to U+E01EF), U+034F, U+17B4, U+17B5,
# U+180B to U+180F, U+115F, U+1160, U+3164, U+FFA0 and U+2800.
_MARKED_CODE_POINTS = frozenset(
    {0x034F, 0x17B4, 0x17B5, 0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800}
    | set(range(0x180B, 0x1810))
    | set(range(0xFE00, 0xFE10))
    | set(range(0xE0100, 0xE01F0))
)


def _marked(ch: str) -> bool:
    return unicodedata.category(ch) in _MARKED_CATEGORIES or ord(ch) in _MARKED_CODE_POINTS


def _marker(ch: str) -> str:
    return f"<U+{ord(ch):04X}>"


def _sanitize(text: str, *, keep: str, crlf: str | None) -> str:
    """Mark every unsafe character except those in ``keep``.

    ``crlf`` is what a CR immediately followed by LF becomes: ``"\\n"`` (then LF is handled as
    usual, so a one-line field marks it), ``"\\r\\n"`` (kept as is) or ``None`` (no special case).
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\r" and crlf is not None and text.startswith("\n", i + 1):
            out.append(crlf if crlf == "\r\n" or "\n" in keep else _marker("\n"))
            i += 2
            continue
        if ch not in keep and (ch == "\r" or _marked(ch)):
            out.append(_marker(ch))
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def sanitize_text(text: str) -> str:
    """A text field: markers everywhere except tab and LF; CRLF becomes LF."""
    return _sanitize(text, keep="\t\n", crlf="\n")


def sanitize_line(text: str) -> str:
    """A one-line field: tab and LF become markers too (a CRLF is one LF marker)."""
    return _sanitize(text, keep="", crlf="\n")


def sanitize_diff(text: str) -> str:
    """A diff: as text, but a CR before LF stays (the file may really use CRLF)."""
    return _sanitize(text, keep="\t\n", crlf="\r\n")


def sanitize_bytes(data: bytes, *, diff: bool = False) -> str:
    """Bytes from a child or a file: invalid UTF-8 becomes U+FFFD, then text or diff rules."""
    text = data.decode("utf-8", errors="replace")
    return sanitize_diff(text) if diff else sanitize_text(text)
