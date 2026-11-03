"""``export [--since TIME]``: the audit records, as JSONL or (``--json``) an array.

The file is untrusted input: another process or user may have appended to it. Each line is read
with a cap, parsed strictly, checked against the audit schema and sanitized; a line that fails
is skipped with a warning that never repeats its content. A missing file is empty output.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO

from . import audit, jsonlog
from .errors import ForgectlError
from .jsonlog import JsonlError
from .output import RecordStream, warn
from .registry import Invocation, register
from .surface import duration

MAX_READ_LINE = 4 * jsonlog.MAX_LINE_BYTES  # longer lines were not written by forgectl
MAX_WARNINGS = 20


def since_cutoff(value: str, now: datetime) -> datetime:
    """The ``--since`` value as a time: RFC 3339 with an offset, else a duration back from now."""
    try:
        parsed = datetime.fromisoformat(value.upper())
    except ValueError:
        parsed = None
    if parsed is not None and parsed.tzinfo is not None:
        return parsed
    return now - timedelta(seconds=duration(value))


def _no_constant(name: str) -> Any:
    raise ValueError(f"constant {name}")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def parse_line(raw: bytes) -> dict[str, Any] | None:
    """The audit record on a line, or None when it is not a valid one."""
    try:
        record = json.loads(
            raw.decode("utf-8"), parse_constant=_no_constant, object_pairs_hook=_no_duplicates
        )
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return None
    if audit.audit_problem(record) is not None:
        return None
    return record  # type: ignore[no-any-return]


def _lines(stream: BinaryIO) -> Iterator[tuple[bytes | None, bool]]:
    """``(line, torn)``: a line without its newline, None when it was over the cap."""
    while True:
        chunk = stream.readline(MAX_READ_LINE + 1)
        if not chunk:
            return
        if chunk.endswith(b"\n"):
            yield chunk[:-1], False
            continue
        if len(chunk) > MAX_READ_LINE:  # over the cap: drop through the next newline
            while chunk and not chunk.endswith(b"\n"):
                chunk = stream.readline(MAX_READ_LINE)
            yield None, False
            continue
        yield chunk, True  # the last line, with no newline: possibly a torn write


def iter_records(
    fd: int, since: datetime | None, skipped: Callable[[str], None]
) -> Iterator[dict[str, Any]]:
    """Valid, sanitized records at or after ``since``, in file order."""
    count = 0

    def skip(why: str) -> None:
        nonlocal count
        count += 1
        if count <= MAX_WARNINGS:
            skipped(why)

    with os.fdopen(fd, "rb") as stream:
        number = 0
        try:
            for line, torn in _lines(stream):
                number += 1
                if line is not None and not line.strip():
                    continue
                record = None if line is None or torn else parse_line(line)
                if record is None:
                    skip(f"audit.jsonl line {number} skipped: malformed")
                    continue
                if since is not None:
                    ts = jsonlog.parse_ts(record["ts"])
                    if ts is None or ts < since:
                        continue
                yield audit.sanitized(record)
        except OSError:
            skip("audit.jsonl could not be read to the end")
    if count > MAX_WARNINGS:
        skipped(f"{count - MAX_WARNINGS} more lines skipped")


@register("export")
def export(inv: Invocation) -> RecordStream:
    since_text: str | None = inv.args.since
    since = None if since_text is None else since_cutoff(since_text, datetime.now(UTC))
    try:
        fd = jsonlog.open_read(jsonlog.state_dir(os.environ), audit.AUDIT_FILE)
    except JsonlError as err:
        raise ForgectlError("audit_unavailable", f"audit.jsonl cannot be read: {err}") from None
    if fd is None:
        return RecordStream(iter(()))
    return RecordStream(iter_records(fd, since, warn))


__all__ = ["export", "iter_records", "parse_line", "since_cutoff"]
