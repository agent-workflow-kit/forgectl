"""The fake ``gh`` and ``glab``: argv grammar, exit codes, streams, routing, logging, signals."""

from __future__ import annotations

import json
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from .harness.forges import Call, FakeForges
from .harness.procs import ProcessRegistry
from .harness.safety import FIXED_SETTINGS, reference_env

Runner = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture
def forges(fake_forges: FakeForges) -> FakeForges:
    return fake_forges


@pytest.fixture
def run(procs: ProcessRegistry, forges: FakeForges, tmp_path: Path) -> Runner:
    counter = iter(range(1000))

    def go(
        backend: str,
        args: list[str],
        *,
        host: str = "github.com",
        env_extra: dict[str, str] | None = None,
        drop: tuple[str, ...] = (),
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        root = tmp_path / f"run{next(counter)}"
        root.mkdir()
        env, cwd = reference_env(backend, host, root=root, path=forges.path())
        for name in drop:
            env.pop(name, None)
        env.update(env_extra or {})
        return procs.run([backend, *args], env=env, cwd=cwd, input=stdin)

    return go


def only(forges: FakeForges) -> Call:
    calls = forges.calls()
    assert len(calls) == 1
    return calls[0]


def test_gh_api_prints_the_body_and_logs_the_call(run: Runner, forges: FakeForges) -> None:
    forges.answer("gh", "repos/o/r/pulls/1", body={"number": 1})
    done = run("gh", ["api", "--hostname", "github.com", "repos/o/r/pulls/1"])
    assert (done.returncode, json.loads(done.stdout), done.stderr) == (0, {"number": 1}, "")
    call = only(forges)
    assert call.argv == ["api", "--hostname", "github.com", "repos/o/r/pulls/1"]
    assert call.env["GH_TOKEN"] == "tok-fixture-0001"
    assert call.cwd_entries == [] and call.cwd_mode == 0o700
    assert call.request and call.request["url"] == "https://api.github.com/repos/o/r/pulls/1"
    assert call.token_sent == "tok-fixture-0001"


def test_include_prints_headers_before_the_body(run: Runner, forges: FakeForges) -> None:
    forges.answer("gh", "x", status=201, body={"id": 5}, headers={"X-A": "1"})
    done = run("gh", ["api", "-i", "--hostname", "github.com", "x"])
    head, _, body = done.stdout.partition("\r\n\r\n")
    assert head.splitlines()[0] == "HTTP/2.0 201 Created"
    assert "X-A: 1" in head
    assert json.loads(body) == {"id": 5}


def test_http_error_exits_1_with_body_on_stdout_and_message_on_stderr(
    run: Runner, forges: FakeForges
) -> None:
    forges.answer("gh", "x", status=404, body={"message": "Not Found"})
    done = run("gh", ["api", "--hostname", "github.com", "x"])
    assert done.returncode == 1
    assert json.loads(done.stdout) == {"message": "Not Found"}
    assert done.stderr == "gh: Not Found (HTTP 404)\n"


def test_graphql_errors_with_http_200_exit_1_with_the_full_body(
    run: Runner, forges: FakeForges
) -> None:
    body = {"data": {"partial": 1}, "errors": [{"type": "NOT_FOUND", "message": "gone"}]}
    forges.answer("gh", "graphql", method="POST", body=body)
    done = run("gh", ["api", "--hostname", "github.com", "graphql", "--input", "-"], stdin="{}")
    assert done.returncode == 1
    assert json.loads(done.stdout) == body


def test_gh_without_a_credential_exits_4_before_any_request(
    run: Runner, forges: FakeForges
) -> None:
    done = run("gh", ["api", "--hostname", "github.com", "x"], drop=("GH_TOKEN",))
    assert done.returncode == 4
    assert "gh auth login" in done.stderr and done.stdout == ""
    assert only(forges).request is None


def test_enterprise_host_needs_the_enterprise_variable(run: Runner, forges: FakeForges) -> None:
    forges.answer("gh", "x", body={})
    wrong = run("gh", ["api", "--hostname", "ghe.example.test", "x"], env_extra={"GH_TOKEN": "t"})
    assert wrong.returncode == 4
    ok = run(
        "gh",
        ["api", "--hostname", "ghe.example.test", "x"],
        host="ghe.example.test",
    )
    assert ok.returncode == 0
    assert forges.calls()[-1].request["url"] == "https://ghe.example.test/api/v3/x"  # type: ignore[index]


def test_ghe_com_tenancy_takes_gh_token(run: Runner, forges: FakeForges) -> None:
    forges.answer("gh", "x", body={})
    done = run("gh", ["api", "--hostname", "acme.ghe.com", "x"], host="acme.ghe.com")
    assert done.returncode == 0
    assert only(forges).request["token_source"] == "env:GH_TOKEN"  # type: ignore[index]


def test_request_body_goes_by_stdin_and_is_logged(run: Runner, forges: FakeForges) -> None:
    forges.answer("gh", "repos/o/r/issues/1/comments", status=201, body={"id": 1})
    done = run(
        "gh",
        ["api", "--hostname", "github.com", "repos/o/r/issues/1/comments", "--input", "-"],
        stdin='{"body": "hi"}',
    )
    assert done.returncode == 0
    call = only(forges)
    assert call.stdin == '{"body": "hi"}'
    assert call.request and call.request["method"] == "POST"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["api", "--bogus", "x"], "unknown flag: --bogus"),
        (["api", "-Z", "x"], "unknown shorthand flag: 'Z' in -Z"),
        (["api", "--hostname"], "flag needs an argument: --hostname"),
        (["api"], "accepts 1 arg(s), received 0"),
        (["api", "a", "b"], "accepts 1 arg(s), received 2"),
    ],
)
def test_usage_errors_exit_1_with_cobra_wording(
    run: Runner, forges: FakeForges, args: list[str], message: str
) -> None:
    done = run("gh", args)
    assert done.returncode == 1
    assert message in done.stderr
    assert done.stdout == ""


def test_flag_forms_are_parsed_like_pflag(run: Runner, forges: FakeForges) -> None:
    forges.answer("gh", "x", body={})
    done = run("gh", ["api", "--hostname=github.com", "-iXPOST", "--", "x"])
    assert done.returncode == 0
    assert only(forges).request["method"] == "POST"  # type: ignore[index]
    assert done.stdout.startswith("HTTP/2.0 200 OK")


@pytest.mark.parametrize("flag", [["--jq", ".x"], ["--paginate"], ["--cache", "1h"]])
def test_unmodelled_flags_fail_loudly_not_silently(
    run: Runner, forges: FakeForges, flag: list[str]
) -> None:
    done = run("gh", ["api", "--hostname", "github.com", *flag, "x"])
    assert done.returncode == 70
    assert "not modelled" in done.stderr


@pytest.mark.parametrize("endpoint", ["repos/{owner}/{repo}/pulls", "projects/:id/x"])
def test_cli_placeholders_are_logged_and_refused(
    run: Runner, forges: FakeForges, endpoint: str
) -> None:
    backend = "gh" if "{" in endpoint else "glab"
    host = "github.com" if backend == "gh" else "gitlab.com"
    done = run(backend, ["api", "--hostname", host, endpoint])
    assert done.returncode == 70
    assert only(forges).placeholders


def test_unknown_subcommand_fails_loudly(run: Runner, forges: FakeForges) -> None:
    done = run("gh", ["pr", "view", "1"])
    assert done.returncode == 70 and "not modelled" in done.stderr


def test_version(run: Runner) -> None:
    assert run("gh", ["--version"]).stdout.startswith("gh version 2.72.0")
    assert run("glab", ["--version"], host="gitlab.com").stdout.startswith("glab 1.127.0")


def test_unmatched_request_answers_404_and_is_reported(run: Runner, forges: FakeForges) -> None:
    done = run("gh", ["api", "--hostname", "github.com", "nothing"])
    assert done.returncode == 1
    assert [c.unmatched for c in forges.calls()] == [True]
    forges.allow_unmatched = True  # the fixture would otherwise fail this test at teardown


def test_rules_match_in_order_with_times_and_nth(run: Runner, forges: FakeForges) -> None:
    forges.answer("gh", "x", times=1, body={"n": "first"})
    forges.answer("gh", "x", body={"n": "later"})
    forges.answer("gh", "y", nth=2, body={"n": "second-call"})
    forges.allow_unmatched = True
    got = [
        json.loads(run("gh", ["api", "--hostname", "github.com", e]).stdout or "{}")
        for e in ("x", "x", "y", "y")
    ]
    assert got[0] == {"n": "first"} and got[1] == {"n": "later"}
    assert got[2] != {"n": "second-call"} and got[3] == {"n": "second-call"}


def test_gh_auth_token_returns_an_ambient_token_for_any_host(
    run: Runner, forges: FakeForges
) -> None:
    done = run("gh", ["auth", "token", "--hostname", "other.example.test"])
    assert (done.returncode, done.stdout) == (0, "tok-fixture-0001\n")


def test_gh_auth_token_without_any_token_exits_1(run: Runner) -> None:
    done = run("gh", ["auth", "token", "--hostname", "github.com"], drop=("GH_TOKEN",))
    assert done.returncode == 1 and "no oauth token found" in done.stderr


def test_glab_auth_token_has_no_hostname_flag(run: Runner) -> None:
    done = run("glab", ["auth", "token", "--hostname", "gitlab.com"], host="gitlab.com")
    assert done.returncode == 1 and "unknown flag: --hostname" in done.stderr


# glab routing: the table in design.md, "glab routing cases".


def glab_call(run: Runner, forges: FakeForges, args: list[str], env: dict[str, str]) -> Call:
    forges.answer("glab", "user", body={})
    run(
        "glab",
        ["api", *args, "user"],
        host="gitlab.example.test",
        env_extra=env,
        drop=("GITLAB_HOST",),
    )
    return forges.calls()[-1]


def test_glab_routing_host_with_prefix_and_hostname_flag(run: Runner, forges: FakeForges) -> None:
    call = glab_call(
        run, forges, ["--hostname", "gl.example.test"], {"GITLAB_HOST": "gl.example.test/pre"}
    )
    assert call.request["url"] == "https://gl.example.test/api/v4/user"  # type: ignore[index]
    assert call.token_sent == "tok-fixture-0001"


def test_glab_routing_host_with_prefix_alone_keeps_the_prefix(
    run: Runner, forges: FakeForges
) -> None:
    call = glab_call(run, forges, [], {"GITLAB_HOST": "gl.example.test/pre"})
    assert call.request["url"] == "https://gl.example.test/pre/api/v4/user"  # type: ignore[index]
    assert call.token_sent == "tok-fixture-0001"


def test_glab_routing_hostname_alone_sends_no_token_and_warns(
    run: Runner, forges: FakeForges
) -> None:
    forges.answer("glab", "user", body={})
    done = run(
        "glab",
        ["api", "--hostname", "gl.example.test", "user"],
        host="gitlab.com",
        env_extra={"GITLAB_TOKEN": "tok"},
    )
    call = only(forges)
    assert call.request["url"] == "https://gl.example.test/api/v4/user"  # type: ignore[index]
    assert call.token_sent is None
    assert "was not sent" in done.stderr


def test_glab_hostname_rejects_a_port(run: Runner, forges: FakeForges) -> None:
    done = run("glab", ["api", "--hostname", "gl.example.test:8443", "user"], host="gitlab.com")
    assert done.returncode == 1 and only(forges).request is None


def test_glab_full_url_endpoint_sends_its_token_to_the_named_host(
    run: Runner, forges: FakeForges
) -> None:
    forges.answer("glab", endpoint_re="user", body={})
    run(
        "glab",
        ["api", "--hostname", "gitlab.com", "https://elsewhere.example.test/api/v4/user"],
        host="gitlab.com",
    )
    call = only(forges)
    assert call.request["full_url_endpoint"] and call.token_sent  # type: ignore[index]
    assert call.request["url"].startswith("https://elsewhere.example.test")  # type: ignore[index]


def test_glab_with_no_credential_is_anonymous(run: Runner, forges: FakeForges) -> None:
    forges.answer("glab", "user", body={})
    done = run(
        "glab",
        ["api", "--hostname", "gitlab.com", "user"],
        host="gitlab.com",
        drop=("GITLAB_TOKEN",),
    )
    assert done.returncode == 0 and only(forges).token_sent is None


def test_glab_always_reports_repository_discovery_and_honors_the_ceiling(
    run: Runner, forges: FakeForges, tmp_path: Path
) -> None:
    forges.answer("glab", "user", body={})
    run("glab", ["api", "--hostname", "gitlab.com", "user"], host="gitlab.com")
    assert only(forges).repo_context is None  # ceiling stops the walk at the private cwd


def test_a_planted_git_dir_is_found_without_a_ceiling(
    procs: ProcessRegistry, forges: FakeForges, tmp_path: Path
) -> None:
    forges.answer("glab", "user", body={})
    (tmp_path / "work" / ".git").mkdir(parents=True)
    (tmp_path / "work" / "sub").mkdir()
    env = {"PATH": forges.path(), "HOME": str(tmp_path), "GITLAB_TOKEN": "t"}
    procs.run(
        ["glab", "api", "--hostname", "gitlab.com", "user"], env=env, cwd=tmp_path / "work" / "sub"
    )
    assert only(forges).repo_context == str((tmp_path / "work").resolve())


def test_stored_login_in_the_config_dir_is_used_and_logged(
    run: Runner, forges: FakeForges, tmp_path: Path
) -> None:
    forges.answer("gh", "x", body={})
    cfg = tmp_path / "cfg"
    forges.stored_login(cfg, "gh", "github.com", "stored-tok")
    run(
        "gh",
        ["api", "--hostname", "github.com", "x"],
        drop=("GH_TOKEN", "GH_CONFIG_DIR"),
        env_extra={"GH_CONFIG_DIR": str(cfg)},
    )
    call = only(forges)
    assert (call.token_sent, call.request["token_source"]) == ("stored-tok", "file")  # type: ignore[index]


def test_keyring_login_needs_the_session_bus(run: Runner, forges: FakeForges) -> None:
    forges.keyring_login("gh", "github.com", "ring-tok")
    forges.answer("gh", "x", body={})
    no_bus = run("gh", ["api", "--hostname", "github.com", "x"], drop=("GH_TOKEN",))
    assert no_bus.returncode == 4
    with_bus = run(
        "gh",
        ["api", "--hostname", "github.com", "x"],
        drop=("GH_TOKEN",),
        env_extra={"DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"},
    )
    assert with_bus.returncode == 0 and forges.calls()[-1].token_sent == "ring-tok"


def test_environment_is_logged_as_received_not_as_python_rewrote_it(
    procs: ProcessRegistry, forges: FakeForges, tmp_path: Path
) -> None:
    forges.answer("gh", "x", body={})
    env = {"PATH": forges.path(), "HOME": str(tmp_path), "GH_TOKEN": "t"}
    procs.run(["gh", "api", "--hostname", "github.com", "x"], env=env)
    assert only(forges).env == env  # no LC_CTYPE or PWD added


def test_fixed_settings_constants_match_the_interface() -> None:
    assert FIXED_SETTINGS["GLAB_SEND_TELEMETRY"] == "false" and FIXED_SETTINGS["NO_PROMPT"] == "1"


# Signals and hangs.


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_is_logged_and_the_fake_dies_by_it(
    procs: ProcessRegistry, forges: FakeForges, tmp_path: Path, sig: signal.Signals
) -> None:
    forges.answer("gh", "slow", hang=True)
    env = {"PATH": forges.path(), "HOME": str(tmp_path), "GH_TOKEN": "t"}
    proc = procs.popen(["gh", "api", "--hostname", "github.com", "slow"], env=env)
    deadline = time.monotonic() + 20
    while not forges.calls() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert procs.signal(proc, sig)
    assert proc.wait(timeout=10) == -sig
    assert forges.calls()[0].signals == [sig.name]


def test_die_after_answering_kills_the_fake_with_that_signal(
    procs: ProcessRegistry, forges: FakeForges, tmp_path: Path
) -> None:
    forges.answer("gh", "x", body={"ok": 1}, die="SIGKILL")
    env = {"PATH": forges.path(), "HOME": str(tmp_path), "GH_TOKEN": "t"}
    done = procs.run(["gh", "api", "--hostname", "github.com", "x"], env=env)
    assert done.returncode == -signal.SIGKILL and json.loads(done.stdout) == {"ok": 1}
