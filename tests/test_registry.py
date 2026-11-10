"""``load_handlers``: complete before anyone looks, and a failed import is retried."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType
from typing import Any

import pytest

from forgectl import registry

MODULES = ("fake.one", "fake.two", "fake.three")


@pytest.fixture
def fake_modules(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A fresh registry whose three modules register one handler each, slowly. Returns the
    modules imported so far, in order."""
    imported: list[str] = []

    def fake_import(name: str) -> ModuleType:
        time.sleep(0.05)  # long enough that a second caller arrives mid-import
        registry.HANDLERS[name] = lambda inv: {}
        imported.append(name)
        return ModuleType(name)

    monkeypatch.setattr(registry, "HANDLERS", {})
    monkeypatch.setattr(registry, "_loaded", False)
    monkeypatch.setattr(registry, "HANDLER_MODULES", MODULES)
    monkeypatch.setattr(registry.importlib, "import_module", fake_import)
    return imported


def test_concurrent_callers_never_see_a_half_loaded_registry(fake_modules: list[str]) -> None:
    threads = 6
    barrier = threading.Barrier(threads)

    def call() -> set[str]:
        barrier.wait()
        registry.load_handlers()
        return set(registry.HANDLERS)

    with ThreadPoolExecutor(threads) as pool:
        seen = [f.result() for f in [pool.submit(call) for _ in range(threads)]]
    assert seen == [set(MODULES)] * threads
    assert fake_modules == list(MODULES)  # each module imported once


def test_a_second_call_after_loading_imports_nothing(fake_modules: list[str]) -> None:
    registry.load_handlers()
    registry.load_handlers()
    assert fake_modules == list(MODULES)


def test_a_failed_import_does_not_poison_later_calls(
    fake_modules: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    good: Callable[[str], Any] = registry.importlib.import_module
    broken = {"on": True}

    def flaky(name: str) -> Any:
        if name == "fake.two" and broken["on"]:
            raise ImportError("cannot import fake.two")
        return good(name)

    monkeypatch.setattr(registry.importlib, "import_module", flaky)
    with pytest.raises(ImportError, match="fake.two"):
        registry.load_handlers()
    with pytest.raises(ImportError, match="fake.two"):  # retried, not silently half-loaded
        registry.load_handlers()
    broken["on"] = False
    registry.load_handlers()
    assert set(registry.HANDLERS) == set(MODULES)
