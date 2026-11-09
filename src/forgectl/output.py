"""Printing results and errors (interface: Output by exit code, Dry-run)."""

from __future__ import annotations

import json
import os
import sys
from argparse import Namespace
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, TextIO

from .errors import ForgectlError, sanitize_tree
from .sanitize import sanitize_line, sanitize_text

PARTIAL_RESULT_COMMANDS = frozenset({"prs", "whoami"})
POST_COMMANDS = frozenset({"review", "comment", "reply", "approve"})


def dumps(data: Any) -> str:
    """JSON text that is plain ASCII, so a lone surrogate can never fail on output."""
    return json.dumps(data, ensure_ascii=True)


def format_human(data: dict[str, Any]) -> str:
    """`key: value` lines, never raw: every string passes through the sanitizer.

    One line per field: a tab or LF inside a key or a value becomes a marker, so untrusted text
    cannot forge a line of its own.
    """
    lines: list[str] = []
    for key, value in data.items():
        text = value if isinstance(value, str) else dumps(sanitize_tree(value))
        lines.append(f"{sanitize_line(str(key))}: {sanitize_line(text)}")
    return "\n".join(lines)


def error_text(err: ForgectlError) -> str:
    """The one stderr line for an error; the message is untrusted and stays on that line."""
    label = err.code if err.reason is None else f"{err.code} ({err.reason})"
    return f"forgectl: error: {label}: {sanitize_line(err.message)}"


def would_refuse_result(command: str, args: Namespace, err: ForgectlError) -> dict[str, Any]:
    """The `--dry-run` result for an exit 20: the command's output with `would_refuse` set.

    Fields the refusal came too early to know are null. A handler fills in what it did read
    through ``err.plan`` (the plan) and ``err.fields`` (the other output fields).
    """
    # `pr` and the thread id are parsed values; their text is the normalized form. It is
    # sanitized anyway, like every other echoed input.
    pr = sanitize_line(str(args.pr))
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
            "thread_id": sanitize_line(str(args.thread)),
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
                "expected_base": None
                if args.expected_base is None
                else sanitize_line(args.expected_base),
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
    if err.plan and "plan" in result:  # resolve and unresolve report `would_refuse` only
        result["plan"].update(sanitize_tree(err.plan))
    if err.fields:
        result.update(sanitize_tree(err.fields))
    return result


def _discard(stream: TextIO) -> None:
    """Point a failed stream's descriptor at /dev/null.

    Whatever the stream still buffers then flushes there at interpreter shutdown, so a failed
    stdout or stderr cannot turn the exit code into 120.
    """
    try:
        fd = stream.fileno()
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, fd)
        finally:
            os.close(devnull)
    except (OSError, ValueError, AttributeError):  # no descriptor behind it, or already closed
        pass


def write_line(stream: TextIO | None, text: str) -> bool:
    """Write ``text`` and a newline, flushed; False when the stream is missing or failed.

    A failure (closed or absent stream, broken pipe, full disk) never raises: the exit code is
    already decided and an output problem must not change it.
    """
    if stream is None:
        return False
    try:
        stream.write(text + "\n")
        stream.flush()
    except (OSError, ValueError):
        _discard(stream)
        return False
    return True


@dataclass
class RecordStream:
    """A result that is a sequence of records (``export``): a JSON line each, or with ``--json``
    one array. Read lazily, so a large file is never held in memory."""

    records: Iterable[dict[str, Any]]


def warn(text: str) -> None:
    """One ``forgectl: warning:`` line on stderr; never raises."""
    write_line(sys.stderr, f"forgectl: warning: {sanitize_line(text)}")


def _write_stream(stream: TextIO | None, records: Iterable[dict[str, Any]], *, array: bool) -> None:
    if stream is None:
        return

    def put(text: str) -> bool:
        # Only the writes are guarded: an error from producing the records is not a broken pipe.
        try:
            stream.write(text)
            if not array:
                stream.flush()
        except (OSError, ValueError):
            _discard(stream)
            return False
        return True

    if array and not put("["):
        return
    for index, record in enumerate(records):
        if not put(("," if array and index else "") + dumps(record) + ("" if array else "\n")):
            return
    if array:
        put("]\n")
    try:
        stream.flush()
    except (OSError, ValueError):
        _discard(stream)


def emit_success(data: dict[str, Any] | RecordStream, *, json_mode: bool) -> None:
    if isinstance(data, RecordStream):
        _write_stream(sys.stdout, data.records, array=json_mode)
        return
    write_line(sys.stdout, dumps(data) if json_mode else format_human(data))


def emit_error(
    err: ForgectlError, *, command: str | None, args: Namespace | None, json_mode: bool
) -> int:
    """Print what the exit code calls for and return the exit code.

    Output by exit code: exit 12 `partial_results` from `prs` or `whoami` prints the command's
    result, exit 20 from a `--dry-run` prints the plan with `would_refuse`, and everything else
    prints the error envelope. The error text always goes to stderr, and only there: if stderr
    is unusable it is dropped, never moved to stdout.
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
        write_line(sys.stdout, dumps(err.envelope()))
    write_line(sys.stderr, error_text(err))
    return err.exit_code
