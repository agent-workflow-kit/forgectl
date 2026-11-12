"""GitLab's ``resolve`` and ``unresolve`` (interface: ``resolve``, ``unresolve``, Reliability).

- The lookup is ``GET /projects/{id}/merge_requests/{iid}/discussions/{id}``: another MR's
  discussion answers 404 (``not_found``). A discussion is resolvable when it is not an individual
  note and any of its notes is, and resolved when every resolvable note is. A half-resolved
  discussion is neither resolved (for ``resolve``) nor unresolved (for ``unresolve``): both write
  it, and an answer leaving a mix is ``outcome_unknown``.
- The write is ``PUT .../discussions/{id}`` with ``{"resolved": <bool>}``, status 200. Its proof is
  the answered discussion: this id, in the requested state. A redirected write answers a GET's
  list or a discussion in the old state, so it fails that proof and is ``outcome_unknown``.

UNVERIFIED until the live suite (#34): whether the PUT answers the discussion with the notes'
``resolved`` flags.
"""

from __future__ import annotations

from typing import Any

from .. import model as m
from ..errors import ForgectlError
from ..identifiers import TypedId
from .gitlab_post import GitLabPoster
from .post_base import ThreadState

OK = frozenset({200})


def _obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def thread_state(data: Any, account_id: str) -> ThreadState:
    """The state of a discussion's answer; a shape that is not a discussion is
    ``upstream_error``."""
    notes = _obj(data).get("notes")
    if not isinstance(notes, list) or not notes or not all(isinstance(n, dict) for n in notes):
        raise ForgectlError("upstream_error", "the discussion answer holds no notes")
    individual = _obj(data).get("individual_note") is True
    resolvable_notes = [n for n in notes if n.get("resolvable") is True]
    resolvable = not individual and bool(resolvable_notes)
    resolved = resolvable and all(n.get("resolved") is True for n in resolvable_notes)
    author = _obj(notes[0].get("author")).get("id")
    any_resolved = resolvable and any(n.get("resolved") is True for n in resolvable_notes)
    mine = None if author is None else str(author) == account_id
    return ThreadState(resolvable, resolved, mine, any_resolved)


class GitLabThreads(GitLabPoster):
    """Reuses the poster's read of the MR (the numeric project id and the iid)."""

    def read_thread(self, pr: m.PR, thread: TypedId, login: str, account_id: str) -> ThreadState:
        if thread.host_prefix != "gl" or thread.kind != "d":
            raise ForgectlError("not_found", "no such discussion in this merge request")
        target, base = self._base(pr)
        gateway = self._env.gateway
        segments = [*base, "discussions", thread.node]
        data = self._env.call(lambda: gateway.api(target, "GET", segments, shape="object").data)
        if data.get("id") != thread.node:
            raise ForgectlError("upstream_error", "the discussion answer is not the one asked")
        return thread_state(data, account_id)

    def set_resolved(self, pr: m.PR, thread: TypedId, resolved: bool) -> ThreadState:
        target, base = self._base(pr)
        segments = [*base, "discussions", thread.node]
        response = self._env.gateway.api(
            target,
            "PUT",
            segments,
            body={"resolved": resolved},
            write=True,
            shape="object",
            expect_status=OK,
        )
        data = response.data
        state: ThreadState | None = None
        if data.get("id") == thread.node:
            try:
                state = thread_state(data, "")
            except ForgectlError:  # not a discussion: the write may still have landed
                state = None
        if state is None or not state.resolvable or not state.settled(resolved):
            raise ForgectlError(
                "outcome_unknown",
                "the answer does not show the discussion in the requested state; "
                "the write may have landed",
            )
        return ThreadState(True, resolved, None)


__all__ = ["GitLabThreads", "thread_state"]
