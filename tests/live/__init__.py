"""The live test harness: guards, host helpers, throwaway lifecycle, recording scrub.

Nothing here runs without credentials in the dedicated ``FORGECTL_LIVE_*`` settings, outside CI
and with ``--run-live`` (``make live``). See docs/live-tests.md.
"""
