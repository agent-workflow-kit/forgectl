"""Stand-in for the sanitizer until ``sanitize.py`` lands.

Everything the CLI prints that may hold untrusted text goes through ``sanitize_text``, so the
call sites are already in place.

TODO(#6): replace the body with ``sanitize.py``'s rules (full marker set, one-line fields, CR
handling). The stand-in is deliberately conservative: it marks every control, format,
surrogate, private-use, unassigned and line/paragraph-separator character, keeping tab and LF.
"""

from __future__ import annotations

import unicodedata

_MARKED = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def sanitize_text(text: str) -> str:
    out: list[str] = []
    for ch in text:
        if ch in "\t\n" or unicodedata.category(ch) not in _MARKED:
            out.append(ch)
        else:
            out.append(f"<U+{ord(ch):04X}>")
    return "".join(out)
