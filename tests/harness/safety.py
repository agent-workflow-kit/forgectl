"""Safety rules every process test asserts on every forge call (interface: Invoking the backends).

``assert_call_safe(call, ...)`` checks one logged call. It raises ``AssertionError`` naming the
broken rule, so a leaked token variable or an untrusted string in argv fails the test.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

from .forges import Call

AMBIENT_TOKEN_VARS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITLAB_TOKEN",
    "GITLAB_ACCESS_TOKEN",
    "OAUTH_TOKEN",
)
# Planted by ``run_forgectl`` in the caller's environment; checked by default on every call.
AMBIENT_SENTINELS = {
    name: f"ambient-sentinel-{name.lower().replace('_', '-')}-5d81" for name in AMBIENT_TOKEN_VARS
}
FIXED_SETTINGS = {
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "NO_COLOR": "1",
    "GLAB_SEND_TELEMETRY": "false",
    "GLAB_CHECK_UPDATE": "false",
    "GLAB_SHOW_WHATS_NEW": "false",
    "NO_PROMPT": "1",
}
NEVER_PASSED = ("GH_FORCE_TTY", "CLICOLOR_FORCE", "GH_REPO", "GH_HOST")
BASE_ALLOWED = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "GLAB_CA_CERT",
    "GIT_CEILING_DIRECTORIES",
    *FIXED_SETTINGS,
}
BUS_VARS = {"DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"}
STORED_ONLY = {"GH_CONFIG_DIR", "GLAB_CONFIG_DIR", "XDG_CONFIG_HOME", *BUS_VARS}

Mode = Literal["token", "stored"]


def token_variable(backend: str, host: str) -> str:
    """The one token variable the child gets in token mode (interface: Credentials)."""
    if backend == "glab":
        return "GITLAB_TOKEN"
    return (
        "GH_TOKEN" if host == "github.com" or host.endswith(".ghe.com") else "GH_ENTERPRISE_TOKEN"
    )


def hostname_values(argv: list[str]) -> list[str]:
    """Every ``--hostname`` value in argv, in either ``--hostname X`` or ``--hostname=X`` form."""
    found: list[str] = []
    for index, arg in enumerate(argv):
        if arg == "--":
            break
        if arg == "--hostname":
            found.append(argv[index + 1] if index + 1 < len(argv) else "")
        elif arg.startswith("--hostname="):
            found.append(arg.partition("=")[2])
    return found


def assert_call_safe(
    call: Call,
    *,
    host: str,
    mode: Mode = "token",
    untrusted: Iterable[str] = (),
    ambient: Mapping[str, str] | None = None,
    gitlab_host: str | None = None,
    caller_config_dir: bool = False,
) -> None:
    """Assert every safety rule on one logged call.

    ``untrusted`` are strings the test planted in forge-controlled input; none may reach argv or
    the environment. ``ambient`` are token variables in the caller's own environment that must not
    reach the child (default: the sentinels ``run_forgectl`` plants); no child argv element or
    environment value may contain any of their values. ``gitlab_host`` is the expected
    ``GITLAB_HOST`` for a GitLab host other than gitlab.com (default: ``host``), in the form
    ``<host>[:port]<api_prefix>``. ``caller_config_dir`` says the caller set this backend's config
    dir, so stored-login mode must pass no session bus variable.
    """
    env = call.env
    where = f"{call.backend} {' '.join(call.argv)}"

    present = [name for name in AMBIENT_TOKEN_VARS if name in env]
    if mode == "token":
        expected = token_variable(call.backend, host)
        assert present == [expected], f"{where}: token variables {present}, want only {expected}"
        assert env[expected], f"{where}: empty {expected}"
    else:
        assert not present, f"{where}: stored-login mode passed token variables {present}"

    for name in present:
        value = env[name]
        in_argv = [a for a in call.argv if value and value in a]
        assert not in_argv, f"{where}: the {name} value is in argv"
        elsewhere = [k for k, v in env.items() if k != name and value and value in v]
        assert not elsewhere, f"{where}: the {name} value is also inside {elsewhere}"

    for name, value in (AMBIENT_SENTINELS if ambient is None else ambient).items():
        assert value, "an empty ambient value proves nothing"
        leaked = [k for k, v in env.items() if value in v]
        assert not leaked, f"{where}: ambient value of {name} reached {leaked}"
        in_argv = [a for a in call.argv if value in a]
        assert not in_argv, f"{where}: ambient value of {name} is in argv"

    for name in NEVER_PASSED:
        assert name not in env, f"{where}: {name} must never pass through"
    for name, value in FIXED_SETTINGS.items():
        assert env.get(name) == value, f"{where}: {name}={env.get(name)!r}, want {value!r}"

    allowed = set(BASE_ALLOWED) | set(present)
    if mode == "stored":
        allowed |= STORED_ONLY
        if caller_config_dir:
            allowed -= BUS_VARS
    else:
        allowed |= {"GH_CONFIG_DIR" if call.backend == "gh" else "GLAB_CONFIG_DIR"}
    if call.backend == "glab" and host != "gitlab.com":
        allowed.add("GITLAB_HOST")
    stray = sorted(set(env) - allowed)
    assert not stray, f"{where}: environment not built from scratch, stray {stray}"

    for entry in env.get("PATH", "").split(os.pathsep):
        assert os.path.isabs(entry), f"{where}: PATH entry {entry!r} is not absolute"

    raw_tmpdir = env.get("TMPDIR")
    assert raw_tmpdir and os.path.isabs(raw_tmpdir), (
        f"{where}: TMPDIR {raw_tmpdir!r} is missing or not absolute"
    )
    tmpdir = os.path.realpath(raw_tmpdir)
    assert call.cwd_entries == [], f"{where}: cwd not empty: {call.cwd_entries}"
    assert call.cwd_mode == 0o700, f"{where}: cwd mode {oct(call.cwd_mode or 0)}, want 0o700"
    assert call.cwd_real.startswith(tmpdir + os.sep), (
        f"{where}: cwd {call.cwd_real} is not under TMPDIR {tmpdir}"
    )
    assert call.repo_context is None, f"{where}: a repository was discoverable: {call.repo_context}"
    ceiling = env.get("GIT_CEILING_DIRECTORIES")
    assert ceiling and os.path.realpath(ceiling) == os.path.realpath(os.path.dirname(call.cwd)), (
        f"{where}: GIT_CEILING_DIRECTORIES {ceiling!r} is not the parent of cwd {call.cwd}"
    )

    if mode == "token":
        _assert_private_config_dir(call, where, tmpdir)

    _assert_routing(call, where, host, gitlab_host)
    assert_no_untrusted_in_argv(call, untrusted)
    assert not call.placeholders, f"{where}: endpoint used a CLI placeholder"
    if call.request is not None:
        assert not call.request["full_url_endpoint"], f"{where}: full-URL endpoint"
        assert not call.request["file_fields"], f"{where}: -F read a file"


def _assert_private_config_dir(call: Call, where: str, tmpdir: str) -> None:
    var = "GH_CONFIG_DIR" if call.backend == "gh" else "GLAB_CONFIG_DIR"
    value = call.env.get(var)
    assert value, f"{where}: token mode needs a private {var}"
    real = call.config_dir_real or os.path.realpath(value)
    assert real.startswith(tmpdir + os.sep), f"{where}: {var} {real} is not under TMPDIR"
    cwd = call.cwd_real
    assert real != cwd and not real.startswith(cwd + os.sep), f"{where}: {var} is in the cwd"
    assert call.config_mode == 0o700, (
        f"{where}: {var} mode {oct(call.config_mode or 0)}, want 0o700"
    )
    assert call.config_entries == [], f"{where}: {var} not empty at start: {call.config_entries}"


def _assert_routing(call: Call, where: str, host: str, gitlab_host: str | None) -> None:
    """interface.md, Routing: gh always names the host; glab by host kind."""
    names = hostname_values(call.argv)
    if call.backend == "gh":
        if call.argv[:1] == ["api"] or call.argv[:2] == ["auth", "token"]:
            assert names == [host], f"{where}: gh needs exactly one --hostname {host}, got {names}"
        return
    if host == "gitlab.com":
        assert "GITLAB_HOST" not in call.env, f"{where}: gitlab.com takes no GITLAB_HOST"
        if call.argv[:1] == ["api"]:
            assert names == ["gitlab.com"], (
                f"{where}: gitlab.com needs exactly one --hostname gitlab.com, got {names}"
            )
        return
    want = gitlab_host or host
    got = call.env.get("GITLAB_HOST")
    assert got == want, f"{where}: GITLAB_HOST {got!r}, want {want!r}"
    assert not names, f"{where}: a self-managed host takes no --hostname, got {names}"


def assert_no_untrusted_in_argv(call: Call, untrusted: Iterable[str]) -> None:
    """No planted string in any argv element, across argv elements, or in any env value."""
    argv = call.argv
    joined = ["".join(argv), " ".join(argv)]
    for planted in untrusted:
        assert planted, "an empty string proves nothing"
        hits = [a for a in argv if planted in a]
        assert not hits, f"{call.backend}: untrusted text {planted!r} in argv: {hits}"
        assert not any(planted in j for j in joined), (
            f"{call.backend}: untrusted text {planted!r} split across argv elements"
        )
        in_env = [k for k, v in call.env.items() if planted in v or planted in k]
        assert not in_env, f"{call.backend}: untrusted text {planted!r} in the environment {in_env}"


def reference_env(
    backend: str,
    host: str,
    *,
    root: Path,
    path: str,
    token: str | None = "tok-fixture-0001",
    mode: Mode = "token",
    gitlab_host: str | None = None,
) -> tuple[dict[str, str], Path]:
    """A child environment and private cwd built the way the interface says the gateway builds
    them. For tests of the harness itself and as a reference for gateway tests; the gateway
    must build its own.
    """
    private = root / "private"
    cwd = private / "cwd"
    cwd.mkdir(parents=True, mode=0o700)
    cwd.chmod(0o700)
    home = root / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": path,
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TMPDIR": str(root),
        "GIT_CEILING_DIRECTORIES": str(cwd.parent),
        **FIXED_SETTINGS,
    }
    if backend == "glab" and host != "gitlab.com":
        env["GITLAB_HOST"] = gitlab_host or host
    if mode == "token":
        assert token
        env[token_variable(backend, host)] = token
        config = root / "config"
        config.mkdir(mode=0o700)
        config.chmod(0o700)
        env["GH_CONFIG_DIR" if backend == "gh" else "GLAB_CONFIG_DIR"] = str(config)
    return env, cwd
