"""The `browse` app: a list, a detail pane, and one action (open in a browser). Read-only.

There is no write action here: no merge (todo(#60)), no post. Forge text reaches the screen
only through ``safe`` and ``render``, as ``Content`` that is never parsed as markup.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import threading
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from rich.cells import cell_len
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.events import Event, Key
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from forgectl.errors import ForgectlError

from . import columns, render, safe, sections, style
from .opener import Opener, OpenRefused, is_openable
from .refresh import BACKOFF_FACTOR, RateLimits, host_of, needs_snapshot
from .source import DataSource, Listing, Row, load_list
from .source_live import Cancelled

DEFAULT_CONCURRENCY = 4  # snapshots in flight at once; `browse.concurrency` sets it
DEFAULT_FULL_REFRESH_EVERY = 5  # `browse.full_refresh_every`

# The help is data, one logical line per entry; the widgets wrap it to the terminal width. Adding a
# key is one new row here (and its Binding). A test compares these keys with BrowseApp.BINDINGS;
# a binding whose Textual key name differs from its label needs a KEY_LABELS entry in that test.
HELP_KEYS: list[tuple[str, str]] = [
    ("q", "quit"),
    ("?", "this help"),
    ("/", "filter the list (enter keeps it, escape clears it)"),
    ("enter", "open the detail pane for the highlighted PR"),
    ("esc", "close the detail pane or the filter, or leave the column header"),
    (
        "1-6",
        "toggle a detail section while the detail pane has focus: 1 facts, 2 description, "
        "3 changes (the diff), 4 CI, 5 comments, 6 reviews; the headers show counts "
        "(the changes start collapsed; `browse.detail_collapsed` sets which start collapsed)",
    ),
    (
        "o",
        "open the PR in a browser; on a check in the detail pane, open the check "
        "(only an http or https url is opened)",
    ),
    ("left", "scroll the list sideways by one column (or h); the PR and note columns stay"),
    ("right", "scroll the list sideways by one column the other way (or l)"),
    (
        "H",
        "move to the column header: left and right (h and l also move) pick a column, "
        "enter or space sorts by it "
        "(again reverses), escape returns to the list; a click on a header cell sorts too",
    ),
    (
        "s",
        "switch the order: by repo (default) or needs-attention first (also drops a column sort)",
    ),
    ("a", "filter: authored by me"),
    ("v", "filter: review requested from me"),
    ("f", "filter: CI failing"),
    (
        "n",
        "filter: new comments from others "
        "(filters combine as an intersection; the status bar names them)",
    ),
    ("r", "refresh selected (the PR in the detail pane, else the highlighted row only)"),
    (
        "R",
        "refresh all (both lists, every row, then the detail pane): rows keep their last data "
        "meanwhile ([refreshing]); a failed refresh keeps it and marks the row [stale: code]; "
        "[changed] marks a row whose data changed, until the next refresh or a key; the timed "
        "refresh re-reads only what may have changed",
    ),
]

HELP_COLUMNS = "columns: PR, note, badge, CI, appr, rev, comments, age, title, author, roles"

HELP_LEGEND: list[tuple[str, str]] = [
    ("note", "[refreshing], [stale: code] or [changed]"),
    ("CI", "✓ passing, ✗ failing, … pending, - none, · loading, ! snapshot failed"),
    (
        "comments",
        "c total, u unresolved threads, n new from others since the push, "
        "r threads awaiting my reply",
    ),
    ("age", "since the last push / since the last update"),
    ("▲ ▼", "header: the list is sorted by this column, ascending or descending"),
]

HELP_NOTES = [
    "browse is read-only: no merge, no post.",
    "Press any other key to close this help; arrows, PgUp and PgDn scroll.",
]


def in_thread[T](fn: Callable[..., T], *args: Any) -> asyncio.Future[T]:
    """Run ``fn`` on a daemon thread and await its answer on the loop.

    Not the default executor: its threads are joined when the loop and the interpreter shut
    down, so ``q`` would wait for a slow forge read. A result that arrives after the app is gone
    is dropped; a stale one is discarded by the callers' tokens and flags.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def deliver(settle: Callable[[], None]) -> None:
        def apply() -> None:
            if not future.done():
                settle()

        try:
            loop.call_soon_threadsafe(apply)
        except RuntimeError:  # the loop is closed: the app has quit
            pass

    def work() -> None:
        try:
            value = fn(*args)
        except Exception as exc:  # handed to the awaiting coroutine
            error = exc  # `exc` is unbound once the except block ends
            deliver(lambda: future.set_exception(error))
        else:
            deliver(lambda: future.set_result(value))

    threading.Thread(target=work, name="browse-read", daemon=True).start()
    return future


HELP_MIN_WIDTH = 30  # the help box never shrinks below this, unless the screen itself is narrower

HELP_SCROLL_KEYS = {
    "up": "scroll_up", "k": "scroll_up", "down": "scroll_down", "j": "scroll_down",
    "pageup": "scroll_page_up", "pagedown": "scroll_page_down",
    "home": "scroll_home", "end": "scroll_end",
}  # fmt: skip


def _key_column_width() -> int:
    """One key column width for every help table, from the longest key label."""
    return max(1, *(cell_len(key) for key, _ in (*HELP_KEYS, *HELP_LEGEND)))


def _help_table(rows: list[tuple[str, str]], table_id: str) -> Grid:
    """A two-column table: a key column sized from the data, a description column that wraps."""
    cells: list[Static] = []
    for key, description in rows:
        cells.append(Static(safe.plain(key), classes="help-key", markup=False))
        cells.append(Static(safe.plain(description), classes="help-desc", markup=False))
    grid = Grid(*cells, id=table_id, classes="help-table")
    grid.styles.grid_columns = f"{_key_column_width() + 1} 1fr"  # +1: a gap before the text
    return grid


class HelpScreen(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help"):
            yield _help_table(HELP_KEYS, "help-keys")
            yield Static(safe.plain(HELP_COLUMNS), classes="help-para", markup=False)
            yield _help_table(HELP_LEGEND, "help-legend")
            for note in HELP_NOTES:
                yield Static(safe.plain(note), classes="help-para", markup=False)

    def on_resize(self) -> None:
        # CSS min-width would push the box past a screen narrower than it, so cap it here
        self.query_one("#help").styles.min_width = min(HELP_MIN_WIDTH, self.size.width)

    def on_key(self, event: Key) -> None:
        event.stop()
        action = HELP_SCROLL_KEYS.get(event.key)
        if action is None:
            self.dismiss(None)
        else:
            getattr(self.query_one("#help"), action)(animate=False)


class HeaderBar(Static, can_focus=True):
    """The column header. It takes focus with ``H``: left and right move the column cursor,
    enter or space sorts by that column, escape gives the focus back to the list. A click on a
    cell sorts by it. The text is drawn by ``BrowseApp``: constant column names, and the sort
    arrow, which takes a cell beside the sorted column's name."""

    BINDINGS = [
        Binding("left", "cursor(-1)", "left", show=False),
        Binding("h", "cursor(-1)", "left", show=False),
        Binding("right", "cursor(1)", "right", show=False),
        Binding("l", "cursor(1)", "right", show=False),
        Binding("enter", "sort", "sort", show=False),
        Binding("space", "sort", "sort", show=False),
        Binding("escape", "leave", "list", show=False),
    ]

    def focus_on_click(self) -> bool:
        return False  # a click sorts and leaves the focus on the list; it never moves the cursor

    def action_cursor(self, direction: int) -> None:
        self.app.header_move(direction)  # type: ignore[attr-defined]

    def action_sort(self) -> None:
        self.app.header_sort()  # type: ignore[attr-defined]

    def action_leave(self) -> None:
        self.app.header_leave()  # type: ignore[attr-defined]

    def on_click(self, event: events.Click) -> None:
        event.stop()
        offset = event.get_content_offset(self)  # relative to the text, past the padding
        if offset is not None:
            self.app.header_click(offset.x)  # type: ignore[attr-defined]

    def on_focus(self) -> None:
        self.app.call_after_refresh(self.app.header_focused)  # type: ignore[attr-defined]

    def on_blur(self) -> None:
        self.app.call_after_refresh(self.app._paint_header)  # type: ignore[attr-defined]


class RowList(OptionList):
    """The list of rows. Left and right scroll the table sideways while it has focus; up and
    down still move the highlight. The table itself is drawn by ``BrowseApp``."""

    # home and end are OptionList's row jumps and stay that way; the far ends of the table are
    # reached by repeating left or right.
    BINDINGS = [
        Binding("left", "scroll_cols(-1)", "left", show=False),
        Binding("h", "scroll_cols(-1)", "left", show=False),
        Binding("right", "scroll_cols(1)", "right", show=False),
        Binding("l", "scroll_cols(1)", "right", show=False),
    ]

    def action_scroll_cols(self, direction: int) -> None:
        self.app.scroll_columns(direction)  # type: ignore[attr-defined]

    def on_resize(self, event: Any) -> None:
        self.app.relayout()  # type: ignore[attr-defined]

    def watch_show_vertical_scrollbar(self, old: bool, new: bool) -> None:
        # the scrollbar takes cells from the rows without changing this widget's size
        self.app.relayout()  # type: ignore[attr-defined]


class BrowseApp(App[None]):
    ENABLE_COMMAND_PALETTE = False  # its screenshot command would write forge text to a file
    CSS = """
    #main { height: 1fr; }
    #left { width: 3fr; }
    #detail { width: 2fr; display: none; border-left: solid $primary; padding: 0 1; }
    #filter { display: none; }
    #header { height: 1; text-style: bold; padding: 0 2; }  /* the list's border and padding;
       constant column names, the sort arrow and the cursor */
    #list { text-wrap: nowrap; text-overflow: ellipsis; }
    #checks { height: auto; max-height: 12; }
    .section-head { text-style: bold; margin-top: 1; }
    #head-facts { margin-top: 0; }
    #footer { dock: bottom; height: 2; }
    #bar, #status { height: 1; }
    HelpScreen { align: center middle; }
    #help {
        width: 90%; max-width: 100; height: auto; max-height: 100%;
        border: round $primary; padding: 1 2; background: $surface;
    }
    .help-table { grid-size: 2; grid-columns: 10 1fr; grid-rows: auto; height: auto; }
    .help-key, .help-desc { height: auto; }
    .help-para { height: auto; margin-top: 1; }
    """
    BINDINGS = [
        Binding("q", "quit_app", "quit"),
        Binding("question_mark", "help", "help"),
        Binding("slash", "filter", "filter"),
        Binding("escape", "close", "close"),
        Binding("o", "open", "open"),
        Binding("s", "sort", "order"),
        Binding("H", "header_focus", "header"),
        Binding("a", "toggle_filter('authored')", "authored"),
        Binding("v", "toggle_filter('requested')", "requested"),
        Binding("f", "toggle_filter('ci-failing')", "ci failing"),
        Binding("n", "toggle_filter('new-comments')", "new comments"),
        Binding("r", "refresh_selected", "refresh"),
        Binding("R", "reload_list", "reload"),
        # Hidden from the footer; each acts only while the detail pane has focus. The help
        # documents the six as one "1-6" row.
        *[
            Binding(str(i), f"toggle_section('{name}')", f"toggle {name}", show=False)
            for i, name in enumerate(sections.NAMES, 1)
        ],
    ]

    def __init__(
        self,
        source: DataSource,
        opener: Opener,
        *,
        focus: str | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        clock: Callable[[], datetime] = columns.utc_now,
        refresh_interval: float = 0,
        full_refresh_every: int = DEFAULT_FULL_REFRESH_EVERY,
        detail_collapsed: Iterable[str] = sections.DEFAULT_COLLAPSED,
    ) -> None:
        super().__init__()
        # Seconds between automatic refreshes; 0, less, or not a number means off.
        valid = isinstance(refresh_interval, int | float) and math.isfinite(refresh_interval)
        self.refresh_interval = float(refresh_interval) if valid and refresh_interval > 0 else 0.0
        self.full_refresh_every = max(1, full_refresh_every)
        self.refresh_timer: Timer | None = None
        self._cycle_listed = True  # the current refresh's `prs` calls gave at least one answer
        self._busy = False  # a refresh (list, then the row snapshots) is in progress
        self._loaded_once = False
        self._last_refresh: datetime | None = None
        self._auto_cycles = 0  # automatic cycles since start or the last manual refresh all
        self._rates = RateLimits()
        self._host_last: dict[str, datetime] = {}  # when a host's rows were last read, by cycle
        self._deferred: set[str] = set()  # PRs that need a read the backoff put off
        self._unlisted: set[str] = set()  # kept from an incomplete listing, shown stale
        self._added: set[str] = set()  # PRs listed since the first load, not yet read
        self._list_complete = True
        self._list_errors = 0
        self._source_cancels = _accepts_cancelled(source)
        self.concurrency = max(0, concurrency)  # 0: the rows' snapshots are not loaded
        self.clock = clock
        self.source = source
        self.opener = opener
        self.focus_pr = focus
        self.intent = "back"
        self.selected: str | None = None
        self.merged: list[str] = []  # todo(#60): PRs merged from here; empty until `m` exists
        self._rows: list[Row] = []
        self._visible: list[Row] = []
        self._states: dict[str, columns.RowState] = {}
        self._order_mode = columns.SORT_REPO  # the preset `s` cycles
        # The one sort state (a column sort replaces the preset while it is set). Set it with
        # ``set_sort``. todo(#105): the query line's ORDER BY sets this same tuple.
        self._sort: columns.SortState = ()
        self._cursor = "pr"  # the header cursor's column
        self._rebuild_pending = False  # a rebuild after snapshot arrivals is queued
        self._list_filters: set[str] = set()
        self._shown_prs: list[str] = []  # the PRs in the option list, in order
        self._snap_token = 0  # a newer list load stops the snapshot pool of an older one
        # One limiter for every round: reads of an older round still in flight keep their slots,
        # so a reload cannot put more than ``concurrency`` snapshot reads on the wire.
        self._snap_slots = asyncio.Semaphore(max(1, self.concurrency))
        self._row_by_pr: dict[str, Row] = {}
        self._pr_width = 0
        self._cuts: list[columns.Cut] = []  # the cut cells of the visible rows, in the same order
        self._painted_view = -1  # the view width the rows and the header were last drawn for
        self._hoffset = 0  # cells the scrolling columns are shifted left; kept across refreshes
        self._snap_done = 0
        self._snap_total = 0
        self._filter = ""
        self._opened = False  # an `o` succeeded: `selected` is the PR it acted on
        self._list_note: str | None = None  # a pending list error, kept on the status line
        self._detail_note: str | None = None  # how the last detail load ended
        self._detail_pr: str | None = None
        self._detail_url: str | None = None
        self._detail_checks: list[dict[str, Any]] = []
        self._detail_token = 0
        # Which detail sections are open: kept for the session, across PRs and refreshes.
        self._open_sections = sections.initial_open(detail_collapsed)
        self._detail: render.Detail | None = None
        self._painted: set[str] = set()  # sections whose body shows the current detail
        self._detail_loading: str | None = None  # the PR whose detail load is in flight

    def result(self) -> dict[str, Any]:
        """The ``--result-file`` content."""
        return {
            "intent": self.intent,
            "selected": [self.selected] if self.selected else [],
            "merged": list(self.merged),
        }

    # -- layout ---------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Input(placeholder="filter", id="filter")
                yield HeaderBar(safe.plain(""), id="header", markup=False)
                yield RowList(id="list")
            with VerticalScroll(id="detail"):
                for name in sections.NAMES:
                    yield Static(
                        safe.plain(""), id=f"head-{name}", classes="section-head", markup=False
                    )
                    yield Static(safe.plain(""), id=name, markup=False)
                    if name == "ci":
                        yield OptionList(id="checks")
        with Vertical(id="footer"):
            yield Static(safe.plain(""), id="bar", markup=False)
            yield Static(safe.plain(""), id="status", markup=False)

    def on_mount(self) -> None:
        self.query_one("#list", OptionList).focus()
        self._start_list(refresh_detail=False)
        if self.focus_pr:
            self._start_detail(self.focus_pr)
        if self.refresh_interval > 0:
            self.refresh_timer = self.set_interval(self.refresh_interval, self._tick)

    def _tick(self) -> None:
        """The automatic refresh. A tick that finds a refresh, a detail load or a modal in the
        way is skipped, not queued: the next tick tries again."""
        if self._busy or self._detail_loading is not None or len(self.screen_stack) > 1:
            return
        self._auto_cycles += 1
        self._start_list(refresh_detail=True, auto=True)

    async def on_event(self, event: Event) -> None:
        if isinstance(event, Key) and self._clear_changed():
            self._apply_filter()
        await super().on_event(event)

    def _clear_changed(self) -> bool:
        any_changed = False
        for state in self._states.values():
            if state.changed:
                state.changed = False
                any_changed = True
        return any_changed

    # -- status ---------------------------------------------------------------------------

    def _paint(self, *parts: str | None) -> None:
        message = "; ".join(p for p in parts if p)
        self.query_one("#status", Static).update(safe.plain(safe.line(message, 500)))

    def _status(self, message: str) -> None:
        """``message``, after any pending list error, which a routine note never hides."""
        self._paint(self._list_note, message)

    @staticmethod
    def _internal(exc: Exception) -> str:
        return f"internal: {type(exc).__name__}"  # the type only: no message, no forge text

    # -- list -----------------------------------------------------------------------------

    def _start_list(self, *, refresh_detail: bool, auto: bool = False) -> None:
        """Start a refresh: `prs`, then the row snapshots. ``auto`` is the timed kind, which
        re-reads only what may have changed; the others re-read every row."""
        self._busy = True
        self._clear_changed()  # a highlight lasts until the next cycle
        self.run_worker(self._load_list(refresh_detail, auto), group="list")

    async def _load_list(self, refresh_detail: bool, auto: bool) -> None:
        self._status("refreshing..." if self._loaded_once else "loading...")
        wanted: set[str] | None = None
        started = False
        listed = False
        detail_pr: str | None = None
        try:
            try:
                listing: Listing = await in_thread(load_list, self.source)
            except Exception as exc:  # a source bug must not take the UI down
                self._list_note = self._internal(exc)  # the rows on screen stay
                self._paint(self._list_note, self._detail_note)
                return
            now = self.clock()
            for rate_limit in listing.rate_limits:
                self._rates.observe(rate_limit, now)
            notes = list(listing.errors)
            if not listing.complete:
                notes.append("list incomplete")
            self._list_note = "; ".join(notes) if notes else None
            listed = listing.answered > 0  # a refresh whose `prs` calls all failed is not one
            self._cycle_listed = listed
            self._list_complete = listing.complete
            self._list_errors = len(listing.errors)
            previous = {r.pr: r.item for r in self._rows}
            rows = listing.rows
            self._unlisted = set()
            if self._loaded_once and not listing.complete:
                # A failed `prs` call hides rows rather than closing them: keep the ones on a
                # host it failed for (any host, when it names none), marked stale, until a
                # complete listing says otherwise. A PR on a healthy host that is gone is closed.
                seen = {r.pr for r in rows}
                gone = [
                    r for r in self._rows if r.pr not in seen and listing.may_hide(host_of(r.pr))
                ]
                for row in gone:
                    old = self._states.get(row.pr)
                    if old is not None and old.error is None:  # never hide an error code
                        old.stale, old.error = True, "list incomplete"
                rows = rows + gone
                self._unlisted = {r.pr for r in gone}  # not read again until listed again
            if self._loaded_once:
                self._added |= {r.pr for r in rows if r.pr not in previous}
            self._rows = rows
            self._row_by_pr = {r.pr: r for r in rows}
            # A row that is still listed keeps its last good data while it reloads.
            self._states = {r.pr: self._states.get(r.pr) or columns.RowState() for r in rows}
            self._deferred &= set(self._row_by_pr)
            if auto:
                wanted = self._plan_cycle(previous, now)
            self._loaded_once = True
            self._apply_filter()
            self._paint(self._list_note or f"{len(self._rows)} PRs", self._detail_note)
            detail_pr = self._detail_to_refresh(refresh_detail, wanted)
            # The detail load reads the PR's snapshot, which also refreshes its row: the batch
            # skips it, so one read serves both.
            started = self._start_snapshots(wanted, skip=detail_pr)
        finally:
            if not started:
                self._finish_refresh(listed=listed)
        if detail_pr is not None:
            self._start_detail(detail_pr)

    def _detail_to_refresh(self, refresh_detail: bool, wanted: set[str] | None) -> str | None:
        """The PR in the open detail pane when this refresh re-reads it, else None."""
        pr = self._detail_pr
        if not refresh_detail or pr is None or not self._detail_shown() or pr in self._unlisted:
            return None
        if pr not in self._row_by_pr:
            return None
        return pr if wanted is None or pr in wanted else None  # a timed cycle skips a settled PR

    def _plan_cycle(self, previous: dict[str, dict[str, Any]], now: datetime) -> set[str]:
        """The PRs a timed cycle reads: all every Nth cycle, else those that may have changed.

        A host below its rate-limit threshold is read at most once per ``BACKOFF_FACTOR``
        intervals, until its ``reset_at``; what it still needs stays deferred until then.
        """
        full = self._auto_cycles % self.full_refresh_every == 0
        wanted = {
            r.pr
            for r in self._rows
            if full
            or r.pr in self._deferred
            or needs_snapshot(r, previous.get(r.pr), self._state(r))
        }
        slow_after = self.refresh_interval * BACKOFF_FACTOR
        allowed: set[str] = set()
        for pr in wanted:
            host = host_of(pr)
            last = self._host_last.get(host)
            if (
                not self._rates.low(host, now)
                or last is None
                or (now - last).total_seconds() >= slow_after
            ):
                allowed.add(pr)
        self._deferred = wanted - allowed
        return allowed

    def _finish_refresh(self, *, listed: bool | None = None) -> None:
        self._busy = False
        if self._cycle_listed if listed is None else listed:
            self._last_refresh = self.clock()
        flagged = any(state.refreshing for state in self._states.values())
        for state in self._states.values():
            state.refreshing = False
        if self.is_running:
            if flagged:
                self._apply_filter()  # also paints the bar
            else:
                self._paint_bar()

    def _state(self, row: Row) -> columns.RowState:
        return self._states.get(row.pr) or columns.RowState()

    def _apply_filter(self) -> None:
        """Rebuild the visible rows; the highlighted PR stays highlighted while it is shown.

        When only cell text changed (a snapshot arrived) the prompts are replaced in place, so
        the scroll position does not move.
        """
        kept = self._highlighted_row()
        needle = self._filter.strip().lower()
        try:
            ordered = columns.order(self._rows, self._states, self._order_mode, self._sort)
            visible = [
                r
                for r in ordered
                if columns.matches(r, self._state(r), self._list_filters)
                and (not needle or needle in render.row_haystack(r))
            ]
            now = self.clock()
            parts = [columns.cells(r, self._state(r), now) for r in visible]
            width = min(columns.PR_MAX_WIDTH, max((cell_len(p[0]) for p in parts), default=0))
            width = max(width, cell_len(columns.COLUMNS[0].header) + 1)  # room for an arrow
            self._pr_width = width
            self._painted_view = self._view_width()
            self._hoffset = columns.clamp_offset(self._hoffset, width, self._painted_view)
            self._paint_header()
            cuts = [columns.cut_row(p, width) for p in parts]
            prompts = [self._prompt(c, self._state(r)) for c, r in zip(cuts, visible, strict=True)]
        except Exception as exc:  # bad data reaches the status line, not a traceback
            self._list_note = self._internal(exc)
            self._status("")
            return
        self._visible = visible
        self._cuts = cuts
        olist = self.query_one("#list", OptionList)
        prs = [r.pr for r in visible]
        if prs == self._shown_prs and olist.option_count == len(prs):
            for i, prompt in enumerate(prompts):
                olist.replace_option_prompt_at_index(i, prompt)
        else:
            self._shown_prs = prs
            scroll = olist.scroll_y
            olist.clear_options()
            olist.add_options([Option(p) for p in prompts])
            if visible:
                index = prs.index(kept.pr) if kept and kept.pr in prs else 0
                olist.highlighted = index
                if kept and kept.pr in prs and scroll <= index < scroll + olist.size.height:
                    # the same PR is still highlighted: put the viewport back where it was
                    self.call_after_refresh(olist.scroll_to, y=scroll, animate=False)
        # the scrollbar appears or goes away only after the rows were replaced
        self.call_after_refresh(self.relayout)
        self._paint_bar()

    def _paint_bar(self) -> None:
        parts = [f"{len(self._visible)}/{len(self._rows)} PRs", f"order: {self._order_text()}"]
        rates = self._rates.text(self.clock())
        if rates:  # early, so a narrow terminal clips the later parts first
            parts.append(rates)
        if self._list_filters:
            parts.append(f"filters: {columns.filters_text(self._list_filters)}")
        if self._snap_total:
            failed = sum(1 for s in self._states.values() if s.status == columns.FAILED)
            progress = f"snapshots {self._snap_done}/{self._snap_total}"
            parts.append(progress + (f" ({failed} failed)" if failed else ""))
        stale = sum(1 for st in self._states.values() if st.stale)
        if stale:
            parts.append(f"{stale} stale")
        if not self._list_complete:
            noun = "error" if self._list_errors == 1 else "errors"
            parts.append(f"partial: {self._list_errors} {noun}")
        if self._last_refresh is not None:
            parts.append(f"refreshed {self._last_refresh.strftime('%H:%M:%S')}")
        if self.refresh_interval:
            parts.append(f"auto {self.refresh_interval:g}s")
        self.query_one("#bar", Static).update(safe.plain(safe.line(" | ".join(parts), 500)))

    # -- snapshots: one per row, loaded progressively -----------------------------------------

    def _snapshot_order(self) -> list[str]:
        """The rows in the viewport first, then the other rows in display order.

        Every row is one line, so the scroll offset is an index into the shown rows. Rows the
        filters hide come last, in the order they would be shown.
        """
        olist = self.query_one("#list", OptionList)
        start = max(0, int(olist.scroll_y))
        height = max(1, olist.size.height)
        shown = list(self._shown_prs)
        first = shown[start : start + height]
        taken = set(first)
        rest = [p for p in shown if p not in taken]
        seen = set(shown)
        hidden_rows = [r for r in self._rows if r.pr not in seen]
        hidden = [
            r.pr for r in columns.order(hidden_rows, self._states, self._order_mode, self._sort)
        ]
        return first + rest + hidden

    def _start_snapshots(
        self, wanted: set[str] | None, skip: str | None = None, listed: bool | None = None
    ) -> bool:
        """Start a round of per-row snapshots (all rows, or those in ``wanted``, but not
        ``skip``); False when there is nothing to load. ``listed=False`` marks a round that
        must not move the refresh time (a single-row refresh)."""
        self._snap_token += 1
        self._snap_done = 0
        order = [
            p
            for p in self._snapshot_order()
            if p not in self._unlisted and p != skip and (wanted is None or p in wanted)
        ]
        self._snap_total = len(order) if self.concurrency else 0
        self._busy = True
        now = self.clock()
        flagged = False
        for pr in order:
            self._host_last[host_of(pr)] = now
            state = self._states.get(pr)
            if self._snap_total and state is not None and state.status == columns.LOADED:
                state.refreshing = flagged = True  # the last good data stays on screen meanwhile
        if flagged:
            self._apply_filter()
        else:
            self._paint_bar()
        if order and self.concurrency:
            self.run_worker(
                self._load_snapshots(self._snap_token, order, listed), group="snapshots"
            )
            return True
        return False

    def _read_snapshot(self, pr: str, token: int) -> dict[str, Any]:
        """Runs on a worker thread. The source is told when a newer round replaced this one,
        so a read that has not started yet is skipped; one that has started runs to its end."""

        def cancelled() -> bool:
            return token != self._snap_token

        if self._source_cancels:
            return self.source.snapshot(pr, cancelled=cancelled)  # type: ignore[call-arg]
        return self.source.snapshot(pr)

    @staticmethod
    def _failed_state(previous: columns.RowState | None, error: str) -> columns.RowState:
        """A failed read keeps the last good data and marks it stale; with none, it is failed."""
        if previous is not None and previous.status == columns.LOADED and previous.summary:
            return columns.RowState(columns.LOADED, previous.summary, error=error, stale=True)
        return columns.RowState(columns.FAILED, error=error)

    async def _load_snapshots(
        self, token: int, order: list[str], listed: bool | None = None
    ) -> None:
        """At most ``self.concurrency`` snapshot reads at once; a failure marks only its row."""
        queue = deque(order)

        async def lane() -> None:
            while queue and token == self._snap_token:
                pr = queue.popleft()
                async with self._snap_slots:  # held until the read ends, even after a reload
                    if token != self._snap_token:
                        return
                    try:
                        snap = await in_thread(self._read_snapshot, pr, token)
                        state = columns.RowState(columns.LOADED, columns.summarize(snap))
                        self._rates.observe(snap.get("rate_limit"), self.clock())
                    except Cancelled:
                        return  # a newer round replaced this one before the read started
                    except ForgectlError as err:
                        state = self._failed_state(self._states.get(pr), safe.line(err.code, 60))
                    except Exception as exc:  # a source bug or bad data marks the row only
                        state = self._failed_state(self._states.get(pr), self._internal(exc))
                if token != self._snap_token:  # a newer round replaced this one
                    return
                self._snap_done += 1
                self._snapshot_arrived(pr, state)

        try:
            await asyncio.gather(*(lane() for _ in range(self.concurrency)))
        finally:
            if token == self._snap_token:
                self._finish_refresh(listed=listed)
                stale = sum(1 for st in self._states.values() if st.stale)
                if stale:
                    self._status(f"{stale} snapshot(s) not refreshed; showing the last good data")

    def _snapshot_arrived(self, pr: str, new: columns.RowState) -> None:
        """Store a row's new state. Only that row's text is redrawn unless the snapshot moves
        it: into or out of the filtered list, or to another place in the attention order."""
        row = self._row_by_pr.get(pr)
        old = self._state(row) if row is not None else columns.RowState()
        if pr in self._added and new.status == columns.LOADED:
            self._added.discard(pr)
            new.changed = True  # a PR that was not listed before
        elif (
            old.status == columns.LOADED
            and new.status == columns.LOADED
            and not new.stale
            and old.summary != new.summary
        ):
            new.changed = True
        if old.changed and not new.stale:
            new.changed = True  # a second read of identical data must not lose the highlight
        self._states[pr] = new
        if row is None:
            self._paint_bar()
            return
        moves = columns.matches(row, old, self._list_filters) != columns.matches(
            row, new, self._list_filters
        )
        if self._sort:
            moves = moves or columns.sort_values(self._sort, row, old) != columns.sort_values(
                self._sort, row, new
            )
        elif self._order_mode == columns.SORT_ATTENTION:
            moves = moves or columns.attention_rank(old) != columns.attention_rank(new)
        if moves:
            # a burst of arrivals rebuilds the list once, after they were all stored
            if not self._rebuild_pending:
                self._rebuild_pending = True
                self.call_after_refresh(self._rebuild)
            return
        if pr not in self._shown_prs:  # hidden before and after: nothing to draw
            self._paint_bar()
            return
        olist = self.query_one("#list", OptionList)
        index = self._shown_prs.index(pr)
        try:
            cut = columns.cut_row(columns.cells(row, new, self.clock()), self._pr_width)
            self._cuts[index] = cut
            olist.replace_option_prompt_at_index(index, self._prompt(cut, new))
        except Exception as exc:  # bad data reaches the status line, not a traceback
            self._list_note = self._internal(exc)
            self._status("")
        self._paint_bar()

    def _rebuild(self) -> None:
        self._rebuild_pending = False
        self._apply_filter()

    def _prompt(self, cut: columns.Cut, state: columns.RowState) -> Content:
        """One row's text; a changed row is also bold."""
        content = safe.plain(columns.view(cut, self._pr_width, self._hoffset, self._view_width()))
        return content.stylize("bold", 0, len(content)) if state.changed else content

    def _view_width(self) -> int:
        """Cells the rows can use: the list's content without its scrollbar; the whole table
        before the list has a size."""
        olist = self.query_one("#list", OptionList)
        return olist.scrollable_content_region.width if olist.size.width > 0 else 10_000

    def _paint_header(self) -> None:
        view = self._view_width()
        line = columns.header_line(self._pr_width, self._hoffset, view, self._sort)
        content = safe.plain(line)
        header = self.query_one("#header", HeaderBar)
        if header.has_focus:  # the cursor: the highlighted column name
            span = columns.column_span(self._cursor, self._pr_width, self._hoffset, view)
            if span is not None:
                content = content.stylize("reverse", span[0], span[1])
        header.update(content)

    # -- sorting ---------------------------------------------------------------------------

    def _order_text(self) -> str:
        return columns.sort_text(self._sort) or self._order_mode

    def set_sort(self, sort: columns.SortState) -> None:
        """The one way to change the sort (the header, and later the query's ORDER BY): rebuild
        the list, keep the highlighted PR, and name the order in the status."""
        self._sort = sort
        self._apply_filter()
        self._status(f"order: {self._order_text()}")

    def _sortable_cursor(self) -> str:
        return self._cursor if self._cursor in columns.SORTABLE else columns.SORTABLE[0]

    def action_header_focus(self) -> None:
        self._cursor = self._sort[0].column if self._sort else self._sortable_cursor()
        header = self.query_one("#header", HeaderBar)
        if header.has_focus:
            self.header_focused()
        else:
            header.focus()  # on_focus reveals the cursor and draws

    def _reveal_cursor(self) -> None:
        self._hoffset = columns.reveal_offset(
            self._cursor, self._pr_width, self._hoffset, self._view_width()
        )

    def header_focused(self) -> None:
        """The header took focus, by ``H`` or by tab: bring the cursor's column into view."""
        self._cursor = self._sortable_cursor()
        self._reveal_cursor()
        self._redraw()

    def header_move(self, direction: int) -> None:
        ids = columns.SORTABLE
        index = ids.index(self._sortable_cursor()) + direction
        self._cursor = ids[max(0, min(index, len(ids) - 1))]
        self._reveal_cursor()
        self._redraw()

    def header_sort(self) -> None:
        self.set_sort(columns.toggle_sort(self._sort, self._sortable_cursor()))

    def header_leave(self) -> None:
        self.query_one("#list", OptionList).focus()
        self._paint_header()

    def header_click(self, x: int) -> None:
        """A click at cell ``x`` of the header text (0 is its left edge)."""
        column = columns.column_at(x, self._pr_width, self._hoffset, self._view_width())
        self.query_one("#list", OptionList).focus()  # the keyboard stays with the list
        if column is None:
            return
        if column not in columns.SORTABLE:
            return  # the note is a status: a click on it does nothing
        self._cursor = column
        self.set_sort(columns.toggle_sort(self._sort, column))

    def _redraw(self) -> None:
        """Draw the header and the rows again for the current offset and view width, from the
        cells already built: no reorder, no refilter, the highlight and scroll stay."""
        olist = self.query_one("#list", OptionList)
        view = self._view_width()
        self._hoffset = columns.clamp_offset(self._hoffset, self._pr_width, view)
        self._paint_header()
        if olist.option_count != len(self._cuts):
            # the rows are being rebuilt; _apply_filter draws them. _painted_view stays as it
            # was, so the next relayout still sees the rows as out of date.
            return
        self._painted_view = view
        try:
            for i, (cut, row) in enumerate(zip(self._cuts, self._visible, strict=True)):
                olist.replace_option_prompt_at_index(i, self._prompt(cut, self._state(row)))
        except Exception as exc:  # bad data reaches the status line, not a traceback
            self._list_note = self._internal(exc)
            self._status("")

    def relayout(self) -> None:
        """The list was resized or got a scrollbar: redraw only when its usable width changed."""
        if self.is_running and self._loaded_once and self._view_width() != self._painted_view:
            self._redraw()

    def scroll_columns(self, direction: int) -> None:
        """Left (-1) or right (+1) by one column; the highlight and the vertical scroll stay."""
        view = self._view_width()
        new = columns.step_offset(self._hoffset, direction, self._pr_width, view)
        if new != self._hoffset:
            self._hoffset = new
            self._redraw()

    def _highlighted_row(self) -> Row | None:
        index = self.query_one("#list", OptionList).highlighted
        if index is None or not 0 <= index < len(self._visible):
            return None
        return self._visible[index]

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._filter = event.value
            self._apply_filter()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            self.query_one("#list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "list":
            return
        row = self._highlighted_row()
        if row is not None:
            self._start_detail(row.pr)

    # -- detail ---------------------------------------------------------------------------

    def _detail_shown(self) -> bool:
        return bool(self.query_one("#detail", VerticalScroll).display)

    def _start_detail(self, pr: str) -> None:
        if pr == self._detail_loading:  # the same PR is already on its way
            self._status(f"already loading {pr}")
            return
        self._detail_token += 1
        self._detail_loading = pr
        self.run_worker(self._load_detail(pr, self._detail_token), group="detail")

    async def _load_detail(self, pr: str, token: int) -> None:
        self._status("loading detail...")
        detail: render.Detail | None = None
        diff_note: str | None = None
        outcome = ""
        try:
            snap = await in_thread(self.source.snapshot, pr)
            self._row_refreshed(pr, snap)
            try:
                diff: dict[str, Any] | None = await in_thread(self.source.diff, pr)
            except ForgectlError as err:
                diff = None
                diff_note = f"diff: {err.code}: {err.message}"
            # Sanitizing and laying out a large pane happens off the event loop.
            detail = await in_thread(render.detail_of, snap, diff)
        except ForgectlError as err:
            outcome = f"{err.code}: {err.message}"
            self._row_failed(pr, safe.line(err.code, 60))
        except Exception as exc:  # a source bug or bad data must not take the UI down
            outcome = self._internal(exc)
            self._row_failed(pr, outcome)
        finally:
            if token == self._detail_token:
                self._detail_loading = None
        if token != self._detail_token:  # a newer request replaced this one
            return
        if detail is not None:
            try:
                self._show_detail(pr, detail, diff_note)
                return
            except Exception as exc:
                outcome = self._internal(exc)
        self._detail_note = outcome
        self._status(outcome)

    def _row_refreshed(self, pr: str, snap: dict[str, Any]) -> None:
        """The detail's snapshot is also the row's fresh data."""
        if pr not in self._row_by_pr:
            return
        try:
            state = columns.RowState(columns.LOADED, columns.summarize(snap))
            self._rates.observe(snap.get("rate_limit"), self.clock())
        except Exception:  # noqa: BLE001 - the detail's own handling reports bad data
            return
        self._snapshot_arrived(pr, state)

    def _row_failed(self, pr: str, error: str) -> None:
        """A failed detail read marks its row: stale, or failed if it never loaded."""
        if pr in self._row_by_pr:
            self._snapshot_arrived(pr, self._failed_state(self._states.get(pr), error))

    def _show_detail(self, pr: str, detail: render.Detail, diff_note: str | None) -> None:
        if pr != self._detail_pr:  # another PR starts on its first check
            self.query_one("#checks", OptionList).highlighted = None
        self._detail_pr = pr
        self._detail_url = detail.url
        self._detail_checks = detail.checks
        if not self._opened:
            self.selected = pr  # enter opened it; the intent stays back
        self._detail = detail
        self._painted.clear()
        for name in sections.NAMES:
            self._paint_section(name)
        pane = self.query_one("#detail", VerticalScroll)
        pane.display = True
        focused = self.focused
        if focused is None or focused.id == "list":  # never take focus from the filter
            pane.focus()
        elif focused.id == "checks" and not focused.display:  # a refresh dropped the checks
            pane.focus()
        self._detail_note = f"{len(detail.checks)} checks" + (f"; {diff_note}" if diff_note else "")
        self._status(self._detail_note)

    def _paint_section(self, name: str) -> None:
        """The header always; the body only while the section is open (and only once per
        detail), so a collapsed diff costs the screen nothing."""
        detail = self._detail
        is_open = name in self._open_sections
        count = detail.counts.get(name) if detail is not None else None
        self.query_one(f"#head-{name}", Static).update(
            safe.plain(sections.header(name, is_open, count))
        )
        body = self.query_one(f"#{name}", Static)
        body.display = is_open
        if name == "ci":
            checks = self.query_one("#checks", OptionList)
            checks.display = is_open and bool(detail and detail.checks)
        if not is_open or detail is None or name in self._painted:
            return
        self._painted.add(name)
        body.update(style.body(name, detail.texts, detail.lines, self._dark()))
        if name == "ci":
            checks = self.query_one("#checks", OptionList)
            kept = checks.highlighted  # a repaint (a theme change) keeps the selected check
            checks.clear_options()
            dark = self._dark()
            checks.add_options([Option(style.check_label(x, dark)) for x in detail.check_labels])
            if detail.checks:
                checks.highlighted = min(kept or 0, len(detail.check_labels) - 1)

    def _dark(self) -> bool:
        """Whether the app theme is dark: the colors of the detail pane follow it."""
        return bool(self.current_theme.dark)

    def watch_theme(self) -> None:
        """A new theme repaints the open sections in its colors."""
        if self._detail is not None and self.is_mounted:
            self._painted.clear()
            for name in sections.NAMES:
                self._paint_section(name)

    def _pane_has_focus(self) -> bool:
        focused = self.focused
        return focused is not None and any(
            node.id == "detail" for node in focused.ancestors_with_self
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "toggle_section":
            return self._pane_has_focus() and self._detail_shown()
        return True

    def action_toggle_section(self, name: str) -> None:
        if name not in sections.NAMES:
            return
        if name in self._open_sections:
            self._open_sections.discard(name)
            focused = self.focused
            if focused is not None and focused.id == "checks" and name == "ci":
                self.query_one("#detail", VerticalScroll).focus()  # a hidden list keeps no focus
        else:
            self._open_sections.add(name)
        self._paint_section(name)

    # -- actions --------------------------------------------------------------------------

    def action_quit_app(self) -> None:
        if self.refresh_timer is not None:
            self.refresh_timer.stop()
            self.refresh_timer = None
        self._snap_token += 1  # reads that have not started yet are skipped
        self.exit()

    def action_sort(self) -> None:
        if self._sort:  # back to the preset a column sort replaced, without advancing
            self._sort = ()
        else:
            modes = columns.SORT_MODES
            self._order_mode = modes[(modes.index(self._order_mode) + 1) % len(modes)]
        self._apply_filter()
        self._status(f"order: {self._order_text()}")

    def action_toggle_filter(self, name: str) -> None:
        self._list_filters ^= {name}
        self._apply_filter()
        self._status(f"filters: {columns.filters_text(self._list_filters) or 'none'}")

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.display = True
        box.focus()

    def action_close(self) -> None:
        box = self.query_one("#filter", Input)
        if box.display:
            box.value = ""
            box.display = False
            self._filter = ""
            self._apply_filter()
            self.query_one("#list", OptionList).focus()
            return
        detail = self.query_one("#detail", VerticalScroll)
        if detail.display:
            detail.display = False
            self.query_one("#list", OptionList).focus()

    def action_refresh_selected(self) -> None:
        """The PR in the visible detail pane (row and detail), else only the highlighted row.
        With the pane closed it never opens it."""
        if self._detail_pr is not None and self._detail_shown():
            self._start_detail(self._detail_pr)
            return
        row = self._highlighted_row()
        if row is None:
            self._status("nothing to refresh: no PR highlighted")
            return
        if self._detail_loading == row.pr:  # a detail load for it is on its way
            self._status("a refresh is already running")
            return
        if self._busy:  # a round of snapshots is running; it covers this row
            self._status("a refresh is already running")
            return
        if not self._start_snapshots({row.pr}, listed=False):
            self._finish_refresh(listed=False)
            self._status("not refreshed: snapshots are off or the PR is not listed")

    def action_reload_list(self) -> None:
        """R: `prs`, then every row's snapshot, then the PR in the detail pane. Never started
        twice: while one is in flight R is ignored. It reads every row, so the next timed
        cycle counts from here, and it does not wait for a host's rate-limit backoff."""
        if self._busy:
            self._status("a reload is already running")
            return
        self._auto_cycles = 0
        self._start_list(refresh_detail=True)

    def action_open(self) -> None:
        url: object
        pr: str | None
        focused = self.focused
        is_check = focused is not None and focused.id == "checks" and "ci" in self._open_sections
        if is_check:
            index = self.query_one("#checks", OptionList).highlighted
            if index is None or not 0 <= index < len(self._detail_checks):
                self._status("not opened: no check highlighted")
                return
            url, pr = self._detail_checks[index].get("url"), self._detail_pr
        elif self._detail_pr is not None and self._detail_shown():
            url, pr = self._detail_url, self._detail_pr
        else:
            row = self._highlighted_row()
            if row is None:
                self._status("not opened: no PR highlighted")
                return
            url, pr = row.item.get("url"), row.pr
        # The PR's own url is built by forgectl and is https; a check's is http or https.
        if not isinstance(url, str) or not is_openable(url):
            self._status("not opened: no http or https url")
            return
        if not is_check and not url.startswith("https://"):
            self._status("not opened: the PR url is not https")
            return
        self.run_worker(self._open(url, pr), group="open")

    async def _open(self, url: str, pr: str | None) -> None:
        try:
            program = await in_thread(self.opener.open, url, self._opener_exited)
        except OpenRefused as err:
            self._status(f"not opened: {err}")
            return
        except Exception as exc:
            self._status(f"not opened: {type(exc).__name__}")
            return
        # intent open means `o` was used and the opener was launched; whether a browser
        # showed the page is the opener's business.
        self.intent = "open"
        self._opened = True
        if pr is not None:
            self.selected = pr
        self._status(f"launched {program} for {url}")

    def _opener_exited(self, status: int) -> None:
        """Called from the opener's reaper thread when it exits non-zero."""
        if not self.is_running:
            return
        try:
            self.call_from_thread(self._status, f"opener exited {status}")
        except Exception:  # noqa: BLE001 - the app may be shutting down; the note is dropped
            pass


def _accepts_cancelled(source: object) -> bool:
    """Whether ``source.snapshot`` takes the ``cancelled`` callable (``LiveSource`` does)."""
    try:
        return "cancelled" in inspect.signature(source.snapshot).parameters  # type: ignore[attr-defined]
    except (TypeError, ValueError, AttributeError):
        return False
