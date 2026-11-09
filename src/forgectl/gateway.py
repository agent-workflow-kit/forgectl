"""Gateway: the single boundary for subprocess and HTTP calls.

Every call to an external program (or HTTP endpoint) goes through this module so that
tests can substitute fakes (see design guide §14). Commands are always run with an argv
list, never through a shell.
"""

from __future__ import annotations

import subprocess


def start(argv: list[str], *, env: dict[str, str]) -> subprocess.Popen[bytes]:
    """Start ``argv`` detached from the terminal and return at once, without a shell.

    The caller owns the returned process: it records ``pid`` and signals only that exact
    process, never by name or pattern.
    """
    return subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def run(
    argv: list[str],
    *,
    stdin: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` without a shell and capture its text output."""
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
