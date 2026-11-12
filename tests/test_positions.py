"""Position validation (#25): every line kind, ranges, renames, cut lists, both hosts.

The pure check runs on hand-built ``RawFiles``; the two readers run against the same fakes as
``files`` and ``diff`` (tests/test_github_diff.py, tests/test_gitlab_diff.py). GitLab's
``line_code`` form is UNVERIFIED until the live suite (#31).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from forgectl import positions as pos
from forgectl.backends.github_diff import RawFile, RawFiles
from forgectl.errors import ForgectlError
from forgectl.positions import InlineComment as C

from .gitlab_support import FakeGitLab
from .gitlab_support import ref as gl_ref
from .harness.forges import FakeForges
from .test_github_diff import HEAD, REF, Router, RouterHarness, item, pull
from .test_gitlab_diff import entry, mr_doc, one


@pytest.fixture
def make_github(fake_forges: FakeForges) -> Callable[..., RouterHarness]:
    def build(retries: int = 0, **routes: Any) -> RouterHarness:
        return RouterHarness(fake_forges, Router(**routes), retries=retries)

    return build


@pytest.fixture
def fake_gitlab() -> FakeGitLab:
    return FakeGitLab()


# old: 1 ctx, 2 removed, 3 ctx, 4 ctx   new: 1 ctx, 2 added, 3 added, 4 ctx, 5 ctx
PATCH = " one\n-two\n+TWO\n+TWO-B\n three\n four\n"
HEADER = "@@ -1,4 +1,5 @@\n"
# a second hunk: old 20 removed, new 21 added, old 21 / new 22 context
HUNK2 = "@@ -20,2 +21,2 @@\n-x\n+y\n z\n"
TWO_HUNKS = HEADER + PATCH + HUNK2


def file(path: str = "a.py", patch: str | None = TWO_HUNKS, **kw: Any) -> RawFile:
    return RawFile(
        path=path,
        previous_path=kw.pop("previous_path", None),
        status=kw.pop("status", "modified"),
        additions=1,
        deletions=1,
        patch=patch,
    )


def listed(*files: RawFile, ended: bool = True) -> RawFiles:
    return RawFiles(tuple(files), ended, None if ended else "cut")


def codes(fn: Callable[[], Any]) -> ForgectlError:
    with pytest.raises(ForgectlError) as raised:
        fn()
    return raised.value


def ok(comment: C, *files: RawFile) -> pos.Placement:
    (placed,) = pos.validate([comment], listed(*(files or (file(),))))
    return placed


def bad(comment: C, *files: RawFile, ended: bool = True) -> list[dict[str, Any]]:
    err = codes(lambda: pos.validate([comment], listed(*(files or (file(),)), ended=ended)))
    assert err.code == "invalid_position"
    assert err.exit_code == 12
    return (err.details or {})["invalid"]


# -- line kinds -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "side"),
    [
        (1, "right"),  # context
        (2, "right"),  # added
        (3, "right"),  # added
        (4, "right"),  # context
        (5, "right"),  # context
        (1, "left"),  # context
        (2, "left"),  # removed
        (3, "left"),  # context
        (4, "left"),  # context
        (20, "left"),  # removed, second hunk
        (21, "right"),  # added, second hunk
        (21, "left"),  # context, second hunk
        (22, "right"),
    ],
)
def test_a_line_of_the_diff_on_its_side_is_valid(line: int, side: str) -> None:
    placed = ok(C(0, "a.py", line, side))
    assert (placed.line, placed.side, placed.start) == (line, side, None)


@pytest.mark.parametrize(
    ("line", "side"),
    [
        (6, "right"),  # past the hunk
        (5, "left"),  # old side has 4 lines
        (0, "right"),
        (-3, "left"),
        (10, "right"),  # between hunks
        (10**12, "right"),
        (20, "right"),  # the second hunk's removed line has no new number: 20 is no new line
        (22, "left"),  # new-only
    ],
)
def test_a_line_not_on_its_side_is_invalid(line: int, side: str) -> None:
    assert bad(C(3, "a.py", line, side)) == [
        {"index": 3, "path": "a.py", "line": line, "side": side, "start_line": None}
    ]


def test_a_removed_line_is_not_valid_on_the_right_and_an_added_one_not_on_the_left() -> None:
    # new line 2 and 3 are added: they are no old lines; old line 2 is removed: no new line is 2
    # ... but new line 2 does exist (the added one), so check the numbers that only one side has
    two = file(patch="@@ -1,2 +1,1 @@\n-gone\n-gone2\n+new\n")
    assert bad(C(0, "a.py", 2, "right"), two)  # no new line 2
    assert ok(C(0, "a.py", 2, "left"), two).end.kind == "-"
    only_added = file(patch="@@ -1,0 +1,3 @@\n+a\n+b\n+c\n")
    assert bad(C(0, "a.py", 1, "left"), only_added)
    assert ok(C(0, "a.py", 3, "right"), only_added).end.kind == "+"


def test_the_default_side_of_a_comment_is_right() -> None:
    assert C(0, "a.py", 2).side == "right"


def test_a_side_that_is_neither_is_invalid() -> None:
    assert bad(C(0, "a.py", 1, "RIGHT"))


# -- ranges ---------------------------------------------------------------------------------------


def test_a_range_on_one_side_in_one_hunk_is_valid() -> None:
    placed = ok(C(0, "a.py", 5, "right", 2))
    assert placed.start is not None and placed.start.new == 2
    assert placed.start_line == 2


def test_a_range_on_the_left_may_cross_removed_and_context_lines() -> None:
    assert ok(C(0, "a.py", 4, "left", 1)).start_line == 1


def test_a_range_across_two_hunks_is_invalid() -> None:
    assert bad(C(0, "a.py", 21, "right", 5))


def test_a_range_that_starts_before_the_hunk_is_invalid() -> None:
    assert bad(C(0, "a.py", 3, "right", 0))


def test_a_range_that_starts_after_its_line_is_invalid() -> None:
    assert bad(C(0, "a.py", 2, "right", 3))


def test_a_range_whose_start_is_not_on_the_side_is_invalid() -> None:
    # left side 2 is removed (valid), but start 5 does not exist on the left
    assert bad(C(0, "a.py", 4, "left", 5))


def test_a_range_of_one_line_is_a_single_line() -> None:
    placed = ok(C(0, "a.py", 2, "right", 2))
    assert placed.start is None and placed.start_line is None


# -- files ----------------------------------------------------------------------------------------


def test_a_renamed_file_matches_by_its_new_path_only() -> None:
    renamed = file("new/a.py", previous_path="old/a.py", status="renamed")
    assert ok(C(0, "new/a.py", 2), renamed).path == "new/a.py"
    assert bad(C(0, "old/a.py", 2), renamed)


def test_the_old_path_of_a_rename_is_invalid_even_in_a_cut_list() -> None:
    renamed = file("new/a.py", previous_path="old/a.py", status="renamed")
    assert bad(C(0, "old/a.py", 2), renamed, ended=False)


@pytest.mark.parametrize("patch", [None])
def test_a_file_with_no_patch_is_invalid(patch: None) -> None:
    assert bad(C(0, "a.py", 1), file(patch=patch))


def test_a_pure_rename_with_no_patch_is_invalid() -> None:
    assert bad(C(0, "n.py", 1), file("n.py", None, previous_path="o.py", status="renamed"))


def test_a_file_not_in_the_diff_is_invalid_when_the_list_ended() -> None:
    assert bad(C(0, "other.py", 1))[0]["path"] == "other.py"


def test_a_path_is_case_sensitive_and_exact() -> None:
    assert bad(C(0, "A.py", 2))
    assert bad(C(0, "./a.py", 2))
    assert bad(C(0, "a.py ", 2))
    assert bad(C(0, "", 2))


def test_a_listed_file_in_a_cut_list_is_checked_normally() -> None:
    placed = pos.validate([C(0, "a.py", 2)], listed(file(), ended=False))
    assert placed[0].line == 2
    assert bad(C(0, "a.py", 99), ended=False)


def test_a_file_a_cut_list_does_not_list_is_too_large() -> None:
    err = codes(lambda: pos.validate([C(0, "zzz.py", 1)], listed(file(), ended=False)))
    assert (err.code, err.reason, err.exit_code) == ("refused", "too_large", 20)


def test_a_finding_wins_over_an_unlisted_file() -> None:
    err = codes(
        lambda: pos.validate([C(0, "zzz.py", 1), C(1, "a.py", 99)], listed(file(), ended=False))
    )
    assert err.code == "invalid_position"
    assert [i["index"] for i in err.details["invalid"]] == [1]  # type: ignore[index]


@pytest.mark.parametrize(
    "comment",
    [
        C(0, "zzz.py", 0),
        C(0, "zzz.py", -3),
        C(0, "zzz.py", 1, "weird"),
        C(0, "zzz.py", 3, "right", 4),
        C(0, "zzz.py", 3, "right", 0),
    ],
)
def test_a_locally_invalid_comment_on_an_unlisted_file_is_invalid_not_too_large(comment: C) -> None:
    assert bad(comment, ended=False)[0]["index"] == 0


def test_every_finding_is_listed_in_order() -> None:
    comments = [C(0, "a.py", 2), C(1, "a.py", 99), C(2, "nope.py", 1), C(3, "a.py", 3, "left", 1)]
    err = codes(lambda: pos.validate(comments, listed(file())))
    assert [i["index"] for i in err.details["invalid"]] == [1, 2]  # type: ignore[index]


def test_a_path_in_a_finding_is_sanitized() -> None:
    hostile = "a\x1b[2J\nb.py"
    found = bad(C(0, hostile, 1))[0]["path"]
    assert "\x1b" not in found and "\n" not in found


def test_an_empty_comment_list_places_nothing() -> None:
    assert pos.validate([], listed(file())) == []


def test_a_type_change_matches_the_old_side_to_the_deleted_entry_and_the_new_to_the_added() -> None:
    deleted = file("link", "@@ -1,2 +0,0 @@\n-a\n-b\n", status="deleted")
    added = file("link", "@@ -0,0 +1,1 @@\n+target\n", status="added")
    files = listed(deleted, added)
    left, right = pos.validate([C(0, "link", 2, "left"), C(1, "link", 1, "right")], files)
    assert left.end.kind == "-" and right.end.kind == "+"
    assert bad(C(0, "link", 2, "right"), deleted, added)
    assert bad(C(0, "link", 1, "left"), added)


def test_a_path_listed_twice_with_the_same_status_is_invalid() -> None:
    assert bad(C(0, "a.py", 2), file(), file())


# -- the patch ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        "not a hunk\n",
        "@@ -1,2 +1,2 @@\n a\n",  # short
        "@@ -1,1 +1,1 @@\n a\n b\n",  # long
        "@@ -1,1 +1,1 @@\n?a\n",  # unknown marker
        "@@ -1,1 +1,1 @@\n a\n\\ No newline at end of file\n\\ No newline at end of file\n",
        "@@ -1,1 +1,1 @@\n\\ No newline at end of file\n a\n",
        "@@ -0,0 +0,0 @@\n",
        "@@ -1,1 +1,1 @@\n+a\n",  # old side not consumed
        "@@ -1 +1 @@\n a\ntrailing\n",
    ],
)
def test_a_mangled_patch_is_upstream_error_never_a_valid_line(patch: str) -> None:
    err = codes(lambda: pos.validate([C(0, "a.py", 1)], listed(file(patch=patch))))
    assert err.code == "upstream_error"


@pytest.mark.parametrize(
    "patch",
    [
        "@@ -10 +10 @@\n-a\n+b\n@@ -1 +1 @@\n-c\n+d\n",  # out of order
        "@@ -1,3 +1,3 @@\n a\n-b\n+c\n d\n@@ -3 +3 @@\n-x\n+y\n",  # overlaps on both sides
        "@@ -1,2 +1,2 @@\n a\n-b\n+c\n@@ -5 +2 @@\n-x\n+y\n",  # new side overlaps
        "@@ -1,2 +1,2 @@\n a\n-b\n+c\n@@ -2 +5 @@\n-x\n+y\n",  # old side overlaps
        "@@ -1 +1 @@\n-a\n+b\n@@ -1 +1 @@\n-c\n+d\n",  # the same place twice
    ],
)
def test_hunks_that_do_not_start_past_the_one_before_are_upstream_error(patch: str) -> None:
    err = codes(lambda: pos.parse_patch(patch))
    assert err.code == "upstream_error"


def test_ordinary_multi_hunk_patches_still_parse() -> None:
    assert len(pos.parse_patch("@@ -1 +1 @@\n-a\n+b\n@@ -3 +3 @@\n-c\n+d\n")) == 2
    assert len(pos.parse_patch("@@ -1 +1 @@\n-a\n+b\n@@ -2,0 +3 @@\n+x\n")) == 2
    assert len(pos.parse_patch("@@ -1,0 +1 @@\n+a\n@@ -2,0 +3 @@\n+x\n")) == 2
    assert len(pos.parse_patch(TWO_HUNKS)) == 2


def test_a_huge_digit_string_in_a_hunk_header_is_upstream_error() -> None:
    huge = ("1" * 5000, "2" * 5000)
    for patch in (f"@@ -{huge[0]} +1 @@\n-a\n", f"@@ -1 +{huge[0]},{huge[1]} @@\n+a\n"):
        assert codes(lambda p=patch: pos.parse_patch(p)).code == "upstream_error"


def test_many_comments_on_a_large_hunk_are_found_quickly() -> None:
    import time

    count = 100_000
    patch = f"@@ -1,{count} +1,{count} @@\n" + " x\n" * count
    comments = [C(n, "a.py", count - n) for n in range(300)]
    started = time.monotonic()
    placed = pos.validate(comments, listed(file(patch=patch)))
    assert time.monotonic() - started < 5
    assert [p.line for p in placed][:2] == [count, count - 1]


def test_an_empty_patch_has_no_lines() -> None:
    assert bad(C(0, "a.py", 1), file(patch=""))


def test_a_hunk_with_a_count_of_one_omits_the_comma() -> None:
    placed = ok(C(0, "a.py", 7, "right"), file(patch="@@ -7 +7 @@\n-a\n+b\n"))
    assert placed.end.kind == "+"


def test_a_no_newline_marker_after_a_line_is_allowed() -> None:
    patch = "@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n"
    assert ok(C(0, "a.py", 1, "left"), file(patch=patch)).end.kind == "-"


def test_a_line_whose_text_looks_like_a_header_or_a_marker_is_still_a_line() -> None:
    patch = "@@ -1,2 +1,2 @@\n-@@ -9 +9 @@\n+--- a/x\n context\n"
    assert ok(C(0, "a.py", 1, "right"), file(patch=patch)).end.kind == "+"


def test_crlf_text_and_unicode_do_not_move_a_line_number() -> None:
    patch = "@@ -1,2 +1,2 @@\n a\r\n+é‮\n"
    # old side is one line, new side two: the header says 2/2, so this is mangled
    assert codes(lambda: pos.parse_patch(patch)).code == "upstream_error"
    fine = "@@ -1,1 +1,2 @@\n a\r\n+é‮\n"
    assert ok(C(0, "a.py", 2, "right"), file(patch=fine)).end.kind == "+"


def test_a_huge_line_number_is_just_not_found() -> None:
    assert bad(C(0, "a.py", 2**63, "right"))


# -- the host's parameters ------------------------------------------------------------------------


def test_github_fields_for_a_line_and_a_range() -> None:
    assert ok(C(0, "a.py", 2)).github_fields() == {"path": "a.py", "line": 2, "side": "RIGHT"}
    assert ok(C(0, "a.py", 4, "left", 1)).github_fields() == {
        "path": "a.py",
        "line": 4,
        "side": "LEFT",
        "start_line": 1,
        "start_side": "LEFT",
    }


def sha(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()  # noqa: S324


def test_the_gitlab_line_code_is_the_path_hash_and_the_two_counters() -> None:
    # added line: new 2, the old counter still stands at 2 (the removed line took old 2 first,
    # so after it the counter is 3)
    added = ok(C(0, "a.py", 2, "right")).end
    assert pos.line_code("a.py", added) == f"{sha('a.py')}_3_2"
    context = ok(C(0, "a.py", 4, "right")).end
    assert pos.line_code("a.py", context) == f"{sha('a.py')}_3_4"
    removed = ok(C(0, "a.py", 2, "left")).end
    assert pos.line_code("a.py", removed) == f"{sha('a.py')}_2_2"


def test_a_line_code_of_a_renamed_file_hashes_the_new_path() -> None:
    renamed = file("new.py", previous_path="old.py", status="renamed")
    end = ok(C(0, "new.py", 2), renamed)
    assert pos.line_code(end.path, end.end).startswith(sha("new.py"))


def test_the_gitlab_position_pairs_old_and_new_lines() -> None:
    from forgectl.backends.gitlab_diff import MrPin

    pin = MrPin(101, "user-9/proj", "h" * 40, "b" * 40, "s" * 40, "d" * 40, 1, False)
    context = ok(C(0, "a.py", 4, "left")).gitlab_position(pin)
    assert (context["old_line"], context["new_line"]) == (4, 5)
    assert context["position_type"] == "text"
    assert (context["base_sha"], context["start_sha"], context["head_sha"]) == (
        "b" * 40,
        "s" * 40,
        "d" * 40,
    )
    assert "line_range" not in context
    added = ok(C(0, "a.py", 2, "right")).gitlab_position(pin)
    assert (added["old_line"], added["new_line"]) == (None, 2)
    removed = ok(C(0, "a.py", 2, "left")).gitlab_position(pin)
    assert (removed["old_line"], removed["new_line"]) == (2, None)


def test_the_gitlab_position_of_a_range_carries_line_range() -> None:
    from forgectl.backends.gitlab_diff import MrPin

    pin = MrPin(101, "user-9/proj", "h" * 40, "b" * 40, "s" * 40, "d" * 40, 1, False)
    renamed = file("n.py", previous_path="o.py", status="renamed")
    position = ok(C(0, "n.py", 3, "right", 1), renamed).gitlab_position(pin)
    assert (position["old_path"], position["new_path"]) == ("o.py", "n.py")
    span = position["line_range"]
    assert (span["start"]["type"], span["start"]["new_line"]) == ("old", 1)  # a context line
    assert (span["end"]["type"], span["end"]["new_line"], span["end"]["old_line"]) == (
        "new",
        3,
        None,
    )
    assert span["end"]["line_code"] == position["line_code"]
    assert span["start"]["line_code"] == f"{sha('n.py')}_1_1"


def test_a_range_point_is_new_on_an_added_line_and_old_on_a_removed_or_context_line() -> None:
    from forgectl.backends.gitlab_diff import MrPin

    pin = MrPin(101, "user-9/proj", "h" * 40, "b" * 40, "s" * 40, "d" * 40, 1, False)

    def types(start: int, end: int, side: str = "right") -> tuple[str, str]:
        span = ok(C(0, "a.py", end, side, start)).gitlab_position(pin)["line_range"]
        return span["start"]["type"], span["end"]["type"]

    assert types(1, 2) == ("old", "new")  # context, added
    assert types(2, 4) == ("new", "old")  # added, context
    assert types(2, 3, "left") == ("old", "old")  # removed, context


def test_a_gitlab_position_without_a_start_sha_is_upstream_error() -> None:
    from forgectl.backends.gitlab_diff import MrPin

    pin = MrPin(101, "user-9/proj", "h" * 40, "b" * 40, None, "d" * 40, 1, False)
    assert codes(lambda: ok(C(0, "a.py", 2)).gitlab_position(pin)).code == "upstream_error"


# -- reading the files ----------------------------------------------------------------------------


def test_github_reads_the_pull_files_and_checks(make_github: Callable[..., RouterHarness]) -> None:
    h = make_github(pull=pull(), files=[item("a.py", patch=TWO_HUNKS)])
    checked = pos.check_github(h.env, REF, [C(0, "a.py", 2), C(1, "a.py", 2, "left")])
    assert checked.head_sha == HEAD
    assert [p.end.kind for p in checked.placements] == ["+", "-"]
    assert checked.pin is None


def test_github_finding_is_invalid_position_after_reading(
    make_github: Callable[..., RouterHarness],
) -> None:
    h = make_github(pull=pull(), files=[item("a.py", patch=TWO_HUNKS), item("bin.png", patch=None)])
    del h.script.routes["files"][1]["patch"]
    err = codes(lambda: pos.check_github(h.env, REF, [C(0, "bin.png", 1), C(1, "a.py", 1)]))
    assert err.code == "invalid_position"
    assert [i["index"] for i in err.details["invalid"]] == [0]  # type: ignore[index]


def test_github_moved_head_is_read_once_more(make_github: Callable[..., RouterHarness]) -> None:
    def pulls(n: int) -> Any:
        return pull(head={**pull()["head"], "sha": "4" * 40}) if n == 2 else pull()

    h = make_github(pull=pulls, files=[item("a.py", patch=TWO_HUNKS)])
    assert pos.check_github(h.env, REF, [C(0, "a.py", 2)]).head_sha == HEAD
    assert h.script.kinds.count("files") == 2


def test_github_moved_head_twice_is_upstream_error(
    make_github: Callable[..., RouterHarness],
) -> None:
    def pulls(n: int) -> Any:
        return pull(head={**pull()["head"], "sha": str(n) * 40})

    h = make_github(pull=pulls, files=[item("a.py", patch=TWO_HUNKS)])
    assert codes(lambda: pos.check_github(h.env, REF, [C(0, "a.py", 2)])).code == "upstream_error"


def test_github_a_5xx_on_the_file_list_is_upstream_error(
    make_github: Callable[..., RouterHarness],
) -> None:
    from .test_github_show import http

    h = make_github(pull=pull(), files=http(503, b"{}"))
    err = codes(lambda: pos.check_github(h.env, REF, [C(0, "a.py", 2)]))
    assert (err.code, err.exit_code) == ("upstream_error", 12)


def test_github_a_422_on_the_file_list_is_too_large(
    make_github: Callable[..., RouterHarness],
) -> None:
    from .test_github_show import http

    h = make_github(pull=pull(), files=http(422, b'{"message":"too many"}'))
    err = codes(lambda: pos.check_github(h.env, REF, [C(0, "a.py", 2)]))
    assert (err.code, err.reason) == ("refused", "too_large")


def test_github_a_cut_list_checks_a_listed_file_and_refuses_an_unlisted_one(
    make_github: Callable[..., RouterHarness],
) -> None:
    full = [item(f"f{n}.py", patch=TWO_HUNKS) for n in range(100)]

    def files(n: int) -> Any:
        return [{**f, "filename": f"p{n}-{f['filename']}"} for f in full]

    h = make_github(pull=pull(), files=files)
    listed_path = "p1-f0.py"
    assert pos.check_github(h.env, REF, [C(0, listed_path, 2)]).placements[0].line == 2
    err = codes(lambda: pos.check_github(h.env, REF, [C(0, "never.py", 2)]))
    assert (err.code, err.reason) == ("refused", "too_large")


def test_gitlab_reads_the_diffs_and_builds_the_position(fake_gitlab: Any, tmp_path: Path) -> None:
    backend = one(fake_gitlab, tmp_path, [entry("a.py", TWO_HUNKS)])
    checked = pos.check_gitlab(backend, gl_ref(), [C(0, "a.py", 5, "right", 2)])
    assert checked.pin is not None
    position = checked.placements[0].gitlab_position(checked.pin)
    assert position["head_sha"] == checked.pin.diff_head_sha
    assert position["line_range"]["start"]["new_line"] == 2


def test_gitlab_a_cut_file_is_invalid(fake_gitlab: Any, tmp_path: Path) -> None:
    backend = one(
        fake_gitlab,
        tmp_path,
        [entry("a.py", TWO_HUNKS), entry("big.py", "", too_large=True)],
    )
    err = codes(lambda: pos.check_gitlab(backend, gl_ref(), [C(0, "big.py", 1), C(1, "a.py", 2)]))
    assert err.code == "invalid_position"
    assert [i["index"] for i in err.details["invalid"]] == [0]  # type: ignore[index]


def test_gitlab_a_binary_notice_is_invalid(fake_gitlab: Any, tmp_path: Path) -> None:
    notice = "Binary files a/x.png and b/x.png differ\n"
    backend = one(fake_gitlab, tmp_path, [entry("x.png", notice, new_file=True, a_mode=None)])
    assert codes(lambda: pos.check_gitlab(backend, gl_ref(), [C(0, "x.png", 1)])).code == (
        "invalid_position"
    )


def test_gitlab_a_type_change_keeps_both_entries(fake_gitlab: Any, tmp_path: Path) -> None:
    backend = one(
        fake_gitlab,
        tmp_path,
        [
            entry("link", "@@ -1 +0,0 @@\n-a\n", deleted_file=True, b_mode=None),
            entry("link", "@@ -0,0 +1 @@\n+t\n", new_file=True, a_mode=None),
        ],
    )
    both = pos.check_gitlab(backend, gl_ref(), [C(0, "link", 1, "left"), C(1, "link", 1, "right")])
    assert [p.end.kind for p in both.placements] == ["-", "+"]


def test_gitlab_a_renamed_file_by_its_new_path(fake_gitlab: Any, tmp_path: Path) -> None:
    backend = one(
        fake_gitlab,
        tmp_path,
        [
            entry(
                "new.py",
                "@@ -1 +1 @@\n-a\n+b\n",
                renamed_file=True,
                old_path="old.py",
                new_path="new.py",
            )
        ],
    )
    checked = pos.check_gitlab(backend, gl_ref(), [C(0, "new.py", 1)])
    assert checked.placements[0].previous_path == "old.py"
    assert codes(lambda: pos.check_gitlab(backend, gl_ref(), [C(0, "old.py", 1)])).code == (
        "invalid_position"
    )


def test_gitlab_a_5xx_on_the_diffs_is_upstream_error(fake_gitlab: Any, tmp_path: Path) -> None:
    from .gitlab_support import make_backend, mr_path, recording
    from .test_gitlab_diff import DIFFS

    fake_gitlab.on(mr_path(), mr_doc(changes_count="1"))
    fake_gitlab.on("projects/101", recording("project"))
    fake_gitlab.on(DIFFS, status=503, body={"message": "down"})
    backend = make_backend(fake_gitlab, tmp_path)
    err = codes(lambda: pos.check_gitlab(backend, gl_ref(), [C(0, "a.py", 1)]))
    assert (err.code, err.exit_code) == ("upstream_error", 12)


# -- a list that ended short is a cut list --------------------------------------------------


def _short_gitlab(fake_gitlab: Any, tmp_path: Path, **doc: Any) -> Any:
    from .test_gitlab_diff import setup

    return setup(fake_gitlab, tmp_path, pages=[[entry("a.py", TWO_HUNKS)]], mr=mr_doc(**doc))


@pytest.mark.parametrize("changes", ["1000+", "5"])
def test_gitlab_a_list_that_ended_short_is_cut(
    fake_gitlab: Any, tmp_path: Path, changes: str
) -> None:
    backend = _short_gitlab(fake_gitlab, tmp_path, changes_count=changes)
    err = codes(lambda: pos.check_gitlab(backend, gl_ref(), [C(0, "other.py", 1)]))
    assert (err.code, err.reason) == ("refused", "too_large")
    assert pos.check_gitlab(backend, gl_ref(), [C(0, "a.py", 2)]).placements[0].line == 2
    assert codes(lambda: pos.check_gitlab(backend, gl_ref(), [C(0, "a.py", 99)])).code == (
        "invalid_position"
    )


def test_gitlab_a_complete_list_still_calls_an_unlisted_file_invalid(
    fake_gitlab: Any, tmp_path: Path
) -> None:
    backend = _short_gitlab(fake_gitlab, tmp_path, changes_count="1")
    err = codes(lambda: pos.check_gitlab(backend, gl_ref(), [C(0, "other.py", 1)]))
    assert err.code == "invalid_position"


def test_github_a_list_that_ended_short_is_cut(make_github: Callable[..., RouterHarness]) -> None:
    h = make_github(pull={**pull(), "changed_files": 5}, files=[item("a.py", patch=TWO_HUNKS)])
    err = codes(lambda: pos.check_github(h.env, REF, [C(0, "other.py", 1)]))
    assert (err.code, err.reason) == ("refused", "too_large")
    assert pos.check_github(h.env, REF, [C(0, "a.py", 2)]).placements[0].line == 2
    assert codes(lambda: pos.check_github(h.env, REF, [C(0, "a.py", 99)])).code == (
        "invalid_position"
    )


def test_github_a_complete_list_still_calls_an_unlisted_file_invalid(
    make_github: Callable[..., RouterHarness],
) -> None:
    h = make_github(pull={**pull(), "changed_files": 1}, files=[item("a.py", patch=TWO_HUNKS)])
    err = codes(lambda: pos.check_github(h.env, REF, [C(0, "other.py", 1)]))
    assert err.code == "invalid_position"


# -- only the list's own refusal is too_large -----------------------------------------------


def test_github_a_4xx_on_the_pull_read_is_host_rejected(
    make_github: Callable[..., RouterHarness],
) -> None:
    from .test_github_show import http

    h = make_github(pull=http(422, b'{"message":"no"}'), files=[item("a.py", patch=TWO_HUNKS)])
    err = codes(lambda: pos.check_github(h.env, REF, [C(0, "a.py", 2)]))
    assert err.code == "host_rejected"


def test_gitlab_a_4xx_on_the_mr_read_is_host_rejected(fake_gitlab: Any, tmp_path: Path) -> None:
    from .gitlab_support import make_backend, mr_path

    fake_gitlab.on(mr_path(), status=400, body={"message": "no"})
    backend = make_backend(fake_gitlab, tmp_path)
    err = codes(lambda: pos.check_gitlab(backend, gl_ref(), [C(0, "a.py", 1)]))
    assert err.code == "host_rejected"


def test_gitlab_a_4xx_on_the_diffs_is_too_large(fake_gitlab: Any, tmp_path: Path) -> None:
    from .gitlab_support import make_backend, mr_path, recording
    from .test_gitlab_diff import DIFFS

    fake_gitlab.on(mr_path(), mr_doc(changes_count="1"))
    fake_gitlab.on("projects/101", recording("project"))
    fake_gitlab.on(DIFFS, status=400, body={"message": "too big"})
    backend = make_backend(fake_gitlab, tmp_path)
    err = codes(lambda: pos.check_gitlab(backend, gl_ref(), [C(0, "a.py", 1)]))
    assert (err.code, err.reason) == ("refused", "too_large")
