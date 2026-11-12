"""`forgectl comment` and `reply` on GitLab (#24): the note and discussion writes, the quick-action
backstop, the lookup of `--to` within the MR, dedupe over notes.

The real CLI runs in process against the fake `glab`; every answer is hand-built from GitLab's
documented REST shapes and is UNVERIFIED against a real host until the live suite (#34).
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from forgectl.cli import main
from forgectl.humanactions import Tty

from .gitlab_support import TOKEN, mr_path, recording
from .harness import golden, schemas
from .harness.fixtures import forgectl_env
from .harness.forges import Call, FakeForges
from .harness.safety import assert_call_safe
from .test_gitlab_show_process import script

PR = "gitlab.com/user-9/proj#7"
MR = "projects/101/merge_requests/7"
NOTES = f"{MR}/notes"
DISCUSSIONS = f"{MR}/discussions"
DISC = "d" * 40
OTHER_DISC = "e" * 40
ME = 31
MARKER = "<!-- forgectl:dedupe:key-1 -->"
NO_TTY = Tty(stdin=False, stderr=False)


def get(forges: FakeForges, endpoint: str, body: Any, **kw: Any) -> None:
    forges.answer(
        "glab", endpoint_re=rf"^{re.escape(endpoint)}(\?|$)", method="GET", body=body, **kw
    )


def note(note_id: int, body: str, author: int = ME, **kw: Any) -> dict[str, Any]:
    return {"id": note_id, "body": body, "author": {"id": author}, "system": False, **kw}


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
        return dict(self.json["error"])


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
        for key, value in forgectl_env(forges, {"GL_TOKEN": TOKEN}).items():
            monkeypatch.setenv(key, value)
        script(forges, "gitlab.com", "")
        get(
            forges, "user", {"id": ME, "username": "user-4", "web_url": "https://gitlab.com/user-4"}
        )
        self.body_path = tmp_path / "body.md"
        self.body_path.write_text("hello from a test\n", "utf-8")
        self.config = tmp_path / "config.yaml"
        self.config.write_text("hosts:\n  gitlab.com:\n    token_env: GL_TOKEN\n", "utf-8")

    def post(self, endpoint: str, status: int = 201, body: Any = None, **kw: Any) -> None:
        self.forges.answer("glab", endpoint, method="POST", status=status, body=body, **kw)

    def run(
        self, command: str = "comment", *args: str, yes: bool = True, dry: bool = False
    ) -> Result:
        argv = [command, PR, "--json", "--config", str(self.config)]
        argv += ["--body-file", str(self.body_path), "--actor", "auto:bot"]
        if yes:
            argv += ["--yes"]
        if dry:
            argv += ["--dry-run"]
        argv += list(args)
        self.monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        code = main(argv, tty=NO_TTY)
        captured = self.capsys.readouterr()
        return Result(code, captured.out, captured.err)

    def poster_mr_get(self, status: int = 200, body: Any = None) -> None:
        """The MR's second GET (the poster's, after the PR read's) answers differently."""
        self.forges.answer(
            "glab", mr_path(), method="GET", nth=2, status=status, body=body or {"message": "x"}
        )
        rules = self.forges._rules
        rules.insert(0, rules.pop())  # rules are tried in order: this one goes first
        self.forges._write_script()

    def calls(self) -> list[Call]:
        return self.forges.calls("glab")

    def posts(self) -> list[Call]:
        return [c for c in self.calls() if "POST" in c.argv]

    def audit(self) -> list[dict[str, Any]]:
        path = Path(os.environ["XDG_STATE_HOME"]) / "forgectl" / "audit.jsonl"
        if not path.exists():
            return []
        records = [json.loads(line) for line in path.read_text().splitlines() if line]
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


def stored(text: str = "hello from a test\n", **kw: Any) -> dict[str, Any]:
    return {"id": 8001, "body": text, "author": {"id": ME}, "commands_changes": {}, **kw}


def test_a_comment_posts_a_note_to_the_numeric_project_id(rig: Rig) -> None:
    rig.post(NOTES, body=stored())
    done = rig.run()
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("comment", out)
    golden.assert_golden("comment/gitlab-posted", out, schema_id="forgectl/cli/comment.json")
    assert out["ids"] == ["gl:n:8001"] and out["thread_ids"] == [] and out["review_id"] is None
    (post,) = rig.posts()
    assert post.argv[-1] == NOTES
    assert json.loads(post.stdin or "") == {"body": "hello from a test\n"}
    for call in rig.calls():
        assert_call_safe(call, host="gitlab.com", mode="token")
    assert [r["result"] for r in rig.audit()] == ["attempted", "posted"]


def test_the_note_is_neutralized_and_the_marker_added(rig: Rig) -> None:
    rig.body_path.write_text("/merge\nplain", "utf-8")
    sent = " /merge\nplain\n" + MARKER
    get(rig.forges, NOTES, [])
    rig.post(NOTES, body=stored(sent))
    done = rig.run("comment", "--dedupe-key", "key-1")
    assert done.code == 0, done.err
    assert done.json["neutralized_lines"] == 1
    assert json.loads(rig.posts()[0].stdin or "") == {"body": sent}


def test_a_gitlab_body_may_be_larger_than_githubs(rig: Rig) -> None:
    rig.body_path.write_text("a" * 300000, "utf-8")
    rig.post(NOTES, body=stored("a" * 300000))
    assert rig.run().code == 0
    rig.body_path.write_text("a" * (1024 * 1024 + 1), "utf-8")
    done = rig.run()
    assert done.code == 2 and done.error["details"] == {"index": None, "limit": 1024 * 1024}


# -- the backstop ------------------------------------------------------------------------------


def test_a_202_is_quick_action_ran_with_a_failed_audit_result_of_that_name(rig: Rig) -> None:
    rig.post(NOTES, status=202, body={"commands_changes": {"merge": True}})
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "quick_action_ran"
    assert done.error["details"] == {"ids": []}
    assert [r["result"] for r in rig.audit()] == ["attempted", "quick_action_ran"]


def test_a_non_empty_commands_changes_is_quick_action_ran_with_the_id(rig: Rig) -> None:
    rig.post(NOTES, body=stored(commands_changes={"labels": ["x"]}))
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "quick_action_ran"
    assert done.error["details"] == {"ids": ["gl:n:8001"]}
    assert rig.audit()[-1]["ids"] == ["gl:n:8001"]


@pytest.mark.parametrize("returned", ["something else", None, 5])
def test_a_stored_body_that_differs_is_quick_action_ran(rig: Rig, returned: Any) -> None:
    rig.post(NOTES, body=stored(returned))
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "quick_action_ran"


def test_a_stored_body_differing_only_in_trailing_whitespace_is_fine(rig: Rig) -> None:
    rig.body_path.write_text("a  \nb\n\n", "utf-8")
    rig.post(NOTES, body=stored("a\nb"))
    assert rig.run().code == 0


# -- the write table ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "result"),
    [
        (403, "permission_denied", "failed"),
        (401, "auth_required", "failed"),
        (400, "host_rejected", "failed"),
        (429, "rate_limited", "failed"),
        (500, "outcome_unknown", "outcome_unknown"),
        (200, "outcome_unknown", "outcome_unknown"),
    ],
)
def test_each_answer_maps_to_its_code_and_audit_result(
    rig: Rig, status: int, code: str, result: str
) -> None:
    rig.post(NOTES, status=status, body={"message": "x"})
    done = rig.run()
    assert done.code == 12 and done.error["code"] == code
    assert [r["result"] for r in rig.audit()] == ["attempted", result]


def test_a_201_without_an_id_is_outcome_unknown(rig: Rig) -> None:
    rig.post(NOTES, body={"body": "hello from a test\n"})
    assert rig.run().error["code"] == "outcome_unknown"


def test_a_redirected_note_post_ending_in_a_200_list_is_never_posted(rig: Rig) -> None:
    rig.forges.answer("glab", NOTES, method="POST", redirect="/projects/101/other")
    get(rig.forges, "projects/101/other", [])
    done = rig.run()
    assert done.code == 12 and done.error["code"] in ("host_rejected", "outcome_unknown")


@pytest.mark.parametrize(
    "extra",
    [
        {"noteable_type": "Issue", "noteable_iid": 7},
        {"noteable_type": "MergeRequest", "noteable_iid": 8},
        {"noteable_iid": 8},
        {"noteable_type": "Snippet"},
    ],
)
def test_a_note_answer_for_another_noteable_is_wrong_host_response_and_unknown(
    rig: Rig, extra: dict[str, Any]
) -> None:
    rig.post(NOTES, body=stored(**extra))
    done = rig.run()
    assert done.code == 12 and done.error["code"] == "wrong_host_response"
    assert [r["result"] for r in rig.audit()] == ["attempted", "outcome_unknown"]


@pytest.mark.parametrize(
    "extra",
    [{}, {"noteable_type": "MergeRequest", "noteable_iid": 7}, {"noteable_type": "MergeRequest"}],
)
def test_a_note_answer_for_this_mr_or_naming_no_noteable_is_accepted(
    rig: Rig, extra: dict[str, Any]
) -> None:
    rig.post(NOTES, body=stored(**extra))  # live-unverified: #34 (the fields are documented)
    assert rig.run().code == 0


# -- the project id is read before anything is audited ---------------------------------------


def mr_with(**changes: Any) -> dict[str, Any]:
    data = recording("mr")
    data["web_url"] = "https://gitlab.com/user-9/proj/-/merge_requests/7"
    data.update(changes)
    return data


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (403, {"message": "no"}, "permission_denied"),
        (200, mr_with(web_url="https://other.example.test/user-9/proj/-/merge_requests/7"), None),
        (200, mr_with(iid=8), "upstream_error"),
        (200, mr_with(project_id="x"), "upstream_error"),
    ],
)
def test_a_failing_project_id_read_leaves_no_trace_and_sends_nothing(
    rig: Rig, status: int, body: Any, code: str | None
) -> None:
    rig.config.write_text(
        rig.config.read_text() + 'hooks:\n  post-post:\n    - cmd: "touch hook-ran"\n', "utf-8"
    )
    rig.poster_mr_get(status, body)
    done = rig.run()
    assert done.code == 12, done.err
    if code is not None:
        assert done.error["code"] == code
    assert rig.posts() == [] and rig.audit() == []
    assert not (rig.tmp / "hook-ran").exists() and not Path("hook-ran").exists()


def test_a_dry_run_comment_reads_the_project_id_too(rig: Rig) -> None:
    rig.poster_mr_get(403, {"message": "no"})
    done = rig.run(yes=False, dry=True)
    assert done.code != 0 and done.error["code"] == "permission_denied"


# -- reply -------------------------------------------------------------------------------------


def reply(rig: Rig, to: str, *args: str, **kw: Any) -> Result:
    return rig.run("reply", "--to", to, *args, **kw)


def test_a_reply_to_a_discussion_posts_into_it(rig: Rig) -> None:
    get(rig.forges, f"{DISCUSSIONS}/{DISC}", {"id": DISC, "notes": [note(5, "x")]})
    rig.post(f"{DISCUSSIONS}/{DISC}/notes", body=stored())
    done = reply(rig, f"gl:d:{DISC}")
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("reply", out)
    assert out["threaded"] is True
    golden.assert_golden("reply/gitlab-threaded", out, schema_id="forgectl/cli/reply.json")
    assert out["ids"] == ["gl:n:8001"] and out["thread_ids"] == [f"gl:d:{DISC}"]
    assert rig.posts()[0].argv[-1] == f"{DISCUSSIONS}/{DISC}/notes"
    assert rig.audit()[0]["inputs"]["reply_to"] == f"gl:d:{DISC}"


def test_a_reply_to_a_note_finds_its_discussion_through_the_mr(rig: Rig) -> None:
    pages = [{"id": OTHER_DISC, "notes": [note(4, "a")]}, {"id": DISC, "notes": [note(5, "b")]}]
    get(rig.forges, DISCUSSIONS, pages)
    rig.post(f"{DISCUSSIONS}/{DISC}/notes", body=stored())
    done = reply(rig, "gl:n:5")
    assert done.code == 0, done.err
    assert done.json["thread_ids"] == [f"gl:d:{DISC}"]


def test_a_note_that_is_not_in_this_mr_is_exit_10_before_any_write(rig: Rig) -> None:
    get(rig.forges, DISCUSSIONS, [{"id": DISC, "notes": [note(5, "b")]}])
    done = reply(rig, "gl:n:6")
    assert done.code == 10 and done.error["code"] == "not_found"
    assert rig.posts() == [] and rig.audit() == []


def test_a_system_note_cannot_be_replied_to(rig: Rig) -> None:
    get(rig.forges, DISCUSSIONS, [{"id": DISC, "notes": [note(5, "b", system=True)]}])
    assert reply(rig, "gl:n:5").code == 10


def test_a_discussion_of_another_mr_is_exit_10(rig: Rig) -> None:
    get(rig.forges, f"{DISCUSSIONS}/{DISC}", {"message": "404 Not Found"}, status=404)
    done = reply(rig, f"gl:d:{DISC}")
    assert done.code == 10 and done.error["code"] == "not_found"
    assert rig.posts() == [] and rig.audit() == []


def test_a_discussion_answer_with_another_id_is_refused(rig: Rig) -> None:
    get(rig.forges, f"{DISCUSSIONS}/{DISC}", {"id": OTHER_DISC, "notes": []})
    done = reply(rig, f"gl:d:{DISC}")
    assert done.code == 12 and rig.posts() == []


def test_a_github_id_on_a_gitlab_mr_is_exit_10(rig: Rig) -> None:
    done = reply(rig, "gh:rt:PRRT_x0000001")
    assert done.code == 10 and rig.posts() == []


def test_a_reply_dry_run_looks_up_and_writes_nothing(rig: Rig) -> None:
    get(rig.forges, f"{DISCUSSIONS}/{DISC}", {"id": DISC, "notes": [note(5, "x")]})
    done = reply(rig, f"gl:d:{DISC}", yes=False, dry=True)
    assert done.code == 0, done.err
    schemas.validate_cli_output("reply", done.json)
    assert done.json["threaded"] is True and rig.posts() == [] and rig.audit() == []


def test_a_reply_backstop_applies_to_discussion_notes(rig: Rig) -> None:
    get(rig.forges, f"{DISCUSSIONS}/{DISC}", {"id": DISC, "notes": [note(5, "x")]})
    rig.post(f"{DISCUSSIONS}/{DISC}/notes", status=202, body={})
    done = reply(rig, f"gl:d:{DISC}")
    assert done.code == 12 and done.error["code"] == "quick_action_ran"
    assert rig.audit()[-1]["action"] == "reply"


# -- dedupe ------------------------------------------------------------------------------------


def test_a_hit_among_this_accounts_notes_is_already_posted(rig: Rig) -> None:
    get(rig.forges, NOTES, [note(1, "x"), note(2, f"earlier\n{MARKER}", ME)])
    done = rig.run("comment", "--dedupe-key", "key-1")
    assert done.code == 0, done.err
    assert done.json["already_posted"] is True and done.json["ids"] == ["gl:n:2"]
    schemas.validate_cli_output("comment", done.json)
    assert rig.posts() == [] and rig.audit() == []


def test_another_accounts_marker_and_a_non_last_line_marker_are_not_hits(rig: Rig) -> None:
    get(rig.forges, NOTES, [note(1, MARKER, 99), note(2, f"{MARKER}\nmore", ME)])
    rig.post(NOTES, body=stored(f"hello from a test\n{MARKER}"))
    done = rig.run("comment", "--dedupe-key", "key-1")
    assert done.code == 0 and done.json["already_posted"] is False


def test_the_search_pages_through_every_note(rig: Rig) -> None:
    rig.forges.answer(
        "glab",
        endpoint_re=rf"^{re.escape(NOTES)}\?.*page=1(&|$)",
        method="GET",
        body=[note(i, "x", 99) for i in range(1, 101)],
        headers={"X-Next-Page": "2"},
    )
    rig.forges.answer(
        "glab",
        endpoint_re=rf"^{re.escape(NOTES)}\?.*page=2(&|$)",
        method="GET",
        body=[note(200, MARKER, ME)],
        headers={"X-Next-Page": ""},
    )
    done = rig.run("comment", "--dedupe-key", "key-1")
    assert done.json["already_posted"] is True and done.json["ids"] == ["gl:n:200"]


def test_an_incomplete_search_exits_12(rig: Rig) -> None:
    get(rig.forges, NOTES, {"message": "boom"}, status=500, times=None)
    done = rig.run("comment", "--dedupe-key", "key-1")
    assert done.code == 12 and done.error["code"] == "upstream_error"
    assert rig.posts() == [] and rig.audit() == []
