"""Scrub account names, emails and secrets from recorded host responses, and check files for leaks.

A recording enters the repo only after ``Scrubber.scrub``. ``check_tree`` (run over ``tests/`` by
``test_harness_scrub.py``) fails on any file that still holds an email, a person's login or
display name in an identity field, a mention of a real account, a ``Name <email>`` pair, or a
token-shaped secret.

Usage: ``python -m tests.harness.scrub RECORDING.json [--extra NAME ...] > clean.json``. Owners
and namespaces are collected from the recording (``owner.login``, ``namespace``, the leading
segments of ``full_name`` and ``path_with_namespace``); pass repository names as ``--extra``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGIN_KEYS = {"login", "username", "user_name"}
EMAIL_KEYS = {"email", "author_email", "committer_email", "public_email", "commit_email"}
NAME_KEYS = {"author_name", "committer_name", "display_name"}
AVATAR_KEYS = {"avatar_url", "gravatar_id"}
# ``name`` is a person's name in an object that also identifies an account, or under one of these.
NAME_IF_SIBLING = {"login", "username", "email", "avatar_url"}
PERSON_PARENTS = {
    "author",
    "committer",
    "user",
    "owner",
    "assignee",
    "assignees",
    "reviewer",
    "reviewers",
    "requested_reviewers",
    "merged_by",
    "closed_by",
    "merge_user",
}
NAMESPACE_KEY = "namespace"  # GitLab: name, path and full_path name a user or a group
# leading segments are owners (``nameWithOwner``: GitHub GraphQL's ``owner/name``)
OWNER_PATH_KEYS = {"full_name", "path_with_namespace", "full_path", "nameWithOwner"}
# A github.com URL in a key ending in ``url`` (any case: ``url``, ``html_url``, ``detailsUrl``),
# and GraphQL's ``resourcePath`` (the URL's path alone), names its owner in the first path
# segment (``/owner/repo/...``, ``/login``), except under GitHub's own routes; ``/orgs/<owner>``
# and ``/users/<login>`` name it second. Other hosts' URL layouts are not known well enough to
# check.
OWNER_URL_SUFFIX = "url"
OWNER_PATH_KEY = "resourcePath"
GITHUB_ROUTES = {"apps", "settings", "marketplace", "sponsors", "features", "enterprises"}
GITHUB_OWNER_ROUTES = {"orgs", "users"}
REDACT_KEYS = {"authorization", "private-token", "cookie", "set-cookie"}
REDACTED = "REDACTED"
SERVICE_LOGINS = {"github", "ghost"}
SERVICE_NAMES = {"github", "gitlab", "ghost"}  # forge display names, not people

PLACEHOLDER_LOGIN = re.compile(r"(app/)?user-\d+(\[bot\])?")
PLACEHOLDER_EMAIL = re.compile(r"user-\d+@example\.test")
PLACEHOLDER_NAME = re.compile(r"User \d+")
PLACEHOLDER_AVATAR = re.compile(r"https://avatars\.example\.test/user-\d+")

# Not ``git@<host>:`` (an ssh remote). Unicode word characters count in the local part.
EMAIL = re.compile(r"(?<![\w.%+-])(?!git@[\w.-]+:)[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
EMAIL_PCT = re.compile(r"(?<![\w.+-])[\w.+-]+%40[\w-]+(?:\.[\w-]+)+")
# Not an email domain (``@example.test``); in code only after whitespace (prose, not literals).
MENTION = re.compile(r"(?<![\w.@/-])@([A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?)(?![\w-]|\.\w)")
CODE_MENTION = re.compile(r"(?:^|(?<=\s))" + MENTION.pattern)
NAME_EMAIL = re.compile(
    r"(?P<name>[^\W\d_][\w.'’-]*(?:[ \t]+[^\W\d_][\w.'’-]*){0,3})"
    r"[ \t]+<(?P<email>[^<>\s@]+@[^<>\s]+)>"
)
SECRET = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|gl(?:pat|ptt|dt|rt|cbt|oas|soat|imt|ft|agent)-[\w-]{8,}"
    r"|(?<![\w-])[Bb]earer[ \t]+[A-Za-z0-9._~+/-]{16,}=*)"
)
HEADER_LINE = re.compile(
    r"(?im)^(?P<lead>[ \t]*(?:authorization|private-token|cookie|set-cookie)[ \t]*:[ \t]*)"
    r"(?P<value>\S.*)$"
)
# A string that is a URL as a whole: display names are never substituted in it.
URL_VALUE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
SAFE_EMAIL_DOMAINS = ("example.test", "example.com", "example.org", "example.net")

DATA_SUFFIXES = {".json", ".jsonl", ".txt", ".md", ".yaml", ".yml", ".http", ".har", ".log", ".csv"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".gz", ".zip", ".whl", ".pyc"}
SKIPPED_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}


def _is_safe_domain(domain: str) -> bool:
    domain = domain.lower()
    return domain.endswith((".test", ".invalid")) or domain in SAFE_EMAIL_DOMAINS


def _split_login(login: str) -> tuple[str, str, str]:
    prefix = "app/" if login.startswith("app/") else ""
    suffix = "[bot]" if login.endswith("[bot]") else ""
    return prefix, login[len(prefix) : len(login) - len(suffix)], suffix


def is_service_login(login: str) -> bool:
    """Forge service accounts, kept as they are: ``github``, ``ghost``, ``*[bot]``, ``*-bot``."""
    lowered = login.lower()
    return lowered in SERVICE_LOGINS or lowered.endswith(("[bot]", "-bot"))


def is_service_name(name: str) -> bool:
    """Forge service display names, kept as they are: ``GitHub``, ``GitLab``, ``Ghost``."""
    return name.lower() in SERVICE_NAMES


def _name_ok(value: str) -> bool:
    return bool(PLACEHOLDER_NAME.fullmatch(value)) or is_service_name(value)


def _is_group(node: dict[str, Any]) -> bool:
    return str(node.get("kind", "")).lower() == "group"


def _login_ok(value: str) -> bool:
    return bool(PLACEHOLDER_LOGIN.fullmatch(value)) or is_service_login(value)


def github_url_owner(value: str) -> str | None:
    """The owner segment of a ``https://github.com/...`` URL, or None when it names none."""
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() != "github.com":
        return None
    return github_path_owner(parsed.path)


def github_path_owner(path: str) -> str | None:
    """The owner segment of a github.com URL path (``/owner/repo/...``), or None."""
    segments = [s for s in urllib.parse.urlsplit(path).path.split("/") if s]
    if not segments or segments[0].lower() in GITHUB_ROUTES:
        return None
    if segments[0].lower() in GITHUB_OWNER_ROUTES:
        return segments[1] if len(segments) > 1 else None
    return segments[0]


def _never_rewrite(key: str) -> bool:
    """Refs and shas are kept verbatim: a short login must not rewrite ``ref: main``."""
    key = key.lower()
    return key in {"ref", "sha", "oid", "branch"} or key.endswith(
        ("_ref", "_sha", "_oid", "_branch", "refname", "refoid", "ref_name")
    )


def _person_name(value: str) -> str:
    """The name in a ``Name <email>`` match, without leading lower-case words (``thanks``)."""
    words = value.split()
    while len(words) > 1 and words[0][:1].islower():
        words.pop(0)
    return " ".join(words)


class Scrubber:
    """Replace identities with stable placeholders (``user-N``), consistently across a document.

    Identity fields are replaced by key; free text is rewritten on word boundaries, never inside
    a host name, and refs and shas are never rewritten. Running it twice changes nothing more.
    """

    def __init__(self, extra: list[str] | None = None) -> None:
        self._logins: dict[str, str] = {}  # lower base login -> user-N
        self._emails: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._avatars: dict[str, str] = {}
        self._count = 0
        for name in extra or []:
            self._login(name)

    def _next(self) -> int:
        self._count += 1
        return self._count

    def _login(self, base: str) -> str:
        key = base.lower()
        if key not in self._logins:
            self._logins[key] = f"user-{self._next()}"
        return self._logins[key]

    def _alias_login(self, full: str) -> str:
        if _login_ok(full):
            return full
        prefix, base, suffix = _split_login(full)
        return f"{prefix}{self._login(base)}{suffix}"

    def _email(self, address: str) -> str:
        if PLACEHOLDER_EMAIL.fullmatch(address) or address.lower().endswith("@example.test"):
            return address
        key = address.lower()
        if key not in self._emails:
            self._emails[key] = f"user-{self._next()}@example.test"
        return self._emails[key]

    def _name(self, name: str) -> str:
        if _name_ok(name):
            return name
        key = name.lower()
        if key not in self._names:
            self._names[key] = f"User {self._next()}"
        return self._names[key]

    def _avatar(self, url: str) -> str:
        if PLACEHOLDER_AVATAR.fullmatch(url):
            return url
        if url not in self._avatars:
            self._avatars[url] = f"https://avatars.example.test/user-{self._next()}"
        return self._avatars[url]

    def _owner_path(self, key: str, value: str, in_namespace: bool) -> str:
        """Map the owner segments of a path; in a namespace every segment is an owner."""
        segments = value.split("/")
        owners = segments if in_namespace or len(segments) == 1 else segments[:-1]
        mapped = [self._alias_login(s) if s else s for s in owners]
        return "/".join(mapped + segments[len(owners) :])

    def _group_name(self, value: str, node: dict[str, Any]) -> str:
        """A group namespace's name is an owner, not a person: it takes its path's placeholder."""
        path = node.get("path") or str(node.get("full_path") or "").rsplit("/", 1)[-1] or value
        return self._alias_login(str(path))

    # traversal ---------------------------------------------------------------------------

    def _by_key(self, key: str, value: str, node: dict[str, Any], parent: str | None) -> str | None:
        """The replacement for an identity field, or None if ``key`` is not one here."""
        lowered = key.lower()
        if lowered in REDACT_KEYS:
            return REDACTED
        in_namespace = parent == NAMESPACE_KEY
        if in_namespace and key == "name" and _is_group(node):
            return self._group_name(value, node)
        if key in LOGIN_KEYS:
            return self._alias_login(value)
        if key in EMAIL_KEYS:
            return self._email(value)
        person = bool(NAME_IF_SIBLING & node.keys()) or parent in PERSON_PARENTS
        if key in NAME_KEYS or (key == "name" and (person or in_namespace)):
            return self._name(value)
        if key in AVATAR_KEYS:
            return self._avatar(value)
        if in_namespace and key in {"path", "full_path"}:
            return self._owner_path(key, value, True)
        if key in OWNER_PATH_KEYS:
            return self._owner_path(key, value, False)
        return None

    def _walk(
        self,
        node: Any,
        on_field: Callable[[str, str, dict[str, Any], str | None], str | None],
        on_text: Callable[[str], str],
        parent: str | None = None,
    ) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, value in node.items():
                if isinstance(value, str) and value:
                    replaced = on_field(key, value, node, parent)
                    if replaced is not None:
                        out[key] = replaced
                    elif _never_rewrite(key):
                        out[key] = value
                    else:
                        out[key] = on_text(value)
                else:
                    out[key] = self._walk(value, on_field, on_text, key)
            return out
        if isinstance(node, list):
            return [self._walk(item, on_field, on_text, parent) for item in node]
        if isinstance(node, str):
            return on_text(node)
        return node

    def _collect_text(self, text: str) -> str:
        for match in NAME_EMAIL.finditer(text):
            self._name(_person_name(match.group("name")))
            self._email(match.group("email"))
        for match in MENTION.finditer(text):
            if not _login_ok(match.group(1)):
                self._alias_login(match.group(1))
        return text

    # free text ---------------------------------------------------------------------------

    def _text(self, text: str) -> str:
        if PLACEHOLDER_AVATAR.fullmatch(text):
            return text
        if text in self._avatars:
            return self._avatars[text]
        text = HEADER_LINE.sub(lambda m: m.group("lead") + REDACTED, text)
        text = SECRET.sub(REDACTED, text)

        def name_email(match: re.Match[str]) -> str:
            whole = match.group("name")
            person = _person_name(whole)
            lead = whole[: len(whole) - len(person)]
            return f"{lead}{self._name(person)} <{self._email(match.group('email'))}>"

        text = NAME_EMAIL.sub(name_email, text)
        text = EMAIL.sub(lambda m: self._email(m.group(0)), text)

        def pct(match: re.Match[str]) -> str:
            address = urllib.parse.unquote(match.group(0))
            return self._email(address).replace("@", "%40")

        text = EMAIL_PCT.sub(pct, text)
        # Not inside a word, an email, a host or a URL path; URL paths take login placeholders.
        names = [] if URL_VALUE.fullmatch(text) else sorted(self._names, key=len, reverse=True)
        for name in names:
            pattern = rf"(?<![\w.@/-]){re.escape(name)}(?![\w-]|\.\w)"
            text = re.sub(pattern, self._names[name], text, flags=re.IGNORECASE)
        for base in sorted(self._logins, key=len, reverse=True):
            if len(base) < 2:
                continue
            # Not inside a word, an email, or a host name (``github`` in ``github.com``).
            pattern = rf"(?<![\w.@-]){re.escape(base)}(?![\w-]|\.\w)"
            text = re.sub(pattern, self._logins[base], text, flags=re.IGNORECASE)

        def mention(match: re.Match[str]) -> str:
            name = match.group(1)
            return match.group(0) if _login_ok(name) else "@" + self._alias_login(name)

        return MENTION.sub(mention, text)

    def scrub(self, document: Any) -> Any:
        """Return a scrubbed copy of a parsed JSON document."""
        self._walk(document, self._by_key, self._collect_text)
        return self._walk(document, self._by_key, self._text)

    def scrub_text(self, text: str) -> str:
        """Scrub a non-JSON text (secrets, emails, mentions, plus identities seen so far)."""
        self._collect_text(text)
        return self._text(text)


@dataclass(frozen=True)
class Finding:
    path: str
    kind: str  # email | identity-field | mention | name-email | secret | undecodable
    value: str


def _identity_findings(node: Any, path: str, out: list[Finding], parent: str | None = None) -> None:
    if isinstance(node, dict):
        person = bool(NAME_IF_SIBLING & node.keys()) or parent in PERSON_PARENTS
        in_namespace = parent == NAMESPACE_KEY
        for key, value in node.items():
            if isinstance(value, str) and value:
                bad = (
                    (key.lower() in REDACT_KEYS and value != REDACTED)
                    or (key in LOGIN_KEYS and not _login_ok(value))
                    or (key in EMAIL_KEYS and not PLACEHOLDER_EMAIL.fullmatch(value))
                    or (in_namespace and key == "name" and _is_group(node) and not _login_ok(value))
                    or (
                        (key in NAME_KEYS or (key == "name" and (person or in_namespace)))
                        and not (in_namespace and _is_group(node))
                        and not _name_ok(value)
                    )
                    or (key in AVATAR_KEYS and not PLACEHOLDER_AVATAR.fullmatch(value))
                    or (
                        in_namespace
                        and key in {"path", "full_path"}
                        and not all(_login_ok(s) for s in value.split("/"))
                    )
                    or (
                        not in_namespace
                        and key in OWNER_PATH_KEYS
                        and "/" in value
                        and not all(_login_ok(s) for s in value.split("/")[:-1])
                    )
                    or (
                        key.lower().endswith(OWNER_URL_SUFFIX)
                        and (owner := github_url_owner(value)) is not None
                        and not _login_ok(owner)
                    )
                    or (
                        key == OWNER_PATH_KEY
                        and (owner := github_path_owner(value)) is not None
                        and not _login_ok(owner)
                    )
                )
                if bad:
                    shown = REDACTED if key.lower() in REDACT_KEYS else value
                    out.append(Finding(path, "identity-field", f"{key}={shown}"))
            else:
                _identity_findings(value, path, out, key)
    elif isinstance(node, list):
        for item in node:
            _identity_findings(item, path, out, parent)


def _text_findings(text: str, path: str, *, code: bool) -> list[Finding]:
    findings: list[Finding] = []
    for variant in dict.fromkeys((text, urllib.parse.unquote(text))):
        for match in EMAIL.finditer(variant):
            if not _is_safe_domain(match.group(0).rsplit("@", 1)[1]):
                findings.append(Finding(path, "email", match.group(0)))
        for match in NAME_EMAIL.finditer(variant):
            name = _person_name(match.group("name"))
            if not _name_ok(name):
                findings.append(Finding(path, "name-email", match.group(0)))
    mention = CODE_MENTION if code else MENTION
    for line in text.splitlines():
        if code and line.lstrip().startswith("@"):
            continue  # a decorator, not a mention
        for match in mention.finditer(line):
            if not _login_ok(match.group(1)):
                findings.append(Finding(path, "mention", match.group(0)))
    for match in SECRET.finditer(text):
        findings.append(Finding(path, "secret", match.group(0)[:8] + "..."))
    for match in HEADER_LINE.finditer(text):
        if match.group("value").strip() != REDACTED:
            findings.append(Finding(path, "secret", match.group("lead").strip() + " ..."))
    return list(dict.fromkeys(findings))


def find_unscrubbed(text: str, path: str = "<text>", *, code: bool = False) -> list[Finding]:
    """Leaks in ``text``: emails off the safe domains, ``Name <email>`` pairs, mentions, secrets,
    and, unless ``code``, identity fields of any JSON or JSON-lines content."""
    findings = _text_findings(text, path, code=code)
    if code:
        return findings
    documents: list[Any] = []
    try:
        documents.append(json.loads(text))
    except ValueError:
        for line in text.splitlines():
            try:
                documents.append(json.loads(line))
            except ValueError:
                continue
    for document in documents:
        _identity_findings(document, path, findings)
        for string in _strings(document):
            findings.extend(
                f for f in _text_findings(string, path, code=False) if f not in findings
            )
    return findings


def _strings(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return [node] if isinstance(node, str) else []


def check_tree(root: Path) -> list[Finding]:
    """Findings in every file under ``root``: data files (and extensionless ones) get every
    check, other text files the email, mention and secret checks; caches are skipped, and a
    file that is not UTF-8 is itself a finding unless its suffix marks it binary."""
    findings: list[Finding] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in SKIPPED_DIRS for part in path.parts):
            continue
        suffix = path.suffix.lower()
        if suffix in BINARY_SUFFIXES:
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            findings.append(Finding(str(path), "undecodable", "not UTF-8"))
            continue
        data = suffix in DATA_SUFFIXES or suffix == ""
        findings.extend(find_unscrubbed(text, str(path), code=not data))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrub a recorded host response (JSON).")
    parser.add_argument("recording", type=Path)
    parser.add_argument("--extra", action="append", default=[], help="repository name to hide")
    args = parser.parse_args(argv)
    document = json.loads(args.recording.read_text(encoding="utf-8"))
    clean = Scrubber(extra=args.extra).scrub(document)
    sys.stdout.write(json.dumps(clean, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
