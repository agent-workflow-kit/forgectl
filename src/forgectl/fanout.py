"""Reads that span hosts: ``prs`` over every host in scope, with partial results (interface:
``prs``, ``whoami``). Generic over backends: nothing here knows GitHub, so the GitLab adapter
(#19) reuses it unchanged.

The contract a backend meets:

- ``Backend.list_prs(query)`` gets a ``PrQuery`` for exactly one host (``query.host`` set, ``me``
  already resolved to that host's login, ``updated_since`` already an absolute UTC time
  ``YYYY-MM-DDTHH:MM:SSZ``), returns at most ``query.limit`` items newest first, and sets
  ``complete`` false when older items were cut or the host capped the list. It raises a
  ``ForgectlError`` for a failed read; it never returns a partial list as complete.
- This module asks hosts one after the other (one budget, one credential selection), keeps each
  host's failure as ``{host, code, message}`` (message redacted of tokens, one line), merges the
  items (``updated_at`` descending, then ``pr``), cuts to ``limit`` and sets ``complete``.
- A failure with several hosts asked is ``ForgectlError("partial_results", output=...)``: the
  normal output plus ``errors``, printed by ``output.emit_error`` with exit 12. With one target
  (``--repo``, ``--host``, or a single host asked) the host's own error is raised instead.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import model as m
from .backends import create_backend
from .config import BUILT_IN_HOSTS, Config
from .errors import ForgectlError
from .hosts import Identity, redact_secrets
from .identifiers import bind_login
from .reads import ReadEnv
from .sanitize import sanitize_line
from .surface import duration

_RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})",
    re.ASCII,
)


def instant(stamp: str) -> datetime:
    """A timestamp of the documented RFC 3339 form as an aware datetime (comparable across
    offsets); anything else is ``upstream_error``."""
    try:
        value = datetime.fromisoformat(stamp.upper())
    except ValueError:
        raise ForgectlError("upstream_error", "the host gave a time that is not RFC 3339") from None
    if value.tzinfo is None:
        raise ForgectlError("upstream_error", "the host gave a time with no offset")
    return value


def sort_items(items: Iterable[m.PrListItem]) -> list[m.PrListItem]:
    """``updated_at`` descending (as instants, whatever the offset), then ``pr`` ascending."""
    by_pr = sorted(items, key=lambda item: item.pr)
    return sorted(by_pr, key=lambda item: instant(item.updated_at), reverse=True)


def resolve_since(value: str, now: datetime | None = None) -> str:
    """``--updated-since`` as an absolute UTC time to the second: an RFC 3339 time is converted,
    a duration is subtracted from ``now``."""
    if _RFC3339.fullmatch(value):
        moment = datetime.fromisoformat(value.upper())
    else:
        moment = (now or datetime.now(UTC)) - timedelta(seconds=duration(value))
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def candidate_hosts(config: Config) -> list[str]:
    """Every built-in and declared host, sorted."""
    return sorted({*BUILT_IN_HOSTS, *config.hosts})


def host_error(host: str, error: ForgectlError) -> dict[str, str]:
    """The ``errors`` item for a failed host: redacted of tokens, one line."""
    return {
        "host": host,
        "code": error.code,
        "message": sanitize_line(redact_secrets(error.message)),
    }


def resolve_me(env: ReadEnv, host: str, known: Identity | None = None) -> str:
    """What ``me`` is on ``host``: the configured ``me``, else the authenticated login (from
    ``known`` when an identity read was already made). Checked under the host backend's grammar."""
    backend = env.hosts.backend(host)
    value = env.hosts.me(host)
    if value is None and known is not None:
        value = known.login
    if value is None:
        value = env.hosts.authenticated_user(env.hosts.target(host)).login
    return bind_login(value, backend)


def _login_for(env: ReadEnv, host: str, value: str | None, known: Identity | None) -> str | None:
    if value is None:
        return None
    if value == "me":
        return resolve_me(env, host, known)
    return bind_login(value, env.hosts.backend(host))


def lowest_rate_limit(pages: Sequence[tuple[str, m.RateLimit]], single: str | None) -> m.RateLimit:
    """The lowest ``remaining`` among the hosts called. With none known: that host's nulls when
    one host was called, else null for every field."""
    known = [(rl.remaining, rl) for _, rl in pages if rl.remaining is not None]
    if known:
        return min(known, key=lambda pair: pair[0])[1]
    return m.RateLimit(host=single, remaining=None, reset_at=None)


@dataclass(frozen=True)
class PrsArgs:
    repo: Any  # a bound RepoRef or None
    host: str | None
    author: str | None
    review_requested_from: str | None
    state: Any
    draft: bool | None
    updated_since: str | None
    limit: int


def run_prs(env: ReadEnv, args: PrsArgs, *, now: datetime | None = None) -> dict[str, Any]:
    """``prs`` over the hosts in scope (see the module doc for the failure rules)."""
    config = env.gateway.config.config
    if args.repo is not None:
        hosts = [args.repo.host]
    elif args.host is not None:
        hosts = [args.host]
    else:
        hosts = candidate_hosts(config)
    single_target = args.repo is not None or args.host is not None
    # A login that does not fit a host's backend is refused up front (exit 2), before any call.
    for host in hosts:
        backend = env.hosts.backend(host)
        for value in (args.author, args.review_requested_from):
            if value is not None and value != "me":
                bind_login(value, backend)
    since = None if args.updated_since is None else resolve_since(args.updated_since, now)

    queried: list[str] = []
    failures: list[tuple[str, ForgectlError]] = []
    pages: list[tuple[str, m.PrPage]] = []
    for host in hosts:
        known: Identity | None = None
        if not single_target and host in BUILT_IN_HOSTS and host not in config.hosts:
            # an undeclared built-in is asked only when the account can be told to exist
            known = env.hosts.identity(host)
            if known.error is None and known.token_source == "none":
                continue
        queried.append(host)
        try:
            if known is not None and known.error is not None:
                raise ForgectlError(known.error["code"], known.error["message"])
            query = m.PrQuery(
                repo=args.repo,
                host=host,
                author=_login_for(env, host, args.author, known),
                review_requested_from=_login_for(env, host, args.review_requested_from, known),
                state=args.state,
                draft=args.draft,
                updated_since=since,
                limit=args.limit,
            )
            pages.append((host, create_backend(env.hosts.backend(host), env).list_prs(query)))
        except ForgectlError as error:
            failures.append((host, error))
    if failures and len(queried) == 1:
        raise failures[0][1]  # one target: the ordinary exit and envelope

    merged = sort_items(item for _, page in pages for item in page.items)
    cut = len(merged) > args.limit
    output = {
        "items": [item.to_dict() for item in merged[: args.limit]],
        "complete": all(page.complete for _, page in pages) and not cut,
        "errors": [host_error(host, error) for host, error in failures],
        "hosts_queried": queried,
        "rate_limit": lowest_rate_limit(
            [(host, page.rate_limit) for host, page in pages],
            queried[0] if len(queried) == 1 else None,
        ).to_dict(),
    }
    if failures:
        shown = ", ".join(f"{host} ({error.code})" for host, error in failures)
        raise ForgectlError(
            "partial_results",
            f"{len(failures)} of {len(queried)} hosts failed: {shown}",
            output=output,
        )
    return output
