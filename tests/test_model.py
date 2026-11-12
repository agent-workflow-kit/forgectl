"""Tests for the normalized model (issue #10): blockers, CI, approvals, tri-states, ordering."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from forgectl import model as m

H = "a" * 40
OLD = "b" * 40
NEW = "c" * 40
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-02T00:00:00Z"
T2 = "2026-01-03T00:00:00Z"

# --- local schema helper -----------------------------------------------------------------------
# TODO(#3): replace with the shared golden-file registry; this duplicates
# tests/cli_schema_check.py on purpose so this module stays independent of it.

_SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


def _registry() -> Registry:
    registry: Registry = Registry()
    for path in sorted(_SCHEMAS.rglob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(
            doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012)
        )
    return registry


_REG = _registry()


def validate(instance: Any, schema_id: str) -> None:
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$ref": schema_id}
    Draft202012Validator(schema, registry=_REG).validate(instance)


# --- builders ----------------------------------------------------------------------------------


def check(name: str, state: str, *, counted: bool | None = None, url: str | None = None) -> m.Check:
    return m.Check(
        name=name,
        state=state,  # type: ignore[arg-type]
        required=None,
        counted=(state != "skipped") if counted is None else counted,
        url=url,
        finished_at=None,
    )


def author(login: str = "alice", id_: str | None = "u1") -> m.Author:
    return m.Author(login=login, id=id_, is_bot=False)


def entry(
    login: str,
    state: str = "approved",
    *,
    id_: str | None = None,
    sha: str | None = H,
    at: str | None = T1,
    can_push: bool | None = True,
) -> m.LatestByReviewer:
    return m.LatestByReviewer(
        login=login,
        id=id_ or f"id-{login}",
        state=state,  # type: ignore[arg-type]
        commit_sha=sha,
        at=at,
        can_push=can_push,
    )


def make_pr(**over: Any) -> m.PR:
    base: dict[str, Any] = dict(
        pr="github.com/o/r#7",
        repo="github.com/o/r",
        pr_id="PR_abc",
        number=7,
        url="https://github.com/o/r/pull/7",
        backend="github",
        title="t",
        repo_id="R_abc",
        body=None,
        author=author(),
        state="open",
        draft=False,
        created_at=T0,
        updated_at=T1,
        closed_at=None,
        merged_at=None,
        merged_by=None,
        labels=("z", "a"),
        author_association=None,
        head=m.Head(sha=H, ref="feat", repo="github.com/o/r", is_fork=False),
        base=m.Base(ref="main", sha=OLD, is_default=True),
        merged_head_sha=None,
        merge_commit_sha=None,
        head_fetch_ref="refs/pull/7/head",
        mergeable=True,
        blocked_by=(),
        auto_merge=m.AutoMerge(enabled=False, by=None),
        merge_queue=False,
        merge_methods_allowed=("squash",),
        additions=1,
        deletions=2,
        changed_files=1,
        review_decision="none",
        approvals=0,
        approvals_at_head=None,
        approvals_left=None,
    )
    base.update(over)
    return m.PR(**base)


# --- CI aggregation ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("checks", "complete", "expected"),
    [
        ([], True, "none"),
        ([check("a", "skipped")], True, "none"),
        ([check("a", "skipped"), check("b", "skipped")], True, "none"),
        ([check("a", "passing")], True, "passing"),
        ([check("a", "passing"), check("b", "skipped")], True, "passing"),
        ([check("a", "failing"), check("b", "pending")], True, "failing"),
        ([check("a", "passing"), check("b", "pending")], True, "pending"),
        ([check("a", "passing"), check("b", "failing")], True, "failing"),
        ([check("a", "passing")], False, "pending"),
        ([check("a", "failing")], False, "failing"),
        ([], False, "none"),
        # an uncounted failing check (GitLab allow_failure) does not fail the PR
        ([check("a", "failing", counted=False), check("b", "passing")], True, "passing"),
        ([check("a", "failing", counted=False)], True, "none"),
        ([check("a", "pending", counted=False)], True, "none"),
    ],
)
def test_ci_aggregation_table(checks: list[m.Check], complete: bool, expected: str) -> None:
    ci = m.aggregate_ci(checks, pr_head_sha=H, checks_sha=H, complete=complete)
    assert ci.state == expected
    assert ci.for_current_head is True
    assert ci.head_sha == H


def test_ci_commit_that_is_not_the_head_forces_pending_whatever_it_reported() -> None:
    ci = m.aggregate_ci([check("a", "passing")], pr_head_sha=H, checks_sha=OLD)
    assert (ci.state, ci.for_current_head, ci.head_sha) == ("pending", False, OLD)


def test_ci_old_commit_with_no_checks_is_still_pending() -> None:
    ci = m.aggregate_ci([], pr_head_sha=H, checks_sha=OLD)
    assert ci.state == "pending"


def test_ci_no_pipeline_is_none_for_the_head() -> None:
    ci = m.aggregate_ci([], pr_head_sha=H, checks_sha=None)
    assert (ci.state, ci.for_current_head, ci.head_sha, ci.complete) == ("none", True, H, True)


def test_ci_none_is_not_passing_helper() -> None:
    assert m.ci_is_passing(m.aggregate_ci([], pr_head_sha=H, checks_sha=H)) is False
    assert m.ci_is_passing(m.aggregate_ci([check("a", "passing")], pr_head_sha=H, checks_sha=H))


def test_ci_to_dict_validates_and_sorts_checks() -> None:
    ci = m.aggregate_ci(
        [
            check("b", "passing", url="https://x/2"),
            check("a", "pending", url="https://x/9"),
            check("a", "passing", url="https://x/1"),
            check("a", "passing", url=None),
        ],
        pr_head_sha=H,
        checks_sha=H,
    )
    out = {"pr": "github.com/o/r#7", "head_sha": H, "ci": ci.to_dict()}
    validate(out, "forgectl/cli/ci.json")
    keys = [(c["name"], c["url"], c["state"]) for c in out["ci"]["checks"]]
    assert keys == [
        ("a", None, "passing"),
        ("a", "https://x/1", "passing"),
        ("a", "https://x/9", "pending"),
        ("b", "https://x/2", "passing"),
    ]


def test_skipped_check_cannot_be_counted() -> None:
    with pytest.raises(ValueError):
        m.Check(name="a", state="skipped", required=None, counted=True, url=None, finished_at=None)


# Every row of the interface's check-state table (CI object), one column per source.
_STATE_ROWS: list[tuple[str, str, bool | None, str]] = [
    *[("github_check", r, None, "passing") for r in ("SUCCESS",)],
    *[
        ("github_check", r, None, "failing")
        for r in (
            "FAILURE",
            "TIMED_OUT",
            "CANCELLED",
            "ACTION_REQUIRED",
            "STARTUP_FAILURE",
            "STALE",
        )
    ],
    *[
        ("github_check", r, None, "pending")
        for r in ("QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED")
    ],
    *[("github_check", r, None, "skipped") for r in ("NEUTRAL", "SKIPPED")],
    ("github_status", "SUCCESS", None, "passing"),
    *[("github_status", r, None, "failing") for r in ("FAILURE", "ERROR")],
    *[("github_status", r, None, "pending") for r in ("PENDING", "EXPECTED")],
    ("gitlab", "success", None, "passing"),
    *[("gitlab", r, None, "failing") for r in ("failed", "canceled")],
    *[
        ("gitlab", r, None, "pending")
        for r in (
            "created",
            "pending",
            "preparing",
            "running",
            "scheduled",
            "waiting_for_resource",
            "waiting_for_callback",
        )
    ],
    ("gitlab", "manual", False, "pending"),
    ("gitlab", "skipped", None, "skipped"),
    ("gitlab", "manual", True, "skipped"),
    ("gitlab", "manual", None, "pending"),  # unknown allow_failure is the stricter answer
]


@pytest.mark.parametrize(("kind", "raw", "allow_failure", "expected"), _STATE_ROWS)
def test_check_state_mapping(
    kind: str, raw: str, allow_failure: bool | None, expected: str
) -> None:
    assert m.normalize_check_state(kind, raw, allow_failure=allow_failure) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kind", "raw"),
    [
        ("github_check", "BRAND_NEW"),
        ("github_check", ""),
        ("github_check", "success"),  # wrong casing is unmapped, never passing
        ("github_status", "STALE"),  # a check-run value, not a status-context one
        ("gitlab", "SUCCESS"),
        ("gitlab", "weird"),
    ],
)
def test_unmapped_state_is_pending_with_a_warning(kind: str, raw: str) -> None:
    warnings: list[str] = []
    assert m.normalize_check_state(kind, raw, warn=warnings.append) == "pending"  # type: ignore[arg-type]
    assert len(warnings) == 1


def test_counted_rule_and_required_and_url() -> None:
    assert m.is_counted("skipped", allow_failure=None) is False
    assert m.is_counted("failing", allow_failure=True) is False
    assert m.is_counted("failing", allow_failure=False) is True
    assert m.is_counted("passing", allow_failure=None) is True
    assert m.gitlab_required(True) is False
    assert m.gitlab_required(False) is None
    assert m.gitlab_required(None) is None
    assert m.clean_check_url("https://ci.example/1") == "https://ci.example/1"
    assert m.clean_check_url("http://ci.example/1") == "http://ci.example/1"
    for bad in ("javascript:alert(1)", "ftp://x", "HTTPS://x", "//x", "", None):
        assert m.clean_check_url(bad) is None


@pytest.mark.parametrize(
    "bad",
    ["https://x/a\nb", "https://x/a\tb", "https://x/a b", "https://x/\x00", "https://x/\x7f"],
)
def test_check_url_with_whitespace_or_control_is_null(bad: str) -> None:
    assert m.clean_check_url(bad) is None


# --- blocked_by --------------------------------------------------------------------------------


def ci_of(*checks: m.Check, checks_sha: str = H, complete: bool = True) -> m.CI:
    return m.aggregate_ci(list(checks), pr_head_sha=H, checks_sha=checks_sha, complete=complete)


def codes(facts: m.BlockFacts) -> list[tuple[str, str | None]]:
    return [(b.code.value, b.detail) for b in m.compute_blocked_by(facts)]


def facts(**over: Any) -> m.BlockFacts:
    base: dict[str, Any] = {"state": "open", "ci": ci_of(check("a", "passing"))}
    base.update(over)
    return m.BlockFacts(**base)


BLOCK_TABLE: list[tuple[str, dict[str, Any], list[tuple[str, str | None]]]] = [
    ("clean", {}, []),
    ("draft", {"draft": True}, [("draft", None)]),
    ("conflicts", {"conflicts": True}, [("conflicts", None)]),
    ("behind", {"behind": True}, [("branch_behind", None)]),
    ("unknown", {"mergeability_unknown": True}, [("mergeability_unknown", None)]),
    ("reviews_required", {"reviews_required": True}, [("reviews_required", None)]),
    ("host_discussions", {"host_discussions_unresolved": True}, [("discussions_unresolved", None)]),
    ("locked", {"locked": True}, [("other", "locked")]),
    ("failing check", {"ci": ci_of(check("a", "failing"))}, [("checks_failing", None)]),
    (
        "failing + pending",
        {"ci": ci_of(check("a", "failing"), check("b", "pending"))},
        [("checks_failing", None), ("checks_pending", None)],
    ),
    ("pending check", {"ci": ci_of(check("a", "pending"))}, [("checks_pending", None)]),
    (
        "old pipeline",
        {"ci": ci_of(check("a", "passing"), checks_sha=OLD)},
        [("checks_pending", None)],
    ),
    (
        "incomplete list",
        {"ci": ci_of(check("a", "passing"), complete=False)},
        [("checks_pending", None)],
    ),
    # no checks: not a blocker by itself when the host does not block
    ("no checks, host clean", {"ci": ci_of()}, []),
    ("skipped only, host clean", {"ci": ci_of(check("a", "skipped"))}, []),
    # GitHub BLOCKED
    (
        "unregistered required check",
        {"ci": ci_of(), "host_blocked": True},
        [("checks_pending", None)],
    ),
    (
        "skipped-only and BLOCKED",
        {"ci": ci_of(check("a", "skipped")), "host_blocked": True},
        [("checks_pending", None)],
    ),
    (
        "BLOCKED + reviews_required + no checks",
        {"ci": ci_of(), "host_blocked": True, "reviews_required": True},
        [("checks_pending", None), ("reviews_required", None)],
    ),
    (
        "BLOCKED + changes_requested + no checks",
        {
            "ci": ci_of(),
            "host_blocked": True,
            "latest_by_reviewer": (entry("bob", "changes_requested"),),
        },
        [("changes_requested", None), ("checks_pending", None)],
    ),
    (
        "BLOCKED explained by reviews",
        {"host_blocked": True, "reviews_required": True},
        [("reviews_required", None)],
    ),
    (
        "BLOCKED explained by threads",
        {"host_blocked": True, "unresolved_threads": 2},
        [("discussions_unresolved", None)],
    ),
    (
        "BLOCKED explained by nothing",
        {"host_blocked": True},
        [("other", "BLOCKED")],
    ),
    (
        "BLOCKED, threads count unknown",
        {"host_blocked": True, "unresolved_threads": None},
        [("other", "BLOCKED")],
    ),
    (
        "BLOCKED explained by failing check",
        {"host_blocked": True, "ci": ci_of(check("a", "failing"))},
        [("checks_failing", None)],
    ),
    # GitLab
    (
        "ci_still_running",
        {"host_checks_pending": True},
        [("checks_pending", None)],
    ),
    (
        "ci_must_pass, nothing failing",
        {"host_ci_must_pass": True, "ci": ci_of()},
        [("checks_pending", None)],
    ),
    (
        "ci_must_pass, a counted failure",
        {"host_ci_must_pass": True, "ci": ci_of(check("a", "failing"))},
        [("checks_failing", None)],
    ),
    (
        "other host statuses keep their wording",
        {"other_details": ("merge_time", "commits_status", "merge_time")},
        [("other", "commits_status"), ("other", "merge_time")],
    ),
    (
        "changes requested",
        {"latest_by_reviewer": (entry("bob", "changes_requested"),)},
        [("changes_requested", None)],
    ),
    (
        "changes requested by an account that cannot push",
        {"latest_by_reviewer": (entry("bob", "changes_requested", can_push=False),)},
        [],
    ),
    (
        "changes requested, can_push unknown still blocks",
        {"latest_by_reviewer": (entry("bob", "changes_requested", can_push=None),)},
        [("changes_requested", None)],
    ),
    (
        "GitLab requested_changes with no entry",
        {"host_changes_requested": True},
        [("changes_requested", None)],
    ),
    (
        "dismissed does not block",
        {"latest_by_reviewer": (entry("bob", "dismissed"),)},
        [],
    ),
    (
        "everything, ordered by code point",
        {
            "draft": True,
            "conflicts": True,
            "behind": True,
            "mergeability_unknown": True,
            "reviews_required": True,
            "host_discussions_unresolved": True,
            "locked": True,
            "ci": ci_of(check("a", "failing"), check("b", "pending")),
            "latest_by_reviewer": (entry("bob", "changes_requested"),),
        },
        [
            ("branch_behind", None),
            ("changes_requested", None),
            ("checks_failing", None),
            ("checks_pending", None),
            ("conflicts", None),
            ("discussions_unresolved", None),
            ("draft", None),
            ("mergeability_unknown", None),
            ("other", "locked"),
            ("reviews_required", None),
        ],
    ),
]


@pytest.mark.parametrize(("name", "over", "expected"), BLOCK_TABLE, ids=[r[0] for r in BLOCK_TABLE])
def test_blocked_by_table(name: str, over: dict[str, Any], expected: list[Any]) -> None:
    assert codes(facts(**over)) == expected


@pytest.mark.parametrize("state", ["closed", "merged"])
def test_closed_or_merged_has_no_blockers(state: str) -> None:
    assert codes(facts(state=state, draft=True, conflicts=True, host_blocked=True)) == []


def test_blocker_shape_rules() -> None:
    assert m.Blocker(m.BlockerCode.DRAFT).to_dict() == {"code": "draft"}
    assert m.Blocker(m.BlockerCode.OTHER, "x").to_dict() == {"code": "other", "detail": "x"}
    with pytest.raises(ValueError):
        m.Blocker(m.BlockerCode.OTHER)
    with pytest.raises(ValueError):
        m.Blocker(m.BlockerCode.OTHER, "")
    with pytest.raises(ValueError):
        m.Blocker(m.BlockerCode.DRAFT, "detail")


def test_blocker_schema_accepts_every_code() -> None:
    for code in m.BlockerCode:
        b = m.Blocker(code, "x" if code is m.BlockerCode.OTHER else None)
        validate([b.to_dict()], "forgectl/defs.json#/$defs/blocked_by")


def test_blocked_by_does_not_mean_ci_passed() -> None:
    f = facts(ci=ci_of())
    assert codes(f) == []
    assert m.ci_is_passing(f.ci) is False


# --- approvals ---------------------------------------------------------------------------------

ALICE = author("alice", "u1")


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([], 0),
        ([entry("bob")], 1),
        ([entry("bob"), entry("carol")], 2),
        ([entry("bob", "changes_requested")], 0),
        ([entry("bob", "dismissed")], 0),
        ([entry("alice", id_="u1")], 0),  # author by id
        ([entry("ALICE", id_="u1")], 0),  # author by id, login spelled differently
        ([entry("alice2", id_="u1")], 0),
        ([entry("alice", id_="other")], 1),  # same login, other id: not the author
        ([entry("bob", can_push=False)], 0),
        ([entry("bob", can_push=None)], 1),  # unknown can_push counts for approvals
        ([entry("bob", sha=OLD)], 1),  # any head
    ],
)
def test_approvals(entries: list[m.LatestByReviewer], expected: int) -> None:
    assert m.count_approvals(entries, ALICE) == expected


def test_author_compare_falls_back_to_login_when_no_id() -> None:
    no_id = m.Author(login="Alice", id=None, is_bot=False)
    e = m.LatestByReviewer("alice", None, "approved", H, T1, True)
    assert m.count_approvals([e], no_id) == 0
    assert m.count_approvals([e], m.Author(login=None, id=None, is_bot=None)) == 1


def gh_at_head(entries: list[m.LatestByReviewer], **kw: Any) -> int | None:
    kw.setdefault("retarget_at", None)
    kw.setdefault("retarget_known", True)
    return m.count_approvals_at_head(entries, ALICE, head_sha=H, backend="github", **kw)


@pytest.mark.parametrize(
    ("entries", "kw", "expected"),
    [
        ([], {}, 0),
        ([entry("bob")], {}, 1),
        ([entry("bob", sha=OLD)], {}, 0),
        ([entry("bob", can_push=None)], {}, 0),  # unknown can_push is not counted at head
        ([entry("bob", can_push=False)], {}, 0),
        ([entry("alice", id_="u1")], {}, 0),
        # retarget discount: older approvals and approvals with no time do not count
        ([entry("bob", at=T0)], {"retarget_at": T1}, 0),
        ([entry("bob", at=T1)], {"retarget_at": T1}, 1),
        ([entry("bob", at=T2)], {"retarget_at": T1}, 1),
        ([entry("bob", at=None)], {"retarget_at": T1}, 0),
        ([entry("bob", at=None)], {}, 1),
        ([entry("bob", at="garbage")], {"retarget_at": T1}, 0),
        ([entry("bob", at="2026-01-01T12:00:00")], {"retarget_at": T0}, 0),  # naive time
        ([entry("bob", at="2026-01-02T01:00:00+02:00")], {"retarget_at": T1}, 0),  # offsets
        ([entry("bob", at="2026-01-02T05:00:00+02:00")], {"retarget_at": T1}, 1),
        # a failed retarget walk: no approval counts
        ([entry("bob")], {"retarget_known": False}, None),
    ],
)
def test_github_approvals_at_head(
    entries: list[m.LatestByReviewer], kw: dict[str, Any], expected: int | None
) -> None:
    assert gh_at_head(entries, **kw) == expected


@pytest.mark.parametrize(
    ("reset", "entries", "kw", "expected"),
    [
        (None, [entry("bob", sha=None, can_push=True)], {}, None),
        (False, [entry("bob", sha=None, can_push=True)], {}, None),
        (True, [entry("bob", sha=None, can_push=True)], {}, 1),
        (True, [entry("bob", sha=None, can_push=None)], {}, 0),
        (True, [entry("bob", sha=None, can_push=False)], {}, 0),
        (True, [], {}, 0),
        (True, [entry("bob", sha=None, at=T0)], {"retarget_at": T1}, 0),
        (True, [entry("bob", sha=None, at=None)], {"retarget_at": T1}, 0),
        (True, [entry("bob", sha=None)], {"retarget_known": False}, None),
        (False, [entry("bob", sha=None)], {"retarget_known": False}, None),
    ],
)
def test_gitlab_approvals_at_head(
    reset: bool | None, entries: list[m.LatestByReviewer], kw: dict[str, Any], expected: int | None
) -> None:
    kw.setdefault("retarget_at", None)
    kw.setdefault("retarget_known", True)
    got = m.count_approvals_at_head(
        entries, ALICE, head_sha=H, backend="gitlab", reset_approvals_on_push=reset, **kw
    )
    assert got == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), (0, False), (1, True), (5, True)],
)
def test_null_approval_count_is_never_approved(value: int | None, expected: bool) -> None:
    assert m.is_approved(value) is expected
    assert make_pr(approvals_at_head=value).approved_at_head is expected


def test_null_approvals_at_head_with_review_decision_approved_is_not_approved() -> None:
    assert make_pr(review_decision="approved", approvals_at_head=None).approved_at_head is False


@pytest.mark.parametrize("bad", [True, "1", 1.0, -1])
def test_is_approved_rejects_non_counts(bad: Any) -> None:
    assert m.is_approved(bad) is False


@pytest.mark.parametrize(
    ("entries", "left", "approvals", "expected"),
    [
        ([entry("b", "changes_requested")], 0, 3, "changes_requested"),
        ([entry("b", "changes_requested", can_push=False)], 0, 3, "changes_requested"),
        ([], 1, 3, "review_required"),
        ([], 0, 1, "approved"),
        ([], 0, 0, "none"),
        ([], None, 4, "none"),  # unknown approvals_left is never read as approved
        ([entry("b", "dismissed")], 0, 0, "none"),
    ],
)
def test_gitlab_review_decision(
    entries: list[m.LatestByReviewer], left: int | None, approvals: int, expected: str
) -> None:
    assert m.gitlab_review_decision(entries, left, approvals) == expected


def test_github_review_decision() -> None:
    assert m.github_review_decision(None) == "none"
    assert m.github_review_decision("APPROVED") == "approved"
    assert m.github_review_decision("CHANGES_REQUESTED") == "changes_requested"
    assert m.github_review_decision("REVIEW_REQUIRED") == "review_required"
    assert m.github_review_decision("NEW_VALUE") == "review_required"  # never approved by default


# --- tri-states --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fn", "value", "expected"),
    [
        (m.is_true, True, True),
        (m.is_true, False, False),
        (m.is_true, None, False),
        (m.is_true, 1, False),
        (m.is_true, "true", False),
        (m.is_false, False, True),
        (m.is_false, None, False),
        (m.is_false, 0, False),
    ],
)
def test_tristate_helpers(fn: Any, value: Any, expected: bool) -> None:
    assert fn(value) is expected


def test_unknown_is_not_write_access_or_a_human_or_mergeable() -> None:
    assert m.is_true(None) is False
    assert m.can_act_for_automatically(m.Author("bot", "1", True)) is False
    assert m.can_act_for_automatically(m.Author("x", "1", None)) is False
    assert m.can_act_for_automatically(m.Author("x", "1", False)) is True
    assert make_pr(mergeable=None).is_mergeable is False
    assert (
        make_pr(mergeable=True, blocked_by=(m.Blocker(m.BlockerCode.DRAFT),)).is_mergeable is False
    )
    assert make_pr(mergeable=True).is_mergeable is True
    assert make_pr(mergeable=False).is_mergeable is False


# --- stable ordering ---------------------------------------------------------------------------


def comment(id_: str, created: str) -> m.Comment:
    return m.Comment(
        id=id_,
        author=author(),
        body="b",
        created_at=created,
        updated_at=created,
        minimized=False,
        reply_to=None,
        review_id=None,
    )


def thread(id_: str, comments: list[m.Comment]) -> m.Thread:
    return m.Thread(
        id=id_,
        resolvable=True,
        resolved=False,
        outdated=False,
        path="f",
        line=1,
        side="right",
        start_line=None,
        commit_sha=H,
        comments=tuple(comments),
    )


def review(id_: str, at: str) -> m.Review:
    return m.Review(
        id=id_, author=author(), state="approved", commit_sha=H, submitted_at=at, body=""
    )


def rr(login: str, kind: str = "user") -> m.ReviewRequest:
    return m.ReviewRequest(
        reviewer=m.Reviewer(login=login, id=None, kind=kind),  # type: ignore[arg-type]
        state="outstanding",
        code_owner=None,
        instance=None,
        requested_at=None,
    )


def make_snapshot(**over: Any) -> m.Snapshot:
    base: dict[str, Any] = dict(
        pr=make_pr(),
        review_requests=(),
        teams_visible=True,
        reviews=(),
        latest_by_reviewer=(),
        threads=(),
        ci=ci_of(check("a", "passing")),
        rate_limit=m.RateLimit(host="github.com", remaining=10, reset_at=T1),
        fetched_at=T2,
        incomplete=(),
        activity=m.Activity(
            comments_total=0, unresolved_threads=0, others_since_push=None, awaiting_my_reply=0
        ),
    )
    base.update(over)
    return m.Snapshot(**base)


def test_activity_serializes_in_the_snapshot_and_rejects_a_bad_count() -> None:
    activity = m.Activity(
        comments_total=3, unresolved_threads=2, others_since_push=1, awaiting_my_reply=0
    )
    out = make_snapshot(activity=activity).to_dict()
    assert out["activity"] == {
        "comments_total": 3,
        "unresolved_threads": 2,
        "others_since_push": 1,
        "awaiting_my_reply": 0,
    }
    validate(out, "forgectl/cli/snapshot.json")
    for bad in (-1, True, 1.5):
        with pytest.raises(ValueError, match="count"):
            m.Activity(
                comments_total=bad,  # type: ignore[arg-type]
                unresolved_threads=0,
                others_since_push=None,
                awaiting_my_reply=0,
            )


def test_a_snapshot_without_activity_cannot_be_built() -> None:
    with pytest.raises(TypeError):
        m.Snapshot(  # type: ignore[call-arg]
            pr=make_pr(),
            review_requests=(),
            teams_visible=True,
            reviews=(),
            latest_by_reviewer=(),
            threads=(),
            ci=ci_of(check("a", "passing")),
            rate_limit=m.RateLimit(host="github.com", remaining=10, reset_at=T1),
            fetched_at=T2,
        )


def test_snapshot_validates_against_schema() -> None:
    snap = make_snapshot(
        review_requests=(rr("bob"),),
        reviews=(review("gh:r:1", T0),),
        latest_by_reviewer=(entry("bob"),),
        threads=(thread("gh:rt:1", [comment("gh:rc:1", T0)]),),
    )
    validate(snap.to_dict(), "forgectl/cli/snapshot.json")
    validate(snap.pr.to_dict(), "forgectl/cli/show.json")


def test_snapshot_incomplete_follows_parts_and_ci() -> None:
    d = make_snapshot().to_dict()
    assert (d["complete"], d["incomplete"]) == (True, [])
    d = make_snapshot(ci=ci_of(check("a", "passing"), complete=False)).to_dict()
    assert (d["complete"], d["incomplete"]) == (False, ["ci"])
    d = make_snapshot(incomplete=("threads", "reviews", "threads")).to_dict()
    assert (d["complete"], d["incomplete"]) == (False, ["reviews", "threads"])
    validate(d, "forgectl/cli/snapshot.json")
    with pytest.raises(ValueError):
        make_snapshot(incomplete=("nonsense",)).to_dict()


def test_snapshots_compare_directly_whatever_the_input_order() -> None:
    reviews = [review("gh:r:b", T1), review("gh:r:a", T1), review("gh:r:z", T0)]
    latest = [entry("Zed"), entry("bob"), entry("Bob2")]
    rrs = [rr("Zed"), rr("bob", "user"), rr("bob", "team"), rr("Alice")]
    threads = [
        thread("gh:rt:2", [comment("gh:rc:9", T1), comment("gh:rc:2", T0)]),
        thread("gh:rt:1", [comment("gh:rc:3", T1)]),
        thread("gh:rt:0", [comment("gh:rc:8", T0), comment("gh:rc:7", T0)]),
    ]
    checks = [check("b", "passing"), check("a", "pending"), check("a", "passing")]
    a = make_snapshot(
        reviews=tuple(reviews),
        latest_by_reviewer=tuple(latest),
        review_requests=tuple(rrs),
        threads=tuple(threads),
        ci=ci_of(*checks),
    )
    b = make_snapshot(
        reviews=tuple(reversed(reviews)),
        latest_by_reviewer=tuple(reversed(latest)),
        review_requests=tuple(reversed(rrs)),
        threads=tuple(reversed(threads)),
        ci=ci_of(*reversed(checks)),
        rate_limit=m.RateLimit(host="github.com", remaining=3, reset_at=T2),
        fetched_at="2027-01-01T00:00:00Z",
    )
    assert m.comparable(a.to_dict()) == m.comparable(b.to_dict())
    assert a.to_dict() != b.to_dict()  # fetched_at and rate_limit differ and only those
    d = a.to_dict()
    assert [r["id"] for r in d["reviews"]] == ["gh:r:z", "gh:r:a", "gh:r:b"]
    assert [e["login"] for e in d["latest_by_reviewer"]] == ["bob", "Bob2", "Zed"]
    assert [(r["reviewer"]["login"], r["reviewer"]["kind"]) for r in d["review_requests"]] == [
        ("Alice", "user"),
        ("bob", "team"),
        ("bob", "user"),
        ("Zed", "user"),
    ]
    # threads: first comment's created_at (after the comments are ordered), then id
    assert [t["id"] for t in d["threads"]] == ["gh:rt:0", "gh:rt:2", "gh:rt:1"]
    assert [c["id"] for c in d["threads"][1]["comments"]] == ["gh:rc:2", "gh:rc:9"]
    assert [c["id"] for c in d["threads"][0]["comments"]] == ["gh:rc:7", "gh:rc:8"]


def test_comparable_drops_only_fetched_at_and_rate_limit() -> None:
    d = make_snapshot().to_dict()
    c = m.comparable(d)
    assert set(d) - set(c) == {"fetched_at", "rate_limit"}
    assert "fetched_at" in d  # the input is not mutated


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["b", "a", "B", "A"], ["A", "B", "a", "b"]),  # code point, not locale or case-folded
        (["é", "z", "Z"], ["Z", "z", "é"]),
        (["a10", "a2", "a1"], ["a1", "a10", "a2"]),
    ],
)
def test_ordering_is_by_code_point(raw: list[str], expected: list[str]) -> None:
    files = [
        m.FileChange(path=p, previous_path=None, status="modified", additions=0, deletions=0)
        for p in raw
    ]
    assert [
        f["path"]
        for f in m.FilesResult(
            pr="github.com/o/r#7",
            head_sha=H,
            files=tuple(files),
            files_complete=True,
            changed_files=len(raw),
        ).to_dict()["files"]
    ] == expected


def test_login_ordering_is_ascii_lowered_then_kind_not_unicode_folded() -> None:
    # "İ" (U+0130) must not be lowered by Unicode rules; ASCII letters only.
    assert m.ascii_lower("AbÇ-İ") == "abÇ-İ"
    d = make_snapshot(latest_by_reviewer=(entry("b"), entry("A"), entry("a"))).to_dict()
    # ascii-lowered key ties on "a": the original spelling breaks the tie, deterministically
    assert [e["login"] for e in d["latest_by_reviewer"]] == ["A", "a", "b"]


def test_pr_to_dict_sorts_labels_and_blockers_and_validates() -> None:
    pr = make_pr(
        blocked_by=(
            m.Blocker(m.BlockerCode.OTHER, "b"),
            m.Blocker(m.BlockerCode.DRAFT),
            m.Blocker(m.BlockerCode.OTHER, "a"),
        ),
        mergeable=False,
    )
    d = pr.to_dict()
    assert d["blocked_by"] == [
        {"code": "draft"},
        {"code": "other", "detail": "a"},
        {"code": "other", "detail": "b"},
    ]
    assert d["labels"] == ["a", "z"]
    assert d["merge_methods_allowed"] == ["squash"]
    validate(d, "forgectl/cli/show.json")


def test_pr_with_unknowns_validates() -> None:
    pr = make_pr(
        mergeable=None,
        merge_queue=None,
        merge_methods_allowed=None,
        additions=None,
        deletions=None,
        changed_files=None,
        approvals_at_head=None,
        approvals_left=None,
        author=m.Author(login=None, id=None, is_bot=None),
        head=m.Head(sha=H, ref="x", repo=None, is_fork=None),
        base=m.Base(ref="main", sha=None, is_default=None),
    )
    validate(pr.to_dict(), "forgectl/cli/show.json")


# --- result objects for the other read commands -------------------------------------------------


def test_result_objects_validate() -> None:
    p = "github.com/o/r#7"
    validate(
        m.FilesResult(
            pr=p,
            head_sha=H,
            files=(
                m.FileChange("b", "a", "renamed", 1, 2),
                m.FileChange("c", None, "added", None, None),
            ),
            files_complete=True,
            changed_files=2,
        ).to_dict(),
        "forgectl/cli/files.json",
    )
    validate(
        m.CommentsResult(
            pr=p,
            head_sha=H,
            threads=(thread("gh:rt:1", [comment("gh:rc:1", T0)]),),
            complete=True,
        ).to_dict(),
        "forgectl/cli/comments.json",
    )
    validate(
        m.ReviewsResult(
            pr=p,
            head_sha=H,
            reviews=(review("gh:r:1", T0),),
            latest_by_reviewer=(entry("bob"),),
            review_decision="approved",
            approvals=1,
            approvals_at_head=None,
            complete=True,
        ).to_dict(),
        "forgectl/cli/reviews.json",
    )
    validate(m.DiffResult(p, H, OLD, None, "diff").to_dict(), "forgectl/cli/diff.json")
    validate(m.CompareResult(p, OLD, H, "ahead", "d").to_dict(), "forgectl/cli/compare.json")
    validate(m.CompareResult(p, OLD, H, "behind", None).to_dict(), "forgectl/cli/compare.json")
    validate(
        m.AccessResult(
            "github.com/o/r", "bob", None, None, "collaborator_permission", "no_permission_to_read"
        ).to_dict(),
        "forgectl/cli/access.json",
    )


def test_compare_and_access_refuse_inconsistent_shapes() -> None:
    p = "github.com/o/r#7"
    for status, diff in [("behind", "x"), ("diverged", ""), ("unavailable", "x"), ("ahead", None)]:
        with pytest.raises(ValueError):
            m.CompareResult(p, OLD, H, status, diff)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        m.AccessResult(
            "github.com/o/r", "bob", True, "write", "project_member", "no_permission_to_read"
        )
    with pytest.raises(ValueError):
        m.AccessResult("github.com/o/r", "bob", None, None, "project_member", None)


def test_renamed_file_needs_previous_path() -> None:
    with pytest.raises(ValueError):
        m.FileChange("b", None, "renamed", 0, 0)
    with pytest.raises(ValueError):
        m.FileChange("b", "a", "modified", 0, 0)


def test_thread_rules() -> None:
    with pytest.raises(ValueError):
        m.Thread(
            id="gh:rt:1",
            resolvable=False,
            resolved=True,
            outdated=False,
            path=None,
            line=None,
            side=None,
            start_line=None,
            commit_sha=None,
            comments=(comment("gh:ic:1", T0),),
        )
    with pytest.raises(ValueError):
        m.Thread(
            id="gh:rt:1",
            resolvable=True,
            resolved=False,
            outdated=False,
            path="f",
            line=1,
            side="right",
            start_line=None,
            commit_sha=None,
            comments=(),
        )


# --- review round 1: constructor invariants and fail-closed inputs ------------------------------


def test_ci_rejects_a_state_its_checks_do_not_give() -> None:
    with pytest.raises(ValueError):
        m.CI(state="passing", head_sha=H, for_current_head=True, complete=True, checks=())
    with pytest.raises(ValueError):
        m.CI(
            state="none",
            head_sha=H,
            for_current_head=True,
            complete=True,
            checks=(check("a", "failing"),),
        )


def test_ci_not_for_the_head_must_be_pending() -> None:
    with pytest.raises(ValueError):
        m.CI(
            state="passing",
            head_sha=OLD,
            for_current_head=False,
            complete=True,
            checks=(check("a", "passing"),),
        )


def test_ci_incomplete_is_never_passing() -> None:
    with pytest.raises(ValueError):
        m.CI(
            state="passing",
            head_sha=H,
            for_current_head=True,
            complete=False,
            checks=(check("a", "passing"),),
        )


def test_ci_built_by_aggregation_is_accepted() -> None:
    ci = ci_of(check("a", "passing"), complete=False)
    assert m.CI(ci.state, ci.head_sha, ci.for_current_head, ci.complete, ci.checks) == ci


@pytest.mark.parametrize("bad", ["javascript:x", "HTTPS://x", "https://x/a\nb", ""])
def test_check_rejects_a_url_the_cleaner_would_null(bad: str) -> None:
    with pytest.raises(ValueError):
        check("a", "passing", url=bad)


def _mixed_id_entries() -> list[m.LatestByReviewer]:
    # `_same_account` is not transitive here: ("a", None) matches both others by login,
    # while ("A", "5") and ("a", "6") differ by id.
    return [
        m.LatestByReviewer("a", None, "approved", H, T1, True),
        m.LatestByReviewer("A", "5", "approved", H, T1, True),
        m.LatestByReviewer("a", "6", "approved", H, T1, True),
    ]


@pytest.mark.parametrize(
    "entries",
    [
        [entry("b", id_="2"), entry("B", id_="2")],
        [
            m.LatestByReviewer("b", None, "approved", H, T1, True),
            m.LatestByReviewer("B", None, "approved", H, T1, True),
        ],
        _mixed_id_entries(),
        list(reversed(_mixed_id_entries())),
    ],
    ids=["same-id", "no-id", "mixed", "mixed-reversed"],
)
def test_approval_counters_reject_duplicate_accounts(
    entries: list[m.LatestByReviewer],
) -> None:
    with pytest.raises(ValueError):
        m.count_approvals(entries, ALICE)
    with pytest.raises(ValueError):
        m.count_approvals_at_head(
            entries, ALICE, head_sha=H, backend="github", retarget_at=None, retarget_known=True
        )


def test_duplicate_accounts_in_latest_by_reviewer_are_rejected() -> None:
    dup = (entry("b", id_="2"), entry("B", id_="2"))
    with pytest.raises(ValueError):
        make_snapshot(latest_by_reviewer=dup)
    with pytest.raises(ValueError):
        facts(latest_by_reviewer=dup)
    with pytest.raises(ValueError):
        m.ReviewsResult(
            pr="github.com/o/r#7",
            head_sha=H,
            reviews=(),
            latest_by_reviewer=dup,
            review_decision="approved",
            approvals=1,
            approvals_at_head=1,
            complete=True,
        )


def test_approvals_at_head_fails_closed_on_unknown_backend() -> None:
    with pytest.raises(ValueError):
        m.count_approvals_at_head(
            [entry("bob", sha=OLD)],
            ALICE,
            head_sha=H,
            backend="bitbucket",  # type: ignore[arg-type]
            retarget_at=None,
            retarget_known=True,
        )


def test_approvals_at_head_needs_the_retarget_arguments() -> None:
    with pytest.raises(TypeError):
        m.count_approvals_at_head([], ALICE, head_sha=H, backend="github")  # type: ignore[call-arg]


def test_block_facts_reject_an_unknown_state() -> None:
    with pytest.raises(ValueError):
        facts(state="locked")


def test_a_non_string_time_does_not_count_after_a_retarget() -> None:
    e = entry("bob", at=5)  # type: ignore[arg-type]
    assert gh_at_head([e], retarget_at=T1) == 0


def test_snapshot_rejects_ci_in_caller_incomplete() -> None:
    with pytest.raises(ValueError):
        make_snapshot(incomplete=("ci",))


def test_check_order_is_total() -> None:
    def full(finished: str | None, required: bool | None, counted: bool) -> m.Check:
        return m.Check(
            name="a",
            state="failing",
            required=required,
            counted=counted,
            url="https://x/1",
            finished_at=finished,
        )

    checks = [
        full(T1, None, True),
        full(T0, True, True),
        full(None, False, False),
        full(T0, None, True),
        full(T0, True, False),
    ]
    a = ci_of(*checks).to_dict()
    b = ci_of(*reversed(checks)).to_dict()
    assert a == b


def test_merge_methods_are_sorted_and_unique() -> None:
    d = make_pr(merge_methods_allowed=("squash", "merge", "rebase")).to_dict()
    assert d["merge_methods_allowed"] == ["merge", "rebase", "squash"]
    with pytest.raises(ValueError):
        make_pr(merge_methods_allowed=("squash", "squash"))


@pytest.mark.parametrize(
    "over",
    [
        {"state": "closed", "closed_at": T1, "blocked_by": (m.Blocker(m.BlockerCode.DRAFT),)},
        {"state": "closed", "closed_at": T1, "mergeable": True},
        {
            "state": "merged",
            "closed_at": T1,
            "merged_at": T1,
            "mergeable": None,
            "auto_merge": m.AutoMerge(enabled=True, by="bob"),
        },
        {"merge_commit_sha": NEW},
        {"state": "closed", "closed_at": T1, "mergeable": None, "merge_commit_sha": NEW},
    ],
)
def test_pr_rejects_fields_its_state_forbids(over: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        make_pr(**over)


def test_merged_pr_validates() -> None:
    pr = make_pr(
        state="merged",
        closed_at=T1,
        merged_at=T1,
        merged_by="bob",
        merged_head_sha=H,
        merge_commit_sha=NEW,
        mergeable=None,
    )
    validate(pr.to_dict(), "forgectl/cli/show.json")


def test_pr_list_item_validates_and_sorts_labels() -> None:
    item = m.PrListItem(
        pr="github.com/o/r#7",
        pr_id="PR_abc",
        repo="github.com/o/r",
        number=7,
        url="https://github.com/o/r/pull/7",
        title="t",
        author=author(),
        state="open",
        draft=False,
        labels=("z", "a"),
        head=m.Head(sha=H, ref="feat", repo="github.com/o/r", is_fork=False),
        base_ref="main",
        created_at=T0,
        updated_at=T1,
        repo_id="R_abc",
    )
    d = item.to_dict()
    validate(d, "forgectl/defs.json#/$defs/pr_list_item")
    assert d["labels"] == ["a", "z"]
    assert d["base"] == {"ref": "main"}
    page = m.PrPage(items=(item,), complete=True, rate_limit=m.RateLimit(None, None, None))
    assert page.items == (item,)


# --- head.pushed_at and pushed_at_source (issue #56) -------------------------------------------


@pytest.mark.parametrize("source", ["push_event", "commit_date"])
def test_push_time_serializes_in_the_snapshot_head_only(source: str) -> None:
    snap = make_snapshot(head_push=m.PushTime(at=T1, source=source))  # type: ignore[arg-type]
    d = snap.to_dict()
    assert d["head"]["pushed_at"] == T1
    assert d["head"]["pushed_at_source"] == source
    validate(d, "forgectl/cli/snapshot.json")
    # the PR object, `show` and `prs` items keep the head fields they had
    assert set(snap.pr.to_dict()["head"]) == {"sha", "ref", "repo", "is_fork"}
    validate(snap.pr.to_dict(), "forgectl/cli/show.json")


def test_push_time_defaults_to_null_with_null_source() -> None:
    d = make_snapshot().to_dict()
    assert d["head"]["pushed_at"] is None
    assert d["head"]["pushed_at_source"] is None
    validate(d, "forgectl/cli/snapshot.json")
    assert make_snapshot(head_push=None).to_dict() == d


def test_push_time_is_emitted_in_utc() -> None:
    pt = m.PushTime(at="2026-01-02T05:30:00+05:30", source="push_event")
    assert pt.to_dict() == {"pushed_at": T1, "pushed_at_source": "push_event"}
    frac = m.PushTime(at="2026-01-02T00:00:00.250-02:00", source="commit_date")
    assert frac.to_dict()["pushed_at"] == "2026-01-02T02:00:00.250000Z"
    validate(make_snapshot(head_push=frac).to_dict(), "forgectl/cli/snapshot.json")


@pytest.mark.parametrize("at", ["2026-01-02T00:00:00z", "2026-01-02t00:00:00z"])
def test_push_time_accepts_lowercase_t_and_z(at: str) -> None:
    pt = m.PushTime(at=at, source="push_event")
    assert pt.to_dict()["pushed_at"] == T1


@pytest.mark.parametrize("at", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_push_time_rejects_times_outside_the_utc_range(at: str) -> None:
    with pytest.raises(ValueError):
        m.PushTime(at=at, source="push_event")


@pytest.mark.parametrize(
    "at",
    [
        "",
        "2026-01-02",
        "2026-01-02T00:00:00",  # naive: no offset
        "2026-01-02 00:00:00Z",  # not RFC 3339
        "20260102T000000Z",
        "2026-13-02T00:00:00Z",
        "2026-01-02T00:00:00Z\n",
        "not a time",
        "2026-01-02T00:00:00Z; drop",
    ],
)
def test_push_time_rejects_malformed_times(at: str) -> None:
    with pytest.raises(ValueError):
        m.PushTime(at=at, source="push_event")


@pytest.mark.parametrize("source", ["", "unknown", "PUSH_EVENT", "push event", None, 1])
def test_push_time_rejects_other_sources(source: Any) -> None:
    with pytest.raises(ValueError):
        m.PushTime(at=T1, source=source)


@pytest.mark.parametrize("at", [None, 5, b"2026-01-02T00:00:00Z"])
def test_push_time_requires_a_string_time(at: Any) -> None:
    with pytest.raises(ValueError):
        m.PushTime(at=at, source="push_event")


def test_new_push_gives_a_later_time_and_a_different_comparable() -> None:
    a = make_snapshot(head_push=m.PushTime(at=T0, source="push_event")).to_dict()
    b = make_snapshot(head_push=m.PushTime(at=T1, source="push_event")).to_dict()
    assert m.comparable(a) != m.comparable(b)
    # consumers parse `pushed_at` (its precision varies), so compare times, not strings
    assert datetime.fromisoformat(a["head"]["pushed_at"]) < datetime.fromisoformat(
        b["head"]["pushed_at"]
    )
    same = make_snapshot(
        head_push=m.PushTime(at=T0, source="push_event"), fetched_at="2027-01-01T00:00:00Z"
    ).to_dict()
    assert m.comparable(a) == m.comparable(same)


def test_snapshot_schema_requires_both_push_fields_or_neither_filled() -> None:
    good = make_snapshot(head_push=m.PushTime(at=T1, source="push_event")).to_dict()
    bad_cases: list[dict[str, Any]] = [
        {"pushed_at": T1, "pushed_at_source": None},
        {"pushed_at": None, "pushed_at_source": "push_event"},
        {"pushed_at": T1, "pushed_at_source": "unknown"},
        {"pushed_at": "2026-01-02T00:00:00+02:00", "pushed_at_source": "push_event"},
        {"pushed_at": "yesterday", "pushed_at_source": "push_event"},
        {"pushed_at": T1 + "\n", "pushed_at_source": "push_event"},
        {"pushed_at": "2026-01-02T00:00:00z", "pushed_at_source": "push_event"},
        {"pushed_at": "2026-01-02t00:00:00Z", "pushed_at_source": "push_event"},
    ]
    for over in bad_cases:
        d = {**good, "head": {**good["head"], **over}}
        with pytest.raises(Exception):  # noqa: B017 - jsonschema.ValidationError
            validate(d, "forgectl/cli/snapshot.json")
    for missing in ("pushed_at", "pushed_at_source"):
        d = {**good, "head": {k: v for k, v in good["head"].items() if k != missing}}
        with pytest.raises(Exception):  # noqa: B017
            validate(d, "forgectl/cli/snapshot.json")


@pytest.mark.parametrize(
    "at", [T1, "2026-01-02T00:00:00.5Z", "2026-01-02T00:00:00.123456789Z", None]
)
def test_snapshot_schema_accepts_utc_push_times_and_null(at: str | None) -> None:
    good = make_snapshot(head_push=m.PushTime(at=T1, source="push_event")).to_dict()
    source = None if at is None else "push_event"
    d = {**good, "head": {**good["head"], "pushed_at": at, "pushed_at_source": source}}
    validate(d, "forgectl/cli/snapshot.json")


def test_show_schema_rejects_push_fields() -> None:
    d = make_pr().to_dict()
    d["head"] = {**d["head"], "pushed_at": T1, "pushed_at_source": "push_event"}
    with pytest.raises(Exception):  # noqa: B017
        validate(d, "forgectl/cli/show.json")
