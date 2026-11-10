"""Hosts and credentials (#8): backend per host, token selection, ``token_command``, ``me``.

Credential rules are security-critical, so these tests run the real gateway against the fake
``gh`` and ``glab`` of ``tests/fakebin`` and real child processes for ``token_command``, and assert
on what the fakes logged: the token variable, argv, and where each token went.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import threading
import traceback
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from forgectl import hosts as hosts_module
from forgectl.config import Host, LoadedConfig, Me
from forgectl.errors import ForgectlError
from forgectl.gateway import NO_BUS_ADDRESS, Budget, Gateway
from forgectl.hosts import Hosts, matches_login, redact_secrets, secret_values

from .harness.fixtures import forgectl_env
from .harness.forges import Call, FakeForges
from .harness.safety import AMBIENT_SENTINELS, assert_call_safe
from .test_gateway import Spawned, private_leftovers

TOKEN = "tok-env-0001-aaaa"
CMD_TOKEN = "tok-cmd-0002-bbbb"
STDERR_SECRET = "SECRET-IN-STDERR-77"


@pytest.fixture(autouse=True)
def clean_secrets() -> Iterator[None]:
    hosts_module.clear_secrets()
    yield
    hosts_module.clear_secrets()


@pytest.fixture
def spawned() -> Spawned:
    return Spawned()


def config_of(hosts: Mapping[str, Host] | None = None, me: Me | None = None) -> LoadedConfig:
    base = LoadedConfig.defaults()
    update: dict[str, Any] = {"hosts": dict(hosts or {})}
    if me is not None:
        update["me"] = me
    return LoadedConfig(
        config=base.config.model_copy(update=update), config_hash="x", config_path=None
    )


def make_hosts(
    forges: FakeForges,
    config: LoadedConfig | None = None,
    *,
    env: Mapping[str, str | None] | None = None,
    spawned: Spawned | None = None,
    timeout: float = 20.0,
) -> Hosts:
    gateway = Gateway(
        config or LoadedConfig.defaults(),
        timeout=timeout,
        environ=forgectl_env(forges, env),
        on_spawn=spawned,
    )
    return Hosts(gateway)


def py(code: str, *args: str) -> tuple[str, ...]:
    # config refuses control characters in an argument, so a multi-line program goes in escaped
    return (sys.executable, "-I", "-c", f"exec({textwrap.dedent(code)!r})", *args)


def printing(text: str) -> tuple[str, ...]:
    """A token_command that writes ``text`` (exact bytes of the repr) to stdout."""
    return py(
        "import sys; sys.stdout.buffer.write(eval(sys.argv[1]))", repr(text.encode("latin-1"))
    )


def only_call(forges: FakeForges) -> Call:
    calls = forges.calls()
    assert len(calls) == 1, calls
    return calls[0]


def login(backend: str, host: str) -> None:
    """A stored login for ``host`` in the isolated config dir the fakes read."""
    variable = "GH_CONFIG_DIR" if backend == "gh" else "GLAB_CONFIG_DIR"
    FakeForges.stored_login(Path(os.environ[variable]), backend, host, "stored-token-5")


def user_answer(forges: FakeForges, backend: str, host: str, login: str = "octo") -> None:
    if backend == "gh":
        body = {"login": login, "id": 12, "html_url": f"https://{host}/{login}"}
    else:
        body = {"username": login, "id": 12, "web_url": f"https://{host}/{login}"}
    forges.answer(backend, "user", body=body)


# -- backend per host ---------------------------------------------------------------------


def test_backend_of_built_in_and_declared_hosts(fake_forges: FakeForges) -> None:
    cfg = config_of({"git.example.test": Host(backend="gitlab", api_prefix="/gl")})
    hosts = make_hosts(fake_forges, cfg)
    assert hosts.backend("github.com") == "github"
    assert hosts.backend("gitlab.com") == "gitlab"
    assert hosts.backend("git.example.test") == "gitlab"


def test_an_undeclared_host_is_refused_before_any_process(fake_forges: FakeForges) -> None:
    hosts = make_hosts(fake_forges)
    with pytest.raises(ForgectlError) as raised:
        hosts.target("evil.example.test")
    assert raised.value.code == "refused" and raised.value.reason == "unknown_host"
    assert fake_forges.calls() == []


def test_target_carries_the_declared_api_prefix_and_port(fake_forges: FakeForges) -> None:
    cfg = config_of({"git.example.test:8443": Host(backend="gitlab", api_prefix="/gitlab")})
    target = make_hosts(fake_forges, cfg).target("git.example.test:8443")
    assert (target.backend, target.host, target.api_prefix) == (
        "gitlab",
        "git.example.test:8443",
        "/gitlab",
    )
    assert target.token is None


def test_built_in_hosts_never_get_an_api_prefix(fake_forges: FakeForges) -> None:
    assert make_hosts(fake_forges).target("gitlab.com").api_prefix == ""


# -- exactly one token variable, the right one --------------------------------------------

VARIABLES = [
    ("github", "github.com", "GH_TOKEN", None),
    ("github", "acme.ghe.com", "GH_TOKEN", None),
    ("github", "ghe.example.test", "GH_ENTERPRISE_TOKEN", None),
    ("gitlab", "gitlab.com", "GITLAB_TOKEN", None),
    ("gitlab", "git.example.test", "GITLAB_TOKEN", None),
    ("gitlab", "git.example.test:8443", "GITLAB_TOKEN", "/gitlab"),
]


@pytest.mark.parametrize(("backend", "host", "variable", "prefix"), VARIABLES)
def test_token_variables_exactly_one_per_backend_and_host_and_no_ambient_token(
    fake_forges: FakeForges, backend: str, host: str, variable: str, prefix: str | None
) -> None:
    program = "gh" if backend == "github" else "glab"
    built_in = host in ("github.com", "gitlab.com")
    declared = {} if built_in else {host: Host(backend=backend, api_prefix=prefix)}  # type: ignore[arg-type]
    declared[host] = declared.get(host, Host()).model_copy(update={"token_env": "MY_TOKEN"})
    hosts = make_hosts(fake_forges, config_of(declared), env={"MY_TOKEN": TOKEN})
    user_answer(fake_forges, program, host)
    target = hosts.target(host)
    assert target.token_variable == variable
    hosts.gateway.api(target, "GET", ["user"])
    call = only_call(fake_forges)
    gitlab_host = None if host == "gitlab.com" or backend == "github" else host + (prefix or "")
    assert_call_safe(call, host=host, mode="token", gitlab_host=gitlab_host)
    assert call.token_sent == TOKEN
    assert [n for n in AMBIENT_SENTINELS if n in call.env] == [variable]
    assert call.env[variable] == TOKEN
    for value in AMBIENT_SENTINELS.values():  # forgectl_env planted every ambient variable
        assert value not in json.dumps(call.env) and value != call.token_sent


def test_an_ambient_token_is_never_used_without_naming_it(fake_forges: FakeForges) -> None:
    hosts = make_hosts(fake_forges, env={"GH_TOKEN": "ambient-looks-valid"})
    target = hosts.target("github.com")
    assert target.token is None
    assert hosts.credential("github.com").source == "stored_login"


def test_naming_an_ambient_variable_uses_it(fake_forges: FakeForges) -> None:
    cfg = config_of({"gitlab.com": Host(token_env="GITLAB_ACCESS_TOKEN")})
    hosts = make_hosts(fake_forges, cfg, env={"GITLAB_ACCESS_TOKEN": TOKEN})
    user_answer(fake_forges, "glab", "gitlab.com")
    hosts.gateway.api(hosts.target("gitlab.com"), "GET", ["user"])
    call = only_call(fake_forges)
    assert call.env["GITLAB_TOKEN"] == TOKEN
    assert "GITLAB_ACCESS_TOKEN" not in call.env


# -- a configured credential that yields nothing ------------------------------------------


@pytest.mark.parametrize("value", [None, ""])
def test_token_env_unset_or_empty_is_auth_required_naming_the_variable_and_never_the_login(
    fake_forges: FakeForges, tmp_path: Path, value: str | None
) -> None:
    FakeForges.stored_login(tmp_path / "gh", "gh", "github.com", "stored-token-1")
    fake_forges.keyring_login("gh", "github.com", "keyring-token-1")
    cfg = config_of({"github.com": Host(token_env="MY_TOKEN")})
    env: dict[str, str | None] = {"GH_CONFIG_DIR": str(tmp_path / "gh"), "MY_TOKEN": value}
    hosts = make_hosts(fake_forges, cfg, env=env)
    with pytest.raises(ForgectlError) as raised:
        hosts.target("github.com")
    err = raised.value
    assert err.code == "auth_required" and err.exit_code == 12
    assert (err.details or {}).get("variable") == "MY_TOKEN"
    assert "hosts.github.com.token_env" in err.message
    assert fake_forges.calls() == []  # the stored login was never tried


def test_a_failing_token_env_is_refused_the_same_way_on_every_later_target(
    fake_forges: FakeForges,
) -> None:
    cfg = config_of({"github.com": Host(token_env="MY_TOKEN")})
    hosts = make_hosts(fake_forges, cfg)
    for _ in range(2):  # a repeated call fails the same way
        with pytest.raises(ForgectlError) as raised:
            hosts.target("github.com")
        assert raised.value.code == "auth_required"


@pytest.mark.parametrize(
    "bad",
    ["has space", "tab\there", "new\nline", "café", "x" * 5000, "\x00z", " ", "a\r"],
)
def test_a_token_env_value_that_is_not_one_printable_word_fails_without_echo(
    fake_forges: FakeForges, bad: str
) -> None:
    cfg = config_of({"github.com": Host(token_env="MY_TOKEN")})
    hosts = make_hosts(fake_forges, cfg, env={"MY_TOKEN": bad})
    with pytest.raises(ForgectlError) as raised:
        hosts.target("github.com")
    err = raised.value
    assert err.code == "auth_required" and (err.details or {}).get("variable") == "MY_TOKEN"
    shown = json.dumps(err.envelope())
    assert bad.strip() == "" or bad not in shown
    assert secret_values() == frozenset()


def test_token_env_wins_and_the_command_is_not_run(
    fake_forges: FakeForges, tmp_path: Path, spawned: Spawned
) -> None:
    marker = tmp_path / "ran"
    cmd = py("import sys; open(sys.argv[1],'w').close(); print('x')", str(marker))
    cfg = config_of({"github.com": Host(token_env="MY_TOKEN", token_command=cmd)})
    hosts = make_hosts(fake_forges, cfg, env={"MY_TOKEN": TOKEN}, spawned=spawned)
    credential = hosts.credential("github.com")
    assert (credential.source, credential.token) == ("token_env", TOKEN)
    assert not marker.exists() and spawned.children == []


def test_token_command_is_used_when_token_env_is_unset(fake_forges: FakeForges) -> None:
    cfg = config_of(
        {"github.com": Host(token_env="MY_TOKEN", token_command=printing(CMD_TOKEN + "\n"))}
    )
    credential = make_hosts(fake_forges, cfg).credential("github.com")
    assert (credential.source, credential.token) == ("token_command", CMD_TOKEN)


def test_both_configured_and_neither_yielding_names_the_token_env_variable(
    fake_forges: FakeForges,
) -> None:
    cfg = config_of({"github.com": Host(token_env="MY_TOKEN", token_command=printing(""))})
    with pytest.raises(ForgectlError) as raised:
        make_hosts(fake_forges, cfg).target("github.com")
    assert (raised.value.details or {}).get("variable") == "MY_TOKEN"


def test_only_token_command_failing_has_no_variable_in_details(fake_forges: FakeForges) -> None:
    cfg = config_of({"github.com": Host(token_command=printing(""))})
    with pytest.raises(ForgectlError) as raised:
        make_hosts(fake_forges, cfg).target("github.com")
    assert raised.value.code == "auth_required"
    assert "variable" not in (raised.value.details or {})
    assert "hosts.github.com.token_command" in raised.value.message


# -- token_command ------------------------------------------------------------------------


def test_token_command_gets_the_host_substituted_and_nothing_else_changed(
    fake_forges: FakeForges,
) -> None:
    code = "import sys; print('|'.join(sys.argv[1:]))"
    cmd = py(code, "{host}", "x{host}", "{HOST}", "$(id);x", "--flag={host}", "{host}{host}")
    cfg = config_of({"git.example.test:8443": Host(backend="gitlab", token_command=cmd)})
    got = make_hosts(fake_forges, cfg).credential("git.example.test:8443").token
    assert got == ("git.example.test:8443|x{host}|{HOST}|$(id);x|--flag={host}|{host}{host}")


def test_token_command_runs_without_a_shell(fake_forges: FakeForges, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    cmd = py("import sys; print('ok-' + str(len(sys.argv)))", f"; touch {victim}", "`id`", "&&x")
    cfg = config_of({"github.com": Host(token_command=cmd)})
    assert make_hosts(fake_forges, cfg).credential("github.com").token == "ok-4"
    assert not victim.exists()


def test_token_command_has_no_ambient_token_and_a_private_empty_cwd_and_null_stdin(
    fake_forges: FakeForges,
) -> None:
    code = """
        import os, sys
        bad = [k for k in os.environ if 'TOKEN' in k or k in ('GH_HOST', 'GH_REPO')]
        data = sys.stdin.buffer.read()
        ok = not bad and not os.listdir('.') and data == b''
        print('fine' if ok else 'bad-' + ','.join(bad))
    """
    cfg = config_of({"github.com": Host(token_command=py(code))})
    env = {"GH_TOKEN": "amb1", "GITHUB_TOKEN": "amb2", "GH_HOST": "evil.example.test"}
    assert make_hosts(fake_forges, cfg, env=env).credential("github.com").token == "fine"


@pytest.mark.parametrize(
    "output",
    [
        "",
        "\n",
        "\n\n",
        "a b\n",
        " S3cr3t\n",
        "S3cr3t \n",
        "a\nb\n",
        "S3cr3t\n\n",
        "S3cr3t\r\n",
        "S3cr3t\x00\n",
        "S3cr3t\x1b[0m\n",
        "S3cr3t\tx\n",
        "S3cr3t\x7f\n",
        "x" * 4097 + "\n",
    ],
)
def test_token_command_output_is_strict_and_a_refusal_never_echoes_it(
    fake_forges: FakeForges, output: str
) -> None:
    cfg = config_of({"github.com": Host(token_command=printing(output))})
    hosts = make_hosts(fake_forges, cfg)
    with pytest.raises(ForgectlError) as raised:
        hosts.target("github.com")
    assert raised.value.code == "auth_required"
    shown = json.dumps(raised.value.envelope())
    for piece in output.split():
        assert piece not in shown or len(piece) < 3
    assert secret_values() == frozenset()


@pytest.mark.parametrize("output", ["tok-é\n".encode(), b"\xff\xfe\n", b"tok\xc3\n"])
def test_token_command_output_that_is_not_ascii_fails(
    fake_forges: FakeForges, output: bytes
) -> None:
    cmd = py("import sys; sys.stdout.buffer.write(eval(sys.argv[1]))", repr(output))
    cfg = config_of({"github.com": Host(token_command=cmd)})
    with pytest.raises(ForgectlError) as raised:
        make_hosts(fake_forges, cfg).target("github.com")
    assert raised.value.code == "auth_required"


def test_token_command_accepts_the_maximum_length_and_a_missing_trailing_newline(
    fake_forges: FakeForges,
) -> None:
    longest = "t" * 4096
    cfg = config_of(
        {
            "github.com": Host(token_command=printing(longest)),
            "gitlab.com": Host(token_command=printing("glpat-abc_DEF.1~2\n")),
        }
    )
    hosts = make_hosts(fake_forges, cfg)
    assert hosts.credential("github.com").token == longest
    assert hosts.credential("gitlab.com").token == "glpat-abc_DEF.1~2"


def test_token_command_nonzero_exit_fails_even_with_a_plausible_token(
    fake_forges: FakeForges,
) -> None:
    code = f"import sys; print('{CMD_TOKEN}'); sys.stderr.write('{STDERR_SECRET}'); sys.exit(3)"
    cfg = config_of({"github.com": Host(token_command=py(code))})
    with pytest.raises(ForgectlError) as raised:
        make_hosts(fake_forges, cfg).target("github.com")
    shown = json.dumps(raised.value.envelope())
    assert raised.value.code == "auth_required"
    assert CMD_TOKEN not in shown and STDERR_SECRET not in shown
    assert secret_values() == frozenset()


def test_token_command_stderr_is_never_shown_on_success_or_failure(
    fake_forges: FakeForges, capfd: pytest.CaptureFixture[str]
) -> None:
    ok = f"import sys; sys.stderr.write('{STDERR_SECRET}'); print('{CMD_TOKEN}')"
    cfg = config_of({"github.com": Host(token_command=py(ok))})
    hosts = make_hosts(fake_forges, cfg)
    assert hosts.credential("github.com").token == CMD_TOKEN
    captured = capfd.readouterr()
    assert STDERR_SECRET not in captured.out + captured.err


def test_token_command_too_much_output_fails_and_stops_the_child(
    fake_forges: FakeForges, spawned: Spawned
) -> None:
    code = "import sys\nwhile True: sys.stdout.write('x' * 65536); sys.stdout.flush()"
    cfg = config_of({"github.com": Host(token_command=py(code))})
    with pytest.raises(ForgectlError) as raised:
        make_hosts(fake_forges, cfg, spawned=spawned).target("github.com")
    assert raised.value.code == "auth_required"
    assert "more than 4098 bytes" in raised.value.message
    assert "status" not in raised.value.message
    spawned.assert_gone()


def test_token_command_output_over_the_cap_names_the_size_not_the_kill(
    fake_forges: FakeForges,
) -> None:
    cfg = config_of({"github.com": Host(token_command=printing("x" * 5000))})
    with pytest.raises(ForgectlError) as raised:
        make_hosts(fake_forges, cfg).target("github.com")
    assert "gave more than 4098 bytes" in raised.value.message
    assert "status" not in raised.value.message


def test_token_command_timeout_kills_the_group_and_is_auth_required(
    fake_forges: FakeForges, spawned: Spawned, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(hosts_module, "TOKEN_COMMAND_TIMEOUT", 0.5)
    code = "import time; time.sleep(30)"  # the group kill itself is tested in test_gateway
    cfg = config_of({"github.com": Host(token_command=py(code))})
    with pytest.raises(ForgectlError) as raised:
        make_hosts(fake_forges, cfg, spawned=spawned).target("github.com")
    assert raised.value.code == "auth_required"
    assert "timed out" in raised.value.message
    spawned.assert_gone()
    assert private_leftovers() == []


def test_token_command_timeout_is_ten_seconds() -> None:
    assert hosts_module.TOKEN_COMMAND_TIMEOUT == 10.0


def test_token_command_missing_executable_is_auth_required(fake_forges: FakeForges) -> None:
    cfg = config_of({"github.com": Host(token_command=("no-such-token-helper", "x"))})
    with pytest.raises(ForgectlError) as raised:
        make_hosts(fake_forges, cfg).target("github.com")
    assert raised.value.code == "auth_required"
    assert "not found" in raised.value.message


def test_token_command_runs_once_per_host_and_failures_are_remembered(
    fake_forges: FakeForges, tmp_path: Path
) -> None:
    counter = tmp_path / "count"
    ok = py(
        "import sys; open(sys.argv[1], 'a').write('x'); print('tok-' + sys.argv[2])",
        str(counter),
        "{host}",
    )
    bad = py("import sys; open(sys.argv[1], 'a').write('y')", str(counter))
    cfg = config_of(
        {
            "github.com": Host(token_command=ok),
            "gitlab.com": Host(token_command=ok),
            "ghe.example.test": Host(backend="github", token_command=bad),
        }
    )
    hosts = make_hosts(fake_forges, cfg)
    for _ in range(3):
        assert hosts.target("github.com").token == "tok-github.com"
        assert hosts.target("gitlab.com").token == "tok-gitlab.com"
        with pytest.raises(ForgectlError):
            hosts.target("ghe.example.test")
    assert sorted(counter.read_text()) == ["x", "x", "y"]


def test_the_token_never_lands_on_disk_in_argv_or_in_the_private_directory(
    fake_forges: FakeForges, tmp_path: Path
) -> None:
    cfg = config_of({"github.com": Host(token_command=printing(CMD_TOKEN))})
    hosts = make_hosts(fake_forges, cfg)
    user_answer(fake_forges, "gh", "github.com")
    hosts.gateway.api(hosts.target("github.com"), "GET", ["user"])
    call = only_call(fake_forges)
    assert not [a for a in call.argv if CMD_TOKEN in a]
    assert private_leftovers() == []
    roots = [
        Path(os.environ["HOME"]),
        Path(os.environ["TMPDIR"]),
        Path(os.environ["GH_CONFIG_DIR"]),
    ]
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file():
                assert CMD_TOKEN.encode() not in path.read_bytes(), path


def test_token_command_runs_for_the_hosts_backend_bus_rule(
    fake_forges: FakeForges, tmp_path: Path
) -> None:
    code = "import os; print(os.environ.get('DBUS_SESSION_BUS_ADDRESS', 'unset').replace(':', '_'))"
    cfg = config_of({"github.com": Host(token_command=py(code))})
    env = {"GH_CONFIG_DIR": str(tmp_path / "gh"), "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"}
    got = make_hosts(fake_forges, cfg, env=env).credential("github.com").token
    assert got == "unix_path=/nonexistent/forgectl-no-bus"


# -- stored login -------------------------------------------------------------------------


def test_no_credential_configured_is_stored_login_mode_with_no_token_variable(
    fake_forges: FakeForges, tmp_path: Path
) -> None:
    FakeForges.stored_login(tmp_path / "gh", "gh", "github.com", "stored-token-9")
    hosts = make_hosts(fake_forges, env={"GH_CONFIG_DIR": str(tmp_path / "gh")})
    user_answer(fake_forges, "gh", "github.com")
    target = hosts.target("github.com")
    assert target.token is None
    hosts.gateway.api(target, "GET", ["user"])
    call = only_call(fake_forges)
    assert_call_safe(call, host="github.com", mode="stored", caller_config_dir=True)
    assert call.token_sent == "stored-token-9"


# -- a token for one host is never sent to another ----------------------------------------


def test_a_token_for_host_a_is_never_sent_to_host_b(fake_forges: FakeForges) -> None:
    login("gh", "other.example.test")
    cfg = config_of(
        {
            "ghe.example.test": Host(backend="github", token_env="A_TOKEN"),
            "other.example.test": Host(backend="github"),
        }
    )
    hosts = make_hosts(fake_forges, cfg, env={"A_TOKEN": TOKEN})
    user_answer(fake_forges, "gh", "other.example.test")
    target_b = hosts.target("other.example.test")
    assert target_b.token is None
    hosts.gateway.api(target_b, "GET", ["user"])
    call = only_call(fake_forges)
    assert not [n for n in AMBIENT_SENTINELS if n in call.env]
    assert TOKEN not in json.dumps(call.env) and TOKEN not in " ".join(call.argv)
    assert call.token_sent == "stored-token-5"  # only the host's own stored login, never A's


def test_two_github_hosts_each_get_only_their_own_token_and_variable(
    fake_forges: FakeForges,
) -> None:
    cfg = config_of(
        {
            "github.com": Host(token_env="T_ONE"),
            "ghe.example.test": Host(backend="github", token_env="T_TWO"),
        }
    )
    hosts = make_hosts(fake_forges, cfg, env={"T_ONE": "tok-one-1111", "T_TWO": "tok-two-2222"})
    user_answer(fake_forges, "gh", "github.com")
    user_answer(fake_forges, "gh", "ghe.example.test")
    hosts.gateway.api(hosts.target("github.com"), "GET", ["user"])
    hosts.gateway.api(hosts.target("ghe.example.test"), "GET", ["user"])
    one, two = fake_forges.calls()
    assert one.env["GH_TOKEN"] == "tok-one-1111" and "GH_ENTERPRISE_TOKEN" not in one.env
    assert two.env["GH_ENTERPRISE_TOKEN"] == "tok-two-2222" and "GH_TOKEN" not in two.env
    assert "tok-one-1111" not in json.dumps(two.env)
    assert "tok-two-2222" not in json.dumps(one.env)


def test_the_host_is_compared_in_lowercase_for_the_token_variable(
    fake_forges: FakeForges,
) -> None:
    cfg = config_of({"github.com": Host(token_env="MY_TOKEN")})
    hosts = make_hosts(fake_forges, cfg, env={"MY_TOKEN": TOKEN})
    target = hosts.target("GitHub.com")
    assert target.host == "github.com" and target.token_variable == "GH_TOKEN"


# -- exact-value redaction ----------------------------------------------------------------


def test_the_tokens_in_use_are_known_by_value_for_redaction(fake_forges: FakeForges) -> None:
    cfg = config_of(
        {
            "github.com": Host(token_env="MY_TOKEN"),
            "gitlab.com": Host(token_command=printing("shapeless-secret~1\n")),
        }
    )
    hosts = make_hosts(fake_forges, cfg, env={"MY_TOKEN": TOKEN})
    hosts.target("github.com")
    hosts.target("gitlab.com")
    assert secret_values() == frozenset({TOKEN, "shapeless-secret~1"})
    text = f"a {TOKEN} b shapeless-secret~1 c shapeless%2Dsecret"
    assert redact_secrets(text) == "a [redacted] b [redacted] c shapeless%2Dsecret"
    assert redact_secrets("nothing here") == "nothing here"


def test_redaction_covers_percent_encoded_forms() -> None:
    hosts_module.register_secret("a/b+c d~e")
    assert "a/b" not in redact_secrets("x a/b+c d~e y a%2Fb%2Bc%20d~e z a%2Fb%2Bc+d~e")
    assert redact_secrets("a%2Fb%2Bc%20d~e") == "[redacted]"


def test_redaction_is_longest_first_so_a_prefix_secret_leaves_no_tail() -> None:
    hosts_module.register_secret("abcd")
    hosts_module.register_secret("abcdefgh")
    assert redact_secrets("x abcdefgh y") == "x [redacted] y"


def test_a_token_that_is_never_validated_is_never_registered(fake_forges: FakeForges) -> None:
    cfg = config_of({"github.com": Host(token_env="MY_TOKEN")})
    with pytest.raises(ForgectlError):
        make_hosts(fake_forges, cfg, env={"MY_TOKEN": "bad token"}).target("github.com")
    assert secret_values() == frozenset()


# -- me -----------------------------------------------------------------------------------


def test_me_prefers_the_host_then_the_backend_then_nothing(fake_forges: FakeForges) -> None:
    cfg = config_of(
        {
            "github.com": Host(me="host-me"),
            "ghe.example.test": Host(backend="github"),
            "gitlab.com": Host(),
        },
        me=Me(github="global-gh", gitlab="global-gl"),
    )
    hosts = make_hosts(fake_forges, cfg)
    assert hosts.me("github.com") == "host-me"
    assert hosts.me("ghe.example.test") == "global-gh"
    assert hosts.me("gitlab.com") == "global-gl"
    assert make_hosts(fake_forges, config_of()).me("github.com") is None


def test_a_me_value_is_checked_under_the_backends_own_grammar(fake_forges: FakeForges) -> None:
    cfg = config_of(
        {"git.example.test": Host(backend="gitlab", me="a.b")}, me=Me(github="ok", gitlab="a.b")
    )
    assert make_hosts(fake_forges, cfg).me("git.example.test") == "a.b"
    # GitLab-only characters are not a GitHub login; config validated the union, not the backend.
    bad = config_of({"github.com": Host(me="dot.name")})
    with pytest.raises(ForgectlError) as raised:
        make_hosts(fake_forges, bad).me("github.com")
    assert raised.value.code == "config_invalid"
    assert raised.value.details == {"key": "hosts.github.com.me"}
    bad_global = config_of(me=Me(github="dot.name"))
    with pytest.raises(ForgectlError) as raised:
        make_hosts(fake_forges, bad_global).me("github.com")
    assert raised.value.details == {"key": "me.github"}


@pytest.mark.parametrize(
    ("me", "login", "expected"),
    [
        ("Octo", "octo", True),
        ("octo", "OCTO", True),
        ("octo", "other", False),
        ("octo[bot]", "octo", False),
        (None, "octo", None),
        ("octo", None, None),
        (None, None, None),
    ],
)
def test_me_matches_login_compares_case_insensitively_and_is_null_when_unknown(
    me: str | None, login: str | None, expected: bool | None
) -> None:
    assert matches_login(me, login) is expected


# -- identity (whoami's per-host entry) ---------------------------------------------------


def test_identity_with_a_configured_token(fake_forges: FakeForges) -> None:
    cfg = config_of({"github.com": Host(token_env="MY_TOKEN", me="Octo")})
    hosts = make_hosts(fake_forges, cfg, env={"MY_TOKEN": TOKEN})
    fake_forges.answer(
        "gh",
        "user",
        body={"login": "octo", "id": 12, "html_url": "https://github.com/octo"},
        headers={"x-oauth-scopes": "repo, read:org"},
    )
    entry = hosts.identity("github.com")
    assert entry.token_source == "token_env"
    assert (entry.login, entry.id, entry.me, entry.me_matches_login) == ("octo", "12", "Octo", True)
    assert entry.scopes == ("repo", "read:org")
    assert entry.error is None
    assert TOKEN not in json.dumps(entry.as_json())


def test_identity_me_defaults_to_the_authenticated_login(fake_forges: FakeForges) -> None:
    login("gh", "github.com")
    hosts = make_hosts(fake_forges)
    user_answer(fake_forges, "gh", "github.com", "octo")
    entry = hosts.identity("github.com")
    assert entry.token_source == "stored_login"
    assert (entry.login, entry.me, entry.me_matches_login) == ("octo", "octo", True)
    assert entry.scopes is None


def test_identity_flags_a_me_that_differs_from_the_account(fake_forges: FakeForges) -> None:
    login("gh", "github.com")
    hosts = make_hosts(fake_forges, config_of({"github.com": Host(me="someone-else")}))
    user_answer(fake_forges, "gh", "github.com", "octo")
    entry = hosts.identity("github.com")
    assert (entry.me, entry.me_matches_login) == ("someone-else", False)


def test_identity_on_gitlab_reads_the_username_and_web_url(fake_forges: FakeForges) -> None:
    login("glab", "git.example.test:8443")
    cfg = config_of({"git.example.test:8443": Host(backend="gitlab", api_prefix="/gl")})
    hosts = make_hosts(fake_forges, cfg)
    user_answer(fake_forges, "glab", "git.example.test:8443", "gl.user")
    entry = hosts.identity("git.example.test:8443")
    assert (entry.login, entry.id, entry.backend) == ("gl.user", "12", "gitlab")


def test_identity_with_no_credential_and_no_login_is_none_without_an_error(
    fake_forges: FakeForges,
) -> None:
    fake_forges.answer("gh", "user", status=401, body={"message": "Bad credentials"})
    entry = make_hosts(fake_forges).identity("github.com")
    assert entry.token_source == "none" and entry.error is None
    assert (entry.login, entry.id, entry.me_matches_login, entry.scopes) == (None, None, None, None)


def test_identity_with_no_binary_and_no_credential_is_none_without_an_error(
    fake_forges: FakeForges,
) -> None:
    hosts = make_hosts(fake_forges, env={"PATH": "/nonexistent-dir-for-test"})
    entry = hosts.identity("github.com")
    assert entry.token_source == "none" and entry.error is None


def test_identity_with_a_failing_configured_credential_reports_an_error_entry(
    fake_forges: FakeForges,
) -> None:
    cfg = config_of({"github.com": Host(token_env="MY_TOKEN")})
    entry = make_hosts(fake_forges, cfg).identity("github.com")
    assert entry.token_source == "none"
    assert entry.error is not None
    assert entry.error["host"] == "github.com" and entry.error["code"] == "auth_required"
    assert set(entry.error) == {"host", "code", "message"}


def test_identity_with_a_rejected_token_is_an_error_not_none(fake_forges: FakeForges) -> None:
    cfg = config_of({"github.com": Host(token_env="MY_TOKEN")})
    hosts = make_hosts(fake_forges, cfg, env={"MY_TOKEN": TOKEN})
    fake_forges.answer("gh", "user", status=401, body={"message": f"Bad credentials {TOKEN}"})
    entry = hosts.identity("github.com")
    assert entry.error is not None and entry.error["code"] == "auth_required"
    assert TOKEN not in json.dumps(entry.as_json())


@pytest.mark.parametrize(
    "observed",
    [
        "https://evil.example.test/octo",
        "https://github.com.evil.example.test/octo",
        "https://user" + "@github.com/octo",
        None,
        42,
    ],
)
def test_identity_detects_an_answer_from_another_host_and_never_leaks_the_token(
    fake_forges: FakeForges, observed: object
) -> None:
    cfg = config_of({"ghe.example.test": Host(backend="github", token_env="MY_TOKEN")})
    hosts = make_hosts(fake_forges, cfg, env={"MY_TOKEN": TOKEN})
    body: dict[str, Any] = {"login": "octo", "id": 12}
    if observed is not None:
        body["html_url"] = observed
    fake_forges.answer("gh", "user", body=body)
    entry = hosts.identity("ghe.example.test")
    assert entry.error is not None and entry.error["code"] == "wrong_host_response"
    assert "rotated" in entry.error["message"]
    assert TOKEN not in json.dumps(entry.as_json())
    assert entry.login is None


def test_the_authenticated_user_function_raises_wrong_host_response(
    fake_forges: FakeForges,
) -> None:
    login("gh", "github.com")
    hosts = make_hosts(fake_forges)
    fake_forges.answer(
        "gh", "user", body={"login": "octo", "id": 1, "html_url": "https://other.example.test/o"}
    )
    with pytest.raises(ForgectlError) as raised:
        hosts.authenticated_user(hosts.target("github.com"))
    assert raised.value.code == "wrong_host_response"


@pytest.mark.parametrize(
    "body",
    [
        {"id": 1, "html_url": "https://github.com/o"},
        {"login": "", "id": 1, "html_url": "https://github.com/o"},
        {"login": "a b", "id": 1, "html_url": "https://github.com/o"},
        {"login": "octo", "id": None, "html_url": "https://github.com/octo"},
        {"login": "octo", "id": {"x": 1}, "html_url": "https://github.com/octo"},
        {"login": "octo", "id": True, "html_url": "https://github.com/octo"},
        {"login": ["octo"], "id": 1, "html_url": "https://github.com/octo"},
    ],
)
def test_a_user_answer_that_is_not_the_documented_shape_is_an_upstream_error(
    fake_forges: FakeForges, body: dict[str, Any]
) -> None:
    login("gh", "github.com")
    hosts = make_hosts(fake_forges)
    fake_forges.answer("gh", "user", body=body)
    with pytest.raises(ForgectlError) as raised:
        hosts.authenticated_user(hosts.target("github.com"))
    assert raised.value.code == "upstream_error"


def test_scopes_that_are_hostile_are_cut_not_trusted(fake_forges: FakeForges) -> None:
    login("gh", "github.com")
    hosts = make_hosts(fake_forges)
    fake_forges.answer(
        "gh",
        "user",
        body={"login": "octo", "id": 1, "html_url": "https://github.com/octo"},
        headers={"x-oauth-scopes": "repo," + ",".join(f"s{i}" for i in range(500))},
    )
    entry = hosts.identity("github.com")
    assert entry.scopes is not None and len(entry.scopes) <= 64


# -- the issue's regression tests ---------------------------------------------------------

AUTH_TOKEN_COMMANDS = [
    ("github", "github.com", ("gh", "auth", "token", "--hostname", "{host}")),
    ("gitlab", "gitlab.com", ("glab", "auth", "token")),
]


@pytest.mark.parametrize(("backend", "host", "command"), AUTH_TOKEN_COMMANDS)
def test_gh_auth_token_hostname_never_returns_an_ambient_token(
    fake_forges: FakeForges, backend: str, host: str, command: tuple[str, ...]
) -> None:
    """design.md: ``gh auth token --hostname`` returns an ambient token for any host (and
    ``glab auth token`` has no ``--hostname``), so a ``token_command`` runs with none."""
    ambient = {
        "GH_TOKEN": "ambient-gh-0001",
        "GITHUB_TOKEN": "ambient-github-0002",
        "GITLAB_TOKEN": "ambient-gitlab-0003",
    }
    cfg = config_of({host: Host(token_command=command)})
    hosts = make_hosts(fake_forges, cfg, env=ambient)
    with pytest.raises(ForgectlError) as raised:
        hosts.target(host)
    assert raised.value.code == "auth_required"
    (call,) = fake_forges.calls()
    assert call.argv[:2] == ["auth", "token"]
    for value in ambient.values():
        assert value not in json.dumps(call.env)
        assert value not in json.dumps(raised.value.envelope())
    assert not [n for n in AMBIENT_SENTINELS if n in call.env]
    assert secret_values() == frozenset()


@pytest.mark.parametrize("keyring", [True, False], ids=["keyring-login", "no-keyring-login"])
@pytest.mark.parametrize(("backend", "host", "command"), AUTH_TOKEN_COMMANDS)
def test_an_empty_config_dir_with_a_login_in_the_os_keyring(
    fake_forges: FakeForges,
    tmp_path: Path,
    backend: str,
    host: str,
    command: tuple[str, ...],
    keyring: bool,
) -> None:
    """A caller-set empty config dir never reaches the keyring over the bus: not on a stored-login
    call and not from a ``token_command``, which runs in the same environment."""
    program = "gh" if backend == "github" else "glab"
    variable = "GH_CONFIG_DIR" if backend == "github" else "GLAB_CONFIG_DIR"
    fake_forges.default_bus_socket(present=True)
    if keyring:
        fake_forges.keyring_login(program, host, "ring-token-0042")
    (tmp_path / "empty-config").mkdir()
    env = {
        variable: str(tmp_path / "empty-config"),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x",
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
    }
    user_answer(fake_forges, program, host)
    stored = make_hosts(fake_forges, env=env)
    target = stored.target(host)
    assert target.token is None
    if backend == "github":
        with pytest.raises(ForgectlError):  # gh api reads only file logins
            stored.gateway.api(target, "GET", ["user"])
    else:
        stored.gateway.api(target, "GET", ["user"])  # anonymous
    with_command = make_hosts(fake_forges, config_of({host: Host(token_command=command)}), env=env)
    with pytest.raises(ForgectlError) as raised:
        with_command.target(host)
    assert raised.value.code == "auth_required"
    api_call, token_call = fake_forges.calls()
    assert token_call.argv[:2] == ["auth", "token"]
    for call in (api_call, token_call):
        assert call.env["DBUS_SESSION_BUS_ADDRESS"] == NO_BUS_ADDRESS
        assert "XDG_RUNTIME_DIR" not in call.env
        assert call.token_sent != "ring-token-0042"
    assert_call_safe(api_call, host=host, mode="stored", caller_config_dir=True)
    assert "ring-token-0042" not in json.dumps(raised.value.envelope())
    assert secret_values() == frozenset()


# -- review round 1 -----------------------------------------------------------------------


def test_a_token_command_cut_short_by_the_budget_is_a_network_error_and_not_remembered(
    fake_forges: FakeForges, tmp_path: Path, spawned: Spawned
) -> None:
    marker = tmp_path / "ran-once"
    code = """
        import os, sys, time
        if not os.path.exists(sys.argv[1]):
            open(sys.argv[1], 'w').close()
            time.sleep(30)
        print('tok-second-run')
    """
    cfg = config_of({"github.com": Host(token_command=py(code, str(marker)))})
    hosts = make_hosts(fake_forges, cfg, spawned=spawned, timeout=0.8)
    with pytest.raises(ForgectlError) as raised:
        hosts.target("github.com")
    assert raised.value.code == "network_error"
    assert "time budget" in raised.value.message
    spawned.assert_gone()
    hosts.gateway.budget = Budget(20.0)  # a later command's budget: the failure was not kept
    assert hosts.credential("github.com").token == "tok-second-run"


@pytest.mark.parametrize(
    ("host_cfg", "env"),
    [
        (Host(token_command=printing("")), {}),
        (Host(token_command=("no-such-token-helper",)), {}),
        (Host(token_env="MY_TOKEN"), {"MY_TOKEN": "bad token"}),
        (Host(token_env="MY_TOKEN"), {}),
    ],
    ids=["command output", "command missing", "token_env value", "token_env unset"],
)
def test_every_credential_failure_says_to_set_token_env_or_log_in(
    fake_forges: FakeForges, host_cfg: Host, env: dict[str, str]
) -> None:
    hosts = make_hosts(fake_forges, config_of({"github.com": host_cfg}), env=env)
    with pytest.raises(ForgectlError) as raised:
        hosts.target("github.com")
    assert raised.value.code == "auth_required"
    assert raised.value.message.endswith(GUIDANCE), raised.value.message


GUIDANCE = (
    "set hosts.github.com.token_env to a variable holding the token, or log in with gh"
    " and remove the configured credential"
)


@pytest.mark.parametrize(
    "backwards",  # the output reversed, so no argv, config or test local holds the output
    ["\nx 13-TUO-LENITNES", "\n\n13-TUO-LENITNES", "\n\xe9-13-TUO-LENITNES"],
)
def test_a_token_command_failure_keeps_no_reference_to_the_output(
    fake_forges: FakeForges, backwards: str
) -> None:
    code = """
        import sys
        sys.stdout.buffer.write(eval(sys.argv[1])[::-1])
        sys.stderr.write('RRE-LENITNES'[::-1])
    """
    cmd = py(code, repr(backwards.encode("utf-8")))
    hosts = make_hosts(fake_forges, config_of({"github.com": Host(token_command=cmd)}))
    for _ in range(2):  # the first failure, then the remembered one
        with pytest.raises(ForgectlError) as raised:
            hosts.target("github.com")
        err = raised.value
        assert err.__context__ is None and err.__cause__ is None
        text = "".join(
            traceback.TracebackException(
                type(err), err, err.__traceback__, capture_locals=True
            ).format()
        )
        assert "SENTINEL" not in text


def test_redaction_merges_overlapping_tokens() -> None:
    hosts_module.register_secret("abcd")
    hosts_module.register_secret("cdef")
    assert redact_secrets("x abcdef y") == "x [redacted] y"
    assert redact_secrets("abcdcdef") == "[redacted]"


def test_redaction_never_mangles_its_own_marker() -> None:
    hosts_module.register_secret("tok-long-secret")
    hosts_module.register_secret("dac")  # inside "[redacted]"
    assert redact_secrets("a tok-long-secret b") == "a [redacted] b"


def test_redaction_covers_overlapping_occurrences_of_one_token() -> None:
    hosts_module.register_secret("abab")
    assert redact_secrets("ababab") == "[redacted]"


def test_redaction_matches_lowercase_percent_encoding() -> None:
    hosts_module.register_secret("a/b+c")
    assert redact_secrets("x a%2fb%2bc y a%2Fb%2bc z") == "x [redacted] y [redacted] z"
    hosts_module.register_secret("Up/Case")
    assert redact_secrets("up%2fcase") == "up%2fcase"  # only the hex digits ignore case


def test_token_command_runs_once_per_host_under_threads(
    fake_forges: FakeForges, tmp_path: Path
) -> None:
    counter = tmp_path / "count"
    code = "import sys, time; open(sys.argv[1], 'a').write('x'); time.sleep(0.5); print('tok-t')"
    cfg = config_of({"github.com": Host(token_command=py(code, str(counter)))})
    hosts = make_hosts(fake_forges, cfg)
    barrier = threading.Barrier(2)
    tokens: list[str | None] = []

    def fetch() -> None:
        barrier.wait()
        tokens.append(hosts.credential("github.com").token)

    threads = [threading.Thread(target=fetch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert tokens == ["tok-t", "tok-t"]
    assert counter.read_text() == "x"


@pytest.mark.parametrize("account", ["abc", -5, 0, True, 1.5, "12"])
def test_a_user_id_that_is_not_a_positive_integer_is_an_upstream_error(
    fake_forges: FakeForges, account: object
) -> None:
    login("gh", "github.com")
    hosts = make_hosts(fake_forges)
    body = {"login": "octo", "id": account, "html_url": "https://github.com/octo"}
    fake_forges.answer("gh", "user", body=body)
    with pytest.raises(ForgectlError) as raised:
        hosts.authenticated_user(hosts.target("github.com"))
    assert raised.value.code == "upstream_error"


@pytest.mark.parametrize(
    "body",
    [
        {"login": "a b", "id": 1, "html_url": "https://evil.example.test/o"},
        {"id": 1, "html_url": "https://evil.example.test/o"},
        {"login": "octo", "id": "x", "html_url": "https://evil.example.test/o"},
    ],
    ids=["bad login", "missing login", "bad id"],
)
def test_a_foreign_answer_is_wrong_host_response_before_its_shape_is_checked(
    fake_forges: FakeForges, body: dict[str, Any]
) -> None:
    cfg = config_of({"github.com": Host(token_env="MY_TOKEN")})
    hosts = make_hosts(fake_forges, cfg, env={"MY_TOKEN": TOKEN})
    fake_forges.answer("gh", "user", body=body)
    entry = hosts.identity("github.com")
    assert entry.error is not None and entry.error["code"] == "wrong_host_response"
    assert "rotated" in entry.error["message"]


@pytest.mark.parametrize("call", ["target", "identity", "me", "backend", "credential"])
def test_a_non_ascii_host_is_refused_before_lowercasing(fake_forges: FakeForges, call: str) -> None:
    cfg = config_of({"kelvin.example.test": Host(backend="github", token_env="MY_TOKEN")})
    hosts = make_hosts(fake_forges, cfg, env={"MY_TOKEN": TOKEN})
    kelvin = "Kelvin.example.test"  # KELVIN SIGN lowercases to an ASCII k
    assert kelvin.lower() == "kelvin.example.test"
    with pytest.raises(ForgectlError) as raised:
        getattr(hosts, call)(kelvin)
    assert raised.value.code == "refused" and raised.value.reason == "unknown_host"
    assert secret_values() == frozenset() and fake_forges.calls() == []
