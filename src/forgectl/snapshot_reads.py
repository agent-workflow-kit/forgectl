"""Handlers of `snapshot`, `comments`, `reviews` and `ci` (#12): bind the PR to its backend and
call the backend's protocol method. The retry rules and the backends come from ``reads``."""

from __future__ import annotations

from typing import Any

from . import registry
from .errors import ForgectlError
from .identifiers import PrRef
from .reads import backend_for, make_env


def _bound(inv: registry.Invocation) -> PrRef:
    ref = inv.args.pr
    if not isinstance(ref, PrRef):  # bind_args binds it before any handler runs
        raise ForgectlError("internal", f"{inv.command}: the pull request was not bound to a host")
    return ref


@registry.register("snapshot")
def snapshot(inv: registry.Invocation) -> dict[str, Any]:
    ref = _bound(inv)
    return backend_for(ref, make_env(inv)).snapshot(ref).to_dict()


@registry.register("comments")
def comments(inv: registry.Invocation) -> dict[str, Any]:
    ref = _bound(inv)
    return backend_for(ref, make_env(inv)).comments(ref).to_dict()


@registry.register("reviews")
def reviews(inv: registry.Invocation) -> dict[str, Any]:
    ref = _bound(inv)
    return backend_for(ref, make_env(inv)).reviews(ref).to_dict()


@registry.register("ci")
def ci(inv: registry.Invocation) -> dict[str, Any]:
    ref = _bound(inv)
    return backend_for(ref, make_env(inv)).ci(ref).to_dict()
