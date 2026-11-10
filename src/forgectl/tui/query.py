"""The `browse` query language: a hand-written lexer, parser, formatter and evaluator.

One query is an optional filter and an optional ``ORDER BY``::

    ci IN (failing, pending) AND author = me AND repo LIKE 'acme/*' AND age > 7d
        AND NOT draft ORDER BY age DESC, ci

The text is typed by the user, and the values it is matched against are forge text, so this module
is built to be total and bounded:

* ``parse`` never raises. It returns a ``Query`` or a ``ParseError`` with a position.
* The text is at most ``MAX_LENGTH`` characters, parentheses nest at most ``MAX_DEPTH`` deep, and a
  query has at most ``MAX_CONDITIONS`` conditions, ``MAX_LIST`` values per ``IN`` and
  ``MAX_ORDER`` sort keys. ``NOT`` chains and ``AND``/``OR`` chains are loops, not recursion.
* There is no ``eval`` and no regular expression built from user text. ``LIKE`` patterns go to the
  iterative glob matcher of ``forgectl.activity`` (work at most pattern length times text length).
* Control, format and other unprintable characters are refused everywhere, quoted or not.

Evaluation is three-valued, like SQL: a condition on a value that is not known (a row whose
snapshot has not loaded, a count the snapshot did not report) is unknown, ``NOT`` and the other
conditions leave unknown as it is, and a row matches only when the whole filter is true. So a row
with an unknown value matches no condition that needs it, whatever the operator, unless the
condition is ``IS NULL``. This module has no widgets and no I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_LENGTH = 1000
MAX_DEPTH = 20
MAX_CONDITIONS = 50
MAX_LIST = 50
MAX_ORDER = 8
MAX_WORD = 200

# -- fields ------------------------------------------------------------------------------------

TEXT, COUNT, TIME, BOOL, SEARCH = "text", "count", "time", "bool", "search"

# Filter fields and their kind. ``age`` is the time since the last update and ``pushed`` since the
# last push (both in seconds). ``text`` is free text: it matches the title, the repo and the author.
FIELDS: dict[str, str] = {
    "repo": TEXT,
    "pr": TEXT,
    "title": TEXT,
    "author": TEXT,
    "state": TEXT,
    "ci": TEXT,
    "review": TEXT,
    "blocker": TEXT,
    "number": COUNT,
    "approvals": COUNT,
    "comments": COUNT,
    "unresolved": COUNT,
    "others_since_push": COUNT,
    "awaiting_reply": COUNT,
    "age": TIME,
    "pushed": TIME,
    "draft": BOOL,
    "authored": BOOL,
    "requested": BOOL,
    "text": SEARCH,
}

# The columns ``ORDER BY`` takes: ``columns.SORTABLE`` (a test keeps the two equal).
ORDER_COLUMNS = (
    "pr",
    "badge",
    "ci",
    "approvals",
    "review",
    "comments",
    "age",
    "title",
    "author",
    "roles",
)

VALUES: dict[str, tuple[str, ...]] = {
    "ci": ("failing", "pending", "passing", "none"),
    "review": ("approved", "changes_requested", "review_required", "none"),
}

# operators, as stored. A bare boolean column is TRUE (``draft``) or FALSE (``NOT draft``).
EQ, NE, LT, LE, GT, GE = "=", "!=", "<", "<=", ">", ">="
LIKE, NOT_LIKE, IN, NOT_IN = "LIKE", "NOT LIKE", "IN", "NOT IN"
IS_NULL, NOT_NULL, TRUE, FALSE = "IS NULL", "IS NOT NULL", "TRUE", "FALSE"

_NEGATED = {
    EQ: NE,
    NE: EQ,
    LT: GE,
    GE: LT,
    LE: GT,
    GT: LE,
    LIKE: NOT_LIKE,
    NOT_LIKE: LIKE,
    IN: NOT_IN,
    NOT_IN: IN,
    IS_NULL: NOT_NULL,
    NOT_NULL: IS_NULL,
    TRUE: FALSE,
    FALSE: TRUE,
}

_COMPARE = (EQ, NE, LT, LE, GT, GE)
_NULLS = (IS_NULL, NOT_NULL)
OPERATORS: dict[str, tuple[str, ...]] = {
    TEXT: (EQ, NE, LIKE, NOT_LIKE, IN, NOT_IN, *_NULLS),
    COUNT: (*_COMPARE, IN, NOT_IN, *_NULLS, TRUE, FALSE),
    TIME: (*_COMPARE, *_NULLS),
    BOOL: (TRUE, FALSE, *_NULLS),
    SEARCH: (LIKE, NOT_LIKE),
}

_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def lower(text: str) -> str:
    """ASCII-only lowercase: keywords, columns and enum values match by it, so a Unicode
    lookalike (the long s, the Kelvin sign) is never a letter of a keyword."""
    return text.translate(_ASCII_LOWER)


KEYWORDS = frozenset(
    {"and", "or", "not", "like", "in", "is", "null", "order", "by", "asc", "desc", "me"}
)


class Me:
    """The literal ``me``: the host's own login (only for ``author``)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ME"


ME = Me()
Value = str | int | bool | Me

# -- the tree ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cond:
    field: str
    op: str
    values: tuple[Value, ...] = ()


@dataclass(frozen=True)
class Not:
    child: Node  # only ever a group: a condition's NOT is folded into its operator


@dataclass(frozen=True)
class And:
    children: tuple[Node, ...]


@dataclass(frozen=True)
class Or:
    children: tuple[Node, ...]


Node = Cond | Not | And | Or
OrderKey = tuple[str, bool]  # (column, descending)


@dataclass(frozen=True)
class Query:
    where: Node | None = None
    order: tuple[OrderKey, ...] = ()


@dataclass(frozen=True)
class ParseError:
    """Why a text is not a query. ``pos`` is a 0-based index into the text."""

    message: str
    pos: int

    def __str__(self) -> str:
        return f"{self.message} (column {self.pos + 1})"


class _Fail(Exception):
    def __init__(self, message: str, pos: int) -> None:
        super().__init__(message)
        self.message = message
        self.pos = pos


# -- lexer -------------------------------------------------------------------------------------

WORD, STRING, LPAREN, RPAREN, COMMA, OP, END = "word", "string", "(", ")", ",", "op", "end"
_SPECIAL = set("()',\"=!<>")


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    pos: int

    def keyword(self) -> str | None:
        return lower(self.text) if self.kind == WORD else None


def _printable(ch: str) -> bool:
    return ch.isprintable() or ch in " \t"


def _lex(text: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t":
            i += 1
            continue
        if not _printable(ch):
            raise _Fail(f"unprintable character U+{ord(ch):04X}", i)
        if ch in "(),":
            tokens.append(Token(ch, ch, i))
            i += 1
        elif ch in "'\"":
            end = _string_end(text, i)
            tokens.append(Token(STRING, _unquote(text, i, end), i))
            i = end
        elif ch in "=!<>":
            two = text[i : i + 2]
            if two in ("!=", "<=", ">=", "<>"):
                tokens.append(Token(OP, "!=" if two == "<>" else two, i))
                i += 2
            elif ch == "!":
                raise _Fail("'!' must be followed by '=' (or use NOT)", i)
            else:
                tokens.append(Token(OP, ch, i))
                i += 1
        else:
            j = i
            while j < n and text[j] not in " \t" and text[j] not in _SPECIAL:
                if not _printable(text[j]):
                    raise _Fail(f"unprintable character U+{ord(text[j]):04X}", j)
                j += 1
            if j - i > MAX_WORD:
                raise _Fail(f"a word is longer than {MAX_WORD} characters", i)
            tokens.append(Token(WORD, text[i:j], i))
            i = j
    tokens.append(Token(END, "", n))
    return tokens


def _string_end(text: str, start: int) -> int:
    """The index after the closing quote of the string that opens at ``start``."""
    quote = text[start]
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
        elif ch == quote:
            return i + 1
        else:
            i += 1
    raise _Fail("unterminated quote", start)


def _unquote(text: str, start: int, end: int) -> str:
    out: list[str] = []
    i = start + 1
    while i < end - 1:
        ch = text[i]
        if ch == "\\":
            i += 1
            if i >= end - 1:
                raise _Fail("a backslash at the end of the quote", i - 1)
            ch = text[i]
        if not _printable(ch) or ch == "\t":
            raise _Fail(f"unprintable character U+{ord(ch):04X}", i)
        out.append(ch)
        i += 1
    value = "".join(out)
    if len(value) > MAX_WORD:
        raise _Fail(f"a value is longer than {MAX_WORD} characters", start)
    return value


# -- durations ---------------------------------------------------------------------------------

_UNITS = (("w", 7 * 86400), ("d", 86400), ("h", 3600), ("m", 60))
MAX_SECONDS = 1000 * 365 * 86400


def parse_duration(word: str) -> int | None:
    """``1w2d`` as seconds; units ``w``, ``d``, ``h``, ``m`` once each, largest first; None when
    the word is not a duration."""
    seconds, i, last = 0, 0, -1
    word = lower(word)
    if not word:
        return None
    while i < len(word):
        j = i
        while j < len(word) and word[j] in "0123456789":
            j += 1
        if j == i or j - i > 6 or j >= len(word):
            return None
        unit = next((k for k, (u, _) in enumerate(_UNITS) if u == word[j]), None)
        if unit is None or unit <= last:
            return None
        seconds += int(word[i:j]) * _UNITS[unit][1]
        last = unit
        i = j + 1
    return seconds if seconds <= MAX_SECONDS else None


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0m"
    out = []
    for unit, size in _UNITS:
        count, seconds = divmod(seconds, size)
        if count:
            out.append(f"{count}{unit}")
    return "".join(out) or "0m"


# -- parser ------------------------------------------------------------------------------------


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens = _lex(text)
        self.i = 0
        self.conditions = 0

    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def take(self) -> Token:
        token = self.tokens[self.i]
        if token.kind != END:
            self.i += 1
        return token

    def kw(self, *words: str) -> bool:
        return self.tok.keyword() in words

    def expect_kw(self, word: str) -> Token:
        if not self.kw(word):
            raise _Fail(f"expected {word.upper()}", self.tok.pos)
        return self.take()

    # query := [or] [ORDER BY keys]
    def query(self) -> Query:
        where: Node | None = None
        if self.tok.kind != END and not self.kw("order"):
            where = self.or_expr(0)
        order: tuple[OrderKey, ...] = ()
        if self.kw("order"):
            self.take()
            self.expect_kw("by")
            order = self.order_keys()
        if self.tok.kind != END:
            raise _Fail(self.unexpected(), self.tok.pos)
        return Query(where, order)

    def unexpected(self) -> str:
        t = self.tok
        if t.kind == END:
            return "unexpected end of the query"
        if t.kind == RPAREN:
            return "unmatched ')'"
        shown = t.text if len(t.text) <= 20 else t.text[:20] + "..."
        return f"unexpected {shown!r}"

    def order_keys(self) -> tuple[OrderKey, ...]:
        keys: list[OrderKey] = []
        while True:
            t = self.take()
            name = lower(t.text) if t.kind == WORD else ""
            if name not in ORDER_COLUMNS:
                raise _Fail("expected a column to order by: " + ", ".join(ORDER_COLUMNS), t.pos)
            if any(k[0] == name for k in keys):
                raise _Fail(f"{name} is ordered twice", t.pos)
            descending = False
            if self.kw("asc", "desc"):
                descending = self.take().keyword() == "desc"
            keys.append((name, descending))
            if len(keys) > MAX_ORDER:
                raise _Fail(f"at most {MAX_ORDER} sort keys", t.pos)
            if self.tok.kind != COMMA:
                return tuple(keys)
            self.take()

    def or_expr(self, depth: int) -> Node:
        parts = [self.and_expr(depth)]
        while self.kw("or"):
            self.take()
            parts.append(self.and_expr(depth))
        flat = [q for p in parts for q in (p.children if isinstance(p, Or) else (p,))]
        return flat[0] if len(flat) == 1 else Or(tuple(flat))

    def and_expr(self, depth: int) -> Node:
        parts = [self.not_expr(depth)]
        while self.kw("and"):
            self.take()
            parts.append(self.not_expr(depth))
        flat = [q for p in parts for q in (p.children if isinstance(p, And) else (p,))]
        return flat[0] if len(flat) == 1 else And(tuple(flat))

    def not_expr(self, depth: int) -> Node:
        negations = 0
        while self.kw("not"):
            self.take()
            negations += 1
        node = self.primary(depth)
        return negate(node) if negations % 2 else node

    def primary(self, depth: int) -> Node:
        t = self.tok
        if t.kind == LPAREN:
            if depth + 1 > MAX_DEPTH:
                raise _Fail(f"parentheses nest more than {MAX_DEPTH} deep", t.pos)
            self.take()
            node = self.or_expr(depth + 1)
            if self.tok.kind != RPAREN:
                raise _Fail("expected ')'", self.tok.pos)
            self.take()
            return node
        if t.kind != WORD or t.keyword() in KEYWORDS:
            raise _Fail("expected a column name or '('", t.pos)
        name = lower(t.text)
        if name not in FIELDS:
            raise _Fail(f"unknown column {t.text[:30]!r}: {', '.join(FIELDS)}", t.pos)
        self.take()
        self.conditions += 1
        if self.conditions > MAX_CONDITIONS:
            raise _Fail(f"at most {MAX_CONDITIONS} conditions", t.pos)
        return self.condition(name, t)

    def condition(self, name: str, at: Token) -> Cond:
        kind = FIELDS[name]
        t = self.tok
        negate_op = False
        if self.kw("not"):  # NOT LIKE, NOT IN
            self.take()
            negate_op = True
            if not self.kw("like", "in"):
                raise _Fail("expected LIKE or IN after NOT", self.tok.pos)
            t = self.tok
        op: str
        values: tuple[Value, ...] = ()
        if t.kind == OP:
            self.take()
            op = t.text
            values = (self.value(name, kind),)
        elif self.kw("like"):
            self.take()
            op = LIKE
            values = (self.value(name, kind, pattern=True),)
        elif self.kw("in"):
            self.take()
            op = IN
            values = self.value_list(name, kind)
        elif self.kw("is"):
            self.take()
            op = IS_NULL
            if self.kw("not"):
                self.take()
                op = NOT_NULL
            self.expect_kw("null")
        elif kind in (BOOL, COUNT):  # a bare column
            return Cond(name, TRUE)
        else:
            raise _Fail(f"expected an operator after {name}", t.pos)
        if negate_op:
            op = _NEGATED[op]
        if kind == BOOL and op in (EQ, NE):
            truth = bool(values) and values[0] is True
            return Cond(name, TRUE if truth == (op == EQ) else FALSE)
        if op not in OPERATORS[kind]:
            raise _Fail(f"{op} does not apply to {name} ({kind})", t.pos)
        return Cond(name, op, values)

    def value_list(self, name: str, kind: str) -> tuple[Value, ...]:
        if self.tok.kind != LPAREN:
            raise _Fail("expected '(' after IN", self.tok.pos)
        self.take()
        values = [self.value(name, kind)]
        while self.tok.kind == COMMA:
            self.take()
            values.append(self.value(name, kind))
            if len(values) > MAX_LIST:
                raise _Fail(f"at most {MAX_LIST} values in a list", self.tok.pos)
        if self.tok.kind != RPAREN:
            raise _Fail("expected ',' or ')'", self.tok.pos)
        self.take()
        return tuple(values)

    def value(self, name: str, kind: str, *, pattern: bool = False) -> Value:
        t = self.tok
        if t.kind == WORD and t.keyword() in KEYWORDS - {"me"}:
            raise _Fail(f"expected a value, found {t.text.upper()} (quote it to match it)", t.pos)
        if t.kind not in (WORD, STRING):
            raise _Fail("expected a value", t.pos)
        self.take()
        if kind == TIME:
            seconds = parse_duration(t.text) if t.kind == WORD else None
            if seconds is None:
                raise _Fail("expected a duration such as 90m, 12h, 2d, 1w or 1w2d", t.pos)
            return seconds
        if kind == COUNT:
            digits = t.text if t.kind == WORD else ""
            if not (digits.isascii() and digits.isdigit() and len(digits) <= 9):
                raise _Fail("expected a whole number", t.pos)
            return int(digits)
        if kind == BOOL:
            word = lower(t.text) if t.kind == WORD else ""
            if word not in ("true", "false"):
                raise _Fail("expected true or false", t.pos)
            return word == "true"
        if t.kind == WORD and lower(t.text) == "me":
            if name != "author":
                raise _Fail("me applies to author only (quote it to match the text)", t.pos)
            if pattern:
                raise _Fail("me cannot be a pattern (quote it to match the text)", t.pos)
            return ME
        if name in VALUES and not pattern:
            if lower(t.text) not in VALUES[name]:
                raise _Fail(f"{name} is one of: {', '.join(VALUES[name])}", t.pos)
            return lower(t.text)  # canonical: an enum value is lowercase
        return t.text


def negate(node: Node) -> Node:
    """``NOT node``: a condition takes the opposite operator (unknown stays unknown), a group is
    wrapped, and a ``NOT`` of a ``NOT`` is the group again."""
    if isinstance(node, Cond):
        return Cond(node.field, _NEGATED[node.op], node.values)
    if isinstance(node, Not):
        return node.child
    return Not(node)


def parse(text: str) -> Query | ParseError:
    """The query in ``text``, or why it is not one. Never raises, whatever the text."""
    try:
        if not isinstance(text, str):
            return ParseError("the query is not text", 0)
        if len(text) > MAX_LENGTH:
            return ParseError(f"the query is longer than {MAX_LENGTH} characters", MAX_LENGTH)
        result = _Parser(text).query()
        if len(format_query(result)) > MAX_LENGTH:
            # the canonical text is what is stored and shown again, so it must fit too
            return ParseError(
                f"the query is longer than {MAX_LENGTH} characters when written canonically",
                MAX_LENGTH,
            )
        return result
    except _Fail as fail:
        return ParseError(fail.message, fail.pos)
    except Exception:  # noqa: BLE001 - a backstop: no text may take the caller down
        return ParseError("the query could not be read", 0)


# -- formatting --------------------------------------------------------------------------------


def _bare_ok(text: str) -> bool:
    return (
        bool(text)
        and lower(text) not in KEYWORDS
        and all(c not in " \t\\" and c not in _SPECIAL and c.isprintable() for c in text)
    )


def quote(text: str) -> str:
    """``text`` as one value: bare when it is one word, else single-quoted with escapes."""
    if _bare_ok(text):
        return text
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def format_value(kind: str, value: Value) -> str:
    if isinstance(value, Me):
        return "me"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return format_duration(value) if kind == TIME else str(value)
    return quote(value)


def format_cond(c: Cond) -> str:
    kind = FIELDS[c.field]
    if c.op == TRUE:
        return c.field
    if c.op == FALSE:
        return f"NOT {c.field}"
    if c.op in _NULLS:
        return f"{c.field} {c.op}"
    shown = [format_value(kind, v) for v in c.values]
    if c.op in (IN, NOT_IN):
        return f"{c.field} {c.op} ({', '.join(shown)})"
    return f"{c.field} {c.op} {shown[0]}"


def format_node(node: Node, parent: str = "") -> str:
    if isinstance(node, Cond):
        return format_cond(node)
    if isinstance(node, Not):
        return f"NOT ({format_node(node.child)})"
    word = " AND " if isinstance(node, And) else " OR "
    text = word.join(
        format_node(c, "and" if isinstance(node, And) else "or") for c in node.children
    )
    return f"({text})" if isinstance(node, Or) and parent == "and" else text


def format_order(order: tuple[OrderKey, ...]) -> str:
    return ", ".join(f"{c} DESC" if d else c for c, d in order)


def format_query(q: Query) -> str:
    """The canonical text of a query: ``parse(format_query(q)) == q``."""
    parts = []
    if q.where is not None:
        parts.append(format_node(q.where))
    if q.order:
        parts.append("ORDER BY " + format_order(q.order))
    return " ".join(parts)


def canonical(text: str) -> str | ParseError:
    result = parse(text)
    return result if isinstance(result, ParseError) else format_query(result)


# -- evaluation --------------------------------------------------------------------------------

# ``get(field)``: the field's value for one row, None when it is not known. Text is a str, a count
# an int, a time an age in seconds, a bool a bool, and ``text`` a tuple of the strings searched.
# ``get("@me")`` is whether the row's author is the viewer (None when not known).
Getter = Callable[[str], Any]


def _glob(pattern: str, text: str) -> bool:
    from forgectl.activity import glob_match  # here: the config module imports this one

    return glob_match(pattern, text)


def _one(c: Cond, kind: str, got: Any, value: Value, get: Getter) -> bool:
    if isinstance(value, Me):
        return bool(get("@me"))
    if kind == TEXT:
        text = str(got)
        if c.op in (LIKE, NOT_LIKE):
            return _glob(str(value), text)
        return text.casefold() == str(value).casefold()
    return bool(got == value)  # EQ, NE, IN and NOT_IN: the caller applies the negation


def _empty_text(value: Any) -> bool:
    return isinstance(value, str) and not value


def _leaf(c: Cond, get: Getter) -> bool | None:
    kind = FIELDS[c.field]
    got = get(c.field)
    # no value is a missing one; for text an empty string is the same (a loaded snapshot with no
    # blocker has known-empty text, which is not unknown)
    empty = got is None or (kind == TEXT and _empty_text(got))
    if c.op == IS_NULL:
        return empty
    if c.op == NOT_NULL:
        return not empty
    if got is None or (any(isinstance(v, Me) for v in c.values) and get("@me") is None):
        return None
    if kind == SEARCH:
        hit = any(_glob(str(c.values[0]), str(s)) for s in got)
        return hit if c.op == LIKE else not hit
    if c.op in (TRUE, FALSE):
        truth = bool(got) if kind == BOOL else got > 0
        return truth if c.op == TRUE else not truth
    if c.op in (EQ, NE, LIKE, NOT_LIKE, IN, NOT_IN):
        hit = any(_one(c, kind, got, v, get) for v in c.values)
        return hit if c.op in (EQ, LIKE, IN) else not hit
    value = c.values[0]
    assert isinstance(value, int)
    if c.op == LT:
        return bool(got < value)
    if c.op == LE:
        return bool(got <= value)
    if c.op == GT:
        return bool(got > value)
    return bool(got >= value)


def evaluate(node: Node | None, get: Getter) -> bool | None:
    """True, False, or None when the answer needs a value that is not known. No filter is true."""
    if node is None:
        return True
    if isinstance(node, Cond):
        return _leaf(node, get)
    if isinstance(node, Not):
        inner = evaluate(node.child, get)
        return None if inner is None else not inner
    # three-valued and short-circuiting: AND stops at the first False, OR at the first True
    stop = not isinstance(node, And)  # the value that decides the group
    unknown = False
    for child in node.children:
        result = evaluate(child, get)
        if result is stop:
            return stop
        unknown = unknown or result is None
    return None if unknown else not stop


def matches(node: Node | None, get: Getter) -> bool:
    return evaluate(node, get) is True


# -- the form ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class FormFilter:
    """One filter row of the form: a column, an operator and the value in query syntax
    (``failing``, ``'a b'``, or ``failing, pending`` for IN; empty for the operators with none)."""

    field: str
    op: str
    value: str = ""


NO_VALUE = (IS_NULL, NOT_NULL, TRUE, FALSE)


@dataclass(frozen=True)
class Form:
    """A query as the form shows it. ``filters`` is None when the filter has an ``OR``, a
    parenthesised group or a ``NOT (...)``: the form cannot show it, only the query line can.
    ``rest`` is then the filter's text, which a form edit of the sort keeps."""

    filters: tuple[FormFilter, ...] | None
    order: tuple[OrderKey, ...]
    rest: str = ""


def _conjuncts(node: Node, out: list[Cond]) -> bool:
    stack = [node]
    while stack:
        n = stack.pop(0)
        if isinstance(n, Cond):
            out.append(n)
        elif isinstance(n, And):
            stack = [*n.children, *stack]
        else:
            return False
    return True


def to_form(q: Query) -> Form:
    if q.where is None:
        return Form((), q.order)
    conds: list[Cond] = []
    if not _conjuncts(q.where, conds):
        return Form(None, q.order, format_node(q.where))
    rows = []
    for c in conds:
        kind = FIELDS[c.field]
        value = ", ".join(format_value(kind, v) for v in c.values)
        rows.append(FormFilter(c.field, c.op, value))
    return Form(tuple(rows), q.order)


def form_text(form: Form) -> str:
    """The query text a form says. It is parsed like any other text, so a row with a bad value
    is an error there, never a way around the parser."""
    if form.filters is None:
        where = form.rest
    else:
        pieces = []
        for f in form.filters:
            if f.op == TRUE:
                pieces.append(f.field)
            elif f.op == FALSE:
                pieces.append(f"NOT {f.field}")
            elif f.op in _NULLS:
                pieces.append(f"{f.field} {f.op}")
            elif f.op in (IN, NOT_IN):
                pieces.append(f"{f.field} {f.op} ({f.value})")
            else:
                pieces.append(f"{f.field} {f.op} {f.value}")
        where = " AND ".join(pieces)
    order = f"ORDER BY {format_order(form.order)}" if form.order else ""
    return " ".join(p for p in (where, order) if p)


# -- quick filters -----------------------------------------------------------------------------

PRESETS: dict[str, Cond] = {
    "authored": Cond("authored", TRUE),
    "requested": Cond("requested", TRUE),
    "ci-failing": Cond("ci", EQ, ("failing",)),
    "new-comments": Cond("others_since_push", TRUE),
}


def _same(a: Cond, b: Cond) -> bool:
    """The same term: column, operator and values, an enum value in any case."""

    def fold(values: tuple[Value, ...]) -> tuple[Value, ...]:
        return tuple(lower(v) if isinstance(v, str) else v for v in values)

    return a.field == b.field and a.op == b.op and fold(a.values) == fold(b.values)


def toggle_term(q: Query, term: Cond) -> Query:
    """The quick filter ``term`` switched in ``q``: removed when the filter is that condition or
    an AND that has it among its direct conditions (whatever else it holds), else added with AND
    (an ``OR`` filter is grouped first)."""
    where = q.where
    if isinstance(where, Cond) and _same(where, term):
        return Query(None, q.order)
    if isinstance(where, And):
        rest = tuple(c for c in where.children if not (isinstance(c, Cond) and _same(c, term)))
        if len(rest) != len(where.children):
            return Query(rest[0] if len(rest) == 1 else And(rest), q.order)
    if where is None:
        return Query(term, q.order)
    if isinstance(where, And):
        return Query(And((*where.children, term)), q.order)
    return Query(And((where, term)), q.order)


def fits(q: Query) -> bool:
    """Whether the canonical text of ``q`` parses back (within every limit): a query built in
    code, not typed, is checked before it becomes the active one."""
    return isinstance(parse(format_query(q)), Query)


def conditions(node: Node | None) -> int:
    if node is None:
        return 0
    if isinstance(node, Cond):
        return 1
    if isinstance(node, Not):
        return conditions(node.child)
    return sum(conditions(c) for c in node.children)
