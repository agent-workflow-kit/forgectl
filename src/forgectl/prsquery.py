"""``prs --query``: the filter and ``ORDER BY`` of ``browse``, applied to the list ``prs`` reads.

The engine is the one the TUI runs, not a copy: ``tui.query`` parses, ``tui.filtering`` gives a
row's values to the evaluator and ``tui.columns`` orders. Those modules need no Textual (the
terminal layout in ``columns`` needs ``rich``, which this module never calls), so the CLI uses
them without the ``tui`` extra.

What the CLI adds is only what the TUI gets elsewhere: the rows (the items ``prs`` listed), the
snapshots (read through ``run_snapshots``, and only when the query needs one) and the viewer
(``me``). See interface: ``prs``, the Query, Narrowing, Roles, Snapshots and Viewer bullets.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from .bulk import run_snapshots
from .config import Config
from .errors import ForgectlError, usage
from .fanout import PrsArgs, entry_error, host_error, lowest_rate_limit, resolve_me, run_prs
from .gateway import Budget
from .identifiers import bind_pr, bind_repo, parse_pr, parse_repo
from .reads import ReadEnv
from .sanitize import sanitize_line
from .tui import columns, filtering, query
from .tui.source import Row

#: The fields whose value comes from the snapshot of a row, never from the ``prs`` item. Every
#: other field is read from the item, so a query on those reads no snapshot. A test keeps this
#: and ``LIST_FIELDS`` equal to ``query.FIELDS``, so a new field has to be classified.
SNAPSHOT_FIELDS = frozenset(
    {
        "ci",
        "review",
        "blocker",
        "approvals",
        "comments",
        "unresolved",
        "others_since_push",
        "awaiting_reply",
        "pushed",
        "ready",
        "attention",
    }
)
LIST_FIELDS = frozenset(
    {"repo", "pr", "title", "author", "state", "number", "age", "draft", "authored", "requested"}
    | {"text"}
)
#: The sort columns whose key comes from the snapshot (``badge`` adds the blocker to the state).
#: ``age`` is not one: its key is the update time of the item, never the snapshot's times
#: (``apply`` leaves them out), so the order does not depend on a snapshot being read.
SNAPSHOT_COLUMNS = frozenset(
    {"badge", "ci", "approvals", "review", "comments", "ready", "attention"}
)
LIST_COLUMNS = frozenset({"pr", "age", "title", "author", "roles"})

#: The ``error.code`` of a row whose snapshot the read gave no answer for at all.
NOT_READ = "not_read"


def fields_of(node: query.Node | None) -> set[str]:
    """Every field a filter names, and ``@me`` where it writes ``me``."""
    found: set[str] = set()
    todo: list[query.Node] = [] if node is None else [node]
    while todo:
        current = todo.pop()
        if isinstance(current, query.Cond):
            found.add(current.field)
            if any(isinstance(v, query.Me) for v in current.values):
                found.add("@me")
        elif isinstance(current, query.Not):
            todo.append(current.child)
        else:
            todo.extend(current.children)
    return found


def needs_snapshots(q: query.Query) -> bool:
    return bool(fields_of(q.where) & SNAPSHOT_FIELDS) or any(
        column in SNAPSHOT_COLUMNS for column, _ in q.order
    )


def needs_me(q: query.Query) -> bool:
    """Whether a row's ``authored`` role matters: the filter says ``authored`` or ``me``, or the
    order sorts by ``roles``."""
    return bool(fields_of(q.where) & {"authored", "@me"}) or any(c == "roles" for c, _ in q.order)


def compile_query(text: str) -> query.Query:
    """The parsed query; a text that does not parse is ``usage`` (exit 2) with the parser's message
    and column. (The command line already checked it; this is for ``browse.query`` of the
    config, which was checked when the config loaded.)"""
    result = query.parse(text)
    if isinstance(result, query.ParseError):
        raise usage(f"not a valid query: {result}")
    return result


def check_scope(q: query.Query, review_requested_from: str | None) -> None:
    """``requested`` is whether the list was asked for PRs awaiting my review; a list that was
    not asked that cannot say it, so a query on it is refused before any host call (exit 2)
    instead of answering ``false`` for every row."""
    uses = "requested" in fields_of(q.where)
    if uses and review_requested_from != "me":
        raise usage("--query: `requested` needs --review-requested-from me")


def _list_age(item: dict[str, Any], now: datetime) -> int | None:
    when = columns.parse_time(item.get("updated_at"))
    return None if when is None else max(0, int((now - when).total_seconds()))


def decide(
    where: query.Node | None,
    row: Row,
    state: columns.RowState,
    now: datetime,
    *,
    cheap: bool = False,
) -> bool | None:
    """The filter's answer for a row (``filtering``'s getter), except that ``age`` is always the
    update time of the list item: not the snapshot's, so that the answer does not depend on
    whether a snapshot was read, and a host-side ``updated_since`` can narrow the list.

    ``cheap`` is the pass that decides which snapshots to read: ``attention`` is 0 for a row
    without a snapshot, which a snapshot may change, so in that pass it is unknown, not 0."""
    base = filtering.getter(row, state, now)

    def get(name: str) -> Any:
        if cheap and name == "attention":
            return None
        return _list_age(row.item, now) if name == "age" else base(name)

    return query.evaluate(where, get)


def undecided(node: query.Node | None) -> query.Node | None:
    """``node`` with every ``IS NULL`` and ``IS NOT NULL`` on a snapshot field made a plain
    condition on it. Both are answered (true and false) for a row whose snapshot has not loaded,
    which is right for a row that stays unloaded but not for the pass that decides which
    snapshots to read: a row it drops is never read, so only an answer that no snapshot can
    change may drop it."""
    if node is None:
        return None
    if isinstance(node, query.Cond):
        if node.field in SNAPSHOT_FIELDS and node.op in (query.IS_NULL, query.NOT_NULL):
            return query.Cond(node.field, query.EQ, ("",))  # unknown while it is unread
        return node
    if isinstance(node, query.Not):
        child = undecided(node.child)
        return query.Not(child) if child is not None else None
    children = tuple(c for c in (undecided(c) for c in node.children) if c is not None)
    return query.And(children) if isinstance(node, query.And) else query.Or(children)


def _host_of(item: dict[str, Any]) -> str:
    return str(item.get("repo") or item.get("pr") or "").split("/", 1)[0].lower()


def _login(item: dict[str, Any]) -> str | None:
    author = item.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    return login if isinstance(login, str) else None


@dataclass(frozen=True)
class Scope:
    """What the list was asked, which decides a row's roles."""

    author_is_me: bool
    requested_from_me: bool


def rows_of(
    items: Iterable[dict[str, Any]],
    scope: Scope,
    me_of: Callable[[str], str | None],
    *,
    want_me: bool,
) -> list[Row]:
    """One ``Row`` per item, in the list's order. ``authored`` is the viewer's own login (as
    ``me`` is on the item's host) equal to the author's, ignoring case; ``requested`` is that the
    list was asked for ``--review-requested-from me``."""
    rows: list[Row] = []
    for item in items:
        roles: set[str] = set()
        if scope.requested_from_me:
            roles.add("requested")
        if scope.author_is_me:
            roles.add("authored")
        elif want_me:
            mine, author = me_of(_host_of(item)), _login(item)
            if mine is not None and author is not None and mine.casefold() == author.casefold():
                roles.add("authored")
        rows.append(Row(item=item, roles=roles))
    return rows


def _states(
    env: ReadEnv, rows: list[Row], read: Callable[..., dict[str, Any]]
) -> tuple[dict[str, columns.RowState], dict[str, str], list[dict[str, str]]]:
    """The state of every row after one bulk read, the message of each failed row, and the errors
    of the hosts it stopped on. A row is loaded, or failed with the code of the read; never
    absent."""
    states: dict[str, columns.RowState] = {}
    config = env.gateway.config.config
    refs = []
    for row in rows:
        try:
            refs.append(bind_pr(parse_pr(row.pr), config))
        except ForgectlError as err:
            states[row.pr] = columns.RowState(columns.FAILED, error=err.code)
    result = read(env, refs) if refs else {"items": [], "failed": [], "errors": []}
    for snap in result.get("items") or []:
        pr = snap.get("pr") if isinstance(snap, dict) else None
        if not isinstance(pr, str) or pr in states:
            continue
        try:
            states[pr] = columns.RowState(columns.LOADED, columns.summarize(snap))
        except Exception:  # bad data in one item costs that row, never the command
            states[pr] = columns.RowState(columns.FAILED, error="internal")
    messages: dict[str, str] = {}
    for failed in result.get("failed") or []:
        pr, error = failed.get("pr"), failed.get("error") or {}
        if isinstance(pr, str):
            messages[pr] = str(error.get("message", ""))
            states.setdefault(pr, columns.RowState(columns.FAILED, error=error.get("code")))
    for row in rows:
        states.setdefault(row.pr, columns.RowState(columns.FAILED, error=NOT_READ))
    return states, messages, list(result.get("errors") or [])


def _budget_note(env: ReadEnv, count: int) -> str:
    """When the budget of the snapshot phase is spent, what a failed read needs to be understood:
    the phase, the number of PRs it was asked for and the budget it had (empty otherwise)."""
    budget = getattr(env.gateway, "budget", None)
    if not isinstance(budget, Budget) or budget.remaining() > 0:
        return ""
    return (
        f"the snapshot phase of {count} PRs used its whole {budget.total:g}s budget; "
        "a larger --timeout reads the rest"
    )


def _with_note(message: str, note: str) -> str:
    """``message`` with ``note`` after it, for a read that ended on the time budget only."""
    return f"{message} ({note})" if "time budget" in message else message


def _without_times(states: dict[str, columns.RowState]) -> dict[str, columns.RowState]:
    """``states`` with the update and push time of every snapshot left out, so that ``ORDER BY
    age`` sorts on the time of the list item whether or not a snapshot was read (the TUI prefers
    the fresher snapshot time, which would make the order depend on the query's other terms)."""
    return {
        pr: (
            replace(state, summary=replace(state.summary, updated_at=None, pushed_at=None))
            if state.summary is not None
            else state
        )
        for pr, state in states.items()
    }


def apply(
    env: ReadEnv,
    output: dict[str, Any],
    q: query.Query,
    scope: Scope,
    *,
    now: datetime | None = None,
    read: Callable[..., dict[str, Any]] = run_snapshots,
) -> dict[str, Any]:
    """``output`` of ``run_prs`` with the query applied: ``items`` are the rows the filter makes
    true, in the order of ``ORDER BY`` (the list's order without one). Never raises for a row:
    the snapshot of a row that could not be read is reported in ``snapshot_failures``, and a host
    whose ``me`` could not be resolved (when the query needs it) is an ``errors`` entry whose rows
    are left out of the result."""
    now = now or columns.utc_now()
    me_cache: dict[str, str | None] = {}
    me_failed: dict[str, dict[str, str]] = {}  # lowercase host -> its `errors` entry

    def me_of(host: str) -> str | None:
        if host not in me_cache:
            queried = {h.lower(): h for h in output["hosts_queried"]}
            me_cache[host] = None
            if host in queried:
                try:
                    me_cache[host] = resolve_me(env, queried[host])
                except ForgectlError as err:  # asked once; the host's rows are left out below
                    me_failed[host] = host_error(queried[host], err)
        return me_cache[host]

    rows = rows_of(output["items"], scope, me_of, want_me=needs_me(q))
    # a row whose `authored` cannot be told is not decided false: its host is an error instead
    rows = [r for r in rows if _host_of(r.item) not in me_failed]
    states: dict[str, columns.RowState] = {}
    host_errors: list[dict[str, str]] = list(me_failed.values())
    messages: dict[str, str] = {}
    failures: list[dict[str, Any]] = []
    # the cheap pass: what the list alone decides. A row the filter makes false without a
    # snapshot is out (its snapshot is never read); the rest, true or unknown, survive
    unloaded = columns.RowState()
    unread = undecided(q.where)
    survivors = [r for r in rows if decide(unread, r, unloaded, now, cheap=True) is not False]
    read_snapshots = bool(survivors) and needs_snapshots(q)
    if read_snapshots:
        states, messages, stopped = _states(env, survivors, read)
        note = _budget_note(env, len(survivors))
        if note:  # a read that ran out of time says in which phase and how much it had
            messages = {pr: _with_note(message, note) for pr, message in messages.items()}
        host_errors += [host_error(e["host"], entry_error(e)) for e in stopped]
    kept = [r for r in survivors if decide(q.where, r, states.get(r.pr) or unloaded, now) is True]
    if q.order:
        kept = columns.order(
            kept, _without_times(states), columns.SORT_REPO, filtering.sort_of(q.order)
        )
    shown = {r.pr for r in kept}
    for row in survivors:  # in the list's order
        state = states.get(row.pr)
        if state is not None and state.status == columns.FAILED:
            failures.append(
                {
                    "pr": row.pr,
                    "error": {
                        "code": state.error or NOT_READ,
                        "message": sanitize_line(
                            messages.get(row.pr) or "the snapshot was not read"
                        ),
                    },
                    "in_items": row.pr in shown,
                }
            )
    merged = dict(output)
    if read_snapshots:  # the query read snapshots: each item says what it was decided on
        merged["items"] = [
            {**r.item, "snapshot": columns.snapshot_summary(states.get(r.pr) or unloaded)}
            for r in kept
        ]
    else:
        merged["items"] = [r.item for r in kept]
    merged["query"] = query.format_query(q)
    merged["errors"] = [*output["errors"], *host_errors]
    merged["complete"] = bool(output["complete"]) and not failures and not host_errors
    if failures:
        merged["snapshot_failures"] = failures
    merged["rate_limit"] = lowest_rate_limit(env, list(output["hosts_queried"])).to_dict()
    return merged


# -- pushdown ------------------------------------------------------------------------------------

_SAFE_LOGIN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", re.ASCII)  # a login on both hosts
_STATES = ("open", "closed", "merged")
_MAX_SINCE = 100 * 365 * 86400  # the longest `--updated-since` duration


def conjuncts(node: query.Node | None) -> list[query.Cond]:
    """The conditions every row must satisfy: the top-level ``AND`` terms, in text order. A
    condition under ``OR`` or ``NOT`` is not one (a row can match without it)."""
    found: list[query.Cond] = []
    todo: list[query.Node] = [] if node is None else [node]
    while todo:
        current = todo.pop(0)
        if isinstance(current, query.Cond):
            found.append(current)
        elif isinstance(current, query.And):
            todo[0:0] = list(current.children)
    return found


def narrow(
    q: query.Query, args: PrsArgs, config: Config, *, state_given: bool, now: datetime | None = None
) -> PrsArgs:
    """``args`` with what the host can be asked, from the top-level ``AND`` terms of the query.
    Only a flag the command line did not give is filled, so an explicit flag always wins (the
    full query is checked again on the rows, so a term it contradicts just empties the list).
    The first term of a kind is the one pushed. The list that comes back holds every row the
    query can match, and ``apply`` removes the rest: pushing down only saves work."""
    state, draft, author, requested = (
        args.state,
        args.draft,
        args.author,
        args.review_requested_from,
    )
    repo, since = args.repo, args.updated_since
    state_open = not state_given
    seconds: list[int] = []
    for c in conjuncts(q.where):
        value = c.values[0] if len(c.values) == 1 else None
        if c.field == "state" and c.op == query.EQ and state_open and isinstance(value, str):
            if value.casefold() in _STATES:
                state, state_open = value.casefold(), False
        elif c.field == "draft" and c.op in (query.TRUE, query.FALSE) and draft is None:
            draft = c.op == query.TRUE
        elif c.field == "author" and c.op == query.EQ and args.author is None and author is None:
            if isinstance(value, query.Me):
                author = "me"
            elif isinstance(value, str) and value.casefold() != "me":
                author = value if _SAFE_LOGIN.fullmatch(value) else None
        elif c.field == "authored" and c.op == query.TRUE and args.author is None:
            author = author or "me"
        elif c.field == "requested" and c.op == query.TRUE and requested is None:
            requested = "me"
        elif c.field == "repo" and c.op == query.EQ and isinstance(value, str):
            repo = repo or _declared_repo(value, config, args)
        elif c.field == "age" and c.op in (query.LT, query.LE) and isinstance(value, int):
            seconds.append(value)
    if args.updated_since is None and seconds and 0 < min(seconds) <= _MAX_SINCE:
        since = f"{min(seconds)}s"
    return replace(
        args,
        state=state,
        draft=draft,
        author=author,
        review_requested_from=requested,
        repo=repo,
        updated_since=since,
    )


def _declared_repo(text: str, config: Config, args: PrsArgs) -> Any:
    """The repo of ``repo = <id>`` as ``--repo`` takes it, when it can be asked for without a
    different outcome: no ``--repo`` or ``--host`` given, the host declared in the config (an
    undeclared built-in is probed, not asked, and ``--repo`` would ask it), the id well formed."""
    if args.repo is not None or args.host is not None:
        return None
    try:
        parsed = parse_repo(text)
        if parsed.host not in config.hosts:
            return None
        return bind_repo(parsed, config)
    except ForgectlError:
        return None


def run(
    env: ReadEnv,
    args: PrsArgs,
    q: query.Query,
    *,
    state_given: bool,
    push: bool = True,
    now: datetime | None = None,
    lister: Callable[..., dict[str, Any]] = run_prs,
    read: Callable[..., dict[str, Any]] = run_snapshots,
) -> dict[str, Any]:
    """``prs --query``: narrow the host's list by the query, list, then apply the whole query.

    Raises ``usage`` before any host call when the query needs a role the list was not asked and
    cannot supply, and ``partial_results`` (with the query applied to what was listed) when a
    host failed to list or its snapshots stopped."""
    asked = narrow(q, args, env.gateway.config.config, state_given=state_given) if push else args
    if asked.repo is None and not (asked.author or asked.review_requested_from):
        raise usage(
            "without --repo, prs needs --author or --review-requested-from, "
            "or a query with a top-level `authored`, `requested`, `author = ...` or `repo = ...`"
        )
    check_scope(q, asked.review_requested_from)
    scope = Scope(
        author_is_me=asked.author == "me", requested_from_me=asked.review_requested_from == "me"
    )
    partial: ForgectlError | None = None
    try:
        output = lister(env, replace(asked, state=str(asked.state)))
    except ForgectlError as err:
        if err.code != "partial_results" or err.output is None:
            raise
        output, partial = err.output, err
    listed = len(output["errors"])  # the hosts that failed to list
    # the snapshot phase has a budget of its own, as long as --timeout: a slow list cannot leave
    # the reads after it without time (D-43)
    if needs_snapshots(q) and isinstance(env.gateway.budget, Budget):
        env.gateway.budget = Budget(env.gateway.budget.total)
    output = apply(env, output, q, scope, now=now, read=read)
    failed = output["errors"]
    if failed:  # a host failed to list, or the read of its snapshots stopped
        shown = ", ".join(f"{e['host']} ({e['code']})" for e in failed)
        message = (
            partial.message
            if partial is not None and len(failed) == listed
            else f"{len(failed)} of {len(output['hosts_queried'])} hosts failed: {shown}"
        )
        raise ForgectlError("partial_results", message, output=output)
    return output
