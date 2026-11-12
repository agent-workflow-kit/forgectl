"""`LiveSource.merge` (#60): the one merge command path, through the fake `gh`, as a human."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgectl import registry
from forgectl.errors import ForgectlError
from forgectl.humanactions import Tty
from forgectl.tui.source_live import LiveSource, in_process_merge_runner

from .harness import schemas
from .harness.forges import FakeForges
from .test_github_show import HEAD, OTHER
from .test_merge import BASE, PR, Rig, doc, merged


@pytest.fixture
def rig(
    tmp_path: Path,
    fake_forges: FakeForges,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Rig:
    return Rig(tmp_path, fake_forges, monkeypatch, capsys)


def source(rig: Rig) -> LiveSource:
    path = rig.config("me:\n  name: tester\n")
    return LiveSource(config_path=str(path), tty=lambda: Tty(stdin=True, stderr=True))


def test_a_merge_goes_through_the_command_as_a_human_on_a_tty(rig: Rig) -> None:
    rig.reads(doc(), merged())
    rig.put()
    out = source(rig).merge(PR, head=HEAD, base=BASE, method="squash")
    schemas.validate_cli_output("merge", out)
    assert out["merged"] is True and out["already_merged"] is False and out["method"] == "squash"
    (put,) = rig.puts()
    assert json.loads(put.stdin or "") == {"sha": HEAD, "merge_method": "squash"}
    first, second = rig.audit()
    assert first["actor"] == "human:tester" and first["inputs"]["actor_source"] == "tty"
    assert first["inputs"]["head_sha"] == HEAD and first["inputs"]["method"] == "squash"
    assert second["result"] == "merged"


def test_a_dry_run_writes_nothing_and_reports_the_plan(rig: Rig) -> None:
    rig.reads(doc())
    out = source(rig).merge(PR, head=HEAD, base=BASE, method="squash", dry_run=True)
    schemas.validate_cli_output("merge", out)
    assert out["dry_run"] is True and out["merged"] is False
    assert out["plan"]["head_sha"] == HEAD and out["plan"]["method"] == "squash"
    assert out["plan"]["override_blocks"] is False and out["plan"]["auto"] is False
    assert rig.puts() == []
    assert rig.audit() == []


def test_a_moved_head_is_refused_with_no_write(rig: Rig) -> None:
    rig.reads(doc(head=OTHER))
    with pytest.raises(ForgectlError) as caught:
        source(rig).merge(PR, head=HEAD, base=BASE, method="squash")
    assert (caught.value.code, caught.value.reason) == ("refused", "stale_head")
    assert rig.puts() == [] and rig.audit() == []


def test_a_moved_base_is_refused_with_no_write(rig: Rig) -> None:
    rig.reads(doc(base="release"))
    with pytest.raises(ForgectlError) as caught:
        source(rig).merge(PR, head=HEAD, base=BASE, method="squash")
    assert caught.value.reason == "stale_base"
    assert rig.puts() == []


def test_a_blocked_pr_is_refused_by_the_command_even_for_the_human(rig: Rig) -> None:
    rig.reads(doc(merge_state="BLOCKED"))
    with pytest.raises(ForgectlError) as caught:
        source(rig).merge(PR, head=HEAD, base=BASE, method="squash")
    assert caught.value.reason == "not_ready"
    assert rig.puts() == []


def test_an_unresolved_method_is_method_required_with_the_allowed_list(rig: Rig) -> None:
    rig.reads(doc(methods=(True, True, False)))
    with pytest.raises(ForgectlError) as caught:
        source(rig).merge(PR, head=HEAD, base=BASE, dry_run=True)
    assert caught.value.code == "method_required"
    assert (caught.value.details or {}).get("allowed") == ["squash", "merge"]
    assert rig.puts() == []


def test_an_option_looking_pr_never_reaches_the_runner() -> None:
    seen: list[list[str]] = []
    src = LiveSource(lambda argv: {}, merge_runner=lambda argv: seen.append(argv) or {})
    with pytest.raises(ForgectlError):
        src.merge("--yes", head=HEAD, base=BASE)
    assert seen == []


def test_the_arguments_are_the_shown_values_and_no_human_only_flag() -> None:
    seen: list[list[str]] = []
    src = LiveSource(lambda argv: {}, merge_runner=lambda argv: seen.append(argv) or {})
    base = "--evil base"
    src.merge(PR, head=HEAD, base=base, method="merge")
    assert seen == [
        [
            "merge",
            PR,
            "--yes",
            f"--expected-head={HEAD}",
            f"--expected-base={base}",
            "--method=merge",
        ]
    ]
    assert not any(a.startswith(("--override", "--auto", "--allow")) for a in seen[0])


@pytest.mark.parametrize("argv", [["snapshot", PR], ["whoami"], ["merge"]])
def test_the_merge_runner_runs_merge_and_nothing_else(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.load_handlers()
    called: list[str] = []
    for name in ("snapshot", "whoami", "merge"):
        monkeypatch.setitem(registry.HANDLERS, name, lambda inv: called.append(inv.command) or {})
    with pytest.raises(ForgectlError):
        in_process_merge_runner(None, {})(argv)
    assert called == []


def test_the_read_runner_still_refuses_merge() -> None:
    from forgectl.tui.source_live import in_process_runner

    with pytest.raises(ForgectlError) as caught:
        in_process_runner(None, {})(["merge", PR, "--yes"])
    assert caught.value.code == "internal"


def test_a_dry_run_logs_command_done_like_the_command_line(rig: Rig) -> None:
    """`cli._dispatch` logs `command_done` at info for every successful write command, a dry run
    included (`_success_level`): the runner does the same."""
    from forgectl import cli

    assert cli._success_level("merge", verbose=False) == "info"
    rig.reads(doc())
    source(rig).merge(PR, head=HEAD, base=BASE, method="squash", dry_run=True)
    done = [r for r in rig.log() if r["event"] == "command_done"]
    assert [(r["level"], (r["details"] or {}).get("command")) for r in done] == [("info", "merge")]


@pytest.mark.parametrize("tty", [Tty(False, True), Tty(True, False), Tty(False, False)])
def test_without_a_terminal_on_stdin_and_stderr_merge_is_refused_before_any_call(
    rig: Rig, tty: Tty
) -> None:
    path = rig.config("me:\n  name: tester\n")
    src = LiveSource(config_path=str(path), tty=lambda: tty)
    assert src.merge_unavailable() == "merge needs a terminal on stdin and stderr"
    rig.reads(doc())
    rig.put()
    with pytest.raises(ForgectlError):
        src.merge(PR, head=HEAD, base=BASE, method="squash")
    assert rig.puts() == [] and rig.audit() == [] and rig.calls() == []


def test_with_a_terminal_merge_is_available(rig: Rig) -> None:
    assert source(rig).merge_unavailable() is None


def test_the_real_terminal_is_the_default_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
    assert LiveSource(lambda argv: {}, merge_runner=lambda argv: {}).merge_unavailable() is not None
