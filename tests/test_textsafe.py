from __future__ import annotations

import pytest

from forgectl.textsafe import sanitize_bytes, sanitize_line, sanitize_text

# Written out from the interface's "Sanitizing" section, not derived from the code under test.
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


@pytest.mark.parametrize("code_point", EXPLICIT, ids=lambda cp: f"U+{cp:04X}")
def test_every_listed_code_point_becomes_a_marker(code_point: int) -> None:
    assert sanitize_text(f"a{chr(code_point)}b") == f"a<U+{code_point:04X}>b"


@pytest.mark.parametrize("code_point", [0x1B, 0x07, 0x7F, 0x85, 0x200B, 0x202E, 0x2028, 0x2029])
def test_control_format_and_separator_characters_become_markers(code_point: int) -> None:
    assert sanitize_text(chr(code_point)) == f"<U+{code_point:04X}>"


@pytest.mark.parametrize("code_point", [0xD800, 0xDCFF, 0xE000, 0x0378, 0xFFFF])
def test_surrogate_private_use_and_unassigned_become_markers(code_point: int) -> None:
    assert sanitize_text(chr(code_point)) == f"<U+{code_point:04X}>"


def test_tab_and_newline_stay_in_text() -> None:
    assert sanitize_text("a\tb\nc") == "a\tb\nc"


def test_crlf_becomes_lf_and_a_lone_cr_a_marker() -> None:
    assert sanitize_text("a\r\nb\rc") == "a\nb<U+000D>c"


def test_ordinary_text_is_untouched() -> None:
    assert sanitize_text("héllo wörld 😀 日本") == "héllo wörld 😀 日本"


def test_sanitize_line_marks_tab_and_newline_too() -> None:
    assert sanitize_line("a\tb\nc\r\nd") == "a<U+0009>b<U+000A>c<U+000A>d"


def test_bytes_that_are_not_utf8_become_replacement_characters() -> None:
    assert sanitize_bytes(b"a\xffb\xc3") == "a�b�"


def test_bytes_pass_through_the_markers_too() -> None:
    assert sanitize_bytes("x‮y".encode()) == "x<U+202E>y"
