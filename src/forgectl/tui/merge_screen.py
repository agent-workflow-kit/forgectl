"""The merge confirmation of `browse` (`m`): what will be merged, then `y` or `n`.

The lines are built by the app from the merge command's own dry run, so what the dialog shows is
what the command checked (repo, number, head, base, method). Nothing is written until `y`. When the
method is unresolved the dialog lists the allowed ones: a number key picks one, and `y` needs a
pick. It has no override of any kind. Text goes through ``safe`` and is never parsed as markup.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Key
from textual.screen import ModalScreen
from textual.widgets import Static

from . import safe


class MergeScreen(ModalScreen[str | None]):
    """Dismisses with the method to merge with (the empty string: the host decides) on `y`,
    or ``None`` on `n` or escape. Any other key does nothing."""

    DEFAULT_CSS = """
    MergeScreen { align: center middle; }
    #merge-box {
        width: 90%; max-width: 90; height: auto; max-height: 100%;
        border: round $warning; padding: 1 2; background: $surface;
    }
    #merge-box Static { height: auto; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def __init__(self, lines: list[str], method: str | None, choices: list[str]) -> None:
        super().__init__()
        self._lines = lines
        self._method = method  # the resolved method, or None while one must be picked
        self._choices = choices  # the methods to pick from; empty when the method is resolved

    def compose(self) -> ComposeResult:
        with Vertical(id="merge-box"):
            for text in self._lines:
                yield Static(safe.plain(safe.line(text)), classes="merge-line", markup=False)
            yield Static(safe.plain(""), id="merge-pick", markup=False)
            yield Static(safe.plain(""), id="merge-keys", markup=False)

    def on_mount(self) -> None:
        self._paint()

    def _paint(self) -> None:
        if self._choices:
            picks = "  ".join(f"{i} {m}" for i, m in enumerate(self._choices, 1))
            chosen = self._method or "none yet"
            self.query_one("#merge-pick", Static).update(
                safe.plain(f"choose the method: {picks} (chosen: {chosen})")
            )
        keys = "y merge   n or esc cancel"
        if self._choices and self._method is None:
            keys = "pick a method with its number first   n or esc cancel"
        self.query_one("#merge-keys", Static).update(safe.plain(keys))

    def on_key(self, event: Key) -> None:
        event.stop()
        key = event.key
        if key == "y":
            if self._choices and self._method is None:
                return
            self.dismiss(self._method or "")
        elif key.isdigit() and self._choices:
            index = int(key) - 1
            if 0 <= index < len(self._choices):
                self._method = self._choices[index]
                self._paint()
        elif key in ("n", "escape"):
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
