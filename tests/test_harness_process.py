"""Shared process-test assertions, process hygiene, environment isolation and the live marker."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .harness import fixtures
from .harness.fixtures import Isolation, IsolationError, forgectl_env
from .harness.forges import Call, FakeForges, shebang_limit
from .harness.procs import ProcessRegistry
from .harness.safety import AMBIENT_SENTINELS, AMBIENT_TOKEN_VARS, assert_call_safe, reference_env

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
    host_args: list[str] | None = None,
    gitlab_host: str | None = None,
) -> Call:
    forges.allow_unmatched = True
    env, cwd = reference_env(
        backend, host, root=tmp_path, path=forges.path(), gitlab_host=gitlab_host
    )
    for name in drop:
        env.pop(name, None)
    env.update(env_extra or {})
    if host_args is None:
        # interface.md, Routing: gh and gitlab.com take --hostname; self-managed glab does not.
        host_args = ["--hostname", host] if backend == "gh" or host == "gitlab.com" else []
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


def test_planted_ambient_sentinels_are_checked_by_default(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    leaked = AMBIENT_SENTINELS["GITLAB_ACCESS_TOKEN"]
    call = one_call(procs, fake_forges, tmp_path, env_extra={"HTTPS_PROXY": f"http://{leaked}:x@p"})
    with pytest.raises(AssertionError, match="ambient value of GITLAB_ACCESS_TOKEN"):
        assert_call_safe(call, host="github.com")


def test_the_child_token_inside_another_variable_is_caught(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    proxy = "http://u:tok-fixture-0001@proxy.example.test:3128"
    call = one_call(procs, fake_forges, tmp_path, env_extra={"HTTPS_PROXY": proxy})
    with pytest.raises(AssertionError, match="GH_TOKEN value is also inside"):
        assert_call_safe(call, host="github.com")


def test_the_child_token_in_argv_is_caught(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    call = one_call(procs, fake_forges, tmp_path, argv_extra=["-H", "X: tok-fixture-0001"])
    with pytest.raises(AssertionError, match="GH_TOKEN value is in argv"):
        assert_call_safe(call, host="github.com")


def test_the_wrong_variable_for_the_host_is_caught(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    call = one_call(procs, fake_forges, tmp_path, host_args=["--hostname", "ghe.example.test"])
    with pytest.raises(AssertionError, match="want only GH_ENTERPRISE_TOKEN"):
        assert_call_safe(call, host="ghe.example.test")


def test_planted_untrusted_text_in_argv_is_caught(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    call = one_call(procs, fake_forges, tmp_path, argv_extra=[f"x?q={PLANTED}"])
    with pytest.raises(AssertionError, match="untrusted text"):
        assert_call_safe(call, host="github.com", untrusted=[PLANTED])


def test_planted_text_split_across_argv_elements_is_caught(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    call = one_call(procs, fake_forges, tmp_path, argv_extra=["-f", PLANTED[:9], PLANTED[9:]])
    with pytest.raises(AssertionError, match="split across argv"):
        assert_call_safe(call, host="github.com", untrusted=[PLANTED])


def test_planted_text_in_the_environment_is_caught(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    call = one_call(procs, fake_forges, tmp_path, env_extra={"LC_ALL": PLANTED})
    with pytest.raises(AssertionError, match="in the environment"):
        assert_call_safe(call, host="github.com", untrusted=[PLANTED])


@pytest.mark.parametrize(
    ("backend", "host", "host_args", "env_extra", "match"),
    [
        ("gh", "github.com", [], {}, "exactly one --hostname"),
        ("gh", "github.com", ["--hostname", "other.example.test"], {}, "exactly one --hostname"),
        ("glab", "gitlab.com", [], {}, "--hostname gitlab.com"),
        ("glab", "gitlab.com", None, {"GITLAB_HOST": "gitlab.com"}, "GITLAB_HOST"),
        ("glab", "gl.example.test", ["--hostname", "gl.example.test"], {}, "takes no --hostname"),
        ("glab", "gl.example.test", None, {"GITLAB_HOST": "gl.example.test/x"}, "GITLAB_HOST"),
    ],
)
def test_routing_rules_are_enforced(
    procs: ProcessRegistry,
    fake_forges: FakeForges,
    tmp_path: Path,
    backend: str,
    host: str,
    host_args: list[str] | None,
    env_extra: dict[str, str],
    match: str,
) -> None:
    call = one_call(
        procs,
        fake_forges,
        tmp_path,
        backend=backend,
        host=host,
        host_args=host_args,
        env_extra=env_extra,
    )
    with pytest.raises(AssertionError, match=match):
        assert_call_safe(call, host=host)


def test_hostname_with_equals_passes(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    call = one_call(procs, fake_forges, tmp_path, host_args=["--hostname=github.com"])
    assert_call_safe(call, host="github.com")


def test_gh_auth_token_needs_the_hostname(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    env, cwd = reference_env("gh", "github.com", root=tmp_path, path=fake_forges.path())
    procs.run(["gh", "auth", "token"], env=env, cwd=cwd)
    with pytest.raises(AssertionError, match="exactly one --hostname"):
        assert_call_safe(fake_forges.calls()[-1], host="github.com")


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ("not-empty", "not empty at start"),
        ("mode", "mode 0o755"),
        ("in-cwd", "is in the cwd"),
        ("outside-tmpdir", "is not under TMPDIR"),
    ],
)
def test_the_token_mode_config_dir_must_be_private_and_empty(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path, change: str, match: str
) -> None:
    fake_forges.allow_unmatched = True
    root = tmp_path / "t"
    root.mkdir()
    env, cwd = reference_env("gh", "github.com", root=root, path=fake_forges.path())
    config = Path(env["GH_CONFIG_DIR"])
    if change == "not-empty":
        (config / "hosts.yml").write_text("", encoding="utf-8")
    elif change == "mode":
        config.chmod(0o755)
    elif change == "in-cwd":
        env["GH_CONFIG_DIR"] = str(cwd)
    else:
        other = tmp_path / "elsewhere"
        other.mkdir(mode=0o700)
        env["GH_CONFIG_DIR"] = str(other)
    procs.run(["gh", "api", "--hostname", "github.com", "user"], env=env, cwd=cwd)
    with pytest.raises(AssertionError, match=match):
        assert_call_safe(fake_forges.calls()[-1], host="github.com")


def test_tmpdir_must_be_present_and_absolute(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    call = one_call(procs, fake_forges, tmp_path, drop=("TMPDIR",))
    with pytest.raises(AssertionError, match="TMPDIR None is missing"):
        assert_call_safe(call, host="github.com")


def test_the_ceiling_is_compared_after_normalizing(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path
) -> None:
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "private")
    call = one_call(procs, fake_forges, tmp_path, env_extra={"GIT_CEILING_DIRECTORIES": f"{link}/"})
    assert_call_safe(call, host="github.com")


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


@pytest.mark.parametrize("ceiling", [None, "/nonexistent-ceiling"])
def test_a_discoverable_repository_is_caught(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path, ceiling: str | None
) -> None:
    fake_forges.allow_unmatched = True
    env, cwd = reference_env("gh", "github.com", root=tmp_path, path=fake_forges.path())
    (tmp_path / ".git").mkdir()  # an ancestor repository, above the private dirs
    if ceiling is None:
        del env["GIT_CEILING_DIRECTORIES"]
    else:
        env["GIT_CEILING_DIRECTORIES"] = ceiling
    procs.run(["gh", "api", "--hostname", "github.com", "user"], env=env, cwd=cwd)
    with pytest.raises(AssertionError, match="discoverable"):
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


@pytest.mark.parametrize("bus", ["DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"])
def test_stored_mode_with_a_caller_config_dir_forbids_bus_variables(
    procs: ProcessRegistry, fake_forges: FakeForges, tmp_path: Path, bus: str
) -> None:
    fake_forges.allow_unmatched = True
    env, cwd = reference_env(
        "gh", "github.com", root=tmp_path, path=fake_forges.path(), mode="stored"
    )
    env.update({"GH_CONFIG_DIR": str(tmp_path / "cfg"), bus: str(tmp_path / "run")})
    procs.run(["gh", "api", "--hostname", "github.com", "user"], env=env, cwd=cwd)
    call = fake_forges.calls()[-1]
    assert_call_safe(call, host="github.com", mode="stored")
    with pytest.raises(AssertionError, match=bus):
        assert_call_safe(call, host="github.com", mode="stored", caller_config_dir=True)


@pytest.mark.parametrize(
    ("host", "gitlab_host"), [("gitlab.com", None), ("gl.example.test", "gl.example.test:8443/pre")]
)
def test_the_glab_shapes_pass(
    procs: ProcessRegistry,
    fake_forges: FakeForges,
    tmp_path: Path,
    host: str,
    gitlab_host: str | None,
) -> None:
    call = one_call(
        procs, fake_forges, tmp_path, backend="glab", host=host, gitlab_host=gitlab_host
    )
    assert_call_safe(call, host=host, gitlab_host=gitlab_host)


# The CLI runner runs the real entry point as a tracked child.


def test_run_forgectl_runs_the_real_cli_isolated(run_forgectl: object) -> None:
    done = run_forgectl(["--version"])  # type: ignore[operator]
    assert done.returncode == 0 and done.stdout.strip()


def test_run_forgectl_plants_ambient_sentinels_and_none_removes(fake_forges: FakeForges) -> None:
    env = forgectl_env(fake_forges, {"GH_TOKEN": None, "EXTRA": "1"})
    assert {k: env.get(k) for k in AMBIENT_TOKEN_VARS if k != "GH_TOKEN"} == {
        k: v for k, v in AMBIENT_SENTINELS.items() if k != "GH_TOKEN"
    }
    assert "GH_TOKEN" not in env and env["EXTRA"] == "1"
    assert len(set(AMBIENT_SENTINELS.values())) == len(AMBIENT_TOKEN_VARS)


# Process hygiene.


def test_launched_children_are_recorded_with_pid_and_group(procs: ProcessRegistry) -> None:
    proc = procs.popen(["sleep", "60"], env=dict(os.environ))
    rec = procs.launched[0]
    assert (rec.pid, rec.pgid) == (proc.pid, proc.pid)
    assert os.getpgid(proc.pid) == proc.pid  # its own group, shared with nobody else


def _wait_for_pid(path: Path) -> int:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        text = path.read_text() if path.exists() else ""
        if text.strip():
            return int(text)
        time.sleep(0.02)
    raise AssertionError(f"no pid written to {path}")


def _gone(pid: int) -> bool:
    """Read-only: the process no longer exists (it was killed and reaped)."""
    stat = Path(f"/proc/{pid}/stat")
    try:
        state = stat.read_text().rpartition(")")[2].split()[0]
    except OSError:
        return True
    return state in ("Z", "X")


def _grandchild_script(pid_file: Path, *, setsid: bool = False, leader_waits: bool = True) -> str:
    inner = (
        "import os, time\n"
        + (
            "os.setsid()\n"
            "fd = os.open(os.devnull, os.O_RDWR)\n"
            "for n in (0, 1, 2):\n"
            "    os.dup2(fd, n)\n"
            if setsid
            else ""
        )
        + f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        + "time.sleep(60)\n"
    )
    wait = (
        f"while not os.path.exists({str(pid_file)!r}) or not open({str(pid_file)!r}).read():\n"
        "    time.sleep(0.02)\n"
    )
    return (
        "import os, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {inner!r}])\n" + (wait if leader_waits else "")
    )


def test_teardown_stops_exactly_the_recorded_children_and_their_group(tmp_path: Path) -> None:
    registry = ProcessRegistry()
    pid_file = tmp_path / "gc.pid"
    script = _grandchild_script(pid_file) + "time.sleep(60)\n"
    leader = registry.popen([sys.executable, "-c", script], env=dict(os.environ))
    bystander = subprocess.Popen(["sleep", "60"], env=dict(os.environ))  # not ours to stop
    grandchild = _wait_for_pid(pid_file)
    try:
        forced = registry.terminate_all()
        assert leader.poll() is not None
        assert _gone(grandchild)
        assert bystander.poll() is None
        assert forced == []  # SIGTERM was enough
    finally:
        bystander.kill()
        bystander.wait()


@pytest.mark.parametrize("pidfd", [True, False])
def test_run_kills_a_grandchild_left_behind_by_an_exited_leader(
    procs: ProcessRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pidfd: bool
) -> None:
    if not pidfd:
        monkeypatch.delattr(os, "pidfd_open", raising=False)  # the fallback path
    pid_file = tmp_path / "gc.pid"
    done = procs.run([sys.executable, "-c", _grandchild_script(pid_file)], env=dict(os.environ))
    assert done.returncode == 0
    grandchild = int(pid_file.read_text())
    assert _gone(grandchild)
    assert procs.launched[0].survivors == [grandchild]
    with pytest.raises(AssertionError, match="left processes"):
        procs.check_teardown()
    procs.allow_survivors = True


def test_an_escaped_grandchild_is_caught_by_the_leak_check(tmp_path: Path) -> None:
    registry = ProcessRegistry()
    pid_file = tmp_path / "gc.pid"
    before = fixtures.direct_children()
    registry.run(
        [sys.executable, "-c", _grandchild_script(pid_file, setsid=True)], env=dict(os.environ)
    )
    grandchild = int(pid_file.read_text())
    assert registry.launched[0].survivors == []  # outside the group, so the sweep missed it
    assert fixtures.reap_new_children(before) == [grandchild]
    assert _gone(grandchild)


def test_a_forced_child_fails_teardown_unless_allowed() -> None:
    registry = ProcessRegistry()
    script = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('up', flush=True); time.sleep(60)"
    )
    proc = registry.popen(
        [sys.executable, "-c", script], env=dict(os.environ), stdout=subprocess.PIPE
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == b"up"
    registry.terminate_all()
    with pytest.raises(AssertionError, match="needed SIGKILL"):
        registry.check_teardown()
    registry.allow_forced = True
    registry.check_teardown()


def test_a_leader_reaped_outside_popen_is_never_signaled(procs: ProcessRegistry) -> None:
    proc = procs.popen(["true"], env=dict(os.environ))
    os.waitpid(proc.pid, 0)  # reaped behind Popen's back: returncode is still None
    assert proc.returncode is None
    assert procs.signal(proc, signal.SIGTERM) is False


def test_run_decodes_strictly_and_keeps_the_raw_bytes(procs: ProcessRegistry) -> None:
    argv = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok\\xff')"]
    with pytest.raises(UnicodeDecodeError):
        procs.run(argv, env=dict(os.environ))
    raw = procs.run(argv, env=dict(os.environ), text=False)
    assert raw.stdout == b"ok\xff" and raw.stdout_bytes == b"ok\xff"


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


@pytest.mark.parametrize("name", ["HOME", "XDG_CONFIG_HOME", "GH_CONFIG_DIR", "XDG_RUNTIME_DIR"])
def test_the_guard_rejects_a_relative_dir(
    isolation: Isolation, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.chdir(isolation.home)  # a relative path that would resolve inside the root
    env = {"HOME": str(isolation.home), name: "rel"}
    with pytest.raises(IsolationError, match="absolute"):
        subprocess.Popen(["true"], env=env)


def test_the_guard_rejects_the_real_runtime_dir(isolation: Isolation) -> None:
    env = {"HOME": str(isolation.home), "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    with pytest.raises(IsolationError, match="XDG_RUNTIME_DIR"):
        subprocess.Popen(["true"], env=env)


@pytest.mark.parametrize("name", ["posix_spawn", "posix_spawnp", "execve"])
def test_the_guard_covers_os_spawn_and_exec(real_home: str, name: str) -> None:
    call = getattr(os, name)
    missing = "/nonexistent/forgectl-guard-probe"  # never runs: the guard raises first
    with pytest.raises(IsolationError):
        call(missing, [missing], {"HOME": real_home})


def test_the_session_runtime_dir_is_private(isolation: Isolation) -> None:
    runtime = os.environ["XDG_RUNTIME_DIR"]
    assert runtime.startswith(str(isolation.root)) and (os.stat(runtime).st_mode & 0o777) == 0o700


@pytest.mark.parametrize(
    ("system", "release", "limit"),
    [
        ("Linux", "6.6.87.2-microsoft", 255),
        ("Linux", "5.1.0", 255),
        ("Linux", "5.0.21", 127),
        ("Linux", "4.19.0", 127),
        ("Darwin", "23.0.0", 127),
    ],
)
def test_the_shebang_limit_follows_the_kernel(system: str, release: str, limit: int) -> None:
    assert shebang_limit(system, release) == limit


# The live marker.


def test_live_tests_are_deselected_by_a_plain_pytest_run() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    options = pyproject["tool"]["pytest"]["ini_options"]
    assert any(m.startswith("live:") for m in options["markers"])
    assert options["addopts"] == "-m 'not live'"


def _probe(tmp_path: Path, args: list[str], env: dict[str, str]) -> str:
    probe = tmp_path / "probe"
    probe.mkdir(exist_ok=True)
    (probe / "conftest.py").write_text('pytest_plugins = ("tests.harness.fixtures",)\n')
    (probe / "test_probe.py").write_text(
        "import pytest\n\n" + "@pytest.mark.live\ndef test_live():\n    pytest.fail('ran')\n\n"
        "def test_plain():\n    pass\n",
        encoding="utf-8",
    )
    ini = probe / "pytest.ini"
    ini.write_text("[pytest]\nmarkers =\n    live: x\n    slow: x\n", encoding="utf-8")
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(ini),
            "--rootdir",
            str(probe),
            "-p",
            "no:cacheprovider",
            *args,
            str(probe),
        ],
        env={**env, "PYTEST_ADDOPTS": "", "PYTHONPATH": str(REPO)},
        capture_output=True,
        text=True,
        cwd=probe,
        check=False,
        timeout=120,
    )
    return done.stdout


@pytest.mark.parametrize(
    ("args", "ci", "summary"),
    [
        (["-m", "not slow"], False, "1 passed, 1 skipped"),  # -m overrides addopts
        (["-m", "not slow", "--run-live"], False, "1 failed, 1 passed"),
        (["--run-live"], True, "1 passed, 1 skipped"),
    ],
)
def test_the_live_gate_skips_live_tests_unless_asked_outside_ci(
    tmp_path: Path, args: list[str], ci: bool, summary: str
) -> None:
    env = {k: v for k, v in os.environ.items() if k not in fixtures.CI_VARS}
    if ci:
        env["CI"] = ""
    out = _probe(tmp_path, args, env)
    assert summary in out, out


def test_the_live_gate_hook_marks_only_live_items(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in fixtures.CI_VARS:
        monkeypatch.delenv(name, raising=False)
    added: list[Any] = []

    def item(live: bool) -> Any:
        return SimpleNamespace(
            get_closest_marker=lambda name: object() if live and name == "live" else None,
            add_marker=lambda marker: added.append((live, marker)),
        )

    config: Any = SimpleNamespace(getoption=lambda name: False)
    fixtures.pytest_collection_modifyitems(config, [item(True), item(False)])
    assert [live for live, _ in added] == [True]
    assert (
        fixtures.live_skip_reason(True, {"GITHUB_ACTIONS": "true"}) == "live tests never run in CI"
    )
    assert fixtures.live_skip_reason(True, {}) is None
