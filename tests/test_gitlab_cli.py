"""`forgectl show` for GitLab through the real CLI and the fake `glab`, and the routing of a host
to its own backend (the integration of #11 and #15, groundwork of #16-#19)."""

from __future__ import annotations

import json
import re
import subprocess
from argparse import Namespace
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from forgectl import reads
from forgectl.backends import BACKEND_CLASSES, create_backend
from forgectl.backends.gitlab import GitLabBackend
from forgectl.errors import ForgectlError
from forgectl.identifiers import PrRef
from forgectl.model import PrRead
from forgectl.reads import SHOW_READINESS, ReadEnv
from forgectl.registry import Invocation

from . import test_reads
from .gitlab_support import (
    MR_ID,
    TOKEN,
    Answer,
    FakeGitLab,
    make_hosts,
    mr_path,
    recording,
    ref,
    standard,
)
from .harness import schemas
from .harness.forges import FakeForges
from .harness.safety import assert_call_safe
from .test_github_show import TOKEN as GH_TOKEN
from .test_github_show import recording as gh_recording
from .test_gitlab_show_process import script

Run = Callable[..., subprocess.CompletedProcess[str]]

SELF = "git.example.test:8443"


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "hosts:\n"
        "  gitlab.com:\n    token_env: GL_TOKEN\n"
        "  github.com:\n    token_env: GH_TOKEN\n"
        f"  {SELF}:\n    backend: gitlab\n    api_prefix: /gl\n    token_env: GL_TOKEN\n",
        encoding="utf-8",
    )
    return path


def run_show(run_forgectl: Run, tmp_path: Path, pr: str) -> subprocess.CompletedProcess[str]:
    return run_forgectl(
        ["show", pr, "--json", "--config", str(write_config(tmp_path))],
        env={"GL_TOKEN": TOKEN, "GH_TOKEN": GH_TOKEN},
    )


@pytest.mark.parametrize(("host", "prefix"), [("gitlab.com", ""), (SELF, "/gl")])
def test_show_of_a_gitlab_mr_through_the_cli(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path, host: str, prefix: str
) -> None:
    script(fake_forges, host, prefix)
    done = run_show(run_forgectl, tmp_path, f"{host}/user-9/proj#7")
    assert done.returncode == 0, done.stderr
    out: dict[str, Any] = json.loads(done.stdout)
    schemas.validate_cli_output("show", out)
    assert out["pr"] == f"{host}/user-9/proj#7"
    assert out["blocked_by"] == []
    assert TOKEN not in done.stdout and TOKEN not in done.stderr
    assert fake_forges.calls("gh") == []
    calls = fake_forges.calls("glab")
    assert calls
    gitlab_host = None if host == "gitlab.com" else host + prefix
    for call in calls:
        assert_call_safe(call, host=host, mode="token", gitlab_host=gitlab_host)
        assert call.token_sent == TOKEN


def test_a_github_ref_never_reaches_the_gitlab_backend(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", body=gh_recording())
    done = run_show(run_forgectl, tmp_path, "github.com/example-org/example-repo#7")
    assert done.returncode == 0, done.stderr
    assert fake_forges.calls("glab") == []
    calls = fake_forges.calls("gh")
    assert calls
    for call in calls:
        assert call.token_sent == GH_TOKEN


def test_the_readiness_loop_rereads_only_the_mr_through_the_cli(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    # Real sleeps here, so only "re-read at least once"; the exact count is pinned in process,
    # over the fake clock, by test_show_of_a_checking_mr_runs_the_readiness_loop_once.
    script(fake_forges, "gitlab.com", "", status="checking")
    done = run_show(run_forgectl, tmp_path, "gitlab.com/user-9/proj#7")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("show", out)
    assert [b["code"] for b in out["blocked_by"]] == ["mergeability_unknown"]
    endpoints = [c.argv[-1].partition("?")[0] for c in fake_forges.calls("glab")]
    assert sum(1 for e in endpoints if re.search(r"merge_requests/7$", e)) >= 2
    assert sum(1 for e in endpoints if re.search(r"projects/101$", e)) == 1
    assert sum(1 for e in endpoints if re.search(r"merge_requests/7/approvals$", e)) == 1


# the factory wiring, in process over the fake clock ------------------------------------------


def gitlab_env(fake: FakeGitLab, tmp_path: Path, *, retries: int = 3) -> ReadEnv:
    """A command's environment over the fake `glab`, with test_reads' fake clock and sleep."""
    clock = test_reads.Clock()
    hosts = make_hosts(fake, tmp_path)
    return ReadEnv(
        gateway=hosts.gateway,
        hosts=hosts,
        retries=retries,
        sleep=clock.sleep,
        clock=clock,
        jitter=lambda: 0.999,
    )


def test_create_backend_builds_a_gitlab_backend_that_retries_through_env_call(
    tmp_path: Path,
) -> None:
    env = gitlab_env(FakeGitLab(), tmp_path)
    backend = create_backend("gitlab", env)
    assert isinstance(backend, GitLabBackend)
    assert backend._retry == env.call


def flaky_mr(fake: FakeGitLab) -> None:
    """Route the base recordings, the MR GET answering 502 once before it succeeds."""
    data = standard(fake)
    state = {"n": 0}

    def handler(_query: dict[str, str]) -> Answer:
        state["n"] += 1
        if state["n"] == 1:
            return Answer(502, {"message": "502 Bad Gateway"})
        return Answer(200, data["mr"])

    fake.on_handler(mr_path(), handler)


def test_a_retryable_mr_failure_is_retried_by_the_factory_built_backend(tmp_path: Path) -> None:
    fake = FakeGitLab()
    flaky_mr(fake)
    read = create_backend("gitlab", gitlab_env(fake, tmp_path)).get_pr(ref())
    assert read.pr.blocked_by == ()
    assert fake.count(mr_path()) == 2


def test_retries_zero_leaves_the_mr_failure_to_the_caller(tmp_path: Path) -> None:
    fake = FakeGitLab()
    flaky_mr(fake)
    backend = create_backend("gitlab", gitlab_env(fake, tmp_path, retries=0))
    with pytest.raises(ForgectlError) as caught:
        backend.get_pr(ref())
    assert caught.value.code == "upstream_error"
    assert fake.count(mr_path()) == 1


def test_show_of_a_checking_mr_runs_the_readiness_loop_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeGitLab()
    mr = recording("mr")
    mr["detailed_merge_status"] = "checking"
    standard(fake, mr=mr)
    env = gitlab_env(fake, tmp_path)
    monkeypatch.setattr(reads, "make_env", lambda _inv: env)
    real_get_pr = GitLabBackend.get_pr
    asked: list[PrRef] = []

    def spy(self: GitLabBackend, pr: PrRef) -> PrRead:
        asked.append(pr)
        return real_get_pr(self, pr)

    monkeypatch.setattr(GitLabBackend, "get_pr", spy)
    out = reads.show(Invocation("show", Namespace(pr=ref()), True, None))
    assert isinstance(out, dict)  # the registered handler type also allows a RecordStream
    assert [b["code"] for b in out["blocked_by"]] == ["mergeability_unknown"]
    assert asked == [ref()]  # the handler asks once: the loop is the backend's
    assert fake.count(mr_path()) == SHOW_READINESS.tries
    others = Counter(c.path for c in fake.calls if c.path != mr_path())
    assert others["projects/101"] == 1
    assert others[f"{MR_ID}/approvals"] == 1
    assert others and set(others.values()) == {1}  # every other read follows once


def test_the_table_names_both_backends() -> None:
    assert set(BACKEND_CLASSES) == {"github", "gitlab"}


def test_a_host_of_an_unregistered_backend_is_an_internal_envelope_in_the_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from forgectl import backends, cli

    monkeypatch.setattr(backends, "BACKEND_CLASSES", {"github": BACKEND_CLASSES["github"]})
    monkeypatch.setenv("GL_TOKEN", TOKEN)
    code = cli.main(
        ["--json", "show", "gitlab.com/user-9/proj#7", "--config", str(write_config(tmp_path))]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.out)["error"]["code"] == "internal"
    assert "Traceback" not in captured.out + captured.err
