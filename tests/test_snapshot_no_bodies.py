"""`snapshot --no-bodies` (#141): the body-free projection, on a snapshot built in memory."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from forgectl.snapshot_reads import drop_bodies


def _snapshot(body: str = "x") -> dict[str, Any]:
    comment = {"id": "c1", "author": {}, "body": body, "created_at": "t"}
    return {
        "pr": "github.com/a/b#1",
        "body": body,
        "reviews": [{"id": "r1", "state": "approved", "body": body}],
        "threads": [{"id": "t1", "comments": [comment, {**comment, "id": "c2"}]}],
        "ci": {"state": "passing", "checks": [{"name": "body", "state": "passing"}]},
        "incomplete": [],
    }


def test_drop_bodies_removes_the_pr_review_and_comment_bodies() -> None:
    out = drop_bodies(_snapshot("hello"))
    assert "body" not in out
    assert "body" not in out["reviews"][0]
    assert [("body" in c) for c in out["threads"][0]["comments"]] == [False, False]


def test_drop_bodies_leaves_a_field_that_merely_is_named_body_elsewhere() -> None:
    # a check called "body" is data, not a body key: only the three known places are touched
    assert drop_bodies(_snapshot())["ci"]["checks"][0]["name"] == "body"


def test_drop_bodies_does_not_mutate_its_input() -> None:
    snap = _snapshot()
    before = copy.deepcopy(snap)
    drop_bodies(snap)
    assert snap == before


@pytest.mark.parametrize("body", ["", "x" * 1_000_000, "‮\x1b[31m'; DROP--", "\U0001f600" * 50])
def test_the_output_no_longer_depends_on_the_body(body: str) -> None:
    assert drop_bodies(_snapshot(body)) == drop_bodies(_snapshot("other"))
    assert len(json.dumps(drop_bodies(_snapshot(body)))) < 500


def test_an_empty_thread_list_and_no_reviews_pass_through() -> None:
    snap = {"pr": "p", "body": None, "reviews": [], "threads": []}
    assert drop_bodies(snap) == {"pr": "p", "reviews": [], "threads": []}
