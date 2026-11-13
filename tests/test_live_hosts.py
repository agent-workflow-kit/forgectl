"""Host helpers and the throwaway lifecycle against a scripted runner: no host is reached."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from .live import guards
from .live.api import LiveApi, LiveApiError, RunResult, WriteScope, redact
from .live.guards import LiveRefused
from .live.hosts import GitHubLive, GitLabLive, Ledger

TOKEN = "tok-live-0001"
NAME = "forgectl-live-20261112t101500z-ab12cd34"
SOURCE = {"PATH": "/usr/bin", "GITHUB_TOKEN": "ambient-other-host-token"}


class Script:
    """Scripted answers by (method, endpoint); every launch is recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str], str | None]] = []
        self.answers: dict[tuple[str, str], list[tuple[int, Any]]] = {}

    def on(self, method: str, endpoint: str, status: int, body: Any = None, *more: Any) -> None:
        self.answers.setdefault((method, endpoint), []).append((status, body))
        for status2, body2 in zip(more[::2], more[1::2], strict=False):
            self.answers[(method, endpoint)].append((status2, body2))

    def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], stdin: str | None, cwd: Path
    ) -> RunResult:
        self.calls.append((list(argv), dict(env), stdin))
        method = argv[argv.index("-X") + 1]
        endpoint = argv[-1]
        queue = self.answers.get((method, endpoint))
        if not queue:
            return RunResult(1, "", f"no scripted answer for {method} {endpoint} {TOKEN}")
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        text = "" if body is None else json.dumps(body)
        return RunResult(
            0 if status < 400 else 1, f"HTTP/2.0 {status} X\r\ncontent-type: x\r\n\r\n{text}", ""
        )

    def endpoints(self, method: str | None = None) -> list[str]:
        return [
            c[0][-1] for c in self.calls if method is None or c[0][c[0].index("-X") + 1] == method
        ]


def make(backend: str, tmp_path: Path) -> tuple[Any, Script, Ledger]:
    cfg = guards.LiveConfig(backend, guards.BACKEND_HOSTS[backend], TOKEN, "me-login")
    dirs = guards.make_private_dirs(tmp_path / "live")
    script = Script()
    creation = GitHubLive.creation if backend == "github" else GitLabLive.creation
    api = LiveApi(cfg, dirs, WriteScope(creation), source_env=SOURCE, runner=script)
    ledger = Ledger()
    host = (GitHubLive if backend == "github" else GitLabLive)(api, cfg, ledger)
    return host, script, ledger


def github_repo(owner: str = "me-login") -> dict[str, Any]:
    return {"owner": {"login": owner}, "default_branch": "main", "html_url": "https://x.test/r"}


def gitlab_project(namespace: str = "me-login") -> dict[str, Any]:
    return {
        "id": 77,
        "namespace": {"full_path": namespace},
        "default_branch": "main",
        "web_url": "https://x.test/p",
    }


# Requests and the runner ---------------------------------------------------------------------


def test_requests_go_through_gh_api_with_the_host_named_and_the_token_only_in_env(
    tmp_path: Path,
) -> None:
    host, script, _ = make("github", tmp_path)
    script.on("GET", "user", 200, {"login": "me-login"})
    assert host.whoami() == "me-login"
    argv, env, _ = script.calls[0]
    assert argv[:5] == ["gh", "api", "--hostname", "github.com", "--include"]
    assert not any(TOKEN in a for a in argv)
    assert env["GH_TOKEN"] == TOKEN and "GITHUB_TOKEN" not in env


def test_a_gitlab_request_goes_through_glab_api(tmp_path: Path) -> None:
    host, script, _ = make("gitlab", tmp_path)
    script.on("GET", "user", 200, {"username": "me-login"})
    host.whoami()
    argv, env, _ = script.calls[0]
    assert argv[:4] == ["glab", "api", "--hostname", "gitlab.com"]
    assert env["GITLAB_TOKEN"] == TOKEN and "GH_TOKEN" not in env


@pytest.mark.parametrize("backend", ["github", "gitlab"])
def test_a_token_of_another_account_stops_the_run_before_any_write(
    tmp_path: Path, backend: str
) -> None:
    host, script, _ = make(backend, tmp_path)
    script.on("GET", "user", 200, {"login": "someone-else", "username": "someone-else"})
    with pytest.raises(LiveRefused):
        host.whoami()
    assert script.endpoints("POST") == []


def test_the_api_refuses_a_foreign_host_even_if_the_config_names_one(tmp_path: Path) -> None:
    cfg = guards.LiveConfig("github", "ghe.example.test", TOKEN, "me-login")
    with pytest.raises(LiveRefused):
        LiveApi(
            cfg,
            guards.make_private_dirs(tmp_path / "l"),
            WriteScope("user/repos"),
            source_env=SOURCE,
            runner=Script(),
        )


@pytest.mark.parametrize(
    "endpoint",
    ["", "/user", "https://example.test/user", "repos/a/../../user", "a b", "user\n", "http://x"],
)
def test_an_endpoint_that_could_leave_the_api_is_refused(tmp_path: Path, endpoint: str) -> None:
    host, script, _ = make("github", tmp_path)
    with pytest.raises(LiveRefused):
        host.api.request("GET", endpoint)
    assert script.calls == []


def test_a_write_outside_the_run_scope_is_refused_before_launch(tmp_path: Path) -> None:
    host, script, _ = make("github", tmp_path)
    for method, endpoint in [
        ("DELETE", f"repos/me-login/{NAME}"),  # not created by this run yet
        ("POST", "orgs/some-org/repos"),
        ("PATCH", "user"),
        ("PUT", "repos/me-login/other/contents/x"),
    ]:
        with pytest.raises(LiveRefused):
            host.api.request(method, endpoint, {})
    with pytest.raises(LiveRefused):
        host.api.request("TRACE", "user")
    assert script.calls == []


def test_a_scope_prefix_does_not_cover_a_sibling_name(tmp_path: Path) -> None:
    host, script, _ = make("github", tmp_path)
    host.api.scope.allow(f"repos/me-login/{NAME}")
    with pytest.raises(LiveRefused):
        host.api.request("DELETE", f"repos/me-login/{NAME}-sibling")
    with pytest.raises(LiveRefused):
        host.api.request("DELETE", f"repos/me-login/{NAME}/../other")
    assert script.calls == []


def test_a_missing_status_line_is_an_error_without_the_token(tmp_path: Path) -> None:
    host, _, _ = make("github", tmp_path)
    with pytest.raises(LiveApiError) as caught:
        host.api.request("GET", "user")  # nothing scripted: stderr carries the token
    assert TOKEN not in str(caught.value) and "<token>" in str(caught.value)


def test_redact_hides_every_value() -> None:
    assert redact("a tok1 b tok2", ["tok1", "tok2", ""]) == "a <token> b <token>"


# GitHub lifecycle and helpers ------------------------------------------------------------------


def test_github_create_registers_before_checking_and_scopes_writes(tmp_path: Path) -> None:
    host, script, ledger = make("github", tmp_path)
    script.on("POST", "user/repos", 201, github_repo())
    throwaway = host.create_throwaway(NAME)
    assert (throwaway.ident, throwaway.owner) == (f"me-login/{NAME}", "me-login")
    assert ledger.entries == [(host, throwaway)]
    sent = json.loads(script.calls[0][2] or "")
    assert sent["name"] == NAME and "org" not in sent and sent["private"] is False
    assert host.api.scope.permits("DELETE", f"repos/me-login/{NAME}")


def test_a_repo_in_another_namespace_is_still_cleaned_up(tmp_path: Path) -> None:
    host, script, ledger = make("github", tmp_path)
    script.on("POST", "user/repos", 201, github_repo(owner="an-org"))
    with pytest.raises(LiveRefused):
        host.create_throwaway(NAME)
    assert len(ledger.entries) == 1  # registered before the check, so the finalizer reaches it


def test_github_create_refuses_a_name_without_the_prefix(tmp_path: Path) -> None:
    host, script, _ = make("github", tmp_path)
    with pytest.raises(LiveRefused):
        host.create_throwaway("my-real-repo")
    assert script.calls == []


def test_github_helpers_send_documented_bodies(tmp_path: Path) -> None:
    host, script, _ = make("github", tmp_path)
    script.on("POST", "user/repos", 201, github_repo())
    t = host.create_throwaway(NAME)
    repo = f"repos/me-login/{NAME}"
    script.on("GET", f"{repo}/git/ref/heads/main", 200, {"object": {"sha": "a" * 40}})
    script.on("POST", f"{repo}/git/refs", 201, {})
    script.on("PUT", f"{repo}/contents/.github/workflows/x.yml", 201, {"commit": {"sha": "b" * 40}})
    script.on("POST", f"{repo}/pulls", 201, {"number": 3})
    script.on("PUT", f"{repo}/branches/main/protection", 200, {})
    sha = host.branch_sha(t, "main")
    host.create_branch(t, "feat", sha)
    commit = host.add_ci_file(t, "feat", "x", "on: push\n")
    assert host.open_change(t, "feat", "main", "title", draft=True) == 3
    host.protect_branch(t, "main", required_checks=("build",), approvals=1)
    assert commit == "b" * 40
    bodies = {c[0][-1]: json.loads(c[2]) for c in script.calls if c[2]}
    assert bodies[f"{repo}/git/refs"] == {"ref": "refs/heads/feat", "sha": sha}
    put = bodies[f"{repo}/contents/.github/workflows/x.yml"]
    assert base64.b64decode(put["content"]) == b"on: push\n" and put["branch"] == "feat"
    assert bodies[f"{repo}/pulls"]["draft"] is True
    protection = bodies[f"{repo}/branches/main/protection"]
    assert protection["required_status_checks"]["contexts"] == ["build"]
    assert protection["required_pull_request_reviews"] == {"required_approving_review_count": 1}


def test_github_delete_is_verified_by_reading(tmp_path: Path) -> None:
    host, script, ledger = make("github", tmp_path)
    script.on("POST", "user/repos", 201, github_repo())
    t = host.create_throwaway(NAME)
    repo = f"repos/me-login/{NAME}"
    script.on("DELETE", repo, 204)
    script.on("GET", repo, 404, {"message": "Not Found"})
    assert ledger.cleanup_all() == [t] and t.status == "deleted"
    assert ledger.declarations() == []


def test_github_without_delete_scope_archives_and_declares_the_name(tmp_path: Path) -> None:
    host, script, ledger = make("github", tmp_path)
    script.on("POST", "user/repos", 201, github_repo())
    t = host.create_throwaway(NAME)
    repo = f"repos/me-login/{NAME}"
    script.on("DELETE", repo, 403, {"message": "Must have admin rights"})
    script.on("PATCH", repo, 200, {})
    script.on("GET", repo, 200, {"archived": True})
    ledger.cleanup_all()
    assert t.status == "archived"
    assert ledger.declarations() == [f"archived: {NAME} on github.com (delete it by hand)"]


def test_a_delete_that_the_host_still_shows_falls_through_to_archive(tmp_path: Path) -> None:
    host, script, ledger = make("github", tmp_path)
    script.on("POST", "user/repos", 201, github_repo())
    t = host.create_throwaway(NAME)
    repo = f"repos/me-login/{NAME}"
    script.on("DELETE", repo, 204)
    script.on("GET", repo, 200, {"archived": False}, 200, {"archived": True})
    script.on("PATCH", repo, 200, {})
    ledger.cleanup_all()
    assert t.status == "archived"


def test_a_resource_that_cannot_be_deleted_or_archived_is_declared_left(tmp_path: Path) -> None:
    host, script, ledger = make("github", tmp_path)
    script.on("POST", "user/repos", 201, github_repo())
    t = host.create_throwaway(NAME)
    repo = f"repos/me-login/{NAME}"
    script.on("DELETE", repo, 500, {})
    script.on("PATCH", repo, 500, {})
    ledger.cleanup_all()
    assert t.status == "left"
    assert ledger.declarations() == [f"left: {NAME} on github.com (delete it by hand)"]


def test_cleanup_is_idempotent_and_reaches_every_entry_after_a_failure(tmp_path: Path) -> None:
    host, script, ledger = make("github", tmp_path)
    script.on("POST", "user/repos", 201, github_repo())
    first = host.create_throwaway(NAME)
    second_name = "forgectl-live-20261112t101501z-ab12cd35"
    script.on("POST", "user/repos", 201, github_repo())
    second = host.create_throwaway(second_name)
    for name in (NAME, second_name):
        script.on("DELETE", f"repos/me-login/{name}", 204)
        script.on("GET", f"repos/me-login/{name}", 404, {})
    original = host.delete_or_archive

    def boom(t: Any) -> str:
        if t is second:
            raise RuntimeError("unexpected")
        return original(t)

    host.delete_or_archive = boom
    ledger.cleanup_all()
    assert (first.status, second.status) == ("deleted", "left")
    deletes = script.endpoints("DELETE")
    ledger.cleanup_all()
    assert script.endpoints("DELETE") == deletes  # nothing is sent twice


def test_a_resource_whose_cleanup_never_ran_is_declared(tmp_path: Path) -> None:
    host, script, ledger = make("github", tmp_path)
    script.on("POST", "user/repos", 201, github_repo())
    host.create_throwaway(NAME)
    assert ledger.declarations() == [f"left: {NAME} on github.com (cleanup did not run)"]


# GitLab lifecycle and helpers ----------------------------------------------------------------


def test_gitlab_create_sends_no_namespace_and_scopes_writes_to_the_project(tmp_path: Path) -> None:
    host, script, ledger = make("gitlab", tmp_path)
    script.on("POST", "projects", 201, gitlab_project())
    t = host.create_throwaway(NAME)
    sent = json.loads(script.calls[0][2] or "")
    assert "namespace_id" not in sent and "namespace" not in sent and sent["path"] == NAME
    assert t.ident == "77" and ledger.entries == [(host, t)]
    assert host.api.scope.permits("DELETE", "projects/77")
    assert not host.api.scope.permits("DELETE", "projects/771")
    assert not host.api.scope.permits("PUT", "projects/78")


def test_a_gitlab_project_in_another_namespace_is_refused_and_still_registered(
    tmp_path: Path,
) -> None:
    host, script, ledger = make("gitlab", tmp_path)
    script.on("POST", "projects", 201, gitlab_project(namespace="a-group"))
    with pytest.raises(LiveRefused):
        host.create_throwaway(NAME)
    assert len(ledger.entries) == 1


def test_gitlab_helpers_send_documented_bodies(tmp_path: Path) -> None:
    host, script, _ = make("gitlab", tmp_path)
    script.on("POST", "projects", 201, gitlab_project())
    t = host.create_throwaway(NAME)
    script.on("GET", "projects/77/repository/branches/main", 200, {"commit": {"id": "a" * 40}})
    script.on("POST", "projects/77/repository/branches", 201, {})
    script.on("POST", "projects/77/repository/commits", 201, {"id": "b" * 40})
    script.on("POST", "projects/77/merge_requests", 201, {"iid": 4})
    script.on("DELETE", "projects/77/protected_branches/main", 204)
    script.on("POST", "projects/77/protected_branches", 201, {})
    script.on("PUT", "projects/77", 200, {})
    sha = host.branch_sha(t, "main")
    host.create_branch(t, "feat", sha)
    assert host.add_ci_file(t, "feat", "x", "job:\n  script: [true]\n") == "b" * 40
    assert host.open_change(t, "feat", "main", "title", draft=True) == 4
    host.protect_branch(t, "main")
    host.set_project(t, only_allow_merge_if_pipeline_succeeds=True)
    bodies = {
        (c[0][c[0].index("-X") + 1], c[0][-1]): json.loads(c[2]) for c in script.calls if c[2]
    }
    commit = bodies[("POST", "projects/77/repository/commits")]
    assert commit["actions"][0]["file_path"] == ".gitlab-ci.yml"
    assert bodies[("POST", "projects/77/merge_requests")]["title"] == "Draft: title"
    assert bodies[("PUT", "projects/77")] == {"only_allow_merge_if_pipeline_succeeds": True}


def test_gitlab_delete_counts_when_the_host_reads_404_or_marks_it(tmp_path: Path) -> None:
    for seen, status in [(404, "deleted"), (200, "scheduled")]:
        host, script, ledger = make("gitlab", tmp_path / status)
        script.on("POST", "projects", 201, gitlab_project())
        t = host.create_throwaway(NAME)
        script.on("DELETE", "projects/77", 202, {"message": "202 Accepted"})
        script.on("GET", "projects/77", seen, {"marked_for_deletion_on": "2026-11-12"})
        ledger.cleanup_all()
        assert t.status == status


def test_gitlab_archives_when_delete_is_refused(tmp_path: Path) -> None:
    host, script, ledger = make("gitlab", tmp_path)
    script.on("POST", "projects", 201, gitlab_project())
    t = host.create_throwaway(NAME)
    script.on("DELETE", "projects/77", 403, {})
    script.on("POST", "projects/77/archive", 201, {})
    script.on("GET", "projects/77", 200, {"archived": True})
    ledger.cleanup_all()
    assert t.status == "archived"
    assert ledger.declarations() == [f"archived: {NAME} on gitlab.com (delete it by hand)"]
