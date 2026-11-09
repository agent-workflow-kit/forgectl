"""The GitHub adapter: one GraphQL query per read, normalized into ``forgectl.model``.

``show`` (``get_pr``) costs one request (the constant ``SHOW_QUERY``), plus more only when the
host cut a list off: a page of reviews, labels or checks, or the checks of the head commit by its
oid when the last listed commit is not the head (GitHub lists commits in date order, not head
order). Every query is a constant text; the owner, name, number, oid and cursors travel as
variables, never in the text.

Everything the host says is untrusted. This module is the normalization boundary: a field is
checked for type and grammar, a string that reaches the output is sanitized for its own field
(``sanitize_line`` for the ``x-one-line`` fields, ``sanitize_text`` for a body), and an answer
that is not the documented shape is ``upstream_error``, never a traceback and never a guess. A
value the host leaves out is ``null`` in the output (a tri-state is unknown), and nothing the
adapter does not know ever lets ``blocked_by`` read as ready.

Logins are the REST form (interface: Logins): GraphQL names a bot ``name``, REST ``name[bot]``,
and ``gh`` ``app/name``; ``rest_login`` maps by the actor's ``__typename``.

Parsing helpers (``parse_actor``, ``latest_by_reviewer``, ``read_checks``) are module-level so
``snapshot``, ``reviews`` and ``ci`` (``github_snapshot.py``) reuse them instead of reading these
fields a second way.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from .. import model as m
from ..errors import ForgectlError
from ..gateway import Target, verify_identity
from ..identifiers import PrRef, RepoRef, bind_login
from ..reads import SHOW_READINESS, ReadEnv
from ..sanitize import sanitize_line, sanitize_text
from .base import not_implemented

T = TypeVar("T")

# -- the constant queries ---------------------------------------------------------------------

_ACTOR_FIELDS = """
fragment ActorFields on Actor {
  __typename
  login
  ... on User { databaseId }
  ... on Bot { databaseId }
  ... on Mannequin { databaseId }
  ... on Organization { databaseId }
}
"""

_COMMIT_CHECKS = """
fragment CommitChecks on Commit {
  oid
  statusCheckRollup {
    contexts(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        __typename
        ... on CheckRun {
          name status conclusion detailsUrl completedAt
          isRequired(pullRequestNumber: $number)
        }
        ... on StatusContext {
          context state targetUrl createdAt
          isRequired(pullRequestNumber: $number)
        }
      }
    }
  }
}
"""

_REVIEW_FIELDS = """
fragment ReviewFields on PullRequestReviewConnection {
  pageInfo { hasPreviousPage startCursor }
  nodes {
    state
    submittedAt
    authorCanPushToRepository
    author { ...ActorFields }
    commit { oid }
  }
}
"""

#: The one query `show` reads a PR with (one point in live verification). `after` is the cursor of
#: the head commit's checks, null on this first page.
SHOW_QUERY = (
    """
query ForgectlShow($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    defaultBranchRef { name }
    squashMergeAllowed
    mergeCommitAllowed
    rebaseMergeAllowed
    pullRequest(number: $number) {
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
      author { ...ActorFields }
      mergedBy { ...ActorFields }
      authorAssociation
      labels(first: 100) {
        pageInfo { hasNextPage endCursor }
        nodes { name }
      }
      headRefName
      headRefOid
      baseRefName
      baseRefOid
      headRepository { nameWithOwner }
      isCrossRepository
      mergeCommit { oid }
      mergeable
      mergeStateStatus
      isMergeQueueEnabled
      autoMergeRequest { enabledBy { ...ActorFields } }
      additions
      deletions
      changedFiles
      reviewDecision
      reviews(last: 100) { ...ReviewFields }
      reviewThreads(first: 100) {
        pageInfo { hasNextPage }
        nodes { isResolved }
      }
      commits(last: 1) { nodes { commit { ...CommitChecks } } }
      timelineItems(
        last: 1
        itemTypes: [BASE_REF_CHANGED_EVENT, AUTOMATIC_BASE_CHANGE_SUCCEEDED_EVENT]
      ) {
        nodes {
          __typename
          ... on BaseRefChangedEvent { createdAt }
          ... on AutomaticBaseChangeSucceededEvent { createdAt }
        }
      }
    }
  }
}
"""
    + _ACTOR_FIELDS
    + _COMMIT_CHECKS
    + _REVIEW_FIELDS
)

#: The head commit's checks by oid, when the last listed commit is not the head, and the next
#: pages of a long check list.
CHECKS_QUERY = (
    """
query ForgectlChecks(
  $owner: String!, $name: String!, $oid: GitObjectID!, $number: Int!, $after: String
) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    url
    object(oid: $oid) { ...CommitChecks }
  }
}
"""
    + _COMMIT_CHECKS
)

#: Earlier reviews, when the newest 100 are not all of them.
REVIEWS_QUERY = (
    """
query ForgectlReviews($id: ID!, $before: String) {
  node(id: $id) {
    ... on PullRequest {
      number
      url
      reviews(last: 100, before: $before) { ...ReviewFields }
    }
  }
}
"""
    + _ACTOR_FIELDS
    + _REVIEW_FIELDS
)

#: More labels, when the first 100 are not all of them.
LABELS_QUERY = """
query ForgectlLabels($id: ID!, $after: String) {
  node(id: $id) {
    ... on PullRequest {
      number
      url
      labels(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { name }
      }
    }
  }
}
"""

MAX_PAGES = 50  # per list, so a hostile answer that never ends cannot loop for ever
MAX_CHECK_PAGES = 10  # a longer check list is `complete: false`, a host-imposed cap
MAX_PR_NUMBER = 2**31 - 1  # GraphQL's Int; a larger number cannot be a PR
MAX_DETAIL = 100

# -- the host's words, in one table ------------------------------------------------------------

#: `mergeStateStatus` and what each means in host-neutral terms. A key of `facts` is a field of
#: `model.BlockFacts`. A status missing here is `other` with the status as its detail, so a new
#: host status never disappears and never reads as ready.
MERGE_STATE_TABLE: Mapping[str, Mapping[str, bool]] = {
    "DIRTY": {"conflicts": True},
    "BEHIND": {"behind": True},
    "DRAFT": {"draft": True},
    "BLOCKED": {"host_blocked": True},
    "UNKNOWN": {},  # still computing: `mergeability_unknown` once the tries ran out
    "CLEAN": {},
    "HAS_HOOKS": {},
    "UNSTABLE": {},
}

#: `mergeable`: true, false, or unknown.
MERGEABLE_TABLE: Mapping[str, bool | None] = {
    "MERGEABLE": True,
    "CONFLICTING": False,
    "UNKNOWN": None,
}

_PR_STATES: Mapping[str, m.PrState] = {"OPEN": "open", "CLOSED": "closed", "MERGED": "merged"}
_REVIEW_STATES: Mapping[str, m.LatestState | None] = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes_requested",
    "DISMISSED": "dismissed",
    "COMMENTED": None,  # never replaces the latest opinionated state
    "PENDING": None,  # a pending review belongs to its author and is not listed
}

# -- grammars the output promises (schemas/defs.json) -----------------------------------------

_SHA = re.compile(r"[0-9a-f]{40}")
_NODE_ID = re.compile(r"[A-Za-z0-9:/_=-]{1,128}")
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9:/_=-]{1,128}")
_TS = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)
_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
_CURSOR = re.compile(r"[\x21-\x7e]{1,512}")


class Malformed(Exception):
    """The host's answer is not the documented shape (turned into ``upstream_error``)."""


def malformed(what: str) -> ForgectlError:
    return ForgectlError(
        "upstream_error", f"GitHub answered {what} that is not the documented shape"
    )


# -- reading untrusted values ------------------------------------------------------------------


def _obj(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Malformed(what)
    return value


def _opt_obj(value: object, what: str) -> dict[str, Any] | None:
    return None if value is None else _obj(value, what)


def _list(value: object, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise Malformed(what)
    return value


def _str(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise Malformed(what)
    return value


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _opt_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _ts(value: object) -> str | None:
    return value if isinstance(value, str) and _TS.fullmatch(value) else None


def _req_ts(value: object, what: str) -> str:
    stamp = _ts(value)
    if stamp is None:
        raise Malformed(what)
    return stamp


def _sha(value: object) -> str | None:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def _req_sha(value: object, what: str) -> str:
    sha = _sha(value)
    if sha is None:
        raise Malformed(what)
    return sha


def _project(value: object, what: str) -> str:
    """``owner/repo`` as GitHub reports it, checked against the grammar of a repo path."""
    text = _str(value, what)
    parts = text.split("/")
    if len(parts) != 2 or any(not _SEGMENT.fullmatch(p) or p in (".", "..") for p in parts):
        raise Malformed(what)
    return text


def _cursor(value: object) -> str | None:
    return value if isinstance(value, str) and _CURSOR.fullmatch(value) else None


# -- logins and accounts -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Actor:
    """An account as the model keeps it: REST login (or None), id (or None), bot flag."""

    login: str | None
    id: str | None
    is_bot: bool | None
    #: a login was given but is not a valid GitHub login (never passed on)
    invalid_login: bool = False

    def author(self) -> m.Author:
        return m.Author(login=self.login, id=self.id, is_bot=self.is_bot)


def rest_login(typename: str | None, login: object) -> str | None:
    """The REST form of a GraphQL login, or None when it is absent or not a GitHub login.

    A ``Bot`` is ``name[bot]`` in REST and ``name`` in GraphQL (``gh`` shows ``app/name``): all
    three map to ``name[bot]``. Any other type keeps its login, so a user named like a bot stays
    the different account it is; a non-Bot login spelled ``app/...`` or ``...[bot]`` is not a
    login. A login that fails the input grammar is dropped, never passed on to a later command.
    """
    if not isinstance(login, str):
        return None
    if typename == "Bot":
        if login.startswith("app/"):
            login = login[len("app/") :]
        if not login.endswith("[bot]"):
            login += "[bot]"
    elif login.startswith("app/") or login.endswith("[bot]"):
        return None  # only a Bot is spelled like a bot
    try:
        return bind_login(login, "github")
    except ForgectlError:
        return None


def parse_actor(value: object) -> Actor | None:
    """An ``ActorFields`` object; None for a null (a deleted account, GitHub's ghost)."""
    if value is None:
        return None
    node = _obj(value, "an account")
    typename = _opt_str(node.get("__typename"))
    login = rest_login(typename, node.get("login"))
    raw_id = node.get("databaseId")
    account = (
        str(raw_id)
        if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id >= 1
        else None
    )
    if account is not None and not _ACCOUNT_ID.fullmatch(account):
        account = None
    bot: bool | None = {"Bot": True, "User": False}.get(typename or "")
    # only a null author is deleted: a present one without a usable login is an invalid login
    return Actor(login=login, id=account, is_bot=bot, invalid_login=login is None)


def _same_account(a: tuple[str | None, str | None], b: tuple[str | None, str | None]) -> bool:
    """Accounts compare by id; logins (ASCII-lowered) decide only when either side has no id."""
    if a[1] is not None and b[1] is not None:
        return a[1] == b[1]
    if a[0] is None or b[0] is None:
        return False
    return m.ascii_lower(a[0]) == m.ascii_lower(b[0])


# -- reviews -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewTable:
    """`latest_by_reviewer` from a list of reviews, and what the list could not say."""

    entries: tuple[m.LatestByReviewer, ...]
    #: a review asking for changes came from an account whose login is unusable: it still blocks
    unnamed_changes_requested: bool


def latest_by_reviewer(reviews: Sequence[Any]) -> ReviewTable:
    """One entry per account holding its latest approved, changes-requested or dismissed review,
    from ``reviews`` oldest first (the host's order). A deleted account has no login, so its
    reviews are skipped, as the interface says.

    A state this adapter does not know replaces the account's latest entry as ``dismissed``: never
    an approval, never a changes request. An account whose login is unusable is tracked by id, so
    its later review replaces its changes request; without an id its changes request sticks."""
    latest: list[tuple[tuple[str | None, str | None], m.LatestByReviewer]] = []
    unnamed_by_id: dict[str, bool] = {}  # id -> its latest review blocks
    sticky = False
    for index, raw in enumerate(reviews):
        review = _obj(raw, f"review {index}")
        word = _str(review.get("state"), "a review state")
        if word in _REVIEW_STATES:
            state = _REVIEW_STATES[word]
            if state is None:
                continue  # commented and pending are never an opinion
        else:
            state = "dismissed"
        actor = parse_actor(review.get("author"))
        if actor is None:
            continue
        can_push = _opt_bool(review.get("authorCanPushToRepository"))
        if actor.login is None:
            blocks = state == "changes_requested" and can_push is not False
            if actor.id is None:
                sticky = sticky or blocks
            else:
                latest = [(k, e) for k, e in latest if k[1] != actor.id]
                unnamed_by_id[actor.id] = blocks
            continue
        if actor.id is not None:
            unnamed_by_id.pop(actor.id, None)
        entry = m.LatestByReviewer(
            login=actor.login,
            id=actor.id,
            state=state,
            commit_sha=_sha((_opt_obj(review.get("commit"), "a commit") or {}).get("oid")),
            at=_ts(review.get("submittedAt")),
            can_push=can_push,
        )
        key = (actor.login, actor.id)
        latest = [(k, e) for k, e in latest if not _same_account(k, key)]
        latest.append((key, entry))
    unnamed = sticky or any(unnamed_by_id.values())
    return ReviewTable(tuple(e for _, e in latest), unnamed)


# -- checks ------------------------------------------------------------------------------------


def _check_name(raw: object) -> str:
    name = sanitize_line(raw) if isinstance(raw, str) else ""
    return name or "(unnamed check)"


def _check_url(raw: object) -> str | None:
    url = m.clean_check_url(raw if isinstance(raw, str) else None)
    # a url holding an invisible or bidi character is not one a reader can trust
    return url if url is not None and sanitize_line(url) == url else None


def read_checks(contexts: Sequence[Any], warn: m.Warn) -> list[m.Check]:
    """The checks of a rollup's context nodes. A node of a kind this adapter does not know is a
    pending check, so it can never read as passing."""
    checks: list[m.Check] = []
    for raw in contexts:
        node = raw if isinstance(raw, dict) else {}
        kind = node.get("__typename")
        if kind == "CheckRun":
            status = node.get("status")
            conclusion = node.get("conclusion")
            word = conclusion if status == "COMPLETED" else status
            state = m.normalize_check_state(
                "github_check", word if isinstance(word, str) else "", warn=warn
            )
            name, url, when = node.get("name"), node.get("detailsUrl"), node.get("completedAt")
        elif kind == "StatusContext":
            word = node.get("state")
            state = m.normalize_check_state(
                "github_status", word if isinstance(word, str) else "", warn=warn
            )
            name, url, when = node.get("context"), node.get("targetUrl"), node.get("createdAt")
        else:
            warn("a check of an unknown kind; reading it as pending")
            checks.append(m.Check("(unrecognized check)", "pending", None, True, None, None))
            continue
        finished = _ts(when) if state != "pending" else None
        checks.append(
            m.Check(
                name=_check_name(name),
                state=state,
                required=_opt_bool(node.get("isRequired")),
                counted=m.is_counted(state, allow_failure=None),
                url=_check_url(url),
                finished_at=finished,
            )
        )
    return checks


# -- the adapter -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Shown:
    """One answer of the show query: the repository and its pull request, identity checked."""

    repo: dict[str, Any]
    pr: dict[str, Any]
    project: str  # the canonical `owner/repo`, from the host


def _is_unknown(shown: _Shown) -> bool:
    """Whether the mergeability of an open PR is not known: still computing, missing, of the
    wrong type, or a `mergeable` this adapter does not map. Never a clean reading."""
    pr = shown.pr
    if pr.get("state") != "OPEN":
        return False
    status = pr.get("mergeStateStatus")
    return (
        not isinstance(status, str)
        or status == "UNKNOWN"
        or pr.get("mergeable") not in ("MERGEABLE", "CONFLICTING")
    )


def _url_path(url: object, project: str, suffix: str) -> None:
    """The raw path of a url the gateway accepted must be exactly ``/{project}{suffix}`` (one
    trailing slash allowed): no percent escape, no dot segment, no other number."""
    if not isinstance(url, str):
        raise Malformed("the url")
    path = urllib.parse.urlsplit(url).path
    expected = f"/{project}{suffix}"
    if "%" in path or path not in (expected, expected + "/"):
        raise Malformed("the url")


@dataclass(frozen=True, slots=True)
class _Pages:
    """What a follow-up page read needs, and whether to read further pages at all."""

    target: Target
    node_id: str
    number: int
    project: str  # the canonical `owner/repo` of the show answer
    more: bool


def _quiet(_message: str) -> None:
    return None


def _split(ref: PrRef) -> tuple[str, str]:
    owner, _, name = ref.project.partition("/")
    if not owner or not name or "/" in name:
        raise ForgectlError("not_found", "a GitHub repository has an owner and a name")
    return owner, name


class GitHubBackend:
    """``gh api graphql`` and ``gh api``, through the gateway, normalized."""

    name: m.BackendName = "github"

    def __init__(self, env: ReadEnv) -> None:
        self._env = env

    # -- show ----------------------------------------------------------------------------------

    def get_pr(self, ref: PrRef) -> m.PrRead:
        owner, name = _split(ref)
        if ref.number > MAX_PR_NUMBER:
            raise ForgectlError("not_found", "no such pull request")
        target = self._env.hosts.target(ref.host)
        shown, still_unknown = self._env.until_known(
            lambda: self._read_show(target, ref, owner, name), _is_unknown, SHOW_READINESS
        )
        try:
            return self._normalize(target, ref, owner, name, shown, still_unknown)
        except Malformed as bad:
            raise malformed(f"{bad}") from None
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise malformed("data") from None

    def _graphql(
        self,
        target: Target,
        query: str,
        variables: Mapping[str, Any],
        check: Callable[[dict[str, Any]], T],
    ) -> T:
        """One query, retried; ``check`` validates the answer inside the retried call, so an
        answer that is not the documented shape is retried like a 5xx (interface: Reliability)."""
        gateway = self._env.gateway

        def once() -> T:
            try:
                data = dict(gateway.graphql(target, query, variables).data)
                return check(data)
            except Malformed as bad:
                raise malformed(f"{bad}") from None
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                raise malformed("data") from None

        return self._env.call(once)

    def _read_show(self, target: Target, ref: PrRef, owner: str, name: str) -> _Shown:
        variables = {"owner": owner, "name": name, "number": ref.number, "after": None}

        def check(data: dict[str, Any]) -> _Shown:
            repo = _opt_obj(data.get("repository"), "the repository")
            if repo is None:
                raise ForgectlError("not_found", "no such repository, or no access to it")
            pr = _opt_obj(repo.get("pullRequest"), "the pull request")
            if pr is None:
                raise ForgectlError("not_found", "no such pull request")
            project = _project(repo.get("nameWithOwner"), "the repository path")
            number = pr.get("number")
            if isinstance(number, bool) or number != ref.number:
                raise Malformed("the pull request number")
            # the answer must be this host's own, for the project it names (a rename is accepted)
            verify_identity(pr.get("url"), host=target.host, project=project, secret=target.token)
            _url_path(pr.get("url"), project, f"/pull/{ref.number}")
            shown = _Shown(repo=repo, pr=pr, project=project)
            # every field of this answer is checked here, so a malformed answer is retried
            self._normalize(target, ref, owner, name, shown, False, more=False)
            return shown

        return self._graphql(target, SHOW_QUERY, variables, check)

    def _normalize(
        self,
        target: Target,
        ref: PrRef,
        owner: str,
        name: str,
        shown: _Shown,
        still_unknown: bool,
        *,
        more: bool = True,
    ) -> m.PrRead:
        """The PR of one show answer. ``more=False`` reads only that answer (no further pages
        and no warnings): the shape check run inside the retried call."""
        pr, repo = shown.pr, shown.repo
        warn = self._env.warn if more else _quiet
        state = _PR_STATES.get(_str(pr.get("state"), "the state"))
        if state is None:
            raise Malformed("the state")
        number = ref.number
        canonical = shown.project
        node_id = _str(pr.get("id"), "the id")
        if not _NODE_ID.fullmatch(node_id):
            raise Malformed("the id")
        draft = pr.get("isDraft")
        if not isinstance(draft, bool):
            raise Malformed("the draft flag")

        head_sha = _req_sha(pr.get("headRefOid"), "the head sha")
        author = (parse_actor(pr.get("author")) or Actor(None, None, None)).author()
        pages = _Pages(target, node_id, number, canonical, more)
        labels = self._labels(pages, pr)
        table = latest_by_reviewer(self._reviews(pages, pr))
        ci = self._ci(pages, owner, name, head_sha, pr, warn)
        retarget_at, retarget_known = _retarget(pr)

        approvals = m.count_approvals(table.entries, author)
        approvals_at_head = m.count_approvals_at_head(
            table.entries,
            author,
            head_sha=head_sha,
            backend="github",
            retarget_at=retarget_at,
            retarget_known=retarget_known,
        )
        decision_raw = pr.get("reviewDecision")
        decision = m.github_review_decision(
            decision_raw if decision_raw is None or isinstance(decision_raw, str) else "?"
        )

        merge_state = pr.get("mergeStateStatus")
        mergeable_raw = pr.get("mergeable")
        facts = self._facts(
            state=state,
            ci=ci,
            draft=draft,
            merge_state=merge_state if isinstance(merge_state, str) else None,
            mergeable=mergeable_raw if isinstance(mergeable_raw, str) else None,
            still_unknown=still_unknown,
            has_decision=decision_raw is not None,
            decision=decision,
            table=table,
            unresolved=_unresolved_threads(pr),
        )
        blocked_by = m.compute_blocked_by(facts)
        mergeable = MERGEABLE_TABLE.get(mergeable_raw) if isinstance(mergeable_raw, str) else None

        merged_at = _ts(pr.get("mergedAt"))
        if state == "merged" and merged_at is None:
            raise Malformed("the merge time of a merged pull request")
        closed_at = _ts(pr.get("closedAt")) if state != "open" else None
        head_repo = _opt_obj(pr.get("headRepository"), "the head repository")
        head_path = None if head_repo is None else _project(head_repo.get("nameWithOwner"), "head")
        default = _opt_obj(repo.get("defaultBranchRef"), "the default branch")
        base_ref = _str(pr.get("baseRefName"), "the base branch")
        merged = state == "merged"
        is_open = state == "open"
        merge_commit = _opt_obj(pr.get("mergeCommit"), "the merge commit")
        merged_by = parse_actor(pr.get("mergedBy"))
        request = _opt_obj(pr.get("autoMergeRequest"), "the auto-merge request")
        enabled_by = parse_actor(request.get("enabledBy")) if request is not None else None

        built = m.PR(
            pr=f"{ref.host}/{canonical}#{number}",
            repo=f"{ref.host}/{canonical}",
            pr_id=node_id,
            number=number,
            url=f"https://{ref.host}/{canonical}/pull/{number}",
            backend="github",
            title=sanitize_line(_str(pr.get("title"), "the title")),
            body=None if pr.get("body") is None else sanitize_text(_str(pr["body"], "the body")),
            author=author,
            state=state,
            draft=draft,
            created_at=_req_ts(pr.get("createdAt"), "the creation time"),
            updated_at=_req_ts(pr.get("updatedAt"), "the update time"),
            closed_at=closed_at,
            merged_at=merged_at if merged else None,
            merged_by=(merged_by.login if merged and merged_by is not None else None),
            labels=labels,
            author_association=(
                sanitize_line(pr["authorAssociation"])
                if isinstance(pr.get("authorAssociation"), str)
                else None
            ),
            head=m.Head(
                sha=head_sha,
                ref=sanitize_line(_str(pr.get("headRefName"), "the head branch")),
                repo=None if head_path is None else f"{ref.host}/{head_path}",
                is_fork=_opt_bool(pr.get("isCrossRepository")) if head_path is not None else None,
            ),
            base=m.Base(
                ref=sanitize_line(base_ref),
                sha=_sha(pr.get("baseRefOid")),
                is_default=(
                    base_ref == default.get("name")
                    if default is not None and isinstance(default.get("name"), str)
                    else None
                ),
            ),
            merged_head_sha=head_sha if merged else None,
            merge_commit_sha=(
                _sha(merge_commit.get("oid")) if merged and merge_commit is not None else None
            ),
            head_fetch_ref=f"refs/pull/{number}/head",
            mergeable=mergeable if is_open else None,
            blocked_by=blocked_by,
            # a closed or merged PR keeps a stale request: armed only while open
            auto_merge=m.AutoMerge(
                enabled=request is not None and is_open,
                by=enabled_by.login if enabled_by is not None and is_open else None,
            ),
            merge_queue=_opt_bool(pr.get("isMergeQueueEnabled")),
            merge_methods_allowed=_methods(repo),
            additions=_count(pr.get("additions")),
            deletions=_count(pr.get("deletions")),
            changed_files=_count(pr.get("changedFiles")),
            review_decision=decision,
            approvals=approvals,
            approvals_at_head=approvals_at_head,
        )
        return m.PrRead(pr=built, ci=ci)

    # -- parts ---------------------------------------------------------------------------------

    def _node_page(
        self, pages: _Pages, query: str, variables: Mapping[str, Any], field: str
    ) -> dict[str, Any]:
        """The ``field`` connection of the PR read again by node id, identity checked."""
        target = pages.target

        def check(data: dict[str, Any]) -> dict[str, Any]:
            holder = _obj(data.get("node"), "the pull request")
            number = holder.get("number")
            if isinstance(number, bool) or number != pages.number:
                raise Malformed("the pull request number")
            url = holder.get("url")
            verify_identity(url, host=target.host, project=pages.project, secret=target.token)
            _url_path(url, pages.project, f"/pull/{pages.number}")
            return _obj(holder.get(field), f"the {field}")

        return self._graphql(target, query, variables, check)

    def _labels(self, pages: _Pages, pr: dict[str, Any]) -> tuple[str, ...]:
        names: list[str] = []
        connection = _obj(pr.get("labels"), "the labels")
        for _page in range(MAX_PAGES):
            for node in _list(connection.get("nodes"), "the labels"):
                names.append(sanitize_line(_str(_obj(node, "a label").get("name"), "a label")))
            info = _obj(connection.get("pageInfo"), "the labels page")
            more = info.get("hasNextPage")
            if more is not True:
                if more is not False:
                    raise Malformed("the labels page")
                return tuple(names)
            cursor = _cursor(info.get("endCursor"))
            if cursor is None:
                raise Malformed("the labels page cursor")
            if not pages.more:
                return tuple(names)
            connection = self._node_page(
                pages, LABELS_QUERY, {"id": pages.node_id, "after": cursor}, "labels"
            )
        raise ForgectlError(
            "upstream_error", "the pull request has more labels than forgectl reads"
        )

    def _reviews(self, pages: _Pages, pr: dict[str, Any]) -> list[Any]:
        chunks: list[list[Any]] = []
        connection = _obj(pr.get("reviews"), "the reviews")
        for _page in range(MAX_PAGES):
            chunks.append(_list(connection.get("nodes"), "the reviews"))
            info = _obj(connection.get("pageInfo"), "the reviews page")
            more = info.get("hasPreviousPage")
            if more is not True:
                if more is not False:
                    raise Malformed("the reviews page")
                return [review for chunk in reversed(chunks) for review in chunk]
            cursor = _cursor(info.get("startCursor"))
            if cursor is None:
                raise Malformed("the reviews page cursor")
            if not pages.more:
                return [review for chunk in reversed(chunks) for review in chunk]
            connection = self._node_page(
                pages, REVIEWS_QUERY, {"id": pages.node_id, "before": cursor}, "reviews"
            )
        raise ForgectlError(
            "upstream_error", "the pull request has more reviews than forgectl reads"
        )

    def _commit_by_oid(
        self, pages: _Pages, owner: str, name: str, head_sha: str, after: str | None
    ) -> dict[str, Any]:
        """The head commit read by oid (a page of its checks), identity checked."""
        target = pages.target
        variables = {
            "owner": owner,
            "name": name,
            "oid": head_sha,
            "number": pages.number,
            "after": after,
        }

        def check(data: dict[str, Any]) -> dict[str, Any]:
            repo = _obj(data.get("repository"), "the repository")
            if _project(repo.get("nameWithOwner"), "the repository path") != pages.project:
                raise Malformed("the repository path")
            url = repo.get("url")
            verify_identity(url, host=target.host, project=pages.project, secret=target.token)
            _url_path(url, pages.project, "")
            commit = _opt_obj(repo.get("object"), "the head commit")
            if commit is None or commit.get("oid") != head_sha:
                raise Malformed("the head commit")
            if after is not None and commit.get("statusCheckRollup") is None:
                raise Malformed("the check rollup of a later page")
            return commit

        return self._graphql(target, CHECKS_QUERY, variables, check)

    def _ci(
        self,
        pages: _Pages,
        owner: str,
        name: str,
        head_sha: str,
        pr: dict[str, Any],
        warn: m.Warn,
    ) -> m.CI:
        """The head's checks, read for ``headRefOid`` exactly: the rollup of the last listed
        commit when its oid matches, else the commit read by oid."""
        nodes = _list(_obj(pr.get("commits"), "the commits").get("nodes"), "the commits")
        last = _opt_obj(_obj(nodes[-1], "a commit").get("commit"), "a commit") if nodes else None
        commit = last if last is not None and last.get("oid") == head_sha else None
        checks: list[m.Check] = []
        complete = True
        cursor: str | None = None
        for page in range(MAX_CHECK_PAGES):
            if commit is None:
                if not pages.more:
                    break
                commit = self._commit_by_oid(pages, owner, name, head_sha, cursor)
            rollup = _opt_obj(commit.get("statusCheckRollup"), "the check rollup")
            if rollup is None:
                if page > 0:
                    raise Malformed("the check rollup of a later page")
                break  # no checks at all: null rollup
            contexts = _obj(rollup.get("contexts"), "the checks")
            checks.extend(read_checks(_list(contexts.get("nodes"), "the checks"), warn))
            info = _obj(contexts.get("pageInfo"), "the checks page")
            more = info.get("hasNextPage")
            if more is False:
                break
            if more is not True:
                raise Malformed("the checks page")
            cursor = _cursor(info.get("endCursor"))
            if cursor is None:
                raise Malformed("the checks page cursor")
            if page == MAX_CHECK_PAGES - 1:
                complete = False  # the host's list is longer than forgectl reads
            commit = None  # the next page is read by oid
        return m.aggregate_ci(checks, pr_head_sha=head_sha, checks_sha=head_sha, complete=complete)

    def _facts(
        self,
        *,
        state: m.PrState,
        ci: m.CI,
        draft: bool,
        merge_state: str | None,
        mergeable: str | None,
        still_unknown: bool,
        has_decision: bool,
        decision: m.ReviewDecision,
        table: ReviewTable,
        unresolved: int | None,
    ) -> m.BlockFacts:
        flags: dict[str, Any] = {}
        others: list[str] = []
        if merge_state is None:
            pass  # missing is unknown: `still_unknown` covers it, and nothing reads as clean
        elif merge_state in MERGE_STATE_TABLE:
            flags.update(MERGE_STATE_TABLE[merge_state])
        else:
            others.append(_detail(merge_state))
        if mergeable == "CONFLICTING":
            flags["conflicts"] = True
        blocked = bool(flags.get("host_blocked"))
        return m.BlockFacts(
            state=state,
            ci=ci,
            draft=draft or bool(flags.get("draft")),
            conflicts=bool(flags.get("conflicts")),
            behind=bool(flags.get("behind")),
            mergeability_unknown=still_unknown,
            reviews_required=blocked and has_decision and decision == "review_required",
            host_blocked=blocked,
            host_changes_requested=table.unnamed_changes_requested,
            unresolved_threads=unresolved,
            other_details=tuple(others),
            latest_by_reviewer=table.entries,
        )

    # -- the reads other issues add ------------------------------------------------------------

    def list_prs(self, query: m.PrQuery) -> m.PrPage:
        raise not_implemented("prs")  # TODO(#14): GraphQL search, partial results across hosts

    def snapshot(self, ref: PrRef) -> m.Snapshot:
        from . import github_snapshot  # (#12) lazy: that module imports this one

        return github_snapshot.snapshot(self._env, ref, self.get_pr)

    def diff(self, ref: PrRef) -> m.DiffResult:
        raise not_implemented("diff")  # TODO(#13)

    def compare(self, ref: PrRef, from_sha: str, to_sha: str) -> m.CompareResult:
        raise not_implemented("compare")  # TODO(#13)

    def files(self, ref: PrRef) -> m.FilesResult:
        raise not_implemented("files")  # TODO(#13)

    def comments(self, ref: PrRef) -> m.CommentsResult:
        from . import github_snapshot  # (#12)

        return github_snapshot.comments(self._env, ref)

    def reviews(self, ref: PrRef) -> m.ReviewsResult:
        from . import github_snapshot  # (#12)

        return github_snapshot.reviews(self._env, ref, self.get_pr)

    def ci(self, ref: PrRef) -> m.CiResult:
        from . import github_snapshot  # (#12)

        return github_snapshot.ci(ref, self.get_pr)

    def access(self, repo: RepoRef, user: str) -> m.AccessResult:
        raise not_implemented("access")  # TODO(#14)

    def whoami(self, host: str) -> m.Identity:
        raise not_implemented("whoami")  # TODO(#14): hosts.identity is the source


# -- small mappings ----------------------------------------------------------------------------


def _detail(status: str) -> str:
    """A host status as a blocker detail: one line, cut, never empty. The cut never splits a
    ``<U+XXXX>`` marker: it goes back to the marker's start."""
    text = sanitize_line(status)
    if len(text) > MAX_DETAIL:
        text = text[:MAX_DETAIL]
        start = text.rfind("<U+")
        if start != -1 and ">" not in text[start:]:
            text = text[:start]
    return text or "(empty)"


def _methods(repo: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The repository's merge methods, or None when any of the three flags is missing."""
    flags = (
        ("squash", repo.get("squashMergeAllowed")),
        ("merge", repo.get("mergeCommitAllowed")),
        ("rebase", repo.get("rebaseMergeAllowed")),
    )
    if any(not isinstance(value, bool) for _, value in flags):
        return None
    return tuple(method for method, value in flags if value)


def _unresolved_threads(pr: Mapping[str, Any]) -> int | None:
    """How many resolvable threads are unresolved, or None when this cannot be told: the first
    100 hold none and there are more, or the field is missing or malformed."""
    connection = pr.get("reviewThreads")
    if not isinstance(connection, dict):
        return None
    nodes = connection.get("nodes")
    info = connection.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(info, dict):
        return None
    unresolved = sum(1 for n in nodes if isinstance(n, dict) and n.get("isResolved") is False)
    if unresolved == 0 and info.get("hasNextPage") is not False:
        return None
    return unresolved


def _retarget(pr: Mapping[str, Any]) -> tuple[str | None, bool]:
    """The newest retarget time (either timeline item type), and whether the lookup worked."""
    items = pr.get("timelineItems")
    nodes = items.get("nodes") if isinstance(items, dict) else None
    if not isinstance(nodes, list):
        return None, False
    if not nodes:
        return None, True
    last = nodes[-1]
    when = _ts(last.get("createdAt")) if isinstance(last, dict) else None
    return (when, True) if when is not None else (None, False)
