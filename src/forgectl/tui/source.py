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


def load_list(source: DataSource) -> Listing:
    """The union of requested-from-you and authored-by-you, as two ``prs`` calls.

    TODO(#58): columns, sorting and filters beyond the default order (``updated_at``
    descending, then ``pr``).
    """
    rows: dict[str, Row] = {}
    complete = True
    errors: list[str] = []
    for role, author, requested in (("requested", None, "me"), ("authored", "me", None)):
        try:
            answer = source.prs(author=author, review_requested_from=requested)
        except ForgectlError as err:
            complete = False
            errors.append(f"{role}: {err.code}: {err.message}")
            continue
        complete = complete and bool(answer.get("complete", False))
        for err_item in answer.get("errors") or []:
            text = f"{err_item.get('host')}: {err_item.get('code')}: {err_item.get('message')}"
            if text not in errors:  # both calls ask every host, so a host fails twice
                errors.append(text)
        for item in answer.get("items") or []:
            row = rows.setdefault(str(item["pr"]), Row(item=item))
            row.roles.add(role)
    # updated_at descending, then pr ascending (two stable sorts)
    ordered = sorted(rows.values(), key=lambda r: r.pr)
    ordered.sort(key=lambda r: str(r.item.get("updated_at", "")), reverse=True)
    return Listing(rows=ordered, complete=complete, errors=errors)
