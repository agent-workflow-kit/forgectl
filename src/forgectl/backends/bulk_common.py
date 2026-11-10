"""What the bulk readers (``github_bulk``, ``gitlab_bulk``) hand to ``forgectl.bulk`` (#116).

A bulk reader reads many PRs of one host through a few GraphQL requests and answers a
``BulkRead``: the snapshots it could build whole, and for every other PR why not. It never falls
back by itself: ``forgectl.bulk`` decides whether an unresolved PR is read by the per-PR
``snapshot`` (the default) or reported (``--bulk-only``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .. import model as m
from ..errors import ForgectlError
from ..identifiers import PrRef

#: ``Unresolved.code`` for a PR the bulk reader could not read whole (a cut part, a missing
#: field, a failed page); any other code is a real ``ForgectlError`` code (``not_found``, ...).
BULK_UNAVAILABLE = "bulk_unavailable"

#: Failures that end a host's run: another request would meet the same answer, or would make a
#: rate limit worse. The PRs not yet read are reported with the code.
HOST_STOPS = frozenset(
    {
        "rate_limited",
        "auth_required",
        "wrong_host_response",
        "tool_missing",
        "config_invalid",
    }
)

#: ``network_error`` (a reset, a timeout, an unreadable answer) is one failed request: the page is
#: halved and asked again and a PR it cannot read falls back to the per-PR path. It ends a host's
#: run only once the command's time budget is spent or after this many failures in a row.
MAX_NETWORK_FAILURES = 3


def ends_run(error: ForgectlError, exhausted: Callable[[], bool]) -> bool:
    """Whether ``error`` ends a host's run: a ``HOST_STOPS`` code, or a ``network_error`` once
    the command's time budget (``exhausted()``) is spent."""
    return error.code in HOST_STOPS or (error.code == "network_error" and exhausted())


class Unresolved(Exception):
    """One PR the bulk reader cannot give whole: why, in a code and a one-line message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class BulkRead:
    """The outcome of one host's bulk run."""

    snapshots: dict[PrRef, m.Snapshot] = field(default_factory=dict)
    unresolved: dict[PrRef, Unresolved] = field(default_factory=dict)
    #: set when the host's run was ended by ``HOST_STOPS``; the PRs after it are in ``unresolved``
    stopped: ForgectlError | None = None
    #: why the whole host uses the per-PR path (a failed capability probe), logged with --verbose
    reason: str | None = None


def chunked[T](items: Sequence[T], size: int) -> list[list[T]]:
    size = max(1, size)
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def run_pages(
    refs: Sequence[PrRef],
    size: int,
    ask: Callable[[list[PrRef], BulkRead], None],
    *,
    min_size: int = 1,
    shrink: Callable[[int], int] = lambda s: max(1, s // 2),
    exhausted: Callable[[], bool] = lambda: False,
) -> BulkRead:
    """Ask for ``refs`` in pages of ``size``; ``ask(page, result)`` fills ``result`` or raises.

    A page that fails (``ForgectlError`` that does not end the run) is split in two halves, each
    asked once more; a half that fails too is unresolved (``bulk_unavailable``) and the size of
    the pages after it is the half. A page of one PR is asked twice before it is given up. A
    failure that ends the run (``ends_run``, or ``MAX_NETWORK_FAILURES`` ``network_error`` in a
    row) leaves this page and every later PR unresolved with that code and sets
    ``result.stopped``.
    """
    result = BulkRead()
    pending = chunked(list(refs), size)
    current = size
    index = 0
    stopped: ForgectlError | None = None
    network_failures = 0

    def attempt(page: list[PrRef]) -> ForgectlError | None:
        nonlocal stopped, network_failures
        try:
            ask(page, result)
        except ForgectlError as error:
            network_failures = network_failures + 1 if error.code == "network_error" else 0
            if ends_run(error, exhausted) or network_failures >= MAX_NETWORK_FAILURES:
                stopped = error
                give_up_stop(result, page, error)
            return error
        network_failures = 0
        return None

    def give_up(page: list[PrRef], error: ForgectlError) -> None:
        for ref in page:
            result.unresolved.setdefault(ref, Unresolved(BULK_UNAVAILABLE, _line(error)))

    while index < len(pending) and stopped is None:
        page = pending[index]
        index += 1
        failure = attempt(page)
        if failure is None or stopped is not None:
            continue
        # the page failed: halve and ask each half once more
        current = max(min_size, shrink(min(current, len(page))))
        if len(page) == 1:
            again = attempt(page)
            if again is not None and stopped is None:
                give_up(page, again)
            continue
        middle = (len(page) + 1) // 2
        for half in (page[:middle], page[middle:]):
            if stopped is not None:
                give_up_stop(result, half, stopped)
                continue
            again = attempt(half)
            if again is not None and stopped is None:
                give_up(half, again if again.code != "internal" else failure)
        # later pages use the smaller size
        if current < size:
            rest = [ref for later in pending[index:] for ref in later]
            pending = pending[:index] + chunked(rest, current)
    if stopped is not None:
        for later in pending[index:]:
            give_up_stop(result, later, stopped)
        result.stopped = stopped
    return result


def give_up_stop(result: BulkRead, page: Sequence[PrRef], error: ForgectlError) -> None:
    for ref in page:
        if ref not in result.snapshots:
            result.unresolved[ref] = Unresolved(error.code, _line(error))


def _line(error: ForgectlError) -> str:
    return " ".join(str(error.message).split())[:300]
