from __future__ import annotations

from argparse import Namespace
from typing import Any

import pytest

from forgectl.errors import ForgectlError
from forgectl.humanactions import Tty, build_context, default_me_name, require_confirmation

HEAD = "a" * 40
TTY = Tty(stdin=True, stderr=True)
NO_TTY = Tty(stdin=False, stderr=False)


def ns(command: str = "comment", **kw: Any) -> Namespace:
    base: dict[str, Any] = {
        "command": command,
        "actor": None,
        "decision_id": None,
        "yes": False,
        "dry_run": False,
        "auto": False,
        "override_blocks": False,
        "allow_self": False,
        "override_escalation": False,
        "body_file": None,
        "file": None,
        "expected_head": None,
        "expected_base": None,
        "dedupe_key": None,
    }
    base.update(kw)
    return Namespace(**base)


def usage_message(args: Namespace, tty: Tty) -> str:
    with pytest.raises(ForgectlError) as exc:
        build_context(args, tty, me_name="me")
    assert exc.value.code == "usage"
    return exc.value.message


def test_default_actor_is_human_on_a_tty() -> None:
    ctx = build_context(ns(), TTY, me_name="jo")
    assert (ctx.actor, ctx.actor_source, ctx.is_human) == ("human:jo", "tty", True)


def test_default_actor_is_auto_unknown_without_a_tty() -> None:
    ctx = build_context(ns(), NO_TTY, me_name="jo")
    assert (ctx.actor, ctx.actor_source, ctx.is_human) == ("auto:unknown", "default", False)


def test_stdin_tty_alone_makes_the_default_actor_human() -> None:
    ctx = build_context(ns(), Tty(stdin=True, stderr=False), me_name="jo")
    assert ctx.is_human


def test_stderr_tty_alone_does_not_make_the_default_actor_human() -> None:
    ctx = build_context(ns(), Tty(stdin=False, stderr=True), me_name="jo")
    assert not ctx.is_human


def test_explicit_human_actor_without_a_tty() -> None:
    ctx = build_context(ns(actor="human:bob"), NO_TTY, me_name="jo")
    assert (ctx.actor, ctx.actor_source, ctx.is_human) == ("human:bob", "flag", True)


def test_explicit_auto_actor_beats_the_tty() -> None:
    ctx = build_context(ns(actor="auto:bot"), TTY, me_name="jo")
    assert (ctx.actor, ctx.actor_source, ctx.is_human) == ("auto:bot", "flag", False)


@pytest.mark.parametrize("flag", ["auto", "override_blocks", "allow_self", "override_escalation"])
def test_human_only_flag_without_a_human_actor_is_usage(flag: str) -> None:
    command = "merge" if flag in ("auto", "override_blocks") else "approve"
    args = ns(command, expected_head=HEAD, expected_base="main", **{flag: True})
    assert "--" + flag.replace("_", "-") in usage_message(args, NO_TTY)


@pytest.mark.parametrize("flag", ["auto", "override_blocks", "allow_self", "override_escalation"])
def test_human_only_flag_with_explicit_auto_actor_on_a_tty_is_usage(flag: str) -> None:
    args = ns("merge", actor="auto:x", expected_head=HEAD, expected_base="main", **{flag: True})
    assert "--" + flag.replace("_", "-") in usage_message(args, TTY)


def test_human_only_flag_with_explicit_human_actor_passes() -> None:
    args = ns("approve", actor="human:bob", allow_self=True, expected_head=HEAD, expected_base="m")
    assert build_context(args, NO_TTY, me_name="jo").is_human


def test_human_only_flag_on_a_tty_without_actor_passes() -> None:
    args = ns("approve", allow_self=True)
    assert build_context(args, TTY, me_name="jo").interactive


def test_interactive_needs_both_ttys() -> None:
    assert build_context(ns(), TTY, me_name="jo").interactive
    assert not build_context(ns(), Tty(stdin=True, stderr=False), me_name="jo").interactive
    assert not build_context(ns(), Tty(stdin=False, stderr=True), me_name="jo").interactive


def test_yes_makes_the_run_not_interactive() -> None:
    assert not build_context(ns(yes=True), TTY, me_name="jo").interactive


def test_auto_actor_is_never_interactive() -> None:
    assert not build_context(ns(actor="auto:x"), TTY, me_name="jo").interactive


@pytest.mark.parametrize("spelling", ["-", "/dev/stdin", "/dev/fd/0"])
@pytest.mark.parametrize(("command", "field"), [("comment", "body_file"), ("review", "file")])
def test_dash_with_stdin_a_tty_is_usage(command: str, field: str, spelling: str) -> None:
    assert "stdin" in usage_message(ns(command, **{field: spelling}), TTY)


def test_a_file_that_only_looks_like_stdin_is_a_file() -> None:
    assert build_context(ns(body_file="/dev/stdin2"), TTY, me_name="jo").interactive
    assert build_context(ns(body_file="dev/stdin"), TTY, me_name="jo").interactive


def test_dash_with_stdin_a_tty_is_usage_even_with_yes_and_dry_run() -> None:
    usage_message(ns(body_file="-", yes=True, dry_run=True), TTY)


def test_dash_without_a_tty_is_not_interactive_and_needs_yes_to_confirm() -> None:
    ctx = build_context(ns(actor="human:bob", body_file="-"), NO_TTY, me_name="jo")
    with pytest.raises(ForgectlError) as exc:
        require_confirmation(ctx)
    assert exc.value.code == "confirmation_required"
    assert exc.value.exit_code == 13


def test_dash_with_yes_confirms() -> None:
    ctx = build_context(ns(actor="human:bob", body_file="-", yes=True), NO_TTY, me_name="jo")
    assert require_confirmation(ctx) is False


def test_dash_is_only_stdin_when_the_whole_value() -> None:
    ctx = build_context(ns(body_file="-x"), TTY, me_name="jo")
    assert ctx.interactive


@pytest.mark.parametrize("command", ["merge", "review", "approve"])
def test_expected_values_required_when_not_interactive(command: str) -> None:
    msg = usage_message(ns(command), NO_TTY)
    assert "--expected-head" in msg and "--expected-base" in msg


@pytest.mark.parametrize("command", ["merge", "review", "approve"])
def test_each_missing_expected_value_is_named(command: str) -> None:
    assert "--expected-base" in usage_message(ns(command, expected_head=HEAD), NO_TTY)
    assert "--expected-head" in usage_message(ns(command, expected_base="main"), NO_TTY)


@pytest.mark.parametrize("command", ["merge", "review", "approve"])
def test_expected_values_not_required_when_interactive(command: str) -> None:
    assert build_context(ns(command), TTY, me_name="jo").interactive


@pytest.mark.parametrize("command", ["merge", "review", "approve"])
def test_expected_values_required_with_yes_even_on_a_tty(command: str) -> None:
    usage_message(ns(command, yes=True), TTY)


@pytest.mark.parametrize("command", ["merge", "review", "approve"])
def test_expected_values_required_for_a_dry_run_without_a_tty(command: str) -> None:
    usage_message(ns(command, dry_run=True), NO_TTY)


@pytest.mark.parametrize("command", ["merge", "review", "approve"])
def test_expected_values_accepted_when_given(command: str) -> None:
    args = ns(command, expected_head=HEAD, expected_base="main")
    assert not build_context(args, NO_TTY, me_name="jo").interactive


@pytest.mark.parametrize("command", ["comment", "reply", "resolve", "unresolve"])
def test_other_writes_need_no_expected_values(command: str) -> None:
    build_context(ns(command), NO_TTY, me_name="jo")


def test_merge_auto_with_override_blocks_is_usage() -> None:
    args = ns("merge", actor="human:bob", auto=True, override_blocks=True, expected_head=HEAD)
    args.expected_base = "main"
    assert "--auto" in usage_message(args, NO_TTY)


def test_approve_dedupe_key_requires_body_file() -> None:
    args = ns("approve", dedupe_key="k", expected_head=HEAD, expected_base="main")
    assert "--dedupe-key" in usage_message(args, NO_TTY)
    args.body_file = "note.md"
    build_context(args, NO_TTY, me_name="jo")


def test_decision_ids_kept_in_order_without_duplicates() -> None:
    ctx = build_context(ns(decision_id=["b", "a", "b"]), NO_TTY, me_name="jo")
    assert ctx.decision_ids == ("b", "a")


def test_more_than_32_decision_ids_is_usage() -> None:
    ids = [f"id{i}" for i in range(33)]
    assert "32" in usage_message(ns(decision_id=ids), NO_TTY)


def test_many_repeats_of_a_few_decision_ids_pass() -> None:
    ctx = build_context(ns(decision_id=["a", "b"] * 5000), NO_TTY, me_name="jo")
    assert ctx.decision_ids == ("a", "b")


def test_the_33rd_distinct_decision_id_fails_even_among_repeats() -> None:
    ids = [f"id{i}" for i in range(32)] * 3 + ["one-more"]
    assert "32" in usage_message(ns(decision_id=ids), NO_TTY)


def test_32_decision_ids_pass() -> None:
    ids = [f"id{i}" for i in range(32)]
    assert len(build_context(ns(decision_id=ids), NO_TTY, me_name="jo").decision_ids) == 32


def test_require_confirmation_not_interactive_without_yes_is_13() -> None:
    ctx = build_context(ns(actor="auto:x"), NO_TTY, me_name="jo")
    with pytest.raises(ForgectlError) as exc:
        require_confirmation(ctx)
    assert exc.value.code == "confirmation_required"


def test_require_confirmation_dry_run_needs_nothing() -> None:
    ctx = build_context(ns(actor="auto:x", dry_run=True), NO_TTY, me_name="jo")
    assert require_confirmation(ctx) is False


def test_require_confirmation_yes_skips_the_dialog() -> None:
    ctx = build_context(ns(actor="auto:x", yes=True), NO_TTY, me_name="jo")
    assert require_confirmation(ctx) is False


def test_require_confirmation_interactive_asks_for_a_dialog() -> None:
    assert require_confirmation(build_context(ns(), TTY, me_name="jo")) is True


class _Entry:
    def __init__(self, name: str) -> None:
        self.pw_name = name


def test_default_me_name_is_the_account_name_not_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("forgectl.humanactions.pwd.getpwuid", lambda uid: _Entry("alice"))
    for var in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        monkeypatch.setenv(var, "mallory")
    assert default_me_name() == "alice"


def test_default_me_name_is_unknown_without_an_account(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(uid: int) -> _Entry:
        raise KeyError(uid)

    monkeypatch.setattr("forgectl.humanactions.pwd.getpwuid", missing)
    monkeypatch.setenv("USER", "mallory")
    assert default_me_name() == "unknown"


def test_default_me_name_is_unknown_when_the_name_fails_the_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("forgectl.humanactions.pwd.getpwuid", lambda uid: _Entry("a b"))
    assert default_me_name() == "unknown"
