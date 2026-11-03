"""Table and property tests for ``forgectl.neutralize`` (interface: Neutralizing, Dedupe key)."""

from __future__ import annotations

import random
import re

import pytest

from forgectl.neutralize import neutralize

# One entry per separator in the interface: LF, CRLF, lone CR, VT, FF, FS, GS, RS, NEL, LS, PS.
SEPARATORS = {
    "LF": "\n",
    "CRLF": "\r\n",
    "CR": "\r",
    "VT": "\x0b",
    "FF": "\x0c",
    "FS": "\x1c",
    "GS": "\x1d",
    "RS": "\x1e",
    "NEL": "\x85",
    "LS": "\u2028",
    "PS": "\u2029",
}


@pytest.mark.parametrize("sep", SEPARATORS.values(), ids=SEPARATORS.keys())
def test_each_separator_is_split_and_rejoined_with_lf(sep: str) -> None:
    assert neutralize(f"a{sep}b{sep}c").text == "a\nb\nc"


@pytest.mark.parametrize("sep", SEPARATORS.values(), ids=SEPARATORS.keys())
def test_a_slash_after_each_separator_is_a_line_start_and_gets_a_space(sep: str) -> None:
    result = neutralize(f"ok{sep}/approve")
    assert result.text == "ok\n /approve"
    assert result.neutralized_lines == 1


@pytest.mark.parametrize("sep", SEPARATORS.values(), ids=SEPARATORS.keys())
def test_a_trailing_separator_leaves_one_trailing_lf(sep: str) -> None:
    assert neutralize(f"a{sep}").text == "a\n"


def test_crlf_is_one_break_not_two() -> None:
    assert neutralize("a\r\nb").text == "a\nb"


def test_cr_cr_lf_is_two_breaks() -> None:
    assert neutralize("a\r\r\nb").text == "a\n\nb"


def test_lf_cr_is_two_breaks() -> None:
    assert neutralize("a\n\rb").text == "a\n\nb"


def test_empty_body() -> None:
    result = neutralize("")
    assert (result.text, result.neutralized_lines, result.markers_removed) == ("", 0, 0)


def test_only_separators() -> None:
    assert neutralize("\r\n\u2028\x85").text == "\n\n\n"


# --- the leading-space rule ----------------------------------------------------------------------


def test_slash_at_the_start_of_the_body() -> None:
    result = neutralize("/approve")
    assert (result.text, result.neutralized_lines) == (" /approve", 1)


def test_slash_alone() -> None:
    assert neutralize("/").text == " /"


def test_every_slash_line_is_counted() -> None:
    result = neutralize("/a\nb\n/c\n//d")
    assert result.text == " /a\nb\n /c\n //d"
    assert result.neutralized_lines == 3


def test_a_slash_in_the_middle_of_a_line_is_untouched() -> None:
    result = neutralize("see a/b and x /approve")
    assert (result.text, result.neutralized_lines) == ("see a/b and x /approve", 0)


def test_one_space_only_and_a_line_already_indented_is_untouched() -> None:
    assert neutralize(" /approve").text == " /approve"
    assert neutralize("\t/approve").text == "\t/approve"


def test_a_line_starting_with_a_url_scheme_slash_pair_is_still_a_slash_line() -> None:
    assert neutralize("//comment").text == " //comment"


# --- breakouts from the live cases ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("```\n/approve\n```", "```\n /approve\n```"),
        ("```suggestion\n/approve\n```", "```suggestion\n /approve\n```"),
        ("~~~\n/merge\n~~~", "~~~\n /merge\n~~~"),
        ("> /approve", "> /approve"),
        ("> quote\n/approve", "> quote\n /approve"),
        (">\n/approve", ">\n /approve"),
        ("<!--\n/approve\n-->", "<!--\n /approve\n-->"),
        ("<!-- x -->\n/approve", "<!-- x -->\n /approve"),
        ("```\r/approve", "```\n /approve"),
        ("```\u2028/approve", "```\n /approve"),
        ("> q\x85/approve", "> q\n /approve"),
        ("text\x0b/approve", "text\n /approve"),
        ("text\x0c/approve", "text\n /approve"),
        ("text\x1c/approve\x1d/x\x1e/y", "text\n /approve\n /x\n /y"),
        ("<details>\n/approve\n</details>", "<details>\n /approve\n</details>"),
        (">>>\n/approve\n>>>", ">>>\n /approve\n>>>"),
    ],
)
def test_fence_quote_comment_and_separator_breakouts(body: str, expected: str) -> None:
    assert neutralize(body).text == expected


def test_a_gitlab_multi_line_quote_breakout_counts_the_slash_line() -> None:
    assert neutralize(">>>\n/approve\n>>>").neutralized_lines == 1


def test_a_bom_before_a_slash_is_not_a_line_start_slash() -> None:
    # Only a first character "/" triggers; a leading U+FEFF is left as it is.
    result = neutralize("\ufeff/approve")
    assert (result.text, result.neutralized_lines) == ("\ufeff/approve", 0)


def test_a_quote_marker_before_a_slash_is_not_a_line_start_slash() -> None:
    # "> /x" starts with ">", so it is not altered; the interface parses no Markdown.
    assert neutralize("> /x").neutralized_lines == 0


# --- marker-shaped text ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "forgectl:dedupe:key",
        "FORGECTL:DEDUPE:key",
        "Forgectl:Dedupe:KEY",
        "fOrGeCtL:dEdUpE:k",
        "forgectl:dedupe:",
        "<!-- forgectl:dedupe:abc -->",
        "x forgectl:dedupe:a y",
    ],
)
def test_marker_shaped_text_in_any_case_has_its_second_colon_replaced(text: str) -> None:
    result = neutralize(text)
    assert "forgectl:dedupe:" not in result.text.lower()
    assert result.markers_removed == 1
    assert len(result.text) == len(text)


def test_the_replacement_is_a_hyphen_in_the_second_colon_and_case_is_kept() -> None:
    assert neutralize("<!-- Forgectl:DEDUPE:key -->").text == "<!-- Forgectl:DEDUPE-key -->"


def test_occurrences_are_counted_across_lines_and_on_one_line() -> None:
    result = neutralize("forgectl:dedupe:a forgectl:dedupe:b\nFORGECTL:DEDUPE:c")
    assert result.markers_removed == 3
    assert result.text == "forgectl:dedupe-a forgectl:dedupe-b\nFORGECTL:DEDUPE-c"


def test_marker_across_a_line_separator_is_not_a_marker() -> None:
    result = neutralize("forgectl:dedupe\n:k")
    assert result.markers_removed == 0
    assert result.text == "forgectl:dedupe\n:k"


def test_marker_hidden_by_a_separator_in_the_middle_never_forms_one_after_rejoin() -> None:
    # A separator splits the text; the rejoined LF is not part of the pattern either.
    assert neutralize("forgectl:\rdedupe:k").markers_removed == 0


def test_text_without_the_marker_reports_zero() -> None:
    assert neutralize("forgectl dedupe: forgectl:dedup:").markers_removed == 0


def test_doubled_colons_still_lose_one() -> None:
    result = neutralize("forgectl:dedupe::k")
    assert result.text == "forgectl:dedupe-:k"
    assert result.markers_removed == 1


def test_marker_on_a_slash_line_counts_both() -> None:
    result = neutralize("/forgectl:dedupe:k")
    assert result.text == " /forgectl:dedupe-k"
    assert (result.neutralized_lines, result.markers_removed) == (1, 1)


# --- properties -------------------------------------------------------------------------------

POOL = [
    *SEPARATORS.values(),
    "/",
    "/",
    " ",
    "a",
    "> ",
    "```",
    "<!--",
    "-->",
    "forgectl:dedupe:",
    "FORGECTL:Dedupe:",
    "forgectl:",
    "dedupe:",
    ":",
    "\t",
    "\u200b",
]
_RNG = random.Random(6)
SAMPLES = ["".join(_RNG.choice(POOL) for _ in range(_RNG.randint(0, 30))) for _ in range(500)]


def test_property_idempotent_and_second_pass_reports_nothing() -> None:
    for s in SAMPLES:
        once = neutralize(s)
        twice = neutralize(once.text)
        assert twice.text == once.text, repr(s)
        assert (twice.neutralized_lines, twice.markers_removed) == (0, 0), repr(s)


def test_property_no_line_starts_with_a_slash_and_no_marker_remains() -> None:
    for s in SAMPLES:
        text = neutralize(s).text
        assert not any(line.startswith("/") for line in text.split("\n")), repr(s)
        assert "forgectl:dedupe:" not in text.lower(), repr(s)


def test_property_only_lf_separators_remain() -> None:
    for s in SAMPLES:
        text = neutralize(s).text
        for sep in SEPARATORS.values():
            if sep != "\n":
                assert sep not in text, repr(s)


def test_property_neutralized_lines_counts_the_input_lines_that_start_with_a_slash() -> None:
    split = re.compile("\\r\\n|[\\n\\r\\x0b\\x0c\\x1c\\x1d\\x1e\\x85\\u2028\\u2029]")
    for s in SAMPLES:
        expected = sum(1 for line in split.split(s) if line.startswith("/"))
        assert neutralize(s).neutralized_lines == expected, repr(s)
