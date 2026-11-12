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

PR = "github.com/user-7/example-repo#7"


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
        "user",
        body={"login": "user-1", "id": 1001, "html_url": "https://github.com/user-1"},
        headers=SCOPES,
    )
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


def run_flagged(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]], config: Path, *flags: str
) -> subprocess.CompletedProcess[str]:
    return run_forgectl(
        ["snapshot", PR, *flags, "--json", "--config", str(config)], env={"SHOW_TOKEN": TOKEN}
    )


def _without_bodies(node: Any) -> Any:
    """The default output with every ``body`` key of the PR, reviews and comments dropped."""
    if isinstance(node, dict):
        return {k: _without_bodies(v) for k, v in node.items() if k != "body"}
    if isinstance(node, list):
        return [_without_bodies(v) for v in node]
    return node


def test_no_bodies_drops_exactly_the_body_keys_and_keeps_everything_else(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    script(fake_forges)
    full = run(run_forgectl, config, "snapshot")
    slim_done = run_flagged(run_forgectl, config, "--no-bodies")
    assert slim_done.returncode == 0, slim_done.stderr
    slim = json.loads(slim_done.stdout)
    schemas.validate_cli_output("snapshot", slim)
    assert any(r["body"] for r in full["reviews"]) or any(
        c["body"] for t in full["threads"] for c in t["comments"]
    )
    assert "body" not in slim
    assert all("body" not in r for r in slim["reviews"])
    assert all("body" not in c for t in slim["threads"] for c in t["comments"])
    # fetched_at and rate_limit are the only fields that may differ between two reads.
    for volatile in ("fetched_at", "rate_limit"):
        del full[volatile], slim[volatile]
    assert slim == _without_bodies(full)
    for kept in ("ci", "incomplete", "latest_by_reviewer", "activity", "complete"):
        assert kept in slim


def test_the_default_output_still_carries_every_body(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    script(fake_forges)
    full = run(run_forgectl, config, "snapshot")
    assert "body" in full
    assert all("body" in r for r in full["reviews"])
    assert all("body" in c for t in full["threads"] for c in t["comments"])


def test_no_bodies_is_given_once_and_takes_no_value(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    config: Path,
) -> None:
    twice = run_flagged(run_forgectl, config, "--no-bodies", "--no-bodies")
    assert twice.returncode != 0 and "Traceback" not in twice.stderr
    valued = run_flagged(run_forgectl, config, "--no-bodies=yes")
    assert valued.returncode != 0 and "Traceback" not in valued.stderr


@pytest.mark.parametrize("command", ["show", "comments", "reviews", "ci", "snapshots"])
def test_no_bodies_belongs_to_snapshot_only(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    config: Path,
    command: str,
) -> None:
    done = run_forgectl([command, PR, "--no-bodies", "--json", "--config", str(config)])
    assert done.returncode != 0 and "Traceback" not in done.stderr


def test_the_schema_takes_all_bodies_or_none_never_a_mixture(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    script(fake_forges)
    full = run(run_forgectl, config, "snapshot")
    slim = json.loads(run_flagged(run_forgectl, config, "--no-bodies").stdout)
    assert full["reviews"] and full["threads"]  # the fixture gives both parts to mix

    def bad(doc: dict[str, Any]) -> None:
        with pytest.raises(Exception):  # noqa: B017, PT011 (jsonschema's error type)
            schemas.validate_cli_output("snapshot", doc)

    no_review_body = json.loads(json.dumps(full))
    del no_review_body["reviews"][0]["body"]
    bad(no_review_body)
    no_comment_body = json.loads(json.dumps(full))
    del no_comment_body["threads"][0]["comments"][0]["body"]
    bad(no_comment_body)
    no_pr_body = json.loads(json.dumps(full))
    del no_pr_body["body"]
    bad(no_pr_body)
    review_body_back = json.loads(json.dumps(slim))
    review_body_back["reviews"][0]["body"] = "x"
    bad(review_body_back)
    comment_body_back = json.loads(json.dumps(slim))
    comment_body_back["threads"][0]["comments"][0]["body"] = "x"
    bad(comment_body_back)
