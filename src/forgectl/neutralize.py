"""Neutralizing every body forgectl posts (interface: Neutralizing, Dedupe key).

No Markdown is parsed: a GitLab quick action runs from any line of a note, so fences, quotes and
comments can be broken out of. Two rules apply to every post, with no opt-out:

1. Split on LF, CRLF, lone CR, VT, FF, FS, GS, RS, NEL, LS and PS, and rejoin with LF.
2. Every line whose first character is ``/`` gets one leading space, inside fences and quotes too.

Marker-shaped text is handled here as well: case-insensitive ``forgectl:dedupe:`` has its second
colon replaced by ``-`` (before the real marker is added by the caller), and each occurrence counts
in ``markers_removed``. A fixed point: neutralizing the output changes and counts nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# CRLF first, so it is one break; then every single-character separator in the interface.
_LINE_BREAK = re.compile("\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
_MARKER = re.compile(r"(forgectl:dedupe):", re.IGNORECASE)


@dataclass(frozen=True)
class Neutralized:
    text: str
    neutralized_lines: int
    markers_removed: int


def neutralize(body: str) -> Neutralized:
    lines = _LINE_BREAK.split(body)
    neutralized = 0
    for i, line in enumerate(lines):
        if line.startswith("/"):
            lines[i] = " " + line
            neutralized += 1
    text, removed = _MARKER.subn(r"\1-", "\n".join(lines))
    return Neutralized(text=text, neutralized_lines=neutralized, markers_removed=removed)
