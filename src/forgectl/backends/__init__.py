"""The backends and the one table that names them.

``BACKEND_CLASSES`` maps a backend name (``github``, ``gitlab``) to the module and class of its
adapter. A class takes one argument, the command's ``reads.ReadEnv``, and implements
``backends.base.Backend``. The module is imported only when that backend is first needed, so a
host of one backend never loads the other's code and a backend not written yet is an error at
use, not at import.

To add a backend, add one line here.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from ..errors import ForgectlError
from .base import Backend

if TYPE_CHECKING:
    from ..reads import ReadEnv

# TODO(#15): GitLabBackend takes ReadEnv as its only argument and runs env.until_known in get_pr
# itself
BACKEND_CLASSES: dict[str, tuple[str, str]] = {
    "github": ("forgectl.backends.github", "GitHubBackend"),
    # TODO(#15): "gitlab": ("forgectl.backends.gitlab", "GitLabBackend"),
}


def create_backend(name: str, env: ReadEnv) -> Backend:
    """The adapter for backend ``name``, built over ``env``."""
    entry = BACKEND_CLASSES.get(name)
    if entry is None:
        raise ForgectlError("internal", f"the {name} backend is not implemented yet")
    module, cls = entry
    backend: Backend = getattr(importlib.import_module(module), cls)(env)
    return backend
