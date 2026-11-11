"""Shared by the bulk snapshot tests (#116): synthetic GitHub answers composed from the recordings
of the per-PR reads, so the bulk reader and the per-PR path are fed the same facts."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from forgectl.gateway import ProcessResult
from forgectl.identifiers import PrRef

from .test_github_show import HEAD, http
from .test_github_show import recording as show_recording
from .test_github_snapshot import ME_USER, SCOPES, pr_of, rec

HOST = "github.com"


def ref(number: int = 7, project: str = "user-7/example-repo") -> PrRef:
    return PrRef(host=HOST, backend="github", project=project, number=number)


def bulk_pr(number: int = 7, *, required: bool = False) -> dict[str, Any]:
    """The ``pullRequest`` object of a bulk answer: the fields of the show, parts, requests and
    push recordings in one object, as ``BulkPr`` selects them. ``required`` keeps the
    ``isRequired`` of the checks (the bulk query does not select it)."""
    show = copy.deepcopy(pr_of(show_recording()))
    parts = copy.deepcopy(pr_of(rec("parts")))
    requests = copy.deepcopy(pr_of(rec("requests")))
    push = copy.deepcopy(pr_of(rec("push")))
    node = show
    node["number"] = number
    node["url"] = f"https://github.com/user-7/example-repo/pull/{number}"
    node["id"] = f"PR_kwDOexample{number:04d}"
    reviews = parts["reviews"]
    reviews["pageInfo"] = {
        "hasNextPage": False,
        "hasPreviousPage": False,
        "endCursor": "Y3Vyc29yOjE=",
        "startCursor": "Y3Vyc29yOjE=",
    }
    node["reviews"] = reviews
    node["reviewThreads"] = parts["reviewThreads"]
    node["comments"] = parts["comments"]
    node["retargets"] = node.pop("timelineItems")
    node["reviewRequests"] = requests["reviewRequests"]
    node["requestEvents"] = requests["timelineItems"]
    node["pushEvents"] = {
        "pageInfo": push["timelineItems"]["pageInfo"],
        "nodes": [
            n for n in push["timelineItems"]["nodes"] if n["__typename"] != "PullRequestCommit"
        ],
    }
    commit = node["commits"]["nodes"][0]["commit"]
    commit["committedDate"] = "2026-10-02T07:30:00Z"
    if not required:
        for check in commit["statusCheckRollup"]["contexts"]["nodes"]:
            check.pop("isRequired", None)
    return node


def bulk_repo(number: int = 7, **kwargs: Any) -> dict[str, Any]:
    show = pr_of(show_recording())
    repo = copy.deepcopy(show_recording()["data"]["repository"])
    repo["pullRequest"] = bulk_pr(number, **kwargs)
    del show
    return repo


def bulk_answer(*numbers: int, remaining: int = 4700) -> dict[str, Any]:
    data: dict[str, Any] = {
        "rateLimit": {"cost": 1, "remaining": remaining, "resetAt": "2026-10-02T12:00:00Z"}
    }
    for i, number in enumerate(numbers):
        data[f"p{i}"] = bulk_repo(number)
    return {"data": data}


def numbers_of(request: Mapping[str, Any]) -> list[int]:
    variables = request.get("variables", {})
    return [variables[f"k{i}"] for i in range(len(variables) // 3)]


def answer_for(request: Mapping[str, Any]) -> dict[str, Any]:
    return bulk_answer(*numbers_of(request))


def operation(request: Mapping[str, Any]) -> str:
    match = re.search(r"query\s+(\w+)", request.get("query", ""))
    return match.group(1) if match else "?"


class BulkRouter:
    """A process runner for the bulk query: ``bulk`` gets the request and gives the document (or
    a ready ``bytes`` answer); every other operation is answered from ``others`` like
    ``test_github_snapshot.Router``."""

    def __init__(
        self,
        bulk: Callable[[dict[str, Any]], Any] = answer_for,
        others: Mapping[str, Any] | None = None,
    ) -> None:
        self.bulk = bulk
        self.others = {"user": http(200, ME_USER, headers=SCOPES), **(others or {})}
        self.requests: list[dict[str, Any]] = []
        self.ops: list[str] = []
        self.argvs: list[list[str]] = []

    def __call__(self, argv: Sequence[str], *, stdin: bytes | None, **_: Any) -> ProcessResult:
        self.argvs.append(list(argv))
        request = json.loads(stdin) if stdin else {}
        self.requests.append(request)
        op = operation(request) if request else "rest"
        if op == "rest" and argv[-1] == "user":
            op = "user"
        self.ops.append(op)
        if op == "ForgectlBulk":
            answer: Any = self.bulk(request)
        else:
            assert op in self.others, f"no answer scripted for {op}"
            answer = self.others[op]
            if callable(answer):
                answer = answer(request)
        if isinstance(answer, ProcessResult):
            return answer
        raw: bytes = answer if isinstance(answer, bytes) else http(200, answer)
        code = 0 if raw.startswith(b"HTTP/2.0 2") and b'"errors"' not in raw else 1
        return ProcessResult(pid=1, pgid=1, returncode=code, stdout=raw, stderr=b"")

    @property
    def bulk_sizes(self) -> list[int]:
        return [len(numbers_of(r)) for r in self.requests if operation(r) == "ForgectlBulk"]


__all__ = ["HEAD", "BulkRouter", "bulk_answer", "bulk_pr", "bulk_repo", "ref"]
