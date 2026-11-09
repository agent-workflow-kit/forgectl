"""The watchdog of an ``async`` post hook (interface: Hooks, Delivery).

forgectl starts ``python -I -m forgectl.hookwatch`` detached (its own session) and hands it one
JSON spec on stdin: the hook's argv, environment, private directory, timeout and payload. The
watchdog runs the hook as the leader of its own process group, enforces the timeout (SIGTERM to
the group, SIGKILL 2 s later, only while the leader is unreaped: ``gateway.run_process``), then
removes the hook's private directory. A hook failure is logged as ``hook_failed`` when the spec
names a state directory. SIGTERM, SIGINT and SIGHUP end the hook's group and the directory is
still removed. Nothing is written to stdout or stderr.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from . import gateway as gw
from . import jsonlog

MAX_SPEC = 4 * 1024 * 1024
MAX_WATCH_STDERR = 1024


def _terminate(signum: int, _frame: Any) -> None:
    raise SystemExit(128 + signum)


def _valid(spec: Any) -> bool:
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
        and 0 < spec["timeout"] < 86400 * 7
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


def run(spec: dict[str, Any]) -> int:
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
        if result.timed_out:
            outcome = "timed out"
        elif result.returncode != 0:
            outcome = f"exited {result.returncode}"
    except SystemExit:
        outcome = "stopped by a signal"
        raise
    finally:
        held = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
        )
        try:
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
    return 0


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
    if not _valid(spec):
        return 2
    return run(spec)


if __name__ == "__main__":
    sys.exit(main())
