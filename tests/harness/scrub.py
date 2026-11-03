"""Scrub account names and emails from recorded host responses, and check data files for leaks.

A recording enters the repo only after ``Scrubber.scrub``. ``check_tree`` (run over ``tests/`` by
``test_harness_scrub.py``) fails on any data file that still holds an email, a person's login or
display name in an identity field, an ``@mention`` of a real account, or a token-shaped secret.

Usage: ``python -m tests.harness.scrub RECORDING.json [--extra NAME ...] > clean.json``. Pass the
owner and repository names of the recorded project as ``--extra`` so they are replaced too.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGIN_KEYS = {"login", "username", "user_name"}
EMAIL_KEYS = {"email", "author_email", "committer_email", "public_email"}
NAME_KEYS = {"author_name", "committer_name", "display_name"}
AVATAR_KEYS = {"avatar_url", "gravatar_id"}
# ``name`` is a person's name only in an object that also identifies an account.
NAME_IF_SIBLING = {"login", "username", "email", "avatar_url"}

PLACEHOLDER_LOGIN = re.compile(r"^(app/)?user-\d+(\[bot\])?$")
PLACEHOLDER_EMAIL = re.compile(r"^user-\d+@example\.test$")
PLACEHOLDER_NAME = re.compile(r"^User \d+$")
PLACEHOLDER_AVATAR = re.compile(r"^https://avatars\.example\.test/user-\d+$")

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
MENTION = re.compile(r"(?<![\w.@/-])@([A-Za-z0-9][A-Za-z0-9_-]{0,38})")
SECRET = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[\w-]{10,})"
)
SAFE_EMAIL_DOMAINS = ("example.test", "example.com", "example.org", "example.net")

DATA_SUFFIXES = {".json", ".jsonl", ".txt", ".md", ".yaml", ".yml", ".http", ".har", ".log", ".csv"}


def _is_safe_domain(domain: str) -> bool:
    domain = domain.lower()
    return domain.endswith((".test", ".invalid")) or domain in SAFE_EMAIL_DOMAINS


def _split_login(login: str) -> tuple[str, str, str]:
    prefix = "app/" if login.startswith("app/") else ""
    suffix = "[bot]" if login.endswith("[bot]") else ""
    return prefix, login[len(prefix) : len(login) - len(suffix)], suffix


class Scrubber:
    """Replace identities with stable placeholders (``user-N``), consistently across a document."""

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
        prefix, base, suffix = _split_login(full)
        return f"{prefix}{self._login(base)}{suffix}"

    def _email(self, address: str) -> str:
        key = address.lower()
        if key not in self._emails:
            self._emails[key] = f"user-{self._next()}@example.test"
        return self._emails[key]

    def _name(self, name: str) -> str:
        key = name.lower()
        if key not in self._names:
            self._names[key] = f"User {self._next()}"
        return self._names[key]

    def _avatar(self, url: str) -> str:
        if url not in self._avatars:
            self._avatars[url] = f"https://avatars.example.test/user-{self._next()}"
        return self._avatars[url]

    def _collect(self, node: Any) -> None:
        if isinstance(node, dict):
            person = bool(NAME_IF_SIBLING & node.keys())
            for key, value in node.items():
                if isinstance(value, str) and value:
                    if key in LOGIN_KEYS and not PLACEHOLDER_LOGIN.match(value):
                        self._alias_login(value)
                    elif key in EMAIL_KEYS and not PLACEHOLDER_EMAIL.match(value):
                        self._email(value)
                    elif (key in NAME_KEYS or (key == "name" and person)) and not (
                        PLACEHOLDER_NAME.match(value)
                    ):
                        self._name(value)
                    elif key in AVATAR_KEYS and not PLACEHOLDER_AVATAR.match(value):
                        self._avatar(value)
                self._collect(value)
        elif isinstance(node, list):
            for item in node:
                self._collect(item)

    def _text(self, text: str) -> str:
        if PLACEHOLDER_AVATAR.match(text):
            return text
        if text in self._avatars:
            return self._avatars[text]

        def email(match: re.Match[str]) -> str:
            address = match.group(0)
            if PLACEHOLDER_EMAIL.match(address) or address.lower().endswith("@example.test"):
                return address
            return self._email(address)

        text = EMAIL.sub(email, text)
        for name in sorted(self._names, key=len, reverse=True):
            text = re.sub(re.escape(name), self._names[name], text, flags=re.IGNORECASE)
        for base in sorted(self._logins, key=len, reverse=True):
            if len(base) < 2:
                continue
            pattern = rf"(?<![\w.@-]){re.escape(base)}(?![\w-])"
            text = re.sub(pattern, self._logins[base], text, flags=re.IGNORECASE)

        def mention(match: re.Match[str]) -> str:
            name = match.group(1)
            if PLACEHOLDER_LOGIN.match(name):
                return match.group(0)
            return "@" + self._login(name)

        return MENTION.sub(mention, text)

    def _apply(self, node: Any) -> Any:
        if isinstance(node, dict):
            return {key: self._apply(value) for key, value in node.items()}
        if isinstance(node, list):
            return [self._apply(item) for item in node]
        if isinstance(node, str):
            return self._text(node)
        return node

    def scrub(self, document: Any) -> Any:
        """Return a scrubbed copy of a parsed JSON document."""
        self._collect(document)
        return self._apply(document)

    def scrub_text(self, text: str) -> str:
        """Scrub a non-JSON text (emails and mentions, plus identities seen so far)."""
        return self._text(text)


@dataclass(frozen=True)
class Finding:
    path: str
    kind: str  # email | identity-field | mention | secret
    value: str


def _identity_findings(node: Any, path: str, out: list[Finding]) -> None:
    if isinstance(node, dict):
        person = bool(NAME_IF_SIBLING & node.keys())
        for key, value in node.items():
            if isinstance(value, str) and value:
                bad = (
                    (key in LOGIN_KEYS and not PLACEHOLDER_LOGIN.match(value))
                    or (key in EMAIL_KEYS and not PLACEHOLDER_EMAIL.match(value))
                    or (
                        (key in NAME_KEYS or (key == "name" and person))
                        and not PLACEHOLDER_NAME.match(value)
                    )
                    or (key in AVATAR_KEYS and not PLACEHOLDER_AVATAR.match(value))
                )
                if bad:
                    out.append(Finding(path, "identity-field", f"{key}={value}"))
            _identity_findings(value, path, out)
    elif isinstance(node, list):
        for item in node:
            _identity_findings(item, path, out)


def find_unscrubbed(text: str, path: str = "<text>") -> list[Finding]:
    """Leaks in ``text``: emails off the safe domains, identity fields, mentions, secrets."""
    findings: list[Finding] = []
    for match in EMAIL.finditer(text):
        if not _is_safe_domain(match.group(0).rsplit("@", 1)[1]):
            findings.append(Finding(path, "email", match.group(0)))
    for match in MENTION.finditer(text):
        if not PLACEHOLDER_LOGIN.match(match.group(1)):
            findings.append(Finding(path, "mention", match.group(0)))
    for match in SECRET.finditer(text):
        findings.append(Finding(path, "secret", match.group(0)[:8] + "..."))
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
    return findings


def check_tree(root: Path) -> list[Finding]:
    """Findings in every data file under ``root`` (caches and bytecode are skipped)."""
    findings: list[Finding] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in DATA_SUFFIXES:
            continue
        if any(part in {"__pycache__", ".pytest_cache", ".ruff_cache"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        findings.extend(find_unscrubbed(text, str(path)))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrub a recorded host response (JSON).")
    parser.add_argument("recording", type=Path)
    parser.add_argument("--extra", action="append", default=[], help="owner or repo name to hide")
    args = parser.parse_args(argv)
    document = json.loads(args.recording.read_text(encoding="utf-8"))
    clean = Scrubber(extra=args.extra).scrub(document)
    sys.stdout.write(json.dumps(clean, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
