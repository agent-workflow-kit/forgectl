"""Where the query language meets the list: a row's values for the evaluator, and the sort state.

``query.py`` knows nothing of rows. Here a row and its load state become the ``get(field)``
function the evaluator takes: the list item's fields are always there, and the snapshot's fields
(CI, review, approvals, the counts, ``pushed``) are None, so unknown, until the snapshot has
loaded. Forge text is compared as it is, never shown; what reaches the screen goes through
``safe`` elsewhere.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import columns, query
from .source import Row

_TEXT_LIMIT = 2000  # forge text longer than this is cut before it is matched: bounded work


def _text(value: Any) -> str | None:
    return value[:_TEXT_LIMIT] if isinstance(value, str) else None


def _login(item: dict[str, Any]) -> str | None:
    author = item.get("author")
    return _text(author.get("login")) if isinstance(author, dict) else None


def _age(value: Any, now: datetime) -> int | None:
    when = columns.parse_time(value)
    return None if when is None else max(0, int((now - when).total_seconds()))


def getter(row: Row, state: columns.RowState, now: datetime) -> query.Getter:
    """``get(field)`` for one row; see ``query.Getter``. A snapshot's fields need a loaded one."""
    item = row.item
    s = state.summary if state.status == columns.LOADED else None

    def ci() -> str | None:
        if s is None or s.ci_state not in query.VALUES["ci"]:
            return None
        # a "none" from an incomplete snapshot is not "no CI": it is unknown (as the cell's `?`)
        return None if s.ci_state == "none" and not s.complete else s.ci_state

    def review() -> str | None:
        return (
            s.review_decision
            if s is not None and s.review_decision in query.VALUES["review"]
            else None
        )

    def updated() -> int | None:
        # the update time as the age cell shows it: the snapshot's is fresher than the list's
        fresh = _age(s.updated_at, now) if s is not None else None
        return fresh if fresh is not None else _age(item.get("updated_at"), now)

    def get(name: str) -> Any:
        if name == "repo":
            return _text(columns.repo_of(row))
        if name == "pr":
            return _text(row.pr)
        if name == "title":
            return _text(item.get("title"))
        if name == "author":
            return _login(item)
        if name == "state":
            return _text(item.get("state"))
        if name == "number":
            return columns.number_of(row)
        if name == "draft":
            return item.get("draft") is True
        if name == "authored":
            return "authored" in row.roles
        if name == "requested":
            return "requested" in row.roles
        if name == "@me":
            return "authored" in row.roles  # the PR is listed as authored by the viewer
        if name == "age":
            return updated()
        if name == "text":
            return tuple(
                t
                for t in (_text(item.get("title")), _text(columns.repo_of(row)), _login(item))
                if t
            )
        if name == "ci":
            return ci()
        if name == "review":
            return review()
        if s is None:
            return None
        return {
            "blocker": s.blocker,
            "approvals": s.approvals,
            "comments": s.comments_total,
            "unresolved": s.unresolved_threads,
            "others_since_push": s.others_since_push,
            "awaiting_reply": s.awaiting_my_reply,
            "pushed": _age(s.pushed_at, now),
        }.get(name)

    return get


def evaluate(
    where: query.Node | None, row: Row, state: columns.RowState, now: datetime
) -> bool | None:
    return query.evaluate(where, getter(row, state, now))


def matches(where: query.Node | None, row: Row, state: columns.RowState, now: datetime) -> bool:
    """Whether the row is shown: the filter must be true. A row whose answer needs a value that is
    not known (no snapshot yet) is not shown."""
    return evaluate(where, row, state, now) is True


def unknown_count(
    where: query.Node | None, rows: list[Row], states: dict[str, columns.RowState], now: datetime
) -> int:
    """How many rows the filter cannot decide yet, for lack of a value (a snapshot not loaded)."""
    return sum(
        1 for r in rows if evaluate(where, r, states.get(r.pr) or columns.RowState(), now) is None
    )


def sort_of(order: tuple[query.OrderKey, ...]) -> columns.SortState:
    return tuple(columns.SortKey(column, descending) for column, descending in order)


def order_of(sort: columns.SortState) -> tuple[query.OrderKey, ...]:
    return tuple((k.column, k.descending) for k in sort)
