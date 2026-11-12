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

TODO(#27): ``review`` on GitLab (drafts, ``bulk_publish``, the read-back by discussions, GraphQL
``mergeRequestRequestChanges``); a GitLab pull request fails as not implemented until then.
TODO(#28): the self-approval check, the already-approved rule with its retarget walk, and an exact
``head_verified``; here an approval at this head counts as already approved and ``head_verified`` is
false when one exists, because forgectl cannot tell it counts for the base.
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
from .errors import ForgectlError, SignalExit, refused, usage
from .humanactions import HumanContext, require_confirmation
from .identifiers import PrRef
from .merge import base_matches
from .model import PR, LatestByReviewer
from .neutralize import neutralize
from .post import GITHUB_COMMENT_LIMIT, INLINE_COMMENT_LIMIT, marker_line, read_input
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


def _too_large(limit: int, index: int | None) -> ForgectlError:
    return ForgectlError(
        "body_too_large",
        f"the {'review body' if index is None else f'inline comment {index}'} is larger than "
        f"{limit} bytes",
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


def prepare(raw: RawReview, key: str | None) -> Prepared:
    """Neutralize each body on its own, add the marker, and check each size (index null for the
    review body)."""
    if key is not None and raw.body is None and not raw.comments:
        raise usage("--dedupe-key needs a review body or inline comments to carry the marker")
    neutralized = markers = 0
    body: str | None = None
    if raw.body is not None:
        done = neutralize(raw.body)
        neutralized += done.neutralized_lines
        markers += done.markers_removed
        body = done.text
    if key is not None:  # the marker is the review body's last line, and alone with no body
        marker = marker_line(key)
        body = marker if body is None else body + ("" if body.endswith("\n") else "\n") + marker
    if body is not None and len(body.encode("utf-8")) > GITHUB_COMMENT_LIMIT:
        raise _too_large(GITHUB_COMMENT_LIMIT, None)
    comments: list[Comment] = []
    for item in raw.comments:
        done = neutralize(item.body)
        neutralized += done.neutralized_lines
        markers += done.markers_removed
        if len(done.text.encode("utf-8")) > INLINE_COMMENT_LIMIT:
            raise _too_large(INLINE_COMMENT_LIMIT, item.index)
        comments.append(Comment(item, done.text))
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


@dataclass
class Outcome:
    result: str  # an audit result: posted, approved, failed, outcome_unknown
    review: Submitted | None = None
    ids: list[str] = field(default_factory=list)
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


class ReviewRun:
    def __init__(self, inv: registry.Invocation) -> None:
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
        self.poster = GitHubPoster(self.env)
        self.reviewer = GitHubReviewer(self.env)
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
            "event": None if prep is None else prep.event,
            "body_sha256": None if prep is None else prep.sha256,
            "body_bytes": 0 if prep is None else prep.nbytes,
            "inline_comments": 0 if prep is None else len(prep.comments),
        }

    def _verified(self) -> bool | None:
        # TODO(#28): an exact answer needs the latest retarget; an approval at this head is
        # reported as one forgectl cannot tell counts for the base
        if self.prep is not None and self.prep.event == "approve" and self.state.approved_at_head:
            return False
        return None

    def _fields(self, **extra: Any) -> dict[str, Any]:
        prep = self.prep
        out: dict[str, Any] = {
            "pr": self.pr_id,
            "action": "review",
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
        raw = read_input(self.args.file, FILE_LIMIT + 1)
        if len(raw) > FILE_LIMIT:
            raise _too_large(FILE_LIMIT, None)
        return prepare(parse_input(raw), self.args.dedupe_key)

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
            raise usage("review needs --expected-head and --expected-base unless interactive")
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
        # TODO(#28): self_approval for an approving event
        mine = _mine(read.latest_by_reviewer, self._account, self._login)
        event = self.prep.event
        approved = mine is not None and mine.state == "approved" and mine.commit_sha == pr.head.sha
        self.state = State(
            needed=(event == "approve" and not approved)
            or (event == "request_changes" and (mine is None or mine.state != "changes_requested")),
            approved_at_head=approved,
        )
        return pr

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
        checked = positions.check_github(self.env, self.ref, asked)
        if checked.head_sha != pr.head.sha:
            raise refused(
                "stale_head",
                "the head moved while the positions were checked",
                details={"head_sha": checked.head_sha},
            )
        self._checked_head = checked.head_sha
        self._placements = checked.placements

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
        if hits and self.state.needed:
            raise ForgectlError(
                "outcome_partial",
                f"the review's text is already posted but the {prep.event} state is not in "
                "effect (dismissed, no longer counting for this head, or the key was reused); "
                "nothing was sent",
                details={"landed": {"ids": hits, "thread_ids": []}, "failed": prep.event},
            )
        if hits or (not prep.has_text and not self.state.needed):
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
            if not prep.has_text and not self.state.needed:
                return self._out(already=True, hits=[])
        return self._write(pr)

    # -- confirmation and hooks ----------------------------------------------------------------

    def _dialog(self, pr: PR) -> bool:
        assert self.prep is not None
        lines = [
            f"review on {self.pr_id}",
            f"  head:    {pr.head.sha}",
            f"  event:   {self.prep.event}",
            f"  inline:  {len(self.prep.comments)} comment(s)",
        ]
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
            "action": "review",
            "event": self.prep.event,
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
            event=prep.event,
            body_sha256=prep.sha256,
            host_login=self._login,
        )
        context = audit.AuditContext(
            actor=self.human.actor,
            decision_ids=self.decision_ids,
            target=self.pr_id,
            action="review",
            inputs=inputs,
            override=overrides,
        )
        if self.env.gateway.budget.remaining() <= 0:  # nothing was sent: no `attempted` record
            raise ForgectlError("network_error", "the time budget ran out before the review")
        audit_error: ForgectlError | None = None
        signalled = False
        with audit.Attempt(self.directory, context) as attempt:
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
        assert outcome.review is not None
        return self._out(outcome=outcome)

    def _attempt(self, pr: PR) -> Outcome:
        """The write and its read-back. A bug here (any non-forgectl exception) is an unknown
        outcome: the write may have landed, so the audit result and the post- hooks still follow.
        A signal is not caught."""
        assert self.prep is not None
        try:
            return self._send(pr)
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
        done = Outcome("approved" if prep.event == "approve" else "posted", review)
        # step 11: the read-back; a read that fails is a warning, the review has landed
        try:
            ids = self.reviewer.read_comments(pr, review)
            if len(ids) != len(comments):
                raise ForgectlError("upstream_error", "the read-back does not match what was sent")
            done.ids = ids
        except ForgectlError:
            done.warnings.append("read_back_failed")
        if prep.event == "approve":  # after an approval this run sent: is it still this head?
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
        return done

    # -- output --------------------------------------------------------------------------------

    def _out(
        self,
        *,
        outcome: Outcome | None = None,
        already: bool = False,
        hits: list[str] | None = None,
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if outcome is not None and outcome.review is not None:
            extra["review_id"] = outcome.review.review_id
            extra["ids"] = list(outcome.ids)
            extra["warnings"] = list(outcome.warnings)
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
    ref = inv.args.pr
    if isinstance(ref, PrRef) and ref.backend != "github":
        raise ForgectlError("internal", "review on GitLab: not implemented yet (#27)")
    return ReviewRun(inv).run()
