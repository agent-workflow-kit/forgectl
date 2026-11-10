"""`forgectl prs`, `access` and `whoami` for GitLab end to end (#19): the real CLI and gateway with
the fake `glab` of tests/fakebin (real child processes, safety rules on every call), including
`me`, self-managed hosts with a port and an `api_prefix`, and partial results across both
backends."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from .gitlab_support import TOKEN
from .harness import golden, schemas
from .harness.forges import FakeForges
from .harness.safety import assert_call_safe
from .test_github_lists import recording as gh_search
from .test_gitlab_lists import lists, mrs

Run = Callable[..., subprocess.CompletedProcess[str]]
GH_TOKEN = "tok-gh-0001-zzzzzz"
SELF = "git.example.test:8443"
ENV = {"GL_TOKEN": TOKEN, "GH_TOKEN": GH_TOKEN}
ROUTE = "projects/user-9%2Fproj/merge_requests"


def config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "retries: 0\n"
        f"{extra}"
        "hosts:\n"
        "  gitlab.com:\n    token_env: GL_TOKEN\n"
        "  github.com:\n    token_env: GH_TOKEN\n"
        f"  {SELF}:\n    backend: gitlab\n    api_prefix: /gl\n    token_env: GL_TOKEN\n",
        encoding="utf-8",
    )
    return path


def route(forges: FakeForges, path: str, body: Any, *, status: int = 200, **kwargs: Any) -> None:
    forges.answer(
        "glab",
        endpoint_re=rf"^{re.escape(path)}(\?|$)",
        body=body,
        status=status,
        headers=kwargs.get("headers", {"X-Next-Page": ""}),
    )


def items_for(host: str, prefix: str) -> list[dict[str, Any]]:
    out = mrs()
    for item in out:
        item["web_url"] = item["web_url"].replace("https://gitlab.com", f"https://{host}{prefix}")
    return out


def script(forges: FakeForges, host: str = "gitlab.com", prefix: str = "") -> None:
    route(forges, ROUTE, items_for(host, prefix))
    project = lists("project")
    project["web_url"] = f"https://{host}{prefix}/user-9/proj"
    route(forges, "projects/user-9%2Fproj", project)
    source = lists("source_project")
    source["web_url"] = f"https://{host}{prefix}/user-8/proj"
    route(forges, "projects/202", source)
    route(forges, "projects/404", {"message": "404 Project Not Found"}, status=404)
    route(forges, "version", {"version": "17.5.1"})


def prs(run: Run, path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["prs", "--json", "--config", str(path), *args], env=ENV)


@pytest.mark.parametrize(("host", "prefix"), [("gitlab.com", ""), (SELF, "/gl")])
def test_prs_of_a_gitlab_repo_through_the_cli(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path, host: str, prefix: str
) -> None:
    script(fake_forges, host, prefix)
    done = prs(run_forgectl, config(tmp_path), "--repo", f"{host}/user-9/proj", "--state", "all")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert [i["number"] for i in out["items"]] == [31, 29, 12, 5]
    assert out["hosts_queried"] == [host] and out["complete"] is True
    heads = {i["number"]: i["head"] for i in out["items"]}
    assert heads[29]["is_fork"] is True and heads[29]["repo"] == f"{host}/user-8/proj"
    assert heads[12] == {"is_fork": True, "ref": "feature/c12", "repo": None, "sha": "c3" * 20}
    assert out["items"][0]["url"] == f"https://{host}{prefix}/user-9/proj/-/merge_requests/31"
    assert TOKEN not in done.stdout + done.stderr
    assert fake_forges.calls("gh") == []
    calls = fake_forges.calls("glab")
    assert calls
    gitlab_host = None if host == "gitlab.com" else host + prefix
    for call in calls:
        assert_call_safe(call, host=host, mode="token", gitlab_host=gitlab_host)
        assert call.token_sent == TOKEN


def test_the_cli_prs_reports_the_host_rate_limit(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    script(fake_forges)
    done = prs(run_forgectl, config(tmp_path), "--repo", "gitlab.com/user-9/proj", "--state", "all")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert out["rate_limit"]["host"] == "gitlab.com"


def test_author_me_is_the_authenticated_gitlab_login(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    route(
        fake_forges,
        "user",
        {"id": 11, "username": "user-1", "web_url": "https://gitlab.com/user-1"},
    )
    route(fake_forges, "merge_requests", [])
    done = prs(run_forgectl, config(tmp_path), "--host", "gitlab.com", "--author", "me")
    assert done.returncode == 0, done.stderr
    lists_calls = [
        c for c in fake_forges.calls("glab") if re.match(r"merge_requests\?", c.argv[-1])
    ]
    (call,) = lists_calls
    assert "author_username=user-1" in call.argv[-1] and "scope=all" in call.argv[-1]


def test_review_requested_from_me_uses_the_configured_gitlab_me(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    route(fake_forges, "merge_requests", [])
    path = config(tmp_path, "me:\n  gitlab: user-5\n")
    done = prs(run_forgectl, path, "--host", "gitlab.com", "--review-requested-from", "me")
    assert done.returncode == 0, done.stderr
    calls = fake_forges.calls("glab")
    assert not [c for c in calls if c.argv[-1] == "user"]
    (call,) = [c for c in calls if c.argv[-1].startswith("merge_requests?")]
    assert "reviewer_username=user-5" in call.argv[-1]


def test_a_hostile_author_exits_2_before_any_call(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    done = prs(run_forgectl, config(tmp_path), "--host", "gitlab.com", "--author", "x&scope=y")
    assert done.returncode == 2
    assert fake_forges.calls() == []


def test_a_failing_gitlab_with_a_working_github_is_partial_results(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    fake_forges.answer(
        "gh", "graphql", method="POST", host="github.com", body=gh_search(), headers=None
    )
    route(fake_forges, "merge_requests", {"message": "boom"}, status=500)
    route(
        fake_forges,
        "user",
        {"id": 11, "username": "user-1", "web_url": "https://gitlab.com/user-1"},
    )
    fake_forges.answer(
        "gh", "user", body={"login": "user-1", "id": 1, "html_url": "https://github.com/user-1"}
    )
    done = prs(run_forgectl, config(tmp_path), "--author", "user-1", "--state", "all")
    assert done.returncode == 12, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert out["complete"] is False
    # the fake answers every glab call alike, so both declared GitLab hosts fail
    assert sorted((e["host"], e["code"]) for e in out["errors"]) == [
        (SELF, "upstream_error"),
        ("gitlab.com", "upstream_error"),
    ]
    assert sorted(out["hosts_queried"]) == sorted(["github.com", "gitlab.com", SELF])
    assert out["items"]
    assert TOKEN not in done.stdout + done.stderr


def test_one_gitlab_host_failing_alone_uses_its_own_error(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    route(fake_forges, ROUTE, {"message": "404 Project Not Found"}, status=404)
    done = prs(run_forgectl, config(tmp_path), "--repo", "gitlab.com/user-9/proj")
    assert done.returncode == 10
    assert json.loads(done.stdout)["error"]["code"] == "not_found"


# -- access -----------------------------------------------------------------------------------


def script_access(forges: FakeForges, *, status: int = 200, **member: Any) -> None:
    route(forges, "projects/user-9%2Fproj", lists("project"))
    route(forges, "users", lists("users"))
    body = lists("member")
    body.update(member)
    route(forges, "projects/101/members/all/12", body, status=status)


def access(run: Run, path: Path, user: str) -> subprocess.CompletedProcess[str]:
    return run(["access", "gitlab.com/user-9/proj", user, "--json", "--config", str(path)], env=ENV)


def test_access_through_the_cli(run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path) -> None:
    script_access(fake_forges, access_level=40)
    done = access(run_forgectl, config(tmp_path), "user-2")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    golden.assert_golden("access/gitlab-maintain", out, schema_id="forgectl/cli/access.json")
    for call in fake_forges.calls("glab"):
        assert_call_safe(call, host="gitlab.com", mode="token")
    assert TOKEN not in done.stdout + done.stderr


def test_access_for_a_blocked_member_is_false(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    script_access(fake_forges, state="blocked")
    out = json.loads(access(run_forgectl, config(tmp_path), "user-2").stdout)
    assert out["write_access"] is False


def test_access_403_is_null_with_a_reason(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    script_access(fake_forges, status=403)
    done = access(run_forgectl, config(tmp_path), "user-2")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("access", out)
    assert out["write_access"] is None and out["reason"] == "no_permission_to_read"


def test_access_me_is_the_authenticated_login(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    script_access(fake_forges)
    route(
        fake_forges,
        "user",
        {"id": 12, "username": "user-2", "web_url": "https://gitlab.com/user-2"},
    )
    done = access(run_forgectl, config(tmp_path), "me")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["user"] == "user-2"
    (lookup,) = [c for c in fake_forges.calls("glab") if c.argv[-1].startswith("users?")]
    assert lookup.argv[-1] == "users?username=user-2"


# -- whoami -----------------------------------------------------------------------------------


def whoami(run: Run, path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["whoami", "--json", "--config", str(path), *args], env=ENV)


def test_whoami_of_gitlab_has_null_scopes_and_no_token(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    route(
        fake_forges,
        "user",
        {"id": 11, "username": "user-1", "web_url": "https://gitlab.com/user-1"},
    )
    done = whoami(run_forgectl, config(tmp_path), "--host", "gitlab.com")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    golden.assert_golden("whoami/gitlab", out, schema_id="forgectl/cli/whoami.json")
    assert out["hosts"][0]["scopes"] is None
    assert TOKEN not in done.stdout + done.stderr


def test_whoami_of_a_self_managed_host(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    route(
        fake_forges,
        "user",
        {"id": 11, "username": "user-1", "web_url": f"https://{SELF}/gl/user-1"},
    )
    done = whoami(run_forgectl, config(tmp_path), "--host", SELF)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["hosts"][0]["host"] == SELF and out["hosts"][0]["login"] == "user-1"
    for call in fake_forges.calls("glab"):
        assert_call_safe(call, host=SELF, mode="token", gitlab_host=SELF + "/gl")


def test_whoami_warns_when_me_differs_and_never_prints_the_token(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    route(
        fake_forges,
        "user",
        {"id": 11, "username": "user-1", "web_url": "https://gitlab.com/user-1"},
    )
    path = config(tmp_path, "me:\n  gitlab: user-9\n")
    done = whoami(run_forgectl, path, "--host", "gitlab.com")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["hosts"][0]["me_matches_login"] is False
    assert [x for x in done.stderr.splitlines() if x.startswith("forgectl: warning:")]
    assert TOKEN not in done.stdout + done.stderr


def test_an_answer_for_another_host_is_wrong_host_response(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    route(
        fake_forges,
        "user",
        {"id": 11, "username": "user-1", "web_url": "https://evil.example.test/user-1"},
    )
    done = whoami(run_forgectl, config(tmp_path), "--host", "gitlab.com")
    assert done.returncode == 12
    out = json.loads(done.stdout)
    assert out["errors"][0]["code"] == "wrong_host_response"
    assert TOKEN not in done.stdout + done.stderr
