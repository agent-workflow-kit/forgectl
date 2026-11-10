"""The browse query language: parser, formatter, evaluator, form and presets (pure, no widgets)."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

import pytest

from forgectl.tui import columns, filtering, query
from forgectl.tui.query import (
    ME,
    And,
    Cond,
    Not,
    Or,
    ParseError,
    Query,
    evaluate,
    format_query,
    matches,
    parse,
)
from forgectl.tui.source import Row

DAY = 86400
GH = "github.com/acme/widgets"
NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)


def ok(text: str) -> Query:
    result = parse(text)
    assert isinstance(result, Query), f"{text!r}: {result}"
    return result


def bad(text: str) -> ParseError:
    result = parse(text)
    assert isinstance(result, ParseError), f"{text!r} parsed as {result}"
    return result


def row(**values: Any) -> Any:
    """A getter over a dict; a field that is not given is unknown."""
    return lambda name: values.get(name)


# -- the syntax --------------------------------------------------------------------------------


def test_the_empty_query_and_order_by_alone_are_queries() -> None:
    assert ok("") == Query()
    assert ok("   ") == Query()
    assert ok("ORDER BY age") == Query(None, (("age", False),))
    assert ok("order by age desc, ci asc") == Query(None, (("age", True), ("ci", False)))


def test_keywords_and_columns_are_case_insensitive() -> None:
    assert ok("CI = failing and Author = ME order BY Age DESC") == ok(
        "ci = failing AND author = me ORDER BY age DESC"
    )


def test_precedence_not_over_and_over_or() -> None:
    assert ok("draft OR authored AND requested") == Query(
        Or((Cond("draft", "TRUE"), And((Cond("authored", "TRUE"), Cond("requested", "TRUE")))))
    )
    assert ok("NOT draft AND authored") == Query(
        And((Cond("draft", "FALSE"), Cond("authored", "TRUE")))
    )
    # parentheses override it
    assert ok("(draft OR authored) AND requested") == Query(
        And((Or((Cond("draft", "TRUE"), Cond("authored", "TRUE"))), Cond("requested", "TRUE")))
    )
    # NOT of a group is kept, a double NOT cancels
    assert ok("NOT (draft OR authored)") == Query(
        Not(Or((Cond("draft", "TRUE"), Cond("authored", "TRUE"))))
    )
    assert ok("NOT NOT draft") == ok("draft")
    assert ok("NOT NOT NOT draft") == ok("NOT draft")
    assert ok("NOT (NOT (draft OR authored))") == ok("draft OR authored")


def test_nested_groups_of_the_same_kind_flatten() -> None:
    assert ok("draft AND (authored AND requested)") == ok("draft AND authored AND requested")
    assert ok("draft OR (authored OR requested)") == ok("draft OR authored OR requested")


def test_comparison_operators() -> None:
    assert ok("approvals >= 2").where == Cond("approvals", ">=", (2,))
    assert ok("approvals <> 2").where == Cond("approvals", "!=", (2,))
    assert ok("approvals<=3").where == Cond("approvals", "<=", (3,))
    assert ok("ci!=failing").where == Cond("ci", "!=", ("failing",))


def test_quoted_values_and_escapes() -> None:
    assert ok("title = 'a b'").where == Cond("title", "=", ("a b",))
    assert ok('title = "a b"').where == Cond("title", "=", ("a b",))
    assert ok(r"title = 'it\'s'").where == Cond("title", "=", ("it's",))
    assert ok(r"title = 'a\\b'").where == Cond("title", "=", ("a\\b",))
    assert ok("title = ''").where == Cond("title", "=", ("",))
    # a quoted keyword and a quoted `me` are text
    assert ok("title = 'and'").where == Cond("title", "=", ("and",))
    assert ok("author = 'me'").where == Cond("author", "=", ("me",))
    assert ok("author = me").where == Cond("author", "=", (ME,))


def test_like_in_not_in_and_is_null() -> None:
    assert ok("repo LIKE 'acme/*'").where == Cond("repo", "LIKE", ("acme/*",))
    assert ok("repo NOT LIKE acme/*").where == Cond("repo", "NOT LIKE", ("acme/*",))
    assert ok("ci IN (failing, pending)").where == Cond("ci", "IN", ("failing", "pending"))
    assert ok("ci NOT IN (failing)").where == Cond("ci", "NOT IN", ("failing",))
    assert ok("approvals IN (1, 2)").where == Cond("approvals", "IN", (1, 2))
    assert ok("ci IS NULL").where == Cond("ci", "IS NULL")
    assert ok("ci IS NOT NULL").where == Cond("ci", "IS NOT NULL")
    assert ok("text LIKE '*bob*'").where == Cond("text", "LIKE", ("*bob*",))


def test_a_not_in_front_of_a_condition_flips_its_operator() -> None:
    assert ok("NOT ci = failing") == ok("ci != failing")
    assert ok("NOT repo LIKE x*") == ok("repo NOT LIKE x*")
    assert ok("NOT ci IN (failing)") == ok("ci NOT IN (failing)")
    assert ok("NOT ci IS NULL") == ok("ci IS NOT NULL")
    assert ok("NOT approvals < 2") == ok("approvals >= 2")
    assert ok("draft = false") == ok("NOT draft")
    assert ok("draft != true") == ok("NOT draft")
    assert ok("draft = true") == ok("draft")


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("90m", 5400),
        ("12h", 12 * 3600),
        ("2d", 2 * DAY),
        ("1w", 7 * DAY),
        ("1w2d", 9 * DAY),
        ("1W2D3H4M", 9 * DAY + 3 * 3600 + 240),
        ("0m", 0),
    ],
)
def test_durations(text: str, seconds: int) -> None:
    assert query.parse_duration(text) == seconds
    assert ok(f"age > {text}").where == Cond("age", ">", (seconds,))


@pytest.mark.parametrize(
    "text", ["", "7", "d", "1d1d", "2d1w", "1x", "1d2", "-1d", "1.5d", "1 d", "9999999d", "1d1"]
)
def test_not_durations(text: str) -> None:
    assert query.parse_duration(text) is None


def test_the_duration_round_trips_through_its_text() -> None:
    for seconds in (0, 60, 3600, 86400, 9 * DAY + 3660, 52 * 7 * DAY):
        assert query.parse_duration(query.format_duration(seconds)) == seconds


# -- errors ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "pos", "words"),
    [
        ("ci =", 4, "value"),
        ("ci = failing AND", 16, "column"),
        ("nonsense = 1", 0, "unknown column"),
        ("ci failing", 3, "operator"),
        ("(ci = failing", 13, "')'"),
        ("ci = failing)", 12, "unmatched"),
        ("ci = 'failing", 5, "unterminated"),
        ("ci ! failing", 3, "'!'"),
        ("age > 7", 6, "duration"),
        ("approvals > two", 12, "whole number"),
        ("ci = bogus", 5, "one of"),
        ("title = me", 8, "author only"),
        ("author LIKE me", 12, "pattern"),
        ("ci < failing", 3, "does not apply"),
        ("age IN (1d)", 4, "does not apply"),
        ("text = x", 5, "does not apply"),
        ("draft < 3", 8, "true or false"),
        ("ci IN ()", 7, "value"),
        ("ci IN failing", 6, "'('"),
        ("ci IS 5", 6, "NULL"),
        ("ci NOT = x", 7, "LIKE or IN"),
        ("title = AND", 8, "quote it"),
        ("ORDER BY", 8, "column to order by"),
        ("ORDER BY nonsense", 9, "column to order by"),
        ("ORDER BY ci, ci", 13, "twice"),
        ("ORDER BY ci,", 12, "column to order by"),
        ("ORDER age", 6, "BY"),
        ("ci = failing ORDER BY ci garbage", 25, "unexpected"),
        ("draft draft", 6, "unexpected"),
        ("ORDER BY ci WHERE draft", 12, "unexpected"),
    ],
)
def test_errors_name_the_position_and_the_problem(text: str, pos: int, words: str) -> None:
    error = bad(text)
    assert error.pos == pos, (text, error)
    assert words in error.message, (text, error)
    assert f"column {pos + 1}" in str(error)


# -- hostile input -----------------------------------------------------------------------------


def test_over_length_is_refused_before_it_is_read() -> None:
    assert "longer" in bad("a" * (query.MAX_LENGTH + 1)).message
    assert isinstance(parse("draft" + " " * (query.MAX_LENGTH - 5)), Query)


def test_deep_parentheses_are_refused_without_recursion_trouble() -> None:
    depth = query.MAX_DEPTH
    assert isinstance(parse("(" * depth + "draft" + ")" * depth), Query)
    error = bad("(" * (depth + 1) + "draft" + ")" * (depth + 1))
    assert "nest" in error.message
    assert isinstance(parse("(" * 400 + "draft" + ")" * 400), ParseError)  # within the length cap
    assert isinstance(parse("(" * query.MAX_LENGTH), ParseError)


def test_long_chains_are_loops_not_recursion() -> None:
    assert ok("NOT " * 200 + "draft") == ok("draft")
    assert ok("NOT " * 201 + "draft") == ok("NOT draft")
    assert "conditions" in bad(" AND ".join(["draft"] * (query.MAX_CONDITIONS + 1))).message
    many = " OR ".join(["draft"] * query.MAX_CONDITIONS)
    assert isinstance(parse(many), Query)
    assert "values" in bad("ci IN (" + ",".join(["failing"] * 60) + ")").message


def test_too_many_sort_keys() -> None:
    names = ",".join(query.ORDER_COLUMNS[: query.MAX_ORDER + 1])
    assert len(query.ORDER_COLUMNS) > query.MAX_ORDER
    assert "at most" in bad("ORDER BY " + names).message


@pytest.mark.parametrize(
    "text",
    [
        "title = 'a\x00b'",
        "title = 'a\x1bb'",
        "title = a\x07",
        "title = 'a\nb'",
        "title =\n'x'",
        "title = 'a‮b'",  # a bidi override
        "title = 'a​b'",  # a zero-width space
        "title = 'a\x85b'",
        "ti\x00tle = x",
        "\ud800",
        "title = 'a\ud800'",
    ],
)
def test_control_and_format_characters_are_refused_everywhere(text: str) -> None:
    assert isinstance(parse(text), ParseError)


def test_a_long_word_or_value_is_refused() -> None:
    assert "longer" in bad("title = " + "x" * (query.MAX_WORD + 1)).message
    assert "longer" in bad("title = '" + "x" * (query.MAX_WORD + 1) + "'").message
    assert isinstance(parse("title = " + "x" * query.MAX_WORD), Query)


@pytest.mark.parametrize(
    "text",
    [
        "title = '; DROP TABLE prs; --'",
        "title = \"__import__('os').system('x')\"",
        "title LIKE '(?i)^(a+)+$'",
        "title LIKE '[a-z]*'",
        "title LIKE '\\d+\\'",
        "title = '[red]x[/red]'",
        "title = '[@click=app.quit]x'",
        "title LIKE '%'",
        "text LIKE '*' ",
        "title = '\\'",
    ],
)
def test_injection_shaped_text_is_data(text: str) -> None:
    result = parse(text)
    if isinstance(result, Query):
        assert isinstance(result.where, Cond)  # one condition, nothing else


def test_regex_like_patterns_match_literally() -> None:
    q = ok("title LIKE '[a-z]*'")
    assert matches(q.where, row(title="[a-z]xyz"))
    assert not matches(q.where, row(title="abc"))
    q = ok("title LIKE '(a+)+$'")
    assert matches(q.where, row(title="(a+)+$"))
    assert not matches(q.where, row(title="aaaa"))
    q = ok("title LIKE '.*'")
    assert not matches(q.where, row(title="abc"))
    assert matches(q.where, row(title=".*"))


def test_pathological_globs_match_only_a_bounded_text(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    real = query._glob

    def spy(pattern: str, text: str) -> bool:
        seen.append(len(text))
        return real(pattern, text)

    monkeypatch.setattr(query, "_glob", spy)
    q = ok("title LIKE '" + "*a" * 20 + "b'")
    get = filtering.getter(
        Row({"pr": f"{GH}#1", "title": "a" * 5000, "author": {"login": "x"}}),
        columns.RowState(),
        NOW,
    )
    assert not matches(q.where, get)
    assert seen == [filtering._TEXT_LIMIT] and filtering._TEXT_LIMIT == 500


def test_the_parser_is_total_on_random_text() -> None:
    rng = random.Random(1105)
    alphabet = "()'\" ,=!<>*?\\\tabcdefilnoprstuwxyz0123456789_-./@#é中\x00\x1b‮"
    pieces = [
        "AND",
        "OR",
        "NOT",
        "IN",
        "LIKE",
        "IS",
        "NULL",
        "ORDER",
        "BY",
        "DESC",
        "me",
        "ci",
        "age",
    ]
    for _ in range(3000):
        if rng.random() < 0.5:
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        else:
            text = " ".join(rng.choice(pieces + list(alphabet)) for _ in range(rng.randint(0, 15)))
        result = parse(text)
        assert isinstance(result, Query | ParseError)
        if isinstance(result, Query):
            assert parse(format_query(result)) == result  # and it formats to a query again


def test_non_text_input_is_an_error_not_a_crash() -> None:
    assert isinstance(parse(None), ParseError)  # type: ignore[arg-type]
    assert isinstance(parse(b"draft"), ParseError)  # type: ignore[arg-type]


def test_unicode_values_round_trip() -> None:
    q = ok("title = 'café 中文 ☃'")
    assert q.where == Cond("title", "=", ("café 中文 ☃",))
    assert ok(format_query(q)) == q
    assert matches(q.where, row(title="CAFÉ 中文 ☃"))  # `=` is case-insensitive beyond ASCII


# -- formatting --------------------------------------------------------------------------------

ROUND_TRIPS = [
    "",
    "draft",
    "NOT draft",
    "ORDER BY age DESC, ci",
    "ci IN (failing, pending) AND author = me AND repo LIKE 'acme/*' AND age > 1w AND NOT draft "
    "ORDER BY age DESC, ci",
    "(draft OR authored) AND requested",
    "draft OR authored AND requested",
    "NOT (draft OR authored)",
    "NOT (draft AND (authored OR requested))",
    "title = 'it\\'s a \\\\ test'",
    "title = 'and'",
    "author = 'me'",
    "ci IS NULL AND approvals IS NOT NULL",
    "age >= 1d12h AND pushed < 90m",
    "text LIKE '*bob*'",
    "title = ''",
]


@pytest.mark.parametrize("text", ROUND_TRIPS)
def test_format_is_canonical_and_round_trips(text: str) -> None:
    q = ok(text)
    canonical = format_query(q)
    assert ok(canonical) == q
    assert format_query(ok(canonical)) == canonical


def test_format_spells_the_canonical_text() -> None:
    assert (
        query.canonical("CI in (failing,pending) and not draft order by AGE desc")
        == "ci IN (failing, pending) AND NOT draft ORDER BY age DESC"
    )
    assert query.canonical("draft or authored and requested") == ("draft OR authored AND requested")
    assert query.canonical("(draft or authored) and requested") == (
        "(draft OR authored) AND requested"
    )
    assert isinstance(query.canonical("("), ParseError)


# -- evaluation --------------------------------------------------------------------------------


def run(query_text: str, **values: Any) -> bool | None:
    return evaluate(ok(query_text).where, row(**values))


def test_no_filter_matches_everything() -> None:
    assert run("") is True
    assert run("ORDER BY age") is True


def test_text_equality_is_exact_and_case_insensitive() -> None:
    assert run("ci = FAILING", ci="failing") is True
    assert run("ci = passing", ci="failing") is False
    assert run("ci != failing", ci="failing") is False
    assert run("ci != passing", ci="failing") is True
    assert run("title = 'a b'", title="A B") is True


def test_like_wildcards() -> None:
    assert run("repo LIKE 'acme/*'", repo="github.com/acme/x") is False
    assert run("repo LIKE '*/acme/*'", repo="github.com/acme/x") is True
    assert run("title LIKE 'fix ?'", title="fix 1") is True
    assert run("title LIKE 'fix ?'", title="fix 12") is False
    assert run("title LIKE '*FIX*'", title="a fix b") is True
    assert run("title NOT LIKE '*fix*'", title="a fix b") is False
    assert run("title LIKE 'a*b'", title="a*b") is True
    assert run("title LIKE '*'", title="") is True


def test_free_text_searches_title_repo_and_author() -> None:
    parts = ("A Title", "github.com/acme/widgets", "bob")
    assert run("text LIKE '*widg*'", text=parts) is True
    assert run("text LIKE '*bob*'", text=parts) is True
    assert run("text LIKE '*title*'", text=parts) is True
    assert run("text LIKE '*zzz*'", text=parts) is False
    assert run("text NOT LIKE '*zzz*'", text=parts) is True


def test_in_and_not_in() -> None:
    assert run("ci IN (failing, pending)", ci="pending") is True
    assert run("ci IN (failing, pending)", ci="passing") is False
    assert run("ci NOT IN (failing, pending)", ci="passing") is True
    assert run("ci NOT IN (failing, pending)", ci="failing") is False
    assert run("approvals IN (1, 3)", approvals=3) is True
    assert run("approvals NOT IN (1, 3)", approvals=3) is False


def test_counts_and_bare_columns() -> None:
    assert run("approvals >= 2", approvals=2) is True
    assert run("approvals > 2", approvals=2) is False
    assert run("approvals = 0", approvals=0) is True
    assert run("approvals != 0", approvals=0) is False
    assert run("others_since_push", others_since_push=3) is True
    assert run("others_since_push", others_since_push=0) is False
    assert run("NOT others_since_push", others_since_push=0) is True
    assert run("draft", draft=True) is True
    assert run("NOT draft", draft=True) is False
    assert run("awaiting_reply AND NOT draft", awaiting_reply=1, draft=False) is True


def test_age_means_the_time_since_the_update() -> None:
    week = 7 * DAY
    assert run("age > 7d", age=week + 1) is True
    assert run("age > 7d", age=week) is False
    assert run("age >= 1w", age=week) is True
    assert run("age < 2d", age=DAY) is True
    assert run("age < 2d", age=3 * DAY) is False
    assert run("age > 1w2d", age=9 * DAY + 1) is True
    assert run("age > 90m", age=5401) is True


def test_me_is_the_viewer_and_applies_to_author() -> None:
    assert run("author = me", author="bob", **{"@me": True}) is True
    assert run("author = me", author="bob", **{"@me": False}) is False
    assert run("author != me", author="bob", **{"@me": False}) is True
    assert run("author IN (me, carol)", author="carol", **{"@me": False}) is True
    assert run("author IN (me, carol)", author="bob", **{"@me": True}) is True
    assert run("author = 'me'", author="me", **{"@me": False}) is True  # quoted: the text
    assert run("author = me", author="bob") is None  # the viewer is not known


def test_unknown_values_match_nothing_but_is_null() -> None:
    for text in (
        "ci = failing",
        "ci != failing",
        "ci IN (failing)",
        "ci NOT IN (failing)",
        "ci LIKE 'f*'",
        "ci NOT LIKE 'f*'",
        "NOT ci = failing",
        "approvals > 1",
        "approvals <= 1",
        "others_since_push",
        "NOT others_since_push",
        "age < 1d",
        "NOT (ci = failing)",
    ):
        assert run(text) is None, text
        assert not matches(ok(text).where, row()), text
    assert run("ci IS NULL") is True
    assert run("ci IS NOT NULL") is False
    assert run("ci IS NULL", ci="failing") is False
    assert run("ci IS NOT NULL", ci="failing") is True


def test_three_valued_logic_through_and_or_not() -> None:
    # `ci` is unknown, `draft` is known
    assert run("ci = failing AND draft", draft=False) is False  # false dominates
    assert run("ci = failing AND draft", draft=True) is None
    assert run("ci = failing OR draft", draft=True) is True  # true dominates
    assert run("ci = failing OR draft", draft=False) is None
    assert run("NOT (ci = failing AND draft)", draft=False) is True
    assert run("NOT (ci = failing OR draft)", draft=False) is None
    assert run("ci IS NULL AND draft", draft=True) is True
    assert not matches(ok("ci = failing OR draft").where, row(draft=False))


def test_the_example_query() -> None:
    text = (
        "ci IN (failing, pending) AND author = me AND repo LIKE 'acme/*' AND age > 7d "
        "AND NOT draft ORDER BY age DESC, ci"
    )
    q = ok(text)
    assert q.order == (("age", True), ("ci", False))
    hit = row(ci="failing", author="x", repo="acme/w", age=8 * DAY, draft=False, **{"@me": True})
    assert matches(q.where, hit)
    miss = row(ci="failing", author="x", repo="acme/w", age=8 * DAY, draft=True, **{"@me": True})
    assert not matches(q.where, miss)


def test_evaluating_a_huge_flat_query_is_bounded() -> None:
    text = " AND ".join(["ci != passing"] * query.MAX_CONDITIONS)
    assert matches(ok(text).where, row(ci="failing"))


# -- the sort keys are the columns' ------------------------------------------------------------


def test_order_by_names_exactly_the_sortable_columns() -> None:
    assert query.ORDER_COLUMNS == columns.SORTABLE


# -- the form ----------------------------------------------------------------------------------


def test_a_conjunction_shows_in_the_form_and_goes_back_to_the_same_query() -> None:
    q = ok("ci IN (failing, pending) AND author = me AND NOT draft AND age > 1w ORDER BY age DESC")
    form = query.to_form(q)
    assert form.filters == (
        query.FormFilter("ci", "IN", "failing, pending"),
        query.FormFilter("author", "=", "me"),
        query.FormFilter("draft", "FALSE", ""),
        query.FormFilter("age", ">", "1w"),
    )
    assert form.order == (("age", True),)
    assert ok(query.form_text(form)) == q


def test_values_with_spaces_keep_their_quotes_in_the_form() -> None:
    q = ok("title = 'a b' AND ci IS NULL")
    form = query.to_form(q)
    assert form.filters is not None and form.filters[0].value == "'a b'"
    assert ok(query.form_text(form)) == q


def test_an_or_or_group_cannot_be_shown_as_rows_but_keeps_its_filter() -> None:
    q = ok("(draft OR authored) AND ci = failing ORDER BY ci")
    form = query.to_form(q)
    assert form.filters is None
    assert ok(query.form_text(form)) == q
    changed = query.Form(None, (("age", False),), form.rest)
    assert ok(query.form_text(changed)).where == q.where
    assert ok(query.form_text(changed)).order == (("age", False),)
    assert query.to_form(ok("NOT (draft AND authored)")).filters is None


def test_a_bad_form_row_is_an_error_from_the_parser() -> None:
    form = query.Form((query.FormFilter("ci", "=", "bogus"),), ())
    assert isinstance(parse(query.form_text(form)), ParseError)
    inject = query.Form((query.FormFilter("title", "=", "x OR 1 = 1)"),), ())
    assert isinstance(parse(query.form_text(inject)), ParseError)
    empty = query.Form((query.FormFilter("ci", "=", ""),), ())
    assert isinstance(parse(query.form_text(empty)), ParseError)


def test_every_operator_of_every_kind_forms_a_valid_row() -> None:
    samples = {"text": "'x y'", "count": "3", "time": "2d", "bool": "true", "search": "'*x*'"}
    for name, kind in query.FIELDS.items():
        for op in query.OPERATORS[kind]:
            value = ""
            if op not in query.NO_VALUE:
                value = samples[kind]
                if name in query.VALUES:
                    value = query.VALUES[name][0]
                if name == "draft":
                    value = "true"
            if op in ("IN", "NOT IN") and kind == "text" and name in query.VALUES:
                value = query.VALUES[name][0]
            text = query.form_text(query.Form((query.FormFilter(name, op, value),), ()))
            assert isinstance(parse(text), Query), (name, op, text)


# -- quick filters -----------------------------------------------------------------------------


def test_presets_toggle_a_term_in_a_conjunction() -> None:
    a = query.PRESETS["authored"]
    f = query.PRESETS["ci-failing"]
    q = query.toggle_term(Query(), a)
    assert format_query(q) == "authored"
    q = query.toggle_term(q, f)
    assert format_query(q) == "authored AND ci = failing"
    q = query.toggle_term(q, a)
    assert format_query(q) == "ci = failing"
    q = query.toggle_term(q, f)
    assert format_query(q) == ""
    assert q.where is None


def test_presets_keep_the_sort_and_group_an_or_filter() -> None:
    q = ok("draft OR requested ORDER BY age")
    q = query.toggle_term(q, query.PRESETS["authored"])
    assert format_query(q) == "(draft OR requested) AND authored ORDER BY age"
    assert ok(format_query(q)) == q


def test_the_four_presets_are_the_quick_filters() -> None:
    assert {k: format_query(Query(v)) for k, v in query.PRESETS.items()} == {
        "authored": "authored",
        "requested": "requested",
        "ci-failing": "ci = failing",
        "new-comments": "others_since_push",
    }
    assert list(query.PRESETS) == ["authored", "requested", "ci-failing", "new-comments"]


# -- round 1 fixes -----------------------------------------------------------------------------


def test_a_query_whose_canonical_text_is_too_long_is_refused() -> None:
    text = " AND ".join(f"title=xxxxxxx{n:02d}" for n in range(50))
    assert len(text) == 995 <= query.MAX_LENGTH  # the typed text fits, its canonical form not
    result = parse(text)
    assert isinstance(result, ParseError)
    assert "canonical" in result.message


def test_an_accepted_query_always_parses_back_from_its_canonical_text() -> None:
    rng = random.Random(2105)
    pieces = ["title=xxxxxxx", "AND", "OR", "NOT", "(", ")", "ci=FAILING", "draft", "age>7d"]
    for _ in range(2000):
        text = " ".join(rng.choice(pieces) for _ in range(rng.randint(1, 60)))
        result = parse(text)
        if isinstance(result, Query):
            assert len(format_query(result)) <= query.MAX_LENGTH
            assert parse(format_query(result)) == result


def test_a_condition_is_toggled_off_among_other_groups() -> None:
    term = query.PRESETS["authored"]
    q = query.toggle_term(ok("authored AND (draft OR requested)"), term)
    assert format_query(q) == "draft OR requested"
    q = query.toggle_term(ok("authored AND NOT (draft OR requested) AND ci = failing"), term)
    assert format_query(q) == "NOT (draft OR requested) AND ci = failing"
    assert format_query(query.toggle_term(ok("authored"), term)) == ""


def test_a_preset_matches_an_enum_value_in_any_case() -> None:
    q = Query(Cond("ci", "=", ("FAILING",)), ())
    assert query.toggle_term(q, query.PRESETS["ci-failing"]).where is None


def test_a_toggle_that_does_not_fit_is_detected() -> None:
    full = ok(" AND ".join(f"approvals > {n}" for n in range(50)))
    over = query.toggle_term(full, query.PRESETS["authored"])
    assert not query.fits(over)
    assert query.fits(full)


def test_enum_values_are_lowercase_in_the_canonical_text() -> None:
    assert format_query(ok("ci = FAILING")) == "ci = failing"
    assert format_query(ok("review IN (Approved, 'NONE')")) == "review IN (approved, none)"
    assert format_query(ok("ci LIKE 'FAIL*'")) == "ci LIKE FAIL*"  # a pattern is kept


@pytest.mark.parametrize(
    "text", ["ORDER BY age DE\u017fC", "ci = failing AND dra\u017ft", "\u212a = 1", "AND\u017f"]
)
def test_unicode_lookalikes_are_not_keywords(text: str) -> None:
    bad(text)


def test_and_or_short_circuit_without_changing_the_three_valued_answer() -> None:
    calls: list[str] = []

    def get(name: str) -> Any:
        calls.append(name)
        return {"draft": False, "authored": True}.get(name)

    assert evaluate(ok("draft AND requested").where, get) is False
    assert calls == ["draft"]
    calls.clear()
    assert evaluate(ok("authored OR requested").where, get) is True
    assert calls == ["authored"]
    assert evaluate(ok("requested AND authored").where, get) is None
    assert evaluate(ok("requested OR draft").where, get) is None
    assert evaluate(ok("requested OR NOT draft").where, get) is True


def _state(blocker: str | None, status: str = columns.LOADED) -> columns.RowState:
    return columns.RowState(status=status, summary=columns.Summary(blocker=blocker))


def _eval(text: str, state: columns.RowState) -> bool | None:
    return filtering.evaluate(
        ok(text).where, Row({"pr": f"{GH}#1", "title": "t", "author": {"login": "x"}}), state, NOW
    )


def test_a_loaded_snapshot_with_no_blocker_is_known_empty() -> None:
    free = _state(None)
    assert _eval("blocker != ci_failing", free) is True
    assert _eval("NOT blocker = ci_failing", free) is True
    assert _eval("blocker = ci_failing", free) is False
    assert _eval("blocker IS NULL", free) is True
    assert _eval("blocker IS NOT NULL", free) is False
    blocked = _state("ci_failing")
    assert _eval("blocker = ci_failing", blocked) is True
    assert _eval("blocker IS NULL", blocked) is False


def test_an_empty_title_is_still_a_value_for_is_null() -> None:
    row = Row({"pr": f"{GH}#1", "title": "", "author": {"login": "x"}})
    got = filtering.evaluate(ok("title IS NULL").where, row, _state(None), NOW)
    assert got is False


def test_toggling_a_duplicated_term_off_leaves_no_filter() -> None:
    q = ok("ci = failing AND ci = failing")
    assert query.toggle_term(q, query.PRESETS["ci-failing"]).where is None


def test_an_unloaded_row_has_an_unknown_blocker_and_is_the_only_one_counted() -> None:
    loading = columns.RowState()
    assert _eval("blocker != x", loading) is None
    assert _eval("blocker IS NULL", loading) is True
    rows = [Row({"pr": f"{GH}#{n}", "title": "t", "author": {"login": "x"}}) for n in (1, 2, 3)]
    states = {rows[0].pr: _state(None), rows[1].pr: _state(None, columns.FAILED)}
    where = ok("blocker != x AND approvals > 0").where
    # #1 is loaded (approvals not reported: its answer), #2 failed, #3 never started
    assert filtering.unknown_count(where, rows, states, NOW) == 2
    assert filtering.unknown_count(ok("blocker != x").where, rows, states, NOW) == 2
