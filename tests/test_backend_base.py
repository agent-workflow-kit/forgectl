"""The backend protocol (issue #10): both adapters must implement every read."""

from __future__ import annotations

from typing import get_type_hints

from forgectl import model as m
from forgectl.backends.base import READ_METHODS, WRITE_METHODS, Backend
from forgectl.identifiers import PrRef, RepoRef


class _Full:
    name = "github"

    def list_prs(self, query: m.PrQuery) -> m.PrPage: ...
    def get_pr(self, ref: PrRef, readiness: m.Readiness | None = None) -> m.PrRead: ...
    def snapshot(self, ref: PrRef) -> m.Snapshot: ...
    def diff(self, ref: PrRef) -> m.DiffResult: ...
    def compare(
        self, ref: PrRef, from_sha: str, to_sha: str, *, no_diff: bool = False
    ) -> m.CompareResult: ...
    def files(self, ref: PrRef) -> m.FilesResult: ...
    def comments(self, ref: PrRef) -> m.CommentsResult: ...
    def reviews(self, ref: PrRef) -> m.ReviewsResult: ...
    def ci(self, ref: PrRef) -> m.CiResult: ...
    def access(self, repo: RepoRef, user: str) -> m.AccessResult: ...
    def whoami(self, host: str) -> m.Identity: ...
    def merge(
        self, pr: m.PR, *, sha: str, method: str | None, auto: bool = False
    ) -> m.MergeAnswer: ...


class _NoCompare:
    name = "gitlab"

    def get_pr(self, ref: PrRef) -> m.PrRead: ...


def test_a_full_adapter_satisfies_the_protocol() -> None:
    assert isinstance(_Full(), Backend)


def test_a_partial_adapter_does_not() -> None:
    assert not isinstance(_NoCompare(), Backend)


def test_read_methods_list_matches_the_protocol() -> None:
    assert set(READ_METHODS) | set(WRITE_METHODS) == {
        n for n in vars(_Full) if not n.startswith("_") and n != "name"
    }


def test_get_pr_returns_the_pr_and_its_ci_from_one_read() -> None:
    assert get_type_hints(Backend.get_pr)["return"] is m.PrRead
    assert [f for f in m.PrRead.__dataclass_fields__] == ["pr", "ci", "latest_by_reviewer"]
