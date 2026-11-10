"""GitLab bulk snapshots (#116): many MRs of one host from a few GraphQL requests.

Where the MRs come from. GraphQL prices a list connection by its node once, whatever the page
holds (about 160 points for this query, plus 2 per MR), but a single ``mergeRequest(iid:)`` at the
full price (about 105 points each against a limit of 250). The MRs of a browse load are the ones
the user authored or was asked to review, so they are read from ``currentUser``'s
``authoredMergeRequests`` and ``reviewRequestedMergeRequests`` in pages of ``PAGE_SIZE``, stopping
once every wanted MR has been seen. An MR in neither list (closed, merged, or another person's)
is read by aliased ``project(fullPath:) { mergeRequest(iid:) }`` lookups, ``LOOKUP_PAGE`` to a
request. The answer is turned into the objects ``snapshot`` builds by the same code: the REST
shaped values the per-PR path reads (an MR, its notes, its statuses) are rebuilt from the GraphQL
answer and handed to the helpers of ``GitLabBackend`` and ``gitlab_snapshot``. What GraphQL does
not carry is read the way ``snapshot`` reads it, once per project and cached for the command: the
project object, ``reset_approvals_on_push`` and, for an approver, ``can_push``.

Capability probe. Before the first page of a host (once per process) one cheap query selects every
field this reader needs; a host that rejects it (an older GitLab) is read by the per-PR path.

Known differences from ``snapshot`` (interface: ``snapshots``): times have whole-second precision;
``additions`` and ``deletions`` are real numbers; a status posted from outside GitLab CI is not
in ``ci.checks`` (GraphQL does not list commit statuses); an approver who was not a requested
reviewer has no approval time; ``threads[].commit_sha`` and ``start_line`` are null (the position
of a note carries neither on GitLab 19.4).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from .. import model as m
from ..activity import for_hosts as derive_activity
from ..errors import ForgectlError
from ..gateway import Target
from ..identifiers import PrRef
from ..reads import ReadEnv
from . import gitlab as gl
from . import gitlab_snapshot as gls
from .bulk_common import (
    BULK_UNAVAILABLE,
    HOST_STOPS,
    BulkRead,
    Unresolved,
    give_up_stop,
    run_pages,
)
from .gitlab import GitLabBackend

#: MRs of a list asked in one request: about 160 points plus 2 per MR, against a limit of 250.
PAGE_SIZE = 30
#: Pages read of one list before the MRs still missing are looked up one by one.
MAX_LIST_PAGES = 10
#: MRs looked up by ``mergeRequest(iid:)`` in one request: about 105 points each.
LOOKUP_PAGE = 2
#: How often a list page that failed is asked again with half the MRs.
MAX_HALVINGS = 3

DISCUSSIONS = 100
DISCUSSION_NOTES = 20
JOBS = 100
REVIEWERS = 30
APPROVERS = 100
LABELS = 100

_GID = re.compile(r"gid://gitlab/[A-Za-z:]+/([0-9]{1,18})\Z")

_USER = "id username bot"

_MR_FIELDS = f"""
fragment BulkMr on MergeRequest {{
  id
  iid
  webUrl
  title
  description
  state
  draft
  createdAt
  updatedAt
  closedAt
  mergedAt
  mergeCommitSha
  project {{ id fullPath }}
  sourceProjectId
  targetProjectId
  author {{ {_USER} }}
  mergeUser {{ {_USER} }}
  mergeWhenPipelineSucceeds
  autoMergeEnabled
  sourceBranch
  targetBranch
  diffHeadSha
  diffRefs {{ baseSha }}
  detailedMergeStatus
  conflicts
  approvalsLeft
  approvedBy(first: {APPROVERS}) {{ pageInfo {{ hasNextPage }} nodes {{ {_USER} }} }}
  reviewers(first: {REVIEWERS}) {{
    pageInfo {{ hasNextPage }}
    nodes {{ {_USER} mergeRequestInteraction {{ approved approvedAt reviewState }} }}
  }}
  labels(first: {LABELS}) {{ pageInfo {{ hasNextPage }} nodes {{ title }} }}
  diffStatsSummary {{ additions deletions fileCount }}
  mergeRequestDiffs(last: 1) {{ nodes {{ createdAt }} }}
  commits(last: 1) {{ nodes {{ sha committedDate }} }}
  headPipeline {{
    id
    sha
    status
    path
    jobs(first: {JOBS}, retried: false) {{
      pageInfo {{ hasNextPage }}
      nodes {{
        id
        name
        kind
        status
        allowFailure
        finishedAt
        detailedStatus {{ detailsPath }}
        downstreamPipeline {{ status path }}
      }}
    }}
  }}
  discussions(first: {DISCUSSIONS}) {{
    pageInfo {{ hasNextPage }}
    nodes {{
      id
      resolvable
      resolved
      notes(first: {DISCUSSION_NOTES}) {{
        pageInfo {{ hasNextPage }}
        nodes {{
          id
          body
          system
          createdAt
          updatedAt
          resolvable
          resolved
          author {{ {_USER} }}
          position {{ newPath oldPath newLine oldLine positionType }}
        }}
      }}
    }}
  }}
}}
"""


def bulk_query(count: int) -> str:
    """The request for ``count`` MRs: ``$p0``/``$i0``... carry the project path and the iid."""
    declared = ", ".join(f"$p{i}: ID!, $i{i}: String!" for i in range(count))
    fields = "\n".join(
        f"  m{i}: project(fullPath: $p{i}) {{ mergeRequest(iid: $i{i}) {{ ...BulkMr }} }}"
        for i in range(count)
    )
    return (
        f"query ForgectlBulk({declared}) {{\n  queryComplexity {{ score limit }}\n{fields}\n}}\n"
        + _MR_FIELDS
    )


#: One cheap query that names every field and argument the reader uses.
PROBE_QUERY = (
    """
query ForgectlProbe {
  queryComplexity { score limit }
  currentUser {
    authoredMergeRequests(first: 1, state: opened) { nodes { ...BulkMr } }
    reviewRequestedMergeRequests(first: 1, state: opened) { nodes { iid } }
  }
}
"""
    + _MR_FIELDS
)

LIST_FIELDS = ("authoredMergeRequests", "reviewRequestedMergeRequests")


def list_query(field: str) -> str:
    """One page of ``currentUser``'s ``field`` list (``$first``, ``$after`` are variables)."""
    if field not in LIST_FIELDS:
        raise ValueError(field)
    return (
        "query ForgectlList($first: Int!, $after: String) {\n"
        "  queryComplexity { score limit }\n"
        f"  currentUser {{ {field}(state: opened, first: $first, after: $after) {{\n"
        "    pageInfo { hasNextPage endCursor }\n    nodes { ...BulkMr }\n  } }\n}\n" + _MR_FIELDS
    )


_PROBES: dict[str, str | None] = {}
_PROBE_LOCK = threading.Lock()


def reset_probes() -> None:
    """Forget what hosts answered to the probe (tests)."""
    with _PROBE_LOCK:
        _PROBES.clear()


def probe(env: ReadEnv, host: str) -> str | None:
    """Why ``host`` cannot be read in bulk, or None when it can. Asked once per process and
    host; only a definite answer (the host rejects the query) is remembered. Any other failure
    (the network, a credential) is raised: it is not a statement about the host's version."""
    with _PROBE_LOCK:
        if host in _PROBES:
            return _PROBES[host]
    target = env.hosts.target(host)
    try:
        result = env.call(lambda: env.gateway.graphql(target, PROBE_QUERY))
    except ForgectlError as error:
        if error.code != "host_rejected":
            raise
        reason = " ".join(error.message.split())[:300]
        with _PROBE_LOCK:
            _PROBES[host] = reason
        return reason
    score = _opt_obj(result.data.get("queryComplexity"))
    limit = score.get("limit") if score else None
    reason = (
        None
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0
        else "the host does not report queryComplexity"
    )
    with _PROBE_LOCK:
        _PROBES[host] = reason
    return reason


# -- small readers ------------------------------------------------------------------------------


def _opt_obj(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _obj(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Unresolved(BULK_UNAVAILABLE, f"unusable answer: {what}")
    return value


def _list(value: object, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise Unresolved(BULK_UNAVAILABLE, f"unusable answer: {what}")
    return value


def _gid(value: object, what: str) -> int:
    match = _GID.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise Unresolved(BULK_UNAVAILABLE, f"unusable answer: {what}")
    return int(match.group(1))


def _nodes(connection: object, what: str) -> list[Any]:
    """The nodes of a connection read to its end; a connection with another page is a cut part."""
    conn = _obj(connection, what)
    info = _obj(conn.get("pageInfo"), what)
    more = info.get("hasNextPage")
    if not isinstance(more, bool):
        raise Unresolved(BULK_UNAVAILABLE, f"unusable answer: {what} page")
    if more:
        raise Unresolved(BULK_UNAVAILABLE, f"more than the page holds of: {what}")
    return _list(conn.get("nodes"), what)


def _user(raw: object, what: str) -> dict[str, Any]:
    """A GraphQL user as the REST user object the per-PR code reads."""
    node = _obj(raw, what)
    bot = node.get("bot")
    return {
        "id": _gid(node.get("id"), what),
        "username": node.get("username"),
        "bot": bot if isinstance(bot, bool) else None,
    }


def _lower(value: object) -> str | None:
    return value.lower() if isinstance(value, str) else None


# -- the run ------------------------------------------------------------------------------------


class _Run:
    def __init__(self, env: ReadEnv, host: str, now: Callable[[], datetime]) -> None:
        self.env = env
        self.host = host
        self.now = now
        self.target: Target = env.hosts.target(host)
        self.backend = GitLabBackend.from_env(env)
        self._projects: dict[tuple[int, str], dict[str, Any]] = {}
        self._reset: dict[int, bool | None] = {}
        self._heads: dict[tuple[int, int], tuple[str | None, bool | None]] = {}
        self.readings = 0

    # -- one request ---------------------------------------------------------------------------

    def _query(self, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        if self.env.gateway.budget.remaining() <= 0:
            raise ForgectlError("network_error", "the time budget ran out before the call")
        return dict(
            self.env.call(lambda: self.env.gateway.graphql(self.target, query, variables)).data
        )

    def ask(self, page: list[PrRef], result: BulkRead) -> None:
        """One request of ``LOOKUP_PAGE`` MRs, each by its project and iid."""
        variables: dict[str, Any] = {}
        for i, ref in enumerate(page):
            variables[f"p{i}"] = ref.project
            variables[f"i{i}"] = str(ref.number)
        data = self._query(bulk_query(len(page)), variables)
        built: list[tuple[PrRef, m.Snapshot | Unresolved]] = []
        for i, ref in enumerate(page):
            container = _opt_obj(data.get(f"m{i}"))
            node = _opt_obj(container.get("mergeRequest")) if container is not None else None
            built.append((ref, self._accept(ref, node)))
        self._store(built, result)

    def read_lists(
        self, wanted: dict[tuple[str, int], PrRef], result: BulkRead, size: int = PAGE_SIZE
    ) -> None:
        """Walk ``currentUser``'s two lists until every wanted MR has been seen; the MRs found are
        built into ``result``. A list page the host refuses (complexity) is asked again with half
        the MRs, at most ``MAX_HALVINGS`` times in all; a list that still fails ends there, and
        what it did not show is looked up."""
        halvings = 0
        for field in LIST_FIELDS:
            after: str | None = None
            seen: set[str] = set()
            for _ in range(MAX_LIST_PAGES * (MAX_HALVINGS + 1)):
                if not wanted:
                    return
                try:
                    data = self._query(list_query(field), {"first": size, "after": after})
                except ForgectlError as error:
                    if error.code in HOST_STOPS:
                        raise
                    if halvings >= MAX_HALVINGS or size <= 1:
                        break
                    halvings += 1
                    size = max(1, size // 2)
                    continue
                user = _opt_obj(data.get("currentUser"))
                conn = _opt_obj(user.get(field)) if user is not None else None
                if conn is None or not isinstance(conn.get("nodes"), list):
                    break
                built: list[tuple[PrRef, m.Snapshot | Unresolved]] = []
                for node in conn["nodes"]:
                    ref = _wanted_ref(wanted, node)
                    if ref is not None:
                        del wanted[(_key(node))]  # type: ignore[arg-type]
                        built.append((ref, self._accept(ref, node)))
                self._store(built, result)
                info = _opt_obj(conn.get("pageInfo")) or {}
                cursor = info.get("endCursor")
                if info.get("hasNextPage") is not True or not isinstance(cursor, str):
                    break
                if cursor in seen:
                    break
                seen.add(cursor)
                after = cursor

    def _accept(self, ref: PrRef, node: dict[str, Any] | None) -> m.Snapshot | Unresolved:
        """The snapshot of ``node`` for ``ref``, or why not."""
        try:
            if node is None:
                raise Unresolved("not_found", "no such merge request, or no access to it")
            return self._item(ref, node)
        except Unresolved as why:
            return why
        except ForgectlError as error:
            if error.code == "wrong_host_response":
                raise
            code = BULK_UNAVAILABLE if error.code == "upstream_error" else error.code
            return Unresolved(code, error.message)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return Unresolved(BULK_UNAVAILABLE, "unusable answer")

    @staticmethod
    def _store(built: list[tuple[PrRef, m.Snapshot | Unresolved]], result: BulkRead) -> None:
        for ref, outcome in built:
            if isinstance(outcome, Unresolved):
                result.unresolved[ref] = outcome
            else:
                result.snapshots[ref] = outcome

    # -- facts read once per project -------------------------------------------------------------

    def project(self, project_id: int, canonical: str) -> dict[str, Any]:
        key = (project_id, canonical)
        if key not in self._projects:
            prefixed = self.backend._prefixed(self.target, canonical)
            self._projects[key] = self.backend._project(
                self.target, project_id, canonical, prefixed
            )
        return self._projects[key]

    def reset_on_push(self, project_id: int) -> bool | None:
        if project_id not in self._reset:
            self._reset[project_id] = self.backend._reset_on_push(self.target, project_id)
        return self._reset[project_id]

    def head_repo(
        self, mr: Mapping[str, Any], project_id: int, canonical: str
    ) -> tuple[str | None, bool | None]:
        source = mr.get("source_project_id")
        key = (project_id, source if isinstance(source, int) else 0)
        if key not in self._heads:
            self._heads[key] = self.backend._head_repo(self.target, mr, project_id, canonical)
        return self._heads[key]

    def rate_limit(self) -> m.RateLimit:
        readings = [r for r in self.env.gateway.rate_readings if r.host == self.host]
        if not readings:
            return m.RateLimit(host=self.host, remaining=None, reset_at=None)
        low = min(readings, key=lambda r: r.remaining)
        return m.RateLimit(host=self.host, remaining=low.remaining, reset_at=low.reset_at)

    # -- one MR --------------------------------------------------------------------------------

    def _item(self, ref: PrRef, node: dict[str, Any]) -> m.Snapshot:
        backend, target = self.backend, self.target
        number = ref.number
        iid = node.get("iid")
        if not isinstance(iid, str) or not iid.isdigit() or int(iid) != number:
            raise Unresolved(BULK_UNAVAILABLE, "unusable answer: iid")
        canonical = backend._canonical_path(node.get("webUrl"), target, number)
        mr_id = _gid(node.get("id"), "id")
        project_id = _gid(_obj(node.get("project"), "project").get("id"), "project id")
        if node.get("targetProjectId") != project_id:
            raise Unresolved(BULK_UNAVAILABLE, "unusable answer: target project")
        source_id = node.get("sourceProjectId")
        mr = {
            "source_project_id": source_id if isinstance(source_id, int) else None,
            "target_branch": node.get("targetBranch"),
        }
        head_sha = gl._sha(node.get("diffHeadSha"), "sha")
        detailed = _lower(node.get("detailedMergeStatus"))
        if detailed is None:
            raise Unresolved(BULK_UNAVAILABLE, "the host reports no detailed merge status")
        if detailed in gl.READINESS_UNKNOWN:
            raise Unresolved(BULK_UNAVAILABLE, "mergeability is still being computed")
        state = gl.GitLabBackend._state(_lower(node.get("state")))
        project = self.project(project_id, canonical)
        author_user = gl._user(_user(node.get("author"), "author"), "author")
        author = m.Author(
            login=author_user.login,
            id=str(author_user.id),
            is_bot=backend._is_bot(target, author_user),
        )
        draft = gl._flag(node.get("draft"), "draft")

        ci = self._ci(node, head_sha)
        entries, approvals_left = self._table(node)
        reset = self.reset_on_push(project_id)
        entries = backend._with_can_push(target, project_id, entries, author, reset)
        retarget_at, retarget_known = (None, True)
        if reset is True and any(e.state == "approved" for e in entries):
            retarget_at, retarget_known = backend._retarget(target, project_id, number)
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
        has_conflicts = gl._flag(node.get("conflicts"), "conflicts")
        status = detailed
        facts = m.BlockFacts(
            state=state,
            ci=ci,
            draft=draft or status == "draft_status",
            conflicts=has_conflicts or status == "conflict",
            behind=status == "need_rebase",
            mergeability_unknown=False,
            locked=_lower(node.get("state")) == "locked",
            reviews_required=status == "not_approved",
            host_discussions_unresolved=status == "discussions_not_resolved",
            host_changes_requested=status == "requested_changes",
            host_checks_pending=status == "ci_still_running",
            host_ci_must_pass=status == "ci_must_pass",
            other_details=backend._other_details(status),
            latest_by_reviewer=entries,
        )
        mergeable: bool | None = None
        if state == "open":
            mergeable = status == "mergeable" and not has_conflicts
        merged = state == "merged"
        merged_by = None
        merge_user = node.get("mergeUser")
        if merged and merge_user:
            merged_by = gl._login(_obj(merge_user, "merge user").get("username"), "merge_user")
        enabled = not merged and (
            node.get("mergeWhenPipelineSucceeds") is True or node.get("autoMergeEnabled") is True
        )
        by = None
        if enabled and merge_user:
            by = gl._login(_obj(merge_user, "merge user").get("username"), "merge_user")
        merge_commit = node.get("mergeCommitSha")
        if merged and merge_commit is None:
            raise Unresolved(BULK_UNAVAILABLE, "a merged MR without a merge commit")
        head_repo, is_fork = self.head_repo(mr, project_id, canonical)
        host_path = f"{ref.host}/{canonical}"
        summary = _obj(node.get("diffStatsSummary"), "diff stats")
        refs = _opt_obj(node.get("diffRefs")) or {}
        base_sha = refs.get("baseSha")
        labels = _nodes(node.get("labels"), "labels")
        pr = m.PR(
            pr=f"{host_path}#{number}",
            repo=host_path,
            pr_id=f"gid://gitlab/MergeRequest/{mr_id}",
            number=number,
            url=f"https://{target.host}{target.api_prefix}/{canonical}/-/merge_requests/{number}",
            backend="gitlab",
            title=gl._req_line(node.get("title"), "title"),
            body=gl._text(node.get("description"), "description", line=False, allow_none=True),
            author=author,
            state=state,
            draft=draft or status == "draft_status",
            created_at=gl._required_time(node.get("createdAt"), "created_at"),
            updated_at=gl._required_time(node.get("updatedAt"), "updated_at"),
            closed_at=gl._rfc3339(node.get("closedAt")) if state == "closed" else None,
            merged_at=gl._required_time(node.get("mergedAt"), "merged_at") if merged else None,
            merged_by=merged_by,
            labels=tuple(
                sorted({gl._req_line(_obj(x, "label").get("title"), "label") for x in labels})
            ),
            author_association=None,
            head=m.Head(
                sha=head_sha,
                ref=gl._req_line(node.get("sourceBranch"), "source_branch"),
                repo=None if head_repo is None else f"{ref.host}/{head_repo}",
                is_fork=is_fork,
            ),
            base=m.Base(
                ref=gl._req_line(node.get("targetBranch"), "target_branch"),
                sha=gl._sha(base_sha, "base_sha") if base_sha is not None else None,
                is_default=backend._is_default(mr, project),
            ),
            merged_head_sha=head_sha if merged else None,
            merge_commit_sha=gl._sha(merge_commit, "merge_commit_sha") if merged else None,
            head_fetch_ref=f"refs/merge-requests/{number}/head",
            mergeable=mergeable,
            blocked_by=m.compute_blocked_by(facts),
            auto_merge=m.AutoMerge(enabled=enabled, by=by),
            merge_queue=backend._merge_queue(project),
            merge_methods_allowed=backend._methods(project),
            additions=_count(summary.get("additions")),
            deletions=_count(summary.get("deletions")),
            changed_files=_count(summary.get("fileCount")),
            review_decision=m.gitlab_review_decision(entries, approvals_left, approvals),
            approvals=approvals,
            approvals_at_head=at_head,
        )
        return self._snapshot(ref, node, pr, ci, entries, mr_id, project_id, canonical, head_sha)

    # -- the parts ------------------------------------------------------------------------------

    def _table(self, node: Mapping[str, Any]) -> tuple[tuple[m.LatestByReviewer, ...], int | None]:
        """``GitLabBackend._reviewers`` over the GraphQL approvers and reviewers."""
        approved: dict[int, tuple[gl._User, str | None]] = {}
        reviewer_states: dict[int, tuple[gl._User, str]] = {}
        reviewers = _nodes(node.get("reviewers"), "reviewers")
        times: dict[int, str | None] = {}
        for raw in reviewers:
            entry = _obj(raw, "reviewer")
            user = gl._user(_user(entry, "reviewer"), "reviewer")
            interaction = _opt_obj(entry.get("mergeRequestInteraction")) or {}
            state = _lower(interaction.get("reviewState"))
            reviewer_states[user.id] = (user, state or "")
            times[user.id] = gl._rfc3339(interaction.get("approvedAt"))
        for raw in _nodes(node.get("approvedBy"), "approvers"):
            user = gl._user(_user(raw, "approver"), "approver")
            approved[user.id] = (user, times.get(user.id))
        left = node.get("approvalsLeft")
        approvals_left = left if isinstance(left, int) and not isinstance(left, bool) else None
        if approvals_left is not None and approvals_left < 0:
            approvals_left = None
        entries: list[m.LatestByReviewer] = []
        for account in sorted(set(approved) | set(reviewer_states)):
            reviewer = reviewer_states.get(account)
            user = approved[account][0] if account in approved else reviewer_states[account][0]
            rstate = reviewer[1] if reviewer else ""
            if rstate == "requested_changes":
                entries.append(self.backend._entry(user, "changes_requested", None))
            elif account in approved:
                entries.append(self.backend._entry(user, "approved", approved[account][1]))
            elif rstate == "unapproved":
                entries.append(self.backend._entry(user, "dismissed", None))
        return tuple(entries), approvals_left

    def _ci(self, node: Mapping[str, Any], head_sha: str) -> m.CI:
        """``GitLabBackend._ci`` over the head pipeline's jobs: a status per job, the pipeline's
        own status, and a check per trigger job from its downstream pipeline."""
        pipeline = node.get("headPipeline")
        if pipeline is None:
            return m.aggregate_ci((), pr_head_sha=head_sha, checks_sha=None)
        data = _obj(pipeline, "head pipeline")
        sha = gl._sha(data.get("sha"), "pipeline sha")
        host = self.target.host + self.target.api_prefix
        jobs = _nodes(_obj(data, "head pipeline").get("jobs"), "jobs")
        rows: list[dict[str, Any]] = []
        children: list[m.Check] = []
        for raw in jobs:
            job = _obj(raw, "job")
            status = _lower(job.get("status"))
            name = job.get("name")
            if status is None or not isinstance(name, str):
                raise Unresolved(BULK_UNAVAILABLE, "unusable answer: job")
            allow = job.get("allowFailure")
            details = _opt_obj(job.get("detailedStatus")) or {}
            path = details.get("detailsPath")
            rows.append(
                {
                    "id": _gid(job.get("id"), "job id"),
                    "name": name,
                    "status": status,
                    "allow_failure": allow if isinstance(allow, bool) else None,
                    "target_url": f"https://{host}{path}" if isinstance(path, str) else None,
                    "finished_at": gl._rfc3339(job.get("finishedAt")),
                }
            )
            if job.get("kind") == "BRIDGE":
                down = _opt_obj(job.get("downstreamPipeline"))
                row: dict[str, Any] = {
                    "name": name,
                    "allow_failure": allow if isinstance(allow, bool) else None,
                    "finished_at": gl._rfc3339(job.get("finishedAt")),
                    "downstream_pipeline": None
                    if down is None
                    else {
                        "status": _lower(down.get("status")),
                        "web_url": (
                            f"https://{host}{down.get('path')}"
                            if isinstance(down.get("path"), str)
                            else None
                        ),
                    },
                }
                children.append(self.backend._bridge_check(row))
        checks = self.backend._checks(rows)
        path = data.get("path")
        own = self.backend._pipeline_check(
            {
                "status": _lower(data.get("status")),
                "web_url": f"https://{host}{path}" if isinstance(path, str) else None,
            },
            jobs_listed=True,
        )
        if own is not None:
            checks.append(own)
        checks.extend(children)
        return m.aggregate_ci(checks, pr_head_sha=head_sha, checks_sha=sha, complete=True)

    def _snapshot(
        self,
        ref: PrRef,
        node: Mapping[str, Any],
        pr: m.PR,
        ci: m.CI,
        entries: tuple[m.LatestByReviewer, ...],
        mr_id: int,
        project_id: int,
        canonical: str,
        head: str,
    ) -> m.Snapshot:
        reader = gls._Reader(
            backend=self.backend,
            target=self.target,
            ref=ref,
            now=self.now,
            limits=gls.Limits(ref.host),
        )
        reader.mr_id, reader.project_id, reader.canonical, reader.head = (
            mr_id,
            project_id,
            canonical,
            head,
        )
        discussions = _nodes(node.get("discussions"), "discussions")
        rows: list[dict[str, Any]] = []
        notes: list[dict[str, Any]] = []
        for raw in discussions:
            row = _discussion(raw, mr_id)
            rows.append(row)
            notes.extend(row["notes"])
        threads: list[m.Thread] = []
        seen: set[object] = set()
        note_ids: set[int] = set()
        for row in rows:
            if row["id"] in seen:
                raise Unresolved(BULK_UNAVAILABLE, "a discussion listed twice")
            found = gls._thread(reader, row, note_ids)
            seen.add(row["id"])
            if found is not None:
                threads.append(found)
        requests = self._requests(reader, node, notes)
        head_push = self._push(node, head)
        incomplete: list[str] = []
        if not requests.complete:
            incomplete.append("review_requests")
        activity = derive_activity(
            self.env.hosts,
            ref.host,
            call=self.env.call,
            threads=tuple(threads),
            reviews=(),
            approvals=entries,
            head_push=head_push,
        )
        return m.Snapshot(
            pr=pr,
            review_requests=requests.requests,
            teams_visible=True,
            reviews=(),
            latest_by_reviewer=entries,
            threads=tuple(threads),
            ci=ci,
            rate_limit=self.rate_limit(),
            fetched_at=gls._stamp(self.now()),
            incomplete=tuple(incomplete),
            head_push=head_push,
            activity=activity,
        )

    def _requests(
        self, reader: gls._Reader, node: Mapping[str, Any], notes: list[dict[str, Any]]
    ) -> gls.RequestsRead:
        """``gitlab_snapshot._read_requests`` over the system notes of the discussions."""
        reviewers: list[gls._Reviewer] = []
        for raw in _nodes(node.get("reviewers"), "reviewers"):
            entry = _obj(raw, "reviewer")
            user = gl._user(_user(entry, "reviewer"), "reviewer")
            interaction = _opt_obj(entry.get("mergeRequestInteraction")) or {}
            reviewers.append(gls._Reviewer(user, _lower(interaction.get("reviewState")) or ""))
        if not reviewers:
            return gls.RequestsRead((), True)
        wanted = {m.ascii_lower(x.user.login) for x in reviewers}
        newest: dict[str, tuple[datetime, int, str]] = {}
        for note in notes:
            if gls._scan_note(reader, note, wanted, newest):
                raise Unresolved(BULK_UNAVAILABLE, "a review request note is too long to parse")
        requests: list[m.ReviewRequest] = []
        for x in reviewers:
            hit = newest.get(m.ascii_lower(x.user.login))
            requests.append(
                m.ReviewRequest(
                    reviewer=m.Reviewer(login=x.user.login, id=str(x.user.id), kind="user"),
                    state=gls._request_state(reader, x.state),
                    code_owner=None,
                    instance=None if hit is None else str(hit[1]),
                    requested_at=None if hit is None else hit[2],
                )
            )
        return gls.RequestsRead(tuple(requests), True)

    def _push(self, node: Mapping[str, Any], head: str) -> m.PushTime | None:
        """The newest diff version's creation time is the head's push (``push_event``); with no
        version, the head commit's date (``commit_date``)."""
        versions = _nodes_or_empty(_obj(node.get("mergeRequestDiffs"), "diffs"))
        found: tuple[str, str] | None = None
        if versions:
            stamp = gl._rfc3339(_obj(versions[-1], "diff").get("createdAt"))
            if stamp is not None:
                found = (stamp, "push_event")
        if found is None:
            commits = _nodes_or_empty(_obj(node.get("commits"), "commits"))
            last = _opt_obj(commits[-1]) if commits else None
            stamp = gl._rfc3339(last.get("committedDate")) if last is not None else None
            if last is not None and last.get("sha") == head and stamp is not None:
                found = (stamp, "commit_date")
        if found is None:
            return None
        when = datetime.fromisoformat(found[0])
        if when > self.now() + gls.FUTURE_SKEW:
            self.env.warn("the push time of the head lies in the future and is not trusted")
            return None
        source: Any = found[1]
        return m.PushTime(at=found[0], source=source)


def _nodes_or_empty(connection: Mapping[str, Any]) -> list[Any]:
    return _list(connection.get("nodes"), "nodes")


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _position(raw: object) -> dict[str, Any] | None:
    pos = _opt_obj(raw)
    if pos is None:
        return None
    return {
        "new_path": pos.get("newPath"),
        "old_path": pos.get("oldPath"),
        "new_line": pos.get("newLine"),
        "old_line": pos.get("oldLine"),
        "position_type": _lower(pos.get("positionType")),
    }


def _discussion(raw: object, mr_id: int) -> dict[str, Any]:
    """A GraphQL discussion as the REST discussion ``gitlab_snapshot._thread`` reads."""
    disc = _obj(raw, "discussion")
    ident = disc.get("id")
    match = (
        re.fullmatch(r"gid://gitlab/Discussion/([0-9a-f]{40})", ident)
        if isinstance(ident, str)
        else None
    )
    if match is None:
        raise Unresolved(BULK_UNAVAILABLE, "unusable answer: discussion id")
    notes: list[dict[str, Any]] = []
    for item in _nodes(disc.get("notes"), "discussion notes"):
        note = _obj(item, "note")
        notes.append(
            {
                "id": _gid(note.get("id"), "note id"),
                "body": note.get("body"),
                "system": note.get("system") is True,
                "created_at": note.get("createdAt"),
                "updated_at": note.get("updatedAt"),
                "resolvable": note.get("resolvable") is True,
                "resolved": note.get("resolved") is True,
                "author": _user(note.get("author"), "note author"),
                "position": _position(note.get("position")),
                "noteable_id": mr_id,
                "noteable_type": "MergeRequest",
            }
        )
    return {"id": match.group(1), "individual_note": False, "notes": notes}


# -- the entry point ------------------------------------------------------------------------------


def _key(node: object) -> tuple[str, int] | None:
    """``(project path, iid)`` of a list node, the path in lower case (paths are not case
    sensitive), or None when the node does not name them."""
    if not isinstance(node, dict):
        return None
    project = node.get("project")
    path = project.get("fullPath") if isinstance(project, dict) else None
    iid = node.get("iid")
    if not isinstance(path, str) or not isinstance(iid, str) or not iid.isdigit():
        return None
    return path.lower(), int(iid)


def _wanted_ref(wanted: Mapping[tuple[str, int], PrRef], node: object) -> PrRef | None:
    key = _key(node)
    return wanted.get(key) if key is not None else None


def read_bulk(
    env: ReadEnv,
    host: str,
    refs: Sequence[PrRef],
    *,
    now: Callable[[], datetime] = gls.utc_now,
    page_size: int = PAGE_SIZE,
) -> BulkRead:
    """The snapshots of ``refs`` (all on ``host``) that the bulk reader can give whole, and why
    not for the others. A host that fails the capability probe has none."""
    if not refs:
        return BulkRead()
    reason = probe(env, host)
    if reason is not None:
        out = BulkRead(reason=f"{host}: the bulk reader is not usable: {reason}")
        for ref in refs:
            out.unresolved[ref] = Unresolved(BULK_UNAVAILABLE, "the host failed the bulk probe")
        return out
    run = _Run(env, host, now)
    result = BulkRead()
    wanted = {(ref.project.lower(), ref.number): ref for ref in refs}
    try:
        run.read_lists(wanted, result, page_size)
    except ForgectlError as error:
        if error.code not in HOST_STOPS:
            raise
        result.stopped = error
        give_up_stop(result, [r for r in refs if r not in result.snapshots], error)
        return result
    missing = [r for r in refs if r not in result.snapshots and r not in result.unresolved]
    if missing:
        more = run_pages(missing, LOOKUP_PAGE, run.ask)
        result.snapshots.update(more.snapshots)
        result.unresolved.update(more.unresolved)
        result.stopped = more.stopped
    return result
