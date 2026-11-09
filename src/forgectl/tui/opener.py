"""Opening a URL in the user's browser, the one action `browse` has.

Only an ``http`` or ``https`` URL of plain printable ASCII is ever passed on, whatever the
caller says; anything else raises ``OpenRefused`` before a process exists. The opener program
is started through the gateway with an environment built from a short list, never the whole
caller environment. forgectl signals no process it did not start, and kills nothing here: the
launcher is left to finish (it may stay alive as the browser), and a thread reaps it.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
from typing import Protocol

from forgectl import gateway

_URL_RE = re.compile(r"https?://[\x21-\x7e]{1,2048}")
_ENV_NAMES = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_TYPE",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_DATA_DIRS",
    "DBUS_SESSION_BUS_ADDRESS",
    "BROWSER",
    "TMPDIR",
)


class OpenRefused(Exception):
    """The URL is not one `browse` opens, or no opener program exists."""


class Opener(Protocol):
    def open(self, url: str) -> None: ...


def is_openable(url: object) -> bool:
    """True for an ``http`` or ``https`` URL made of printable ASCII only."""
    return isinstance(url, str) and _URL_RE.fullmatch(url) is not None


def _absolute_path(path: str) -> str:
    return os.pathsep.join(p for p in path.split(os.pathsep) if os.path.isabs(p))


class BrowserOpener:
    """``xdg-open`` (``open`` on macOS), resolved against absolute ``PATH`` entries only."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._environ = dict(os.environ if environ is None else environ)

    def _env(self) -> dict[str, str]:
        env = {k: self._environ[k] for k in _ENV_NAMES if k in self._environ}
        env["PATH"] = _absolute_path(env.get("PATH", ""))
        return env

    def open(self, url: str) -> None:
        if not is_openable(url):
            raise OpenRefused("only http and https URLs are opened")
        env = self._env()
        program = shutil.which("open" if sys.platform == "darwin" else "xdg-open", path=env["PATH"])
        if program is None or not os.path.isabs(program):
            raise OpenRefused("no opener program found (xdg-open or open)")
        try:
            proc = gateway.start([program, url], env=env)
        except OSError as err:
            raise OpenRefused(f"could not start the opener: {err.strerror or 'error'}") from None
        # Reap it when it ends; the exact process we started, nothing else.
        threading.Thread(target=proc.wait, daemon=True).start()
