"""What the light readers (``github_light``, ``gitlab_light``) hand to ``forgectl.poll`` (#118).

A light reader reads, for every open PR the account authored or was asked to review on one host,
only the tuple that tells whether the PR changed since the last read, plus the few fields a list
row needs to show before its snapshot arrives. It never reads a snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class LightUnsupported(Exception):
    """The host cannot be polled lightly (an older GitLab): the caller uses ``prs`` instead."""


@dataclass(frozen=True, slots=True)
class LightItem:
    """One PR of a light poll (``schemas/cli/poll.json``). Every value but the identity is the
    host's, normalized only in type: it is compared for equality with the previous poll and never
    interpreted. A field the host does not have is None."""

    pr: str
    pr_id: str
    repo: str
    number: int
    url: str
    title: str
    author: str | None
    draft: bool
    head_sha: str
    updated_at: str
    ci_state: str | None = None
    review_decision: str | None = None
    approvals_left: int | None = None
    approved: bool | None = None
    merge_status: str | None = None
    notes_count: int | None = None
    roles: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr": self.pr,
            "pr_id": self.pr_id,
            "repo": self.repo,
            "number": self.number,
            "url": self.url,
            "title": self.title,
            "author": self.author,
            "draft": self.draft,
            "head_sha": self.head_sha,
            "updated_at": self.updated_at,
            "ci_state": self.ci_state,
            "review_decision": self.review_decision,
            "approvals_left": self.approvals_left,
            "approved": self.approved,
            "merge_status": self.merge_status,
            "notes_count": self.notes_count,
            "roles": list(self.roles),
        }


@dataclass
class LightPage:
    """One host's answer: its PRs (a PR on both lists once, with both roles) and whether the
    lists were read to their end."""

    items: list[LightItem] = field(default_factory=list)
    complete: bool = True
