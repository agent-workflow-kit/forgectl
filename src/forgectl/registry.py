"""Command handlers: where each command's implementation plugs into the CLI.

``cli.py`` parses and gates a command, then calls the handler registered here under the
command's name. A command with no handler answers with the ``internal`` envelope, so the whole
surface parses before every command exists.

A command's module registers itself with ``@register("show")`` and is listed in
``HANDLER_MODULES`` (one line each), which ``load_handlers`` imports on first use.
"""

from __future__ import annotations

import importlib
import math
import threading
from argparse import Namespace
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import LoadedConfig
from .humanactions import HumanContext
from .output import RecordStream

_FROM_CONFIG = math.nan  # the timeout default: take it from the config (no real timeout is NaN)


@dataclass(frozen=True)
class Invocation:
    """A parsed command that passed the human-action gate."""

    command: str
    args: Namespace
    json: bool
    human: HumanContext | None  # set for the write commands only
    config: LoadedConfig = field(default_factory=LoadedConfig.defaults)
    # Seconds: --timeout, else FORGECTL_TIMEOUT, else the config. Left out, it is the config's.
    timeout: float = _FROM_CONFIG

    def __post_init__(self) -> None:
        if math.isnan(self.timeout):
            object.__setattr__(self, "timeout", self.config.timeout_for(None))


Handler = Callable[[Invocation], dict[str, Any] | RecordStream]

HANDLERS: dict[str, Handler] = {}

# Modules that register handlers, as dotted names. Each command's issue adds its own line.
HANDLER_MODULES: tuple[str, ...] = (
    "forgectl.reads",
    "forgectl.listing",
    "forgectl.snapshot_reads",  # snapshot, comments, reviews, ci (#12)
    "forgectl.exportcmd",
    "forgectl.merge",  # merge (#21)
    "forgectl.browse",
    "forgectl.bulk",  # snapshots (#116)
    "forgectl.poll",  # poll (#118)
    "forgectl.post",  # comment, reply (#24)
    "forgectl.review",  # review on GitHub (#26) and GitLab (#27)
    "forgectl.resolve",  # resolve, unresolve (#29)
)

_loaded = False
_load_lock = threading.RLock()  # held for the whole import: a caller waits for a full HANDLERS


def register(command: str) -> Callable[[Handler], Handler]:
    def decorator(handler: Handler) -> Handler:
        HANDLERS[command] = handler
        return handler

    return decorator


def load_handlers() -> None:
    """Import every handler module once. Safe to call from several threads: a caller that
    arrives mid-import waits, so none sees a partial ``HANDLERS``. ``_loaded`` is set only after
    every import succeeded, so a failed import is retried (and raises again) on the next call.

    Handler modules must not call ``load_handlers`` at import time: it runs under the registry
    lock, so one could deadlock against a thread importing that module directly."""
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        for name in HANDLER_MODULES:
            importlib.import_module(name)
        _loaded = True
