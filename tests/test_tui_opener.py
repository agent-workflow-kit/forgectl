"""The real opener, against a fake ``xdg-open`` in a private directory; no real browser."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from forgectl import gateway
from forgectl.tui.opener import BrowserOpener, OpenRefused, is_openable


@pytest.fixture(autouse=True)
def private_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The harness refuses a child without a HOME under the test's private directory; the opener
    passes only what its allowlist finds in the test's environment, so give each child the
    private HOME when the test's own environment names none. The opener is never killed, so each
    test waits for the fake it started to finish."""
    real = gateway.start
    started: list[subprocess.Popen[bytes]] = []

    def start(
        argv: list[str], *, env: dict[str, str], cwd: str | None = None
    ) -> subprocess.Popen[bytes]:
        proc = real(argv, env={"HOME": os.environ["HOME"], **env}, cwd=cwd)
        started.append(proc)
        return proc

    monkeypatch.setattr(gateway, "start", start)
    yield
    for proc in started:
        proc.wait(timeout=10)


@pytest.fixture
def fake_opener(tmp_path: Path) -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "opened.log"
    name = "open" if sys.platform == "darwin" else "xdg-open"
    script = bindir / name
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$#" "$1" "${{GH_TOKEN:-unset}}" "${{SECRET_THING:-unset}}" >> "{log}"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bindir, log


def _wait_for(path: Path) -> list[str]:
    for _ in range(100):
        if path.exists() and path.read_text().count("\n") >= 4:
            return path.read_text().splitlines()
        time.sleep(0.05)
    raise AssertionError("the fake opener never ran")


def test_the_url_is_the_only_argument_and_the_environment_is_not_inherited(
    fake_opener: tuple[Path, Path], tmp_path: Path
) -> None:
    bindir, log = fake_opener
    environ = {
        "PATH": f"{bindir}{os.pathsep}relative/dir{os.pathsep}",
        "HOME": str(tmp_path / "home"),
        "GH_TOKEN": "leak-me",
        "SECRET_THING": "leak-me-too",
    }
    BrowserOpener(environ).open("https://github.com/acme/widgets/pull/1")
    assert _wait_for(log) == ["1", "https://github.com/acme/widgets/pull/1", "unset", "unset"]


def test_no_signal_is_ever_sent(
    fake_opener: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir, log = fake_opener
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    BrowserOpener({"PATH": str(bindir)}).open("http://example.com/x")
    _wait_for(log)
    assert sent == []


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,x",
        "https://a b",
        "https://a\nb",
        "--help",
        "-n https://a.example",
        "https://é.example",
        "",
    ],
)
def test_a_url_that_is_not_plain_http_or_https_is_refused_before_any_process(
    url: str, fake_opener: tuple[Path, Path]
) -> None:
    bindir, log = fake_opener
    with pytest.raises(OpenRefused):
        BrowserOpener({"PATH": str(bindir)}).open(url)
    time.sleep(0.2)
    assert not log.exists()


def test_a_missing_opener_program_is_a_refusal_not_a_crash(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(OpenRefused):
        BrowserOpener({"PATH": str(empty)}).open("https://example.com/")


def test_a_relative_path_entry_is_never_searched(
    fake_opener: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir, log = fake_opener
    monkeypatch.chdir(bindir.parent)
    with pytest.raises(OpenRefused):
        BrowserOpener({"PATH": "bin"}).open("https://example.com/")
    assert not log.exists()


def test_is_openable() -> None:
    assert is_openable("https://example.com/a?b=c#d")
    assert is_openable("http://example.com")
    assert not is_openable(None)
    assert not is_openable(5)
    assert not is_openable("HTTPS ://x")


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_browser_relevant_variables_pass_and_tokens_do_not(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "env.log"
    names = [
        "XAUTHORITY",
        "WSL_DISTRO_NAME",
        "WSL_INTEROP",
        "WSLENV",
        "DESKTOP_SESSION",
        "http_proxy",
        "HTTPS_PROXY",
        "no_proxy",
        "GH_TOKEN",
        "GITLAB_TOKEN",
    ]
    printed = " ".join(f'"${{{n}:-unset}}"' for n in names)
    name = "open" if sys.platform == "darwin" else "xdg-open"
    _script(bindir / name, f'printf "%s\\n" {printed} "$(pwd)" >> "{log}"\n')
    environ = {n: f"v-{n}" for n in names}
    environ["PATH"] = str(bindir)
    BrowserOpener(environ).open("https://example.com/")
    for _ in range(100):
        if log.exists() and log.read_text().count("\n") >= len(names) + 1:
            break
        time.sleep(0.05)
    lines = log.read_text().splitlines()
    assert lines[:-1] == [f"v-{n}" for n in names[:-2]] + ["unset", "unset"]
    assert lines[-1] == "/"  # a neutral working directory


def test_open_returns_the_program_name_it_launched(fake_opener: tuple[Path, Path]) -> None:
    bindir, log = fake_opener
    name = BrowserOpener({"PATH": str(bindir)}).open("https://example.com/")
    _wait_for(log)
    assert name == ("open" if sys.platform == "darwin" else "xdg-open")


def test_a_non_zero_opener_exit_is_reported(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _script(bindir / ("open" if sys.platform == "darwin" else "xdg-open"), "exit 4\n")
    codes: list[int] = []
    BrowserOpener({"PATH": str(bindir)}).open("https://example.com/", on_exit=codes.append)
    for _ in range(100):
        if codes:
            break
        time.sleep(0.05)
    assert codes == [4]


linux_only = pytest.mark.skipif(sys.platform == "darwin", reason="macOS always uses open")


@linux_only
def test_browser_names_a_bare_program_when_xdg_open_is_absent(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "opened.log"
    _script(bindir / "wslview", f'printf "%s\\n" "$1" >> "{log}"\n')
    name = BrowserOpener({"PATH": str(bindir), "BROWSER": "wslview:other"}).open(
        "https://example.com/"
    )
    assert name == "wslview"
    for _ in range(100):
        if log.exists():
            break
        time.sleep(0.05)
    assert log.read_text().splitlines() == ["https://example.com/"]


@linux_only
def test_browser_may_be_an_absolute_executable(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    prog = _script(tmp_path / "mybrowser", "exit 0\n")
    name = BrowserOpener({"PATH": str(bindir), "BROWSER": str(prog)}).open("https://example.com/")
    assert name == "mybrowser"


@linux_only
@pytest.mark.parametrize("value", ["firefox %s", "../x", "relative/x", "a b", ".", ".."])
def test_an_unsafe_browser_value_is_skipped_for_sensible_browser(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for bad in ("firefox", "x"):
        _script(bindir / bad, "exit 0\n")
    (bindir / "relative").mkdir()
    _script(bindir / "relative" / "x", "exit 0\n")
    monkeypatch.chdir(bindir)
    environ = {"PATH": str(bindir), "BROWSER": value}
    with pytest.raises(OpenRefused):
        BrowserOpener(environ).open("https://example.com/")
    _script(bindir / "sensible-browser", "exit 0\n")
    assert BrowserOpener(environ).open("https://example.com/") == "sensible-browser"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com;x" + "@evil.example/",
        "https://user" + "@github.com/",
        "https://github.com:443" + "@evil.example/a",
        "http://a@b/",
    ],
)
def test_a_url_with_userinfo_is_refused(url: str) -> None:
    assert not is_openable(url)


def test_the_length_cap_is_2048_characters_including_the_scheme() -> None:
    ok = "https://e.example/" + "a" * (2048 - len("https://e.example/"))
    assert len(ok) == 2048 and is_openable(ok)
    assert not is_openable(ok + "a")
    assert is_openable("https://e.example/a@b")  # @ outside the authority is fine


def test_the_opener_is_detached_in_its_own_session_with_slash_as_its_directory(
    tmp_path: Path,
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "session.log"
    script = bindir / ("open" if sys.platform == "darwin" else "xdg-open")
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$(pwd)" "$(ps -o sid= -p $$ | tr -d " ")" "$$" "x" >> "{log}"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    path = f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin"
    BrowserOpener({"PATH": path}).open("https://github.com/acme/widgets/pull/1")
    cwd, session, pid, _ = _wait_for(log)
    assert cwd == "/"
    assert session == pid  # a session leader: a group kill aimed at forgectl's group misses it


def test_no_token_variable_of_any_kind_reaches_the_opener(
    fake_opener: tuple[Path, Path], tmp_path: Path
) -> None:
    bindir, log = fake_opener
    environ = {
        "PATH": str(bindir),
        "HOME": str(tmp_path / "home"),
        "GITLAB_TOKEN": "x",
        "GH_ENTERPRISE_TOKEN": "x",
        "GITHUB_TOKEN": "x",
        "FORGECTL_ANYTHING": "x",
        "MY_TOKEN_ENV": "x",
    }
    opener = BrowserOpener(environ)
    assert set(opener._env()) == {"PATH", "HOME"}
    opener.open("https://github.com/acme/widgets/pull/1")
    _wait_for(log)
