"""A minimal, safe lexer for JavaScript and TypeScript sources.

This is a **lexer**, not a parser, and deliberately so. What the milestone needs
from a JS file is a handful of well-known Odoo shapes -- imports, a class and
its base, ``static template``, ``registry.category(...).add(...)``, ``patch(...)``,
``useService(...)`` -- and those are recognisable from a token stream. Writing a
real ECMAScript parser without a dependency would be a large amount of code
whose failure modes are far worse than the ones here.

Nothing is executed. No Node, no interpreter, no sandbox: the file is read as
bytes, turned into tokens, and thrown away. A file that would delete your home
directory when run is analysed exactly as safely as an empty one.

Why a lexer rather than regular expressions on the raw text: a comment or a
string can contain anything. ``// registry.category("fake")`` and
``"class X extends Component"`` both match naive patterns, and an Odoo codebase
is full of both. Tokenising first means a scan sees code and only code.

The one genuinely ambiguous thing in JS lexing is ``/``: it starts a regular
expression in some positions and is division in others. The rule used here is
the standard conservative one -- a ``/`` is a regex only where a value cannot
legally appear -- and when it is wrong the damage is bounded to one file's
tokens. Everything this module feeds is therefore recorded at ``heuristic``
confidence, never as a fact read out of a grammar.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Token kinds the extractor cares about. Anything else is dropped.
NAME = "name"
STRING = "string"
PUNCT = "punct"
NUMBER = "number"

#: After one of these, a `/` starts a regular expression rather than division.
#: Everything else (an identifier, a literal, `)`, `]`, `}`) means division.
_REGEX_ALLOWED_AFTER = frozenset(
    {
        "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
        "throw", "case", "do", "else", "yield", "await",
    }
)
_REGEX_ALLOWED_PUNCT = frozenset(
    {
        "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";", "+", "-",
        "*", "/", "%", "<", ">", "~", "^", "=>", "===", "!==", "==", "!=",
        "&&", "||", "??", "+=", "-=", "*=", "/=",
    }
)

_IDENT_START = "_$"
_PUNCT_CHARS = "{}()[];,<>+-*/%&|^!~?:=."


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    line: int


class JsReadError(Exception):
    """The source could not be decoded. Malformed *syntax* never raises."""


def read_js(source: bytes) -> list[Token]:
    """Tokenise a JS/TS source. Returns tokens; never executes anything.

    Malformed JavaScript does not raise: a lexer has no opinion about grammar,
    and a half-written file mid-edit should still yield whatever it can rather
    than costing the whole run. Only an undecodable file is an error.
    """
    try:
        text = source.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise JsReadError(f"not valid UTF-8: {exc}") from exc

    tokens: list[Token] = []
    index = 0
    line = 1
    size = len(text)
    previous: Token | None = None

    while index < size:
        char = text[index]

        if char == "\n":
            line += 1
            index += 1
            continue
        if char.isspace():
            index += 1
            continue

        # -- comments ---------------------------------------------------
        if char == "/" and index + 1 < size and text[index + 1] == "/":
            end = text.find("\n", index)
            index = size if end < 0 else end
            continue
        if char == "/" and index + 1 < size and text[index + 1] == "*":
            end = text.find("*/", index + 2)
            if end < 0:
                break
            line += text.count("\n", index, end)
            index = end + 2
            continue

        # -- strings ----------------------------------------------------
        if char in "\"'":
            value, index, line = _read_quoted(text, index, line, char)
            token = Token(STRING, value, line)
            tokens.append(token)
            previous = token
            continue
        if char == "`":
            # A template literal's static head is useful (module paths are
            # sometimes written with backticks); interpolations are skipped
            # rather than evaluated.
            value, index, line = _read_template(text, index, line)
            token = Token(STRING, value, line)
            tokens.append(token)
            previous = token
            continue

        # -- regular expressions ----------------------------------------
        if char == "/" and _regex_may_start_here(previous):
            index, line = _skip_regex(text, index, line)
            previous = Token(PUNCT, "/regex/", line)
            continue

        # -- identifiers and keywords -----------------------------------
        if char.isalpha() or char in _IDENT_START:
            start = index
            while index < size and (text[index].isalnum() or text[index] in _IDENT_START):
                index += 1
            token = Token(NAME, text[start:index], line)
            tokens.append(token)
            previous = token
            continue

        # -- numbers ----------------------------------------------------
        if char.isdigit():
            start = index
            while index < size and (text[index].isalnum() or text[index] in "._"):
                index += 1
            token = Token(NUMBER, text[start:index], line)
            tokens.append(token)
            previous = token
            continue

        # -- punctuation ------------------------------------------------
        if char in _PUNCT_CHARS:
            for width in (3, 2):
                candidate = text[index:index + width]
                if candidate in _REGEX_ALLOWED_PUNCT or candidate in ("...", "?.", "**"):
                    token = Token(PUNCT, candidate, line)
                    tokens.append(token)
                    previous = token
                    index += width
                    break
            else:
                token = Token(PUNCT, char, line)
                tokens.append(token)
                previous = token
                index += 1
            continue

        # Anything else (stray unicode, decorators, JSX punctuation) is not
        # something the extractor keys on. Skipping keeps the stream clean.
        index += 1

    return tokens


def _read_quoted(text: str, index: int, line: int, quote: str):
    """The body of a '...' or "..." literal, escapes left as written."""
    start_line = line
    index += 1
    chunks: list[str] = []
    size = len(text)
    while index < size:
        char = text[index]
        if char == "\\" and index + 1 < size:
            # Kept verbatim: nothing here needs the decoded value, and decoding
            # invites surprises. \\" is the one that matters for termination.
            chunks.append(text[index + 1])
            index += 2
            continue
        if char == quote:
            index += 1
            break
        if char == "\n":
            line += 1
        chunks.append(char)
        index += 1
    return "".join(chunks), index, max(line, start_line)


def _read_template(text: str, index: int, line: int):
    """A template literal's literal text, with ${...} spans dropped."""
    index += 1
    chunks: list[str] = []
    size = len(text)
    depth = 0
    while index < size:
        char = text[index]
        if char == "\\" and index + 1 < size:
            index += 2
            continue
        if char == "\n":
            line += 1
            index += 1
            continue
        if depth == 0 and char == "$" and index + 1 < size and text[index + 1] == "{":
            depth = 1
            index += 2
            continue
        if depth:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
            continue
        if char == "`":
            index += 1
            break
        chunks.append(char)
        index += 1
    return "".join(chunks), index, line


def _regex_may_start_here(previous: Token | None) -> bool:
    """Whether a `/` here begins a regex rather than a division.

    Conservative: a regex is only possible where a *value* is expected. After
    an identifier, a number, a string, or a closing bracket, a value has just
    been produced, so `/` divides.
    """
    if previous is None:
        return True
    if previous.kind in (STRING, NUMBER):
        return False
    if previous.kind == NAME:
        return previous.value in _REGEX_ALLOWED_AFTER
    if previous.kind == PUNCT:
        return previous.value not in (")", "]", "}", "++", "--")
    return True


def _skip_regex(text: str, index: int, line: int):
    """Step over a regex literal, respecting classes and escapes."""
    index += 1
    size = len(text)
    in_class = False
    while index < size:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "\n":
            # An unterminated regex: treat the `/` as punctuation after all
            # rather than swallowing the rest of the file.
            return index, line
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            index += 1
            break
        index += 1
    while index < size and text[index].isalpha():
        index += 1
    return index, line


# --------------------------------------------------------------------------
# small helpers the extractor uses to walk a token stream
# --------------------------------------------------------------------------


def is_name(token: Token | None, value: str) -> bool:
    return token is not None and token.kind == NAME and token.value == value


def is_punct(token: Token | None, value: str) -> bool:
    return token is not None and token.kind == PUNCT and token.value == value


def at(tokens: list[Token], index: int) -> Token | None:
    return tokens[index] if 0 <= index < len(tokens) else None


def dotted_name(tokens: list[Token], index: int) -> tuple[str | None, int]:
    """Read ``a.b.c`` starting at ``index``. Returns (name, next index)."""
    token = at(tokens, index)
    if token is None or token.kind != NAME:
        return None, index
    parts = [token.value]
    index += 1
    while is_punct(at(tokens, index), ".") and at(tokens, index + 1) is not None \
            and at(tokens, index + 1).kind == NAME:
        parts.append(at(tokens, index + 1).value)
        index += 2
    return ".".join(parts), index
