"""Schema registry over ``schemas/``, keyed by ``$id``, and the typed validators built on it.

Every ``--json`` output, hook payload, log record and audit record a test produces goes through
one of these functions, so a shape the schemas do not allow fails the test.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"

HOOKS = ("pre-merge", "post-merge", "pre-post", "post-post")
RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)


class UnknownSchemaError(KeyError):
    """A schema ``$id`` the registry does not hold. Never a silent pass."""


@cache
def _documents() -> dict[str, Any]:
    docs: dict[str, Any] = {}
    for path in sorted(SCHEMA_DIR.rglob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        schema_id = doc["$id"]
        if schema_id in docs:
            raise ValueError(f"duplicate schema $id {schema_id!r} ({path})")
        docs[schema_id] = doc
    return docs


@cache
def registry() -> Registry[Any]:
    reg: Registry[Any] = Registry()
    for schema_id, doc in _documents().items():
        reg = reg.with_resource(schema_id, Resource.from_contents(doc, DRAFT202012))
    return reg


def schema_ids() -> list[str]:
    return sorted(_documents())


def is_rfc3339(value: object) -> bool:
    """RFC 3339 date-time, checked without the optional ``rfc3339-validator`` package."""
    if not isinstance(value, str):
        return True  # formats apply to strings only
    if not RFC3339.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value.replace("t", "T").replace("z", "Z"))
    except ValueError:
        return False
    return True


@cache
def format_checker() -> FormatChecker:
    """The draft's format checkers, with ``date-time`` always enforced."""
    checker = FormatChecker()
    checker.checks("date-time")(is_rfc3339)
    return checker


def validate(instance: Any, schema_id: str) -> None:
    """Raise ``ValidationError`` unless ``instance`` satisfies the schema with this ``$id``.

    ``schema_id`` may carry a fragment, such as ``forgectl/defs.json#/$defs/ts``.
    """
    try:
        registry().resolver().lookup(schema_id)
    except Unresolvable:
        raise UnknownSchemaError(schema_id) from None
    validator = Draft202012Validator(
        {"$ref": schema_id}, registry=registry(), format_checker=format_checker()
    )
    validator.validate(instance)


def validate_cli_output(command: str, instance: Any) -> None:
    validate(instance, f"forgectl/cli/{command}.json")


def validate_error_envelope(instance: Any) -> None:
    validate(instance, "forgectl/cli/error.json")


def validate_hook_payload(hook: str, instance: Any) -> None:
    if hook not in HOOKS:
        raise UnknownSchemaError(f"hook {hook!r}")
    validate(instance, f"forgectl/hooks/{hook}.json")


def validate_log_record(instance: Any) -> None:
    validate(instance, "forgectl/log.json")


def validate_audit_record(instance: Any) -> None:
    validate(instance, "forgectl/audit.json")


def validate_jsonl(text: str, schema_id: str) -> list[Any]:
    """Validate every non-empty line of a ``.jsonl`` text; return the parsed records."""
    records: list[Any] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            validate(record, schema_id)
        except (json.JSONDecodeError, ValidationError) as err:
            failure = ValidationError(f"line {number}: {getattr(err, 'message', err)}")
            failure.add_note(f"line {number}")
            raise failure from err
        records.append(record)
    return records


def check_all_schemas() -> None:
    """Raise if any schema is not itself a valid Draft 2020-12 schema."""
    for doc in _documents().values():
        Draft202012Validator.check_schema(doc)


def _refs(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_refs(item))
    return found


def unresolvable_refs() -> list[str]:
    """Every ``$ref`` in ``schemas/`` the registry cannot resolve, as ``<id> -> <ref>``."""
    bad: list[str] = []
    for schema_id, doc in _documents().items():
        resolver = registry().resolver(base_uri=schema_id)
        for ref in _refs(doc):
            try:
                resolver.lookup(ref)
            except Unresolvable:
                bad.append(f"{schema_id} -> {ref}")
    return bad
