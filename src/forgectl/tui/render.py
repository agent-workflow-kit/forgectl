"""Plain text for the list and the detail pane. Every forge string goes through ``safe``.

These functions return sanitized ``str``; ``safe.plain`` makes them inert display content.
Nothing here knows about widgets.
"""

from __future__ import annotations

from typing import Any

from . import safe
from .opener import is_openable
from .source import Row

MAX_THREADS = 200
MAX_COMMENTS_PER_THREAD = 50
MAX_CHECKS = 500
MAX_REVIEWS = 200
COMMENT_LIMIT = 4_000


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _login(author: Any) -> str:
    login = author.get("login") if isinstance(author, dict) else None
    return safe.line(login) if login else "(unknown)"


def row_text(row: Row) -> str:
    item = row.item
    parts = [safe.line(item.get("pr")), safe.line(item.get("title"))]
    if item.get("draft"):
        parts.append("[draft]")
    parts.append(f"@{_login(item.get('author'))}")
    parts.append("+".join(sorted(row.roles)))
    return "  ".join(p for p in parts if p)


def row_haystack(row: Row) -> str:
    """The text ``/`` filters over, sanitized and lowercased."""
    item = row.item
    head = _d(item.get("head"))
    fields = [
        item.get("pr"),
        item.get("title"),
        (item.get("author") or {}).get("login") if isinstance(item.get("author"), dict) else None,
        head.get("ref"),
        *[label for label in (item.get("labels") or []) if isinstance(label, str)],
    ]
    return "\n".join(safe.line(f) for f in fields if f).lower()


def check_label(check: dict[str, Any]) -> str:
    url = check.get("url")
    if url is None:
        suffix = ""
    elif is_openable(url):
        suffix = f"  {safe.line(url, 500)}"
    else:  # shown, never opened
        suffix = f"  {safe.line(url, 500)}  (not opened: not http or https)"
    return f"{safe.line(check.get('state'))}  {safe.line(check.get('name'))}{suffix}"


def facts_text(snap: dict[str, Any]) -> str:
    head = _d(snap.get("head"))
    base = _d(snap.get("base"))
    blockers = []
    for b in snap.get("blocked_by") or []:
        if isinstance(b, dict):
            detail = f" ({safe.line(b['detail'])})" if b.get("detail") else ""
            blockers.append(f"{safe.line(b.get('code'))}{detail}")
    labels = ", ".join(safe.line(x, 100) for x in (snap.get("labels") or [])[:50])
    ci = _d(snap.get("ci"))
    lines = [
        safe.line(snap.get("title")),
        f"{safe.line(snap.get('pr'))}   {safe.line(snap.get('url'), 500)}",
        f"state: {safe.line(snap.get('state'))}"
        + ("  draft" if snap.get("draft") else "")
        + f"   author: {_login(snap.get('author'))}",
        f"branch: {safe.line(head.get('ref'))} -> {safe.line(base.get('ref'))}",
        f"review: {safe.line(snap.get('review_decision'))}   approvals: "
        f"{safe.line(snap.get('approvals'))}   mergeable: {safe.line(snap.get('mergeable'))}",
        f"blocked by: {', '.join(blockers) if blockers else 'nothing known'}",
        f"size: +{safe.line(snap.get('additions'))} -{safe.line(snap.get('deletions'))} in "
        f"{safe.line(snap.get('changed_files'))} files",
        f"labels: {labels or '(none)'}",
        f"updated: {safe.line(snap.get('updated_at'))}",
        "",
        safe.text(snap.get("body")),
        "",
        f"CI: {safe.line(ci.get('state', 'none'))}"
        + ("" if ci.get("complete", True) else " (list cut off)")
        + ("" if ci.get("for_current_head", True) else " (checks are for an older commit)"),
    ]
    return "\n".join(lines)


def checks_of(snap: dict[str, Any]) -> list[dict[str, Any]]:
    ci = _d(snap.get("ci"))
    checks = ci.get("checks") or []
    return [c for c in checks if isinstance(c, dict)][:MAX_CHECKS]


def rest_text(snap: dict[str, Any], diff: dict[str, Any] | None) -> str:
    out: list[str] = ["Reviews"]
    latest = [x for x in (snap.get("latest_by_reviewer") or []) if isinstance(x, dict)]
    for entry in latest[:MAX_REVIEWS]:
        out.append(f"  {safe.line(entry.get('state'))}  @{safe.line(entry.get('login'))}")
    for review in [x for x in (snap.get("reviews") or []) if isinstance(x, dict)][:MAX_REVIEWS]:
        out.append(f"  review {safe.line(review.get('state'))} by {_login(review.get('author'))}")
        body = review.get("body")
        if body:
            out.append(_indent(safe.text(body, COMMENT_LIMIT)))
    if len(out) == 1:
        out.append("  (none)")
    out += ["", "Comments"]
    threads = [t for t in (snap.get("threads") or []) if isinstance(t, dict)]
    for thread in threads[:MAX_THREADS]:
        where = ""
        if thread.get("path"):
            where = f" {safe.line(thread['path'])}"
            if thread.get("line"):
                where += f":{safe.line(thread['line'])}"
        state = "resolved" if thread.get("resolved") else "open"
        out.append(f"  thread{where} ({state})")
        for comment in [c for c in (thread.get("comments") or []) if isinstance(c, dict)][
            :MAX_COMMENTS_PER_THREAD
        ]:
            out.append(f"    @{_login(comment.get('author'))}:")
            out.append(_indent(safe.text(comment.get("body"), COMMENT_LIMIT), 6))
    if not threads:
        out.append("  (none)")
    if len(threads) > MAX_THREADS:
        out.append(f"  ... {len(threads) - MAX_THREADS} more threads not shown")
    out += ["", "Diff"]
    if diff is None:
        out.append("  (not loaded)")
    else:
        out.append(safe.text(diff.get("diff"), safe.DIFF_LIMIT))
    return "\n".join(out)


def _indent(text: str, n: int = 4) -> str:
    pad = " " * n
    return "\n".join(pad + line for line in text.split("\n"))
