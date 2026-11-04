"""Shared fixtures and hooks: environment isolation guard, process registry, leaked-child check,
fake forges, CLI runner, and the ``live`` marker gate.

Loaded as a plugin from ``tests/conftest.py``.
"""

from __future__ import annotations

import contextlib
import ctypes
import inspect
import os
import pwd
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from .forges import FakeForges
from .procs import ProcessRegistry
from .safety import AMBIENT_SENTINELS, AMBIENT_TOKEN_VARS

XDG_VARS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
CONFIG_DIR_VARS = ("GH_CONFIG_DIR", "GLAB_CONFIG_DIR")
CHECKED_VARS = ("HOME", *XDG_VARS, *CONFIG_DIR_VARS, "XDG_RUNTIME_DIR")
CI_VARS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI")
CLEARED = (
    *AMBIENT_TOKEN_VARS,
    "GH_HOST",
    "GH_REPO",
    "GITLAB_HOST",
    "GL_HOST",
    "GH_FORCE_TTY",
    "CLICOLOR_FORCE",
    "DBUS_SESSION_BUS_ADDRESS",
    "FORGECTL_GH_BIN",
    "FORGECTL_GLAB_BIN",
    "FORGECTL_TIMEOUT",
    "FORGECTL_RETRIES",
)
PR_SET_CHILD_SUBREAPER = 36


def in_ci(env: Mapping[str, str] | None = None) -> bool:
    """Any CI marker variable present, whatever its value (``CI=`` counts)."""
    env = os.environ if env is None else env
    return any(name in env for name in CI_VARS)


# The live marker -------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run tests marked live (needs real hosts and credentials; refused in CI)",
    )


def live_skip_reason(run_live: bool, env: Mapping[str, str]) -> str | None:
    if in_ci(env):
        return "live tests never run in CI"
    if not run_live:
        return "live test: pass --run-live to run it"
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``live`` tests unless ``--run-live`` is given outside CI, whatever ``-m`` says."""
    reason = live_skip_reason(bool(config.getoption("--run-live")), os.environ)
    if reason is None:
        return
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(pytest.mark.skip(reason=reason))


# Leaked children -------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Make this process a child subreaper (Linux), so an orphan that escaped its process group
    with setsid becomes our child and the leak check below sees it."""
    if sys.platform.startswith("linux"):
        with contextlib.suppress(OSError, AttributeError):
            libc = ctypes.CDLL(None, use_errno=True)
            libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)


def direct_children() -> set[int]:
    """PIDs of this process's direct children, running or unreaped (Linux; empty elsewhere)."""
    found: set[int] = set()
    tasks = Path("/proc/self/task")
    if not tasks.is_dir():
        return found
    for task in tasks.iterdir():
        with contextlib.suppress(OSError):
            found.update(int(p) for p in (task / "children").read_text().split())
    return found


def reap_new_children(before: set[int]) -> list[int]:
    """SIGKILL and reap our direct children not present in ``before``; return their PIDs.

    Each is our own unreaped child, so its PID cannot have been reused; a pidfd taken first and
    re-checked with ``waitid(WNOWAIT)`` pins the exact process before it is signaled.
    """
    leaked = sorted(direct_children() - before)
    for pid in leaked:
        try:
            pidfd = os.pidfd_open(pid)
        except (OSError, AttributeError):
            continue
        try:
            try:
                os.waitid(  # type: ignore[attr-defined]
                    os.P_PIDFD, pidfd, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
            except ChildProcessError:
                continue  # not ours any more
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(pidfd, signal.SIGKILL)  # type: ignore[attr-defined]
            with contextlib.suppress(ChildProcessError):
                os.waitid(os.P_PIDFD, pidfd, os.WEXITED)  # type: ignore[attr-defined]
        finally:
            os.close(pidfd)
    return leaked


@pytest.fixture(autouse=True)
def no_leaked_children() -> Iterator[None]:
    """Fail a test that leaves a child of this process behind (torn down after ``procs``)."""
    before = direct_children()
    yield
    leaked = reap_new_children(before)
    if leaked:
        pytest.fail(f"test left child processes behind (killed and reaped): {leaked}")


# Isolation -------------------------------------------------------------------------------


class IsolationError(AssertionError):
    """A test started a program with a HOME or config directory outside its private temp dir."""


@dataclass(frozen=True)
class Isolation:
    root: Path  # pytest's base temp directory: the only place a child may keep config
    home: Path

    def check_env(self, env: Mapping[Any, Any]) -> None:
        text = {
            os.fsdecode(k): os.fsdecode(v)
            for k, v in env.items()
            if isinstance(k, str | bytes) and isinstance(v, str | bytes)
        }
        home = text.get("HOME")
        if not home or not os.path.isabs(home) or not _inside(home, self.root):
            raise IsolationError(f"child HOME {home!r} is not an absolute path under {self.root}")
        for name in CHECKED_VARS[1:]:
            if name not in text:
                continue
            value = text[name]
            if not os.path.isabs(value) or not _inside(value, self.root):
                raise IsolationError(
                    f"child {name}={value!r} is not an absolute path under {self.root}"
                )


def _inside(path: str, root: Path) -> bool:
    real = os.path.realpath(path)
    base = os.path.realpath(root)
    return real == base or real.startswith(base + os.sep)


@pytest.fixture(autouse=True)
def isolation(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Isolation]:
    """Private HOME, XDG and config dirs for every test, and a guard on every child process.

    The guard wraps ``subprocess.Popen``, ``os.posix_spawn``, ``os.posix_spawnp`` and
    ``os.execve``. ``os.system``, ``os.spawn*`` and ``os.exec*`` without an environment inherit
    ``os.environ``, which this fixture has already made private.
    """
    root = tmp_path_factory.getbasetemp()
    private = tmp_path_factory.mktemp("iso")
    dirs = {
        "HOME": private / "home",
        "XDG_CONFIG_HOME": private / "xdg-config",
        "XDG_DATA_HOME": private / "xdg-data",
        "XDG_STATE_HOME": private / "xdg-state",
        "XDG_CACHE_HOME": private / "xdg-cache",
        "XDG_RUNTIME_DIR": private / "xdg-runtime",
        "GH_CONFIG_DIR": private / "gh-config",
        "GLAB_CONFIG_DIR": private / "glab-config",
        "TMPDIR": private / "tmp",
    }
    for name, path in dirs.items():
        path.mkdir(mode=0o700)
        monkeypatch.setenv(name, str(path))
    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
    for name in [n for n in os.environ if n.startswith("FORGECTL_")]:
        monkeypatch.delenv(name)  # #4 clears every FORGECTL_* variable, not only the listed ones
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(tempfile, "tempdir", str(dirs["TMPDIR"]))

    iso = Isolation(root=root, home=dirs["HOME"])
    original = subprocess.Popen.__init__
    signature = inspect.signature(original)

    def guarded(self: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
        bound = signature.bind(self, *args, **kwargs)
        env = bound.arguments.get("env")
        iso.check_env(os.environ if env is None else env)
        original(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded)

    def wrap_spawn(real: Any) -> Any:
        def spawn(path: Any, argv: Any, env: Any, *args: Any, **kwargs: Any) -> Any:
            iso.check_env(os.environ if env is None else env)
            return real(path, argv, env, *args, **kwargs)

        return spawn

    for name in ("posix_spawn", "posix_spawnp", "execve"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, wrap_spawn(getattr(os, name)))
    yield iso


@pytest.fixture
def real_home() -> str:
    """The account's real home directory, for tests that prove the guard rejects it."""
    return pwd.getpwuid(os.getuid()).pw_dir


# Processes, fakes and the CLI runner -----------------------------------------------------


@pytest.fixture
def procs() -> Iterator[ProcessRegistry]:
    registry = ProcessRegistry()
    yield registry
    registry.terminate_all()
    registry.check_teardown()


@pytest.fixture
def fake_forges(tmp_path: Path) -> Iterator[FakeForges]:
    forges = FakeForges(tmp_path)
    yield forges
    forges.check_teardown()


def forgectl_env(forges: FakeForges, env: Mapping[str, str | None] | None = None) -> dict[str, str]:
    """The environment ``run_forgectl`` gives the CLI: the private dirs, the fakes first on
    ``PATH``, a distinct sentinel in every ambient token variable, then ``env`` (a value of
    ``None`` removes that variable)."""
    child = {
        k: v for k, v in os.environ.items() if k in {"HOME", "TMPDIR", *XDG_VARS, *CONFIG_DIR_VARS}
    }
    child["PATH"] = forges.path(str(Path(sys.executable).parent))
    child.update(AMBIENT_SENTINELS)
    for name, value in (env or {}).items():
        if value is None:
            child.pop(name, None)
        else:
            child[name] = value
    return child


@pytest.fixture
def run_forgectl(procs: ProcessRegistry, fake_forges: FakeForges) -> Any:
    """Run the real CLI as a tracked child, fakes first on PATH, isolated HOME and XDG, and
    sentinel ambient tokens that ``assert_call_safe`` checks by default."""

    def run(
        argv: list[str],
        *,
        env: Mapping[str, str | None] | None = None,
        cwd: Path | None = None,
        input: str | None = None,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[str]:
        return procs.run(
            [sys.executable, "-m", "forgectl", *argv],
            env=forgectl_env(fake_forges, env),
            cwd=cwd,
            input=input,
            timeout=timeout,
        )

    return run
