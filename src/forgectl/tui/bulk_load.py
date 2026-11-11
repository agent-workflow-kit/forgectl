"""The first load of `browse` through `snapshots` (#116): one bulk batch per host at a time, the
hosts in parallel, a row painted as soon as its batch arrives.

This is the fast path; the per-row ``snapshot`` pool of ``app.py`` stays the fallback and runs
for every row this module does not settle: a batch that failed (any exception, a ``ForgectlError``
or bad data), a row the batch reports in ``failed``, an item that is not complete or cannot be
summarized, and every row after a host's rate limit ran out. Nothing here ever raises into the
app, and a row is never dropped: what is not returned as settled is returned for the pool.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from forgectl.errors import ForgectlError

from . import columns
from .refresh import host_of

#: PRs asked in one ``snapshots`` call. A GitHub page holds 25 (more risks a 502); a larger GitLab
#: page is possible, but a call per 25 keeps the first rows arriving early on either host.
BATCH = 25


def _batches(prs: list[str]) -> list[list[str]]:
    return [prs[i : i + BATCH] for i in range(0, len(prs), BATCH)]


async def run(app: Any, token: int, order: list[str]) -> list[str]:
    """Load ``order`` in bulk and return the PRs the per-row pool still has to read, in the
    order given. ``app`` is the ``BrowseApp`` (its ``source``, ``_snap_token``, ``_rates``,
    ``_snap_done``, ``clock`` and ``_snapshot_arrived``)."""
    from .app import in_thread  # a late import: app.py imports this module

    reader = getattr(app.source, "snapshots", None)
    if reader is None or not order:
        return order
    settled: set[str] = set()
    by_host: dict[str, list[str]] = {}
    for pr in order:
        by_host.setdefault(host_of(pr), []).append(pr)

    def cancelled() -> bool:
        return token != app._snap_token

    def take(batch: list[str], answer: Any) -> None:
        items = answer.get("items") if isinstance(answer, dict) else None
        for snap in items if isinstance(items, list) else []:
            pr = snap.get("pr") if isinstance(snap, dict) else None
            if not isinstance(pr, str) or pr not in batch or pr in settled:
                continue
            try:
                state = columns.RowState(columns.LOADED, columns.summarize(snap))
                app._rates.observe(snap.get("rate_limit"), app.clock())
            except Exception:  # bad data: this row is left to the per-row pool
                continue
            app.adopt_item(pr, snap)
            app._keep_snapshot(pr, snap)
            app._snapshot_arrived(pr, state)
            if snap.get("complete") is True:
                settled.add(pr)
                app._snap_done += 1
            # an incomplete item is shown, and read again by the pool

    async def lane(prs: list[str]) -> None:
        for batch in _batches(prs):
            if cancelled():
                return
            try:
                answer = await in_thread(_ask, reader, batch, cancelled)
            except Exception:  # any failure of a batch: its rows go to the pool, the host goes on
                continue
            if token != app._snap_token:
                return
            take(batch, answer)
            errors = answer.get("errors") if isinstance(answer, dict) else None
            if isinstance(errors, list) and errors:
                return  # the host stopped (rate limit, credential): the pool reads the rest

    await asyncio.gather(*(lane(prs) for prs in by_host.values()), return_exceptions=True)
    return [pr for pr in order if pr not in settled]


def _takes_cancelled(reader: Any) -> bool:
    """Whether ``reader`` takes ``cancelled`` (the signature decides, as for ``snapshot`` in
    ``app.py``): a TypeError raised inside a read must never make it run a second time."""
    try:
        return "cancelled" in inspect.signature(reader).parameters
    except (TypeError, ValueError):
        return False


def _ask(reader: Any, batch: list[str], cancelled: Any) -> dict[str, Any]:
    answer = reader(batch, cancelled=cancelled) if _takes_cancelled(reader) else reader(batch)
    if not isinstance(answer, dict):
        raise ForgectlError("internal", "snapshots: the source gave no object")
    return answer
