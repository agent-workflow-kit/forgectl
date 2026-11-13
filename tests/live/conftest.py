"""Live fixtures: skip cleanly without settings, one throwaway per host per session, cleanup in a
finally, a session finalizer and a terminal summary of anything left for a human.

Nothing here runs unless ``make live`` (``-m live --run-live``) is used outside CI with the
dedicated ``FORGECTL_LIVE_*`` settings. Every live test module sets ``pytestmark =
pytest.mark.live`` (``test_live_gate.py`` checks it).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from . import guards
from .api import LiveApi, Runner, WriteScope, subprocess_runner
from .hosts import GitHubLive, GitLabLive, Ledger, Throwaway

LEDGER = Ledger()  # one per process: the finalizer cleans whatever a fixture missed
# The process environment as pytest started it. The suite's per-test isolation fixture clears
# every FORGECTL_* variable (and the CI markers) before each test, so the live settings are read
# from this copy, which ``pytest_configure`` takes before any test runs. Nothing else uses it.
ENV: dict[str, str] = {}


@dataclass
class LiveSession:
    cfg: guards.LiveConfig
    dirs: guards.PrivateDirs = field(repr=False)
    host: GitHubLive | GitLabLive
    throwaway: Throwaway
    source_env: Mapping[str, str] = field(repr=False)  # holds the live token
    runner: Runner = field(repr=False)

    def forgectl(
        self, argv: Sequence[str], *, second: bool = False, stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run forgectl in the private environment, credentialed through the dedicated variable.
        Refused before a process starts unless every reference points at this throwaway."""
        guards.check_forgectl_argv(argv, self.cfg, f"{self.throwaway.owner}/{self.throwaway.name}")
        guards.write_forgectl_config(self.dirs, self.cfg.backend)
        env = guards.forgectl_child_env(self.cfg, self.dirs, self.source_env, second=second)
        guards.assert_env_safe(env, self.dirs, self.source_env)
        result = self.runner([sys.executable, "-m", "forgectl", *argv], env, stdin, self.dirs.cwd)
        return subprocess.CompletedProcess(
            list(argv), result.returncode, result.stdout, result.stderr
        )


def _start(
    backend: str,
    request: pytest.FixtureRequest,
    tmp_root: Path,
    *,
    env: Mapping[str, str],
    runner: Runner,
    ledger: Ledger,
) -> LiveSession:
    run_live = bool(request.config.getoption("--run-live"))
    cfg, reason = guards.load_config(backend, env, run_live=run_live)
    if cfg is None:
        pytest.skip(reason or "live settings absent")
    dirs = guards.make_private_dirs(tmp_root / f"live-{backend}")
    scope = WriteScope(GitHubLive.creation if backend == "github" else GitLabLive.creation)
    api = LiveApi(cfg, dirs, scope, source_env=env, runner=runner)
    host: GitHubLive | GitLabLive = (
        GitHubLive(api, cfg, ledger) if backend == "github" else GitLabLive(api, cfg, ledger)
    )
    host.whoami()  # refuses a token that is not the declared account's, before any write
    name = guards.throwaway_name(guards.new_run_id())
    throwaway = host.create_throwaway(name)
    return LiveSession(cfg, dirs, host, throwaway, env, runner)


def _session(
    backend: str,
    request: pytest.FixtureRequest,
    *,
    env: Mapping[str, str] | None = None,
    runner: Runner = subprocess_runner,
    ledger: Ledger | None = None,
) -> Generator[LiveSession]:
    """The private directories live for the whole session and are removed only after the cleanup,
    which runs even when a step after the creation raises (the cleanup needs the directories)."""
    ledger = LEDGER if ledger is None else ledger
    with tempfile.TemporaryDirectory(prefix="forgectl-live-") as raw:
        try:
            yield _start(
                backend,
                request,
                Path(raw),
                env=ENV if env is None else env,
                runner=runner,
                ledger=ledger,
            )
        finally:
            ledger.cleanup_all()


@pytest.fixture(scope="session")
def live_github(request: pytest.FixtureRequest) -> Iterator[LiveSession]:
    yield from _session("github", request)


@pytest.fixture(scope="session")
def live_gitlab(request: pytest.FixtureRequest) -> Iterator[LiveSession]:
    yield from _session("gitlab", request)


@pytest.fixture
def second_account(request: pytest.FixtureRequest) -> Callable[[str], guards.LiveConfig]:
    """``second_account("github")``: the config with its optional second account, or a skip when
    the dedicated settings are absent."""

    def get(backend: str) -> guards.LiveConfig:
        run_live = bool(request.config.getoption("--run-live"))
        cfg, reason = guards.load_config(backend, ENV, run_live=run_live)
        if cfg is not None and cfg.second_token is None:
            reason = guards.second_account_problem(backend, ENV)
        if cfg is None or cfg.second_token is None:
            pytest.skip(reason or f"no second {backend} account configured")
        return cfg

    return get


def pytest_configure(config: pytest.Config) -> None:
    ENV.clear()
    ENV.update(os.environ)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """The finalizer behind the fixtures: also runs after an interrupted session, and tries once
    more what an earlier pass left."""
    LEDGER.cleanup_all(retry_left=True)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    lines = LEDGER.declarations()
    if lines:
        terminalreporter.section("live harness: left for manual deletion")
        for line in lines:
            terminalreporter.line(line)
