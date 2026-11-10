"""``hidden_repos`` (#96): config rules, glob matching, the listing filter and its limit rules,
and the CLI end to end on both hosts. Hiding applies to listings only; a command naming a PR is
unaffected."""

from __future__ import annotations

import copy
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forgectl import model as m
from forgectl.activity import glob_match, ignored
from forgectl.config import Config, config_hash, load_config
from forgectl.errors import ForgectlError
from forgectl.fanout import list_visible
from forgectl.identifiers import RepoRef

from .harness import schemas
from .harness.forges import FakeForges
from .test_github_diff import PR_ENDPOINT, RECORDINGS
from .test_github_diff import pull as diff_pull
from .test_github_diff import recording as diff_recording
from .test_github_lists import nodes_of, recording
from .test_github_show import TOKEN as SHOW_TOKEN
from .test_github_show import recording as show_recording
from .test_gitlab_lists import mrs as gl_mrs
from .test_gitlab_lists_cli import config as gl_config
from .test_gitlab_lists_cli import route as gl_route
from .test_gitlab_lists_cli import script as gl_script
from .test_lists_cli import TOKEN_A, prs
from .test_snapshot_cli import PR
from .test_snapshot_cli import script as snapshot_script

Run = Callable[..., subprocess.CompletedProcess[str]]
RATE = m.RateLimit(host=None, remaining=None, reset_at=None)


def load(tmp_path: Path, text: str) -> Config:
    home = tmp_path / "h"
    (home / ".config" / "forgectl").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "forgectl" / "config.yaml").write_text(text, encoding="utf-8")
    return load_config(None, env={"HOME": str(home)}).config


# --- config --------------------------------------------------------------------------------------


def test_hidden_repos_defaults_to_empty_and_loads(tmp_path: Path) -> None:
    assert load(tmp_path, "").hidden_repos == ()
    text = 'hidden_repos: ["github.com/user-1/old", "gitlab.example.test/grp/*"]\n'
    assert load(tmp_path, text).hidden_repos == (
        "github.com/user-1/old",
        "gitlab.example.test/grp/*",
    )


@pytest.mark.parametrize(
    "entry",
    [
        '""',
        "1",
        "null",
        '"github.com"',  # no path
        '"user-1"',
        '"github.com/user 1/old"',
        '"github.com/../old"',
        '"github.com//old"',
        '"github.com/old/"',
        '"github.com/a[b]"',  # brackets are not allowed in a repo id
        '"github.com/a\\u0007b"',
        '"github.com/\\u00e9"',
    ],
)
def test_an_invalid_entry_is_config_invalid_naming_the_key(tmp_path: Path, entry: str) -> None:
    with pytest.raises(ForgectlError) as caught:
        load(tmp_path, f"hidden_repos: [{entry}]\n")
    assert caught.value.code == "config_invalid"
    assert caught.value.message.startswith("hidden_repos")


def test_a_non_list_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ForgectlError) as caught:
        load(tmp_path, "hidden_repos: github.com/user-1/old\n")
    assert caught.value.code == "config_invalid"


def test_the_config_hash_covers_hidden_repos(tmp_path: Path) -> None:
    base = config_hash(load(tmp_path, ""))
    one = config_hash(load(tmp_path, "hidden_repos: [github.com/user-1/old]\n"))
    other = config_hash(load(tmp_path, "hidden_repos: [github.com/user-1/older]\n"))
    assert len({base, one, other}) == 3
    assert config_hash(load(tmp_path, "hidden_repos: []\n")) == base
    assert '"hidden_repos":[]' in json.dumps(
        load(tmp_path, "").model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    )


def test_a_later_file_replaces_the_list(tmp_path: Path) -> None:
    home = tmp_path / "h"
    base = home / ".config" / "forgectl"
    (base / "config.d").mkdir(parents=True)
    (base / "config.yaml").write_text("hidden_repos: [github.com/a/b]\n", encoding="utf-8")
    (base / "config.d" / "x.yaml").write_text("hidden_repos: [github.com/c/d]\n", encoding="utf-8")
    assert load_config(None, env={"HOME": str(home)}).config.hidden_repos == ("github.com/c/d",)


# --- matching ------------------------------------------------------------------------------------


def item(repo: str, n: int = 1) -> Any:
    return SimpleNamespace(repo=repo, pr=f"{repo}#{n}")


class Stub:
    """A backend over a fixed newest-first list: the first ``limit`` items, complete when none
    were cut (the contract of ``Backend.list_prs``)."""

    def __init__(self, items: list[Any], *, complete: bool | None = None) -> None:
        self.items = items
        self.asked: list[int] = []
        self.skips: list[Any] = []
        self.complete = complete  # a fixed answer, whatever was cut

    def list_prs(self, query: m.PrQuery, skip: Any = None) -> m.PrPage:
        self.asked.append(query.limit)
        self.skips.append(skip)
        if self.complete is not None:  # a host that ignores the page size
            return m.PrPage(items=tuple(self.items), complete=self.complete, rate_limit=RATE)
        cut = len(self.items) > query.limit
        return m.PrPage(items=tuple(self.items[: query.limit]), complete=not cut, rate_limit=RATE)


def shown(repos: list[str], hidden: tuple[str, ...]) -> list[str]:
    page = list_visible(Stub([item(r, i) for i, r in enumerate(repos)]), m.PrQuery(), hidden)
    return [i.repo for i in page.items]


@pytest.mark.parametrize(
    ("pattern", "repo", "hidden"),
    [
        ("github.com/user-1/old", "github.com/user-1/old", True),
        ("GitHub.com/USER-1/Old", "github.com/user-1/old", True),  # ASCII case-insensitive
        ("github.com/user-1/old", "github.com/user-1/older", False),  # whole id
        ("github.com/user-1/old", "xgithub.com/user-1/old", False),
        ("github.com/user-1/*", "github.com/user-1/anything", True),
        ("github.com/user-1/*", "github.com/user-2/anything", False),
        ("github.com/user-1/old?", "github.com/user-1/old2", True),
        ("github.com/user-1/old?", "github.com/user-1/old", False),
        ("github.com/user-1/old?", "github.com/user-1/old22", False),
        ("*/user-1/old", "gitlab.example.test/user-1/old", True),
        ("gitlab.example.test/grp/*", "gitlab.example.test/grp/proj", True),
        ("gitlab.example.test/grp/*", "gitlab.example.test/grp/sub/deep/proj", True),  # nested
        ("gitlab.example.test/grp/*", "gitlab.example.test/grp2/proj", False),
        ("gitlab.example.test/grp/*", "gitlab.example.test/other/grp/proj", False),
        ("gitlab.example.test:8443/g/p", "gitlab.example.test:8443/g/p", True),
        ("gitlab.example.test:8443/g/p", "gitlab.example.test/g/p", False),
        ("github.com/user-1/a.c", "github.com/user-1/abc", False),  # `.` is literal
    ],
)
def test_matching(pattern: str, repo: str, hidden: bool) -> None:
    assert (shown([repo], (pattern,)) == []) is hidden


def test_several_entries_hide_the_union_and_keep_the_order() -> None:
    repos = ["github.com/a/x", "github.com/b/y", "github.com/c/z", "github.com/b/w"]
    assert shown(repos, ("github.com/a/x", "github.com/b/*")) == ["github.com/c/z"]


# --- limit and complete --------------------------------------------------------------------------


def many(hidden: int, visible: int) -> list[Any]:
    return [item("github.com/u/old", i) for i in range(hidden)] + [
        item("github.com/u/new", 1000 + i) for i in range(visible)
    ]


def test_hidden_items_do_not_consume_the_limit() -> None:
    stub = Stub(many(50, 300))
    page = list_visible(stub, m.PrQuery(limit=100), ("github.com/u/old",))
    assert [i.repo for i in page.items[:100]] == ["github.com/u/new"] * 100
    assert len(page.items) > 100  # the caller's merge cuts to the limit and says incomplete
    assert page.complete is False
    assert stub.asked == [101, 202]  # limit + 1, then one widening


def test_a_backlog_the_one_widening_cannot_clear_leaves_the_backend_answer() -> None:
    stub = Stub(many(150, 300))
    page = list_visible(stub, m.PrQuery(limit=100), ("github.com/u/old",))
    assert stub.asked == [101, 202] and len(page.items) == 52
    assert page.complete is False  # the backend's own: it cut the list


def test_all_hidden_and_nothing_more_is_complete_and_empty() -> None:
    page = list_visible(Stub(many(30, 0)), m.PrQuery(limit=100), ("github.com/u/old",))
    assert page.items == () and page.complete is True


def test_hiding_alone_never_makes_the_list_incomplete() -> None:
    page = list_visible(Stub(many(5, 5)), m.PrQuery(limit=100), ("github.com/u/old",))
    assert len(page.items) == 5 and page.complete is True


def test_no_hidden_repos_is_a_single_untouched_call() -> None:
    stub = Stub(many(0, 300))
    page = list_visible(stub, m.PrQuery(limit=100), ())
    assert stub.asked == [100] and len(page.items) == 100 and page.complete is False


def test_nothing_hidden_in_the_page_asks_once() -> None:
    stub = Stub(many(0, 300))
    list_visible(stub, m.PrQuery(limit=100), ("github.com/u/old",))
    assert stub.asked == [101]


def test_the_widening_stops_at_the_search_cap() -> None:
    stub = Stub(many(2000, 5))
    page = list_visible(stub, m.PrQuery(limit=600), ("github.com/u/old",))
    assert stub.asked == [601, 1000]
    assert page.items == () and page.complete is False  # past the cap: honest, not complete


def test_a_hidden_item_past_the_limit_does_not_make_the_list_incomplete() -> None:
    stub = Stub([item("github.com/u/new", 1), item("github.com/u/old", 2)])
    page = list_visible(stub, m.PrQuery(limit=1), ("github.com/u/old",))
    assert [i.repo for i in page.items] == ["github.com/u/new"]
    assert page.complete is True
    assert stub.asked == [2]


def test_a_visible_item_past_the_limit_is_incomplete() -> None:
    stub = Stub([item("github.com/u/new", 1), item("github.com/u/other", 2)])
    page = list_visible(stub, m.PrQuery(limit=1), ("github.com/u/old",))
    assert page.complete is False


def test_the_first_ask_is_capped_at_the_maximum() -> None:
    stub = Stub(many(0, 3))
    list_visible(stub, m.PrQuery(limit=1000), ("github.com/u/old",))
    assert stub.asked == [1000]


@pytest.mark.parametrize("complete", [False, True])
def test_a_host_that_ignores_the_page_size_is_asked_at_most_twice(complete: bool) -> None:
    stub = Stub(many(500, 3), complete=complete)  # always the same short, hidden-heavy answer
    page = list_visible(stub, m.PrQuery(limit=100), ("github.com/u/old",))
    assert len(stub.asked) <= 2
    assert page.complete is complete  # the backend's own answer, never invented


def test_an_unrelated_incompleteness_is_asked_twice_at_most_and_kept_as_is() -> None:
    stub = Stub(many(0, 5), complete=False)  # nothing hidden: no widening, real reason stays
    page = list_visible(stub, m.PrQuery(limit=100), ("github.com/u/old",))
    assert stub.asked == [101] and page.complete is False


def test_the_backend_is_given_a_skip_for_hidden_repos() -> None:
    stub = Stub(many(1, 1))
    list_visible(stub, m.PrQuery(limit=10), ("github.com/u/old",))
    (skip,) = stub.skips
    assert skip("github.com/U/OLD") is True and skip("github.com/u/new") is False


def test_a_query_for_a_hidden_repo_is_empty_and_complete_without_a_call() -> None:
    stub = Stub(many(5, 5))
    repo = RepoRef(host="github.com", backend="github", path="u/old")
    page = list_visible(
        stub, m.PrQuery(repo=repo, host="github.com", limit=10), ("github.com/u/*",)
    )
    assert page.items == () and page.complete is True
    assert stub.asked == []


# --- the matcher ---------------------------------------------------------------------------------


def test_a_pathological_pattern_is_cheap() -> None:
    pattern = "github.com/" + "*a" * 12 + "b"
    text = "github.com/" + "a" * 200
    started = time.perf_counter()
    assert glob_match(pattern, text) is False
    assert time.perf_counter() - started < 1.0
    assert ignored("a" * 200, ["*a" * 12 + "b"]) is False


@pytest.mark.parametrize(
    ("pattern", "text", "expected"),
    [
        ("", "", True),
        ("", "a", False),
        ("*", "", True),
        ("*", "a/b\n", True),
        ("a*b*c", "aXbYc", True),
        ("a*b*c", "aXbYcZ", False),
        ("a?c", "a\nc", True),
        ("a**b", "ab", True),
        ("dependabot[bot]", "dependabot[bot]", True),
        ("dependabot[bot]", "dependabotb", False),
        ("USER-*", "user-12", True),
        ("*x", "abxx", True),
        ("*x*", "abc", False),
        ("é*", "É1", False),  # only ASCII letters fold
    ],
)
def test_glob_match_rules(pattern: str, text: str, expected: bool) -> None:
    assert glob_match(pattern, text) is expected


# --- the CLI, GitHub -----------------------------------------------------------------------------


def hidden_config(tmp_path: Path, *entries: str) -> Path:
    path = tmp_path / "hidden.yaml"
    listed = "".join(f"  - {e}\n" for e in entries)
    path.write_text(
        "retries: 0\nhidden_repos:\n" + listed + "hosts:\n  github.com:\n    token_env: TOK_A\n",
        encoding="utf-8",
    )
    return path


def assert_no_trace(done: subprocess.CompletedProcess[str], *names: str) -> None:
    """No hidden repo id or name in stdout, stderr (errors included) and the audit record."""
    seen = done.stdout + done.stderr
    for name in names:
        assert name not in seen, name


def test_prs_never_lists_a_hidden_repo(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", body=recording())
    cfg = hidden_config(tmp_path, "github.com/user-7/example-repo")
    done = prs(run_forgectl, cfg, "--host", "github.com", "--author", "user-1")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert [i["number"] for i in out["items"]] == [12]
    assert out["complete"] is True and out["errors"] == []
    assert set(out) == {"items", "complete", "errors", "hosts_queried", "rate_limit"}  # no count
    assert_no_trace(done, "example-repo", "user-7/example-repo")


def test_a_wildcard_hides_every_matching_repo(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", body=recording())
    cfg = hidden_config(tmp_path, "GITHUB.COM/user-7/*")
    done = prs(run_forgectl, cfg, "--host", "github.com", "--author", "user-1")
    out = json.loads(done.stdout)
    assert out["items"] == [] and out["complete"] is True


def test_prs_keeps_repos_that_do_not_match(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", body=recording())
    cfg = hidden_config(tmp_path, "github.com/user-7/example-repo2", "github.com/other/*")
    done = prs(run_forgectl, cfg, "--host", "github.com", "--author", "user-1")
    assert [i["number"] for i in json.loads(done.stdout)["items"]] == [31, 29, 12]


def test_hidden_items_do_not_consume_the_limit_on_the_cli(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", body=recording())  # 31, 29 hidden, then 12
    cfg = hidden_config(tmp_path, "github.com/user-7/example-repo")
    done = prs(run_forgectl, cfg, "--host", "github.com", "--author", "user-1", "--limit", "1")
    assert done.returncode == 0, done.stdout + done.stderr
    out = json.loads(done.stdout)
    assert [i["number"] for i in out["items"]] == [12]
    assert out["complete"] is True
    assert len(fake_forges.calls("gh")) == 1


def test_a_named_pr_is_unaffected_by_hiding(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    snapshot_script(fake_forges)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "hidden_repos: [github.com/user-7/*]\nhosts:\n  github.com:\n    token_env: SHOW_TOKEN\n",
        encoding="utf-8",
    )
    done = run_forgectl(
        ["snapshot", PR, "--json", "--config", str(cfg)], env={"SHOW_TOKEN": SHOW_TOKEN}
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["pr"] == PR


def test_an_invalid_entry_exits_2_naming_the_key(run_forgectl: Run, tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text('hidden_repos: [""]\n', encoding="utf-8")
    done = run_forgectl(
        ["prs", "--json", "--config", str(cfg), "--host", "github.com", "--author", "user-1"]
    )
    assert done.returncode == 2
    assert "config_invalid" in done.stderr and "hidden_repos" in done.stderr


# --- the CLI, GitLab -----------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["gitlab.com/user-9/proj", "gitlab.com/user-9/*", "gitlab.com/*"])
def test_gitlab_prs_never_lists_a_hidden_repo(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path, entry: str
) -> None:
    gl_script(fake_forges)
    cfg = gl_config(tmp_path, f"hidden_repos: ['{entry}']\n")
    done = run_forgectl(
        [
            "prs",
            "--json",
            "--config",
            str(cfg),
            "--repo",
            "gitlab.com/user-9/proj",
            "--state",
            "all",
        ],
        env={"GL_TOKEN": TOKEN_A, "GH_TOKEN": TOKEN_A},
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert out["items"] == [] and out["complete"] is True


def test_gitlab_nested_group_is_matched_by_a_group_glob(tmp_path: Path) -> None:
    assert shown(["gitlab.com/g/sub/proj"], ("gitlab.com/g/*",)) == []
    assert shown(["gitlab.com/g/sub/proj"], ("gitlab.com/g/?",)) == ["gitlab.com/g/sub/proj"]


LONG_OK = "github.com/" + "a" * 244  # 255 characters
LONG_BAD = LONG_OK + "a"  # 256


def schema_accepts(entry: str) -> bool:
    from jsonschema import Draft202012Validator

    schema = json.loads((Path(__file__).parent.parent / "schemas" / "config.json").read_text())
    return not list(Draft202012Validator(schema).iter_errors({"hidden_repos": [entry]}))


def model_accepts(entry: str) -> bool:
    try:
        Config.model_validate({"hidden_repos": [entry]})
    except ValueError:
        return False
    return True


@pytest.mark.parametrize(
    ("entry", "valid"),
    [
        ("github.com/user-1/old", True),
        ("gitlab.example.test:8443/g/sub/p", True),
        ("*/user-1/*?", True),
        ("github.com", False),
        ("github.com/../x", False),
        ("github.com//x", False),
        ("github.com/a b", False),
        ("github.com/a[b]", False),
        ("github.com/a/b\n", False),  # a trailing newline
        ("github.com/a/b\r", False),
        ("\ngithub.com/a/b", False),
        ("github.com/a/..\n", False),
        ("github.com/a/.\n", False),
        ("github.com/a/", False),
        ("*/*", True),
        (":/b", True),  # the host part is a loose glob, not checked as a hostname
        ("-/b", True),
        ("/b", False),  # a host part is required
        ("", False),
        (LONG_OK, True),
        (LONG_BAD, False),
    ],
)
def test_the_schema_and_the_model_agree_on_an_entry(entry: str, valid: bool) -> None:
    assert schema_accepts(entry) is model_accepts(entry) is valid


def test_an_entry_over_255_characters_is_config_invalid(tmp_path: Path) -> None:
    assert load(tmp_path, f"hidden_repos: ['{LONG_OK}']\n").hidden_repos == (LONG_OK,)
    with pytest.raises(ForgectlError) as caught:
        load(tmp_path, f"hidden_repos: ['{LONG_BAD}']\n")
    assert caught.value.code == "config_invalid"
    assert caught.value.message.startswith("hidden_repos")


# --- more of the CLI, GitHub ---------------------------------------------------------------------


def test_prs_for_a_hidden_repo_is_empty_and_makes_no_host_call(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", body=recording())
    cfg = hidden_config(tmp_path, "github.com/user-7/*")
    done = prs(run_forgectl, cfg, "--repo", "github.com/user-7/example-repo")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert out["items"] == [] and out["complete"] is True and out["errors"] == []
    assert fake_forges.calls("gh") == []
    assert_no_trace(done, "example-repo")


def test_a_hidden_pr_is_not_enriched_and_its_failure_cannot_leak_on_github(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    doc = recording()
    # #12 (other-repo) has more label pages to read; the only scripted answer is the search page
    nodes_of(doc)[2]["labels"]["pageInfo"] = {"hasNextPage": True, "endCursor": "c1"}
    fake_forges.answer("gh", "graphql", method="POST", body=doc)
    cfg = hidden_config(tmp_path, "github.com/user-7/other-repo")
    done = prs(run_forgectl, cfg, "--host", "github.com", "--author", "user-1")
    assert done.returncode == 0, done.stdout + done.stderr
    out = json.loads(done.stdout)
    assert [i["number"] for i in out["items"]] == [31, 29] and out["errors"] == []
    assert len(fake_forges.calls("gh")) == 1, "no label read for a hidden PR"
    assert_no_trace(done, "other-repo")


def test_show_and_diff_of_a_hidden_repos_pr_still_work(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "hidden_repos: [github.com/user-7/*]\nhosts:\n  github.com:\n    token_env: SHOW_TOKEN\n",
        encoding="utf-8",
    )
    fake_forges.answer("gh", "graphql", method="POST", body=show_recording())
    shown_pr = run_forgectl(
        ["show", PR, "--json", "--config", str(cfg)], env={"SHOW_TOKEN": SHOW_TOKEN}
    )
    assert shown_pr.returncode == 0, shown_pr.stderr
    assert json.loads(shown_pr.stdout)["pr"] == PR


def test_diff_of_a_hidden_repos_pr_still_works(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "hidden_repos: [github.com/user-7/*]\nhosts:\n  github.com:\n    token_env: SHOW_TOKEN\n",
        encoding="utf-8",
    )
    doc = diff_pull(changed_files=1)
    fake_forges.answer("gh", PR_ENDPOINT, body=doc, times=1)
    fake_forges.answer("gh", PR_ENDPOINT, body_text=(RECORDINGS / "pull.diff").read_text(), times=1)
    fake_forges.answer("gh", endpoint_re=r"/compare/", body=diff_recording("merge-base"))
    fake_forges.answer("gh", PR_ENDPOINT, body=doc)
    done = run_forgectl(
        ["diff", PR, "--json", "--config", str(cfg)], env={"SHOW_TOKEN": SHOW_TOKEN}
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["diff"].startswith("diff --git")


# --- more of the CLI, GitLab ---------------------------------------------------------------------


def mixed_gitlab(forges: FakeForges) -> None:
    """A host-wide list: the newest MR is in a repo to hide (its head project and author would
    each need a further read), then the recorded MRs of another repo."""
    gl_script(forges)
    recorded = gl_mrs()
    for mr in recorded:
        mr["author"] = recorded[0]["author"]  # one author, so `--author` keeps them all
    newest = copy.deepcopy(recorded[0])
    newest.update(
        iid=40,
        id=90040,
        project_id=303,
        source_project_id=505,
        target_project_id=303,
        web_url="https://gitlab.com/user-8/hidden-proj/-/merge_requests/40",
        updated_at="2026-10-06T12:00:00.000Z",
    )
    newest["author"] = {"id": 77, "username": "user-1", "name": "User 77", "state": "active"}
    newest["reviewers"] = []
    gl_route(forges, "merge_requests", [newest, *recorded])


def gitlab_paths(forges: FakeForges) -> str:
    return " ".join(" ".join(c.argv) for c in forges.calls("glab"))


def test_gitlab_mixed_list_hides_one_repo_and_keeps_the_limit(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    mixed_gitlab(fake_forges)
    cfg = gl_config(tmp_path, "hidden_repos: ['gitlab.com/user-8/*']\n")
    done = run_forgectl(
        ["prs", "--json", "--config", str(cfg), "--host", "gitlab.com", "--state", "all"]
        + ["--author", "user-1", "--limit", "4"],
        env={"GL_TOKEN": TOKEN_A, "GH_TOKEN": TOKEN_A},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert [i["number"] for i in out["items"]] == [31, 29, 12, 5]  # not [40, 31, 29, 12]
    assert out["complete"] is True and out["errors"] == []
    seen = gitlab_paths(fake_forges)
    assert "projects/505" not in seen and "users/77" not in seen  # no enrichment of the hidden MR
    assert_no_trace(done, "hidden-proj", "gitlab.com/user-8/hidden")


def test_gitlab_a_hidden_item_past_the_limit_keeps_the_list_complete(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    mixed_gitlab(fake_forges)
    gl_route(fake_forges, "projects/505", {"message": "404 Project Not Found"}, status=404)
    gl_route(fake_forges, "users/77", {"id": 77, "username": "user-1", "bot": False})
    cfg = gl_config(tmp_path, "hidden_repos: ['gitlab.com/user-9/*']\n")
    done = run_forgectl(
        ["prs", "--json", "--config", str(cfg), "--host", "gitlab.com", "--state", "all"]
        + ["--author", "user-1", "--limit", "1"],
        env={"GL_TOKEN": TOKEN_A, "GH_TOKEN": TOKEN_A},
    )
    out = json.loads(done.stdout)
    assert [i["number"] for i in out["items"]] == [40] and out["complete"] is True


def test_gitlab_prs_for_a_hidden_repo_is_empty_with_no_call(
    run_forgectl: Run, fake_forges: FakeForges, tmp_path: Path
) -> None:
    mixed_gitlab(fake_forges)
    cfg = gl_config(tmp_path, "hidden_repos: ['gitlab.com/user-9/proj']\n")
    done = run_forgectl(
        ["prs", "--json", "--config", str(cfg), "--repo", "gitlab.com/user-9/proj"],
        env={"GL_TOKEN": TOKEN_A, "GH_TOKEN": TOKEN_A},
    )
    out = json.loads(done.stdout)
    assert done.returncode == 0 and out["items"] == [] and out["complete"] is True
    assert fake_forges.calls("glab") == []
