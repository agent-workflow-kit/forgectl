"""GitLab `snapshot`, `comments` and `reviews` (issue #16), and the push time of the head (#56).

``GitLabBackend`` only delegates here. Everything a host sends is untrusted: text is sanitized per
field, ids and logins are validated, nothing from a host's dict keys reaches the output, and an
answer that is not the documented shape is an `upstream_error`. A list the host or the page cap cut
off is named in `incomplete`, never silently short.

What is read, over the project id the merge request's own answer gives:

- `/reviewers` (request states) and `/notes` (the newest `requested review from` system note per
  reviewer: its id is the request's `instance`, its time `requested_at`);
- `/approvals` and the project's approval settings, through the backend's own `_reviewers`, so the
  table of `latest_by_reviewer` is built by the same code as in `show` and compared with `show`'s;
- `/discussions` (threads and general comments; system notes and drafts left out);
- `/versions`, else the head commit, for `head.pushed_at`.

The merge request is read once more after the parts: its head must still be the one `show` read.
A difference (head, identity, reviewer table, a repeated discussion) means the MR moved while it
was read; the whole reading is repeated once, then `upstream_error`.

Every GitLab behavior assumed here that no real host has confirmed is marked `UNVERIFIED`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from .. import model as m
from ..errors import ForgectlError
from ..gateway import Response, Target, path_int
from ..identifiers import PrRef
from . import gitlab as gl

if TYPE_CHECKING:
    from .gitlab import GitLabBackend

Now = Callable[[], datetime]

#: A host time later than the reading by more than this is not trusted (a clock skew allowance).
FUTURE_SKEW = timedelta(minutes=5)

_REQUEST_PREFIX = "requested review from "
_MAX_NOTE_SCAN = 4096  # characters of a note body parsed for logins
_MAX_TOKENS = 200
_LOGIN_TOKEN = re.compile(r"@([A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,253}[A-Za-z0-9_])?)")
_SEPARATOR = re.compile(r", and |, | and ")
_DISCUSSION_ID = re.compile(r"^[0-9a-f]{40}\Z")
_DIGITS = re.compile(r"[0-9]{1,18}")
_MAX_PATH = 4096
_MAX_LINE = 10_000_000
#: `/reviewers` states: `outstanding` for no review yet or a draft only, `reviewed` for the rest
#: (interface: Review requests). UNVERIFIED: the value names are from the documented enum.
_OUTSTANDING = frozenset({"unreviewed", "review_started"})
_REVIEWED = frozenset({"approved", "unapproved", "reviewed", "requested_changes"})


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Moved(Exception):
    """The merge request changed while it was being read."""


def _moved() -> ForgectlError:
    return ForgectlError(
        "upstream_error", "the merge request changed while it was being read; read it again"
    )


def _guarded[T](read: Callable[[], T]) -> T:
    try:
        return read()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        raise gl._bad("data") from None


def _twice[T](read: Callable[[], T]) -> T:
    """``read``, repeated once when the MR moved during it, then `upstream_error`."""
    for attempt in range(2):
        try:
            return _guarded(read)
        except _Moved:
            if attempt == 1:
                raise _moved() from None
    raise _moved()  # unreachable: the second attempt returns or raises


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


# -- rate limit --------------------------------------------------------------------------------


@dataclass
class Limits:
    """The lowest `ratelimit-remaining` seen across this module's own reads (UNVERIFIED header
    names: GitLab documents `RateLimit-Remaining` and `RateLimit-Reset`, an epoch time). Reads
    that go through `show` or the backend's table code are not counted.
    TODO(#9): count every read of the command once the gateway records response headers."""

    host: str
    remaining: int | None = None
    reset_at: str | None = None

    def note_headers(self, headers: Mapping[str, str]) -> None:
        left = headers.get("ratelimit-remaining", "")
        if not _DIGITS.fullmatch(left):
            return
        remaining = int(left)
        if self.remaining is not None and remaining >= self.remaining:
            return
        self.remaining = remaining
        reset = headers.get("ratelimit-reset", "")
        self.reset_at = None
        if _DIGITS.fullmatch(reset):
            try:
                self.reset_at = _stamp(datetime.fromtimestamp(int(reset), UTC))
            except (ValueError, OverflowError, OSError):
                self.reset_at = None

    def result(self) -> m.RateLimit:
        return m.RateLimit(host=self.host, remaining=self.remaining, reset_at=self.reset_at)


# -- the reader --------------------------------------------------------------------------------


@dataclass
class _Reader:
    backend: GitLabBackend
    target: Target
    ref: PrRef
    now: Now
    limits: Limits
    #: set by `read_mr`
    project_id: int = 0
    mr_id: int = 0
    canonical: str = ""
    head: str = ""

    def warn(self, message: str) -> None:
        self.backend._warn(message)

    def get(
        self,
        segments: list[str],
        *,
        query: tuple[tuple[str, str], ...] = (),
        shape: Any = "object",
        sub: bool = True,
    ) -> Response:
        response = self.backend._get(self.target, segments, query=query, shape=shape, sub=sub)
        self.limits.note_headers(response.headers)
        return response

    def base(self) -> list[str]:
        return [
            "projects",
            path_int(self.project_id),
            "merge_requests",
            path_int(self.ref.number),
        ]

    def read_mr(self) -> dict[str, Any]:
        """The MR's own answer, with the same identity checks as `show`."""
        number = self.ref.number
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not 1 <= number <= 9_999_999_999
        ):
            raise ForgectlError("invalid_pr", "the MR number is not usable")
        segments = ["projects", self.ref.project, "merge_requests", str(number)]
        mr = gl._obj(self.get(segments, sub=False).data, "merge request")
        canonical = self.backend._canonical_path(mr.get("web_url"), self.target, number)
        if gl._int_id(mr.get("iid"), "iid") != number:
            raise self.backend._wrong_host(
                self.target, "another merge request number", mr.get("web_url")
            )
        self.project_id = gl._int_id(mr.get("project_id"), "project_id")
        self.mr_id = gl._int_id(mr.get("id"), "id")
        self.canonical = canonical
        self.head = gl._sha(mr.get("sha"), "sha")
        return mr

    def expect(self, read: m.PrRead) -> None:
        """What `show` read must still be what this MR says."""
        pr = read.pr
        same = (
            pr.pr_id == f"gid://gitlab/MergeRequest/{self.mr_id}"
            and pr.repo == f"{self.ref.host}/{self.canonical}"
            and pr.head.sha == self.head
        )
        if not same:
            raise _Moved

    def check_head(self, head: str) -> None:
        """The head is still the one the parts were read for."""
        again = self.read_mr()
        if again.get("id") != self.mr_id or self.head != head:
            raise _Moved

    def pages(
        self,
        segments: list[str],
        query: tuple[tuple[str, str], ...] = (),
        stop: Callable[[list[dict[str, Any]]], bool] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Items of a paginated list and whether the list was read to its end (or `stop` ended
        it). The next page comes from `x-next-page`, which must be exactly the next number."""
        items: list[dict[str, Any]] = []
        page = 1
        for _ in range(gl.MAX_PAGES):
            response = self.get(
                segments,
                query=(*query, ("per_page", gl.PER_PAGE), ("page", str(page))),
                shape="list",
            )
            batch = [gl._obj(item, "list item") for item in gl._list(response.data, "list")]
            items.extend(batch)
            if stop is not None and stop(batch):
                return items, True
            header = response.headers.get("x-next-page")
            if header is not None:
                if header == "":
                    return items, True
                if header != str(page + 1):
                    raise gl._bad("pagination header")
            elif len(batch) < int(gl.PER_PAGE):
                return items, True
            page += 1
        return items, False


def _reader(backend: GitLabBackend, ref: PrRef, now: Now) -> _Reader:
    target = backend._hosts.target(ref.host)
    if target.backend != "gitlab":
        raise ForgectlError("internal", "the gitlab backend was given another backend's host")
    return _Reader(backend=backend, target=target, ref=ref, now=now, limits=Limits(ref.host))


# -- reviewer table ----------------------------------------------------------------------------


def _table_key(entry: m.LatestByReviewer) -> tuple[object, ...]:
    return (entry.login, entry.id, entry.state, entry.commit_sha, entry.can_push)


def _same_table(
    ours: tuple[m.LatestByReviewer, ...], shown: tuple[m.LatestByReviewer, ...]
) -> bool:
    """Whether two tables agree on every account, state and push right (the time is not
    compared: both come from the same `approved_by` rows, so a change shows in the others)."""
    return sorted(map(_table_key, ours), key=repr) == sorted(map(_table_key, shown), key=repr)


def _own_table(r: _Reader, author: m.Author) -> tuple[m.LatestByReviewer, ...]:
    """The table again, by the code `show` uses, from fresh reads."""
    entries, _left = r.backend._reviewers(r.target, r.project_id, r.ref.number)
    reset = r.backend._reset_on_push(r.target, r.project_id)
    return r.backend._with_can_push(r.target, r.project_id, entries, author, reset)


# -- review requests ---------------------------------------------------------------------------


def parse_requested(body: str) -> tuple[str, ...]:
    """The logins a `requested review from` note names: every `@login` up to the first word that
    is not a login, `,` or `and`. A token counts only when it is a whole word (followed by a
    separator or the end), so `@a!x` names nobody. Empty when the wording does not begin so."""
    text = body[:_MAX_NOTE_SCAN]
    if not text.startswith(_REQUEST_PREFIX):
        return ()
    pos = len(_REQUEST_PREFIX)
    found: list[str] = []
    while len(found) < _MAX_TOKENS:
        token = _LOGIN_TOKEN.match(text, pos)
        if token is None:
            break
        end = token.end()
        separator = _SEPARATOR.match(text, end)
        if end != len(text) and separator is None:
            break  # `@a!x`: the token is not a whole word
        found.append(token.group(1))
        if separator is None:
            break
        pos = separator.end()
    return tuple(found)


@dataclass(frozen=True)
class _Reviewer:
    user: gl._User
    state: str


@dataclass(frozen=True)
class RequestsRead:
    requests: tuple[m.ReviewRequest, ...]
    complete: bool


def _read_reviewers(r: _Reader) -> list[_Reviewer]:
    rows = gl._list(r.get([*r.base(), "reviewers"], shape="list").data, "reviewers")
    seen: dict[int, _Reviewer] = {}
    for item in rows:
        entry = gl._obj(item, "reviewer")
        user = gl._user(entry.get("user"), "reviewer")
        raw = entry.get("state")
        seen[user.id] = _Reviewer(user, raw if isinstance(raw, str) else "")
    return list(seen.values())


def _request_state(r: _Reader, raw: str) -> Literal["outstanding", "reviewed"]:
    if raw in _REVIEWED:
        return "reviewed"
    if raw not in _OUTSTANDING:
        r.warn("a reviewer state is not one forgectl knows; the request reads as outstanding")
    return "outstanding"


def _read_requests(r: _Reader, reviewers: list[_Reviewer]) -> RequestsRead:
    if not reviewers:
        return RequestsRead((), True)
    wanted = {m.ascii_lower(x.user.login) for x in reviewers}
    newest: dict[str, tuple[datetime, int, str]] = {}
    state = {"ordered": True, "last": None}

    def take(batch: list[dict[str, Any]]) -> bool:
        for note in batch:
            _scan_note(r, note, wanted, newest)
            at = gl._rfc3339(note.get("created_at"))
            if at is not None:
                when = _parse(at)
                last = state["last"]
                if isinstance(last, datetime) and when > last:
                    state["ordered"] = False
                state["last"] = when
        # Early stop only while the order is the requested newest-first one.
        return bool(state["ordered"]) and wanted <= set(newest)

    segments = [*r.base(), "notes"]
    _items, done = r.pages(segments, (("order_by", "created_at"), ("sort", "desc")), take)
    complete = done
    if not done:
        r.warn("the review request notes hit the page cap; review_requests is incomplete")
    requests: list[m.ReviewRequest] = []
    unmatched = 0
    for x in reviewers:
        hit = newest.get(m.ascii_lower(x.user.login))
        if hit is None:
            unmatched += 1
        requests.append(
            m.ReviewRequest(
                reviewer=m.Reviewer(login=x.user.login, id=str(x.user.id), kind="user"),
                state=_request_state(r, x.state),
                code_owner=None,
                instance=None if hit is None else str(hit[1]),
                requested_at=None if hit is None else hit[2],
            )
        )
    if unmatched and done:
        r.warn(f"no review request note matches {unmatched} reviewer(s); their instance is null")
    return RequestsRead(tuple(requests), complete)


def _scan_note(
    r: _Reader,
    note: Mapping[str, Any],
    wanted: set[str],
    newest: dict[str, tuple[datetime, int, str]],
) -> None:
    """Record a system note's request for each wanted reviewer it names (newest per reviewer).
    A note from a user is never a request, whatever its wording."""
    body = note.get("body")
    if note.get("system") is not True or not isinstance(body, str):
        return
    logins = parse_requested(body)
    if not logins:
        return
    _same_mr(r, note)
    at = gl._rfc3339(note.get("created_at"))
    note_id = note.get("id")
    if at is None or isinstance(note_id, bool) or not isinstance(note_id, int) or note_id < 1:
        r.warn("a review request note has no usable id or time and is skipped")
        return
    when = _parse(at)
    for login in logins:
        key = m.ascii_lower(login)
        if key not in wanted:
            continue
        current = newest.get(key)
        if current is None or (when, note_id) > (current[0], current[1]):
            newest[key] = (when, note_id, at)


def _same_mr(r: _Reader, note: Mapping[str, Any]) -> None:
    """A note that names its noteable must name this MR (a mismatch is another host's answer)."""
    if "noteable_id" in note and note["noteable_id"] != r.mr_id:
        raise r.backend._wrong_host(r.target, "another merge request's note", None)
    if "noteable_type" in note and note["noteable_type"] != "MergeRequest":
        raise r.backend._wrong_host(r.target, "a note of another kind of object", None)


# -- threads -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreadsRead:
    threads: tuple[m.Thread, ...]
    complete: bool


def _positive(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_LINE:
        return None
    return value


def _comment_id(note_id: int) -> str:
    return f"gl:n:{note_id}"


def _note_int(raw: Any) -> int:
    return gl._int_id(raw, "note id")


def _author(r: _Reader, raw: Any) -> m.Author:
    user = gl._user(raw, "note author")
    return m.Author(login=user.login, id=str(user.id), is_bot=r.backend._is_bot(r.target, user))


def _posted(r: _Reader, raw: Any) -> list[dict[str, Any]]:
    """The notes of a discussion that are posted comments: no system note, no draft."""
    out: list[dict[str, Any]] = []
    for item in gl._list(raw, "notes"):
        note = gl._obj(item, "note")
        _same_mr(r, note)
        if note.get("system") is True or note.get("draft") is True:
            continue
        out.append(note)
    return out


def _position(
    raw: Any,
) -> tuple[str | None, int | None, Literal["left", "right"] | None, int | None, str | None]:
    """(path, line, side, start_line, commit_sha) of a diff note's position; all null without
    one. A file-level position has a path and no line."""
    if raw is None:
        return None, None, None, None, None
    pos = gl._obj(raw, "position")
    path = None
    for key in ("new_path", "old_path"):
        value = pos.get(key)
        if isinstance(value, str) and value:
            path = gl._line(value)[:_MAX_PATH] or None
            break
    sha = pos.get("head_sha")
    commit_sha = sha if isinstance(sha, str) and gl._SHA.match(sha) else None
    kind = pos.get("position_type")
    if kind in ("file", "image"):
        return path, None, None, None, commit_sha
    new_line, old_line = _positive(pos.get("new_line")), _positive(pos.get("old_line"))
    line, side, key = None, None, ""
    if new_line is not None:
        line, side, key = new_line, "right", "new_line"
    elif old_line is not None:
        line, side, key = old_line, "left", "old_line"
    start = None
    span = pos.get("line_range")
    if line is not None and isinstance(span, dict) and isinstance(span.get("start"), dict):
        candidate = _positive(span["start"].get(key))
        if candidate is not None and candidate < line:
            start = candidate
    return path, line, side, start, commit_sha  # type: ignore[return-value]


def _thread(r: _Reader, raw: dict[str, Any]) -> m.Thread | None:
    discussion = raw.get("id")
    if not isinstance(discussion, str) or not _DISCUSSION_ID.match(discussion):
        raise gl._bad("discussion id")
    notes = gl._list(raw.get("notes"), "notes")
    if not notes:
        r.warn("a discussion has no notes and is left out")
        return None
    posted = _posted(r, notes)
    if not posted:
        return None  # only system notes (or drafts): nothing was posted
    keyed = sorted(
        (
            (gl._required_time(n.get("created_at"), "created_at"), _note_int(n.get("id")), n)
            for n in posted
        ),
        key=lambda item: (item[0], item[1]),
    )
    ids = [item[1] for item in keyed]
    if len(set(ids)) != len(ids):
        raise _Moved  # a note listed twice: the list shifted while it was read
    first = keyed[0][2]
    individual = raw.get("individual_note") is True
    resolvable = (not individual) and any(n.get("resolvable") is True for n in posted)
    flags = [n.get("resolved") is True for n in posted if n.get("resolvable") is True]
    resolved = resolvable and bool(flags) and all(flags)
    path, line, side, start, commit_sha = _position(first.get("position"))
    root = _comment_id(keyed[0][1])
    comments: list[m.Comment] = []
    for index, (created, note_id, note) in enumerate(keyed):
        body = gl._text(note.get("body"), "note body", line=False)
        assert body is not None
        updated = gl._rfc3339(note.get("updated_at")) or created
        comments.append(
            m.Comment(
                id=_comment_id(note_id),
                author=_author(r, note.get("author")),
                body=body,
                created_at=created,
                updated_at=updated,
                minimized=False,
                reply_to=None if index == 0 else root,
                review_id=None,
            )
        )
    return m.Thread(
        id=f"gl:d:{discussion}",
        resolvable=resolvable,
        resolved=resolved,
        outdated=None,
        path=path,
        line=line,
        side=side,
        start_line=start,
        commit_sha=commit_sha,
        comments=tuple(comments),
    )


def _read_threads(r: _Reader) -> ThreadsRead:
    rows, done = r.pages([*r.base(), "discussions"])
    threads: list[m.Thread] = []
    seen: set[str] = set()
    for raw in rows:
        found = _thread(r, raw)
        if found is None:
            continue
        if found.id in seen:
            raise _Moved  # the same discussion on two pages: the list shifted while it was read
        seen.add(found.id)
        threads.append(found)
    if not done:
        r.warn("the discussions hit the page cap; threads is incomplete")
    return ThreadsRead(tuple(threads), done)


# -- the head's push time ----------------------------------------------------------------------


def _version_time(r: _Reader, head: str) -> tuple[datetime, str] | None:
    """The newest `created_at` of the versions whose head is `head`, or None when there is none
    or the versions cannot be read (unreadable is a fallback, a malformed answer a warning)."""
    try:
        response = r.get([*r.base(), "versions"], query=(("per_page", gl.PER_PAGE),), shape="list")
    except ForgectlError as error:
        if error.code in gl._NEVER_DEGRADED:
            raise
        if not gl._part_missing(error):
            r.warn("the versions of the merge request could not be read")
        return None
    best: tuple[datetime, str] | None = None
    try:
        for item in gl._list(response.data, "versions"):
            row = gl._obj(item, "version")
            if "merge_request_id" in row and row["merge_request_id"] != r.mr_id:
                raise r.backend._wrong_host(r.target, "another merge request's version", None)
            sha = gl._sha(row.get("head_commit_sha"), "version head")
            created = gl._required_time(row.get("created_at"), "version time")
            if sha != head:
                continue
            when = _parse(created)
            if best is None or when > best[0]:
                best = (when, created)
    except ForgectlError as error:
        if error.code in gl._NEVER_DEGRADED:
            raise
        r.warn("the versions of the merge request are not readable; the commit date is used")
        return None
    return best


def _commit_time(r: _Reader, head: str) -> tuple[datetime, str] | None:
    try:
        data = gl._obj(
            r.get(["projects", path_int(r.project_id), "repository", "commits", head]).data,
            "head commit",
        )
        if data.get("id") != head:
            raise gl._bad("head commit id")
        stamp = gl._required_time(data.get("committed_date"), "commit date")
    except ForgectlError as error:
        if error.code in gl._NEVER_DEGRADED:
            raise
        r.warn("the head commit date could not be read")
        return None
    return _parse(stamp), stamp


def read_head_push(r: _Reader, head: str) -> m.PushTime | None:
    """`push_event`: the newest version created for this head; else `commit_date`: the head
    commit's date. A time more than `FUTURE_SKEW` after the reading is not trusted, and neither
    answer failing the snapshot: unknown is null with a warning."""
    found = _version_time(r, head)
    source: Literal["push_event", "commit_date"] = "push_event"
    if found is None:
        found = _commit_time(r, head)
        source = "commit_date"
    if found is None:
        return None
    when, stamp = found
    if when > r.now() + FUTURE_SKEW:
        r.warn("the push time of the head lies in the future and is not trusted")
        return None
    return m.PushTime(at=stamp, source=source)


# -- the commands ------------------------------------------------------------------------------


def snapshot(backend: GitLabBackend, ref: PrRef, now: Now | None = None) -> m.Snapshot:
    clock = now or utc_now

    def once() -> m.Snapshot:
        read = backend.get_pr(ref)
        r = _reader(backend, ref, clock)
        r.read_mr()
        r.expect(read)
        head = read.pr.head.sha
        reviewers = _read_reviewers(r)
        if not _same_table(_own_table(r, read.pr.author), read.latest_by_reviewer):
            raise _Moved
        requests = _read_requests(r, reviewers)
        threads = _read_threads(r)
        head_push = read_head_push(r, head)
        r.check_head(head)
        incomplete: list[str] = []
        if not requests.complete:
            incomplete.append("review_requests")
        if not threads.complete:
            incomplete.append("threads")
        # TODO(#57): the derived `activity` block (others' comments since the last push).
        return m.Snapshot(
            pr=read.pr,
            review_requests=requests.requests,
            teams_visible=True,  # GitLab has no team reviewers: an absent team means nothing
            reviews=(),
            latest_by_reviewer=read.latest_by_reviewer,
            threads=threads.threads,
            ci=read.ci,
            rate_limit=r.limits.result(),
            fetched_at=_stamp(clock()),
            incomplete=tuple(incomplete),
            head_push=head_push,
        )

    return _twice(once)


def comments(backend: GitLabBackend, ref: PrRef, now: Now | None = None) -> m.CommentsResult:
    clock = now or utc_now

    def once() -> m.CommentsResult:
        r = _reader(backend, ref, clock)
        r.read_mr()
        head = r.head
        threads = _read_threads(r)
        r.check_head(head)
        return m.CommentsResult(
            pr=f"{ref.host}/{r.canonical}#{ref.number}",
            head_sha=head,
            threads=threads.threads,
            complete=threads.complete,
        )

    return _twice(once)


def reviews(backend: GitLabBackend, ref: PrRef, now: Now | None = None) -> m.ReviewsResult:
    """GitLab has no review objects: `reviews` is empty by decision (D-25), and the table is
    `show`'s, confirmed by a second reading."""
    clock = now or utc_now

    def once() -> m.ReviewsResult:
        read = backend.get_pr(ref)
        r = _reader(backend, ref, clock)
        r.read_mr()
        r.expect(read)
        table = _own_table(r, read.pr.author)
        if not _same_table(table, read.latest_by_reviewer):
            raise _Moved
        r.check_head(read.pr.head.sha)
        return m.ReviewsResult(
            pr=read.pr.pr,
            head_sha=read.pr.head.sha,
            reviews=(),
            latest_by_reviewer=table,
            review_decision=read.pr.review_decision,
            approvals=read.pr.approvals,
            approvals_at_head=read.pr.approvals_at_head,
            complete=True,
        )

    return _twice(once)
