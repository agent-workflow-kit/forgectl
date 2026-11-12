"""GitHub ``files``, ``diff`` and ``compare`` (REST), with no silent truncation.

Everything the host says is untrusted and checked here. Every cut a host makes is reported, never
passed off as a whole answer:

- ``files``: ``/pulls/N/files`` lists at most 3000 files, 100 a page. forgectl pages by itself,
  stops at ``MAX_FILE_PAGES`` and reports ``files_complete: false`` unless the list ended and its
  length equals the PR's ``changed_files``. A file whose ``patch`` the host omitted (binary,
  large, rename only) is kept, with ``patch`` ``None`` in ``RawFile`` (the data position
  validation, TODO(#25), needs: it treats such a file as invalid for inline comments).
- ``diff``: the PR's diff media type; a 406 (more than 300 files, or a diff the host will not
  produce), or an answer past forgectl's read cap, is ``too_large``. Its ``diff --git`` headers
  must number the PR's ``changed_files``. The PR is read before and after; a moved head, base or
  project (or a file list that shifted) fetches once more, then ``upstream_error``.
- ``compare``: reads the PR first (``not_found``, and the canonical path), then
  ``/compare/<from>...<to>``, status from the host cross-checked with its counts; the diff only on
  ``ahead``, by the diff media type; a 406, a cut answer, a listed file count at the host's cap
  or as many ``diff --git`` headers is ``too_large``; a 404, an unclear answer or a header count
  other than the listed count is ``unavailable``.

Every answer is checked inside the call ``ReadEnv.call`` retries, so a malformed one is retried
like a 5xx (interface: Reliability).

Diff text is decoded and sanitized with ``sanitize_bytes(..., diff=True)`` (a CR before LF stays,
invalid UTF-8 becomes U+FFFD); a path is sanitized as a one-line field. Repo, number and shas
reach an endpoint only through ``Gateway.api`` (``build_endpoint``); a link in a ``Link`` header
is never followed.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import model as m
from ..errors import ForgectlError, refused
from ..gateway import AnswerCut, Response, Target, shown_text, verify_identity
from ..identifiers import PrRef
from ..reads import ReadEnv
from ..sanitize import sanitize_bytes, sanitize_line
from .github import (
    MAX_PR_NUMBER,
    Malformed,
    _count,
    _list,
    _obj,
    _project,
    _req_sha,
    _split,
    _str,
    _url_path,
    malformed,
)

PAGE_SIZE = 100
#: 3000 files at 100 a page is 30 pages; one more page confirms the end of a full list.
MAX_FILE_PAGES = 31
#: GitHub's cap on the files of a diff; a compare diff with this many files may be cut.
COMPARE_FILE_CAP = 300
MAX_PATH = 4096
DIFF_ACCEPT = {"Accept": "application/vnd.github.diff"}

#: GitHub's file statuses, mapped to the interface's four.
FILE_STATUS: dict[str, m.FileStatus] = {
    "added": "added",
    "removed": "deleted",
    "modified": "modified",
    "renamed": "renamed",
    "copied": "added",
    "changed": "modified",
    "unchanged": "modified",
}

_NEXT_LINK = re.compile(r'<[^>]*>\s*;[^,<]*\brel\s*=\s*"?(?:[^",;]*\s)?next\b', re.IGNORECASE)
_DIFF_HEADER = re.compile(rb"(?m)^diff --git ")


# -- the data other issues reuse ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawFile:
    """One listed file as the host gave it, checked but not sanitized: for position validation
    (TODO(#25)) and the GitLab twin (TODO(#18)). ``path`` and ``patch`` are untrusted raw text;
    never print them. ``patch`` is None when the host omitted it."""

    path: str
    previous_path: str | None
    status: m.FileStatus
    additions: int | None
    deletions: int | None
    patch: str | None

    def change(self) -> m.FileChange:
        return m.FileChange(
            path=sanitize_line(self.path),
            previous_path=None if self.previous_path is None else sanitize_line(self.previous_path),
            status=self.status,
            additions=self.additions,
            deletions=self.deletions,
        )


@dataclass(frozen=True, slots=True)
class RawFiles:
    files: tuple[RawFile, ...]
    ended: bool  # pagination reached the host's end of the list
    reason: str | None  # why it did not, as a fixed phrase


@dataclass(frozen=True, slots=True)
class Pull:
    """The facts of the PR that a read is pinned to."""

    project: str  # the canonical `owner/repo`, from the host (a rename is accepted)
    head_sha: str
    base_sha: str
    changed_files: int | None


# -- reading through the gateway ---------------------------------------------------------------


def _get[T](
    env: ReadEnv,
    target: Target,
    segments: list[str],
    check: Callable[[Response], T],
    *,
    cut: str | None = None,
    **kwargs: Any,
) -> T:
    """One GET, retried by ``env.call``; ``check`` validates the answer inside the retried call,
    so an answer that is not the documented shape is retried like a 5xx (interface:
    Reliability). With ``cut`` (a diff), an answer past forgectl's read cap is ``too_large``,
    never retried: the cut is forgectl's own and a partial diff is never shown."""
    gateway = env.gateway

    def once() -> T:
        try:
            response = gateway.api(target, "GET", segments, **kwargs)
        except AnswerCut:
            if cut is None:
                raise
            raise refused(
                "too_large",
                f"{cut} is larger than forgectl's read cap; a partial diff would mislead",
            ) from None
        try:
            return check(response)
        except Malformed as bad:
            raise malformed(f"{bad}") from None

    return env.call(once)


def _compare_identity(url: object, target: Target, project: str) -> None:
    """A comparison's ``html_url`` must be this host's, with the path exactly
    ``/{project}/compare/<one segment>`` (no percent escape, no dot segment)."""
    verify_identity(url, host=target.host, project=project, secret=target.token)
    path = urllib.parse.urlsplit(url).path if isinstance(url, str) else ""  # it is, once accepted
    prefix = f"/{project}/compare/"
    rest = path[len(prefix) :].removesuffix("/") if path.startswith(prefix) else ""
    if "%" in path or not rest or "/" in rest or rest in (".", ".."):
        raise ForgectlError(
            "wrong_host_response",
            f"the answer does not belong to {target.host} and {project}: the token may have"
            " reached another host and should be rotated",
            details={"observed": shown_text(str(url), target.token)},
        )


# -- reading one PR ----------------------------------------------------------------------------


class _Moved(Exception):
    """The PR's files shifted under the read (a file on two pages, or a diff whose file count is
    not the PR's ``changed_files``): read it all once more."""


def _pull_segments(ref: PrRef) -> list[str]:
    owner, name = _split(ref)
    if ref.number > MAX_PR_NUMBER:
        raise ForgectlError("not_found", "no such pull request")
    return ["repos", owner, name, "pulls", str(ref.number)]


def read_pull(
    env: ReadEnv, target: Target, ref: PrRef, *, need_changed_files: bool = False
) -> Pull:
    """The PR, whose ``html_url`` must be exactly this PR's on this host (a rename is accepted,
    as for ``show``). ``need_changed_files``: a missing or invalid count is malformed."""

    def check(response: Response) -> Pull:
        pr = _obj(response.data, "the pull request")
        number = pr.get("number")
        if isinstance(number, bool) or number != ref.number:
            raise Malformed("the pull request number")
        head = _obj(pr.get("head"), "the head")
        base = _obj(pr.get("base"), "the base")
        project = _project(_obj(base.get("repo"), "the base repository").get("full_name"), "repo")
        verify_identity(pr.get("html_url"), host=target.host, project=project, secret=target.token)
        _url_path(pr.get("html_url"), project, f"/pull/{ref.number}")
        head_sha = _req_sha(head.get("sha"), "the head sha")
        base_sha = _req_sha(base.get("sha"), "the base sha")
        changed = _count(pr.get("changed_files"))
        if changed is None and need_changed_files:
            raise Malformed("the changed file count")
        return Pull(project=project, head_sha=head_sha, base_sha=base_sha, changed_files=changed)

    return _get(env, target, _pull_segments(ref), check, shape="object")


def _stable[T](
    env: ReadEnv,
    target: Target,
    ref: PrRef,
    fetch: Callable[[Pull], T],
    *,
    need_changed_files: bool = False,
) -> tuple[Pull, T]:
    """``fetch`` pinned to a PR whose project, head and base did not move meanwhile, and whose
    file list did not shift under the read: one more try, then ``upstream_error``."""
    for _ in range(2):
        before = read_pull(env, target, ref, need_changed_files=need_changed_files)
        try:
            value = fetch(before)
        except _Moved:
            continue
        after = read_pull(env, target, ref, need_changed_files=need_changed_files)
        if (before.project, before.head_sha, before.base_sha) == (
            after.project,
            after.head_sha,
            after.base_sha,
        ):
            return after, value
    raise ForgectlError(
        "upstream_error", "the pull request moved during the read, twice; try again"
    )


# -- files -------------------------------------------------------------------------------------


def _path(value: object, what: str) -> str:
    """A repository path from the host: git's own rules (no empty, ``.`` or ``..`` component, not
    absolute, no NUL), a length cap. A newline, a leading ``-`` or bidi text are legal and stay,
    sanitized when printed."""
    text = _str(value, what)
    if not text or len(text) > MAX_PATH or "\x00" in text or text.startswith("/"):
        raise Malformed(what)
    if any(part in ("", ".", "..") for part in text.split("/")):
        raise Malformed(what)
    return text


def _req_count(value: object, what: str) -> int:
    count = _count(value)
    if count is None:
        raise Malformed(what)
    return count


def parse_file(item: object) -> RawFile:
    entry = _obj(item, "a file entry")
    path = _path(entry.get("filename"), "a file name")
    status = FILE_STATUS.get(_str(entry.get("status"), "a file status"))
    if status is None:
        raise Malformed("a file status")
    previous = (
        _path(entry.get("previous_filename"), "a previous file name")
        if (status == "renamed")
        else None
    )
    patch = entry.get("patch")
    if patch is not None and not isinstance(patch, str):
        raise Malformed("a patch")
    return RawFile(
        path=path,
        previous_path=previous,
        status=status,
        additions=_req_count(entry.get("additions"), "a file's additions"),
        deletions=_req_count(entry.get("deletions"), "a file's deletions"),
        patch=patch,
    )


def has_next_page(response: Response) -> bool:
    link = response.headers.get("link")
    if not link:
        return False
    return _NEXT_LINK.search(link) is not None


def _file_page(response: Response) -> tuple[list[RawFile], bool]:
    """One page, checked whole; and whether it ends the list."""
    items = _list(response.data, "the file list")
    if len(items) > PAGE_SIZE:
        raise Malformed("a page longer than the page size")
    parsed = [parse_file(item) for item in items]
    if len({f.path for f in parsed}) != len(parsed):
        raise Malformed("a page that lists a file twice")
    return parsed, len(items) < PAGE_SIZE and not has_next_page(response)


def list_files(env: ReadEnv, target: Target, ref: PrRef) -> RawFiles:
    """Every file the host lists, 100 a page, up to ``MAX_FILE_PAGES`` pages. A file on two
    pages is a list that shifted under the read (``_Moved``)."""
    segments = [*_pull_segments(ref), "files"]
    files: list[RawFile] = []
    seen: set[str] = set()
    for page in range(1, MAX_FILE_PAGES + 1):
        parsed, last = _get(
            env,
            target,
            segments,
            _file_page,
            query=[("per_page", str(PAGE_SIZE)), ("page", str(page))],
            shape="list",
        )
        for found in parsed:
            if found.path in seen:
                raise _Moved
            seen.add(found.path)
            files.append(found)
        if last:
            return RawFiles(tuple(files), ended=True, reason=None)
    return RawFiles(tuple(files), ended=False, reason="the host's list went past the page limit")


def files_result(env: ReadEnv, ref: PrRef) -> m.FilesResult:
    target = env.hosts.target(ref.host)
    pull, raw = _stable(env, target, ref, lambda _pull: list_files(env, target, ref))
    complete = raw.ended and pull.changed_files is not None and len(raw.files) == pull.changed_files
    if not complete:
        env.warn(raw.reason or "files: the listed files do not add up to the PR's changed_files")
    return m.FilesResult(
        pr=f"{ref.host}/{pull.project}#{ref.number}",
        head_sha=pull.head_sha,
        files=tuple(f.change() for f in raw.files),
        files_complete=complete,
        changed_files=pull.changed_files,
    )


# -- diff --------------------------------------------------------------------------------------


def _too_large(what: str) -> ForgectlError:
    return refused(
        "too_large", f"GitHub will not produce {what} whole; a partial diff would mislead"
    )


def _merge_base(env: ReadEnv, target: Target, ref: PrRef, pull: Pull) -> str | None:
    owner, name = _split(ref)

    def check(response: Response) -> str | None:
        if response.status == 404:
            return None
        data = _obj(response.data, "the comparison")
        _compare_identity(data.get("html_url"), target, pull.project)
        found = data.get("merge_base_commit")
        if found is None:
            return None
        return _req_sha(_obj(found, "the merge base").get("sha"), "the merge base sha")

    return _get(
        env,
        target,
        ["repos", owner, name, "compare", f"{pull.base_sha}...{pull.head_sha}"],
        check,
        query=[("per_page", "1")],
        shape="object",
        passthrough=frozenset({404}),
    )


def _diff_text(env: ReadEnv, target: Target, ref: PrRef) -> tuple[bytes, int]:
    """The raw diff and its file header count. The count is compared with the PR's
    ``changed_files`` by the caller, outside the retried call: a mismatch is a PR that moved."""

    def check(response: Response) -> tuple[bytes, int]:
        if response.status == 406:
            raise _too_large("this diff")
        raw = response.data
        if not isinstance(raw, bytes):
            raise Malformed("no diff text")
        return raw, len(_DIFF_HEADER.findall(raw))

    return _get(
        env,
        target,
        _pull_segments(ref),
        check,
        cut="this diff",
        headers=DIFF_ACCEPT,
        parse="bytes",
        passthrough=frozenset({406}),
    )


def diff_result(env: ReadEnv, ref: PrRef) -> m.DiffResult:
    target = env.hosts.target(ref.host)

    def fetch(pull: Pull) -> tuple[str, str | None]:
        raw, count = _diff_text(env, target, ref)
        if count != pull.changed_files:
            raise _Moved  # a push mid-read; a second mismatch ends upstream_error
        return sanitize_bytes(raw, diff=True), _merge_base(env, target, ref, pull)

    pull, (text, merge_base) = _stable(env, target, ref, fetch, need_changed_files=True)
    return m.DiffResult(
        pr=f"{ref.host}/{pull.project}#{ref.number}",
        head_sha=pull.head_sha,
        base_sha=pull.base_sha,
        merge_base_sha=merge_base,
        diff=text,
    )


# -- compare -----------------------------------------------------------------------------------


def _compare_status(data: dict[str, Any]) -> m.CompareStatus:
    """The host's status, accepted only when its own counts agree; anything else is unclear."""
    status = data.get("status")
    ahead, behind = _count(data.get("ahead_by")), _count(data.get("behind_by"))
    if ahead is None or behind is None:
        return "unavailable"
    if status == "identical" and ahead == 0 and behind == 0:
        return "identical"
    if status == "ahead" and ahead > 0 and behind == 0:
        return "ahead"
    if status == "behind" and ahead == 0 and behind > 0:
        return "behind"
    if status == "diverged" and ahead > 0 and behind > 0:
        return "diverged"
    return "unavailable"


def compare_result(
    env: ReadEnv, ref: PrRef, from_sha: str, to_sha: str, *, no_diff: bool = False
) -> m.CompareResult:
    """``no_diff`` (#44) answers from the status request alone: the diff is never requested, so
    it can neither be read nor be cut, and the result has no ``diff`` key."""
    target = env.hosts.target(ref.host)
    pull = read_pull(env, target, ref)  # a missing PR is not_found; the canonical path
    pr = f"{ref.host}/{pull.project}#{ref.number}"

    def result(status: m.CompareStatus, diff: str | None) -> m.CompareResult:
        return m.CompareResult(
            pr=pr,
            from_sha=from_sha,
            to_sha=to_sha,
            status=status,
            diff=None if no_diff else diff,
            omit_diff=no_diff,
        )

    if from_sha == to_sha:
        return result("identical", "")
    owner, name = _split(ref)
    segments = ["repos", owner, name, "compare", f"{from_sha}...{to_sha}"]

    def check(response: Response) -> tuple[m.CompareStatus, int | None] | None:
        if response.status == 404:
            return None
        data = _obj(response.data, "the comparison")
        _compare_identity(data.get("html_url"), target, pull.project)
        status = _compare_status(data)
        listed = None
        if not no_diff and (status == "ahead" or "files" in data):
            listed = len(_list(data.get("files"), "the listed files"))
        return status, listed

    answer = _get(
        env,
        target,
        segments,
        check,
        query=[("per_page", "1")],
        shape="object",
        passthrough=frozenset({404}),
    )
    if answer is None:
        return result("unavailable", None)
    status, listed = answer
    if status == "identical":
        return result(status, "")
    if status != "ahead" or listed is None:  # behind, diverged: no diff, by the interface
        return result(status, None)
    if no_diff:  # the status is the answer; the diff, and so its caps, are not asked about
        return result("ahead", None)
    if listed >= COMPARE_FILE_CAP:  # the host lists at most this many: the list may be cut
        raise _too_large("this comparison")

    def check_diff(response: Response) -> bytes | None:
        if response.status == 404:
            return None
        if response.status == 406:
            raise _too_large("this comparison")
        if not isinstance(response.data, bytes):
            raise Malformed("no diff text")
        return response.data

    raw = _get(
        env,
        target,
        segments,
        check_diff,
        cut="this comparison",
        headers=DIFF_ACCEPT,
        parse="bytes",
        passthrough=frozenset({404, 406}),
    )
    if raw is None:
        return result("unavailable", None)
    found = len(_DIFF_HEADER.findall(raw))
    if found >= COMPARE_FILE_CAP:  # a backstop: the host may have cut the diff here
        raise _too_large("this comparison")
    if found != listed:  # an unclear answer, never a partial diff
        return result("unavailable", None)
    return result("ahead", sanitize_bytes(raw, diff=True))
