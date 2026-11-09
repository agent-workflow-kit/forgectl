"""Gateway: the single boundary for subprocess and HTTP calls.

Every call to an external program (or HTTP endpoint) goes through this module so that
tests can substitute fakes (see design guide §14). Commands are always run with an argv
list, never through a shell.
"""

from __future__ import annotations

import subprocess


def start(
    argv: list[str], *, env: dict[str, str], cwd: str | None = None
) -> subprocess.Popen[bytes]:
    """Start ``argv`` detached from the terminal and return at once, without a shell.

    The caller owns the returned process: it records ``pid`` and signals only that exact
    process, never by name or pattern.

    TODO(#7): the browser opener moves onto ``Gateway.run_external(mode="browser")`` (env =
    forgectl's environment minus every token variable, a private cwd), keeping this detached,
    never-killed lifecycle; this entry point then goes.
    """
    return subprocess.Popen(
        argv,
        env=env,
        cwd=cwd,
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
