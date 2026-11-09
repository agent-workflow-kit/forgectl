"""Read commands: dispatch to the backend of a host, and the retry rules every read shares.

``reads.py`` knows no host. A handler binds the command's PR to its backend through
``backends.create_backend`` (a table keyed by the backend name) and calls the backend's protocol
method (``backends/base.py``); everything a backend needs to be reliable comes from the
``ReadEnv`` it is built with:

- ``ReadEnv.call(fn)``: one read, retried (interface: Reliability). 429, a secondary rate limit,
  5xx and network errors are retried up to ``retries`` times with a backoff from 1 s doubling to
  8 s, jittered, never shorter than the host's ``retry-after``, all inside the command's budget.
  A ``retry-after`` that does not fit in what is left of the budget ends the read at once with
  ``retry_after`` set. Only reads are retried, so a backend never passes a write to it.
- ``ReadEnv.until_known(fetch, is_unknown, spec)``: a read whose answer holds a value the host
  is still computing (GitHub ``mergeStateStatus: UNKNOWN``, GitLab ``checking``) is read again a
  few times (``SHOW_READINESS``: 3 tries in 4 s; ``MERGE_READINESS``: 5 in 10 s; ``prs`` none).
  ``retries: 0`` turns off both kinds of retry. A value still unknown after the last try is
  reported by the caller as ``mergeability_unknown``.
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
from .model import Warn

T = TypeVar("T")

#: Backoff for a retried read: ``BACKOFF_FIRST`` seconds, doubling, at most ``BACKOFF_CAP``.
BACKOFF_FIRST = 1.0
BACKOFF_CAP = 8.0
#: What a retry covers: 429 and secondary limits (``rate_limited``), 5xx and a host answer that
#: is not the documented shape (``upstream_error``), no status (``network_error``). The
#: gateway gives every other answer (401, 403, 404, other 4xx, 3xx) a code that is never retried.
RETRYABLE_CODES = frozenset({"rate_limited", "upstream_error", "network_error"})


def _ignore(_message: str) -> None:
    # TODO(#9): write a `warn` line to the log (jsonlog.py)
    return None


@dataclass(frozen=True)
class Readiness:
    """How long a read may be repeated while a value is still being computed."""

    tries: int
    window: float  # seconds, from the first try

    @property
    def pause(self) -> float:
        return self.window / self.tries


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

    def backoff(self, attempt: int) -> float:
        """The wait before retry number ``attempt`` (0 is the first): the table value, scaled
        into its upper half by the jitter, so it is never shorter than half the table value."""
        base = min(BACKOFF_CAP, BACKOFF_FIRST * (2**attempt))
        return base * (0.5 + 0.5 * self.jitter())

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
                    if error.retry_after > self.gateway.budget.remaining():
                        raise  # waiting does not fit the budget: exit at once, retry_after set
                    delay = max(delay, error.retry_after)
                if delay > self.gateway.budget.remaining():
                    raise
                self._wait(delay)
                attempt += 1

    def until_known(
        self, fetch: Callable[[], T], is_unknown: Callable[[T], bool], spec: Readiness
    ) -> tuple[T, bool]:
        """Read with ``fetch`` (each read itself retried) until ``is_unknown`` is false or the
        tries or window of ``spec`` run out. Returns the last answer and whether it is still
        unknown. ``retries: 0`` makes it one try."""
        tries = spec.tries if self.retries > 0 else 1
        started = self.clock()
        value = self.call(fetch)
        for _ in range(tries - 1):
            if not is_unknown(value):
                return value, False
            elapsed = self.clock() - started
            if elapsed + spec.pause > spec.window or spec.pause > self.gateway.budget.remaining():
                return value, True
            self._wait(spec.pause)
            value = self.call(fetch)
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
