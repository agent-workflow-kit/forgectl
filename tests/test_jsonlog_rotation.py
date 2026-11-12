"""Rotation of ``log.jsonl`` (interface: Files): limits, kept files, concurrency, not audit."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from forgectl import jsonlog
from forgectl.jsonlog import JsonlError, Rotation

LOG = "log.jsonl"


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state" / "forgectl"


@pytest.fixture(autouse=True)
def default_rotation() -> Iterator[None]:
    jsonlog.set_rotation(Rotation())
    yield
    jsonlog.set_rotation(Rotation())


def record(n: int, pad: int = 0) -> dict[str, object]:
    return {"n": n, "pad": "x" * pad}


def line_size(rec: dict[str, object]) -> int:
    return len(jsonlog.encode_line(rec))


def names(state: Path) -> list[str]:
    return sorted(p.name for p in state.iterdir() if p.name != "log.lock")


def lines_of(path: Path) -> list[dict[str, object]]:
    return [json.loads(x) for x in path.read_bytes().split(b"\n") if x]


def small(keep: int = 3) -> Rotation:
    return Rotation(max_bytes=jsonlog.MIN_MAX_BYTES, keep=keep)


def fill(state: Path, name: str, size: int) -> None:
    """A log file of exactly ``size`` bytes made of complete lines."""
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = b""
    n = 0
    while len(data) < size:
        room = size - len(data)
        pad = max(0, min(room - line_size(record(n)), 4000))
        data += jsonlog.encode_line(record(n, pad))
        n += 1
    assert len(data) == size
    (state / name).write_bytes(data)
    (state / name).chmod(0o600)


# --- defaults and bounds ----------------------------------------------------------------------


def test_defaults_are_10_mib_and_three_files() -> None:
    assert Rotation() == Rotation(max_bytes=10 * 1024 * 1024, keep=3)


@pytest.mark.parametrize(
    "max_bytes, keep",
    [
        (jsonlog.MIN_MAX_BYTES - 1, 3),
        (jsonlog.MAX_MAX_BYTES + 1, 3),
        (10_000_000, 0),
        (10_000_000, 101),
    ],
)
def test_rotation_refuses_values_out_of_range(max_bytes: int, keep: int) -> None:
    with pytest.raises(ValueError):
        Rotation(max_bytes=max_bytes, keep=keep)


def test_the_smallest_limit_holds_a_full_line() -> None:
    assert jsonlog.MIN_MAX_BYTES >= jsonlog.MAX_LINE_BYTES


# --- when it rotates --------------------------------------------------------------------------


def test_no_rotation_while_the_file_stays_within_the_limit(state: Path) -> None:
    jsonlog.append_log(state, record(0), small())
    jsonlog.append_log(state, record(1), small())
    assert names(state) == [LOG]
    assert [r["n"] for r in lines_of(state / LOG)] == [0, 1]


def test_a_line_that_fills_the_file_exactly_does_not_rotate(state: Path) -> None:
    rec = record(7)
    fill(state, LOG, jsonlog.MIN_MAX_BYTES - line_size(rec))
    jsonlog.append_log(state, rec, small())
    assert names(state) == [LOG]
    assert (state / LOG).stat().st_size == jsonlog.MIN_MAX_BYTES


def test_a_line_one_byte_over_rotates_first(state: Path) -> None:
    rec = record(7)
    fill(state, LOG, jsonlog.MIN_MAX_BYTES - line_size(rec) + 1)
    before = (state / LOG).read_bytes()
    jsonlog.append_log(state, rec, small())
    assert names(state) == [LOG, "log.jsonl.1"]
    assert (state / "log.jsonl.1").read_bytes() == before
    assert lines_of(state / LOG) == [rec]


def test_the_new_file_is_private_and_the_rotated_file_keeps_its_mode(state: Path) -> None:
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    (state / LOG).chmod(0o644)
    jsonlog.append_log(state, record(0), small())
    assert (state / LOG).stat().st_mode & 0o777 == 0o600
    assert (state / "log.jsonl.1").stat().st_mode & 0o777 == 0o644


def test_the_lock_file_is_private_and_only_made_when_rotating(state: Path) -> None:
    jsonlog.append_log(state, record(0), small())
    assert not (state / "log.lock").exists()
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    jsonlog.append_log(state, record(1), small())
    assert (state / "log.lock").stat().st_mode & 0o777 == 0o600


def test_an_empty_file_is_never_rotated(state: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    (state / LOG).write_bytes(b"")
    jsonlog.append_log(state, record(0), Rotation(max_bytes=jsonlog.MIN_MAX_BYTES, keep=1))
    assert names(state) == [LOG]


def test_a_missing_directory_is_created_and_nothing_rotates(state: Path) -> None:
    jsonlog.append_log(state, record(0), small())
    assert names(state) == [LOG]


# --- keeping files ----------------------------------------------------------------------------


def rotate_times(state: Path, times: int, keep: int) -> None:
    for n in range(times):
        fill(state, LOG, jsonlog.MIN_MAX_BYTES)
        (state / LOG).write_bytes((state / LOG).read_bytes()[:-1] + b"\n")  # tag unused
        jsonlog.append_log(state, {"gen": n}, small(keep))


def test_files_shift_up_and_the_oldest_is_dropped(state: Path) -> None:
    keep = 3
    for gen in range(5):
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        # the current file is full; its first line names the generation
        first = jsonlog.encode_line({"gen": gen, "pad": ""})
        rest = jsonlog.MIN_MAX_BYTES - len(first)
        body = first + b"\n" * rest
        (state / LOG).write_bytes(body)
        (state / LOG).chmod(0o600)
        jsonlog.append_log(state, {"after": gen}, small(keep))
    assert names(state) == [LOG, "log.jsonl.1", "log.jsonl.2", "log.jsonl.3"]
    gens = [lines_of(state / f"log.jsonl.{i}")[0]["gen"] for i in (1, 2, 3)]
    assert gens == [4, 3, 2]  # newest first; 0 and 1 fell off
    assert lines_of(state / LOG) == [{"after": 4}]


def test_a_single_kept_file_is_replaced_each_time(state: Path) -> None:
    for gen in range(3):
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        first = jsonlog.encode_line({"gen": gen})
        (state / LOG).write_bytes(first + b"\n" * (jsonlog.MIN_MAX_BYTES - len(first)))
        (state / LOG).chmod(0o600)
        jsonlog.append_log(state, {"after": gen}, small(1))
    assert names(state) == [LOG, "log.jsonl.1"]
    assert lines_of(state / "log.jsonl.1")[0] == {"gen": 2}


def test_existing_numbered_files_are_shifted_not_overwritten_in_place(state: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    (state / "log.jsonl.1").write_bytes(b'{"old":1}\n')
    (state / "log.jsonl.2").write_bytes(b'{"old":2}\n')
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    jsonlog.append_log(state, record(0), small(3))
    assert lines_of(state / "log.jsonl.2") == [{"old": 1}]
    assert lines_of(state / "log.jsonl.3") == [{"old": 2}]
    assert (state / "log.jsonl.1").stat().st_size == jsonlog.MIN_MAX_BYTES


def test_numbered_files_beyond_keep_are_removed(state: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    for i in (1, 2, 3, 4, 7):
        (state / f"log.jsonl.{i}").write_bytes(b'{"old":%d}\n' % i)
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    jsonlog.append_log(state, record(0), small(2))
    assert names(state) == [LOG, "log.jsonl.1", "log.jsonl.2"]
    assert lines_of(state / "log.jsonl.2") == [{"old": 1}]


def test_other_files_in_the_directory_are_left_alone(state: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    others = {
        "log.jsonl.bak": b"a",
        "log.jsonl.0": b"b",
        "log.jsonl.01": b"c",
        "log.jsonl.1x": b"d",
        "log.jsonl.": b"e",
        "xlog.jsonl.9": b"f",
        "audit.jsonl.1": b"g",
        "audit.jsonl": b"h",
    }
    for name, data in others.items():
        (state / name).write_bytes(data)
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    jsonlog.append_log(state, record(0), small(3))
    for name, data in others.items():
        assert (state / name).read_bytes() == data, name


def test_a_symlink_among_the_numbered_files_is_replaced_not_followed(
    state: Path, tmp_path: Path
) -> None:
    state.mkdir(parents=True, mode=0o700)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"precious")
    (state / "log.jsonl.2").write_bytes(b'{"old":2}\n')
    (state / "log.jsonl.3").symlink_to(outside)
    (state / "log.jsonl.4").symlink_to(outside)
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    jsonlog.append_log(state, record(0), small(3))
    assert outside.read_bytes() == b"precious"
    assert not (state / "log.jsonl.4").exists() and not (state / "log.jsonl.4").is_symlink()
    assert not (state / "log.jsonl.3").is_symlink()


def test_a_numbered_directory_does_not_stop_logging(state: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    (state / "log.jsonl.2").write_bytes(b'{"old":2}\n')
    (state / "log.jsonl.3").mkdir()
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    with pytest.raises(JsonlError):
        jsonlog.rotate_log(state, 10, small(3))
    # logging itself reports it as a warning and still writes the record
    jsonlog.set_rotation(small(3))
    warning = jsonlog.log_event(state, "info", "x")
    assert warning is not None and "not rotated" in warning
    assert lines_of(state / LOG)[-1]["event"] == "x"


def test_a_symlinked_log_is_not_rotated_and_still_refused(state: Path, tmp_path: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    target = tmp_path / "elsewhere"
    target.write_bytes(b"x" * jsonlog.MIN_MAX_BYTES)
    (state / LOG).symlink_to(target)
    with pytest.raises(JsonlError):
        jsonlog.append_log(state, record(0), small())
    assert (state / LOG).is_symlink()
    assert names(state) == [LOG]
    assert target.read_bytes() == b"x" * jsonlog.MIN_MAX_BYTES


# --- audit.jsonl is never rotated -------------------------------------------------------------


def test_audit_jsonl_is_never_rotated(state: Path) -> None:
    fill(state, "audit.jsonl", 3 * jsonlog.MIN_MAX_BYTES)
    before = (state / "audit.jsonl").read_bytes()
    jsonlog.append(state, "audit.jsonl", {"a": 1})
    after_append = before + b'{"a":1}\n'
    assert (state / "audit.jsonl").read_bytes() == after_append
    # log rotation, even many times, does not touch it
    for _ in range(3):
        fill(state, LOG, jsonlog.MIN_MAX_BYTES)
        jsonlog.append_log(state, record(0), small(2))
    assert (state / "audit.jsonl").read_bytes() == after_append
    assert not any(n.startswith("audit.jsonl.") for n in names(state))


def test_rotate_log_has_no_file_name_parameter() -> None:
    import inspect

    assert "name" not in inspect.signature(jsonlog.rotate_log).parameters
    assert "name" not in inspect.signature(jsonlog.append_log).parameters


# --- log_event uses the process setting -------------------------------------------------------


def test_log_event_rotates_with_the_configured_limits(state: Path) -> None:
    jsonlog.set_rotation(small(2))
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    assert jsonlog.log_event(state, "info", "after_rotation") is None
    assert names(state) == [LOG, "log.jsonl.1"]
    assert lines_of(state / LOG)[0]["event"] == "after_rotation"


def test_log_event_rotation_argument_wins_over_the_process_setting(state: Path) -> None:
    jsonlog.set_rotation(Rotation())
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    assert jsonlog.log_event(state, "info", "x", rotation=small(1)) is None
    assert "log.jsonl.1" in names(state)


# --- hostile environments ---------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_a_read_only_state_directory_warns_and_still_appends(state: Path) -> None:
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    state.chmod(0o500)
    try:
        jsonlog.set_rotation(small())
        warning = jsonlog.log_event(state, "info", "x")
    finally:
        state.chmod(0o700)
    assert warning is not None and "not rotated" in warning
    assert names(state) == [LOG]
    assert lines_of(state / LOG)[-1]["event"] == "x"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_a_read_only_log_file_in_a_writable_directory_rotates_and_starts_fresh(
    state: Path,
) -> None:
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    (state / LOG).chmod(0o400)
    jsonlog.append_log(state, record(0), small())
    assert (state / "log.jsonl.1").stat().st_mode & 0o777 == 0o400
    assert lines_of(state / LOG) == [record(0)]


def test_an_unsafe_state_directory_is_refused_before_any_rotation(state: Path) -> None:
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    state.chmod(0o770)
    with pytest.raises(JsonlError):
        jsonlog.append_log(state, record(0), small())
    assert names(state) == [LOG]


def test_a_missing_state_directory_is_not_a_rotation_error(state: Path) -> None:
    jsonlog.rotate_log(state, 10, small())  # nothing to rotate, nothing to create
    assert not state.exists()


# --- concurrency ------------------------------------------------------------------------------


def hold_lock(state: Path) -> int:
    fd = os.open(state / "log.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_a_busy_lock_skips_rotation_without_losing_the_record(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jsonlog, "LOCK_WAIT_SECONDS", 0.05)
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    fd = hold_lock(state)
    try:
        jsonlog.append_log(state, record(0), small())
    finally:
        os.close(fd)
    assert "log.jsonl.1" not in names(state)
    assert lines_of(state / LOG)[-1] == record(0)


def test_a_process_that_loses_the_race_sees_the_rotated_file_and_does_not_rotate_again(
    state: Path,
) -> None:
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    fd = hold_lock(state)
    done = threading.Event()
    problems: list[BaseException] = []

    def writer() -> None:
        try:
            jsonlog.append_log(state, record(5), small())
        except BaseException as err:  # noqa: BLE001
            problems.append(err)
        done.set()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert not done.wait(0.3)  # it is waiting for the lock
        # the "other process" rotates while this one waits
        os.rename(state / LOG, state / "log.jsonl.1")
        (state / LOG).write_bytes(b'{"fresh":1}\n')
        (state / LOG).chmod(0o600)
    finally:
        os.close(fd)
    thread.join(timeout=30)
    assert done.is_set() and not problems
    assert names(state) == [LOG, "log.jsonl.1"]
    assert lines_of(state / LOG) == [{"fresh": 1}, record(5)]


def test_a_file_that_vanishes_during_rotation_is_not_an_error(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fill(state, LOG, jsonlog.MIN_MAX_BYTES)
    real = os.rename

    def racing(src: str, dst: str, **kw: object) -> None:
        if src == LOG:  # another process moved it first
            real(state / LOG, state / "log.jsonl.9")
        real(src, dst, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "rename", racing)
    jsonlog.append_log(state, record(0), small())
    assert lines_of(state / LOG) == [record(0)]


_WRITER = """
import sys
from pathlib import Path
from forgectl import jsonlog
state, who, count, pad = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rot = jsonlog.Rotation(max_bytes=int(sys.argv[5]), keep=int(sys.argv[6]))
for i in range(count):
    jsonlog.append_log(state, {"who": who, "i": i, "pad": who[0] * pad}, rot)
"""


@pytest.fixture
def launched() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)


def run_writers(
    state: Path,
    launched: list[subprocess.Popen[bytes]],
    *,
    workers: int,
    count: int,
    pad: int,
    keep: int,
) -> None:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    for n in range(workers):
        args = [str(state), f"w{n}", str(count), str(pad), str(jsonlog.MIN_MAX_BYTES), str(keep)]
        launched.append(subprocess.Popen([sys.executable, "-c", _WRITER, *args], env=env))
    for proc in launched:
        assert proc.wait(timeout=300) == 0


def all_records(state: Path) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for path in state.iterdir():
        if path.name == LOG or path.name.startswith("log.jsonl."):
            found.extend(lines_of(path))
    return found


def test_concurrent_writers_lose_no_record_and_split_no_line(
    state: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    workers, count, pad = 6, 300, 400  # about 1.2 MB: some twenty rotations
    run_writers(state, launched, workers=workers, count=count, pad=pad, keep=100)
    records = all_records(state)
    assert len(records) == workers * count
    assert len({(r["who"], r["i"]) for r in records}) == workers * count
    assert all(r["pad"] == str(r["who"])[0] * pad for r in records)
    assert len([n for n in names(state) if n.startswith("log.jsonl.")]) >= 5
    for path in state.iterdir():
        if path.name.startswith("log.jsonl"):
            assert path.stat().st_size <= jsonlog.MIN_MAX_BYTES + workers * 1024


def test_concurrent_writers_with_a_small_keep_stay_bounded_and_whole(
    state: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    run_writers(state, launched, workers=6, count=300, pad=400, keep=2)
    rotated = [n for n in names(state) if n.startswith("log.jsonl.")]
    assert sorted(rotated) == ["log.jsonl.1", "log.jsonl.2"]
    for path in (state / LOG, state / "log.jsonl.1", state / "log.jsonl.2"):
        for raw in path.read_bytes().split(b"\n"):
            if raw:
                assert json.loads(raw)["pad"]  # every line parses whole


def test_concurrent_log_events_in_one_process_family_never_crash(
    state: Path, launched: list[subprocess.Popen[bytes]]
) -> None:
    # writers that find the directory mid-rotation (no log.jsonl at all) just create it
    run_writers(state, launched, workers=4, count=150, pad=900, keep=3)
    assert (state / LOG).exists()


# --- the config reaches the log writes --------------------------------------------------------


def test_a_command_rotates_with_the_limits_of_its_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from forgectl.cli import main
    from forgectl.humanactions import Tty

    live = Path(os.environ["XDG_STATE_HOME"]) / "forgectl"
    config = tmp_path / "c.yaml"
    config.write_text(f"log:\n  max_bytes: {jsonlog.MIN_MAX_BYTES}\n  keep: 1\n", encoding="utf-8")
    fill(live, LOG, jsonlog.MIN_MAX_BYTES)
    before = (live / LOG).read_bytes()
    code = main(
        ["--config", str(config), "--verbose", "export"], tty=Tty(stdin=False, stderr=False)
    )
    capsys.readouterr()
    assert code == 0
    assert (live / "log.jsonl.1").read_bytes() == before
    assert [r["event"] for r in lines_of(live / LOG)] == ["command_done"]
    assert jsonlog.rotation() == Rotation(max_bytes=jsonlog.MIN_MAX_BYTES, keep=1)


def test_a_run_with_a_bad_config_logs_with_the_defaults(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from forgectl.cli import main
    from forgectl.humanactions import Tty

    live = Path(os.environ["XDG_STATE_HOME"]) / "forgectl"
    config = tmp_path / "c.yaml"
    config.write_text("log:\n  keep: 0\n", encoding="utf-8")
    fill(live, LOG, jsonlog.MIN_MAX_BYTES)  # far under the 10 MiB default
    code = main(["--config", str(config), "export"], tty=Tty(stdin=False, stderr=False))
    capsys.readouterr()
    assert code == 2
    assert "log.jsonl.1" not in names(live)
    assert lines_of(live / LOG)[-1]["event"] == "config_invalid"
