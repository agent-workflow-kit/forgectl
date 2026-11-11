"""The light poll of a refresh (#118). Pure: no widgets, no I/O beyond the source call.

``LiveSource.light()`` (the ``poll`` command) answers, for every PR of mine on every host, a small
tuple that tells whether the PR changed. ``browse`` keeps the tuple of each row it read; a refresh
polls, compares, and reads only the rows whose tuple differs, through the bulk reader. A source
without ``light``, a poll that fails, a host that cannot be polled and a list the poll cut are all
read the old way (``prs``, then the per-row snapshots): ``LightFallback`` says why. A poll that
failed on every host for a rate limit is not followed by ``prs`` (``LightHeld``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import columns, safe
from .source import DataSource, Listing, Row

#: The fields of a ``prs`` item that a snapshot also carries: a row that was read takes them.
LIST_KEYS = (
    "pr",
    "pr_id",
    "repo",
    "number",
    "url",
    "title",
    "author",
    "state",
    "draft",
    "labels",
    "head",
    "base",
    "updated_at",
)

Key = tuple[Any, ...]

#: A kept tuple that must not be trusted (its row was read outside the poll, or its read never
#: arrived): it equals no real tuple, so the next poll reads the row.
STALE: Key = ()


class LightFallback(Exception):
    """The poll cannot stand in for ``prs`` this time; the text is the reason (status line)."""


class LightUnsupportedHost(LightFallback):
    """A host cannot be polled lightly: the source stays on the old path for the session."""


class LightHeld(Exception):
    """The poll failed on every host for a rate limit: ``prs`` would fail the same way, so the
    refresh ends. ``rate_limit`` is the poll's own reading (None when the error carried none)."""

    def __init__(self, text: str, rate_limit: Any = None) -> None:
        super().__init__(text)
        self.rate_limit = rate_limit


def key_of(item: dict[str, Any]) -> Key:
    """What makes a PR changed: ``(pr_id, head sha, updated_at, CI state, review decision)`` plus
    the GitLab extras (approvals left, approved, merge status, note count; null on GitHub)."""
    return (
        item.get("pr_id"),
        item.get("head_sha"),
        item.get("updated_at"),
        item.get("ci_state"),
        item.get("review_decision"),
        item.get("approvals_left"),
        item.get("approved"),
        item.get("merge_status"),
        item.get("notes_count"),
    )


def placeholder(item: dict[str, Any]) -> dict[str, Any]:
    """A ``prs``-shaped item for a PR first seen by the poll, until its snapshot arrives."""
    login = item.get("author")
    return {
        "pr": item["pr"],
        "pr_id": item.get("pr_id"),
        "repo": item.get("repo"),
        "number": item.get("number"),
        "url": item.get("url"),
        "title": item.get("title"),
        "author": {"login": login if isinstance(login, str) else None, "id": None},
        "state": "open",
        "draft": item.get("draft") is True,
        "labels": [],
        "head": {"sha": item.get("head_sha"), "ref": None, "repo": None, "is_fork": None},
        "base": {"ref": None},
        "updated_at": item.get("updated_at"),
    }


def item_of_snapshot(pr: str, snap: dict[str, Any]) -> dict[str, Any] | None:
    """The list item a snapshot carries, or None when it is not that PR's or lacks the basics."""
    if snap.get("pr") != pr or not isinstance(snap.get("updated_at"), str):
        return None
    return {k: snap[k] for k in LIST_KEYS if k in snap}


def head_sha(item: dict[str, Any]) -> Any:
    head = item.get("head")
    return head.get("sha") if isinstance(head, dict) else None


def load_light(source: DataSource, current: dict[str, Row]) -> Listing:
    """The ``Listing`` of the light poll: a row already shown stays the same object (its roles
    kept in step), a PR new to the list gets a placeholder row. Raises ``LightFallback`` when the
    list cannot be taken from the poll, ``ForgectlError`` when the poll failed."""
    poll = getattr(source, "light", None)
    if poll is None:
        raise LightFallback("the source has no light poll")
    answer = poll()
    if not isinstance(answer, dict) or not isinstance(answer.get("items"), list):
        raise LightFallback("the light poll gave no list")
    errors_raw = answer.get("errors") or []
    queried = answer.get("hosts_queried") or []
    if answer.get("unsupported"):
        raise LightUnsupportedHost("a host cannot be polled lightly")
    if answer.get("truncated"):
        raise LightFallback("the light poll was cut")  # whatever else failed: rows would be lost
    if not answer.get("complete", False) and not errors_raw:
        raise LightFallback("the light poll was cut")
    if errors_raw and len(errors_raw) >= max(1, len(queried)):
        if all(isinstance(e, dict) and e.get("code") == "rate_limited" for e in errors_raw):
            raise LightHeld("rate_limited", answer.get("rate_limit"))
        raise LightFallback("the light poll failed on every host")
    rows: list[Row] = []
    moved: set[str] = set()
    failed_hosts: set[str] = set()
    hosts_unknown = False
    errors: list[str] = []
    for err_item in errors_raw:
        host = err_item.get("host") if isinstance(err_item, dict) else None
        if isinstance(host, str) and host:
            failed_hosts.add(host)
        else:
            hosts_unknown = True
        errors.append(
            f"{safe.line(host, 60)}: {safe.line(err_item.get('code'), 60)}: "
            f"{safe.line(err_item.get('message'), 200)}"
            if isinstance(err_item, dict)
            else "poll: bad error"
        )
    for item in answer["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("pr"), str):
            raise LightFallback("the light poll gave an item without a PR")
        roles = {r for r in item.get("roles") or [] if r in ("authored", "requested")}
        row = current.get(item["pr"])
        if row is None:
            row = Row(item=placeholder(item), roles=roles)
        else:
            if row.roles != roles:
                row.roles = roles
            fresh = {k: item[k] for k in ("updated_at", "draft", "title") if k in item}
            if any(row.item.get(k) != v for k, v in fresh.items()):
                if row.item.get("updated_at") != fresh.get("updated_at"):
                    moved.add(item["pr"])
                row.item = {**row.item, **fresh}  # the order and the title follow the poll
        rows.append(row)
    rows.sort(key=lambda r: r.pr)
    rows.sort(key=lambda r: str(r.item.get("updated_at", "")), reverse=True)
    return Listing(
        rows=rows,
        complete=bool(answer.get("complete", False)),
        errors=errors,
        rate_limits=[answer.get("rate_limit")],
        answered=max(1, len(queried)) - len(errors_raw),
        failed_hosts=failed_hosts,
        hosts_unknown=hosts_unknown,
        light_items=list(answer["items"]),
        light_moved=moved,
    )


def changed(
    items: list[dict[str, Any]],
    rows: dict[str, Row],
    states: dict[str, columns.RowState],
    keys: dict[str, Key],
    known: set[str],
    moved: frozenset[str] | set[str] = frozenset(),
    keep: Callable[[str], bool] = lambda pr: False,
) -> set[str]:
    """The PRs to read: new to the list (not in ``known``), not loaded, stale or incomplete, or
    whose tuple differs from the one kept (``STALE`` differs from every tuple). ``keys`` is
    updated to this poll. A PR with no kept tuple (the first poll after a full read) has nothing
    to compare: it is read when its update time moved (``moved``) or its head sha differs from
    its list item, or when its snapshot shows pending CI, as the old path reads it. The kept
    tuples of the PRs ``keep`` names (those of a failed host) survive a poll that lacks them."""
    wanted: set[str] = set()
    for item in items:
        pr = item["pr"]
        key = key_of(item)
        old = keys.get(pr)
        keys[pr] = key
        row = rows.get(pr)
        state = states.get(pr)
        if pr not in known or row is None:
            wanted.add(pr)
        elif state is None or state.status != columns.LOADED or state.summary is None:
            wanted.add(pr)
        elif state.stale or not state.summary.complete:
            wanted.add(pr)
        elif old is None:
            if (
                pr in moved
                or head_sha(row.item) != item.get("head_sha")
                or state.summary.ci_state == "pending"
            ):
                wanted.add(pr)
        elif old != key:
            wanted.add(pr)
    listed = {i["pr"] for i in items}
    for gone in set(keys) - listed:
        if not keep(gone):
            del keys[gone]
    return wanted
