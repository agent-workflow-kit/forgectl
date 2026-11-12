from __future__ import annotations

import json
import os
import signal
from argparse import Namespace
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from forgectl import registry
from forgectl.cli import main
from forgectl.errors import EXIT_BY_CODE, ForgectlError, refused
from forgectl.humanactions import Tty, require_confirmation
from forgectl.output import would_refuse_result
from forgectl.registry import Invocation

from .harness.schemas import validate

PR = "github.com/o/r#1"
H1 = "a" * 40
H2 = "b" * 40
TTY = Tty(stdin=True, stderr=True)
NO_TTY = Tty(stdin=False, stderr=False)
EXPECTED = ["--expected-head", H1, "--expected-base", "main"]

# One valid command line per command, for a non-interactive run.
VALID: dict[str, list[str]] = {
    "prs": ["prs", "--author", "me"],
    "show": ["show", PR],
    "snapshot": ["snapshot", PR],
    "diff": ["diff", PR],
    "compare": ["compare", PR, "--from", H1, "--to", H2],
    "files": ["files", PR],
    "comments": ["comments", PR],
    "reviews": ["reviews", PR],
    "ci": ["ci", PR],
    "access": ["access", "github.com/o/r", "bob"],
    "whoami": ["whoami"],
    "merge": ["merge", PR, *EXPECTED],
    "review": ["review", PR, "--file", "r.json", *EXPECTED],
    "comment": ["comment", PR, "--body-file", "b.md"],
    "reply": ["reply", PR, "--to", "gh:rc:abc", "--body-file", "b.md"],
    "resolve": ["resolve", PR, "--thread", "gh:rt:abc"],
    "unresolve": ["unresolve", PR, "--thread", "gh:rt:abc"],
    "approve": ["approve", PR, *EXPECTED],
    "browse": ["browse"],
    "export": ["export"],
}
WRITES = ["merge", "review", "comment", "reply", "resolve", "unresolve", "approve"]
READS = [c for c in VALID if c not in WRITES]


@pytest.fixture(autouse=True)
def no_handlers(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    handlers: dict[str, Any] = {}
    monkeypatch.setattr(registry, "HANDLERS", handlers)
    monkeypatch.setattr(registry, "load_handlers", lambda: None)
    yield handlers


def run(
    capsys: pytest.CaptureFixture[str], argv: list[str], tty: Tty = NO_TTY
) -> tuple[int, str, str]:
    code = main(argv, tty=tty)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def reaches_handler(
    capsys: pytest.CaptureFixture[str], argv: list[str], command: str, tty: Tty = NO_TTY
) -> None:
    """The command line parsed, passed the gate and found no handler for ``command``."""
    code, _, err = run(capsys, argv, tty)
    assert code == 1, err
    assert f"{command}: not implemented yet" in err


def envelope(out: str) -> dict[str, Any]:
    body = json.loads(out)
    validate(body, "forgectl/cli/error.json")
    return body["error"]


# --- the whole surface parses and answers with the envelope -------------------------------


@pytest.mark.parametrize("command", sorted(VALID))
def test_every_command_parses_and_returns_the_envelope(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, ["--json", *VALID[command]])
    assert code == 1
    error = envelope(out)
    assert error["code"] == "internal"
    assert error["message"] == f"{command}: not implemented yet"
    assert "internal" in err


@pytest.mark.parametrize("command", sorted(VALID))
def test_json_flag_after_the_command_works(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, [*VALID[command], "--json"])
    assert code == 1
    assert envelope(out)["message"] == f"{command}: not implemented yet"


def test_without_json_stdout_stays_empty_and_stderr_has_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run(capsys, VALID["show"])
    assert (code, out) == (1, "")
    assert err.startswith("forgectl: error: internal:")


def test_no_command_is_usage(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, ["--json"])
    assert code == 2
    assert envelope(out)["code"] == "usage"


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"], tty=NO_TTY)
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("forgectl ")


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["merge", "--help"], tty=NO_TTY)
    assert exc.value.code == 0
    assert "--expected-head" in capsys.readouterr().out


# --- argparse hostility -----------------------------------------------------------------------


HOSTILE: list[tuple[str, list[str]]] = [
    ("unknown command", ["nope"]),
    ("abbreviated command", ["sho", PR]),
    ("unknown global flag", ["--bogus", "show", PR]),
    ("unknown command flag", ["show", PR, "--bogus"]),
    ("abbreviated global flag", ["--js", "show", PR]),
    ("abbreviated command flag", ["merge", PR, "--expected-h", H1]),
    ("missing pr", ["show"]),
    ("extra positional", ["show", PR, PR]),
    ("empty pr", ["show", ""]),
    ("write flag on a read", ["show", PR, "--yes"]),
    ("actor on a read", ["show", PR, "--actor", "human:bob"]),
    ("dry-run on a read", ["prs", "--author", "me", "--dry-run"]),
    ("decision-id on a read", ["show", PR, "--decision-id", "d"]),
    ("override-escalation on a read", ["show", PR, "--override-escalation"]),
    ("merge flag on comment", ["comment", PR, "--body-file", "b", "--auto"]),
    ("duplicate json", ["--json", "--json", "show", PR]),
    ("duplicate json across positions", ["--json", "show", PR, "--json"]),
    ("duplicate config", ["--config", "a", "--config", "b", "show", PR]),
    ("duplicate timeout", ["--timeout", "1", "--timeout", "2", "show", PR]),
    (
        "duplicate actor",
        ["comment", PR, "--body-file", "b", "--actor", "auto:a", "--actor", "auto:b"],
    ),
    ("duplicate body-file", ["comment", PR, "--body-file", "a", "--body-file", "b"]),
    ("duplicate yes", ["comment", PR, "--body-file", "b", "--yes", "--yes"]),
    ("verbose and quiet", ["--verbose", "--quiet", "show", PR]),
    ("flag as a value", ["comment", PR, "--body-file", "--yes"]),
    ("missing flag value", ["comment", PR, "--body-file"]),
    ("missing required flag", ["comment", PR]),
    ("empty body-file", ["comment", PR, "--body-file", ""]),
    ("empty config", ["--config", "", "show", PR]),
    ("zero timeout", ["--timeout", "0", "show", PR]),
    ("negative timeout", ["--timeout=-5", "show", PR]),
    ("bad timeout unit", ["--timeout", "5w", "show", PR]),
    ("nan timeout", ["--timeout", "nan", "show", PR]),
    ("inf timeout", ["--timeout", "inf", "show", PR]),
    ("huge timeout", ["--timeout", "9" * 400, "show", PR]),
    ("unicode digit timeout", ["--timeout", "٥", "show", PR]),
    ("compare without to", ["compare", PR, "--from", H1]),
    ("compare short sha", ["compare", PR, "--from", "abc", "--to", H2]),
    ("compare 64 hex sha", ["compare", PR, "--from", "a" * 64, "--to", H2]),
    ("compare ref instead of sha", ["compare", PR, "--from", "main", "--to", H2]),
    ("compare sha with newline", ["compare", PR, "--from", H1 + "\n", "--to", H2]),
    ("prs bad state", ["prs", "--author", "me", "--state", "all-of-them"]),
    ("prs limit zero", ["prs", "--author", "me", "--limit", "0"]),
    ("prs limit too big", ["prs", "--author", "me", "--limit", "1001"]),
    ("prs limit unicode digits", ["prs", "--author", "me", "--limit", "١٢"]),
    ("prs limit signed", ["prs", "--author", "me", "--limit", "+5"]),
    ("prs limit float", ["prs", "--author", "me", "--limit", "5.0"]),
    ("prs without filters", ["prs"]),
    ("prs host only", ["prs", "--host", "github.com"]),
    ("prs repo with host", ["prs", "--repo", "github.com/o/r", "--host", "github.com"]),
    ("prs draft and no-draft", ["prs", "--author", "me", "--draft", "--no-draft"]),
    ("prs draft twice", ["prs", "--author", "me", "--draft", "--draft"]),
    ("prs no-draft twice", ["prs", "--author", "me", "--no-draft", "--no-draft"]),
    ("prs no-draft after draft", ["prs", "--author", "me", "--draft", "--no-draft"]),
    ("timeout over a hundred years", ["--timeout", "36501d", "show", PR]),
    ("huge duration unit", ["--timeout", "99999999999999d", "show", PR]),
    ("export huge since", ["export", "--since", "99999999999d"]),
    ("prs huge updated-since", ["prs", "--author", "me", "--updated-since", "1e9999999d"]),
    (
        "merge 64 hex expected head",
        ["merge", PR, "--expected-head", "a" * 64, "--expected-base", "m"],
    ),
    (
        "merge 41 hex expected head",
        ["merge", PR, "--expected-head", "a" * 41, "--expected-base", "m"],
    ),
    ("abbreviated write flag", ["merge", PR, *EXPECTED, "--dry"]),
    ("duplicate dry-run across positions", ["merge", PR, *EXPECTED, "--dry-run", "--dry-run"]),
    ("verbose after the command with quiet", ["show", PR, "--verbose", "--quiet"]),
    ("zoneless time with fraction", ["export", "--since", "2026-01-01T00:00:00.5"]),
    ("prs bad updated-since", ["prs", "--author", "me", "--updated-since", "yesterday"]),
    ("prs date without zone", ["prs", "--author", "me", "--updated-since", "2026-01-01T00:00:00"]),
    ("prs date only", ["prs", "--author", "me", "--updated-since", "2026-01-01"]),
    ("prs impossible date", ["prs", "--author", "me", "--updated-since", "2026-13-45T00:00:00Z"]),
    ("export bad since", ["export", "--since", "soon"]),
    ("merge bad method", ["merge", PR, *EXPECTED, "--method", "fast-forward"]),
    ("merge short expected head", ["merge", PR, "--expected-head", "abc", "--expected-base", "m"]),
    ("merge expected base with dash", ["merge", PR, "--expected-head", H1, "--expected-base=-x"]),
    ("merge expected base empty", ["merge", PR, "--expected-head", H1, "--expected-base="]),
    ("bad actor", ["comment", PR, "--body-file", "b", "--actor", "root"]),
    ("actor bad kind", ["comment", PR, "--body-file", "b", "--actor", "bot:x"]),
    ("actor trailing newline", ["comment", PR, "--body-file", "b", "--actor", "human:bob\n"]),
    ("actor empty name", ["comment", PR, "--body-file", "b", "--actor", "human:"]),
    ("actor name too long", ["comment", PR, "--body-file", "b", "--actor", "human:" + "a" * 65]),
    ("actor non-ascii", ["comment", PR, "--body-file", "b", "--actor", "human:böb"]),
    ("decision-id empty", ["comment", PR, "--body-file", "b", "--decision-id", ""]),
    ("decision-id space", ["comment", PR, "--body-file", "b", "--decision-id", "a b"]),
    ("decision-id newline", ["comment", PR, "--body-file", "b", "--decision-id", "a\n"]),
    ("decision-id too long", ["comment", PR, "--body-file", "b", "--decision-id", "a" * 129]),
    (
        "decision-id 33 values",
        ["comment", PR, "--body-file", "b", "--actor", "auto:x"]
        + [x for i in range(33) for x in ("--decision-id", f"d{i}")],
    ),
    ("dedupe-key bad", ["comment", PR, "--body-file", "b", "--dedupe-key", "a b"]),
    ("dedupe-key too long", ["comment", PR, "--body-file", "b", "--dedupe-key", "k" * 129]),
    ("dedupe-key newline", ["comment", PR, "--body-file", "b", "--dedupe-key", "k\n"]),
    ("approve dedupe without body", ["approve", PR, *EXPECTED, "--dedupe-key", "k"]),
    (
        "merge auto and override-blocks",
        ["merge", PR, *EXPECTED, "--auto", "--override-blocks", "--actor", "human:bob"],
    ),
    ("auto without human", ["merge", PR, *EXPECTED, "--auto"]),
    ("override-blocks without human", ["merge", PR, *EXPECTED, "--override-blocks"]),
    ("allow-self without human", ["approve", PR, *EXPECTED, "--allow-self"]),
    (
        "override-escalation without human",
        ["comment", PR, "--body-file", "b", "--override-escalation"],
    ),
    ("auto with auto actor", ["merge", PR, *EXPECTED, "--auto", "--actor", "auto:ci"]),
    ("merge no expected", ["merge", PR]),
    ("merge only expected head", ["merge", PR, "--expected-head", H1]),
    ("review no expected", ["review", PR, "--file", "r.json"]),
    ("review without file", ["review", PR, *EXPECTED]),
    ("approve no expected", ["approve", PR]),
    ("dry-run merge no expected", ["merge", PR, "--dry-run"]),
    ("reply without to", ["reply", PR, "--body-file", "b"]),
    ("resolve without thread", ["resolve", PR]),
    ("access one arg", ["access", "github.com/o/r"]),
    ("browse positional", ["browse", PR]),
    ("browse interval negative", ["browse", "--interval", "-5"]),
    ("browse interval word", ["browse", "--interval", "soon"]),
    ("browse interval empty", ["browse", "--interval", ""]),
    ("browse interval zero with unit", ["browse", "--interval", "0s"]),
    ("browse interval below the floor", ["browse", "--interval", "1ms"]),
    ("browse interval half a second", ["browse", "--interval", "0.5"]),
    ("browse interval over a hundred years", ["browse", "--interval", "36501d"]),
    ("browse interval twice", ["browse", "--interval", "5", "--interval", "6"]),
    (
        "comment allow-self",
        ["comment", PR, "--body-file", "b", "--allow-self", "--actor", "human:b"],
    ),
    ("double dash then flag as pr", ["show", "--", "--json", PR]),
]


HOSTILE_PR = frozenset({"empty pr", "double dash then flag as pr"})  # a bad PR is invalid_pr


@pytest.mark.parametrize(("name", "argv"), HOSTILE, ids=[n for n, _ in HOSTILE])
def test_hostile_input_is_usage_error_exit_2(
    name: str, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, ["--json", *argv])
    assert code == 2, (out, err)
    code_name = envelope(out)["code"]
    assert code_name == ("invalid_pr" if name in HOSTILE_PR else "usage")
    assert code_name in err


# `-h` is the help flag only when it is exactly `-h`; `-h<anything>` must not be read as it.
URL_LOOKALIKE = "-https://github.com/o/r/pull/1"
HELP_LOOKALIKES = ["-hh", "-hx", "-h=x", URL_LOOKALIKE, "-help", "-h-h"]
# Every subparser shares the same `_Parser`, so the full token list runs against one
# representative command and every command gets the single URL-shaped token.
REPRESENTATIVE = "show"


@pytest.fixture
def private_home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


def _help_free(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    try:
        code = main(["--json", *argv], tty=NO_TTY)
    except SystemExit as exc:  # a help or version exit leaks out of main
        pytest.fail(f"{argv} exited via SystemExit({exc.code})")
    out, err = capsys.readouterr()
    assert "usage: forgectl" not in out, out
    return code, out, err


@pytest.mark.usefixtures("private_home")
@pytest.mark.parametrize("bad", HELP_LOOKALIKES)
def test_help_lookalike_after_the_command_is_usage(
    bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _help_free([*VALID[REPRESENTATIVE], bad], capsys)
    assert code == 2, (out, err)
    assert envelope(out)["code"] == "usage"


@pytest.mark.usefixtures("private_home")
@pytest.mark.parametrize("bad", HELP_LOOKALIKES)
def test_help_lookalike_in_pr_position_is_usage(
    bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    valid = VALID[REPRESENTATIVE]
    code, out, err = _help_free([valid[0], bad, *valid[1:]], capsys)
    assert code == 2, (out, err)
    assert envelope(out)["code"] == "usage"


@pytest.mark.usefixtures("private_home")
@pytest.mark.parametrize("command", sorted(VALID))
def test_url_lookalike_in_pr_position_is_usage_for_every_command(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    valid = VALID[command]
    code, out, err = _help_free([valid[0], URL_LOOKALIKE, *valid[1:]], capsys)
    assert code == 2, (out, err)
    assert envelope(out)["code"] == "usage"


# The PR-position and before-command tests are the ones that catch #51. The option-value,
# `--timeout`/`--config` value and `--` tests pin behavior argparse already gives (it reads
# `-hx` as an option, not a value, for nargs=None), as a guard against regressions.
@pytest.mark.usefixtures("private_home")
@pytest.mark.parametrize("bad", HELP_LOOKALIKES)
@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--timeout", "{bad}"],
        ["--config", "{bad}"],
    ],
    ids=["before-command", "as-timeout-value", "as-config-value"],
)
def test_help_lookalike_in_global_position_is_usage(
    argv: list[str], bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [a.replace("{bad}", bad) for a in argv] or [bad]
    code, out, err = _help_free([*argv, "whoami"], capsys)
    assert code == 2, (out, err)
    assert envelope(out)["code"] == "usage"


@pytest.mark.usefixtures("private_home")
@pytest.mark.parametrize("bad", HELP_LOOKALIKES)
def test_help_lookalike_as_an_option_value_is_usage(
    bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv in (
        ["comment", PR, "--body-file", bad],
        ["merge", PR, "--expected-head", bad, "--expected-base", "main"],
        ["prs", "--author", bad],
        ["prs", "--author", "me", "--limit", bad],
    ):
        code, out, err = _help_free(argv, capsys)
        assert code == 2, (argv, out, err)
        assert envelope(out)["code"] == "usage"


@pytest.mark.usefixtures("private_home")
@pytest.mark.parametrize("bad", HELP_LOOKALIKES)
def test_help_lookalike_after_double_dash_is_never_help(
    bad: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # After `--` it is a positional value, never a flag; it must not print help or exit 0.
    code, out, err = _help_free(["show", "--", bad], capsys)
    assert code != 0, (out, err)


@pytest.mark.usefixtures("private_home")
@pytest.mark.parametrize(
    ("command", "flag"),
    [(REPRESENTATIVE, "--help"), *((command, "-h") for command in sorted(VALID))],
)
def test_exact_help_flag_still_exits_zero(
    command: str, flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main([command, flag], tty=NO_TTY)
    assert exc.value.code == 0
    assert "usage: forgectl" in capsys.readouterr().out


@pytest.mark.usefixtures("private_home")
@pytest.mark.parametrize("bad", ["-v", "-q", "-V", "-j", "-vq", "-hv", "-vh"])
def test_no_other_short_flags_exist(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _help_free(["show", PR, bad], capsys)
    assert code == 2, (out, err)


def test_usage_error_without_json_prints_text_only(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, ["show"])
    assert (code, out) == (2, "")
    assert err.startswith("forgectl: error: usage:")


def test_usage_error_on_a_parse_failure_is_json_when_json_is_anywhere(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = run(capsys, ["show", "--json"])
    assert code == 2
    envelope(out)


def test_error_message_and_stderr_are_sanitized(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(inv: Invocation) -> dict[str, Any]:
        raise ForgectlError("not_found", "gone\x1b[31m\u202e", details=None)

    registry.HANDLERS["show"] = handler
    code, out, err = run(capsys, ["--json", "show", PR])
    assert code == 10
    assert "<U+001B>" in envelope(out)["message"]
    assert "<U+202E>" in err
    assert "\x1b" not in out + err and "\u202e" not in out + err


def test_parse_error_never_echoes_raw_control_characters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run(capsys, ["--json", "nope\x1b[31m\u202e"])
    assert code == 2
    assert "\x1b" not in out + err
    assert "\u202e" not in out + err


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "--verbose", "--timeout", "5s", "--config", "c.yaml", *VALID["show"]],
        ["--quiet", *VALID["show"]],
        ["show", PR, "--json", "--timeout", "1.5", "--config", "c.yaml", "--verbose"],
        ["--timeout", "250ms", "show", PR],
        ["--timeout", "2d", "show", PR],
        ["--timeout=30", "show", PR],
    ],
)
def test_global_flags_parse_in_either_position(
    argv: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    config = tmp_path / "c.yaml"  # `--config` must name a file that exists
    config.write_text("", encoding="utf-8")
    reaches_handler(capsys, [str(config) if a == "c.yaml" else a for a in argv], "show")


def test_a_positional_after_double_dash_is_not_a_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = run(capsys, ["--json", "show", "--", "github.com/o/r#1"])
    assert code == 1
    assert envelope(out)["message"] == "show: not implemented yet"


@pytest.mark.parametrize(
    "extra",
    [
        ["--state", "closed"],
        ["--state", "all"],
        ["--draft"],
        ["--no-draft"],
        ["--limit", "1000"],
        ["--limit", "1"],
        ["--updated-since", "2026-01-01T00:00:00Z"],
        ["--updated-since", "2026-01-01T00:00:00.5+02:00"],
        ["--updated-since", "7d"],
        ["--updated-since", "90"],
        ["--review-requested-from", "me"],
    ],
)
def test_prs_options_parse(extra: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    reaches_handler(capsys, ["prs", "--author", "bob", *extra], "prs")


def test_prs_with_repo_needs_no_person(capsys: pytest.CaptureFixture[str]) -> None:
    reaches_handler(capsys, ["prs", "--repo", "github.com/o/r"], "prs")
    reaches_handler(capsys, ["prs", "--host", "github.com", "--author", "me"], "prs")


def test_uppercase_expected_head_is_lowered(capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[str] = []

    def handler(inv: Invocation) -> dict[str, Any]:
        seen.append(inv.args.expected_head)
        raise ForgectlError("internal", "stop")

    registry.HANDLERS["merge"] = handler
    run(capsys, ["merge", PR, "--expected-head", "A" * 40, "--expected-base", "main"])
    assert seen == ["a" * 40]


# --- the human-only gate through the CLI -------------------------------------------------------


def test_human_only_flag_with_human_actor_reaches_the_handler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["merge", PR, *EXPECTED, "--auto", "--actor", "human:bob"]
    reaches_handler(capsys, argv, "merge")


def test_human_only_flag_on_a_tty_reaches_the_handler(capsys: pytest.CaptureFixture[str]) -> None:
    reaches_handler(capsys, ["merge", PR, *EXPECTED, "--override-blocks"], "merge", TTY)


def test_explicit_actor_beats_the_tty_through_the_cli(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(
        capsys, ["merge", PR, *EXPECTED, "--override-blocks", "--actor", "auto:x"], tty=TTY
    )
    assert code == 2
    assert "--override-blocks" in err


def test_interactive_merge_needs_no_expected_values(capsys: pytest.CaptureFixture[str]) -> None:
    reaches_handler(capsys, ["merge", PR], "merge", TTY)


def test_dash_with_stdin_tty_is_usage_through_the_cli(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, ["comment", PR, "--body-file", "-"], tty=TTY)
    assert code == 2
    assert "stdin" in err


def test_dash_without_tty_reaches_the_handler(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["comment", PR, "--body-file", "-", "--actor", "auto:x"]
    reaches_handler(capsys, argv, "comment")


def test_handler_sees_the_human_context(capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[Invocation] = []

    def handler(inv: Invocation) -> dict[str, Any]:
        seen.append(inv)
        return {}

    registry.HANDLERS["comment"] = handler
    run(
        capsys,
        ["comment", PR, "--body-file", "b", "--actor", "human:bob", "--decision-id", "d1"],
    )
    (inv,) = seen
    assert inv.human is not None
    assert (inv.human.actor, inv.human.is_human, inv.human.decision_ids) == (
        "human:bob",
        True,
        ("d1",),
    )
    assert inv.command == "comment"
    assert inv.json is False


def test_reads_have_no_human_context(capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[Invocation] = []

    def handler(inv: Invocation) -> dict[str, Any]:
        seen.append(inv)
        return {}

    registry.HANDLERS["show"] = handler
    run(capsys, ["show", PR], tty=TTY)
    assert seen[0].human is None


def test_confirmation_required_is_exit_13_with_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(inv: Invocation) -> dict[str, Any]:
        assert inv.human is not None
        require_confirmation(inv.human)
        return {}

    registry.HANDLERS["comment"] = handler
    code, out, _ = run(
        capsys, ["--json", "comment", PR, "--body-file", "-", "--actor", "human:bob"]
    )
    assert code == 13
    assert envelope(out)["code"] == "confirmation_required"


def test_yes_lets_a_stdin_body_proceed(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(inv: Invocation) -> dict[str, Any]:
        assert inv.human is not None
        assert require_confirmation(inv.human) is False
        raise ForgectlError("internal", "reached")

    registry.HANDLERS["comment"] = handler
    argv = ["comment", PR, "--body-file", "-", "--actor", "human:bob", "--yes"]
    code, _, err = run(capsys, argv)
    assert code == 1
    assert "reached" in err


# --- success, exit codes, output by exit code ---------------------------------------------


def good_resolve(inv: Invocation) -> dict[str, Any]:
    return {
        "pr": PR,
        "action": "resolve",
        "thread_id": "gh:rt:abc",
        "resolved": True,
        "changed": True,
        "dry_run": False,
    }


def test_success_prints_the_result_object_validated_against_its_schema(
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry.HANDLERS["resolve"] = good_resolve
    code, out, err = run(capsys, ["--json", "resolve", PR, "--thread", "gh:rt:abc"])
    assert (code, err) == (0, "")
    validate(json.loads(out), "forgectl/cli/resolve.json")


def test_success_without_json_prints_readable_text(capsys: pytest.CaptureFixture[str]) -> None:
    registry.HANDLERS["resolve"] = good_resolve
    code, out, _ = run(capsys, ["resolve", PR, "--thread", "gh:rt:abc"])
    assert code == 0
    assert "thread_id: gh:rt:abc" in out
    assert not out.lstrip().startswith("{")


def test_human_output_is_never_raw(capsys: pytest.CaptureFixture[str]) -> None:
    registry.HANDLERS["show"] = lambda inv: {"title": "evil\x1b]0;x\x07\u202e"}
    code, out, _ = run(capsys, ["show", PR])
    assert code == 0
    assert "\x1b" not in out and "\u202e" not in out and "\x07" not in out


@pytest.mark.parametrize("code_name", sorted(EXIT_BY_CODE))
def test_handler_errors_become_their_exit_code(
    code_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from .test_errors import _build

    def handler(inv: Invocation) -> dict[str, Any]:
        raise _build(code_name)

    registry.HANDLERS["show"] = handler
    code, out, _ = run(capsys, ["--json", "show", PR])
    assert code == EXIT_BY_CODE[code_name]
    error = envelope(out)
    assert error["code"] == code_name
    assert ("reason" in error) == (code_name == "refused")


def test_unexpected_exception_is_internal_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(inv: Invocation) -> dict[str, Any]:
        raise RuntimeError("secret detail")

    registry.HANDLERS["show"] = handler
    code, out, err = run(capsys, ["--json", "show", PR])
    assert code == 1
    assert envelope(out)["code"] == "internal"
    assert "Traceback" not in out + err
    assert "secret detail" not in out + err
    assert "RuntimeError" in envelope(out)["message"]


# --- --dry-run and would_refuse -----------------------------------------------------------


def refusing(reason: str = "stale_head", **kw: Any) -> Callable[[Invocation], dict[str, Any]]:
    def handler(inv: Invocation) -> dict[str, Any]:
        raise refused(reason, "the head moved", **kw)

    return handler


DRY_ARGV = {
    "merge": ["merge", PR, *EXPECTED, "--dry-run"],
    "review": ["review", PR, "--file", "r.json", *EXPECTED, "--dry-run"],
    "comment": ["comment", PR, "--body-file", "b.md", "--dry-run"],
    "reply": ["reply", PR, "--to", "gh:rc:abc", "--body-file", "b.md", "--dry-run"],
    "resolve": ["resolve", PR, "--thread", "gh:rt:abc", "--dry-run"],
    "unresolve": ["unresolve", PR, "--thread", "gh:rt:abc", "--dry-run"],
    "approve": ["approve", PR, *EXPECTED, "--dry-run"],
}


@pytest.mark.parametrize("command", sorted(DRY_ARGV))
def test_dry_run_refusal_is_would_refuse_exit_20_against_the_schema(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    registry.HANDLERS[command] = refusing()
    code, out, _ = run(capsys, ["--json", *DRY_ARGV[command]])
    assert code == 20
    result = json.loads(out)
    validate(result, f"forgectl/cli/{command}.json")
    assert result["dry_run"] is True
    assert result["pr"] == PR
    assert result["would_refuse"] == {
        "reason": "stale_head",
        "message": "the head moved",
        "details": None,
    }


def test_dry_run_merge_plan_echoes_the_given_flags(capsys: pytest.CaptureFixture[str]) -> None:
    registry.HANDLERS["merge"] = refusing("no_head_match")
    argv = [
        "--json",
        "merge",
        PR,
        *EXPECTED,
        "--method",
        "squash",
        "--auto",
        "--dry-run",
        "--actor",
        "human:bob",
    ]
    code, out, _ = run(capsys, argv)
    assert code == 20
    assert json.loads(out)["plan"] == {
        "head_sha": None,
        "expected_head": H1,
        "expected_base": "main",
        "base_ref": None,
        "auto": True,
        "override_blocks": False,
        "blocked_by": None,
        "method": "squash",
    }


def test_dry_run_plan_takes_the_handlers_values(capsys: pytest.CaptureFixture[str]) -> None:
    registry.HANDLERS["merge"] = refusing(
        "not_ready",
        details={"blocked_by": [{"code": "draft"}]},
        plan={"head_sha": H1, "base_ref": "main", "blocked_by": [{"code": "draft"}]},
    )
    code, out, _ = run(capsys, ["--json", *DRY_ARGV["merge"]])
    result = json.loads(out)
    validate(result, "forgectl/cli/merge.json")
    assert result["plan"]["head_sha"] == H1
    assert result["would_refuse"]["details"] == {"blocked_by": [{"code": "draft"}]}


def test_dry_run_post_fields_come_from_the_handler(capsys: pytest.CaptureFixture[str]) -> None:
    registry.HANDLERS["reply"] = refusing(
        "not_open",
        details={"state": "merged"},
        plan={"body_sha256": "c" * 64, "body_bytes": 12},
        fields={"threaded": True, "head_sha": H1},
    )
    code, out, _ = run(capsys, ["--json", *DRY_ARGV["reply"]])
    result = json.loads(out)
    validate(result, "forgectl/cli/reply.json")
    assert result["threaded"] is True
    assert result["head_sha"] == H1
    assert result["plan"]["body_bytes"] == 12


def test_dry_run_needs_no_yes_and_no_tty(capsys: pytest.CaptureFixture[str]) -> None:
    registry.HANDLERS["comment"] = refusing("not_open", details={"state": "closed"})
    code, _, _ = run(capsys, ["--json", *DRY_ARGV["comment"]], tty=NO_TTY)
    assert code == 20


def test_dry_run_does_not_ask_for_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(inv: Invocation) -> dict[str, Any]:
        assert inv.human is not None
        assert require_confirmation(inv.human) is False
        raise refused("not_open", "closed", details={"state": "closed"})

    registry.HANDLERS["comment"] = handler
    assert run(capsys, ["--json", *DRY_ARGV["comment"]])[0] == 20


def test_dry_run_other_failure_prints_the_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    def handler(inv: Invocation) -> dict[str, Any]:
        raise ForgectlError("not_found", "no such pr")

    registry.HANDLERS["merge"] = handler
    code, out, _ = run(capsys, ["--json", *DRY_ARGV["merge"]])
    assert code == 10
    assert envelope(out)["code"] == "not_found"


def test_refusal_without_dry_run_is_the_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    registry.HANDLERS["merge"] = refusing()
    code, out, _ = run(capsys, ["--json", "merge", PR, *EXPECTED])
    assert code == 20
    error = envelope(out)
    assert (error["code"], error["reason"]) == ("refused", "stale_head")


def test_dry_run_would_refuse_without_json_is_text(capsys: pytest.CaptureFixture[str]) -> None:
    registry.HANDLERS["merge"] = refusing()
    code, out, err = run(capsys, DRY_ARGV["merge"])
    assert code == 20
    assert "would_refuse" in out
    assert "stale_head" in err


# --- partial results ----------------------------------------------------------------------------


def partial(inv: Invocation) -> dict[str, Any]:
    raise ForgectlError(
        "partial_results",
        "one host failed",
        output={
            "items": [],
            "complete": True,
            "errors": [{"host": "gitlab.com", "code": "auth_required", "message": "no token"}],
            "hosts_queried": ["github.com", "gitlab.com"],
            "rate_limit": {"host": "github.com", "remaining": None, "reset_at": None},
        },
    )


def test_prs_partial_results_print_the_schema_and_exit_12(
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry.HANDLERS["prs"] = partial
    code, out, err = run(capsys, ["--json", "prs", "--author", "me"])
    assert code == 12
    validate(json.loads(out), "forgectl/cli/prs.json")
    assert "partial_results" in err


def test_poll_partial_results_print_the_schema_and_exit_12(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def partial_poll(inv: Invocation) -> dict[str, Any]:
        raise ForgectlError(
            "partial_results",
            "one host failed",
            output={
                "items": [],
                "complete": False,
                "errors": [{"host": "gitlab.com", "code": "auth_required", "message": "no token"}],
                "hosts_queried": ["github.com", "gitlab.com"],
                "unsupported": [],
                "truncated": [],
                "rate_limit": {"host": "github.com", "remaining": None, "reset_at": None},
            },
        )

    registry.HANDLERS["poll"] = partial_poll
    code, out, err = run(capsys, ["--json", "poll"])
    assert code == 12
    validate(json.loads(out), "forgectl/cli/poll.json")
    assert "partial_results" in err


def test_partial_results_from_another_command_is_the_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry.HANDLERS["show"] = partial
    code, out, _ = run(capsys, ["--json", "show", PR])
    assert code == 12
    assert envelope(out)["code"] == "partial_results"


def test_partial_results_without_output_is_the_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(inv: Invocation) -> dict[str, Any]:
        raise ForgectlError("partial_results", "all hosts failed")

    registry.HANDLERS["whoami"] = handler
    code, out, _ = run(capsys, ["--json", "whoami"])
    assert code == 12
    assert envelope(out)["code"] == "partial_results"


# --- signals ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sig", "expected"), [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)]
)
def test_a_signal_exits_128_plus_n_with_no_envelope(
    sig: signal.Signals, expected: int, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(inv: Invocation) -> dict[str, Any]:
        os.kill(os.getpid(), sig)
        raise AssertionError("signal handler did not run")

    registry.HANDLERS["show"] = handler
    previous = signal.getsignal(sig)
    code, out, err = run(capsys, ["--json", "show", PR])
    assert code == expected
    assert (out, err) == ("", "")
    assert signal.getsignal(sig) == previous


def test_signal_handlers_are_restored_after_a_normal_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    run(capsys, ["show", PR])
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before


# --- misplaced write flags, branch names, durations, JSON detection -----------------------------


@pytest.mark.parametrize(
    "flag",
    [["--yes"], ["--dry-run"], ["--actor", "human:bob"], ["--decision-id", "d"], ["--actor=auto:x"]]
    + [["--override-escalation"]],
    ids=lambda f: f[0],
)
def test_a_write_flag_before_the_command_says_it_must_follow_it(
    flag: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run(capsys, [*flag, "merge", PR, *EXPECTED])
    assert code == 2
    assert f"{flag[0].split('=')[0]} must follow the command" in err


def test_write_flags_after_the_command_still_parse(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["merge", PR, *EXPECTED, "--dry-run", "--yes", "--actor", "auto:x", "--decision-id", "d"]
    reaches_handler(capsys, argv, "merge")


@pytest.mark.parametrize(
    "name",
    [
        "a//b",
        "a/.b",
        ".a",
        "a.lock",
        "a/b.lock",
        "a.lock/b",
        "@",
        "HEAD",
        "a@{b",
        "a..b",
        "a/",
        "/a",
        "a.",
        "a b",
        "a~b",
        "a^b",
        "a:b",
        "a?b",
        "a*b",
        "a[b",
        "a\\b",
        "a\nb",
        "a\tb",
        "a\x7fb",
        "a\x85b",
        "a\u202eb",
        "a\u200bb",
        "a\u2028b",
        "a\u2029b",
        "a\ud800b",
    ],
    ids=repr,
)
def test_invalid_branch_names_are_usage(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = run(capsys, ["merge", PR, "--expected-head", H1, f"--expected-base={name}"])
    assert code == 2


@pytest.mark.parametrize(
    "name", ["main", "feature/x-1", "release/1.2", "é/ü", "a./b", "@x", "x@", "a.lock.b", "日本"]
)
def test_valid_branch_names_parse(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["merge", PR, "--expected-head", H1, f"--expected-base={name}"]
    reaches_handler(capsys, argv, "merge")


def test_a_duration_of_exactly_a_hundred_years_parses(capsys: pytest.CaptureFixture[str]) -> None:
    reaches_handler(capsys, ["--timeout", "36500d", "show", PR], "show")
    reaches_handler(capsys, ["export", "--since", "36500d"], "export")


@pytest.mark.parametrize("flag", ["--json=x", "--json="])
@pytest.mark.parametrize("position", ["before", "after"])
def test_a_json_flag_with_a_value_is_still_json_mode_for_the_usage_error(
    flag: str, position: str, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [flag, "show", PR] if position == "before" else ["show", PR, flag]
    code, out, _ = run(capsys, argv)
    assert code == 2
    assert envelope(out)["code"] == "usage"


def test_signal_handlers_stay_installed_while_an_error_is_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from forgectl import cli

    seen: list[object] = []
    real = cli.emit_error

    def spy(*args: Any, **kwargs: Any) -> int:
        seen.append(signal.getsignal(signal.SIGTERM))
        return real(*args, **kwargs)

    monkeypatch.setattr(cli, "emit_error", spy)
    run(capsys, ["show", PR])
    assert seen
    assert seen[0] not in (signal.SIG_DFL, signal.SIG_IGN)


def test_a_signal_while_the_error_is_printed_still_exits_128_plus_n(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from forgectl import cli

    def killed(*args: Any, **kwargs: Any) -> int:
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("signal handler did not run")

    monkeypatch.setattr(cli, "emit_error", killed)
    assert run(capsys, ["show", PR])[0] == 143


# --- one line per field ---------------------------------------------------------------------


def test_an_argv_with_a_newline_cannot_forge_a_stderr_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, err = run(capsys, ["show", PR, "x\nforgectl: error: internal: fake"])
    assert code == 2
    assert err.count("\n") == 1
    assert "<U+000A>" in err


def test_a_handler_message_with_a_newline_stays_on_one_stderr_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(inv: Invocation) -> dict[str, Any]:
        raise ForgectlError("not_found", "gone\r\nforgectl: error: fake\tx")

    registry.HANDLERS["show"] = handler
    code, out, err = run(capsys, ["--json", "show", PR])
    assert code == 10
    assert err.count("\n") == 1
    assert envelope(out)["message"] == "gone\nforgectl: error: fake\tx"


def test_human_output_is_one_line_per_field(capsys: pytest.CaptureFixture[str]) -> None:
    registry.HANDLERS["show"] = lambda inv: {
        "title": "a\nforged: yes",
        "ke\ny": "v",
        "labels": ["x\ny"],
    }
    code, out, _ = run(capsys, ["show", PR])
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == 3
    assert lines[0] == "title: a<U+000A>forged: yes"
    assert lines[1].startswith("ke<U+000A>y: ")


# --- would_refuse echoes argv, so it sanitizes it --------------------------------------------


def test_dry_run_would_refuse_sanitizes_the_echoed_input() -> None:
    """The parsers already refuse control characters in `pr` and `--thread`; the echo is
    sanitized anyway, in case a parsed value ever carries one."""
    args = Namespace(pr="x\x1b[31m\u202e\udcff\nz", thread="t\x07\ud800")
    result = would_refuse_result("resolve", args, refused("not_open", "m", details={"state": "c"}))
    assert result["pr"] == "x<U+001B>[31m<U+202E><U+DCFF><U+000A>z"
    assert result["thread_id"] == "t<U+0007><U+D800>"


def test_dry_run_plan_values_from_the_handler_are_sanitized(
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry.HANDLERS["merge"] = refusing(
        "no_head_match", plan={"base_ref": "m\x1bain"}, fields={"method": "sq\u202e"}
    )
    code, out, _ = run(capsys, ["--json", *DRY_ARGV["merge"]])
    result = json.loads(out)
    assert result["plan"]["base_ref"] == "m<U+001B>ain"
    assert result["method"] == "sq<U+202E>"


@pytest.mark.parametrize("command", ["resolve", "unresolve"])
def test_dry_run_refusal_with_a_handler_plan_is_would_refuse_only(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    registry.HANDLERS[command] = refusing(
        "not_resolvable", plan={"body_bytes": 3}, fields={"thread_id": "gh:rt:zzz"}
    )
    code, out, err = run(capsys, ["--json", *DRY_ARGV[command]])
    assert code == 20, err
    result = json.loads(out)
    validate(result, f"forgectl/cli/{command}.json")
    assert "plan" not in result
    assert result["thread_id"] == "gh:rt:zzz"


# --- stdin spellings and parse-only coverage --------------------------------------------------


@pytest.mark.parametrize("spelling", ["-", "/dev/stdin", "/dev/fd/0"])
@pytest.mark.parametrize(
    ("command", "flag"),
    [("comment", "--body-file"), ("review", "--file"), ("approve", "--body-file")],
)
def test_every_spelling_of_stdin_with_a_stdin_tty_is_usage(
    spelling: str, command: str, flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run(capsys, [command, PR, flag, spelling], tty=TTY)
    assert code == 2
    assert "stdin" in err


def test_the_version_is_the_installed_packages(capsys: pytest.CaptureFixture[str]) -> None:
    from importlib.metadata import version

    from forgectl import __version__

    assert __version__ == version("forgectl")
    with pytest.raises(SystemExit):
        main(["--version"], tty=NO_TTY)
    assert capsys.readouterr().out.strip() == f"forgectl {__version__}"


def test_export_whoami_and_browse_flags_parse(capsys: pytest.CaptureFixture[str]) -> None:
    reaches_handler(capsys, ["export", "--since", "2026-01-01T00:00:00Z"], "export")
    reaches_handler(capsys, ["export", "--since", "7d"], "export")
    reaches_handler(capsys, ["whoami", "--host", "github.com"], "whoami")
    reaches_handler(capsys, ["browse", "--focus", PR], "browse")
    reaches_handler(capsys, ["browse", "--result-file", "out.json"], "browse")
    for good in ("0", "30", "90s", "2m", "1.5", "1", "1000ms"):
        reaches_handler(capsys, ["browse", "--interval", good], "browse")
    reaches_handler(capsys, ["browse", "--focus", PR, "--result-file", "out.json"], "browse")


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (
            ["review", PR, "--file", "r.json", *EXPECTED, "--allow-self", "--actor", "human:b"],
            "review",
        ),
        (
            [
                "review",
                PR,
                "--file",
                "r.json",
                *EXPECTED,
                "--override-escalation",
                "--actor",
                "human:b",
            ],
            "review",
        ),
        (
            ["comment", PR, "--body-file", "b", "--override-escalation", "--actor", "human:b"],
            "comment",
        ),
        (
            ["resolve", PR, "--thread", "gh:rt:a", "--override-escalation", "--actor", "human:b"],
            "resolve",
        ),
        (
            ["unresolve", PR, "--thread", "gh:rt:a", "--override-escalation", "--actor", "human:b"],
            "unresolve",
        ),
    ],
)
def test_human_only_flags_parse_with_a_human_actor(
    argv: list[str], command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    reaches_handler(capsys, argv, command)


def test_a_human_actor_with_decision_ids_on_a_dry_run_reaches_the_handler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[Invocation] = []

    def handler(inv: Invocation) -> dict[str, Any]:
        seen.append(inv)
        raise refused("stale_head", "moved")

    registry.HANDLERS["merge"] = handler
    argv = [
        *DRY_ARGV["merge"],
        "--actor",
        "human:bob",
        "--decision-id",
        "d1",
        "--decision-id",
        "d2",
    ]
    code, _, _ = run(capsys, ["--json", *argv])
    assert code == 20
    assert seen[0].human is not None
    assert seen[0].human.decision_ids == ("d1", "d2")
    assert seen[0].human.dry_run is True
