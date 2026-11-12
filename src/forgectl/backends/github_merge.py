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

``--auto`` (#23) is ``enablePullRequestAutoMerge`` with ``expectedHeadOid`` set to the head the
human saw (``put_auto_merge``). A refusal of the form ``in clean status`` or ``in unstable
status``, or of a repository that does not allow auto-merge, is ``auto_unavailable``; a head that
moved is ``stale_head``. Both wordings are UNVERIFIED until the live suite (#32) checks them.
"""

from __future__ import annotations

import re
from typing import Any

from .. import model as m
from ..errors import ForgectlError
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


# -- --auto (#23) --------------------------------------------------------------------------------

_ENABLE_AUTO_MERGE = """
mutation($id: ID!, $head: GitObjectID!, $method: PullRequestMergeMethod) {
  enablePullRequestAutoMerge(
    input: {pullRequestId: $id, expectedHeadOid: $head, mergeMethod: $method}
  ) {
    clientMutationId
  }
}
"""
_AUTO_FIELD = "enablePullRequestAutoMerge"
# "Pull request is in clean status", "Pull request is in unstable status", and a repository
# that has auto-merge switched off ("Auto merge is not allowed for this repository") (UNVERIFIED)
_AUTO_UNAVAILABLE = re.compile(
    r"\bin (?:clean|unstable) status\b|\bauto[- ]?merge is not allowed\b"
    r"|\bnot allowed for this repository\b",
    re.IGNORECASE,
)
# "Expected head sha didn't match current head ref." and "Head branch was modified" (UNVERIFIED)
_HEAD_MOVED = re.compile(
    r"\bexpected head (?:oid|sha)\b|\bhead branch was modified\b", re.IGNORECASE
)


def put_auto_merge(
    gateway: Gateway, target: Target, pr: m.PR, *, sha: str, method: str | None
) -> m.MergeAnswer:
    """Arm auto-merge for ``pr`` once, pinned to ``sha`` (``expectedHeadOid``). The one mapped
    refusals are a moved head and an auto-merge the host will not arm; any other failure is the
    gateway's, and the caller never retries."""
    variables: dict[str, Any] = {"id": pr.pr_id, "head": sha, "method": None}
    if method is not None:
        variables["method"] = method.upper()
    try:
        gateway.graphql(target, _ENABLE_AUTO_MERGE, variables, write=True, mutation=_AUTO_FIELD)
    except ForgectlError as err:
        if err.code != "host_rejected":
            raise
        if _HEAD_MOVED.search(err.message):
            return m.MergeAnswer("stale_head", 200, err.message)
        if _AUTO_UNAVAILABLE.search(err.message):
            return m.MergeAnswer("auto_unavailable", 200, err.message)
        raise
    return m.MergeAnswer("accepted", 200)
