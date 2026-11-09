"""GitHub `prs`, `access` and `whoami` on one host (#14): the search query, its pagination and
limits, the permission read, and every hostile answer.

The gateway is real; its process runner is a script that answers like `gh api -i` does. The
recordings under tests/recordings/github/lists are hand-built from GitHub's documented GraphQL
and REST shapes, scrubbed of real names. Every assumption about GitHub's real behavior is
UNVERIFIED until the live suite (#31) re-checks it; see the report of #14 for the list.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from forgectl.backends import github_lists
from forgectl.backends.github import GitHubBackend
from forgectl.backends.github_lists import PRS_QUERY, build_search
from forgectl.config import Host, LoadedConfig
from forgectl.errors import ForgectlError
from forgectl.gateway import Gateway
from forgectl.hosts import Hosts
from forgectl.identifiers import RepoRef
from forgectl.model import AccessResult, PrPage, PrQuery
from forgectl.reads import ReadEnv

from .harness import golden, schemas
from .harness.fixtures import forgectl_env
from .harness.forges import FakeForges
from .test_github_show import Script, Slept, http

RECORDINGS = Path(__file__).parent / "recordings" / "github" / "lists"
TOKEN = "tok-lists-0001-zzzz"
REPO = RepoRef(host="github.com", backend="github", path="example-org/example-repo")


def recording(name: str = "search-open") -> dict[str, Any]:
    return json.loads((RECORDINGS / f"{name}.json").read_text(encoding="utf-8"))


def nodes_of(doc: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = doc["data"]["search"]["nodes"]
    return nodes


def search_of(doc: dict[str, Any]) -> dict[str, Any]:
    search: dict[str, Any] = doc["data"]["search"]
    return search


class Harness:
    def __init__(
        self, forges: FakeForges, script: Script, *, retries: int = 3, timeout: float = 60.0
    ):
        config = LoadedConfig.defaults()
        config = LoadedConfig(
            config=config.config.model_copy(
                update={"hosts": {"github.com": Host(backend="github", token_env="LISTS_TOKEN")}}
            ),
            config_hash="x",
            config_path=None,
        )
        self.script = script
        self.slept = Slept()
        gateway = Gateway(
            config,
            timeout=timeout,
            environ=forgectl_env(forges, {"LISTS_TOKEN": TOKEN}),
            runner=script,  # type: ignore[arg-type]
        )
        self.env = ReadEnv(
            gateway=gateway,
            hosts=Hosts(gateway),
            retries=retries,
            sleep=self.slept.sleep,
            clock=self.slept,
            jitter=lambda: 0.0,
        )
        self.backend = GitHubBackend(self.env)

    def prs(self, **kwargs: Any) -> PrPage:
        kwargs.setdefault("host", "github.com")
        return self.backend.list_prs(PrQuery(**kwargs))

    def access(self, user: str = "user-1", repo: RepoRef = REPO) -> AccessResult:
        return self.backend.access(repo, user)


@pytest.fixture
def make(fake_forges: FakeForges) -> Callable[..., Harness]:
    def build(*answers: Any, **kwargs: Any) -> Harness:
        return Harness(fake_forges, Script(*answers), **kwargs)

    return build


def failure(harness: Harness, call: Callable[[Harness], Any]) -> ForgectlError:
    with pytest.raises(ForgectlError) as caught:
        call(harness)
    return caught.value


# -- the search string: built only from validated parts --------------------------------------


def test_the_search_for_a_repo_with_defaults() -> None:
    q = build_search(PrQuery(repo=REPO, host="github.com"))
    assert q == "is:pr is:open repo:example-org/example-repo sort:updated-desc"


def test_every_filter_has_one_qualifier() -> None:
    q = build_search(
        PrQuery(
            host="github.com",
            author="user-1",
            review_requested_from="user-2",
            state="merged",
            draft=False,
            updated_since="2026-10-01T00:00:00Z",
        )
    )
    assert q.split(" ") == [
        "is:pr",
        "is:merged",
        "author:user-1",
        "user-review-requested:user-2",
        "draft:false",
        "updated:>=2026-10-01T00:00:00Z",
        "sort:updated-desc",
    ]


@pytest.mark.parametrize(
    ("state", "words"),
    [
        ("open", ["is:open"]),
        ("closed", ["is:closed", "is:unmerged"]),
        ("merged", ["is:merged"]),
        ("all", []),
    ],
)
def test_the_state_qualifiers(state: str, words: list[str]) -> None:
    q = build_search(PrQuery(repo=REPO, host="github.com", state=state))  # type: ignore[arg-type]
    assert q.split(" ")[1 : 1 + len(words)] == words


def test_draft_true_and_unset() -> None:
    assert "draft:true" in build_search(PrQuery(repo=REPO, host="github.com", draft=True)).split()
    assert "draft" not in build_search(PrQuery(repo=REPO, host="github.com"))


def test_a_bot_author_is_searched_by_its_app_form() -> None:
    q = build_search(PrQuery(host="github.com", author="user-3[bot]"))
    assert "author:app/user-3" in q.split()
    assert "[bot]" not in q


HOSTILE_LOGINS = [
    "user-1 repo:example-org/private",  # a space adds a qualifier
    "user-1\nrepo:other/x",
    "user-1:admin",
    'user-1"',
    "'user-1'",
    "user-1 OR user-2",
    "-user-1",
    "user-1)",
    "user-1\x00",
    "user-1‮",
    "١user",  # an Arabic-Indic digit is not an ASCII digit
    "usеr",  # Cyrillic e
    "",
    " ",
    "a" * 40,
    "user-1[bot]x",
    "user-1[BOT]",
    "*",
    "user-1 ",
    "user-1\n",  # a trailing newline must not slip past an end anchor
]


@pytest.mark.parametrize("value", HOSTILE_LOGINS)
@pytest.mark.parametrize("field", ["author", "review_requested_from"])
def test_a_login_outside_the_grammar_is_refused_never_escaped(value: str, field: str) -> None:
    with pytest.raises(ForgectlError) as caught:
        build_search(PrQuery(host="github.com", **{field: value}))  # type: ignore[arg-type]
    assert caught.value.code == "usage"


@pytest.mark.parametrize(
    "path",
    [
        "example-org/example-repo repo:other/x",
        "example-org/example-repo\n",
        "example-org/",
        "/example-repo",
        "example-org/a/b",
        "example-org",
        "../x",
        "o/..",
        "o/.",
        "o/r:x",
        'o/r"',
        "o/r r",
        "o/r‮",
        "o/١",
    ],
)
def test_a_repo_path_outside_the_grammar_is_refused(path: str) -> None:
    repo = RepoRef(host="github.com", backend="github", path=path)
    with pytest.raises(ForgectlError) as caught:
        build_search(PrQuery(repo=repo, host="github.com"))
    assert caught.value.code == "usage"


@pytest.mark.parametrize(
    "since",
    [
        "2026-10-01",
        "2026-10-01T00:00:00+02:00",
        "2026-10-01T00:00:00Z extra:1",
        "2026-10-01T00:00:00Z\n",
        "2026-10-01T00:00:00.5Z",
        "yesterday",
        "",
        "2026-10-01T00:00:00z",
    ],
)
def test_a_time_that_is_not_utc_seconds_is_refused(since: str) -> None:
    with pytest.raises(ForgectlError) as caught:
        build_search(PrQuery(repo=REPO, host="github.com", updated_since=since))
    assert caught.value.code == "usage"


@pytest.mark.parametrize("limit", [0, -1, 1001, True])
def test_a_limit_outside_1_to_1000_is_refused(limit: int) -> None:
    with pytest.raises(ForgectlError) as caught:
        build_search(PrQuery(repo=REPO, host="github.com", limit=limit))
    assert caught.value.code == "usage"


def test_a_query_without_repo_or_person_is_refused() -> None:
    with pytest.raises(ForgectlError) as caught:
        build_search(PrQuery(host="github.com"))
    assert caught.value.code == "usage"


def test_the_graphql_text_is_constant_with_variables_only() -> None:
    assert "$q" in PRS_QUERY and "search(query: $q" in PRS_QUERY
    assert "example" not in PRS_QUERY
    assert "rateLimit" in PRS_QUERY


# -- listing -----------------------------------------------------------------------------------


def test_golden_search_page(make: Callable[..., Harness]) -> None:
    harness = make(recording())
    page = harness.prs(repo=REPO)
    out = {
        "items": [item.to_dict() for item in page.items],
        "complete": page.complete,
        "errors": [],
        "hosts_queried": ["github.com"],
        "rate_limit": page.rate_limit.to_dict(),
    }
    golden.assert_golden("prs/github-open", out, schema_id="forgectl/cli/prs.json")


def test_the_items_are_normalized_and_ordered(make: Callable[..., Harness]) -> None:
    page = make(recording()).prs(repo=REPO)
    assert [i.pr for i in page.items] == [
        "github.com/example-org/example-repo#31",
        "github.com/example-org/example-repo#29",
        "github.com/example-org/other-repo#12",
    ]
    first, bot, ghost = page.items
    assert first.labels == ("needs review", "bug")
    assert first.head.repo == "github.com/example-org/example-repo"
    assert first.head.is_fork is False
    assert bot.author.login == "user-2[bot]" and bot.author.is_bot is True
    assert bot.draft is True
    assert ghost.author.login is None
    assert ghost.head.repo is None and ghost.head.is_fork is None
    assert page.complete is True
    assert page.rate_limit.host == "github.com"
    assert page.rate_limit.remaining == 4987
    assert page.rate_limit.reset_at == "2026-10-02T09:00:00Z"


def test_the_items_are_sorted_by_updated_at_then_pr_whatever_the_host_order(
    make: Callable[..., Harness],
) -> None:
    doc = recording()
    nodes_of(doc).reverse()
    same = copy.deepcopy(nodes_of(doc)[2])
    same["number"], same["id"] = 5, "PR_kwDOexample5"
    same["url"] = "https://github.com/example-org/example-repo/pull/5"
    same["updatedAt"] = "2026-10-01T14:00:00+02:00"  # the same instant as #29's 12:00:00Z
    nodes_of(doc).append(same)
    search_of(doc)["issueCount"] = 4
    page = make(doc).prs(repo=REPO)
    assert [i.number for i in page.items] == [31, 29, 5, 12]  # equal instants: by the pr string


def test_one_request_with_a_constant_query_and_the_search_as_a_variable(
    make: Callable[..., Harness],
) -> None:
    harness = make(recording())
    harness.prs(author="user-1")
    (request,) = harness.script.requests
    assert request["query"] == PRS_QUERY
    assert request["variables"] == {
        "q": "is:pr is:open author:user-1 sort:updated-desc",
        "first": 100,
        "after": None,
    }
    assert harness.script.argvs[0][-1] == "graphql"


def test_a_limit_below_a_page_asks_for_limit_plus_one(make: Callable[..., Harness]) -> None:
    harness = make(recording())
    harness.prs(repo=REPO, limit=2)
    assert harness.script.requests[0]["variables"]["first"] == 3


def test_a_limit_cuts_the_oldest_and_says_incomplete(make: Callable[..., Harness]) -> None:
    page = make(recording()).prs(repo=REPO, limit=2)
    assert [i.number for i in page.items] == [31, 29]
    assert page.complete is False


def test_exactly_the_limit_is_complete(make: Callable[..., Harness]) -> None:
    page = make(recording()).prs(repo=REPO, limit=3)
    assert len(page.items) == 3 and page.complete is True


def page_of(
    start: int, count: int, *, more: bool, total: int, cursor: str = "Y3Vyc29yOjEwMA=="
) -> dict[str, Any]:
    doc = recording()
    template = nodes_of(doc)[0]
    nodes = []
    for index in range(start, start + count):
        node = copy.deepcopy(template)
        node["number"] = 100_000 - index  # newest first: higher numbers, later times
        node["id"] = f"PR_kwDOexample{index}"
        node["url"] = f"https://github.com/example-org/example-repo/pull/{100_000 - index}"
        node["updatedAt"] = f"2026-10-02T08:{(59 - index // 60) % 60:02d}:{59 - index % 60:02d}Z"
        nodes.append(node)
    doc["data"]["search"] = {
        "issueCount": total,
        "pageInfo": {"hasNextPage": more, "endCursor": cursor if more else None},
        "nodes": nodes,
    }
    return doc


def test_a_second_page_is_read_with_the_cursor_and_the_remainder(
    make: Callable[..., Harness],
) -> None:
    harness = make(page_of(0, 100, more=True, total=130), page_of(100, 30, more=False, total=130))
    page = harness.prs(repo=REPO, limit=130)
    assert len(page.items) == 130 and page.complete is True
    first, second = harness.script.requests
    assert first["variables"]["first"] == 100
    assert second["variables"]["after"] == "Y3Vyc29yOjEwMA=="
    assert second["variables"]["first"] == 31  # limit + 1 minus what is read


def test_the_search_cap_of_1000_is_incomplete_not_complete(make: Callable[..., Harness]) -> None:
    pages = [
        page_of(i * 100, 100, more=i < 9, total=2500, cursor=f"Y3Vyc29yOjE{i}") for i in range(10)
    ]
    harness = make(*pages)
    page = harness.prs(repo=REPO, limit=1000)
    assert len(harness.script.requests) == 10
    assert len(page.items) == 1000
    assert page.complete is False  # issueCount says 2500 matched


def test_a_total_over_the_cap_is_incomplete_even_when_the_last_page_says_no_more(
    make: Callable[..., Harness],
) -> None:
    doc = page_of(0, 3, more=False, total=1500)
    page = make(doc).prs(repo=REPO)
    assert page.complete is False


def test_a_never_ending_cursor_stops_at_the_page_bound(make: Callable[..., Harness]) -> None:
    harness = make(*[page_of(i * 100, 100, more=True, total=5000) for i in range(30)])
    page = harness.prs(repo=REPO, limit=1000)
    assert len(harness.script.requests) <= 10
    assert page.complete is False


def test_an_empty_result_is_complete(make: Callable[..., Harness]) -> None:
    doc = recording()
    doc["data"]["search"] = {
        "issueCount": 0,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [],
    }
    page = make(doc).prs(repo=REPO)
    assert page.items == () and page.complete is True


def test_a_rate_limit_without_numbers_is_null(make: Callable[..., Harness]) -> None:
    doc = recording()
    doc["data"]["rateLimit"] = {"remaining": "many", "resetAt": "soon"}
    page = make(doc).prs(repo=REPO)
    assert page.rate_limit.to_dict() == {"host": "github.com", "remaining": None, "reset_at": None}


def test_a_missing_rate_limit_is_null(make: Callable[..., Harness]) -> None:
    doc = recording()
    del doc["data"]["rateLimit"]
    page = make(doc).prs(repo=REPO)
    assert page.rate_limit.remaining is None


def test_the_lowest_remaining_of_the_pages_is_reported(make: Callable[..., Harness]) -> None:
    first = page_of(0, 100, more=True, total=130)
    first["data"]["rateLimit"] = {"remaining": 10, "resetAt": "2026-10-02T09:00:00Z"}
    second = page_of(100, 30, more=False, total=130)
    second["data"]["rateLimit"] = {"remaining": 8, "resetAt": "2026-10-02T09:01:00Z"}
    page = make(first, second).prs(repo=REPO, limit=130)
    assert page.rate_limit.remaining == 8


def test_more_than_100_labels_are_read_to_the_end(make: Callable[..., Harness]) -> None:
    doc = recording()
    node = nodes_of(doc)[0]
    node["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": "bGFiZWxz"}
    more = {
        "data": {
            "node": {
                "labels": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"name": "extra"}],
                }
            }
        }
    }
    harness = make(doc, more)
    page = harness.prs(repo=REPO)
    assert "extra" in page.items[0].labels
    assert harness.script.requests[1]["variables"] == {
        "id": "PR_kwDOexample31",
        "after": "bGFiZWxz",
    }


# -- hostile answers ---------------------------------------------------------------------------


def test_text_from_the_forge_is_sanitized_per_field(make: Callable[..., Harness]) -> None:
    doc = recording()
    node = nodes_of(doc)[0]
    node["title"] = "t‮itle\nforged: line"
    node["headRefName"] = "br\tanch\x1b[31m"
    node["baseRefName"] = "ba\nse"
    node["labels"]["nodes"] = [{"name": "l​ab\nel"}]
    page = make(doc).prs(repo=REPO)
    out = json.dumps([i.to_dict() for i in page.items])
    for bad in ("\\u202e", "\\n", "\\t", "\\u001b", "\\u200b"):
        assert bad not in out
    schemas.validate(
        {
            "items": [i.to_dict() for i in page.items],
            "complete": True,
            "errors": [],
            "hosts_queried": ["github.com"],
            "rate_limit": page.rate_limit.to_dict(),
        },
        "forgectl/cli/prs.json",
    )


def test_a_node_from_another_host_is_wrong_host_response(make: Callable[..., Harness]) -> None:
    doc = recording()
    nodes_of(doc)[0]["url"] = "https://evil.example.test/example-org/example-repo/pull/31"
    error = failure(make(doc), lambda h: h.prs(repo=REPO))
    assert error.code == "wrong_host_response"
    assert TOKEN not in json.dumps(error.envelope())


def test_a_node_whose_url_names_another_project_is_wrong_host_response(
    make: Callable[..., Harness],
) -> None:
    doc = recording()
    nodes_of(doc)[0]["url"] = "https://github.com/example-org/elsewhere/pull/31"
    assert failure(make(doc), lambda h: h.prs(repo=REPO)).code == "wrong_host_response"


@pytest.mark.parametrize(
    "change",
    [
        lambda n: n.update(number="31"),
        lambda n: n.update(number=True),
        lambda n: n.update(number=-1),
        lambda n: n.update(state="WEIRD"),
        lambda n: n.update(state=None),
        lambda n: n.update(isDraft="yes"),
        lambda n: n.update(title=None),
        lambda n: n.update(title=5),
        lambda n: n.update(headRefOid="zz"),
        lambda n: n.update(headRefOid="A" * 40),
        lambda n: n.update(updatedAt="yesterday"),
        lambda n: n.update(id="a b"),
        lambda n: n.update(id=None),
        lambda n: n.update(repository=None),
        lambda n: n.update(repository={"nameWithOwner": "a/b/c"}),
        lambda n: n.update(repository={"nameWithOwner": "a b/c"}),
        lambda n: n.update(url=None),
        lambda n: n.update(labels=None),
        lambda n: n.update(labels={"nodes": "x", "pageInfo": {}}),
        lambda n: n.update(labels={"nodes": [None], "pageInfo": {"hasNextPage": False}}),
        lambda n: n.update(author="user-1"),
        lambda n: n.update(headRepository="x"),
        lambda n: n.update(baseRefName=None),
        lambda n: n.pop("baseRefName"),
        lambda n: n.pop("headRefName"),
    ],
)
def test_a_node_that_is_not_the_documented_shape_is_upstream_error(
    make: Callable[..., Harness], change: Callable[[dict[str, Any]], Any]
) -> None:
    doc = recording()
    change(nodes_of(doc)[0])
    error = failure(make(doc), lambda h: h.prs(repo=REPO))
    assert error.code == "upstream_error"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["data"].update(search=None),
        lambda d: d["data"].update(search=[]),
        lambda d: d["data"]["search"].update(nodes="x"),
        lambda d: d["data"]["search"].update(nodes=None),
        lambda d: d["data"]["search"].update(issueCount="3"),
        lambda d: d["data"]["search"].update(issueCount=-1),
        lambda d: d["data"]["search"].update(issueCount=True),
        lambda d: d["data"]["search"].update(pageInfo=None),
        lambda d: d["data"]["search"]["pageInfo"].update(hasNextPage="no"),
        lambda d: d["data"]["search"]["pageInfo"].update(hasNextPage=True, endCursor=None),
        lambda d: d["data"]["search"]["pageInfo"].update(hasNextPage=True, endCursor="a b"),
        lambda d: d["data"].update(rateLimit="low"),
    ],
)
def test_a_search_that_is_not_the_documented_shape_is_upstream_error(
    make: Callable[..., Harness], mutate: Callable[[dict[str, Any]], Any]
) -> None:
    doc = recording()
    mutate(doc)
    assert failure(make(doc), lambda h: h.prs(repo=REPO)).code == "upstream_error"


def test_a_node_that_is_not_a_pull_request_is_dropped_and_the_page_is_incomplete(
    make: Callable[..., Harness],
) -> None:
    doc = recording()
    nodes_of(doc).insert(1, {})
    page = make(doc).prs(repo=REPO)
    assert len(page.items) == 3
    assert page.complete is False


def test_a_host_that_ignores_the_state_filter_is_upstream_error(
    make: Callable[..., Harness],
) -> None:
    doc = recording()
    nodes_of(doc)[0]["state"] = "MERGED"
    assert failure(make(doc), lambda h: h.prs(repo=REPO)).code == "upstream_error"


def test_a_host_that_ignores_the_draft_filter_is_upstream_error(
    make: Callable[..., Harness],
) -> None:
    assert failure(make(recording()), lambda h: h.prs(repo=REPO, draft=False)).code == (
        "upstream_error"
    )


def test_a_host_that_ignores_the_updated_filter_is_upstream_error(
    make: Callable[..., Harness],
) -> None:
    error = failure(
        make(recording()), lambda h: h.prs(repo=REPO, updated_since="2026-10-01T10:00:00Z")
    )
    assert error.code == "upstream_error"


def test_a_page_with_more_items_than_asked_is_upstream_error(make: Callable[..., Harness]) -> None:
    assert failure(make(recording()), lambda h: h.prs(repo=REPO, limit=1)).code == "upstream_error"


def test_graphql_errors_fail_the_read_never_a_partial_list(make: Callable[..., Harness]) -> None:
    doc = recording()
    doc["errors"] = [{"type": "FORBIDDEN", "message": "Resource protected by SAML enforcement"}]
    error = failure(make(doc), lambda h: h.prs(repo=REPO))
    assert error.code == "permission_denied"


def test_a_5xx_is_retried_then_read(make: Callable[..., Harness]) -> None:
    harness = make(http(502, {"message": "bad"}), recording())
    page = harness.prs(repo=REPO)
    assert len(page.items) == 3
    assert harness.slept.waits == [0.5]


def test_the_token_is_in_no_error(make: Callable[..., Harness]) -> None:
    error = failure(
        make(http(401, {"message": f"Bad credentials {TOKEN}"})), lambda h: h.prs(repo=REPO)
    )
    assert error.code == "auth_required"
    assert TOKEN not in json.dumps(error.envelope())


def test_a_repo_for_another_host_is_refused(make: Callable[..., Harness]) -> None:
    other = RepoRef(host="ghe.example.test", backend="github", path="o/r")
    error = failure(make(recording()), lambda h: h.prs(repo=other, host="github.com"))
    assert error.code == "internal"


# -- access ------------------------------------------------------------------------------------


def repo_doc() -> dict[str, Any]:
    return recording("repo")


def permission(
    permission: str = "write", role: str | None = "write", login: str = "user-1"
) -> dict[str, Any]:
    doc: dict[str, Any] = {"permission": permission, "user": {"login": login, "id": 1001}}
    if role is not None:
        doc["role_name"] = role
    return doc


def test_golden_access(make: Callable[..., Harness]) -> None:
    result = make(repo_doc(), recording("permission-write")).access()
    golden.assert_golden(
        "access/github-maintain", result.to_dict(), schema_id="forgectl/cli/access.json"
    )


def test_access_reads_the_repo_first_then_the_permission(make: Callable[..., Harness]) -> None:
    harness = make(repo_doc(), permission())
    harness.access()
    first, second = harness.script.argvs
    assert first[-1] == "repos/example-org/example-repo"
    assert second[-1] == "repos/example-org/example-repo/collaborators/user-1/permission"
    assert "GET" in first and "GET" in second


@pytest.mark.parametrize(
    ("perm", "role", "write", "level"),
    [
        ("admin", "admin", True, "admin"),
        ("write", "maintain", True, "maintain"),
        ("write", "write", True, "write"),
        ("write", None, True, "write"),
        ("read", "triage", False, "triage"),
        ("read", "read", False, "read"),
        ("read", None, False, "read"),
        ("none", None, False, "none"),
        ("none", "none", False, "none"),
        ("write", "custom-role", True, "write"),  # a custom role: permission decides
        ("read", "custom-role", False, "read"),
        ("admin", "custom-role", True, "admin"),
        ("read", 5, False, "read"),
    ],
)
def test_the_permission_decides_write_access_and_the_role_the_level(
    make: Callable[..., Harness], perm: str, role: Any, write: bool, level: str
) -> None:
    doc = permission(perm)
    if role is not None:
        doc["role_name"] = role
    result = make(repo_doc(), doc).access()
    assert (result.write_access, result.level) == (write, level)
    assert result.reason is None and result.source == "collaborator_permission"
    assert result.repo == "github.com/example-org/example-repo"


def test_a_role_that_claims_more_than_the_permission_does_not_grant_write(
    make: Callable[..., Harness],
) -> None:
    # `maintain` and `admin` in role_name with a read permission: the permission decides
    result = make(repo_doc(), permission("read", "maintain")).access()
    assert result.write_access is False


def test_a_403_is_unknown_never_write(make: Callable[..., Harness]) -> None:
    result = make(repo_doc(), http(403, {"message": "Must have push access"})).access()
    assert result.write_access is None
    assert result.level is None
    assert result.reason == "no_permission_to_read"


def test_a_rate_limited_403_is_not_a_null_answer(make: Callable[..., Harness]) -> None:
    answer = http(403, {"message": "API rate limit exceeded"}, headers={"retry-after": "1"})
    error = failure(make(repo_doc(), answer, answer, answer, answer), lambda h: h.access())
    assert error.code == "rate_limited"


def test_a_missing_repo_is_not_found(make: Callable[..., Harness]) -> None:
    error = failure(make(http(404, {"message": "Not Found"})), lambda h: h.access())
    assert error.code == "not_found"


def test_a_user_github_does_not_know_is_not_found(make: Callable[..., Harness]) -> None:
    error = failure(make(repo_doc(), http(404, {"message": "Not Found"})), lambda h: h.access())
    assert error.code == "not_found"


def test_a_bot_the_host_cannot_resolve_is_unknown_not_not_found(
    make: Callable[..., Harness],
) -> None:
    result = make(repo_doc(), http(404, {"message": "Not Found"})).access("user-3[bot]")
    assert result.write_access is None and result.reason == "no_permission_to_read"


def test_a_bot_login_is_percent_encoded_in_the_path(make: Callable[..., Harness]) -> None:
    harness = make(repo_doc(), permission("write", "write", "user-3[bot]"))
    result = harness.access("user-3[bot]")
    assert harness.script.argvs[1][-1].endswith("/collaborators/user-3%5Bbot%5D/permission")
    assert result.user == "user-3[bot]"


def test_the_repo_answer_must_be_this_hosts_own(make: Callable[..., Harness]) -> None:
    doc = repo_doc()
    doc["html_url"] = "https://evil.example.test/example-org/example-repo"
    assert failure(make(doc), lambda h: h.access()).code == "wrong_host_response"


def test_a_renamed_repo_uses_the_canonical_path(make: Callable[..., Harness]) -> None:
    doc = repo_doc()
    doc["full_name"] = "example-org/renamed"
    doc["html_url"] = "https://github.com/example-org/renamed"
    harness = make(doc, permission())
    result = harness.access()
    assert (
        harness.script.argvs[1][-1] == "repos/example-org/renamed/collaborators/user-1/permission"
    )
    assert result.repo == "github.com/example-org/renamed"


@pytest.mark.parametrize(
    "answer",
    [
        {"permission": "superuser"},
        {"permission": None},
        {"permission": 1},
        {"permission": True},
        {},
        {"permission": "write", "user": {"login": "someone-else"}},
        {"permission": "write", "user": "user-1"},
        {"permission": "write", "user": {"login": 5}},
        [],
        "write",
    ],
)
def test_a_permission_answer_that_is_not_the_documented_shape_is_an_error_never_write(
    make: Callable[..., Harness], answer: Any
) -> None:
    error = failure(make(repo_doc(), http(200, answer)), lambda h: h.access())
    assert error.code in ("upstream_error", "wrong_host_response")


def test_the_host_form_of_the_login_is_reported_when_it_differs_only_in_case(
    make: Callable[..., Harness],
) -> None:
    result = make(repo_doc(), permission("write", "write", "User-1")).access("user-1")
    assert result.user == "User-1"


def test_the_user_with_a_hostile_login_in_the_answer_is_refused(
    make: Callable[..., Harness],
) -> None:
    doc = permission("write", "write", "user-1\nforged")
    assert failure(make(repo_doc(), doc), lambda h: h.access()).code == "upstream_error"


@pytest.mark.parametrize("user", ["a b", "a/b", "a:b", "..", "", "a\n", "uе", "a?x=1"])
def test_access_refuses_a_login_outside_the_grammar_before_any_call(
    make: Callable[..., Harness], user: str
) -> None:
    harness = make(repo_doc())
    assert failure(harness, lambda h: h.access(user)).code == "usage"
    assert harness.script.argvs == []


@pytest.mark.parametrize("path", ["a", "a/b/c", "a/..", "../b", "a/b c", ""])
def test_access_refuses_a_repo_path_outside_the_grammar(
    make: Callable[..., Harness], path: str
) -> None:
    harness = make(repo_doc())
    repo = RepoRef(host="github.com", backend="github", path=path)
    assert failure(harness, lambda h: h.access(repo=repo)).code in ("usage", "not_found")
    assert harness.script.argvs == []


# -- whoami ------------------------------------------------------------------------------------


def test_the_backend_whoami_is_the_authenticated_user_with_scopes(
    make: Callable[..., Harness],
) -> None:
    answer = http(200, recording("user"), headers={"x-oauth-scopes": "repo, read:org"})
    identity = make(answer).backend.whoami("github.com")
    assert (identity.host, identity.backend, identity.login, identity.id) == (
        "github.com",
        "github",
        "user-1",
        "1001",
    )
    assert identity.scopes == ("repo", "read:org")
    assert TOKEN not in repr(identity)


def test_a_failed_identity_read_is_raised(make: Callable[..., Harness]) -> None:
    error = failure(
        make(http(401, {"message": "Bad credentials"})), lambda h: h.backend.whoami("github.com")
    )
    assert error.code == "auth_required"


def test_module_exports() -> None:
    assert callable(github_lists.list_prs)
    assert callable(github_lists.access)
    assert callable(github_lists.whoami)
