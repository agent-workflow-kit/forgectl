"""The bounded caches behind the `browse` detail pane (#120)."""

from __future__ import annotations

from forgectl.tui.detail_cache import BoundedCache, detail_key, diff_key


def test_evicts_oldest_beyond_its_bound() -> None:
    cache: BoundedCache[str, int] = BoundedCache(2)
    for i, key in enumerate("abc"):
        cache.put(key, i)
    assert cache.keys() == ["b", "c"]
    assert cache.get("a") is None


def test_put_of_a_held_key_makes_it_newest() -> None:
    cache: BoundedCache[str, int] = BoundedCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 3)
    cache.put("c", 4)
    assert cache.keys() == ["a", "c"]
    assert cache.get("a") == 3


def test_a_bound_below_one_still_holds_one() -> None:
    cache: BoundedCache[str, int] = BoundedCache(0)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.keys() == ["b"]


def test_drop_where() -> None:
    cache: BoundedCache[tuple[str, int], int] = BoundedCache(5)
    cache.put(("x", 1), 1)
    cache.put(("y", 1), 2)
    cache.drop_where(lambda k: k[0] == "x")
    assert cache.keys() == [("y", 1)]


def test_keys_follow_head_and_update_time() -> None:
    item = {"head": {"sha": "abc"}, "updated_at": "t1"}
    assert detail_key("p", item) == ("p", "abc", "t1")
    assert detail_key("p", {**item, "updated_at": "t2"}) != detail_key("p", item)
    assert diff_key("p", item) == ("p", "abc")
    assert diff_key("p", {"head": {"sha": 5}}) is None
    assert diff_key("p", {}) is None
