"""Hosts and credentials (interface: Hosts, Credentials; Stage 0).

``Hosts`` turns a host name into the gateway's ``Target``: the backend and API prefix from the
config, and exactly the credential configured for that host, in this order: ``token_env``,
``token_command``, then the stored login of ``gh`` or ``glab`` only when neither is configured. A
configured credential that yields nothing is ``auth_required`` and never falls back to the stored
login. A token found for one host is held only in that host's ``Target``; no other host's call
can carry it.

``token_command`` runs through ``Gateway.run_external`` (no shell, ``/dev/null`` stdin, its own
process group, a private empty cwd) once per ``Hosts`` (so once per process) and host. Its output
must be one line of printable ASCII with no space. Neither its output nor its stderr is ever
shown, logged or kept: a failure names only a fixed reason.

Every token in use is also registered here by value (``redact_secrets``), so the log can mask a
token of any shape.
"""

from __future__ import annotations

import re
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from .activity import Viewer
from .errors import ForgectlError, refused
from .gateway import (
    MAX_TOKEN,
    Backend,
    ExecutableMissing,
    Gateway,
    ProcessResult,
    Target,
    verify_identity,
)
from .identifiers import backend_of, bind_login

TOKEN_COMMAND_TIMEOUT = 10.0  # seconds
MAX_SCOPES = 64
MAX_SCOPE_CHARS = 128

TokenSource = Literal["token_env", "token_command", "stored_login", "none"]

_TOKEN = re.compile(rf"[\x21-\x7e]{{1,{MAX_TOKEN}}}")
_SCOPE = re.compile(rf"[\x21-\x7e]{{1,{MAX_SCOPE_CHARS}}}")
_PERCENT = re.compile(r"%[0-9A-F]{2}")
MARKER = "[redacted]"


# -- tokens in use, by value ---------------------------------------------------------------

_secrets: set[str] = set()
_secrets_lock = threading.Lock()


def register_secret(token: str) -> None:
    """Remember a token in use so ``redact_secrets`` can mask it whatever its shape."""
    if token:
        with _secrets_lock:
            _secrets.add(token)


def secret_values() -> frozenset[str]:
    with _secrets_lock:
        return frozenset(_secrets)


def clear_secrets() -> None:
    """For tests: forget every registered token."""
    with _secrets_lock:
        _secrets.clear()


def _pattern(form: str) -> str:
    """``form`` as a regex: literal, except that each ``%XX`` matches its hex digits in either
    case (``%2f`` is ``%2F``)."""
    parts: list[str] = []
    last = 0
    for match in _PERCENT.finditer(form):
        parts.append(re.escape(form[last : match.start()]))
        hex_digits = match[0][1:]
        parts.append("%" + "".join(f"[{c}{c.lower()}]" if c.isalpha() else c for c in hex_digits))
        last = match.end()
    parts.append(re.escape(form[last:]))
    return "".join(parts)


def _forms(secret: str) -> set[str]:
    return {
        secret,
        urllib.parse.quote(secret, safe=""),
        urllib.parse.quote(secret),
        urllib.parse.quote_plus(secret),
    }


# TODO(#9): jsonlog.scrub_text must call redact_secrets on raw strings before JSON serialization
def redact_secrets(text: str) -> str:
    """``text`` with every registered token, and its percent-encoded forms (hex digits in either
    case), as ``[redacted]``. Every match of every form is found on the original text, and
    overlapping or adjacent matches are merged, so one token cannot leave part of another behind
    and a token can never match inside the marker. Other transformations (base64, JSON
    escaping) are not caught."""
    secrets = secret_values()
    if not text or not secrets:
        return text
    spans: list[tuple[int, int]] = []
    for form in {form for secret in secrets for form in _forms(secret)}:
        # a lookahead finds every start, so overlapping occurrences of one form count too
        for match in re.finditer(f"(?=({_pattern(form)}))", text):
            spans.append((match.start(1), match.end(1)))
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:  # overlapping or adjacent
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out: list[str] = []
    last = 0
    for start, end in merged:
        out.append(text[last:start])
        out.append(MARKER)
        last = end
    out.append(text[last:])
    return "".join(out)


# -- results -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Credential:
    source: Literal["token_env", "token_command", "stored_login"]
    token: str | None = field(default=None, repr=False)  # None for the stored login
    variable: str | None = None  # the configured token_env name, if any


@dataclass(frozen=True)
class User:
    login: str
    id: str
    scopes: tuple[str, ...] | None


@dataclass(frozen=True)
class Identity:
    """One ``whoami`` entry for a host, plus the error that goes in ``errors`` if there is one."""

    host: str
    backend: Backend
    token_source: TokenSource
    login: str | None = None
    id: str | None = None
    me: str | None = None
    me_matches_login: bool | None = None
    scopes: tuple[str, ...] | None = None
    error: dict[str, str] | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "backend": self.backend,
            "login": self.login,
            "id": self.id,
            "me": self.me,
            "me_matches_login": self.me_matches_login,
            "token_source": self.token_source,
            "scopes": None if self.scopes is None else list(self.scopes),
        }


def matches_login(me: str | None, login: str | None) -> bool | None:
    """Whether ``me`` names the authenticated account; None when either is unknown. Logins
    compare case-insensitively (no id exists for a configured ``me``)."""
    if me is None or login is None:
        return None
    return me.lower() == login.lower()


# -- hosts ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Failure:
    """A remembered credential failure: only its fixed text, never the error object (whose
    traceback could keep a child's output alive). Raised afresh each time."""

    code: str
    message: str
    details: dict[str, Any] | None

    def error(self) -> ForgectlError:
        details = None if self.details is None else dict(self.details)
        return ForgectlError(self.code, self.message, details=details)


def _ascii_host(host: str) -> str:
    """``host`` lowercased; a non-ASCII host is refused first, since lowercasing could turn it
    into a declared one (the Kelvin sign becomes ``k``)."""
    if not host.isascii():
        raise refused("unknown_host", "a host that is not ASCII is not built in or declared")
    return host.lower()


def _guidance(host: str, backend: Backend) -> str:
    program = "gh" if backend == "github" else "glab"
    return (
        f"; set hosts.{host}.token_env to a variable holding the token, or log in with"
        f" {program} and remove the configured credential"
    )


class Hosts:
    """Credentials, targets and identity for one command, over one gateway."""

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway
        self._credentials: dict[str, Credential | _Failure] = {}
        self._viewers: dict[str, Viewer] = {}
        self._lock = threading.Lock()  # select-and-store: a token_command runs once per host

    @property
    def _config(self) -> Any:
        return self.gateway.config.config

    def backend(self, host: str) -> Backend:
        """The backend of a built-in or declared host; any other host is ``unknown_host``."""
        return backend_of(_ascii_host(host), self._config)

    def target(self, host: str) -> Target:
        """The gateway target for ``host``, carrying only this host's own credential."""
        host = _ascii_host(host)
        backend = self.backend(host)
        entry = self._config.hosts.get(host)
        prefix = entry.api_prefix if entry is not None and entry.api_prefix else ""
        credential = self.credential(host)
        return Target(backend, host, api_prefix=prefix, token=credential.token)

    def credential(self, host: str) -> Credential:
        """This host's credential, selected once per ``Hosts``. A failure is remembered and
        raised afresh each time, except a ``network_error`` (the time budget ran out), which
        says nothing about the credential."""
        host = _ascii_host(host)
        self.backend(host)  # an unknown host never reaches a credential
        with self._lock:
            if host not in self._credentials:
                outcome: Credential | _Failure
                try:
                    outcome = self._select(host)
                except ForgectlError as error:
                    outcome = _Failure(error.code, error.message, error.details)
                if isinstance(outcome, _Failure) and outcome.code == "network_error":
                    raise outcome.error()  # outside the except: no context, nothing kept
                self._credentials[host] = outcome
            outcome = self._credentials[host]
        if isinstance(outcome, _Failure):
            raise outcome.error()
        return outcome

    # selection --------------------------------------------------------------------------

    def _select(self, host: str) -> Credential:
        entry = self._config.hosts.get(host)
        env_name = entry.token_env if entry is not None else None
        command = entry.token_command if entry is not None else None
        if env_name is None and command is None:
            return Credential("stored_login")
        details = {"variable": env_name} if env_name is not None else None
        guidance = _guidance(host, self.backend(host))
        if env_name is not None:
            value = self.gateway.environ.get(env_name)
            if value:
                if not _TOKEN.fullmatch(value):
                    raise ForgectlError(
                        "auth_required",
                        f"hosts.{host}.token_env: the value of {env_name} is not one line of"
                        " printable ASCII without spaces; it was not used" + guidance,
                        details=details,
                    )
                register_secret(value)
                return Credential("token_env", value, env_name)
        if command is not None:
            token = self._run_command(host, tuple(command), details, guidance)
            register_secret(token)
            return Credential("token_command", token, env_name)
        raise ForgectlError(
            "auth_required",
            f"hosts.{host}.token_env: {env_name} is unset or empty" + guidance,
            details=details,
        )

    def _run_command(
        self, host: str, command: tuple[str, ...], details: dict[str, Any] | None, guidance: str
    ) -> str:
        budget = self.gateway.budget
        remaining = budget.remaining()
        if remaining <= 0:
            raise ForgectlError("network_error", "the time budget ran out before the call")
        argv = [host if arg == "{host}" else arg for arg in command]
        limit = min(TOKEN_COMMAND_TIMEOUT, remaining)
        reason: str | None
        token: str | None
        try:
            with budget.spending():
                result = self.gateway.run_external(
                    argv,
                    timeout=limit,
                    mode="token_command",
                    backend=self.backend(host),
                    max_stdout=MAX_TOKEN + 2,
                )
        except ExecutableMissing:
            reason, token = "program not found on an absolute PATH entry", None
        else:
            cut_short = result.timed_out and limit < TOKEN_COMMAND_TIMEOUT
            reason, token = _command_token(result)
            del result  # a failure keeps no reference to the child's output
            if cut_short:  # the budget, not the 10 s limit: says nothing about the credential
                raise ForgectlError(
                    "network_error",
                    f"the time budget ran out while hosts.{host}.token_command ran",
                )
        if reason is not None or token is None:
            raise ForgectlError(
                "auth_required",
                f"hosts.{host}.token_command {reason}{guidance}",
                details=details,
            )
        return token

    # me ---------------------------------------------------------------------------------

    def me(self, host: str) -> str | None:
        """``hosts.<host>.me``, else ``me.github`` or ``me.gitlab``, else None (the caller then
        uses the authenticated user). Checked under the backend's own login grammar (config
        load checks it too; this is the backstop)."""
        host = _ascii_host(host)
        backend = self.backend(host)
        entry = self._config.hosts.get(host)
        value = entry.me if entry is not None else None
        key = f"hosts.{host}.me"
        if value is None:
            value = getattr(self._config.me, backend)
            key = f"me.{backend}"
        if value is None:
            return None
        try:
            return bind_login(value, backend)
        except ForgectlError:
            raise ForgectlError(
                "config_invalid",
                f"{key}: not a valid {backend} login",
                details={"key": key},
            ) from None

    def ignore_authors(self) -> tuple[str, ...]:
        """``activity.ignore_authors``: the config list alone decides who is ignored."""
        return tuple(self._config.activity.ignore_authors)

    def viewer(
        self, host: str, *, call: Callable[[Callable[[], User]], User] | None = None
    ) -> Viewer:
        """Me on ``host`` for ``activity``: the authenticated account, read once per ``Hosts``
        (one command run) however many snapshots follow. A configured ``me`` that names another
        login than the authenticated one wins and has no id. A failed read raises and is not
        remembered, so the next snapshot tries again."""
        host = _ascii_host(host)
        with self._lock:
            known = self._viewers.get(host)
        if known is None:
            target = self.target(host)
            user = (call or _once)(lambda: self.authenticated_user(target))
            configured = self.me(host)
            if configured is not None and not matches_login(configured, user.login):
                known = Viewer(login=configured, id=None)
            else:
                known = Viewer(login=user.login, id=user.id)
            with self._lock:
                known = self._viewers.setdefault(host, known)
        return known

    # identity ---------------------------------------------------------------------------

    def authenticated_user(self, target: Target) -> User:
        """``GET /user`` as the account this target authenticates. The answer must belong to the
        target's host (``wrong_host_response`` otherwise)."""
        response = self.gateway.api(target, "GET", ["user"], shape="object")
        data = response.data
        github = target.backend == "github"
        # The host first: a foreign answer of any shape is wrong_host_response (rotate the token).
        verify_identity(
            data.get("html_url" if github else "web_url"), host=target.host, secret=target.token
        )
        login = data.get("login" if github else "username")
        account = data.get("id")
        if (
            not isinstance(login, str)
            or isinstance(account, bool)
            or not isinstance(account, int)
            or account < 1
        ):
            raise ForgectlError("upstream_error", "the identity answer is not the documented shape")
        try:
            bind_login(login, target.backend)
        except ForgectlError:
            raise ForgectlError(
                "upstream_error", "the identity answer holds an unusable login"
            ) from None
        scopes = _scopes(response.headers.get("x-oauth-scopes")) if github else None
        return User(login=login, id=str(account), scopes=scopes)

    def identity(
        self, host: str, *, call: Callable[[Callable[[], User]], User] | None = None
    ) -> Identity:
        """The ``whoami`` entry for ``host``. An unknown host raises; a failed credential or read
        is returned in ``error`` (``{host, code, message}``). ``call`` wraps the ``GET /user``
        read (a command passes ``ReadEnv.call``, so the read is retried like any other)."""
        host = _ascii_host(host)
        backend = self.backend(host)
        try:
            credential = self.credential(host)
        except ForgectlError as error:
            return self._failed(host, backend, "none", error)
        source: TokenSource = credential.source
        try:
            target = self.target(host)
            user = (call or _once)(lambda: self.authenticated_user(target))
        except ForgectlError as error:
            if credential.source == "stored_login":
                if error.code in ("auth_required", "tool_missing"):
                    return Identity(host, backend, "none", me=self.me(host))
                return self._failed(host, backend, "none", error)
            return self._failed(host, backend, source, error)
        me = self.me(host) or user.login
        return Identity(
            host,
            backend,
            source,
            login=user.login,
            id=user.id,
            me=me,
            me_matches_login=matches_login(me, user.login),
            scopes=user.scopes,
        )

    @staticmethod
    def _failed(host: str, backend: Backend, source: TokenSource, error: ForgectlError) -> Identity:
        message = redact_secrets(error.message)
        return Identity(
            host, backend, source, error={"host": host, "code": error.code, "message": message}
        )


def _once(read: Callable[[], User]) -> User:
    return read()


def _command_token(result: ProcessResult) -> tuple[str | None, str | None]:
    """``(reason, None)`` for a failed ``token_command``, ``(None, token)`` otherwise. Checked
    in this order: timeout, size, streams, exit status, content."""
    if result.timed_out:
        return "timed out", None
    if result.stdout_truncated:
        return f"gave more than {MAX_TOKEN + 2} bytes", None
    if result.streams_incomplete:
        return "kept its output open after it exited", None
    if result.returncode != 0:
        return f"failed with status {result.returncode}", None
    out = result.stdout
    if out.endswith(b"\n"):
        out = out[:-1]
    if not out.isascii():
        return "did not give one line of printable ASCII", None
    text = out.decode("ascii")
    if not _TOKEN.fullmatch(text):
        return "did not give one line of printable ASCII without spaces", None
    return None, text


def _scopes(header: str | None) -> tuple[str, ...] | None:
    """Scope names from ``X-OAuth-Scopes`` (GitHub classic tokens); None when not reported."""
    if header is None:
        return None
    names = [part.strip() for part in header.split(",")]
    return tuple(n for n in names if _SCOPE.fullmatch(n))[:MAX_SCOPES]


__all__ = [
    "Credential",
    "Hosts",
    "Identity",
    "TOKEN_COMMAND_TIMEOUT",
    "User",
    "clear_secrets",
    "matches_login",
    "redact_secrets",
    "register_secret",
    "secret_values",
]
