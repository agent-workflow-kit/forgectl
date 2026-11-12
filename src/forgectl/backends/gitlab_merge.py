"""GitLab's merge call: ``PUT /projects/:id/merge_requests/:iid/merge`` (#22).

The body is ``{sha}``, plus ``squash: true`` for ``--method squash`` and nothing else: GitLab has no
per-merge method (the project's ``merge_method`` decides) and ``squash=false`` does nothing, so it
is never sent. There is no merge message and no ``should_remove_source_branch``. ``--auto`` (#23)
adds ``auto_merge=true`` on GitLab 17.11 or newer and ``merge_when_pipeline_succeeds=true`` before
that (``auto_flag``): a running pipeline arms it, no pipeline or a passed one merges at once, and a
failed, canceled or skipped one answers 405. forgectl never calls ``/rebase``: it
would change the head. ``squash`` goes out only now, after ``merge.py``'s checks passed, because
``squash=true`` on a refused merge stays set on the MR.

A write is never retried and never trusted: every 2xx is only ``accepted`` and the caller's
verification read decides. ``sha`` is enforced by the host (409 when the head moved), and a 409
beats the bare 405 a moved target gets on an ``ff`` or ``rebase_merge`` project. 405, 406 and 422
(not mergeable; a conflict; a draft) are mapped to ``refused``: ``merge.py`` re-reads once and
decides. 423 (locked) is mapped to ``locked`` and handled like a 5xx: verification decides.
Every other failure is the gateway's. All status meanings are UNVERIFIED until the live suite
(#33) checks them; a ``locked`` MR (GitLab's transient state while a merge runs) reads as
open with a ``locked`` blocker and counts as not yet decided during verification.

The facts a read saw about the MR and its project are kept per instance (``MergeFacts``), so the
write needs no further read: the numeric project id for the route, and what the reported
``method`` is derived from.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .. import model as m
from ..errors import ForgectlError, refused
from ..gateway import Gateway, Target, path_int, shown_text

#: Any 2xx reaches verification; the status itself proves nothing.
ACCEPTED = frozenset(range(200, 300))
#: The statuses mapped here instead of raised: a wrong ``sha`` and the refusals.
MAPPED = frozenset({405, 406, 409, 422, 423})
_PROJECT_METHODS = ("merge", "rebase_merge", "ff")
#: The first version whose merge call takes ``auto_merge``; older ones take
#: ``merge_when_pipeline_succeeds``.
AUTO_MERGE_PARAM = (17, 11)
#: The first version where a push cancels an armed auto-merge, so the sent ``sha`` is kept.
PUSH_CANCELS_AUTO = (17, 7)


def auto_flag(version: tuple[int, int] | None) -> str:
    """The merge call's auto parameter for a host version (an unreadable one is refused before
    this is asked)."""
    if version is None:
        raise ForgectlError(
            "unsupported_host_version", "the host version cannot be read, so --auto is refused"
        )
    return "auto_merge" if version >= AUTO_MERGE_PARAM else "merge_when_pipeline_succeeds"


@dataclass(frozen=True)
class MergeFacts:
    """What one MR read showed that the merge needs."""

    project_id: int
    squash_on_merge: bool  # the MR will be squashed when merged (its own flag or the project's)
    squashed: bool  # a squash commit exists (the MR merged by squashing)
    project_method: str | None  # the project's merge_method when it is a known value


def facts_from(mr: Mapping[str, Any], project: Mapping[str, Any], project_id: int) -> MergeFacts:
    method = project.get("merge_method")
    return MergeFacts(
        project_id=project_id,
        squash_on_merge=mr.get("squash_on_merge") is True,
        squashed=mr.get("squash_commit_sha") is not None,
        project_method=method if method in _PROJECT_METHODS else None,
    )


def check_method(facts: MergeFacts, method: str | None) -> None:
    """The one row of the Merge method table the project settings (``merge_methods_allowed``)
    do not hold: ``merge`` or ``rebase`` on an MR that squashes on merge is refused, since the
    merge would squash whatever was asked."""
    if method in ("merge", "rebase") and facts.squash_on_merge:
        raise refused(
            "method_not_allowed",
            f"the merge request squashes on merge, so --method {method} is not possible",
            details={"allowed": ["squash"]},
        )


def reported_method(facts: MergeFacts | None, sent: str | None) -> str | None:
    """The method that merged, derived from the MR and the project, so it is reported with no
    flag as well: ``squash`` when it was sent, or when the MR squashes or has a squash commit,
    else ``rebase`` on an ``ff`` project and ``merge`` on ``merge`` or ``rebase_merge``. What was
    sent when the project setting is not a known value."""
    if sent == "squash":
        return "squash"
    if facts is None:
        return sent
    if facts.squash_on_merge or facts.squashed:
        return "squash"
    if facts.project_method == "ff":
        return "rebase"
    if facts.project_method is not None:
        return "merge"
    return sent


def classify(status: int, message: str) -> m.MergeAnswer:
    """Map a mapped answer: 409 is a head that moved, 423 a locked MR that verification decides,
    the rest are refusals to re-read."""
    if status == 409:
        return m.MergeAnswer("stale_head", status, message)
    if status == 423:  # locked: a merge may be running; like a 5xx, only a read can decide
        return m.MergeAnswer("locked", status, message)
    return m.MergeAnswer("refused", status, message)


def _message(data: Any, secret: str | None) -> str:
    text = None
    if isinstance(data, dict):
        text = data.get("message") or data.get("error")
    elif isinstance(data, str):
        text = data
    return shown_text(text, secret) if isinstance(text, str) else ""


def put_merge(
    gateway: Gateway,
    target: Target,
    pr: m.PR,
    facts: MergeFacts | None,
    *,
    sha: str,
    method: str | None,
    auto_param: str | None = None,
) -> m.MergeAnswer:
    """Send the merge for ``pr`` once; ``auto_param`` names the auto flag to set for ``--auto``."""
    if facts is None:
        raise ForgectlError("internal", "gitlab merge: the merge request was not read first")
    body: dict[str, Any] = {"sha": sha}
    if method == "squash":
        body["squash"] = True
    if auto_param is not None:
        body[auto_param] = True
    response = gateway.api(
        target,
        "PUT",
        [
            "projects",
            path_int(facts.project_id),
            "merge_requests",
            path_int(pr.number),
            "merge",
        ],
        body=body,
        write=True,
        expect_status=ACCEPTED,
        passthrough=MAPPED,
    )
    if response.status in MAPPED:
        return classify(response.status, _message(response.data, target.token))
    return m.MergeAnswer("accepted", response.status)
