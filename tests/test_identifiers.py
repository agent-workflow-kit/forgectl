"""Table tests for the identifier parsers (interface: Identifiers, Hosts)."""

from __future__ import annotations

import pytest

from forgectl.config import Config, Host
from forgectl.errors import ForgectlError
from forgectl.identifiers import (
    PrInput,
    PrRef,
    RepoRef,
    bind_host,
    bind_login,
    bind_pr,
    bind_repo,
    parse_branch,
    parse_host,
    parse_login,
    parse_pr,
    parse_repo,
    parse_sha,
    parse_typed_id,
)

CONFIG = Config(
    hosts={
        "git.example.test": Host(backend="gitlab"),
        "pre.example.test": Host(backend="gitlab", api_prefix="/gitlab"),
        "ghe.example.test": Host(backend="github"),
        "ghe.example.test:8443": Host(backend="github"),
    }
)


def code_of(call, *args) -> tuple[str, str | None]:  # type: ignore[no-untyped-def]
    with pytest.raises(ForgectlError) as caught:
        call(*args)
    return caught.value.code, caught.value.reason


# --- PR references that parse --------------------------------------------------------------

GOOD_PRS = [
    ("https://github.com/o/r/pull/7", "github.com/o/r#7"),
    ("https://github.com/o/r/pull/7/", "github.com/o/r#7"),
    ("https://github.com/o/r/pull/7/files", "github.com/o/r#7"),
    ("https://github.com/o/r/pull/7/commits", "github.com/o/r#7"),
    ("https://github.com/o/r/pull/7/commits/" + "a" * 40, "github.com/o/r#7"),
    ("https://github.com/o/r/pull/7/commits/abcdef1", "github.com/o/r#7"),
    ("https://github.com/o/r/pull/7/checks", "github.com/o/r#7"),
    ("https://github.com/o/r/pull/7.diff", "github.com/o/r#7"),
    ("https://github.com/o/r/pull/7.patch", "github.com/o/r#7"),
    ("https://github.com/o/r/pull/7?w=1&x=a.b_c~d-e", "github.com/o/r#7"),
    ("https://github.com/o/r/pull/7#issuecomment-1", "github.com/o/r#7"),
    ("https://github.com/o/r/pull/7/files?a=b#diff-ab", "github.com/o/r#7"),
    ("https://github.com/o/r/pull/7?#", "github.com/o/r#7"),
    ("https://GitHub.COM/o/R/pull/7", "github.com/o/R#7"),
    ("https://www.github.com/o/r/pull/7", "github.com/o/r#7"),
    ("https://github.com:443/o/r/pull/7", "github.com/o/r#7"),
    ("https://gitlab.com/g/p/-/merge_requests/3", "gitlab.com/g/p#3"),
    ("https://gitlab.com/g/p/merge_requests/3", "gitlab.com/g/p#3"),
    ("https://gitlab.com/g/s/p/-/merge_requests/3/diffs", "gitlab.com/g/s/p#3"),
    ("https://gitlab.com/g/p/-/merge_requests/3/commits", "gitlab.com/g/p#3"),
    ("https://gitlab.com/g/p/-/merge_requests/3/pipelines/", "gitlab.com/g/p#3"),
    ("https://gitlab.com/g/p/-/merge_requests/3?x=1#note_5", "gitlab.com/g/p#3"),
    ("https://www.gitlab.com/g/p/-/merge_requests/3", "gitlab.com/g/p#3"),
    ("https://git.example.test:8443/g/p/-/merge_requests/3", "git.example.test:8443/g/p#3"),
    ("https://git.example.test:443/g/p/-/merge_requests/3", "git.example.test/g/p#3"),
    ("github.com/o/r#7", "github.com/o/r#7"),
    ("GitHub.com/o/r#7", "github.com/o/r#7"),
    ("www.github.com/o/r#7", "github.com/o/r#7"),
    ("github.com:443/o/r#7", "github.com/o/r#7"),
    ("gitlab.com/g/s/p#4294967295", "gitlab.com/g/s/p#4294967295"),
    ("gitlab.com/g/-/p#4", "gitlab.com/g/-/p#4"),
    ("github.com/o/r#9999999999", "github.com/o/r#9999999999"),
    ("github.com/o.x/r-y_z.git#1", "github.com/o.x/r-y_z.git#1"),
]


@pytest.mark.parametrize(("text", "normalized"), GOOD_PRS)
def test_good_pr_references_normalize(text: str, normalized: str) -> None:
    assert str(parse_pr(text)) == normalized


# --- every URL trick ----------------------------------------------------------------------

BAD_PRS = [
    ("empty", ""),
    ("only space", " "),
    ("userinfo", "https://user@github.com/o/r/pull/7"),
    ("userinfo with password", "https://user:pw@github.com/o/r/pull/7"),
    ("at in path", "https://github.com/o@x/r/pull/7"),
    ("at in host-form", "github.com/o@x/r#7"),
    ("percent escape", "https://github.com/o/r%2Fx/pull/7"),
    ("percent in host", "https://github.com%2eevil.test/o/r/pull/7"),
    ("percent in query", "https://github.com/o/r/pull/7?a=%41"),
    ("backslash", "https://github.com\\@evil.test/o/r/pull/7"),
    ("backslash in path", "https://github.com/o\\r/pull/7"),
    ("backslash host-form", "github.com/o\\r#7"),
    ("non-ascii host", "https://gïthub.com/o/r/pull/7"),
    ("idn host", "https://münchen.example/o/r/pull/7"),
    ("cyrillic homoglyph host", "https://github.com/o/r/pull/7".replace("i", "і")),
    ("fullwidth host", "https://github．com/o/r/pull/7"),
    ("fullwidth digit number", "github.com/o/r#７"),
    ("arabic digit number", "github.com/o/r#٧"),
    ("non-ascii path", "https://github.com/ö/r/pull/7"),
    ("space", "https://github.com/o/r/pull/7 "),
    ("leading space", " https://github.com/o/r/pull/7"),
    ("inner space", "https://github.com/o /r/pull/7"),
    ("tab", "https://github.com/o/r/pull/7\t"),
    ("newline", "https://github.com/o/r/pull/7\n"),
    ("newline host-form", "github.com/o/r#7\n"),
    ("carriage return", "https://github.com/o/r/pull/7\r"),
    ("nul", "https://github.com/o/r/pull/7\x00"),
    ("escape", "https://github.com/o/r/pull/7\x1b[31m"),
    ("del", "https://github.com/o/r/pull/7\x7f"),
    ("nbsp", "https://github.com/o/r/pull/7 "),
    ("line separator", "github.com/o/r#7 "),
    ("lone surrogate", "github.com/o/r#7\ud800"),
    ("bidi override", "github.com/o/r#7‮"),
    ("traversal dotdot", "https://github.com/o/../pull/7"),
    ("traversal in repo", "https://github.com/o/../r/pull/7"),
    ("traversal dot", "https://github.com/o/./pull/7"),
    ("traversal host-form", "github.com/o/../r#7"),
    ("traversal dot host-form", "github.com/./r#7"),
    ("empty segment", "https://github.com/o//r/pull/7"),
    ("empty segment host-form", "github.com/o//r#7"),
    ("leading double slash", "https://github.com//o/r/pull/7"),
    ("non-integer number", "https://github.com/o/r/pull/7abc"),
    ("non-integer host-form", "github.com/o/r#7abc"),
    ("number then slash junk", "https://github.com/o/r/pull/7/x"),
    ("zero", "github.com/o/r#0"),
    ("zero url", "https://github.com/o/r/pull/0"),
    ("leading zero", "github.com/o/r#07"),
    ("negative", "github.com/o/r#-1"),
    ("negative url", "https://github.com/o/r/pull/-1"),
    ("plus sign", "github.com/o/r#+1"),
    ("float", "github.com/o/r#1.5"),
    ("hex", "github.com/o/r#0x10"),
    ("exponent", "github.com/o/r#1e3"),
    ("eleven digits", "github.com/o/r#10000000000"),
    ("huge", "github.com/o/r#" + "9" * 5000),
    ("empty number", "github.com/o/r#"),
    ("missing number", "github.com/o/r"),
    ("bang number", "gitlab.com/g/p#!3"),
    ("bang url", "https://gitlab.com/g/p/-/merge_requests/!3"),
    ("trailing dot host", "https://github.com./o/r/pull/7"),
    ("trailing dot host-form", "github.com./o/r#7"),
    ("leading dot host", ".github.com/o/r#7"),
    ("double dot host", "git..hub.com/o/r#7"),
    ("host dash start", "-github.com/o/r#7"),
    ("host dash end", "github-.com/o/r#7"),
    ("host underscore", "git_hub.com/o/r#7"),
    ("port zero", "https://github.com:0/o/r/pull/7"),
    ("port too large", "https://github.com:65536/o/r/pull/7"),
    ("port leading zero", "https://github.com:0443/o/r/pull/7"),
    ("port empty", "https://github.com:/o/r/pull/7"),
    ("port non-numeric", "https://github.com:ab/o/r/pull/7"),
    ("port six digits", "https://github.com:123456/o/r/pull/7"),
    ("two ports", "https://github.com:1:2/o/r/pull/7"),
    ("http scheme", "http://github.com/o/r/pull/7"),
    ("ftp scheme", "ftp://github.com/o/r/pull/7"),
    ("file scheme", "file:///etc/passwd"),
    ("javascript scheme", "javascript:alert(1)"),
    ("uppercase scheme", "HTTPS://github.com/o/r/pull/7"),
    ("scheme relative", "//github.com/o/r/pull/7"),
    ("single slash scheme", "https:/github.com/o/r/pull/7"),
    ("three slashes", "https:///github.com/o/r/pull/7"),
    ("no host", "https:///o/r/pull/7"),
    ("only host", "https://github.com"),
    ("only host slash", "https://github.com/"),
    ("wrong word", "https://github.com/o/r/pulls/7"),
    ("issues", "https://github.com/o/r/issues/7"),
    ("bad suffix", "https://github.com/o/r/pull/7/reviews"),
    ("gitlab suffix on github", "https://github.com/o/r/pull/7/diffs"),
    ("github suffix on gitlab", "https://gitlab.com/g/p/-/merge_requests/3/files"),
    ("diff then slash", "https://github.com/o/r/pull/7.diff/"),
    ("diff twice", "https://github.com/o/r/pull/7.diff.diff"),
    ("commits bad sha", "https://github.com/o/r/pull/7/commits/xyz"),
    ("commits short sha", "https://github.com/o/r/pull/7/commits/abc"),
    ("commits long sha", "https://github.com/o/r/pull/7/commits/" + "a" * 41),
    ("query bad char", "https://github.com/o/r/pull/7?a=b c"),
    ("query slash", "https://github.com/o/r/pull/7?a=/"),
    ("query semicolon", "https://github.com/o/r/pull/7?a=b;c"),
    ("query colon", "https://github.com/o/r/pull/7?a=b:c"),
    ("query plus", "https://github.com/o/r/pull/7?a=b+c"),
    ("query question", "https://github.com/o/r/pull/7?a?b"),
    ("fragment bad char", "https://github.com/o/r/pull/7#a/b"),
    ("fragment percent", "https://github.com/o/r/pull/7#%41"),
    ("fragment hash", "https://github.com/o/r/pull/7#a#b"),
    ("fragment before query", "https://github.com/o/r/pull/7#a?b=c"),
    ("host-form fragment extra", "github.com/o/r#7#8"),
    ("host-form query", "github.com/o/r?x=1#7"),
    ("host-form scheme and hash", "https://github.com/o/r#7"),
    ("dash project segment", "https://gitlab.com/g/-/p/-/merge_requests/3"),
    ("merge_requests project segment", "https://gitlab.com/g/merge_requests/p/-/merge_requests/3"),
    ("dash project segment github", "https://github.com/-/r/pull/7"),
    ("only dash project", "https://gitlab.com/-/merge_requests/3"),
    ("empty project", "https://gitlab.com/merge_requests/3"),
    ("angle brackets", "https://github.com/<o>/r/pull/7"),
    ("quote", 'https://github.com/o/r"/pull/7'),
    ("semicolon", "https://github.com/o/r;/pull/7"),
    ("host with slash only", "/o/r#7"),
    ("leading slash", "/github.com/o/r#7"),
    ("too long", "github.com/" + "a/" * 2000 + "r#7"),
    ("ipv6", "https://[::1]/o/r/pull/7"),
]


@pytest.mark.parametrize("text", [t for _, t in BAD_PRS], ids=[n for n, _ in BAD_PRS])
def test_bad_pr_references_are_invalid_pr(text: str) -> None:
    assert code_of(parse_pr, text) == ("invalid_pr", None)


def test_the_message_is_bounded_and_does_not_need_the_raw_text() -> None:
    with pytest.raises(ForgectlError) as caught:
        parse_pr("github.com/o/r#" + "9" * 5000)
    assert len(caught.value.message) < 400


# --- hosts and the closed list ---------------------------------------------------------------


def test_a_known_host_binds_with_its_backend() -> None:
    ref = bind_pr(parse_pr("https://github.com/o/r/pull/7"), CONFIG)
    assert ref == PrRef(host="github.com", backend="github", project="o/r", number=7)
    assert ref.repo == RepoRef(host="github.com", backend="github", path="o/r")
    assert ref.repo.id == "github.com/o/r"
    assert str(ref) == "github.com/o/r#7"


def test_a_declared_host_binds() -> None:
    ref = bind_pr(parse_pr("git.example.test/g/s/p#2"), CONFIG)
    assert (ref.backend, ref.project) == ("gitlab", "g/s/p")


def test_the_port_is_part_of_the_host_key() -> None:
    assert bind_pr(parse_pr("ghe.example.test:8443/o/r#2"), CONFIG).host == "ghe.example.test:8443"
    assert code_of(bind_pr, parse_pr("ghe.example.test:9/o/r#2"), CONFIG) == (
        "refused",
        "unknown_host",
    )


@pytest.mark.parametrize(
    "text",
    [
        "other.example.test/o/r#1",
        "https://other.example.test/o/r/pull/1",
        "www.git.example.test/g/p#1",  # www. is stripped for built-in hosts only
        "githubb.com/o/r#1",
        "github.com.evil.test/o/r#1",
        "xn--gthub-vua.com/o/r#1",
        "127.0.0.1/o/r#1",
        "localhost/o/r#1",
        "o/r#7",  # the first segment is a host, and a dotless one is still not declared
    ],
)
def test_an_undeclared_host_is_unknown_host(text: str) -> None:
    assert code_of(bind_pr, parse_pr(text), CONFIG) == ("refused", "unknown_host")


def test_unknown_host_details_are_not_needed_but_the_message_names_the_host() -> None:
    with pytest.raises(ForgectlError) as caught:
        bind_pr(parse_pr("other.example.test/o/r#1"), CONFIG)
    assert "other.example.test" in caught.value.message


def test_a_github_url_on_a_gitlab_host_is_invalid_pr() -> None:
    assert code_of(bind_pr, parse_pr("https://git.example.test/g/p/pull/1"), CONFIG) == (
        "invalid_pr",
        None,
    )
    assert code_of(bind_pr, parse_pr("https://github.com/o/r/merge_requests/1"), CONFIG) == (
        "invalid_pr",
        None,
    )


@pytest.mark.parametrize("text", ["github.com/o#1", "github.com/o/r/x#1", "github.com/a/b/c/d#1"])
def test_github_takes_exactly_two_segments(text: str) -> None:
    assert code_of(bind_pr, parse_pr(text), CONFIG) == ("invalid_pr", None)


def test_gitlab_takes_one_or_more_segments() -> None:
    assert bind_pr(parse_pr("gitlab.com/p#1"), CONFIG).project == "p"
    assert bind_pr(parse_pr("gitlab.com/a/b/c/d#1"), CONFIG).project == "a/b/c/d"


def test_api_prefix_is_required_and_stripped_for_urls() -> None:
    ref = bind_pr(parse_pr("https://pre.example.test/gitlab/g/p/-/merge_requests/3"), CONFIG)
    assert (ref.project, str(ref)) == ("g/p", "pre.example.test/g/p#3")


def test_a_url_without_the_api_prefix_is_invalid_pr() -> None:
    url = "https://pre.example.test/g/p/-/merge_requests/3"
    assert code_of(bind_pr, parse_pr(url), CONFIG) == ("invalid_pr", None)


def test_a_url_that_is_only_the_api_prefix_is_invalid_pr() -> None:
    url = "https://pre.example.test/gitlab/-/merge_requests/3"
    assert code_of(bind_pr, parse_pr(url), CONFIG) == ("invalid_pr", None)


def test_the_prefix_must_match_whole_segments() -> None:
    url = "https://pre.example.test/gitlabx/g/p/-/merge_requests/3"
    assert code_of(bind_pr, parse_pr(url), CONFIG) == ("invalid_pr", None)


def test_the_host_form_is_not_prefixed() -> None:
    assert bind_pr(parse_pr("pre.example.test/g/p#3"), CONFIG).project == "g/p"


# --- repo ids ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "normalized"),
    [
        ("github.com/o/r", "github.com/o/r"),
        ("GITHUB.com/O/R", "github.com/O/R"),
        ("www.github.com/o/r", "github.com/o/r"),
        ("github.com:443/o/r", "github.com/o/r"),
        ("gitlab.com/a/b/c", "gitlab.com/a/b/c"),
        ("git.example.test:8443/a", "git.example.test:8443/a"),
    ],
)
def test_good_repo_ids(text: str, normalized: str) -> None:
    assert str(parse_repo(text)) == normalized


@pytest.mark.parametrize(
    "text",
    [
        "",
        "github.com",
        "github.com/",
        "github.com/o/",
        "github.com//o",
        "github.com/o/..",
        "github.com/./r",
        "github.com/o/r#7",
        "github.com/o/r?x=1",
        "github.com/o/r\n",
        "github.com/o/r ",
        "github.com/o/r%20",
        "github.com/o@r/x",
        "github.com/o\\r",
        "github.com/ö/r",
        "gïthub.com/o/r",
        "github.com./o/r",
        "https://github.com/o/r",
        "user@github.com/o/r",
        "github.com:0/o/r",
        "github.com/" + "a" * 5000,
    ],
)
def test_bad_repo_ids_are_usage(text: str) -> None:
    assert code_of(parse_repo, text) == ("usage", None)


def test_repo_binding_checks_host_and_segments() -> None:
    assert bind_repo(parse_repo("github.com/o/r"), CONFIG) == RepoRef(
        host="github.com", backend="github", path="o/r"
    )
    assert code_of(bind_repo, parse_repo("other.example.test/o/r"), CONFIG) == (
        "refused",
        "unknown_host",
    )
    assert code_of(bind_repo, parse_repo("github.com/o"), CONFIG) == ("usage", None)
    assert code_of(bind_repo, parse_repo("github.com/o/r/x"), CONFIG) == ("usage", None)
    assert bind_repo(parse_repo("gitlab.com/o"), CONFIG).path == "o"


def test_unknown_host_beats_a_bad_segment_count() -> None:
    assert code_of(bind_repo, parse_repo("other.example.test/o"), CONFIG) == (
        "refused",
        "unknown_host",
    )


@pytest.mark.parametrize(
    ("text", "normalized"),
    [
        ("github.com", "github.com"),
        ("GitLab.COM", "gitlab.com"),
        ("www.gitlab.com", "gitlab.com"),
        ("github.com:443", "github.com"),
        ("git.example.test:8443", "git.example.test:8443"),
    ],
)
def test_good_hosts(text: str, normalized: str) -> None:
    assert parse_host(text) == normalized


@pytest.mark.parametrize(
    "text",
    [
        "",
        "github.com/",
        "github.com/o",
        "user@github.com",
        "github.com.",
        "gïthub.com",
        "github.com\n",
        "git hub.com",
        "github.com:",
        "github.com:99999999",
        "https://github.com",
        "github.com%00",
        "[::1]",
        "-a.com",
        "a" * 300 + ".com",
    ],
)
def test_bad_hosts_are_usage(text: str) -> None:
    assert code_of(parse_host, text) == ("usage", None)


def test_host_binding_is_the_closed_list() -> None:
    assert bind_host("github.com", CONFIG) == "github"
    assert bind_host("git.example.test", CONFIG) == "gitlab"
    assert code_of(bind_host, "elsewhere.example.test", CONFIG) == ("refused", "unknown_host")


# --- logins -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "me",
        "bob",
        "Bob-1",
        "a",
        "a" * 39,
        "dependabot[bot]",
        "a_b",
        ".hidden",
        "_x",
        "a.b",
        "a" * 255,
    ],
)
def test_good_logins(text: str) -> None:
    assert parse_login(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "-bob",
        "bob\n",
        "bob ",
        "bob smith",
        "bob@x",
        "bob/x",
        "bób",
        "bob[bot]x",
        "[bot]",
        "a" * 256,
        "bob%41",
        "bob\\x",
        "bob;rm",
        "--flag",
        "a‮b",
    ],
)
def test_bad_logins_are_usage(text: str) -> None:
    assert code_of(parse_login, text) == ("usage", None)


def test_login_binding_uses_the_backends_grammar() -> None:
    assert bind_login("dependabot[bot]", "github") == "dependabot[bot]"
    assert bind_login("a" * 40, "gitlab") == "a" * 40
    assert bind_login(".hidden", "gitlab") == ".hidden"
    assert bind_login("me", "gitlab") == "me"
    assert code_of(bind_login, "a" * 40, "github") == ("usage", None)
    assert code_of(bind_login, ".hidden", "github") == ("usage", None)
    assert code_of(bind_login, "dependabot[bot]", "gitlab") == ("usage", None)


# --- typed ids --------------------------------------------------------------------------------

GOOD_IDS = [
    "gh:ic:IC_kwDOAbc123",
    "gh:rc:PRRC_abc-def_ghi=",
    "gh:rt:PRRT_kwDO",
    "gh:r:PRR_kwDO",
    "gh:ic:" + "a" * 100,
    "gl:d:" + "a" * 40,
    "gl:d:" + "0123456789abcdef" * 2 + "01234567",
    "gl:n:1",
    "gl:n:" + "9" * 19,
]


@pytest.mark.parametrize("text", GOOD_IDS)
def test_good_typed_ids(text: str) -> None:
    assert str(parse_typed_id(text)) == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "gh:ic:",
        "gh:ic:" + "a" * 101,
        "gh:xx:abc",
        "gh:ic",
        "GH:ic:abc",
        "gh:IC:abc",
        "gh:ic:abc\n",
        "gh:ic:abc def",
        "gh:ic:a/b",
        "gh:ic:a:b",
        "gh:ic:äbc",
        "gh:ic:abc%41",
        "abc",
        "ic:abc",
        "gl:d:" + "a" * 39,
        "gl:d:" + "a" * 41,
        "gl:d:" + "A" * 40,
        "gl:d:" + "g" * 40,
        "gl:n:0",
        "gl:n:01",
        "gl:n:-1",
        "gl:n:1.5",
        "gl:n:" + "9" * 20,
        "gl:n:abc",
        "gl:r:abc",
        "gh:d:" + "a" * 40,
        " gh:ic:abc",
        "gh:ic:abc\x00",
    ],
)
def test_bad_typed_ids_are_usage(text: str) -> None:
    assert code_of(parse_typed_id, text) == ("usage", None)


def test_typed_id_parts() -> None:
    ident = parse_typed_id("gh:rt:PRRT_x")
    assert (ident.host_prefix, ident.kind, ident.node) == ("gh", "rt", "PRRT_x")
    assert parse_typed_id("gl:n:12").node == "12"


# --- shas -------------------------------------------------------------------------------------


def test_sha_lowers_uppercase() -> None:
    assert parse_sha("A" * 40) == "a" * 40


@pytest.mark.parametrize(
    "text", ["", "a" * 39, "a" * 41, "a" * 64, "g" * 40, "a" * 40 + "\n", " " + "a" * 39, "ä" * 40]
)
def test_bad_shas_are_usage(text: str) -> None:
    assert code_of(parse_sha, text) == ("usage", None)


# --- branch names (check-ref-format) ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "main",
        "feat/x",
        "release/1.2.3",
        "a-b_c",
        "feat/ünï",
        "x@y",
        "a.b",
        "a/b.c/d",
        "end-with-dash-",
        "1",
        "a" * 255,
        "@a",
        "refsx/heads/a",
    ],
)
def test_good_branches(name: str) -> None:
    assert parse_branch(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "@",
        "HEAD",
        "-x",
        "--upload-pack=x",
        "/a",
        "a/",
        "a//b",
        "a..b",
        "..",
        ".a",
        "a/.b",
        "a.",
        "a/b.",
        "a.lock",
        "a/b.lock/c",
        "a b",
        "a\tb",
        "a~b",
        "a^b",
        "a:b",
        "a?b",
        "a*b",
        "a[b",
        "a\\b",
        "a@{b",
        "a@{0}",
        "a\x00b",
        "a\x01b",
        "a\x1fb",
        "a\x7fb",
        "a\nb",
        "a\n",
        "a\x85b",
        "a​b",
        "a‮b",
        "a b",
        "a\ud800b",
        "refs/heads/main",
        "refs/tags/x",
        "refs",  # a bare ref root is not a branch
        "a" * 1025,
        "HEAD:refs/heads/x",
        "+main:main",
    ],
)
def test_bad_branches_are_usage(name: str) -> None:
    assert code_of(parse_branch, name) == ("usage", None)


def test_unbound_types_expose_their_normalized_text() -> None:
    assert isinstance(parse_pr("github.com/o/r#7"), PrInput)
