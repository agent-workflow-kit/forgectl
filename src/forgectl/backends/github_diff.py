"""GitHub ``files``, ``diff`` and ``compare`` (REST), with no silent truncation.

Everything the host says is untrusted and checked here. Every cut a host makes is reported, never
passed off as a whole answer:

- ``files``: ``/pulls/N/files`` lists at most 3000 files, 100 a page. forgectl pages by itself,
  stops at ``MAX_FILE_PAGES`` and reports ``files_complete: false`` unless the list ended and its
  length equals the PR's ``changed_files``. A file whose ``patch`` the host omitted (binary,
  large, rename only) is kept, with ``patch`` ``None`` in ``RawFile`` (the data position
  validation, TODO(#25), needs: it treats such a file as invalid for inline comments).
- ``diff``: the PR's diff media type; a 406 (more than 300 files, or a diff the host will not
  produce) is ``too_large``. The head is read before and after; a moved head fetches once more,
  then ``upstream_error``.
- ``compare``: ``/compare/<from>...<to>``, status from the host cross-checked with its counts; the
  diff only on ``ahead``, by the diff media type; a 406, or as many ``diff --git`` headers as the
  host's file cap, is ``too_large``; a 404 or an unclear answer is ``unavailable``.

Diff text is decoded and sanitized with ``sanitize_bytes(..., diff=True)`` (a CR before LF stays,
invalid UTF-8 becomes U+FFFD); a path is sanitized as a one-line field. Repo, number and shas
reach an endpoint only through ``Gateway.api`` (``build_endpoint``); a link in a ``Link`` header
is never followed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import model as m
from ..errors import ForgectlError, refused
from ..gateway import Response, Target, verify_identity
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


# -- reading one PR ----------------------------------------------------------------------------


def _api(env: ReadEnv, target: Target, segments: list[str], **kwargs: Any) -> Response:
    gateway = env.gateway
    return env.call(lambda: gateway.api(target, "GET", segments, **kwargs))


def _pull_segments(ref: PrRef) -> list[str]:
    owner, name = _split(ref)
    if ref.number > MAX_PR_NUMBER:
        raise ForgectlError("not_found", "no such pull request")
    return ["repos", owner, name, "pulls", str(ref.number)]


def read_pull(env: ReadEnv, target: Target, ref: PrRef) -> Pull:
    response = _api(env, target, _pull_segments(ref), shape="object")
    try:
        pr = _obj(response.data, "the pull request")
        number = pr.get("number")
        if isinstance(number, bool) or number != ref.number:
            raise Malformed("the pull request number")
        head = _obj(pr.get("head"), "the head")
        base = _obj(pr.get("base"), "the base")
        project = _project(_obj(base.get("repo"), "the base repository").get("full_name"), "repo")
        head_sha = _req_sha(head.get("sha"), "the head sha")
        base_sha = _req_sha(base.get("sha"), "the base sha")
        changed = _count(pr.get("changed_files"))
    except Malformed as bad:
        raise malformed(f"{bad}") from None
    verify_identity(pr.get("html_url"), host=target.host, project=project, secret=target.token)
    return Pull(project=project, head_sha=head_sha, base_sha=base_sha, changed_files=changed)


def _stable[T](
    env: ReadEnv, target: Target, ref: PrRef, fetch: Callable[[Pull], T]
) -> tuple[Pull, T]:
    """``fetch`` pinned to a PR whose head and base did not move meanwhile: one more try, then
    ``upstream_error``."""
    for _ in range(2):
        before = read_pull(env, target, ref)
        value = fetch(before)
        after = read_pull(env, target, ref)
        if (before.head_sha, before.base_sha) == (after.head_sha, after.base_sha):
            return after, value
    raise ForgectlError(
        "upstream_error", "the pull request's head moved during the read, twice; try again"
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
        additions=_count(entry.get("additions")),
        deletions=_count(entry.get("deletions")),
        patch=patch,
    )


def has_next_page(response: Response) -> bool:
    link = response.headers.get("link")
    return bool(link) and _NEXT_LINK.search(link) is not None  # type: ignore[arg-type]


def list_files(env: ReadEnv, target: Target, ref: PrRef) -> RawFiles:
    """Every file the host lists, 100 a page, up to ``MAX_FILE_PAGES`` pages."""
    segments = [*_pull_segments(ref), "files"]
    files: list[RawFile] = []
    seen: set[str] = set()
    for page in range(1, MAX_FILE_PAGES + 1):
        response = _api(
            env,
            target,
            segments,
            query=[("per_page", str(PAGE_SIZE)), ("page", str(page))],
            shape="list",
        )
        try:
            items = _list(response.data, "the file list")
            if len(items) > PAGE_SIZE:
                raise Malformed("a page longer than the page size")
            for item in items:
                parsed = parse_file(item)
                if parsed.path in seen:  # a list that shifted under the read repeats a file
                    raise Malformed("a file listed twice")
                seen.add(parsed.path)
                files.append(parsed)
        except Malformed as bad:
            raise malformed(f"{bad}") from None
        if len(items) < PAGE_SIZE and not has_next_page(response):
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
    response = _api(
        env,
        target,
        ["repos", owner, name, "compare", f"{pull.base_sha}...{pull.head_sha}"],
        query=[("per_page", "1")],
        shape="object",
        passthrough=frozenset({404}),
    )
    if response.status == 404:
        return None
    try:
        found = _obj(response.data, "the comparison").get("merge_base_commit")
        if found is None:
            return None
        return _req_sha(_obj(found, "the merge base").get("sha"), "the merge base sha")
    except Malformed as bad:
        raise malformed(f"{bad}") from None


def _diff_text(env: ReadEnv, target: Target, ref: PrRef, pull: Pull) -> str:
    response = _api(
        env,
        target,
        _pull_segments(ref),
        headers=DIFF_ACCEPT,
        parse="bytes",
        passthrough=frozenset({406}),
    )
    if response.status == 406:
        raise _too_large("this diff")
    raw = response.data
    if not isinstance(raw, bytes) or (not raw.strip() and pull.changed_files):
        raise ForgectlError("upstream_error", "GitHub answered no diff for a PR with changes")
    return sanitize_bytes(raw, diff=True)


def diff_result(env: ReadEnv, ref: PrRef) -> m.DiffResult:
    target = env.hosts.target(ref.host)

    def fetch(pull: Pull) -> tuple[str, str | None]:
        return _diff_text(env, target, ref, pull), _merge_base(env, target, ref, pull)

    pull, (text, merge_base) = _stable(env, target, ref, fetch)
    return m.DiffResult(
        pr=f"{ref.host}/{pull.project}#{ref.number}",
        head_sha=pull.head_sha,
        base_sha=pull.base_sha,
        merge_base_sha=merge_base,
        diff=text,
    )


# -- compare -----------------------------------------------------------------------------------


def _unavailable(ref: PrRef, from_sha: str, to_sha: str) -> m.CompareResult:
    return m.CompareResult(
        pr=str(ref), from_sha=from_sha, to_sha=to_sha, status="unavailable", diff=None
    )


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


def compare_result(env: ReadEnv, ref: PrRef, from_sha: str, to_sha: str) -> m.CompareResult:
    if from_sha == to_sha:
        return m.CompareResult(
            pr=str(ref), from_sha=from_sha, to_sha=to_sha, status="identical", diff=""
        )
    target = env.hosts.target(ref.host)
    owner, name = _split(ref)
    segments = ["repos", owner, name, "compare", f"{from_sha}...{to_sha}"]
    response = _api(
        env,
        target,
        segments,
        query=[("per_page", "1")],
        shape="object",
        passthrough=frozenset({404}),
    )
    if response.status == 404:
        return _unavailable(ref, from_sha, to_sha)
    data = _obj_or_none(response.data)
    if data is None:
        return _unavailable(ref, from_sha, to_sha)
    verify_identity(data.get("html_url"), host=target.host, secret=target.token)
    status = _compare_status(data)
    if status == "unavailable":
        return _unavailable(ref, from_sha, to_sha)
    if status == "identical":
        return m.CompareResult(
            pr=str(ref), from_sha=from_sha, to_sha=to_sha, status=status, diff=""
        )
    if status != "ahead":  # behind, diverged: no diff, by the interface
        return m.CompareResult(
            pr=str(ref), from_sha=from_sha, to_sha=to_sha, status=status, diff=None
        )
    text = _api(
        env,
        target,
        segments,
        headers=DIFF_ACCEPT,
        parse="bytes",
        passthrough=frozenset({404, 406}),
    )
    if text.status == 404:
        return _unavailable(ref, from_sha, to_sha)
    if text.status == 406:
        raise _too_large("this comparison")
    raw = text.data
    if not isinstance(raw, bytes):
        raise ForgectlError("upstream_error", "GitHub answered no diff text")
    if len(_DIFF_HEADER.findall(raw)) >= COMPARE_FILE_CAP:  # the host may have cut the list here
        raise _too_large("this comparison")
    return m.CompareResult(
        pr=str(ref),
        from_sha=from_sha,
        to_sha=to_sha,
        status="ahead",
        diff=sanitize_bytes(raw, diff=True),
    )


def _obj_or_none(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None
