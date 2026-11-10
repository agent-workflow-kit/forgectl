"""Process hygiene for tests: every child is recorded at launch and torn down by exact id.

Children start in their own session, so the process group id equals the child's PID and no other
process shares it. A leader is waited for without being reaped (pidfd poll, else
``waitid(WNOWAIT)``), so while the sweep runs its PID and group id cannot be reused. Each signal
first confirms, through ``waitid(WNOWAIT)``, that the leader is still our unreaped child. The
sweep SIGKILLs whatever is left in the group, records it as a survivor and reaps every member
that is our child (the session is a child subreaper, see ``fixtures.py``). Nothing here matches
processes by name or pattern.
"""

from __future__ import annotations

import contextlib
import os
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

WAIT_FLAGS = os.WEXITED | os.WNOHANG | os.WNOWAIT


@dataclass
class Launched:
    proc: subprocess.Popen[Any]
    pid: int
    pgid: int
    pidfd: int | None
    forced: bool = False  # needed SIGKILL at teardown
    survivors: list[int] = field(default_factory=list)  # group members alive after the leader
    readers: list[threading.Thread] = field(default_factory=list)  # run()'s pipe readers


class Completed(subprocess.CompletedProcess[Any]):
    """``CompletedProcess`` that also carries the raw bytes of both streams."""

    stdout_bytes: bytes
    stderr_bytes: bytes


def group_members(pgid: int) -> list[tuple[int, int, str]]:
    """(pid, ppid, state) of every process in group ``pgid``, from ``/proc`` (Linux only)."""
    members: list[tuple[int, int, str]] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return members
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fields = raw.rpartition(")")[2].split()
        if len(fields) > 2 and int(fields[2]) == pgid:
            members.append((int(entry.name), int(fields[1]), fields[0]))
    return members


def _feed(stream: IO[bytes], data: bytes) -> None:
    with contextlib.suppress(OSError, ValueError):
        stream.write(data)
    with contextlib.suppress(OSError, ValueError):
        stream.close()


class _Drain(threading.Thread):
    def __init__(self, stream: IO[bytes]) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.chunks: list[bytes] = []

    def run(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            for chunk in iter(lambda: self.stream.read1(65536), b""):  # type: ignore[attr-defined]
                self.chunks.append(chunk)

    @property
    def data(self) -> bytes:
        return b"".join(self.chunks)


@dataclass
class ProcessRegistry:
    launched: list[Launched] = field(default_factory=list)
    allow_forced: bool = False  # a test that expects a child to need SIGKILL opts in
    allow_survivors: bool = False  # a test that expects a child to leave processes behind

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
        input: str | bytes | None = None,
        timeout: float = 60,
        text: bool = True,
        **kwargs: Any,
    ) -> Completed:
        """Like ``subprocess.run`` with capture, but the child is tracked and cleaned up.

        With ``text`` (default) both streams are decoded as strict UTF-8, so invalid output
        fails; ``stdout_bytes`` and ``stderr_bytes`` always hold the raw bytes.
        """
        kwargs.setdefault("stdin", subprocess.PIPE if input is not None else subprocess.DEVNULL)
        proc = self.popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        rec = self._record(proc)
        assert proc.stdout is not None and proc.stderr is not None
        drains = [_Drain(proc.stdout), _Drain(proc.stderr)]
        for drain in drains:
            drain.start()
        rec.readers = list(drains)
        if input is not None and proc.stdin is not None:
            data = input.encode() if isinstance(input, str) else input
            threading.Thread(target=_feed, args=(proc.stdin, data), daemon=True).start()
        if not self._exited(rec, timeout):
            self._terminate(rec)
            self._finish(rec, drains)
            raise subprocess.TimeoutExpired(argv, timeout)
        # The leader has exited but is not reaped: sweep its group first, then reap.
        self._sweep(rec, grace=0.0)
        proc.wait()
        self._finish(rec, drains)
        out, err = drains[0].data, drains[1].data
        if text:
            # Decoded by hand: text mode would turn the CRLF of `-i` headers into LF.
            done = Completed(argv, proc.returncode, out.decode("utf-8"), err.decode("utf-8"))
        else:
            done = Completed(argv, proc.returncode, out, err)
        done.stdout_bytes, done.stderr_bytes = out, err
        return done

    def _finish(self, rec: Launched, drains: list[_Drain]) -> None:
        """Join the readers, then close the pipes. A pipe still held open by a process that
        escaped the group is left open: closing it under a blocked reader could deadlock, and
        the leak check kills that process at teardown."""
        for drain in drains:
            drain.join(5)
        self._close(rec)

    def _record(self, proc: subprocess.Popen[Any]) -> Launched:
        for rec in self.launched:
            if rec.proc is proc:
                return rec
        raise ValueError("process was not started through this registry")

    def alive(self, proc: subprocess.Popen[Any]) -> bool:
        return proc.poll() is None

    @staticmethod
    def _unreaped(rec: Launched) -> bool:
        """Whether the leader is still our child and not reaped (running or a zombie)."""
        if rec.proc.returncode is not None:
            return False
        try:
            if rec.pidfd is not None and hasattr(os, "P_PIDFD"):
                os.waitid(os.P_PIDFD, rec.pidfd, WAIT_FLAGS)  # type: ignore[attr-defined]
            else:
                os.waitid(os.P_PID, rec.pid, WAIT_FLAGS)
        except ChildProcessError:
            return False  # reaped outside Popen: the PID may already belong to someone else
        return True

    def signal(self, proc: subprocess.Popen[Any], sig: int) -> bool:
        """Signal exactly this child, if it has not been reaped. Returns whether it was sent."""
        rec = self._record(proc)
        if not self._unreaped(rec):
            return False
        if rec.pidfd is not None:
            signal.pidfd_send_signal(rec.pidfd, sig)  # type: ignore[attr-defined]
        else:
            os.kill(rec.pid, sig)
        return True

    def _signal_group(self, rec: Launched, sig: int) -> None:
        if not self._unreaped(rec):
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(rec.pgid, sig)

    def _exited(self, rec: Launched, timeout: float) -> bool:
        """Wait for the leader to exit without reaping it."""
        if rec.proc.returncode is not None:
            return True
        if rec.pidfd is not None:
            ready, _, _ = select.select([rec.pidfd], [], [], timeout)
            return bool(ready)
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.waitid(os.P_PID, rec.pid, WAIT_FLAGS) is not None:
                    return True
            except ChildProcessError:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def _others(self, rec: Launched) -> list[tuple[int, int, str]]:
        return [m for m in group_members(rec.pgid) if m[0] != rec.pid]

    def _sweep(self, rec: Launched, grace: float) -> None:
        """With the leader exited and unreaped: kill and reap what is left in its group."""
        deadline = time.monotonic() + grace
        while grace and time.monotonic() < deadline:
            if not [m for m in self._others(rec) if m[2] != "Z"]:
                break
            time.sleep(0.02)
        alive = [pid for pid, _, state in self._others(rec) if state != "Z"]
        rec.survivors.extend(p for p in alive if p not in rec.survivors)
        self._signal_group(rec, signal.SIGKILL)
        me = os.getpid()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            others = self._others(rec)
            if not others:
                break
            for pid, ppid, _ in others:
                if ppid == me:  # our child (reparented to this subreaper): safe to reap
                    with contextlib.suppress(ChildProcessError):
                        os.waitpid(pid, os.WNOHANG)
            time.sleep(0.01)

    def _close(self, rec: Launched) -> None:
        busy = any(reader.is_alive() for reader in rec.readers)
        for stream in (rec.proc.stdin, rec.proc.stdout, rec.proc.stderr) if not busy else ():
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        if rec.pidfd is not None:
            with contextlib.suppress(OSError):
                os.close(rec.pidfd)
            rec.pidfd = None

    def _terminate(self, rec: Launched) -> None:
        if self._unreaped(rec):
            exited = self._exited(rec, 0)
            if not exited:
                self._signal_group(rec, signal.SIGTERM)
                if not self._exited(rec, 2.0):
                    rec.forced = True
                    self._signal_group(rec, signal.SIGKILL)
                    self._exited(rec, 5.0)
            # Sweep stragglers in the group before the leader is reaped and the id is free.
            self._sweep(rec, grace=0.0 if exited else 1.0)
            with contextlib.suppress(subprocess.TimeoutExpired):
                rec.proc.wait(timeout=5)
        elif rec.proc.returncode is None:
            with contextlib.suppress(subprocess.TimeoutExpired, ChildProcessError):
                rec.proc.wait(timeout=0)  # reaped elsewhere: let Popen notice
        self._close(rec)

    def terminate_all(self) -> list[Launched]:
        """Teardown: stop every recorded child that is still running. Returns the forced ones."""
        for rec in reversed(self.launched):
            self._terminate(rec)
        return [rec for rec in self.launched if rec.forced]

    def check_teardown(self) -> None:
        """Fail if a child needed SIGKILL or left processes behind, unless the test opted in."""
        forced = [rec.pid for rec in self.launched if rec.forced]
        survivors = {rec.pid: rec.survivors for rec in self.launched if rec.survivors}
        problems: list[str] = []
        if forced and not self.allow_forced:
            problems.append(f"children needed SIGKILL (set allow_forced to accept): {forced}")
        if survivors and not self.allow_survivors:
            problems.append(
                f"children left processes in their group (set allow_survivors): {survivors}"
            )
        assert not problems, "; ".join(problems)
