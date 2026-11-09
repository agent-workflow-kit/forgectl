"""Human actions: actor, human-only gate, interactive runs, required expected values.

Implements interface.md "Human actions" and the flag-combination rules of the write commands
that do not need a host read. Confirmation dialogs themselves belong to the commands that run
them; ``require_confirmation`` says whether one is needed or the run must stop.
"""

from __future__ import annotations

import os
import pwd
import re
from argparse import Namespace
from dataclasses import dataclass

from .errors import ForgectlError, usage

ACTOR_RE = re.compile(r"(?:human|auto):[A-Za-z0-9._@-]{1,64}", re.ASCII)
DECISION_ID_RE = re.compile(r"[A-Za-z0-9._:@/-]{1,128}", re.ASCII)
MAX_DECISION_IDS = 32
STDIN_SPELLINGS = ("-", "/dev/stdin", "/dev/fd/0")
_ME_NAME_RE = re.compile(r"[A-Za-z0-9._@-]{1,64}", re.ASCII)

HUMAN_ONLY_FLAGS = ("auto", "override_blocks", "allow_self", "override_escalation")
NEEDS_EXPECTED = frozenset({"merge", "review", "approve"})


@dataclass(frozen=True)
class Tty:
    stdin: bool
    stderr: bool


@dataclass(frozen=True)
class HumanContext:
    actor: str
    actor_source: str  # "flag", "tty" or "default": audit `actor_source`
    is_human: bool
    interactive: bool
    yes: bool
    dry_run: bool
    decision_ids: tuple[str, ...]


def default_me_name() -> str:
    """The `<user>` of `human:<user>` when `me.name` is not configured: the OS username,
    `unknown` when it fails the pattern. The caller prefers the config's `me.name`."""
    try:  # the account database, never LOGNAME or USER: the environment is the caller's to set
        name = pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        return "unknown"
    return name if _ME_NAME_RE.fullmatch(name) else "unknown"


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def build_context(args: Namespace, tty: Tty, *, me_name: str) -> HumanContext:
    """Apply the human-action rules to a parsed write command; raise exit 2 on a violation."""
    command: str = args.command
    body_file = getattr(args, "body_file", None)
    input_file = getattr(args, "file", None)
    dedupe_key = getattr(args, "dedupe_key", None)
    explicit: str | None = args.actor
    if explicit is not None:
        actor, source = explicit, "flag"
        is_human = explicit.startswith("human:")
    elif tty.stdin:
        actor, source, is_human = f"human:{me_name}", "tty", True
    else:
        actor, source, is_human = "auto:unknown", "default", False

    if not is_human:
        for name in HUMAN_ONLY_FLAGS:
            if getattr(args, name, False):
                raise usage(f"{_flag(name)} is human-only: it needs --actor human:<user> or a TTY")

    if tty.stdin and (body_file in STDIN_SPELLINGS or input_file in STDIN_SPELLINGS):
        raise usage("'-' reads the body from stdin, which is a terminal; pass a file")

    yes: bool = args.yes
    # A stdin body with a stdin terminal was refused above, so it never reaches here interactive.
    interactive = tty.stdin and tty.stderr and not actor.startswith("auto:") and not yes

    if command in NEEDS_EXPECTED and not interactive:
        missing = [
            _flag(name)
            for name in ("expected_head", "expected_base")
            if getattr(args, name, None) is None
        ]
        if missing:
            raise usage(
                f"{command} needs {' and '.join(missing)} unless the run is interactive "
                "(stdin and stderr terminals, a human actor, no --yes, no '-' input)"
            )
    if (
        command == "merge"
        and getattr(args, "auto", False)
        and getattr(args, "override_blocks", False)
    ):
        raise usage("--auto and --override-blocks cannot be combined")
    if command == "approve" and dedupe_key is not None and body_file is None:
        raise usage("--dedupe-key needs --body-file")

    distinct: dict[str, None] = {}  # first-seen order, duplicates dropped
    for decision in args.decision_id or []:
        distinct[decision] = None
        if len(distinct) > MAX_DECISION_IDS:
            raise usage(f"at most {MAX_DECISION_IDS} distinct --decision-id values are allowed")
    ids = list(distinct)

    return HumanContext(
        actor=actor,
        actor_source=source,
        is_human=is_human,
        interactive=interactive,
        yes=yes,
        dry_run=args.dry_run,
        decision_ids=tuple(ids),
    )


def require_confirmation(ctx: HumanContext) -> bool:
    """Whether a confirmation dialog must be shown; exit 13 when it cannot be.

    ``--dry-run`` and ``--yes`` need none. Otherwise an interactive run gets the dialog, and any
    other run stops with ``confirmation_required``.
    """
    if ctx.dry_run or ctx.yes:
        return False
    if ctx.interactive:
        return True
    raise ForgectlError(
        "confirmation_required",
        "this write needs confirmation: run it on a terminal as a human, or pass --yes",
    )
