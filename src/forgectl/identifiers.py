"""Parsers for everything forgectl takes from the command line (interface: Identifiers, Hosts).

Input may come from third-party text, so nothing here uses a URL library or normalizes before
matching. Every grammar is one anchored ASCII pattern matched with ``fullmatch`` over the whole
raw text (never ``$``, which accepts a trailing newline). A child process only ever gets the
validated parts of a result, never input text.

Parsing is two-step because the closed host list lives in the config, which is loaded after the
command line is parsed:

1. ``parse_*`` checks the grammar alone and needs no config. It returns an unbound value
   (``PrInput``, ``RepoInput``) or a validated string. A bad value raises ``invalid_pr`` for a PR
   and ``usage`` for the rest, both exit 2.
2. ``bind_*`` takes the config: a host that is not built in or declared exits 20 (``unknown_host``),
   then the backend's own rules apply (segment counts, ``api_prefix``, the login grammar). It
   returns a bound value (``PrRef``, ``RepoRef``) that is safe to hand to a command. ``bind_args``
   does this for a parsed command line before any handler runs.
"""

from __future__ import annotations

import re
import unicodedata
from argparse import Namespace
from dataclasses import dataclass, field
from typing import Literal

from .config import BUILT_IN_HOSTS, Config
from .errors import ForgectlError, refused, usage

Backend = Literal["github", "gitlab"]

MAX_INPUT_CHARS = 2048
MAX_BRANCH_CHARS = 1024

# --- grammars ------------------------------

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_HOST = rf"(?P<host>{_LABEL}(?:\.{_LABEL})*)(?::(?P<port>[0-9]{{1,5}}))?"
_SEG = r"[A-Za-z0-9._-]+"
_PROJECT = rf"(?P<project>{_SEG}(?:/{_SEG})*)"
_PROJECT_LAZY = rf"(?P<project>{_SEG}(?:/{_SEG})*?)"  # so a trailing `/-` is not a segment
_NUMBER = r"(?P<number>[1-9][0-9]{0,9})"
_TAIL = r"(?:\?(?P<query>[A-Za-z0-9._~=&-]*))?(?:#(?P<fragment>[A-Za-z0-9._~=&-]*))?"
_SHORT_SHA = r"[0-9A-Fa-f]{7,40}"

_ASCII = re.ASCII
_HOST_RE = re.compile(_HOST, _ASCII)
_REPO_RE = re.compile(rf"{_HOST}/{_PROJECT}", _ASCII)
_PR_TEXT_RE = re.compile(rf"{_HOST}/{_PROJECT}#{_NUMBER}", _ASCII)
_GITHUB_URL_RE = re.compile(
    rf"https://{_HOST}/{_PROJECT_LAZY}"
    rf"/pull/{_NUMBER}(?:(?:/files|/commits(?:/{_SHORT_SHA})?|/checks)?/?|\.(?:diff|patch)/?){_TAIL}",
    _ASCII,
)
_GITLAB_URL_RE = re.compile(
    rf"https://{_HOST}/{_PROJECT_LAZY}(?:/-)?"
    rf"/merge_requests/{_NUMBER}(?:(?:/diffs|/commits|/pipelines)?/?){_TAIL}",
    _ASCII,
)
_GITHUB_LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?", _ASCII)
# never ending in `.`, so `.` and `..` (path traversal in REST `users/<name>`) are out
_GITLAB_LOGIN_RE = re.compile(
    r"[A-Za-z0-9_]|[A-Za-z0-9_.][A-Za-z0-9_.-]{0,253}[A-Za-z0-9_-]", _ASCII
)
_SHA_RE = re.compile(r"[0-9a-fA-F]{40}", _ASCII)
_NODE = r"[A-Za-z0-9_=-]{1,100}"
_TYPED_ID_RE = re.compile(
    rf"(?:(?P<gh>gh):(?P<ghkind>ic|rc|rt|r):(?P<ghnode>{_NODE})"
    r"|(?P<gl>gl):d:(?P<discussion>[0-9a-f]{40})"
    r"|(?P<gln>gl):n:(?P<note>[1-9][0-9]{0,18}))",
    _ASCII,
)

_BRANCH_FORBIDDEN_CHARS = frozenset(" ~^:?*[\\\x7f")
_BRANCH_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_BAD_PROJECT_SEGMENTS_IN_URL = frozenset({"-", "merge_requests"})


def _shown(text: str) -> str:
    """The input as an error message may repeat it: bounded; output sanitizes it."""
    return repr(text if len(text) <= 100 else text[:100] + "...")


# --- hosts ------------------------------


def _normalize_host(host: str, port: str | None) -> str | None:
    """Lowercase, drop ``:443``, strip ``www.`` for a built-in host; None for a bad port."""
    host = host.lower()
    if port is not None:
        if port.startswith("0") or not 1 <= int(port) <= 65535:
            return None
        if port == "443":
            port = None
    if port is None and host.startswith("www.") and host[4:] in BUILT_IN_HOSTS:
        host = host[4:]
    if len(host) > 253:
        return None
    return host if port is None else f"{host}:{port}"


def parse_host(text: str) -> str:
    """A ``--host`` value: lowercased, ``:443`` dropped, ``www.`` stripped for built-in hosts."""
    match = _HOST_RE.fullmatch(text) if len(text) <= MAX_INPUT_CHARS else None
    host = _normalize_host(match["host"], match["port"]) if match else None
    if host is None:
        raise usage(f"not a valid host: {_shown(text)}")
    return host


def backend_of(host: str, config: Config) -> Backend:
    """The backend of ``host``; a host that is not built in or declared exits 20."""
    built_in = BUILT_IN_HOSTS.get(host)
    if built_in is not None:
        return built_in  # type: ignore[return-value]
    declared = config.hosts.get(host)
    if declared is None or declared.backend is None:
        raise refused(
            "unknown_host",
            f"host {_shown(host)} is not built in or declared in the config",
        )
    return declared.backend


def bind_host(host: str, config: Config) -> Backend:
    """The backend for a parsed ``--host``; exit 20 ``unknown_host`` for an undeclared host."""
    return backend_of(host, config)


# --- repo ids and PR references ------------------------------


@dataclass(frozen=True)
class RepoRef:
    """A repo on a known host: ``host/path`` in the host's backend."""

    host: str
    backend: Backend
    path: str

    @property
    def id(self) -> str:
        return f"{self.host}/{self.path}"

    def __str__(self) -> str:
        return self.id


@dataclass(frozen=True)
class PrRef:
    """A pull or merge request on a known host. ``str()`` is the ``host/path#N`` form."""

    host: str
    backend: Backend
    project: str
    number: int

    @property
    def repo(self) -> RepoRef:
        return RepoRef(host=self.host, backend=self.backend, path=self.project)

    def __str__(self) -> str:
        return f"{self.host}/{self.project}#{self.number}"


@dataclass(frozen=True)
class RepoInput:
    """A repo id whose grammar is checked but whose host is not (``bind_repo`` does that)."""

    host: str
    path: str

    def __str__(self) -> str:
        return f"{self.host}/{self.path}"


@dataclass(frozen=True)
class PrInput:
    """A PR reference whose grammar is checked but whose host is not (``bind_pr`` does that).

    ``project`` is the path as given: for a URL on a host with an ``api_prefix`` it still holds
    the prefix. ``kind`` is the URL's ``pull`` or ``merge_requests``, None for ``host/path#N``.
    ``raw`` is the text as given, for a bind-time ``invalid_pr`` message (the normalized form can
    itself be a valid reference); it is not part of equality or ``str()``.
    """

    host: str
    project: str
    number: int
    kind: Literal["pull", "merge_requests"] | None
    raw: str = field(default="", compare=False, repr=False)

    def __str__(self) -> str:
        return f"{self.host}/{self.project}#{self.number}"


def _clean_segments(project: str) -> bool:
    return not any(seg in (".", "..") for seg in project.split("/"))


def _no_option_segments(backend: Backend, path: str) -> bool:
    """No segment that reads as an option: GitLab group and project paths and GitHub owners
    (logins) cannot start with ``-``; a GitHub repo name can."""
    segments = path.split("/")
    checked = segments if backend == "gitlab" else segments[:1]
    return not any(seg.startswith("-") for seg in checked)


def parse_pr(text: str) -> PrInput:
    """An ``https`` URL or ``host/<project path>#N``; anything else is ``invalid_pr`` (exit 2)."""
    parsed = _match_pr(text) if len(text) <= MAX_INPUT_CHARS else None
    if parsed is None:
        raise ForgectlError(
            "invalid_pr",
            f"not a pull request URL or host/<project path>#N: {_shown(text)}",
        )
    return parsed


def _match_pr(text: str) -> PrInput | None:
    kind: Literal["pull", "merge_requests"] | None
    if text.startswith("https://"):
        match = _GITHUB_URL_RE.fullmatch(text)
        kind = "pull"
        if match is None:
            match = _GITLAB_URL_RE.fullmatch(text)
            kind = "merge_requests"
    else:
        match = _PR_TEXT_RE.fullmatch(text)
        kind = None
    if match is None:
        return None
    host = _normalize_host(match["host"], match["port"])
    project = match["project"]
    if host is None or not _clean_segments(project):
        return None
    if kind == "merge_requests" and _BAD_PROJECT_SEGMENTS_IN_URL.intersection(project.split("/")):
        return None
    return PrInput(host=host, project=project, number=int(match["number"]), kind=kind, raw=text)


def parse_repo(text: str) -> RepoInput:
    """``host/<project path>``; a bad ``--repo`` is ``usage`` (exit 2)."""
    match = _REPO_RE.fullmatch(text) if len(text) <= MAX_INPUT_CHARS else None
    host = _normalize_host(match["host"], match["port"]) if match else None
    if match is None or host is None or not _clean_segments(match["project"]):
        raise usage(f"not a valid repo id (host/<project path>): {_shown(text)}")
    return RepoInput(host=host, path=match["project"])


def _check_segments(backend: Backend, path: str) -> bool:
    count = len(path.split("/"))
    return count == 2 if backend == "github" else count >= 1


def bind_repo(repo: RepoInput, config: Config) -> RepoRef:
    backend = backend_of(repo.host, config)
    if not _check_segments(backend, repo.path) or not _no_option_segments(backend, repo.path):
        raise usage(f"not a valid {backend} repo id: {_shown(str(repo))}")
    return RepoRef(host=repo.host, backend=backend, path=repo.path)


def bind_pr(pr: PrInput, config: Config) -> PrRef:
    backend = backend_of(pr.host, config)
    shown = _shown(pr.raw or str(pr))
    invalid = ForgectlError("invalid_pr", f"not a valid {backend} pull request: {shown}")
    expected_kind = "pull" if backend == "github" else "merge_requests"
    if pr.kind not in (None, expected_kind):
        raise invalid
    project = pr.project
    if pr.kind is not None and backend == "gitlab":
        declared = config.hosts.get(pr.host)
        prefix = declared.api_prefix if declared is not None else None
        if prefix is not None:
            rest = "/" + project
            if not rest.startswith(prefix + "/"):
                raise invalid
            project = rest[len(prefix) + 1 :]
            if _BAD_PROJECT_SEGMENTS_IN_URL.intersection(project.split("/")):
                raise invalid
    if not project or not _check_segments(backend, project):
        raise invalid
    if not _no_option_segments(backend, project):
        raise invalid
    return PrRef(host=pr.host, backend=backend, project=project, number=pr.number)


# --- logins ------------------------------


def parse_login(text: str) -> str:
    """A login that is valid on at least one backend (or ``me``); ``usage`` otherwise."""
    if len(text) <= MAX_INPUT_CHARS and (
        _GITHUB_LOGIN_RE.fullmatch(text) or _GITLAB_LOGIN_RE.fullmatch(text)
    ):
        return text
    raise usage(f"not a valid login: {_shown(text)}")


def bind_login(text: str, backend: Backend) -> str:
    """The login under one backend's grammar. ``me`` passes: it is a valid login on both."""
    pattern = _GITHUB_LOGIN_RE if backend == "github" else _GITLAB_LOGIN_RE
    if len(text) > MAX_INPUT_CHARS or not pattern.fullmatch(text):
        raise usage(f"not a valid {backend} login: {_shown(text)}")
    return text


# --- typed comment, thread and review ids ------------------------------


@dataclass(frozen=True)
class TypedId:
    """``gh:ic|rc|rt|r:<node>``, ``gl:d:<40 hex>`` or ``gl:n:<integer>``. Opaque to callers."""

    host_prefix: Literal["gh", "gl"]
    kind: str
    node: str

    def __str__(self) -> str:
        return f"{self.host_prefix}:{self.kind}:{self.node}"


def parse_typed_id(text: str) -> TypedId:
    match = _TYPED_ID_RE.fullmatch(text) if len(text) <= MAX_INPUT_CHARS else None
    if match is None:
        raise usage(f"not a valid comment or thread id: {_shown(text)}")
    if match["gh"]:
        return TypedId("gh", match["ghkind"], match["ghnode"])
    if match["discussion"]:
        return TypedId("gl", "d", match["discussion"])
    return TypedId("gl", "n", match["note"])


# --- shas and branch names ------------------------------


def parse_sha(text: str) -> str:
    """A full 40-hex commit sha; uppercase is lowered."""
    if len(text) != 40 or not _SHA_RE.fullmatch(text):
        raise usage(f"not a full 40-hex sha: {_shown(text)}")
    return text.lower()


def parse_branch(name: str) -> str:
    """A branch name under ``git check-ref-format --branch``, with no subprocess.

    Beyond git's rules: no control, format, surrogate or line-separator character (so U+0085 and
    bidi marks are out), no ``refs`` or ``refs/`` prefix (a branch is never given as a full ref),
    and at most 1024 characters.
    """
    if not _valid_branch(name):
        raise usage(f"not a valid branch name: {_shown(name)}")
    return name


def _valid_branch(name: str) -> bool:
    if not name or len(name) > MAX_BRANCH_CHARS or name in ("@", "HEAD", "refs"):
        return False
    if name[0] in "-/" or name[-1] in "/." or name.startswith("refs/"):
        return False
    if ".." in name or "@{" in name or "//" in name:
        return False
    for ch in name:
        if (
            ch in _BRANCH_FORBIDDEN_CHARS
            or unicodedata.category(ch) in _BRANCH_FORBIDDEN_CATEGORIES
        ):
            return False
    return not any(part.startswith(".") or part.endswith(".lock") for part in name.split("/"))


# --- binding a parsed command line ------------------------------


def bind_args(args: Namespace, config: Config) -> None:
    """Replace the unbound identifiers of a parsed command line with bound ones, in place.

    Run after the config is loaded and before any handler or process: an undeclared host exits 20
    here. A value is only replaced once everything for the command has been checked, so a refusal
    leaves the unbound (printable) values for the ``--dry-run`` result.
    """
    updates: dict[str, object] = {}
    backends: set[Backend] = set()
    pr = getattr(args, "pr", None)
    if isinstance(pr, PrInput):
        updates["pr"] = bind_pr(pr, config)
    refs = getattr(args, "refs", None)
    if isinstance(refs, list) and any(isinstance(r, PrInput) for r in refs):
        # `snapshots`: every PR is bound, or none is (an undeclared host exits 20 here)
        updates["refs"] = [bind_pr(r, config) if isinstance(r, PrInput) else r for r in refs]
    repo = getattr(args, "repo", None)
    if isinstance(repo, RepoInput):
        bound = bind_repo(repo, config)
        updates["repo"] = bound
        backends.add(bound.backend)
    host = getattr(args, "host", None)
    if isinstance(host, str):
        backends.add(bind_host(host, config))
    for name in ("author", "review_requested_from", "user"):
        login = getattr(args, name, None)
        if isinstance(login, str):
            for backend in backends:  # no repo or host: the parse-time union grammar stands
                bind_login(login, backend)
    for name, value in updates.items():
        setattr(args, name, value)
