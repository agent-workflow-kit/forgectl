"""Position validation for inline review comments (interface.md "Position validation", Post order
step 3).

Every inline comment is checked against the host's own file list before anything is posted:

- ``right`` is valid on added and context lines, ``left`` on removed and context lines; a line must
  be inside a hunk; ``start_line <= line``, and the range is in one hunk on one side.
- A renamed file matches by its new path; a file with no patch (binary, too large, rename only) is
  invalid; the old path of a rename is not a path of the diff.
- A finding is ``invalid_position`` (exit 12, ``details.invalid``). ``too_large`` (exit 20) comes
  only from a cut file list with a comment on a file it does not list, or a host's refusal of the
  whole list. A listed file of a cut list is checked normally.

``validate`` is pure: it takes the ``RawFiles`` both hosts' diff readers give. ``check_github`` and
``check_gitlab`` read them and return the ``Placement`` of each comment, from which a review module
builds the host's parameters (``Placement.github_fields``, ``Placement.gitlab_position``).

A path or patch is the host's raw text: nothing here prints one except through ``sanitize_line``.

UNVERIFIED until the live suite (#31): GitLab's ``line_code`` is
``sha1(new path)_<old>_<new>``, where ``<old>`` and ``<new>`` are the hunk's running line
counters at that line (an added line still carries the old counter), and a ``line_range``
point's ``type`` is ``old``, ``new`` or null.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .backends import github_diff, gitlab_diff
from .backends.github_diff import RawFile, RawFiles
from .backends.gitlab_diff import MrPin
from .errors import ForgectlError, refused
from .identifiers import PrRef
from .reads import ReadEnv
from .sanitize import sanitize_line

if TYPE_CHECKING:
    from .backends.gitlab import GitLabBackend

_HUNK = re.compile(r"^@@ -([0-9]{1,9})(?:,([0-9]{1,9}))? \+([0-9]{1,9})(?:,([0-9]{1,9}))? @@")
_NO_NEWLINE = "\\ No newline at end of file"
SIDES = ("left", "right")


@dataclass(frozen=True, slots=True)
class InlineComment:
    """The position of one review comment, as ``review-input.json`` gives it. ``index`` is its
    place in the input's ``comments``, which ``details.invalid`` reports."""

    index: int
    path: str
    line: int
    side: str = "right"
    start_line: int | None = None


@dataclass(frozen=True, slots=True)
class DiffLine:
    """One line of a hunk. ``old`` and ``new`` are the file's own line numbers (None on the side
    the line is not on); ``old_pos`` and ``new_pos`` are the hunk's running counters, which
    GitLab's ``line_code`` uses."""

    kind: str  # " ", "-" or "+"
    old: int | None
    new: int | None
    old_pos: int
    new_pos: int


@dataclass(frozen=True, slots=True)
class Hunk:
    lines: tuple[DiffLine, ...]
    _old: dict[int, DiffLine] = field(init=False, repr=False, compare=False)
    _new: dict[int, DiffLine] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_old", {n.old: n for n in self.lines if n.old is not None})
        object.__setattr__(self, "_new", {n.new: n for n in self.lines if n.new is not None})

    def find(self, side: str, number: int) -> DiffLine | None:
        return (self._new if side == "right" else self._old).get(number)


def _bad_patch() -> ForgectlError:
    return ForgectlError("upstream_error", "the host gave a file patch that is not a valid diff")


def parse_patch(patch: str) -> tuple[Hunk, ...]:
    """The hunks of a patch (hunks only, no file headers), checked against each header's counts.
    Anything else is ``upstream_error``: a patch the host mangled proves nothing about a line."""
    rows = patch.split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    hunks: list[Hunk] = []
    current: list[DiffLine] = []
    old_left = new_left = old_pos = new_pos = 0
    old_next = new_next = 0  # the first line the next hunk may start at
    last = ""
    for row in rows:
        if row == _NO_NEWLINE:
            if last == "" or last == "\\":
                raise _bad_patch()
            last = "\\"
            continue
        if old_left == 0 and new_left == 0:
            header = _HUNK.match(row)
            if header is None:
                raise _bad_patch()
            if current:
                hunks.append(Hunk(tuple(current)))
            current = []
            old_pos, new_pos = int(header.group(1)), int(header.group(3))
            old_left = 1 if header.group(2) is None else int(header.group(2))
            new_left = 1 if header.group(4) is None else int(header.group(4))
            if old_left == 0 and new_left == 0:
                raise _bad_patch()
            if old_pos < old_next or new_pos < new_next:
                raise _bad_patch()  # a hunk must start past the one before, on both sides
            old_next = old_pos + (old_left or 1)
            new_next = new_pos + (new_left or 1)
            last = ""
            continue
        marker = row[:1]
        if marker == " ":
            current.append(DiffLine(" ", old_pos, new_pos, old_pos, new_pos))
            old_left, new_left, old_pos, new_pos = (
                old_left - 1,
                new_left - 1,
                old_pos + 1,
                new_pos + 1,
            )
        elif marker == "-":
            current.append(DiffLine("-", old_pos, None, old_pos, new_pos))
            old_left, old_pos = old_left - 1, old_pos + 1
        elif marker == "+":
            current.append(DiffLine("+", None, new_pos, old_pos, new_pos))
            new_left, new_pos = new_left - 1, new_pos + 1
        else:
            raise _bad_patch()
        if old_left < 0 or new_left < 0:
            raise _bad_patch()
        last = marker
    if old_left != 0 or new_left != 0:
        raise _bad_patch()
    if current:
        hunks.append(Hunk(tuple(current)))
    return tuple(hunks)


# -- a placed comment ------------------------------------------------------------------------------


def line_code(path: str, line: DiffLine) -> str:
    """GitLab's line code for a line of ``path`` (the file's new path)."""
    digest = hashlib.sha1(path.encode("utf-8", errors="surrogatepass"), usedforsecurity=False)
    return f"{digest.hexdigest()}_{line.old_pos}_{line.new_pos}"


@dataclass(frozen=True, slots=True)
class Placement:
    """A comment that passed. ``end`` is the line the comment is on; ``start`` is the first line
    of a range (None for one line)."""

    index: int
    path: str
    previous_path: str | None
    side: str
    line: int
    start_line: int | None
    start: DiffLine | None
    end: DiffLine

    def github_fields(self) -> dict[str, Any]:
        """The comment as ``POST /pulls/N/reviews`` takes it (``path``, ``line``, ``side``, and
        for a range ``start_line`` and ``start_side``); ``body`` is the caller's."""
        side = self.side.upper()
        out: dict[str, Any] = {"path": self.path, "line": self.line, "side": side}
        if self.start_line is not None:
            out["start_line"] = self.start_line
            out["start_side"] = side
        return out

    def gitlab_position(self, pin: MrPin) -> dict[str, Any]:
        """The comment's text position for a draft note: the MR's three shas, both paths, both
        line numbers paired from the diff (None on the side a line is not on), the computed
        ``line_code``, and for a range ``line_range``. UNVERIFIED until #31."""
        if pin.start_sha is None:
            raise ForgectlError("upstream_error", "the merge request has no start sha")
        position: dict[str, Any] = {
            "position_type": "text",
            "base_sha": pin.base_sha,
            "start_sha": pin.start_sha,
            "head_sha": pin.diff_head_sha,
            "old_path": self.previous_path or self.path,
            "new_path": self.path,
            "old_line": self.end.old,
            "new_line": self.end.new,
            "line_code": line_code(self.path, self.end),
        }
        if self.start is not None:
            position["line_range"] = {
                "start": self._point(self.start),
                "end": self._point(self.end),
            }
        return position

    def _point(self, line: DiffLine) -> dict[str, Any]:
        # UNVERIFIED until #31: new for an added line, otherwise old
        kind = "new" if line.kind == "+" else "old"
        return {
            "line_code": line_code(self.path, line),
            "type": kind,
            "old_line": line.old,
            "new_line": line.new,
        }


# -- the check -------------------------------------------------------------------------------------


def _finding(comment: InlineComment) -> dict[str, Any]:
    return {
        "index": comment.index,
        "path": sanitize_line(comment.path),
        "line": comment.line,
        "side": sanitize_line(comment.side),
        "start_line": comment.start_line,
    }


def _file_for(candidates: list[RawFile], side: str) -> RawFile | None:
    """The one file of a path. A type change lists the path twice (deleted, then added): the old
    side belongs to the deleted entry and the new side to the added one."""
    if len(candidates) > 1:
        skip = "added" if side == "left" else "deleted"
        candidates = [f for f in candidates if f.status != skip]
    return candidates[0] if len(candidates) == 1 else None


def _fields_ok(comment: InlineComment) -> bool:
    """The checks that hold whatever the file is."""
    if comment.side not in SIDES or comment.line < 1:
        return False
    return comment.start_line is None or 1 <= comment.start_line <= comment.line


def _place(comment: InlineComment, file: RawFile, hunks: tuple[Hunk, ...]) -> Placement | None:
    first = comment.line if comment.start_line is None else comment.start_line
    for hunk in hunks:
        end = hunk.find(comment.side, comment.line)
        if end is None:
            continue
        start = hunk.find(comment.side, first)
        if start is None:
            return None  # the range leaves the hunk (it can start in no other: lines are unique)
        ranged = comment.start_line is not None and comment.start_line < comment.line
        return Placement(
            comment.index,
            file.path,
            file.previous_path,
            comment.side,
            comment.line,
            comment.start_line if ranged else None,
            start if ranged else None,
            end,
        )
    return None


def validate(comments: Sequence[InlineComment], files: RawFiles) -> list[Placement]:
    """Check every comment against ``files``; return their placements, in order.

    Raises ``invalid_position`` listing every comment that cannot be placed (nothing is posted),
    else ``too_large`` if the list was cut and a comment names a file it does not list."""
    by_path: dict[str, list[RawFile]] = {}
    old_paths: set[str] = set()
    for file in files.files:
        by_path.setdefault(file.path, []).append(file)
        if file.previous_path is not None:
            old_paths.add(file.previous_path)
    parsed: dict[int, tuple[Hunk, ...]] = {}
    placed: list[Placement] = []
    invalid: list[dict[str, Any]] = []
    unlisted = 0
    for comment in comments:
        if not _fields_ok(comment):
            invalid.append(_finding(comment))
            continue
        candidates = by_path.get(comment.path)
        if candidates is None:
            if files.ended or comment.path in old_paths:
                invalid.append(_finding(comment))
            else:
                unlisted += 1
            continue
        file = _file_for(candidates, comment.side)
        if file is None or file.patch is None:
            invalid.append(_finding(comment))
            continue
        hunks = parsed.get(id(file))
        if hunks is None:
            hunks = parsed[id(file)] = parse_patch(file.patch)
        found = _place(comment, file, hunks)
        if found is None:
            invalid.append(_finding(comment))
        else:
            placed.append(found)
    if invalid:
        raise ForgectlError(
            "invalid_position",
            f"{len(invalid)} inline comment(s) are not on a line of the diff; nothing was posted",
            details={"invalid": invalid},
        )
    if unlisted:
        raise refused(
            "too_large",
            f"the host's file list is cut and {unlisted} inline comment(s) name a file it does "
            "not list, so they cannot be checked",
        )
    return placed


# -- reading the files -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Checked:
    """The placements of a passed check, and the head they were checked against (the caller
    compares it with the head it read at Post order step 2)."""

    head_sha: str
    placements: tuple[Placement, ...]
    pin: MrPin | None = None  # GitLab: the shas a position needs


def _list_refused(err: ForgectlError) -> ForgectlError:
    """A host's refusal of the whole list (a 4xx other than auth and absence) is ``too_large``;
    every other failure, a 5xx or a network one included, keeps its own code. Only the list's own
    call goes through this: a 4xx on the pull or MR read stays ``host_rejected``."""
    if err.code == "host_rejected":
        return refused("too_large", "the host refused to list this pull request's files")
    return err


def _cut(raw: RawFiles) -> RawFiles:
    return RawFiles(raw.files, False, raw.reason or "the listed files do not add up to the total")


def check_github(env: ReadEnv, ref: PrRef, comments: Sequence[InlineComment]) -> Checked:
    target = env.hosts.target(ref.host)

    def fetch(_pull: github_diff.Pull) -> RawFiles:
        try:
            return github_diff.list_files(env, target, ref)
        except ForgectlError as err:
            raise _list_refused(err) from None

    pull, raw = github_diff._stable(env, target, ref, fetch, need_changed_files=True)
    if not (raw.ended and len(raw.files) == pull.changed_files):
        raw = _cut(raw)
    return Checked(pull.head_sha, tuple(validate(comments, raw)))


def check_gitlab(gl: GitLabBackend, ref: PrRef, comments: Sequence[InlineComment]) -> Checked:
    target = gl._hosts.target(ref.host)

    def fetch(pin: MrPin) -> RawFiles:
        try:
            return gitlab_diff.list_files(gl, target, ref, pin).raw()
        except ForgectlError as err:
            raise _list_refused(err) from None

    pin, raw = gitlab_diff._stable(gl, target, ref, fetch)
    if not (raw.ended and not pin.overflow and pin.changes == len(raw.files)):
        raw = _cut(raw)
    return Checked(pin.head_sha, tuple(validate(comments, raw)), pin)


__all__ = [
    "Checked",
    "DiffLine",
    "Hunk",
    "InlineComment",
    "Placement",
    "check_github",
    "check_gitlab",
    "line_code",
    "parse_patch",
    "validate",
]
