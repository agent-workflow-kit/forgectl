"""The live data source over the command runner: arguments, errors, threads, read-only.

The runner is faked at the handler boundary; ``test_browse_source_e2e.py`` drives the real
handlers through the fake gh/glab once the read stack is on this branch.
"""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from forgectl import registry
from forgectl.errors import ForgectlError
from forgectl.tui.source import DataSource, load_list
from forgectl.tui.source_live import Cancelled, HostStopped, LiveSource, in_process_runner

from .tui_fakes import make_diff, make_item, make_snapshot

PR1 = "github.com/acme/widgets#1"
PR2 = "gitlab.com/acme/widgets#2"


class Recorder:
    """A runner that records argv and answers from a function."""

    def __init__(self, answer: Any = None) -> None:
        self.calls: list[list[str]] = []
        self.answer = answer or (lambda argv: {})
        self._lock = threading.Lock()

    def __call__(self, argv: list[str]) -> dict[str, Any]:
        with self._lock:
            self.calls.append(list(argv))
        return self.answer(argv)


def listing(items: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "items": items,
        "complete": True,
        "errors": [],
        "hosts_queried": ["github.com"],
        "rate_limit": None,
        **extra,
    }


def test_it_is_a_data_source() -> None:
    source: DataSource = LiveSource(Recorder())
    assert source is not None


def test_prs_passes_the_filters_as_arguments() -> None:
    run = Recorder(lambda argv: listing([]))
    LiveSource(run).prs(author="me", review_requested_from=None, state="all", limit=7)
    assert run.calls == [["prs", "--author=me", "--state=all", "--limit=7"]]


def test_prs_review_requested_filter() -> None:
    run = Recorder(lambda argv: listing([]))
    LiveSource(run).prs(review_requested_from="me")
    assert run.calls == [["prs", "--review-requested-from=me", "--state=open", "--limit=100"]]


def test_prs_returns_the_handler_answer() -> None:
    item = make_item(1)
    answer = LiveSource(Recorder(lambda argv: listing([item]))).prs(author="me")
    assert answer["items"] == [item]


def test_partial_results_become_an_incomplete_listing() -> None:
    err_entry = {"host": "gitlab.com", "code": "auth_required", "message": "no token"}
    output = listing([make_item(1)], complete=False, errors=[err_entry])

    def answer(argv: list[str]) -> dict[str, Any]:
        raise ForgectlError("partial_results", "1 of 2 hosts failed", output=output)

    got = LiveSource(Recorder(answer)).prs(author="me")
    assert got["complete"] is False
    assert got["errors"] == [err_entry]
    assert [i["pr"] for i in got["items"]] == [make_item(1)["pr"]]


def test_partial_results_are_never_complete_even_if_the_output_says_so() -> None:
    def answer(argv: list[str]) -> dict[str, Any]:
        raise ForgectlError("partial_results", "x", output=listing([], complete=True))

    assert LiveSource(Recorder(answer)).prs(author="me")["complete"] is False


def test_partial_results_without_an_output_stay_an_error() -> None:
    def answer(argv: list[str]) -> dict[str, Any]:
        raise ForgectlError("partial_results", "x")

    with pytest.raises(ForgectlError) as caught:
        LiveSource(Recorder(answer)).prs(author="me")
    assert caught.value.code == "partial_results"


def test_other_errors_reach_the_caller_as_forgectl_errors() -> None:
    def answer(argv: list[str]) -> dict[str, Any]:
        raise ForgectlError("auth_required", "no token")

    with pytest.raises(ForgectlError) as caught:
        LiveSource(Recorder(answer)).snapshot(PR1)
    assert caught.value.code == "auth_required"


def test_a_bug_in_a_handler_becomes_an_internal_error_without_its_text() -> None:
    def answer(argv: list[str]) -> dict[str, Any]:
        raise RuntimeError("token=hunter2")

    for call in (
        lambda s: s.prs(author="me"),
        lambda s: s.snapshot(PR1),
        lambda s: s.diff(PR1),
    ):
        with pytest.raises(ForgectlError) as caught:
            call(LiveSource(Recorder(answer)))
        assert caught.value.code == "internal"
        assert "hunter2" not in caught.value.message
        assert "RuntimeError" in caught.value.message
        assert isinstance(caught.value.__cause__, RuntimeError)  # kept for debugging, not shown


def test_keyboard_interrupt_is_not_swallowed() -> None:
    def answer(argv: list[str]) -> dict[str, Any]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        LiveSource(Recorder(answer)).snapshot(PR1)


def test_snapshot_and_diff_call_the_matching_commands() -> None:
    run = Recorder(lambda argv: make_snapshot(1) if argv[0] == "snapshot" else make_diff(PR1))
    source = LiveSource(run)
    assert source.snapshot(PR1)["number"] == 1
    assert source.diff(PR1)["pr"] == PR1
    assert run.calls == [["snapshot", PR1], ["diff", PR1]]


@pytest.mark.parametrize(
    "bad",
    ["", "--json", "-x", "github.com/acme/widgets", "github.com/a/b#0", "a b#1", "x\n#1", "../x#1"],
)
def test_a_malformed_pr_is_refused_before_the_runner(bad: str) -> None:
    run = Recorder()
    source = LiveSource(run)
    for call in (source.snapshot, source.diff):
        with pytest.raises(ForgectlError) as caught:
            call(bad)
        assert caught.value.code == "invalid_pr"
    assert run.calls == []


def test_only_the_three_read_commands_are_ever_run() -> None:
    run = Recorder(lambda argv: listing([]) if argv[0] == "prs" else {"pr": PR1})
    source = LiveSource(run)
    source.prs(author="me")
    source.snapshot(PR1)
    source.diff(PR1)
    assert {c[0] for c in run.calls} == {"prs", "snapshot", "diff"}
    assert not hasattr(source, "merge") and not hasattr(source, "comment")


def test_a_failing_row_leaves_the_others_alone() -> None:
    def answer(argv: list[str]) -> dict[str, Any]:
        if argv[1] == PR2:
            raise ForgectlError("not_found", "gone")
        return make_snapshot(1)

    source = LiveSource(Recorder(answer))
    with pytest.raises(ForgectlError):
        source.snapshot(PR2)
    assert source.snapshot(PR1)["number"] == 1


def test_concurrent_snapshots_each_get_their_own_answer() -> None:
    prs = [f"github.com/acme/widgets#{n}" for n in range(1, 41)]
    barrier = threading.Barrier(8, timeout=10)

    def answer(argv: list[str]) -> dict[str, Any]:
        if argv[1] in prs[:8]:
            barrier.wait()  # the first eight are in flight together
        return make_snapshot(int(argv[1].rsplit("#", 1)[1]))

    source = LiveSource(Recorder(answer))
    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(source.snapshot, prs))
    assert [s["number"] for s in got] == list(range(1, 41))


def test_a_cancelled_call_is_skipped_before_it_starts() -> None:
    run = Recorder()
    source = LiveSource(run)
    with pytest.raises(Cancelled):
        source.snapshot(PR1, cancelled=lambda: True)
    with pytest.raises(Cancelled):
        source.diff(PR1, cancelled=lambda: True)
    assert run.calls == []


def test_not_cancelled_runs() -> None:
    run = Recorder(lambda argv: make_snapshot(1))
    LiveSource(run).snapshot(PR1, cancelled=lambda: False)
    assert len(run.calls) == 1


def test_load_list_names_the_failed_host_once() -> None:
    err_entry = {"host": "gitlab.com", "code": "auth_required", "message": "no token"}

    def answer(argv: list[str]) -> dict[str, Any]:
        raise ForgectlError(
            "partial_results",
            "x",
            output=listing([make_item(1)], complete=False, errors=[err_entry]),
        )

    got = load_list(LiveSource(Recorder(answer)))
    assert got.complete is False
    assert got.errors == ["gitlab.com: auth_required: no token"]
    assert [r.pr for r in got.rows] == [make_item(1)["pr"]]


def test_run_browse_defaults_to_the_live_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from forgectl.tui import run as run_mod

    seen: dict[str, Any] = {}

    class StubApp:
        return_code = 0

        def __init__(self, source: Any, opener: Any, **kwargs: Any) -> None:
            seen["source"] = source

        def run(self) -> None:
            return None

        def result(self) -> dict[str, Any]:
            return {"intent": "back", "selected": [], "merged": []}

    monkeypatch.setattr(run_mod, "BrowseApp", StubApp)
    config = tmp_path / "config.yaml"
    config.write_text("", encoding="utf-8")
    run_mod.run_browse(focus=None, config_path=str(config))
    assert isinstance(seen["source"], LiveSource)


def test_the_config_path_reaches_the_in_process_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    from forgectl.tui import source_live

    paths: list[str | None] = []

    def fake_runner(
        config_path: str | None = None, environ: Any = None, timeout: float | None = None
    ) -> Any:
        paths.append(config_path)
        return Recorder(lambda argv: listing([]))

    monkeypatch.setattr(source_live, "in_process_runner", fake_runner)
    LiveSource(config_path="/x.yaml").prs(author="me")
    assert paths == ["/x.yaml"]


CONFIG_TEXT = "retries: 0\nme:\n  github: user-1\nhosts:\n  github.com:\n    token_env: TOK_A\n"


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_TEXT, encoding="utf-8")
    return path


@pytest.fixture
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handlers not loaded yet, as at the start of a process; the real state comes back after."""
    registry.load_handlers()  # so every handler module is in sys.modules and is put back after
    monkeypatch.setattr(registry, "_loaded", False)
    monkeypatch.setattr(registry, "HANDLERS", {})
    for name in registry.HANDLER_MODULES:  # import runs the decorators again
        monkeypatch.delitem(sys.modules, name, raising=False)


def test_an_option_looking_filter_value_is_one_literal_argument() -> None:
    run = Recorder(lambda argv: listing([]))
    LiveSource(run).prs(author="-x", review_requested_from="-y")
    assert run.calls[0][1:3] == ["--author=-x", "--review-requested-from=-y"]


def test_an_option_looking_author_reaches_the_parser_as_a_value(tmp_path: Path) -> None:
    source = LiveSource(config_path=str(write_config(tmp_path)))
    with pytest.raises(ForgectlError) as caught:
        source.prs(author="-x")
    assert caught.value.code == "usage"
    assert "not a valid login" in caught.value.message  # not "expected one argument"


def test_the_first_calls_from_many_threads_all_find_the_handlers(
    fresh_registry: None, tmp_path: Path
) -> None:
    path = str(write_config(tmp_path))
    barrier = threading.Barrier(8, timeout=10)

    def go(_: int) -> str:
        run = in_process_runner(path, {})  # one runner per source: the race is process-wide
        barrier.wait()
        try:
            run(["snapshot", PR1])
        except ForgectlError as err:
            return f"{err.code}: {err.message}"
        return "ok"

    with ThreadPoolExecutor(max_workers=8) as pool:
        seen = list(pool.map(go, range(8)))
    assert not [s for s in seen if "not implemented" in s], seen


def test_a_failed_config_load_is_not_cached(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("hosts: [", encoding="utf-8")
    run = in_process_runner(str(path), {})
    with pytest.raises(ForgectlError):
        run(["snapshot", PR1])
    path.write_text(CONFIG_TEXT, encoding="utf-8")
    registry.load_handlers()
    seen: list[str] = []

    def handler(inv: Any) -> dict[str, Any]:
        seen.append(inv.command)
        return {"ok": True}

    original = registry.HANDLERS["snapshot"]
    registry.HANDLERS["snapshot"] = handler
    try:
        assert run(["snapshot", PR1]) == {"ok": True}
    finally:
        registry.HANDLERS["snapshot"] = original
    assert seen == ["snapshot"]


def test_a_failing_call_is_logged_like_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    run = in_process_runner(str(tmp_path / "missing.yaml"), {})
    with pytest.raises(ForgectlError) as caught:
        run(["snapshot", PR1])
    lines = (state / "forgectl" / "log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == caught.value.code
    assert record["details"]["command"] == "snapshot"


def test_the_timeout_reaches_the_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry.load_handlers()
    seen: list[float] = []

    def handler(inv: Any) -> dict[str, Any]:
        seen.append(inv.timeout)
        return {}

    monkeypatch.setitem(registry.HANDLERS, "snapshot", handler)
    LiveSource(config_path=str(write_config(tmp_path)), timeout=7.5).snapshot(PR1)
    assert seen == [7.5]


def test_without_a_timeout_the_config_value_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.load_handlers()
    seen: list[float] = []
    monkeypatch.setitem(registry.HANDLERS, "snapshot", lambda inv: seen.append(inv.timeout) or {})
    LiveSource(config_path=str(write_config(tmp_path))).snapshot(PR1)
    assert seen and seen[0] > 0


@pytest.mark.parametrize("argv", [["whoami"], ["merge", PR1, "--yes"], ["merge", PR1]])
def test_the_runner_refuses_a_command_outside_the_three_reads(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.load_handlers()
    called: list[str] = []
    for name in ("whoami", "merge"):
        monkeypatch.setitem(registry.HANDLERS, name, lambda inv: called.append(inv.command) or {})
    run = in_process_runner(str(write_config(tmp_path)), {})
    with pytest.raises(ForgectlError) as caught:
        run(argv)
    assert caught.value.code in {"internal", "usage"}
    assert called == []
    if caught.value.code == "usage":
        pytest.fail("the command was refused by the parser, not by the runner: " + str(argv))


def test_a_cancelled_check_that_raises_is_an_internal_error() -> None:
    def broken() -> bool:
        raise ValueError("token=hunter2")

    run = Recorder()
    with pytest.raises(ForgectlError) as caught:
        LiveSource(run).snapshot(PR1, cancelled=broken)
    assert caught.value.code == "internal"
    assert caught.value.message == "cancelled: ValueError"
    assert run.calls == []


# -- snapshots (#116) ------------------------------------------------------------------------


def test_snapshots_runs_the_bulk_only_command_for_the_given_prs() -> None:
    run = Recorder(lambda argv: {"items": [], "failed": [], "complete": True, "errors": []})
    LiveSource(run).snapshots([PR1, "github.com/acme/other#3"])
    assert run.calls == [["snapshots", "--bulk-only", PR1, "github.com/acme/other#3"]]


def test_snapshots_of_nothing_makes_no_call() -> None:
    run = Recorder()
    assert LiveSource(run).snapshots([])["items"] == []
    assert run.calls == []


@pytest.mark.parametrize("bad", ["--help", "-", "not a pr", "github.com/a/b#0", "github.com/a/b"])
def test_snapshots_refuses_anything_that_is_not_a_pr_before_any_call(bad: str) -> None:
    run = Recorder()
    with pytest.raises(ForgectlError) as caught:
        LiveSource(run).snapshots([PR1, bad])
    assert caught.value.code == "invalid_pr" and run.calls == []


# -- detail (#119) ---------------------------------------------------------------------------------


def test_detail_is_one_lookup_read_of_the_pr_and_gives_its_item() -> None:
    item = make_snapshot(1)
    run = Recorder(lambda argv: {"items": [item], "failed": [], "complete": True, "errors": []})
    assert LiveSource(run).detail(PR1) == item
    assert run.calls == [["snapshots", "--bulk-only", "--lookup", PR1]]


def test_detail_of_a_pr_the_bulk_reader_gave_up_raises_and_names_the_reason() -> None:
    failed = [{"pr": PR1, "error": {"code": "bulk_unavailable", "message": "x\x1b[31m"}}]
    run = Recorder(lambda argv: {"items": [], "failed": failed, "complete": False, "errors": []})
    with pytest.raises(ForgectlError) as caught:
        LiveSource(run).detail(PR1)
    assert caught.value.code == "upstream_error"
    assert "bulk_unavailable" in caught.value.message and "\x1b" not in caught.value.message


def test_detail_of_a_pr_cut_off_by_a_host_stop_raises_the_stop_for_the_caller() -> None:
    failed = [{"pr": PR1, "error": {"code": "rate_limited", "message": "x"}}]
    run = Recorder(lambda argv: {"items": [], "failed": failed, "complete": False, "errors": []})
    with pytest.raises(HostStopped) as caught:
        LiveSource(run).detail(PR1)
    assert caught.value.code == "rate_limited"


def test_detail_of_an_item_that_is_not_whole_raises() -> None:
    item = make_snapshot(1, extra={"complete": False, "incomplete": ["threads"]})
    run = Recorder(lambda argv: {"items": [item], "failed": [], "complete": False, "errors": []})
    with pytest.raises(ForgectlError):
        LiveSource(run).detail(PR1)


def test_detail_ignores_an_item_for_another_pr() -> None:
    other = make_snapshot(5)
    run = Recorder(lambda argv: {"items": [other], "failed": [], "complete": True, "errors": []})
    with pytest.raises(ForgectlError):
        LiveSource(run).detail(PR1)


@pytest.mark.parametrize("bad", ["--help", "-", "not a pr"])
def test_detail_refuses_anything_that_is_not_a_pr_before_any_call(bad: str) -> None:
    run = Recorder()
    with pytest.raises(ForgectlError) as caught:
        LiveSource(run).detail(bad)
    assert caught.value.code == "invalid_pr" and run.calls == []


def test_a_cancelled_detail_raises_cancelled_without_a_call() -> None:
    run = Recorder()
    with pytest.raises(Cancelled):
        LiveSource(run).detail(PR1, cancelled=lambda: True)
    assert run.calls == []


def test_a_cancelled_batch_raises_cancelled_without_a_call() -> None:
    run = Recorder()
    with pytest.raises(Cancelled):
        LiveSource(run).snapshots([PR1], cancelled=lambda: True)
    assert run.calls == []


def test_snapshots_errors_are_forgectl_errors() -> None:
    def boom(argv: list[str]) -> dict[str, Any]:
        raise RuntimeError("handler bug")

    with pytest.raises(ForgectlError) as caught:
        LiveSource(boom).snapshots([PR1])
    assert caught.value.code == "internal"
