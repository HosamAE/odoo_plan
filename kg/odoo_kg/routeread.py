"""HTTP controllers and routes, read from Python by AST alone.

The same discipline as :mod:`odoo_kg.pyast`: :func:`ast.parse` and nothing
else. Project code is never imported and never executed, and method bodies are
not analysed -- a route is a *declaration* on a method, and the declaration is
entirely in the decorator.

What makes routes worth their own reader rather than a corner of the model
reader: a route is the boundary where an HTTP request enters the system, so its
``auth`` and ``type`` are the most security-relevant literals in an Odoo
codebase. Recording them exactly as declared, with a file and line, is the
point. Deciding whether a route is *safe* is not: that depends on what the
method does, which this milestone does not read.

testing19 declares its controllers on two bases -- ``http.Controller`` and a
project base called ``HxController``. Recognising only the first would miss
thirteen of twenty-five controllers, so the rule is name-shaped rather than
exact, and the base is recorded so a reader can see which was matched.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

#: A class is a controller when a base's name ends with this. Odoo's own base
#: is ``http.Controller``; projects routinely subclass it once and inherit from
#: that. Both are recorded, with ``base`` saying which was seen.
CONTROLLER_SUFFIX = "Controller"

#: Decorators that declare a route.
ROUTE_DECORATORS = frozenset({"http.route", "route"})

#: Keyword arguments worth keeping. Everything else is counted, not stored:
#: an unknown keyword is a signal that Odoo grew one, not a fact about access.
ROUTE_KEYWORDS = ("auth", "type", "methods", "website", "csrf", "sitemap", "cors",
                  "save_session", "readonly")


@dataclass
class ParsedRoute:
    """One ``@http.route`` declaration on one controller method."""

    paths: tuple[str, ...]
    controller: str
    method: str
    line: int
    decorator_line: int
    base: str
    auth: str | None = None
    route_type: str | None = None
    methods: tuple[str, ...] = ()
    website: bool | None = None
    csrf: bool | None = None
    sitemap: str | None = None
    #: True when a path or a keyword was an expression this reader will not
    #: evaluate. The route is still recorded; the unknown part is not invented.
    has_dynamic_argument: bool = False
    unknown_keywords: tuple[str, ...] = ()


@dataclass
class ParsedControllers:
    routes: list[ParsedRoute] = field(default_factory=list)
    controllers: list[tuple[str, str, int]] = field(default_factory=list)
    findings: list[tuple[str, str, str, int | None]] = field(default_factory=list)
    parsed: bool = True


def parse_controllers(source: bytes) -> ParsedControllers:
    """Read every controller class and route declaration in one file."""
    result = ParsedControllers()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        result.parsed = False
        result.findings.append((
            "error", "controller_syntax_error",
            f"could not parse: {type(exc).__name__}: "
            f"{exc.args[0] if exc.args else exc}",
            getattr(exc, "lineno", None),
        ))
        return result

    for node in _iter_class_defs(tree.body):
        bases = [_dotted(base) for base in node.bases]
        matched = next(
            (b for b in bases if b and b.split(".")[-1].endswith(CONTROLLER_SUFFIX)),
            None,
        )
        if matched is None:
            continue
        result.controllers.append((node.name, matched, node.lineno))
        for statement in node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in statement.decorator_list:
                parsed = _read_route(decorator, node.name, statement, matched, result)
                if parsed is not None:
                    result.routes.append(parsed)
    return result


def _read_route(decorator, controller, statement, base, result) -> ParsedRoute | None:
    call = decorator if isinstance(decorator, ast.Call) else None
    name = _dotted(call.func if call else decorator)
    if name not in ROUTE_DECORATORS:
        return None

    if call is None:
        # `@http.route` without parentheses: legal, and it routes on the
        # method name. The declaration exists; its path does not.
        result.findings.append((
            "info", "route_without_arguments",
            f"{controller}.{statement.name}: @route with no arguments; the path "
            "is derived at runtime and is not statically known",
            statement.lineno,
        ))
        return ParsedRoute(
            paths=(), controller=controller, method=statement.name,
            line=statement.lineno, decorator_line=decorator.lineno, base=base,
            has_dynamic_argument=True,
        )

    paths: list[str] = []
    dynamic = False
    for argument in call.args:
        literal = _const_str(argument)
        if literal is not None:
            paths.append(literal)
            continue
        if isinstance(argument, (ast.List, ast.Tuple)):
            for element in argument.elts:
                value = _const_str(element)
                if value is None:
                    dynamic = True
                else:
                    paths.append(value)
            continue
        dynamic = True

    route = ParsedRoute(
        paths=tuple(paths), controller=controller, method=statement.name,
        line=statement.lineno, decorator_line=decorator.lineno, base=base,
    )
    unknown: list[str] = []
    for keyword in call.keywords:
        if keyword.arg is None:
            # `**kwargs` in the decorator: the real keywords are computed.
            dynamic = True
            unknown.append("**")
            continue
        if keyword.arg not in ROUTE_KEYWORDS:
            unknown.append(keyword.arg)
            continue
        value = keyword.value
        if keyword.arg == "auth":
            route.auth = _const_str(value)
            dynamic = dynamic or route.auth is None
        elif keyword.arg == "type":
            route.route_type = _const_str(value)
        elif keyword.arg == "methods":
            if isinstance(value, (ast.List, ast.Tuple)):
                literals = [_const_str(e) for e in value.elts]
                route.methods = tuple(m for m in literals if m)
                dynamic = dynamic or any(m is None for m in literals)
            else:
                dynamic = True
        elif keyword.arg == "website":
            route.website = _const_bool(value)
        elif keyword.arg == "csrf":
            route.csrf = _const_bool(value)
        elif keyword.arg == "sitemap":
            route.sitemap = _const_str(value) or (
                "True" if _const_bool(value) is True
                else "False" if _const_bool(value) is False else None
            )

    route.has_dynamic_argument = dynamic
    route.unknown_keywords = tuple(sorted(set(unknown)))

    if not paths and not route.has_dynamic_argument:
        result.findings.append((
            "warning", "route_without_path",
            f"{controller}.{statement.name}: @route declares no path",
            statement.lineno,
        ))
    if dynamic:
        result.findings.append((
            "info", "route_argument_not_literal",
            f"{controller}.{statement.name}: part of the route declaration is an "
            "expression; it is recorded as unknown rather than evaluated",
            decorator.lineno,
        ))
    if route.auth is None and not dynamic:
        # Odoo defaults to auth='user'. The default is not written down here,
        # so the graph says "not declared" rather than asserting the default.
        result.findings.append((
            "info", "route_auth_not_declared",
            f"{controller}.{statement.name}: no auth= is declared; Odoo's default "
            "applies at runtime and this graph does not assume it",
            decorator.lineno,
        ))
    return route


def _iter_class_defs(body):
    """Classes at module level and inside classes. Never inside a function."""
    for node in body:
        if isinstance(node, ast.ClassDef):
            yield node
            yield from _iter_class_defs(node.body)


def _dotted(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _const_str(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _const_bool(node) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None
