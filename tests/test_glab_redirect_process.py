"""Process test: does the real ``glab`` repeat ``Private-Token`` on a cross-origin redirect?

This is the loopback reproduction behind the upstream report drafted in
``docs/upstream/glab-private-token-redirect.md`` (#37). Everything stays on 127.0.0.1 and the
token is a made-up string. Two loopback servers play two origins (a different port is a
different origin): server A answers with a redirect to server B.

The test records what the installed ``glab`` does and never fails because of it. When the
behavior differs from the one written in ``docs/interface.md`` (Residual risks), the test skips
with a reason that names the documents to update, so a fix upstream shows up in ``-rs`` output.
What forgectl itself guarantees is asserted unconditionally: the invariants that hold for any
``glab`` version and the gateway's answer to a redirect.
"""

from __future__ import annotations

import http.server
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from forgectl import gateway as gw

from .harness.forges import FakeForges
from .harness.procs import ProcessRegistry
from .test_gateway import GL_SELF, make_gateway, only_call

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
def two_origins() -> Iterator[Origins]:
    a, b = Seen(), Seen()
    server_b = _server(b, None)
    port_b = server_b.server_address[1]
    server_a = _server(a, f"http://{LOOPBACK}:{port_b}")
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
        "NO_PROXY": LOOPBACK,
        "no_proxy": LOOPBACK,
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


@needs_glab
@pytest.mark.parametrize("case", sorted(CASES))
def test_glab_cross_origin_redirect_behavior_is_recorded(
    procs: ProcessRegistry, tmp_path: Path, case: str
) -> None:
    with two_origins() as origins:
        code, stdout = run_glab(procs, tmp_path, origins.port_a, CASES[case])
        a, b = list(origins.a.requests), list(origins.b.requests)
    # Invariants for any glab version: it sent its token to the origin it was configured for,
    # and the fake token never showed up anywhere but in the Private-Token header.
    assert [token for _, _, token in a] == [FAKE_TOKEN]
    assert a[0][1] == "/api/v4/projects/1"
    assert code == 0, "glab followed the redirect and printed the final answer"
    assert stdout.strip() == '{"id": 1}'
    # The one thing that varies by version: what origin B was sent.
    assert len(b) == 1, "glab did not reach the redirect target exactly once"
    assert b[0][0] == "GET", "a followed 302 becomes a body-less GET"
    if b[0][2] is None:
        pytest.skip(
            "glab no longer repeats Private-Token across origins: update docs/interface.md "
            "(Residual risks), design.md D-2 and D-5, the upstream draft and this test"
        )
    assert b[0][2] == FAKE_TOKEN, KNOWN_BEHAVIOR  # a different value would be a new finding


# -- What forgectl guarantees whatever glab does -----------------------------------------------


def test_gateway_never_follows_a_redirect_itself_and_a_shown_3xx_is_host_rejected(
    fake_forges: FakeForges,
) -> None:
    # A redirect glab did not follow (307 or 308) reaches the gateway as a 3xx.
    fake_forges.answer("glab", endpoint_re="^projects", status=307, body={})
    gateway = make_gateway(fake_forges)
    with pytest.raises(gw.ForgectlError) as raised:
        gateway.api(GL_SELF, "GET", ["projects", "7"])
    assert raised.value.code == "host_rejected"
    assert raised.value.details == {"status": 307}
    assert len(only_call(fake_forges).requests) == 1  # no second request by forgectl


def test_an_answer_that_came_through_a_cross_origin_redirect_is_refused_with_a_rotation_hint() -> (
    None
):
    # The only guard on a redirected read: the answer names another host.
    observed = "http://other.example.test/g/p/-/merge_requests/1"
    with pytest.raises(gw.ForgectlError) as raised:
        gw.verify_identity(observed, host=GL_SELF.host, project="g/p")
    assert raised.value.code == "wrong_host_response"
    assert "rotated" in raised.value.message
