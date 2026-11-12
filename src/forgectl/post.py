"""``comment`` and ``reply``: the post order of interface.md "Post order", for every backend.

The order is host-neutral and lives here; a ``Poster`` (``backends/github_post.py``,
``backends/gitlab_post.py``) supplies the lookup of ``--to``, the dedupe search and the one write:

1. usage: open and read the body, neutralize it, add the dedupe marker, check the size, 2. the
audit file can be appended, the account is read, then the PR, then ``--to`` is looked up within it,
4. dedupe (a hit is exit 0 before confirmation and hooks), 6. confirmation (``--dry-run`` stops
here), 7. ``pre-post`` hooks, 8. a re-read after a hook or a dialog, 9. audit ``attempted`` and the
one write, 11. the backstop (inside the poster), 12. the audit result, ``post-post`` hooks, output.

Nothing believes an answer that lacks its documented status and proof. The write is never
retried. After ``Attempt.begin()`` every path records exactly one outcome, then runs the
``post-post`` hooks and exits; a signal before the outcome is recorded records ``outcome_unknown``
and exits 12 (``cli.py``) without hooks, and one after it stops the remaining hooks.

TODO(#25): position validation (step 3) for ``review`` comments.
TODO(#26), TODO(#27): ``review`` on GitHub and GitLab: events, drafts, the read-back (steps 5, 9
to 11) and the review body's own limits; they reuse ``prepare_body`` and ``PostRun``'s hooks.
TODO(#28): ``approve`` (self-approval, the state step, ``head_verified``).
TODO(#29): ``resolve`` and ``unresolve``.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import audit, hooks, jsonlog, output, registry
from .backends.post_base import Anchor, Posted, Poster
from .errors import ForgectlError, SignalExit, usage
from .humanactions import HumanContext, require_confirmation
from .identifiers import PrRef, TypedId
from .model import PR
from .neutralize import neutralize
from .reads import NO_READINESS, ReadEnv, backend_for, make_env
from .sanitize import sanitize_line

#: Interface, Post order step 1: GitHub's review body and issue comment; GitLab's every body.
GITHUB_COMMENT_LIMIT = 262144
INLINE_COMMENT_LIMIT = 1024 * 1024
GITLAB_LIMIT = 1024 * 1024
_READ_CHUNK = 65536


def limit_for(backend: str, to: TypedId | None = None) -> int:
    """The body limit. A GitHub reply to a review thread or review comment (``gh:rt:`` or
    ``gh:rc:``) becomes an inline review comment, which takes 1 MiB; the id says so before any
    host is read, and a reply to anything else is a general comment."""
    if backend != "github":
        return GITLAB_LIMIT
    if to is not None and to.host_prefix == "gh" and to.kind in ("rt", "rc"):
        return INLINE_COMMENT_LIMIT
    return GITHUB_COMMENT_LIMIT


# -- the input -----------------------------------------------------------------------------------


def _too_large(limit: int) -> ForgectlError:
    return ForgectlError(
        "body_too_large",
        f"the body is larger than {limit} bytes",
        details={"index": None, "limit": limit},
    )


def _read_fd(fd: int, cap: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < cap:
        data = os.read(fd, min(_READ_CHUNK, cap - total))
        if not data:
            break
        chunks.append(data)
        total += len(data)
    return b"".join(chunks)


def read_input(source: str, cap: int) -> bytes:
    """At most ``cap`` bytes of a path or of ``-``, the inherited stdin read to EOF.

    A path is opened with ``O_NOFOLLOW`` and ``O_NONBLOCK`` and must be a regular file by
    ``fstat``, so a FIFO fails the check instead of hanging, and a final symlink is refused. The
    checks are for paths only: ``-`` is a pipe (or a file) the caller already holds open, and a
    blocking read to EOF is what it needs. Parent components are not checked: a caller passing a
    path under a directory someone else can write must freeze the file first."""
    if source == "-":
        stream = sys.stdin
        if stream is None:  # closed standard input
            raise usage("standard input cannot be read: it is closed")
        buffer = getattr(stream, "buffer", None)
        try:
            data: Any = buffer.read(cap) if buffer is not None else stream.read(cap)
        except (OSError, ValueError, TypeError) as err:
            raise usage(f"standard input cannot be read: {type(err).__name__}") from None
        if data is None:  # a non-blocking stream with nothing to give
            raise usage("standard input cannot be read: it has no data to give yet")
        if isinstance(data, bytes):
            return data
        return str(data).encode("utf-8", errors="surrogatepass")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(source, flags)
    except OSError as err:
        raise usage(
            f"the body file cannot be opened ({err.strerror or type(err).__name__})"
        ) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise usage("the body file is not a regular file")
        return _read_fd(fd, cap)
    except OSError as err:
        raise usage(
            f"the body file cannot be read ({err.strerror or type(err).__name__})"
        ) from None
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Body:
    """A body as it will be posted."""

    text: str
    sha256: str
    nbytes: int
    neutralized_lines: int
    markers_removed: int


def marker_line(key: str) -> str:
    return f"<!-- forgectl:dedupe:{key} -->"


def prepare_body(source: str, *, limit: int, key: str | None) -> Body:
    """Post order step 1 for one body: read at most limit + 1 bytes, strict UTF-8, an empty body
    is a usage error, neutralize, add the marker as the last line, check the size."""
    raw = read_input(source, limit + 1)
    if len(raw) > limit:
        raise _too_large(limit)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise usage("the body is not valid UTF-8") from None
    if not text.strip():
        raise usage("the body is empty")
    done = neutralize(text)
    posted = done.text
    if key is not None:
        posted += ("" if posted.endswith("\n") else "\n") + marker_line(key)
    data = posted.encode("utf-8")
    if len(data) > limit:
        raise _too_large(limit)
    return Body(
        posted,
        hashlib.sha256(data).hexdigest(),
        len(data),
        done.neutralized_lines,
        done.markers_removed,
    )


# -- the command ---------------------------------------------------------------------------------


@dataclass
class Outcome:
    result: str  # an audit result: posted, quick_action_ran, failed, outcome_unknown
    posted: Posted | None = None
    error: ForgectlError | None = None


def _poster(backend: str, env: ReadEnv) -> Poster:
    if backend == "github":
        from .backends.github_post import GitHubPoster

        return GitHubPoster(env)
    from .backends.gitlab_post import GitLabPoster

    return GitLabPoster(env)


class PostRun:
    """One ``comment`` or ``reply``."""

    def __init__(self, inv: registry.Invocation) -> None:
        if inv.human is None:  # cli gates every write command and passes its context
            raise ForgectlError("internal", f"{inv.command}: no human-action context")
        self.inv = inv
        self.args = inv.args
        self.action: str = inv.command
        self.human: HumanContext = inv.human
        ref = inv.args.pr
        if not isinstance(ref, PrRef):
            raise ForgectlError("internal", f"{inv.command}: the pull request was not bound")
        self.ref: PrRef = ref
        self.pr_id: str = str(ref)
        self.to: TypedId | None = getattr(inv.args, "to", None)
        self.env: ReadEnv = make_env(inv)
        self.backend = backend_for(ref, self.env)
        self.poster: Poster = _poster(ref.backend, self.env)
        try:
            self.directory: Path = jsonlog.state_dir(os.environ)
        except jsonlog.JsonlError as err:
            raise ForgectlError(
                "audit_unavailable", f"audit.jsonl cannot be appended to: {err}"
            ) from None
        self.decision_ids: list[str] = list(self.human.decision_ids)
        self.escalated = False
        self.body: Body | None = None
        self.head_sha: str | None = None
        self._login: str | None = None
        self._plan: dict[str, Any] | None = None

    # -- helpers -------------------------------------------------------------------------------

    def _log(self, level: str, event: str, details: dict[str, Any]) -> None:
        text = jsonlog.log_event(
            self.directory, level, event, pr=self.pr_id, host=self.ref.host, details=details
        )
        if text:
            output.warn(text)

    def _plan_dict(self) -> dict[str, Any]:
        body = self.body
        return {
            "event": None,
            "body_sha256": None if body is None else body.sha256,
            "body_bytes": 0 if body is None else body.nbytes,
            "inline_comments": 0,
        }

    def _fields(self, **extra: Any) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pr": self.pr_id,
            "action": self.action,
            "ids": [],
            "thread_ids": [],
            "review_id": None,
            "already_posted": False,
            "follow_up_done": False,
            "neutralized_lines": 0 if self.body is None else self.body.neutralized_lines,
            "markers_removed": 0 if self.body is None else self.body.markers_removed,
            "warnings": [],
            "head_sha": self.head_sha,
            "dry_run": self.human.dry_run,
        }
        out.update(extra)
        if self.action == "reply" and "threaded" not in out:
            out["threaded"] = False
        return out

    # -- the whole run -------------------------------------------------------------------------

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
                    "neutralized_lines": 0 if self.body is None else self.body.neutralized_lines,
                    "markers_removed": 0 if self.body is None else self.body.markers_removed,
                }
            raise

    def _read(self) -> PR:
        pr = self.backend.get_pr(self.ref, NO_READINESS).pr
        self.pr_id = f"{pr.repo}#{pr.number}"
        self.head_sha = pr.head.sha
        return pr

    def _run(self) -> dict[str, Any]:
        args = self.args
        # step 1: usage. The body is read, neutralized and sized before any host is touched.
        self.body = prepare_body(
            args.body_file, limit=limit_for(self.ref.backend, self.to), key=args.dedupe_key
        )
        # step 2: the audit file, the account, the PR, then --to within it
        audit.check_writable(self.directory)
        target = self.env.hosts.target(self.ref.host)
        user = self.env.call(lambda: self.env.hosts.authenticated_user(target))
        self._login = user.login
        pr = self._read()
        self.poster.prepare(pr)  # nothing the write needs is read inside the audited window
        anchor = self._anchor(pr)
        # step 4: dedupe. A hit leaves no text to post and no state step: exit 0 here.
        if args.dedupe_key is not None:
            hits = self.env.call(
                lambda: self.poster.find_marker(pr, user.id, marker_line(args.dedupe_key))
            )
            if hits:
                return self._out(already=True, ids=hits, anchor=anchor)
        # step 6: confirmation. A dry run stops here; any other run without one is exit 13.
        dialog = require_confirmation(self.human)
        if self.human.dry_run:
            return self._out(anchor=anchor)
        if dialog and not self._dialog(pr, anchor):
            raise ForgectlError("confirmation_required", "declined")
        # step 7: pre-post hooks (a veto or escalation raises)
        pre = hooks.run_pre_hooks(
            self.env.gateway,
            "pre-post",
            self._hook_fields(pr, anchor),
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
            self.poster.prepare(pr)
            anchor = self._anchor(pr)
        return self._write(pr, anchor, user.id)

    def _anchor(self, pr: PR) -> Anchor | None:
        if self.to is None:
            return None
        return self.poster.find_anchor(pr, self.to)

    # -- confirmation and hooks ----------------------------------------------------------------

    def _dialog(self, pr: PR, anchor: Anchor | None) -> bool:
        assert self.body is not None
        lines = [f"{self.action} on {self.pr_id}", f"  head:    {pr.head.sha}"]
        if self.to is not None:
            lines.append(f"  to:      {self.to}")
            if anchor is not None and not anchor.threaded:
                lines.append("  note:    no thread to join: posts a new general comment")
        lines.append(f"  body:    {self.body.nbytes} bytes, sha256 {self.body.sha256}")
        if self.body.neutralized_lines:
            lines.append(f"  note:    {self.body.neutralized_lines} line(s) given a leading space")
        sys.stderr.write("\n".join(sanitize_line(line) for line in lines) + "\n")
        sys.stderr.write("Post? [y/N] ")
        sys.stderr.flush()
        return sys.stdin.readline().strip().lower() in ("y", "yes")

    def _hook_fields(self, pr: PR, anchor: Anchor | None) -> dict[str, Any]:
        assert self.body is not None
        return {
            "object_id": self.pr_id,
            "repo": pr.repo,
            "head_sha": pr.head.sha,
            "actor": self.human.actor,
            "action": self.action,
            "event": None,
            "body_sha256": self.body.sha256,
            "body_bytes": self.body.nbytes,
            "thread_id": None,
            "thread_author_is_self": None,
            "reply_to": None if self.to is None else str(self.to),
            "override_escalation": bool(self.args.override_escalation),
            "allow_self": False,
        }

    def _post_hooks(self, pr: PR, anchor: Anchor | None, outcome: Outcome) -> None:
        fields = self._hook_fields(pr, anchor)
        del fields["override_escalation"], fields["allow_self"]
        fields["result"] = outcome.result
        fields["ids"] = list(outcome.posted.ids) if outcome.posted else []
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

    def _write(self, pr: PR, anchor: Anchor | None, account_id: str) -> dict[str, Any]:
        assert self.body is not None
        overrides = ["override_escalation"] if self.escalated else []
        inputs = audit.AuditInputs(
            config_hash=self.inv.config.config_hash,
            config_path=self.inv.config.config_path,
            token_source=self.env.hosts.credential(self.ref.host).source,
            stdin_tty=self.human.stdin_tty,
            actor_source=self.human.actor_source,
            head_sha=pr.head.sha,
            base_ref=pr.base.ref,
            event=None,
            body_sha256=self.body.sha256,
            reply_to=None if self.to is None else str(self.to),
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
            raise ForgectlError("network_error", "the time budget ran out before the post")
        audit_error: ForgectlError | None = None
        signalled = False
        with audit.Attempt(self.directory, context) as attempt:
            attempt.begin()  # a failure here is audit_unavailable and nothing has been sent
            outcome = self._attempt(pr, anchor)
            ids = list(outcome.posted.ids) if outcome.posted else []
            try:
                attempt.finish(outcome.result, ids=ids)
            except ForgectlError as err:  # audit_failed: the post- hooks still run
                audit_error = err
            except SignalExit:
                if not attempt.finished:
                    raise  # the outcome is not on record: Attempt records outcome_unknown
                signalled = True
                audit_error = attempt.finish_error
        if not signalled:
            try:
                self._post_hooks(pr, anchor, outcome)
            except SignalExit:
                pass  # the running hook's group was ended; the remaining hooks do not run
        if audit_error is not None:
            raise audit_error
        if outcome.error is not None:
            raise outcome.error
        assert outcome.posted is not None
        return self._out(posted=outcome.posted, anchor=anchor)

    def _attempt(self, pr: PR, anchor: Anchor | None) -> Outcome:
        """The write. A bug here (any non-forgectl exception) is an unknown outcome: the write
        may have landed, so the audit result and the post- hooks still follow. A signal is not
        caught."""
        assert self.body is not None
        try:
            if anchor is not None:
                posted = self.poster.post_reply(pr, anchor, self.body.text)
            else:
                posted = self.poster.post_comment(pr, self.body.text)
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
                    f"internal error after the post was sent ({type(exc).__name__}); "
                    "it may have landed",
                ),
            )
        if posted.quick_action:
            return Outcome(
                "quick_action_ran",
                posted,
                ForgectlError(
                    "quick_action_ran",
                    "the host may have run a quick action from the body",
                    details={"ids": list(posted.ids)},
                ),
            )
        return Outcome("posted", posted)

    # -- output --------------------------------------------------------------------------------

    def _out(
        self,
        *,
        posted: Posted | None = None,
        already: bool = False,
        ids: list[str] | None = None,
        anchor: Anchor | None = None,
    ) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if posted is not None:
            extra["ids"] = list(posted.ids)
            extra["thread_ids"] = list(posted.thread_ids)
        if ids is not None:
            extra["ids"] = list(ids)
        if already:
            extra["already_posted"] = True
        if self.action == "reply":
            extra["threaded"] = bool(anchor and anchor.threaded)
        if self.human.dry_run:
            extra["would_refuse"] = None
            extra["plan"] = self._plan_dict()
        return self._fields(**extra)


@registry.register("comment")
def comment(inv: registry.Invocation) -> dict[str, Any]:
    return PostRun(inv).run()


@registry.register("reply")
def reply(inv: registry.Invocation) -> dict[str, Any]:
    return PostRun(inv).run()
