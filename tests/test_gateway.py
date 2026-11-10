"""The gateway (#7): every forge call goes through one boundary that builds the environment, the
cwd, the argv and the deadline, and turns the answer into data or a typed error.

Process tests run the real gateway against the fake ``gh`` and ``glab`` of ``tests/fakebin`` and
assert on the environment the gateway itself built, as logged by the fake. Nothing here compares
against ``safety.reference_env`` alone.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import sys
import threading
import time
import types
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from forgectl import gateway as gw
from forgectl.config import LoadedConfig
from forgectl.errors import ForgectlError
from forgectl.gateway import AnswerCut, Gateway, ProcessResult, Target

from .harness.fixtures import forgectl_env
from .harness.forges import Call, FakeForges
from .harness.procs import ProcessRegistry, group_members
from .harness.safety import assert_call_safe

TOKEN = "fake-token-0001"
GH = Target("github", "github.com", token=TOKEN)
GHE = Target("github", "ghe.example.test", token=TOKEN)
GHE_CLOUD = Target("github", "acme.ghe.com", token=TOKEN)
GL = Target("gitlab", "gitlab.com", token=TOKEN)
GL_SELF = Target("gitlab", "git.example.test", token=TOKEN)
GL_PREFIX = Target("gitlab", "git.example.test", api_prefix="/gitlab", token=TOKEN)


@pytest.fixture(autouse=True)
def default_sigint() -> Iterator[None]:
    """A shell `&` job (and `nohup`-style launchers) hands its children an ignored SIGINT, so
    Python never installs its KeyboardInterrupt handler and the signal tests below fail in
    confusing ways. These tests are about the default disposition: set it for each test."""
    current = signal.getsignal(signal.SIGINT)
    if current is signal.SIG_IGN:
        signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        if current is signal.SIG_IGN:
            signal.signal(signal.SIGINT, signal.SIG_IGN)


class Spawned:
    """Every child the gateway starts, as (pid, pgid), recorded at launch."""

    def __init__(self) -> None:
        self.children: list[tuple[int, int]] = []

    def __call__(self, pid: int, pgid: int) -> None:
        self.children.append((pid, pgid))

    def assert_gone(self) -> None:
        assert self.children, "nothing was started"
        for pid, pgid in self.children:
            members = [m for m in group_members(pgid) if m[2] != "Z"]
            assert members == [], f"group {pgid} (leader {pid}) still has {members}"


@pytest.fixture
def spawned() -> Spawned:
    return Spawned()


def make_gateway(
    forges: FakeForges,
    spawned: Spawned | None = None,
    *,
    timeout: float = 20.0,
    env: Mapping[str, str | None] | None = None,
    **kwargs: Any,
) -> Gateway:
    environ = forgectl_env(forges, env)
    return Gateway(
        LoadedConfig.defaults(), timeout=timeout, environ=environ, on_spawn=spawned, **kwargs
    )


def private_leftovers() -> list[str]:
    return sorted(p for p in os.listdir(os.environ["TMPDIR"]) if p.startswith("forgectl-"))


def only_call(forges: FakeForges) -> Call:
    calls = forges.calls()
    assert len(calls) == 1, calls
    return calls[0]


# -- Encoding -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ("plain-._~09AZaz", "plain-._~09AZaz"),
        ("a/b", "a%2Fb"),
        ("{owner}", "%7Bowner%7D"),
        (":id", "%3Aid"),
        ("--hostname=evil", "--hostname%3Devil"),
        ("a b", "a%20b"),
        ("100%", "100%25"),
        ("q?x#y&z=1", "q%3Fx%23y%26z%3D1"),
        ("café", "caf%C3%A9"),
        ("‮\u0000\n", "%E2%80%AE%00%0A"),
        ("\U0001f600", "%F0%9F%98%80"),
    ],
)
def test_quote_component_encodes_everything_but_unreserved(value: str, encoded: str) -> None:
    assert gw.quote_component(value) == encoded


def test_quote_component_refuses_a_lone_surrogate_and_non_strings() -> None:
    with pytest.raises(ValueError):
        gw.quote_component("a\ud800")
    with pytest.raises(TypeError):
        gw.quote_component(b"x")  # type: ignore[arg-type]


def test_build_endpoint_joins_encoded_segments_and_sorted_query_in_given_order() -> None:
    endpoint = gw.build_endpoint(
        ["repos", "o", "r", "pulls", "1"], [("per_page", "100"), ("q", "a b")]
    )
    assert endpoint == "repos/o/r/pulls/1?per_page=100&q=a%20b"


@pytest.mark.parametrize(
    "segments",
    [[], [""], ["repos", ""], ["repos", ".."], ["repos", "."], ["-x"], ["_x"], ["%41"], ["~x"]],
)
def test_build_endpoint_refuses_unusable_segments(segments: list[str]) -> None:
    with pytest.raises(ValueError):
        gw.build_endpoint(segments)


def test_build_endpoint_refuses_an_endless_endpoint() -> None:
    with pytest.raises(ValueError):
        gw.build_endpoint(["repos", "x" * 9000])


def test_a_url_given_as_a_segment_is_encoded_never_a_full_url() -> None:
    endpoint = gw.build_endpoint(["repos", "https://evil.example.test/x"])
    assert endpoint == "repos/https%3A%2F%2Fevil.example.test%2Fx"


@pytest.mark.parametrize(
    ("value", "ok"),
    [(1, True), (0, True), ("12", True), (2**63 - 1, True), (-1, False), (True, False),
     (1.0, False), ("1.0", False), ("-3", False), ("", False), ("١", False),
     ("9" * 20, False), (2**63, False), (None, False)],
)  # fmt: skip
def test_path_int(value: object, ok: bool) -> None:
    if ok:
        assert gw.path_int(value) == str(int(value))  # type: ignore[call-overload]
    else:
        with pytest.raises(ValueError):
            gw.path_int(value)


# -- Targets ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["", "a/b", "a@b", "a b", "-a.test", "a.test\n", "a..", "x:99999999", "a.test:", "é.test",
     "a" * 300, "a.test/x", "[::1]", "a..b", "a.-b", "a-.b", "x:0", "x:65536", "x:00443",
     "\u212a.test", "a.test:8443\n"],
)  # fmt: skip
def test_target_refuses_hostile_hosts(host: str) -> None:
    with pytest.raises(ValueError):
        Target("gitlab", host)


@pytest.mark.parametrize(
    "prefix",
    ["gitlab", "/gitlab/", "/a/../b", "//a", "/a b", "/a?x", "/", "/.", "/./x", "/a/.", "/..",
     "/gitlab\n"],
)  # fmt: skip
def test_target_refuses_bad_api_prefixes(prefix: str) -> None:
    with pytest.raises(ValueError):
        Target("gitlab", "git.example.test", api_prefix=prefix)


def test_api_prefix_is_only_for_self_managed_gitlab() -> None:
    with pytest.raises(ValueError):
        Target("github", "git.example.test", api_prefix="/x")
    with pytest.raises(ValueError):
        Target("gitlab", "gitlab.com", api_prefix="/x")


@pytest.mark.parametrize("token", ["", "a b", "a\nb", "toké", "x" * 5000, "a\x00b", "a\tb"])
def test_a_token_that_is_not_one_printable_ascii_word_is_refused_without_echo(token: str) -> None:
    with pytest.raises(ForgectlError) as raised:
        Target("github", "github.com", token=token)
    assert raised.value.code == "auth_required"
    assert token not in raised.value.message or token == ""


@pytest.mark.parametrize(
    ("target", "variable"),
    [(GH, "GH_TOKEN"), (GHE, "GH_ENTERPRISE_TOKEN"), (GHE_CLOUD, "GH_TOKEN"),
     (GL, "GITLAB_TOKEN"), (GL_SELF, "GITLAB_TOKEN")],
)  # fmt: skip
def test_token_variable_by_backend_and_host(target: Target, variable: str) -> None:
    assert target.token_variable == variable


def test_a_target_host_is_lowercased_before_any_routing_or_token_decision(
    fake_forges: FakeForges,
) -> None:
    assert Target("github", "GitHub.com", token=TOKEN).token_variable == "GH_TOKEN"
    assert Target("github", "Acme.GHE.com", token=TOKEN).token_variable == "GH_TOKEN"
    assert Target("github", "GHE.Example.Test", token=TOKEN).host == "ghe.example.test"
    with pytest.raises(ValueError):
        Target("gitlab", "GitLab.com", api_prefix="/x")  # gitlab.com, whatever its case
    script_ok(fake_forges, "glab")
    script_ok(fake_forges, "gh")
    gateway = make_gateway(fake_forges)
    gateway.api(Target("gitlab", "GitLab.COM", token=TOKEN), "GET", ["user"])
    gateway.api(Target("github", "GitHub.com", token=TOKEN), "GET", ["user"])
    glab, gh = fake_forges.calls()
    assert_call_safe(glab, host="gitlab.com")
    assert_call_safe(gh, host="github.com")
    assert glab.token_sent == TOKEN and gh.token_sent == TOKEN


def test_a_github_host_with_a_port_is_refused_naming_the_host() -> None:
    with pytest.raises(ForgectlError) as raised:
        Target("github", "ghe.example.test:8443", token=TOKEN)
    assert raised.value.code == "config_invalid"
    assert raised.value.details == {"key": "hosts.ghe.example.test:8443"}
    assert Target("gitlab", "git.example.test:8443").host == "git.example.test:8443"


# -- Environment, cwd and argv through the fakes (both modes) ----------------------------------


def script_ok(forges: FakeForges, backend: str, body: Any = None, **kwargs: Any) -> None:
    forges.answer(backend, endpoint_re=".", body={"ok": True} if body is None else body, **kwargs)


@pytest.mark.parametrize(
    ("target", "host", "gitlab_host"),
    [(GH, "github.com", None), (GHE, "ghe.example.test", None), (GHE_CLOUD, "acme.ghe.com", None),
     (GL, "gitlab.com", None), (GL_SELF, "git.example.test", None),
     (GL_PREFIX, "git.example.test", "git.example.test/gitlab"),
     (Target("gitlab", "git.example.test:8443", token=TOKEN), "git.example.test:8443", None)],
)  # fmt: skip
def test_token_mode_environment_is_safe_for_every_host_kind(
    fake_forges: FakeForges, spawned: Spawned, target: Target, host: str, gitlab_host: str | None
) -> None:
    script_ok(fake_forges, target.program)
    gateway = make_gateway(fake_forges, spawned)
    response = gateway.api(target, "GET", ["repos", "o", "r"])
    assert response.status == 200 and response.data == {"ok": True}
    call = only_call(fake_forges)
    assert_call_safe(call, host=host, mode="token", gitlab_host=gitlab_host)
    assert call.token_sent == TOKEN
    assert call.argv[0] == "api"
    spawned.assert_gone()
    assert private_leftovers() == []


def test_self_managed_gitlab_names_no_hostname_flag_and_gitlab_com_does(
    fake_forges: FakeForges,
) -> None:
    script_ok(fake_forges, "glab")
    gateway = make_gateway(fake_forges)
    gateway.api(GL, "GET", ["user"])
    gateway.api(GL_SELF, "GET", ["user"])
    com, self_managed = fake_forges.calls()
    assert com.argv[com.argv.index("--hostname") + 1] == "gitlab.com"
    assert "--hostname" not in self_managed.argv


@pytest.mark.parametrize("target", [GH, GL_SELF])
def test_stored_login_mode_passes_the_callers_config_dirs_and_no_bus_for_them(
    fake_forges: FakeForges, spawned: Spawned, target: Target, tmp_path: Path
) -> None:
    var = "GH_CONFIG_DIR" if target.backend == "github" else "GLAB_CONFIG_DIR"
    config = tmp_path / "my-config"
    FakeForges.stored_login(config, target.program, target.host, "stored-token-9")
    script_ok(fake_forges, target.program)
    env = {
        var: str(config),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x",
        "XDG_RUNTIME_DIR": "/run/x",
    }
    gateway = make_gateway(fake_forges, spawned, env=env)
    stored = Target(target.backend, target.host)
    gateway.api(stored, "GET", ["user"])
    call = only_call(fake_forges)
    assert_call_safe(call, host=target.host, mode="stored", caller_config_dir=True)
    assert call.env[var] == str(config)
    assert call.token_sent == "stored-token-9"
    # the caller's directory is used as it is: nothing private is made or put in it
    assert call.config_dir == str(config)
    assert call.config_entries == ["hosts.yml" if target.backend == "github" else "config.yml"]
    spawned.assert_gone()


def test_stored_login_mode_keeps_the_bus_when_only_xdg_config_home_is_set(
    fake_forges: FakeForges, tmp_path: Path
) -> None:
    script_ok(fake_forges, "gh")
    FakeForges.stored_login(tmp_path / "xdg" / "gh", "gh", "github.com", "stored-token")
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "GH_CONFIG_DIR": None,
        "GLAB_CONFIG_DIR": None,
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x",
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
    }
    make_gateway(fake_forges, env=env).api(Target("github", "github.com"), "GET", ["user"])
    call = only_call(fake_forges)
    assert_call_safe(call, host="github.com", mode="stored", caller_config_dir=False)
    assert call.env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/x"
    assert "GH_CONFIG_DIR" not in call.env
    assert call.env["XDG_CONFIG_HOME"] == str(tmp_path / "xdg")


def test_stored_login_with_no_credential_on_github_is_auth_required(
    fake_forges: FakeForges,
) -> None:
    gateway = make_gateway(fake_forges, env={"GH_CONFIG_DIR": None, "XDG_CONFIG_HOME": None})
    with pytest.raises(ForgectlError) as raised:
        gateway.api(Target("github", "github.com"), "GET", ["user"])
    assert raised.value.code == "auth_required"
    assert only_call(fake_forges).exit == 4  # gh exits 4 with no headers


HOSTILE_ENV = {
    "GH_HOST": "evil.example.test",
    "GH_REPO": "evil/repo",
    "GH_FORCE_TTY": "100",
    "CLICOLOR_FORCE": "1",
    "GITHUB_TOKEN": "ambient-github-token",
    "GITHUB_REPOSITORY": "x/y",
    "GITHUB_ACTIONS": "true",
    "GITLAB_CI": "true",
    "GITLAB_URI": "https://evil.example.test",
    "GL_HOST": "evil.example.test",
    "GLAB_TOKEN": "ambient-glab-token",
    "GH_DEBUG": "api",
    "GH_PAGER": "evil",
    "GLAB_PAGER": "evil",
    "PAGER": "evil",
    "BROWSER": "evil",
    "EDITOR": "evil",
    "GIT_DIR": "/nonexistent",
    "GIT_WORK_TREE": "/nonexistent",
    "GIT_SSH_COMMAND": "evil",
    "GIT_ASKPASS": "evil",
    "GIT_CONFIG_GLOBAL": "/nonexistent",
    "LD_PRELOAD": "/nonexistent.so",
    "LD_LIBRARY_PATH": "/nonexistent",
    "DYLD_INSERT_LIBRARIES": "/nonexistent",
    "PYTHONPATH": "/nonexistent",
    "NO_PROMPT": "0",
    "GLAB_NO_PROMPT": "0",
    "GH_PROMPT_DISABLED": "0",
    "ALL_PROXY": "http://evil.example.test",
    "SSH_AUTH_SOCK": "/nonexistent",
    "FORGECTL_GH_BIN": "evil",
    "FORGEFAKE_STATE": "/nonexistent",
}


@pytest.mark.parametrize("target", [GH, GHE, GL, GL_SELF])
def test_no_ambient_variable_leaks_in_token_mode(fake_forges: FakeForges, target: Target) -> None:
    script_ok(fake_forges, target.program)
    make_gateway(fake_forges, env=HOSTILE_ENV).api(target, "GET", ["x"])
    call = only_call(fake_forges)
    assert_call_safe(call, host=target.host, mode="token")
    for name in HOSTILE_ENV:
        if name in ("NO_PROMPT", "GLAB_NO_PROMPT", "GH_PROMPT_DISABLED"):
            continue
        assert name not in call.env, name
    assert "NO_PROMPT" not in call.env
    assert call.env["GLAB_NO_PROMPT"] == "1" and call.env["GH_PROMPT_DISABLED"] == "1"


def test_no_ambient_variable_leaks_in_stored_mode(fake_forges: FakeForges) -> None:
    script_ok(fake_forges, "glab")
    make_gateway(fake_forges, env=HOSTILE_ENV).api(Target("gitlab", "gitlab.com"), "GET", ["x"])
    call = only_call(fake_forges)
    assert_call_safe(call, host="gitlab.com", mode="stored", caller_config_dir=True)
    assert "NO_PROMPT" not in call.env


def test_proxy_and_ca_variables_pass_through_in_both_modes(fake_forges: FakeForges) -> None:
    script_ok(fake_forges, "glab")
    env = {
        "HTTPS_PROXY": "http://proxy.example.test:3128",
        "https_proxy": "http://proxy.example.test:3128",
        "NO_PROXY": "internal.example.test",
        "SSL_CERT_FILE": "/etc/ssl/x.pem",
        "SSL_CERT_DIR": "/etc/ssl/certs",
        "GLAB_CA_CERT": "/etc/ssl/ca.pem",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
    }
    gateway = make_gateway(fake_forges, env=env)
    gateway.api(GL, "GET", ["x"])
    gateway.api(Target("gitlab", "gitlab.com"), "GET", ["x"])
    assert len(fake_forges.calls()) == 2
    for call in fake_forges.calls():
        for name, value in env.items():
            assert call.env[name] == value


def test_environment_values_with_a_nul_are_dropped_not_passed() -> None:
    dirs = gw.PrivateDirs("/r", "/r/cwd", "/r/config")
    source = {"PATH": "/usr/bin", "HOME": "/h", "LANG": "a\0b", "HTTPS_PROXY": ""}
    env = gw.build_environment(source, dirs=dirs, backend="github")
    assert "LANG" not in env and "HTTPS_PROXY" not in env


def test_path_keeps_only_absolute_entries_and_resolution_ignores_cwd_and_relative(
    fake_forges: FakeForges, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evil = tmp_path / "evil"
    evil.mkdir()
    marker = tmp_path / "evil-ran"
    for name in ("gh", "glab"):
        script = evil / name
        script.write_text(f"#!/bin/sh\ntouch {marker}\n")
        script.chmod(0o755)
    (tmp_path / "rel").mkdir()
    shutil_target = tmp_path / "rel" / "gh"
    shutil_target.write_text(f"#!/bin/sh\ntouch {marker}\n")
    shutil_target.chmod(0o755)
    monkeypatch.chdir(evil)
    script_ok(fake_forges, "gh")
    bad_path = os.pathsep.join(
        ["", ".", "rel", "../evil", str(evil / "..") + "/nope", fake_forges.path()]
    )
    # Relative entries are only dangerous if the cwd holds them: ``.`` here is the evil dir.
    gateway = make_gateway(fake_forges, env={"PATH": bad_path})
    gateway.api(GH, "GET", ["x"])
    call = only_call(fake_forges)
    assert not marker.exists()
    for entry in call.env["PATH"].split(os.pathsep):
        assert os.path.isabs(entry), entry
    assert "" not in call.env["PATH"].split(os.pathsep)
    assert_call_safe(call, host="github.com")


def test_an_empty_or_all_relative_path_gets_a_safe_default_and_finds_nothing(
    fake_forges: FakeForges,
) -> None:
    gateway = make_gateway(fake_forges, env={"PATH": ".::rel"})
    with pytest.raises(ForgectlError) as raised:
        gateway.api(GH, "GET", ["x"])
    assert raised.value.code == "tool_missing"
    assert gw.child_path(".::rel") == "/usr/bin:/bin"
    assert gw.child_path(None) == "/usr/bin:/bin"
    assert fake_forges.calls() == []


def test_resolve_executable_rules(tmp_path: Path) -> None:
    exe = tmp_path / "tool"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    plain = tmp_path / "plain"
    plain.write_text("x")
    (tmp_path / "dir").mkdir()
    path = f"{tmp_path}{os.pathsep}rel"
    assert gw.resolve_executable("tool", path) == str(exe)
    assert gw.resolve_executable("plain", path) is None  # not executable
    assert gw.resolve_executable("dir", path) is None
    assert gw.resolve_executable("tool", "rel:.:") is None
    assert gw.resolve_executable(str(exe), "") == str(exe)
    assert gw.resolve_executable(str(tmp_path / "missing"), path) is None
    for bad in ("./tool", "sub/tool", "", "to\0ol"):
        with pytest.raises(ForgectlError) as raised:
            gw.resolve_executable(bad, path)
        assert raised.value.code == "config_invalid"


def test_resolve_executable_never_finds_a_program_in_the_cwd_through_a_relative_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = tmp_path / "here"
    here.mkdir()
    planted = here / "tool"
    planted.write_text("#!/bin/sh\n")
    planted.chmod(0o755)
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real = real_dir / "tool"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    monkeypatch.chdir(here)
    assert gw.resolve_executable("tool", ".") is None
    assert gw.resolve_executable("tool", ":") is None
    assert gw.resolve_executable("tool", f".{os.pathsep}{real_dir}") == str(real)


def test_missing_gh_is_tool_missing_only_for_github_and_names_the_install_command(
    fake_forges: FakeForges,
) -> None:
    script_ok(fake_forges, "glab")
    # Only a nonexistent dir on PATH: a real gh in /usr/bin (as on CI runners) must not be found.
    gateway = make_gateway(fake_forges, env={"PATH": str(fake_forges.bin_dir / "nowhere")})
    with pytest.raises(ForgectlError) as raised:
        gateway.api(GH, "GET", ["x"])
    assert raised.value.code == "tool_missing"
    assert "gh" in raised.value.details["install"]  # type: ignore[index]
    with pytest.raises(ForgectlError) as raised:
        gateway.api(GL, "GET", ["x"])
    assert "glab" in raised.value.details["install"]  # type: ignore[index]


def test_an_absolute_bin_that_is_not_executable_is_tool_missing(
    fake_forges: FakeForges, tmp_path: Path
) -> None:
    plain = tmp_path / "gh"
    plain.write_text("not executable")
    config = LoadedConfig.defaults()
    config = LoadedConfig(
        config=config.config.model_copy(update={"gh_bin": str(plain)}),
        config_hash="x",
        config_path=None,
    )
    gateway = Gateway(config, timeout=5, environ=forgectl_env(fake_forges))
    with pytest.raises(ForgectlError) as raised:
        gateway.api(GH, "GET", ["x"])
    assert raised.value.code == "tool_missing"


def test_a_planted_git_repository_above_the_private_directory_is_not_found(
    fake_forges: FakeForges,
) -> None:
    tmpdir = Path(os.environ["TMPDIR"])
    (tmpdir / ".git").mkdir()
    (tmpdir / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmpdir / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://evil.example.test/x/y\n'
    )
    script_ok(fake_forges, "gh")
    script_ok(fake_forges, "glab")
    gateway = make_gateway(fake_forges)
    gateway.api(GH, "GET", ["x"])
    gateway.api(GL, "GET", ["x"])
    for call in fake_forges.calls():
        assert call.repo_context is None
        assert call.cwd_entries == []
        assert call.cwd_mode == 0o700


def test_every_call_gets_a_fresh_empty_config_dir_and_removes_it(fake_forges: FakeForges) -> None:
    script_ok(fake_forges, "glab")
    gateway = make_gateway(fake_forges)
    for _ in range(3):
        gateway.api(GL, "GET", ["user"])
    calls = fake_forges.calls()
    assert len({c.config_dir for c in calls}) == 3
    for call in calls:
        assert_call_safe(call, host="gitlab.com")  # empty at start, 0700, under TMPDIR
        assert not Path(call.config_dir or "/x").exists()
        assert not Path(call.cwd).exists()
    assert private_leftovers() == []


def test_the_private_directory_tree_is_removed_even_when_the_call_fails(
    fake_forges: FakeForges,
) -> None:
    fake_forges.answer("gh", endpoint_re=".", status=500, body={"message": "x"})
    gateway = make_gateway(fake_forges)
    with pytest.raises(ForgectlError):
        gateway.api(GH, "GET", ["x"])
    assert private_leftovers() == []


# -- argv: no shell, no untrusted text in option position ---------------------------------------


UNTRUSTED = [
    "--hostname=evil.example.test",
    "-X DELETE",
    "{owner}/{repo}",
    ":id",
    "$(touch pwned)",
    "`touch pwned`",
    "a;b|c&d",
    "../../etc/passwd",
    "x\ny",
    "‮",
    "\x40file",
    "--input=/etc/passwd",
    "https://evil.example.test/x",
]


@pytest.mark.parametrize("target", [GH, GL, GL_PREFIX])
def test_untrusted_values_are_encoded_and_never_in_option_position(
    fake_forges: FakeForges, target: Target
) -> None:
    script_ok(fake_forges, target.program)
    gateway = make_gateway(fake_forges)
    for value in UNTRUSTED:
        gateway.api(target, "GET", ["repos", value, "x"], query=[("q", value), (value, "1")])
    calls = fake_forges.calls()
    assert len(calls) == len(UNTRUSTED)
    for call, value in zip(calls, UNTRUSTED, strict=True):
        assert_call_safe(
            call,
            host=target.host,
            gitlab_host=None if target is not GL_PREFIX else "git.example.test/gitlab",
        )
        *flags, sep, endpoint = call.argv
        assert sep == "--" and endpoint.startswith("repos/")
        assert not any(value in flag for flag in flags)
        assert value not in endpoint or value.isalnum()
        assert not Path("pwned").exists() and not (Path(call.cwd).parent / "pwned").exists()
        assert call.request is not None and not call.request["file_fields"]
        assert not call.placeholders


def test_the_text_body_goes_by_stdin_and_never_by_field_flags(fake_forges: FakeForges) -> None:
    script_ok(fake_forges, "gh", status=201, body={"id": 5})
    gateway = make_gateway(fake_forges)
    text = "hello \x40file $(x) ‮ `y` --input=x\né"
    gateway.api(
        GH, "POST", ["repos", "o", "r", "issues", "1", "comments"], body={"body": text},
        write=True, expect_status=frozenset({201}), shape="object",
    )  # fmt: skip
    call = only_call(fake_forges)
    assert_call_safe(call, host="github.com", untrusted=[text, "hello"])
    assert json.loads(call.stdin or "") == {"body": text}
    assert call.argv[call.argv.index("--input") + 1] == "-"
    for flag in ("-f", "-F", "--field", "--raw-field"):
        assert flag not in call.argv
    assert "Content-Type: application/json" in call.argv


def test_body_with_a_lone_surrogate_or_nan_is_refused_before_a_process_starts(
    fake_forges: FakeForges,
) -> None:
    gateway = make_gateway(fake_forges)
    for body in ({"body": "a\ud800"}, {"n": float("nan")}):
        with pytest.raises(ValueError):
            gateway.api(GH, "POST", ["x"], body=body, write=True, expect_status=frozenset({201}))
    assert fake_forges.calls() == []


@pytest.mark.parametrize(
    "headers",
    [{"Authorization": "x"}, {"private-token": "x"}, {"Cookie": "x"}, {"Host": "evil"},
     {"X-A\nB": "x"}, {"X-A": "a\nb"}, {"X A": "x"}, {"": "x"}, {"X-A": "é"}],
)  # fmt: skip
def test_headers_that_could_carry_a_credential_or_break_framing_are_refused(
    fake_forges: FakeForges, headers: dict[str, str]
) -> None:
    gateway = make_gateway(fake_forges)
    with pytest.raises(ValueError):
        gateway.api(GH, "GET", ["x"], headers=headers)
    assert fake_forges.calls() == []


def mixed_case(name: str) -> str:
    return "".join(c.upper() if i % 2 == 0 else c.lower() for i, c in enumerate(name))


@pytest.mark.parametrize("name", sorted(gw._FORBIDDEN_HEADERS))
def test_every_denied_header_is_refused_in_any_case(fake_forges: FakeForges, name: str) -> None:
    gateway = make_gateway(fake_forges)
    for spelled in (name, name.upper(), mixed_case(name)):
        with pytest.raises(ValueError):
            gateway.api(GH, "GET", ["x"], headers={spelled: "x"})
    assert fake_forges.calls() == []


def test_the_denied_header_list_holds_every_credential_and_framing_header() -> None:
    assert {"authorization", "proxy-authorization", "private-token", "job-token",
            "deploy-token", "cookie", "host", "content-length", "transfer-encoding",
            "connection"} <= gw._FORBIDDEN_HEADERS  # fmt: skip


@pytest.mark.parametrize("headers", [{"X-A": "v\n"}, {"X-A\n": "v"}])
def test_a_header_with_a_trailing_newline_is_refused(
    fake_forges: FakeForges, headers: dict[str, str]
) -> None:
    gateway = make_gateway(fake_forges)
    with pytest.raises(ValueError):
        gateway.api(GH, "GET", ["x"], headers=headers)
    assert fake_forges.calls() == []


def test_methods_are_a_closed_set_and_a_get_has_no_body(fake_forges: FakeForges) -> None:
    gateway = make_gateway(fake_forges)
    for method in ("TRACE", "get ", "", "GET\n", "CONNECT"):
        with pytest.raises(ValueError):
            gateway.api(GH, method, ["x"])
    with pytest.raises(ValueError):
        gateway.api(GH, "GET", ["x"], body={"a": 1})
    with pytest.raises(ValueError):
        gateway.api(GH, "GET", ["x"], write=True)
    assert fake_forges.calls() == []


def test_a_caller_accept_header_is_passed_as_one_argv_element(fake_forges: FakeForges) -> None:
    script_ok(fake_forges, "gh", body_text="diff --git a b\n")
    response = make_gateway(fake_forges).api(
        GH, "GET", ["repos", "o", "r", "pulls", "1"],
        headers={"Accept": "application/vnd.github.v3.diff"}, parse="text",
    )  # fmt: skip
    assert response.data == "diff --git a b\n"
    assert "Accept: application/vnd.github.v3.diff" in only_call(fake_forges).argv


# -- Status parsing and typed errors -------------------------------------------------------------


def stub(
    stdout: bytes, *, stderr: bytes = b"", code: int = 0, **flags: Any
) -> Callable[..., ProcessResult]:
    def run(argv: Sequence[str], **_kw: Any) -> ProcessResult:
        return ProcessResult(pid=1, pgid=1, returncode=code, stdout=stdout, stderr=stderr, **flags)

    return run


def head(status: int, headers: Mapping[str, str] | None = None, reason: str = "X") -> bytes:
    lines = f"HTTP/2.0 {status} {reason}\n"
    lines += "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    return (lines + "\r\n").encode()


def stub_gateway(tmp_path: Path, runner: Callable[..., ProcessResult], **kwargs: Any) -> Gateway:
    exe = tmp_path / "bin"
    exe.mkdir(exist_ok=True)
    for name in ("gh", "glab"):
        path = exe / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    environ = {"PATH": str(exe), "HOME": os.environ["HOME"], "TMPDIR": os.environ["TMPDIR"]}
    return Gateway(LoadedConfig.defaults(), timeout=30, environ=environ, runner=runner, **kwargs)


def call_of(gateway: Gateway, *, write: bool = False, **kwargs: Any) -> Any:
    if write:
        kwargs.setdefault("expect_status", frozenset({200}))
    return gateway.api(GH, "POST" if write else "GET", ["x"], write=write, **kwargs)


def code_of(gateway: Gateway, **kwargs: Any) -> ForgectlError:
    with pytest.raises(ForgectlError) as raised:
        call_of(gateway, **kwargs)
    return raised.value


READ_TABLE = [
    (401, "auth_required"), (403, "permission_denied"), (404, "not_found"), (429, "rate_limited"),
    (500, "upstream_error"), (502, "upstream_error"), (503, "upstream_error"),
    (400, "host_rejected"), (405, "host_rejected"), (409, "host_rejected"), (410, "host_rejected"),
    (422, "host_rejected"), (301, "host_rejected"), (304, "host_rejected"),
    (307, "host_rejected"), (308, "host_rejected"),
]  # fmt: skip


@pytest.mark.parametrize(("status", "code"), READ_TABLE)
def test_read_status_table(tmp_path: Path, status: int, code: str) -> None:
    gateway = stub_gateway(tmp_path, stub(head(status) + b"{}", code=1))
    err = code_of(gateway)
    assert err.code == code
    if code == "host_rejected":
        assert err.details == {"status": status}


WRITE_TABLE = [
    (401, "auth_required"), (403, "permission_denied"), (404, "not_found"), (429, "rate_limited"),
    (500, "outcome_unknown"), (502, "outcome_unknown"), (503, "outcome_unknown"),
    (400, "host_rejected"), (422, "host_rejected"), (409, "host_rejected"),
    # gh and glab show a 3xx only when they do not follow it: a 307 or 308 on a body from stdin
    (307, "host_rejected"), (308, "host_rejected"),
]  # fmt: skip


@pytest.mark.parametrize(("status", "code"), WRITE_TABLE)
def test_write_status_table(tmp_path: Path, status: int, code: str) -> None:
    gateway = stub_gateway(tmp_path, stub(head(status) + b"{}", code=1))
    assert code_of(gateway, write=True).code == code


def test_a_status_the_caller_maps_itself_returns_instead_of_raising(tmp_path: Path) -> None:
    gateway = stub_gateway(tmp_path, stub(head(409) + b'{"message":"sha"}', code=1))
    response = call_of(gateway, write=True, passthrough=frozenset({409, 405}))
    assert response.status == 409 and response.data == {"message": "sha"}
    gateway = stub_gateway(tmp_path, stub(head(405) + b"not json", code=1))
    assert call_of(gateway, write=True, passthrough=frozenset({405})).data is None


def test_status_comes_from_the_headers_never_from_message_text_or_the_exit_code(
    tmp_path: Path,
) -> None:
    # A 404 whose body talks about HTTP 200 stays a 404; a 200 whose body and stderr talk about
    # 404, 500 and rate limits stays a 200, whatever the exit code of a successful call is.
    decoy = b'{"message":"HTTP 200 OK, not a rate limit"}'
    assert code_of(stub_gateway(tmp_path, stub(head(404) + decoy, code=1))).code == "not_found"
    body = b'{"status":404,"message":"HTTP 500 rate limit exceeded"}'
    stderr = b"gh: HTTP 500 Internal Server Error\n"
    ok = call_of(stub_gateway(tmp_path, stub(head(200) + body, stderr=stderr)))
    assert ok.status == 200 and ok.data["status"] == 404
    text = b"HTTP/2.0 404 Not Found\n\r\n"
    # a status line in the body (after the header block) is not a status
    assert (
        call_of(stub_gateway(tmp_path, stub(head(200) + text)), parse="text").data == text.decode()
    )


def test_rate_limit_rows_and_retry_after(tmp_path: Path) -> None:
    def err(status: int, headers: Mapping[str, str], body: bytes) -> ForgectlError:
        return code_of(stub_gateway(tmp_path, stub(head(status, headers) + body, code=1)))

    e = err(403, {"Retry-After": "7"}, b"{}")
    assert (e.code, e.retry_after) == ("rate_limited", 7.0)
    e = err(429, {"Retry-After": "2"}, b"{}")
    assert (e.code, e.retry_after) == ("rate_limited", 2.0)
    e = err(429, {}, b"")
    assert (e.code, e.retry_after) == ("rate_limited", None)
    e = err(403, {}, b'{"message":"You have exceeded a secondary rate limit."}')
    assert (e.code, e.retry_after) == ("rate_limited", None)
    assert err(403, {}, b'{"message":"Resource not accessible"}').code == "permission_denied"
    assert err(403, {"Retry-After": "soon"}, b"{}").code == "rate_limited"
    assert err(403, {"retry-after": "7"}, b"{}").retry_after == 7.0
    soon = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 30))
    got = err(429, {"Retry-After": soon}, b"{}").retry_after
    assert got is not None and 20 < got <= 31
    past = err(429, {"Retry-After": "Mon, 01 Jan 2001 00:00:00 GMT"}, b"{}").retry_after
    assert past == 0.0
    assert err(429, {"Retry-After": "-5"}, b"{}").retry_after is None
    assert err(429, {"Retry-After": "1e3"}, b"{}").retry_after is None
    assert err(429, {"Retry-After": "9" * 30}, b"{}").retry_after is None


def test_host_message_is_sanitized_cut_and_never_decides_the_code(tmp_path: Path) -> None:
    message = "bad ‮\x1b[31m thing " + "x" * 1000
    body = json.dumps({"message": message}).encode()
    err = code_of(stub_gateway(tmp_path, stub(head(422) + body, code=1)))
    assert err.code == "host_rejected"
    assert "‮" not in err.message and "\x1b" not in err.message
    assert len(err.message) < 600


def test_an_informational_status_is_never_a_success(tmp_path: Path) -> None:
    for status in (100, 101, 103):
        runner = stub(head(status) + b"{}", code=0)
        assert code_of(stub_gateway(tmp_path, runner)).code == "upstream_error", status
        assert code_of(stub_gateway(tmp_path, runner), write=True).code == "outcome_unknown"


def test_a_token_straddling_the_message_cuts_never_leaks_a_prefix(tmp_path: Path) -> None:
    secret = "tok" + "s" * 60
    target = Target("github", "github.com", token=secret)
    for pad in range(150, 205, 7):
        stderr = ("e" * pad + secret + " tail").encode()
        with pytest.raises(ForgectlError) as raised:
            stub_gateway(tmp_path, stub(b"", stderr=stderr, code=1)).api(target, "GET", ["x"])
        assert "toks" not in raised.value.message and "sss" not in raised.value.message, pad
    for pad in range(250, 305, 7):
        body = json.dumps({"message": "m" * pad + secret}).encode()
        with pytest.raises(ForgectlError) as raised:
            stub_gateway(tmp_path, stub(head(422) + body, code=1)).api(target, "GET", ["x"])
        assert "toks" not in raised.value.message and "sss" not in raised.value.message, pad


def test_a_percent_encoded_token_in_stderr_is_redacted(tmp_path: Path) -> None:
    secret = "glpat-a/b+c=d"
    target = Target("gitlab", "gitlab.com", token=secret)
    for form in (urllib.parse.quote(secret, safe=""), urllib.parse.quote(secret), secret):
        stderr = f"GET https://x.example.test/?t={form} failed\nsecond line".encode()
        with pytest.raises(ForgectlError) as raised:
            stub_gateway(tmp_path, stub(b"", stderr=stderr, code=1)).api(target, "GET", ["x"])
        assert form not in raised.value.message and "[redacted]" in raised.value.message
        assert "second line" in raised.value.message  # all of stderr, newlines escaped
        assert "\n" not in raised.value.message


def test_a_token_in_graphql_errors_never_reaches_a_message(tmp_path: Path) -> None:
    doc = {"data": None, "errors": [{"type": "X", "message": f"bad {TOKEN} here"}]}
    with pytest.raises(ForgectlError) as raised:
        graphql_gateway(tmp_path, doc, code=1).graphql(GH, QUERY)
    assert TOKEN not in raised.value.message and "[redacted]" in raised.value.message
    doc = {"data": {"addReview": {"errors": [{"message": f"no {TOKEN}"}]}}}
    with pytest.raises(ForgectlError) as raised:
        graphql_gateway(tmp_path, doc).graphql(GH, MUTATION, write=True, mutation="addReview")
    assert raised.value.code == "host_rejected"
    assert TOKEN not in raised.value.message and "[redacted]" in raised.value.message


def test_a_token_in_an_observed_url_is_redacted() -> None:
    with pytest.raises(ForgectlError) as raised:
        gw.verify_identity(
            f"https://evil.example.test/x?private_token={TOKEN}",
            host="git.example.test",
            secret=TOKEN,
        )
    observed = (raised.value.details or {})["observed"]
    assert TOKEN not in observed and "[redacted]" in observed


def test_a_token_in_stderr_or_a_host_message_never_reaches_a_message(tmp_path: Path) -> None:
    gateway = stub_gateway(
        tmp_path,
        stub(b"", stderr=f"dial failed for {TOKEN} here".encode(), code=1),
    )
    err = code_of(gateway)
    assert err.code == "network_error" and TOKEN not in err.message and "[redacted]" in err.message
    body = json.dumps({"message": f"bad credential {TOKEN}"}).encode()
    err = code_of(stub_gateway(tmp_path, stub(head(422) + body, code=1)))
    assert TOKEN not in err.message and TOKEN not in json.dumps(err.envelope())


def test_gh_exit_4_with_no_headers_is_auth_required_on_a_read_and_a_write(tmp_path: Path) -> None:
    for write in (False, True):
        err = code_of(
            stub_gateway(tmp_path, stub(b"", stderr=b"gh auth login", code=4)), write=write
        )
        assert err.code == "auth_required"


def test_no_status_is_network_error_for_a_read_and_outcome_unknown_for_a_write(
    tmp_path: Path,
) -> None:
    cases = [
        b"",
        b"garbage",
        b"HTTP/2.0 abc OK\n\r\n",
        b"HTTP/2.0 99 X\n\r\n",
        b"HTTP/2.0 600 X\n\r\n",
        b"  HTTP/2.0 200 OK\n\r\n",
        b"Hello\nHTTP/2.0 200 OK\n\r\n",
        b"HTTP/2.0 200 OK\nbad header\r\n\r\n",
        b"HTTP/2.0 200 OK\n\xff: x\r\n\r\n",
        b"HTTP/2.0 200 OK\nA: b\r\n",
    ]
    for stdout in cases:
        assert code_of(stub_gateway(tmp_path, stub(stdout, code=1))).code == "network_error", stdout
        assert (
            code_of(stub_gateway(tmp_path, stub(stdout, code=1)), write=True).code
            == "outcome_unknown"
        )


def test_a_gitlab_gh_exit_4_rule_is_only_for_gh(tmp_path: Path) -> None:
    gateway = stub_gateway(tmp_path, stub(b"", code=4))
    with pytest.raises(ForgectlError) as raised:
        gateway.api(GL, "GET", ["user"])
    assert raised.value.code == "network_error"


def test_a_timeout_is_network_error_on_a_read_and_outcome_unknown_on_a_write(
    tmp_path: Path,
) -> None:
    runner = stub(b"", code=-15, timed_out=True)
    assert code_of(stub_gateway(tmp_path, runner)).code == "network_error"
    assert code_of(stub_gateway(tmp_path, runner), write=True).code == "outcome_unknown"


def test_oversized_and_unfinished_answers_are_errors_never_partial_data(tmp_path: Path) -> None:
    flag_sets: list[dict[str, Any]] = [{"stdout_truncated": True}, {"streams_incomplete": True}]
    for flags in flag_sets:
        for body in (b'{"a":', b"{}"):  # a valid answer is refused too: the flag decides
            runner = stub(head(200) + body, **flags)
            assert code_of(stub_gateway(tmp_path, runner)).code == "upstream_error", flags
            assert code_of(stub_gateway(tmp_path, runner), write=True).code == "outcome_unknown"


def test_a_read_past_the_cap_is_told_apart_from_one_that_never_ended(tmp_path: Path) -> None:
    cut = code_of(stub_gateway(tmp_path, stub(head(200) + b"{}", stdout_truncated=True)))
    assert isinstance(cut, AnswerCut) and cut.code == "upstream_error"
    endless = code_of(stub_gateway(tmp_path, stub(head(200) + b"{}", streams_incomplete=True)))
    assert not isinstance(endless, AnswerCut)
    write = stub_gateway(tmp_path, stub(head(200) + b"{}", stdout_truncated=True))
    assert not isinstance(code_of(write, write=True), AnswerCut)


MALFORMED = [
    b"{", b'{"a":1}}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e999}', b'{"a":1,"a":2}',
    b"\xef\xbb\xbf{}", b'{"a":"\xff"}', b"[" * 100000, b"{'a':1}", b"nul", b'{"a":1} x',
]  # fmt: skip


@pytest.mark.parametrize("body", MALFORMED)
def test_malformed_bodies_are_typed_errors(tmp_path: Path, body: bytes) -> None:
    runner = stub(head(200) + body)
    assert code_of(stub_gateway(tmp_path, runner)).code == "upstream_error"
    assert code_of(stub_gateway(tmp_path, runner), write=True).code == "outcome_unknown"


def test_a_success_status_with_a_failing_exit_is_not_a_success(tmp_path: Path) -> None:
    runner = stub(head(200) + b'{"a":1}', code=1)
    assert code_of(stub_gateway(tmp_path, runner)).code == "upstream_error"
    assert code_of(stub_gateway(tmp_path, runner), write=True).code == "outcome_unknown"


def test_text_mode_is_strict_utf8(tmp_path: Path) -> None:
    err = code_of(stub_gateway(tmp_path, stub(head(200) + b"\xff")), parse="text")
    assert err.code == "upstream_error"


def test_bytes_mode_returns_the_raw_body_whatever_its_encoding(tmp_path: Path) -> None:
    body = b"diff --git a b\n+caf\xe9 \xff\n"
    response = call_of(stub_gateway(tmp_path, stub(head(200) + body)), parse="bytes")
    assert response.data == body


def test_a_passthrough_status_in_text_mode_tolerates_invalid_utf8(tmp_path: Path) -> None:
    gateway = stub_gateway(tmp_path, stub(head(409) + b"\xff", code=1))
    response = call_of(gateway, passthrough=frozenset({409}), parse="text")
    assert (response.status, response.data) == (409, None)


@pytest.mark.parametrize("shape", ["object", "list"])
def test_shape_mismatch_on_a_read_is_upstream_error(tmp_path: Path, shape: str) -> None:
    other = b"[]" if shape == "object" else b"{}"
    assert (
        code_of(stub_gateway(tmp_path, stub(head(200) + other)), shape=shape).code
        == "upstream_error"
    )  # type: ignore[arg-type]
    assert call_of(
        stub_gateway(tmp_path, stub(head(200) + (b"{}" if shape == "object" else b"[]"))),
        shape=shape,
    )  # type: ignore[arg-type]


def test_a_write_documented_as_one_object_that_returns_a_list_is_host_rejected(
    tmp_path: Path,
) -> None:
    runner = stub(head(200) + b'[{"id":1}]')
    err = code_of(stub_gateway(tmp_path, runner), write=True, shape="object")
    assert (err.code, err.details) == ("host_rejected", {"status": 200})
    # not an object and not a list: no proof either way
    err = code_of(stub_gateway(tmp_path, stub(head(200) + b"null")), write=True, shape="object")
    assert err.code == "outcome_unknown"
    err = code_of(stub_gateway(tmp_path, stub(head(200))), write=True, shape="object")
    assert err.code == "outcome_unknown"


def test_a_write_answering_another_2xx_than_documented_is_outcome_unknown(tmp_path: Path) -> None:
    for status in (200, 202, 204):
        runner = stub(head(status) + b'{"id":1}')
        err = code_of(stub_gateway(tmp_path, runner), write=True, expect_status=frozenset({201}))
        assert err.code == "outcome_unknown", status
    ok = call_of(
        stub_gateway(tmp_path, stub(head(201) + b'{"id":1}')),
        write=True,
        expect_status=frozenset({201}),
    )
    assert ok.status == 201


def test_http_status_line_forms() -> None:
    assert gw.parse_http(b"HTTP/2.0 200 OK\n\r\n{}") == (200, {}, b"{}")
    assert gw.parse_http(b"HTTP/1.1 204 No Content\r\nA: b\r\n\r\n") == (204, {"a": "b"}, b"")
    assert gw.parse_http(b"HTTP/2 200\nA: b\r\nA: c\r\n\r\nbody") == (200, {"a": "b, c"}, b"body")
    assert gw.parse_http(b"HTTP/2.0 304 Not Modified\n") == (304, {}, b"")
    assert gw.parse_http(b"HTTP/2.0 200 OK\nA: b\n\nx") == (200, {"a": "b"}, b"x")


# -- Real runs: statuses through the fake -----------------------------------------------------


@pytest.mark.parametrize("target", [GH, GL])
def test_statuses_through_the_fakes_with_the_exit_code_ignored(
    fake_forges: FakeForges, target: Target
) -> None:
    fake_forges.answer(
        target.program, endpoint_re="^missing", status=404, body={"message": "Not Found"}
    )
    fake_forges.answer(
        target.program,
        endpoint_re="^limited",
        status=403,
        headers={"Retry-After": "9"},
        body={"message": "x"},
    )
    fake_forges.answer(target.program, endpoint_re="^broken", status=502, body={"message": "x"})
    fake_forges.answer(
        target.program, endpoint_re="^teapot", status=418, body={"message": "short and stout"}
    )
    fake_forges.answer(target.program, endpoint_re="^fine", status=200, body={"a": 1})
    fake_forges.answer(target.program, endpoint_re="^empty", status=204)
    gateway = make_gateway(fake_forges)
    assert gateway.api(target, "GET", ["fine"]).data == {"a": 1}
    assert gateway.api(target, "GET", ["empty"]).data is None
    seen = {}
    for name in ("missing", "limited", "broken", "teapot"):
        with pytest.raises(ForgectlError) as raised:
            gateway.api(target, "GET", [name])
        seen[name] = raised.value
    assert seen["missing"].code == "not_found"
    assert (seen["limited"].code, seen["limited"].retry_after) == ("rate_limited", 9.0)
    assert seen["broken"].code == "upstream_error"
    assert seen["teapot"].code == "host_rejected" and "short and stout" in seen["teapot"].message
    for call in fake_forges.calls():
        assert_call_safe(call, host=target.host)


# -- Redirects ---------------------------------------------------------------------------------


def test_a_redirected_gh_post_that_ends_in_a_list_is_host_rejected_and_the_token_stays_home(
    fake_forges: FakeForges,
) -> None:
    fake_forges.answer(
        "gh",
        endpoint_re="reviews",
        method="POST",
        redirect="https://other.example.test/x",
        status=302,
    )
    fake_forges.answer("gh", endpoint="x", body=[{"id": 1}])
    gateway = make_gateway(fake_forges)
    with pytest.raises(ForgectlError) as raised:
        gateway.api(
            GH, "POST", ["repos", "o", "r", "pulls", "1", "reviews"], body={"event": "COMMENT"},
            write=True, shape="object", expect_status=frozenset({200}),
        )  # fmt: skip
    assert raised.value.code == "host_rejected"
    call = only_call(fake_forges)
    assert call.requests[-1]["method"] == "GET"  # gh turned the POST into a GET
    assert call.requests[-1]["token_sent"] is None  # and sent no token to the other host


def test_a_patch_redirected_by_a_301_reaches_the_gateway_as_the_gets_answer(
    fake_forges: FakeForges,
) -> None:
    """gh and glab turn a 301/302/303 into a body-less GET and show only its answer: the
    gateway cannot see the redirect, so a write's proof must reject the GET's answer
    (TODO(#24) in gateway.py)."""
    fake_forges.answer("gh", endpoint_re="pulls/1$", method="PATCH", redirect="/x", status=301)
    fake_forges.answer("gh", endpoint="x", method="GET", body={"number": 1, "title": "old"})
    gateway = make_gateway(fake_forges)
    response = gateway.api(
        GH, "PATCH", ["repos", "o", "r", "pulls", "1"], body={"title": "new"},
        write=True, shape="object", expect_status=frozenset({200}),
    )  # fmt: skip
    assert (response.status, response.data) == (200, {"number": 1, "title": "old"})
    hop = only_call(fake_forges).requests[-1]
    assert (hop["method"], hop["body"]) == ("GET", None)


def test_a_post_redirected_by_a_301_to_a_200_where_201_is_documented_is_outcome_unknown(
    fake_forges: FakeForges,
) -> None:
    fake_forges.answer("gh", endpoint_re="comments$", method="POST", redirect="/x", status=301)
    fake_forges.answer("gh", endpoint="x", method="GET", body={"id": 1})
    with pytest.raises(ForgectlError) as raised:
        make_gateway(fake_forges).api(
            GH, "POST", ["repos", "o", "r", "issues", "1", "comments"], body={"body": "hi"},
            write=True, shape="object", expect_status=frozenset({201}),
        )  # fmt: skip
    assert raised.value.code == "outcome_unknown"


@pytest.mark.parametrize("status", [307, 308])
def test_a_write_answered_307_or_308_is_shown_not_followed_and_is_host_rejected(
    fake_forges: FakeForges, status: int
) -> None:
    fake_forges.answer("gh", endpoint_re="comments$", redirect="/x", status=status)
    with pytest.raises(ForgectlError) as raised:
        make_gateway(fake_forges).api(
            GH, "POST", ["repos", "o", "r", "issues", "1", "comments"], body={"body": "hi"},
            write=True, shape="object", expect_status=frozenset({201}),
        )  # fmt: skip
    assert (raised.value.code, raised.value.details) == ("host_rejected", {"status": status})
    assert len(only_call(fake_forges).requests) == 1


def test_a_redirected_glab_read_is_caught_by_the_identity_check(fake_forges: FakeForges) -> None:
    fake_forges.answer(
        "glab", endpoint_re="^projects", redirect="https://other.example.test/api/v4/p", status=301
    )
    fake_forges.answer(
        "glab",
        endpoint_re="^api|p$",
        body={"web_url": "https://other.example.test/g/p/-/merge_requests/1"},
    )
    gateway = make_gateway(fake_forges)
    response = gateway.api(GL_SELF, "GET", ["projects", "7", "merge_requests", "1"])
    call = only_call(fake_forges)
    assert (
        call.requests[-1]["token_sent"] == TOKEN
    )  # glab repeats Private-Token: known residual risk
    with pytest.raises(ForgectlError) as raised:
        gw.verify_identity(response.data["web_url"], host=GL_SELF.host, project="g/p")
    assert raised.value.code == "wrong_host_response"
    assert "rotated" in raised.value.message
    assert raised.value.details == {"observed": response.data["web_url"]}


@pytest.mark.parametrize(
    ("observed", "project", "ok"),
    [
        ("https://git.example.test/g/p/-/merge_requests/1", "g/p", True),
        ("https://GIT.example.test/g/p", "g/p", True),
        ("https://git.example.test/g/p", None, True),
        ("https://git.example.test/g/p2/-/x", "g/p", False),
        ("https://git.example.test/g/pp", "g/p", False),
        ("https://git.example.test/g%2Fp/x", "g/p", True),
        ("https://git.example.test/x/y", "g/p", False),
        ("https://git.example.test@evil.example.test/g/p", "g/p", False),
        ("https://evil.example.test@git.example.test/g/p", "g/p", False),
        ("https://git.example.test.evil.example.test/g/p", "g/p", False),
        ("https://git.example.test:8443/g/p", "g/p", False),
        ("ftp://git.example.test/g/p", "g/p", False),
        ("//git.example.test/g/p", "g/p", False),
        ("/g/p", "g/p", False),
        ("", "g/p", False),
        (None, None, False),
        (5, None, False),
        (["https://git.example.test/g/p"], None, False),
        ("https://[::1/g/p", None, False),
    ],
)
def test_verify_identity(observed: object, project: str | None, ok: bool) -> None:
    if ok:
        gw.verify_identity(observed, host="git.example.test", project=project)
        return
    with pytest.raises(ForgectlError) as raised:
        gw.verify_identity(observed, host="git.example.test", project=project)
    assert raised.value.code == "wrong_host_response"
    assert "observed" in (raised.value.details or {})


# -- GraphQL -----------------------------------------------------------------------------------

QUERY = "query($id: ID!) { node(id: $id) { id } }"
MUTATION = "mutation($in: X!) { addReview(input: $in) { id } }"


def graphql_gateway(tmp_path: Path, doc: Any, *, status: int = 200, code: int = 0) -> Gateway:
    body = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    return stub_gateway(tmp_path, stub(head(status) + body, code=code))


def test_graphql_success_returns_the_data(tmp_path: Path) -> None:
    result = graphql_gateway(tmp_path, {"data": {"node": {"id": "1"}}}).graphql(
        GH, QUERY, {"id": "1"}
    )
    assert result.data == {"node": {"id": "1"}} and result.errors == ()


def test_graphql_goes_by_stdin_with_variables_and_no_jq(fake_forges: FakeForges) -> None:
    fake_forges.answer("gh", endpoint="graphql", body={"data": {"node": {"id": "1"}}})
    hostile = 'x"}) { viewer { login } } #  $(y) --jq=. ‮'
    make_gateway(fake_forges).graphql(GH, QUERY, {"id": hostile})
    call = only_call(fake_forges)
    assert_call_safe(call, host="github.com", untrusted=[hostile])
    assert json.loads(call.stdin or "") == {"query": QUERY, "variables": {"id": hostile}}
    assert not any(a in call.argv for a in ("--jq", "-q", "--template", "-t", "--paginate"))
    assert call.argv[-1] == "graphql"


def test_graphql_errors_with_http_200_exit_1_and_partial_data_fail(fake_forges: FakeForges) -> None:
    doc = {"data": {"node": {"id": "1"}}, "errors": [{"type": "NOT_FOUND", "message": "gone"}]}
    fake_forges.answer("gh", endpoint="graphql", body=doc)
    gateway = make_gateway(fake_forges)
    with pytest.raises(ForgectlError) as raised:
        gateway.graphql(GH, QUERY, {"id": "1"})
    assert raised.value.code == "not_found"
    assert only_call(fake_forges).exit == 1  # gh exited 1 with HTTP 200 and the whole body


@pytest.mark.parametrize(
    ("types", "code"),
    [(["RATE_LIMITED"], "rate_limited"), (["NOT_FOUND"], "not_found"),
     (["FORBIDDEN"], "permission_denied"),
     (["NOT_FOUND", "RATE_LIMITED"], "rate_limited"), (["FORBIDDEN", "NOT_FOUND"], "not_found"),
     (["SOMETHING"], "host_rejected"), ([None], "host_rejected")],
)  # fmt: skip
def test_graphql_error_types(tmp_path: Path, types: list[Any], code: str) -> None:
    doc = {"data": None, "errors": [{"type": t, "message": "m"} for t in types]}
    with pytest.raises(ForgectlError) as raised:
        graphql_gateway(tmp_path, doc, code=1).graphql(GH, QUERY)
    assert raised.value.code == code


def test_graphql_errors_shaped_oddly_still_fail(tmp_path: Path) -> None:
    for errors in ("boom", {"message": "x"}, [None], [1]):
        with pytest.raises(ForgectlError) as raised:
            graphql_gateway(tmp_path, {"data": {"a": 1}, "errors": errors}, code=1).graphql(
                GH, QUERY
            )
        assert raised.value.code == "host_rejected"


def test_graphql_allow_errors_returns_partial_data_with_its_errors(tmp_path: Path) -> None:
    doc = {"data": {"a": 1}, "errors": [{"type": "FORBIDDEN", "message": "read:org"}]}
    result = graphql_gateway(tmp_path, doc, code=1).graphql(GH, QUERY, allow_errors=True)
    assert result.data == {"a": 1} and len(result.errors) == 1
    with pytest.raises(ForgectlError):
        graphql_gateway(tmp_path, {"errors": [{"message": "x"}]}, code=1).graphql(
            GH, QUERY, allow_errors=True
        )
    with pytest.raises(ValueError):
        graphql_gateway(tmp_path, doc).graphql(GH, MUTATION, write=True, allow_errors=True)


@pytest.mark.parametrize("doc", [{}, {"data": None}, {"data": []}, {"data": "x"}, [], "x"])
def test_graphql_read_with_no_data_object_is_upstream_error(tmp_path: Path, doc: Any) -> None:
    with pytest.raises(ForgectlError) as raised:
        graphql_gateway(tmp_path, doc).graphql(GH, QUERY)
    assert raised.value.code == "upstream_error"


def test_graphql_mutation_checks(tmp_path: Path) -> None:
    good = graphql_gateway(tmp_path, {"data": {"addReview": {"id": "1"}}})
    assert (
        good.graphql(GH, MUTATION, {}, write=True, mutation="addReview").data["addReview"]["id"]
        == "1"
    )
    for doc, code in [
        ({"data": {"addReview": None}}, "outcome_unknown"),  # a null result: no proof
        ({"data": {}}, "outcome_unknown"),
        ({"data": None}, "outcome_unknown"),
        ({"data": {"addReview": {"errors": ["no"]}}}, "host_rejected"),  # nothing landed
        ({"data": {"addReview": {"id": "1"}}, "errors": [{"message": "x"}]}, "outcome_unknown"),
        ({"data": {"addReview": None}, "errors": [{"type": "FORBIDDEN"}]}, "permission_denied"),
        ({"errors": [{"type": "RATE_LIMITED"}]}, "rate_limited"),
    ]:
        with pytest.raises(ForgectlError) as raised:
            graphql_gateway(tmp_path, doc, code=1).graphql(
                GH, MUTATION, write=True, mutation="addReview"
            )
        assert raised.value.code == code, doc
    with pytest.raises(ForgectlError) as raised:
        graphql_gateway(tmp_path, {"data": {"addReview": None}}).graphql(
            GH, MUTATION, mutation="addReview"
        )
    assert raised.value.code == "upstream_error"  # the same on a read: never partial data


def test_a_read_naming_a_mutation_key_with_a_result_and_forbidden_is_permission_denied(
    tmp_path: Path,
) -> None:
    doc = {"data": {"addReview": {"id": "1"}}, "errors": [{"type": "FORBIDDEN", "message": "x"}]}
    with pytest.raises(ForgectlError) as raised:
        graphql_gateway(tmp_path, doc, code=1).graphql(GH, MUTATION, mutation="addReview")
    assert raised.value.code == "permission_denied"


@pytest.mark.parametrize("errors", [{}, "", 0, False])
def test_present_non_list_graphql_errors_on_a_read_are_host_rejected(
    tmp_path: Path, errors: Any
) -> None:
    for allow in (False, True):
        with pytest.raises(ForgectlError) as raised:
            graphql_gateway(tmp_path, {"data": {"a": 1}, "errors": errors}, code=1).graphql(
                GH, QUERY, allow_errors=allow
            )
        assert raised.value.code == "host_rejected", (errors, allow)


def test_graphql_rate_limited_carries_retry_after_and_host_rejected_the_real_status(
    tmp_path: Path,
) -> None:
    doc = json.dumps({"errors": [{"type": "RATE_LIMITED", "message": "slow"}]}).encode()
    gateway = stub_gateway(tmp_path, stub(head(200, {"Retry-After": "11"}) + doc, code=1))
    with pytest.raises(ForgectlError) as raised:
        gateway.graphql(GH, QUERY)
    assert (raised.value.code, raised.value.retry_after) == ("rate_limited", 11.0)
    doc = json.dumps({"data": None, "errors": [{"message": "odd"}]}).encode()
    gateway = stub_gateway(tmp_path, stub(head(203) + doc, code=1))
    with pytest.raises(ForgectlError) as raised:
        gateway.graphql(GH, QUERY)
    assert (raised.value.code, raised.value.details) == ("host_rejected", {"status": 203})


def test_a_write_needs_its_proof_and_a_changing_method_needs_write(tmp_path: Path) -> None:
    gateway = stub_gateway(tmp_path, stub(head(200) + b'{"data":{}}'))
    with pytest.raises(ValueError):
        gateway.graphql(GH, MUTATION, write=True)  # no mutation key: no proof
    with pytest.raises(ValueError):
        gateway.api(GH, "POST", ["x"], body={"a": 1}, write=True)  # no expect_status
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(ValueError):
            gateway.api(GH, method, ["x"])  # a change of state passed as a read


def test_graphql_mutation_must_answer_exactly_200(tmp_path: Path) -> None:
    gateway = graphql_gateway(tmp_path, {"data": {"addReview": {"id": "1"}}}, status=202)
    with pytest.raises(ForgectlError) as raised:
        gateway.graphql(GH, MUTATION, write=True, mutation="addReview")
    assert raised.value.code == "outcome_unknown"


def test_each_graphql_call_is_judged_alone_and_a_502_after_a_200_fails_that_call(
    fake_forges: FakeForges,
) -> None:
    fake_forges.answer("gh", endpoint="graphql", times=1, body={"data": {"page": 1}})
    fake_forges.answer("gh", endpoint="graphql", status=502, body={"message": "bad gateway"})
    gateway = make_gateway(fake_forges)
    assert gateway.graphql(GH, QUERY, {"after": None}).data == {"page": 1}
    with pytest.raises(ForgectlError) as raised:
        gateway.graphql(GH, QUERY, {"after": "c1"})
    assert raised.value.code == "upstream_error"


def test_graphql_query_must_be_a_non_empty_string(tmp_path: Path) -> None:
    gateway = graphql_gateway(tmp_path, {"data": {}})
    for bad in ("", None, b"query", 5):
        with pytest.raises(ValueError):
            gateway.graphql(GH, bad)  # type: ignore[arg-type]


def test_graphql_goes_to_the_prefixed_instance_on_gitlab(fake_forges: FakeForges) -> None:
    fake_forges.answer("glab", endpoint="graphql", body={"data": {"a": 1}})
    make_gateway(fake_forges).graphql(GL_PREFIX, QUERY)
    call = only_call(fake_forges)
    assert call.request is not None
    assert call.request["url"] == "https://git.example.test/gitlab/api/graphql"
    assert_call_safe(call, host="git.example.test", gitlab_host="git.example.test/gitlab")


# -- glab routing table (design.md Testing), through the fake's modelled routing ----------------


@pytest.mark.parametrize(
    ("target", "path", "token_sent"),
    [
        (GL_PREFIX, "/gitlab/api/v4/user", True),  # GITLAB_HOST=<host>/<prefix> alone
        (GL_SELF, "/api/v4/user", True),  # GITLAB_HOST=<host>
        (GL, "/api/v4/user", True),  # --hostname gitlab.com
    ],
)
def test_glab_routing_rows_the_gateway_produces(
    fake_forges: FakeForges, target: Target, path: str, token_sent: bool
) -> None:
    script_ok(fake_forges, "glab")
    make_gateway(fake_forges).api(target, "GET", ["user"])
    call = only_call(fake_forges)
    assert call.request is not None
    assert urllib_path(call.request["url"]) == path
    assert (call.token_sent == TOKEN) is token_sent


def test_api_prefix_is_kept_through_gitlab_host_and_no_hostname_flag(
    fake_forges: FakeForges,
) -> None:
    """The gateway's half of the routing table: a prefixed host gets ``GITLAB_HOST`` with its
    prefix and never ``--hostname`` (which would drop it: the fake's rows are tested in
    ``tests/test_harness_fakes.py``)."""
    script_ok(fake_forges, "glab")
    make_gateway(fake_forges).api(GL_PREFIX, "GET", ["user"])
    ours = only_call(fake_forges)
    assert ours.env["GITLAB_HOST"] == "git.example.test/gitlab" and "--hostname" not in ours.argv
    assert ours.argv[:1] == ["api"] and ours.argv[-2:] == ["--", "user"]


def urllib_path(url: str) -> str:
    import urllib.parse

    return urllib.parse.urlsplit(url).path


def test_glab_defaults_are_turned_off_in_every_call(fake_forges: FakeForges) -> None:
    script_ok(fake_forges, "glab")
    make_gateway(fake_forges).api(GL, "GET", ["user"])
    call = only_call(fake_forges)
    assert call.env["GLAB_SEND_TELEMETRY"] == "false"
    assert call.env["GLAB_CHECK_UPDATE"] == "false"
    assert call.env["GLAB_SHOW_WHATS_NEW"] == "false"
    assert call.env["GLAB_NO_PROMPT"] == "1" and "NO_PROMPT" not in call.env
    assert call.env["NO_COLOR"] == "1"


def test_gh_with_a_url_or_r_flag_is_never_used(fake_forges: FakeForges) -> None:
    script_ok(fake_forges, "gh")
    make_gateway(fake_forges).api(GH, "GET", ["repos", "o", "r"])
    call = only_call(fake_forges)
    assert "-R" not in call.argv and "--repo" not in call.argv
    assert call.repo_context is None  # a planted remote could not be read in the private cwd


# -- Deadline, process groups, signals -----------------------------------------------------------


def test_deadline_kills_the_child_and_a_read_is_network_error(
    fake_forges: FakeForges, spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gw, "KILL_GRACE", 0.5)
    fake_forges.answer("gh", endpoint_re=".", hang=30)
    gateway = make_gateway(fake_forges, spawned, timeout=1.0)
    started = time.monotonic()
    with pytest.raises(ForgectlError) as raised:
        gateway.api(GH, "GET", ["x"])
    assert raised.value.code == "network_error"
    assert time.monotonic() - started < 10
    spawned.assert_gone()
    assert private_leftovers() == []
    assert only_call(fake_forges).signals == ["SIGTERM"]  # gh dies on SIGTERM: no SIGKILL needed


def test_deadline_on_a_write_is_outcome_unknown_and_the_budget_gets_its_reserve(
    fake_forges: FakeForges, spawned: Spawned
) -> None:
    fake_forges.answer("gh", endpoint_re=".", hang=30)
    gateway = make_gateway(fake_forges, spawned, timeout=1.0)
    with pytest.raises(ForgectlError) as raised:
        gateway.api(GH, "POST", ["x"], body={"a": 1}, write=True, expect_status=WROTE)
    assert raised.value.code == "outcome_unknown"
    spawned.assert_gone()
    assert gateway.budget.remaining() == pytest.approx(gw.WRITE_RESERVE, abs=0.5)


WROTE = frozenset({201})


def test_a_read_never_grants_the_write_reserve(fake_forges: FakeForges) -> None:
    script_ok(fake_forges, "gh")
    gateway = make_gateway(fake_forges, timeout=7.0)
    gateway.api(GH, "GET", ["x"])
    assert gateway.budget.total == 7.0


def test_an_exhausted_budget_stops_before_any_process_starts(fake_forges: FakeForges) -> None:
    script_ok(fake_forges, "gh")
    gateway = make_gateway(fake_forges, timeout=1.0)
    gateway.budget.spent = 5.0
    for write in (False, True):
        with pytest.raises(ForgectlError) as raised:
            if write:
                gateway.api(GH, "POST", ["x"], write=True, expect_status=WROTE)
            else:
                gateway.api(GH, "GET", ["x"])
        assert raised.value.code == "network_error"
    assert fake_forges.calls() == []


def test_budget_clock_and_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([100.0, 102.5])  # a fake clock: the call "takes" exactly 2.5 s
    monkeypatch.setattr(gw, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))
    budget = gw.Budget(10)
    assert budget.remaining() == 10
    with budget.spending():
        pass
    assert budget.remaining() == 7.5
    budget.grant_write_reserve()
    budget.grant_write_reserve()  # once only
    assert budget.total == 10 + gw.WRITE_RESERVE
    budget.spent = 100
    assert budget.remaining() == 0


def python_child(code: str) -> list[str]:
    return [sys.executable, "-I", "-c", code]


def plain_env() -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"], "TMPDIR": os.environ["TMPDIR"]}


GRANDCHILD = """
import os, subprocess, sys, time, signal
if {ignore_term}:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
gc = subprocess.Popen([sys.executable, "-I", "-c",
    "import signal,time;signal.signal(signal.SIGTERM, signal.SIG_IGN);time.sleep(60)"])
print(gc.pid, os.getpgrp(), flush=True)
time.sleep(60)
"""


def reap_orphans(pgid: int) -> None:
    """Grandchildren killed with their leader are reparented to this process (a subreaper) and
    wait here as zombies; reap them so the leaked-children check sees a clean process."""
    deadline = time.monotonic() + 5
    while group_members(pgid) and time.monotonic() < deadline:
        for pid, _ppid, state in group_members(pgid):
            if state == "Z":
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
        time.sleep(0.02)


@pytest.mark.parametrize("ignore_term", [False, True])
def test_deadline_kills_the_whole_group_with_sigterm_then_sigkill(
    monkeypatch: pytest.MonkeyPatch, ignore_term: bool
) -> None:
    monkeypatch.setattr(gw, "KILL_GRACE", 0.4)
    started: list[tuple[int, int]] = []
    result = gw.run_process(
        python_child(GRANDCHILD.format(ignore_term=ignore_term)),
        env=plain_env(),
        cwd=os.environ["TMPDIR"],
        timeout=0.6,
        on_start=lambda pid, pgid: started.append((pid, pgid)),
    )
    assert result.timed_out and result.returncode in (-15, -9)
    assert result.returncode == (-9 if ignore_term else -15)
    ((pid, pgid),) = started
    assert (result.pid, result.pgid) == (pid, pgid) == (pid, pid)
    grandchild = int(result.stdout.split()[0])
    reap_orphans(pgid)
    assert group_members(pgid) == []
    assert not Path(f"/proc/{grandchild}").exists()


def test_a_grandchild_left_behind_by_a_finished_child_is_killed_with_the_group() -> None:
    code = (
        "import subprocess,sys;"
        "p=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);print(p.pid,flush=True)"
    )
    result = gw.run_process(
        python_child(code), env=plain_env(), cwd=os.environ["TMPDIR"], timeout=10
    )
    assert result.returncode == 0 and not result.timed_out
    reap_orphans(result.pgid)
    assert group_members(result.pgid) == []


def test_a_held_open_pipe_after_the_leader_exits_is_flagged_not_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gw, "DRAIN_AFTER_EXIT", 0.2)
    code = (
        "import subprocess,sys;"
        "subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)']);"
        "print('x',flush=True)"
    )
    started = time.monotonic()
    result = gw.run_process(
        python_child(code), env=plain_env(), cwd=os.environ["TMPDIR"], timeout=30
    )
    assert result.streams_incomplete and not result.timed_out
    assert time.monotonic() - started < 10
    reap_orphans(result.pgid)
    assert group_members(result.pgid) == []


def test_stdout_cap_kills_the_child_and_flags_it_without_reading_on() -> None:
    code = "import sys\nwhile True: sys.stdout.write('x'*65536); sys.stdout.flush()"
    started = time.monotonic()
    result = gw.run_process(
        python_child(code),
        env=plain_env(),
        cwd=os.environ["TMPDIR"],
        timeout=30,
        max_stdout=100_000,
    )
    assert result.stdout_truncated and len(result.stdout) == 100_000
    assert not result.timed_out and time.monotonic() - started < 20  # far below the 30 s timeout
    assert group_members(result.pgid) == []


@pytest.mark.parametrize(("size", "cut"), [(100_000, False), (150_000, True)])
def test_stdout_cap_boundary(size: int, cut: bool) -> None:
    code = f"import sys\nsys.stdout.buffer.write(b'x'*{size}); sys.stdout.flush()"
    result = gw.run_process(
        python_child(code), env=plain_env(), cwd=os.environ["TMPDIR"], timeout=30,
        max_stdout=100_000,
    )  # fmt: skip
    assert result.stdout_truncated is cut
    assert len(result.stdout) == 100_000


def test_stderr_flood_is_capped_without_blocking_the_child() -> None:
    code = "import sys\nfor _ in range(200): sys.stderr.write('e'*65536)\nprint('done')"
    result = gw.run_process(
        python_child(code), env=plain_env(), cwd=os.environ["TMPDIR"], timeout=30, max_stderr=1000
    )
    assert result.returncode == 0 and result.stdout == b"done\n"
    assert result.stderr_truncated and len(result.stderr) == 1000


def test_a_large_stdin_and_large_stdout_do_not_deadlock() -> None:
    code = "import sys\nd=sys.stdin.buffer.read()\nsys.stdout.buffer.write(d)"
    payload = b"y" * 3_000_000
    result = gw.run_process(
        python_child(code), env=plain_env(), cwd=os.environ["TMPDIR"], timeout=30, stdin=payload
    )
    assert result.stdout == payload


def test_a_child_that_never_reads_its_stdin_does_not_hang_the_gateway() -> None:
    code = "import os\nos.close(0)\nprint('ok')"
    result = gw.run_process(
        python_child(code),
        env=plain_env(),
        cwd=os.environ["TMPDIR"],
        timeout=10,
        stdin=b"z" * 5_000_000,
    )
    assert result.stdout == b"ok\n"


def test_the_child_starts_with_the_callers_signal_mask_and_default_handlers() -> None:
    code = (
        "import signal;"
        "print(sorted(int(s) for s in signal.pthread_sigmask(signal.SIG_BLOCK, [])),"
        "signal.getsignal(signal.SIGINT) == signal.default_int_handler,"
        "signal.getsignal(signal.SIGTERM) == signal.SIG_DFL, flush=True)"
    )
    result = gw.run_process(
        python_child(code), env=plain_env(), cwd=os.environ["TMPDIR"], timeout=10
    )
    assert result.stdout.decode().startswith("[] True True")


def test_the_child_leads_its_own_group_and_session() -> None:
    code = "import os;print(os.getpid(),os.getpgrp(),os.getsid(0))"
    result = gw.run_process(
        python_child(code), env=plain_env(), cwd=os.environ["TMPDIR"], timeout=10
    )
    pid, pgid, sid = map(int, result.stdout.split())
    assert pid == pgid == sid == result.pid and pgid != os.getpgrp()


def test_a_non_positive_timeout_is_refused() -> None:
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            gw.run_process(["true"], env=plain_env(), cwd="/", timeout=bad)


class Interrupted(BaseException):
    pass


def test_a_signal_during_a_call_kills_the_exact_group_removes_dirs_and_propagates(
    fake_forges: FakeForges, spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_forges.answer("gh", endpoint_re=".", hang=30)

    def handler(signum: int, frame: Any) -> None:
        raise Interrupted(signum)

    old = signal.signal(signal.SIGALRM, handler)
    gateway = make_gateway(fake_forges, spawned, timeout=30)
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.8)
        with pytest.raises(Interrupted):
            gateway.api(GH, "POST", ["x"], body={"a": 1}, write=True, expect_status=WROTE)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    spawned.assert_gone()
    assert private_leftovers() == []
    call = only_call(fake_forges)
    assert call.signals == ["SIGTERM"]
    assert call.pid == spawned.children[0][0]


DRIVER = r"""
import sys
from forgectl import gateway as gw
from forgectl.cli import SignalExit, _signals
from forgectl.config import LoadedConfig

def on_spawn(pid, pgid):
    print("STARTED", pid, pgid, flush=True)
g = gw.Gateway(LoadedConfig.defaults(), timeout=60, on_spawn=on_spawn)
with _signals():  # the CLI's own handlers
    try:
        target = gw.Target("github", "github.com", token="fake-token-0001")
        g.api(target, "POST", ["x"], body={"a": 1}, write=True, expect_status=frozenset({201}))
    except SignalExit as exc:
        print("STOPPED", exc.signum, flush=True)
        sys.exit(128 + exc.signum)
"""


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_sent_to_a_running_forgectl_process_ends_the_child_group_and_cleans_up(
    fake_forges: FakeForges, procs: ProcessRegistry, signum: int
) -> None:
    fake_forges.answer("gh", endpoint_re=".", hang=60)
    env = forgectl_env(fake_forges)
    proc = procs.popen(
        [sys.executable, "-c", DRIVER],
        env=env,
        stdout=-1,
        stderr=-1,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().split()
    assert line[0] == "STARTED"
    child_pid, child_pgid = int(line[1]), int(line[2])
    assert child_pid == child_pgid and child_pid != proc.pid
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not fake_forges.calls():
        time.sleep(0.05)
    time.sleep(0.3)  # the fake is running and hanging
    proc.send_signal(signum)  # the exact Popen of the driver
    assert proc.wait(timeout=20) == 128 + signum
    assert group_members(child_pgid) == []
    call = only_call(fake_forges)
    assert call.pid == child_pid and call.signals == ["SIGTERM"]
    assert not Path(f"/proc/{child_pid}").exists()
    leftovers = [p for p in os.listdir(os.environ["TMPDIR"]) if p.startswith("forgectl-")]
    assert leftovers == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_DUMPABLE is Linux only")
def test_harden_process_clears_the_dumpable_flag_in_the_process_that_calls_it(
    procs: ProcessRegistry,
) -> None:
    code = (
        "import ctypes;from forgectl.gateway import harden_process as h;"
        "libc=ctypes.CDLL(None);before=libc.prctl(3,0,0,0,0);applied=h();"
        "print(before,applied,libc.prctl(3,0,0,0,0))"
    )
    result = procs.run([sys.executable, "-c", code], env=forgectl_env_plain())
    assert result.stdout.split() == ["1", "True", "0"]


def forgectl_env_plain() -> dict[str, str]:
    env = plain_env()
    env["PATH"] = os.environ["PATH"]
    for name in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "GH_CONFIG_DIR",
        "GLAB_CONFIG_DIR",
        "XDG_RUNTIME_DIR",
    ):
        env[name] = os.environ[name]
    return env


def test_main_hardens_the_process_for_a_real_invocation_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from forgectl import cli

    calls: list[bool] = []
    monkeypatch.setattr(gw, "harden_process", lambda: calls.append(True) or True)
    monkeypatch.setattr(sys, "argv", ["forgectl", "--version"])
    with pytest.raises(SystemExit):
        cli.main()
    assert calls == [True]
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert calls == [True]


# -- The second entry point: hooks, token_command, the browser -------------------------------


ENV_DUMP = (
    "import os,json;"
    "print(json.dumps({'cwd':os.getcwd(),'ls':os.listdir('.'),'env':dict(os.environ)}))"
)


def config_with_token_env(*names: str) -> LoadedConfig:
    from forgectl.config import Host

    base = LoadedConfig.defaults()
    hosts = {f"h{i}.example.test": Host(backend="gitlab", token_env=n) for i, n in enumerate(names)}
    return LoadedConfig(
        config=base.config.model_copy(update={"hosts": hosts}), config_hash="x", config_path=None
    )


def test_run_external_token_command_mode_gets_the_stored_login_environment(
    fake_forges: FakeForges, spawned: Spawned
) -> None:
    gateway = make_gateway(fake_forges, spawned, env=HOSTILE_ENV)
    result = gateway.run_external(
        python_child(ENV_DUMP), timeout=10, mode="token_command", backend="github"
    )
    seen = json.loads(result.stdout)
    assert seen["ls"] == [] and not Path(seen["cwd"]).exists()
    env = seen["env"]
    for name in (*gw.TOKEN_VARIABLES, "GH_HOST", "GH_REPO", "PAGER", "BROWSER", "LD_PRELOAD",
                 "GIT_DIR", "NO_PROMPT"):  # fmt: skip
        assert name not in env, name
    assert env["GLAB_NO_PROMPT"] == "1"
    assert env["GIT_CEILING_DIRECTORIES"] == str(Path(seen["cwd"]).parent)
    assert all(os.path.isabs(e) for e in env["PATH"].split(os.pathsep))
    spawned.assert_gone()
    assert private_leftovers() == []
    with pytest.raises(ValueError):
        gateway.run_external(python_child(ENV_DUMP), timeout=10, mode="token_command")


@pytest.mark.parametrize("mode", ["hook", "browser"])
def test_hooks_and_the_browser_inherit_the_environment_minus_every_token_variable(
    fake_forges: FakeForges, spawned: Spawned, tmp_path: Path, mode: str
) -> None:
    extra = {
        "MY_FORGE_TOKEN": "configured-secret",
        "OTHER_TOKEN": "passed-secret",
        "DISPLAY": ":0",
        "WAYLAND_DISPLAY": "wayland-0",
        "WSL_DISTRO_NAME": "x",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/x",
    }
    environ = forgectl_env(fake_forges, {**HOSTILE_ENV, **extra})
    gateway = Gateway(
        config_with_token_env("MY_FORGE_TOKEN"), timeout=20, environ=environ, on_spawn=spawned
    )
    result = gateway.run_external(
        python_child(ENV_DUMP), timeout=10, mode=mode,  # type: ignore[arg-type]
        token_env_names=("OTHER_TOKEN",), extra_env={"FORGECTL_HOOK": "pre-merge"},
    )  # fmt: skip
    seen = json.loads(result.stdout)
    env = seen["env"]
    assert seen["ls"] == [] and not Path(seen["cwd"]).exists()
    for name in (*gw.TOKEN_VARIABLES, "MY_FORGE_TOKEN", "OTHER_TOKEN"):
        assert name not in env, name
    for name in ("BROWSER", "DISPLAY", "WAYLAND_DISPLAY", "WSL_DISTRO_NAME", "PAGER", "GH_HOST",
                 "DBUS_SESSION_BUS_ADDRESS", "HOME"):  # fmt: skip
        assert env[name] == environ[name], name
    assert env["FORGECTL_HOOK"] == "pre-merge"
    spawned.assert_gone()
    assert private_leftovers() == []


def test_run_external_refuses_token_variables_and_nul_and_finds_only_absolute_path_entries(
    fake_forges: FakeForges,
) -> None:
    environ = forgectl_env(fake_forges, {"PATH": ".:rel"})
    gateway = Gateway(config_with_token_env("MY_FORGE_TOKEN"), timeout=5, environ=environ)
    for name in ("GH_TOKEN", "GH_FORCE_TTY", "CLICOLOR_FORCE", "GH_HOST", "GH_REPO",
                 "MY_FORGE_TOKEN"):  # fmt: skip
        with pytest.raises(ValueError):
            gateway.run_external(["x"], timeout=1, mode="hook", extra_env={name: "t"})
    with pytest.raises(ValueError):
        gateway.run_external(
            ["x"], timeout=1, mode="hook", token_env_names=("MINE",), extra_env={"MINE": "t"}
        )
    for bad in ({"A": "b\0c"}, {"A\0": "b"}):
        with pytest.raises(ValueError):
            gateway.run_external(["x"], timeout=1, mode="hook", extra_env=bad)
    with pytest.raises(ValueError):
        gateway.run_external([], timeout=1, mode="hook")
    with pytest.raises(ValueError):
        gateway.run_external(["a\0b"], timeout=1, mode="hook")
    with pytest.raises(gw.ExecutableMissing):
        gateway.run_external(["sh", "-c", "true"], timeout=1, mode="hook")


def test_run_external_kills_the_whole_group_at_its_timeout(
    fake_forges: FakeForges, spawned: Spawned, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gw, "KILL_GRACE", 0.3)
    gateway = make_gateway(fake_forges, spawned)
    result = gateway.run_external(
        python_child(GRANDCHILD.format(ignore_term=True)), timeout=0.5, mode="hook"
    )
    assert result.timed_out
    reap_orphans(result.pgid)
    spawned.assert_gone()


def test_run_external_stdin_is_devnull_unless_given(fake_forges: FakeForges) -> None:
    gateway = make_gateway(fake_forges)
    code = "import sys;print(repr(sys.stdin.read()))"
    run = gateway.run_external
    assert run(python_child(code), timeout=10, mode="hook").stdout == b"''\n"
    assert run(python_child(code), timeout=10, mode="hook", stdin=b"hi").stdout == b"'hi'\n"


# -- Substitutable runner --------------------------------------------------------------------


def test_a_substituted_runner_sees_the_exact_argv_environment_and_cwd(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def runner(argv: Sequence[str], **kw: Any) -> ProcessResult:
        seen.update(argv=list(argv), **kw)
        seen["cwd_exists"] = os.path.isdir(kw["cwd"]) and os.listdir(kw["cwd"]) == []
        return ProcessResult(1, 1, 0, head(200) + b"{}", b"")

    gateway = stub_gateway(tmp_path, runner)
    gateway.api(GH, "GET", ["repos", "o", "r"], query=[("per_page", "100")])
    assert seen["argv"][1:] == [
        "api",
        "--hostname",
        "github.com",
        "-i",
        "-X",
        "GET",
        "--",
        "repos/o/r?per_page=100",
    ]
    assert seen["env"]["GH_TOKEN"] == TOKEN and seen["cwd_exists"]
    assert not os.path.exists(seen["cwd"])
    assert TOKEN not in " ".join(seen["argv"])


# -- Cleanup under signals (review round 1) ------------------------------------------------------

IGNORES_TERM = "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)"


def blocked_mask() -> list[int]:
    return sorted(int(s) for s in signal.pthread_sigmask(signal.SIG_BLOCK, []))


def test_signals_during_the_kill_grace_still_kill_the_group_and_reraise_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TERM-ignoring child; the first SIGALRM interrupts the call, a SIGINT arrives while
    cleanup holds it, and a second SIGALRM (not held) lands in the grace. The group still gets
    SIGKILL, the leader is reaped, and the first exception is the one that propagates."""
    monkeypatch.setattr(gw, "KILL_GRACE", 1.5)
    count = [0]

    def alarm(signum: int, frame: Any) -> None:
        count[0] += 1
        if count[0] >= 2:
            signal.setitimer(signal.ITIMER_REAL, 0)
        raise Interrupted(count[0])

    def send_sigint() -> None:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})  # only the main thread gets it
        time.sleep(0.9)
        os.kill(os.getpid(), signal.SIGINT)

    started: list[int] = []
    before = blocked_mask()
    old = signal.signal(signal.SIGALRM, alarm)
    sender = threading.Thread(target=send_sigint, daemon=True)
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.5, 0.8)  # at 0.5 s and 1.3 s
        sender.start()
        with pytest.raises(Interrupted) as raised:
            gw.run_process(
                python_child(IGNORES_TERM), env=plain_env(), cwd=os.environ["TMPDIR"],
                timeout=30, on_start=lambda pid, pgid: started.append(pid),
            )  # fmt: skip
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
        sender.join(5)
    assert raised.value.args == (1,)
    assert count[0] == 2  # the second one landed in the grace
    notes = " ".join(getattr(raised.value, "__notes__", []))
    assert "KeyboardInterrupt" in notes and "Interrupted" in notes
    assert blocked_mask() == before
    (pid,) = started
    assert group_members(pid) == [] and not Path(f"/proc/{pid}").exists()


def test_a_signal_during_dir_cleanup_of_an_ordinary_error_is_raised_not_noted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary error is leaving ``private_dirs`` and a SIGINT lands while the dirs are being
    removed: the KeyboardInterrupt propagates (the error is its context), it is not a note."""
    real_remove = gw.remove_tree

    def remove_with_sigint(path: str) -> None:
        os.kill(os.getpid(), signal.SIGINT)  # held until the mask is restored
        real_remove(path)

    monkeypatch.setattr(gw, "remove_tree", remove_with_sigint)
    before = blocked_mask()
    roots: list[str] = []
    with pytest.raises(KeyboardInterrupt) as raised:  # noqa: PT012
        with gw.private_dirs(plain_env(), config=True) as dirs:
            roots.append(dirs.root)
            raise gw._error("network_error", "boom")
    assert isinstance(raised.value.__context__, ForgectlError)
    assert raised.value.__context__.code == "network_error"
    (root,) = roots
    assert not os.path.exists(root)
    assert blocked_mask() == before


def test_a_signal_during_process_cleanup_of_an_ordinary_error_is_raised_not_noted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ordinary error after the spawn starts the cleanup; a SIGINT held during it propagates
    once the mask is restored, and the group is still killed and the leader reaped."""
    real_stop = gw._Leader.stop_group
    started: list[int] = []

    def stop_with_sigint(self: Any) -> None:
        os.kill(os.getpid(), signal.SIGINT)  # held until the leader is reaped
        real_stop(self)

    def fail_after_spawn(pid: int, pgid: int) -> None:
        started.append(pid)
        raise gw._error("internal", "after the spawn")

    monkeypatch.setattr(gw._Leader, "stop_group", stop_with_sigint)
    before = blocked_mask()
    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            gw.run_process(
                python_child("import time;time.sleep(30)"), env=plain_env(),
                cwd=os.environ["TMPDIR"], timeout=30, on_start=fail_after_spawn,
            )  # fmt: skip
    finally:
        for pid in started:  # only the child this test started, if the code under test left it
            if group_members(pid):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
    assert isinstance(raised.value.__context__, ForgectlError)
    assert blocked_mask() == before
    (pid,) = started
    assert group_members(pid) == [] and not Path(f"/proc/{pid}").exists()


def test_a_failure_right_after_the_spawn_restores_the_mask_and_kills_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned_procs: list[Any] = []
    real_popen = gw._popen

    def recording(*args: Any, **kwargs: Any) -> Any:
        proc = real_popen(*args, **kwargs)
        spawned_procs.append(proc)
        return proc

    def broken(self: Any, proc: Any) -> None:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(gw, "_popen", recording)
    monkeypatch.setattr(gw._Leader, "__init__", broken)
    before = blocked_mask()
    with pytest.raises(OSError):
        gw.run_process(
            python_child("import time;time.sleep(30)"), env=plain_env(),
            cwd=os.environ["TMPDIR"], timeout=30,
        )  # fmt: skip
    assert blocked_mask() == before
    (proc,) = spawned_procs
    assert proc.returncode is not None  # reaped
    assert group_members(proc.pid) == []


def test_a_selector_that_cannot_be_made_fails_before_anything_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[int] = []

    def boom() -> Any:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(gw.selectors, "DefaultSelector", boom)
    monkeypatch.setattr(gw, "_popen", lambda *a, **k: started.append(1))
    before = blocked_mask()
    with pytest.raises(OSError):
        gw.run_process(["true"], env=plain_env(), cwd=os.environ["TMPDIR"], timeout=5)
    assert started == [] and blocked_mask() == before


LEAVES_A_GRANDCHILD = (
    "import subprocess,sys;"
    "p=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(30)'],"
    "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);print(p.pid,flush=True)"
)


def kill_exact_orphan(pid: int, pgid: int) -> None:
    """SIGKILL and reap one grandchild reparented to this process (a subreaper), after checking
    through a pidfd that it is still our child in the expected group."""
    fd = os.pidfd_open(pid)
    try:
        assert [m for m in group_members(pgid) if m[0] == pid], "not in the expected group"
        os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG | os.WNOWAIT)  # type: ignore[attr-defined]
        signal.pidfd_send_signal(fd, signal.SIGKILL)  # type: ignore[attr-defined]
        os.waitid(os.P_PIDFD, fd, os.WEXITED)  # type: ignore[attr-defined]
    finally:
        os.close(fd)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs pidfd and /proc")
def test_without_waitid_or_kqueue_a_reaped_leaders_group_is_not_signaled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last fallback: polling reaps the leader, so its group id may be reused and is never
    signaled afterwards; a grandchild it left behind survives (documented, not wanted)."""
    monkeypatch.setattr(gw, "_HAS_WAITID", False)
    monkeypatch.setattr(gw, "_HAS_KQUEUE", False)
    result = gw.run_process(
        python_child(LEAVES_A_GRANDCHILD), env=plain_env(), cwd=os.environ["TMPDIR"], timeout=10
    )
    grandchild = int(result.stdout.split()[0])
    try:
        assert result.returncode == 0
        assert [m for m in group_members(result.pgid) if m[0] == grandchild and m[2] != "Z"]
    finally:
        kill_exact_orphan(grandchild, result.pgid)


@pytest.mark.skipif(not hasattr(select, "kqueue"), reason="kqueue only (macOS, BSD)")
def test_with_kqueue_and_no_waitid_a_grandchild_dies_with_the_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gw, "_HAS_WAITID", False)
    result = gw.run_process(
        python_child(LEAVES_A_GRANDCHILD), env=plain_env(), cwd=os.environ["TMPDIR"], timeout=10
    )
    grandchild = int(result.stdout.split()[0])
    assert result.returncode == 0
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)  # only a probe; never a signal to an unknown process
        except ProcessLookupError:
            break
        with contextlib.suppress(ChildProcessError):
            os.waitpid(grandchild, os.WNOHANG)
        time.sleep(0.05)
    else:
        pytest.fail("the grandchild outlived its group")


@pytest.mark.skipif(not hasattr(select, "kqueue"), reason="kqueue only (macOS, BSD)")
def test_with_kqueue_a_deadline_still_kills_the_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw, "_HAS_WAITID", False)
    monkeypatch.setattr(gw, "KILL_GRACE", 0.4)
    result = gw.run_process(
        python_child(IGNORES_TERM), env=plain_env(), cwd=os.environ["TMPDIR"], timeout=0.5
    )
    assert result.timed_out and result.returncode == -9


# -- Caps through the fake (review round 1) ---------------------------------------------------


def test_the_gateway_stdout_cap_is_honored_and_stops_the_child_with_sigterm_first(
    fake_forges: FakeForges, spawned: Spawned
) -> None:
    # 10 MB: the fake blocks writing it once the pipe is full, so it is still running at the cap
    fake_forges.answer("gh", endpoint_re=".", body_text="x" * 1000, body_repeat=10_000)
    gateway = make_gateway(fake_forges, spawned, timeout=20, max_stdout=1000)
    started = time.monotonic()
    with pytest.raises(ForgectlError) as raised:
        gateway.api(GH, "GET", ["x"])
    assert raised.value.code == "upstream_error"
    assert time.monotonic() - started < 10
    assert only_call(fake_forges).signals == ["SIGTERM"]
    spawned.assert_gone()


# -- Environment (review round 1) ----------------------------------------------------------------


def test_a_relative_stored_mode_config_dir_is_made_absolute() -> None:
    dirs = gw.PrivateDirs("/r", "/r/cwd", "/r/config")
    source = {"PATH": "/usr/bin", "HOME": "/h", "GH_CONFIG_DIR": "rel/cfg"}
    env = gw.build_environment(source, dirs=dirs, backend="github")
    assert env["GH_CONFIG_DIR"] == os.path.abspath("rel/cfg")


@pytest.mark.parametrize("value", ["relative", ".", "/nonexistent/forgectl-tmp", ""])
def test_an_unusable_tmpdir_falls_back_to_tmp_never_the_cwd_or_the_process_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "relative").mkdir()
    monkeypatch.chdir(work)
    seen: dict[str, Any] = {}

    def runner(argv: Sequence[str], **kw: Any) -> ProcessResult:
        seen.update(cwd=kw["cwd"], tmpdir=kw["env"]["TMPDIR"])
        return ProcessResult(1, 1, 0, head(200) + b"{}", b"")

    gateway = stub_gateway(tmp_path, runner)
    gateway._environ = {**gateway.environ, "TMPDIR": value}  # process TMPDIR stays the private one
    gateway.api(GH, "GET", ["x"])
    assert seen["tmpdir"] == os.path.realpath("/tmp")
    assert os.path.dirname(os.path.dirname(seen["cwd"])) == os.path.realpath("/tmp")
    assert os.listdir(work / "relative") == [] and sorted(os.listdir(work)) == ["relative"]


def test_an_unwritable_tmpdir_falls_back_to_tmp(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        assert gw._tmpdir({"TMPDIR": str(locked)}) == "/tmp"
        assert gw._tmpdir({"TMPDIR": str(tmp_path)}) == str(tmp_path)
    finally:
        locked.chmod(0o700)


@pytest.mark.parametrize("target", [GH, GL_SELF])
def test_the_bus_is_made_unreachable_not_only_unnamed_where_it_must_not_be_used(
    fake_forges: FakeForges, tmp_path: Path, target: Target
) -> None:
    """gh 2.87.3 finds the default per-user bus socket with no bus variable at all, so where
    the bus must stay unused the gateway names a fixed unusable address instead."""
    fake_forges.default_bus_socket(present=True)
    fake_forges.keyring_login(target.program, target.host, "ring-token")
    script_ok(fake_forges, target.program)
    var = target.config_dir_variable
    config = tmp_path / "my-config"
    config.mkdir()
    env = {
        var: str(config),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x",
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
    }
    gateway = make_gateway(fake_forges, env=env)
    gateway.api(target, "GET", ["user"])  # token mode
    stored = Target(target.backend, target.host)  # stored-login mode, the caller's own dir
    if target.backend == "github":
        with pytest.raises(ForgectlError):  # gh api reads only file logins: exit 4
            gateway.api(stored, "GET", ["user"])
    else:
        gateway.api(stored, "GET", ["user"])
    token_call, stored_call = fake_forges.calls()
    for call in (token_call, stored_call):
        assert call.env["DBUS_SESSION_BUS_ADDRESS"] == gw.NO_BUS_ADDRESS
        assert "XDG_RUNTIME_DIR" not in call.env
    assert_call_safe(token_call, host=target.host)
    assert_call_safe(stored_call, host=target.host, mode="stored", caller_config_dir=True)
    assert stored_call.token_sent != "ring-token"


def test_the_fixed_unusable_bus_address_matches_the_harness() -> None:
    from .harness.safety import NO_BUS_ADDRESS

    assert gw.NO_BUS_ADDRESS == NO_BUS_ADDRESS == "unix:path=/nonexistent/forgectl-no-bus"


def test_glab_1_89_with_no_credential_is_auth_required(fake_forges: FakeForges) -> None:
    fake_forges.glab_unauthenticated_without_credential()
    gateway = make_gateway(fake_forges, env={"GLAB_CONFIG_DIR": None, "XDG_CONFIG_HOME": None})
    for write in (False, True):
        with pytest.raises(ForgectlError) as raised:
            if write:
                gateway.api(Target("gitlab", "gitlab.com"), "POST", ["x"], body={},
                            write=True, expect_status=WROTE)  # fmt: skip
            else:
                gateway.api(Target("gitlab", "gitlab.com"), "GET", ["user"])
        assert raised.value.code == "auth_required"
    assert [c.request for c in fake_forges.calls()] == [None, None]  # nothing was sent


def test_a_hostile_caller_directory_cannot_steer_or_run_anything(
    fake_forges: FakeForges, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "hostile"
    git = repo / ".git"
    (git / "hooks").mkdir(parents=True)
    marker = tmp_path / "ran"
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "config").write_text(
        "[core]\n"
        f"\tfsmonitor = touch {marker}\n"
        f"\thooksPath = {repo}/hooks-dir\n"
        '[remote "origin"]\n'
        "\turl = https://evil.example.test/evil/repo.git\n"
    )
    (repo / "hooks-dir").mkdir()
    for name in ("post-checkout", "pre-commit", "fsmonitor-watchman"):
        hook = repo / "hooks-dir" / name
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
    monkeypatch.chdir(repo)
    script_ok(fake_forges, "gh")
    script_ok(fake_forges, "glab")
    gateway = make_gateway(fake_forges)
    gateway.api(GH, "GET", ["repos", "o", "r"])
    gateway.api(GL, "GET", ["projects", "7"])
    gateway.api(GL_SELF, "GET", ["projects", "7"])
    for call in fake_forges.calls():
        assert call.repo_context is None and call.cwd_real != str(repo)
        assert "-R" not in call.argv and "--repo" not in call.argv
        assert call.argv[-1] in ("repos/o/r", "projects/7")
        assert_call_safe(call, host=call.request["host"] if call.request else "")
    assert not marker.exists()


# -- Hook machinery round 1 (#20) ------------------------------------------------------------------


def test_run_process_accepts_the_config_maximum_timeout() -> None:
    """A timeout past the selector's int-millisecond range (about 24.8 days) still runs."""
    result = gw.run_process(
        ["/bin/sh", "-c", "exit 0"], env=plain_env(), cwd=os.environ["TMPDIR"],
        timeout=100 * 365 * 86400.0,
    )  # fmt: skip
    assert result.returncode == 0 and not result.timed_out


def test_remove_tree_never_chmods_through_a_symlink(tmp_path: Path) -> None:
    """An unremovable directory holding a symlink: the retry fixes the directory, never the
    symlink's target."""
    victim = tmp_path / "victim"
    victim.write_text("x")
    victim.chmod(0o644)
    top = tmp_path / "forgectl-top"
    inner = top / "d"
    inner.mkdir(parents=True)
    (inner / "link").symlink_to(victim)
    inner.chmod(0o500)  # the link cannot be unlinked until the directory is writable
    gw.remove_tree(str(top))
    assert not top.exists()
    assert (victim.stat().st_mode & 0o777) == 0o644


def test_remove_tree_never_touches_the_parent_of_the_top_path(tmp_path: Path) -> None:
    """A tree already deleted (a hook removed its own root) leaves the parent's mode alone."""
    base = tmp_path / "base"
    base.mkdir()
    base.chmod(0o755)
    gw.remove_tree(str(base / "forgectl-gone"))
    assert (base.stat().st_mode & 0o777) == 0o755
    top = base / "forgectl-top"
    top.mkdir()
    top.chmod(0o500)
    gw.remove_tree(str(top))  # the top itself is ours to fix; its parent is not
    assert not top.exists() and (base.stat().st_mode & 0o777) == 0o755


def wait_zombie(pid: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stat = Path(f"/proc/{pid}/stat").read_text()
        if stat.rpartition(")")[2].split()[0] == "Z":
            return
        time.sleep(0.02)
    raise AssertionError(f"{pid} did not exit")


def test_spawn_detached_restores_the_mask_when_on_start_raises() -> None:
    before = blocked_mask()
    started: list[int] = []

    def boom(pid: int, _pgid: int) -> None:
        started.append(pid)
        raise RuntimeError("on_start")

    with pytest.raises(RuntimeError):
        gw.spawn_detached(
            [sys.executable, "-I", "-c", "import sys; sys.stdin.read()"],
            env=plain_env(), cwd="/", stdin=b"", on_start=boom,
        )  # fmt: skip
    assert blocked_mask() == before
    (pid,) = started
    gw.detached_children()
    assert not Path(f"/proc/{pid}").exists()


def test_spawn_detached_reaps_earlier_children_that_exited() -> None:
    first: list[int] = []
    gw.spawn_detached(
        ["/bin/sh", "-c", "exit 0"], env=plain_env(), cwd="/", stdin=b"",
        on_start=lambda pid, _pgid: first.append(pid),
    )  # fmt: skip
    wait_zombie(first[0])
    second: list[int] = []
    gw.spawn_detached(
        ["/bin/sh", "-c", "exit 0"], env=plain_env(), cwd="/", stdin=b"",
        on_start=lambda pid, _pgid: second.append(pid),
    )  # fmt: skip
    assert not Path(f"/proc/{first[0]}").exists()  # reaped by the second spawn's entry pass
    wait_zombie(second[0])
    assert gw.detached_children() == []


def test_spawn_detached_waits_for_an_acknowledgement_when_asked() -> None:
    started: list[int] = []
    gw.spawn_detached(
        [sys.executable, "-I", "-c", "import os, sys; sys.stdin.read(); os.write(1, b'1')"],
        env=plain_env(), cwd="/", stdin=b"spec", on_start=lambda p, _g: started.append(p),
        ack_timeout=10,
    )  # fmt: skip
    with pytest.raises(gw.NoAcknowledgement):
        gw.spawn_detached(
            ["/bin/sh", "-c", "exit 0"], env=plain_env(), cwd="/", stdin=b"",
            on_start=lambda p, _g: started.append(p), ack_timeout=10,
        )  # fmt: skip
    for pid in started:
        wait_zombie(pid) if Path(f"/proc/{pid}").exists() else None
    assert gw.detached_children() == []
    assert all(not Path(f"/proc/{pid}").exists() for pid in started)


# -- Round 2 fixes ---------------------------------------------------------------------------------


def test_remove_tree_removes_a_mode_000_directory_nested_in_a_read_only_one(
    tmp_path: Path,
) -> None:
    """R2-2: ``top/cwd/x`` mode 500 holding ``x/y`` mode 000 (rmtree reports ``os.open``) is
    removed completely, and nothing is raised."""
    top = tmp_path / "forgectl-top"
    x = top / "cwd" / "x"
    (x / "y").mkdir(parents=True)
    (x / "y").chmod(0o000)
    x.chmod(0o500)
    gw.remove_tree(str(top))
    assert not top.exists()


def test_remove_tree_never_raises_for_an_entry_it_cannot_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-2: a retry that fails in any way (here ``_chmod_dir`` raising) is suppressed."""

    def boom(*_args: Any) -> None:
        raise TypeError("boom")

    top = tmp_path / "forgectl-top"
    (top / "d" / "e").mkdir(parents=True)
    (top / "d").chmod(0o500)
    monkeypatch.setattr(gw, "_chmod_dir", boom)
    gw.remove_tree(str(top))  # cannot remove d/e, and does not raise
    monkeypatch.undo()
    gw.remove_tree(str(top))
    assert not top.exists()


def test_chmod_dir_never_follows_a_symlink_or_changes_a_file(tmp_path: Path) -> None:
    """R2-5: ``_chmod_dir`` opens each component under ``top`` without following symlinks; a
    symlinked component or a non-directory is skipped, and a mode 000 directory is fixed."""
    victim = tmp_path / "victim"
    victim.mkdir()
    victim.chmod(0o755)
    top = tmp_path / "forgectl-top"
    (top / "d").mkdir(parents=True)
    (top / "link").symlink_to(victim)
    (top / "f").write_text("x")
    (top / "f").chmod(0o644)
    gw._chmod_dir(str(top), str(top / "link"))
    gw._chmod_dir(str(top), str(top / "link" / "sub"))
    gw._chmod_dir(str(top), str(top / "f"))
    assert (victim.stat().st_mode & 0o777) == 0o755
    assert ((top / "f").stat().st_mode & 0o777) == 0o644
    (top / "d").chmod(0o000)
    gw._chmod_dir(str(top), str(top / "d"))
    assert ((top / "d").stat().st_mode & 0o777) == 0o700
    gw.remove_tree(str(top))


def test_spawn_detached_kills_the_group_before_closing_the_ack_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-3: with no acknowledgement, the group is killed while the read end is still open, so
    the child can never see its acknowledgement succeed after forgectl gave up."""
    seen: list[bool] = []
    real = gw._signal_group

    def record(pgid: int, signum: int) -> Any:
        (proc,) = [p for p in gw._DETACHED if p.pid == pgid]
        assert proc.stdout is not None
        seen.append(proc.stdout.closed)
        return real(pgid, signum)

    monkeypatch.setattr(gw, "_signal_group", record)
    started: list[int] = []
    with pytest.raises(gw.NoAcknowledgement):
        gw.spawn_detached(
            [sys.executable, "-I", "-c", "import time; time.sleep(30)"], env=plain_env(),
            cwd="/", stdin=b"", on_start=lambda p, _g: started.append(p), ack_timeout=0.3,
        )  # fmt: skip
    assert seen == [False]
    (proc,) = [p for p in gw._DETACHED if p.pid == started[0]] or [None]
    assert proc is None or (proc.stdout is not None and proc.stdout.closed)
    assert gw.detached_children() == []
