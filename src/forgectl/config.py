"""Config: YAML and ``config.d`` merge, Pydantic models, ``FORGECTL_*`` overrides, config hash.

Implements interface.md "Config" and "Environment variables". The models mirror
``schemas/config.json`` (a test checks the two agree) and are stricter only where the schema
cannot say so; every such place is marked "stricter" below.

Layers, lowest to highest: built-in defaults, ``config.yaml`` and then ``config.d/*.yaml`` in
lexical order (maps merge, hook lists append, other lists replace), then the environment. Flags
and the per-repo ``merge_method`` are applied by the caller through ``LoadedConfig``.

Every file is untrusted input: size-capped, read without blocking, UTF-8 only, parsed by a loader
that refuses anchors, aliases, merge keys, non-string keys, duplicate keys and every tag outside
YAML's core types. Any failure is ``config_invalid`` (exit 2) naming the key or the file.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pwd
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)
from yaml.composer import ComposerError
from yaml.constructor import ConstructorError

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
_MAX_DURATION_SECONDS = 100 * 365 * 86400.0  # as for the --timeout flag: 100 years

BUILT_IN_HOSTS = {"github.com": "github", "gitlab.com": "gitlab"}

# --- value grammars (the patterns of schemas/config.json, with ASCII classes and fullmatch) -----

_DURATION_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h|d)?", re.ASCII)
_UNIT_SECONDS = {None: 1.0, "ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_NAME_RE = re.compile(r"[A-Za-z0-9._@-]{1,64}", re.ASCII)
_LOGIN_RE = re.compile(
    r"(?:[A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?|[A-Za-z0-9_.][A-Za-z0-9_.-]{0,254})", re.ASCII
)
_BIN_RE = re.compile(r"/.*|[^/]+", re.ASCII)
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_HOST_PATTERN = rf"{_HOST_LABEL}(?:\.{_HOST_LABEL})*(?::[0-9]{{1,5}})?"
_HOST_RE = re.compile(_HOST_PATTERN, re.ASCII)
_REPO_RE = re.compile(rf"{_HOST_PATTERN}(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._-]+)+", re.ASCII)
_API_PREFIX_RE = re.compile(r"(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._-]+)+", re.ASCII)
_ENV_RETRIES_RE = re.compile(r"0|[1-9][0-9]?", re.ASCII)


def parse_duration(value: object) -> float:
    """Seconds from a positive number, or a string ``<number>`` or ``<number><unit>``.

    Zero, negative, non-finite, a bool, and anything over 100 years (stricter) are refused.
    """
    if isinstance(value, bool):
        raise ValueError("not a positive duration")
    seconds: float
    try:
        if isinstance(value, int | float):
            seconds = float(value)
        elif isinstance(value, str) and (match := _DURATION_RE.fullmatch(value)):
            seconds = float(match[1]) * _UNIT_SECONDS[match[2]]
        else:
            raise ValueError
    except (ValueError, OverflowError):
        raise ValueError(
            "not a duration: a positive number of seconds, or a number with ms, s, m, h or d"
        ) from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("a duration must be positive")
    if seconds > _MAX_DURATION_SECONDS:
        raise ValueError("a duration may not exceed 100 years")
    return seconds


def _matches(pattern: re.Pattern[str], what: str) -> AfterValidator:
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise ValueError(f"not a valid {what}")
        return value

    return AfterValidator(check)


def _no_control_characters(value: str) -> str:
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("must not contain control characters")
    return value


def _bin_name(value: str) -> str:
    """A bare name or an absolute path (the schema), not ``.`` or ``..``, no control characters."""
    if not _BIN_RE.fullmatch(value) or value in (".", ".."):
        raise ValueError("must be a bare executable name or an absolute path")
    return _no_control_characters(value)


def _command(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    if "\x00" in value:
        raise ValueError("must not contain NUL")
    return value


Duration = Annotated[float, BeforeValidator(parse_duration)]
Retries = Annotated[int, Field(strict=True, ge=0, le=10)]
BinName = Annotated[str, AfterValidator(_bin_name)]
Name = Annotated[str, _matches(_NAME_RE, "name: 1 to 64 of letters, digits and . _ @ -")]
Login = Annotated[str, _matches(_LOGIN_RE, "login")]
EnvName = Annotated[str, _matches(_ENV_NAME_RE, "variable name")]
ApiPrefix = Annotated[str, _matches(_API_PREFIX_RE, "path prefix such as /gitlab")]
Command = Annotated[str, AfterValidator(_command)]
Arg = Annotated[str, Field(min_length=1), AfterValidator(_no_control_characters)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=False)


class Me(_Model):
    name: Name | None = None
    github: Login | None = None
    gitlab: Login | None = None


class Host(_Model):
    backend: Literal["github", "gitlab"] | None = None
    token_env: EnvName | None = None
    token_command: Annotated[list[Arg], Field(min_length=1)] | None = None
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
    pre_merge: list[PreHook] = Field(default_factory=list, alias="pre-merge")
    post_merge: list[PostHook] = Field(default_factory=list, alias="post-merge")
    pre_post: list[PreHook] = Field(default_factory=list, alias="pre-post")
    post_post: list[PostHook] = Field(default_factory=list, alias="post-post")


class Config(_Model):
    me: Me = Field(default_factory=Me)
    gh_bin: BinName = "gh"
    glab_bin: BinName = "glab"
    timeout: Duration = DEFAULT_TIMEOUT
    retries: Retries = 3
    hosts: dict[str, Host] = Field(default_factory=dict)
    repos: dict[str, Repo] = Field(default_factory=dict)
    hooks: Hooks = Field(default_factory=Hooks)


# --- errors --------------------------------------------------------------------------------------


def _invalid(key: str, message: str) -> ForgectlError:
    return ForgectlError("config_invalid", f"{key}: {message}", details={"key": key})


def _key_of(loc: tuple[int | str, ...]) -> str:
    return ".".join(str(part) for part in loc) or "(config)"


# --- reading and parsing one file ----------------------------------------------------------------

_ALLOWED_TAGS = frozenset(
    f"tag:yaml.org,2002:{name}" for name in ("str", "int", "float", "bool", "null", "seq", "map")
)


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader without anchors, aliases, merge keys, duplicate or non-string keys, and with
    only YAML's core scalar and collection types (no timestamps, binary, sets, ordered maps)."""

    yaml_constructors = {
        tag: constructor
        for tag, constructor in yaml.SafeLoader.yaml_constructors.items()
        if tag is None or tag in _ALLOWED_TAGS  # None: the constructor that refuses unknown tags
    }

    def compose_node(self, parent: Any, index: Any) -> Any:
        event: Any = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise ComposerError(
                None,
                None,
                "anchors and aliases are not allowed",
                event.start_mark,  # type: ignore[arg-type]
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise ConstructorError(None, None, "expected a mapping", node.start_mark)
        seen: set[str] = set()
        for key_node, _value in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise ConstructorError(
                    None, None, "merge keys are not allowed", key_node.start_mark
                )
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, str):
                raise ConstructorError(None, None, "keys must be strings", key_node.start_mark)
            if key in seen:
                raise ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return yaml.constructor.BaseConstructor.construct_mapping(self, node, deep=True)


def _contains_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, list):
        return any(_contains_nul(v) for v in value)
    if isinstance(value, dict):
        return any(_contains_nul(k) or _contains_nul(v) for k, v in value.items())
    return False


def _read_bytes(path: Path, *, required: bool) -> bytes | None:
    """The file's bytes, or None when it is absent and not required.

    Opened non-blocking so a FIFO cannot hang the open; anything but a regular file (after
    following symlinks, which dotfile managers use) is refused; at most ``MAX_FILE_BYTES``.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        if required:
            raise _invalid(str(path), "no such file") from None
        return None
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
    try:
        document = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - the strict loader above
    except yaml.MarkedYAMLError as err:
        line = err.problem_mark.line + 1 if err.problem_mark is not None else "?"
        problem = err.problem or "invalid YAML"
        raise _invalid(where, f"invalid YAML at line {line}: {problem}") from None
    except yaml.YAMLError:
        raise _invalid(where, "invalid YAML") from None
    except RecursionError:
        raise _invalid(where, "nested too deeply") from None
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise _invalid(where, "the top level must be a mapping")
    if _contains_nul(document):
        raise _invalid(where, "a value contains a NUL character")
    return document


def _drop_in_paths(directory: Path) -> list[Path]:
    """``*.yaml`` files of ``config.d`` in lexical (code point) order, hidden names skipped."""
    try:
        with os.scandir(directory) as entries:
            names = [e.name for e in entries if e.name.endswith(".yaml") and e.name[0] != "."]
    except (FileNotFoundError, NotADirectoryError):
        return []
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
            if entry.backend not in (None, built_in):
                raise _invalid(f"{key}.backend", f"a built-in host's backend is {built_in}")
            if "api_prefix" in entry.model_fields_set:
                raise _invalid(f"{key}.api_prefix", "not allowed for a built-in host")
            continue
        if not _HOST_RE.fullmatch(host):
            raise _invalid(key, "not a valid host name (lowercase, with an optional port)")
        if entry.backend is None:
            raise _invalid(f"{key}.backend", "required for a host that is not built in")
        if entry.backend == "github" and entry.api_prefix is not None:
            raise _invalid(f"{key}.api_prefix", "must be null for a GitHub host")
    seen: set[str] = set()
    for repo in config.repos:
        key = f"repos.{repo}"
        if not _REPO_RE.fullmatch(repo):
            raise _invalid(key, "not a valid repo id: host/path")
        if repo.lower() in seen:  # stricter: the lookup ignores case, so these would collide
            raise _invalid(key, "differs from another repo key only by case")
        seen.add(repo.lower())


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
    """sha256 of the sorted-key, compact JSON of the config after defaults (no flags).

    Durations are in seconds, so ``60``, ``"60"``, ``"60s"`` and ``"1m"`` hash alike.
    """
    document = config.model_dump(mode="json", by_alias=True)
    text = json.dumps(document, sort_keys=True, separators=(",", ":"))
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
