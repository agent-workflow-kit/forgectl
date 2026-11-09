"""Output stream failures: the exit code computed before printing survives a bad stdout/stderr.

Each case runs the real entry point in a subprocess with a scratch HOME and XDG dirs, so closed
descriptors, ``/dev/full`` and broken pipes behave as they do for a user's shell.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# A harness with one working command, since the surface has no implemented handler yet.
HARNESS = (
    "import sys; from forgectl import registry; "
    "registry.HANDLERS['whoami'] = lambda inv: {'ok': 'yes'}; "
    "registry.load_handlers = lambda: None; "
    "from forgectl.cli import main; sys.exit(main(sys.argv[1:]))"
)
CLI = [sys.executable, "-c", "from forgectl.cli import main; import sys; sys.exit(main())"]
SUCCESS = [sys.executable, "-c", HARNESS]


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
    }


def run(
    command: list[str], argv: list[str], env: dict[str, str], redirect: str = ""
) -> subprocess.CompletedProcess[bytes]:
    """Run ``command argv`` with a shell redirection such as ``>&-`` or ``2>/dev/full``."""
    full = ["sh", "-c", f'exec "$@" {redirect}', "sh", *command, *argv]
    return subprocess.run(full, env=env, capture_output=True, timeout=60, check=False)


def run_broken_pipe(
    command: list[str], argv: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    """Run with stdout a pipe whose reader is already gone."""
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        return subprocess.run(
            [*command, *argv],
            env=env,
            stdout=write_end,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    finally:
        os.close(write_end)


def clean(stderr: bytes) -> None:
    assert b"Traceback" not in stderr
    assert b"Exception ignored" not in stderr
    assert b"BrokenPipe" not in stderr


USAGE = ["--json", "show"]  # missing the pr: exit 2


def test_stderr_closed_does_not_move_the_error_text_to_stdout(env: dict[str, str]) -> None:
    result = run(CLI, ["show"], env, "2>&-")
    assert result.returncode == 2
    assert result.stdout == b""


def test_stderr_closed_with_json_keeps_stdout_to_the_envelope(env: dict[str, str]) -> None:
    result = run(CLI, USAGE, env, "2>&-")
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "usage"


def test_stdout_closed_keeps_exit_2_and_the_stderr_text(env: dict[str, str]) -> None:
    result = run(CLI, USAGE, env, ">&-")
    assert result.returncode == 2
    assert b"forgectl: error: usage:" in result.stderr
    clean(result.stderr)


def test_stdout_full_keeps_exit_2(env: dict[str, str]) -> None:
    result = run(CLI, USAGE, env, ">/dev/full")
    assert result.returncode == 2
    assert b"forgectl: error: usage:" in result.stderr
    clean(result.stderr)


def test_stderr_full_keeps_exit_2(env: dict[str, str]) -> None:
    result = run(CLI, USAGE, env, "2>/dev/full")
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "usage"


def test_both_streams_full_keep_exit_2(env: dict[str, str]) -> None:
    assert run(CLI, USAGE, env, ">/dev/full 2>/dev/full").returncode == 2


def test_both_streams_closed_keep_exit_2(env: dict[str, str]) -> None:
    assert run(CLI, USAGE, env, ">&- 2>&-").returncode == 2


def test_broken_pipe_keeps_exit_2(env: dict[str, str]) -> None:
    result = run_broken_pipe(CLI, USAGE, env)
    assert result.returncode == 2
    assert b"forgectl: error: usage:" in result.stderr
    clean(result.stderr)


def test_unimplemented_command_with_full_stdout_keeps_exit_1(env: dict[str, str]) -> None:
    result = run(CLI, ["--json", "snapshot", "github.com/o/r#1"], env, ">/dev/full")
    assert result.returncode == 1


def test_success_with_stdout_full_stays_exit_0(env: dict[str, str]) -> None:
    result = run(SUCCESS, ["whoami"], env, ">/dev/full")
    assert result.returncode == 0
    clean(result.stderr)


def test_success_with_a_broken_pipe_stays_exit_0(env: dict[str, str]) -> None:
    result = run_broken_pipe(SUCCESS, ["--json", "whoami"], env)
    assert result.returncode == 0
    clean(result.stderr)


def test_success_with_stdout_closed_stays_exit_0(env: dict[str, str]) -> None:
    result = run(SUCCESS, ["whoami"], env, ">&-")
    assert result.returncode == 0
    clean(result.stderr)


def test_success_with_stderr_closed_still_prints_the_result(env: dict[str, str]) -> None:
    result = run(SUCCESS, ["whoami"], env, "2>&-")
    assert (result.returncode, result.stdout) == (0, b"ok: yes\n")


def test_success_with_stderr_full_stays_exit_0(env: dict[str, str]) -> None:
    assert run(SUCCESS, ["whoami"], env, "2>/dev/full").returncode == 0


@pytest.mark.parametrize("flag", ["--help", "--version"])
@pytest.mark.parametrize("redirect", [">/dev/full", ">&-"])
def test_help_and_version_exit_0_with_a_failed_stdout(
    env: dict[str, str], flag: str, redirect: str
) -> None:
    result = run(CLI, [flag], env, redirect)
    assert result.returncode == 0
    clean(result.stderr)
