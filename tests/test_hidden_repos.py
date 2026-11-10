"""``hidden_repos`` (#96): config rules, glob matching, the listing filter and its limit rules,
and the CLI end to end on both hosts. Hiding applies to listings only; a command naming a PR is
unaffected."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forgectl import model as m
from forgectl.config import Config, config_hash, load_config
from forgectl.errors import ForgectlError
from forgectl.fanout import list_visible

from .harness import schemas
from .harness.forges import FakeForges
from .test_github_lists import nodes_of, recording
from .test_github_show import TOKEN as SHOW_TOKEN
from .test_gitlab_lists_cli import config as gl_config
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

    def __init__(self, items: list[Any]) -> None:
        self.items = items
        self.asked: list[int] = []

    def list_prs(self, query: m.PrQuery) -> m.PrPage:
        self.asked.append(query.limit)
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
    stub = Stub(many(150, 300))
    page = list_visible(stub, m.PrQuery(limit=100), ("github.com/u/old",))
    assert [i.repo for i in page.items[:100]] == ["github.com/u/new"] * 100
    assert len(page.items) > 100  # the caller's merge cuts to the limit and says incomplete
    assert page.complete is False
    assert stub.asked == [100, 200, 400]


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
    assert stub.asked == [100]


def test_the_widening_stops_at_the_search_cap() -> None:
    stub = Stub(many(2000, 5))
    page = list_visible(stub, m.PrQuery(limit=100), ("github.com/u/old",))
    assert stub.asked[-1] == 1000 and max(stub.asked) == 1000
    assert page.items == () and page.complete is False  # past the cap: honest, not complete


# --- the CLI, GitHub -----------------------------------------------------------------------------


def hidden_config(tmp_path: Path, *entries: str) -> Path:
    path = tmp_path / "hidden.yaml"
    listed = "".join(f"  - {e}\n" for e in entries)
    path.write_text(
        "retries: 0\nhidden_repos:\n" + listed + "hosts:\n  github.com:\n    token_env: TOK_A\n",
        encoding="utf-8",
    )
    return path


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
    assert "example-repo" not in json.dumps(out)
    assert "hidden" not in done.stdout + done.stderr


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
    first = recording()  # the host cuts the answer to the 2 items asked for (limit 1, plus one)
    first["data"]["search"]["nodes"] = nodes_of(first)[:2]
    fake_forges.answer("gh", "graphql", method="POST", times=1, body=first)
    fake_forges.answer("gh", "graphql", method="POST", body=recording())  # the wider second ask
    cfg = hidden_config(tmp_path, "github.com/user-7/example-repo")
    done = prs(run_forgectl, cfg, "--host", "github.com", "--author", "user-1", "--limit", "1")
    assert done.returncode == 0, done.stdout + done.stderr
    out = json.loads(done.stdout)
    assert [i["number"] for i in out["items"]] == [12]
    assert out["complete"] is True


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
    ],
)
def test_the_schema_and_the_model_agree_on_an_entry(entry: str, valid: bool) -> None:
    from jsonschema import Draft202012Validator

    schema = json.loads((Path(__file__).parent.parent / "schemas" / "config.json").read_text())
    ok = not list(Draft202012Validator(schema).iter_errors({"hidden_repos": [entry]}))
    try:
        Config.model_validate({"hidden_repos": [entry]})
        accepted = True
    except ValueError:
        accepted = False
    assert ok is accepted is valid
