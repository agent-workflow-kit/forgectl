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
from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import ForgectlError
from .gateway import (
    MAX_TOKEN,
    Backend,
    ExecutableMissing,
    Gateway,
    Target,
    _redact,
    verify_identity,
)
from .identifiers import backend_of, bind_login

TOKEN_COMMAND_TIMEOUT = 10.0  # seconds
MAX_SCOPES = 64
MAX_SCOPE_CHARS = 128

TokenSource = Literal["token_env", "token_command", "stored_login", "none"]

_TOKEN = re.compile(rf"[\x21-\x7e]{{1,{MAX_TOKEN}}}")
_SCOPE = re.compile(rf"[\x21-\x7e]{{1,{MAX_SCOPE_CHARS}}}")
_ID = re.compile(r"[A-Za-z0-9:/_=-]{1,128}")


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


def redact_secrets(text: str) -> str:
    """``text`` with every registered token, and its percent-encoded forms, as ``[redacted]``."""
    for secret in sorted(secret_values(), key=len, reverse=True):
        text = _redact(text, secret)
    return text


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


class Hosts:
    """Credentials, targets and identity for one command, over one gateway."""

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway
        self._credentials: dict[str, Credential | ForgectlError] = {}

    @property
    def _config(self) -> Any:
        return self.gateway.config.config

    def backend(self, host: str) -> Backend:
        """The backend of a built-in or declared host; any other host is ``unknown_host``."""
        return backend_of(host.lower(), self._config)

    def target(self, host: str) -> Target:
        """The gateway target for ``host``, carrying only this host's own credential."""
        host = host.lower()
        backend = self.backend(host)
        entry = self._config.hosts.get(host)
        prefix = entry.api_prefix if entry is not None and entry.api_prefix else ""
        credential = self.credential(host)
        return Target(backend, host, api_prefix=prefix, token=credential.token)

    def credential(self, host: str) -> Credential:
        host = host.lower()
        self.backend(host)  # an unknown host never reaches a credential
        if host not in self._credentials:
            try:
                self._credentials[host] = self._select(host)
            except ForgectlError as error:
                self._credentials[host] = error
        outcome = self._credentials[host]
        if isinstance(outcome, ForgectlError):
            raise outcome
        return outcome

    # selection --------------------------------------------------------------------------

    def _select(self, host: str) -> Credential:
        entry = self._config.hosts.get(host)
        env_name = entry.token_env if entry is not None else None
        command = entry.token_command if entry is not None else None
        if env_name is None and command is None:
            return Credential("stored_login")
        details = {"variable": env_name} if env_name is not None else None
        if env_name is not None:
            value = self.gateway.environ.get(env_name)
            if value:
                if not _TOKEN.fullmatch(value):
                    raise ForgectlError(
                        "auth_required",
                        f"hosts.{host}.token_env: the value of {env_name} is not one line of"
                        " printable ASCII without spaces; it was not used",
                        details=details,
                    )
                register_secret(value)
                return Credential("token_env", value, env_name)
        if command is not None:
            token = self._run_command(host, tuple(command), details)
            register_secret(token)
            return Credential("token_command", token, env_name)
        raise ForgectlError(
            "auth_required",
            f"hosts.{host}.token_env: {env_name} is unset or empty; set it, or remove"
            f" hosts.{host}.token_env to use the stored login",
            details=details,
        )

    def _run_command(
        self, host: str, command: tuple[str, ...], details: dict[str, Any] | None
    ) -> str:
        where = f"hosts.{host}.token_command"

        def fail(reason: str) -> ForgectlError:
            return ForgectlError("auth_required", f"{where} {reason}", details=details)

        budget = self.gateway.budget
        remaining = budget.remaining()
        if remaining <= 0:
            raise ForgectlError("network_error", "the time budget ran out before the call")
        argv = [host if arg == "{host}" else arg for arg in command]
        try:
            with budget.spending():
                result = self.gateway.run_external(
                    argv,
                    timeout=min(TOKEN_COMMAND_TIMEOUT, remaining),
                    mode="token_command",
                    backend=self.backend(host),
                    max_stdout=MAX_TOKEN + 2,
                )
        except ExecutableMissing:
            raise fail("program not found on an absolute PATH entry") from None
        if result.timed_out:
            raise fail("timed out")
        if result.returncode != 0:
            raise fail(f"failed with status {result.returncode}")
        if result.stdout_truncated or result.streams_incomplete:
            raise fail("gave unusable output")
        out = result.stdout
        if out.endswith(b"\n"):
            out = out[:-1]
        try:
            text = out.decode("ascii")
        except UnicodeDecodeError:
            raise fail("did not give one line of printable ASCII") from None
        if not _TOKEN.fullmatch(text):
            raise fail("did not give one line of printable ASCII without spaces")
        return text

    # me ---------------------------------------------------------------------------------

    def me(self, host: str) -> str | None:
        """``hosts.<host>.me``, else ``me.github`` or ``me.gitlab``, else None (the caller then
        uses the authenticated user). Checked under the backend's own login grammar."""
        host = host.lower()
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

    # identity ---------------------------------------------------------------------------

    def authenticated_user(self, target: Target) -> User:
        """``GET /user`` as the account this target authenticates. The answer must belong to the
        target's host (``wrong_host_response`` otherwise)."""
        response = self.gateway.api(target, "GET", ["user"], shape="object")
        data = response.data
        github = target.backend == "github"
        login = data.get("login" if github else "username")
        account = data.get("id")
        if (
            not isinstance(login, str)
            or isinstance(account, bool)
            or not isinstance(account, int | str)
            or not _ID.fullmatch(str(account))
        ):
            raise ForgectlError("upstream_error", "the identity answer is not the documented shape")
        try:
            bind_login(login, target.backend)
        except ForgectlError:
            raise ForgectlError(
                "upstream_error", "the identity answer holds an unusable login"
            ) from None
        verify_identity(
            data.get("html_url" if github else "web_url"), host=target.host, secret=target.token
        )
        scopes = _scopes(response.headers.get("x-oauth-scopes")) if github else None
        return User(login=login, id=str(account), scopes=scopes)

    def identity(self, host: str) -> Identity:
        """The ``whoami`` entry for ``host``. An unknown host raises; a failed credential or read
        is returned in ``error`` (``{host, code, message}``)."""
        host = host.lower()
        backend = self.backend(host)
        try:
            credential = self.credential(host)
        except ForgectlError as error:
            return self._failed(host, backend, "none", error)
        source: TokenSource = credential.source
        try:
            user = self.authenticated_user(self.target(host))
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
