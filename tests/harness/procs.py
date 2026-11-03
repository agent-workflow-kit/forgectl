"""Process hygiene for tests: every child is recorded at launch and torn down by exact id.

Children start in their own session, so the process group id equals the child's PID and no other
process shares it. Teardown signals only that PID or group, and only while the leader has not
been reaped (an unreaped leader keeps its PID and group id from being reused). Nothing here
matches processes by name or pattern.
"""

from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Launched:
    proc: subprocess.Popen[Any]
    pid: int
    pgid: int
    pidfd: int | None
    forced: bool = False  # needed SIGKILL at teardown


@dataclass
class ProcessRegistry:
    launched: list[Launched] = field(default_factory=list)

    def popen(self, argv: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        """Start ``argv`` in a new session and record its PID and process group."""
        kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **kwargs)
        pidfd: int | None = None
        if hasattr(os, "pidfd_open"):
            with contextlib.suppress(OSError):
                pidfd = os.pidfd_open(proc.pid)
        # start_new_session makes the child a session and group leader: pgid == pid.
        self.launched.append(Launched(proc, proc.pid, proc.pid, pidfd))
        return proc

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        timeout: float = 60,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        """Like ``subprocess.run`` with text capture, but the child is tracked and cleaned up."""
        kwargs.setdefault("stdin", subprocess.PIPE if input is not None else subprocess.DEVNULL)
        proc = self.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        data = None if input is None else input.encode()
        try:
            out, err = proc.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate(self._record(proc))
            proc.communicate()
            raise
        # Decoded by hand: text mode would turn the CRLF of `-i` headers into LF.
        return subprocess.CompletedProcess(
            argv, proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
        )

    def _record(self, proc: subprocess.Popen[Any]) -> Launched:
        for rec in self.launched:
            if rec.proc is proc:
                return rec
        raise ValueError("process was not started through this registry")

    def alive(self, proc: subprocess.Popen[Any]) -> bool:
        return proc.poll() is None

    def signal(self, proc: subprocess.Popen[Any], sig: int) -> bool:
        """Signal exactly this child, if it has not been reaped. Returns whether it was sent."""
        rec = self._record(proc)
        if proc.returncode is not None:
            return False
        if rec.pidfd is not None:
            signal.pidfd_send_signal(rec.pidfd, sig)  # type: ignore[attr-defined]
        else:
            os.kill(rec.pid, sig)
        return True

    def _signal_group(self, rec: Launched, sig: int) -> None:
        if rec.proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            if os.getpgid(rec.pid) == rec.pgid:
                os.killpg(rec.pgid, sig)

    def _exited(self, rec: Launched, timeout: float) -> bool:
        """Wait for the leader to exit without reaping it."""
        if rec.pidfd is not None:
            ready, _, _ = select.select([rec.pidfd], [], [], timeout)
            return bool(ready)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if rec.proc.poll() is not None:
                return True
            time.sleep(0.02)
        return False

    def _terminate(self, rec: Launched) -> None:
        if rec.proc.returncode is None:
            self._signal_group(rec, signal.SIGTERM)
            if not self._exited(rec, 2.0):
                rec.forced = True
            # Sweep stragglers in the group before the leader is reaped and the id is free.
            self._signal_group(rec, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                rec.proc.wait(timeout=5)
        for stream in (rec.proc.stdin, rec.proc.stdout, rec.proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        if rec.pidfd is not None:
            with contextlib.suppress(OSError):
                os.close(rec.pidfd)
            rec.pidfd = None

    def terminate_all(self) -> list[Launched]:
        """Teardown: stop every recorded child that is still running. Returns the forced ones."""
        for rec in reversed(self.launched):
            self._terminate(rec)
        return [rec for rec in self.launched if rec.forced]
