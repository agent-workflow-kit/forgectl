"""`browse` over the real handlers, the real gateway and the fake gh/glab (#73).

Needs textual; the module is skipped without it.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

from textual.pilot import Pilot  # noqa: E402
from textual.widgets import OptionList, Static  # noqa: E402

from forgectl.errors import ForgectlError  # noqa: E402
from forgectl.tui.app import BrowseApp  # noqa: E402
from forgectl.tui.source import load_list  # noqa: E402
from forgectl.tui.source_live import LiveSource  # noqa: E402

from .bulk_support import bulk_answer  # noqa: E402
from .harness.fixtures import direct_children, forgectl_env  # noqa: E402
from .harness.forges import FakeForges  # noqa: E402
from .test_github_diff import PR_ENDPOINT, RECORDINGS, pull, recording  # noqa: E402
from .test_github_lists import recording as list_recording  # noqa: E402
from .test_lists_cli import GHE, TOKEN_A, TOKEN_B, rate  # noqa: E402
from .test_snapshot_cli import PR  # noqa: E402
from .test_snapshot_cli import script as script_snapshot
from .tui_fakes import FakeOpener  # noqa: E402

FAILED_CODE = "not_found"


@pytest.fixture(autouse=True)
def drained_children() -> Iterator[None]:
    """The app reads forges on daemon threads (so `q` never waits for a slow read), and leaving
    the test context can beat a read that a newer round replaced: its fake `gh` is still ending
    when the leaked-child check runs. Give those reads time to finish and be reaped (their own
    gateway reaps them) before that check, which runs after this teardown. This cannot tell a child
    the gateway killed from one that exits on its own within the 30 s: it only waits for both."""
    before = direct_children()
    yield
    deadline = time.monotonic() + 30
    while direct_children() - before and time.monotonic() < deadline:
        time.sleep(0.02)


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "retries: 0\nme:\n  github: user-1\nhosts:\n"
        "  github.com:\n    token_env: TOK_A\n"
        f"  {GHE}:\n    backend: github\n    token_env: TOK_B\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def forged(fake_forges: FakeForges, monkeypatch: pytest.MonkeyPatch) -> FakeForges:
    """The fakes first on PATH and the tokens set, in this process (no child forgectl)."""
    for name, value in forgectl_env(fake_forges, {"TOK_A": TOKEN_A, "TOK_B": TOKEN_B}).items():
        monkeypatch.setenv(name, value)
    return fake_forges


def listing_with_the_snapshot_pr() -> dict[str, Any]:
    """The recorded search, its newest row renumbered to the PR that the snapshot fakes serve."""
    doc = list_recording()
    doc["data"]["search"]["nodes"][0]["number"] = 7
    return doc


def two_hosts_one_failing(forges: FakeForges) -> None:
    # Only the list query: snapshot's own GraphQL queries must not match these answers.
    prs_query = r"query ForgectlPrs\b"
    forges.answer(
        "gh",
        "graphql",
        method="POST",
        host="github.com",
        stdin_re=prs_query,
        body=listing_with_the_snapshot_pr(),
        headers=rate(900),
    )
    forges.answer(
        "gh",
        "graphql",
        method="POST",
        host=GHE,
        stdin_re=prs_query,
        status=500,
        body={"message": "x"},
    )
    forges.answer("glab", "user", status=401, body={"message": "401 Unauthorized"})


def test_a_failing_host_gives_the_good_rows_and_an_error(forged: FakeForges, config: Path) -> None:
    two_hosts_one_failing(forged)
    got = load_list(LiveSource(config_path=str(config)))
    assert got.complete is False
    assert got.rows, "the good host's rows are still listed"
    assert any(GHE in e for e in got.errors)
    assert len(got.errors) == len(set(got.errors))


def test_the_source_never_starts_forgectl_and_calls_the_handlers_in_process(
    forged: FakeForges, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import forgectl.cli

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("the forgectl entry point was used")

    monkeypatch.setattr(forgectl.cli, "main", boom)
    started: list[Any] = []
    real = subprocess.Popen.__init__

    def spy(self: Any, args: Any, *a: Any, **k: Any) -> None:
        started.append(args)
        real(self, args, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", spy)
    two_hosts_one_failing(forged)
    load_list(LiveSource(config_path=str(config)))
    assert started, "the fake gh ran"
    for argv in started:
        assert Path(str(argv[0])).name in {"gh", "glab"}
        assert "forgectl" not in " ".join(map(str, argv))
        assert sys.executable not in argv


def test_a_host_outside_the_config_is_not_queried(forged: FakeForges, tmp_path: Path) -> None:
    only = tmp_path / "only.yaml"
    only.write_text(
        "retries: 0\nme:\n  github: user-1\nhosts:\n  github.com:\n    token_env: TOK_A\n",
        encoding="utf-8",
    )
    forged.answer("gh", "graphql", method="POST", host="github.com", body=list_recording())
    forged.answer("glab", "user", status=401, body={"message": "401 Unauthorized"})
    answer = LiveSource(config_path=str(only)).prs(author="me")
    assert GHE not in answer["hosts_queried"]
    assert all(GHE not in c.argv for c in forged.calls("gh"))


def test_the_filter_reaches_the_host_query(forged: FakeForges, tmp_path: Path) -> None:
    only = tmp_path / "only.yaml"
    only.write_text(
        "retries: 0\nme:\n  github: user-9\nhosts:\n  github.com:\n    token_env: TOK_A\n",
        encoding="utf-8",
    )
    forged.answer(
        "gh",
        "graphql",
        method="POST",
        stdin_re="user-review-requested:user-9",
        body=list_recording(),
    )
    forged.answer("glab", "user", status=401, body={"message": "401 Unauthorized"})
    answer = LiveSource(config_path=str(only)).prs(review_requested_from="me")
    assert answer["items"], "the scripted search answered"
    queries = [c.stdin or "" for c in forged.calls("gh") if "ForgectlPrs" in (c.stdin or "")]
    assert len(queries) == 1
    assert "user-review-requested:user-9" in queries[0]


def snapshot_and_diff_fakes(forges: FakeForges) -> None:
    script_snapshot(forges)
    # the first load of `browse` asks `snapshots` first (#116): it answers the first alias only,
    # so the rows after it are given to the per-row `snapshot` pool, as they were before
    forges.answer(
        "gh", "graphql", method="POST", stdin_re=r"query ForgectlBulk\b", body=bulk_answer(7)
    )
    doc = pull(changed_files=1)  # the `diff` read, as in test_diff_cli
    forges.answer("gh", PR_ENDPOINT, body=doc, times=1)
    forges.answer("gh", PR_ENDPOINT, body_text=(RECORDINGS / "pull.diff").read_text(), times=1)
    forges.answer("gh", endpoint_re=r"/compare/", body=recording("merge-base"))
    forges.answer("gh", PR_ENDPOINT, body=doc)


def test_snapshot_and_diff_of_one_pr_through_the_handlers(forged: FakeForges, config: Path) -> None:
    snapshot_and_diff_fakes(forged)
    source = LiveSource(config_path=str(config))
    snap = source.snapshot(PR)
    assert snap["pr"] == PR and snap["complete"] is True
    json.dumps(snap)


def test_a_failing_snapshot_is_a_forgectl_error_and_the_next_call_works(
    forged: FakeForges, config: Path
) -> None:
    forged.allow_unmatched = True  # the first call asks for what nothing answers
    source = LiveSource(config_path=str(config))
    with pytest.raises(ForgectlError) as caught:  # nothing scripted: the fake answers an error
        source.snapshot("github.com/user-7/missing#9")
    assert caught.value.code == FAILED_CODE
    snapshot_and_diff_fakes(forged)
    assert source.snapshot(PR)["pr"] == PR


def test_a_failing_diff_is_a_forgectl_error_and_the_snapshot_still_works(
    forged: FakeForges, config: Path
) -> None:
    forged.allow_unmatched = True  # only the snapshot is scripted: the diff reads get no answer
    script_snapshot(forged)
    source = LiveSource(config_path=str(config))
    with pytest.raises(ForgectlError) as caught:
        source.diff(PR)
    assert caught.value.code == FAILED_CODE
    assert source.snapshot(PR)["pr"] == PR


def test_browse_lists_both_hosts_and_enter_shows_the_snapshot(
    forged: FakeForges, config: Path
) -> None:
    two_hosts_one_failing(forged)
    snapshot_and_diff_fakes(forged)
    source = LiveSource(config_path=str(config))
    app = BrowseApp(source, FakeOpener(), focus=PR, detail_collapsed=())

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await _settle(app, pilot)
            assert app.query_one("#list", OptionList).option_count >= 1
            status = str(app.query_one("#status", Static).content)
            assert app.query_one("#detail").display is True, status
            assert "user-7/example-repo#7" in str(app.query_one("#facts", Static).content)
            assert "src/app.py" in str(app.query_one("#changes", Static).content)  # the diff

    asyncio.run(go())


class FailsFor(LiveSource):
    """The live source, except that the snapshot of one PR fails."""

    def __init__(self, bad: list[str], **kw: Any) -> None:
        super().__init__(**kw)
        self.bad = bad

    def snapshot(self, pr: str, **kw: Any) -> dict[str, Any]:
        if pr in self.bad:
            raise ForgectlError("not_found", "no such pull request")
        return super().snapshot(pr, **kw)


def test_enter_on_a_row_shows_the_snapshot_and_the_diff(forged: FakeForges, config: Path) -> None:
    # No `focus`: the detail comes from selecting a listed row, through the real handlers.
    # The newest row is the PR that the scripted snapshot is for.
    # GAP: forgectl has no GitLab `prs` list yet (only github_lists), so no GitLab row can be
    # listed here; the GitLab reads are covered by their own backend tests.
    two_hosts_one_failing(forged)
    snapshot_and_diff_fakes(forged)
    app = BrowseApp(
        LiveSource(config_path=str(config)), FakeOpener(), focus=None, detail_collapsed=()
    )

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await _settle(app, pilot)
            assert app.query_one("#list", OptionList).option_count >= 1
            assert app.query_one("#detail").display is False
            await pilot.press("enter")
            await _settle(app, pilot)
            status = str(app.query_one("#status", Static).content)
            assert app.query_one("#detail").display is True, status
            assert "user-7/example-repo#7" in str(app.query_one("#facts", Static).content)
            assert "src/app.py" in str(app.query_one("#changes", Static).content)

    asyncio.run(go())


def test_one_pr_failing_to_load_leaves_the_next_one_working(
    forged: FakeForges, config: Path
) -> None:
    two_hosts_one_failing(forged)
    snapshot_and_diff_fakes(forged)
    bad: list[str] = []
    app = BrowseApp(FailsFor(bad, config_path=str(config)), FakeOpener(), focus=None)

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await _settle(app, pilot)
            assert app.query_one("#list", OptionList).option_count >= 2
            bad.append(app._visible[1].pr)
            await pilot.press("down", "enter")
            await _settle(app, pilot)
            status = str(app.query_one("#status", Static).content)
            assert "not_found" in status
            assert app.query_one("#detail").display is False
            await pilot.press("up", "enter")
            await _settle(app, pilot)
            assert app.query_one("#detail").display is True
            assert "user-7/example-repo#7" in str(app.query_one("#facts", Static).content)

    asyncio.run(go())


async def _settle(app: BrowseApp, pilot: Pilot[None]) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_a_hidden_repo_gets_no_row_and_no_snapshot_in_browse(
    forged: FakeForges, tmp_path: Path
) -> None:
    hidden = tmp_path / "hidden.yaml"
    hidden.write_text(
        "retries: 0\nme:\n  github: user-1\nhidden_repos: ['GitHub.com/user-7/*']\nhosts:\n"
        "  github.com:\n    token_env: TOK_A\n"
        f"  {GHE}:\n    backend: github\n    token_env: TOK_B\n",
        encoding="utf-8",
    )
    two_hosts_one_failing(forged)
    snapshot_and_diff_fakes(forged)
    got = load_list(LiveSource(config_path=str(hidden)))
    assert got.rows == []
    assert got.complete is False  # the GHE failure only, not the hiding
    assert [e for e in got.errors if GHE not in e] == []
    assert all(c.argv[-1] != "snapshot" for c in forged.calls("gh"))
    queries = [c for c in forged.calls("gh") if "ForgectlPrs" in (c.stdin or "")]
    assert queries and len(queries) == len(forged.calls("gh")), "only list queries were made"


def test_an_exact_entry_hides_only_that_repos_rows_in_browse(
    forged: FakeForges, tmp_path: Path
) -> None:
    exact = tmp_path / "exact.yaml"
    exact.write_text(
        "retries: 0\nme:\n  github: user-1\nhidden_repos: ['github.com/user-7/example-repo']\n"
        "hosts:\n  github.com:\n    token_env: TOK_A\n",
        encoding="utf-8",
    )
    forged.answer(
        "gh", "graphql", method="POST", host="github.com", stdin_re=r"query ForgectlPrs\b",
        body=list_recording(),
    )  # fmt: skip
    forged.answer("glab", "user", status=401, body={"message": "401 Unauthorized"})
    got = load_list(LiveSource(config_path=str(exact)))
    assert [r.item["repo"] for r in got.rows] == ["github.com/user-7/other-repo"]
    assert got.complete is True


def test_a_hidden_gitlab_repo_gets_no_row_in_browse(
    forged: FakeForges, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .test_gitlab_lists import mrs as gitlab_mrs
    from .test_gitlab_lists_cli import config as gitlab_config
    from .test_gitlab_lists_cli import route as gitlab_route
    from .test_gitlab_lists_cli import script as gitlab_script

    gitlab_script(forged)
    recorded = gitlab_mrs()
    for mr in recorded:
        mr["author"] = recorded[0]["author"]
    shown = recorded[0]  # moved to another project: the one row browse keeps
    shown.update(
        project_id=303,
        source_project_id=303,
        target_project_id=303,
        web_url="https://gitlab.com/user-5/visible/-/merge_requests/31",
    )
    gitlab_route(forged, "merge_requests", recorded)
    forged.answer("gh", "graphql", method="POST", host="github.com", body=_empty_search(), times=2)
    cfg = gitlab_config(
        tmp_path, "me:\n  github: user-1\n  gitlab: user-1\nhidden_repos: ['gitlab.com/user-9/*']\n"
    )
    monkeypatch.setenv("GL_TOKEN", TOKEN_A)
    monkeypatch.setenv("GH_TOKEN", TOKEN_A)
    got = load_list(LiveSource(config_path=str(cfg)))
    assert [r.item["repo"] for r in got.rows] == ["gitlab.com/user-5/visible"]
    assert all("user-9/proj" not in e for e in got.errors)
    assert not any("projects/202" in " ".join(c.argv) for c in forged.calls("glab"))


def _empty_search() -> dict[str, Any]:
    doc = list_recording()
    doc["data"]["search"]["nodes"] = []
    doc["data"]["search"]["issueCount"] = 0
    return doc


def test_the_first_load_reads_a_row_through_the_bulk_query_not_one_by_one(
    forged: FakeForges, config: Path
) -> None:
    two_hosts_one_failing(forged)
    snapshot_and_diff_fakes(forged)
    app = BrowseApp(
        LiveSource(config_path=str(config)), FakeOpener(), focus=None, detail_collapsed=()
    )

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await _settle(app, pilot)
            for _ in range(300):  # the rows after PR 7 are still read one by one: let them end
                if not app._busy:
                    break
                await pilot.pause(0.1)
            await _settle(app, pilot)
            assert app._states[PR].status == "loaded"

    asyncio.run(go())
    requests = [json.loads(c.stdin) for c in forged.calls("gh") if c.stdin]
    assert any("ForgectlBulk" in r.get("query", "") for r in requests)
    shows = [r for r in requests if "ForgectlShow" in r.get("query", "")]
    assert all(r["variables"]["number"] != 7 for r in shows)  # PR 7 never needed its own read
