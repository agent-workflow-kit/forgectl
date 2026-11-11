"""``poll`` (#118): the light readers of both hosts and the command over them, on a stub
environment that answers GraphQL from a router (no process, no network)."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from forgectl import poll
from forgectl.backends import github_light, gitlab_bulk, gitlab_light
from forgectl.backends.light_common import LightPage, LightUnsupported
from forgectl.errors import ForgectlError
from forgectl.gateway import GraphQLResult, Target

from .harness import schemas

SHA = "a" * 40
SHA2 = "b" * 40
TS = "2026-11-03T12:00:00Z"


class Stub:
    """A ``ReadEnv`` look-alike: ``route(query, variables)`` answers each GraphQL call."""

    def __init__(
        self, route: Callable[[str, dict[str, Any]], dict[str, Any]], backend: str
    ) -> None:
        self.requests: list[dict[str, Any]] = []
        host = "github.com" if backend == "github" else "gitlab.example.test"
        self.target = Target(backend=backend, host=host)  # type: ignore[arg-type]

        def graphql(target: Target, query: str, variables: Any = None, **_kw: Any) -> GraphQLResult:
            self.requests.append({"query": query, "variables": dict(variables or {})})
            return GraphQLResult(data=route(query, dict(variables or {})), errors=())

        self.gateway = SimpleNamespace(
            graphql=graphql,
            budget=SimpleNamespace(remaining=lambda: 100.0),
            rate_readings=[],
            config=SimpleNamespace(
                config=SimpleNamespace(hosts={}, hidden_repos=()),
            ),
        )
        self.hosts = SimpleNamespace(
            target=lambda host: self.target,
            me=lambda host: "octo",
            backend=lambda host: backend,
            gateway=self.gateway,
        )
        self.retries = 0
        self.warn = lambda message: None

    def call(self, fn: Callable[[], Any]) -> Any:
        return fn()

    def until_known(self, fetch: Callable[[], Any], unknown: Any, spec: Any = None) -> Any:
        return fetch()


def gh_node(
    number: int,
    *,
    repo: str = "acme/widgets",
    state: str | None = "SUCCESS",
    decision: str | None = None,
    sha: str = SHA,
    updated: str = TS,
) -> dict[str, Any]:
    return {
        "__typename": "PullRequest",
        "id": f"PR_{number}",
        "number": number,
        "url": f"https://github.com/{repo}/pull/{number}",
        "title": f"change {number}",
        "isDraft": False,
        "updatedAt": updated,
        "headRefOid": sha,
        "reviewDecision": decision,
        "author": {"__typename": "User", "login": "alice", "databaseId": 5},
        "repository": {"nameWithOwner": repo},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": state}}}]},
    }


def gh_page(nodes: list[Any], *, more: bool = False, count: int | None = None) -> dict[str, Any]:
    return {
        "search": {
            "issueCount": len(nodes) if count is None else count,
            "pageInfo": {"hasNextPage": more, "endCursor": "c1" if more else None},
            "nodes": nodes,
        }
    }


def by_search(authored: list[Any], requested: list[Any]) -> Callable[..., dict[str, Any]]:
    def route(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        q = variables["q"]
        return gh_page(authored if "author:octo" in q else requested)

    return route


# -- GitHub --------------------------------------------------------------------------------------


def test_github_reads_one_search_per_role_and_a_pr_on_both_lists_has_both_roles() -> None:
    env = Stub(by_search([gh_node(1), gh_node(2)], [gh_node(2), gh_node(3)]), "github")
    page = github_light.read_light(env, "github.com", None)  # type: ignore[arg-type]
    assert len(env.requests) == 2
    assert "mergeStateStatus" not in env.requests[0]["query"]
    roles = {i.number: i.roles for i in page.items}
    assert roles == {1: ("authored",), 2: ("authored", "requested"), 3: ("requested",)}
    one = next(i for i in page.items if i.number == 1)
    assert (one.head_sha, one.ci_state, one.review_decision) == (SHA, "SUCCESS", None)
    assert one.pr == "github.com/acme/widgets#1" and page.complete is True


def test_github_tuple_fields_follow_the_answer() -> None:
    node = gh_node(1, state="FAILURE", decision="APPROVED", sha=SHA2)
    page = github_light.read_light(Stub(by_search([node], []), "github"), "github.com", None)  # type: ignore[arg-type]
    item = page.items[0]
    assert (item.ci_state, item.review_decision, item.head_sha) == ("FAILURE", "APPROVED", SHA2)


def test_github_a_pr_with_no_ci_has_a_null_state() -> None:
    node = gh_node(1)
    node["commits"] = {"nodes": [{"commit": {"statusCheckRollup": None}}]}
    page = github_light.read_light(Stub(by_search([node], []), "github"), "github.com", None)  # type: ignore[arg-type]
    assert page.items[0].ci_state is None


def test_github_hidden_repos_are_dropped() -> None:
    env = Stub(by_search([gh_node(1), gh_node(2, repo="acme/secret")], []), "github")
    page = github_light.read_light(
        env,  # type: ignore[arg-type]
        "github.com",
        None,
        lambda repo: repo.endswith("/secret"),
    )
    assert [i.number for i in page.items] == [1]
    assert page.complete is True  # a hidden PR does not make the list incomplete


def test_github_pages_are_followed_and_a_list_beyond_the_cap_is_incomplete() -> None:
    calls = {"n": 0}

    def route(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "author:octo" not in variables["q"]:
            return gh_page([])
        calls["n"] += 1
        return gh_page([gh_node(calls["n"])], more=True, count=1000)

    env = Stub(route, "github")
    page = github_light.read_light(env, "github.com", None)  # type: ignore[arg-type]
    assert calls["n"] == github_light.MAX_PAGES
    assert page.complete is False


def test_github_fewer_results_than_counted_is_incomplete() -> None:
    def route(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return gh_page([gh_node(1)] if "author:octo" in variables["q"] else [], count=3)

    page = github_light.read_light(Stub(route, "github"), "github.com", None)  # type: ignore[arg-type]
    assert page.complete is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda n: n.update(number="7"),
        lambda n: n.update(headRefOid="zz"),
        lambda n: n.update(isDraft="no"),
        lambda n: n.update(url="https://evil.example/acme/widgets/pull/1"),
        lambda n: n.update(repository={"nameWithOwner": "a b/c"}),
        lambda n: n.update(updatedAt="yesterday"),
    ],
)
def test_github_a_malformed_node_is_an_error(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    node = gh_node(1)
    mutate(node)
    env = Stub(by_search([node], []), "github")
    with pytest.raises(ForgectlError) as raised:
        github_light.read_light(env, "github.com", None)  # type: ignore[arg-type]
    assert raised.value.code in ("upstream_error", "wrong_host_response")


def test_github_hostile_title_is_sanitized() -> None:
    node = gh_node(1)
    node["title"] = "x\x1b[31m red\nnewline"
    page = github_light.read_light(Stub(by_search([node], []), "github"), "github.com", None)  # type: ignore[arg-type]
    assert "\x1b" not in page.items[0].title and "\n" not in page.items[0].title


# -- GitLab --------------------------------------------------------------------------------------


def gl_node(
    iid: int,
    *,
    path: str = "team/proj",
    left: int | None = 1,
    status: str | None = "running",
    merge: str = "mergeable",
    notes: int = 2,
    sha: str = SHA,
) -> dict[str, Any]:
    return {
        "id": f"gid://gitlab/MergeRequest/{1000 + iid}",
        "iid": str(iid),
        "webUrl": f"https://gitlab.example.test/{path}/-/merge_requests/{iid}",
        "title": f"mr {iid}",
        "draft": False,
        "updatedAt": TS,
        "diffHeadSha": sha,
        "approved": False,
        "approvalsLeft": left,
        "detailedMergeStatus": merge,
        "userNotesCount": notes,
        "author": {"username": "alice"},
        "headPipeline": None if status is None else {"status": status},
    }


def gl_route(
    authored: list[Any], requested: list[Any], pages: dict[str, list[dict[str, Any]]] | None = None
) -> Callable[..., dict[str, Any]]:
    def conn(nodes: list[Any], more: bool = False) -> dict[str, Any]:
        return {
            "pageInfo": {"hasNextPage": more, "endCursor": "next" if more else None},
            "nodes": nodes,
        }

    def route(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        user: dict[str, Any] = {}
        if variables["doA"]:
            user["authored"] = conn(authored)
        if variables["doR"]:
            user["requested"] = conn(requested)
        return {"queryComplexity": {"score": 80, "limit": 250}, "currentUser": user}

    return route


@pytest.fixture
def probed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitlab_bulk, "probe", lambda env, host: None)


def test_gitlab_reads_both_lists_in_one_request(probed: None) -> None:
    env = Stub(gl_route([gl_node(1), gl_node(2)], [gl_node(2), gl_node(3)]), "gitlab")
    page = gitlab_light.read_light(env, "gitlab.example.test")  # type: ignore[arg-type]
    assert len(env.requests) == 1
    assert "mergeRequestInteraction" not in env.requests[0]["query"]
    roles = {i.number: i.roles for i in page.items}
    assert roles == {1: ("authored",), 2: ("authored", "requested"), 3: ("requested",)}
    first = page.items[0]
    assert first.pr == "gitlab.example.test/team/proj#1"
    assert (first.approvals_left, first.merge_status, first.notes_count, first.ci_state) == (
        1,
        "mergeable",
        2,
        "running",
    )
    assert first.approved is False and first.review_decision is None and page.complete


def test_gitlab_a_second_page_asks_only_for_the_list_that_has_one(probed: None) -> None:
    seen: list[dict[str, Any]] = []

    def route(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        seen.append(variables)
        if variables["a"] is None and variables["doA"]:
            first = {
                "pageInfo": {"hasNextPage": True, "endCursor": "c9"},
                "nodes": [gl_node(1)],
            }
            second = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}
            return {"currentUser": {"authored": first, "requested": second}}
        return {
            "currentUser": {
                "authored": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [gl_node(2)],
                }
            }
        }

    page = gitlab_light.read_light(Stub(route, "gitlab"), "gitlab.example.test")  # type: ignore[arg-type]
    assert len(seen) == 2 and seen[1]["doR"] is False and seen[1]["a"] == "c9"
    assert sorted(i.number for i in page.items) == [1, 2] and page.complete


def test_gitlab_a_list_that_never_ends_is_incomplete(probed: None) -> None:
    counter = {"n": 0}

    def route(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        node = {
            "pageInfo": {"hasNextPage": True, "endCursor": f"c{counter['n']}"},
            "nodes": [gl_node(counter["n"])],
        }
        return {"currentUser": {"authored": node, "requested": node}}

    page = gitlab_light.read_light(Stub(route, "gitlab"), "gitlab.example.test")  # type: ignore[arg-type]
    assert page.complete is False and counter["n"] == gitlab_light.MAX_PAGES


def test_gitlab_a_host_that_fails_the_probe_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitlab_bulk, "probe", lambda env, host: "no complexity")
    env = Stub(gl_route([], []), "gitlab")
    with pytest.raises(LightUnsupported):
        gitlab_light.read_light(env, "gitlab.example.test")  # type: ignore[arg-type]
    assert env.requests == []


def test_gitlab_hidden_repos_are_dropped(probed: None) -> None:
    env = Stub(gl_route([gl_node(1), gl_node(2, path="team/secret")], []), "gitlab")
    page = gitlab_light.read_light(
        env,  # type: ignore[arg-type]
        "gitlab.example.test",
        None,
        lambda repo: repo.endswith("/secret"),
    )
    assert [i.number for i in page.items] == [1]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda n: n.update(iid="x1"),
        lambda n: n.update(webUrl="https://evil.example/team/proj/-/merge_requests/1"),
        lambda n: n.update(diffHeadSha=None),
        lambda n: n.update(draft="no"),
    ],
)
def test_gitlab_a_malformed_node_is_an_error(
    probed: None, mutate: Callable[[dict[str, Any]], None]
) -> None:
    node = gl_node(1)
    mutate(node)
    with pytest.raises(ForgectlError):
        gitlab_light.read_light(Stub(gl_route([node], []), "gitlab"), "gitlab.example.test")  # type: ignore[arg-type]


# -- the command ---------------------------------------------------------------------------------


class FakeReader:
    def __init__(self, page: LightPage | Exception) -> None:
        self.page = page

    def read_light(self, env: Any, host: str, known: Any, skip: Any = None) -> LightPage:
        if isinstance(self.page, Exception):
            raise self.page
        return self.page


def command(
    monkeypatch: pytest.MonkeyPatch, pages: dict[str, LightPage | Exception]
) -> dict[str, Any]:
    env = Stub(lambda q, v: {}, "github")
    monkeypatch.setattr(poll, "host_scope", lambda env, hosts, **kw: [(h, None) for h in pages])
    monkeypatch.setattr(poll, "_reader", lambda backend: FakeReader(pages[backend]))
    env.hosts.backend = lambda host: host  # type: ignore[attr-defined]
    return poll.run_poll(env)  # type: ignore[arg-type]


def light_item(n: int, updated: str, host: str = "github.com") -> Any:
    from forgectl.backends.light_common import LightItem

    return LightItem(
        pr=f"{host}/a/b#{n}",
        pr_id=f"id{n}",
        repo=f"{host}/a/b",
        number=n,
        url=f"https://{host}/a/b/pull/{n}",
        title="t",
        author="alice",
        draft=False,
        head_sha=SHA,
        updated_at=updated,
        roles=("authored",),
    )


def test_poll_merges_hosts_newest_first_and_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    out = command(
        monkeypatch,
        {
            "github": LightPage([light_item(1, "2026-11-03T10:00:00Z")]),
            "gitlab": LightPage([light_item(2, "2026-11-03T11:00:00Z", "gl.test")]),
        },
    )
    assert [i["number"] for i in out["items"]] == [2, 1]
    assert out["complete"] is True and out["unsupported"] == []
    schemas.validate_cli_output("poll", out)


def test_poll_a_failed_host_is_a_partial_result(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ForgectlError) as raised:
        command(
            monkeypatch,
            {
                "github": LightPage([light_item(1, TS)]),
                "gitlab": ForgectlError("rate_limited", "slow down"),
            },
        )
    assert raised.value.code == "partial_results"
    out = raised.value.output
    assert out is not None and out["complete"] is False
    assert [e["host"] for e in out["errors"]] == ["gitlab"] and len(out["items"]) == 1
    schemas.validate_cli_output("poll", out)


def test_poll_an_unsupported_host_is_named_and_the_list_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = command(
        monkeypatch,
        {"github": LightPage([light_item(1, TS)]), "gitlab": LightUnsupported("old")},
    )
    assert out["unsupported"] == ["gitlab"] and out["complete"] is False
    schemas.validate_cli_output("poll", out)


def test_poll_one_host_failing_raises_its_own_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ForgectlError) as raised:
        command(monkeypatch, {"github": ForgectlError("auth_required", "no token")})
    assert raised.value.code == "auth_required"


def test_poll_a_cut_list_is_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    out = command(monkeypatch, {"github": LightPage([light_item(1, TS)], complete=False)})
    assert out["complete"] is False
