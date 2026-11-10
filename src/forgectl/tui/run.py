"""Start the app and hand back the result; the one entry point `forgectl.browse` imports."""

from __future__ import annotations

from typing import Any

from forgectl.errors import ForgectlError

from .app import BrowseApp
from .opener import BrowserOpener, Opener
from .source import DataSource
from .source_live import LiveSource


def run_browse(
    *,
    focus: str | None,
    config_path: str | None = None,
    timeout: float | None = None,
    source: DataSource | None = None,
    opener: Opener | None = None,
) -> dict[str, Any]:
    """Run the app; an app that stopped on an error raises ``internal`` and gives no result."""
    app = BrowseApp(
        source or LiveSource(config_path=config_path, timeout=timeout),
        opener or BrowserOpener(),
        focus=focus,
    )
    # TODO(#2): SIGHUP has no handler in cli.py; on a hangup the terminal state (alternate
    # screen, modes) is not restored.
    app.run()
    if app.return_code != 0 or getattr(app, "_exception", None) is not None:
        raise ForgectlError("internal", "browse stopped on an internal error")
    return app.result()
