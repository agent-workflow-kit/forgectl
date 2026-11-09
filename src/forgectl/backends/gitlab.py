"""The GitLab backend: `show` (issue #15) and the seam the later GitLab reads plug into.

Every call is `glab api` through the gateway, built from segments (`quote_component` encodes the
project path as `%2F`, `path_int` checks every id taken from an answer before it builds a path).
Everything a host sends is untrusted: strings are sanitized at this boundary, logins must fit the
GitLab grammar, ids must be integers, and any answer that is not the documented shape is an
`upstream_error`, never a traceback or a guess. Missing or unknown data never reads as ready.

Retries and the readiness retry loop belong to `reads.py`: a caller may pass `retry`, a wrapper run
around every gateway read, and `until_known`, the readiness loop run around the single-MR GET only
(every other read is made once, after it). UNVERIFIED host behavior (field names, enum values, null
versus missing, pagination headers) is marked `UNVERIFIED` where it is relied on.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, TypeVar

from .. import model as m
from ..errors import ForgectlError
from ..gateway import Gateway, Response, Target, path_int, shown_text, verify_identity
from ..hosts import Hosts, redact_secrets
from ..identifiers import PrRef, RepoRef, bind_login
from ..reads import SHOW_READINESS, ReadEnv
from ..sanitize import sanitize_line, sanitize_text

T = TypeVar("T")

#: Statuses of `detailed_merge_status` whose value is still being computed (the interface's
#: readiness retry applies to them; `mergeable` reads null and `blocked_by` says so).
READINESS_UNKNOWN = frozenset({"checking", "unchecked", "preparing"})

#: Pages read for one list (100 items each). A list cut here is incomplete, never silently short.
MAX_PAGES = 20
PER_PAGE = "100"

_STATUS_TABLE_QUIET = frozenset({"mergeable"})
#: The head pipeline's own `status` (interface: CI object). Quiet statuses add nothing (the jobs
#: are listed); failed ones add a failing `(pipeline)` check; unfinished ones a pending one. Any
#: other value, or none, is a pending `(pipeline)` check with a warning.
_PIPELINE_QUIET = frozenset({"success", "skipped", "manual"})
_PIPELINE_FAILED = frozenset({"failed", "canceled"})
_PIPELINE_UNFINISHED = frozenset(
    {
        "created",
        "waiting_for_resource",
        "preparing",
        "pending",
        "running",
        "scheduled",
        "canceling",
        "waiting_for_callback",
    }
)
#: `pipeline_id` on `/repository/commits/:sha/statuses` (docs.gitlab.com/api/commits, 17.9).
_STATUSES_PIPELINE_FILTER = (17, 9)
#: Errors no degraded read swallows: they say the call itself is unsafe or impossible.
_NEVER_DEGRADED = frozenset({"auth_required", "wrong_host_response", "tool_missing"})
#: Not project segments in an MR's `web_url` (the same set as identifiers.py:77, whose constant is
#: private to that module).
_BAD_PROJECT_SEGMENTS_IN_URL = frozenset({"-", "merge_requests"})
_SEVERITY: dict[str, int] = {"skipped": 0, "passing": 1, "pending": 2, "failing": 3}
_MAX_NUMBER = 9_999_999_999
_SHA = re.compile(r"^[0-9a-f]{40}\Z")
_RFC3339 = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)
_SEGMENT = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]{0,254}\Z")
_VERSION = re.compile(r"^([0-9]{1,6})\.([0-9]{1,6})")
_BOT_NAME = re.compile(r"^(?:project|group)_[0-9]+_bot(?:_[0-9a-f]+)?\Z")
_RETARGET = "changed target branch from"
_AUTO_RETARGET = "now targets the"
_MAX_DETAIL = 64
_MAX_ID = 2**63

Retry = Callable[[Callable[[], Response]], Response]
#: `until_known(fetch, is_unknown)` gives the answer and whether it is still unknown.
UntilKnown = Callable[[Callable[[], Response], Callable[[Response], bool]], tuple[Response, bool]]
_SUB_FAILURES = frozenset({"not_found", "permission_denied"})


def _bad(what: str) -> ForgectlError:
    """A host answer that is not the documented shape (the value is never echoed)."""
    return ForgectlError("upstream_error", f"gitlab: the answer has an unusable {what}")


class _PartMissing(ForgectlError):
    """A 404 or 403 on a part of the answer, as `upstream_error`. The marker is the class, so
    nothing private reaches the error envelope's `details`."""

    def __init__(self, cause: str) -> None:
        super().__init__(
            "upstream_error", f"gitlab: a part of the answer could not be read ({cause})"
        )


def _line(value: str) -> str:
    """One-line host text: every token in use redacted, then sanitized."""
    return sanitize_line(redact_secrets(value))


def _capped(value: str) -> str:
    """Host text for a detail or a warning: redacted, one line, then cut."""
    return _line(value)[:_MAX_DETAIL]


# -- validators for untrusted answers ----------------------------------------------------------


def _obj(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _bad(what)
    return value


def _list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise _bad(what)
    return value


def _int_id(value: Any, what: str) -> int:
    """A positive integer id (bool and floats are refused; a numeric string is not an id here)."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value < _MAX_ID:
        raise _bad(what)
    return value


def _opt_int_id(value: Any, what: str) -> int | None:
    return None if value is None else _int_id(value, what)


def _text(value: Any, what: str, *, line: bool = True, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise _bad(what)
    return _line(value) if line else sanitize_text(redact_secrets(value))


def _req_line(value: Any, what: str) -> str:
    text = _text(value, what)
    assert text is not None
    return text


def _flag(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise _bad(what)
    return value


def _opt_flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _rfc3339(value: Any) -> str | None:
    """The string when it is an RFC 3339 time with an offset, else None."""
    if not isinstance(value, str) or not _RFC3339.match(value):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _required_time(value: Any, what: str) -> str:
    found = _rfc3339(value)
    if found is None:
        raise _bad(what)
    return found


def _sha(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _SHA.match(value):
        raise _bad(what)
    return value


def _login(value: Any, what: str) -> str:
    """A GitLab username under the project's login grammar; anything else is an error (a login
    reaches later commands and hooks, so it is validated, not merely sanitized). A login that
    contains a token in use cannot be redacted without changing it, so it is an error too."""
    if not isinstance(value, str) or redact_secrets(value) != value:
        raise _bad(what)
    try:
        return bind_login(value, "gitlab")
    except ForgectlError:
        raise _bad(what) from None


def _project_path(value: Any, what: str) -> str:
    """A project path: segments of `[A-Za-z0-9._-]`, none `.`, `..` or starting with `-`."""
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise _bad(what)
    segments = value.split("/")
    for segment in segments:
        if segment in (".", "..") or not _SEGMENT.match(segment):
            raise _bad(what)
    return value


@dataclass(frozen=True)
class _User:
    id: int
    login: str
    bot: bool | None  # the answer's own `bot` field when it is a boolean


def _user(value: Any, what: str) -> _User:
    data = _obj(value, what)
    return _User(
        id=_int_id(data.get("id"), what),
        login=_login(data.get("username"), what),
        bot=_opt_flag(data.get("bot")),
    )


def _status_unknown(response: Response) -> bool:
    """Whether the MR answer's `detailed_merge_status` is still being computed."""
    data = response.data
    status = data.get("detailed_merge_status") if isinstance(data, dict) else None
    return isinstance(status, str) and status in READINESS_UNKNOWN


def _single_read(
    fetch: Callable[[], Response], is_unknown: Callable[[Response], bool]
) -> tuple[Response, bool]:
    answer = fetch()
    return answer, is_unknown(answer)


def _noop(_message: str) -> None:
    return None


def _direct(call: Callable[[], Response]) -> Response:
    return call()


def _part_missing(error: ForgectlError) -> bool:
    """Whether `_get` turned a 404 or 403 of an optional part into an `upstream_error`."""
    return isinstance(error, _PartMissing)


def _not_yet(command: str, issue: int) -> ForgectlError:
    return ForgectlError("internal", f"gitlab {command} is not implemented yet (#{issue})")


class GitLabBackend:
    """`Backend` for GitLab hosts. One instance per command: it caches the version, bot lookups
    and membership answers per process, as the interface says."""

    name: m.BackendName = "gitlab"

    def __init__(
        self,
        hosts: Hosts,
        *,
        warn: m.Warn | None = None,
        retry: Retry | None = None,
        today: Callable[[], date] | None = None,
        until_known: UntilKnown | None = None,
    ) -> None:
        self._hosts = hosts
        self._gateway: Gateway = hosts.gateway
        self._warn_to = warn or _noop
        self._retry: Retry = retry or _direct
        self._until_known: UntilKnown = until_known or _single_read
        self._today = today or (lambda: datetime.now(UTC).date())
        self._versions: dict[str, tuple[int, int] | None] = {}
        self._bots: dict[tuple[str, int], bool | None] = {}
        self._push: dict[tuple[str, int, int], bool | None] = {}

    @classmethod
    def from_env(cls, env: ReadEnv) -> GitLabBackend:
        """The adapter of one command: reads retry through ``env.call``, and the readiness loop
        (``env.until_known``) wraps the single-MR GET only, so ``get_pr`` is called once."""
        return cls(
            env.hosts,
            warn=env.warn,
            retry=env.call,
            until_known=lambda fetch, unknown: env.until_known(fetch, unknown, SHOW_READINESS),
        )

    # -- plumbing ------------------------------------------------------------------------------

    def _warn(self, message: str) -> None:
        self._warn_to(sanitize_line(redact_secrets(message)))

    def _get(
        self,
        target: Target,
        segments: list[str],
        *,
        query: tuple[tuple[str, str], ...] = (),
        shape: Any = "object",
        sub: bool = True,
    ) -> Response:
        """One read. A part of the answer being absent (404, 403) is `upstream_error` unless the
        caller reads it as optional: only the first read's 404 means the PR is not found."""
        try:
            return self._retry(
                lambda: self._gateway.api(target, "GET", segments, query=query, shape=shape)
            )
        except ForgectlError as error:
            if sub and error.code in _SUB_FAILURES:
                raise _PartMissing(error.code) from None
            raise

    def _pages(
        self, target: Target, segments: list[str], query: tuple[tuple[str, str], ...] = ()
    ) -> tuple[list[dict[str, Any]], bool]:
        """Every item of a paginated list, and whether the end was reached. A failed page fails
        the read. The next page comes from `x-next-page` (UNVERIFIED), else from a full page."""
        items: list[dict[str, Any]] = []
        page = 1
        for _ in range(MAX_PAGES):
            response = self._get(
                target,
                segments,
                query=(*query, ("per_page", PER_PAGE), ("page", str(page))),
                shape="list",
            )
            batch = _list(response.data, "list")
            for item in batch:
                items.append(_obj(item, "list item"))
            header = response.headers.get("x-next-page")
            if header is not None:
                if header == "":
                    return items, True
                if header != str(page + 1):
                    raise _bad("pagination header")
                page += 1
            elif len(batch) < int(PER_PAGE):
                return items, True
            else:
                page += 1
        return items, False

    # -- version -------------------------------------------------------------------------------

    def host_version(self, host: str) -> tuple[int, int] | None:
        """`GET /version`, once per instance: `(major, minor)`, or None when the version is
        unreadable (an unreadable version counts as below every row): the host has no such route,
        the answer is not readable, or the read fails with any code other than `auth_required`,
        `wrong_host_response` or `tool_missing`, which are raised. Every answer is cached."""
        if host in self._versions:
            return self._versions[host]
        target = self._hosts.target(host)
        found: tuple[int, int] | None = None
        try:
            response = self._get(target, ["version"], shape="any", sub=False)
        except ForgectlError as error:
            if error.code in _NEVER_DEGRADED:
                raise
        else:
            data = response.data
            version = data.get("version") if isinstance(data, dict) else None
            match = _VERSION.match(version) if isinstance(version, str) else None
            if match is not None:
                found = (int(match.group(1)), int(match.group(2)))
        self._versions[host] = found
        return found

    # -- show ----------------------------------------------------------------------------------

    def get_pr(self, ref: PrRef) -> m.PrRead:
        raw_number: Any = ref.number
        if (
            isinstance(raw_number, bool)
            or not isinstance(raw_number, int)
            or not 1 <= raw_number <= _MAX_NUMBER
        ):
            raise ForgectlError("invalid_pr", "the MR number is not usable")
        target = self._hosts.target(ref.host)
        if target.backend != "gitlab":
            raise ForgectlError("internal", "the gitlab backend was given another backend's host")
        number = int(path_int(raw_number))
        segments = ["projects", ref.project, "merge_requests", str(number)]
        # Only this GET is rechecked while the readiness is unknown; every other read follows once.
        response, _ = self._until_known(
            lambda: self._get(target, segments, sub=False), _status_unknown
        )
        mr = _obj(response.data, "merge request")
        canonical = self._canonical_path(mr.get("web_url"), target, number)
        iid = _int_id(mr.get("iid"), "iid")
        if iid != number:
            raise self._wrong_host(target, "another merge request number", mr.get("web_url"))
        project_id = _int_id(mr.get("project_id"), "project_id")
        if mr.get("target_project_id") is not None and mr["target_project_id"] != project_id:
            raise _bad("target_project_id")
        prefixed = self._prefixed(target, canonical)
        project = self._project(target, project_id, canonical, prefixed)
        state = self._state(mr.get("state"))
        status = self._detailed_status(mr, target)
        head_sha = _sha(mr.get("sha"), "sha")
        diff_refs = mr.get("diff_refs")
        base_sha = None
        if isinstance(diff_refs, dict) and diff_refs.get("base_sha") is not None:
            base_sha = _sha(diff_refs["base_sha"], "base_sha")
        author_data = _obj(mr.get("author"), "author")
        author_user = _user(author_data, "author")
        author = m.Author(
            login=author_user.login,
            id=str(author_user.id),
            is_bot=self._is_bot(target, author_user),
        )
        draft = _flag(mr.get("draft", mr.get("work_in_progress", False)), "draft")
        ci = self._ci(target, mr, head_sha, project_id)
        entries, approvals_left = self._reviewers(target, project_id, iid)
        reset = self._reset_on_push(target, project_id)
        entries = self._with_can_push(target, project_id, entries, author, reset)
        retarget_at, retarget_known = (None, True)
        if reset is True and any(e.state == "approved" for e in entries):
            retarget_at, retarget_known = self._retarget(target, project_id, iid)
        approvals = m.count_approvals(entries, author)
        at_head = m.count_approvals_at_head(
            entries,
            author,
            head_sha=head_sha,
            backend="gitlab",
            reset_approvals_on_push=reset,
            retarget_at=retarget_at,
            retarget_known=retarget_known,
        )
        has_conflicts = _flag(mr.get("has_conflicts", False), "has_conflicts")
        facts = m.BlockFacts(
            state=state,
            ci=ci,
            draft=draft or status == "draft_status",
            conflicts=has_conflicts or status == "conflict",
            behind=status == "need_rebase",
            mergeability_unknown=status in READINESS_UNKNOWN,
            locked=mr.get("state") == "locked",
            reviews_required=status == "not_approved",
            host_discussions_unresolved=status == "discussions_not_resolved",
            host_changes_requested=status == "requested_changes",
            host_checks_pending=status == "ci_still_running",
            host_ci_must_pass=status == "ci_must_pass",
            other_details=self._other_details(status),
            latest_by_reviewer=entries,
        )
        mergeable: bool | None = None
        if state == "open":
            if status in READINESS_UNKNOWN:
                mergeable = None
            else:
                mergeable = status == "mergeable" and not has_conflicts
        merged = state == "merged"
        merged_by = None
        if merged:
            who = mr.get("merge_user") or mr.get("merged_by")
            merged_by = (
                _login(_obj(who, "merge_user").get("username"), "merge_user") if who else None
            )
        enabled = not merged and (
            mr.get("merge_when_pipeline_succeeds") is True or mr.get("auto_merge_enabled") is True
        )
        by = None
        if enabled and mr.get("merge_user"):
            by = _login(_obj(mr["merge_user"], "merge_user").get("username"), "merge_user")
        head_repo, is_fork = self._head_repo(target, mr, project_id, canonical)
        host_path = f"{ref.host}/{canonical}"
        pr = m.PR(
            pr=f"{host_path}#{number}",
            repo=host_path,
            pr_id=f"gid://gitlab/MergeRequest/{_int_id(mr.get('id'), 'id')}",
            number=number,
            url=f"https://{target.host}{target.api_prefix}/{canonical}/-/merge_requests/{number}",
            backend="gitlab",
            title=_req_line(mr.get("title"), "title"),
            body=_text(mr.get("description"), "description", line=False, allow_none=True),
            author=author,
            state=state,
            draft=draft or status == "draft_status",
            created_at=_required_time(mr.get("created_at"), "created_at"),
            updated_at=_required_time(mr.get("updated_at"), "updated_at"),
            closed_at=_rfc3339(mr.get("closed_at")) if state == "closed" else None,
            merged_at=_required_time(mr.get("merged_at"), "merged_at") if merged else None,
            merged_by=merged_by,
            labels=self._labels(mr.get("labels")),
            author_association=None,
            head=m.Head(
                sha=head_sha,
                ref=_req_line(mr.get("source_branch"), "source_branch"),
                repo=None if head_repo is None else f"{ref.host}/{head_repo}",
                is_fork=is_fork,
            ),
            base=m.Base(
                ref=_req_line(mr.get("target_branch"), "target_branch"),
                sha=base_sha,
                is_default=self._is_default(mr, project),
            ),
            merged_head_sha=head_sha if merged else None,
            merge_commit_sha=self._merge_commit(mr, head_sha) if merged else None,
            head_fetch_ref=f"refs/merge-requests/{number}/head",
            mergeable=mergeable,
            blocked_by=m.compute_blocked_by(facts),
            auto_merge=m.AutoMerge(enabled=enabled, by=by),
            merge_queue=self._merge_queue(project),
            merge_methods_allowed=self._methods(project),
            additions=None,
            deletions=None,
            changed_files=self._changed_files(mr.get("changes_count")),
            review_decision=m.gitlab_review_decision(entries, approvals_left, approvals),
            approvals=approvals,
            approvals_at_head=at_head,
        )
        return m.PrRead(pr=pr, ci=ci, latest_by_reviewer=entries)

    # -- identity ------------------------------------------------------------------------------

    @staticmethod
    def _prefixed(target: Target, canonical: str) -> str:
        prefix = target.api_prefix.strip("/")
        return f"{prefix}/{canonical}" if prefix else canonical

    @staticmethod
    def _wrong_host(target: Target, what: str, observed: Any) -> ForgectlError:
        shown = observed if isinstance(observed, str) else repr(observed)
        return ForgectlError(
            "wrong_host_response",
            f"the answer is for {what}, not the requested one: the token may have reached"
            " another host and should be rotated",
            details={"observed": shown_text(redact_secrets(shown), target.token)},
        )

    def _canonical_path(self, web_url: Any, target: Target, number: int) -> str:
        """The project path from the MR's own `web_url`: the host is verified first, then the
        path must be exactly `<prefix>/<path>/-/merge_requests/<number>` (a rename on the same
        host is accepted: the path comes from this first answer)."""
        verify_identity(web_url, host=target.host, secret=target.token)
        assert isinstance(web_url, str)
        # urlsplit drops tab, CR and LF silently, so the raw text is checked first.
        if not web_url.isascii() or any(c.isspace() or not c.isprintable() for c in web_url):
            raise _bad("web_url")
        parts = urllib.parse.urlsplit(web_url)
        path = parts.path
        if (
            parts.scheme != "https"
            or parts.query
            or parts.fragment
            or "?" in web_url
            or "#" in web_url
            or ";" in path
            or "%" in path
            or not path.startswith("/")
        ):
            raise _bad("web_url")
        path = path[1:]
        prefix = target.api_prefix.strip("/")
        if prefix:
            if not path.startswith(prefix + "/"):
                raise self._wrong_host(target, "a path outside the host's prefix", web_url)
            path = path[len(prefix) + 1 :]
        match = re.fullmatch(r"(.+?)(?:/-)?/merge_requests/([1-9][0-9]{0,9})", path)
        if match is None:
            raise _bad("web_url")
        if int(match.group(2)) != number:
            raise self._wrong_host(target, "another merge request number", web_url)
        project = match.group(1)
        if _BAD_PROJECT_SEGMENTS_IN_URL.intersection(project.split("/")):
            raise _bad("web_url")
        return _project_path(project, "web_url")

    @staticmethod
    def _https(web_url: Any, what: str) -> None:
        """A project's own `web_url` must be `https`, as the MR's is."""
        if not isinstance(web_url, str) or not web_url.startswith("https://"):
            raise _bad(what)

    def _project(
        self, target: Target, project_id: int, canonical: str, prefixed: str
    ) -> dict[str, Any]:
        data = _obj(self._get(target, ["projects", path_int(project_id)]).data, "project")
        verify_identity(
            data.get("web_url"), host=target.host, project=prefixed, secret=target.token
        )
        self._https(data.get("web_url"), "project web_url")
        if data.get("id") != project_id or data.get("path_with_namespace") != canonical:
            raise self._wrong_host(target, "another project", data.get("web_url"))
        return data

    @staticmethod
    def _merge_queue(project: Mapping[str, Any]) -> bool | None:
        """`merge_trains_enabled`: absent is false, a boolean is itself, anything else null."""
        if "merge_trains_enabled" not in project:
            return False
        value = project["merge_trains_enabled"]
        return value if isinstance(value, bool) else None

    @staticmethod
    def _is_default(mr: Mapping[str, Any], project: Mapping[str, Any]) -> bool | None:
        default = project.get("default_branch")
        target = mr.get("target_branch")
        if not isinstance(default, str) or not isinstance(target, str):
            return None
        return default == target

    def _head_repo(
        self, target: Target, mr: Mapping[str, Any], project_id: int, canonical: str
    ) -> tuple[str | None, bool | None]:
        """The head's project path and `is_fork`. A null `source_project_id` (the project was
        deleted) is null for both; a differing id whose project cannot be read (deleted or
        hidden) is `repo: null` with `is_fork: true`, since the differing id proves a fork."""
        source = _opt_int_id(mr.get("source_project_id"), "source_project_id")
        if source is None:
            return None, None
        if source == project_id:
            return canonical, False
        try:
            data = _obj(self._get(target, ["projects", path_int(source)]).data, "source project")
        except ForgectlError as error:
            if not _part_missing(error):
                raise
            self._warn("the head project could not be read; its repo is reported null")
            return None, True
        path = _project_path(data.get("path_with_namespace"), "source project")
        verify_identity(
            data.get("web_url"),
            host=target.host,
            project=self._prefixed(target, path),
            secret=target.token,
        )
        self._https(data.get("web_url"), "source project web_url")
        if data.get("id") != source:
            raise self._wrong_host(target, "another project", data.get("web_url"))
        return path, True

    # -- scalars -------------------------------------------------------------------------------

    @staticmethod
    def _state(raw: Any) -> m.PrState:
        table: dict[str, m.PrState] = {
            "opened": "open",
            "locked": "open",
            "closed": "closed",
            "merged": "merged",
        }
        if not isinstance(raw, str) or raw not in table:
            raise _bad("state")
        return table[raw]

    def _detailed_status(self, mr: Mapping[str, Any], target: Target) -> str:
        raw = mr.get("detailed_merge_status")
        if raw is None or "detailed_merge_status" not in mr:
            version = self.host_version(target.host)
            shown = f"{version[0]}.{version[1]}" if version else "unreadable"
            raise ForgectlError(
                "unsupported_host_version",
                f"{target.host} does not report detailed_merge_status (GitLab 15.6 or later is"
                f" needed; version {shown})",
            )
        if not isinstance(raw, str) or not raw:
            raise _bad("detailed_merge_status")
        return raw

    @staticmethod
    def _other_details(status: str) -> tuple[str, ...]:
        known = (
            _STATUS_TABLE_QUIET
            | READINESS_UNKNOWN
            | {
                "draft_status",
                "conflict",
                "need_rebase",
                "not_approved",
                "requested_changes",
                "discussions_not_resolved",
                "ci_still_running",
                "ci_must_pass",
            }
        )
        if status in known:
            return ()
        # `not_open` on an open (or locked) MR says something the state does not: it blocks.
        detail = _capped(status)
        return (detail or "unknown",)

    @staticmethod
    def _labels(raw: Any) -> tuple[str, ...]:
        found = _list(raw, "labels")
        return tuple(sorted({_req_line(label, "label") for label in found}))

    @staticmethod
    def _merge_commit(mr: Mapping[str, Any], head_sha: str) -> str:
        for key in ("merge_commit_sha", "squash_commit_sha"):
            value = mr.get(key)
            if value is not None:
                return _sha(value, key)
        return head_sha

    @staticmethod
    def _changed_files(raw: Any) -> int | None:
        """`changes_count` is a string; a form like `1000+` (or anything else) is unknown."""
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw if 0 <= raw < _MAX_ID else None
        if isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) <= 9:
            return int(raw)
        return None

    @staticmethod
    def _methods(project: Mapping[str, Any]) -> tuple[str, ...] | None:
        """From the Merge method table: `squash` unless `squash_option` is `never`, `merge` for
        `merge`/`rebase_merge` unless it is `always`, `rebase` for `ff` unless it is `always`.
        Null when either project setting is not a known value."""
        method = project.get("merge_method")
        squash = project.get("squash_option")
        if method not in ("merge", "rebase_merge", "ff") or squash not in (
            "never",
            "default_off",
            "default_on",
            "always",
        ):
            return None
        allowed: list[str] = []
        if squash != "never":
            allowed.append("squash")
        if squash != "always":
            allowed.append("rebase" if method == "ff" else "merge")
        return tuple(allowed)

    # -- bots ----------------------------------------------------------------------------------

    def _is_bot(self, target: Target, user: _User) -> bool | None:
        """The answer's own `bot` field, else `/users/:id` (cached per process), else the name
        patterns; null when these settle nothing (UNVERIFIED: `bot` is documented from 19.5)."""
        if user.bot is not None:
            return user.bot
        key = (target.host, user.id)
        if key not in self._bots:
            self._bots[key] = self._lookup_bot(target, user)
        return self._bots[key]

    def _lookup_bot(self, target: Target, user: _User) -> bool | None:
        found: bool | None = None
        try:
            data = self._get(target, ["users", path_int(user.id)]).data
        except ForgectlError as error:
            if not _part_missing(error):
                raise
            data = None
        if isinstance(data, dict) and data.get("id") == user.id:
            found = _opt_flag(data.get("bot"))
        if found is None and _BOT_NAME.match(user.login):
            return True
        return found

    # -- reviews and approvals -----------------------------------------------------------------

    def _reviewers(
        self, target: Target, project_id: int, iid: int
    ) -> tuple[tuple[m.LatestByReviewer, ...], int | None]:
        base = ["projects", path_int(project_id), "merge_requests", path_int(iid)]
        approvals = _obj(self._get(target, [*base, "approvals"]).data, "approvals")
        left = approvals.get("approvals_left")
        approvals_left = left if isinstance(left, int) and not isinstance(left, bool) else None
        if approvals_left is not None and approvals_left < 0:
            approvals_left = None
        approved: dict[int, tuple[_User, str | None]] = {}
        for item in _list(approvals.get("approved_by"), "approved_by"):
            entry = _obj(item, "approved_by")
            user = _user(entry.get("user"), "approver")
            approved[user.id] = (user, _rfc3339(entry.get("approved_at")))
        reviewer_states: dict[int, tuple[_User, str]] = {}
        # Required: without the reviewer states a `requested_changes` could read as approved.
        listed = self._get(target, [*base, "reviewers"], shape="list").data
        for item in _list(listed, "reviewers"):
            entry = _obj(item, "reviewer")
            user = _user(entry.get("user"), "reviewer")
            raw = entry.get("state")
            reviewer_states[user.id] = (user, raw if isinstance(raw, str) else "")
        entries: list[m.LatestByReviewer] = []
        for account in sorted(set(approved) | set(reviewer_states)):
            reviewer = reviewer_states.get(account)
            user = approved[account][0] if account in approved else reviewer_states[account][0]
            rstate = reviewer[1] if reviewer else ""
            if rstate == "requested_changes":
                entries.append(self._entry(user, "changes_requested", None))
            elif account in approved:
                entries.append(self._entry(user, "approved", approved[account][1]))
            elif rstate == "unapproved":
                entries.append(self._entry(user, "dismissed", None))
        return tuple(entries), approvals_left

    @staticmethod
    def _entry(user: _User, state: m.LatestState, at: str | None) -> m.LatestByReviewer:
        return m.LatestByReviewer(
            login=user.login, id=str(user.id), state=state, commit_sha=None, at=at, can_push=None
        )

    def _reset_on_push(self, target: Target, project_id: int) -> bool | None:
        """The project's `reset_approvals_on_push` (UNVERIFIED route and field); unknown when it
        cannot be read or is not a boolean, which keeps `approvals_at_head` null."""
        try:
            data = self._get(target, ["projects", path_int(project_id), "approvals"]).data
        except ForgectlError as error:
            if not _part_missing(error):
                raise
            self._warn("reset_approvals_on_push could not be read; approvals_at_head is null")
            return None
        return _opt_flag(data.get("reset_approvals_on_push")) if isinstance(data, dict) else None

    def _with_can_push(
        self,
        target: Target,
        project_id: int,
        entries: tuple[m.LatestByReviewer, ...],
        author: m.Author,
        reset: bool | None,
    ) -> tuple[m.LatestByReviewer, ...]:
        """`can_push` only for an `approved` entry on a project that resets approvals on push."""
        if reset is not True:
            return entries
        out: list[m.LatestByReviewer] = []
        for e in entries:
            if e.state == "approved" and e.id is not None and e.id != author.id:
                out.append(
                    m.LatestByReviewer(
                        login=e.login,
                        id=e.id,
                        state=e.state,
                        commit_sha=e.commit_sha,
                        at=e.at,
                        can_push=self._can_push(target, project_id, int(e.id)),
                    )
                )
            else:
                out.append(e)
        return tuple(out)

    def _can_push(self, target: Target, project_id: int, user_id: int) -> bool | None:
        """The `access` rule: level 30 or more, `state: active` and an unexpired `expires_at`
        (`members/all`); a 404 is false; a 401 or 403 is null. Cached per process."""
        key = (target.host, project_id, user_id)
        if key in self._push:
            return self._push[key]
        segments = ["projects", path_int(project_id), "members", "all", path_int(user_id)]
        result: bool | None
        try:
            data = self._retry(
                lambda: self._gateway.api(target, "GET", segments, shape="object")
            ).data
        except ForgectlError as error:
            if error.code == "not_found":
                result = False
            elif error.code in ("auth_required", "permission_denied"):
                result = None
            else:
                raise
        else:
            member = _obj(data, "member")
            member_id = member.get("id")
            if (
                isinstance(member_id, bool)
                or not isinstance(member_id, int)
                or member_id != user_id
            ):
                raise _bad("member id")
            result = self._member_can_push(member)
        self._push[key] = result
        return result

    def _member_can_push(self, data: Mapping[str, Any]) -> bool:
        level = data.get("access_level")
        if isinstance(level, bool) or not isinstance(level, int) or level < 30:
            return False
        if data.get("state") != "active":
            return False
        if "membership_state" in data and data["membership_state"] != "active":
            return False  # e.g. `awaiting` a seat: not yet a member who can push
        expires = data.get("expires_at")
        if expires is None:
            return True
        if not isinstance(expires, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", expires):
            return False
        try:
            return date.fromisoformat(expires) > self._today()
        except ValueError:
            return False

    def _retarget(self, target: Target, project_id: int, iid: int) -> tuple[str | None, bool]:
        """The newest system note beginning `changed target branch from`, or holding `now targets
        the` (GitLab's automatic retarget after the target branch is merged and deleted), over
        every page (the order is not trusted). A walk that cannot complete is `(None, False)`:
        no approval counts."""
        segments = ["projects", path_int(project_id), "merge_requests", path_int(iid), "notes"]
        try:
            notes, complete = self._pages(
                target, segments, (("order_by", "created_at"), ("sort", "desc"))
            )
        except ForgectlError as error:
            if error.code in _NEVER_DEGRADED:
                raise
            self._warn("the retarget walk failed; no approval counts at the head")
            return None, False
        if not complete:
            self._warn("the retarget walk hit the page cap; no approval counts at the head")
            return None, False
        newest: datetime | None = None
        newest_text: str | None = None
        for note in notes:
            body = note.get("body")
            if note.get("system") is not True or not isinstance(body, str):
                continue
            if not body.startswith(_RETARGET) and _AUTO_RETARGET not in body:
                continue
            at = _rfc3339(note.get("created_at"))
            if at is None:
                self._warn("a retarget note has no usable time; no approval counts at the head")
                return None, False
            parsed = datetime.fromisoformat(at)
            if newest is None or parsed > newest:
                newest, newest_text = parsed, at
        return newest_text, True

    # -- CI ------------------------------------------------------------------------------------

    def _ci(self, target: Target, mr: Mapping[str, Any], head_sha: str, project_id: int) -> m.CI:
        """Checks from `head_pipeline`'s statuses (a fork's pipeline from its own project), its
        own status, and its child pipelines (one check per bridge)."""
        pipeline = mr.get("head_pipeline")
        if pipeline is None:
            return m.aggregate_ci((), pr_head_sha=head_sha, checks_sha=None)
        data = _obj(pipeline, "head_pipeline")
        pipeline_id = _int_id(data.get("id"), "pipeline id")
        pipeline_sha = _sha(data.get("sha"), "pipeline sha")
        pipeline_project = _int_id(data.get("project_id"), "pipeline project")
        source = mr.get("source_project_id")
        allowed = {project_id}
        if isinstance(source, int) and not isinstance(source, bool):
            allowed.add(source)
        if pipeline_project not in allowed:
            raise _bad("pipeline project")
        fork = pipeline_project != project_id
        try:
            status_rows, complete = self._pages(
                target,
                [
                    "projects",
                    path_int(pipeline_project),
                    "repository",
                    "commits",
                    pipeline_sha,
                    "statuses",
                ],
                (("pipeline_id", path_int(pipeline_id)),),
            )
        except ForgectlError as error:
            # A fork's pipeline the token cannot see is unknown, never a failed `show`.
            if not (fork and _part_missing(error)):
                raise
            self._warn("the fork's pipeline could not be read; CI is incomplete")
            status_rows, complete = [], False
        checks = self._checks(status_rows)
        version = self.host_version(target.host)
        if version is None or version < _STATUSES_PIPELINE_FILTER:
            # Older hosts ignore `pipeline_id` and list every pipeline's statuses on the sha.
            self._warn(
                "the host is older than GitLab 17.9 or its version is unreadable, so its"
                " statuses may mix pipelines; CI is incomplete"
            )
            complete = False
        own = self._pipeline_check(data)
        if own is not None:
            checks.append(own)
        children, children_complete = self._children(target, pipeline_project, pipeline_id)
        checks.extend(children)
        # TODO(#31): a merged-results or merge-train pipeline runs on the merge ref, so its sha
        # differs from the head and `for_current_head` is false (pending) until a spec decision.
        return m.aggregate_ci(
            checks,
            pr_head_sha=head_sha,
            checks_sha=pipeline_sha,
            complete=complete and children_complete,
        )

    def _pipeline_check(self, data: Mapping[str, Any]) -> m.Check | None:
        """The head pipeline's own status as a `(pipeline)` check, per its table."""
        raw = data.get("status")
        if isinstance(raw, str) and raw in _PIPELINE_QUIET:
            return None
        state: m.CheckState
        if isinstance(raw, str) and raw in _PIPELINE_FAILED:
            state = "failing"
        else:
            state = "pending"
            if not (isinstance(raw, str) and raw in _PIPELINE_UNFINISHED):
                shown = _capped(raw) if isinstance(raw, str) else type(raw).__name__
                self._warn(f"unmapped gitlab pipeline status {shown!r}; reading it as pending")
        return m.Check(
            name="(pipeline)",
            state=state,
            required=None,
            counted=True,
            url=self._check_url(data.get("web_url")),
            finished_at=None,
        )

    def _children(
        self, target: Target, project: int, pipeline_id: int
    ) -> tuple[list[m.Check], bool]:
        """One `(child) <bridge name>` check per bridge of the head pipeline, from its downstream
        pipeline's status. A list that cannot be read or is cut is incomplete; a malformed row is
        `upstream_error`, like a malformed commit-status row."""
        segments = ["projects", path_int(project), "pipelines", path_int(pipeline_id), "bridges"]
        try:
            rows, done = self._pages(target, segments)
        except ForgectlError as error:
            if error.code in _NEVER_DEGRADED:
                raise
            self._warn("the child pipelines could not be read; CI is incomplete")
            return [], False
        checks = [self._bridge_check(row) for row in rows]
        if not done:
            self._warn("the child pipelines hit the page cap; CI is incomplete")
        return checks, done

    def _bridge_check(self, row: Mapping[str, Any]) -> m.Check:
        name = row.get("name")
        if not isinstance(name, str):
            raise _bad("bridge name")
        allow = _opt_flag(row.get("allow_failure"))
        downstream = row.get("downstream_pipeline")
        state: m.CheckState = "pending"
        url = None
        if downstream is not None:
            child = _obj(downstream, "downstream pipeline")
            state = self._child_state(child.get("status"))
            url = self._check_url(child.get("web_url"))
        return m.Check(
            name=f"(child) {_line(name)}",
            state=state,
            required=m.gitlab_required(allow),
            counted=m.is_counted(state, allow_failure=allow),
            url=url,
            finished_at=_rfc3339(row.get("finished_at")),
        )

    def _child_state(self, raw: Any) -> m.CheckState:
        """A child pipeline's status through the pipeline table: `success` passes, `skipped` is
        skipped, failed ones fail; `manual` is pending (its jobs are not listed here), as is
        every unfinished or unknown value (unknown with a warning)."""
        if raw == "success":
            return "passing"
        if raw == "skipped":
            return "skipped"
        if isinstance(raw, str) and raw in _PIPELINE_FAILED:
            return "failing"
        if not (isinstance(raw, str) and (raw in _PIPELINE_UNFINISHED or raw == "manual")):
            shown = _capped(raw) if isinstance(raw, str) else type(raw).__name__
            self._warn(f"unmapped gitlab child pipeline status {shown!r}; reading it as pending")
        return "pending"

    def _checks(self, rows: list[dict[str, Any]]) -> list[m.Check]:
        """The latest status per name: the highest id, and on equal ids the worse state."""
        latest: dict[str, tuple[int, int, m.Check]] = {}
        for row in rows:
            name = row.get("name")
            if not isinstance(name, str):
                raise _bad("status name")
            identifier = _int_id(row.get("id"), "status id")
            raw = row.get("status")
            if not isinstance(raw, str):
                raise _bad("status")
            # A long value is never a known state; cut it so a warning stays bounded.
            shown = raw if len(raw) <= _MAX_DETAIL else _capped(raw)
            allow = _opt_flag(row.get("allow_failure"))
            state = m.normalize_check_state("gitlab", shown, allow_failure=allow, warn=self._warn)
            check = m.Check(
                name=_line(name) or "(unnamed)",
                state=state,
                required=m.gitlab_required(allow),
                counted=m.is_counted(state, allow_failure=allow),
                url=self._check_url(row.get("target_url")),
                finished_at=_rfc3339(row.get("finished_at")),
            )
            rank = (identifier, _SEVERITY[state])
            if name not in latest or rank > latest[name][:2]:
                latest[name] = (*rank, check)
        return [check for _, _, check in latest.values()]

    @staticmethod
    def _check_url(raw: Any) -> str | None:
        url = m.clean_check_url(raw if isinstance(raw, str) else None)
        if url is None:
            return None
        return m.clean_check_url(_line(url))

    # -- the rest of the protocol (other issues) ---------------------------------------------------

    def list_prs(self, query: m.PrQuery) -> m.PrPage:
        raise _not_yet("prs", 19)  # TODO(#19): GitLab prs

    def snapshot(self, ref: PrRef) -> m.Snapshot:
        from . import gitlab_snapshot  # (#16) lazy: that module imports this one

        return gitlab_snapshot.snapshot(self, ref)

    def diff(self, ref: PrRef) -> m.DiffResult:
        raise _not_yet("diff", 18)  # TODO(#18): GitLab diff

    def compare(self, ref: PrRef, from_sha: str, to_sha: str) -> m.CompareResult:
        raise _not_yet("compare", 18)  # TODO(#18): GitLab compare

    def files(self, ref: PrRef) -> m.FilesResult:
        raise _not_yet("files", 18)  # TODO(#18): GitLab files

    def comments(self, ref: PrRef) -> m.CommentsResult:
        from . import gitlab_snapshot  # (#16)

        return gitlab_snapshot.comments(self, ref)

    def reviews(self, ref: PrRef) -> m.ReviewsResult:
        from . import gitlab_snapshot  # (#16)

        return gitlab_snapshot.reviews(self, ref)

    def ci(self, ref: PrRef) -> m.CiResult:
        raise _not_yet("ci", 17)  # TODO(#17): GitLab ci; `_ci` above is the shared read

    def access(self, repo: RepoRef, user: str) -> m.AccessResult:
        raise _not_yet("access", 19)  # TODO(#19): GitLab access; `_member_can_push` is the rule

    def whoami(self, host: str) -> m.Identity:
        raise _not_yet("whoami", 19)  # TODO(#19): GitLab whoami


#: The one-line registration seam for the backend factory table.
BACKEND = GitLabBackend

__all__ = ["BACKEND", "MAX_PAGES", "READINESS_UNKNOWN", "GitLabBackend", "UntilKnown"]
