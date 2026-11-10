"""The normalized model both backends map into (interface: Normalized objects).

Everything a backend may not decide differently lives here: blocker codes, `blocked_by`, CI
aggregation, the approval rules, tri-state helpers and the fixed ordering of lists. Backends
build these frozen dataclasses from host answers; `to_dict()` produces the schema-shaped output
with every list in its fixed order, so two snapshots compare directly.

Nothing here talks to a host, reads config or logs: a backend passes a ``warn`` callable where a
warning is due.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from .identifiers import RepoRef

# --- literals ----------------------------------------------------------------------------------

BackendName = Literal["github", "gitlab"]
PrState = Literal["open", "closed", "merged"]
CheckState = Literal["passing", "failing", "pending", "skipped"]
CiState = Literal["passing", "failing", "pending", "none"]
ReviewDecision = Literal["approved", "changes_requested", "review_required", "none"]
ReviewState = Literal["approved", "changes_requested", "commented", "dismissed"]
LatestState = Literal["approved", "changes_requested", "dismissed"]
FileStatus = Literal["added", "modified", "deleted", "renamed"]
CompareStatus = Literal["ahead", "behind", "diverged", "identical", "unavailable"]
CheckSource = Literal["github_check", "github_status", "gitlab"]
PushSource = Literal["push_event", "commit_date"]
Warn = Callable[[str], None]

#: Parts a caller may name in `Snapshot.incomplete`; `ci` comes only from `ci.complete`.
INCOMPLETE_PARTS = frozenset({"review_requests", "reviews", "threads"})
_PR_STATES = frozenset({"open", "closed", "merged"})

# --- tri-states --------------------------------------------------------------------------------
# `true`, `false` or `null` (unknown). Consumers treat null as the safe answer.


def is_true(value: object) -> bool:
    """True only for the boolean ``True``; unknown (``None``) and anything else is not."""
    return value is True


def is_false(value: object) -> bool:
    """True only for the boolean ``False``; unknown is not known-false."""
    return value is False


def is_approved(count: object) -> bool:
    """A count of approvals reads as approved only when it is a real integer above zero.

    ``None`` (unknown) is never approved, and neither is a bool or a negative number.
    """
    return isinstance(count, int) and not isinstance(count, bool) and count > 0


def can_act_for_automatically(author: Author) -> bool:
    """An unknown ``is_bot`` is not a human the caller may act for automatically."""
    return author.is_bot is False


# --- ordering ----------------------------------------------------------------------------------

_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def ascii_lower(text: str) -> str:
    """Lowercase ASCII letters only, so ordering never depends on Unicode case rules."""
    return text.translate(_ASCII_LOWER)


# --- blocker codes -----------------------------------------------------------------------------


class BlockerCode(StrEnum):
    DRAFT = "draft"
    CONFLICTS = "conflicts"
    CHECKS_PENDING = "checks_pending"
    CHECKS_FAILING = "checks_failing"
    REVIEWS_REQUIRED = "reviews_required"
    CHANGES_REQUESTED = "changes_requested"
    DISCUSSIONS_UNRESOLVED = "discussions_unresolved"
    BRANCH_BEHIND = "branch_behind"
    MERGEABILITY_UNKNOWN = "mergeability_unknown"
    OTHER = "other"
    # Only in `merge`'s refusal of a non-human actor; never in a PR's blocked_by.
    CHECKS_ABSENT = "checks_absent"


@dataclass(frozen=True, slots=True)
class Blocker:
    code: BlockerCode
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.code is BlockerCode.OTHER:
            if not self.detail:
                raise ValueError("a blocker with code 'other' needs a non-empty detail")
        elif self.detail is not None:
            raise ValueError(f"a blocker with code {self.code.value!r} takes no detail")

    def to_dict(self) -> dict[str, str]:
        if self.code is BlockerCode.OTHER:
            assert self.detail is not None
            return {"code": self.code.value, "detail": self.detail}
        return {"code": self.code.value}

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.code.value, self.detail or "")


def sorted_blockers(blockers: Iterable[Blocker]) -> tuple[Blocker, ...]:
    """Unique blockers ordered by code, then detail (code point)."""
    return tuple(sorted(set(blockers), key=lambda b: b.sort_key))


# --- people ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Author:
    login: str | None
    id: str | None
    is_bot: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {"login": self.login, "id": self.id, "is_bot": self.is_bot}


def _same_account(login: str | None, id_: str | None, author: Author) -> bool:
    """Accounts compare by id; only when either side has no id do logins (ASCII-lowered) decide."""
    if id_ is not None and author.id is not None:
        return id_ == author.id
    if login is None or author.login is None:
        return False
    return ascii_lower(login) == ascii_lower(author.login)


def _as_author(entry: LatestByReviewer) -> Author:
    return Author(login=entry.login, id=entry.id, is_bot=None)


def _reject_duplicate_accounts(entries: Iterable[LatestByReviewer]) -> None:
    """`latest_by_reviewer` holds one entry per account (accounts compare as `_same_account`)."""
    seen: list[Author] = []
    for e in entries:
        if any(_same_account(e.login, e.id, a) for a in seen):
            raise ValueError(f"latest_by_reviewer names the account {e.login!r} twice")
        seen.append(_as_author(e))


# --- CI ----------------------------------------------------------------------------------------

_CHECK_TABLE: dict[str, dict[str, CheckState]] = {
    "github_check": {
        **dict.fromkeys(("SUCCESS",), "passing"),
        **dict.fromkeys(
            (
                "FAILURE",
                "TIMED_OUT",
                "CANCELLED",
                "ACTION_REQUIRED",
                "STARTUP_FAILURE",
                "STALE",
            ),
            "failing",
        ),
        **dict.fromkeys(("QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"), "pending"),
        **dict.fromkeys(("NEUTRAL", "SKIPPED"), "skipped"),
    },
    "github_status": {
        "SUCCESS": "passing",
        "FAILURE": "failing",
        "ERROR": "failing",
        "PENDING": "pending",
        "EXPECTED": "pending",
    },
    "gitlab": {
        "success": "passing",
        "failed": "failing",
        "canceled": "failing",
        **dict.fromkeys(
            (
                "created",
                "pending",
                "preparing",
                "running",
                "scheduled",
                "waiting_for_resource",
                "waiting_for_callback",
                "canceling",
            ),
            "pending",
        ),
        "skipped": "skipped",
    },
}


#: The name of a check whose host gave no usable name (both backends).
UNNAMED_CHECK = "(unnamed check)"


def normalize_check_state(
    source: CheckSource,
    raw: str,
    *,
    allow_failure: bool | None = None,
    warn: Warn | None = None,
) -> CheckState:
    """Map a host's check state; an unmapped value is ``pending`` with a warning.

    GitLab ``manual`` is ``pending`` with ``allow_failure`` false and ``skipped`` with true; an
    unknown ``allow_failure`` reads as false, the stricter answer.
    """
    if source == "gitlab" and raw == "manual":
        return "skipped" if allow_failure is True else "pending"
    mapped = _CHECK_TABLE[source].get(raw)
    if mapped is None:
        if warn is not None:
            warn(f"unmapped {source} check state {raw!r}; reading it as pending")
        return "pending"
    return mapped


def is_counted(state: CheckState, *, allow_failure: bool | None) -> bool:
    """False for a GitLab ``allow_failure`` job and for ``skipped``; true otherwise."""
    return state != "skipped" and allow_failure is not True


def gitlab_required(allow_failure: bool | None) -> bool | None:
    """GitLab's ``required``: false for ``allow_failure: true``, else unknown."""
    return False if allow_failure is True else None


def clean_check_url(url: str | None) -> str | None:
    """A check's url, or null for any scheme other than lowercase ``http://`` or ``https://``
    and for a url holding whitespace or a control character."""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    if any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in url):
        return None
    return url


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    state: CheckState
    required: bool | None
    counted: bool
    url: str | None
    finished_at: str | None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a check needs a name")
        if self.state == "skipped" and self.counted:
            raise ValueError("a skipped check is never counted")
        if self.url is not None and clean_check_url(self.url) is None:
            raise ValueError("a check's url is null unless clean_check_url keeps it")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "required": self.required,
            "counted": self.counted,
            "url": self.url,
            "finished_at": self.finished_at,
        }

    @property
    def sort_key(self) -> tuple[str, bool, str, str, bool, str, int, bool]:
        """The spec's key (`name`, `url`, `state`; a null url first), then every other field, so
        the order never depends on the input order."""
        required = {None: 0, False: 1, True: 2}[self.required]
        return (
            self.name,
            self.url is not None,
            self.url or "",
            self.state,
            self.finished_at is not None,
            self.finished_at or "",
            required,
            self.counted,
        )


@dataclass(frozen=True, slots=True)
class CI:
    state: CiState
    head_sha: str
    for_current_head: bool
    complete: bool
    checks: tuple[Check, ...] = ()

    def __post_init__(self) -> None:
        expected = _ci_state(self.checks, for_head=self.for_current_head, complete=self.complete)
        if self.state != expected:
            raise ValueError(
                f"ci state {self.state!r} does not follow from its checks, for_current_head and"
                f" complete, which give {expected!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        checks = sorted(self.checks, key=lambda c: c.sort_key)
        return {
            "state": self.state,
            "head_sha": self.head_sha,
            "for_current_head": self.for_current_head,
            "complete": self.complete,
            "checks": [c.to_dict() for c in checks],
        }


def _ci_state(checks: Iterable[Check], *, for_head: bool, complete: bool) -> CiState:
    """The one aggregation rule, shared by `aggregate_ci` and `CI`'s own check."""
    counted = [c for c in checks if c.counted]
    if any(c.state == "failing" for c in counted):
        state: CiState = "failing"
    elif any(c.state == "pending" for c in counted):
        state = "pending"
    elif any(c.state == "passing" for c in counted):
        state = "passing"
    else:
        state = "none"
    if not for_head:
        return "pending"
    if state == "passing" and not complete:
        return "pending"
    return state


def aggregate_ci(
    checks: Iterable[Check],
    *,
    pr_head_sha: str,
    checks_sha: str | None,
    complete: bool = True,
) -> CI:
    """Aggregate the counted checks of one commit into the CI object.

    ``checks_sha`` is the commit the checks belong to (``None`` when the host has no pipeline at
    all). Any failing counted check gives ``failing``; else any pending gives ``pending``; else
    any passing gives ``passing``; else ``none``. Then: a commit that is not the head forces
    ``pending``; an incomplete list turns ``passing`` into ``pending``. ``none`` is not passing.
    """
    listed = tuple(checks)
    for_head = checks_sha is None or checks_sha == pr_head_sha
    state = _ci_state(listed, for_head=for_head, complete=complete)
    head = pr_head_sha if checks_sha is None or for_head else checks_sha
    return CI(
        state=state, head_sha=head, for_current_head=for_head, complete=complete, checks=listed
    )


def ci_is_passing(ci: CI) -> bool:
    """Only ``passing`` is passing: ``none`` means no evidence."""
    return ci.state == "passing"


# --- reviews and approvals ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LatestByReviewer:
    login: str
    id: str | None
    state: LatestState
    commit_sha: str | None
    at: str | None
    can_push: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "login": self.login,
            "id": self.id,
            "state": self.state,
            "commit_sha": self.commit_sha,
            "at": self.at,
            "can_push": self.can_push,
        }

    @property
    def sort_key(self) -> tuple[str, str]:
        return (ascii_lower(self.login), self.login)


@dataclass(frozen=True, slots=True)
class Review:
    id: str
    author: Author
    state: ReviewState
    commit_sha: str | None
    submitted_at: str
    body: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "author": self.author.to_dict(),
            "state": self.state,
            "commit_sha": self.commit_sha,
            "submitted_at": self.submitted_at,
            "body": self.body,
        }


def count_approvals(entries: Iterable[LatestByReviewer], author: Author) -> int:
    """Accounts whose latest state is ``approved`` at any head, other than the author, with
    ``can_push`` not false (unknown still counts). Entries naming one account twice are an
    error."""
    entries = tuple(entries)
    _reject_duplicate_accounts(entries)
    return sum(
        1
        for e in entries
        if e.state == "approved"
        and not _same_account(e.login, e.id, author)
        and e.can_push is not False
    )


def _not_before(at: str | None, floor: str) -> bool:
    """Whether ``at`` is at or after ``floor``; any missing, naive or unparsable time is no."""
    if not isinstance(at, str) or not isinstance(floor, str):
        return False
    try:
        t, f = datetime.fromisoformat(at), datetime.fromisoformat(floor)
    except (TypeError, ValueError):
        return False
    if t.tzinfo is None or f.tzinfo is None:
        return False
    return t >= f


def count_approvals_at_head(
    entries: Iterable[LatestByReviewer],
    author: Author,
    *,
    head_sha: str,
    backend: BackendName,
    reset_approvals_on_push: bool | None = None,
    retarget_at: str | None,
    retarget_known: bool,
) -> int | None:
    """Approvals at the current head with ``can_push`` true, or ``None`` (not approved).

    GitHub matches ``commit_sha`` to the head. GitLab has no sha on an approval, so the count
    exists only when ``reset_approvals_on_push`` is true. An approval older than the latest
    retarget does not count, nor does one with no time when a retarget exists. If the retarget
    could not be determined (``retarget_known`` false) no approval counts and the answer is
    ``None``. Every backend but ``gitlab`` compares the sha; an unknown backend is an error, as
    are entries naming one account twice.
    """
    if backend not in ("github", "gitlab"):
        raise ValueError(f"unknown backend {backend!r}")
    entries = tuple(entries)
    _reject_duplicate_accounts(entries)
    if not retarget_known:
        return None
    if backend == "gitlab" and reset_approvals_on_push is not True:
        return None
    counted: list[LatestByReviewer] = []
    for e in entries:
        if e.state != "approved" or e.can_push is not True:
            continue
        if _same_account(e.login, e.id, author):
            continue
        if backend != "gitlab" and e.commit_sha != head_sha:
            continue
        if retarget_at is not None and not _not_before(e.at, retarget_at):
            continue
        counted.append(e)
    return len(counted)


def github_review_decision(raw: str | None) -> ReviewDecision:
    """GitHub's ``reviewDecision``; null is ``none``; an unknown value never reads as approved."""
    if raw is None:
        return "none"
    table: dict[str, ReviewDecision] = {
        "APPROVED": "approved",
        "CHANGES_REQUESTED": "changes_requested",
        "REVIEW_REQUIRED": "review_required",
    }
    return table.get(raw, "review_required")


def gitlab_review_decision(
    entries: Iterable[LatestByReviewer], approvals_left: int | None, approvals: int
) -> ReviewDecision:
    """GitLab: ``changes_requested`` on any such entry; else ``review_required`` if approvals are
    left; else ``approved`` if any exist; else ``none``. An unknown ``approvals_left`` is ``none``
    unless a change was requested, never ``approved``."""
    if any(e.state == "changes_requested" for e in entries):
        return "changes_requested"
    if approvals_left is None:
        return "none"
    if approvals_left > 0:
        return "review_required"
    return "approved" if approvals > 0 else "none"


@dataclass(frozen=True, slots=True)
class Reviewer:
    login: str
    id: str | None
    kind: Literal["user", "team"]

    def to_dict(self) -> dict[str, Any]:
        return {"login": self.login, "id": self.id, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    reviewer: Reviewer
    state: Literal["outstanding", "reviewed"]
    code_owner: bool | None
    instance: str | None
    requested_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewer": self.reviewer.to_dict(),
            "state": self.state,
            "code_owner": self.code_owner,
            "instance": self.instance,
            "requested_at": self.requested_at,
        }

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (ascii_lower(self.reviewer.login), self.reviewer.kind, self.reviewer.login)


# --- threads and comments ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Comment:
    id: str
    author: Author
    body: str
    created_at: str
    updated_at: str
    minimized: bool
    reply_to: str | None
    review_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "author": self.author.to_dict(),
            "body": self.body,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "minimized": self.minimized,
            "reply_to": self.reply_to,
            "review_id": self.review_id,
        }

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.created_at, self.id)


@dataclass(frozen=True, slots=True)
class Thread:
    id: str
    resolvable: bool
    resolved: bool
    outdated: bool | None
    path: str | None
    line: int | None
    side: Literal["left", "right"] | None
    start_line: int | None
    commit_sha: str | None
    comments: tuple[Comment, ...]

    def __post_init__(self) -> None:
        if not self.comments:
            raise ValueError("a thread has at least one comment")
        if self.resolved and not self.resolvable:
            raise ValueError("a thread that is not resolvable is never resolved")
        for n in (self.line, self.start_line):
            if n is not None and n < 1:
                raise ValueError("a line number is null or at least 1")

    @property
    def ordered_comments(self) -> tuple[Comment, ...]:
        return tuple(sorted(self.comments, key=lambda c: c.sort_key))

    @property
    def sort_key(self) -> tuple[str, str]:
        first = self.ordered_comments[0]
        return (first.created_at, self.id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "resolvable": self.resolvable,
            "resolved": self.resolved,
            "outdated": self.outdated,
            "path": self.path,
            "line": self.line,
            "side": self.side,
            "start_line": self.start_line,
            "commit_sha": self.commit_sha,
            "comments": [c.to_dict() for c in self.ordered_comments],
        }


# --- files -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    previous_path: str | None
    status: FileStatus
    additions: int | None
    deletions: int | None

    def __post_init__(self) -> None:
        if (self.status == "renamed") != (self.previous_path is not None):
            raise ValueError("previous_path is set exactly when the file is renamed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "previous_path": self.previous_path,
            "status": self.status,
            "additions": self.additions,
            "deletions": self.deletions,
        }


# --- the PR object -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Head:
    sha: str
    ref: str
    repo: str | None
    is_fork: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {"sha": self.sha, "ref": self.ref, "repo": self.repo, "is_fork": self.is_fork}


@dataclass(frozen=True, slots=True)
class Base:
    ref: str
    sha: str | None
    is_default: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {"ref": self.ref, "sha": self.sha, "is_default": self.is_default}


@dataclass(frozen=True, slots=True)
class AutoMerge:
    enabled: bool
    by: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "by": self.by}


@dataclass(frozen=True, slots=True)
class RateLimit:
    host: str | None
    remaining: int | None
    reset_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"host": self.host, "remaining": self.remaining, "reset_at": self.reset_at}


@dataclass(frozen=True, slots=True)
class PR:
    pr: str
    repo: str
    pr_id: str
    number: int
    url: str
    backend: BackendName
    title: str
    body: str | None
    author: Author
    state: PrState
    draft: bool
    created_at: str
    updated_at: str
    closed_at: str | None
    merged_at: str | None
    merged_by: str | None
    labels: tuple[str, ...]
    author_association: str | None
    head: Head
    base: Base
    merged_head_sha: str | None
    merge_commit_sha: str | None
    head_fetch_ref: str
    mergeable: bool | None
    blocked_by: tuple[Blocker, ...]
    auto_merge: AutoMerge
    merge_queue: bool | None
    merge_methods_allowed: tuple[str, ...] | None
    additions: int | None
    deletions: int | None
    changed_files: int | None
    review_decision: ReviewDecision
    approvals: int
    approvals_at_head: int | None

    def __post_init__(self) -> None:
        if any(b.code is BlockerCode.CHECKS_ABSENT for b in self.blocked_by):
            raise ValueError("checks_absent never appears in a PR's blocked_by")
        if self.state != "open":
            if self.blocked_by:
                raise ValueError("a closed or merged PR has an empty blocked_by")
            if self.mergeable is not None:
                raise ValueError("a closed or merged PR has mergeable null")
        if self.state == "merged" and self.auto_merge.enabled:
            raise ValueError("auto_merge.enabled is false once merged")
        if self.state != "merged" and self.merge_commit_sha is not None:
            raise ValueError("merge_commit_sha is null until merged")
        methods = self.merge_methods_allowed
        if methods is not None and len(set(methods)) != len(methods):
            raise ValueError("merge_methods_allowed lists each method once")

    @property
    def approved_at_head(self) -> bool:
        """``approvals_at_head`` above zero. A null count is never approved."""
        return is_approved(self.approvals_at_head)

    @property
    def is_mergeable(self) -> bool:
        """Open, host-mergeable (``null`` is not) and nothing known blocks it. This does not say
        CI passed; read ``ci.state``."""
        return self.state == "open" and is_true(self.mergeable) and not self.blocked_by

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr": self.pr,
            "repo": self.repo,
            "pr_id": self.pr_id,
            "number": self.number,
            "url": self.url,
            "backend": self.backend,
            "title": self.title,
            "body": self.body,
            "author": self.author.to_dict(),
            "state": self.state,
            "draft": self.draft,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "closed_at": self.closed_at,
            "merged_at": self.merged_at,
            "merged_by": self.merged_by,
            "labels": sorted(self.labels),
            "author_association": self.author_association,
            "head": self.head.to_dict(),
            "base": self.base.to_dict(),
            "merged_head_sha": self.merged_head_sha,
            "merge_commit_sha": self.merge_commit_sha,
            "head_fetch_ref": self.head_fetch_ref,
            "mergeable": self.mergeable,
            "blocked_by": [b.to_dict() for b in sorted_blockers(self.blocked_by)],
            "auto_merge": self.auto_merge.to_dict(),
            "merge_queue": self.merge_queue,
            "merge_methods_allowed": (
                None if self.merge_methods_allowed is None else sorted(self.merge_methods_allowed)
            ),
            "additions": self.additions,
            "deletions": self.deletions,
            "changed_files": self.changed_files,
            "review_decision": self.review_decision,
            "approvals": self.approvals,
            "approvals_at_head": self.approvals_at_head,
        }


# --- blocked_by --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlockFacts:
    """What a backend read, in host-neutral terms. The backend maps its own statuses to these
    flags (one table per backend); `compute_blocked_by` applies the shared rules."""

    state: PrState
    ci: CI
    draft: bool = False
    conflicts: bool = False
    behind: bool = False
    mergeability_unknown: bool = False
    locked: bool = False
    reviews_required: bool = False
    #: GitLab `discussions_not_resolved`.
    host_discussions_unresolved: bool = False
    #: GitLab `requested_changes`.
    host_changes_requested: bool = False
    #: GitHub `mergeStateStatus: BLOCKED`.
    host_blocked: bool = False
    #: GitLab `ci_still_running`.
    host_checks_pending: bool = False
    #: GitLab `ci_must_pass`.
    host_ci_must_pass: bool = False
    #: GitHub: unresolved resolvable threads; None when not read.
    unresolved_threads: int | None = None
    #: Host statuses no code covers; each becomes `{code: other, detail}`.
    other_details: tuple[str, ...] = ()
    latest_by_reviewer: tuple[LatestByReviewer, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.state not in _PR_STATES:
            raise ValueError(f"unknown PR state {self.state!r}")
        _reject_duplicate_accounts(self.latest_by_reviewer)


def compute_blocked_by(facts: BlockFacts) -> tuple[Blocker, ...]:
    """The PR's `blocked_by`, from forgectl's own reading of checks and reviews as well as the
    host's status. Closed and merged PRs have none. Empty does not mean CI passed."""
    if facts.state != "open":
        return ()
    C = BlockerCode
    out: set[Blocker] = set()

    def add(code: BlockerCode, detail: str | None = None) -> None:
        out.add(Blocker(code, detail))

    if facts.draft:
        add(C.DRAFT)
    if facts.conflicts:
        add(C.CONFLICTS)
    if facts.behind:
        add(C.BRANCH_BEHIND)
    if facts.mergeability_unknown:
        add(C.MERGEABILITY_UNKNOWN)
    if facts.locked:
        add(C.OTHER, "locked")

    ci = facts.ci
    counted = [c for c in ci.checks if c.counted]
    # An old commit's checks say nothing about the head, so they cannot fail it.
    failing = ci.for_current_head and any(c.state == "failing" for c in counted)
    if failing:
        add(C.CHECKS_FAILING)
    if (
        ci.state == "pending"
        or any(c.state == "pending" for c in counted)
        or facts.host_checks_pending
        or (facts.host_ci_must_pass and not failing)
    ):
        add(C.CHECKS_PENDING)

    if facts.reviews_required:
        add(C.REVIEWS_REQUIRED)
    if facts.host_changes_requested or any(
        e.state == "changes_requested" and e.can_push is not False for e in facts.latest_by_reviewer
    ):
        add(C.CHANGES_REQUESTED)
    if facts.host_discussions_unresolved:
        add(C.DISCUSSIONS_UNRESOLVED)

    if facts.host_blocked:
        unexplained = not {b.code for b in out} - {C.OTHER}
        if ci.state == "none":
            add(C.CHECKS_PENDING)  # a required check that has not reported, whatever else blocks
        if unexplained:
            if facts.unresolved_threads is not None and facts.unresolved_threads > 0:
                add(C.DISCUSSIONS_UNRESOLVED)
            elif ci.state != "none":
                add(C.OTHER, "BLOCKED")

    for detail in facts.other_details:
        add(C.OTHER, detail)
    return sorted_blockers(out)


# --- snapshot and command results --------------------------------------------------------------

PUSH_SOURCES: frozenset[str] = frozenset({"push_event", "commit_date"})
# RFC 3339 with an explicit offset; the same shape as `ts` in schemas/defs.json.
_RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)


@dataclass(frozen=True, slots=True)
class PushTime:
    """When the current head was last pushed and how that was determined (interface: Push time).

    ``push_event`` is a host push event for the head sha; ``commit_date`` is the head commit's
    date, which can precede the push. An unknown time is not a ``PushTime``: ``Snapshot.head_push``
    is ``None`` and both serialized fields are null, so a time without a source cannot be
    expressed. The model reads no clock, so it cannot say a time lies in the future.
    """

    at: str
    source: PushSource
    #: ``at`` in UTC as emitted, computed once so ``to_dict`` cannot fail.
    _utc_text: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.source not in PUSH_SOURCES:
            raise ValueError(f"pushed_at_source must be one of {sorted(PUSH_SOURCES)}")
        if not isinstance(self.at, str) or not _RFC3339.fullmatch(self.at):
            raise ValueError("pushed_at must be an RFC 3339 time with an offset")
        try:
            # RFC 3339 allows lowercase `t` and `z`; fromisoformat does not.
            utc = datetime.fromisoformat(self.at.upper()).astimezone(UTC)
        except (ValueError, OverflowError):
            raise ValueError("pushed_at must be an RFC 3339 time with an offset") from None
        object.__setattr__(self, "_utc_text", utc.isoformat().removesuffix("+00:00") + "Z")

    def to_dict(self) -> dict[str, Any]:
        return {"pushed_at": self._utc_text, "pushed_at_source": self.source}


_UNKNOWN_PUSH: dict[str, Any] = {"pushed_at": None, "pushed_at_source": None}


@dataclass(frozen=True, slots=True)
class Snapshot:
    pr: PR
    review_requests: tuple[ReviewRequest, ...]
    teams_visible: bool
    reviews: tuple[Review, ...]
    latest_by_reviewer: tuple[LatestByReviewer, ...]
    threads: tuple[Thread, ...]
    ci: CI
    rate_limit: RateLimit
    fetched_at: str
    #: Parts a host cut off, from `review_requests`, `reviews`, `threads`; `ci` follows
    #: `ci.complete` and is added by `to_dict`, never by the caller.
    incomplete: tuple[str, ...] = ()
    #: `snapshot` only; ``None`` serializes as null/null (unknown).
    #: GitHub: `backends/github_snapshot.read_head_push` (the PR timeline, #12).
    #: TODO(#16): fill `Snapshot.head_push` from the host (GitLab: the MR versions).
    head_push: PushTime | None = None

    def __post_init__(self) -> None:
        unknown = set(self.incomplete) - INCOMPLETE_PARTS
        if unknown:
            raise ValueError(
                f"snapshot parts not allowed in incomplete: {sorted(unknown)}"
                " (ci follows ci.complete)"
            )
        _reject_duplicate_accounts(self.latest_by_reviewer)

    def to_dict(self) -> dict[str, Any]:
        incomplete = set(self.incomplete)
        if not self.ci.complete:
            incomplete.add("ci")
        out = self.pr.to_dict()
        out["head"] = {
            **out["head"],
            **(self.head_push.to_dict() if self.head_push else _UNKNOWN_PUSH),
        }
        out.update(
            {
                "review_requests": [
                    r.to_dict() for r in sorted(self.review_requests, key=lambda r: r.sort_key)
                ],
                "teams_visible": self.teams_visible,
                "reviews": [
                    r.to_dict() for r in sorted(self.reviews, key=lambda r: (r.submitted_at, r.id))
                ],
                "latest_by_reviewer": [
                    e.to_dict() for e in sorted(self.latest_by_reviewer, key=lambda e: e.sort_key)
                ],
                "threads": [t.to_dict() for t in sorted(self.threads, key=lambda t: t.sort_key)],
                "ci": self.ci.to_dict(),
                "rate_limit": self.rate_limit.to_dict(),
                "complete": not incomplete,
                "incomplete": sorted(incomplete),
                "fetched_at": self.fetched_at,
            }
        )
        return out


def comparable(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """A snapshot's dict without `fetched_at` and `rate_limit`, the only fields allowed to
    differ between two readings of an unchanged PR."""
    return {k: v for k, v in snapshot.items() if k not in ("fetched_at", "rate_limit")}


def _files_sorted(files: Iterable[FileChange]) -> list[dict[str, Any]]:
    return [f.to_dict() for f in sorted(files, key=lambda f: f.path)]


@dataclass(frozen=True, slots=True)
class FilesResult:
    pr: str
    head_sha: str
    files: tuple[FileChange, ...]
    files_complete: bool
    changed_files: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr": self.pr,
            "head_sha": self.head_sha,
            "files": _files_sorted(self.files),
            "files_complete": self.files_complete,
            "changed_files": self.changed_files,
        }


@dataclass(frozen=True, slots=True)
class CommentsResult:
    pr: str
    head_sha: str
    threads: tuple[Thread, ...]
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr": self.pr,
            "head_sha": self.head_sha,
            "threads": [t.to_dict() for t in sorted(self.threads, key=lambda t: t.sort_key)],
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class ReviewsResult:
    pr: str
    head_sha: str
    reviews: tuple[Review, ...]
    latest_by_reviewer: tuple[LatestByReviewer, ...]
    review_decision: ReviewDecision
    approvals: int
    approvals_at_head: int | None
    complete: bool

    def __post_init__(self) -> None:
        _reject_duplicate_accounts(self.latest_by_reviewer)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr": self.pr,
            "head_sha": self.head_sha,
            "reviews": [
                r.to_dict() for r in sorted(self.reviews, key=lambda r: (r.submitted_at, r.id))
            ],
            "latest_by_reviewer": [
                e.to_dict() for e in sorted(self.latest_by_reviewer, key=lambda e: e.sort_key)
            ],
            "review_decision": self.review_decision,
            "approvals": self.approvals,
            "approvals_at_head": self.approvals_at_head,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class CiResult:
    pr: str
    head_sha: str
    ci: CI

    def to_dict(self) -> dict[str, Any]:
        return {"pr": self.pr, "head_sha": self.head_sha, "ci": self.ci.to_dict()}


@dataclass(frozen=True, slots=True)
class DiffResult:
    pr: str
    head_sha: str
    base_sha: str | None
    merge_base_sha: str | None
    diff: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr": self.pr,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "merge_base_sha": self.merge_base_sha,
            "diff": self.diff,
        }


@dataclass(frozen=True, slots=True)
class CompareResult:
    pr: str
    from_sha: str
    to_sha: str
    status: CompareStatus
    diff: str | None

    def __post_init__(self) -> None:
        if self.status in ("behind", "diverged", "unavailable"):
            if self.diff is not None:
                raise ValueError(f"compare status {self.status!r} has no diff")
        elif self.diff is None:
            raise ValueError(f"compare status {self.status!r} needs a diff")
        elif self.status == "identical" and self.diff != "":
            raise ValueError("an identical compare has an empty diff")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr": self.pr,
            "from_sha": self.from_sha,
            "to_sha": self.to_sha,
            "status": self.status,
            "diff": self.diff,
        }


@dataclass(frozen=True, slots=True)
class AccessResult:
    repo: str
    user: str
    write_access: bool | None
    level: Literal["admin", "maintain", "write", "triage", "read", "none"] | None
    source: Literal["collaborator_permission", "project_member"]
    reason: Literal["no_permission_to_read"] | None

    def __post_init__(self) -> None:
        if (self.write_access is None) != (self.reason is not None):
            raise ValueError("reason is set exactly when write_access is null")

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "user": self.user,
            "write_access": self.write_access,
            "level": self.level,
            "source": self.source,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Identity:
    """The authenticated account on one host (the backend's part of `whoami`)."""

    host: str
    backend: BackendName
    login: str | None
    id: str | None
    scopes: tuple[str, ...] | None


# --- queries (identifiers are validated and bound by identifiers.py) ----------------------------


@dataclass(frozen=True, slots=True)
class PrQuery:
    """The filters of `prs`, already validated; `me` already resolved to a login."""

    repo: RepoRef | None = None
    host: str | None = None
    author: str | None = None
    review_requested_from: str | None = None
    state: Literal["open", "closed", "merged", "all"] = "open"
    draft: bool | None = None
    updated_since: str | None = None
    limit: int = 100


@dataclass(frozen=True, slots=True)
class PrListItem:
    """One item of `prs` (`defs#/$defs/pr_list_item`); a request instance needs `snapshot`."""

    pr: str
    pr_id: str
    repo: str
    number: int
    url: str
    title: str
    author: Author
    state: PrState
    draft: bool
    labels: tuple[str, ...]
    head: Head
    base_ref: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr": self.pr,
            "pr_id": self.pr_id,
            "repo": self.repo,
            "number": self.number,
            "url": self.url,
            "title": self.title,
            "author": self.author.to_dict(),
            "state": self.state,
            "draft": self.draft,
            "labels": sorted(self.labels),
            "head": self.head.to_dict(),
            "base": {"ref": self.base_ref},
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class PrPage:
    """One backend's answer to `prs`: PRs newest `updated_at` first, then `pr`."""

    items: tuple[PrListItem, ...]
    complete: bool
    rate_limit: RateLimit


@dataclass(frozen=True, slots=True)
class PrRead:
    """`get_pr`'s answer: the PR and the CI of the same read, so a caller never pairs a PR with
    checks read at another head.

    ``latest_by_reviewer`` is the table ``pr``'s review fields were computed from (not part of
    ``show``'s output), so a later read of the reviews can be checked to agree with it."""

    pr: PR
    ci: CI
    latest_by_reviewer: tuple[LatestByReviewer, ...]
