"""In-memory caches of `browse`'s detail pane (D-40): the detail read by what identifies its
version, and the diff by head sha. Bounded; the oldest entry goes first; never persisted."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any

DETAIL_ENTRIES = 40
DIFF_ENTRIES = 12  # a diff is the heaviest read (about 1 MB for a big PR)


class BoundedCache[K: Hashable, V]:
    """A dict of at most ``limit`` entries. A ``put`` of a key already held refreshes it as the
    newest; a ``get`` does not change the order (the oldest *read* goes first, not the least
    recently used, which keeps the rule one line)."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, limit)
        self._items: OrderedDict[K, V] = OrderedDict()

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def get(self, key: K) -> V | None:
        return self._items.get(key)

    def put(self, key: K, value: V) -> None:
        self._items.pop(key, None)
        self._items[key] = value
        while len(self._items) > self.limit:
            self._items.popitem(last=False)

    def keys(self) -> list[K]:
        return list(self._items)

    def drop_where(self, test: Callable[[K], bool]) -> None:
        for key in [k for k in self._items if test(k)]:
            del self._items[key]


# A detail entry is keyed by (pr, head sha, updated_at) of the list item; the entry also holds the
# CI state and review decision it was read with, so a row whose summary has moved on misses.
DetailKey = tuple[str, str | None, str | None]
DiffKey = tuple[str, str]  # (pr, head sha)


def detail_key(pr: str, item: dict[str, Any]) -> DetailKey:
    head = item.get("head")
    sha = head.get("sha") if isinstance(head, dict) else None
    updated = item.get("updated_at")
    return (
        pr,
        sha if isinstance(sha, str) else None,
        updated if isinstance(updated, str) else None,
    )


def diff_key(pr: str, item: dict[str, Any]) -> DiffKey | None:
    """None (never cached) when the item has no head sha."""
    sha = detail_key(pr, item)[1]
    return (pr, sha) if sha else None
