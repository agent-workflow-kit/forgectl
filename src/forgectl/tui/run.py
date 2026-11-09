"""Start the app and hand back the result; the one entry point `forgectl.browse` imports."""

from __future__ import annotations

from typing import Any

from .app import BrowseApp
from .opener import BrowserOpener, Opener
from .source import DataSource, UnwiredSource


def run_browse(
    *, focus: str | None, source: DataSource | None = None, opener: Opener | None = None
) -> dict[str, Any]:
    app = BrowseApp(source or UnwiredSource(), opener or BrowserOpener(), focus=focus)
    app.run()
    return app.result()
