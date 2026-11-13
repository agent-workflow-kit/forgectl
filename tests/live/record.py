"""Record-and-scrub: a live response enters the repository only through ``scrub_recording``.

``python -m tests.live.record RAW.json --dest tests/recordings/github/name.json --login LOGIN
--extra NAME ...`` scrubs the raw recording with ``tests/harness/scrub.py``, refuses to write if
``find_unscrubbed`` still finds a leak, a token value or a throwaway name, and writes only under
``tests/recordings``. Raw recordings stay outside the repository (a private temp directory).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tests.harness.scrub import Scrubber, check_tree, find_unscrubbed

from .guards import BACKEND_HOSTS, NAME_PREFIX, LiveSettings

RECORDINGS = Path(__file__).resolve().parent.parent / "recordings"


class RecordingRefused(Exception):
    """The recording still holds something that must not enter the repository."""


def live_secrets(environ: Mapping[str, str]) -> list[str]:
    """The values of every ``FORGECTL_LIVE_*`` token setting, which must never be recorded."""
    found = []
    for backend in BACKEND_HOSTS:
        names = LiveSettings.for_backend(backend)
        for name in (names.token, names.second_token):
            value = environ.get(name, "").strip()
            if value:
                found.append(value)
    return found


def scrub_recording(
    document: Any, *, logins: Sequence[str], extra: Sequence[str] = (), secrets: Sequence[str] = ()
) -> str:
    """The scrubbed JSON text of ``document``, or ``RecordingRefused``.

    ``logins`` are the accounts the run used (always hidden, even where no identity field names
    them), ``extra`` further names to hide, ``secrets`` token values that must not appear at all.
    Run ids (``forgectl-live-...``) are replaced by ``throwaway``.
    """
    if not logins:
        raise RecordingRefused("name the accounts the run used (logins)")
    clean = Scrubber(extra=[*logins, *extra]).scrub(document)
    text = json.dumps(clean, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    text = re.sub(re.escape(NAME_PREFIX) + r"[0-9a-z-]+", "throwaway", text)
    for login in [*logins, *extra]:
        if login and re.search(rf"(?<![\w-]){re.escape(login)}(?![\w-])", text, re.IGNORECASE):
            raise RecordingRefused("a login or name given to the scrub is still in the output")
    for secret in secrets:
        if secret and secret in text:
            raise RecordingRefused("a token value is in the recording")
    findings = find_unscrubbed(text, "<recording>")
    if findings:
        kinds = sorted({f.kind for f in findings})
        raise RecordingRefused(f"the scrub left {len(findings)} finding(s): {kinds}")
    return text


def write_recording(
    document: Any,
    dest: Path,
    *,
    logins: Sequence[str],
    extra: Sequence[str] = (),
    secrets: Sequence[str] = (),
    root: Path | None = None,
) -> Path:
    """Scrub, then write under ``root`` and check the written tree again."""
    root = RECORDINGS if root is None else root
    resolved = dest.resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved.suffix != ".json":
        raise RecordingRefused("a recording is a .json file under tests/recordings")
    text = scrub_recording(document, logins=logins, extra=extra, secrets=secrets)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(text, encoding="utf-8")
    leaks = check_tree(resolved.parent)
    if leaks:
        resolved.unlink()  # our own just-written file, never a tracked one
        raise RecordingRefused(f"the written tree failed the scrub check: {leaks[0].kind}")
    return resolved


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrub a raw live recording into the repository.")
    parser.add_argument("recording", type=Path)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--login", action="append", default=[], required=True)
    parser.add_argument("--extra", action="append", default=[])
    args = parser.parse_args(argv)
    document = json.loads(args.recording.read_text(encoding="utf-8"))
    try:
        secrets = live_secrets(os.environ if environ is None else environ)
        path = write_recording(
            document, args.dest, logins=args.login, extra=args.extra, secrets=secrets
        )
    except RecordingRefused as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 1
    sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
