"""GitLab ``files``, ``diff`` and ``compare`` (REST), with no silent truncation.

The twin of ``github_diff``. Everything the host says is untrusted and checked here; every cut a
host makes is reported, never passed off as a whole answer. UNVERIFIED host behavior (field names,
flags, headers) is marked ``UNVERIFIED``; the live suites (#31, #35) settle it.

- ``/merge_requests/:iid/diffs`` (15.7) lists one entry per file, 100 a page, with ``old_path``,
  ``new_path``, ``a_mode``, ``b_mode``, ``diff`` (one fragment, hunks only) and the flags
  ``new_file``, ``renamed_file``, ``deleted_file`` (and, UNVERIFIED, ``too_large``, ``collapsed``,
  ``generated_file``). forgectl pages by ``x-next-page`` (exactly page+1 or empty), stops at
  ``MAX_FILE_PAGES`` and reports the list incomplete if it did not end.
- A file is *cut* when ``too_large`` or ``collapsed`` is true, when it is generated with no
  fragment, when it is ``modified`` with an empty fragment and no path or mode change (what a
  binary file may look like, UNVERIFIED), when it has a ``Binary files ... differ`` notice and is
  neither added nor deleted, or when its last hunk ends before its header's line counts (a
  fragment cut mid-hunk). ``files`` lists a cut file with null counts; ``diff`` and ``compare``
  refuse (``too_large``): a partial diff would mislead a reviewer.
- ``diff`` assembles the text from the fragments, with the header lines forgectl writes (see
  ``render_file``): ``diff --git``, then ``new file mode`` or ``deleted file mode`` or ``old mode``
  and ``new mode``, then ``rename from`` and ``rename to``, then ``---`` and ``+++`` (``/dev/null``
  for the missing side, and a TAB after a path with a space, as git writes it) when the file has
  a fragment, then the fragment. There is no ``index`` line (no blob shas), no ``similarity
  index`` (unknown) and no ``Binary files differ`` line (an added or deleted file's binary notice
  is written as headers only). Paths are quoted as git quotes them
  (``core.quotePath`` on): a path with a quote, backslash, control character or non-ASCII byte is
  in double quotes with C escapes and octal bytes, so a path can never forge a header line.
- Every fragment is checked hunk by hunk against its header's line counts and git's hunk grammar
  (no empty hunk, hunks in order and apart, an added file's old range ``-0,0`` and a deleted
  file's new range ``+0,0``, only ``\\ No newline at end of file``, once a side, after a line): any
  other line is malformed (``upstream_error``), so fragment text can never open a file section of
  its own. A path appears once, except a type change (file to symlink and back): one deleted
  entry and one added entry, written deleted first.
- The MR is read before and after (``_stable``): project, head, ``diff_refs`` base, head and start
  must not move, and ``diff_refs.head_sha`` must be the MR's head; one more try, then
  ``upstream_error``.
- ``compare`` reads the MR's identity first (``not_found``, the canonical path; not its
  ``diff_refs``), then ``/repository/compare``
  in the target project with ``straight=false``, both ways. Commits only forward is ``ahead``,
  only backward ``behind``, both ``diverged``; ``identical`` is equal shas or both answers' own
  ``compare_same_ref``; anything else is ``unavailable``. ``compare_timeout`` is ``too_large``. An
  answer past forgectl's read cap one way decides from the other: a cut backward answer is
  ``diverged`` or ``behind``; a cut forward one is ``diverged`` or ``too_large``.

Every answer is checked inside the call ``ReadEnv.call`` retries, so a malformed one is retried
like a 5xx. Diff text is sanitized with ``sanitize_bytes(..., diff=True)``, which keeps the line
count; a path is sanitized as a one-line field. Ids reach an endpoint only through ``path_int``
and ``Gateway.api`` (``build_endpoint``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import model as m
from ..errors import ForgectlError, refused
from ..gateway import MAX_STDOUT, AnswerCut, Response, Target, path_int, verify_identity
from ..identifiers import PrRef
from ..sanitize import sanitize_bytes
from .github_diff import RawFile, RawFiles
from .gitlab import (
    MAX_PAGES,
    _bad,
    _flag,
    _int_id,
    _list,
    _obj,
    _sha,
)

if TYPE_CHECKING:
    from .gitlab import GitLabBackend

PAGE_SIZE = 100
MAX_FILE_PAGES = MAX_PAGES
MAX_PATH = 4096
#: GitLab's default cap on the files of a compare; an answer with this many may be cut (UNVERIFIED).
COMPARE_FILE_CAP = 1000
#: `/diffs` first shipped in this GitLab version.
DIFFS_VERSION = (15, 7)

#: git's modes; "0", "" or null is a side that does not exist.
_MODES = frozenset({"100644", "100755", "120000", "160000"})
_NO_NEWLINE = "\\ No newline at end of file"
_SIDE = r'(?:/dev/null|"{0}/(?:[^"\\\n]|\\.)*"|{0}/[^\n]*)'
#: GitLab's text for a binary file (UNVERIFIED): git's one notice line, names as git writes them.
_BINARY = re.compile(rf"^Binary files {_SIDE.format('a')} and {_SIDE.format('b')} differ\n?\Z")
_HUNK = re.compile(r"^@@ -([0-9]{1,9})(?:,([0-9]{1,9}))? \+([0-9]{1,9})(?:,([0-9]{1,9}))? @@")
_COUNT = re.compile(r"^[0-9]{1,9}\Z")
_OVERFLOW = re.compile(r"^[0-9]{1,9}\+\Z")


class _Moved(Exception):
    """The MR's diff shifted under the read: read it all once more."""


class _Cut(Exception):
    """An answer past forgectl's read cap, for a caller that decides what it means (never
    retried: it is not a ``ForgectlError``)."""


# -- the data other issues reuse ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GitLabFile:
    """One ``/diffs`` entry, checked but not sanitized. ``path``, ``previous_path`` and
    ``fragment`` are untrusted raw text: never print them. ``fragment`` is "" with no hunks;
    ``cut`` says the host did not give the whole file."""

    path: str
    previous_path: str | None  # set exactly when renamed
    status: m.FileStatus
    old_mode: str | None
    new_mode: str | None
    fragment: str
    cut: bool
    additions: int | None
    deletions: int | None

    def raw(self) -> RawFile:
        """The shape position validation (``positions.py``) reads: ``patch`` is None with no usable
        fragment (binary, cut, rename only), which that rule treats as invalid. A ``Binary files
        ... differ`` notice is no fragment: its ``patch`` is None too, even for an added or
        deleted file that ``diff`` writes whole (headers only). A type change gives two entries
        with one ``path`` (deleted, then added)."""
        return RawFile(
            path=self.path,
            previous_path=self.previous_path,
            status=self.status,
            additions=self.additions,
            deletions=self.deletions,
            patch=None if self.cut or not self.fragment else self.fragment,
        )

    def change(self) -> m.FileChange:
        return self.raw().change()


@dataclass(frozen=True, slots=True)
class MrPin:
    """The facts of the MR that a read is pinned to."""

    project_id: int
    canonical: str  # the project path from the MR's own web_url
    head_sha: str
    base_sha: str  # diff_refs.base_sha
    start_sha: str | None
    diff_head_sha: str  # diff_refs.head_sha
    changes: int | None  # `changes_count`; null reads 0
    overflow: bool  # `changes_count` like "1000+": the host's own file limit was hit

    def pin(self) -> tuple[object, ...]:
        return (
            self.project_id,
            self.canonical,
            self.head_sha,
            self.base_sha,
            self.start_sha,
            self.diff_head_sha,
        )


# -- reading through the backend's gateway -----------------------------------------------------


def _read[T](
    gl: GitLabBackend,
    target: Target,
    segments: list[str],
    check: Callable[[Response], T],
    *,
    query: tuple[tuple[str, str], ...] = (),
    shape: Any = "object",
    passthrough: frozenset[int] = frozenset(),
    cut: str | bool | None = None,
) -> T:
    """One GET, retried through the backend's ``retry`` (``env.call``). ``check`` validates the
    answer inside the retried call, so a malformed answer is retried like a 5xx. With ``cut`` (a
    diff), an answer past forgectl's read cap is ``too_large``, never retried; with ``cut=True``
    it raises ``_Cut`` for the caller. Without it, it is ``upstream_error`` like any read."""

    checked: list[T] = []

    def once() -> Response:
        try:
            response = gl._gateway.api(
                target, "GET", segments, query=query, shape=shape, passthrough=passthrough
            )
        except AnswerCut:
            if cut is None:
                raise
            if cut is True:
                raise _Cut from None
            raise _too_large(f"{cut} is larger than forgectl's read cap") from None
        checked.append(check(response))  # raises inside the retried call
        return response

    gl._retry(once)
    return checked[-1]


def _too_large(what: str) -> ForgectlError:
    return refused("too_large", f"{what}; a partial diff would mislead")


def _mr_segments(ref: PrRef, mr: MrPin) -> list[str]:
    return [
        "projects",
        path_int(mr.project_id),
        "merge_requests",
        path_int(ref.number),
    ]


def _count(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**31:
        raise _bad(what)
    return value


def _changes(raw: Any) -> tuple[int | None, bool]:
    """`changes_count`: a string of digits, null for no changes yet (0), or `1000+`."""
    if raw is None:
        return 0, False
    if isinstance(raw, int) and not isinstance(raw, bool):
        return _count(raw, "changes_count"), False
    if isinstance(raw, str) and _COUNT.match(raw):
        return int(raw), False
    if isinstance(raw, str) and _OVERFLOW.match(raw):
        return int(raw[:-1]), True
    raise _bad("changes_count")


def _identity(
    gl: GitLabBackend, target: Target, number: int, mr: dict[str, Any]
) -> tuple[int, str]:
    """``(project_id, canonical)`` of an MR answer: its own web_url, iid and project id."""
    canonical = gl._canonical_path(mr.get("web_url"), target, number)
    if _int_id(mr.get("iid"), "iid") != number:
        raise gl._wrong_host(target, "another merge request number", mr.get("web_url"))
    project_id = _int_id(mr.get("project_id"), "project_id")
    if mr.get("target_project_id") is not None and mr["target_project_id"] != project_id:
        raise _bad("target_project_id")
    return project_id, canonical


def read_mr_identity(gl: GitLabBackend, target: Target, ref: PrRef) -> tuple[int, str]:
    """The MR's ``(project_id, canonical)`` with the identity chain of ``show`` and the project
    read, and nothing a diff read needs (``compare`` uses no ``diff_refs``)."""
    number = int(path_int(ref.number))
    segments = ["projects", ref.project, "merge_requests", str(number)]
    project_id, canonical = _read(
        gl,
        target,
        segments,
        lambda response: _identity(gl, target, number, _obj(response.data, "merge request")),
    )
    gl._project(target, project_id, canonical, gl._prefixed(target, canonical))
    return project_id, canonical


def read_mr(gl: GitLabBackend, target: Target, ref: PrRef, *, project_check: bool = True) -> MrPin:
    """The MR with the identity chain of ``show`` (its own web_url, iid and project id; the
    project read when ``project_check``) and its ``diff_refs``, which a diff read needs."""
    number = int(path_int(ref.number))
    segments = ["projects", ref.project, "merge_requests", str(number)]

    def check(response: Response) -> MrPin:
        mr = _obj(response.data, "merge request")
        project_id, canonical = _identity(gl, target, number, mr)
        refs = _obj(mr.get("diff_refs"), "diff_refs")  # null while the host computes the diff
        start = refs.get("start_sha")
        changes, overflow = _changes(mr.get("changes_count"))
        return MrPin(
            project_id=project_id,
            canonical=canonical,
            head_sha=_sha(mr.get("sha"), "sha"),
            base_sha=_sha(refs.get("base_sha"), "diff_refs base_sha"),
            start_sha=None if start is None else _sha(start, "diff_refs start_sha"),
            diff_head_sha=_sha(refs.get("head_sha"), "diff_refs head_sha"),
            changes=changes,
            overflow=overflow,
        )

    pin = _read(gl, target, segments, check)
    if project_check:
        gl._project(target, pin.project_id, pin.canonical, gl._prefixed(target, pin.canonical))
    return pin


def _stable[T](
    gl: GitLabBackend, target: Target, ref: PrRef, fetch: Callable[[MrPin], T]
) -> tuple[MrPin, T]:
    """``fetch`` pinned to an MR whose project, head and ``diff_refs`` did not move meanwhile and
    whose listing did not shift under the read: one more try, then ``upstream_error``."""
    for _ in range(2):
        before = read_mr(gl, target, ref)
        if before.diff_head_sha != before.head_sha:  # the host's diff is of another head
            continue
        try:
            value = fetch(before)
        except _Moved:
            continue
        after = read_mr(gl, target, ref, project_check=False)
        if before.pin() == after.pin():
            return after, value
    raise ForgectlError(
        "upstream_error", "the merge request moved during the read, twice; try again"
    )


# -- one file ----------------------------------------------------------------------------------


def _path(value: object, what: str) -> str:
    """A repository path from the host: git's own rules (no empty, ``.`` or ``..`` component, not
    absolute, no NUL, encodable), a length cap. A newline, a leading ``-`` or bidi text are legal
    and stay, quoted in the diff and sanitized when listed."""
    if not isinstance(value, str) or not value or len(value) > MAX_PATH:
        raise _bad(what)
    if "\x00" in value or value.startswith("/"):
        raise _bad(what)
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise _bad(what)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _bad(what) from None
    return value


def _mode(value: object, what: str) -> str | None:
    """One of git's modes, or None for a side that does not exist ("0", "" or null)."""
    if value is None or value in ("", "0"):
        return None
    if not isinstance(value, str) or value not in _MODES:
        raise _bad(what)
    return value


def _optional_flag(entry: dict[str, Any], key: str) -> bool:
    if key not in entry or entry[key] is None:
        return False
    return _flag(entry[key], key)


def _end(start: int, count: int) -> int:
    """The first line a later hunk may start at: past this one's lines, or past the line an empty
    range sits after."""
    return start + (count if count else 1)


def _fragment(text: str, status: m.FileStatus) -> tuple[int, int, bool]:
    """``(additions, deletions, whole)`` of a fragment, checked hunk by hunk against its headers'
    line counts and git's grammar: no hunk empty on both sides, each hunk past the one before on
    both sides, an added file's old range ``-0,0`` and a deleted file's new range ``+0,0``, and
    only ``\\ No newline at end of file``, at most once a side, right after a line. ``whole`` is
    False when the text ends inside a hunk (a fragment the host cut). A line that belongs to no
    hunk is malformed. Lines end at LF only."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    additions = deletions = 0
    old_left = new_left = 0
    old_next = new_next = 0  # the first line the next hunk may start at
    last = ""  # the marker of the line before, "" after a header or a no-newline line
    marked: set[str] = set()  # the sides a no-newline line already closed
    for line in lines:
        if line.startswith("\\"):
            sides = {" ": {"-", "+"}, "-": {"-"}, "+": {"+"}}.get(last)
            if line != _NO_NEWLINE or sides is None or sides & marked:
                raise _bad("diff fragment")
            marked |= sides
            last = ""
            continue
        if old_left == 0 and new_left == 0:
            header = _HUNK.match(line)
            if header is None:
                raise _bad("diff fragment")
            old_start, new_start = int(header.group(1)), int(header.group(3))
            old_left = 1 if header.group(2) is None else int(header.group(2))
            new_left = 1 if header.group(4) is None else int(header.group(4))
            if old_left == 0 and new_left == 0:
                raise _bad("diff fragment")
            if old_start < old_next or new_start < new_next:
                raise _bad("diff fragment")
            if status == "added" and (old_start, header.group(2)) != (0, "0"):
                raise _bad("diff fragment")
            if status == "deleted" and (new_start, header.group(4)) != (0, "0"):
                raise _bad("diff fragment")
            old_next, new_next = _end(old_start, old_left), _end(new_start, new_left)
            last = ""
            continue
        marker = line[:1]
        if marker == " ":
            old_left -= 1
            new_left -= 1
        elif marker == "-":
            old_left -= 1
            deletions += 1
        elif marker == "+":
            new_left -= 1
            additions += 1
        else:
            raise _bad("diff fragment")
        if old_left < 0 or new_left < 0:
            raise _bad("diff fragment")
        last = marker
    return additions, deletions, old_left == 0 and new_left == 0


def parse_file(item: object) -> GitLabFile:
    entry = _obj(item, "a diff entry")
    old_path = _path(entry.get("old_path"), "old_path")
    new_path = _path(entry.get("new_path"), "new_path")
    added = _flag(entry.get("new_file"), "new_file")
    renamed = _flag(entry.get("renamed_file"), "renamed_file")
    deleted = _flag(entry.get("deleted_file"), "deleted_file")
    if added + renamed + deleted > 1:
        raise _bad("file flags")
    status: m.FileStatus = (
        "added" if added else "deleted" if deleted else "renamed" if renamed else "modified"
    )
    if (old_path != new_path) != renamed:
        raise _bad("paths")
    text = entry.get("diff")
    if not isinstance(text, str):
        raise _bad("diff")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise _bad("diff") from None
    old_mode = _mode(entry.get("a_mode"), "a_mode")
    new_mode = _mode(entry.get("b_mode"), "b_mode")
    if (old_mode is None) != added or (new_mode is None) != deleted:
        raise _bad("file modes")
    host_cut = _optional_flag(entry, "too_large") or _optional_flag(entry, "collapsed")
    additions: int | None = None
    deletions: int | None = None
    whole = True
    binary = bool(text) and not host_cut and _BINARY.match(text) is not None
    if text and not host_cut and not binary:
        additions, deletions, whole = _fragment(text, status)
    fragment = "" if host_cut or binary or not whole else text
    if not whole:
        additions = deletions = None
    changed = renamed or (old_mode is not None and new_mode is not None and old_mode != new_mode)
    cut = (
        host_cut
        or not whole
        # a binary notice proves the content changed; only an added or deleted file is whole
        or (binary and status not in ("added", "deleted"))
        or (not fragment and _optional_flag(entry, "generated_file"))
        or (not fragment and status == "modified" and not changed)
    )
    return GitLabFile(
        path=new_path,
        previous_path=old_path if renamed else None,
        status=status,
        old_mode=old_mode,
        new_mode=new_mode,
        fragment=fragment,
        cut=cut,
        additions=additions,
        deletions=deletions,
    )


def _repeat(files: list[GitLabFile], found: GitLabFile) -> bool:
    """Add ``found`` to ``files``; True when its path is already there in a way git never lists.
    A path is listed once, or twice as a type change (file to symlink and back): one deleted and
    one added entry, kept together with the deleted one first, as git writes it."""
    same = [i for i, f in enumerate(files) if f.path == found.path]
    if not same:
        files.append(found)
        return False
    if len(same) > 1 or {files[same[0]].status, found.status} != {"deleted", "added"}:
        return True
    files.insert(same[0] + (found.status == "added"), found)
    return False


def parse_files(items: list[Any], what: str, *, limit: int) -> list[GitLabFile]:
    if len(items) > limit:
        raise _bad(what)
    files: list[GitLabFile] = []
    for item in items:
        if _repeat(files, parse_file(item)):
            raise _bad("a list that names a file twice")
    return files


# -- the text ----------------------------------------------------------------------------------

_C_ESCAPES = {7: "a", 8: "b", 9: "t", 10: "n", 11: "v", 12: "f", 13: "r", 34: '"', 92: "\\"}


def quote_path(path: str) -> str:
    """The path as git prints it (``core.quotePath`` on): unchanged, or in double quotes with C
    escapes and octal bytes for a quote, a backslash, a control character or a non-ASCII byte."""
    data = path.encode("utf-8")
    if not any(b < 0x20 or b == 0x7F or b >= 0x80 or b in (0x22, 0x5C) for b in data):
        return path
    out = ['"']
    for byte in data:
        if byte in _C_ESCAPES:
            out.append("\\" + _C_ESCAPES[byte])
        elif byte < 0x20 or byte >= 0x7F:
            out.append(f"\\{byte:03o}")
        else:
            out.append(chr(byte))
    out.append('"')
    return "".join(out)


def _tab(path: str) -> str:
    return "\t" if " " in path else ""


def render_file(f: GitLabFile) -> str:
    """One file's section of the assembled diff. Order, as git writes it: ``diff --git``; then
    ``new file mode`` (added), ``deleted file mode`` (deleted) or ``old mode`` and ``new mode``
    (a mode change); then ``rename from`` and ``rename to``; then, only with a fragment, ``---``
    and ``+++`` (``/dev/null`` for the side that does not exist; a TAB after a path that has a
    space), then the fragment."""
    old = f.previous_path if f.previous_path is not None else f.path
    a, b = quote_path(f"a/{old}"), quote_path(f"b/{f.path}")
    lines = [f"diff --git {a} {b}"]
    if f.status == "added":
        if f.new_mode is not None:
            lines.append(f"new file mode {f.new_mode}")
    elif f.status == "deleted":
        if f.old_mode is not None:
            lines.append(f"deleted file mode {f.old_mode}")
    elif f.old_mode is not None and f.new_mode is not None and f.old_mode != f.new_mode:
        lines += [f"old mode {f.old_mode}", f"new mode {f.new_mode}"]
    if f.status == "renamed":
        lines += [f"rename from {quote_path(old)}", f"rename to {quote_path(f.path)}"]
    if f.fragment:
        # git ends a name with a space with a TAB (diff.c), never /dev/null
        lines.append("--- /dev/null" if f.status == "added" else f"--- {a}{_tab(old)}")
        lines.append("+++ /dev/null" if f.status == "deleted" else f"+++ {b}{_tab(f.path)}")
    text = "\n".join(lines) + "\n"
    if f.fragment:
        text += f.fragment if f.fragment.endswith("\n") else f.fragment + "\n"
    return text


def assemble(files: list[GitLabFile], what: str) -> str:
    """The diff of whole files, sanitized. A cut file, or text past forgectl's read cap, is
    ``too_large``."""
    if any(f.cut for f in files):
        raise _too_large(f"GitLab cut a file of {what}")
    text = "".join(render_file(f) for f in files)
    if len(text.encode("utf-8")) > MAX_STDOUT:
        raise _too_large(f"{what} is larger than forgectl's read cap")
    return sanitize_bytes(text.encode("utf-8"), diff=True)


# -- the file list -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Listing:
    files: tuple[GitLabFile, ...]
    ended: bool  # pagination reached the host's end of the list
    reason: str | None

    def raw(self) -> RawFiles:
        return RawFiles(tuple(f.raw() for f in self.files), self.ended, self.reason)


def _unsupported(gl: GitLabBackend, target: Target) -> ForgectlError:
    version = gl.host_version(target.host)
    if version is not None and version >= DIFFS_VERSION:
        return ForgectlError("not_found", "the merge request has no diff to read")
    shown = f"{version[0]}.{version[1]}" if version else "unreadable"
    return ForgectlError(
        "unsupported_host_version",
        f"{target.host} has no merge request /diffs (GitLab 15.7 or later is needed;"
        f" version {shown})",
    )


def list_files(
    gl: GitLabBackend, target: Target, ref: PrRef, mr: MrPin, *, diff: bool = False
) -> Listing:
    """Every file ``/diffs`` lists, 100 a page, up to ``MAX_FILE_PAGES`` pages. A file on two
    pages (other than a type change's two entries) is a list that shifted under the read
    (``_Moved``). With ``diff``, text past forgectl's read cap is ``too_large``; without it (a
    list of files) there is no size cap, and a page past the gateway's cap is ``upstream_error``,
    as on GitHub."""
    segments = [*_mr_segments(ref, mr), "diffs"]
    files: list[GitLabFile] = []
    size = 0
    for page in range(1, MAX_FILE_PAGES + 1):

        def check(response: Response, page: int = page) -> tuple[list[GitLabFile], bool]:
            if response.status == 404:
                if page == 1:
                    raise _unsupported(gl, target)
                raise ForgectlError("upstream_error", "gitlab: the diff list vanished mid-read")
            batch = parse_files(_list(response.data, "diff list"), "diff list", limit=PAGE_SIZE)
            header = response.headers.get("x-next-page")
            if header is None:
                return batch, len(batch) < PAGE_SIZE
            if header == "":
                return batch, True
            if header != str(page + 1):
                raise _bad("pagination header")
            return batch, False

        batch, last = _read(
            gl,
            target,
            segments,
            check,
            query=(("per_page", str(PAGE_SIZE)), ("page", str(page))),
            shape="list",
            passthrough=frozenset({404}),
            cut="the diff list" if diff else None,
        )
        for found in batch:
            if _repeat(files, found):
                raise _Moved
            size += len(found.fragment) + len(found.path)
        if diff and size > MAX_STDOUT:
            raise _too_large("the diff is larger than forgectl's read cap")
        if last:
            return Listing(tuple(files), ended=True, reason=None)
    return Listing(tuple(files), ended=False, reason="the host's list went past the page limit")


def read_raw_files(gl: GitLabBackend, ref: PrRef) -> tuple[MrPin, RawFiles]:
    """The pinned MR and its files as ``RawFiles`` (for position validation, ``positions.py``)."""
    target = gl._hosts.target(ref.host)
    mr, listing = _stable(gl, target, ref, lambda pin: list_files(gl, target, ref, pin))
    return mr, listing.raw()


def _pr_name(ref: PrRef, mr: MrPin) -> str:
    return f"{ref.host}/{mr.canonical}#{ref.number}"


# -- files -------------------------------------------------------------------------------------


def files_result(gl: GitLabBackend, ref: PrRef) -> m.FilesResult:
    target = gl._hosts.target(ref.host)
    mr, listing = _stable(gl, target, ref, lambda pin: list_files(gl, target, ref, pin))
    listed = len(listing.files)
    complete = listing.ended and not mr.overflow and mr.changes == listed
    if not complete:
        gl._warn(listing.reason or "files: the listed files do not add up to changes_count")
    return m.FilesResult(
        pr=_pr_name(ref, mr),
        head_sha=mr.head_sha,
        files=tuple(f.change() for f in listing.files),
        files_complete=complete,
        changed_files=listed,
    )


# -- diff --------------------------------------------------------------------------------------


def diff_result(gl: GitLabBackend, ref: PrRef) -> m.DiffResult:
    target = gl._hosts.target(ref.host)

    def fetch(pin: MrPin) -> str:
        listing = list_files(gl, target, ref, pin, diff=True)
        if not listing.ended or pin.overflow:
            raise _too_large("GitLab's list of this merge request's files may be cut")
        if pin.changes != len(listing.files):
            raise _Moved  # a push mid-read, or a list GitLab cut; a second mismatch is an error
        return assemble(list(listing.files), "this diff")

    mr, text = _stable(gl, target, ref, fetch)
    return m.DiffResult(
        pr=_pr_name(ref, mr),
        head_sha=mr.head_sha,
        base_sha=mr.base_sha,
        # diff_refs.base_sha is the merge base the host diffs from (UNVERIFIED)
        merge_base_sha=mr.base_sha,
        diff=text,
    )


# -- compare -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Compared:
    commits: int
    same_ref: bool
    timeout: bool
    files: list[GitLabFile] | None  # parsed (forward only); None at GitLab's file cap
    capped: bool  # as many `diffs` as GitLab's cap: the list may be cut


#: A comparison answer past forgectl's read cap.
_CUT = "cut"


def _compare_one(
    gl: GitLabBackend,
    target: Target,
    mr: tuple[int, str],
    from_sha: str,
    to_sha: str,
    *,
    parse: bool,
) -> _Compared | None | str:
    """``/repository/compare`` of the target project, ``straight=false``; None on a 404, ``_CUT``
    past forgectl's read cap. With ``parse``, the ``diffs`` entries are checked inside the
    retried call, and only when this answer can still make the status ``ahead``: it has commits,
    no ``compare_timeout``, no ``compare_same_ref`` and fewer entries than GitLab's cap."""
    project_id, canonical = mr

    def check(response: Response) -> _Compared | None:
        if response.status == 404:
            return None
        data = _obj(response.data, "comparison")
        web_url = data.get("web_url")
        if web_url is not None:
            verify_identity(
                web_url,
                host=target.host,
                project=gl._prefixed(target, canonical),
                secret=target.token,
            )
        commits = _list(data.get("commits"), "commits")
        for commit in commits:
            _sha(_obj(commit, "commit").get("id"), "commit id")
        if commits:
            head = _obj(data.get("commit"), "commit")
            if _sha(head.get("id"), "commit id") != to_sha:
                raise _bad("commit")
        diffs = _list(data.get("diffs"), "diffs")
        capped = len(diffs) >= COMPARE_FILE_CAP
        same_ref = _flag(data.get("compare_same_ref"), "compare_same_ref")
        timeout = _flag(data.get("compare_timeout"), "compare_timeout")
        files = None
        if parse and commits and not (capped or same_ref or timeout):
            files = parse_files(diffs, "diffs", limit=COMPARE_FILE_CAP)
        return _Compared(
            commits=len(commits), same_ref=same_ref, timeout=timeout, files=files, capped=capped
        )

    try:
        return _read(
            gl,
            target,
            ["projects", path_int(project_id), "repository", "compare"],
            check,
            query=(("from", from_sha), ("to", to_sha), ("straight", "false")),
            passthrough=frozenset({404}),
            cut=True,
        )
    except _Cut:
        return _CUT


def _compare_status(forward: _Compared, backward: _Compared) -> m.CompareStatus:
    if forward.same_ref or backward.same_ref:
        both = forward.same_ref and backward.same_ref
        quiet = forward.commits == 0 and backward.commits == 0
        return "identical" if both and quiet else "unavailable"
    if forward.commits and not backward.commits:
        return "ahead"
    if backward.commits and not forward.commits:
        return "behind"
    if forward.commits and backward.commits:
        return "diverged"
    return "unavailable"  # two different shas with no commit either way: unclear


def compare_result(
    gl: GitLabBackend, ref: PrRef, from_sha: str, to_sha: str, *, no_diff: bool = False
) -> m.CompareResult:
    """``no_diff`` (#44): the ``diffs`` entries of the answers are never parsed (GitLab sends
    them with the commits either way), and the result has no ``diff`` key."""
    target = gl._hosts.target(ref.host)
    mr = read_mr_identity(gl, target, ref)  # a missing MR is not_found; the canonical path
    pr = f"{ref.host}/{mr[1]}#{ref.number}"

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
    # Backward first: only a backward answer that is not cut, with no commits and no same-ref
    # flag, leaves `ahead` possible, and only then are the forward diffs parsed.
    backward = _compare_one(gl, target, mr, to_sha, from_sha, parse=False)
    if backward is None:
        return result("unavailable", None)
    can_be_ahead = (
        not no_diff
        and isinstance(backward, _Compared)
        and not backward.commits
        and not backward.same_ref
    )
    forward = _compare_one(gl, target, mr, from_sha, to_sha, parse=can_be_ahead)
    if forward is None:
        return result("unavailable", None)
    if any(isinstance(c, _Compared) and c.timeout for c in (forward, backward)):
        raise _too_large("GitLab timed out computing this comparison")
    if isinstance(backward, str):  # cut: its diffs are never used, so the forward answer decides
        if isinstance(forward, str):
            raise _too_large("this comparison is larger than forgectl's read cap")
        return result("diverged" if forward.commits else "behind", None)
    if isinstance(forward, str):  # cut: no diff to show, which only an ahead status needs
        if backward.commits:
            return result("diverged", None)
        raise _too_large("this comparison is larger than forgectl's read cap")
    status = _compare_status(forward, backward)
    if status == "identical":
        return result(status, "")
    if status != "ahead":  # behind, diverged: no diff, by the interface
        return result(status, None)
    if no_diff:
        return result("ahead", None)
    if forward.capped or forward.files is None:
        raise _too_large("this comparison lists as many files as GitLab's cap")
    return result("ahead", assemble(forward.files, "this comparison"))


__all__ = [
    "GitLabFile",
    "Listing",
    "MrPin",
    "RawFile",
    "RawFiles",
    "assemble",
    "compare_result",
    "diff_result",
    "files_result",
    "list_files",
    "parse_file",
    "quote_path",
    "read_mr",
    "read_mr_identity",
    "read_raw_files",
    "render_file",
]
