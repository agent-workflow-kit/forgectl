"""GitHub's ``review``: one atomic ``POST /pulls/N/reviews`` and its read-back (interface: Post
order steps 9 and 11, Reliability).

- The write: ``POST /repos/{o}/{r}/pulls/{n}/reviews`` with ``commit_id`` (the expected head),
  ``event``, an optional ``body`` and the inline ``comments``. It is atomic: GitHub posts the
  whole review or none of it. Status 200, and the proof is a single object with an integer ``id``,
  a ``node_id``, this repository's ``html_url`` and a ``commit_id`` equal to the one sent: a
  redirected write comes back as a GET's answer (a list, or an object with another commit), which
  fails the proof and is ``outcome_unknown``, never reported posted.
- A 422 whose message names a pending review is ``pending_review_exists`` (exit 20); any other 422
  keeps ``host_rejected`` with the host's message (a position GitHub refused after the local check
  passed).
- The read-back: ``GET /pulls/N/reviews/:id/comments``, the inline comments of the review.

A write is never retried. UNVERIFIED until the live suite (#34): the 200 and the answer's field
names, the wording of the pending-review 422 and that ``commit_id`` is echoed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .. import model as m
from ..errors import ForgectlError, refused
from ..gateway import Target, path_int, shown_text, verify_identity
from ..reads import ReadEnv
from .post_base import NODE, PAGE_SIZE, collect, created_id

OK = frozenset({200})
EVENTS = {"comment": "COMMENT", "request_changes": "REQUEST_CHANGES", "approve": "APPROVE"}
# UNVERIFIED until #34: GitHub's documented sentence, "User can only have one pending review per
# pull request"; anchored so a position error that merely says "pending review" is not read as it.
_PENDING = re.compile(r"only\s+have\s+one\s+pending\s+review", re.IGNORECASE)
# The state each event's answer must carry; PENDING (or anything else) is not a submitted review.
# UNVERIFIED until #34.
STATES = {"comment": "COMMENTED", "request_changes": "CHANGES_REQUESTED", "approve": "APPROVED"}


@dataclass(frozen=True)
class Submitted:
    """The proven answer to the review write."""

    review_id: str  # ``gh:r:<node>``
    rest_id: int  # the review's integer id, for the read-back path


def _obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class GitHubReviewer:
    def __init__(self, env: ReadEnv) -> None:
        self._env = env

    def _where(self, pr: m.PR) -> tuple[Target, str, str]:
        host, _, path = pr.repo.partition("/")
        owner, _, name = path.partition("/")
        return self._env.hosts.target(host), owner, name

    # -- the write -----------------------------------------------------------------------------

    def submit(
        self,
        pr: m.PR,
        *,
        event: str,
        commit_id: str,
        body: str | None,
        comments: list[dict[str, Any]],
        account_id: str,
    ) -> Submitted:
        target, owner, name = self._where(pr)
        payload: dict[str, Any] = {"commit_id": commit_id, "event": EVENTS[event]}
        if body is not None:
            payload["body"] = body
        if comments:
            payload["comments"] = comments
        segments = ["repos", owner, name, "pulls", path_int(pr.number), "reviews"]
        response = self._env.gateway.api(
            target,
            "POST",
            segments,
            body=payload,
            write=True,
            shape="object",
            expect_status=OK,
            passthrough=frozenset({422}),
        )
        if response.status == 422:  # nothing landed: GitHub refused the whole review
            raise self._refusal(response.data, pr, target, owner, name, account_id)
        return self._proven(response.data, target, pr, commit_id, event)

    def _proven(self, data: Any, target: Target, pr: m.PR, commit_id: str, event: str) -> Submitted:
        where = "review"
        rest_id = created_id(data, where)
        node = data.get("node_id")
        if not isinstance(node, str) or NODE.fullmatch(node) is None:
            raise ForgectlError(
                "outcome_unknown",
                f"{where}: the answer holds no usable node id; it may have landed",
            )
        if data.get("commit_id") != commit_id:
            raise ForgectlError(
                "outcome_unknown",
                f"{where}: the answer does not carry the commit sent; the write may have landed",
            )
        if data.get("state") != STATES[event]:
            raise ForgectlError(
                "outcome_unknown",
                f"{where}: the answer is not a submitted {event} review; it may have landed",
            )
        verify_identity(
            data.get("html_url"),
            host=target.host,
            project=pr.repo.partition("/")[2],
            secret=target.token,
        )
        return Submitted(f"gh:r:{node}", rest_id)

    def _refusal(
        self, data: Any, pr: m.PR, target: Target, owner: str, name: str, account_id: str
    ) -> ForgectlError:
        """A 422 about a pending review is a refusal naming it; any other 422 is ``host_rejected``
        with the host's own words (a position it refused after the local check passed)."""
        texts: list[str] = []
        doc = _obj(data)
        if isinstance(doc.get("message"), str):
            texts.append(doc["message"])
        errors = doc.get("errors")
        for entry in errors if isinstance(errors, list) else []:
            if isinstance(entry, str):
                texts.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("message"), str):
                texts.append(entry["message"])
        shown = shown_text("; ".join(texts), target.token, limit=200)
        if not any(_PENDING.search(text) for text in texts):
            where = f"{target.host}/repos/{owner}/{name}/pulls/{pr.number}/reviews"
            suffix = f": {shown}" if shown else ""
            return ForgectlError(
                "host_rejected",
                f"{where}: the host answered 422{suffix}",
                details={"status": 422},
            )
        pending = ""
        try:  # the review that is in the way, best effort: the 422 itself is the answer
            segments = ["repos", owner, name, "pulls", path_int(pr.number), "reviews"]
            for item in self._list(target, segments):
                node = item.get("node_id")
                if (
                    item.get("state") == "PENDING"
                    and str(_obj(item.get("user")).get("id")) == account_id
                    and isinstance(node, str)
                    and NODE.fullmatch(node)
                ):
                    pending = f"gh:r:{node}"
                    break
        except ForgectlError:
            pass
        named = f" ({pending})" if pending else ""
        return refused(
            "pending_review_exists",
            f"this account has a pending review{named} on the pull request: submit or delete "
            "it first",
        )

    # -- the read-back -------------------------------------------------------------------------

    def read_comments(self, pr: m.PR, review: Submitted) -> list[str]:
        """The ids (``gh:rc:``) of the inline comments of the review just posted."""
        target, owner, name = self._where(pr)
        segments = ["repos", owner, name, "pulls", path_int(pr.number)]
        items = self._list(target, [*segments, "reviews", path_int(review.rest_id), "comments"])
        ids: list[str] = []
        for item in items:
            comment = item.get("node_id")
            if not isinstance(comment, str) or NODE.fullmatch(comment) is None:
                raise ForgectlError("upstream_error", "a review comment holds no usable node id")
            ids.append(f"gh:rc:{comment}")
        return ids

    def _list(self, target: Target, segments: list[str]) -> list[dict[str, Any]]:
        gateway = self._env.gateway

        def page(number: int) -> tuple[list[dict[str, Any]], bool]:
            query = (("per_page", str(PAGE_SIZE)), ("page", str(number)))
            data = self._env.call(
                lambda: gateway.api(target, "GET", segments, query=query, shape="list").data
            )
            if not all(isinstance(item, dict) for item in data):
                raise ForgectlError("upstream_error", "a review list holds a non-object")
            return data, len(data) >= PAGE_SIZE

        return collect(page, "/".join(segments[-2:]))


__all__ = ["EVENTS", "GitHubReviewer", "Submitted"]
