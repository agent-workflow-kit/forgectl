"""Hook machinery (issue #20): payloads, outcomes, decision ids, process safety, the watchdog."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from forgectl import hooks
from forgectl.config import Hooks, LoadedConfig
from forgectl.errors import ForgectlError
from forgectl.gateway import TOKEN_VARIABLES, Detached, Gateway, detached_children

from .harness.fixtures import forgectl_env
from .harness.forges import FakeForges
from .harness.procs import group_members
from .harness.schemas import validate_hook_payload

SHA = "a" * 40
PR = "github.com/o/r#7"
REPO = "github.com/o/r"


class Spawned:
    """Every child the gateway starts, as (pid, pgid), recorded at launch."""

    def __init__(self) -> None:
        self.children: list[tuple[int, int]] = []

    def __call__(self, pid: int, pgid: int) -> None:
        self.children.append((pid, pgid))

    def reap_and_assert_gone(self) -> None:
        for _pid, pgid in self.children:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                members = group_members(pgid)
                for pid, _ppid, state in members:
                    if state == "Z":
                        try:
                            os.waitpid(pid, os.WNOHANG)
                        except ChildProcessError:
                            pass
                if not group_members(pgid):
                    break
                time.sleep(0.02)
            assert group_members(pgid) == [], f"group {pgid} still has members"


@pytest.fixture
def spawned() -> Spawned:
    return Spawned()


def config_with(**lists: list[dict[str, Any]]) -> LoadedConfig:
    base = LoadedConfig.defaults()
    hooks_model = Hooks.model_validate({k.replace("_", "-"): v for k, v in lists.items()})
    return LoadedConfig(
        config=base.config.model_copy(update={"hooks": hooks_model}),
        config_hash="x",
        config_path=None,
    )


def make_gateway(
    forges: FakeForges,
    spawned: Spawned,
    config: LoadedConfig,
    env: Mapping[str, str | None] | None = None,
    **kwargs: Any,
) -> Gateway:
    return Gateway(
        config, timeout=20, environ=forgectl_env(forges, env), on_spawn=spawned, **kwargs
    )


def merge_fields(**over: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "object_id": PR,
        "repo": REPO,
        "head_sha": SHA,
        "actor": "human:me",
        "action": "merge",
        "auto": False,
        "method": "squash",
        "base_ref": "main",
        "expected_head": SHA,
        "expected_base": "main",
        "blocked_by": [],
        "override_blocks": False,
        "ci": {"state": "passing", "checks": [{"name": "build", "state": "passing"}]},
        "override_escalation": False,
    }
    fields.update(over)
    return fields


def post_merge_fields(**over: Any) -> dict[str, Any]:
    fields = merge_fields()
    del fields["ci"], fields["override_escalation"]
    fields.update(observed=None, result="merged")
    fields.update(over)
    return fields


def pre_post_fields(**over: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "object_id": PR,
        "repo": REPO,
        "head_sha": SHA,
        "actor": "auto:bot",
        "action": "comment",
        "event": None,
        "body_sha256": "b" * 64,
        "body_bytes": 5,
        "thread_id": None,
        "thread_author_is_self": None,
        "reply_to": None,
        "override_escalation": False,
        "allow_self": False,
    }
    fields.update(over)
    return fields


def post_post_fields(**over: Any) -> dict[str, Any]:
    fields = pre_post_fields()
    del fields["override_escalation"], fields["allow_self"]
    fields.update(result="posted", ids=["gh:ic:abc"])
    fields.update(over)
    return fields


def entry(cmd: str, timeout: float = 10, **extra: Any) -> dict[str, Any]:
    if "async_" in extra:
        extra["async"] = extra.pop("async_")
    return {"cmd": cmd, "timeout": timeout, **extra}


def tmp_leftovers() -> list[str]:
    return sorted(p for p in os.listdir(os.environ["TMPDIR"]) if p.startswith("forgectl-"))


# -- Payload -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hook", "fields"),
    [
        ("pre-merge", merge_fields()),
        ("post-merge", post_merge_fields()),
        ("post-merge", post_merge_fields(observed={"head_sha": SHA, "base_ref": "x"})),
        ("pre-post", pre_post_fields()),
        ("post-post", post_post_fields()),
    ],
)
def test_every_payload_validates_against_its_schema(hook: str, fields: dict[str, Any]) -> None:
    payload = hooks.build_payload(hook, fields, ["d1"])
    validate_hook_payload(hook, json.loads(hooks.serialize(payload)))


def test_the_field_sets_equal_the_schemas_properties() -> None:
    root = Path(__file__).resolve().parent.parent / "schemas" / "hooks"
    for hook in hooks.HOOKS:
        props = set(json.loads((root / f"{hook}.json").read_text())["properties"])
        assert props - {"tool", "hook", "object_type", "decision_ids"} == hooks.FIELDS[hook]


def test_a_missing_or_extra_field_is_refused_so_a_body_cannot_ride_along() -> None:
    fields = merge_fields()
    del fields["ci"]
    with pytest.raises(ValueError, match="missing"):
        hooks.build_payload("pre-merge", fields)
    with pytest.raises(ValueError, match="unexpected"):
        hooks.build_payload("pre-merge", merge_fields(body="secret text"))
    with pytest.raises(ValueError):
        hooks.build_payload("nope", {})


def test_build_payload_does_not_modify_its_input_and_sanitizes_untrusted_strings() -> None:
    fields = merge_fields(
        base_ref="ma\u202ein\x00\ud800",
        ci={"state": "failing", "checks": [{"name": "a\nb\u2066", "state": "failing"}]},
    )
    before = json.dumps(fields, sort_keys=True)
    payload = hooks.build_payload("pre-merge", fields)
    assert json.dumps(fields, sort_keys=True) == before
    assert payload["base_ref"] == "ma<U+202E>in<U+0000><U+D800>"
    assert payload["ci"]["checks"][0]["name"] == "a<U+000A>b<U+2066>"
    text = hooks.serialize(payload)
    assert text.isascii() and text.endswith(b"\n")
    json.loads(text)


def test_serialize_refuses_nan_and_an_oversize_payload() -> None:
    with pytest.raises(ValueError):
        hooks.serialize({"x": float("nan")})
    big = hooks.build_payload("pre-merge", merge_fields(base_ref="x" * (hooks.MAX_PAYLOAD + 1)))
    with pytest.raises(ForgectlError) as raised:
        hooks.serialize(big)
    assert raised.value.code == "internal"


# -- Decision ids ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "ids", "problem"),
    [
        (b"", [], False),
        (b"  \n", [], False),
        (b'{"decision_id": "a:b/c.d-e_f@g"}', ["a:b/c.d-e_f@g"], False),
        (b'{"decision_id": ["one", "two", "one"]}', ["one", "two", "one"], False),
        (b'{"other": 1}', [], False),
        (b'{"decision_id": []}', [], False),
        (b'{"decision_id": "has space"}', [], True),
        (b'{"decision_id": ""}', [], True),
        (b'{"decision_id": "' + b"x" * 129 + b'"}', [], True),
        (b'{"decision_id": ["ok", "bad id"]}', [], True),
        (b'{"decision_id": ["ok", 5]}', [], True),
        (b'{"decision_id": 5}', [], True),
        (b'{"decision_id": null}', [], True),
        (b'{"decision_id": "a\\n"}', [], True),
        (b'{"decision_id": "\xc3\xa9"}', [], True),
        (b'{"decision_id": "a", "decision_id": "b"}', [], True),
        (b'["decision_id"]', [], True),
        (b"not json", [], True),
        (b"\xef\xbb\xbf" + b'{"decision_id": "a"}', [], True),
        (b'{"decision_id": NaN}', [], True),
        (b"\xff\xfe", [], True),
        (b"[" * 100000, [], True),
    ],
)
def test_parse_decision_ids(stdout: bytes, ids: list[str], problem: bool) -> None:
    got, why = hooks.parse_decision_ids(stdout)
    assert got == ids
    assert (why is not None) == problem


def test_merge_decision_ids_dedupes_in_order_and_caps_at_32() -> None:
    merged, dropped = hooks.merge_decision_ids(["b", "a", "b"], ["a", "c", "c"])
    assert (merged, dropped) == (["b", "a", "c"], 0)
    many = [f"id{i}" for i in range(40)]
    merged, dropped = hooks.merge_decision_ids(["first"], many)
    assert len(merged) == 32 and merged[0] == "first" and dropped == 9
    merged, dropped = hooks.merge_decision_ids(many[:32], ["extra", many[0]])
    assert merged == many[:32] and dropped == 1


# -- Pre hooks: outcomes -----------------------------------------------------------------------


def run_pre(
    forges: FakeForges,
    spawned: Spawned,
    entries_: list[dict[str, Any]],
    *,
    env: Mapping[str, str | None] | None = None,
    override: bool = False,
    ids: list[str] | None = None,
    fields: dict[str, Any] | None = None,
    hook: str = "pre-merge",
) -> hooks.HookResult:
    gateway = make_gateway(forges, spawned, config_with(**{hook.replace("-", "_"): entries_}), env)
    default = merge_fields() if hook == "pre-merge" else pre_post_fields()
    return hooks.run_pre_hooks(
        gateway, hook, fields or default, decision_ids=ids or [], override_escalation=override
    )


def veto_of(
    forges: FakeForges, spawned: Spawned, entries_: list[dict[str, Any]], **kw: Any
) -> ForgectlError:
    with pytest.raises(ForgectlError) as raised:
        run_pre(forges, spawned, entries_, **kw)
    assert raised.value.code == "vetoed" and raised.value.exit_code == 11
    return raised.value


def test_no_hooks_and_exit_zero_allow(fake_forges: FakeForges, spawned: Spawned) -> None:
    assert run_pre(fake_forges, spawned, []).decision_ids == []
    result = run_pre(fake_forges, spawned, [entry("exit 0")], ids=["x"])
    assert result.decision_ids == ["x"] and result.escalated == []
    assert tmp_leftovers() == []
    spawned.reap_and_assert_gone()


@pytest.mark.parametrize("cmd", ["exit 1", "exit 2", "exit 12", "exit 14", "exit 255", "exit 127",
                                 "nosuchcommand-xyz", "kill -9 $$", "kill -TERM $$"])  # fmt: skip
def test_any_other_exit_or_a_signal_vetoes(
    fake_forges: FakeForges, spawned: Spawned, cmd: str
) -> None:
    err = veto_of(fake_forges, spawned, [entry(cmd)])
    assert err.hook == "pre-merge" and err.hook_stderr is not None
    spawned.reap_and_assert_gone()


def test_a_veto_stops_later_hooks(
    fake_forges: FakeForges, spawned: Spawned, tmp_path: Path
) -> None:
    mark = tmp_path / "ran"
    err = veto_of(
        fake_forges,
        spawned,
        [entry("echo no >&2; exit 1"), entry(f"touch {mark}")],
    )
    assert "no" in (err.hook_stderr or "")
    assert not mark.exists()


def test_escalation_runs_later_hooks_and_exits_13_with_every_escalated_hook(
    fake_forges: FakeForges, spawned: Spawned, tmp_path: Path
) -> None:
    mark = tmp_path / "later"
    with pytest.raises(ForgectlError) as raised:
        run_pre(
            fake_forges,
            spawned,
            [
                entry("echo first >&2; exit 13"),
                entry("exit 0"),
                entry(f"echo second >&2; touch {mark}; exit 13"),
            ],
        )
    err = raised.value
    assert err.code == "escalated" and err.exit_code == 13
    assert err.hook == "pre-merge" and err.hook_stderr == "first\n"
    assert err.escalated_hooks == [
        {"hook": "pre-merge", "index": 0},
        {"hook": "pre-merge", "index": 2},
    ]
    assert mark.exists()


def test_a_veto_after_an_escalation_vetoes(fake_forges: FakeForges, spawned: Spawned) -> None:
    err = veto_of(fake_forges, spawned, [entry("exit 13"), entry("exit 1")])
    assert err.escalated_hooks is None


def test_override_counts_13_as_allow_and_lists_the_overridden_entries(
    fake_forges: FakeForges, spawned: Spawned
) -> None:
    result = run_pre(fake_forges, spawned, [entry("exit 13"), entry("exit 0")], override=True)
    assert result.escalated == [{"hook": "pre-merge", "index": 0}]


def test_a_veto_still_vetoes_under_override(fake_forges: FakeForges, spawned: Spawned) -> None:
    veto_of(fake_forges, spawned, [entry("exit 13"), entry("exit 1")], override=True)
    veto_of(fake_forges, spawned, [entry("exit 1")], override=True)


def test_pre_post_hooks_run_the_same_way(fake_forges: FakeForges, spawned: Spawned) -> None:
    with pytest.raises(ForgectlError) as raised:
        run_pre(fake_forges, spawned, [entry("exit 13")], hook="pre-post")
    assert raised.value.escalated_hooks == [{"hook": "pre-post", "index": 0}]
    with pytest.raises(ValueError):
        hooks.run_pre_hooks(
            make_gateway(fake_forges, spawned, config_with()), "post-merge", post_merge_fields()
        )


# -- Pre hooks: time and process safety ----------------------------------------------------------

GROUP_HOLDER = (
    "trap '' TERM; sh -c \"trap '' TERM; echo \\$\\$ > {pidfile}; exec sleep 60\" & "
    "echo $$ > {leader}; wait"
)


def test_a_timeout_vetoes_and_kills_the_whole_group_even_if_term_is_ignored(
    fake_forges: FakeForges, spawned: Spawned, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("forgectl.gateway.KILL_GRACE", 0.3)
    pidfile, leader = tmp_path / "child.pid", tmp_path / "leader.pid"
    start = time.monotonic()
    err = veto_of(
        fake_forges,
        spawned,
        [entry(GROUP_HOLDER.format(pidfile=pidfile, leader=leader), timeout=0.6)],
    )
    assert time.monotonic() - start < 10
    assert "timed out" in err.message
    child = int(pidfile.read_text())
    assert child != int(leader.read_text()) and spawned.children[0][1] == int(leader.read_text())
    spawned.reap_and_assert_gone()
    assert not Path(f"/proc/{child}").exists() or "Z" in Path(f"/proc/{child}/stat").read_text()


def test_a_hook_that_forks_and_holds_the_pipes_open_is_a_veto_and_the_group_dies(
    fake_forges: FakeForges, spawned: Spawned
) -> None:
    err = veto_of(fake_forges, spawned, [entry("sleep 60 & echo hi; exit 0")])
    assert "left its output open" in err.message
    spawned.reap_and_assert_gone()


def test_huge_stdout_is_a_veto_and_the_hook_is_killed(
    fake_forges: FakeForges, spawned: Spawned
) -> None:
    err = veto_of(fake_forges, spawned, [entry("head -c 5000000 /dev/zero; sleep 60", timeout=20)])
    assert "stdout" in err.message
    spawned.reap_and_assert_gone()


def test_the_payload_is_on_stdin_only(
    fake_forges: FakeForges, spawned: Spawned, tmp_path: Path
) -> None:
    out, args, envdump = tmp_path / "stdin", tmp_path / "args", tmp_path / "env"
    marker = "needle-" + "z" * 12
    cmd = f'cat > {out}; printf "%s\\0" "$0" "$@" > {args}; env > {envdump}'
    run_pre(
        fake_forges,
        spawned,
        [entry(cmd)],
        fields=merge_fields(base_ref=marker),
        ids=["pre-id"],
    )
    payload = json.loads(out.read_text())
    validate_hook_payload("pre-merge", payload)
    assert payload["base_ref"] == marker and payload["decision_ids"] == ["pre-id"]
    assert payload["tool"] == "forgectl" and payload["object_type"] == "pr"
    assert marker not in args.read_text() and marker not in envdump.read_text()
    assert SHA not in envdump.read_text()


def test_a_hook_that_does_not_read_stdin_is_not_a_problem(
    fake_forges: FakeForges, spawned: Spawned
) -> None:
    big = merge_fields(base_ref="x" * 900_000)
    run_pre(fake_forges, spawned, [entry("exit 0")], fields=big)


def test_the_environment_has_no_token_and_the_hook_variables(
    fake_forges: FakeForges, spawned: Spawned, tmp_path: Path
) -> None:
    dump = tmp_path / "env.json"
    code = (
        "import os,json;json.dump({'env':dict(os.environ),'cwd':os.getcwd(),"
        f"'ls':os.listdir('.')}},open({str(dump)!r},'w'))"
    )
    cmd = f'exec {sys.executable} -I -c "{code}"'
    env = {name: "secret-" + name for name in TOKEN_VARIABLES}
    env["MY_TOKEN_ENV"] = "secret-mine"
    gateway = make_gateway(
        fake_forges,
        spawned,
        config_with_host_token("MY_TOKEN_ENV", pre_merge=[entry(cmd)]),
        env,
    )
    hooks.run_pre_hooks(gateway, "pre-merge", merge_fields())
    seen = json.loads(dump.read_text())
    for name in (*TOKEN_VARIABLES, "MY_TOKEN_ENV"):
        assert name not in seen["env"]
    assert seen["env"]["FORGECTL_HOOK"] == "pre-merge"
    assert seen["env"]["FORGECTL_ID"] == PR
    assert seen["env"]["FORGECTL_OBJECT_TYPE"] == "pr"
    assert seen["ls"] == [] and not Path(seen["cwd"]).exists()
    assert Path(seen["cwd"]).parent.name.startswith("forgectl-")
    assert tmp_leftovers() == []


def config_with_host_token(token_env: str, **lists: list[dict[str, Any]]) -> LoadedConfig:
    from forgectl.config import Host

    loaded = config_with(**lists)
    cfg = loaded.config.model_copy(
        update={"hosts": {"h.example.test": Host(backend="gitlab", token_env=token_env)}}
    )
    return LoadedConfig(config=cfg, config_hash="x", config_path=None)


def test_a_hook_cannot_change_what_the_caller_was_shown(
    fake_forges: FakeForges, spawned: Spawned
) -> None:
    fields = merge_fields()
    before = json.dumps(fields, sort_keys=True)
    forged = '{"decision_id":"ok","head_sha":"' + "c" * 40 + '","base_ref":"evil","method":"merge"}'
    result = run_pre(fake_forges, spawned, [entry(f"echo '{forged}'")], fields=fields)
    assert result.decision_ids == ["ok"]
    assert json.dumps(fields, sort_keys=True) == before
    assert not hasattr(result, "head_sha")


def test_later_hooks_see_earlier_ids_in_decision_ids(
    fake_forges: FakeForges, spawned: Spawned, tmp_path: Path
) -> None:
    seen = tmp_path / "seen"
    result = run_pre(
        fake_forges,
        spawned,
        [
            entry('echo \'{"decision_id": ["a", "b"]}\''),
            entry(f'cat > {seen}; echo \'{{"decision_id": "b"}}\''),
            entry('echo \'{"decision_id": "c"}\''),
        ],
        ids=["given"],
    )
    assert json.loads(seen.read_text())["decision_ids"] == ["given", "a", "b"]
    assert result.decision_ids == ["given", "a", "b", "c"]


def test_invalid_output_is_warned_and_ignored_and_ids_past_32_are_dropped(
    fake_forges: FakeForges, spawned: Spawned
) -> None:
    many = json.dumps({"decision_id": [f"n{i}" for i in range(40)]})
    result = run_pre(
        fake_forges,
        spawned,
        [
            entry("echo nonsense"),
            entry('echo \'{"decision_id": "bad id"}\''),
            entry(f"echo '{many}'"),
        ],
    )
    assert len(result.decision_ids) == 32 and result.decision_ids[0] == "n0"
    text = " ".join(result.warnings)
    assert "not strict JSON" in text and "grammar" in text and "dropped" in text


def test_a_vetoing_hook_still_contributes_its_ids_to_the_error(
    fake_forges: FakeForges, spawned: Spawned
) -> None:
    err = veto_of(fake_forges, spawned, [entry('echo \'{"decision_id": "why"}\'; exit 1')])
    assert err.decision_ids == ["why"]


# -- hook_stderr -------------------------------------------------------------------------------


def test_stderr_is_sanitized_and_cut_on_a_code_point_boundary(
    fake_forges: FakeForges, spawned: Spawned
) -> None:
    cmd = (
        f'{sys.executable} -I -c "import sys;sys.stderr.buffer.write('
        "b'a\\x1b[31m\\xe2\\x80\\xae' + '\\u00e9'.encode() * 5000 + b'\\xff')\"; exit 1"
    )
    err = veto_of(fake_forges, spawned, [entry(cmd)])
    text = err.hook_stderr or ""
    assert len(text.encode()) <= hooks.HOOK_STDERR_LIMIT
    assert text.startswith("a<U+001B>[31m<U+202E>") and "\x1b" not in text
    text.encode("utf-8")  # strict: no half code point
    body = err.envelope()["error"]["hook_stderr"]
    assert len(body.encode()) <= 4096 and "\ufffd" not in body


def test_an_empty_stderr_is_still_present_on_a_veto(
    fake_forges: FakeForges, spawned: Spawned
) -> None:
    err = veto_of(fake_forges, spawned, [entry("exit 3")])
    assert err.envelope()["error"]["hook_stderr"] == ""


# -- Concurrency ---------------------------------------------------------------------------------


def test_concurrent_hooks_do_not_share_directories_or_payloads(
    fake_forges: FakeForges, spawned: Spawned, tmp_path: Path
) -> None:
    outs = [tmp_path / f"o{i}" for i in range(6)]
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            gateway = make_gateway(
                fake_forges,
                spawned,
                config_with(pre_merge=[entry(f"cat > {outs[i]}; pwd >> {outs[i]}.cwd; sleep 0.2")]),
            )
            hooks.run_pre_hooks(gateway, "pre-merge", merge_fields(base_ref=f"b{i}"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    cwds = {Path(f"{o}.cwd").read_text() for o in outs}
    assert len(cwds) == 6
    for i, o in enumerate(outs):
        assert json.loads(o.read_text())["base_ref"] == f"b{i}"
    assert tmp_leftovers() == []


# -- Post hooks ----------------------------------------------------------------------------------


def run_post(
    forges: FakeForges,
    spawned: Spawned,
    entries_: list[dict[str, Any]],
    hook: str = "post-merge",
    env: Mapping[str, str | None] | None = None,
    **kwargs: Any,
) -> hooks.HookResult:
    gateway = make_gateway(forges, spawned, config_with(**{hook.replace("-", "_"): entries_}), env)
    fields = post_merge_fields() if hook == "post-merge" else post_post_fields()
    return hooks.run_post_hooks(gateway, hook, fields, **kwargs)


def test_post_failures_warn_and_never_raise(fake_forges: FakeForges, spawned: Spawned) -> None:
    result = run_post(
        fake_forges,
        spawned,
        [
            entry("echo boom >&2; exit 1"),
            entry("exit 13"),
            entry("sleep 30", timeout=0.3),
            entry("sleep 60 & exit 0"),
            entry('echo \'{"decision_id": "z"}\''),
        ],
    )
    assert len(result.warnings) == 4
    assert "boom" in result.warnings[0] and "timed out" in result.warnings[2]
    assert [r.outcome for r in result.reports] == ["failed"] * 4 + ["allow"]
    assert result.decision_ids == ["z"]
    spawned.reap_and_assert_gone()


def test_post_payloads_validate(fake_forges: FakeForges, spawned: Spawned, tmp_path: Path) -> None:
    run_post(fake_forges, spawned, [entry(f"cat > {tmp_path}/m")], "post-merge")
    validate_hook_payload("post-merge", json.loads((tmp_path / "m").read_text()))
    run_post(fake_forges, spawned, [entry(f"cat > {tmp_path}/p")], "post-post")
    validate_hook_payload("post-post", json.loads((tmp_path / "p").read_text()))


# -- Async -----------------------------------------------------------------------------------


def wait_for(path: Path, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def finish_detached(timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while detached_children() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert detached_children() == [], "a watchdog is still running"


def test_an_async_hook_outlives_the_command_and_its_watchdog_cleans_up(
    fake_forges: FakeForges, spawned: Spawned, tmp_path: Path
) -> None:
    started, done, out = tmp_path / "started", tmp_path / "done", tmp_path / "stdin"
    cmd = f"cat > {out}; pwd > {started}; echo ignored-stdout; sleep 0.6; touch {done}"
    result = run_post(fake_forges, spawned, [entry(cmd, async_=True)])
    assert [r.outcome for r in result.reports] == ["async_started"] and result.warnings == []
    assert not done.exists()  # the command is already back: the hook is detached
    assert wait_for(done)
    finish_detached()
    validate_hook_payload("post-merge", json.loads(out.read_text()))
    cwd = Path(started.read_text().strip())
    assert not cwd.exists() and tmp_leftovers() == []
    assert len(spawned.children) == 1  # exactly the watchdog was recorded
    assert result.decision_ids == []  # async stdout is discarded


def test_an_async_hook_has_its_own_private_directory_and_no_token(
    fake_forges: FakeForges, spawned: Spawned, tmp_path: Path
) -> None:
    dump = tmp_path / "dump.json"
    code = (
        "import os,json;json.dump({'env':dict(os.environ),'cwd':os.getcwd(),"
        f"'ls':os.listdir('.')}},open({str(dump)!r},'w'))"
    )
    gateway = make_gateway(
        fake_forges,
        spawned,
        config_with_host_token(
            "MY_TOKEN_ENV", post_merge=[entry(f'exec {sys.executable} -I -c "{code}"', async_=True)]
        ),
        {"GH_TOKEN": "secret", "MY_TOKEN_ENV": "secret"},
    )
    hooks.run_post_hooks(gateway, "post-merge", post_merge_fields())
    assert wait_for(dump)
    finish_detached()
    seen = json.loads(dump.read_text())
    assert "GH_TOKEN" not in seen["env"] and "MY_TOKEN_ENV" not in seen["env"]
    assert seen["env"]["FORGECTL_HOOK"] == "post-merge" and seen["ls"] == []
    assert not Path(seen["cwd"]).exists()


def test_the_watchdog_kills_an_async_hook_at_its_timeout_whole_group_included(
    fake_forges: FakeForges, spawned: Spawned, tmp_path: Path
) -> None:
    pidfile, leader = tmp_path / "child.pid", tmp_path / "leader.pid"
    cmd = GROUP_HOLDER.format(pidfile=pidfile, leader=leader)
    run_post(fake_forges, spawned, [entry(cmd, timeout=0.5, async_=True)])
    assert wait_for(pidfile) and wait_for(leader)
    child, lead = int(pidfile.read_text()), int(leader.read_text())
    finish_detached()  # SIGTERM ignored: the SIGKILL 2 s later ends it
    for pid in (child, lead):
        stat = Path(f"/proc/{pid}/stat")
        assert not stat.exists() or stat.read_text().rpartition(")")[2].split()[0] == "Z"
    spawned_orphans = Spawned()
    spawned_orphans.children = [(lead, lead)]
    spawned_orphans.reap_and_assert_gone()
    assert tmp_leftovers() == []


def test_a_failing_async_hook_is_logged_by_the_watchdog(
    fake_forges: FakeForges, spawned: Spawned, tmp_path: Path
) -> None:
    from forgectl import jsonlog

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    run_post(fake_forges, spawned, [entry("exit 3", async_=True)], state_dir=str(state), pr=PR)
    finish_detached()
    records = [json.loads(line) for line in (state / "log.jsonl").read_text().splitlines()]
    assert records[0]["event"] == "hook_failed" and records[0]["level"] == "warn"
    assert records[0]["details"] == {"hook": "post-merge", "index": 0, "why": "exited 3"}
    assert records[0]["pr"] == PR
    assert jsonlog.state_dir  # the module the watchdog writes through


def test_a_spawn_failure_warns_and_removes_the_private_directory(
    fake_forges: FakeForges, spawned: Spawned
) -> None:
    def broken(*_a: Any, **_k: Any) -> Detached:
        raise OSError("no exec")

    gateway = make_gateway(
        fake_forges, spawned, config_with(post_merge=[entry("exit 0", async_=True)]),
        spawner=broken,
    )  # fmt: skip
    result = hooks.run_post_hooks(gateway, "post-merge", post_merge_fields())
    assert len(result.warnings) == 1 and "watchdog" in result.warnings[0]
    assert tmp_leftovers() == []


def test_the_watchdog_refuses_a_bad_spec_and_a_directory_it_did_not_make(
    fake_forges: FakeForges, tmp_path: Path
) -> None:
    from forgectl import hookwatch

    victim = tmp_path / "victim"
    (victim / "cwd").mkdir(parents=True)
    spec = {
        "argv": ["/bin/sh", "-c", "exit 0"],
        "env": {"PATH": "/bin"},
        "root": str(victim),
        "cwd": str(victim / "cwd"),
        "payload": "{}",
        "timeout": 5,
    }
    assert hookwatch._valid(spec)
    assert hookwatch.run(spec) == 2 and victim.exists()
    for bad in ({**spec, "timeout": 0}, {**spec, "timeout": True}, {**spec, "argv": []},
                {**spec, "env": {"A": 1}}, [], {**spec, "argv": ["a\0b"]}):  # fmt: skip
        assert not hookwatch._valid(bad)
    assert not hookwatch._own_private_dir("relative", "relative/cwd")
    link = tmp_path / "forgectl-link"
    link.symlink_to(victim)
    assert not hookwatch._own_private_dir(str(link), str(link / "cwd"))


def test_the_watchdog_ends_the_hook_and_removes_the_directory_on_sigterm(
    fake_forges: FakeForges, spawned: Spawned, tmp_path: Path
) -> None:
    started = tmp_path / "pid"
    cmd = f"echo $$ > {started}; exec sleep 60"
    run_post(fake_forges, spawned, [entry(cmd, timeout=50, async_=True)])
    assert wait_for(started)
    hook_pid = int(started.read_text())
    (watchdog_pid, watchdog_pgid) = spawned.children[0]
    proc = next(p for p in detached_children() if p.pid == watchdog_pid)
    proc.send_signal(signal.SIGTERM)  # our own unreaped child, held in the module list
    finish_detached()
    stat = Path(f"/proc/{hook_pid}/stat")
    assert not stat.exists() or stat.read_text().rpartition(")")[2].split()[0] == "Z"
    spawned.children.append((hook_pid, hook_pid))
    spawned.reap_and_assert_gone()
    assert watchdog_pgid == watchdog_pid and tmp_leftovers() == []
