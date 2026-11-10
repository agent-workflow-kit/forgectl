"""The query panel of `browse`: a form and a query line over one state, kept in step.

The query text is the state the list uses. Editing the line rewrites the form, and editing the form
rewrites the line, each only while the edit is valid: an invalid line or form row shows its error
and position, and the last valid text stays (it is what Apply uses and what is saved). Nothing here
touches the list until Apply; Cancel leaves the active query as it was.

The panel shows constant labels and the user's own query text, through ``safe.plain``, so nothing
is parsed as markup. Forge text is not shown here (only a count of rows).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Select, Static
from textual.widgets.option_list import Option

from . import query, safe

OP_LABELS = {
    query.EQ: "=",
    query.NE: "!=",
    query.LT: "<",
    query.LE: "<=",
    query.GT: ">",
    query.GE: ">=",
    query.LIKE: "LIKE",
    query.NOT_LIKE: "NOT LIKE",
    query.IN: "IN",
    query.NOT_IN: "NOT IN",
    query.IS_NULL: "IS NULL",
    query.NOT_NULL: "IS NOT NULL",
    query.TRUE: "is set",
    query.FALSE: "is not set",
}
_ID = re.compile(r"^([sf])(\d+)-(\d+)-([a-z]+)$")


@dataclass
class PanelActions:
    """What the panel needs from the app: the state to show and the things it can do. An action
    changes the app and says so on its status line; ``set_default`` also returns that text, which
    the panel shows too."""

    text: str  # the active query, canonical
    default: Callable[[], str]  # the configured default query (it changes with Set as default)
    history: Callable[[], list[str]]
    host_note: str  # the list-call filters, read-only
    apply: Callable[[str], None]
    clear: Callable[[], None]
    set_default: Callable[[str], str]
    reset_default: Callable[[], None]
    report: Callable[[query.Node | None], str]


class QueryScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    QueryScreen { align: center middle; }
    #qpanel {
        width: 95%; max-width: 110; height: auto; max-height: 100%;
        border: round $primary; padding: 0 1; background: $surface;
    }
    .q-head { text-style: bold; margin-top: 1; }
    #q-title { text-style: bold; }
    #q-status { height: auto; }
    #q-sort, #q-filters { height: auto; }
    .q-row { height: auto; }
    .q-row Select { width: 1fr; min-width: 8; }
    .q-row Input { width: 2fr; min-width: 8; }
    .q-row Button { min-width: 6; }
    #qpanel.narrow .q-row { layout: vertical; }
    #qpanel.narrow .q-row Select, #qpanel.narrow .q-row Input { width: 100%; }
    #q-history { height: 6; }
    #q-buttons { layout: grid; grid-size: 5; grid-gutter: 0 1; height: auto; margin-top: 1; }
    #q-buttons Button { width: 100%; min-width: 4; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def __init__(self, actions: PanelActions) -> None:
        super().__init__()
        self.actions = actions
        parsed = query.parse(actions.text)
        start = parsed if isinstance(parsed, query.Query) else query.Query()
        self.form = query.to_form(start)
        self.valid = query.format_query(start)  # the last valid text
        self.where: query.Node | None = start.where
        self.line_error: str | None = None
        self.form_error: str | None = None
        self._line_value = self.valid  # the text last put in the line by the panel itself
        self._gen = 0
        self._past = -1  # the position in the history while Up and Down step through it
        self._draft = ""

    # -- layout ---------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="qpanel"):
            yield Static(safe.plain("Query: filter and order the list"), id="q-title")
            yield Input(
                value=self.valid,
                placeholder="ci = failing AND age > 7d ORDER BY age DESC",
                id="q-line",
            )
            yield Static(safe.plain(""), id="q-status", markup=False)
            yield Static(safe.plain(self.actions.host_note), id="q-host", markup=False)
            yield Static(safe.plain(""), id="q-report", markup=False)
            yield Static(safe.plain(""), id="q-default-text", markup=False)
            yield Static(safe.plain("Sort keys, first key first"), classes="q-head")
            yield Vertical(id="q-sort")
            yield Button("Add sort key", id="q-add-sort")
            yield Static(safe.plain("Filters, all must hold"), classes="q-head")
            yield Vertical(id="q-filters")
            yield Button("Add filter", id="q-add-filter")
            yield Static(
                safe.plain("History, newest first (Up and Down in the query line)"),
                classes="q-head",
            )
            yield OptionList(id="q-history")
            with Grid(id="q-buttons"):
                yield Button("Apply", id="q-apply", variant="primary")
                yield Button("Cancel", id="q-cancel")
                yield Button("Reset", id="q-reset")
                yield Button("Set as default", id="q-default")
                yield Button("Reset to default", id="q-reset-default")

    async def on_mount(self) -> None:
        await self._render_form()
        self._fill_history()
        self._show_status()
        self._fit(self.size.width)
        self.query_one("#q-line", Input).focus()

    def on_resize(self, event: events.Resize) -> None:
        self._fit(event.size.width)

    def _fit(self, width: int) -> None:
        """Stack the form rows and wrap the buttons into fewer columns on a narrow terminal, so
        every control stays inside the screen."""
        if width <= 0:
            return
        self.query_one("#qpanel").set_class(width < 64, "narrow")
        self.query_one("#q-buttons", Grid).styles.grid_size_columns = (
            5 if width >= 100 else 3 if width >= 56 else 2
        )

    def _fill_history(self) -> None:
        olist = self.query_one("#q-history", OptionList)
        olist.clear_options()
        olist.add_options([Option(safe.plain(text)) for text in self.actions.history()])

    async def _render_form(self) -> None:
        self._gen += 1
        g = self._gen
        sort = self.query_one("#q-sort", Vertical)
        filters = self.query_one("#q-filters", Vertical)
        await sort.remove_children()
        await filters.remove_children()
        column_options = [(c, c) for c in query.ORDER_COLUMNS]
        dir_options = [("ascending", "asc"), ("descending", "desc")]
        rows = []
        for i, (column, descending) in enumerate(self.form.order):
            rows.append(
                Horizontal(
                    Select(column_options, value=column, allow_blank=False, id=f"s{g}-{i}-col"),
                    Select(
                        dir_options,
                        value="desc" if descending else "asc",
                        allow_blank=False,
                        id=f"s{g}-{i}-dir",
                    ),
                    Button("up", id=f"s{g}-{i}-up"),
                    Button("down", id=f"s{g}-{i}-down"),
                    Button("x", id=f"s{g}-{i}-del"),
                    classes="q-row",
                )
            )
        await sort.mount_all(rows)
        if self.form.filters is None:
            await filters.mount(
                Static(
                    safe.plain(
                        "This filter uses OR, NOT (...) or a group, which the form cannot show: "
                        "edit it in the query line."
                    ),
                    markup=False,
                )
            )
            return
        frows = []
        for i, f in enumerate(self.form.filters):
            kind = query.FIELDS[f.field]
            ops = [(OP_LABELS[op], op) for op in query.OPERATORS[kind]]
            value = Input(value=f.value, placeholder="value", id=f"f{g}-{i}-val")
            value.display = f.op not in query.NO_VALUE
            frows.append(
                Horizontal(
                    Select(
                        [(n, n) for n in query.FIELDS],
                        value=f.field,
                        allow_blank=False,
                        id=f"f{g}-{i}-field",
                    ),
                    Select(ops, value=f.op, allow_blank=False, id=f"f{g}-{i}-op", classes="q-op"),
                    value,
                    Button("x", id=f"f{g}-{i}-del"),
                    classes="q-row",
                )
            )
        await filters.mount_all(frows)

    # -- the status -----------------------------------------------------------------------

    def _show_status(self, note: str = "") -> None:
        if self.line_error:
            text = f"query line: {self.line_error}"
        elif self.form_error:
            text = f"form: {self.form_error}"
        else:
            text = "valid"
        if note:
            text = f"{text}; {note}"
        self.query_one("#q-status", Static).update(safe.plain(text))
        self.refresh_report()

    def refresh_report(self) -> None:
        """The match count and the default again: a snapshot may have arrived, or the default
        been saved."""
        self.query_one("#q-report", Static).update(safe.plain(self.actions.report(self.where)))
        default = self.actions.default()
        self.query_one("#q-default-text", Static).update(
            safe.plain(f"Default (Reset to default restores it): {default or 'none'}")
        )

    # -- the line -------------------------------------------------------------------------

    def _set_line(self, text: str) -> None:
        self._line_value = text
        self.query_one("#q-line", Input).value = text

    async def _line_edited(self, text: str) -> None:
        result = query.parse(text)
        if isinstance(result, query.ParseError):
            self.line_error = str(result)  # the last valid text, form and report stay
            self._show_status()
            return
        self.line_error = None
        self.form_error = None
        self.valid = query.format_query(result)
        self.where = result.where
        form = query.to_form(result)
        if form != self.form:
            self.form = form
            await self._render_form()
        self._show_status()

    async def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        widget_id = event.input.id or ""
        if widget_id == "q-line":
            if event.value == self._line_value:
                return  # the panel's own write
            self._line_value = event.value
            self._past = -1
            await self._line_edited(event.value)
            return
        m = _ID.match(widget_id)
        if m is None or m[1] != "f" or int(m[2]) != self._gen or m[4] != "val":
            return
        index = int(m[3])
        filters = self.form.filters
        if filters is None or index >= len(filters) or filters[index].value == event.value:
            return
        row = filters[index]
        self._edit_filters(index, query.FormFilter(row.field, row.op, event.value))

    # -- the form -------------------------------------------------------------------------

    def _form_edited(self) -> None:
        """The form changed: if it says a valid query, the line follows; else the error shows."""
        result = query.parse(query.form_text(self.form))
        if isinstance(result, query.ParseError):
            self.form_error = str(result)
            self._show_status()
            return
        replaced = self.line_error is not None  # an invalid line being typed is overwritten
        self.form_error = None
        self.line_error = None
        self.valid = query.format_query(result)
        self.where = result.where
        if self.query_one("#q-line", Input).value != self.valid:
            self._set_line(self.valid)
        self._show_status("line replaced by the form" if replaced else "")

    def _edit_filters(self, index: int | None, row: query.FormFilter | None) -> None:
        """Replace (or with ``row`` None remove) filter ``index``; None ``index`` appends."""
        current = list(self.form.filters or ())
        if index is None:
            assert row is not None
            current.append(row)
        elif row is None:
            del current[index]
        else:
            current[index] = row
        self.form = query.Form(tuple(current), self.form.order)
        self._form_edited()

    def _edit_order(self, order: list[query.OrderKey]) -> None:
        self.form = query.Form(self.form.filters, tuple(order), self.form.rest)
        self._form_edited()

    async def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        m = _ID.match(event.select.id or "")
        if m is None or int(m[2]) != self._gen or not isinstance(event.value, str):
            return
        index, part, value = int(m[3]), m[4], event.value
        if m[1] == "s":
            order = list(self.form.order)
            if index >= len(order):
                return
            column, descending = order[index]
            new = (value, descending) if part == "col" else (column, value == "desc")
            if new == order[index]:
                return
            order[index] = new
            self._edit_order(order)
            return
        filters = self.form.filters
        if filters is None or index >= len(filters):
            return
        row = filters[index]
        if part == "field" and value != row.field:
            kind = query.FIELDS[value]
            op = row.op if row.op in query.OPERATORS[kind] else query.OPERATORS[kind][0]
            keep = row.value if query.FIELDS[row.field] == kind and op not in query.NO_VALUE else ""
            self._edit_filters(index, query.FormFilter(value, op, keep))
            await self._render_form()  # the operators on offer depend on the column
        elif part == "op" and value != row.op:
            keep = "" if value in query.NO_VALUE else row.value
            self._edit_filters(index, query.FormFilter(row.field, value, keep))
            await self._render_form()  # a value box shows only for an operator that takes one

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        widget_id = event.button.id or ""
        if widget_id == "q-apply":
            self._apply()
        elif widget_id == "q-cancel":
            self.dismiss(None)
        elif widget_id == "q-reset":
            self.actions.clear()
            self.dismiss(None)
        elif widget_id == "q-reset-default":
            self.actions.reset_default()
            self.dismiss(None)
        elif widget_id == "q-default":
            if self._refuse():
                return
            self._show_status(self.actions.set_default(self.valid))
        elif widget_id == "q-add-sort":
            used = {c for c, _ in self.form.order}
            free = [c for c in query.ORDER_COLUMNS if c not in used]
            if not free or len(self.form.order) >= query.MAX_ORDER:
                self._show_status("no more sort keys to add")
                return
            self._edit_order([*self.form.order, (free[0], False)])
            await self._render_form()
        elif widget_id == "q-add-filter":
            if self.form.filters is None:
                self._show_status("edit this filter in the query line")
                return
            if len(self.form.filters) >= query.MAX_CONDITIONS:
                self._show_status("no more filters to add")
                return
            self._edit_filters(None, query.FormFilter("ci", query.EQ, "failing"))
            await self._render_form()
        else:
            await self._row_button(widget_id)

    async def _row_button(self, widget_id: str) -> None:
        m = _ID.match(widget_id)
        if m is None or int(m[2]) != self._gen:
            return
        index, part = int(m[3]), m[4]
        if m[1] == "f":
            if self.form.filters is not None and index < len(self.form.filters) and part == "del":
                self._edit_filters(index, None)
                await self._render_form()
            return
        order = list(self.form.order)
        if index >= len(order):
            return
        if part == "del":
            del order[index]
        elif part == "up" and index > 0:
            order[index - 1], order[index] = order[index], order[index - 1]
        elif part == "down" and index < len(order) - 1:
            order[index + 1], order[index] = order[index], order[index + 1]
        else:
            return
        self._edit_order(order)
        await self._render_form()

    # -- applying -------------------------------------------------------------------------

    def _refuse(self) -> bool:
        """True (with the error shown) while the line or the form is invalid."""
        if self.line_error or self.form_error:
            self._show_status("not applied or saved: fix the error first")
            return True
        return False

    def _apply(self) -> None:
        if self._refuse():
            return
        self.actions.apply(self.valid)
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "q-line":
            self._apply()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        entries = self.actions.history()
        if 0 <= event.option_index < len(entries):
            self.run_worker(self._use_history(entries[event.option_index]), exclusive=True)

    async def _use_history(self, text: str) -> None:
        self._set_line(text)
        await self._line_edited(text)
        self.query_one("#q-line", Input).focus()

    async def on_key(self, event: events.Key) -> None:
        """Up and Down in the query line step through the history (the typed text comes back at
        the bottom)."""
        if event.key not in ("up", "down") or self.focused is None or self.focused.id != "q-line":
            return
        entries = self.actions.history()
        event.stop()
        event.prevent_default()
        if event.key == "up":
            if self._past + 1 >= len(entries):
                return
            if self._past == -1:
                self._draft = self.query_one("#q-line", Input).value
            self._past += 1
        else:
            if self._past < 0:
                return
            self._past -= 1
        text = self._draft if self._past == -1 else entries[self._past]
        self._set_line(text)
        await self._line_edited(text)
