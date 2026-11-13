"""The live harness's safety guards, with fakes only: no host is ever reached."""

from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgectl.config import load_config as load_forgectl_config

from .harness.safety import AMBIENT_TOKEN_VARS
from .live import guards
from .live.guards import LiveRefused

SENTINEL = "ambient-sentinel-value-9c41"


def cfg(backend: str = "github", **extra: str | None) -> guards.LiveConfig:
    host = guards.BACKEND_HOSTS[backend]
    return guards.LiveConfig(backend, host, "tok-live-0001", "me-login", **extra)  # type: ignore[arg-type]


def settings_env(backend: str = "github") -> dict[str, str]:
    names = guards.LiveSettings.for_backend(backend)
    return {names.token: "tok-live-0001", names.user: "me-login"}


@pytest.mark.parametrize("host", ["github.com", "gitlab.com"])
def test_the_two_public_hosts_are_allowed(host: str) -> None:
    assert guards.require_host(host) == host


@pytest.mark.parametrize(
    "host",
    [
        "",
        "GitHub.com",
        "github.com:443",
        "www.github.com",
        "api.github.com",
        "github.com.example.test",
        "example.test/github.com",
        "gitlab.example.test",
        "ghe.example.test",
        "github.com ",
        "localhost",
    ],
)
def test_every_other_host_is_refused(host: str) -> None:
    with pytest.raises(LiveRefused):
        guards.require_host(host)


def test_there_is_no_override_of_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGECTL_LIVE_HOSTS", "gitlab.example.test")
    monkeypatch.setenv("FORGECTL_LIVE_ALLOW_HOST", "gitlab.example.test")
    assert guards.ALLOWED_HOSTS == {"github.com", "gitlab.com"}
    with pytest.raises(LiveRefused):
        guards.require_host("gitlab.example.test")


def test_a_throwaway_name_has_the_prefix_and_a_run_id() -> None:
    run_id = guards.new_run_id(datetime(2026, 11, 12, 10, 15, tzinfo=UTC), "ab12cd34")
    assert run_id == "20261112t101500z-ab12cd34"
    assert guards.throwaway_name(run_id) == "forgectl-live-20261112t101500z-ab12cd34"


def test_run_ids_differ_between_runs() -> None:
    assert guards.new_run_id() != guards.new_run_id()


@pytest.mark.parametrize(
    "name",
    [
        "",
        "forgectl-live-",
        "my-repo",
        "forgectl-live-20261112t101500z-ab12cd34-extra",
        "Forgectl-Live-20261112t101500z-ab12cd34",
        "forgectl-live-20261112t101500z-ab12cd3",
        "x/forgectl-live-20261112t101500z-ab12cd34",
        "forgectl-live-20261112t101500z-ab12cd34/../other",
        "forgectl-live-20261112t101500z-ab12cd34\n",
        "forgectl-livé-20261112t101500z-ab12cd34",
    ],
)
def test_a_name_without_the_exact_shape_is_refused(name: str) -> None:
    with pytest.raises(LiveRefused):
        guards.require_throwaway_name(name)


def test_the_resolved_account_must_be_the_declared_one() -> None:
    guards.require_account("Me-Login", "me-login")
    with pytest.raises(LiveRefused):
        guards.require_account("me-login", "someone-else")
    with pytest.raises(LiveRefused):
        guards.require_account("me-login", "")


def test_there_is_no_default_expected_account() -> None:
    with pytest.raises(LiveRefused):
        guards.require_account("", "anyone")
    config, reason = guards.load_config("github", {"FORGECTL_LIVE_GITHUB_TOKEN": "tok-live-0001"})
    assert config is None
    assert reason is not None and "FORGECTL_LIVE_GITHUB_USER" in reason


def test_a_resource_outside_the_declared_namespace_is_refused() -> None:
    guards.require_owned_by("ME-login", "me-login")
    for owner in ("", "an-org", "me-login-2"):
        with pytest.raises(LiveRefused):
            guards.require_owned_by(owner, "me-login")


@pytest.mark.parametrize("backend", ["github", "gitlab"])
def test_absent_settings_skip_with_the_names_to_set(backend: str) -> None:
    config, reason = guards.load_config(backend, {})
    names = guards.LiveSettings.for_backend(backend)
    assert config is None
    assert reason is not None and names.token in reason and names.user in reason


@pytest.mark.parametrize("backend", ["github", "gitlab"])
def test_present_settings_load(backend: str) -> None:
    config, reason = guards.load_config(backend, settings_env(backend))
    assert reason is None and config is not None
    assert (config.host, config.token, config.login) == (
        guards.BACKEND_HOSTS[backend],
        "tok-live-0001",
        "me-login",
    )
    assert config.second_token is None


@pytest.mark.parametrize("marker", ["CI", "GITHUB_ACTIONS", "GITLAB_CI"])
def test_ci_skips_even_with_every_setting_present(marker: str) -> None:
    config, reason = guards.load_config("github", {**settings_env(), marker: ""})
    assert config is None and reason == "live tests never run in CI"


def test_without_run_live_the_config_skips() -> None:
    config, reason = guards.load_config("github", settings_env(), run_live=False)
    assert config is None and reason is not None and "--run-live" in reason


@pytest.mark.parametrize("value", ["tok en", "tok\nen", "tok\x00en", "tök"])
def test_a_malformed_token_is_a_refusal_not_a_skip(value: str) -> None:
    env = {**settings_env(), "FORGECTL_LIVE_GITHUB_TOKEN": value}
    with pytest.raises(LiveRefused):
        guards.load_config("github", env)


def test_whitespace_around_a_setting_is_trimmed() -> None:
    env = {"FORGECTL_LIVE_GITHUB_TOKEN": " tok-live-0001\n", "FORGECTL_LIVE_GITHUB_USER": " me \n"}
    config, _ = guards.load_config("github", env)
    assert config is not None and (config.token, config.login) == ("tok-live-0001", "me")


def test_half_a_second_account_is_no_second_account() -> None:
    env = {**settings_env(), "FORGECTL_LIVE_GITHUB_SECOND_TOKEN": "tok-live-0002"}
    config, _ = guards.load_config("github", env)
    assert config is not None and config.second_token is None
    env["FORGECTL_LIVE_GITHUB_SECOND_USER"] = "other-login"
    config, _ = guards.load_config("github", env)
    assert config is not None and config.second_token == "tok-live-0002"
    assert config.second_login == "other-login"


def test_a_config_never_prints_its_tokens() -> None:
    config = cfg(second_token="tok-live-0002", second_login="x")
    assert "tok-live" not in repr(config) and "tok-live" not in str(config)


# The child environment ---------------------------------------------------------------------


def dirs_for(tmp_path: Path) -> guards.PrivateDirs:
    return guards.make_private_dirs(tmp_path / "live")


def hostile_source(tmp_path: Path) -> dict[str, str]:
    source = {name: f"{SENTINEL}-{name}" for name in guards.FORBIDDEN_VARS}
    source.update(
        PATH=f"/usr/bin:relative/bin::{tmp_path}/bin",
        HOME="/home/the-real-user",
        XDG_CONFIG_HOME="/home/the-real-user/.config",
        GH_CONFIG_DIR="/home/the-real-user/.config/gh",
        GLAB_CONFIG_DIR="/home/the-real-user/.config/glab",
        HTTPS_PROXY="http://proxy.example.test:3128",
        LANG="C.UTF-8",
        UNRELATED="x",
    )
    return source


@pytest.mark.parametrize("backend", ["github", "gitlab"])
def test_the_child_environment_is_built_from_scratch(tmp_path: Path, backend: str) -> None:
    dirs, source = dirs_for(tmp_path), hostile_source(tmp_path)
    env = guards.child_env(cfg(backend), dirs, source)
    assert not [v for v in env.values() if SENTINEL in v]
    assert not [
        n for n in guards.FORBIDDEN_VARS if n in env and n not in ("GH_TOKEN", "GITLAB_TOKEN")
    ]
    own = guards.token_variable(backend)
    assert env[own] == "tok-live-0001"
    assert [n for n in ("GH_TOKEN", "GITLAB_TOKEN") if n in env] == [own]
    assert "UNRELATED" not in env
    assert env["HTTPS_PROXY"] == "http://proxy.example.test:3128"
    assert env["PATH"] == f"/usr/bin:{tmp_path}/bin"
    guards.assert_env_safe(env, dirs, source)


def test_home_and_every_config_directory_are_private_and_empty(tmp_path: Path) -> None:
    dirs, source = dirs_for(tmp_path), hostile_source(tmp_path)
    env = guards.child_env(cfg(), dirs, source)
    for name in ("HOME", "XDG_CONFIG_HOME", "GH_CONFIG_DIR", "GLAB_CONFIG_DIR", "TMPDIR"):
        path = Path(env[name])
        assert path.is_relative_to(dirs.root), name
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, name
    assert env["HOME"] != source["HOME"]
    assert list(dirs.gh_config.iterdir()) == [] and list(dirs.glab_config.iterdir()) == []
    for name in ("XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        assert Path(env[name]).is_relative_to(dirs.home), name


def test_no_variable_is_ever_left_set_that_names_another_host(tmp_path: Path) -> None:
    env = guards.child_env(cfg("gitlab"), dirs_for(tmp_path), hostile_source(tmp_path))
    for name in ("GH_HOST", "GITLAB_HOST", "GL_HOST", "GH_REPO"):
        assert name not in env


def test_the_forbidden_variables_include_every_one_the_process_tests_plant() -> None:
    assert set(AMBIENT_TOKEN_VARS) <= set(guards.FORBIDDEN_VARS)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_ACCESS_TOKEN", "GITLAB_TOKEN"):
        assert name in guards.FORBIDDEN_VARS
    for name in ("GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN"):
        assert name in guards.FORBIDDEN_VARS


def test_forgectl_gets_its_token_only_through_the_dedicated_variable(tmp_path: Path) -> None:
    dirs, source = dirs_for(tmp_path), hostile_source(tmp_path)
    env = guards.forgectl_child_env(cfg(), dirs, source)
    assert env["FORGECTL_LIVE_GITHUB_TOKEN"] == "tok-live-0001"
    assert "GH_TOKEN" not in env and "GITLAB_TOKEN" not in env
    guards.assert_env_safe(env, dirs, source)


def test_the_second_account_uses_its_own_token_and_refuses_when_absent(tmp_path: Path) -> None:
    dirs, source = dirs_for(tmp_path), hostile_source(tmp_path)
    two = cfg(second_token="tok-live-0002", second_login="other-login")
    assert guards.child_env(two, dirs, source, second=True)["GH_TOKEN"] == "tok-live-0002"
    assert (
        guards.forgectl_child_env(two, dirs, source, second=True)["FORGECTL_LIVE_GITHUB_TOKEN"]
        == "tok-live-0002"
    )
    with pytest.raises(LiveRefused):
        guards.child_env(cfg(), dirs, source, second=True)


def test_the_last_check_refuses_a_tampered_environment(tmp_path: Path) -> None:
    dirs, source = dirs_for(tmp_path), hostile_source(tmp_path)
    good = guards.child_env(cfg(), dirs, source)
    guards.assert_env_safe(good, dirs, source)
    for bad in (
        {**good, "GITHUB_TOKEN": "x"},
        {**good, "GITHUB_ACCESS_TOKEN": "x"},
        {**good, "GH_HOST": "ghe.example.test"},
        {**good, "GITLAB_TOKEN": "x"},  # two host tokens
        {**good, "HOME": source["HOME"]},
        {**good, "GH_CONFIG_DIR": source["GH_CONFIG_DIR"]},
        {**good, "UNRELATED_NAME": source["GITHUB_TOKEN"]},  # an ambient value under any name
    ):
        with pytest.raises(LiveRefused):
            guards.assert_env_safe(bad, dirs, source)


def test_the_written_forgectl_config_is_valid_and_names_only_the_dedicated_variable(
    tmp_path: Path,
) -> None:
    for backend in ("github", "gitlab"):
        dirs = guards.make_private_dirs(tmp_path / backend)
        path = guards.write_forgectl_config(dirs, backend)
        host = guards.BACKEND_HOSTS[backend]
        loaded = load_forgectl_config(None, env={"XDG_CONFIG_HOME": str(dirs.xdg_config)})
        assert loaded.config.hosts[host].token_env == f"FORGECTL_LIVE_{backend.upper()}_TOKEN"
        assert set(loaded.config.hosts) == {host}
        assert "tok-live" not in path.read_text(encoding="utf-8")
