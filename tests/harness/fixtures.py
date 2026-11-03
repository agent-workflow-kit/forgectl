"""Shared fixtures: environment isolation guard, process registry, fake forges, CLI runner.

Loaded as a plugin from ``tests/conftest.py``.
"""

from __future__ import annotations

import inspect
import os
import pwd
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
from .safety import AMBIENT_TOKEN_VARS

XDG_VARS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
CONFIG_DIR_VARS = ("GH_CONFIG_DIR", "GLAB_CONFIG_DIR")
CLEARED = (
    *AMBIENT_TOKEN_VARS,
    "GH_HOST",
    "GH_REPO",
    "GITLAB_HOST",
    "GH_FORCE_TTY",
    "CLICOLOR_FORCE",
    "DBUS_SESSION_BUS_ADDRESS",
    "FORGECTL_GH_BIN",
    "FORGECTL_GLAB_BIN",
    "FORGECTL_TIMEOUT",
    "FORGECTL_RETRIES",
)


class IsolationError(AssertionError):
    """A test started a program with a HOME or config directory outside its private temp dir."""


@dataclass(frozen=True)
class Isolation:
    root: Path  # pytest's base temp directory: the only place a child may keep config
    home: Path

    def check_env(self, env: Mapping[str, str]) -> None:
        home = env.get("HOME")
        if not home or not _inside(home, self.root):
            raise IsolationError(f"child HOME {home!r} is not under {self.root}")
        for name in (*XDG_VARS, *CONFIG_DIR_VARS):
            if name in env and not _inside(env[name], self.root):
                raise IsolationError(f"child {name}={env[name]!r} is not under {self.root}")


def _inside(path: str, root: Path) -> bool:
    real = os.path.realpath(path)
    base = os.path.realpath(root)
    return real == base or real.startswith(base + os.sep)


@pytest.fixture(autouse=True)
def isolation(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Isolation]:
    """Private HOME, XDG and config dirs for every test, and a guard on every child process."""
    root = tmp_path_factory.getbasetemp()
    private = tmp_path_factory.mktemp("iso")
    dirs = {
        "HOME": private / "home",
        "XDG_CONFIG_HOME": private / "xdg-config",
        "XDG_DATA_HOME": private / "xdg-data",
        "XDG_STATE_HOME": private / "xdg-state",
        "XDG_CACHE_HOME": private / "xdg-cache",
        "GH_CONFIG_DIR": private / "gh-config",
        "GLAB_CONFIG_DIR": private / "glab-config",
        "TMPDIR": private / "tmp",
    }
    for name, path in dirs.items():
        path.mkdir(mode=0o700)
        monkeypatch.setenv(name, str(path))
    for name in CLEARED:
        monkeypatch.delenv(name, raising=False)
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
    yield iso


@pytest.fixture
def real_home() -> str:
    """The account's real home directory, for tests that prove the guard rejects it."""
    return pwd.getpwuid(os.getuid()).pw_dir


@pytest.fixture
def procs() -> Iterator[ProcessRegistry]:
    registry = ProcessRegistry()
    try:
        yield registry
    finally:
        registry.terminate_all()


@pytest.fixture
def fake_forges(tmp_path: Path) -> Iterator[FakeForges]:
    forges = FakeForges(tmp_path)
    yield forges
    if not forges.allow_unmatched:
        unmatched = forges.unmatched()
        assert not unmatched, (
            "requests with no scripted answer (set forges.allow_unmatched to accept): "
            + "; ".join(f"{c.backend} {' '.join(c.argv)}" for c in unmatched)
        )


@pytest.fixture
def run_forgectl(procs: ProcessRegistry, fake_forges: FakeForges) -> Any:
    """Run the real CLI as a tracked child, fakes first on PATH, isolated HOME and XDG."""

    def run(
        argv: list[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        input: str | None = None,
        timeout: float = 60,
    ) -> subprocess.CompletedProcess[str]:
        child = {
            k: v
            for k, v in os.environ.items()
            if k in {"HOME", "TMPDIR", *XDG_VARS, *CONFIG_DIR_VARS}
        }
        child["PATH"] = fake_forges.path(str(Path(sys.executable).parent))
        child.update(env or {})
        return procs.run(
            [sys.executable, "-m", "forgectl", *argv],
            env=child,
            cwd=cwd,
            input=input,
            timeout=timeout,
        )

    return run
