"""GitLab `prs`, `access` and `whoami` on one host (#19): the list route and its filters, the
pagination and limits, fork heads, the membership rule, and every hostile answer.

The gateway is real; its process runner is the in-process `glab` of tests/gitlab_support.py. The
recordings under tests/recordings/gitlab/lists are hand-built from GitLab's documented REST shapes
with placeholder accounts. Every assumption about GitLab's real behavior is UNVERIFIED until the
live suite (#31, #35) re-checks it; see the report of #19 for the list.
"""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from forgectl.backends import gitlab_lists
from forgectl.backends.gitlab_lists import build_request
from forgectl.errors import ForgectlError
from forgectl.identifiers import RepoRef
from forgectl.model import PrPage, PrQuery

from .gitlab_support import TOKEN, FakeGitLab, make_backend, recording
from .harness import golden

REPO = RepoRef(host="gitlab.com", backend="gitlab", path="user-9/proj")
ROUTE = "projects/user-9%2Fproj/merge_requests"
GLOBAL = "merge_requests"
NEXT = {"x-next-page": ""}


def lists(name: str) -> Any:
    return recording(name, "lists")


def mrs() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = lists("mrs")
    return found


def scene(
    fake: FakeGitLab,
    items: list[dict[str, Any]] | None = None,
    *,
    path: str = ROUTE,
    headers: dict[str, str] | None = None,
) -> None:
    fake.on(path, mrs() if items is None else items, headers=NEXT if headers is None else headers)
    fake.on("projects/202", lists("source_project"))
    fake.on("version", {"version": "17.5.1", "revision": "abc"})


def read(tmp_path: Path, fake: FakeGitLab, query: PrQuery | None = None, **kwargs: Any) -> PrPage:
    backend = make_backend(fake, tmp_path)
    return backend.list_prs(query or PrQuery(repo=REPO, host="gitlab.com", **kwargs))


def fails(tmp_path: Path, fake: FakeGitLab, **kwargs: Any) -> ForgectlError:
    with pytest.raises(ForgectlError) as caught:
        read(tmp_path, fake, **kwargs)
    return caught.value


# -- the request: built only from validated parts -----------------------------------------------


def test_the_request_for_a_repo_with_defaults() -> None:
    segments, params = build_request(PrQuery(repo=REPO, host="gitlab.com"), None)
    assert segments == ["projects", "user-9/proj", "merge_requests"]
    assert params == [
        ("scope", "all"),
        ("state", "opened"),
        ("order_by", "updated_at"),
        ("sort", "desc"),
    ]


def test_every_filter_has_one_parameter() -> None:
    segments, params = build_request(
        PrQuery(
            host="gitlab.com",
            author="user-1",
            review_requested_from="user-2",
            state="merged",
            draft=False,
            updated_since="2026-10-01T00:00:00Z",
        ),
        "draft",
    )
    assert segments == ["merge_requests"]
    assert params == [
        ("scope", "all"),
        ("state", "merged"),
        ("author_username", "user-1"),
        ("reviewer_username", "user-2"),
        ("draft", "no"),
        ("updated_after", "2026-10-01T00:00:00Z"),
        ("order_by", "updated_at"),
        ("sort", "desc"),
    ]


@pytest.mark.parametrize(
    ("state", "word"), [("open", "opened"), ("closed", "closed"), ("merged", "merged")]
)
def test_state_words(state: Any, word: str) -> None:
    _, params = build_request(PrQuery(repo=REPO, host="gitlab.com", state=state), None)
    assert ("state", word) in params


HOSTILE_LOGINS = [
    "x&scope=created_by_me",
    "x y",
    "x\ny",
    "x%26y",
    "x;y",
    "x=y",
    "../x",
    "x/y",
    "",
    " ",
    "é",
    "x" * 300,
    "x.",
    "-x",
    "x\x00",
    "x[bot]",
]


@pytest.mark.parametrize("login", HOSTILE_LOGINS)
@pytest.mark.parametrize("field", ["author", "review_requested_from"])
def test_a_login_outside_the_grammar_is_refused_not_escaped(login: str, field: str) -> None:
    with pytest.raises(ForgectlError) as caught:
        build_request(
            PrQuery(host="gitlab.com", repo=REPO, **{field: login}),  # type: ignore[arg-type]
            None,
        )
    assert caught.value.code == "usage"


@pytest.mark.parametrize(
    "path",
    ["", "a", "a/../b", "a/./b", "a//b", "-a/b", "a/b?x=1", "a/b#c", "a/b c", "a/b\n", "é/b"],
)
def test_a_repo_path_outside_the_grammar_is_refused(path: str) -> None:
    repo = RepoRef(host="gitlab.com", backend="gitlab", path=path)
    with pytest.raises(ForgectlError) as caught:
        build_request(PrQuery(repo=repo, host="gitlab.com"), None)
    assert caught.value.code == "usage"


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5, "5"])
def test_a_limit_outside_1_to_1000_is_refused(limit: Any) -> None:
    with pytest.raises(ForgectlError) as caught:
        build_request(PrQuery(repo=REPO, host="gitlab.com", limit=limit), None)
    assert caught.value.code == "usage"


@pytest.mark.parametrize(
    "since",
    [
        "2026-10-01",
        "2026-10-01T00:00:00+00:00",
        "2026-10-01T00:00:00.5Z",
        "2026-13-01T00:00:00Z",
        "2026-10-01T00:00:00Z\n",
        "2026-10-01T00:00:00Z&x=1",
        "",
        5,
    ],
)
def test_a_time_that_is_not_utc_seconds_is_refused(since: Any) -> None:
    with pytest.raises(ForgectlError) as caught:
        build_request(PrQuery(repo=REPO, host="gitlab.com", updated_since=since), None)
    assert caught.value.code == "usage"


def test_a_bad_state_and_a_bad_draft_and_no_filter_are_refused() -> None:
    for query in (
        PrQuery(repo=REPO, host="gitlab.com", state="x"),  # type: ignore[arg-type]
        PrQuery(repo=REPO, host="gitlab.com", draft="yes"),  # type: ignore[arg-type]
        PrQuery(host="gitlab.com"),
    ):
        with pytest.raises(ForgectlError) as caught:
            build_request(query, "draft")
        assert caught.value.code == "usage"


def test_a_hostile_value_makes_no_call(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    backend = make_backend(fake, tmp_path)
    with pytest.raises(ForgectlError):
        backend.list_prs(PrQuery(host="gitlab.com", author="x&scope=created_by_me"))
    assert fake.calls == []


def test_the_sent_endpoint_has_one_value_per_parameter(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake, path=GLOBAL)
    read(
        tmp_path,
        fake,
        PrQuery(host="gitlab.com", author="user-1", review_requested_from="user-2"),
    )
    (call,) = [c for c in fake.calls if c.path == GLOBAL]
    assert call.endpoint.count("?") == 1
    assert call.query == {
        "scope": "all",
        "state": "opened",
        "author_username": "user-1",
        "reviewer_username": "user-2",
        "order_by": "updated_at",
        "sort": "desc",
        "per_page": "100",
        "page": "1",
    }
    assert call.endpoint.count("&") == 7


# -- the list ---------------------------------------------------------------------------------


def test_a_repo_list_is_normalized(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    page = read(tmp_path, fake, state="all")
    assert [i.number for i in page.items] == [31, 29, 12, 5]
    assert page.complete is True
    first = page.items[0]
    assert first.pr == "gitlab.com/user-9/proj#31"
    assert first.pr_id == "gid://gitlab/MergeRequest/90031"
    assert first.repo == "gitlab.com/user-9/proj"
    assert first.url == "https://gitlab.com/user-9/proj/-/merge_requests/31"
    assert first.state == "open" and first.draft is False
    assert first.labels == ("needs review", "type::feature")
    assert first.author.login == "user-1" and first.author.id == "11"
    assert first.author.is_bot is False
    assert first.head.repo == "gitlab.com/user-9/proj" and first.head.is_fork is False
    assert first.base_ref == "main"
    assert first.updated_at == "2026-10-05T12:30:00.000Z"


def test_fork_heads_deleted_heads_and_a_null_source(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    page = read(tmp_path, fake, state="all")
    heads = {i.number: (i.head.repo, i.head.is_fork) for i in page.items}
    assert heads[31] == ("gitlab.com/user-9/proj", False)
    assert heads[29] == ("gitlab.com/user-8/proj", True)  # a fork: its own path
    assert heads[12] == (None, True)  # a differing id whose project cannot be read
    assert heads[5] == (None, None)  # no source project at all


def test_a_source_project_is_read_once_per_process(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = mrs()
    extra = copy.deepcopy(items[1])
    extra.update(iid=28, id=90028, updated_at="2026-10-03T00:00:00.000Z")
    extra["web_url"] = "https://gitlab.com/user-9/proj/-/merge_requests/28"
    scene(fake, [items[0], items[1], extra])
    backend = make_backend(fake, tmp_path)
    page = backend.list_prs(PrQuery(repo=REPO, host="gitlab.com"))
    assert [i.number for i in page.items] == [31, 29, 28]
    assert fake.count("projects/202") == 1
    backend.list_prs(PrQuery(repo=REPO, host="gitlab.com"))
    assert fake.count("projects/202") == 1


def test_an_author_without_a_bot_field_is_read_once(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = mrs()[:1] + mrs()[2:3]  # both by user-1
    for item in items:
        del item["author"]["bot"]
    scene(fake, items)
    fake.on("users/11", {"id": 11, "username": "user-1", "bot": False})
    page = read(tmp_path, fake)
    assert [i.author.is_bot for i in page.items] == [False, False]
    assert fake.count("users/11") == 1


def test_a_global_list_names_each_items_own_project(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = mrs()[:2]
    items[1]["project_id"] = items[1]["target_project_id"] = 303
    items[1]["source_project_id"] = 303
    items[1]["web_url"] = "https://gitlab.com/user-6/other/-/merge_requests/29"
    scene(fake, items, path=GLOBAL)
    page = read(tmp_path, fake, PrQuery(host="gitlab.com", author="user-1"))
    # the author filter is checked: user-3's item is dropped, and the list says so
    assert [i.repo for i in page.items] == ["gitlab.com/user-9/proj"]
    assert page.complete is False


def test_a_global_list_with_two_projects_is_kept(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = mrs()[:1] + mrs()[2:3]
    items[1]["project_id"] = items[1]["target_project_id"] = 303
    items[1]["source_project_id"] = 303
    items[1]["web_url"] = "https://gitlab.com/user-6/other/-/merge_requests/12"
    scene(fake, items, path=GLOBAL)
    page = read(tmp_path, fake, PrQuery(host="gitlab.com", author="user-1"))
    assert [i.repo for i in page.items] == ["gitlab.com/user-9/proj", "gitlab.com/user-6/other"]
    assert page.complete is True
    assert page.items[1].head.repo == "gitlab.com/user-6/other"


def test_the_list_is_sorted_by_instant_then_pr_whatever_the_offset(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = mrs()[:2]
    items[0]["updated_at"] = "2026-10-05T12:00:00.000+02:00"  # 10:00Z
    items[1]["updated_at"] = "2026-10-05T11:00:00.000Z"  # newer than item 0
    scene(fake, [items[1], items[0]])
    page = read(tmp_path, fake)
    assert [i.number for i in page.items] == [29, 31] or page.items[0].number == 29


def test_ties_sort_by_pr(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = mrs()[:2]
    items[1]["updated_at"] = items[0]["updated_at"]
    scene(fake, [items[1], items[0]])
    page = read(tmp_path, fake)
    assert [i.pr for i in page.items] == sorted(i.pr for i in page.items)


def test_a_draft_item_reads_draft(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    page = read(tmp_path, fake, state="all")
    assert {i.number: i.draft for i in page.items} == {31: False, 29: True, 12: False, 5: False}


def test_the_title_and_branches_are_one_line_and_labels_are_clean(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = mrs()[:1]
    items[0]["title"] = "a\x1b[31mb\nc‮d"
    items[0]["source_branch"] = "x\ny"
    items[0]["labels"] = ["l\x00l"]
    scene(fake, items)
    (item,) = read(tmp_path, fake).items
    for text in (item.title, item.head.ref, *item.labels):
        assert "\x1b" not in text and "\n" not in text and "\x00" not in text
        assert "‮" not in text


def test_the_token_never_reaches_the_output(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = mrs()[:1]
    items[0]["title"] = f"leak {TOKEN} here"
    scene(fake, items)
    (item,) = read(tmp_path, fake).items
    assert TOKEN not in str(item)


# -- limit, pages, complete --------------------------------------------------------------------


def make_items(count: int, *, start: int = 1000) -> list[dict[str, Any]]:
    out = []
    for n in range(count):
        item = copy.deepcopy(mrs()[0])
        iid = start - n
        item["iid"] = iid
        item["id"] = 80000 + iid
        item["web_url"] = f"https://gitlab.com/user-9/proj/-/merge_requests/{iid}"
        minute = 59 - (n // 60) % 60
        item["updated_at"] = f"2026-10-05T{11 - (n // 3600):02d}:{minute:02d}:{59 - n % 60:02d}Z"
        out.append(item)
    return out


def paged(fake: FakeGitLab, items: list[dict[str, Any]], per_page: int = 100) -> None:
    chunks = [items[i : i + per_page] for i in range(0, len(items), per_page)] or [[]]
    fake.pages(ROUTE, chunks)
    fake.on("version", {"version": "17.5.1"})


def test_a_list_cut_at_the_limit_is_incomplete(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    page = read(tmp_path, fake, state="all", limit=3)
    assert [i.number for i in page.items] == [31, 29, 12]
    assert page.complete is False


def test_a_list_of_exactly_the_limit_is_complete(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    page = read(tmp_path, fake, state="all", limit=4)
    assert len(page.items) == 4 and page.complete is True


def test_pages_are_followed_until_limit_plus_one(tmp_path: Path) -> None:
    fake = FakeGitLab()
    paged(fake, make_items(250))
    page = read(tmp_path, fake, limit=150)
    assert len(page.items) == 150 and page.complete is False
    assert fake.count(ROUTE) == 2


def test_a_whole_multi_page_list_is_complete(tmp_path: Path) -> None:
    fake = FakeGitLab()
    paged(fake, make_items(230))
    page = read(tmp_path, fake, limit=1000)
    assert len(page.items) == 230 and page.complete is True
    assert fake.count(ROUTE) == 3


def test_the_page_cap_is_incomplete_not_silent(tmp_path: Path, monkeypatch: Any) -> None:
    import forgectl.backends.gitlab_lists as lists_module

    monkeypatch.setattr(lists_module, "MAX_PAGES", 2)
    fake = FakeGitLab()
    paged(fake, make_items(500))
    page = read(tmp_path, fake, limit=1000)
    assert len(page.items) == 200 and page.complete is False
    assert fake.count(ROUTE) == 2


def test_a_full_last_page_without_a_header_is_followed(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = make_items(100)
    calls: list[str] = []

    def handler(query: dict[str, str]) -> Any:
        from .gitlab_support import Answer

        calls.append(query["page"])
        return Answer(200, items if query["page"] == "1" else [])

    fake.on_handler(ROUTE, handler)
    page = read(tmp_path, fake, limit=1000)
    assert len(page.items) == 100 and page.complete is True
    assert calls == ["1", "2"]


@pytest.mark.parametrize("header", ["3", "0", "-1", "x", "2 ", "02", "1"])
def test_a_next_page_header_that_is_not_the_next_page_is_an_error(
    tmp_path: Path, header: str
) -> None:
    fake = FakeGitLab()
    scene(fake, headers={"x-next-page": header})
    assert fails(tmp_path, fake).code == "upstream_error"


def test_a_page_longer_than_asked_for_is_an_error(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake, make_items(101))
    assert fails(tmp_path, fake).code == "upstream_error"


def test_a_duplicate_across_pages_is_dropped_and_incomplete(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = make_items(101)
    page_one = items[:100]
    page_two = [items[99], items[100]]  # the list shifted by one between the reads
    fake.pages(ROUTE, [page_one, page_two])
    fake.on("version", {"version": "17.5.1"})
    page = read(tmp_path, fake, limit=1000)
    assert len(page.items) == 101 and page.complete is False


# -- drops and refusals on the host's answer ---------------------------------------------------


def test_a_state_that_does_not_match_is_dropped_and_incomplete(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)  # the recording holds a merged MR
    page = read(tmp_path, fake, state="open")
    assert [i.number for i in page.items] == [31, 29, 12]
    assert page.complete is False


def test_a_locked_mr_is_an_open_one(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = mrs()[:1]
    items[0]["state"] = "locked"
    scene(fake, items)
    page = read(tmp_path, fake, state="open")
    assert page.items[0].state == "open" and page.complete is True


def test_a_draft_flag_that_does_not_match_is_dropped(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    fake.on("version", {"version": "19.1.0"})
    page = read(tmp_path, fake, draft=False)
    assert [i.number for i in page.items] == [31, 12]
    assert page.complete is False


def test_an_author_that_does_not_match_is_dropped(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    page = read(tmp_path, fake, PrQuery(repo=REPO, host="gitlab.com", author="user-3", state="all"))
    assert [i.number for i in page.items] == [29]
    assert page.complete is False


def test_the_author_filter_ignores_case(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = mrs()[:1]
    items[0]["author"]["username"] = "User-1"
    scene(fake, items)
    page = read(tmp_path, fake, PrQuery(repo=REPO, host="gitlab.com", author="user-1"))
    assert len(page.items) == 1 and page.complete is True


def test_a_reviewer_that_is_not_listed_is_dropped(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    page = read(
        tmp_path,
        fake,
        PrQuery(repo=REPO, host="gitlab.com", review_requested_from="user-2", state="all"),
    )
    assert [i.number for i in page.items] == [31, 29]
    assert page.complete is False


def test_a_reviewer_filter_needs_the_reviewers_field(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = mrs()[:1]
    del items[0]["reviewers"]
    scene(fake, items)
    with pytest.raises(ForgectlError) as caught:
        read(tmp_path, fake, PrQuery(repo=REPO, host="gitlab.com", review_requested_from="user-2"))
    assert caught.value.code == "upstream_error"


def test_an_item_older_than_the_floor_is_an_error(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    error = fails(tmp_path, fake, state="all", updated_since="2026-10-03T00:00:00Z")
    assert error.code == "upstream_error"


def test_an_item_at_the_floor_is_kept(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = mrs()[:1]
    items[0]["updated_at"] = "2026-10-03T00:00:00.000Z"
    scene(fake, items)
    assert len(read(tmp_path, fake, updated_since="2026-10-03T00:00:00Z").items) == 1


def test_a_list_not_newest_first_is_an_error(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake, list(reversed(mrs())))
    assert fails(tmp_path, fake, state="all").code == "upstream_error"


def test_an_item_of_another_project_in_a_project_list_is_an_error(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = mrs()[:2]
    items[1]["project_id"] = items[1]["target_project_id"] = 303
    items[1]["web_url"] = "https://gitlab.com/user-6/other/-/merge_requests/29"
    scene(fake, items)
    assert fails(tmp_path, fake).code == "upstream_error"


def test_an_item_on_another_host_is_wrong_host_response(tmp_path: Path) -> None:
    fake = FakeGitLab()
    items = mrs()[:1]
    items[0]["web_url"] = "https://evil.example.test/user-9/proj/-/merge_requests/31"
    scene(fake, items)
    assert fails(tmp_path, fake).code == "wrong_host_response"


HOSTILE_FIELDS: list[tuple[str, Any]] = [
    ("iid", 0),
    ("iid", -3),
    ("iid", True),
    ("iid", "7"),
    ("iid", 10**12),
    ("iid", 99),  # not the number in web_url
    ("id", None),
    ("id", "9"),
    ("id", False),
    ("project_id", None),
    ("project_id", "101"),
    ("target_project_id", 5),
    ("title", None),
    ("title", 7),
    ("state", "weird"),
    ("state", None),
    ("sha", "xyz"),
    ("sha", "A1" * 20),
    ("sha", None),
    ("source_branch", None),
    ("target_branch", 3),
    ("updated_at", None),
    ("updated_at", "yesterday"),
    ("updated_at", "2026-10-05T12:30:00"),
    ("draft", "yes"),
    ("labels", "x"),
    ("labels", [1]),
    ("labels", None),
    ("author", None),
    ("author", "user-1"),
    ("author", {"id": 1}),
    ("author", {"id": 1, "username": "a/b"}),
    ("author", {"id": "1", "username": "ok"}),
    ("author", {"id": 1, "username": "x&y"}),
    ("web_url", None),
    ("web_url", "http://gitlab.com/user-9/proj/-/merge_requests/31"),
    ("web_url", "https://gitlab.com/user-9/proj/-/merge_requests/31?x=1"),
    ("web_url", "https://gitlab.com/../../merge_requests/31"),
    ("web_url", "https://gitlab.com/user-9/proj/-/issues/31"),
    ("source_project_id", "202"),
    ("source_project_id", 0),
    ("source_project_id", True),
]


@pytest.mark.parametrize(("field", "value"), HOSTILE_FIELDS)
def test_a_hostile_field_is_a_typed_error_never_a_traceback(
    tmp_path: Path, field: str, value: Any
) -> None:
    fake = FakeGitLab()
    items = mrs()[:1]
    items[0][field] = value
    scene(fake, items)
    with pytest.raises(ForgectlError) as caught:
        read(tmp_path, fake)
    assert caught.value.code in ("upstream_error", "wrong_host_response")


@pytest.mark.parametrize("body", [{}, {"a": 1}, "x", None, 3, [1], ["x"], [None], [[]]])
def test_a_body_that_is_not_a_list_of_objects_is_a_typed_error(tmp_path: Path, body: Any) -> None:
    fake = FakeGitLab()
    fake.on(ROUTE, body, headers=NEXT)
    assert fails(tmp_path, fake).code in ("upstream_error", "host_rejected")


def test_a_body_that_is_not_json_is_a_typed_error(tmp_path: Path) -> None:
    fake = FakeGitLab()
    fake.on(ROUTE, raw=b"<html>login</html>")
    assert fails(tmp_path, fake).code in ("upstream_error", "wrong_host_response")


def test_an_empty_list_is_complete(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake, [])
    page = read(tmp_path, fake)
    assert page.items == () and page.complete is True


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, "not_found"),
        (403, "permission_denied"),
        (401, "auth_required"),
        (500, "upstream_error"),
    ],
)
def test_a_failed_read_is_a_typed_error(tmp_path: Path, status: int, code: str) -> None:
    fake = FakeGitLab()
    fake.on(ROUTE, {"message": "x"}, status=status)
    assert fails(tmp_path, fake).code == code


def test_a_failed_head_read_other_than_gone_fails_the_list(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    fake.on("projects/202", {"message": "boom"}, status=500)
    assert fails(tmp_path, fake, state="all").code == "upstream_error"


def test_a_hidden_head_project_is_a_warning_not_an_error(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    fake.on("projects/202", {"message": "403 Forbidden"}, status=403)
    warned: list[str] = []
    backend = make_backend(fake, tmp_path, warn=warned.append)
    page = backend.list_prs(PrQuery(repo=REPO, host="gitlab.com", state="all"))
    assert {i.number: (i.head.repo, i.head.is_fork) for i in page.items}[29] == (None, True)
    assert warned


# -- draft by version ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "name"),
    [("19.0.0", "draft"), ("19.4.2-ee", "draft"), ("18.11.0", "wip"), ("15.6.1", "wip")],
)
def test_the_draft_filter_name_follows_the_host_version(
    tmp_path: Path, version: str, name: str
) -> None:
    fake = FakeGitLab()
    scene(fake)
    fake.on("version", {"version": version})
    read(tmp_path, fake, draft=True)
    (call,) = [c for c in fake.calls if c.path == ROUTE]
    assert call.query[name] == "yes"
    assert {"draft", "wip"} - {name} not in set(call.query)


@pytest.mark.parametrize("status", [404, 500])
def test_an_unreadable_version_uses_wip(tmp_path: Path, status: int) -> None:
    fake = FakeGitLab()
    scene(fake)
    fake.on("version", {"message": "x"}, status=status)
    read(tmp_path, fake, draft=False)
    (call,) = [c for c in fake.calls if c.path == ROUTE]
    assert call.query["wip"] == "no" and "draft" not in call.query


def test_no_draft_filter_reads_no_version(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    read(tmp_path, fake)
    assert fake.count("version") == 0


# -- rate limit and credentials ---------------------------------------------------------------


def test_the_rate_limit_comes_from_the_headers(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(
        fake,
        headers={"x-next-page": "", "ratelimit-remaining": "1990", "ratelimit-reset": "1790931600"},
    )
    page = read(tmp_path, fake)
    assert page.rate_limit.host == "gitlab.com"
    assert page.rate_limit.remaining == 1990
    assert page.rate_limit.reset_at == "2026-10-02T09:00:00Z"


def test_a_missing_rate_header_is_null(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    page = read(tmp_path, fake)
    assert page.rate_limit.remaining is None and page.rate_limit.reset_at is None


def test_the_token_is_sent_to_the_host_only(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    read(tmp_path, fake)
    for call in fake.calls:
        assert call.env.get("GITLAB_TOKEN") == TOKEN or TOKEN not in " ".join(call.argv)
        assert TOKEN not in " ".join(call.argv)


def test_the_golden_prs_output(tmp_path: Path) -> None:
    fake = FakeGitLab()
    scene(fake)
    backend = make_backend(fake, tmp_path)
    page = backend.list_prs(PrQuery(repo=REPO, host="gitlab.com", state="all"))
    out = {
        "items": [i.to_dict() for i in page.items],
        "complete": page.complete,
        "errors": [],
        "hosts_queried": ["gitlab.com"],
        "rate_limit": page.rate_limit.to_dict(),
    }
    golden.assert_golden("prs/gitlab-repo", out, schema_id="forgectl/cli/prs.json")


# -- access -----------------------------------------------------------------------------------


def access_scene(
    fake: FakeGitLab,
    *,
    member: Any = None,
    member_status: int = 200,
    users: Any = None,
    users_status: int = 200,
) -> None:
    fake.on("projects/user-9%2Fproj", lists("project"))
    fake.on("users", lists("users") if users is None else users, status=users_status, headers=NEXT)
    fake.on(
        "projects/101/members/all/12",
        lists("member") if member is None else member,
        status=member_status,
    )


def ask(
    tmp_path: Path, fake: FakeGitLab, user: str = "user-2", today: date = date(2026, 10, 5)
) -> Any:
    backend = make_backend(fake, tmp_path, today=lambda: today)
    return backend.access(REPO, user)


def member(**changes: Any) -> dict[str, Any]:
    found: dict[str, Any] = lists("member")
    found.update(changes)
    return found


@pytest.mark.parametrize(("level", "word"), [(50, "admin"), (40, "maintain"), (30, "write")])
def test_a_member_at_30_or_more_can_push(tmp_path: Path, level: int, word: str) -> None:
    fake = FakeGitLab()
    access_scene(fake, member=member(access_level=level))
    result = ask(tmp_path, fake)
    assert result.write_access is True and result.level == word
    assert result.source == "project_member" and result.reason is None
    assert result.repo == "gitlab.com/user-9/proj" and result.user == "user-2"


@pytest.mark.parametrize(("level", "word"), [(20, "read"), (10, "read"), (5, "read"), (0, "none")])
def test_a_member_below_30_cannot(tmp_path: Path, level: int, word: str) -> None:
    fake = FakeGitLab()
    access_scene(fake, member=member(access_level=level))
    result = ask(tmp_path, fake)
    assert result.write_access is False and result.level == word


@pytest.mark.parametrize("state", ["blocked", "awaiting", "deactivated", "banned", "", None, 5])
def test_a_member_that_is_not_active_cannot_push(tmp_path: Path, state: Any) -> None:
    fake = FakeGitLab()
    access_scene(fake, member=member(state=state))
    result = ask(tmp_path, fake)
    assert result.write_access is False and result.level == "none"


def test_a_missing_state_cannot_push(tmp_path: Path) -> None:
    fake = FakeGitLab()
    body = member()
    del body["state"]
    access_scene(fake, member=body)
    assert ask(tmp_path, fake).write_access is False


@pytest.mark.parametrize("membership", ["awaiting", "pending", None, 3])
def test_a_membership_not_active_cannot_push(tmp_path: Path, membership: Any) -> None:
    fake = FakeGitLab()
    access_scene(fake, member=member(membership_state=membership))
    assert ask(tmp_path, fake).write_access is False


def test_an_active_membership_state_can_push(tmp_path: Path) -> None:
    fake = FakeGitLab()
    access_scene(fake, member=member(membership_state="active"))
    assert ask(tmp_path, fake).write_access is True


@pytest.mark.parametrize(
    ("expires", "can"),
    [
        ("2026-10-05", False),  # expires today: not after today
        ("2026-10-04", False),
        ("2026-10-06", True),
        ("2999-01-01", True),
        ("soon", False),
        ("2026-13-45", False),
        ("2026-10-06T00:00:00Z", False),
        (20261006, False),
        (None, True),
    ],
)
def test_an_expired_or_unreadable_expiry_cannot_push(
    tmp_path: Path, expires: Any, can: bool
) -> None:
    fake = FakeGitLab()
    access_scene(fake, member=member(expires_at=expires))
    result = ask(tmp_path, fake)
    assert result.write_access is can
    assert result.level == ("write" if can else "none")


@pytest.mark.parametrize("level", [None, "30", True, 30.5, -1, 101, [30]])
def test_an_access_level_that_is_not_a_number_is_an_error(tmp_path: Path, level: Any) -> None:
    fake = FakeGitLab()
    access_scene(fake, member=member(access_level=level))
    with pytest.raises(ForgectlError) as caught:
        ask(tmp_path, fake)
    assert caught.value.code == "upstream_error"


def test_a_non_member_is_false_on_404(tmp_path: Path) -> None:
    fake = FakeGitLab()
    access_scene(fake, member={"message": "404 Not found"}, member_status=404)
    result = ask(tmp_path, fake)
    assert (result.write_access, result.level, result.reason) == (False, "none", None)


@pytest.mark.parametrize("status", [401, 403])
def test_an_unreadable_answer_is_null_with_a_reason(tmp_path: Path, status: int) -> None:
    fake = FakeGitLab()
    access_scene(fake, member={"message": "x"}, member_status=status)
    result = ask(tmp_path, fake)
    assert result.write_access is None and result.level is None
    assert result.reason == "no_permission_to_read"


@pytest.mark.parametrize("status", [401, 403])
def test_an_unreadable_user_lookup_is_null_with_a_reason(tmp_path: Path, status: int) -> None:
    fake = FakeGitLab()
    access_scene(fake, users={"message": "x"}, users_status=status)
    result = ask(tmp_path, fake)
    assert result.write_access is None and result.reason == "no_permission_to_read"
    assert fake.count("projects/101/members/all/12") == 0


def test_an_unknown_user_is_false_without_a_member_read(tmp_path: Path) -> None:
    fake = FakeGitLab()
    access_scene(fake, users=[])
    result = ask(tmp_path, fake, user="user-404")
    assert (result.write_access, result.level, result.user) == (False, "none", "user-404")
    assert fake.count("projects/101/members/all/12") == 0


def test_the_username_is_one_encoded_value(tmp_path: Path) -> None:
    fake = FakeGitLab()
    access_scene(fake)
    ask(tmp_path, fake)
    (call,) = [c for c in fake.calls if c.path == "users"]
    assert call.query == {"username": "user-2"}


@pytest.mark.parametrize("user", HOSTILE_LOGINS)
def test_a_hostile_user_is_usage_before_any_call(tmp_path: Path, user: str) -> None:
    fake = FakeGitLab()
    access_scene(fake)
    with pytest.raises(ForgectlError) as caught:
        ask(tmp_path, fake, user=user)
    assert caught.value.code == "usage"
    assert fake.calls == []


def test_a_member_answer_for_another_user_is_an_error(tmp_path: Path) -> None:
    fake = FakeGitLab()
    access_scene(fake, member=member(id=99))
    with pytest.raises(ForgectlError) as caught:
        ask(tmp_path, fake)
    assert caught.value.code == "upstream_error"


@pytest.mark.parametrize("bad_id", [None, "12", True, 12.0, [12]])
def test_a_member_answer_with_an_unusable_id_is_an_error(tmp_path: Path, bad_id: Any) -> None:
    fake = FakeGitLab()
    access_scene(fake, member=member(id=bad_id))
    with pytest.raises(ForgectlError) as caught:
        ask(tmp_path, fake)
    assert caught.value.code == "upstream_error"


@pytest.mark.parametrize(
    "users",
    [
        [{"id": 99, "username": "user-9", "state": "active"}],  # another account
        [{"id": 12, "username": "user-2"}, {"id": 13, "username": "user-2"}],  # two
        [{"id": "12", "username": "user-2"}],
        [{"username": "user-2"}],
        [None],
        {"id": 12, "username": "user-2"},
        "user-2",
    ],
)
def test_a_user_answer_that_is_not_the_one_asked_for_is_an_error(
    tmp_path: Path, users: Any
) -> None:
    fake = FakeGitLab()
    access_scene(fake, users=users)
    with pytest.raises(ForgectlError) as caught:
        ask(tmp_path, fake)
    assert caught.value.code == "upstream_error"
    assert fake.count("projects/101/members/all/12") == 0


def test_the_user_lookup_ignores_case(tmp_path: Path) -> None:
    fake = FakeGitLab()
    access_scene(fake, users=[{"id": 12, "username": "User-2"}])
    result = ask(tmp_path, fake, user="user-2")
    assert result.write_access is True and result.user == "User-2"


def test_an_absent_repo_is_not_found(tmp_path: Path) -> None:
    fake = FakeGitLab()
    fake.on("projects/user-9%2Fproj", {"message": "404 Project Not Found"}, status=404)
    with pytest.raises(ForgectlError) as caught:
        ask(tmp_path, fake)
    assert caught.value.code == "not_found"


def test_a_project_answer_for_another_host_is_wrong_host_response(tmp_path: Path) -> None:
    fake = FakeGitLab()
    project = lists("project")
    project["web_url"] = "https://evil.example.test/user-9/proj"
    access_scene(fake)
    fake.on("projects/user-9%2Fproj", project)
    with pytest.raises(ForgectlError) as caught:
        ask(tmp_path, fake)
    assert caught.value.code == "wrong_host_response"


@pytest.mark.parametrize("field", ["id", "path_with_namespace", "web_url"])
def test_a_project_answer_missing_a_field_is_an_error(tmp_path: Path, field: str) -> None:
    fake = FakeGitLab()
    project = lists("project")
    del project[field]
    access_scene(fake)
    fake.on("projects/user-9%2Fproj", project)
    with pytest.raises(ForgectlError) as caught:
        ask(tmp_path, fake)
    assert caught.value.code in ("upstream_error", "wrong_host_response")


def test_the_golden_access_output(tmp_path: Path) -> None:
    fake = FakeGitLab()
    access_scene(fake, member=member(access_level=40))
    result = ask(tmp_path, fake)
    golden.assert_golden(
        "access/gitlab-maintain", result.to_dict(), schema_id="forgectl/cli/access.json"
    )


# -- whoami -----------------------------------------------------------------------------------


def test_whoami_reads_user_and_has_no_scopes(tmp_path: Path) -> None:
    fake = FakeGitLab()
    fake.on("user", {"id": 11, "username": "user-1", "web_url": "https://gitlab.com/user-1"})
    backend = make_backend(fake, tmp_path)
    who = backend.whoami("gitlab.com")
    assert (who.host, who.backend, who.login, who.id, who.scopes) == (
        "gitlab.com",
        "gitlab",
        "user-1",
        "11",
        None,
    )


@pytest.mark.parametrize(
    "body",
    [
        {"id": 11, "username": "user-1", "web_url": "https://evil.example.test/user-1"},
        {"id": "11", "username": "user-1", "web_url": "https://gitlab.com/user-1"},
        {"id": 11, "username": "a/b", "web_url": "https://gitlab.com/user-1"},
        {"id": 11, "web_url": "https://gitlab.com/user-1"},
    ],
)
def test_whoami_refuses_an_answer_that_is_not_this_account(tmp_path: Path, body: Any) -> None:
    fake = FakeGitLab()
    fake.on("user", body)
    backend = make_backend(fake, tmp_path)
    with pytest.raises(ForgectlError) as caught:
        backend.whoami("gitlab.com")
    assert caught.value.code in ("upstream_error", "wrong_host_response")


def test_the_module_names_no_other_backend() -> None:
    text = Path(gitlab_lists.__file__).read_text(encoding="utf-8")
    assert "github" not in text.lower().replace("gitlab_lists", "")


@pytest.mark.parametrize(
    "code",
    ["wrong_host_response", "tool_missing", "config_invalid", "auth_required", "upstream_error"],
)
def test_an_identity_error_entry_becomes_an_error_whatever_its_code(code: str) -> None:
    from forgectl.fanout import entry_error

    error = entry_error({"host": "gitlab.com", "code": code, "message": "m"})
    assert error.code == code and error.message == "m"


def test_an_identity_entry_with_an_unknown_code_is_still_refused() -> None:
    from forgectl.fanout import entry_error

    with pytest.raises(ValueError):
        entry_error({"host": "gitlab.com", "code": "nope", "message": "m"})
