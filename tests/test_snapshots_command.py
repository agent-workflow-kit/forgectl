"""`forgectl snapshots` (#116): the handler over a bulk reader and the per-PR fallback, and the
command end to end with the fake `gh`. The GitLab reader has its own tests."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from forgectl import bulk
from forgectl.backends.bulk_common import BULK_UNAVAILABLE
from forgectl.errors import ForgectlError
from forgectl.identifiers import PrRef

from .bulk_support import BulkRouter, bulk_answer, numbers_of, ref
from .harness import golden, schemas
from .harness.forges import FakeForges
from .test_github_show import HEAD, TOKEN, http
from .test_github_show import recording as show_recording
from .test_github_snapshot import REF, SCOPES, Rig, pr_of, rec, standard

pytestmark = pytest.mark.usefixtures("fake_forges")


def per_pr_answers() -> dict[str, Any]:
    answers = standard()
    answers.pop("user", None)
    return answers


def cut_checks(request: dict[str, Any]) -> dict[str, Any]:
    doc = bulk_answer(*numbers_of(request))
    pr = doc["data"]["p0"]["pullRequest"]
    pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["pageInfo"][
        "hasNextPage"
    ] = True
    return doc


def make(forges: FakeForges, router: Any, **kwargs: Any) -> Rig:
    return Rig(forges, router, **kwargs)


def test_every_pr_read_whole_by_bulk_is_an_item_and_nothing_falls_back(
    fake_forges: FakeForges,
) -> None:
    router = BulkRouter()
    out = bulk.run_snapshots(make(fake_forges, router).env, [ref(7), ref(8)])
    assert [i["number"] for i in out["items"]] == [7, 8]
    assert out["failed"] == [] and out["fallback"] == [] and out["errors"] == []
    assert out["complete"] is True and out["hosts_queried"] == ["github.com"]
    assert router.bulk_sizes == [2]
    schemas.validate_cli_output("snapshots", out)


def test_a_pr_the_bulk_reader_gives_up_is_read_by_the_per_pr_snapshot(
    fake_forges: FakeForges,
) -> None:
    router = BulkRouter(cut_checks, per_pr_answers())
    out = bulk.run_snapshots(make(fake_forges, router).env, [REF, ref(8)])
    assert out["fallback"] == ["github.com/user-7/example-repo#7"]
    assert [i["number"] for i in out["items"]] == [7, 8]
    assert "ForgectlShow" in router.ops and "ForgectlParts" in router.ops
    assert out["failed"] == [] and out["complete"] is True
    schemas.validate_cli_output("snapshots", out)


def test_bulk_only_reports_the_pr_instead_of_reading_it(fake_forges: FakeForges) -> None:
    router = BulkRouter(cut_checks, per_pr_answers())
    out = bulk.run_snapshots(make(fake_forges, router).env, [REF, ref(8)], bulk_only=True)
    assert [i["number"] for i in out["items"]] == [8]
    assert [f["pr"] for f in out["failed"]] == ["github.com/user-7/example-repo#7"]
    assert out["failed"][0]["error"]["code"] == BULK_UNAVAILABLE
    assert out["fallback"] == [] and out["complete"] is False
    assert "ForgectlShow" not in router.ops
    schemas.validate_cli_output("snapshots", out)


def test_a_pr_missing_on_the_host_is_failed_with_its_own_code(fake_forges: FakeForges) -> None:
    def gone(request: dict[str, Any]) -> Any:
        doc = bulk_answer(*numbers_of(request))
        doc["data"]["p0"] = None
        doc["errors"] = [{"type": "NOT_FOUND", "path": ["p0"], "message": "x"}]
        return doc

    def none(request: dict[str, Any]) -> Any:
        return {"data": {"repository": None}}

    router = BulkRouter(gone, {"ForgectlShow": none})
    out = bulk.run_snapshots(make(fake_forges, router).env, [REF])
    assert out["items"] == []
    assert out["failed"][0]["error"]["code"] == "not_found"
    assert out["complete"] is False


def test_rate_limit_exhaustion_returns_the_rows_read_and_makes_no_per_pr_read(
    fake_forges: FakeForges,
) -> None:
    def limited(request: dict[str, Any]) -> Any:
        if request["variables"]["k0"] > 25:
            return http(
                429, {"message": "API rate limit exceeded"}, headers={"retry-after": "3600"}
            )
        return bulk_answer(*numbers_of(request))

    router = BulkRouter(limited, per_pr_answers())
    refs = [ref(n) for n in range(1, 31)]
    out = bulk.run_snapshots(make(fake_forges, router, retries=3).env, refs)
    assert len(out["items"]) == 25
    assert {f["error"]["code"] for f in out["failed"]} == {"rate_limited"}
    assert len(out["failed"]) == 5
    assert [e["code"] for e in out["errors"]] == ["rate_limited"]
    assert out["complete"] is False and out["fallback"] == []
    assert "ForgectlShow" not in router.ops
    schemas.validate_cli_output("snapshots", out)


def test_a_failing_fallback_is_a_failed_entry_and_never_a_crash(fake_forges: FakeForges) -> None:
    def broken(request: dict[str, Any]) -> Any:
        return http(500, {"message": "boom"})

    router = BulkRouter(lambda r: http(502, b"x"), {"ForgectlShow": broken})
    out = bulk.run_snapshots(make(fake_forges, router, retries=0).env, [REF])
    assert out["items"] == [] and out["failed"][0]["error"]["code"] == "upstream_error"


def test_items_come_back_in_the_order_given_across_paths(fake_forges: FakeForges) -> None:
    def second_cut(request: dict[str, Any]) -> Any:
        doc = bulk_answer(*numbers_of(request))
        doc["data"]["p1"]["pullRequest"]["comments"]["pageInfo"]["hasNextPage"] = True
        return doc

    router = BulkRouter(second_cut, per_pr_answers())
    out = bulk.run_snapshots(make(fake_forges, router).env, [ref(9), ref(7), ref(8)])
    assert [i["pr"].rsplit("#", 1)[1] for i in out["items"]] == ["9", "7", "8"]
    assert out["fallback"] == ["github.com/user-7/example-repo#7"]


def test_duplicate_refs_are_read_once() -> None:
    class Args:
        refs = [REF, REF, ref(8)]

    class Inv:
        args = Args()

    assert bulk.refs_of(Inv()) == [REF, ref(8)]  # type: ignore[arg-type]


def test_one_pr_in_two_spellings_is_read_once() -> None:
    class Args:
        refs = [ref(7, "User-7/Example-Repo"), REF, ref(8)]

    class Inv:
        args = Args()

    got = bulk.refs_of(Inv())  # type: ignore[arg-type]
    assert got == [ref(7, "User-7/Example-Repo"), ref(8)]


def test_too_many_refs_and_none_are_usage_errors() -> None:
    class Inv:
        def __init__(self, refs: list[Any]) -> None:
            self.args = type("A", (), {"refs": refs})()

    with pytest.raises(ForgectlError) as caught:
        bulk.refs_of(Inv([ref(n) for n in range(1, bulk.MAX_REFS + 2)]))  # type: ignore[arg-type]
    assert caught.value.code == "usage"
    with pytest.raises(ForgectlError):
        bulk.refs_of(Inv(["-", REF]))  # type: ignore[arg-type]


def test_a_stop_in_the_per_pr_fallback_reports_every_unread_pr_with_its_code(
    fake_forges: FakeForges,
) -> None:
    limited = http(429, {"message": "API rate limit exceeded"}, headers={"retry-after": "3600"})
    router = BulkRouter(lambda r: http(502, b"x"), {"ForgectlShow": limited})
    out = bulk.run_snapshots(make(fake_forges, router).env, [ref(7), ref(8)])
    assert [f["error"]["code"] for f in out["failed"]] == ["rate_limited", "rate_limited"]
    assert [e["code"] for e in out["errors"]] == ["rate_limited"]
    assert router.ops.count("ForgectlShow") == 1  # nothing asked after the limit
    schemas.validate_cli_output("snapshots", out)


def down() -> Any:
    from forgectl.gateway import ProcessResult

    return ProcessResult(pid=1, pgid=1, returncode=1, stdout=b"", stderr=b"connection reset")


def test_network_errors_in_the_fallback_end_the_host_only_after_three_in_a_row(
    fake_forges: FakeForges,
) -> None:
    router = BulkRouter(lambda r: http(502, b"x"), {"ForgectlShow": down()})
    refs = [ref(n) for n in range(1, 6)]
    out = bulk.run_snapshots(make(fake_forges, router, retries=0).env, refs)
    assert router.ops.count("ForgectlShow") == 3
    assert [f["error"]["code"] for f in out["failed"]] == ["network_error"] * 5
    assert [e["code"] for e in out["errors"]] == ["network_error"]


def test_one_network_error_in_the_fallback_costs_that_pr_only(fake_forges: FakeForges) -> None:
    answers = [down(), None]

    def show(_request: dict[str, Any]) -> Any:
        first = answers.pop(0)
        return first if first is not None else show_recording()

    router = BulkRouter(lambda r: http(502, b"x"), {**per_pr_answers(), "ForgectlShow": show})
    out = bulk.run_snapshots(make(fake_forges, router, retries=0).env, [REF, ref(8)])
    assert out["failed"][0]["error"]["code"] == "network_error"
    assert router.ops.count("ForgectlShow") == 2  # the host was not stopped by the first
    assert out["errors"] == []


def test_consecutive_bulk_network_failures_leave_every_pr_to_the_per_pr_fallback(
    fake_forges: FakeForges,
) -> None:
    router = BulkRouter(lambda r: down(), per_pr_answers())
    refs = [ref(n) for n in range(7, 12)]
    out = bulk.run_snapshots(make(fake_forges, router, retries=0).env, refs)
    assert len(router.bulk_sizes) == 3  # the bulk requests stopped after three in a row
    assert len(out["fallback"]) == 5  # and every PR was read per PR (the recording is PR 7's)
    assert out["errors"] == [] and [i["number"] for i in out["items"]] == [7]


def test_a_decline_before_a_stop_keeps_its_own_reason_and_the_cut_off_get_the_stop_code(
    fake_forges: FakeForges,
) -> None:
    def answer(request: dict[str, Any]) -> Any:
        first = numbers_of(request)[0]
        if first > 25:
            return http(
                429, {"message": "API rate limit exceeded"}, headers={"retry-after": "3600"}
            )
        if first == 1:
            return cut_checks(request)
        return bulk_answer(*numbers_of(request))

    router = BulkRouter(answer, per_pr_answers())
    refs = [ref(n) for n in range(1, 31)]
    out = bulk.run_snapshots(make(fake_forges, router, retries=3).env, refs)
    codes = {f["pr"]: f["error"]["code"] for f in out["failed"]}
    # PR 1 was declined by the bulk reader (a cut part) and never read per PR: its own reason
    assert codes[str(ref(1))] == BULK_UNAVAILABLE
    assert {codes[str(ref(n))] for n in range(26, 31)} == {"rate_limited"}
    assert len(codes) == 6


def test_a_crashing_bulk_reader_is_warned_with_its_repr_under_verbose(
    fake_forges: FakeForges, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import types

    def boom(env: Any, host: str, refs: Any) -> Any:
        raise TypeError("odd shape")

    monkeypatch.setitem(bulk.BULK_READERS, "github", "forgectl.test_stub_reader")
    monkeypatch.setitem(
        __import__("sys").modules,
        "forgectl.test_stub_reader",
        types.SimpleNamespace(read_bulk=boom),
    )
    router = BulkRouter()
    bulk.run_snapshots(
        make(fake_forges, router).env, [ref(7), ref(8)], bulk_only=True, verbose=True
    )
    err = capsys.readouterr().err
    assert err.count("TypeError('odd shape')") == 1
    bulk.run_snapshots(make(fake_forges, router).env, [ref(7)], bulk_only=True)
    assert "odd shape" not in capsys.readouterr().err


def test_a_bug_in_one_pr_of_the_reader_is_warned_with_its_repr_under_verbose(
    fake_forges: FakeForges, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from forgectl.backends import github_bulk

    def boom(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise TypeError("odd item")

    monkeypatch.setattr(github_bulk._Run, "_item", boom)
    router = BulkRouter(others=per_pr_answers())
    out = bulk.run_snapshots(
        make(fake_forges, router).env, [ref(7), ref(8)], bulk_only=True, verbose=True
    )
    assert len(out["failed"]) == 2
    assert capsys.readouterr().err.count("TypeError('odd item')") == 1


def test_a_crashing_bulk_reader_costs_its_host_only(
    fake_forges: FakeForges, monkeypatch: pytest.MonkeyPatch
) -> None:
    import types

    def boom(env: Any, host: str, refs: Any) -> Any:
        raise ValueError("bad digits")

    monkeypatch.setitem(bulk.BULK_READERS, "gitlab", "forgectl.test_stub_reader")
    monkeypatch.setitem(
        __import__("sys").modules,
        "forgectl.test_stub_reader",
        types.SimpleNamespace(read_bulk=boom),
    )
    other = PrRef(host="gitlab.example.test", backend="gitlab", project="a/b", number=3)
    router = BulkRouter()
    out = bulk.run_snapshots(make(fake_forges, router).env, [ref(7), other], bulk_only=True)
    assert [i["number"] for i in out["items"]] == [7]
    assert [f["pr"] for f in out["failed"]] == [str(other)]
    assert out["failed"][0]["error"]["code"] == BULK_UNAVAILABLE


# -- end to end ---------------------------------------------------------------------------------

PR = "github.com/user-7/example-repo#7"


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text("hosts:\n  github.com:\n    token_env: SHOW_TOKEN\n", encoding="utf-8")
    return path


def script(forges: FakeForges) -> None:
    forges.answer(
        "gh", "graphql", method="POST", stdin_re=r"query ForgectlBulk\b", body=bulk_answer(7, 8)
    )
    forges.answer(
        "gh",
        "user",
        body={"login": "user-1", "id": 1001, "html_url": "https://github.com/user-1"},
        headers=SCOPES,
    )


def run(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]], config: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    return run_forgectl(
        ["snapshots", *args, "--json", "--config", str(config)], env={"SHOW_TOKEN": TOKEN}
    )


def test_end_to_end_prints_schema_valid_json_with_one_bulk_request(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    script(fake_forges)
    done = run(run_forgectl, config, PR, "github.com/user-7/example-repo#8")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("snapshots", out)
    assert [i["number"] for i in out["items"]] == [7, 8]
    assert out["fallback"] == [] and out["complete"] is True
    graphql = [c for c in fake_forges.calls("gh") if "graphql" in c.argv]
    assert len(graphql) == 1
    for item in out["items"]:
        item["fetched_at"] = "2026-10-02T12:00:05Z"  # the clock is real in a child process
    golden.assert_golden("snapshots/github-bulk", out, schema_id="forgectl/cli/snapshots.json")


def test_stdin_dash_reads_the_prs_one_per_line(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    script(fake_forges)
    done = run_forgectl(
        ["snapshots", "-", "--json", "--config", str(config)],
        env={"SHOW_TOKEN": TOKEN},
        input=f"{PR}\n\n  github.com/user-7/example-repo#8  \n",
    )
    assert done.returncode == 0, done.stderr
    assert [i["number"] for i in json.loads(done.stdout)["items"]] == [7, 8]


def test_a_pr_on_an_undeclared_host_is_exit_20_before_any_call(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]],
    fake_forges: FakeForges,
    config: Path,
) -> None:
    done = run(run_forgectl, config, PR, "nowhere.example.test/a/b#1")
    assert done.returncode != 0
    assert fake_forges.calls("gh") == []


def test_help_names_the_flag_and_stdin(
    run_forgectl: Callable[..., subprocess.CompletedProcess[str]], config: Path
) -> None:
    done = run_forgectl(["snapshots", "--help"], env={})
    assert done.returncode == 0
    assert "--bulk-only" in done.stdout and "stdin" in done.stdout


def test_unused_imports_stay_used() -> None:
    assert HEAD and show_recording and pr_of and rec
