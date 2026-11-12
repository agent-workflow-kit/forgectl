"""``review`` on GitHub: the post order of interface.md "Post order" for one atomic review.

The input is ``--file`` JSON (``schemas/review-input.json``): ``{event, body, comments}``. The steps
are those of ``post.py``, with the review's own parts:

1. usage: read at most 16 MiB, parse and validate the input, at most 200 inline comments,
   neutralize every body, add the dedupe marker, check each size, 2. the audit file can be appended,
   the account and the PR are read, then ``not_open``, ``stale_head`` and ``stale_base``, then
   whether the state step is still needed, 3. positions (``positions.check_github``) and the head
   they were checked at, 4. dedupe, 6. confirmation (``--dry-run`` stops here), 7. ``pre-post``
   hooks, 8. a re-read after a hook or a dialog, 9. audit ``attempted`` and the one atomic
   ``POST /pulls/N/reviews``, 11. the read-back and, after an approval, the head read, 12. the
   audit result, ``post-post`` hooks, output.

GitHub posts text and state together, so the state step is not a second write: a dedupe hit with a
state step still needed is ``outcome_partial`` with no write (a dismissed approval, or a reused
key), and a new key posts both.

GitLab (#27) has no atomic review: the text is draft notes, published together by one
``bulk_publish`` and read back through the MR's discussions, then the state step runs (GraphQL
``mergeRequestRequestChanges`` or ``POST /approve`` with ``sha``). That path is ``_send_gitlab``;
the host calls are in ``backends/gitlab_review.py``. A dedupe hit leaves no text to post but the
state step still runs when needed (``follow_up_done``).

``approve`` (#28) is this same run with ``action == "approve"``: the event is always ``approve``,
the text is the ``--body-file`` body alone (GitHub: the review's body; GitLab: a note posted by
``POST /notes``, then ``POST /approve``). The state step shared with ``review``'s ``approve`` event
is decided in ``_approval_state``: the ``self_approval`` check (``_check_self``), whether this
account's approval already counts for this head and base (GitHub: the latest approval at the head
and not older than the latest retarget; GitLab: ``user_has_approved``) and an exact
``head_verified``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import audit, hooks, jsonlog, output, positions, registry
from .backends.github_post import GitHubPoster
from .backends.github_review import GitHubReviewer, Submitted
from .backends.gitlab_post import GitLabPoster
from .backends.gitlab_review import Found, GitLabReviewer, host_rejected, matches_sent
from .errors import ForgectlError, SignalExit, refused, usage
from .humanactions import HumanContext, require_confirmation
from .identifiers import PrRef
from .merge import base_matches
from .model import PR, LatestByReviewer, PrRead, not_before, same_account
from .neutralize import neutralize
from .post import (
    GITHUB_COMMENT_LIMIT,
    GITLAB_LIMIT,
    INLINE_COMMENT_LIMIT,
    marker_line,
    prepare_body,
    read_input,
)
from .reads import NO_READINESS, ReadEnv, backend_for, make_env
from .sanitize import sanitize_line

#: Interface, Post order step 1.
FILE_LIMIT = 16 * 1024 * 1024
MAX_COMMENTS = 200
EVENTS = ("comment", "request_changes", "approve")
_TOP_KEYS = frozenset({"event", "body", "comments"})
_COMMENT_KEYS = frozenset({"path", "line", "side", "start_line", "body"})
STATE_EVENTS = ("approve", "request_changes")


# -- the input -----------------------------------------------------------------------------------


def _too_large(limit: int, index: int | None, what: str | None = None) -> ForgectlError:
    what = what or ("the review body" if index is None else f"inline comment {index}")
    return ForgectlError(
        "body_too_large",
        f"{what} is larger than {limit} bytes",
        details={"index": index, "limit": limit},
    )


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key is repeated")
    return dict(pairs)


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _text(value: object, where: str) -> str:
    """A string field: it must encode as UTF-8 (JSON allows a lone surrogate, UTF-8 does not)."""
    if not isinstance(value, str):
        raise usage(f"review file: {where} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise usage(f"review file: {where} is not valid text (a lone surrogate)") from None
    return value


def _int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise usage(f"review file: {where} must be an integer of at least 1")
    return value


@dataclass(frozen=True)
class RawComment:
    index: int
    path: str
    line: int
    side: str
    start_line: int | None
    body: str


@dataclass(frozen=True)
class RawReview:
    event: str
    body: str | None
    comments: tuple[RawComment, ...]


def parse_input(raw: bytes) -> RawReview:
    """The file as ``schemas/review-input.json`` and the interface's own rules (an empty review,
    ``request_changes`` with no body, more than 200 comments) allow it; else exit 2 naming the
    field. A repeated key, ``NaN`` and a lone surrogate are refused: the text posted must be the
    text meant."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise usage("the review file is not valid UTF-8") from None
    try:
        doc = json.loads(text, object_pairs_hook=_pairs, parse_constant=_no_constant)
    except (ValueError, RecursionError) as err:
        raise usage(f"the review file is not valid JSON ({type(err).__name__})") from None
    if not isinstance(doc, dict):
        raise usage("review file: the top level must be an object")
    extra = sorted(set(doc) - _TOP_KEYS)
    if extra:
        raise usage(f"review file: unknown field {sanitize_line(str(extra[0]))!r}")
    event = doc.get("event")
    if event not in EVENTS:
        raise usage("review file: event must be comment, request_changes or approve")
    body = doc.get("body")
    if body is not None:
        body = _text(body, "body")
        if not body.strip():
            raise usage("review file: body is empty (use null for none)")
    items = doc.get("comments", [])
    if not isinstance(items, list):
        raise usage("review file: comments must be a list")
    if len(items) > MAX_COMMENTS:
        raise usage(f"review file: comments holds {len(items)} items; at most {MAX_COMMENTS}")
    comments = tuple(_comment(i, item) for i, item in enumerate(items))
    if event == "request_changes" and body is None:
        raise usage("review file: request_changes needs a body")
    if event == "comment" and body is None and not comments:
        raise usage("review file: an empty review (a comment with no body and no comments)")
    return RawReview(event, body, comments)


def _comment(index: int, item: object) -> RawComment:
    where = f"comments[{index}]"
    if not isinstance(item, dict):
        raise usage(f"review file: {where} must be an object")
    extra = sorted(set(item) - _COMMENT_KEYS)
    if extra:
        raise usage(f"review file: {where}: unknown field {sanitize_line(str(extra[0]))!r}")
    for key in ("path", "line", "body"):
        if key not in item:
            raise usage(f"review file: {where}.{key} is required")
    path = _text(item["path"], f"{where}.path")
    if not path:
        raise usage(f"review file: {where}.path is empty")
    side = item.get("side", "right")
    if side not in positions.SIDES:
        raise usage(f"review file: {where}.side must be left or right")
    start = item.get("start_line")
    if "start_line" in item:
        start = _int(start, f"{where}.start_line")
    body = _text(item["body"], f"{where}.body")
    if not body.strip():
        raise usage(f"review file: {where}.body is empty")
    return RawComment(index, path, _int(item["line"], f"{where}.line"), side, start, body)


@dataclass(frozen=True)
class Comment:
    """An inline comment as it will be posted (neutralized)."""

    raw: RawComment
    body: str


@dataclass(frozen=True)
class Prepared:
    event: str
    body: str | None  # as posted: neutralized, with the marker
    comments: tuple[Comment, ...]
    sha256: str | None
    nbytes: int
    neutralized_lines: int
    markers_removed: int

    @property
    def has_text(self) -> bool:
        return self.body is not None or bool(self.comments)


def canonical(body: str | None, comments: tuple[Comment, ...]) -> bytes:
    """The bytes ``body_sha256`` hashes: JSON of ``{"body", "comments"}`` as posted, ``comments``
    in input order, each ``{"body", "line", "path", "side", "start_line"}`` (``side`` always
    given, ``start_line`` null when absent), keys sorted at every level, separators ``,`` and ``:``
    with no spaces, non-ASCII written as UTF-8 and only ``"`` ``\\`` and control characters
    escaped (Python's ``json.dumps(..., sort_keys=True, ensure_ascii=False)``), no final newline."""
    doc = {
        "body": body,
        "comments": [
            {
                "path": c.raw.path,
                "line": c.raw.line,
                "side": c.raw.side,
                "start_line": c.raw.start_line,
                "body": c.body,
            }
            for c in comments
        ],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _with_marker(text: str, marker: str) -> str:
    return text + ("" if text.endswith("\n") else "\n") + marker


def prepare(raw: RawReview, key: str | None, backend: str = "github") -> Prepared:
    """Neutralize each body on its own, add the marker, and check each size (index null for the
    review body). The marker is the review body's last line; with no body it is a body holding only
    the marker on GitHub and the last inline comment's last line on GitLab (Dedupe key, Where)."""
    gitlab = backend == "gitlab"
    body_limit = GITLAB_LIMIT if gitlab else GITHUB_COMMENT_LIMIT
    inline_limit = GITLAB_LIMIT if gitlab else INLINE_COMMENT_LIMIT
    if key is not None and raw.body is None and not raw.comments:
        raise usage("--dedupe-key needs a review body or inline comments to carry the marker")
    neutralized = markers = 0
    body: str | None = None
    if raw.body is not None:
        done = neutralize(raw.body)
        neutralized += done.neutralized_lines
        markers += done.markers_removed
        body = done.text
    marker = None if key is None else marker_line(key)
    if marker is not None and not (gitlab and body is None):
        body = marker if body is None else _with_marker(body, marker)
    if body is not None and len(body.encode("utf-8")) > body_limit:
        raise _too_large(body_limit, None)
    comments: list[Comment] = []
    for n, item in enumerate(raw.comments):
        done = neutralize(item.body)
        neutralized += done.neutralized_lines
        markers += done.markers_removed
        text = done.text
        if marker is not None and gitlab and body is None and n == len(raw.comments) - 1:
            text = _with_marker(text, marker)
        if len(text.encode("utf-8")) > inline_limit:
            raise _too_large(inline_limit, item.index)
        comments.append(Comment(item, text))
    parts = tuple(comments)
    if body is None and not parts:
        return Prepared(raw.event, None, parts, None, 0, neutralized, markers)
    data = canonical(body, parts)
    total = len((body or "").encode("utf-8")) + sum(len(c.body.encode("utf-8")) for c in parts)
    return Prepared(
        raw.event, body, parts, hashlib.sha256(data).hexdigest(), total, neutralized, markers
    )


# -- the run -------------------------------------------------------------------------------------


@dataclass
class State:
    """Whether the review's state step is still needed (Post order step 2) and whether an approval
    of this account at this head already exists."""

    needed: bool = False
    approved_at_head: bool = False
    #: ``head_verified`` of an approval that already existed: null when none did
    verified: bool | None = None


@dataclass
class Outcome:
    result: str  # an audit result: posted, approved, failed, outcome_unknown
    review: Submitted | None = None
    landed: bool = False  # the text or the state is on the host (GitLab has no review object)
    follow_up_done: bool = False
    ids: list[str] = field(default_factory=list)
    thread_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: ForgectlError | None = None


def _mine(
    entries: tuple[LatestByReviewer, ...], user_id: str, login: str
) -> LatestByReviewer | None:
    for entry in entries:
        if entry.id is not None:
            if entry.id == user_id:
                return entry
        elif entry.login.lower() == login.lower():
            return entry
    return None


#: The host answers a failed draft creation or publish keeps as they are: the Reliability table's
#: rows for 401, 403 (not a rate limit), 404 and 429 (or a rate-limit 403).
_HOST_ANSWERS = frozenset({"auth_required", "permission_denied", "not_found", "rate_limited"})


def _leftover_text(left: list[int], unknown: bool) -> str:
    """What a failed post leaves on the merge request, for the end of an error message."""
    out = ""
    if left:
        ids = ", ".join(str(i) for i in sorted(left))
        out += f" (drafts left on the merge request: {ids})"
    if unknown:
        out += (
            " (drafts of unknown ids may remain on the merge request: the re-list failed; "
            "the next run exits 20 drafts_present until they are gone)"
        )
    return out


class ReviewRun:
    def __init__(self, inv: registry.Invocation, action: str = "review") -> None:
        self.action = action
        if inv.human is None:  # cli gates every write command and passes its context
            raise ForgectlError("internal", f"{inv.command}: no human-action context")
        self.inv = inv
        self.args = inv.args
        self.human: HumanContext = inv.human
        ref = inv.args.pr
        if not isinstance(ref, PrRef):
            raise ForgectlError("internal", f"{inv.command}: the pull request was not bound")
        self.ref: PrRef = ref
        self.pr_id: str = str(ref)
        self.env: ReadEnv = make_env(inv)
        self.backend = backend_for(ref, self.env)
        self.gitlab = ref.backend == "gitlab"
        self.poster: GitHubPoster | GitLabPoster
        if self.gitlab:
            self.poster = GitLabPoster(self.env)
            self.drafts = GitLabReviewer(self.env, self.poster)
        else:
            self.poster = GitHubPoster(self.env)
        self.reviewer = GitHubReviewer(self.env)
        self.hits: list[str] = []
        self._gl_positions: dict[int, dict[str, Any]] = {}
        self._first_drafts: list[int] = []
        try:
            self.directory: Path = jsonlog.state_dir(os.environ)
        except jsonlog.JsonlError as err:
            raise ForgectlError(
                "audit_unavailable", f"audit.jsonl cannot be appended to: {err}"
            ) from None
        self.decision_ids: list[str] = list(self.human.decision_ids)
        self.escalated = False
        self.prep: Prepared | None = None
        self.head_sha: str | None = None
        self.expected_head: str | None = self.args.expected_head
        self.expected_base: str | None = self.args.expected_base
        self.state = State()
        self._login = ""
        self._account = ""
        self._checked_head: str | None = None
        self._placements: tuple[positions.Placement, ...] = ()
        self._attempt_record: audit.Attempt | None = None

    # -- helpers -------------------------------------------------------------------------------

    def _log(self, level: str, event: str, details: dict[str, Any]) -> None:
        text = jsonlog.log_event(
            self.directory, level, event, pr=self.pr_id, host=self.ref.host, details=details
        )
        if text:
            output.warn(text)

    def _plan_dict(self) -> dict[str, Any]:
        prep = self.prep
        return {
            "event": self._event(),
            "body_sha256": None if prep is None else prep.sha256,
            "body_bytes": 0 if prep is None else prep.nbytes,
            "inline_comments": 0 if prep is None else len(prep.comments),
        }

    def _event(self) -> str | None:
        """The ``event`` of the plan, the hooks and the audit record: null for ``approve``."""
        if self.prep is None or self.action == "approve":
            return None
        return self.prep.event

    def _verified(self) -> bool | None:
        if self.prep is not None and self.prep.event == "approve":
            return self.state.verified
        return None

    def _fields(self, **extra: Any) -> dict[str, Any]:
        prep = self.prep
        out: dict[str, Any] = {
            "pr": self.pr_id,
            "action": self.action,
            "ids": [],
            "thread_ids": [],
            "review_id": None,
            "already_posted": False,
            "follow_up_done": False,
            "neutralized_lines": 0 if prep is None else prep.neutralized_lines,
            "markers_removed": 0 if prep is None else prep.markers_removed,
            "warnings": [],
            "head_sha": self.head_sha,
            "head_verified": self._verified(),
            "dry_run": self.human.dry_run,
        }
        out.update(extra)
        return out

    def run(self) -> dict[str, Any]:
        try:
            return self._run()
        except ForgectlError as err:
            if self.human.dry_run and err.exit_code == 20:
                if err.plan is None:
                    err.plan = self._plan_dict()
                err.fields = {
                    **(err.fields or {}),
                    "pr": self.pr_id,
                    "head_sha": self.head_sha,
                    "head_verified": self._verified(),
                    "neutralized_lines": 0 if self.prep is None else self.prep.neutralized_lines,
                    "markers_removed": 0 if self.prep is None else self.prep.markers_removed,
                }
            raise

    # -- steps 1 to 4 --------------------------------------------------------------------------

    def _prepare(self) -> Prepared:
        if self.action == "approve":  # the body is plain text, posted like a comment's
            if self.args.body_file is None:
                return Prepared("approve", None, (), None, 0, 0, 0)
            limit = GITLAB_LIMIT if self.gitlab else GITHUB_COMMENT_LIMIT
            body = prepare_body(self.args.body_file, limit=limit, key=self.args.dedupe_key)
            return Prepared(
                "approve",
                body.text,
                (),
                body.sha256,
                body.nbytes,
                body.neutralized_lines,
                body.markers_removed,
            )
        raw = read_input(self.args.file, FILE_LIMIT + 1)
        if len(raw) > FILE_LIMIT:
            raise _too_large(FILE_LIMIT, None, "the review file")
        return prepare(parse_input(raw), self.args.dedupe_key, self.ref.backend)

    def _read(self) -> PR:
        """Step 2: the PR, then ``not_open``, ``stale_head`` and ``stale_base`` in that order,
        then whether the state step is needed. An interactive run pins the head and base here."""
        assert self.prep is not None
        read = self.backend.get_pr(self.ref, NO_READINESS)
        pr = read.pr
        self.pr_id = f"{pr.repo}#{pr.number}"
        self.head_sha = pr.head.sha
        if self.expected_head is None and self.human.interactive:
            self.expected_head = pr.head.sha
        if self.expected_base is None and self.human.interactive:
            self.expected_base = pr.base.ref
        if self.expected_head is None or self.expected_base is None:
            raise usage(
                f"{self.action} needs --expected-head and --expected-base unless interactive"
            )
        if pr.state != "open":
            raise refused("not_open", "the pull request is not open", details={"state": pr.state})
        if pr.head.sha != self.expected_head:
            raise refused(
                "stale_head", "the head is not the expected head", details={"head_sha": pr.head.sha}
            )
        if not base_matches(pr, self.expected_base):
            raise refused(
                "stale_base",
                "the base is not the expected base",
                details={"base_ref": pr.base.ref},
            )
        if self.gitlab and self.prep.event == "request_changes":
            version = self.backend.host_version(self.ref.host)  # type: ignore[attr-defined]
            if version is None or version < (17, 2):
                raise ForgectlError(
                    "unsupported_host_version",
                    "request_changes needs GitLab 17.2 or newer "
                    "(mergeRequestRequestChanges); the version is older or unreadable",
                )
        event = self.prep.event
        if event == "approve":
            self._check_self(pr)
        mine = _mine(read.latest_by_reviewer, self._account, self._login)
        if event == "approve":
            self.state = self._approval_state(read, mine)
        else:
            self.state = State(
                needed=event == "request_changes"
                and (mine is None or mine.state != "changes_requested")
            )
        return pr

    def _check_self(self, pr: PR) -> None:
        """``self_approval`` (Post order step 2): the author is the authenticated account, or the
        configured ``me``. ``--allow-self`` (human only) skips it; GitHub still refuses."""
        if self.args.allow_self:
            return
        author = pr.author
        basis: str | None = None
        if same_account(self._login, self._account, author):
            basis = "authenticated"
        else:
            me = self.env.hosts.me(self.ref.host)
            if (
                me is not None
                and author.login is not None
                and me.casefold() == author.login.casefold()
            ):
                basis = "configured_me"
        if basis is not None:
            raise refused(
                "self_approval",
                "the pull request's author is the account that would approve it",
                details={"basis": basis},
            )

    def _approval_state(self, read: PrRead, mine: LatestByReviewer | None) -> State:
        """Whether this account's approval already counts, and ``head_verified`` for it.

        GitHub: the latest review by this account is an approval at the head, not older than the
        latest retarget (an unknown retarget counts nothing). It is then verified. An approval at
        another head, or older than a retarget, is none: the approval is sent again, null.
        GitLab: ``user_has_approved`` decides, retarget or not (there is no re-approval path);
        it is verified only when ``reset_approvals_on_push`` is true and the retarget walk found
        none newer than the approval."""
        pr = read.pr
        if not self.gitlab:
            counts = (
                read.retarget_known
                and mine is not None
                and mine.state == "approved"
                and mine.commit_sha == pr.head.sha
                and (read.retarget_at is None or not_before(mine.at, read.retarget_at))
            )
            return State(
                needed=not counts, approved_at_head=counts, verified=True if counts else None
            )
        self.poster.prepare(pr)  # the MR's ids are read here, not inside the audited window
        has = self.env.call(lambda: self.drafts.user_has_approved(pr))
        if not has:
            return State(needed=True)
        return State(
            needed=False, approved_at_head=True, verified=self._gitlab_verified(read, mine)
        )

    @staticmethod
    def _gitlab_verified(read: PrRead, mine: LatestByReviewer | None) -> bool:
        if read.reset_on_push is not True or not read.retarget_known:
            return False
        if read.retarget_at is None:
            return True
        return mine is not None and not_before(mine.at, read.retarget_at)

    def _positions(self, pr: PR) -> None:
        """Step 3: every inline comment is on a line of the diff, as read at this head."""
        assert self.prep is not None
        if not self.prep.comments:
            return
        asked = [
            positions.InlineComment(
                c.raw.index, c.raw.path, c.raw.line, c.raw.side, c.raw.start_line
            )
            for c in self.prep.comments
        ]
        if self.gitlab:
            checked = positions.check_gitlab(self.backend, self.ref, asked)  # type: ignore[arg-type]
        else:
            checked = positions.check_github(self.env, self.ref, asked)
        if checked.head_sha != pr.head.sha:
            raise refused(
                "stale_head",
                "the head moved while the positions were checked",
                details={"head_sha": checked.head_sha},
            )
        self._checked_head = checked.head_sha
        self._placements = checked.placements
        if self.gitlab:  # a missing start sha is upstream_error here, before anything is audited
            assert checked.pin is not None
            self._gl_positions = {
                p.index: p.gitlab_position(checked.pin) for p in checked.placements
            }

    def _run(self) -> dict[str, Any]:
        args = self.args
        self.prep = prep = self._prepare()
        audit.check_writable(self.directory)
        target = self.env.hosts.target(self.ref.host)
        user = self.env.call(lambda: self.env.hosts.authenticated_user(target))
        self._login, self._account = user.login, user.id
        pr = self._read()
        self._positions(pr)
        # step 4: dedupe. A hit leaves no text to post; with no state step left it is exit 0.
        hits: list[str] = []
        if args.dedupe_key is not None:
            marker = marker_line(args.dedupe_key)
            hits = self.env.call(lambda: self.poster.find_marker(pr, user.id, marker))
        self.hits = hits
        if self.gitlab and self.action == "review" and prep.has_text and not hits:  # step 5
            self._first_drafts = [d.id for d in self.env.call(lambda: self.drafts.list_drafts(pr))]
            if self._first_drafts:
                raise refused(
                    "drafts_present",
                    f"this account has {len(self._first_drafts)} draft note(s) on the merge "
                    "request: finish or delete the pending review in the web UI first",
                    details={"count": len(self._first_drafts)},
                )
        if hits and self.state.needed and not self.gitlab:
            raise ForgectlError(
                "outcome_partial",
                f"the review's text is already posted but the {prep.event} state is not in "
                "effect (dismissed, no longer counting for this head, or the key was reused); "
                "nothing was sent",
                details={"landed": {"ids": hits, "thread_ids": []}, "failed": prep.event},
            )
        if (hits and not self.state.needed) or (not prep.has_text and not self.state.needed):
            return self._out(already=True, hits=hits)
        # step 6: confirmation. A dry run stops here; any other run without one is exit 13.
        dialog = require_confirmation(self.human)
        if self.human.dry_run:
            return self._out()
        if dialog and not self._dialog(pr):
            raise ForgectlError("confirmation_required", "declined")
        # step 7: pre-post hooks (a veto or escalation raises)
        pre = hooks.run_pre_hooks(
            self.env.gateway,
            "pre-post",
            self._hook_fields(pr),
            decision_ids=self.decision_ids,
            override_escalation=bool(args.override_escalation),
        )
        for text in pre.warnings:
            output.warn(text)
        self.decision_ids = list(pre.decision_ids)
        self.escalated = bool(pre.escalated)
        # step 8: a slow gate may have outlasted what it saw
        if dialog or hooks.entries(self.inv.config.config, "pre-post"):
            pr = self._read()
            if self._checked_head is not None and pr.head.sha != self._checked_head:
                raise refused(
                    "stale_head",
                    "the head moved after the positions were checked",
                    details={"head_sha": pr.head.sha},
                )
            if not (prep.has_text and not self.hits) and not self.state.needed:
                return self._out(already=True, hits=self.hits)
        return self._write(pr)

    # -- confirmation and hooks ----------------------------------------------------------------

    def _dialog(self, pr: PR) -> bool:
        assert self.prep is not None
        lines = [
            f"{self.action} on {self.pr_id}",
            f"  head:    {pr.head.sha}",
        ]
        if self.action == "review":
            lines.append(f"  event:   {self.prep.event}")
            lines.append(f"  inline:  {len(self.prep.comments)} comment(s)")
        if self.prep.sha256 is not None:
            lines.append(f"  text:    {self.prep.nbytes} bytes, sha256 {self.prep.sha256}")
        if self.prep.neutralized_lines:
            lines.append(f"  note:    {self.prep.neutralized_lines} line(s) given a leading space")
        sys.stderr.write("\n".join(sanitize_line(line) for line in lines) + "\n")
        sys.stderr.write("Post? [y/N] ")
        sys.stderr.flush()
        return sys.stdin.readline().strip().lower() in ("y", "yes")

    def _hook_fields(self, pr: PR) -> dict[str, Any]:
        assert self.prep is not None
        return {
            "object_id": self.pr_id,
            "repo": pr.repo,
            "head_sha": pr.head.sha,
            "actor": self.human.actor,
            "action": self.action,
            "event": self._event(),
            "body_sha256": self.prep.sha256,
            "body_bytes": self.prep.nbytes,
            "thread_id": None,
            "thread_author_is_self": None,
            "reply_to": None,
            "override_escalation": bool(self.args.override_escalation),
            "allow_self": bool(self.args.allow_self),
        }

    def _post_hooks(self, pr: PR, outcome: Outcome) -> None:
        fields = self._hook_fields(pr)
        del fields["override_escalation"], fields["allow_self"]
        fields["result"] = outcome.result
        fields["ids"] = self._audit_ids(outcome)
        result = hooks.run_post_hooks(
            self.env.gateway,
            "post-post",
            fields,
            decision_ids=self.decision_ids,
            state_dir=str(self.directory),
            pr=self.pr_id,
        )
        for text in result.warnings:
            output.warn(text)
        for report in result.reports:
            if report.outcome == "failed":
                self._log(
                    "warn",
                    "hook_failed",
                    {"hook": report.hook, "index": report.index, "why": report.detail},
                )

    # -- steps 9 to 12 -------------------------------------------------------------------------

    # TODO(#29): switch to the shared post.py write helpers once the #29 refactor lands
    # (_post_hooks, _write and _attempt repeat post.py's audit window, hooks and signal handling)

    @staticmethod
    def _audit_ids(outcome: Outcome) -> list[str]:
        head = [outcome.review.review_id] if outcome.review else []
        return head + outcome.ids

    def _write(self, pr: PR) -> dict[str, Any]:
        assert self.prep is not None
        prep = self.prep
        overrides = ["override_escalation"] if self.escalated else []
        inputs = audit.AuditInputs(
            config_hash=self.inv.config.config_hash,
            config_path=self.inv.config.config_path,
            token_source=self.env.hosts.credential(self.ref.host).source,
            stdin_tty=self.human.stdin_tty,
            actor_source=self.human.actor_source,
            head_sha=pr.head.sha,
            base_ref=pr.base.ref,
            event=self._event(),
            body_sha256=prep.sha256,
            host_login=self._login,
        )
        context = audit.AuditContext(
            actor=self.human.actor,
            decision_ids=self.decision_ids,
            target=self.pr_id,
            action=self.action,
            inputs=inputs,
            override=overrides,
        )
        if self.env.gateway.budget.remaining() <= 0:  # nothing was sent: no `attempted` record
            raise ForgectlError("network_error", "the time budget ran out before the review")
        audit_error: ForgectlError | None = None
        signalled = False
        with audit.Attempt(self.directory, context) as attempt:
            self._attempt_record = attempt
            attempt.begin()  # a failure here is audit_unavailable and nothing has been sent
            outcome = self._attempt(pr)
            try:
                attempt.finish(outcome.result, ids=self._audit_ids(outcome))
            except ForgectlError as err:  # audit_failed: the post- hooks still run
                audit_error = err
            except SignalExit:
                if not attempt.finished:
                    raise  # the outcome is not on record: Attempt records outcome_unknown
                signalled = True
                audit_error = attempt.finish_error
        if not signalled:
            try:
                self._post_hooks(pr, outcome)
            except SignalExit:
                pass  # the running hook's group was ended; the remaining hooks do not run
        if audit_error is not None:
            raise audit_error
        if outcome.error is not None:
            raise outcome.error
        assert outcome.landed
        return self._out(outcome=outcome, already=bool(self.hits), hits=self.hits)

    def _attempt(self, pr: PR) -> Outcome:
        """The write and its read-back. A bug here (any non-forgectl exception) is an unknown
        outcome: the write may have landed, so the audit result and the post- hooks still follow.
        A signal is not caught."""
        assert self.prep is not None
        try:
            return self._send_gitlab(pr) if self.gitlab else self._send(pr)
        except ForgectlError as err:
            unknown = err.code in ("outcome_unknown", "wrong_host_response")
            return Outcome("outcome_unknown" if unknown else "failed", error=err)
        except Exception as exc:
            try:
                self._log("error", "internal", {"exception": type(exc).__name__})
            except Exception:  # a failing log must not skip the record and the hooks
                pass
            return Outcome(
                "outcome_unknown",
                error=ForgectlError(
                    "outcome_unknown",
                    f"internal error after the review was sent ({type(exc).__name__}); "
                    "it may have landed",
                ),
            )

    def _send(self, pr: PR) -> Outcome:
        prep = self.prep
        assert prep is not None
        by_index = {p.index: p for p in self._placements}
        comments = [
            {**by_index[c.raw.index].github_fields(), "body": c.body} for c in prep.comments
        ]
        review = self.reviewer.submit(
            pr,
            event=prep.event,
            commit_id=pr.head.sha,
            body=prep.body,
            comments=comments,
            account_id=self._account,
        )
        done = Outcome("approved" if prep.event == "approve" else "posted", review, landed=True)
        if self._attempt_record is not None:  # a signal from here on still records the proven id
            self._attempt_record.unknown_ids = [review.review_id]
        # step 11: the read-back; a read that fails is a warning, the review has landed
        try:
            ids = self.reviewer.read_comments(pr, review)
            if len(ids) != len(comments):
                raise ForgectlError("upstream_error", "the read-back does not match what was sent")
            done.ids = ids
        except ForgectlError:
            done.warnings.append("read_back_failed")
        if prep.event == "approve":
            self._after_approval(pr, done)
        return done

    def _after_approval(self, pr: PR, done: Outcome) -> None:
        """After an approval this run sent: is it still this head? A failed read is a warning."""
        try:
            after = self.backend.get_pr(self.ref, NO_READINESS).pr
        except ForgectlError:
            if "read_back_failed" not in done.warnings:
                done.warnings.append("read_back_failed")
        else:
            assert self.expected_base is not None
            if after.head.sha != pr.head.sha or not base_matches(after, self.expected_base):
                done.error = ForgectlError(
                    "approved_stale_head",
                    "the approval was sent but the head or base is no longer the expected one",
                    details={"ids": self._audit_ids(done)},
                )

    # -- GitLab: drafts, one publish, the read-back, the state step ------------------------------

    def _sent(self) -> list[tuple[str, dict[str, Any] | None]]:
        """What the drafts are, in the order they are created: each inline comment with its
        position, then the review body."""
        assert self.prep is not None
        sent: list[tuple[str, dict[str, Any] | None]] = [
            (c.body, self._gl_positions[c.raw.index]) for c in self.prep.comments
        ]
        if self.prep.body is not None:
            sent.append((self.prep.body, None))
        return sent

    def _abandon(
        self,
        pr: PR,
        own: list[int],
        why: ForgectlError | None,
        sent: list[Any],
        *,
        report_strangers: bool = True,
        unproven: bool = False,
    ) -> ForgectlError:
        """Nothing was published: delete forgectl's own drafts (the ones whose creation was proven
        and any listed one with exactly the text and position sent that was not there before) and
        return the error to raise. ``unproven`` says a creation may have landed unseen (a timeout, a
        5xx or an answer with no usable id: ``outcome_unknown``), so a failed re-list leaves drafts
        of unknown ids behind; a definitive 4xx or a wrong answer is not unproven.

        The host's own answer for a 401, 403, 404 or 429 is kept (its code, details and
        ``retry_after``); any other failure is ``host_rejected``. Either way the message names the
        drafts left behind."""
        candidates = set(own)
        strangers: list[int] = []
        relisted = True
        try:
            current = self.drafts.list_drafts(pr)
        except ForgectlError:
            current = []
            relisted = False
        for draft in current:
            if draft.id in self._first_drafts or draft.id in candidates:
                continue
            if any(matches_sent(draft, self._account, n, p) for n, p in sent):
                candidates.add(draft.id)
            else:
                strangers.append(draft.id)
        left = sorted(strangers) if report_strangers else []
        for ident in sorted(candidates):
            try:
                self.drafts.delete_draft(pr, ident)
            except ForgectlError:
                left.append(ident)
        unknown = unproven and not relisted
        kept = why is not None and why.code in _HOST_ANSWERS
        if kept:
            assert why is not None
            text = f"{why.message} (the review's draft notes were not published)"
            leftover = _leftover_text(left, unknown)
            return ForgectlError(
                why.code,
                text + leftover,
                details=why.details,
                retry_after=why.retry_after,
            )
        status: int | None = None
        if why is not None and isinstance(why.details, dict):
            raw = why.details.get("status")
            status = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
        text = f": {why.message}" if why is not None else ""
        error = host_rejected(
            f"the review's draft notes were not published{text}",
            status=status,
            leftover=left,
        )
        if unknown:
            error = ForgectlError(
                error.code,
                error.message + _leftover_text([], True),
                details=error.details,
            )
        return error

    def _post_drafts(self, pr: PR) -> tuple[list[Found | None], list[Found], list[str], bool]:
        """Steps 9 and 11 for the text: create the drafts, re-list, publish, read back. Returns
        the notes found per sent body, the found ones, the warnings, and whether the read-back was
        a must (the publish was not answered)."""
        sent = self._sent()
        own: list[int] = []
        try:
            for note, position in sent:
                own.append(self.drafts.create_draft(pr, note, position))
        except ForgectlError as err:
            raise self._abandon(
                pr, own, err, sent, unproven=err.code == "outcome_unknown"
            ) from None
        if len(set(own)) != len(own):
            twice = ForgectlError(
                "wrong_host_response",
                "draft_notes: two creations answered with the same id",
                details={"observed": "a repeated draft id"},
            )
            raise self._abandon(pr, sorted(set(own)), twice, sent)
        try:
            listed = {d.id for d in self.drafts.list_drafts(pr)}
            above = self.drafts.highest_note_id(pr)
        except ForgectlError as err:
            raise self._abandon(pr, own, err, sent) from None
        strangers = sorted(listed - set(own))
        if strangers or not set(own) <= listed:
            gone = self._abandon(pr, own, None, sent, report_strangers=not strangers)
            if strangers and not (gone.details or {}).get("leftover_drafts"):
                raise refused(
                    "drafts_present",
                    f"{len(strangers)} draft note(s) of this account appeared during the review; "
                    "forgectl's own were deleted and nothing was published",
                    details={"count": len(strangers)},
                )
            raise gone
        strict = False
        try:
            self.drafts.bulk_publish(pr)
        except ForgectlError as err:  # timeout, 5xx or a 4xx: the re-list tells which
            try:
                remaining = {d.id for d in self.drafts.list_drafts(pr)}
            except ForgectlError:
                raise ForgectlError(
                    "outcome_unknown",
                    "the publish was not answered and the drafts could not be listed; "
                    "the review may have landed",
                ) from None
            still = set(own) & remaining
            if still == set(own):
                raise self._abandon(pr, sorted(still), err, sent) from None
            # some or all of own are gone: the review landed at least in part. Delete what is
            # left of it, then the read-back decides.
            stuck = []
            for ident in sorted(still):
                try:
                    self.drafts.delete_draft(pr, ident)
                except ForgectlError:
                    stuck.append(ident)
            if stuck:
                ids = ", ".join(str(i) for i in stuck)
                raise ForgectlError(
                    "outcome_unknown",
                    "the publish was not answered and only part of the review's drafts are gone; "
                    f"drafts left on the merge request that could not be deleted: {ids}; "
                    "part of the review may have landed",
                ) from None
            strict = True
        bodies = [n for n, _ in sent]
        try:
            per_body, found = self.drafts.read_back(
                pr, above=above, account_id=self._account, bodies=bodies
            )
        except ForgectlError:
            if strict:
                raise ForgectlError(
                    "outcome_unknown",
                    "the publish was not answered and the read-back failed; "
                    "the review may have landed",
                ) from None
            return [], [], ["read_back_failed"], False
        return per_body, found, [], strict

    def _send_gitlab(self, pr: PR) -> Outcome:
        prep = self.prep
        assert prep is not None
        done = Outcome("approved" if prep.event == "approve" else "posted")
        if self.action == "approve" and prep.has_text and not self.hits:
            noted = self._post_note(pr, done)
            if noted is not None:
                return noted
        elif prep.has_text and not self.hits:
            per_body, found, warnings, after_publish_error = self._post_drafts(pr)
            done.landed = True
            done.warnings.extend(warnings)
            if not warnings:
                done.ids = [f"gl:n:{f.note_id}" for f in found]
                threads: list[str] = []
                for f in found:
                    if f"gl:d:{f.discussion}" not in threads:
                        threads.append(f"gl:d:{f.discussion}")
                done.thread_ids = threads
                if self._attempt_record is not None:  # a signal from here on keeps the ids
                    self._attempt_record.unknown_ids = list(done.ids)
                if any(f is None for f in per_body):  # a sent body that did not come back intact
                    done.result = "quick_action_ran"
                    if after_publish_error:
                        message = (
                            "the publish was not answered; "
                            f"notes {', '.join(done.ids)} landed and a sent body did not come "
                            "back intact (the host may have run a quick action from it, or "
                            "forgectl deleted drafts that were not published); "
                            "the state step was not run"
                        )
                    else:
                        message = (
                            "a published body does not match the one sent: the host may have "
                            "run a quick action from it; the state step was not run"
                        )
                    done.error = ForgectlError(
                        "quick_action_ran", message, details={"ids": list(done.ids)}
                    )
                    return done
        if not self.state.needed:
            return done
        try:
            if prep.event == "approve":
                self.drafts.approve(pr, pr.head.sha)
            else:
                self.drafts.request_changes(pr)
        except ForgectlError as err:
            # a 401 with the approval in place: it is there, whoever put it (Post order step 10)
            seen = (
                prep.event == "approve" and err.code == "auth_required" and self._approved_now(pr)
            )
            if not seen:
                if err.code == "stale_head" and not done.landed:
                    raise self._stale_head(err) from None
                if done.landed:  # text landed: any failure of the state step is a partial outcome
                    done.result = "outcome_partial"
                    done.error = ForgectlError(
                        "outcome_partial",
                        f"the review's text landed but the {prep.event} step failed: {err.message}",
                        details={
                            "landed": {"ids": list(done.ids), "thread_ids": list(done.thread_ids)},
                            "failed": prep.event,
                        },
                    )
                    return done
                raise
        done.landed = True
        done.follow_up_done = bool(self.hits)
        if prep.event == "approve":
            self._after_approval(pr, done)
        return done

    def _stale_head(self, err: ForgectlError) -> ForgectlError:
        """A 409 on ``POST /approve``: ``details.head_sha`` from one read, left out if it fails."""
        try:
            head = self.backend.get_pr(self.ref, NO_READINESS).pr.head.sha
        except ForgectlError:
            return err
        return refused("stale_head", err.message, details={"head_sha": head})

    def _approved_now(self, pr: PR) -> bool:
        """The re-read after a 401 on ``POST /approve``: ``user_has_approved``. A failed read is
        no."""
        try:
            return self.drafts.user_has_approved(pr)
        except ForgectlError:
            return False

    def _post_note(self, pr: PR, done: Outcome) -> Outcome | None:
        """``approve --body-file`` on GitLab: the note, through ``POST /notes`` with the backstop of
        ``comment``. Returns the outcome to stop with when the backstop tripped (the state step does
        not run), else None."""
        assert self.prep is not None and self.prep.body is not None
        posted = self.poster.post_comment(pr, self.prep.body)
        done.landed = True
        done.ids = list(posted.ids)
        done.thread_ids = list(posted.thread_ids)
        if self._attempt_record is not None:  # a signal from here on keeps the ids
            self._attempt_record.unknown_ids = list(done.ids)
        if posted.quick_action:
            done.result = "quick_action_ran"
            done.error = ForgectlError(
                "quick_action_ran",
                "the host may have run a quick action from the note's text; the approval was "
                "not sent",
                details={"ids": list(done.ids)},
            )
            return done
        return None

    # -- output --------------------------------------------------------------------------------

    def _out(
        self,
        *,
        outcome: Outcome | None = None,
        already: bool = False,
        hits: list[str] | None = None,
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if outcome is not None and outcome.landed:
            extra["review_id"] = None if outcome.review is None else outcome.review.review_id
            extra["ids"] = list(outcome.ids)
            extra["thread_ids"] = list(outcome.thread_ids)
            extra["warnings"] = list(outcome.warnings)
            extra["follow_up_done"] = outcome.follow_up_done
        if hits:
            extra["ids"] = list(hits)
            extra["review_id"] = next((h for h in hits if h.startswith("gh:r:")), None)
        if already:
            extra["already_posted"] = True
        if self.human.dry_run:
            extra["would_refuse"] = None
            extra["plan"] = self._plan_dict()
        return self._fields(**extra)


@registry.register("review")
def review(inv: registry.Invocation) -> dict[str, Any]:
    return ReviewRun(inv).run()


@registry.register("approve")
def approve(inv: registry.Invocation) -> dict[str, Any]:
    return ReviewRun(inv, "approve").run()
