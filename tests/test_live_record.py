"""Record-and-scrub: nothing unscrubbed is written, and what is written passes ``check_tree``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from .harness.scrub import check_tree
from .live import record
from .live.record import RecordingRefused

THROWAWAY = "forgectl-live-20261112t101500z-ab12cd34"


def raw() -> dict[str, Any]:
    return {
        "number": 1,
        "title": f"fix in {THROWAWAY}",
        "user": {"login": "me-login", "email": "me@real-mail.test", "name": "Me Person"},
        "html_url": f"https://github.com/me-login/{THROWAWAY}/pull/1",
        "head": {"ref": "feature", "sha": "a" * 40},
        "body": "ping @" + "other-person about this",
    }


def test_a_recording_is_scrubbed_and_written_under_the_recordings_root(tmp_path: Path) -> None:
    root = tmp_path / "recordings"
    dest = root / "github" / "pull.json"
    path = record.write_recording(raw(), dest, logins=["me-login"], extra=[THROWAWAY], root=root)
    text = path.read_text(encoding="utf-8")
    for leaked in ("me-login", "me@real-mail.test", "Me Person", "other-person", "forgectl-live-"):
        assert leaked not in text
    assert json.loads(text)["head"] == {"ref": "feature", "sha": "a" * 40}
    assert check_tree(root) == []


def test_a_recording_outside_the_root_or_not_json_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "recordings"
    for dest in (tmp_path / "elsewhere" / "x.json", root / "x.txt", root / ".." / "x.json"):
        with pytest.raises(RecordingRefused):
            record.write_recording(raw(), dest, logins=["me-login"], root=root)
    assert not (tmp_path / "elsewhere").exists()


def test_at_least_one_login_must_be_named(tmp_path: Path) -> None:
    with pytest.raises(RecordingRefused):
        record.scrub_recording(raw(), logins=[])


def test_a_token_value_in_a_recording_is_refused() -> None:
    document = {"note": "the token was tok-live-0001 today"}
    with pytest.raises(RecordingRefused):
        record.scrub_recording(document, logins=["me-login"], secrets=["tok-live-0001"])


def test_a_secret_shaped_string_never_survives() -> None:
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    document = {"headers": {"note": secret, "authorization": "Bearer " + "x" * 30}}
    try:
        text = record.scrub_recording(document, logins=["me-login"])
    except RecordingRefused:
        return
    assert secret not in text and "x" * 30 not in text


def test_a_login_inside_free_text_is_hidden_or_the_recording_refused() -> None:
    text = record.scrub_recording({"body": "thanks me-login for this"}, logins=["me-login"])
    assert "me-login" not in text


def test_the_cli_writes_a_clean_file_and_refuses_a_dirty_destination(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "recordings"
    monkeypatch.setattr(record, "RECORDINGS", root)
    source = tmp_path / "raw.json"
    source.write_text(json.dumps(raw()), encoding="utf-8")
    ok = record.main([str(source), "--dest", str(root / "a.json"), "--login", "me-login"])
    assert ok == 0 and (root / "a.json").exists()
    bad = record.main([str(source), "--dest", str(tmp_path / "a.json"), "--login", "me-login"])
    assert bad == 1 and "refused" in capsys.readouterr().err
