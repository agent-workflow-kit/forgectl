"""`browse`: the handler and the parts of it that never import ``textual``.

The terminal UI itself lives in ``forgectl.tui`` and is imported only after the extra
``forgectl[tui]`` is found, so cron and shared use never pull it in. This module checks the
command line (``--focus``, ``--result-file``), starts the app, and writes the result file.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from typing import Any

from .errors import ForgectlError, usage
from .registry import Invocation, register

INSTALL_COMMAND = "pip install 'forgectl[tui]'"

# The `pr` form of schemas/defs.json: host/<full project path>#N.
_PR_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
    r"(?:/(?!\.\.?(?:/|#))[A-Za-z0-9._-]+)+#[1-9][0-9]{0,9}"
)
_PR_MAX_LENGTH = 512
_ASCII_UPPER_TO_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_INTENTS = ("open", "back")
_RESULT_KEYS = frozenset({"intent", "selected", "merged"})


def is_pr(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= _PR_MAX_LENGTH
        and _PR_RE.fullmatch(value) is not None
    )


def parse_focus(text: str) -> str:
    """``--focus`` takes the ``host/path#N`` form, matched whole; the ASCII host is lowercased.

    TODO(#5): accept the URL forms too, through the grammars of ``identifiers.py`` (host
    lookup, closed host set), once that module is on this branch's base; and refuse a host that
    is neither built in nor declared in config with ``unknown_host`` (exit 20) before the UI
    starts.
    """
    host, slash, rest = text.partition("/")
    candidate = host.translate(_ASCII_UPPER_TO_LOWER) + slash + rest
    if not is_pr(candidate):
        raise ForgectlError("invalid_pr", "--focus takes a PR in the form host/<project path>#N")
    return candidate


def validate_result(result: object) -> None:
    """Check ``result`` against ``schemas/cli/browse-result.json`` (tests cross-check it)."""
    if not isinstance(result, dict) or set(result) != _RESULT_KEYS:
        raise ForgectlError("internal", "browse result: wrong keys")
    if result["intent"] not in _INTENTS:
        raise ForgectlError("internal", "browse result: bad intent")
    selected, merged = result["selected"], result["merged"]
    if not isinstance(selected, list) or len(selected) > 1 or not all(map(is_pr, selected)):
        raise ForgectlError("internal", "browse result: bad selected")
    if not isinstance(merged, list) or not all(map(is_pr, merged)):
        raise ForgectlError("internal", "browse result: bad merged")


def preflight_result_file(path: str) -> None:
    """Refuse an unusable ``--result-file`` before the UI starts (exit 2)."""
    if "\x00" in path:
        raise usage("--result-file: bad path")
    directory = os.path.dirname(path) or "."
    try:
        if not stat.S_ISDIR(os.stat(directory).st_mode):
            raise usage("--result-file: parent is not a directory")
        if not os.access(directory, os.W_OK | os.X_OK):
            raise usage("--result-file: the parent directory is not writable")
        st = os.lstat(path)
    except FileNotFoundError:
        if os.path.isdir(directory):
            return
        raise usage("--result-file: parent directory does not exist") from None
    except OSError as err:
        raise usage(f"--result-file: {err.strerror or 'unusable'}") from None
    if not stat.S_ISREG(st.st_mode):  # a symlink, a FIFO, a directory, a device
        raise usage("--result-file must be a regular file, not a link or special file")


def write_result_file(path: str, result: dict[str, Any]) -> None:
    """Write the result atomically: a private temporary file in the same directory, then a rename.

    The temporary file has an unpredictable name and is created by ``mkstemp`` (``O_EXCL``,
    ``O_NOFOLLOW``, mode 0600). A symlink or special file at ``path`` is refused (the rename
    would otherwise replace it). Any failure here, the re-run preflight included, is
    ``internal``: the command line was already accepted.
    """
    validate_result(result)
    try:
        preflight_result_file(path)
    except ForgectlError as err:
        raise ForgectlError("internal", f"could not write --result-file: {err.message}") from None
    data = json.dumps(result, ensure_ascii=True).encode("ascii") + b"\n"
    directory = os.path.dirname(path) or "."
    base = os.path.basename(path)
    try:
        fd, temp = tempfile.mkstemp(dir=directory, prefix="." + base + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
    except OSError as err:
        raise ForgectlError(
            "internal", f"could not write --result-file: {err.strerror or 'error'}"
        ) from None


def _require_extra() -> None:
    try:
        import textual  # noqa: F401
    except ImportError:
        raise usage(
            f"browse needs the extra forgectl[tui]; install it with: {INSTALL_COMMAND}"
        ) from None


def _run_app(focus: str | None, config_path: str | None = None) -> dict[str, Any]:
    """Start the UI and return the result it ends with."""
    from .tui.run import run_browse  # imported only with the extra

    return run_browse(focus=focus, config_path=config_path)


@register("browse")
def browse(inv: Invocation) -> dict[str, Any]:
    args = inv.args
    focus = parse_focus(args.focus) if args.focus is not None else None
    result_file: str | None = args.result_file
    if result_file is not None:
        preflight_result_file(result_file)
    _require_extra()
    result = _run_app(focus, getattr(args, "config", None))
    if result_file is not None:
        write_result_file(result_file, result)
    # The result goes to the file; stdout stays empty apart from the empty object.
    return {}
