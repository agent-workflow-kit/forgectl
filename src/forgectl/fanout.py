"""Reads that span hosts: ``prs`` over every host in scope, with partial results (interface:
``prs``, ``whoami``). Generic over backends: nothing here knows GitHub, so the GitLab adapter
(#19) reuses it unchanged.

The contract a backend meets:

- ``Backend.list_prs(query)`` gets a ``PrQuery`` for exactly one host (``query.host`` set, ``me``
  already resolved to that host's login, ``updated_since`` already an absolute UTC time
  ``YYYY-MM-DDTHH:MM:SSZ``), returns at most ``query.limit`` items newest first, and sets
  ``complete`` false when older items were cut or the host capped the list. It raises a
  ``ForgectlError`` for a failed read; it never returns a partial list as complete.
- This module asks hosts one after the other (one budget, one credential selection; ``prs
  --query`` gives the snapshots it reads a second budget, D-43), keeps each host's failure as
  ``{host, code, message}`` (message redacted of tokens, one line), merges the items
  (``updated_at`` descending, then ``pr``), cuts to ``limit`` and sets ``complete`` (false when
  any host failed). ``rate_limit`` is the lowest ``remaining`` in the headers of every answer
  the command received from a host in ``hosts_queried`` (``Gateway.rate_readings``), whatever
  the call and whether it failed; a skipped host's probe does not count.
- A failure with several hosts asked is ``ForgectlError("partial_results", output=...)``: the
  normal output plus ``errors``, printed by ``output.emit_error`` with exit 12. With one target
  (``--repo``, ``--host``, or a single host asked) the host's own error is raised instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from . import model as m
from .activity import ignored as matches_any
from .backends import create_backend
from .config import BUILT_IN_HOSTS, Config
from .errors import EXIT_BY_CODE, REQUIRED_DETAILS_BY_CODE, ForgectlError
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
    """``--updated-since`` as an absolute UTC time to the second, the year zero-padded: an RFC
    3339 time is converted, a duration is subtracted from ``now``. A time that falls outside the
    years 1 to 9999 once in UTC is ``usage``."""
    try:
        if _RFC3339.fullmatch(value):
            moment = datetime.fromisoformat(value.upper())
        else:
            moment = (now or datetime.now(UTC)) - timedelta(seconds=duration(value))
        moment = moment.astimezone(UTC)
    except (OverflowError, ValueError):
        raise ForgectlError(
            "usage", f"--updated-since is outside the years 1 to 9999 in UTC: {value!r}"
        ) from None
    return f"{moment.year:04d}-{moment:%m-%dT%H:%M:%S}Z"


def check_me_spelling(value: str | None, what: str) -> None:
    """``me`` is reserved; a login equal to it in another case is refused (logins are
    case-insensitive on both hosts), so ``ME`` never searches an account named ``me``."""
    if value is not None and value != "me" and value.lower() == "me":
        raise ForgectlError("usage", f"{what}: `me` is reserved; write it as `me`")


def candidate_hosts(config: Config) -> list[str]:
    """Every built-in and declared host, sorted."""
    return sorted({*BUILT_IN_HOSTS, *config.hosts})


_PLACEHOLDER_DETAILS: dict[str, Any] = {"observed": "", "install": "", "key": "", "status": 0}


def entry_error(entry: dict[str, str]) -> ForgectlError:
    """The ``ForgectlError`` of an ``Identity.error`` entry ``{host, code, message}``. That entry
    keeps no ``details``, so a code whose envelope requires some (``wrong_host_response``,
    ``tool_missing``...) would be refused by the constructor; empty placeholders stand in, and the
    ``errors`` item of a listing never shows them."""
    code, message = entry["code"], entry["message"]
    try:
        return ForgectlError(code, message)
    except ValueError:
        if code not in EXIT_BY_CODE:
            raise
    required = REQUIRED_DETAILS_BY_CODE.get(code, ())
    return ForgectlError(
        code, message, details={key: _PLACEHOLDER_DETAILS.get(key) for key in required}
    )


def host_error(host: str, error: ForgectlError) -> dict[str, str]:
    """The ``errors`` item for a failed host: redacted of tokens, one line."""
    return {
        "host": host,
        "code": error.code,
        "message": sanitize_line(redact_secrets(error.message)),
    }


def resolve_me(env: ReadEnv, host: str, known: Identity | None = None) -> str:
    """What ``me`` is on ``host``: the configured ``me``, else the authenticated login (from
    ``known`` when an identity read was already made, else one retried read). Checked under the
    host backend's grammar."""
    backend = env.hosts.backend(host)
    value = env.hosts.me(host)
    if value is None and known is not None:
        value = known.login
    if value is None:
        target = env.hosts.target(host)
        value = env.call(lambda: env.hosts.authenticated_user(target)).login
    return bind_login(value, backend)


def _login_for(env: ReadEnv, host: str, value: str | None, known: Identity | None) -> str | None:
    if value is None:
        return None
    if value == "me":
        return resolve_me(env, host, known)
    return bind_login(value, env.hosts.backend(host))


def lowest_rate_limit(env: ReadEnv, queried: list[str]) -> m.RateLimit:
    """The lowest ``remaining`` in the headers of every answer this command received from a
    host in ``queried``, success or failure (a skipped host's probe does not count). With none
    known: that host's nulls when one host was asked, else null for every field."""
    readings = [r for r in env.gateway.rate_readings if r.host in queried]
    reading = min(readings, key=lambda r: r.remaining) if readings else None
    if reading is None:
        single = queried[0] if len(queried) == 1 else None
        return m.RateLimit(host=single, remaining=None, reset_at=None)
    return m.RateLimit(host=reading.host, remaining=reading.remaining, reset_at=reading.reset_at)


def _check_logins(env: ReadEnv, host: str, args: PrsArgs) -> None:
    backend = env.hosts.backend(host)
    for value in (args.author, args.review_requested_from):
        if value is not None and value != "me":
            bind_login(value, backend)


MAX_LIMIT = 1000  # `--limit` and both backends' search cap


def list_visible(backend: Any, query: m.PrQuery, hidden: tuple[str, ...]) -> m.PrPage:
    """``backend.list_prs`` without the PRs of ``config.hidden_repos``, which are dropped before
    the limit counts. The backend is given a skip predicate and applies it while paging, so a
    hidden PR costs no further request and never takes a place among the ``limit + 1`` visible
    items it fills; the filter here is a safety net. The ask is ``limit + 1`` (at most
    ``MAX_LIMIT``), so a hidden item past the limit cannot make the list incomplete, and there is
    never a second ask. A query for one hidden repo is an empty complete page without a call."""
    if not hidden:
        return backend.list_prs(query)
    if query.repo is not None and matches_any(query.repo.id, hidden):
        return m.PrPage(
            items=(),
            complete=True,
            rate_limit=m.RateLimit(host=query.host, remaining=None, reset_at=None),
        )

    def skip(repo: str) -> bool:
        return matches_any(repo, hidden)

    ask = min(query.limit + 1, MAX_LIMIT)
    page = backend.list_prs(replace(query, limit=ask), skip=skip)
    kept = tuple(i for i in page.items if not skip(i.repo))
    # more kept items than the limit: the caller's merge cuts them, so the list is not whole
    return m.PrPage(
        items=kept,
        complete=page.complete and len(kept) <= query.limit,
        rate_limit=page.rate_limit,
    )


def is_probed(config: Config, host: str, single_target: bool) -> bool:
    """An undeclared built-in host that no one named: asked only when the account can be told to
    exist there."""
    return not single_target and host in BUILT_IN_HOSTS and host not in config.hosts


def host_scope(
    env: ReadEnv,
    hosts: list[str],
    *,
    single_target: bool,
    check: Callable[[str], None] | None = None,
) -> list[tuple[str, Identity | None]]:
    """The hosts to ask, with the identity read for a probed one. A probed host with no
    credential is left out; ``check(host)`` runs for a probed host once it is in scope."""
    config = env.gateway.config.config
    scope: list[tuple[str, Identity | None]] = []
    for host in hosts:
        known: Identity | None = None
        if is_probed(config, host, single_target):
            known = env.hosts.identity(host, call=env.call)
            if known.error is None and known.token_source == "none":
                continue
            if check is not None:
                check(host)
        scope.append((host, known))
    return scope


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
    check_me_spelling(args.author, "--author")
    check_me_spelling(args.review_requested_from, "--review-requested-from")
    if args.repo is not None:
        hosts = [args.repo.host]
    elif args.host is not None:
        hosts = [args.host]
    else:
        hosts = candidate_hosts(config)
    single_target = args.repo is not None or args.host is not None

    def probed(host: str) -> bool:
        return is_probed(config, host, single_target)

    # A login that does not fit a declared or named host's backend is refused (exit 2) before
    # any call; an undeclared built-in is checked only once its probe puts it in scope.
    for host in hosts:
        if not probed(host):
            _check_logins(env, host, args)
    since = None if args.updated_since is None else resolve_since(args.updated_since, now)

    scope = host_scope(
        env, hosts, single_target=single_target, check=lambda host: _check_logins(env, host, args)
    )

    queried = [host for host, _ in scope]
    failures: list[tuple[str, ForgectlError]] = []
    pages: list[tuple[str, m.PrPage]] = []
    for host, known in scope:
        try:
            if known is not None and known.error is not None:
                raise entry_error(known.error)
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
            backend = create_backend(env.hosts.backend(host), env)
            pages.append((host, list_visible(backend, query, config.hidden_repos)))
        except ForgectlError as error:
            failures.append((host, error))
    if failures and len(queried) == 1:
        raise failures[0][1]  # one target: the ordinary exit and envelope

    merged = sort_items(item for _, page in pages for item in page.items)
    cut = len(merged) > args.limit
    output = {
        "items": [item.to_dict() for item in merged[: args.limit]],
        # a failed host's items are missing, so the list is never complete then
        "complete": not failures and all(page.complete for _, page in pages) and not cut,
        "errors": [host_error(host, error) for host, error in failures],
        "hosts_queried": queried,
        "rate_limit": lowest_rate_limit(env, queried).to_dict(),
    }
    if failures:
        shown = ", ".join(f"{host} ({error.code})" for host, error in failures)
        raise ForgectlError(
            "partial_results",
            f"{len(failures)} of {len(queried)} hosts failed: {shown}",
            output=output,
        )
    return output
