"""`forgectl comment` and `reply` on GitHub (#24): the post order, the body input, neutralizing,
dedupe, the write proof, hooks, audit and signals.

The real CLI runs in process against the fake `gh` (tests/fakebin), which logs every call; a few
tests run it as a child for stdin pipes and signals. Nothing here reaches a host. The answers are
hand-built from GitHub's documented REST and GraphQL shapes; every assumed field name is
UNVERIFIED until the live suite (#34) re-checks it.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from forgectl import post as post_module
from forgectl.cli import main
from forgectl.errors import ForgectlError
from forgectl.humanactions import Tty

from .harness import golden, schemas
from .harness.fixtures import forgectl_env
from .harness.forges import Call, FakeForges
from .harness.procs import ProcessRegistry
from .harness.safety import assert_call_safe
from .test_github_show import HEAD, TOKEN, recording

PR = "github.com/user-7/example-repo#7"
REPO = "user-7/example-repo"
COMMENTS = f"repos/{REPO}/issues/7/comments"
REVIEW_COMMENTS = f"repos/{REPO}/pulls/7/comments"
REVIEWS = f"repos/{REPO}/pulls/7/reviews"
ME = 4242
OTHER_ACCOUNT = 99
NO_TTY = Tty(stdin=False, stderr=False)
HUMAN = Tty(stdin=True, stderr=True)
NODE = "IC_kwDOtest0001"
NEW_NODE = "IC_kwDOnew00001"
SHOW = r"pullRequest\(number"
MARKER = "<!-- forgectl:dedupe:key-1 -->"


def answer_get(forges: FakeForges, endpoint: str, *, page: int | None = None, **kw: Any) -> None:
    """A GET answer matched on the path whatever the query; ``page`` narrows it to one page."""
    tail = rf"\?.*page={page}(&|$)" if page else r"(\?|$)"
    forges.answer("gh", endpoint_re=f"^{re.escape(endpoint)}{tail}", method="GET", **kw)


def created(node: str = NEW_NODE, *, number: int = 7001, url_tail: str = "issuecomment-1") -> dict:
    return {
        "id": number,
        "node_id": node,
        "html_url": f"https://github.com/{REPO}/pull/7#{url_tail}",
        "body": "ignored",
    }


def item(node: str, body: str, user: int = ME, **kw: Any) -> dict[str, Any]:
    return {"node_id": node, "body": body, "user": {"id": user}, **kw}


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def json(self) -> Any:
        return json.loads(self.out)

    @property
    def error(self) -> dict[str, Any]:
        schemas.validate_error_envelope(self.json)
        body = self.json["error"]
        assert isinstance(body, dict)
        return body


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        forges: FakeForges,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self.tmp = tmp_path
        self.forges = forges
        self.monkeypatch = monkeypatch
        self.capsys = capsys
        for key, value in forgectl_env(forges, {"POST_TOKEN": TOKEN}).items():
            monkeypatch.setenv(key, value)
        self.body_path = tmp_path / "body.md"
        self.write_body("hello from a test\n")
        forges.answer(
            "gh",
            "user",
            method="GET",
            body={"login": "user-4", "id": ME, "html_url": "https://github.com/user-4"},
        )
        forges.answer("gh", "graphql", method="POST", stdin_re=SHOW, body=recording(), times=None)
        self.pr_rule = len(forges._rules) - 1

    def set_pr(self, doc: dict[str, Any]) -> None:
        self.forges._rules[self.pr_rule]["body"] = doc
        self.forges._write_script()

    def write_body(self, text: str | bytes) -> None:
        data = text.encode() if isinstance(text, str) else text
        self.body_path.write_bytes(data)

    def comments_empty(self) -> None:
        for endpoint in (COMMENTS, REVIEW_COMMENTS, REVIEWS):
            answer_get(self.forges, endpoint, body=[])

    def post_ok(self, endpoint: str = COMMENTS, **kw: Any) -> None:
        self.forges.answer("gh", endpoint, method="POST", status=201, body=created(**kw))

    def config(self, extra: str = "") -> Path:
        path = self.tmp / "config.yaml"
        path.write_text("hosts:\n  github.com:\n    token_env: POST_TOKEN\n" + extra, "utf-8")
        return path

    def run(
        self,
        command: str = "comment",
        *args: str,
        tty: Tty = NO_TTY,
        stdin: str = "",
        extra_config: str = "",
        actor: str | None = "auto:bot",
        yes: bool = True,
        body_file: str | None = None,
    ) -> Result:
        argv = [command, PR, "--json", "--config", str(self.config(extra_config))]
        argv += ["--body-file", body_file or str(self.body_path)]
        if actor:
            argv += ["--actor", actor]
        if yes:
            argv += ["--yes"]
        argv += list(args)
        self.monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = main(argv, tty=tty)
        captured = self.capsys.readouterr()
        return Result(code, captured.out, captured.err)

    def calls(self) -> list[Call]:
        return self.forges.calls("gh")

    def posts(self) -> list[Call]:
        return [c for c in self.calls() if "POST" in c.argv and c.argv[-1] != "graphql"]

    def sent(self) -> list[dict[str, Any]]:
        return [json.loads(c.stdin or "") for c in self.posts()]

    def state_file(self, name: str) -> list[dict[str, Any]]:
        path = Path(os.environ["XDG_STATE_HOME"]) / "forgectl" / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def audit(self) -> list[dict[str, Any]]:
        records = self.state_file("audit.jsonl")
        for record in records:
            schemas.validate_audit_record(record)
        return records


@pytest.fixture
def rig(
    tmp_path: Path,
    fake_forges: FakeForges,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Rig:
    return Rig(tmp_path, fake_forges, monkeypatch, capsys)


def hook_config(name: str, cmd: str) -> str:
    return f"hooks:\n  {name}:\n    - cmd: {json.dumps(cmd)}\n"


def assert_nothing_written(rig: Rig) -> None:
    assert rig.posts() == []
    assert rig.audit() == []


# -- the happy path --------------------------------------------------------------------------


def test_a_comment_posts_the_body_and_reports_the_new_id(rig: Rig) -> None:
    rig.post_ok()
    done = rig.run()
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("comment", out)
    assert out == {
        "pr": PR,
        "action": "comment",
        "ids": [f"gh:ic:{NEW_NODE}"],
        "thread_ids": [],
        "review_id": None,
        "already_posted": False,
        "follow_up_done": False,
        "neutralized_lines": 0,
        "markers_removed": 0,
        "warnings": [],
        "head_sha": HEAD,
        "dry_run": False,
    }
    assert rig.sent() == [{"body": "hello from a test\n"}]
    golden.assert_golden("comment/github-posted", out, schema_id="forgectl/cli/comment.json")
    for call in rig.calls():
        assert_call_safe(call, host="github.com")
        assert call.token_sent == TOKEN


def test_a_comment_on_a_closed_pr_is_allowed(rig: Rig) -> None:
    doc = recording()
    doc["data"]["repository"]["pullRequest"]["state"] = "CLOSED"
    doc["data"]["repository"]["pullRequest"]["closedAt"] = "2026-10-02T11:00:00Z"
    rig.set_pr(doc)
    rig.post_ok()
    assert rig.run().code == 0


def test_the_audit_records_attempted_then_posted_with_hashes_only(rig: Rig) -> None:
    rig.post_ok()
    rig.run("comment", "--decision-id", "d-1")
    first, second = rig.audit()
    assert (first["result"], second["result"]) == ("attempted", "posted")
    assert first["attempt_id"] == second["attempt_id"]
    assert first["action"] == "comment" and first["target"] == PR
    assert first["inputs"]["head_sha"] == HEAD
    assert first["inputs"]["body_sha256"] == hashlib.sha256(b"hello from a test\n").hexdigest()
    assert first["inputs"]["host_login"] == "user-4"
    assert first["decision_id"] == ["d-1"]
    assert second["ids"] == [f"gh:ic:{NEW_NODE}"]
    assert "hello from a test" not in json.dumps(rig.audit())
    assert TOKEN not in json.dumps(rig.audit())


def test_attempted_is_on_disk_before_the_post_reaches_the_host(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forgectl.backends import github_post

    seen: list[list[str]] = []
    original = github_post.GitHubPoster.post_comment

    def spy(self: Any, pr: Any, body: str) -> Any:
        seen.append([r["result"] for r in rig.audit()])
        return original(self, pr, body)

    monkeypatch.setattr(github_post.GitHubPoster, "post_comment", spy)
    rig.post_ok()
    rig.run()
    assert seen == [["attempted"]]


# -- neutralizing and the marker -------------------------------------------------------------


def test_every_body_is_neutralized_and_the_counts_are_reported(rig: Rig) -> None:
    rig.write_body("/approve\r\nfine\n```\n/close\n```\nforgectl:dedupe:x\n")
    rig.post_ok()
    done = rig.run()
    assert done.code == 0, done.err
    assert done.json["neutralized_lines"] == 2 and done.json["markers_removed"] == 1
    (sent,) = rig.sent()
    assert sent["body"] == " /approve\nfine\n```\n /close\n```\nforgectl:dedupe-x\n"


def test_a_dedupe_key_adds_the_marker_as_the_last_line(rig: Rig) -> None:
    rig.comments_empty()
    rig.post_ok()
    done = rig.run("comment", "--dedupe-key", "key-1")
    assert done.code == 0, done.err
    (sent,) = rig.sent()
    assert sent["body"] == f"hello from a test\n{MARKER}"


def test_a_planted_marker_in_the_body_is_defused_before_the_real_one_is_added(rig: Rig) -> None:
    rig.write_body("text <!-- forgectl:dedupe:other -->")
    rig.comments_empty()
    rig.post_ok()
    done = rig.run("comment", "--dedupe-key", "key-1")
    assert done.json["markers_removed"] == 1
    (sent,) = rig.sent()
    assert sent["body"] == f"text <!-- forgectl:dedupe-other -->\n{MARKER}"


# -- the body input --------------------------------------------------------------------------


def test_an_empty_body_is_a_usage_error_before_any_call(rig: Rig) -> None:
    rig.write_body("")
    done = rig.run()
    assert done.code == 2 and done.error["code"] == "usage"
    assert rig.calls() == []


def test_a_whitespace_only_body_is_a_usage_error(rig: Rig) -> None:
    rig.write_body(" \n\t\n")
    assert rig.run().code == 2


def test_invalid_utf8_is_a_usage_error(rig: Rig) -> None:
    rig.write_body(b"ok \xff\xfe broken")
    done = rig.run()
    assert done.code == 2 and "UTF-8" in done.error["message"]
    assert rig.calls() == []


def test_a_body_over_the_github_limit_is_body_too_large(rig: Rig) -> None:
    rig.write_body("a" * 262145)
    done = rig.run()
    assert done.code == 2
    assert done.error["code"] == "body_too_large"
    assert done.error["details"] == {"index": None, "limit": 262144}
    assert rig.calls() == []


def test_a_body_exactly_at_the_limit_is_posted(rig: Rig) -> None:
    rig.write_body("a" * 262144)
    rig.post_ok()
    assert rig.run().code == 0


def test_the_marker_counts_toward_the_limit(rig: Rig) -> None:
    rig.write_body("a" * 262144)
    done = rig.run("comment", "--dedupe-key", "key-1")
    assert done.code == 2 and done.error["code"] == "body_too_large"
    assert rig.calls() == []


def test_neutralizing_counts_toward_the_limit(rig: Rig) -> None:
    rig.write_body("/" * 1 + "a" * 262143)  # one more byte after the leading space
    done = rig.run()
    assert done.code == 2 and done.error["code"] == "body_too_large"


def test_a_reply_that_becomes_an_inline_review_comment_may_use_the_inline_limit(
    rig: Rig,
) -> None:
    answer_anchor(
        rig, anchor_doc("PullRequestReviewThread", comments={"nodes": [{"databaseId": 555}]})
    )
    rig.post_ok(f"{REVIEW_COMMENTS}/555/replies", node="PRRC_new0001")
    rig.write_body("a" * 1024 * 1024)
    assert reply(rig, "gh:rt:PRRT_thread01").code == 0
    rig.write_body("a" * (1024 * 1024 + 1))
    done = reply(rig, "gh:rt:PRRT_thread01")
    assert done.code == 2 and done.error["details"] == {"index": None, "limit": 1024 * 1024}


def test_a_reply_that_becomes_a_general_comment_keeps_the_comment_limit(rig: Rig) -> None:
    rig.write_body("a" * 262145)
    done = reply(rig, "gh:ic:IC_old00001")
    assert done.code == 2 and done.error["details"] == {"index": None, "limit": 262144}
    assert rig.calls() == []


def test_a_fifo_is_refused_instead_of_hanging(rig: Rig, tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    done = rig.run(body_file=str(fifo))
    assert done.code == 2 and "regular file" in done.error["message"]
    assert rig.calls() == []


def test_a_final_symlink_is_refused(rig: Rig, tmp_path: Path) -> None:
    link = tmp_path / "link.md"
    link.symlink_to(rig.body_path)
    done = rig.run(body_file=str(link))
    assert done.code == 2 and done.error["code"] == "usage"
    assert rig.calls() == []


def test_a_missing_file_is_a_usage_error(rig: Rig, tmp_path: Path) -> None:
    assert rig.run(body_file=str(tmp_path / "nope.md")).code == 2


def test_a_directory_is_refused(rig: Rig, tmp_path: Path) -> None:
    assert rig.run(body_file=str(tmp_path)).code == 2


def test_a_parent_symlink_is_not_checked(rig: Rig, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "b.md").write_text("via a parent link", "utf-8")
    (tmp_path / "parent").symlink_to(real)
    rig.post_ok()
    assert rig.run(body_file=str(tmp_path / "parent" / "b.md")).code == 0
    assert rig.sent() == [{"body": "via a parent link"}]


def test_unicode_and_a_leading_bom_pass_through_as_text(rig: Rig) -> None:
    rig.write_body("﻿/approve ☃ \U0001f600\n")
    rig.post_ok()
    assert rig.run().code == 0
    assert rig.sent() == [{"body": "﻿/approve ☃ \U0001f600\n"}]


# -- stdin is a pipe, not a path -------------------------------------------------------------


def child_env(forges: FakeForges, tmp_path: Path) -> dict[str, str]:
    env = forgectl_env(forges, {"POST_TOKEN": TOKEN})
    config = tmp_path / "child.yaml"
    config.write_text("hosts:\n  github.com:\n    token_env: POST_TOKEN\n", "utf-8")
    env["_CFG"] = str(config)
    return env


@pytest.mark.parametrize("mode", ["--dry-run", "--yes"])
def test_dash_reads_the_inherited_stdin_pipe_to_eof_in_both_modes(
    fake_forges: FakeForges, procs: ProcessRegistry, tmp_path: Path, mode: str
) -> None:
    for endpoint in (COMMENTS, REVIEW_COMMENTS, REVIEWS):
        answer_get(fake_forges, endpoint, body=[], times=None)
    fake_forges.answer(
        "gh", "user", method="GET",
        body={"login": "user-4", "id": ME, "html_url": "https://github.com/user-4"},
    )  # fmt: skip
    fake_forges.answer("gh", "graphql", method="POST", stdin_re=SHOW, body=recording(), times=None)
    fake_forges.answer("gh", COMMENTS, method="POST", status=201, body=created())
    env = child_env(fake_forges, tmp_path)
    big = ("line of a body piped in\n" * 5000) + "/not-a-command\n"
    argv = [sys.executable, "-m", "forgectl", "comment", PR, "--json", "--config", env["_CFG"],
            "--body-file", "-", "--actor", "auto:bot", mode]  # fmt: skip
    proc = procs.popen(
        argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    out, err = proc.communicate(input=big.encode(), timeout=60)
    assert proc.returncode == 0, err
    result = json.loads(out)
    schemas.validate_cli_output("comment", result)
    posts = [c for c in fake_forges.calls("gh") if "POST" in c.argv and c.argv[-1] != "graphql"]
    if mode == "--dry-run":
        assert posts == [] and result["dry_run"] is True
        assert result["neutralized_lines"] == 1
        assert result["plan"]["body_bytes"] == len((big.replace("/not", " /not")).encode())
    else:
        (post,) = posts
        assert json.loads(post.stdin or "")["body"] == big.replace("/not", " /not")


class NonBlockingStdin:
    """A stdin whose read has nothing yet: ``read`` answers None, as a non-blocking pipe does."""

    class buffer:  # noqa: N801 - mimics the ``.buffer`` attribute of a text stream
        @staticmethod
        def read(size: int) -> None:
            return None


@pytest.mark.parametrize("stream", [None, NonBlockingStdin()])
def test_a_stdin_that_cannot_be_read_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, stream: Any
) -> None:
    monkeypatch.setattr(sys, "stdin", stream)
    with pytest.raises(ForgectlError) as raised:
        post_module.read_input("-", 100)
    assert raised.value.code == "usage" and raised.value.exit_code == 2
    assert "standard input cannot be read" in raised.value.message


def test_dash_without_yes_is_confirmation_required(rig: Rig) -> None:
    done = rig.run(body_file="-", stdin="hi", yes=False)
    assert done.code == 13 and rig.posts() == []


# -- dry run, confirmation ---------------------------------------------------------------------


def test_dry_run_prints_the_plan_and_writes_nothing(rig: Rig) -> None:
    rig.comments_empty()
    done = rig.run("comment", "--dry-run", "--dedupe-key", "key-1", yes=False)
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("comment", out)
    body = f"hello from a test\n{MARKER}"
    assert out["dry_run"] is True and out["would_refuse"] is None
    golden.assert_golden("comment/github-dry-run", out, schema_id="forgectl/cli/comment.json")
    assert out["plan"] == {
        "event": None,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "body_bytes": len(body.encode()),
        "inline_comments": 0,
    }
    assert out["ids"] == [] and out["head_sha"] == HEAD
    assert_nothing_written(rig)


def test_no_yes_and_no_tty_is_exit_13(rig: Rig) -> None:
    done = rig.run(yes=False)
    assert done.code == 13 and done.error["code"] == "confirmation_required"
    assert_nothing_written(rig)


def test_a_declined_dialog_writes_nothing(rig: Rig) -> None:
    done = rig.run(yes=False, tty=HUMAN, actor="human:me", stdin="n\n")
    assert done.code == 13
    assert "Post?" in done.err
    assert_nothing_written(rig)


def test_an_accepted_dialog_posts_after_a_reread(rig: Rig) -> None:
    rig.post_ok()
    done = rig.run(yes=False, tty=HUMAN, actor="human:me", stdin="y\n")
    assert done.code == 0, done.err
    assert [c.argv[-1] for c in rig.calls()].count("graphql") == 2


# -- dedupe ----------------------------------------------------------------------------------


def test_a_hit_in_this_accounts_comment_is_already_posted_with_no_write_or_hook(
    rig: Rig, tmp_path: Path
) -> None:
    marker = tmp_path / "hook-ran"
    answer_get(rig.forges, COMMENTS, body=[item("IC_old", f"earlier\n{MARKER}\n")])
    answer_get(rig.forges, REVIEW_COMMENTS, body=[])
    answer_get(rig.forges, REVIEWS, body=[])
    done = rig.run(
        "comment",
        "--dedupe-key",
        "key-1",
        extra_config=hook_config("pre-post", f"touch {marker}"),
    )
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("comment", out)
    assert out["already_posted"] is True and out["ids"] == ["gh:ic:IC_old"]
    assert_nothing_written(rig)
    assert not marker.exists()


@pytest.mark.parametrize(
    ("endpoint", "prefix", "extra"),
    [(REVIEW_COMMENTS, "rc", {}), (REVIEWS, "r", {"state": "COMMENTED"})],
)
def test_a_hit_is_searched_in_review_comments_and_submitted_reviews(
    rig: Rig, endpoint: str, prefix: str, extra: dict[str, Any]
) -> None:
    for e in (COMMENTS, REVIEW_COMMENTS, REVIEWS):
        answer_get(rig.forges, e, body=[item("N_1", MARKER, **extra)] if e == endpoint else [])
    done = rig.run("comment", "--dedupe-key", "key-1")
    assert done.json["already_posted"] is True and done.json["ids"] == [f"gh:{prefix}:N_1"]


def test_a_pending_review_is_not_searched(rig: Rig) -> None:
    answer_get(rig.forges, COMMENTS, body=[])
    answer_get(rig.forges, REVIEW_COMMENTS, body=[])
    answer_get(rig.forges, REVIEWS, body=[item("R_1", MARKER, state="PENDING")])
    rig.post_ok()
    assert rig.run("comment", "--dedupe-key", "key-1").json["already_posted"] is False


def test_a_marker_in_another_accounts_comment_is_not_a_hit(rig: Rig) -> None:
    answer_get(rig.forges, COMMENTS, body=[item("IC_x", MARKER, OTHER_ACCOUNT)])
    answer_get(rig.forges, REVIEW_COMMENTS, body=[])
    answer_get(rig.forges, REVIEWS, body=[])
    rig.post_ok()
    done = rig.run("comment", "--dedupe-key", "key-1")
    assert done.json["already_posted"] is False and len(rig.posts()) == 1


def test_a_marker_that_is_not_the_last_line_is_not_a_hit(rig: Rig) -> None:
    body = f"{MARKER}\nand then more text"
    answer_get(rig.forges, COMMENTS, body=[item("IC_x", body)])
    answer_get(rig.forges, REVIEW_COMMENTS, body=[])
    answer_get(rig.forges, REVIEWS, body=[])
    rig.post_ok()
    assert rig.run("comment", "--dedupe-key", "key-1").json["already_posted"] is False


def test_a_marker_with_extra_text_on_its_line_is_not_a_hit(rig: Rig) -> None:
    answer_get(rig.forges, COMMENTS, body=[item("IC_x", f"x {MARKER} ")])
    answer_get(rig.forges, REVIEW_COMMENTS, body=[])
    answer_get(rig.forges, REVIEWS, body=[])
    rig.post_ok()
    assert rig.run("comment", "--dedupe-key", "key-1").json["already_posted"] is False


def test_an_incomplete_search_is_exit_12_not_a_miss(rig: Rig) -> None:
    answer_get(rig.forges, COMMENTS, status=500, body={"message": "boom"}, times=None)
    done = rig.run("comment", "--dedupe-key", "key-1")
    assert done.code == 12 and done.error["code"] == "upstream_error"
    assert_nothing_written(rig)


def test_a_search_that_never_ends_is_incomplete(rig: Rig) -> None:
    full = [item(f"IC_{i}", "x", OTHER_ACCOUNT) for i in range(100)]
    answer_get(rig.forges, COMMENTS, body=full, times=None)
    done = rig.run("comment", "--dedupe-key", "key-1")
    assert done.code == 12 and done.error["code"] == "upstream_error"
    assert_nothing_written(rig)


# -- the write proof -------------------------------------------------------------------------


def test_a_redirected_post_that_ends_in_200_is_never_reported_posted(rig: Rig) -> None:
    rig.forges.answer(
        "gh", COMMENTS, method="POST", redirect=f"/repos/{REPO}/issues/7/comments/moved"
    )
    answer_get(rig.forges, f"repos/{REPO}/issues/7/comments/moved", status=200, body=created())
    done = rig.run()
    assert done.code == 12
    assert done.error["code"] == "outcome_unknown"
    assert [r["result"] for r in rig.audit()] == ["attempted", "outcome_unknown"]


def test_a_201_that_holds_a_list_is_host_rejected(rig: Rig) -> None:
    rig.forges.answer("gh", COMMENTS, method="POST", status=201, body=[])
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "host_rejected"
    assert [r["result"] for r in rig.audit()] == ["attempted", "failed"]


def test_a_redirect_whose_get_answers_a_list_with_200_is_never_posted(rig: Rig) -> None:
    rig.forges.answer("gh", COMMENTS, method="POST", redirect=f"/repos/{REPO}/issues/7/other")
    answer_get(rig.forges, f"repos/{REPO}/issues/7/other", body=[])
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "host_rejected"
    assert [r["result"] for r in rig.audit()] == ["attempted", "failed"]


def test_a_201_without_an_integer_id_is_outcome_unknown(rig: Rig) -> None:
    body = created()
    body["id"] = "7001"
    rig.forges.answer("gh", COMMENTS, method="POST", status=201, body=body)
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "outcome_unknown"


def test_a_201_from_another_host_is_wrong_host_response_and_unknown(rig: Rig) -> None:
    body = created()
    body["html_url"] = "https://elsewhere.example/x/y/pull/7"
    rig.forges.answer("gh", COMMENTS, method="POST", status=201, body=body)
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "wrong_host_response"
    assert [r["result"] for r in rig.audit()] == ["attempted", "outcome_unknown"]


@pytest.mark.parametrize(
    ("status", "code", "result"),
    [
        (403, "permission_denied", "failed"),
        (401, "auth_required", "failed"),
        (404, "not_found", "failed"),
        (422, "host_rejected", "failed"),
        (429, "rate_limited", "failed"),
        (500, "outcome_unknown", "outcome_unknown"),
        (200, "outcome_unknown", "outcome_unknown"),
        (202, "outcome_unknown", "outcome_unknown"),
    ],
)
def test_the_write_table_maps_each_answer(rig: Rig, status: int, code: str, result: str) -> None:
    rig.forges.answer("gh", COMMENTS, method="POST", status=status, body={"message": "x"})
    done = rig.run()
    assert done.code == (10 if code == "not_found" else 12), done.out
    assert done.error["code"] == code
    assert [r["result"] for r in rig.audit()] == ["attempted", result]


def test_a_write_is_never_retried(rig: Rig) -> None:
    rig.forges.answer("gh", COMMENTS, method="POST", status=500, times=None)
    rig.run()
    assert len(rig.posts()) == 1


# -- reply -----------------------------------------------------------------------------------


def anchor_doc(typename: str, *, number: int = 7, repo: str = REPO, **fields: Any) -> dict:
    node = {
        "__typename": typename,
        "pullRequest": {"number": number},
        "repository": {"nameWithOwner": repo},
        **fields,
    }
    return {"data": {"node": node}}


def answer_anchor(rig: Rig, doc: dict[str, Any]) -> None:
    rig.forges.answer("gh", "graphql", method="POST", stdin_re=r"node\(id", body=doc, times=None)


def reply(rig: Rig, to: str, *args: str, **kw: Any) -> Result:
    return rig.run("reply", "--to", to, *args, **kw)


def test_a_reply_to_a_review_thread_joins_it_through_its_first_comment(rig: Rig) -> None:
    answer_anchor(
        rig, anchor_doc("PullRequestReviewThread", comments={"nodes": [{"databaseId": 555}]})
    )
    rig.post_ok(f"{REVIEW_COMMENTS}/555/replies", node="PRRC_new0001")
    done = reply(rig, "gh:rt:PRRT_thread01")
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("reply", out)
    assert out["threaded"] is True
    golden.assert_golden("reply/github-threaded", out, schema_id="forgectl/cli/reply.json")
    assert out["ids"] == ["gh:rc:PRRC_new0001"] and out["thread_ids"] == ["gh:rt:PRRT_thread01"]
    assert rig.sent() == [{"body": "hello from a test\n"}]
    first, second = rig.audit()
    assert first["action"] == "reply" and first["inputs"]["reply_to"] == "gh:rt:PRRT_thread01"


def test_a_reply_to_a_reply_goes_to_the_root(rig: Rig) -> None:
    answer_anchor(
        rig,
        anchor_doc("PullRequestReviewComment", databaseId=777, replyTo={"databaseId": 555}),
    )
    rig.post_ok(f"{REVIEW_COMMENTS}/555/replies", node="PRRC_new0001")
    assert reply(rig, "gh:rc:PRRC_child001").code == 0
    assert rig.posts()[0].argv[-1].endswith("/comments/555/replies")


def test_a_reply_to_a_root_review_comment_uses_its_own_id(rig: Rig) -> None:
    answer_anchor(rig, anchor_doc("PullRequestReviewComment", databaseId=555, replyTo=None))
    rig.post_ok(f"{REVIEW_COMMENTS}/555/replies", node="PRRC_new0001")
    assert reply(rig, "gh:rc:PRRC_root0001").code == 0


@pytest.mark.parametrize(
    ("to", "typename"),
    [("gh:ic:IC_old00001", "IssueComment"), ("gh:r:PRR_rev00001", "PullRequestReview")],
)
def test_a_reply_to_a_general_comment_or_review_posts_a_general_comment(
    rig: Rig, to: str, typename: str
) -> None:
    answer_anchor(rig, anchor_doc(typename))
    rig.post_ok()
    done = reply(rig, to)
    assert done.code == 0, done.err
    assert done.json["threaded"] is False
    assert done.json["ids"] == [f"gh:ic:{NEW_NODE}"] and done.json["thread_ids"] == []
    assert rig.posts()[0].argv[-1] == COMMENTS


@pytest.mark.parametrize(
    "doc",
    [
        {"data": {"node": None}},
        anchor_doc("PullRequestReviewThread", number=8, comments={"nodes": [{"databaseId": 1}]}),
        anchor_doc("PullRequestReviewThread", repo="user-7/other", comments={"nodes": []}),
        anchor_doc("Issue"),
        anchor_doc("IssueComment", pullRequest=None),
    ],
)
def test_an_id_that_is_absent_or_from_another_pr_is_exit_10_before_any_write(
    rig: Rig, doc: dict[str, Any]
) -> None:
    doc = json.loads(json.dumps(doc))
    if doc["data"]["node"] is not None and "pullRequest" not in doc["data"]["node"]:
        pass
    answer_anchor(rig, doc)
    done = reply(rig, "gh:rt:PRRT_thread01")
    assert done.code == 10 and done.error["code"] == "not_found"
    assert_nothing_written(rig)


def test_a_typename_that_does_not_match_the_prefix_is_exit_10(rig: Rig) -> None:
    answer_anchor(rig, anchor_doc("IssueComment"))
    done = reply(rig, "gh:rc:PRRC_x0000001")
    assert done.code == 10
    assert_nothing_written(rig)


def test_a_gitlab_id_on_a_github_pr_is_exit_10(rig: Rig) -> None:
    done = reply(rig, "gl:n:5")
    assert done.code == 10 and done.error["code"] == "not_found"
    assert_nothing_written(rig)


def test_the_lookup_runs_before_dry_run_and_confirmation(rig: Rig) -> None:
    answer_anchor(rig, {"data": {"node": None}})
    assert reply(rig, "gh:rt:PRRT_thread01", "--dry-run", yes=False).code == 10
    assert reply(rig, "gh:rt:PRRT_thread01", yes=False).code == 10


def test_a_dry_run_reply_computes_threaded(rig: Rig) -> None:
    answer_anchor(rig, anchor_doc("IssueComment"))
    done = reply(rig, "gh:ic:IC_old00001", "--dry-run", yes=False)
    assert done.code == 0, done.err
    schemas.validate_cli_output("reply", done.json)
    assert done.json["threaded"] is False and done.json["dry_run"] is True
    assert rig.posts() == []


# -- hooks -----------------------------------------------------------------------------------


def test_the_pre_post_hook_gets_the_schema_payload_without_a_body(rig: Rig, tmp_path: Path) -> None:
    out = tmp_path / "payload.json"
    rig.post_ok()
    done = rig.run(extra_config=hook_config("pre-post", f"cat > {out}"))
    assert done.code == 0, done.err
    payload = json.loads(out.read_text())
    schemas.validate_hook_payload("pre-post", payload)
    assert payload["action"] == "comment" and payload["head_sha"] == HEAD
    assert payload["body_bytes"] == len(b"hello from a test\n")
    assert "hello from a test" not in out.read_text()
    assert [c.argv[-1] for c in rig.calls()].count("graphql") == 2  # a hook ran: re-read


def test_a_pre_post_veto_stops_everything(rig: Rig) -> None:
    done = rig.run(extra_config=hook_config("pre-post", "echo no >&2; exit 3"))
    assert done.code == 11 and done.error["code"] == "vetoed"
    assert_nothing_written(rig)


def test_a_pre_post_escalation_is_exit_13_and_an_override_is_audited(rig: Rig) -> None:
    done = rig.run(extra_config=hook_config("pre-post", "exit 13"))
    assert done.code == 13 and rig.posts() == []
    rig.post_ok()
    done = rig.run("comment", "--override-escalation", actor="human:me",
                   extra_config=hook_config("pre-post", "exit 13"))  # fmt: skip
    assert done.code == 0, done.err
    assert rig.audit()[0]["override"] == ["override_escalation"]


def test_post_post_fires_with_the_result_and_ids(rig: Rig, tmp_path: Path) -> None:
    out = tmp_path / "post.json"
    rig.post_ok()
    done = rig.run(extra_config=hook_config("post-post", f"cat > {out}"))
    assert done.code == 0, done.err
    payload = json.loads(out.read_text())
    schemas.validate_hook_payload("post-post", payload)
    assert payload["result"] == "posted" and payload["ids"] == [f"gh:ic:{NEW_NODE}"]


def test_post_post_fires_with_the_failed_result(rig: Rig, tmp_path: Path) -> None:
    out = tmp_path / "post.json"
    rig.forges.answer("gh", COMMENTS, method="POST", status=422, body={"message": "bad"})
    done = rig.run(extra_config=hook_config("post-post", f"cat > {out}"))
    assert done.code == 12
    payload = json.loads(out.read_text())
    schemas.validate_hook_payload("post-post", payload)
    assert payload["result"] == "failed" and payload["ids"] == []


def test_a_failing_post_post_hook_never_changes_the_result(rig: Rig) -> None:
    rig.post_ok()
    done = rig.run(extra_config=hook_config("post-post", "exit 9"))
    assert done.code == 0 and "warning" in done.err


# -- signals (a real process) ----------------------------------------------------------------


def _wait_for_calls(forges: FakeForges, count: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and len(forges.calls("gh")) < count:
        time.sleep(0.05)
    time.sleep(0.4)


@pytest.fixture
def signal_env(fake_forges: FakeForges, tmp_path: Path) -> Iterator[dict[str, str]]:
    cfg = tmp_path / "config.yaml"
    marker = tmp_path / "post-ran"
    body = tmp_path / "body.md"
    body.write_text("hi\n", "utf-8")
    cfg.write_text(
        "hosts:\n  github.com:\n    token_env: POST_TOKEN\n"
        f"hooks:\n  post-post:\n    - cmd: {json.dumps(f'touch {marker}')}\n",
        encoding="utf-8",
    )
    env = forgectl_env(fake_forges, {"POST_TOKEN": TOKEN})
    env.update(_CFG=str(cfg), _MARK=str(marker), _BODY=str(body))
    fake_forges.answer(
        "gh", "user", method="GET",
        body={"login": "user-4", "id": ME, "html_url": "https://github.com/user-4"},
    )  # fmt: skip
    yield env


def signal_argv(env: dict[str, str]) -> list[str]:
    return [sys.executable, "-m", "forgectl", "comment", PR, "--json", "--config", env["_CFG"],
            "--body-file", env["_BODY"], "--actor", "auto:bot", "--yes"]  # fmt: skip


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_during_the_write_is_outcome_unknown_and_runs_no_post_hook(
    fake_forges: FakeForges, procs: ProcessRegistry, signal_env: dict[str, str], signum: int
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", stdin_re=SHOW, body=recording(), times=None)
    fake_forges.answer("gh", COMMENTS, method="POST", hang=60)
    proc = procs.popen(signal_argv(signal_env), env=signal_env, stdout=-1, stderr=-1, text=True)
    _wait_for_calls(fake_forges, 3)
    proc.send_signal(signum)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 12, out
    assert json.loads(out)["error"]["code"] == "outcome_unknown"
    audit_file = Path(signal_env["XDG_STATE_HOME"]) / "forgectl" / "audit.jsonl"
    records = [json.loads(x) for x in audit_file.read_text().splitlines()]
    assert [r["result"] for r in records] == ["attempted", "outcome_unknown"]
    assert not Path(signal_env["_MARK"]).exists()


def test_a_signal_before_the_write_is_128_plus_n_with_no_audit(
    fake_forges: FakeForges, procs: ProcessRegistry, signal_env: dict[str, str]
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", stdin_re=SHOW, hang=60)
    proc = procs.popen(signal_argv(signal_env), env=signal_env, stdout=-1, stderr=-1, text=True)
    _wait_for_calls(fake_forges, 2)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=30)
    assert proc.returncode == 143
    audit_file = Path(signal_env["XDG_STATE_HOME"]) / "forgectl" / "audit.jsonl"
    assert not audit_file.exists() or audit_file.read_text() == ""
