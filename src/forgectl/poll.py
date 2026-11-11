"""``poll``: the light read of ``browse``'s refresh (#118).

For every host in scope, the open PRs the account authored or was asked to review, each with only
the tuple that tells whether it changed since the last poll (head sha, update time, CI state,
review decision, and on GitLab approvals left, approval and merge status, note count). It costs
about one request per role on GitHub and one for both roles on GitLab, and never reads a
snapshot. The caller re-reads only the PRs whose tuple changed (``snapshots``).

Scope and failure rules are those of ``prs`` over every host (``fanout``): a failed host is an
``errors`` entry and makes the list incomplete; with one host asked, its own error is raised. A
host that cannot be polled lightly (an older GitLab) is listed in ``unsupported`` and reads
nothing; a host whose list was cut is listed in ``truncated``; the caller then lists with ``prs``.
``hidden_repos`` are dropped.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any, Protocol

from . import registry
from .activity import ignored as matches_any
from .backends.light_common import LightItem, LightPage, LightUnsupported
from .errors import ForgectlError
from .fanout import (
    candidate_hosts,
    entry_error,
    host_error,
    host_scope,
    instant,
    lowest_rate_limit,
)
from .hosts import Identity
from .reads import ReadEnv, make_env

LIGHT_READERS: dict[str, str] = {
    "github": "forgectl.backends.github_light",
    "gitlab": "forgectl.backends.gitlab_light",
}


class LightReader(Protocol):
    def read_light(
        self,
        env: ReadEnv,
        host: str,
        known: Identity | None,
        skip: Callable[[str], bool] | None = None,
    ) -> LightPage: ...


def _reader(backend: str) -> LightReader:
    module = LIGHT_READERS.get(backend)
    if module is None:
        raise ForgectlError("internal", f"the {backend} backend has no light reader")
    reader: LightReader = importlib.import_module(module)  # type: ignore[assignment]
    return reader


def _sorted(items: list[LightItem]) -> list[LightItem]:
    """``updated_at`` descending (as instants), then ``pr`` ascending, as ``prs`` lists."""
    by_pr = sorted(items, key=lambda item: item.pr)
    return sorted(by_pr, key=lambda item: instant(item.updated_at), reverse=True)


def run_poll(env: ReadEnv) -> dict[str, Any]:
    config = env.gateway.config.config
    scope = host_scope(env, candidate_hosts(config), single_target=False)
    queried = [host for host, _ in scope]
    hidden = config.hidden_repos
    items: list[LightItem] = []
    failures: list[tuple[str, ForgectlError]] = []
    unsupported: list[str] = []
    truncated: list[str] = []
    complete = True

    def skip(repo: str) -> bool:
        return bool(hidden) and matches_any(repo, hidden)

    for host, known in scope:
        try:
            if known is not None and known.error is not None:
                raise entry_error(known.error)
            reader = _reader(env.hosts.backend(host))
            page = reader.read_light(env, host, known, skip)
        except LightUnsupported:
            unsupported.append(host)
            continue
        except ForgectlError as error:
            failures.append((host, error))
            continue
        items.extend(page.items)
        if not page.complete:
            truncated.append(host)
        complete = complete and page.complete
    if failures and len(queried) == 1:
        raise failures[0][1]
    try:
        ordered = _sorted(items)
    except ForgectlError as error:  # a time that is not RFC 3339
        raise ForgectlError("upstream_error", error.message) from None
    output: dict[str, Any] = {
        "items": [item.to_dict() for item in ordered],
        "complete": complete and not failures and not unsupported,
        "errors": [host_error(host, error) for host, error in failures],
        "hosts_queried": queried,
        "unsupported": unsupported,
        "truncated": truncated,
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


@registry.register("poll")
def poll(inv: registry.Invocation) -> dict[str, Any]:
    return run_poll(make_env(inv))
