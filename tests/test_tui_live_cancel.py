"""Cancelling a live read between its host calls, and ending its process groups on quit (#93).

The gateway is the real one with a substituted process runner, so no forge program is needed;
the one test that holds a child process starts it itself and checks the exact PID it started.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from forgectl.config import LoadedConfig
from forgectl.errors import Cancelled, ForgectlError
from forgectl.gateway import Gateway, ProcessResult, Target
from forgectl.tui.source_live import LiveSource

PR = "github.com/acme/widgets#1"
GH = Target("github", "github.com", token="fake-token-0001")
OK = b'HTTP/2.0 200 OK\r\n\r\n{"ok": true}'


def environ_for(tmp_path: Path) -> dict[str, str]:
    exe = tmp_path / "bin"
    exe.mkdir(exist_ok=True)
    for name in ("gh", "glab"):
        (exe / name).write_text("#!/bin/sh\n")
        (exe / name).chmod(0o755)
    tmp = tmp_path / "tmp"
    tmp.mkdir(exist_ok=True)
    return {"PATH": str(exe), "HOME": str(tmp_path), "TMPDIR": str(tmp)}


def answer(argv: Any, **_kw: Any) -> ProcessResult:
    return ProcessResult(pid=1, pgid=1, returncode=0, stdout=OK, stderr=b"")


def reads(tmp_path: Path, runner: Callable[..., ProcessResult], count: int) -> Callable[..., Any]:
    """A LiveSource runner: ``count`` host calls through one real gateway, as a handler makes."""

    def run(argv: list[str]) -> dict[str, Any]:
        gateway = Gateway(
            LoadedConfig.defaults(), timeout=30, environ=environ_for(tmp_path), runner=runner
        )
        for _ in range(count):
            gateway.api(GH, "GET", ["x"])
        return {}

    return run


def test_a_cancel_observed_between_host_calls_stops_the_next_call(tmp_path: Path) -> None:
    calls: list[int] = []

    def runner(argv: Any, **kw: Any) -> ProcessResult:
        calls.append(1)
        return answer(argv, **kw)

    source = LiveSource(reads(tmp_path, runner, 3))
    with pytest.raises(Cancelled):
        source.snapshot(PR, cancelled=lambda: len(calls) >= 1)
    assert len(calls) == 1  # the first call ran; the cancel was seen before the second


def test_cancelled_is_not_turned_into_an_internal_error(tmp_path: Path) -> None:
    source = LiveSource(reads(tmp_path, answer, 2))
    with pytest.raises(Cancelled):
        source.diff(PR, cancelled=lambda: True)


def test_an_uncancelled_read_makes_every_call(tmp_path: Path) -> None:
    calls: list[int] = []

    def runner(argv: Any, **kw: Any) -> ProcessResult:
        calls.append(1)
        return answer(argv, **kw)

    LiveSource(reads(tmp_path, runner, 3)).snapshot(PR, cancelled=lambda: False)
    assert len(calls) == 3


def test_a_cancel_check_that_raises_between_calls_is_an_internal_error(tmp_path: Path) -> None:
    seen: list[int] = []

    def check() -> bool:
        if seen:
            raise RuntimeError("secret detail")
        return False

    def runner(argv: Any, **kw: Any) -> ProcessResult:
        seen.append(1)
        return answer(argv, **kw)

    with pytest.raises(ForgectlError) as raised:
        LiveSource(reads(tmp_path, runner, 2)).snapshot(PR, cancelled=check)
    assert raised.value.code == "internal"
    assert "secret detail" not in raised.value.message


def test_a_gateway_outside_a_live_read_ignores_cancellation(tmp_path: Path) -> None:
    gateway = Gateway(
        LoadedConfig.defaults(), timeout=30, environ=environ_for(tmp_path), runner=answer
    )
    assert gateway.api(GH, "GET", ["x"]).status == 200


def test_close_ends_the_process_group_and_removes_the_temp_dir(tmp_path: Path) -> None:
    environ = environ_for(tmp_path)
    started = threading.Event()
    children: list[subprocess.Popen[bytes]] = []

    def runner(argv: Any, *, on_start: Callable[[int, int], None] | None, **_kw: Any) -> Any:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
        )
        children.append(proc)
        try:
            assert on_start is not None
            on_start(proc.pid, proc.pid)
            started.set()
            code = proc.wait(timeout=30)
        finally:
            if proc.poll() is None:  # only the PID started here
                proc.kill()
                proc.wait()
        return ProcessResult(pid=proc.pid, pgid=proc.pid, returncode=code, stdout=b"", stderr=b"")

    def run(argv: list[str]) -> dict[str, Any]:
        gateway = Gateway(LoadedConfig.defaults(), timeout=30, environ=environ, runner=runner)
        gateway.api(GH, "GET", ["x"])
        return {}

    source = LiveSource(run)
    outcome: list[BaseException] = []

    def worker() -> None:
        try:
            source.snapshot(PR)
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert started.wait(10)
    assert os.listdir(environ["TMPDIR"])  # the private dir exists while the read runs
    source.close()
    thread.join(10)
    assert not thread.is_alive()
    assert children[0].returncode == -signal.SIGKILL
    assert os.listdir(environ["TMPDIR"]) == []
    assert outcome and isinstance(outcome[0], Exception)


def test_a_read_started_after_close_is_cancelled_without_a_call(tmp_path: Path) -> None:
    calls: list[int] = []

    def runner(argv: Any, **kw: Any) -> ProcessResult:
        calls.append(1)
        return answer(argv, **kw)

    source = LiveSource(reads(tmp_path, runner, 1))
    source.close()
    with pytest.raises(Cancelled):
        source.snapshot(PR)
    assert calls == []


def test_close_is_idempotent_and_needs_no_reads() -> None:
    source = LiveSource(lambda argv: {})
    source.close()
    source.close()


def test_a_spawn_after_close_is_killed_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel check passed just before close; the child then starts: it is not left."""
    monkeypatch.setattr("forgectl.tui.source_live.CLOSE_WAIT", 0.1)  # close runs inside the read
    environ = environ_for(tmp_path)
    holder: dict[str, Any] = {}

    def runner(argv: Any, *, on_start: Callable[[int, int], None] | None, **_kw: Any) -> Any:
        source.close()  # between the check that passed and the spawn
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
        )
        holder["proc"] = proc
        try:
            assert on_start is not None
            on_start(proc.pid, proc.pid)
            code = proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        return ProcessResult(pid=proc.pid, pgid=proc.pid, returncode=code, stdout=b"", stderr=b"")

    def run(argv: list[str]) -> dict[str, Any]:
        Gateway(LoadedConfig.defaults(), timeout=30, environ=environ, runner=runner).api(
            GH, "GET", ["x"]
        )
        return {}

    source = LiveSource(run)
    with pytest.raises((ForgectlError, Cancelled)):
        source.snapshot(PR)
    assert holder["proc"].returncode == -signal.SIGKILL


def run_browse_with(monkeypatch: pytest.MonkeyPatch, source: Any, boom: bool = False) -> None:
    from forgectl.tui import run as run_mod

    class StubApp:
        return_code = 0

        def __init__(self, src: Any, opener: Any, **kwargs: Any) -> None:
            pass

        def run(self) -> None:
            if boom:
                raise RuntimeError("app failed")

        def result(self) -> dict[str, Any]:
            return {"intent": "back", "selected": [], "merged": []}

    monkeypatch.setattr(run_mod, "BrowseApp", StubApp)
    run_mod.run_browse(focus=None, source=source)


class ClosingSource:
    closed = 0

    def close(self) -> None:
        self.closed += 1


def test_quitting_browse_closes_the_source(monkeypatch: pytest.MonkeyPatch) -> None:
    source = ClosingSource()
    run_browse_with(monkeypatch, source)
    assert source.closed == 1


def test_the_source_is_closed_when_the_app_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    source = ClosingSource()
    with pytest.raises(RuntimeError):
        run_browse_with(monkeypatch, source, boom=True)
    assert source.closed == 1


def test_a_source_without_close_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    run_browse_with(monkeypatch, object())
