"""The fake ``gh`` and ``glab``: argv grammar, exit codes, streams, routing, logging, signals."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from .harness.forges import FAKEBIN, Call, FakeForges
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


@pytest.mark.parametrize("backend", ["gh", "glab"])
def test_include_status_line_ends_lf_and_header_lines_crlf(
    run: Runner, forges: FakeForges, backend: str
) -> None:
    forges.answer(backend, "x", body={}, headers={"zeta": "1", "x-mixed-case": "3", "alpha": "2"})
    host = "github.com" if backend == "gh" else "gitlab.com"
    done = run(backend, ["api", "-i", "--hostname", host, "x"], host=host)
    assert done.stdout_bytes.startswith(b"HTTP/2.0 200 OK\nAlpha: 2\r\n")  # type: ignore[attr-defined]
    head = done.stdout.split("\r\n\r\n")[0].split("\n", 1)[1]
    assert head.split("\r\n") == [
        "Alpha: 2",
        "Content-Type: application/json; charset=utf-8",
        "X-Mixed-Case: 3",
        "Zeta: 1",
    ]


def test_http_error_exits_1_with_body_on_stdout_and_message_on_stderr(
    run: Runner, forges: FakeForges
) -> None:
    forges.answer("gh", "x", status=404, body={"message": "Not Found"})
    done = run("gh", ["api", "--hostname", "github.com", "x"])
    assert done.returncode == 1
    assert json.loads(done.stdout) == {"message": "Not Found"}
    assert done.stderr == "gh: Not Found (HTTP 404)\n"


@pytest.mark.parametrize(
    ("backend", "body", "stderr"),
    [
        ("gh", {}, "gh: HTTP 404\n"),
        ("glab", {"error": "insufficient_scope"}, "glab: HTTP 404\n"),
        ("glab", {"message": "404 Not found"}, "glab: 404 Not found (HTTP 404)\n"),
    ],
)
def test_http_error_without_a_message_names_only_the_status(
    run: Runner, forges: FakeForges, backend: str, body: dict[str, str], stderr: str
) -> None:
    forges.answer(backend, "x", status=404, body=body)
    host = "github.com" if backend == "gh" else "gitlab.com"
    done = run(backend, ["api", "--hostname", host, "x"], host=host)
    assert (done.returncode, done.stderr) == (1, stderr)


@pytest.mark.parametrize("backend", ["gh", "glab"])
def test_304_exits_1_and_prints_headers_with_include(
    run: Runner, forges: FakeForges, backend: str
) -> None:
    forges.answer(backend, "x", status=304, headers={"ETag": '"abc"'})
    host = "github.com" if backend == "gh" else "gitlab.com"
    done = run(backend, ["api", "-i", "--hostname", host, "x"], host=host)
    assert done.returncode == 1
    assert done.stdout.startswith("HTTP/2.0 304 Not Modified\n") and 'Etag: "abc"' in done.stdout
    assert done.stderr == f"{backend}: HTTP 304\n"


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
    # gh 2.87.3: a variable that does not apply to the host gives an anonymous request.
    forges.answer("gh", "x", body={})
    wrong = run("gh", ["api", "--hostname", "ghe.example.test", "x"], env_extra={"GH_TOKEN": "t"})
    assert wrong.returncode == 0
    assert forges.calls()[-1].token_sent is None
    ok = run(
        "gh",
        ["api", "--hostname", "ghe.example.test", "x"],
        host="ghe.example.test",
    )
    assert ok.returncode == 0
    assert forges.calls()[-1].request["url"] == "https://ghe.example.test/api/v3/x"  # type: ignore[index]
    assert forges.calls()[-1].token_sent == "tok-fixture-0001"


def test_no_gh_variable_at_all_exits_4_naming_gh_token(run: Runner, forges: FakeForges) -> None:
    done = run(
        "gh",
        ["api", "--hostname", "ghe.example.test", "x"],
        host="ghe.example.test",
        drop=("GH_ENTERPRISE_TOKEN",),
    )
    assert done.returncode == 4 and "populate the GH_TOKEN environment variable" in done.stderr


def test_gh_hostname_with_a_port_is_a_usage_error(run: Runner, forges: FakeForges) -> None:
    done = run("gh", ["api", "--hostname", "ghe.example.test:8443", "x"])
    assert done.returncode == 1
    assert "error parsing `--hostname`: invalid hostname" in done.stderr
    assert only(forges).request is None


def test_ghe_com_tenancy_takes_gh_token(run: Runner, forges: FakeForges) -> None:
    forges.answer("gh", "x", body={})
    done = run(
        "gh", ["api", "--hostname", "tenant-fixture.ghe.com", "x"], host="tenant-fixture.ghe.com"
    )
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


@pytest.mark.parametrize("backend", ["gh", "glab"])
@pytest.mark.parametrize("extra", [["-X", "GET"], ["--input", "-"]])
def test_fields_go_to_the_query_string_with_get_or_input(
    run: Runner, forges: FakeForges, backend: str, extra: list[str]
) -> None:
    forges.answer(backend, "x", body={})
    host = "github.com" if backend == "gh" else "gitlab.com"
    run(
        backend,
        ["api", "--hostname", host, *extra, "-f", "a=b", "-F", "n=3", "x"],
        host=host,
        stdin="{}",
    )
    url = only(forges).request["url"]  # type: ignore[index]
    assert url.endswith("/x?a=b&n=3"), url


def test_silent_with_include_prints_only_the_headers(run: Runner, forges: FakeForges) -> None:
    forges.answer("gh", "x", body={"id": 1})
    done = run("gh", ["api", "--silent", "-i", "--hostname", "github.com", "x"])
    assert done.stdout.startswith("HTTP/2.0 200 OK\n") and done.stdout.endswith("\r\n\r\n")


@pytest.mark.parametrize(
    ("flag", "include"), [("--include=false", False), ("--include=true", True), ("-i=0", False)]
)
def test_boolean_flag_values_are_parsed(
    run: Runner, forges: FakeForges, flag: str, include: bool
) -> None:
    forges.answer("gh", "x", body={})
    done = run("gh", ["api", flag, "--silent=false", "--hostname", "github.com", "x"])
    assert done.stdout.startswith("HTTP/2.0") is include
    assert done.stdout.endswith("{}")


def test_a_non_boolean_flag_value_is_rejected_like_parsebool(run: Runner) -> None:
    done = run("gh", ["api", "--include=yes", "--hostname", "github.com", "x"])
    assert done.returncode == 1
    assert 'invalid argument "yes" for "-i, --include" flag: strconv.ParseBool' in done.stderr


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
    forges.allow_not_modelled = True
    done = run("gh", ["api", "--hostname", "github.com", *flag, "x"])
    assert done.returncode == 70
    assert "not modelled" in done.stderr
    assert only(forges).not_modelled


@pytest.mark.parametrize("endpoint", ["repos/{owner}/{repo}/pulls", "projects/:id/x"])
def test_cli_placeholders_are_logged_and_refused(
    run: Runner, forges: FakeForges, endpoint: str
) -> None:
    backend = "gh" if "{" in endpoint else "glab"
    host = "github.com" if backend == "gh" else "gitlab.com"
    forges.allow_not_modelled = True
    done = run(backend, ["api", "--hostname", host, endpoint], host=host)
    assert done.returncode == 70
    assert only(forges).placeholders


def test_a_not_modelled_call_fails_teardown_even_with_allow_unmatched(
    run: Runner, forges: FakeForges
) -> None:
    run("gh", ["pr", "list"])
    forges.allow_unmatched = True
    with pytest.raises(AssertionError, match="does not model"):
        forges.check_teardown()
    forges.allow_not_modelled = True
    forges.check_teardown()


def test_unknown_subcommand_fails_loudly(run: Runner, forges: FakeForges) -> None:
    forges.allow_not_modelled = True
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


def test_a_default_answer_is_marked_defaulted_not_unmatched(
    run: Runner, forges: FakeForges
) -> None:
    forges.default_answer(200, {"ok": True})
    done = run("gh", ["api", "--hostname", "github.com", "anything"])
    assert done.returncode == 0
    call = only(forges)
    assert (call.defaulted, call.unmatched) == (True, False)


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
    # The design's glab 1.127.0 row; glab 1.89.0 has no `auth token` at all.
    done = run("glab", ["auth", "token", "--hostname", "gitlab.com"], host="gitlab.com")
    assert done.returncode == 1
    assert "\n   ERROR  \n" in done.stderr and "  Unknown flag: --hostname. " in done.stderr
    assert done.stderr.endswith("  Try --help for usage.\n\n")


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


def test_glab_gl_host_is_an_alias_of_gitlab_host(run: Runner, forges: FakeForges) -> None:
    call = glab_call(run, forges, [], {"GL_HOST": "gl.example.test"})
    assert call.request["url"] == "https://gl.example.test/api/v4/user"  # type: ignore[index]
    assert call.token_sent == "tok-fixture-0001"


def test_glab_graphql_keeps_the_prefix_and_errors_exit_1(run: Runner, forges: FakeForges) -> None:
    body = {"data": {"x": 1}, "errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}]}
    forges.answer("glab", "graphql", body=body)
    done = run(
        "glab",
        ["api", "graphql", "--input", "-"],
        host="gl.example.test",
        stdin="{}",
        env_extra={"GITLAB_HOST": "gl.example.test/pre"},
    )
    assert only(forges).request["url"] == "https://gl.example.test/pre/api/graphql"  # type: ignore[index]
    assert (done.returncode, json.loads(done.stdout)) == (1, body)
    assert done.stderr == "glab: Could not resolve\n"


def test_glab_oauth_token_is_sent_as_private_token(run: Runner, forges: FakeForges) -> None:
    forges.answer("glab", "user", body={})
    run(
        "glab",
        ["api", "--hostname", "gitlab.com", "user"],
        host="gitlab.com",
        drop=("GITLAB_TOKEN",),
        env_extra={"OAUTH_TOKEN": "oauth-tok"},
    )
    request = only(forges).request or {}
    assert (request["token_sent"], request["token_header"]) == ("oauth-tok", "Private-Token")


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


def test_glab_stored_login_with_real_indentation_is_used(
    run: Runner, forges: FakeForges, tmp_path: Path
) -> None:
    forges.answer("glab", "user", body={})
    cfg = tmp_path / "glcfg"
    cfg.mkdir()
    text = "host: gitlab.com\nhosts:\n    gitlab.com:\n        token: stored-gl\n"
    (cfg / "config.yml").write_text(text, encoding="utf-8")
    (cfg / "config.yml").chmod(0o600)
    run(
        "glab",
        ["api", "--hostname", "gitlab.com", "user"],
        host="gitlab.com",
        drop=("GITLAB_TOKEN",),
        env_extra={"GLAB_CONFIG_DIR": str(cfg)},
    )
    assert only(forges).token_sent == "stored-gl"


def test_glab_refuses_a_config_file_not_mode_0600(
    run: Runner, forges: FakeForges, tmp_path: Path
) -> None:
    cfg = tmp_path / "glcfg"
    forges.stored_login(cfg, "glab", "gitlab.com", "stored-gl")
    assert stat.S_IMODE((cfg / "config.yml").stat().st_mode) == 0o600
    (cfg / "config.yml").chmod(0o644)
    done = run(
        "glab",
        ["api", "--hostname", "gitlab.com", "user"],
        host="gitlab.com",
        env_extra={"GLAB_CONFIG_DIR": str(cfg)},
    )
    assert done.returncode == 2
    assert "has the permissions 644, but glab requires 600." in done.stderr
    assert only(forges).request is None


def test_glab_writes_its_config_on_first_run(run: Runner, forges: FakeForges) -> None:
    forges.answer("glab", "user", body={})
    run("glab", ["api", "--hostname", "gitlab.com", "user"], host="gitlab.com")
    call = only(forges)
    assert call.config_entries == []  # logged as found, before glab wrote anything
    cfg = Path(call.env["GLAB_CONFIG_DIR"])
    assert sorted(os.listdir(cfg)) == ["aliases.yml", "config.yml"]
    assert {stat.S_IMODE((cfg / n).stat().st_mode) for n in os.listdir(cfg)} == {0o600}


def test_glab_with_an_unwritable_config_dir_exits_2(
    run: Runner, forges: FakeForges, tmp_path: Path
) -> None:
    cfg = tmp_path / "ro"
    cfg.mkdir(mode=0o500)
    try:
        done = run(
            "glab",
            ["api", "--hostname", "gitlab.com", "user"],
            host="gitlab.com",
            env_extra={"GLAB_CONFIG_DIR": str(cfg)},
        )
    finally:
        cfg.chmod(0o700)
    assert done.returncode == 2
    assert done.stderr.startswith(f"failed to read configuration:  open {cfg}/.config.yml")
    assert done.stderr.endswith(": permission denied\n")


def test_gh_works_with_an_unwritable_config_dir(
    run: Runner, forges: FakeForges, tmp_path: Path
) -> None:
    forges.answer("gh", "x", body={})
    cfg = tmp_path / "ro"
    cfg.mkdir(mode=0o500)
    try:
        done = run(
            "gh", ["api", "--hostname", "github.com", "x"], env_extra={"GH_CONFIG_DIR": str(cfg)}
        )
    finally:
        cfg.chmod(0o700)
    assert done.returncode == 0


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


def test_keyring_login_needs_the_session_bus(
    run: Runner, forges: FakeForges, tmp_path: Path
) -> None:
    # design.md: with a bus, `gh auth token` prints it and `glab api` sends it; `gh api` refuses.
    forges.keyring_login("gh", "github.com", "ring-tok")
    forges.keyring_login("glab", "gitlab.com", "ring-gl")
    forges.answer("gh", "x", body={})
    forges.answer("glab", "user", body={})
    token_args = ["auth", "token", "--hostname", "github.com"]
    no_bus = run("gh", token_args, drop=("GH_TOKEN",))
    assert no_bus.returncode == 1
    runtime = str(tmp_path / "runtime")
    for bus in ({"DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}, {"XDG_RUNTIME_DIR": runtime}):
        done = run("gh", token_args, drop=("GH_TOKEN",), env_extra=bus)
        assert (done.returncode, done.stdout) == (0, "ring-tok\n")
        api = run("gh", ["api", "--hostname", "github.com", "x"], drop=("GH_TOKEN",), env_extra=bus)
        assert api.returncode == 4
        run(
            "glab",
            ["api", "--hostname", "gitlab.com", "user"],
            host="gitlab.com",
            drop=("GITLAB_TOKEN",),
            env_extra=bus,
        )
        assert forges.calls()[-1].token_sent == "ring-gl"


def test_environment_is_logged_as_received_not_as_python_rewrote_it(
    procs: ProcessRegistry, forges: FakeForges, tmp_path: Path
) -> None:
    forges.answer("gh", "x", body={})
    env = {"PATH": forges.path(), "HOME": str(tmp_path), "GH_TOKEN": "t"}
    procs.run(["gh", "api", "--hostname", "github.com", "x"], env=env)
    assert only(forges).env == env  # no LC_CTYPE or PWD added


def test_fixed_settings_constants_match_the_interface() -> None:
    assert FIXED_SETTINGS["GLAB_SEND_TELEMETRY"] == "false" and FIXED_SETTINGS["GLAB_NO_PROMPT"] == "1"


# Signals and hangs.


@pytest.mark.parametrize(
    ("sig", "text"),
    [
        (signal.SIGINT, "interrupt signal received."),
        (signal.SIGTERM, "terminated signal received."),
    ],
)
def test_glab_exits_1_with_a_message_on_sigint_and_sigterm(
    procs: ProcessRegistry, forges: FakeForges, tmp_path: Path, sig: signal.Signals, text: str
) -> None:
    forges.answer("glab", "slow", hang=True)
    env = {"PATH": forges.path(), "HOME": str(tmp_path), "GITLAB_TOKEN": "t"}
    proc = procs.popen(
        ["glab", "api", "--hostname", "gitlab.com", "slow"], env=env, stderr=subprocess.PIPE
    )
    deadline = time.monotonic() + 20
    while (
        not (forges.calls() and forges.calls()[0].request is None) and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    time.sleep(0.2)
    assert procs.signal(proc, sig)
    assert proc.wait(timeout=10) == 1
    assert proc.stderr is not None and proc.stderr.read().decode() == text + "\n"
    call = forges.calls()[0]
    assert (call.signals, call.exit) == ([sig.name], 1)


def test_a_scripted_hang_is_capped(
    procs: ProcessRegistry, forges: FakeForges, tmp_path: Path
) -> None:
    forges.answer("gh", "slow", hang=0.3)
    forges.allow_not_modelled = True
    env = {"PATH": forges.path(), "HOME": str(tmp_path), "GH_TOKEN": "t"}
    done = procs.run(["gh", "api", "--hostname", "github.com", "slow"], env=env, timeout=20)
    assert done.returncode == 70 and "hang capped" in done.stderr


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


# Redirects (design.md: Redirect from a fake server, Project rename).


def redirect_run(
    run: Runner, forges: FakeForges, backend: str, method: str, status: int, location: str
) -> tuple[subprocess.CompletedProcess[str], Call]:
    forges.answer(backend, "start", status=status, redirect=location)
    forges.answer(backend, endpoint_re="landed", body={"final": True}, headers={"X-Hop": "2"})
    host = "ghe.example.test" if backend == "gh" else "gitlab.com"
    args = ["api", "-i", "-X", method, "--input", "-", "start"]
    if backend == "gh":
        args[1:1] = ["--hostname", host]
    else:
        args[1:1] = ["--hostname", "gitlab.com"]
    done = run(backend, args, host=host, stdin='{"a":"b"}')
    return done, only(forges)


def test_gh_follows_a_redirect_and_prints_only_the_final_response(
    run: Runner, forges: FakeForges
) -> None:
    done, call = redirect_run(run, forges, "gh", "GET", 302, "/api/v3/landed")
    assert done.returncode == 0
    assert done.stdout.count("HTTP/2.0") == 1 and done.stdout.startswith("HTTP/2.0 200 OK\n")
    assert json.loads(done.stdout.split("\r\n\r\n", 1)[1]) == {"final": True}
    hop = call.requests[1]
    assert (hop["url"], hop["token_sent"]) == (
        "https://ghe.example.test/api/v3/landed",
        "tok-fixture-0001",
    )


@pytest.mark.parametrize(
    "location", ["https://other.example.test/landed", "https://ghe.example.test:443/landed"]
)
def test_gh_drops_the_token_on_a_redirect_to_another_host(
    run: Runner, forges: FakeForges, location: str
) -> None:
    _, call = redirect_run(run, forges, "gh", "GET", 302, location)
    assert [r["token_sent"] for r in call.requests] == ["tok-fixture-0001", None]


def test_glab_repeats_its_token_on_a_redirect_to_another_host(
    run: Runner, forges: FakeForges
) -> None:
    _, call = redirect_run(run, forges, "glab", "GET", 302, "https://other.example.test/landed")
    assert [r["token_sent"] for r in call.requests] == ["tok-fixture-0001"] * 2
    assert call.requests[1]["host"] == "other.example.test"


@pytest.mark.parametrize(
    ("status", "method", "body"),
    [
        (301, "GET", None),
        (302, "GET", None),
        (307, "POST", '{"a":"b"}'),
        (308, "POST", '{"a":"b"}'),
    ],
)
def test_a_redirected_post_follows_go_method_rules(
    run: Runner, forges: FakeForges, status: int, method: str, body: str | None
) -> None:
    _, call = redirect_run(run, forges, "gh", "POST", status, "/api/v3/landed")
    hop = call.requests[1]
    assert (hop["method"], hop["body"]) == (method, body)


# The developer launchers in tests/fakebin.


@pytest.mark.parametrize("backend", ["gh", "glab"])
def test_the_developer_launcher_refuses_a_state_dir_in_the_repo(
    procs: ProcessRegistry, tmp_path: Path, backend: str
) -> None:
    path = os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"])
    base = {"PATH": path, "HOME": str(tmp_path), "GH_TOKEN": "t", "GITLAB_TOKEN": "t"}
    launcher = str(FAKEBIN / backend)
    for state in (None, str(FAKEBIN), "relative"):
        env = dict(base) if state is None else {**base, "FORGEFAKE_STATE": state}
        done = procs.run([launcher, "--version"], env=env, cwd=tmp_path)
        assert done.returncode == 64 and "FORGEFAKE_STATE" in done.stderr
    assert not (FAKEBIN / "calls.jsonl").exists()
    state = tmp_path / "state"
    state.mkdir()
    done = procs.run(
        [launcher, "--version"], env={**base, "FORGEFAKE_STATE": str(state)}, cwd=tmp_path
    )
    assert done.returncode == 0 and (state / "calls.jsonl").exists()
