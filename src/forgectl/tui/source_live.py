"""The live data source: ``prs``, ``snapshot`` and ``diff`` through the registered handlers.

``LiveSource`` builds the argument list of a read command and gives it to a *runner*. The
default runner (``in_process_runner``) does what ``forgectl <command>`` does after it parsed the
command line, in this process: load the config, bind the identifiers (the closed host list), and
call the handler registered for the command. It never starts ``forgectl``, ``gh`` or ``glab``
itself; the handlers reach the forges through the gateway, as for the CLI.

The read runner reaches only the read commands. The one write, ``merge``, has a runner of its own
(``in_process_merge_runner``) that runs nothing else and goes through the same gate as the command
line (``build_context``), then the registered ``merge`` handler: there is no second merge.

Thread safety, for the progressive loading of #58: ``prs``, ``snapshot(pr)`` and ``diff(pr)`` may
be called from any number of worker threads at once. A ``LiveSource`` keeps only the registry of
the calls in flight (under one lock, for ``close``) and its closed flag; each call parses its own
arguments, builds its own gateway and tracks its own process groups, so one call failing (any
exception) does not touch another. The config is loaded once, under a lock, and shared read-only.

Cancelling: ``cancelled`` (a callable the caller owns, for example
``lambda: token != self._token``) is checked before the call starts and again before every host
call (``gh``, ``glab``, a ``token_command``) the handler makes, and between the slices of a retry
wait, through the gateway. When it is true the call raises ``Cancelled`` and sends nothing more; a
host call already running is not interrupted, it ends by itself (only ``close`` ends it).
``snapshot`` and ``diff`` may therefore raise ``Cancelled``; it is not an error to show, so a
caller must catch it before it treats an exception as a failure. ``prs`` takes no ``cancelled``
but is refused after ``close`` and ended by it like the others.

Closing: ``close()``, called when the app exits, makes every later and every running call
cancelled, SIGKILLs the process group of each host call in flight (only groups this source saw
start and that are still tracked: the gateway runner reports a group untracked, under the same
lock ``close`` holds while killing, just before it reaps the leader, so a reaped group is never
signaled; where ``os.waitid`` exists a reaped leader is also checked directly) and waits up to
``CLOSE_WAIT`` seconds for the calls to return. Only the read paths are tracked: a detached child
(a hook watchdog) is never started from here and never touched. Each call removes its own private
temp dir as it unwinds, so no group and no dir is left for the daemon worker threads, which the
interpreter would otherwise stop mid-call. The app keeps its concurrency slot until the worker
returned, so a cancelled read still counts toward the bound.

Failures are logged as the CLI logs them (``log.jsonl``), by the in-process runner.
"""

from __future__ import annotations

import contextlib
import os
import signal
import threading
from argparse import Namespace
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from forgectl import cli, registry
from forgectl.backends.bulk_common import HOST_STOPS
from forgectl.browse import is_pr
from forgectl.config import load_config
from forgectl.errors import Cancelled, ForgectlError
from forgectl.gateway import CALL_SCOPE, CallScope
from forgectl.humanactions import Tty, build_context, default_me_name
from forgectl.identifiers import bind_args
from forgectl.registry import Invocation
from forgectl.surface import parse

Runner = Callable[[list[str]], dict[str, Any]]
"""Run one read command (``["snapshot", pr]``) and return its ``to_dict`` output; raise a
``ForgectlError`` for a failed command, ``partial_results`` carrying ``output``."""


__all__ = ["Cancelled", "LiveSource", "in_process_merge_runner", "in_process_runner"]

CLOSE_WAIT = 5.0
"""Seconds ``LiveSource.close`` waits for the calls in flight to return after their groups died."""

READ_COMMANDS = frozenset({"prs", "snapshot", "snapshots", "poll", "diff"})

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


def in_process_merge_runner(
    config_path: str | None = None,
    environ: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> Runner:
    """A runner for the ``merge`` command only, mirroring ``cli._run`` for a write: config, the
    closed host list, the human-action gate with a terminal on stdin and stderr (so the actor is
    ``human:<me.name>`` with ``actor_source: tty``), then the registered handler. ``browse`` always
    passes ``--yes`` with ``--expected-head`` and ``--expected-base`` after its own dialog, so the
    command line dialog never runs and a stale PR is refused by the command itself."""

    def run(argv: list[str]) -> dict[str, Any]:
        args: Namespace | None = None
        try:
            args = parse(argv)
            if args.command != "merge":
                raise ForgectlError("internal", f"{args.command}: not the merge command")
            registry.load_handlers()
            with _LOAD_LOCK:
                cfg = load_config(config_path, env=environ if environ is not None else os.environ)
            bind_args(args, cfg.config)
            human = build_context(
                args, Tty(stdin=True, stderr=True), me_name=cfg.config.me.name or default_me_name()
            )
            handler = registry.HANDLERS.get("merge")
            if handler is None:
                raise ForgectlError("internal", "merge: not implemented")
            out = handler(
                Invocation(
                    command="merge",
                    args=args,
                    json=True,
                    human=human,
                    config=cfg,
                    timeout=cfg.timeout_for(timeout),
                )
            )
            if not isinstance(out, dict):
                raise ForgectlError("internal", "merge: the handler gave no object")
            if not human.dry_run:
                cli._log("info", "command_done", args, {"command": "merge"})
            return out
        except ForgectlError as err:
            _log_failure(err, args)
            raise

    return run


def _kill_group(pid: int, pgid: int) -> None:
    """SIGKILL the group of a child this process started, if its leader is not reaped yet.

    The caller only passes tracked groups (see ``_Call``), which is what guards a reaped leader
    where the gateway can observe the leader's exit without reaping it (``waitid`` on Linux,
    kqueue on macOS); its poll-mode fallback reaps before the group is untracked. Where
    ``os.waitid`` exists the leader is checked as well: it is ours, so
    ``waitid(WNOWAIT)`` answers for exactly that PID without reaping it, and
    ``ChildProcessError`` means it is gone and its number may belong to another process now.
    """
    if hasattr(os, "waitid"):
        try:
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


class _Call:
    """The process groups of one read that are alive now: added at launch, removed (under the
    source lock) just before the gateway reaps the leader, so a group listed here has an
    unreaped leader. That holds where the gateway has ``waitid`` or kqueue (Linux and macOS); its
    poll-mode fallback reaps inside ``proc.poll()``, before the removal runs."""

    def __init__(self) -> None:
        self.groups: set[tuple[int, int]] = set()


class HostStopped(ForgectlError):
    """The bulk reader was stopped by its host (a ``HOST_STOPS`` code, such as a rate limit):
    another read of that host would meet the same answer, so ``detail`` raises this and the
    caller does not fall back to ``snapshot``."""


class LiveSource:
    """``DataSource`` over a runner. ``LiveSource()`` uses the in-process handlers."""

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        config_path: str | None = None,
        timeout: float | None = None,
        merge_runner: Runner | None = None,
    ) -> None:
        self._runner = runner or in_process_runner(config_path, timeout=timeout)
        self._merge_runner = merge_runner or in_process_merge_runner(config_path, timeout=timeout)
        self._cond = threading.Condition()
        self._closed = False
        self._calls: list[_Call] = []

    def close(self) -> None:
        """End every read in flight and refuse later ones (see the module docstring). Idempotent."""
        with self._cond:
            self._closed = True
            for call in self._calls:
                for pid, pgid in call.groups:
                    _kill_group(pid, pgid)
            self._cond.wait_for(lambda: not self._calls, timeout=CLOSE_WAIT)

    def _run(self, argv: list[str], runner: Runner | None = None) -> dict[str, Any]:
        try:
            return (runner or self._runner)(argv)
        except (ForgectlError, Cancelled):
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
            return self._tracked(argv, None, "prs")
        except ForgectlError as err:
            if err.code == "partial_results" and isinstance(err.output, dict):
                return {**err.output, "complete": False}
            raise

    def light(self) -> dict[str, Any]:
        """The ``poll`` output over every configured host (#118): the change-detection tuple of
        each PR of mine. Like ``prs``, exit 12 ``partial_results`` is returned as the answer it
        carries with ``complete`` false; any other failure raises ``ForgectlError``."""
        try:
            return self._tracked(["poll"], None, "poll")
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
        like ``snapshot``, the read is cancellable and ``close`` kills it."""
        for pr in prs:
            if not is_pr(pr):  # also keeps an option-looking value out of the argument list
                raise ForgectlError("invalid_pr", "snapshots: not a PR in the form host/path#N")
        if not prs:
            return {"items": [], "failed": [], "complete": True, "errors": []}
        return self._tracked(["snapshots", "--bulk-only", *prs], cancelled, prs[0])

    def detail(self, pr: str, *, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
        """The ``snapshot`` object of one PR from one bulk read (``snapshots --bulk-only
        --lookup`` of that PR: a GraphQL request on GitHub plus one small read of the checks'
        ``isRequired`` (#136), a single lookup on GitLab), for the detail pane, which paints
        from the snapshot it already holds and is refreshed by this.

        Raises ``ForgectlError`` when the bulk reader cannot read the PR whole (``upstream_error``
        naming the reason code of ``failed``), so the caller reads ``snapshot`` instead.
        Thread-safe and cancellable like ``snapshot``."""
        if not is_pr(pr):
            raise ForgectlError("invalid_pr", "detail: not a PR in the form host/path#N")
        answer = self._tracked(["snapshots", "--bulk-only", "--lookup", pr], cancelled, pr)
        for item in answer.get("items") or []:
            if isinstance(item, dict) and item.get("pr") == pr:
                if item.get("complete") is not True:
                    raise ForgectlError("upstream_error", "detail: the bulk read is not whole")
                return item
        reason = "no item"
        for entry in answer.get("failed") or []:
            error = entry.get("error") if isinstance(entry, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            if isinstance(code, str) and code.replace("_", "").isalnum() and len(code) <= 40:
                reason = code
        if reason in HOST_STOPS:  # the code of the stop itself: a stopped host is not asked again
            raise HostStopped(reason, f"detail: the host stopped the bulk read ({reason})")
        raise ForgectlError("upstream_error", f"detail: the bulk read failed ({reason})")

    def merge(
        self,
        pr: str,
        *,
        head: str,
        base: str,
        method: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """The ``merge`` output of one PR, through the merge command's own path, with the head and
        base the caller showed as ``--expected-head`` and ``--expected-base`` (a PR that moved is
        refused as ``stale_head`` or ``stale_base``) and ``--yes`` (the caller's dialog is the
        confirmation). ``dry_run`` runs every check and writes nothing. Never ``--override-blocks``,
        ``--auto`` or any other human-only flag. Not cancellable: once the write started, only
        the command itself decides the outcome."""
        if not is_pr(pr):
            raise ForgectlError("invalid_pr", "merge: not a PR in the form host/path#N")
        argv = ["merge", pr, "--yes", f"--expected-head={head}", f"--expected-base={base}"]
        if method is not None:
            argv.append(f"--method={method}")
        if dry_run:
            argv.append("--dry-run")
        return self._tracked(argv, None, pr, self._merge_runner)

    def _one(self, command: str, pr: str, cancelled: Callable[[], bool] | None) -> dict[str, Any]:
        if not is_pr(pr):  # also keeps an option-looking value out of the argument list
            raise ForgectlError("invalid_pr", f"{command}: not a PR in the form host/path#N")
        return self._tracked([command, pr], cancelled, pr)

    def _tracked(
        self,
        argv: list[str],
        cancelled: Callable[[], bool] | None,
        subject: str,
        runner: Runner | None = None,
    ) -> dict[str, Any]:
        """Run one read inside a ``CallScope`` that this source can cancel and kill (``prs``,
        ``snapshot``, ``diff`` and ``snapshots`` all come through here)."""

        def stop() -> bool:
            if self._closed:
                return True
            if cancelled is None:
                return False
            try:
                return cancelled()
            except Exception as exc:  # the caller's callable: its text may hold anything
                raise ForgectlError("internal", f"cancelled: {type(exc).__name__}") from exc

        call = _Call()

        def spawned(pid: int, pgid: int) -> None:
            with self._cond:
                call.groups.add((pid, pgid))
                if self._closed:  # closed between the last check and the start of the child
                    _kill_group(pid, pgid)

        def reaped(pid: int, pgid: int) -> None:
            with self._cond:  # close() holds this lock while it signals: never after the reap
                call.groups.discard((pid, pgid))

        def done() -> None:
            with self._cond:
                call.groups.clear()  # the leader is reaped: the numbers may be reused

        with self._cond:
            if self._closed:
                raise Cancelled(subject)
            self._calls.append(call)
        token = CALL_SCOPE.set(
            CallScope(cancelled=stop, on_spawn=spawned, on_done=done, on_reap=reaped)
        )
        try:
            if stop():  # before the first host call, whatever the runner is
                raise Cancelled(subject)
            return self._run(argv, runner)
        finally:
            CALL_SCOPE.reset(token)
            with self._cond:
                self._calls.remove(call)
                self._cond.notify_all()
