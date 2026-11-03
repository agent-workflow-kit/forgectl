"""Minimal schema validation for the CLI-surface tests.

TODO(#3): replace with the shared golden-file registry when the test harness lands.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


def _registry() -> Registry:
    registry: Registry = Registry()
    for path in sorted(SCHEMAS.rglob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(doc, default_specification=DRAFT202012)
        registry = registry.with_resource(doc["$id"], resource)
    return registry


_REGISTRY = _registry()


def validate(instance: Any, schema_id: str) -> None:
    """Raise if ``instance`` does not satisfy the schema with ``$id`` ``schema_id``."""
    schema = _REGISTRY.get_or_retrieve(schema_id).value.contents
    Draft202012Validator(schema, registry=_REGISTRY).validate(instance)
