"""GitHub `files`, `diff` and `compare` (#13): no silent truncation.

The gateway is real; its process runner answers like `gh api -i` does. The recordings under
tests/recordings/github/diff are hand-built from GitHub's documented REST answers, scrubbed of real
names. Every assumed GitHub behavior is UNVERIFIED until the live suite (#31) re-checks it; the
report of #13 lists them.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from forgectl.errors import ForgectlError
from forgectl.gateway import ProcessResult
from forgectl.model import CompareResult, DiffResult, FilesResult

from .harness import golden
from .harness.forges import FakeForges
from .test_github_show import REF, Harness, Script, Slept, http  # noqa: F401

RECORDINGS = Path(__file__).parent / "recordings" / "github" / "diff"
HEAD = "3f8a1c0d5e7b9a2c4d6e8f0a1b3c5d7e9f0a1b2c"
BASE = "7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a"
FROM = "1" * 40
TO = "2" * 40
PR_ENDPOINT = "repos/example-org/example-repo/pulls/7"
DIFF_TYPE = "application/vnd.github.diff"


def recording(name: str) -> Any:
    return json.loads((RECORDINGS / f"{name}.json").read_text(encoding="utf-8"))


def pull(**changes: Any) -> dict[str, Any]:
    doc = recording("pull")
    doc.update(changes)
    return doc


def item(name: str, status: str = "modified", **extra: Any) -> dict[str, Any]:
    return {
        "filename": name,
        "status": status,
        "additions": 1,
        "deletions": 0,
        "patch": "@@",
        **extra,
    }


Answer = Any  # bytes, a ProcessResult, a JSON value, or a callable(n) of those


class Router(Script):
    """Answers each request by its kind: pull, diff, files, mergebase, compare, comparediff."""

    def __init__(self, **routes: Answer) -> None:
        super().__init__(b"")
        self.routes = routes
        self.kinds: list[str] = []
        self.accepts: list[str | None] = []

    @staticmethod
    def classify(argv: Sequence[str]) -> tuple[str, str | None]:
        endpoint = argv[-1]
        accept = next(
            (a.split(": ", 1)[1] for a in argv if a.startswith("Accept: ")),
            None,
        )
        path, _, query = endpoint.partition("?")
        if path == PR_ENDPOINT:
            return ("diff" if accept == DIFF_TYPE else "pull"), accept
        if path == f"{PR_ENDPOINT}/files":
            return "files", accept
        if "/compare/" in path:
            name = path.rsplit("/", 1)[1]
            base = "mergebase" if name.endswith("..." + HEAD) else "compare"
            return (base + "diff" if accept == DIFF_TYPE and base == "compare" else base), accept
        return "other", accept

    def __call__(self, argv: Sequence[str], *, stdin: bytes | None, **_: Any) -> ProcessResult:
        self.argvs.append(list(argv))
        kind, accept = self.classify(argv)
        self.kinds.append(kind)
        self.accepts.append(accept)
        assert kind in self.routes, f"nothing scripted for {kind}: {argv[-1]}"
        answer = self.routes[kind]
        n = self.kinds.count(kind)
        if callable(answer):
            answer = answer(n)
        if isinstance(answer, ProcessResult):
            return answer
        if isinstance(answer, bytes):
            raw = answer
        else:
            raw = http(200, answer)
        code = 0 if raw.startswith(b"HTTP/2.0 2") else 1
        return ProcessResult(pid=1, pgid=1, returncode=code, stdout=raw, stderr=b"")


def ok_bytes(body: bytes, headers: dict[str, str] | None = None) -> bytes:
    return http(200, body, headers=headers)


class RouterHarness(Harness):
    script: Router


@pytest.fixture
def make(fake_forges: FakeForges) -> Callable[..., RouterHarness]:
    def build(retries: int = 0, **routes: Answer) -> RouterHarness:
        return RouterHarness(fake_forges, Router(**routes), retries=retries)

    return build


def raises(fn: Callable[[], Any]) -> ForgectlError:
    with pytest.raises(ForgectlError) as raised:
        fn()
    return raised.value


# -- files ---------------------------------------------------------------------------------------


def test_golden_files(make: Callable[..., RouterHarness]) -> None:
    h = make(pull=pull(), files=recording("files"))
    result = h.backend.files(REF)
    assert isinstance(result, FilesResult)
    golden.assert_golden(
        "files/github-complete", result.to_dict(), schema_id="forgectl/cli/files.json"
    )
    assert result.files_complete is True


def test_status_mapping_and_previous_path(make: Callable[..., RouterHarness]) -> None:
    h = make(pull=pull(), files=recording("files"))
    by = {f.path: f for f in h.backend.files(REF).files}
    assert by["src/old.py"].status == "deleted"
    assert by["src/moved.py"].status == "renamed"
    assert by["src/moved.py"].previous_path == "src/orig.py"
    assert by["src/copy.py"].status == "added"
    assert by["src/copy.py"].previous_path is None
    assert by["assets/logo.png"].status == "modified"


@pytest.mark.parametrize("host_status,ours", [("changed", "modified"), ("unchanged", "modified")])
def test_changed_and_unchanged_are_modified(
    make: Callable[..., RouterHarness], host_status: str, ours: str
) -> None:
    h = make(pull=pull(changed_files=1), files=[item("a.txt", host_status)])
    assert h.backend.files(REF).files[0].status == ours


def test_a_cut_list_is_incomplete_with_exit_0_data(make: Callable[..., RouterHarness]) -> None:
    def page(n: int) -> Any:
        if n > 30:
            return []
        return [item(f"f{n}-{i}.txt") for i in range(100)]

    h = make(pull=pull(changed_files=4156), files=page)
    result = h.backend.files(REF)
    assert len(result.files) == 3000
    assert result.files_complete is False
    assert result.changed_files == 4156
    assert h.warnings  # a warn line says why


def test_a_list_that_never_ends_stops_at_the_page_limit_and_is_incomplete(
    make: Callable[..., RouterHarness],
) -> None:
    h = make(
        pull=pull(changed_files=100000),
        files=lambda n: ok_bytes(
            json.dumps([item(f"p{n}-{i}") for i in range(100)]).encode(),
            {"Link": '<https://x/y?page=99>; rel="next"'},
        ),
    )
    result = h.backend.files(REF)
    assert h.script.kinds.count("files") == 31
    assert result.files_complete is False


def test_pages_use_per_page_100_and_a_short_page_ends_the_list(
    make: Callable[..., RouterHarness],
) -> None:
    def page(n: int) -> Any:
        return [item(f"a{i}") for i in range(100)] if n == 1 else [item(f"b{i}") for i in range(20)]

    h = make(pull=pull(changed_files=120), files=page)
    result = h.backend.files(REF)
    assert result.files_complete is True and len(result.files) == 120
    endpoints = [a[-1] for a in h.script.argvs if "/files" in a[-1]]
    assert endpoints == [
        f"{PR_ENDPOINT}/files?per_page=100&page=1",
        f"{PR_ENDPOINT}/files?per_page=100&page=2",
    ]


def test_a_full_page_with_no_link_header_still_asks_for_the_next(
    make: Callable[..., RouterHarness],
) -> None:
    h = make(
        pull=pull(changed_files=100),
        files=lambda n: [item(f"a{i}") for i in range(100)] if n == 1 else [],
    )
    assert h.backend.files(REF).files_complete is True
    assert h.script.kinds.count("files") == 2


def test_a_link_header_is_a_hint_and_is_never_followed(make: Callable[..., RouterHarness]) -> None:
    link = '<https://evil.example/steal?token=1>; rel="next"'
    h = make(
        pull=pull(changed_files=1),
        files=lambda n: ok_bytes(
            json.dumps([item("a")] if n == 1 else []).encode(), {"Link": link}
        ),
    )
    h.backend.files(REF)
    assert all("evil" not in a[-1] for a in h.script.argvs)


def test_more_files_than_changed_files_is_not_complete(make: Callable[..., RouterHarness]) -> None:
    h = make(pull=pull(changed_files=1), files=[item("a"), item("b")])
    assert h.backend.files(REF).files_complete is False


def test_an_unknown_changed_files_is_not_complete(make: Callable[..., RouterHarness]) -> None:
    doc = pull()
    del doc["changed_files"]
    result = make(pull=doc, files=[item("a")]).backend.files(REF)
    assert result.files_complete is False and result.changed_files is None


def test_a_file_listed_twice_is_upstream_error(make: Callable[..., RouterHarness]) -> None:
    h = make(pull=pull(changed_files=2), files=[item("a"), item("a")])
    assert raises(lambda: h.backend.files(REF)).code == "upstream_error"


@pytest.mark.parametrize(
    "bad",
    [
        item("a", "teleported"),
        item("a", "renamed"),  # no previous_filename
        {"status": "added"},
        item(""),
        item("/etc/passwd"),
        item("a/../../b"),
        item("a//b"),
        item("./a"),
        item("a\x00b"),
        item("x" * 5000),
        item("a", "renamed", previous_filename="../x"),
        {**item("a"), "filename": 7},
        {**item("a"), "patch": 5},
        {**item("a"), "additions": "1"},
        {**item("a"), "additions": -1},
        {**item("a"), "deletions": True},
        {k: v for k, v in item("a").items() if k != "deletions"},
        "not an object",
    ],
)
def test_a_hostile_file_entry_is_upstream_error(
    make: Callable[..., RouterHarness], bad: Any
) -> None:
    h = make(pull=pull(changed_files=1), files=[bad])
    assert raises(lambda: h.backend.files(REF)).code == "upstream_error"


def test_hostile_but_legal_paths_are_sanitized_on_one_line(
    make: Callable[..., RouterHarness],
) -> None:
    names = ["-rf", "a\nb", "e‮doc.txt", "café/☃.txt", "tab\there", "esc\x1b[2J"]
    h = make(pull=pull(changed_files=len(names)), files=[item(n) for n in names])
    paths = [f.path for f in h.backend.files(REF).files]
    assert "-rf" in paths
    assert "a<U+000A>b" in paths
    assert "e<U+202E>doc.txt" in paths
    assert "café/☃.txt" in paths
    assert "tab<U+0009>here" in paths
    assert "esc<U+001B>[2J" in paths
    assert all("\n" not in p and "\x1b" not in p for p in paths)


def test_files_moved_head_fetches_once_more_then_succeeds(
    make: Callable[..., RouterHarness],
) -> None:
    def pulls(n: int) -> Any:
        return pull(head={**pull()["head"], "sha": "4" * 40}) if n == 2 else pull()

    h = make(pull=pulls, files=[item("a")])
    result = h.backend.files(REF)
    assert result.head_sha == HEAD
    assert h.script.kinds == ["pull", "files", "pull", "pull", "files", "pull"]


def test_files_moved_head_twice_is_upstream_error(make: Callable[..., RouterHarness]) -> None:
    def pulls(n: int) -> Any:
        return pull(head={**pull()["head"], "sha": str(n) * 40})

    h = make(pull=pulls, files=[item("a")])
    assert raises(lambda: h.backend.files(REF)).code == "upstream_error"
    assert h.script.kinds.count("pull") == 4  # one more fetch, never a third


def test_a_pull_from_another_host_is_wrong_host_response(
    make: Callable[..., RouterHarness],
) -> None:
    h = make(pull=pull(html_url="https://evil.example/example-org/example-repo/pull/7"))
    assert raises(lambda: h.backend.files(REF)).code == "wrong_host_response"


def test_a_pull_of_another_number_is_upstream_error(make: Callable[..., RouterHarness]) -> None:
    h = make(pull=pull(number=8))
    assert raises(lambda: h.backend.files(REF)).code == "upstream_error"


def test_a_missing_pull_is_not_found(make: Callable[..., RouterHarness]) -> None:
    h = make(pull=http(404, {"message": "Not Found"}))
    assert raises(lambda: h.backend.files(REF)).code == "not_found"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/user-1/example-repo/issues/7",
        "https://github.com/user-1/example-repo/pull/9999",
        "https://github.com/user-1/example-repo",
        "https://github.com/user-1/example-repo/pull/7/../../../other/x",
        "https://github.com/user-1/example-repo/%2e%2e/example-repo/pull/7",
    ],
)
@pytest.mark.parametrize("command", ["files", "diff"])
def test_a_pull_whose_url_is_not_this_pr_is_refused(
    make: Callable[..., RouterHarness], url: str, command: str
) -> None:
    h = make(**diff_routes(pull=pull(changed_files=1, html_url=url)), files=[item("a")])
    assert raises(lambda: getattr(h.backend, command)(REF)).code == "upstream_error"


def test_a_file_listed_on_two_pages_is_a_moved_list_read_once_more(
    make: Callable[..., RouterHarness],
) -> None:
    def pages(n: int) -> Any:
        if n % 2:
            return [item(f"a{i}") for i in range(100)]
        return [item("a0")] if n == 2 else [item("b")]

    h = make(pull=pull(changed_files=101), files=pages)
    result = h.backend.files(REF)
    assert result.files_complete is True and len(result.files) == 101
    assert h.script.kinds == ["pull", "files", "files", "pull", "files", "files", "pull"]


def test_a_file_listed_on_two_pages_twice_is_upstream_error(
    make: Callable[..., RouterHarness],
) -> None:
    def pages(n: int) -> Any:
        return [item(f"a{i}") for i in range(100)] if n % 2 else [item("a0")]

    h = make(pull=pull(changed_files=101), files=pages)
    assert raises(lambda: h.backend.files(REF)).code == "upstream_error"
    assert h.script.kinds.count("files") == 4


def test_a_project_that_changed_between_the_reads_reads_once_more(
    make: Callable[..., RouterHarness],
) -> None:
    def moved(n: int) -> Any:
        if n != 2:
            return pull()
        repo = {"full_name": "user-2/example-repo"}
        return pull(
            html_url="https://github.com/user-2/example-repo/pull/7",
            base={**pull()["base"], "repo": repo},
        )

    h = make(pull=moved, files=[item("a")])
    assert h.backend.files(REF).pr == "github.com/user-1/example-repo#7"
    assert h.script.kinds.count("files") == 2


def test_the_endpoints_hold_only_validated_parts_and_the_token_is_not_in_argv(
    make: Callable[..., RouterHarness],
) -> None:
    h = make(pull=pull(), files=recording("files"))
    h.backend.files(REF)
    for argv in h.script.argvs:
        assert "tok-show" not in " ".join(argv)
        assert argv[-1].startswith("repos/example-org/example-repo/pulls/7")
        assert "--hostname" in argv and argv[argv.index("--hostname") + 1] == "github.com"


# -- diff ----------------------------------------------------------------------------------------


def diff_routes(text: bytes | None = None, **over: Answer) -> dict[str, Answer]:
    body = (RECORDINGS / "pull.diff").read_bytes() if text is None else text
    routes: dict[str, Answer] = {
        "pull": pull(changed_files=1),
        "diff": ok_bytes(body),
        "mergebase": recording("merge-base"),
    }
    routes.update(over)
    return routes


def test_golden_diff(make: Callable[..., RouterHarness]) -> None:
    result = make(**diff_routes()).backend.diff(REF)
    assert isinstance(result, DiffResult)
    golden.assert_golden("diff/github-open", result.to_dict(), schema_id="forgectl/cli/diff.json")
    assert result.merge_base_sha == "6b" * 20
    assert result.base_sha == BASE and result.head_sha == HEAD


def test_the_diff_is_asked_by_media_type_and_the_merge_base_by_shas(
    make: Callable[..., RouterHarness],
) -> None:
    h = make(**diff_routes())
    h.backend.diff(REF)
    assert h.script.kinds == ["pull", "diff", "mergebase", "pull"]
    assert h.script.accepts[1] == DIFF_TYPE
    assert h.script.argvs[2][-1] == (
        f"repos/example-org/example-repo/compare/{BASE}...{HEAD}?per_page=1"
    )


def test_a_406_is_too_large_exit_20(make: Callable[..., RouterHarness]) -> None:
    h = make(
        **diff_routes(
            diff=http(406, {"message": "diff exceeded the maximum number of files (300)"})
        )
    )
    err = raises(lambda: h.backend.diff(REF))
    assert (err.code, err.reason, err.exit_code) == ("refused", "too_large", 20)


def test_a_missing_merge_base_is_null(make: Callable[..., RouterHarness]) -> None:
    h = make(**diff_routes(mergebase=http(404, {"message": "Not Found"})))
    assert h.backend.diff(REF).merge_base_sha is None


def test_a_merge_base_that_is_not_a_sha_is_upstream_error(
    make: Callable[..., RouterHarness],
) -> None:
    doc = recording("merge-base")
    doc["merge_base_commit"]["sha"] = "nope"
    assert (
        raises(lambda: make(**diff_routes(mergebase=doc)).backend.diff(REF)).code
        == "upstream_error"
    )


def test_an_empty_answer_for_a_pr_with_changes_is_upstream_error(
    make: Callable[..., RouterHarness],
) -> None:
    h = make(**diff_routes(text=b""))
    assert raises(lambda: h.backend.diff(REF)).code == "upstream_error"


def test_an_empty_diff_for_a_pr_with_no_changes_is_empty(
    make: Callable[..., RouterHarness],
) -> None:
    h = make(**diff_routes(text=b"", pull=pull(changed_files=0)))
    assert h.backend.diff(REF).diff == ""


def test_a_diff_with_fewer_headers_than_changed_files_is_upstream_error(
    make: Callable[..., RouterHarness],
) -> None:
    h = make(**diff_routes(pull=pull(changed_files=250)))
    assert raises(lambda: h.backend.diff(REF)).code == "upstream_error"


def test_a_diff_with_more_headers_than_changed_files_is_upstream_error(
    make: Callable[..., RouterHarness],
) -> None:
    text = b"diff --git a/x b/x\n+a\ndiff --git a/y b/y\n+b\n"
    h = make(**diff_routes(text=text))
    assert raises(lambda: h.backend.diff(REF)).code == "upstream_error"


@pytest.mark.parametrize("changed", ["1", -1, True, None, "missing"])
@pytest.mark.parametrize("text", [b"", None])
def test_a_diff_needs_a_valid_changed_files(
    make: Callable[..., RouterHarness], changed: Any, text: bytes | None
) -> None:
    doc = pull(changed_files=changed)
    if changed == "missing":
        del doc["changed_files"]
    h = make(**diff_routes(text=text, pull=doc))
    assert raises(lambda: h.backend.diff(REF)).code == "upstream_error"


@pytest.mark.parametrize(
    "url",
    [
        f"https://evil.example/user-1/example-repo/compare/{BASE}...{HEAD}",
        f"https://github.com/other/example-repo/compare/{BASE}...{HEAD}",
        f"https://github.com/user-1/example-repo/%2e%2e/compare/{BASE}...{HEAD}",
    ],
)
def test_a_merge_base_answer_of_another_place_is_wrong_host_response(
    make: Callable[..., RouterHarness], url: str
) -> None:
    doc = {**recording("merge-base"), "html_url": url}
    h = make(**diff_routes(mergebase=doc))
    assert raises(lambda: h.backend.diff(REF)).code == "wrong_host_response"


HOSTILE = (
    b"diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,6 +1,6 @@\n"
    b"-crlf\r\n+\x1b[31mred\x1b[0m\n+rlo \xe2\x80\xae evil\n+nul\x00here\n+lone\rcr\n"
    b"+bad \xff\xfe utf8\n+zw\xe2\x80\x8bj\n"
)


def test_hostile_diff_content_is_sanitized_per_line_with_line_count_kept(
    make: Callable[..., RouterHarness],
) -> None:
    text = make(**diff_routes(text=HOSTILE)).backend.diff(REF).diff
    assert text.count("\n") == HOSTILE.count(b"\n")
    assert "-crlf\r\n" in text  # a CR before LF stays
    assert "<U+001B>[31mred<U+001B>[0m" in text
    assert "rlo <U+202E> evil" in text
    assert "nul<U+0000>here" in text
    assert "lone<U+000D>cr" in text
    assert "bad �� utf8" in text
    assert "zw<U+200B>j" in text
    assert "\x1b" not in text and "\x00" not in text and "‮" not in text


def test_a_huge_line_is_sanitized_whole(make: Callable[..., RouterHarness]) -> None:
    line = b"+" + b"\x1b" * 200_000 + b"\n"
    text = (
        make(**diff_routes(text=b"diff --git a/x b/x\n@@ -1 +1 @@\n" + line)).backend.diff(REF).diff
    )
    assert text.count("<U+001B>") == 200_000


def test_diff_head_moved_twice_is_upstream_error_and_once_refetches(
    make: Callable[..., RouterHarness],
) -> None:
    moved = pull(changed_files=1, head={**pull()["head"], "sha": "4" * 40})
    h = make(**diff_routes(pull=lambda n: moved if n == 2 else pull(changed_files=1)))
    assert h.backend.diff(REF).head_sha == HEAD
    assert h.script.kinds.count("diff") == 2
    h2 = make(
        **diff_routes(
            pull=lambda n: pull(changed_files=1, base={**pull()["base"], "sha": str(n) * 40})
        )
    )
    assert raises(lambda: h2.backend.diff(REF)).code == "upstream_error"


CUT = ProcessResult(
    pid=1,
    pgid=1,
    returncode=0,
    stdout=b"HTTP/2.0 200 OK\r\n\r\ndiff --git a/x b/x\n",
    stderr=b"",
    stdout_truncated=True,
)


def test_a_diff_answer_over_the_read_cap_is_too_large_and_never_retried(
    make: Callable[..., RouterHarness],
) -> None:
    h = make(retries=3, **diff_routes(diff=CUT))
    err = raises(lambda: h.backend.diff(REF))
    assert (err.code, err.reason, err.exit_code) == ("refused", "too_large", 20)
    assert h.script.kinds.count("diff") == 1


# -- compare -------------------------------------------------------------------------------------


def compare_doc(status: str, ahead: Any, behind: Any, **over: Any) -> dict[str, Any]:
    doc = copy.deepcopy(recording("compare-ahead"))
    doc.update(status=status, ahead_by=ahead, behind_by=behind, **over)
    return doc


def listed(n: int) -> list[dict[str, Any]]:
    return [item(f"f{i}") for i in range(n)]


def headers(n: int) -> bytes:
    return b"".join(b"diff --git a/f%d b/f%d\n" % (i, i) for i in range(n))


def cmp(h: RouterHarness, from_sha: str = FROM, to_sha: str = TO) -> CompareResult:
    result = h.backend.compare(REF, from_sha, to_sha)
    assert isinstance(result, CompareResult)
    return result


@pytest.fixture
def cmake(make: Callable[..., RouterHarness]) -> Callable[..., RouterHarness]:
    """``make`` with the PR read every ``compare`` starts with."""

    def build(retries: int = 0, **routes: Answer) -> RouterHarness:
        return make(retries=retries, **{"pull": pull(), **routes})

    return build


def test_golden_compare_ahead(cmake: Callable[..., RouterHarness]) -> None:
    h = cmake(
        compare=recording("compare-ahead"),
        comparediff=ok_bytes((RECORDINGS / "pull.diff").read_bytes()),
    )
    result = cmp(h)
    golden.assert_golden(
        "compare/github-ahead", result.to_dict(), schema_id="forgectl/cli/compare.json"
    )
    assert h.script.kinds == ["pull", "compare", "comparediff"]
    assert h.script.accepts == [None, None, DIFF_TYPE]
    assert (
        h.script.argvs[1][-1] == f"repos/example-org/example-repo/compare/{FROM}...{TO}?per_page=1"
    )


def test_compare_names_the_pr_by_its_canonical_path(cmake: Callable[..., RouterHarness]) -> None:
    result = cmp(cmake(compare=compare_doc("behind", 0, 3)))
    assert result.pr == "github.com/user-1/example-repo#7"


def test_compare_of_a_missing_pr_is_not_found(cmake: Callable[..., RouterHarness]) -> None:
    h = cmake(pull=http(404, {"message": "Not Found"}))
    assert raises(lambda: cmp(h)).code == "not_found"
    assert h.script.kinds == ["pull"]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/user-1/example-repo/compare/x...y",
        "https://github.com/other/example-repo/compare/x...y",
        "https://github.com/example-org/example-repo/compare/x...y",  # the typed, not canonical
        "https://github.com/user-1/example-repo/%2e%2e/example-repo/compare/x...y",
        "https://github.com/user-1/example-repo/compare/x/../../y",
        "https://github.com/user-1/example-repo/pull/7",
        "https://github.com/user-1/example-repo/compare/",
        "https://github.com/user-1/example-repo",
        7,
    ],
)
def test_a_compare_answer_of_another_place_is_wrong_host_response(
    cmake: Callable[..., RouterHarness], url: Any
) -> None:
    h = cmake(retries=3, compare=compare_doc("ahead", 1, 0, html_url=url))
    assert raises(lambda: cmp(h)).code == "wrong_host_response"
    assert h.script.kinds == ["pull", "compare"]  # never retried


@pytest.mark.parametrize(
    "status,ahead,behind",
    [("behind", 0, 3), ("diverged", 2, 3)],
)
def test_behind_and_diverged_have_a_null_diff_and_no_diff_request(
    cmake: Callable[..., RouterHarness], status: str, ahead: int, behind: int
) -> None:
    h = cmake(compare=compare_doc(status, ahead, behind))
    result = cmp(h)
    assert (result.status, result.diff) == (status, None)
    assert h.script.kinds == ["pull", "compare"]


def test_identical_by_the_host_has_an_empty_diff(cmake: Callable[..., RouterHarness]) -> None:
    result = cmp(cmake(compare=compare_doc("identical", 0, 0, files=[])))
    assert (result.status, result.diff) == ("identical", "")


def test_equal_shas_read_the_pr_and_are_identical_with_no_compare_request(
    cmake: Callable[..., RouterHarness],
) -> None:
    h = cmake()
    result = cmp(h, FROM, FROM)
    assert (result.status, result.diff) == ("identical", "")
    assert result.pr == "github.com/user-1/example-repo#7"
    assert h.script.kinds == ["pull"]


def test_a_404_is_unavailable(cmake: Callable[..., RouterHarness]) -> None:
    h = cmake(compare=http(404, {"message": "Not Found"}))
    result = cmp(h)
    assert (result.status, result.diff) == ("unavailable", None)


def test_a_404_on_the_diff_after_an_ahead_is_unavailable_never_partial(
    cmake: Callable[..., RouterHarness],
) -> None:
    h = cmake(compare=compare_doc("ahead", 1, 0), comparediff=http(404, {"message": "Not Found"}))
    result = cmp(h)
    assert (result.status, result.diff) == ("unavailable", None)


def test_a_406_on_the_diff_is_too_large(cmake: Callable[..., RouterHarness]) -> None:
    h = cmake(compare=compare_doc("ahead", 1, 0), comparediff=http(406, {"message": "too big"}))
    err = raises(lambda: cmp(h))
    assert (err.code, err.reason) == ("refused", "too_large")


def test_a_compare_diff_over_the_read_cap_is_too_large_and_never_retried(
    cmake: Callable[..., RouterHarness],
) -> None:
    h = cmake(retries=3, compare=compare_doc("ahead", 1, 0), comparediff=CUT)
    err = raises(lambda: cmp(h))
    assert (err.code, err.reason, err.exit_code) == ("refused", "too_large", 20)
    assert h.script.kinds.count("comparediff") == 1


def test_a_listed_file_count_at_the_cap_is_too_large_with_no_diff_request(
    cmake: Callable[..., RouterHarness],
) -> None:
    h = cmake(compare=compare_doc("ahead", 1, 0, files=listed(300)))
    assert raises(lambda: cmp(h)).reason == "too_large"
    assert "comparediff" not in h.script.kinds


def test_a_diff_with_as_many_files_as_the_cap_is_too_large(
    cmake: Callable[..., RouterHarness],
) -> None:
    h = cmake(
        compare=compare_doc("ahead", 1, 0, files=listed(1)), comparediff=ok_bytes(headers(300))
    )
    assert raises(lambda: cmp(h)).reason == "too_large"
    h = cmake(
        compare=compare_doc("ahead", 1, 0, files=listed(299)), comparediff=ok_bytes(headers(299))
    )
    assert cmp(h).status == "ahead"


@pytest.mark.parametrize(
    "files,text", [(2, headers(1)), (1, headers(2)), (1, b""), (0, headers(1))]
)
def test_a_diff_that_does_not_match_the_listed_count_is_unavailable(
    cmake: Callable[..., RouterHarness], files: int, text: bytes
) -> None:
    h = cmake(compare=compare_doc("ahead", 1, 0, files=listed(files)), comparediff=ok_bytes(text))
    assert (cmp(h).status, cmp(h).diff) == ("unavailable", None)


@pytest.mark.parametrize("files", ["x", {"a": 1}, None])
def test_a_listed_files_that_is_not_a_list_is_upstream_error(
    cmake: Callable[..., RouterHarness], files: Any
) -> None:
    h = cmake(compare=compare_doc("ahead", 1, 0, files=files))
    assert raises(lambda: cmp(h)).code == "upstream_error"


def test_a_hunk_line_that_looks_like_a_header_does_not_count(
    cmake: Callable[..., RouterHarness],
) -> None:
    text = headers(1) + b"@@ -0,0 +1,400 @@\n" + b"+diff --git a/x b/x\n" * 400
    h = cmake(compare=compare_doc("ahead", 1, 0, files=listed(1)), comparediff=ok_bytes(text))
    assert cmp(h).status == "ahead"


def test_the_compare_diff_is_sanitized(cmake: Callable[..., RouterHarness]) -> None:
    h = cmake(compare=compare_doc("ahead", 1, 0), comparediff=ok_bytes(HOSTILE))
    result = cmp(h)
    assert result.diff is not None and "\x1b" not in result.diff
    assert result.diff.count("\n") == HOSTILE.count(b"\n")


@pytest.mark.parametrize(
    "doc",
    [
        compare_doc("ahead", 0, 0),  # ahead with no commits
        compare_doc("behind", 1, 0),
        compare_doc("diverged", 1, 0),
        compare_doc("identical", 1, 0),
        compare_doc("sideways", 1, 0),
        compare_doc("ahead", "1", 0),
        compare_doc("ahead", -1, 0),
        compare_doc("ahead", True, 0),
        {"html_url": compare_doc("ahead", 1, 0)["html_url"]},
    ],
)
def test_an_unclear_answer_is_unavailable_never_a_diff(
    cmake: Callable[..., RouterHarness], doc: dict[str, Any]
) -> None:
    h = cmake(compare=doc)
    result = cmp(h)
    assert (result.status, result.diff) == ("unavailable", None)
    assert h.script.kinds == ["pull", "compare"]


def test_a_compare_that_is_not_an_object_is_upstream_error(
    cmake: Callable[..., RouterHarness],
) -> None:
    assert raises(lambda: cmp(cmake(compare=[]))).code == "upstream_error"


def test_a_server_error_follows_the_read_table(cmake: Callable[..., RouterHarness]) -> None:
    assert raises(lambda: cmp(cmake(compare=http(502, {"message": "x"})))).code == "upstream_error"
    assert (
        raises(lambda: cmp(cmake(compare=http(403, {"message": "no"})))).code == "permission_denied"
    )


def test_the_head_of_the_request_is_not_a_ref(cmake: Callable[..., RouterHarness]) -> None:
    # shas only: the endpoint is built from the two validated shas and nothing else
    h = cmake(compare=compare_doc("behind", 0, 1))
    cmp(h)
    assert re.fullmatch(
        r"repos/example-org/example-repo/compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40}\?per_page=1",
        h.script.argvs[1][-1],
    )


# -- retries of a malformed answer (interface: Reliability) --------------------------------------


def first_bad(bad: Any, good: Any) -> Callable[[int], Any]:
    return lambda n: bad if n == 1 else good


def test_a_malformed_pull_is_retried_like_a_5xx(make: Callable[..., RouterHarness]) -> None:
    h = make(retries=3, pull=first_bad({"number": 7}, pull()), files=recording("files"))
    assert h.backend.files(REF).files_complete is True
    assert h.script.kinds[:2] == ["pull", "pull"] and h.slept.waits


def test_a_malformed_files_page_is_retried_like_a_5xx(make: Callable[..., RouterHarness]) -> None:
    h = make(retries=3, pull=pull(), files=first_bad([{"status": "added"}], recording("files")))
    assert h.backend.files(REF).files_complete is True
    assert h.script.kinds.count("files") == 2


def test_a_malformed_merge_base_is_retried_like_a_5xx(make: Callable[..., RouterHarness]) -> None:
    bad = copy.deepcopy(recording("merge-base"))
    bad["merge_base_commit"]["sha"] = "nope"
    h = make(retries=3, **diff_routes(mergebase=first_bad(bad, recording("merge-base"))))
    assert h.backend.diff(REF).merge_base_sha == "6b" * 20
    assert h.script.kinds.count("mergebase") == 2


def test_a_malformed_compare_is_retried_like_a_5xx(cmake: Callable[..., RouterHarness]) -> None:
    h = cmake(
        retries=3,
        compare=first_bad(compare_doc("ahead", 1, 0, files="x"), recording("compare-ahead")),
        comparediff=ok_bytes((RECORDINGS / "pull.diff").read_bytes()),
    )
    assert cmp(h).status == "ahead"
    assert h.script.kinds.count("compare") == 2


def test_a_wrong_host_pull_is_never_retried(make: Callable[..., RouterHarness]) -> None:
    h = make(retries=3, pull=pull(html_url="https://evil.example/user-1/example-repo/pull/7"))
    assert raises(lambda: h.backend.files(REF)).code == "wrong_host_response"
    assert h.script.kinds == ["pull"]
