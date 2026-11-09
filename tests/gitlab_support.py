"""Support for the GitLab adapter tests: hand-built REST recordings and an in-process `glab`.

``FakeGitLab.runner`` replaces the gateway's process runner. It answers by endpoint (the path the
gateway built, query stripped) with a status, headers and a JSON body, and records every call.
The recordings in ``tests/recordings/gitlab/`` are built by hand from the documented GitLab API
(no recorded host exists yet) and use placeholder accounts, so ``test_harness_scrub`` keeps them
clean. Every field name, enum value and null-versus-missing choice in them is UNVERIFIED against a
real host (the live-verification issues settle that).
"""

from __future__ import annotations

import copy
import json
import stat
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgectl.backends.gitlab import GitLabBackend
from forgectl.config import Host, LoadedConfig
from forgectl.gateway import Gateway, ProcessResult, quote_component
from forgectl.hosts import Hosts
from forgectl.identifiers import PrRef

RECORDINGS = Path(__file__).resolve().parent / "recordings" / "gitlab"
TOKEN = "tok-gitlab-test-0001"
HEAD = "a1" * 20
BASE = "b2" * 20
OLD = "c3" * 20

MR_PATH = "projects/user-9%2Fproj/merge_requests/7"
P = "projects/101"
MR_ID = "projects/101/merge_requests/7"


def recording(name: str, scenario: str = "base") -> Any:
    """A fresh copy of a recorded answer."""
    return copy.deepcopy(json.loads((RECORDINGS / scenario / f"{name}.json").read_text("utf-8")))


@dataclass
class Answer:
    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    raw: bytes | None = None  # a body sent as is (not JSON)


Handler = Callable[[dict[str, str]], Answer]


@dataclass
class Seen:
    endpoint: str
    path: str
    query: dict[str, str]
    argv: list[str]
    env: dict[str, str]
    cwd: str


class FakeGitLab:
    """An in-process `glab api`: routes by endpoint path, records calls."""

    def __init__(self) -> None:
        self.routes: dict[str, Handler] = {}
        self.calls: list[Seen] = []
        self.unrouted: list[str] = []

    # routing ---------------------------------------------------------------------------

    def on(
        self,
        path: str,
        body: Any = None,
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        raw: bytes | None = None,
    ) -> None:
        answer = Answer(status, body, dict(headers or {}), raw)
        self.routes[path] = lambda _q: answer

    def on_handler(self, path: str, handler: Handler) -> None:
        self.routes[path] = handler

    def pages(self, path: str, pages: Sequence[Any], *, per_page: int | None = None) -> None:
        """A paginated list: ``page=N`` gets ``pages[N-1]`` with ``x-next-page`` set."""

        def handler(query: dict[str, str]) -> Answer:
            n = int(query.get("page", "1"))
            if not 1 <= n <= len(pages):
                return Answer(200, [], {"x-next-page": ""})
            return Answer(200, pages[n - 1], {"x-next-page": str(n + 1) if n < len(pages) else ""})

        self.routes[path] = handler

    def count(self, path: str) -> int:
        return sum(1 for c in self.calls if c.path == path)

    # the runner ------------------------------------------------------------------------

    def runner(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        stdin: bytes | None,
        timeout: float,
        max_stdout: int,
        max_stderr: int,
        on_start: Callable[[int, int], None] | None,
    ) -> ProcessResult:
        endpoint = argv[-1]
        path, _, query_text = endpoint.partition("?")
        query = dict(urllib.parse.parse_qsl(query_text, keep_blank_values=True))
        self.calls.append(Seen(endpoint, path, query, list(argv), dict(env), cwd))
        handler = self.routes.get(path)
        if handler is None:
            self.unrouted.append(endpoint)
            answer = Answer(404, {"message": "404 Not Found"})
        else:
            answer = handler(query)
        raw = answer.raw if answer.raw is not None else json.dumps(answer.body).encode()
        head = f"HTTP/2.0 {answer.status} X\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in answer.headers.items()) + "\r\n"
        ok = 200 <= answer.status < 300
        return ProcessResult(1, 1, 0 if ok else 1, head.encode() + raw, b"")


def make_backend(
    fake: FakeGitLab,
    tmp_path: Path,
    *,
    host: str = "gitlab.com",
    prefix: str = "",
    warn: Callable[[str], None] | None = None,
    retry: Any = None,
    today: Any = None,
    until_known: Any = None,
    timeout: float = 30.0,
) -> GitLabBackend:
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    glab = bindir / "glab"
    glab.write_text("#!/bin/sh\nexit 1\n")
    glab.chmod(glab.stat().st_mode | stat.S_IXUSR)
    declared: dict[str, Host] = {}
    if host != "gitlab.com":
        declared[host] = Host(backend="gitlab", api_prefix=prefix or None)
    declared[host] = declared.get(host, Host()).model_copy(update={"token_env": "MY_TOKEN"})
    base = LoadedConfig.defaults()
    config = LoadedConfig(
        config=base.config.model_copy(update={"hosts": declared}),
        config_hash="x",
        config_path=None,
    )
    environ = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "MY_TOKEN": TOKEN,
    }
    gateway = Gateway(config, timeout=timeout, environ=environ, runner=fake.runner)
    hosts = Hosts(gateway)
    return GitLabBackend(hosts, warn=warn, retry=retry, today=today, until_known=until_known)


def ref(
    project: str = "user-9/proj", number: int = 7, host: str = "gitlab.com", backend: str = "gitlab"
) -> PrRef:
    return PrRef(host=host, backend=backend, project=project, number=number)  # type: ignore[arg-type]


def mr_path(project: str = "user-9/proj", number: int = 7) -> str:
    return f"projects/{quote_component(project)}/merge_requests/{number}"


def standard(fake: FakeGitLab, **overrides: Any) -> dict[str, Any]:
    """Route the base recordings; ``overrides`` replace any named recording. Returns the
    recordings (after overrides) so a test can read what it routed."""
    data = {
        name: recording(name)
        for name in (
            "mr",
            "project",
            "approvals",
            "project_approvals",
            "reviewers",
            "statuses",
            "notes",
            "member",
            "user",
            "version",
            "source_project",
            "bridges",
        )
    }
    data.update(overrides)
    fake.on(mr_path(), data["mr"])
    fake.on("projects/101", data["project"])
    fake.on(f"{MR_ID}/approvals", data["approvals"])
    fake.on("projects/101/approvals", data["project_approvals"])
    fake.on(f"{MR_ID}/reviewers", data["reviewers"])
    for project in (101, 202):
        fake.pages(f"projects/{project}/repository/commits/{HEAD}/statuses", [data["statuses"]])
        fake.pages(f"projects/{project}/pipelines/5001/bridges", [data["bridges"]])
    fake.pages(f"{MR_ID}/notes", [data["notes"]])
    fake.on("projects/101/members/all/12", data["member"])
    fake.on("users/11", data["user"])
    fake.on("version", data["version"])
    fake.on("projects/202", data["source_project"])
    return data


__all__ = [
    "BASE",
    "HEAD",
    "MR_ID",
    "MR_PATH",
    "OLD",
    "TOKEN",
    "Answer",
    "FakeGitLab",
    "make_backend",
    "mr_path",
    "recording",
    "ref",
    "standard",
]
