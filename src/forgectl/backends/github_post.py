"""GitHub's text posts: ``comment`` and ``reply`` (interface: Post order, Reliability).

- ``comment``: ``POST /repos/{o}/{r}/issues/{n}/comments``, status 201, proof an integer ``id``.
- ``reply`` to a review comment or thread: ``POST /repos/{o}/{r}/pulls/{n}/comments/{id}/replies``,
  status 201, the same proof. GitHub normalizes a reply to a reply to the thread's root, and so
  does this module, by naming the root. A reply to a general comment or a review has no thread to
  join: it posts a general comment and is reported ``threaded: false``.
- The lookup of ``--to`` is one GraphQL ``node(id)`` read: its ``__typename`` must match the id's
  prefix and it must belong to this PR of this repository, else ``not_found``.

A write is never retried. ``gh`` follows a 301, 302 or 303 by sending a GET without the body, so a
redirected POST answers 200 (``outcome_unknown`` by the status table) or a list (``host_rejected``
by the shape); only a 201 with an integer ``id`` is a post. UNVERIFIED until the live suite (#34):
the ``node(id)`` query's field names on ``IssueComment``, and the 201 answers.
"""

from __future__ import annotations

from typing import Any

from .. import model as m
from ..errors import ForgectlError
from ..gateway import Target, path_int, verify_identity
from ..identifiers import TypedId
from ..reads import ReadEnv
from .post_base import (
    NODE,
    PAGE_SIZE,
    Anchor,
    Posted,
    collect,
    created_id,
    last_line,
    positive_int,
)

CREATED = frozenset({201})

ANCHOR_QUERY = """
query($id: ID!) {
  node(id: $id) {
    __typename
    ... on IssueComment { pullRequest { number } repository { nameWithOwner } }
    ... on PullRequestReview { pullRequest { number } repository { nameWithOwner } }
    ... on PullRequestReviewComment {
      databaseId
      replyTo { databaseId }
      pullRequest { number }
      repository { nameWithOwner }
    }
    ... on PullRequestReviewThread {
      pullRequest { number }
      repository { nameWithOwner }
      comments(first: 1) { nodes { databaseId } }
    }
  }
}
"""

_TYPENAME = {
    "ic": "IssueComment",
    "rc": "PullRequestReviewComment",
    "rt": "PullRequestReviewThread",
    "r": "PullRequestReview",
}
_MISSING = "no such comment, thread or review in this pull request"


def _obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _not_found() -> ForgectlError:
    return ForgectlError("not_found", _MISSING)


class GitHubPoster:
    def __init__(self, env: ReadEnv) -> None:
        self._env = env

    # -- plumbing ------------------------------------------------------------------------------

    def _where(self, pr: m.PR) -> tuple[Target, str, str]:
        host, _, path = pr.repo.partition("/")
        owner, _, name = path.partition("/")
        return self._env.hosts.target(host), owner, name

    def _created(self, data: Any, target: Target, pr: m.PR, prefix: str, where: str) -> str:
        """The id of what a 201 created. Its URL must be this host's and this repository's."""
        created_id(data, where)
        node = data.get("node_id")
        if not isinstance(node, str) or NODE.fullmatch(node) is None:
            raise ForgectlError(
                "outcome_unknown",
                f"{where}: the answer holds no usable node id; it may have landed",
            )
        verify_identity(
            data.get("html_url"),
            host=target.host,
            project=pr.repo.partition("/")[2],
            secret=target.token,
        )
        return f"gh:{prefix}:{node}"

    # -- the lookup of --to --------------------------------------------------------------------

    def find_anchor(self, pr: m.PR, to: TypedId) -> Anchor:
        if to.host_prefix != "gh":
            raise _not_found()
        target, _, _ = self._where(pr)
        gateway = self._env.gateway
        data = self._env.call(
            lambda: dict(gateway.graphql(target, ANCHOR_QUERY, {"id": to.node}).data)
        )
        node = _obj(data.get("node"))
        if not node or node.get("__typename") != _TYPENAME.get(to.kind):
            raise _not_found()
        number = _obj(node.get("pullRequest")).get("number")
        repo = _obj(node.get("repository")).get("nameWithOwner")
        if (
            number != pr.number
            or isinstance(number, bool)
            or not isinstance(repo, str)
            or repo.lower() != pr.repo.partition("/")[2].lower()
        ):
            raise _not_found()
        if to.kind in ("ic", "r"):  # nothing to join: a new general comment
            return Anchor(to, False, None, None)
        if to.kind == "rt":
            nodes = _obj(node.get("comments")).get("nodes")
            first = _obj(nodes[0]) if isinstance(nodes, list) and nodes else {}
            root = positive_int(first.get("databaseId"))
            thread = str(to)
        else:  # rc: a reply to a reply goes to the root
            reply = positive_int(_obj(node.get("replyTo")).get("databaseId"))
            root = reply if reply is not None else positive_int(node.get("databaseId"))
            thread = None
        if root is None:
            raise ForgectlError("upstream_error", "the host answered a thread with no comment id")
        return Anchor(to, True, thread, str(root))

    # -- the search for a dedupe marker --------------------------------------------------------

    def find_marker(self, pr: m.PR, account_id: str, marker: str) -> list[str]:
        target, owner, name = self._where(pr)
        number = path_int(pr.number)
        lists = (
            (["repos", owner, name, "issues", number, "comments"], "ic"),
            (["repos", owner, name, "pulls", number, "comments"], "rc"),
            (["repos", owner, name, "pulls", number, "reviews"], "r"),
        )
        found: list[str] = []
        for segments, prefix in lists:
            for item in self._list(target, segments):
                if prefix == "r" and item.get("state") == "PENDING":
                    continue  # a pending review is never searched
                body = item.get("body")
                user = _obj(item.get("user"))
                node = item.get("node_id")
                if (
                    isinstance(body, str)
                    and str(user.get("id")) == account_id
                    and last_line(body) == marker
                    and isinstance(node, str)
                    and NODE.fullmatch(node)
                ):
                    found.append(f"gh:{prefix}:{node}")
        return found

    def _list(self, target: Target, segments: list[str]) -> list[dict[str, Any]]:
        gateway = self._env.gateway

        def page(number: int) -> tuple[list[dict[str, Any]], bool]:
            query = (("per_page", str(PAGE_SIZE)), ("page", str(number)))
            data = self._env.call(
                lambda: gateway.api(target, "GET", segments, query=query, shape="list").data
            )
            if not all(isinstance(item, dict) for item in data):
                raise ForgectlError("upstream_error", "a comment list holds a non-object")
            return data, len(data) >= PAGE_SIZE

        return collect(page, "/".join(segments[-2:]))

    # -- the writes ----------------------------------------------------------------------------

    def post_comment(self, pr: m.PR, body: str) -> Posted:
        target, owner, name = self._where(pr)
        response = self._env.gateway.api(
            target,
            "POST",
            ["repos", owner, name, "issues", path_int(pr.number), "comments"],
            body={"body": body},
            write=True,
            shape="object",
            expect_status=CREATED,
        )
        return Posted((self._created(response.data, target, pr, "ic", "issue comment"),), ())

    def post_reply(self, pr: m.PR, anchor: Anchor, body: str) -> Posted:
        if not anchor.threaded:
            return self.post_comment(pr, body)
        assert anchor.host_id is not None
        target, owner, name = self._where(pr)
        response = self._env.gateway.api(
            target,
            "POST",
            [
                "repos",
                owner,
                name,
                "pulls",
                path_int(pr.number),
                "comments",
                anchor.host_id,
                "replies",
            ],
            body={"body": body},
            write=True,
            shape="object",
            expect_status=CREATED,
        )
        new = self._created(response.data, target, pr, "rc", "review comment reply")
        threads = (anchor.thread_id,) if anchor.thread_id else ()
        return Posted((new,), threads)


__all__ = ["ANCHOR_QUERY", "GitHubPoster"]
