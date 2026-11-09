"""Handlers of ``prs``, ``access`` and ``whoami`` (#14); the multi-host logic is in fanout.py."""

from __future__ import annotations

from typing import Any

from . import registry
from .backends import create_backend
from .errors import ForgectlError
from .fanout import PrsArgs, candidate_hosts, check_me_spelling, host_error, resolve_me, run_prs
from .identifiers import RepoRef
from .output import emit_warning
from .reads import make_env
from .sanitize import sanitize_line


@registry.register("prs")
def prs(inv: registry.Invocation) -> dict[str, Any]:
    a = inv.args
    return run_prs(
        make_env(inv),
        PrsArgs(
            repo=a.repo,
            host=a.host,
            author=a.author,
            review_requested_from=a.review_requested_from,
            state=a.state,
            draft=a.draft,
            updated_since=a.updated_since,
            limit=a.limit,
        ),
    )


@registry.register("access")
def access(inv: registry.Invocation) -> dict[str, Any]:
    repo = inv.args.repo
    if not isinstance(repo, RepoRef):  # bind_args binds it before any handler runs
        raise ForgectlError("internal", "access: the repository was not bound to a host")
    user = inv.args.user
    check_me_spelling(user, "the user")
    env = make_env(inv)
    if user == "me":
        user = resolve_me(env, repo.host)
    return create_backend(repo.backend, env).access(repo, user).to_dict()


@registry.register("whoami")
def whoami(inv: registry.Invocation) -> dict[str, Any]:
    env = make_env(inv)
    hosts = (
        [inv.args.host] if inv.args.host is not None else candidate_hosts(env.gateway.config.config)
    )
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for host in hosts:
        identity = env.hosts.identity(host, call=env.call)  # retried like any read
        entry = identity.as_json()
        for key in ("login", "me"):  # untrusted one-line fields
            if isinstance(entry[key], str):
                entry[key] = sanitize_line(entry[key])
        if entry["scopes"] is not None:
            entry["scopes"] = [sanitize_line(s) for s in entry["scopes"]]
        entries.append(entry)
        if identity.me_matches_login is False:  # interface: a mismatch warns
            emit_warning(
                f"{host}: me is {entry['me']} but the authenticated login is {entry['login']};"
                " prs uses me, posts and rules act as the authenticated account"
            )
        if identity.error is not None:
            errors.append(
                host_error(host, ForgectlError(identity.error["code"], identity.error["message"]))
            )
    output = {"hosts": entries, "errors": errors}
    if errors:
        shown = ", ".join(f"{e['host']} ({e['code']})" for e in errors)
        raise ForgectlError(
            "partial_results", f"{len(errors)} of {len(hosts)} hosts failed: {shown}", output=output
        )
    return output
