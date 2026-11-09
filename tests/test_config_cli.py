"""Config as the CLI applies it: loaded before any command, `--config`, `--timeout`, `me.name`."""

from __future__ import annotations

import json
import os
from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from forgectl import registry
from forgectl.cli import main
from forgectl.config import load_config
from forgectl.humanactions import Tty
from forgectl.registry import Invocation

from .harness.schemas import validate

PR = "github.com/o/r#1"
TTY = Tty(stdin=True, stderr=True)
NO_TTY = Tty(stdin=False, stderr=False)


@pytest.fixture(autouse=True)
def handlers(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    table: dict[str, Any] = {}
    monkeypatch.setattr(registry, "HANDLERS", table)
    monkeypatch.setattr(registry, "load_handlers", lambda: None)
    yield table


def capture(handlers: dict[str, Any], command: str = "show") -> list[Invocation]:
    seen: list[Invocation] = []

    def handler(inv: Invocation) -> dict[str, Any]:
        seen.append(inv)
        return {}

    handlers[command] = handler
    return seen


def default_config(text: str) -> Path:
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "forgectl" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_reach_the_handler(handlers: dict[str, Any]) -> None:
    seen = capture(handlers)
    assert main(["show", PR], tty=NO_TTY) == 0
    assert seen[0].timeout == 60.0
    assert seen[0].config.config.retries == 3


def test_invalid_config_exits_2_naming_the_key_and_never_reaches_a_handler(
    handlers: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    seen = capture(handlers)
    default_config("retries: 99\n")
    assert main(["--json", "show", PR], tty=NO_TTY) == 2
    out = json.loads(capsys.readouterr().out)
    validate(out, "forgectl/cli/error.json")
    assert out["error"]["code"] == "config_invalid"
    assert out["error"]["details"] == {"key": "retries"}
    assert seen == []


def test_invalid_config_also_stops_writes_and_human_output(
    handlers: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    capture(handlers, "comment")
    default_config("verbose: 1\n")
    code = main(["comment", PR, "--body-file", "b.md"], tty=NO_TTY)
    assert code == 2
    assert "verbose" in capsys.readouterr().err


def test_config_flag_with_a_missing_file_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.yaml"
    assert main(["--json", "--config", str(missing), "show", PR], tty=NO_TTY) == 2
    out = json.loads(capsys.readouterr().out)
    validate(out, "forgectl/cli/error.json")
    assert out["error"]["code"] == "config_invalid"
    assert out["error"]["details"] == {"key": str(missing)}


def test_config_flag_is_applied_wherever_it_is_given(
    handlers: dict[str, Any], tmp_path: Path
) -> None:
    seen = capture(handlers)
    path = tmp_path / "c.yaml"
    path.write_text("timeout: 7s\n", encoding="utf-8")
    assert main(["show", PR, "--config", str(path)], tty=NO_TTY) == 0
    assert seen[0].timeout == 7.0
    assert seen[0].config.config_path == str(path)


@pytest.mark.parametrize(
    ("env", "flag", "expected"),
    [
        (None, None, 60.0),
        ("20s", None, 20.0),
        (None, "3s", 3.0),
        ("20s", "3s", 3.0),
    ],
)
def test_timeout_precedence_through_the_cli(
    handlers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    env: str | None,
    flag: str | None,
    expected: float,
) -> None:
    seen = capture(handlers)
    if env is not None:
        monkeypatch.setenv("FORGECTL_TIMEOUT", env)
    argv = ["show", PR] + (["--timeout", flag] if flag else [])
    assert main(argv, tty=NO_TTY) == 0
    assert seen[0].timeout == expected


def test_config_timeout_is_the_default_budget(handlers: dict[str, Any]) -> None:
    seen = capture(handlers)
    default_config("timeout: 90\n")
    main(["show", PR], tty=NO_TTY)
    assert seen[0].timeout == 90.0


@pytest.mark.parametrize(
    ("name", "value"),
    [("FORGECTL_TIMEOUT", "0"), ("FORGECTL_RETRIES", "11"), ("FORGECTL_GH_BIN", "a/b")],
)
def test_a_bad_environment_value_exits_2(
    handlers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
) -> None:
    seen = capture(handlers)
    monkeypatch.setenv(name, value)
    assert main(["--json", "show", PR], tty=NO_TTY) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "config_invalid"
    assert out["error"]["details"] == {"key": name}
    assert seen == []


def test_me_name_from_config_feeds_the_default_actor(handlers: dict[str, Any]) -> None:
    seen = capture(handlers, "comment")
    default_config("me: {name: configured}\n")
    assert main(["comment", PR, "--body-file", "b.md", "--yes"], tty=TTY) == 0
    assert seen[0].human is not None
    assert seen[0].human.actor == "human:configured"
    assert seen[0].human.actor_source == "tty"


def test_without_me_name_the_os_username_is_used_not_the_environment(
    handlers: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Entry:
        pw_name = "osuser"

    monkeypatch.setattr("forgectl.humanactions.pwd.getpwuid", lambda uid: Entry())
    for var in ("USER", "LOGNAME", "USERNAME"):
        monkeypatch.setenv(var, "mallory")
    monkeypatch.setenv("FORGECTL_ME__NAME", "mallory")
    seen = capture(handlers, "comment")
    main(["comment", PR, "--body-file", "b.md", "--yes"], tty=TTY)
    assert seen[0].human is not None
    assert seen[0].human.actor == "human:osuser"


def test_a_non_matching_os_username_gives_human_unknown(
    handlers: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Entry:
        pw_name = "a b"

    monkeypatch.setattr("forgectl.humanactions.pwd.getpwuid", lambda uid: Entry())
    seen = capture(handlers, "comment")
    main(["comment", PR, "--body-file", "b.md", "--yes"], tty=TTY)
    assert seen[0].human is not None
    assert seen[0].human.actor == "human:unknown"


def test_the_config_hash_reaches_the_handler(handlers: dict[str, Any]) -> None:
    seen = capture(handlers)
    main(["show", PR], tty=NO_TTY)
    first = seen[0].config.config_hash
    default_config("retries: 4\n")
    main(["show", PR, "--timeout", "1s"], tty=NO_TTY)
    assert seen[1].config.config_hash != first


@pytest.mark.parametrize("json_flag", [True, False])
@pytest.mark.parametrize(
    "text",
    [
        "gh_bin: !SECRETXYZ x\n",
        "gh_bin: !SECRETXYZ!x y\n",
        "%TAG !e! tag:SECRETXYZ,2000:\n---\ngh_bin: !e!x y\n",
        "retries: !!int SECRETXYZ\n",
    ],
)
def test_a_yaml_error_never_echoes_file_content_to_either_stream(
    handlers: dict[str, Any], capsys: pytest.CaptureFixture[str], text: str, json_flag: bool
) -> None:
    seen = capture(handlers)
    default_config(text)
    argv = (["--json"] if json_flag else []) + ["show", PR]
    assert main(argv, tty=NO_TTY) == 2
    captured = capsys.readouterr()
    assert "SECRETXYZ" not in captured.out + captured.err
    assert "config_invalid" in captured.out + captured.err
    assert seen == []


def test_an_invocation_timeout_defaults_to_its_config(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("timeout: 5s\n", encoding="utf-8")
    loaded = load_config(str(path), env=os.environ)
    args = Namespace()
    assert Invocation("show", args, False, None, loaded).timeout == 5.0
    keywords = Invocation(command="show", args=args, json=False, human=None, config=loaded)
    assert keywords.timeout == 5.0
    assert Invocation("show", args, False, None, loaded, 2.0).timeout == 2.0
    assert Invocation("show", args, False, None).timeout == 60.0
