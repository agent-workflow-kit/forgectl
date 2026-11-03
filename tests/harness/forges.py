"""Driver for the fake ``gh`` and ``glab`` in ``tests/fakebin``.

``FakeForges`` makes a private bin directory holding launchers for both programs, a script of
answers, and the log of every call. Put ``bin_dir`` first on the ``PATH`` the code under test
sees. The programs themselves are ``tests/fakebin/_forgefake.py``; read its docstring for what is
modelled.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FAKEBIN = Path(__file__).resolve().parents[1] / "fakebin"
FAKE_PROGRAM = FAKEBIN / "_forgefake.py"
MAX_SHEBANG = 120


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
    stdin: str | None = None
    exit: int | None = None  # None: still running, or killed by a signal
    signals: list[str] = field(default_factory=list)
    request: dict[str, Any] | None = None
    unmatched: bool = False
    rule: int | None = None
    placeholders: bool = False

    @property
    def finished(self) -> bool:
        return self.exit is not None or bool(self.signals)

    @property
    def token_sent(self) -> str | None:
        return (self.request or {}).get("token_sent")


class FakeForges:
    def __init__(self, root: Path) -> None:
        self.bin_dir = root / "fakebin"
        self.bin_dir.mkdir(parents=True)
        self.allow_unmatched = False
        self._rules: list[dict[str, Any]] = []
        self._keyring: dict[str, str] = {}
        self._default: dict[str, Any] | None = None
        for backend in ("gh", "glab"):
            self._install(backend)
        self._write_script()

    def _install(self, backend: str) -> None:
        shebang = f"#!{sys.executable} -I"
        if len(shebang) > MAX_SHEBANG or " " in sys.executable:
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
        script: dict[str, Any] = {"rules": self._rules, "keyring": self._keyring}
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
        hang: bool = False,
        die: str | None = None,
    ) -> None:
        """Script an answer. Rules are tried in order; the first match with uses left wins."""
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
            rule["hang"] = True
        self._rules.append(rule)
        self._write_script()

    def default_answer(self, status: int, body: Any = None) -> None:
        self._default = {"status": status, "body": body}
        self._write_script()

    def keyring_login(self, backend: str, host: str, token: str) -> None:
        """A login in the OS keyring, visible only when the caller passes a session bus variable."""
        self._keyring[f"{backend}:{host}"] = token
        self._write_script()

    @staticmethod
    def stored_login(config_dir: Path, backend: str, host: str, token: str) -> None:
        """A login in ``hosts.yml`` (gh) or ``config.yml`` (glab) under ``config_dir``."""
        config_dir.mkdir(parents=True, exist_ok=True)
        if backend == "gh":
            text = f"{host}:\n    oauth_token: {token}\n    user: someone\n"
            (config_dir / "hosts.yml").write_text(text, encoding="utf-8")
        else:
            text = f"hosts:\n  {host}:\n    token: {token}\n"
            (config_dir / "config.yml").write_text(text, encoding="utf-8")

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
                call.rule = rec.get("rule")
                call.placeholders = bool(rec.get("placeholders"))
        found = [merged[c] for c in order]
        return [c for c in found if backend is None or c.backend == backend]

    def unmatched(self) -> list[Call]:
        return [c for c in self.calls() if c.unmatched]
