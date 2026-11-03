"""Command-line entry point for forgectl: parse, gate, dispatch, print."""

from __future__ import annotations

import os
import signal
import sys
from argparse import Namespace
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

from . import registry
from .config import load_config
from .errors import ForgectlError
from .humanactions import Tty, build_context, default_me_name
from .output import emit_error, emit_success
from .registry import Invocation
from .surface import WRITE_COMMANDS, build_parser, parse

__all__ = ["build_parser", "main"]


class SignalExit(BaseException):
    """A SIGINT or SIGTERM arrived: exit 128+N with no envelope.

    TODO(#7): once a write is in flight the gateway turns a signal into `outcome_unknown`
    instead (the audit record and exit 12), and this applies only before any write.
    """

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _raise_signal_exit(signum: int, frame: FrameType | None) -> None:
    raise SignalExit(signum)


@contextmanager
def _signals() -> Iterator[None]:
    previous: dict[int, object] = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, _raise_signal_exit)
    except ValueError:  # not the main thread: leave the host's handlers alone
        pass
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)  # type: ignore[arg-type]


def _tty() -> Tty:
    def isatty(stream: object) -> bool:
        try:
            return bool(stream.isatty())  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            return False

    return Tty(stdin=isatty(sys.stdin), stderr=isatty(sys.stderr))


def _run(args: Namespace, tty: Tty) -> dict[str, object]:
    command: str = args.command
    # Config first: a bad config or FORGECTL_* value ends the run before any command logic.
    loaded = load_config(getattr(args, "config", None), env=os.environ)
    timeout = loaded.timeout_for(getattr(args, "timeout", None))
    human = (
        build_context(args, tty, me_name=loaded.config.me.name or default_me_name())
        if command in WRITE_COMMANDS
        else None
    )
    registry.load_handlers()
    handler = registry.HANDLERS.get(command)
    if handler is None:
        raise ForgectlError("internal", f"{command}: not implemented yet")
    return handler(
        Invocation(
            command=command,
            args=args,
            json=args.json,
            human=human,
            config=loaded,
            timeout=timeout,
        )
    )


def _wants_json(argv: list[str]) -> bool:
    """Whether ``--json`` (or ``--json=...``) is on the command line before any ``--``.

    Until parsing succeeds we cannot know for sure, so a bad command line still gets the
    envelope when the flag is there, even with a value that parsing would reject.
    """
    head = argv[: argv.index("--")] if "--" in argv else argv
    return any(arg == "--json" or arg.startswith("--json=") for arg in head)


def _dispatch(argv: list[str], tty: Tty | None) -> int:
    json_mode = _wants_json(argv)
    args: Namespace | None = None
    try:
        args = parse(argv)
        json_mode = args.json
        emit_success(_run(args, tty or _tty()), json_mode=json_mode)
        return 0
    except ForgectlError as err:
        return emit_error(
            err, command=getattr(args, "command", None), args=args, json_mode=json_mode
        )
    except Exception as exc:  # last line of defense: never a traceback
        internal = ForgectlError("internal", f"internal error: {type(exc).__name__}")
        return emit_error(
            internal, command=getattr(args, "command", None), args=args, json_mode=json_mode
        )


def main(argv: list[str] | None = None, *, tty: Tty | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # The signal handlers stay installed while an error is printed, so a signal then still ends
    # the run as 128+N rather than a default-handler death in the middle of the output.
    with _signals():
        try:
            return _dispatch(argv, tty)
        except SignalExit as signalled:
            return 128 + signalled.signum
