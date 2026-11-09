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

PR = "github.com/example-org/example-repo#7"
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
    fake_forges.answer("gh", endpoint_re=r"/compare/", body=recording("compare-ahead"), times=1)
    fake_forges.answer(
        "gh", endpoint_re=r"/compare/", body_text=(RECORDINGS / "pull.diff").read_text()
    )
    done = go(run_forgectl, config, "compare", PR, "--from", FROM, "--to", TO)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("compare", out)
    assert out["status"] == "ahead"


def test_compare_of_an_unknown_commit_is_unavailable_exit_0(
    run_forgectl: Run, fake_forges: FakeForges, config: Path
) -> None:
    fake_forges.answer("gh", endpoint_re=r"/compare/", status=404, body={"message": "Not Found"})
    done = go(run_forgectl, config, "compare", PR, "--from", FROM, "--to", TO)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("compare", out)
    assert (out["status"], out["diff"]) == ("unavailable", None)
