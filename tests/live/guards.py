"""Hard safety guards of the live harness. Pure code: no host call, no file outside private dirs.

Every rule here is a refusal (``LiveRefused``) or, for missing settings, a skip reason. None has
an override: the host allowlist is a constant, the name prefix is a constant, and the expected
account has no default.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tests.harness.fixtures import in_ci
from tests.harness.safety import AMBIENT_TOKEN_VARS, FIXED_SETTINGS, NO_BUS_ADDRESS

ALLOWED_HOSTS = frozenset({"github.com", "gitlab.com"})
BACKEND_HOSTS = {"github": "github.com", "gitlab": "gitlab.com"}
NAME_PREFIX = "forgectl-live-"
NAME_PATTERN = re.compile(r"forgectl-live-[0-9]{8}t[0-9]{6}z-[0-9a-f]{8}")
# Variables that must never reach a live child, on top of the ambient tokens the process tests
# already plant. The child environment is built from scratch, so none is copied; the final check
# still names them.
EXTRA_FORBIDDEN = (
    "GITHUB_ACCESS_TOKEN",
    "GH_HOST",
    "GH_REPO",
    "GITLAB_HOST",
    "GL_HOST",
    "GIT_ASKPASS",
    "SSH_AUTH_SOCK",
)
FORBIDDEN_VARS = (*AMBIENT_TOKEN_VARS, *EXTRA_FORBIDDEN)
TOKEN_VARS = frozenset(
    {"GH_TOKEN", "GITLAB_TOKEN"}
    | {f"FORGECTL_LIVE_{b}_{k}TOKEN" for b in ("GITHUB", "GITLAB") for k in ("", "SECOND_")}
)
MIN_SUBSTRING = 8
PROXY_VARS = frozenset({"http_proxy", "https_proxy", "all_proxy"})
PASSTHROUGH = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "GLAB_CA_CERT",
)


class LiveRefused(AssertionError):
    """A safety guard refused. Never a skip: the run must stop and be looked at."""


@dataclass(frozen=True)
class LiveSettings:
    """The names of the dedicated settings for one backend (documented in docs/live-tests.md)."""

    backend: str
    host: str
    token: str
    user: str
    second_token: str
    second_user: str

    @classmethod
    def for_backend(cls, backend: str) -> LiveSettings:
        if backend not in BACKEND_HOSTS:
            raise LiveRefused(f"unknown backend {backend!r}")
        upper = backend.upper()
        return cls(
            backend=backend,
            host=BACKEND_HOSTS[backend],
            token=f"FORGECTL_LIVE_{upper}_TOKEN",
            user=f"FORGECTL_LIVE_{upper}_USER",
            second_token=f"FORGECTL_LIVE_{upper}_SECOND_TOKEN",
            second_user=f"FORGECTL_LIVE_{upper}_SECOND_USER",
        )


@dataclass(frozen=True)
class LiveConfig:
    backend: str
    host: str
    token: str
    login: str  # the login the user declared; the resolved account must match it
    second_token: str | None = None
    second_login: str | None = None

    def secrets(self) -> list[str]:
        return [s for s in (self.token, self.second_token) if s]

    def __repr__(self) -> str:  # a config must never print its tokens
        return f"LiveConfig(backend={self.backend!r}, host={self.host!r}, token=<hidden>)"


def require_host(host: str) -> str:
    """The allowlist: exactly github.com or gitlab.com. No normalization, no override."""
    if host not in ALLOWED_HOSTS:
        raise LiveRefused(f"live tests may only target github.com and gitlab.com, not {host!r}")
    return host


def require_throwaway_name(name: str) -> str:
    if not NAME_PATTERN.fullmatch(name):
        raise LiveRefused(f"{name!r} is not a throwaway name ({NAME_PREFIX}<run id>)")
    return name


def new_run_id(now: datetime | None = None, random_hex: str | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dt%H%M%Sz")
    return f"{stamp}-{random_hex or secrets.token_hex(4)}"


def throwaway_name(run_id: str) -> str:
    return require_throwaway_name(NAME_PREFIX + run_id)


def same_login(a: str, b: str) -> bool:
    """ASCII-only, case-insensitive: ``casefold`` would equate "SS" with a non-ASCII sharp s."""
    return bool(a) and a.isascii() and b.isascii() and a.lower() == b.lower()


def require_account(expected: str, resolved: str) -> None:
    """The token must belong to the account the user declared (case-insensitive logins)."""
    if not expected:
        raise LiveRefused("no expected login is configured")
    if not same_login(resolved, expected):
        raise LiveRefused("the token's account is not the declared live-test account")


def require_owned_by(owner: str, login: str) -> None:
    """A created resource must live in the declared account's own namespace."""
    if not same_login(owner, login):
        raise LiveRefused("a throwaway resource is not in the declared account's namespace")


def _clean(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None


def load_config(
    backend: str, environ: Mapping[str, str], *, run_live: bool = True
) -> tuple[LiveConfig | None, str | None]:
    """The live config for one backend, or a skip reason (never a value) when anything is absent.

    A token or login with an inner space or control character is a refusal, not a skip: the
    setting exists but is wrong.
    """
    names = LiveSettings.for_backend(backend)
    if in_ci(environ):
        return None, "live tests never run in CI"
    if not run_live:
        return None, "live test: pass --run-live to run it"
    token, login = _clean(environ.get(names.token)), _clean(environ.get(names.user))
    if not token or not login:
        return None, f"set {names.token} and {names.user} to run the {backend} live tests"
    second_token = _clean(environ.get(names.second_token))
    second_login = _clean(environ.get(names.second_user))
    for label, value in (
        ("token", token),
        ("login", login),
        ("second token", second_token),
        ("second login", second_login),
    ):
        if value is not None and not re.fullmatch(r"[\x21-\x7e]+", value):
            raise LiveRefused(f"the {backend} {label} setting is not one line of printable ASCII")
    if bool(second_token) != bool(second_login):
        second_token = second_login = None  # half a second account is no second account
    return LiveConfig(backend, names.host, token, login, second_token, second_login), None


@dataclass(frozen=True)
class PrivateDirs:
    root: Path
    home: Path
    xdg_config: Path
    gh_config: Path
    glab_config: Path
    tmp: Path
    cwd: Path


def make_private_dirs(root: Path) -> PrivateDirs:
    """Dedicated, empty, owner-only directories for one live session."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    dirs = {}
    for label in ("home", "xdg", "gh", "glab", "tmp", "cwd"):
        path = root / label
        path.mkdir(mode=0o700)
        path.chmod(0o700)
        dirs[label] = path
    return PrivateDirs(
        root, dirs["home"], dirs["xdg"], dirs["gh"], dirs["glab"], dirs["tmp"], dirs["cwd"]
    )


def child_env(
    cfg: LiveConfig,
    dirs: PrivateDirs,
    source: Mapping[str, str],
    *,
    use_token: bool = True,
    second: bool = False,
) -> dict[str, str]:
    """The whole environment of a ``gh``, ``glab`` or ``forgectl`` child, built from scratch.

    Only ``PATH`` (absolute entries), the locale and the proxy and certificate variables are
    taken from ``source``. HOME and every XDG directory are private; ``GH_CONFIG_DIR`` and
    ``GLAB_CONFIG_DIR`` are dedicated empty directories; no ambient token variable is copied. The
    one credential is ``FORGECTL_LIVE_<BACKEND>_TOKEN`` for forgectl (named by the config
    written with ``forgectl_config_text``) or the backend's own token variable for ``gh`` and
    ``glab``. ``use_token`` is false for a child that needs none.
    """
    require_host(cfg.host)
    token = cfg.second_token if second else cfg.token
    if not token:
        raise LiveRefused("no second account is configured")
    env = {
        "PATH": os.pathsep.join(
            e for e in source.get("PATH", "").split(os.pathsep) if e and os.path.isabs(e)
        ),
        "HOME": str(dirs.home),
        "TMPDIR": str(dirs.tmp),
        "XDG_CONFIG_HOME": str(dirs.xdg_config),
        "XDG_CACHE_HOME": str(dirs.home / ".cache"),
        "XDG_DATA_HOME": str(dirs.home / ".local" / "share"),
        "XDG_STATE_HOME": str(dirs.home / ".local" / "state"),
        "GH_CONFIG_DIR": str(dirs.gh_config),
        "GLAB_CONFIG_DIR": str(dirs.glab_config),
        "DBUS_SESSION_BUS_ADDRESS": NO_BUS_ADDRESS,
        "GIT_CEILING_DIRECTORIES": str(dirs.cwd.parent),
        **FIXED_SETTINGS,
    }
    for name, value in source.items():
        if name.lower() in PROXY_VARS and re.search(r"^(?:[^/?#]*://)?[^/?#]*@", value):
            raise LiveRefused(f"{name} carries credentials (user:password@): not passed on")
    for name in PASSTHROUGH:
        if name in source:
            env[name] = source[name]
    if use_token:
        env[token_variable(cfg.backend)] = token
    return env


def token_variable(backend: str) -> str:
    """The only variable a live child gets its token in. ``gh`` and ``glab`` read their own."""
    return {"github": "GH_TOKEN", "gitlab": "GITLAB_TOKEN"}[backend]


def forgectl_child_env(
    cfg: LiveConfig, dirs: PrivateDirs, source: Mapping[str, str], *, second: bool = False
) -> dict[str, str]:
    """``child_env`` for forgectl itself: the token is in ``FORGECTL_LIVE_<BACKEND>_TOKEN`` only,
    which the written config names as ``token_env``. forgectl then hands it to ``gh``/``glab``."""
    env = child_env(cfg, dirs, source, use_token=False, second=second)
    env[LiveSettings.for_backend(cfg.backend).token] = (
        cfg.second_token if second and cfg.second_token else cfg.token
    )
    return env


def forgectl_config_text(backend: str) -> str:
    """The config.yaml a live forgectl run reads (from the private XDG dir): one built-in host,
    its credential read from the dedicated variable, no hooks and no hidden repositories."""
    host = BACKEND_HOSTS[backend]
    var = LiveSettings.for_backend(backend).token
    return f"hosts:\n  {host}:\n    token_env: {var}\n"


def write_forgectl_config(dirs: PrivateDirs, backend: str) -> Path:
    path = dirs.xdg_config / "forgectl" / "config.yaml"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(forgectl_config_text(backend), encoding="utf-8")
    return path


def assert_env_safe(env: Mapping[str, str], dirs: PrivateDirs, source: Mapping[str, str]) -> None:
    """The last check before any launch. Raises ``LiveRefused``; the checks need no host."""
    allowed_token_vars = {"GH_TOKEN", "GITLAB_TOKEN"}
    present = [n for n in FORBIDDEN_VARS if n in env and n not in allowed_token_vars]
    if present:
        raise LiveRefused(f"forbidden variables in the child environment: {present}")
    if len([n for n in allowed_token_vars if n in env]) > 1:
        raise LiveRefused("more than one host token variable in the child environment")
    private = {
        "HOME": dirs.home,
        "XDG_CONFIG_HOME": dirs.xdg_config,
        "GH_CONFIG_DIR": dirs.gh_config,
        "GLAB_CONFIG_DIR": dirs.glab_config,
    }
    for name, path in private.items():
        if env.get(name) != str(path):
            raise LiveRefused(f"{name} is not the dedicated private directory")
    tokens = {v.strip() for n, v in env.items() if n in TOKEN_VARS and v.strip()}
    for name in FORBIDDEN_VARS:
        value = (source.get(name) or "").strip()
        if not value:
            continue
        # A short value (GH_HOST=1) is a substring of anything, so only a long one is matched
        # inside other values; a live token equal to any ambient value is refused at any length.
        if value in tokens or (
            len(value) >= MIN_SUBSTRING and any(value in v for v in env.values())
        ):
            raise LiveRefused(f"the ambient value of {name} reached the child environment")


def second_account_problem(backend: str, environ: Mapping[str, str]) -> str | None:
    """A skip reason when only one of the second account's two settings is set, else ``None``."""
    names = LiveSettings.for_backend(backend)
    token, user = _clean(environ.get(names.second_token)), _clean(environ.get(names.second_user))
    if bool(token) != bool(user):
        return (
            f"the second {backend} account is half-configured: "
            f"set both {names.second_token} and {names.second_user}"
        )
    return None


def check_forgectl_argv(argv: Sequence[str], cfg: LiveConfig, full_name: str) -> None:
    """Refuse a forgectl command that could reach anything but this run's throwaway.

    ``full_name`` is the throwaway's full path (``owner/name``). A URL argument must be https on
    ``cfg.host`` with a path that starts with it, segment by segment; a ``host/path#N`` reference
    and a ``--repo`` must name it; ``--host`` must be ``cfg.host``. Raises before any process.
    """
    require_host(cfg.host)
    own = [s for s in full_name.lower().split("/") if s]

    def under(path: str) -> bool:
        parts = path.lower().split("/")
        if parts[-1] == "":
            parts.pop()  # a trailing slash is accepted by forgectl
        if any(p in ("", ".", "..") for p in parts):
            return False
        return len(parts) >= len(own) and parts[: len(own)] == own

    def refuse(arg: str) -> LiveRefused:
        return LiveRefused(f"forgectl argument {arg!r} does not point at the throwaway")

    args = list(argv)
    for index, arg in enumerate(args):
        flag, eq, inline = arg.partition("=")
        if flag in ("--host", "--repo"):
            value = inline if eq else (args[index + 1] if index + 1 < len(args) else "")
            if flag == "--host" and value.lower() != cfg.host:
                raise refuse(arg)
            if flag == "--repo" and not under_repo(value, cfg.host, own):
                raise refuse(arg)
        elif "://" in arg:
            match = re.fullmatch(r"https://([a-z0-9.-]+)/([^?#@\\%\s]*)(?:[?#].*)?", arg.lower())
            if not match or match.group(1) != cfg.host or not under(match.group(2)):
                raise refuse(arg)
        elif "#" in arg and not arg.startswith("-"):
            if not under_repo(arg.split("#", 1)[0], cfg.host, own):
                raise refuse(arg)


def under_repo(value: str, host: str, own: list[str]) -> bool:
    """``host/<full path>`` exactly (a ``--repo``, or the part of ``host/path#N`` before ``#``)."""
    parts = value.lower().split("/")
    return parts[0] == host and parts[1:] == own
