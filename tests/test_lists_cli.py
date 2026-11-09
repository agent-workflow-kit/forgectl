"""`forgectl prs`, `access` and `whoami` end to end: the real CLI, the real gateway, fake `gh`
and `glab` (#14), with partial results across hosts."""

from __future__ import annotations

import copy
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from forgectl.errors import ForgectlError
from forgectl.fanout import check_me_spelling, resolve_since
from forgectl.gateway import RateReading, rate_reading

from .harness import golden, schemas
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


def gh_login() -> None:
    """A stored gh login for github.com in the isolated config dir the fakes read."""
    FakeForges.stored_login(Path(os.environ["GH_CONFIG_DIR"]), "gh", "github.com", "stored-tok-5")


def prs(run: Run, config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["prs", "--json", "--config", str(config), *args], env=ENV)


def other_host_page() -> dict[str, Any]:
    """A page on the second host: one PR, newer than every PR of the recording."""
    doc = recording()
    node = copy.deepcopy(nodes_of(doc)[0])
    node["url"] = f"https://{GHE}/user-7/example-repo/pull/90"
    node["number"] = 90
    node["id"] = "PR_kwDOexample90"
    node["updatedAt"] = "2026-10-03T00:00:00Z"
    doc["data"]["search"]["nodes"] = [node]
    doc["data"]["search"]["issueCount"] = 1
    return doc


def rate(remaining: int, reset: int = 1790989200) -> dict[str, str]:
    """Rate-limit headers; the default reset is 2026-10-03T01:00:00Z."""
    return {"x-ratelimit-remaining": str(remaining), "x-ratelimit-reset": str(reset)}


def script_both(
    forges: FakeForges, *, ghe_status: int = 200, ghe_headers: dict[str, str] | None = None
) -> None:
    forges.answer(
        "gh", "graphql", method="POST", host="github.com", body=recording(), headers=rate(4987)
    )
    if ghe_status == 200:
        forges.answer(
            "gh", "graphql", method="POST", host=GHE, body=other_host_page(), headers=rate(100)
        )
    else:
        forges.answer(
            "gh",
            "graphql",
            method="POST",
            host=GHE,
            status=ghe_status,
            body={"message": "x"},
            headers=ghe_headers,
        )
    forges.answer("glab", "user", status=401, body={"message": "401 Unauthorized"})


def test_prs_for_a_repo_is_schema_valid(run_forgectl: Run, fake_forges: FakeForges, one: Path):
    fake_forges.answer("gh", "graphql", method="POST", body=recording())
    done = prs(run_forgectl, one, "--repo", "github.com/user-7/example-repo")
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
    assert out["complete"] is False  # the failed host's items are missing
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
    assert out["complete"] is False
    assert sorted(e["host"] for e in out["errors"]) == [GHE, "github.com"]


def test_a_rate_limited_failing_host_reports_its_remaining_zero(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    headers = {**rate(0, 1790992800), "retry-after": "60"}
    script_both(fake_forges, ghe_status=403, ghe_headers=headers)
    done = prs(run_forgectl, two, "--author", "user-1")
    assert done.returncode == 12
    out = json.loads(done.stdout)
    assert [(e["host"], e["code"]) for e in out["errors"]] == [(GHE, "rate_limited")]
    assert out["rate_limit"] == {"host": GHE, "remaining": 0, "reset_at": "2026-10-03T02:00:00Z"}


def test_an_identity_read_counts_for_the_rate_limit(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    config = tmp_path / "ghe.yaml"
    config.write_text(
        f"retries: 0\nhosts:\n  {GHE}:\n    backend: github\n    token_env: TOK_B\n",
        encoding="utf-8",
    )
    gh_login()
    fake_forges.answer(
        "gh", "user", host="github.com", status=401, body={"message": "x"}, headers=rate(2)
    )
    fake_forges.answer("glab", "user", status=401, body={"message": "x"})
    fake_forges.answer(
        "gh", "graphql", method="POST", host=GHE, body=other_host_page(), headers=rate(100)
    )
    done = prs(run_forgectl, config, "--author", "user-1")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["hosts_queried"] == [GHE]
    assert out["rate_limit"]["host"] == "github.com" and out["rate_limit"]["remaining"] == 2


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


def test_an_undeclared_builtin_with_no_binary_is_skipped_and_not_queried(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    config = tmp_path / "noglab.yaml"
    config.write_text(
        "retries: 0\nglab_bin: glab-absent\nhosts:\n  github.com:\n    token_env: TOK_A\n",
        encoding="utf-8",
    )
    fake_forges.answer("gh", "graphql", method="POST", body=recording())
    done = prs(run_forgectl, config, "--author", "user-1")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["hosts_queried"] == ["github.com"]
    assert out["errors"] == []
    assert fake_forges.calls("glab") == []


def test_a_bot_login_is_not_refused_for_an_undeclared_gitlab_that_is_skipped(
    run_forgectl: Run, fake_forges: FakeForges, one: Path
) -> None:
    fake_forges.answer("glab", "user", status=401, body={"message": "401"})
    fake_forges.answer(
        "gh", "graphql", method="POST", stdin_re="author:app/user-3", body=recording()
    )
    done = prs(run_forgectl, one, "--author", "user-3[bot]")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["hosts_queried"] == ["github.com"]


def test_a_gitlab_only_login_with_no_host_in_scope_is_an_empty_complete_list(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    config = tmp_path / "nogh.yaml"
    config.write_text("retries: 0\ngh_bin: gh-absent\n", encoding="utf-8")
    fake_forges.answer("glab", "user", status=401, body={"message": "401"})
    done = prs(run_forgectl, config, "--author", "user.1")  # GitLab grammar only
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert out == {
        "items": [],
        "complete": True,
        "errors": [],
        "hosts_queried": [],
        "rate_limit": {"host": None, "remaining": None, "reset_at": None},
    }


def test_a_login_that_does_not_fit_an_undeclared_builtin_in_scope_is_usage(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    config = tmp_path / "none.yaml"
    config.write_text("retries: 0\n", encoding="utf-8")
    gh_login()
    fake_forges.answer(
        "gh", "user", body={"login": "user-1", "id": 1001, "html_url": "https://github.com/user-1"}
    )
    fake_forges.answer("glab", "user", status=401, body={"message": "401"})
    done = prs(run_forgectl, config, "--author", "user.1")  # github.com is in scope
    assert done.returncode == 2
    assert not [c for c in fake_forges.calls("gh") if c.argv[-1] == "graphql"]


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
        stdin_re=r"updated:>=20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00",
        body=recording(),
    )
    done = prs(
        run_forgectl,
        one,
        "--repo",
        "github.com/user-7/example-repo",
        "--updated-since",
        "3650d",
    )
    assert done.returncode == 0, done.stderr


def test_an_updated_since_with_an_offset_is_sent_in_utc(
    run_forgectl: Run, fake_forges: FakeForges, one: Path
) -> None:
    fake_forges.answer(
        "gh",
        "graphql",
        method="POST",
        stdin_re=r"updated:>=2020-01-01T00:00:00\+00:00",
        body=recording(),
    )
    done = prs(
        run_forgectl,
        one,
        "--repo",
        "github.com/user-7/example-repo",
        "--updated-since",
        "2020-01-01T02:00:00+02:00",
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0999-06-01T00:00:00Z", "0999-06-01T00:00:00Z"),  # the year is zero-padded
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z"),
        ("2020-01-01T02:00:00+02:00", "2020-01-01T00:00:00Z"),
    ],
)
def test_resolve_since_is_utc_with_a_four_digit_year(value: str, expected: str) -> None:
    assert resolve_since(value) == expected


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_a_time_outside_the_utc_years_is_usage(value: str) -> None:
    with pytest.raises(ForgectlError) as caught:
        resolve_since(value)
    assert caught.value.code == "usage"
    assert "outside the years" in caught.value.message


def test_an_out_of_range_updated_since_is_exit_2_before_any_call(
    run_forgectl: Run, fake_forges: FakeForges, one: Path
) -> None:
    done = prs(
        run_forgectl,
        one,
        "--repo",
        "github.com/user-7/example-repo",
        "--updated-since",
        "9999-12-31T23:59:59-01:00",
    )
    assert done.returncode == 2, done.stderr
    assert "outside the years" in done.stderr
    assert fake_forges.calls() == []


@pytest.mark.parametrize("value", ["ME", "Me", "mE"])
def test_another_case_of_me_is_refused(value: str) -> None:
    with pytest.raises(ForgectlError) as caught:
        check_me_spelling(value, "--author")
    assert caught.value.code == "usage"
    assert "write it as `me`" in caught.value.message


@pytest.mark.parametrize("flag", ["--author", "--review-requested-from"])
def test_another_case_of_me_is_exit_2_before_any_call(
    run_forgectl: Run, fake_forges: FakeForges, one: Path, flag: str
) -> None:
    done = prs(run_forgectl, one, "--host", "github.com", flag, "ME")
    assert done.returncode == 2, done.stderr
    assert fake_forges.calls() == []


def retrying(tmp_path: Path, extra: str = "") -> Path:
    config = tmp_path / "retry.yaml"
    config.write_text(
        f"retries: 1\n{extra}hosts:\n  github.com:\n    token_env: TOK_A\n", encoding="utf-8"
    )
    return config


def test_author_me_retries_a_failed_identity_read(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    fake_forges.answer("gh", "user", times=1, status=502, body={"message": "bad"})
    fake_forges.answer(
        "gh", "user", body={"login": "user-1", "id": 1001, "html_url": "https://github.com/user-1"}
    )
    fake_forges.answer("gh", "graphql", method="POST", stdin_re="author:user-1", body=recording())
    done = prs(run_forgectl, retrying(tmp_path), "--host", "github.com", "--author", "me")
    assert done.returncode == 0, done.stderr
    assert len([c for c in fake_forges.calls("gh") if c.argv[-1] == "user"]) == 2


def test_the_scope_probe_retries_a_failed_identity_read(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    config = tmp_path / "ghe.yaml"
    config.write_text(
        f"retries: 1\nhosts:\n  {GHE}:\n    backend: github\n    token_env: TOK_B\n",
        encoding="utf-8",
    )
    gh_login()
    fake_forges.answer("gh", "user", host="github.com", times=1, status=502, body={"message": "x"})
    fake_forges.answer(
        "gh",
        "user",
        host="github.com",
        body={"login": "user-1", "id": 1001, "html_url": "https://github.com/user-1"},
    )
    fake_forges.answer("glab", "user", status=401, body={"message": "x"})
    fake_forges.answer("gh", "graphql", method="POST", host="github.com", body=recording())
    fake_forges.answer("gh", "graphql", method="POST", host=GHE, body=other_host_page())
    done = prs(run_forgectl, config, "--author", "user-1")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["hosts_queried"] == [GHE, "github.com"]
    assert out["errors"] == []


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


@pytest.mark.parametrize("value", ["ME", "Me"])
def test_access_refuses_another_case_of_me_before_any_call(
    run_forgectl: Run, fake_forges: FakeForges, one: Path, value: str
) -> None:
    done = run_forgectl(
        ["access", "github.com/user-5/example-repo", value, "--json", "--config", str(one)],
        env=ENV,
    )
    assert done.returncode == 2, done.stderr
    assert fake_forges.calls() == []


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


def test_golden_whoami(run_forgectl: Run, fake_forges: FakeForges, two: Path) -> None:
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
    golden.assert_golden("whoami/github", out, schema_id="forgectl/cli/whoami.json")


def test_whoami_retries_a_failed_identity_read(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    fake_forges.answer("gh", "user", times=1, status=502, body={"message": "bad"})
    fake_forges.answer("gh", "user", body=user_body("user-1", "github.com"))
    done = whoami(run_forgectl, retrying(tmp_path), "--host", "github.com")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["hosts"][0]["login"] == "user-1"


def test_whoami_warns_when_me_is_not_the_authenticated_login(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    config = retrying(tmp_path, "me:\n  github: user-9\n")
    fake_forges.answer("gh", "user", body=user_body("user-1", "github.com"))
    done = whoami(run_forgectl, config, "--host", "github.com")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["hosts"][0]["me_matches_login"] is False
    warnings = [line for line in done.stderr.splitlines() if line.startswith("forgectl: warning:")]
    assert len(warnings) == 1
    assert "github.com" in warnings[0] and "user-9" in warnings[0] and "user-1" in warnings[0]


def test_whoami_does_not_warn_when_me_matches(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    fake_forges.answer("gh", "user", host=GHE, body=user_body("user-2", GHE))
    done = whoami(run_forgectl, two, "--host", GHE)
    assert "warning" not in done.stderr


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (rate(5), RateReading("h.test", 5, "2026-10-03T01:00:00Z")),
        (
            {"ratelimit-remaining": "9", "ratelimit-reset": "1790989200"},
            RateReading("h.test", 9, "2026-10-03T01:00:00Z"),
        ),
        ({"x-ratelimit-remaining": "0"}, RateReading("h.test", 0, None)),
        (
            {"x-ratelimit-remaining": "3", "x-ratelimit-reset": "9" * 12},
            RateReading("h.test", 3, None),
        ),
        ({"x-ratelimit-remaining": "-1"}, None),
        ({"x-ratelimit-remaining": "lots"}, None),
        ({}, None),
    ],
)
def test_a_rate_reading_comes_from_the_headers(
    headers: dict[str, str], expected: RateReading | None
) -> None:
    assert rate_reading("h.test", headers) == expected
