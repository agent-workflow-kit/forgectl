"""Append-only JSONL files in the state directory (interface: Files, Audit and log).

One record is one ASCII line written with a single ``write`` on an ``O_APPEND`` descriptor, so
concurrent processes never interleave lines. The state directory and the file are opened without
following a final symlink, and must be what they should be: a directory (or regular file) owned
by the caller, a directory nobody else can write, a file with one link. Nothing here changes the
mode of something that exists; creation is 0700 for the directory and 0600 for a file.

``log.jsonl`` (and only it) is rotated by size, see ``append_log``; ``audit.jsonl`` never is.
The only other size cap is per line.
"""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_LINE_BYTES = 64 * 1024  # a line, newline included; above it nothing is written

# The patterns of schemas/defs.json, audit.json and log.json, matched with ``fullmatch`` (so a
# trailing newline never passes). A test compares them with the schema files.
PATTERNS: dict[str, str] = {
    "host": r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?$",  # noqa: E501
    "pr": r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?(?:/(?!\.\.?(?:/|#))[A-Za-z0-9._-]+)+#[1-9][0-9]{0,9}$",  # noqa: E501
    "ts": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$",  # noqa: E501
    "id": r"^(?:gh:(?:ic|rc|rt|r):[A-Za-z0-9_=-]{1,100}|gl:d:[0-9a-f]{40}|gl:n:[1-9][0-9]{0,18})$",
    "actor": r"^(human|auto):[A-Za-z0-9._@-]{1,64}$",
    "decision_id": r"^[A-Za-z0-9._:@/-]{1,128}$",
    "sha": r"^[0-9a-f]{40}$",
    "sha256": r"^[0-9a-f]{64}$",
    "attempt_id": r"^[A-Za-z0-9_-]{8,64}$",
    "event": r"^[a-z][a-z0-9_]*$",
}
_COMPILED = {name: re.compile(pattern) for name, pattern in PATTERNS.items()}


def matches(name: str, value: object) -> bool:
    """Whether ``value`` is a string matching the named schema pattern in full."""
    return isinstance(value, str) and _COMPILED[name].fullmatch(value) is not None


def parse_ts(value: object) -> datetime | None:
    """An RFC 3339 ``ts`` as an aware datetime; None when it is not one (or not a real time)."""
    if not matches("ts", value):
        return None
    assert isinstance(value, str)
    try:
        return datetime.fromisoformat(value.upper())
    except ValueError:
        return None


def now_ts() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class JsonlError(OSError):
    """The file could not be appended to or read, for any reason (the message is fixed text)."""


def state_dir(env: Mapping[str, str]) -> Path:
    """``$XDG_STATE_HOME/forgectl`` when absolute, else ``~/.local/state/forgectl``."""
    xdg = env.get("XDG_STATE_HOME", "")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / "forgectl"
    home = env.get("HOME", "")
    if not (home and os.path.isabs(home)):
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
        except (KeyError, OSError):
            raise JsonlError("cannot locate the home directory") from None
    return Path(home) / ".local" / "state" / "forgectl"


def encode_line(record: Mapping[str, Any]) -> bytes:
    """The record as one ASCII line, checked against the line cap."""
    try:
        text = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise JsonlError("record is not JSON") from None
    data = text.encode("ascii") + b"\n"
    if len(data) > MAX_LINE_BYTES:
        raise JsonlError("record is over the line cap")
    return data


def _check_dir(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise JsonlError("state directory is not a directory of this user")
    if info.st_mode & 0o022:
        raise JsonlError("state directory is writable by group or others")


def _make_parents(path: Path) -> None:
    """Create the missing directories of ``path``, top down, each 0700.

    ``mkdir``'s mode is only ever narrowed by the umask, so a created directory is never more
    open than 0700 (pathlib's ``parents=True`` would give the parents umask-only modes).
    Directories that exist are left as they are.
    """
    missing: list[Path] = []
    while not os.path.lexists(path) and path != path.parent:
        missing.append(path)
        path = path.parent
    for each in reversed(missing):
        try:
            os.mkdir(each, 0o700)
        except FileExistsError:
            pass


def open_dir(directory: Path, *, create: bool) -> int | None:
    """A descriptor for the state directory; None when it is absent and ``create`` is false."""
    try:
        try:
            fd = os.open(directory, _DIR_FLAGS)
        except FileNotFoundError:
            if not create:
                return None
            _make_parents(directory.parent)
            try:
                os.mkdir(directory, 0o700)
            except FileExistsError:
                pass
            fd = os.open(directory, _DIR_FLAGS)
        try:
            _check_dir(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd
    except JsonlError:
        raise
    except OSError as err:
        raise JsonlError(f"state directory: {os.strerror(err.errno or 0)}") from None


def _check_file(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise JsonlError("not a regular file")
    if info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise JsonlError("file is not this user's single-link file")
    if info.st_mode & 0o022:
        raise JsonlError("file is writable by group or others")


def _open_append(directory: Path, name: str) -> int:
    dfd = open_dir(directory, create=True)
    assert dfd is not None
    flags = os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        try:
            # Read access too: append() checks whether the file ends in a newline.
            fd = os.open(name, os.O_RDWR | flags, 0o600, dir_fd=dfd)
        except PermissionError:
            # An existing write-only file: append without that check.
            fd = os.open(name, os.O_WRONLY | flags, 0o600, dir_fd=dfd)
    except OSError as err:
        raise JsonlError(f"{name}: {os.strerror(err.errno or 0)}") from None
    finally:
        os.close(dfd)
    try:
        _check_file(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def check_appendable(directory: Path, name: str) -> None:
    """Open (creating) the file for appending and close it, writing nothing."""
    os.close(_open_append(directory, name))


def append(directory: Path, name: str, record: Mapping[str, Any]) -> None:
    """Append ``record`` as one line; raises ``JsonlError`` (nothing partial that it knows of).

    If the file ends in an unterminated fragment (an earlier short write), a newline goes
    first, in the same single write, so this record is not glued onto it. Racing another
    writer, the worst case is an empty line, which ``export`` skips.
    """
    data = encode_line(record)
    fd = _open_append(directory, name)
    try:
        try:
            readable = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDWR
            size = os.fstat(fd).st_size
            if readable and size > 0 and os.pread(fd, 1, size - 1) != b"\n":
                data = b"\n" + data
            written = os.write(fd, data)
        except OSError as err:
            raise JsonlError(f"{name}: {os.strerror(err.errno or 0)}") from None
        if written != len(data):
            raise JsonlError(f"{name}: short write")
    finally:
        os.close(fd)


def open_read(directory: Path, name: str) -> int | None:
    """A read descriptor for the file; None when the directory or file is absent."""
    dfd = open_dir(directory, create=False)
    if dfd is None:
        return None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
    except FileNotFoundError:
        return None
    except OSError as err:
        raise JsonlError(f"{name}: {os.strerror(err.errno or 0)}") from None
    finally:
        os.close(dfd)
    try:
        _check_file(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


# --- log.jsonl ----------

LOG_FILE = "log.jsonl"
LEVELS = ("error", "warn", "info", "debug")
_MAX_STRING = 2048
_MAX_DEPTH = 6
# Userinfo runs to the last "@" before the authority ends ("/", "?" or "#"), as urlsplit has it.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/?#]*@")
# GitHub token prefixes, and GitLab's documented token prefixes, whose routable form has
# "."-separated parts. A GitLab prefix must not follow a letter or digit, so ordinary words
# such as "english-translations" do not match.
_GITLAB_PREFIXES = "glpat|gloas|glrt|gldt|glptt|glft|glimt|glcbt|glsoat|glagent|glwt|glffct|gltst"
_TOKEN_SHAPES = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}"
    rf"|(?<![A-Za-z0-9])(?:{_GITLAB_PREFIXES})-[A-Za-z0-9_.-]{{8,}})"
)


def scrub_text(text: str) -> str:
    """Drop userinfo from URLs and mask token-shaped words; cap the length."""
    # TODO(#8): redact the exact token values in use, not only their shapes.
    text = _URL_USERINFO.sub(r"\1", text)
    text = _TOKEN_SHAPES.sub("<redacted>", text)
    return text if len(text) <= _MAX_STRING else text[:_MAX_STRING] + "..."


def scrub_details(value: Any, depth: int = 0) -> Any:
    """``details`` for the log: JSON-safe, no URL userinfo, no token shapes, bounded."""
    if isinstance(value, str):
        return scrub_text(value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if depth >= _MAX_DEPTH:
        return "<too deep>"
    if isinstance(value, Mapping):
        return {
            scrub_text(str(k)): scrub_details(v, depth + 1) for k, v in list(value.items())[:64]
        }
    if isinstance(value, list | tuple):
        return [scrub_details(v, depth + 1) for v in list(value)[:64]]
    return "<unserializable>"


def log_problem(record: object) -> str | None:
    """Why ``record`` is not a valid ``log.json`` record, or None."""
    keys = {"ts", "level", "event", "pr", "host", "details"}
    if not isinstance(record, dict) or set(record) != keys:
        return "not a log record"
    if parse_ts(record["ts"]) is None:
        return "ts"
    if record["level"] not in LEVELS:
        return "level"
    if not matches("event", record["event"]):
        return "event"
    if record["pr"] is not None and not matches("pr", record["pr"]):
        return "pr"
    if record["host"] is not None and not matches("host", record["host"]):
        return "host"
    if record["details"] is not None and not isinstance(record["details"], dict):
        return "details"
    return None


# --- rotation of log.jsonl ----------

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_KEEP = 3
MIN_MAX_BYTES = 64 * 1024  # an empty file always holds one full line
MAX_MAX_BYTES = 1024 * 1024 * 1024
MAX_KEEP = 100
LOCK_FILE = "log.lock"
LOCK_WAIT_SECONDS = 1.0
_ROTATED = re.compile(r"log\.jsonl\.([1-9][0-9]*)")


@dataclass(frozen=True)
class Rotation:
    """When ``log.jsonl`` rotates (config ``log.max_bytes``) and how many rotated files stay."""

    max_bytes: int = DEFAULT_MAX_BYTES
    keep: int = DEFAULT_KEEP

    def __post_init__(self) -> None:
        for value in (self.max_bytes, self.keep):
            if type(value) is not int:
                raise ValueError("rotation limits are integers")
        if not MIN_MAX_BYTES <= self.max_bytes <= MAX_MAX_BYTES:
            raise ValueError("max_bytes out of range")
        if not 1 <= self.keep <= MAX_KEEP:
            raise ValueError("keep out of range")


_rotation = Rotation()


def set_rotation(value: Rotation) -> None:
    """The limits this process uses for every ``log_event`` that is not given its own."""
    global _rotation
    _rotation = value


def rotation() -> Rotation:
    return _rotation


def _needs_rotation(dfd: int, incoming: int, limits: Rotation) -> bool:
    try:
        info = os.stat(LOG_FILE, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_size > 0
        and (info.st_size + incoming > limits.max_bytes)
    )


def _lock(dfd: int) -> int | None:
    """The exclusive lock on ``log.lock``; None when another process holds it for too long."""
    try:
        fd = os.open(
            LOCK_FILE,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
            dir_fd=dfd,
        )
    except OSError as err:
        raise JsonlError(f"{LOCK_FILE}: {os.strerror(err.errno or 0)}") from None
    try:
        _check_file(fd)
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return None
                time.sleep(0.01)
    except BaseException:
        os.close(fd)
        raise


def _shift(dfd: int, keep: int) -> None:
    """``log.jsonl.N`` becomes ``.N+1`` (the one past ``keep`` is replaced), then ``log.jsonl``
    becomes ``.1``. A rename never follows a link, and a missing file (another process was
    first) is skipped."""
    for name in os.listdir(dfd):
        found = _ROTATED.fullmatch(name)
        if found and int(found.group(1)) > keep:
            try:
                os.unlink(name, dir_fd=dfd)
            except FileNotFoundError:
                pass
    steps = [(f"{LOG_FILE}.{n}", f"{LOG_FILE}.{n + 1}") for n in range(keep - 1, 0, -1)]
    for src, dst in [*steps, (LOG_FILE, f"{LOG_FILE}.1")]:
        try:
            os.rename(src, dst, src_dir_fd=dfd, dst_dir_fd=dfd)
        except FileNotFoundError:
            pass


def rotate_log(directory: Path, incoming: int, limits: Rotation) -> None:
    """Rotate ``log.jsonl`` when ``incoming`` more bytes would take it over the limit.

    Only ever ``log.jsonl``: ``audit.jsonl`` is the audit trail and is never rotated. Processes
    rotating at once take turns under ``flock`` on ``log.lock`` and re-check the size once they
    hold it, so only the first rotates. A writer that already has the old file open keeps
    appending to it (now ``log.jsonl.1``), so no line is lost; one that opens the file next
    creates a fresh one. Raises ``JsonlError``; nothing is created when the directory is absent.
    """
    dfd = open_dir(directory, create=False)
    if dfd is None:
        return
    try:
        if not _needs_rotation(dfd, incoming, limits):
            return
        lock = _lock(dfd)
        if lock is None:
            return  # another process is rotating; this record goes to whichever file is current
        try:
            if _needs_rotation(dfd, incoming, limits):
                _shift(dfd, limits.keep)
        finally:
            os.close(lock)
    except OSError as err:
        if isinstance(err, JsonlError):
            raise
        raise JsonlError(f"{LOG_FILE}: {os.strerror(err.errno or 0)}") from None
    finally:
        os.close(dfd)


def append_log(
    directory: Path, record: Mapping[str, Any], limits: Rotation | None = None
) -> str | None:
    """Append one ``log.jsonl`` line, rotating first when it would not fit.

    A failed rotation does not stop the append; it is returned as a warning text. A failed
    append raises ``JsonlError``.
    """
    data_len = len(encode_line(record))
    note: str | None = None
    try:
        rotate_log(directory, data_len, limits or _rotation)
    except JsonlError as err:
        note = f"log not rotated: {err}"
    append(directory, LOG_FILE, record)
    return note


def log_event(
    directory: Path,
    level: str,
    event: str,
    *,
    pr: str | None = None,
    host: str | None = None,
    details: Mapping[str, Any] | None = None,
    rotation: Rotation | None = None,
) -> str | None:
    """Append one log record. Returns a warning text when it could not be written (or rotated).

    A log failure never raises: it must not change a command's result. ``pr`` and ``host`` that
    do not match the schema are recorded as null rather than as raw text.
    """
    record: dict[str, Any] = {
        "ts": now_ts(),
        "level": level,
        "event": event,
        "pr": pr if matches("pr", pr) else None,
        "host": host if matches("host", host) else None,
        "details": None if details is None else scrub_details(details),
    }
    problem = log_problem(record)
    if problem is not None:
        return f"log record not written: invalid {problem}"
    try:
        try:
            return append_log(directory, record, rotation)
        except JsonlError as err:
            if "line cap" not in str(err):
                raise
            record["details"] = {"truncated": True}
            return append_log(directory, record, rotation)
    except JsonlError as err:
        return f"log not written: {err}"
