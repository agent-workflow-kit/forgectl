"""GitHub's ``resolve`` and ``unresolve`` (interface: ``resolve``, ``unresolve``, Reliability).

- The lookup is one GraphQL ``node(id)`` read: its ``__typename`` must be
  ``PullRequestReviewThread`` and it must belong to this PR of this repository, else
  ``not_found``. Every review thread is resolvable on GitHub.
- The write is the ``resolveReviewThread`` or ``unresolveReviewThread`` mutation. Its proof is the
  non-null result the gateway demands plus the answered thread: this id, in the requested state.
  The mutation is idempotent on the host, so a thread that changed after the read is no error.

UNVERIFIED until the live suite (#34): the field names of ``PullRequestReviewThread`` and the
mutations' answers.
"""

from __future__ import annotations

from typing import Any

from .. import model as m
from ..errors import ForgectlError
from ..identifiers import TypedId
from ..reads import ReadEnv
from .post_base import ThreadState

THREAD_QUERY = """
query($id: ID!) {
  node(id: $id) {
    __typename
    ... on PullRequestReviewThread {
      isResolved
      pullRequest { number }
      repository { nameWithOwner }
      comments(first: 1) { nodes { author { login } } }
    }
  }
}
"""

MUTATIONS = {
    True: (
        "resolveReviewThread",
        "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) "
        "{ thread { id isResolved } } }",
    ),
    False: (
        "unresolveReviewThread",
        "mutation($id: ID!) { unresolveReviewThread(input: {threadId: $id}) "
        "{ thread { id isResolved } } }",
    ),
}
_MISSING = "no such review thread in this pull request"


def _obj(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class GitHubThreads:
    def __init__(self, env: ReadEnv) -> None:
        self._env = env

    def prepare(self, pr: m.PR) -> None:
        """Nothing to read first on GitHub."""

    def read_thread(self, pr: m.PR, thread: TypedId, login: str, account_id: str) -> ThreadState:
        if thread.host_prefix != "gh" or thread.kind != "rt":
            raise ForgectlError("not_found", _MISSING)
        target = self._env.hosts.target(pr.repo.partition("/")[0])
        gateway = self._env.gateway
        data = self._env.call(
            lambda: dict(gateway.graphql(target, THREAD_QUERY, {"id": thread.node}).data)
        )
        node = _obj(data.get("node"))
        if not node or node.get("__typename") != "PullRequestReviewThread":
            raise ForgectlError("not_found", _MISSING)
        number = _obj(node.get("pullRequest")).get("number")
        repo = _obj(node.get("repository")).get("nameWithOwner")
        if (
            number != pr.number
            or isinstance(number, bool)
            or not isinstance(repo, str)
            or repo.lower() != pr.repo.partition("/")[2].lower()
        ):
            raise ForgectlError("not_found", _MISSING)
        resolved = node.get("isResolved")
        if not isinstance(resolved, bool):
            raise ForgectlError("upstream_error", "the host answered a thread with no state")
        nodes = _obj(node.get("comments")).get("nodes")
        first = _obj(nodes[0]) if isinstance(nodes, list) and nodes else {}
        author = _obj(first.get("author")).get("login")
        mine = author.lower() == login.lower() if isinstance(author, str) and author else None
        return ThreadState(True, resolved, mine)

    def set_resolved(self, pr: m.PR, thread: TypedId, resolved: bool) -> ThreadState:
        field, query = MUTATIONS[resolved]
        target = self._env.hosts.target(pr.repo.partition("/")[0])
        answer = self._env.gateway.graphql(
            target, query, {"id": thread.node}, write=True, mutation=field
        )
        done = _obj(_obj(_obj(answer.data).get(field)).get("thread"))
        if done.get("isResolved") is not resolved or done.get("id") != thread.node:
            raise ForgectlError(
                "outcome_unknown",
                f"{field}: the answer does not show the thread in the requested state; "
                "the write may have landed",
            )
        return ThreadState(True, resolved, None)


__all__ = ["MUTATIONS", "THREAD_QUERY", "GitHubThreads"]
