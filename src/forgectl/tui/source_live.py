"""The live data source: ``prs``, ``snapshot`` and ``diff`` through the registered handlers.

``LiveSource`` builds the argument list of a read command and gives it to a *runner*. The
default runner (``in_process_runner``) does what ``forgectl <command>`` does after it parsed the
command line, in this process: load the config, bind the identifiers (the closed host list), and
call the handler registered for the command. It never starts ``forgectl``, ``gh`` or ``glab``
itself; the handlers reach the forges through the gateway, as for the CLI.

Only the three read commands can be run from here, so no write handler is reachable.

Thread safety, for the progressive loading of #58: a ``LiveSource`` holds no per-call state.
``snapshot(pr)`` and ``diff(pr)`` may be called from any number of worker threads at once; each
call parses its own arguments and builds its own gateway, so one call failing (any exception) does
not touch another. The config is loaded once, under a lock, and shared read-only.

Cancelling: a call that is already running cannot be stopped. ``cancelled`` (a callable the
caller owns, for example ``lambda: token != self._token``) is checked once before the call
starts; if it is true the call raises ``Cancelled`` without any host traffic. A finished call
whose answer is no longer wanted is dropped by the caller's own token, as in ``tui.app``.
"""

# pyright: reportMissingImports=false, reportCallIssue=false
# The read stack (config, identifiers, the extended Invocation) is not on this branch yet; the
# imports are lazy and the marker can go once it is merged in.
from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from typing import Any

from forgectl.browse import is_pr
from forgectl.errors import ForgectlError

Runner = Callable[[list[str]], dict[str, Any]]
"""Run one read command (``["snapshot", pr]``) and return its ``to_dict`` output; raise a
``ForgectlError`` for a failed command, ``partial_results`` carrying ``output``."""


class Cancelled(Exception):
    """The call was cancelled before it started; nothing was sent to any host."""


def in_process_runner(
    config_path: str | None = None, environ: Mapping[str, str] | None = None
) -> Runner:
    """A runner over the registered handlers (see the module docstring).

    ``config_path`` is the command line's ``--config`` (else the default lookup). The imports are
    inside, so the module loads on a branch that has no read stack.
    """
    lock = threading.Lock()
    loaded: list[Any] = []  # the LoadedConfig, once

    def config() -> Any:
        """The config, with the handlers imported, once and before any call goes on.

        ``registry.load_handlers`` marks itself done before it has imported anything, so a second
        thread calling it meanwhile finds no handler. Every call comes through this lock first, so
        the first caller finishes the imports before another one looks.
        """
        from forgectl import registry
        from forgectl.config import load_config

        with lock:
            if not loaded:  # a failure is not cached: fixing the config and refreshing works
                registry.load_handlers()
                loaded.append(
                    load_config(config_path, env=environ if environ is not None else os.environ)
                )
            return loaded[0]

    def run(argv: list[str]) -> dict[str, Any]:
        from forgectl import registry
        from forgectl.identifiers import bind_args
        from forgectl.registry import Invocation
        from forgectl.surface import parse

        args = parse(argv)
        cfg = config()
        bind_args(args, cfg.config)
        handler = registry.HANDLERS.get(args.command)
        if handler is None:
            raise ForgectlError("internal", f"{args.command}: not implemented")
        out = handler(
            Invocation(
                command=args.command,
                args=args,
                json=True,
                human=None,
                config=cfg,
                timeout=cfg.timeout_for(None),
            )
        )
        if not isinstance(out, dict):
            raise ForgectlError("internal", f"{args.command}: the handler gave no object")
        return out

    return run


class LiveSource:
    """``DataSource`` over a runner. ``LiveSource()`` uses the in-process handlers."""

    def __init__(self, runner: Runner | None = None, *, config_path: str | None = None) -> None:
        self._runner = runner or in_process_runner(config_path)

    def _run(self, argv: list[str]) -> dict[str, Any]:
        try:
            return self._runner(argv)
        except ForgectlError:
            raise
        except Exception as exc:  # a handler bug: the type only, its text may hold anything
            raise ForgectlError("internal", f"{argv[0]}: {type(exc).__name__}") from None

    def prs(
        self,
        *,
        author: str | None = None,
        review_requested_from: str | None = None,
        state: str = "open",
        limit: int = 100,
    ) -> dict[str, Any]:
        """The ``prs`` output over every configured host.

        Exit 12 ``partial_results`` is returned as the listing it carries, with ``complete``
        false and the failed hosts in ``errors``; any other failure raises ``ForgectlError``.
        """
        argv = ["prs"]
        if author is not None:
            argv += ["--author", author]
        if review_requested_from is not None:
            argv += ["--review-requested-from", review_requested_from]
        argv += ["--state", state, "--limit", str(limit)]
        try:
            return self._run(argv)
        except ForgectlError as err:
            if err.code == "partial_results" and isinstance(err.output, dict):
                return {**err.output, "complete": False}
            raise

    def snapshot(self, pr: str, *, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """The ``snapshot`` output of one PR. Thread-safe; see the module docstring."""
        return self._one("snapshot", pr, cancelled)

    def diff(self, pr: str, *, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """The ``diff`` output of one PR. Thread-safe."""
        return self._one("diff", pr, cancelled)

    def _one(self, command: str, pr: str, cancelled: Callable[[], bool] | None) -> dict[str, Any]:
        if not is_pr(pr):  # also keeps an option-looking value out of the argument list
            raise ForgectlError("invalid_pr", f"{command}: not a PR in the form host/path#N")
        if cancelled is not None and cancelled():
            raise Cancelled(pr)
        return self._run([command, pr])
