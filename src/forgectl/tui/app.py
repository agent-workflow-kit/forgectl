"""The `browse` app: a list, a detail pane, and one action (open in a browser). Read-only.

There is no write action here: no merge (TODO(#60)), no post. Forge text reaches the screen
only through ``safe`` and ``render``, as ``Content`` that is never parsed as markup.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from forgectl.errors import ForgectlError

from . import render, safe
from .opener import Opener, OpenRefused, is_openable
from .source import DataSource, Listing, Row, load_list

HELP = """\
q       quit
?       this help
/       filter the list (enter keeps it, escape clears it)
enter   open the detail pane for the highlighted PR
esc     close the detail pane or the filter
o       open the PR in a browser; on a check in the detail pane, open the check
        (only an http or https url is opened)
r       refresh selected (the PR in the detail pane, else the highlighted PR)
R       refresh all (both lists, then the PR in the detail pane)

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
    #left { width: 1fr; }
    #detail { width: 2fr; display: none; border-left: solid $primary; padding: 0 1; }
    #filter { display: none; }
    #checks { height: auto; max-height: 12; }
    #status { height: 1; dock: bottom; }
    HelpScreen { align: center middle; }
    #help { width: 76; height: auto; border: round $primary; padding: 1 2; background: $surface; }
    """
    BINDINGS = [
        Binding("q", "quit_app", "quit"),
        Binding("question_mark", "help", "help"),
        Binding("slash", "filter", "filter"),
        Binding("escape", "close", "close"),
        Binding("o", "open", "open"),
        Binding("r", "refresh_selected", "refresh"),
        Binding("R", "reload_list", "reload"),
    ]

    def __init__(
        self,
        source: DataSource,
        opener: Opener,
        *,
        focus: str | None = None,
    ) -> None:
        super().__init__()
        self.source = source
        self.opener = opener
        self.focus_pr = focus
        self.intent = "back"
        self.selected: str | None = None
        self.merged: list[str] = []  # TODO(#60): PRs merged from here; empty until `m` exists
        self._rows: list[Row] = []
        self._visible: list[Row] = []
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
            self._apply_filter()
            self._paint(self._list_note or f"{len(self._rows)} PRs", self._detail_note)
        finally:
            self._loading = False
        if refresh_detail and self._detail_pr is not None and self._detail_shown():
            self._start_detail(self._detail_pr)

    def _apply_filter(self) -> None:
        """Rebuild the visible rows; the highlighted PR stays highlighted while it is shown."""
        kept = self._highlighted_row()
        needle = self._filter.strip().lower()
        try:
            visible = [r for r in self._rows if not needle or needle in render.row_haystack(r)]
            options = [Option(safe.plain(render.row_text(r))) for r in visible]
        except Exception as exc:  # bad data reaches the status line, not a traceback
            self._list_note = self._internal(exc)
            self._status("")
            return
        self._visible = visible
        olist = self.query_one("#list", OptionList)
        olist.clear_options()
        olist.add_options(options)
        if visible:
            prs = [r.pr for r in visible]
            olist.highlighted = prs.index(kept.pr) if kept and kept.pr in prs else 0

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
        # TODO(#59): refresh only what may have changed, and the automatic refresh.
        if self._detail_pr is not None and self._detail_shown():
            self._start_detail(self._detail_pr)
            return
        row = self._highlighted_row()
        if row is None:
            self._status("nothing to refresh: no PR highlighted")
            return
        self._start_detail(row.pr)

    def action_reload_list(self) -> None:
        # TODO(#59): R refreshes all: `prs`, then every row's snapshot; selection and scroll
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
        except RuntimeError:  # the app stopped meanwhile
            pass
