"""``merge``: the fixed order of interface.md "Merge order", for every backend.

The sequence is host-neutral and lives here; a backend supplies only the read (``get_pr``) and the
one write (``merge``), through ``backends/``:

1. usage (the human-action gate ran before this handler), 3. the audit file can be appended, then
the PR is read, 4. the checks in the table's order, 5. confirmation, 6. ``pre-merge`` hooks, 7. a
re-read after a hook or a dialog, 8. audit ``attempted`` and the one write carrying the expected
head as ``sha``, 9. verification by reading, 10. the audit result, ``post-merge`` hooks, output.

Nothing here believes an answer to the write: a 2xx is never proof, and every outcome is decided
by what a later read shows. The write is never retried. After ``Attempt.begin()`` every path
records exactly one outcome, then runs the ``post-merge`` hooks and exits. A signal before the
outcome is recorded records ``outcome_unknown`` and exits 12 (``cli.py``), without hooks; a signal
after it stops the remaining hooks and the run ends with the recorded outcome. Never 128+N.

TODO(#23): ``--auto`` (the GitHub ``enablePullRequestAutoMerge`` mutation, GitLab's flags, the
``already armed`` row, the ``--auto`` rows of the checks and the head-match availability step).
TODO(#22): the GitLab backend's ``merge`` (the method table, ``squash`` only after the checks).
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import audit, hooks, jsonlog, output, registry
from .backends.base import Backend
from .errors import ForgectlError, SignalExit, refused, usage
from .humanactions import HumanContext, require_confirmation
from .identifiers import PrRef
from .model import (
    MERGE_METHODS,
    PR,
    Blocker,
    BlockerCode,
    MergeAnswer,
    PrRead,
    Readiness,
    sorted_blockers,
)
from .reads import MERGE_READINESS, NO_READINESS, ReadEnv, backend_for, make_env
from .sanitize import sanitize_line

#: The seconds slept before each verification read after the first (interface: 1 s, 2 s, 3 s,
#: 4 s: up to 10 s). Tests replace ``_sleep``.
VERIFY_DELAYS = (1.0, 2.0, 3.0, 4.0)
_MARKER = re.compile(r"<U\+[0-9A-F]{4,6}>")
_sleep = time.sleep


def _pause(seconds: float) -> None:
    _sleep(seconds)


# -- what a run decided --------------------------------------------------------------------------


@dataclass
class Outcome:
    """How the write ended, for the audit record, the hooks and the answer."""

    result: str  # an audit result: merged, merged_unexpected, failed, outcome_unknown
    pr: PR | None = None  # the PR as the deciding read showed it
    already: bool = False  # merged by someone else at the expected head and base (a 405 re-read)
    error: ForgectlError | None = None
    observed: dict[str, str] | None = None  # merged_unexpected: the head and base it merged at


@dataclass(frozen=True)
class Verdict:
    """What the checks of step 4 decided."""

    already_merged: bool
    method: str | None
    blockers: tuple[Blocker, ...]  # what blocked a merge that --override-blocks overrides


def _dicts(blockers: tuple[Blocker, ...]) -> list[dict[str, str]]:
    return [b.to_dict() for b in sorted_blockers(blockers)]


def _ci_blockers(read: PrRead) -> tuple[Blocker, ...]:
    """What a non-human actor adds when the CI is not ``passing``: ``none`` is no evidence."""
    code = {
        "passing": None,
        "pending": BlockerCode.CHECKS_PENDING,
        "failing": BlockerCode.CHECKS_FAILING,
        "none": BlockerCode.CHECKS_ABSENT,
    }[read.ci.state]
    return () if code is None else (Blocker(code),)


def base_matches(pr: PR, expected_base: str) -> bool:
    """The PR's base against the expected one. A base whose text was marked by the sanitizer
    (``<U+XXXX>``) never matches: the caller cannot have named it, and a caller who writes the
    marker text must not be taken for naming the character (interface: Human actions)."""
    return pr.base.ref == expected_base and _MARKER.search(pr.base.ref) is None


def _allowed_list(pr: PR) -> list[str]:
    allowed = pr.merge_methods_allowed or ()
    return [x for x in MERGE_METHODS if x in allowed]


def _still_allowed(pr: PR, method: str) -> None:
    """The re-read of step 7: the method shown in the dialog and sent to the hooks is the one
    written, so a repo that stopped allowing it refuses rather than switching methods."""
    if pr.merge_methods_allowed is not None and method not in pr.merge_methods_allowed:
        raise refused(
            "method_not_allowed",
            f"the repository no longer allows {method} merges",
            details={"allowed": _allowed_list(pr)},
        )


def resolve_method(inv: registry.Invocation, pr: PR) -> str:
    """Merge method (interface: Merge method): ``--method``, else ``repos.<id>.merge_method``,
    else the only allowed one, else ``method_required``. A method the repo disallows is
    ``method_not_allowed``; with the allowed list unknown the host decides."""
    allowed = pr.merge_methods_allowed
    listed = _allowed_list(pr)
    chosen = inv.config.merge_method_for(pr.repo, flag=inv.args.method)
    if chosen is None:
        if allowed is not None and len(listed) == 1:
            return listed[0]
        raise ForgectlError(
            "method_required",
            "name a merge method with --method or repos.<id>.merge_method"
            + ("" if allowed is not None else " (the repository's allowed methods are unknown)"),
            details={"allowed": listed},
        )
    if allowed is not None and chosen not in allowed:
        raise refused(
            "method_not_allowed",
            f"the repository does not allow {chosen} merges",
            details={"allowed": listed},
        )
    return chosen


# -- the command ---------------------------------------------------------------------------------


class MergeRun:
    def __init__(self, inv: registry.Invocation) -> None:
        if inv.human is None:  # cli gates every write command and passes its context
            raise ForgectlError("internal", "merge: no human-action context")
        self.inv = inv
        self.args = inv.args
        self.human: HumanContext = inv.human
        ref = inv.args.pr
        if not isinstance(ref, PrRef):  # bind_args binds it before any handler runs
            raise ForgectlError("internal", "merge: the pull request was not bound to a host")
        self.ref: PrRef = ref
        #: What output, the audit target, hooks and the log name: the canonical path from the
        #: first read once there is one (interface: Identifiers), the normalized input before.
        self.pr_id: str = str(ref)
        self.expected_head: str | None = self.args.expected_head
        self.expected_base: str | None = self.args.expected_base
        self.env: ReadEnv = make_env(inv, sleep=_pause)
        self.backend: Backend = backend_for(ref, self.env)
        self.directory: Path = self._state_dir()
        self.escalated = False
        self.decision_ids: list[str] = list(self.human.decision_ids)
        self._plan_fields: dict[str, Any] | None = None  # what a --dry-run refusal reports

    # -- helpers -------------------------------------------------------------------------------

    @staticmethod
    def _state_dir() -> Path:
        try:
            return jsonlog.state_dir(os.environ)
        except jsonlog.JsonlError as err:
            raise ForgectlError(
                "audit_unavailable", f"audit.jsonl cannot be appended to: {err}"
            ) from None

    def _log(self, level: str, event: str, details: dict[str, Any]) -> None:
        text = jsonlog.log_event(
            self.directory, level, event, pr=self.pr_id, host=self.ref.host, details=details
        )
        if text:
            output.warn(text)

    def _read(self, readiness: Readiness = MERGE_READINESS) -> PrRead:
        return self.backend.get_pr(self.ref, readiness)

    @property
    def sha(self) -> str:
        assert self.expected_head is not None
        return self.expected_head

    @property
    def base(self) -> str:
        assert self.expected_base is not None
        return self.expected_base

    def _remember(self, read: PrRead, method: str | None) -> None:
        pr = read.pr
        self._plan_fields = {
            "head_sha": pr.head.sha,
            "expected_head": self.expected_head,
            "expected_base": None
            if self.expected_base is None
            else sanitize_line(self.expected_base),
            "base_ref": pr.base.ref,
            "blocked_by": _dicts(self._effective_blockers(read)),
            "method": method,
        }

    def _effective_blockers(self, read: PrRead) -> tuple[Blocker, ...]:
        blockers = read.pr.blocked_by
        if not self.human.is_human:
            blockers = (*blockers, *_ci_blockers(read))
        return sorted_blockers(blockers)

    # -- step 4 --------------------------------------------------------------------------------

    def _check(
        self, read: PrRead, *, earlier: tuple[Blocker, ...] | None, method: str | None = None
    ) -> Verdict:
        """The table of step 4. ``earlier`` is the blockers of the first run when this is the
        re-read of step 7: a blocker that was not there then refuses even under
        ``--override-blocks``. ``method`` is the first run's method on the re-read: it is kept,
        or refused if the repo no longer allows it, never resolved again."""
        pr = read.pr
        # a --dry-run refusal reports what was read, and the caller's method until it resolves
        self._remember(read, self.args.method if method is None else method)
        if pr.state == "closed":
            raise refused(
                "not_open", "the pull request is closed and not merged", details={"state": "closed"}
            )
        self._check_pin(pr)
        if pr.state == "merged":
            return Verdict(True, None, ())
        if pr.merge_queue is not False and not self.human.is_human:
            raise refused(
                "no_head_match",
                "a merge queue (or an unknown queue setting) needs a human: a non-human actor "
                "cannot rely on the head check",
            )
        if method is None:
            method = resolve_method(self.inv, pr)
        else:
            _still_allowed(pr, method)
        blockers = self._effective_blockers(read)
        self._remember(read, method)
        if blockers and not self.args.override_blocks:
            raise refused(
                "not_ready",
                "the pull request is not ready: " + ", ".join(b.code.value for b in blockers),
                details={"blocked_by": _dicts(blockers)},
            )
        if earlier is not None:
            new = [b for b in blockers if b not in earlier]
            if new:
                raise refused(
                    "not_ready",
                    "a new blocker appeared since the first read: "
                    + ", ".join(b.code.value for b in new),
                    details={"blocked_by": _dicts(blockers)},
                )
        return Verdict(False, method, blockers)

    def _check_pin(self, pr: PR) -> None:
        if pr.head.sha != self.expected_head:
            raise refused(
                "stale_head",
                "the head is not the expected head",
                details={"head_sha": pr.head.sha},
            )
        if not base_matches(pr, self.base):
            raise refused(
                "stale_base",
                "the base is not the expected base",
                details={"base_ref": pr.base.ref},
            )

    # -- the whole run -------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        args = self.args
        if args.auto:
            # TODO(#23): --auto
            raise ForgectlError("internal", "merge --auto: not implemented yet")
        if args.override_blocks and not self.human.is_human:  # the gate already refused it
            raise usage("--override-blocks is human-only")
        if self.backend.name == "gitlab":
            # TODO(#22): the GitLab backend's merge; refused before any read, hook or record
            raise ForgectlError("internal", "gitlab merge is not implemented yet (#22)")
        try:
            return self._run()
        except ForgectlError as err:
            if self.human.dry_run and err.exit_code == 20:
                if err.plan is None:
                    err.plan = dict(self._plan_fields or {})
                err.fields = {**(err.fields or {}), "pr": self.pr_id}
            raise

    def _run(self) -> dict[str, Any]:
        # step 3: the audit file is checked before the PR is read
        audit.check_writable(self.directory)
        first = self._read()
        self.pr_id = f"{first.pr.repo}#{first.pr.number}"
        self._pin(first.pr)
        verdict = self._check(first, earlier=None)
        if verdict.already_merged:
            return self._already_merged_output(first.pr)
        # step 5: confirmation. A dry run stops here; any other run without one is exit 13.
        dialog = require_confirmation(self.human)
        if self.human.dry_run:
            return self._dry_output(first, verdict)
        if dialog and not self._dialog(first, verdict):
            raise ForgectlError("confirmation_required", "declined")
        # step 6: pre-merge hooks (a veto or escalation raises). The method is fixed from here:
        # what the dialog showed and the hooks saw is what is written.
        method = verdict.method
        pre = hooks.run_pre_hooks(
            self.env.gateway,
            "pre-merge",
            self._hook_fields(first, method, verdict.blockers, ci=True),
            decision_ids=self.decision_ids,
            override_escalation=bool(self.args.override_escalation),
        )
        self._absorb_hooks(pre)
        self.decision_ids = list(pre.decision_ids)
        self.escalated = bool(pre.escalated)
        # step 7: a slow gate may have outlasted the state it saw
        current = first
        if dialog or hooks.entries(self.inv.config.config, "pre-merge"):
            current = self._read()
            verdict = self._check(current, earlier=self._effective_blockers(first), method=method)
            if verdict.already_merged:
                self._log("info", "already_merged", {"decision_ids": self.decision_ids})
                return self._already_merged_output(current.pr)
        return self._write(current, verdict, method)

    def _pin(self, pr: PR) -> None:
        """An interactive run takes the first read's head and base as given, each only where its
        flag was not (interface: Human actions)."""
        if self.expected_head is None and self.human.interactive:
            self.expected_head = pr.head.sha
        if self.expected_base is None and self.human.interactive:
            self.expected_base = pr.base.ref
        if self.expected_head is None or self.expected_base is None:
            raise usage("merge needs --expected-head and --expected-base unless interactive")

    # -- step 5: the dialog --------------------------------------------------------------------

    def _dialog(self, read: PrRead, verdict: Verdict) -> bool:
        pr = read.pr
        blockers = ", ".join(b.code.value for b in verdict.blockers) or "none"
        ci = "no checks on this head" if read.ci.state == "none" else read.ci.state
        lines = [
            f"merge {self.pr_id}",
            f"  head:    {pr.head.sha}",
            f"  base:    {pr.base.ref}",
            f"  method:  {verdict.method or 'the host decides'}",
            f"  ci:      {ci}",
            f"  blocked: {blockers}",
        ]
        if pr.merge_queue is not False:
            lines.append("  warning: a merge queue may apply (or the setting is unknown)")
        sys.stderr.write("\n".join(sanitize_line(line) for line in lines) + "\n")
        sys.stderr.write("Merge? [y/N] ")
        sys.stderr.flush()
        answer = sys.stdin.readline().strip().lower()
        return answer in ("y", "yes")

    # -- hooks ---------------------------------------------------------------------------------

    def _hook_fields(
        self, read: PrRead, method: str | None, blockers: tuple[Blocker, ...], *, ci: bool
    ) -> dict[str, Any]:
        pr = read.pr
        fields: dict[str, Any] = {
            "object_id": self.pr_id,
            "repo": pr.repo,
            "head_sha": pr.head.sha,
            "actor": self.human.actor,
            "action": "merge",
            "auto": False,
            "method": method,
            "base_ref": pr.base.ref,
            "expected_head": self.sha,
            "expected_base": self.base,
            "blocked_by": _dicts(blockers),
            "override_blocks": bool(self.args.override_blocks),
        }
        if ci:
            checks = sorted(read.ci.checks, key=lambda c: c.sort_key)
            fields["ci"] = {
                "state": read.ci.state,
                "checks": [{"name": c.name, "state": c.state} for c in checks],
            }
        return fields

    @staticmethod
    def _absorb_hooks(result: hooks.HookResult) -> None:
        for text in result.warnings:
            output.warn(text)

    def _post_hooks(
        self,
        read: PrRead,
        method: str | None,
        blockers: tuple[Blocker, ...],
        outcome: Outcome,
    ) -> None:
        # already merged by someone else: what merged is unknown
        fields = self._hook_fields(read, None if outcome.already else method, blockers, ci=False)
        fields["observed"] = outcome.observed
        fields["result"] = outcome.result
        result = hooks.run_post_hooks(
            self.env.gateway,
            "post-merge",
            fields,
            decision_ids=self.decision_ids,
            state_dir=str(self.directory),
            pr=self.pr_id,
        )
        self._absorb_hooks(result)
        for report in result.reports:
            if report.outcome == "failed":
                self._log(
                    "warn",
                    "hook_failed",
                    {"hook": report.hook, "index": report.index, "why": report.detail},
                )

    # -- step 8 to 10 --------------------------------------------------------------------------

    def _write(self, current: PrRead, verdict: Verdict, method: str | None) -> dict[str, Any]:
        pr = current.pr
        overrides: list[str] = []
        if self.args.override_blocks and verdict.blockers:
            overrides.append("override_blocks")
        if self.escalated:
            overrides.append("override_escalation")
        inputs = audit.AuditInputs(
            config_hash=self.inv.config.config_hash,
            config_path=self.inv.config.config_path,
            token_source=self.env.hosts.credential(self.ref.host).source,
            stdin_tty=self.human.stdin_tty,
            actor_source=self.human.actor_source,
            head_sha=self.sha,
            base_ref=pr.base.ref,
            method=method,
        )
        context = audit.AuditContext(
            actor=self.human.actor,
            decision_ids=self.decision_ids,
            target=self.pr_id,
            action="merge",
            inputs=inputs,
            override=overrides,
        )
        if self.env.gateway.budget.remaining() <= 0:  # nothing was sent: no `attempted` record
            raise ForgectlError("network_error", "the time budget ran out before the merge call")
        audit_error: ForgectlError | None = None
        signalled = False
        with audit.Attempt(self.directory, context) as attempt:
            attempt.begin()  # a failure here is audit_unavailable and nothing has been sent
            outcome = self._attempt(current, method)
            try:
                attempt.finish(outcome.result, observed=outcome.observed)
            except ForgectlError as err:  # audit_failed: the post- hooks still run
                audit_error = err
            except SignalExit:
                if not attempt.finished:
                    raise  # the outcome is not on record: Attempt records outcome_unknown
                # held during the append and delivered after it: the run ends with what the
                # record says, and without the post- hooks
                signalled = True
                audit_error = attempt.finish_error
        # A signal before the outcome was recorded left the block above (exit 12
        # outcome_unknown, no hooks). From here every path ends with the recorded outcome.
        if not signalled:
            try:
                self._post_hooks(current, method, verdict.blockers, outcome)
            except SignalExit:
                pass  # the running hook's group was ended; the remaining hooks do not run
        if audit_error is not None:
            raise audit_error
        if outcome.error is not None:
            raise outcome.error
        return self._merged_output(outcome, method)

    def _attempt(self, current: PrRead, method: str | None) -> Outcome:
        """The write and its verification. A bug here (any non-forgectl exception) is an
        unknown outcome: the write may have landed, so the audit result and the post- hooks
        still follow. A signal is not caught."""
        try:
            return self._merge_and_verify(current, method)
        except ForgectlError as err:  # typed: keeps its code; the record and hooks follow
            return Outcome("outcome_unknown", error=err)
        except Exception as exc:
            try:
                self._log("error", "internal", {"exception": type(exc).__name__})
            except Exception:  # a failing log must not skip the record and the hooks
                pass
            return Outcome(
                "outcome_unknown",
                error=ForgectlError(
                    "outcome_unknown",
                    f"internal error after the merge was sent ({type(exc).__name__}); "
                    "it may have landed",
                ),
            )

    def _merge_and_verify(self, current: PrRead, method: str | None) -> Outcome:
        try:
            answer = self.backend.merge(current.pr, sha=self.sha, method=method)
        except ForgectlError as err:
            if err.code == "outcome_unknown":  # a 5xx, a timeout, a protocol failure: step 9
                return self._verify(accepted=False, why=err)
            return Outcome("failed", error=err)
        if answer.kind == "accepted":
            return self._verify(accepted=True, why=None)
        if answer.kind == "stale_head":
            return Outcome("failed", error=self._stale_head())
        return self._after_405(answer)

    def _stale_head(self) -> ForgectlError:
        details: dict[str, Any] | None = None
        try:  # one read for the head the host saw; omitted if it fails
            details = {"head_sha": self._read(NO_READINESS).pr.head.sha}
        except ForgectlError:
            pass
        return refused(
            "stale_head",
            "the host refused the merge: the head is not the expected head",
            details=details,
        )

    def _after_405(self, answer: MergeAnswer) -> Outcome:
        """A 405: one re-read decides (interface: Merge order, step 8)."""
        shown = answer.message or "no reason given"
        rejected = ForgectlError(
            "host_rejected",
            f"the host refused the merge (405): {shown}",
            details={"status": answer.status},
        )
        try:
            pr = self._read(NO_READINESS).pr
        except ForgectlError:
            return Outcome("failed", error=rejected)
        if pr.state == "merged":
            if pr.head.sha == self.sha and base_matches(pr, self.base):
                return Outcome("merged", pr=pr, already=True)
            return self._unexpected(pr)
        if answer.kind == "base_modified":
            return Outcome("failed", error=rejected)
        blockers = sorted_blockers(pr.blocked_by)
        if pr.state == "open" and blockers:
            return Outcome(
                "failed",
                error=refused(
                    "not_ready",
                    "the host refused the merge and the pull request has blockers: "
                    + ", ".join(b.code.value for b in blockers),
                    details={"blocked_by": _dicts(blockers)},
                ),
            )
        if answer.kind == "method_not_allowed":
            return Outcome(
                "failed",
                error=refused(
                    "method_not_allowed",
                    f"the host does not allow this merge method: {shown}",
                    details={"allowed": _allowed_list(pr)},
                ),
            )
        return Outcome("failed", error=rejected)

    # -- step 9 --------------------------------------------------------------------------------

    def _verify(self, *, accepted: bool, why: ForgectlError | None) -> Outcome:
        """Poll the PR (up to 10 s): merged at the sent head and the expected base is success; a
        merged PR anywhere else is ``merge_unverified``; still open or closed after the window is
        ``merge_unverified`` after an accepted write and ``outcome_unknown`` after a failure
        answer; a failed read is ``outcome_unknown``."""
        last: PR | None = None
        for attempt in range(len(VERIFY_DELAYS) + 1):
            if attempt:
                delay = VERIFY_DELAYS[attempt - 1]
                if self.env.gateway.budget.remaining() <= delay:
                    break
                self.env.wait(delay)
            try:
                pr = self._read(NO_READINESS).pr
            except ForgectlError as err:
                return Outcome(
                    "outcome_unknown",
                    error=ForgectlError(
                        "outcome_unknown",
                        "the merge may have landed and the verification read failed: "
                        f"{err.message}",
                    ),
                )
            last = pr
            if pr.state == "merged":
                if pr.head.sha == self.sha and base_matches(pr, self.base):
                    return Outcome("merged", pr=pr)
                return self._unexpected(pr)
            if pr.state == "closed":
                break  # a closed PR does not merge later
        if accepted:
            seen = "not seen" if last is None else f"{last.state}, head {last.head.sha}"
            return Outcome(
                "outcome_unknown",
                error=ForgectlError(
                    "merge_unverified",
                    "the host accepted the merge but the pull request did not merge "
                    f"({seen}); it may be queued",
                    details={
                        "observed": seen,
                        "head_sha": self.sha if last is None else last.head.sha,
                    },
                ),
            )
        assert why is not None
        return Outcome(
            "outcome_unknown",
            error=ForgectlError(
                "outcome_unknown",
                f"{why.message}; the pull request is not merged after the verification window",
            ),
        )

    def _unexpected(self, pr: PR) -> Outcome:
        observed = {"head_sha": pr.head.sha, "base_ref": pr.base.ref}
        text = f"merged at head {pr.head.sha} into base {pr.base.ref}"
        return Outcome(
            "merged_unexpected",
            pr=pr,
            observed=observed,
            error=ForgectlError(
                "merge_unverified",
                f"the pull request merged, but not at the expected head and base: {text}",
                details={"observed": text, "head_sha": pr.head.sha},
            ),
        )

    # -- output --------------------------------------------------------------------------------

    def _merged_output(self, outcome: Outcome, method: str | None) -> dict[str, Any]:
        pr = outcome.pr
        assert pr is not None
        return {
            "pr": self.pr_id,
            "merged": True,
            "already_merged": outcome.already,
            "auto_merge_enabled": False,
            "merged_head_sha": pr.merged_head_sha,
            "merge_commit_sha": pr.merge_commit_sha,
            "method": None if outcome.already else method,
            "dry_run": False,
        }

    def _already_merged_output(self, pr: PR) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pr": self.pr_id,
            "merged": True,
            "already_merged": True,
            "auto_merge_enabled": False,
            "merged_head_sha": pr.merged_head_sha,
            "merge_commit_sha": pr.merge_commit_sha,
            "method": None,
            "dry_run": self.human.dry_run,
        }
        if self.human.dry_run:
            out["would_refuse"] = None
            out["plan"] = self._plan_for(pr, None, ())
        return out

    def _plan_for(
        self, pr: PR, method: str | None, blockers: tuple[Blocker, ...]
    ) -> dict[str, Any]:
        return {
            "head_sha": pr.head.sha,
            "expected_head": self.expected_head,
            "expected_base": None
            if self.expected_base is None
            else sanitize_line(self.expected_base),
            "base_ref": pr.base.ref,
            "auto": False,
            "override_blocks": bool(self.args.override_blocks),
            "blocked_by": _dicts(blockers),
            "method": method,
        }

    def _dry_output(self, read: PrRead, verdict: Verdict) -> dict[str, Any]:
        return {
            "pr": self.pr_id,
            "merged": False,
            "already_merged": False,
            "auto_merge_enabled": False,
            "merged_head_sha": None,
            "merge_commit_sha": None,
            "method": verdict.method,
            "dry_run": True,
            "would_refuse": None,
            "plan": self._plan_for(read.pr, verdict.method, verdict.blockers),
        }


@registry.register("merge")
def merge(inv: registry.Invocation) -> dict[str, Any]:
    return MergeRun(inv).run()
