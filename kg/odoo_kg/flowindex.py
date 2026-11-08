"""Building the workflow layer: workflows, states, transitions, triggers, crons.

The shape of the claim
----------------------
A workflow is one state-carrying field on one model. That pairing is the
identity. ``draft`` on ``sale.order`` and ``draft`` on ``account.move`` are two
different states, and every id here includes the model and the field so they
can never merge.

Three evidence kinds, kept strictly apart:

``declared``
    The source says it outright: a value in a ``Selection`` list, an XML button
    naming a method, an ``ir.cron`` record naming a model and a method.

``static``
    An AST verified it: ``self.state = 'done'``, ``write({'state': 'done'})``,
    a controller calling ``env['sale.order'].action_confirm()``.

``heuristic``
    Inferred rather than read: a source state taken from a nearby guard, a
    transition whose target state no Selection declares. A candidate for a
    human to confirm, never a confirmed business process.

``documented`` belongs to M4.3 and is never written here.

What this refuses to do
-----------------------
It does not evaluate. Not a domain, not a Python expression, not an XML
``invisible`` attribute beyond recognising two exact textual shapes. It does
not decide that a transition ran, only that a method contains a line that
would set a state. It does not connect two models because their state values
happen to share a name -- a cross-model edge exists only where one model's code
literally names the other. And it never invents a state: a write whose value is
an expression produces a transition with no target and the expression kept
beside it.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field as dc_field

from .flowread import (
    FlowReadError, is_stage_field, is_state_field, read_flows, read_model_calls,
)
from .store import (
    GraphEdge, NodeDiagnostic, ScheduledActionRow, TransitionRow, TriggerRow,
    WorkflowRow, WorkflowStateRow, stable_id,
)
from .xmlread import XmlReadError, read_xml

EVIDENCE_DECLARED = "declared"
EVIDENCE_STATIC = "static"
EVIDENCE_HEURISTIC = "heuristic"

#: The confidence vocabulary the edge tables already enforce. `certain` is
#: reserved for what the source states outright; anything inferred is lower.
CONFIDENCE_FOR_EVIDENCE = {
    EVIDENCE_DECLARED: "certain",
    EVIDENCE_STATIC: "certain",
    EVIDENCE_HEURISTIC: "heuristic",
}

#: ``model.<name>()`` and nothing else. An ir.cron whose code is anything more
#: than a single bare call keeps its text and gets no method edge.
CRON_CODE_CALL = re.compile(r"^\s*model\.([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)\s*$")

#: The only two shapes of an `invisible` attribute this build will read state
#: names out of. Both are inverted to give the states the button is visible in.
#: Anything else keeps its raw text and yields no states -- an expression is
#: read, never evaluated, and never half-understood.
INVISIBLE_NE = re.compile(
    r"^\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*!=\s*'(?P<value>[^']*)'\s*$"
)
INVISIBLE_NOT_IN = re.compile(
    r"^\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s+not\s+in\s*[\(\[]"
    r"(?P<values>[^\)\]]*)[\)\]]\s*$"
)
_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")


@dataclass
class WorkflowLayer:
    workflows: list[WorkflowRow] = dc_field(default_factory=list)
    states: list[WorkflowStateRow] = dc_field(default_factory=list)
    transitions: list[TransitionRow] = dc_field(default_factory=list)
    triggers: list[TriggerRow] = dc_field(default_factory=list)
    crons: list[ScheduledActionRow] = dc_field(default_factory=list)
    edges: list[GraphEdge] = dc_field(default_factory=list)
    diagnostics: list[NodeDiagnostic] = dc_field(default_factory=list)
    counts: dict[str, int] = dc_field(default_factory=dict)


@dataclass
class FlowSource:
    """One scanned file, addressed the way the rest of the graph addresses it."""

    module: str
    rel_path: str
    file_key: str
    disk_path: str


class MethodIndex:
    """The Python layer's methods, looked up two ways.

    ``get((model, name))`` is the exact answer, used where the model is known.
    ``by_name[name]`` is every model defining a method of that name, used by XML
    buttons -- a button names a method but not a model, and which model it hits
    depends on the view it sits in.
    """

    def __init__(self, rows=()):
        self._by_pair: dict[tuple[str, str], str] = {}
        self.by_name: dict[str, list[tuple[str, str]]] = {}
        for model_name, method_name, method_id in rows:
            self._by_pair.setdefault((model_name, method_name), method_id)
            bucket = self.by_name.setdefault(method_name, [])
            if not any(m == model_name for m, _ in bucket):
                bucket.append((model_name, method_id))

    def get(self, key):
        return self._by_pair.get(key)


class RouteIndex:
    """The frontend layer's routes, addressed by where they were declared."""

    def __init__(self, rows=()):
        self._by_site: dict[tuple[str, str, str], list[str]] = {}
        self._files: set[str] = set()
        for rel_path, controller, method, route_id in rows:
            key = (rel_path, controller, method)
            self._by_site.setdefault(key, []).append(route_id)
            self._files.add(rel_path)

    def has_file(self, rel_path) -> bool:
        return rel_path in self._files

    def get(self, rel_path, controller, method):
        return sorted(self._by_site.get((rel_path, controller, method), ()))


class _Builder:
    """Holds the state one build needs. Never a module global.

    Two builds in one process must not be able to see each other's data -- a
    lesson this project has now learned twice, in M3.2 and again in M4.1.
    """

    def __init__(self, project_id, known_models, method_index, route_index):
        self.project_id = project_id
        self.known_models = known_models
        #: (model, method_name) -> method_id, from the Python layer.
        self.method_index = method_index
        #: (rel_path, controller, method) -> route_id, from the frontend layer.
        self.route_index = route_index
        self.layer = WorkflowLayer()
        self.counts = self.layer.counts
        #: (model, field) -> WorkflowRow
        self.workflows: dict[tuple[str, str], WorkflowRow] = {}
        #: (model, field) -> {value: WorkflowStateRow}
        self.states: dict[tuple[str, str], dict[str, WorkflowStateRow]] = {}
        #: (model, field) -> where the Selection was declared
        self.declared_at: dict[tuple[str, str], tuple[str, int, str]] = {}
        self._transition_seen: dict[tuple, int] = {}

    def occurrence(self, *parts) -> int:
        """A stable ordinal for otherwise identical transitions in one method.

        Line numbers are deliberately not part of a transition's identity: they
        churn on reformatting, and an id that changes when nothing changed
        makes every digest comparison useless. Two writes of the same value in
        the same method are told apart by their order instead.
        """
        key = tuple(parts)
        seen = self._transition_seen.get(key, 0)
        self._transition_seen[key] = seen + 1
        return seen

    # -- identity ------------------------------------------------------

    def workflow_id(self, model, field):
        return stable_id(self.project_id, "workflow", model, field)

    def state_id(self, model, field, value):
        return stable_id(self.project_id, "workflow_state", model, field, value)

    def bump(self, key, amount=1):
        self.counts[key] = self.counts.get(key, 0) + amount

    # -- workflows and states -------------------------------------------

    def workflow(self, model, field, field_type, module, state_source,
                 rel_path, line, evidence):
        key = (model, field)
        row = self.workflows.get(key)
        if row is None:
            row = WorkflowRow(
                workflow_id=self.workflow_id(model, field),
                model_name=model, field_name=field, field_type=field_type,
                module=module, state_source=state_source, state_count=0,
                transition_count=0, has_dynamic_states=False,
                evidence=evidence, rel_path=rel_path, line=line,
            )
            self.workflows[key] = row
            self.states[key] = {}
        elif state_source == "selection" and row.state_source != "selection":
            # A Selection is better evidence than an inferred workflow, so it
            # wins and takes the declaration site with it.
            row.state_source = "selection"
            row.evidence = evidence
            row.rel_path, row.line = rel_path, line
        return row

    def state(self, model, field, value, label, position, evidence,
              rel_path, line):
        key = (model, field)
        bucket = self.states.setdefault(key, {})
        existing = bucket.get(value)
        if existing is not None:
            if existing.evidence == EVIDENCE_STATIC and evidence == EVIDENCE_DECLARED:
                existing.evidence = EVIDENCE_DECLARED
                existing.label = label or existing.label
                existing.position = position
                existing.rel_path, existing.line = rel_path, line
            return existing
        row = WorkflowStateRow(
            state_id=self.state_id(model, field, value),
            workflow_id=self.workflow_id(model, field),
            model_name=model, field_name=field, value=value, label=label,
            position=position, evidence=evidence, rel_path=rel_path, line=line,
        )
        bucket[value] = row
        return row

    # -- edges ----------------------------------------------------------

    def edge(self, table, src_kind, src_id, dst_kind, dst_id, target,
             provenance, evidence, rel_path, line, binding=None):
        confidence = CONFIDENCE_FOR_EVIDENCE[evidence]
        if binding is None:
            binding = "resolved" if dst_id else "unresolved"
        self.layer.edges.append(GraphEdge(
            table=table, src_kind=src_kind, src_id=src_id, dst_kind=dst_kind,
            dst_id=dst_id, dst_unresolved=target, binding=binding,
            confidence=confidence, provenance=provenance,
            src_file=rel_path, src_line=line,
        ))

    def note(self, severity, category, message, node_kind, node_id,
             rel_path=None, line=None):
        self.layer.diagnostics.append(NodeDiagnostic(
            severity, category, message, node_kind, node_id, rel_path, line
        ))


def _guard_states(guards, field_name):
    """The states a guard positively admits, as a (values, partial) pair.

    Only ``==`` and ``in`` say what a state *is*. ``!=`` and ``not in`` say
    what it is not, which does not name a source state, so they are ignored
    here rather than inverted against a state list this build may not have.
    """
    values: list[str] = []
    partial = False
    for guard in guards:
        if guard.field_name != field_name:
            continue
        if guard.operator in ("eq", "in"):
            values.extend(guard.values)
            partial = partial or guard.partial
        else:
            partial = True
    ordered = sorted(dict.fromkeys(values))
    return ordered, partial


def _condition_kind(write, method_flow):
    """Why a transition is conditional, in one word."""
    if write.conditional:
        return "branch"
    if method_flow.raises:
        # Not lexically nested, but the method refuses to proceed in some
        # states. Calling that unconditional would overstate it.
        return "guarded_by_raise"
    return None


def _read_invisible(text):
    """The states an XML button is visible in, if the shape is exactly known.

    Returns (states, evidence) or ([], None). Two shapes are recognised, both
    read purely textually:

        invisible="state != 'draft'"          -> visible in draft
        invisible="state not in ('a','b')"    -> visible in a, b

    Anything else -- an ``or``, a comparison against a field, a context key --
    yields nothing. Half-reading a condition is worse than not reading it.
    """
    if not text:
        return [], None
    match = INVISIBLE_NE.match(text)
    if match and is_state_field(match.group("field")):
        return [match.group("value")], EVIDENCE_DECLARED
    match = INVISIBLE_NOT_IN.match(text)
    if match and is_state_field(match.group("field")):
        values = [a or b for a, b in _QUOTED.findall(match.group("values"))]
        if values:
            return values, EVIDENCE_DECLARED
    return [], None


def build_workflow_layer(project_id, sources, read_bytes, known_models,
                         method_index, route_index) -> WorkflowLayer:
    """Build the whole workflow layer from already-scanned sources."""
    builder = _Builder(project_id, known_models, method_index, route_index)

    _index_python(builder, sources, read_bytes)
    _index_xml(builder, sources, read_bytes)
    _index_route_calls(builder, sources, read_bytes)
    _finalise(builder)
    return builder.layer


# --------------------------------------------------------------------------
# Python: Selection states, transitions, guards, cross-model operations
# --------------------------------------------------------------------------


def _index_python(builder, sources, read_bytes) -> None:
    for source in sources:
        if not source.rel_path.lower().endswith(".py"):
            continue
        try:
            raw = read_bytes(source.disk_path)
        except OSError:
            continue
        try:
            flows = read_flows(raw)
        except FlowReadError:
            builder.bump("python_unreadable")
            continue
        if not flows.parsed:
            builder.bump("python_unparsed")
            for severity, category, message, line in flows.findings:
                builder.note(severity, category, f"{source.rel_path}: {message}",
                             "module", source.module, source.rel_path, line)
            continue

        for klass in flows.classes:
            _index_class(builder, source, klass)


def _index_class(builder, source, klass) -> None:
    model = klass.model_name

    # -- declared states, from Selection literals -----------------------
    for field_name, found in klass.selections.items():
        pairs, dynamic, line = found["pairs"], found["dynamic"], found["line"]
        if not pairs and not dynamic:
            continue
        workflow = builder.workflow(
            model, field_name, "Selection", source.module, "selection",
            source.rel_path, line, EVIDENCE_DECLARED,
        )
        if dynamic:
            workflow.has_dynamic_states = True
            builder.bump("selection_computed")
            builder.note(
                "info", "workflow_states_partly_computed",
                f"{model}.{field_name}: part of the Selection is produced at "
                "runtime, so the declared states are not the complete set",
                "workflow", workflow.workflow_id, source.rel_path, line,
            )
        for position, (value, label) in enumerate(pairs):
            builder.state(model, field_name, value, label, position,
                          EVIDENCE_DECLARED, source.rel_path, line)
            builder.bump("states_declared")
        builder.declared_at[(model, field_name)] = (source.rel_path, line,
                                                    source.module)

    # -- stage fields: workflows whose states live in data ---------------
    for field_name, found in klass.stage_fields.items():
        workflow = builder.workflow(
            model, field_name, "Many2one", source.module, "records",
            source.rel_path, found["line"], EVIDENCE_DECLARED,
        )
        builder.bump("workflow_stage_field")
        builder.note(
            "info", "workflow_states_are_data_records",
            f"{model}.{field_name} points at {found['comodel'] or 'a stage model'}; "
            "its states are data records, which this build does not read, so "
            "the workflow has no declared states here",
            "workflow", workflow.workflow_id, source.rel_path, found["line"],
        )

    # -- transitions -----------------------------------------------------
    for method in klass.methods:
        method_id = builder.method_index.get((model, method.method_name))
        guard_cache = {}
        for write in method.writes:
            target_model = write.target_model or model
            field_name = write.field_name
            if not is_state_field(field_name) and not is_stage_field(field_name):
                continue

            # A write against a literally-named other model belongs to that
            # model's workflow, not to the class it was written in.
            cross_model = write.target_model is not None and write.target_model != model
            workflow = builder.workflow(
                target_model, field_name, "Selection", source.module,
                "inferred", source.rel_path, write.line, EVIDENCE_STATIC,
            )

            if write.value is None:
                _record_dynamic(builder, source, model, method, write,
                                workflow, method_id)
                continue

            known = builder.states.get((target_model, field_name), {})
            if write.value not in known:
                builder.state(target_model, field_name, write.value, None,
                              len(known), EVIDENCE_STATIC, source.rel_path,
                              write.line)
                builder.bump("states_only_seen_in_code")
                builder.note(
                    "warning", "transition_target_not_in_selection",
                    f"{target_model}.{field_name} is set to {write.value!r} at "
                    f"{source.rel_path}:{write.line}, but no Selection this "
                    "build read declares that value",
                    "workflow", workflow.workflow_id, source.rel_path, write.line,
                )

            if field_name not in guard_cache:
                guard_cache[field_name] = _guard_states(method.guards, field_name)
            from_candidates, guard_partial = guard_cache[field_name]
            from_state = from_candidates[0] if len(from_candidates) == 1 else None
            from_evidence = EVIDENCE_HEURISTIC if from_state else None

            kind = _condition_kind(write, method)
            if kind is None and (len(from_candidates) > 1 or guard_partial):
                # Conditional for a third reason: the method guards on this
                # field in more than one place, or on something this build
                # could not read in full. Saying "conditional" without saying
                # why would be the flattening this layer exists to avoid.
                kind = ("ambiguous_guard" if len(from_candidates) > 1
                        else "partly_read_guard")
            conditional = bool(kind)
            transition = _make_transition(
                builder, source, workflow, target_model, field_name,
                from_state, from_evidence, write, method, method_id,
                conditional, kind,
            )
            builder.bump("transitions_static")
            if cross_model:
                builder.bump("transitions_cross_model")

            if len(from_candidates) > 1:
                builder.note(
                    "info", "transition_source_state_ambiguous",
                    f"{model}.{method.method_name} guards on more than one "
                    f"value of {field_name} ({', '.join(from_candidates)}), so "
                    "no single source state can be attributed to this "
                    "transition",
                    "transition", transition.transition_id, source.rel_path,
                    write.line,
                )

        # -- cross-model create/write, whether or not it set a state ------
        for operation in method.cross_model:
            if operation.target_model == model:
                continue
            resolved = operation.target_model in builder.known_models
            builder.edge(
                "transition_touches_model", "method",
                method_id or f"{model}.{method.method_name}", "model",
                operation.target_model if resolved else None,
                operation.target_model,
                f"python.{operation.operation}",
                EVIDENCE_STATIC, source.rel_path, operation.line,
                binding="resolved" if resolved else "external",
            )
            builder.bump("cross_model_operations")


def _record_dynamic(builder, source, model, method, write, workflow, method_id):
    """A write whose value is an expression. Kept as a question, not an answer."""
    transition = TransitionRow(
        transition_id=stable_id(
            builder.project_id, "transition", workflow.workflow_id,
            method.method_name, write.operation, "<dynamic>",
            builder.occurrence(workflow.workflow_id, method.method_name,
                        write.operation, "<dynamic>"),
        ),
        workflow_id=workflow.workflow_id,
        model_name=workflow.model_name, field_name=write.field_name,
        from_state=None, from_evidence=None, to_state=None,
        operation=write.operation, receiver=write.receiver,
        method_name=method.method_name, method_id=method_id,
        conditional=bool(_condition_kind(write, method)),
        condition_kind=_condition_kind(write, method),
        condition_text=write.condition,
        dynamic_expression=write.dynamic_expression,
        evidence=EVIDENCE_STATIC, confidence="heuristic",
        module=source.module, rel_path=source.rel_path, line=write.line,
    )
    builder.layer.transitions.append(transition)
    builder.bump("transitions_dynamic_target")
    builder.edge(
        "transition_in_workflow", "transition", transition.transition_id,
        "workflow", workflow.workflow_id, workflow.workflow_id,
        f"python.{write.operation}", EVIDENCE_STATIC, source.rel_path, write.line,
    )
    if method_id:
        builder.edge(
            "method_performs_transition", "method", method_id, "transition",
            transition.transition_id, transition.transition_id,
            f"python.{write.operation}", EVIDENCE_STATIC, source.rel_path,
            write.line,
        )
    builder.note(
        "info", "transition_target_is_an_expression",
        f"{model}.{method.method_name} sets {write.field_name} to "
        f"{write.dynamic_expression!r}, which this build does not evaluate; the "
        "transition is recorded with no target state",
        "transition", transition.transition_id, source.rel_path, write.line,
    )


def _make_transition(builder, source, workflow, model, field_name, from_state,
                     from_evidence, write, method, method_id, conditional, kind):
    transition = TransitionRow(
        transition_id=stable_id(
            builder.project_id, "transition", workflow.workflow_id,
            method.method_name, write.operation, write.value,
            builder.occurrence(workflow.workflow_id, method.method_name,
                        write.operation, write.value),
        ),
        workflow_id=workflow.workflow_id,
        model_name=model, field_name=field_name,
        from_state=from_state, from_evidence=from_evidence,
        to_state=write.value, operation=write.operation,
        receiver=write.receiver, method_name=method.method_name,
        method_id=method_id, conditional=conditional, condition_kind=kind,
        condition_text=write.condition,
        dynamic_expression=None,
        evidence=EVIDENCE_STATIC, confidence="certain",
        module=source.module, rel_path=source.rel_path, line=write.line,
    )
    builder.layer.transitions.append(transition)

    builder.edge(
        "transition_in_workflow", "transition", transition.transition_id,
        "workflow", workflow.workflow_id, workflow.workflow_id,
        f"python.{write.operation}", EVIDENCE_STATIC, source.rel_path, write.line,
    )
    builder.edge(
        "transition_to_state", "transition", transition.transition_id,
        "workflow_state", builder.state_id(model, field_name, write.value),
        write.value, f"python.{write.operation}", EVIDENCE_STATIC,
        source.rel_path, write.line,
    )
    if from_state:
        builder.edge(
            "transition_from_state", "transition", transition.transition_id,
            "workflow_state", builder.state_id(model, field_name, from_state),
            from_state, "python.guard", EVIDENCE_HEURISTIC,
            source.rel_path, write.line,
        )
    if method_id:
        builder.edge(
            "method_performs_transition", "method", method_id, "transition",
            transition.transition_id, transition.transition_id,
            f"python.{write.operation}", EVIDENCE_STATIC, source.rel_path,
            write.line,
        )
    return transition


# --------------------------------------------------------------------------
# XML: buttons and ir.cron records
# --------------------------------------------------------------------------


def _index_xml(builder, sources, read_bytes) -> None:
    for source in sources:
        if not source.rel_path.lower().endswith(".xml"):
            continue
        try:
            raw = read_bytes(source.disk_path)
        except OSError:
            continue
        try:
            roots = read_xml(raw)
        except XmlReadError:
            builder.bump("xml_unreadable")
            continue

        for root in roots:
            for node in root.iter():
                if node.tag == "button":
                    _index_button(builder, source, node)
                elif node.tag == "record" and node.get("model") == "ir.cron":
                    _index_cron(builder, source, node)


def _index_button(builder, source, node) -> None:
    if (node.get("type") or "").strip() != "object":
        return
    name = (node.get("name") or "").strip()
    if not name:
        return
    builder.bump("xml_buttons")

    guard_text = node.get("states") or node.get("invisible") or None
    guard_states, guard_evidence = _read_invisible(node.get("invisible"))
    if node.get("states"):
        # The pre-17 form: a literal list of states the button shows in.
        guard_states = [s.strip() for s in node.get("states").split(",") if s.strip()]
        guard_evidence = EVIDENCE_DECLARED

    trigger_id = stable_id(
        builder.project_id, "trigger", "xml_button", source.file_key,
        node.line, name,
    )
    builder.layer.triggers.append(TriggerRow(
        trigger_id=trigger_id, kind="xml_button", name=name,
        module=source.module, target_model=None, target_method=name,
        guard_text=guard_text,
        guard_states=",".join(guard_states) if guard_states else None,
        guard_state_evidence=guard_evidence,
        evidence=EVIDENCE_DECLARED, rel_path=source.rel_path, line=node.line,
    ))

    # A button names a method, not a model: the view it sits in decides the
    # model, and an inherited view can move it. Bind to every method of that
    # name this project defines, and say so when there is more than one.
    candidates = builder.method_index.by_name.get(name, ())
    if len(candidates) == 1:
        (model, method_id), = candidates
        builder.edge(
            "trigger_invokes_method", "trigger", trigger_id, "method",
            method_id, name, "xml.button@name", EVIDENCE_DECLARED,
            source.rel_path, node.line,
        )
        builder.bump("button_bound_to_one_method")
    elif candidates:
        # Unresolved, so the target must say what was sought well enough to be
        # acted on without the edge's context -- the same rule the Python layer
        # follows. A bare method name would not: the point is precisely that
        # the model is undetermined.
        builder.edge(
            "trigger_invokes_method", "trigger", trigger_id, "method",
            None, f"unknown:method_on_several_models:{name}",
            "xml.button@name", EVIDENCE_DECLARED,
            source.rel_path, node.line, binding="unresolved",
        )
        builder.bump("button_method_ambiguous")
        builder.note(
            "info", "button_method_defined_on_several_models",
            f"button {name!r} names a method defined on "
            f"{len(candidates)} models "
            f"({', '.join(sorted(m for m, _ in candidates)[:5])}); which one "
            "runs depends on the view's model, which this build does not "
            "resolve",
            "trigger", trigger_id, source.rel_path, node.line,
        )
    else:
        builder.edge(
            "trigger_invokes_method", "trigger", trigger_id, "method",
            None, name, "xml.button@name", EVIDENCE_DECLARED,
            source.rel_path, node.line, binding="external",
        )
        builder.bump("button_method_not_in_project")


def _index_cron(builder, source, node) -> None:
    fields = {}
    for child in node.iter():
        if child.tag == "field" and child.get("name"):
            fields[child.get("name")] = (
                child.get("ref") or child.get("eval") or child.text or ""
            ).strip()

    declared_id = node.get("id") or ""
    xmlid = declared_id if "." in declared_id else (
        f"{source.module}.{declared_id}" if declared_id else None
    )
    cron_id = stable_id(builder.project_id, "cron", source.file_key, node.line,
                        declared_id)
    code = fields.get("code") or ""
    match = CRON_CODE_CALL.match(code)
    method_name = match.group(1) if match else None
    code_shape = "model_method_call" if match else (
        "code" if code else "no_code"
    )
    model_ref = fields.get("model_id") or None
    target_model = _model_from_ref(model_ref)

    builder.layer.crons.append(ScheduledActionRow(
        cron_id=cron_id, xmlid=xmlid, name=fields.get("name"),
        module=source.module, model_ref=model_ref, target_model=target_model,
        method_name=method_name, interval=_interval(fields),
        active=fields.get("active"), code_text=code or None,
        code_shape=code_shape, evidence=EVIDENCE_DECLARED,
        rel_path=source.rel_path, line=node.line,
    ))
    builder.bump("crons")

    if target_model:
        resolved = target_model in builder.known_models
        builder.edge(
            "cron_on_model", "cron", cron_id, "model",
            target_model if resolved else None, target_model,
            "xml.ir_cron.model_id", EVIDENCE_DECLARED, source.rel_path, node.line,
            binding="resolved" if resolved else "external",
        )
    else:
        builder.bump("cron_without_resolvable_model")

    if method_name:
        method_id = (
            builder.method_index.get((target_model, method_name))
            if target_model else None
        )
        # Named the same way the Python layer names an unresolved target: a
        # dotted name when the model is known, an explicit reason when it is
        # not. Never a placeholder standing in for the part that is missing.
        target = (
            f"{target_model}.{method_name}" if target_model
            else f"unknown:cron_model_unresolved:{method_name}"
        )
        builder.edge(
            "cron_runs_method", "cron", cron_id, "method", method_id, target,
            "xml.ir_cron.code", EVIDENCE_DECLARED, source.rel_path, node.line,
            binding="resolved" if method_id else "unresolved",
        )
        builder.bump("cron_calls_method")
    else:
        builder.bump("cron_code_not_a_simple_call")
        if code:
            builder.note(
                "info", "cron_code_is_not_a_single_call",
                f"{xmlid or declared_id}: the scheduled code is not a bare "
                "`model.method()` call, so this build does not say which "
                "method it runs",
                "cron", cron_id, source.rel_path, node.line,
            )


def _model_from_ref(ref):
    """``model_sale_order`` / ``base.model_res_partner`` -> ``sale.order``."""
    if not ref:
        return None
    local = ref.split(".")[-1]
    if not local.startswith("model_"):
        return None
    return local[len("model_"):].replace("_", ".")


def _interval(fields):
    number = fields.get("interval_number")
    unit = fields.get("interval_type")
    if number and unit:
        return f"{number} {unit}"
    return unit or number or None


# --------------------------------------------------------------------------
# Controllers: a route reaching a model method
# --------------------------------------------------------------------------


def _index_route_calls(builder, sources, read_bytes) -> None:
    for source in sources:
        if not source.rel_path.lower().endswith(".py"):
            continue
        if not builder.route_index.has_file(source.rel_path):
            continue
        try:
            raw = read_bytes(source.disk_path)
        except OSError:
            continue
        for call in read_model_calls(raw):
            route_ids = builder.route_index.get(
                source.rel_path, call.controller, call.method
            )
            if not route_ids:
                continue
            method_id = builder.method_index.get(
                (call.target_model, call.target_method)
            )
            for route_id in route_ids:
                builder.edge(
                    "route_invokes_method", "route", route_id, "method",
                    method_id, f"{call.target_model}.{call.target_method}",
                    "python.controller.env_call", EVIDENCE_STATIC,
                    source.rel_path, call.line,
                    binding="resolved" if method_id else "external",
                )
                builder.bump("route_method_calls" if method_id
                             else "route_calls_outside_project")


# --------------------------------------------------------------------------
# finishing
# --------------------------------------------------------------------------


def _finalise(builder) -> None:
    layer = builder.layer
    transitions_by_workflow: dict[str, int] = {}
    for transition in layer.transitions:
        transitions_by_workflow[transition.workflow_id] = (
            transitions_by_workflow.get(transition.workflow_id, 0) + 1
        )

    for key in sorted(builder.workflows):
        model = key[0]
        workflow = builder.workflows[key]
        states = builder.states.get(key, {})
        workflow.state_count = len(states)
        workflow.transition_count = transitions_by_workflow.get(
            workflow.workflow_id, 0
        )
        # A workflow with neither a declared state nor a transition describes
        # nothing. Dropping it keeps the layer about processes that exist.
        if workflow.state_count == 0 and workflow.transition_count == 0 \
                and workflow.state_source != "records":
            builder.bump("workflow_discarded_as_empty")
            continue
        layer.workflows.append(workflow)

        resolved = model in builder.known_models
        builder.edge(
            "workflow_on_model", "workflow", workflow.workflow_id, "model",
            model if resolved else None, model, "python.field", workflow.evidence,
            workflow.rel_path, workflow.line,
            binding="resolved" if resolved else "external",
        )
        for value in sorted(states):
            state = states[value]
            layer.states.append(state)
            builder.edge(
                "workflow_has_state", "workflow", workflow.workflow_id,
                "workflow_state", state.state_id, value,
                "python.selection" if state.evidence == EVIDENCE_DECLARED
                else "python.write", state.evidence,
                state.rel_path, state.line,
            )

    # A guard names the value it compares against (``if rec.state == 'won'``), and
    # that value is not always a state of this workflow: the guard may test a
    # different field, or a value the Selection never declared and nothing
    # writes. Such an edge was recorded as resolved to a state id that no node
    # carries. It keeps its target name and becomes unresolved instead.
    state_ids = {row.state_id for row in layer.states}
    for index, edge in enumerate(layer.edges):
        if edge.table == "transition_from_state" and edge.dst_id \
                and edge.dst_id not in state_ids:
            layer.edges[index] = dataclasses.replace(
                edge, dst_id=None, binding="unresolved")
            builder.bump("guard_value_not_a_state")

    kept = {row.workflow_id for row in layer.workflows}
    layer.transitions[:] = [t for t in layer.transitions if t.workflow_id in kept]
    layer.edges[:] = [
        e for e in layer.edges
        if not (e.table == "transition_in_workflow" and e.dst_id not in kept)
    ]

    layer.triggers.sort(key=lambda row: (row.rel_path, row.line, row.name))
    layer.crons.sort(key=lambda row: (row.rel_path, row.line, row.cron_id))
    layer.transitions.sort(
        key=lambda row: (row.model_name, row.field_name, row.rel_path, row.line,
                         row.transition_id)
    )

    # A diagnostic must point at a node that exists. Some are written while a
    # workflow is still a candidate, and a workflow with neither a state nor a
    # transition is dropped above -- which would leave the finding attached to
    # nothing. Dropping those findings with it keeps the rule intact: every
    # diagnostic in the graph can be resolved to the thing it is about.
    known = {
        "workflow": {row.workflow_id for row in layer.workflows},
        "workflow_state": {row.state_id for row in layer.states},
        "transition": {row.transition_id for row in layer.transitions},
        "trigger": {row.trigger_id for row in layer.triggers},
        "cron": {row.cron_id for row in layer.crons},
    }
    kept_diagnostics = []
    for finding in layer.diagnostics:
        bucket = known.get(finding.node_kind)
        if bucket is None or finding.node_id in bucket:
            kept_diagnostics.append(finding)
        else:
            builder.bump("diagnostics_dropped_with_their_node")
    layer.diagnostics[:] = kept_diagnostics

    counts = builder.counts
    counts["workflows"] = len(layer.workflows)
    counts["states"] = len(layer.states)
    counts["transitions"] = len(layer.transitions)
    counts["triggers"] = len(layer.triggers)
    counts["scheduled_actions"] = len(layer.crons)
    counts["edges"] = len(layer.edges)
