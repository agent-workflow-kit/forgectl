"""Bounds shared by the config and the async-hook watchdog.

Standard library only: ``hookwatch`` imports this under an interpreter that may have none of
forgectl's dependencies installed.
"""

from __future__ import annotations

MAX_DURATION_SECONDS = 100 * 365 * 86400  # as for the --timeout flag: 100 years
MAX_DURATION = float(MAX_DURATION_SECONDS)  # the largest duration a config may give
