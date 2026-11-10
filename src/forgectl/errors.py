"""Error codes, exit codes and the error envelope (interface: Exit codes, Error envelope)."""

from __future__ import annotations

from typing import Any

from .sanitize import sanitize_line, sanitize_text

EXIT_BY_CODE: dict[str, int] = {
    "internal": 1,
    "usage": 2,
    "config_invalid": 2,
    "invalid_pr": 2,
    "body_too_large": 2,
    "method_required": 2,
    "not_found": 10,
    "vetoed": 11,
    "auth_required": 12,
    "permission_denied": 12,
    "rate_limited": 12,
    "upstream_error": 12,
    "network_error": 12,
    "tool_missing": 12,
    "host_rejected": 12,
    "outcome_unknown": 12,
    "outcome_partial": 12,
    "merge_unverified": 12,
    "invalid_position": 12,
    "quick_action_ran": 12,
    "approved_stale_head": 12,
    "wrong_host_response": 12,
    "audit_unavailable": 12,
    "audit_failed": 12,
    "partial_results": 12,
    "unsupported_host_version": 12,
    "confirmation_required": 13,
    "escalated": 13,
    # Exit 20 is always `refused`, with a `reason`.
    "refused": 20,
}

REFUSAL_REASONS: frozenset[str] = frozenset(
    {
        "unknown_host",
        "stale_head",
        "stale_base",
        "no_head_match",
        "not_open",
        "not_ready",
        "method_not_allowed",
        "auto_merge_unavailable",
        "self_approval",
        "not_resolvable",
        "too_large",
        "drafts_present",
        "pending_review_exists",
    }
)

HOOK_STDERR_LIMIT = 4096

# `details` keys the interface's Error envelope table lists without a condition, by code or (for
# exit 20) by reason. `stale_head.head_sha` (when known), `auth_required.variable` and
# `host_rejected.leftover_drafts` are conditional and so not required.
REQUIRED_DETAILS_BY_CODE: dict[str, tuple[str, ...]] = {
    "method_required": ("allowed",),
    "invalid_position": ("invalid",),
    "outcome_partial": ("landed", "failed"),
    "host_rejected": ("status",),
    "quick_action_ran": ("ids",),
    "approved_stale_head": ("ids",),
    "audit_failed": ("outcome",),
    "body_too_large": ("index", "limit"),
    "config_invalid": ("key",),
    "tool_missing": ("install",),
    "wrong_host_response": ("observed",),
    "merge_unverified": ("observed", "head_sha"),
}
REQUIRED_DETAILS_BY_REASON: dict[str, tuple[str, ...]] = {
    "not_ready": ("blocked_by",),
    "drafts_present": ("count",),
    "stale_base": ("base_ref",),
    "not_open": ("state",),
    "self_approval": ("basis",),
    "method_not_allowed": ("allowed",),
    "auto_merge_unavailable": ("observed",),
}


def truncate_utf8(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes, on a code-point boundary."""
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def sanitize_tree(value: Any) -> Any:
    """Sanitize every string value as text and every string key as one line (a key must not
    start a line of its own either). The `x-one-line` fields of a forge's own data are sanitized
    where that data is normalized (`backends/`), by field, not here."""
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_tree(v) for v in value]
    if isinstance(value, dict):
        return {
            (sanitize_line(k) if isinstance(k, str) else k): sanitize_tree(v)
            for k, v in value.items()
        }
    return value


class ForgectlError(Exception):
    """A failure that ends the command with an exit code and the error envelope.

    ``plan`` and ``fields`` are not part of the envelope: a ``--dry-run`` that ends in exit 20
    uses them to fill the plan and the other output fields of its ``would_refuse`` result.
    ``output`` is the command's normal result for exit 12 ``partial_results`` (``prs``,
    ``whoami``), which is printed in place of the envelope.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after: float | None = None,
        hook: str | None = None,
        hook_stderr: str | None = None,
        decision_ids: list[str] | None = None,
        escalated_hooks: list[dict[str, Any]] | None = None,
        plan: dict[str, Any] | None = None,
        fields: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
    ) -> None:
        if code not in EXIT_BY_CODE:
            raise ValueError(f"unknown error code: {code!r}")
        if code == "refused":
            if reason not in REFUSAL_REASONS:
                raise ValueError(f"refused needs a known reason, got {reason!r}")
        elif reason is not None:
            raise ValueError(f"reason is only for code 'refused', not {code!r}")
        if code in ("vetoed", "escalated") and (hook is None or hook_stderr is None):
            raise ValueError(f"{code} needs hook and hook_stderr")
        if code == "escalated" and not escalated_hooks:
            raise ValueError("escalated needs escalated_hooks")
        required = REQUIRED_DETAILS_BY_CODE.get(code, ()) + REQUIRED_DETAILS_BY_REASON.get(
            reason or "", ()
        )
        missing = [key for key in required if key not in (details or {})]
        if missing:
            raise ValueError(f"{reason or code} needs details {missing}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.details = details
        self.retry_after = retry_after
        self.hook = hook
        self.hook_stderr = hook_stderr
        self.decision_ids = decision_ids
        self.escalated_hooks = escalated_hooks
        self.plan = plan
        self.fields = fields
        self.output = output

    @property
    def exit_code(self) -> int:
        return EXIT_BY_CODE[self.code]

    def envelope(self) -> dict[str, Any]:
        """The `--json` error envelope; empty optional fields are omitted."""
        body: dict[str, Any] = {"code": self.code, "message": sanitize_text(self.message)}
        if self.reason is not None:
            body["reason"] = self.reason
        if self.details:
            body["details"] = sanitize_tree(self.details)
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        if self.hook is not None:
            body["hook"] = self.hook
        if self.hook_stderr or self.code in ("vetoed", "escalated"):
            stderr = sanitize_text(self.hook_stderr or "")
            body["hook_stderr"] = truncate_utf8(stderr, HOOK_STDERR_LIMIT)
        if self.decision_ids:
            body["decision_ids"] = list(self.decision_ids)
        if self.escalated_hooks:
            body["escalated_hooks"] = list(self.escalated_hooks)
        return {"error": body}


def refused(
    reason: str, message: str, *, details: dict[str, Any] | None = None, **kwargs: Any
) -> ForgectlError:
    """An exit 20 refusal."""
    return ForgectlError("refused", message, reason=reason, details=details, **kwargs)


def usage(message: str, **kwargs: Any) -> ForgectlError:
    """An exit 2 usage error."""
    return ForgectlError("usage", message, **kwargs)


_write_attempted = False


def mark_write_attempted() -> None:
    """Process-level: ``audit.Attempt.begin()`` ran, so a write may have reached the host."""
    global _write_attempted
    _write_attempted = True


def write_was_attempted() -> bool:
    return _write_attempted


def reset_write_attempted() -> None:
    """Start of a run (and of each test): nothing has been attempted yet."""
    global _write_attempted
    _write_attempted = False


class SignalExit(BaseException):
    """A SIGINT, SIGTERM or SIGHUP arrived: exit 128+N with no envelope, unless a write was
    attempted (``write_attempted``): then exit 12 ``outcome_unknown``.

    The gateway ends the child's group, removes its private directories and lets this propagate.
    ``audit.Attempt`` sets ``write_attempted`` when the signal passes through its block after
    ``begin()``, once it has recorded ``outcome_unknown``. A signal that never passes through
    that block (while the result is printed, say) is told by the process-level flag
    ``write_was_attempted()``, set by ``begin()``.
    """

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum
        self.write_attempted = False
