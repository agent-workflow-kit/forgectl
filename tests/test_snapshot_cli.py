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
    """The default output with every review and comment ``body`` key dropped, the PR's kept."""

    def strip(n: Any) -> Any:
        if isinstance(n, dict):
            return {k: strip(v) for k, v in n.items() if k != "body"}
        if isinstance(n, list):
            return [strip(v) for v in n]
        return n

    return {**node, "reviews": strip(node["reviews"]), "threads": strip(node["threads"])}


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
    assert slim["body"] == full["body"]
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
    assert twice.returncode == 2 and "Traceback" not in twice.stderr
    assert "usage" in twice.stderr.lower() and "--no-bodies" in twice.stderr
    valued = run_flagged(run_forgectl, config, "--no-bodies=yes")
    assert valued.returncode == 2 and "Traceback" not in valued.stderr
    assert "usage" in valued.stderr.lower() and "--no-bodies" in valued.stderr


@pytest.mark.parametrize("command", ["show", "comments", "reviews", "ci", "snapshots"])
def test_no_bodies_belongs_to_snapshot_only(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    config: Path,
    command: str,
) -> None:
    done = run_forgectl([command, PR, "--no-bodies", "--json", "--config", str(config)])
    assert done.returncode == 2 and "Traceback" not in done.stderr
    assert "usage" in done.stderr.lower() and "--no-bodies" in done.stderr


def _bad(schema: str, doc: dict[str, Any]) -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011 (jsonschema's error type)
        schemas.validate(doc, f"forgectl/cli/{schema}.json")


def _copy(doc: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(json.dumps(doc))
    return out


def test_the_schema_takes_every_review_and_comment_with_a_body_or_none(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    script(fake_forges)
    full = run(run_forgectl, config, "snapshot")
    slim = json.loads(run_flagged(run_forgectl, config, "--no-bodies").stdout)
    assert len(full["reviews"]) >= 1 and full["threads"]  # the fixture gives both parts to mix
    assert len(full["threads"][0]["comments"]) >= 1

    # mixed within the reviews, within the comments, and across the two
    some_review = _copy(full)
    del some_review["reviews"][0]["body"]
    _bad("snapshot", some_review)
    some_comment = _copy(full)
    del some_comment["threads"][0]["comments"][0]["body"]
    _bad("snapshot", some_comment)
    slim_review_full_comment = _copy(full)
    slim_review_full_comment["reviews"] = _copy(slim)["reviews"]
    _bad("snapshot", slim_review_full_comment)
    full_review_slim_comment = _copy(slim)
    full_review_slim_comment["reviews"] = _copy(full)["reviews"]
    _bad("snapshot", full_review_slim_comment)
    review_body_back = _copy(slim)
    review_body_back["reviews"][0]["body"] = "x"
    _bad("snapshot", review_body_back)
    comment_body_back = _copy(slim)
    comment_body_back["threads"][0]["comments"][0]["body"] = "x"
    _bad("snapshot", comment_body_back)
    if len(full["reviews"]) > 1:
        one_of_many = _copy(slim)
        one_of_many["reviews"][0] = _copy(full)["reviews"][0]
        _bad("snapshot", one_of_many)


@pytest.mark.parametrize("value", [None, ""])
def test_a_null_or_empty_review_or_comment_body_is_not_a_slim_item(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
    value: str | None,
) -> None:
    script(fake_forges)
    slim = json.loads(run_flagged(run_forgectl, config, "--no-bodies").stdout)
    review = _copy(slim)
    review["reviews"][0]["body"] = value
    comment = _copy(slim)
    comment["threads"][0]["comments"][0]["body"] = value
    _bad("snapshot", review)
    _bad("snapshot", comment)
    if value is None:  # a null body is no full item either (the body is a string)
        full = run(run_forgectl, config, "snapshot")
        full["reviews"][0]["body"] = None
        _bad("snapshot", full)


def test_the_pr_body_stays_required_with_or_without_the_flag(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    script(fake_forges)
    full = run(run_forgectl, config, "snapshot")
    slim = json.loads(run_flagged(run_forgectl, config, "--no-bodies").stdout)
    for doc in (full, slim):
        gone = _copy(doc)
        del gone["body"]
        _bad("snapshot", gone)


def test_snapshots_accepts_only_the_full_item_shape(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    script(fake_forges)
    full = run(run_forgectl, config, "snapshot")
    slim = json.loads(run_flagged(run_forgectl, config, "--no-bodies").stdout)
    wrap = {
        "items": [full],
        "failed": [],
        "complete": True,
        "errors": [],
        "hosts_queried": ["github.com"],
        "fallback": [],
        "rate_limit": full["rate_limit"],
    }
    schemas.validate(wrap, "forgectl/cli/snapshots.json")
    _bad("snapshots", {**wrap, "items": [slim]})


def test_thread_no_body_is_the_defs_thread_without_the_comment_bodies() -> None:
    defs = schemas._documents()["forgectl/defs.json"]["$defs"]
    slim = schemas._documents()["forgectl/cli/snapshot.json"]["$defs"]
    thread, thread_slim = defs["thread"], slim["thread_no_body"]
    for key in ("required", "allOf", "additionalProperties", "type"):
        assert thread_slim[key] == thread[key], key
    # the copy lives in another file, so its local refs point at defs.json
    own = json.dumps({k: v for k, v in thread["properties"].items() if k != "comments"})
    copy = {k: v for k, v in thread_slim["properties"].items() if k != "comments"}
    assert json.dumps(copy).replace("../defs.json#/", "#/") == own
    assert set(thread_slim["properties"]) == set(thread["properties"])
    comment, comment_slim = defs["comment"], slim["comment_no_body"]
    assert comment_slim["required"] == [k for k in comment["required"] if k != "body"]
    assert set(comment_slim["properties"]) == set(comment["properties"]) - {"body"}
