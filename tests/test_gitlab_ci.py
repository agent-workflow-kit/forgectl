"""GitLab `ci` (#17): the head pipeline's statuses, `allow_failure`, retries, fork pipelines, child
pipelines, hosts below 17.9 and the merged-results decision, on hand-built recordings. Every
GitLab field and value used here is UNVERIFIED against a real host (see gitlab_support)."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from forgectl.backends.base import Backend
from forgectl.backends.gitlab import GitLabBackend
from forgectl.errors import ForgectlError
from forgectl.model import CiResult

from .gitlab_support import (
    HEAD,
    OLD,
    FakeGitLab,
    make_backend,
    mr_path,
    recording,
    ref,
    standard,
)
from .harness import golden, schemas

FORK_STATUSES = f"projects/202/repository/commits/{HEAD}/statuses"
STATUSES = f"projects/101/repository/commits/{HEAD}/statuses"


@pytest.fixture
def fake() -> FakeGitLab:
    return FakeGitLab()


def fork_mr(mr: dict[str, Any]) -> None:
    mr["source_project_id"] = 202
    mr["head_pipeline"]["project_id"] = 202


def job(name: str, status: str, identifier: int, *, allow_failure: bool = False) -> dict[str, Any]:
    row = copy.deepcopy(recording("statuses")[0])
    row.update(name=name, status=status, id=identifier, allow_failure=allow_failure)
    row["target_url"] = f"https://gitlab.com/user-9/proj/-/jobs/{identifier}"
    return row


def read_ci(
    fake: FakeGitLab,
    tmp_path: Path,
    *,
    fork: bool = False,
    mr_edit: Callable[[dict[str, Any]], None] | None = None,
    warnings: list[str] | None = None,
    **overrides: Any,
) -> CiResult:
    mr = recording("mr")
    if fork:
        fork_mr(mr)
    if mr_edit is not None:
        mr_edit(mr)
    standard(fake, mr=mr, **overrides)
    backend = make_backend(fake, tmp_path, warn=None if warnings is None else warnings.append)
    result = backend.ci(ref())
    schemas.validate_cli_output("ci", result.to_dict())
    return result


def only(job_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"statuses": job_rows}


BOTH = pytest.mark.parametrize("fork", [False, True], ids=["same-project", "fork"])


def test_the_backend_has_a_ci_method_that_is_not_a_stub(fake: FakeGitLab, tmp_path: Path) -> None:
    backend = make_backend(fake, tmp_path)
    assert isinstance(backend, Backend) and isinstance(backend, GitLabBackend)
    standard(fake)
    assert isinstance(backend.ci(ref()), CiResult)


@BOTH
def test_golden_ci(fake: FakeGitLab, tmp_path: Path, fork: bool) -> None:
    result = read_ci(fake, tmp_path, fork=fork)
    assert result.ci.state == "passing" and result.ci.complete
    assert fake.count(FORK_STATUSES if fork else STATUSES) == 1
    assert fake.count(STATUSES if fork else FORK_STATUSES) == 0
    golden.assert_golden(
        f"ci/gitlab-{'fork' if fork else 'open'}",
        result.to_dict(),
        schema_id="forgectl/cli/ci.json",
    )


# -- every state row, same project and fork -------------------------------------------------

_PENDING = (
    "created",
    "pending",
    "preparing",
    "running",
    "scheduled",
    "waiting_for_resource",
    "waiting_for_callback",
    "canceling",
)

ROWS: list[tuple[str, bool, str, bool, bool | None, str]] = [
    # job status, allow_failure, check state, counted, required, ci state
    ("success", False, "passing", True, None, "passing"),
    ("success", True, "passing", False, False, "none"),
    ("failed", False, "failing", True, None, "failing"),
    ("failed", True, "failing", False, False, "none"),
    ("canceled", False, "failing", True, None, "failing"),
    ("canceled", True, "failing", False, False, "none"),
    *[(s, False, "pending", True, None, "pending") for s in _PENDING],
    *[(s, True, "pending", False, False, "none") for s in _PENDING],
    ("manual", False, "pending", True, None, "pending"),
    ("manual", True, "skipped", False, False, "none"),
    ("skipped", False, "skipped", False, None, "none"),
    ("skipped", True, "skipped", False, False, "none"),
    ("brand_new_state", False, "pending", True, None, "pending"),
]


@BOTH
@pytest.mark.parametrize(("status", "allow", "state", "counted", "required", "ci_state"), ROWS)
def test_each_status_row_maps_to_a_check_and_a_ci_state(
    fake: FakeGitLab,
    tmp_path: Path,
    fork: bool,
    status: str,
    allow: bool,
    state: str,
    counted: bool,
    required: bool | None,
    ci_state: str,
) -> None:
    warnings: list[str] = []
    result = read_ci(
        fake,
        tmp_path,
        fork=fork,
        warnings=warnings,
        **only([job("j", status, 9, allow_failure=allow)]),
    )
    (check,) = result.ci.checks
    assert (check.state, check.counted, check.required) == (state, counted, required)
    assert result.ci.state == ci_state
    assert bool(warnings) is (status == "brand_new_state")


@BOTH
def test_a_non_string_or_missing_job_status_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path, fork: bool
) -> None:
    for bad in (None, 7, ["success"]):
        row = job("j", "success", 9)
        row["status"] = bad
        with pytest.raises(ForgectlError) as raised:
            read_ci(FakeGitLab(), tmp_path, fork=fork, **only([row]))
        assert raised.value.code == "upstream_error"


@BOTH
def test_a_counted_failure_with_a_passing_allow_failure_job_fails(
    fake: FakeGitLab, tmp_path: Path, fork: bool
) -> None:
    rows = [job("test", "failed", 1), job("lint", "success", 2, allow_failure=True)]
    ci = read_ci(fake, tmp_path, fork=fork, **only(rows)).ci
    assert ci.state == "failing"
    assert {c.name: c.counted for c in ci.checks} == {"test": True, "lint": False}


@BOTH
def test_only_allow_failure_jobs_read_none_not_passing(
    fake: FakeGitLab, tmp_path: Path, fork: bool
) -> None:
    rows = [job("a", "success", 1, allow_failure=True), job("b", "failed", 2, allow_failure=True)]
    assert read_ci(fake, tmp_path, fork=fork, **only(rows)).ci.state == "none"


@BOTH
def test_a_retried_job_counts_by_its_highest_id_whatever_the_order(
    fake: FakeGitLab, tmp_path: Path, fork: bool
) -> None:
    rows = [job("t", "success", 20), job("t", "failed", 10)]
    ci = read_ci(fake, tmp_path, fork=fork, **only(rows)).ci
    assert ci.state == "passing" and [c.state for c in ci.checks] == ["passing"]
    rows = [job("t", "failed", 10), job("t", "success", 20)]
    assert read_ci(FakeGitLab(), tmp_path, fork=fork, **only(rows)).ci.state == "passing"
    rows = [job("t", "success", 10), job("t", "failed", 20)]
    assert read_ci(FakeGitLab(), tmp_path, fork=fork, **only(rows)).ci.state == "failing"


@BOTH
def test_equal_ids_take_the_worse_state(fake: FakeGitLab, tmp_path: Path, fork: bool) -> None:
    rows = [job("t", "success", 5), job("t", "failed", 5)]
    assert read_ci(fake, tmp_path, fork=fork, **only(rows)).ci.state == "failing"


@pytest.mark.parametrize("bad_id", [None, "7", 1.5, True, [1], -0.0])
def test_a_status_id_that_is_not_an_integer_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path, bad_id: Any
) -> None:
    row = job("t", "success", 1)
    row["id"] = bad_id
    with pytest.raises(ForgectlError) as raised:
        read_ci(fake, tmp_path, **only([row]))
    assert raised.value.code == "upstream_error"


# -- the head pipeline: absent, skipped, another commit, merged results -----------------------


def test_a_null_head_pipeline_is_none_with_no_read(fake: FakeGitLab, tmp_path: Path) -> None:
    def clear(mr: dict[str, Any]) -> None:
        mr["head_pipeline"] = None

    result = read_ci(fake, tmp_path, mr_edit=clear)
    ci = result.ci
    assert (ci.state, ci.checks, ci.for_current_head, ci.complete) == ("none", (), True, True)
    assert ci.head_sha == HEAD
    assert fake.count(STATUSES) == fake.count(FORK_STATUSES) == 0
    assert not [c for c in fake.calls if "bridges" in c.path]


@BOTH
def test_a_skipped_pipeline_is_none(fake: FakeGitLab, tmp_path: Path, fork: bool) -> None:
    def skipped(mr: dict[str, Any]) -> None:
        mr["head_pipeline"]["status"] = "skipped"

    rows = [job("a", "skipped", 1), job("b", "skipped", 2)]
    assert read_ci(fake, tmp_path, fork=fork, mr_edit=skipped, **only(rows)).ci.state == "none"


@BOTH
def test_a_pipeline_of_another_commit_is_pending_whatever_it_reported(
    fake: FakeGitLab, tmp_path: Path, fork: bool
) -> None:
    def old(mr: dict[str, Any]) -> None:
        mr["head_pipeline"]["sha"] = OLD

    project = 202 if fork else 101
    mr = recording("mr")
    if fork:
        fork_mr(mr)
    old(mr)
    standard(fake, mr=mr)
    fake.pages(
        f"projects/{project}/repository/commits/{OLD}/statuses",
        [[job("t", "success", 3), job("u", "failed", 4)]],
    )
    ci = make_backend(fake, tmp_path).ci(ref()).ci
    assert (ci.state, ci.for_current_head, ci.head_sha) == ("pending", False, OLD)


def test_merged_results_and_merge_train_pipelines_stay_pending_fail_closed(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    """TODO(#31) decision pending; the documented behavior is `pending`, never `passing`: such a
    pipeline's sha is the merge ref's, not the head's, and a green one proves nothing here."""
    merge_sha = "d4" * 20

    def merged(mr: dict[str, Any]) -> None:
        mr["head_pipeline"]["sha"] = merge_sha
        mr["head_pipeline"]["ref"] = "refs/merge-requests/7/merge"

    mr = recording("mr")
    merged(mr)
    standard(fake, mr=mr)
    fake.pages(
        f"projects/101/repository/commits/{merge_sha}/statuses",
        [[job("t", "success", 3)]],
    )
    ci = make_backend(fake, tmp_path).ci(ref()).ci
    assert (ci.state, ci.for_current_head, ci.head_sha) == ("pending", False, merge_sha)


@BOTH
def test_checks_pending_and_failing_enter_blocked_by_while_the_host_reads_mergeable(
    fake: FakeGitLab, tmp_path: Path, fork: bool
) -> None:
    for status, code in (("running", "checks_pending"), ("failed", "checks_failing")):
        mr = recording("mr")
        if fork:
            fork_mr(mr)
        assert mr["detailed_merge_status"] == "mergeable"
        standard(fake := FakeGitLab(), mr=mr, **only([job("t", status, 1)]))
        read = make_backend(fake, tmp_path).get_pr(ref())
        assert [b.code.value for b in read.pr.blocked_by] == [code]


@pytest.mark.parametrize("fork", [False, True])
def test_a_pipeline_project_that_is_neither_the_mr_project_nor_its_fork_is_upstream_error(
    fake: FakeGitLab, tmp_path: Path, fork: bool
) -> None:
    def other(mr: dict[str, Any]) -> None:
        mr["head_pipeline"]["project_id"] = 999

    with pytest.raises(ForgectlError) as raised:
        read_ci(fake, tmp_path, fork=fork, mr_edit=other)
    assert raised.value.code == "upstream_error"
    assert not [c for c in fake.calls if "statuses" in c.path]


def test_an_unreadable_fork_pipeline_is_incomplete_and_a_target_one_is_an_error(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    for status in (404, 403):
        fk = FakeGitLab()
        mr = recording("mr")
        fork_mr(mr)
        standard(fk, mr=mr)
        fk.on(FORK_STATUSES, {"message": "x"}, status=status)
        warnings: list[str] = []
        ci = make_backend(fk, tmp_path, warn=warnings.append).ci(ref()).ci
        assert (ci.complete, ci.checks, ci.state) == (False, (), "none") and warnings
    for status in (404, 403):
        same = FakeGitLab()
        standard(same)
        same.on(STATUSES, {"message": "x"}, status=status)
        with pytest.raises(ForgectlError) as raised:
            make_backend(same, tmp_path).ci(ref())
        assert raised.value.code == "upstream_error"


# -- hosts below 17.9 -----------------------------------------------------------------------


@BOTH
@pytest.mark.parametrize("version", [{"version": "17.8.9"}, {"version": "garbage"}, {}])
def test_a_host_below_17_9_never_reads_passing(
    fake: FakeGitLab, tmp_path: Path, fork: bool, version: dict[str, str]
) -> None:
    warnings: list[str] = []
    ci = read_ci(fake, tmp_path, fork=fork, warnings=warnings, version=version).ci
    assert (ci.complete, ci.state) == (False, "pending")
    assert [c.state for c in ci.checks].count("passing") >= 1  # the jobs are listed
    assert any("17.9" in w for w in warnings)


def test_a_failure_on_an_old_host_still_reads_failing(fake: FakeGitLab, tmp_path: Path) -> None:
    ci = read_ci(fake, tmp_path, version={"version": "16.0.0"}, **only([job("t", "failed", 1)])).ci
    assert (ci.complete, ci.state) == (False, "failing")


# -- hostile answers --------------------------------------------------------------------------


def test_hostile_names_urls_and_dict_keys_never_reach_the_output(
    fake: FakeGitLab, tmp_path: Path
) -> None:
    row = job("te\nst‮", "success", 1)
    row["target_url"] = "javascript:alert(1)"
    row["evil\nkey"] = "x"
    ci = read_ci(fake, tmp_path, **only([row])).to_dict()["ci"]
    (check,) = ci["checks"]
    assert check["url"] is None and "\n" not in check["name"] and "‮" not in check["name"]
    assert set(check) == {"name", "state", "required", "counted", "url", "finished_at"}


def test_a_huge_unknown_status_is_capped_in_the_warning(fake: FakeGitLab, tmp_path: Path) -> None:
    warnings: list[str] = []
    read_ci(fake, tmp_path, warnings=warnings, **only([job("t", "x" * 100_000, 1)]))
    assert warnings and all(len(w) < 400 for w in warnings)


def test_the_token_is_never_in_the_output_or_warnings(fake: FakeGitLab, tmp_path: Path) -> None:
    from .gitlab_support import TOKEN

    warnings: list[str] = []
    result = read_ci(fake, tmp_path, warnings=warnings, **only([job("t", "weird", 1)]))
    assert TOKEN not in repr(result.to_dict()) and not any(TOKEN in w for w in warnings)


def test_an_mr_that_is_not_found_is_the_show_error(fake: FakeGitLab, tmp_path: Path) -> None:
    fake.on(mr_path(), {"message": "404 Not found"}, status=404)
    with pytest.raises(ForgectlError) as raised:
        make_backend(fake, tmp_path).ci(ref())
    assert raised.value.code == "not_found"


def test_the_ci_command_through_the_cli(
    run_forgectl: Any, fake_forges: Any, tmp_path: Path
) -> None:
    import json

    from .gitlab_support import TOKEN
    from .test_gitlab_cli import write_config
    from .test_gitlab_show_process import script

    script(fake_forges, "gitlab.com", "")
    done = run_forgectl(
        ["ci", "gitlab.com/user-9/proj#7", "--json", "--config", str(write_config(tmp_path))],
        env={"GL_TOKEN": TOKEN, "GH_TOKEN": "x"},
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    schemas.validate_cli_output("ci", out)
    assert out["ci"]["state"] == "passing" and out["pr"] == "gitlab.com/user-9/proj#7"
    assert TOKEN not in done.stdout and TOKEN not in done.stderr
