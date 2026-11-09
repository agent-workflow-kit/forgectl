"""Hook machinery (interface: Hooks).

A hook is a user-configured shell command that sees one strict-JSON payload on stdin and answers by
its exit status. This module owns what the commands that fire hooks (``merge``, ``review``,
``comment``, ``reply``, ``approve``, ``resolve``, ``unresolve``) share:

- the payload: built here from the fields the command passes, sanitized, serialized as strict
  JSON and sent on stdin only (never argv, never the environment); a body is never in it;
- the process: ``sh -c <cmd>`` through ``gateway.run_external(mode="hook")``, so it runs as the
  leader of its own process group in a private empty cwd, with forgectl's environment minus every
  token variable, a per-hook timeout and a 64 KiB stdout cap;
- the outcome of each hook: exit 0 allows, 13 escalates (later hooks still run), anything else, a
  timeout, a stdout over the cap or output still held open by a leftover process vetoes at once;
- the decision ids a hook prints, collected under the grammar and the cap of 32;
- the ``async`` post hooks, started under a detached watchdog (``hookwatch.py``).

A hook can change nothing the command was shown: it receives a serialized copy, and the only thing
read back from it is its exit status, its stderr (as text for the error) and ``decision_id`` from
its stdout. ``pre-`` hooks raise ``ForgectlError`` ``vetoed`` or ``escalated``; ``post-`` hooks
never raise for a hook's failure, they warn.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import gateway as gw
from .config import Config
from .errors import HOOK_STDERR_LIMIT, ForgectlError, truncate_utf8
from .gateway import Gateway
from .sanitize import sanitize_bytes, sanitize_line, sanitize_text

SHELL = "/bin/sh"
MAX_STDOUT = 64 * 1024
MAX_STDERR = 64 * 1024  # read and kept in memory; the error shows the first 4096 bytes
MAX_PAYLOAD = 1024 * 1024
MAX_DECISION_IDS = 32
ESCALATE_EXIT = 13
VETO_EXIT = 11
DECISION_ID = re.compile(r"[A-Za-z0-9._:@/-]{1,128}")

PRE_HOOKS = ("pre-merge", "pre-post")
POST_HOOKS = ("post-merge", "post-post")
HOOKS = ("pre-merge", "post-merge", "pre-post", "post-post")

_MERGE = ("auto", "method", "base_ref", "expected_head", "expected_base", "blocked_by")
_POST = ("event", "body_sha256", "body_bytes", "thread_id", "thread_author_is_self", "reply_to")
_COMMON = ("object_id", "repo", "head_sha", "actor", "action")
# What the calling command passes, per hook; forgectl adds tool, hook, object_type, decision_ids.
FIELDS: dict[str, frozenset[str]] = {
    "pre-merge": frozenset((*_COMMON, *_MERGE, "override_blocks", "ci", "override_escalation")),
    "post-merge": frozenset((*_COMMON, *_MERGE, "override_blocks", "observed", "result")),
    "pre-post": frozenset((*_COMMON, *_POST, "override_escalation", "allow_self")),
    "post-post": frozenset((*_COMMON, *_POST, "result", "ids")),
}
OBJECT_TYPE = "pr"


# -- Payload -----------------------------------------------------------------------------------


def _sanitized(value: Any) -> Any:
    """Every string in the payload through ``sanitize_line``: a one-line marker form that holds no
    control, bidi or invisible character and no lone surrogate (so the JSON is valid for any
    consumer). A value that satisfies the schemas' grammars is unchanged."""
    if isinstance(value, str):
        return sanitize_line(value)
    if isinstance(value, list | tuple):
        return [_sanitized(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _sanitized(v) for k, v in value.items()}
    return value


def build_payload(
    hook: str, fields: Mapping[str, Any], decision_ids: Sequence[str] = ()
) -> dict[str, Any]:
    """The payload for ``hook``: exactly the fields of its schema, sanitized. ``fields`` is not
    modified. A missing or extra field is a programming error (``ValueError``): a body or any
    other key can never ride along."""
    if hook not in FIELDS:
        raise ValueError(f"unknown hook {hook!r}")
    names = set(fields)
    if names != FIELDS[hook]:
        missing, extra = sorted(FIELDS[hook] - names), sorted(names - FIELDS[hook])
        raise ValueError(f"{hook} payload fields: missing {missing}, unexpected {extra}")
    payload: dict[str, Any] = {
        "tool": "forgectl",
        "hook": hook,
        "object_type": OBJECT_TYPE,
        **_sanitized(dict(fields)),
        "decision_ids": list(decision_ids),
    }
    return payload


def serialize(payload: Mapping[str, Any]) -> bytes:
    """Strict JSON (no NaN, ASCII only) plus a newline, at most ``MAX_PAYLOAD`` bytes."""
    data = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    out = data.encode("ascii") + b"\n"
    if len(out) > MAX_PAYLOAD:
        raise ForgectlError(
            "internal", f"hook payload is {len(out)} bytes, over the {MAX_PAYLOAD} byte limit"
        )
    return out


# -- Decision ids ------------------------------------------------------------------------------


def parse_decision_ids(stdout: bytes) -> tuple[list[str], str | None]:
    """The ``decision_id`` a hook printed: ``([ids], None)``, or ``([], reason)`` when the output
    is not a strict JSON object whose ``decision_id`` is one valid id or a list of them. Empty
    output and an object without the key are fine and give ``([], None)``. One invalid id
    invalidates the whole output (the conservative reading of "invalid output is ignored")."""
    if not stdout.strip():
        return [], None
    try:
        data = gw.parse_json(stdout)
    except ValueError:
        return [], "stdout is not strict JSON"
    if not isinstance(data, dict):
        return [], "stdout is not a JSON object"
    if "decision_id" not in data:
        return [], None
    value = data["decision_id"]
    items = value if isinstance(value, list) else [value]
    for item in items:
        if not isinstance(item, str) or DECISION_ID.fullmatch(item) is None:
            return [], "decision_id does not match the grammar"
    return list(items), None


def merge_decision_ids(known: Sequence[str], new: Sequence[str]) -> tuple[list[str], int]:
    """``known`` then the unseen ``new`` ids, deduplicated in first-seen order, cut to
    ``MAX_DECISION_IDS``; also returns how many new ids were dropped for the cap."""
    merged = list(dict.fromkeys(known))
    dropped = 0
    for item in dict.fromkeys(new):
        if item in merged:
            continue
        if len(merged) >= MAX_DECISION_IDS:
            dropped += 1
        else:
            merged.append(item)
    return merged, dropped


# -- Configuration -----------------------------------------------------------------------------


@dataclass(frozen=True)
class HookEntry:
    hook: str
    index: int  # position in the configured list
    cmd: str
    timeout: float
    is_async: bool = False


def entries(config: Config, hook: str) -> list[HookEntry]:
    """The configured entries of ``hook``, in order."""
    attribute = {
        "pre-merge": "pre_merge",
        "post-merge": "post_merge",
        "pre-post": "pre_post",
        "post-post": "post_post",
    }.get(hook)
    if attribute is None:
        raise ValueError(f"unknown hook {hook!r}")
    return [
        HookEntry(hook, i, e.cmd, float(e.timeout), bool(getattr(e, "async_", False)))
        for i, e in enumerate(getattr(config.hooks, attribute))
    ]


# -- Running -----------------------------------------------------------------------------------


@dataclass
class HookReport:
    """What one hook did, for the log."""

    hook: str
    index: int
    outcome: str  # allow | escalate | veto | failed | async_started
    detail: str = ""
    exit_code: int | None = None


@dataclass
class HookResult:
    decision_ids: list[str]  # the ids given plus the ones the hooks printed, deduplicated, <= 32
    escalated: list[dict[str, Any]] = field(default_factory=list)  # [{hook, index}] (overridden)
    warnings: list[str] = field(default_factory=list)
    reports: list[HookReport] = field(default_factory=list)


def _stderr_text(data: bytes) -> str:
    return truncate_utf8(sanitize_bytes(data), HOOK_STDERR_LIMIT)


@dataclass
class _Ran:
    entry: HookEntry
    outcome: str  # allow | escalate | veto
    reason: str  # why, for a veto (forgectl's words, no hook text)
    stderr: str
    ids: list[str]
    warning: str | None
    exit_code: int | None


def _run_sync(
    gateway: Gateway, entry: HookEntry, payload: bytes, object_id: str, *, collect_ids: bool
) -> _Ran:
    """Run one hook to its end and classify it. Never raises for what the hook did."""
    name = f"{entry.hook}[{entry.index}]"
    try:
        result = gateway.run_external(
            [SHELL, "-c", entry.cmd],
            timeout=entry.timeout,
            mode="hook",
            extra_env={
                "FORGECTL_HOOK": entry.hook,
                "FORGECTL_ID": object_id,
                "FORGECTL_OBJECT_TYPE": OBJECT_TYPE,
            },
            stdin=payload,
            max_stdout=MAX_STDOUT,
            max_stderr=MAX_STDERR,
        )
    except gw.ExecutableMissing:
        return _Ran(entry, "veto", f"{name} could not be started", "", [], None, None)
    stderr = _stderr_text(result.stderr)
    code = result.returncode
    if result.timed_out:
        return _Ran(
            entry, "veto", f"{name} timed out after {entry.timeout:g}s", stderr, [], None, code
        )
    if result.stdout_truncated:
        reason = f"{name} wrote more than {MAX_STDOUT} bytes to stdout"
        return _Ran(entry, "veto", reason, stderr, [], None, code)
    if result.streams_incomplete:
        reason = f"{name} left its output open after it exited"
        return _Ran(entry, "veto", reason, stderr, [], None, code)
    ids: list[str] = []
    warning: str | None = None
    if collect_ids:
        ids, problem = parse_decision_ids(result.stdout)
        if problem is not None:
            warning = f"{name}: {problem}; ignored"
    if code == 0:
        return _Ran(entry, "allow", "", stderr, ids, warning, code)
    if code == ESCALATE_EXIT:
        return _Ran(entry, "escalate", f"{name} escalated", stderr, ids, warning, code)
    how = f"was killed by signal {-code}" if code < 0 else f"exited {code}"
    return _Ran(entry, "veto", f"{name} {how}", stderr, ids, warning, code)


def _absorb(result: HookResult, ran: _Ran, ids_now: list[str]) -> list[str]:
    """Fold a hook's printed ids and warning into ``result``; returns the new id list."""
    if ran.warning:
        result.warnings.append(ran.warning)
    merged, dropped = merge_decision_ids(ids_now, ran.ids)
    if dropped:
        result.warnings.append(
            f"{ran.entry.hook}[{ran.entry.index}]: {dropped} decision id(s) past "
            f"{MAX_DECISION_IDS} dropped"
        )
    return merged


def run_pre_hooks(
    gateway: Gateway,
    hook: str,
    fields: Mapping[str, Any],
    *,
    decision_ids: Sequence[str] = (),
    override_escalation: bool = False,
) -> HookResult:
    """Run the ``pre-`` hook ``hook`` in order. Returns the ids to record, or raises.

    A veto (any exit but 0 and 13, a timeout, an over-cap stdout, output held open) raises
    ``vetoed`` at once and later hooks do not run. A 13 escalates and later hooks still run; if
    none vetoes, ``escalated`` is raised, unless ``override_escalation``, which counts 13 as
    allow and lists the entries in ``HookResult.escalated`` so the caller can record the
    override. ``fields`` must carry exactly the schema's fields (``FIELDS``); its
    ``override_escalation`` is the caller's to set.
    """
    if hook not in PRE_HOOKS:
        raise ValueError(f"not a pre hook: {hook!r}")
    configured = entries(gateway.config.config, hook)
    ids = list(merge_decision_ids((), decision_ids)[0])
    result = HookResult(ids)
    escalated_stderr: str | None = None
    object_id = str(fields.get("object_id", ""))
    for entry in configured:
        payload = serialize(build_payload(hook, fields, ids))
        ran = _run_sync(gateway, entry, payload, _sanitized(object_id), collect_ids=True)
        ids = _absorb(result, ran, ids)
        result.decision_ids = ids
        result.reports.append(HookReport(hook, entry.index, ran.outcome, ran.reason, ran.exit_code))
        if ran.outcome == "veto":
            raise ForgectlError(
                "vetoed",
                ran.reason,
                hook=hook,
                hook_stderr=ran.stderr,
                decision_ids=list(ids) or None,
            )
        if ran.outcome == "escalate":
            result.escalated.append({"hook": hook, "index": entry.index})
            if escalated_stderr is None:
                escalated_stderr = ran.stderr
    if result.escalated and not override_escalation:
        raise ForgectlError(
            "escalated",
            f"{len(result.escalated)} {hook} hook(s) escalated",
            hook=hook,
            hook_stderr=escalated_stderr or "",
            decision_ids=list(ids) or None,
            escalated_hooks=list(result.escalated),
        )
    return result


def run_post_hooks(
    gateway: Gateway,
    hook: str,
    fields: Mapping[str, Any],
    *,
    decision_ids: Sequence[str] = (),
    state_dir: str | None = None,
    pr: str | None = None,
) -> HookResult:
    """Run the ``post-`` hook ``hook`` in order. A failure of any kind is a warning in the result,
    never an exception (only a programming error or a signal raises). ``async`` entries start
    under the watchdog and are not waited for: their stdout is discarded and their failures are
    logged by the watchdog under ``state_dir`` (``pr`` is that record's ``pr``)."""
    if hook not in POST_HOOKS:
        raise ValueError(f"not a post hook: {hook!r}")
    configured = entries(gateway.config.config, hook)
    ids = list(merge_decision_ids((), decision_ids)[0])
    result = HookResult(ids)
    object_id = str(fields.get("object_id", ""))
    for entry in configured:
        name = f"{hook}[{entry.index}]"
        try:
            payload = serialize(build_payload(hook, fields, ids))
        except ForgectlError as exc:
            result.warnings.append(f"{name}: {exc.message}; not run")
            result.reports.append(HookReport(hook, entry.index, "failed", exc.message))
            continue
        if entry.is_async:
            problem = _start_async(gateway, entry, payload, object_id, state_dir, pr)
            if problem is None:
                result.reports.append(HookReport(hook, entry.index, "async_started"))
            else:
                result.warnings.append(f"{name}: {problem}")
                result.reports.append(HookReport(hook, entry.index, "failed", problem))
            continue
        ran = _run_sync(gateway, entry, payload, _sanitized(object_id), collect_ids=True)
        ids = _absorb(result, ran, ids)
        result.decision_ids = ids
        if ran.outcome == "allow":
            result.reports.append(HookReport(hook, entry.index, "allow", "", ran.exit_code))
            continue
        shown = ran.reason + (f": {ran.stderr[:200]}" if ran.stderr else "")
        result.warnings.append(sanitize_text(shown))
        result.reports.append(HookReport(hook, entry.index, "failed", ran.reason, ran.exit_code))
    return result


# -- Async -------------------------------------------------------------------------------------


def _start_async(
    gateway: Gateway,
    entry: HookEntry,
    payload: bytes,
    object_id: str,
    state_dir: str | None,
    pr: str | None,
) -> str | None:
    """Start the watchdog for one async hook; returns a problem text, or None when it started.

    The hook's private directory is made here and removed by the watchdog (or here, if the
    watchdog could not be started)."""
    try:
        dirs = gw.make_private_dirs(gateway.environ, config=False)
    except (OSError, ForgectlError) as exc:
        return f"no private directory ({type(exc).__name__}); not run"
    try:
        extra = {
            "FORGECTL_HOOK": entry.hook,
            "FORGECTL_ID": _sanitized(object_id),
            "FORGECTL_OBJECT_TYPE": OBJECT_TYPE,
        }
        spec = {
            "version": 1,
            "argv": [SHELL, "-c", "exec >/dev/null\n" + entry.cmd],
            "env": gateway.inherited_environment((), extra),
            "root": dirs.root,
            "cwd": dirs.cwd,
            "timeout": entry.timeout,
            "payload": payload.decode("ascii"),
            "hook": entry.hook,
            "index": entry.index,
            "state_dir": state_dir,
            "pr": pr,
        }
        data = json.dumps(spec, ensure_ascii=True, allow_nan=False).encode("ascii")
        watchdog_env = gw.build_environment(gateway.environ, dirs=dirs, backend=None)
        gateway.spawn_detached(
            [sys.executable, "-I", "-m", "forgectl.hookwatch"],
            env=watchdog_env,
            cwd="/",
            stdin=data,
        )
    except (OSError, ValueError, TimeoutError) as exc:
        gw.remove_tree(dirs.root)
        return f"the watchdog could not be started ({type(exc).__name__}); not run"
    except BaseException:
        gw.remove_tree(dirs.root)
        raise
    return None
