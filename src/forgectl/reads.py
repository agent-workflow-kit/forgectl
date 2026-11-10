"""Read commands: dispatch to the backend of a host, and the retry rules every read shares.

``reads.py`` knows no host. A handler binds the command's PR to its backend through
``backends.create_backend`` (a table keyed by the backend name) and calls the backend's protocol
method (``backends/base.py``); everything a backend needs to be reliable comes from the
``ReadEnv`` it is built with:

- ``ReadEnv.call(fn)``: one read, retried (interface: Reliability). 429, a secondary rate limit,
  5xx, network errors and an answer that is not the documented shape are retried up to
  ``retries`` times with a backoff from 1 s doubling to 8 s, jitter adding up to a quarter,
  never shorter than the host's ``retry-after``, all inside the command's budget. A wait that
  would leave less than ``MIN_LEFT`` of the budget ends the read at once (with ``retry_after``
  set when the host gave one). Only reads are retried, so a backend never passes a write to it.
- ``ReadEnv.until_known(fetch, is_unknown, spec)``: a read whose answer holds a value the host
  is still computing (GitHub ``mergeStateStatus: UNKNOWN``, GitLab ``checking``) is read again a
  few times (``SHOW_READINESS``: 3 tries in 4 s; ``MERGE_READINESS``: 5 in 10 s; ``prs`` none).
  ``retries: 0`` turns off both kinds of retry. A value still unknown after the last try, or
  whose re-read does not fit the budget or fails a retryable way, is reported by the caller as
  ``mergeability_unknown``.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from . import registry
from .backends import create_backend
from .backends.base import Backend
from .errors import ForgectlError
from .gateway import Gateway
from .hosts import Hosts
from .identifiers import PrRef
from .model import Readiness, Warn

T = TypeVar("T")

#: Backoff for a retried read: ``BACKOFF_FIRST`` seconds, doubling, at most ``BACKOFF_CAP``.
BACKOFF_FIRST = 1.0
BACKOFF_CAP = 8.0
#: A wait must leave at least this much of the budget for the call after it.
MIN_LEFT = 1.0
#: What a retry covers: 429 and secondary limits (``rate_limited``), 5xx and a host answer that
#: is not the documented shape (``upstream_error``), no status (``network_error``). The
#: gateway gives every other answer (401, 403, 404, other 4xx, 3xx) a code that is never retried.
RETRYABLE_CODES = frozenset({"rate_limited", "upstream_error", "network_error"})


def _ignore(_message: str) -> None:
    # TODO(#9): write a `warn` line to the log (jsonlog.py)
    return None


SHOW_READINESS = Readiness(tries=3, window=4.0)  # `show` and `snapshot`
MERGE_READINESS = Readiness(tries=5, window=10.0)  # `merge`
NO_READINESS = Readiness(tries=1, window=0.0)  # `prs`


@dataclass
class ReadEnv:
    """What a backend is built with: the gateway, the hosts, and the retry rules of one command.

    ``sleep``, ``clock`` and ``jitter`` are replaced by tests. ``jitter()`` is in ``[0, 1)``.
    """

    gateway: Gateway
    hosts: Hosts
    retries: int
    warn: Warn = _ignore
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    jitter: Callable[[], float] = field(default=random.random)

    def _wait(self, seconds: float) -> None:
        # the wait is part of the command's budget, so retrying never overruns it
        with self.gateway.budget.spending():
            self.sleep(seconds)

    def wait(self, seconds: float) -> None:
        """Wait inside the command's budget, as a retry does (a caller polling a verification)."""
        self._wait(seconds)

    def backoff(self, attempt: int) -> float:
        """The wait before retry number ``attempt`` (0 is the first): the table value plus up to
        a quarter of it by the jitter, so it is never shorter than the table value."""
        base = min(BACKOFF_CAP, BACKOFF_FIRST * (2**attempt))
        return base * (1.0 + 0.25 * self.jitter())

    def _fits(self, seconds: float) -> bool:
        return seconds + MIN_LEFT <= self.gateway.budget.remaining()

    def call(self, fn: Callable[[], T]) -> T:
        """Run one read, retrying a retryable failure up to ``retries`` times."""
        attempt = 0
        while True:
            try:
                return fn()
            except ForgectlError as error:
                if error.code not in RETRYABLE_CODES or attempt >= self.retries:
                    raise
                delay = self.backoff(attempt)
                if error.retry_after is not None:
                    delay = max(delay, error.retry_after)
                if not self._fits(delay):
                    raise  # waiting does not fit the budget: exit at once, retry_after kept
                self._wait(delay)
                attempt += 1

    def until_known(
        self, fetch: Callable[[], T], is_unknown: Callable[[T], bool], spec: Readiness
    ) -> tuple[T, bool]:
        """Read with ``fetch`` until ``is_unknown`` is false or the tries or window of ``spec``
        run out. ``fetch`` retries its own calls through ``call``; this never retries one, so a
        failure is not retried twice over. Returns the last answer and whether it is still
        unknown. ``retries: 0`` makes it one try."""
        tries = spec.tries if self.retries > 0 else 1
        started = self.clock()
        value = fetch()
        for _ in range(tries - 1):
            if not is_unknown(value):
                return value, False
            elapsed = self.clock() - started
            if elapsed + spec.pause > spec.window or not self._fits(spec.pause):
                return value, True
            self._wait(spec.pause)
            try:
                value = fetch()
            except ForgectlError as error:
                if error.code not in RETRYABLE_CODES:
                    raise
                return value, True  # the re-read failed or ran out of budget: still unknown
        return value, is_unknown(value)


def make_env(inv: registry.Invocation, **overrides: Any) -> ReadEnv:
    """The environment of one command: its own gateway, under the command's timeout."""
    gateway = Gateway(inv.config, timeout=inv.timeout)
    return ReadEnv(
        gateway=gateway, hosts=Hosts(gateway), retries=inv.config.config.retries, **overrides
    )


def backend_for(ref: PrRef, env: ReadEnv) -> Backend:
    """The backend of the host a bound PR names."""
    return create_backend(ref.backend, env)


@registry.register("show")
def show(inv: registry.Invocation) -> dict[str, Any]:
    ref = inv.args.pr
    if not isinstance(ref, PrRef):  # bind_args binds it before any handler runs
        raise ForgectlError("internal", "show: the pull request was not bound to a host")
    read = backend_for(ref, make_env(inv)).get_pr(ref)
    return read.pr.to_dict()


def _bound(inv: registry.Invocation, command: str) -> PrRef:
    ref = inv.args.pr
    if not isinstance(ref, PrRef):  # bind_args binds it before any handler runs
        raise ForgectlError("internal", f"{command}: the pull request was not bound to a host")
    return ref


@registry.register("files")
def files(inv: registry.Invocation) -> dict[str, Any]:
    ref = _bound(inv, "files")
    return backend_for(ref, make_env(inv)).files(ref).to_dict()


@registry.register("diff")
def diff(inv: registry.Invocation) -> dict[str, Any]:
    ref = _bound(inv, "diff")
    return backend_for(ref, make_env(inv)).diff(ref).to_dict()


@registry.register("compare")
def compare(inv: registry.Invocation) -> dict[str, Any]:
    ref = _bound(inv, "compare")
    result = backend_for(ref, make_env(inv)).compare(ref, inv.args.from_sha, inv.args.to_sha)
    return result.to_dict()
