"""What every text post shares across the hosts (interface: Post order, Dedupe key, Reliability).

``Poster`` is the write side of ``comment`` and ``reply``. It is kept apart from ``Backend`` so a
new post command adds its own module and never edits the read adapters. A poster never retries a
write, and it never believes an answer without its documented status and proof (``created_id``):
a redirected write comes back as a GET's answer and fails that proof, so nothing "posted" is
reported for it.

``review`` (``github_review.py``, #26, and ``gitlab_review.py``, #27) has its own modules and
protocol. ``ThreadPoster`` is the write side of ``resolve`` and ``unresolve``, in its own backend
modules (``github_resolve``, ``gitlab_resolve``).

TODO(#28): ``approve`` posts extend the protocol here, in its own backend module.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from .. import model as m
from ..errors import ForgectlError
from ..identifiers import TypedId

#: Comment and discussion ids as the interface writes them: the tail is ``[A-Za-z0-9_=-]{1,100}``.
NODE = re.compile(r"[A-Za-z0-9_=-]{1,100}")
_LINE_BREAK = re.compile(r"\r\n|[\n\r]")
#: A search never walks more pages than this; a list longer than that is an incomplete search.
MAX_SEARCH_PAGES = 50
PAGE_SIZE = 100


@dataclass(frozen=True)
class Anchor:
    """What a reply goes to, found within the PR before anything is confirmed or sent."""

    to: TypedId
    threaded: bool  # false: the reply is a new general comment (GitHub ``ic`` and ``r`` ids)
    thread_id: str | None  # the thread the reply joins, as reported in ``thread_ids``
    host_id: str | None  # GitHub: the REST id of the root review comment; GitLab: discussion id


@dataclass(frozen=True)
class Posted:
    """The documented answer to one text write."""

    ids: tuple[str, ...]
    thread_ids: tuple[str, ...]
    # The backstop tripped (a 202, a non-empty ``commands_changes``, or a stored body that is not
    # the one sent): a quick action may have run. ``ids`` holds what landed, if anything did.
    quick_action: bool = False


class Poster(Protocol):
    def prepare(self, pr: m.PR) -> None:
        """Whatever the write needs read first (GitLab: the numeric project id and the MR's iid),
        so a failing read is an ordinary error before anything is audited or sent."""
        ...

    def find_anchor(self, pr: m.PR, to: TypedId) -> Anchor:
        """``reply --to`` looked up within ``pr``; absent or another PR's is ``not_found``."""
        ...

    def find_marker(self, pr: m.PR, account_id: str, marker: str) -> list[str]:
        """Ids of this account's comments whose last non-empty line is exactly ``marker``, on
        raw text. A search that could not finish is ``upstream_error``, never an empty list."""
        ...

    def post_comment(self, pr: m.PR, body: str) -> Posted:
        """One general comment. Never retried."""
        ...

    def post_reply(self, pr: m.PR, anchor: Anchor, body: str) -> Posted:
        """One reply to ``anchor``. Never retried."""
        ...


@dataclass(frozen=True)
class ThreadState:
    """A review thread as ``resolve`` and ``unresolve`` need it, read within the PR."""

    resolvable: bool
    resolved: bool  # never true for a thread that is not resolvable
    author_is_self: bool | None  # the thread's first comment is the account's; None: not known
    # whether any resolvable part is resolved; None: the same as ``resolved`` (GitHub). GitLab's
    # half-resolved discussion is resolved=False and any_resolved=True.
    any_resolved: bool | None = None

    def settled(self, want: bool) -> bool:
        """Already in the requested state: ``resolve`` needs every part resolved, ``unresolve``
        needs none."""
        if want:
            return self.resolved
        return not (self.resolved if self.any_resolved is None else self.any_resolved)


class ThreadPoster(Protocol):
    def prepare(self, pr: m.PR) -> None:
        """As for ``Poster``: whatever the write needs read first."""
        ...

    def read_thread(self, pr: m.PR, thread: TypedId, login: str, account_id: str) -> ThreadState:
        """The thread's state, within ``pr``; absent, of another host or another PR's is
        ``not_found``. ``login`` and ``account_id`` are the authenticated account's."""
        ...

    def set_resolved(self, pr: m.PR, thread: TypedId, resolved: bool) -> ThreadState:
        """The one write. Never retried. The answer must show the requested state, else
        ``outcome_unknown``: the write may have landed."""
        ...


def positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def created_id(data: Any, where: str) -> int:
    """The proof of a created comment or note: an object with an integer ``id``. Anything else
    after a 201 (a redirected write's answer reads as a list or an object with no ``id``) may
    still have landed, so it is ``outcome_unknown``."""
    value = positive_int(data.get("id")) if isinstance(data, dict) else None
    if value is None:
        raise ForgectlError(
            "outcome_unknown", f"{where}: the answer holds no comment id; the write may have landed"
        )
    return value


def last_line(text: str) -> str | None:
    """The last line of ``text`` that is not blank, exactly as written; None if there is none."""
    for line in reversed(_LINE_BREAK.split(text)):
        if line.strip():
            return line
    return None


def same_text(sent: str, stored: str) -> bool:
    """The comparison of a read-back: the whole body's outer whitespace and each line's trailing
    whitespace removed on both sides."""

    def norm(text: str) -> str:
        return "\n".join(line.rstrip() for line in _LINE_BREAK.split(text)).strip()

    return norm(sent) == norm(stored)


def collect(pages: Callable[[int], tuple[Iterable[Any], bool]], where: str) -> list[Any]:
    """Every item of a paginated list. ``pages(n)`` returns page ``n``'s items and whether more
    follow; a list that is still going after ``MAX_SEARCH_PAGES`` is ``upstream_error``."""
    items: list[Any] = []
    for number in range(1, MAX_SEARCH_PAGES + 1):
        page, more = pages(number)
        items.extend(page)
        if not more:
            return items
    raise ForgectlError(
        "upstream_error", f"{where}: more than {MAX_SEARCH_PAGES} pages; the search is incomplete"
    )
