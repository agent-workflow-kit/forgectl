"""The live fixtures against a fake runner: setup, cleanup on failure, the settings snapshot."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from pprint import saferepr
from types import SimpleNamespace
from typing import Any

import pytest

from .harness.fixtures import CI_VARS
from .live import conftest as live_conftest
from .live import guards
from .live.api import RunResult
from .live.hosts import Ledger

REPO = Path(__file__).resolve().parent.parent
TOKEN = "tok-live-0001"
ENV = {
    "PATH": "/usr/bin",
    "FORGECTL_LIVE_GITHUB_TOKEN": TOKEN,
    "FORGECTL_LIVE_GITHUB_USER": "me-login",
}


class Fake:
    """A GitHub that answers like the real one; every call checks that its cwd still exists."""

    def __init__(self, owner: str = "me-login") -> None:
        self.owner = owner
        self.calls: list[tuple[str, str]] = []
        self.cwd_missing = False
        self.deleted = False
        self.fail_deletes = 0  # the first N deletes answer 502

    def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], stdin: str | None, cwd: Path
    ) -> RunResult:
        self.cwd_missing = self.cwd_missing or not cwd.is_dir()
        method, endpoint = argv[argv.index("-X") + 1], argv[-1]
        self.calls.append((method, endpoint))
        if (method, endpoint) == ("GET", "user"):
            return self.answer(200, {"login": "me-login"})
        if (method, endpoint) == ("POST", "user/repos"):
            return self.answer(201, {"owner": {"login": self.owner}, "default_branch": "main"})
        if method == "DELETE" and self.fail_deletes:
            self.fail_deletes -= 1
            return self.answer(502, {"message": "bad gateway"})
        if method == "DELETE":
            self.deleted = True
            return self.answer(204, None)
        if method == "GET":
            return self.answer(404 if self.deleted else 200, {})
        return self.answer(404, None)

    @staticmethod
    def answer(status: int, body: Any) -> RunResult:
        import json

        text = "" if body is None else json.dumps(body)
        return RunResult(0, f"HTTP/2.0 {status} X\r\n\r\n{text}", "")


def request(run_live: bool = True) -> Any:
    return SimpleNamespace(config=SimpleNamespace(getoption=lambda name: run_live))


def test_cleanup_runs_with_its_directories_when_setup_fails_after_the_creation() -> None:
    fake, ledger = Fake(owner="an-org"), Ledger()  # the repo lands in the wrong namespace
    session = live_conftest._session("github", request(), env=ENV, runner=fake, ledger=ledger)
    with pytest.raises(guards.LiveRefused):
        next(session)
    assert fake.deleted and not fake.cwd_missing
    assert [t.status for _, t in ledger.entries] == ["deleted"]


def test_cleanup_runs_with_its_directories_when_the_test_body_raises() -> None:
    fake, ledger = Fake(), Ledger()
    session = live_conftest._session("github", request(), env=ENV, runner=fake, ledger=ledger)
    next(session)
    with pytest.raises(RuntimeError):
        session.throw(RuntimeError("test failed"))
    assert fake.deleted and not fake.cwd_missing


def test_the_finalizer_retry_runs_with_its_directories_and_removes_them_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, ledger = Fake(), Ledger()
    fake.fail_deletes = 1
    monkeypatch.setattr(live_conftest, "LEDGER", ledger)
    monkeypatch.setattr(live_conftest, "KEPT_DIRS", [])
    session = live_conftest._session("github", request(), env=ENV, runner=fake, ledger=ledger)
    live = next(session)
    root = live.dirs.root
    with pytest.raises(StopIteration):
        next(session)
    assert [t.status for _, t in ledger.entries] == ["left"]
    assert root.is_dir()  # kept for the finalizer's retry
    live_conftest.pytest_sessionfinish(None, 0)  # type: ignore[arg-type]
    assert [t.status for _, t in ledger.entries] == ["deleted"]
    assert fake.deleted and not fake.cwd_missing
    assert not root.exists() and live_conftest.KEPT_DIRS == []


def test_a_session_repr_never_shows_the_token() -> None:
    fake, ledger = Fake(), Ledger()
    session = live_conftest._session("github", request(), env=ENV, runner=fake, ledger=ledger)
    live = next(session)
    for text in (repr(live), saferepr(live), str(live)):
        assert TOKEN not in text
    with pytest.raises(StopIteration):
        next(session)


def test_a_session_refuses_a_forgectl_command_for_anything_but_its_throwaway() -> None:
    fake, ledger = Fake(), Ledger()
    session = live_conftest._session("github", request(), env=ENV, runner=fake, ledger=ledger)
    live = next(session)
    launched = len(fake.calls)
    with pytest.raises(guards.LiveRefused):
        live.forgectl(["show", "https://github.com/someone/else/pull/1"])
    with pytest.raises(guards.LiveRefused):
        live.forgectl(["whoami", "--host", "ghe.example.test"])
    assert len(fake.calls) == launched
    with pytest.raises(StopIteration):
        next(session)


# The settings snapshot --------------------------------------------------------------------------

PROBE = """
import pytest
from tests.live import conftest as c
from tests.test_live_fixtures import Fake, request
from tests.live.hosts import Ledger


def test_the_fixtures_see_the_settings_the_isolation_fixture_cleared(monkeypatch):
    import os

    assert not [n for n in os.environ if n.startswith("FORGECTL_")]  # cleared for this test
    fake, ledger = Fake(), Ledger()
    session = c._session("github", request(), env=c.ENV, runner=fake, ledger=ledger)
    live = next(session)
    assert live.cfg.login == "me-login" and live.cfg.second_token is None
    assert ("GET", "user") in fake.calls
    with pytest.raises(StopIteration):
        next(session)
    assert fake.deleted


def test_a_lone_second_token_skips_and_says_so(second_account):
    second_account("github")
"""


def run_probe(tmp_path: Path, env_extra: dict[str, str], extra: list[str]) -> str:
    probe = tmp_path / "test_probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    env = {
        k: v for k, v in os.environ.items() if k not in CI_VARS and not k.startswith("FORGECTL_")
    }
    env.update(env_extra)
    env["PYTEST_ADDOPTS"] = ""
    env["PYTHONPATH"] = str(REPO)
    argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]
    argv += ["-p", "tests.harness.fixtures", "-p", "tests.live.conftest"]
    argv += ["-c", str(REPO / "pyproject.toml"), "--rootdir", str(REPO)]
    argv += ["--run-live", "-rs", "-q", str(probe), *extra]
    done = subprocess.run(
        argv, env=env, capture_output=True, text=True, cwd=REPO, check=False, timeout=120
    )
    return done.stdout + done.stderr


def test_live_settings_survive_the_per_test_environment_clearing(tmp_path: Path) -> None:
    out = run_probe(
        tmp_path,
        {
            **{k: v for k, v in ENV.items() if k != "PATH"},
            "FORGECTL_LIVE_GITHUB_SECOND_TOKEN": "tok-live-0002",
        },
        ["-k", "survive or cleared or fixtures_see"],
    )
    assert "1 passed" in out, out


def test_a_lone_second_token_skips_with_a_half_configured_reason(tmp_path: Path) -> None:
    out = run_probe(
        tmp_path,
        {
            **{k: v for k, v in ENV.items() if k != "PATH"},
            "FORGECTL_LIVE_GITHUB_SECOND_TOKEN": "tok-live-0002",
        },
        ["-k", "lone_second"],
    )
    assert "half-configured" in out and "tok-live-0002" not in out, out
