"""GitHub ``prs``, ``access`` and ``whoami`` (#14), kept out of ``github.py`` so the issues that
share that class do not collide. ``GitHubBackend`` delegates here in three thin methods.

``prs`` is one GraphQL ``search`` (a constant query text; the search string is a variable). The
string is built by ``build_search`` only from parts that fit a strict grammar: a login that does
not fit is refused (``usage``), never escaped, because a space, colon or quote in a search string
adds a qualifier. GitHub caps a search at 1000 results and ``issueCount`` says how many matched,
so a list that stops at the cap, or holds fewer items than ``issueCount``, is ``complete: false``.
The host's answer is checked against the filters it was asked for. The search index lags the live
``state`` and ``isDraft``, so an item whose state or draft flag no longer matches (merged, closed,
reopened or marked ready seconds earlier) is dropped and the list is ``complete: false``; an item
older than the ``updated_since`` floor is ``upstream_error``, never silently kept.

``access`` reads the repository, then ``collaborators/U/permission``. Only a clear answer is a
yes or a no: a 403 (the account may not read it) is null with ``no_permission_to_read``, and an
answer that is not the documented shape is an error, never write access.

Everything the host says is untrusted and is checked or sanitized here, as in ``github.py``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal

from .. import model as m
from ..errors import ForgectlError
from ..fanout import instant, sort_items
from ..gateway import Target, verify_identity
from ..identifiers import RepoRef, bind_login
from ..reads import ReadEnv
from ..sanitize import sanitize_line
from .github import (
    _ACTOR_FIELDS,
    _NODE_ID,
    _PR_STATES,
    MAX_PR_NUMBER,
    Actor,
    Malformed,
    _cursor,
    _list,
    _obj,
    _opt_bool,
    _opt_obj,
    _Pages,
    _project,
    _repo_node_id,
    _req_sha,
    _req_ts,
    _str,
    malformed,
    parse_actor,
)

PAGE_SIZE = 100
SEARCH_CAP = 1000  # GitHub's search returns at most this many results
MAX_SEARCH_PAGES = SEARCH_CAP // PAGE_SIZE

#: The one query ``prs`` reads with. ``$q`` is the search string built by ``build_search``;
#: nothing else is ever put into the text.
PRS_QUERY = (
    """
query ForgectlPrs($q: String!, $first: Int!, $after: String) {
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
        state
        isDraft
        createdAt
        updatedAt
        author { ...ActorFields }
        labels(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes { name }
        }
        headRefName
        headRefOid
        baseRefName
        headRepository { nameWithOwner }
        isCrossRepository
        repository { id nameWithOwner }
      }
    }
  }
}
"""
    + _ACTOR_FIELDS
)

# -- the search string -------------------------------------------------------------------------

# ASCII only, matched whole (``fullmatch``): a trailing newline or a lookalike never passes.
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?", re.ASCII)
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._-]{1,100}", re.ASCII)
_UTC_SECONDS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)

_STATE_WORDS: Mapping[str, tuple[str, ...]] = {
    "open": ("is:open",),
    "closed": ("is:closed", "is:unmerged"),
    "merged": ("is:merged",),
    "all": (),
}


def _refuse(what: str) -> ForgectlError:
    return ForgectlError("usage", f"{what}; it cannot be used in a GitHub search")


def _login(value: object, what: str) -> str:
    if not isinstance(value, str) or not _LOGIN.fullmatch(value):
        raise _refuse(f"{what} is not a GitHub login")
    return value


def _repo_path(path: object) -> tuple[str, str]:
    parts = path.split("/") if isinstance(path, str) else []
    if len(parts) != 2 or any(p in (".", "..") or not _PATH_SEGMENT.fullmatch(p) for p in parts):
        raise _refuse("the repository is not an owner/name path")
    return parts[0], parts[1]


def build_search(query: m.PrQuery) -> str:
    """The GitHub search string of ``query``, from validated parts only.

    Every value is matched whole against an ASCII grammar with no space, colon, quote or
    newline; anything else is refused, never escaped. A bot author is searched by its app form
    (``app/name``).
    """
    limit = query.limit
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= SEARCH_CAP:
        raise _refuse("the limit is not a whole number from 1 to 1000")
    if query.state not in _STATE_WORDS:
        raise _refuse("the state is not one prs knows")
    if query.repo is None and query.author is None and query.review_requested_from is None:
        raise _refuse("a search needs a repository, an author or a requested reviewer")
    words = ["is:pr", *_STATE_WORDS[query.state]]
    if query.repo is not None:
        owner, name = _repo_path(query.repo.path)
        words.append(f"repo:{owner}/{name}")
    if query.author is not None:
        author = _login(query.author, "the author")
        if author.endswith("[bot]"):
            words.append(f"author:app/{author[: -len('[bot]')]}")
        else:
            words.append(f"author:{author}")
    if query.review_requested_from is not None:
        # `user-review-requested:` is direct requests by name, no team-only ones (interface)
        words.append(f"user-review-requested:{_login(query.review_requested_from, 'the reviewer')}")
    if query.draft is not None:
        if not isinstance(query.draft, bool):
            raise _refuse("draft is not a boolean")
        words.append("draft:true" if query.draft else "draft:false")
    if query.updated_since is not None:
        since = query.updated_since
        if not isinstance(since, str) or not _UTC_SECONDS.fullmatch(since):
            raise _refuse("the time is not UTC to the second (YYYY-MM-DDTHH:MM:SSZ)")
        try:
            datetime.fromisoformat(since)
        except ValueError:
            raise _refuse("the time is not a real time") from None
        # GitHub documents the offset form only: `...T00:00:00+00:00`, not `...Z`
        words.append(f"updated:>={since[:-1]}+00:00")
    words.append("sort:updated-desc")
    return " ".join(words)


# -- prs ---------------------------------------------------------------------------------------


def _host_of(query: m.PrQuery) -> str:
    host = query.host if query.host is not None else None
    if query.repo is not None:
        if host is not None and host != query.repo.host:
            raise ForgectlError("internal", "prs: the repository is not on the queried host")
        host = query.repo.host
    if host is None:
        raise ForgectlError("internal", "prs: the query names no host")
    return host


def _page_info(search: dict[str, Any]) -> tuple[bool, str | None]:
    info = _obj(search.get("pageInfo"), "the search page")
    more = info.get("hasNextPage")
    if not isinstance(more, bool):
        raise Malformed("the search page")
    if not more:
        return False, None
    cursor = _cursor(info.get("endCursor"))
    if cursor is None:
        raise Malformed("the search page cursor")
    return True, cursor


def _check_floor(item: m.PrListItem, floor: datetime | None) -> None:
    """An item older than the time asked for is not the documented behavior: it ends the read."""
    if floor is not None and instant(item.updated_at) < floor:
        raise Malformed("an item older than the time asked for")


def _matches(item: m.PrListItem, query: m.PrQuery) -> bool:
    """Whether the item's live state and draft flag match the filters. The search runs on an
    index that lags them, so a mismatch is a PR that changed since: dropped, list incomplete."""
    if query.state != "all" and item.state != query.state:
        return False
    return query.draft is None or item.draft == query.draft


def _node_repo(target: Target, node: dict[str, Any]) -> str:
    """The repo id of a search result, read the way ``_item`` reads it."""
    repository = _obj(node.get("repository"), "the repository")
    return f"{target.host}/{_project(repository.get('nameWithOwner'), 'the repository path')}"


def _item(env: ReadEnv, target: Target, node: dict[str, Any], *, more: bool) -> m.PrListItem:
    """One search result; ``more: False`` checks it without reading further label pages."""
    from .github import GitHubBackend  # the label paging of `show`, not written twice

    number = node.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= MAX_PR_NUMBER:
        raise Malformed("the number")
    node_id = _str(node.get("id"), "the id")
    if not _NODE_ID.fullmatch(node_id):
        raise Malformed("the id")
    repository = _obj(node.get("repository"), "the repository")
    project = _project(repository.get("nameWithOwner"), "the repository path")
    verify_identity(
        _str(node.get("url"), "the url"), host=target.host, project=project, secret=target.token
    )
    state = _PR_STATES.get(_str(node.get("state"), "the state"))
    if state is None:
        raise Malformed("the state")
    draft = node.get("isDraft")
    if not isinstance(draft, bool):
        raise Malformed("the draft flag")
    head_repo = _opt_obj(node.get("headRepository"), "the head repository")
    head_path = None if head_repo is None else _project(head_repo.get("nameWithOwner"), "head")
    pages = _Pages(target, node_id, number, project, more)
    labels = GitHubBackend(env)._labels(pages, {"labels": node.get("labels")})
    author = (parse_actor(node.get("author")) or Actor(None, None, None)).author()
    return m.PrListItem(
        pr=f"{target.host}/{project}#{number}",
        pr_id=node_id,
        repo=f"{target.host}/{project}",
        number=number,
        url=f"https://{target.host}/{project}/pull/{number}",
        title=sanitize_line(_str(node.get("title"), "the title")),
        author=author,
        state=state,
        draft=draft,
        labels=labels,
        head=m.Head(
            sha=_req_sha(node.get("headRefOid"), "the head sha"),
            ref=sanitize_line(_str(node.get("headRefName"), "the head branch")),
            repo=None if head_path is None else f"{target.host}/{head_path}",
            is_fork=_opt_bool(node.get("isCrossRepository")) if head_path is not None else None,
        ),
        base_ref=sanitize_line(_str(node.get("baseRefName"), "the base branch")),
        created_at=_req_ts(node.get("createdAt"), "the creation time"),
        updated_at=_req_ts(node.get("updatedAt"), "the update time"),
        repo_id=_repo_node_id(repository.get("id")),
    )


def list_prs(env: ReadEnv, query: m.PrQuery, skip: Callable[[str], bool] | None = None) -> m.PrPage:
    """``prs`` on one GitHub host: at most ``limit`` items, newest ``updated_at`` first. An item
    whose repo id ``skip`` accepts is left out before its label pages are read, and does not make
    the list incomplete."""
    search = build_search(query)  # refuses before any credential is read or call made
    host = _host_of(query)
    target = env.hosts.target(host)
    floor = None if query.updated_since is None else instant(query.updated_since)
    need = min(query.limit + 1, SEARCH_CAP)  # one more than asked tells `complete`
    try:
        items, dropped, more, total, skipped = _read_pages(
            env, target, query, search, need, floor, skip
        )
    except Malformed as bad:
        raise malformed(f"{bad}") from None
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise malformed("data") from None
    items = sort_items(items)
    cut = len(items) > query.limit
    complete = not (cut or dropped or more or total > len(items) + skipped)
    rate = env.gateway.lowest_rate(target.host)  # from the response headers (interface: prs)
    return m.PrPage(
        items=tuple(items[: query.limit]),
        complete=complete,
        rate_limit=m.RateLimit(
            host=target.host,
            remaining=None if rate is None else rate.remaining,
            reset_at=None if rate is None else rate.reset_at,
        ),
    )


def _count(block: dict[str, Any]) -> int:
    count = block.get("issueCount")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise Malformed("the match count")
    return count


def _results(block: dict[str, Any], first: int) -> list[dict[str, Any] | None]:
    """The page's pull requests in order; None for a result that is not one (or is hidden)."""
    nodes = _list(block.get("nodes"), "the search results")
    if len(nodes) > first:
        raise Malformed("more results than asked for")
    results: list[dict[str, Any] | None] = []
    for raw in nodes:
        if raw is None or raw == {}:
            results.append(None)
            continue
        node = _obj(raw, "a result")
        results.append(None if node.get("__typename") not in (None, "PullRequest") else node)
    return results


def _search_page(
    env: ReadEnv,
    target: Target,
    query: m.PrQuery,
    floor: datetime | None,
    search: str,
    first: int,
    after: str | None,
) -> dict[str, Any]:
    """One search page, its shape checked inside the retried call (as ``show`` does), so an
    answer that is not the documented shape is retried like a 5xx. Label pages past the first
    are read afterwards, each with its own retries."""
    from .github import GitHubBackend  # the retried, checked GraphQL call of `show`

    def check(data: dict[str, Any]) -> dict[str, Any]:
        block = _obj(data.get("search"), "the search")
        _count(block)
        for node in _results(block, first):
            if node is not None:
                _check_floor(_item(env, target, node, more=False), floor)
        _page_info(block)
        return data

    variables = {"q": search, "first": first, "after": after}
    return GitHubBackend(env)._graphql(target, PRS_QUERY, variables, check)


def _read_pages(
    env: ReadEnv,
    target: Target,
    query: m.PrQuery,
    search: str,
    need: int,
    floor: datetime | None,
    skip: Callable[[str], bool] | None = None,
) -> tuple[list[m.PrListItem], bool, bool, int, int]:
    items: list[m.PrListItem] = []
    seen: set[str] = set()
    skipped_ids: set[str] = set()  # left out on purpose: counted against the host's match count
    dropped = False  # something the host listed could not be kept: the list is not whole
    after: str | None = None
    more = False
    total = 0
    for _page in range(MAX_SEARCH_PAGES):
        first = min(PAGE_SIZE, need - len(items))
        data = _search_page(env, target, query, floor, search, first, after)
        block = _obj(data.get("search"), "the search")
        total = _count(block)
        for node in _results(block, first):
            if node is None:
                dropped = True  # not a pull request, or hidden from this account
                continue
            if skip is not None and skip(_node_repo(target, node)):
                skipped_ids.add(_str(node.get("id"), "the id"))
                continue  # before `_item` reads label pages: a skipped PR costs no request
            item = _item(env, target, node, more=True)
            if item.pr_id in seen:
                dropped = True  # the list moved between pages: something may be missing
                continue
            seen.add(item.pr_id)
            _check_floor(item, floor)
            if not _matches(item, query):
                dropped = True  # changed since the index was written: not listed, not whole
                continue
            items.append(item)
        more, after = _page_info(block)
        if not more or len(items) >= need:
            break
    return items, dropped, more, total, len(skipped_ids)


# -- access ------------------------------------------------------------------------------------

_LEVELS: Mapping[str, frozenset[str]] = {
    # the legacy `permission` value, and the roles that can sit under it
    "admin": frozenset({"admin"}),
    "write": frozenset({"maintain", "write"}),
    "read": frozenset({"triage", "read"}),
    "none": frozenset({"none"}),
}
Level = Literal["admin", "maintain", "write", "triage", "read", "none"]


def _api[T](env: ReadEnv, target: Target, segments: list[str], check: Callable[[Any], T]) -> T:
    """One REST read; ``check`` validates the answer inside the retried call, so an answer that
    is not the documented shape is retried like a 5xx (interface: Reliability)."""

    def once() -> T:
        try:
            return check(env.gateway.api(target, "GET", segments, shape="object").data)
        except Malformed as bad:
            raise malformed(f"{bad}") from None

    return env.call(once)


def access(env: ReadEnv, repo: RepoRef, user: str) -> m.AccessResult:
    """Whether ``user`` can push to ``repo``; null (never true) when the answer is not clear."""
    user = bind_login(user, "github")
    try:
        owner, name = _repo_path(repo.path)
    except ForgectlError:
        raise ForgectlError("usage", "the repository is not an owner/name path") from None
    target = env.hosts.target(repo.host)

    def check_repo(read: Any) -> str:
        full_name = _project(_obj(read, "the repository").get("full_name"), "the repository path")
        verify_identity(
            read.get("html_url"), host=target.host, project=full_name, secret=target.token
        )
        return full_name

    full_name = _api(env, target, ["repos", owner, name], check_repo)
    canonical_owner, canonical_name = full_name.split("/")
    where = f"{target.host}/{full_name}"
    try:
        return _api(
            env,
            target,
            ["repos", canonical_owner, canonical_name, "collaborators", user, "permission"],
            lambda answer: _permission(where, user, _obj(answer, "the permission")),
        )
    except ForgectlError as error:
        # the account may not read this answer (403), or the host cannot resolve a bot's
        # login in this path (404): unknown, which consumers read as no write access
        if error.code == "permission_denied" or (
            error.code == "not_found" and user.endswith("[bot]")
        ):
            return m.AccessResult(
                repo=where,
                user=user,
                write_access=None,
                level=None,
                source="collaborator_permission",
                reason="no_permission_to_read",
            )
        raise


def _permission(where: str, user: str, answer: dict[str, Any]) -> m.AccessResult:
    permission = answer.get("permission")
    if not isinstance(permission, str) or permission not in _LEVELS:
        raise Malformed("the permission")
    # the answer must name the user it is for: one with no user could be anyone's
    who = _obj(answer.get("user"), "the user")
    login = _str(who.get("login"), "the user's login")
    if m.ascii_lower(login) != m.ascii_lower(user):
        raise Malformed("an answer for another user")
    try:
        shown = bind_login(login, "github")
    except ForgectlError:
        raise Malformed("the user's login") from None
    role = answer.get("role_name")
    level: Level = permission  # type: ignore[assignment]
    if isinstance(role, str) and role in _LEVELS[permission]:
        level = role  # type: ignore[assignment]
    return m.AccessResult(
        repo=where,
        user=shown,
        write_access=permission in ("admin", "write"),
        level=level,
        source="collaborator_permission",
        reason=None,
    )


# -- whoami ------------------------------------------------------------------------------------


def whoami(env: ReadEnv, host: str) -> m.Identity:
    """The authenticated account on ``host`` (``GET /user``), read with the shared retries."""
    target = env.hosts.target(host)
    user = env.call(lambda: env.hosts.authenticated_user(target))
    return m.Identity(
        host=target.host, backend="github", login=user.login, id=user.id, scopes=user.scopes
    )


__all__ = ["PRS_QUERY", "access", "build_search", "list_prs", "whoami"]
