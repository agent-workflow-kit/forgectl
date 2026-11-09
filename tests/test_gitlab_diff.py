"""GitLab `files`, `diff` and `compare` (#18): assembled diffs, cut files, no silent truncation.

Answers come from hand-built recordings (``tests/recordings/gitlab/diff``) served by an in-process
`glab` through the real gateway. Every GitLab behavior these recordings assume is UNVERIFIED until
the live suites (#31, #35) run; the issue report lists them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forgectl.backends import gitlab_diff as gd
from forgectl.backends.gitlab import GitLabBackend
from forgectl.errors import ForgectlError
from forgectl.gateway import AnswerCut
from forgectl.model import CompareResult, DiffResult, FilesResult

from .gitlab_support import (
    BASE,
    HEAD,
    MR_ID,
    TOKEN,
    Answer,
    FakeGitLab,
    make_backend,
    mr_path,
    recording,
    ref,
)
from .harness import golden

DIFFS = f"{MR_ID}/diffs"
COMPARE = "projects/101/repository/compare"
FROM = "1" * 40
TO = "2" * 40


def diffs_recording() -> list[dict[str, Any]]:
    return recording("diffs", "diff")


def entry(
    path: str = "a.txt",
    diff: str = "@@ -1 +1 @@\n-a\n+b\n",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "old_path": path,
        "new_path": path,
        "a_mode": "100644",
        "b_mode": "100644",
        "diff": diff,
        "new_file": False,
        "renamed_file": False,
        "deleted_file": False,
        "generated_file": False,
        **extra,
    }


def mr_doc(**changes: Any) -> dict[str, Any]:
    doc = recording("mr")
    doc.update(changes)
    return doc


@pytest.fixture
def fake() -> FakeGitLab:
    return FakeGitLab()


def setup(
    fake: FakeGitLab,
    tmp_path: Path,
    *,
    pages: list[list[dict[str, Any]]] | None = None,
    mr: Any = None,
    **kwargs: Any,
) -> GitLabBackend:
    if mr is None or isinstance(mr, dict):
        doc = mr or mr_doc()
        fake.on(mr_path(), doc)
    else:
        fake.on_handler(mr_path(), mr)
    fake.on("projects/101", recording("project"))
    fake.pages(DIFFS, pages if pages is not None else [diffs_recording()])
    return make_backend(fake, tmp_path, **kwargs)


def raises(fn: Any) -> ForgectlError:
    with pytest.raises(ForgectlError) as raised:
        fn()
    return raised.value


def one(fake: FakeGitLab, tmp_path: Path, files: list[dict[str, Any]], **mr: Any) -> Any:
    return setup(fake, tmp_path, pages=[files], mr=mr_doc(changes_count=str(len(files)), **mr))


# -- the assembled text ------------------------------------------------------------------------


def test_the_assembled_diff_text(fake: FakeGitLab, tmp_path: Path) -> None:
    result = setup(fake, tmp_path).diff(ref())
    assert isinstance(result, DiffResult)
    assert result.diff == (
        "diff --git a/src/widget.py b/src/widget.py\n"
        "--- a/src/widget.py\n"
        "+++ b/src/widget.py\n"
        "@@ -1,4 +1,5 @@\n import os\n+import sys\n \n def run():\n-    pass\n+    return 1\n"
        "diff --git a/docs/old name.md b/docs/new name.md\n"
        "rename from docs/old name.md\n"
        "rename to docs/new name.md\n"
        "--- a/docs/old name.md\t\n"
        "+++ b/docs/new name.md\t\n"
        "@@ -1 +1,2 @@\n # Title\n+More text.\n"
        "diff --git a/src/gone.py b/src/gone.py\n"
        "deleted file mode 100755\n"
        "--- a/src/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-print('bye')\n-last\n\\ No newline at end of file\n"
    )
    assert (result.head_sha, result.base_sha, result.merge_base_sha) == (HEAD, BASE, BASE)
    assert result.pr == "gitlab.com/user-9/proj#7"


def test_golden_diff(fake: FakeGitLab, tmp_path: Path) -> None:
    result = setup(fake, tmp_path).diff(ref())
    golden.assert_golden("diff/gitlab-open", result.to_dict(), schema_id="forgectl/cli/diff.json")


def test_golden_files(fake: FakeGitLab, tmp_path: Path) -> None:
    result = setup(fake, tmp_path).files(ref())
    assert isinstance(result, FilesResult)
    golden.assert_golden(
        "files/gitlab-complete", result.to_dict(), schema_id="forgectl/cli/files.json"
    )
    assert result.files_complete is True and result.changed_files == 3


def test_files_statuses_counts_and_previous_path(fake: FakeGitLab, tmp_path: Path) -> None:
    by = {f.path: f for f in setup(fake, tmp_path).files(ref()).files}
    assert (by["src/widget.py"].additions, by["src/widget.py"].deletions) == (2, 1)
    assert by["docs/new name.md"].status == "renamed"
    assert by["docs/new name.md"].previous_path == "docs/old name.md"
    assert by["src/gone.py"].status == "deleted"
    assert by["src/gone.py"].previous_path is None


def test_added_file_modes_and_dev_null(fake: FakeGitLab, tmp_path: Path) -> None:
    new = entry(
        "n.sh",
        "@@ -0,0 +1 @@\n+x\n",
        new_file=True,
        a_mode="0",
        b_mode="100755",
    )
    text = one(fake, tmp_path, [new]).diff(ref()).diff
    assert text == (
        "diff --git a/n.sh b/n.sh\nnew file mode 100755\n--- /dev/null\n+++ b/n.sh\n"
        "@@ -0,0 +1 @@\n+x\n"
    )


def test_a_mode_change_with_content_and_without(fake: FakeGitLab, tmp_path: Path) -> None:
    with_text = entry("m.sh", b_mode="100755")
    mode_only = entry("n.sh", "", b_mode="100755")
    text = one(fake, tmp_path, [with_text, mode_only]).diff(ref()).diff
    assert text == (
        "diff --git a/m.sh b/m.sh\nold mode 100644\nnew mode 100755\n--- a/m.sh\n+++ b/m.sh\n"
        "@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/n.sh b/n.sh\nold mode 100644\nnew mode 100755\n"
    )


def test_a_pure_rename_has_no_content_headers(fake: FakeGitLab, tmp_path: Path) -> None:
    pure = entry("new.txt", "", renamed_file=True, old_path="old.txt")
    text = one(fake, tmp_path, [pure]).diff(ref()).diff
    assert text == "diff --git a/old.txt b/new.txt\nrename from old.txt\nrename to new.txt\n"


def test_an_empty_added_file_is_listed_with_its_mode(fake: FakeGitLab, tmp_path: Path) -> None:
    empty = entry("e.txt", "", new_file=True, a_mode="0", b_mode="100644")
    text = one(fake, tmp_path, [empty]).diff(ref()).diff
    assert text == "diff --git a/e.txt b/e.txt\nnew file mode 100644\n"


@pytest.mark.parametrize(
    "path,header",
    [
        ("plain/ok.txt", "diff --git a/plain/ok.txt b/plain/ok.txt"),
        ("with space.txt", "diff --git a/with space.txt b/with space.txt"),
        ("a\nb.txt", 'diff --git "a/a\\nb.txt" "b/a\\nb.txt"'),
        ('q"uote', 'diff --git "a/q\\"uote" "b/q\\"uote"'),
        ("back\\slash", 'diff --git "a/back\\\\slash" "b/back\\\\slash"'),
        ("tab\there", 'diff --git "a/tab\\there" "b/tab\\there"'),
        ("café.txt", 'diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"'),
        ("esc\x1b[2J", 'diff --git "a/esc\\033[2J" "b/esc\\033[2J"'),
        ("e\u202edoc", 'diff --git "a/e\\342\\200\\256doc" "b/e\\342\\200\\256doc"'),
    ],
)
def test_paths_are_quoted_as_git_quotes_them(
    fake: FakeGitLab, tmp_path: Path, path: str, header: str
) -> None:
    text = one(fake, tmp_path, [entry(path)]).diff(ref()).diff
    lines = text.split("\n")
    assert lines[0] == header
    assert lines[1].startswith("--- ") and lines[2].startswith("+++ ")
    assert len([ln for ln in lines if ln.startswith("diff --git ")]) == 1  # never forged
    assert text.isascii()


def test_quote_path_exact_forms() -> None:
    assert gd.quote_path("a/x") == "a/x"
    assert gd.quote_path("a/x\ny") == '"a/x\\ny"'
    assert gd.quote_path("b/café") == '"b/caf\\303\\251"'
    assert gd.quote_path('a/"') == '"a/\\""'


def test_a_newline_path_cannot_forge_a_header(fake: FakeGitLab, tmp_path: Path) -> None:
    evil = "x\ndiff --git a/etc/passwd b/etc/passwd\n+++ b/etc/passwd"
    text = one(fake, tmp_path, [entry(evil)]).diff(ref()).diff
    assert [ln for ln in text.split("\n") if ln.startswith("diff --git ")] == [
        ln for ln in text.split("\n")[:1]
    ]


def test_diff_text_is_sanitized_and_keeps_its_line_count(fake: FakeGitLab, tmp_path: Path) -> None:
    body = "@@ -1,2 +1,2 @@\n a\r\n-b\x1b[31m\n+c\x00\r\n"
    text = one(fake, tmp_path, [entry("a.txt", body)]).diff(ref()).diff
    assert "\x1b" not in text and "\x00" not in text
    assert "a\r\n" in text  # a CR before LF stays
    assert "<U+001B>" in text and "<U+0000>" in text
    assert text.count("\n") == 3 + body.count("\n")


# -- cut files ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cut",
    [
        entry("a.txt", "", too_large=True),
        entry("a.txt", "@@ -1 +1 @@\n-a\n+b\n", collapsed=True),
        entry("a.txt", ""),  # modified, empty, nothing else changed: a binary file looks so
        entry("a.txt", "", generated_file=True, new_file=True, a_mode="0"),
        entry("a.txt", "@@ -1,5 +1,5 @@\n a\n-b\n+c\n"),  # ends inside a hunk
    ],
)
def test_a_cut_file_is_too_large_for_diff(fake: FakeGitLab, tmp_path: Path, cut: Any) -> None:
    error = raises(lambda: one(fake, tmp_path, [entry("ok.txt"), cut]).diff(ref()))
    assert (error.code, error.reason) == ("refused", "too_large")


def test_a_cut_file_is_listed_without_counts_by_files(fake: FakeGitLab, tmp_path: Path) -> None:
    cut = entry("big.bin", "", too_large=True)
    result = one(fake, tmp_path, [entry("ok.txt"), cut]).files(ref())
    by = {f.path: f for f in result.files}
    assert (by["big.bin"].additions, by["big.bin"].deletions) == (None, None)
    assert (by["ok.txt"].additions, by["ok.txt"].deletions) == (1, 1)
    assert result.files_complete is True


def test_raw_files_give_no_patch_for_a_cut_file(fake: FakeGitLab, tmp_path: Path) -> None:
    cut = entry("big.bin", "", too_large=True)
    backend = one(fake, tmp_path, [entry("ok.txt"), cut])
    _, raw = gd.read_raw_files(backend, ref())
    by = {f.path: f for f in raw.files}
    assert by["big.bin"].patch is None
    assert by["ok.txt"].patch == "@@ -1 +1 @@\n-a\n+b\n"
    assert raw.ended is True


def test_a_modified_file_with_a_rename_and_no_fragment_is_not_cut(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    pure = entry("new.txt", "", renamed_file=True, old_path="old.txt")
    assert one(fake, tmp_path, [pure]).files(ref()).files[0].status == "renamed"


# -- hostile entries ---------------------------------------------------------------------------

GOOD = "@@ -1 +1 @@\n-a\n+b\n"


@pytest.mark.parametrize(
    "bad",
    [
        "not an object",
        entry("a", new_file=True, deleted_file=True),
        entry("a", new_file="yes"),
        entry("a", too_large="yes"),
        {k: v for k, v in entry("a").items() if k != "diff"},
        {**entry("a"), "diff": 5},
        {**entry("a"), "diff": "\ud800"},
        {**entry("a"), "old_path": "b"},  # a path change without a rename flag
        entry("a", renamed_file=True),  # a rename flag with the same path
        entry(""),
        entry("/etc/passwd"),
        entry("a/../../b"),
        entry("a//b"),
        entry("./a"),
        entry("a\x00b"),
        entry("x" * 5000),
        {**entry("a"), "new_path": 7},
        {**entry("a"), "a_mode": "xyz"},
        {**entry("a"), "b_mode": 100644},
        # fragments that are not hunks, or hold lines outside their counts
        entry("a", "diff --git a/x b/x\n"),
        entry("a", GOOD + "diff --git a/etc/passwd b/etc/passwd\n"),
        entry("a", GOOD + "--- a/etc/passwd\n+++ b/etc/passwd\n"),
        entry("a", GOOD + "+one more\n"),
        entry("a", "@@ -1 +1 @@\n-a\n+b\n\n"),  # an empty line is no hunk line
        entry("a", "@@ -1 +1 @@\n-a\n+b\n@@ not a header\n"),
        entry("a", "@@ -1 +1 @@\n\\ stray first\n-a\n+b\n"),
        entry("a", "plain text\n"),
        entry("a", "@@ -1 +1 @@\n-a\n+b\n-c\n"),
        # binary forms other than the one-line notice
        entry("a", "GIT binary patch\nliteral 5\nMcmZ?\n\nliteral 0\nHcmV?d00001\n"),
        entry("a", "Binary files a/a and b/a differ extra\n"),
        entry("a", "Binary files a/a and b/a differ\n@@ -1 +1 @@\n-a\n+b\n"),
        entry("a", "Binary files x/a and y/a differ\n"),
        # modes: git's set only, and consistent with the new and deleted flags
        entry("a", "@@ -0,0 +1 @@\n+x\n", new_file=True, a_mode="0", b_mode="0"),
        {
            k: v
            for k, v in entry("a", "@@ -1 +0,0 @@\n-x\n", deleted_file=True, b_mode="0").items()
            if k != "a_mode"
        },
        entry("a", "@@ -0,0 +1 @@\n+x\n", new_file=True),  # added, with an old mode
        entry("a", "@@ -1 +0,0 @@\n-x\n", deleted_file=True),  # deleted, with a new mode
        entry("a", b_mode="0"),  # modified with no new mode
        {**entry("a"), "a_mode": "7"},
        {**entry("a"), "b_mode": "0100644"},
        {**entry("a"), "b_mode": "100600"},
        # hunks outside git's grammar
        entry("a", "@@ -1 +1 @@\n-a\n\\ No newline\n+b\n"),
        entry(
            "a",
            "@@ -1 +1 @@\n-a\n\\ No newline at end of file\n\\ No newline at end of file\n+b\n",
        ),
        entry(
            "a",
            "@@ -1,2 +1 @@\n-a\n\\ No newline at end of file\n-b\n"
            "\\ No newline at end of file\n+c\n",
        ),
        entry("a", "@@ -1,0 +1,0 @@\n"),
        entry("a", "@@ -1 +1 @@\n-a\n+b\n", new_file=True, a_mode="0"),
        entry("a", "@@ -0 +1 @@\n+b\n", new_file=True, a_mode="0"),
        entry("a", "@@ -1 +1 @@\n-a\n+b\n", deleted_file=True, b_mode="0"),
        entry("a", "@@ -1,2 +1,2 @@\n a\n-b\n+c\n@@ -2 +2 @@\n-x\n+y\n"),  # overlap
        entry("a", "@@ -5 +5 @@\n-a\n+b\n@@ -1 +1 @@\n-c\n+d\n"),  # reordered
        entry("a", "@@ -1 +5 @@\n-a\n+b\n@@ -3 +1 @@\n-c\n+d\n"),  # new side reordered
    ],
)
def test_a_hostile_entry_is_upstream_error(fake: FakeGitLab, tmp_path: Path, bad: Any) -> None:
    backend = setup(fake, tmp_path, pages=[[bad]], mr=mr_doc(changes_count="1"))
    assert raises(lambda: backend.diff(ref())).code == "upstream_error"
    assert raises(lambda: backend.files(ref())).code == "upstream_error"


def test_a_file_listed_twice_on_one_page_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = setup(fake, tmp_path, pages=[[entry("a"), entry("a")]], mr=mr_doc(changes_count="2"))
    assert raises(lambda: backend.files(ref())).code == "upstream_error"


def test_a_page_longer_than_the_page_size_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    page = [entry(f"f{i}") for i in range(101)]
    backend = setup(fake, tmp_path, pages=[page], mr=mr_doc(changes_count="101"))
    assert raises(lambda: backend.files(ref())).code == "upstream_error"


def test_a_diff_answer_that_is_not_a_list_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = setup(fake, tmp_path)
    fake.on(DIFFS, {"message": "surprise"}, headers={"x-next-page": ""})
    assert raises(lambda: backend.diff(ref())).code == "upstream_error"


# -- paging ------------------------------------------------------------------------------------


def paged(count: int, size: int = 100) -> list[list[dict[str, Any]]]:
    names = [f"f{i}.txt" for i in range(count)]
    return [[entry(n) for n in names[i : i + size]] for i in range(0, count, size)]


def test_pages_use_per_page_100_and_x_next_page(fake: FakeGitLab, tmp_path: Path) -> None:
    backend = setup(fake, tmp_path, pages=paged(120), mr=mr_doc(changes_count="120"))
    result = backend.files(ref())
    assert result.files_complete is True and len(result.files) == 120
    sent = [c.endpoint for c in fake.calls if c.path == DIFFS]
    assert sent == [f"{DIFFS}?per_page=100&page=1", f"{DIFFS}?per_page=100&page=2"]


def test_a_full_page_with_no_header_asks_for_the_next(fake: FakeGitLab, tmp_path: Path) -> None:
    backend = setup(fake, tmp_path, mr=mr_doc(changes_count="100"))
    fake.on_handler(
        DIFFS,
        lambda q: Answer(200, [entry(f"f{i}") for i in range(100)] if q["page"] == "1" else []),
    )
    assert backend.files(ref()).files_complete is True
    assert fake.count(DIFFS) == 2


@pytest.mark.parametrize("header", ["3", "1", "0", "-1", "abc", "2 ", "02"])
def test_a_wrong_x_next_page_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path, header: str
) -> None:
    backend = setup(fake, tmp_path, mr=mr_doc(changes_count="100"))
    fake.on(DIFFS, [entry(f"f{i}") for i in range(100)], headers={"x-next-page": header})
    assert raises(lambda: backend.files(ref())).code == "upstream_error"


def test_a_list_that_never_ends_stops_at_the_page_cap_and_is_incomplete(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = setup(fake, tmp_path, mr=mr_doc(changes_count="100000"))
    fake.on_handler(
        DIFFS,
        lambda q: Answer(
            200,
            [entry(f"p{q['page']}-{i}") for i in range(100)],
            {"x-next-page": str(int(q["page"]) + 1)},
        ),
    )
    warned: list[str] = []
    backend._warn_to = warned.append
    result = backend.files(ref())
    assert fake.count(DIFFS) == gd.MAX_FILE_PAGES
    assert result.files_complete is False and len(result.files) == 100 * gd.MAX_FILE_PAGES
    assert warned
    error = raises(lambda: backend.diff(ref()))
    assert (error.code, error.reason) == ("refused", "too_large")


def test_a_file_on_two_pages_is_a_shifted_list_then_upstream_error(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    page = [entry(f"f{i}") for i in range(100)]
    backend = setup(fake, tmp_path, pages=[page, [entry("f0")]], mr=mr_doc(changes_count="101"))
    assert raises(lambda: backend.files(ref())).code == "upstream_error"
    assert fake.count(mr_path()) == 2  # one more try, never a third


def test_changes_count_over_the_limit_is_incomplete_and_too_large(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = setup(fake, tmp_path, pages=paged(3), mr=mr_doc(changes_count="1000+"))
    assert backend.files(ref()).files_complete is False
    assert raises(lambda: backend.diff(ref())).reason == "too_large"


def test_a_changes_count_that_disagrees_marks_files_incomplete(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = setup(fake, tmp_path, pages=paged(3), mr=mr_doc(changes_count="5"))
    result = backend.files(ref())
    assert result.files_complete is False and result.changed_files == 3


def test_diff_with_a_disagreeing_changes_count_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = setup(fake, tmp_path, pages=paged(3), mr=mr_doc(changes_count="5"))
    assert raises(lambda: backend.diff(ref())).code == "upstream_error"


def test_a_null_changes_count_with_no_files_is_an_empty_diff(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = setup(fake, tmp_path, pages=[[]], mr=mr_doc(changes_count=None))
    assert backend.diff(ref()).diff == ""
    assert backend.files(ref()).files_complete is True


@pytest.mark.parametrize("count", ["x", "-1", "1e3", True, 1.5, [], "1000++"])
def test_a_malformed_changes_count_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path, count: Any
) -> None:
    backend = setup(fake, tmp_path, mr=mr_doc(changes_count=count))
    assert raises(lambda: backend.files(ref())).code == "upstream_error"


# -- the MR read -------------------------------------------------------------------------------


def test_the_mr_is_read_before_and_after_and_endpoints_use_the_project_id(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    setup(fake, tmp_path).diff(ref())
    paths = [c.path for c in fake.calls]
    assert paths == [mr_path(), "projects/101", DIFFS, mr_path()]
    assert all(TOKEN not in c.endpoint for c in fake.calls)


def test_a_head_that_moved_fetches_once_more_then_succeeds(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    # the first "after" read sees a new head; the redo (reads 3 and 4) is stable
    seen = {"n": 0}

    def handler(_q: dict[str, str]) -> Answer:
        seen["n"] += 1
        if seen["n"] == 2:
            sha = "4" * 40
            return Answer(
                200, mr_doc(sha=sha, diff_refs={**mr_doc()["diff_refs"], "head_sha": sha})
            )
        return Answer(200, mr_doc())

    backend = setup(fake, tmp_path, mr=handler)
    assert backend.files(ref()).head_sha == HEAD
    assert fake.count(DIFFS) == 2


def test_a_head_that_moved_twice_is_upstream_error(fake: FakeGitLab, tmp_path: Path) -> None:
    seen = {"n": 0}

    def handler(_q: dict[str, str]) -> Answer:
        seen["n"] += 1
        sha = f"{seen['n']}" * 40
        return Answer(200, mr_doc(sha=sha, diff_refs={**mr_doc()["diff_refs"], "head_sha": sha}))

    backend = setup(fake, tmp_path, mr=handler)
    assert raises(lambda: backend.diff(ref())).code == "upstream_error"
    assert fake.count(mr_path()) == 4


def test_a_moved_base_is_a_moved_read(fake: FakeGitLab, tmp_path: Path) -> None:
    seen = {"n": 0}

    def handler(_q: dict[str, str]) -> Answer:
        seen["n"] += 1
        base = f"{seen['n']}" * 40
        return Answer(200, mr_doc(diff_refs={**mr_doc()["diff_refs"], "base_sha": base}))

    backend = setup(fake, tmp_path, mr=handler)
    assert raises(lambda: backend.diff(ref())).code == "upstream_error"


def test_diff_refs_of_another_head_than_the_mr_is_never_read(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    stale = mr_doc(diff_refs={**mr_doc()["diff_refs"], "head_sha": "9" * 40})
    backend = setup(fake, tmp_path, mr=stale)
    assert raises(lambda: backend.diff(ref())).code == "upstream_error"
    assert fake.count(DIFFS) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"diff_refs": None},
        {"diff_refs": "x"},
        {"diff_refs": {"base_sha": "short", "head_sha": HEAD}},
        {"sha": "not a sha"},
        {"project_id": "101"},
        {"target_project_id": 202},
    ],
)
def test_an_mr_answer_that_is_not_usable_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path, changes: dict[str, Any]
) -> None:
    backend = setup(fake, tmp_path, mr=mr_doc(**changes))
    assert raises(lambda: backend.diff(ref())).code == "upstream_error"


def test_an_mr_answer_of_another_number_is_wrong_host_response(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = setup(fake, tmp_path, mr=mr_doc(iid=8))
    assert raises(lambda: backend.diff(ref())).code == "wrong_host_response"


def test_an_mr_of_another_host_is_wrong_host_response(fake: FakeGitLab, tmp_path: Path) -> None:
    backend = setup(
        fake, tmp_path, mr=mr_doc(web_url="https://evil.example/user-9/proj/-/merge_requests/7")
    )
    assert raises(lambda: backend.files(ref())).code == "wrong_host_response"


def test_a_project_of_another_path_is_wrong_host_response(fake: FakeGitLab, tmp_path: Path) -> None:
    backend = setup(fake, tmp_path)
    project = {**recording("project"), "path_with_namespace": "other/proj"}
    fake.on("projects/101", project)
    assert raises(lambda: backend.diff(ref())).code == "wrong_host_response"


def test_a_missing_mr_is_not_found(fake: FakeGitLab, tmp_path: Path) -> None:
    backend = setup(fake, tmp_path)
    fake.on(mr_path(), {"message": "404 Not found"}, status=404)
    assert raises(lambda: backend.files(ref())).code == "not_found"


def test_the_pr_name_uses_the_canonical_path_of_the_mr(fake: FakeGitLab, tmp_path: Path) -> None:
    mr = mr_doc(web_url="https://gitlab.com/user-9/renamed/-/merge_requests/7")
    backend = setup(fake, tmp_path, mr=mr)
    fake.on(
        "projects/101",
        {
            **recording("project"),
            "path_with_namespace": "user-9/renamed",
            "web_url": "https://gitlab.com/user-9/renamed",
        },
    )
    assert backend.files(ref()).pr == "gitlab.com/user-9/renamed#7"


def test_diffs_404_on_an_old_host_is_unsupported_host_version(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = setup(fake, tmp_path)
    fake.on(DIFFS, {"message": "404 Not Found"}, status=404)
    fake.on("version", {"version": "15.6.2", "revision": "x"})
    assert raises(lambda: backend.files(ref())).code == "unsupported_host_version"


def test_diffs_404_with_an_unreadable_version_is_unsupported_host_version(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = setup(fake, tmp_path)
    fake.on(DIFFS, {"message": "404 Not Found"}, status=404)
    assert raises(lambda: backend.diff(ref())).code == "unsupported_host_version"


def test_diffs_404_on_a_new_host_is_not_found(fake: FakeGitLab, tmp_path: Path) -> None:
    backend = setup(fake, tmp_path)
    fake.on(DIFFS, {"message": "404 Not Found"}, status=404)
    fake.on("version", {"version": "17.2.0", "revision": "x"})
    assert raises(lambda: backend.files(ref())).code == "not_found"


def test_a_server_error_on_a_page_is_retried_like_any_read(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def flaky(_q: dict[str, str]) -> Answer:
        calls["n"] += 1
        return (
            Answer(502, {"message": "bad gateway"})
            if calls["n"] == 1
            else Answer(200, diffs_recording(), {"x-next-page": ""})
        )

    retries: list[int] = []

    def retry(fn: Any) -> Any:
        try:
            return fn()
        except ForgectlError:
            retries.append(1)
            return fn()

    backend = setup(fake, tmp_path, retry=retry)
    fake.on_handler(DIFFS, flaky)
    assert backend.files(ref()).files_complete is True
    assert retries


def test_a_malformed_page_is_retried_inside_the_retried_call(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def bad_then_good(_q: dict[str, str]) -> Answer:
        calls["n"] += 1
        body: Any = {"oops": 1} if calls["n"] == 1 else diffs_recording()
        return Answer(200, body, {"x-next-page": ""})

    def retry(fn: Any) -> Any:
        try:
            return fn()
        except ForgectlError:
            return fn()

    backend = setup(fake, tmp_path, retry=retry)
    fake.on_handler(DIFFS, bad_then_good)
    assert backend.files(ref()).files_complete is True
    assert calls["n"] == 2


def test_an_answer_past_the_read_cap_is_too_large_and_never_retried(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = setup(fake, tmp_path)
    api = backend._gateway.api
    attempts: list[int] = []

    def cut_api(target: Any, method: str, segments: Any, **kw: Any) -> Any:
        if segments[-1] == "diffs":
            attempts.append(1)
            raise AnswerCut("upstream_error", "the answer went past the read cap")
        return api(target, method, segments, **kw)

    backend._gateway.api = cut_api  # type: ignore[method-assign]
    fake.on(DIFFS, diffs_recording(), headers={"x-next-page": ""})
    error = raises(lambda: backend.diff(ref()))
    assert (error.code, error.reason) == ("refused", "too_large")
    assert len(attempts) == 1


# -- compare -----------------------------------------------------------------------------------


def commit(char: str) -> dict[str, Any]:
    return {"id": char * 40, "short_id": char * 8, "title": "t"}


def cmp_doc(
    to: str, commits: list[str], diffs: list[Any] | None = None, **extra: Any
) -> dict[str, Any]:
    doc = {
        "commit": commit(to[0]) if commits else None,
        "commits": [commit(c) for c in commits],
        "diffs": diffs if diffs is not None else [],
        "compare_timeout": False,
        "compare_same_ref": False,
    }
    doc.update(extra)
    return doc


def compare_routes(fake: FakeGitLab, forward: Any, backward: Any) -> None:
    def handler(q: dict[str, str]) -> Answer:
        assert q["straight"] == "false"
        doc = forward if (q["from"], q["to"]) == (FROM, TO) else backward
        if isinstance(doc, Answer):
            return doc
        return Answer(200, doc)

    fake.on_handler(COMPARE, handler)


def compare(fake: FakeGitLab, tmp_path: Path, forward: Any, backward: Any, **kw: Any) -> Any:
    backend = setup(fake, tmp_path, **kw)
    compare_routes(fake, forward, backward)
    return backend.compare(ref(), FROM, TO)


def test_compare_ahead_has_the_assembled_diff(fake: FakeGitLab, tmp_path: Path) -> None:
    forward = cmp_doc("2", ["2", "4"], [entry("a.txt")])
    result = compare(fake, tmp_path, forward, cmp_doc("1", []))
    assert isinstance(result, CompareResult)
    assert result.status == "ahead"
    assert result.diff == (
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+b\n"
    )
    golden.assert_golden(
        "compare/gitlab-ahead", result.to_dict(), schema_id="forgectl/cli/compare.json"
    )


def test_compare_asks_both_ways_in_the_target_project(fake: FakeGitLab, tmp_path: Path) -> None:
    compare(fake, tmp_path, cmp_doc("2", ["2"]), cmp_doc("1", []))
    sent = [c.endpoint for c in fake.calls if c.path == COMPARE]
    assert sent == [
        f"{COMPARE}?from={FROM}&to={TO}&straight=false",
        f"{COMPARE}?from={TO}&to={FROM}&straight=false",
    ]


def test_compare_behind(fake: FakeGitLab, tmp_path: Path) -> None:
    result = compare(fake, tmp_path, cmp_doc("2", []), cmp_doc("1", ["1"]))
    assert (result.status, result.diff) == ("behind", None)


def test_compare_diverged_has_no_diff(fake: FakeGitLab, tmp_path: Path) -> None:
    result = compare(
        fake, tmp_path, cmp_doc("2", ["2"], [entry("a.txt")]), cmp_doc("1", ["1"], [entry("b")])
    )
    assert (result.status, result.diff) == ("diverged", None)


def test_compare_with_the_same_sha_is_identical_without_a_compare_call(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = setup(fake, tmp_path)
    result = backend.compare(ref(), FROM, FROM)
    assert (result.status, result.diff) == ("identical", "")
    assert fake.count(COMPARE) == 0
    assert fake.count(mr_path()) == 1  # the MR is still read: not_found, the canonical path


def test_compare_identical_needs_the_hosts_own_same_ref_both_ways(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    same = cmp_doc("2", [], compare_same_ref=True)
    result = compare(fake, tmp_path, same, {**same, "commit": None})
    assert (result.status, result.diff) == ("identical", "")


def test_compare_same_ref_one_way_or_with_commits_is_unavailable(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    one_way = compare(fake, tmp_path, cmp_doc("2", [], compare_same_ref=True), cmp_doc("1", []))
    assert one_way.status == "unavailable"
    both = compare(
        FakeGitLab(),
        tmp_path / "x",
        cmp_doc("2", ["2"], compare_same_ref=True),
        cmp_doc("1", [], compare_same_ref=True),
    )
    assert both.status == "unavailable"


def test_compare_no_commits_either_way_for_different_shas_is_unavailable(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    result = compare(fake, tmp_path, cmp_doc("2", []), cmp_doc("1", []))
    assert (result.status, result.diff) == ("unavailable", None)


@pytest.mark.parametrize("which", ["forward", "backward"])
def test_compare_404_is_unavailable(fake: FakeGitLab, tmp_path: Path, which: str) -> None:
    missing = Answer(404, {"message": "404 Commit Not Found"})
    forward = missing if which == "forward" else cmp_doc("2", ["2"])
    backward = missing if which == "backward" else cmp_doc("1", [])
    result = compare(fake, tmp_path, forward, backward)
    assert (result.status, result.diff) == ("unavailable", None)


@pytest.mark.parametrize("which", ["forward", "backward"])
def test_compare_timeout_is_too_large(fake: FakeGitLab, tmp_path: Path, which: str) -> None:
    forward = cmp_doc("2", ["2"], compare_timeout=which == "forward")
    backward = cmp_doc("1", [], compare_timeout=which == "backward")
    error = raises(lambda: compare(fake, tmp_path, forward, backward))
    assert (error.code, error.reason) == ("refused", "too_large")


def test_compare_with_a_cut_file_is_too_large(fake: FakeGitLab, tmp_path: Path) -> None:
    forward = cmp_doc("2", ["2"], [entry("a", "", too_large=True)])
    error = raises(lambda: compare(fake, tmp_path, forward, cmp_doc("1", [])))
    assert (error.code, error.reason) == ("refused", "too_large")


def test_compare_with_as_many_files_as_the_cap_is_too_large(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    files = [entry(f"f{i}") for i in range(gd.COMPARE_FILE_CAP)]
    error = raises(lambda: compare(fake, tmp_path, cmp_doc("2", ["2"], files), cmp_doc("1", [])))
    assert error.reason == "too_large"


@pytest.mark.parametrize(
    "doc",
    [
        "x",
        {**cmp_doc("2", ["2"]), "commits": "x"},
        {**cmp_doc("2", ["2"]), "commits": [{"id": "nope"}]},
        {**cmp_doc("2", ["2"]), "commit": {"id": "5" * 40}},  # not the sha asked for
        {**cmp_doc("2", ["2"]), "commit": None},
        {**cmp_doc("2", ["2"]), "diffs": {}},
        {**cmp_doc("2", ["2"]), "compare_timeout": "no"},
        {k: v for k, v in cmp_doc("2", ["2"]).items() if k != "compare_same_ref"},
        {**cmp_doc("2", ["2"], [entry("a", "diff --git a/x b/x\n")])},
    ],
)
def test_a_hostile_compare_answer_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path, doc: Any
) -> None:
    error = raises(lambda: compare(fake, tmp_path, doc, cmp_doc("1", [])))
    assert error.code == "upstream_error"


def test_a_compare_answer_of_another_host_is_wrong_host_response(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    forward = cmp_doc("2", ["2"], web_url="https://evil.example/user-9/proj/-/compare/x...y")
    assert raises(lambda: compare(fake, tmp_path, forward, cmp_doc("1", []))).code == (
        "wrong_host_response"
    )


def test_compare_of_a_missing_mr_is_not_found(fake: FakeGitLab, tmp_path: Path) -> None:
    backend = setup(fake, tmp_path)
    fake.on(mr_path(), {"message": "404"}, status=404)
    assert raises(lambda: backend.compare(ref(), FROM, TO)).code == "not_found"


def test_the_recorded_compare_answers_parse(fake: FakeGitLab, tmp_path: Path) -> None:
    ahead = recording("compare-ahead", "diff")
    ahead["commit"]["id"] = TO
    ahead["commits"][0]["id"] = TO
    ahead["web_url"] = "https://gitlab.com/user-9/proj/-/compare/a...b"
    result = compare(fake, tmp_path, ahead, recording("compare-empty", "diff"))
    assert result.status == "ahead" and result.diff.startswith("diff --git a/src/widget.py")


# -- round 1 review ----------------------------------------------------------------------------


def test_files_and_raw_files_have_no_size_cap_but_diff_does(
    fake: FakeGitLab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gd, "MAX_STDOUT", 10)
    backend = setup(fake, tmp_path)
    assert backend.files(ref()).files_complete is True
    _, raw = gd.read_raw_files(backend, ref())
    assert raw.ended is True and len(raw.files) == 3
    error = raises(lambda: backend.diff(ref()))
    assert (error.code, error.reason) == ("refused", "too_large")


def cut_reads(backend: GitLabBackend, cut: Any) -> list[str]:
    """Make the gateway raise AnswerCut on the reads ``cut(segments, query)`` picks."""
    api = backend._gateway.api
    attempts: list[str] = []

    def cut_api(target: Any, method: str, segments: Any, **kw: Any) -> Any:
        if cut(segments, dict(kw.get("query", ()))):
            attempts.append(segments[-1])
            raise AnswerCut("upstream_error", "the answer went past the read cap")
        return api(target, method, segments, **kw)

    backend._gateway.api = cut_api  # type: ignore[method-assign]
    return attempts


def test_a_page_past_the_read_cap_is_upstream_error_for_files(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    backend = setup(fake, tmp_path)
    cut_reads(backend, lambda segments, _q: segments[-1] == "diffs")
    error = raises(lambda: backend.files(ref()))
    assert (error.code, error.reason) == ("upstream_error", None)
    error = raises(lambda: gd.read_raw_files(backend, ref()))
    assert (error.code, error.reason) == ("upstream_error", None)


@pytest.mark.parametrize(
    "changes", [{"diff_refs": None}, {"changes_count": "garbage"}, {"sha": None}]
)
def test_compare_reads_only_the_mr_identity(
    fake: FakeGitLab, tmp_path: Path, changes: dict[str, Any]
) -> None:
    forward = cmp_doc("2", ["2"], [entry("a.txt")])
    result = compare(fake, tmp_path, forward, cmp_doc("1", []), mr=mr_doc(**changes))
    assert result.status == "ahead"


@pytest.mark.parametrize("path", ["with space.txt", "trail ", 'q"u te'])
def test_a_path_with_a_space_ends_its_content_headers_with_a_tab(
    fake: FakeGitLab, tmp_path: Path, path: str
) -> None:
    lines = one(fake, tmp_path, [entry(path)]).diff(ref()).diff.split("\n")
    a, b = gd.quote_path(f"a/{path}"), gd.quote_path(f"b/{path}")
    assert lines[1:3] == [f"--- {a}\t", f"+++ {b}\t"]


def test_a_path_without_a_space_has_no_tab(fake: FakeGitLab, tmp_path: Path) -> None:
    lines = one(fake, tmp_path, [entry("plain.txt")]).diff(ref()).diff.split("\n")
    assert lines[1:3] == ["--- a/plain.txt", "+++ b/plain.txt"]


def test_dev_null_never_takes_a_tab(fake: FakeGitLab, tmp_path: Path) -> None:
    new = entry("x y", "@@ -0,0 +1 @@\n+x\n", new_file=True, a_mode="0")
    gone = entry("p q", "@@ -1 +0,0 @@\n-x\n", deleted_file=True, b_mode="0")
    text = one(fake, tmp_path, [new, gone]).diff(ref()).diff
    assert "--- /dev/null\n+++ b/x y\t\n" in text
    assert "--- a/p q\t\n+++ /dev/null\n" in text


def test_a_malformed_compare_entry_is_retried_inside_the_retried_call(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def handler(q: dict[str, str]) -> Answer:
        if (q["from"], q["to"]) != (FROM, TO):
            return Answer(200, cmp_doc("1", []))
        calls["n"] += 1
        diffs = [entry("a", "plain text\n")] if calls["n"] == 1 else [entry("a.txt")]
        return Answer(200, cmp_doc("2", ["2"], diffs))

    def retry(fn: Any) -> Any:
        try:
            return fn()
        except ForgectlError:
            return fn()

    backend = setup(fake, tmp_path, retry=retry)
    fake.on_handler(COMPARE, handler)
    assert backend.compare(ref(), FROM, TO).status == "ahead"
    assert calls["n"] == 2


NOTICE = "Binary files a/img.png and b/img.png differ\n"


@pytest.mark.parametrize(
    "notice",
    [NOTICE, NOTICE.rstrip("\n"), 'Binary files "a/caf\\303\\251" and "b/caf\\303\\251" differ'],
)
def test_a_modified_binary_notice_is_cut(fake: FakeGitLab, tmp_path: Path, notice: str) -> None:
    path = "café" if "caf" in notice else "img.png"
    backend = one(fake, tmp_path, [entry("ok.txt"), entry(path, notice)])
    error = raises(lambda: backend.diff(ref()))
    assert (error.code, error.reason) == ("refused", "too_large")
    by = {f.path: f for f in backend.files(ref()).files}
    assert (by[path].additions, by[path].deletions) == (None, None)
    _, raw = gd.read_raw_files(backend, ref())
    assert {f.path: f.patch for f in raw.files}[path] is None


def test_an_added_or_deleted_binary_notice_is_headers_only(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    new = entry("n.png", "Binary files /dev/null and b/n.png differ\n", new_file=True, a_mode="0")
    gone_notice = "Binary files a/g.png and /dev/null differ"
    gone = entry("g.png", gone_notice, deleted_file=True, b_mode="0")
    backend = one(fake, tmp_path, [new, gone])
    assert backend.diff(ref()).diff == (
        "diff --git a/n.png b/n.png\nnew file mode 100644\n"
        "diff --git a/g.png b/g.png\ndeleted file mode 100644\n"
    )
    counts = [(f.additions, f.deletions) for f in backend.files(ref()).files]
    assert counts == [(None, None), (None, None)]


@pytest.mark.parametrize(
    "binary",
    [
        entry(
            "new.png",
            "Binary files a/old.png and b/new.png differ\n",
            renamed_file=True,
            old_path="old.png",
        ),
        entry("m.png", "Binary files a/m.png and b/m.png differ\n", b_mode="100755"),
    ],
)
def test_a_renamed_or_mode_changed_binary_notice_is_cut(
    fake: FakeGitLab, tmp_path: Path, binary: Any
) -> None:
    error = raises(lambda: one(fake, tmp_path, [binary]).diff(ref()))
    assert (error.code, error.reason) == ("refused", "too_large")


LINK_GONE = entry(
    "link",
    "@@ -1 +0,0 @@\n-target\n\\ No newline at end of file\n",
    deleted_file=True,
    a_mode="120000",
    b_mode="0",
)
LINK_NEW = entry("link", "@@ -0,0 +1 @@\n+text\n", new_file=True, a_mode="0")
LINK_TEXT = (
    "diff --git a/link b/link\ndeleted file mode 120000\n--- a/link\n+++ /dev/null\n"
    "@@ -1 +0,0 @@\n-target\n\\ No newline at end of file\n"
    "diff --git a/link b/link\nnew file mode 100644\n--- /dev/null\n+++ b/link\n"
    "@@ -0,0 +1 @@\n+text\n"
)


@pytest.mark.parametrize("order", [[LINK_GONE, LINK_NEW], [LINK_NEW, LINK_GONE]])
def test_a_type_change_on_one_page_is_two_sections(
    fake: FakeGitLab, tmp_path: Path, order: list[Any]
) -> None:
    backend = one(fake, tmp_path, order)
    assert backend.diff(ref()).diff == LINK_TEXT
    assert [f.status for f in backend.files(ref()).files] == ["deleted", "added"]


def test_a_type_change_split_across_pages_is_two_sections(fake: FakeGitLab, tmp_path: Path) -> None:
    first = [entry(f"f{i}") for i in range(99)] + [LINK_NEW]
    backend = setup(fake, tmp_path, pages=[first, [LINK_GONE]], mr=mr_doc(changes_count="101"))
    text = backend.diff(ref()).diff
    assert text.endswith(LINK_TEXT)
    files = backend.files(ref()).files
    assert [(f.path, f.status) for f in files[-2:]] == [("link", "deleted"), ("link", "added")]


@pytest.mark.parametrize(
    "pair",
    [
        [entry("a"), entry("a")],
        [LINK_GONE, LINK_GONE],
        [LINK_NEW, LINK_NEW],
        [LINK_GONE, LINK_NEW, LINK_NEW],
        [LINK_GONE, entry("link")],
    ],
)
def test_any_other_repeat_of_a_path_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path, pair: list[Any]
) -> None:
    backend = one(fake, tmp_path, pair)
    assert raises(lambda: backend.files(ref())).code == "upstream_error"
    assert raises(lambda: backend.diff(ref())).code == "upstream_error"


@pytest.mark.parametrize(
    "fragment",
    [
        "@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n",
        "@@ -1 +1 @@\n a\n\\ No newline at end of file\n",
        "@@ -1,2 +1,2 @@\n a\n-b\n+c\n@@ -3 +3 @@\n-x\n+y\n",  # adjacent hunks
        "@@ -2,0 +3,2 @@\n+x\n+y\n@@ -3 +6 @@\n-z\n+w\n",  # an insertion, then a change
    ],
)
def test_valid_hunks_still_render(fake: FakeGitLab, tmp_path: Path, fragment: str) -> None:
    text = one(fake, tmp_path, [entry("a.txt", fragment)]).diff(ref()).diff
    assert text.endswith(fragment)


def test_a_generated_added_file_with_no_fragment_is_cut(fake: FakeGitLab, tmp_path: Path) -> None:
    gen = entry("gen.txt", "", generated_file=True, new_file=True, a_mode="0")
    error = raises(lambda: one(fake, tmp_path, [gen]).diff(ref()))
    assert (error.code, error.reason) == ("refused", "too_large")


def cut_compare(fake: FakeGitLab, tmp_path: Path, which: str, other: Any) -> Any:
    """``compare`` with the ``which`` way cut by forgectl's read cap and ``other`` the answer
    of the other way."""
    backend = setup(fake, tmp_path)
    compare_routes(
        fake, other if which == "backward" else None, other if which == "forward" else None
    )
    cut_reads(
        backend,
        lambda segments, q: (
            segments[-1] == "compare" and (q["from"] == FROM) == (which == "forward")
        ),
    )
    return backend.compare(ref(), FROM, TO)


def test_a_cut_backward_answer_with_forward_commits_is_diverged(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    result = cut_compare(fake, tmp_path, "backward", cmp_doc("2", ["2"], [entry("a.txt")]))
    assert (result.status, result.diff) == ("diverged", None)


def test_a_cut_backward_answer_with_no_forward_commits_is_behind(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    result = cut_compare(fake, tmp_path, "backward", cmp_doc("2", []))
    assert (result.status, result.diff) == ("behind", None)


def test_a_cut_forward_answer_with_backward_commits_is_diverged(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    result = cut_compare(fake, tmp_path, "forward", cmp_doc("1", ["1"]))
    assert (result.status, result.diff) == ("diverged", None)


def test_a_cut_forward_answer_with_no_backward_commits_is_too_large(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    error = raises(lambda: cut_compare(fake, tmp_path, "forward", cmp_doc("1", [])))
    assert (error.code, error.reason) == ("refused", "too_large")
