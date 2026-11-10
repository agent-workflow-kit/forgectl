"""``snapshots``: the snapshot of many PRs from a few bulk requests per host (#116).

The PRs are grouped by host and each host's bulk reader (``backends/github_bulk.py``,
``backends/gitlab_bulk.py``) reads as many as it can whole. A PR it cannot read whole is read by
the per-PR ``snapshot`` (the source of truth), unless ``--bulk-only`` asks to be told instead.
Once a host's run has ended on a rate limit, a credential failure or the spent time budget no
further request is made to it: every PR of it not yet read is reported with that code, with or
without ``--bulk-only`` (interface: ``snapshots``). A ``network_error`` alone is one failed
request: it ends the run only with the budget spent or after ``MAX_NETWORK_FAILURES`` in a row.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from . import registry
from .backends import create_backend
from .backends.bulk_common import (
    BULK_UNAVAILABLE,
    MAX_NETWORK_FAILURES,
    BulkRead,
    Unresolved,
    ends_run,
)
from .errors import ForgectlError, usage
from .fanout import host_error, lowest_rate_limit
from .hosts import redact_secrets
from .identifiers import PrRef, bind_pr, parse_pr
from .output import emit_warning
from .reads import ReadEnv, make_env
from .sanitize import sanitize_line

#: The most PRs one command reads, whatever the source of them.
MAX_REFS = 500
_MAX_STDIN = 4 * 1024 * 1024

#: The module of each backend's bulk reader (imported when a host of that backend is asked).
BULK_READERS: dict[str, str] = {
    "github": "forgectl.backends.github_bulk",
    "gitlab": "forgectl.backends.gitlab_bulk",
}


class BulkReader(Protocol):
    def read_bulk(self, env: ReadEnv, host: str, refs: Sequence[PrRef]) -> BulkRead: ...


def _line(text: str) -> str:
    return sanitize_line(redact_secrets(" ".join(text.split())))[:300]


def _stdin_refs(inv: registry.Invocation) -> list[PrRef]:
    text = sys.stdin.read(_MAX_STDIN + 1)
    if len(text) > _MAX_STDIN:
        raise usage("snapshots: the PR list on stdin is larger than 4 MiB")
    out: list[PrRef] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            out.append(bind_pr(parse_pr(line), inv.config.config))
    return out


def refs_of(inv: registry.Invocation) -> list[PrRef]:
    """The PRs named on the command line (bound by ``bind_args``) or on stdin, once each, in
    the order given."""
    given = list(inv.args.refs)
    if given == ["-"]:
        found = _stdin_refs(inv)
    elif "-" in given:
        raise usage("snapshots: `-` (stdin) cannot be combined with PRs")
    else:
        found = given
    if not all(isinstance(r, PrRef) for r in found):
        raise ForgectlError("internal", "snapshots: a pull request was not bound to a host")
    # one PR in two spellings (the host and the path are not case sensitive) is read once
    seen: dict[tuple[str, str, int], PrRef] = {}
    for found_ref in found:
        seen.setdefault(
            (found_ref.host.lower(), found_ref.project.lower(), found_ref.number), found_ref
        )
    unique = list(seen.values())
    if not unique:
        raise usage("snapshots: no pull request given")
    if len(unique) > MAX_REFS:
        raise usage(f"snapshots: at most {MAX_REFS} pull requests at a time")
    return unique


def _reader(backend: str) -> BulkReader:
    module = BULK_READERS.get(backend)
    if module is None:
        raise ForgectlError("internal", f"the {backend} backend has no bulk reader")
    reader: BulkReader = importlib.import_module(module)  # type: ignore[assignment]
    return reader


def _exhausted(env: ReadEnv) -> Callable[[], bool]:
    return lambda: env.gateway.budget.remaining() <= 0


def _bulk_host(env: ReadEnv, host: str, refs: list[PrRef]) -> BulkRead:
    """One host's bulk run. A failure of the whole run, whatever it is, is every PR of the host
    unresolved and never a crash: the other hosts' results are kept."""
    try:
        return _reader(refs[0].backend).read_bulk(env, host, refs)
    except ForgectlError as error:
        stop = ends_run(error, _exhausted(env))
        code = error.code if stop else BULK_UNAVAILABLE
        result = BulkRead(reason=_line(error.message), stopped=error if stop else None)
        for ref in refs:
            result.unresolved[ref] = Unresolved(code, _line(error.message))
        return result
    except Exception as error:  # a reader bug must not lose the other hosts' results
        text = f"unusable answer ({type(error).__name__})"
        result = BulkRead(reason=text)
        for ref in refs:
            result.unresolved[ref] = Unresolved(BULK_UNAVAILABLE, text)
        return result


def run_snapshots(
    env: ReadEnv, refs: list[PrRef], *, bulk_only: bool = False, verbose: bool = False
) -> dict[str, Any]:
    by_host: dict[str, list[PrRef]] = {}
    for ref in refs:
        by_host.setdefault(ref.host, []).append(ref)
    found: dict[PrRef, dict[str, Any]] = {}
    failed: dict[PrRef, dict[str, str]] = {}
    fallback: list[PrRef] = []
    stopped: dict[str, ForgectlError] = {}
    for host, host_refs in by_host.items():
        read = _bulk_host(env, host, host_refs)
        if read.reason and verbose:
            emit_warning(f"snapshots: {host}: {read.reason}")
        for ref, snap in read.snapshots.items():
            found[ref] = snap.to_dict()
        if read.stopped is not None:
            stopped[host] = read.stopped
        network_failures = 0
        for ref in host_refs:
            if ref in found:
                continue
            why = read.unresolved.get(ref) or Unresolved(BULK_UNAVAILABLE, "not read")
            if host in stopped and why.code == BULK_UNAVAILABLE:
                # a PR the host's run ended before it was read: the code of the stop
                why = Unresolved(stopped[host].code, stopped[host].message)
            if bulk_only or host in stopped:
                failed[ref] = {"code": why.code, "message": _line(why.message)}
                continue
            # the per-PR snapshot is the source of truth for whatever the bulk reader gave up
            fallback.append(ref)
            try:
                found[ref] = create_backend(ref.backend, env).snapshot(ref).to_dict()
                network_failures = 0
            except ForgectlError as error:
                failed[ref] = {"code": error.code, "message": _line(error.message)}
                network_failures = network_failures + 1 if error.code == "network_error" else 0
                if ends_run(error, _exhausted(env)) or network_failures >= MAX_NETWORK_FAILURES:
                    stopped[host] = error
            except Exception as error:  # a bug of the per-PR path costs this PR, not the command
                failed[ref] = {
                    "code": "internal",
                    "message": f"unusable answer ({type(error).__name__})",
                }
    items = [found[r] for r in refs if r in found]
    complete = not failed and not stopped and all(i.get("complete") is True for i in items)
    return {
        "items": items,
        "failed": [{"pr": str(r), "error": failed[r]} for r in refs if r in failed],
        "complete": complete,
        "errors": [host_error(h, e) for h, e in stopped.items()],
        "hosts_queried": list(by_host),
        "fallback": [str(r) for r in fallback],
        "rate_limit": lowest_rate_limit(env, list(by_host)).to_dict(),
    }


@registry.register("snapshots")
def snapshots(inv: registry.Invocation) -> dict[str, Any]:
    refs = refs_of(inv)
    return run_snapshots(
        make_env(inv), refs, bulk_only=bool(inv.args.bulk_only), verbose=bool(inv.args.verbose)
    )
