"""The recording scrubber and the check that no unscrubbed data file sits under ``tests/``."""

from __future__ import annotations

import json
from pathlib import Path

from .harness import scrub

TESTS_DIR = Path(__file__).resolve().parent

RECORDING = {
    "number": 7,
    "user": {"login": "jdoe-real", "name": "Jane Doe", "email": "jane.doe@corp.example.org"},
    "author": {"login": "dependabot[bot]"},
    "body": "thanks @jdoe-real and @other-person, cc jane.doe@corp.example.org",
    "html_url": "https://host.test/jdoe-real/widgets/pull/7",
    "commit": {"author": {"name": "Jane Doe", "email": "jd@personal.net", "date": "2026-01-01"}},
    "check": {"name": "build"},
}


def test_recording_with_a_name_and_an_email_comes_out_clean() -> None:
    out = scrub.Scrubber(extra=["widgets"]).scrub(RECORDING)
    text = json.dumps(out)
    for leaked in ("jdoe-real", "Jane Doe", "jane.doe", "corp.example.org", "other-person"):
        assert leaked not in text
    assert "jd@personal.net" not in text
    assert scrub.find_unscrubbed(text) == []


def test_scrubbing_is_consistent_and_keeps_shape() -> None:
    out = scrub.Scrubber().scrub(RECORDING)
    assert out["user"]["login"] == out["body"].split()[1].lstrip("@")
    assert out["author"]["login"].endswith("[bot]")
    assert out["number"] == 7
    assert out["check"]["name"] == "build"  # not a person's name


def test_scrubbing_is_idempotent() -> None:
    once = scrub.Scrubber().scrub(RECORDING)
    assert scrub.Scrubber().scrub(once) == once


def test_the_check_flags_each_kind_of_leak() -> None:
    kinds = {f.kind for f in scrub.find_unscrubbed(json.dumps(RECORDING), "x.json")}
    assert {"email", "identity-field", "mention"} <= kinds


def test_the_check_flags_a_token_shaped_secret() -> None:
    fake = "ghp_" + "a" * 36
    assert [f.kind for f in scrub.find_unscrubbed(f'{{"t": "{fake}"}}')] == ["secret"]


def test_the_check_fails_on_an_unscrubbed_file_in_a_tree(tmp_path: Path) -> None:
    (tmp_path / "rec.json").write_text(json.dumps(RECORDING), encoding="utf-8")
    assert scrub.check_tree(tmp_path)


def test_the_check_passes_on_a_scrubbed_tree(tmp_path: Path) -> None:
    clean = scrub.Scrubber().scrub(RECORDING)
    (tmp_path / "rec.json").write_text(json.dumps(clean), encoding="utf-8")
    assert scrub.check_tree(tmp_path) == []


def test_no_data_file_under_tests_holds_a_real_name_or_email() -> None:
    assert scrub.check_tree(TESTS_DIR) == []
