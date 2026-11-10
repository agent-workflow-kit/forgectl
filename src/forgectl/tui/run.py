"""Start the app and hand back the result; the one entry point `forgectl.browse` imports."""

from __future__ import annotations

import os
from typing import Any

from forgectl.config import config_directory, load_config
from forgectl.errors import ForgectlError

from .app import BrowseApp
from .opener import BrowserOpener, Opener
from .query_store import HISTORY_FILE, QueryStore, state_dir
from .source import DataSource
from .source_live import LiveSource


def _store(config_path: str | None) -> QueryStore:
    """The query history under the state directory and the default's drop-in beside the config in
    use. The paths come from the environment only here: a test's app has neither."""
    state = state_dir(os.environ)
    try:
        config_dir = config_directory(config_path, os.environ)
    except ForgectlError:
        config_dir = None
    return QueryStore(
        history_path=None if state is None else state / HISTORY_FILE,
        config_dir=config_dir,
        no_path_note="" if state is not None else "history not saved: no state directory",
    )


def run_browse(
    *,
    focus: str | None,
    config_path: str | None = None,
    timeout: float | None = None,
    source: DataSource | None = None,
    opener: Opener | None = None,
    interval: float | None = None,
) -> dict[str, Any]:
    """Run the app; an app that stopped on an error raises ``internal`` and gives no result.

    ``interval`` is the ``--interval`` flag in seconds (0: off); without it the config's
    ``browse.refresh_interval`` decides. The live source reads the config, so an invalid one
    stops here (``config_invalid``). A source handed in (a test's) does not: its refresh is off
    unless ``interval`` asks for it, and the other tunables keep the app's defaults.
    """
    tunables: dict[str, Any] = {}
    refresh = interval
    if source is None:
        browse = load_config(config_path, env=os.environ).config.browse
        refresh = browse.refresh_interval if interval is None else interval
        tunables = {
            "full_refresh_every": browse.full_refresh_every,
            "concurrency": browse.concurrency,
            "detail_collapsed": browse.detail_collapsed,
            "query": browse.query,
            "store": _store(config_path),
        }
    app = BrowseApp(
        source or LiveSource(config_path=config_path, timeout=timeout),
        opener or BrowserOpener(),
        focus=focus,
        refresh_interval=refresh or 0,
        **tunables,
    )
    # TODO(#2): SIGHUP has no handler in cli.py; on a hangup the terminal state (alternate
    # screen, modes) is not restored.
    app.run()
    if app.return_code != 0 or getattr(app, "_exception", None) is not None:
        raise ForgectlError("internal", "browse stopped on an internal error")
    return app.result()
