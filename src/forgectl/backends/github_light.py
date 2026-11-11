"""GitHub light poll (#118): the change-detection tuple of every open PR the account authored or
was asked to review, from one search per role.

The search strings are the ones ``prs`` builds (``github_lists.build_search``), so a configured
``me`` and the account's own login behave as in ``prs``. The query selects only ``id number url
title isDraft updatedAt headRefOid reviewDecision`` and ``statusCheckRollup { state }`` of the
last commit; ``mergeStateStatus`` is deliberately absent (it makes a page 9 to 12 s slow). A list
that does not end within ``MAX_PAGES`` pages, or has fewer results than ``issueCount``, is
reported incomplete, and the caller reads the list with ``prs`` instead.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from .. import model as m
from ..fanout import resolve_me
from ..gateway import Target, verify_identity
from ..hosts import Identity
from ..reads import ReadEnv
from ..sanitize import sanitize_line
from .github import (
    _ACTOR_FIELDS,
    _NODE_ID,
    GitHubBackend,
    Malformed,
    _cursor,
    _list,
    _obj,
    _opt_obj,
    _opt_str,
    _project,
    _req_sha,
    _req_ts,
    _str,
    parse_actor,
)
from .github_lists import build_search
from .light_common import LightItem, LightPage

PAGE_SIZE = 100
MAX_PAGES = 5

LIGHT_QUERY = (
    """
query ForgectlLight($q: String!, $first: Int!, $after: String) {
  search(query: $q, type: ISSUE, first: $first, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      __typename
      ... on PullRequest {
        id
        number
        url
        title
        isDraft
        updatedAt
        headRefOid
        reviewDecision
        author { ...ActorFields }
        repository { nameWithOwner }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
  }
}
"""
    + _ACTOR_FIELDS
)


def _state(node: dict[str, Any]) -> str | None:
    commits = _opt_obj(node.get("commits"), "the commits")
    nodes = _list(commits.get("nodes") if commits else [], "the commits")
    if not nodes:
        return None
    commit = _opt_obj(_obj(nodes[0], "a commit").get("commit"), "the commit")
    rollup = _opt_obj(commit.get("statusCheckRollup") if commit else None, "the rollup")
    return _opt_str(rollup.get("state")) if rollup else None


def _item(target: Target, node: dict[str, Any], role: str) -> LightItem:
    number = node.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise Malformed("the number")
    node_id = _str(node.get("id"), "the id")
    if not _NODE_ID.fullmatch(node_id):
        raise Malformed("the id")
    repository = _obj(node.get("repository"), "the repository")
    project = _project(repository.get("nameWithOwner"), "the repository path")
    verify_identity(
        _str(node.get("url"), "the url"), host=target.host, project=project, secret=target.token
    )
    draft = node.get("isDraft")
    if not isinstance(draft, bool):
        raise Malformed("the draft flag")
    actor = parse_actor(node.get("author"))
    return LightItem(
        pr=f"{target.host}/{project}#{number}",
        pr_id=node_id,
        repo=f"{target.host}/{project}",
        number=number,
        url=f"https://{target.host}/{project}/pull/{number}",
        title=sanitize_line(_str(node.get("title"), "the title")),
        author=None if actor is None else actor.login,
        draft=draft,
        head_sha=_req_sha(node.get("headRefOid"), "the head sha"),
        updated_at=_req_ts(node.get("updatedAt"), "the update time"),
        ci_state=_state(node),
        review_decision=_opt_str(node.get("reviewDecision")),
        roles=(role,),
    )


def _read_list(
    env: ReadEnv,
    target: Target,
    search: str,
    role: str,
    skip: Callable[[str], bool] | None,
) -> tuple[list[LightItem], bool]:
    """One role's PRs and whether the list was read whole."""
    backend = GitHubBackend(env)
    items: list[LightItem] = []
    complete = True
    skipped = 0
    after: str | None = None
    for _ in range(MAX_PAGES):

        def check(data: dict[str, Any]) -> tuple[list[LightItem], int, bool, str | None, int]:
            block = _obj(data.get("search"), "the search")
            count = block.get("issueCount")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise Malformed("the match count")
            info = _obj(block.get("pageInfo"), "the search page")
            more = info.get("hasNextPage")
            if not isinstance(more, bool):
                raise Malformed("the search page")
            cursor = _cursor(info.get("endCursor"))
            if more and cursor is None:
                raise Malformed("the search page cursor")
            found: list[LightItem] = []
            dropped = 0
            for raw in _list(block.get("nodes"), "the search results"):
                if (
                    raw is None
                    or raw == {}
                    or (
                        isinstance(raw, dict) and raw.get("__typename") not in (None, "PullRequest")
                    )
                ):
                    dropped += 1
                    continue
                found.append(_item(target, _obj(raw, "a result"), role))
            return found, count, more, cursor, dropped

        variables = {"q": search, "first": PAGE_SIZE, "after": after}
        found, count, more, cursor, dropped = backend._graphql(
            target, LIGHT_QUERY, variables, check
        )
        if dropped:
            complete = False
        for item in found:
            if skip is not None and skip(item.repo):
                skipped += 1
            else:
                items.append(item)
        if not more:
            if count > len(items) + skipped:
                complete = False
            return items, complete
        after = cursor
    return items, False  # more pages than MAX_PAGES


def read_light(
    env: ReadEnv,
    host: str,
    known: Identity | None,
    skip: Callable[[str], bool] | None = None,
) -> LightPage:
    """The authored and the review-requested open PRs of the account on ``host``."""
    target = env.hosts.target(host)
    me = resolve_me(env, host, known)
    merged: dict[str, LightItem] = {}
    complete = True
    for role, query in (
        ("authored", m.PrQuery(host=host, author=me)),
        ("requested", m.PrQuery(host=host, review_requested_from=me)),
    ):
        items, whole = _read_list(env, target, build_search(query), role, skip)
        complete = complete and whole
        for item in items:
            have = merged.get(item.pr)
            if have is None:
                merged[item.pr] = item
            else:
                merged[item.pr] = replace(have, roles=(*have.roles, role))
    return LightPage(items=list(merged.values()), complete=complete)
