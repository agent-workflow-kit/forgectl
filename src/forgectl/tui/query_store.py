"""What `browse` writes for its query: the history file and the default's drop-in.

Two files, both written atomically (a temp file in the same directory, then a rename) with mode
0600, and nothing else:

* ``${XDG_STATE_HOME:-~/.local/state}/forgectl/browse-history``: the submitted queries, one per
  line, newest first, at most ``HISTORY_MAX``. Text only: no forge data ever goes in it.
* ``<config dir>/config.d/90-browse-query.yaml``: ``browse.query``, the default query. The user's
  ``config.yaml`` is never touched, so its comments and layout stay.

A ``QueryStore`` without paths keeps the history in memory and cannot write a default: that is
how the tests run, so no test writes to a real state or config directory.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import query

HISTORY_MAX = 100
HISTORY_FILE = "browse-history"
DROP_IN = "90-browse-query.yaml"
_READ_LIMIT = 256 * 1024


def state_dir(env: Mapping[str, str]) -> Path | None:
    """``$XDG_STATE_HOME/forgectl`` when absolute, else ``~/.local/state/forgectl``; None when
    there is no usable home."""
    xdg = env.get("XDG_STATE_HOME", "")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / "forgectl"
    home = env.get("HOME", "")
    if home and os.path.isabs(home):
        return Path(home) / ".local" / "state" / "forgectl"
    return None


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a temp file in its directory (hidden, so the config
    loader skips it), mode 0600. Raises OSError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_history(path: Path) -> list[str]:
    """The stored queries, newest first. A missing, unreadable or odd file is an empty history,
    and a line that is not a valid query is skipped (the language may have changed)."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_READ_LIMIT + 1)
    except OSError:
        return []
    if len(raw) > _READ_LIMIT:
        return []
    out: list[str] = []
    for line in raw.decode("utf-8", errors="replace").split("\n"):
        text = line.strip()
        parsed = query.parse(text) if text else None
        if isinstance(parsed, query.Query):
            canonical = query.format_query(parsed)
            if canonical and canonical not in out:
                out.append(canonical)
        if len(out) >= HISTORY_MAX:
            break
    return out


def drop_in_text(text: str) -> str:
    """The drop-in file: ``browse.query`` as a double-quoted scalar (JSON strings are YAML)."""
    return f"browse:\n  query: {json.dumps(text)}\n"


@dataclass
class QueryStore:
    history_path: Path | None = None
    config_dir: Path | None = None
    entries: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.history_path is not None:
            self.entries = read_history(self.history_path)

    @property
    def drop_in_path(self) -> Path | None:
        return None if self.config_dir is None else self.config_dir / "config.d" / DROP_IN

    def add(self, text: str) -> str | None:
        """Remember a submitted query: canonical, no duplicate, newest first, capped. The empty
        query is not remembered. Returns a short note when the file could not be written (the
        in-memory history is updated anyway)."""
        parsed = query.parse(text)
        if not isinstance(parsed, query.Query):
            return None  # an invalid query is never stored
        canonical = query.format_query(parsed)
        if not canonical:
            return None
        self.entries = [canonical, *(e for e in self.entries if e != canonical)][:HISTORY_MAX]
        if self.history_path is None:
            return None
        try:
            write_atomic(self.history_path, "".join(f"{e}\n" for e in self.entries))
        except OSError as err:
            return f"history not saved: {err.strerror or 'error'}"
        return None

    def write_default(self, text: str) -> Path:
        """Write ``text`` as ``browse.query`` into the drop-in. Raises OSError, or ValueError for
        a text that is not a valid query (never written)."""
        parsed = query.parse(text)
        if not isinstance(parsed, query.Query):
            raise ValueError(str(parsed))
        path = self.drop_in_path
        if path is None:
            raise OSError("no config directory")
        write_atomic(path, drop_in_text(query.format_query(parsed)))
        return path
