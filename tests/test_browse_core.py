"""The parts of browse that never touch textual: focus, result file, the missing extra."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from forgectl import browse
from forgectl.cli import main
from forgectl.errors import ForgectlError

from .cli_schema_check import validate

PR = "github.com/acme/widgets#7"


def test_core_import_never_loads_textual() -> None:
    code = (
        "import sys, forgectl.cli, forgectl.browse, forgectl.registry;"
        "forgectl.registry.load_handlers();"
        "sys.exit(1 if any(m == 'textual' or m.startswith('textual.') for m in sys.modules) else 0)"
    )
    env = {"PATH": os.environ.get("PATH", ""), "HOME": "/nonexistent"}
    done = subprocess.run([sys.executable, "-I", "-c", code], env=env, check=False)
    assert done.returncode == 0


def test_without_the_extra_exit_2_with_the_install_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "textual", None)  # makes `import textual` fail
    assert main(["browse"]) == 2
    err = capsys.readouterr().err
    assert "forgectl[tui]" in err
    assert "pip install" in err


def test_without_the_extra_json_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "textual", None)
    assert main(["browse", "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "usage"
    validate(out, "forgectl/cli/error.json")


@pytest.mark.parametrize(
    "bad",
    [
        "github.com/acme/widgets",  # no number
        "github.com/acme/widgets#0",
        "github.com/acme/widgets#01",
        "github.com/acme/widgets#12345678901",
        "https://github.com/acme/widgets/pull/7",  # URL forms wait for identifiers.py
        "github.com/acme/../widgets#7",
        "github.com/acme/widgets#7\n",
        "github.com/acme/widgets#7 ; rm -rf /",
        "github.com/acme/wid\x1b]0;x\x07gets#7",
        "-" * 5000,
    ],
)
def test_focus_rejected_before_anything_starts(
    bad: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[str] = []
    monkeypatch.setattr(browse, "_run_app", lambda *a, **k: started.append("app"))
    assert main(["browse", "--focus", bad]) == 2
    assert started == []
    capsys.readouterr()


def test_focus_accepts_the_pr_form() -> None:
    assert browse.parse_focus(PR) == PR


def test_focus_lowercases_the_ascii_host_only() -> None:
    assert browse.parse_focus("GitHub.COM/Acme/Widgets#7") == "github.com/Acme/Widgets#7"
    with pytest.raises(ForgectlError):
        browse.parse_focus("G\u0130thub.com/acme/widgets#7")  # not ASCII: refused, not folded


def _result(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"intent": "back", "selected": [], "merged": []}
    base.update(over)
    return base


@pytest.mark.parametrize(
    "result",
    [
        _result(),
        _result(intent="open", selected=[PR]),
        _result(selected=[PR]),
    ],
)
def test_result_validation_agrees_with_the_schema_on_good_results(result: dict[str, Any]) -> None:
    validate(result, "forgectl/cli/browse-result.json")
    browse.validate_result(result)


@pytest.mark.parametrize(
    "result",
    [
        _result(intent="merge"),
        _result(intent="OPEN"),
        _result(selected=[PR, PR]),
        _result(selected=["nope"]),
        _result(extra=1),
        {"intent": "back", "selected": []},
        {"intent": "back", "selected": [], "merged": "x"},
    ],
)
def test_result_validation_agrees_with_the_schema_on_bad_results(result: dict[str, Any]) -> None:
    with pytest.raises(Exception):  # noqa: B017 - jsonschema's error type
        validate(result, "forgectl/cli/browse-result.json")
    with pytest.raises(ForgectlError):
        browse.validate_result(result)


def test_write_result_file_is_0600_atomic_and_valid(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    browse.write_result_file(str(target), _result(intent="open", selected=[PR]))
    assert (target.stat().st_mode & 0o777) == 0o600
    validate(json.loads(target.read_text()), "forgectl/cli/browse-result.json")
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"]  # no temp file left behind


def test_write_result_file_replaces_a_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    target.write_text("old")
    browse.write_result_file(str(target), _result())
    assert json.loads(target.read_text())["intent"] == "back"


def test_write_result_file_refuses_a_symlink_and_leaves_its_target(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep")
    link = tmp_path / "link.json"
    link.symlink_to(victim)
    with pytest.raises(ForgectlError):
        browse.preflight_result_file(str(link))
    with pytest.raises(ForgectlError) as exc:
        browse.write_result_file(str(link), _result())
    assert exc.value.code == "internal"  # a failure at write time is not a usage error
    assert victim.read_text() == "keep"
    assert link.is_symlink()


def test_preflight_refuses_a_fifo_a_directory_and_a_missing_parent(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    for bad in (fifo, tmp_path, tmp_path / "missing" / "out.json"):
        with pytest.raises(ForgectlError) as exc:
            browse.preflight_result_file(str(bad))
        assert exc.value.code == "usage"


def test_invalid_result_file_is_exit_2_before_the_app_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[str] = []
    monkeypatch.setattr(browse, "_run_app", lambda *a, **k: started.append("app"))
    assert main(["browse", "--result-file", str(tmp_path / "no" / "such" / "f.json")]) == 2
    assert started == []
    capsys.readouterr()


def test_nothing_is_written_when_the_result_is_invalid(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    with pytest.raises(ForgectlError):
        browse.write_result_file(str(target), _result(intent="merge"))
    assert not target.exists()


def test_result_validation_is_stricter_than_the_schema_on_a_trailing_newline() -> None:
    # The schema's `$` accepts a trailing LF; fullmatch here does not.
    with pytest.raises(ForgectlError):
        browse.validate_result(_result(merged=["github.com/acme/widgets#7\n"]))


def test_the_handler_writes_the_apps_result_to_the_result_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str | None] = []

    def fake_run(focus: str | None) -> dict[str, Any]:
        seen.append(focus)
        return _result(intent="open", selected=[PR])

    monkeypatch.setattr(browse, "_run_app", fake_run)
    target = tmp_path / "result.json"
    assert main(["browse", "--focus", PR, "--result-file", str(target)]) == 0
    assert seen == [PR]
    validate(json.loads(target.read_text()), "forgectl/cli/browse-result.json")
    assert (target.stat().st_mode & 0o777) == 0o600
    capsys.readouterr()


def test_the_handler_registers_through_the_registry() -> None:
    from forgectl import registry

    registry.load_handlers()
    assert registry.HANDLERS["browse"] is browse.browse


needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")


@needs_non_root
def test_preflight_refuses_an_unwritable_directory(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(ForgectlError) as exc:
            browse.preflight_result_file(str(locked / "out.json"))
        assert exc.value.code == "usage"
    finally:
        locked.chmod(0o700)


@needs_non_root
def test_an_unwritable_directory_is_exit_2_before_the_app_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[str] = []
    monkeypatch.setattr(browse, "_run_app", lambda *a, **k: started.append("app"))
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert main(["browse", "--result-file", str(locked / "out.json")]) == 2
    finally:
        locked.chmod(0o700)
    assert started == []
    capsys.readouterr()


@needs_non_root
def test_a_directory_made_unwritable_during_the_run_is_internal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    work = tmp_path / "work"
    work.mkdir()

    def fake_run(focus: str | None) -> dict[str, Any]:
        work.chmod(0o500)
        return _result()

    monkeypatch.setattr(browse, "_run_app", fake_run)
    try:
        assert main(["browse", "--json", "--result-file", str(work / "out.json")]) == 1
    finally:
        work.chmod(0o700)
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "internal"
    assert not (work / "out.json").exists()


def test_a_planted_symlink_at_the_old_temp_name_does_not_block_the_write(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    (tmp_path / f".out.json.{os.getpid()}.tmp").symlink_to(tmp_path / "elsewhere")
    browse.write_result_file(str(target), _result())
    assert json.loads(target.read_text())["intent"] == "back"
    assert not (tmp_path / "elsewhere").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        ["out.json", f".out.json.{os.getpid()}.tmp"]
    )


def test_the_temp_file_is_removed_when_the_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(src: str, dst: str) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(browse.os, "replace", boom)
    with pytest.raises(ForgectlError) as exc:
        browse.write_result_file(str(tmp_path / "out.json"), _result())
    assert exc.value.code == "internal"
    assert list(tmp_path.iterdir()) == []


def test_browse_json_success_output_matches_its_schema(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(browse, "_run_app", lambda focus: _result())
    assert main(["browse", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {}
    validate(out, "forgectl/cli/browse.json")
    with pytest.raises(Exception):  # noqa: B017 - jsonschema's error type
        validate({"x": 1}, "forgectl/cli/browse.json")


def test_an_app_that_stopped_on_an_error_is_internal_and_writes_no_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from forgectl.tui import run as tui_run

    class CrashedApp:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.return_code: int | None = None

        def run(self) -> None:
            self.return_code = 1

        def result(self) -> dict[str, Any]:
            return _result()

    monkeypatch.setattr(tui_run, "BrowseApp", CrashedApp)
    target = tmp_path / "out.json"
    assert main(["browse", "--json", "--result-file", str(target)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "internal"
    validate(out, "forgectl/cli/error.json")
    assert not target.exists()


def test_a_crash_inside_the_real_app_is_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    from forgectl.tui import run as tui_run
    from forgectl.tui.app import BrowseApp

    from .tui_fakes import FakeOpener, FakeSource

    def crash(self: BrowseApp) -> None:
        raise ValueError("boom")

    monkeypatch.setattr(BrowseApp, "on_mount", crash)
    app_holder: list[BrowseApp] = []

    class Headless(BrowseApp):
        def run(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
            app_holder.append(self)
            return super().run(headless=True)

    monkeypatch.setattr(tui_run, "BrowseApp", Headless)
    with pytest.raises(ForgectlError) as exc:
        tui_run.run_browse(focus=None, source=FakeSource(), opener=FakeOpener())
    assert exc.value.code == "internal"
    assert "boom" not in exc.value.message
