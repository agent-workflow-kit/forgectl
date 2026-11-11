"""`prs --query` (#139): the browse query applied to the list, with the engine of the TUI."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forgectl import prsquery
from forgectl.config import LoadedConfig
from forgectl.errors import ForgectlError
from forgectl.fanout import PrsArgs, resolve_since
from forgectl.identifiers import bind_repo, parse_repo
from forgectl.surface import parse
from forgectl.tui import columns, filtering, query
from forgectl.tui.source import Row

from .bulk_support import BulkRouter
from .harness import schemas
from .harness.forges import FakeForges
from .test_github_lists import recording
from .test_lists_cli import GHE, TOKEN_A, TOKEN_B, prs, script_both
from .test_snapshots_command import make

NOW = datetime(2026, 10, 3, tzinfo=UTC)
Run = Callable[..., subprocess.CompletedProcess[str]]
REPO = "github.com/acme/app"


@pytest.fixture
def two(tmp_path: Path) -> Path:
    path = tmp_path / "two.yaml"
    path.write_text(
        "retries: 0\n"
        "hosts:\n"
        "  github.com:\n    token_env: TOK_A\n"
        f"  {GHE}:\n    backend: github\n    token_env: TOK_B\n",
        encoding="utf-8",
    )
    return path


def item(number: int, *, login: str = "carol", draft: bool = False, day: int = 2) -> dict[str, Any]:
    return {
        "pr": f"{REPO}#{number}",
        "repo": REPO,
        "number": number,
        "title": f"change {number}",
        "author": {"login": login, "id": "1", "is_bot": False},
        "state": "open",
        "draft": draft,
        "updated_at": f"2026-10-{day:02d}T00:00:00Z",
    }


def snap(number: int, ci: str = "passing", review: str = "approved") -> dict[str, Any]:
    return {
        "pr": f"{REPO}#{number}",
        "ci": {"state": ci},
        "review_decision": review,
        "approvals": 1,
        "blocked_by": [],
        "complete": True,
        "updated_at": "2026-10-02T00:00:00Z",
    }


def listing(*items: dict[str, Any]) -> dict[str, Any]:
    return {
        "items": list(items),
        "complete": True,
        "errors": [],
        "hosts_queried": ["github.com"],
        "rate_limit": {"host": "github.com", "remaining": None, "reset_at": None},
    }


class Env:
    """The slice of ``ReadEnv`` that ``apply`` touches."""

    def __init__(self, config: Any = None) -> None:
        self.gateway = SimpleNamespace(
            config=LoadedConfig.defaults() if config is None else SimpleNamespace(config=config),
            rate_readings=[],
            budget=None,
        )
        self.hosts = SimpleNamespace(backend=lambda host: "github", me=lambda host: "dave")


def reader(
    snaps: list[dict[str, Any]], failed: dict[int, str] | None = None, errors: Any = ()
) -> Callable[..., dict[str, Any]]:
    calls: list[list[str]] = []

    def read(env: Any, refs: list[Any]) -> dict[str, Any]:
        calls.append([str(r) for r in refs])
        return {
            "items": snaps,
            "failed": [
                {"pr": f"{REPO}#{n}", "error": {"code": c, "message": "boom"}}
                for n, c in (failed or {}).items()
            ],
            "errors": list(errors),
        }

    read.calls = calls  # type: ignore[attr-defined]
    return read


def run(
    text: str,
    out: dict[str, Any],
    read: Callable[..., dict[str, Any]],
    scope: prsquery.Scope | None = None,
) -> dict[str, Any]:
    q = prsquery.compile_query(text)
    return prsquery.apply(
        Env(),  # type: ignore[arg-type]
        out,
        q,
        scope or prsquery.Scope(author_is_me=False, requested_from_me=False),
        now=NOW,
        read=read,
    )


def numbers(out: dict[str, Any]) -> list[int]:
    return [i["number"] for i in out["items"]]


# -- which queries read snapshots ----------------------------------------------------------------


def test_every_field_and_column_is_classified() -> None:
    assert prsquery.SNAPSHOT_FIELDS | prsquery.LIST_FIELDS == set(query.FIELDS)
    assert not prsquery.SNAPSHOT_FIELDS & prsquery.LIST_FIELDS
    assert prsquery.SNAPSHOT_COLUMNS | prsquery.LIST_COLUMNS == set(query.ORDER_COLUMNS)
    assert not prsquery.SNAPSHOT_COLUMNS & prsquery.LIST_COLUMNS


@pytest.mark.parametrize(
    "text",
    [
        "",
        "repo LIKE 'acme/*' AND author = carol AND NOT draft AND age < 3d",
        "title LIKE 'x*' OR text LIKE 'y*' ORDER BY age DESC, title",
        "state = open ORDER BY pr DESC",
    ],
)
def test_a_query_on_list_fields_reads_no_snapshot(text: str) -> None:
    read = reader([])
    out = run(text, listing(item(1), item(2)), read)
    assert read.calls == []  # type: ignore[attr-defined]
    assert "snapshot_failures" not in out


@pytest.mark.parametrize(
    "text",
    [
        "ci = failing",
        "NOT (author = bob OR review = approved)",
        "blocker IS NULL",
        "comments > 0",
        "pushed < 1d",
        "NOT draft ORDER BY badge",
        "NOT draft ORDER BY comments DESC",
    ],
)
def test_a_query_on_a_snapshot_field_reads_the_snapshots_once(text: str) -> None:
    read = reader([snap(1), snap(2)])
    run(text, listing(item(1), item(2)), read)
    assert read.calls == [[f"{REPO}#1", f"{REPO}#2"]]  # type: ignore[attr-defined]


# -- filter, order and ties ----------------------------------------------------------------------


def test_filter_and_order_are_the_engine_of_the_tui() -> None:
    items = [item(3, day=1), item(1, day=3), item(2, day=2), item(4, day=2)]
    snaps = [snap(1, "failing"), snap(2, "passing"), snap(3, "failing"), snap(4, "pending")]
    text = "ci IN (failing, pending) ORDER BY ci, pr DESC"
    out = run(text, listing(*items), reader(snaps))
    rows = [Row(item=i) for i in items]
    states = {s["pr"]: columns.RowState(columns.LOADED, columns.summarize(s)) for s in snaps}
    q = prsquery.compile_query(text)
    want = columns.order(
        [r for r in rows if filtering.matches(q.where, r, states[r.pr], NOW)],
        states,
        columns.SORT_REPO,
        filtering.sort_of(q.order),
    )
    assert numbers(out) == [r.item["number"] for r in want] == [3, 1, 4]


def test_ties_end_on_the_pr_ascending() -> None:
    items = [item(10), item(9), item(11)]
    out = run("ORDER BY ci DESC", listing(*items), reader([snap(n) for n in (9, 10, 11)]))
    assert numbers(out) == [9, 10, 11]


def test_without_order_by_the_list_order_stays() -> None:
    out = run("NOT draft", listing(item(5, day=3), item(2, day=2), item(9, draft=True)), reader([]))
    assert numbers(out) == [5, 2]


def test_an_empty_result_is_an_empty_complete_list() -> None:
    out = run("author = nobody", listing(item(1)), reader([]))
    assert out["items"] == [] and out["complete"] is True and out["query"] == "author = nobody"


def test_an_empty_listing_reads_no_snapshot() -> None:
    read = reader([])
    out = run("ci = failing", listing(), read)
    assert out["items"] == [] and read.calls == []  # type: ignore[attr-defined]


# -- rows whose snapshot failed ------------------------------------------------------------------


def test_a_row_whose_snapshot_failed_is_reported_and_sorts_last() -> None:
    items = [item(1), item(2), item(3)]
    out = run(
        "NOT ci = failing OR draft ORDER BY ci DESC",
        listing(*items),
        reader([snap(1, "passing"), snap(3, "pending")], failed={2: "rate_limited"}),
    )
    assert numbers(out) == [1, 3]  # #2 is unknown: neither true nor false
    assert out["snapshot_failures"] == [
        {
            "pr": f"{REPO}#2",
            "error": {"code": "rate_limited", "message": "boom"},
            "in_items": False,
        }
    ]
    assert out["complete"] is False


def test_a_failed_row_the_filter_decides_without_the_snapshot_is_shown_and_marked() -> None:
    out = run(
        "draft OR ci = failing ORDER BY ci",
        listing(item(1, draft=True), item(2)),
        reader([], failed={1: "not_found", 2: "not_found"}),
    )
    assert numbers(out) == [1]
    assert [(f["pr"][-1], f["in_items"]) for f in out["snapshot_failures"]] == [
        ("1", True),
        ("2", False),
    ]


def test_a_row_the_read_never_answered_is_not_read() -> None:
    out = run("ci = passing", listing(item(1), item(2)), reader([snap(1)]))
    assert numbers(out) == [1]
    assert out["snapshot_failures"][0]["error"]["code"] == prsquery.NOT_READ


def test_is_null_selects_a_failed_row() -> None:
    out = run("ci IS NULL", listing(item(1), item(2)), reader([snap(2)], failed={1: "x"}))
    assert numbers(out) == [1]


def test_a_stopped_host_is_an_error_of_the_output() -> None:
    stop = {"host": "github.com", "code": "rate_limited", "message": "slow down"}
    out = run(
        "ci = passing", listing(item(1)), reader([], failed={1: "rate_limited"}, errors=[stop])
    )
    assert out["errors"] == [stop] and out["items"] == []
    assert out["snapshot_failures"][0]["in_items"] is False


def test_a_malformed_snapshot_item_costs_only_that_row() -> None:
    bad: dict[str, Any] = {"pr": f"{REPO}#1", "ci": "not an object", "approvals": "x"}
    out = run("ci = passing", listing(item(1), item(2)), reader([bad, snap(2)]))
    assert numbers(out) == [2]


# -- viewer --------------------------------------------------------------------------------------


def test_author_me_with_author_me_listed_is_every_row() -> None:
    scope = prsquery.Scope(author_is_me=True, requested_from_me=False)
    out = run("author = me AND authored", listing(item(1), item(2)), reader([]), scope)
    assert numbers(out) == [1, 2]


def test_author_me_is_the_login_of_me_on_the_host_of_the_row() -> None:
    out = run(
        "author = me", listing(item(1, login="dave"), item(2), item(3, login="DAVE")), reader([])
    )
    assert numbers(out) == [1, 3]


OTHER = "other.example"


def two_hosts_env(calls: list[str]) -> Any:
    """``me`` is not configured; the authenticated user is rate limited on ``OTHER`` only."""
    env = Env()

    def authenticated_user(host: str) -> Any:
        calls.append(host)
        if host == OTHER:
            raise ForgectlError("rate_limited", "slow down", retry_after=5)
        return SimpleNamespace(login="dave")

    env.hosts = SimpleNamespace(
        backend=lambda host: "github",
        me=lambda host: None,
        target=lambda host: host,
        authenticated_user=authenticated_user,
    )
    env.call = lambda fn: fn()  # type: ignore[attr-defined]
    return env


def two_hosts_listing() -> dict[str, Any]:
    far = {**item(2, login="dave"), "pr": f"{OTHER}/acme/app#2", "repo": f"{OTHER}/acme/app"}
    return {**listing(item(1, login="dave"), far), "hosts_queried": ["github.com", OTHER]}


def test_a_host_whose_me_cannot_be_resolved_is_an_error_and_its_rows_are_left_out() -> None:
    calls: list[str] = []
    q = prsquery.compile_query("authored OR draft")
    scope = prsquery.Scope(author_is_me=False, requested_from_me=False)
    out = prsquery.apply(
        two_hosts_env(calls),  # type: ignore[arg-type]
        two_hosts_listing(),
        q,
        scope,
        now=NOW,
        read=reader([]),
    )
    assert numbers(out) == [1] and calls == ["github.com", OTHER]  # each host asked once
    assert [(e["host"], e["code"]) for e in out["errors"]] == [(OTHER, "rate_limited")]
    assert out["complete"] is False


def test_a_failed_me_is_partial_results_with_the_output() -> None:
    q = prsquery.compile_query("author IN (me, x)")
    with pytest.raises(ForgectlError) as err:
        prsquery.run(
            two_hosts_env([]),  # type: ignore[arg-type]
            replace(BASE, author="erin"),
            q,
            state_given=True,
            now=NOW,
            lister=lambda env, args: two_hosts_listing(),
            read=reader([]),
        )
    assert err.value.code == "partial_results" and err.value.output is not None
    assert [i["number"] for i in err.value.output["items"]] == [1]
    assert [e["host"] for e in err.value.output["errors"]] == [OTHER]


def test_requested_is_the_listing_scope() -> None:
    scope = prsquery.Scope(author_is_me=False, requested_from_me=True)
    out = run("requested ORDER BY roles", listing(item(1)), reader([]), scope)
    assert numbers(out) == [1]


def test_requested_without_the_flag_is_refused_before_any_call() -> None:
    q = prsquery.compile_query("requested OR draft")
    with pytest.raises(ForgectlError) as err:
        prsquery.check_scope(q, None)
    assert err.value.code == "usage"
    prsquery.check_scope(q, "me")


# -- the real bulk reader ------------------------------------------------------------------------


def test_the_snapshots_come_from_the_bulk_reader(fake_forges: FakeForges) -> None:
    env = make(fake_forges, BulkRouter()).env
    items = [
        {
            **item(7),
            "repo": "github.com/user-7/example-repo",
            "pr": "github.com/user-7/example-repo#7",
        },
        {
            **item(8),
            "repo": "github.com/user-7/example-repo",
            "pr": "github.com/user-7/example-repo#8",
        },
    ]
    out = prsquery.apply(
        env,
        listing(*items),
        prsquery.compile_query("review IS NOT NULL ORDER BY pr DESC"),
        prsquery.Scope(author_is_me=False, requested_from_me=False),
        now=NOW,
    )
    assert numbers(out) == [8, 7]
    assert "snapshot_failures" not in out


# -- the command line ----------------------------------------------------------------------------


def one_failure(text: str) -> str:
    with pytest.raises(ForgectlError) as err:
        parse(["prs", "--author", "me", "--query", text])
    assert err.value.code == "usage"
    return err.value.message


def test_an_invalid_query_is_usage_with_the_position() -> None:
    assert "(column 4)" in one_failure("ci ~ x")


@pytest.mark.parametrize(
    "text",
    [
        "title = 'a\x07b'",
        "title = a\x1bb",
        "title = a‮b",
        "title = " + "x" * 1001,
        "(" * 21 + "draft" + ")" * 21,
        " AND ".join(["draft"] * 51),
        "ci IN (" + ",".join(["failing"] * 51) + ")",
        "ORDER BY pr, pr",
        "ORDER BY nope",
        "'",
    ],
)
def test_hostile_query_text_is_usage(text: str) -> None:
    one_failure(text)


def test_query_and_default_query_are_exclusive() -> None:
    with pytest.raises(ForgectlError) as err:
        parse(["prs", "--author", "me", "--query", "draft", "--default-query"])
    assert err.value.code == "usage"


def test_a_query_given_twice_is_usage() -> None:
    with pytest.raises(ForgectlError):
        parse(["prs", "--author", "me", "--query", "draft", "--query", "NOT draft"])


def test_the_default_query_is_never_applied_unasked(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    two.write_text(two.read_text() + "browse:\n  query: draft\n")
    script_both(fake_forges)
    done = prs(run_forgectl, two, "--author", "user-1")
    out = json.loads(done.stdout)
    assert [i["number"] for i in out["items"]] == [90, 31, 29, 12] and "query" not in out


def test_default_query_applies_the_config_query(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    two.write_text(two.read_text() + "browse:\n  query: draft\n")
    script_both(fake_forges)
    done = prs(run_forgectl, two, "--author", "user-1", "--default-query")
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert [i["number"] for i in out["items"]] == [29] and out["query"] == "draft"


def test_end_to_end_filters_orders_and_reads_no_snapshot(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    script_both(fake_forges)
    done = prs(run_forgectl, two, "--author", "user-1", "--query", "NOT draft ORDER BY pr DESC")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert [i["number"] for i in out["items"]] == [12, 31, 90]
    assert out["query"] == "NOT draft ORDER BY pr DESC"
    assert all("graphql" in c.argv or "user" in c.argv for c in fake_forges.calls("gh"))


def test_an_invalid_query_exits_2_before_any_call(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    done = prs(run_forgectl, two, "--author", "user-1", "--query", "ci = ")
    assert done.returncode == 2
    assert "column" in done.stderr
    assert fake_forges.calls("gh") == []


def test_requested_without_its_flag_exits_2_before_any_call(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    done = prs(run_forgectl, two, "--author", "user-1", "--query", "requested OR draft")
    assert done.returncode == 2 and fake_forges.calls("gh") == []


def test_no_role_and_nothing_the_query_supplies_exits_2_before_any_call(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    done = prs(run_forgectl, two, "--query", "draft OR authored")
    assert done.returncode == 2 and "top-level" in done.stderr
    assert fake_forges.calls("gh") == []


def test_a_host_alone_with_a_query_exits_2_before_any_call(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    done = prs(run_forgectl, two, "--host", "github.com", "--query", "draft")
    assert done.returncode == 2 and "without --repo" in done.stderr
    assert fake_forges.calls("gh") == []


def test_a_failed_host_is_partial_results_with_the_query_applied(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    script_both(fake_forges, ghe_status=502)
    done = prs(run_forgectl, two, "--author", "user-1", "--query", "NOT draft")
    assert done.returncode == 12
    out = json.loads(done.stdout)
    schemas.validate_cli_output("prs", out)
    assert [i["number"] for i in out["items"]] == [31, 12]
    assert [e["host"] for e in out["errors"]] == [GHE] and out["complete"] is False
    assert TOKEN_A not in done.stdout + done.stderr and TOKEN_B not in done.stdout + done.stderr


@pytest.fixture
def me_config(tmp_path: Path) -> Path:
    path = tmp_path / "me.yaml"
    path.write_text(
        "retries: 0\nme:\n  github: user-9\nhosts:\n  github.com:\n    token_env: TOK_A\n",
        encoding="utf-8",
    )
    return path


def asked_states(forges: FakeForges) -> list[str]:
    """The state words of the searches the fake GitHub received."""
    found = []
    for call in forges.calls("gh"):
        stdin = call.stdin or ""
        found += [w for w in ("is:open", "is:merged", "is:closed") if w in stdin]
    return found


def test_a_state_in_the_query_is_asked_when_the_flag_is_absent(
    run_forgectl: Run, fake_forges: FakeForges, me_config: Path
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", stdin_re="is:merged", body=recording())
    done = prs(
        run_forgectl,
        me_config,
        "--host",
        "github.com",
        "--author",
        "me",
        "--query",
        "state = merged",
    )
    assert done.returncode == 0, done.stderr
    assert asked_states(fake_forges) == ["is:merged"]
    schemas.validate_cli_output("prs", json.loads(done.stdout))


def test_a_state_flag_and_a_contradicting_query_give_the_intersection(
    run_forgectl: Run, fake_forges: FakeForges, me_config: Path
) -> None:
    fake_forges.answer("gh", "graphql", method="POST", stdin_re="is:open", body=recording())
    done = prs(
        run_forgectl,
        me_config,
        "--host",
        "github.com",
        "--author",
        "me",
        "--state",
        "open",
        "--query",
        "state = merged",
    )
    assert done.returncode == 0, done.stderr
    assert asked_states(fake_forges) == ["is:open"]
    assert json.loads(done.stdout)["items"] == []


def test_the_empty_query_changes_nothing_but_says_so(
    run_forgectl: Run, fake_forges: FakeForges, two: Path
) -> None:
    script_both(fake_forges)
    out = json.loads(prs(run_forgectl, two, "--author", "user-1", "--query", "").stdout)
    assert [i["number"] for i in out["items"]] == [90, 31, 29, 12] and out["query"] == ""


# -- no Textual ----------------------------------------------------------------------------------


def test_the_engine_imports_without_textual_or_rich() -> None:
    code = (
        "import sys\n"
        "for name in ('textual', 'rich', 'pygments', 'markdown_it'):\n"
        "    sys.modules[name] = None\n"
        "from forgectl import listing, prsquery, surface\n"
        "from forgectl.tui import columns, filtering, query\n"
        "q = prsquery.compile_query('ci = failing ORDER BY title')\n"
        "from forgectl.tui.source import Row\n"
        "row = Row(item={'pr': 'h/a#1', 'title': 'T', 'author': {'login': 'x'}}, roles=set())\n"
        "columns.order([row], {}, columns.SORT_REPO, filtering.sort_of(q.order))\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr


# -- pushdown (host-side narrowing) --------------------------------------------------------------

LIB = "github.com/acme/lib"
CONFIG = SimpleNamespace(hosts={"github.com": object()})
BASE = PrsArgs(
    repo=None,
    host=None,
    author=None,
    review_requested_from=None,
    state="open",
    draft=None,
    updated_since=None,
    limit=100,
)
#: (repo, number, author, state, draft, hours since update, logins asked to review)
DATA = [
    (REPO, 1, "dave", "open", False, 1, ()),
    (REPO, 2, "carol", "open", True, 30, ("dave",)),
    (REPO, 3, "carol", "merged", False, 50, ()),
    (REPO, 4, "dave", "closed", True, 100, ("carol",)),
    (LIB, 5, "dave", "open", False, 3, ("dave",)),
    (LIB, 6, "erin", "merged", False, 500, ()),
    (LIB, 7, "carol", "open", False, 20, ("dave",)),
    (LIB, 8, "Erin", "open", True, 2, ()),
]


def data_items() -> tuple[list[dict[str, Any]], dict[str, tuple[str, ...]]]:
    items, asked = [], {}
    for repo, n, login, state, draft, hours, reviewers in DATA:
        when = NOW - timedelta(hours=hours)
        found = {
            **item(n, login=login, draft=draft),
            "repo": repo,
            "pr": f"{repo}#{n}",
            "state": state,
            "updated_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        items.append(found)
        asked[found["pr"]] = reviewers
    return items, asked


class Host:
    """A list that honors the flags as a host does, and counts what it was asked. With
    ``any_role`` the ``me`` role filters (the stand-in scope of a test) keep every row."""

    def __init__(self, *, any_role: bool = False) -> None:
        self.items, self.reviewers = data_items()
        self.any_role = any_role
        self.asked: list[PrsArgs] = []

    def __call__(self, env: Any, args: PrsArgs) -> dict[str, Any]:
        self.asked.append(args)
        since = None if args.updated_since is None else resolve_since(args.updated_since, NOW)
        out = []
        for i in self.items:
            login = i["author"]["login"].casefold()
            ok = (
                (args.repo is None or i["repo"].casefold() == args.repo.id.casefold())
                and (args.state == "all" or i["state"] == args.state)
                and (args.draft is None or i["draft"] is args.draft)
                and (
                    args.author is None
                    or (self.any_role and args.author == "me")
                    or login == ("dave" if args.author == "me" else args.author.casefold())
                )
                and (
                    args.review_requested_from is None
                    or (
                        "dave" if args.review_requested_from == "me" else args.review_requested_from
                    )
                    in self.reviewers[i["pr"]]
                )
                and (since is None or i["updated_at"] >= since)
            )
            if ok:
                out.append(i)
        out.sort(key=lambda i: i["pr"])
        out.sort(key=lambda i: i["updated_at"], reverse=True)
        return {**listing(*out[: args.limit]), "complete": len(out) <= args.limit}


def go(
    text: str, args: PrsArgs, *, push: bool, state_given: bool = True, host: Host | None = None
) -> dict[str, Any]:
    env = Env(CONFIG)
    return prsquery.run(
        env,  # type: ignore[arg-type]
        args,
        prsquery.compile_query(text),
        state_given=state_given,
        push=push,
        now=NOW,
        lister=host or Host(),
        read=reader([]),
    )


def pushed_args(text: str, args: PrsArgs = BASE, *, state_given: bool = True) -> PrsArgs:
    return prsquery.narrow(
        prsquery.compile_query(text),
        args,
        CONFIG,  # type: ignore[arg-type]
        state_given=state_given,
    )


GRID = [
    "draft",
    "NOT draft",
    "state = merged",
    "state = closed AND draft",
    "state = OPEN AND NOT draft ORDER BY age",
    "state = bogus",
    "draft AND age < 2d",
    "age < 1h",
    "age <= 3h AND state != merged",
    "age < 0m",
    "age > 2d AND age < 9d",
    "age <= 3d OR draft",
    "NOT (state = open)",
    "NOT (draft AND state = open)",
    "author = carol",
    "author = CAROL",
    "author = erin ORDER BY pr DESC",
    "author = 'Car ol'",
    "author IN (carol, dave)",
    "author = me AND NOT draft",
    f"repo = {LIB}",
    f"repo = {LIB} AND state = open AND author = carol",
    "repo = github.com/acme/none",
    "repo = 'not a repo'",
    "repo = unknown.example/a/b",
    "title LIKE 'change 1*' AND age < 10d",
    "text LIKE 'carol*' AND state = open",
    "number > 3 AND draft",
    "(draft OR state = merged) AND age < 5d",
    "draft AND draft = false",
    "",
]


@pytest.mark.parametrize("text", GRID)
def test_pushing_the_query_down_never_changes_the_result(text: str) -> None:
    plain, narrowed = Host(any_role=True), Host(any_role=True)
    # a role stands in for the scope the unpushed run needs; state is given, so "all"
    scope = replace(BASE_HOST, state="all")
    want = go(text, scope, push=False, host=plain)
    got = go(text, scope, push=True, host=narrowed)
    assert got == want
    assert len(narrowed.asked) == 1 and len(narrowed.asked[0].__dict__) == len(
        plain.asked[0].__dict__
    )


def test_the_grid_really_narrows_something() -> None:
    plain, narrowed = Host(any_role=True), Host(any_role=True)
    scope = replace(BASE_HOST, state="all")
    go("draft AND age < 2d AND author = carol", scope, push=False, host=plain)
    go("draft AND age < 2d AND author = carol", scope, push=True, host=narrowed)
    asked = narrowed.asked[0]
    assert (asked.draft, asked.author, asked.updated_since) == (True, "me", "172800s")
    assert plain.asked[0].draft is None and plain.asked[0].updated_since is None


@pytest.mark.parametrize(
    "text, change",
    [
        ("draft", {"draft": True}),
        ("NOT draft", {"draft": False}),
        ("draft = true AND state = x", {"draft": True}),
        ("age < 2d", {"updated_since": "172800s"}),
        ("age <= 90m AND age < 3h", {"updated_since": "5400s"}),
        ("author = carol", {"author": "carol"}),
        ("author = me", {"author": "me"}),
        ("authored", {"author": "me"}),
        ("requested", {"review_requested_from": "me"}),
        ("authored AND requested", {"author": "me", "review_requested_from": "me"}),
        ("state = merged", {"state": "merged"}),
        ("state = Closed", {"state": "closed"}),
        ("draft AND (author = carol OR ci = failing)", {"draft": True}),
        # not pushed: under OR or NOT, other fields, other operators, anything snapshot-derived
        ("draft OR author = carol", {}),
        ("NOT (draft AND author = carol)", {}),
        ("NOT authored", {}),
        ("NOT author = carol", {}),
        ("author != carol", {}),
        ("author IN (carol)", {}),
        ("author LIKE 'car*'", {}),
        ("author = 'ME'", {}),
        ("author = 'bad[bot]'", {}),
        ("state != open", {}),
        ("state = all", {}),
        ("state IN (merged)", {}),
        ("age > 2d", {}),
        ("age < 52000w", {}),
        ("age < 0m", {}),
        ("title = x AND text LIKE y* AND pr = z AND number = 3", {}),
        ("ci = failing AND review = approved AND blocker = x AND approvals > 1", {}),
        ("comments > 0 AND unresolved > 0 AND pushed < 1d ORDER BY badge", {}),
        ("repo LIKE 'github.com/*'", {}),
        ("repo = unknown.example/a/b", {}),
        ("repo = 'not a repo'", {}),
    ],
)
def test_what_is_pushed_down(text: str, change: dict[str, Any]) -> None:
    got = pushed_args(text, state_given=False)
    assert got == replace(BASE, **change)


def test_a_declared_repo_is_pushed_and_names_the_host() -> None:
    got = pushed_args(f"repo = {LIB} AND draft")
    assert got.repo == bind_repo(parse_repo(LIB), CONFIG)  # type: ignore[arg-type]
    assert got.draft is True


def test_the_first_term_of_a_kind_is_the_one_pushed() -> None:
    assert pushed_args("author = carol AND author = dave").author == "carol"
    assert pushed_args("age < 5d AND age < 1d").updated_since == "86400s"


def test_an_explicit_flag_wins_over_the_query() -> None:
    given = replace(
        BASE,
        state="open",
        draft=False,
        author="erin",
        review_requested_from="carol",
        updated_since="1d",
        repo=bind_repo(parse_repo(LIB), CONFIG),  # type: ignore[arg-type]
    )
    text = (
        "state = merged AND draft AND author = carol AND authored AND requested AND age < 2h"
        f" AND repo = {REPO}"
    )
    assert pushed_args(text, given) == given
    assert pushed_args("state = merged", replace(BASE), state_given=True) == BASE


def test_a_host_alone_is_not_a_scope() -> None:
    host = Host()
    with pytest.raises(ForgectlError) as err:
        go("draft", replace(BASE, host="github.com"), push=True, host=host)
    assert err.value.code == "usage" and "without --repo" in err.value.message
    assert host.asked == []


def test_a_contradicting_query_is_the_intersection() -> None:
    host = Host(any_role=True)
    out = go("state = merged", BASE_HOST, push=True, state_given=True, host=host)
    assert host.asked[0].state == "open" and out["items"] == []


#: a role stands in for the scope a host alone cannot give (use `Host(any_role=True)` with it)
BASE_HOST = replace(BASE, author="me")


def test_the_state_comes_from_the_query_only_when_the_flag_was_not_given() -> None:
    both = [("state = merged", "merged"), ("draft", "open"), ("state = merged OR draft", "open")]
    for text, want in both:
        host = Host(any_role=True)
        go(text, BASE_HOST, push=True, state_given=False, host=host)
        assert host.asked[0].state == want, text


@pytest.mark.parametrize(
    "text, author, requested",
    [
        ("authored", "me", None),
        ("requested", None, "me"),
        ("authored AND requested", "me", "me"),
        ("author = carol AND NOT draft", "carol", None),
        ("author = me OR requested", None, None),
    ],
)
def test_the_query_supplies_the_role_the_command_needs(
    text: str, author: str | None, requested: str | None
) -> None:
    if author is None and requested is None:
        with pytest.raises(ForgectlError) as err:
            go(text, BASE, push=True)
        assert err.value.code == "usage"
        return
    host = Host()
    got = go(text, BASE, push=True, host=host)
    assert (host.asked[0].author, host.asked[0].review_requested_from) == (author, requested)
    base = replace(BASE, author=author, review_requested_from=requested)
    assert got == go(text, base, push=False)


def test_authored_alone_asks_only_the_authored_list() -> None:
    host = Host()
    out = go("authored", BASE, push=True, host=host)
    assert len(host.asked) == 1 and host.asked[0].review_requested_from is None
    assert numbers(out) == [1, 5]
    host = Host()
    out = go("requested", BASE, push=True, host=host)
    assert host.asked[0].author is None and sorted(numbers(out)) == [2, 5, 7]


def test_an_explicit_role_flag_is_never_contradicted() -> None:
    host = Host()
    out = go("authored", replace(BASE, author="carol"), push=True, host=host)
    assert host.asked[0].author == "carol"
    assert out["items"] == []  # carol's PRs are not authored by me (dave): the intersection


def test_snapshots_are_read_only_for_the_rows_the_list_fields_keep() -> None:
    host = Host(any_role=True)
    read = reader([{**snap(i["number"]), "pr": i["pr"]} for i in host.items])
    env = Env(CONFIG)
    out = prsquery.run(
        env,  # type: ignore[arg-type]
        replace(BASE_HOST, state="all"),
        prsquery.compile_query("draft OR state = merged AND ci = passing ORDER BY ci"),
        state_given=True,
        now=NOW,
        lister=host,
        read=read,
    )
    (asked,) = read.calls  # type: ignore[attr-defined]
    # not draft and not merged are false without a snapshot: #1, #5 and #7 are never read
    assert sorted(a.rsplit("#", 1)[1] for a in asked) == ["2", "3", "4", "6", "8"]
    assert sorted(numbers(out)) == [2, 3, 4, 6, 8]


def test_a_query_that_the_list_fields_decide_reads_nothing() -> None:
    read = reader([])
    env = Env(CONFIG)
    prsquery.run(
        env,  # type: ignore[arg-type]
        BASE_HOST,
        prsquery.compile_query("draft ORDER BY age"),
        state_given=True,
        now=NOW,
        lister=Host(any_role=True),
        read=read,
    )
    assert read.calls == []  # type: ignore[attr-defined]


def test_age_is_the_list_time_whether_or_not_a_snapshot_was_read() -> None:
    fresh = {**snap(1), "updated_at": "2026-10-02T23:59:00Z"}  # the item says an hour ago
    out = run("age > 30m AND ci = passing", listing(item(1, day=2)), reader([fresh]))
    assert numbers(out) == [1]


def test_order_by_age_is_the_list_time_whether_or_not_a_snapshot_was_read() -> None:
    a = {**item(1), "updated_at": "2026-10-02T10:00:00Z"}
    b = {**item(2), "updated_at": "2026-10-02T10:30:00Z"}
    fresh = [
        {**snap(1), "updated_at": "2026-10-02T11:00:00Z"},  # fresher than b's, but only here
        {**snap(2), "updated_at": "2026-10-02T10:30:00Z"},
    ]
    plain = run("ORDER BY age", listing(a, b), reader([]))
    read = run("ci = passing ORDER BY age", listing(a, b), reader(fresh))
    assert numbers(plain) == numbers(read) == [2, 1]
    pushed = {**snap(1), "head": {"pushed_at": "2026-10-02T09:00:00Z"}}  # the tie-break
    same = {**item(2), "updated_at": a["updated_at"]}
    tied = run("ci = passing ORDER BY age", listing(a, same), reader([pushed, snap(2)]))
    assert numbers(tied) == numbers(run("ORDER BY age", listing(a, same), reader([])))


@pytest.mark.parametrize(
    "text, read",
    [
        ("ci IS NOT NULL", [1, 2]),
        ("ci IS NULL", [1, 2]),
        ("NOT ci IS NULL AND NOT draft", [1, 2]),
        ("comments IS NOT NULL AND draft", []),
        ("blocker IS NULL OR draft", [1, 2]),
    ],
)
def test_an_is_null_on_an_unread_snapshot_never_drops_a_row_before_it_is_read(
    text: str, read: list[int]
) -> None:
    reads = reader([snap(1), snap(2)])
    out = run(text, listing(item(1), item(2)), reads)
    asked = [int(r.rsplit("#", 1)[1]) for call in reads.calls for r in call]  # type: ignore[attr-defined]
    assert asked == read
    if text == "ci IS NOT NULL":
        assert numbers(out) == [1, 2]
