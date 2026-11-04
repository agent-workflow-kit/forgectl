"""Every test runs with a private HOME and XDG dirs and no FORGECTL_* variable, so no test reads
the real user's config or is steered by the caller's environment."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_environment(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    home = tmp_path_factory.mktemp("home")
    for name in list(os.environ):
        if name.startswith(("FORGECTL_", "XDG_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(home))
    # Files the tests create by hand must not depend on the caller's umask: the audit and log
    # files are refused when group- or world-writable.
    previous = os.umask(0o077)
    for name in ("CONFIG", "DATA", "STATE", "CACHE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(Path(home) / f".xdg-{name.lower()}"))
    yield
    os.umask(previous)
