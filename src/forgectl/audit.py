"""``audit.jsonl``: two records per write that reached the host (interface: Audit and log).

``Attempt`` writes the ``attempted`` record before the run's first host write and an outcome
record with the same ``attempt_id`` after it. Leaving the ``with`` block with no outcome (an
exception, a signal) records ``outcome_unknown``, so every attempt ends in a record.

A first append that fails is exit 12 ``audit_unavailable`` (nothing was sent); a second that
fails is exit 12 ``audit_failed`` with the outcome's result in ``details``.

Records are validated by ``audit_problem`` before they are written, a hand check that a test
compares with ``schemas/audit.json``; jsonschema is test-only.
"""

from __future__ import annotations

import secrets
import signal
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from . import jsonlog
from .errors import ForgectlError, SignalExit
from .jsonlog import PATTERNS, JsonlError, matches, parse_ts
from .sanitize import sanitize_line, sanitize_text

__all__ = [
    "PATTERNS",
    "Attempt",
    "AuditContext",
    "AuditInputs",
    "audit_problem",
    "check_writable",
    "sanitized",
]

AUDIT_FILE = "audit.jsonl"
MAX_REF_CHARS = 1024  # base_ref is untrusted; a branch name is never this long in practice

ACTIONS = ("merge", "review", "comment", "reply", "approve", "resolve", "unresolve")
RESULTS = (
    "attempted",
    "merged",
    "merged_unexpected",
    "auto_merge_enabled",
    "posted",
    "approved",
    "resolved",
    "unresolved",
    "failed",
    "outcome_unknown",
    "outcome_partial",
    "quick_action_ran",
)
_COMMON = ("attempted", "failed", "outcome_unknown")
RESULTS_BY_ACTION: dict[str, tuple[str, ...]] = {
    "merge": (*_COMMON, "merged", "merged_unexpected", "auto_merge_enabled"),
    "review": (*_COMMON, "posted", "approved", "outcome_partial", "quick_action_ran"),
    "comment": (*_COMMON, "posted", "quick_action_ran"),
    "reply": (*_COMMON, "posted", "quick_action_ran"),
    "approve": (*_COMMON, "approved", "outcome_partial", "quick_action_ran"),
    "resolve": (*_COMMON, "resolved"),
    "unresolve": (*_COMMON, "unresolved"),
}
OVERRIDES = ("override_blocks", "override_escalation", "allow_self")
EVENTS = ("comment", "request_changes", "approve", None)
METHODS = ("squash", "merge", "rebase", None)
TOKEN_SOURCES = ("token_env", "token_command", "stored_login", "none")
ACTOR_SOURCES = ("flag", "tty", "default")
_INPUT_KEYS = (
    "head_sha",
    "base_ref",
    "event",
    "body_sha256",
    "method",
    "thread_id",
    "reply_to",
    "config_hash",
    "config_path",
    "host_login",
    "token_source",
    "stdin_tty",
    "actor_source",
)
_KEYS = (
    "ts",
    "attempt_id",
    "actor",
    "decision_id",
    "target",
    "action",
    "inputs",
    "result",
    "override",
    "ids",
    "observed",
)
_REQUIRED = _KEYS[:9]


def _opt(name: str, value: object) -> bool:
    return value is None or matches(name, value)


def _inputs_problem(inputs: object) -> str | None:
    if not isinstance(inputs, dict) or set(inputs) != set(_INPUT_KEYS):
        return "inputs"
    checks: dict[str, bool] = {
        "head_sha": _opt("sha", inputs["head_sha"]),
        "base_ref": inputs["base_ref"] is None or isinstance(inputs["base_ref"], str),
        "event": inputs["event"] in EVENTS,
        "body_sha256": _opt("sha256", inputs["body_sha256"]),
        "method": inputs["method"] in METHODS,
        "thread_id": _opt("id", inputs["thread_id"]),
        "reply_to": _opt("id", inputs["reply_to"]),
        "config_hash": matches("sha256", inputs["config_hash"]),
        "config_path": inputs["config_path"] is None or isinstance(inputs["config_path"], str),
        "host_login": inputs["host_login"] is None
        or (isinstance(inputs["host_login"], str) and inputs["host_login"] != ""),
        "token_source": inputs["token_source"] in TOKEN_SOURCES,
        "stdin_tty": isinstance(inputs["stdin_tty"], bool),
        "actor_source": inputs["actor_source"] in ACTOR_SOURCES,
    }
    bad = [name for name, ok in checks.items() if not ok]
    return f"inputs.{bad[0]}" if bad else None


def _observed_problem(observed: object) -> bool:
    if observed is None:
        return False
    return not (
        isinstance(observed, dict)
        and set(observed) == {"head_sha", "base_ref"}
        and matches("sha", observed["head_sha"])
        and isinstance(observed["base_ref"], str)
    )


def audit_problem(record: object) -> str | None:
    """Why ``record`` is not a valid ``audit.json`` record (the field), or None."""
    if not isinstance(record, dict):
        return "not an object"
    unknown = set(record) - set(_KEYS)
    if unknown:
        return "unknown key"
    missing = [key for key in _REQUIRED if key not in record]
    if missing:
        return f"missing {missing[0]}"
    attempted = record["result"] == "attempted"
    if attempted and ("ids" in record or "observed" in record):
        return "attempted record with outcome fields"
    if not attempted and ("ids" not in record or "observed" not in record):
        return "outcome record without ids or observed"
    action, result = record["action"], record["result"]
    # Type-check before any dict or set lookup: an unhashable value must not raise.
    action_ok = isinstance(action, str) and action in ACTIONS
    decisions = record["decision_id"]
    ids = record.get("ids", [])
    overrides = record["override"]
    checks: dict[str, bool] = {
        "ts": parse_ts(record["ts"]) is not None,
        "attempt_id": matches("attempt_id", record["attempt_id"]),
        "actor": matches("actor", record["actor"]),
        "decision_id": isinstance(decisions, list)
        and len(decisions) <= 32
        and all(matches("decision_id", d) for d in decisions),
        "target": matches("pr", record["target"]),
        "action": action_ok,
        "result": isinstance(result, str)
        and result in RESULTS
        and action_ok
        and result in RESULTS_BY_ACTION[action],
        "override": isinstance(overrides, list)
        and all(o in OVERRIDES for o in overrides)
        and len(set(overrides)) == len(overrides),
        "ids": isinstance(ids, list) and all(matches("id", i) for i in ids),
        "observed": not _observed_problem(record.get("observed"))
        and (result != "merged_unexpected" or record.get("observed") is not None),
    }
    bad = [name for name, ok in checks.items() if not ok]
    if bad:
        return bad[0]
    return _inputs_problem(record["inputs"])


def new_attempt_id() -> str:
    return secrets.token_urlsafe(12)


def _cap(ref: str) -> str:
    return ref if len(ref) <= MAX_REF_CHARS else ref[:MAX_REF_CHARS]


@dataclass(frozen=True)
class AuditInputs:
    """The ``inputs`` object. Only hashes and ids, never a body or a token."""

    config_hash: str
    token_source: str
    stdin_tty: bool
    actor_source: str
    head_sha: str | None = None
    base_ref: str | None = None
    event: str | None = None
    body_sha256: str | None = None
    method: str | None = None
    thread_id: str | None = None
    reply_to: str | None = None
    config_path: str | None = None
    host_login: str | None = None

    def as_record(self) -> dict[str, Any]:
        record = {key: getattr(self, key) for key in _INPUT_KEYS}
        if record["base_ref"] is not None:
            record["base_ref"] = _cap(record["base_ref"])
        return record


@dataclass(frozen=True)
class AuditContext:
    actor: str
    decision_ids: Sequence[str]
    target: str
    action: str
    inputs: AuditInputs
    override: Sequence[str] = field(default_factory=tuple)


def _unavailable(err: JsonlError) -> ForgectlError:
    return ForgectlError("audit_unavailable", f"audit.jsonl cannot be appended to: {err}")


def check_writable(directory: Path) -> None:
    """Interface Merge order step 3: the file can be appended, before the PR is read."""
    try:
        jsonlog.check_appendable(directory, AUDIT_FILE)
    except JsonlError as err:
        raise _unavailable(err) from None


#: The signals cli.py turns into ``SignalExit`` (SIGHUP does not exist on Windows).
_HELD_SIGNALS = frozenset(
    getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name)
)


@contextmanager
def _signals_held() -> Iterator[None]:
    """Hold SIGINT, SIGTERM and SIGHUP while a record is appended and its flag set.

    A signal that arrives meanwhile stays pending and is delivered when the old mask is
    restored, so its handler's exception is raised from that restore: after the record is on
    disk and the flag is set. The record is then neither dropped nor written twice. Off the
    main thread (or without ``pthread_sigmask``) the mask would not cover the thread that runs
    Python handlers, so nothing is held there.
    """
    if not hasattr(signal, "pthread_sigmask") or (
        threading.current_thread() is not threading.main_thread()
    ):
        yield
        return
    # Read the mask without changing it, so a signal raised as the block returns still
    # reaches the restore below.
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, _HELD_SIGNALS)
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


class Attempt:
    """One run's audit records. Use as a context manager around the writes."""

    def __init__(self, directory: Path, context: AuditContext) -> None:
        self._dir = directory
        self._ctx = context
        self.attempt_id = new_attempt_id()
        self._begun = False
        self._finished = False
        #: ``audit_failed`` when the outcome append failed, kept so a signal delivered as the
        #: append returns does not lose it.
        self.finish_error: ForgectlError | None = None

    @property
    def finished(self) -> bool:
        """Whether the outcome append ran (written, or failed as ``finish_error``)."""
        return self._finished

    def _record(
        self,
        result: str,
        ids: Iterable[str] | None = None,
        observed: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        ctx = self._ctx
        record: dict[str, Any] = {
            "ts": jsonlog.now_ts(),
            "attempt_id": self.attempt_id,
            "actor": ctx.actor,
            "decision_id": list(ctx.decision_ids),
            "target": ctx.target,
            "action": ctx.action,
            "inputs": ctx.inputs.as_record(),
            "result": result,
            "override": list(ctx.override),
        }
        if result != "attempted":
            record["ids"] = list(ids or [])
            record["observed"] = (
                None
                if observed is None
                else {"head_sha": observed["head_sha"], "base_ref": _cap(observed["base_ref"])}
            )
        problem = audit_problem(record)
        if problem is not None:
            raise ValueError(f"audit record invalid: {problem}: {result} for {ctx.action}")
        return record

    def begin(self) -> None:
        """Append ``attempted``, once. Exit 12 ``audit_unavailable`` if it cannot be written."""
        if self._begun:
            return
        record = self._record("attempted")
        with _signals_held():
            try:
                jsonlog.append(self._dir, AUDIT_FILE, record)
            except JsonlError as err:
                raise _unavailable(err) from None
            self._begun = True

    def finish(
        self,
        result: str,
        *,
        ids: Iterable[str] | None = None,
        observed: Mapping[str, str] | None = None,
    ) -> None:
        """Append the outcome, once. Exit 12 ``audit_failed`` if it cannot be written, with the
        result in ``details.outcome``. A signal held during the append is raised as it returns,
        before that error; ``finish_error`` keeps it."""
        if not self._begun:
            raise RuntimeError("finish() before begin()")
        if self._finished:
            return
        record = self._record(result, ids, observed)
        # TODO(#26): an outcome record's `ids` is unbounded, so a large review can pass the
        # 64 KiB line cap and end in audit_failed after the post landed. Decided: `review`
        # accepts at most 200 inline comments per call (more is a usage error, exit 2), which
        # keeps this record under the cap. Enforced in #26 and #27, not here.
        with _signals_held():
            self._finished = True
            try:
                jsonlog.append(self._dir, AUDIT_FILE, record)
            except JsonlError as err:
                self.finish_error = ForgectlError(
                    "audit_failed",
                    f"the outcome could not be recorded in audit.jsonl: {err}",
                    details={"outcome": result},
                )
        if self.finish_error is not None:
            raise self.finish_error

    def __enter__(self) -> Attempt:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        if not self._begun:
            return False
        if isinstance(exc, SignalExit):
            exc.write_attempted = True  # cli exits 12 outcome_unknown, not 128+N
        if self._finished:
            return False
        try:
            self.finish("outcome_unknown")
        except SignalExit as late:  # held during the append, it replaces the one in flight
            late.write_attempted = True
            raise
        except ForgectlError:
            if exc is None or isinstance(exc, Exception):
                raise  # audit_failed carries the outcome; a signal in flight is not replaced
        return False


# --- reading, for export --------------------------------------------------------------------------

_ONE_LINE_PATHS = (("inputs", "base_ref"), ("inputs", "host_login"), ("observed", "base_ref"))


def sanitized(record: dict[str, Any]) -> dict[str, Any]:
    """A copy of a valid record with every untrusted string made safe to print."""
    out = _sanitize_tree(record)
    for outer, inner in _ONE_LINE_PATHS:
        holder = record.get(outer)
        if isinstance(holder, dict) and isinstance(holder.get(inner), str):
            out[outer][inner] = sanitize_line(holder[inner])
    return out


def _sanitize_tree(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [_sanitize_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_tree(v) for k, v in value.items()}
    return value
