"""Human output of commands with multi-line text fields (`compare`, `show`, `export`)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from forgectl import registry
from forgectl.cli import main
from forgectl.humanactions import Tty
from forgectl.output import emit_success, format_human, sanitize_tree
from forgectl.registry import Invocation

from .test_export import SHA, put, record

NO_TTY = Tty(stdin=False, stderr=False)
HOSTILE = "a\n\x1b[31mred\x1b[0m\n‮bidi⁦\r\nlone\rcr\n\ud800\n"
FORBIDDEN = ("\x1b", "\r", "‮", "⁦", "\ud800")


def printed(capsys: pytest.CaptureFixture[str], data: dict[str, Any], command: str | None) -> str:
    emit_success(data, json_mode=False, command=command)
    return capsys.readouterr().out


def no_raw_controls(text: str) -> None:
    for bad in FORBIDDEN:
        assert bad not in text
    assert all(ch in "\n\t" or ch >= " " and ch != "\x7f" for ch in text)


COMPARE = {
    "pr": "github.com/o/r#7",
    "from_sha": "a" * 40,
    "to_sha": "b" * 40,
    "status": "ahead",
}


def test_compare_prints_the_diff_as_an_indented_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    diff = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n"
    out = printed(capsys, {**COMPARE, "diff": diff}, "compare")
    assert out.splitlines() == [
        "pr: github.com/o/r#7",
        f"from_sha: {'a' * 40}",
        f"to_sha: {'b' * 40}",
        "status: ahead",
        "diff:",
        "    diff --git a/f b/f",
        "    --- a/f",
        "    +++ b/f",
        "    @@ -1 +1 @@",
        "    -old",
        "    +new",
    ]


def test_compare_block_keeps_the_diffs_line_count(capsys: pytest.CaptureFixture[str]) -> None:
    for diff in ("x", "x\n", "x\n\n", "\n", "a\n\nb"):
        out = printed(capsys, {**COMPARE, "diff": diff}, "compare")
        block = out.split("diff:\n", 1)[1]
        assert len(block.splitlines()) == len(diff.split("\n")) - diff.endswith("\n")


def test_compare_hostile_diff_has_no_raw_controls_and_cannot_forge_a_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    diff = "+status: merged\n+\x1b[2J\r\n+bidi ‮\n+lone \ud800 \r\n"
    out = printed(capsys, {**COMPARE, "diff": diff}, "compare")
    no_raw_controls(out)
    top = [line for line in out.splitlines() if not line.startswith(" ")]
    assert top == [
        "pr: github.com/o/r#7",
        f"from_sha: {'a' * 40}",
        f"to_sha: {'b' * 40}",
        "status: ahead",
        "diff:",
    ]
    assert "    +\x1b" not in out and "<U+001B>[2J<U+000D>" in out
    assert "<U+202E>" in out and "<U+D800>" in out


@pytest.mark.parametrize(
    ("diff", "line"), [(None, "diff: null"), ("", "diff: ")], ids=["null", "empty"]
)
def test_compare_without_diff_text_stays_one_line(
    capsys: pytest.CaptureFixture[str], diff: str | None, line: str
) -> None:
    status = "unavailable" if diff is None else "identical"
    out = printed(capsys, {**COMPARE, "status": status, "diff": diff}, "compare")
    assert out.splitlines()[-1] == line


def test_compare_without_a_diff_key_prints_the_other_fields_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = printed(capsys, dict(COMPARE), "compare")
    assert "diff" not in out
    assert out.splitlines()[-1] == f"status: {COMPARE['status']}"


def test_show_prints_a_multiline_body_as_a_block_and_keeps_the_title_on_one_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = {
        "pr": "github.com/o/r#7",
        "title": "one\ntwo\x1b",
        "body": HOSTILE,
        "labels": ["a\nb"],
        "author": {"login": "u\n", "id": "1", "is_bot": False},
    }
    out = printed(capsys, data, "show")
    no_raw_controls(out)
    lines = out.splitlines()
    assert lines == [
        "pr: github.com/o/r#7",
        "title: one<U+000A>two<U+001B>",
        "body:",
        "    a",
        "    <U+001B>[31mred<U+001B>[0m",
        "    <U+202E>bidi<U+2066><U+000D>",
        "    lone<U+000D>cr",
        "    <U+D800>",
        "labels: " + json.dumps(["a\nb"]),
        "author: " + json.dumps({"login": "u\n", "id": "1", "is_bot": False}),
    ]


def test_show_body_null_and_empty_stay_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert printed(capsys, {"pr": "p", "body": None}, "show").splitlines()[-1] == "body: null"
    assert printed(capsys, {"pr": "p", "body": ""}, "show").splitlines()[-1] == "body: "


def test_a_key_that_is_not_text_stays_one_line_even_with_line_feeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = printed(capsys, {"pr": "p", "diff": "a\nb"}, "show")  # `diff` is not show's text field
    assert out == "pr: p\ndiff: a<U+000A>b\n"


def test_other_commands_keep_the_generic_form(capsys: pytest.CaptureFixture[str]) -> None:
    out = printed(capsys, {"pr": "p", "body": "a\nb"}, "comments")
    assert out == "pr: p\nbody: a<U+000A>b\n"


def test_format_human_without_text_fields_is_unchanged() -> None:
    data = {"k": "v\x1b\n", "n": 3, "t": {"a": "b\n"}}
    nested = json.dumps(sanitize_tree(data["t"]))
    assert format_human(data) == f"k: v<U+001B><U+000A>\nn: 3\nt: {nested}"


def test_json_output_is_unchanged_by_the_human_form(capsys: pytest.CaptureFixture[str]) -> None:
    data = {**COMPARE, "diff": "a\nb\n"}
    emit_success(data, json_mode=True, command="compare")
    assert json.loads(capsys.readouterr().out) == data


def test_export_human_output_is_jsonl_records_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = Path(os.environ["XDG_STATE_HOME"]) / "forgectl"
    evil = "x\n\x1b[31m‮y\r\ud800"
    first = record("2026-01-01T00:00:00Z")
    first["inputs"]["base_ref"] = evil
    put(state, first, record("2026-01-02T00:00:00Z"))
    assert main(["export"], tty=NO_TTY) == 0
    out = capsys.readouterr().out
    assert out.isascii()
    lines = out.split("\n")
    assert len(lines) == 3 and lines[2] == ""
    parsed = [json.loads(line) for line in lines[:2]]
    assert parsed[0]["inputs"]["base_ref"] == "x<U+000A><U+001B>[31m<U+202E>y<U+000D><U+D800>"
    assert parsed[1]["inputs"]["head_sha"] == SHA
    assert main(["--json", "export"], tty=NO_TTY) == 0
    assert json.loads(capsys.readouterr().out) == parsed


def fake_handlers(monkeypatch: pytest.MonkeyPatch, command: str, result: dict[str, Any]) -> None:
    def handler(inv: Invocation) -> dict[str, Any]:
        return result

    monkeypatch.setattr(registry, "HANDLERS", {command: handler})
    monkeypatch.setattr(registry, "load_handlers", lambda: None)


def test_main_show_prints_the_body_as_a_block_that_cannot_forge_a_field(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_handlers(monkeypatch, "show", {"title": "t", "body": "hello\nstatus: forged\nend\n"})
    assert main(["show", "github.com/o/r#7"], tty=NO_TTY) == 0
    assert capsys.readouterr().out.splitlines() == [
        "title: t",
        "body:",
        "    hello",
        "    status: forged",
        "    end",
    ]


def test_main_compare_prints_the_diff_as_a_block_that_cannot_forge_a_field(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    diff = "@@ -1 +1 @@\n-old\nstatus: forged\n"
    fake_handlers(monkeypatch, "compare", {**COMPARE, "diff": diff})
    argv = ["compare", "github.com/o/r#7", "--from", "a" * 40, "--to", "b" * 40]
    assert main(argv, tty=NO_TTY) == 0
    lines = capsys.readouterr().out.splitlines()
    block = ["    @@ -1 +1 @@", "    -old", "    status: forged"]
    assert lines[lines.index("diff:") + 1 :] == block
    assert "status: forged" not in lines
