"""GitHub `snapshot`, `comments`, `reviews` and `ci` (#12) and the push time of the head (#56).

The gateway is real; its process runner is a router that answers each constant query (told apart
by its operation name) and the REST call from scripted documents. The recordings under
tests/recordings/github/snapshot are hand-built from GitHub's documented GraphQL and REST shapes,
scrubbed of real names. Every assumption about GitHub's real behavior is UNVERIFIED until the live
suite (#31) re-checks it; see the report of #12 for the list.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from forgectl.backends import github, github_snapshot
from forgectl.backends.base import Backend
from forgectl.backends.github_snapshot import (
    PARTS_QUERY,
    PUSH_QUERY,
    REQUESTS_QUERY,
    REQUESTS_QUERY_NO_TEAMS,
)
from forgectl.config import Host, LoadedConfig
from forgectl.errors import ForgectlError
from forgectl.gateway import Gateway, ProcessResult
from forgectl.hosts import Hosts
from forgectl.model import comparable
from forgectl.reads import ReadEnv
from forgectl.sanitize import sanitize_line, sanitize_text

from .harness import golden, schemas
from .harness.fixtures import forgectl_env
from .harness.forges import FakeForges
from .test_github_show import HEAD, OTHER, REF, TOKEN, Slept, http
from .test_github_show import recording as show_recording

RECORDINGS = Path(__file__).parent / "recordings" / "github" / "snapshot"
NOW = datetime(2026, 10, 2, 12, 0, 5, tzinfo=UTC)
SCOPES = {"x-oauth-scopes": "repo, read:org", "x-ratelimit-remaining": "4990"}


def rec(name: str) -> Any:
    return json.loads((RECORDINGS / f"{name}.json").read_text(encoding="utf-8"))


def pr_of(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["data"]["repository"]["pullRequest"]


def operation(request: Mapping[str, Any]) -> str:
    match = re.search(r"query\s+(\w+)", request.get("query", ""))
    return match.group(1) if match else "?"


class Router:
    """A process runner. ``answers`` maps an operation name (or ``"rest"``) to a document, a
    ready ``bytes`` answer, or a list of them used in turn (the last repeats); a callable gets
    the request body."""

    def __init__(self, answers: Mapping[str, Any]) -> None:
        self.answers = {k: (list(v) if isinstance(v, list) else [v]) for k, v in answers.items()}
        self.requests: list[dict[str, Any]] = []
        self.argvs: list[list[str]] = []
        self.ops: list[str] = []

    def __call__(self, argv: Sequence[str], *, stdin: bytes | None, **_: Any) -> ProcessResult:
        self.argvs.append(list(argv))
        request = json.loads(stdin) if stdin else {}
        self.requests.append(request)
        op = operation(request) if request else "rest"
        self.ops.append(op)
        assert op in self.answers, f"no answer scripted for {op}"
        queue = self.answers[op]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, ProcessResult):
            return answer
        raw: bytes = answer if isinstance(answer, bytes) else http(200, answer)
        code = 0 if raw.startswith(b"HTTP/2.0 2") and b'"errors"' not in raw else 1
        return ProcessResult(pid=1, pgid=1, returncode=code, stdout=raw, stderr=b"")


def rest_answer(doc: Any = None, **headers: str) -> bytes:
    return http(
        200, rec("requested-reviewers") if doc is None else doc, headers={**SCOPES, **headers}
    )


def standard(**overrides: Any) -> dict[str, Any]:
    answers: dict[str, Any] = {
        "ForgectlShow": show_recording(),
        "ForgectlParts": rec("parts"),
        "ForgectlRequests": rec("requests"),
        "ForgectlPush": rec("push"),
        "ForgectlReviewsPage": {"data": {"node": {"reviews": pr_of(rec("parts"))["reviews"]}}},
        "rest": rest_answer(),
    }
    answers.update(overrides)
    return answers


class Rig:
    def __init__(self, forges: FakeForges, router: Router, *, retries: int = 0) -> None:
        config = LoadedConfig.defaults()
        config = LoadedConfig(
            config=config.config.model_copy(
                update={"hosts": {"github.com": Host(backend="github", token_env="SNAP_TOKEN")}}
            ),
            config_hash="x",
            config_path=None,
        )
        self.router = router
        self.slept = Slept()
        gateway = Gateway(
            config,
            timeout=60.0,
            environ=forgectl_env(forges, {"SNAP_TOKEN": TOKEN}),
            runner=router,  # type: ignore[arg-type]
        )
        self.warnings: list[str] = []
        self.env = ReadEnv(
            gateway=gateway,
            hosts=Hosts(gateway),
            retries=retries,
            warn=self.warnings.append,
            sleep=self.slept.sleep,
            clock=self.slept,
            jitter=lambda: 0.0,
        )
        self.backend = github.GitHubBackend(self.env)

    def snapshot(self) -> dict[str, Any]:
        snap = github_snapshot.snapshot(self.env, REF, self.backend.get_pr, now=lambda: NOW)
        return snap.to_dict()


@pytest.fixture
def make(fake_forges: FakeForges) -> Callable[..., Rig]:
    def build(answers: Mapping[str, Any] | None = None, **kwargs: Any) -> Rig:
        return Rig(fake_forges, Router(standard() if answers is None else answers), **kwargs)

    return build


def snap(make: Callable[..., Rig], **overrides: Any) -> dict[str, Any]:
    return make(standard(**overrides)).snapshot()


def failure(make: Callable[..., Rig], **overrides: Any) -> ForgectlError:
    with pytest.raises(ForgectlError) as caught:
        make(standard(**overrides)).snapshot()
    return caught.value


# -- the golden output and the shape of the reading -----------------------------------------


def test_the_adapter_still_satisfies_the_backend_protocol(make: Callable[..., Rig]) -> None:
    assert isinstance(make().backend, Backend)


def test_golden_snapshot(make: Callable[..., Rig]) -> None:
    golden.assert_golden("snapshot/github-open", snap(make), schema_id="forgectl/cli/snapshot.json")


def test_the_snapshot_is_complete_and_names_no_incomplete_part(make: Callable[..., Rig]) -> None:
    out = snap(make)
    assert out["complete"] is True and out["incomplete"] == []
    assert out["fetched_at"] == "2026-10-02T12:00:05Z"
    assert out["pr"] == "github.com/example-org/example-repo#7"


def test_every_query_is_a_constant_text_and_values_travel_as_variables(
    make: Callable[..., Rig],
) -> None:
    rig = make()
    rig.snapshot()
    assert rig.router.ops == [
        "ForgectlShow",
        "ForgectlParts",
        "ForgectlRequests",
        "rest",
        "ForgectlPush",
    ]
    texts = {r["query"] for r in rig.router.requests if r}
    assert PARTS_QUERY in texts and REQUESTS_QUERY in texts and PUSH_QUERY in texts
    for request in rig.router.requests:
        assert "example-org" not in request.get("query", "")
    push = next(r for r in rig.router.requests if operation(r) == "ForgectlPush")
    assert push["variables"]["head"] == HEAD
    rest_argv = rig.router.argvs[3]
    assert (
        rest_argv[-1] == "repos/example-org/example-repo/pulls/7/requested_reviewers?per_page=100"
    )
    assert rest_argv[rest_argv.index("-X") + 1] == "GET"


def test_the_untrusted_token_never_reaches_the_argv(make: Callable[..., Rig]) -> None:
    rig = make()
    rig.snapshot()
    assert all(TOKEN not in arg for argv in rig.router.argvs for arg in argv)


def test_two_readings_of_an_unchanged_pr_compare_equal(make: Callable[..., Rig]) -> None:
    a, b = snap(make), snap(make)
    b["fetched_at"] = "2030-01-01T00:00:00Z"
    b["rate_limit"] = {"host": None, "remaining": 1, "reset_at": None}
    assert comparable(a) == comparable(b)


# -- rate limit ----------------------------------------------------------------------------


def test_rate_limit_is_the_lowest_remaining_across_the_apis(make: Callable[..., Rig]) -> None:
    out = snap(
        make, rest=rest_answer(**{"x-ratelimit-remaining": "17", "x-ratelimit-reset": "1790000000"})
    )
    assert out["rate_limit"] == {
        "host": "github.com",
        "remaining": 17,
        "reset_at": "2026-09-21T14:13:20Z",
    }


def test_rate_limit_from_graphql_when_it_is_lower(make: Callable[..., Rig]) -> None:
    out = snap(make)
    # parts 4800, requests 4799, push 4798, rest 4990
    assert out["rate_limit"] == {
        "host": "github.com",
        "remaining": 4798,
        "reset_at": "2026-10-02T12:00:00Z",
    }


def test_rate_limit_is_null_where_no_header_or_field_says(make: Callable[..., Rig]) -> None:
    def bare(name: str) -> dict[str, Any]:
        doc = rec(name)
        del doc["data"]["rateLimit"]
        return doc

    out = snap(
        make,
        ForgectlParts=bare("parts"),
        ForgectlRequests=bare("requests"),
        ForgectlPush=bare("push"),
        rest=http(200, rec("requested-reviewers"), headers={"x-oauth-scopes": "repo"}),
    )
    assert out["rate_limit"] == {"host": "github.com", "remaining": None, "reset_at": None}


@pytest.mark.parametrize("value", ["-1", "abc", "1e3", "", "99999999999999999999"])
def test_a_hostile_rate_limit_header_is_ignored(make: Callable[..., Rig], value: str) -> None:
    out = snap(make, rest=rest_answer(**{"x-ratelimit-remaining": value}))
    assert out["rate_limit"]["remaining"] == 4798


# -- review requests ------------------------------------------------------------------------


def requests_by(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["reviewer"]["login"]: r for r in out["review_requests"]}


def test_request_instances_follow_the_latest_event_of_each_reviewer(
    make: Callable[..., Rig],
) -> None:
    by = requests_by(snap(make))
    # user-5 was requested twice: the instance is the newest event's node id
    assert by["user-5"]["instance"] == "RRE_kwDOexample0003"
    assert by["user-5"]["requested_at"] == "2026-10-02T07:00:00Z"
    assert by["user-8"]["instance"] == "RRE_kwDOexample0002"
    assert by["user-8"]["reviewer"] == {
        "login": "user-8",
        "id": "77",
        "kind": "team",
    }
    assert all(r["state"] == "outstanding" for r in by.values())


def test_a_reviewer_with_no_event_has_a_null_instance(make: Callable[..., Rig]) -> None:
    by = requests_by(snap(make))
    assert by["user-7"]["instance"] is None and by["user-7"]["requested_at"] is None


def test_a_re_request_gives_a_new_instance_and_a_push_does_not(make: Callable[..., Rig]) -> None:
    first = requests_by(snap(make))["user-5"]["instance"]
    doc = rec("requests")
    pr_of(doc)["timelineItems"]["nodes"].append(
        {
            "__typename": "ReviewRequestedEvent",
            "id": "RRE_kwDOexample0009",
            "createdAt": "2026-10-02T09:00:00Z",
            "requestedReviewer": {"__typename": "User", "login": "user-5", "databaseId": 1005},
        }
    )
    again = requests_by(snap(make, ForgectlRequests=doc))["user-5"]["instance"]
    assert (first, again) == ("RRE_kwDOexample0003", "RRE_kwDOexample0009")
    # a push changes the head, not the instance
    pushed = snap(make, ForgectlPush=rec("push"))
    assert requests_by(pushed)["user-5"]["instance"] == first


def test_code_owner_comes_from_the_request(make: Callable[..., Rig]) -> None:
    by = requests_by(snap(make))
    assert (by["user-5"]["code_owner"], by["user-7"]["code_owner"]) == (True, False)


def test_teams_are_visible_when_scopes_are_reported_and_the_counts_agree(
    make: Callable[..., Rig],
) -> None:
    assert snap(make)["teams_visible"] is True


def test_a_token_that_reports_no_scopes_gives_teams_visible_false(
    make: Callable[..., Rig],
) -> None:
    out = snap(make, rest=http(200, rec("requested-reviewers"), headers={}))
    assert out["teams_visible"] is False
    assert out["complete"] is True  # false visibility is not an incomplete part


def test_a_hidden_team_is_added_from_requested_reviewers_and_teams_visible_is_false(
    make: Callable[..., Rig],
) -> None:
    doc = rec("requests")
    nodes = pr_of(doc)["reviewRequests"]["nodes"]
    nodes[:] = [n for n in nodes if n["requestedReviewer"]["__typename"] != "Team"]
    out = snap(make, ForgectlRequests=doc)
    assert out["teams_visible"] is False
    team = requests_by(out)["user-8"]
    assert team["reviewer"]["kind"] == "team" and team["instance"] == "RRE_kwDOexample0002"


def test_a_null_requested_reviewer_is_a_hidden_team(make: Callable[..., Rig]) -> None:
    doc = rec("requests")
    pr_of(doc)["reviewRequests"]["nodes"][1]["requestedReviewer"] = None
    out = snap(make, ForgectlRequests=doc)
    assert out["teams_visible"] is False
    assert "user-8" in requests_by(out)  # still named, from the REST list


def scope_error() -> bytes:
    body = {
        "data": rec("requests")["data"],
        "errors": [
            {
                "type": "INSUFFICIENT_SCOPES",
                "path": [
                    "repository",
                    "pullRequest",
                    "reviewRequests",
                    "nodes",
                    1,
                    "requestedReviewer",
                ],
                "message": "Your token has not been granted the required scopes: read:org",
            }
        ],
    }
    return http(200, body)


def test_a_read_org_less_token_retries_a_constant_query_without_team_fields(
    make: Callable[..., Rig],
) -> None:
    stub = rec("requests")
    for node in pr_of(stub)["reviewRequests"]["nodes"] + pr_of(stub)["timelineItems"]["nodes"]:
        holder = node
        reviewer = holder["requestedReviewer"]
        if reviewer["__typename"] == "Team":
            holder["requestedReviewer"] = {"__typename": "Team"}
    rig = make(standard(ForgectlRequests=[scope_error(), stub]))
    out = rig.snapshot()
    sent = [r["query"] for r in rig.router.requests if operation(r) == "ForgectlRequests"]
    assert sent == [REQUESTS_QUERY, REQUESTS_QUERY_NO_TEAMS]
    assert "slug" not in REQUESTS_QUERY_NO_TEAMS and "... on Team" not in REQUESTS_QUERY_NO_TEAMS
    assert out["teams_visible"] is False
    assert "user-8" in requests_by(out)  # from REST
    schemas.validate_cli_output("snapshot", out)


def test_a_forbidden_team_error_also_retries_without_team_fields(
    make: Callable[..., Rig],
) -> None:
    body = {
        "data": rec("requests")["data"],
        "errors": [{"type": "FORBIDDEN", "message": "Resource not accessible: team"}],
    }
    rig = make(standard(ForgectlRequests=[http(200, body), rec("requests")]))
    assert rig.snapshot()["teams_visible"] is False
    assert rig.router.ops.count("ForgectlRequests") == 2


def test_another_graphql_error_on_the_requests_is_a_failed_read(make: Callable[..., Rig]) -> None:
    body = {"data": rec("requests")["data"], "errors": [{"type": "SOMETHING", "message": "boom"}]}
    error = failure(make, ForgectlRequests=http(200, body))
    assert error.code == "host_rejected"


def test_partial_data_with_a_scope_error_is_not_trusted_without_the_retry(
    make: Callable[..., Rig],
) -> None:
    # the answer after the retry still carries errors: a failed read, never complete
    error = failure(make, ForgectlRequests=scope_error())
    assert error.code == "host_rejected"


def test_requested_reviewers_a_user_missing_from_the_graph_is_added(
    make: Callable[..., Rig],
) -> None:
    doc = rec("requested-reviewers")
    doc["users"].append({"login": "dependabot[bot]", "id": 4000, "type": "Bot"})
    out = snap(make, rest=rest_answer(doc))
    assert "dependabot[bot]" in requests_by(out)
    assert out["teams_visible"] is False  # the counts differ: absence means nothing


def test_a_forbidden_requested_reviewers_read_hides_teams_but_does_not_fail(
    make: Callable[..., Rig],
) -> None:
    out = snap(make, rest=http(403, {"message": "Resource not accessible by integration"}))
    assert out["teams_visible"] is False


def test_a_request_that_cannot_be_named_is_reported_incomplete(make: Callable[..., Rig]) -> None:
    doc = rec("requests")
    pr_of(doc)["reviewRequests"]["nodes"][0]["requestedReviewer"]["login"] = "bad login"
    out = snap(make, ForgectlRequests=doc)
    assert out["incomplete"] == ["review_requests"] and out["complete"] is False
    assert "bad login" not in json.dumps(out)


def test_more_than_a_page_of_requests_is_incomplete(make: Callable[..., Rig]) -> None:
    doc = rec("requests")
    pr_of(doc)["reviewRequests"]["pageInfo"]["hasNextPage"] = True
    assert snap(make, ForgectlRequests=doc)["incomplete"] == ["review_requests"]


def test_events_are_paged_back_until_every_instance_is_found(make: Callable[..., Rig]) -> None:
    doc = rec("requests")
    older = doc["data"]["repository"]["pullRequest"]["timelineItems"]
    keep = [n for n in older["nodes"] if n["requestedReviewer"].get("login") != "user-5"]
    older["nodes"], older["pageInfo"] = (
        keep,
        {"hasPreviousPage": True, "startCursor": "Y3Vyc29yOjk="},
    )
    page = {
        "data": {
            "rateLimit": {"remaining": 4700, "resetAt": "2026-10-02T12:00:00Z"},
            "node": {
                "timelineItems": {
                    "pageInfo": {"hasPreviousPage": False, "startCursor": "x"},
                    "nodes": [
                        {
                            "__typename": "ReviewRequestedEvent",
                            "id": "RRE_kwDOexample0005",
                            "createdAt": "2026-09-30T10:00:00Z",
                            "requestedReviewer": {
                                "__typename": "User",
                                "login": "user-5",
                                "databaseId": 1005,
                            },
                        }
                    ],
                }
            },
        }
    }
    rig = make(standard(ForgectlRequests=doc, ForgectlRequestEvents=page))
    out = rig.snapshot()
    assert requests_by(out)["user-5"]["instance"] == "RRE_kwDOexample0005"
    ask = next(r for r in rig.router.requests if operation(r) == "ForgectlRequestEvents")
    assert ask["variables"] == {"id": "PR_kwDOexample0007", "before": "Y3Vyc29yOjk="}


def test_events_that_never_end_make_the_requests_incomplete_not_a_loop(
    make: Callable[..., Rig],
) -> None:
    doc = rec("requests")
    older = pr_of(doc)["timelineItems"]
    older["nodes"], older["pageInfo"] = [], {"hasPreviousPage": True, "startCursor": "Y3Vyc29yOjk="}
    page = {"data": {"node": {"timelineItems": copy.deepcopy(older)}}}
    rig = make(standard(ForgectlRequests=doc, ForgectlRequestEvents=page))
    out = rig.snapshot()
    assert out["incomplete"] == ["review_requests"]
    assert rig.router.ops.count("ForgectlRequestEvents") <= github.MAX_PAGES


def test_a_bot_request_is_named_in_the_rest_form(make: Callable[..., Rig]) -> None:
    doc = rec("requests")
    pr_of(doc)["reviewRequests"]["nodes"][2]["requestedReviewer"] = {
        "__typename": "Bot",
        "login": "dependabot",
        "databaseId": 1007,
    }
    rest = rec("requested-reviewers")
    rest["users"][1] = {"login": "dependabot[bot]", "id": 1007, "type": "Bot"}
    out = snap(make, ForgectlRequests=doc, rest=rest_answer(rest))
    assert "dependabot[bot]" in requests_by(out)
    assert out["teams_visible"] is True  # one account, three spellings


# -- reviews --------------------------------------------------------------------------------


def test_reviews_lists_every_non_pending_review_with_id_and_body(make: Callable[..., Rig]) -> None:
    out = snap(make)
    assert [r["id"] for r in out["reviews"]] == [
        "gh:r:PRR_kwDOexample0001",
        "gh:r:PRR_kwDOexample0002",
    ]
    assert out["reviews"][0]["body"] == "Looks good."
    assert out["reviews"][0]["commit_sha"] == HEAD
    assert [r["state"] for r in out["reviews"]] == ["approved", "commented"]


def test_latest_by_reviewer_is_computed_here_with_can_push(make: Callable[..., Rig]) -> None:
    out = snap(make)
    assert out["latest_by_reviewer"] == [
        {
            "login": "user-2",
            "id": "1002",
            "state": "approved",
            "commit_sha": HEAD,
            "at": "2026-10-02T09:00:00Z",
            "can_push": True,
        }
    ]
    assert out["approvals"] == 1


def test_a_deleted_reviewer_has_a_null_author_and_no_latest_entry(make: Callable[..., Rig]) -> None:
    doc = rec("parts")
    pr_of(doc)["reviews"]["nodes"][0]["author"] = None
    pr_doc = show_recording()
    doc_show = pr_of(pr_doc)
    doc_show["reviews"]["nodes"][0]["author"] = None
    doc_show["reviewDecision"] = None
    out = snap(make, ForgectlParts=doc, ForgectlShow=pr_doc)
    assert out["reviews"][0]["author"] == {"login": None, "id": None, "is_bot": None}
    assert out["latest_by_reviewer"] == []


def test_an_unknown_review_state_is_reported_incomplete_and_never_dropped_silently(
    make: Callable[..., Rig],
) -> None:
    doc = rec("parts")
    pr_of(doc)["reviews"]["nodes"][1]["state"] = "FUTURE_STATE"
    out = snap(make, ForgectlParts=doc)
    assert out["incomplete"] == ["reviews"] and out["complete"] is False
    assert [r["id"] for r in out["reviews"]] == ["gh:r:PRR_kwDOexample0001"]


def test_reviews_are_paged_and_a_page_cap_is_incomplete(make: Callable[..., Rig]) -> None:
    doc = rec("parts")
    pr_of(doc)["reviews"]["pageInfo"] = {"hasNextPage": True, "endCursor": "Y3Vyc29yOjI="}
    later = {
        "data": {
            "node": {
                "reviews": {
                    "pageInfo": {"hasNextPage": False, "endCursor": "x"},
                    "nodes": [
                        {
                            "id": "PRR_kwDOexample0010",
                            "state": "CHANGES_REQUESTED",
                            "submittedAt": "2026-10-03T09:00:00Z",
                            "body": "No.",
                            "authorCanPushToRepository": False,
                            "author": {"__typename": "User", "login": "user-2", "databaseId": 1002},
                            "commit": {"oid": HEAD},
                        }
                    ],
                }
            }
        }
    }
    show_doc = show_recording()
    pr_of(show_doc)["reviews"]["nodes"].append(
        {
            "id": "PRR_kwDOexample0010",
            "state": "CHANGES_REQUESTED",
            "submittedAt": "2026-10-03T09:00:00Z",
            "authorCanPushToRepository": False,
            "author": {"__typename": "User", "login": "user-2", "databaseId": 1002},
            "commit": {"oid": HEAD},
        }
    )
    pr_of(show_doc)["reviewDecision"] = "CHANGES_REQUESTED"
    out = snap(make, ForgectlParts=doc, ForgectlReviewsPage=later, ForgectlShow=show_doc)
    assert out["complete"] is True
    assert out["latest_by_reviewer"][0]["state"] == "changes_requested"
    # never-ending pages stop at the cap
    forever = {"data": {"node": {"reviews": copy.deepcopy(pr_of(doc)["reviews"])}}}
    capped = snap(make, ForgectlParts=doc, ForgectlReviewsPage=forever)
    assert capped["incomplete"] == ["reviews"]


def test_a_review_landing_between_the_reads_repeats_the_reading_once_then_fails(
    make: Callable[..., Rig],
) -> None:
    doc = rec("parts")
    pr_of(doc)["reviews"]["nodes"][0]["state"] = "DISMISSED"  # show still counts one approval
    rig = make(standard(ForgectlParts=doc))  # show still says one approval and no change request
    with pytest.raises(ForgectlError) as caught:
        rig.snapshot()
    assert caught.value.code == "upstream_error"
    assert rig.router.ops.count("ForgectlShow") == 2


def test_a_head_that_moved_repeats_the_reading_once(make: Callable[..., Rig]) -> None:
    moved = rec("parts")
    pr_of(moved)["headRefOid"] = OTHER
    rig = make(standard(ForgectlParts=[moved, rec("parts")]))
    assert rig.snapshot()["head"]["sha"] == HEAD
    assert rig.router.ops.count("ForgectlShow") == 2


def test_a_head_that_keeps_moving_is_upstream_error(make: Callable[..., Rig]) -> None:
    moved = rec("parts")
    pr_of(moved)["headRefOid"] = OTHER
    assert failure(make, ForgectlParts=moved).code == "upstream_error"


@pytest.mark.parametrize("query", ["ForgectlRequests", "ForgectlPush"])
def test_the_other_reads_name_their_head_too(make: Callable[..., Rig], query: str) -> None:
    moved = rec("requests" if query == "ForgectlRequests" else "push")
    pr_of(moved)["headRefOid"] = OTHER
    assert failure(make, **{query: moved}).code == "upstream_error"


# -- threads and comments -------------------------------------------------------------------


def threads_by(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["id"]: t for t in out["threads"]}


def test_a_review_thread_carries_its_position_and_resolution(make: Callable[..., Rig]) -> None:
    thread = threads_by(snap(make))["gh:rt:PRRT_kwDOexample0001"]
    assert (thread["resolvable"], thread["resolved"], thread["outdated"]) == (True, False, False)
    assert (thread["path"], thread["line"], thread["side"]) == ("src/parser.py", 42, "right")
    assert thread["commit_sha"] == HEAD
    assert [c["id"] for c in thread["comments"]] == [
        "gh:rc:PRRC_kwDOexample0001",
        "gh:rc:PRRC_kwDOexample0002",
    ]
    first, reply = thread["comments"]
    assert first["review_id"] == "gh:r:PRR_kwDOexample0002" and first["reply_to"] is None
    assert reply["reply_to"] == "gh:rc:PRRC_kwDOexample0001" and reply["review_id"] is None


def test_start_line_is_the_first_comments_own_range(make: Callable[..., Rig]) -> None:
    thread = threads_by(snap(make))["gh:rt:PRRT_kwDOexample0001"]
    assert thread["start_line"] == 40
    # a reply's own range, or one that is not a range, never decides
    doc = rec("parts")
    first = pr_of(doc)["reviewThreads"]["nodes"][0]["comments"]["nodes"]
    first[0]["startLine"] = None
    first[1]["startLine"] = 10
    assert (
        threads_by(snap(make, ForgectlParts=doc))["gh:rt:PRRT_kwDOexample0001"]["start_line"]
        is None
    )


@pytest.mark.parametrize("start", [42, 43, 0, -3, "40", True, 4.5])
def test_a_start_line_that_is_not_before_the_line_is_not_a_range(
    make: Callable[..., Rig], start: Any
) -> None:
    doc = rec("parts")
    pr_of(doc)["reviewThreads"]["nodes"][0]["comments"]["nodes"][0]["startLine"] = start
    thread = threads_by(snap(make, ForgectlParts=doc))["gh:rt:PRRT_kwDOexample0001"]
    assert thread["start_line"] is None


def test_an_outdated_thread_has_no_line_and_a_resolved_one_is_resolved(
    make: Callable[..., Rig],
) -> None:
    thread = threads_by(snap(make))["gh:rt:PRRT_kwDOexample0002"]
    assert (thread["outdated"], thread["resolved"], thread["line"]) == (True, True, None)
    assert thread["comments"][0]["minimized"] is True
    assert thread["commit_sha"] == "1" * 40


def test_a_file_level_thread_has_no_line_never_one(make: Callable[..., Rig]) -> None:
    thread = threads_by(snap(make))["gh:rt:PRRT_kwDOexample0003"]
    assert thread["line"] is None and thread["path"] == "README.md"
    doc = rec("parts")
    pr_of(doc)["reviewThreads"]["nodes"][2]["line"] = 1
    assert threads_by(snap(make, ForgectlParts=doc))["gh:rt:PRRT_kwDOexample0003"]["line"] is None


@pytest.mark.parametrize("raw", [None, "yes", 1, 0, [], {}])
def test_a_resolved_state_that_is_unknown_is_not_resolved(
    make: Callable[..., Rig], raw: Any
) -> None:
    doc = rec("parts")
    pr_of(doc)["reviewThreads"]["nodes"][1]["isResolved"] = raw
    assert (
        threads_by(snap(make, ForgectlParts=doc))["gh:rt:PRRT_kwDOexample0002"]["resolved"] is False
    )


def test_a_general_comment_is_a_one_comment_thread_that_cannot_be_resolved(
    make: Callable[..., Rig],
) -> None:
    out = snap(make)
    thread = threads_by(out)["gh:ic:IC_kwDOexample0001"]
    assert (thread["resolvable"], thread["resolved"], thread["path"], thread["line"]) == (
        False,
        False,
        None,
        None,
    )
    (comment,) = thread["comments"]
    assert comment["id"] == "gh:ic:IC_kwDOexample0001" and comment["review_id"] is None
    ghost = threads_by(out)["gh:ic:IC_kwDOexample0002"]["comments"][0]
    assert ghost["author"]["login"] is None and ghost["minimized"] is True


def test_review_bodies_are_not_comments(make: Callable[..., Rig]) -> None:
    out = snap(make)
    bodies = [c["body"] for t in out["threads"] for c in t["comments"]]
    assert "Looks good." not in bodies and "One note on the parser." not in bodies


def test_pending_comments_and_threads_are_not_listed(make: Callable[..., Rig]) -> None:
    doc = rec("parts")
    nodes = pr_of(doc)["reviewThreads"]["nodes"]
    nodes[0]["comments"]["nodes"][1]["state"] = "PENDING"
    nodes[2]["comments"]["nodes"][0]["state"] = "PENDING"
    out = snap(make, ForgectlParts=doc)
    assert "gh:rt:PRRT_kwDOexample0003" not in threads_by(out)
    assert len(threads_by(out)["gh:rt:PRRT_kwDOexample0001"]["comments"]) == 1


def test_a_thread_with_no_comments_is_left_out_not_a_crash(make: Callable[..., Rig]) -> None:
    doc = rec("parts")
    pr_of(doc)["reviewThreads"]["nodes"][2]["comments"]["nodes"] = []
    rig = make(standard(ForgectlParts=doc))
    out = rig.snapshot()
    assert "gh:rt:PRRT_kwDOexample0003" not in threads_by(out)


def test_threads_and_their_comments_are_paged(make: Callable[..., Rig]) -> None:
    doc = rec("parts")
    nodes = pr_of(doc)["reviewThreads"]
    nodes["pageInfo"] = {"hasNextPage": True, "endCursor": "Y3Vyc29yOjM="}
    nodes["nodes"][0]["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "Y3Vyc29yOjQ="}
    extra_comment = {
        "id": "PRRC_kwDOexample0020",
        "state": "SUBMITTED",
        "body": "Third.",
        "createdAt": "2026-10-02T12:00:00Z",
        "updatedAt": "2026-10-02T12:00:00Z",
        "isMinimized": False,
        "line": 42,
        "startLine": None,
        "author": {"__typename": "User", "login": "user-3", "databaseId": 1003},
        "replyTo": None,
        "pullRequestReview": None,
        "originalCommit": {"oid": HEAD},
    }
    comments_page = {
        "data": {
            "node": {
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": "x"},
                    "nodes": [extra_comment],
                }
            }
        }
    }
    threads_page = {
        "data": {
            "node": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": "x"},
                    "nodes": [
                        {
                            "id": "PRRT_kwDOexample0004",
                            "isResolved": False,
                            "isOutdated": False,
                            "path": "a.py",
                            "line": 3,
                            "diffSide": "LEFT",
                            "subjectType": "LINE",
                            "comments": {
                                "pageInfo": {"hasNextPage": False, "endCursor": "x"},
                                "nodes": [{**extra_comment, "id": "PRRC_kwDOexample0021"}],
                            },
                        }
                    ],
                }
            }
        }
    }
    rig = make(
        standard(
            ForgectlParts=doc,
            ForgectlThreadsPage=threads_page,
            ForgectlThreadCommentsPage=comments_page,
        )
    )
    out = rig.snapshot()
    assert out["complete"] is True
    assert "gh:rt:PRRT_kwDOexample0004" in threads_by(out)
    assert len(threads_by(out)["gh:rt:PRRT_kwDOexample0001"]["comments"]) == 3
    ask = next(r for r in rig.router.requests if operation(r) == "ForgectlThreadCommentsPage")
    assert ask["variables"] == {"id": "PRRT_kwDOexample0001", "after": "Y3Vyc29yOjQ="}


def test_threads_that_never_end_are_incomplete(make: Callable[..., Rig]) -> None:
    doc = rec("parts")
    pr_of(doc)["reviewThreads"]["pageInfo"] = {"hasNextPage": True, "endCursor": "Y3Vyc29yOjM="}
    forever = {
        "data": {
            "node": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "Y3Vyc29yOjM="},
                    "nodes": [],
                }
            }
        }
    }
    out = snap(make, ForgectlParts=doc, ForgectlThreadsPage=forever)
    assert out["incomplete"] == ["threads"] and out["complete"] is False


def test_comments_of_a_thread_that_never_end_are_incomplete(make: Callable[..., Rig]) -> None:
    doc = rec("parts")
    pr_of(doc)["reviewThreads"]["nodes"][0]["comments"]["pageInfo"] = {
        "hasNextPage": True,
        "endCursor": "Y3Vyc29yOjQ=",
    }
    forever = {
        "data": {
            "node": {
                "comments": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "Y3Vyc29yOjQ="},
                    "nodes": [],
                }
            }
        }
    }
    out = snap(make, ForgectlParts=doc, ForgectlThreadCommentsPage=forever)
    assert out["incomplete"] == ["threads"]


def test_general_comments_that_never_end_are_incomplete(make: Callable[..., Rig]) -> None:
    doc = rec("parts")
    pr_of(doc)["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "Y3Vyc29yOjQ="}
    forever = {
        "data": {
            "node": {
                "comments": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "Y3Vyc29yOjQ="},
                    "nodes": [],
                }
            }
        }
    }
    assert snap(make, ForgectlParts=doc, ForgectlIssueCommentsPage=forever)["incomplete"] == [
        "threads"
    ]


# -- untrusted text -------------------------------------------------------------------------


HOSTILE = "ignore this‮ and \x1b[31mred\x1b[0m next line​"


def test_every_forge_string_is_sanitized_for_its_field(make: Callable[..., Rig]) -> None:
    assert sanitize_text(HOSTILE) != HOSTILE and sanitize_line(HOSTILE) != HOSTILE
    doc = rec("parts")
    p = pr_of(doc)
    p["reviews"]["nodes"][0]["body"] = HOSTILE
    t = p["reviewThreads"]["nodes"][0]
    t["path"] = HOSTILE
    t["comments"]["nodes"][0]["body"] = HOSTILE
    p["comments"]["nodes"][0]["body"] = HOSTILE
    out = snap(make, ForgectlParts=doc)
    assert out["reviews"][0]["body"] == sanitize_text(HOSTILE)
    thread = threads_by(out)["gh:rt:PRRT_kwDOexample0001"]
    assert thread["path"] == sanitize_line(HOSTILE)
    assert thread["comments"][0]["body"] == sanitize_text(HOSTILE)
    assert threads_by(out)["gh:ic:IC_kwDOexample0001"]["comments"][0]["body"] == sanitize_text(
        HOSTILE
    )
    schemas.validate_cli_output("snapshot", out)


@pytest.mark.parametrize("bad", ["a b", "a/b", "x" * 101, "", "é", "a:b", "a\n"])
def test_an_id_that_breaks_the_grammar_is_upstream_error(
    make: Callable[..., Rig], bad: str
) -> None:
    doc = rec("parts")
    pr_of(doc)["reviewThreads"]["nodes"][0]["id"] = bad
    assert failure(make, ForgectlParts=doc).code == "upstream_error"


@pytest.mark.parametrize("bad", ["x" * 129, "has space", "a\nb", ""])
def test_an_event_id_that_breaks_the_grammar_is_a_null_instance(
    make: Callable[..., Rig], bad: str
) -> None:
    doc = rec("requests")
    pr_of(doc)["timelineItems"]["nodes"][3]["id"] = bad
    assert requests_by(snap(make, ForgectlRequests=doc))["user-5"]["instance"] is None


def test_a_login_that_is_not_a_github_login_is_never_passed_on(make: Callable[..., Rig]) -> None:
    doc = rec("parts")
    pr_of(doc)["comments"]["nodes"][0]["author"] = {
        "__typename": "User",
        "login": "a b‮",
        "databaseId": 4,
    }
    out = snap(make, ForgectlParts=doc)
    author = threads_by(out)["gh:ic:IC_kwDOexample0001"]["comments"][0]["author"]
    assert author["login"] is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(reviews=None),
        lambda p: p["reviews"].update(nodes="x"),
        lambda p: p["reviews"]["nodes"].append(7),
        lambda p: p["reviews"]["nodes"][0].update(submittedAt="yesterday"),
        lambda p: p["reviews"]["nodes"][0].update(id=None),
        lambda p: p["reviewThreads"]["nodes"][0].update(comments=[]),
        lambda p: p["reviewThreads"]["nodes"][0]["comments"]["nodes"][0].update(createdAt=None),
        lambda p: p["comments"]["nodes"].append({"id": "IC_x"}),
        lambda p: p["reviews"].update(pageInfo={"hasNextPage": "maybe"}),
        lambda p: p["reviews"].update(pageInfo={"hasNextPage": True, "endCursor": "bad cursor"}),
        lambda p: p.update(number=8),
        lambda p: p.update(headRefOid="zz"),
        lambda p: p.update(url="https://evil.example.test/example-org/example-repo/pull/7"),
    ],
)
def test_hostile_answers_are_typed_errors_never_tracebacks(
    make: Callable[..., Rig], mutate: Callable[[dict[str, Any]], Any]
) -> None:
    doc = rec("parts")
    mutate(pr_of(doc))
    error = failure(make, ForgectlParts=doc)
    assert error.code in ("upstream_error", "wrong_host_response")
    assert TOKEN not in str(error)


def test_a_missing_pull_request_is_not_found(make: Callable[..., Rig]) -> None:
    doc = rec("parts")
    doc["data"]["repository"]["pullRequest"] = None
    assert failure(make, ForgectlParts=doc).code == "not_found"


# -- the head's push time (#56) -------------------------------------------------------------


def push_doc(
    *nodes: dict[str, Any], commit_date: str | None = "2026-10-02T07:30:00Z"
) -> dict[str, Any]:
    doc = rec("push")
    repo = doc["data"]["repository"]
    repo["pullRequest"]["timelineItems"]["nodes"] = list(nodes)
    repo["object"] = (
        None
        if commit_date is None
        else {"__typename": "Commit", "oid": HEAD, "committedDate": commit_date}
    )
    return doc


def force(at: str, sha: str = HEAD) -> dict[str, Any]:
    return {"__typename": "HeadRefForcePushedEvent", "createdAt": at, "afterCommit": {"oid": sha}}


def commit(at: str, sha: str = HEAD) -> dict[str, Any]:
    return {"__typename": "PullRequestCommit", "commit": {"oid": sha, "committedDate": at}}


def pushed(out: dict[str, Any]) -> tuple[Any, Any]:
    return out["head"]["pushed_at"], out["head"]["pushed_at_source"]


def test_a_force_push_event_gives_its_time_as_push_event(make: Callable[..., Rig]) -> None:
    out = snap(
        make,
        ForgectlPush=push_doc(commit("2026-10-01T09:00:00Z", OTHER), force("2026-10-02T08:30:00Z")),
    )
    assert pushed(out) == ("2026-10-02T08:30:00Z", "push_event")


def test_an_offset_is_converted_to_utc(make: Callable[..., Rig]) -> None:
    assert pushed(snap(make)) == ("2026-10-02T08:30:00Z", "push_event")  # the recording says +02:00


def test_ordinary_commits_after_the_last_event_give_the_newest_commit_time(
    make: Callable[..., Rig],
) -> None:
    docs = push_doc(force("2026-10-02T08:30:00Z", OTHER), commit("2026-10-02T09:15:00Z"))
    assert pushed(snap(make, ForgectlPush=docs)) == ("2026-10-02T09:15:00Z", "push_event")


def test_the_newer_of_event_and_commit_wins(make: Callable[..., Rig]) -> None:
    docs = push_doc(commit("2026-10-02T07:00:00Z"), force("2026-10-02T08:30:00Z"))
    assert pushed(snap(make, ForgectlPush=docs)) == ("2026-10-02T08:30:00Z", "push_event")


def test_no_push_events_give_the_head_commit_date_as_commit_date(make: Callable[..., Rig]) -> None:
    docs = push_doc(commit("2026-10-01T09:00:00Z", OTHER), force("2026-10-01T10:00:00Z", OTHER))
    assert pushed(snap(make, ForgectlPush=docs)) == ("2026-10-02T07:30:00Z", "commit_date")


def test_neither_readable_is_null_null(make: Callable[..., Rig]) -> None:
    out = snap(make, ForgectlPush=push_doc(commit("2026-10-01T09:00:00Z", OTHER), commit_date=None))
    assert pushed(out) == (None, None)
    schemas.validate_cli_output("snapshot", out)
    golden.assert_golden(
        "snapshot/github-no-push-time", out, schema_id="forgectl/cli/snapshot.json"
    )


@pytest.mark.parametrize("bad", [None, "", "yesterday", "2026-10-02", "2026-13-45T25:61:61Z", 7])
def test_an_unreadable_event_time_falls_back_to_the_commit_date(
    make: Callable[..., Rig], bad: Any
) -> None:
    out = snap(make, ForgectlPush=push_doc(force(bad)))
    assert pushed(out) == ("2026-10-02T07:30:00Z", "commit_date")


def test_an_unreadable_commit_date_with_no_event_is_null(make: Callable[..., Rig]) -> None:
    out = snap(make, ForgectlPush=push_doc(commit_date="not a date"))
    assert pushed(out) == (None, None)


def test_a_malformed_push_answer_is_unknown_not_a_failed_snapshot(make: Callable[..., Rig]) -> None:
    doc = rec("push")
    doc["data"]["repository"]["pullRequest"]["timelineItems"] = "x"
    rig = make(standard(ForgectlPush=doc))
    assert pushed(rig.snapshot()) == (None, None)
    assert rig.warnings


def test_a_new_push_gives_a_later_pushed_at(make: Callable[..., Rig]) -> None:
    before = snap(make, ForgectlPush=push_doc(force("2026-10-02T10:30:00+02:00")))
    after = snap(make, ForgectlPush=push_doc(force("2026-10-02T09:45:00Z")))
    stamp = lambda out: datetime.fromisoformat(out["head"]["pushed_at"])  # noqa: E731
    assert stamp(after) > stamp(before)


def test_the_timeline_is_paged_back_when_the_first_page_has_no_event_of_this_head(
    make: Callable[..., Rig],
) -> None:
    first = push_doc(commit("2026-10-01T09:00:00Z", OTHER))
    first["data"]["repository"]["pullRequest"]["timelineItems"]["pageInfo"] = {
        "hasPreviousPage": True,
        "startCursor": "Y3Vyc29yOjk=",
    }
    page = {
        "data": {
            "node": {
                "timelineItems": {
                    "pageInfo": {"hasPreviousPage": False, "startCursor": "x"},
                    "nodes": [force("2026-10-02T08:00:00Z")],
                }
            }
        }
    }
    out = snap(make, ForgectlPush=first, ForgectlPushPage=page)
    assert pushed(out) == ("2026-10-02T08:00:00Z", "push_event")


def test_the_push_search_is_capped(make: Callable[..., Rig]) -> None:
    first = push_doc(commit("2026-10-01T09:00:00Z", OTHER))
    first["data"]["repository"]["pullRequest"]["timelineItems"]["pageInfo"] = {
        "hasPreviousPage": True,
        "startCursor": "Y3Vyc29yOjk=",
    }
    forever = {
        "data": {
            "node": {
                "timelineItems": copy.deepcopy(
                    first["data"]["repository"]["pullRequest"]["timelineItems"]
                )
            }
        }
    }
    rig = make(standard(ForgectlPush=first, ForgectlPushPage=forever))
    assert pushed(rig.snapshot()) == ("2026-10-02T07:30:00Z", "commit_date")
    assert rig.router.ops.count("ForgectlPushPage") <= github_snapshot.MAX_PUSH_PAGES


# -- comments, reviews and ci commands ------------------------------------------------------


def test_comments_reads_one_query_and_no_show(make: Callable[..., Rig]) -> None:
    rig = make()
    result = github_snapshot.comments(rig.env, REF).to_dict()
    golden.assert_golden("comments/github-open", result, schema_id="forgectl/cli/comments.json")
    assert rig.router.ops == ["ForgectlParts"]
    assert result["head_sha"] == HEAD and result["complete"] is True
    assert result["pr"] == "github.com/example-org/example-repo#7"


def test_comments_is_incomplete_when_a_thread_list_is_cut(make: Callable[..., Rig]) -> None:
    doc = rec("parts")
    pr_of(doc)["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "Y3Vyc29yOjQ="}
    forever = {"data": {"node": {"comments": copy.deepcopy(pr_of(doc)["comments"])}}}
    rig = make(standard(ForgectlParts=doc, ForgectlIssueCommentsPage=forever))
    assert github_snapshot.comments(rig.env, REF).to_dict()["complete"] is False


def test_comments_through_the_backend_delegation(make: Callable[..., Rig]) -> None:
    rig = make()
    assert rig.backend.comments(REF).to_dict()["threads"]


def test_reviews_command_golden_and_decision_from_the_pr(make: Callable[..., Rig]) -> None:
    rig = make()
    result = rig.backend.reviews(REF).to_dict()
    golden.assert_golden("reviews/github-open", result, schema_id="forgectl/cli/reviews.json")
    assert result["review_decision"] == "approved"
    assert (result["approvals"], result["approvals_at_head"]) == (1, 1)
    assert rig.router.ops == ["ForgectlShow", "ForgectlReviewsPage"]
    ask = rig.router.requests[1]["variables"]
    assert ask == {"id": "PR_kwDOexample0007", "after": None}


def test_reviews_command_is_incomplete_on_a_page_cap(make: Callable[..., Rig]) -> None:
    page = rec("parts")
    conn = pr_of(page)["reviews"]
    conn["pageInfo"] = {"hasNextPage": True, "endCursor": "Y3Vyc29yOjI="}
    answer = {"data": {"node": {"reviews": conn}}}
    rig = make(standard(ForgectlReviewsPage=answer))
    assert rig.backend.reviews(REF).complete is False


def test_the_ci_command_reads_for_the_head_and_matches_show(make: Callable[..., Rig]) -> None:
    rig = make()
    result = rig.backend.ci(REF).to_dict()
    golden.assert_golden("ci/github-open", result, schema_id="forgectl/cli/ci.json")
    assert rig.router.ops == ["ForgectlShow"]
    assert result["ci"]["state"] == "passing" and result["head_sha"] == HEAD


def mutated_show(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    doc = show_recording()
    change(pr_of(doc))
    return doc


def ci_of(make: Callable[..., Rig], change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    rig = make(standard(ForgectlShow=mutated_show(change)))
    out = rig.backend.ci(REF).to_dict()
    schemas.validate_cli_output("ci", out)
    return out["ci"]


def rollup(pr: dict[str, Any]) -> dict[str, Any]:
    return pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]


def test_ci_none_without_checks_and_none_is_not_passing(make: Callable[..., Rig]) -> None:
    ci = ci_of(make, lambda pr: pr["commits"]["nodes"][0]["commit"].update(statusCheckRollup=None))
    assert (ci["state"], ci["checks"], ci["complete"]) == ("none", [], True)


def test_ci_none_for_skipped_only(make: Callable[..., Rig]) -> None:
    def change(pr: dict[str, Any]) -> None:
        rollup(pr)["contexts"]["nodes"] = [
            {
                "__typename": "CheckRun",
                "name": "s",
                "status": "COMPLETED",
                "conclusion": "SKIPPED",
                "detailsUrl": None,
                "completedAt": "2026-10-02T08:00:00Z",
                "isRequired": False,
            }
        ]

    ci = ci_of(make, change)
    assert ci["state"] == "none" and ci["checks"][0]["state"] == "skipped"


def test_an_unregistered_required_check_is_invisible_to_ci_but_blocks_the_pr(
    make: Callable[..., Rig],
) -> None:
    def change(pr: dict[str, Any]) -> None:
        pr["mergeStateStatus"] = "BLOCKED"
        pr["reviewDecision"] = None
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None

    out = snap(make, ForgectlShow=mutated_show(change))
    assert out["ci"]["state"] == "none"
    assert [b["code"] for b in out["blocked_by"]] == ["checks_pending"]


@pytest.mark.parametrize(
    ("status", "conclusion", "state"),
    [
        ("COMPLETED", "SUCCESS", "passing"),
        ("COMPLETED", "FAILURE", "failing"),
        ("COMPLETED", "TIMED_OUT", "failing"),
        ("COMPLETED", "CANCELLED", "failing"),
        ("COMPLETED", "ACTION_REQUIRED", "failing"),
        ("COMPLETED", "STARTUP_FAILURE", "failing"),
        ("COMPLETED", "STALE", "failing"),
        ("COMPLETED", "NEUTRAL", "none"),
        ("COMPLETED", "SKIPPED", "none"),
        ("IN_PROGRESS", None, "pending"),
        ("QUEUED", None, "pending"),
        ("WAITING", None, "pending"),
        ("PENDING", None, "pending"),
        ("REQUESTED", None, "pending"),
        ("COMPLETED", "FUTURE", "pending"),
    ],
)
def test_each_check_state_maps_to_the_ci_state(
    make: Callable[..., Rig], status: str, conclusion: str | None, state: str
) -> None:
    def change(pr: dict[str, Any]) -> None:
        rollup(pr)["contexts"]["nodes"] = [
            {
                "__typename": "CheckRun",
                "name": "c",
                "status": status,
                "conclusion": conclusion,
                "detailsUrl": "javascript:alert(1)",
                "completedAt": None,
                "isRequired": None,
            }
        ]

    ci = ci_of(make, change)
    assert ci["state"] == state
    assert ci["checks"][0]["url"] is None  # another scheme is null


def test_ci_reads_the_head_by_oid_when_the_last_listed_commit_is_not_the_head(
    make: Callable[..., Rig],
) -> None:
    show_doc = mutated_show(
        lambda pr: pr["commits"]["nodes"][0]["commit"].update(oid=OTHER, statusCheckRollup=None)
    )
    by_oid = {
        "data": {
            "repository": {
                "object": {
                    "oid": HEAD,
                    "statusCheckRollup": {
                        "contexts": {
                            "pageInfo": {"hasNextPage": False, "endCursor": "x"},
                            "nodes": [
                                {
                                    "__typename": "StatusContext",
                                    "context": "ci/x",
                                    "state": "FAILURE",
                                    "targetUrl": None,
                                    "createdAt": "2026-10-02T08:00:00Z",
                                    "isRequired": True,
                                }
                            ],
                        }
                    },
                }
            }
        }
    }
    rig = make(standard(ForgectlShow=show_doc, ForgectlChecks=by_oid))
    ci = rig.backend.ci(REF).to_dict()["ci"]
    assert ci["state"] == "failing" and ci["head_sha"] == HEAD and ci["for_current_head"] is True
    assert rig.router.ops == ["ForgectlShow", "ForgectlChecks"]


def test_a_check_list_cut_off_is_incomplete_so_never_passing(make: Callable[..., Rig]) -> None:
    show_doc = mutated_show(
        lambda pr: rollup(pr)["contexts"]["pageInfo"].update(
            hasNextPage=True, endCursor="Y3Vyc29yOjM="
        )
    )
    forever = {
        "data": {
            "repository": {
                "object": {"oid": HEAD, "statusCheckRollup": copy.deepcopy(rollup(pr_of(show_doc)))}
            }
        }
    }
    rig = make(standard(ForgectlShow=show_doc, ForgectlChecks=forever))
    ci = rig.backend.ci(REF).to_dict()["ci"]
    assert ci["complete"] is False and ci["state"] == "pending"
    snapshot_out = make(standard(ForgectlShow=show_doc, ForgectlChecks=forever)).snapshot()
    assert snapshot_out["incomplete"] == ["ci"]


# -- regressions named by the issue ---------------------------------------------------------


def test_regression_team_requests_are_never_users(make: Callable[..., Rig]) -> None:
    by = requests_by(snap(make))
    assert by["user-8"]["reviewer"]["kind"] == "team"
    assert by["user-5"]["reviewer"]["kind"] == "user"
    # a user named like a team slug is a different reviewer
    doc = rec("requests")
    pr_of(doc)["reviewRequests"]["nodes"][2]["requestedReviewer"]["login"] = "user-8"
    rest = rec("requested-reviewers")
    rest["users"][1]["login"] = "user-8"
    out = snap(make, ForgectlRequests=doc, rest=rest_answer(rest))
    assert {
        r["reviewer"]["kind"] for r in out["review_requests"] if r["reviewer"]["login"] == "user-8"
    } == {
        "user",
        "team",
    }


def test_regression_updated_at_is_not_the_change_signal(make: Callable[..., Rig]) -> None:
    """A new comment moves the snapshot's lists, not its `updated_at`: a consumer compares parts."""
    before = snap(make)
    doc = rec("parts")
    pr_of(doc)["comments"]["nodes"].append(
        {
            "id": "IC_kwDOexample0003",
            "body": "New.",
            "createdAt": "2026-10-02T11:59:00Z",
            "updatedAt": "2026-10-02T11:59:00Z",
            "isMinimized": False,
            "author": {"__typename": "User", "login": "user-4", "databaseId": 1004},
        }
    )
    after = snap(make, ForgectlParts=doc)
    assert before["updated_at"] == after["updated_at"]
    assert comparable(before) != comparable(after)


def test_the_lists_have_a_fixed_order(make: Callable[..., Rig]) -> None:
    out = snap(make)
    assert [t["comments"][0]["created_at"] for t in out["threads"]] == sorted(
        t["comments"][0]["created_at"] for t in out["threads"]
    )
    assert [r["reviewer"]["login"] for r in out["review_requests"]] == [
        "user-5",
        "user-7",
        "user-8",
    ]
