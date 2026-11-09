"""GitHub `show` (#11): the one GraphQL query, its mapping, retries and readiness.

The gateway is real; its process runner is a script that answers like `gh api -i` does. The
recordings under tests/recordings/github/show are hand-built from GitHub's documented GraphQL
schema, scrubbed of real names. Every assumption about GitHub's real behavior is UNVERIFIED until
the live suite (#31) re-checks it; see the report of #11 for the list.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from forgectl.backends import github
from forgectl.backends.base import Backend
from forgectl.backends.github import SHOW_QUERY, GitHubBackend
from forgectl.config import Host, LoadedConfig
from forgectl.errors import ForgectlError
from forgectl.gateway import Gateway, ProcessResult
from forgectl.hosts import Hosts
from forgectl.identifiers import PrRef
from forgectl.model import PrRead
from forgectl.reads import ReadEnv

from .harness import golden, schemas
from .harness.fixtures import forgectl_env
from .harness.forges import FakeForges

RECORDINGS = Path(__file__).parent / "recordings" / "github" / "show"
HEAD = "3f8a1c0d5e7b9a2c4d6e8f0a1b3c5d7e9f0a1b2c"
OTHER = "5555555555555555555555555555555555555555"
REF = PrRef(host="github.com", backend="github", project="example-org/example-repo", number=7)
TOKEN = "tok-show-0001-zzzz"
PR_URL = "https://github.com/example-org/example-repo/pull/7"
REPO_URL = "https://github.com/example-org/example-repo"


def recording(name: str = "open-clean") -> dict[str, Any]:
    return json.loads((RECORDINGS / f"{name}.json").read_text(encoding="utf-8"))


def pr_of(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["data"]["repository"]["pullRequest"]


def repo_of(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["data"]["repository"]


def http(status: int = 200, body: Any = None, *, headers: Mapping[str, str] | None = None) -> bytes:
    reason = {200: "OK", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found"}.get(status, "X")
    head = f"HTTP/2.0 {status} {reason}\r\nContent-Type: application/json\r\n"
    for k, v in (headers or {}).items():
        head += f"{k}: {v}\r\n"
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return head.encode() + b"\r\n" + (raw if body is not None else b"")


class Script:
    """A process runner that answers each call from a queue and logs what was asked."""

    def __init__(self, *answers: Any) -> None:
        self.queue: list[Any] = list(answers)
        self.requests: list[dict[str, Any]] = []
        self.argvs: list[list[str]] = []

    def __call__(self, argv: Sequence[str], *, stdin: bytes | None, **_: Any) -> ProcessResult:
        self.argvs.append(list(argv))
        self.requests.append(json.loads(stdin) if stdin else {})
        assert self.queue, "no answer scripted"
        answer: Any = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]  # last repeats
        if callable(answer):
            answer = answer(self.requests[-1])
        if isinstance(answer, ProcessResult):
            return answer
        raw: bytes = http(200, answer) if isinstance(answer, dict) else answer
        # gh exits 1 on a GraphQL body holding errors, and on any non-2xx status
        code = 0 if raw.startswith(b"HTTP/2.0 2") and b'"errors"' not in raw else 1
        return ProcessResult(pid=1, pgid=1, returncode=code, stdout=raw, stderr=b"")


class Slept:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


class Harness:
    def __init__(
        self, forges: FakeForges, script: Script, *, retries: int = 3, timeout: float = 60.0
    ):
        config = LoadedConfig.defaults()
        config = LoadedConfig(
            config=config.config.model_copy(
                update={"hosts": {"github.com": Host(backend="github", token_env="SHOW_TOKEN")}}
            ),
            config_hash="x",
            config_path=None,
        )
        self.script = script
        self.slept = Slept()
        gateway = Gateway(
            config,
            timeout=timeout,
            environ=forgectl_env(forges, {"SHOW_TOKEN": TOKEN}),
            runner=script,  # type: ignore[arg-type]
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
        self.backend = GitHubBackend(self.env)

    def show(self, ref: PrRef = REF) -> PrRead:
        return self.backend.get_pr(ref)


@pytest.fixture
def make(fake_forges: FakeForges) -> Callable[..., Harness]:
    def build(*answers: Any, **kwargs: Any) -> Harness:
        return Harness(fake_forges, Script(*answers), **kwargs)

    return build


def run(make: Callable[..., Harness], doc: dict[str, Any], *more: Any, **kw: Any) -> PrRead:
    return make(doc, *more, **kw).show()


def mutated(change: Callable[[dict[str, Any], dict[str, Any]], None]) -> dict[str, Any]:
    """The base recording after ``change(pull_request, repository)``."""
    doc = recording()
    change(pr_of(doc), repo_of(doc))
    return doc


def codes(read: PrRead) -> list[tuple[str, str | None]]:
    return [(b.code.value, b.detail) for b in read.pr.blocked_by]


def failure(make: Callable[..., Harness], *answers: Any, **kw: Any) -> ForgectlError:
    with pytest.raises(ForgectlError) as caught:
        make(*answers, **kw).show()
    return caught.value


# -- the happy path and the golden output ----------------


def test_the_adapter_satisfies_the_backend_protocol(make: Callable[..., Harness]) -> None:
    assert isinstance(make(recording()).backend, Backend)


def test_golden_open_clean_pr(make: Callable[..., Harness]) -> None:
    read = run(make, recording())
    golden.assert_golden(
        "show/github-open-clean", read.pr.to_dict(), schema_id="forgectl/cli/show.json"
    )


def test_show_costs_one_request_with_a_constant_query_and_variables_only(
    make: Callable[..., Harness],
) -> None:
    harness = make(recording())
    harness.show()
    (request,) = harness.script.requests
    assert request["query"] == SHOW_QUERY
    assert request["variables"] == {
        "owner": "example-org",
        "name": "example-repo",
        "number": 7,
        "after": None,
    }
    assert harness.script.argvs[0][-1] == "graphql"


def test_the_query_text_has_no_slot_for_input() -> None:
    assert "{owner}" not in SHOW_QUERY and "%s" not in SHOW_QUERY
    for needed in (
        "isMergeQueueEnabled",
        "squashMergeAllowed",
        "defaultBranchRef",
        "reviewDecision",
    ):
        assert needed in SHOW_QUERY
    assert "BASE_REF_CHANGED_EVENT" in SHOW_QUERY
    assert "AUTOMATIC_BASE_CHANGE_SUCCEEDED_EVENT" in SHOW_QUERY


def test_the_ci_comes_from_the_same_read(make: Callable[..., Harness]) -> None:
    read = run(make, recording())
    assert read.ci.state == "passing"
    assert read.ci.head_sha == HEAD
    assert [c.name for c in read.ci.checks] == ["build", "ci/lint"]
    assert [c.required for c in read.ci.checks] == [True, False]


def test_the_untrusted_token_never_reaches_the_argv(make: Callable[..., Harness]) -> None:
    harness = make(recording())
    harness.show()
    assert all(TOKEN not in arg for arg in harness.script.argvs[0])


# -- blocked_by and readiness ----------------


def with_status(status: str | None, mergeable: str | None = "MERGEABLE") -> dict[str, Any]:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["mergeStateStatus"] = status
        pr["mergeable"] = mergeable

    return mutated(change)


@pytest.mark.parametrize(
    ("status", "mergeable", "expected", "ready"),
    [
        ("CLEAN", "MERGEABLE", [], True),
        ("UNSTABLE", "MERGEABLE", [], True),
        ("HAS_HOOKS", "MERGEABLE", [], True),
        ("DIRTY", "CONFLICTING", [("conflicts", None)], False),
        ("BEHIND", "MERGEABLE", [("branch_behind", None)], False),
        ("DRAFT", "MERGEABLE", [("draft", None)], False),
    ],
)
def test_the_merge_state_table(
    make: Callable[..., Harness],
    status: str,
    mergeable: str,
    expected: list[tuple[str, str | None]],
    ready: bool,
) -> None:
    read = run(make, with_status(status, mergeable))
    assert codes(read) == expected
    assert read.pr.is_mergeable is ready


def test_conflicting_without_a_dirty_status_still_reads_conflicts(
    make: Callable[..., Harness],
) -> None:
    assert codes(run(make, with_status("CLEAN", "CONFLICTING"))) == [("conflicts", None)]


def test_a_draft_flag_without_the_draft_status_reads_draft(make: Callable[..., Harness]) -> None:
    doc = with_status("CLEAN")
    pr_of(doc)["isDraft"] = True
    read = run(make, doc)
    assert read.pr.draft is True
    assert codes(read) == [("draft", None)]


def test_an_unmapped_status_is_other_with_the_status_as_detail(
    make: Callable[..., Harness],
) -> None:
    assert codes(run(make, with_status("NEW_STATE_2027"))) == [("other", "NEW_STATE_2027")]


def test_a_hostile_status_is_one_line_in_the_detail(make: Callable[..., Harness]) -> None:
    ((_, detail),) = codes(run(make, with_status("A\nB\u202e" + "x" * 500)))
    assert detail is not None and "\n" not in detail and "\u202e" not in detail
    assert len(detail) <= 200


def test_a_missing_status_never_reads_ready(make: Callable[..., Harness]) -> None:
    harness = make(with_status(None, None))
    got = harness.show()
    assert len(harness.script.requests) == 3
    assert ("mergeability_unknown", None) in codes(got)
    assert got.pr.mergeable is None


def test_unknown_is_read_again_and_reports_the_value_it_settles_on(
    make: Callable[..., Harness],
) -> None:
    unknown = with_status("UNKNOWN", "UNKNOWN")
    harness = make(unknown, unknown, recording())
    read = harness.show()
    assert len(harness.script.requests) == 3
    assert codes(read) == []
    assert read.pr.mergeable is True
    assert len(harness.slept.waits) == 2


def test_unknown_after_three_tries_is_mergeability_unknown(make: Callable[..., Harness]) -> None:
    harness = make(with_status("UNKNOWN", "UNKNOWN"))
    read = harness.show()
    assert len(harness.script.requests) == 3
    assert codes(read) == [("mergeability_unknown", None)]
    assert read.pr.mergeable is None
    assert read.pr.is_mergeable is False


def test_retries_zero_reads_unknown_once(make: Callable[..., Harness]) -> None:
    harness = make(with_status("UNKNOWN", "UNKNOWN"), retries=0)
    read = harness.show()
    assert len(harness.script.requests) == 1
    assert codes(read) == [("mergeability_unknown", None)]


def test_a_closed_pr_gets_no_readiness_retry_and_no_blockers(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["state"] = "CLOSED"
        pr["closedAt"] = "2026-10-03T00:00:00Z"
        pr["mergeStateStatus"] = "UNKNOWN"
        pr["mergeable"] = "UNKNOWN"

    harness = make(mutated(change))
    read = harness.show()
    assert len(harness.script.requests) == 1
    assert read.pr.blocked_by == ()
    assert read.pr.mergeable is None
    assert read.pr.state == "closed"
    assert read.pr.closed_at == "2026-10-03T00:00:00Z"


def test_blocked_with_an_unregistered_required_check_is_checks_pending(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["mergeStateStatus"] = "BLOCKED"
        pr["reviewDecision"] = None
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None

    read = run(make, mutated(change))
    assert read.ci.state == "none"
    assert codes(read) == [("checks_pending", None)]


def test_blocked_with_review_required_is_reviews_required(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["mergeStateStatus"] = "BLOCKED"
        pr["reviewDecision"] = "REVIEW_REQUIRED"

    read = run(make, mutated(change))
    assert codes(read) == [("reviews_required", None)]
    assert read.pr.review_decision == "review_required"


def test_blocked_with_unresolved_threads_is_discussions_unresolved(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["mergeStateStatus"] = "BLOCKED"
        pr["reviewThreads"]["nodes"] = [{"isResolved": False}, {"isResolved": True}]

    assert codes(run(make, mutated(change))) == [("discussions_unresolved", None)]


def test_blocked_with_nothing_to_explain_it_is_other_blocked(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["mergeStateStatus"] = "BLOCKED"

    assert codes(run(make, mutated(change))) == [("other", "BLOCKED")]


def test_blocked_with_more_threads_than_one_page_and_none_open_still_blocks(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["mergeStateStatus"] = "BLOCKED"
        pr["reviewThreads"]["pageInfo"]["hasNextPage"] = True

    assert codes(run(make, mutated(change))) == [("other", "BLOCKED")]  # not read: never ready


def test_failing_and_pending_checks_block_though_the_host_says_clean(
    make: Callable[..., Harness],
) -> None:
    def failing(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][0][
            "conclusion"
        ] = "FAILURE"

    def pending(pr: dict[str, Any], _: dict[str, Any]) -> None:
        node = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][0]
        node["status"] = "IN_PROGRESS"
        node["conclusion"] = None

    assert codes(run(make, mutated(failing))) == [("checks_failing", None)]
    assert codes(run(make, mutated(pending))) == [("checks_pending", None)]


def test_changes_requested_by_a_reviewer_blocks_whatever_the_host_decision(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["reviews"]["nodes"].append(
            {
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-10-02T11:00:00Z",
                "authorCanPushToRepository": True,
                "author": {"__typename": "User", "login": "user-4", "databaseId": 1004},
                "commit": {"oid": HEAD},
            }
        )

    assert ("changes_requested", None) in codes(run(make, mutated(change)))


def test_a_non_writers_changes_requested_does_not_block(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["reviews"]["nodes"].append(
            {
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-10-02T11:00:00Z",
                "authorCanPushToRepository": False,
                "author": {"__typename": "User", "login": "user-4", "databaseId": 1004},
                "commit": {"oid": HEAD},
            }
        )

    assert codes(run(make, mutated(change))) == []


# -- a commit that is not the head ----------------


def checks_page(
    commit: str, nodes: list[dict[str, Any]], *, more: bool = False, cursor: str | None = "c1"
) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "nameWithOwner": "example-org/example-repo",
                "url": REPO_URL,
                "object": {
                    "oid": commit,
                    "statusCheckRollup": {
                        "contexts": {
                            "pageInfo": {"hasNextPage": more, "endCursor": cursor},
                            "nodes": nodes,
                        }
                    },
                },
            }
        }
    }


def run_node(name: str, conclusion: str = "SUCCESS", status: str = "COMPLETED") -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": "https://ci.example.test/" + name,
        "completedAt": "2026-10-02T08:00:00Z",
        "isRequired": None,
    }


def test_a_last_listed_commit_that_is_not_the_head_reads_the_head_by_oid(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["commits"]["nodes"][0]["commit"]["oid"] = OTHER  # old commit, all green
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = [
            run_node("old")
        ]

    harness = make(mutated(change), checks_page(HEAD, [run_node("new", "FAILURE")]))
    read = harness.show()
    assert len(harness.script.requests) == 2
    second = harness.script.requests[1]
    assert second["variables"]["oid"] == HEAD
    assert second["variables"]["number"] == 7
    assert [c.name for c in read.ci.checks] == ["new"]
    assert read.ci.state == "failing"
    assert codes(read) == [("checks_failing", None)]


def test_the_head_commit_missing_in_the_oid_answer_fails_the_read(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["commits"]["nodes"][0]["commit"]["oid"] = OTHER

    for answer in (
        {"data": {"repository": {"object": None}}},
        checks_page(OTHER, [run_node("x")]),
    ):
        assert failure(make, mutated(change), answer).code == "upstream_error"


def test_an_unlisted_commit_list_also_reads_by_oid(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["commits"]["nodes"] = []

    harness = make(mutated(change), checks_page(HEAD, [run_node("only")]))
    assert [c.name for c in harness.show().ci.checks] == ["only"]


def test_long_check_lists_are_paged_and_a_failed_page_fails_the_read(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        ctx = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        ctx["pageInfo"] = {"hasNextPage": True, "endCursor": "cur-1"}

    harness = make(mutated(change), checks_page(HEAD, [run_node("later")]))
    read = harness.show()
    assert harness.script.requests[1]["variables"]["after"] == "cur-1"
    assert [c.name for c in read.ci.checks] == ["build", "ci/lint", "later"]
    assert read.ci.complete is True
    broken = make(
        mutated(change),
        http(502, {"message": "bad"}),
        http(502, {}),
        http(502, {}),
        http(502, {}),
        retries=3,
    )
    with pytest.raises(ForgectlError) as caught:
        broken.show()
    assert caught.value.code == "upstream_error"


def test_a_check_list_longer_than_the_cap_is_incomplete_so_never_passing(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        ctx = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        ctx["pageInfo"] = {"hasNextPage": True, "endCursor": "cur-0"}

    pages = [
        checks_page(HEAD, [run_node(f"p{i}")], more=True, cursor=f"cur-{i + 1}") for i in range(20)
    ]
    read = run(make, mutated(change), *pages)
    assert read.ci.complete is False
    assert read.ci.state == "pending"
    assert ("checks_pending", None) in codes(read)


@pytest.mark.parametrize(
    ("node", "state", "counted"),
    [
        (run_node("a", "SUCCESS"), "passing", True),
        (run_node("a", "SKIPPED"), "skipped", False),
        (run_node("a", "NEUTRAL"), "skipped", False),
        (run_node("a", "TIMED_OUT"), "failing", True),
        (run_node("a", "CANCELLED"), "failing", True),
        (run_node("a", "STARTUP_FAILURE"), "failing", True),
        (run_node("a", "x", status="QUEUED"), "pending", True),
        (run_node("a", "x", status="WAITING"), "pending", True),
        (run_node("a", "BRAND_NEW"), "pending", True),
        (
            {
                "__typename": "StatusContext",
                "context": "s",
                "state": "ERROR",
                "targetUrl": None,
                "createdAt": None,
                "isRequired": True,
            },
            "failing",
            True,
        ),
        (
            {
                "__typename": "StatusContext",
                "context": "s",
                "state": "EXPECTED",
                "targetUrl": None,
                "createdAt": None,
                "isRequired": True,
            },
            "pending",
            True,
        ),
        ({"__typename": "Future", "name": "?"}, "pending", True),
        (None, "pending", True),
    ],
)
def test_check_states_map_and_an_unknown_one_is_pending(
    make: Callable[..., Harness], node: Any, state: str, counted: bool
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = [node]

    harness = make(mutated(change))
    read = harness.show()
    (check,) = read.ci.checks
    assert (check.state, check.counted) == (state, counted)


def test_skipped_only_head_has_ci_none_and_clean_is_not_passing(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"] = [
            run_node("s", "SKIPPED")
        ]

    read = run(make, mutated(change))
    assert read.ci.state == "none"
    assert codes(read) == []


# -- logins, bots, ghosts ----------------


@pytest.mark.parametrize(
    ("typename", "login", "mapped"),
    [
        ("Bot", "dependabot", "dependabot[bot]"),
        ("Bot", "dependabot[bot]", "dependabot[bot]"),
        ("Bot", "app/dependabot", "dependabot[bot]"),
        ("User", "dependabot", "dependabot"),  # a different account that REST would say so of
        ("User", "user-1", "user-1"),
        ("Mannequin", "user-9", "user-9"),
        ("User", "bad login", None),
        ("User", "-leading", None),
        ("User", "a" * 40, None),
        ("User", "x\nforged: line", None),
        ("User", "\u202eevil", None),
        ("User", "../etc", None),
        ("User", 7, None),
        ("User", None, None),
        ("User", "app/user-2", None),  # only a Bot is spelled like a bot
        ("User", "user-2[bot]", None),
        ("Mannequin", "app/x", None),
        (None, "y[bot]", None),
    ],
)
def test_login_mapping(typename: str | None, login: Any, mapped: str | None) -> None:
    assert github.rest_login(typename, login) == mapped


def test_three_spellings_of_one_bot_are_one_author_one_account(
    make: Callable[..., Harness],
) -> None:
    for login in ("dependabot", "dependabot[bot]", "app/dependabot"):

        def change(pr: dict[str, Any], _: dict[str, Any], login: str = login) -> None:
            pr["author"] = {"__typename": "Bot", "login": login, "databaseId": 49699333}

        read = run(make, mutated(change))
        assert read.pr.author.login == "dependabot[bot]"
        assert read.pr.author.is_bot is True
        assert read.pr.author.id == "49699333"


def test_one_bot_reviewing_under_two_spellings_is_one_latest_entry() -> None:
    def review(login: str, state: str, at: str) -> dict[str, Any]:
        return {
            "state": state,
            "submittedAt": at,
            "authorCanPushToRepository": True,
            "author": {"__typename": "Bot", "login": login, "databaseId": 5},
            "commit": {"oid": HEAD},
        }

    table = github.latest_by_reviewer(
        [
            review("dependabot", "CHANGES_REQUESTED", "2026-10-02T09:00:00Z"),
            review("app/dependabot", "APPROVED", "2026-10-02T10:00:00Z"),
        ]
    )
    (entry,) = table.entries
    assert (entry.login, entry.state) == ("dependabot[bot]", "approved")


def test_a_deleted_author_has_null_login_id_and_bot(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["author"] = None

    read = run(make, mutated(change))
    assert read.pr.author.to_dict() == {"login": None, "id": None, "is_bot": None}


def test_an_author_whose_login_fails_the_grammar_has_a_null_login(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["author"] = {"__typename": "User", "login": "bad login;rm", "databaseId": 12}

    read = run(make, mutated(change))
    assert read.pr.author.login is None
    assert read.pr.author.id == "12"
    assert read.pr.author.is_bot is False


def test_an_unusable_reviewer_login_asking_for_changes_still_blocks(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["reviews"]["nodes"].append(
            {
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-10-02T11:00:00Z",
                "authorCanPushToRepository": True,
                "author": {"__typename": "User", "login": "no good", "databaseId": 4},
                "commit": {"oid": HEAD},
            }
        )

    assert ("changes_requested", None) in codes(run(make, mutated(change)))


def test_a_deleted_reviewer_is_skipped_and_the_author_cannot_approve_themselves(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["reviews"]["nodes"] = [
            {
                "state": "APPROVED",
                "submittedAt": "2026-10-02T09:00:00Z",
                "authorCanPushToRepository": True,
                "author": None,
                "commit": {"oid": HEAD},
            },
            {
                "state": "APPROVED",
                "submittedAt": "2026-10-02T09:01:00Z",
                "authorCanPushToRepository": True,
                "author": {"__typename": "User", "login": "user-1", "databaseId": 1001},
                "commit": {"oid": HEAD},
            },
        ]

    read = run(make, mutated(change))
    assert (read.pr.approvals, read.pr.approvals_at_head) == (0, 0)


# -- approvals and the retarget ----------------


def test_an_approval_at_the_head_counts(make: Callable[..., Harness]) -> None:
    read = run(make, recording())
    assert (read.pr.approvals, read.pr.approvals_at_head) == (1, 1)


def test_an_approval_of_an_older_head_counts_only_as_approvals(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["reviews"]["nodes"][0]["commit"]["oid"] = OTHER

    read = run(make, mutated(change))
    assert (read.pr.approvals, read.pr.approvals_at_head) == (1, 0)


def test_a_non_writers_approval_is_not_at_head(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["reviews"]["nodes"][0]["authorCanPushToRepository"] = False

    read = run(make, mutated(change))
    assert (read.pr.approvals, read.pr.approvals_at_head) == (0, 0)


@pytest.mark.parametrize("kind", ["BaseRefChangedEvent", "AutomaticBaseChangeSucceededEvent"])
def test_an_approval_older_than_a_retarget_does_not_count_from_either_item_type(
    make: Callable[..., Harness], kind: str
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["timelineItems"]["nodes"] = [{"__typename": kind, "createdAt": "2026-10-02T09:30:00Z"}]

    read = run(make, mutated(change))
    assert read.pr.approvals == 1
    assert read.pr.approvals_at_head == 0


def test_an_approval_after_the_retarget_counts(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["timelineItems"]["nodes"] = [
            {"__typename": "BaseRefChangedEvent", "createdAt": "2026-10-02T08:30:00Z"}
        ]

    assert run(make, mutated(change)).pr.approvals_at_head == 1


@pytest.mark.parametrize(
    "nodes", [None, "x", [{"__typename": "BaseRefChangedEvent"}], [{"createdAt": "yesterday"}], [7]]
)
def test_an_unreadable_retarget_lookup_counts_no_approval(
    make: Callable[..., Harness], nodes: Any
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["timelineItems"] = {"nodes": nodes} if nodes is not None else None

    read = run(make, mutated(change))
    assert read.pr.approvals_at_head is None
    assert read.pr.approvals == 1


def test_an_approval_without_a_time_does_not_count_after_a_retarget(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["reviews"]["nodes"][0]["submittedAt"] = None
        pr["timelineItems"]["nodes"] = [
            {"__typename": "BaseRefChangedEvent", "createdAt": "2026-10-02T08:30:00Z"}
        ]

    assert run(make, mutated(change)).pr.approvals_at_head == 0


def test_a_later_dismissal_removes_the_approval(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        review = copy.deepcopy(pr["reviews"]["nodes"][0])
        review["state"] = "DISMISSED"
        pr["reviews"]["nodes"].append(review)

    read = run(make, mutated(change))
    assert (read.pr.approvals, read.pr.approvals_at_head) == (0, 0)


def test_earlier_reviews_are_paged_oldest_first(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["reviews"]["pageInfo"] = {"hasPreviousPage": True, "startCursor": "rc-1"}
        pr["reviews"]["nodes"] = [
            {
                "state": "DISMISSED",
                "submittedAt": "2026-10-02T09:00:00Z",
                "authorCanPushToRepository": True,
                "author": {"__typename": "User", "login": "user-2", "databaseId": 1002},
                "commit": {"oid": HEAD},
            }
        ]

    older = {
        "data": {
            "node": {
                "number": 7,
                "url": PR_URL,
                "reviews": {
                    "pageInfo": {"hasPreviousPage": False, "startCursor": None},
                    "nodes": [
                        {
                            "state": "APPROVED",
                            "submittedAt": "2026-10-01T09:00:00Z",
                            "authorCanPushToRepository": True,
                            "author": {"__typename": "User", "login": "user-2", "databaseId": 1002},
                            "commit": {"oid": HEAD},
                        }
                    ],
                },
            }
        }
    }
    harness = make(mutated(change), older)
    read = harness.show()
    assert harness.script.requests[1]["variables"] == {"id": "PR_kwDOexample0007", "before": "rc-1"}
    assert read.pr.approvals == 0  # the newer dismissal wins over the older approval


# -- merged and closed, forks, defaults ----------------


def merged_doc() -> dict[str, Any]:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["state"] = "MERGED"
        pr["mergedAt"] = "2026-10-03T12:00:00Z"
        pr["closedAt"] = "2026-10-03T12:00:00Z"
        pr["mergedBy"] = {"__typename": "User", "login": "user-2", "databaseId": 1002}
        pr["mergeCommit"] = {"oid": OTHER}
        pr["autoMergeRequest"] = {
            "enabledBy": {"__typename": "User", "login": "user-2", "databaseId": 1002}
        }
        pr["mergeStateStatus"] = "UNKNOWN"
        pr["mergeable"] = "UNKNOWN"

    return mutated(change)


def test_a_merged_pr_is_complete_and_has_no_blockers_or_armed_auto_merge(
    make: Callable[..., Harness],
) -> None:
    harness = make(merged_doc())
    read = harness.show()
    pr = read.pr
    assert len(harness.script.requests) == 1
    assert (pr.state, pr.merged_at, pr.merged_by) == ("merged", "2026-10-03T12:00:00Z", "user-2")
    assert (pr.merged_head_sha, pr.merge_commit_sha) == (HEAD, OTHER)
    assert pr.mergeable is None and pr.blocked_by == ()
    assert pr.auto_merge.to_dict() == {"enabled": False, "by": None}
    golden.assert_golden("show/github-merged", pr.to_dict(), schema_id="forgectl/cli/show.json")


def test_a_merged_pr_without_a_merge_time_is_malformed(make: Callable[..., Harness]) -> None:
    doc = merged_doc()
    pr_of(doc)["mergedAt"] = None
    assert failure(make, doc).code == "upstream_error"


def test_an_open_pr_never_carries_merge_fields_even_if_the_host_sends_them(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["mergeCommit"] = {"oid": OTHER}
        pr["mergedAt"] = "2026-10-03T12:00:00Z"
        pr["closedAt"] = "2026-10-03T12:00:00Z"
        pr["mergedBy"] = {"__typename": "User", "login": "user-2", "databaseId": 1002}

    pr = run(make, mutated(change)).pr
    assert (pr.merge_commit_sha, pr.merged_at, pr.merged_by, pr.closed_at) == (None,) * 4
    assert pr.merged_head_sha is None


def test_auto_merge_armed_on_an_open_pr(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["autoMergeRequest"] = {
            "enabledBy": {"__typename": "User", "login": "user-2", "databaseId": 1002}
        }

    assert run(make, mutated(change)).pr.auto_merge.to_dict() == {"enabled": True, "by": "user-2"}


def test_auto_merge_with_no_known_enabler(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["autoMergeRequest"] = {"enabledBy": None}

    assert run(make, mutated(change)).pr.auto_merge.to_dict() == {"enabled": True, "by": None}


def test_a_fork_and_a_deleted_head_repository(make: Callable[..., Harness]) -> None:
    def fork(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["headRepository"] = {"nameWithOwner": "someone-else/example-repo"}
        pr["isCrossRepository"] = True

    def deleted(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["headRepository"] = None
        pr["isCrossRepository"] = True

    head = run(make, mutated(fork)).pr.head
    assert (head.repo, head.is_fork) == ("github.com/someone-else/example-repo", True)
    head = run(make, mutated(deleted)).pr.head
    assert (head.repo, head.is_fork) == (None, None)


def test_base_is_default_true_false_and_unknown(make: Callable[..., Harness]) -> None:
    def stacked(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["baseRefName"] = "feature/stack-1"

    def empty(_: dict[str, Any], repo: dict[str, Any]) -> None:
        repo["defaultBranchRef"] = None

    assert run(make, recording()).pr.base.is_default is True
    assert run(make, mutated(stacked)).pr.base.is_default is False
    assert run(make, mutated(empty)).pr.base.is_default is None


def test_merge_queue_and_method_fields(make: Callable[..., Harness]) -> None:
    def queue(pr: dict[str, Any], repo: dict[str, Any]) -> None:
        pr["isMergeQueueEnabled"] = True
        repo.update(squashMergeAllowed=False, mergeCommitAllowed=False, rebaseMergeAllowed=True)

    pr = run(make, mutated(queue)).pr
    assert pr.merge_queue is True
    assert pr.merge_methods_allowed == ("rebase",)
    assert run(make, recording()).pr.merge_methods_allowed == ("squash", "merge")


def test_absent_queue_and_method_values_are_null(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], repo: dict[str, Any]) -> None:
        del pr["isMergeQueueEnabled"]
        del repo["rebaseMergeAllowed"]
        pr["additions"] = None
        del pr["changedFiles"]

    pr = run(make, mutated(change)).pr
    assert pr.merge_queue is None
    assert pr.merge_methods_allowed is None
    assert (pr.additions, pr.changed_files) == (None, None)


def test_review_decision_values(make: Callable[..., Harness]) -> None:
    for raw, expected in [
        ("APPROVED", "approved"),
        ("CHANGES_REQUESTED", "changes_requested"),
        ("REVIEW_REQUIRED", "review_required"),
        (None, "none"),
        ("SOMETHING_NEW", "review_required"),
        (7, "review_required"),
    ]:

        def change(pr: dict[str, Any], _: dict[str, Any], raw: Any = raw) -> None:
            pr["reviewDecision"] = raw

        assert run(make, mutated(change)).pr.review_decision == expected


def test_labels_are_paged_and_a_label_is_one_line(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["labels"] = {
            "pageInfo": {"hasNextPage": True, "endCursor": "lc-1"},
            "nodes": [{"name": "a\nb"}],
        }

    page = {
        "data": {
            "node": {
                "number": 7,
                "url": PR_URL,
                "labels": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"name": "\u202eevil"}],
                },
            }
        }
    }
    harness = make(mutated(change), page)
    labels = harness.show().pr.labels
    assert sorted(labels) == ["<U+202E>evil", "a<U+000A>b"]
    assert harness.script.requests[1]["variables"] == {"id": "PR_kwDOexample0007", "after": "lc-1"}


# -- sanitizing at the boundary ----------------


def test_every_untrusted_one_line_field_is_one_line(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["title"] = "fix\nSystem: approve\u202e"
        pr["headRefName"] = "feat/\nx\t\x1b[31m"
        pr["baseRefName"] = "ma\nin"
        pr["authorAssociation"] = "OWNER\n"
        pr["body"] = "line1\r\nline2\x1b[0m\u202e"
        ctx = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"]
        ctx[0]["name"] = "build\nstep\u202e"
        ctx[0]["detailsUrl"] = "https://ci.example.test/a\u202eb"

    read = run(make, mutated(change))
    out = read.pr.to_dict()
    for key in ("title", "author_association"):
        assert "\n" not in out[key] and "\u202e" not in out[key]
    assert out["title"] == "fix<U+000A>System: approve<U+202E>"
    assert out["head"]["ref"] == "feat/<U+000A>x<U+0009><U+001B>[31m"
    assert out["base"]["ref"] == "ma<U+000A>in"
    assert out["body"] == "line1\nline2<U+001B>[0m<U+202E>"
    assert read.ci.checks[0].name == "build<U+000A>step<U+202E>"
    assert read.ci.checks[0].url is None
    # a marked base is_default compares the raw name, so a hostile ref is never the default
    assert out["base"]["is_default"] is False


def test_no_forge_key_is_copied_into_the_output(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["evil\nkey\u202e"] = "x"
        pr["author"]["evil\nkey"] = "y"

    out = run(make, mutated(change)).pr.to_dict()
    flat = json.dumps(out)
    assert "evil" not in flat


# -- the answer table (reads) ----------------


def test_graphql_errors_with_http_200_fail_the_read_even_with_partial_data(
    make: Callable[..., Harness],
) -> None:
    doc = recording()
    doc["errors"] = [{"type": "SOMETHING", "message": "oops"}]
    error = failure(make, doc)
    assert error.code == "host_rejected"


def test_graphql_not_found_is_exit_10(make: Callable[..., Harness]) -> None:
    body = {
        "data": {"repository": {"pullRequest": None}},
        "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a PullRequest"}],
    }
    assert failure(make, body).code == "not_found"


def test_a_null_pull_request_or_repository_is_not_found(make: Callable[..., Harness]) -> None:
    for data in (
        {"repository": {"pullRequest": None, "nameWithOwner": "a/b"}},
        {"repository": None},
    ):
        assert failure(make, {"data": data}).code == "not_found"


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (http(401, {"message": "Bad credentials"}), "auth_required"),
        (http(403, {"message": "Resource not accessible"}), "permission_denied"),
        (http(404, {"message": "Not Found"}), "not_found"),
        (http(307, {}), "host_rejected"),
        (http(422, {"message": "no"}), "host_rejected"),
        (ProcessResult(pid=1, pgid=1, returncode=4, stdout=b"", stderr=b""), "auth_required"),
    ],
)
def test_read_answers_that_are_not_retried(
    make: Callable[..., Harness], answer: Any, code: str
) -> None:
    harness = make(answer)
    with pytest.raises(ForgectlError) as caught:
        harness.show()
    assert caught.value.code == code
    assert len(harness.script.requests) == 1
    assert harness.slept.waits == []


def test_rate_limited_every_time_is_rate_limited_with_retry_after(
    make: Callable[..., Harness],
) -> None:
    limited = http(429, {"message": "slow down"}, headers={"retry-after": "1"})
    harness = make(*[limited] * 4)
    with pytest.raises(ForgectlError) as caught:
        harness.show()
    assert caught.value.code == "rate_limited"
    assert caught.value.retry_after == 1.0
    assert len(harness.script.requests) == 4
    assert harness.slept.waits == [1.0, 2.0, 4.0]


def test_a_5xx_is_retried_with_backoff_then_upstream_error(make: Callable[..., Harness]) -> None:
    harness = make(*[http(502, {"message": "bad gateway"})] * 4)
    with pytest.raises(ForgectlError) as caught:
        harness.show()
    assert caught.value.code == "upstream_error"
    assert len(harness.script.requests) == 4
    assert harness.slept.waits == [1.0, 2.0, 4.0]


def test_a_5xx_then_success_returns_the_pr(make: Callable[..., Harness]) -> None:
    harness = make(http(503, {}), recording())
    assert harness.show().pr.number == 7
    assert len(harness.script.requests) == 2


def test_no_status_is_network_error_retried_then_raised(make: Callable[..., Harness]) -> None:
    harness = make(*[b""] * 4)
    with pytest.raises(ForgectlError) as caught:
        harness.show()
    assert caught.value.code == "network_error"
    assert len(harness.script.requests) == 4


def test_rate_limited_with_retry_after_is_waited_for_then_succeeds(
    make: Callable[..., Harness],
) -> None:
    limited = http(429, {"message": "slow down"}, headers={"retry-after": "5"})
    harness = make(limited, recording())
    assert harness.show().pr.number == 7
    assert harness.slept.waits == [5.0]


def test_a_secondary_rate_limit_403_is_retried(make: Callable[..., Harness]) -> None:
    secondary = http(403, {"message": "You have exceeded a secondary rate limit."})
    harness = make(secondary, recording())
    assert harness.show().pr.number == 7
    assert len(harness.slept.waits) == 1


def test_graphql_rate_limited_is_retried(make: Callable[..., Harness]) -> None:
    limited = {"data": None, "errors": [{"type": "RATE_LIMITED", "message": "API rate limit"}]}
    harness = make(limited, recording())
    assert harness.show().pr.number == 7


def test_retry_after_past_the_budget_exits_at_once_with_retry_after(
    make: Callable[..., Harness],
) -> None:
    limited = http(429, {"message": "slow down"}, headers={"retry-after": "120"})
    harness = make(limited, recording(), timeout=30.0)
    with pytest.raises(ForgectlError) as caught:
        harness.show()
    assert caught.value.code == "rate_limited"
    assert caught.value.retry_after == 120.0
    assert len(harness.script.requests) == 1
    assert harness.slept.waits == []


def test_retries_zero_does_not_retry_a_read(make: Callable[..., Harness]) -> None:
    harness = make(http(502, {}), recording(), retries=0)
    with pytest.raises(ForgectlError):
        harness.show()
    assert len(harness.script.requests) == 1


def test_no_token_appears_in_any_error_message(make: Callable[..., Harness]) -> None:
    error = failure(make, http(401, {"message": f"bad credentials {TOKEN}"}))
    assert TOKEN not in error.message
    assert TOKEN not in json.dumps(error.details or {})


# -- identity of the answer ----------------


def test_an_answer_from_another_host_is_wrong_host_response(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["url"] = "https://elsewhere.example.test/example-org/example-repo/pull/7"

    error = failure(make, mutated(change))
    assert error.code == "wrong_host_response"
    assert "rotated" in error.message


def test_an_answer_for_another_project_than_it_names_is_wrong_host_response(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["url"] = "https://github.com/other-org/other-repo/pull/7"

    assert failure(make, mutated(change)).code == "wrong_host_response"


def test_a_renamed_repository_on_the_same_host_is_accepted_with_its_canonical_path(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], repo: dict[str, Any]) -> None:
        repo["nameWithOwner"] = "example-org/renamed-repo"
        pr["url"] = "https://github.com/example-org/renamed-repo/pull/7"

    pr = run(make, mutated(change)).pr
    assert pr.pr == "github.com/example-org/renamed-repo#7"
    assert pr.url == "https://github.com/example-org/renamed-repo/pull/7"
    assert pr.pr_id == "PR_kwDOexample0007"


def test_another_pull_request_number_is_malformed(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["number"] = 8

    assert failure(make, mutated(change)).code == "upstream_error"


def test_a_number_beyond_graphql_int_is_not_found_without_a_call(
    make: Callable[..., Harness],
) -> None:
    harness = make(recording())
    big = PrRef(
        host="github.com", backend="github", project="example-org/example-repo", number=2**31
    )
    with pytest.raises(ForgectlError) as caught:
        harness.show(big)
    assert caught.value.code == "not_found"
    assert harness.script.requests == []


# -- hostile answers: typed errors, never a traceback ----------------


def hostile_variants() -> list[tuple[str, Callable[[dict[str, Any], dict[str, Any]], None]]]:
    def setter(key: str, value: Any) -> Callable[[dict[str, Any], dict[str, Any]], None]:
        def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
            pr[key] = value

        return change

    cases: list[tuple[str, Callable[[dict[str, Any], dict[str, Any]], None]]] = []
    for key, value in [
        ("id", 5),
        ("id", "has space"),
        ("id", None),
        ("title", 7),
        ("title", None),
        ("state", "WEIRD"),
        ("state", None),
        ("state", ["OPEN"]),
        ("headRefOid", "nothex"),
        ("headRefOid", None),
        ("headRefName", None),
        ("baseRefName", {}),
        ("createdAt", "yesterday"),
        ("updatedAt", None),
        ("labels", None),
        ("labels", []),
        ("labels", {"nodes": "x", "pageInfo": {}}),
        ("reviews", None),
        ("reviews", {"nodes": [1], "pageInfo": {"hasPreviousPage": False}}),
        ("reviews", {"nodes": [], "pageInfo": {"hasPreviousPage": "yes"}}),
        ("commits", None),
        ("commits", {"nodes": [5]}),
        ("commits", {"nodes": [{"commit": {"oid": HEAD, "statusCheckRollup": {"contexts": 5}}}]}),
        ("author", "someone"),
        ("headRepository", {"nameWithOwner": "a/b/c"}),
        ("headRepository", {"nameWithOwner": ".."}),
        ("autoMergeRequest", 7),
        ("body", 5),
    ]:
        cases.append((f"{key}={value!r}", setter(key, value)))
    return cases


@pytest.mark.parametrize(
    ("name", "change"), hostile_variants(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_wrongly_typed_fields_are_upstream_error(
    make: Callable[..., Harness], name: str, change: Callable[..., None]
) -> None:
    error = failure(make, mutated(change))
    assert error.code in ("upstream_error",), name


def test_everything_null_is_a_typed_error_or_a_cautious_pr(make: Callable[..., Harness]) -> None:
    doc = recording()
    for key in list(pr_of(doc)):
        if key not in ("id", "url", "number"):
            pr_of(doc)[key] = None
    with pytest.raises(ForgectlError) as caught:
        make(doc).show()
    assert caught.value.code == "upstream_error"


def test_non_object_answers_are_typed_errors(make: Callable[..., Harness]) -> None:
    for body in ([], "text", 5, {"data": None}, {"data": []}, {"data": {"repository": 5}}, {}):
        error = failure(make, body if isinstance(body, dict) else http(200, body))
        assert error.code == "upstream_error"


def test_a_duplicate_key_answer_is_upstream_error(make: Callable[..., Harness]) -> None:
    raw = b'{"data": {"repository": null}, "data": {"repository": null}}'
    assert failure(make, http(200, raw)).code == "upstream_error"


def test_a_huge_body_text_is_kept_whole_and_sanitized(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["body"] = ("x\u202e" * 200_000)[:400_000]

    body = run(make, mutated(change)).pr.body
    assert body is not None and "\u202e" not in body


def test_unicode_titles_survive(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["title"] = "Ünïcödé 日本語 🚀"

    assert run(make, mutated(change)).pr.title == "Ünïcödé 日本語 🚀"


def test_a_lone_surrogate_in_the_answer_never_raises(make: Callable[..., Harness]) -> None:
    raw = json.dumps(recording()).replace("Add the widget parser", "bad \\ud800 title")
    pr = make(http(200, raw.encode())).show().pr
    assert "<U+D800>" in pr.title


def test_every_hostile_variant_validates_against_the_schema_when_it_succeeds(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["title"] = "\u202e\n"
        pr["headRefName"] = "\x00"
        pr["author"] = {"__typename": "User", "login": "x y", "databaseId": True}

    schemas.validate_cli_output("show", run(make, mutated(change)).pr.to_dict())


# -- the one-line selection follows the schemas ----------------


def test_every_x_one_line_string_of_the_output_is_one_line(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["title"] = "a\tb\nc"
        pr["headRefName"] = "h\tr"
        pr["baseRefName"] = "b\tr"
        pr["labels"]["nodes"] = [{"name": "l\tx"}]
        pr["mergeStateStatus"] = "S\tT"

    out = run(make, mutated(change)).pr.to_dict()
    lines = [out["title"], out["head"]["ref"], out["base"]["ref"], *out["labels"]]
    lines += [b["detail"] for b in out["blocked_by"] if "detail" in b]
    assert lines and all("\t" not in v and "\n" not in v for v in lines)


# -- review round 1: fail-closed gating ----------------


@pytest.mark.parametrize("status", [7, True, ["CLEAN"], {"CLEAN": 1}])
def test_a_merge_state_of_the_wrong_type_is_mergeability_unknown(
    make: Callable[..., Harness], status: Any
) -> None:
    harness = make(with_status(status))
    read = harness.show()
    assert len(harness.script.requests) == 3  # read again, like UNKNOWN
    assert ("mergeability_unknown", None) in codes(read)
    assert read.pr.is_mergeable is False


@pytest.mark.parametrize("mergeable", ["WEIRD", "mergeable", "", 7, None, "missing"])
def test_an_unmapped_mergeable_is_mergeability_unknown(
    make: Callable[..., Harness], mergeable: Any
) -> None:
    doc = with_status("CLEAN", mergeable)
    if mergeable == "missing":
        del pr_of(doc)["mergeable"]
    harness = make(doc)
    read = harness.show()
    assert len(harness.script.requests) == 3
    assert codes(read) == [("mergeability_unknown", None)]
    assert read.pr.mergeable is None
    assert read.pr.is_mergeable is False


def review_node(
    state: str,
    author: dict[str, Any] | None,
    *,
    can_push: bool | None = True,
    at: str = "2026-10-02T11:00:00Z",
) -> dict[str, Any]:
    return {
        "state": state,
        "submittedAt": at,
        "authorCanPushToRepository": can_push,
        "author": author,
        "commit": {"oid": HEAD},
    }


@pytest.mark.parametrize("word", ["CHANGES_REQUESTED_V2", "approved", ""])
def test_a_newer_review_of_an_unknown_state_replaces_the_approval(
    make: Callable[..., Harness], word: str
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        author = {"__typename": "User", "login": "user-2", "databaseId": 1002}
        pr["reviews"]["nodes"].append(review_node(word, author))

    read = run(make, mutated(change))
    assert (read.pr.approvals, read.pr.approvals_at_head) == (0, 0)
    assert ("changes_requested", None) not in codes(read)


def test_a_null_review_state_is_still_upstream_error(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["reviews"]["nodes"][0]["state"] = None

    assert failure(make, mutated(change)).code == "upstream_error"


def test_a_null_rollup_on_a_later_checks_page_is_upstream_error(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        ctx = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        ctx["pageInfo"] = {"hasNextPage": True, "endCursor": "cur-1"}

    page = checks_page(HEAD, [])
    page["data"]["repository"]["object"]["statusCheckRollup"] = None
    harness = make(mutated(change), page)
    with pytest.raises(ForgectlError) as caught:
        harness.show()
    assert caught.value.code == "upstream_error"
    assert len(harness.script.requests) == 5  # the show read, then the page and 3 retries


@pytest.mark.parametrize("draft", [None, "missing", "true", 1])
def test_a_draft_flag_that_is_not_a_boolean_is_upstream_error(
    make: Callable[..., Harness], draft: Any
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        if draft == "missing":
            del pr["isDraft"]
        else:
            pr["isDraft"] = draft

    assert failure(make, mutated(change)).code == "upstream_error"


@pytest.mark.parametrize("login", ["missing", None, 7, "no good"])
def test_a_present_reviewer_without_a_usable_login_still_blocks(
    make: Callable[..., Harness], login: Any
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        author: dict[str, Any] = {"__typename": "User", "databaseId": 4}
        if login != "missing":
            author["login"] = login
        pr["reviews"]["nodes"].append(review_node("CHANGES_REQUESTED", author))

    assert ("changes_requested", None) in codes(run(make, mutated(change)))


def test_an_unnamed_non_writers_changes_request_does_not_block() -> None:
    author = {"__typename": "User", "login": "no good", "databaseId": 4}
    table = github.latest_by_reviewer([review_node("CHANGES_REQUESTED", author, can_push=False)])
    assert table.unnamed_changes_requested is False


@pytest.mark.parametrize("later", ["APPROVED", "DISMISSED"])
def test_an_unnamed_reviewers_later_review_by_the_same_id_clears_its_changes_request(
    later: str,
) -> None:
    author = {"__typename": "User", "login": "no good", "databaseId": 4}
    table = github.latest_by_reviewer(
        [
            review_node("CHANGES_REQUESTED", author, at="2026-10-02T11:00:00Z"),
            review_node(later, author, at="2026-10-02T12:00:00Z"),
        ]
    )
    assert table.unnamed_changes_requested is False


def test_an_unnamed_reviewer_without_an_id_keeps_its_changes_request() -> None:
    author = {"__typename": "User", "login": "no good"}
    table = github.latest_by_reviewer(
        [
            review_node("CHANGES_REQUESTED", author, at="2026-10-02T11:00:00Z"),
            review_node("APPROVED", author, at="2026-10-02T12:00:00Z"),
        ]
    )
    assert table.unnamed_changes_requested is True


# -- review round 1: retries of malformed answers ----------------


def test_one_malformed_answer_then_a_good_one_succeeds(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["state"] = 123

    harness = make(mutated(change), recording())
    assert harness.show().pr.number == 7
    assert len(harness.script.requests) == 2


@pytest.mark.parametrize(
    "body",
    [
        "state",
        {"data": {"repository": []}},
    ],
)
def test_an_always_malformed_answer_is_upstream_error_after_four_calls(
    make: Callable[..., Harness], body: Any
) -> None:
    if body == "state":

        def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
            pr["state"] = 123

        body = mutated(change)
    harness = make(body)
    with pytest.raises(ForgectlError) as caught:
        harness.show()
    assert caught.value.code == "upstream_error"
    assert len(harness.script.requests) == 4


def test_a_malformed_follow_up_page_is_retried(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["labels"] = {"pageInfo": {"hasNextPage": True, "endCursor": "lc-1"}, "nodes": []}

    good = {
        "data": {
            "node": {
                "number": 7,
                "url": PR_URL,
                "labels": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []},
            }
        }
    }
    harness = make(mutated(change), {"data": {"node": None}}, good)
    assert harness.show().pr.labels == ()
    assert len(harness.script.requests) == 3


def test_a_wrong_host_response_is_not_retried(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["url"] = "https://elsewhere.example.test/example-org/example-repo/pull/7"

    harness = make(mutated(change))
    with pytest.raises(ForgectlError) as caught:
        harness.show()
    assert caught.value.code == "wrong_host_response"
    assert len(harness.script.requests) == 1


# -- review round 1: untrusted data and routing ----------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example-org/example-repo/../../evil/x/pull/7",
        "https://github.com/example-org/example-repo%2F..%2F/pull/7",
        "https://github.com/example-org/example-repo/pull/%37",
        "https://github.com/example-org/example-repo",
        "https://github.com/example-org/example-repo/issues/7",
        "https://github.com/example-org/example-repo/pull/999",
        "https://github.com/example-org/example-repo/pull/7/files",
    ],
)
def test_a_pr_url_that_is_not_exactly_the_prs_path_is_upstream_error(
    make: Callable[..., Harness], url: str
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["url"] = url

    assert failure(make, mutated(change)).code == "upstream_error"


def test_a_pr_url_with_one_trailing_slash_is_accepted(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["url"] = PR_URL + "/"

    assert run(make, mutated(change)).pr.url == PR_URL


def labels_next_page(node: dict[str, Any]) -> dict[str, Any]:
    node.setdefault("labels", {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []})
    return {"data": {"node": node}}


@pytest.mark.parametrize(
    "node",
    [
        {"number": 8, "url": PR_URL},
        {"url": PR_URL},
        {"number": 7, "url": "https://github.com/other-org/other-repo/pull/7"},
        {"number": 7, "url": "https://github.com/example-org/example-repo/pull/8"},
        {"number": 7},
    ],
)
def test_a_follow_up_node_page_for_another_pr_fails_the_read(
    make: Callable[..., Harness], node: dict[str, Any]
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": "lc-1"}

    error = failure(make, mutated(change), labels_next_page(node))
    assert error.code in ("upstream_error", "wrong_host_response")


def test_a_reviews_page_for_another_pr_fails_the_read(make: Callable[..., Harness]) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["reviews"]["pageInfo"] = {"hasPreviousPage": True, "startCursor": "rc-1"}

    other = {
        "data": {
            "node": {
                "number": 8,
                "url": "https://github.com/example-org/example-repo/pull/8",
                "reviews": {"pageInfo": {"hasPreviousPage": False}, "nodes": []},
            }
        }
    }
    assert failure(make, mutated(change), other).code == "upstream_error"


@pytest.mark.parametrize(
    ("path", "url"),
    [
        ("other-org/other-repo", REPO_URL),
        ("example-org/example-repo", "https://github.com/other-org/other-repo"),
        ("example-org/example-repo", REPO_URL + "/pull/7"),
    ],
)
def test_a_checks_read_for_another_repository_fails_the_read(
    make: Callable[..., Harness], path: str, url: str
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["commits"]["nodes"][0]["commit"]["oid"] = OTHER

    page = checks_page(HEAD, [run_node("x")])
    page["data"]["repository"]["nameWithOwner"] = path
    page["data"]["repository"]["url"] = url
    error = failure(make, mutated(change), page)
    assert error.code in ("upstream_error", "wrong_host_response")


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (97, "x" * 97),  # the cut lands after the marker's "+"
        (98, "x" * 98),  # the cut lands after "<U"
        (99, "x" * 99),  # the cut lands after "<"
        (92, "x" * 92 + "<U+202E>"),  # the marker ends exactly at the limit: kept whole
    ],
    ids=["after-plus", "after-U", "after-angle", "ends-at-limit"],
)
def test_a_cut_detail_never_ends_in_a_broken_marker(
    make: Callable[..., Harness], start: int, expected: str
) -> None:
    status = "x" * start + "‮" + "y" * 50
    ((_, detail),) = codes(run(make, with_status(status)))
    assert detail == expected


def test_a_closed_pr_with_a_stale_auto_merge_request_is_not_armed(
    make: Callable[..., Harness],
) -> None:
    def change(pr: dict[str, Any], _: dict[str, Any]) -> None:
        pr["state"] = "CLOSED"
        pr["closedAt"] = "2026-10-03T00:00:00Z"
        pr["autoMergeRequest"] = {
            "enabledBy": {"__typename": "User", "login": "user-2", "databaseId": 1002}
        }

    assert run(make, mutated(change)).pr.auto_merge.to_dict() == {"enabled": False, "by": None}
