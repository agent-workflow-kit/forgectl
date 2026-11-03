"""The recording scrubber and the check that no unscrubbed file sits under ``tests/``.

Leaky sample values are assembled at run time, so this file itself passes the tree check.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .harness import scrub

TESTS_DIR = Path(__file__).resolve().parent
AT = "@"
LEAK_EMAIL = "jane.doe" + AT + "corp.example.org"  # a subdomain: not on the safe list

RECORDING = {
    "number": 7,
    "user": {"login": "jdoe-real", "name": "Jane Doe", "email": LEAK_EMAIL},
    "author": {"login": "dependabot[bot]"},
    "body": f"thanks {AT}jdoe-real and {AT}other-person, cc {LEAK_EMAIL}",
    "html_url": "https://host.test/jdoe-real/widgets/pull/7",
    "commit": {
        "author": {"name": "Jane Doe", "email": "jd" + AT + "example.net", "date": "2026-01-01"}
    },
    "check": {"name": "build"},
}


def test_recording_with_a_name_and_an_email_comes_out_clean() -> None:
    out = scrub.Scrubber(extra=["widgets"]).scrub(RECORDING)
    text = json.dumps(out)
    for leaked in ("jdoe-real", "Jane Doe", "jane.doe", "corp.example.org", "other-person"):
        assert leaked not in text
    assert "jd" + AT + "example.net" not in text
    assert scrub.find_unscrubbed(text) == []


def test_scrubbing_is_consistent_and_keeps_shape() -> None:
    out = scrub.Scrubber().scrub(RECORDING)
    assert out["user"]["login"] == out["body"].split()[1].lstrip("@")
    assert out["author"]["login"] == "dependabot[bot]"  # a service account is kept
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


# GitLab namespaces, name-only people, owners and trailers.

GITLAB = {
    "path_with_namespace": "jdoe-real/widgets",
    "namespace": {
        "name": "Jane Doe",
        "path": "jdoe-real",
        "kind": "user",
        "full_path": "jdoe-real",
    },
    "author": {"name": "Ann Other"},
    "assignees": [{"name": "Review Person"}],
    "owner": {"login": "org-sample"},
    "full_name": "org-sample/tools",
    "message": "fix\n\nCo-authored-by: Trailer Person <trailer" + AT + "corp.example.org>",
    "description": "Signed-off-by: Signer Person <user-9" + AT + "example.test>",
}


@pytest.mark.parametrize(
    "leak",
    [
        {"namespace": {"name": "Jane Doe", "path": "jdoe-real", "kind": "user"}},
        {"namespace": {"full_path": "some-group/sub"}},
        {"path_with_namespace": "some-group/widgets"},
        {"full_name": "org-sample/tools"},
        {"author": {"name": "Ann Other"}},
        {"reviewers": [{"name": "Review Person"}]},
        {"text": "Co-authored-by: Trailer Person <user-9" + AT + "example.test>"},
        {"headers": {"Authorization": "token abc"}},
    ],
)
def test_the_check_flags_identity_shapes(leak: dict[str, object]) -> None:
    assert scrub.find_unscrubbed(json.dumps(leak)), leak


def test_gitlab_shapes_are_scrubbed() -> None:
    out = scrub.Scrubber().scrub(GITLAB)
    text = json.dumps(out)
    for leaked in (
        "jdoe-real",
        "Jane Doe",
        "Ann Other",
        "Review Person",
        "org-sample",
        "Trailer Person",
        "Signer Person",
    ):
        assert leaked not in text, leaked
    assert out["path_with_namespace"].endswith("/widgets")  # repository names need --extra
    assert scrub.find_unscrubbed(text) == []
    assert scrub.Scrubber().scrub(out) == out


def test_short_names_and_logins_match_on_word_boundaries_only() -> None:
    doc = {
        "user": {"login": "main", "name": "Ann"},
        "owner": {"login": "github"},
        "ref": "main",
        "head": {"ref": "main", "sha": "a" * 40},
        "body": "Annotated by Ann on the main branch: see https://github.com/x and the domain",
    }
    out = scrub.Scrubber().scrub(doc)
    assert out["ref"] == "main" and out["head"]["ref"] == "main"
    assert out["owner"]["login"] == "github"  # a service account
    body = out["body"]
    assert body.startswith("Annotated by User ") and "https://github.com/x" in body
    assert "the domain" in body
    assert scrub.Scrubber().scrub(out) == out


def test_secrets_and_credential_headers_are_redacted() -> None:
    secret = "glpat-" + "x" * 12
    doc = {
        "headers": {"Authorization": "token abc", "Private-Token": "abc", "Set-Cookie": "s=1"},
        "log": f"Authorization: Bearer {'y' * 20}\nok {secret}",
    }
    out = scrub.Scrubber().scrub(doc)
    assert set(out["headers"].values()) == {scrub.REDACTED}
    assert secret not in out["log"] and "y" * 20 not in out["log"]
    assert scrub.find_unscrubbed(json.dumps(out)) == []


@pytest.mark.parametrize(
    "secret",
    [
        prefix + "-" + "a" * 12
        for prefix in (
            "glptt",
            "gldt",
            "glrt",
            "glcbt",
            "gloas",
            "glsoat",
            "glimt",
            "glft",
            "glagent",
        )
    ]
    + ["glpat-" + "a" * 8, "token_ghp_" + "b" * 30, "Bearer " + "c" * 24],
)
def test_the_secret_pattern_covers_every_token_family(secret: str) -> None:
    assert [f.kind for f in scrub.find_unscrubbed(f"x {secret} y")] == ["secret"]


def test_an_ssh_remote_is_not_an_email() -> None:
    remote = "git" + AT + "github.com:o/r.git"
    assert scrub.find_unscrubbed(f"remote {remote}") == []
    assert scrub.Scrubber().scrub_text(f"remote {remote}") == f"remote {remote}"


@pytest.mark.parametrize(
    "text", ["mailto:jane%" + "40corp.example.org", "contact jos\u00e9" + AT + "corp.example.org"]
)
def test_encoded_and_non_ascii_emails_are_caught_and_scrubbed(text: str) -> None:
    assert [f.kind for f in scrub.find_unscrubbed(text)] == ["email"]
    clean = scrub.Scrubber().scrub_text(text)
    assert "corp.example.org" not in clean and scrub.find_unscrubbed(clean) == []


# The tree check.


def test_the_check_fails_on_an_unscrubbed_file_in_a_tree(tmp_path: Path) -> None:
    (tmp_path / "rec.json").write_text(json.dumps(RECORDING), encoding="utf-8")
    assert scrub.check_tree(tmp_path)


def test_the_check_passes_on_a_scrubbed_tree(tmp_path: Path) -> None:
    clean = scrub.Scrubber().scrub(RECORDING)
    (tmp_path / "rec.json").write_text(json.dumps(clean), encoding="utf-8")
    assert scrub.check_tree(tmp_path) == []


@pytest.mark.parametrize("name", ["rec.JSON", "notes", "change.patch", "x.diff", "probe.py"])
def test_the_check_scans_every_text_file(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text(f"cc {LEAK_EMAIL}\n", encoding="utf-8")
    assert [f.kind for f in scrub.check_tree(tmp_path)] == ["email"]


def test_the_check_reports_an_undecodable_file_unless_binary(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe")
    (tmp_path / "pic.png").write_bytes(b"\xff\xfe")
    assert [(Path(f.path).name, f.kind) for f in scrub.check_tree(tmp_path)] == [
        ("blob.bin", "undecodable")
    ]


def test_python_decorators_are_not_mentions(tmp_path: Path) -> None:
    (tmp_path / "t.py").write_text("@pytest.fixture\ndef f():\n    pass\n", encoding="utf-8")
    assert scrub.check_tree(tmp_path) == []


def test_no_file_under_tests_holds_a_real_name_or_email() -> None:
    assert scrub.check_tree(TESTS_DIR) == []
