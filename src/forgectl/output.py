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

PARTIAL_RESULT_COMMANDS = frozenset({"prs", "poll", "whoami"})
POST_COMMANDS = frozenset({"review", "comment", "reply", "approve"})


def dumps(data: Any) -> str:
    """JSON text that is plain ASCII, so a lone surrogate can never fail on output."""
    return json.dumps(data, ensure_ascii=True)


# The multi-line text fields of a command's human output, printed as indented blocks.
# `compare`'s diff and `show`'s body; every other field, and every other command, keeps the
# one-line form. `diff` prints its diff alone and `export` prints records (see emit_success).
HUMAN_TEXT_FIELDS: dict[str, frozenset[str]] = {
    "compare": frozenset({"diff"}),
    "show": frozenset({"body"}),
}
BLOCK_INDENT = "    "


def text_block(text: str) -> list[str]:
    """A text field as indented lines: one per line of ``text``, each sanitized on its own.

    Every line, an empty one too, carries the indent, so a line of the text can never start a
    line of the output at the left margin and forge a ``key: value`` line. A CR the diff kept
    before its LF is a visible marker here. The last LF ends its line and starts no new one, so
    the block has the text's line count.
    """
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [BLOCK_INDENT + sanitize_text(line) for line in lines]


def format_human(data: dict[str, Any], text_fields: frozenset[str] = frozenset()) -> str:
    """`key: value` lines, never raw: every string passes through the sanitizer.

    One line per field: a tab or LF inside a key or a value becomes a marker, so untrusted text
    cannot forge a line of its own. The exception is a key in ``text_fields`` whose value is a
    non-empty string: ``key:`` and then its lines as an indented block (``text_block``). Null
    and the empty string stay on the key's line.
    """
    lines: list[str] = []
    for key, value in data.items():
        name = sanitize_line(str(key))
        if key in text_fields and isinstance(value, str) and value:
            lines.append(f"{name}:")
            lines.extend(text_block(value))
            continue
        text = value if isinstance(value, str) else dumps(sanitize_tree(value))
        lines.append(f"{name}: {sanitize_line(text)}")
    return "\n".join(lines)


def error_text(err: ForgectlError) -> str:
    """The one stderr line for an error; the message is untrusted and stays on that line."""
    label = err.code if err.reason is None else f"{err.code} ({err.reason})"
    return f"forgectl: error: {label}: {sanitize_line(err.message)}"


def warning_text(message: str) -> str:
    """The one stderr line for a warning, in the form of the error line."""
    return f"forgectl: warning: {sanitize_line(message)}"


def emit_warning(message: str) -> None:
    """Write a warning line to stderr; a failed stream never raises."""
    # TODO(#9): also write a `warn` line to the log (jsonlog.py)
    write_line(sys.stderr, warning_text(message))


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


def write_text(stream: TextIO | None, text: str) -> bool:
    """Write ``text`` as it is, flushed; False when the stream is missing or failed.

    A failure (closed or absent stream, broken pipe, full disk) never raises: the exit code is
    already decided and an output problem must not change it.
    """
    if stream is None:
        return False
    try:
        stream.write(text)
        stream.flush()
    except (OSError, ValueError):
        _discard(stream)
        return False
    return True


def write_line(stream: TextIO | None, text: str) -> bool:
    """Write ``text`` and a newline, as ``write_text`` does."""
    return write_text(stream, text + "\n")


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


def emit_success(
    data: dict[str, Any] | RecordStream, *, json_mode: bool, command: str | None = None
) -> None:
    """The result on stdout. Human `diff` prints the unified diff alone, as the backend
    sanitized it (interface: `diff`; Sanitizing): LF, tab and a CR before LF stay raw, so its
    lines are the diff's own. `compare` and `show` print their text field as an indented block
    and `export` prints its record lines, or one array with --json (interface: Human output)."""
    if isinstance(data, RecordStream):
        _write_stream(sys.stdout, data.records, array=json_mode)
        return
    if command == "diff" and not json_mode:
        write_text(sys.stdout, data["diff"])
        return
    if json_mode:
        write_line(sys.stdout, dumps(data))
    else:
        text_fields = HUMAN_TEXT_FIELDS.get(command or "", frozenset())
        write_line(sys.stdout, format_human(data, text_fields))


def emit_error(
    err: ForgectlError, *, command: str | None, args: Namespace | None, json_mode: bool
) -> int:
    """Print what the exit code calls for and return the exit code.

    Output by exit code: exit 12 `partial_results` from `prs`, `poll` or `whoami` prints the
    command's result, exit 20 from a `--dry-run` prints the plan with `would_refuse`, and
    everything else prints the error envelope. The error text always goes to stderr, and only
    there: if stderr is unusable it is dropped, never moved to stdout.
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
