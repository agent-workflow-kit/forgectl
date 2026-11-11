"""Plain text for the list and the detail pane. Every forge string goes through ``safe``.

These functions return sanitized ``str``; ``safe.plain`` makes them inert display content.
Nothing here knows about widgets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import safe
from .opener import URL_MAX_LENGTH, is_openable
from .source import Row
from .style import Markdown

MAX_THREADS = 200
MAX_COMMENTS_PER_THREAD = 50
MAX_CHECKS = 500
MAX_REVIEWS = 200
COMMENT_LIMIT = 4_000
# One budget for the reviews and comments of the pane, counted after sanitizing, so no
# combination of per-field caps can build a pane that freezes the UI.
REST_BUDGET = 200_000
# The same kind of budget for the check labels, and a shorter display for URLs never opened.
CHECK_BUDGET = 200_000
UNOPENED_URL_LIMIT = 500


def _d(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _login(author: Any) -> str:
    login = author.get("login") if isinstance(author, dict) else None
    return safe.line(login) if login else "(unknown)"


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
        suffix = f"  {safe.line(url, URL_MAX_LENGTH)}"  # shown whole: what o opens
    else:  # shown, never opened
        suffix = f"  {safe.line(url, UNOPENED_URL_LIMIT)}  (not opened: not http or https)"
    return f"{safe.line(check.get('state'))}  {safe.line(check.get('name'))}{suffix}"


def facts_text(snap: dict[str, Any]) -> str:
    """Title and the one-line facts; the description and the CI line are sections of their own."""
    head = _d(snap.get("head"))
    base = _d(snap.get("base"))
    blockers = []
    for b in snap.get("blocked_by") or []:
        if isinstance(b, dict):
            detail = f" ({safe.line(b['detail'])})" if b.get("detail") else ""
            blockers.append(f"{safe.line(b.get('code'))}{detail}")
    labels = ", ".join(safe.line(x, 100) for x in (snap.get("labels") or [])[:50])
    lines = [
        safe.line(snap.get("title")),
        f"{safe.line(snap.get('pr'))}   {safe.line(snap.get('url'), URL_MAX_LENGTH)}",
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
    ]
    return "\n".join(lines)


def description_text(snap: dict[str, Any]) -> str:
    return safe.text(snap.get("body"))


def ci_text(snap: dict[str, Any]) -> str:
    ci = _d(snap.get("ci"))
    return (
        f"CI: {safe.line(ci.get('state', 'none'))}"
        + ("" if ci.get("complete", True) else " (list cut off)")
        + ("" if ci.get("for_current_head", True) else " (checks are for an older commit)")
    )


def checks_of(snap: dict[str, Any]) -> list[dict[str, Any]]:
    ci = _d(snap.get("ci"))
    checks = ci.get("checks") or []
    return [c for c in checks if isinstance(c, dict)][:MAX_CHECKS]


class _Budget:
    """Lines added while they fit in ``limit`` characters; the rest are only counted."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.lines: list[str] = []
        self.skipped = 0

    @property
    def full(self) -> bool:
        return self.skipped > 0

    def add(self, *lines: str) -> None:
        """Add ``lines`` as one block, whole or not at all."""
        size = sum(len(x) + 1 for x in lines)
        if self.full or self.used + size > self.limit:
            self.skipped += 1
            return
        self.used += size
        self.lines.extend(lines)

    def skip(self, n: int) -> None:
        self.skipped += n

    def note(self, *lines: str) -> None:
        """A heading or marker: added while there is room, never counted as not shown."""
        if not self.full and self.used + sum(len(x) + 1 for x in lines) <= self.limit:
            self.used += sum(len(x) + 1 for x in lines)
            self.lines.extend(lines)


def _reviews_out(snap: dict[str, Any]) -> _Budget:
    """The reviews within half of ``REST_BUDGET``, so they cannot starve the comments."""
    out = _Budget(REST_BUDGET // 2)
    latest = [x for x in (snap.get("latest_by_reviewer") or []) if isinstance(x, dict)]
    reviews = [x for x in (snap.get("reviews") or []) if isinstance(x, dict)]
    for entry in latest[:MAX_REVIEWS]:
        out.add(f"  {safe.line(entry.get('state'))}  @{safe.line(entry.get('login'))}")
    for review in reviews[:MAX_REVIEWS]:
        if out.full:  # count what is left, without sanitizing it
            out.skip(1)
            continue
        head = f"  review {safe.line(review.get('state'))} by {_login(review.get('author'))}"
        body = review.get("body")
        out.add(head, *([Markdown(safe.text(body, COMMENT_LIMIT), 4)] if body else []))
    if not latest and not reviews:
        out.note("  (none)")
    return out


def _lines(out: _Budget) -> list[str]:
    lines = list(out.lines)
    if out.full:
        lines.append(f"  ... {out.skipped} more not shown")
    return lines


def _joined(out: _Budget) -> str:
    return "\n".join(_lines(out))


def reviews_text(snap: dict[str, Any], out: _Budget | None = None) -> str:
    return _joined(out if out is not None else _reviews_out(snap))


def comments_text(snap: dict[str, Any], reviews: _Budget | None = None) -> str:
    return _joined(_comments_out(snap, reviews))


def _comments_out(snap: dict[str, Any], reviews: _Budget | None = None) -> _Budget:
    """The comment threads within what the reviews leave of ``REST_BUDGET``."""
    reviews = reviews if reviews is not None else _reviews_out(snap)
    out = _Budget(REST_BUDGET - reviews.used)
    threads = [t for t in (snap.get("threads") or []) if isinstance(t, dict)]
    for thread in threads[:MAX_THREADS]:
        comments = [c for c in (thread.get("comments") or []) if isinstance(c, dict)]
        comments = comments[:MAX_COMMENTS_PER_THREAD]
        if out.full:
            out.skip(1 + len(comments))
            continue
        where = ""
        if thread.get("path"):
            where = f" {safe.line(thread['path'])}"
            if thread.get("line"):
                where += f":{safe.line(thread['line'])}"
        state = "resolved" if thread.get("resolved") else "open"
        out.add(f"  thread{where} ({state})")
        for comment in comments:
            if out.full:
                out.skip(1)
                continue
            out.add(
                f"    @{_login(comment.get('author'))}:",
                Markdown(safe.text(comment.get("body"), COMMENT_LIMIT), 6),
            )
    if not threads:
        out.note("  (none)")
    if len(threads) > MAX_THREADS:
        if out.full:
            out.skip(len(threads) - MAX_THREADS)
        else:
            out.note(f"  ... {len(threads) - MAX_THREADS} more threads not shown")
    return out


LOADING = "loading..."


def note_text(note: str) -> str:
    """A section's body while its data is on its way or failed: one sanitized line."""
    return f"  ({safe.line(note, 200)})"


def failed_note(reason: str) -> str:
    return f"failed: {reason}"


def changes_text(diff: dict[str, Any] | None, note: str | None = None) -> str:
    """The diff (bounded by its own limit), or ``note`` (default: not loaded) when there is none."""
    if diff is None:
        return note_text(note or "not loaded")
    return safe.diff(diff.get("diff"))


def _count(value: Any) -> str:
    """A number for a header: never forge text. Anything but an int from 0 to 10**9 is ``?``."""
    ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**9
    return str(value) if ok else "?"


def counts_of(snap: dict[str, Any]) -> dict[str, str]:
    """The header count of each section that has one, numbers only. CI counts every check
    the forge sent, not only the capped list that is shown."""
    ci_checks = _d(snap.get("ci")).get("checks") or []
    checks = [c for c in ci_checks if isinstance(c, dict)]
    failing = sum(1 for c in checks if c.get("state") == "failing")
    threads = [t for t in (snap.get("threads") or []) if isinstance(t, dict)]
    comments = sum(
        len([c for c in (t.get("comments") or []) if isinstance(c, dict)]) for t in threads
    )
    reviews = [x for x in (snap.get("reviews") or []) if isinstance(x, dict)]
    ci = f"{len(checks)}" + (f", {failing} failing" if failing else "")
    return {
        "changes": f"+{_count(snap.get('additions'))} -{_count(snap.get('deletions'))}, "
        f"{_count(snap.get('changed_files'))} files",
        "ci": ci,
        "comments": str(comments),
        "reviews": str(len(reviews)),
    }


@dataclass
class Detail:
    """Everything the detail pane shows, built off the event loop."""

    url: str | None
    checks: list[dict[str, Any]]
    check_labels: list[str]
    texts: dict[str, str]  # the body of each section, by name (``sections.NAMES``)
    counts: dict[str, str]  # the header count of a section, numbers only; absent: none
    # The lines of a section whose bodies are markdown (``style.Markdown`` entries), so the
    # pane can style them; ``texts`` has the same lines joined, as plain text.
    lines: dict[str, list[str]] = field(default_factory=dict)


def check_labels_of(checks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """The checks whose labels fit in ``CHECK_BUDGET``, their labels, and a marker if cut.

    The returned checks line up with the first labels; a trailing marker has no check.
    """
    labels: list[str] = []
    used = 0
    for check in checks:
        label = check_label(check)
        if used + len(label) > CHECK_BUDGET:
            break
        used += len(label)
        labels.append(label)
    shown = checks[: len(labels)]
    if len(shown) < len(checks):
        labels.append(f"  ... {len(checks) - len(shown)} more not shown")
    return shown, labels


def stub_of(
    item: dict[str, Any],
    note: str,
    changes_note: str | None = None,
    diff: dict[str, Any] | None = None,
) -> Detail:
    """The pane for a PR with no snapshot yet: the facts the list holds, and ``note`` in the
    sections that need the snapshot. The changes section shows ``diff`` when it has arrived,
    else ``changes_note`` (default: ``note``)."""
    url = item.get("url")
    body = note_text(note)
    return Detail(
        url=url if isinstance(url, str) else None,
        checks=[],
        check_labels=[],
        texts={
            "facts": facts_text(item),
            "description": body,
            "changes": changes_text(diff, changes_note or note),
            "ci": body,
            "comments": body,
            "reviews": body,
        },
        counts={},
        lines={},
    )


def detail_of(
    snap: dict[str, Any], diff: dict[str, Any] | None, changes_note: str | None = None
) -> Detail:
    """The pane for ``snap`` and the diff; without a diff the changes section shows
    ``changes_note`` (default: not loaded)."""
    url = snap.get("url")
    checks, labels = check_labels_of(checks_of(snap))
    reviews = _reviews_out(snap)
    comments_out = _comments_out(snap, reviews)
    return Detail(
        url=url if isinstance(url, str) else None,
        checks=checks,
        check_labels=labels,
        texts={
            "facts": facts_text(snap),
            "description": description_text(snap),
            "changes": changes_text(diff, changes_note),
            "ci": ci_text(snap),
            "comments": "\n".join(_lines(comments_out)),
            "reviews": reviews_text(snap, reviews),
        },
        counts=counts_of(snap),
        lines={
            "description": [Markdown(description_text(snap), 0)],
            "comments": _lines(comments_out),
            "reviews": _lines(reviews),
        },
    )
