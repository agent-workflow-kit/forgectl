"""Driver for the fake ``gh`` and ``glab`` in ``tests/fakebin``.

``FakeForges`` makes a private bin directory holding launchers for both programs, a script of
answers, and the log of every call. Put ``bin_dir`` first on the ``PATH`` the code under test
sees. The programs themselves are ``tests/fakebin/_forgefake.py``; read its docstring for what is
modelled.
"""

from __future__ import annotations

import json
import os
import platform
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FAKEBIN = Path(__file__).resolve().parents[1] / "fakebin"
FAKE_PROGRAM = FAKEBIN / "_forgefake.py"


def shebang_limit(system: str | None = None, release: str | None = None) -> int:
    """The kernel's ``#!`` line limit: 255 bytes on Linux 5.1 and later, else 127."""
    system = platform.system() if system is None else system
    release = platform.release() if release is None else release
    if system == "Linux":
        numbers = [int(n) for n in release.split("-")[0].split(".")[:2] if n.isdigit()]
        if len(numbers) == 2 and tuple(numbers) >= (5, 1):
            return 255
    return 127


@dataclass
class Call:
    """One invocation of a fake program, merged from its log records."""

    id: str
    backend: str
    argv: list[str]
    env: dict[str, str]
    cwd: str
    cwd_real: str
    cwd_entries: list[str] | None
    cwd_mode: int | None
    cwd_owner_is_me: bool | None
    pid: int
    pgid: int
    repo_context: str | None
    config_dir: str | None = None  # the backend's config dir variable, as received
    config_dir_real: str | None = None
    config_entries: list[str] | None = None  # its listing when the program started
    config_mode: int | None = None
    stdin: str | None = None
    exit: int | None = None  # None: still running, or killed by a signal
    signals: list[str] = field(default_factory=list)
    request: dict[str, Any] | None = None
    unmatched: bool = False
    defaulted: bool = False  # answered by ``default_answer``
    not_modelled: bool = False  # exit 70: the fake refused something it does not model
    rule: int | None = None
    placeholders: bool = False

    @property
    def finished(self) -> bool:
        return self.exit is not None or bool(self.signals)

    @property
    def token_sent(self) -> str | None:
        return (self.request or {}).get("token_sent")

    @property
    def requests(self) -> list[dict[str, Any]]:
        """The first request and one more per redirect hop followed, each with its token."""
        if self.request is None:
            return []
        return [self.request, *self.request.get("hops", [])]


class FakeForges:
    def __init__(self, root: Path) -> None:
        self.bin_dir = root / "fakebin"
        self.bin_dir.mkdir(parents=True)
        self.allow_unmatched = False
        self.allow_not_modelled = False  # separate opt-in: allow_unmatched never waives these
        self._rules: list[dict[str, Any]] = []
        self._keyring: dict[str, str] = {}
        self._default: dict[str, Any] | None = None
        self._options: dict[str, Any] = {"default_bus_socket": str(self.bin_dir / "default-bus")}
        for backend in ("gh", "glab"):
            self._install(backend)
        self._write_script()

    def _install(self, backend: str) -> None:
        shebang = f"#!{sys.executable} -I"
        if len(shebang) > shebang_limit() or " " in sys.executable:
            raise RuntimeError(f"interpreter path unusable in a shebang: {sys.executable}")
        body = (
            f"{shebang}\n"
            f"import sys\n"
            f"sys.path.insert(0, {str(FAKEBIN)!r})\n"
            f"import _forgefake\n"
            f"_forgefake.main({backend!r}, {str(self.bin_dir)!r})\n"
        )
        path = self.bin_dir / backend
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # script ------------------------------------------------------------------------------

    def _write_script(self) -> None:
        script: dict[str, Any] = {"rules": self._rules, "keyring": self._keyring, **self._options}
        if self._default is not None:
            script["default"] = self._default
        tmp = self.bin_dir / "script.json.tmp"
        tmp.write_text(json.dumps(script), encoding="utf-8")
        os.replace(tmp, self.bin_dir / "script.json")

    def answer(
        self,
        backend: str,
        endpoint: str | None = None,
        *,
        endpoint_re: str | None = None,
        method: str | None = None,
        host: str | None = None,
        stdin_re: str | None = None,
        nth: int | None = None,
        times: int | None = None,
        status: int = 200,
        body: Any = None,
        body_text: str | None = None,
        body_repeat: int | None = None,
        headers: dict[str, str] | None = None,
        stderr: str | None = None,
        exit: int | None = None,
        delay: float | None = None,
        hang: bool | float = False,
        die: str | None = None,
        redirect: str | None = None,
    ) -> None:
        """Script an answer. Rules are tried in order; the first match with uses left wins.

        ``redirect`` is a ``Location`` (absolute or relative): the fake follows it as the real
        programs do (``status`` 301, 302 or 303 turns any method into a GET without a body, 307
        and 308 keep both, but with a body from stdin are shown, not followed; default 302) and
        the followed request is matched against the rules again, with ``endpoint`` the new URL's
        path without its leading slash.
        """
        if redirect is not None and status == 200:
            status = 302
        match = {
            k: v
            for k, v in dict(
                endpoint=endpoint,
                endpoint_re=endpoint_re,
                method=method,
                host=host,
                stdin_re=stdin_re,
                nth=nth,
            ).items()
            if v is not None
        }
        rule: dict[str, Any] = {"backend": backend, "match": match, "status": status}
        optional = dict(
            body=body,
            body_text=body_text,
            body_repeat=body_repeat,
            headers=headers,
            stderr=stderr,
            exit=exit,
            delay=delay,
            times=times,
            die=die,
        )
        rule.update({k: v for k, v in optional.items() if v is not None})
        if hang:
            rule["hang"] = hang  # True: until signaled (capped at 120 s); a number: that cap
        if redirect is not None:
            rule["redirect"] = {"location": redirect}
        self._rules.append(rule)
        self._write_script()

    def default_answer(self, status: int, body: Any = None) -> None:
        self._default = {"status": status, "body": body}
        self._write_script()

    def keyring_login(self, backend: str, host: str, token: str) -> None:
        """A keyring login: reached only with a session bus variable, and never by ``gh api``."""
        self._keyring[f"{backend}:{host}"] = token
        self._write_script()

    def default_bus_socket(self, *, present: bool) -> None:
        """The per-user bus socket the programs dial with no bus variable at all (the fake's
        stand-in for ``/run/user/<uid>/bus``, private to this test): created or removed."""
        path = Path(self._options["default_bus_socket"])
        if present:
            path.touch()
        else:
            path.unlink(missing_ok=True)

    def glab_unauthenticated_without_credential(self) -> None:
        """glab 1.89.0: with no credential at all it prints a boxed ``Unauthenticated.``, exits
        1 with nothing on stdout and sends nothing (1.127 sends an anonymous request)."""
        self._options["glab_1_89_no_credential"] = True
        self._write_script()

    @staticmethod
    def stored_login(config_dir: Path, backend: str, host: str, token: str) -> None:
        """A login in ``hosts.yml`` (gh) or ``config.yml`` (glab, 0600) under ``config_dir``."""
        config_dir.mkdir(parents=True, exist_ok=True)
        if backend == "gh":
            text = f"{host}:\n    oauth_token: {token}\n    user: someone\n"
            (config_dir / "hosts.yml").write_text(text, encoding="utf-8")
        else:
            text = f"hosts:\n    {host}:\n        token: {token}\n"
            path = config_dir / "config.yml"
            path.write_text(text, encoding="utf-8")
            path.chmod(0o600)  # glab refuses any other mode

    # environment -------------------------------------------------------------------------

    def path(self, *more: str) -> str:
        """A ``PATH`` with the fakes first, then ``more``, then the system directories."""
        return os.pathsep.join([str(self.bin_dir), *more, "/usr/bin", "/bin"])

    # log ---------------------------------------------------------------------------------

    def calls(self, backend: str | None = None) -> list[Call]:
        log = self.bin_dir / "calls.jsonl"
        if not log.exists():
            return []
        merged: dict[str, Call] = {}
        order: list[str] = []
        for line in log.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            cid = rec["id"]
            phase = rec["phase"]
            if phase == "start":
                merged[cid] = Call(
                    id=cid,
                    backend=rec["backend"],
                    argv=rec["argv"],
                    env=rec["env"],
                    cwd=rec["cwd"],
                    cwd_real=rec["cwd_real"],
                    cwd_entries=rec["cwd_entries"],
                    cwd_mode=rec["cwd_mode"],
                    cwd_owner_is_me=rec["cwd_owner_is_me"],
                    pid=rec["pid"],
                    pgid=rec["pgid"],
                    repo_context=rec["repo_context"],
                    config_dir=rec.get("config_dir"),
                    config_dir_real=rec.get("config_dir_real"),
                    config_entries=rec.get("config_entries"),
                    config_mode=rec.get("config_mode"),
                )
                order.append(cid)
            elif phase == "stdin":
                merged[cid].stdin = rec["stdin"]
            elif phase == "signal":
                merged[cid].signals.append(rec["signal"])
            elif phase == "end":
                call = merged[cid]
                call.exit = rec["exit"]
                call.request = rec.get("request")
                call.unmatched = bool(rec.get("unmatched"))
                call.defaulted = bool(rec.get("defaulted"))
                call.not_modelled = bool(rec.get("not_modelled"))
                call.rule = rec.get("rule")
                call.placeholders = bool(rec.get("placeholders"))
        found = [merged[c] for c in order]
        return [c for c in found if backend is None or c.backend == backend]

    def unmatched(self) -> list[Call]:
        return [c for c in self.calls() if c.unmatched]

    def not_modelled_calls(self) -> list[Call]:
        return [c for c in self.calls() if c.not_modelled or c.placeholders]

    def check_teardown(self) -> None:
        """Fail on calls the fake did not model (always, unless ``allow_not_modelled``) and on
        requests with no scripted answer (unless ``allow_unmatched``)."""
        problems: list[str] = []
        if not self.allow_not_modelled:
            refused = self.not_modelled_calls()
            if refused:
                problems.append(
                    "calls the fake does not model (set allow_not_modelled to accept): "
                    + "; ".join(f"{c.backend} {' '.join(c.argv)}" for c in refused)
                )
        if not self.allow_unmatched:
            unmatched = self.unmatched()
            if unmatched:
                problems.append(
                    "requests with no scripted answer (set allow_unmatched to accept): "
                    + "; ".join(f"{c.backend} {' '.join(c.argv)}" for c in unmatched)
                )
        assert not problems, "\n".join(problems)
