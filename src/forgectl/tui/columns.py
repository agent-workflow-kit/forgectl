"""List columns, ordering and filters for `browse`. Pure functions: no widgets, no I/O.

A row's cells come from two sources: the ``prs`` item (always there) and one ``snapshot`` per
row, which arrives later. ``Summary`` is the small slice of a snapshot the list needs, so the
full snapshot is not kept per row. ``activity`` is derived by forgectl (docs/interface.md,
Activity), and the counts here are shown as given: this module never recomputes "others since
the push", so an author in ``activity.ignore_authors`` cannot move a row.

Fields read from the snapshot (all optional, anything of the wrong type counts as unknown):
``ci.state``, ``approvals``, ``review_decision``, ``blocked_by[0].code``, ``head.pushed_at``,
``updated_at``, ``complete`` and
``activity.{comments_total, unresolved_threads, others_since_push, awaiting_my_reply}``.

Every fixed-width cell is cut and padded by terminal cell width, so wide characters keep the
later columns aligned, and a row is one line.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rich.cells import cell_len, set_cell_size

from . import safe
from .source import Row

# Row load states.
LOADING = "loading"
LOADED = "loaded"
FAILED = "failed"

SORT_REPO = "repo"
SORT_ATTENTION = "attention"
SORT_MODES = (SORT_REPO, SORT_ATTENTION)

FILTER_AUTHORED = "authored"
FILTER_REQUESTED = "requested"
FILTER_CI_FAILING = "ci-failing"
FILTER_NEW_COMMENTS = "new-comments"
FILTER_ORDER = (FILTER_AUTHORED, FILTER_REQUESTED, FILTER_CI_FAILING, FILTER_NEW_COMMENTS)

CI_GLYPHS = {"passing": "✓", "failing": "✗", "pending": "…", "none": "-"}
DECISIONS = {"approved": "ok", "changes_requested": "chg", "review_required": "req", "none": "-"}


@dataclass
class Summary:
    """What the list shows from one snapshot. ``None`` means unknown or not reported."""

    ci_state: str | None = None
    approvals: int | None = None
    review_decision: str | None = None
    blocker: str | None = None
    pushed_at: str | None = None
    updated_at: str | None = None
    comments_total: int | None = None
    unresolved_threads: int | None = None
    others_since_push: int | None = None
    awaiting_my_reply: int | None = None
    complete: bool = True  # False: the snapshot says part of its data could not be read


@dataclass
class RowState:
    status: str = LOADING
    summary: Summary | None = None
    error: str | None = None  # short and sanitized: "code" of the failed snapshot
    stale: bool = False  # the last refresh failed; ``summary`` is the last good data
    refreshing: bool = False  # a refresh is under way; ``summary`` is the previous data
    changed: bool = False  # the last refresh changed what the row shows; cleared by a keypress


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> int | None:
    # bool is an int subclass; a true/false is not a count.
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _word(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def summarize(snap: dict[str, Any]) -> Summary:
    activity = _d(snap.get("activity"))
    blocked = snap.get("blocked_by")
    first = blocked[0] if isinstance(blocked, list) and blocked else None
    return Summary(
        ci_state=_word(_d(snap.get("ci")).get("state")),
        approvals=_count(snap.get("approvals")),
        review_decision=_word(snap.get("review_decision")),
        blocker=_word(_d(first).get("code")),
        pushed_at=_word(_d(snap.get("head")).get("pushed_at")),
        updated_at=_word(snap.get("updated_at")),
        comments_total=_count(activity.get("comments_total")),
        unresolved_threads=_count(activity.get("unresolved_threads")),
        others_since_push=_count(activity.get("others_since_push")),
        awaiting_my_reply=_count(activity.get("awaiting_my_reply")),
        complete=snap.get("complete") is not False,
    )


# -- cells -------------------------------------------------------------------------------------


def parse_time(value: Any) -> datetime | None:
    """An RFC 3339 time as an aware datetime, or None (never compare these as strings)."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        # 3.12's parser takes `Z` but not `z`, which RFC 3339 allows
        parsed = datetime.fromisoformat(value[:-1] + "Z" if value.endswith("z") else value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def age(value: Any, now: datetime) -> str:
    """``5m``, ``3h``, ``2d``, ``6w``, ``1y``; blank when the time is null or unparsable."""
    then = parse_time(value)
    if then is None:
        return ""
    seconds = max(0, int((now - then).total_seconds()))
    for unit, size in (("y", 365 * 86400), ("w", 7 * 86400), ("d", 86400), ("h", 3600)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds // 60}m"


def approvals_cell(s: Summary) -> str:
    """The bare ``approvals`` count on both hosts; ``?`` when the snapshot has none."""
    # todo(#86): render approvals/required once the snapshot exposes the required count
    return "?" if s.approvals is None else str(s.approvals)


def ci_cell(s: Summary) -> str:
    """The CI glyph. A none or unknown state from an incomplete snapshot is ``?``, not "no CI"."""
    glyph = CI_GLYPHS.get(s.ci_state or "", "?")
    return "?" if glyph == CI_GLYPHS["none"] and not s.complete else glyph


def comments_cell(s: Summary) -> str:
    def part(label: str, value: int | None) -> str:
        return f"{label}{'-' if value is None else value}"

    text = " ".join(
        (
            part("c", s.comments_total),
            part("u", s.unresolved_threads),
            part("n", s.others_since_push),
            part("r", s.awaiting_my_reply),
        )
    )
    return text if s.complete else text + "?"


def badge_cell(item: dict[str, Any], s: Summary | None) -> str:
    state = "draft" if item.get("draft") is True else safe.line(item.get("state"), 12)
    if s is not None and s.blocker:
        return f"{state} {safe.line(s.blocker, 20)}"
    return state


def cells(row: Row, state: RowState, now: datetime) -> list[str]:
    """The text cells of a row, in column order, every forge string sanitized."""
    item = row.item
    pushed = ""
    updated = age(item.get("updated_at"), now)
    if state.status == LOADED and state.summary is not None:
        s = state.summary
        pushed = age(s.pushed_at, now)
        # the snapshot's updated_at is fresher than the list's when both are there
        updated = age(s.updated_at, now) or updated
        ci = ci_cell(s)
        approvals = approvals_cell(s)
        decision = DECISIONS.get(s.review_decision or "", "?")
        comments = comments_cell(s)
        badge = badge_cell(item, s)
    elif state.status == FAILED:
        ci, approvals, decision = "!", "", ""
        comments = f"ERROR {state.error or 'failed'}"
        badge = badge_cell(item, None)
    else:
        ci, approvals, decision, comments = "·", "", "", "loading..."
        badge = badge_cell(item, None)
    marker = ""
    if state.refreshing:
        marker = "[refreshing]"
    elif state.stale:
        marker = f"[stale: {state.error or 'refresh failed'}]"
    elif state.changed:
        marker = "[changed]"
    return [
        safe.line(item.get("pr")),
        badge,
        ci,
        approvals,
        decision,
        comments,
        f"{pushed or '-'}/{updated or '-'}",
        safe.line(item.get("title")),
        "@" + _login(item.get("author")),
        "+".join(sorted(row.roles)),
        marker,
    ]


def _login(author: Any) -> str:
    login = _d(author).get("login")
    return safe.line(login) if login else "(unknown)"


SEPARATOR = "  "
PR_MAX_WIDTH = 28  # a longer PR id is cut from the left, so the number stays visible
PINNED = 2  # the first columns that stay at the left while the rest scroll: pr, note


@dataclass(frozen=True)
class Column:
    """One list column. ``index`` is the cell's place in ``cells()``; ``width`` is None for the
    PR column, whose width is set per list. ``header`` is constant text, never forge text."""

    id: str
    header: str
    index: int
    width: int | None


# The one place that says what the columns are, in what order and how wide. Sort keys and click
# targets and colors (todo(#102)) attach here.
COLUMNS = (
    Column("pr", "PR", 0, None),
    Column("note", "note", 10, 12),  # [refreshing], [stale: code], [changed]; pinned
    Column("badge", "badge", 1, 26),
    Column("ci", "CI", 2, 3),  # a glyph; wide enough for the sort arrow beside the header
    Column("approvals", "appr", 3, 5),
    Column("review", "rev", 4, 4),
    Column("comments", "comments", 5, 20),
    Column("age", "age", 6, 9),
    Column("title", "title", 7, 40),
    Column("author", "author", 8, 20),
    Column("roles", "roles", 9, 18),  # "authored+requested"
)


def fit(text: str, width: int) -> str:
    """``text`` cut to ``width`` terminal cells (ending in ``…`` when cut) and padded to it."""
    if cell_len(text) <= width:
        return set_cell_size(text, width)
    if width < 1:
        return ""
    return set_cell_size(text, width - 1) + "…"


def fit_tail(text: str, width: int) -> str:
    """Like ``fit`` but a cut text keeps its end and starts with ``…`` (``…repo#12``)."""
    if cell_len(text) <= width:
        return set_cell_size(text, width)
    if width < 1:
        return ""
    tail = ""
    for ch in reversed(text):
        if cell_len(ch + tail) > width - 1:
            break
        tail = ch + tail
    return set_cell_size("…" + tail, width)


def _widths(pr_width: int) -> list[int]:
    return [pr_width if c.width is None else c.width for c in COLUMNS]


def layout(pr_width: int) -> list[tuple[Column, int, int]]:
    """``(column, start, width)`` in table coordinates: the pinned columns first, then the
    scrolling ones, separated by ``SEPARATOR``. The one source for rendering and hit tests."""
    out: list[tuple[Column, int, int]] = []
    x = 0
    for column, width in zip(COLUMNS, _widths(pr_width), strict=True):
        out.append((column, x, width))
        x += width + len(SEPARATOR)
    return out


def pinned_width(pr_width: int) -> int:
    _, start, width = layout(pr_width)[PINNED - 1]
    return start + width


def scroll_extent(pr_width: int) -> int:
    """The width of the scrolling columns (the offset is counted from their start)."""
    _, start, width = layout(pr_width)[-1]
    first = layout(pr_width)[PINNED][1]
    return start + width - first


def _scroll_room(pr_width: int, view_width: int) -> int:
    return max(0, view_width - pinned_width(pr_width) - len(SEPARATOR))


def clamp_offset(offset: int, pr_width: int, view_width: int) -> int:
    """The horizontal offset (cells into the scrolling columns) kept within the table: 0 when
    the table is not wider than the view, never past the point where its end meets the edge."""
    furthest = max(0, scroll_extent(pr_width) - _scroll_room(pr_width, view_width))
    return max(0, min(offset, furthest))


def step_offset(offset: int, direction: int, pr_width: int, view_width: int) -> int:
    """The offset one column to the left (``direction`` -1) or right (+1), clamped."""
    current = clamp_offset(offset, pr_width, view_width)
    if _scroll_room(pr_width, view_width) == 0:
        return current  # nothing of the scrolling columns is in view: do not step blindly
    first = layout(pr_width)[PINNED][1]
    starts = [start - first for _, start, _ in layout(pr_width)[PINNED:]]
    if direction > 0:
        later = [s for s in starts if s > current]
        return clamp_offset(later[0] if later else current, pr_width, view_width)
    earlier = [s for s in starts if s < current]
    return clamp_offset(earlier[-1] if earlier else 0, pr_width, view_width)


def column_at(x: int, pr_width: int, offset: int, view_width: int) -> str | None:
    """The id of the column under cell ``x`` of the list area (0 is its left edge), or None on
    a separator or outside the view. Counts the pinned columns and the horizontal offset."""
    if x < 0 or x >= view_width:
        return None
    pinned = pinned_width(pr_width)
    rows = layout(pr_width)
    first = rows[PINNED][1]
    if x < pinned:
        table_x, candidates = x, rows[:PINNED]
    elif x < pinned + len(SEPARATOR):
        return None
    else:
        table_x = x - pinned - len(SEPARATOR) + clamp_offset(offset, pr_width, view_width) + first
        candidates = rows[PINNED:]
    for column, start, width in candidates:
        if start <= table_x < start + width:
            return column.id
    return None


def _slice_cells(text: str, start: int, width: int) -> str:
    """``width`` cells of ``text`` from cell ``start``; a wide char cut by an edge becomes a
    space."""
    if text.isascii():  # one cell per character (zero-width marks can offset wide chars)
        return set_cell_size(text[start : start + width], width)
    out: list[str] = []
    x = 0
    end = start + width
    kept = False  # the previous character was copied whole; a zero-width mark follows its base
    for ch in text:
        w = cell_len(ch)
        if w == 0:
            if kept:
                out.append(ch)
            continue
        if x >= end:
            break
        kept = x >= start and x + w <= end
        if kept:
            out.append(ch)
        elif x + w > start and x < end:
            out.append(" " * (min(x + w, end) - max(x, start)))
        x += w
    return set_cell_size("".join(out), width)


Cut = tuple[str, str]  # a row's pinned text and its scrolling text, every cell cut and padded


def _cut(texts: list[str], pr_width: int) -> Cut:
    widths = _widths(pr_width)
    cut = [
        (fit_tail if c.id == "pr" else fit)(texts[i], w)
        for i, (c, w) in enumerate(zip(COLUMNS, widths, strict=True))
    ]
    return SEPARATOR.join(cut[:PINNED]), SEPARATOR.join(cut[PINNED:])


def view(cut: Cut, pr_width: int, offset: int, view_width: int | None) -> str:
    """A cut row as one line. With ``view_width`` the pinned text stays, the rest is shifted
    left by ``offset`` and clipped to the view; without it the whole table is returned."""
    pinned, scrolling = cut
    if view_width is None:
        return pinned + SEPARATOR + scrolling
    offset = clamp_offset(offset, pr_width, view_width)
    room = min(_scroll_room(pr_width, view_width), cell_len(scrolling))
    line = pinned + SEPARATOR + _slice_cells(scrolling, offset, room)
    return _slice_cells(line, 0, min(view_width, cell_len(line)))


def cut_row(parts: list[str], pr_width: int) -> Cut:
    """The cut and padded cells of a row from ``cells()``; draw it with ``view``."""
    return _cut([parts[c.index] for c in COLUMNS], pr_width)


def row_line(
    parts: list[str], pr_width: int, offset: int = 0, view_width: int | None = None
) -> str:
    """One line from ``cells()``: every cell cut and padded to its column. With ``view_width``
    the first columns stay and the rest are shifted left by ``offset`` and clipped to the view;
    without it the whole table is returned."""
    return view(cut_row(parts, pr_width), pr_width, offset, view_width)


def header_line(
    pr_width: int, offset: int = 0, view_width: int | None = None, sort: SortState = ()
) -> str:
    """The column names, laid out exactly like ``row_line``. A sorted column's name is followed
    by an arrow for its direction. Constant text only: no forge text reaches the header."""
    # todo(#105): with several keys, show each key's position beside its arrow
    arrows = {k.column: DESC_GLYPH if k.descending else ASC_GLYPH for k in sort}
    texts = [c.header + arrows.get(c.id, "") for c in COLUMNS]
    cut = [fit(t, w) for t, w in zip(texts, _widths(pr_width), strict=True)]
    return view(
        (SEPARATOR.join(cut[:PINNED]), SEPARATOR.join(cut[PINNED:])), pr_width, offset, view_width
    )


def column_span(
    column_id: str, pr_width: int, offset: int, view_width: int
) -> tuple[int, int] | None:
    """The cells ``[start, end)`` of the list area a column shows at, clipped to the view, or
    None when none of it is in view. The inverse of ``column_at``."""
    rows = layout(pr_width)
    index = next((i for i, (c, _, _) in enumerate(rows) if c.id == column_id), None)
    if index is None:
        return None
    _, start, width = rows[index]
    low = 0
    if index >= PINNED:
        low = pinned_width(pr_width) + len(SEPARATOR)
        shift = low - rows[PINNED][1] - clamp_offset(offset, pr_width, view_width)
        start += shift
    first, last = max(start, low), min(start + width, view_width)
    return (first, last) if first < last else None


def reveal_offset(column_id: str, pr_width: int, offset: int, view_width: int) -> int:
    """The offset that brings a scrolling column into view (its left edge when it is wider than
    the room); a pinned column leaves the offset as it is."""
    rows = layout(pr_width)
    index = next((i for i, (c, _, _) in enumerate(rows) if c.id == column_id), None)
    current = clamp_offset(offset, pr_width, view_width)
    if index is None or index < PINNED:
        return current
    _, start, width = rows[index]
    left = start - rows[PINNED][1]
    room = _scroll_room(pr_width, view_width)
    if left < current or width > room:
        return clamp_offset(left, pr_width, view_width)
    if left + width > current + room:
        return clamp_offset(left + width - room, pr_width, view_width)
    return current


# -- ordering ----------------------------------------------------------------------------------

ASC_GLYPH = "▲"
DESC_GLYPH = "▼"


@dataclass(frozen=True)
class SortKey:
    column: str  # a ``Column.id``
    descending: bool = False


# The one sort state: an ordered list of keys, most significant first; empty means the preset
# order. The header (click and cursor) sets it, and ``order`` applies it, in one place.
# todo(#105): the ORDER BY of the query line reads and writes this same tuple, with several keys.
SortState = tuple[SortKey, ...]

SORTABLE = tuple(c.id for c in COLUMNS if c.id != "note")  # the note is a status, not data


def toggle_sort(sort: SortState, column: str) -> SortState:
    """Activate a column: the same single column flips its direction, any other column becomes
    the only key, ascending. A column that cannot be sorted leaves the sort as it is."""
    if column not in SORTABLE:
        return sort
    if len(sort) == 1 and sort[0].column == column:
        return (SortKey(column, not sort[0].descending),)
    # todo(#105): a multi-key sort is built in the query form, not by header activation
    return (SortKey(column),)


def sort_text(sort: SortState) -> str:
    return ", ".join(f"{k.column} {'desc' if k.descending else 'asc'}" for k in sort)


CI_RANK = {"failing": 0, "pending": 1, "passing": 2, "none": 3}
REVIEW_RANK = {"changes_requested": 0, "review_required": 1, "approved": 2, "none": 3}


def _instant(value: Any) -> float | None:
    when = parse_time(value)
    return None if when is None else when.timestamp()


def _number(row: Row) -> int:
    n = row.item.get("number")
    if isinstance(n, int) and not isinstance(n, bool):
        return n
    digits = row.pr.rsplit("#", 1)[-1]
    return int(digits) if digits.isdigit() else 0


def _value(column: str, row: Row, state: RowState) -> Any:
    """The parsed value a column sorts by, or None when it is unknown (no snapshot yet, or the
    snapshot did not report it). Never the displayed text."""
    item = row.item
    s = state.summary if state.status == LOADED else None
    if column == "pr":
        return (repo_of(row).casefold(), _number(row))
    if column == "title":
        return safe.line(item.get("title")).casefold()
    if column == "author":
        return _login(item.get("author")).casefold()
    if column == "roles":
        return "+".join(sorted(row.roles)).casefold()
    if column == "badge":
        return badge_cell(item, s).casefold()
    if column == "age":
        # the update time as the cell shows it (the snapshot's is fresher), then the push
        updated = _instant(s.updated_at) if s else None
        if updated is None:
            updated = _instant(item.get("updated_at"))
        if updated is None:
            return None
        pushed = _instant(s.pushed_at) if s else None
        # youngest first when ascending: the key is minus the instant
        return (-updated, float("inf") if pushed is None else -pushed)
    if s is None:
        return None
    if column == "ci":
        return CI_RANK.get(s.ci_state or "")
    if column == "review":
        return REVIEW_RANK.get(s.review_decision or "")
    if column == "approvals":
        return s.approvals
    if column == "comments":
        parts = (s.unresolved_threads, s.others_since_push, s.awaiting_my_reply, s.comments_total)
        if all(p is None for p in parts):
            return None
        return tuple(-1 if p is None else p for p in parts)  # a count not reported is below 0
    return None


def sort_values(sort: SortState, row: Row, state: RowState) -> tuple[Any, ...]:
    """The raw value of each key for a row; two equal tuples sort the row to the same place."""
    return tuple(_value(k.column, row, state) for k in sort)


class _Reversed:
    """Wraps a value so that it compares the other way round (a descending key)."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __lt__(self, other: _Reversed) -> bool:
        return bool(other.value < self.value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Reversed) and self.value == other.value

    def __hash__(self) -> int:
        return hash(self.value)


def sort_key(sort: SortState, row: Row, state: RowState) -> tuple[Any, ...]:
    """The key of a row under ``sort``. An unknown value is after every known one whatever the
    direction, and rows that tie end on the PR (repo, then number as an integer)."""
    key: list[Any] = []
    for k, value in zip(sort, sort_values(sort, row, state), strict=True):
        if value is None:
            key.append((1, 0))
        else:
            key.append((0, _Reversed(value) if k.descending else value))
    key.append((repo_of(row).casefold(), _number(row), row.pr))
    return tuple(key)


def attention_rank(state: RowState) -> int:
    """0 failing CI, 1 new comments from others, 2 awaiting my reply, 3 changes requested, 4 rest.

    A row without a loaded snapshot is in the rest: nothing is known to need attention.
    """
    if state.status != LOADED or state.summary is None:
        return 4
    s = state.summary
    if s.ci_state == "failing":
        return 0
    if (s.others_since_push or 0) > 0:
        return 1
    if (s.awaiting_my_reply or 0) > 0:
        return 2
    if s.review_decision == "changes_requested":
        return 3
    return 4


def repo_of(row: Row) -> str:
    return str(row.item.get("repo") or row.pr.rsplit("#", 1)[0])


def _newest_first(row: Row) -> tuple[int, float]:
    when = parse_time(row.item.get("updated_at"))
    return (1, 0.0) if when is None else (0, -when.timestamp())


def order(
    rows: list[Row], states: dict[str, RowState], mode: str, sort: SortState = ()
) -> list[Row]:
    """A column ``sort``, when there is one, replaces the preset ``mode``. Otherwise ``repo``:
    grouped by repo, newest update first within a repo. ``attention``: needs attention first,
    then by repo and newest update. Ties end on ``pr``. Stable sorts. Updates
    compare as instants; a null or unparsable time sorts after every real one."""
    if sort:
        return sorted(rows, key=lambda r: sort_key(sort, r, states.get(r.pr) or RowState()))
    ordered = sorted(rows, key=lambda r: r.pr)
    ordered.sort(key=_newest_first)
    ordered.sort(key=repo_of)
    if mode == SORT_ATTENTION:
        ordered.sort(key=lambda r: attention_rank(states.get(r.pr) or RowState()))
    return ordered


# -- filters -----------------------------------------------------------------------------------


def matches(row: Row, state: RowState, filters: set[str]) -> bool:
    """Every active filter must hold (an intersection). The two snapshot-based filters need a
    loaded snapshot: a loading or failed row does not match them."""
    s = state.summary if state.status == LOADED else None
    if FILTER_AUTHORED in filters and "authored" not in row.roles:
        return False
    if FILTER_REQUESTED in filters and "requested" not in row.roles:
        return False
    if FILTER_CI_FAILING in filters and not (s is not None and s.ci_state == "failing"):
        return False
    if FILTER_NEW_COMMENTS in filters and not (s is not None and (s.others_since_push or 0) > 0):
        return False
    return True


def filters_text(filters: set[str]) -> str:
    return ", ".join(f for f in FILTER_ORDER if f in filters)


def utc_now() -> datetime:
    return datetime.now(UTC)
