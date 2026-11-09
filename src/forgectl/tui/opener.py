"""Opening a URL in the user's browser, the one action `browse` has.

Only an ``http`` or ``https`` URL of plain printable ASCII, at most 2048 characters and with no
userinfo, is ever passed on, whatever the caller says; anything else raises ``OpenRefused``
before a process exists. The opener program is started through the gateway with an
environment built from a list, never the whole caller environment, and with ``/`` as its
working directory. forgectl signals no process it did not start, and kills nothing here: the
launcher is left to finish (it may stay alive as the browser), and a thread reaps it.

TODO(#7): start the opener through ``Gateway.run_external(mode="browser")`` (env = forgectl's
environment minus every token variable, a private cwd) instead of this allowlist, keeping the
no-kill, detached lifecycle.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
from collections.abc import Callable
from typing import Protocol

from forgectl import gateway

URL_MAX_LENGTH = 2048
_URL_RE = re.compile(r"https?://([^/?#]*)[\x21-\x7e]*")
_PRINTABLE_RE = re.compile(r"[\x21-\x7e]+")
_BARE_PROGRAM_RE = re.compile(r"[A-Za-z0-9._+-]+")
_PROXY_NAMES = ("http_proxy", "https_proxy", "no_proxy", "all_proxy")
_ENV_NAMES = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
    "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_TYPE",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_DATA_DIRS",
    "DBUS_SESSION_BUS_ADDRESS",
    "DESKTOP_SESSION",
    "KDE_FULL_SESSION",
    "GNOME_DESKTOP_SESSION_ID",
    "WSL_DISTRO_NAME",
    "WSL_INTEROP",
    "WSLENV",
    "BROWSER",
    "TMPDIR",
    *_PROXY_NAMES,
    *(name.upper() for name in _PROXY_NAMES),
)


class OpenRefused(Exception):
    """The URL is not one `browse` opens, or no opener program exists."""


class Opener(Protocol):
    def open(self, url: str, on_exit: Callable[[int], None] | None = None) -> str:
        """Hand ``url`` to an opener program and return its name.

        ``on_exit`` is called, from another thread, with the program's exit status when it is
        not zero.
        """
        ...


def is_openable(url: object) -> bool:
    """True for an ``http`` or ``https`` URL of printable ASCII, at most 2048 characters long,
    whose authority holds no userinfo (no ``@``)."""
    if not isinstance(url, str) or len(url) > URL_MAX_LENGTH:
        return False
    if _PRINTABLE_RE.fullmatch(url) is None:
        return False
    match = _URL_RE.fullmatch(url)
    return match is not None and match.group(1) != "" and "@" not in match.group(1)


def _absolute_path(path: str) -> str:
    return os.pathsep.join(p for p in path.split(os.pathsep) if os.path.isabs(p))


def _executable_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _from_browser(value: str, path: str) -> str | None:
    """``$BROWSER``'s first entry, only when it is a bare program name or an absolute file."""
    first = value.split(":", 1)[0]
    if os.path.isabs(first):
        return first if _PRINTABLE_RE.fullmatch(first) and _executable_file(first) else None
    if first in (".", "..") or _BARE_PROGRAM_RE.fullmatch(first) is None:
        return None  # a %s template, an argument, a relative path: never used
    return shutil.which(first, path=path)


def find_program(env: dict[str, str]) -> str | None:
    """``open`` on macOS; elsewhere ``xdg-open``, then ``$BROWSER``, then ``sensible-browser``.

    Names are resolved against absolute ``PATH`` entries only.
    """
    path = env.get("PATH", "")
    if sys.platform == "darwin":
        return shutil.which("open", path=path)
    found = shutil.which("xdg-open", path=path)
    if found is None and env.get("BROWSER"):
        found = _from_browser(env["BROWSER"], path)
    if found is None:
        found = shutil.which("sensible-browser", path=path)
    return found


class BrowserOpener:
    """Starts the opener program found by ``find_program`` with ``[program, url]``."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._environ = dict(os.environ if environ is None else environ)

    def _env(self) -> dict[str, str]:
        env = {k: self._environ[k] for k in _ENV_NAMES if k in self._environ}
        env["PATH"] = _absolute_path(env.get("PATH", ""))
        return env

    def open(self, url: str, on_exit: Callable[[int], None] | None = None) -> str:
        if not is_openable(url):
            raise OpenRefused("only http and https URLs are opened")
        env = self._env()
        program = find_program(env)
        if program is None or not os.path.isabs(program):
            raise OpenRefused("no opener program found (xdg-open, $BROWSER, sensible-browser)")
        try:
            proc = gateway.start([program, url], env=env, cwd="/")
        except OSError as err:
            raise OpenRefused(f"could not start the opener: {err.strerror or 'error'}") from None

        def reap() -> None:  # the exact process we started, nothing else
            status = proc.wait()
            if status != 0 and on_exit is not None:
                on_exit(status)

        threading.Thread(target=reap, daemon=True).start()
        return os.path.basename(program)
