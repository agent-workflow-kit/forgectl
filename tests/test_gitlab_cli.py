"""`forgectl show` for GitLab through the real CLI and the fake `glab`, and the routing of a host
to its own backend (the integration of #11 and #15, groundwork of #16-#19)."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from forgectl.backends import BACKEND_CLASSES, create_backend
from forgectl.errors import ForgectlError

from . import test_reads
from .gitlab_support import TOKEN
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
    done = run_show(run_forgectl, tmp_path, "github.com/user-7/example-repo#7")
    assert done.returncode == 0, done.stderr
    assert fake_forges.calls("glab") == []
    calls = fake_forges.calls("gh")
    assert calls
    for call in calls:
        assert call.token_sent == GH_TOKEN


def test_a_gitlab_ref_never_reaches_the_github_backend(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    script(fake_forges, "gitlab.com", "")
    done = run_show(run_forgectl, tmp_path, "gitlab.com/user-9/proj#7")
    assert done.returncode == 0, done.stderr
    assert fake_forges.calls("gh") == []
    assert all(call.token_sent == TOKEN for call in fake_forges.calls("glab"))


def test_the_readiness_loop_runs_once_in_the_backend_not_twice(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    script(fake_forges, "gitlab.com", "", status="checking")
    done = run_show(run_forgectl, tmp_path, "gitlab.com/user-9/proj#7")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("show", out)
    assert [b["code"] for b in out["blocked_by"]] == ["mergeability_unknown"]
    mr_gets = [c for c in fake_forges.calls("glab") if re.search(r"merge_requests/7$", c.argv[-1])]
    assert len(mr_gets) == 3  # SHOW_READINESS.tries, once


def test_an_unknown_backend_name_is_an_internal_error() -> None:
    env, _ = test_reads.make_env()
    with pytest.raises(ForgectlError) as caught:
        create_backend("bitbucket", env)
    assert caught.value.code == "internal"


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
