"""Golden files for ``--json`` output: ``tests/golden/<name>.json``.

``assert_golden`` validates the value against its schema first (so an invalid output can never be
blessed), then compares it with the stored file. ``schema_id`` is required; a golden that is not
CLI output says ``schema_id=None``. Regenerate with ``make golden`` (``pytest --update-golden``) or
``FORGECTL_UPDATE_GOLDEN=1`` (exactly ``1``). The shared fixtures clear every ``FORGECTL_*``
variable before each test, so the request is read once at session start (``session_update``, set by
``pytest_configure`` in ``fixtures.py``) and the variable is also honoured when a test sets it
itself. Regeneration is refused when ``CI``, ``GITHUB_ACTIONS`` or ``GITLAB_CI`` is present at
all; the written content must pass the scrub check. A missing golden file fails.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path
from typing import Any

from . import schemas, scrub

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden"
UPDATE_ENV = "FORGECTL_UPDATE_GOLDEN"
# Set once per session by the pytest plugin, before the per-test environment scrub.
session_update = False
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*")
CI_VARS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI")


def update_requested() -> bool:
    return session_update or os.environ.get(UPDATE_ENV) == "1"


def render(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _path(name: str) -> Path:
    if not NAME.fullmatch(name):
        raise ValueError(f"bad golden name {name!r}")
    base = GOLDEN_DIR.resolve()
    path = (GOLDEN_DIR / f"{name}.json").resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"golden name {name!r} resolves outside {base}")
    return path


def assert_golden(name: str, actual: Any, *, schema_id: str | None) -> None:
    path = _path(name)
    if schema_id is not None:
        schemas.validate(actual, schema_id)
    text = render(actual)
    if update_requested():
        present = [v for v in CI_VARS if v in os.environ]
        assert not present, f"golden regeneration is refused in CI ({', '.join(present)} present)"
        leaks = scrub.find_unscrubbed(text, str(path))
        assert not leaks, f"refusing to write unscrubbed content: {leaks}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), f"no golden file {path}; create it with `make golden` after review"
    expected = path.read_text(encoding="utf-8")
    if expected != text:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                text.splitlines(keepends=True),
                "golden",
                "actual",
            )
        )
        raise AssertionError(f"golden {name} differs:\n{diff}")
