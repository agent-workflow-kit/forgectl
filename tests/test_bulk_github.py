"""The GitHub bulk reader (#116): the aliased query, equality with the per-PR snapshot on the
same facts, the page size, and every way a PR is given up. The gateway is real; the process
runner is a router over synthetic answers composed from the per-PR recordings."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from forgectl.backends import github_bulk
from forgectl.backends.bulk_common import BULK_UNAVAILABLE
from forgectl.backends.github_bulk import bulk_query, read_bulk
from forgectl.model import comparable

from .bulk_support import BulkRouter, answer_for, bulk_answer, operation, ref
from .harness.forges import FakeForges
from .test_github_show import http
from .test_github_show import recording as show_recording
from .test_github_snapshot import NOW, REF, Rig, Router, pr_of, standard

pytestmark = pytest.mark.usefixtures("fake_forges")


def rig(forges: FakeForges, router: Any, **kwargs: Any) -> Rig:
    return Rig(forges, router, **kwargs)


# -- equality with the per-PR snapshot ----------------------------------------------------------


def per_pr_without_required(forges: FakeForges) -> dict[str, Any]:
    """The per-PR snapshot of the recordings, with `isRequired` left out of the checks (the one
    thing a bulk query cannot supply)."""
    show = copy.deepcopy(show_recording())
    for check in pr_of(show)["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"][
        "nodes"
    ]:
        check.pop("isRequired", None)
    r = rig(forges, Router(standard(ForgectlShow=show)))
    return r.snapshot()


def test_bulk_item_equals_the_per_pr_snapshot_on_the_same_facts(fake_forges: FakeForges) -> None:
    per_pr = per_pr_without_required(fake_forges)
    r = rig(fake_forges, BulkRouter())
    got = read_bulk(r.env, "github.com", [REF], now=lambda: NOW)
    assert got.unresolved == {}
    bulk = got.snapshots[REF].to_dict()
    assert comparable(bulk) == comparable(per_pr)
    assert bulk["fetched_at"] == "2026-10-02T12:00:05Z"


def test_bulk_leaves_check_required_null_and_snapshot_fills_it(fake_forges: FakeForges) -> None:
    r = rig(fake_forges, BulkRouter())
    bulk = read_bulk(r.env, "github.com", [REF], now=lambda: NOW).snapshots[REF].to_dict()
    assert [c["required"] for c in bulk["ci"]["checks"]] == [None, None]
    per_pr = rig(fake_forges, Router(standard())).snapshot()
    assert sorted(c["required"] for c in per_pr["ci"]["checks"]) == [False, True]


def test_bulk_item_validates_against_the_snapshot_schema(fake_forges: FakeForges) -> None:
    from .harness import schemas

    r = rig(fake_forges, BulkRouter())
    got = read_bulk(r.env, "github.com", [REF], now=lambda: NOW)
    schemas.validate_cli_output("snapshot", got.snapshots[REF].to_dict())


# -- the query ----------------------------------------------------------------------------------


def test_the_query_is_constant_text_and_values_travel_as_variables(fake_forges: FakeForges) -> None:
    router = BulkRouter()
    r = rig(fake_forges, router)
    read_bulk(r.env, "github.com", [ref(7), ref(8, "user-9/other-repo")], now=lambda: NOW)
    (request,) = [r for r in router.requests if operation(r) == "ForgectlBulk"]
    assert request["query"] == bulk_query(2)
    assert request["variables"] == {
        "o0": "user-7",
        "n0": "example-repo",
        "k0": 7,
        "o1": "user-9",
        "n1": "other-repo",
        "k1": 8,
    }
    assert "user-7" not in request["query"] and "example-repo" not in request["query"]
    assert "p0: repository(owner: $o0, name: $n0)" in request["query"]
    assert "p1: repository(owner: $o1, name: $n1)" in request["query"]
    # the expensive and the unavailable fields
    assert "mergeStateStatus" in request["query"] and "isRequired" not in request["query"]


def test_every_connection_of_the_query_is_bounded_and_has_page_info() -> None:
    query = bulk_query(1)
    for name in ("reviews", "reviewThreads", "comments", "reviewRequests", "labels"):
        assert f"{name}(first: " in query
    assert query.count("pageInfo") >= 9


def test_the_pages_hold_page_size_prs_and_answer_every_pr(fake_forges: FakeForges) -> None:
    router = BulkRouter()
    r = rig(fake_forges, router)
    refs = [ref(n) for n in range(1, 61)]
    got = read_bulk(r.env, "github.com", refs, now=lambda: NOW)
    assert router.bulk_sizes == [25, 25, 10]
    assert set(got.snapshots) == set(refs) and got.unresolved == {}
    assert [got.snapshots[x].pr.number for x in refs] == list(range(1, 61))


# -- the page size -------------------------------------------------------------------------------


def fail_above(limit: int) -> Callable[[dict[str, Any]], Any]:
    def answer(request: dict[str, Any]) -> Any:
        count = len(request["variables"]) // 3
        if count > limit:
            return http(502, b"<html>Bad Gateway</html>")
        return bulk_answer(*[request["variables"][f"k{i}"] for i in range(count)])

    return answer


def test_a_502_halves_the_page_and_later_pages_keep_the_smaller_size(
    fake_forges: FakeForges,
) -> None:
    router = BulkRouter(fail_above(13))
    r = rig(fake_forges, router)
    refs = [ref(n) for n in range(1, 41)]
    got = read_bulk(r.env, "github.com", refs, now=lambda: NOW)
    assert router.bulk_sizes == [25, 13, 12, 12, 3]
    assert got.unresolved == {} and len(got.snapshots) == 40


def test_a_502_that_halving_does_not_cure_is_bounded_and_unresolved(
    fake_forges: FakeForges,
) -> None:
    router = BulkRouter(fail_above(0))
    r = rig(fake_forges, router, retries=3)
    got = read_bulk(r.env, "github.com", [ref(n) for n in range(1, 26)], now=lambda: NOW)
    assert router.bulk_sizes == [25, 13, 12]  # no retry loop on a 502
    assert got.snapshots == {}
    assert {u.code for u in got.unresolved.values()} == {BULK_UNAVAILABLE}
    assert len(got.unresolved) == 25


def test_a_single_pr_page_is_asked_twice_then_given_up(fake_forges: FakeForges) -> None:
    router = BulkRouter(fail_above(0))
    got = read_bulk(rig(fake_forges, router).env, "github.com", [REF], now=lambda: NOW)
    assert router.bulk_sizes == [1, 1]
    assert list(got.unresolved) == [REF]


def test_an_answer_that_is_not_json_is_a_failed_page(fake_forges: FakeForges) -> None:
    router = BulkRouter(lambda _r: http(200, b"not json at all"))
    got = read_bulk(rig(fake_forges, router).env, "github.com", [REF], now=lambda: NOW)
    assert got.snapshots == {} and REF in got.unresolved


# -- rate limit ----------------------------------------------------------------------------------


def test_rate_limit_exhaustion_stops_the_host_and_keeps_what_was_read(
    fake_forges: FakeForges,
) -> None:
    def answer(request: dict[str, Any]) -> Any:
        if request["variables"]["k0"] > 25:
            return http(
                429, {"message": "API rate limit exceeded"}, headers={"retry-after": "3600"}
            )
        return bulk_answer(
            *[request["variables"][f"k{i}"] for i in range(len(request["variables"]) // 3)]
        )

    router = BulkRouter(answer)
    r = rig(fake_forges, router, retries=3)
    refs = [ref(n) for n in range(1, 61)]
    got = read_bulk(r.env, "github.com", refs, now=lambda: NOW)
    assert len(got.snapshots) == 25
    assert got.stopped is not None and got.stopped.code == "rate_limited"
    assert {u.code for u in got.unresolved.values()} == {"rate_limited"}
    assert len(got.unresolved) == 35
    assert router.bulk_sizes == [25, 25]  # nothing asked after the limit


def test_the_rate_limit_of_the_answer_is_the_items(fake_forges: FakeForges) -> None:
    router = BulkRouter(lambda r: bulk_answer(7, remaining=17))
    got = read_bulk(rig(fake_forges, router).env, "github.com", [REF], now=lambda: NOW)
    assert got.snapshots[REF].to_dict()["rate_limit"] == {
        "host": "github.com",
        "remaining": 17,
        "reset_at": "2026-10-02T12:00:00Z",
    }


# -- a PR given up --------------------------------------------------------------------------------


def read_with(forges: FakeForges, mutate: Callable[[dict[str, Any]], None]) -> Any:
    def answer(request: dict[str, Any]) -> Any:
        doc = bulk_answer(7)
        mutate(doc["data"]["p0"]["pullRequest"])
        return doc

    return read_bulk(rig(forges, BulkRouter(answer)).env, "github.com", [REF], now=lambda: NOW)


def contexts(pr: dict[str, Any]) -> dict[str, Any]:
    return pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]


CUTS = {
    "check contexts": lambda pr: contexts(pr)["pageInfo"].update(hasNextPage=True),
    "review threads": lambda pr: pr["reviewThreads"]["pageInfo"].update(hasNextPage=True),
    "thread comments": lambda pr: pr["reviewThreads"]["nodes"][0]["comments"]["pageInfo"].update(
        hasNextPage=True
    ),
    "comments": lambda pr: pr["comments"]["pageInfo"].update(hasNextPage=True),
    "reviews": lambda pr: pr["reviews"]["pageInfo"].update(hasNextPage=True),
    "labels": lambda pr: pr["labels"]["pageInfo"].update(hasNextPage=True),
    "review requests": lambda pr: pr["reviewRequests"]["pageInfo"].update(hasNextPage=True),
}


@pytest.mark.parametrize("part", sorted(CUTS))
def test_a_cut_part_gives_the_pr_up_for_the_per_pr_path(fake_forges: FakeForges, part: str) -> None:
    got = read_with(fake_forges, CUTS[part])
    assert got.snapshots == {}
    assert got.unresolved[REF].code == BULK_UNAVAILABLE
    assert part in got.unresolved[REF].message


def test_request_events_cut_before_every_reviewer_is_found_give_the_pr_up(
    fake_forges: FakeForges,
) -> None:
    def cut(pr: dict[str, Any]) -> None:
        pr["requestEvents"]["nodes"] = []
        pr["requestEvents"]["pageInfo"]["hasPreviousPage"] = True

    assert REF in read_with(fake_forges, cut).unresolved


def test_a_force_push_search_cut_without_a_push_to_the_head_gives_the_pr_up(
    fake_forges: FakeForges,
) -> None:
    def cut(pr: dict[str, Any]) -> None:
        pr["pushEvents"]["nodes"] = []
        pr["pushEvents"]["pageInfo"]["hasPreviousPage"] = True

    assert REF in read_with(fake_forges, cut).unresolved


def test_no_force_push_and_nothing_cut_gives_the_head_commit_date(fake_forges: FakeForges) -> None:
    def none(pr: dict[str, Any]) -> None:
        pr["pushEvents"]["nodes"] = []

    snap = read_with(fake_forges, none).snapshots[REF].to_dict()
    assert snap["head"]["pushed_at"] == "2026-10-02T07:30:00Z"
    assert snap["head"]["pushed_at_source"] == "commit_date"


def test_a_head_that_is_not_the_last_commit_gives_the_pr_up(fake_forges: FakeForges) -> None:
    def moved(pr: dict[str, Any]) -> None:
        pr["commits"]["nodes"][0]["commit"]["oid"] = "1" * 40

    assert REF in read_with(fake_forges, moved).unresolved


@pytest.mark.parametrize("status", ["UNKNOWN", None])
def test_mergeability_still_computing_gives_the_pr_up(
    fake_forges: FakeForges, status: str | None
) -> None:
    got = read_with(fake_forges, lambda pr: pr.update(mergeStateStatus=status))
    assert "mergeability" in got.unresolved[REF].message


def test_a_requested_team_the_token_cannot_read_gives_the_pr_up(fake_forges: FakeForges) -> None:
    def hidden(pr: dict[str, Any]) -> None:
        pr["reviewRequests"]["nodes"][1]["requestedReviewer"] = None

    assert REF in read_with(fake_forges, hidden).unresolved


@pytest.mark.parametrize("missing", ["reviews", "reviewThreads", "comments", "commits", "labels"])
def test_a_missing_field_gives_the_pr_up_and_never_a_crash(
    fake_forges: FakeForges, missing: str
) -> None:
    got = read_with(fake_forges, lambda pr: pr.pop(missing))
    assert got.snapshots == {} and got.unresolved[REF].code == BULK_UNAVAILABLE


def test_a_wrong_type_gives_the_pr_up(fake_forges: FakeForges) -> None:
    assert REF in read_with(fake_forges, lambda pr: pr.update(reviews="nothing")).unresolved


def test_one_bad_pr_does_not_take_the_others_of_its_page(fake_forges: FakeForges) -> None:
    def answer(request: dict[str, Any]) -> Any:
        doc = bulk_answer(7, 8, 9)
        doc["data"]["p1"]["pullRequest"]["comments"] = None
        return doc

    router = BulkRouter(answer)
    refs = [ref(7), ref(8), ref(9)]
    got = read_bulk(rig(fake_forges, router).env, "github.com", refs, now=lambda: NOW)
    assert set(got.snapshots) == {ref(7), ref(9)} and set(got.unresolved) == {ref(8)}
    assert router.bulk_sizes == [3]


def test_a_missing_pr_is_not_found_and_its_neighbours_are_read(fake_forges: FakeForges) -> None:
    def answer(request: dict[str, Any]) -> Any:
        doc = bulk_answer(7, 8)
        doc["data"]["p1"] = None
        doc["errors"] = [
            {
                "type": "NOT_FOUND",
                "path": ["p1"],
                "message": "Could not resolve to a Repository with the name 'x/y'.",
            }
        ]
        return doc

    router = BulkRouter(answer)
    got = read_bulk(rig(fake_forges, router).env, "github.com", [ref(7), ref(8)], now=lambda: NOW)
    assert set(got.snapshots) == {ref(7)}
    assert got.unresolved[ref(8)].code == "not_found"
    assert router.bulk_sizes == [2]


def test_an_error_on_one_alias_gives_that_pr_up_only(fake_forges: FakeForges) -> None:
    def answer(request: dict[str, Any]) -> Any:
        doc = bulk_answer(7, 8)
        doc["errors"] = [
            {"type": "FORBIDDEN", "path": ["p1", "pullRequest", "reviewRequests"], "message": "x"}
        ]
        return doc

    got = read_bulk(
        rig(fake_forges, BulkRouter(answer)).env, "github.com", [ref(7), ref(8)], now=lambda: NOW
    )
    assert set(got.snapshots) == {ref(7)} and ref(8) in got.unresolved


def test_an_error_that_names_no_pr_fails_the_page_and_halves_it(fake_forges: FakeForges) -> None:
    calls: list[int] = []

    def answer(request: dict[str, Any]) -> Any:
        count = len(request["variables"]) // 3
        calls.append(count)
        if count > 1:
            return {
                "data": None,
                "errors": [{"message": "Something went wrong while executing your query."}],
            }
        return bulk_answer(*[request["variables"][f"k{i}"] for i in range(count)])

    got = read_bulk(
        rig(fake_forges, BulkRouter(answer)).env, "github.com", [ref(7), ref(8)], now=lambda: NOW
    )
    assert calls == [2, 1, 1]
    assert set(got.snapshots) == {ref(7), ref(8)}


# -- hostile content ------------------------------------------------------------------------------


def test_untrusted_text_is_sanitized_as_in_the_per_pr_path(fake_forges: FakeForges) -> None:
    def hostile(pr: dict[str, Any]) -> None:
        pr["title"] = "Fix ‮evil‬\x1b[31m title\x00 \U0001f600 é"
        pr["body"] = "body\x07 with <script>alert(1)</script> and ```;rm -rf /"
        pr["comments"]["nodes"][0]["body"] = " line  sep ${jndi:ldap://x}"

    snap = read_with(fake_forges, hostile).snapshots[REF].to_dict()
    assert "‮" not in snap["title"] and "\x1b" not in snap["title"] and "\x00" not in snap["title"]
    assert "\U0001f600" in snap["title"] and "é" in snap["title"]
    assert "\x07" not in snap["body"]
    json.dumps(snap, ensure_ascii=True)


def test_an_answer_for_another_host_ends_the_run_as_wrong_host(fake_forges: FakeForges) -> None:
    def foreign(pr: dict[str, Any]) -> None:
        pr["url"] = "https://evil.example.test/user-7/example-repo/pull/7"

    got = read_with(fake_forges, foreign)
    assert got.stopped is not None and got.stopped.code == "wrong_host_response"
    assert got.snapshots == {}


def test_another_pr_number_in_the_answer_gives_the_pr_up(fake_forges: FakeForges) -> None:
    def other(pr: dict[str, Any]) -> None:
        pr["number"] = 8
        pr["url"] = "https://github.com/user-7/example-repo/pull/8"

    assert REF in read_with(fake_forges, other).unresolved


def test_an_empty_page_asks_nothing(fake_forges: FakeForges) -> None:
    router = BulkRouter()
    got = read_bulk(rig(fake_forges, router).env, "github.com", [], now=lambda: NOW)
    assert got.snapshots == {} and router.requests == []


def test_the_operation_is_named_for_the_router() -> None:
    assert operation({"query": bulk_query(1)}) == "ForgectlBulk"
    assert github_bulk.PAGE_SIZE == 25 and UTC and datetime


def down_result() -> Any:
    from forgectl.gateway import ProcessResult

    return ProcessResult(pid=1, pgid=1, returncode=1, stdout=b"", stderr=b"dial tcp: no such host")


def test_one_transient_network_failure_halves_the_page_and_every_pr_is_read(
    fake_forges: FakeForges,
) -> None:
    failed: list[int] = []

    def answer(request: dict[str, Any]) -> Any:
        if not failed:
            failed.append(1)
            return down_result()
        return answer_for(request)

    router = BulkRouter(answer)
    r = rig(fake_forges, router, retries=0)
    refs = [ref(n) for n in range(1, 61)]
    got = read_bulk(r.env, "github.com", refs, now=lambda: NOW)
    assert got.stopped is None and got.unresolved == {}
    assert len(got.snapshots) == 60
    assert router.bulk_sizes[:3] == [25, 13, 12]


def test_repeated_network_failures_end_the_host_run_after_three_in_a_row(
    fake_forges: FakeForges,
) -> None:
    router = BulkRouter(lambda _r: down_result())
    r = rig(fake_forges, router, retries=0)
    refs = [ref(n) for n in range(1, 61)]
    got = read_bulk(r.env, "github.com", refs, now=lambda: NOW)
    assert router.bulk_sizes == [25, 13, 12]
    assert got.stopped is not None and got.stopped.code == "network_error"
    assert len(got.unresolved) == 60 and got.snapshots == {}
    assert {u.code for u in got.unresolved.values()} == {"network_error", BULK_UNAVAILABLE}


def test_a_network_failure_with_the_budget_spent_ends_the_host_run_at_once(
    fake_forges: FakeForges,
) -> None:
    holder: list[Rig] = []

    def answer(_r: dict[str, Any]) -> Any:
        holder[0].env.gateway.budget.spent = holder[0].env.gateway.budget.total
        return down_result()

    router = BulkRouter(answer)
    r = rig(fake_forges, router, retries=0)
    holder.append(r)
    got = read_bulk(r.env, "github.com", [ref(n) for n in range(1, 61)], now=lambda: NOW)
    assert router.bulk_sizes == [25]
    assert got.stopped is not None and got.stopped.code == "network_error"
    assert len(got.unresolved) == 60


# -- refs that cannot be asked, and answers that are not shaped as asked -------------------------


def test_refs_that_cannot_be_asked_are_not_found_and_never_poison_a_page(
    fake_forges: FakeForges,
) -> None:
    router = BulkRouter()
    odd = ref(3, "a/b/c")
    huge = ref(2**31)
    refs = [ref(7), odd, huge, ref(8)]
    got = read_bulk(rig(fake_forges, router).env, "github.com", refs, now=lambda: NOW)
    assert router.bulk_sizes == [2]
    assert set(got.snapshots) == {ref(7), ref(8)}
    assert got.unresolved[odd].code == "not_found" and got.unresolved[huge].code == "not_found"


@pytest.mark.parametrize("head", ["p\u00b2", "p" + "9" * 5000, "p", "p-1", "p٣"])
def test_an_error_path_that_is_not_an_alias_fails_the_page_and_never_crashes(
    fake_forges: FakeForges, head: str
) -> None:
    def answer(request: dict[str, Any]) -> Any:
        doc = answer_for(request)
        doc["errors"] = [{"type": "FORBIDDEN", "path": [head], "message": "x"}]
        return doc

    got = read_bulk(
        rig(fake_forges, BulkRouter(answer)).env, "github.com", [ref(7), ref(8)], now=lambda: NOW
    )
    assert got.snapshots == {} and set(got.unresolved) == {ref(7), ref(8)}


@pytest.mark.parametrize("kind", [["RATE_LIMITED"], {"a": 1}, 7, None])
def test_an_error_type_that_is_not_a_string_fails_the_page_and_never_crashes(
    fake_forges: FakeForges, kind: Any
) -> None:
    def answer(request: dict[str, Any]) -> Any:
        doc = answer_for(request)
        doc["errors"] = [{"type": kind, "message": "x"}]
        return doc

    got = read_bulk(rig(fake_forges, BulkRouter(answer)).env, "github.com", [REF], now=lambda: NOW)
    assert got.snapshots == {} and REF in got.unresolved


def test_an_omitted_alias_is_not_a_missing_pr(fake_forges: FakeForges) -> None:
    def answer(request: dict[str, Any]) -> Any:
        doc = bulk_answer(7, 8)
        del doc["data"]["p1"]
        return doc

    got = read_bulk(
        rig(fake_forges, BulkRouter(answer)).env, "github.com", [ref(7), ref(8)], now=lambda: NOW
    )
    assert set(got.snapshots) == {ref(7)}
    assert got.unresolved[ref(8)].code == BULK_UNAVAILABLE


def test_a_null_alias_without_errors_is_still_not_found(fake_forges: FakeForges) -> None:
    def answer(request: dict[str, Any]) -> Any:
        doc = bulk_answer(7, 8)
        doc["data"]["p1"] = None
        return doc

    got = read_bulk(
        rig(fake_forges, BulkRouter(answer)).env, "github.com", [ref(7), ref(8)], now=lambda: NOW
    )
    assert got.unresolved[ref(8)].code == "not_found"


@pytest.mark.parametrize("field", ["additions", "deletions", "changedFiles", "author"])
def test_an_absent_pr_field_gives_the_pr_up_but_a_null_author_is_fine(
    fake_forges: FakeForges, field: str
) -> None:
    got = read_with(fake_forges, lambda pr: pr.pop(field))
    assert got.snapshots == {} and got.unresolved[REF].code == BULK_UNAVAILABLE
    assert field in got.unresolved[REF].message


def test_an_absent_repository_field_gives_the_pr_up(fake_forges: FakeForges) -> None:
    def answer(request: dict[str, Any]) -> Any:
        doc = bulk_answer(7)
        del doc["data"]["p0"]["mergeCommitAllowed"]
        return doc

    got = read_bulk(rig(fake_forges, BulkRouter(answer)).env, "github.com", [REF], now=lambda: NOW)
    assert got.snapshots == {} and got.unresolved[REF].code == BULK_UNAVAILABLE


def test_a_null_author_is_a_ghost_and_still_read(fake_forges: FakeForges) -> None:
    got = read_with(fake_forges, lambda pr: pr.update(author=None))
    assert REF in got.snapshots
