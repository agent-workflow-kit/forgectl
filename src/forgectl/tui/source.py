"""Where `browse` gets its data: a small protocol, and the list it builds from two calls.

The answers have the normalized objects' ``to_dict`` shapes of ``schemas/cli/``: ``prs`` the
``prs`` output, ``snapshot`` the ``snapshot`` output (the PR object plus CI, reviews and
threads), ``diff`` the ``diff`` output. Tests substitute an in-memory fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from forgectl.errors import ForgectlError


class DataSource(Protocol):
    def prs(
        self,
        *,
        author: str | None = None,
        review_requested_from: str | None = None,
        state: str = "open",
        limit: int = 100,
    ) -> dict[str, Any]: ...

    def snapshot(self, pr: str) -> dict[str, Any]: ...

    def diff(self, pr: str) -> dict[str, Any]: ...


@dataclass
class Row:
    """One list row: the ``prs`` item and why it is listed."""

    item: dict[str, Any]
    roles: set[str] = field(default_factory=set)  # "authored" and/or "requested"

    @property
    def pr(self) -> str:
        return str(self.item["pr"])


@dataclass
class Listing:
    rows: list[Row]
    complete: bool
    errors: list[str]
    rate_limits: list[Any] = field(default_factory=list)  # the ``rate_limit`` of each answer
    answered: int = 0  # how many of the ``prs`` calls returned an answer at all
    failed_hosts: set[str] = field(default_factory=set)  # hosts named by the errors
    hosts_unknown: bool = False  # a failure that names no host: any host may be hiding PRs
    light_items: list[dict[str, Any]] = field(default_factory=list)  # a light poll's tuples (#118)

    def may_hide(self, host: str) -> bool:
        """Whether a PR on ``host`` missing from this incomplete listing may only be hidden by
        the failure, not closed. A failure naming no host could hide any host's PRs."""
        return self.hosts_unknown or host in self.failed_hosts


def load_list(source: DataSource) -> Listing:
    """The union of requested-from-you and authored-by-you, as two ``prs`` calls.

    TODO(#58): columns, sorting and filters beyond the default order (``updated_at``
    descending, then ``pr``).
    """
    rows: dict[str, Row] = {}
    complete = True
    errors: list[str] = []
    rate_limits: list[Any] = []
    answered = 0
    failed_hosts: set[str] = set()
    hosts_unknown = False
    for role, author, requested in (("requested", None, "me"), ("authored", "me", None)):
        try:
            answer = source.prs(author=author, review_requested_from=requested)
        except ForgectlError as err:
            complete = False
            hosts_unknown = True  # the whole call failed: no host is named
            errors.append(f"{role}: {err.code}: {err.message}")
            continue
        answered += 1
        answer_complete = bool(answer.get("complete", False))
        complete = complete and answer_complete
        named = False
        rate_limits.append(answer.get("rate_limit"))
        for err_item in answer.get("errors") or []:
            host = err_item.get("host")
            if isinstance(host, str) and host:
                failed_hosts.add(host)
                named = True
            text = f"{err_item.get('host')}: {err_item.get('code')}: {err_item.get('message')}"
            if text not in errors:  # both calls ask every host, so a host fails twice
                errors.append(text)
        if not answer_complete and not named:
            hosts_unknown = True  # incomplete, and no error says which host
        for item in answer.get("items") or []:
            row = rows.setdefault(str(item["pr"]), Row(item=item))
            row.roles.add(role)
    # updated_at descending, then pr ascending (two stable sorts)
    ordered = sorted(rows.values(), key=lambda r: r.pr)
    ordered.sort(key=lambda r: str(r.item.get("updated_at", "")), reverse=True)
    return Listing(
        rows=ordered,
        complete=complete,
        errors=errors,
        rate_limits=rate_limits,
        answered=answered,
        failed_hosts=failed_hosts,
        hosts_unknown=hosts_unknown,
    )
