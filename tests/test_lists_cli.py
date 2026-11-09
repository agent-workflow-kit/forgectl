"""`forgectl prs`, `access` and `whoami` end to end: the real CLI, the real gateway, fake `gh`
and `glab` (#14), with partial results across hosts."""

from __future__ import annotations

import copy
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from .harness import schemas
from .harness.forges import FakeForges
from .harness.safety import assert_call_safe
from .test_github_lists import nodes_of, recording

TOKEN_A = "tok-aaaa-0001-zzzzzz"
TOKEN_B = "tok-bbbb-0002-zzzzzz"
GHE = "ghe.example.test"
Run = Callable[..., subprocess.CompletedProcess[str]]
ENV = {"TOK_A": TOKEN_A, "TOK_B": TOKEN_B}


@pytest.fixture
def one(tmp_path: Path) -> Path:
    path = tmp_path / "one.yaml"
    path.write_text("retries: 0\nhosts:\n  github.com:\n    token_env: TOK_A\n", encoding="utf-8")
    return path


@pytest.fixture
def two(tmp_path: Path) -> Path:
    path = tmp_path / "two.yaml"
    path.write_text(
        "retries: 0\n"
        "hosts:\n"
        "  github.com:\n    token_env: TOK_A\n"
        f"  {GHE}:\n    backend: github\n    token_env: TOK_B\n",
        encoding="utf-8",
    )
    return path


def prs(run: Run, config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["prs", "--json", "--config", str(config), *args], env=ENV)


def other_host_page() -> dict[str, Any]:
    """A page on the second host: one PR, newer than every PR of the recording."""
    doc = recording()
    node = copy.deepcopy(nodes_of(doc)[0])
    node["url"] = f"https://{GHE}/example-org/example-repo/pull/90"
    node["number"] = 90
    node["id"] = "PR_kwDOexample90"
    node["updatedAt"] = "2026-10-03T00:00:00Z"
    doc["data"]["search"]["nodes"] = [node]
    doc["data"]["search"]["issueCount"] = 1
    doc["data"]["rateLimit"] = {"remaining": 100, "resetAt": "2026-10-03T01:00:00Z"}
    return doc


def script_both(forges: FakeForges, *, ghe_status: int = 200) -> None:
    forges.answer("gh", "graphql", method="POST", host="github.com", body=recording())
    if ghe_status == 200:
        forges.answer("gh", "graphql", method="POST", host=GHE, body=other_host_page())
    else:
        forges.answer(
            "gh", "graphql", method="POST", host=GHE, status=ghe_status, body={"message": "x"}
        )
    forges.answer("glab", "user", status=401, body={"message": "401 Unauthorized"})


def test_prs_for_a_repo_is_schema_valid(run_forgectl: Run, fake_forges: FakeForges, one: Path):
    fake_forges.answer("gh", "graphql", method="POST", body=recording())
    done = prs(run_forgectl, one, "--repo", "github.com/example-org/example-repo")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert [i["number"] for i in out["items"]] == [31, 29, 12]
    assert out["hosts_queried"] == ["github.com"]
    (call,) = fake_forges.calls("gh")
    assert_call_safe(call, host="github.com")
    assert call.token_sent == TOKEN_A


def test_author_me_uses_the_authenticated_login_when_no_me_is_configured(
    run_forgectl: Run, fake_forges: FakeForges, one: Path
) -> None:
    fake_forges.answer(
        "gh", "user", body={"login": "user-1", "id": 1001, "html_url": "https://github.com/user-1"}
    )
    fake_forges.answer("gh", "graphql", method="POST", body=recording())
    done = prs(run_forgectl, one, "--host", "github.com", "--author", "me")
    assert done.returncode == 0, done.stderr
    queries = [c for c in fake_forges.calls("gh") if c.argv[-1] == "graphql"]
    assert len(queries) == 1


def test_a_configured_me_wins_over_the_authenticated_login(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    config = tmp_path / "me.yaml"
    config.write_text(
        "retries: 0\nme:\n  github: user-9\nhosts:\n  github.com:\n    token_env: TOK_A\n",
        encoding="utf-8",
    )
    fake_forges.answer("gh", "graphql", method="POST", stdin_re="author:user-9", body=recording())
    done = prs(run_forgectl, config, "--host", "github.com", "--author", "me")
    assert done.returncode == 0, done.stderr
    assert not [c for c in fake_forges.calls("gh") if c.argv[-1] == "user"]


def test_review_requested_from_me_is_a_direct_request_search(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    config = tmp_path / "me.yaml"
    config.write_text(
        "retries: 0\nme:\n  github: user-9\nhosts:\n  github.com:\n    token_env: TOK_A\n",
        encoding="utf-8",
    )
    fake_forges.answer(
        "gh", "graphql", method="POST", stdin_re="user-review-requested:user-9", body=recording()
    )
    done = prs(run_forgectl, config, "--host", "github.com", "--review-requested-from", "me")
    assert done.returncode == 0, done.stderr


def test_two_hosts_are_merged_newest_first(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    script_both(fake_forges)
    done = prs(run_forgectl, two, "--author", "user-1")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert [i["number"] for i in out["items"]] == [90, 31, 29, 12]
    assert out["hosts_queried"] == [GHE, "github.com"]
    assert out["errors"] == []
    assert out["rate_limit"] == {"host": GHE, "remaining": 100, "reset_at": "2026-10-03T01:00:00Z"}
    assert out["complete"] is True


def test_the_limit_is_a_total_across_hosts(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    script_both(fake_forges)
    done = prs(run_forgectl, two, "--author", "user-1", "--limit", "2")
    out = json.loads(done.stdout)
    assert [i["number"] for i in out["items"]] == [90, 31]
    assert out["complete"] is False


def test_one_failed_host_is_partial_results_with_the_rest_listed(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    script_both(fake_forges, ghe_status=502)
    done = prs(run_forgectl, two, "--author", "user-1")
    assert done.returncode == 12
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert [i["number"] for i in out["items"]] == [31, 29, 12]
    assert [(e["host"], e["code"]) for e in out["errors"]] == [(GHE, "upstream_error")]
    assert "partial_results" in done.stderr
    assert TOKEN_A not in done.stdout + done.stderr and TOKEN_B not in done.stdout + done.stderr


def test_all_hosts_failing_is_partial_results_with_no_items(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", status=502, body={"message": "x"})
    fake_forges.answer("glab", "user", status=401, body={"message": "401"})
    done = prs(run_forgectl, two, "--author", "user-1")
    assert done.returncode == 12
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert out["items"] == []
    assert sorted(e["host"] for e in out["errors"]) == [GHE, "github.com"]


def test_a_failure_message_never_holds_a_token(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", host="github.com", body=recording())
    fake_forges.answer(
        "gh",
        "graphql",
        method="POST",
        host=GHE,
        status=401,
        body={"message": f"Bad credentials {TOKEN_B}\nforged: line"},
    )
    fake_forges.answer("glab", "user", status=401, body={"message": "401"})
    done = prs(run_forgectl, two, "--author", "user-1")
    assert done.returncode == 12
    assert TOKEN_B not in done.stdout and TOKEN_B not in done.stderr
    out = json.loads(done.stdout)
    assert all("\n" not in e["message"] for e in out["errors"])


def test_a_single_target_failure_is_the_ordinary_envelope(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    fake_forges.answer(
        "gh", "graphql", method="POST", status=401, body={"message": "Bad credentials"}
    )
    done = prs(run_forgectl, two, "--host", GHE, "--author", "user-1")
    assert done.returncode == 12
    envelope = json.loads(done.stdout)
    schemas.validate_error_envelope(envelope)
    assert envelope["error"]["code"] == "auth_required"


def test_an_undeclared_builtin_without_a_login_is_skipped_not_an_error(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    config = tmp_path / "ghe.yaml"
    config.write_text(
        f"retries: 0\nhosts:\n  {GHE}:\n    backend: github\n    token_env: TOK_B\n",
        encoding="utf-8",
    )
    fake_forges.answer("gh", "user", host="github.com", status=401, body={"message": "x"})
    fake_forges.answer("glab", "user", status=401, body={"message": "x"})
    fake_forges.answer("gh", "graphql", method="POST", host=GHE, body=other_host_page())
    done = prs(run_forgectl, config, "--author", "user-1")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["hosts_queried"] == [GHE]
    assert out["errors"] == []


def test_a_login_that_does_not_fit_a_host_backend_is_usage_before_any_call(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    done = prs(run_forgectl, two, "--author", "a.b")  # valid on GitLab, not on GitHub
    assert done.returncode == 2
    assert fake_forges.calls() == []


@pytest.mark.parametrize(
    "value", ["x repo:other/y", "x:y", 'x"', "x\ny", "a b", "x OR y", "-x", "x,y", ""]
)
def test_injection_shaped_logins_never_reach_a_search(
    run_forgectl: Run, fake_forges: FakeForges, one: Path, value: str
) -> None:
    for flag in ("--author", "--review-requested-from"):
        done = prs(run_forgectl, one, "--host", "github.com", flag, value)
        assert done.returncode == 2, (flag, value, done.stderr)
    assert fake_forges.calls() == []


def test_an_updated_since_duration_becomes_a_utc_time(
    run_forgectl: Run, fake_forges: FakeForges, one: Path
) -> None:
    fake_forges.answer(
        "gh",
        "graphql",
        method="POST",
        stdin_re=r"updated:>=20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ",
        body=recording(),
    )
    done = prs(
        run_forgectl,
        one,
        "--repo",
        "github.com/example-org/example-repo",
        "--updated-since",
        "3650d",
    )
    assert done.returncode == 0, done.stderr


def test_an_updated_since_with_an_offset_is_sent_in_utc(
    run_forgectl: Run, fake_forges: FakeForges, one: Path
) -> None:
    fake_forges.answer(
        "gh", "graphql", method="POST", stdin_re="updated:>=2020-01-01T00:00:00Z", body=recording()
    )
    done = prs(
        run_forgectl,
        one,
        "--repo",
        "github.com/example-org/example-repo",
        "--updated-since",
        "2020-01-01T02:00:00+02:00",
    )
    assert done.returncode == 0, done.stderr


# -- access ------------------------------------------------------------------------------------


def test_access_prints_the_schema_valid_answer(
    run_forgectl: Run, fake_forges: FakeForges, one: Path
) -> None:
    fake_forges.answer(
        "gh",
        "repos/user-5/example-repo",
        body=json.loads((Path(__file__).parent / "recordings/github/lists/repo.json").read_text()),
    )
    fake_forges.answer(
        "gh",
        "repos/user-5/example-repo/collaborators/user-1/permission",
        body={"permission": "write", "role_name": "write", "user": {"login": "user-1"}},
    )
    done = run_forgectl(
        ["access", "github.com/user-5/example-repo", "user-1", "--json", "--config", str(one)],
        env=ENV,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("access", out)
    assert out["write_access"] is True
    for call in fake_forges.calls("gh"):
        assert_call_safe(call, host="github.com")


def test_access_for_me_is_the_configured_login(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    config = tmp_path / "me.yaml"
    config.write_text(
        "retries: 0\nme:\n  github: user-9\nhosts:\n  github.com:\n    token_env: TOK_A\n",
        encoding="utf-8",
    )
    fake_forges.answer(
        "gh",
        "repos/user-5/example-repo",
        body=json.loads((Path(__file__).parent / "recordings/github/lists/repo.json").read_text()),
    )
    fake_forges.answer(
        "gh",
        "repos/user-5/example-repo/collaborators/user-9/permission",
        status=403,
        body={"message": "Must have push access to view collaborator permission"},
    )
    done = run_forgectl(
        ["access", "github.com/user-5/example-repo", "me", "--json", "--config", str(config)],
        env=ENV,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("access", out)
    assert (out["write_access"], out["reason"], out["user"]) == (
        None,
        "no_permission_to_read",
        "user-9",
    )


def test_access_to_a_missing_repo_is_exit_10(
    run_forgectl: Run, fake_forges: FakeForges, one: Path
) -> None:
    fake_forges.answer("gh", "repos/user-5/example-repo", status=404, body={"message": "Not Found"})
    done = run_forgectl(
        ["access", "github.com/user-5/example-repo", "user-1", "--json", "--config", str(one)],
        env=ENV,
    )
    assert done.returncode == 10
    schemas.validate_error_envelope(json.loads(done.stdout))


# -- whoami ------------------------------------------------------------------------------------


def whoami(run: Run, config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["whoami", "--json", "--config", str(config), *args], env=ENV)


def user_body(login: str, host: str) -> dict[str, Any]:
    return {"login": login, "id": 1001, "html_url": f"https://{host}/{login}"}


def test_whoami_lists_every_host_and_never_a_token(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    fake_forges.answer(
        "gh",
        "user",
        host="github.com",
        body=user_body("user-1", "github.com"),
        headers={"x-oauth-scopes": "repo, read:org"},
    )
    fake_forges.answer("gh", "user", host=GHE, body=user_body("user-2", GHE))
    fake_forges.answer("glab", "user", status=401, body={"message": "401"})
    done = whoami(run_forgectl, two)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("whoami", out)
    by_host = {h["host"]: h for h in out["hosts"]}
    assert set(by_host) == {"github.com", GHE, "gitlab.com"}
    assert by_host["github.com"]["scopes"] == ["repo", "read:org"]
    assert by_host["github.com"]["token_source"] == "token_env"
    assert by_host["gitlab.com"]["token_source"] == "none"
    assert TOKEN_A not in done.stdout + done.stderr
    assert TOKEN_B not in done.stdout + done.stderr


def test_whoami_with_a_failing_host_is_partial_results(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    fake_forges.answer("gh", "user", host="github.com", body=user_body("user-1", "github.com"))
    fake_forges.answer(
        "gh", "user", host=GHE, status=401, body={"message": f"Bad credentials {TOKEN_B}"}
    )
    fake_forges.answer("glab", "user", status=401, body={"message": "401"})
    done = whoami(run_forgectl, two)
    assert done.returncode == 12
    out = json.loads(done.stdout)
    schemas.validate_cli_output("whoami", out)
    assert [(e["host"], e["code"]) for e in out["errors"]] == [(GHE, "auth_required")]
    assert TOKEN_B not in done.stdout + done.stderr


def test_whoami_host_limits_to_one_host(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    fake_forges.answer("gh", "user", host=GHE, body=user_body("user-2", GHE))
    done = whoami(run_forgectl, two, "--host", GHE)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert [h["host"] for h in out["hosts"]] == [GHE]
    assert out["hosts"][0]["me_matches_login"] is True
