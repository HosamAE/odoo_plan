"""Recognising tests, and what they name, without running any of them.

Python is read with :mod:`ast`; JavaScript with the project's own lexer. Both
readers are the ones earlier milestones already trust, and neither imports or
executes the file it reads. A test suite is the one part of a repository whose
whole purpose is to be run, so it is worth saying plainly: this never runs one.

What a reference means here
---------------------------
A test that contains the literal ``"hx.conference.booking"`` is evidence that
somebody wrote that model's name in that test. It is **not** evidence that the
model is covered, that the test passes, or that the behaviour is correct. And
the absence of a reference is not evidence of anything at all -- a test can
exercise a model through a fixture, a helper, or a related record without ever
naming it. Callers are told this in every answer; the reader's job is only to
find the literals.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field as dc_field

from .jsread import JsReadError, read_js

#: Base classes that make a class an Odoo test. Matched on the last segment, so
#: `common.TransactionCase` and a project's own `HxApiCase` both count.
TEST_BASE_MARKERS = ("TransactionCase", "SingleTransactionCase", "HttpCase",
                     "SavepointCase", "TestCase", "Case", "Common")

#: A model name: dotted, lowercase, at least two segments.
MODEL_SHAPE = re.compile(r"^[a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)+$")
#: An XML ID: `module.identifier`, exactly two segments.
XMLID_SHAPE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
#: A route path.
ROUTE_SHAPE = re.compile(r"^/[A-Za-z0-9_\-/.<>:]*$")

#: ORM verbs that say nothing about which behaviour a test exercises.
UNINFORMATIVE_METHODS = frozenset({
    "get", "set", "append", "join", "format", "split", "strip", "lower",
    "upper", "keys", "values", "items", "assertEqual", "assertTrue",
    "assertFalse", "assertIn", "assertNotIn", "assertRaises", "assertIsNone",
    "assertIsNotNone", "assertGreater", "assertLess", "setUp", "tearDown",
    "browse", "search", "create", "write", "unlink", "read", "ref", "env",
    "sudo", "with_context", "with_user", "flush", "invalidate_cache",
})


@dataclass
class TestCase:
    name: str
    line: int
    end_line: int


@dataclass
class TestClass:
    name: str
    bases: tuple[str, ...]
    tags: tuple[str, ...]
    line: int
    cases: list[TestCase] = dc_field(default_factory=list)


@dataclass
class Reference:
    kind: str            # model | xmlid | route | method
    value: str
    line: int


@dataclass
class TestFile:
    language: str        # python | javascript
    framework: str | None
    classes: list[TestClass] = dc_field(default_factory=list)
    references: list[Reference] = dc_field(default_factory=list)
    parsed: bool = True
    findings: list[tuple[str, str, str, int | None]] = dc_field(default_factory=list)


def looks_like_a_test_path(rel_path: str) -> bool:
    """Odoo's own convention: a `tests` package, or a `test_`-prefixed file."""
    lowered = rel_path.replace("\\", "/").lower()
    if "/tests/" in f"/{lowered}":
        return True
    name = lowered.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.startswith("test")


def _literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dotted(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _tags_of(node: ast.ClassDef) -> tuple[str, ...]:
    """The literal arguments of an `@tagged(...)` decorator."""
    found = []
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call else decorator
        name = target.attr if isinstance(target, ast.Attribute) else (
            target.id if isinstance(target, ast.Name) else None
        )
        if name != "tagged" or call is None:
            continue
        for argument in call.args:
            value = _literal(argument)
            if value:
                found.append(value)
    return tuple(found)


def _is_test_class(bases) -> bool:
    return any(
        base and any(base.split(".")[-1].endswith(marker)
                     for marker in TEST_BASE_MARKERS)
        for base in bases
    )


def classify_literal(value: str) -> str | None:
    """Which kind of subject a string literal could name, if any."""
    if not value or len(value) > 120:
        return None
    if ROUTE_SHAPE.match(value) and len(value) > 1:
        return "route"
    if MODEL_SHAPE.match(value):
        # A model name and an XML ID overlap in shape: `sale.order` is a model,
        # `base.group_user` is an XML ID, and nothing in the string says which.
        # The caller resolves it against what the graph already holds; here it
        # is only reported as a candidate for both.
        return "model_or_xmlid"
    return None


def read_python_tests(source: bytes) -> TestFile:
    """Test classes, their cases, their tags, and the literals they name."""
    result = TestFile(language="python", framework="odoo.tests")
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        result.parsed = False
        result.findings.append((
            "error", "test_syntax_error",
            f"could not parse: {type(exc).__name__}: "
            f"{exc.args[0] if exc.args else exc}",
            getattr(exc, "lineno", None),
        ))
        return result

    for node in _iter_class_defs(tree.body):
        bases = tuple(filter(None, (_dotted(base) for base in node.bases)))
        if not _is_test_class(bases):
            continue
        klass = TestClass(
            name=node.name, bases=bases, tags=_tags_of(node), line=node.lineno,
        )
        for statement in node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and statement.name.startswith("test"):
                klass.cases.append(TestCase(
                    name=statement.name, line=statement.lineno,
                    end_line=getattr(statement, "end_lineno", statement.lineno),
                ))
        result.classes.append(klass)

    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            kind = classify_literal(node.value)
            if kind and (kind, node.value) not in seen:
                seen.add((kind, node.value))
                result.references.append(
                    Reference(kind=kind, value=node.value, line=node.lineno)
                )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if name.startswith("action_") or name.startswith("button_") \
                    or name.startswith("_cron_"):
                if ("method", name) not in seen:
                    seen.add(("method", name))
                    result.references.append(
                        Reference(kind="method", value=name, line=node.lineno)
                    )
    return result


def _iter_class_defs(body):
    for node in body:
        if isinstance(node, ast.ClassDef):
            yield node
            yield from _iter_class_defs(node.body)


#: How a JavaScript test announces itself. A tour is Odoo's own shape; the
#: others are the generic runners a module might bring.
_JS_FRAMEWORKS = (
    ("web_tour", re.compile(r"registry\s*\.\s*category\s*\(\s*['\"]web_tour\.tours")),
    ("hoot", re.compile(r"\bfrom\s+['\"]@odoo/hoot")),
    ("qunit", re.compile(r"\bQUnit\b")),
)


def read_javascript_tests(source: bytes) -> TestFile:
    """A JS test file's framework and the literals it names.

    Read with the lexer, so strings inside comments cannot become references.
    Everything here is heuristic for the same reason the frontend layer is:
    a lexer recognises shapes, it does not parse a grammar.
    """
    result = TestFile(language="javascript", framework=None)
    try:
        tokens = read_js(source)
    except JsReadError as exc:
        result.parsed = False
        result.findings.append(
            ("warning", "test_js_undecodable", str(exc), None)
        )
        return result

    try:
        text = source.decode("utf-8-sig", errors="replace")
    except Exception:                     # pragma: no cover - defensive
        text = ""
    for name, pattern in _JS_FRAMEWORKS:
        if pattern.search(text):
            result.framework = name
            break

    seen = set()
    for token in tokens:
        if token.kind != "string":
            continue
        kind = classify_literal(token.value)
        if kind and (kind, token.value) not in seen:
            seen.add((kind, token.value))
            result.references.append(
                Reference(kind=kind, value=token.value, line=token.line)
            )
    return result


#: ``<module>/migrations/<version>/<phase>-<name>.py``
MIGRATION_PATH = re.compile(
    r"(?:^|/)migrations/(?P<version>[^/]+)/(?P<script>[^/]+\.py)$",
    re.IGNORECASE,
)
_PHASE = re.compile(r"^(pre|post|end)[-_]", re.IGNORECASE)


@dataclass
class Migration:
    version: str
    script: str
    phase: str


def read_migration_path(rel_path: str) -> Migration | None:
    """The version and phase a migration script's own path declares.

    Odoo decides when to run a script from exactly this: the directory is the
    version it upgrades to, and the filename prefix is the phase. Nothing in
    the script body is read -- a migration is code that runs against a live
    database, and this build has no business interpreting it.
    """
    match = MIGRATION_PATH.search(rel_path.replace("\\", "/"))
    if not match:
        return None
    script = match.group("script")
    phase_match = _PHASE.match(script)
    return Migration(
        version=match.group("version"),
        script=script,
        phase=phase_match.group(1).lower() if phase_match else "unknown",
    )
