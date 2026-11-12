"""`forgectl merge --auto` (#23) on GitHub and GitLab: the head-match availability, the `--auto`
rows of the checks, arming, the already-armed no-op and verification of "armed or merged".

The real CLI runs in process against the fake `gh` and `glab` (tests/fakebin), which log every
call. No host is ever contacted. Every host wording and status assumed here is UNVERIFIED until
the live suite (#32, #33) re-checks it.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import pytest

from forgectl.cli import main
from forgectl.humanactions import Tty

from . import test_gitlab_merge as gl
from . import test_merge as gh
from .gitlab_support import HEAD as GL_HEAD
from .harness import schemas
from .test_gitlab_show_process import route
from .test_merge import HUMAN, Rig


@pytest.fixture
def rig(
    tmp_path: Path,
    fake_forges: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Rig:
    return Rig(tmp_path, fake_forges, monkeypatch, capsys)


HOST_USER = {"__typename": "User", "login": "user-2", "databaseId": 1002}
ENABLE = "enablePullRequestAutoMerge"


# -- GitHub ------------------------------------------------------------------------------------


def armed_doc(**kw: Any) -> dict[str, Any]:
    d = gh.doc(**kw)
    d["data"]["repository"]["pullRequest"]["autoMergeRequest"] = {"enabledBy": HOST_USER}
    return d


def stale_armed_merged() -> dict[str, Any]:
    d = gh.merged()
    d["data"]["repository"]["pullRequest"]["autoMergeRequest"] = {"enabledBy": HOST_USER}
    return d


def mutation(rig: Rig, *, errors: str | None = None) -> None:
    """Script the mutation answer; registered before the reads so its rule is tried first."""
    if errors is None:
        body: Any = {"data": {ENABLE: {"clientMutationId": None}}}
    else:
        body = {"errors": [{"message": errors}], "data": {ENABLE: None}}
    rig.forges.answer("gh", "graphql", method="POST", stdin_re=ENABLE, body=body)


def interactive(rig: Rig, *args: str, **kw: Any) -> gh.Result:
    """A human at a terminal: no expected values, a dialog, answered `y`."""
    return rig.run(*args, tty=HUMAN, stdin="y\n", expected=False, actor=None, yes=False, **kw)


def mutations(rig: Rig) -> list[dict[str, Any]]:
    return [
        json.loads(c.stdin or "")
        for c in rig.calls()
        if c.stdin and ENABLE in c.stdin and c.argv[-1] == "graphql"
    ]


def test_github_auto_with_expected_head_is_refused_with_no_call(rig: Rig) -> None:
    done = rig.run("--auto", actor="human:me")
    assert done.code == 20
    assert (done.error["code"], done.error["reason"]) == ("refused", "no_head_match")
    assert rig.calls() == []
    gh.assert_no_write(rig)


def test_github_auto_with_expected_head_is_refused_under_dry_run_without_a_tty(rig: Rig) -> None:
    done = rig.run("--auto", "--dry-run", actor="human:me")
    assert done.code == 20
    out = done.json
    schemas.validate_cli_output("merge", out)
    assert out["would_refuse"]["reason"] == "no_head_match"
    assert out["plan"]["auto"] is True and out["plan"]["head_sha"] is None
    assert rig.calls() == []


def test_github_auto_without_a_tty_and_without_expected_values_is_a_usage_error(rig: Rig) -> None:
    rig.reads(gh.doc(ci="pending"))
    done = rig.run("--auto", expected=False, actor="human:me")
    assert done.code == 2
    gh.assert_no_write(rig)


def test_github_auto_arms_a_pending_pr_pinned_to_the_head_seen(rig: Rig) -> None:
    mutation(rig)
    rig.reads(gh.doc(ci="pending"), gh.doc(ci="pending"), armed_doc(ci="pending"))
    done = interactive(rig, "--auto", method="squash")
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("merge", out)
    assert out == {
        "pr": gh.PR,
        "merged": False,
        "already_merged": False,
        "auto_merge_enabled": True,
        "merged_head_sha": None,
        "merge_commit_sha": None,
        "method": None,
        "dry_run": False,
    }
    (sent,) = mutations(rig)
    assert sent["variables"]["head"] == gh.HEAD
    assert sent["variables"]["method"] == "SQUASH"
    assert sent["variables"]["id"]
    assert rig.puts() == []
    assert "any later head" in done.err
    first, second = rig.audit()
    assert (first["result"], second["result"]) == ("attempted", "auto_merge_enabled")
    assert first["inputs"]["head_sha"] == gh.HEAD
    for call in rig.calls():
        assert call.token_sent == gh.TOKEN


def test_github_auto_with_nothing_blocking_is_a_plain_merge_carrying_sha(rig: Rig) -> None:
    rig.reads(gh.doc(), gh.doc(), gh.merged())
    rig.put()
    done = interactive(rig, "--auto")
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("merge", out)
    assert out["merged"] is True and out["auto_merge_enabled"] is False
    (put,) = rig.puts()
    assert json.loads(put.stdin or "") == {"sha": gh.HEAD, "merge_method": "squash"}
    assert mutations(rig) == []
    assert [r["result"] for r in rig.audit()] == ["attempted", "merged"]


def test_the_dialog_says_what_auto_will_do(rig: Rig) -> None:
    mutation(rig)
    rig.reads(gh.doc(ci="pending"), gh.doc(ci="pending"), armed_doc(ci="pending"))
    done = interactive(rig, "--auto")
    assert "arm auto-merge: any later head can merge" in done.err


@pytest.mark.parametrize(
    "wording",
    [
        "Pull request is in clean status",
        "Pull request Pull request is in unstable status",
        "Auto merge is not allowed for this repository",
    ],
)
def test_github_auto_unavailable_has_observed_and_never_falls_back_to_a_merge(
    rig: Rig, wording: str
) -> None:
    mutation(rig, errors=wording)
    rig.reads(gh.doc(ci="pending"))
    done = interactive(rig, "--auto")
    assert done.code == 20
    err = done.error
    assert (err["code"], err["reason"]) == ("refused", "auto_merge_unavailable")
    assert wording.split("Pull request ")[-1] in err["details"]["observed"]
    assert "without --auto" in err["message"]
    assert rig.puts() == []
    assert len(mutations(rig)) == 1
    assert [r["result"] for r in rig.audit()] == ["attempted", "failed"]


def test_github_auto_with_a_moved_head_is_stale_head(rig: Rig) -> None:
    mutation(rig, errors="Expected head sha didn't match current head ref. (expected head oid)")
    rig.reads(gh.doc(ci="pending"), gh.doc(ci="pending"), gh.doc(ci="pending", head=gh.OTHER))
    done = interactive(rig, "--auto")
    assert done.code == 20 and done.error["reason"] == "stale_head"
    assert [r["result"] for r in rig.audit()] == ["attempted", "failed"]


def test_github_auto_with_another_host_error_is_not_mapped_to_unavailable(rig: Rig) -> None:
    mutation(rig, errors="Something else went wrong")
    rig.reads(gh.doc(ci="pending"))
    done = interactive(rig, "--auto")
    assert done.code != 0
    assert done.error["code"] == "host_rejected"
    assert rig.puts() == []


@pytest.mark.parametrize("which", ["draft", "conflicts", "failing"])
def test_github_auto_refuses_what_arming_cannot_help(rig: Rig, which: str) -> None:
    d = gh.doc(ci="failing" if which == "failing" else "passing")
    pr = d["data"]["repository"]["pullRequest"]
    if which == "draft":
        pr["isDraft"] = True
    if which == "conflicts":
        pr["mergeable"] = "CONFLICTING"
        pr["mergeStateStatus"] = "DIRTY"
    rig.reads(d)
    done = interactive(rig, "--auto")
    assert done.code == 20 and done.error["reason"] == "not_ready"
    codes = {b["code"] for b in done.error["details"]["blocked_by"]}
    assert codes & {"draft", "conflicts", "checks_failing"}
    assert mutations(rig) == [] and rig.puts() == []
    gh.assert_no_write(rig)


def test_github_auto_on_a_merge_queue_is_no_head_match_even_for_a_human(rig: Rig) -> None:
    rig.reads(gh.doc(queue=True))
    done = interactive(rig, "--auto")
    assert done.code == 20 and done.error["reason"] == "no_head_match"
    gh.assert_no_write(rig)


def test_an_armed_pr_is_a_no_op_with_no_write_audit_or_hook(rig: Rig, tmp_path: Path) -> None:
    marker = tmp_path / "hook-ran"
    rig.reads(armed_doc(ci="pending"))
    done = interactive(
        rig, "--auto", method=None, extra_config=gl.hook_config("pre-merge", f"touch {marker}")
    )
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("merge", out)
    assert out["auto_merge_enabled"] is True and out["merged"] is False
    assert [c.argv[-1] for c in rig.calls()] == ["graphql"]
    assert not marker.exists()
    gh.assert_no_write(rig)
    assert "ignored" not in done.err


def test_an_armed_pr_warns_that_a_named_method_is_ignored(rig: Rig) -> None:
    rig.reads(armed_doc(ci="pending"))
    done = interactive(rig, "--auto", method="squash")
    assert done.code == 0 and "ignored" in done.err
    gh.assert_no_write(rig)


def test_an_armed_pr_under_dry_run_reports_the_plan(rig: Rig) -> None:
    rig.reads(armed_doc(ci="pending"))
    done = interactive(rig, "--auto", "--dry-run")
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("merge", out)
    assert out["dry_run"] is True and out["auto_merge_enabled"] is True
    assert out["plan"]["auto"] is True


def test_a_dry_run_auto_plans_and_sends_nothing(rig: Rig) -> None:
    rig.reads(gh.doc(ci="pending"))
    done = interactive(rig, "--auto", "--dry-run")
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("merge", out)
    assert out["plan"]["auto"] is True and out["auto_merge_enabled"] is False
    assert [c.argv[-1] for c in rig.calls()] == ["graphql"]


def test_a_stale_enabled_flag_after_merge_reads_as_merged_not_armed(rig: Rig) -> None:
    mutation(rig)
    rig.reads(gh.doc(ci="pending"), gh.doc(ci="pending"), stale_armed_merged())
    done = interactive(rig, "--auto")
    assert done.code == 0, done.err
    out = done.json
    assert out["merged"] is True and out["auto_merge_enabled"] is False
    assert [r["result"] for r in rig.audit()] == ["attempted", "merged"]


def test_an_accepted_arming_that_never_reads_armed_is_merge_unverified(rig: Rig) -> None:
    mutation(rig)
    rig.reads(gh.doc(ci="pending"))
    done = interactive(rig, "--auto")
    assert done.code == 12
    assert done.error["code"] == "merge_unverified"
    assert [r["result"] for r in rig.audit()] == ["attempted", "outcome_unknown"]
    assert rig.sleeps == [1.0, 2.0, 3.0, 4.0]


def test_armed_at_another_head_is_not_success(rig: Rig) -> None:
    mutation(rig)
    rig.reads(gh.doc(ci="pending"), gh.doc(ci="pending"), armed_doc(ci="pending", head=gh.OTHER))
    done = interactive(rig, "--auto")
    assert done.code == 12 and done.error["code"] == "merge_unverified"
    assert "armed at head" in done.error["details"]["observed"]


def test_a_plain_auto_merge_that_gains_a_blocker_after_the_dialog_refuses(rig: Rig) -> None:
    rig.reads(gh.doc(), gh.doc(ci="pending"))
    done = interactive(rig, "--auto")
    assert done.code == 20 and done.error["reason"] == "not_ready"
    gh.assert_no_write(rig)


def test_an_armed_run_that_gains_a_failing_check_after_the_dialog_refuses(rig: Rig) -> None:
    rig.reads(gh.doc(ci="pending"), gh.doc(ci="failing"))
    done = interactive(rig, "--auto")
    assert done.code == 20 and done.error["reason"] == "not_ready"
    gh.assert_no_write(rig)


def test_a_pr_armed_while_the_dialog_was_open_is_a_no_op(rig: Rig) -> None:
    rig.reads(gh.doc(ci="pending"), armed_doc(ci="pending"))
    done = interactive(rig, "--auto")
    assert done.code == 0
    assert done.json["auto_merge_enabled"] is True
    gh.assert_no_write(rig)


def test_an_armed_run_stays_an_arm_run_when_the_reread_shows_nothing_blocking(rig: Rig) -> None:
    mutation(rig, errors="Pull request is in clean status")
    rig.reads(gh.doc(ci="pending"), gh.doc())
    done = interactive(rig, "--auto")
    assert done.code == 20 and done.error["reason"] == "auto_merge_unavailable"
    assert rig.puts() == []
    assert len(mutations(rig)) == 1
    assert [r["result"] for r in rig.audit()] == ["attempted", "failed"]


def test_an_armed_run_whose_reread_is_clean_still_arms_when_the_host_accepts(rig: Rig) -> None:
    mutation(rig)
    rig.reads(gh.doc(ci="pending"), gh.doc(), armed_doc())
    done = interactive(rig, "--auto")
    assert done.code == 0, done.err
    assert done.json["auto_merge_enabled"] is True and done.json["merged"] is False
    assert rig.puts() == []
    assert len(mutations(rig)) == 1


def test_a_pr_armed_after_the_pre_merge_hooks_ran_exits_0_with_no_record_and_no_post_hooks(
    rig: Rig, tmp_path: Path
) -> None:
    pre, post = tmp_path / "pre.json", tmp_path / "post.json"
    rig.reads(gh.doc(ci="pending"), armed_doc(ci="pending"))
    config = gl.hook_config("pre-merge", f"cat > {pre}") + gl.hook_config(
        "post-merge", f"cat > {post}"
    ).replace("hooks:\n", "")
    done = interactive(rig, "--auto", extra_config=config)
    assert done.code == 0, done.err
    assert done.json["auto_merge_enabled"] is True
    assert pre.exists() and not post.exists()
    assert rig.audit() == []
    gh.assert_no_write(rig)


def test_the_hooks_see_auto_and_the_post_payload_matches_the_output(
    rig: Rig, tmp_path: Path
) -> None:
    pre, post = tmp_path / "pre.json", tmp_path / "post.json"
    mutation(rig)
    rig.reads(gh.doc(ci="pending"), gh.doc(ci="pending"), armed_doc(ci="pending"))
    config = gl.hook_config("pre-merge", f"cat > {pre}") + gl.hook_config(
        "post-merge", f"cat > {post}"
    ).replace("hooks:\n", "")
    done = interactive(rig, "--auto", method="squash", extra_config=config)
    assert done.code == 0, done.err
    before = json.loads(pre.read_text())
    schemas.validate_hook_payload("pre-merge", before)
    assert before["auto"] is True and before["method"] == "squash"
    after = json.loads(post.read_text())
    schemas.validate_hook_payload("post-merge", after)
    assert after["auto"] is True
    assert after["result"] == "auto_merge_enabled"
    assert after["method"] == done.json["method"] is None


# -- GitLab ------------------------------------------------------------------------------------


def version(rig: gl.Rig, text: str | None) -> None:
    """Script `GET /version`; registered before `rig.script`, whose own recording then loses."""
    if text is None:
        rig.forges.answer("glab", "version", status=404, body={"message": "404 Not found"})
    else:
        route(rig.forges, "version", {"version": f"{text}-ee", "revision": "abc"})


@pytest.fixture
def glrig(
    tmp_path: Path,
    fake_forges: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> gl.Rig:
    return gl.Rig(tmp_path, fake_forges, monkeypatch, capsys)


def armed_mr(**kw: Any) -> dict[str, Any]:
    d = gl.mr(**kw)
    d["merge_when_pipeline_succeeds"] = True
    d["merge_user"] = gl.recording("user") | {"id": 12, "username": "user-2"}
    return d


@pytest.mark.parametrize(
    ("ver", "param"),
    [
        ("17.11.0", "auto_merge"),
        ("18.0.2", "auto_merge"),
        ("17.10.4", "merge_when_pipeline_succeeds"),
        ("17.7.0", "merge_when_pipeline_succeeds"),
    ],
)
def test_gitlab_auto_sends_the_flag_for_the_version_with_sha(
    glrig: gl.Rig, ver: str, param: str
) -> None:
    version(glrig, ver)
    glrig.script(gl.mr(status="ci_still_running"), armed_mr(status="mergeable"))
    glrig.put()
    done = glrig.run("--auto", actor="human:me", method="squash")
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("merge", out)
    assert out["auto_merge_enabled"] is True and out["merged"] is False
    assert out["method"] is None and out["merged_head_sha"] is None
    assert glrig.body() == {"sha": GL_HEAD, "squash": True, param: True}
    other = "merge_when_pipeline_succeeds" if param == "auto_merge" else "auto_merge"
    assert other not in glrig.body()
    assert [r["result"] for r in glrig.audit()] == ["attempted", "auto_merge_enabled"]


def test_gitlab_auto_without_a_method_sends_only_sha_and_the_flag(glrig: gl.Rig) -> None:
    version(glrig, "17.11.1")
    glrig.script(gl.mr(status="ci_still_running"), armed_mr())
    glrig.put()
    done = glrig.run("--auto", actor="human:me", method=None)
    assert done.code == 0, done.err
    assert glrig.body() == {"sha": GL_HEAD, "auto_merge": True}


def test_gitlab_auto_merges_at_once_when_the_pipeline_already_passed(glrig: gl.Rig) -> None:
    version(glrig, "17.11.0")
    glrig.script(gl.mr(), gl.merged(merge_sha=gl.MERGE_SHA))
    glrig.put()
    done = glrig.run("--auto", actor="human:me", method="merge")
    assert done.code == 0, done.err
    out = done.json
    assert out["merged"] is True and out["auto_merge_enabled"] is False
    assert out["merge_commit_sha"] == gl.MERGE_SHA
    assert glrig.body() == {"sha": GL_HEAD, "auto_merge": True}
    assert [r["result"] for r in glrig.audit()] == ["attempted", "merged"]


def test_gitlab_a_stale_enabled_after_merge_reads_false(glrig: gl.Rig) -> None:
    version(glrig, "17.11.0")
    after = gl.merged(merge_sha=gl.MERGE_SHA)
    after["merge_when_pipeline_succeeds"] = True
    glrig.script(gl.mr(status="ci_still_running"), after)
    glrig.put()
    done = glrig.run("--auto", actor="human:me", method="merge")
    assert done.code == 0, done.err
    assert done.json["merged"] is True and done.json["auto_merge_enabled"] is False


def test_gitlab_auto_with_expected_head_below_17_7_is_no_head_match(glrig: gl.Rig) -> None:
    version(glrig, "17.6.9")
    glrig.script(gl.mr())
    done = glrig.run("--auto", actor="human:me", method="merge")
    assert done.code == 20 and done.error["reason"] == "no_head_match"
    assert glrig.puts() == []
    assert [c.argv[-1] for c in glrig.calls()] == ["version"]
    assert glrig.audit() == []


def test_gitlab_auto_with_expected_head_below_17_7_is_refused_under_dry_run(
    glrig: gl.Rig,
) -> None:
    version(glrig, "16.11.0")
    glrig.script(gl.mr())
    done = glrig.run("--auto", "--dry-run", actor="human:me", method="merge")
    assert done.code == 20
    assert done.json["would_refuse"]["reason"] == "no_head_match"
    schemas.validate_cli_output("merge", done.json)


def test_gitlab_auto_with_an_unreadable_version_is_exit_12_for_any_auto(glrig: gl.Rig) -> None:
    version(glrig, None)
    glrig.script(gl.mr())
    done = glrig.run("--auto", actor="human:me", method="merge")
    assert done.code == 12
    assert done.error["code"] == "unsupported_host_version"
    assert glrig.puts() == [] and glrig.audit() == []


def test_gitlab_interactive_auto_below_17_7_warns_that_a_push_may_not_cancel(
    glrig: gl.Rig,
) -> None:
    version(glrig, "17.5.0")
    running = gl.mr(status="ci_still_running")
    glrig.script(running, running, armed_mr())  # the read, the re-read after the dialog, verify
    glrig.put()
    glrig.monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    argv = [
        "merge", gl.PR, "--json", "--config", str(glrig.config()), "--auto", "--method", "merge",
    ]  # fmt: skip
    code = main(argv, tty=Tty(stdin=True, stderr=True))
    captured = glrig.capsys.readouterr()
    assert code == 0, captured.err
    assert "may not cancel" in captured.err
    assert glrig.body() == {"sha": GL_HEAD, "merge_when_pipeline_succeeds": True}


def test_gitlab_a_405_on_a_failed_pipeline_does_not_merge(glrig: gl.Rig) -> None:
    version(glrig, "17.11.0")
    # the first read shows nothing wrong; the host then answers 405 and the re-read shows why
    glrig.script(gl.mr(), gl.mr(status="ci_must_pass"), failing=False)
    glrig.put(405, {"message": "405 Method Not Allowed"})
    done = glrig.run("--auto", actor="human:me", method="merge")
    assert done.code != 0
    assert done.error["code"] in ("refused", "host_rejected")
    assert len(glrig.puts()) == 1
    assert [r["result"] for r in glrig.audit()] == ["attempted", "failed"]


def test_gitlab_a_405_that_reads_armed_at_the_sent_head_is_armed_even_with_blockers(
    glrig: gl.Rig,
) -> None:
    version(glrig, "17.11.0")
    glrig.script(gl.mr(status="ci_still_running"), armed_mr(status="ci_must_pass"), failing=False)
    glrig.put(405, {"message": "405 Method Not Allowed"})
    done = glrig.run("--auto", actor="human:me", method="merge")
    assert done.code == 0, done.err
    assert done.json["auto_merge_enabled"] is True
    assert len(glrig.puts()) == 1
    assert [r["result"] for r in glrig.audit()] == ["attempted", "auto_merge_enabled"]


def test_gitlab_a_failed_pipeline_seen_before_the_write_is_refused(glrig: gl.Rig) -> None:
    version(glrig, "17.11.0")
    glrig.script(gl.mr(), failing=True)
    done = glrig.run("--auto", actor="human:me", method="merge")
    assert done.code == 20 and done.error["reason"] == "not_ready"
    assert {"code": "checks_failing"} in done.error["details"]["blocked_by"]
    assert glrig.puts() == [] and glrig.audit() == []


@pytest.mark.parametrize("status", ["draft_status", "conflict"])
def test_gitlab_auto_refuses_draft_and_conflicts(glrig: gl.Rig, status: str) -> None:
    version(glrig, "17.11.0")
    glrig.script(gl.mr(status=status))
    done = glrig.run("--auto", actor="human:me", method="merge")
    assert done.code == 20 and done.error["reason"] == "not_ready"
    assert glrig.puts() == []


def test_gitlab_auto_is_not_refused_for_pending_or_unapproved(glrig: gl.Rig) -> None:
    version(glrig, "17.11.0")
    glrig.script(gl.mr(status="not_approved"), armed_mr())
    glrig.put()
    done = glrig.run("--auto", actor="human:me", method="merge")
    assert done.code == 0, done.err
    assert done.json["auto_merge_enabled"] is True


def test_gitlab_an_armed_mr_is_a_no_op(glrig: gl.Rig) -> None:
    version(glrig, "17.11.0")
    glrig.script(armed_mr(status="ci_still_running"))
    done = glrig.run("--auto", actor="human:me", method="merge")
    assert done.code == 0, done.err
    assert done.json["auto_merge_enabled"] is True and done.json["merged"] is False
    assert glrig.puts() == [] and glrig.audit() == []
    assert "ignored" in done.err


def test_gitlab_an_accepted_arming_that_never_shows_armed_is_merge_unverified(
    glrig: gl.Rig,
) -> None:
    version(glrig, "17.11.0")
    glrig.script(gl.mr(status="ci_still_running"))
    glrig.put()
    done = glrig.run("--auto", actor="human:me", method="merge")
    assert done.code == 12 and done.error["code"] == "merge_unverified"


def test_gitlab_a_locked_mr_is_not_yet_decided_while_arming(glrig: gl.Rig) -> None:
    version(glrig, "17.11.0")
    glrig.script(gl.mr(status="ci_still_running"), armed_mr())
    glrig.put(423, {"message": "locked"})
    done = glrig.run("--auto", actor="human:me", method="merge")
    assert done.code == 0, done.err
    assert done.json["auto_merge_enabled"] is True
    assert len(glrig.puts()) == 1


def test_gitlab_hooks_see_auto_and_a_null_method_when_armed(glrig: gl.Rig, tmp_path: Path) -> None:
    out = tmp_path / "post.json"
    version(glrig, "17.11.0")
    glrig.script(gl.mr(status="ci_still_running"), armed_mr())
    glrig.put()
    done = glrig.run(
        "--auto",
        actor="human:me",
        method="merge",
        extra_config=gl.hook_config("post-merge", f"cat > {out}"),
    )
    assert done.code == 0, done.err
    payload = json.loads(out.read_text())
    schemas.validate_hook_payload("post-merge", payload)
    assert payload["auto"] is True and payload["result"] == "auto_merge_enabled"
    assert payload["method"] == done.json["method"] is None


def test_the_version_pattern_is_the_documented_one() -> None:
    assert re.match(r"^(\d{1,6})\.(\d{1,6})", "17.11.0")
