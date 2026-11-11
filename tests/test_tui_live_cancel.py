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
from forgectl.gateway import CALL_SCOPE, CallScope, Gateway, ProcessResult, Target, run_process
from forgectl.tui.source_live import LiveSource, _kill_group

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


def child_env(tmp_path: Path) -> dict[str, str]:
    """The environment of a child these tests start: a private HOME and XDG_CONFIG_HOME."""
    home = tmp_path / "child-home"
    home.mkdir(exist_ok=True)
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "XDG_CONFIG_HOME": str(home)}


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
    calls: list[int] = []

    def runner(argv: Any, **kw: Any) -> ProcessResult:
        calls.append(1)
        return answer(argv, **kw)

    source = LiveSource(reads(tmp_path, runner, 2))
    with pytest.raises(Cancelled):  # the pre-check passes; the gateway raises after the first call
        source.diff(PR, cancelled=lambda: len(calls) >= 1)
    assert len(calls) == 1


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
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
            env=child_env(tmp_path),
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
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
            env=child_env(tmp_path),
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


def sleeper_runner(
    tmp_path: Path, started: threading.Event, children: list[subprocess.Popen[bytes]]
) -> Callable[..., Any]:
    def runner(argv: Any, *, on_start: Callable[[int, int], None] | None, **_kw: Any) -> Any:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
            env=child_env(tmp_path),
        )
        children.append(proc)
        try:
            assert on_start is not None
            on_start(proc.pid, proc.pid)
            started.set()
            code = proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        return ProcessResult(pid=proc.pid, pgid=proc.pid, returncode=code, stdout=b"", stderr=b"")

    return runner


def test_close_ends_a_group_started_during_the_pr_list_load(tmp_path: Path) -> None:
    started = threading.Event()
    children: list[subprocess.Popen[bytes]] = []
    runner = sleeper_runner(tmp_path, started, children)

    def run(argv: list[str]) -> dict[str, Any]:
        gateway = Gateway(
            LoadedConfig.defaults(), timeout=30, environ=environ_for(tmp_path), runner=runner
        )
        gateway.api(GH, "GET", ["x"])
        return {}

    source = LiveSource(run)
    thread = threading.Thread(target=lambda: _swallow(source.prs), daemon=True)
    thread.start()
    assert started.wait(10)
    source.close()
    thread.join(10)
    assert not thread.is_alive()  # close() waited for the list load, then it unwound
    assert children[0].returncode == -signal.SIGKILL


def test_close_ends_a_group_started_during_a_bulk_snapshot_read(tmp_path: Path) -> None:
    started = threading.Event()
    children: list[subprocess.Popen[bytes]] = []
    runner = sleeper_runner(tmp_path, started, children)

    def run(argv: list[str]) -> dict[str, Any]:
        gateway = Gateway(
            LoadedConfig.defaults(), timeout=30, environ=environ_for(tmp_path), runner=runner
        )
        gateway.api(GH, "GET", ["x"])
        return {}

    source = LiveSource(run)
    thread = threading.Thread(target=lambda: _swallow(lambda: source.snapshots([PR])), daemon=True)
    thread.start()
    assert started.wait(10)
    source.close()
    thread.join(10)
    assert not thread.is_alive()
    assert children[0].returncode == -signal.SIGKILL


def test_a_bulk_snapshot_read_started_after_close_is_cancelled_without_a_call(
    tmp_path: Path,
) -> None:
    calls: list[int] = []

    def runner(argv: Any, **kw: Any) -> ProcessResult:
        calls.append(1)
        return answer(argv, **kw)

    source = LiveSource(reads(tmp_path, runner, 1))
    source.close()
    with pytest.raises(Cancelled):
        source.snapshots([PR])
    assert calls == []


def _swallow(fn: Callable[[], Any]) -> None:
    try:
        fn()
    except Exception:
        pass


def test_a_pr_list_load_started_after_close_is_cancelled_without_a_call(tmp_path: Path) -> None:
    calls: list[int] = []

    def runner(argv: Any, **kw: Any) -> ProcessResult:
        calls.append(1)
        return answer(argv, **kw)

    source = LiveSource(reads(tmp_path, runner, 1))
    source.close()
    with pytest.raises(Cancelled):
        source.prs()
    assert calls == []


def scoped_gateway(
    tmp_path: Path, runner: Callable[..., ProcessResult], scope: CallScope
) -> tuple[Gateway, Any]:
    token = CALL_SCOPE.set(scope)
    try:
        return Gateway(
            LoadedConfig.defaults(), timeout=30, environ=environ_for(tmp_path), runner=runner
        ), token
    finally:
        CALL_SCOPE.reset(token)


def test_run_external_reports_done_after_the_runner_returns(tmp_path: Path) -> None:
    events: list[str] = []
    scope = CallScope(on_done=lambda: events.append("done"))

    def runner(argv: Any, **kw: Any) -> ProcessResult:
        events.append("ran")
        return answer(argv, **kw)

    gateway, _ = scoped_gateway(tmp_path, runner, scope)
    gateway.run_external(["gh"], timeout=5, mode="hook")
    assert events == ["ran", "done"]


def test_run_external_reports_done_when_the_runner_raises(tmp_path: Path) -> None:
    events: list[str] = []
    scope = CallScope(on_done=lambda: events.append("done"))

    def runner(argv: Any, **kw: Any) -> ProcessResult:
        raise RuntimeError("boom")

    gateway, _ = scoped_gateway(tmp_path, runner, scope)
    with pytest.raises(RuntimeError):
        gateway.run_external(["gh"], timeout=5, mode="hook")
    assert events == ["done"]


def test_a_cancelled_read_starts_no_token_command(tmp_path: Path) -> None:
    calls: list[int] = []

    def runner(argv: Any, **kw: Any) -> ProcessResult:
        calls.append(1)
        return answer(argv, **kw)

    gateway, _ = scoped_gateway(tmp_path, runner, CallScope(cancelled=lambda: True))
    with pytest.raises(Cancelled):
        gateway.run_external(["gh"], timeout=5, mode="token_command", backend="github")
    assert calls == []


def test_a_cancelled_api_call_resolves_no_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, _ = scoped_gateway(tmp_path, answer, CallScope(cancelled=lambda: True))

    def boom(*_a: Any, **_k: Any) -> str:
        raise AssertionError("the token lookup started after the cancel")

    monkeypatch.setattr(gateway, "_executable", boom)
    with pytest.raises(Cancelled):
        gateway.api(GH, "GET", ["x"])


def test_kill_group_does_nothing_for_a_child_that_is_already_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"], start_new_session=True, env=child_env(tmp_path)
    )
    proc.wait()  # reaped: its number may be anyone's now
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    _kill_group(proc.pid, proc.pid)
    assert sent == []


def test_run_process_reports_the_reap_before_it_reaps(tmp_path: Path) -> None:
    seen: list[str] = []

    def on_reap(pid: int, pgid: int) -> None:
        try:
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            seen.append("unreaped")
        except ChildProcessError:
            seen.append("reaped")

    run_process(
        [sys.executable, "-c", "pass"],
        env=child_env(tmp_path),
        cwd=str(tmp_path),
        timeout=20,
        on_reap=on_reap,
    )
    assert seen == ["unreaped"]  # the tracker can drop the group while the number is reserved


def test_a_reaped_group_is_dropped_from_the_tracking_by_the_reap_hook(tmp_path: Path) -> None:
    """close() signals only tracked groups: after the reap hook, nothing is left to signal."""
    source = LiveSource(lambda argv: {})
    call_seen: list[set[tuple[int, int]]] = []

    def runner(argv: Any, *, on_start: Any, on_reap: Any = None, **_kw: Any) -> ProcessResult:
        on_start(4242, 4242)
        call_seen.append({(4242, 4242)})
        assert on_reap is not None
        on_reap(4242, 4242)
        return answer(argv)

    def run(argv: list[str]) -> dict[str, Any]:
        Gateway(
            LoadedConfig.defaults(), timeout=30, environ=environ_for(tmp_path), runner=runner
        ).api(GH, "GET", ["x"])
        return {}

    source = LiveSource(run)
    sent: list[int] = []
    import forgectl.tui.source_live as live

    original = live._kill_group
    live._kill_group = lambda pid, pgid: sent.append(pid)  # type: ignore[assignment]
    try:
        # a close() racing in after the reap hook must find nothing to signal
        orig_run = source._runner

        def racing(argv: list[str]) -> dict[str, Any]:
            out = orig_run(argv)
            source.close()
            return out

        source._runner = racing
        source.snapshot(PR)
    finally:
        live._kill_group = original
    assert sent == []


def test_a_detached_spawn_is_not_tracked_by_the_read_scope(tmp_path: Path) -> None:
    from forgectl.gateway import Detached

    got: list[Any] = []

    def spawner(argv: Any, *, on_start: Any, **_kw: Any) -> Detached:
        got.append(on_start)
        return Detached(1, 1)

    token = CALL_SCOPE.set(CallScope(on_spawn=lambda pid, pgid: None))
    try:
        gateway = Gateway(LoadedConfig.defaults(), environ=environ_for(tmp_path), spawner=spawner)
    finally:
        CALL_SCOPE.reset(token)
    gateway.spawn_detached(["x"], env={}, cwd=str(tmp_path), stdin=b"")
    assert got == [None]
