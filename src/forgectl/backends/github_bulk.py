"""GitHub bulk snapshots (#116): many PRs of one host from a few GraphQL requests.

One request asks a page of PRs through aliased ``repository { pullRequest }`` fields and carries
what ``snapshot`` reads in six calls: the ``show`` fields, the reviews, threads and general
comments, the review requests and their events, and the force pushes of the head. Every answer
is turned into the same objects as the per-PR path by the same code: ``GitHubBackend._normalize``
builds the PR and its CI from the ``show`` fields, and ``github_snapshot`` parses the reviews,
threads and requests and derives ``activity``. This module only supplies the normalized input
and decides when an answer is not whole.

A PR is never given partial: every connection of the query is bounded and carries ``pageInfo``;
one that has another page (or a ``mergeStateStatus`` still being computed, a head that is not
the last commit, a requested team the token cannot read, ...) makes the PR ``Unresolved`` and
``forgectl.bulk`` reads it with ``snapshot``. Differences from ``snapshot`` (interface:
``snapshots``): ``ci.checks[].required`` is null, since ``isRequired`` takes the PR number as an
argument; the review requests come from GraphQL only.

Page size. ``mergeable`` and ``mergeStateStatus`` are the expensive fields: a page of 50 returns
HTTP 502 after about 10 s. Pages start at ``PAGE_SIZE``; a page that fails is halved and each half
asked once more (``bulk_common.run_pages``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from .. import model as m
from ..activity import for_snapshot as derive_activity
from ..errors import ForgectlError
from ..gateway import verify_identity
from ..identifiers import PrRef
from ..reads import ReadEnv
from . import github_snapshot as gs
from .bulk_common import BULK_UNAVAILABLE, BulkRead, Unresolved, run_pages
from .github import (
    _ACTOR_FIELDS,
    MAX_PR_NUMBER,
    GitHubBackend,
    Malformed,
    _is_unknown,
    _obj,
    _opt_obj,
    _project,
    _Shown,
    _split,
    _url_path,
)

#: PRs asked in one request while ``mergeable`` and ``mergeStateStatus`` are in the query.
PAGE_SIZE = 25

#: The bound of every connection of the query. A connection with another page is a cut part.
REVIEWS = 30
THREADS = 30
THREAD_COMMENTS = 10
COMMENTS = 30
CONTEXTS = 50
REQUESTS = 30
REQUEST_EVENTS = 30
FORCE_PUSHES = 20
LABELS = 100

_THREADS_PAGE = gs._THREADS_PAGE.replace(
    "comments(first: 50)", f"comments(first: {THREAD_COMMENTS})"
)
if _THREADS_PAGE == gs._THREADS_PAGE:  # the shared fragment changed: this module must follow
    raise RuntimeError("the thread fragment no longer holds `comments(first: 50)`")

_FRAGMENTS = f"""
fragment BulkPr on PullRequest {{
  id
  url
  number
  title
  body
  state
  isDraft
  createdAt
  updatedAt
  closedAt
  mergedAt
  author {{ ...ActorFields }}
  mergedBy {{ ...ActorFields }}
  authorAssociation
  labels(first: {LABELS}) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ name }}
  }}
  headRefName
  headRefOid
  baseRefName
  baseRefOid
  headRepository {{ nameWithOwner }}
  isCrossRepository
  mergeCommit {{ oid }}
  mergeable
  mergeStateStatus
  isMergeQueueEnabled
  autoMergeRequest {{ enabledBy {{ ...ActorFields }} }}
  additions
  deletions
  changedFiles
  reviewDecision
  reviews(first: {REVIEWS}) {{
    pageInfo {{ hasNextPage hasPreviousPage endCursor startCursor }}
    nodes {{
      id
      state
      submittedAt
      body
      authorCanPushToRepository
      author {{ ...ActorFields }}
      commit {{ oid }}
    }}
  }}
  reviewThreads(first: {THREADS}) {{ ...ThreadsPage }}
  comments(first: {COMMENTS}) {{ ...IssueCommentsPage }}
  commits(last: 1) {{
    nodes {{
      commit {{
        oid
        committedDate
        statusCheckRollup {{
          contexts(first: {CONTEXTS}) {{
            pageInfo {{ hasNextPage endCursor }}
            nodes {{
              __typename
              ... on CheckRun {{ name status conclusion detailsUrl completedAt }}
              ... on StatusContext {{ context state targetUrl createdAt }}
            }}
          }}
        }}
      }}
    }}
  }}
  retargets: timelineItems(
    last: 1
    itemTypes: [BASE_REF_CHANGED_EVENT, AUTOMATIC_BASE_CHANGE_SUCCEEDED_EVENT]
  ) {{
    nodes {{
      __typename
      ... on BaseRefChangedEvent {{ createdAt }}
      ... on AutomaticBaseChangeSucceededEvent {{ createdAt }}
    }}
  }}
  reviewRequests(first: {REQUESTS}) {{
    pageInfo {{ hasNextPage }}
    nodes {{ asCodeOwner requestedReviewer {{ ...ReviewerFields }} }}
  }}
  requestEvents: timelineItems(last: {REQUEST_EVENTS}, itemTypes: [REVIEW_REQUESTED_EVENT]) {{
    pageInfo {{ hasPreviousPage startCursor }}
    nodes {{
      __typename
      ... on ReviewRequestedEvent {{ id createdAt requestedReviewer {{ ...ReviewerFields }} }}
    }}
  }}
  pushEvents: timelineItems(last: {FORCE_PUSHES}, itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT]) {{
    pageInfo {{ hasPreviousPage startCursor }}
    nodes {{
      __typename
      ... on HeadRefForcePushedEvent {{ createdAt afterCommit {{ oid }} }}
    }}
  }}
}}
"""

_SHARED = (
    _FRAGMENTS
    + _ACTOR_FIELDS
    + _THREADS_PAGE
    + gs._THREAD_COMMENTS
    + gs._ISSUE_COMMENTS_PAGE
    + gs._REVIEWER_WITH_TEAMS
)


def bulk_query(count: int) -> str:
    """The request for ``count`` PRs. Owner, name and number travel as variables (``$o0``,
    ``$n0``, ``$k0``, ...); only the aliases ``p0``... and these names depend on ``count``."""
    declared = ", ".join(f"$o{i}: String!, $n{i}: String!, $k{i}: Int!" for i in range(count))
    fields = "\n".join(
        f"  p{i}: repository(owner: $o{i}, name: $n{i}) {{"
        " nameWithOwner defaultBranchRef { name }"
        " squashMergeAllowed mergeCommitAllowed rebaseMergeAllowed"
        f" pullRequest(number: $k{i}) {{ ...BulkPr }} }}"
        for i in range(count)
    )
    return (
        f"query ForgectlBulk({declared}) {{\n  rateLimit {{ cost remaining resetAt }}\n"
        f"{fields}\n}}\n{_SHARED}"
    )


def _variables(page: Sequence[PrRef]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for i, ref in enumerate(page):
        owner, name = _split(ref)
        out.update({f"o{i}": owner, f"n{i}": name, f"k{i}": ref.number})
    return out


def _chunk_error(errors: Sequence[Any]) -> ForgectlError:
    """The failure a GraphQL ``errors`` list that names no PR amounts to."""
    types = {e.get("type") for e in errors if isinstance(e, dict)}
    if "RATE_LIMITED" in types:
        return ForgectlError("rate_limited", "GraphQL: rate limited")
    if "FORBIDDEN" in types:
        return ForgectlError("permission_denied", "GraphQL: forbidden")
    return ForgectlError("upstream_error", "GraphQL: the host answered errors for the page")


def _alias_errors(errors: Sequence[Any], count: int) -> dict[int, list[dict[str, Any]]]:
    """The errors that name one alias (``p<i>`` first in their path), by index. An error that
    names none raises: the page as a whole failed."""
    named: dict[int, list[dict[str, Any]]] = {}
    for error in errors:
        path = error.get("path") if isinstance(error, dict) else None
        head = path[0] if isinstance(path, list) and path else None
        index = int(head[1:]) if isinstance(head, str) and head[1:].isdigit() else -1
        if not isinstance(error, dict) or not 0 <= index < count:
            raise _chunk_error(errors)
        named.setdefault(index, []).append(error)
    return named


class _Run:
    """One host's bulk run: its target, rate-limit reading and the page asker."""

    def __init__(self, env: ReadEnv, host: str, now: Callable[[], datetime]) -> None:
        self.env = env
        self.host = host
        self.now = now
        self.target = env.hosts.target(host)
        self.limits = gs.Limits(host=host)
        self.backend = GitHubBackend(env)

    # -- one request ---------------------------------------------------------------------------

    def _graphql(self, page: Sequence[PrRef]) -> tuple[dict[str, Any], tuple[Any, ...]]:
        """The page's answer, after the retries of a rate limit only: any other failure is the
        caller's, which halves the page instead of asking the same again."""
        gateway = self.env.gateway
        retries = self.env.retries
        query = bulk_query(len(page))
        variables = _variables(page)
        attempt = 0
        while True:
            if gateway.budget.remaining() <= 0:
                raise ForgectlError("network_error", "the time budget ran out before the call")
            try:
                result = gateway.graphql(self.target, query, variables, allow_errors=True)
                return dict(result.data), result.errors
            except ForgectlError as error:
                if error.code != "rate_limited" or attempt >= retries:
                    raise
                delay = self.env.backoff(attempt)
                if error.retry_after is not None:
                    delay = max(delay, error.retry_after)
                if delay + 1.0 > gateway.budget.remaining():
                    raise
                self.env.wait(delay)
                attempt += 1

    def ask(self, page: list[PrRef], result: BulkRead) -> None:
        data, errors = self._graphql(page)
        named = _alias_errors(errors, len(page)) if errors else {}
        info = data.get("rateLimit")
        if isinstance(info, dict):
            self.limits.note_graphql(data)
        built: list[tuple[PrRef, m.Snapshot | Unresolved]] = []
        for i, ref in enumerate(page):
            problems = named.get(i, [])
            try:
                built.append((ref, self._item(ref, data.get(f"p{i}"), problems)))
            except Unresolved as why:
                built.append((ref, why))
            except Malformed as bad:
                built.append((ref, Unresolved(BULK_UNAVAILABLE, f"unusable answer: {bad}")))
            except ForgectlError as error:
                if error.code in ("wrong_host_response",):
                    raise
                code = BULK_UNAVAILABLE if error.code == "upstream_error" else error.code
                built.append((ref, Unresolved(code, error.message)))
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                built.append((ref, Unresolved(BULK_UNAVAILABLE, "unusable answer")))
        # the page is whole: nothing is stored before every alias has been looked at
        for ref, outcome in built:
            if isinstance(outcome, Unresolved):
                result.unresolved[ref] = outcome
            else:
                result.snapshots[ref] = outcome

    # -- one PR --------------------------------------------------------------------------------

    def _item(self, ref: PrRef, repo_raw: object, problems: list[dict[str, Any]]) -> m.Snapshot:
        if any(p.get("type") == "NOT_FOUND" for p in problems) or (
            repo_raw is None and not problems
        ):
            raise Unresolved("not_found", "no such repository, or no access to it, or no such PR")
        if problems:  # a partial answer for this PR (a field the token may not read)
            raise Unresolved(BULK_UNAVAILABLE, "the host answered errors for this pull request")
        repo = _obj(repo_raw, "the repository")
        pr_raw = _opt_obj(repo.get("pullRequest"), "the pull request")
        if pr_raw is None:
            raise Unresolved("not_found", "no such pull request")
        if ref.number > MAX_PR_NUMBER:
            raise Unresolved("not_found", "no such pull request")
        owner, name = _split(ref)
        project = _project(repo.get("nameWithOwner"), "the repository path")
        number = pr_raw.get("number")
        if isinstance(number, bool) or number != ref.number:
            raise Malformed("the pull request number")
        verify_identity(
            pr_raw.get("url"),
            host=self.target.host,
            project=project,
            secret=self.target.token,
        )
        _url_path(pr_raw.get("url"), project, f"/pull/{ref.number}")
        _check_whole(pr_raw)
        pr = dict(pr_raw)
        pr["timelineItems"] = pr.pop("retargets")
        shown = _Shown(repo=dict(repo), pr=pr, project=project)
        if _is_unknown(shown):
            raise Unresolved(BULK_UNAVAILABLE, "mergeability is still being computed")
        read = self.backend._normalize(self.target, ref, owner, name, shown, False, more=False)
        return self._snapshot(ref, owner, name, project, pr, read)

    def _snapshot(
        self,
        ref: PrRef,
        owner: str,
        name: str,
        project: str,
        pr: dict[str, Any],
        read: m.PrRead,
    ) -> m.Snapshot:
        reader = gs._Reader(
            env=self.env,
            target=self.target,
            ref=ref,
            owner=owner,
            name=name,
            limits=self.limits,
        )
        reader.project = project
        reviews = gs._reviews_from(reader, _obj(pr.get("reviews"), "the reviews"))
        threads = gs._threads_from(
            reader,
            _obj(pr.get("reviewThreads"), "the threads"),
            _obj(pr.get("comments"), "the comments"),
        )
        head = read.pr.head.sha
        requests, teams_visible = _requests(pr)
        head_push = _head_push(pr, head, self.env, self.now)
        incomplete: list[str] = []
        if not reviews.complete:
            incomplete.append("reviews")
        if not threads.complete:
            incomplete.append("threads")
        activity = derive_activity(
            self.env,
            ref.host,
            threads=threads.threads,
            reviews=reviews.reviews,
            approvals=(),
            head_push=head_push,
        )
        return m.Snapshot(
            pr=read.pr,
            review_requests=requests,
            teams_visible=teams_visible,
            reviews=reviews.reviews,
            latest_by_reviewer=reviews.latest,
            threads=threads.threads,
            ci=read.ci,
            rate_limit=self.limits.result(),
            fetched_at=gs._stamp(self.now()),
            incomplete=tuple(incomplete),
            head_push=head_push,
            activity=activity,
        )


# -- completeness ------------------------------------------------------------------------------


def _more(connection: object, flag: str = "hasNextPage") -> bool | None:
    """Whether ``connection`` has another page; None when its page info is not usable."""
    if not isinstance(connection, dict):
        return None
    info = connection.get("pageInfo")
    value = info.get(flag) if isinstance(info, dict) else None
    return value if isinstance(value, bool) else None


def _check_whole(pr: Mapping[str, Any]) -> None:
    """Raise ``Unresolved`` unless every bounded part of the PR was read to its end."""
    cut: list[str] = []

    def full(connection: object, what: str, flag: str = "hasNextPage") -> None:
        more = _more(connection, flag)
        if more is None:
            raise Malformed(f"the {what} page")
        if more:
            cut.append(what)

    full(pr.get("labels"), "labels")
    full(pr.get("reviews"), "reviews")
    full(pr.get("reviewThreads"), "review threads")
    for thread in _list_of(_obj(pr.get("reviewThreads"), "the threads").get("nodes")):
        full(_obj(thread, "a thread").get("comments"), "thread comments")
    full(pr.get("comments"), "comments")
    full(pr.get("reviewRequests"), "review requests")
    full(pr.get("requestEvents"), "review request events", "hasPreviousPage")
    # the checks are those of the head commit only, and every context of them
    commits = _obj(pr.get("commits"), "the commits")
    nodes = _list_of(commits.get("nodes"))
    head = pr.get("headRefOid")
    last = _opt_obj(_obj(nodes[-1], "a commit").get("commit"), "a commit") if nodes else None
    if last is None or last.get("oid") != head:
        raise Unresolved(BULK_UNAVAILABLE, "the last commit is not the head")
    rollup = _opt_obj(last.get("statusCheckRollup"), "the check rollup")
    if rollup is not None:
        full(rollup.get("contexts"), "check contexts")
    if cut:
        raise Unresolved(BULK_UNAVAILABLE, "more than the page holds of: " + ", ".join(cut))


def _list_of(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise Malformed("a list")
    return value


# -- review requests ---------------------------------------------------------------------------


def _requests(pr: Mapping[str, Any]) -> tuple[tuple[m.ReviewRequest, ...], bool]:
    """The outstanding review requests and their newest events, from GraphQL alone: the logic of
    ``github_snapshot.read_requests`` without its REST read of the teams a token cannot see. A
    reviewer that cannot be named (a team the token cannot read, an unknown type) makes the PR
    ``Unresolved``: only ``snapshot``'s second read can say whether anything is missing."""
    listed = _obj(pr.get("reviewRequests"), "the review requests")
    people: list[tuple[gs._Who, bool | None]] = []
    for raw in _list_of(listed.get("nodes")):
        node = _obj(raw, "a review request")
        who = gs._who(node.get("requestedReviewer"))
        if not isinstance(who, gs._Who):
            raise Unresolved(BULK_UNAVAILABLE, "a requested reviewer cannot be named")
        people.append((who, gs._opt_bool(node.get("asCodeOwner"))))
    latest: list[gs._Event] = []
    everyone = [who for who, _ in people]
    events = _obj(pr.get("requestEvents"), "the request events")
    for raw in reversed(_list_of(events.get("nodes"))):
        event = gs._event(raw)
        if event is None or any(event.who.same(e.who) for e in latest):
            continue
        if any(event.who.same(w) for w in everyone):
            latest.append(event)
    found_all = all(any(e.who.same(w) for e in latest) for w in everyone)
    if not found_all and _more(events, "hasPreviousPage") is not False:
        raise Unresolved(BULK_UNAVAILABLE, "the request events are cut before every reviewer")
    out: list[m.ReviewRequest] = []
    done: list[gs._Who] = []
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
    return tuple(out), True


# -- the head's push time ----------------------------------------------------------------------


def _head_push(
    pr: Mapping[str, Any], head: str, env: ReadEnv, now: Callable[[], datetime]
) -> m.PushTime | None:
    """``read_head_push`` over the newest force pushes and the head commit's own date: a force
    push to the head gives ``push_event``, else the head commit's date is ``commit_date``. The
    newest ``FORCE_PUSHES`` events hold a push to the head or the PR is ``Unresolved``."""
    events = _obj(pr.get("pushEvents"), "the push events")
    forced: tuple[datetime, str] | None = None
    for raw in _list_of(events.get("nodes")):
        found = gs._push_item(raw, head)
        if found is None:
            continue
        when = gs._parse_time(found[1])
        if when is not None and (forced is None or when > forced[0]):
            forced = (when, found[1])
    if forced is None and _more(events, "hasPreviousPage") is not False:
        raise Unresolved(BULK_UNAVAILABLE, "the force pushes are cut before one to the head")
    moment: datetime | None = None
    push: m.PushTime | None = None
    if forced is not None:
        moment, push = forced[0], m.PushTime(at=forced[1], source="push_event")
    else:
        nodes = _list_of(_obj(pr.get("commits"), "the commits").get("nodes"))
        commit = _opt_obj(_obj(nodes[-1], "a commit").get("commit"), "a commit") if nodes else None
        date = gs._ts(commit.get("committedDate")) if commit is not None else None
        parsed = gs._parse_time(date) if date is not None else None
        if date is not None and parsed is not None:
            moment, push = parsed, m.PushTime(at=date, source="commit_date")
    if moment is not None and moment > now() + gs.FUTURE_SKEW:
        env.warn("the push time of the head lies in the future and is not trusted")
        return None
    return push


# -- the entry point ---------------------------------------------------------------------------


def read_bulk(
    env: ReadEnv,
    host: str,
    refs: Sequence[PrRef],
    *,
    now: Callable[[], datetime] = gs.utc_now,
    page_size: int = PAGE_SIZE,
) -> BulkRead:
    """The snapshots of ``refs`` (all on ``host``) that the bulk reader can give whole, and why
    not for the others."""
    if not refs:
        return BulkRead()
    # a rate limit is retried by `_Run._graphql` itself; every other failure halves the page
    run = _Run(env, host, now)
    return run_pages(list(refs), page_size, run.ask)
