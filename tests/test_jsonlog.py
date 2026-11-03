"""The append-only JSONL file layer: modes, symlinks, atomic lines, caps, concurrent writers."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from forgectl import jsonlog
from forgectl.jsonlog import JsonlError


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state" / "forgectl"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- where the files live ---------------------------------------------------------------------


def test_state_dir_follows_xdg_state_home_when_absolute() -> None:
    assert jsonlog.state_dir({"XDG_STATE_HOME": "/x/state", "HOME": "/h"}) == Path(
        "/x/state/forgectl"
    )


@pytest.mark.parametrize("xdg", ["", "relative/path"])
def test_state_dir_ignores_an_empty_or_relative_xdg_state_home(xdg: str) -> None:
    assert jsonlog.state_dir({"XDG_STATE_HOME": xdg, "HOME": "/h"}) == Path(
        "/h/.local/state/forgectl"
    )


# --- creation modes -----------------------------------------------------------------------------


def test_the_directory_is_created_0700_and_the_file_0600(state: Path) -> None:
    jsonlog.append(state, "audit.jsonl", {"a": 1})
    assert mode(state) == 0o700
    assert mode(state / "audit.jsonl") == 0o600


def test_modes_hold_under_a_permissive_umask(state: Path) -> None:
    old = os.umask(0)
    try:
        jsonlog.append(state, "audit.jsonl", {"a": 1})
    finally:
        os.umask(old)
    assert mode(state) == 0o700
    assert mode(state / "audit.jsonl") == 0o600


def test_an_existing_files_mode_is_not_changed(state: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    path = state / "log.jsonl"
    path.write_text("")
    path.chmod(0o640)
    jsonlog.append(state, "log.jsonl", {"a": 1})
    assert mode(path) == 0o640


def test_a_group_or_world_writable_directory_is_refused(state: Path) -> None:
    state.mkdir(parents=True)
    state.chmod(0o770)
    with pytest.raises(JsonlError):
        jsonlog.append(state, "audit.jsonl", {"a": 1})
    assert not (state / "audit.jsonl").exists()


# --- symlinks, types, owners ------------------------------------------------------------------


def test_a_symlinked_file_is_not_followed(state: Path, tmp_path: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    (state / "audit.jsonl").symlink_to(target)
    with pytest.raises(JsonlError):
        jsonlog.append(state, "audit.jsonl", {"a": 1})
    assert target.read_text() == "keep\n"


def test_a_dangling_symlink_is_not_created_through(state: Path, tmp_path: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    target = tmp_path / "new-file"
    (state / "audit.jsonl").symlink_to(target)
    with pytest.raises(JsonlError):
        jsonlog.append(state, "audit.jsonl", {"a": 1})
    assert not target.exists()


def test_a_symlinked_state_directory_is_not_followed(state: Path, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    state.parent.mkdir(parents=True)
    state.symlink_to(real)
    with pytest.raises(JsonlError):
        jsonlog.append(state, "audit.jsonl", {"a": 1})
    assert list(real.iterdir()) == []


def test_a_symlinked_parent_of_the_state_directory_is_allowed(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    jsonlog.append(tmp_path / "link" / "forgectl", "audit.jsonl", {"a": 1})
    assert (real / "forgectl" / "audit.jsonl").exists()


def test_a_directory_in_place_of_the_file_is_refused(state: Path) -> None:
    (state / "audit.jsonl").mkdir(parents=True)
    state.chmod(0o700)
    with pytest.raises(JsonlError):
        jsonlog.append(state, "audit.jsonl", {"a": 1})


def test_a_fifo_in_place_of_the_file_is_refused_without_hanging(state: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    fifo = state / "audit.jsonl"
    os.mkfifo(fifo)
    # A reader holds the FIFO open so a blocking open could not be told apart from a refusal.
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(JsonlError):
            jsonlog.append(state, "audit.jsonl", {"a": 1})
    finally:
        os.close(reader)


def test_a_hard_linked_file_is_refused(state: Path, tmp_path: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    other = tmp_path / "other"
    other.write_text("")
    os.link(other, state / "audit.jsonl")
    with pytest.raises(JsonlError):
        jsonlog.append(state, "audit.jsonl", {"a": 1})
    assert other.read_text() == ""


def test_a_directory_owned_by_someone_else_is_refused(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(state).st_uid + 1)
    with pytest.raises(JsonlError):
        jsonlog.append(state, "audit.jsonl", {"a": 1})


# --- lines ----------------------------------------------------------------------------------------


def test_a_record_is_one_ascii_line_ending_in_a_newline(state: Path) -> None:
    hostile = "a\nb\r\x00\x1b[31m‮ \u0085é\ud800" + "x" * 10
    jsonlog.append(state, "audit.jsonl", {"k": hostile})
    raw = (state / "audit.jsonl").read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert raw.isascii()
    assert json.loads(raw)["k"] == hostile


def test_appends_keep_earlier_lines(state: Path) -> None:
    for n in range(3):
        jsonlog.append(state, "audit.jsonl", {"n": n})
    lines = (state / "audit.jsonl").read_text().splitlines()
    assert [json.loads(x)["n"] for x in lines] == [0, 1, 2]


def test_a_record_over_the_line_cap_is_refused_and_nothing_is_written(state: Path) -> None:
    jsonlog.append(state, "audit.jsonl", {"n": 1})
    with pytest.raises(JsonlError):
        jsonlog.append(state, "audit.jsonl", {"big": "x" * jsonlog.MAX_LINE_BYTES})
    assert len((state / "audit.jsonl").read_text().splitlines()) == 1


def test_a_record_just_under_the_cap_is_written(state: Path) -> None:
    pad = jsonlog.MAX_LINE_BYTES - len(json.dumps({"p": ""}, separators=(",", ":"))) - 1
    jsonlog.append(state, "audit.jsonl", {"p": "x" * pad})
    assert len((state / "audit.jsonl").read_bytes()) == jsonlog.MAX_LINE_BYTES


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_non_finite_numbers_are_refused(state: Path, value: float) -> None:
    with pytest.raises(JsonlError):
        jsonlog.append(state, "audit.jsonl", {"v": value})


def test_an_unserializable_record_is_refused(state: Path) -> None:
    with pytest.raises(JsonlError):
        jsonlog.append(state, "audit.jsonl", {"v": object()})


# --- failures -------------------------------------------------------------------------------------


def test_a_full_disk_is_a_jsonl_error(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def full(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "write", full)
    with pytest.raises(JsonlError):
        jsonlog.append(state, "audit.jsonl", {"a": 1})


def test_a_short_write_is_a_jsonl_error(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, data[:3]))
    with pytest.raises(JsonlError):
        jsonlog.append(state, "audit.jsonl", {"a": 1})


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_an_unwritable_directory_is_a_jsonl_error(state: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    state.chmod(0o500)
    try:
        with pytest.raises(JsonlError):
            jsonlog.append(state, "audit.jsonl", {"a": 1})
    finally:
        state.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_an_uncreatable_state_directory_is_a_jsonl_error(tmp_path: Path) -> None:
    parent = tmp_path / "ro"
    parent.mkdir(mode=0o500)
    try:
        with pytest.raises(JsonlError):
            jsonlog.append(parent / "forgectl", "audit.jsonl", {"a": 1})
    finally:
        parent.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_an_existing_write_only_file_is_still_appended_to(state: Path) -> None:
    jsonlog.append(state, "audit.jsonl", {"a": 1})
    path = state / "audit.jsonl"
    path.chmod(0o200)
    try:
        jsonlog.check_appendable(state, "audit.jsonl")
        jsonlog.append(state, "audit.jsonl", {"a": 2})
        path.chmod(0o600)
        assert path.read_bytes().splitlines() == [b'{"a":1}', b'{"a":2}']
    finally:
        path.chmod(0o600)


def test_check_appendable_creates_the_file_and_writes_nothing(state: Path) -> None:
    jsonlog.check_appendable(state, "audit.jsonl")
    assert (state / "audit.jsonl").read_bytes() == b""
    assert mode(state / "audit.jsonl") == 0o600


def test_check_appendable_refuses_a_symlink(state: Path, tmp_path: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    (state / "audit.jsonl").symlink_to(tmp_path / "x")
    with pytest.raises(JsonlError):
        jsonlog.check_appendable(state, "audit.jsonl")


# --- concurrent writers ------------------------------------------------------------------------

_WRITER = """
import sys
from pathlib import Path
from forgectl import jsonlog
state, who, count, size = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
for i in range(count):
    jsonlog.append(state, "audit.jsonl", {"who": who, "i": i, "pad": who[0] * size})
"""


@pytest.fixture
def launched() -> Iterator[list[subprocess.Popen[bytes]]]:
    """Processes started by a test; teardown signals exactly these (by their Popen handles)."""
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)


def test_concurrent_processes_never_interleave_lines(
    state: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    workers, count, size = 8, 60, 20_000
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    for n in range(workers):
        launched.append(
            subprocess.Popen(
                [sys.executable, "-c", _WRITER, str(state), f"w{n}", str(count), str(size)],
                env=env,
            )
        )
    for proc in launched:
        assert proc.wait(timeout=120) == 0
    lines = (state / "audit.jsonl").read_bytes().split(b"\n")
    assert lines.pop() == b""
    # A writer that reads the file's end while another write is in flight adds a newline
    # first; the worst case is an empty line, which export skips.
    lines = [line for line in lines if line]
    assert len(lines) == workers * count
    seen: dict[str, list[int]] = {}
    for line in lines:
        record = json.loads(line)
        assert record["pad"] == record["who"][0] * size
        seen.setdefault(record["who"], []).append(record["i"])
    assert all(order == list(range(count)) for order in seen.values())


# --- review round 1 ----------


def test_an_append_after_an_unterminated_fragment_starts_its_own_line(state: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    (state / "audit.jsonl").write_bytes(b'{"n":0}\n{"torn":')
    jsonlog.append(state, "audit.jsonl", {"n": 1})
    lines = (state / "audit.jsonl").read_bytes().split(b"\n")
    assert lines == [b'{"n":0}', b'{"torn":', b'{"n":1}', b""]


def test_an_append_after_a_complete_line_adds_no_blank_line(state: Path) -> None:
    jsonlog.append(state, "audit.jsonl", {"n": 0})
    jsonlog.append(state, "audit.jsonl", {"n": 1})
    assert (state / "audit.jsonl").read_bytes() == b'{"n":0}\n{"n":1}\n'


def test_a_record_at_the_cap_still_goes_after_a_fragment(state: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    (state / "audit.jsonl").write_bytes(b"frag")
    pad = jsonlog.MAX_LINE_BYTES - len(json.dumps({"p": ""}, separators=(",", ":"))) - 1
    jsonlog.append(state, "audit.jsonl", {"p": "x" * pad})
    raw = (state / "audit.jsonl").read_bytes()
    assert raw.startswith(b"frag\n") and len(raw) == 5 + jsonlog.MAX_LINE_BYTES


def test_missing_parents_are_created_0700_under_a_permissive_umask(tmp_path: Path) -> None:
    state = tmp_path / "home" / ".local" / "state" / "forgectl"
    (tmp_path / "home").mkdir(mode=0o755)
    old = os.umask(0)
    try:
        jsonlog.append(state, "audit.jsonl", {"a": 1})
    finally:
        os.umask(old)
    assert mode(tmp_path / "home" / ".local") == 0o700
    assert mode(tmp_path / "home" / ".local" / "state") == 0o700
    assert mode(state) == 0o700
    assert mode(tmp_path / "home") == 0o755  # an existing parent is left alone


def test_open_read_refuses_a_hard_linked_file(state: Path, tmp_path: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    other = tmp_path / "other"
    other.write_text("")
    os.link(other, state / "audit.jsonl")
    with pytest.raises(JsonlError):
        jsonlog.open_read(state, "audit.jsonl")


def test_userinfo_is_dropped_up_to_the_last_at_sign() -> None:
    assert jsonlog.scrub_text("see https://u:pa@ss@host/x now") == "see https://host/x now"
    assert jsonlog.scrub_text("https://host/p@q") == "https://host/p@q"


@pytest.mark.parametrize(
    "token",
    [
        "glpat-abcdefghijkl",
        "glpat-abcdefghijkl.01.abcdefghi",
        "gloas-abcdefghijkl",
        "glrt-abcdefghijkl",
        "gldt-abcdefghijkl",
        "glptt-abcdefghijkl",
        "glft-abcdefghijkl",
        "glimt-abcdefghijkl",
        "glsoat-abcdefghijkl",
        "glcbt-abcdefghijkl",
        "glagent-abcdefghijkl",
        "glwt-abcdefghijkl",
        "glffct-abcdefghijkl",
        "gltst-abcdefghijkl",
        "(glpat-abcdefghijkl)",
    ],
)
def test_gitlab_token_shapes_are_masked(token: str) -> None:
    assert "abcdefghijkl" not in jsonlog.scrub_text(f"tok {token} end")


@pytest.mark.parametrize(
    "text",
    [
        "glossary-update-2024",
        "feature/global-config-refactor",
        "english-translations",
        "xglpat-abcdefghijkl",
    ],
)
def test_ordinary_words_are_not_masked_as_gitlab_tokens(text: str) -> None:
    assert jsonlog.scrub_text(f"see {text} end") == f"see {text} end"
