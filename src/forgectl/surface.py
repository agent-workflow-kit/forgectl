"""The frozen command and flag set (interface: Commands, Global flags).

Parsing only: a bad command line raises a ``usage`` error (exit 2) that ``cli.main`` turns into
the envelope, and argparse prints and exits only for ``--help`` and ``--version`` (exit 0).
Flag values are checked for their own grammar here; rules that span flags and the human-only
gate are in ``humanactions``.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from argparse import Namespace
from contextvars import ContextVar
from datetime import datetime
from typing import Any, NoReturn

from . import __version__
from .errors import usage
from .humanactions import ACTOR_RE, DECISION_ID_RE
from .identifiers import (
    PrInput,
    parse_branch,
    parse_host,
    parse_login,
    parse_pr,
    parse_repo,
    parse_sha,
    parse_typed_id,
)

PROG = "forgectl"
DESCRIPTION = "Normalized access to pull/merge requests across GitHub and GitLab."

READ_COMMANDS = (
    "prs",
    "show",
    "snapshot",
    "snapshots",
    "poll",
    "diff",
    "compare",
    "files",
    "comments",
    "reviews",
    "ci",
    "access",
    "whoami",
)
WRITE_COMMANDS = ("merge", "review", "comment", "reply", "resolve", "unresolve", "approve")
OTHER_COMMANDS = ("browse", "export")
COMMANDS = READ_COMMANDS + WRITE_COMMANDS + OTHER_COMMANDS

_DURATION_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d)?", re.ASCII)
_UNIT_SECONDS = {None: 1.0, "ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_RFC3339_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})",
    re.ASCII,
)
_DEDUPE_KEY_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}", re.ASCII)
_LIMIT_RE = re.compile(r"[0-9]{1,4}", re.ASCII)
# The longest duration accepted, so that `now - duration` can never overflow: 100 years.
_MAX_DURATION_SECONDS = 100 * 365 * 86400.0

# --- value types ---------------------------------------------------------------------------


def _bad(message: str) -> NoReturn:
    raise argparse.ArgumentTypeError(message)


def nonempty(value: str) -> str:
    if not value:
        _bad("must not be empty")
    return value


def duration(value: str) -> float:
    """A positive number of seconds, or `<number><unit>`; see interface Config."""
    match = _DURATION_RE.fullmatch(value)
    seconds = float(match[1]) * _UNIT_SECONDS[match[2]] if match else math.nan
    if not math.isfinite(seconds) or seconds <= 0:
        _bad(f"not a positive duration (seconds, or a number with ms, s, m, h or d): {value!r}")
    if seconds > _MAX_DURATION_SECONDS:
        _bad(f"longer than 100 years: {value!r}")
    return seconds


def duration_or_off(value: str) -> float:
    """A duration of at least 1 s, or a bare `0` for "off" (`browse --interval`); see interface
    Config."""
    if value == "0":
        return 0.0
    seconds = duration(value)
    if seconds < 1:
        _bad(f"an interval is at least 1s (or 0 for off): {value!r}")
    return seconds


def time_or_duration(value: str) -> str:
    """RFC 3339 with an offset, or a duration; returned as given."""
    if _RFC3339_RE.fullmatch(value):
        try:
            datetime.fromisoformat(value.upper())
        except ValueError:
            _bad(f"not a real RFC 3339 time: {value!r}")
        return value
    duration(value)
    return value


def pr_or_stdin(value: str) -> PrInput | str:
    """A PR as ``parse_pr`` reads it, or the single word ``-`` (the PRs come from stdin)."""
    return value if value == "-" else parse_pr(value)


def sha(value: str) -> str:
    """A full 40-hex commit sha; uppercase is lowered."""
    return parse_sha(value)


def branch(value: str) -> str:
    """A branch name under the `check-ref-format` rules (see ``identifiers.parse_branch``)."""
    return parse_branch(value)


def actor(value: str) -> str:
    if not ACTOR_RE.fullmatch(value):
        _bad(f"must look like human:<user> or auto:<name>: {value!r}")
    return value


def decision_id(value: str) -> str:
    if not DECISION_ID_RE.fullmatch(value):
        _bad(f"not a valid decision id: {value!r}")
    return value


def dedupe_key(value: str) -> str:
    if not _DEDUPE_KEY_RE.fullmatch(value):
        _bad(f"not a valid dedupe key: {value!r}")
    return value


def limit(value: str) -> int:
    if not _LIMIT_RE.fullmatch(value) or not 1 <= int(value) <= 1000:
        _bad(f"must be a whole number from 1 to 1000: {value!r}")
    return int(value)


# --- actions: a flag given twice is an error --------------------------------------------------

_SEEN: ContextVar[set[str] | None] = ContextVar("forgectl_seen_flags", default=None)


def _note_flag(parser: argparse.ArgumentParser, dest: str, option: str | None) -> None:
    seen = _SEEN.get()
    if seen is None:
        return
    if dest in seen:
        parser.error(f"{option} given more than once")
    seen.add(dest)


class _Once(argparse.Action):
    """Store one value; a second use of the flag, in either position, is an error."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        _note_flag(parser, self.dest, option_string)
        setattr(namespace, self.dest, values)


class _OnceFlag(_Once):
    def __init__(
        self,
        option_strings: list[str],
        dest: str,
        default: Any = False,
        required: bool = False,
        help: str | None = None,
    ) -> None:
        super().__init__(
            option_strings, dest, nargs=0, default=default, required=required, help=help
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        super().__call__(parser, namespace, True, option_string)


class _OnceConst(_Once):
    """A flag that stores ``const``; like ``_OnceFlag`` but for a shared ``dest``."""

    def __init__(
        self,
        option_strings: list[str],
        dest: str,
        const: Any = None,
        default: Any = None,
        help: str | None = None,
    ) -> None:
        super().__init__(option_strings, dest, nargs=0, const=const, default=default, help=help)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        super().__call__(parser, namespace, self.const, option_string)


class _MustFollowCommand(argparse.Action):
    """A write flag given before the command: say where it goes instead of a vague error."""

    def __init__(self, option_strings: list[str], dest: str, **kw: Any) -> None:
        super().__init__(
            option_strings, dest, nargs="?", default=argparse.SUPPRESS, help=argparse.SUPPRESS
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        parser.error(
            f"{option_string} must follow the command (forgectl <command> ... {option_string})"
        )


class _Parser(argparse.ArgumentParser):
    """argparse that raises a usage error instead of printing and exiting."""

    def error(self, message: str) -> NoReturn:
        raise usage(message)

    def _get_option_tuples(self, option_string: str) -> list[Any]:
        """No prefix matching of options. ``allow_abbrev=False`` stops it only for ``--long``
        options; a single-dash argument such as ``-https://...`` would still match ``-h`` as a
        short-option cluster and print help. Only an exact option string (or ``--opt=value``,
        which argparse resolves before it gets here) is an option."""
        return []

    def _print_message(self, message: str, file: Any = None) -> None:
        """Print help and version text through the stream-safe writer, so a full or closed
        stdout cannot change the exit code or fail at shutdown."""
        if message:
            from .output import write_line

            write_line(file if file is not None else sys.stderr, message.removesuffix("\n"))


# --- the parser ----------------------------------------------------------------------------------


def _globals(parser: argparse.ArgumentParser, *, sub: bool) -> None:
    """The global flags. On a subcommand they only apply when given there, so they never
    overwrite a value given before the command."""
    flag_default: Any = argparse.SUPPRESS if sub else False
    value_default: Any = argparse.SUPPRESS if sub else None
    parser.add_argument(
        "--json", action=_OnceFlag, default=flag_default, help="machine-readable output"
    )
    parser.add_argument(
        "--verbose", action=_OnceFlag, default=flag_default, help="more human output and log detail"
    )
    parser.add_argument("--quiet", action=_OnceFlag, default=flag_default, help="less human output")
    parser.add_argument(
        "--config",
        metavar="PATH",
        type=nonempty,
        action=_Once,
        default=value_default,
        help="config file instead of the default",
    )
    parser.add_argument(
        "--timeout",
        metavar="D",
        type=duration,
        action=_Once,
        default=value_default,
        help="budget for host calls: seconds, or a number with ms, s, m, h or d",
    )


def _writes(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--yes", action=_OnceFlag, help="skip the confirmation prompt")
    parser.add_argument("--dry-run", action=_OnceFlag, help="every check, no write")
    parser.add_argument(
        "--actor",
        type=actor,
        action=_Once,
        default=None,
        metavar="A",
        help="human:<user> or auto:<name>",
    )
    parser.add_argument(
        "--decision-id",
        dest="decision_id",
        type=decision_id,
        action="append",
        metavar="D",
        help="decision id for the audit record; repeatable",
    )
    parser.add_argument(
        "--override-escalation",
        action=_OnceFlag,
        help="human-only: count a hook's exit 13 as allow",
    )


def _once(parser: argparse.ArgumentParser, flag: str, **kw: Any) -> None:
    kw.setdefault("default", None)
    parser.add_argument(flag, action=_Once, **kw)


def _expected(parser: argparse.ArgumentParser) -> None:
    _once(parser, "--expected-head", type=sha, metavar="SHA", help="full 40-hex head sha")
    _once(parser, "--expected-base", type=branch, metavar="REF", help="base branch name")


def _pr(parser: argparse.ArgumentParser) -> None:
    # Grammar only; the closed host list is checked once the config is loaded (`bind_args`).
    parser.add_argument("pr", type=parse_pr, help="URL, or host/<project path>#N")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, description=DESCRIPTION, allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    _globals(parser, sub=False)
    for flag in ("--yes", "--dry-run", "--actor", "--decision-id", "--override-escalation"):
        parser.add_argument(flag, action=_MustFollowCommand)
    subs = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    def add(name: str, summary: str) -> argparse.ArgumentParser:
        sub = subs.add_parser(name, help=summary, description=summary, allow_abbrev=False)
        _globals(sub, sub=True)
        return sub

    p = add("prs", "list pull requests")
    _once(p, "--repo", type=parse_repo, metavar="ID")
    _once(p, "--host", type=parse_host, metavar="HOST")
    _once(p, "--author", type=parse_login, metavar="me|USER")
    _once(
        p,
        "--review-requested-from",
        dest="review_requested_from",
        type=parse_login,
        metavar="me|USER",
    )
    _once(p, "--state", choices=["open", "closed", "merged", "all"], default="open")
    draft = p.add_mutually_exclusive_group()
    draft.add_argument("--draft", dest="draft", action=_OnceConst, const=True, default=None)
    draft.add_argument("--no-draft", dest="draft", action=_OnceConst, const=False)
    _once(p, "--updated-since", dest="updated_since", type=time_or_duration, metavar="TIME")
    _once(p, "--limit", type=limit, default=100, metavar="N")

    for name, summary in (
        ("show", "the normalized PR object"),
        ("snapshot", "the PR with reviews, threads and CI in one call"),
        ("diff", "the unified diff"),
        ("files", "the changed files"),
        ("comments", "review threads and comments"),
        ("reviews", "reviews and who approved"),
        ("ci", "checks on the head"),
    ):
        _pr(add(name, summary))

    p = add("snapshots", "snapshots of many PRs from a few bulk requests")
    p.add_argument(
        "refs",
        nargs="+",
        type=pr_or_stdin,
        metavar="PR|-",
        help="URL or host/<project path>#N, repeatable; a single - reads them from stdin",
    )
    p.add_argument(
        "--bulk-only",
        dest="bulk_only",
        action=_OnceFlag,
        help="read only through the bulk path; list a PR it cannot read whole in failed",
    )
    p.add_argument(
        "--lookup",
        dest="lookup",
        action=_OnceFlag,
        help="read each PR by its own lookup, not through the lists (a few PRs: GitLab only)",
    )

    add("poll", "the change-detection tuple of every PR of mine, in a few requests")

    p = add("compare", "compare two commits of a PR")
    _pr(p)
    p.add_argument("--from", dest="from_sha", type=sha, required=True, action=_Once, metavar="SHA")
    p.add_argument("--to", dest="to_sha", type=sha, required=True, action=_Once, metavar="SHA")

    p = add("access", "whether a user has write access to a repo")
    p.add_argument("repo", type=parse_repo)
    p.add_argument("user", type=parse_login)

    p = add("whoami", "who forgectl acts as on each host")
    _once(p, "--host", type=parse_host, metavar="HOST")

    p = add("merge", "merge a PR")
    _pr(p)
    p.add_argument("--auto", action=_OnceFlag, help="human-only: merge when checks pass")
    _expected(p)
    _once(p, "--method", choices=["squash", "merge", "rebase"])
    p.add_argument("--override-blocks", action=_OnceFlag, help="human-only: skip not_ready")
    _writes(p)

    p = add("review", "post a review from a JSON file")
    _pr(p)
    _once(p, "--file", type=nonempty, required=True, metavar="PATH|-", default=argparse.SUPPRESS)
    _expected(p)
    p.add_argument(
        "--allow-self", action=_OnceFlag, help="human-only: skip the self-approval check"
    )
    _once(p, "--dedupe-key", dest="dedupe_key", type=dedupe_key, metavar="K")
    _writes(p)

    p = add("comment", "post a general comment")
    _pr(p)
    _once(
        p,
        "--body-file",
        dest="body_file",
        type=nonempty,
        required=True,
        metavar="PATH|-",
        default=argparse.SUPPRESS,
    )
    _once(p, "--dedupe-key", dest="dedupe_key", type=dedupe_key, metavar="K")
    _writes(p)

    p = add("reply", "reply to a comment, thread or review")
    _pr(p)
    _once(p, "--to", type=parse_typed_id, required=True, metavar="ID", default=argparse.SUPPRESS)
    _once(
        p,
        "--body-file",
        dest="body_file",
        type=nonempty,
        required=True,
        metavar="PATH|-",
        default=argparse.SUPPRESS,
    )
    _once(p, "--dedupe-key", dest="dedupe_key", type=dedupe_key, metavar="K")
    _writes(p)

    for name, summary in (("resolve", "resolve a review thread"), ("unresolve", "reopen a thread")):
        p = add(name, summary)
        _pr(p)
        _once(
            p,
            "--thread",
            type=parse_typed_id,
            required=True,
            metavar="ID",
            default=argparse.SUPPRESS,
        )
        _writes(p)

    p = add("approve", "approve a PR")
    _pr(p)
    _expected(p)
    p.add_argument(
        "--allow-self", action=_OnceFlag, help="human-only: skip the self-approval check"
    )
    _once(p, "--body-file", dest="body_file", type=nonempty, metavar="PATH|-")
    _once(p, "--dedupe-key", dest="dedupe_key", type=dedupe_key, metavar="K")
    _writes(p)

    p = add("browse", "terminal browser (needs the tui extra)")
    # TODO(#5): the URL forms of --focus, through identifiers.py (see browse.parse_focus).
    _once(p, "--focus", type=nonempty, metavar="PR")
    _once(p, "--interval", type=duration_or_off, metavar="D")
    _once(p, "--result-file", dest="result_file", type=nonempty, metavar="PATH")

    p = add("export", "audit records as JSONL")
    _once(p, "--since", type=time_or_duration, metavar="TIME")

    return parser


def parse(argv: list[str]) -> Namespace:
    """Parse ``argv``; raises a ``usage`` error for anything the surface does not allow."""
    parser = build_parser()
    token = _SEEN.set(set())
    try:
        args = parser.parse_args(argv)
    finally:
        _SEEN.reset(token)
    return _check_combinations(args)


def _check_combinations(args: Namespace) -> Namespace:
    if args.verbose and args.quiet:
        raise usage("--verbose and --quiet cannot be combined")
    if args.command == "prs":
        if args.repo and args.host:
            raise usage("--repo and --host cannot be combined")
        if not args.repo and not (args.author or args.review_requested_from):
            raise usage("without --repo, prs needs --author or --review-requested-from")
    return args
