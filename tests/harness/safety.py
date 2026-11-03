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
STORED_ONLY = {
    "GH_CONFIG_DIR",
    "GLAB_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
}

Mode = Literal["token", "stored"]


def token_variable(backend: str, host: str) -> str:
    """The one token variable the child gets in token mode (interface: Credentials)."""
    if backend == "glab":
        return "GITLAB_TOKEN"
    return (
        "GH_TOKEN" if host == "github.com" or host.endswith(".ghe.com") else "GH_ENTERPRISE_TOKEN"
    )


def assert_call_safe(
    call: Call,
    *,
    host: str,
    mode: Mode = "token",
    untrusted: Iterable[str] = (),
    ambient: Mapping[str, str] | None = None,
) -> None:
    """Assert every safety rule on one logged call.

    ``untrusted`` are strings the test planted in forge-controlled input; none may reach argv.
    ``ambient`` are token variables the test set in the caller's own environment and that must
    not reach the child: no child variable may hold any of their values.
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

    for name, value in (ambient or {}).items():
        leaked = [k for k, v in env.items() if v == value]
        assert not leaked, f"{where}: ambient value of {name} reached {leaked}"

    for name in NEVER_PASSED:
        assert name not in env, f"{where}: {name} must never pass through"
    for name, value in FIXED_SETTINGS.items():
        assert env.get(name) == value, f"{where}: {name}={env.get(name)!r}, want {value!r}"

    allowed = set(BASE_ALLOWED) | set(present)
    if mode == "stored":
        allowed |= STORED_ONLY
    else:
        allowed |= {"GH_CONFIG_DIR" if call.backend == "gh" else "GLAB_CONFIG_DIR"}
    if call.backend == "glab" and host != "gitlab.com":
        allowed.add("GITLAB_HOST")
    stray = sorted(set(env) - allowed)
    assert not stray, f"{where}: environment not built from scratch, stray {stray}"
    if mode == "token":
        config_var = "GH_CONFIG_DIR" if call.backend == "gh" else "GLAB_CONFIG_DIR"
        assert env.get(config_var), f"{where}: token mode needs a private {config_var}"

    for entry in env.get("PATH", "").split(os.pathsep):
        assert os.path.isabs(entry), f"{where}: PATH entry {entry!r} is not absolute"

    assert call.cwd_entries == [], f"{where}: cwd not empty: {call.cwd_entries}"
    assert call.cwd_mode == 0o700, f"{where}: cwd mode {oct(call.cwd_mode or 0)}, want 0o700"
    tmpdir = os.path.realpath(env.get("TMPDIR", ""))
    assert tmpdir and call.cwd_real.startswith(tmpdir + os.sep), (
        f"{where}: cwd {call.cwd_real} is not under TMPDIR {tmpdir}"
    )
    assert env.get("GIT_CEILING_DIRECTORIES") == os.path.dirname(call.cwd), (
        f"{where}: GIT_CEILING_DIRECTORIES {env.get('GIT_CEILING_DIRECTORIES')!r} is not the "
        f"parent of cwd {call.cwd}"
    )
    assert call.repo_context is None, f"{where}: a repository was discoverable: {call.repo_context}"

    assert_no_untrusted_in_argv(call, untrusted)
    assert not call.placeholders, f"{where}: endpoint used a CLI placeholder"
    if call.backend == "gh" and call.argv[:1] == ["api"]:
        assert "--hostname" in call.argv, f"{where}: gh needs --hostname on every call"
    if call.request is not None:
        assert not call.request["full_url_endpoint"], f"{where}: full-URL endpoint"
        assert not call.request["file_fields"], f"{where}: -F read a file"


def assert_no_untrusted_in_argv(call: Call, untrusted: Iterable[str]) -> None:
    for planted in untrusted:
        assert planted, "an empty string proves nothing"
        hits = [a for a in call.argv if planted in a]
        assert not hits, f"{call.backend}: untrusted text {planted!r} in argv: {hits}"


def reference_env(
    backend: str,
    host: str,
    *,
    root: Path,
    path: str,
    token: str | None = "tok-fixture-0001",
    mode: Mode = "token",
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
        env["GITLAB_HOST"] = host
    if mode == "token":
        assert token
        env[token_variable(backend, host)] = token
        config = private / "config"
        config.mkdir(mode=0o700)
        env["GH_CONFIG_DIR" if backend == "gh" else "GLAB_CONFIG_DIR"] = str(config)
    return env, cwd
