"""`forgectl review` on GitHub (#26): the input, the position check, one atomic post, dedupe, the
state step, the read-back, hooks and the audit.

The real CLI runs in process against the fake `gh`, which logs every call. Nothing reaches a host.
The answers are hand-built from GitHub's documented REST shapes; every assumed field name is
UNVERIFIED until the live suite (#34) re-checks it.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from forgectl import review as review_module
from forgectl.cli import main
from forgectl.errors import ForgectlError

from .harness import golden, schemas
from .harness.safety import assert_call_safe
from .test_github_diff import pull
from .test_github_show import HEAD, TOKEN, recording
from .test_post_github import (
    COMMENTS,
    ME,
    NO_TTY,
    REPO,
    REVIEW_COMMENTS,
    REVIEWS,
    Result,
    Rig,
    answer_get,
    assert_nothing_written,
    hook_config,
    item,
    rig,  # noqa: F401  (the fixture)
)

PR = "github.com/user-7/example-repo#7"
PULL = f"repos/{REPO}/pulls/7"
NODE = "PRR_kwDOnew0001"
REVIEW_REST_ID = 9001
OTHER_HEAD = "4" * 40
# old: 1 ctx, 2 removed, 3 ctx   new: 1 ctx, 2 added, 3 ctx
PATCH = "@@ -1,3 +1,3 @@\n a\n-b\n+c\n d"
MARKER = "<!-- forgectl:dedupe:key-1 -->"


def review_created(**kw: Any) -> dict[str, Any]:
    doc = {
        "id": REVIEW_REST_ID,
        "node_id": NODE,
        "html_url": f"https://github.com/{REPO}/pull/7#pullrequestreview-{REVIEW_REST_ID}",
        "commit_id": HEAD,
        "state": "COMMENTED",
    }
    doc.update(kw)
    return doc


class ReviewRig:
    """The post rig, driven through ``review --file``."""

    def __init__(self, base: Rig) -> None:
        self.base = base
        self.file = base.tmp / "review.json"
        self.pulls_n = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)

    def input(self, doc: Any) -> None:
        self.file.write_text(doc if isinstance(doc, str) else json.dumps(doc), "utf-8")

    def diff(self, patch: str = PATCH, head: str = HEAD) -> None:
        doc = pull(changed_files=1)
        doc["head"] = {**doc["head"], "sha": head}
        answer_get(self.base.forges, PULL, body=doc, times=None)
        files = [
            {
                "filename": "src/app.py",
                "status": "modified",
                "additions": 1,
                "deletions": 1,
                "patch": patch,
            }
        ]
        answer_get(self.base.forges, f"{PULL}/files", body=files, page=1)

    def posted(self, comment_nodes: tuple[str, ...] = ()) -> None:
        self.base.forges.answer("gh", REVIEWS, method="POST", status=200, body=review_created())
        items = [{"node_id": n} for n in comment_nodes]
        for page in range(1, len(items) // 100 + 2):
            answer_get(
                self.base.forges,
                f"{REVIEWS}/{REVIEW_REST_ID}/comments",
                page=page,
                body=items[(page - 1) * 100 : page * 100],
            )

    def run(
        self,
        *args: str,
        expected_head: str | None = HEAD,
        expected_base: str | None = "main",
        yes: bool = True,
        actor: str | None = "auto:bot",
        extra_config: str = "",
        tty: Any = NO_TTY,
        stdin: str = "",
        file: str | None = None,
    ) -> Result:
        argv = ["review", PR, "--json", "--config", str(self.base.config(extra_config))]
        argv += ["--file", file or str(self.file)]
        if expected_head:
            argv += ["--expected-head", expected_head]
        if expected_base:
            argv += ["--expected-base", expected_base]
        if actor:
            argv += ["--actor", actor]
        if yes:
            argv += ["--yes"]
        argv += list(args)
        self.base.monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = main(argv, tty=tty)
        captured = self.base.capsys.readouterr()
        return Result(code, captured.out, captured.err)

    def set_reviews(self, nodes: list[dict[str, Any]]) -> None:
        doc = recording()
        doc["data"]["repository"]["pullRequest"]["reviews"]["nodes"].extend(nodes)
        self.base.set_pr(doc)


@pytest.fixture
def rv(rig: Rig) -> ReviewRig:  # noqa: F811
    return ReviewRig(rig)


def inline(**kw: Any) -> dict[str, Any]:
    return {"path": "src/app.py", "line": 2, "body": "use c here", **kw}


def my_review(state: str, oid: str = HEAD) -> dict[str, Any]:
    return {
        "author": {"__typename": "User", "databaseId": ME, "login": "user-4"},
        "authorCanPushToRepository": True,
        "commit": {"oid": oid},
        "state": state,
        "submittedAt": "2026-10-02T10:00:00Z",
    }


# -- the input (pure) ---------------------------------------------------------------------------


def parse(doc: Any) -> review_module.RawReview:
    text = doc if isinstance(doc, str) else json.dumps(doc)
    return review_module.parse_input(text.encode("utf-8"))


def usage_of(doc: Any) -> str:
    with pytest.raises(ForgectlError) as caught:
        parse(doc)
    assert caught.value.code == "usage"
    return caught.value.message


def test_a_valid_input_parses_with_the_side_defaulted() -> None:
    raw = parse({"event": "comment", "body": "ok", "comments": [inline(), inline(side="left")]})
    assert [c.side for c in raw.comments] == ["right", "left"]
    assert [c.start_line for c in raw.comments] == [None, None]


@pytest.mark.parametrize(
    ("doc", "word"),
    [
        ("[]", "object"),
        ("{", "JSON"),
        ('{"event": "comment", "event": "approve"}', "JSON"),
        ('{"event": "comment", "body": NaN}', "JSON"),
        ({"event": "merge", "body": "x"}, "event"),
        ({"body": "x"}, "event"),
        ({"event": "comment", "body": "x", "extra": 1}, "unknown field"),
        ({"event": "comment", "body": 5}, "body"),
        ({"event": "comment", "body": ""}, "empty"),
        ({"event": "comment", "body": " \n\t"}, "empty"),
        ({"event": "comment", "body": None}, "empty review"),
        ({"event": "comment", "comments": []}, "empty review"),
        ({"event": "request_changes", "body": None}, "request_changes"),
        ({"event": "request_changes"}, "request_changes"),
        ({"event": "comment", "comments": {}}, "list"),
        ({"event": "comment", "comments": [3]}, "object"),
        ({"event": "comment", "comments": [{"path": "a", "body": "x"}]}, "line"),
        ({"event": "comment", "comments": [{"path": "a", "line": 1}]}, "body"),
        ({"event": "comment", "comments": [inline(path="")]}, "path"),
        ({"event": "comment", "comments": [inline(line=0)]}, "line"),
        ({"event": "comment", "comments": [inline(line=True)]}, "line"),
        ({"event": "comment", "comments": [inline(line=1.5)]}, "line"),
        ({"event": "comment", "comments": [inline(line="2")]}, "line"),
        ({"event": "comment", "comments": [inline(start_line=None)]}, "start_line"),
        ({"event": "comment", "comments": [inline(start_line=0)]}, "start_line"),
        ({"event": "comment", "comments": [inline(side="both")]}, "side"),
        ({"event": "comment", "comments": [inline(body="")]}, "empty"),
        ({"event": "comment", "comments": [inline(body="  ")]}, "empty"),
        ({"event": "comment", "comments": [inline(extra=1)]}, "unknown field"),
        ('{"event": "comment", "body": "\\ud800"}', "lone surrogate"),
        ('{"event": "comment", "comments": [{"path": "\\udc00", "line": 1, "body": "x"}]}', "path"),
    ],
)
def test_a_malformed_input_is_a_usage_error_naming_the_field(doc: Any, word: str) -> None:
    assert word in usage_of(doc)


def test_an_approve_with_neither_body_nor_comments_is_allowed() -> None:
    raw = parse({"event": "approve"})
    assert raw.body is None and raw.comments == ()


def test_at_most_200_inline_comments_are_accepted() -> None:
    assert len(parse({"event": "comment", "comments": [inline()] * 200}).comments) == 200
    assert "200" in usage_of({"event": "comment", "comments": [inline()] * 201})


def test_input_that_is_not_utf8_is_a_usage_error() -> None:
    with pytest.raises(ForgectlError) as caught:
        review_module.parse_input(b'{"event": "comment", "body": "\xff"}')
    assert caught.value.code == "usage" and "UTF-8" in caught.value.message


def test_the_schema_and_the_parser_agree_on_what_a_file_may_hold() -> None:
    schema = json.loads(Path("schemas/review-input.json").read_text("utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    docs = [
        {"event": "approve"},
        {"event": "comment", "body": "x", "comments": [inline(), inline(side="left")]},
        {"event": "request_changes", "body": "x"},
        {"event": "request_changes"},
        {"event": "comment", "comments": [inline(line=0)]},
        {"event": "comment", "comments": [inline(side="up")]},
        {"event": "comment", "comments": [{"path": "a", "body": "x"}]},
        {"event": "comment", "comments": [inline(extra=1)]},
        {"event": "nope"},
        {"event": "comment", "body": "x", "extra": 1},
    ]
    for doc in docs:
        try:
            parse(doc)
            ours = True
        except ForgectlError:
            ours = False
        assert validator.is_valid(doc) == ours, doc


def test_the_canonical_form_is_the_documented_json() -> None:
    raw = parse(
        {
            "event": "comment",
            "body": "café /x",
            "comments": [inline(path="sü.py", side="left", start_line=1, line=2)],
        }
    )
    prep = review_module.prepare(raw, "k1")
    expected = (
        '{"body":"café /x\\n<!-- forgectl:dedupe:k1 -->","comments":[{"body":"use c here",'
        '"line":2,"path":"sü.py","side":"left","start_line":1}]}'
    )
    assert review_module.canonical(prep.body, prep.comments).decode("utf-8") == expected
    assert prep.sha256 == hashlib.sha256(expected.encode("utf-8")).hexdigest()


def test_each_body_is_neutralized_alone_and_counted() -> None:
    raw = parse(
        {
            "event": "comment",
            "body": "/approve\nforgectl:dedupe:x\n",
            "comments": [inline(body="/close\nfine")],
        }
    )
    prep = review_module.prepare(raw, None)
    assert prep.body == " /approve\nforgectl:dedupe-x\n"
    assert prep.comments[0].body == " /close\nfine"
    assert (prep.neutralized_lines, prep.markers_removed) == (2, 1)


def test_the_marker_is_alone_in_the_body_when_there_is_none() -> None:
    prep = review_module.prepare(parse({"event": "comment", "comments": [inline()]}), "key-1")
    assert prep.body == MARKER


def test_a_dedupe_key_with_nothing_to_carry_it_is_a_usage_error() -> None:
    with pytest.raises(ForgectlError) as caught:
        review_module.prepare(parse({"event": "approve"}), "key-1")
    assert caught.value.code == "usage"


@pytest.mark.parametrize(
    ("doc", "index", "limit"),
    [
        ({"event": "comment", "body": "a" * 262145}, None, 262144),
        (
            {"event": "comment", "comments": [inline(), inline(body="a" * (1024 * 1024 + 1))]},
            1,
            1048576,
        ),
    ],
)
def test_a_body_over_its_limit_is_body_too_large_with_the_index(
    doc: Any, index: int | None, limit: int
) -> None:
    with pytest.raises(ForgectlError) as caught:
        review_module.prepare(parse(doc), None)
    assert caught.value.code == "body_too_large"
    assert caught.value.details == {"index": index, "limit": limit}


def test_the_marker_counts_toward_the_review_body_limit() -> None:
    doc = {"event": "comment", "body": "a" * 262144}
    review_module.prepare(parse(doc), None)
    with pytest.raises(ForgectlError) as caught:
        review_module.prepare(parse(doc), "key-1")
    assert caught.value.code == "body_too_large"


# -- the run: the happy path --------------------------------------------------------------------


def test_a_review_posts_whole_and_reports_the_read_back(rv: ReviewRig) -> None:
    rv.input(
        {
            "event": "comment",
            "body": "looks fine",
            "comments": [inline(), inline(side="left", line=2, start_line=1, body="ctx")],
        }
    )
    rv.diff()
    rv.posted(("PRRC_kwDOa1", "PRRC_kwDOa2"))
    done = rv.run()
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("review", out)
    assert out == {
        "pr": PR,
        "action": "review",
        "ids": ["gh:rc:PRRC_kwDOa1", "gh:rc:PRRC_kwDOa2"],
        "thread_ids": [],
        "review_id": f"gh:r:{NODE}",
        "already_posted": False,
        "follow_up_done": False,
        "neutralized_lines": 0,
        "markers_removed": 0,
        "warnings": [],
        "head_sha": HEAD,
        "head_verified": None,
        "dry_run": False,
    }
    (sent,) = rv.sent()
    assert sent == {
        "commit_id": HEAD,
        "event": "COMMENT",
        "body": "looks fine",
        "comments": [
            {"path": "src/app.py", "line": 2, "side": "RIGHT", "body": "use c here"},
            {
                "path": "src/app.py",
                "line": 2,
                "side": "LEFT",
                "start_line": 1,
                "start_side": "LEFT",
                "body": "ctx",
            },
        ],
    }
    golden.assert_golden("review/github-posted", out, schema_id="forgectl/cli/review.json")
    for call in rv.calls():
        assert_call_safe(call, host="github.com")
        assert call.token_sent == TOKEN


def test_a_body_only_review_has_no_comment_ids_and_needs_no_diff(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "body": "just words"})
    rv.posted()
    done = rv.run()
    assert done.code == 0, done.err
    assert done.json["ids"] == [] and done.json["review_id"] == f"gh:r:{NODE}"
    assert not any("files" in " ".join(c.argv) for c in rv.calls())
    (sent,) = rv.sent()
    assert "comments" not in sent and sent["body"] == "just words"


def test_inline_comments_alone_send_no_body(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "comments": [inline()]})
    rv.diff()
    rv.posted(("PRRC_kwDOa1",))
    assert rv.run().code == 0
    (sent,) = rv.sent()
    assert "body" not in sent and len(sent["comments"]) == 1


def test_a_request_changes_review_posts_its_event(rv: ReviewRig) -> None:
    rv.input({"event": "request_changes", "body": "please fix"})
    rv.posted()
    done = rv.run()
    assert done.code == 0, done.err
    assert rv.sent()[0]["event"] == "REQUEST_CHANGES"
    assert rv.audit()[-1]["result"] == "posted"


def test_a_bodyless_approve_sends_the_event_and_the_commit_only(rv: ReviewRig) -> None:
    rv.input({"event": "approve"})
    rv.posted()
    done = rv.run()
    assert done.code == 0, done.err
    assert rv.sent() == [{"commit_id": HEAD, "event": "APPROVE"}]
    assert done.json["head_verified"] is None and done.json["already_posted"] is False
    assert rv.audit()[-1]["result"] == "approved"
    assert [c.argv[-1] for c in rv.calls()].count("graphql") == 2  # the read and the post-check


def test_an_approve_with_text_posts_it_even_when_an_approval_exists(rv: ReviewRig) -> None:
    rv.set_reviews([my_review("APPROVED")])
    rv.input({"event": "approve", "body": "still good"})
    rv.posted()
    done = rv.run()
    assert done.code == 0, done.err
    assert rv.sent()[0]["body"] == "still good"
    assert done.json["head_verified"] is False  # exists, and forgectl cannot tell it counts


def test_a_bodyless_approve_over_an_existing_approval_sends_nothing(rv: ReviewRig) -> None:
    rv.set_reviews([my_review("APPROVED")])
    rv.input({"event": "approve"})
    done = rv.run()
    assert done.code == 0, done.err
    assert done.json["already_posted"] is True and done.json["head_verified"] is False
    assert rv.posts() == [] and rv.audit() == []
    schemas.validate_cli_output("review", done.json)


def test_an_approval_at_an_old_head_is_approved_again(rv: ReviewRig) -> None:
    rv.set_reviews([my_review("APPROVED", oid=OTHER_HEAD)])
    rv.input({"event": "approve"})
    rv.posted()
    done = rv.run()
    assert done.code == 0, done.err
    assert done.json["head_verified"] is None and rv.sent()[0]["event"] == "APPROVE"


def test_the_audit_holds_hashes_and_ids_only(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "body": "secret words", "comments": [inline()]})
    rv.diff()
    rv.posted(("PRRC_kwDOa1",))
    done = rv.run("--decision-id", "d-1")
    assert done.code == 0, done.err
    first, second = rv.audit()
    assert (first["result"], second["result"]) == ("attempted", "posted")
    assert first["action"] == "review" and first["inputs"]["event"] == "comment"
    prep = review_module.prepare(
        parse({"event": "comment", "body": "secret words", "comments": [inline()]}), None
    )
    assert first["inputs"]["body_sha256"] == prep.sha256
    assert first["inputs"]["head_sha"] == HEAD and first["decision_id"] == ["d-1"]
    assert second["ids"] == [f"gh:r:{NODE}", "gh:rc:PRRC_kwDOa1"]
    assert "secret words" not in json.dumps(rv.audit()) and TOKEN not in json.dumps(rv.audit())


def test_200_inline_comments_leave_the_audit_record_under_the_line_cap(rv: ReviewRig) -> None:
    comments = [inline(body=f"c{n}") for n in range(200)]
    rv.input({"event": "comment", "comments": comments})
    rv.diff()
    nodes = tuple(f"PRRC_{n:0>95}" for n in range(200))
    rv.posted(nodes)
    done = rv.run()
    assert done.code == 0, done.err
    assert len(done.json["ids"]) == 200
    outcome = rv.audit()[-1]
    assert outcome["result"] == "posted" and len(json.dumps(outcome)) < 64 * 1024


# -- the checks before the write ------------------------------------------------------------------


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_a_pr_that_is_not_open_is_refused(rv: ReviewRig, state: str) -> None:
    doc = recording()
    pr = doc["data"]["repository"]["pullRequest"]
    pr["state"] = state
    pr["closedAt"] = "2026-10-02T11:00:00Z"
    if state == "MERGED":
        pr["mergedAt"] = "2026-10-02T11:00:00Z"
        pr["mergeCommit"] = {"oid": "5" * 40}
        pr["mergedBy"] = {"__typename": "User", "login": "user-2", "databaseId": 1002}
    rv.base.set_pr(doc)
    rv.input({"event": "comment", "body": "x"})
    done = rv.run()
    assert done.code == 20 and done.error["reason"] == "not_open"
    assert done.error["details"] == {"state": state.lower()}
    assert_nothing_written(rv.base)


def test_a_stale_head_is_refused_before_anything_is_written(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "body": "x"})
    done = rv.run(expected_head=OTHER_HEAD)
    assert done.code == 20 and done.error["reason"] == "stale_head"
    assert done.error["details"] == {"head_sha": HEAD}
    assert_nothing_written(rv.base)


def test_a_stale_base_is_refused(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "body": "x"})
    done = rv.run(expected_base="develop")
    assert done.code == 20 and done.error["reason"] == "stale_base"
    assert done.error["details"] == {"base_ref": "main"}
    assert_nothing_written(rv.base)


def test_a_head_that_moved_during_the_position_check_is_stale_head(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "comments": [inline()]})
    rv.diff(head=OTHER_HEAD)
    done = rv.run()
    assert done.code == 20 and done.error["reason"] == "stale_head"
    assert done.error["details"] == {"head_sha": OTHER_HEAD}
    assert_nothing_written(rv.base)


def test_a_comment_off_the_diff_is_invalid_position_and_nothing_is_written(rv: ReviewRig) -> None:
    rv.input(
        {
            "event": "comment",
            "body": "x",
            "comments": [inline(), inline(line=99), inline(path="no")],
        }
    )
    rv.diff()
    done = rv.run()
    assert done.code == 12 and done.error["code"] == "invalid_position"
    assert [i["index"] for i in done.error["details"]["invalid"]] == [1, 2]
    assert_nothing_written(rv.base)


def test_a_comment_on_a_removed_line_is_only_valid_on_the_left(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "comments": [inline(line=2, side="left")]})
    rv.diff()
    rv.posted(("PRRC_kwDOa1",))
    assert rv.run().code == 0
    assert rv.sent()[0]["comments"][0]["side"] == "LEFT"


def test_a_422_after_the_local_check_passed_is_host_rejected(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "comments": [inline()]})
    rv.diff()
    rv.forges.answer(
        "gh", REVIEWS, method="POST", status=422, body={"message": "Unprocessable: line invalid"}
    )
    done = rv.run()
    assert done.code == 12 and done.error["code"] == "host_rejected"
    assert done.error["details"]["status"] == 422
    assert [r["result"] for r in rv.audit()] == ["attempted", "failed"]


def test_a_pending_review_422_names_the_review_in_the_way(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "body": "x"})
    rv.forges.answer(
        "gh",
        REVIEWS,
        method="POST",
        status=422,
        body={
            "message": "Unprocessable Entity",
            "errors": ["User can only have one pending review per pull request"],
        },
    )
    # the message carries the host's text; the lookup finds the account's pending review
    answer_get(
        rv.forges,
        REVIEWS,
        body=[
            {"node_id": "PRR_other", "state": "PENDING", "user": {"id": 99}},
            {"node_id": "PRR_mine", "state": "PENDING", "user": {"id": ME}},
        ],
    )
    done = rv.run()
    assert done.code == 20, done.out
    assert done.error["reason"] == "pending_review_exists"
    assert "gh:r:PRR_mine" in done.error["message"] and "PRR_other" not in done.error["message"]
    assert [r["result"] for r in rv.audit()] == ["attempted", "failed"]


# -- the write proof ------------------------------------------------------------------------------


def test_a_redirected_write_that_answers_a_list_is_not_a_post(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "body": "x"})
    rv.forges.answer("gh", REVIEWS, method="POST", status=200, body=[])
    done = rv.run()
    assert done.code == 12 and done.error["code"] == "host_rejected"


@pytest.mark.parametrize(
    "changes",
    [
        {"commit_id": OTHER_HEAD},
        {"commit_id": None},
        {"id": None},
        {"id": True},
        {"node_id": "bad node!"},
        {"html_url": "https://evil.example/o/r/pull/7"},
    ],
)
def test_an_answer_without_the_proof_is_never_reported_posted(
    rv: ReviewRig, changes: dict[str, Any]
) -> None:
    rv.input({"event": "comment", "body": "x"})
    rv.forges.answer("gh", REVIEWS, method="POST", status=200, body=review_created(**changes))
    done = rv.run()
    assert done.code == 12 and done.error["code"] in ("outcome_unknown", "wrong_host_response")
    assert rv.audit()[-1]["result"] == "outcome_unknown"


def test_a_201_is_not_the_documented_status(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "body": "x"})
    rv.forges.answer("gh", REVIEWS, method="POST", status=201, body=review_created())
    done = rv.run()
    assert done.code == 12 and done.error["code"] == "outcome_unknown"


def test_a_5xx_on_the_write_is_outcome_unknown_and_is_not_retried(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "body": "x"})
    rv.forges.answer("gh", REVIEWS, method="POST", status=502, body={"message": "bad"}, times=None)
    done = rv.run()
    assert done.code == 12 and done.error["code"] == "outcome_unknown"
    assert len(rv.posts()) == 1


def test_a_failed_read_back_is_a_warning_with_no_ids(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "comments": [inline()]})
    rv.diff()
    rv.base.forges.answer("gh", REVIEWS, method="POST", status=200, body=review_created())
    answer_get(rv.forges, f"{REVIEWS}/{REVIEW_REST_ID}/comments", status=500, body={}, times=None)
    done = rv.run("--timeout", "20")
    assert done.code == 0, done.err
    assert done.json["ids"] == [] and done.json["warnings"] == ["read_back_failed"]
    assert done.json["review_id"] == f"gh:r:{NODE}"
    assert rv.audit()[-1]["result"] == "posted"


def test_a_read_back_that_does_not_match_the_count_sent_is_a_failed_read_back(
    rv: ReviewRig,
) -> None:
    rv.input({"event": "comment", "comments": [inline(), inline(body="two")]})
    rv.diff()
    rv.posted(("PRRC_kwDOa1",))
    done = rv.run()
    assert done.code == 0 and done.json["ids"] == []
    assert done.json["warnings"] == ["read_back_failed"]


def test_an_approval_that_landed_on_a_moved_head_is_approved_stale_head(rv: ReviewRig) -> None:
    moved = recording()
    moved_pr = moved["data"]["repository"]["pullRequest"]
    moved_pr["headRefOid"] = OTHER_HEAD
    moved_pr["commits"]["nodes"][0]["commit"]["oid"] = OTHER_HEAD
    rv.forges._rules[rv.base.pr_rule]["times"] = 1
    rv.forges._write_script()
    rv.forges.answer(
        "gh", "graphql", method="POST", stdin_re=r"pullRequest\(number", body=moved, times=None
    )
    rv.input({"event": "approve"})
    rv.posted()
    done = rv.run()
    assert done.code == 12 and done.error["code"] == "approved_stale_head"
    assert done.error["details"]["ids"] == [f"gh:r:{NODE}"]
    assert [r["result"] for r in rv.audit()] == ["attempted", "approved"]


# -- dedupe and the state step ------------------------------------------------------------


def test_a_dedupe_key_adds_the_marker_to_the_review_body(rv: ReviewRig) -> None:
    rv.comments_empty()
    rv.input({"event": "comment", "body": "words"})
    rv.posted()
    assert rv.run("--dedupe-key", "key-1").code == 0
    assert rv.sent()[0]["body"] == f"words\n{MARKER}"


def test_a_marker_alone_is_the_body_when_the_review_has_only_comments(rv: ReviewRig) -> None:
    rv.comments_empty()
    rv.input({"event": "comment", "comments": [inline()]})
    rv.diff()
    rv.posted(("PRRC_kwDOa1",))
    assert rv.run("--dedupe-key", "key-1").code == 0
    assert rv.sent()[0]["body"] == MARKER


def _hit(rv: ReviewRig) -> None:
    answer_get(rv.forges, COMMENTS, body=[])
    answer_get(rv.forges, REVIEW_COMMENTS, body=[])
    answer_get(rv.forges, REVIEWS, body=[item("PRR_old", f"words\n{MARKER}")])


def test_a_dedupe_hit_for_a_comment_review_is_exit_0_before_confirmation(rv: ReviewRig) -> None:
    _hit(rv)
    rv.input({"event": "comment", "body": "words"})
    done = rv.run("--dedupe-key", "key-1", yes=False)  # no --yes, no tty: would be exit 13
    assert done.code == 0, done.err
    assert done.json["already_posted"] is True
    assert done.json["ids"] == ["gh:r:PRR_old"] and done.json["review_id"] == "gh:r:PRR_old"
    assert rv.posts() == [] and rv.audit() == []
    schemas.validate_cli_output("review", done.json)


def test_a_hit_with_a_dismissed_approval_is_outcome_partial_and_writes_nothing(
    rv: ReviewRig,
) -> None:
    _hit(rv)
    rv.input({"event": "approve", "body": "words"})
    done = rv.run("--dedupe-key", "key-1")
    assert done.code == 12 and done.error["code"] == "outcome_partial"
    assert done.error["details"] == {
        "landed": {"ids": ["gh:r:PRR_old"], "thread_ids": []},
        "failed": "approve",
    }
    assert rv.posts() == [] and rv.audit() == []


def test_a_hit_with_request_changes_not_in_effect_is_outcome_partial(rv: ReviewRig) -> None:
    _hit(rv)
    rv.set_reviews([my_review("CHANGES_REQUESTED")])
    rv.input({"event": "request_changes", "body": "words"})
    assert rv.run("--dedupe-key", "key-1").code == 0  # in effect: nothing more to do
    rv.set_reviews([my_review("DISMISSED")])
    done = rv.run("--dedupe-key", "key-1")
    assert done.code == 12 and done.error["details"]["failed"] == "request_changes"


def test_a_hit_with_the_approval_in_effect_is_exit_0(rv: ReviewRig) -> None:
    _hit(rv)
    rv.set_reviews([my_review("APPROVED")])
    rv.input({"event": "approve", "body": "words"})
    done = rv.run("--dedupe-key", "key-1")
    assert done.code == 0, done.err
    assert done.json["already_posted"] is True and done.json["head_verified"] is False
    assert rv.posts() == []


def test_a_new_key_posts_text_and_state_together(rv: ReviewRig) -> None:
    rv.comments_empty()
    rv.input({"event": "approve", "body": "words"})
    rv.posted()
    done = rv.run("--dedupe-key", "key-2")
    assert done.code == 0, done.err
    assert rv.sent()[0]["event"] == "APPROVE"


def test_positions_are_checked_before_a_dedupe_hit_is_honored(rv: ReviewRig) -> None:
    _hit(rv)
    rv.input({"event": "comment", "body": "words", "comments": [inline(line=99)]})
    rv.diff()
    assert rv.run("--dedupe-key", "key-1").code == 12


# -- dry run, confirmation ------------------------------------------------------------------------


def test_dry_run_prints_the_plan_and_writes_nothing(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "body": "words", "comments": [inline()]})
    rv.diff()
    done = rv.run("--dry-run", yes=False)
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("review", out)
    prep = review_module.prepare(
        parse({"event": "comment", "body": "words", "comments": [inline()]}), None
    )
    assert out["dry_run"] is True and out["would_refuse"] is None
    assert out["plan"] == {
        "event": "comment",
        "body_sha256": prep.sha256,
        "body_bytes": len(b"words") + len(b"use c here"),
        "inline_comments": 1,
    }
    golden.assert_golden("review/github-dry-run", out, schema_id="forgectl/cli/review.json")
    assert rv.posts() == [] and rv.audit() == []


def test_dry_run_of_a_refused_review_reports_would_refuse(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "body": "words"})
    done = rv.run("--dry-run", yes=False, expected_head=OTHER_HEAD)
    assert done.code == 20, done.err
    out = done.json
    schemas.validate_cli_output("review", out)
    assert out["would_refuse"]["reason"] == "stale_head" and out["plan"]["event"] == "comment"
    golden.assert_golden(
        "review/github-dry-run-would-refuse", out, schema_id="forgectl/cli/review.json"
    )


def test_a_dry_run_with_a_bad_position_is_still_exit_12(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "comments": [inline(line=99)]})
    rv.diff()
    done = rv.run("--dry-run", yes=False)
    assert done.code == 12 and done.error["code"] == "invalid_position"


def test_without_confirmation_a_non_interactive_run_is_exit_13(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "body": "words"})
    done = rv.run(yes=False)
    assert done.code == 13 and rv.posts() == []


def test_an_interactive_run_pins_the_head_and_base_it_reads(rv: ReviewRig) -> None:
    from .test_post_github import HUMAN

    rv.input({"event": "comment", "body": "words"})
    rv.posted()
    done = rv.run(
        expected_head=None, expected_base=None, tty=HUMAN, stdin="y\n", yes=False, actor=None
    )
    assert done.code == 0, done.err
    assert rv.sent()[0]["commit_id"] == HEAD


def test_a_declined_dialog_writes_nothing(rv: ReviewRig) -> None:
    from .test_post_github import HUMAN

    rv.input({"event": "comment", "body": "words"})
    done = rv.run(tty=HUMAN, stdin="n\n", yes=False, actor=None)
    assert done.code == 13 and rv.posts() == []


# -- input handling at the command ----------------------------------------------------------------


def test_a_bad_file_is_exit_2_before_any_host_call(rv: ReviewRig) -> None:
    rv.input({"event": "comment"})
    done = rv.run()
    assert done.code == 2 and done.error["code"] == "usage"
    assert rv.calls() == []


def test_a_missing_file_is_exit_2(rv: ReviewRig) -> None:
    done = rv.run(file=str(rv.base.tmp / "absent.json"))
    assert done.code == 2 and rv.calls() == []


def test_a_file_over_16_mib_is_body_too_large_with_a_null_index(rv: ReviewRig) -> None:
    rv.file.write_bytes(b" " * (16 * 1024 * 1024 + 1))
    done = rv.run()
    assert done.code == 2 and done.error["code"] == "body_too_large"
    assert done.error["details"] == {"index": None, "limit": 16 * 1024 * 1024}
    assert rv.calls() == []


def test_the_file_may_be_read_from_stdin(rv: ReviewRig) -> None:
    rv.posted()
    done = rv.run(file="-", stdin=json.dumps({"event": "comment", "body": "piped"}))
    assert done.code == 0, done.err
    assert rv.sent()[0]["body"] == "piped"


# -- hooks ----------------------------------------------------------------------------------------


def test_the_pre_post_hook_gets_the_review_payload_without_a_body(
    rv: ReviewRig, tmp_path: Path
) -> None:
    out = tmp_path / "payload.json"
    rv.input({"event": "request_changes", "body": "private words"})
    rv.posted()
    done = rv.run(extra_config=hook_config("pre-post", f"cat > {out}"))
    assert done.code == 0, done.err
    payload = json.loads(out.read_text())
    schemas.validate_hook_payload("pre-post", payload)
    assert payload["action"] == "review" and payload["event"] == "request_changes"
    assert payload["body_bytes"] == len(b"private words") and payload["allow_self"] is False
    assert "private words" not in out.read_text()


def test_a_pre_post_veto_stops_the_review(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "body": "words"})
    done = rv.run(extra_config=hook_config("pre-post", "echo no >&2; exit 3"))
    assert done.code == 11 and done.error["code"] == "vetoed"
    assert_nothing_written(rv.base)


def test_post_post_fires_with_the_result_and_ids(rv: ReviewRig, tmp_path: Path) -> None:
    out = tmp_path / "post.json"
    rv.input({"event": "approve", "body": "words"})
    rv.posted()
    done = rv.run(extra_config=hook_config("post-post", f"cat > {out}"))
    assert done.code == 0, done.err
    payload = json.loads(out.read_text())
    schemas.validate_hook_payload("post-post", payload)
    assert payload["result"] == "approved" and payload["ids"] == [f"gh:r:{NODE}"]


def test_post_post_fires_on_a_failed_write(rv: ReviewRig, tmp_path: Path) -> None:
    out = tmp_path / "post.json"
    rv.input({"event": "comment", "body": "words"})
    rv.forges.answer("gh", REVIEWS, method="POST", status=422, body={"message": "no"})
    done = rv.run(extra_config=hook_config("post-post", f"cat > {out}"))
    assert done.code == 12
    assert json.loads(out.read_text())["result"] == "failed"


def test_a_head_that_moves_during_a_hook_is_caught_by_the_re_read(rv: ReviewRig) -> None:
    moved = recording()
    moved_pr = moved["data"]["repository"]["pullRequest"]
    moved_pr["headRefOid"] = OTHER_HEAD
    moved_pr["commits"]["nodes"][0]["commit"]["oid"] = OTHER_HEAD
    rv.forges._rules[rv.base.pr_rule]["times"] = 1
    rv.forges._write_script()
    rv.forges.answer(
        "gh", "graphql", method="POST", stdin_re=r"pullRequest\(number", body=moved, times=None
    )
    rv.input({"event": "comment", "body": "words"})
    done = rv.run(extra_config=hook_config("pre-post", "exit 0"))
    assert done.code == 20 and done.error["reason"] == "stale_head"
    assert rv.posts() == []


# -- GitLab ---------------------------------------------------------------------------------------


def test_review_on_gitlab_fails_as_not_implemented_until_27(rv: ReviewRig) -> None:
    rv.input({"event": "comment", "body": "words"})
    config = rv.base.config("  gitlab.com:\n    token_env: POST_TOKEN\n")
    argv = ["review", "gitlab.com/user-9/proj#7", "--json", "--config", str(config)]
    argv += ["--file", str(rv.file)]
    argv += ["--expected-head", HEAD, "--expected-base", "main", "--actor", "auto:bot", "--yes"]
    rv.monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code = main(argv, tty=NO_TTY)
    captured = rv.capsys.readouterr()
    assert code == 1
    error = json.loads(captured.out)["error"]
    assert error["code"] == "internal" and "#27" in error["message"]
    assert rv.calls() == []
