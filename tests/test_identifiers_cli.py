"""The identifier grammars through the command line: bad input exits 2, an undeclared host exits
20 before any handler (and so any process) runs, and a URL is echoed in its normalized form."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from forgectl import registry
from forgectl.cli import main
from forgectl.humanactions import Tty
from forgectl.identifiers import PrRef, RepoRef
from forgectl.registry import Invocation

from .cli_schema_check import validate

NO_TTY = Tty(stdin=False, stderr=False)
H1 = "a" * 40
EXPECTED = ["--expected-head", H1, "--expected-base", "main"]
URL = "https://github.com/o/r/pull/7/files?x=1#frag"
NORMAL = "github.com/o/r#7"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Invocation]]:
    seen: list[Invocation] = []
    handlers: dict[str, Any] = {}

    def record(inv: Invocation) -> dict[str, Any]:
        seen.append(inv)
        return {"recorded": True}

    for command in ("show", "prs", "access", "merge", "reply", "resolve", "comment", "whoami"):
        handlers[command] = record
    monkeypatch.setattr(registry, "HANDLERS", handlers)
    monkeypatch.setattr(registry, "load_handlers", lambda: None)
    yield seen


@pytest.fixture
def config_file(tmp_path: Path) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(
        "hosts:\n"
        "  git.example.test:\n    backend: gitlab\n"
        "  pre.example.test:\n    backend: gitlab\n    api_prefix: /gitlab\n",
        encoding="utf-8",
    )
    return str(path)


def run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    code = main(argv, tty=NO_TTY)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- PR references ----------------------------------------------------------------------------


def test_a_url_reaches_the_handler_as_a_bound_ref(
    calls: list[Invocation], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run(capsys, ["show", URL])
    assert code == 0, err
    assert calls[0].args.pr == PrRef(host="github.com", backend="github", project="o/r", number=7)
    assert str(calls[0].args.pr) == NORMAL


def test_a_declared_host_with_api_prefix(
    calls: list[Invocation], capsys: pytest.CaptureFixture[str], config_file: str
) -> None:
    url = "https://pre.example.test/gitlab/g/p/-/merge_requests/3/diffs"
    code, _, err = run(capsys, ["--config", config_file, "show", url])
    assert code == 0, err
    assert str(calls[0].args.pr) == "pre.example.test/g/p#3"


@pytest.mark.parametrize(
    "bad",
    [
        "https://user@github.com/o/r/pull/7",
        "https://github.com/o/r/pull/7abc",
        "github.com/o/r#7\n",
        "github.com/o/r#0",
        "https://github.com/o/r%2f/pull/7",
        "https://github.com/o\\r/pull/7",
        "https://gïthub.com/o/r/pull/7",
        "https://github.com/o/../pull/7",
        "\x1b[31mgithub.com/o/r#7",
    ],
)
def test_a_bad_pr_is_invalid_pr_before_any_handler(
    bad: str, calls: list[Invocation], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, ["--json", "show", bad])
    assert code == 2
    assert calls == []
    error = json.loads(out)["error"]
    validate(json.loads(out), "forgectl/cli/error.json")
    assert error["code"] == "invalid_pr"
    assert "\x1b" not in out + err
    assert "\n" not in err.rstrip("\n")


@pytest.mark.parametrize(
    "argv",
    [
        ["show", "https://other.example.test/o/r/pull/1"],
        ["show", "other.example.test/o/r#1"],
        ["show", "https://www.git.example.test/g/p/-/merge_requests/1"],
        ["prs", "--repo", "other.example.test/o/r"],
        ["prs", "--host", "other.example.test", "--author", "me"],
        ["access", "other.example.test/o/r", "bob"],
        ["whoami", "--host", "other.example.test"],
    ],
)
def test_an_undeclared_host_exits_20_before_any_handler(
    argv: list[str], calls: list[Invocation], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, ["--json", *argv])
    assert code == 20
    assert calls == []
    body = json.loads(out)
    validate(body, "forgectl/cli/error.json")
    assert (body["error"]["code"], body["error"]["reason"]) == ("refused", "unknown_host")
    assert "unknown_host" in err


def test_unknown_host_text_is_sanitized(
    calls: list[Invocation], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, ["show", "evil.example.test\x1b]0;x\x07/o/r#1"])
    assert code == 2  # the control characters never reach the host check
    assert "\x1b" not in out + err
    code, _, err = run(capsys, ["--json", "show", "evil.example.test/o/r#1"])
    assert code == 20
    assert err.count("\n") == 1


def test_a_declared_host_passes_the_check(
    calls: list[Invocation], capsys: pytest.CaptureFixture[str], config_file: str
) -> None:
    code, _, err = run(capsys, ["--config", config_file, "show", "git.example.test/g/p#1"])
    assert code == 0, err
    assert len(calls) == 1


def test_a_github_ref_with_a_third_segment_is_invalid_pr(
    calls: list[Invocation], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, ["--json", "show", "github.com/o/r/x#1"])
    assert (code, json.loads(out)["error"]["code"]) == (2, "invalid_pr")
    assert calls == []


# --- dry-run results use the normalized reference ---------------------------------------------


def refusing(reason: str = "stale_head") -> Any:
    from forgectl.errors import refused

    def handler(inv: Invocation) -> dict[str, Any]:
        details = {"head_sha": H1} if reason == "stale_head" else {"state": "open"}
        raise refused(reason, "no", details=details)

    return handler


@pytest.mark.parametrize(
    "argv",
    [
        ["merge", URL, *EXPECTED, "--dry-run"],
        ["comment", URL, "--body-file", "b", "--dry-run"],
        ["reply", URL, "--to", "gh:rc:abc", "--body-file", "b", "--dry-run"],
        ["resolve", URL, "--thread", "gh:rt:abc", "--dry-run"],
    ],
)
def test_dry_run_echoes_the_normalized_pr_from_a_url(
    argv: list[str], calls: list[Invocation], capsys: pytest.CaptureFixture[str]
) -> None:
    registry.HANDLERS[argv[0]] = refusing()
    code, out, err = run(capsys, ["--json", *argv])
    assert code == 20, err
    result = json.loads(out)
    validate(result, f"forgectl/cli/{argv[0]}.json")
    assert result["pr"] == NORMAL


@pytest.mark.parametrize("command", ["merge", "comment", "resolve"])
def test_dry_run_unknown_host_is_would_refuse_with_the_normalized_pr(
    command: str, calls: list[Invocation], capsys: pytest.CaptureFixture[str]
) -> None:
    extra = {
        "merge": EXPECTED,
        "comment": ["--body-file", "b"],
        "resolve": ["--thread", "gh:rt:abc"],
    }[command]
    url = "https://other.example.test/o/r/pull/9?a=b"
    code, out, _ = run(capsys, ["--json", command, url, *extra, "--dry-run"])
    assert code == 20
    assert calls == []
    result = json.loads(out)
    validate(result, f"forgectl/cli/{command}.json")
    assert result["pr"] == "other.example.test/o/r#9"
    assert result["would_refuse"]["reason"] == "unknown_host"


def test_dry_run_unknown_host_for_a_gitlab_shaped_url(
    calls: list[Invocation], capsys: pytest.CaptureFixture[str]
) -> None:
    url = "https://other.example.test/g/s/p/-/merge_requests/9"
    code, out, _ = run(capsys, ["--json", "merge", url, *EXPECTED, "--dry-run"])
    assert code == 20
    result = json.loads(out)
    validate(result, "forgectl/cli/merge.json")
    assert result["pr"] == "other.example.test/g/s/p#9"


# --- repo, host, login, thread and branch flags -------------------------------------------------


def test_repo_and_login_bind_for_prs(
    calls: list[Invocation], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run(capsys, ["prs", "--repo", "GitHub.com/o/r", "--author", "bob"])
    assert code == 0, err
    assert calls[0].args.repo == RepoRef(host="github.com", backend="github", path="o/r")
    assert calls[0].args.author == "bob"


def test_host_is_normalized(calls: list[Invocation], capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, ["prs", "--host", "WWW.GitLab.com:443", "--author", "me"])
    assert code == 0, err
    assert calls[0].args.host == "gitlab.com"


@pytest.mark.parametrize(
    "argv",
    [
        ["prs", "--repo", "github.com/o", "--author", "me"],
        ["prs", "--repo", "github.com/o/r/x", "--author", "me"],
        ["prs", "--repo", "github.com/o/r#1"],
        ["prs", "--repo", "github.com/../r"],
        ["prs", "--host", "github.com/o", "--author", "me"],
        ["prs", "--host", "gïthub.com", "--author", "me"],
        ["prs", "--author", "bob smith"],
        ["prs", "--author", "-x"],
        ["prs", "--review-requested-from", "a\n"],
        ["prs", "--host", "github.com", "--author", "a" * 40],
        ["prs", "--repo", "gitlab.com/g/p", "--author", "dependabot[bot]"],
        ["access", "github.com/o/r", "bad name"],
        ["access", "github.com/o/r", "a" * 40],
        ["access", "github.com", "bob"],
        ["reply", "github.com/o/r#1", "--to", "gh:zz:abc", "--body-file", "b"],
        ["reply", "github.com/o/r#1", "--to", "abc", "--body-file", "b"],
        ["reply", "github.com/o/r#1", "--to", "gl:d:" + "A" * 40, "--body-file", "b"],
        ["resolve", "github.com/o/r#1", "--thread", "gh:rt:"],
        ["resolve", "github.com/o/r#1", "--thread", "gh:rt:a b"],
        ["resolve", "github.com/o/r#1", "--thread", "gl:n:0"],
        ["merge", "github.com/o/r#1", "--expected-head", H1, "--expected-base", "refs/heads/main"],
        ["merge", "github.com/o/r#1", "--expected-head", H1, "--expected-base", "a:b"],
    ],
)
def test_bad_flag_values_are_usage(
    argv: list[str], calls: list[Invocation], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, ["--json", *argv])
    assert code == 2
    assert calls == []
    assert json.loads(out)["error"]["code"] == "usage"


def test_a_gitlab_login_is_valid_on_gitlab_and_the_typed_ids_reach_the_handler(
    calls: list[Invocation], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, ["prs", "--host", "gitlab.com", "--author", "." + "a" * 100])[0] == 0
    assert run(capsys, ["access", "gitlab.com/g/p", "me"])[0] == 0
    argv = ["reply", NORMAL, "--to", "gl:n:12", "--body-file", "b", "--yes"]
    assert run(capsys, argv)[0] == 0
    assert str(calls[-1].args.to) == "gl:n:12"
    argv = ["resolve", NORMAL, "--thread", "gh:rt:PRRT_x", "--yes"]
    assert run(capsys, argv)[0] == 0
    assert str(calls[-1].args.thread) == "gh:rt:PRRT_x"


def test_dry_run_echoes_the_thread_id(
    calls: list[Invocation], capsys: pytest.CaptureFixture[str]
) -> None:
    registry.HANDLERS["resolve"] = refusing("not_open")
    argv = ["--json", "resolve", NORMAL, "--thread", "gl:d:" + "b" * 40, "--dry-run"]
    code, out, _ = run(capsys, argv)
    result = json.loads(out)
    validate(result, "forgectl/cli/resolve.json")
    assert (code, result["thread_id"]) == (20, "gl:d:" + "b" * 40)


# --- a refusal at the identifier stage never starts a backend ---------------------------------


@pytest.fixture
def fake_backends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``gh`` and ``glab`` stand-ins that log every call, named by FORGECTL_*_BIN and on PATH."""
    log = tmp_path / "backend-calls.log"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("gh", "glab"):
        script = bindir / name
        script.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n', encoding="utf-8")
        script.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("FORGECTL_GH_BIN", str(bindir / "gh"))
    monkeypatch.setenv("FORGECTL_GLAB_BIN", str(bindir / "glab"))
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    return log


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["show", "https://other.example.test/o/r/pull/1"], 20),
        (["show", "other.example.test/o/r#1"], 20),
        (["merge", "other.example.test/o/r#1", *EXPECTED, "--dry-run"], 20),
        (["prs", "--repo", "other.example.test/o/r"], 20),
        (["prs", "--host", "other.example.test", "--author", "me"], 20),
        (["whoami", "--host", "other.example.test"], 20),
        (["show", "github.com/o/r#0"], 2),
        (["show", "https://gitlab.com/g/p/pull/3"], 2),
        (["show", "gitlab.com/--help#1"], 2),
        (["prs", "--repo", "gitlab.com/-x/y"], 2),
        (["prs", "--host", "github.com/o", "--author", "me"], 2),
    ],
)
def test_an_identifier_refusal_never_runs_gh_or_glab(
    argv: list[str], code: int, fake_backends: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(capsys, ["--json", *argv])[0] == code
    assert not fake_backends.exists()


@pytest.mark.xfail(
    strict=True,
    reason="positive control for the test above: no command calls gh or glab yet; drop this "
    "xfail once the first command calls the backend",
)
def test_a_valid_reference_does_run_the_backend(
    fake_backends: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, ["--json", "show", NORMAL])
    assert fake_backends.exists()
