"""``hide_archived_repos`` (#187): the config key, the skip predicate, and both backends' reads of
the host's archived flag, on fake gateway answers. Archived state is read live on every call."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from forgectl import model as m
from forgectl import poll
from forgectl.backends import github_light, gitlab_bulk, gitlab_light
from forgectl.config import Config, config_hash, load_config
from forgectl.errors import ForgectlError
from forgectl.fanout import list_visible

from .gitlab_support import FakeGitLab, make_backend
from .harness import schemas
from .test_github_lists import Harness, make, nodes_of, rated, recording
from .test_gitlab_lists import GLOBAL
from .test_gitlab_lists import lists as gl_lists
from .test_light_poll import SHA, Stub, by_search, gh_node, gl_node, gl_route
from .test_lists_cli import prs

__all__ = ["make"]  # the fixture of test_github_lists, used here

SCHEMA = json.loads((Path(__file__).parent.parent / "schemas" / "config.json").read_text())
RATE = m.RateLimit(host=None, remaining=None, reset_at=None)


def load(tmp_path: Path, text: str) -> Config:
    home = tmp_path / "h"
    (home / ".config" / "forgectl").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "forgectl" / "config.yaml").write_text(text, encoding="utf-8")
    return load_config(None, env={"HOME": str(home)}).config


# --- config --------------------------------------------------------------------------------------


def test_the_option_defaults_to_true(tmp_path: Path) -> None:
    assert load(tmp_path, "").hide_archived_repos is True
    assert Config().hide_archived_repos is True
    assert SCHEMA["properties"]["hide_archived_repos"]["default"] is True


def test_the_option_can_be_turned_off(tmp_path: Path) -> None:
    assert load(tmp_path, "hide_archived_repos: false\n").hide_archived_repos is False


@pytest.mark.parametrize("value", ["yes", "'true'", "1", "null", "[]"])
def test_a_value_that_is_not_a_boolean_is_refused(tmp_path: Path, value: str) -> None:
    with pytest.raises(ForgectlError) as caught:
        load(tmp_path, f"hide_archived_repos: {value}\n")
    assert caught.value.code == "config_invalid"
    assert caught.value.message.startswith("hide_archived_repos")
    assert not Draft202012Validator(SCHEMA).is_valid({"hide_archived_repos": "yes"})


def test_a_drop_in_overrides_and_the_hash_covers_the_option(tmp_path: Path) -> None:
    home = tmp_path / "h"
    base = home / ".config" / "forgectl"
    (base / "config.d").mkdir(parents=True)
    (base / "config.yaml").write_text("hide_archived_repos: true\n", encoding="utf-8")
    (base / "config.d" / "x.yaml").write_text("hide_archived_repos: false\n", encoding="utf-8")
    loaded = load_config(None, env={"HOME": str(home)})
    assert loaded.config.hide_archived_repos is False
    assert loaded.config_hash != config_hash(Config())


# --- the skip predicate --------------------------------------------------------------------------


def item(repo: str, n: int = 1) -> Any:
    return SimpleNamespace(repo=repo, pr=f"{repo}#{n}")


class Stub2:
    """A backend over fixed items, each with the host's archived word for its repo."""

    def __init__(self, items: list[Any], archived: set[str]) -> None:
        self.items, self.archived, self.asked = items, archived, 0

    def list_prs(self, query: m.PrQuery, skip: Any = None) -> m.PrPage:
        self.asked += 1
        kept = [i for i in self.items if skip is None or not skip(i.repo, i.repo in self.archived)]
        return m.PrPage(items=tuple(kept[: query.limit]), complete=True, rate_limit=RATE)


def listed(hide: bool, hidden: tuple[str, ...] = (), archived: set[str] | None = None) -> list[str]:
    repos = ["github.com/octo/live", "github.com/octo/archived-repo", "github.com/octo/old"]
    stub = Stub2(
        [item(r, i) for i, r in enumerate(repos)], {repos[1]} if archived is None else archived
    )
    page = list_visible(stub, m.PrQuery(limit=10), hidden, hide)
    return [i.repo for i in page.items]


def test_an_archived_repo_is_hidden_and_a_live_one_is_not() -> None:
    assert listed(True) == ["github.com/octo/live", "github.com/octo/old"]


def test_option_false_lists_the_archived_repo_again() -> None:
    assert listed(False) == [
        "github.com/octo/live",
        "github.com/octo/archived-repo",
        "github.com/octo/old",
    ]


def test_the_archived_rule_and_hidden_repos_are_a_union() -> None:
    assert listed(True, ("github.com/octo/old",)) == ["github.com/octo/live"]
    # hidden_repos alone still works when the option is off
    assert listed(False, ("github.com/octo/old",)) == [
        "github.com/octo/live",
        "github.com/octo/archived-repo",
    ]


def test_nothing_archived_and_nothing_hidden_changes_nothing() -> None:
    assert len(listed(True, archived=set())) == 3


def test_nothing_hidden_and_the_option_off_is_the_untouched_call() -> None:
    stub = Stub2([item("github.com/octo/a")], set())
    seen: list[Any] = []
    stub.list_prs = lambda query, skip=None: (
        seen.append(skip)
        or m.PrPage(  # type: ignore[method-assign]
            items=(), complete=True, rate_limit=RATE
        )
    )
    list_visible(stub, m.PrQuery(limit=5), (), False)
    assert seen == [None]  # called without a skip


# --- GitHub: the search -------------------------------------------------------------------------


def archived_search(flags: Mapping[str, Any]) -> dict[str, Any]:
    doc = recording()
    for node in nodes_of(doc):
        name = node["repository"]["nameWithOwner"]
        if name in flags:
            node["repository"]["isArchived"] = flags[name]
    return doc


def hide_archived(harness: Harness, hide: bool, hidden: tuple[str, ...] = ()) -> m.PrPage:
    def skip(repo: str, archived: bool = False) -> bool:
        return (hide and archived) or repo in hidden

    return harness.backend.list_prs(m.PrQuery(host="github.com", author="user-1"), skip=skip)


def test_the_github_query_asks_for_the_archived_flag() -> None:
    from forgectl.backends.github_lists import PRS_QUERY

    assert "isArchived" in PRS_QUERY and "isArchived" in github_light.LIGHT_QUERY


def test_github_an_archived_repo_is_left_out(make: Callable[..., Harness]) -> None:
    harness = make(
        rated(archived_search({"user-7/other-repo": True, "user-7/example-repo": False}))
    )
    page = hide_archived(harness, True)
    assert {i.repo for i in page.items} == {"github.com/user-7/example-repo"}
    assert page.complete is True  # an archived item does not make the list incomplete


def test_github_a_repo_not_archived_stays(make: Callable[..., Harness]) -> None:
    harness = make(
        rated(archived_search({"user-7/other-repo": False, "user-7/example-repo": False}))
    )
    assert len(hide_archived(harness, True).items) == 3


def test_github_a_missing_flag_counts_as_not_archived(make: Callable[..., Harness]) -> None:
    assert len(hide_archived(make(rated(recording())), True).items) == 3


def test_github_option_false_keeps_the_archived_repo(make: Callable[..., Harness]) -> None:
    harness = make(rated(archived_search({"user-7/other-repo": True})))
    assert len(hide_archived(harness, False).items) == 3


def test_github_union_with_a_hidden_repo(make: Callable[..., Harness]) -> None:
    harness = make(rated(archived_search({"user-7/other-repo": True})))
    page = hide_archived(harness, True, ("github.com/user-7/example-repo",))
    assert page.items == ()


@pytest.mark.parametrize("bad", ["true", 1, []])
def test_github_a_flag_that_is_not_a_boolean_is_an_upstream_error(
    make: Callable[..., Harness], bad: Any
) -> None:
    harness = make(rated(archived_search({"user-7/other-repo": bad})))
    with pytest.raises(ForgectlError) as caught:
        hide_archived(harness, True)
    assert caught.value.code == "upstream_error"


def test_github_a_refresh_sees_a_repo_archived_since_the_last_read(
    make: Callable[..., Harness],
) -> None:
    before = archived_search({"user-7/other-repo": False})
    after = archived_search({"user-7/other-repo": True})
    harness = make(rated(before), rated(after), rated(before))
    repos = [{i.repo for i in hide_archived(harness, True).items} for _ in range(3)]
    assert "github.com/user-7/other-repo" in repos[0]
    assert "github.com/user-7/other-repo" not in repos[1]  # archived since: gone on the next read
    assert "github.com/user-7/other-repo" in repos[2]  # unarchived: back


# --- GitLab: the merge request list -------------------------------------------------------------


def gl_scene(fake: FakeGitLab, archived: dict[int, Any]) -> None:
    everything = gl_lists("mrs")
    items = [
        i for i in everything if i["author"]["username"] == "user-1" and i["state"] == "opened"
    ]
    other = copy.deepcopy(items[0])
    other.update(
        iid=77,
        id=90077,
        project_id=303,
        target_project_id=303,
        source_project_id=303,
        web_url="https://gitlab.com/user-8/old/-/merge_requests/77",
    )
    other["updated_at"] = "2026-01-01T00:00:00.000Z"
    fake.on(GLOBAL, [*items, other], headers={"x-next-page": ""})
    fake.on("projects/202", gl_lists("source_project"))
    fake.on("version", {"version": "17.5.1", "revision": "abc"})
    for pid, flag in archived.items():
        body: dict[str, Any] = {"id": pid, "archived": flag}
        fake.on(f"projects/{pid}", body)


def gl_prs(
    tmp_path: Path, fake: FakeGitLab, hide: bool = True, hidden: tuple[str, ...] = ()
) -> Any:
    backend = make_backend(fake, tmp_path)

    def skip(repo: str, archived: bool = False) -> bool:
        return (hide and archived) or repo in hidden

    return backend.list_prs(m.PrQuery(host="gitlab.com", author="user-1", state="open"), skip=skip)


def test_gitlab_an_archived_project_is_left_out(tmp_path: Path) -> None:
    fake = FakeGitLab()
    gl_scene(fake, {101: False, 303: True})
    page = gl_prs(tmp_path, fake)
    assert {i.repo for i in page.items} == {"gitlab.com/user-9/proj"}
    assert page.complete is True


def test_gitlab_a_project_not_archived_stays(tmp_path: Path) -> None:
    fake = FakeGitLab()
    gl_scene(fake, {101: False, 303: False})
    assert {i.repo for i in gl_prs(tmp_path, fake).items} == {
        "gitlab.com/user-9/proj",
        "gitlab.com/user-8/old",
    }


def test_gitlab_each_project_is_read_once_per_command(tmp_path: Path) -> None:
    fake = FakeGitLab()
    gl_scene(fake, {101: False, 303: True})
    gl_prs(tmp_path, fake)
    assert fake.count("projects/101") == 1 and fake.count("projects/303") == 1


def test_gitlab_option_false_reads_no_project(tmp_path: Path) -> None:
    fake = FakeGitLab()
    gl_scene(fake, {101: False, 303: True})
    page = gl_prs(tmp_path, fake, hide=False)
    assert len(page.items) == 3 and page.complete is True
    assert fake.count("projects/101") == fake.count("projects/303") == 0


def test_gitlab_a_hidden_repo_is_not_read_for_its_archived_state(tmp_path: Path) -> None:
    fake = FakeGitLab()
    gl_scene(fake, {101: False, 303: True})
    page = gl_prs(tmp_path, fake, hidden=("gitlab.com/user-8/old",))
    assert {i.repo for i in page.items} == {"gitlab.com/user-9/proj"}
    assert fake.count("projects/303") == 0  # union: the repo id already decided


def test_gitlab_an_unreadable_project_is_not_treated_as_archived(tmp_path: Path) -> None:
    fake = FakeGitLab()
    gl_scene(fake, {101: False})
    fake.on("projects/303", {"message": "404 Project Not Found"}, status=404)
    page = gl_prs(tmp_path, fake)
    assert "gitlab.com/user-8/old" in {i.repo for i in page.items}


def test_gitlab_a_flag_that_is_not_a_boolean_is_an_upstream_error(tmp_path: Path) -> None:
    fake = FakeGitLab()
    gl_scene(fake, {101: "yes", 303: False})
    with pytest.raises(ForgectlError) as caught:
        gl_prs(tmp_path, fake)
    assert caught.value.code == "upstream_error"


def test_gitlab_a_refresh_sees_a_project_archived_since_the_last_read(tmp_path: Path) -> None:
    fake = FakeGitLab()
    gl_scene(fake, {101: False, 303: False})
    backend = make_backend(fake, tmp_path)  # one long-lived backend: no memory between calls

    def skip(repo: str, archived: bool = False) -> bool:
        return archived

    def read() -> set[str]:
        page = backend.list_prs(
            m.PrQuery(host="gitlab.com", author="user-1", state="open"), skip=skip
        )
        return {i.repo for i in page.items}

    assert "gitlab.com/user-8/old" in read()
    fake.on("projects/303", {"id": 303, "archived": True})
    assert "gitlab.com/user-8/old" not in read()
    fake.on("projects/303", {"id": 303, "archived": False})
    assert "gitlab.com/user-8/old" in read()


# --- the light poll (browse's refresh) -----------------------------------------------------------


def with_archived(node: dict[str, Any], flag: Any) -> dict[str, Any]:
    node["repository"]["isArchived"] = flag
    return node


def test_github_light_drops_an_archived_repo() -> None:
    nodes = [with_archived(gh_node(1), False), with_archived(gh_node(2, repo="acme/old"), True)]
    env = Stub(by_search(nodes, []), "github")

    page = github_light.read_light(env, "github.com", None, lambda repo, archived=False: archived)  # type: ignore[arg-type]

    assert [i.number for i in page.items] == [1] and page.complete is True


def test_github_light_a_flag_that_is_not_a_boolean_is_an_error() -> None:
    env = Stub(by_search([with_archived(gh_node(1), "yes")], []), "github")
    with pytest.raises(ForgectlError):
        github_light.read_light(env, "github.com", None)  # type: ignore[arg-type]


@pytest.fixture
def probed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitlab_bulk, "probe", lambda env, host: None)


def gl_with(node: dict[str, Any], flag: Any) -> dict[str, Any]:
    node["project"] = {"archived": flag}
    return node


def test_gitlab_light_asks_for_the_project_flag_and_drops_an_archived_one(probed: None) -> None:
    nodes = [gl_with(gl_node(1), False), gl_with(gl_node(2, path="team/old"), True)]
    env = Stub(gl_route(nodes, []), "gitlab")
    page = gitlab_light.read_light(
        env,  # type: ignore[arg-type]
        "gitlab.example.test",
        None,
        lambda r, archived=False: archived,
    )
    assert "project { archived }" in env.requests[0]["query"]
    assert [i.number for i in page.items] == [1] and page.complete is True


def test_gitlab_light_a_node_without_the_project_is_not_archived(probed: None) -> None:
    env = Stub(gl_route([gl_node(1)], []), "gitlab")
    page = gitlab_light.read_light(
        env,  # type: ignore[arg-type]
        "gitlab.example.test",
        None,
        lambda r, archived=False: archived,
    )
    assert [i.number for i in page.items] == [1]


def test_gitlab_light_a_flag_that_is_not_a_boolean_is_an_error(probed: None) -> None:
    env = Stub(gl_route([gl_with(gl_node(1), "yes")], []), "gitlab")
    with pytest.raises(ForgectlError) as caught:
        gitlab_light.read_light(env, "gitlab.example.test", None)  # type: ignore[arg-type]
    assert caught.value.code == "upstream_error"


def poll_with(
    monkeypatch: pytest.MonkeyPatch,
    nodes: list[dict[str, Any]],
    *,
    hide: bool,
    hidden: tuple[str, ...] = (),
) -> list[int]:
    env = Stub(by_search(nodes, []), "github")
    env.gateway.config.config = SimpleNamespace(
        hosts={}, hidden_repos=hidden, hide_archived_repos=hide
    )
    monkeypatch.setattr(poll, "host_scope", lambda env, hosts, **kw: [("github.com", None)])
    monkeypatch.setattr(poll, "_reader", lambda backend: github_light)
    out = poll.run_poll(env)  # type: ignore[arg-type]
    return [i["number"] for i in out["items"]]


def test_poll_hides_archived_by_default_and_the_option_off_lists_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = [with_archived(gh_node(1), False), with_archived(gh_node(2, repo="acme/old"), True)]
    assert poll_with(monkeypatch, nodes, hide=True) == [1]
    assert sorted(poll_with(monkeypatch, nodes, hide=False)) == [1, 2]


def test_poll_unions_archived_with_hidden_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = [
        with_archived(gh_node(1), False),
        with_archived(gh_node(2, repo="acme/old"), True),
        with_archived(gh_node(3, repo="acme/secret"), False),
    ]
    assert poll_with(monkeypatch, nodes, hide=True, hidden=("github.com/acme/secret",)) == [1]


def test_poll_picks_up_a_repo_archived_between_two_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    before = [with_archived(gh_node(1), False), with_archived(gh_node(2, repo="acme/old"), False)]
    after = [with_archived(gh_node(1), False), with_archived(gh_node(2, repo="acme/old"), True)]
    assert sorted(poll_with(monkeypatch, before, hide=True)) == [1, 2]
    assert poll_with(monkeypatch, after, hide=True) == [1]  # the next refresh drops it
    assert sorted(poll_with(monkeypatch, before, hide=True)) == [1, 2]  # unarchived: back


def test_the_light_item_output_has_no_archived_field() -> None:
    from forgectl.backends.light_common import LightItem

    item = LightItem(
        pr="a/b#1", pr_id="i", repo="a/b", number=1, url="u", title="t", author=None,
        draft=False, head_sha=SHA, updated_at="2026-11-03T12:00:00Z", archived=True,
    )  # fmt: skip
    assert "archived" not in item.to_dict()


# --- the CLI -------------------------------------------------------------------------------------


def cli_config(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "archived.yaml"
    path.write_text(
        f"retries: 0\n{extra}hosts:\n  github.com:\n    token_env: TOK_A\n", encoding="utf-8"
    )
    return path


def cli_prs(run: Any, forges: Any, cfg: Path, flagged: dict[str, bool]) -> dict[str, Any]:
    forges.answer("gh", "graphql", method="POST", body=archived_search(flagged))
    done = prs(run, cfg, "--host", "github.com", "--author", "user-1")
    assert done.returncode == 0, done.stderr
    out: dict[str, Any] = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    return out


def test_prs_hides_an_archived_repo_by_default(
    run_forgectl: Any, fake_forges: Any, tmp_path: Path
) -> None:
    out = cli_prs(run_forgectl, fake_forges, cli_config(tmp_path, ""), {"user-7/other-repo": True})
    assert [i["number"] for i in out["items"]] == [31, 29]
    assert out["complete"] is True and set(out) == {
        "items",
        "complete",
        "errors",
        "hosts_queried",
        "rate_limit",
    }


def test_prs_lists_an_archived_repo_when_the_option_is_off(
    run_forgectl: Any, fake_forges: Any, tmp_path: Path
) -> None:
    cfg = cli_config(tmp_path, "hide_archived_repos: false\n")
    out = cli_prs(run_forgectl, fake_forges, cfg, {"user-7/other-repo": True})
    assert [i["number"] for i in out["items"]] == [31, 29, 12]


def test_prs_unions_the_archived_rule_with_hidden_repos(
    run_forgectl: Any, fake_forges: Any, tmp_path: Path
) -> None:
    cfg = cli_config(tmp_path, "hidden_repos: ['github.com/user-7/example-repo']\n")
    out = cli_prs(run_forgectl, fake_forges, cfg, {"user-7/other-repo": True})
    assert out["items"] == [] and out["complete"] is True
