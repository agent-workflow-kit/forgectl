"""The harness's own live smoke test: the throwaway lifecycle and the credentialed forgectl run.

It is the first thing to run on a new setup (``make live``). The feature suites (reads, merges,
posting) are separate files that stand on the same fixtures.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


@pytest.mark.parametrize("backend", ["github", "gitlab"])
def test_throwaway_branch_change_and_whoami(backend: str, request: pytest.FixtureRequest) -> None:
    session = request.getfixturevalue(f"live_{backend}")
    host, throwaway = session.host, session.throwaway
    base = throwaway.default_branch
    sha = host.branch_sha(throwaway, base)
    host.create_branch(throwaway, "live-smoke", sha)
    host.commit_file(throwaway, "live-smoke", "smoke.txt", "smoke\n", "test: smoke")
    host.open_change(throwaway, "live-smoke", base, "live smoke")
    done = session.forgectl(["whoami", "--json", "--host", session.cfg.host])
    assert done.returncode == 0, done.stderr
