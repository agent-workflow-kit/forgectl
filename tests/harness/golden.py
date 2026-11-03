"""Golden files for ``--json`` output: ``tests/golden/<name>.json``.

``assert_golden`` validates the value against its schema first (so an invalid output can never be
blessed), then compares it with the stored file. Regenerate with ``FORGECTL_UPDATE_GOLDEN=1``,
never in CI; the written content must pass the scrub check. A missing golden file fails.
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
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$")


def render(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _path(name: str) -> Path:
    if not NAME.match(name):
        raise ValueError(f"bad golden name {name!r}")
    return GOLDEN_DIR / f"{name}.json"


def assert_golden(name: str, actual: Any, *, schema_id: str | None = None) -> None:
    path = _path(name)
    if schema_id is not None:
        schemas.validate(actual, schema_id)
    text = render(actual)
    if os.environ.get(UPDATE_ENV):
        assert not os.environ.get("CI"), f"{UPDATE_ENV} is refused in CI"
        leaks = scrub.find_unscrubbed(text, str(path))
        assert not leaks, f"refusing to write unscrubbed content: {leaks}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), f"no golden file {path}; create it with {UPDATE_ENV}=1 after review"
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
