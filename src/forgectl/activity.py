"""The derived `activity` block of `snapshot` (#57; interface: Activity).

Host-neutral: a backend hands over normalized inputs (the snapshot's own threads and reviews, the
approvals it wants counted, the push time of the head) and gets the same four counts for either
host. Nothing is stored. GitHub passes ``reviews`` and no ``approvals``; GitLab, whose ``reviews``
is always empty, passes no ``reviews`` and its ``latest_by_reviewer`` as ``approvals``.

Times are compared as instants, never as text: a time is RFC 3339 with an offset, and one that
is not is an error (a silent miss would hide a comment from the answer).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from functools import lru_cache
from typing import TYPE_CHECKING

from . import model as m

if TYPE_CHECKING:
    from .reads import ReadEnv

_RFC3339 = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.([0-9]+))?([Zz]|[+-][0-9]{2}:[0-9]{2})"
)

# An instant: UTC to the whole second, then the exact fraction of the second. Tuples order
# like the instants they stand for, however many fraction digits a host wrote.
type _Instant = tuple[datetime, Fraction]


@dataclass(frozen=True, slots=True)
class Viewer:
    """Me: the authenticated account (or the configured ``me``, which has no id)."""

    login: str
    id: str | None


def _instant(text: str, what: str) -> _Instant:
    found = _RFC3339.fullmatch(text) if isinstance(text, str) else None
    if found is None:
        raise ValueError(f"{what} is not an RFC 3339 time with an offset")
    whole, digits, offset = found.groups()
    try:
        moment = datetime.fromisoformat(f"{whole}{offset}".upper()).astimezone(UTC)
        fraction = Fraction(int(digits), 10 ** len(digits)) if digits else Fraction(0)
    except (ValueError, OverflowError):
        raise ValueError(f"{what} is not an RFC 3339 time with an offset") from None
    return moment, fraction


@lru_cache(maxsize=256)
def _pattern(glob: str) -> re.Pattern[str]:
    """``*`` is any run and ``?`` one character; everything else is literal, so the brackets of
    ``dependabot[bot]`` are not a character class. Case-insensitive like logins."""
    parts = [".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in glob]
    return re.compile("".join(parts), re.DOTALL)


def ignored(login: str | None, patterns: Iterable[str]) -> bool:
    """Whether ``login`` matches one of ``activity.ignore_authors``. No login is never ignored."""
    if login is None:
        return False
    name = m.ascii_lower(login)
    return any(_pattern(m.ascii_lower(p)).fullmatch(name) for p in patterns)


def _is_me(me: Viewer, login: str | None, id_: str | None) -> bool:
    """Accounts compare by id; only when either side has no id do logins (ASCII-lowered)."""
    if me.id is not None and id_ is not None:
        return me.id == id_
    if login is None:
        return False
    return m.ascii_lower(me.login) == m.ascii_lower(login)


def derive(
    *,
    threads: Sequence[m.Thread],
    reviews: Sequence[m.Review],
    approvals: Sequence[m.LatestByReviewer],
    pushed_at: str | None,
    me: Viewer,
    ignore_authors: Sequence[str],
) -> m.Activity:
    """The counts of interface.md "Activity". ``pushed_at`` is ``head.pushed_at`` whatever its
    source, or ``None``, which makes ``others_since_push`` ``None``.

    My own account is never "others" and never ignored, so my reply always counts as mine even
    when the config lists my login.
    """
    # Every time is read, whoever wrote it: a bad one is an error, never a silent miss.
    pushed = _instant(pushed_at, "pushed_at") if pushed_at is not None else None
    comment_at = {
        id(c): _instant(c.created_at, "a comment's time") for t in threads for c in t.comments
    }
    review_at = [_instant(r.submitted_at, "a review's time") for r in reviews]
    approval_at = [
        _instant(a.at, "an approval's time") if a.at is not None else None for a in approvals
    ]

    def other(login: str | None, id_: str | None) -> bool:
        return not _is_me(me, login, id_) and not ignored(login, ignore_authors)

    others: int | None = None
    if pushed is not None:
        others = 0
        for t in threads:
            for c in t.comments:
                if other(c.author.login, c.author.id):
                    others += comment_at[id(c)] > pushed
        for r, at in zip(reviews, review_at, strict=True):
            if other(r.author.login, r.author.id):
                others += at > pushed
        for a, at in zip(approvals, approval_at, strict=True):
            if a.state == "approved" and at is not None and other(a.login, a.id):
                others += at > pushed

    awaiting = 0
    for t in threads:
        if t.resolved:
            continue
        # by instant; sorted() is stable, so equal instants keep the order the host gave
        for c in reversed(sorted(t.comments, key=lambda c: comment_at[id(c)])):
            if _is_me(me, c.author.login, c.author.id):
                break  # my reply is the last word
            if not ignored(c.author.login, ignore_authors):
                awaiting += 1
                break

    return m.Activity(
        comments_total=sum(len(t.comments) for t in threads),
        unresolved_threads=sum(1 for t in threads if not t.resolved),
        others_since_push=others,
        awaiting_my_reply=awaiting,
    )


def for_snapshot(
    env: ReadEnv,
    host: str,
    *,
    threads: Sequence[m.Thread],
    reviews: Sequence[m.Review],
    approvals: Sequence[m.LatestByReviewer],
    head_push: m.PushTime | None,
) -> m.Activity:
    """What a backend calls: ``derive`` with me (resolved once per run, see ``Hosts.viewer``)
    and the configured ignore list."""
    return derive(
        threads=threads,
        reviews=reviews,
        approvals=approvals,
        pushed_at=head_push.at if head_push is not None else None,
        me=env.hosts.viewer(host, call=env.call),
        ignore_authors=env.hosts.ignore_authors(),
    )
