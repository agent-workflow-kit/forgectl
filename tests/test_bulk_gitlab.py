"""The GitLab bulk reader (#116): the capability probe, the aliased query, equality with the
per-PR snapshot on the same facts, the page size and every way an MR is given up. The gateway is
real; `glab` is an in-process fake that answers the GraphQL request from synthetic documents
composed from the REST recordings of the per-PR reads."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from forgectl.backends import gitlab_bulk
from forgectl.backends.bulk_common import BULK_UNAVAILABLE
from forgectl.backends.gitlab_bulk import (
    PROBE_QUERY,
    bulk_query,
    list_query,
    read_bulk,
    reset_probes,
)
from forgectl.errors import ForgectlError
from forgectl.gateway import ProcessResult
from forgectl.model import comparable
from forgectl.reads import ReadEnv

from .gitlab_support import HEAD, FakeGitLab, make_hosts, recording, ref, standard
from .harness import schemas
from .test_gitlab_snapshot import APPROVALS, Rig, snap_rec

NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
HOST = "gitlab.com"


@pytest.fixture(autouse=True)
def fresh_probes() -> None:
    reset_probes()


def gid(kind: str, number: int) -> str:
    return f"gid://gitlab/{kind}/{number}"


def user_node(rest: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": gid("User", rest["id"]), "username": rest["username"], "bot": False}


def graph_time(text: str) -> str:
    return text


def discussion_node(rest: Mapping[str, Any]) -> dict[str, Any]:
    notes = []
    for note in rest["notes"]:
        position = note.get("position")
        notes.append(
            {
                "id": gid("DiffNote" if position else "Note", note["id"]),
                "body": note["body"],
                "system": note.get("system", False),
                "createdAt": note["created_at"],
                "updatedAt": note["updated_at"],
                "resolvable": note.get("resolvable", False),
                "resolved": note.get("resolved", False),
                "author": user_node(note["author"]),
                "position": None
                if not position
                else {
                    "newPath": position.get("new_path"),
                    "oldPath": position.get("old_path"),
                    "newLine": position.get("new_line"),
                    "oldLine": position.get("old_line"),
                    "positionType": position.get("position_type", "text").upper(),
                },
            }
        )
    return {
        "id": f"gid://gitlab/Discussion/{rest['id']}",
        "resolvable": any(n["resolvable"] for n in notes),
        "resolved": all(n["resolved"] for n in notes if n["resolvable"])
        and any(n["resolvable"] for n in notes),
        "notes": {"pageInfo": {"hasNextPage": False}, "nodes": notes},
    }


def mr_node(number: int = 7) -> dict[str, Any]:
    """A GraphQL ``mergeRequest`` composed from the REST recordings of the per-PR reads."""
    mr = recording("mr")
    approvals = recording("approvals")
    reviewers = recording("reviewers")
    statuses = recording("statuses")
    notes = recording("notes")
    discussions = snap_rec("discussions")
    versions = snap_rec("versions")
    states = {r["user"]["id"]: r["state"] for r in reviewers}
    head = mr["head_pipeline"]
    system = [
        {
            "id": f"gid://gitlab/Discussion/{(str(n['id']) * 40)[:40]}",
            "resolvable": False,
            "resolved": False,
            "notes": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [
                    {
                        "id": gid("Note", n["id"]),
                        "body": n["body"],
                        "system": True,
                        "createdAt": n["created_at"],
                        "updatedAt": n["updated_at"],
                        "resolvable": False,
                        "resolved": False,
                        "author": user_node(n["author"]),
                        "position": None,
                    }
                ],
            },
        }
        for n in notes
    ]
    return {
        "id": gid("MergeRequest", mr["id"]),
        "iid": str(number),
        "webUrl": mr["web_url"].replace("/7", f"/{number}"),
        "title": mr["title"],
        "description": mr["description"],
        "state": "opened",
        "draft": False,
        "createdAt": mr["created_at"],
        "updatedAt": mr["updated_at"],
        "closedAt": None,
        "mergedAt": None,
        "mergeCommitSha": None,
        "project": {"id": gid("Project", 101), "fullPath": "user-9/proj"},
        "sourceProjectId": 101,
        "targetProjectId": 101,
        "author": user_node(mr["author"]),
        "mergeUser": None,
        "mergeWhenPipelineSucceeds": False,
        "autoMergeEnabled": False,
        "sourceBranch": mr["source_branch"],
        "targetBranch": mr["target_branch"],
        "diffHeadSha": mr["sha"],
        "diffRefs": {"baseSha": mr["diff_refs"]["base_sha"]},
        "detailedMergeStatus": "MERGEABLE",
        "conflicts": False,
        "approvalsLeft": approvals["approvals_left"],
        "approvedBy": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [user_node(a["user"]) for a in approvals["approved_by"]],
        },
        "reviewers": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {
                    **user_node(r),
                    "mergeRequestInteraction": {
                        "approved": states[r["id"]] == "approved",
                        "approvedAt": None,
                        "reviewState": states[r["id"]].upper(),
                    },
                }
                for r in mr["reviewers"]
            ],
        },
        "labels": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{"title": t} for t in mr["labels"]],
        },
        "diffStatsSummary": {"additions": 10, "deletions": 2, "fileCount": 3},
        "mergeRequestDiffs": {"nodes": [{"createdAt": versions[0]["created_at"]}]},
        "commits": {"nodes": [{"sha": HEAD, "committedDate": "2026-10-01T08:59:00.000Z"}]},
        "headPipeline": {
            "id": gid("Pipeline", head["id"]),
            "sha": head["sha"],
            "status": head["status"].upper(),
            "path": "/user-9/proj/-/pipelines/5001",
            "jobs": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [
                    {
                        "id": gid("Ci::Build", s["id"]),
                        "name": s["name"],
                        "kind": "BUILD",
                        "status": s["status"].upper(),
                        "allowFailure": s["allow_failure"],
                        "finishedAt": s["finished_at"],
                        "detailedStatus": {"detailsPath": f"/user-9/proj/-/jobs/{s['id']}"},
                        "downstreamPipeline": None,
                    }
                    for s in statuses
                ],
            },
        },
        "discussions": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [*(discussion_node(d) for d in discussions), *system],
        },
    }


def lookup_answer(
    request: Mapping[str, Any], nodes: Mapping[int, dict[str, Any]] | None = None
) -> Any:
    """The answer to aliased ``project { mergeRequest }`` lookups."""
    variables = request["variables"]
    out: dict[str, Any] = {"queryComplexity": {"score": 120, "limit": 250}}
    for i in range(len(variables) // 2):
        number = int(variables[f"i{i}"])
        node = (nodes or {}).get(number) or mr_node(number)
        out[f"m{i}"] = {"mergeRequest": node}
    return {"data": out}


def list_answer(nodes: Sequence[dict[str, Any]], request: Mapping[str, Any], field: str) -> Any:
    """One page of ``currentUser``'s list; the cursor is the offset."""
    variables = request["variables"]
    start = int(variables["after"] or 0)
    size = variables["first"]
    page = list(nodes[start : start + size])
    end = start + len(page)
    return {
        "data": {
            "queryComplexity": {"score": 160 + 2 * len(page), "limit": 250},
            "currentUser": {
                field: {
                    "pageInfo": {"hasNextPage": end < len(nodes), "endCursor": str(end)},
                    "nodes": page,
                }
            },
        }
    }


PROBE_OK = {
    "data": {
        "queryComplexity": {"score": 170, "limit": 250},
        "currentUser": {
            "authoredMergeRequests": {"nodes": []},
            "reviewRequestedMergeRequests": {"nodes": []},
        },
    }
}


class GraphFake(FakeGitLab):
    """``FakeGitLab`` that also answers ``POST graphql``: the probe, the two lists of
    ``currentUser`` (``authored`` and ``requested``) and the aliased lookups (``lookup``)."""

    def __init__(self, authored: Sequence[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.authored: list[dict[str, Any]] = (
            [mr_node(n) for n in range(1, 200)] if authored is None else list(authored)
        )
        self.requested: list[dict[str, Any]] = []
        self.lookup: Callable[[dict[str, Any]], Any] = lookup_answer
        self.list_hook: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None
        self.requests: list[dict[str, Any]] = []
        self.probe: Any = PROBE_OK

    def runner(self, argv: Sequence[str], **kwargs: Any) -> ProcessResult:  # type: ignore[override]
        if argv[-1] != "graphql":
            return super().runner(argv, **kwargs)
        request = json.loads(kwargs["stdin"])
        self.requests.append(request)
        text = request["query"]
        if "query ForgectlProbe" in text:
            answer = self.probe
        elif "query ForgectlList" in text:
            field = (
                "authoredMergeRequests"
                if "authoredMergeRequests" in text
                else "reviewRequestedMergeRequests"
            )
            nodes = self.authored if field == "authoredMergeRequests" else self.requested
            answer = list_answer(nodes, request, field)
            if self.list_hook is not None:
                answer = self.list_hook(request, answer) or answer
        else:
            answer = self.lookup(request)
        if isinstance(answer, ProcessResult):
            return answer
        raw = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        ok = not isinstance(answer, bytes) or answer.startswith(b"HTTP/2.0 2")
        head = b"" if isinstance(answer, bytes) else b"HTTP/2.0 200 OK\r\n\r\n"
        return ProcessResult(1, 1, 0 if ok else 1, head + raw, b"")

    def kinds(self) -> list[str]:
        """The kind of each request: ``probe``, ``list`` or ``lookup``."""
        out = []
        for r in self.requests:
            q = r["query"]
            out.append(
                "probe" if "ForgectlProbe" in q else "list" if "ForgectlList" in q else "lookup"
            )
        return out

    def list_sizes(self) -> list[int]:
        return [r["variables"]["first"] for r in self.requests if "ForgectlList" in r["query"]]

    def lookup_sizes(self) -> list[int]:
        return [len(r["variables"]) // 2 for r in self.requests if "ForgectlBulk" in r["query"]]


def env_of(fake: FakeGitLab, tmp_path: Path, **kwargs: Any) -> ReadEnv:
    hosts = make_hosts(fake, tmp_path)
    return ReadEnv(gateway=hosts.gateway, hosts=hosts, retries=kwargs.pop("retries", 0), **kwargs)


def read(fake: GraphFake, tmp_path: Path, refs: Sequence[Any] | None = None, **kw: Any) -> Any:
    standard(fake)
    fake.on("user", recording("user"))
    for account in (12, 13):
        fake.on(
            f"users/{account}",
            {**recording("user"), "id": account, "username": f"user-{account - 10}"},
        )
    return read_bulk(env_of(fake, tmp_path), HOST, refs or [ref()], now=lambda: NOW, **kw)


# -- equality with the per-PR snapshot -----------------------------------------------------------


def neutral(snap: dict[str, Any]) -> dict[str, Any]:
    """A snapshot without what the bulk reader documents as different: the real additions and
    deletions, and the position facts a GraphQL note does not carry."""
    out = copy.deepcopy(comparable(snap))
    out["additions"] = out["deletions"] = None
    for thread in out["threads"]:
        thread["commit_sha"] = thread["start_line"] = None
    return out


def test_bulk_item_equals_the_per_pr_snapshot_on_the_same_facts(tmp_path: Path) -> None:
    per_pr_fake = GraphFake()
    per_pr = Rig(per_pr_fake, tmp_path / "a").snapshot()
    fake = GraphFake()
    got = read(fake, tmp_path / "b")
    assert got.unresolved == {}
    bulk = got.snapshots[ref()].to_dict()
    assert neutral(bulk) == neutral(per_pr)
    schemas.validate_cli_output("snapshot", bulk)
    expected = recording("approvals")["approvals_left"]
    assert bulk["approvals_left"] == per_pr["approvals_left"] == expected


def test_bulk_and_per_pr_agree_on_a_nonzero_approvals_left(tmp_path: Path) -> None:
    approvals = {**recording("approvals"), "approvals_left": 2}
    per_pr_fake = GraphFake()
    rig = Rig(per_pr_fake, tmp_path / "a")
    per_pr_fake.on(APPROVALS, approvals)
    per_pr = rig.snapshot()
    node = mr_node()
    node["approvalsLeft"] = 2
    fake = GraphFake(authored=[node])
    bulk = read(fake, tmp_path / "b").snapshots[ref()].to_dict()
    assert bulk["approvals_left"] == per_pr["approvals_left"] == 2


@pytest.mark.parametrize(
    ("raw", "expected"), [(2, 2), (0, 0), (None, None), (-1, None), (True, None), ("1", None)]
)
def test_bulk_approvals_left_is_a_non_negative_integer_or_null(
    tmp_path: Path, raw: object, expected: int | None
) -> None:
    node = mr_node()
    node["approvalsLeft"] = raw
    fake = GraphFake(authored=[node])
    bulk = read(fake, tmp_path).snapshots[ref()].to_dict()
    assert bulk["approvals_left"] == expected


def test_bulk_gives_real_additions_and_deletions(tmp_path: Path) -> None:
    bulk = read(GraphFake(), tmp_path).snapshots[ref()].to_dict()
    assert (bulk["additions"], bulk["deletions"], bulk["changed_files"]) == (10, 2, 3)


def test_bulk_makes_few_calls_and_reads_the_project_once(tmp_path: Path) -> None:
    fake = GraphFake()
    refs = [ref(number=n) for n in (7, 8, 9)]
    got = read(fake, tmp_path, refs)
    assert len(got.snapshots) == 3
    assert fake.kinds() == ["probe", "list"]  # one request reads the three MRs
    assert fake.count("projects/101") == 1 and fake.count("projects/101/approvals") == 1
    assert fake.count("projects/user-9%2Fproj/merge_requests/7") == 0  # no per-MR REST read


def test_a_requested_reviewer_gets_the_instance_of_the_newest_request_note(tmp_path: Path) -> None:
    bulk = read(GraphFake(), tmp_path).snapshots[ref()].to_dict()
    by = {r["reviewer"]["login"]: r for r in bulk["review_requests"]}
    assert by["user-2"]["instance"] == "801" and by["user-2"]["requested_at"] is not None


# -- the probe ---------------------------------------------------------------------------------


def test_the_probe_is_one_query_naming_every_field_of_the_reader() -> None:
    for name in (
        "queryComplexity",
        "detailedMergeStatus",
        "diffStatsSummary",
        "mergeRequestDiffs",
        "mergeRequestInteraction",
        "approvalsLeft",
        "headPipeline",
        "authoredMergeRequests",
        "reviewRequestedMergeRequests",
    ):
        assert name in PROBE_QUERY


def rejected(message: str) -> Any:
    body = json.dumps({"errors": [{"message": message}]}).encode()
    return b"HTTP/2.0 200 OK\r\n\r\n" + body


@pytest.mark.parametrize(
    "missing",
    ["queryComplexity", "detailedMergeStatus", "mergeRequestDiffs", "mergeRequestInteraction"],
)
def test_a_host_missing_a_field_uses_the_per_pr_path_and_says_why(
    tmp_path: Path, missing: str
) -> None:
    fake = GraphFake()
    fake.probe = rejected(f"Field '{missing}' doesn't exist on type 'Query'")
    got = read(fake, tmp_path)
    assert got.snapshots == {} and got.unresolved[ref()].code == BULK_UNAVAILABLE
    assert got.reason is not None and missing in got.reason
    assert fake.kinds() == ["probe"]  # no bulk request after a failed probe


def test_the_probe_is_asked_once_per_host(tmp_path: Path) -> None:
    fake = GraphFake()
    read(fake, tmp_path)
    read(fake, tmp_path, [ref(number=8)])
    assert fake.kinds().count("probe") == 1


def test_a_failed_probe_is_remembered_and_not_asked_again(tmp_path: Path) -> None:
    fake = GraphFake()
    fake.probe = rejected("Field 'queryComplexity' doesn't exist on type 'Query'")
    read(fake, tmp_path)
    read(fake, tmp_path)
    assert fake.kinds().count("probe") == 1


def test_a_probe_that_fails_on_the_network_is_not_remembered(tmp_path: Path) -> None:
    fake = GraphFake()
    fake.probe = b"HTTP/2.0 502 Bad Gateway\r\n\r\nnope"
    with pytest.raises(ForgectlError):
        read(fake, tmp_path)
    assert HOST not in gitlab_bulk._PROBES


# -- the queries and the page size ----------------------------------------


def test_the_queries_are_constant_text_and_values_travel_as_variables(tmp_path: Path) -> None:
    fake = GraphFake(authored=[])
    read(fake, tmp_path, [ref(number=7), ref("grp/sub/other", 9)])
    (first,) = [r for r in fake.requests if "ForgectlList" in r["query"]][:1]
    assert first["query"] == list_query("authoredMergeRequests")
    assert first["variables"] == {"first": 30, "after": None}
    (lookup,) = [r for r in fake.requests if "ForgectlBulk" in r["query"]]
    assert lookup["query"] == bulk_query(2)
    assert lookup["variables"] == {"p0": "user-9/proj", "i0": "7", "p1": "grp/sub/other", "i1": "9"}
    assert "user-9" not in lookup["query"] and "grp/sub" not in lookup["query"]
    assert "m0: project(fullPath: $p0) { mergeRequest(iid: $i0)" in lookup["query"]
    for connection in ("discussions(first:", "jobs(first:", "reviewers(first:", "labels(first:"):
        assert connection in lookup["query"]


def test_lists_are_read_in_pages_of_thirty_and_stop_when_every_mr_is_seen(
    tmp_path: Path,
) -> None:
    fake = GraphFake()
    refs = [ref(number=n) for n in range(1, 71)]
    got = read(fake, tmp_path, refs)
    assert fake.list_sizes() == [30, 30, 30]  # the 70th MR is on the third page
    assert len(got.snapshots) == 70 and fake.lookup_sizes() == []


def test_the_second_list_is_read_only_for_what_the_first_did_not_show(tmp_path: Path) -> None:
    fake = GraphFake(authored=[mr_node(7)])
    fake.requested = [mr_node(8), mr_node(9)]
    got = read(fake, tmp_path, [ref(number=7), ref(number=8)])
    assert set(got.snapshots) == {ref(number=7), ref(number=8)}
    assert fake.kinds() == ["probe", "list", "list"]
    reset_probes()
    fake2 = GraphFake()
    read(fake2, tmp_path / "x", [ref(number=7)])
    assert fake2.kinds() == ["probe", "list"]  # found in the first list: the second is not asked


def test_an_mr_in_neither_list_is_looked_up_two_to_a_request(tmp_path: Path) -> None:
    fake = GraphFake(authored=[])
    got = read(fake, tmp_path, [ref(number=n) for n in (7, 8, 9)])
    assert fake.lookup_sizes() == [2, 1]
    assert len(got.snapshots) == 3


def test_a_complexity_error_halves_the_list_page(tmp_path: Path) -> None:
    fake = GraphFake()

    def refuse(request: dict[str, Any], answer: Any) -> Any:
        if request["variables"]["first"] > 10:
            return rejected("Query has complexity of 300, which exceeds max complexity of 250")
        return None

    fake.list_hook = refuse
    got = read(fake, tmp_path, [ref(number=n) for n in range(1, 31)])
    assert fake.list_sizes() == [30, 15, 7, 7, 7, 7, 7]
    assert len(got.snapshots) == 30


def test_a_list_that_keeps_failing_gives_up_after_bounded_halvings_and_looks_up(
    tmp_path: Path,
) -> None:
    fake = GraphFake()
    fake.list_hook = lambda r, a: rejected("Query has complexity of 999, which exceeds 250")
    got = read(fake, tmp_path, [ref(number=7)])
    assert fake.list_sizes() == [30, 15, 7, 3, 3]  # three halvings in all, then the lists end
    assert len(got.snapshots) == 1  # read by a lookup after all
    assert fake.lookup_sizes() == [1]


def test_lists_that_never_end_are_bounded(tmp_path: Path) -> None:
    fake = GraphFake(authored=[mr_node(n) for n in range(1000, 1400)])
    got = read(fake, tmp_path, [ref(number=7)])
    assert len(fake.list_sizes()) <= 20  # MAX_LIST_PAGES for each list
    assert len(got.snapshots) == 1  # looked up


# -- an MR given up --------------------------------------------------------------------------------


def read_with(tmp_path: Path, mutate: Callable[[dict[str, Any]], None]) -> Any:
    node = mr_node(7)
    mutate(node)
    return read(GraphFake([node]), tmp_path)


CUTS = {
    "discussions": lambda mr: mr["discussions"]["pageInfo"].update(hasNextPage=True),
    "discussion notes": lambda mr: mr["discussions"]["nodes"][0]["notes"]["pageInfo"].update(
        hasNextPage=True
    ),
    "jobs": lambda mr: mr["headPipeline"]["jobs"]["pageInfo"].update(hasNextPage=True),
    "reviewers": lambda mr: mr["reviewers"]["pageInfo"].update(hasNextPage=True),
    "approvers": lambda mr: mr["approvedBy"]["pageInfo"].update(hasNextPage=True),
    "labels": lambda mr: mr["labels"]["pageInfo"].update(hasNextPage=True),
}


@pytest.mark.parametrize("part", sorted(CUTS))
def test_a_cut_part_gives_the_mr_up(tmp_path: Path, part: str) -> None:
    got = read_with(tmp_path, CUTS[part])
    assert got.snapshots == {} and part in got.unresolved[ref()].message


@pytest.mark.parametrize("status", ["CHECKING", "UNCHECKED", "PREPARING", None])
def test_mergeability_not_known_gives_the_mr_up(tmp_path: Path, status: str | None) -> None:
    got = read_with(tmp_path, lambda mr: mr.update(detailedMergeStatus=status))
    assert ref() in got.unresolved


@pytest.mark.parametrize(
    "missing",
    [
        "diffHeadSha",
        "author",
        "headPipeline",
        "discussions",
        "reviewers",
        "sourceProjectId",
        "diffRefs",
        "approvalsLeft",
        "mergeUser",
        "mergeWhenPipelineSucceeds",
        "autoMergeEnabled",
    ],
)
def test_a_missing_field_gives_the_mr_up_never_a_crash(tmp_path: Path, missing: str) -> None:
    # an absent key is not a null: a null `headPipeline` is a fact (no pipeline), an absent one
    # is an answer that is not the one asked for
    got = read_with(tmp_path, lambda mr: mr.pop(missing))
    assert got.snapshots == {} and got.unresolved[ref()].code == BULK_UNAVAILABLE


@pytest.mark.parametrize("value", [None, 0, "true", "", 1])
def test_a_system_flag_that_is_not_a_boolean_gives_the_mr_up(tmp_path: Path, value: Any) -> None:
    def odd(mr: dict[str, Any]) -> None:
        mr["discussions"]["nodes"][0]["notes"]["nodes"][0]["system"] = value

    got = read_with(tmp_path, odd)
    assert got.snapshots == {} and got.unresolved[ref()].code == BULK_UNAVAILABLE


@pytest.mark.parametrize("iid", ["\u00b2", "\u0663", "9" * 5000, ""])
def test_an_iid_that_is_not_ascii_digits_never_crashes(tmp_path: Path, iid: str) -> None:
    odd = mr_node(8)
    odd["iid"] = iid
    fake = GraphFake([odd, mr_node(7)])
    got = read(fake, tmp_path, [ref(number=7), ref(number=8)])
    assert set(got.snapshots) == {ref(number=7), ref(number=8)}  # the lookup read both


def test_a_rest_read_that_hits_a_rate_limit_ends_the_run(tmp_path: Path) -> None:
    fake = GraphFake([mr_node(n) for n in range(1, 7)])
    routed(fake)
    fake.on(
        "projects/101",
        {"message": "429"},
        status=429,
        headers={"retry-after": "3600"},
    )
    refs = [ref(number=n) for n in range(1, 7)]
    got = read_bulk(env_of(fake, tmp_path / "x"), HOST, refs, now=lambda: NOW)
    assert got.stopped is not None and got.stopped.code == "rate_limited"
    assert {u.code for u in got.unresolved.values()} == {"rate_limited"} and len(
        got.unresolved
    ) == 6
    assert fake.count("projects/101") == 1  # nothing asked after the limit
    assert fake.kinds() == ["probe", "list"]


def test_a_node_that_names_no_project_is_not_wanted_and_is_looked_up(tmp_path: Path) -> None:
    got = read_with(tmp_path, lambda mr: mr.pop("project"))
    assert ref() in got.snapshots  # the lookup answered it (the default lookup answer)


def test_a_missing_mr_is_not_found_and_the_rest_are_read(tmp_path: Path) -> None:
    fake = GraphFake(authored=[mr_node(7)])

    def lookup(request: dict[str, Any]) -> Any:
        doc = lookup_answer(request)
        doc["data"]["m0"] = {"mergeRequest": None}
        return doc

    fake.lookup = lookup
    got = read(fake, tmp_path, [ref(number=7), ref(number=8)])
    assert set(got.snapshots) == {ref(number=7)}
    assert got.unresolved[ref(number=8)].code == "not_found"


def test_an_unknown_project_is_not_found(tmp_path: Path) -> None:
    fake = GraphFake(authored=[])

    def lookup(request: dict[str, Any]) -> Any:
        doc = lookup_answer(request)
        doc["data"]["m0"] = None
        return doc

    fake.lookup = lookup
    assert read(fake, tmp_path).unresolved[ref()].code == "not_found"


def test_an_answer_for_another_host_ends_the_run(tmp_path: Path) -> None:
    got = read_with(
        tmp_path,
        lambda mr: mr.update(webUrl="https://evil.example.test/user-9/proj/-/merge_requests/7"),
    )
    assert got.stopped is not None and got.stopped.code == "wrong_host_response"


def test_another_iid_in_a_lookup_answer_gives_the_mr_up(tmp_path: Path) -> None:
    fake = GraphFake(authored=[])

    def lookup(request: dict[str, Any]) -> Any:
        doc = lookup_answer(request)
        doc["data"]["m0"]["mergeRequest"]["iid"] = "8"
        return doc

    fake.lookup = lookup
    assert ref() in read(fake, tmp_path).unresolved


def test_no_pipeline_is_ci_none(tmp_path: Path) -> None:
    got = read_with(tmp_path, lambda mr: mr.update(headPipeline=None))
    assert got.snapshots[ref()].to_dict()["ci"]["state"] == "none"


def test_a_trigger_job_is_a_child_check_from_its_downstream_pipeline(tmp_path: Path) -> None:
    def child(mr: dict[str, Any]) -> None:
        mr["headPipeline"]["jobs"]["nodes"].append(
            {
                "id": gid("Ci::Bridge", 7100),
                "name": "deploy",
                "kind": "BRIDGE",
                "status": "RUNNING",
                "allowFailure": False,
                "finishedAt": None,
                "detailedStatus": {"detailsPath": "/user-9/proj/-/pipelines/5002"},
                "downstreamPipeline": {"status": "RUNNING", "path": "/user-9/other/-/pipelines/9"},
            }
        )

    ci = read_with(tmp_path, child).snapshots[ref()].to_dict()["ci"]
    names = {c["name"]: c["state"] for c in ci["checks"]}
    assert names["(child) deploy"] == "pending" and ci["state"] == "pending"


def test_a_failed_pipeline_is_a_failing_pipeline_check(tmp_path: Path) -> None:
    def failed(mr: dict[str, Any]) -> None:
        mr["headPipeline"]["status"] = "FAILED"
        mr["headPipeline"]["jobs"]["nodes"][0]["status"] = "FAILED"

    ci = read_with(tmp_path, failed).snapshots[ref()].to_dict()["ci"]
    assert ci["state"] == "failing"
    assert {"(pipeline)", "build"} <= {c["name"] for c in ci["checks"]}


def test_a_status_from_outside_gitlab_ci_is_not_in_the_checks(tmp_path: Path) -> None:
    # the documented gap: GraphQL lists jobs only, so an external commit status is absent
    ci = read(GraphFake(), tmp_path).snapshots[ref()].to_dict()["ci"]
    assert {c["name"] for c in ci["checks"]} == {"build", "test", "lint"}


# -- hostile content ------------------------------------------------------------------------------


def test_untrusted_text_is_sanitized_as_in_the_per_pr_path(tmp_path: Path) -> None:
    def hostile(mr: dict[str, Any]) -> None:
        mr["title"] = "Fix \u202eevil\u202c\x1b[31m title\x00 \U0001f600 \u00e9"
        mr["description"] = "body\x07 </script> ${jndi:ldap://x}"
        mr["discussions"]["nodes"][0]["notes"]["nodes"][0]["body"] = "\u2028x\u2029 \x1b]0;t\x07"

    snap = read_with(tmp_path, hostile).snapshots[ref()].to_dict()
    assert "\u202e" not in snap["title"] and "\x1b" not in snap["title"]
    assert "\U0001f600" in snap["title"] and "\u00e9" in snap["title"]
    assert "\x07" not in snap["body"]
    json.dumps(snap, ensure_ascii=True)


def test_an_unusable_login_gives_the_mr_up(tmp_path: Path) -> None:
    got = read_with(tmp_path, lambda mr: mr["author"].update(username="bad name; rm -rf"))
    assert got.snapshots == {} and ref() in got.unresolved


def test_the_path_of_a_list_node_matches_without_regard_to_case(tmp_path: Path) -> None:
    node = mr_node(7)
    node["project"]["fullPath"] = "USER-9/Proj"
    got = read(GraphFake([node]), tmp_path)
    assert ref() in got.snapshots and got.unresolved == {}


# -- what is read once per project, and the approvals rules ---------------------------------------


def with_reset(fake: FakeGitLab) -> None:
    fake.on("projects/101/approvals", {"reset_approvals_on_push": True})


def routed(fake: FakeGitLab) -> None:
    standard(fake)
    fake.on("user", recording("user"))
    for account in (12, 13):
        fake.on(
            f"users/{account}",
            {**recording("user"), "id": account, "username": f"user-{account - 10}"},
        )


def test_with_reset_approvals_on_push_the_item_equals_the_per_pr_snapshot(tmp_path: Path) -> None:
    per_fake = GraphFake()
    per_rig = Rig(per_fake, tmp_path / "a")
    with_reset(per_fake)
    per_pr = per_rig.snapshot()
    fake = GraphFake()
    routed(fake)
    with_reset(fake)
    got = read_bulk(env_of(fake, tmp_path / "b"), HOST, [ref()], now=lambda: NOW)
    bulk = got.snapshots[ref()].to_dict()
    assert bulk["approvals_at_head"] == per_pr["approvals_at_head"] == 1
    assert neutral(bulk) == neutral(per_pr)
    assert [e["can_push"] for e in bulk["latest_by_reviewer"]] == [True]


def test_an_account_is_looked_up_once_for_many_mrs(tmp_path: Path) -> None:
    nodes = [mr_node(n) for n in (7, 8, 9)]
    for node in nodes:
        node["author"]["bot"] = None
    got = read(GraphFake(nodes), tmp_path, [ref(number=n) for n in (7, 8, 9)])
    assert all(s.pr.author.is_bot is False for s in got.snapshots.values())
    assert got.snapshots  # read through the lists
    fake = GraphFake(nodes)
    read(fake, tmp_path / "again", [ref(number=n) for n in (7, 8, 9)])
    assert fake.count("users/11") == 1


def test_a_fork_mr_names_its_head_project(tmp_path: Path) -> None:
    got = read_with(tmp_path, lambda mr: mr.update(sourceProjectId=202))
    snap = got.snapshots[ref()].to_dict()
    assert snap["head"]["is_fork"] is True and snap["head"]["repo"] is not None


def test_a_project_the_token_cannot_read_gives_the_mr_up(tmp_path: Path) -> None:
    fake = GraphFake()
    routed(fake)
    fake.on("projects/101", {"message": "403 Forbidden"}, status=403)
    got = read_bulk(env_of(fake, tmp_path / "x"), HOST, [ref()], now=lambda: NOW)
    assert got.snapshots == {} and got.unresolved[ref()].code == BULK_UNAVAILABLE


def test_a_rate_limit_ends_the_run_and_makes_no_further_request(tmp_path: Path) -> None:
    fake = GraphFake(authored=[])
    fake.lookup = lambda r: b"HTTP/2.0 429 Too Many Requests\r\nretry-after: 3600\r\n\r\n{}"
    got = read(fake, tmp_path, [ref(number=n) for n in range(1, 7)])
    assert got.stopped is not None and got.stopped.code == "rate_limited"
    assert len(got.unresolved) == 6 and fake.lookup_sizes() == [2]


# -- the command over this reader ----------------------------------------


def test_a_host_that_fails_the_probe_is_read_by_the_per_pr_snapshot(tmp_path: Path) -> None:
    from forgectl import bulk

    fake = GraphFake()
    fake.probe = rejected("Field 'queryComplexity' doesn't exist on type 'Query'")
    Rig(fake, tmp_path / "rig")  # routes the REST recordings of the per-PR path
    out = bulk.run_snapshots(env_of(fake, tmp_path / "env"), [ref()])
    assert out["fallback"] == ["gitlab.com/user-9/proj#7"] and out["failed"] == []
    assert out["items"][0]["number"] == 7 and out["complete"] is True
    schemas.validate_cli_output("snapshots", out)


def test_bulk_only_lists_the_mr_of_a_failed_probe_instead_of_reading_it(tmp_path: Path) -> None:
    from forgectl import bulk

    fake = GraphFake()
    fake.probe = rejected("Field 'detailedMergeStatus' doesn't exist on type 'MergeRequest'")
    Rig(fake, tmp_path / "rig")
    out = bulk.run_snapshots(env_of(fake, tmp_path / "env"), [ref()], bulk_only=True)
    assert out["items"] == [] and out["failed"][0]["error"]["code"] == BULK_UNAVAILABLE
    assert fake.count("projects/user-9%2Fproj/merge_requests/7") == 0


# -- lookup: one or a few MRs without walking the lists (#119) --------------------------------


def test_lookup_reads_the_mr_by_its_own_request_and_never_walks_the_lists(tmp_path: Path) -> None:
    fake = GraphFake(authored=[mr_node(7)])
    got = read(fake, tmp_path, [ref(number=7)], lookup=True)
    assert fake.kinds() == ["probe", "lookup"]
    assert list(got.snapshots) == [ref(number=7)] and got.unresolved == {}


def test_lookup_gives_the_same_snapshot_as_the_list_read(tmp_path: Path) -> None:
    by_list = read(GraphFake(authored=[mr_node(7)]), tmp_path, [ref(number=7)])
    by_lookup = read(GraphFake(authored=[]), tmp_path, [ref(number=7)], lookup=True)
    wanted = by_list.snapshots[ref(number=7)].to_dict()
    got = by_lookup.snapshots[ref(number=7)].to_dict()
    assert comparable(got) == comparable(wanted)


def test_lookup_of_a_missing_mr_is_not_found(tmp_path: Path) -> None:
    fake = GraphFake(authored=[])
    fake.lookup = lambda request: {"data": {"m0": None}}
    got = read(fake, tmp_path, [ref(number=7)], lookup=True)
    assert got.snapshots == {} and got.unresolved[ref(number=7)].code == "not_found"
    assert "list" not in fake.kinds()
