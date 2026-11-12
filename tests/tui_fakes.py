"""Deterministic in-memory data source and opener for the browse tests.

The shapes are the normalized objects' ``to_dict`` forms from ``schemas/``; the tests validate
them against the schemas, so a fixture cannot drift from the contract.
"""

from __future__ import annotations

import copy
import re
import threading
from collections.abc import Callable
from typing import Any

SHA_A = "a" * 40
SHA_B = "b" * 40
TS = "2026-11-03T12:00:00Z"


def make_item(
    number: int = 1,
    *,
    repo: str = "github.com/acme/widgets",
    title: str = "A change",
    login: str = "alice",
    updated_at: str = TS,
    draft: bool = False,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "pr": f"{repo}#{number}",
        "pr_id": f"PR_{number}",
        "repo": repo,
        "repo_id": "R_" + repo.replace("/", "_").replace(".", "_"),
        "number": number,
        "url": f"https://{repo.split('/', 1)[0]}/{repo.split('/', 1)[1]}/pull/{number}",
        "title": title,
        "author": {"login": login, "id": f"U_{login}", "is_bot": False},
        "state": "open",
        "draft": draft,
        "labels": labels or [],
        "head": {
            "sha": SHA_A,
            "ref": "feature",
            "repo": repo,
            "is_fork": False,
        },
        "base": {"ref": "main"},
        "created_at": TS,
        "updated_at": updated_at,
    }


def make_check(
    name: str = "build", state: str = "passing", url: str | None = "https://ci.example/1"
) -> dict[str, Any]:
    return {
        "name": name,
        "state": state,
        "required": None,
        "counted": state != "skipped",
        "url": url,
        "finished_at": None,
    }


def make_comment(body: str = "looks fine", login: str = "bob", n: int = 1) -> dict[str, Any]:
    return {
        "id": f"gh:ic:C{n}",
        "author": {"login": login, "id": f"U_{login}", "is_bot": False},
        "body": body,
        "created_at": TS,
        "updated_at": TS,
        "minimized": False,
        "reply_to": None,
        "review_id": None,
    }


def make_snapshot(
    number: int = 1,
    *,
    repo: str = "github.com/acme/widgets",
    title: str = "A change",
    body: str = "Body text",
    login: str = "alice",
    checks: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    branch: str = "feature",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = make_item(number, repo=repo, title=title, login=login)
    checks = [make_check()] if checks is None else checks
    states = {c["state"] for c in checks if c["counted"]}
    ci_state = (
        "failing"
        if "failing" in states
        else "pending"
        if "pending" in states
        else "passing"
        if "passing" in states
        else "none"
    )
    threads = [
        {
            "id": f"gh:rt:T{i}",
            "resolvable": False,
            "resolved": False,
            "outdated": None,
            "path": None,
            "line": None,
            "side": None,
            "start_line": None,
            "commit_sha": None,
            "comments": [c],
        }
        for i, c in enumerate(comments or [], start=1)
    ]
    snap: dict[str, Any] = {
        **item,
        "backend": "github",
        "body": body,
        "created_at": TS,
        "closed_at": None,
        "merged_at": None,
        "merged_by": None,
        "author_association": None,
        "merged_head_sha": None,
        "merge_commit_sha": None,
        "head_fetch_ref": f"refs/pull/{number}/head",
        "mergeable": True,
        "blocked_by": [],
        "auto_merge": {"enabled": False, "by": None},
        "merge_queue": False,
        "merge_methods_allowed": None,
        "additions": 3,
        "deletions": 1,
        "changed_files": 1,
        "review_decision": "none",
        "approvals": 0,
        "approvals_at_head": 0,
        "approvals_left": None,
        "review_requests": [],
        "teams_visible": True,
        "reviews": reviews or [],
        "latest_by_reviewer": [],
        "threads": threads,
        "activity": {
            "comments_total": len(threads),
            "unresolved_threads": len(threads),
            "others_since_push": None,
            "awaiting_my_reply": len(threads),
        },
        "ci": {
            "state": ci_state,
            "head_sha": SHA_A,
            "for_current_head": True,
            "complete": True,
            "checks": checks,
        },
        "rate_limit": {"host": "github.com", "remaining": 4000, "reset_at": TS},
        "complete": True,
        "incomplete": [],
        "fetched_at": TS,
    }
    snap["head"] = {
        **item["head"],
        "ref": branch,
        "pushed_at": None,
        "pushed_at_source": None,
    }
    snap["base"] = {"ref": "main", "sha": SHA_B, "is_default": True}
    snap.update(extra or {})
    return snap


def make_diff(pr: str, text: str = "@@ -1 +1 @@\n-old\n+new\n") -> dict[str, Any]:
    return {
        "pr": pr,
        "head_sha": SHA_A,
        "base_sha": SHA_B,
        "merge_base_sha": SHA_B,
        "diff": text,
    }


class FakeSource:
    """A data source over fixed answers; it records every call it gets."""

    def __init__(
        self,
        *,
        authored: list[dict[str, Any]] | None = None,
        requested: list[dict[str, Any]] | None = None,
        snapshots: dict[str, dict[str, Any]] | None = None,
        diffs: dict[str, dict[str, Any]] | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.authored = authored or []
        self.requested = requested or []
        self.snapshots = snapshots or {}
        self.diffs = diffs or {}
        self.errors = errors or []
        self.calls: list[tuple[str, Any]] = []

    def _list(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "items": copy.deepcopy(items),
            "complete": True,
            "errors": copy.deepcopy(self.errors),
            "hosts_queried": ["github.com"],
            "rate_limit": {"host": "github.com", "remaining": 4000, "reset_at": TS},
        }

    def prs(
        self,
        *,
        author: str | None = None,
        review_requested_from: str | None = None,
        state: str = "open",
        limit: int = 100,
    ) -> dict[str, Any]:
        self.calls.append(
            ("prs", {"author": author, "review_requested_from": review_requested_from})
        )
        return self._list(self.authored if author else self.requested)

    def snapshot(self, pr: str) -> dict[str, Any]:
        self.calls.append(("snapshot", pr))
        return copy.deepcopy(self.snapshots[pr])

    def diff(self, pr: str) -> dict[str, Any]:
        self.calls.append(("diff", pr))
        return copy.deepcopy(self.diffs.get(pr) or make_diff(pr))


class GatedSource(FakeSource):
    """A ``FakeSource`` whose chosen calls block until ``gate`` is set (at most ``timeout``)."""

    def __init__(self, *, gated: tuple[str, ...], timeout: float = 10.0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gated = gated
        self.gate = threading.Event()
        self.timeout = timeout

    def _wait(self, name: str) -> None:
        if name in self.gated:
            self.gate.wait(self.timeout)

    def prs(self, **kwargs: Any) -> dict[str, Any]:
        answer = super().prs(**kwargs)
        self._wait("prs")
        return answer

    def snapshot(self, pr: str) -> dict[str, Any]:
        answer = super().snapshot(pr)
        self._wait("snapshot")
        return answer


class FakeOpener:
    """Records the URLs it was asked to open; never starts a process."""

    def __init__(self) -> None:
        self.opened: list[str] = []

    def open(self, url: str, on_exit: Callable[[int], None] | None = None) -> str:
        self.opened.append(url)
        return "fake-opener"


def safe_style(style: object) -> bool:
    """Whether a span style is built only from fixed words, hex colors and ``on <hex>``."""
    if not isinstance(style, str):
        return False
    words = style.split()
    i = 0
    while i < len(words):
        word = words[i]
        if word == "on":
            i += 1
            if i >= len(words) or not re.fullmatch(r"#[0-9a-fA-F]{6}", words[i]):
                return False
        elif word not in ("bold", "italic", "underline", "strike", "dim") and not re.fullmatch(
            r"#[0-9a-fA-F]{6}", word
        ):
            return False
        i += 1
    return bool(words)


class MergeSource(FakeSource):
    """A ``FakeSource`` that can merge: ``merge`` records its call and answers from ``script``,
    a list of answers (a dict, or an exception to raise) taken in order, the last one repeating.
    ``gate``, when set, blocks the real write (not the dry run) until it is released."""

    def __init__(self, *, script: list[Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.script: list[Any] = list(script or [])
        self.merges: list[dict[str, Any]] = []
        self.gate: threading.Event | None = None
        self.in_write = threading.Event()

    def merge(
        self,
        pr: str,
        *,
        head: str,
        base: str,
        method: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        call = {"pr": pr, "head": head, "base": base, "method": method, "dry_run": dry_run}
        self.merges.append(call)
        self.calls.append(("merge", call))
        if not dry_run:
            self.in_write.set()
            if self.gate is not None:
                self.gate.wait(10.0)
        answer = self.script[min(len(self.merges), len(self.script)) - 1] if self.script else None
        if isinstance(answer, BaseException):
            raise answer
        if answer is not None:
            return copy.deepcopy(answer)
        return merge_plan(pr, head, base, method) if dry_run else merge_done(pr, method)


def merge_plan(
    pr: str, head: str, base: str, method: str | None, *, blocked: list[Any] | None = None
) -> dict[str, Any]:
    method = method or "squash"
    return {
        "pr": pr,
        "merged": False,
        "already_merged": False,
        "auto_merge_enabled": False,
        "merged_head_sha": None,
        "merge_commit_sha": None,
        "method": method,
        "dry_run": True,
        "would_refuse": None,
        "plan": {
            "head_sha": head,
            "expected_head": head,
            "expected_base": base,
            "base_ref": base,
            "auto": False,
            "override_blocks": False,
            "blocked_by": blocked or [],
            "method": method,
        },
    }


def merge_done(pr: str, method: str | None) -> dict[str, Any]:
    return {
        "pr": pr,
        "merged": True,
        "already_merged": False,
        "auto_merge_enabled": False,
        "merged_head_sha": SHA_A,
        "merge_commit_sha": "c" * 40,
        "method": method or "squash",
        "dry_run": False,
    }
