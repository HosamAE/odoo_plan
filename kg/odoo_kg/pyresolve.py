"""Second pass: turn parsed Python structure into graph nodes and edges.

The parser in :mod:`odoo_kg.pyast` reads one file at a time and never looks
beyond it. Everything that needs the whole picture happens here:

* Model identity is collected across files, because one model is routinely
  defined in one module and extended in five others.
* ``related=`` comodels are derived. A related field inherits its comodel from
  the field at the end of its path, so the target cannot be known until every
  field in the project has been read. This is the "separate second pass" the
  milestone calls for, and it is a fixpoint: a related field may point at
  another related field, so derivation repeats until nothing new resolves.
* Bindings are decided. The rule is the same one the module layer already uses:
  a target that is not in this graph does not get a node invented for it. It is
  recorded by name as ``external`` when the name is known and ``unresolved``
  when it is not, and an unresolved target always produces a diagnostic.

Nothing here guesses a target. If a path cannot be walked to the end, the edge
says so rather than falling back to a plausible-looking model.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from .pyast import ParsedField, ParsedModelDef, ParsedPythonFile
from .store import (
    FieldRow,
    GraphEdge,
    MethodRow,
    ModelDefRow,
    ModelRow,
    NodeDiagnostic,
    stable_id,
)

#: ``dst_unresolved`` must always say what was being looked for. An edge that
#: could not be bound is still evidence, and an empty string throws that
#: evidence away: forty-eight unresolved relations that all read ``''`` are
#: indistinguishable from each other and from a bug.
#:
#: Two shapes, both non-empty and both parseable:
#:
#:   ``related_path:<field.path>``  the target was to be reached by following a
#:                                  ``related=`` path, and the path is given
#:   ``unknown:<reason>``           there was no name and no path to record
#:
#: Used identically by ``edge_field_relation`` and ``edge_field_inverse_of``;
#: the latter appends ``.<inverse_name>`` so the descriptor still reads as a
#: field reference.
UNRESOLVED_RELATED_PREFIX = "related_path:"
UNRESOLVED_UNKNOWN_PREFIX = "unknown:"

#: How many derivation rounds to run before declaring the remainder unresolved.
#: Each round can only resolve fields whose dependencies resolved in an earlier
#: one, so the count bounds the longest related-of-related chain that will be
#: followed. Ten is far beyond anything seen in practice and keeps a cyclic
#: definition from looping forever.
MAX_DERIVATION_ROUNDS = 10


@dataclass
class SourceFile:
    """One scanned Python file and what the parser made of it."""

    file_key: str
    rel_path: str
    module: str | None
    parsed: ParsedPythonFile


@dataclass
class _DefRecord:
    def_id: str
    source: SourceFile
    parsed: ParsedModelDef


@dataclass
class _FieldRecord:
    field_id: str
    def_record: _DefRecord
    parsed: ParsedField
    #: Filled in by the derivation pass. None means "still not known".
    derived_comodel: str | None = None
    #: Why the comodel could not be derived, if it could not be.
    unresolved_reason: str | None = None

    @property
    def comodel(self) -> str | None:
        return self.parsed.comodel_literal or self.derived_comodel


@dataclass
class PythonLayer:
    """Everything the Python pass produces, ready to be written."""

    models: list[ModelRow] = dc_field(default_factory=list)
    defs: list[ModelDefRow] = dc_field(default_factory=list)
    fields: list[FieldRow] = dc_field(default_factory=list)
    methods: list[MethodRow] = dc_field(default_factory=list)
    edges: list[GraphEdge] = dc_field(default_factory=list)
    diagnostics: list[NodeDiagnostic] = dc_field(default_factory=list)
    #: Counters for the run summary, keyed by a short stable name.
    counts: dict[str, int] = dc_field(default_factory=dict)


def build_python_layer(project_id: str, sources: list[SourceFile]) -> PythonLayer:
    """Build the whole Python layer from parsed files. Pure; writes nothing."""
    layer = PythonLayer()

    def_records = _collect_defs(project_id, sources)
    field_records = _collect_fields(project_id, def_records)
    known_models = {
        record.parsed.model_name
        for record in def_records
        if record.parsed.model_name is not None
    }

    fields_by_model = _index_fields_by_model(field_records)
    parents = _index_parents(def_records)

    derived, still_unresolved = _derive_related_comodels(
        field_records, fields_by_model, parents
    )
    layer.counts["related_derived"] = derived
    layer.counts["related_unresolved"] = still_unresolved

    _emit_models(layer, def_records, field_records, known_models)
    _emit_defs(layer, def_records, known_models)
    _emit_fields(layer, field_records, known_models, fields_by_model, parents)
    _emit_methods(layer, project_id, def_records, known_models)
    _emit_parser_findings(layer, sources, def_records)

    layer.counts["models"] = len(layer.models)
    layer.counts["model_defs"] = len(layer.defs)
    layer.counts["fields"] = len(layer.fields)
    layer.counts["methods"] = len(layer.methods)
    layer.counts["edges"] = len(layer.edges)
    return layer


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------


def _collect_defs(project_id: str, sources: list[SourceFile]) -> list[_DefRecord]:
    records: list[_DefRecord] = []
    for source in sources:
        for parsed in source.parsed.defs:
            # Identity is (file, class, line). Two classes of the same name in
            # one file are different definitions and must not collapse.
            def_id = stable_id(
                project_id, "model_def", source.file_key, parsed.class_name, parsed.line
            )
            records.append(_DefRecord(def_id=def_id, source=source, parsed=parsed))
    records.sort(key=lambda r: (r.source.file_key, r.parsed.line, r.parsed.class_name))
    return records


def _collect_fields(project_id: str, defs: list[_DefRecord]) -> list[_FieldRecord]:
    records: list[_FieldRecord] = []
    for def_record in defs:
        for parsed in def_record.parsed.fields:
            field_id = stable_id(project_id, "field", def_record.def_id, parsed.name)
            records.append(
                _FieldRecord(field_id=field_id, def_record=def_record, parsed=parsed)
            )
    return records


def _index_fields_by_model(
    fields: list[_FieldRecord],
) -> dict[str, dict[str, _FieldRecord]]:
    """model -> field name -> record.

    When the same field name is declared on one model in several places -- an
    override in another module -- the first by (file, line) wins, so the index
    is deterministic rather than dependent on traversal order.
    """
    index: dict[str, dict[str, _FieldRecord]] = {}
    for record in sorted(
        fields,
        key=lambda r: (r.def_record.source.file_key, r.def_record.parsed.line, r.parsed.name),
    ):
        model = record.def_record.parsed.model_name
        if model is None:
            continue
        index.setdefault(model, {}).setdefault(record.parsed.name, record)
    return index


def _index_parents(defs: list[_DefRecord]) -> dict[str, set[str]]:
    """model -> the models it inherits from or delegates to.

    Both ``_inherit`` and ``_inherits`` contribute, because a field reached
    through delegation is just as real as one reached through inheritance when
    following a ``related`` path.
    """
    parents: dict[str, set[str]] = {}
    for record in defs:
        model = record.parsed.model_name
        if model is None:
            continue
        bucket = parents.setdefault(model, set())
        for target in record.parsed.inherit:
            if target != model:
                bucket.add(target)
        for target, _column in record.parsed.delegates:
            if target != model:
                bucket.add(target)
    return parents


# --------------------------------------------------------------------------
# pass 2: related-comodel derivation
# --------------------------------------------------------------------------


def _lookup_field(
    model: str,
    name: str,
    fields_by_model: dict[str, dict[str, _FieldRecord]],
    parents: dict[str, set[str]],
) -> _FieldRecord | None:
    """Find a field on a model, following inheritance and delegation.

    Breadth-first so a field declared directly on the model wins over one
    inherited from a parent, which is what Odoo does.
    """
    seen: set[str] = set()
    queue = [model]
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        found = fields_by_model.get(current, {}).get(name)
        if found is not None:
            return found
        queue.extend(sorted(parents.get(current, ())))
    return None


def _walk_related(
    record: _FieldRecord,
    fields_by_model: dict[str, dict[str, _FieldRecord]],
    parents: dict[str, set[str]],
) -> tuple[str | None, str | None]:
    """Follow a ``related`` path. Returns (comodel, reason_it_failed)."""
    start = record.def_record.parsed.model_name
    if start is None:
        return None, "the declaring class has no model identity"
    path = (record.parsed.related_path or "").split(".")
    if not path or not all(path):
        return None, "related path is empty or malformed"

    current_model = start
    for index, segment in enumerate(path):
        hop = _lookup_field(current_model, segment, fields_by_model, parents)
        if hop is None:
            return None, (
                f"field {segment!r} is not declared on {current_model!r} in this "
                "project; it most likely lives in Odoo core, which is not indexed"
            )
        if index == len(path) - 1:
            if not hop.parsed.is_relational:
                return None, (
                    f"the path ends on {segment!r}, which is a "
                    f"{hop.parsed.field_type} and has no comodel"
                )
            return hop.comodel, (
                None if hop.comodel else f"the comodel of {segment!r} is itself unknown"
            )
        if not hop.parsed.is_relational:
            return None, (
                f"cannot traverse {segment!r}: it is a {hop.parsed.field_type}, "
                "not a relation"
            )
        next_model = hop.comodel
        if next_model is None:
            return None, f"the comodel of {segment!r} is not known yet"
        current_model = next_model
    return None, "related path did not terminate"


def _derive_related_comodels(
    fields: list[_FieldRecord],
    fields_by_model: dict[str, dict[str, _FieldRecord]],
    parents: dict[str, set[str]],
) -> tuple[int, int]:
    """Resolve every relational field whose comodel came from ``related``.

    Repeats until a round resolves nothing new, because a related field may
    depend on another related field that had not been derived yet.
    """
    pending = [
        record
        for record in fields
        if record.parsed.is_relational and record.parsed.comodel_state == "related"
    ]
    derived = 0
    for _round in range(MAX_DERIVATION_ROUNDS):
        progressed = False
        for record in pending:
            if record.derived_comodel is not None:
                continue
            comodel, reason = _walk_related(record, fields_by_model, parents)
            if comodel is not None:
                record.derived_comodel = comodel
                record.unresolved_reason = None
                derived += 1
                progressed = True
            else:
                record.unresolved_reason = reason
        if not progressed:
            break
    unresolved = sum(1 for record in pending if record.derived_comodel is None)
    return derived, unresolved


# --------------------------------------------------------------------------
# emission
# --------------------------------------------------------------------------


def unresolved_target(parsed: ParsedField) -> str:
    """A non-empty, self-describing stand-in for a target that is not known.

    See ``UNRESOLVED_RELATED_PREFIX``. The point is that the edge alone must
    still answer "what were you trying to reach?" without a join back to the
    field row.
    """
    if parsed.related_path:
        return f"{UNRESOLVED_RELATED_PREFIX}{parsed.related_path}"
    if parsed.comodel_state == "dynamic":
        return f"{UNRESOLVED_UNKNOWN_PREFIX}comodel_expression"
    return f"{UNRESOLVED_UNKNOWN_PREFIX}comodel_absent"


def _unresolved_model(kind: str) -> str:
    """The same convention for an edge whose *source* has no model identity."""
    return f"{UNRESOLVED_UNKNOWN_PREFIX}{kind}"


def _binding_for_model(target: str | None, known: set[str], derived: bool) -> tuple[str, str, str | None]:
    """(binding, confidence, dst_id) for an edge pointing at a model."""
    if target is None:
        return "unresolved", "certain", None
    if target in known:
        return ("derived" if derived else "resolved"), ("derived" if derived else "certain"), target
    # The name is known and understood; the model simply is not part of this
    # graph. No node is invented for it -- the same rule the module layer uses
    # for a dependency on Odoo core.
    return "external", ("derived" if derived else "certain"), None


def _emit_models(
    layer: PythonLayer,
    defs: list[_DefRecord],
    fields: list[_FieldRecord],
    known: set[str],
) -> None:
    by_model: dict[str, list[_DefRecord]] = {}
    for record in defs:
        model = record.parsed.model_name
        if model is not None:
            by_model.setdefault(model, []).append(record)

    field_counts: dict[str, int] = {}
    for record in fields:
        model = record.def_record.parsed.model_name
        if model is not None:
            field_counts[model] = field_counts.get(model, 0) + 1

    for model in sorted(by_model):
        records = by_model[model]
        declares = any(r.parsed.declared_name == model for r in records)
        # A model's kind comes from the definition that names it. An extension
        # class always reads models.Model regardless of what it extends, so
        # trusting any definition would mislabel abstract and transient models.
        naming = [r for r in records if r.parsed.declared_name == model]
        kind = (naming[0] if naming else records[0]).parsed.base_kind
        methods = sum(len(r.parsed.methods) for r in records)
        layer.models.append(
            ModelRow(
                model_name=model,
                kind=kind if declares else "extended",
                definition_count=len(records),
                declares_name=declares,
                field_count=field_counts.get(model, 0),
                method_count=methods,
            )
        )
        for record in records:
            layer.edges.append(
                GraphEdge(
                    table="model_defined_by",
                    src_kind="model",
                    src_id=model,
                    dst_kind="model_def",
                    dst_id=record.def_id,
                    dst_unresolved=record.def_id,
                    binding="resolved",
                    confidence="certain",
                    provenance="ast.class",
                    src_file=record.source.rel_path,
                    src_line=record.parsed.line,
                )
            )


def _emit_defs(layer: PythonLayer, defs: list[_DefRecord], known: set[str]) -> None:
    for record in defs:
        parsed = record.parsed
        layer.defs.append(
            ModelDefRow(
                def_id=record.def_id,
                model_name=parsed.model_name,
                module=record.source.module,
                file_key=record.source.file_key,
                rel_path=record.source.rel_path,
                class_name=parsed.class_name,
                line=parsed.line,
                end_line=parsed.end_line,
                base_kind=parsed.base_kind,
                declared_name=parsed.declared_name,
                shape=parsed.shape,
                parse_state=parsed.parse_state,
                field_count=len(parsed.fields),
                method_count=len(parsed.methods),
            )
        )
        # The definition, not the model, is the source of an inheritance edge:
        # it records *where* the claim was written, which a model-level edge
        # would lose when several modules extend the same model.
        for target in parsed.inherit:
            binding, confidence, dst = _binding_for_model(target, known, derived=False)
            layer.edges.append(
                GraphEdge(
                    table="model_inherits",
                    src_kind="model_def",
                    src_id=record.def_id,
                    dst_kind="model",
                    dst_id=dst,
                    dst_unresolved=target,
                    binding=binding,
                    confidence=confidence,
                    provenance="ast.model._inherit",
                    src_file=record.source.rel_path,
                    src_line=parsed.line,
                )
            )
        for target, column in parsed.delegates:
            binding, confidence, dst = _binding_for_model(target, known, derived=False)
            layer.edges.append(
                GraphEdge(
                    table="model_delegates",
                    src_kind="model_def",
                    src_id=record.def_id,
                    dst_kind="model",
                    dst_id=dst,
                    dst_unresolved=target,
                    binding=binding,
                    confidence=confidence,
                    # The foreign key carrying the delegation is part of the
                    # relationship, so it travels with the edge.
                    provenance=f"ast.model._inherits[{column}]",
                    src_file=record.source.rel_path,
                    src_line=parsed.line,
                )
            )


def _emit_fields(
    layer: PythonLayer,
    fields: list[_FieldRecord],
    known: set[str],
    fields_by_model: dict[str, dict[str, _FieldRecord]],
    parents: dict[str, set[str]],
) -> None:
    for record in fields:
        parsed = record.parsed
        def_record = record.def_record
        model = def_record.parsed.model_name
        comodel_state = parsed.comodel_state
        if comodel_state == "related":
            comodel_state = "derived" if record.derived_comodel else "unresolved"

        layer.fields.append(
            FieldRow(
                field_id=record.field_id,
                def_id=def_record.def_id,
                model_name=model,
                field_name=parsed.name,
                field_type=parsed.field_type,
                is_relational=parsed.is_relational,
                comodel_literal=parsed.comodel_literal,
                comodel_state=comodel_state,
                related_path=parsed.related_path,
                compute_method=parsed.compute_method,
                inverse_name=parsed.inverse_name,
                stored=parsed.stored,
                required=parsed.required,
                readonly=parsed.readonly,
                groups_literal=parsed.groups_literal,
                rel_path=def_record.source.rel_path,
                line=parsed.line,
            )
        )

        binding, confidence, dst = _binding_for_model(model, known, derived=False)
        layer.edges.append(
            GraphEdge(
                table="field_declared_on",
                src_kind="field",
                src_id=record.field_id,
                dst_kind="model",
                dst_id=dst,
                dst_unresolved=model or _unresolved_model("model_identity"),
                binding=binding,
                confidence=confidence,
                provenance="ast.field",
                src_file=def_record.source.rel_path,
                src_line=parsed.line,
            )
        )

        if not parsed.is_relational:
            continue

        target = record.comodel
        was_derived = parsed.comodel_literal is None and record.derived_comodel is not None
        binding, confidence, dst = _binding_for_model(target, known, derived=was_derived)
        provenance = (
            "ast.field.comodel_name"
            if parsed.comodel_literal
            else f"ast.field.related[{parsed.related_path}]"
            if was_derived
            else f"ast.field.unresolved[{parsed.comodel_state}]"
        )
        layer.edges.append(
            GraphEdge(
                table="field_relation",
                src_kind="field",
                src_id=record.field_id,
                dst_kind="model",
                dst_id=dst,
                dst_unresolved=target or unresolved_target(parsed),
                binding=binding,
                confidence=confidence,
                provenance=provenance,
                src_file=def_record.source.rel_path,
                src_line=parsed.line,
            )
        )
        if binding == "unresolved":
            layer.diagnostics.append(
                NodeDiagnostic(
                    severity="info" if parsed.related_path else "warning",
                    category="field_comodel_unresolved",
                    message=(
                        f"{model}.{parsed.name}: comodel not determined"
                        + (f" -- {record.unresolved_reason}" if record.unresolved_reason else "")
                    ),
                    node_kind="field",
                    node_id=record.field_id,
                    rel_path=def_record.source.rel_path,
                    line=parsed.line,
                )
            )

        if parsed.field_type == "One2many" and parsed.inverse_name:
            _emit_inverse_edge(layer, record, target, known, fields_by_model, parents)


def _emit_inverse_edge(
    layer: PythonLayer,
    record: _FieldRecord,
    comodel: str | None,
    known: set[str],
    fields_by_model: dict[str, dict[str, _FieldRecord]],
    parents: dict[str, set[str]],
) -> None:
    """Link a One2many to the Many2one on the other side, when it is visible."""
    parsed = record.parsed
    source = record.def_record.source
    inverse_name = parsed.inverse_name or ""
    dst_id = None
    binding = "unresolved"
    if comodel is None:
        binding = "unresolved"
    elif comodel not in known:
        # The other side lives in core. The name of the inverse is known, the
        # field node is not in this graph, and no node is invented for it.
        binding = "external"
    else:
        inverse = _lookup_field(comodel, inverse_name, fields_by_model, parents)
        if inverse is not None:
            dst_id = inverse.field_id
            binding = "resolved"
    layer.edges.append(
        GraphEdge(
            table="field_inverse_of",
            src_kind="field",
            src_id=record.field_id,
            dst_kind="field",
            dst_id=dst_id,
            # Same descriptor vocabulary as field_relation, so one parser reads
            # both. When the comodel is known this is an ordinary
            # `model.field`; when it is not, the `related_path:`/`unknown:`
            # stand-in takes the model's place and the field name still follows.
            dst_unresolved=f"{comodel or unresolved_target(parsed)}.{inverse_name}",
            binding=binding,
            confidence="certain",
            provenance="ast.field.inverse_name",
            src_file=source.rel_path,
            src_line=parsed.line,
        )
    )
    if binding == "unresolved":
        layer.diagnostics.append(
            NodeDiagnostic(
                severity="info",
                category="one2many_inverse_unresolved",
                message=(
                    f"{record.def_record.parsed.model_name}.{parsed.name}: inverse "
                    f"{inverse_name!r} could not be located on {comodel or 'an unknown comodel'}"
                ),
                node_kind="field",
                node_id=record.field_id,
                rel_path=source.rel_path,
                line=parsed.line,
            )
        )


def _emit_methods(
    layer: PythonLayer, project_id: str, defs: list[_DefRecord], known: set[str]
) -> None:
    for record in defs:
        model = record.parsed.model_name
        for parsed in record.parsed.methods:
            # Identity is (project, definition, name) -- deliberately not the
            # line. A class body cannot hold two methods of one name, so the
            # line adds nothing to uniqueness, and including it made every id
            # and therefore the whole python digest change when a file was
            # merely reformatted. The line is still recorded on the row; it is
            # a fact about the method, not part of what the method *is*.
            #
            # project_id is passed explicitly rather than inherited through
            # def_id, so every M3 node id is scoped the same visible way.
            method_id = stable_id(project_id, "method", record.def_id, parsed.name)
            layer.methods.append(
                MethodRow(
                    method_id=method_id,
                    def_id=record.def_id,
                    model_name=model,
                    method_name=parsed.name,
                    kind=parsed.kind,
                    is_private=parsed.is_private,
                    decorators=", ".join(parsed.decorators) or None,
                    depends_literal=", ".join(parsed.depends_literal) or None,
                    args_state=parsed.args_state,
                    rel_path=record.source.rel_path,
                    line=parsed.line,
                )
            )
            binding, confidence, dst = _binding_for_model(model, known, derived=False)
            layer.edges.append(
                GraphEdge(
                    table="method_declared_on",
                    src_kind="method",
                    src_id=method_id,
                    dst_kind="model",
                    dst_id=dst,
                    dst_unresolved=model or _unresolved_model("model_identity"),
                    binding=binding,
                    confidence=confidence,
                    provenance="ast.method",
                    src_file=record.source.rel_path,
                    src_line=parsed.line,
                )
            )


def _emit_parser_findings(
    layer: PythonLayer, sources: list[SourceFile], defs: list[_DefRecord]
) -> None:
    """Attach parser findings to the node they belong to.

    A finding that names a class is hung on that definition; anything else is
    hung on the file. Either way it lands in ``diagnostic_node`` and not in the
    M1 ``diagnostic`` table, which stays exactly what it was.
    """
    # Keyed on the class's line as well as its name: one file may hold two
    # classes of the same name in different scopes, and a name-only key would
    # hand the second one's findings to the first.
    def_by_class: dict[tuple[str, str, int], _DefRecord] = {
        (record.source.file_key, record.parsed.class_name, record.parsed.line): record
        for record in defs
    }
    for source in sources:
        for finding in source.parsed.findings:
            record = (
                def_by_class.get(
                    (source.file_key, finding.scope_class, finding.scope_line)
                )
                if finding.scope_class and finding.scope_line is not None
                else None
            )
            if record is not None:
                node_kind, node_id = "model_def", record.def_id
            else:
                node_kind, node_id = "file", source.file_key
            layer.diagnostics.append(
                NodeDiagnostic(
                    severity=finding.severity,
                    category=finding.category,
                    message=finding.message,
                    node_kind=node_kind,
                    node_id=node_id,
                    rel_path=source.rel_path,
                    line=finding.line,
                )
            )
