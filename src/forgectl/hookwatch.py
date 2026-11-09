"""The watchdog of an ``async`` post hook (interface: Hooks, Delivery).

forgectl starts the watchdog detached (its own session) as ``python -I -c <boot> <dir>``, where
``<dir>`` holds the running forgectl package (``hooks.watchdog_argv``), and hands it one JSON spec
on stdin: the hook's argv, environment, private directory, timeout and payload. Once the spec is
valid and its directory is forgectl's, the watchdog acknowledges it with one byte on stdout (which
forgectl waits for, then reads no more) and points stdout at /dev/null. It runs the hook as the
leader of its own process group, enforces the timeout (SIGTERM to the group, SIGKILL 2 s later,
only while the leader is unreaped: ``gateway.run_process``), then removes the hook's private
directory. A hook failure, and a spec refused after the directory check, is logged as
``hook_failed`` when the spec names a state directory. SIGTERM, SIGINT and SIGHUP end the hook's
group and the directory is still removed. Nothing else is written to stdout or stderr.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import gateway as gw
from . import jsonlog
from .config import MAX_DURATION

MAX_SPEC = 4 * 1024 * 1024
MAX_WATCH_STDERR = 1024


def _terminate(signum: int, _frame: Any) -> None:
    raise SystemExit(128 + signum)


def valid_spec(spec: Any) -> bool:
    """Whether ``spec`` is one the watchdog runs; forgectl checks it before spawning, too. The
    timeout bound is the config's own maximum."""
    if not isinstance(spec, dict):
        return False
    argv = spec.get("argv")
    env = spec.get("env")
    return (
        isinstance(argv, list)
        and bool(argv)
        and all(isinstance(a, str) and "\0" not in a for a in argv)
        and isinstance(env, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
        and isinstance(spec.get("root"), str)
        and isinstance(spec.get("cwd"), str)
        and isinstance(spec.get("payload"), str)
        and isinstance(spec.get("timeout"), int | float)
        and not isinstance(spec.get("timeout"), bool)
        and 0 < spec["timeout"] <= MAX_DURATION
    )


def _own_private_dir(root: str, cwd: str) -> bool:
    """Only a directory forgectl made (``forgectl-*``, a real directory, ours) is ever removed."""
    if not os.path.isabs(root) or os.path.islink(root):
        return False
    if not os.path.basename(root).startswith("forgectl-") or Path(cwd).parent != Path(root):
        return False
    try:
        info = os.lstat(root)
    except OSError:
        return False
    return os.path.isdir(root) and info.st_uid == os.getuid()


def _finish(spec: dict[str, Any], outcome: str) -> None:
    """Remove the private directory (re-checked just before) and log ``outcome`` if any, with
    SIGINT, SIGTERM and SIGHUP held."""
    root, cwd = spec["root"], spec["cwd"]
    held = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    try:
        if _own_private_dir(root, cwd):
            gw.remove_tree(root)
        if outcome and isinstance(spec.get("state_dir"), str):
            jsonlog.log_event(
                Path(spec["state_dir"]),
                "warn",
                "hook_failed",
                pr=spec.get("pr") if isinstance(spec.get("pr"), str) else None,
                details={"hook": spec.get("hook"), "index": spec.get("index"), "why": outcome},
            )
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, held)


def _outcome(result: gw.ProcessResult) -> str:
    if result.timed_out:
        return "timed out"
    if result.streams_incomplete:
        return "left its output open"
    if result.returncode < 0:
        return f"killed by signal {-result.returncode}"
    if result.returncode != 0:
        return f"exited {result.returncode}"
    return ""


def run(spec: dict[str, Any]) -> int:
    """Run a valid spec's hook and clean up after it; 2 if the directory is not forgectl's."""
    root, cwd = spec["root"], spec["cwd"]
    if not _own_private_dir(root, cwd):
        return 2
    outcome = ""
    try:
        result = gw.run_process(
            spec["argv"],
            env=spec["env"],
            cwd=cwd,
            stdin=spec["payload"].encode("ascii", errors="replace"),
            timeout=float(spec["timeout"]),
            max_stdout=65536,
            max_stderr=MAX_WATCH_STDERR,
        )
        outcome = _outcome(result)
    except SystemExit:
        outcome = "stopped by a signal"
        raise
    except Exception:
        outcome = "could not be started"
    finally:
        _finish(spec, outcome)
    return 0


def start(spec: Any, ack: Callable[[], None]) -> int:
    """Check ``spec``, acknowledge it and run it. Once its directory is known to be forgectl's,
    a refused spec still has the directory removed and is logged; nothing is acknowledged."""
    if not isinstance(spec, dict):
        return 2
    root, cwd = spec.get("root"), spec.get("cwd")
    if not (isinstance(root, str) and isinstance(cwd, str) and _own_private_dir(root, cwd)):
        return 2
    if not valid_spec(spec):
        _finish(spec, "the watchdog refused its spec")
        return 2
    ack()
    return run(spec)


def _acknowledge() -> None:
    """One byte on stdout for forgectl, then stdout is /dev/null."""
    with contextlib.suppress(OSError):
        os.write(1, b"1")
    null = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null, 1)
    finally:
        os.close(null)


def main() -> int:
    for name in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(name, _terminate)
    raw = sys.stdin.buffer.read(MAX_SPEC + 1)
    if len(raw) > MAX_SPEC:
        return 2
    try:
        spec = json.loads(raw)
    except ValueError:
        return 2
    return start(spec, _acknowledge)


if __name__ == "__main__":
    sys.exit(main())
