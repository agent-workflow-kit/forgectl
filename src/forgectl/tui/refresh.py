"""What an automatic refresh re-reads, and the rate-limit backoff. Pure: no widgets, no I/O.

``snapshot`` costs several host calls per PR, so an ordinary cycle re-reads a PR only when the
list says it may have changed (its ``updated_at`` or head sha moved), when its CI is still
pending, or when its last read did not give a complete answer. ``updated_at`` alone is not a
reliable change signal (docs/interface.md, Automatic refresh), so every Nth cycle reads all.

The rate limits come from the ``rate_limit`` of ``prs`` and ``snapshot`` answers
(``{host, remaining, reset_at}``). A host whose ``remaining`` is below the threshold is read
less often until its ``reset_at``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import columns, safe
from .source import Row

RATE_THRESHOLD = 100  # remaining calls below which a host is backed off
BACKOFF_FACTOR = 4  # a backed-off host is read at most once per this many intervals
UNKNOWN_RESET_HOLD = timedelta(minutes=15)  # how long a low reading counts without a reset_at


def host_of(pr: str) -> str:
    """The host of a ``host/path#N`` PR id."""
    return pr.split("/", 1)[0]


def _head_sha(item: dict[str, Any]) -> Any:
    head = item.get("head")
    return head.get("sha") if isinstance(head, dict) else None


def needs_snapshot(
    row: Row, previous_item: dict[str, Any] | None, state: columns.RowState
) -> bool:
    """Whether an ordinary cycle re-reads this row."""
    if previous_item is None or state.status != columns.LOADED or state.summary is None:
        return True  # new, never loaded or failed: nothing good to keep
    if state.stale or not state.summary.complete or state.summary.ci_state == "pending":
        return True
    return row.item.get("updated_at") != previous_item.get("updated_at") or _head_sha(
        row.item
    ) != _head_sha(previous_item)


@dataclass
class _Reading:
    remaining: int
    reset_at: datetime | None
    seen_at: datetime


class RateLimits:
    """The latest ``rate_limit`` reading per host."""

    def __init__(self, threshold: int = RATE_THRESHOLD) -> None:
        self.threshold = threshold
        self._hosts: dict[str, _Reading] = {}

    def observe(self, rate_limit: Any, now: datetime) -> None:
        """Take a reading; anything malformed is ignored (the data is the forge's)."""
        if not isinstance(rate_limit, dict):
            return
        host, remaining = rate_limit.get("host"), rate_limit.get("remaining")
        if not isinstance(host, str) or not host:
            return
        if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0:
            return
        self._hosts[host] = _Reading(remaining, columns.parse_time(rate_limit.get("reset_at")), now)

    def low(self, host: str, now: datetime) -> bool:
        reading = self._hosts.get(host)
        if reading is None or reading.remaining >= self.threshold:
            return False
        if reading.reset_at is not None:
            return now < reading.reset_at
        return now < reading.seen_at + UNKNOWN_RESET_HOLD

    def text(self, now: datetime) -> str:
        """``rate: host 4000, other 12`` and, for a backed-off host, ``slow: other until 13:00:00``."""
        if not self._hosts:
            return ""
        shown = ", ".join(
            f"{safe.line(host, 60)} {reading.remaining}"
            for host, reading in sorted(self._hosts.items())
        )
        parts = [f"rate: {shown}"]
        for host, reading in sorted(self._hosts.items()):
            if self.low(host, now):
                until = f" until {reading.reset_at.strftime('%H:%M:%S')}" if reading.reset_at else ""
                parts.append(f"slow: {safe.line(host, 60)}{until}")
        return " | ".join(parts)
