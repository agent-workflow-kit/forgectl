"""The live marker can never run in CI or under ``make verify``, and live modules are marked."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from .harness.fixtures import CI_VARS

REPO = Path(__file__).resolve().parent.parent
LIVE_DIR = REPO / "tests" / "live"


def make_target(name: str) -> tuple[list[str], list[str]]:
    """(prerequisites, recipe lines) of a Makefile target."""
    lines = (REPO / "Makefile").read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        match = re.fullmatch(rf"{re.escape(name)}:(.*)", line)
        if match:
            recipe = []
            for follow in lines[index + 1 :]:
                if not follow.startswith("\t"):
                    break
                recipe.append(follow.strip())
            return match.group(1).split(), recipe
    raise AssertionError(f"no Makefile target {name}")


def test_make_live_runs_the_live_marker_only_with_the_explicit_gate() -> None:
    _, recipe = make_target("live")
    assert recipe == ["uv run pytest -m live --run-live tests/live"]
    assert "live" in re.search(r"\.PHONY:(.*)", (REPO / "Makefile").read_text())[1].split()  # type: ignore[index]


def test_verify_and_test_never_reach_the_live_target() -> None:
    needs, _ = make_target("verify")
    assert "live" not in needs
    for target in ("verify", "test", "lint", "typecheck"):
        prerequisites, recipe = make_target(target)
        assert not any("live" in word for word in prerequisites + recipe), target


def test_no_workflow_selects_the_live_marker_or_names_a_live_setting() -> None:
    forbidden = ("make live", "--run-live", "-m live", '-m "live', "FORGECTL_LIVE_", "tests/live")
    workflows = sorted((REPO / ".github").rglob("*"))
    assert any(p.name == "ci.yml" for p in workflows)
    for path in workflows:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not [f for f in forbidden if f in text], path.name


def test_the_default_addopts_still_deselect_the_marker() -> None:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "addopts = \"-m 'not live'\"" in text


def test_every_live_test_module_is_marked_live() -> None:
    modules = sorted(LIVE_DIR.glob("test_*.py"))
    assert modules, "the harness ships a live smoke test"
    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        marked = any(
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets)
            and "live" in ast.unparse(node.value)
            for node in tree.body
        )
        assert marked, f"{module.name} lacks pytestmark = pytest.mark.live"


def run_collect(env_extra: dict[str, str], args: list[str]) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in CI_VARS}
    env.update(env_extra)
    env["PYTEST_ADDOPTS"] = ""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "tests/live", "-q", *args],
        env=env,
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
        timeout=120,
    )


def test_a_plain_run_deselects_every_live_test() -> None:
    done = run_collect({}, ["--collect-only"])
    assert (
        "deselected" in done.stdout and "test_lifecycle" not in done.stdout.split("deselected")[0]
    )


@pytest.mark.parametrize("ci", [False, True])
def test_the_live_target_skips_cleanly_without_settings_and_in_ci(ci: bool) -> None:
    """Even with fake settings present, CI skips; without them every test skips. The host is
    never reached: the fixtures skip before any request."""
    env: dict[str, str] = {}
    if ci:
        env = {
            "CI": "true",
            "FORGECTL_LIVE_GITHUB_TOKEN": "fake-token-0001",
            "FORGECTL_LIVE_GITHUB_USER": "someone",
            "FORGECTL_LIVE_GITLAB_TOKEN": "fake-token-0002",
            "FORGECTL_LIVE_GITLAB_USER": "someone",
        }
    else:
        for name in os.environ:
            if name.startswith("FORGECTL_LIVE_"):
                env[name] = ""
    done = run_collect(env, ["-m", "live", "--run-live", "-rs"])
    assert done.returncode == 0, done.stdout + done.stderr
    assert "passed" not in done.stdout and "failed" not in done.stdout, done.stdout
    assert "skipped" in done.stdout
