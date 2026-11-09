"""`forgectl snapshot`, `comments`, `reviews` and `ci` end to end (#12): the real CLI, the real
gateway, the fake `gh`."""

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
from .test_github_snapshot import SCOPES, pr_of, rec, with_identity

PR = "github.com/example-org/example-repo#7"


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("hosts:\n  github.com:\n    token_env: SHOW_TOKEN\n", encoding="utf-8")
    return path


def script(forges: FakeForges) -> None:
    for op, doc in {
        "ForgectlShow": recording(),
        "ForgectlParts": rec("parts"),
        "ForgectlRequests": rec("requests"),
        "ForgectlPush": rec("push"),
        "ForgectlReviewsPage": with_identity(
            {"data": {"node": {"reviews": pr_of(rec("parts"))["reviews"]}}}
        ),
    }.items():
        forges.answer("gh", "graphql", method="POST", stdin_re=f"query {op}\\b", body=doc)
    forges.answer(
        "gh",
        endpoint_re=r"pulls/7/requested_reviewers",
        method="GET",
        body=rec("requested-reviewers"),
        headers=SCOPES,
    )


def run(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]], config: Path, command: str
) -> dict[str, Any]:
    done = run_forgectl([command, PR, "--json", "--config", str(config)], env={"SHOW_TOKEN": TOKEN})
    assert done.returncode == 0, done.stderr
    out: dict[str, Any] = json.loads(done.stdout)
    schemas.validate_cli_output(command, out)
    return out


@pytest.mark.parametrize("command", ["snapshot", "comments", "reviews", "ci"])
def test_each_command_prints_schema_valid_json(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
    command: str,
) -> None:
    script(fake_forges)
    out = run(run_forgectl, config, command)
    assert out["pr"] == PR
    assert out["head_sha" if command != "snapshot" else "head"] is not None
    for call in fake_forges.calls("gh"):
        assert_call_safe(call, host="github.com")
        assert call.token_sent == TOKEN


def test_snapshot_end_to_end_names_the_head_and_the_parts(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    script(fake_forges)
    out = run(run_forgectl, config, "snapshot")
    assert out["head"]["sha"] == HEAD
    assert out["complete"] is True and out["incomplete"] == []
    assert {t["id"][:5] for t in out["threads"]} == {"gh:rt", "gh:ic"}
    assert out["rate_limit"]["host"] == "github.com"
