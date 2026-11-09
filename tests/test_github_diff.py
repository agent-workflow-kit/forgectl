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
    def build(**routes: Answer) -> RouterHarness:
        return RouterHarness(fake_forges, Router(**routes), retries=0)

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
    text = make(**diff_routes(text=b"@@ -1 +1 @@\n" + line)).backend.diff(REF).diff
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


def test_a_diff_answer_over_the_size_cap_is_upstream_error(
    make: Callable[..., RouterHarness],
) -> None:
    big = ProcessResult(
        pid=1,
        pgid=1,
        returncode=0,
        stdout=b"HTTP/2.0 200 OK\r\n\r\n",
        stderr=b"",
        stdout_truncated=True,
    )
    assert raises(lambda: make(**diff_routes(diff=big)).backend.diff(REF)).code == "upstream_error"


# -- compare -------------------------------------------------------------------------------------


def compare_doc(status: str, ahead: Any, behind: Any, **over: Any) -> dict[str, Any]:
    doc = copy.deepcopy(recording("compare-ahead"))
    doc.update(status=status, ahead_by=ahead, behind_by=behind, **over)
    return doc


def cmp(h: RouterHarness, from_sha: str = FROM, to_sha: str = TO) -> CompareResult:
    result = h.backend.compare(REF, from_sha, to_sha)
    assert isinstance(result, CompareResult)
    return result


def test_golden_compare_ahead(make: Callable[..., RouterHarness]) -> None:
    h = make(
        compare=recording("compare-ahead"),
        comparediff=ok_bytes((RECORDINGS / "pull.diff").read_bytes()),
    )
    result = cmp(h)
    golden.assert_golden(
        "compare/github-ahead", result.to_dict(), schema_id="forgectl/cli/compare.json"
    )
    assert h.script.accepts == [None, DIFF_TYPE]
    assert (
        h.script.argvs[0][-1] == f"repos/example-org/example-repo/compare/{FROM}...{TO}?per_page=1"
    )


@pytest.mark.parametrize(
    "status,ahead,behind",
    [("behind", 0, 3), ("diverged", 2, 3)],
)
def test_behind_and_diverged_have_a_null_diff_and_no_diff_request(
    make: Callable[..., RouterHarness], status: str, ahead: int, behind: int
) -> None:
    h = make(compare=compare_doc(status, ahead, behind))
    result = cmp(h)
    assert (result.status, result.diff) == (status, None)
    assert h.script.kinds == ["compare"]


def test_identical_by_the_host_has_an_empty_diff(make: Callable[..., RouterHarness]) -> None:
    result = cmp(make(compare=compare_doc("identical", 0, 0)))
    assert (result.status, result.diff) == ("identical", "")


def test_equal_shas_are_identical_with_no_request(make: Callable[..., RouterHarness]) -> None:
    h = make()
    result = cmp(h, FROM, FROM)
    assert (result.status, result.diff) == ("identical", "")
    assert h.script.argvs == []


def test_a_404_is_unavailable(make: Callable[..., RouterHarness]) -> None:
    h = make(compare=http(404, {"message": "Not Found"}))
    result = cmp(h)
    assert (result.status, result.diff) == ("unavailable", None)


def test_a_404_on_the_diff_after_an_ahead_is_unavailable_never_partial(
    make: Callable[..., RouterHarness],
) -> None:
    h = make(compare=compare_doc("ahead", 1, 0), comparediff=http(404, {"message": "Not Found"}))
    result = cmp(h)
    assert (result.status, result.diff) == ("unavailable", None)


def test_a_406_on_the_diff_is_too_large(make: Callable[..., RouterHarness]) -> None:
    h = make(compare=compare_doc("ahead", 1, 0), comparediff=http(406, {"message": "too big"}))
    err = raises(lambda: cmp(h))
    assert (err.code, err.reason) == ("refused", "too_large")


def test_a_diff_with_as_many_files_as_the_cap_is_too_large(
    make: Callable[..., RouterHarness],
) -> None:
    text = b"".join(b"diff --git a/f%d b/f%d\n" % (i, i) for i in range(300))
    h = make(compare=compare_doc("ahead", 1, 0), comparediff=ok_bytes(text))
    assert raises(lambda: cmp(h)).reason == "too_large"
    under = b"".join(b"diff --git a/f%d b/f%d\n" % (i, i) for i in range(299))
    h = make(compare=compare_doc("ahead", 1, 0), comparediff=ok_bytes(under))
    assert cmp(h).status == "ahead"


def test_a_hunk_line_that_looks_like_a_header_does_not_count(
    make: Callable[..., RouterHarness],
) -> None:
    text = b"@@ -0,0 +1,400 @@\n" + b"+diff --git a/x b/x\n" * 400
    h = make(compare=compare_doc("ahead", 1, 0), comparediff=ok_bytes(text))
    assert cmp(h).status == "ahead"


def test_the_compare_diff_is_sanitized(make: Callable[..., RouterHarness]) -> None:
    h = make(compare=compare_doc("ahead", 1, 0), comparediff=ok_bytes(HOSTILE))
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
    make: Callable[..., RouterHarness], doc: dict[str, Any]
) -> None:
    h = make(compare=doc)
    result = cmp(h)
    assert (result.status, result.diff) == ("unavailable", None)
    assert h.script.kinds == ["compare"]


def test_a_compare_answer_from_another_host_is_wrong_host_response(
    make: Callable[..., RouterHarness],
) -> None:
    h = make(compare=compare_doc("ahead", 1, 0, html_url="https://evil.example/c"))
    assert raises(lambda: cmp(h)).code == "wrong_host_response"


def test_a_compare_that_is_not_an_object_is_upstream_error(
    make: Callable[..., RouterHarness],
) -> None:
    assert raises(lambda: cmp(make(compare=[]))).code == "upstream_error"


def test_a_server_error_follows_the_read_table(make: Callable[..., RouterHarness]) -> None:
    assert raises(lambda: cmp(make(compare=http(502, {"message": "x"})))).code == "upstream_error"
    assert (
        raises(lambda: cmp(make(compare=http(403, {"message": "no"})))).code == "permission_denied"
    )


def test_the_head_of_the_request_is_not_a_ref(make: Callable[..., RouterHarness]) -> None:
    # shas only: the endpoint is built from the two validated shas and nothing else
    h = make(compare=compare_doc("behind", 0, 1))
    cmp(h)
    assert re.fullmatch(
        r"repos/example-org/example-repo/compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40}\?per_page=1",
        h.script.argvs[0][-1],
    )
