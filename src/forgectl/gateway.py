"""Gateway: the only process boundary for forge calls (interface: Invoking the backends).

Every program forgectl starts for a forge goes through this module, so a test can substitute a
fake runner and so one place decides which token reaches which host. It owns:

- the executable (absolute ``PATH`` entries only) and an argv list that is never a shell command;
- an environment built from scratch per mode (token mode: exactly one token variable and a fresh
  private config dir; stored-login mode: the caller's config variables), with the fixed settings;
- an empty private working directory (0700, under ``TMPDIR``) and a git ceiling above it, both
  removed when the call ends;
- routing (``gh --hostname`` always; ``glab`` by the routing table) and one percent-encoding
  function for every path segment and query value;
- the child as the leader of its own process group, a deadline that kills the whole group (SIGTERM,
  then SIGKILL, only while the leader is unreaped) and size caps on both streams;
- status from the ``-i`` headers (never from the exit code or message text), the typed errors of
  the reliability tables, GraphQL result checks and the answer identity check.

Reads are not retried here (``reads.py`` owns the retry loop and pagination); a write is never
retried. A signal that reaches the main thread while a child runs terminates the child's group,
removes the private directories and propagates unchanged: for a write, ``audit.Attempt`` records
``outcome_unknown`` as it unwinds. SIGINT, SIGTERM and SIGHUP are held while that cleanup runs, and
nothing raised during it replaces the exception already on its way out.

``run_external`` is the second entry point (hooks, ``token_command``, the browser): the same
process-group rules and no forge parsing, with an explicit environment mode per use.
"""

from __future__ import annotations

import contextlib
import ctypes
import email.utils
import errno
import json
import math
import os
import pwd
import re
import select
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .errors import Cancelled, ForgectlError
from .sanitize import sanitize_line

if TYPE_CHECKING:  # the watchdog imports this module with the standard library only
    from .config import LoadedConfig

KILL_GRACE = 2.0  # seconds between SIGTERM and SIGKILL to a child's group
MAX_SELECT_WAIT = 86400.0  # one select wait; the loop recomputes the deadline after it
REAP_WAIT = 5.0  # seconds to wait for the leader after SIGKILL
DRAIN_AFTER_EXIT = 0.5  # seconds to keep reading after the leader exited (a held-open pipe)
MAX_STDOUT = 32 * 1024 * 1024
MAX_STDERR = 64 * 1024
WRITE_RESERVE = 15.0  # seconds added to the budget, once, after a write (verification reads)
MAX_ENDPOINT = 8192
MAX_TOKEN = 4096
MAX_MESSAGE = 300

Backend = Literal["github", "gitlab"]
ExternalMode = Literal["hook", "browser", "token_command"]
Shape = Literal["any", "object", "list"]

# Never reach a child (the environment is built from scratch; the names are listed so a test and
# ``run_external`` can refuse them in ``extra_env``).
TOKEN_VARIABLES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITLAB_TOKEN",
    "GITLAB_ACCESS_TOKEN",
    "OAUTH_TOKEN",
)
_NEVER_EXTRA = frozenset({"GH_HOST", "GH_REPO", "GH_FORCE_TTY", "CLICOLOR_FORCE"})
FIXED_SETTINGS = {
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "NO_COLOR": "1",
    "GLAB_SEND_TELEMETRY": "false",
    "GLAB_CHECK_UPDATE": "false",
    "GLAB_SHOW_WHATS_NEW": "false",
    # Never NO_PROMPT: glab 1.89 prints a deprecation warning to stdout for it on every call.
    "GLAB_NO_PROMPT": "1",
}
_PASSTHROUGH = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "GLAB_CA_CERT",
)
_BUS_VARIABLES = ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
# Where the session bus must stay unused, the child gets this address, which fails at once: with
# no bus variable at all gh 2.87.3 still dials the default /run/user/<uid>/bus socket.
NO_BUS_ADDRESS = "unix:path=/nonexistent/forgectl-no-bus"
_FALLBACK_TMPDIR = "/tmp"
_HELD_SIGNALS = frozenset({signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
_HAS_WAITID = hasattr(os, "waitid")  # Linux: exit is seen without reaping
_HAS_KQUEUE = hasattr(select, "kqueue")  # macOS, BSD: EVFILT_PROC / NOTE_EXIT, the same
_FALLBACK_PATH = "/usr/bin:/bin"
_INSTALL = {
    "gh": "brew install gh (or see https://cli.github.com/)",
    "glab": "brew install glab (or see https://gitlab.com/gitlab-org/cli#installation)",
}
_FORBIDDEN_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "private-token",
        "job-token",
        "deploy-token",
        "cookie",
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
    }
)
_HEADER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}\Z")
_HEADER_VALUE = re.compile(r"^[\x20-\x7e]{0,512}\Z")
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_NAME = re.compile(rf"^{_HOST_LABEL}(?:\.{_HOST_LABEL})*\Z")
_PORT = re.compile(r"^[1-9][0-9]{0,4}\Z")
_API_PREFIX = re.compile(r"^(?:/(?!\.\.?(?:/|\Z))[A-Za-z0-9._-]{1,64}){1,8}\Z")
_FIRST_SEGMENT = re.compile(r"^[A-Za-z0-9]")
_TOKEN = re.compile(rf"^[\x21-\x7e]{{1,{MAX_TOKEN}}}\Z")
_STATUS_LINE = re.compile(rb"^HTTP/[0-9](?:\.[0-9])? ([0-9]{3})(?: [^\r\n]*)?$")
_RATE_WORDING = re.compile(r"rate limit|abuse detection|too many requests", re.IGNORECASE)
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


def _error(code: str, message: str, **kwargs: Any) -> ForgectlError:
    return ForgectlError(code, message, **kwargs)


class AnswerCut(ForgectlError):
    """A read whose answer went past forgectl's own size cap: ``upstream_error`` like any other
    unusable answer, told apart by its type for a caller to which a cut means too large (a
    diff)."""


# -- Percent-encoding and endpoints ---------------------------------------------------------


def quote_component(value: str) -> str:
    """The one percent-encoding function: every byte but ``A-Za-z0-9._~-`` becomes ``%XX``.

    ``/``, ``:``, ``{``, ``}``, ``?``, ``#``, ``%`` and space are all encoded, so the placeholders
    the CLIs expand (``{owner}``, ``:id``) never survive.
    """
    if not isinstance(value, str):
        raise TypeError(f"a path segment or query value must be str, not {type(value).__name__}")
    try:
        return urllib.parse.quote(value, safe="")
    except UnicodeEncodeError as exc:  # a lone surrogate
        raise ValueError("value is not encodable as UTF-8") from exc


def path_int(value: object) -> str:
    """An integer from a response, checked before it builds a path (no bool, no sign, no float)."""
    if isinstance(value, bool):
        raise ValueError("not an integer")
    if isinstance(value, int):
        if 0 <= value < 2**63:
            return str(value)
        raise ValueError("integer out of range")
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 19:
        number = int(value)
        if number < 2**63:
            return str(number)
    raise ValueError("not an integer")


def build_endpoint(segments: Sequence[str], query: Sequence[tuple[str, str]] = ()) -> str:
    """``seg/seg?k=v&k=v`` from validated parts, every part percent-encoded, never a URL."""
    if not segments:
        raise ValueError("an endpoint needs at least one segment")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        if segment in ("", ".", ".."):
            raise ValueError(f"unusable path segment {segment!r}")
        encoded = quote_component(segment)
        if index == 0 and not _FIRST_SEGMENT.match(encoded):
            raise ValueError("the first segment must start with a letter or digit")
        parts.append(encoded)
    endpoint = "/".join(parts)
    if query:
        endpoint += "?" + "&".join(f"{quote_component(k)}={quote_component(v)}" for k, v in query)
    if len(endpoint) > MAX_ENDPOINT:
        raise ValueError("endpoint too long")
    return endpoint


# -- Targets, environment and private directories -------------------------------------------


def _valid_host(host: object) -> bool:
    """DNS labels joined by dots (lowercase ASCII), with an optional port 1-65535."""
    if not isinstance(host, str) or not host.isascii():
        return False
    name, colon, port = host.partition(":")
    if not (len(name) <= 253 and _HOST_NAME.match(name)):
        return False
    return not colon or (bool(_PORT.match(port)) and int(port) <= 65535)


@dataclass(frozen=True)
class Target:
    """The host a call goes to and how it authenticates.

    ``token`` set is token mode (the one token variable of the interface's Credentials table);
    None is stored-login mode. The host is lowercased first: every routing and token decision
    uses the lowercase form.

    ``hosts.py`` builds this from the config and the host's own credential.
    """

    backend: Backend
    host: str
    api_prefix: str = ""
    token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.backend not in ("github", "gitlab"):
            raise ValueError(f"unknown backend {self.backend!r}")
        if not isinstance(self.host, str) or not self.host.isascii():
            raise ValueError("host is not a plain host name with an optional port")
        object.__setattr__(self, "host", self.host.lower())  # hosts are case-insensitive
        if not _valid_host(self.host):
            raise ValueError("host is not a plain host name with an optional port")
        if self.backend == "github" and ":" in self.host:
            raise _error(
                "config_invalid",
                f"hosts.{self.host}: a GitHub host takes no port (gh refuses one)",
                details={"key": f"hosts.{self.host}"},
            )
        if self.api_prefix:
            if self.backend != "gitlab" or self.host == "gitlab.com":
                raise ValueError("api_prefix is only for self-managed GitLab hosts")
            if not isinstance(self.api_prefix, str) or not _API_PREFIX.match(self.api_prefix):
                raise ValueError("api_prefix must be /segment[/segment...] with no trailing slash")
        if self.token is not None and not _TOKEN.match(self.token):
            raise _error(
                "auth_required",
                "the token is not one line of printable ASCII without spaces; it was not used",
            )

    @property
    def program(self) -> str:
        return "gh" if self.backend == "github" else "glab"

    @property
    def token_variable(self) -> str:
        if self.backend == "gitlab":
            return "GITLAB_TOKEN"
        if self.host == "github.com" or self.host.endswith(".ghe.com"):
            return "GH_TOKEN"
        return "GH_ENTERPRISE_TOKEN"

    @property
    def config_dir_variable(self) -> str:
        return "GH_CONFIG_DIR" if self.backend == "github" else "GLAB_CONFIG_DIR"


def absolute_path_entries(path: str | None) -> list[str]:
    """The absolute, NUL-free entries of ``path``; ``.``, empty and relative entries are dropped."""
    seen: list[str] = []
    for entry in (path or "").split(os.pathsep):
        if entry and os.path.isabs(entry) and "\0" not in entry and entry not in seen:
            seen.append(entry)
    return seen


def child_path(path: str | None) -> str:
    return os.pathsep.join(absolute_path_entries(path)) or _FALLBACK_PATH


def resolve_executable(name: str, path: str | None, *, key: str = "gh_bin") -> str | None:
    """An absolute path for ``name``: itself if absolute, else the first executable file named so
    in an absolute ``PATH`` entry. None when nothing qualifies. A name with a separator that is
    not absolute is a config error (config validation allows only bare names and absolute paths).
    """
    if "\0" in name or not name:
        raise _error("config_invalid", f"{key}: not an executable name", details={"key": key})
    if os.path.isabs(name):
        return name if _is_executable(name) else None
    if os.sep in name or (os.altsep and os.altsep in name):
        raise _error(
            "config_invalid", f"{key}: use a bare name or an absolute path", details={"key": key}
        )
    for entry in absolute_path_entries(path):
        candidate = os.path.join(entry, name)
        if _is_executable(candidate):
            return candidate
    return None


def _is_executable(path: str) -> bool:
    try:
        return stat.S_ISREG(os.stat(path).st_mode) and os.access(path, os.X_OK)
    except OSError:
        return False


@dataclass(frozen=True)
class PrivateDirs:
    root: str  # mode 0700, under TMPDIR, unique
    cwd: str  # empty, mode 0700
    config: str  # empty, mode 0700 (created for token mode)


def _usable_dir(value: str | None) -> bool:
    return bool(
        value
        and "\0" not in value
        and os.path.isabs(value)
        and os.path.isdir(value)
        and os.access(value, os.W_OK | os.X_OK)
    )


def _tmpdir(source: Mapping[str, str]) -> str:
    """``TMPDIR`` from ``source`` only, if absolute, an existing directory and writable; else the
    fixed ``/tmp`` under the same checks. Never the process environment or the cwd."""
    value = source.get("TMPDIR")
    if _usable_dir(value):
        return value  # type: ignore[return-value]
    if _usable_dir(_FALLBACK_TMPDIR):
        return _FALLBACK_TMPDIR
    raise _error("internal", "no usable temporary directory: TMPDIR and /tmp are both unusable")


def _mkdir_private(path: str) -> None:
    os.mkdir(path, 0o700)
    os.chmod(path, 0o700)  # the umask cannot loosen it, a mask cannot tighten it past 0700


def make_private_dirs(source: Mapping[str, str], *, config: bool) -> PrivateDirs:
    base = os.path.realpath(_tmpdir(source))
    root = os.path.realpath(tempfile.mkdtemp(prefix="forgectl-", dir=base))
    try:
        os.chmod(root, 0o700)
        cwd = os.path.join(root, "cwd")
        _mkdir_private(cwd)
        config_dir = os.path.join(root, "config")
        if config:
            _mkdir_private(config_dir)
    except BaseException:
        remove_tree(root)
        raise
    return PrivateDirs(root=root, cwd=cwd, config=config_dir)


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_O_PATH = getattr(os, "O_PATH", 0)  # Linux: a handle that needs no permission on the dir itself


def _open_dir(name: str, dir_fd: int | None) -> tuple[int, bool]:
    """Open the real directory ``name`` (relative to ``dir_fd``) without following a symlink;
    returns the fd and whether it is an ``O_PATH`` handle (an unreadable dir, on Linux)."""
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dir_fd), False
    except PermissionError:
        if not _O_PATH:
            raise
    flags = _O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    return os.open(name, flags, dir_fd=dir_fd), True


def _chmod_dir(top: str, path: str) -> None:
    """Make ``path`` (``top`` or a path inside it) 0700 if it is a real directory, never by path:
    from an fd for ``top``, each component is opened relative to its parent with ``O_NOFOLLOW |
    O_DIRECTORY`` and the final fd is changed. A component that is a symlink or not a directory
    is skipped, so a symlink's target and anything outside ``top`` are never changed."""
    rel = os.path.relpath(path, top)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return
    parts = [] if rel == os.curdir else rel.split(os.sep)
    fds: list[int] = []
    try:
        fd, handle = _open_dir(top, None)
        fds.append(fd)
        for name in parts:
            fd, handle = _open_dir(name, fds[-1])
            fds.append(fd)
        if not handle:
            os.fchmod(fd, 0o700)
        elif os.path.isdir(f"/proc/self/fd/{fd}"):
            os.chmod(f"/proc/self/fd/{fd}", 0o700)  # the magic link is the opened dir itself
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
            raise
    finally:
        for opened in fds:
            os.close(opened)


def remove_tree(path: str) -> None:
    """Remove ``path`` (a directory this module made), without following symlinks. Never raises.

    An entry that cannot be removed is retried after making its directory (and, for a
    directory, itself) 0700; a directory that could not be opened or listed is then removed
    with the same handling. Only real directories inside ``path`` are changed: never a
    symlink's target and never the parent of ``path``."""
    top = os.path.normpath(path)
    descended: set[str] = set()

    def retry(function: Callable[..., Any], target: str, _exc: BaseException) -> None:
        with contextlib.suppress(Exception):
            target = os.path.normpath(target)
            if target != top and not target.startswith(top + os.sep):
                return
            if target != top:
                _chmod_dir(top, os.path.dirname(target))
            _chmod_dir(top, target)
            if function in (os.open, os.scandir):
                if target not in descended and os.path.isdir(target) and not os.path.islink(target):
                    descended.add(target)
                    shutil.rmtree(target, onexc=retry)
            elif function in (os.rmdir, os.unlink):
                function(target)

    with contextlib.suppress(Exception):
        shutil.rmtree(path, onexc=retry)


@contextlib.contextmanager
def private_dirs(source: Mapping[str, str], *, config: bool) -> Iterator[PrivateDirs]:
    dirs = make_private_dirs(source, config=config)
    try:
        yield dirs
    finally:
        in_flight = sys.exc_info()[1]
        held = _block_signals()
        try:
            remove_tree(dirs.root)
        finally:
            _restore_signals(held, in_flight)


def _home(source: Mapping[str, str]) -> str:
    home = source.get("HOME", "")
    if home and os.path.isabs(home) and "\0" not in home:
        return home
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return "/"


def _clean(value: str | None) -> str | None:
    return value if isinstance(value, str) and value and "\0" not in value else None


def build_environment(
    source: Mapping[str, str],
    *,
    dirs: PrivateDirs,
    backend: Backend | None,
    target: Target | None = None,
) -> dict[str, str]:
    """The child's environment, from scratch: nothing is inherited, only what is named here.

    ``target`` set means a forge call: token mode (``target.token``) adds the one token variable
    and the private config dir; stored-login mode adds the caller's config variables and, unless
    the caller named this backend's config dir, the session bus variables. Where the bus is not
    passed (token mode, or the caller's own config dir) ``DBUS_SESSION_BUS_ADDRESS`` is the fixed
    unusable ``NO_BUS_ADDRESS``. ``backend`` None (no forge program) keeps the bus unless the
    caller named either config dir. ``GITLAB_HOST`` carries the host and API prefix for any GitLab
    host but gitlab.com. ``GH_FORCE_TTY``, ``CLICOLOR_FORCE``, ``GH_REPO`` and ``GH_HOST`` are
    never set.
    """
    env: dict[str, str] = {
        "PATH": child_path(source.get("PATH")),
        "HOME": _home(source),
        "TMPDIR": os.path.realpath(_tmpdir(source)),
        "GIT_CEILING_DIRECTORIES": dirs.root,
    }
    for name in _PASSTHROUGH:
        value = _clean(source.get(name))
        if value is not None:
            env[name] = value
    env.update(FIXED_SETTINGS)
    if target is not None and target.token is not None:
        env[target.token_variable] = target.token
        env[target.config_dir_variable] = dirs.config
        env["DBUS_SESSION_BUS_ADDRESS"] = NO_BUS_ADDRESS
    else:
        for name in ("GH_CONFIG_DIR", "GLAB_CONFIG_DIR", "XDG_CONFIG_HOME"):
            value = _clean(source.get(name))
            if value is not None:
                env[name] = os.path.abspath(value)
        own = {"github": "GH_CONFIG_DIR", "gitlab": "GLAB_CONFIG_DIR"}.get(backend or "")
        # With no backend (a hook), the bus stays unless the caller set either config dir.
        named = (own in source and _clean(source.get(own))) if own else False
        if backend is None:
            named = any(_clean(source.get(v)) for v in ("GH_CONFIG_DIR", "GLAB_CONFIG_DIR"))
        if not named:
            for name in _BUS_VARIABLES:
                value = _clean(source.get(name))
                if value is not None:
                    env[name] = value
        else:
            env["DBUS_SESSION_BUS_ADDRESS"] = NO_BUS_ADDRESS
    if target is not None and target.backend == "gitlab" and target.host != "gitlab.com":
        env["GITLAB_HOST"] = target.host + target.api_prefix
    return env


# -- Process runner ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessResult:
    pid: int
    pgid: int
    returncode: int  # negative: killed by that signal
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False  # over the cap: the child was killed
    stderr_truncated: bool = False  # over the cap: the rest was discarded
    timed_out: bool = False  # the deadline passed: the group was killed
    streams_incomplete: bool = False  # a pipe stayed open after the leader exited


class Runner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        stdin: bytes | None,
        timeout: float,
        max_stdout: int,
        max_stderr: int,
        on_start: Callable[[int, int], None] | None,
        on_reap: Callable[[int, int], None] | None = None,
    ) -> ProcessResult: ...


def _signal_group(pgid: int, signum: int) -> None:
    """Signal the group this module created; only ever called while its leader is unreaped (so the
    group id cannot have been reused) or on a platform that cannot tell."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signum)


class _Leader:
    """The group leader: exit is observed without reaping it, so its PID stays reserved.

    Linux: ``waitid(WNOWAIT)`` and a pidfd to wait on. Without waitid (macOS): a kqueue
    ``EVFILT_PROC``/``NOTE_EXIT`` filter, which also leaves the leader unreaped. With neither,
    ``poll()`` reaps it, so its group is never signaled after that.
    """

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self.proc = proc
        self.pidfd: int | None = None
        self.kqueue: Any = None
        self.exited = False
        self.on_reap: Callable[[int, int], None] | None = None
        self.mode = "waitid" if _HAS_WAITID else "kqueue" if _HAS_KQUEUE else "poll"
        if self.mode == "waitid" and hasattr(os, "pidfd_open"):
            with contextlib.suppress(OSError):
                self.pidfd = os.pidfd_open(proc.pid)
        elif self.mode == "kqueue":
            kq: Any = select  # the kqueue names exist only where kqueue does
            self.kqueue = kq.kqueue()
            try:
                watch = kq.kevent(
                    proc.pid,
                    filter=kq.KQ_FILTER_PROC,
                    flags=kq.KQ_EV_ADD | kq.KQ_EV_ONESHOT,
                    fflags=kq.KQ_NOTE_EXIT,
                )
                self.kqueue.control([watch], 0, 0)
            except ProcessLookupError:  # it already exited (and is not reaped: we never waited)
                self.exited = True
            except BaseException:
                self.kqueue.close()
                raise

    @classmethod
    def bare(cls, proc: subprocess.Popen[bytes]) -> _Leader:
        """A leader that opens nothing: for cleanup when building the real one failed."""
        leader = cls.__new__(cls)
        leader.proc, leader.pidfd, leader.kqueue, leader.exited = proc, None, None, False
        leader.on_reap = None
        leader.mode = "waitid" if _HAS_WAITID else "poll"
        return leader

    @property
    def wait_fd(self) -> int | None:
        """A descriptor that becomes readable when the leader exits, if there is one."""
        if self.pidfd is not None:
            return self.pidfd
        return self.kqueue.fileno() if self.kqueue is not None else None

    def check(self) -> bool:
        if self.exited:
            return True
        if self.mode == "waitid":
            try:
                found = os.waitid(os.P_PID, self.proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                found = None
                self.exited = True
            self.exited = self.exited or found is not None
        elif self.mode == "kqueue" and self.kqueue is not None:
            self.exited = bool(self.kqueue.control(None, 1, 0))
        else:  # no waitid, no kqueue: polling reaps, so the group is not signaled afterwards
            self.exited = self.proc.poll() is not None
        return self.exited

    @property
    def reaped(self) -> bool:
        return self.proc.returncode is not None

    def stop_group(self) -> None:
        """SIGTERM the group, wait up to ``KILL_GRACE`` for the leader, then SIGKILL the group.

        The SIGKILL is sent however the wait ends (an exception from a signal handler included),
        as long as the leader is unreaped.
        """
        pgid = self.proc.pid
        if not self.reaped:
            _signal_group(pgid, signal.SIGTERM)
        try:
            end = time.monotonic() + KILL_GRACE
            while not self.check() and time.monotonic() < end:
                self._sleep(min(0.02, max(0.0, end - time.monotonic())))
        finally:
            if not self.reaped:
                _signal_group(pgid, signal.SIGKILL)

    def _sleep(self, seconds: float) -> None:
        if self.pidfd is not None:
            select.select([self.pidfd], [], [], seconds)
        elif self.kqueue is not None:
            if self.kqueue.control(None, 1, seconds):
                self.exited = True
        else:
            time.sleep(seconds)

    def reap(self) -> None:
        try:
            if self.on_reap is not None:  # the owner stops tracking the group while the PID is held
                try:
                    self.on_reap(self.proc.pid, self.proc.pid)
                finally:
                    self.proc.wait(timeout=REAP_WAIT)
            else:
                self.proc.wait(timeout=REAP_WAIT)
        except subprocess.TimeoutExpired:
            raise _error(
                "internal", f"child {self.proc.pid} did not exit after SIGKILL; not reaped"
            ) from None

    def close(self) -> None:
        if self.pidfd is not None:
            with contextlib.suppress(OSError):
                os.close(self.pidfd)
            self.pidfd = None
        if self.kqueue is not None:
            with contextlib.suppress(OSError):
                self.kqueue.close()
            self.kqueue = None


def _block_signals() -> Any:
    """Hold SIGINT, SIGTERM and SIGHUP (main thread only); returns the mask to restore."""
    if threading.current_thread() is not threading.main_thread():
        return None
    return signal.pthread_sigmask(signal.SIG_BLOCK, _HELD_SIGNALS)


def _restore_signals(old: Any, in_flight: BaseException | None = None) -> None:
    """Restore the mask; a signal held meanwhile is delivered now. If an exception is already on
    its way out, what that delivery raises is settled against it (see ``_settle``)."""
    if old is None:
        return
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)
    except BaseException as late:
        if in_flight is None or _settle(in_flight, late) is late:
            raise


def _settle(in_flight: BaseException, late: BaseException) -> BaseException:
    """Return the exception that goes on propagating when ``late`` is raised during the cleanup
    after ``in_flight``. A signal-derived ``late`` (any non-``Exception``) replaces an ordinary
    in-flight error, with that error as its context, so a signal is never lost; anything else is
    noted on ``in_flight``, which keeps propagating."""
    if isinstance(in_flight, Exception) and not isinstance(late, Exception):
        late.__context__ = in_flight
        return late
    with contextlib.suppress(Exception):
        in_flight.add_note(f"forgectl: during cleanup also: {type(late).__name__}: {late}")
    return in_flight


def _popen(
    argv: Sequence[str],
    env: Mapping[str, str],
    cwd: str,
    has_stdin: bool,
    old_mask: Any,
) -> subprocess.Popen[bytes]:
    def child_signals() -> None:  # the child starts with the caller's mask, not our spawn mask
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)

    return subprocess.Popen(
        list(argv),
        shell=False,
        stdin=subprocess.PIPE if has_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
        close_fds=True,
        start_new_session=True,  # a session and group leader: pgid == pid, shared with nobody
        preexec_fn=child_signals if old_mask is not None else None,  # noqa: PLW1509
    )


def _close_streams(proc: subprocess.Popen[bytes], selector: selectors.BaseSelector) -> None:
    with contextlib.suppress(OSError, ValueError):
        selector.close()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()


def _cleanup_after(
    exc: BaseException,
    proc: subprocess.Popen[bytes],
    leader: _Leader | None,
    selector: selectors.BaseSelector,
) -> BaseException:
    """End the group, close the pipes and reap the leader after ``exc``; never raises. Returns
    the exception to propagate: ``exc``, or a signal-derived one raised here (see ``_settle``)."""
    outgoing = exc
    if leader is None:
        leader = _Leader.bare(proc)
    try:
        leader.stop_group()
    except BaseException as late:
        outgoing = _settle(outgoing, late)
    _close_streams(proc, selector)
    try:
        leader.reap()
    except BaseException as late:
        outgoing = _settle(outgoing, late)
    finally:
        leader.close()
    return outgoing


def run_process(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str,
    stdin: bytes | None = None,
    timeout: float,
    max_stdout: int = MAX_STDOUT,
    max_stderr: int = MAX_STDERR,
    on_start: Callable[[int, int], None] | None = None,
    on_reap: Callable[[int, int], None] | None = None,
) -> ProcessResult:
    """Run ``argv`` (no shell) as the leader of a new process group and collect its output.

    At ``timeout`` seconds, or when stdout passes ``max_stdout``, the whole group gets SIGTERM and,
    after ``KILL_GRACE``, SIGKILL. Any exception raised while the child runs (a signal handler's
    ``SystemExit``, ``KeyboardInterrupt``) ends the group the same way, with SIGINT, SIGTERM and
    SIGHUP held until the leader is reaped, and propagates: a second signal or a failed reap is
    noted on it, except that a signal during the cleanup of an ordinary error is raised in its
    place (the error is its context), so a signal is never lost. Only the PID started here, and
    the group it leads, are ever signaled; the leader is reaped last. ``on_reap(pid, pgid)`` runs
    just before the reap, while the leader is still unreaped, so an owner that signals the group
    on its own can stop doing so before the number is free.
    """
    if not timeout > 0 or math.isinf(timeout):
        raise ValueError("timeout must be positive and finite")
    selector = selectors.DefaultSelector()  # before the spawn: nothing can fail between the two
    old_mask = _block_signals()
    try:
        proc = _popen(argv, env, cwd, stdin is not None, old_mask)
    except BaseException as exc:
        with contextlib.suppress(OSError, ValueError):
            selector.close()
        _restore_signals(old_mask, exc)
        raise
    leader: _Leader | None = None
    masked = True
    try:
        leader = _Leader(proc)
        leader.on_reap = on_reap
        masked = False
        _restore_signals(old_mask)  # a signal held during the spawn is delivered here, covered
        if on_start is not None:
            on_start(proc.pid, proc.pid)
        result = _collect(proc, leader, selector, stdin, timeout, max_stdout, max_stderr)
    except BaseException as exc:
        held = old_mask if masked else _block_signals()
        outgoing: BaseException = exc
        try:
            outgoing = _cleanup_after(exc, proc, leader, selector)
        finally:
            _restore_signals(held, outgoing)
        if outgoing is not exc:
            raise outgoing from exc
        raise
    _close_streams(proc, selector)
    leader.close()
    return result


def _collect(
    proc: subprocess.Popen[bytes],
    leader: _Leader,
    selector: selectors.BaseSelector,
    stdin: bytes | None,
    timeout: float,
    max_stdout: int,
    max_stderr: int,
) -> ProcessResult:
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None and proc.stderr is not None
    out, err = bytearray(), bytearray()
    flags = {"out_cut": False, "err_cut": False}
    pipes = 2
    pending = memoryview(stdin) if stdin else None
    for stream, tag in ((proc.stdout, "out"), (proc.stderr, "err")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, tag)
    if proc.stdin is not None:
        if pending:
            os.set_blocking(proc.stdin.fileno(), False)
            selector.register(proc.stdin, selectors.EVENT_WRITE, "in")
        else:
            proc.stdin.close()
    if leader.wait_fd is not None:
        selector.register(leader.wait_fd, selectors.EVENT_READ, "exit")
    exited_at: float | None = None
    timed_out = False
    while pipes or exited_at is None:
        now = time.monotonic()
        if now >= deadline:
            timed_out = True
            break
        wait = deadline - now
        if exited_at is not None:
            left = exited_at + DRAIN_AFTER_EXIT - now
            if left <= 0:
                break
            wait = min(wait, left)
        elif leader.wait_fd is None:
            wait = min(wait, 0.05)
        wait = min(wait, MAX_SELECT_WAIT)  # a selector takes a timeout of at most ~24.8 days
        for key, _events in selector.select(wait):
            tag = key.data
            if tag == "exit":
                selector.unregister(key.fileobj)
            elif tag == "in":
                assert pending is not None and proc.stdin is not None
                try:
                    sent = os.write(proc.stdin.fileno(), pending[:65536])
                    pending = pending[sent:]
                except BlockingIOError:
                    continue
                except OSError:  # the child closed its stdin: nothing more to send
                    pending = memoryview(b"")
                if not pending:
                    selector.unregister(key.fileobj)
                    proc.stdin.close()
            else:
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    selector.unregister(key.fileobj)
                    pipes -= 1
                elif tag == "out":
                    out += chunk
                    if len(out) > max_stdout:
                        flags["out_cut"] = True
                        del out[max_stdout:]
                        pipes = 0
                else:
                    err += chunk[: max(0, max_stderr - len(err))]
                    flags["err_cut"] = flags["err_cut"] or len(err) >= max_stderr
        if flags["out_cut"]:
            break
        if exited_at is None and leader.check():
            exited_at = time.monotonic()
    incomplete = bool(pipes) and not timed_out and not flags["out_cut"]
    if timed_out or flags["out_cut"]:
        leader.stop_group()
    else:
        # Whatever the leader left in its own group (a grandchild) dies with it.
        if not leader.reaped:
            _signal_group(proc.pid, signal.SIGKILL)
    leader.reap()
    code = proc.returncode
    return ProcessResult(
        pid=proc.pid,
        pgid=proc.pid,
        returncode=code,
        stdout=bytes(out),
        stderr=bytes(err),
        stdout_truncated=flags["out_cut"],
        stderr_truncated=flags["err_cut"],
        timed_out=timed_out,
        streams_incomplete=incomplete,
    )


@dataclass(frozen=True)
class Detached:
    pid: int
    pgid: int


class Spawner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        stdin: bytes,
        on_start: Callable[[int, int], None] | None,
        ack_timeout: float | None = None,
    ) -> Detached: ...


SPAWN_WRITE_TIMEOUT = 10.0  # seconds to hand a detached child its stdin
_DETACHED: list[subprocess.Popen[bytes]] = []  # started here, reaped here (never signaled)


class NoAcknowledgement(OSError):
    """``spawn_detached``: the child exited, or closed its stdout, or stayed silent past the
    bound, without writing its acknowledgement byte."""


def detached_children() -> list[subprocess.Popen[bytes]]:
    """The detached children not yet reaped; polls each, so a finished one is reaped."""
    _DETACHED[:] = [p for p in _DETACHED if p.poll() is None]
    return list(_DETACHED)


def spawn_detached(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str,
    stdin: bytes,
    on_start: Callable[[int, int], None] | None = None,
    ack_timeout: float | None = None,
) -> Detached:
    """Start ``argv`` (no shell) as the leader of a new session and hand it ``stdin``; then return
    without waiting. stderr goes to /dev/null, and so does stdout unless ``ack_timeout`` is
    given: then stdout is a pipe and the child must write one byte to it within ``ack_timeout``
    seconds of the hand-over, else ``NoAcknowledgement``. The child outlives this process, so it
    must enforce its own deadline. If handing over stdin or the acknowledgement fails, the
    child's group is killed (it is our unreaped child, so the id is ours) and the error is
    raised. Each call first reaps the earlier detached children that have exited."""
    detached_children()
    old_mask = _block_signals()
    try:
        proc = _popen_detached(argv, env, cwd, old_mask, ack=ack_timeout is not None)
    except BaseException as exc:
        _restore_signals(old_mask, exc)
        raise
    _DETACHED.append(proc)
    masked = True
    try:
        if on_start is not None:
            on_start(proc.pid, proc.pid)
        masked = False
        _restore_signals(old_mask)
        _hand_over(proc, stdin)
        if ack_timeout is not None:
            _await_ack(proc, ack_timeout)
    except BaseException:
        held = old_mask if masked else _block_signals()
        try:
            _signal_group(proc.pid, signal.SIGKILL)  # before the ack pipe closes: see _await_ack
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                proc.wait(timeout=REAP_WAIT)
        finally:
            _close_ack_pipe(proc)
            _restore_signals(held)
        raise
    _close_ack_pipe(proc)
    return Detached(proc.pid, proc.pid)


def _close_ack_pipe(proc: subprocess.Popen[bytes]) -> None:
    if proc.stdout is not None:
        with contextlib.suppress(OSError):
            proc.stdout.close()


def _popen_detached(
    argv: Sequence[str], env: Mapping[str, str], cwd: str, old_mask: Any, *, ack: bool
) -> subprocess.Popen[bytes]:
    def child_signals() -> None:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)

    return subprocess.Popen(
        list(argv),
        shell=False,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE if ack else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(env),
        cwd=cwd,
        close_fds=True,
        start_new_session=True,
        preexec_fn=child_signals if old_mask is not None else None,  # noqa: PLW1509
    )


def _hand_over(proc: subprocess.Popen[bytes], data: bytes) -> None:
    assert proc.stdin is not None
    fd = proc.stdin.fileno()
    os.set_blocking(fd, False)
    view = memoryview(data)
    deadline = time.monotonic() + SPAWN_WRITE_TIMEOUT
    try:
        while view:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("the detached child did not read its stdin")
            _, writable, _ = select.select([], [fd], [], left)
            if writable:
                try:
                    view = view[os.write(fd, view[:65536]) :]
                except BlockingIOError:
                    continue
    finally:
        with contextlib.suppress(OSError):
            proc.stdin.close()


def _await_ack(proc: subprocess.Popen[bytes], seconds: float) -> None:
    """Wait for the acknowledgement byte. The read end stays open: on failure the caller kills
    the group first, so a late acknowledgement can only succeed while the child is still ours
    to kill; a child whose write fails (the pipe was closed) runs nothing."""
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    ready, _, _ = select.select([fd], [], [], seconds)
    got = os.read(fd, 1) if ready else b""
    if not got:
        raise NoAcknowledgement("the detached child did not acknowledge its start")


def harden_process() -> bool:
    """``PR_SET_DUMPABLE=0`` on Linux, so a same-user process cannot read this process's memory
    (where a token lives). Returns whether it was applied."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE
    except (OSError, AttributeError):
        return False


# -- Budget -----------------------------------------------------------------------------------


class Budget:
    """The command's host-call time: the clock runs only inside gateway calls. After a write,
    ``WRITE_RESERVE`` seconds are added once, for the reads that verify it."""

    def __init__(self, seconds: float) -> None:
        self.total = float(seconds)
        self.spent = 0.0
        self._reserved = False

    def remaining(self) -> float:
        return max(0.0, self.total - self.spent)

    def grant_write_reserve(self) -> None:
        if not self._reserved:
            self._reserved = True
            self.total += WRITE_RESERVE

    @contextlib.contextmanager
    def spending(self) -> Iterator[None]:
        start = time.monotonic()
        try:
            yield
        finally:
            self.spent += time.monotonic() - start


# -- HTTP answers ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]  # lower-case names; repeated names are joined with ", "
    data: Any  # parsed JSON (None for an empty body) or the text, per ``parse``
    returncode: int


@dataclass(frozen=True)
class RateReading:
    """The rate-limit headers of one answer: ``x-ratelimit-remaining`` and
    ``x-ratelimit-reset`` (GitHub), or ``ratelimit-remaining`` and ``ratelimit-reset`` (GitLab),
    the reset as an RFC 3339 UTC time (null when absent or unusable)."""

    host: str
    remaining: int
    reset_at: str | None


_RATE_NUMBER = re.compile(r"[0-9]{1,12}", re.ASCII)


def rate_reading(host: str, headers: Mapping[str, str]) -> RateReading | None:
    """The reading of one answer's headers; None when it has no usable ``remaining``."""
    remaining = headers.get("x-ratelimit-remaining", headers.get("ratelimit-remaining"))
    if remaining is None or not _RATE_NUMBER.fullmatch(remaining.strip()):
        return None
    reset = headers.get("x-ratelimit-reset", headers.get("ratelimit-reset"))
    reset_at = None
    if reset is not None and _RATE_NUMBER.fullmatch(reset.strip()):
        try:
            moment = datetime.fromtimestamp(int(reset.strip()), UTC)
            reset_at = f"{moment.year:04d}-{moment:%m-%dT%H:%M:%S}Z"
        except (OverflowError, OSError, ValueError):
            reset_at = None
    return RateReading(host=host, remaining=int(remaining.strip()), reset_at=reset_at)


@dataclass(frozen=True)
class GraphQLResult:
    data: Mapping[str, Any]
    errors: tuple[Any, ...]  # empty unless ``allow_errors`` accepted a partial answer


def parse_http(stdout: bytes) -> tuple[int, dict[str, str], bytes] | None:
    """Split ``-i`` output into (status, headers, body); None when there is no status line or the
    header block is not well formed (a message is never searched for a status)."""
    newline = stdout.find(b"\n")
    if newline < 0:
        match = _STATUS_LINE.match(stdout.rstrip(b"\r"))
        newline = len(stdout)
    else:
        match = _STATUS_LINE.match(stdout[:newline].rstrip(b"\r"))
    if match is None:
        return None
    status = int(match.group(1))
    if not 100 <= status <= 599:
        return None
    headers: dict[str, str] = {}
    pos = newline + 1
    while True:
        end = stdout.find(b"\n", pos)
        if end < 0:  # no blank line closes the header block
            if pos >= len(stdout) and status in (204, 304):
                break
            return None
        line = stdout[pos:end].rstrip(b"\r")
        pos = end + 1
        if not line:
            break
        name, colon, value = line.partition(b":")
        if not colon or not name or name != name.strip():
            return None
        try:
            key = name.decode("ascii").lower()
            text = value.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
        headers[key] = f"{headers[key]}, {text}" if key in headers else text
    return status, headers, stdout[pos:]


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("number out of range")
    return value


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def parse_json(body: bytes) -> Any:
    """Strict JSON: UTF-8 without a BOM, no NaN or Infinity, no duplicate keys, no overflow.
    Raises ValueError. An empty body is None."""
    if not body.strip():
        return None
    try:
        return json.loads(
            body.decode("utf-8"),
            parse_constant=_no_constants,
            parse_float=_finite_float,
            object_pairs_hook=_no_duplicates,
        )
    except RecursionError as exc:
        raise ValueError("JSON nested too deeply") from exc


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    if re.fullmatch(r"[0-9]{1,9}", raw):
        return float(raw)
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def _redact(text: str, secret: str | None) -> str:
    """``text`` with the token and its percent-encoded forms replaced by ``[redacted]``."""
    # TODO(#7): match lowercase percent-encoded hex too (as hosts.redact_secrets does)
    if not secret or not text:
        return text
    forms = {
        secret,
        urllib.parse.quote(secret, safe=""),
        urllib.parse.quote(secret),
        urllib.parse.quote_plus(secret),
    }
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, "[redacted]")
    return text


def shown_text(text: str, secret: str | None, limit: int = MAX_MESSAGE) -> str:
    """Host- or child-derived text for a message: redacted first, then sanitized (one line,
    control characters marked), then cut, so a cut can never leave part of a token behind."""
    return sanitize_line(_redact(text, secret))[:limit]


def _host_message(body: bytes, secret: str | None) -> str:
    """The host's ``message`` string, redacted, sanitized and cut; empty if there is none."""
    try:
        doc = parse_json(body)
    except ValueError:
        return ""
    text = doc.get("message") if isinstance(doc, dict) else None
    if not isinstance(text, str):
        return ""
    return shown_text(text, secret)


def verify_identity(
    observed: object, *, host: str, project: str | None = None, secret: str | None = None
) -> None:
    """The answer's own URL must be on the expected host (and project, if one is given).

    Raises ``wrong_host_response``; ``details.observed`` is the URL redacted of ``secret`` (the
    call's token), sanitized and cut. This detects an accidental redirect, not a hostile host.
    """
    # TODO(#7): require https in html_url for github.com and *.ghe.com
    # TODO(#7): match the url path exactly, not by prefix
    ok = False
    shown = ""
    if isinstance(observed, str):
        shown = shown_text(observed, secret)
        try:
            parts = urllib.parse.urlsplit(observed)
        except ValueError:
            parts = None
        if (
            parts is not None
            and parts.scheme in ("http", "https")
            and "@" not in parts.netloc
            and parts.netloc.lower() == host.lower()
        ):
            path = urllib.parse.unquote(parts.path)
            ok = project is None or path == f"/{project}" or path.startswith(f"/{project}/")
    if not ok:
        raise _error(
            "wrong_host_response",
            f"the answer does not belong to {host}"
            + (f" and {project}" if project else "")
            + ": the token may have reached another host and should be rotated",
            details={"observed": shown},
        )


# -- The gateway ----------------------------------------------------------------------------


class ExecutableMissing(Exception):
    """``run_external``: the program is not on an absolute ``PATH`` entry."""

    def __init__(self, name: str) -> None:
        super().__init__(f"executable not found: {name}")
        self.name = name


@dataclass(frozen=True)
class CallScope:
    """What a caller that owns a read wants of every gateway built in its thread meanwhile.

    ``cancelled`` is checked before each host call (true: ``Cancelled``, nothing sent).
    ``on_spawn(pid, pgid)`` sees each child at launch, and ``on_done()`` runs when that call has
    ended and its child is reaped, so the caller knows which groups are alive. ``on_reap(pid,
    pgid)`` runs just before the runner reaps a child, while its PID is still held. The scope
    applies to the read paths only (``api`` and ``run_external``), never to ``spawn_detached``.
    """

    cancelled: Callable[[], bool] | None = None
    on_spawn: Callable[[int, int], None] | None = None
    on_done: Callable[[], None] | None = None
    on_reap: Callable[[int, int], None] | None = None


CALL_SCOPE: ContextVar[CallScope | None] = ContextVar("forgectl_call_scope", default=None)
"""Set by ``tui.source_live`` around one read on a worker thread; read when a Gateway is built."""


class Gateway:
    """One command's forge access: a budget, the configured executables and a runner.

    ``environ`` is the caller's environment (default ``os.environ``, read at each call).
    ``runner`` replaces the process runner (tests); ``on_spawn(pid, pgid)`` sees every child at
    launch so a test can record exactly the processes started.
    """

    def __init__(
        self,
        config: LoadedConfig,
        *,
        timeout: float | None = None,
        environ: Mapping[str, str] | None = None,
        runner: Runner = run_process,
        on_spawn: Callable[[int, int], None] | None = None,
        max_stdout: int = MAX_STDOUT,
        max_stderr: int = MAX_STDERR,
        spawner: Spawner | None = None,
    ) -> None:
        self.config = config
        self._spawner: Spawner = spawn_detached if spawner is None else spawner
        self.budget = Budget(config.timeout_for(None) if timeout is None else timeout)
        self._environ = environ
        self._runner = runner
        scope = CALL_SCOPE.get()
        self._cancelled = None if scope is None else scope.cancelled
        self._on_done = None if scope is None else scope.on_done
        self._on_reap = None if scope is None else scope.on_reap
        self._on_spawn = on_spawn  # the explicit one serves every spawn, including detached
        self._read_spawn = on_spawn if on_spawn is not None or scope is None else scope.on_spawn
        self._max_stdout = max_stdout
        self._max_stderr = max_stderr
        #: The rate-limit headers of every answer this command received, success or failure.
        self.rate_readings: list[RateReading] = []

    def _reap_kwargs(self) -> dict[str, Any]:
        """``on_reap`` for the runner, only when a scope asked (a plain runner may not take it)."""
        return {} if self._on_reap is None else {"on_reap": self._on_reap}

    @property
    def cancellable(self) -> bool:
        """Whether a caller's scope can cancel this gateway's reads."""
        return self._cancelled is not None

    def check_cancelled(self) -> None:
        """Raise ``Cancelled`` when the read this gateway serves was cancelled: called before
        each host call, so an abandoned read makes no further call."""
        if self._cancelled is None:
            return
        try:
            stop = self._cancelled()
        except Exception as exc:  # the caller's callable: its text may hold anything
            raise _error("internal", f"cancelled: {type(exc).__name__}") from exc
        if stop:
            raise Cancelled("cancelled")

    def lowest_rate(self, host: str | None = None) -> RateReading | None:
        """The reading with the lowest ``remaining`` (of ``host``, or of every host); the
        first seen wins a tie. None when no answer carried one."""
        readings = [r for r in self.rate_readings if host is None or r.host == host]
        return min(readings, key=lambda r: r.remaining) if readings else None

    @property
    def environ(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    # forge calls ------------------------------------------------------------------------

    def api(
        self,
        target: Target,
        method: str,
        segments: Sequence[str],
        *,
        query: Sequence[tuple[str, str]] = (),
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        write: bool = False,
        shape: Shape = "any",
        parse: Literal["json", "text", "bytes"] = "json",
        expect_status: frozenset[int] | None = None,
        passthrough: frozenset[int] = frozenset(),
        _graphql: bool = False,
    ) -> Response:
        """One ``gh api`` or ``glab api`` call.

        ``write`` marks a call that changes the host (never retried here; a failure after the
        request may have been sent is ``outcome_unknown``); every method but GET is one, except
        the GraphQL POST of a query. ``expect_status`` (required for a write) is the documented
        success status set: another 2xx is ``outcome_unknown``. gh and glab follow a 301, 302 or
        303 by sending a GET without the body and show only its answer, so a redirected write
        arrives here as that GET's answer. ``shape`` is the
        documented body: a list where one object is documented on a write is ``host_rejected``.
        ``passthrough`` lists non-2xx statuses the caller maps itself (409, 405...): they return
        as a ``Response`` instead of raising. ``parse="bytes"`` returns the body undecoded (a
        diff may hold any bytes; the caller decodes with ``sanitize_bytes``).
        """
        method = method.upper()
        if method not in _METHODS:
            raise ValueError(f"unusable method {method!r}")
        if write and method == "GET":
            raise ValueError("a write is not a GET")
        if write and expect_status is None:
            raise ValueError("a write names its documented success status (expect_status)")
        if not write and method != "GET" and not _graphql:
            raise ValueError(f"a {method} changes the host: pass write=True")
        if body is not None and method == "GET":
            raise ValueError("a GET has no body")
        endpoint = build_endpoint(segments, query)
        stdin = None if body is None else _encode_body(body)
        extra = _header_args(headers, has_body=body is not None)
        self.check_cancelled()  # before the token lookup, which may start a process
        exe = self._executable(target)
        argv = self._argv(exe, target, method, endpoint, extra, has_body=body is not None)
        remaining = self.budget.remaining()
        if remaining <= 0:
            raise _error("network_error", "the time budget ran out before the call; nothing sent")
        # A redirected write (a 301/302/303 the CLIs followed as a body-less GET) is caught by the
        # endpoint's documented status and proof, never by this call: the text posts check the
        # 201 and an integer id (backends/post_base.py ``created_id``), merge by step 9's re-read.
        # TODO(#29): PUT discussions must match the requested ``resolved``.
        # TODO(#27): DELETE draft notes and bulk_publish by read-back.
        with private_dirs(self.environ, config=target.token is not None) as dirs:
            env = build_environment(self.environ, dirs=dirs, backend=target.backend, target=target)
            try:
                with self.budget.spending():
                    result = self._runner(
                        argv,
                        env=env,
                        cwd=dirs.cwd,
                        stdin=stdin,
                        timeout=remaining,
                        max_stdout=self._max_stdout,
                        max_stderr=self._max_stderr,
                        on_start=self._read_spawn,
                        **self._reap_kwargs(),
                    )
            except (FileNotFoundError, PermissionError, NotADirectoryError):
                raise self._missing(target) from None
            finally:
                if write:
                    self.budget.grant_write_reserve()
                if self._on_done is not None:
                    self._on_done()
        return self._classify(
            result,
            target,
            endpoint,
            write=write,
            shape=shape,
            parse=parse,
            expect_status=expect_status,
            passthrough=passthrough,
            graphql=_graphql,
        )

    def graphql(
        self,
        target: Target,
        query: str,
        variables: Mapping[str, Any] | None = None,
        *,
        write: bool = False,
        mutation: str | None = None,
        allow_errors: bool = False,
    ) -> GraphQLResult:
        """A GraphQL call: ``query`` is a constant text, values travel as ``variables``, the body
        goes by stdin and ``--jq`` is never used (``gh`` exits 1 on any ``errors`` and the whole
        body is still parsed).

        A failure is top-level ``errors`` (typed by ``type``: ``RATE_LIMITED``, ``NOT_FOUND``,
        ``FORBIDDEN``, else ``host_rejected``), a non-empty ``errors`` in the mutation's payload,
        or a null mutation result, whatever the HTTP status. ``allow_errors`` (reads only) returns
        a partial answer with its errors instead of raising.
        """
        if allow_errors and write:
            raise ValueError("allow_errors is for reads")
        if write and not mutation:
            raise ValueError("a GraphQL write names its mutation field (its proof)")
        if not isinstance(query, str) or not query:
            raise ValueError("a GraphQL query is a non-empty constant string")
        document = {"query": query, "variables": dict(variables or {})}
        response = self.api(
            target,
            "POST",
            ["graphql"],
            body=document,
            write=write,
            shape="object",
            expect_status=frozenset({200}) if write else None,
            _graphql=True,
        )
        body = response.data
        secret = target.token
        errors = body.get("errors")
        failed = errors is not None and errors != []  # present, not null, not an empty list
        data = body.get("data")
        result = data.get(mutation) if mutation is not None and isinstance(data, dict) else None
        if failed:
            if write and result is not None:  # a result and errors: it may have landed
                raise _error(
                    "outcome_unknown", "the mutation answered a result together with errors"
                )
            if not isinstance(errors, list):  # not GraphQL's shape: never a partial answer
                raise _error(
                    "host_rejected",
                    "GraphQL: the host answered errors that are not a list",
                    details={"status": response.status},
                )
            if allow_errors:
                if not isinstance(data, dict):
                    raise _error("upstream_error", "the host answered errors and no data")
                return GraphQLResult(data=data, errors=tuple(errors))
            raise _graphql_error(errors, response.headers, response.status, secret)
        if not isinstance(data, dict):
            raise self._bad_answer(write, "the host answered no data")
        if mutation is not None:
            if result is None:
                raise self._bad_answer(write, "the mutation answered a null result")
            payload_errors = result.get("errors") if isinstance(result, dict) else None
            if payload_errors not in (None, []):
                raise _error(
                    "host_rejected",
                    "the host refused the mutation: " + _graphql_text(payload_errors, secret),
                    details={"status": response.status},
                )
        return GraphQLResult(data=data, errors=())

    def _bad_answer(self, write: bool, message: str) -> ForgectlError:
        if write:
            return _error("outcome_unknown", message + "; the write may have landed")
        return _error("upstream_error", message)

    # internals --------------------------------------------------------------------------

    def _executable(self, target: Target) -> str:
        config = self.config.config
        name = config.gh_bin if target.backend == "github" else config.glab_bin
        key = "gh_bin" if target.backend == "github" else "glab_bin"
        found = resolve_executable(name, self.environ.get("PATH"), key=key)
        if found is None:
            raise self._missing(target)
        return found

    def _missing(self, target: Target) -> ForgectlError:
        config = self.config.config
        name = config.gh_bin if target.backend == "github" else config.glab_bin
        return _error(
            "tool_missing",
            f"{name} was not found on an absolute PATH entry (it is needed for {target.host})",
            details={"install": _INSTALL[target.program]},
        )

    @staticmethod
    def _argv(
        exe: str,
        target: Target,
        method: str,
        endpoint: str,
        headers: list[str],
        *,
        has_body: bool,
    ) -> list[str]:
        argv = [exe, "api"]
        if target.backend == "github" or target.host == "gitlab.com":
            argv += ["--hostname", target.host]
        argv += ["-i", "-X", method]
        if has_body:
            argv += ["--input", "-"]
        argv += headers
        argv += ["--", endpoint]
        return argv

    def _classify(
        self,
        result: ProcessResult,
        target: Target,
        endpoint: str,
        *,
        write: bool,
        shape: Shape,
        parse: Literal["json", "text", "bytes"],
        expect_status: frozenset[int] | None,
        passthrough: frozenset[int],
        graphql: bool,
    ) -> Response:
        secret = target.token
        where = f"{target.host}/{endpoint.partition('?')[0]}"

        def failure(read_code: str, message: str, **kwargs: Any) -> ForgectlError:
            """A transport or protocol failure: a write may have landed."""
            if write:
                return _error("outcome_unknown", f"{message}; the write may have landed")
            return _error(read_code, message, **kwargs)

        if result.timed_out:
            raise failure("network_error", f"{where}: no answer within the time budget")
        if result.stdout_truncated and not write:
            raise AnswerCut("upstream_error", f"{where}: the answer was larger than the read cap")
        if result.stdout_truncated or result.streams_incomplete:
            raise failure("upstream_error", f"{where}: the answer was too large or never ended")
        parsed = parse_http(result.stdout)
        if parsed is None:
            if target.backend == "github" and result.returncode == 4 and not result.stdout:
                raise _error("auth_required", f"{target.host}: no credential for gh (it exited 4)")
            if (
                target.backend == "gitlab"
                and target.token is None
                and result.returncode != 0
                and b"Unauthenticated." in result.stderr
            ):  # glab 1.89 with no credential: it sent nothing
                raise _error(
                    "auth_required",
                    f"{target.host}: no credential for glab (it answered Unauthenticated.)",
                )
            note = shown_text(
                result.stderr.decode("utf-8", errors="replace").strip(), secret, limit=200
            )
            raise failure(
                "network_error",
                f"{where}: no HTTP status in the answer (exit {result.returncode})"
                + (f": {note}" if note else ""),
            )
        status, headers, raw = parsed
        reading = rate_reading(target.host, headers)
        if reading is not None:
            self.rate_readings.append(reading)
        if status < 200:  # an informational status is never the answer
            raise failure("upstream_error", f"{where}: only an informational status {status}")
        if status in passthrough:
            return Response(status, headers, _decode(raw, parse, tolerant=True), result.returncode)
        if 200 <= status < 300:
            ok_exit = result.returncode == 0 or (graphql and result.returncode == 1)
            if not ok_exit:
                raise failure("upstream_error", f"{where}: {status} but exit {result.returncode}")
            if write and expect_status is not None and status not in expect_status:
                raise _error(
                    "outcome_unknown",
                    f"{where}: unexpected status {status}; the write may have landed",
                )
            try:
                data = _decode(raw, parse, tolerant=False)
            except ValueError:
                raise failure(
                    "upstream_error", f"{where}: the answer is not valid UTF-8 JSON"
                ) from None
            self._check_shape(data, shape, where, status, write=write)
            return Response(status, headers, data, result.returncode)
        raise self._status_error(status, headers, raw, where, target, write=write)

    @staticmethod
    def _check_shape(data: Any, shape: Shape, where: str, status: int, *, write: bool) -> None:
        if shape == "any":
            return
        right = isinstance(data, dict) if shape == "object" else isinstance(data, list)
        if right:
            return
        if write and shape == "object" and isinstance(data, list):
            raise _error(
                "host_rejected",
                f"{where}: a list came back where one object is documented (a redirect?)",
                details={"status": status},
            )
        if write:
            raise _error(
                "outcome_unknown", f"{where}: no {shape} in the answer; it may have landed"
            )
        raise _error("upstream_error", f"{where}: expected a JSON {shape}")

    @staticmethod
    def _status_error(
        status: int,
        headers: Mapping[str, str],
        raw: bytes,
        where: str,
        target: Target,
        *,
        write: bool,
    ) -> ForgectlError:
        # TODO(#7): attach retry_after to retried 5xx read errors so reads honor retry-after on
        # 5xx (interface.md Reliability)
        host_text = _host_message(raw, target.token)
        suffix = f": {host_text}" if host_text else ""
        retry = _retry_after(headers)
        rate = status == 429 or (
            status == 403 and ("retry-after" in headers or bool(_RATE_WORDING.search(host_text)))
        )
        if rate:
            return _error(
                "rate_limited", f"{where}: rate limited ({status}){suffix}", retry_after=retry
            )
        if status == 401:
            return _error("auth_required", f"{target.host} rejected the credential (401){suffix}")
        if status == 403:
            return _error("permission_denied", f"{where}: forbidden (403){suffix}")
        if status == 404:
            return _error("not_found", f"{where}: not found (404)")
        if status >= 500:
            if write:
                return _error(
                    "outcome_unknown", f"{where}: host error {status}; it may have landed"
                )
            return _error("upstream_error", f"{where}: host error {status}{suffix}")
        return _error(
            "host_rejected",
            f"{where}: the host answered {status}{suffix}",
            details={"status": status},
        )

    # other processes --------------------------------------------------------------------

    def run_external(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        mode: ExternalMode,
        backend: Backend | None = None,
        token_env_names: Collection[str] = (),
        extra_env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        max_stdout: int = MAX_STDOUT,
        max_stderr: int = MAX_STDERR,
    ) -> ProcessResult:
        """The second entry point: hooks, ``token_command`` and the browser.

        Same process rules as a forge call (no shell, its own process group, the deadline kill of
        that group, a private empty cwd removed afterwards), without the forge parsing or the
        budget. The environment depends on ``mode``:

        - ``hook`` and ``browser``: forgectl's environment minus every token variable: the
          ambient list, each configured ``token_env`` and each name in ``token_env_names``
          (interface: Hooks; TUI). The browser keeps DISPLAY, BROWSER and the like.
        - ``token_command``: the stored-login environment built from scratch for ``backend``
          (required), bus rule included (interface: Credentials).

        ``extra_env`` adds variables (``FORGECTL_HOOK`` and the like) but never a token variable,
        ``GH_HOST``, ``GH_REPO``, ``GH_FORCE_TTY``, ``CLICOLOR_FORCE`` or a NUL; any of those is a
        programming error (``ValueError``). Raises ``ExecutableMissing`` when ``argv[0]`` is not on
        an absolute ``PATH`` entry.
        """
        self.check_cancelled()
        if not argv or not all(isinstance(a, str) and "\0" not in a for a in argv):
            raise ValueError("argv must be non-empty strings without NUL")
        if mode not in ("hook", "browser", "token_command"):
            raise ValueError(f"unknown mode {mode!r}")
        if mode == "token_command" and backend is None:
            raise ValueError("a token_command runs for one backend: pass backend")
        inherited = self.inherited_environment(token_env_names, extra_env)
        exe = resolve_executable(argv[0], self.environ.get("PATH"), key="command")
        if exe is None:
            raise ExecutableMissing(argv[0])
        with private_dirs(self.environ, config=False) as dirs:
            if mode == "token_command":
                env = build_environment(self.environ, dirs=dirs, backend=backend)
            else:
                env = dict(inherited)
            env.update(extra_env or {})
            try:
                return self._runner(
                    [exe, *argv[1:]],
                    env=env,
                    cwd=dirs.cwd,
                    stdin=stdin,
                    timeout=timeout,
                    max_stdout=max_stdout,
                    max_stderr=max_stderr,
                    on_start=self._read_spawn,
                    **self._reap_kwargs(),
                )
            except (FileNotFoundError, PermissionError, NotADirectoryError):
                raise ExecutableMissing(argv[0]) from None
            finally:
                if self._on_done is not None:
                    self._on_done()

    def inherited_environment(
        self, token_env_names: Collection[str] = (), extra_env: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """forgectl's environment minus every token variable (the hook and browser environment).

        ``extra_env`` is checked here (``ValueError`` for a NUL, a token variable, ``GH_HOST``,
        ``GH_REPO``, ``GH_FORCE_TTY`` or ``CLICOLOR_FORCE``) and then added on top.
        """
        withheld = self._token_names(token_env_names)
        for name, value in (extra_env or {}).items():
            if "\0" in name or "\0" in value:
                raise ValueError("extra_env holds a NUL")
            if name in withheld or name in _NEVER_EXTRA:
                raise ValueError(f"extra_env may not set {name}")
        env = {
            k: v
            for k, v in self.environ.items()
            if k not in withheld and "\0" not in k and "\0" not in v
        }
        env.update(extra_env or {})
        return env

    def spawn_detached(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: str,
        stdin: bytes,
        ack_timeout: float | None = None,
    ) -> Detached:
        """Start ``argv`` in its own session, outliving this process (the async hook watchdog);
        ``ack_timeout`` as for the module's ``spawn_detached``."""
        return self._spawner(
            argv, env=env, cwd=cwd, stdin=stdin, on_start=self._on_spawn, ack_timeout=ack_timeout
        )

    def _token_names(self, extra: Collection[str]) -> frozenset[str]:
        """Every variable that may hold a token: the ambient list, each configured
        ``token_env`` and the names the caller passes."""
        configured = {h.token_env for h in self.config.config.hosts.values() if h.token_env}
        return frozenset({*TOKEN_VARIABLES, *configured, *extra})


def _header_args(headers: Mapping[str, str] | None, *, has_body: bool) -> list[str]:
    merged: dict[str, str] = {}
    if has_body:
        merged["Content-Type"] = "application/json"
    for name, value in (headers or {}).items():
        if not _HEADER_NAME.match(name) or name.lower() in _FORBIDDEN_HEADERS:
            raise ValueError(f"unusable header name {name!r}")
        if not _HEADER_VALUE.match(value):
            raise ValueError(f"unusable value for header {name}")
        merged[name] = value
    args: list[str] = []
    for name, value in merged.items():
        args += ["-H", f"{name}: {value}"]
    return args


def _encode_body(body: Any) -> bytes:
    try:
        return json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except UnicodeEncodeError as exc:
        raise ValueError("body holds a lone surrogate") from exc


def _decode(raw: bytes, parse: Literal["json", "text", "bytes"], *, tolerant: bool) -> Any:
    if parse == "bytes":  # the caller decodes and sanitizes (a diff may hold any bytes)
        return raw
    try:
        return raw.decode("utf-8") if parse == "text" else parse_json(raw)
    except ValueError:  # UnicodeDecodeError included
        if tolerant:  # a pass-through status: the caller reads what it can
            return None
        raise


def _as_list(errors: Any) -> list[Any]:
    return errors if isinstance(errors, list) else [errors]


def _graphql_text(errors: Any, secret: str | None) -> str:
    parts: list[str] = []
    for item in _as_list(errors)[:3]:
        message = item.get("message") if isinstance(item, dict) else item
        parts.append(shown_text(str(message), secret, limit=100))
    return "; ".join(parts)


def _graphql_error(
    errors: Any, headers: Mapping[str, str], status: int, secret: str | None
) -> ForgectlError:
    types = {item.get("type") for item in _as_list(errors) if isinstance(item, dict)}
    text = _graphql_text(errors, secret)
    if "RATE_LIMITED" in types:
        return _error(
            "rate_limited", f"GraphQL: rate limited: {text}", retry_after=_retry_after(headers)
        )
    if "NOT_FOUND" in types:
        return _error("not_found", f"GraphQL: not found: {text}")
    if "FORBIDDEN" in types:
        return _error("permission_denied", f"GraphQL: forbidden: {text}")
    return _error("host_rejected", f"GraphQL: {text}", details={"status": status})


def start(
    argv: list[str], *, env: dict[str, str], cwd: str | None = None
) -> subprocess.Popen[bytes]:
    """Start ``argv`` detached from the terminal and return at once, without a shell.

    This is the browser opener's entry point: the opener may stay alive as the browser, so it
    runs in its own session, outside the process-group kill ``run_external`` applies at its
    deadline, and is never killed. ``env`` is the caller's explicit allowlist (no token variable
    is ever built into it here). The caller owns the returned process: it records ``pid`` and
    signals only that exact process, never by name or pattern.
    """
    return subprocess.Popen(
        argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
