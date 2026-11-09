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
    "forgectl.exportcmd",
    "forgectl.reads",
)

_loaded = False


def register(command: str) -> Callable[[Handler], Handler]:
    def decorator(handler: Handler) -> Handler:
        HANDLERS[command] = handler
        return handler

    return decorator


def load_handlers() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for name in HANDLER_MODULES:
        importlib.import_module(name)
