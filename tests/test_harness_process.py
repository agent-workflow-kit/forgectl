"""Shared process-test assertions, process hygiene, environment isolation and the live marker."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from .harness.fixtures import Isolation, IsolationError
from .harness.forges import Call, FakeForges
from .harness.procs import ProcessRegistry
from .harness.safety import assert_call_safe, reference_env

REPO = Path(__file__).resolve().parent.parent
PLANTED = "UNTRUSTED-PLANT-9f3a"


def one_call(
    procs: ProcessRegistry,
    forges: FakeForges,
    tmp_path: Path,
    *,
    backend: str = "gh",
    host: str = "github.com",
    argv_extra: list[str] | None = None,
    env_extra: dict[str, str] | None = None,
    drop: tuple[str, ...] = (),
) -> Call:
    forges.allow_unmatched = True
    env, cwd = reference_env(backend, host, root=tmp_path, path=forges.path())
    for name in drop:
        env.pop(name, None)
    env.update(env_extra or {})
    host_args = ["--hostname", host]
    procs.run([backend, "api", *host_args, "user", *(argv_extra or [])], env=env, cwd=cwd)
    return forges.calls()[-1]


# Acceptance: a sample process test fails when a token variable leaks or argv holds planted text.


def test_a_well_formed_call_passes_every_safety_rule(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    call = one_call(procs, fake_forges, tmp_path)
    assert_call_safe(call, host="github.com", untrusted=[PLANTED])


def test_a_second_token_variable_is_caught(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    call = one_call(procs, fake_forges, tmp_path, env_extra={"GITHUB_TOKEN": "leak"})
    with pytest.raises(AssertionError, match="token variables"):
        assert_call_safe(call, host="github.com")


def test_an_inherited_ambient_token_is_caught_even_under_another_name(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    call = one_call(procs, fake_forges, tmp_path, env_extra={"SOME_OTHER": "ambient-secret"})
    with pytest.raises(AssertionError, match="ambient value|stray"):
        assert_call_safe(call, host="github.com", ambient={"GH_TOKEN": "ambient-secret"})


def test_the_wrong_variable_for_the_host_is_caught(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    call = one_call(procs, fake_forges, tmp_path)
    with pytest.raises(AssertionError, match="want only GH_ENTERPRISE_TOKEN"):
        assert_call_safe(call, host="ghe.example.test")


def test_planted_untrusted_text_in_argv_is_caught(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    call = one_call(procs, fake_forges, tmp_path, argv_extra=[f"x?q={PLANTED}"])
    with pytest.raises(AssertionError, match="untrusted text"):
        assert_call_safe(call, host="github.com", untrusted=[PLANTED])


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("GH_HOST", "x.example.test", "never pass"),
        ("GH_FORCE_TTY", "1", "never pass"),
        ("NO_COLOR", "0", "NO_COLOR"),
        ("GLAB_SEND_TELEMETRY", "true", "GLAB_SEND_TELEMETRY"),
        ("EDITOR", "vi", "stray"),
    ],
)
def test_environment_rules_are_enforced(
    procs: ProcessRegistry,
    fake_forges: FakeForges,
    tmp_path: Path,
    name: str,
    value: str,
    match: str,
) -> None:
    call = one_call(procs, fake_forges, tmp_path, env_extra={name: value})
    with pytest.raises(AssertionError, match=match):
        assert_call_safe(call, host="github.com")


def test_a_missing_telemetry_setting_is_caught(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    call = one_call(procs, fake_forges, tmp_path, drop=("GH_NO_UPDATE_NOTIFIER",))
    with pytest.raises(AssertionError, match="GH_NO_UPDATE_NOTIFIER"):
        assert_call_safe(call, host="github.com")


def test_a_relative_path_entry_is_caught(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    path = fake_forges.path() + os.pathsep + "relative/bin"
    call = one_call(procs, fake_forges, tmp_path, env_extra={"PATH": path})
    with pytest.raises(AssertionError, match="not absolute"):
        assert_call_safe(call, host="github.com")


def test_a_non_empty_or_ancestor_repo_cwd_is_caught(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    fake_forges.allow_unmatched = True
    env, cwd = reference_env("gh", "github.com", root=tmp_path, path=fake_forges.path())
    (cwd / "leftover").write_text("x")
    procs.run(["gh", "api", "--hostname", "github.com", "user"], env=env, cwd=cwd)
    with pytest.raises(AssertionError, match="cwd not empty"):
        assert_call_safe(fake_forges.calls()[-1], host="github.com")


def test_a_discoverable_repository_is_caught(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    fake_forges.allow_unmatched = True
    env, cwd = reference_env("gh", "github.com", root=tmp_path, path=fake_forges.path())
    (cwd / ".git").mkdir()
    cwd.chmod(0o700)
    procs.run(["gh", "api", "--hostname", "github.com", "user"], env=env, cwd=cwd)
    with pytest.raises(AssertionError):
        assert_call_safe(fake_forges.calls()[-1], host="github.com")


def test_stored_login_mode_forbids_any_token_variable(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    fake_forges.allow_unmatched = True
    env, cwd = reference_env(
        "gh", "github.com", root=tmp_path, path=fake_forges.path(), mode="stored"
    )
    procs.run(["gh", "api", "--hostname", "github.com", "user"], env=env, cwd=cwd)
    call = fake_forges.calls()[-1]
    assert_call_safe(call, host="github.com", mode="stored")
    call.env["GH_TOKEN"] = "x"
    with pytest.raises(AssertionError, match="stored-login mode"):
        assert_call_safe(call, host="github.com", mode="stored")


def test_the_glab_enterprise_shape_passes(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    call = one_call(procs, fake_forges, tmp_path, backend="glab", host="gitlab.com")
    assert_call_safe(call, host="gitlab.com")


# The CLI runner runs the real entry point as a tracked child.


def test_run_forgectl_runs_the_real_cli_isolated(run_forgectl: object) -> None:
    done = run_forgectl(["--version"])  # type: ignore[operator]
    assert done.returncode == 0 and done.stdout.strip()


# Process hygiene.


def test_launched_children_are_recorded_with_pid_and_group(procs: ProcessRegistry) -> None:
    proc = procs.popen(["sleep", "60"], env=dict(os.environ))
    rec = procs.launched[0]
    assert (rec.pid, rec.pgid) == (proc.pid, proc.pid)
    assert os.getpgid(proc.pid) == proc.pid  # its own group, shared with nobody else


def test_teardown_stops_exactly_the_recorded_children_and_their_group() -> None:
    registry = ProcessRegistry()
    script = "import os, time; os.system('sleep 60 &'); time.sleep(60)"
    leader = registry.popen([sys.executable, "-c", script], env=dict(os.environ))
    bystander = subprocess.Popen(["sleep", "60"], env=dict(os.environ))  # not ours to stop
    time.sleep(0.5)
    try:
        forced = registry.terminate_all()
        assert leader.poll() is not None
        assert bystander.poll() is None
        assert forced == []  # SIGTERM was enough
    finally:
        bystander.kill()
        bystander.wait()


def test_a_child_that_ignores_sigterm_is_force_killed_and_reported() -> None:
    registry = ProcessRegistry()
    script = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('up', flush=True); time.sleep(60)"
    )
    proc = registry.popen(
        [sys.executable, "-c", script], env=dict(os.environ), stdout=subprocess.PIPE
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == b"up"
    forced = registry.terminate_all()
    assert [f.pid for f in forced] == [proc.pid]
    assert proc.returncode == -signal.SIGKILL


def test_a_reaped_child_is_never_signaled(procs: ProcessRegistry) -> None:
    proc = procs.popen(["true"], env=dict(os.environ))
    proc.wait()
    assert procs.signal(proc, signal.SIGTERM) is False


def test_signal_refuses_a_process_it_did_not_start(procs: ProcessRegistry) -> None:
    stranger = subprocess.Popen(["sleep", "60"], env=dict(os.environ))
    try:
        with pytest.raises(ValueError):
            procs.signal(stranger, signal.SIGTERM)
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()


def test_a_timed_out_run_is_killed_not_leaked(procs: ProcessRegistry) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        procs.run(["sleep", "60"], env=dict(os.environ), timeout=0.3)
    assert procs.launched[0].proc.returncode is not None


# Isolation guard.


def test_every_test_runs_with_private_home_and_config_dirs(isolation: Isolation) -> None:
    for name in ("HOME", "XDG_CONFIG_HOME", "GH_CONFIG_DIR", "GLAB_CONFIG_DIR", "TMPDIR"):
        assert os.environ[name].startswith(str(isolation.root)), name
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN", "GITLAB_HOST", "GH_HOST"):
        assert name not in os.environ


def test_the_guard_rejects_a_child_with_the_real_home(real_home: str) -> None:
    with pytest.raises(IsolationError):
        subprocess.Popen(["true"], env={"HOME": real_home})


def test_the_guard_rejects_a_child_with_no_home() -> None:
    with pytest.raises(IsolationError):
        subprocess.Popen(["true"], env={"PATH": "/usr/bin"})


def test_the_guard_rejects_a_real_xdg_dir(real_home: str, isolation: Isolation) -> None:
    env = {"HOME": str(isolation.home), "XDG_CONFIG_HOME": real_home + "/.config"}
    with pytest.raises(IsolationError):
        subprocess.Popen(["true"], env=env)


def test_the_guard_checks_the_inherited_environment_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch, real_home: str
) -> None:
    monkeypatch.setenv("HOME", real_home)
    with pytest.raises(IsolationError):
        subprocess.Popen(["true"])


def test_a_child_with_private_dirs_is_allowed(isolation: Isolation) -> None:
    subprocess.run(["true"], env={"HOME": str(isolation.home)}, check=True)


# The live marker.


def test_live_tests_are_deselected_by_a_plain_pytest_run(tmp_path: Path) -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    options = pyproject["tool"]["pytest"]["ini_options"]
    assert any(m.startswith("live:") for m in options["markers"])
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "test_probe.py").write_text(
        "import pytest\n\n@pytest.mark.live\ndef test_live():\n    raise SystemExit('ran')\n\n"
        "def test_plain():\n    pass\n",
        encoding="utf-8",
    )
    ini = probe / "pytest.ini"
    ini.write_text(
        "[pytest]\nmarkers =\n    live: x\naddopts = " + options["addopts"] + "\n",
        encoding="utf-8",
    )
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(ini), "-p", "no:cacheprovider", str(probe)],
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        cwd=probe,
        check=False,
    )
    assert "1 passed, 1 deselected" in done.stdout, done.stdout


def test_ci_does_not_select_live_tests() -> None:
    workflows = REPO / ".github" / "workflows"
    for path in workflows.glob("*.yml"):
        assert "-m live" not in path.read_text(encoding="utf-8"), path
