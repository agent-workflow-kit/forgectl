"""`forgectl show` end to end: the real CLI, the real gateway, the fake `gh` (#11)."""

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
from .test_github_show import HEAD, TOKEN, recording

PR = "github.com/user-7/example-repo#7"


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("hosts:\n  github.com:\n    token_env: SHOW_TOKEN\n", encoding="utf-8")
    return path


def show(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]], config: Path, *extra: str
) -> subprocess.CompletedProcess[str]:
    return run_forgectl(
        ["show", PR, "--json", "--config", str(config), *extra], env={"SHOW_TOKEN": TOKEN}
    )


def test_show_prints_the_schema_valid_pr(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", body=recording())
    done = show(run_forgectl, config)
    assert done.returncode == 0, done.stderr
    out: dict[str, Any] = json.loads(done.stdout)
    schemas.validate_cli_output("show", out)
    assert out["pr"] == PR
    assert out["head"]["sha"] == HEAD
    assert out["blocked_by"] == []
    (call,) = fake_forges.calls("gh")
    assert_call_safe(call, host="github.com")
    assert call.argv[-1] == "graphql"
    assert call.token_sent == TOKEN


def test_a_not_found_is_exit_10_with_the_envelope(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    body = {
        "data": {"repository": {"pullRequest": None}},
        "errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}],
    }
    fake_forges.answer("gh", "graphql", method="POST", body=body)
    done = show(run_forgectl, config)
    assert done.returncode == 10
    envelope = json.loads(done.stdout)
    schemas.validate_error_envelope(envelope)
    assert envelope["error"]["code"] == "not_found"


def test_a_5xx_is_retried_by_the_real_cli_then_succeeds(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", status=502, body={"message": "bad"}, times=1)
    fake_forges.answer("gh", "graphql", method="POST", body=recording())
    done = show(run_forgectl, config)
    assert done.returncode == 0, done.stderr
    assert len(fake_forges.calls("gh")) == 2


def test_retries_zero_fails_on_the_first_5xx(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", status=502, body={"message": "bad"})
    done = run_forgectl(
        ["show", PR, "--json", "--config", str(config)],
        env={"SHOW_TOKEN": TOKEN, "FORGECTL_RETRIES": "0"},
    )
    assert done.returncode == 12
    assert json.loads(done.stdout)["error"]["code"] == "upstream_error"
    assert len(fake_forges.calls("gh")) == 1


def test_a_hostile_title_is_one_marked_line_in_the_output(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    doc = recording()
    doc["data"]["repository"]["pullRequest"]["title"] = "ok\nforged: yes‮"
    fake_forges.answer("gh", "graphql", method="POST", body=doc)
    done = show(run_forgectl, config)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["title"] == "ok<U+000A>forged: yes<U+202E>"
    assert "‮" not in done.stdout
