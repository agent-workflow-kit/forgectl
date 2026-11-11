"""`forgectl merge` on GitLab (#22): the method table, `squash` only after the checks, the answers
of the merge route and the verification by reading.

The real CLI runs in process against the fake `glab` (tests/fakebin), which logs every call, so
each test asserts what reached the host (the PUT in particular) as well as what was printed. No
host is ever contacted. Every GitLab status and wording assumed here is UNVERIFIED until the live
suite (#33) re-checks it.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from forgectl import merge as merge_module
from forgectl.cli import main
from forgectl.humanactions import Tty

from .gitlab_support import HEAD, TOKEN, mr_path, recording
from .harness import golden, schemas
from .harness.fixtures import forgectl_env
from .harness.forges import Call, FakeForges
from .harness.safety import assert_call_safe
from .test_gitlab_show_process import route, script
from .test_merge import NO_TTY, Result

PR = "gitlab.com/user-9/proj#7"
MERGE_ENDPOINT = "projects/101/merge_requests/7/merge"
BASE = "main"
OTHER = "e5" * 20
SQUASH_SHA = "f6" * 20
MERGE_SHA = "d4" * 20


def mr(
    *,
    state: str = "opened",
    status: str = "mergeable",
    head: str = HEAD,
    base: str = BASE,
    squash_on_merge: bool = False,
    squash_sha: str | None = None,
    merge_sha: str | None = None,
) -> dict[str, Any]:
    d = recording("mr")
    d["state"] = state
    d["detailed_merge_status"] = status
    d["sha"] = head
    d["target_branch"] = base
    d["squash_on_merge"] = squash_on_merge
    d["squash"] = squash_on_merge
    if state == "merged":
        d.update(
            merged_at="2026-10-06T08:00:00.000Z",
            merge_user=recording("user") | {"id": 12, "username": "user-2"},
            detailed_merge_status="not_open",
            squash_commit_sha=squash_sha,
            merge_commit_sha=merge_sha,
        )
    if state == "closed":
        d["closed_at"] = "2026-10-06T08:00:00.000Z"
    return d


def merged(**kw: Any) -> dict[str, Any]:
    return mr(state="merged", **kw)


def project(merge_method: str = "merge", squash_option: str = "default_off") -> dict[str, Any]:
    d = recording("project")
    d["merge_method"] = merge_method
    d["squash_option"] = squash_option
    return d


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
        self.sleeps: list[float] = []
        for key, value in forgectl_env(forges, {"MERGE_TOKEN": TOKEN}).items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(merge_module, "_sleep", self.sleeps.append)

    def script(
        self, *reads: dict[str, Any], proj: dict[str, Any] | None = None, failing: bool = False
    ) -> None:
        """One MR answer per read, in order (the last repeats), then the standard recordings."""
        for index, d in enumerate(reads):
            last = index == len(reads) - 1
            self.forges.answer(
                "glab",
                endpoint_re=rf"^{re.escape(mr_path())}(\?|$)",
                method="GET",
                body=d,
                times=None if last else 1,
            )
        route(self.forges, "projects/101", proj or project())
        if failing:
            statuses = recording("statuses")
            statuses[1]["status"] = "failed"
            route(
                self.forges,
                f"projects/101/repository/commits/{HEAD}/statuses",
                statuses,
                headers={"X-Next-Page": ""},
            )
        script(self.forges, "gitlab.com", "")

    def put(self, status: int = 200, body: Any = None, **kw: Any) -> None:
        self.forges.answer(
            "glab",
            MERGE_ENDPOINT,
            method="PUT",
            status=status,
            body={"state": "merged"} if body is None else body,
            **kw,
        )

    def config(self, extra: str = "") -> Path:
        path = self.tmp / "config.yaml"
        path.write_text("hosts:\n  gitlab.com:\n    token_env: MERGE_TOKEN\n" + extra, "utf-8")
        return path

    def run(
        self,
        *args: str,
        tty: Tty = NO_TTY,
        extra_config: str = "",
        method: str | None = "squash",
        actor: str | None = "auto:bot",
        pr: str = PR,
    ) -> Result:
        argv = ["merge", pr, "--json", "--config", str(self.config(extra_config))]
        argv += ["--expected-head", HEAD, "--expected-base", BASE, "--yes"]
        if method:
            argv += ["--method", method]
        if actor:
            argv += ["--actor", actor]
        argv += list(args)
        self.monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        code = main(argv, tty=tty)
        captured = self.capsys.readouterr()
        return Result(code, captured.out, captured.err)

    def calls(self) -> list[Call]:
        return self.forges.calls("glab")

    def puts(self) -> list[Call]:
        return [c for c in self.calls() if c.argv[-1] == MERGE_ENDPOINT]

    def body(self) -> dict[str, Any]:
        (put,) = self.puts()
        body: dict[str, Any] = json.loads(put.stdin or "")
        return body

    def audit(self) -> list[dict[str, Any]]:
        path = Path(os.environ["XDG_STATE_HOME"]) / "forgectl" / "audit.jsonl"
        if not path.exists():
            return []
        records = [json.loads(x) for x in path.read_text().splitlines() if x]
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


def assert_no_write(rig: Rig) -> None:
    assert rig.puts() == []
    assert rig.audit() == []


# -- the happy path, one per method ------------------------------------------------------------


def test_squash_sends_squash_true_and_the_expected_head(rig: Rig) -> None:
    rig.script(mr(), merged(squash_sha=SQUASH_SHA, squash_on_merge=True))
    rig.put()
    done = rig.run(method="squash")
    assert done.code == 0, done.err
    out = done.json
    schemas.validate_cli_output("merge", out)
    assert out == {
        "pr": PR,
        "merged": True,
        "already_merged": False,
        "auto_merge_enabled": False,
        "merged_head_sha": HEAD,
        "merge_commit_sha": SQUASH_SHA,
        "method": "squash",
        "dry_run": False,
    }
    assert rig.body() == {"sha": HEAD, "squash": True}
    for call in rig.calls():
        assert_call_safe(call, host="gitlab.com", mode="token")
        assert call.token_sent == TOKEN


def test_merge_on_a_merge_project_sends_no_squash(rig: Rig) -> None:
    rig.script(mr(), merged(merge_sha=MERGE_SHA), proj=project("merge", "default_off"))
    rig.put()
    done = rig.run(method="merge")
    assert done.code == 0, done.err
    assert done.json["method"] == "merge"
    assert done.json["merge_commit_sha"] == MERGE_SHA
    assert done.json["merged_head_sha"] == HEAD
    assert rig.body() == {"sha": HEAD}


def test_merge_on_a_rebase_merge_project(rig: Rig) -> None:
    rig.script(mr(), merged(merge_sha=MERGE_SHA), proj=project("rebase_merge"))
    rig.put()
    done = rig.run(method="merge")
    assert done.code == 0, done.err
    assert (done.json["method"], rig.body()) == ("merge", {"sha": HEAD})


def test_rebase_on_an_ff_project_falls_back_to_the_head_for_the_commit(rig: Rig) -> None:
    # an ff merge has neither a merge commit nor a squash commit: the fallback is the head
    rig.script(mr(), merged(), proj=project("ff"))
    rig.put()
    done = rig.run(method="rebase")
    assert done.code == 0, done.err
    assert done.json["method"] == "rebase"
    assert done.json["merged_head_sha"] == HEAD
    assert done.json["merge_commit_sha"] == HEAD
    assert rig.body() == {"sha": HEAD}


def test_the_write_carries_nothing_but_sha_and_squash(rig: Rig) -> None:
    rig.script(mr(), merged(squash_sha=SQUASH_SHA))
    rig.put()
    rig.run(method="squash")
    assert set(rig.body()) == {"sha", "squash"}
    (put,) = rig.puts()
    assert put.argv[-1] == MERGE_ENDPOINT  # the numeric project id, never a path from input


@pytest.mark.parametrize(
    ("merge_method", "squashed", "expected"),
    [("merge", False, "merge"), ("rebase_merge", False, "merge"), ("ff", False, "rebase")]
    + [("merge", True, "squash"), ("ff", True, "squash")],
)
def test_with_no_flag_the_method_is_derived_from_the_merged_mr(
    rig: Rig, merge_method: str, squashed: bool, expected: str
) -> None:
    after = merged(squash_sha=SQUASH_SHA if squashed else None, squash_on_merge=squashed)
    rig.script(mr(squash_on_merge=squashed), after, proj=project(merge_method, "default_off"))
    rig.put()
    done = rig.run(method=None)
    assert done.code == 0, done.err
    assert done.json["method"] == expected
    assert rig.body() == {"sha": HEAD}  # nothing is sent: the project decides
    first, second = rig.audit()
    assert first["inputs"]["method"] is None and second["result"] == "merged"


def test_a_configured_method_is_used_when_no_flag_is_given(rig: Rig) -> None:
    rig.script(mr(), merged(squash_sha=SQUASH_SHA))
    rig.put()
    config = "repos:\n  gitlab.com/user-9/proj:\n    merge_method: squash\n"
    done = rig.run(method=None, extra_config=config)
    assert done.code == 0, done.err
    assert rig.body() == {"sha": HEAD, "squash": True}


def test_the_audit_records_the_method_sent(rig: Rig) -> None:
    rig.script(mr(), merged(squash_sha=SQUASH_SHA))
    rig.put()
    rig.run(method="squash")
    first, second = rig.audit()
    assert (first["result"], second["result"]) == ("attempted", "merged")
    assert first["inputs"]["method"] == "squash" and first["target"] == PR
    assert TOKEN not in json.dumps(rig.audit())


def test_a_dry_run_reports_a_null_method_when_the_project_decides(rig: Rig) -> None:
    rig.script(mr())
    done = rig.run("--dry-run", method=None)
    assert done.code == 0, done.err
    assert done.json["method"] is None and done.json["plan"]["method"] is None
    assert rig.puts() == []


# -- the method table --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "merge_method", "squash_option", "allowed"),
    [
        ("squash", "merge", "never", ["merge"]),
        ("merge", "merge", "always", ["squash"]),
        ("merge", "rebase_merge", "always", ["squash"]),
        ("merge", "ff", "default_off", ["squash", "rebase"]),
        ("rebase", "merge", "default_off", ["squash", "merge"]),
        ("rebase", "rebase_merge", "default_on", ["squash", "merge"]),
        ("rebase", "ff", "always", ["squash"]),
    ],
)
def test_a_method_the_project_does_not_allow_is_refused_before_any_write(
    rig: Rig, method: str, merge_method: str, squash_option: str, allowed: list[str]
) -> None:
    rig.script(mr(), proj=project(merge_method, squash_option))
    rig.put()
    done = rig.run(method=method)
    assert done.code == 20 and done.error["reason"] == "method_not_allowed"
    assert done.error["details"] == {"allowed": allowed}
    assert_no_write(rig)


@pytest.mark.parametrize("method", ["merge", "rebase"])
def test_merge_or_rebase_on_an_mr_that_squashes_on_merge_is_refused(rig: Rig, method: str) -> None:
    proj = project("ff" if method == "rebase" else "merge")
    rig.script(mr(squash_on_merge=True), proj=proj)
    rig.put()
    done = rig.run(method=method)
    assert done.code == 20 and done.error["reason"] == "method_not_allowed"
    assert done.error["details"] == {"allowed": ["squash"]}
    assert_no_write(rig)


def test_squash_on_an_mr_that_squashes_on_merge_is_sent(rig: Rig) -> None:
    rig.script(mr(squash_on_merge=True), merged(squash_sha=SQUASH_SHA, squash_on_merge=True))
    rig.put()
    assert rig.run(method="squash").code == 0
    assert rig.body() == {"sha": HEAD, "squash": True}


def test_unknown_project_settings_with_no_method_need_one(rig: Rig) -> None:
    bad = project()
    bad["merge_method"] = "something_new"
    rig.script(mr(), proj=bad)
    done = rig.run(method=None)
    assert done.code == 2 and done.error["code"] == "method_required"
    assert done.error["details"] == {"allowed": []}
    assert_no_write(rig)


def test_unknown_project_settings_with_a_method_let_the_host_decide(rig: Rig) -> None:
    bad = project()
    bad["squash_option"] = "something_new"
    rig.script(mr(), merged(squash_sha=SQUASH_SHA), proj=bad)
    rig.put()
    assert rig.run(method="squash").code == 0
    assert rig.body() == {"sha": HEAD, "squash": True}


# -- squash is not sent when a check refuses ---------------------------------------------------


@pytest.mark.parametrize(
    ("first", "reason"),
    [
        (mr(head=OTHER), "stale_head"),
        (mr(base="release"), "stale_base"),
        (mr(state="closed"), "not_open"),
        (mr(status="draft_status"), "not_ready"),
        (mr(status="conflict"), "not_ready"),
        (mr(status="not_approved"), "not_ready"),
        (mr(status="need_rebase"), "not_ready"),
    ],
)
def test_a_refused_squash_never_sends_squash(rig: Rig, first: dict[str, Any], reason: str) -> None:
    rig.script(first)
    rig.put()
    done = rig.run(method="squash")
    assert done.code == 20 and done.error["reason"] == reason
    assert_no_write(rig)


def test_a_plain_merge_is_refused_on_a_failed_pipeline_the_host_reads_as_mergeable(
    rig: Rig,
) -> None:
    rig.script(mr(status="mergeable"), failing=True)
    rig.put()  # the host would accept it
    done = rig.run(method="merge", actor="human:me")
    assert done.code == 20 and done.error["reason"] == "not_ready"
    assert {"code": "checks_failing"} in done.error["details"]["blocked_by"]
    assert_no_write(rig)


def test_a_non_human_actor_needs_a_passing_ci(rig: Rig) -> None:
    rig.script(mr(status="mergeable"), failing=True)
    done = rig.run(method="merge", actor="auto:bot")
    assert done.code == 20 and done.error["reason"] == "not_ready"
    assert_no_write(rig)


def test_an_already_merged_mr_at_the_expected_head_sends_nothing(rig: Rig) -> None:
    rig.script(merged(merge_sha=MERGE_SHA))
    done = rig.run(method="squash")
    assert done.code == 0, done.err
    assert done.json["already_merged"] is True and done.json["method"] is None
    assert_no_write(rig)


def test_a_merged_mr_at_another_head_is_stale_head(rig: Rig) -> None:
    rig.script(merged(head=OTHER))
    done = rig.run()
    assert done.code == 20 and done.error["reason"] == "stale_head"
    assert_no_write(rig)


def test_a_merged_mr_into_another_base_is_stale_base(rig: Rig) -> None:
    rig.script(merged(base="release"))
    done = rig.run()
    assert done.code == 20 and done.error["reason"] == "stale_base"
    assert_no_write(rig)


def test_the_merge_route_is_the_only_write_ever_sent(rig: Rig) -> None:
    rig.script(mr(), merged(squash_sha=SQUASH_SHA))
    rig.put()
    rig.run(method="squash")
    writes = [c for c in rig.calls() if "-X" in c.argv and "GET" not in c.argv]
    assert all(c.argv[-1] == MERGE_ENDPOINT for c in writes)
    assert not any("rebase" in c.argv[-1] for c in rig.calls())


# -- the answers of the merge route ------------------------------------------------------------


def test_a_409_is_stale_head_whatever_the_method(rig: Rig) -> None:
    rig.script(mr(), mr(head=OTHER), proj=project("ff"))
    rig.put(409, {"message": "SHA does not match HEAD of source branch"})
    done = rig.run(method="rebase")
    assert done.code == 20 and done.error["reason"] == "stale_head"
    assert done.error["details"] == {"head_sha": OTHER}
    assert [r["result"] for r in rig.audit()] == ["attempted", "failed"]


def test_a_405_whose_re_read_shows_merged_at_the_expected_head_is_already_merged(
    rig: Rig,
) -> None:
    rig.script(mr(), merged(merge_sha=MERGE_SHA))
    rig.put(405, {"message": "405 Method Not Allowed"})
    done = rig.run(method="merge")
    assert done.code == 0, done.err
    assert done.json["already_merged"] is True and done.json["method"] is None
    assert [r["result"] for r in rig.audit()] == ["attempted", "merged"]


def test_a_405_whose_re_read_shows_another_head_merged_is_merge_unverified(rig: Rig) -> None:
    rig.script(mr(), merged(head=OTHER))
    rig.put(405)
    done = rig.run(method="merge")
    assert done.code == 12 and done.error["code"] == "merge_unverified"
    assert [r["result"] for r in rig.audit()] == ["attempted", "merged_unexpected"]


def test_a_405_with_blockers_on_the_re_read_is_not_ready(rig: Rig) -> None:
    rig.script(mr(), mr(status="ci_must_pass"))
    rig.put(405, {"message": "Pipeline must succeed"})
    done = rig.run(method="merge")
    assert done.code == 20 and done.error["reason"] == "not_ready"


@pytest.mark.parametrize("status", [405, 406, 422])
def test_a_refusal_with_a_clean_re_read_is_host_rejected(rig: Rig, status: int) -> None:
    rig.script(mr())
    rig.put(status, {"message": "Branch cannot be merged"})
    done = rig.run(method="merge")
    assert done.code == 12 and done.error["code"] == "host_rejected"
    assert done.error["details"] == {"status": status}
    assert [r["result"] for r in rig.audit()] == ["attempted", "failed"]


def test_a_5xx_whose_re_read_shows_merged_is_success(rig: Rig) -> None:
    rig.script(mr(), merged(merge_sha=MERGE_SHA))
    rig.put(502, {"message": "Bad gateway"})
    done = rig.run(method="merge")
    assert done.code == 0, done.err
    assert done.json["merged"] is True and rig.sleeps == []


def test_a_5xx_whose_mr_stays_open_is_outcome_unknown(rig: Rig) -> None:
    rig.script(mr())
    rig.put(502, {"message": "Bad gateway"})
    done = rig.run(method="merge")
    assert done.code == 12 and done.error["code"] == "outcome_unknown"
    assert rig.sleeps == [1.0, 2.0, 3.0, 4.0]
    assert [r["result"] for r in rig.audit()] == ["attempted", "outcome_unknown"]


def test_a_200_with_an_mr_still_open_is_merge_unverified(rig: Rig) -> None:
    rig.script(mr())
    rig.put()
    done = rig.run(method="merge")
    assert done.code == 12 and done.error["code"] == "merge_unverified"
    assert rig.sleeps == [1.0, 2.0, 3.0, 4.0]


def test_a_200_with_a_merge_at_another_head_is_merge_unverified(rig: Rig) -> None:
    rig.script(mr(), merged(head=OTHER))
    rig.put()
    done = rig.run(method="merge")
    assert done.code == 12 and done.error["code"] == "merge_unverified"
    assert done.error["details"]["head_sha"] == OTHER


def test_a_transient_locked_state_is_not_yet_decided(rig: Rig) -> None:
    locked = mr(state="locked")
    rig.script(mr(), locked, locked, merged(merge_sha=MERGE_SHA))
    rig.put()
    done = rig.run(method="merge")
    assert done.code == 0, done.err
    assert done.json["merged"] is True
    assert rig.sleeps == [1.0, 2.0]


def test_a_head_that_moves_during_verification_is_reported_not_trusted(rig: Rig) -> None:
    rig.script(mr(), merged(head=OTHER, merge_sha=MERGE_SHA))
    rig.put()
    done = rig.run(method="merge")
    assert done.code == 12 and done.error["code"] == "merge_unverified"


def test_the_first_read_waits_while_the_merge_status_is_computed(rig: Rig) -> None:
    rig.script(mr(status="checking"), mr(status="checking"), mr(), merged(merge_sha=MERGE_SHA))
    rig.put()
    done = rig.run(method="merge")
    assert done.code == 0, done.err
    assert done.json["merged"] is True


def test_a_status_that_stays_unknown_blocks_the_merge(rig: Rig) -> None:
    rig.script(mr(status="checking"))
    rig.put()
    done = rig.run(method="merge")
    assert done.code == 20 and done.error["reason"] == "not_ready"
    assert {"code": "mergeability_unknown"} in done.error["details"]["blocked_by"]
    assert_no_write(rig)


def test_auto_is_still_not_implemented(rig: Rig) -> None:
    rig.script(mr())
    done = rig.run("--auto", actor="human:me", method="merge")
    assert done.code == 1 and "not implemented" in done.error["message"]
    assert_no_write(rig)


# -- round 1 review fixes ----------------------------------------------------------------------


def hook_config(name: str, cmd: str) -> str:
    return f"hooks:\n  {name}:\n    - cmd: {json.dumps(cmd)}\n"


@pytest.mark.parametrize(
    ("merge_method", "squashed", "flag", "expected"),
    [
        ("merge", False, None, "merge"),
        ("ff", False, None, "rebase"),
        ("merge", True, None, "squash"),
        ("ff", False, "rebase", "rebase"),
        ("merge", False, "merge", "merge"),
    ],
)
def test_the_post_merge_payload_method_is_the_reported_method(
    rig: Rig, tmp_path: Path, merge_method: str, squashed: bool, flag: str | None, expected: str
) -> None:
    out = tmp_path / "post.json"
    after = merged(squash_sha=SQUASH_SHA if squashed else None, squash_on_merge=squashed)
    rig.script(mr(squash_on_merge=squashed), after, proj=project(merge_method))
    rig.put()
    done = rig.run(method=flag, extra_config=hook_config("post-merge", f"cat > {out}"))
    assert done.code == 0, done.err
    assert done.json["method"] == expected
    assert json.loads(out.read_text())["method"] == expected


def test_a_sent_squash_is_reported_even_when_the_re_read_shows_no_squash(rig: Rig) -> None:
    rig.script(mr(), merged(merge_sha=MERGE_SHA, squash_on_merge=False))
    rig.put()
    done = rig.run(method="squash")
    assert done.code == 0, done.err
    assert done.json["method"] == "squash"


def test_a_423_whose_re_read_shows_merged_is_success_after_one_put(rig: Rig) -> None:
    rig.script(mr(), merged(merge_sha=MERGE_SHA))
    rig.put(423, {"message": "locked"})
    done = rig.run(method="merge")
    assert done.code == 0, done.err
    assert done.json["merged"] is True
    assert len(rig.puts()) == 1


def test_a_423_whose_mr_stays_open_is_outcome_unknown_after_one_put(rig: Rig) -> None:
    rig.script(mr())
    rig.put(423, {"message": "locked"})
    done = rig.run(method="merge")
    assert done.code == 12 and done.error["code"] == "outcome_unknown"
    assert len(rig.puts()) == 1
    assert [r["result"] for r in rig.audit()] == ["attempted", "outcome_unknown"]


@pytest.mark.parametrize("status", [405, 406, 422])
def test_a_refusal_whose_re_read_shows_locked_is_polled_not_not_ready(
    rig: Rig, status: int
) -> None:
    rig.script(mr(), mr(state="locked"), merged(merge_sha=MERGE_SHA))
    rig.put(status, {"message": "x"})
    done = rig.run(method="merge")
    assert done.code == 0, done.err
    assert done.json["merged"] is True
    assert len(rig.puts()) == 1


def test_a_refusal_whose_re_read_stays_locked_is_outcome_unknown(rig: Rig) -> None:
    rig.script(mr(), mr(state="locked"))
    rig.put(422, {"message": "x"})
    done = rig.run(method="merge")
    assert done.code == 12 and done.error["code"] == "outcome_unknown"


def test_the_host_rejected_message_names_the_real_status(rig: Rig) -> None:
    rig.script(mr())
    rig.put(422, {"message": "Branch cannot be merged"})
    done = rig.run(method="merge")
    assert "(422)" in done.error["message"] and "(405)" not in done.error["message"]


def test_the_squash_refusal_has_no_doubled_word(rig: Rig) -> None:
    rig.script(mr(squash_on_merge=True))
    done = rig.run(method="merge")
    assert done.code == 20
    assert "merge merge" not in done.error["message"]


def test_the_gitlab_backend_offers_the_merge_hooks() -> None:
    from forgectl.backends.gitlab import GitLabBackend

    assert callable(getattr(GitLabBackend, "check_method", None))
    assert callable(getattr(GitLabBackend, "merged_method", None))


def test_check_method_without_facts_is_internal(rig: Rig) -> None:
    from forgectl.backends.gitlab import GitLabBackend
    from forgectl.errors import ForgectlError

    backend = GitLabBackend.__new__(GitLabBackend)
    backend._merge_facts = {}
    pr = object.__new__(type("P", (), {}))
    pr.pr = "x"  # type: ignore[attr-defined]
    with pytest.raises(ForgectlError) as err:
        backend.check_method(pr, "merge")  # type: ignore[arg-type]
    assert err.value.code == "internal"


def test_the_no_flag_method_is_not_resolved_again_at_the_re_read(rig: Rig) -> None:
    # the first read lets the project decide; the re-read after a hook sees an unknown setting,
    # which would need a method if it were resolved again
    bad = project()
    bad["merge_method"] = "something_new"
    rig.forges.answer("glab", endpoint_re=r"^projects/101(\?|$)", body=project(), times=1)
    rig.forges.answer("glab", endpoint_re=r"^projects/101(\?|$)", body=bad)
    rig.script(mr(), mr(), merged(merge_sha=MERGE_SHA), proj=bad)
    rig.put()
    done = rig.run(method=None, extra_config=hook_config("pre-merge", "true"))
    assert done.code == 0, done.err


# -- goldens -----------------------------------------------------------------------------------


def test_golden_squash(rig: Rig) -> None:
    rig.script(mr(), merged(squash_sha=SQUASH_SHA, squash_on_merge=True))
    rig.put()
    done = rig.run(method="squash")
    assert done.code == 0, done.err
    golden.assert_golden("merge/gitlab-squash", done.json, schema_id="forgectl/cli/merge.json")


def test_golden_dry_run_plan(rig: Rig) -> None:
    rig.script(mr(), proj=project("ff"))
    done = rig.run("--dry-run", method=None)
    assert done.code == 0, done.err
    golden.assert_golden("merge/gitlab-dry-run", done.json, schema_id="forgectl/cli/merge.json")
