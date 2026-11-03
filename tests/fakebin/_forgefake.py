"""Fake ``gh`` and ``glab`` for process tests (standard library only).

Modelled, from docs/design.md (Live verification) and docs/interface.md (Invoking the backends):

- ``gh api`` and ``glab api``: flag grammar (long, short, ``--flag=value``, clustered short flags,
  ``--``), cobra-style usage errors (exit 1), host routing, which environment variable or stored
  login supplies the token, ``-i`` output, ``--input -``, HTTP errors (exit 1, body on stdout,
  message on stderr), GraphQL ``errors`` with HTTP 200 (``gh``: exit 1, full body on stdout), no
  credential (``gh``: exit 4).
- ``gh auth token`` (returns an ambient token for any host) and ``glab auth token`` (no
  ``--hostname`` flag), ``--version``.
- ``glab`` routing: ``GITLAB_HOST`` keeps its path prefix, ``--hostname`` drops it and wins, and
  an environment token is sent only to the host ``GITLAB_HOST`` names (default gitlab.com).
- Signals: SIGINT, SIGTERM and SIGHUP are logged, then the process dies by the signal, as a Go
  program without a handler does.
- Every call is logged to ``calls.jsonl`` in the state directory: argv, the environment as
  received, cwd, cwd listing and mode, repository discovery, stdin, the request that would be
  sent, which token was sent, the exit code and any signal.

Anything else fails loudly with exit 70 and ``forgefake: not modelled``. Answers come from
``script.json`` in the state directory (see ``tests/harness/forges.py``).
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
from collections.abc import Callable
from pathlib import Path
from typing import Any

NOT_MODELLED = 70

REASONS = {
    200: "OK",
    201: "Created",
    202: "Accepted",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    304: "Not Modified",
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

    def not_modelled(self, what: str) -> Exit:
        return self.fail(NOT_MODELLED, f"forgefake: not modelled: {what}")

    # environment pictures ----------------------------------------------------------------

    def start_record(self) -> None:
        cwd = os.getcwd()
        entries: list[str] | None
        try:
            entries = sorted(os.listdir("."))
            mode = stat.S_IMODE(os.stat(".").st_mode)
            owner = os.stat(".").st_uid
        except OSError:
            entries, mode, owner = None, None, None
        self.log(
            "start",
            argv=self.argv,
            env=self.env,
            cwd=cwd,
            cwd_real=os.path.realpath(cwd),
            cwd_entries=entries,
            cwd_mode=mode,
            cwd_owner_is_me=owner == os.getuid() if owner is not None else None,
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

    def file_login(self, host: str) -> str | None:
        """Token for ``host`` from ``hosts.yml`` (gh) or ``config.yml`` (glab) in the config dir."""
        name, key = (
            ("hosts.yml", "oauth_token") if self.backend == "gh" else ("config.yml", "token")
        )
        try:
            lines = (self.config_dir() / name).read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        current: str | None = None
        for line in lines:
            if line and not line[0].isspace() and line.rstrip().endswith(":"):
                current = None if line.startswith("hosts:") else line.strip()[:-1]
            elif (
                line.startswith("  ") and not line.startswith("   ") and line.strip().endswith(":")
            ):
                current = line.strip()[:-1].strip("'\"")
            elif current == host and line.strip().startswith(key + ":"):
                return line.split(":", 1)[1].strip().strip("'\"")
        return None

    def stored_token(self, host: str) -> tuple[str | None, str | None]:
        for source, token in (
            ("file", self.file_login(host)),
            ("keyring", self.keyring_login(host)),
        ):
            if token:
                return source, token
        return None, None

    def keyring_login(self, host: str) -> str | None:
        """A login kept in the OS keyring: reachable only with a session bus variable."""
        if not self.env.get("DBUS_SESSION_BUS_ADDRESS"):
            return None
        value = self.script.get("keyring", {}).get(f"{self.backend}:{host}")
        return value if isinstance(value, str) else None

    # parsing -----------------------------------------------------------------------------

    def parse(
        self, args: list[str], flags: list[tuple[str, str | None, bool]]
    ) -> tuple[dict[str, list[str]], list[str]]:
        """pflag-like parse. Returns (flag values by long name, positionals)."""
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
                    raise self.fail(1, f"unknown flag: --{name}\n{self.usage()}")
                takes = longs[name][1]
                if takes and not eq:
                    if i >= len(args):
                        raise self.fail(1, f"flag needs an argument: --{name}\n{self.usage()}")
                    inline, i = args[i], i + 1
                elif not takes and eq and inline not in ("true", "false"):
                    raise self.fail(1, f'invalid argument "{inline}" for "--{name}" flag')
                values.setdefault(name, []).append(inline if takes else (inline or "true"))
            elif arg.startswith("-") and arg != "-":
                cluster = arg[1:]
                while cluster:
                    ch, cluster = cluster[0], cluster[1:]
                    if ch not in shorts:
                        raise self.fail(
                            1, f"unknown shorthand flag: '{ch}' in {arg}\n{self.usage()}"
                        )
                    name, takes = shorts[ch]
                    if takes:
                        if not cluster:
                            if i >= len(args):
                                raise self.fail(
                                    1, f"flag needs an argument: '{ch}' in -{ch}\n{self.usage()}"
                                )
                            cluster, i = args[i], i + 1
                        values.setdefault(name, []).append(cluster.removeprefix("="))
                        break
                    values.setdefault(name, []).append("true")
            else:
                positional.append(arg)
        return values, positional

    def usage(self) -> str:
        return f"Usage:  {self.backend} api <endpoint> [flags]\n"

    # commands ----------------------------------------------------------------------------

    def run(self) -> int:
        self.start_record()
        try:
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
            host = (values.get("hostname") or [self.env.get("GH_HOST", "github.com")])[0]
            token = next(
                (self.env[v] for v in (*GH_TOKEN_VARS, *GH_ENTERPRISE_VARS) if self.env.get(v)),
                None,
            )
            token = token or self.file_login(host) or self.keyring_login(host)
        else:
            host = (self.env.get("GITLAB_HOST") or "gitlab.com").split("/")[0]
            token = next((self.env[v] for v in GLAB_TOKEN_VARS if self.env.get(v)), None)
            token = token or self.file_login(host) or self.keyring_login(host)
        if token is None:
            raise self.fail(1, f"no oauth token found for {host}")
        self.out.write(token.encode() + b"\n")

    def api(self, args: list[str]) -> None:
        values, positional = self.parse(args, API_FLAGS[self.backend])
        if "help" in values:
            self.out.write(self.usage().encode())
            return
        if len(positional) != 1:
            raise self.fail(1, f"accepts 1 arg(s), received {len(positional)}\n{self.usage()}")
        for name in UNMODELLED & values.keys():
            raise self.not_modelled(f"--{name}")
        endpoint = positional[0]
        if PLACEHOLDER[self.backend].search(endpoint):
            self.summary["placeholders"] = True
            raise self.not_modelled("placeholder expansion in an endpoint")
        fields = values.get("field", []) + values.get("raw-field", [])
        method = (values.get("method") or ["POST" if fields or "input" in values else "GET"])[-1]
        stdin_text: str | None = None
        if values.get("input") == ["-"]:
            stdin_text = sys.stdin.read()
            self.log("stdin", stdin=stdin_text)
        elif "input" in values:
            raise self.not_modelled("--input FILE (forgectl sends bodies on stdin)")
        file_fields = [f for f in fields if "=" in f and f.split("=", 1)[1].startswith("@")]
        request = self.route(values, endpoint, method)
        request["file_fields"] = file_fields
        request["headers_sent"] = values.get("header", [])
        request["fields"] = fields
        request["include"] = "include" in values
        request["full_url_endpoint"] = bool(re.match(r"https?://", endpoint, re.I))
        self.summary["request"] = request
        self.answer(values, request, stdin_text)

    def route(self, values: dict[str, list[str]], endpoint: str, method: str) -> dict[str, Any]:
        if self.backend == "gh":
            return self.route_gh(values, endpoint, method)
        return self.route_glab(values, endpoint, method)

    def route_gh(self, values: dict[str, list[str]], endpoint: str, method: str) -> dict[str, Any]:
        host = (values.get("hostname") or [self.env.get("GH_HOST") or "github.com"])[-1]
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
            source, token = self.stored_token(host)
        if token is None:
            var = names[0]
            raise self.fail(
                4,
                "To get started with GitHub CLI, please run:  gh auth login\n"
                f"Alternatively, populate the {var} environment variable with a GitHub API "
                "authentication token.",
            )
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
            "token_header": "Authorization",
        }

    def route_glab(
        self, values: dict[str, list[str]], endpoint: str, method: str
    ) -> dict[str, Any]:
        flag = (values.get("hostname") or [None])[-1]
        env_host = self.env.get("GITLAB_HOST") or "gitlab.com"
        scheme = "https"
        spec = env_host
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
        cfg = self.config_dir()
        if cfg.exists() and not os.access(cfg, os.W_OK):
            raise self.fail(1, f"glab: cannot write config directory {cfg}: permission denied")
        source, token, header = None, None, "Private-Token"
        env_token = next((v for v in GLAB_TOKEN_VARS if self.env.get(v)), None)
        if env_token and netloc.split(":")[0] == env_netloc.split(":")[0]:
            source, token = "env:" + env_token, self.env[env_token]
            header = "Authorization" if env_token == "OAUTH_TOKEN" else "Private-Token"
        elif env_token:
            warnings.append(f"credential in {env_token} was not sent to {host}")
            self.say(f"warning: {warnings[-1]}\n")
        if token is None:
            source, token = self.stored_token(host)
        path = endpoint.lstrip("/")
        full = bool(re.match(r"https?://", endpoint, re.I))
        # glab sends its token to whatever host a full-URL endpoint names.
        url = endpoint if full else f"{scheme}://{netloc}{prefix}/api/v4/{path}"
        return {
            "host": host,
            "method": method,
            "url": url,
            "endpoint": path,
            "token_sent": token,
            "token_source": source,
            "token_header": header if token else None,
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

    def answer(
        self, values: dict[str, list[str]], request: dict[str, Any], stdin_text: str | None
    ) -> None:
        rule = self.pick_rule(request, stdin_text)
        if rule is None:
            self.summary["unmatched"] = True
            rule = self.script.get("default") or {
                "status": 404,
                "body": {"message": "Not Found", "forgefake": "no scripted answer"},
            }
        else:
            self.summary["rule"] = rule["index"]
        if rule.get("delay"):
            time.sleep(rule["delay"])
        if rule.get("hang"):
            self.out.flush()
            while True:
                time.sleep(3600)
        status = rule.get("status", 200)
        if "body_text" in rule:
            body = (rule["body_text"] * rule.get("body_repeat", 1)).encode()
        elif "body" in rule:
            body = json.dumps(rule["body"], separators=(",", ":")).encode()
        else:
            body = b""
        if status == 204:
            body = b""
        headers = {"Content-Type": "application/json; charset=utf-8", **rule.get("headers", {})}
        silent = "silent" in values
        if "include" in values and not silent:
            head = f"HTTP/2.0 {status} {REASONS.get(status, 'Status')}\r\n"
            head += "".join(f"{k}: {v}\r\n" for k, v in sorted(headers.items())) + "\r\n"
            self.out.write(head.encode())
        if not silent:
            self.out.write(body)
        self.out.flush()
        if rule.get("stderr"):
            self.say(rule["stderr"])
        if "exit" in rule:
            raise Exit(rule["exit"])
        if status >= 400:
            message = self.error_message(body, status)
            self.say(f"{self.backend}: {message} (HTTP {status})\n")
            raise Exit(1)
        if self.backend == "gh" and request["endpoint"] == "graphql":
            try:
                errors = json.loads(body).get("errors")
            except (ValueError, AttributeError):
                errors = None
            if errors:
                text = "; ".join(
                    str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors
                )
                self.say(f"gh: {text}\n")
                raise Exit(1)
        if rule.get("die"):
            os.kill(os.getpid(), getattr(signal, rule["die"]))

    @staticmethod
    def error_message(body: bytes, status: int) -> str:
        try:
            doc = json.loads(body)
            if isinstance(doc, dict):
                for key in ("message", "error"):
                    if isinstance(doc.get(key), str):
                        return doc[key]
        except ValueError:
            pass
        return REASONS.get(status, "HTTP error")


def main(backend: str, state_dir: str, argv: list[str] | None = None) -> None:
    fake = Fake(backend, Path(state_dir), list(sys.argv[1:] if argv is None else argv))

    def die(signum: int, _frame: Any) -> None:
        fake.log("signal", signal=signal.Signals(signum).name)
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
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
