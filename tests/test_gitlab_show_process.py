"""GitLab `show` through the fake `glab` of tests/fakebin (issue #15): real child processes, with
the safety rules asserted on every call (one token variable, a private cwd, no untrusted text in
argv, `GITLAB_HOST` routing for a port and an `api_prefix`)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from forgectl.backends.gitlab import GitLabBackend
from forgectl.config import Host, LoadedConfig
from forgectl.errors import ForgectlError
from forgectl.gateway import Gateway
from forgectl.hosts import Hosts

from .gitlab_support import HEAD, TOKEN, mr_path, recording, ref
from .harness.fixtures import forgectl_env
from .harness.forges import FakeForges
from .harness.safety import assert_call_safe


def route(forges: FakeForges, path: str, body: Any, **kwargs: Any) -> None:
    forges.answer(
        "glab", endpoint_re=rf"^{re.escape(path)}(\?|$)", body=body, headers=kwargs.get("headers")
    )


def script(
    forges: FakeForges,
    host: str,
    prefix: str,
    mr_host: str | None = None,
    status: str | None = None,
) -> None:
    web = f"https://{host}{prefix}"
    mr = recording("mr")
    if status is not None:
        mr["detailed_merge_status"] = status
    mr["web_url"] = f"https://{mr_host or host}{prefix}/user-9/proj/-/merge_requests/7"
    mr["author"]["web_url"] = f"{web}/user-1"
    project = recording("project")
    project["web_url"] = f"{web}/user-9/proj"
    route(forges, mr_path(), mr)
    route(forges, "projects/101", project)
    route(forges, "projects/101/merge_requests/7/approvals", recording("approvals"))
    route(forges, "projects/101/approvals", recording("project_approvals"))
    route(forges, "projects/101/merge_requests/7/reviewers", recording("reviewers"))
    route(
        forges,
        f"projects/101/repository/commits/{HEAD}/statuses",
        recording("statuses"),
        headers={"X-Next-Page": ""},
    )
    route(forges, "users/11", recording("user"))
    route(forges, "version", recording("version"))
    route(
        forges,
        "projects/101/pipelines/5001/bridges",
        recording("bridges"),
        headers={"X-Next-Page": ""},
    )


def backend(forges: FakeForges, host: str, prefix: str) -> GitLabBackend:
    declared: dict[str, Host] = {}
    if host != "gitlab.com":
        declared[host] = Host(backend="gitlab", api_prefix=prefix or None)
    declared[host] = declared.get(host, Host()).model_copy(update={"token_env": "MY_TOKEN"})
    base = LoadedConfig.defaults()
    config = LoadedConfig(
        config=base.config.model_copy(update={"hosts": declared}), config_hash="x", config_path=None
    )
    gateway = Gateway(config, timeout=60.0, environ=forgectl_env(forges, {"MY_TOKEN": TOKEN}))
    return GitLabBackend(Hosts(gateway))


@pytest.mark.parametrize(
    ("host", "prefix"),
    [("gitlab.com", ""), ("git.example.test:8443", "/gl"), ("git.example.test", "")],
)
def test_show_runs_only_safe_glab_calls(fake_forges: FakeForges, host: str, prefix: str) -> None:
    script(fake_forges, host, prefix)
    read = backend(fake_forges, host, prefix).get_pr(ref(host=host))
    assert read.pr.pr == f"{host}/user-9/proj#7"
    assert read.pr.blocked_by == () and read.ci.state == "passing"
    calls = fake_forges.calls("glab")
    assert len(calls) == 9
    gitlab_host = None if host == "gitlab.com" else host + prefix
    for call in calls:
        assert_call_safe(call, host=host, mode="token", gitlab_host=gitlab_host)
        assert call.token_sent == TOKEN
        assert call.argv[-2] == "--"
        assert call.argv[call.argv.index("api") + 1 :][0] in ("--hostname", "-i")
    assert calls[0].argv[-1] == "projects/user-9%2Fproj/merge_requests/7"
    assert json.dumps(read.pr.to_dict()).count(TOKEN) == 0


def test_a_redirect_to_another_host_is_wrong_host_response(fake_forges: FakeForges) -> None:
    """glab follows a redirect and shows the last answer; its web_url names the other host."""
    script(fake_forges, "gitlab.com", "", mr_host="other.example.test")
    with pytest.raises(ForgectlError) as raised:
        backend(fake_forges, "gitlab.com", "").get_pr(ref())
    assert raised.value.code == "wrong_host_response"
    assert TOKEN not in raised.value.message
