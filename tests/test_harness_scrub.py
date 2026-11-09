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


@pytest.mark.parametrize(
    "leak",
    [
        {"repository": {"nameWithOwner": "org-sample/tools"}},
        {"headRepository": {"nameWithOwner": "jdoe-real/widgets"}},
        {"url": "https://github.com/org-sample/tools/pull/3"},
        {"html_url": "https://github.com/jdoe-real"},
        {"html_url": "https://github.com/orgs/org-sample/teams/x"},
    ],
)
def test_the_check_flags_a_github_owner_that_is_not_a_placeholder(leak: dict[str, object]) -> None:
    assert scrub.find_unscrubbed(json.dumps(leak)), leak


@pytest.mark.parametrize(
    "fine",
    [
        {"repository": {"nameWithOwner": "user-7/tools"}},
        {"url": "https://github.com/user-7/tools/pull/3"},
        {"url": "https://github.com/apps/some-app"},
        {"url": "https://api.github.com/repos/user-1/r"},
        {"url": "https://ghe.example.test/whatever/r/pull/1"},
    ],
)
def test_a_placeholder_owner_or_an_unchecked_url_passes(fine: dict[str, object]) -> None:
    assert scrub.find_unscrubbed(json.dumps(fine)) == [], fine


def test_a_graphql_owner_is_scrubbed_in_name_with_owner_and_in_the_url() -> None:
    doc = {
        "repository": {"nameWithOwner": "org-sample/tools"},
        "url": "https://github.com/org-sample/tools/pull/3",
    }
    out = scrub.Scrubber().scrub(doc)
    assert "org-sample" not in json.dumps(out)
    assert out["repository"]["nameWithOwner"].endswith("/tools")
    assert scrub.find_unscrubbed(json.dumps(out)) == []


def test_web_flow_committer_recording_keeps_html_url_and_url_hosts() -> None:
    doc = {
        "commit": {
            "committer": {"name": "GitHub", "email": "noreply" + AT + "github.com"},
            "author": {"name": "Ann Other", "email": "ann" + AT + "corp.example.org"},
            "message": "Merge pull request #5",
        },
        "html_url": "https://github.com/user-1/r/commit/abc",
        "url": "https://api.github.com/repos/user-1/r",
    }
    out = scrub.Scrubber().scrub(doc)
    assert out["commit"]["committer"]["name"] == "GitHub"  # a forge service, kept
    assert out["commit"]["author"]["name"].startswith("User ")
    assert out["html_url"] == doc["html_url"]
    assert out["url"] == doc["url"]
    assert scrub.find_unscrubbed(json.dumps(out)) == []
    assert scrub.Scrubber().scrub(out) == out


@pytest.mark.parametrize("service", ["GitHub", "GitLab", "Ghost"])
def test_forge_service_display_names_are_kept(service: str) -> None:
    doc = {"user": {"name": service, "login": "user-1"}, "body": f"Posted via {service}"}
    out = scrub.Scrubber().scrub(doc)
    assert out == doc
    assert scrub.find_unscrubbed(json.dumps(out)) == []


@pytest.mark.parametrize(
    ("name", "path", "url"),
    [
        ("Docs", "docs", "https://gitlab.example.test/docs/handbook"),
        ("gitlab", "gitlab-org", "https://gitlab.com/gitlab-org/x"),
    ],
)
def test_group_namespace_web_url_path_uses_the_namespace_path_placeholder(
    name: str, path: str, url: str
) -> None:
    doc = {
        "namespace": {"name": name, "path": path, "kind": "group"},
        "web_url": url,
        "description": f"{name} handbook",
    }
    out = scrub.Scrubber().scrub(doc)
    placeholder = out["namespace"]["path"]
    assert scrub.PLACEHOLDER_LOGIN.fullmatch(placeholder)
    assert out["web_url"] == url.replace(f"/{path}/", f"/{placeholder}/")
    assert out["namespace"]["name"] == placeholder  # a group is not a person
    assert "User " not in json.dumps(out)
    assert scrub.find_unscrubbed(json.dumps(out)) == []
    assert scrub.Scrubber().scrub(out) == out


def test_the_check_flags_an_unscrubbed_group_namespace_name() -> None:
    leak = {"namespace": {"name": "Some Group", "path": "user-1", "kind": "group"}}
    assert scrub.find_unscrubbed(json.dumps(leak))


def test_display_names_are_not_rewritten_inside_urls_or_host_names() -> None:
    doc = {
        "user": {"name": "Docs", "email": "d" + AT + "corp.example.org"},
        "html_url": "https://forge.example.test/docs/handbook",
        "body": "Docs wrote docs.example.test/x and /docs/y and docs-team",
    }
    out = scrub.Scrubber().scrub(doc)
    placeholder = out["user"]["name"]
    assert out["html_url"] == doc["html_url"]
    assert out["body"] == f"{placeholder} wrote docs.example.test/x and /docs/y and docs-team"


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
