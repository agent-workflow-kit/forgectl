"""`forgectl files`, `diff` and `compare` end to end: the real CLI, the real gateway, the fake `gh`
(#13)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from .harness import schemas
from .harness.forges import FakeForges
from .harness.safety import assert_call_safe
from .test_github_diff import BASE, FROM, HEAD, PR_ENDPOINT, RECORDINGS, TO, pull, recording
from .test_github_show import TOKEN

PR = "github.com/user-7/example-repo#7"
Run = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("hosts:\n  github.com:\n    token_env: SHOW_TOKEN\n", encoding="utf-8")
    return path


def go(run: Run, config: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return run([*argv, "--json", "--config", str(config)], env={"SHOW_TOKEN": TOKEN})


def test_files_prints_the_schema_valid_list(
    run_forgectl: Run, fake_forges: FakeForges, config: Path
) -> None:
    fake_forges.answer("gh", PR_ENDPOINT, body=pull(), times=1)
    fake_forges.answer("gh", f"{PR_ENDPOINT}/files?per_page=100&page=1", body=recording("files"))
    fake_forges.answer("gh", PR_ENDPOINT, body=pull())
    done = go(run_forgectl, config, "files", PR)
    assert done.returncode == 0, done.stderr
    out: dict[str, Any] = json.loads(done.stdout)
    schemas.validate_cli_output("files", out)
    assert out["files_complete"] is True and len(out["files"]) == 6
    for call in fake_forges.calls("gh"):
        assert_call_safe(call, host="github.com")


def test_a_cut_list_exits_0_with_files_complete_false(
    run_forgectl: Run, fake_forges: FakeForges, config: Path
) -> None:
    fake_forges.answer("gh", PR_ENDPOINT, body=pull(changed_files=4156))
    fake_forges.answer("gh", f"{PR_ENDPOINT}/files?per_page=100&page=1", body=recording("files"))
    done = go(run_forgectl, config, "files", PR)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["files_complete"] is False


def test_diff_prints_the_schema_valid_object(
    run_forgectl: Run, fake_forges: FakeForges, config: Path
) -> None:
    doc = pull(changed_files=1)
    fake_forges.answer("gh", PR_ENDPOINT, body=doc, times=1)
    fake_forges.answer("gh", PR_ENDPOINT, body_text=(RECORDINGS / "pull.diff").read_text(), times=1)
    fake_forges.answer("gh", endpoint_re=r"/compare/", body=recording("merge-base"))
    fake_forges.answer("gh", PR_ENDPOINT, body=doc)
    done = go(run_forgectl, config, "diff", PR)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("diff", out)
    assert out["head_sha"] == HEAD and out["base_sha"] == BASE
    assert out["diff"].startswith("diff --git a/src/app.py")


HOSTILE_DIFF = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\tb\n+\x1b[31mred\n"


def test_diff_without_json_prints_only_the_sanitized_unified_diff(
    run_forgectl: Run, fake_forges: FakeForges, config: Path
) -> None:
    doc = pull(changed_files=1)
    fake_forges.answer("gh", PR_ENDPOINT, body=doc, times=1)
    fake_forges.answer("gh", PR_ENDPOINT, body_text=HOSTILE_DIFF, times=1)
    fake_forges.answer("gh", endpoint_re=r"/compare/", body=recording("merge-base"))
    fake_forges.answer("gh", PR_ENDPOINT, body=doc)
    done = run_forgectl(["diff", PR, "--config", str(config)], env={"SHOW_TOKEN": TOKEN})
    assert done.returncode == 0, done.stderr
    assert done.stdout == (
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\tb\n+<U+001B>[31mred\n"
    )


def test_a_406_exits_20_too_large(run_forgectl: Run, fake_forges: FakeForges, config: Path) -> None:
    doc = pull(changed_files=400)
    fake_forges.answer("gh", PR_ENDPOINT, body=doc, times=1)
    fake_forges.answer("gh", PR_ENDPOINT, status=406, body={"message": "too many files"})
    done = go(run_forgectl, config, "diff", PR)
    assert done.returncode == 20
    envelope = json.loads(done.stdout)
    schemas.validate_error_envelope(envelope)
    assert envelope["error"]["reason"] == "too_large"


def test_compare_prints_the_schema_valid_object(
    run_forgectl: Run, fake_forges: FakeForges, config: Path
) -> None:
    fake_forges.answer("gh", PR_ENDPOINT, body=pull())
    fake_forges.answer("gh", endpoint_re=r"/compare/", body=recording("compare-ahead"), times=1)
    fake_forges.answer(
        "gh", endpoint_re=r"/compare/", body_text=(RECORDINGS / "pull.diff").read_text()
    )
    done = go(run_forgectl, config, "compare", PR, "--from", FROM, "--to", TO)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("compare", out)
    assert out["status"] == "ahead"
    assert out["pr"] == "github.com/user-1/example-repo#7"


def test_compare_no_diff_leaves_the_diff_key_out_and_makes_no_diff_request(
    run_forgectl: Run, fake_forges: FakeForges, config: Path
) -> None:
    fake_forges.answer("gh", PR_ENDPOINT, body=pull())
    # only the status answer exists: a diff request would find no route and fail the run
    fake_forges.answer("gh", endpoint_re=r"/compare/", body=recording("compare-ahead"), times=1)
    done = go(run_forgectl, config, "compare", PR, "--from", FROM, "--to", TO, "--no-diff")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("compare", out)
    assert out["status"] == "ahead"
    assert "diff" not in out
    assert set(out) == {"pr", "from_sha", "to_sha", "status"}


def test_compare_default_output_still_carries_the_diff_key(
    run_forgectl: Run, fake_forges: FakeForges, config: Path
) -> None:
    fake_forges.answer("gh", PR_ENDPOINT, body=pull())
    fake_forges.answer("gh", endpoint_re=r"/compare/", body=recording("compare-ahead"), times=1)
    fake_forges.answer(
        "gh", endpoint_re=r"/compare/", body_text=(RECORDINGS / "pull.diff").read_text()
    )
    out = json.loads(go(run_forgectl, config, "compare", PR, "--from", FROM, "--to", TO).stdout)
    assert isinstance(out["diff"], str)


@pytest.mark.parametrize("status", ["identical", "ahead", "behind", "diverged", "unavailable"])
def test_compare_default_mode_always_carries_the_diff_key_for_every_status(
    run_forgectl: Run, fake_forges: FakeForges, config: Path, status: str
) -> None:
    fake_forges.answer("gh", PR_ENDPOINT, body=pull())
    to = TO
    if status == "identical":
        to = FROM  # equal shas are answered without a compare request
    elif status == "unavailable":
        fake_forges.answer("gh", endpoint_re=r"/compare/", status=404, body={"message": "no"})
    else:
        doc = recording("compare-ahead")
        doc.update(
            status=status,
            ahead_by=2 if status in ("ahead", "diverged") else 0,
            behind_by=3 if status in ("behind", "diverged") else 0,
        )
        fake_forges.answer("gh", endpoint_re=r"/compare/", body=doc, times=1)
        if status == "ahead":
            fake_forges.answer(
                "gh", endpoint_re=r"/compare/", body_text=(RECORDINGS / "pull.diff").read_text()
            )
    done = go(run_forgectl, config, "compare", PR, "--from", FROM, "--to", to)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("compare", out)
    assert out["status"] == status
    assert "diff" in out
    if status == "identical":
        assert out["diff"] == ""
    elif status == "ahead":
        assert isinstance(out["diff"], str)
    else:
        assert out["diff"] is None


def test_compare_no_diff_is_given_once_and_takes_no_value(run_forgectl: Run, config: Path) -> None:
    base = ["compare", PR, "--from", FROM, "--to", TO]
    for extra in (["--no-diff", "--no-diff"], ["--no-diff=yes"]):
        done = go(run_forgectl, config, *base, *extra)
        assert done.returncode == 2 and "Traceback" not in done.stderr
        assert "--no-diff" in done.stderr


@pytest.mark.parametrize("command", ["diff", "files", "show", "snapshot"])
def test_no_diff_belongs_to_compare_only(run_forgectl: Run, config: Path, command: str) -> None:
    done = go(run_forgectl, config, command, PR, "--no-diff")
    assert done.returncode == 2 and "Traceback" not in done.stderr
    assert "--no-diff" in done.stderr


def test_compare_of_an_unknown_commit_is_unavailable_exit_0(
    run_forgectl: Run, fake_forges: FakeForges, config: Path
) -> None:
    fake_forges.answer("gh", PR_ENDPOINT, body=pull())
    fake_forges.answer("gh", endpoint_re=r"/compare/", status=404, body={"message": "Not Found"})
    done = go(run_forgectl, config, "compare", PR, "--from", FROM, "--to", TO)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("compare", out)
    assert (out["status"], out["diff"]) == ("unavailable", None)


def _compare_doc(status: str, **extra: Any) -> dict[str, Any]:
    return {"pr": "github.com/o/r#1", "from_sha": FROM, "to_sha": TO, "status": status, **extra}


def test_the_compare_schema_takes_a_missing_diff_and_still_judges_a_present_one() -> None:
    for status in ("ahead", "behind", "diverged", "identical", "unavailable"):
        schemas.validate(_compare_doc(status), "forgectl/cli/compare.json")
    schemas.validate(_compare_doc("ahead", diff="d"), "forgectl/cli/compare.json")
    for bad in (
        _compare_doc("ahead", diff=None),
        _compare_doc("behind", diff="d"),
        _compare_doc("identical", diff="d"),
        _compare_doc("ahead", diff=3),
        _compare_doc("ahead", extra=1),
    ):
        with pytest.raises(Exception):  # noqa: B017, PT011 (jsonschema's error type)
            schemas.validate(bad, "forgectl/cli/compare.json")
