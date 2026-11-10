"""The golden-file helper: validates against schemas, compares, refuses silent blessing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import ValidationError

from .harness import golden

ENVELOPE_ID = "forgectl/cli/error.json"


@pytest.fixture
def golden_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "golden"
    directory.mkdir()
    monkeypatch.setattr(golden, "GOLDEN_DIR", directory)
    monkeypatch.delenv(golden.UPDATE_ENV, raising=False)
    monkeypatch.setattr(golden, "session_update", False)
    for name in golden.CI_VARS:
        monkeypatch.delenv(name, raising=False)
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
    with pytest.raises(AssertionError, match="make golden"):
        golden.assert_golden("new", GOOD, schema_id=ENVELOPE_ID)
    assert not (golden_dir / "new.json").exists()


def test_schema_id_is_required_and_none_is_an_explicit_opt_out(golden_dir: Path) -> None:
    (golden_dir / "a.json").write_text(golden.render({"any": 1}), encoding="utf-8")
    with pytest.raises(TypeError):
        golden.assert_golden("a", {"any": 1})  # type: ignore[call-arg]
    golden.assert_golden("a", {"any": 1}, schema_id=None)


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
        golden.assert_golden("c", {"user": {"login": "real-person"}}, schema_id=None)


@pytest.mark.parametrize("value", ["0", "true", "yes", " 1"])
def test_update_mode_needs_exactly_1(
    golden_dir: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(golden.UPDATE_ENV, value)
    with pytest.raises(AssertionError, match="no golden file"):
        golden.assert_golden("e", GOOD, schema_id=ENVELOPE_ID)
    assert not (golden_dir / "e.json").exists()


@pytest.mark.parametrize(
    ("name", "value"), [("CI", "true"), ("CI", ""), ("GITHUB_ACTIONS", "true"), ("GITLAB_CI", "")]
)
def test_update_mode_is_refused_in_ci(
    golden_dir: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(golden.UPDATE_ENV, "1")
    monkeypatch.setenv(name, value)
    with pytest.raises(AssertionError, match="refused in CI"):
        golden.assert_golden("d", GOOD, schema_id=ENVELOPE_ID)
    assert not (golden_dir / "d.json").exists()


@pytest.mark.parametrize("name", ["../x", "/abs", "a/../b", "", "A b", "x.json", "a\n"])
def test_hostile_names_are_rejected(golden_dir: Path, name: str) -> None:
    with pytest.raises(ValueError):
        golden.assert_golden(name, GOOD, schema_id=ENVELOPE_ID)


def test_a_symlinked_subdir_cannot_write_outside_the_golden_dir(
    golden_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (golden_dir / "link").symlink_to(outside)
    monkeypatch.setenv(golden.UPDATE_ENV, "1")
    with pytest.raises(ValueError, match="outside"):
        golden.assert_golden("link/a", GOOD, schema_id=ENVELOPE_ID)
    assert not (outside / "a.json").exists()


def test_a_real_cli_output_against_a_checked_in_golden(run_forgectl: Any) -> None:
    # A usage error: stable across every stage, from the real entry point as a child process.
    done = run_forgectl(["show", "--json"])
    assert done.returncode == 2
    envelope = json.loads(done.stdout)
    golden.assert_golden("harness/usage-error", envelope, schema_id=ENVELOPE_ID)


# End to end: a child pytest run with the real plugin, against a temporary golden directory.

REPO = Path(__file__).resolve().parents[1]
INNER_TEST = """
import os
from pathlib import Path

from tests.harness import golden


def test_value() -> None:
    golden.GOLDEN_DIR = Path(os.environ["INNER_GOLDEN_DIR"])
    golden.assert_golden("x", {"a": 2}, schema_id=None)
"""


def _inner(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> Any:
    work = tmp_path / "inner"
    work.mkdir(exist_ok=True)
    (work / "conftest.py").write_text('pytest_plugins = ("tests.harness.fixtures",)\n')
    (work / "test_inner.py").write_text(INNER_TEST)
    home = tmp_path / "inner-home"
    home.mkdir(exist_ok=True)
    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(REPO),
        "INNER_GOLDEN_DIR": str(tmp_path / "inner-golden"),
        **(env or {}),
    }
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args],
        cwd=work,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_normal_run_never_rewrites_a_golden(tmp_path: Path) -> None:
    target = tmp_path / "inner-golden" / "x.json"
    target.parent.mkdir()
    target.write_text(golden.render({"a": 1}), encoding="utf-8")
    done = _inner(tmp_path)
    assert done.returncode == 1, done.stdout
    assert "golden x differs" in done.stdout
    assert json.loads(target.read_text()) == {"a": 1}


def test_update_golden_option_regenerates_end_to_end(tmp_path: Path) -> None:
    target = tmp_path / "inner-golden" / "x.json"
    done = _inner(tmp_path, "--update-golden")
    assert done.returncode == 0, done.stdout
    assert json.loads(target.read_text()) == {"a": 2}
    assert _inner(tmp_path).returncode == 0  # a normal run now matches


def test_env_var_survives_the_per_test_environment_scrub(tmp_path: Path) -> None:
    # Regression for #84: the shared fixture clears FORGECTL_* before each test.
    done = _inner(tmp_path, env={golden.UPDATE_ENV: "1"})
    assert done.returncode == 0, done.stdout
    assert (tmp_path / "inner-golden" / "x.json").exists()


def test_update_golden_option_is_refused_in_ci(tmp_path: Path) -> None:
    done = _inner(tmp_path, "--update-golden", env={"CI": "true"})
    assert done.returncode == 1, done.stdout
    assert "refused in CI" in done.stdout
    assert not (tmp_path / "inner-golden" / "x.json").exists()
