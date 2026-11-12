"""GitLab text posts: ``comment`` and ``reply`` (interface: Post order, Neutralizing, Reliability).

- ``comment``: ``POST /projects/{id}/merge_requests/{iid}/notes``.
- ``reply``: ``POST /projects/{id}/merge_requests/{iid}/discussions/{discussion}/notes``.
- Both: status 201, proof an integer ``id``. Writes go to the numeric project id, read once from
  the MR. ``forgectl`` never sends ``merge_request_diff_head_sha``.

**Backstop.** On GitLab a quick action runs from any line of a note, so a post is checked after the
fact (Neutralizing, Backstop): an answer of 202, a non-empty ``commands_changes``, or a stored body
that is not the one sent is ``quick_action_ran``. An empty ``commands_changes`` proves nothing, so
the stored body is compared too. UNVERIFIED until the live suite (#34): the exact 202 shape and
whether an individual note takes a reply.
"""

from __future__ import annotations

import re
from typing import Any

from .. import model as m
from ..errors import ForgectlError
from ..gateway import Response, Target, path_int, shown_text, verify_identity
from ..identifiers import TypedId
from ..reads import ReadEnv
from .post_base import (
    PAGE_SIZE,
    Anchor,
    Posted,
    collect,
    created_id,
    last_line,
    positive_int,
    same_text,
)

CREATED = frozenset({201})
ACCEPTED = frozenset({202})
_DISCUSSION = re.compile(r"[0-9a-f]{40}")


def _obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _not_found() -> ForgectlError:
    return ForgectlError("not_found", "no such comment or thread in this merge request")


class GitLabPoster:
    def __init__(self, env: ReadEnv) -> None:
        self._env = env
        self._mr: dict[tuple[str, int], tuple[Target, str, str]] = {}

    def _where(self, pr: m.PR) -> tuple[Target, str, str]:
        """The target, the numeric project id and the MR's iid, from one read of the MR."""
        key = (pr.repo, pr.number)
        if key not in self._mr:
            host, _, path = pr.repo.partition("/")
            target = self._env.hosts.target(host)
            gateway = self._env.gateway
            segments = ["projects", path, "merge_requests", path_int(pr.number)]
            data = self._env.call(lambda: gateway.api(target, "GET", segments, shape="object").data)
            verify_identity(data.get("web_url"), host=target.host, secret=target.token)
            project = positive_int(data.get("project_id"))
            if project is None or data.get("iid") != pr.number:
                raise ForgectlError("upstream_error", "the merge request answer is not usable")
            self._mr[key] = (target, str(project), path_int(pr.number))
        return self._mr[key]

    def prepare(self, pr: m.PR) -> None:
        self._where(pr)

    def _base(self, pr: m.PR) -> tuple[Target, list[str]]:
        target, project, iid = self._where(pr)
        return target, ["projects", project, "merge_requests", iid]

    # -- lookups -------------------------------------------------------------------------------

    def _pages(self, target: Target, segments: list[str]) -> list[dict[str, Any]]:
        gateway = self._env.gateway

        def page(number: int) -> tuple[list[dict[str, Any]], bool]:
            query = (("per_page", str(PAGE_SIZE)), ("page", str(number)))
            response = self._env.call(
                lambda: gateway.api(target, "GET", segments, query=query, shape="list")
            )
            data = response.data
            if not all(isinstance(item, dict) for item in data):
                raise ForgectlError("upstream_error", "a list holds a non-object")
            header = response.headers.get("x-next-page")
            more = bool(header and header.strip()) if header is not None else len(data) >= PAGE_SIZE
            return data, more

        return collect(page, "/".join(segments[-2:]))

    def find_anchor(self, pr: m.PR, to: TypedId) -> Anchor:
        if to.host_prefix != "gl":
            raise _not_found()
        target, base = self._base(pr)
        if to.kind == "d":
            gateway = self._env.gateway
            segments = [*base, "discussions", to.node]
            data = self._env.call(lambda: gateway.api(target, "GET", segments, shape="object").data)
            if data.get("id") != to.node:
                raise ForgectlError("upstream_error", "the discussion answer is not the one asked")
            return Anchor(to, True, str(to), to.node)
        wanted = int(to.node)
        for discussion in self._pages(target, [*base, "discussions"]):
            notes = discussion.get("notes")
            for note in notes if isinstance(notes, list) else []:
                if _obj(note).get("id") == wanted:
                    ident = discussion.get("id")
                    if _obj(note).get("system") is True or not (
                        isinstance(ident, str) and _DISCUSSION.fullmatch(ident)
                    ):
                        raise _not_found()
                    return Anchor(to, True, f"gl:d:{ident}", ident)
        raise _not_found()

    def find_marker(self, pr: m.PR, account_id: str, marker: str) -> list[str]:
        target, base = self._base(pr)
        found: list[str] = []
        for note in self._pages(target, [*base, "notes"]):
            body = note.get("body")
            note_id = positive_int(note.get("id"))
            if (
                isinstance(body, str)
                and note_id is not None
                and str(_obj(note.get("author")).get("id")) == account_id
                and last_line(body) == marker
            ):
                found.append(f"gl:n:{note_id}")
        return found

    # -- the writes ----------------------------------------------------------------------------

    def _post(
        self, target: Target, segments: list[str], body: str, thread: tuple[str, ...], iid: str
    ) -> Posted:
        response: Response = self._env.gateway.api(
            target,
            "POST",
            segments,
            body={"body": body},
            write=True,
            shape="object",
            expect_status=CREATED,
            passthrough=ACCEPTED,
        )
        data = response.data
        where = "/".join(segments[-2:])
        if response.status == 202:  # the note became a quick action
            known = positive_int(_obj(data).get("id"))
            return Posted(() if known is None else (f"gl:n:{known}",), thread, True)
        note = created_id(data, where)
        # UNVERIFIED (#34): GitLab documents ``noteable_type`` and ``noteable_iid`` on a note; an
        # answer that names another noteable is not the answer to this write, and absent fields
        # are accepted.
        kind, number = data.get("noteable_type"), data.get("noteable_iid")
        if ("noteable_type" in data and kind != "MergeRequest") or (
            "noteable_iid" in data and str(number) != iid
        ):
            raise ForgectlError(
                "wrong_host_response",
                f"{where}: the note answered belongs to another noteable",
                details={"observed": shown_text(f"{kind} {number}", target.token)},
            )
        stored = data.get("body")
        ran = bool(data.get("commands_changes")) or not (
            isinstance(stored, str) and same_text(body, stored)
        )
        return Posted((f"gl:n:{note}",), thread, ran)

    def post_comment(self, pr: m.PR, body: str) -> Posted:
        target, base = self._base(pr)
        return self._post(target, [*base, "notes"], body, (), base[-1])

    def post_reply(self, pr: m.PR, anchor: Anchor, body: str) -> Posted:
        assert anchor.host_id is not None
        target, base = self._base(pr)
        thread = (anchor.thread_id,) if anchor.thread_id else ()
        segments = [*base, "discussions", anchor.host_id, "notes"]
        return self._post(target, segments, body, thread, base[-1])


__all__ = ["GitLabPoster"]
