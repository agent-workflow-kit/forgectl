"""GitHub `snapshot`, `comments`, `reviews` and `ci` (#12), and the push time of the head (#56).

``GitHubBackend`` delegates here; the shared parsing helpers (``parse_actor``,
``latest_by_reviewer``, the typed readers) stay in ``github.py``. Every query is a constant text;
owner, name, number, oid, node ids and cursors travel as variables.

How a snapshot is read. ``show`` (``get_pr``) comes first: the PR object and the head's checks.
Then the parts the PR object does not carry, each as its own read: one ``PARTS_QUERY`` (reviews
with ids and bodies, review threads with their comments, general comments), the review requests
with their events, the REST ``requested_reviewers``, and the push time of the head. Every part
that is a list pages to its end; a list that hits ``MAX_PAGES`` is reported in ``incomplete`` and
never passed off as the whole. A query that answers another head than ``show`` read means the PR
moved during the reading: the whole reading is repeated once, then ``upstream_error``.

The ``rateLimit`` of the output is the lowest ``remaining`` among this module's own calls: the
GraphQL ``rateLimit`` field of each query and the REST ``x-ratelimit-*`` headers. The ``show``
read before them carries none (``Gateway.graphql`` drops response headers and ``SHOW_QUERY`` has
no ``rateLimit`` field), so it is not counted; each later query reports the budget left after it.

Everything the host says is untrusted: a field is checked for type and grammar, a string reaching
the output is sanitized for its own field, and an answer that is not the documented shape is
``upstream_error``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .. import model as m
from ..activity import for_snapshot as derive_activity
from ..errors import ForgectlError
from ..gateway import GraphQLResult, Response, Target, verify_identity
from ..identifiers import PrRef
from ..reads import ReadEnv
from ..sanitize import sanitize_line, sanitize_text
from .github import (
    _ACTOR_FIELDS,
    _NODE_ID,
    MAX_PAGES,
    MAX_PR_NUMBER,
    Actor,
    Malformed,
    _cursor,
    _list,
    _obj,
    _opt_bool,
    _opt_obj,
    _opt_str,
    _project,
    _req_sha,
    _req_ts,
    _sha,
    _split,
    _str,
    _ts,
    _url_path,
    latest_by_reviewer,
    malformed,
    parse_actor,
    rest_login,
)

# -- the constant queries ---------------------------------------------------------------------

_RATE = "rateLimit { remaining resetAt }"

_REVIEWS_PAGE = """
fragment ReviewsPage on PullRequestReviewConnection {
  pageInfo { hasNextPage endCursor }
  nodes {
    id
    state
    submittedAt
    body
    authorCanPushToRepository
    author { ...ActorFields }
    commit { oid }
  }
}
"""

_THREAD_COMMENTS = """
fragment ThreadComments on PullRequestReviewCommentConnection {
  pageInfo { hasNextPage endCursor }
  nodes {
    id
    state
    body
    createdAt
    updatedAt
    isMinimized
    line
    startLine
    author { ...ActorFields }
    replyTo { id }
    pullRequestReview { id }
    originalCommit { oid }
  }
}
"""

_THREADS_PAGE = """
fragment ThreadsPage on PullRequestReviewThreadConnection {
  pageInfo { hasNextPage endCursor }
  nodes {
    id
    isResolved
    isOutdated
    path
    line
    diffSide
    subjectType
    comments(first: 50) { ...ThreadComments }
  }
}
"""

_ISSUE_COMMENTS_PAGE = """
fragment IssueCommentsPage on IssueCommentConnection {
  pageInfo { hasNextPage endCursor }
  nodes {
    id
    body
    createdAt
    updatedAt
    isMinimized
    author { ...ActorFields }
  }
}
"""

_REVIEWER_WITH_TEAMS = """
fragment ReviewerFields on RequestedReviewer {
  __typename
  ... on User { login databaseId }
  ... on Bot { login databaseId }
  ... on Mannequin { login databaseId }
  ... on Team { slug databaseId }
}
"""

# The same fragment without the team fields, for a token that may not read them.
_REVIEWER_NO_TEAMS = """
fragment ReviewerFields on RequestedReviewer {
  __typename
  ... on User { login databaseId }
  ... on Bot { login databaseId }
  ... on Mannequin { login databaseId }
}
"""

#: Reviews, review threads and general comments of the PR, first pages. `head` is the head oid
#: `show` read, so a query for another head can be told apart from this one.
PARTS_QUERY = (
    f"""
query ForgectlParts($owner: String!, $name: String!, $number: Int!) {{
  {_RATE}
  repository(owner: $owner, name: $name) {{
    nameWithOwner
    pullRequest(number: $number) {{
      id
      url
      number
      headRefOid
      reviews(first: 100) {{ ...ReviewsPage }}
      reviewThreads(first: 100) {{ ...ThreadsPage }}
      comments(first: 100) {{ ...IssueCommentsPage }}
    }}
  }}
}}
"""
    + _ACTOR_FIELDS
    + _REVIEWS_PAGE
    + _THREADS_PAGE
    + _THREAD_COMMENTS
    + _ISSUE_COMMENTS_PAGE
)

REVIEWS_PAGE_QUERY = (
    f"""
query ForgectlReviewsPage($id: ID!, $after: String) {{
  {_RATE}
  node(id: $id) {{
    ... on PullRequest {{
      number
      url
      headRefOid
      reviews(first: 100, after: $after) {{ ...ReviewsPage }}
    }}
  }}
}}
"""
    + _ACTOR_FIELDS
    + _REVIEWS_PAGE
)

THREADS_PAGE_QUERY = (
    f"""
query ForgectlThreadsPage($id: ID!, $after: String) {{
  {_RATE}
  node(id: $id) {{
    ... on PullRequest {{
      number
      url
      headRefOid
      reviewThreads(first: 100, after: $after) {{ ...ThreadsPage }}
    }}
  }}
}}
"""
    + _ACTOR_FIELDS
    + _THREADS_PAGE
    + _THREAD_COMMENTS
)

THREAD_COMMENTS_PAGE_QUERY = (
    f"""
query ForgectlThreadCommentsPage($id: ID!, $after: String) {{
  {_RATE}
  node(id: $id) {{
    ... on PullRequestReviewThread {{
      id
      pullRequest {{ number url headRefOid }}
      comments(first: 50, after: $after) {{ ...ThreadComments }}
    }}
  }}
}}
"""
    + _ACTOR_FIELDS
    + _THREAD_COMMENTS
)

ISSUE_COMMENTS_PAGE_QUERY = (
    f"""
query ForgectlIssueCommentsPage($id: ID!, $after: String) {{
  {_RATE}
  node(id: $id) {{
    ... on PullRequest {{
      number
      url
      headRefOid
      comments(first: 100, after: $after) {{ ...IssueCommentsPage }}
    }}
  }}
}}
"""
    + _ACTOR_FIELDS
    + _ISSUE_COMMENTS_PAGE
)


def _requests_query(reviewer: str) -> str:
    return (
        f"""
query ForgectlRequests($owner: String!, $name: String!, $number: Int!) {{
  {_RATE}
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
      id
      number
      url
      headRefOid
      reviewRequests(first: 100) {{
        pageInfo {{ hasNextPage }}
        nodes {{ asCodeOwner requestedReviewer {{ ...ReviewerFields }} }}
      }}
      timelineItems(last: 100, itemTypes: [REVIEW_REQUESTED_EVENT]) {{
        pageInfo {{ hasPreviousPage startCursor }}
        nodes {{
          __typename
          ... on ReviewRequestedEvent {{
            id createdAt requestedReviewer {{ ...ReviewerFields }}
          }}
        }}
      }}
    }}
  }}
}}
"""
        + reviewer
    )


def _events_query(reviewer: str) -> str:
    return (
        f"""
query ForgectlRequestEvents($id: ID!, $before: String) {{
  {_RATE}
  node(id: $id) {{
    ... on PullRequest {{
      number
      url
      headRefOid
      timelineItems(last: 100, before: $before, itemTypes: [REVIEW_REQUESTED_EVENT]) {{
        pageInfo {{ hasPreviousPage startCursor }}
        nodes {{
          __typename
          ... on ReviewRequestedEvent {{
            id createdAt requestedReviewer {{ ...ReviewerFields }}
          }}
        }}
      }}
    }}
  }}
}}
"""
        + reviewer
    )


#: The review requests with their events, with and without the team fields (the second is sent
#: when the token may not read teams).
REQUESTS_QUERY = _requests_query(_REVIEWER_WITH_TEAMS)
REQUESTS_QUERY_NO_TEAMS = _requests_query(_REVIEWER_NO_TEAMS)
REQUEST_EVENTS_QUERY = _events_query(_REVIEWER_WITH_TEAMS)
REQUEST_EVENTS_QUERY_NO_TEAMS = _events_query(_REVIEWER_NO_TEAMS)

_PUSH_ITEMS = """
pageInfo { hasPreviousPage startCursor }
nodes {
  __typename
  ... on HeadRefForcePushedEvent { createdAt afterCommit { oid } }
  ... on PullRequestCommit { commit { oid committedDate } }
}
"""

#: The newest push events of the PR and the head commit's own date (#56).
PUSH_QUERY = f"""
query ForgectlPush($owner: String!, $name: String!, $number: Int!, $head: GitObjectID!) {{
  {_RATE}
  repository(owner: $owner, name: $name) {{
    object(oid: $head) {{ __typename ... on Commit {{ oid committedDate }} }}
    pullRequest(number: $number) {{
      id
      number
      url
      headRefOid
      timelineItems(
        last: 100
        itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT, PULL_REQUEST_COMMIT]
      ) {{ {_PUSH_ITEMS} }}
    }}
  }}
}}
"""

PUSH_PAGE_QUERY = f"""
query ForgectlPushPage($id: ID!, $before: String) {{
  {_RATE}
  node(id: $id) {{
    ... on PullRequest {{
      number
      url
      headRefOid
      timelineItems(
        last: 100
        before: $before
        itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT, PULL_REQUEST_COMMIT]
      ) {{ {_PUSH_ITEMS} }}
    }}
  }}
}}
"""

MAX_PUSH_PAGES = 5  # the head's own events are among the newest; a longer search is not made

# -- grammars the output promises --------------------------------------------------------------

_ID_TAIL = re.compile(r"[A-Za-z0-9_=-]{1,100}")
_INSTANCE = re.compile(r"[A-Za-z0-9:_=-]{1,128}")
_ACCOUNT = re.compile(r"[A-Za-z0-9:/_=-]{1,128}")
_TEAM_SLUG = re.compile(r"[A-Za-z0-9._-]{1,100}")
_DIGITS = re.compile(r"[0-9]{1,18}")

_REVIEW_STATES: Mapping[str, m.ReviewState] = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes_requested",
    "COMMENTED": "commented",
    "DISMISSED": "dismissed",
}
_SIDES: Mapping[str, Literal["left", "right"]] = {"LEFT": "left", "RIGHT": "right"}

#: GraphQL error types that mean "this token may not read the field" (interface: Review requests).
_SCOPE_ERROR_TYPES = frozenset({"INSUFFICIENT_SCOPES", "FORBIDDEN"})

Now = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class _Moved(Exception):
    """The pull request changed while it was being read."""


def _prefixed(prefix: str, value: object, what: str) -> str:
    text = _str(value, what)
    if not _ID_TAIL.fullmatch(text):
        raise Malformed(what)
    return f"gh:{prefix}:{text}"


def _pos_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.upper()).astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _author(raw: object) -> m.Author:
    return (parse_actor(raw) or Actor(None, None, None)).author()


# -- rate limit --------------------------------------------------------------------------------


@dataclass
class Limits:
    """The lowest ``remaining`` seen across this module's calls."""

    host: str
    remaining: int | None = None
    reset_at: str | None = None

    def note(self, remaining: object, reset_at: object) -> None:
        if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0:
            return
        if self.remaining is None or remaining < self.remaining:
            self.remaining = remaining
            self.reset_at = reset_at if isinstance(reset_at, str) and _ts(reset_at) else None

    def note_graphql(self, data: Mapping[str, Any]) -> None:
        info = data.get("rateLimit")
        if isinstance(info, dict):
            self.note(info.get("remaining"), info.get("resetAt"))

    def note_headers(self, headers: Mapping[str, str]) -> None:
        left = headers.get("x-ratelimit-remaining", "")
        if not _DIGITS.fullmatch(left):
            return
        reset = headers.get("x-ratelimit-reset", "")
        stamp: str | None = None
        if _DIGITS.fullmatch(reset):
            try:
                stamp = _stamp(datetime.fromtimestamp(int(reset), UTC))
            except (ValueError, OverflowError, OSError):
                stamp = None
        self.note(int(left), stamp)

    def result(self) -> m.RateLimit:
        return m.RateLimit(host=self.host, remaining=self.remaining, reset_at=self.reset_at)


# -- the reader --------------------------------------------------------------------------------


@dataclass
class _Reader:
    env: ReadEnv
    target: Target
    ref: PrRef
    owner: str
    name: str
    limits: Limits
    #: set when a token that may not read teams made a query be sent without the team fields
    team_fallback: bool = False
    warnings: list[str] = field(default_factory=list)

    #: the canonical `owner/repo` of the answers, once one has been verified
    project: str | None = None
    #: the head every answer must name (from `show`, or the first answer of `comments`)
    head: str | None = None
    #: the PR's node id every answer must name, and the only one sent as a later `$id`
    pr_id: str | None = None
    #: whether the last call made by ``gql`` failed because its answer was not the documented shape
    last_malformed: bool = False

    def gql(
        self,
        query: str,
        variables: Mapping[str, Any],
        check: Callable[[dict[str, Any]], object] | None = None,
    ) -> dict[str, Any]:
        """One query, retried. ``check`` validates the whole answer inside the retried call, so
        an answer that is not the documented shape is retried like a 5xx (interface:
        Reliability)."""
        gateway = self.env.gateway

        def once() -> dict[str, Any]:
            self.last_malformed = False
            try:
                data = dict(gateway.graphql(self.target, query, variables).data)
                if check is not None:
                    check(data)
                return data
            except Malformed as bad:
                self.last_malformed = True
                raise malformed(f"{bad}") from None
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                self.last_malformed = True
                raise malformed("data") from None

        data = self.env.call(once)
        self.limits.note_graphql(data)
        return data

    def verify_pr(self, holder: object) -> dict[str, Any]:
        """The ``number url headRefOid`` of a pull request object: this PR, on this host and
        project, at the head the reading started from. An unreadable head is malformed; a valid
        other head means the PR moved (``_Moved``)."""
        pr = _obj(holder, "the pull request")
        number = pr.get("number")
        if isinstance(number, bool) or number != self.ref.number or self.project is None:
            raise Malformed("the pull request number")
        verify_identity(
            pr.get("url"), host=self.target.host, project=self.project, secret=self.target.token
        )
        _url_path(pr.get("url"), self.project, f"/pull/{self.ref.number}")
        head = _req_sha(pr.get("headRefOid"), "the head sha")
        if self.head is not None and head != self.head:
            raise _Moved
        return pr

    def verify_id(self, pr: Mapping[str, Any]) -> str:
        """The PR's node id: the id grammar, and the id `show` read when there is one."""
        pr_id = _str(pr.get("id"), "the id")
        if not _NODE_ID.fullmatch(pr_id) or (self.pr_id is not None and pr_id != self.pr_id):
            raise Malformed("the id")
        return pr_id

    def page(
        self,
        query: str,
        variables: Mapping[str, Any],
        connection: str,
        what: str,
        node_check: Callable[[Any], object],
        *,
        seen: frozenset[str] = frozenset(),
        thread_id: str | None = None,
        backward: bool = False,
    ) -> dict[str, Any]:
        """One page of a connection of a node read by id: the connection object, checked whole
        inside the retried call. The node (or, for a review thread, the thread asked for and its
        pull request) is checked to be this PR at this head."""

        def check(data: dict[str, Any]) -> dict[str, Any]:
            node = _obj(data.get("node"), "the node")
            if thread_id is not None:
                if node.get("id") != thread_id:
                    raise Malformed("the review thread")
                self.verify_pr(node.get("pullRequest"))
            else:
                self.verify_pr(node)
            found = _obj(node.get(connection), f"the {connection}")
            _page_shape(found, what, node_check, seen, backward=backward)
            return found

        result = self.gql(query, variables, check)
        return check(result)


def _page_shape(
    connection: Mapping[str, Any],
    what: str,
    node_check: Callable[[Any], object],
    seen: frozenset[str] | set[str],
    *,
    backward: bool = False,
) -> tuple[bool, str | None]:
    """Check one page of a connection whole: every node, and the page info. Returns whether
    another page follows and its cursor. A cursor already used in the same walk is malformed:
    it would read the same entries twice."""
    for node in _list(connection.get("nodes"), what):
        node_check(node)
    info = _obj(connection.get("pageInfo"), f"the {what} page")
    more = info.get("hasPreviousPage" if backward else "hasNextPage")
    if not isinstance(more, bool):
        raise Malformed(f"the {what} page")
    if not more:
        return False, None
    cursor = _cursor(info.get("startCursor" if backward else "endCursor"))
    if cursor is None or cursor in seen:
        raise Malformed(f"the {what} page cursor")
    return True, cursor


def _drain(
    first: dict[str, Any],
    nxt: Callable[[str, frozenset[str]], dict[str, Any]],
    what: str,
    node_check: Callable[[Any], object],
    take: Callable[[Any], None],
) -> bool:
    """Feed every node of a forward-paged connection to ``take``; True when the end was reached.

    ``first`` is the first page; ``nxt(cursor, seen)`` reads the next, refusing a cursor in
    ``seen``. The page budget is checked before a page is read, so ``MAX_PAGES`` pages cost
    ``MAX_PAGES`` reads; a page cap reports False, never a prefix as the whole.
    """
    seen: set[str] = set()
    connection = first
    for index in range(MAX_PAGES):
        more, cursor = _page_shape(connection, what, node_check, seen)
        for node in _list(connection.get("nodes"), what):
            take(node)
        if not more or cursor is None:
            return True
        if index == MAX_PAGES - 1:
            return False
        seen.add(cursor)
        connection = nxt(cursor, frozenset(seen))
    return False


# -- reviews -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewsRead:
    reviews: tuple[m.Review, ...]
    latest: tuple[m.LatestByReviewer, ...]
    #: every review was read and every one could be shown
    complete: bool
    #: the review list was read to its end (an unknown state does not clear this)
    drained: bool


def _review(raw: object, index: int) -> tuple[m.Review | None, dict[str, Any], bool]:
    """The review, its node, and whether its state was one this adapter does not know."""
    node = _obj(raw, f"review {index}")
    word = _str(node.get("state"), "a review state")
    if word == "PENDING":
        return None, node, False  # a pending review belongs to its author and is not listed
    state = _REVIEW_STATES.get(word)
    if state is None:
        return None, node, True
    review = m.Review(
        id=_prefixed("r", node.get("id"), "a review id"),
        author=_author(node.get("author")),
        state=state,
        commit_sha=_sha((_opt_obj(node.get("commit"), "a commit") or {}).get("oid")),
        submitted_at=_req_ts(node.get("submittedAt"), "a review's submission time"),
        body=sanitize_text(_opt_str(node.get("body")) or ""),
    )
    return review, node, False


def _check_review(raw: object) -> None:
    """Everything a review node is read for: the listed review and its table entry."""
    _review(raw, 0)
    latest_by_reviewer([raw])


def _reviews_from(reader: _Reader, first: dict[str, Any]) -> ReviewsRead:
    nodes: list[Any] = []

    def nxt(cursor: str, seen: frozenset[str]) -> dict[str, Any]:
        return reader.page(
            REVIEWS_PAGE_QUERY,
            {"id": reader.pr_id, "after": cursor},
            "reviews",
            "reviews",
            _check_review,
            seen=seen,
        )

    drained = _drain(first, nxt, "reviews", _check_review, nodes.append)
    complete = drained
    reviews: list[m.Review] = []
    for index, raw in enumerate(nodes):
        review, _node, unknown = _review(raw, index)
        if unknown:
            complete = False  # a state this adapter cannot show: the list is not the whole
            reader.env.warn("a review of an unknown state was left out")
        if review is not None:
            reviews.append(review)
    table = latest_by_reviewer(nodes)
    return ReviewsRead(tuple(reviews), table.entries, complete, drained)


def _table_key(entry: m.LatestByReviewer) -> tuple[object, ...]:
    return (entry.login, entry.id, entry.state, entry.commit_sha, entry.can_push)


def _same_table(
    ours: tuple[m.LatestByReviewer, ...], shown: tuple[m.LatestByReviewer, ...]
) -> bool:
    """Whether two ``latest_by_reviewer`` tables agree on every account, state, commit and
    push right (the review time is not compared: ``show`` and this read select the same reviews,
    so a change shows in the other fields)."""
    return sorted(map(_table_key, ours), key=repr) == sorted(map(_table_key, shown), key=repr)


# -- threads and comments ----------------------------------------------------------------------


def _comment(raw: object, prefix: str) -> tuple[m.Comment, dict[str, Any]]:
    node = _obj(raw, "a comment")
    reply = _opt_obj(node.get("replyTo"), "a reply target")
    review = _opt_obj(node.get("pullRequestReview"), "a review")
    comment = m.Comment(
        id=_prefixed(prefix, node.get("id"), "a comment id"),
        author=_author(node.get("author")),
        body=sanitize_text(_opt_str(node.get("body")) or ""),
        created_at=_req_ts(node.get("createdAt"), "a comment's creation time"),
        updated_at=_req_ts(node.get("updatedAt"), "a comment's update time"),
        minimized=node.get("isMinimized") is True,
        reply_to=(
            _prefixed("rc", reply.get("id"), "a reply target") if reply is not None else None
        ),
        review_id=(_prefixed("r", review.get("id"), "a review id") if review is not None else None),
    )
    return comment, node


def _check_review_comment(raw: object) -> None:
    node = _comment(raw, "rc")[1]
    _opt_obj(node.get("originalCommit"), "a commit")


def _check_thread(raw: object) -> None:
    """Everything a thread node of a page is read for, its first page of comments included."""
    node = _obj(raw, "a thread")
    _prefixed("rt", node.get("id"), "a thread id")
    comments = _obj(node.get("comments"), "the thread's comments")
    _page_shape(comments, "comments", _check_review_comment, frozenset())


def _thread(reader: _Reader, raw: object) -> tuple[m.Thread | None, bool]:
    """A review thread, or None when it holds no submitted comment; and whether it is whole."""
    node = _obj(raw, "a thread")
    thread_id = _prefixed("rt", node.get("id"), "a thread id")
    raw_id = _str(node.get("id"), "a thread id")
    connection = _obj(node.get("comments"), "the thread's comments")
    raw_nodes: list[Any] = []

    def nxt(cursor: str, seen: frozenset[str]) -> dict[str, Any]:
        return reader.page(
            THREAD_COMMENTS_PAGE_QUERY,
            {"id": raw_id, "after": cursor},
            "comments",
            "comments",
            _check_review_comment,
            seen=seen,
            thread_id=raw_id,
        )

    whole = _drain(connection, nxt, "comments", _check_review_comment, raw_nodes.append)
    parsed: list[tuple[m.Comment, dict[str, Any]]] = []
    for raw_comment in raw_nodes:
        comment, comment_node = _comment(raw_comment, "rc")
        state = comment_node.get("state")
        if state == "SUBMITTED":
            parsed.append((comment, comment_node))
        elif state != "PENDING":  # a pending review's comments belong to their author
            whole = False  # a state this adapter does not know: never shown as posted
            reader.env.warn("a review comment of an unknown state was left out")
    if not parsed:
        reader.env.warn("a review thread without a submitted comment was left out")
        return None, whole
    parsed.sort(key=lambda pair: pair[0].sort_key)
    first_node = parsed[0][1]
    outdated = _opt_bool(node.get("isOutdated"))
    on_file = node.get("subjectType") == "FILE"
    # a line of an outdated thread or of a file-level comment is not a line of the current diff
    line = None if outdated is True or on_file else _pos_int(node.get("line"))
    # the first comment's own range, never the thread's startLine
    start = _pos_int(first_node.get("startLine"))
    start_line = start if start is not None and line is not None and start < line else None
    path = _opt_str(node.get("path"))
    path = (sanitize_line(path) or None) if path is not None else None
    resolved_raw = node.get("isResolved")
    side_raw = node.get("diffSide")
    thread = m.Thread(
        id=thread_id,
        resolvable=True,
        resolved=resolved_raw is True,  # unknown is not resolved
        outdated=outdated,
        path=path,
        line=line,
        side=_SIDES.get(side_raw) if isinstance(side_raw, str) else None,
        start_line=start_line,
        commit_sha=_sha((_opt_obj(first_node.get("originalCommit"), "a commit") or {}).get("oid")),
        comments=tuple(c for c, _ in parsed),
    )
    return thread, whole


def _general_thread(raw: object) -> m.Thread:
    comment, _node = _comment_general(raw)
    return m.Thread(
        id=comment.id,
        resolvable=False,
        resolved=False,
        outdated=False,
        path=None,
        line=None,
        side=None,
        start_line=None,
        commit_sha=None,
        comments=(comment,),
    )


def _comment_general(raw: object) -> tuple[m.Comment, dict[str, Any]]:
    node = _obj(raw, "a comment")
    comment = m.Comment(
        id=_prefixed("ic", node.get("id"), "a comment id"),
        author=_author(node.get("author")),
        body=sanitize_text(_opt_str(node.get("body")) or ""),
        created_at=_req_ts(node.get("createdAt"), "a comment's creation time"),
        updated_at=_req_ts(node.get("updatedAt"), "a comment's update time"),
        minimized=node.get("isMinimized") is True,
        reply_to=None,
        review_id=None,
    )
    return comment, node


@dataclass(frozen=True, slots=True)
class ThreadsRead:
    threads: tuple[m.Thread, ...]
    complete: bool


def _threads_from(
    reader: _Reader, first_threads: dict[str, Any], first_general: dict[str, Any]
) -> ThreadsRead:
    threads: list[m.Thread] = []
    whole = True

    def take_thread(raw: Any) -> None:
        nonlocal whole
        thread, thread_whole = _thread(reader, raw)
        whole = whole and thread_whole
        if thread is not None:
            threads.append(thread)

    def next_threads(cursor: str, seen: frozenset[str]) -> dict[str, Any]:
        return reader.page(
            THREADS_PAGE_QUERY,
            {"id": reader.pr_id, "after": cursor},
            "reviewThreads",
            "threads",
            _check_thread,
            seen=seen,
        )

    def next_general(cursor: str, seen: frozenset[str]) -> dict[str, Any]:
        return reader.page(
            ISSUE_COMMENTS_PAGE_QUERY,
            {"id": reader.pr_id, "after": cursor},
            "comments",
            "comments",
            _comment_general,
            seen=seen,
        )

    whole = _drain(first_threads, next_threads, "threads", _check_thread, take_thread) and whole
    general = _drain(
        first_general,
        next_general,
        "comments",
        _comment_general,
        lambda raw: threads.append(_general_thread(raw)),
    )
    return ThreadsRead(tuple(threads), whole and general)


@dataclass(frozen=True, slots=True)
class Parts:
    """The first query's answer, normalized."""

    pr_id: str
    project: str
    head_sha: str
    reviews: ReviewsRead
    threads: ThreadsRead


def read_parts(reader: _Reader) -> Parts:
    """The parts query. With a project, head and id from `show` already on ``reader`` the answer
    must name the same; without them (``comments``) the answer's own become the reading's."""
    expected = reader.project

    def check(data: dict[str, Any]) -> dict[str, Any]:
        repo = _opt_obj(data.get("repository"), "the repository")
        if repo is None:
            raise ForgectlError("not_found", "no such repository, or no access to it")
        pr = _opt_obj(repo.get("pullRequest"), "the pull request")
        if pr is None:
            raise ForgectlError("not_found", "no such pull request")
        project = _project(repo.get("nameWithOwner"), "the repository path")
        if expected is not None and project != expected:
            raise Malformed("the repository path")
        reader.project = project
        reader.verify_pr(pr)
        reader.verify_id(pr)
        _page_shape(_obj(pr.get("reviews"), "the reviews"), "reviews", _check_review, frozenset())
        _page_shape(
            _obj(pr.get("reviewThreads"), "the threads"), "threads", _check_thread, frozenset()
        )
        _page_shape(
            _obj(pr.get("comments"), "the comments"), "comments", _comment_general, frozenset()
        )
        return pr

    data = reader.gql(
        PARTS_QUERY,
        {"owner": reader.owner, "name": reader.name, "number": reader.ref.number},
        check,
    )
    pr = check(data)
    project = reader.project or ""
    pr_id = reader.verify_id(pr)
    head = _req_sha(pr.get("headRefOid"), "the head sha")
    reader.pr_id = pr_id
    reader.head = head
    reviews = _reviews_from(reader, _obj(pr.get("reviews"), "the reviews"))
    threads = _threads_from(
        reader,
        _obj(pr.get("reviewThreads"), "the threads"),
        _obj(pr.get("comments"), "the comments"),
    )
    return Parts(pr_id, project, head, reviews, threads)


# -- review requests ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Who:
    """A requested reviewer as read: how it is compared and what the output names."""

    kind: Literal["user", "team"]
    login: str
    id: str | None

    def same(self, other: _Who) -> bool:
        if self.kind != other.kind:
            return False
        if self.id is not None and other.id is not None:
            return self.id == other.id
        return m.ascii_lower(self.login) == m.ascii_lower(other.login)


def _account_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    text = str(value)
    return text if _ACCOUNT.fullmatch(text) else None


def _team_login(slug: object) -> str | None:
    """A team's login is its slug: every team on a PR belongs to the repository's organization,
    so the slug is unique within it, and ``kind`` tells it from a user of the same name."""
    return slug if isinstance(slug, str) and _TEAM_SLUG.fullmatch(slug) else None


#: What a requested reviewer node came to: a reviewer, nothing readable (a team the token cannot
#: see, or a login that is not a GitHub login), or an unreadable team stub.
_HIDDEN = "hidden"
_UNNAMED = "unnamed"

#: The reviewer types the fragments read; any other (`EnterpriseTeam`, or one GitHub adds later)
#: cannot be named. A fragment on a type an older GHES does not know would fail every query.
_NAMED_TYPES = frozenset({"User", "Bot", "Mannequin", "Team"})
_TYPENAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def _who(raw: object) -> _Who | str:
    if raw is None:
        return _HIDDEN
    node = _obj(raw, "a requested reviewer")
    typename = node.get("__typename")
    if typename == "Team":
        if "slug" not in node:
            return _HIDDEN  # sent without the team fields
        login = _team_login(node.get("slug"))
        if login is None:
            return _UNNAMED
        return _Who("team", login, _account_id(node.get("databaseId")))
    actor = parse_actor(node)
    if actor is None or actor.login is None:
        return _UNNAMED
    return _Who("user", actor.login, actor.id)


def _unnamed_warning(raw: object) -> str:
    typename = raw.get("__typename") if isinstance(raw, dict) else None
    if isinstance(typename, str) and typename not in _NAMED_TYPES and _TYPENAME.fullmatch(typename):
        return f"a review request of a reviewer type forgectl does not read ({typename}) is absent"
    return "a review request whose reviewer cannot be named is absent"


@dataclass(frozen=True, slots=True)
class _Event:
    who: _Who
    instance: str
    at: str | None


def _event(raw: object) -> _Event | None:
    node = _obj(raw, "a timeline item")
    if node.get("__typename") != "ReviewRequestedEvent":
        return None
    ident = node.get("id")
    if not isinstance(ident, str) or not _INSTANCE.fullmatch(ident):
        raise Malformed("a review request event id")
    who = _who(node.get("requestedReviewer"))
    if isinstance(who, str):
        return None
    return _Event(who, ident, _ts(node.get("createdAt")))


def _scope_failure(errors: tuple[Any, ...]) -> bool:
    """Whether every error says the token may not read a team field."""
    if not errors:
        return False
    for error in errors:
        if not isinstance(error, dict) or error.get("type") not in _SCOPE_ERROR_TYPES:
            return False
        path = error.get("path")
        names_team = isinstance(path, list) and "requestedReviewer" in path
        message = error.get("message")
        if not names_team and not (isinstance(message, str) and "team" in message.lower()):
            return False
    return True


@dataclass(frozen=True, slots=True)
class RequestsRead:
    requests: tuple[m.ReviewRequest, ...]
    teams_visible: bool
    complete: bool


def _check_requests(reader: _Reader, data: Mapping[str, Any]) -> dict[str, Any]:
    """Everything the requests answer is read for; the pull request object."""
    repo = _obj(data.get("repository"), "the repository")
    pr = reader.verify_pr(repo.get("pullRequest"))
    reader.verify_id(pr)
    listed = _obj(pr.get("reviewRequests"), "the review requests")
    _obj(listed.get("pageInfo"), "the requests page")
    for raw in _list(listed.get("nodes"), "the review requests"):
        _who(_obj(raw, "a review request").get("requestedReviewer"))
    _page_shape(
        _obj(pr.get("timelineItems"), "the request events"),
        "request events",
        _event,
        frozenset(),
        backward=True,
    )
    return pr


def read_requests(reader: _Reader) -> RequestsRead:
    variables = {"owner": reader.owner, "name": reader.name, "number": reader.ref.number}
    gateway = reader.env.gateway

    def ask(query: str, *, allow_errors: bool) -> GraphQLResult:
        def once() -> GraphQLResult:
            result = gateway.graphql(reader.target, query, variables, allow_errors=allow_errors)
            if not result.errors:  # a partial answer is never read: it is sent again
                _guarded(lambda: _check_requests(reader, dict(result.data)))
            return result

        return reader.env.call(once)

    teams = REQUESTS_QUERY
    result = ask(teams, allow_errors=True)
    if result.errors:
        if _scope_failure(result.errors):
            reader.team_fallback = True
            teams = REQUESTS_QUERY_NO_TEAMS
            result = ask(teams, allow_errors=True)
        if result.errors:  # any other failure is a failed read, with its own classification
            result = ask(teams, allow_errors=False)
    data = dict(result.data)
    reader.limits.note_graphql(data)
    pr = _check_requests(reader, data)

    listed = _obj(pr.get("reviewRequests"), "the review requests")
    complete = (
        _opt_bool(_obj(listed.get("pageInfo"), "the requests page").get("hasNextPage")) is False
    )
    visible = not reader.team_fallback
    # the GraphQL list leaves a reviewer out: who `requested_reviewers` adds may be that one,
    # whose events cannot name it either, so it does not keep the event walk going
    gap = False
    people: list[tuple[_Who, bool | None]] = []
    nodes = _list(listed.get("nodes"), "the review requests")
    for raw in nodes:
        node = _obj(raw, "a review request")
        who = _who(node.get("requestedReviewer"))
        if who == _HIDDEN:
            visible = False
            gap = True
        elif who == _UNNAMED:
            complete = False  # a request that cannot be named is absent from the list
            gap = True
            reader.env.warn(_unnamed_warning(node.get("requestedReviewer")))
        elif isinstance(who, _Who):
            people.append((who, _opt_bool(node.get("asCodeOwner"))))
    required = [who for who, _ in people]

    # the REST list: the teams the token could not read, and the check on the count
    rest_who, rest_ok, rest_total, rest_full = _rest_requested(reader)
    if not rest_ok or rest_total != len(nodes):
        visible = False
    if not rest_full:
        complete = False
    for who in rest_who:
        if not any(known.same(who) for known, _ in people):
            people.append((who, None))
            if not gap:
                required.append(who)

    # instances: the newest event of each reviewer, paging back until every one is found
    latest: list[_Event] = []
    everyone = [who for who, _ in people]
    seen: set[str] = set()
    connection = _obj(pr.get("timelineItems"), "the request events")
    for index in range(MAX_PAGES):
        more, cursor = _page_shape(connection, "request events", _event, seen, backward=True)
        for raw in reversed(_list(connection.get("nodes"), "the request events")):
            event = _event(raw)
            if event is None or any(event.who.same(e.who) for e in latest):
                continue
            if any(event.who.same(w) for w in everyone):
                latest.append(event)
        if all(any(e.who.same(w) for e in latest) for w in required):
            break
        if not more or cursor is None:
            break
        if index == MAX_PAGES - 1:
            complete = False  # cut off before every reviewer was found
            break
        seen.add(cursor)
        connection = reader.page(
            REQUEST_EVENTS_QUERY_NO_TEAMS if reader.team_fallback else REQUEST_EVENTS_QUERY,
            {"id": reader.pr_id, "before": cursor},
            "timelineItems",
            "request events",
            _event,
            seen=frozenset(seen),
            backward=True,
        )
    out: list[m.ReviewRequest] = []
    done: list[_Who] = []
    for who, code_owner in people:
        if any(who.same(s) for s in done):
            continue
        done.append(who)
        event = next((e for e in latest if e.who.same(who)), None)
        out.append(
            m.ReviewRequest(
                reviewer=m.Reviewer(login=who.login, id=who.id, kind=who.kind),
                state="outstanding",
                code_owner=code_owner,
                instance=event.instance if event is not None else None,
                requested_at=event.at if event is not None else None,
            )
        )
    return RequestsRead(tuple(out), visible, complete)


def _rest_body(response: Response) -> tuple[list[_Who], bool, int, bool]:
    body = _obj(response.data, "the requested reviewers")
    users = _list(body.get("users"), "the requested users")
    teams = _list(body.get("teams"), "the requested teams")
    who: list[_Who] = []
    for raw in users:
        node = _obj(raw, "a requested user")
        kind = node.get("type")
        login = _opt_str(node.get("login"))
        name = rest_login("Bot" if kind == "Bot" else None, login)
        if name is not None:
            who.append(_Who("user", name, _account_id(node.get("id"))))
    for raw in teams:
        node = _obj(raw, "a requested team")
        login = _team_login(node.get("slug"))
        if login is not None:
            who.append(_Who("team", login, _account_id(node.get("id"))))
    scopes = response.headers.get("x-oauth-scopes", "").strip()
    reports_scopes = bool(scopes)
    full = len(users) < 100 and len(teams) < 100
    return who, reports_scopes, len(users) + len(teams), full


def _rest_requested(reader: _Reader) -> tuple[list[_Who], bool, int, bool]:
    """``GET .../requested_reviewers``: who it names, whether it was read and the token reports
    its scopes, how many it names, and whether the list is whole. The body is checked inside the
    retried call."""
    gateway = reader.env.gateway

    def once() -> tuple[Response, tuple[list[_Who], bool, int, bool]]:
        response = gateway.api(
            reader.target,
            "GET",
            ["repos", reader.owner, reader.name, "pulls", str(reader.ref.number)]
            + ["requested_reviewers"],
            query=[("per_page", "100")],
            shape="object",
        )
        return response, _guarded(lambda: _rest_body(response))

    try:
        response, read = reader.env.call(once)
    except ForgectlError as error:
        if error.code in ("permission_denied", "not_found"):
            return [], False, 0, True
        raise
    reader.limits.note_headers(response.headers)
    return read


# -- the head's push time ----------------------------------------------------------------------

#: A host time later than the reading by more than this is not trusted (a clock skew allowance).
FUTURE_SKEW = timedelta(minutes=5)


def read_head_push(reader: _Reader, head: str, now: Now = utc_now) -> m.PushTime | None:
    """When the head was pushed, else None (with a warning when an answer could not be read).

    A ``HeadRefForcePushedEvent`` to this head sha gives its ``createdAt``, a host time, as
    ``push_event``; with none, the newest ``PullRequestCommit`` of this sha gives its committer
    date as ``push_event``; with neither, the head commit's own date is ``commit_date``. A
    committer date is chosen by whoever made the commit, so it never wins over a host event, and
    any time later than the reading plus ``FUTURE_SKEW`` is not trusted (interface: Push time).

    Every page is checked inside its retried call; once the retries are spent on an answer that
    is not the documented shape, the push time is unknown and the snapshot is not failed. A wrong
    host stays ``wrong_host_response``; another head is ``_Moved``.
    """
    try:
        return _read_head_push(reader, head, now)
    except ForgectlError as error:
        if error.code == "upstream_error" and reader.last_malformed:
            reader.env.warn("the push time of the head could not be read")
            return None
        raise


def _read_head_push(reader: _Reader, head: str, now: Now) -> m.PushTime | None:
    def item(raw: object) -> object:
        return _push_item(raw, head)

    def check(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        repo = _obj(data.get("repository"), "the repository")
        pr = reader.verify_pr(repo.get("pullRequest"))
        reader.verify_id(pr)
        _opt_obj(repo.get("object"), "the head commit")
        _page_shape(
            _obj(pr.get("timelineItems"), "the timeline"),
            "timeline",
            item,
            frozenset(),
            backward=True,
        )
        return repo, pr

    data = reader.gql(
        PUSH_QUERY,
        {"owner": reader.owner, "name": reader.name, "number": reader.ref.number, "head": head},
        check,
    )
    repo, pr = check(data)
    forced: tuple[datetime, str] | None = None
    committed: tuple[datetime, str] | None = None
    seen: set[str] = set()
    connection = _obj(pr.get("timelineItems"), "the timeline")
    for index in range(MAX_PUSH_PAGES):
        more, cursor = _page_shape(connection, "timeline", item, seen, backward=True)
        for raw in _list(connection.get("nodes"), "the timeline"):
            found = _push_item(raw, head)
            if found is None:
                continue
            kind, when = found
            parsed = _parse_time(when)
            if parsed is None:
                continue
            if kind == "forced" and (forced is None or parsed > forced[0]):
                forced = (parsed, when)
            elif kind == "commit" and (committed is None or parsed > committed[0]):
                committed = (parsed, when)
        if forced is not None or committed is not None:
            break
        if not more or cursor is None or index == MAX_PUSH_PAGES - 1:
            break
        seen.add(cursor)
        connection = reader.page(
            PUSH_PAGE_QUERY,
            {"id": reader.pr_id, "before": cursor},
            "timelineItems",
            "timeline",
            item,
            seen=frozenset(seen),
            backward=True,
        )
    best = forced or committed
    found_time: m.PushTime | None = None
    when_parsed: datetime | None = None
    if best is not None:
        when_parsed, found_time = best[0], m.PushTime(at=best[1], source="push_event")
    else:
        commit = _opt_obj(repo.get("object"), "the head commit")
        if commit is not None and commit.get("oid") == head:
            date = _ts(commit.get("committedDate"))
            parsed = _parse_time(date) if date is not None else None
            if date is not None and parsed is not None:
                when_parsed, found_time = parsed, m.PushTime(at=date, source="commit_date")
    if when_parsed is not None and when_parsed > now() + FUTURE_SKEW:
        reader.env.warn("the push time of the head lies in the future and is not trusted")
        return None
    return found_time


def _push_item(raw: object, head: str) -> tuple[Literal["forced", "commit"], str] | None:
    node = _obj(raw, "a timeline item")
    kind = node.get("__typename")
    if kind == "HeadRefForcePushedEvent":
        after = _opt_obj(node.get("afterCommit"), "a commit")
        when = _ts(node.get("createdAt"))
        if after is not None and after.get("oid") == head and when is not None:
            return "forced", when
    elif kind == "PullRequestCommit":
        commit = _opt_obj(node.get("commit"), "a commit")
        when = _ts(commit.get("committedDate")) if commit is not None else None
        if commit is not None and commit.get("oid") == head and when is not None:
            return "commit", when
    return None


# -- the commands ------------------------------------------------------------------------------


def _reader(env: ReadEnv, ref: PrRef) -> _Reader:
    owner, name = _split(ref)
    if ref.number > MAX_PR_NUMBER:
        raise ForgectlError("not_found", "no such pull request")
    return _Reader(
        env=env,
        target=env.hosts.target(ref.host),
        ref=ref,
        owner=owner,
        name=name,
        limits=Limits(host=ref.host),
    )


def _guarded[T](read: Callable[[], T]) -> T:
    try:
        return read()
    except Malformed as bad:
        raise malformed(f"{bad}") from None
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise malformed("data") from None


def _moved() -> ForgectlError:
    return ForgectlError(
        "upstream_error", "the pull request changed while it was being read; read it again"
    )


def _twice[T](read: Callable[[], T]) -> T:
    """``read``, repeated once when the PR moved during it, then ``upstream_error``."""
    for attempt in range(2):
        try:
            return _guarded(read)
        except _Moved:
            if attempt == 1:
                raise _moved() from None
    raise _moved()  # unreachable: the second attempt returns or raises


def _from_show(reader: _Reader, read: m.PrRead) -> None:
    """Every later answer must name the project, head and node id `show` read."""
    reader.project = read.pr.repo.partition("/")[2]  # `host/owner/repo`, canonical from show
    reader.head = read.pr.head.sha
    reader.pr_id = read.pr.pr_id


def snapshot(
    env: ReadEnv, ref: PrRef, get_pr: Callable[[PrRef], m.PrRead], now: Now = utc_now
) -> m.Snapshot:
    def once() -> m.Snapshot:
        reader = _reader(env, ref)
        read = get_pr(ref)
        return _snapshot_once(reader, read, now)

    return _twice(once)


def _snapshot_once(reader: _Reader, read: m.PrRead, now: Now) -> m.Snapshot:
    pr = read.pr
    head = pr.head.sha
    _from_show(reader, read)
    parts = read_parts(reader)
    if parts.head_sha != head:
        raise _Moved
    # a review that landed between the two reads: `show`'s decision, approvals and blocks would
    # contradict this table. Only a list read to its end can be compared.
    if parts.reviews.drained and not _same_table(parts.reviews.latest, read.latest_by_reviewer):
        raise _Moved
    requests = read_requests(reader)
    head_push = read_head_push(reader, head, now)
    incomplete: list[str] = []
    if not requests.complete:
        incomplete.append("review_requests")
    if not parts.reviews.complete:
        incomplete.append("reviews")
    if not parts.threads.complete:
        incomplete.append("threads")
    # GitHub: comments from `threads`, reviews from `reviews` (counted at `submitted_at`); no
    # `approvals`, which `latest_by_reviewer` would count a second time.
    activity = derive_activity(
        reader.env,
        reader.ref.host,
        threads=parts.threads.threads,
        reviews=parts.reviews.reviews,
        approvals=(),
        head_push=head_push,
    )
    return m.Snapshot(
        pr=pr,
        review_requests=requests.requests,
        teams_visible=requests.teams_visible,
        reviews=parts.reviews.reviews,
        latest_by_reviewer=parts.reviews.latest,
        threads=parts.threads.threads,
        ci=read.ci,
        rate_limit=reader.limits.result(),
        fetched_at=_stamp(now()),
        incomplete=tuple(incomplete),
        head_push=head_push,
        activity=activity,
    )


def comments(env: ReadEnv, ref: PrRef) -> m.CommentsResult:
    def once() -> m.CommentsResult:
        reader = _reader(env, ref)
        parts = read_parts(reader)
        return m.CommentsResult(
            pr=f"{ref.host}/{parts.project}#{ref.number}",
            head_sha=parts.head_sha,
            threads=parts.threads.threads,
            complete=parts.threads.complete,
        )

    return _twice(once)


def reviews(env: ReadEnv, ref: PrRef, get_pr: Callable[[PrRef], m.PrRead]) -> m.ReviewsResult:
    def once() -> m.ReviewsResult:
        reader = _reader(env, ref)
        read = get_pr(ref)
        return _reviews_once(reader, read)

    return _twice(once)


def _reviews_once(reader: _Reader, read: m.PrRead) -> m.ReviewsResult:
    pr = read.pr
    _from_show(reader, read)
    first = reader.page(
        REVIEWS_PAGE_QUERY, {"id": pr.pr_id, "after": None}, "reviews", "reviews", _check_review
    )
    got = _reviews_from(reader, first)
    if got.drained and not _same_table(got.latest, read.latest_by_reviewer):
        raise _Moved
    return m.ReviewsResult(
        pr=pr.pr,
        head_sha=pr.head.sha,
        reviews=got.reviews,
        latest_by_reviewer=got.latest,
        review_decision=pr.review_decision,
        approvals=pr.approvals,
        approvals_at_head=pr.approvals_at_head,
        complete=got.complete,
    )


def ci(ref: PrRef, get_pr: Callable[[PrRef], m.PrRead]) -> m.CiResult:
    """The checks come from the same read as ``show``, which reads them for ``headRefOid``
    exactly and says when the host cut the list off."""
    read = get_pr(ref)
    return m.CiResult(pr=read.pr.pr, head_sha=read.pr.head.sha, ci=read.ci)
