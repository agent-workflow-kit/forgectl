"""The schema registry and the typed validators the golden tests build on."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import ValidationError

from .harness import schemas

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"


def test_every_schema_file_is_registered_under_its_path() -> None:
    for path in sorted(SCHEMA_DIR.rglob("*.json")):
        expected = "forgectl/" + path.relative_to(SCHEMA_DIR).as_posix()
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["$id"] == expected, path
        assert expected in schemas.schema_ids()


def test_every_schema_is_a_valid_draft_2020_12_schema() -> None:
    schemas.check_all_schemas()


def test_every_ref_resolves_through_the_registry() -> None:
    assert schemas.unresolvable_refs() == []


def test_unknown_schema_id_is_an_error_not_a_pass() -> None:
    with pytest.raises(schemas.UnknownSchemaError):
        schemas.validate({}, "forgectl/cli/nope.json")


def test_error_envelope_validates_and_a_broken_one_is_rejected() -> None:
    good = {"error": {"code": "internal", "message": "x"}}
    schemas.validate_error_envelope(good)
    with pytest.raises(ValidationError):
        schemas.validate_error_envelope({"error": {"code": "not-a-code", "message": "x"}})
    with pytest.raises(ValidationError):
        schemas.validate_error_envelope({"error": {"message": "no code"}})


def test_schema_rejects_extra_and_wrong_typed_fields() -> None:
    # The golden helper must reject output a schema does not allow.
    with pytest.raises(ValidationError):
        schemas.validate({"error": {"code": "internal", "message": 3}}, "forgectl/cli/error.json")
    with pytest.raises(ValidationError):
        schemas.validate("not an object", "forgectl/cli/error.json")


def test_validate_cli_output_picks_the_command_schema() -> None:
    with pytest.raises(schemas.UnknownSchemaError):
        schemas.validate_cli_output("no-such-command", {})


@pytest.mark.parametrize("hook", ["pre-merge", "post-merge", "pre-post", "post-post"])
def test_hook_validator_knows_every_hook(hook: str) -> None:
    assert f"forgectl/hooks/{hook}.json" in schemas.schema_ids()
    with pytest.raises(ValidationError):
        schemas.validate_hook_payload(hook, [])  # a payload is an object


def test_hook_validator_rejects_an_unknown_hook() -> None:
    with pytest.raises(schemas.UnknownSchemaError):
        schemas.validate_hook_payload("pre-lunch", {})


def test_jsonl_validator_reports_the_failing_line() -> None:
    text = '{"error": {"code": "internal", "message": "x"}}\n{"error": 1}\n'
    with pytest.raises(ValidationError) as caught:
        schemas.validate_jsonl(text, "forgectl/cli/error.json")
    assert "line 2" in str(caught.value.__notes__)


def test_jsonl_validator_rejects_a_line_that_is_not_json() -> None:
    with pytest.raises(ValidationError):
        schemas.validate_jsonl("{broken\n", "forgectl/cli/error.json")


def test_log_and_audit_validators_reject_an_empty_record() -> None:
    empty: dict[str, Any] = {}
    with pytest.raises(ValidationError):
        schemas.validate_log_record(empty)
    with pytest.raises(ValidationError):
        schemas.validate_audit_record(empty)


def test_a_fragment_id_validates_against_that_definition() -> None:
    schemas.validate("2026-01-02T03:04:05Z", "forgectl/defs.json#/$defs/ts")
    with pytest.raises(ValidationError):
        schemas.validate(5, "forgectl/defs.json#/$defs/ts")


def test_an_unknown_fragment_is_an_error_not_a_pass() -> None:
    with pytest.raises(schemas.UnknownSchemaError):
        schemas.validate({}, "forgectl/defs.json#/$defs/no-such-def")


@pytest.mark.parametrize(
    "value", ["2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z", "2026-01-01T25:00:00Z"]
)
def test_date_time_format_is_enforced(value: str) -> None:
    with pytest.raises(ValidationError):
        schemas.validate(value, "forgectl/defs.json#/$defs/ts")


def test_the_date_time_checker_follows_rfc_3339() -> None:
    assert schemas.is_rfc3339("2026-01-02t03:04:05.123+05:30")
    assert not schemas.is_rfc3339("2026-01-02 03:04:05Z")  # a space is ISO 8601, not RFC 3339
    assert not schemas.is_rfc3339("2026-13-01T00:00:00Z")
