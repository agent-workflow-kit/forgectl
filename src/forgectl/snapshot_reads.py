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


def drop_bodies(snap: dict[str, Any]) -> dict[str, Any]:
    """``snapshot --no-bodies`` (#141): the snapshot without the free text of its reviews and its
    comments. The PR's own ``body`` stays. The review and comment ``body`` keys are absent, not
    empty: an empty body is a real answer. Only those two places are touched, so ids, authors,
    states, shas, ``ci``, ``activity`` and ``incomplete`` are what the full snapshot has. The
    input is not changed."""
    out = dict(snap)
    out["reviews"] = [{k: v for k, v in r.items() if k != "body"} for r in snap["reviews"]]
    out["threads"] = [
        {
            **t,
            "comments": [{k: v for k, v in c.items() if k != "body"} for c in t["comments"]],
        }
        for t in snap["threads"]
    ]
    return out


@registry.register("snapshot")
def snapshot(inv: registry.Invocation) -> dict[str, Any]:
    ref = _bound(inv)
    out = backend_for(ref, make_env(inv)).snapshot(ref).to_dict()
    return drop_bodies(out) if inv.args.no_bodies else out


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
