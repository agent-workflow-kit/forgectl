"""Host helpers: throwaway repository or project lifecycle, branches, commits, PRs and MRs,
protected branches and CI files. Each helper writes only to the resource this run created.

``GitHubLive`` and ``GitLabLive`` share method names so a suite can be written once per host.
Response fields and statuses used here are the documented ones; where a real run may differ,
the cleanup falls through to archiving rather than leaving a resource undeclared.
"""

from __future__ import annotations

import base64
import contextlib
import subprocess
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .api import LiveApi, LiveApiError
from .guards import (
    LiveConfig,
    require_account,
    require_host,
    require_owned_by,
    require_throwaway_name,
)

DESCRIPTION = "forgectl live test, safe to delete"


@dataclass
class Throwaway:
    backend: str
    host: str
    name: str
    owner: str
    ident: str  # GitHub: owner/name. GitLab: the numeric project id.
    default_branch: str
    web_url: str
    status: str = "live"  # live, deleted, scheduled, archived, left
    unconfirmed: bool = False  # the creation's answer was lost: the host may still finish it
    note: str = ""


# What a failed or timed-out call can raise; a timeout must still reach the archive fallback.
CALL_ERRORS = (LiveApiError, subprocess.SubprocessError, OSError)


class _Base:
    backend = ""
    creation = ""

    def __init__(self, api: LiveApi, cfg: LiveConfig, ledger: Ledger | None = None) -> None:
        require_host(cfg.host)
        self.api, self.cfg = api, cfg
        self.ledger = ledger if ledger is not None else Ledger()

    def whoami(self) -> str:
        """The login the token resolves to, checked against the declared one."""
        login = self._login(self.api.expect("GET", "user", [200]))
        require_account(self.cfg.login, login)
        return login

    def _login(self, user: Any) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _lookup(self, t: Throwaway) -> bool:  # pragma: no cover - overridden
        """Fill ``ident`` from a lookup by name and allow writes to it; false when it is absent."""
        raise NotImplementedError

    def _resolve(self, t: Throwaway) -> None:
        """A creation whose answer was lost: find the resource by name, or note it never existed."""
        if t.ident:
            return
        if self._lookup(t):
            return
        if t.unconfirmed:
            # A timeout or an answer without a status: the host may still finish the creation.
            t.status = "left"
            t.note = "the creation may still complete: verify by name, then delete it by hand"
        else:
            t.status = "deleted"  # the host refused or never created it: nothing to clean up

    def _creating(self, t: Throwaway, error: BaseException) -> None:
        """Called when the creation request failed after it may have reached the host. Only a
        definite client refusal (a 4xx other than 429) means it was never created; a lost answer,
        a 5xx, a 429 or an interrupt leaves the outcome open."""
        t.unconfirmed = not (
            isinstance(error, LiveApiError) and 400 <= error.status < 500 and error.status != 429
        ) and not isinstance(error, KeyError | TypeError | ValueError)
        with contextlib.suppress(*CALL_ERRORS):
            self._resolve(t)
            if t.status == "live":
                self.delete_or_archive(t)

    def delete_or_archive(self, t: Throwaway) -> str:  # pragma: no cover - overridden
        raise NotImplementedError


class GitHubLive(_Base):
    backend = "github"
    creation = "user/repos"

    def _login(self, user: Any) -> str:
        return str(user.get("login", ""))

    def create_throwaway(self, name: str, *, private: bool = False) -> Throwaway:
        """A repository in the declared account. Public by default so branch protection works
        on a free plan; it holds nothing but what the tests push."""
        require_throwaway_name(name)
        body = {"name": name, "private": private, "auto_init": True, "description": DESCRIPTION}
        throwaway = Throwaway("github", self.cfg.host, name, "", "", "main", "")
        # Registered before the request: a creation that succeeds on the host but fails here
        # (a timeout, an unreadable answer) is still found by name and cleaned up.
        self.ledger.register(self, throwaway)
        try:
            data = self.api.expect("POST", "user/repos", [201], body)
            throwaway.owner = str(data["owner"]["login"])
            throwaway.ident = f"{throwaway.owner}/{name}"
            throwaway.default_branch = str(data.get("default_branch", "main"))
            throwaway.web_url = str(data.get("html_url", ""))
            self.api.scope.allow(f"repos/{throwaway.ident}")
        except BaseException as error:
            throwaway.ident = ""
            self._creating(throwaway, error)
            raise
        require_owned_by(throwaway.owner, self.cfg.login)
        return throwaway

    def _lookup(self, t: Throwaway) -> bool:
        ident = f"{self.cfg.login}/{t.name}"
        found = self.api.request("GET", f"repos/{ident}")
        if found.status == 404:
            return False
        if not found.ok():
            raise LiveApiError(f"lookup answered {found.status}", found.status)
        t.owner, t.ident = self.cfg.login, ident
        self.api.scope.allow(f"repos/{ident}")
        return True

    def _repo(self, t: Throwaway) -> str:
        require_throwaway_name(t.name)
        return f"repos/{t.ident}"

    def branch_sha(self, t: Throwaway, branch: str) -> str:
        data = self.api.expect("GET", f"{self._repo(t)}/git/ref/heads/{quote(branch)}", [200])
        return str(data["object"]["sha"])

    def create_branch(self, t: Throwaway, name: str, from_sha: str) -> None:
        body = {"ref": f"refs/heads/{name}", "sha": from_sha}
        self.api.expect("POST", f"{self._repo(t)}/git/refs", [201], body)

    def commit_file(
        self,
        t: Throwaway,
        branch: str,
        path: str,
        content: str,
        message: str,
        *,
        replace_sha: str | None = None,
    ) -> str:
        """Create (or, with the blob sha, replace) a file; returns the commit sha."""
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if replace_sha:
            body["sha"] = replace_sha
        data = self.api.expect("PUT", f"{self._repo(t)}/contents/{quote(path)}", [200, 201], body)
        return str(data["commit"]["sha"])

    def open_change(
        self, t: Throwaway, head: str, base: str, title: str, body: str = "", *, draft: bool = False
    ) -> int:
        payload = {"head": head, "base": base, "title": title, "body": body, "draft": draft}
        data = self.api.expect("POST", f"{self._repo(t)}/pulls", [201], payload)
        return int(data["number"])

    def protect_branch(
        self,
        t: Throwaway,
        branch: str,
        *,
        required_checks: tuple[str, ...] = (),
        approvals: int = 0,
    ) -> None:
        """Classic branch protection (public repositories on a free plan)."""
        body = {
            "required_status_checks": (
                {"strict": False, "contexts": list(required_checks)} if required_checks else None
            ),
            "enforce_admins": False,
            "required_pull_request_reviews": (
                {"required_approving_review_count": approvals} if approvals else None
            ),
            "restrictions": None,
        }
        self.api.expect("PUT", f"{self._repo(t)}/branches/{quote(branch)}/protection", [200], body)

    def add_ci_file(self, t: Throwaway, branch: str, name: str, workflow: str) -> str:
        """An Actions workflow (the token needs the ``workflow`` scope)."""
        return self.commit_file(t, branch, f".github/workflows/{name}.yml", workflow, f"ci: {name}")

    def exists(self, t: Throwaway) -> Any:
        return self.api.request("GET", self._repo(t))

    def delete_or_archive(self, t: Throwaway) -> str:
        """Delete; if the token cannot (no ``delete_repo`` scope), archive. Verified by reading."""
        try:
            self._resolve(t)
            if t.status != "live":
                return t.status
            if self.api.request("DELETE", self._repo(t)).status == 204:
                if self.exists(t).status == 404:
                    t.status = "deleted"
                    return t.status
        except CALL_ERRORS:
            pass
        try:
            self._resolve(t)
            self.api.expect("PATCH", self._repo(t), [200], {"archived": True})
            seen = self.exists(t)
            archived = isinstance(seen.data, dict) and seen.data.get("archived") is True
            t.status = "archived" if archived else "left"
        except CALL_ERRORS:
            t.status = "left"
        return t.status


class GitLabLive(_Base):
    backend = "gitlab"
    creation = "projects"

    def _login(self, user: Any) -> str:
        return str(user.get("username", ""))

    def create_throwaway(self, name: str, *, visibility: str = "public") -> Throwaway:
        """A project in the declared account's own namespace (no ``namespace_id`` is ever sent)."""
        require_throwaway_name(name)
        body = {
            "name": name,
            "path": name,
            "visibility": visibility,
            "initialize_with_readme": True,
            "description": DESCRIPTION,
        }
        throwaway = Throwaway("gitlab", self.cfg.host, name, "", "", "main", "")
        self.ledger.register(self, throwaway)  # before the request, see GitHubLive
        try:
            data = self.api.expect("POST", "projects", [201], body)
            throwaway.ident = str(data["id"])
            self.api.scope.allow(f"projects/{throwaway.ident}")
            throwaway.owner = str(data["namespace"]["full_path"])
            throwaway.default_branch = str(data.get("default_branch", "main"))
            throwaway.web_url = str(data.get("web_url", ""))
        except BaseException as error:
            self._creating(throwaway, error)
            raise
        require_owned_by(throwaway.owner, self.cfg.login)
        return throwaway

    def _lookup(self, t: Throwaway) -> bool:
        found = self.api.request("GET", f"projects?owned=true&simple=true&search={t.name}")
        if not found.ok():
            raise LiveApiError(f"lookup answered {found.status}", found.status)
        rows = found.data if isinstance(found.data, list) else []
        for row in rows:
            if isinstance(row, dict) and row.get("path") == t.name and row.get("id"):
                t.ident = str(row["id"])
                self.api.scope.allow(f"projects/{t.ident}")
                return True
        return False

    def _project(self, t: Throwaway) -> str:
        require_throwaway_name(t.name)
        return f"projects/{t.ident}"

    def branch_sha(self, t: Throwaway, branch: str) -> str:
        data = self.api.expect(
            "GET", f"{self._project(t)}/repository/branches/{quote(branch, safe='')}", [200]
        )
        return str(data["commit"]["id"])

    def create_branch(self, t: Throwaway, name: str, from_sha: str) -> None:
        self.api.expect(
            "POST",
            f"{self._project(t)}/repository/branches",
            [201],
            {"branch": name, "ref": from_sha},
        )

    def commit_file(
        self,
        t: Throwaway,
        branch: str,
        path: str,
        content: str,
        message: str,
        *,
        replace_sha: str | None = None,
    ) -> str:
        """Create a file, or update it when ``replace_sha`` is given (any non-empty value)."""
        action = {
            "action": "update" if replace_sha else "create",
            "file_path": path,
            "content": content,
        }
        body = {"branch": branch, "commit_message": message, "actions": [action]}
        data = self.api.expect("POST", f"{self._project(t)}/repository/commits", [201], body)
        return str(data["id"])

    def open_change(
        self, t: Throwaway, head: str, base: str, title: str, body: str = "", *, draft: bool = False
    ) -> int:
        payload = {
            "source_branch": head,
            "target_branch": base,
            "title": f"Draft: {title}" if draft else title,
            "description": body,
        }
        data = self.api.expect("POST", f"{self._project(t)}/merge_requests", [201], payload)
        return int(data["iid"])

    def protect_branch(
        self,
        t: Throwaway,
        branch: str,
        *,
        required_checks: tuple[str, ...] = (),
        approvals: int = 0,
    ) -> None:
        """Protect ``branch`` (push: maintainers, merge: developers). GitLab requires checks and
        approvals as project settings, not branch rules; ``set_project`` takes those."""
        del required_checks, approvals
        project, name = self._project(t), quote(branch, safe="")
        self.api.request("DELETE", f"{project}/protected_branches/{name}")  # 404 when unprotected
        body = {"name": branch, "push_access_level": 40, "merge_access_level": 30}
        self.api.expect("POST", f"{project}/protected_branches", [201], body)

    def set_project(self, t: Throwaway, **settings: Any) -> None:
        """Project settings, e.g. ``only_allow_merge_if_pipeline_succeeds=True``."""
        self.api.expect("PUT", self._project(t), [200], settings)

    def add_ci_file(self, t: Throwaway, branch: str, name: str, pipeline: str) -> str:
        """The project's ``.gitlab-ci.yml`` (``name`` only labels the commit)."""
        return self.commit_file(t, branch, ".gitlab-ci.yml", pipeline, f"ci: {name}")

    def exists(self, t: Throwaway) -> Any:
        return self.api.request("GET", self._project(t))

    def delete_or_archive(self, t: Throwaway) -> str:
        """Delete; a project that is scheduled for deletion counts, anything else is archived."""
        try:
            self._resolve(t)
            if t.status != "live":
                return t.status
            if self.api.request("DELETE", self._project(t)).status in (202, 204):
                seen = self.exists(t)
                if seen.status == 404:
                    t.status = "deleted"
                    return t.status
                data = seen.data if isinstance(seen.data, dict) else {}
                if data.get("marked_for_deletion_on") or data.get("marked_for_deletion_at"):
                    t.status = "scheduled"
                    return t.status
        except CALL_ERRORS:
            pass
        try:
            self._resolve(t)
            self.api.expect("POST", f"{self._project(t)}/archive", [201], {})
            seen = self.exists(t)
            archived = isinstance(seen.data, dict) and seen.data.get("archived") is True
            t.status = "archived" if archived else "left"
        except CALL_ERRORS:
            t.status = "left"
        return t.status


@dataclass
class Ledger:
    """Everything this run created, in order; cleaned up in reverse, once, whatever happens."""

    entries: list[tuple[Any, Throwaway]] = field(default_factory=list)

    def register(self, host: GitHubLive | GitLabLive, throwaway: Throwaway) -> None:
        self.entries.append((host, throwaway))

    def cleanup_all(self, *, retry_left: bool = False) -> list[Throwaway]:
        """Idempotent: a resource already settled is skipped, a failing one never stops the rest.
        ``retry_left`` (the session finalizer) also tries once more what an earlier pass left."""
        retry = ("live", "left") if retry_left else ("live",)
        for host, throwaway in reversed(self.entries):
            if throwaway.status not in retry:
                continue
            throwaway.status = "live"
            try:
                host.delete_or_archive(throwaway)
            except Exception:
                throwaway.status = "left"
        return [t for _, t in self.entries]

    def declarations(self) -> list[str]:
        """Lines naming what needs a human: archived, scheduled and left resources, by name."""
        lines = []
        for _, t in self.entries:
            if t.status in ("archived", "scheduled", "left"):
                note = f"; {t.note}" if t.note else ""
                lines.append(f"{t.status}: {t.name} on {t.host} (delete it by hand{note})")
            elif t.status == "live":
                lines.append(f"left: {t.name} on {t.host} (cleanup did not run)")
        return lines
