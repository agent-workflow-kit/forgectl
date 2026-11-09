"""The real opener, against a fake ``xdg-open`` in a private directory; no real browser."""

from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path

import pytest

from forgectl.tui.opener import BrowserOpener, OpenRefused, is_openable


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
