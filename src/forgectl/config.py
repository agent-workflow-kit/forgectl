"""Config: YAML and ``config.d`` merge, Pydantic models, ``FORGECTL_*`` overrides, config hash.

Implements interface.md "Config" and "Environment variables". The models mirror
``schemas/config.json`` (a test checks the two agree) and are stricter only where the schema
cannot say so; every such place is marked "stricter" below.

Layers, lowest to highest: built-in defaults, ``config.yaml`` and then ``config.d/*.yaml`` in
lexical order (maps merge, hook lists append, other lists replace), then the environment. Flags
and the per-repo ``merge_method`` are applied by the caller through ``LoadedConfig``.

Every file is untrusted input: size-capped, read without blocking, UTF-8 only, parsed by a loader
that resolves scalars under the YAML 1.2 core schema, caps the node count, and refuses anchors,
aliases, merge keys, non-string keys, duplicate keys, NUL, lone surrogates and every tag outside
YAML's core types. Any failure is ``config_invalid`` (exit 2) naming the key or the file, and a
message never repeats file content beyond a key.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Context, Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    TypeAdapter,
    ValidationError,
)
from yaml.composer import ComposerError
from yaml.constructor import ConstructorError
from yaml.parser import ParserError
from yaml.scanner import ScannerError

from .errors import ForgectlError

__all__ = [
    "DEFAULT_TIMEOUT",
    "Config",
    "LoadedConfig",
    "config_hash",
    "load_config",
    "parse_duration",
]

DEFAULT_TIMEOUT = 60.0
MAX_FILE_BYTES = 256 * 1024
MAX_DROP_INS = 256
MAX_NODES = 10_000  # per file: bounds parse time, far above any real config
_MAX_DURATION_SECONDS = Decimal(100 * 365 * 86400)  # as for the --timeout flag: 100 years

BUILT_IN_HOSTS = {"github.com": "github", "gitlab.com": "gitlab"}

# --- value grammars (the patterns of schemas/config.json, with ASCII classes and fullmatch) -----

_DURATION_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d)?", re.ASCII)
# Decimal, so equal durations are equal floats: "9ms" is 0.009 and "4.1m" is 246.
_UNIT_SECONDS = {
    None: Decimal(1),
    "ms": Decimal("0.001"),
    "s": Decimal(1),
    "m": Decimal(60),
    "h": Decimal(3600),
    "d": Decimal(86400),
}
_DECIMAL = Context(prec=34)  # a private context: the thread's context may have been changed
_NAME_RE = re.compile(r"[A-Za-z0-9._@-]{1,64}", re.ASCII)
_LOGIN_RE = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?"
    r"|[A-Za-z0-9_]|[A-Za-z0-9_.][A-Za-z0-9_.-]{0,253}[A-Za-z0-9_-])",
    re.ASCII,
)
_BIN_RE = re.compile(r"/.*|[^/]+", re.ASCII)
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
# Aligned with gateway._valid_host: labels of at most 63 characters, a name of at most 253.
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_PATTERN = (
    rf"(?=[a-z0-9.-]{{1,253}}(?![a-z0-9.-])){_HOST_LABEL}(?:\.{_HOST_LABEL})*(?::[0-9]{{1,5}})?"
)
_HOST_RE = re.compile(_HOST_PATTERN, re.ASCII)
_REPO_RE = re.compile(rf"{_HOST_PATTERN}(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._-]+)+", re.ASCII)
# Aligned with gateway._API_PREFIX: 1 to 8 segments of 1 to 64 characters.
_API_PREFIX_RE = re.compile(r"(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._-]{1,64}){1,8}", re.ASCII)
_ENV_RETRIES_RE = re.compile(r"0|[1-9][0-9]?", re.ASCII)


def parse_duration(value: object) -> float:
    """Seconds from a positive number, or a string ``<number>`` or ``<number><unit>``.

    Zero, negative, NaN, a bool, and anything over 100 years (stricter; infinity included) are
    refused. The arithmetic is decimal and the result is rounded to a float once.
    """
    not_a_duration = (
        "not a duration: a positive number of seconds, or a number with ms, s, m, h or d"
    )
    if isinstance(value, bool):
        raise ValueError("not a positive duration")
    if isinstance(value, int | float):
        seconds = Decimal(value)
    elif isinstance(value, str) and (match := _DURATION_RE.fullmatch(value)):
        seconds = _DECIMAL.multiply(Decimal(match[1]), _UNIT_SECONDS[match[2]])
    else:
        raise ValueError(not_a_duration)
    if seconds.is_nan():
        raise ValueError(not_a_duration)
    if seconds <= 0:
        raise ValueError("a duration must be positive")
    if seconds > _MAX_DURATION_SECONDS:
        raise ValueError("a duration may not exceed 100 years")
    return float(seconds)


def _matches(pattern: re.Pattern[str], what: str) -> AfterValidator:
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise ValueError(f"not a valid {what}")
        return value

    return AfterValidator(check)


def _no_control_characters(value: str) -> str:
    """Refuse Unicode category Cc: C0, DEL and C1 (not format characters, Cf)."""
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError("must not contain control characters")
    return value


def _bin_name(value: str) -> str:
    """A bare name or an absolute path (the schema), not ``.`` or ``..``, no control characters
    and no lone surrogates (an environment value can carry one from undecodable bytes)."""
    if not _BIN_RE.fullmatch(value) or value in (".", ".."):
        raise ValueError("must be a bare executable name or an absolute path")
    if any(unicodedata.category(ch) == "Cs" for ch in value):
        raise ValueError("must not contain a lone surrogate")
    return _no_control_characters(value)


def _command(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    if "\x00" in value:
        raise ValueError("must not contain NUL")
    return value


def _as_tuple(value: object) -> object:
    """A YAML list becomes a tuple, so the loaded config holds no mutable list."""
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, tuple):
        return value
    raise ValueError("Input should be a valid list")


def _read_only(value: dict[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(value)


def _plain_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    return dict(value)


def _empty_map() -> Mapping[str, Any]:
    return MappingProxyType({})


Duration = Annotated[float, BeforeValidator(parse_duration)]
Retries = Annotated[int, Field(strict=True, ge=0, le=10)]
BinName = Annotated[str, AfterValidator(_bin_name)]
Name = Annotated[str, _matches(_NAME_RE, "name: 1 to 64 of letters, digits and . _ @ -")]
Login = Annotated[str, _matches(_LOGIN_RE, "login")]
EnvName = Annotated[str, _matches(_ENV_NAME_RE, "variable name")]
ApiPrefix = Annotated[str, _matches(_API_PREFIX_RE, "path prefix such as /gitlab")]
Command = Annotated[str, AfterValidator(_command)]
Arg = Annotated[str, Field(min_length=1), AfterValidator(_no_control_characters)]
ArgList = Annotated[tuple[Arg, ...], BeforeValidator(_as_tuple), Field(min_length=1)]
PreHooks = Annotated[tuple["PreHook", ...], BeforeValidator(_as_tuple)]
PostHooks = Annotated[tuple["PostHook", ...], BeforeValidator(_as_tuple)]
HostMap = Annotated[Mapping[str, "Host"], AfterValidator(_read_only), PlainSerializer(_plain_dict)]
RepoMap = Annotated[Mapping[str, "Repo"], AfterValidator(_read_only), PlainSerializer(_plain_dict)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=False)


class Me(_Model):
    name: Name | None = None
    github: Login | None = None
    gitlab: Login | None = None


class Host(_Model):
    backend: Literal["github", "gitlab"] | None = None
    token_env: EnvName | None = None
    token_command: ArgList | None = None
    me: Login | None = None
    api_prefix: ApiPrefix | None = None


class Repo(_Model):
    merge_method: Literal["squash", "merge", "rebase"] | None = None


class PreHook(_Model):
    cmd: Command
    timeout: Duration = 30.0


class PostHook(_Model):
    cmd: Command
    timeout: Duration = 30.0
    async_: bool = Field(default=False, alias="async")


class Hooks(_Model):
    pre_merge: PreHooks = Field(default=(), alias="pre-merge")
    post_merge: PostHooks = Field(default=(), alias="post-merge")
    pre_post: PreHooks = Field(default=(), alias="pre-post")
    post_post: PostHooks = Field(default=(), alias="post-post")


class Config(_Model):
    me: Me = Field(default_factory=Me)
    gh_bin: BinName = "gh"
    glab_bin: BinName = "glab"
    timeout: Duration = DEFAULT_TIMEOUT
    retries: Retries = 3
    hosts: HostMap = Field(default_factory=_empty_map)
    repos: RepoMap = Field(default_factory=_empty_map)
    hooks: Hooks = Field(default_factory=Hooks)


# --- errors --------------------------------------------------------------------------------------


def _invalid(key: str, message: str) -> ForgectlError:
    return ForgectlError("config_invalid", f"{key}: {message}", details={"key": key})


def _key_of(loc: tuple[int | str, ...]) -> str:
    return ".".join(str(part) for part in loc) or "(config)"


# --- reading and parsing one file ----------------------------------------------------------------

_CORE = "tag:yaml.org,2002:"
_ALLOWED_TAGS = frozenset(
    f"{_CORE}{name}" for name in ("str", "int", "float", "bool", "null", "seq", "map")
)

# The YAML 1.2 core schema's forms, with numbers decimal only and without leading zeros (core
# also reads 0x1f, 0o7 and 010 as integers). PyYAML implements YAML 1.1, whose implicit
# resolution reads `1:30` as 90, `010` as 8, `0x3` as 3 and `yes` as true; here those are strings.
_CORE_INT = r"[-+]?(?:0|[1-9][0-9]*)"
_CORE_FLOAT = r"[-+]?(?:\.[0-9]+|(?:0|[1-9][0-9]*)(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
_CORE_INF = r"[-+]?\.(?:inf|Inf|INF)"
_CORE_NAN = r"\.(?:nan|NaN|NAN)"
_CORE_TRUE = ("true", "True", "TRUE")
_CORE_FALSE = ("false", "False", "FALSE")
_INT_RE = re.compile(_CORE_INT, re.ASCII)
_FLOAT_RE = re.compile(_CORE_FLOAT, re.ASCII)
_INF_RE = re.compile(_CORE_INF, re.ASCII)
_NAN_RE = re.compile(_CORE_NAN, re.ASCII)

_MISTYPED = "a value does not match its type"


class _Refusal(Exception):
    """A refusal by the strict loader: a fixed phrase, never file content; ``key`` is the dotted
    path it concerns, or None when it names the file."""

    def __init__(self, phrase: str, mark: Any, key: str | None = None) -> None:
        super().__init__(phrase)
        self.phrase = phrase
        self.line: int | None = mark.line + 1 if mark is not None else None
        self.key = key


def _dotted(path: tuple[str, ...]) -> str | None:
    return ".".join(path) if path else None


def _check_text(text: str, path: tuple[str, ...], mark: Any) -> None:
    if "\x00" in text:
        raise _Refusal("contains a NUL character", mark, _dotted(path))
    if any("\ud800" <= ch <= "\udfff" for ch in text):
        raise _Refusal("contains a lone surrogate", mark, _dotted(path))


def _check_tree(node: yaml.Node, path: tuple[str, ...]) -> None:
    """Keys are plain strings without duplicates or merge keys; no string holds NUL or a lone
    surrogate. A problem is named by its key path, or by the file at the top level."""
    if isinstance(node, yaml.ScalarNode):
        _check_text(node.value, path, node.start_mark)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _check_tree(item, (*path, str(index)))
    elif isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            if key_node.tag == f"{_CORE}merge":
                raise _Refusal("merge keys are not allowed", key_node.start_mark)
            if not isinstance(key_node, yaml.ScalarNode) or key_node.tag != f"{_CORE}str":
                raise _Refusal("keys must be strings", key_node.start_mark)
            name: str = key_node.value
            _check_text(name, path, key_node.start_mark)
            if name in seen:
                raise _Refusal("duplicate key", key_node.start_mark, ".".join((*path, name)))
            seen.add(name)
            _check_tree(value_node, (*path, name))


def _scalar(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    if not isinstance(node, yaml.ScalarNode):
        raise _Refusal(_MISTYPED, node.start_mark)
    return loader.construct_scalar(node)


def _construct_int(loader: yaml.SafeLoader, node: yaml.Node) -> int:
    text = _scalar(loader, node)
    if not _INT_RE.fullmatch(text):
        raise _Refusal(_MISTYPED, node.start_mark)
    try:
        return int(text)
    except ValueError:  # beyond Python's limit on the digits of an int
        raise _Refusal(_MISTYPED, node.start_mark) from None


def _construct_float(loader: yaml.SafeLoader, node: yaml.Node) -> float:
    text = _scalar(loader, node)
    if _INF_RE.fullmatch(text):
        return float("-inf") if text[0] == "-" else float("inf")
    if _NAN_RE.fullmatch(text):
        return float("nan")
    if not _FLOAT_RE.fullmatch(text):
        raise _Refusal(_MISTYPED, node.start_mark)
    return float(text)


def _construct_bool(loader: yaml.SafeLoader, node: yaml.Node) -> bool:
    text = _scalar(loader, node)
    if text in _CORE_TRUE:
        return True
    if text in _CORE_FALSE:
        return False
    raise _Refusal(_MISTYPED, node.start_mark)


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader with the YAML 1.2 core schema's scalars, at most ``MAX_NODES`` nodes, without
    anchors, aliases, merge keys, duplicate or non-string keys, NUL or lone surrogates, and with
    only the core types (no timestamps, binary, sets, ordered maps)."""

    yaml_implicit_resolvers: dict[Any, Any] = {}  # this class's own; SafeLoader's are untouched
    yaml_constructors = {
        tag: constructor
        for tag, constructor in yaml.SafeLoader.yaml_constructors.items()
        if tag is None or tag in _ALLOWED_TAGS  # None: the constructor that refuses unknown tags
    }
    _nodes = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        event: Any = self.peek_event()
        mark: Any = event.start_mark
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise _Refusal("anchors and aliases are not allowed", mark)
        self._nodes += 1
        if self._nodes > MAX_NODES:
            raise _Refusal(f"too many nodes (limit {MAX_NODES})", mark)
        return super().compose_node(parent, index)

    def get_single_node(self) -> Any:
        node = super().get_single_node()
        if node is not None:
            _check_tree(node, ())
        return node

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise _Refusal(_MISTYPED, node.start_mark)
        return yaml.constructor.BaseConstructor.construct_mapping(self, node, deep=True)


for _tag, _pattern, _first in (
    ("bool", "|".join(_CORE_TRUE + _CORE_FALSE), "tTfF"),
    ("int", _CORE_INT, "-+0123456789"),
    ("float", f"{_CORE_FLOAT}|{_CORE_INF}|{_CORE_NAN}", "-+.0123456789"),
    ("null", "~|null|Null|NULL|", ["~", "n", "N", ""]),
    ("merge", "<<", "<"),  # resolved only to be refused
):
    _StrictLoader.add_implicit_resolver(
        f"{_CORE}{_tag}", re.compile(rf"(?:{_pattern})\Z", re.ASCII), list(_first)
    )
_StrictLoader.add_constructor(f"{_CORE}int", _construct_int)
_StrictLoader.add_constructor(f"{_CORE}float", _construct_float)
_StrictLoader.add_constructor(f"{_CORE}bool", _construct_bool)


def _entry_exists(path: Path) -> bool:
    """Whether a directory entry is there at all (a dangling symlink is one)."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as err:
        raise _invalid(str(path), f"cannot be read: {err.strerror or 'error'}") from None
    return True


def _read_bytes(path: Path, *, required: bool) -> bytes | None:
    """The file's bytes, or None when no entry exists at the path and it is not required.

    Opened non-blocking so a FIFO cannot hang the open; anything but a regular file (after
    following symlinks, which dotfile managers use) is refused, a dangling symlink included;
    at most ``MAX_FILE_BYTES``.
    """
    if not required and not _entry_exists(path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise _invalid(str(path), "no such file, or a symbolic link to nothing") from None
    except OSError as err:
        raise _invalid(str(path), f"cannot be read: {err.strerror or 'error'}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _invalid(str(path), "not a regular file")
        data = b""
        while len(data) <= MAX_FILE_BYTES:
            chunk = os.read(fd, MAX_FILE_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError as err:
        raise _invalid(str(path), f"cannot be read: {err.strerror or 'error'}") from None
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise _invalid(str(path), f"file too large (limit {MAX_FILE_BYTES // 1024} KiB)")
    return data


def _parse_layer(path: Path, *, required: bool) -> dict[str, Any] | None:
    data = _read_bytes(path, required=required)
    if data is None:
        return None
    where = str(path)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _invalid(where, "not valid UTF-8") from None
    # Messages are fixed phrases plus a line: PyYAML's own text can quote tags, handles and
    # directives from the file.
    try:
        document = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - the strict loader above
    except _Refusal as err:
        at = f" at line {err.line}" if err.line is not None else ""
        if err.key is not None:
            raise _invalid(err.key, f"{err.phrase}{at}") from None
        raise _invalid(where, f"invalid YAML{at}: {err.phrase}") from None
    except yaml.reader.ReaderError as err:
        line = text.count("\n", 0, err.position) + 1
        message = f"invalid YAML at line {line}: a character YAML does not allow"
        raise _invalid(where, message) from None
    except yaml.MarkedYAMLError as err:
        if isinstance(err, ScannerError | ParserError):
            phrase = "syntax error"
        elif isinstance(err, ComposerError):
            phrase = "more than one document"
        elif isinstance(err, ConstructorError):
            phrase = "unsupported tag, or a value that does not match its tag"
        else:
            phrase = "invalid YAML"
        mark: Any = err.problem_mark or err.context_mark
        at = f" at line {mark.line + 1}" if mark is not None else ""
        raise _invalid(where, f"invalid YAML{at}: {phrase}") from None
    except yaml.YAMLError:
        raise _invalid(where, "invalid YAML") from None
    except RecursionError:
        raise _invalid(where, "invalid YAML: nested too deeply") from None
    except (ValueError, IndexError, KeyError, OverflowError, TypeError):  # a backstop
        raise _invalid(where, f"invalid YAML: {_MISTYPED}") from None
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise _invalid(where, "the top level must be a mapping")
    return document


def _drop_in_paths(directory: Path) -> list[Path]:
    """``*.yaml`` files of ``config.d`` in lexical (code point) order, hidden names skipped.

    No entry at the path means no drop-ins; an entry that is not a directory after following
    symlinks (a dangling link, a file, a FIFO) is refused, so drop-ins are never lost silently.
    """
    if not _entry_exists(directory):
        return []
    try:
        with os.scandir(directory) as entries:
            names = [e.name for e in entries if e.name.endswith(".yaml") and e.name[0] != "."]
    except (FileNotFoundError, NotADirectoryError):
        raise _invalid(str(directory), "not a directory") from None
    except OSError as err:
        raise _invalid(str(directory), f"cannot be read: {err.strerror or 'error'}") from None
    if len(names) > MAX_DROP_INS:
        raise _invalid(str(directory), f"too many drop-in files (limit {MAX_DROP_INS})")
    return [directory / name for name in sorted(names)]


# --- merging -------------------------------------------------------------------------------------


def _merge(base: Any, over: Any, path: tuple[str, ...] = ()) -> Any:
    """Maps merge; ``hooks.<name>`` lists append; every other value, lists included, replaces."""
    if isinstance(base, dict) and isinstance(over, dict):
        merged = dict(base)
        for key, value in over.items():
            merged[key] = _merge(base[key], value, (*path, key)) if key in base else value
        return merged
    if isinstance(base, list) and isinstance(over, list) and len(path) == 2 and path[0] == "hooks":
        return [*base, *over]
    return over


# --- validation ----------------------------------------------------------------------------------


def _validate(raw: dict[str, Any]) -> Config:
    try:
        config = Config.model_validate(raw)
    except ValidationError as err:
        first = err.errors(include_input=False, include_url=False)[0]
        message = first["msg"].removeprefix("Value error, ")
        raise _invalid(_key_of(first["loc"]), message) from None
    _check_keys(config)
    return config


def _check_keys(config: Config) -> None:
    """The rules that depend on a map's keys or on several fields at once."""
    for host, entry in config.hosts.items():
        key = f"hosts.{host}"
        built_in = BUILT_IN_HOSTS.get(host)
        if built_in is not None:
            if "backend" in entry.model_fields_set and entry.backend != built_in:
                raise _invalid(f"{key}.backend", f"a built-in host's backend is {built_in}")
            if "api_prefix" in entry.model_fields_set:
                raise _invalid(f"{key}.api_prefix", "not allowed for a built-in host")
            continue
        if not _HOST_RE.fullmatch(host):
            raise _invalid(key, "not a valid host name (lowercase, with an optional port)")
        _check_host_normalized(key, host)
        if entry.backend is None:
            raise _invalid(f"{key}.backend", "required for a host that is not built in")
        if entry.backend == "github" and entry.api_prefix is not None:
            raise _invalid(f"{key}.api_prefix", "must be null for a GitHub host")
        if entry.backend == "github" and ":" in host:
            raise _invalid(key, "a GitHub host takes no port (gh refuses one)")
    seen: set[str] = set()
    for repo in config.repos:
        key = f"repos.{repo}"
        if not _REPO_RE.fullmatch(repo):
            raise _invalid(key, "not a valid repo id: host/path")
        _check_host_normalized(key, repo.split("/", 1)[0])
        if repo.lower() in seen:  # stricter: the lookup ignores case, so these would collide
            raise _invalid(key, "differs from another repo key only by case")
        seen.add(repo.lower())


def _check_host_normalized(key: str, host: str) -> None:
    """A host (already matching ``_HOST_RE``) must be in the form input normalizes to, or no
    reference could ever reach it. Uses the input normalization so the two stay in step."""
    from .identifiers import _normalize_host  # here: identifiers imports this module

    name, _, port = host.partition(":")
    if _normalize_host(name, port or None) != host or (
        name.startswith("www.") and name[4:] in BUILT_IN_HOSTS
    ):
        raise _invalid(
            key,
            "the host is not in normalized form (no :443, no leading-zero port, port 1-65535,"
            " no www. in front of a built-in host)",
        )


# --- the environment -----------------------------------------------------------------------------

_TIMEOUT_ADAPTER: TypeAdapter[float] = TypeAdapter(Duration)
_RETRIES_ADAPTER: TypeAdapter[int] = TypeAdapter(Retries)
_BIN_ADAPTER: TypeAdapter[str] = TypeAdapter(BinName)


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """The four ``FORGECTL_*`` settings that are present, validated under the config's rules.

    Only these four are read: structured config cannot come from the environment, and the hook
    variables (``FORGECTL_HOOK``, ``FORGECTL_ID``, ``FORGECTL_OBJECT_TYPE``) are not config keys.
    An empty value is refused rather than treated as unset.
    """
    found: dict[str, Any] = {}
    for name, field, adapter in (
        ("FORGECTL_TIMEOUT", "timeout", _TIMEOUT_ADAPTER),
        ("FORGECTL_RETRIES", "retries", _RETRIES_ADAPTER),
        ("FORGECTL_GH_BIN", "gh_bin", _BIN_ADAPTER),
        ("FORGECTL_GLAB_BIN", "glab_bin", _BIN_ADAPTER),
    ):
        raw = env.get(name)
        if raw is None:
            continue
        try:
            if field == "retries":
                if not _ENV_RETRIES_RE.fullmatch(raw):
                    raise ValueError("must be a whole number from 0 to 10")
                found[field] = adapter.validate_python(int(raw))
            else:
                found[field] = adapter.validate_python(raw)
        except (ValueError, ValidationError) as err:
            message = (
                err.errors(include_input=False, include_url=False)[0]["msg"]
                if isinstance(err, ValidationError)
                else str(err)
            )
            raise _invalid(name, message.removeprefix("Value error, ")) from None
    return found


# --- locating ------------------------------------------------------------------------------------


def _config_home(env: Mapping[str, str]) -> Path:
    """``$XDG_CONFIG_HOME`` when absolute (a relative or empty value is ignored, per the XDG
    spec), else ``~/.config``; the home directory is ``$HOME`` when absolute, else the account's."""
    xdg = env.get("XDG_CONFIG_HOME", "")
    if xdg and os.path.isabs(xdg):
        return Path(xdg)
    home = env.get("HOME", "")
    if not (home and os.path.isabs(home)):
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
        except (KeyError, OSError):
            raise _invalid("HOME", "cannot locate the home directory") from None
    return Path(home) / ".config"


# --- the result ----------------------------------------------------------------------------------


def config_hash(config: Config) -> str:
    """The canonical form of interface.md "Audit and log" (``config_hash``): sha256 hex of the
    sorted-key, compact, ASCII-escaped JSON, in UTF-8, of the config after files, environment and
    defaults (no flags), with schema key spellings and durations in seconds, so ``60``, ``"60"``,
    ``"60s"`` and ``"1m"`` hash alike.
    """
    document = config.model_dump(mode="json", by_alias=True)
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LoadedConfig:
    """The config in effect (files and environment), its hash, and the flag-aware lookups."""

    config: Config
    config_hash: str
    config_path: str | None  # the `--config` path; None for the default location

    @classmethod
    def defaults(cls) -> LoadedConfig:
        config = Config()
        return cls(config=config, config_hash=config_hash(config), config_path=None)

    def timeout_for(self, flag: float | None) -> float:
        """Seconds for the host calls: ``--timeout``, else ``FORGECTL_TIMEOUT``, else the config."""
        return flag if flag is not None else self.config.timeout

    def merge_method_for(self, repo_id: str, *, flag: str | None) -> str | None:
        """``--method``, else the per-repo override (the key matches ignoring case), else None."""
        if flag is not None:
            return flag
        wanted = repo_id.lower()
        for key, repo in self.config.repos.items():
            if key.lower() == wanted:
                return repo.merge_method
        return None


def load_config(config_path: str | None, *, env: Mapping[str, str]) -> LoadedConfig:
    """Load and validate the config; raise ``config_invalid`` (exit 2) on any problem.

    ``config_path`` is ``--config``: a file that must exist, whose directory's ``config.d`` holds
    the drop-ins. None reads ``<config home>/forgectl/config.yaml`` (absent is fine) and its
    ``config.d``.
    """
    if config_path is None:
        main = _config_home(env) / "forgectl" / "config.yaml"
        required = False
    else:
        main = Path(os.path.abspath(config_path))
        required = True
    raw: dict[str, Any] = {}
    for path, must_exist in [(main, required)] + [
        (p, True) for p in _drop_in_paths(main.parent / "config.d")
    ]:
        layer = _parse_layer(path, required=must_exist)
        if layer is not None:
            raw = _merge(raw, layer)
    config = _validate(raw)
    overrides = _env_overrides(env)
    if overrides:
        config = config.model_copy(update=overrides)
    return LoadedConfig(
        config=config,
        config_hash=config_hash(config),
        config_path=None if config_path is None else str(main),
    )
