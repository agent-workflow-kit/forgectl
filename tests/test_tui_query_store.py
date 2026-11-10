"""The query history file and the default's drop-in: modes, atomicity, caps, and what is refused."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from forgectl.config import config_directory, load_config
from forgectl.tui import query
from forgectl.tui.query_store import (
    DROP_IN,
    HISTORY_MAX,
    QueryStore,
    drop_in_text,
    read_history,
    state_dir,
)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_state_dir_follows_xdg_state_home_then_home() -> None:
    assert state_dir({"XDG_STATE_HOME": "/s", "HOME": "/h"}) == Path("/s/forgectl")
    assert state_dir({"XDG_STATE_HOME": "rel", "HOME": "/h"}) == Path("/h/.local/state/forgectl")
    assert state_dir({"XDG_STATE_HOME": "", "HOME": "/h"}) == Path("/h/.local/state/forgectl")
    assert state_dir({"HOME": "rel"}) is None
    assert state_dir({}) is None


def test_history_is_newest_first_deduplicated_and_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "state" / "browse-history"
    store = QueryStore(history_path=path)
    for text in ("draft", "ci = failing", "authored", "DRAFT"):
        assert store.add(text) is None
    assert store.entries == ["draft", "authored", "ci = failing"]
    assert path.read_text() == "draft\nauthored\nci = failing\n"
    assert mode(path) == 0o600
    assert read_history(path) == store.entries
    assert QueryStore(history_path=path).entries == store.entries


def test_history_is_capped_at_100(tmp_path: Path) -> None:
    path = tmp_path / "browse-history"
    store = QueryStore(history_path=path)
    for n in range(HISTORY_MAX + 20):
        store.add(f"approvals > {n}")
    assert len(store.entries) == HISTORY_MAX == 100
    assert store.entries[0] == f"approvals > {HISTORY_MAX + 19}"
    assert len(path.read_text().splitlines()) == 100


@pytest.mark.parametrize(
    "text", ["ci =", "nonsense", "(", "title = 'x", "ORDER BY", "title = 'a\nb'"]
)
def test_an_invalid_query_is_never_stored(tmp_path: Path, text: str) -> None:
    path = tmp_path / "browse-history"
    store = QueryStore(history_path=path)
    assert store.add(text) is None
    assert store.entries == []
    assert not path.exists()


def test_the_empty_query_is_not_remembered(tmp_path: Path) -> None:
    store = QueryStore(history_path=tmp_path / "h")
    store.add("")
    store.add("   ")
    assert store.entries == []


def test_reading_skips_invalid_lines_and_survives_junk(tmp_path: Path) -> None:
    path = tmp_path / "h"
    path.write_bytes(b"draft\nnot a query (\n\xff\xfe\nci = failing\ndraft\n\n")
    assert read_history(path) == ["draft", "ci = failing"]
    assert read_history(tmp_path / "missing") == []
    path.write_bytes(b"x" * (300 * 1024))
    assert read_history(path) == []
    assert read_history(tmp_path) == []  # a directory


def test_a_write_failure_is_a_note_and_the_memory_history_still_updates(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    store = QueryStore(history_path=blocker / "browse-history")  # the parent is a file
    note = store.add("draft")
    assert note is not None and note.startswith("history not saved")
    assert store.entries == ["draft"]


def test_no_paths_means_memory_only() -> None:
    store = QueryStore()
    assert store.add("draft") is None
    assert store.entries == ["draft"]
    with pytest.raises(OSError):
        store.write_default("draft")


def test_the_drop_in_is_written_atomically_with_mode_0600(tmp_path: Path) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    main = config / "config.yaml"
    main.write_text("# my comments\nme: {name: jo}\n")
    before = main.read_bytes()
    store = QueryStore(config_dir=config)
    path = store.write_default("ci in (failing,pending) and not draft order by age desc")
    assert path == config / "config.d" / DROP_IN
    assert mode(path) == 0o600
    assert main.read_bytes() == before  # config.yaml is never touched
    assert sorted(p.name for p in (config / "config.d").iterdir()) == [DROP_IN]  # no temp file left
    assert path.read_text() == drop_in_text(
        "ci IN (failing, pending) AND NOT draft ORDER BY age DESC"
    )


def test_the_drop_in_replaces_an_older_one_and_loads_back(tmp_path: Path) -> None:
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "config.yaml").write_text("browse: {query: draft}\n")
    store = QueryStore(config_dir=config)
    for text in ("authored", "title = 'it\\'s \"quoted\" \\\\ [x]' AND ci IS NULL", ""):
        store.write_default(text)
        loaded = load_config(str(config / "config.yaml"), env={"HOME": str(tmp_path)})
        assert loaded.config.browse.query == query.canonical(text)


def test_the_drop_in_follows_the_config_file_in_use(tmp_path: Path) -> None:
    assert config_directory("/a/b/c.yaml", {}) == Path("/a/b")
    assert config_directory(None, {"HOME": "/h"}) == Path("/h/.config/forgectl")
    assert config_directory(None, {"XDG_CONFIG_HOME": "/x", "HOME": "/h"}) == Path("/x/forgectl")


def test_an_invalid_default_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    store = QueryStore(config_dir=tmp_path)
    with pytest.raises(ValueError):
        store.write_default("ci =")
    assert not (tmp_path / "config.d").exists()


def test_a_failed_write_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "cfg"
    store = QueryStore(config_dir=config)

    def boom(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.write_default("draft")
    assert list((config / "config.d").iterdir()) == []
