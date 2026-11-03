"""The golden-file helper: validates against schemas, compares, refuses silent blessing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from forgectl.cli import main

from .harness import golden

ENVELOPE_ID = "forgectl/cli/error.json"


@pytest.fixture
def golden_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "golden"
    directory.mkdir()
    monkeypatch.setattr(golden, "GOLDEN_DIR", directory)
    monkeypatch.delenv(golden.UPDATE_ENV, raising=False)
    monkeypatch.delenv("CI", raising=False)
    return directory


GOOD = {"error": {"code": "internal", "message": "x"}}


def test_matching_golden_passes(golden_dir: Path) -> None:
    (golden_dir / "a.json").write_text(golden.render(GOOD), encoding="utf-8")
    golden.assert_golden("a", GOOD, schema_id=ENVELOPE_ID)


def test_differing_output_fails_with_a_diff(golden_dir: Path) -> None:
    (golden_dir / "a.json").write_text(golden.render(GOOD), encoding="utf-8")
    other = {"error": {"code": "internal", "message": "y"}}
    with pytest.raises(AssertionError, match='-    "message": "x"'):
        golden.assert_golden("a", other, schema_id=ENVELOPE_ID)


def test_missing_golden_fails_and_is_not_created(golden_dir: Path) -> None:
    with pytest.raises(AssertionError, match=golden.UPDATE_ENV):
        golden.assert_golden("new", GOOD)
    assert not (golden_dir / "new.json").exists()


def test_output_the_schema_rejects_fails_even_if_the_golden_matches(golden_dir: Path) -> None:
    bad = {"error": {"code": "internal", "message": 5}}
    (golden_dir / "a.json").write_text(golden.render(bad), encoding="utf-8")
    with pytest.raises(ValidationError):
        golden.assert_golden("a", bad, schema_id=ENVELOPE_ID)


def test_update_mode_writes_but_never_blesses_invalid_output(
    golden_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(golden.UPDATE_ENV, "1")
    golden.assert_golden("sub/a", GOOD, schema_id=ENVELOPE_ID)
    assert json.loads((golden_dir / "sub" / "a.json").read_text()) == GOOD
    with pytest.raises(ValidationError):
        golden.assert_golden("b", {"error": 1}, schema_id=ENVELOPE_ID)
    assert not (golden_dir / "b.json").exists()


def test_update_mode_refuses_unscrubbed_content(
    golden_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(golden.UPDATE_ENV, "1")
    with pytest.raises(AssertionError, match="unscrubbed"):
        golden.assert_golden("c", {"user": {"login": "real-person"}})


def test_update_mode_is_refused_in_ci(golden_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(golden.UPDATE_ENV, "1")
    monkeypatch.setenv("CI", "true")
    with pytest.raises(AssertionError, match="CI"):
        golden.assert_golden("d", GOOD)


@pytest.mark.parametrize("name", ["../x", "/abs", "a/../b", "", "A b", "x.json"])
def test_hostile_names_are_rejected(golden_dir: Path, name: str) -> None:
    with pytest.raises(ValueError):
        golden.assert_golden(name, GOOD)


def test_a_real_cli_output_against_a_checked_in_golden(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # No handler is registered for this command yet, so the envelope is the internal error.
    code = main(["whoami", "--json"])
    assert code == 1
    envelope = json.loads(capsys.readouterr().out)
    golden.assert_golden("harness/unimplemented-command", envelope, schema_id=ENVELOPE_ID)
