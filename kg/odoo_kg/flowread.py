"""Reading workflow evidence out of Python method bodies, with ast only.

This is the first reader in the project that looks *inside* a method. Every
earlier layer stopped at the signature, and stopping there was the right call
while nothing needed the body. A workflow does need it: a state transition is
written as an assignment or a ``write``, and both live in the body.

What it will and will not say
-----------------------------
The module never imports the code it reads, never evaluates an expression, and
never calls ``eval``, ``safe_eval`` or a domain parser. It reads a syntax tree
and reports literals.

Three kinds of claim come out of it, and they are kept apart on purpose:

``static``
    An assignment or a ``write``/``create`` whose key and value are both string
    literals in the tree. ``self.state = 'done'`` is this. It is as certain as
    reading the line yourself -- but it says the code *can* run, never that it
    *did*.

``heuristic``
    A source state guessed from a guard: ``if self.state == 'draft'`` near a
    transition suggests the transition starts at ``draft``. Suggestive, not
    proof: the guard may sit in a branch that has nothing to do with the write,
    and a method may guard on one thing and change another.

nothing at all
    A value that is not a literal. ``self.state = target`` is recorded as a
    dynamic transition with no target, not as a transition to ``"target"``.
    Guessing here would put a state into the graph that no Selection declares.

Conditionality is preserved rather than flattened. A write inside an ``if``,
a loop, a ``try`` or after a ``raise`` guard is marked conditional and carries
the source text of the condition unevaluated, because "this method sets state
to done" and "this method sets state to done when the invoice is paid" are
different claims and only the second one is true.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field as dc_field

#: Field names that carry a workflow state. Deliberately short: a graph that
#: called every Selection field a workflow would drown the real ones.
STATE_FIELD_NAMES = frozenset({"state", "status", "kanban_state"})

#: Suffixes that also mark a state field, e.g. ``bank_state``, ``sync_status``.
STATE_FIELD_SUFFIXES = ("_state", "_status")

#: Stage fields hold a Many2one to a stage model. They are workflows, but their
#: states are data records rather than code, so no state is ever read for them.
STAGE_FIELD_NAMES = frozenset({"stage_id", "stage"})

#: Statements whose presence makes everything inside them conditional.
_BRANCHING = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try,
              ast.With, ast.AsyncWith, ast.IfExp)


def is_state_field(name: str) -> bool:
    return name in STATE_FIELD_NAMES or name.endswith(STATE_FIELD_SUFFIXES)


def is_stage_field(name: str) -> bool:
    return name in STAGE_FIELD_NAMES


@dataclass
class StateWrite:
    """One place a method sets a workflow field to a value."""

    field_name: str
    #: None when the value is not a literal. The expression is kept instead.
    value: str | None
    operation: str            # assign | write | create
    receiver: str             # self, record, self.env['x'], ...
    target_model: str | None  # literal model when the receiver names one
    conditional: bool
    condition: str | None     # source text, never evaluated
    line: int
    dynamic_expression: str | None = None


@dataclass
class StateGuard:
    """A comparison of a workflow field against literals, in a method body."""

    field_name: str
    operator: str             # eq | ne | in | not_in
    values: tuple[str, ...]
    line: int
    #: True when part of the comparison was not a literal, so the guard is
    #: only partly known and must not be read as the whole condition.
    partial: bool = False


@dataclass
class CrossModelOp:
    """A create or write against a literally-named other model."""

    target_model: str
    operation: str            # create | write
    conditional: bool
    condition: str | None
    line: int
    sets_state: str | None = None


@dataclass
class MethodFlow:
    method_name: str
    line: int
    end_line: int
    writes: list[StateWrite] = dc_field(default_factory=list)
    guards: list[StateGuard] = dc_field(default_factory=list)
    cross_model: list[CrossModelOp] = dc_field(default_factory=list)
    #: Raised exceptions make a method a gate rather than a straight path.
    raises: int = 0
    branches: int = 0


@dataclass
class ClassFlow:
    model_name: str | None
    class_name: str
    line: int
    #: field name -> (values, labels, line, dynamic)
    selections: dict = dc_field(default_factory=dict)
    stage_fields: dict = dc_field(default_factory=dict)
    methods: list[MethodFlow] = dc_field(default_factory=list)


@dataclass
class FileFlow:
    classes: list[ClassFlow] = dc_field(default_factory=list)
    parsed: bool = True
    findings: list[tuple[str, str, str, int | None]] = dc_field(default_factory=list)


class FlowReadError(Exception):
    """The file could not be parsed. Malformed code costs only that file."""


def _literal(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dotted(node) -> str | None:
    """``self.env.user`` -> "self.env.user"; anything else -> None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _receiver_text(node) -> str:
    """A readable name for whatever a call was made on."""
    dotted = _dotted(node)
    if dotted:
        return dotted
    if isinstance(node, ast.Subscript):
        base = _dotted(node.value) or "?"
        key = _literal(node.slice)
        return f"{base}[{key!r}]" if key else f"{base}[...]"
    if isinstance(node, ast.Call):
        return (_dotted(node.func) or "?") + "(...)"
    return "?"


def _env_model(node) -> str | None:
    """``self.env['sale.order']`` -> "sale.order"; dynamic -> None."""
    if not isinstance(node, ast.Subscript):
        return None
    base = node.value
    if isinstance(base, ast.Attribute) and base.attr == "env":
        return _literal(node.slice)
    return None


def _receiver_model(node) -> str | None:
    """The literal model a receiver names, looking through .search/.browse etc."""
    seen = 0
    while seen < 8:
        seen += 1
        model = _env_model(node)
        if model:
            return model
        if isinstance(node, ast.Call):
            node = node.func
            continue
        if isinstance(node, ast.Attribute):
            node = node.value
            continue
        return None
    return None


def _unparse(node) -> str:
    try:
        text = ast.unparse(node)
    except Exception:            # pragma: no cover - defensive
        return "<unreadable expression>"
    return text if len(text) <= 240 else text[:237] + "..."


def _selection_values(call: ast.Call):
    """The literal (value, label) pairs of a Selection field, plus a dynamic flag."""
    argument = call.args[0] if call.args else None
    for keyword in call.keywords:
        if keyword.arg == "selection":
            argument = keyword.value
    if argument is None:
        return [], True
    if not isinstance(argument, (ast.List, ast.Tuple)):
        # A method name or a callable: the values are produced at runtime.
        return [], True

    pairs = []
    dynamic = False
    for element in argument.elts:
        if isinstance(element, (ast.Tuple, ast.List)) and element.elts:
            value = _literal(element.elts[0])
            label = _literal(element.elts[1]) if len(element.elts) > 1 else None
            if value is None:
                dynamic = True
            else:
                pairs.append((value, label))
        else:
            dynamic = True
    return pairs, dynamic


def _field_call(node) -> tuple[str, ast.Call] | None:
    """``x = fields.Selection(...)`` -> ("Selection", call)."""
    if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
        return None
    target = node.targets[0]
    if not isinstance(target, ast.Name):
        return None
    call = node.value
    if not isinstance(call, ast.Call):
        return None
    dotted = _dotted(call.func)
    if not dotted or not dotted.startswith("fields."):
        return None
    return dotted.split(".", 1)[1], call


def _condition_stack(ancestors) -> tuple[bool, str | None]:
    """Whether a node sits under a branch, and the nearest condition's text."""
    condition = None
    conditional = False
    for parent in reversed(ancestors):
        if isinstance(parent, _BRANCHING):
            conditional = True
            test = getattr(parent, "test", None)
            if test is not None and condition is None:
                condition = _unparse(test)
            elif condition is None:
                condition = type(parent).__name__.lower()
    return conditional, condition


def _walk_with_parents(root):
    """Yield (node, ancestors) so a statement knows what encloses it."""
    stack = [(root, [])]
    while stack:
        node, ancestors = stack.pop()
        yield node, ancestors
        for child in ast.iter_child_nodes(node):
            stack.append((child, ancestors + [node]))


def _read_method(function: ast.AST) -> MethodFlow:
    flow = MethodFlow(
        method_name=function.name,
        line=function.lineno,
        end_line=getattr(function, "end_lineno", function.lineno),
    )

    for node, ancestors in _walk_with_parents(function):
        if isinstance(node, ast.Raise):
            flow.raises += 1
        if isinstance(node, _BRANCHING) and node is not function:
            flow.branches += 1

        # -- self.state = 'done' -------------------------------------
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not (isinstance(target, ast.Attribute)
                        and is_state_field(target.attr)):
                    continue
                conditional, condition = _condition_stack(ancestors)
                value = _literal(node.value)
                flow.writes.append(StateWrite(
                    field_name=target.attr,
                    value=value,
                    operation="assign",
                    receiver=_receiver_text(target.value),
                    target_model=_receiver_model(target.value),
                    conditional=conditional,
                    condition=condition,
                    line=node.lineno,
                    dynamic_expression=None if value is not None
                    else _unparse(node.value),
                ))

        # -- x.write({'state': 'done'}) / create({...}) ----------------
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method = node.func.attr
            if method in ("write", "create") and node.args:
                conditional, condition = _condition_stack(ancestors)
                receiver = _receiver_text(node.func.value)
                target_model = _receiver_model(node.func.value)
                payloads = []
                argument = node.args[0]
                if isinstance(argument, ast.Dict):
                    payloads = [argument]
                elif isinstance(argument, (ast.List, ast.Tuple)):
                    payloads = [e for e in argument.elts if isinstance(e, ast.Dict)]

                touched_state = None
                for payload in payloads:
                    for key_node, value_node in zip(payload.keys, payload.values):
                        key = _literal(key_node)
                        if key is None or not is_state_field(key):
                            continue
                        value = _literal(value_node)
                        touched_state = touched_state or value
                        flow.writes.append(StateWrite(
                            field_name=key,
                            value=value,
                            operation=method,
                            receiver=receiver,
                            target_model=target_model,
                            conditional=conditional,
                            condition=condition,
                            line=node.lineno,
                            dynamic_expression=None if value is not None
                            else _unparse(value_node),
                        ))
                if target_model:
                    flow.cross_model.append(CrossModelOp(
                        target_model=target_model,
                        operation=method,
                        conditional=conditional,
                        condition=condition,
                        line=node.lineno,
                        sets_state=touched_state,
                    ))

        # -- if self.state == 'draft' ---------------------------------
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute) \
                and is_state_field(node.left.attr):
            operator = {
                ast.Eq: "eq", ast.NotEq: "ne", ast.In: "in", ast.NotIn: "not_in",
            }.get(type(node.ops[0]))
            if operator is not None:
                values = []
                partial = False
                for comparator in node.comparators:
                    single = _literal(comparator)
                    if single is not None:
                        values.append(single)
                        continue
                    if isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                        for element in comparator.elts:
                            found = _literal(element)
                            if found is None:
                                partial = True
                            else:
                                values.append(found)
                        continue
                    partial = True
                if values:
                    flow.guards.append(StateGuard(
                        field_name=node.left.attr,
                        operator=operator,
                        values=tuple(values),
                        line=node.lineno,
                        partial=partial,
                    ))
    return flow


def read_flows(source: bytes) -> FileFlow:
    """Read every model class in one file for workflow evidence."""
    result = FileFlow()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        result.parsed = False
        result.findings.append((
            "error", "flow_syntax_error",
            f"could not parse: {type(exc).__name__}: "
            f"{exc.args[0] if exc.args else exc}",
            getattr(exc, "lineno", None),
        ))
        return result

    for node in _iter_class_defs(tree.body):
        model_name = None
        for statement in node.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
                if isinstance(target, ast.Name) and target.id in ("_name", "_inherit"):
                    model_name = _literal(statement.value) or model_name
        if model_name is None:
            continue        # not an Odoo model: nothing here is a workflow

        klass = ClassFlow(
            model_name=model_name, class_name=node.name, line=node.lineno
        )
        for statement in node.body:
            found = _field_call(statement)
            if found is not None:
                field_type, call = found
                name = statement.targets[0].id
                if field_type == "Selection" and is_state_field(name):
                    pairs, dynamic = _selection_values(call)
                    klass.selections[name] = {
                        "pairs": pairs,
                        "dynamic": dynamic,
                        "line": statement.lineno,
                    }
                elif field_type == "Many2one" and is_stage_field(name):
                    comodel = _literal(call.args[0]) if call.args else None
                    for keyword in call.keywords:
                        if keyword.arg == "comodel_name":
                            comodel = _literal(keyword.value) or comodel
                    klass.stage_fields[name] = {
                        "comodel": comodel,
                        "line": statement.lineno,
                    }
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                klass.methods.append(_read_method(statement))
        result.classes.append(klass)
    return result


def _iter_class_defs(body):
    """Classes at module level and nested in classes. Never inside a function.

    A class built inside a function only exists if that function runs, and this
    reader never runs anything.
    """
    for node in body:
        if isinstance(node, ast.ClassDef):
            yield node
            yield from _iter_class_defs(node.body)


@dataclass
class ModelCall:
    """A call on a literally-named model, made from inside some method."""

    controller: str
    method: str
    target_model: str
    target_method: str
    conditional: bool
    line: int


def read_model_calls(source: bytes) -> list[ModelCall]:
    """``request.env['sale.order'].action_confirm()`` sites, per method.

    Used to bind an HTTP route to the model method it reaches. Only the
    literal shape counts: ``env[model_name]`` with a variable, or a method
    reached through a local alias, produces nothing rather than a guess.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    calls: list[ModelCall] = []
    for klass in _iter_class_defs(tree.body):
        for statement in klass.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node, ancestors in _walk_with_parents(statement):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)):
                    continue
                target_method = node.func.attr
                # Skip the ORM's own verbs: they say nothing about a workflow
                # step, and every controller uses them.
                if target_method in ("search", "browse", "read", "search_read",
                                     "sudo", "with_context", "with_user",
                                     "exists", "ensure_one", "search_count",
                                     "read_group", "filtered", "mapped"):
                    continue
                model = _receiver_model(node.func.value)
                if not model:
                    continue
                conditional, _condition = _condition_stack(ancestors)
                calls.append(ModelCall(
                    controller=klass.name,
                    method=statement.name,
                    target_model=model,
                    target_method=target_method,
                    conditional=conditional,
                    line=node.lineno,
                ))
    return calls
