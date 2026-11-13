"""GitLab's ``review``: draft notes, one ``bulk_publish``, the read-back, the state step
(interface: Post order steps 5, 9 to 11, Reliability).

GitLab has no atomic review. The text is posted as draft notes (one per inline comment, then the
review body) that ``POST /draft_notes/bulk_publish`` publishes together:

- ``GET /draft_notes`` lists the caller's drafts. Any draft before the run is ``drafts_present``:
  forgectl never publishes or deletes a draft it did not create.
- ``POST /draft_notes`` (201, proof an integer ``id``) creates one draft with its ``position``.
- ``DELETE /draft_notes/:id`` (204) removes one of forgectl's own.
- ``POST /draft_notes/bulk_publish`` (204, no body, no ids) publishes all of the caller's drafts.
  The answer proves nothing, so the read-back through ``/discussions`` decides.
- The state step: GraphQL ``mergeRequestRequestChanges`` (17.2) or ``POST /approve`` with ``sha``.

A write is never retried. UNVERIFIED until the live suite (#34): the draft's answer and list
fields (``id``, ``author_id``, ``note``, ``position``), that ``bulk_publish`` takes no body, that a
published draft appears in ``/discussions`` with the account's id as author and the body unchanged,
the ``mergeRequestRequestChanges`` input (``projectPath``, ``iid``) and the ``/approve`` answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .. import model as m
from ..errors import ForgectlError, refused
from ..gateway import Target
from ..reads import ReadEnv
from .gitlab_post import GitLabPoster
from .post_base import created_id, positive_int, same_text

CREATED = frozenset({201})
NO_CONTENT = frozenset({204})
STALE = frozenset({409})
_DISCUSSION = re.compile(r"[0-9a-f]{40}")

_REQUEST_CHANGES = (
    "mutation($projectPath: ID!, $iid: String!) {"
    " mergeRequestRequestChanges(input: {projectPath: $projectPath, iid: $iid}) {"
    " mergeRequest { iid } errors } }"
)


def _obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def host_rejected(
    message: str, *, status: int | None = None, leftover: list[int] | None = None
) -> ForgectlError:
    """Exit 12 ``host_rejected``; ``leftover_drafts`` is the count of drafts left on the host (the
    message names their ids, so the caller can delete them in the web UI)."""
    details: dict[str, Any] = {"status": status}  # null when the host gave no status
    if leftover:
        ids = ", ".join(str(i) for i in sorted(leftover))
        message = f"{message} (drafts left on the merge request: {ids})"
        details["leftover_drafts"] = len(leftover)
    return ForgectlError("host_rejected", message, details=details)


@dataclass(frozen=True)
class Draft:
    id: int
    author_id: str
    note: str
    position: dict[str, Any] | None


@dataclass(frozen=True)
class Found:
    """A published note of the review: its id and its discussion."""

    note_id: int
    discussion: str


def matches_sent(draft: Draft, account_id: str, note: str, position: dict[str, Any] | None) -> bool:
    """Whether a listed draft is exactly what was sent: this account's, the same text and the same
    place (the paths and line numbers of the position; a body draft has none)."""
    if draft.author_id != account_id or not same_text(note, draft.note):
        return False
    if position is None:
        return not draft.position
    seen = draft.position or {}
    return all(seen.get(k) == position.get(k) for k in ("new_path", "old_line", "new_line"))


class GitLabReviewer:
    def __init__(self, env: ReadEnv, poster: GitLabPoster) -> None:
        self._env = env
        self._poster = poster

    def _base(self, pr: m.PR) -> tuple[Target, list[str]]:
        return self._poster.base(pr)

    # -- drafts --------------------------------------------------------------------------------

    def list_drafts(self, pr: m.PR) -> list[Draft]:
        target, base = self._base(pr)
        out: list[Draft] = []
        for item in self._poster.pages(target, [*base, "draft_notes"]):
            ident = positive_int(item.get("id"))
            note = item.get("note")
            if ident is None:
                raise ForgectlError("upstream_error", "a draft note holds no usable id")
            if not isinstance(note, str):
                note = ""  # a draft with an id and no text still counts as an existing draft
            position = item.get("position")
            out.append(
                Draft(
                    ident,
                    str(item.get("author_id")),
                    note,
                    position if isinstance(position, dict) else None,
                )
            )
        return out

    def create_draft(self, pr: m.PR, note: str, position: dict[str, Any] | None) -> int:
        target, base = self._base(pr)
        payload: dict[str, Any] = {"note": note}
        if position is not None:
            payload["position"] = position
        response = self._env.gateway.api(
            target,
            "POST",
            [*base, "draft_notes"],
            body=payload,
            write=True,
            shape="object",
            expect_status=CREATED,
        )
        ident = created_id(response.data, "draft_notes")
        # live-unverified: #34: the answer echoes the text sent; an answer that names other text is
        # not the answer to this write (absent is accepted)
        stored = response.data.get("note")
        if stored is not None and not (isinstance(stored, str) and same_text(note, stored)):
            raise ForgectlError(
                "wrong_host_response",
                "draft_notes: the answer holds other text than was sent",
                details={"observed": "another draft text"},
            )
        return ident

    def delete_draft(self, pr: m.PR, draft_id: int) -> None:
        target, base = self._base(pr)
        self._env.gateway.api(
            target,
            "DELETE",
            [*base, "draft_notes", str(draft_id)],
            write=True,
            expect_status=NO_CONTENT,
        )

    def bulk_publish(self, pr: m.PR) -> None:
        """One call; the answer (204, no body) names nothing, so the caller reads back."""
        target, base = self._base(pr)
        self._env.gateway.api(
            target,
            "POST",
            [*base, "draft_notes", "bulk_publish"],
            write=True,
            expect_status=NO_CONTENT,
        )

    # -- the read-back -------------------------------------------------------------------------

    def highest_note_id(self, pr: m.PR) -> int:
        """The highest note id of the MR, system notes included (0 for none): what the read-back
        takes notes above."""
        target, base = self._base(pr)
        top = 0
        for note in self._poster.pages(target, [*base, "notes"]):
            ident = positive_int(note.get("id"))
            if ident is None:
                raise ForgectlError("upstream_error", "a note holds no usable id")
            top = max(top, ident)
        return top

    def read_back(
        self, pr: m.PR, *, above: int, account_id: str, bodies: list[str]
    ) -> tuple[list[Found | None], list[Found]]:
        """Walk every discussion for this account's notes above ``above``; each sent body takes
        the first unused note whose text matches. Returns, per sent body in order, the note or
        None, and the matched notes."""
        target, base = self._base(pr)
        candidates: list[tuple[int, str, str]] = []
        for discussion in self._poster.pages(target, [*base, "discussions"]):
            ident = discussion.get("id")
            if not (isinstance(ident, str) and _DISCUSSION.fullmatch(ident)):
                raise ForgectlError("upstream_error", "a discussion holds no usable id")
            notes = discussion.get("notes")
            for note in notes if isinstance(notes, list) else []:
                nid, text = positive_int(_obj(note).get("id")), _obj(note).get("body")
                if (
                    nid is not None
                    and nid > above
                    and isinstance(text, str)
                    and _obj(note).get("system") is not True
                    and str(_obj(_obj(note).get("author")).get("id")) == account_id
                ):
                    candidates.append((nid, ident, text))
        candidates.sort()
        used: set[int] = set()
        result: list[Found | None] = []
        for sent in bodies:
            hit = next((c for c in candidates if c[0] not in used and same_text(sent, c[2])), None)
            if hit is None:
                result.append(None)
            else:
                used.add(hit[0])
                result.append(Found(hit[0], hit[1]))
        return result, [f for f in result if f is not None]

    # -- the state step ------------------------------------------------------------------------

    def request_changes(self, pr: m.PR) -> None:
        target = self._env.hosts.target(pr.repo.partition("/")[0])
        project = pr.repo.partition("/")[2]
        result = self._env.gateway.graphql(
            target,
            _REQUEST_CHANGES,
            {"projectPath": project, "iid": str(pr.number)},
            write=True,
            mutation="mergeRequestRequestChanges",
        )
        done = _obj(_obj(result.data).get("mergeRequestRequestChanges"))
        if _obj(done.get("mergeRequest")).get("iid") != str(pr.number):
            raise ForgectlError(
                "outcome_unknown",
                "request changes: the answer does not name this merge request; it may have landed",
            )

    def user_has_approved(self, pr: m.PR) -> bool:
        """``GET /approvals``: whether the authenticated account has approved. Anything but a
        boolean is ``upstream_error``, so the state step fails closed."""
        target, base = self._base(pr)
        response = self._env.call(
            lambda: self._env.gateway.api(target, "GET", [*base, "approvals"], shape="object")
        )
        value = response.data.get("user_has_approved")
        if not isinstance(value, bool):
            raise ForgectlError(
                "upstream_error", "approvals: user_has_approved is missing or not a boolean"
            )
        return value

    def approve(self, pr: m.PR, sha: str) -> None:
        """``POST /approve`` with ``sha`` (the expected head: GitLab refuses another head)."""
        target, base = self._base(pr)
        response = self._env.gateway.api(
            target,
            "POST",
            [*base, "approve"],
            body={"sha": sha},
            write=True,
            shape="object",
            expect_status=CREATED,
            passthrough=STALE,
        )
        if response.status == 409:  # the host enforces ``sha``: the head moved
            raise refused("stale_head", "the head moved: the host refused the approval")
        data = response.data
        # live-unverified: #34: the approvals object names the MR's iid; absent is accepted
        if "iid" in data and str(data.get("iid")) != base[-1]:
            raise ForgectlError(
                "wrong_host_response",
                "approve: the answer belongs to another merge request",
                details={"observed": "another merge request"},
            )


__all__ = ["Draft", "Found", "GitLabReviewer", "host_rejected", "matches_sent"]
