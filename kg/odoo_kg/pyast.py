"""Static Python analysis of Odoo model definitions.

What this module is allowed to do is as much a part of its design as what it
extracts:

* It parses with :func:`ast.parse` and nothing else. Project code is never
  imported, never executed, and no project package ever reaches ``sys.path``.
  A repository full of models with import-time side effects is analysed exactly
  as safely as an empty one.
* It reads module bodies and class bodies. It does **not** descend into
  function bodies -- :func:`_iter_class_defs` recurses through ``ClassDef``
  only, so a class defined inside a method is invisible by construction rather
  than by a filter somebody could remove later.
* It returns names, types, line numbers and findings. No source text is kept,
  not even a snippet in a diagnostic message.

The one exception to "AST only" is deliberate and narrow. Two of the signals
the milestone asks for -- ``env.registry`` access and legacy decorators inside
methods -- live in code this module refuses to analyse semantically. Rather
than either lying about them or opening up method bodies, they are detected
**lexically** with :mod:`tokenize`, which sees real identifiers but not string
or comment contents. Findings from that pass say so in their category.

Nothing here guesses. A comodel that is not written down as a literal is
reported as such and left for the resolver in :mod:`odoo_kg.pyresolve`, which
may derive it or may give up; either way the result is recorded, never
invented.
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass, field
from typing import Iterator

#: Base classes that make a class an Odoo model, mapped to the kind recorded on
#: the node. Only the attribute form is accepted. A bare ``Model`` base would be
#: ambiguous -- plenty of non-Odoo classes are called that -- so an unqualified
#: base raises a finding instead of silently creating a model node.
MODEL_BASES: dict[str, str] = {
    "models.Model": "Model",
    "models.AbstractModel": "AbstractModel",
    "models.TransientModel": "TransientModel",
}

#: Bare base names that look like a model base but are not qualified. Present
#: so the parser can say "this might be a model and I did not index it" rather
#: than staying quiet.
AMBIGUOUS_BASES = frozenset({"Model", "AbstractModel", "TransientModel"})

#: Field constructors whose target is another model.
RELATIONAL_FIELDS = frozenset(
    {"Many2one", "One2many", "Many2many", "Many2oneReference"}
)

#: Positional signatures for the constructors whose first arguments carry
#: meaning, so an argument written positionally is read as reliably as one
#: written as a keyword. Odoo code uses both spellings freely.
_POSITIONAL = {
    "Many2one": ("comodel_name", "string"),
    "One2many": ("comodel_name", "inverse_name", "string"),
    "Many2many": ("comodel_name", "relation", "column1", "column2", "string"),
    "Many2oneReference": ("model_field", "string"),
    "Selection": ("selection", "string"),
}

#: ORM methods an override of which changes persistence behaviour. Used only to
#: classify a method node; the body is never read.
ORM_METHODS = frozenset(
    {
        "create", "write", "unlink", "copy", "read", "search", "search_read",
        "search_count", "search_fetch", "name_get", "name_search", "name_create",
        "default_get", "fields_get", "read_group", "_read_group", "_search",
        "web_search_read", "new", "browse", "onchange", "check_access",
        "check_access_rights", "check_access_rule", "init", "_auto_init",
    }
)

#: Decorators removed from Odoo long before 19.0. Their presence means the file
#: has not been migrated, and any behaviour that relies on them is already dead.
LEGACY_DECORATORS = frozenset({"api.one", "api.multi"})


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass
class Finding:
    """A parser observation. ``scope_class`` is None for module-level ones.

    ``scope_line`` is the declaring class's own line. A file may legitimately
    hold two classes of one name in different scopes, so the name alone is not
    an address; carrying the line makes attaching a finding to the right
    definition exact rather than first-match.
    """

    severity: str
    category: str
    message: str
    line: int | None = None
    scope_class: str | None = None
    scope_line: int | None = None


@dataclass
class ParsedField:
    name: str
    field_type: str
    line: int
    comodel_literal: str | None = None
    comodel_state: str = "not_relational"
    related_path: str | None = None
    compute_method: str | None = None
    inverse_name: str | None = None
    stored: bool | None = None
    required: bool | None = None
    readonly: bool | None = None
    groups_literal: str | None = None

    @property
    def is_relational(self) -> bool:
        return self.field_type in RELATIONAL_FIELDS


@dataclass
class ParsedMethod:
    name: str
    line: int
    decorators: tuple[str, ...] = ()
    depends_literal: tuple[str, ...] = ()
    args_state: str = "literal"
    kind: str = "public"

    @property
    def is_private(self) -> bool:
        return self.name.startswith("_")


@dataclass
class ParsedModelDef:
    """One ``class X(models.Model)`` body."""

    class_name: str
    line: int
    end_line: int | None
    base_kind: str
    declared_name: str | None = None
    inherit: tuple[str, ...] = ()
    delegates: tuple[tuple[str, str], ...] = ()
    fields: list[ParsedField] = field(default_factory=list)
    methods: list[ParsedMethod] = field(default_factory=list)
    parse_state: str = "complete"

    @property
    def model_name(self) -> str | None:
        """The model this class contributes to.

        ``_name`` wins. Without it, a class is an extension and its identity is
        its first ``_inherit`` target. With neither, the class has no model
        identity at all and is reported rather than guessed at.
        """
        if self.declared_name:
            return self.declared_name
        if self.inherit:
            return self.inherit[0]
        return None

    @property
    def shape(self) -> str:
        if self.declared_name and self.inherit:
            # Odoo's prototype inheritance: copy another model's definition into
            # a new model. Very different from extending one in place, and the
            # distinction decides whether edits here affect the parent.
            return (
                "inherit_same_name"
                if self.declared_name in self.inherit
                else "prototype"
            )
        if self.declared_name:
            return "definition"
        if self.inherit:
            return "extension"
        return "unknown"


@dataclass
class ParsedPythonFile:
    defs: list[ParsedModelDef] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    parsed: bool = True

    @property
    def has_models(self) -> bool:
        return bool(self.defs)


# --------------------------------------------------------------------------
# small AST helpers
# --------------------------------------------------------------------------


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _const_bool(node: ast.AST | None) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _dotted(node: ast.AST | None) -> str | None:
    """``models.Model`` -> 'models.Model'; ``Model`` -> 'Model'."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _positional(call: ast.Call, ctor: str, name: str) -> ast.AST | None:
    """Read a constructor argument written positionally."""
    order = _POSITIONAL.get(ctor)
    if not order or name not in order:
        return None
    index = order.index(name)
    return call.args[index] if len(call.args) > index else None


def _argument(call: ast.Call, ctor: str, name: str) -> ast.AST | None:
    """A constructor argument, whether written positionally or by keyword."""
    node = _keyword(call, name)
    return node if node is not None else _positional(call, ctor, name)


def _field_ctor(node: ast.AST) -> str | None:
    """``fields.Char(...)`` -> 'Char'. Anything else -> None."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "fields"
    ):
        return func.attr
    return None


def _iter_class_defs(
    body: list[ast.stmt], scope: str = ""
) -> Iterator[tuple[str, ast.ClassDef]]:
    """Yield ``(scope, class)`` pairs, recursing into classes only.

    Never descends into ``FunctionDef``/``AsyncFunctionDef``. That is what
    enforces "method bodies are not analysed": a class declared inside a method
    is not reachable from here at all.

    ``scope`` is the dotted path of enclosing classes, empty at module level.
    Two classes shadow each other only inside the same scope, so the caller
    needs it to tell a redeclaration from a namesake.
    """
    for node in body:
        if isinstance(node, ast.ClassDef):
            yield scope, node
            child = f"{scope}.{node.name}" if scope else node.name
            yield from _iter_class_defs(node.body, child)


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[
    tuple[str, ...], tuple[str, ...], str
]:
    """Return (decorator names, literal api.depends args, args_state)."""
    names: list[str] = []
    depends: list[str] = []
    args_state = "literal"
    for deco in node.decorator_list:
        call = deco if isinstance(deco, ast.Call) else None
        name = _dotted(call.func if call else deco)
        if name is None:
            # A decorator built by an expression, e.g. a factory call result.
            # It exists, but this build cannot name it.
            args_state = "unresolved"
            continue
        names.append(name)
        if call is None:
            continue
        if name in ("api.depends", "api.depends_context", "api.constrains", "api.onchange"):
            for arg in call.args:
                literal = _const_str(arg)
                if literal is None:
                    args_state = "unresolved"
                elif name == "api.depends":
                    depends.append(literal)
            if call.keywords:
                args_state = "unresolved"
    return tuple(names), tuple(depends), args_state


def _classify_method(name: str, decorators: tuple[str, ...]) -> str:
    """Bucket a method by what Odoo will do with it.

    Decorators are checked before names because they are declarations, whereas
    a name is a convention. ``create`` decorated with ``api.model_create_multi``
    is still an ORM override, so ORM membership is checked first of all.
    """
    if name in ORM_METHODS:
        return "orm_override"
    for deco in decorators:
        if deco == "api.depends":
            return "compute"
        if deco == "api.constrains":
            return "constraint"
        if deco == "api.onchange":
            return "onchange"
        if deco.startswith("api.model_create"):
            return "orm_override"
        if deco == "api.ondelete":
            return "ondelete"
    if name.startswith(("action_", "button_")):
        return "action"
    if name.startswith("_compute_"):
        return "compute"
    if name.startswith("_inverse_"):
        return "inverse"
    if name.startswith("_search_"):
        return "search"
    if name.startswith("_onchange_"):
        return "onchange"
    if "cron" in name:
        return "cron_like"
    return "private" if name.startswith("_") else "public"


# --------------------------------------------------------------------------
# field and class parsing
# --------------------------------------------------------------------------


def _parse_field(name: str, call: ast.Call, ctor: str) -> tuple[ParsedField, list[Finding]]:
    findings: list[Finding] = []
    parsed = ParsedField(name=name, field_type=ctor, line=call.lineno)

    related = _keyword(call, "related")
    parsed.related_path = _const_str(related)
    if related is not None and parsed.related_path is None:
        findings.append(
            Finding(
                "info",
                "field_related_not_literal",
                f"{name}: related= is not a literal string; the target cannot be followed",
                call.lineno,
            )
        )

    parsed.compute_method = _const_str(_keyword(call, "compute"))
    parsed.stored = _const_bool(_keyword(call, "store"))
    parsed.required = _const_bool(_keyword(call, "required"))
    parsed.readonly = _const_bool(_keyword(call, "readonly"))
    parsed.groups_literal = _const_str(_keyword(call, "groups"))

    if ctor not in RELATIONAL_FIELDS:
        selection = _argument(call, ctor, "selection") or _keyword(call, "selection")
        if selection is not None and not isinstance(selection, (ast.List, ast.Tuple)):
            findings.append(
                Finding(
                    "info",
                    "field_selection_not_literal",
                    f"{name}: selection is computed or named indirectly; "
                    "its values are not statically known",
                    call.lineno,
                )
            )
        return parsed, findings

    comodel_node = _argument(call, ctor, "comodel_name")
    parsed.comodel_literal = _const_str(comodel_node)
    if parsed.comodel_literal is not None:
        parsed.comodel_state = "literal"
    elif comodel_node is not None:
        # An expression was supplied. It exists, but its value is a runtime
        # matter; nothing here will pretend to know it.
        parsed.comodel_state = "dynamic"
        findings.append(
            Finding(
                "warning",
                "field_comodel_not_literal",
                f"{name}: comodel_name is an expression, not a literal",
                call.lineno,
            )
        )
    elif parsed.related_path is not None:
        # The ordinary Odoo idiom: a related field inherits its comodel from
        # the field at the end of the path. Deferred to the second pass.
        parsed.comodel_state = "related"
    else:
        parsed.comodel_state = "absent"
        findings.append(
            Finding(
                "warning",
                "field_comodel_absent",
                f"{name}: relational field declares no comodel and no related path",
                call.lineno,
            )
        )

    if ctor == "One2many":
        parsed.inverse_name = _const_str(_argument(call, ctor, "inverse_name"))
        if parsed.inverse_name is None and parsed.related_path is None:
            findings.append(
                Finding(
                    "info",
                    "one2many_without_inverse",
                    f"{name}: One2many has no literal inverse_name",
                    call.lineno,
                )
            )
    return parsed, findings


def _parse_class(node: ast.ClassDef) -> tuple[ParsedModelDef | None, list[Finding]]:
    findings: list[Finding] = []
    bases = [_dotted(base) for base in node.bases]
    kind = next((MODEL_BASES[b] for b in bases if b in MODEL_BASES), None)

    if kind is None:
        if any(b in AMBIGUOUS_BASES for b in bases if b):
            findings.append(
                Finding(
                    "warning",
                    "model_base_unqualified",
                    f"{node.name} inherits an unqualified model base "
                    f"({', '.join(b for b in bases if b)}); not indexed as a model",
                    node.lineno,
                    node.name,
                )
            )
        return None, findings

    parsed = ParsedModelDef(
        class_name=node.name,
        line=node.lineno,
        end_line=getattr(node, "end_lineno", None),
        base_kind=kind,
    )
    inherit: list[str] = []
    delegates: list[tuple[str, str]] = []
    #: member name -> its index in the corresponding list, so a redeclaration
    #: replaces the earlier entry instead of creating a second member that
    #: cannot exist. Python has already resolved the conflict; the graph must
    #: describe the class that results, not the text that produced it.
    field_index: dict[str, int] = {}
    method_index: dict[str, int] = {}

    for stmt in node.body:
        if isinstance(stmt, (ast.If, ast.For, ast.While, ast.Try, ast.With)):
            # A conditional class body means what the class contains depends on
            # runtime state. Whatever is extracted is a subset, and saying so is
            # more useful than presenting it as the whole picture.
            parsed.parse_state = "partial_parse"
            findings.append(
                Finding(
                    "warning",
                    "class_body_control_flow",
                    f"{node.name}: {type(stmt).__name__} in the class body; "
                    "the extracted members may be incomplete",
                    stmt.lineno,
                    node.name,
                )
            )
            continue

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names, depends, args_state = _decorator_names(stmt)
            method = ParsedMethod(
                name=stmt.name,
                line=stmt.lineno,
                decorators=names,
                depends_literal=depends,
                args_state=args_state,
                kind=_classify_method(stmt.name, names),
            )
            previous_method = method_index.get(stmt.name)
            if previous_method is None:
                method_index[stmt.name] = len(parsed.methods)
                parsed.methods.append(method)
            else:
                # Same rule as a redeclared field: the class ends up with one
                # method, so the graph records one. Reporting it matters more
                # here than for a field, because the dead definition may be the
                # one carrying the @api.depends everyone assumes is active.
                findings.append(
                    Finding(
                        "warning",
                        "method_declared_twice",
                        f"{node.name}.{stmt.name}: declared more than once in one "
                        f"class body; the definition at line "
                        f"{parsed.methods[previous_method].line} is dead code",
                        stmt.lineno,
                        node.name,
                    )
                )
                parsed.methods[previous_method] = method
            for deco in names:
                if deco in LEGACY_DECORATORS:
                    findings.append(
                        Finding(
                            "warning",
                            "legacy_decorator",
                            f"{node.name}.{stmt.name}: @{deco} was removed from "
                            "Odoo long before 19.0 and no longer has any effect",
                            stmt.lineno,
                            node.name,
                        )
                    )
            if args_state == "unresolved":
                findings.append(
                    Finding(
                        "info",
                        "decorator_args_not_literal",
                        f"{node.name}.{stmt.name}: decorator arguments are not "
                        "all literals; dependencies are recorded as unresolved",
                        stmt.lineno,
                        node.name,
                    )
                )
            continue

        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            continue

        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        value = stmt.value
        if value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            attr = target.id

            if attr == "_name":
                literal = _const_str(value)
                if literal:
                    if parsed.declared_name is not None:
                        # Python keeps the last assignment, so the model this
                        # class defines is the last name -- not the first, and
                        # not both. A reader scanning the file would likely
                        # believe the first.
                        findings.append(
                            Finding(
                                "warning",
                                "model_name_declared_twice",
                                f"{node.name}: _name assigned more than once; "
                                f"{parsed.declared_name!r} is overwritten by "
                                f"{literal!r}, which is the model that exists",
                                stmt.lineno,
                                node.name,
                                node.lineno,
                            )
                        )
                    parsed.declared_name = literal
                else:
                    parsed.parse_state = "incomplete"
                    findings.append(
                        Finding(
                            "warning",
                            "model_name_not_literal",
                            f"{node.name}: _name is not a literal string; the "
                            "model has no statically knowable identity",
                            stmt.lineno,
                            node.name,
                        )
                    )
            elif attr == "_inherit":
                values, dynamic = _string_sequence(value)
                inherit.extend(values)
                if dynamic:
                    findings.append(
                        Finding(
                            "warning",
                            "model_inherit_not_literal",
                            f"{node.name}: _inherit contains a non-literal entry",
                            stmt.lineno,
                            node.name,
                        )
                    )
            elif attr == "_inherits":
                pairs, dynamic = _delegation_map(value)
                delegates.extend(pairs)
                if dynamic:
                    findings.append(
                        Finding(
                            "warning",
                            "model_inherits_not_literal",
                            f"{node.name}: _inherits has a non-literal key or value",
                            stmt.lineno,
                            node.name,
                        )
                    )
            elif attr == "_sql_constraints":
                findings.append(
                    Finding(
                        "warning",
                        "legacy_sql_constraints",
                        f"{node.name}: _sql_constraints is the pre-19.0 form; "
                        "Odoo 19 declares constraints with models.Constraint",
                        stmt.lineno,
                        node.name,
                    )
                )
            else:
                ctor = _field_ctor(value)
                if ctor:
                    parsed_field, field_findings = _parse_field(attr, value, ctor)
                    for finding in field_findings:
                        finding.scope_class = node.name
                    findings.extend(field_findings)
                    previous = field_index.get(attr)
                    if previous is None:
                        field_index[attr] = len(parsed.fields)
                        parsed.fields.append(parsed_field)
                    else:
                        # Python has already decided this: the second assignment
                        # wins and the first never becomes a field. The graph
                        # records one field, as the model will have one -- but
                        # silently dropping the loser would hide a real defect.
                        findings.append(
                            Finding(
                                "warning",
                                "field_declared_twice",
                                f"{node.name}.{attr}: declared more than once in "
                                f"one class body; the definition at line "
                                f"{parsed.fields[previous].line} is dead code",
                                value.lineno,
                                node.name,
                            )
                        )
                        parsed.fields[previous] = parsed_field

    parsed.inherit = tuple(inherit)
    parsed.delegates = tuple(delegates)

    if parsed.model_name is None:
        findings.append(
            Finding(
                "warning",
                "model_without_identity",
                f"{node.name}: neither _name nor _inherit; no model identity",
                node.lineno,
                node.name,
            )
        )

    # Every finding raised while reading this class belongs to this class.
    # Stamping the address once here beats remembering it at twelve call sites.
    for finding in findings:
        finding.scope_class = node.name
        finding.scope_line = node.lineno
    return parsed, findings


def _string_sequence(node: ast.AST) -> tuple[list[str], bool]:
    """Literal strings from a string or a list/tuple of strings."""
    single = _const_str(node)
    if single is not None:
        return [single], False
    if isinstance(node, (ast.List, ast.Tuple)):
        out: list[str] = []
        dynamic = False
        for element in node.elts:
            literal = _const_str(element)
            if literal is None:
                dynamic = True
            else:
                out.append(literal)
        return out, dynamic
    return [], True


def _delegation_map(node: ast.AST) -> tuple[list[tuple[str, str]], bool]:
    """``_inherits`` as (delegated model, foreign key field) pairs.

    testing19 currently has zero of these, which is exactly why the support is
    written and tested now rather than when the first one appears: core models
    use delegation heavily, and the code that reads them must already handle it.
    """
    if not isinstance(node, ast.Dict):
        return [], True
    pairs: list[tuple[str, str]] = []
    dynamic = False
    for key, value in zip(node.keys, node.values):
        model = _const_str(key)
        column = _const_str(value)
        if model is None or column is None:
            dynamic = True
            continue
        pairs.append((model, column))
    return pairs, dynamic


# --------------------------------------------------------------------------
# lexical pass
# --------------------------------------------------------------------------


def _lexical_findings(source: bytes) -> list[Finding]:
    """Signals that live inside method bodies, found without parsing them.

    :mod:`tokenize` gives real identifiers, so a mention inside a docstring,
    a comment or a string literal does not count -- which a plain substring
    search would get wrong. The findings are informational and are labelled as
    lexical so nobody mistakes them for structural facts.
    """
    findings: list[Finding] = []
    seen: set[str] = set()
    previous_op = ""
    try:
        tokens = tokenize.tokenize(io.BytesIO(source).readline)
        for token in tokens:
            if token.type == tokenize.OP:
                previous_op = token.string
                continue
            if token.type != tokenize.NAME:
                previous_op = ""
                continue
            if token.string == "registry" and previous_op == ".":
                if "registry" not in seen:
                    seen.add("registry")
                    findings.append(
                        Finding(
                            "info",
                            "lexical_registry_access",
                            "the file reaches env.registry; behaviour here can "
                            "be decided at runtime and is not in the graph",
                            token.start[0],
                        )
                    )
            previous_op = ""
    except (tokenize.TokenError, SyntaxError, UnicodeDecodeError):
        # The AST pass reports a real syntax error. This pass simply stops.
        return findings
    return findings


def _module_level_setattr(tree: ast.Module, model_classes: set[str]) -> list[Finding]:
    """``setattr(SomeModel, ...)`` written at module level.

    A class mutated after its body has run is not fully described by its body,
    so the definition is marked incomplete. Note the deliberate limit: a
    ``setattr`` inside a method is invisible to this build, because method
    bodies are not analysed. testing19's two ``setattr`` calls are both of that
    kind and both operate on ``self``, not on a class.
    """
    findings: list[Finding] = []
    for stmt in tree.body:
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            continue
        call = stmt.value
        if not (isinstance(call.func, ast.Name) and call.func.id == "setattr"):
            continue
        target = call.args[0] if call.args else None
        name = _dotted(target) if target is not None else None
        if name and name in model_classes:
            findings.append(
                Finding(
                    "warning",
                    "model_class_mutated",
                    f"{name}: mutated by a module-level setattr; the class body "
                    "is not the whole definition",
                    stmt.lineno,
                    name,
                )
            )
    return findings


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def parse_python_source(source: bytes) -> ParsedPythonFile:
    """Parse one Python file's model structure. Never imports or executes it."""
    result = ParsedPythonFile()
    try:
        # ast.parse on bytes honours the file's own coding declaration, so no
        # decoding guess is made here.
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        result.parsed = False
        result.findings.append(
            Finding(
                "error",
                "python_syntax_error",
                f"could not parse: {type(exc).__name__}: {exc.args[0] if exc.args else exc}",
                getattr(exc, "lineno", None),
            )
        )
        return result

    #: (scope, class name) -> index in result.defs, so a class redeclared in the
    #: same scope replaces the earlier one. Python binds the name to the last
    #: definition; the earlier class object is unreachable and never becomes an
    #: Odoo model. Keeping both would report a model the registry will not have.
    seen: dict[tuple[str, str], int] = {}
    for scope, class_node in _iter_class_defs(tree.body):
        parsed, findings = _parse_class(class_node)
        result.findings.extend(findings)
        if parsed is None:
            continue
        key = (scope, parsed.class_name)
        previous = seen.get(key)
        if previous is None:
            seen[key] = len(result.defs)
            result.defs.append(parsed)
            continue
        superseded = result.defs[previous]
        result.defs[previous] = parsed
        where = "module level" if not scope else f"class {scope}"
        result.findings.append(
            Finding(
                "warning",
                "class_declared_twice",
                f"{parsed.class_name}: declared more than once at {where}; the "
                f"definition at line {superseded.line} is shadowed and never "
                "becomes a model",
                parsed.line,
                parsed.class_name,
                # Addressed to the surviving definition, which is the one that
                # exists in the graph and the one a reader must go and look at.
                parsed.line,
            )
        )
        # Findings already collected for the shadowed class would otherwise
        # point at a definition that is not in the graph. Re-address them to
        # the survivor rather than dropping evidence on the floor.
        for finding in result.findings:
            if (
                finding.scope_class == parsed.class_name
                and finding.scope_line == superseded.line
            ):
                finding.scope_line = parsed.line

    if result.defs:
        model_classes = {d.class_name for d in result.defs}
        setattr_findings = _module_level_setattr(tree, model_classes)
        for finding in setattr_findings:
            for parsed in result.defs:
                if parsed.class_name == finding.scope_class:
                    parsed.parse_state = "incomplete"
        result.findings.extend(setattr_findings)
        result.findings.extend(_lexical_findings(source))

    return result
