"""``resolve`` and ``unresolve``: the post order of interface.md "Post order" for a review thread.

The same pipeline as ``comment`` and ``reply`` (``post.py``: the audit attempt, the hooks, the
signal rules), with no body and no dedupe: 2. the audit file, the account, the PR, then the thread
within it and its state, ``already there`` (exit 0, before confirmation, hooks and audit),
6. confirmation (``--dry-run`` stops here), 7. ``pre-post`` hooks, 8. a re-read after a hook or a
dialog, 9. audit ``attempted`` and the one write, 12. the audit result and ``post-post`` hooks.

``--thread`` takes ``gh:rt:`` and ``gl:d:``. Any other well-formed id is exit 20
(``not_resolvable``) before a host is touched; a thread that is not resolvable (GitLab: an
individual note, or a discussion of system notes) is the same refusal once it is read. The
output's ``changed`` is true only when this run wrote: a thread already in the state, at the first
read or at the re-read, is false.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from . import audit, hooks, jsonlog, output, registry
from .backends.post_base import ThreadPoster, ThreadState
from .errors import ForgectlError, refused
from .humanactions import HumanContext, require_confirmation
from .identifiers import PrRef, TypedId
from .model import PR
from .post import (
    Outcome,
    attempt_write,
    audited_write,
    fire_post_hooks,
    fire_pre_hooks,
)
from .reads import NO_READINESS, ReadEnv, backend_for, make_env
from .sanitize import sanitize_line

RESOLVABLE_KINDS = frozenset({"rt", "d"})


def _threads(backend: str, env: ReadEnv) -> ThreadPoster:
    if backend == "github":
        from .backends.github_resolve import GitHubThreads

        return GitHubThreads(env)
    from .backends.gitlab_resolve import GitLabThreads

    return GitLabThreads(env)


class ResolveRun:
    """One ``resolve`` or ``unresolve``."""

    def __init__(self, inv: registry.Invocation) -> None:
        if inv.human is None:  # cli gates every write command and passes its context
            raise ForgectlError("internal", f"{inv.command}: no human-action context")
        self.inv = inv
        self.args = inv.args
        self.action: str = inv.command
        self.want: bool = inv.command == "resolve"
        self.human: HumanContext = inv.human
        ref = inv.args.pr
        if not isinstance(ref, PrRef):
            raise ForgectlError("internal", f"{inv.command}: the pull request was not bound")
        self.ref: PrRef = ref
        self.pr_id: str = str(ref)
        thread = inv.args.thread
        if not isinstance(thread, TypedId):
            raise ForgectlError("internal", f"{inv.command}: the thread id was not parsed")
        self.thread: TypedId = thread
        self.env: ReadEnv = make_env(inv)
        self.backend = backend_for(ref, self.env)
        self.poster: ThreadPoster = _threads(ref.backend, self.env)
        try:
            self.directory: Path = jsonlog.state_dir(os.environ)
        except jsonlog.JsonlError as err:
            raise ForgectlError(
                "audit_unavailable", f"audit.jsonl cannot be appended to: {err}"
            ) from None
        self.decision_ids: list[str] = list(self.human.decision_ids)
        self.escalated = False
        self.head_sha: str | None = None
        self._login = ""
        self._account = ""

    # -- helpers -------------------------------------------------------------------------------

    def _log(self, level: str, event: str, details: dict[str, Any]) -> None:
        text = jsonlog.log_event(
            self.directory, level, event, pr=self.pr_id, host=self.ref.host, details=details
        )
        if text:
            output.warn(text)

    def run(self) -> dict[str, Any]:
        try:
            return self._run()
        except ForgectlError as err:
            if self.human.dry_run and err.exit_code == 20:
                err.fields = {**(err.fields or {}), "pr": self.pr_id}
            raise

    def _read(self) -> tuple[PR, ThreadState]:
        """Steps 2 and 8: the PR, then the thread within it."""
        pr = self.backend.get_pr(self.ref, NO_READINESS).pr
        self.pr_id = f"{pr.repo}#{pr.number}"
        self.head_sha = pr.head.sha
        self.poster.prepare(pr)  # nothing the write needs is read inside the audited window
        state = self.env.call(
            lambda: self.poster.read_thread(pr, self.thread, self._login, self._account)
        )
        if not state.resolvable:
            raise refused(
                "not_resolvable",
                "the thread is not resolvable",
            )
        return pr, state

    def _run(self) -> dict[str, Any]:
        if self.thread.kind not in RESOLVABLE_KINDS:
            raise refused(
                "not_resolvable",
                f"only review threads (gh:rt: and gl:d: ids) can be {self.action}d",
            )
        audit.check_writable(self.directory)
        target = self.env.hosts.target(self.ref.host)
        user = self.env.call(lambda: self.env.hosts.authenticated_user(target))
        self._login, self._account = user.login, str(user.id)
        pr, state = self._read()
        if state.resolved is self.want:  # already there: no confirmation, hooks or audit
            return self._out(changed=False)
        dialog = require_confirmation(self.human)
        if self.human.dry_run:
            return self._out(changed=False)
        if dialog and not self._dialog(pr):
            raise ForgectlError("confirmation_required", "declined")
        pre = fire_pre_hooks(
            self.env,
            self._hook_fields(pr, state),
            self.decision_ids,
            bool(self.args.override_escalation),
        )
        self.decision_ids = list(pre.decision_ids)
        self.escalated = bool(pre.escalated)
        # step 8: a slow gate may have outlasted what it saw
        if dialog or hooks.entries(self.inv.config.config, "pre-post"):
            pr, state = self._read()
            if state.resolved is self.want:  # someone else got there first: nothing to write
                return self._out(changed=False)
        return self._write(pr, state)

    # -- confirmation and hooks ----------------------------------------------------------------

    def _dialog(self, pr: PR) -> bool:
        lines = [
            f"{self.action} on {self.pr_id}",
            f"  head:    {pr.head.sha}",
            f"  thread:  {self.thread}",
        ]
        sys.stderr.write("\n".join(sanitize_line(line) for line in lines) + "\n")
        sys.stderr.write("Proceed? [y/N] ")
        sys.stderr.flush()
        return sys.stdin.readline().strip().lower() in ("y", "yes")

    def _hook_fields(self, pr: PR, state: ThreadState) -> dict[str, Any]:
        return {
            "object_id": self.pr_id,
            "repo": pr.repo,
            "head_sha": pr.head.sha,
            "actor": self.human.actor,
            "action": self.action,
            "event": None,
            "body_sha256": None,
            "body_bytes": 0,
            "thread_id": str(self.thread),
            "thread_author_is_self": state.author_is_self,
            "reply_to": None,
            "override_escalation": bool(self.args.override_escalation),
            "allow_self": False,
        }

    def _post_hooks(self, pr: PR, state: ThreadState, outcome: Outcome) -> None:
        fields = self._hook_fields(pr, state)
        del fields["override_escalation"], fields["allow_self"]
        fields["result"] = outcome.result
        fields["ids"] = []
        fire_post_hooks(self.env, fields, self.decision_ids, self.directory, self.pr_id, self._log)

    # -- steps 9 and 12 ------------------------------------------------------------------------

    def _write(self, pr: PR, state: ThreadState) -> dict[str, Any]:
        overrides = ["override_escalation"] if self.escalated else []
        inputs = audit.AuditInputs(
            config_hash=self.inv.config.config_hash,
            config_path=self.inv.config.config_path,
            token_source=self.env.hosts.credential(self.ref.host).source,
            stdin_tty=self.human.stdin_tty,
            actor_source=self.human.actor_source,
            head_sha=pr.head.sha,
            base_ref=pr.base.ref,
            thread_id=str(self.thread),
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
            raise ForgectlError("network_error", "the time budget ran out before the write")

        def write() -> Outcome:
            def call() -> Outcome:
                self.poster.set_resolved(pr, self.thread, self.want)
                return Outcome("resolved" if self.want else "unresolved")

            return attempt_write(call, self._log)

        outcome = audited_write(
            self.directory, context, write, lambda done: self._post_hooks(pr, state, done)
        )
        if outcome.error is not None:
            raise outcome.error
        return self._out(changed=True)

    # -- output --------------------------------------------------------------------------------

    def _out(self, *, changed: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pr": self.pr_id,
            "action": self.action,
            "thread_id": str(self.thread),
            "resolved": self.want,
            "changed": changed,
            "dry_run": self.human.dry_run,
        }
        if self.human.dry_run:
            out["would_refuse"] = None
        return out


@registry.register("resolve")
def resolve(inv: registry.Invocation) -> dict[str, Any]:
    return ResolveRun(inv).run()


@registry.register("unresolve")
def unresolve(inv: registry.Invocation) -> dict[str, Any]:
    return ResolveRun(inv).run()
