from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgectl.errors import (
    EXIT_BY_CODE,
    REFUSAL_REASONS,
    ForgectlError,
    refused,
    truncate_utf8,
    usage,
)

from .harness.schemas import validate

DEFS = json.loads((Path(__file__).parent.parent / "schemas" / "defs.json").read_text())["$defs"]

# Written out from the interface's "Exit codes" table, not derived from the code under test.
EXPECTED_EXIT = {
    "internal": 1,
    "usage": 2,
    "config_invalid": 2,
    "invalid_pr": 2,
    "body_too_large": 2,
    "method_required": 2,
    "not_found": 10,
    "vetoed": 11,
    "auth_required": 12,
    "permission_denied": 12,
    "rate_limited": 12,
    "upstream_error": 12,
    "network_error": 12,
    "tool_missing": 12,
    "host_rejected": 12,
    "outcome_unknown": 12,
    "outcome_partial": 12,
    "merge_unverified": 12,
    "invalid_position": 12,
    "quick_action_ran": 12,
    "approved_stale_head": 12,
    "wrong_host_response": 12,
    "audit_unavailable": 12,
    "audit_failed": 12,
    "partial_results": 12,
    "unsupported_host_version": 12,
    "confirmation_required": 13,
    "escalated": 13,
    "refused": 20,
}


def test_exit_table_matches_interface() -> None:
    assert EXIT_BY_CODE == EXPECTED_EXIT


def test_exit_table_covers_every_schema_code() -> None:
    assert set(EXIT_BY_CODE) == set(DEFS["error_code"]["enum"])


def test_refusal_reasons_match_schema() -> None:
    assert set(REFUSAL_REASONS) == set(DEFS["refusal_reason"]["enum"])


DETAILS_BY_CODE = {
    "outcome_partial": {"landed": {"ids": [], "thread_ids": []}, "failed": "approve"},
    "host_rejected": {"status": None},
    "quick_action_ran": {"ids": ["gh:ic:abc"]},
    "approved_stale_head": {"ids": ["gh:r:abc"]},
    "audit_failed": {"outcome": "merged"},
    "body_too_large": {"index": None, "limit": 262144},
    "config_invalid": {"key": "timeout"},
    "tool_missing": {"install": "brew install gh"},
    "wrong_host_response": {"observed": "an html page"},
    "merge_unverified": {"observed": "open", "head_sha": "a" * 40},
}
DETAILS_BY_REASON = {
    "not_ready": {"blocked_by": [{"code": "draft"}]},
    "drafts_present": {"count": 2},
    "stale_base": {"base_ref": "main"},
    "not_open": {"state": "closed"},
    "self_approval": {"basis": "authenticated"},
    "method_not_allowed": {"allowed": ["squash"]},
    "auto_merge_unavailable": {"observed": "not enabled"},
}


def _build(code: str) -> ForgectlError:
    if code == "refused":
        return refused("not_open", "closed", details={"state": "closed"})
    if code in ("vetoed", "escalated"):
        hooks = [{"hook": "pre-merge", "index": 0}] if code == "escalated" else None
        return ForgectlError(
            code, "hook said no", hook="pre-merge", hook_stderr="nope", escalated_hooks=hooks
        )
    if code == "method_required":
        return ForgectlError(code, "pick one", details={"allowed": ["squash"]})
    if code == "invalid_position":
        return ForgectlError(
            code,
            "bad line",
            details={
                "invalid": [
                    {"index": 0, "path": "a.py", "line": 3, "side": "right", "start_line": None}
                ]
            },
        )
    if code in DETAILS_BY_CODE:
        return ForgectlError(code, "boom", details=DETAILS_BY_CODE[code])
    return ForgectlError(code, "boom")


@pytest.mark.parametrize("code", sorted(EXPECTED_EXIT))
def test_every_code_maps_and_its_envelope_validates(code: str) -> None:
    err = _build(code)
    assert err.exit_code == EXPECTED_EXIT[code]
    validate(err.envelope(), "forgectl/cli/error.json")


@pytest.mark.parametrize("reason", sorted(REFUSAL_REASONS))
def test_every_refusal_reason_exits_20_with_reason(reason: str) -> None:
    details = DETAILS_BY_REASON.get(reason)
    err = refused(reason, "refused", details=details)
    assert err.exit_code == 20
    env = err.envelope()
    assert env["error"]["code"] == "refused"
    assert env["error"]["reason"] == reason
    validate(env, "forgectl/cli/error.json")


def test_refused_code_without_reason_is_rejected() -> None:
    with pytest.raises(ValueError):
        ForgectlError("refused", "no reason")


def test_unknown_reason_is_rejected() -> None:
    with pytest.raises(ValueError):
        refused("because", "x")


def test_reason_on_a_non_refused_code_is_rejected() -> None:
    with pytest.raises(ValueError):
        ForgectlError("usage", "x", reason="not_open")


@pytest.mark.parametrize("reason", sorted(DETAILS_BY_REASON))
def test_refusal_missing_required_details_is_rejected(reason: str) -> None:
    with pytest.raises(ValueError):
        refused(reason, "x", details={})


@pytest.mark.parametrize("code", sorted(DETAILS_BY_CODE) + ["method_required", "invalid_position"])
def test_code_missing_required_details_is_rejected(code: str) -> None:
    with pytest.raises(ValueError):
        ForgectlError(code, "x")


def _each_required_key() -> list[tuple[str, str, str]]:
    cases = [("code", c, k) for c, d in DETAILS_BY_CODE.items() for k in d]
    return cases + [("reason", r, k) for r, d in DETAILS_BY_REASON.items() for k in d]


@pytest.mark.parametrize(
    ("kind", "name", "missing"),
    _each_required_key(),
    ids=lambda v: str(v),
)
def test_each_required_details_key_is_enforced_on_its_own(
    kind: str, name: str, missing: str
) -> None:
    table = DETAILS_BY_CODE if kind == "code" else DETAILS_BY_REASON
    partial = {k: v for k, v in table[name].items() if k != missing}
    with pytest.raises(ValueError, match=missing):
        if kind == "code":
            ForgectlError(name, "x", details=partial)
        else:
            refused(name, "x", details=partial)


def test_stale_head_and_auth_required_details_stay_optional() -> None:
    refused("stale_head", "moved")
    ForgectlError("auth_required", "no token")
    ForgectlError("host_rejected", "no", details={"status": 422})


def test_unknown_code_is_rejected() -> None:
    with pytest.raises(ValueError):
        ForgectlError("nonsense", "x")


def test_vetoed_requires_hook() -> None:
    with pytest.raises(ValueError):
        ForgectlError("vetoed", "x")


def test_escalated_requires_escalated_hooks() -> None:
    with pytest.raises(ValueError):
        ForgectlError("escalated", "x", hook="pre-post", hook_stderr="")


def test_envelope_omits_empty_fields() -> None:
    env = usage("bad flag").envelope()
    assert env == {"error": {"code": "usage", "message": "bad flag"}}


def test_envelope_keeps_set_fields() -> None:
    err = ForgectlError("rate_limited", "slow down", retry_after=12.5, decision_ids=["d1"])
    body = err.envelope()["error"]
    assert body["retry_after"] == 12.5
    assert body["decision_ids"] == ["d1"]


@pytest.mark.parametrize("code", ["vetoed", "escalated"])
def test_veto_and_escalation_envelopes_keep_an_empty_hook_stderr(code: str) -> None:
    hooks = [{"hook": "pre-merge", "index": 0}] if code == "escalated" else None
    env = ForgectlError(
        code, "x", hook="pre-merge", hook_stderr="", escalated_hooks=hooks
    ).envelope()
    assert env["error"]["hook"] == "pre-merge"
    assert env["error"]["hook_stderr"] == ""
    validate(env, "forgectl/cli/error.json")


def test_hook_stderr_is_cut_to_4096_bytes_on_a_code_point_boundary() -> None:
    err = ForgectlError("vetoed", "x", hook="pre-post", hook_stderr="é" * 5000)
    cut = err.envelope()["error"]["hook_stderr"]
    assert len(cut.encode("utf-8")) <= 4096
    assert len(cut.encode("utf-8")) >= 4094
    cut.encode("utf-8").decode("utf-8")


def test_truncate_utf8_never_splits_a_character() -> None:
    assert truncate_utf8("a€b", 3) == "a"  # the euro sign needs bytes 1..3
    assert truncate_utf8("a€b", 4) == "a€"
    assert truncate_utf8("abc", 10) == "abc"
    assert truncate_utf8("😀😀", 5) == "😀"


def test_sanitize_tree_marks_forge_keys_too() -> None:
    from forgectl.errors import sanitize_tree

    out = sanitize_tree({"a\nb‮": {"k\t": ["v‮", 1, None]}, 5: "x"})
    assert out == {"a<U+000A>b<U+202E>": {"k<U+0009>": ["v<U+202E>", 1, None]}, 5: "x"}
