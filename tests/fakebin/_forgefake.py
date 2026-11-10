"""Fake ``gh`` and ``glab`` for process tests (standard library only).

Modelled, from docs/design.md (Live verification) and docs/interface.md (Invoking the backends),
plus observations of gh 2.87.3 and glab 1.89.0 where the design has no row (version rule: the
newest verified behavior per program; a divergence is recorded, not silently resolved):

- ``gh api`` and ``glab api``: flag grammar (long, short, ``--flag=value``, clustered short flags,
  boolean values parsed like Go's ``strconv.ParseBool``, ``--``), usage errors (exit 1; ``gh``
  in cobra wording, ``glab`` boxed, capitalised, with a trailing period), host routing, which
  environment variable or stored login supplies the token, ``-i`` output (status line ending in
  LF, Go-canonical header names sorted, CRLF header lines), ``--silent``, ``--input -``, fields
  in the query string with ``-X GET`` or ``--input``, HTTP errors (exit 1, body on stdout,
  ``<prog>: <message> (HTTP N)`` or ``<prog>: HTTP N`` on stderr), 304 (exit 1), GraphQL
  ``errors`` with HTTP 200 (exit 1, full body on stdout), redirects (Go's net/http rules, as
  both programs use it: 301, 302 and 303 turn any method into a GET without a body and only the
  last answer is shown; 307 and 308 keep method and body, but a body from stdin cannot be resent,
  so the 3xx itself is shown and the program exits 1; the token rules of each program), no
  credential (``gh``: exit 4; ``glab``: anonymous, or, in the opt-in glab 1.89.0 mode, a boxed
  ``Unauthenticated.``, exit 1, nothing sent).
- ``gh auth token`` (returns an ambient token for any host, the gh 2.72.0 row; gh 2.87.3 is
  host-scoped) and ``glab auth token`` (no ``--hostname`` flag), ``--version``.
- ``gh`` token variables: a variable that does not apply to the host gives an anonymous request
  (gh 2.87.3); exit 4 only when no token variable is set at all and there is no stored login.
- ``gh api`` reads only file logins; ``gh auth token`` and ``glab`` also reach a keyring login
  when the session bus is reachable: ``DBUS_SESSION_BUS_ADDRESS`` or ``XDG_RUNTIME_DIR`` set, or
  neither set and the default per-user socket present (gh 2.87.3 dials ``/run/user/<uid>/bus``;
  the fake uses the per-test path in ``script.json``). ``NO_BUS_ADDRESS`` is never reachable.
- ``glab`` routing: ``GITLAB_HOST`` (alias ``GL_HOST``) keeps its path prefix, ``--hostname``
  drops it and wins, and an environment token is sent only to the host ``GITLAB_HOST`` names
  (default gitlab.com); GraphQL goes to ``<prefix>/api/graphql``.
- ``glab`` config: ``config.yml`` must be mode 0600 (else exit 2); on first run glab writes
  ``config.yml`` and ``aliases.yml`` (0600) into its config dir, and an unwritable dir exits 2.
- Signals: ``gh`` dies by SIGINT, SIGTERM or SIGHUP; ``glab`` exits 1 on SIGINT or SIGTERM with
  a message (glab 1.89.0) and dies by SIGHUP. Each is logged first.
- Every call is logged to ``calls.jsonl`` in the state directory: argv, the environment as
  received, cwd, cwd listing and mode, the config dir listing and mode, repository discovery,
  stdin, the requests that would be sent (one per redirect hop), which token each carried, the
  exit code and any signal.

Anything else fails loudly with exit 70 and ``forgefake: not modelled`` (logged as
``not_modelled``). Answers come from ``script.json`` in the state directory (see
``tests/harness/forges.py``).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import stat
import sys
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

NOT_MODELLED = 70
HANG_CAP = 120.0  # seconds; a scripted hang never outlives a forgotten test by more
MAX_REDIRECTS = 10
STATE_VAR = "FORGEFAKE_STATE"
NO_BUS_ADDRESS = "unix:path=/nonexistent/forgectl-no-bus"  # the gateway's unusable bus
REPO = Path(__file__).resolve().parents[2]

REASONS = {
    200: "OK",
    201: "Created",
    202: "Accepted",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    303: "See Other",
    304: "Not Modified",
    307: "Temporary Redirect",
    308: "Permanent Redirect",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    406: "Not Acceptable",
    409: "Conflict",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}
BOOL_TRUE = {"1", "t", "T", "TRUE", "true", "True"}
BOOL_FALSE = {"0", "f", "F", "FALSE", "false", "False"}

# name, short, takes a value
API_FLAGS: dict[str, list[tuple[str, str | None, bool]]] = {
    "gh": [
        ("field", "F", True),
        ("raw-field", "f", True),
        ("header", "H", True),
        ("hostname", None, True),
        ("include", "i", False),
        ("input", None, True),
        ("method", "X", True),
        ("silent", None, False),
        ("help", "h", False),
        # accepted by the real program, not modelled here
        ("cache", None, True),
        ("jq", "q", True),
        ("paginate", None, False),
        ("preview", "p", True),
        ("slurp", None, False),
        ("template", "t", True),
        ("verbose", None, False),
    ],
    "glab": [
        ("field", "F", True),
        ("raw-field", "f", True),
        ("header", "H", True),
        ("hostname", None, True),
        ("include", "i", False),
        ("input", None, True),
        ("method", "X", True),
        ("silent", None, False),
        ("help", "h", False),
        ("output", None, True),
        ("paginate", None, False),
    ],
}
UNMODELLED = {"cache", "jq", "paginate", "preview", "slurp", "template", "verbose", "output"}
AUTH_TOKEN_FLAGS: dict[str, list[tuple[str, str | None, bool]]] = {
    "gh": [("hostname", None, True), ("user", "u", True), ("help", "h", False)],
    "glab": [("help", "h", False)],
}
PLACEHOLDER = {
    "gh": re.compile(r"\{(owner|repo|branch)\}"),
    "glab": re.compile(r":(id|fullpath|group|namespace|repo|user|username|branch)(?![\w])"),
}
GH_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")
GH_ENTERPRISE_VARS = ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
GLAB_TOKEN_VARS = ("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN")
GLAB_SIGNAL_TEXT = {
    signal.SIGINT: "interrupt signal received.",
    signal.SIGTERM: "terminated signal received.",
}
GLAB_DEFAULT_CONFIG = (
    "git_protocol: ssh\n"
    "check_update: true\n"
    "host: gitlab.com\n"
    "no_prompt: false\n"
    "telemetry: true\n"
    "hosts:\n"
    "    gitlab.com:\n"
    "        api_protocol: https\n"
    "        api_host: gitlab.com\n"
    "        token:\n"
)
GLAB_DEFAULT_ALIASES = "ci: pipeline ci\nco: mr checkout\n"


class Exit(Exception):
    def __init__(self, code: int) -> None:
        self.code = code


def original_env() -> dict[str, str]:
    """The environment as exec'd, before the interpreter's own changes (PEP 538 adds LC_CTYPE)."""
    try:
        raw = Path("/proc/self/environ").read_bytes()
    except OSError:
        return dict(os.environ)
    out: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            key, _, value = item.partition(b"=")
            out[os.fsdecode(key)] = os.fsdecode(value)
    return out


def canonical_header(key: str) -> str:
    """Go's ``textproto.CanonicalMIMEHeaderKey``: unchanged if it holds a non-token byte."""
    token = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
    if not token.match(key):
        return key
    out: list[str] = []
    upper = True
    for ch in key:
        out.append(ch.upper() if upper else ch.lower())
        upper = ch == "-"
    return "".join(out)


def glab_box(message: str, *, hint: bool) -> str:
    """glab's boxed error: a blank-padded ERROR title, the message, an optional usage hint."""
    lines = [" " * 10, "   ERROR  ", " " * 10, ("  " + message).ljust(118), ""]
    if hint:
        lines += ["  Try --help for usage.", ""]
    return "\n".join(lines) + "\n"


def parse_simple_yaml(text: str) -> dict[tuple[str, ...], str]:
    """Scalar leaves of a block-mapping YAML file by key path, for any consistent indentation."""
    leaves: dict[tuple[str, ...], str] = {}
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        found = re.match(r"^( *)([^:\s][^:]*?):(?:\s+(.*))?$", line)
        if not found:
            continue
        indent = len(found.group(1))
        key = found.group(2).strip().strip("'\"")
        value = (found.group(3) or "").strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = (*(k for _, k in stack), key)
        if value:
            leaves[path] = value.strip("'\"")
        else:
            stack.append((indent, key))
    return leaves


class Fake:
    def __init__(self, backend: str, state_dir: Path, argv: list[str]) -> None:
        self.backend = backend
        self.dir = state_dir
        self.argv = argv
        self.env = original_env()
        self.call_id = f"{os.getpid()}-{time.time_ns()}"
        self.out = sys.stdout.buffer
        self.err = sys.stderr.buffer
        self.script = self._load("script.json", {})
        self.summary: dict[str, Any] = {}

    # logging -----------------------------------------------------------------------------

    def _load(self, name: str, default: Any) -> Any:
        try:
            return json.loads((self.dir / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default

    def log(self, phase: str, **fields: Any) -> None:
        record = {"id": self.call_id, "phase": phase, "backend": self.backend, **fields}
        line = (json.dumps(record, sort_keys=True) + "\n").encode()
        fd = os.open(self.dir / "calls.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)

    def say(self, text: str) -> None:
        self.err.write(text.encode())
        self.err.flush()

    def fail(self, code: int, message: str) -> Exit:
        self.say(message if message.endswith("\n") else message + "\n")
        return Exit(code)

    def usage_error(self, message: str) -> Exit:
        if self.backend == "glab":
            text = message[:1].upper() + message[1:]
            return self.fail(1, glab_box(text if text.endswith(".") else text + ".", hint=True))
        return self.fail(1, f"{message}\n{self.usage()}")

    def not_modelled(self, what: str) -> Exit:
        self.summary["not_modelled"] = True
        return self.fail(NOT_MODELLED, f"forgefake: not modelled: {what}")

    # environment pictures ----------------------------------------------------------------

    @staticmethod
    def _listing(path: str) -> tuple[list[str] | None, int | None, bool | None]:
        try:
            entries = sorted(os.listdir(path))
            info = os.stat(path)
        except OSError:
            return None, None, None
        return entries, stat.S_IMODE(info.st_mode), info.st_uid == os.getuid()

    def start_record(self) -> None:
        cwd = os.getcwd()
        entries, mode, mine = self._listing(".")
        config_var = "GH_CONFIG_DIR" if self.backend == "gh" else "GLAB_CONFIG_DIR"
        config = self.env.get(config_var)
        config_entries, config_mode, _ = self._listing(config) if config else (None, None, None)
        self.log(
            "start",
            argv=self.argv,
            env=self.env,
            cwd=cwd,
            cwd_real=os.path.realpath(cwd),
            cwd_entries=entries,
            cwd_mode=mode,
            cwd_owner_is_me=mine,
            config_dir=config,
            config_dir_real=os.path.realpath(config) if config else None,
            config_entries=config_entries,
            config_mode=config_mode,
            pid=os.getpid(),
            ppid=os.getppid(),
            pgid=os.getpgrp(),
            time=time.time(),
            repo_context=self.discover_repo(cwd),
        )

    def discover_repo(self, cwd: str) -> str | None:
        """Where git would find a repository from cwd, honoring GIT_CEILING_DIRECTORIES."""
        ceilings = [
            os.path.realpath(p)
            for p in self.env.get("GIT_CEILING_DIRECTORIES", "").split(os.pathsep)
            if p.startswith("/")
        ]
        here = os.path.realpath(cwd)
        while True:
            if os.path.exists(os.path.join(here, ".git")):
                return here
            parent = os.path.dirname(here)
            if parent == here or parent in ceilings:
                return None
            here = parent

    def config_dir(self) -> Path:
        var, sub = ("GH_CONFIG_DIR", "gh") if self.backend == "gh" else ("GLAB_CONFIG_DIR", None)
        if self.env.get(var):
            return Path(self.env[var])
        xdg = self.env.get("XDG_CONFIG_HOME")
        leaf = sub or "glab-cli"
        if xdg:
            return Path(xdg) / leaf
        return Path(self.env.get("HOME", "/nonexistent")) / ".config" / leaf

    def glab_config(self) -> None:
        """glab loads its config first: mode 0600 is enforced, and a first run writes defaults."""
        cfg = self.config_dir()
        path = cfg / "config.yml"
        prefix = "failed to read configuration:  "
        if path.exists():
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode != 0o600:
                raise self.fail(
                    2,
                    f"{prefix}{path} has the permissions {mode:o}, but glab requires 600.\n"
                    f"Consider running `chmod 600 {path}`",
                )
            return
        try:
            cfg.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as err:
            reason = (err.strerror or str(err)).lower()
            raise self.fail(
                2, f"{prefix}failed to create config directory: mkdir {cfg}: {reason}"
            ) from None
        if not os.access(cfg, os.W_OK):
            temp = f"{cfg}/.config.yml{time.time_ns() % 10**19}"
            raise self.fail(2, f"{prefix}open {temp}: permission denied")
        for name, text in (
            ("config.yml", GLAB_DEFAULT_CONFIG),
            ("aliases.yml", GLAB_DEFAULT_ALIASES),
        ):
            target = cfg / name
            if not target.exists():
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)

    def file_login(self, host: str) -> str | None:
        """Token for ``host`` from ``hosts.yml`` (gh) or ``config.yml`` (glab) in the config dir."""
        name = "hosts.yml" if self.backend == "gh" else "config.yml"
        try:
            leaves = parse_simple_yaml((self.config_dir() / name).read_text(encoding="utf-8"))
        except OSError:
            return None
        key = (host, "oauth_token") if self.backend == "gh" else ("hosts", host, "token")
        return leaves.get(key) or None

    def bus_reachable(self) -> bool:
        address = self.env.get("DBUS_SESSION_BUS_ADDRESS")
        if address == NO_BUS_ADDRESS:
            return False
        if address or self.env.get("XDG_RUNTIME_DIR"):
            return True
        default = self.script.get("default_bus_socket") or f"/run/user/{os.getuid()}/bus"
        return os.path.exists(default)

    def keyring_login(self, host: str) -> str | None:
        """A login kept in the OS keyring: reachable only with a session bus variable."""
        if not self.bus_reachable():
            return None
        value = self.script.get("keyring", {}).get(f"{self.backend}:{host}")
        return value if isinstance(value, str) else None

    def stored_token(self, host: str) -> tuple[str | None, str | None]:
        for source, token in (
            ("file", self.file_login(host)),
            ("keyring", self.keyring_login(host)),
        ):
            if token:
                return source, token
        return None, None

    # parsing -----------------------------------------------------------------------------

    def parse(
        self, args: list[str], flags: list[tuple[str, str | None, bool]]
    ) -> tuple[dict[str, list[str]], list[str]]:
        """pflag-like parse. Returns (flag values by long name, positionals).

        A boolean flag's values are "true" or "false"; use ``on`` to read one.
        """
        longs = {n: (s, v) for n, s, v in flags}
        shorts = {s: (n, v) for n, s, v in flags if s}
        values: dict[str, list[str]] = {}
        positional: list[str] = []
        i = 0
        while i < len(args):
            arg = args[i]
            i += 1
            if arg == "--":
                positional.extend(args[i:])
                break
            if arg.startswith("--"):
                name, eq, inline = arg[2:].partition("=")
                if name not in longs:
                    raise self.usage_error(f"unknown flag: --{name}")
                short, takes = longs[name]
                if takes and not eq:
                    if i >= len(args):
                        raise self.usage_error(f"flag needs an argument: --{name}")
                    inline, i = args[i], i + 1
                elif not takes:
                    inline = self._bool(name, short, inline if eq else "true")
                values.setdefault(name, []).append(inline)
            elif arg.startswith("-") and arg != "-":
                cluster = arg[1:]
                while cluster:
                    ch, cluster = cluster[0], cluster[1:]
                    if ch not in shorts:
                        raise self.usage_error(f"unknown shorthand flag: '{ch}' in {arg}")
                    name, takes = shorts[ch]
                    if takes:
                        if not cluster:
                            if i >= len(args):
                                raise self.usage_error(f"flag needs an argument: '{ch}' in -{ch}")
                            cluster, i = args[i], i + 1
                        values.setdefault(name, []).append(cluster.removeprefix("="))
                        break
                    if cluster.startswith("="):
                        values.setdefault(name, []).append(self._bool(name, ch, cluster[1:]))
                        break
                    values.setdefault(name, []).append("true")
            else:
                positional.append(arg)
        return values, positional

    def _bool(self, name: str, short: str | None, text: str) -> str:
        if text in BOOL_TRUE:
            return "true"
        if text in BOOL_FALSE:
            return "false"
        spelled = f"-{short}, --{name}" if short else f"--{name}"
        raise self.usage_error(
            f'invalid argument "{text}" for "{spelled}" flag: '
            f'strconv.ParseBool: parsing "{text}": invalid syntax'
        )

    @staticmethod
    def on(values: dict[str, list[str]], name: str) -> bool:
        return values.get(name, ["false"])[-1] == "true"

    def usage(self) -> str:
        return f"Usage:  {self.backend} api <endpoint> [flags]\n"

    # commands ----------------------------------------------------------------------------

    def run(self) -> int:
        self.start_record()
        try:
            if self.backend == "glab":
                self.glab_config()
            self.dispatch()
            code = 0
        except Exit as stop:
            code = stop.code
        self.out.flush()
        self.log("end", exit=code, **self.summary)
        return code

    def dispatch(self) -> None:
        args = self.argv
        if args[:1] in (["--version"], ["version"]):
            if self.backend == "gh":
                self.out.write(
                    b"gh version 2.72.0\nhttps://github.com/cli/cli/releases/tag/v2.72.0\n"
                )
            else:
                self.out.write(b"glab 1.127.0\n")
            return
        if args[:1] == ["api"]:
            return self.api(args[1:])
        if args[:2] == ["auth", "token"]:
            return self.auth_token(args[2:])
        what = " ".join(args[:2]) or "(no command)"
        raise self.not_modelled(f"`{self.backend} {what}`; only `api`, `auth token` and --version")

    def auth_token(self, args: list[str]) -> None:
        values, extra = self.parse(args, AUTH_TOKEN_FLAGS[self.backend])
        if extra:
            raise self.fail(1, f'unknown command "{extra[0]}"')
        if self.backend == "gh":
            host = (values.get("hostname") or [self.env.get("GH_HOST", "github.com")])[-1]
            # design.md (gh 2.72.0): any ambient variable, for any host.
            token = next(
                (self.env[v] for v in (*GH_TOKEN_VARS, *GH_ENTERPRISE_VARS) if self.env.get(v)),
                None,
            )
        else:
            host = self.glab_env_host().split("/")[0]
            token = next((self.env[v] for v in GLAB_TOKEN_VARS if self.env.get(v)), None)
        token = token or self.file_login(host) or self.keyring_login(host)
        if token is None:
            raise self.fail(1, f"no oauth token found for {host}")
        self.out.write(token.encode() + b"\n")

    def api(self, args: list[str]) -> None:
        values, positional = self.parse(args, API_FLAGS[self.backend])
        if self.on(values, "help"):
            self.out.write(self.usage().encode())
            return
        if len(positional) != 1:
            raise self.usage_error(f"accepts 1 arg(s), received {len(positional)}")
        for name in sorted(UNMODELLED & values.keys()):
            raise self.not_modelled(f"--{name}")
        endpoint = positional[0]
        if PLACEHOLDER[self.backend].search(endpoint):
            self.summary["placeholders"] = True
            raise self.not_modelled("placeholder expansion in an endpoint")
        fields = values.get("field", []) + values.get("raw-field", [])
        has_input = "input" in values
        explicit = (values.get("method") or [None])[-1]
        method = (explicit or ("POST" if fields or has_input else "GET")).upper()
        stdin_text: str | None = None
        if values.get("input") == ["-"]:
            stdin_text = sys.stdin.read()
            self.log("stdin", stdin=stdin_text)
        elif has_input:
            raise self.not_modelled("--input FILE (forgectl sends bodies on stdin)")
        file_fields = [f for f in fields if "=" in f and f.split("=", 1)[1].startswith("@")]
        request = self.route(values, endpoint, method)
        query = [f.split("=", 1) if "=" in f else [f, ""] for f in fields]
        if fields and (method == "GET" or has_input):
            # gh and glab put fields in the query string with -X GET or with --input, encoded
            # as Go's url.Values does: sorted by key.
            request["url"] += ("&" if "?" in request["url"] else "?") + urllib.parse.urlencode(
                sorted((k, v) for k, v in query)
            )
            request["query"] = query
        request["file_fields"] = file_fields
        request["headers_sent"] = values.get("header", [])
        request["fields"] = fields
        request["include"] = self.on(values, "include")
        request["full_url_endpoint"] = bool(re.match(r"https?://", endpoint, re.I))
        request["body"] = stdin_text
        self.summary["request"] = request
        self.answer(values, request, stdin_text)

    def route(self, values: dict[str, list[str]], endpoint: str, method: str) -> dict[str, Any]:
        if self.backend == "gh":
            return self.route_gh(values, endpoint, method)
        return self.route_glab(values, endpoint, method)

    def route_gh(self, values: dict[str, list[str]], endpoint: str, method: str) -> dict[str, Any]:
        host = (values.get("hostname") or [self.env.get("GH_HOST") or "github.com"])[-1]
        if not host.strip():
            raise self.usage_error("error parsing `--hostname`: a value for hostname is required")
        if ":" in host or "/" in host:
            raise self.usage_error("error parsing `--hostname`: invalid hostname")
        names = (
            GH_TOKEN_VARS
            if host == "github.com" or host.endswith(".ghe.com")
            else GH_ENTERPRISE_VARS
        )
        source, token = None, None
        for name in names:
            if self.env.get(name):
                source, token = "env:" + name, self.env[name]
                break
        if token is None:
            # gh api reads only file logins: with an empty config dir it refuses (design.md).
            stored = self.file_login(host)
            source, token = ("file", stored) if stored else (None, None)
        any_var = any(self.env.get(v) for v in (*GH_TOKEN_VARS, *GH_ENTERPRISE_VARS))
        if token is None and not any_var:
            raise self.fail(
                4,
                "To get started with GitHub CLI, please run:  gh auth login\n"
                "Alternatively, populate the GH_TOKEN environment variable with a GitHub API "
                "authentication token.",
            )
        # gh 2.87.3: a set variable that does not apply to this host gives an anonymous request.
        path = endpoint.lstrip("/")
        if host == "github.com":
            url = f"https://api.github.com/{path}"
        elif host.endswith(".ghe.com"):
            url = f"https://api.{host}/{path}"
        elif path == "graphql":
            url = f"https://{host}/api/graphql"
        else:
            url = f"https://{host}/api/v3/{path}"
        return {
            "host": host,
            "method": method,
            "url": url,
            "endpoint": path,
            "token_sent": token,
            "token_source": source,
            "token_header": "Authorization" if token else None,
        }

    def glab_env_host(self) -> str:
        return self.env.get("GITLAB_HOST") or self.env.get("GL_HOST") or "gitlab.com"

    def route_glab(
        self, values: dict[str, list[str]], endpoint: str, method: str
    ) -> dict[str, Any]:
        flag = (values.get("hostname") or [None])[-1]
        scheme = "https"
        spec = self.glab_env_host()
        if "://" in spec:
            scheme, _, spec = spec.partition("://")
        env_netloc, _, env_prefix = spec.partition("/")
        env_prefix = "/" + env_prefix.strip("/") if env_prefix.strip("/") else ""
        warnings: list[str] = []
        if flag is not None:
            if ":" in flag or "/" in flag:
                raise self.fail(1, f"invalid hostname {flag!r}: --hostname takes a bare host")
            netloc, prefix = flag, ""  # --hostname wins and drops GITLAB_HOST's prefix
        else:
            netloc, prefix = env_netloc, env_prefix
        host = netloc.split(":")[0]
        source, token = None, None
        env_token = next((v for v in GLAB_TOKEN_VARS if self.env.get(v)), None)
        # design.md (glab 1.127.0): an environment token goes only to GITLAB_HOST's host.
        if env_token and host == env_netloc.split(":")[0]:
            source, token = "env:" + env_token, self.env[env_token]
        elif env_token:
            warnings.append(f"credential in {env_token} was not sent to {host}")
            self.say(f"warning: {warnings[-1]}\n")
        if token is None:
            source, token = self.stored_token(host)
        if token is None and self.script.get("glab_1_89_no_credential"):
            raise self.fail(1, glab_box("Unauthenticated.", hint=False))
        path = endpoint.lstrip("/")
        full = bool(re.match(r"https?://", endpoint, re.I))
        if full:
            url = endpoint  # glab sends its token to whatever host a full-URL endpoint names
        elif path == "graphql":
            url = f"{scheme}://{netloc}{prefix}/api/graphql"
        else:
            url = f"{scheme}://{netloc}{prefix}/api/v4/{path}"
        return {
            "host": host,
            "method": method,
            "url": url,
            "endpoint": path,
            "token_sent": token,
            "token_source": source,
            "token_header": "Private-Token" if token else None,  # OAUTH_TOKEN too
            "warnings": warnings,
        }

    # answers -----------------------------------------------------------------------------

    def pick_rule(self, request: dict[str, Any], stdin_text: str | None) -> dict[str, Any] | None:
        with open(self.dir / "state.lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self._load("state.json", {"uses": {}, "matches": {}})
            chosen = None
            for index, rule in enumerate(self.script.get("rules", [])):
                if not self.matches(rule.get("match", {}), request, stdin_text):
                    continue
                if rule.get("backend", self.backend) != self.backend:
                    continue
                key = str(index)
                seen = state["matches"].get(key, 0) + 1
                state["matches"][key] = seen
                nth = rule.get("match", {}).get("nth")
                if nth is not None and seen != nth:
                    continue
                used = state["uses"].get(key, 0)
                if rule.get("times") is not None and used >= rule["times"]:
                    continue
                state["uses"][key] = used + 1
                chosen = dict(rule, index=index)
                break
            (self.dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        return chosen

    @staticmethod
    def matches(match: dict[str, Any], request: dict[str, Any], stdin_text: str | None) -> bool:
        if "endpoint" in match and match["endpoint"] != request["endpoint"]:
            return False
        if "endpoint_re" in match and not re.search(match["endpoint_re"], request["endpoint"]):
            return False
        if "method" in match and match["method"].upper() != request["method"].upper():
            return False
        if "host" in match and match["host"] != request["host"]:
            return False
        if "stdin_re" in match and not re.search(match["stdin_re"], stdin_text or ""):
            return False
        return True

    def choose(self, request: dict[str, Any], body: str | None) -> dict[str, Any]:
        rule = self.pick_rule(request, body)
        if rule is not None:
            self.summary.setdefault("rules", []).append(rule["index"])
            self.summary.setdefault("rule", rule["index"])
            return rule
        if self.script.get("default"):
            self.summary["defaulted"] = True
            return self.script["default"]
        self.summary["unmatched"] = True
        return {"status": 404, "body": {"message": "Not Found", "forgefake": "no scripted answer"}}

    def follow(self, request: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
        """The next request of a redirect, with Go's net/http method, body and header rules."""
        status = rule.get("status", 302)
        location = urllib.parse.urljoin(request["url"], rule["redirect"]["location"])
        parts = urllib.parse.urlsplit(location)
        method, body = request["method"], request.get("body")
        if status in (301, 302, 303):  # Go: any method but GET and HEAD becomes GET, no body
            if method not in ("GET", "HEAD"):
                method = "GET"
            body = None
        initial = urllib.parse.urlsplit(self.summary["request"]["url"]).netloc
        token = request["token_sent"]
        if self.backend == "gh" and parts.netloc != initial:
            token = None  # Go drops Authorization on another host; an explicit :443 differs
        return {
            "host": parts.hostname or "",
            "method": method,
            "url": location,
            "endpoint": parts.path.lstrip("/"),
            "token_sent": token,
            "token_header": request["token_header"] if token else None,
            "body": body,
            "status_followed": status,
        }

    def answer(
        self, values: dict[str, list[str]], request: dict[str, Any], stdin_text: str | None
    ) -> None:
        rule = self.choose(request, stdin_text)
        current = request
        hops: list[dict[str, Any]] = []
        shown_location: str | None = None
        while rule.get("redirect"):
            if rule.get("status", 302) in (307, 308) and current.get("body") is not None:
                # Go cannot resend a body read from stdin: the 3xx itself is the answer.
                shown_location = rule["redirect"]["location"]
                break
            if len(hops) >= MAX_REDIRECTS:
                raise self.fail(1, f"{self.backend}: stopped after {MAX_REDIRECTS} redirects")
            current = self.follow(current, rule)
            hops.append(current)
            request["hops"] = hops
            rule = self.choose(current, current["body"])
        if rule.get("delay"):
            time.sleep(rule["delay"])
        if rule.get("hang"):
            self.out.flush()
            hang = rule["hang"]
            cap = HANG_CAP if hang is True else min(float(hang), HANG_CAP)
            deadline = time.monotonic() + cap
            while time.monotonic() < deadline:
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            self.summary["hang_capped"] = True
            raise self.fail(NOT_MODELLED, f"forgefake: hang capped after {cap:g} s")
        status = rule.get("status", 200)
        if "body_text" in rule:
            body = (rule["body_text"] * rule.get("body_repeat", 1)).encode()
        elif rule.get("body") is not None:
            body = json.dumps(rule["body"], separators=(",", ":")).encode()
        else:
            body = b""
        if status in (204, 304):
            body = b""
        scripted = {"Content-Type": "application/json; charset=utf-8", **rule.get("headers", {})}
        if shown_location is not None:
            scripted["Location"] = shown_location
        headers = {canonical_header(k): v for k, v in scripted.items()}
        if self.on(values, "include"):
            head = f"HTTP/2.0 {status} {REASONS.get(status, 'Status')}\n"
            head += "".join(f"{k}: {v}\r\n" for k, v in sorted(headers.items())) + "\r\n"
            self.out.write(head.encode())
        if not self.on(values, "silent"):
            self.out.write(body)
        self.out.flush()
        if rule.get("stderr"):
            self.say(rule["stderr"])
        if "exit" in rule:
            raise Exit(rule["exit"])
        if status >= 300:  # 304, a 3xx not followed, and every error
            message = self.error_message(body)
            text = f"{message} (HTTP {status})" if message else f"HTTP {status}"
            self.say(f"{self.backend}: {text}\n")
            raise Exit(1)
        if current["endpoint"] == "graphql" or current["url"].endswith("/graphql"):
            try:
                errors = json.loads(body).get("errors")
            except (ValueError, AttributeError):
                errors = None
            if errors:
                text = "; ".join(
                    str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors
                )
                self.say(f"{self.backend}: {text}\n")
                raise Exit(1)
        if rule.get("die"):
            os.kill(os.getpid(), getattr(signal, rule["die"]))

    @staticmethod
    def error_message(body: bytes) -> str | None:
        """The ``message`` string of a JSON body; gh and glab ignore an ``error`` key."""
        try:
            doc = json.loads(body)
        except ValueError:
            return None
        if isinstance(doc, dict) and isinstance(doc.get("message"), str):
            return doc["message"]
        return None


def state_from_env() -> str:
    """For the developer launchers: the state dir must be named and outside the repository."""
    state = os.environ.get(STATE_VAR, "")
    if not state or not os.path.isabs(state) or not os.path.isdir(state):
        sys.stderr.write(f"forgefake: set {STATE_VAR} to an existing absolute directory\n")
        raise SystemExit(64)
    real, repo = os.path.realpath(state), os.path.realpath(REPO)
    if real == repo or real.startswith(repo + os.sep):
        sys.stderr.write(f"forgefake: {STATE_VAR} must be outside the repository ({repo})\n")
        raise SystemExit(64)
    return state


def main(backend: str, state_dir: str, argv: list[str] | None = None) -> None:
    fake = Fake(backend, Path(state_dir), list(sys.argv[1:] if argv is None else argv))

    def die(signum: int, _frame: Any) -> None:
        fake.log("signal", signal=signal.Signals(signum).name)
        if backend == "glab" and signum in GLAB_SIGNAL_TEXT:
            # glab 1.89.0 handles these: a message and exit 1.
            fake.say(GLAB_SIGNAL_TEXT[signal.Signals(signum)] + "\n")
            fake.log("end", exit=1, **fake.summary)
            sys.stdout.flush()
            os._exit(1)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    handler: Callable[[int, Any], None] = die
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, handler)
    code = fake.run()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--dev"]:
        main(sys.argv[2], state_from_env(), sys.argv[3:])
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
