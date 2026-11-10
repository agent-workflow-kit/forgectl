"""The `browse` app: a list, a detail pane, and one action (open in a browser). Read-only.

There is no write action here: no merge (todo(#60)), no post. Forge text reaches the screen
only through ``safe`` and ``render``, as ``Content`` that is never parsed as markup.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from forgectl.errors import ForgectlError

from . import columns, render, safe
from .opener import Opener, OpenRefused, is_openable
from .source import DataSource, Listing, Row, load_list

DEFAULT_CONCURRENCY = 4  # snapshots in flight at once; a small default
# todo(#59): config key for snapshot concurrency (and its schema entry)

HELP = """\
q       quit
?       this help
/       filter the list (enter keeps it, escape clears it)
enter   open the detail pane for the highlighted PR
esc     close the detail pane or the filter
o       open the PR in a browser; on a check in the detail pane, open the check
        (only an http or https url is opened)
s       switch the order: by repo (default) or needs-attention first
a       filter: authored by me
v       filter: review requested from me
f       filter: CI failing
n       filter: new comments from others
        (filters combine as an intersection; the status bar names them)
r       refresh selected (the PR in the detail pane, else the highlighted PR)
R       refresh all (both lists, then the PR in the detail pane)

columns: PR  badge  CI  approvals  review  comments  age  title
  CI: ✓ passing, ✗ failing, … pending, - none, · loading, ! snapshot failed
  comments: c total, u unresolved threads, n new from others since the push,
            r threads awaiting my reply
  age: since the last push / since the last update

browse is read-only: no merge, no post.
Press any key to close this help."""


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


class HelpScreen(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        yield Static(safe.plain(HELP), id="help", markup=False)

    def on_key(self, event: Key) -> None:
        event.stop()
        self.dismiss(None)


class BrowseApp(App[None]):
    ENABLE_COMMAND_PALETTE = False  # its screenshot command would write forge text to a file
    CSS = """
    #main { height: 1fr; }
    #left { width: 3fr; }
    #detail { width: 2fr; display: none; border-left: solid $primary; padding: 0 1; }
    #filter { display: none; }
    #list { text-wrap: nowrap; text-overflow: ellipsis; }
    #checks { height: auto; max-height: 12; }
    #footer { dock: bottom; height: 2; }
    #bar, #status { height: 1; }
    HelpScreen { align: center middle; }
    #help { width: 76; height: auto; border: round $primary; padding: 1 2; background: $surface; }
    """
    BINDINGS = [
        Binding("q", "quit_app", "quit"),
        Binding("question_mark", "help", "help"),
        Binding("slash", "filter", "filter"),
        Binding("escape", "close", "close"),
        Binding("o", "open", "open"),
        Binding("s", "sort", "order"),
        Binding("a", "toggle_filter('authored')", "authored"),
        Binding("v", "toggle_filter('requested')", "requested"),
        Binding("f", "toggle_filter('ci-failing')", "ci failing"),
        Binding("n", "toggle_filter('new-comments')", "new comments"),
        Binding("r", "refresh_selected", "refresh"),
        Binding("R", "reload_list", "reload"),
    ]

    def __init__(
        self,
        source: DataSource,
        opener: Opener,
        *,
        focus: str | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        clock: Callable[[], datetime] = columns.utc_now,
    ) -> None:
        super().__init__()
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
        self._order_mode = columns.SORT_REPO
        self._list_filters: set[str] = set()
        self._shown_prs: list[str] = []  # the PRs in the option list, in order
        self._snap_token = 0  # a newer list load stops the snapshot pool of an older one
        # One limiter for every round: reads of an older round still in flight keep their slots,
        # so a reload cannot put more than ``concurrency`` snapshot reads on the wire.
        self._snap_slots = asyncio.Semaphore(max(1, self.concurrency))
        self._row_by_pr: dict[str, Row] = {}
        self._pr_width = 0
        self._snap_done = 0
        self._snap_total = 0
        self._filter = ""
        self._loading = False
        self._opened = False  # an `o` succeeded: `selected` is the PR it acted on
        self._list_note: str | None = None  # a pending list error, kept on the status line
        self._detail_note: str | None = None  # how the last detail load ended
        self._detail_pr: str | None = None
        self._detail_url: str | None = None
        self._detail_checks: list[dict[str, Any]] = []
        self._detail_token = 0
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
                yield OptionList(id="list")
            with VerticalScroll(id="detail"):
                yield Static(safe.plain(""), id="facts", markup=False)
                yield OptionList(id="checks")
                yield Static(safe.plain(""), id="rest", markup=False)
        with Vertical(id="footer"):
            yield Static(safe.plain(""), id="bar", markup=False)
            yield Static(safe.plain(""), id="status", markup=False)

    def on_mount(self) -> None:
        self.query_one("#list", OptionList).focus()
        self._start_list(refresh_detail=False)
        if self.focus_pr:
            self._start_detail(self.focus_pr)

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

    def _start_list(self, *, refresh_detail: bool) -> None:
        self._loading = True
        self.run_worker(self._load_list(refresh_detail), group="list")

    async def _load_list(self, refresh_detail: bool) -> None:
        self._status("loading...")
        try:
            try:
                listing: Listing = await in_thread(load_list, self.source)
            except Exception as exc:  # a source bug must not take the UI down
                self._list_note = self._internal(exc)
                self._paint(self._list_note, self._detail_note)
                return
            notes = list(listing.errors)
            if not listing.complete:
                notes.append("list incomplete")
            self._list_note = "; ".join(notes) if notes else None
            self._rows = listing.rows
            self._row_by_pr = {r.pr: r for r in self._rows}
            # todo(#59): keep a row's last good data while it refreshes, and mark it stale.
            self._states = {r.pr: columns.RowState() for r in self._rows}
            self._apply_filter()
            self._paint(self._list_note or f"{len(self._rows)} PRs", self._detail_note)
            self._start_snapshots()
        finally:
            self._loading = False
        if refresh_detail and self._detail_pr is not None and self._detail_shown():
            self._start_detail(self._detail_pr)

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
            ordered = columns.order(self._rows, self._states, self._order_mode)
            visible = [
                r
                for r in ordered
                if columns.matches(r, self._state(r), self._list_filters)
                and (not needle or needle in render.row_haystack(r))
            ]
            now = self.clock()
            parts = [columns.cells(r, self._state(r), now) for r in visible]
            width = min(60, max((cell_len(p[0]) for p in parts), default=0))
            prompts = [safe.plain(columns.row_line(p, width)) for p in parts]
        except Exception as exc:  # bad data reaches the status line, not a traceback
            self._list_note = self._internal(exc)
            self._status("")
            return
        self._visible = visible
        self._pr_width = width
        olist = self.query_one("#list", OptionList)
        prs = [r.pr for r in visible]
        if prs == self._shown_prs and olist.option_count == len(prs):
            for i, prompt in enumerate(prompts):
                olist.replace_option_prompt_at_index(i, prompt)
        else:
            self._shown_prs = prs
            olist.clear_options()
            olist.add_options([Option(p) for p in prompts])
            if visible:
                olist.highlighted = prs.index(kept.pr) if kept and kept.pr in prs else 0
        self._paint_bar()

    def _paint_bar(self) -> None:
        parts = [f"{len(self._visible)}/{len(self._rows)} PRs", f"order: {self._order_mode}"]
        if self._list_filters:
            parts.append(f"filters: {columns.filters_text(self._list_filters)}")
        if self._snap_total:
            failed = sum(1 for s in self._states.values() if s.status == columns.FAILED)
            progress = f"snapshots {self._snap_done}/{self._snap_total}"
            parts.append(progress + (f" ({failed} failed)" if failed else ""))
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
        hidden = [r.pr for r in columns.order(hidden_rows, self._states, self._order_mode)]
        return first + rest + hidden

    def _start_snapshots(self) -> None:
        self._snap_token += 1
        self._snap_done = 0
        self._snap_total = len(self._rows) if self.concurrency else 0
        self._paint_bar()
        if self._rows and self.concurrency:
            self.run_worker(
                self._load_snapshots(self._snap_token, self._snapshot_order()), group="snapshots"
            )

    async def _load_snapshots(self, token: int, order: list[str]) -> None:
        """At most ``self.concurrency`` snapshot reads at once; a failure marks only its row."""
        queue = deque(order)

        async def lane() -> None:
            while queue and token == self._snap_token:
                pr = queue.popleft()
                async with self._snap_slots:  # held until the read ends, even after a reload
                    if token != self._snap_token:
                        return
                    try:
                        snap = await in_thread(self.source.snapshot, pr)
                        state = columns.RowState(columns.LOADED, columns.summarize(snap))
                    except ForgectlError as err:
                        state = columns.RowState(columns.FAILED, error=safe.line(err.code, 60))
                    except Exception as exc:  # a source bug or bad data marks the row only
                        state = columns.RowState(columns.FAILED, error=self._internal(exc))
                if token != self._snap_token:  # a newer list replaced this one
                    return
                self._snapshot_arrived(pr, state)

        await asyncio.gather(*(lane() for _ in range(self.concurrency)))

    def _snapshot_arrived(self, pr: str, new: columns.RowState) -> None:
        """Store a row's new state. Only that row's text is redrawn unless the snapshot moves
        it: into or out of the filtered list, or to another place in the attention order."""
        old = self._state(self._row_by_pr[pr]) if pr in self._row_by_pr else columns.RowState()
        self._states[pr] = new
        self._snap_done += 1
        row = self._row_by_pr.get(pr)
        if row is None:
            self._paint_bar()
            return
        moves = columns.matches(row, old, self._list_filters) != columns.matches(
            row, new, self._list_filters
        )
        if self._order_mode == columns.SORT_ATTENTION:
            moves = moves or columns.attention_rank(old) != columns.attention_rank(new)
        if moves:
            self._apply_filter()
            return
        if pr not in self._shown_prs:  # hidden before and after: nothing to draw
            self._paint_bar()
            return
        olist = self.query_one("#list", OptionList)
        index = self._shown_prs.index(pr)
        try:
            parts = columns.cells(row, new, self.clock())
            olist.replace_option_prompt_at_index(
                index, safe.plain(columns.row_line(parts, self._pr_width))
            )
        except Exception as exc:  # bad data reaches the status line, not a traceback
            self._list_note = self._internal(exc)
            self._status("")
        self._paint_bar()

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
            try:
                diff: dict[str, Any] | None = await in_thread(self.source.diff, pr)
            except ForgectlError as err:
                diff = None
                diff_note = f"diff: {err.code}: {err.message}"
            # Sanitizing and laying out a large pane happens off the event loop.
            detail = await in_thread(render.detail_of, snap, diff)
        except ForgectlError as err:
            outcome = f"{err.code}: {err.message}"
        except Exception as exc:  # a source bug or bad data must not take the UI down
            outcome = self._internal(exc)
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

    def _show_detail(self, pr: str, detail: render.Detail, diff_note: str | None) -> None:
        self._detail_pr = pr
        self._detail_url = detail.url
        self._detail_checks = detail.checks
        if not self._opened:
            self.selected = pr  # enter opened it; the intent stays back
        self.query_one("#facts", Static).update(safe.plain(detail.facts))
        checks = self.query_one("#checks", OptionList)
        checks.clear_options()
        checks.add_options([Option(safe.plain(label)) for label in detail.check_labels])
        if detail.checks:
            checks.highlighted = 0
        checks.display = bool(detail.checks)
        self.query_one("#rest", Static).update(safe.plain(detail.rest))
        pane = self.query_one("#detail", VerticalScroll)
        pane.display = True
        focused = self.focused
        if focused is None or focused.id == "list":  # never take focus from the filter
            pane.focus()
        self._detail_note = f"{len(detail.checks)} checks" + (f"; {diff_note}" if diff_note else "")
        self._status(self._detail_note)

    # -- actions --------------------------------------------------------------------------

    def action_quit_app(self) -> None:
        self.exit()

    def action_sort(self) -> None:
        modes = columns.SORT_MODES
        self._order_mode = modes[(modes.index(self._order_mode) + 1) % len(modes)]
        self._apply_filter()
        self._status(f"order: {self._order_mode}")

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
        """The PR in the visible detail pane, else the highlighted row's PR (opening it)."""
        # todo(#59): refresh only what may have changed, and the automatic refresh.
        if self._detail_pr is not None and self._detail_shown():
            self._start_detail(self._detail_pr)
            return
        row = self._highlighted_row()
        if row is None:
            self._status("nothing to refresh: no PR highlighted")
            return
        self._start_detail(row.pr)

    def action_reload_list(self) -> None:
        # todo(#59): R refreshes all: `prs`, then every row's snapshot; selection and scroll
        # position survive any refresh. Until the rows carry snapshots, R reloads both `prs`
        # calls and then the PR in the visible detail pane.
        if self._loading:
            self._status("a reload is already running")
            return
        self._start_list(refresh_detail=True)

    def action_open(self) -> None:
        url: object
        pr: str | None
        focused = self.focused
        is_check = focused is not None and focused.id == "checks"
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
