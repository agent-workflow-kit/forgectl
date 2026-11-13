"""The live harness's one way to talk to a host: ``gh api`` or ``glab api`` through a runner.

A runner is a plain callable, so tests substitute a fake and no host is reached. Every request
checks the host allowlist, refuses an endpoint that could leave the API (a URL, a ``..``), and
refuses a write outside the ``WriteScope``: only the creation call and the resources this run
created. Tokens travel only in the child environment, never in argv, and every error text is
redacted of them.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .guards import (
    LiveConfig,
    LiveRefused,
    PrivateDirs,
    assert_env_safe,
    child_env,
    require_host,
)

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
READ = frozenset({"GET", "HEAD"})
STATUS_LINE = re.compile(r"HTTP/\S+\s+(\d{3})")


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


# argv, environment (the whole of it), stdin text, working directory
Runner = Callable[[Sequence[str], Mapping[str, str], "str | None", Path], RunResult]


def subprocess_runner(
    argv: Sequence[str], env: Mapping[str, str], stdin: str | None, cwd: Path
) -> RunResult:
    """Run one child with exactly ``env``. ``subprocess.run`` kills its own child on timeout."""
    done = subprocess.run(
        list(argv),
        env=dict(env),
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=120,
        check=False,
        start_new_session=True,
    )
    return RunResult(done.returncode, done.stdout, done.stderr)


class LiveApiError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<token>")
    return text


@dataclass
class WriteScope:
    """Where this run may write: the creation endpoint, then the resources it created."""

    creation: str  # e.g. "user/repos" or "projects"
    prefixes: list[str] = field(default_factory=list)

    def allow(self, prefix: str) -> None:
        self.prefixes.append(prefix.strip("/"))

    def permits(self, method: str, endpoint: str) -> bool:
        path = endpoint.split("?", 1)[0].strip("/")
        if method == "POST" and path == self.creation:
            return True
        return any(path == p or path.startswith(p + "/") for p in self.prefixes)


@dataclass(frozen=True)
class Response:
    status: int
    data: Any

    def ok(self) -> bool:
        return 200 <= self.status < 300


def check_endpoint(endpoint: str) -> None:
    if (
        not endpoint
        or "://" in endpoint
        or endpoint.startswith("/")
        or ".." in endpoint.split("?", 1)[0].split("/")
        or any(c in endpoint for c in "\r\n\x00 ")
    ):
        raise LiveRefused(f"endpoint {endpoint!r} is not a plain relative API path")


class LiveApi:
    def __init__(
        self,
        cfg: LiveConfig,
        dirs: PrivateDirs,
        scope: WriteScope,
        *,
        source_env: Mapping[str, str],
        runner: Runner = subprocess_runner,
        second: bool = False,
    ) -> None:
        self.cfg = cfg
        self.dirs = dirs
        self.scope = scope
        self.source_env = source_env
        self.runner = runner
        self.second = second
        require_host(cfg.host)

    def request(
        self, method: str, endpoint: str, body: Mapping[str, Any] | None = None
    ) -> Response:
        method = method.upper()
        require_host(self.cfg.host)
        check_endpoint(endpoint)
        if method not in MUTATING | READ:
            raise LiveRefused(f"method {method!r} is not allowed")
        if method in MUTATING and not self.scope.permits(method, endpoint):
            raise LiveRefused(f"{method} {endpoint.split('?', 1)[0]} is outside this run's scope")
        env = child_env(self.cfg, self.dirs, self.source_env, second=self.second)
        assert_env_safe(env, self.dirs, self.source_env)
        tool = "gh" if self.cfg.backend == "github" else "glab"
        argv = [tool, "api", "--hostname", self.cfg.host, "--include", "-X", method]
        stdin = None
        if body is not None:
            argv += ["-H", "Content-Type: application/json", "--input", "-"]
            stdin = json.dumps(body)
        argv.append(endpoint)
        result = self.runner(argv, env, stdin, self.dirs.cwd)
        return self._parse(result)

    def _parse(self, result: RunResult) -> Response:
        secrets = self.cfg.secrets()
        head, _sep, body = result.stdout.replace("\r\n", "\n").partition("\n\n")
        match = STATUS_LINE.match(head)
        if not match:
            detail = redact(result.stderr.strip()[:300], secrets)
            raise LiveApiError(f"no HTTP status in the answer (exit {result.returncode}): {detail}")
        status = int(match.group(1))
        text = body.strip()
        data: Any = None
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                data = redact(text[:300], secrets)
        return Response(status, data)

    def expect(self, method: str, endpoint: str, status: Sequence[int], body: Any = None) -> Any:
        """``request`` that raises ``LiveApiError`` unless the status is one of ``status``."""
        response = self.request(method, endpoint, body)
        if response.status not in status:
            detail = redact(json.dumps(response.data)[:300], self.cfg.secrets())
            raise LiveApiError(
                f"{method} {endpoint.split('?', 1)[0]} answered {response.status}: {detail}",
                response.status,
            )
        return response.data
