"""The `browse` app: a list, a detail pane, and one action (open in a browser). Read-only.

There is no write action here: no merge (TODO(#60)), no post. Forge text reaches the screen
only through ``safe`` and ``render``, as ``Content`` that is never parsed as markup.
"""

from __future__ import annotations

import asyncio
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
r       refresh the open PR
R       reload the list

browse is read-only: no merge, no post.
Press any key to close this help."""


class HelpScreen(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        yield Static(safe.plain(HELP), id="help", markup=False)

    def on_key(self, event: Key) -> None:
        event.stop()
        self.dismiss(None)


class BrowseApp(App[None]):
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
        self._detail_pr: str | None = None
        self._detail_url: str | None = None
        self._detail_checks: list[dict[str, Any]] = []
        self._detail_token = 0

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
        self.run_worker(self._load_list(), group="list", exclusive=True)
        if self.focus_pr:
            self.run_worker(self._load_detail(self.focus_pr), group="detail", exclusive=True)

    # -- status ---------------------------------------------------------------------------

    def _status(self, message: str) -> None:
        self.query_one("#status", Static).update(safe.plain(safe.line(message, 500)))

    # -- list -----------------------------------------------------------------------------

    async def _load_list(self) -> None:
        if self._loading:
            self._status("a reload is already running")
            return
        self._loading = True
        self._status("loading...")
        try:
            listing: Listing = await asyncio.to_thread(load_list, self.source)
        except Exception as exc:  # a source bug must not take the UI down
            self._status(f"load failed: {type(exc).__name__}")
            return
        finally:
            self._loading = False
        self._rows = listing.rows
        self._apply_filter()
        notes = list(listing.errors)
        if not listing.complete:
            notes.append("list incomplete")
        self._status("; ".join(notes) if notes else f"{len(self._rows)} PRs")

    def _apply_filter(self) -> None:
        needle = self._filter.strip().lower()
        self._visible = [r for r in self._rows if not needle or needle in render.row_haystack(r)]
        olist = self.query_one("#list", OptionList)
        olist.clear_options()
        olist.add_options([Option(safe.plain(render.row_text(r))) for r in self._visible])
        if self._visible:
            olist.highlighted = 0

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
            self.run_worker(self._load_detail(row.pr), group="detail", exclusive=True)

    # -- detail ---------------------------------------------------------------------------

    async def _load_detail(self, pr: str) -> None:
        self._detail_token += 1
        token = self._detail_token
        self._status("loading detail...")
        try:
            snap = await asyncio.to_thread(self.source.snapshot, pr)
            try:
                diff: dict[str, Any] | None = await asyncio.to_thread(self.source.diff, pr)
            except ForgectlError as err:
                diff = None
                self._status(f"diff: {err.code}: {err.message}")
        except ForgectlError as err:
            self._status(f"{err.code}: {err.message}")
            return
        except Exception as exc:
            self._status(f"load failed: {type(exc).__name__}")
            return
        if token != self._detail_token:  # a newer request replaced this one
            return
        self._show_detail(pr, snap, diff)

    def _show_detail(self, pr: str, snap: dict[str, Any], diff: dict[str, Any] | None) -> None:
        self._detail_pr = pr
        url = snap.get("url")
        self._detail_url = url if isinstance(url, str) else None
        self._detail_checks = render.checks_of(snap)
        self.selected = pr  # enter opened it; the intent stays back
        self.query_one("#facts", Static).update(safe.plain(render.facts_text(snap)))
        checks = self.query_one("#checks", OptionList)
        checks.clear_options()
        checks.add_options([Option(safe.plain(render.check_label(c))) for c in self._detail_checks])
        checks.display = bool(self._detail_checks)
        self.query_one("#rest", Static).update(safe.plain(render.rest_text(snap, diff)))
        detail = self.query_one("#detail", VerticalScroll)
        detail.display = True
        detail.focus()
        self._status(f"{len(self._detail_checks)} checks")

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
        # TODO(#59): refresh only what may have changed, and the automatic refresh.
        if self._detail_pr is None:
            self._status("nothing to refresh: press enter on a PR first")
            return
        self.run_worker(self._load_detail(self._detail_pr), group="detail", exclusive=True)

    def action_reload_list(self) -> None:
        self.run_worker(self._load_list(), group="list", exclusive=True)

    def action_open(self) -> None:
        url: object
        pr: str | None
        focused = self.focused
        is_check = focused is not None and focused.id == "checks"
        if is_check:
            index = self.query_one("#checks", OptionList).highlighted
            if index is None or not 0 <= index < len(self._detail_checks):
                return
            url, pr = self._detail_checks[index].get("url"), self._detail_pr
        elif self._detail_pr is not None and self.query_one("#detail").display:
            url, pr = self._detail_url, self._detail_pr
        else:
            row = self._highlighted_row()
            if row is None:
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
            await asyncio.to_thread(self.opener.open, url)
        except OpenRefused as err:
            self._status(f"not opened: {err}")
            return
        except Exception as exc:
            self._status(f"not opened: {type(exc).__name__}")
            return
        self.intent = "open"
        if pr is not None:
            self.selected = pr
        self._status("opened in the browser")
