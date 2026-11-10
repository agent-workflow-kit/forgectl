"""GitHub's merge call: ``PUT /repos/{owner}/{repo}/pulls/{n}/merge``.

The one request body is ``{sha, merge_method}``. ``sha`` is the head the caller expected, and the
host enforces it (a head that moved answers 409). There is no ``commit_title``, no
``commit_message`` and no ``merge_method`` when the caller names none: the host's own defaults
write the merge message, so nothing a pull request says can become one through forgectl.

A write is never retried and never trusted. Every 2xx is only ``accepted``: GitHub also answers
200 ``merged: true`` for an already merged PR with a wrong ``sha``, and ``gh`` follows a 301, 302
or 303 by sending a body-less GET, so the 2xx may belong to a read. The caller's verification
read decides (interface: Merge order, step 9). A 409 and a 405 are mapped here; every other
failure is the gateway's (``rate_limited``, ``permission_denied``, ``not_found``,
``host_rejected``, or ``outcome_unknown`` for a 5xx, a timeout or a protocol failure).

Verified live (docs/design.md, Live verification): a ``sha`` that does not match the head answers
409, and a merged PR with a wrong ``sha`` answers 200 ``merged: true``. UNVERIFIED until the live
suite (#31, #32) checks it: the 200 answer of a fresh merge, the 405 wordings below, and what a
merge queue answers.
"""

from __future__ import annotations

import re
from typing import Any

from .. import model as m
from ..gateway import Gateway, Target, path_int, shown_text

#: Any 2xx reaches verification; the status itself proves nothing.
ACCEPTED = frozenset(range(200, 300))
#: The statuses mapped here instead of raised: a wrong ``sha`` and a refusal.
MAPPED = frozenset({405, 409})
_BASE_MODIFIED = re.compile(r"base branch was modified", re.IGNORECASE)
# "Merge commits are not allowed on this repository.", "Squash merges are not allowed ...",
# "Rebase merges are not allowed ..." (GitHub's wording; UNVERIFIED)
_METHOD_NOT_ALLOWED = re.compile(
    r"\b(?:merge commits?|squash(?: merges?| commits?)?|rebase(?: merges?| commits?)?"
    r"|merge method)\b[^.]*\bnot allowed\b",
    re.IGNORECASE,
)


def _message(data: Any, secret: str | None) -> str:
    text = data.get("message") if isinstance(data, dict) else data
    return shown_text(text, secret) if isinstance(text, str) else ""


def classify(status: int, message: str) -> m.MergeAnswer:
    """Map a 405 or 409 answer."""
    if status == 409:
        return m.MergeAnswer("stale_head", status, message)
    if _BASE_MODIFIED.search(message):
        return m.MergeAnswer("base_modified", status, message)
    if _METHOD_NOT_ALLOWED.search(message):
        return m.MergeAnswer("method_not_allowed", status, message)
    return m.MergeAnswer("refused", status, message)


def put_merge(
    gateway: Gateway, target: Target, pr: m.PR, *, sha: str, method: str | None
) -> m.MergeAnswer:
    """Send the merge for ``pr`` once. ``pr.repo`` is the canonical path from the read."""
    _, _, path = pr.repo.partition("/")
    owner, _, name = path.partition("/")
    body: dict[str, str] = {"sha": sha}
    if method is not None:
        body["merge_method"] = method
    response = gateway.api(
        target,
        "PUT",
        ["repos", owner, name, "pulls", path_int(pr.number), "merge"],
        body=body,
        write=True,
        expect_status=ACCEPTED,
        passthrough=MAPPED,
    )
    if response.status in MAPPED:
        return classify(response.status, _message(response.data, target.token))
    return m.MergeAnswer("accepted", response.status)
