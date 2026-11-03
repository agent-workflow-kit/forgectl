"""Schema registry over ``schemas/``, keyed by ``$id``, and the typed validators built on it.

Every ``--json`` output, hook payload, log record and audit record a test produces goes through
one of these functions, so a shape the schemas do not allow fails the test.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"

HOOKS = ("pre-merge", "post-merge", "pre-post", "post-post")


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


def _schema(schema_id: str) -> Any:
    try:
        return _documents()[schema_id]
    except KeyError:
        raise UnknownSchemaError(schema_id) from None


def validate(instance: Any, schema_id: str) -> None:
    """Raise ``ValidationError`` unless ``instance`` satisfies the schema with this ``$id``."""
    schema = _schema(schema_id)
    validator = Draft202012Validator(
        schema, registry=registry(), format_checker=Draft202012Validator.FORMAT_CHECKER
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
