"""Printing results and errors (interface: Output by exit code, Dry-run)."""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from typing import Any

from .errors import ForgectlError, sanitize_tree
from .textsafe import sanitize_text

PARTIAL_RESULT_COMMANDS = frozenset({"prs", "whoami"})
POST_COMMANDS = frozenset({"review", "comment", "reply", "approve"})


def dumps(data: Any) -> str:
    """JSON text that is plain ASCII, so a lone surrogate can never fail on output."""
    return json.dumps(data, ensure_ascii=True)


def format_human(data: dict[str, Any]) -> str:
    """`key: value` lines, never raw: every string passes through the sanitizer."""
    lines: list[str] = []
    for key, value in data.items():
        text = value if isinstance(value, str) else dumps(sanitize_tree(value))
        lines.append(f"{sanitize_text(str(key))}: {sanitize_text(text)}")
    return "\n".join(lines)


def error_text(err: ForgectlError) -> str:
    label = err.code if err.reason is None else f"{err.code} ({err.reason})"
    return f"forgectl: error: {label}: {sanitize_text(err.message)}"


def would_refuse_result(command: str, args: Namespace, err: ForgectlError) -> dict[str, Any]:
    """The `--dry-run` result for an exit 20: the command's output with `would_refuse` set.

    Fields the refusal came too early to know are null. A handler fills in what it did read
    through ``err.plan`` (the plan) and ``err.fields`` (the other output fields).
    """
    pr = args.pr  # TODO(#5): the normalized `host/path#N` form.
    refusal = {
        "reason": err.reason,
        "message": sanitize_text(err.message),
        "details": sanitize_tree(err.details) if err.details else None,
    }
    result: dict[str, Any]
    if command in ("resolve", "unresolve"):
        result = {
            "pr": pr,
            "action": command,
            "thread_id": args.thread,
            "resolved": None,
            "dry_run": True,
            "would_refuse": refusal,
        }
    elif command == "merge":
        result = {
            "pr": pr,
            "merged": False,
            "already_merged": False,
            "auto_merge_enabled": False,
            "merged_head_sha": None,
            "merge_commit_sha": None,
            "method": None,
            "dry_run": True,
            "would_refuse": refusal,
            "plan": {
                "head_sha": None,
                "expected_head": args.expected_head,
                "expected_base": args.expected_base,
                "base_ref": None,
                "auto": args.auto,
                "override_blocks": args.override_blocks,
                "blocked_by": None,
                "method": args.method,
            },
        }
    else:
        result = {
            "pr": pr,
            "action": command,
            "ids": [],
            "thread_ids": [],
            "review_id": None,
            "already_posted": False,
            "follow_up_done": False,
            "neutralized_lines": 0,
            "markers_removed": 0,
            "warnings": [],
            "head_sha": None,
            "dry_run": True,
            "would_refuse": refusal,
            "plan": {"event": None, "body_sha256": None, "body_bytes": 0, "inline_comments": 0},
        }
        if command in ("review", "approve"):
            result["head_verified"] = None
        if command == "reply":
            result["threaded"] = False
    if err.plan:
        result["plan"].update(err.plan)
    if err.fields:
        result.update(err.fields)
    return result


def emit_success(data: dict[str, Any], *, json_mode: bool) -> None:
    print(dumps(data) if json_mode else format_human(data))


def emit_error(
    err: ForgectlError, *, command: str | None, args: Namespace | None, json_mode: bool
) -> int:
    """Print what the exit code calls for and return the exit code.

    Output by exit code: exit 12 `partial_results` from `prs` or `whoami` prints the command's
    result, exit 20 from a `--dry-run` prints the plan with `would_refuse`, and everything else
    prints the error envelope. The error text always goes to stderr.
    """
    printable: dict[str, Any] | None = None
    if err.code == "partial_results" and command in PARTIAL_RESULT_COMMANDS:
        printable = err.output
    elif (
        err.exit_code == 20
        and command is not None
        and args is not None
        and getattr(args, "dry_run", False)
    ):
        printable = would_refuse_result(command, args, err)
    if printable is not None:
        emit_success(printable, json_mode=json_mode)
    elif json_mode:
        print(dumps(err.envelope()))
    print(error_text(err), file=sys.stderr)
    return err.exit_code
