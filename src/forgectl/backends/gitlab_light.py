"""GitLab light poll (#118): the change-detection tuple of every open MR the account authored or
was asked to review, from one GraphQL request for both lists.

``currentUser`` names the account, so a configured ``me`` is not consulted (as for the bulk
reader). The query selects ``iid project updatedAt diffHeadSha approvalsLeft approved
detailedMergeStatus userNotesCount headPipeline { status }`` and the few fields a new row shows.
The host is probed once per process by ``gitlab_bulk.probe``: a host that fails it is
``LightUnsupported`` and the caller reads its lists with ``prs``. A list with another page is read
page by page, up to ``MAX_PAGES``; beyond that the poll is incomplete.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ..errors import ForgectlError
from ..gateway import Target
from ..reads import ReadEnv
from ..sanitize import sanitize_line
from . import gitlab_bulk
from .gitlab import GitLabBackend
from .light_common import LightItem, LightPage, LightUnsupported

PAGE_SIZE = 50
MAX_PAGES = 5

LIGHT_QUERY = """
query ForgectlLight($first: Int!, $a: String, $r: String, $doA: Boolean!, $doR: Boolean!) {
  queryComplexity { score limit }
  currentUser {
    authored: authoredMergeRequests(state: opened, first: $first, after: $a) @include(if: $doA) {
      pageInfo { hasNextPage endCursor }
      nodes { ...LightMr }
    }
    requested: reviewRequestedMergeRequests(state: opened, first: $first, after: $r)
      @include(if: $doR) {
      pageInfo { hasNextPage endCursor }
      nodes { ...LightMr }
    }
  }
}
fragment LightMr on MergeRequest {
  id
  iid
  webUrl
  title
  draft
  updatedAt
  diffHeadSha
  approved
  approvalsLeft
  detailedMergeStatus
  userNotesCount
  author { username }
  headPipeline { status }
}
"""

_ROLES = (("authored", "a", "doA"), ("requested", "r", "doR"))


class _Bad(ValueError):
    pass


def _opt(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise _Bad(what)
    return value


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _item(backend: GitLabBackend, target: Target, node: object, role: str) -> LightItem:
    raw = _opt(node)
    if raw is None:
        raise _Bad("a merge request")
    iid = _text(raw.get("iid"), "iid")
    if not iid.isascii() or not iid.isdigit() or len(iid) > 9:
        raise _Bad("iid")
    number = int(iid)
    host, prefix = target.host, target.api_prefix
    canonical = backend._canonical_path(raw.get("webUrl"), target, number)
    detailed = raw.get("detailedMergeStatus")
    pipeline = _opt(raw.get("headPipeline"))
    author = _opt(raw.get("author"))
    approved = raw.get("approved")
    status = pipeline.get("status") if pipeline else None
    login = author.get("username") if author else None
    draft = raw.get("draft")
    if not isinstance(draft, bool):
        raise _Bad("draft")
    return LightItem(
        pr=f"{host}/{canonical}#{number}",
        pr_id=_text(raw.get("id"), "id"),
        repo=f"{host}/{canonical}",
        number=number,
        url=f"https://{host}{prefix}/{canonical}/-/merge_requests/{number}",
        title=sanitize_line(_text(raw.get("title"), "title")),
        author=login if isinstance(login, str) else None,
        draft=draft,
        head_sha=_text(raw.get("diffHeadSha"), "diffHeadSha"),
        updated_at=_text(raw.get("updatedAt"), "updatedAt"),
        ci_state=status if isinstance(status, str) else None,
        approvals_left=_int(raw.get("approvalsLeft")),
        approved=approved if isinstance(approved, bool) else None,
        merge_status=detailed if isinstance(detailed, str) else None,
        notes_count=_int(raw.get("userNotesCount")),
        roles=(role,),
    )


def read_light(
    env: ReadEnv,
    host: str,
    known: object = None,
    skip: Callable[[str], bool] | None = None,
) -> LightPage:
    reason = gitlab_bulk.probe(env, host)
    if reason is not None:
        raise LightUnsupported(reason)
    target = env.hosts.target(host)
    backend = GitLabBackend.from_env(env)
    merged: dict[str, LightItem] = {}
    cursors: dict[str, str | None] = {"a": None, "r": None}
    active = {"a": True, "r": True}
    for _ in range(MAX_PAGES):
        if env.gateway.budget.remaining() <= 0:
            raise ForgectlError("network_error", "the time budget ran out before the call")
        variables = {
            "first": PAGE_SIZE,
            "a": cursors["a"],
            "r": cursors["r"],
            "doA": active["a"],
            "doR": active["r"],
        }
        data = dict(env.call(lambda v=variables: env.gateway.graphql(target, LIGHT_QUERY, v)).data)
        user = _opt(data.get("currentUser"))
        if user is None:
            raise ForgectlError("upstream_error", "the host named no current user")
        for role, key, _flag in _ROLES:
            if not active[key]:
                continue
            conn = _opt(user.get(role))
            nodes = conn.get("nodes") if conn else None
            if conn is None or not isinstance(nodes, list):
                raise ForgectlError("upstream_error", "the host gave no merge request list")
            try:
                for node in nodes:
                    item = _item(backend, target, node, role)
                    if skip is not None and skip(item.repo):
                        continue
                    have = merged.get(item.pr)
                    merged[item.pr] = (
                        item if have is None else replace(have, roles=(*have.roles, role))
                    )
            except _Bad as bad:
                raise ForgectlError("upstream_error", f"unusable answer: {bad}") from None
            info = _opt(conn.get("pageInfo")) or {}
            cursor = info.get("endCursor")
            if (
                info.get("hasNextPage") is True
                and isinstance(cursor, str)
                and cursor != cursors[key]
            ):
                cursors[key] = cursor
            else:
                active[key] = False
                if info.get("hasNextPage") is True:  # no usable cursor: the list is cut
                    return LightPage(items=list(merged.values()), complete=False)
        if not any(active.values()):
            return LightPage(items=list(merged.values()), complete=True)
    return LightPage(items=list(merged.values()), complete=False)
