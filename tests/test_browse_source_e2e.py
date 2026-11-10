"""`browse` over the real handlers, the real gateway and the fake gh/glab (#73).

Needs textual; the module is skipped without it.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
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

from .harness.fixtures import forgectl_env  # noqa: E402
from .harness.forges import FakeForges  # noqa: E402
from .test_github_diff import PR_ENDPOINT, RECORDINGS, pull, recording  # noqa: E402
from .test_github_lists import recording as list_recording  # noqa: E402
from .test_lists_cli import GHE, TOKEN_A, TOKEN_B, rate  # noqa: E402
from .test_snapshot_cli import PR  # noqa: E402
from .test_snapshot_cli import script as script_snapshot
from .tui_fakes import FakeOpener  # noqa: E402


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


def two_hosts_one_failing(forges: FakeForges) -> None:
    # Only the list query: snapshot's own GraphQL queries must not match these answers.
    prs_query = r"query ForgectlPrs\b"
    forges.answer(
        "gh",
        "graphql",
        method="POST",
        host="github.com",
        stdin_re=prs_query,
        body=list_recording(),
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
    LiveSource(config_path=str(only)).prs(review_requested_from="me")


def snapshot_and_diff_fakes(forges: FakeForges) -> None:
    script_snapshot(forges)
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
    with pytest.raises(ForgectlError):  # nothing scripted: the fake answers an error
        source.snapshot("github.com/user-7/missing#9")
    snapshot_and_diff_fakes(forged)
    assert source.snapshot(PR)["pr"] == PR


def test_browse_lists_both_hosts_and_enter_shows_the_snapshot(
    forged: FakeForges, config: Path
) -> None:
    two_hosts_one_failing(forged)
    snapshot_and_diff_fakes(forged)
    source = LiveSource(config_path=str(config))
    app = BrowseApp(source, FakeOpener(), focus=PR)

    async def go() -> None:
        async with app.run_test(size=(140, 50)) as pilot:
            await _settle(app, pilot)
            assert app.query_one("#list", OptionList).option_count >= 1
            status = str(app.query_one("#status", Static).content)
            assert app.query_one("#detail").display is True, status
            assert "user-7/example-repo#7" in str(app.query_one("#facts", Static).content)
            assert "src/app.py" in str(app.query_one("#rest", Static).content)  # the diff

    asyncio.run(go())


async def _settle(app: BrowseApp, pilot: Pilot[None]) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()
