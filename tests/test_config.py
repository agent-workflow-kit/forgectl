from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from forgectl.config import (
    DEFAULT_TIMEOUT,
    MAX_FILE_BYTES,
    MAX_NODES,
    Config,
    LoadedConfig,
    config_hash,
    load_config,
)
from forgectl.errors import ForgectlError

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.yaml"
SCHEMA = json.loads((ROOT / "schemas" / "config.json").read_text(encoding="utf-8"))
SCHEMA_VALIDATOR = Draft202012Validator(SCHEMA)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "h"
    path.mkdir()
    return path


def env_for(home: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(home), **extra}


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def default_file(home: Path) -> Path:
    return home / ".config" / "forgectl" / "config.yaml"


def load(home: Path, text: str | None = None, **env: str) -> LoadedConfig:
    if text is not None:
        write(default_file(home), text)
    return load_config(None, env=env_for(home, **env))


def invalid(home: Path, text: str | None, **env: str) -> ForgectlError:
    with pytest.raises(ForgectlError) as exc:
        load(home, text, **env)
    assert exc.value.code == "config_invalid"
    assert exc.value.exit_code == 2
    assert isinstance(exc.value.details, dict) and "key" in exc.value.details
    return exc.value


# --- defaults equal config.example.yaml ----------------------------------------------------------


def example_data() -> dict[str, Any]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def test_defaults_equal_the_example_file(home: Path) -> None:
    from_example = load_config(str(EXAMPLE), env=env_for(home)).config
    assert from_example == Config()
    assert from_example.model_dump(by_alias=True) == Config().model_dump(by_alias=True)


def test_example_keys_are_exactly_the_models_keys() -> None:
    assert set(example_data()) == set(Config.model_fields)
    assert set(example_data()["me"]) == {"name", "github", "gitlab"}


def test_example_values_equal_the_schema_defaults() -> None:
    props = SCHEMA["properties"]
    data = example_data()
    for key in ("gh_bin", "glab_bin", "retries", "hosts", "repos"):
        assert data[key] == props[key]["default"], key
    assert data["timeout"] == props["timeout"]["default"] == "60s"
    assert data["hooks"] == props["hooks"]["default"] == {}
    assert data["activity"] == props["activity"]["default"] == {}
    assert data["browse"] == props["browse"]["default"] == {}
    browse_props = props["browse"]["properties"]
    assert browse_props["refresh_interval"]["default"] == "120s"
    assert browse_props["full_refresh_every"]["default"] == 5
    assert browse_props["concurrency"]["default"] == 4
    assert props["activity"]["properties"]["ignore_authors"]["default"] == []
    for key, prop in props["me"]["properties"].items():
        assert data["me"][key] == prop["default"], key


def test_activity_ignore_authors_loads_and_defaults_to_empty(home: Path) -> None:
    assert load(home, "").config.activity.ignore_authors == ()
    text = 'activity:\n  ignore_authors: ["dependabot[bot]", "renovate*"]\n'
    assert load(home, text).config.activity.ignore_authors == ("dependabot[bot]", "renovate*")


@pytest.mark.parametrize(
    "text",
    [
        "activity:\n  ignore_authors: renovate\n",
        "activity:\n  ignore_authors: [1]\n",
        'activity:\n  ignore_authors: [""]\n',
        'activity:\n  ignore_authors: ["a\\u0007b"]\n',
        "activity:\n  ignore_bots: true\n",
    ],
)
def test_activity_refuses_a_bad_ignore_list(home: Path, text: str) -> None:
    with pytest.raises(ForgectlError) as caught:
        load(home, text)
    assert caught.value.code == "config_invalid"


def test_browse_defaults_and_values(home: Path) -> None:
    browse = load(home, "").config.browse
    assert (browse.refresh_interval, browse.full_refresh_every, browse.concurrency) == (120.0, 5, 4)
    text = "browse: {refresh_interval: 30s, full_refresh_every: 3, concurrency: 8}\n"
    browse = load(home, text).config.browse
    assert (browse.refresh_interval, browse.full_refresh_every, browse.concurrency) == (30.0, 3, 8)


@pytest.mark.parametrize("value", ["1", "1s", "1000ms", "0.5m", "'1.5'"])
def test_browse_refresh_interval_accepts_one_second_and_more(home: Path, value: str) -> None:
    assert (
        load(home, f"browse: {{refresh_interval: {value}}}\n").config.browse.refresh_interval >= 1
    )


def test_browse_refresh_interval_floor_says_why(home: Path) -> None:
    err = invalid(home, "browse: {refresh_interval: 1ms}\n")
    assert "at least 1" in err.message and err.details == {"key": "browse.refresh_interval"}


@pytest.mark.parametrize("value", ["0", "0.0"])
def test_browse_refresh_interval_accepts_zero_to_disable(home: Path, value: str) -> None:
    assert (
        load(home, f"browse: {{refresh_interval: {value}}}\n").config.browse.refresh_interval == 0
    )


@pytest.mark.parametrize(
    "text",
    [
        "browse: {refresh_interval: -1}\n",
        "browse: {refresh_interval: abc}\n",
        'browse: {refresh_interval: "0"}\n',
        "browse: {refresh_interval: true}\n",
        "browse: {refresh_interval: 0s}\n",
        "browse: {refresh_interval: 1ms}\n",
        "browse: {refresh_interval: 0.5}\n",
        "browse: {refresh_interval: 999ms}\n",
        "browse: {full_refresh_every: 0}\n",
        "browse: {full_refresh_every: 2.5}\n",
        "browse: {full_refresh_every: 1001}\n",
        "browse: {concurrency: 0}\n",
        "browse: {concurrency: 17}\n",
        "browse: {concurrency: true}\n",
        "browse: {interval: 5}\n",
    ],
)
def test_browse_refuses_bad_values(home: Path, text: str) -> None:
    with pytest.raises(ForgectlError) as caught:
        load(home, text)
    assert caught.value.code == "config_invalid"


def test_default_timeout_is_sixty_seconds() -> None:
    assert Config().timeout == DEFAULT_TIMEOUT == 60.0


def uncommented(key: str) -> str:
    """The commented example block that follows the line ``<key>: {}`` in the example file."""
    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"{key}: {{}}") + 1
    block: list[str] = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        block.append(line[2:] if line.startswith("# ") else line[1:])
    assert block and block[0] == f"{key}:"
    return "\n".join(block) + "\n"


def test_the_commented_examples_in_the_example_file_are_valid(home: Path) -> None:
    text = "".join(uncommented(key) for key in ("hosts", "repos", "hooks", "activity", "browse"))
    config = load(home, text).config
    assert (config.browse.refresh_interval, config.browse.full_refresh_every) == (120.0, 5)
    assert config.browse.concurrency == 4
    assert config.hosts["gitlab.example.com"].backend == "gitlab"
    assert config.hosts["gitlab.example.com"].api_prefix == "/gitlab"
    assert config.hosts["gitlab.example.com"].token_command == (
        "example-secret-tool",
        "lookup",
        "{host}",
    )
    assert config.repos["gitlab.example.com/group/project"].merge_method == "squash"
    assert [h.cmd for h in config.hooks.post_merge] == ["example-notify"]
    assert config.hooks.post_merge[0].async_ is True
    assert config.hooks.pre_merge[0].timeout == 30.0


# --- locating and reading files ------------------------------------------------------------------


def test_no_config_file_means_defaults(home: Path) -> None:
    loaded = load_config(None, env=env_for(home))
    assert loaded.config == Config()
    assert loaded.config_path is None


def test_default_location_is_under_home(home: Path) -> None:
    write(default_file(home), "retries: 5\n")
    assert load_config(None, env=env_for(home)).config.retries == 5


def test_xdg_config_home_is_respected(home: Path, tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    write(xdg / "forgectl" / "config.yaml", "retries: 6\n")
    write(default_file(home), "retries: 1\n")
    assert load_config(None, env=env_for(home, XDG_CONFIG_HOME=str(xdg))).config.retries == 6


@pytest.mark.parametrize("value", ["", "relative/dir"])
def test_an_empty_or_relative_xdg_config_home_is_ignored(home: Path, value: str) -> None:
    write(default_file(home), "retries: 1\n")
    assert load_config(None, env=env_for(home, XDG_CONFIG_HOME=value)).config.retries == 1


def test_drop_ins_alone_are_read_without_a_main_file(home: Path) -> None:
    write(default_file(home).parent / "config.d" / "a.yaml", "retries: 2\n")
    assert load(home).config.retries == 2


def test_a_drop_in_replaces_the_ignore_list_rather_than_appending(home: Path) -> None:
    write(default_file(home).parent / "config.d" / "a.yaml", "activity:\n  ignore_authors: [b]\n")
    config = load(home, "activity:\n  ignore_authors: [a, c]\n").config
    assert config.activity.ignore_authors == ("b",)


def test_an_empty_file_and_a_comment_only_file_are_defaults(home: Path) -> None:
    assert load(home, "").config == Config()
    assert load(home, "# nothing\n").config == Config()
    assert load(home, "---\n").config == Config()


def test_config_flag_names_the_file_and_reads_its_drop_ins(home: Path, tmp_path: Path) -> None:
    other = write(tmp_path / "elsewhere" / "my.yaml", "retries: 7\ngh_bin: gh1\n")
    write(other.parent / "config.d" / "x.yaml", "gh_bin: gh2\n")
    write(default_file(home), "retries: 1\n")  # the default is not read at all
    loaded = load_config(str(other), env=env_for(home))
    assert (loaded.config.retries, loaded.config.gh_bin) == (7, "gh2")
    assert loaded.config_path == str(other)


def test_config_flag_with_a_missing_file_is_config_invalid(home: Path, tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    write(tmp_path / "config.d" / "x.yaml", "retries: 2\n")
    with pytest.raises(ForgectlError) as exc:
        load_config(str(missing), env=env_for(home))
    assert exc.value.code == "config_invalid"
    assert exc.value.details == {"key": str(missing)}


def test_config_flag_with_a_directory_is_config_invalid(home: Path, tmp_path: Path) -> None:
    with pytest.raises(ForgectlError) as exc:
        load_config(str(tmp_path), env=env_for(home))
    assert exc.value.code == "config_invalid"


def test_a_fifo_is_refused_without_hanging(home: Path) -> None:
    path = default_file(home)
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    assert invalid(home, None).details == {"key": str(path)}


def test_a_fifo_drop_in_is_refused_without_hanging(home: Path) -> None:
    path = default_file(home).parent / "config.d" / "a.yaml"
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    assert invalid(home, None).details == {"key": str(path)}


def test_a_symlink_to_a_regular_file_is_followed(home: Path, tmp_path: Path) -> None:
    target = write(tmp_path / "dotfiles" / "forgectl.yaml", "retries: 4\n")
    link = default_file(home)
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    assert load(home).config.retries == 4


def test_a_symlink_to_a_directory_or_a_dangling_link_is_refused(home: Path, tmp_path: Path) -> None:
    drop = default_file(home).parent / "config.d"
    drop.mkdir(parents=True)
    (drop / "a.yaml").symlink_to(tmp_path)
    invalid(home, None)
    (drop / "a.yaml").unlink()
    (drop / "a.yaml").symlink_to(tmp_path / "gone.yaml")
    invalid(home, None)


def test_a_dangling_default_config_file_is_refused_not_absent(home: Path, tmp_path: Path) -> None:
    link = default_file(home)
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "gone.yaml")
    assert invalid(home, None).details == {"key": str(link)}


def test_a_dangling_config_d_is_refused(home: Path, tmp_path: Path) -> None:
    drop = default_file(home).parent / "config.d"
    drop.parent.mkdir(parents=True)
    drop.symlink_to(tmp_path / "gone")
    assert invalid(home, None).details == {"key": str(drop)}


def test_a_config_d_that_is_a_file_or_a_fifo_is_refused(home: Path) -> None:
    drop = default_file(home).parent / "config.d"
    write(drop, "retries: 2\n")
    assert invalid(home, None).details == {"key": str(drop)}
    drop.unlink()
    os.mkfifo(drop)
    assert invalid(home, None).details == {"key": str(drop)}


def test_a_config_home_that_is_a_file_means_defaults(home: Path, tmp_path: Path) -> None:
    xdg = write(tmp_path / "xdg-file", "not a directory\n")
    assert load_config(None, env=env_for(home, XDG_CONFIG_HOME=str(xdg))).config == Config()
    write(home / ".config", "not a directory\n")
    assert load_config(None, env=env_for(home)).config == Config()


def test_a_directory_named_like_a_drop_in_is_refused(home: Path) -> None:
    (default_file(home).parent / "config.d" / "a.yaml").mkdir(parents=True)
    invalid(home, None)


def test_an_oversized_file_is_refused_unparsed(home: Path) -> None:
    text = "me:\n  name: x\n" + "# " + "a" * (300 * 1024) + "\n"
    err = invalid(home, text)
    assert "too large" in err.message


def test_an_oversized_drop_in_is_refused(home: Path) -> None:
    write(default_file(home).parent / "config.d" / "a.yaml", "#" + "a" * (300 * 1024))
    assert "too large" in invalid(home, None).message


def test_too_many_drop_ins_are_refused(home: Path) -> None:
    drop = default_file(home).parent / "config.d"
    for i in range(300):
        write(drop / f"{i:04}.yaml", "")
    assert "too many" in invalid(home, None).message


def test_invalid_utf8_is_refused(home: Path) -> None:
    path = default_file(home)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"gh_bin: \xff\xfe\n")
    assert invalid(home, None).details == {"key": str(path)}


def test_a_utf8_byte_order_mark_is_accepted(home: Path) -> None:
    path = default_file(home)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbfretries: 2\n")
    assert load(home).config.retries == 2


# --- drop-in ordering and merging ----------------------------------------------------------------


def dropins(home: Path, files: Mapping[str, str]) -> None:
    for name, text in files.items():
        write(default_file(home).parent / "config.d" / name, text)


def test_drop_ins_apply_in_lexical_order_after_the_main_file(home: Path) -> None:
    write(default_file(home), "gh_bin: main\n")
    dropins(home, {"20-b.yaml": "gh_bin: b\n", "10-a.yaml": "gh_bin: a\n"})
    assert load(home).config.gh_bin == "b"


def test_lexical_means_by_code_point_not_numerically_or_by_case(home: Path) -> None:
    dropins(home, {"2.yaml": "retries: 2\n", "10.yaml": "retries: 10\n"})
    assert load(home).config.retries == 2  # "10.yaml" sorts before "2.yaml"
    dropins(home, {"B.yaml": "gh_bin: upper\n", "a.yaml": "gh_bin: lower\n"})
    assert load(home).config.gh_bin == "lower"  # "B" (0x42) sorts before "a" (0x61)


def test_only_yaml_files_that_are_not_hidden_are_read(home: Path) -> None:
    dropins(
        home,
        {
            "a.yml": "retries: 9\n",
            "a.yaml.bak": "retries: 9\n",
            "a.yaml~": "retries: 9\n",
            ".hidden.yaml": "retries: 9\n",
            "README": "not: [valid",
        },
    )
    (default_file(home).parent / "config.d" / ".#lock.yaml").symlink_to("/nonexistent/x")
    assert load(home).config.retries == 3


def test_maps_merge_across_files(home: Path) -> None:
    write(default_file(home), "me: {name: jo}\nhosts:\n  h.example.com: {backend: gitlab}\n")
    dropins(
        home,
        {
            "a.yaml": "me: {github: jo-gh}\nhosts:\n  h.example.com: {token_env: TOK}\n",
            "b.yaml": "hosts:\n  other.example.com: {backend: github}\n",
        },
    )
    config = load(home).config
    assert (config.me.name, config.me.github) == ("jo", "jo-gh")
    assert config.hosts["h.example.com"].backend == "gitlab"
    assert config.hosts["h.example.com"].token_env == "TOK"
    assert config.hosts["other.example.com"].backend == "github"


def test_hook_lists_append_in_file_order(home: Path) -> None:
    write(default_file(home), "hooks:\n  pre-merge:\n    - cmd: one\n")
    dropins(
        home,
        {
            "a.yaml": "hooks:\n  pre-merge:\n    - cmd: two\n  post-merge:\n    - cmd: p1\n",
            "b.yaml": "hooks:\n  pre-merge:\n    - cmd: three\n    - cmd: four\n"
            "  post-merge:\n    - cmd: p2\n      async: true\n",
        },
    )
    hooks = load(home).config.hooks
    assert [h.cmd for h in hooks.pre_merge] == ["one", "two", "three", "four"]
    assert [h.cmd for h in hooks.post_merge] == ["p1", "p2"]


def test_other_lists_replace(home: Path) -> None:
    write(default_file(home), "hosts:\n  h.example.com: {backend: gitlab, token_command: [a, b]}\n")
    dropins(home, {"a.yaml": "hosts:\n  h.example.com: {token_command: [c]}\n"})
    assert load(home).config.hosts["h.example.com"].token_command == ("c",)


def test_a_later_null_replaces_an_earlier_value(home: Path) -> None:
    write(default_file(home), "me: {name: jo}\n")
    dropins(home, {"a.yaml": "me: {name: null}\n"})
    assert load(home).config.me.name is None


def test_a_later_file_may_replace_a_map_with_a_scalar_and_validation_then_refuses(
    home: Path,
) -> None:
    write(default_file(home), "me: {name: jo}\n")
    dropins(home, {"a.yaml": "me: nobody\n"})
    with pytest.raises(ForgectlError) as exc:
        load(home)
    assert exc.value.details == {"key": "me"}


def test_a_hook_list_replaced_by_a_non_list_is_refused(home: Path) -> None:
    write(default_file(home), "hooks:\n  pre-merge:\n    - cmd: one\n")
    dropins(home, {"a.yaml": "hooks:\n  pre-merge:\n"})
    with pytest.raises(ForgectlError) as exc:
        load(home)
    assert exc.value.details == {"key": "hooks.pre-merge"}


def test_a_partial_drop_in_is_valid_only_with_the_main_file(home: Path) -> None:
    dropins(home, {"a.yaml": "hosts:\n  h.example.com: {token_env: TOK}\n"})
    assert invalid(home, None).details == {"key": "hosts.h.example.com.backend"}


# --- hostile YAML --------------------------------------------------------------------------------

FILE = None  # the refusal names the file, not a key
BIG_INT = "9" * 5000  # beyond Python's int digit limit

HOSTILE_YAML: dict[str, tuple[str, str | None]] = {
    "anchor": ("me: &a {name: jo}\n", FILE),
    "alias": ("x: &a 1\nretries: *a\n", FILE),
    "alias bomb": ("a: &a [1,1]\nb: &b [*a,*a]\nc: &c [*b,*b]\nd: [*c,*c]\n", FILE),
    "python tag": ("gh_bin: !!python/object/apply:os.system ['id']\n", FILE),
    "custom tag": ("gh_bin: !secret x\n", FILE),
    "binary": ("gh_bin: !!binary aGk=\n", FILE),
    "set": ("me: !!set {name}\n", FILE),
    "timestamp tag": ("me: {name: !!timestamp 2024-01-01}\n", FILE),
    "merge key": ("me: {<<: {name: jo}}\n", FILE),
    "explicit merge tag": ("me: {!!merge x: {name: jo}}\n", FILE),
    "duplicate key": ("retries: 1\nretries: 2\n", "retries"),
    "nested duplicate key": ("me:\n  name: a\n  name: b\n", "me.name"),
    "duplicate hook name": ("hooks:\n  pre-merge: []\n  pre-merge: []\n", "hooks.pre-merge"),
    "integer key": ("1: x\n", FILE),
    "bool key": ("true: x\n", FILE),
    "null key": ("~: x\n", FILE),
    "complex key": ("? [a]\n: x\n", FILE),
    "two documents": ("retries: 1\n---\nretries: 2\n", FILE),
    "top-level list": ("- a\n- b\n", FILE),
    "top-level scalar": ("just text\n", FILE),
    "syntax error": ("me: {name: jo\n", FILE),
    "tab indent": ("me:\n\tname: jo\n", FILE),
    "nul escape": ('me: {name: "a\\0b"}\n', "me.name"),
    "nul in command": ('hooks:\n  pre-merge:\n    - cmd: "a\\0b"\n', "hooks.pre-merge.0.cmd"),
    "nul in a nested key": ('me: {"a\\0": 1}\n', "me"),
    "nul in a top-level key": ('"a\\0": 1\n', FILE),
    "surrogate in command": (
        'hooks:\n  pre-merge:\n    - cmd: "echo \\ud800"\n',
        "hooks.pre-merge.0.cmd",
    ),
    "surrogate in gh_bin": ('gh_bin: "\\ud800"\n', "gh_bin"),
    "surrogate in a top-level key": ('"\\udfff": 1\n', FILE),
    "directive abuse": ("%TAG ! tag:evil,2000:\n---\nretries: !x 1\n", FILE),
    "deep nesting": ("a: " + "[" * 5000 + "]" * 5000 + "\n", FILE),
    "control character": ("me: {name: \x01}\n", FILE),
    "int tag on text": ("retries: !!int abc\n", FILE),
    "int tag on 0x": ("retries: !!int 0x\n", FILE),
    "int tag on sexagesimal": ("retries: !!int '1::'\n", FILE),
    "int tag on empty": ("retries: !!int ''\n", FILE),
    "int tag on a sign": ("retries: !!int '-'\n", FILE),
    "int tag on an underscore": ("retries: !!int '_'\n", FILE),
    "int tag on hex": ("retries: !!int 0x3\n", FILE),
    "int tag on a list": ("retries: !!int [1]\n", FILE),
    "float tag on text": ("timeout: !!float abc\n", FILE),
    "float tag on a unit": ("timeout: !!float 30s\n", FILE),
    "float tag on empty": ("timeout: !!float ''\n", FILE),
    "bool tag on maybe": (
        "hooks:\n  post-merge:\n    - cmd: x\n      async: !!bool maybe\n",
        FILE,
    ),
    "bool tag on empty": ("hooks:\n  post-merge:\n    - {cmd: x, async: !!bool ''}\n", FILE),
    "huge int": (f"retries: {BIG_INT}\n", FILE),
    "huge int nested": (f"hooks:\n  pre-merge:\n    - {{cmd: x, timeout: {BIG_INT}}}\n", FILE),
    "huge int as a key": (f"{BIG_INT}: x\n", FILE),
}


@pytest.mark.parametrize(("text", "key"), HOSTILE_YAML.values(), ids=HOSTILE_YAML.keys())
def test_hostile_yaml_is_config_invalid_never_a_traceback(
    home: Path, text: str, key: str | None
) -> None:
    err = invalid(home, text)
    assert err.details == {"key": str(default_file(home)) if key is FILE else key}


def test_a_yaml_error_names_the_line(home: Path) -> None:
    err = invalid(home, "retries: 1\nretries: !!int abc\n")
    assert "line 2" in err.message
    assert "abc" not in err.message


@pytest.mark.parametrize(
    "text",
    [
        "gh_bin: !SECRETXYZ x\n",
        "gh_bin: !SECRETXYZ!x y\n",
        "%TAG !e! tag:SECRETXYZ,2000:\n---\ngh_bin: !e!x y\n",
        "retries: !!int SECRETXYZ\n",
        "a: &SECRETXYZ 1\n",
    ],
)
def test_a_yaml_error_never_echoes_tags_handles_or_directives(home: Path, text: str) -> None:
    err = invalid(home, text)
    assert err.details == {"key": str(default_file(home))}
    assert "SECRETXYZ" not in err.message


def test_too_many_nodes_are_refused_quickly(home: Path) -> None:
    items = MAX_NODES - 3  # the root map, the key, the list: exactly at the cap
    at_cap = invalid(home, "x: [" + "1," * (items - 1) + "1]\n")
    assert at_cap.details == {"key": "x"}  # parsed; refused only as an unknown key
    started = time.monotonic()
    over = invalid(home, "x: [" + "1," * items + "1]\n")
    assert over.details == {"key": str(default_file(home))}
    assert "too many nodes" in over.message
    assert time.monotonic() - started < 2.0
    huge = invalid(home, "x: [" + "1," * (MAX_FILE_BYTES // 2 - 10) + "1]\n")
    assert "too many nodes" in huge.message


def test_the_example_file_is_far_under_the_node_cap() -> None:
    def count(node: yaml.Node) -> int:
        if isinstance(node, yaml.MappingNode):
            return 1 + sum(count(k) + count(v) for k, v in node.value)
        if isinstance(node, yaml.SequenceNode):
            return 1 + sum(count(item) for item in node.value)
        return 1

    examples = "".join(uncommented(key) for key in ("hosts", "repos", "hooks"))
    for text in (EXAMPLE.read_text(encoding="utf-8"), examples):
        assert count(yaml.compose(text)) * 20 < MAX_NODES


def test_a_yaml_error_names_the_file_and_the_line_but_never_echoes_content(home: Path) -> None:
    err = invalid(home, "retries: 1\nme: {name: SECRET-VALUE\n")
    assert err.details == {"key": str(default_file(home))}
    assert "SECRET-VALUE" not in err.message
    assert "line 3" in err.message or "line 2" in err.message


def test_a_duplicate_key_message_names_the_key(home: Path) -> None:
    err = invalid(home, "retries: 1\nretries: 2\n")
    assert "retries" in err.message and "duplicate" in err.message


# YAML 1.2 core schema: only true/false are booleans, integers are decimal, the rest is text.
ASYNC = "hooks.post-merge.0.async"
RESOLVES_AS_TEXT_AND_IS_REFUSED: dict[str, tuple[str, str]] = {
    "sexagesimal timeout": ("timeout: 1:30\n", "timeout"),
    "binary retries": ("retries: 0b11\n", "retries"),
    "hex retries": ("retries: 0x3\n", "retries"),
    "octal retries": ("retries: 0o3\n", "retries"),
    "leading-zero retries": ("retries: 010\n", "retries"),
    "underscore retries": ("retries: 1_0\n", "retries"),
    "yes async": ("hooks:\n  post-merge:\n    - {cmd: x, async: yes}\n", ASYNC),
    "on async": ("hooks:\n  post-merge:\n    - {cmd: x, async: on}\n", ASYNC),
    "y async": ("hooks:\n  post-merge:\n    - {cmd: x, async: y}\n", ASYNC),
    "a YAML 1.2 directive": ("%YAML 1.2\n---\nretries: 0x3\n", "retries"),
    "underscore timeout": ("timeout: 1_000\n", "timeout"),
}


@pytest.mark.parametrize(
    ("text", "key"),
    RESOLVES_AS_TEXT_AND_IS_REFUSED.values(),
    ids=RESOLVES_AS_TEXT_AND_IS_REFUSED.keys(),
)
def test_yaml_1_1_spellings_are_text_and_then_refused(home: Path, text: str, key: str) -> None:
    assert invalid(home, text).details == {"key": key}


def test_yaml_1_2_core_resolution(home: Path) -> None:
    assert load(home, "timeout: 010\n").config.timeout == 10.0  # a numeric string: seconds
    assert load(home, "retries: +3\n").config.retries == 3
    assert load(home, "timeout: 1e2\n").config.timeout == 100.0
    for name in ("yes", "on", "off", "no", "0x1f", "010", "1_0", "2024-01-01", "1.5e3x"):
        assert load(home, f"me: {{name: {name}}}\n").config.me.name == name
    for spelling in ("true", "True", "TRUE"):
        text = f"hooks:\n  post-merge:\n    - {{cmd: x, async: {spelling}}}\n"
        assert load(home, text).config.hooks.post_merge[0].async_ is True
    for spelling in ("null", "Null", "NULL", "~", ""):
        assert load(home, f"me: {{name: {spelling}}}\n").config.me.name is None
    assert load(home, "me: {name: !!str 12}\n").config.me.name == "12"
    assert load(home, "retries: !!int 4\n").config.retries == 4
    assert load(home, "timeout: !!float 2\n").config.timeout == 2.0


def test_yaml_scalar_typing_is_not_coerced_into_validity(home: Path) -> None:
    assert invalid(home, "retries: '3'\n").details == {"key": "retries"}
    assert invalid(home, "me: {name: 12345}\n").details == {"key": "me.name"}


# --- validation, by key --------------------------------------------------------------------------

HOST = "hosts:\n  h.example.com:\n"

INVALID: dict[str, tuple[str, str]] = {
    "unknown top-level key": ("verbose: true\n", "verbose"),
    "unknown me key": ("me: {nick: x}\n", "me.nick"),
    "unknown host key": (
        HOST + "    backend: gitlab\n    tokn_env: X\n",
        "hosts.h.example.com.tokn_env",
    ),
    "unknown repo key": (
        "repos:\n  h.example.com/a/b: {allow_self: true}\n",
        "repos.h.example.com/a/b.allow_self",
    ),
    "unknown hook name": ("hooks:\n  pre-delete: []\n", "hooks.pre-delete"),
    "retries too high": ("retries: 11\n", "retries"),
    "retries negative": ("retries: -1\n", "retries"),
    "retries float": ("retries: 3.0\n", "retries"),
    "retries bool": ("retries: true\n", "retries"),
    "retries string": ("retries: '3'\n", "retries"),
    "retries null": ("retries: null\n", "retries"),
    "timeout zero": ("timeout: 0\n", "timeout"),
    "timeout zero string": ("timeout: '0s'\n", "timeout"),
    "timeout zero fraction": ("timeout: '0.0'\n", "timeout"),
    "timeout negative": ("timeout: -5\n", "timeout"),
    "timeout bool": ("timeout: true\n", "timeout"),
    "timeout inf": ("timeout: .inf\n", "timeout"),
    "timeout nan": ("timeout: .nan\n", "timeout"),
    "timeout exponent": ("timeout: 1e3s\n", "timeout"),
    "timeout space": ("timeout: '5 s'\n", "timeout"),
    "timeout bad unit": ("timeout: 5w\n", "timeout"),
    "timeout no number": ("timeout: s\n", "timeout"),
    "timeout unicode digits": ("timeout: '\u0663s'\n", "timeout"),
    "timeout trailing newline": ('timeout: "5s\\n"\n', "timeout"),
    "timeout list": ("timeout: [5]\n", "timeout"),
    "timeout over a century": ("timeout: 9999999999999\n", "timeout"),
    "timeout huge int": ("timeout: " + "9" * 400 + "\n", "timeout"),
    "timeout huge float": ("timeout: 1.0e+400\n", "timeout"),
    "timeout huge numeric string": ("timeout: '" + "9" * 5000 + "s'\n", "timeout"),
    "timeout negative inf": ("timeout: -.inf\n", "timeout"),
    "me.name space": ("me: {name: 'a b'}\n", "me.name"),
    "me.name newline": ('me: {name: "ab\\n"}\n', "me.name"),
    "me.name empty": ("me: {name: ''}\n", "me.name"),
    "me.name long": (f"me: {{name: {'a' * 65}}}\n", "me.name"),
    "me.name unicode": ("me: {name: 'jos\u00e9'}\n", "me.name"),
    "me.github slash": ("me: {github: 'a/b'}\n", "me.github"),
    "me.gitlab empty": ("me: {gitlab: ''}\n", "me.gitlab"),
    "me.gitlab dotdot": ("me: {gitlab: '..'}\n", "me.gitlab"),
    "me.gitlab dot": ("me: {gitlab: '.'}\n", "me.gitlab"),
    "me.gitlab trailing dot": ("me: {gitlab: 'a.'}\n", "me.gitlab"),
    "gh_bin relative path": ("gh_bin: bin/gh\n", "gh_bin"),
    "gh_bin empty": ("gh_bin: ''\n", "gh_bin"),
    "gh_bin newline": ('gh_bin: "gh\\n"\n', "gh_bin"),
    "gh_bin dot": ("gh_bin: .\n", "gh_bin"),
    "gh_bin dotdot": ("gh_bin: ..\n", "gh_bin"),
    "gh_bin control": ('gh_bin: "g\\x1bh"\n', "gh_bin"),
    "gh_bin delete": ('gh_bin: "gh\\x7f"\n', "gh_bin"),
    "gh_bin C1 control": ('gh_bin: "gh\\x9b2J"\n', "gh_bin"),
    "glab_bin next line": ('glab_bin: "\\x85"\n', "glab_bin"),
    "gh_bin NUL": ('gh_bin: "a\\0b"\n', "gh_bin"),
    "gh_bin surrogate": ('gh_bin: "\\ud800"\n', "gh_bin"),
    "glab_bin list": ("glab_bin: [glab]\n", "glab_bin"),
    "hosts not a map": ("hosts: [a]\n", "hosts"),
    "undeclared host without backend": (HOST + "    token_env: X\n", "hosts.h.example.com.backend"),
    "host bad backend": (HOST + "    backend: bitbucket\n", "hosts.h.example.com.backend"),
    "host null entry": ("hosts:\n  h.example.com:\n", "hosts.h.example.com"),
    "built-in github.com wrong backend": (
        "hosts:\n  github.com: {backend: gitlab}\n",
        "hosts.github.com.backend",
    ),
    "built-in github.com null backend": (
        "hosts:\n  github.com: {backend: null}\n",
        "hosts.github.com.backend",
    ),
    "built-in gitlab.com wrong backend": (
        "hosts:\n  gitlab.com: {backend: github}\n",
        "hosts.gitlab.com.backend",
    ),
    "api_prefix on a built-in host": (
        "hosts:\n  gitlab.com: {api_prefix: /x}\n",
        "hosts.gitlab.com.api_prefix",
    ),
    "api_prefix null on a built-in host": (
        "hosts:\n  github.com: {api_prefix: null}\n",
        "hosts.github.com.api_prefix",
    ),
    "api_prefix on a github host": (
        HOST + "    backend: github\n    api_prefix: /x\n",
        "hosts.h.example.com.api_prefix",
    ),
    "api_prefix no slash": (
        HOST + "    backend: gitlab\n    api_prefix: gitlab\n",
        "hosts.h.example.com.api_prefix",
    ),
    "api_prefix trailing slash": (
        HOST + "    backend: gitlab\n    api_prefix: /gitlab/\n",
        "hosts.h.example.com.api_prefix",
    ),
    "api_prefix dotdot": (
        HOST + "    backend: gitlab\n    api_prefix: /a/../b\n",
        "hosts.h.example.com.api_prefix",
    ),
    "api_prefix query": (
        HOST + "    backend: gitlab\n    api_prefix: '/a?b=1'\n",
        "hosts.h.example.com.api_prefix",
    ),
    "api_prefix tilde": (
        HOST + "    backend: gitlab\n    api_prefix: '/~a'\n",
        "hosts.h.example.com.api_prefix",
    ),
    "api_prefix segment of 65": (
        HOST + "    backend: gitlab\n    api_prefix: '/" + "a" * 65 + "'\n",
        "hosts.h.example.com.api_prefix",
    ),
    "api_prefix nine segments": (
        HOST + "    backend: gitlab\n    api_prefix: '/a/b/c/d/e/f/g/h/i'\n",
        "hosts.h.example.com.api_prefix",
    ),
    "host label of 64": (
        "hosts:\n  " + "a" * 64 + ".example.com: {backend: gitlab}\n",
        "hosts." + "a" * 64 + ".example.com",
    ),
    "host name of 254": (
        "hosts:\n  " + ".".join(["a" * 50] * 5) + ": {backend: gitlab}\n",
        "hosts." + ".".join(["a" * 50] * 5),
    ),
    "github host with a port": (
        "hosts:\n  h.example.com:8443: {backend: github}\n",
        "hosts.h.example.com:8443",
    ),
    "host name uppercase": ("hosts:\n  H.Example.com: {backend: gitlab}\n", "hosts.H.Example.com"),
    "host name with userinfo": (
        "hosts:\n  'u@h.example.test': {backend: gitlab}\n",
        "hosts.u@h.example.test",
    ),
    "host name with a path": ("hosts:\n  b.com/x: {backend: gitlab}\n", "hosts.b.com/x"),
    "host name uppercase builtin": (
        "hosts:\n  GITHUB.COM: {backend: github}\n",
        "hosts.GITHUB.COM",
    ),
    "host name empty": ("hosts:\n  '': {backend: gitlab}\n", "hosts."),
    "token_env with equals": (
        HOST + "    backend: gitlab\n    token_env: 'A=B'\n",
        "hosts.h.example.com.token_env",
    ),
    "token_env empty": (
        HOST + "    backend: gitlab\n    token_env: ''\n",
        "hosts.h.example.com.token_env",
    ),
    "token_command empty list": (
        HOST + "    backend: gitlab\n    token_command: []\n",
        "hosts.h.example.com.token_command",
    ),
    "token_command empty arg": (
        HOST + "    backend: gitlab\n    token_command: ['x', '']\n",
        "hosts.h.example.com.token_command.1",
    ),
    "token_command C0 control": (
        HOST + '    backend: gitlab\n    token_command: ["a\\x01"]\n',
        "hosts.h.example.com.token_command.0",
    ),
    "token_command C1 control": (
        HOST + '    backend: gitlab\n    token_command: [x, "a\\x9b"]\n',
        "hosts.h.example.com.token_command.1",
    ),
    "token_command a string": (
        HOST + "    backend: gitlab\n    token_command: 'x y'\n",
        "hosts.h.example.com.token_command",
    ),
    "token_command non-string arg": (
        HOST + "    backend: gitlab\n    token_command: [x, 1]\n",
        "hosts.h.example.com.token_command.1",
    ),
    "host me bad": (
        HOST + "    backend: gitlab\n    me: 'a b'\n",
        "hosts.h.example.com.me",
    ),
    "host me dotdot": (
        HOST + "    backend: gitlab\n    me: '..'\n",
        "hosts.h.example.com.me",
    ),
    "host me dot": (
        HOST + "    backend: gitlab\n    me: '.'\n",
        "hosts.h.example.com.me",
    ),
    "host me trailing dot": (
        HOST + "    backend: gitlab\n    me: 'a.'\n",
        "hosts.h.example.com.me",
    ),
    "repo bad method": (
        "repos:\n  h.example.com/a/b: {merge_method: fast-forward}\n",
        "repos.h.example.com/a/b.merge_method",
    ),
    "repo method uppercase": (
        "repos:\n  h.example.com/a/b: {merge_method: Squash}\n",
        "repos.h.example.com/a/b.merge_method",
    ),
    "repo key without a path": ("repos:\n  h.example.com: {}\n", "repos.h.example.com"),
    # a host key is in the normalized form input produces, or it could never be matched
    "host key with :443": (
        "hosts:\n  h.example.com:443: {backend: gitlab}\n",
        "hosts.h.example.com:443",
    ),
    "host key leading-zero port": (
        "hosts:\n  h.example.com:08443: {backend: gitlab}\n",
        "hosts.h.example.com:08443",
    ),
    "host key port zero": (
        "hosts:\n  h.example.com:0: {backend: gitlab}\n",
        "hosts.h.example.com:0",
    ),
    "host key port too big": (
        "hosts:\n  h.example.com:65536: {backend: gitlab}\n",
        "hosts.h.example.com:65536",
    ),
    "host key www built-in": (
        "hosts:\n  www.github.com: {backend: github}\n",
        "hosts.www.github.com",
    ),
    "host key www built-in with port": (
        "hosts:\n  www.gitlab.com:8443: {backend: gitlab}\n",
        "hosts.www.gitlab.com:8443",
    ),
    "repo key host with :443": (
        "repos:\n  h.example.com:443/a/b: {}\n",
        "repos.h.example.com:443/a/b",
    ),
    "repo key host leading-zero port": (
        "repos:\n  h.example.com:065/a/b: {}\n",
        "repos.h.example.com:065/a/b",
    ),
    "repo key host port zero": ("repos:\n  h.example.com:0/a/b: {}\n", "repos.h.example.com:0/a/b"),
    "repo key host port too big": (
        "repos:\n  h.example.com:99999/a/b: {}\n",
        "repos.h.example.com:99999/a/b",
    ),
    "repo key www built-in": ("repos:\n  www.github.com/o/r: {}\n", "repos.www.github.com/o/r"),
    "repo key dotdot": ("repos:\n  h.example.com/a/..: {}\n", "repos.h.example.com/a/.."),
    "repo key uppercase host": ("repos:\n  H.com/a/b: {}\n", "repos.H.com/a/b"),
    "repo key scheme": ("repos:\n  https://h.com/a/b: {}\n", "repos.https://h.com/a/b"),
    "repo keys differing only by case": (
        "repos:\n  h.example.com/Org/Repo: {merge_method: squash}\n"
        "  h.example.com/org/repo: {merge_method: merge}\n",
        "repos.h.example.com/org/repo",
    ),
    "repo null entry": ("repos:\n  h.example.com/a/b:\n", "repos.h.example.com/a/b"),
    "hook empty cmd": ("hooks:\n  pre-merge:\n    - cmd: ''\n", "hooks.pre-merge.0.cmd"),
    "hook blank cmd": ("hooks:\n  pre-merge:\n    - cmd: '   '\n", "hooks.pre-merge.0.cmd"),
    "hook unicode-blank cmd": (
        'hooks:\n  pre-merge:\n    - cmd: "\\u2003\\u3000"\n',
        "hooks.pre-merge.0.cmd",
    ),
    "hook cmd NUL": ('hooks:\n  pre-merge:\n    - cmd: "a\\0b"\n', "hooks.pre-merge.0.cmd"),
    "hook cmd surrogate": (
        'hooks:\n  pre-merge:\n    - cmd: "echo \\ud800"\n',
        "hooks.pre-merge.0.cmd",
    ),
    "hook missing cmd": ("hooks:\n  pre-merge:\n    - timeout: 5s\n", "hooks.pre-merge.0.cmd"),
    "hook cmd a list": ("hooks:\n  pre-merge:\n    - cmd: [a, b]\n", "hooks.pre-merge.0.cmd"),
    "hook async on a pre hook": (
        "hooks:\n  pre-merge:\n    - cmd: x\n      async: true\n",
        "hooks.pre-merge.0.async",
    ),
    "hook async false on a pre hook": (
        "hooks:\n  pre-post:\n    - cmd: x\n      async: false\n",
        "hooks.pre-post.0.async",
    ),
    "hook async not a bool": (
        "hooks:\n  post-merge:\n    - cmd: x\n      async: 'yes'\n",
        "hooks.post-merge.0.async",
    ),
    "hook bad timeout": (
        "hooks:\n  post-post:\n    - cmd: x\n      timeout: 0\n",
        "hooks.post-post.0.timeout",
    ),
    "hook unknown key": (
        "hooks:\n  pre-post:\n    - cmd: x\n      shell: bash\n",
        "hooks.pre-post.0.shell",
    ),
    "hook entry a string": ("hooks:\n  pre-merge:\n    - just-a-string\n", "hooks.pre-merge.0"),
    "hook list a map": ("hooks:\n  pre-merge: {cmd: x}\n", "hooks.pre-merge"),
    "hooks a list": ("hooks: [a]\n", "hooks"),
    # each login under its own backend's grammar, before any process (the schema has the union)
    "me.github with a GitLab-only dot": ("me: {github: 'dot.name'}\n", "me.github"),
    "me.gitlab with a bot suffix": ("me: {gitlab: 'octo[bot]'}\n", "me.gitlab"),
    "built-in host me under its backend": (
        "hosts:\n  github.com: {me: 'dot.name'}\n",
        "hosts.github.com.me",
    ),
    "declared host me under its backend": (
        HOST + "    backend: gitlab\n    me: 'octo[bot]'\n",
        "hosts.h.example.com.me",
    ),
    # token_command's program: a bare name or an absolute path, as for gh_bin
    "token_command relative program": (
        HOST + "    backend: gitlab\n    token_command: [bin/tool, x]\n",
        "hosts.h.example.com.token_command",
    ),
    "token_command dot-slash program": (
        HOST + "    backend: gitlab\n    token_command: [./tool]\n",
        "hosts.h.example.com.token_command",
    ),
    "token_command dotdot program": (
        "hosts:\n  github.com: {token_command: ['..']}\n",
        "hosts.github.com.token_command",
    ),
    # token_env never names a variable the gateway sets or passes to a child
    "token_env PATH": (
        "hosts:\n  github.com: {token_env: PATH}\n",
        "hosts.github.com.token_env",
    ),
    "token_env HTTPS_PROXY": (
        HOST + "    backend: gitlab\n    token_env: HTTPS_PROXY\n",
        "hosts.h.example.com.token_env",
    ),
    "token_env GH_CONFIG_DIR": (
        "hosts:\n  github.com: {token_env: GH_CONFIG_DIR}\n",
        "hosts.github.com.token_env",
    ),
    "browse interval 1ms": ("browse: {refresh_interval: 1ms}\n", "browse.refresh_interval"),
    "browse interval below one second": (
        "browse: {refresh_interval: 0.5}\n",
        "browse.refresh_interval",
    ),
    "browse interval 999ms": ("browse: {refresh_interval: 999ms}\n", "browse.refresh_interval"),
    "browse interval tiny minutes": (
        "browse: {refresh_interval: 0.001m}\n",
        "browse.refresh_interval",
    ),
    "browse interval zero string": ('browse: {refresh_interval: "0"}\n', "browse.refresh_interval"),
    "browse interval zero seconds": ("browse: {refresh_interval: 0s}\n", "browse.refresh_interval"),
    "browse full_refresh_every zero": (
        "browse: {full_refresh_every: 0}\n",
        "browse.full_refresh_every",
    ),
    "browse full_refresh_every float": (
        "browse: {full_refresh_every: 5.0}\n",
        "browse.full_refresh_every",
    ),
    "browse concurrency too big": ("browse: {concurrency: 17}\n", "browse.concurrency"),
    "browse concurrency float": ("browse: {concurrency: 4.0}\n", "browse.concurrency"),
    "browse unknown key": ("browse: {interval: 5}\n", "browse.interval"),
    # GitHub hosts gh rewrites to another host
    "github host under github.com": (
        "hosts:\n  api.github.com: {backend: github}\n",
        "hosts.api.github.com",
    ),
    "github host github.localhost": (
        "hosts:\n  github.localhost: {backend: github}\n",
        "hosts.github.localhost",
    ),
    "github host under github.localhost": (
        "hosts:\n  x.github.localhost: {backend: github}\n",
        "hosts.x.github.localhost",
    ),
}


@pytest.mark.parametrize(("text", "key"), INVALID.values(), ids=INVALID.keys())
def test_invalid_config_is_config_invalid_naming_the_key(home: Path, text: str, key: str) -> None:
    assert invalid(home, text).details == {"key": key}


VALID: dict[str, str] = {
    "timeout number": "timeout: 90\n",
    "timeout float": "timeout: 1.5\n",
    "timeout string seconds": "timeout: '90'\n",
    "timeout ms": "timeout: 500ms\n",
    "timeout d": "timeout: 1d\n",
    "timeout fraction": "timeout: 0.5h\n",
    "retries zero": "retries: 0\n",
    "retries ten": "retries: 10\n",
    "all nulls": "me: {name: null, github: null, gitlab: null}\n",
    "me names": "me: {name: jo.d_o@x-1, github: octo-cat, gitlab: a.b_c}\n",
    "bot login": "me: {github: 'dependabot[bot]'}\n",
    "gh_bin absolute": "gh_bin: /usr/local/bin/gh\n",
    "gh_bin bare": "gh_bin: gh-2.50\n",
    "built-in with matching backend": "hosts:\n  github.com: {backend: github, token_env: T}\n",
    "built-in gitlab with token command": (
        "hosts:\n  gitlab.com: {token_command: [tool, '{host}']}\n"
    ),
    "declared with port": "hosts:\n  git.example.com:8443: {backend: gitlab, api_prefix: /gl}\n",
    "declared with the highest port": "hosts:\n  git.example.com:65535: {backend: gitlab}\n",
    "repo key with a port": "repos:\n  git.example.com:8443/a/b: {}\n",
    "repo key on a www host that is not built in": "repos:\n  www.example.com/a/b: {}\n",
    "declared nested prefix": (
        "hosts:\n  git.example.com: {backend: gitlab, api_prefix: /a/b.c_d-e}\n"
    ),
    "declared github enterprise": (
        "hosts:\n  ghe.example.com: {backend: github, api_prefix: null}\n"
    ),
    "repo mixed-case path": "repos:\n  h.example.com/Org/Repo: {merge_method: rebase}\n",
    "repo nested group": "repos:\n  h.example.com/a/b/c: {merge_method: null}\n",
    "repo empty override": "repos:\n  h.example.com/a/b: {}\n",
    "hook defaults": "hooks:\n  pre-merge:\n    - cmd: x\n",
    "post hook async": "hooks:\n  post-post:\n    - cmd: x\n      async: true\n      timeout: 5s\n",
    "empty hook lists": "hooks:\n  pre-merge: []\n  post-merge: []\n",
    "token_env naming an ambient token variable": "hosts:\n  github.com: {token_env: GH_TOKEN}\n",
    "token_command absolute program": "hosts:\n  gitlab.com: {token_command: [/usr/bin/tool]}\n",
    "gitlab host under github.com": "hosts:\n  git.github.com: {backend: gitlab}\n",
    "github host on another domain": "hosts:\n  github.example.com: {backend: github}\n",
    "browse empty": "browse: {}\n",
    "browse interval off": "browse: {refresh_interval: 0}\n",
    "browse interval one second": "browse: {refresh_interval: 1}\n",
    "browse interval 1000ms": "browse: {refresh_interval: 1000ms}\n",
    "browse interval half a minute": "browse: {refresh_interval: 0.5m}\n",
    "browse all keys": "browse: {refresh_interval: 30s, full_refresh_every: 9, concurrency: 16}\n",
}


def test_token_env_refuses_every_variable_the_gateway_gives_a_child(tmp_path: Path) -> None:
    """The refused names come from the gateway's own lists; this keeps the literals in step."""
    from forgectl import gateway as gw
    from forgectl.config import child_variables

    names = ["PATH", "HOME", "TMPDIR", "GH_CONFIG_DIR", "GLAB_CONFIG_DIR", "XDG_CONFIG_HOME"]
    names += [*gw._PASSTHROUGH, *gw._BUS_VARIABLES]  # pyright: ignore[reportPrivateUsage]
    source = {name: str(tmp_path) for name in names} | {"PATH": "/usr/bin"}
    dirs = gw.PrivateDirs(root=str(tmp_path), cwd=str(tmp_path), config=str(tmp_path))
    seen: set[str] = set()
    for backend in ("github", "gitlab", None):
        targets: list[gw.Target | None] = [None]
        if backend is not None:
            host = "ghe.example.test" if backend == "github" else "git.example.test"
            targets += [gw.Target(backend, host), gw.Target(backend, host, token="t-1")]
        for target in targets:
            for drop in [None, "GH_CONFIG_DIR", "GLAB_CONFIG_DIR"]:
                env = {k: v for k, v in source.items() if k != drop}
                seen |= set(gw.build_environment(env, dirs=dirs, backend=backend, target=target))
    given = seen - set(gw.TOKEN_VARIABLES)
    assert given <= child_variables(), sorted(given - child_variables())
    assert not child_variables() & set(gw.TOKEN_VARIABLES)  # naming an ambient token is allowed


@pytest.mark.parametrize("text", VALID.values(), ids=VALID.keys())
def test_valid_config_loads(home: Path, text: str) -> None:
    load(home, text)


def test_value_semantics(home: Path) -> None:
    config = load(
        home,
        "timeout: 500ms\nhooks:\n  post-merge:\n    - cmd: x\n    - cmd: y\n      timeout: '2m'\n",
    ).config
    assert config.timeout == 0.5
    assert [h.timeout for h in config.hooks.post_merge] == [30.0, 120.0]
    assert [h.async_ for h in config.hooks.post_merge] == [False, False]
    assert load(home, "timeout: '90'\n").config.timeout == 90.0
    assert load(home, "timeout: 0.5h\n").config.timeout == 1800.0


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        ("timeout: .inf\n", "100 years"),
        ("timeout: 1e400\n", "100 years"),
        ("timeout: " + "9" * 400 + "\n", "100 years"),
        ("timeout: '" + "9" * 5000 + "'\n", "100 years"),
        ("timeout: .nan\n", "not a duration"),
        ("timeout: 0\n", "must be positive"),
        ("timeout: -2.5\n", "must be positive"),
        ("timeout: '0ms'\n", "must be positive"),
    ],
)
def test_duration_refusals_say_why(home: Path, text: str, phrase: str) -> None:
    assert phrase in invalid(home, text).message


def test_an_overflowing_environment_timeout_says_over_100_years(home: Path) -> None:
    err = invalid(home, None, FORGECTL_TIMEOUT="9" * 5000)
    assert err.details == {"key": "FORGECTL_TIMEOUT"}
    assert "100 years" in err.message


@pytest.mark.parametrize(
    ("spelled", "plain"),
    [("'9ms'", "0.009"), ("'4.1m'", "246"), ("'1m'", "60"), ("'60s'", "60"), ("'0.1s'", "0.1")],
)
def test_equal_durations_are_equal_values_and_hash_alike(
    home: Path, spelled: str, plain: str
) -> None:
    a = load(home, f"timeout: {spelled}\n")
    b = load(home, f"timeout: {plain}\n")
    assert a.config.timeout == b.config.timeout == float(plain)
    assert a.config_hash == b.config_hash


def test_the_error_message_does_not_echo_the_offending_value(home: Path) -> None:
    err = invalid(home, "me: {name: 'SECRET VALUE'}\n")
    assert "SECRET" not in err.message


# --- the schema and the models agree -------------------------------------------------------------

# The models' refusals beyond schemas/config.json, which cannot express them; interface.md
# "Reading files" lists the same rules. (YAML-level refusals, such as non-string keys and the node
# cap, are in HOSTILE_YAML: JSON has no such documents.)
STRICTER_THAN_SCHEMA = {
    "hook blank cmd",  # only whitespace, Unicode whitespace included
    "hook unicode-blank cmd",
    "gh_bin dot",  # an executable named . or ..
    "gh_bin dotdot",
    "gh_bin control",  # control characters (Unicode Cc) in an executable or a token_command arg
    "gh_bin delete",
    "gh_bin C1 control",
    "glab_bin next line",
    "token_command C0 control",
    "token_command C1 control",
    "gh_bin NUL",  # NUL in any string
    "hook cmd NUL",
    "gh_bin surrogate",  # a lone surrogate in any string
    "hook cmd surrogate",
    "retries float",  # a number with a fraction part: JSON Schema counts 3.0 as an integer
    "timeout inf",  # non-finite durations
    "timeout nan",
    "timeout over a century",  # a duration over 100 years
    "timeout huge int",
    "timeout huge float",
    "timeout huge numeric string",
    "repo keys differing only by case",
    "github host with a port",  # the key decides, which a schema cannot see
    "host key with :443",  # a host key that input normalization could never produce
    "host key leading-zero port",
    "host key port zero",
    "host key port too big",
    "host key www built-in",
    "host key www built-in with port",
    "repo key host with :443",
    "repo key host leading-zero port",
    "repo key host port zero",
    "repo key host port too big",
    "repo key www built-in",
    "me.github with a GitLab-only dot",  # each login under its own backend's grammar
    "me.gitlab with a bot suffix",
    "built-in host me under its backend",
    "declared host me under its backend",
    "token_command relative program",  # a bare name or an absolute path
    "token_command dot-slash program",
    "token_command dotdot program",
    "token_env PATH",  # a variable the gateway sets or passes to a child
    "token_env HTTPS_PROXY",
    "token_env GH_CONFIG_DIR",
    "github host under github.com",  # gh rewrites these hosts
    "github host github.localhost",
    "github host under github.localhost",
    "browse interval tiny minutes",  # sub-second after a unit conversion: only the model sees it
    "browse full_refresh_every float",  # 5.0 counts as an integer to JSON Schema
    "browse concurrency float",
}

# Refused by both under ECMA-262, whose `$` does not match before a final newline; the Python
# `re` that jsonschema uses here does, so this test's validator accepts them.
PYTHON_REGEX_NEWLINE = {
    "timeout trailing newline",
    "me.name newline",
    "gh_bin newline",
}


def schema_accepts(text: str) -> bool:
    return SCHEMA_VALIDATOR.is_valid(yaml.safe_load(text) or {})


@pytest.mark.parametrize(("text", "key"), INVALID.values(), ids=INVALID.keys())
def test_schema_rejects_what_the_models_reject(text: str, key: str, request: Any) -> None:
    name = request.node.callspec.id
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        pytest.skip("not YAML")
    if not isinstance(data, dict) or name in STRICTER_THAN_SCHEMA | PYTHON_REGEX_NEWLINE:
        pytest.skip("stricter than the schema, or not a mapping")
    assert not SCHEMA_VALIDATOR.is_valid(data), name


@pytest.mark.parametrize("name", sorted(STRICTER_THAN_SCHEMA | PYTHON_REGEX_NEWLINE))
def test_every_stricter_case_is_schema_valid_and_model_invalid(home: Path, name: str) -> None:
    text, key = INVALID[name]
    assert schema_accepts(text), name
    assert invalid(home, text).details == {"key": key}


@pytest.mark.parametrize("text", VALID.values(), ids=VALID.keys())
def test_schema_accepts_what_the_models_accept(text: str) -> None:
    assert schema_accepts(text)


def test_the_example_file_validates_against_the_schema() -> None:
    SCHEMA_VALIDATOR.validate(example_data())
    SCHEMA_VALIDATOR.validate(
        yaml.safe_load(
            "".join(uncommented(k) for k in ("hosts", "repos", "hooks", "activity", "browse"))
        )
    )


# --- environment ---------------------------------------------------------------------------------


def test_environment_sets_the_four_keys(home: Path) -> None:
    config = load(
        home,
        None,
        FORGECTL_TIMEOUT="2m",
        FORGECTL_RETRIES="7",
        FORGECTL_GH_BIN="/opt/gh",
        FORGECTL_GLAB_BIN="glab-x",
    ).config
    assert (config.timeout, config.retries) == (120.0, 7)
    assert (config.gh_bin, config.glab_bin) == ("/opt/gh", "glab-x")


def test_environment_beats_the_config_files(home: Path) -> None:
    write(default_file(home), "timeout: 5s\nretries: 1\ngh_bin: a\nglab_bin: b\n")
    dropins(home, {"x.yaml": "retries: 2\n"})
    config = load(home, None, FORGECTL_RETRIES="9", FORGECTL_GH_BIN="envgh").config
    assert (config.retries, config.gh_bin, config.timeout, config.glab_bin) == (
        9,
        "envgh",
        5.0,
        "b",
    )


BAD_ENV = [
    ("FORGECTL_TIMEOUT", ""),
    ("FORGECTL_TIMEOUT", "0"),
    ("FORGECTL_TIMEOUT", "0s"),
    ("FORGECTL_TIMEOUT", "-3"),
    ("FORGECTL_TIMEOUT", "abc"),
    ("FORGECTL_TIMEOUT", "5 s"),
    ("FORGECTL_TIMEOUT", " 5s"),
    ("FORGECTL_TIMEOUT", "5s\n"),
    ("FORGECTL_TIMEOUT", "1e3"),
    ("FORGECTL_TIMEOUT", "inf"),
    ("FORGECTL_TIMEOUT", "nan"),
    ("FORGECTL_TIMEOUT", "\u0663s"),
    ("FORGECTL_RETRIES", ""),
    ("FORGECTL_RETRIES", "11"),
    ("FORGECTL_RETRIES", "-1"),
    ("FORGECTL_RETRIES", "+3"),
    ("FORGECTL_RETRIES", "3.0"),
    ("FORGECTL_RETRIES", " 3"),
    ("FORGECTL_RETRIES", "3\n"),
    ("FORGECTL_RETRIES", "0x3"),
    ("FORGECTL_RETRIES", "\u0663"),
    ("FORGECTL_RETRIES", "true"),
    ("FORGECTL_RETRIES", "0" * 50 + "3"),
    ("FORGECTL_RETRIES", "03"),
    ("FORGECTL_GH_BIN", ""),
    ("FORGECTL_GH_BIN", "bin/gh"),
    ("FORGECTL_GH_BIN", "gh\n"),
    ("FORGECTL_GH_BIN", "gh\u009b"),
    ("FORGECTL_GLAB_BIN", "glab\x7f"),
    ("FORGECTL_TIMEOUT", "9" * 5000),
    ("FORGECTL_GLAB_BIN", ""),
    ("FORGECTL_GLAB_BIN", "./glab"),
    ("FORGECTL_GLAB_BIN", ".."),
    ("FORGECTL_GH_BIN", os.fsdecode(b"gh\xff")),
    ("FORGECTL_GLAB_BIN", "glab\ud800"),
]


@pytest.mark.parametrize("name", ["FORGECTL_GH_BIN", "FORGECTL_GLAB_BIN"])
def test_a_lone_surrogate_in_an_environment_executable_is_refused_without_echoing_it(
    home: Path, name: str
) -> None:
    with pytest.raises(ForgectlError) as exc:
        load(home, None, **{name: os.fsdecode(b"tool\xff")})
    assert exc.value.details == {"key": name}
    assert "tool" not in str(exc.value)


@pytest.mark.parametrize(("name", "value"), BAD_ENV)
def test_a_bad_environment_value_is_config_invalid_naming_the_variable(
    home: Path, name: str, value: str
) -> None:
    with pytest.raises(ForgectlError) as exc:
        load(home, None, **{name: value})
    assert exc.value.code == "config_invalid"
    assert exc.value.exit_code == 2
    assert exc.value.details == {"key": name}


def test_environment_values_are_checked_even_when_the_file_sets_the_key(home: Path) -> None:
    write(default_file(home), "retries: 2\n")
    with pytest.raises(ForgectlError) as exc:
        load(home, None, FORGECTL_RETRIES="99")
    assert exc.value.details == {"key": "FORGECTL_RETRIES"}


def test_structured_config_cannot_come_from_the_environment(home: Path) -> None:
    config = load(
        home,
        None,
        FORGECTL_HOSTS='{"evil.example.com": {"backend": "github"}}',
        FORGECTL_ME__NAME="mallory",
        FORGECTL_HOOKS__PRE_MERGE="rm -rf /",
        FORGECTL_REPOS="x",
        FORGECTL_HOOK="pre-merge",
        FORGECTL_ID="1",
        FORGECTL_OBJECT_TYPE="pr",
    ).config
    assert config == Config()


def test_a_config_file_error_wins_the_naming_over_a_bad_environment_value(home: Path) -> None:
    with pytest.raises(ForgectlError) as exc:
        load(home, "retries: 99\n", FORGECTL_RETRIES="2")
    assert exc.value.details == {"key": "retries"}


# --- precedence ----------------------------------------------------------------------------------


def test_timeout_precedence_flag_then_environment_then_config_then_default(home: Path) -> None:
    assert load(home).timeout_for(None) == 60.0
    assert load(home, "timeout: 10s\n").timeout_for(None) == 10.0
    assert load(home, "timeout: 10s\n", FORGECTL_TIMEOUT="20s").timeout_for(None) == 20.0
    assert load(home, "timeout: 10s\n", FORGECTL_TIMEOUT="20s").timeout_for(30.0) == 30.0
    assert load(home).timeout_for(0.25) == 0.25


def test_per_repo_override_beats_everything_for_merge_method(home: Path) -> None:
    loaded = load(home, "repos:\n  h.example.com/a/b: {merge_method: rebase}\n")
    assert loaded.merge_method_for("h.example.com/a/b", flag="squash") == "squash"
    assert loaded.merge_method_for("h.example.com/a/b", flag=None) == "rebase"
    assert loaded.merge_method_for("h.example.com/a/c", flag=None) is None


def test_the_repo_key_matches_the_canonical_path_ignoring_case(home: Path) -> None:
    loaded = load(home, "repos:\n  h.example.com/Org/Repo: {merge_method: merge}\n")
    assert loaded.merge_method_for("h.example.com/org/repo", flag=None) == "merge"
    assert loaded.merge_method_for("H.EXAMPLE.COM/ORG/REPO", flag=None) == "merge"
    assert loaded.merge_method_for("h.example.com/org/repo2", flag=None) is None


def test_a_null_repo_override_leaves_the_method_open(home: Path) -> None:
    loaded = load(home, "repos:\n  h.example.com/a/b: {merge_method: null}\n")
    assert loaded.merge_method_for("h.example.com/a/b", flag=None) is None


def test_the_hooks_and_token_command_are_tuples(home: Path) -> None:
    text = "hosts:\n  h.example.com: {backend: gitlab, token_command: [a, b]}\n"
    config = load(home, text).config
    assert config.hosts["h.example.com"].token_command == ("a", "b")
    assert Config().hooks.pre_merge == ()


# --- the config hash -----------------------------------------------------------------------------


def canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def test_hash_is_sha256_of_sorted_key_json_of_the_merged_config_after_defaults(
    home: Path,
) -> None:
    loaded = load(home, "me: {name: jo}\nretries: 5\n")
    expected = {
        "me": {"name": "jo", "github": None, "gitlab": None},
        "gh_bin": "gh",
        "glab_bin": "glab",
        "timeout": 60.0,
        "retries": 5,
        "hosts": {},
        "repos": {},
        "activity": {"ignore_authors": []},
        "browse": {"refresh_interval": 120.0, "full_refresh_every": 5, "concurrency": 4},
        "hooks": {"pre-merge": [], "post-merge": [], "pre-post": [], "post-post": []},
    }
    assert loaded.config_hash == hashlib.sha256(canonical(expected).encode()).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", loaded.config_hash)


def test_hash_covers_hosts_repos_and_hooks(home: Path) -> None:
    loaded = load(
        home,
        "hosts:\n  h.example.com: {backend: gitlab, api_prefix: /g}\n"
        "repos:\n  h.example.com/a/b: {merge_method: squash}\n"
        "hooks:\n  post-merge:\n    - cmd: x\n      async: true\n",
    )
    dump = json.loads(canonical(loaded.config.model_dump(mode="json", by_alias=True)))
    assert dump["hosts"]["h.example.com"] == {
        "backend": "gitlab",
        "token_env": None,
        "token_command": None,
        "me": None,
        "api_prefix": "/g",
    }
    assert dump["repos"]["h.example.com/a/b"] == {"merge_method": "squash"}
    assert dump["hooks"]["post-merge"] == [{"cmd": "x", "timeout": 30.0, "async": True}]
    assert loaded.config_hash == config_hash(loaded.config)


def test_hash_ignores_key_order_file_layout_and_spelling_of_durations(home: Path) -> None:
    a = load(home, "retries: 5\ntimeout: 60s\nme: {name: jo}\n").config_hash
    write(default_file(home), "")
    dropins(home, {"1.yaml": "me: {name: jo}\n", "2.yaml": "timeout: 1m\nretries: 5\n"})
    b = load(home).config_hash
    dropins(home, {"3.yaml": "timeout: '60'\n"})
    c = load(home).config_hash
    assert a == b == c


def test_hash_input_writes_durations_as_float_seconds(home: Path) -> None:
    dump = load(home, "timeout: 60\n").config.model_dump(mode="json", by_alias=True)
    text = json.dumps(dump, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert '"timeout":60.0' in text
    assert load(home, "timeout: 9ms\n").config.model_dump(mode="json")["timeout"] == 0.009


def test_hash_changes_with_any_value(home: Path) -> None:
    base = load(home).config_hash
    assert load(home, "retries: 4\n").config_hash != base
    assert load(home, "me: {name: jo}\n").config_hash != base
    assert load(home, "timeout: 61\n").config_hash != base


def test_hash_includes_environment_overrides_and_excludes_flags(home: Path) -> None:
    loaded = load(home)
    base = loaded.config_hash
    loaded.timeout_for(5.0)
    assert load(home).config_hash == base == loaded.config_hash
    from_env = load(home, None, FORGECTL_RETRIES="4").config_hash
    assert from_env != base
    assert from_env == load(home, "retries: 4\n").config_hash


def test_hash_does_not_depend_on_the_path_the_config_came_from(home: Path, tmp_path: Path) -> None:
    other = write(tmp_path / "x" / "c.yaml", "retries: 5\n")
    write(default_file(home), "retries: 5\n")
    assert load_config(str(other), env=env_for(home)).config_hash == load(home).config_hash


def test_the_loaded_config_is_immutable(home: Path) -> None:
    config = load(
        home,
        "hosts:\n  h.example.com: {backend: gitlab, token_command: [a]}\n"
        "repos:\n  h.example.com/a/b: {merge_method: squash}\n"
        "hooks:\n  pre-merge:\n    - cmd: x\n",
    ).config
    before = config_hash(config)
    with pytest.raises(Exception):  # noqa: B017 - pydantic's frozen-instance error
        config.retries = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        config.hosts["x.example.com"] = config.hosts["h.example.com"]  # type: ignore[index]
    with pytest.raises(TypeError):
        del config.repos["h.example.com/a/b"]  # type: ignore[attr-defined]
    token_command = config.hosts["h.example.com"].token_command
    assert token_command is not None
    with pytest.raises(AttributeError):
        token_command.append("b")  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        config.hooks.pre_merge.append(config.hooks.pre_merge[0])  # type: ignore[attr-defined]
    assert config_hash(config) == before
    assert list(config.hosts) == ["h.example.com"]
    assert [h.cmd for h in config.hooks.pre_merge] == ["x"]


def test_loading_does_not_mutate_its_inputs(home: Path) -> None:
    env = env_for(home, FORGECTL_RETRIES="2")
    before = copy.deepcopy(env)
    write(default_file(home), "retries: 1\n")
    load_config(None, env=env)
    assert env == before
