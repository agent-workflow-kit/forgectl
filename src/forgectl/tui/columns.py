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


WIDTHS = (0, 26, 1, 5, 3, 20, 9)  # columns 0..6; the first is set per list, the rest are fixed
TITLE_WIDTH = 40


def fit(text: str, width: int) -> str:
    """``text`` cut to ``width`` terminal cells (ending in ``…`` when cut) and padded to it."""
    if cell_len(text) <= width:
        return set_cell_size(text, width)
    if width < 1:
        return ""
    return set_cell_size(text, width - 1) + "…"


def row_line(parts: list[str], pr_width: int) -> str:
    """One line: the seven fixed columns, the title, then the author and roles."""
    widths = (pr_width, *WIDTHS[1:])
    fixed = [fit(p, w) for p, w in zip(parts[:7], widths, strict=True)]
    rest = [fit(parts[7], TITLE_WIDTH), *parts[8:]]
    return "  ".join([*fixed, *[p for p in rest if p]]).rstrip()


# -- ordering ----------------------------------------------------------------------------------


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


def order(rows: list[Row], states: dict[str, RowState], mode: str) -> list[Row]:
    """``repo``: grouped by repo, newest update first within a repo. ``attention``: needs
    attention first, then by repo and newest update. Ties end on ``pr``. Stable sorts. Updates
    compare as instants; a null or unparsable time sorts after every real one."""
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
