"""The sections of the browse detail pane: their order, number keys, headers and the open state.

Headers are built only from constants and numbers; forge text never reaches one.
"""

from __future__ import annotations

from collections.abc import Iterable

# (name, title) in the order the pane shows them; the key of a section is its position, from 1.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("facts", "Facts"),
    ("description", "Description"),
    ("changes", "Changes"),
    ("ci", "CI"),
    ("comments", "Comments"),
    ("reviews", "Reviews"),
)
NAMES: tuple[str, ...] = tuple(name for name, _ in SECTIONS)
DEFAULT_COLLAPSED: tuple[str, ...] = ("changes",)
OPEN_MARK = "▾"
CLOSED_MARK = "▸"


def number_of(name: str) -> int:
    return NAMES.index(name) + 1


def name_of(key: str) -> str | None:
    """The section a number key toggles, or None."""
    if len(key) == 1 and key.isascii() and key.isdigit() and 1 <= int(key) <= len(NAMES):
        return NAMES[int(key) - 1]
    return None


def initial_open(collapsed: Iterable[str]) -> set[str]:
    return set(NAMES) - set(collapsed)


def header(name: str, is_open: bool, count: str | None) -> str:
    title = dict(SECTIONS)[name]
    mark = OPEN_MARK if is_open else CLOSED_MARK
    return f"{mark} {number_of(name)} {title}" + (f" ({count})" if count else "")
