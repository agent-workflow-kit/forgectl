"""Process test: does the real ``glab`` repeat ``Private-Token`` on a cross-origin redirect?

This is the loopback reproduction behind the upstream report drafted in
``docs/upstream/glab-private-token-redirect.md`` (#37). Everything stays on 127.0.0.1 and the
token is a made-up string. Two loopback servers play two origins (a different port is a
different origin): server A answers with a redirect to server B.

The test records what the installed ``glab`` does. When the behavior differs from the one written
in ``docs/interface.md`` (Residual risks), because ``glab`` drops the token or stops at the
redirect, the test skips with a reason that names the documents to update, so a fix upstream
shows up in ``-rs`` output. Other deviations fail: a nonzero exit, or anything other than exactly
one body-less GET at origin B. The invariants that hold for any ``glab`` version are asserted
unconditionally. The gateway's
answer to a redirect it was shown (3xx is ``host_rejected``, a wrong host in the answer is
``wrong_host_response``) is covered in ``tests/test_gateway.py``.
"""

from __future__ import annotations

import http.server
import shutil
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from .harness.procs import ProcessRegistry

FAKE_TOKEN = "FAKE-loopback-token-not-real"  # obviously fake; never a real credential
LOOPBACK = "127.0.0.1"
KNOWN_BEHAVIOR = (
    "glab repeats Private-Token on a redirect to another origin (documented in docs/interface.md, "
    "Residual risks, and design.md D-2 and D-5)"
)

needs_glab = pytest.mark.skipif(shutil.which("glab") is None, reason="glab is not on PATH")


@dataclass
class Seen:
    """Requests one loopback server received: (method, path, Private-Token or None)."""

    requests: list[tuple[str, str, str | None]] = field(default_factory=list)


def _server(seen: Seen, redirect_to: str | None) -> http.server.ThreadingHTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _answer(self) -> None:
            seen.requests.append((self.command, self.path, self.headers.get("Private-Token")))
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if redirect_to is not None:
                self.send_response(302)
                self.send_header("Location", f"{redirect_to}/api/v4/projects/1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b'{"id": 1}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

        def log_message(self, format: str, *args: object) -> None:
            pass

    return http.server.ThreadingHTTPServer((LOOPBACK, 0), Handler)


@dataclass
class Origins:
    a: Seen
    b: Seen
    port_a: int
    port_b: int


@contextmanager
def two_origins(redirect_host: str = LOOPBACK) -> Iterator[Origins]:
    a, b = Seen(), Seen()
    server_b = _server(b, None)
    port_b = server_b.server_address[1]
    server_a = _server(a, f"http://{redirect_host}:{port_b}")
    port_a = server_a.server_address[1]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (server_a, server_b)]
    for thread in threads:
        thread.start()
    try:
        yield Origins(a, b, port_a, port_b)
    finally:
        for server in (server_a, server_b):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=10)


def run_glab(procs: ProcessRegistry, root: Path, port: int, args: list[str]) -> tuple[int, str]:
    """Run the installed glab against origin A with a private config and the fake token."""
    config = root / "glab-config"
    config.mkdir(mode=0o700)
    host = f"{LOOPBACK}:{port}"
    # glab speaks https unless the host's config says otherwise; the file must be 0600.
    (config / "config.yml").write_text(
        f'hosts:\n  "{host}":\n    api_protocol: http\n    api_host: {host}\n', encoding="utf-8"
    )
    (config / "config.yml").chmod(0o600)
    home = root / "home"
    home.mkdir(exist_ok=True)
    cwd = root / "cwd"
    cwd.mkdir(exist_ok=True)
    env = {
        "PATH": str(Path(shutil.which("glab") or "glab").parent),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(root / "xdg"),
        "GLAB_CONFIG_DIR": str(config),
        "GITLAB_HOST": host,
        "GITLAB_TOKEN": FAKE_TOKEN,
        "NO_PROXY": f"{LOOPBACK},localhost",
        "no_proxy": f"{LOOPBACK},localhost",
        "GLAB_NO_PROMPT": "1",
        "GLAB_SEND_TELEMETRY": "false",
        "GLAB_CHECK_UPDATE": "false",
        "GLAB_SHOW_WHATS_NEW": "false",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/forgectl-no-bus",
    }
    done = procs.run(["glab", "api", *args], env=env, cwd=cwd, timeout=60)
    return done.returncode, done.stdout


CASES = {
    "get": ["projects/1"],
    "post_turned_into_get": ["-X", "POST", "projects/1"],
}
# The redirect target names the same host (only the port differs) or a different hostname that
# still resolves to the loopback listener. Go's client treats only the second as another host.
REDIRECT_HOSTS = {"other_port": LOOPBACK, "other_hostname": "localhost"}


def fixed_upstream(b_requests: list[tuple[str, str, str | None]]) -> bool:
    """True when glab no longer sends its token to origin B: B got nothing, or no token header."""
    return not b_requests or b_requests[0][2] is None


def localhost_is_loopback() -> bool:
    try:
        infos = socket.getaddrinfo("localhost", None, socket.AF_INET)
    except OSError:
        return False
    return any(info[4][0] == LOOPBACK for info in infos)


FIXED_MESSAGE = (
    "glab no longer repeats Private-Token across origins: update docs/interface.md "
    "(Residual risks), design.md D-2 and D-5, the upstream draft and this test"
)


def test_no_requests_at_b_counts_as_fixed_upstream() -> None:
    assert fixed_upstream([])


def test_a_request_at_b_without_the_token_counts_as_fixed_upstream() -> None:
    assert fixed_upstream([("GET", "/api/v4/projects/1", None)])


def test_a_request_at_b_with_the_token_is_the_known_behavior() -> None:
    assert not fixed_upstream([("GET", "/api/v4/projects/1", FAKE_TOKEN)])


@needs_glab
@pytest.mark.parametrize("redirect", sorted(REDIRECT_HOSTS))
@pytest.mark.parametrize("case", sorted(CASES))
def test_glab_cross_origin_redirect_behavior_is_recorded(
    procs: ProcessRegistry, tmp_path: Path, case: str, redirect: str
) -> None:
    if redirect == "other_hostname" and not localhost_is_loopback():
        pytest.skip("localhost does not resolve to 127.0.0.1 here")
    with two_origins(REDIRECT_HOSTS[redirect]) as origins:
        code, stdout = run_glab(procs, tmp_path, origins.port_a, CASES[case])
        a, b = list(origins.a.requests), list(origins.b.requests)
    # Invariants for any glab version: it sent its token to the origin it was configured for,
    # and the fake token never showed up anywhere but in the Private-Token header.
    assert [token for _, _, token in a] == [FAKE_TOKEN]
    assert a[0][1] == "/api/v4/projects/1"
    # The one thing that varies by version: what origin B was sent. Decide "fixed" first, so a
    # glab that stops at the redirect (non-zero exit, B empty) skips instead of failing.
    if fixed_upstream(b):
        pytest.skip(FIXED_MESSAGE)
    assert code == 0, "glab followed the redirect and printed the final answer"
    assert stdout.strip() == '{"id": 1}'
    assert len(b) == 1, "glab did not reach the redirect target exactly once"
    assert b[0][0] == "GET", "a followed 302 becomes a body-less GET"
    assert b[0][2] == FAKE_TOKEN, KNOWN_BEHAVIOR  # a different value would be a new finding
