"""The protocol both adapters implement (GitHub in `github.py`, GitLab in `gitlab.py`).

Each method reads one host and returns the normalized objects of `forgectl.model`. Adapters run
every child process through `forgectl.gateway`, map host wording to codes in one table each, and
never truncate silently: a list a host cut off sets its `complete` flag to false.

Errors are raised as `forgectl.errors` types; the protocol does not catch them. Retries, the
deadline, and merging results across hosts belong to `reads.py`, not to an adapter.

The protocol covers every read and, so far, the one write `merge` (Stage 3); the posts and
`resolve` are added with their issues. A write is never retried: it reaches the host once, and a
caller decides from a later read what happened.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .. import model as m
from ..errors import ForgectlError
from ..identifiers import PrRef, RepoRef

#: Every read an adapter provides; the protocol test keeps this equal to `Backend`.
READ_METHODS: tuple[str, ...] = (
    "list_prs",
    "get_pr",
    "snapshot",
    "diff",
    "compare",
    "files",
    "comments",
    "reviews",
    "ci",
    "access",
    "whoami",
)


#: Every write an adapter provides (`merge` so far); the protocol test keeps this too.
WRITE_METHODS: tuple[str, ...] = ("merge",)


def not_implemented(command: str) -> ForgectlError:
    """What an adapter raises for a read its own issue has not added yet (an `internal` error,
    never a traceback). Each such method carries a `TODO(#N)` naming the issue that fills it."""
    return ForgectlError("internal", f"{command}: not implemented yet")


@runtime_checkable
class Backend(Protocol):
    #: `github` or `gitlab`.
    name: m.BackendName

    def list_prs(self, query: m.PrQuery) -> m.PrPage:
        """`prs` on this backend's hosts."""
        ...

    def get_pr(self, ref: PrRef, readiness: m.Readiness | None = None) -> m.PrRead:
        """`show`: the PR object, with `blocked_by` computed by `model.compute_blocked_by`, and
        the `CI` from the same read, so `merge` and `pre-merge` never re-read CI at a possibly
        different head. ``readiness`` says how long a value the host is still computing is read
        again (default: `show`'s; `merge` passes its own)."""
        ...

    def snapshot(self, ref: PrRef) -> m.Snapshot:
        """`snapshot`: the PR plus every part, in one reading."""
        ...

    def diff(self, ref: PrRef) -> m.DiffResult:
        """`diff`; a host that will not produce the whole diff is `too_large`, never partial."""
        ...

    def compare(self, ref: PrRef, from_sha: str, to_sha: str) -> m.CompareResult:
        """`compare` between two full shas."""
        ...

    def files(self, ref: PrRef) -> m.FilesResult:
        """`files`."""
        ...

    def comments(self, ref: PrRef) -> m.CommentsResult:
        """`comments`: threads, system notes left out."""
        ...

    def reviews(self, ref: PrRef) -> m.ReviewsResult:
        """`reviews`: `latest_by_reviewer` computed by forgectl, never the host's own."""
        ...

    def ci(self, ref: PrRef) -> m.CiResult:
        """`ci`, built with `model.aggregate_ci`."""
        ...

    def access(self, repo: RepoRef, user: str) -> m.AccessResult:
        """`access`: `write_access` null when the account may not read the answer."""
        ...

    def whoami(self, host: str) -> m.Identity:
        """The authenticated account on `host`."""
        ...

    def merge(self, pr: m.PR, *, sha: str, method: str | None) -> m.MergeAnswer:
        """The merge call for ``pr`` (as just read, so the write goes to its canonical path),
        carrying ``sha`` for the host to enforce and ``method`` where the host takes one. Never
        retried. A 2xx is ``accepted``, which proves nothing; the caller verifies by reading.
        A failure that may have landed is `outcome_unknown`, raised as such."""
        ...
