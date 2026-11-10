"""GitLab ``prs``, ``access`` and ``whoami`` (#19), kept out of ``gitlab.py`` so the issues that
share that class do not collide. ``GitLabBackend`` delegates here in three thin methods; every
function takes the backend and uses its gateway, retries, caches and validators.

``prs`` reads ``GET /merge_requests`` (every host) or ``GET /projects/:path/merge_requests`` (one
repository) with ``scope=all``, ``order_by=updated_at`` and ``sort=desc``. Every query value is
built here from a validated part: a login must fit the GitLab grammar, the state comes from a
fixed table, the time is matched whole, and the gateway percent-encodes each part, so no value
can add a parameter. The list ends at ``limit + 1`` kept items, a short page, or the page cap,
and ``complete`` is false for any cut: the limit, the page cap, or an item the host listed that
was not kept. The host's answer is checked against the filters it was asked for: an item whose
state, draft flag, author or reviewers no longer match is dropped and the list is incomplete; an
item older than the ``updated_since`` floor, or out of ``updated_at`` order, is
``upstream_error``, never kept silently.

A project list is tied to the project asked for by one ``GET /projects/:path``: its id and the
path of its verified ``web_url`` must be every item's, so a list that is another project's, or
whose paths differ under one id, is ``upstream_error``. The author's bot flag and the head project
are read only for the items that are returned.

``access`` reads the project, resolves the user, then ``members/all/:id``: only a clear answer is
a yes or a no (a 401 or 403 is null with ``no_permission_to_read``), and an answer that is not the
documented shape is an error, never write access.

Everything the host says is untrusted and is checked or sanitized with ``gitlab.py``'s validators.
Every GitLab behavior relied on here and not yet seen on a real host is marked ``UNVERIFIED``.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from .. import model as m
from ..errors import ForgectlError
from ..fanout import instant, sort_items
from ..gateway import Target, path_int, verify_identity
from ..hosts import User
from ..identifiers import RepoRef, bind_login
from .gitlab import (
    _MAX_NUMBER,
    MAX_PAGES,
    PER_PAGE,
    _bad,
    _flag,
    _int_id,
    _list,
    _obj,
    _opt_int_id,
    _project_path,
    _req_line,
    _required_time,
    _sha,
    _user,
)

if TYPE_CHECKING:
    from .gitlab import GitLabBackend

MAX_LIMIT = 1000
#: `draft` is a filter from GitLab 19.0 (Host versions); older and unreadable versions use `wip`.
_DRAFT_FILTER_FROM = (19, 0)
_STATE_PARAM: Mapping[str, str] = {
    "open": "opened",  # UNVERIFIED: a `locked` MR (briefly, while merging) is not listed
    "closed": "closed",
    "merged": "merged",
    "all": "all",
}
_UTC_SECONDS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)


def _refuse(what: str) -> ForgectlError:
    return ForgectlError("usage", f"{what}; it cannot be used in a GitLab query")


def _login(value: object, what: str) -> str:
    """A login that fits the GitLab grammar, else ``usage`` (never escaped into a parameter)."""
    if not isinstance(value, str):
        raise _refuse(f"{what} is not a GitLab username")
    try:
        return bind_login(value, "gitlab")
    except ForgectlError:
        raise _refuse(f"{what} is not a GitLab username") from None


def _repo_path(path: object) -> str:
    try:
        found = _project_path(path, "the repository path")
    except ForgectlError:
        raise _refuse("the repository is not a project path") from None
    if "/" not in found:  # a project sits in a namespace
        raise _refuse("the repository is not a project path")
    return found


def _host_of(query: m.PrQuery) -> str:
    host = query.host
    if query.repo is not None:
        if host is not None and host != query.repo.host:
            raise ForgectlError("internal", "prs: the repository is not on the queried host")
        host = query.repo.host
    if host is None:
        raise ForgectlError("internal", "prs: the query names no host")
    return host


def build_request(
    query: m.PrQuery, draft_name: str | None
) -> tuple[list[str], list[tuple[str, str]]]:
    """The route segments and the query parameters of ``query``, from validated parts only
    (``draft_name`` is ``draft`` or ``wip``, chosen by host version). Anything that does not fit
    is ``usage`` before any call."""
    limit = query.limit
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise _refuse("the limit is not a whole number from 1 to 1000")
    if query.state not in _STATE_PARAM:
        raise _refuse("the state is not one prs knows")
    if query.repo is None and query.author is None and query.review_requested_from is None:
        raise _refuse("a list needs a repository, an author or a requested reviewer")
    segments = ["merge_requests"]
    if query.repo is not None:
        segments = ["projects", _repo_path(query.repo.path), "merge_requests"]
    params: list[tuple[str, str]] = [("scope", "all"), ("state", _STATE_PARAM[query.state])]
    if query.author is not None:
        params.append(("author_username", _login(query.author, "the author")))
    if query.review_requested_from is not None:
        # `reviewer_username`: reviewers by name; a finished request stays listed (interface)
        params.append(
            ("reviewer_username", _login(query.review_requested_from, "the requested reviewer"))
        )
    if query.draft is not None:
        if not isinstance(query.draft, bool):
            raise _refuse("draft is not a boolean")
        params.append((draft_name or "wip", "yes" if query.draft else "no"))
    if query.updated_since is not None:
        since = query.updated_since
        if not isinstance(since, str) or not _UTC_SECONDS.fullmatch(since):
            raise _refuse("the time is not UTC to the second (YYYY-MM-DDTHH:MM:SSZ)")
        try:
            datetime.fromisoformat(since)
        except ValueError:
            raise _refuse("the time is not a real time") from None
        params.append(("updated_after", since))  # UNVERIFIED: ISO 8601 with `Z`, inclusive
    params += [("order_by", "updated_at"), ("sort", "desc")]
    return segments, params


# -- prs ---------------------------------------------------------------------------------------

#: One command's source-project reads of `head.repo`: the answer for a (host, project, source
#: project) is read once, whatever the number of pages. It is made per `list_prs` call, so a
#: backend that lives on does not answer a later command from an old read.
_HeadCache = dict[tuple[str, int, int | None], tuple[str | None, bool | None]]


def _head(
    backend: GitLabBackend,
    target: Target,
    cache: _HeadCache,
    parsed: _Parsed,
    mr: Mapping[str, Any],
    canonical: str,
) -> tuple[str | None, bool | None]:
    key = (target.host, parsed.project_id, parsed.source_project_id)
    if key not in cache:
        cache[key] = backend._head_repo(target, mr, parsed.project_id, canonical)
    return cache[key]


class _Parsed:
    """One listed MR read without any side read: the item holds a null ``is_bot`` and head repo
    until ``_finish`` fills them for an item that is returned."""

    __slots__ = (
        "author_user",
        "canonical",
        "item",
        "mr",
        "project_id",
        "reviewers",
        "source_project_id",
    )

    def __init__(
        self,
        item: m.PrListItem,
        mr: Mapping[str, Any],
        author_user: Any,
        project_id: int,
        canonical: str,
        source_project_id: int | None,
        reviewers: frozenset[str] | None,
    ):
        self.item = item
        self.mr = mr
        self.author_user = author_user
        self.project_id = project_id
        self.canonical = canonical
        self.source_project_id = source_project_id
        self.reviewers = reviewers


def _reviewers(raw: Any) -> frozenset[str] | None:
    """The listed reviewers' logins (lower case); None when the MR has no ``reviewers`` field."""
    if raw is None:
        return None
    return frozenset(m.ascii_lower(_user(r, "reviewer").login) for r in _list(raw, "reviewers"))


def _parse(backend: GitLabBackend, target: Target, raw: Any) -> _Parsed:
    """Every field is read and checked here; the two reads of other resources (the author's bot
    flag, the head project) wait until the item is known to be returned."""
    mr = _obj(raw, "list item")
    number = _int_id(mr.get("iid"), "iid")
    if number > _MAX_NUMBER:
        raise _bad("iid")
    canonical = backend._canonical_path(mr.get("web_url"), target, number)
    project_id = _int_id(mr.get("project_id"), "project_id")
    if mr.get("target_project_id") is not None and mr["target_project_id"] != project_id:
        raise _bad("target_project_id")
    source_project_id = _opt_int_id(mr.get("source_project_id"), "source_project_id")
    author_user = _user(mr.get("author"), "author")
    author = m.Author(login=author_user.login, id=str(author_user.id), is_bot=None)
    status = mr.get("detailed_merge_status")
    draft = _flag(mr.get("draft", mr.get("work_in_progress", False)), "draft")
    host_path = f"{target.host}/{canonical}"
    item = m.PrListItem(
        pr=f"{host_path}#{number}",
        pr_id=f"gid://gitlab/MergeRequest/{_int_id(mr.get('id'), 'id')}",
        repo=host_path,
        number=number,
        url=f"https://{target.host}{target.api_prefix}/{canonical}/-/merge_requests/{number}",
        title=_req_line(mr.get("title"), "title"),
        author=author,
        state=backend._state(mr.get("state")),
        draft=draft or status == "draft_status",
        labels=backend._labels(mr.get("labels")),
        head=m.Head(
            sha=_sha(mr.get("sha"), "sha"),
            ref=_req_line(mr.get("source_branch"), "source_branch"),
            repo=None,
            is_fork=None,
        ),
        base_ref=_req_line(mr.get("target_branch"), "target_branch"),
        updated_at=_required_time(mr.get("updated_at"), "updated_at"),
    )
    return _Parsed(
        item,
        mr,
        author_user,
        project_id,
        canonical,
        source_project_id,
        _reviewers(mr.get("reviewers")),
    )


def _finish(
    backend: GitLabBackend, target: Target, cache: _HeadCache, parsed: _Parsed
) -> m.PrListItem:
    """The item with its side reads done: the author's bot flag and the head project."""
    item = parsed.item
    head_repo, is_fork = _head(backend, target, cache, parsed, parsed.mr, parsed.canonical)
    return replace(
        item,
        author=replace(item.author, is_bot=backend._is_bot(target, parsed.author_user)),
        head=replace(
            item.head,
            repo=None if head_repo is None else f"{target.host}/{head_repo}",
            is_fork=is_fork,
        ),
    )


def _project_web_path(backend: GitLabBackend, target: Target, web_url: Any) -> str:
    """The path of a project's own ``web_url``: on this host (exactly), ``https``, nothing but
    the project path (a trailing slash aside), inside the host's prefix. The host is compared
    exactly; the caller compares the path, which GitLab does not treat as case sensitive."""
    verify_identity(web_url, host=target.host, secret=target.token)
    backend._https(web_url, "project web_url")
    assert isinstance(web_url, str)
    if not web_url.isascii() or any(c.isspace() or not c.isprintable() for c in web_url):
        raise _bad("project web_url")
    parts = urllib.parse.urlsplit(web_url)
    path = parts.path
    if (
        parts.query
        or parts.fragment
        or "?" in web_url
        or "#" in web_url
        or ";" in path
        or "%" in path
        or not path.startswith("/")
    ):
        raise _bad("project web_url")
    path = path[1:].removesuffix("/")
    prefix = target.api_prefix.strip("/")
    if prefix:
        if not path.startswith(prefix + "/"):
            raise backend._wrong_host(target, "a path outside the host's prefix", web_url)
        path = path[len(prefix) + 1 :]
    return _project_path(path, "project web_url")


def _tie_to_project(
    backend: GitLabBackend, target: Target, requested: str, scope: set[tuple[int, str]]
) -> None:
    """A project list is of the project asked for: one read of it gives its id and its own path
    (a rename is followed here, as the read of the path is the host's answer), and every listed
    item must name that id and that path, whatever the case of the path."""
    project = _obj(backend._get(target, ["projects", requested], sub=False).data, "project")
    path = _project_web_path(backend, target, project.get("web_url"))
    project_id = _int_id(project.get("id"), "project id")
    for item_project, canonical in scope:
        if item_project != project_id or m.ascii_lower(canonical) != m.ascii_lower(path):
            raise ForgectlError(
                "upstream_error", "gitlab: a project list holds an item of another project"
            )


def _matches(parsed: _Parsed, query: m.PrQuery) -> bool:
    """Whether the item answers the filters it was asked for. A host that lists one that does
    not (a state changed since, an ignored parameter) is not trusted: dropped, list incomplete."""
    item = parsed.item
    if query.state != "all" and item.state != query.state:
        return False
    if query.draft is not None and item.draft != query.draft:
        return False
    if query.author is not None and m.ascii_lower(item.author.login or "") != m.ascii_lower(
        query.author
    ):
        return False
    if query.review_requested_from is not None:
        if parsed.reviewers is None:
            raise _bad("reviewers")  # the field is documented on every listed MR (UNVERIFIED)
        if m.ascii_lower(query.review_requested_from) not in parsed.reviewers:
            return False
    return True


def _draft_name(backend: GitLabBackend, host: str) -> str:
    version = backend.host_version(host)
    return "draft" if version is not None and version >= _DRAFT_FILTER_FROM else "wip"


def list_prs(backend: GitLabBackend, query: m.PrQuery) -> m.PrPage:
    """``prs`` on one GitLab host: at most ``limit`` items, newest ``updated_at`` first."""
    host = _host_of(query)
    build_request(query, None)  # refuses before any credential is read or call made
    target = backend._hosts.target(host)
    if target.backend != "gitlab":
        raise ForgectlError("internal", "the gitlab backend was given another backend's host")
    # the version is read only for a draft filter (Host versions)
    draft_name = None if query.draft is None else _draft_name(backend, host)
    segments, params = build_request(query, draft_name)
    floor = None if query.updated_since is None else instant(query.updated_since)
    need = query.limit + 1  # one more than asked tells `complete`
    items: list[_Parsed] = []
    seen: set[str] = set()
    dropped = False  # something the host listed was not kept: the list is not whole
    more = False
    newest: datetime | None = None  # the previous item's time, for the order check
    scope_project: int | None = None
    scope: set[tuple[int, str]] = set()  # (project id, path) of every item the host listed
    page = 1
    for _ in range(MAX_PAGES):
        response = backend._get(
            target,
            segments,
            query=(*params, ("per_page", PER_PAGE), ("page", str(page))),
            shape="list",
            sub=False,
        )
        batch = _list(response.data, "list")
        if len(batch) > int(PER_PAGE):
            raise _bad("list length")  # more than the page size asked for
        for raw in batch:
            parsed = _parse(backend, target, raw)
            moment = instant(parsed.item.updated_at)
            if floor is not None and moment < floor:
                raise ForgectlError(
                    "upstream_error", "gitlab: an item is older than the time asked for"
                )
            if newest is not None and moment > newest:
                raise ForgectlError("upstream_error", "gitlab: the list is not newest first")
            newest = moment
            if query.repo is not None:
                scope_project = parsed.project_id if scope_project is None else scope_project
                if parsed.project_id != scope_project:
                    raise ForgectlError(
                        "upstream_error", "gitlab: an item of another project in a project list"
                    )
                scope.add((parsed.project_id, parsed.canonical))
            if parsed.item.pr_id in seen:
                dropped = True  # the list moved between pages: something may be missing
                continue
            seen.add(parsed.item.pr_id)
            if not _matches(parsed, query):
                dropped = True  # changed since the list was built: not listed, not whole
                continue
            items.append(parsed)
        header = response.headers.get("x-next-page")
        if header is not None:
            more = header != ""
            if more and header != str(page + 1):
                raise _bad("pagination header")  # UNVERIFIED: exactly the next page or empty
        else:
            more = len(batch) >= int(PER_PAGE)
        if not more or len(items) >= need:
            break
        page += 1
    if query.repo is not None and scope:
        _tie_to_project(backend, target, _repo_path(query.repo.path), scope)
    ordered = sort_items(p.item for p in items)
    cut = len(ordered) > query.limit
    # side reads only for what is returned: the order key is in the item, so the same ones win
    cache: _HeadCache = {}
    by_pr = {p.item.pr: p for p in items}
    returned = tuple(_finish(backend, target, cache, by_pr[i.pr]) for i in ordered[: query.limit])
    rate = backend._gateway.lowest_rate(target.host)  # from the response headers
    return m.PrPage(
        items=returned,
        complete=not (cut or dropped or more),
        rate_limit=m.RateLimit(
            host=target.host,
            remaining=None if rate is None else rate.remaining,
            reset_at=None if rate is None else rate.reset_at,
        ),
    )


# -- access ------------------------------------------------------------------------------------

Level = Literal["admin", "maintain", "write", "read", "none"]


def _level(member: Mapping[str, Any]) -> Level:
    """The role of an effective member: 50 owner, 40 maintainer, 30 developer, others read."""
    raw = member.get("access_level")
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 100:
        raise _bad("access_level")
    if raw >= 50:
        return "admin"
    if raw >= 40:
        return "maintain"
    if raw >= 30:
        return "write"
    return "read" if raw >= 5 else "none"


def _effective(backend: GitLabBackend, member: Mapping[str, Any]) -> bool:
    """Whether the membership is in force: active user and seat, not expired. The level of
    one that is not counts for nothing."""
    return backend._member_in_force(member)


def _answer(
    where: str, user: str, write: bool | None, level: Level | None, *, reason: bool = False
) -> m.AccessResult:
    return m.AccessResult(
        repo=where,
        user=user,
        write_access=write,
        level=level,
        source="project_member",
        reason="no_permission_to_read" if reason else None,
    )


_UNREADABLE = frozenset({"auth_required", "permission_denied"})


def access(backend: GitLabBackend, repo: RepoRef, user: str) -> m.AccessResult:
    """Whether ``user`` can push to ``repo``; null (never true) when the answer is not clear."""
    user = bind_login(user, "gitlab")
    path = _repo_path(repo.path)
    target = backend._hosts.target(repo.host)
    if target.backend != "gitlab":
        raise ForgectlError("internal", "the gitlab backend was given another backend's host")
    try:
        project = _obj(backend._get(target, ["projects", path], sub=False).data, "project")
    except ForgectlError as error:
        if error.code in _UNREADABLE:
            return _answer(f"{target.host}/{path}", user, None, None, reason=True)
        raise
    # the answer names the project by its own verified url; a path that differs from the one
    # asked for (other than in case) is another project's answer
    shown = _project_web_path(backend, target, project.get("web_url"))
    if m.ascii_lower(shown) != m.ascii_lower(path):
        raise backend._wrong_host(target, "another project", project.get("web_url"))
    project_id = _int_id(project.get("id"), "project id")
    _project_path(project.get("path_with_namespace"), "project path")  # the shape, not trusted
    where = f"{target.host}/{shown}"
    try:
        found = backend._get(
            target, ["users"], query=(("username", user),), shape="list", sub=False
        ).data
    except ForgectlError as error:
        if error.code in _UNREADABLE:
            return _answer(where, user, None, None, reason=True)
        raise
    users = _list(found, "users")
    if not users:
        return _answer(where, user, False, "none")  # no such user: reported like a non-member
    if len(users) != 1:
        raise _bad("users")
    who = _user(users[0], "user")
    if m.ascii_lower(who.login) != m.ascii_lower(user):
        raise _bad("user")  # the host answered for another account
    segments = ["projects", path_int(project_id), "members", "all", path_int(who.id)]
    try:
        data = backend._get(target, segments, sub=False).data
    except ForgectlError as error:
        if error.code == "not_found":
            return _answer(where, who.login, False, "none")
        if error.code in _UNREADABLE:
            return _answer(where, who.login, None, None, reason=True)
        raise
    member = _obj(data, "member")
    member_id = member.get("id")
    if isinstance(member_id, bool) or not isinstance(member_id, int) or member_id != who.id:
        raise _bad("member id")  # an answer for another user could be anyone's rights
    level = _level(member)
    if not _effective(backend, member):
        return _answer(where, who.login, False, "none")
    return _answer(where, who.login, backend._member_can_push(member), level)


# -- whoami ------------------------------------------------------------------------------------


def whoami(backend: GitLabBackend, host: str) -> m.Identity:
    """The authenticated account on ``host`` (``GET /user``), read with the shared retries. The
    command itself goes through ``Hosts.identity``, which also reports the token source; scopes
    are always null on GitLab."""
    target = backend._hosts.target(host)
    retry = cast(Callable[[Callable[[], User]], User], backend._retry)
    user = retry(lambda: backend._hosts.authenticated_user(target))
    return m.Identity(
        host=target.host, backend="gitlab", login=user.login, id=user.id, scopes=user.scopes
    )


__all__ = ["access", "build_request", "list_prs", "whoami"]
