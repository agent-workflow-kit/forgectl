"""Table and property tests for ``forgectl.sanitize`` (interface: Sanitizing).

The tables are written out from the interface, not derived from the code under test. Code points
are chosen to be stable across the Unicode versions of the supported Pythons (3.12 and 3.13), so
no assigned-in-one-version-only character appears here.
"""

from __future__ import annotations

import random
import re

import pytest

from forgectl.sanitize import sanitize_bytes, sanitize_diff, sanitize_line, sanitize_text

# The interface's explicit list.
EXPLICIT = [
    0x034F,
    0x17B4,
    0x17B5,
    0x180B,
    0x180C,
    0x180D,
    0x180E,
    0x180F,
    0x115F,
    0x1160,
    0x3164,
    0xFFA0,
    0x2800,
    *range(0xFE00, 0xFE10),
    *range(0xE0100, 0xE01F0),
]
CC_MARKED = [cp for cp in [*range(0x00, 0x20), *range(0x7F, 0xA0)] if cp not in (0x09, 0x0A)]
CF = [0x00AD, 0x0600, 0x061C, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0x2066, 0xFEFF]
BIDI = [0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069]
SURROGATES = [0xD800, 0xDB7F, 0xDBFF, 0xDC00, 0xDCFF, 0xDFFF]
PRIVATE_USE = [0xE000, 0xF8FF, 0xF0000, 0xFFFFD, 0x100000, 0x10FFFD]
UNASSIGNED = [0x0378, 0x0379, 0x0FFFF, 0xFFFF, 0xFDD0, 0x10FFFF, 0xE0000]
SEPARATORS = [0x2028, 0x2029]
ORDINARY = [
    "a",
    "Z",
    " ",
    "\u00e9",
    "\u65e5\u672c",
    "\U0001f600",
    "\u00a0",
    "\u3000",
    "<",
    "U+0041",
    "\\u202e",
]


def marker(cp: int) -> str:
    return f"<U+{cp:04X}>"


ALL_MARKED = [
    *EXPLICIT,
    *CC_MARKED,
    *CF,
    *BIDI,
    *SURROGATES,
    *PRIVATE_USE,
    *UNASSIGNED,
    *SEPARATORS,
]


@pytest.mark.parametrize("cp", sorted(set(ALL_MARKED)), ids=lambda cp: f"U+{cp:04X}")
@pytest.mark.parametrize("fn", [sanitize_text, sanitize_line, sanitize_diff])
def test_every_listed_code_point_becomes_a_marker(cp: int, fn) -> None:
    assert fn(f"a{chr(cp)}b") == f"a{marker(cp)}b"


def test_variation_selector_fe0f_in_an_emoji_sequence_is_marked() -> None:
    assert sanitize_text("\u2764\ufe0f") == "\u2764<U+FE0F>"


def test_zero_width_joiner_inside_an_emoji_is_marked() -> None:
    assert sanitize_text("\U0001f468\u200d\U0001f469") == "\U0001f468<U+200D>\U0001f469"


def test_markers_have_at_least_four_hex_digits_and_five_or_six_above_the_bmp() -> None:
    assert sanitize_text("\x00") == "<U+0000>"
    assert sanitize_text("\U000e0100") == "<U+E0100>"
    assert sanitize_text("\U0010ffff") == "<U+10FFFF>"


@pytest.mark.parametrize("text", ORDINARY)
@pytest.mark.parametrize("fn", [sanitize_text, sanitize_line, sanitize_diff])
def test_ordinary_text_is_untouched(text: str, fn) -> None:
    assert fn(text) == text


@pytest.mark.parametrize("fn", [sanitize_text, sanitize_line, sanitize_diff])
def test_empty_string(fn) -> None:
    assert fn("") == ""


def test_bidi_override_stays_visible_in_a_diff_line() -> None:
    assert sanitize_diff("+if (admin\u202e) {\n") == "+if (admin<U+202E>) {\n"


def test_lone_surrogate_does_not_raise_and_is_marked() -> None:
    assert sanitize_text("\ud800") == "<U+D800>"
    assert sanitize_text("x\udcffy") == "x<U+DCFF>y"


def test_surrogate_pair_written_as_two_code_units_is_two_markers() -> None:
    # Two lone code units built by chr(): Python never joins them into U+1F600.
    assert sanitize_text(chr(0xD83D) + chr(0xDE00)) == "<U+D83D><U+DE00>"


# --- tab, newline, CR -----------------------------------------------------------------------


def test_text_keeps_tab_and_newline() -> None:
    assert sanitize_text("a\tb\nc") == "a\tb\nc"


def test_one_line_field_marks_tab_and_newline() -> None:
    assert sanitize_line("a\tb\nc") == "a<U+0009>b<U+000A>c"


def test_crlf_becomes_lf_in_text() -> None:
    assert sanitize_text("a\r\nb") == "a\nb"


def test_lone_cr_becomes_a_marker_in_text() -> None:
    assert sanitize_text("a\rb\r") == "a<U+000D>b<U+000D>"


def test_cr_cr_lf_is_a_marker_then_lf_in_text() -> None:
    assert sanitize_text("a\r\r\nb") == "a<U+000D>\nb"


def test_lf_cr_is_lf_then_marker_in_text() -> None:
    assert sanitize_text("a\n\rb") == "a\n<U+000D>b"


def test_crlf_in_a_one_line_field_is_one_newline_marker() -> None:
    assert sanitize_line("a\r\nb") == "a<U+000A>b"


def test_lone_cr_in_a_one_line_field_is_a_marker() -> None:
    assert sanitize_line("a\rb") == "a<U+000D>b"


def test_diff_keeps_a_cr_before_lf() -> None:
    assert sanitize_diff("+a\r\n-b\r\n") == "+a\r\n-b\r\n"


def test_diff_marks_a_lone_cr() -> None:
    assert sanitize_diff("+a\rb\n") == "+a<U+000D>b\n"
    assert sanitize_diff("+a\r") == "+a<U+000D>"


def test_diff_marks_the_first_of_two_crs_before_lf() -> None:
    assert sanitize_diff("a\r\r\nb") == "a<U+000D>\r\nb"


def test_diff_keeps_tab() -> None:
    assert sanitize_diff("+\tx\n") == "+\tx\n"


# --- a replacement stays on its line ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "a\u2028b\nc",
        "a\u2029b\nc",
        "a\x85b\nc",
        "a\x0bb\x0cc\nd",
        "a\x1cb\x1dc\x1ed\nx",
        "a\rb\nc",
        "a\u202e\nb\u200b\nc",
    ],
)
def test_line_numbers_hold_for_text_and_diff(text: str) -> None:
    for fn in (sanitize_text, sanitize_diff):
        out = fn(text)
        assert out.count("\n") == text.count("\n")
        assert out.split("\n")[-1].endswith(text.split("\n")[-1].replace("\x85", "<U+0085>"))


def test_unicode_line_separators_do_not_break_a_line_in_str_splitlines_terms() -> None:
    out = sanitize_text("one\u2028two\x85three\x0bfour\x0cfive\x1csix")
    assert out.splitlines() == [out]


# --- bytes ------------------------------------------------------------------------------------


def test_invalid_utf8_becomes_replacement_characters() -> None:
    assert sanitize_bytes(b"a\xffb\xc3") == "a\ufffdb\ufffd"


def test_utf8_encoded_surrogate_is_invalid_and_never_a_marker_source() -> None:
    out = sanitize_bytes(b"\xed\xa0\x80")
    assert "\ud800" not in out
    assert set(out) == {"\ufffd"}


def test_overlong_and_truncated_sequences_become_replacement_characters() -> None:
    assert set(sanitize_bytes(b"\xc0\xaf\xe2\x82")) == {"\ufffd"}


def test_valid_utf8_bytes_are_sanitized_like_text() -> None:
    assert sanitize_bytes("a\u202eb\r\nc".encode()) == "a<U+202E>b\nc"


def test_bytes_nul_and_escape_become_markers() -> None:
    assert sanitize_bytes(b"\x00\x1b[31m") == "<U+0000><U+001B>[31m"


def test_bytes_diff_mode_keeps_cr_before_lf() -> None:
    assert sanitize_bytes(b"+a\r\n+b\rc", diff=True) == "+a\r\n+b<U+000D>c"


def test_bom_is_marked() -> None:
    assert sanitize_bytes(b"\xef\xbb\xbfx") == "<U+FEFF>x"


# --- regression: control characters ------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "\x1b[2J\x1b[H",
        "\x1b]0;title\x07",
        "\x08\x08\x08",
        "\x00\x01\x02",
        "\x7f",
        "\x9b31m",
        "\x84",
    ],
)
def test_regression_control_characters_never_reach_a_reader_raw(raw: str) -> None:
    for fn in (sanitize_text, sanitize_line, sanitize_diff):
        out = fn(raw)
        assert not any(ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 for c in out)


# --- properties ---------------------------------------------------------------------------------

POOL = [
    *[chr(cp) for cp in ALL_MARKED],
    "\t",
    "\n",
    "\r",
    "\r\n",
    "a",
    "b",
    " ",
    "/",
    "<",
    ">",
    "U",
    "+",
    "0",
    "\u00e9",
    "\U0001f600",
]
# A fixed seed keeps the property tests deterministic.
_RNG = random.Random(6)
SAMPLES = ["".join(_RNG.choice(POOL) for _ in range(_RNG.randint(0, 40))) for _ in range(400)]
_MARKER_RE = re.compile(r"<U\+[0-9A-F]{4,6}>")


_ALL = frozenset(ALL_MARKED)


def _unsafe(ch: str) -> bool:
    return ord(ch) in _ALL or ch == "\r"


@pytest.mark.parametrize("fn", [sanitize_text, sanitize_line, sanitize_diff])
def test_property_idempotent(fn) -> None:
    for s in SAMPLES:
        once = fn(s)
        assert fn(once) == once


@pytest.mark.parametrize("fn", [sanitize_text, sanitize_line])
def test_property_output_has_no_unsafe_character(fn) -> None:
    for s in SAMPLES:
        residue = _MARKER_RE.sub("", fn(s))
        assert not any(_unsafe(c) for c in residue), repr(s)


def test_property_one_line_output_has_no_newline_or_tab() -> None:
    for s in SAMPLES:
        out = sanitize_line(s)
        assert "\n" not in out and "\t" not in out and "\r" not in out


def test_property_line_count_holds() -> None:
    for s in SAMPLES:
        # Both a CRLF and a lone LF are one line break in the output.
        lf_like = s.count("\n")
        assert sanitize_text(s).count("\n") == lf_like
        assert sanitize_diff(s).count("\n") == lf_like
