"""`forgectl review` on GitLab (#27): draft notes, one `bulk_publish`, the read-back through the
discussions, the state step, and every failure path between them.

The real CLI runs in process against the fake `glab`, which logs every call. Nothing reaches a
host. The answers are hand-built from GitLab's documented REST shapes; every assumed field name is
UNVERIFIED until the live suite (#34) re-checks it.
"""

from __future__ import annotations

import functools
import io
import json
import re
import sys
from typing import Any

import pytest

from forgectl import review as review_module
from forgectl.backends.gitlab_review import Draft, matches_sent

from .gitlab_support import HEAD, mr_path
from .harness import schemas
from .harness.forges import Call
from .harness.safety import assert_call_safe
from .test_gitlab_diff import entry
from .test_post_gitlab import (
    DISCUSSIONS,
    ME,
    MR,
    NO_TTY,
    PR,
    Result,
    Rig,
    get,
    note,
    rig,  # noqa: F401  (the fixture)
)

DIFFS = f"{MR}/diffs"
DRAFTS = f"{MR}/draft_notes"
PUBLISH = f"{DRAFTS}/bulk_publish"
APPROVE = f"{MR}/approve"
NOTES = f"{MR}/notes"
D1, D2 = "1" * 40, "2" * 40
MARKER = "<!-- forgectl:dedupe:key-1 -->"
# old: 1 ctx, 2 removed, 3 ctx   new: 1 ctx, 2 added, 3 ctx
PATCH = "@@ -1,3 +1,3 @@\n a\n-b\n+c\n d\n"
TOP = 500  # the highest note id before the review


def inline(**kw: Any) -> dict[str, Any]:
    return {"path": "src/app.py", "line": 2, "body": "use c here", **kw}


def draft(ident: int, **kw: Any) -> dict[str, Any]:
    return {"id": ident, "author_id": ME, "note": "x", "position": None, **kw}


def disc(disc_id: str, *notes: dict[str, Any]) -> dict[str, Any]:
    return {"id": disc_id, "individual_note": False, "notes": list(notes)}


def mine(note_id: int, body: str, **kw: Any) -> dict[str, Any]:
    return note(note_id, body, **kw)


def front(fn: Any) -> Any:
    """A rig helper whose rules go ahead of the ones already scripted (the later call wins; the
    rules of one call keep their order), so a test can start from ``happy`` and override."""

    @functools.wraps(fn)
    def wrapper(self: Gl, *args: Any, **kwargs: Any) -> None:
        rules = self.forges._rules
        before = len(rules)
        fn(self, *args, **kwargs)
        moved = rules[before:]
        del rules[before:]
        rules[0:0] = moved
        self.forges._write_script()

    return wrapper


class Gl:
    """The GitLab rig, driven through ``review --file``."""

    def __init__(self, base: Rig) -> None:
        self.base = base
        self.forges = base.forges
        self.file = base.tmp / "review.json"
        self.diff()
        self.notes([note(TOP, "earlier", author=77)])

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base, name)

    def input(self, doc: Any) -> None:
        self.file.write_text(doc if isinstance(doc, str) else json.dumps(doc), "utf-8")

    @front
    def diff(self, patch: str = PATCH) -> None:
        get(self.forges, DIFFS, [entry("src/app.py", patch)], headers={"X-Next-Page": ""})

    @front
    def notes(self, items: list[dict[str, Any]]) -> None:
        get(self.forges, NOTES, items, headers={"X-Next-Page": ""})

    @front
    def discussions(self, items: list[dict[str, Any]]) -> None:
        get(self.forges, DISCUSSIONS, items, headers={"X-Next-Page": ""})

    @front
    def drafts(self, *lists: list[dict[str, Any]]) -> None:
        """Each GET of the drafts answers the next list (the last one repeats)."""
        for n, items in enumerate(lists):
            last = n == len(lists) - 1
            get(
                self.forges,
                DRAFTS,
                items,
                headers={"X-Next-Page": ""},
                times=None if last else 1,
            )

    @front
    def created(self, *ids: int, status: int = 201) -> None:
        for ident in ids:
            self.forges.answer(
                "glab",
                DRAFTS,
                method="POST",
                status=status,
                body={"id": ident, "author_id": ME},
                times=1,
            )

    @front
    def create_fails(self, status: int, body: Any) -> None:
        self.forges.answer("glab", DRAFTS, method="POST", status=status, body=body)

    @front
    def publish(self, status: int = 204, body: Any = None) -> None:
        self.forges.answer(
            "glab",
            PUBLISH,
            method="POST",
            status=status,
            body_text="" if body is None else None,
            body=body,
        )

    @front
    def deleted(self, ident: int, status: int = 204) -> None:
        self.forges.answer(
            "glab", f"{DRAFTS}/{ident}", method="DELETE", status=status, body_text=""
        )

    @front
    def approve_ok(self) -> None:
        self.forges.answer("glab", APPROVE, method="POST", status=201, body={"id": 90001, "iid": 7})

    @front
    def request_changes_ok(self) -> None:
        self.forges.answer(
            "glab",
            "graphql",
            method="POST",
            body={
                "data": {"mergeRequestRequestChanges": {"mergeRequest": {"iid": "7"}, "errors": []}}
            },
        )

    @front
    def version(self, text: str) -> None:
        self.forges.answer("glab", "version", body={"version": text, "revision": "x"})

    def run(
        self,
        *args: str,
        expected_head: str | None = HEAD,
        expected_base: str | None = "main",
        yes: bool = True,
    ) -> Result:
        argv = ["review", PR, "--json", "--config", str(self.base.config), "--file", str(self.file)]
        if expected_head:
            argv += ["--expected-head", expected_head]
        if expected_base:
            argv += ["--expected-base", expected_base]
        argv += ["--actor", "auto:bot"]
        if yes:
            argv += ["--yes"]
        argv += list(args)
        self.base.monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        code = review_main(argv)
        captured = self.base.capsys.readouterr()
        return Result(code, captured.out, captured.err)

    # what the fake saw ------------------------------------------------------------------

    def seen(self) -> list[tuple[str, str]]:
        """Every call as (method, endpoint without its query)."""
        out: list[tuple[str, str]] = []
        for call in self.base.calls():
            method = "POST" if "POST" in call.argv else "DELETE" if "DELETE" in call.argv else "GET"
            out.append((method, call.argv[-1].partition("?")[0]))
        return out

    def writes(self) -> list[Call]:
        return [c for c in self.base.calls() if "GET" not in _method(c)]

    def endpoints(self, method: str) -> list[str]:
        return [e for m, e in self.seen() if m == method]


def _method(call: Call) -> str:
    i = call.argv.index("-X")
    return call.argv[i + 1]


def review_main(argv: list[str]) -> int:
    from forgectl.cli import main

    return main(argv, tty=NO_TTY)


@pytest.fixture
def gl(rig: Rig) -> Gl:  # noqa: F811
    return Gl(rig)


def mine_published(ids: list[tuple[int, str, str]]) -> list[dict[str, Any]]:
    """Discussions holding this account's notes: (note id, discussion, body)."""
    return [disc(d, mine(i, body)) for i, d, body in ids]


def happy(gl: Gl, comment_body: str = "use c here", body: str = "overall") -> None:
    gl.input({"event": "comment", "body": "overall", "comments": [inline(body=comment_body)]})
    gl.drafts([], [draft(11), draft(12)])
    gl.created(11, 12)
    gl.publish()
    gl.discussions(mine_published([(601, D1, comment_body), (602, D2, body)]))


# -- the happy path -----------------------------------------------------------------------------


def test_a_review_posts_drafts_publishes_once_and_reads_back(gl: Gl) -> None:
    happy(gl)
    done = gl.run()
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("review", out)
    assert out["ids"] == ["gl:n:601", "gl:n:602"]
    assert out["thread_ids"] == [f"gl:d:{D1}", f"gl:d:{D2}"]
    assert out["review_id"] is None and out["already_posted"] is False
    assert out["warnings"] == [] and out["follow_up_done"] is False
    drafts = [c for c in gl.writes() if c.argv[-1].endswith("draft_notes")]
    first, second = (json.loads(c.stdin or "") for c in drafts)
    assert first["note"] == "use c here"
    position = first["position"]
    assert position["position_type"] == "text" and position["new_path"] == "src/app.py"
    assert position["new_line"] == 2 and position["old_line"] is None
    assert position["head_sha"] == HEAD and position["line_code"]
    assert second == {"note": "overall"}  # the body draft has no position
    assert gl.endpoints("POST").count(PUBLISH) == 1
    for call in gl.calls():
        assert_call_safe(call, host="gitlab.com", mode="token")
    assert [r["result"] for r in gl.audit()] == ["attempted", "posted"]
    assert gl.audit()[-1]["ids"] == ["gl:n:601", "gl:n:602"]


def test_the_order_is_list_create_relist_notes_publish_discussions(gl: Gl) -> None:
    happy(gl)
    assert gl.run().code == 0
    seen = [(m, e) for m, e in gl.seen() if "draft" in e or e.endswith(("notes", "discussions"))]
    gets = [i for i, c in enumerate(seen) if c == ("GET", DRAFTS)]
    creates = [i for i, c in enumerate(seen) if c == ("POST", DRAFTS)]
    assert len(gets) == 2 and len(creates) == 2
    assert gets[0] < creates[0] < creates[1] < gets[1]
    assert seen.index(("POST", PUBLISH)) > seen.index(("GET", NOTES))
    assert seen.index(("GET", DISCUSSIONS)) > seen.index(("POST", PUBLISH))


def test_a_review_with_no_body_posts_only_the_inline_drafts(gl: Gl) -> None:
    gl.input({"event": "comment", "comments": [inline()]})
    gl.drafts([], [draft(11)])
    gl.created(11)
    gl.publish()
    gl.discussions(mine_published([(601, D1, "use c here")]))
    done = gl.run()
    assert done.code == 0, done.err
    assert done.json["ids"] == ["gl:n:601"]
    assert len([c for c in gl.writes() if c.argv[-1].endswith("draft_notes")]) == 1


def test_a_range_goes_out_as_a_line_range(gl: Gl) -> None:
    gl.diff("@@ -1,3 +1,4 @@\n a\n-b\n+c\n+e\n d\n")
    gl.input({"event": "comment", "comments": [inline(start_line=2, line=3)]})
    gl.drafts([], [draft(11)])
    gl.created(11)
    gl.publish()
    gl.discussions(mine_published([(601, D1, "use c here")]))
    assert gl.run().code == 0
    sent = json.loads(
        next(c for c in gl.writes() if c.argv[-1].endswith("draft_notes")).stdin or ""
    )
    assert set(sent["position"]["line_range"]) == {"start", "end"}
    assert sent["position"]["line_range"]["end"]["new_line"] == 3


def test_the_marker_goes_on_the_body_or_on_the_last_inline_comment() -> None:
    raw = review_module.parse_input(
        json.dumps(
            {"event": "comment", "body": "b", "comments": [inline(), inline(body="z")]}
        ).encode()
    )
    with_body = review_module.prepare(raw, "key-1", "gitlab")
    assert with_body.body == "b\n" + MARKER
    assert [c.body for c in with_body.comments] == ["use c here", "z"]
    bodiless = review_module.RawReview("comment", None, raw.comments)
    last = review_module.prepare(bodiless, "key-1", "gitlab")
    assert last.body is None
    assert [c.body for c in last.comments] == ["use c here", "z\n" + MARKER]
    github = review_module.prepare(bodiless, "key-1", "github")
    assert github.body == MARKER and [c.body for c in github.comments] == ["use c here", "z"]


def test_gitlab_bodies_may_be_larger_than_githubs_review_body() -> None:
    raw = review_module.RawReview("comment", "a" * 300000, ())
    assert review_module.prepare(raw, None, "gitlab").nbytes == 300000
    with pytest.raises(Exception) as caught:
        review_module.prepare(raw, None, "github")
    assert getattr(caught.value, "code", "") == "body_too_large"


# -- before anything is written -----------------------------------------------------------------


def test_a_draft_already_there_is_drafts_present_and_nothing_is_written(gl: Gl) -> None:
    happy(gl)
    gl.drafts([draft(5), draft(6)])
    done = gl.run()
    assert done.code == 20 and done.error["reason"] == "drafts_present"
    assert done.error["details"] == {"count": 2}
    assert gl.writes() == [] and gl.audit() == []


def test_a_dry_run_checks_drafts_and_stops(gl: Gl) -> None:
    happy(gl)
    done = gl.run("--dry-run")
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("review", out)
    assert out["dry_run"] is True and out["plan"]["inline_comments"] == 1
    assert gl.writes() == [] and gl.audit() == []
    gl.drafts([draft(5)])  # a dry run reports a refusal too
    again = gl.run("--dry-run")
    assert again.code == 20
    schemas.validate_cli_output("review", again.json)
    assert again.json["would_refuse"]["reason"] == "drafts_present"


def test_an_invalid_position_is_exit_12_with_nothing_written(gl: Gl) -> None:
    gl.input({"event": "comment", "comments": [inline(line=9)]})
    done = gl.run()
    assert done.code == 12 and done.error["code"] == "invalid_position"
    assert gl.writes() == [] and gl.audit() == []
    assert DRAFTS not in gl.endpoints("GET")  # positions come before the drafts check


def test_a_head_other_than_the_expected_is_stale_head(gl: Gl) -> None:
    happy(gl)
    done = gl.run(expected_head="9" * 40)
    assert done.code == 20 and done.error["reason"] == "stale_head"
    assert gl.writes() == []


def test_the_diff_head_moving_is_stale_head(gl: Gl) -> None:
    """The positions were checked at another head than step 2 read."""
    happy(gl)
    from .gitlab_support import recording

    doc = recording("mr")
    doc["sha"] = "c" * 40
    doc["diff_refs"]["head_sha"] = "c" * 40
    for nth in (2, 3):  # the pin's two reads; the first read is step 2's
        gl.forges.answer("glab", mr_path(), method="GET", nth=nth, body=doc)
        gl.forges._rules.insert(0, gl.forges._rules.pop())
    gl.forges._write_script()
    done = gl.run()
    assert done.code == 20 and done.error["reason"] == "stale_head"
    assert gl.writes() == []


@pytest.mark.parametrize("version", ["17.1.0", "16.11.2", "garbage"])
def test_request_changes_on_an_old_or_unreadable_host_is_unsupported(gl: Gl, version: str) -> None:
    gl.input({"event": "request_changes", "body": "no", "comments": []})
    gl.version(version)
    done = gl.run()
    assert done.code == 12 and done.error["code"] == "unsupported_host_version"
    assert gl.writes() == [] and gl.audit() == []


# -- the draft stage fails ----------------------------------------------------------------------


def test_a_failed_second_draft_deletes_the_first_and_is_host_rejected(gl: Gl) -> None:
    happy(gl)
    gl.create_fails(422, {"message": "position is invalid"})
    gl.created(11)
    gl.drafts([], [{"id": 11, "author_id": ME, "note": "use c here"}])
    gl.deleted(11)
    done = gl.run()
    assert done.code == 12 and done.error["code"] == "host_rejected"
    assert "leftover_drafts" not in done.error["details"]
    assert f"{DRAFTS}/11" in gl.endpoints("DELETE")
    assert PUBLISH not in gl.endpoints("POST")
    assert [r["result"] for r in gl.audit()] == ["attempted", "failed"]


def test_a_timed_out_creation_deletes_a_draft_that_matches_what_was_sent(gl: Gl) -> None:
    gl.input({"event": "comment", "comments": [inline()]})
    gl.create_fails(502, {"message": "bad gateway"})
    position = {"new_path": "src/app.py", "new_line": 2, "old_line": None}
    gl.drafts(
        [],
        [
            {"id": 31, "author_id": ME, "note": "use c here", "position": position},
            {"id": 32, "author_id": ME, "note": "someone else's", "position": None},
        ],
    )
    gl.deleted(31)
    done = gl.run()
    assert done.code == 12 and done.error["code"] == "host_rejected"
    assert f"{DRAFTS}/31" in gl.endpoints("DELETE")
    assert f"{DRAFTS}/32" not in gl.endpoints("DELETE")  # not forgectl's: never deleted
    assert done.error["details"]["leftover_drafts"] == 1


def test_a_201_with_no_draft_id_is_not_believed_and_the_matching_draft_is_removed(gl: Gl) -> None:
    gl.input({"event": "comment", "comments": [inline()]})
    gl.create_fails(201, {"nothing": "useful"})
    position = {"new_path": "src/app.py", "new_line": 2, "old_line": None}
    gl.drafts([], [draft(41, note="use c here", position=position)])
    gl.deleted(41)
    done = gl.run()
    assert done.code == 12 and done.error["code"] == "host_rejected"
    assert gl.endpoints("DELETE") == [f"{DRAFTS}/41"]
    assert PUBLISH not in gl.endpoints("POST")
    assert [r["result"] for r in gl.audit()] == ["attempted", "failed"]


def test_a_failed_cleanup_reports_the_leftover_drafts(gl: Gl) -> None:
    happy(gl)
    gl.create_fails(500, {"message": "boom"})
    gl.created(11)
    gl.drafts([], [{"id": 11, "author_id": ME, "note": "use c here"}])
    gl.deleted(11, status=500)
    done = gl.run()
    assert done.code == 12 and done.error["code"] == "host_rejected"
    assert done.error["details"]["leftover_drafts"] == 1


def test_a_foreign_draft_at_the_relist_deletes_ours_and_is_drafts_present(gl: Gl) -> None:
    happy(gl)
    gl.drafts(
        [],
        [draft(11), draft(12), draft(99)],
        [],
    )
    gl.deleted(11)
    gl.deleted(12)
    done = gl.run()
    assert done.code == 20 and done.error["reason"] == "drafts_present"
    assert done.error["details"] == {"count": 1}
    assert sorted(gl.endpoints("DELETE")) == [f"{DRAFTS}/11", f"{DRAFTS}/12"]  # never 99
    assert PUBLISH not in gl.endpoints("POST")
    assert [r["result"] for r in gl.audit()] == ["attempted", "failed"]


# -- the publish --------------------------------------------------------------------------------


def test_a_publish_that_errors_with_own_drafts_still_there_deletes_them(gl: Gl) -> None:
    happy(gl)
    gl.drafts([], [draft(11), draft(12)])
    gl.publish(status=500, body={"message": "boom"})
    gl.drafts([], [draft(11), draft(12)])
    gl.deleted(11)
    gl.deleted(12)
    done = gl.run()
    assert done.code == 12 and done.error["code"] == "host_rejected"
    assert sorted(gl.endpoints("DELETE")) == [f"{DRAFTS}/11", f"{DRAFTS}/12"]
    assert DISCUSSIONS not in gl.endpoints("GET")


def test_a_publish_with_a_redirected_answer_is_not_believed(gl: Gl) -> None:
    """A 200 with a list is a redirected GET's answer (the documented status is 204)."""
    happy(gl)
    gl.publish(status=200, body=[])
    gl.drafts([], [draft(11), draft(12)])
    gl.deleted(11)
    gl.deleted(12)
    done = gl.run()
    assert done.code == 12 and done.error["code"] == "host_rejected"
    assert DISCUSSIONS not in gl.endpoints("GET")


def test_a_publish_error_with_the_drafts_gone_reads_as_published(gl: Gl) -> None:
    happy(gl)
    gl.drafts([], [draft(11), draft(12)], [])
    gl.publish(status=500, body={"message": "timeout"})
    done = gl.run()
    assert done.code == 0, done.err
    assert done.json["ids"] == ["gl:n:601", "gl:n:602"]
    assert gl.endpoints("DELETE") == []


def test_a_publish_error_then_a_failed_readback_is_outcome_unknown_never_zero(gl: Gl) -> None:
    happy(gl)
    gl.drafts([], [draft(11), draft(12)], [])
    gl.publish(status=500, body={"message": "timeout"})
    get(gl.forges, DISCUSSIONS, {"message": "down"}, status=400)
    rules = gl.forges._rules
    rules.insert(0, rules.pop())
    gl.forges._write_script()
    done = gl.run()
    assert done.code == 12 and done.error["code"] == "outcome_unknown"
    assert [r["result"] for r in gl.audit()] == ["attempted", "outcome_unknown"]


def test_a_publish_error_and_a_failed_relist_is_outcome_unknown(gl: Gl) -> None:
    happy(gl)
    gl.publish(status=500, body={"message": "timeout"})
    get(gl.forges, DRAFTS, {"message": "down"}, status=400, nth=3)
    rules = gl.forges._rules
    rules.insert(0, rules.pop())
    gl.forges._write_script()
    done = gl.run()
    assert done.code == 12 and done.error["code"] == "outcome_unknown"
    assert gl.endpoints("DELETE") == []  # nothing is deleted when the state is unknown


# -- the read-back ------------------------------------------------------------------------------


def test_a_body_that_does_not_come_back_is_quick_action_ran_and_the_state_step_is_skipped(
    gl: Gl,
) -> None:
    gl.input({"event": "request_changes", "body": "overall", "comments": [inline()]})
    gl.drafts([], [draft(11), draft(12)])
    gl.created(11, 12)
    gl.publish()
    gl.discussions(mine_published([(601, D1, "use c here"), (602, D2, "overall /merge")]))
    gl.request_changes_ok()
    done = gl.run()
    assert done.code == 12 and done.error["code"] == "quick_action_ran"
    assert done.error["details"] == {"ids": ["gl:n:601"]}
    assert "graphql" not in gl.endpoints("POST")
    assert [r["result"] for r in gl.audit()] == ["attempted", "quick_action_ran"]


def test_the_readback_ignores_other_authors_system_notes_and_old_ids(gl: Gl) -> None:
    happy(gl)
    gl.discussions(
        [
            disc(D1, note(TOP - 1, "use c here"), note(603, "use c here", author=77)),
            disc(D2, note(604, "overall", system=True), mine(602, "overall")),
            disc("3" * 40, mine(601, "use c here")),
        ]
    )
    done = gl.run()
    assert done.code == 0, done.err
    assert done.json["ids"] == ["gl:n:601", "gl:n:602"]
    assert done.json["thread_ids"] == [f"gl:d:{'3' * 40}", f"gl:d:{D2}"]


def test_a_failed_readback_is_a_warning_and_the_state_step_still_runs(gl: Gl) -> None:
    gl.input({"event": "request_changes", "body": "overall", "comments": []})
    gl.drafts([], [draft(12)])
    gl.created(12)
    gl.publish()
    get(gl.forges, DISCUSSIONS, {"message": "down"}, status=400)
    gl.request_changes_ok()
    done = gl.run()
    assert done.code == 0, done.err
    assert done.json["ids"] == [] and done.json["thread_ids"] == []
    assert done.json["warnings"] == ["read_back_failed"]
    assert "graphql" in gl.endpoints("POST")


# -- the state step -----------------------------------------------------------------------------


def test_request_changes_publishes_reads_back_then_sends_the_mutation(gl: Gl) -> None:
    gl.input({"event": "request_changes", "body": "overall", "comments": []})
    gl.drafts([], [draft(12)])
    gl.created(12)
    gl.publish()
    gl.discussions(mine_published([(602, D2, "overall")]))
    gl.request_changes_ok()
    done = gl.run()
    assert done.code == 0, done.err
    posts = gl.endpoints("POST")
    assert posts.index("graphql") > posts.index(PUBLISH)
    mutation = json.loads(next(c for c in gl.writes() if c.argv[-1] == "graphql").stdin or "")
    assert mutation["variables"] == {"projectPath": "user-9/proj", "iid": "7"}
    assert "mergeRequestRequestChanges" in mutation["query"]
    assert [r["result"] for r in gl.audit()] == ["attempted", "posted"]


def test_approve_publishes_then_approves_with_the_expected_head(gl: Gl) -> None:
    gl.input({"event": "approve", "body": "overall", "comments": []})
    gl.drafts([], [draft(12)])
    gl.created(12)
    gl.publish()
    gl.discussions(mine_published([(602, D2, "overall")]))
    gl.approve_ok()
    done = gl.run()
    assert done.code == 0, done.err
    approve = next(c for c in gl.writes() if c.argv[-1] == APPROVE)
    assert json.loads(approve.stdin or "") == {"sha": HEAD}
    assert gl.endpoints("POST").index(APPROVE) > gl.endpoints("POST").index(PUBLISH)
    assert [r["result"] for r in gl.audit()] == ["attempted", "approved"]


def test_an_approve_with_nothing_to_post_sends_only_the_approval(gl: Gl) -> None:
    gl.input({"event": "approve", "comments": []})
    gl.approve_ok()
    done = gl.run()
    assert done.code == 0, done.err
    assert gl.endpoints("POST") == [APPROVE]
    assert DRAFTS not in gl.endpoints("GET")


def test_a_failed_state_step_after_the_text_landed_is_outcome_partial(gl: Gl) -> None:
    gl.input({"event": "approve", "body": "overall", "comments": []})
    gl.drafts([], [draft(12)])
    gl.created(12)
    gl.publish()
    gl.discussions(mine_published([(602, D2, "overall")]))
    gl.forges.answer("glab", APPROVE, method="POST", status=500, body={"message": "boom"})
    done = gl.run()
    assert done.code == 12 and done.error["code"] == "outcome_partial"
    assert done.error["details"] == {
        "landed": {"ids": ["gl:n:602"], "thread_ids": [f"gl:d:{D2}"]},
        "failed": "approve",
    }
    assert [r["result"] for r in gl.audit()] == ["attempted", "outcome_partial"]
    assert gl.audit()[-1]["ids"] == ["gl:n:602"]


def test_an_approval_whose_head_moved_is_approved_stale_head(gl: Gl) -> None:
    gl.input({"event": "approve", "comments": []})
    gl.approve_ok()
    from .gitlab_support import recording

    moved = recording("mr")
    moved["sha"] = "d" * 40
    moved["diff_refs"]["head_sha"] = "d" * 40
    gl.forges.answer(
        "glab", mr_path(), method="GET", nth=3, body=moved
    )  # 1: step 2, 2: the poster, 3: after
    rules = gl.forges._rules
    rules.insert(0, rules.pop())
    gl.forges._write_script()
    done = gl.run()
    assert done.code == 12 and done.error["code"] == "approved_stale_head"


# -- dedupe -------------------------------------------------------------------------------------


def test_a_dedupe_hit_with_no_state_step_is_already_posted_and_writes_nothing(gl: Gl) -> None:
    gl.input({"event": "comment", "body": "overall", "comments": []})
    gl.notes([mine(700, "overall\n" + MARKER)])
    done = gl.run("--dedupe-key", "key-1")
    assert done.code == 0, done.err
    assert done.json["already_posted"] is True and done.json["ids"] == ["gl:n:700"]
    assert done.json["review_id"] is None
    assert gl.writes() == [] and gl.audit() == []
    assert DRAFTS not in gl.endpoints("GET")


def test_a_dedupe_hit_with_the_state_step_missing_only_does_the_state_step(gl: Gl) -> None:
    gl.input({"event": "approve", "body": "overall", "comments": []})
    gl.notes([mine(700, "overall\n" + MARKER)])
    gl.approve_ok()
    done = gl.run("--dedupe-key", "key-1")
    assert done.code == 0, done.err
    assert done.json["already_posted"] is True and done.json["follow_up_done"] is True
    assert gl.endpoints("POST") == [APPROVE]
    assert gl.endpoints("DELETE") == [] and DRAFTS not in gl.endpoints("GET")


def test_a_key_with_no_hit_posts_and_the_marker_lands_on_the_last_comment(gl: Gl) -> None:
    gl.input({"event": "comment", "comments": [inline()]})
    gl.drafts([], [draft(11)])
    gl.created(11)
    gl.publish()
    gl.discussions(mine_published([(601, D1, "use c here\n" + MARKER)]))
    done = gl.run("--dedupe-key", "key-1")
    assert done.code == 0, done.err
    sent = json.loads(
        next(c for c in gl.writes() if c.argv[-1].endswith("draft_notes")).stdin or ""
    )
    assert sent["note"] == "use c here\n" + MARKER


# -- the unit pieces ----------------------------------------------------------------------------


def test_a_listed_draft_is_ours_only_with_the_same_author_text_and_place() -> None:
    position = {"new_path": "a.py", "new_line": 2, "old_line": None}
    draft = Draft(1, str(ME), "text\n", dict(position))
    assert matches_sent(draft, str(ME), "text", position)
    assert not matches_sent(draft, "99", "text", position)
    assert not matches_sent(draft, str(ME), "other", position)
    assert not matches_sent(draft, str(ME), "text", {**position, "new_line": 3})
    assert not matches_sent(draft, str(ME), "text", None)  # a body draft has no place
    assert matches_sent(Draft(2, str(ME), "text", None), str(ME), "text", None)


def test_the_endpoints_are_paths_the_fake_knows(gl: Gl) -> None:
    """A guard on the helper itself: the draft regexes do not shadow each other."""
    assert re.match(rf"^{re.escape(DRAFTS)}(\?|$)", DRAFTS + "?page=1")
    assert not re.match(rf"^{re.escape(DRAFTS)}(\?|$)", PUBLISH)
