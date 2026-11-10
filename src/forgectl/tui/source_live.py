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
``snapshot`` and ``diff`` may therefore raise ``Cancelled``; it is not an error to show, so a
caller must catch it before it treats an exception as a failure.

Failures are logged as the CLI logs them (``log.jsonl``), by the in-process runner.

TODO(#93): two lifecycle gaps that need the refresh and cancel work of #59. (1) Quitting while a
read is in flight leaves the child ``gh``/``glab`` process group and its private temp dir alive:
the worker is a daemon thread that dies before the gateway cleans up, and a kill only happens
from the main thread. ``LiveSource`` should track the in-flight process groups, and a
``close()`` called on app exit should kill them and remove their dirs. (2) ``cancelled`` is
only checked before a call, not between the handler's host calls; mid-call cancellation needs
``ReadEnv.call`` (the gateway) to take a cancel check.
"""

from __future__ import annotations

import os
import threading
from argparse import Namespace
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from forgectl import cli, registry
from forgectl.browse import is_pr
from forgectl.config import load_config
from forgectl.errors import ForgectlError
from forgectl.identifiers import bind_args
from forgectl.registry import Invocation
from forgectl.surface import parse

Runner = Callable[[list[str]], dict[str, Any]]
"""Run one read command (``["snapshot", pr]``) and return its ``to_dict`` output; raise a
``ForgectlError`` for a failed command, ``partial_results`` carrying ``output``."""


class Cancelled(Exception):
    """The call was cancelled before it started; nothing was sent to any host.

    It is a plain ``Exception``, not a ``ForgectlError``, and not an error to display: a caller
    of ``LiveSource.snapshot`` or ``diff`` that passes ``cancelled`` must catch it before it
    treats an exception as a failure.
    """


READ_COMMANDS = frozenset({"prs", "snapshot", "snapshots", "diff"})

# Guards the once-only config load of a runner (``registry.load_handlers`` locks itself).
_LOAD_LOCK = threading.Lock()


def _log_failure(err: ForgectlError, args: Namespace | None) -> None:
    """One ``log.jsonl`` line for a failed read, as ``cli._fail`` writes it (nothing printed)."""
    details = {
        "command": getattr(args, "command", None),
        "exit_code": err.exit_code,
        "reason": err.reason,
        "message": err.message,
    }
    cli._log(cli._failure_level(err), err.code, args, details)


def in_process_runner(
    config_path: str | None = None,
    environ: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> Runner:
    """A runner over the registered handlers (see the module docstring).

    ``config_path`` is the command line's ``--config`` (else the default lookup) and ``timeout``
    its ``--timeout`` (else the config's). It mirrors ``cli._run`` between parsing and the
    handler call: keep the two in step.
    """
    loaded: list[Any] = []  # the LoadedConfig, once

    def config() -> Any:
        """The config, loaded once, after the handlers are imported."""
        registry.load_handlers()  # thread-safe: it returns only once every handler is registered
        with _LOAD_LOCK:
            if not loaded:  # a failure is not cached: fixing the config and refreshing works
                loaded.append(
                    load_config(config_path, env=environ if environ is not None else os.environ)
                )
            return loaded[0]

    def run(argv: list[str]) -> dict[str, Any]:
        args: Namespace | None = None
        try:
            args = parse(argv)
            if args.command not in READ_COMMANDS:
                raise ForgectlError("internal", f"{args.command}: not a read command of browse")
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
                    timeout=cfg.timeout_for(timeout),
                )
            )
            if not isinstance(out, dict):
                raise ForgectlError("internal", f"{args.command}: the handler gave no object")
            return out
        except ForgectlError as err:
            _log_failure(err, args)
            raise

    return run


class LiveSource:
    """``DataSource`` over a runner. ``LiveSource()`` uses the in-process handlers."""

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        config_path: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._runner = runner or in_process_runner(config_path, timeout=timeout)

    def _run(self, argv: list[str]) -> dict[str, Any]:
        try:
            return self._runner(argv)
        except ForgectlError:
            raise
        except Exception as exc:  # a handler bug: the type only, its text may hold anything
            raise ForgectlError("internal", f"{argv[0]}: {type(exc).__name__}") from exc

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
            argv.append(f"--author={author}")  # `=`: a value that starts with `-` stays a value
        if review_requested_from is not None:
            argv.append(f"--review-requested-from={review_requested_from}")
        argv += [f"--state={state}", f"--limit={limit}"]
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

    def snapshots(
        self, prs: Sequence[str], *, cancelled: Callable[[], bool] | None = None
    ) -> dict[str, Any]:
        """The ``snapshots --bulk-only`` output of ``prs``: what the bulk reader read whole as
        ``items``, the rest in ``failed`` (the caller reads those with ``snapshot``). Thread-safe;
        ``cancelled`` is checked once before the call, as for ``snapshot``."""
        for pr in prs:
            if not is_pr(pr):  # also keeps an option-looking value out of the argument list
                raise ForgectlError("invalid_pr", "snapshots: not a PR in the form host/path#N")
        if not prs:
            return {"items": [], "failed": [], "complete": True, "errors": []}
        self._check_cancelled(prs[0], cancelled)
        return self._run(["snapshots", "--bulk-only", *prs])

    def _one(self, command: str, pr: str, cancelled: Callable[[], bool] | None) -> dict[str, Any]:
        # `cancelled` is checked here only, before the call: see TODO(#93) in the module docstring.
        if not is_pr(pr):  # also keeps an option-looking value out of the argument list
            raise ForgectlError("invalid_pr", f"{command}: not a PR in the form host/path#N")
        self._check_cancelled(pr, cancelled)
        return self._run([command, pr])

    @staticmethod
    def _check_cancelled(pr: str, cancelled: Callable[[], bool] | None) -> None:
        if cancelled is not None:
            try:
                stop = cancelled()
            except Exception as exc:  # the caller's callable: its text may hold anything
                raise ForgectlError("internal", f"cancelled: {type(exc).__name__}") from exc
            if stop:
                raise Cancelled(pr)
