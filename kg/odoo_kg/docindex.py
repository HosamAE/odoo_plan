"""Building the v7 layer: documents, tests, migrations, processes, conventions.

Three evidence kinds, and what each is worth
--------------------------------------------
``documented``
    A document says it, in words, at a line this graph names. The strongest
    kind here -- and still only a claim by whoever wrote the document, which
    may be out of date with the code beside it.

``tested``
    A test names a subject, statically. It is **not** coverage. A test that
    mentions ``sale.order`` may assert nothing about it, and a model no test
    mentions may be exercised through a fixture. Both directions are wrong to
    infer, and every answer says so.

``observed``
    A structure recurs across modules. The claim is arithmetic -- *this many of
    those* -- and nothing more. An observed pattern is never a rule, and this
    module refuses to promote one into a rule no matter how universal it looks.

How a documented process is allowed to exist
---------------------------------------------
Only from a line somebody actually wrote as a flow. A chain of arrows becomes a
process when either

* it binds to a v6 workflow in the same module, by matching at least two of its
  steps against that workflow's state values or Selection labels -- which is
  why ``To Start -> Appraisal Sent -> Done`` binds and a sentence containing an
  arrow does not; or
* the author labelled the line as a flow (``Statusflöde:``, ``lifecycle:``), in
  which case it is recorded as ``incomplete``: a described process this build
  could not tie to code.

Nothing is created from name similarity alone, and a step whose label matches
no state in a bound workflow is recorded as a **conflict** rather than dropped.
The graph reports that the document and the code disagree, and names both.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field as dc_field

from .docread import (
    DocReadError, announces_a_process, normalise_label, read_document,
)
from .store import (
    BusinessProcessRow, ConventionRow, DocSectionRow, DocumentRow, GraphEdge,
    MigrationRow, NodeDiagnostic, ProcessStepRow, TestCaseRow, TestClassRow,
    TestFileRow, stable_id,
)
from .testread import (
    looks_like_a_test_path, read_javascript_tests, read_migration_path,
    read_python_tests,
)

EVIDENCE_DOCUMENTED = "documented"
EVIDENCE_TESTED = "tested"
EVIDENCE_OBSERVED = "observed"

CONFIDENCE_FOR_EVIDENCE = {
    EVIDENCE_DOCUMENTED: "derived",
    EVIDENCE_TESTED: "derived",
    EVIDENCE_OBSERVED: "heuristic",
}

DOC_SUFFIXES = {".md": "md", ".rst": "rst", ".txt": "txt"}

#: A process must match at least this many of its steps to a workflow before
#: the binding is asserted. One match is a coincidence; two in order is not.
MIN_WORKFLOW_MATCHES = 2

#: A structure seen in at least this share of modules is worth reporting as an
#: observed pattern. Below it, the sample says nothing.
OBSERVED_THRESHOLD = 0.30

#: Provenance for a subject edge, one value per subject kind.
#:
#: ``section_documents_subject`` is the only polymorphic edge table in the
#: graph: a section can point at a model, an XML ID, a route, a workflow or a
#: method. An edge's identity is
#: ``sha1(project_id | table | src_id | dst_unresolved | provenance)`` and does
#: **not** include ``dst_kind``, so two subjects of different kinds with the
#: same textual name are the same edge, and the one written second silently
#: replaces the first.
#:
#: That is not hypothetical. testing19 has a model named ``hx.ai.feature`` and
#: a method ``feature`` on the model ``hx.ai``; both render as the string
#: ``hx.ai.feature``, and the method edge was overwriting the model edge in the
#: two sections that mention it. The graph then said those sections document a
#: method and had forgotten they document the model.
#:
#: Naming the kind in the provenance separates the identities. It is done here
#: rather than by adding ``dst_kind`` to ``GraphEdge.edge_id`` because that
#: function is shared by every layer: changing it would change every edge id in
#: the graph, and every digest with them, to fix one table.
#: How a subject edge was arrived at. One value per *binding mode*, not per
#: target kind, because the four ways a method reference can be reached are not
#: worth the same:
#:
#: ``method.qualified``
#:     The document wrote ``model.method``. Both halves are in the text and both
#:     are in the graph, so the binding is as good as a static reference gets.
#: ``method.unique_bare_name``
#:     The document wrote a bare method name that exactly one model in the
#:     project defines. Plausible, and a guess: the document never named the
#:     model, and this build inferred it from the fact that nothing else has
#:     that method today.
#: ``method.ambiguous_bare_name``
#:     A bare name several models define. The reference is real and the target
#:     is unknown, so the edge is recorded unresolved and names the candidates.
#: ``method.unresolved``
#:     A name shaped like an Odoo method that no model in this graph defines.
#:     Recorded so documentation drift is visible rather than silent.
#:
#: "Documented" says only that the text is in the document. It never says the
#: link to a code object is certain, and these four values are how the
#: difference is kept legible.
SUBJECT_PROVENANCE = {
    "model": "document.text.model",
    "route": "document.text.route",
    "xmlid": "document.text.xmlid",
    "workflow": "document.text.workflow",
    "method": "document.text.method.qualified",
    "method_unique_bare_name": "document.text.method.unique_bare_name",
    "method_ambiguous_bare_name": "document.text.method.ambiguous_bare_name",
    "method_unresolved": "document.text.method.unresolved",
}


@dataclass
class DocLayer:
    documents: list[DocumentRow] = dc_field(default_factory=list)
    sections: list[DocSectionRow] = dc_field(default_factory=list)
    test_files: list[TestFileRow] = dc_field(default_factory=list)
    test_classes: list[TestClassRow] = dc_field(default_factory=list)
    test_cases: list[TestCaseRow] = dc_field(default_factory=list)
    migrations: list[MigrationRow] = dc_field(default_factory=list)
    processes: list[BusinessProcessRow] = dc_field(default_factory=list)
    steps: list[ProcessStepRow] = dc_field(default_factory=list)
    conventions: list[ConventionRow] = dc_field(default_factory=list)
    edges: list[GraphEdge] = dc_field(default_factory=list)
    diagnostics: list[NodeDiagnostic] = dc_field(default_factory=list)
    counts: dict[str, int] = dc_field(default_factory=dict)


@dataclass
class DocSource:
    module: str | None
    rel_path: str
    file_key: str
    disk_path: str


@dataclass
class GraphFacts:
    """What the earlier layers already hold, for resolving references."""

    models: set = dc_field(default_factory=set)
    routes: dict = dc_field(default_factory=dict)      # path -> route_id
    xmlids: set = dc_field(default_factory=set)
    modules: set = dc_field(default_factory=set)
    #: (model, field) -> workflow_id
    workflows: dict = dc_field(default_factory=dict)
    #: workflow_id -> module
    workflow_module: dict = dc_field(default_factory=dict)
    #: workflow_id -> {normalised label or value: (state_id, value, kind)}
    workflow_states: dict = dc_field(default_factory=dict)
    #: model -> {method_name: method_id}
    methods: dict = dc_field(default_factory=dict)


class _Builder:
    def __init__(self, project_id, facts):
        self.project_id = project_id
        self.facts = facts
        self.layer = DocLayer()
        self.counts = self.layer.counts
        #: Built once on first use: method name -> every model that defines it.
        #: Deriving it per section would walk every method in the project 450
        #: times. Rebuilt from scratch on each index run, which is what lets a
        #: binding stop being unique when a second model claims the name.
        self.method_owners = None

    def bump(self, key, amount=1):
        self.counts[key] = self.counts.get(key, 0) + amount

    def edge(self, table, src_kind, src_id, dst_kind, dst_id, target,
             provenance, evidence, rel_path, line, binding=None,
             confidence=None):
        """One edge. ``confidence`` overrides what the evidence kind implies.

        The override exists for one reason: a documented reference is normally
        ``derived`` -- somebody wrote the name and the name matches -- but a
        bare method name bound by uniqueness is a guess about which model was
        meant. That has to be ``heuristic`` however strong the evidence kind
        sounds, and the two must not be conflated.
        """
        if binding is None:
            binding = "resolved" if dst_id else "unresolved"
        self.layer.edges.append(GraphEdge(
            table=table, src_kind=src_kind, src_id=src_id, dst_kind=dst_kind,
            dst_id=dst_id, dst_unresolved=target, binding=binding,
            confidence=confidence or CONFIDENCE_FOR_EVIDENCE[evidence],
            provenance=provenance, src_file=rel_path, src_line=line,
        ))

    def note(self, severity, category, message, node_kind, node_id,
             rel_path=None, line=None):
        self.layer.diagnostics.append(NodeDiagnostic(
            severity, category, message, node_kind, node_id, rel_path, line
        ))


def build_doc_layer(project_id, sources, read_bytes, facts,
                    module_names) -> DocLayer:
    """Build the whole v7 layer from already-scanned sources."""
    builder = _Builder(project_id, facts)
    _index_documents(builder, sources, read_bytes)
    _index_tests(builder, sources, read_bytes)
    _index_migrations(builder, sources)
    _index_conventions(builder, sources, module_names)
    _link_tests_to_steps(builder)
    _finalise(builder)
    return builder.layer


# --------------------------------------------------------------------------
# documents and sections
# --------------------------------------------------------------------------


def _index_documents(builder, sources, read_bytes) -> None:
    for source in sources:
        suffix = os.path.splitext(source.rel_path)[1].lower()
        kind = DOC_SUFFIXES.get(suffix)
        if kind is None:
            continue
        try:
            raw = read_bytes(source.disk_path)
        except OSError:
            continue
        try:
            document = read_document(raw, kind=kind)
        except DocReadError as exc:
            builder.bump("documents_unreadable")
            builder.note("warning", "document_unreadable",
                         f"{source.rel_path}: {exc}", "module",
                         source.module or "<root>", source.rel_path, None)
            continue

        doc_id = stable_id(builder.project_id, "document", source.file_key)
        builder.layer.documents.append(DocumentRow(
            doc_id=doc_id, module=source.module, rel_path=source.rel_path,
            kind=kind, title=document.title,
            section_count=len(document.sections),
            line_count=document.line_count, char_count=document.char_count,
            injection_flags=",".join(document.injection_flags) or None,
            evidence=EVIDENCE_DOCUMENTED,
        ))
        builder.bump("documents")
        if document.injection_flags:
            builder.bump("documents_with_injection_like_text")
            builder.note(
                "info", "document_contains_instruction_like_text",
                f"{source.rel_path}: contains "
                f"{', '.join(document.injection_flags)}. The text is stored as "
                "data and is never followed; this is a note to whoever reads "
                "it, not a defect in the document.",
                "document", doc_id, source.rel_path, None,
            )

        if source.module:
            resolved = source.module in builder.facts.modules
            builder.edge(
                "document_in_module", "document", doc_id, "module",
                source.module if resolved else None, source.module,
                "path.module", EVIDENCE_DOCUMENTED, source.rel_path, 1,
                binding="resolved" if resolved else "external",
            )

        for section in document.sections:
            _index_section(builder, source, doc_id, section)

        _index_chains(builder, source, doc_id, document)


def _index_section(builder, source, doc_id, section) -> None:
    section_id = stable_id(
        builder.project_id, "doc_section", source.file_key, section.anchor
    )
    builder.layer.sections.append(DocSectionRow(
        section_id=section_id, doc_id=doc_id, module=source.module,
        rel_path=source.rel_path, level=section.level, title=section.title,
        anchor=section.anchor, line_start=section.line_start,
        line_end=section.line_end, excerpt=section.excerpt,
        excerpt_truncated=section.excerpt_truncated,
        char_count=section.char_count,
        injection_flags=",".join(section.injection_flags) or None,
        evidence=EVIDENCE_DOCUMENTED,
    ))
    builder.bump("doc_sections")
    builder.edge(
        "section_in_document", "doc_section", section_id, "document", doc_id,
        doc_id, "markdown.heading", EVIDENCE_DOCUMENTED, source.rel_path,
        section.line_start,
    )

    _index_subjects(builder, source, section_id, section)


#: A bare method name has to earn its edge. ``create`` and ``action_confirm``
#: appear on many models and in ordinary prose; a name has to be long, shaped
#: like an Odoo method, and defined on exactly one model in the whole project
#: before an unqualified mention counts. A qualified ``model.method`` needs
#: none of this, because it is already unambiguous.
MIN_BARE_METHOD_CHARS = 10
BARE_METHOD_PREFIXES = ("action_", "button_", "_compute_", "_onchange_", "_cron_")


#: Tokens in document text that are shaped like an Odoo method name. Built from
#: the prefix list so there is one definition of "distinctive enough".
#:
#: The lookbehind refuses a match preceded by a dot, so the method half of a
#: qualified ``model.action_confirm`` is not also read as a bare name -- the
#: qualified pass has already bound it, and better.
_BARE_METHOD_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.])("
    + "|".join(re.escape(prefix) for prefix in BARE_METHOD_PREFIXES)
    + r")([A-Za-z0-9_]+)"
)


def bare_method_candidates(text: str) -> set[str]:
    """Names in ``text`` shaped like an Odoo method, distinctive enough to use.

    Extracted from the text rather than matched against every known method, so
    a name the graph has never heard of is found too. That is the whole point
    of the ``unresolved`` mode: a document naming ``action_send_reminder`` when
    no model defines it is documentation drift, and drift nobody can see is
    drift nobody fixes.
    """
    found = set()
    for prefix, rest in _BARE_METHOD_TOKEN.findall(text):
        name = prefix + rest
        if len(name) >= MIN_BARE_METHOD_CHARS:
            found.add(name)
    return found


def mentioned_as_code(name: str, text: str) -> bool:
    """Whether ``name`` appears written as code rather than as a word.

    Required only for the ``unresolved`` mode, and the asymmetry is deliberate.
    When the graph already holds a method by that name, the graph itself
    corroborates that the token is a method name. When nothing holds it, the
    only evidence is the shape of the token -- and a shape is thin evidence.

    testing19 showed why. The three unresolved candidates it produced were all
    ``button_text``, a colour key in a JSON example in an API document. It is
    ten characters, it starts with ``button_``, and it is not a method. A claim
    that a document references a method nobody defines has to rest on more than
    that, so it rests on the author having written it as a call or in a code
    span. Prose mentions of names the graph does not hold are dropped rather
    than recorded weakly: an edge nobody can act on is worse than no edge.
    """
    escaped = re.escape(name)
    return re.search(rf"`{escaped}`|{escaped}\s*\(", text) is not None


def method_owners(facts) -> dict[str, list[tuple[str, str]]]:
    """method_name -> [(model, method_id), ...], every model that defines it.

    Recomputed on every index run, which is what makes a binding correct itself:
    a name bound today because one model defines it becomes ambiguous the moment
    a second model defines it, and the next rebuild records it that way instead
    of continuing to point at the old target.
    """
    owners: dict[str, list[tuple[str, str]]] = {}
    for model, methods in facts.methods.items():
        for name, method_id in methods.items():
            owners.setdefault(name, []).append((model, method_id))
    for candidates in owners.values():
        candidates.sort()
    return owners


def _index_subjects(builder, source, section_id, section) -> None:
    """What the section talks about, read from the whole section.

    Only exact names the graph already holds count: a section that mentions
    "booking" is not about ``hx.conference.booking``.

    The text matched here is ``section.search_text`` -- the entire section,
    normalised -- and not the excerpt. Matching the excerpt meant the graph
    could only see references in the first 480 characters, so what a document
    was recorded as being about depended on a display bound. The text is used
    here and nowhere else: it is not stored, not returned, not logged.

    It is untrusted content. It is compared against, never interpreted: no
    expression in it is evaluated, no link in it is followed, and text shaped
    like an instruction is flagged on the section rather than obeyed.
    """
    body = section.search_text or f"{section.title}\n{section.excerpt}"

    for model in builder.facts.models:
        if len(model) > 6 and model in body:
            builder.edge(
                "section_documents_subject", "doc_section", section_id, "model",
                model, model, SUBJECT_PROVENANCE["model"], EVIDENCE_DOCUMENTED,
                source.rel_path, section.line_start,
            )
            builder.bump("section_documents_model")
    for path, route_id in builder.facts.routes.items():
        if len(path) > 4 and path in body:
            builder.edge(
                "section_documents_subject", "doc_section", section_id, "route",
                route_id, path, SUBJECT_PROVENANCE["route"], EVIDENCE_DOCUMENTED,
                source.rel_path, section.line_start,
            )
            builder.bump("section_documents_route")
    for xmlid in builder.facts.xmlids:
        if len(xmlid) > 10 and xmlid in body:
            builder.edge(
                "section_documents_subject", "doc_section", section_id, "xmlid",
                xmlid, xmlid, SUBJECT_PROVENANCE["xmlid"], EVIDENCE_DOCUMENTED,
                source.rel_path, section.line_start,
            )
            builder.bump("section_documents_xmlid")

    # A workflow is named the way the tools name it: model.field. Nothing
    # weaker -- "the state field" is not a reference to a state machine.
    for (model, field), workflow_id in builder.facts.workflows.items():
        dotted = f"{model}.{field}"
        if len(dotted) > 6 and dotted in body:
            builder.edge(
                "section_documents_subject", "doc_section", section_id,
                "workflow", workflow_id, dotted, SUBJECT_PROVENANCE["workflow"],
                EVIDENCE_DOCUMENTED, source.rel_path, section.line_start,
            )
            builder.bump("section_documents_workflow")

    _index_method_subjects(builder, source, section_id, section, body)


def _index_method_subjects(builder, source, section_id, section, body) -> None:
    """Method references, each recorded as the kind of reference it is.

    Four modes, and they are not interchangeable. A qualified ``model.method``
    is a reference the author spelled out. A bare name bound by uniqueness is
    this build guessing which model was meant. An ambiguous name is a real
    reference with an unknown target. A name nothing defines is drift. Only the
    first is strong, and the graph says which is which rather than presenting
    four different things as one edge kind.
    """
    if builder.method_owners is None:
        builder.method_owners = method_owners(builder.facts)

    # 1. Qualified: the document named the model as well as the method.
    seen_methods: set[str] = set()
    named_bare: set[str] = set()
    for model, methods in builder.facts.methods.items():
        for name, method_id in methods.items():
            dotted = f"{model}.{name}"
            if dotted in body and method_id not in seen_methods:
                seen_methods.add(method_id)
                named_bare.add(name)
                builder.edge(
                    "section_documents_subject", "doc_section", section_id,
                    "method", method_id, dotted, SUBJECT_PROVENANCE["method"],
                    EVIDENCE_DOCUMENTED, source.rel_path, section.line_start,
                    confidence="derived",
                )
                builder.bump("section_documents_method_qualified")

    # 2-4. Bare names, classified by how many models define them.
    for name in sorted(bare_method_candidates(body)):
        owners = builder.method_owners.get(name, [])
        if name in named_bare:
            # The document already spelled this one out as model.method in this
            # same section. Recording the bare mention again would add a second,
            # weaker edge for a reference the author disambiguated themselves.
            continue
        if len(owners) == 1:
            model, method_id = owners[0]
            if method_id in seen_methods:
                continue
            seen_methods.add(method_id)
            builder.edge(
                "section_documents_subject", "doc_section", section_id,
                "method", method_id, f"{model}.{name}",
                SUBJECT_PROVENANCE["method_unique_bare_name"],
                EVIDENCE_DOCUMENTED, source.rel_path, section.line_start,
                confidence="heuristic",
            )
            builder.bump("section_documents_method_by_bare_name")
        elif owners:
            builder.edge(
                "section_documents_subject", "doc_section", section_id,
                "method", None,
                f"unknown:method_on_several_models:{name}",
                SUBJECT_PROVENANCE["method_ambiguous_bare_name"],
                EVIDENCE_DOCUMENTED, source.rel_path, section.line_start,
                binding="unresolved", confidence="heuristic",
            )
            builder.bump("section_documents_method_ambiguous")
        elif mentioned_as_code(name, body):
            builder.edge(
                "section_documents_subject", "doc_section", section_id,
                "method", None,
                f"unknown:method_not_in_project:{name}",
                SUBJECT_PROVENANCE["method_unresolved"],
                EVIDENCE_DOCUMENTED, source.rel_path, section.line_start,
                binding="unresolved", confidence="heuristic",
            )
            builder.bump("section_documents_method_unresolved")
        else:
            builder.bump("bare_name_not_written_as_code")


# --------------------------------------------------------------------------
# documented processes
# --------------------------------------------------------------------------


def _index_chains(builder, source, doc_id, document) -> None:
    anchors = {s.anchor: s for s in document.sections}
    for chain in document.chains:
        workflow_id, matches, conflicts = _match_workflow(builder, source, chain)
        section = anchors.get(chain.section_anchor)
        announced = announces_a_process(chain, section.title if section else "")

        if workflow_id is None and not announced:
            # A sentence that happens to contain an arrow. Recording it would
            # manufacture a process nobody described.
            builder.bump("arrow_lines_rejected_as_prose")
            continue

        status = "documented" if workflow_id else "incomplete"
        name = chain.label_prefix.strip() or " -> ".join(chain.steps)[:80]
        process_id = stable_id(
            builder.project_id, "business_process", source.file_key,
            chain.section_anchor or "", name, len(chain.steps),
        )
        section_id = (
            stable_id(builder.project_id, "doc_section", source.file_key,
                      section.anchor)
            if section else None
        )
        builder.layer.processes.append(BusinessProcessRow(
            process_id=process_id, name=name[:120], module=source.module,
            doc_id=doc_id, section_id=section_id, status=status,
            step_count=len(chain.steps), matched_steps=len(matches),
            workflow_id=workflow_id, conflict_count=len(conflicts),
            evidence=EVIDENCE_DOCUMENTED, rel_path=source.rel_path,
            line=chain.line,
        ))
        builder.bump(f"processes_{status}")

        builder.edge(
            "process_from_document", "business_process", process_id, "document",
            doc_id, doc_id, "document.arrow_chain", EVIDENCE_DOCUMENTED,
            source.rel_path, chain.line,
        )
        if workflow_id:
            builder.edge(
                "process_for_workflow", "business_process", process_id,
                "workflow", workflow_id, workflow_id,
                "document.state_label_match", EVIDENCE_DOCUMENTED,
                source.rel_path, chain.line,
            )

        for position, label in enumerate(chain.steps):
            normalised = normalise_label(label)
            found = matches.get(position)
            step_id = stable_id(
                builder.project_id, "process_step", process_id, position
            )
            builder.layer.steps.append(ProcessStepRow(
                step_id=step_id, process_id=process_id, position=position,
                label=label[:120], normalised=normalised,
                state_id=found[0] if found else None,
                state_value=found[1] if found else None,
                match_kind=found[2] if found else None,
                evidence=EVIDENCE_DOCUMENTED, rel_path=source.rel_path,
                line=chain.line,
            ))
            builder.edge(
                "process_has_step", "business_process", process_id,
                "process_step", step_id, str(position),
                "document.arrow_chain", EVIDENCE_DOCUMENTED,
                source.rel_path, chain.line,
            )
            builder.edge(
                "step_supported_by", "process_step", step_id, "doc_section",
                section_id, section_id or source.rel_path,
                "document.arrow_chain", EVIDENCE_DOCUMENTED,
                source.rel_path, chain.line,
                binding="resolved" if section_id else "unresolved",
            )
            if found:
                builder.edge(
                    "step_supported_by", "process_step", step_id,
                    "workflow_state", found[0], found[1],
                    f"document.state_{found[2]}_match", EVIDENCE_DOCUMENTED,
                    source.rel_path, chain.line,
                )
                builder.bump("steps_matched_to_state")

        if conflicts and workflow_id:
            builder.note(
                "warning", "documented_step_not_in_workflow",
                f"{source.rel_path}:{chain.line}: the document names "
                f"{', '.join(repr(c) for c in conflicts)}, which the workflow "
                f"bound to this process does not declare. The document and the "
                "code disagree; both are recorded and neither is preferred.",
                "business_process", process_id, source.rel_path, chain.line,
            )
            builder.bump("process_conflicts")
        if status == "incomplete":
            builder.note(
                "info", "process_not_bound_to_code",
                f"{source.rel_path}:{chain.line}: {name[:60]!r} is described as "
                "a flow, but its steps could not be matched to any workflow "
                "this build indexed. Recorded as incomplete rather than as a "
                "verified process.",
                "business_process", process_id, source.rel_path, chain.line,
            )


def _match_workflow(builder, source, chain):
    """Which workflow, if any, this chain describes.

    A chain binds only to a workflow declared in the *same module* as the
    document, and only when at least two of its steps line up with that
    workflow's state values or Selection labels. Matching across modules would
    be exactly the name-similarity guess this layer must not make.
    """
    if not source.module:
        return None, {}, []
    candidates = [
        workflow_id
        for workflow_id, module in builder.facts.workflow_module.items()
        if module == source.module
    ]
    best = (None, {}, [])
    for workflow_id in candidates:
        states = builder.facts.workflow_states.get(workflow_id, {})
        if not states:
            continue
        matches = {}
        unmatched = []
        for position, label in enumerate(chain.steps):
            key = normalise_label(label)
            found = states.get(key)
            if found:
                matches[position] = found
            else:
                unmatched.append(label)
        if len(matches) >= MIN_WORKFLOW_MATCHES and len(matches) > len(best[1]):
            best = (workflow_id, matches, unmatched)
    return best


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------


def _index_tests(builder, sources, read_bytes) -> None:
    for source in sources:
        lowered = source.rel_path.lower()
        is_python = lowered.endswith(".py")
        is_js = lowered.endswith((".js", ".ts"))
        if not (is_python or is_js) or not looks_like_a_test_path(source.rel_path):
            continue
        try:
            raw = read_bytes(source.disk_path)
        except OSError:
            continue

        found = read_python_tests(raw) if is_python else read_javascript_tests(raw)
        if is_python and not found.classes and not found.parsed:
            builder.bump("test_files_unparsed")
        if is_python and not found.classes and found.parsed:
            # A `tests/__init__.py` or a helper: not a test file.
            continue
        if is_js and not found.framework:
            continue

        file_id = stable_id(builder.project_id, "test_file", source.file_key)
        case_count = sum(len(k.cases) for k in found.classes)
        builder.layer.test_files.append(TestFileRow(
            test_file_id=file_id, module=source.module,
            rel_path=source.rel_path, language=found.language,
            framework=found.framework, class_count=len(found.classes),
            case_count=case_count,
            parse_state="ok" if found.parsed else "unparsed",
            evidence=EVIDENCE_TESTED,
        ))
        builder.bump(f"test_files_{found.language}")

        if source.module:
            resolved = source.module in builder.facts.modules
            builder.edge(
                "test_in_module", "test_file", file_id, "module",
                source.module if resolved else None, source.module,
                "path.module", EVIDENCE_TESTED, source.rel_path, 1,
                binding="resolved" if resolved else "external",
            )

        for klass in found.classes:
            class_id = stable_id(
                builder.project_id, "test_class", file_id, klass.name
            )
            builder.layer.test_classes.append(TestClassRow(
                test_class_id=class_id, test_file_id=file_id,
                module=source.module, class_name=klass.name,
                bases=",".join(klass.bases) or None,
                tags=",".join(klass.tags) or None,
                case_count=len(klass.cases), rel_path=source.rel_path,
                line=klass.line, evidence=EVIDENCE_TESTED,
            ))
            builder.bump("test_classes")
            builder.edge(
                "test_class_in_file", "test_class", class_id, "test_file",
                file_id, file_id, "python.class", EVIDENCE_TESTED,
                source.rel_path, klass.line,
            )
            for case in klass.cases:
                case_id = stable_id(
                    builder.project_id, "test_case", class_id, case.name
                )
                builder.layer.test_cases.append(TestCaseRow(
                    test_case_id=case_id, test_class_id=class_id,
                    module=source.module, name=case.name,
                    rel_path=source.rel_path, line=case.line,
                    evidence=EVIDENCE_TESTED,
                ))
                builder.bump("test_cases")
                builder.edge(
                    "test_case_in_class", "test_case", case_id, "test_class",
                    class_id, class_id, "python.method", EVIDENCE_TESTED,
                    source.rel_path, case.line,
                )

        _link_test_references(builder, source, file_id, found)


def _link_test_references(builder, source, file_id, found) -> None:
    for reference in found.references:
        if reference.kind == "route":
            route_id = builder.facts.routes.get(reference.value)
            if route_id:
                builder.edge(
                    "test_references_subject", "test_file", file_id, "route",
                    route_id, reference.value, "test.literal", EVIDENCE_TESTED,
                    source.rel_path, reference.line,
                )
                builder.bump("test_references_route")
            continue
        if reference.kind == "model_or_xmlid":
            value = reference.value
            if value in builder.facts.models:
                builder.edge(
                    "test_references_subject", "test_file", file_id, "model",
                    value, value, "test.literal", EVIDENCE_TESTED,
                    source.rel_path, reference.line,
                )
                builder.bump("test_references_model")
            elif value in builder.facts.xmlids:
                builder.edge(
                    "test_references_subject", "test_file", file_id, "xmlid",
                    value, value, "test.literal", EVIDENCE_TESTED,
                    source.rel_path, reference.line,
                )
                builder.bump("test_references_xmlid")
            continue
        if reference.kind == "method":
            owners = [
                (model, names[reference.value])
                for model, names in builder.facts.methods.items()
                if reference.value in names
            ]
            if len(owners) == 1:
                model, method_id = owners[0]
                builder.edge(
                    "test_references_subject", "test_file", file_id, "method",
                    method_id, f"{model}.{reference.value}", "test.call",
                    EVIDENCE_TESTED, source.rel_path, reference.line,
                )
                builder.bump("test_references_method")
            elif owners:
                # The same method name on several models. Which one the test
                # calls depends on the record it holds, which this build does
                # not track, so no edge is claimed.
                builder.bump("test_method_reference_ambiguous")


def _link_tests_to_steps(builder) -> None:
    """A test supports a step when it names the state that step matched.

    Deliberately narrow. The test must name the *state value* the step was
    matched to, and belong to the same module as the process. Anything looser
    would turn a mention into a claim of coverage.
    """
    by_module = {}
    for row in builder.layer.test_files:
        by_module.setdefault(row.module, []).append(row)

    steps_by_process = {}
    for step in builder.layer.steps:
        steps_by_process.setdefault(step.process_id, []).append(step)

    literals = {}
    for edge in builder.layer.edges:
        if edge.table == "test_references_subject":
            literals.setdefault(edge.src_id, set()).add(edge.dst_unresolved)

    for process in builder.layer.processes:
        if not process.workflow_id or not process.module:
            continue
        for test_file in by_module.get(process.module, ()):
            named = literals.get(test_file.test_file_id, set())
            for step in steps_by_process.get(process.process_id, ()):
                if step.state_value and step.state_value in named:
                    builder.edge(
                        "test_supports_step", "test_file",
                        test_file.test_file_id, "process_step", step.step_id,
                        step.label, "test.literal.state", EVIDENCE_TESTED,
                        test_file.rel_path, 1,
                    )
                    builder.bump("steps_supported_by_test")


# --------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------


def _index_migrations(builder, sources) -> None:
    for source in sources:
        if not source.rel_path.lower().endswith(".py"):
            continue
        migration = read_migration_path(source.rel_path)
        if migration is None or not source.module:
            continue
        migration_id = stable_id(
            builder.project_id, "migration", source.file_key
        )
        builder.layer.migrations.append(MigrationRow(
            migration_id=migration_id, module=source.module,
            version=migration.version, phase=migration.phase,
            script=migration.script, rel_path=source.rel_path,
            evidence=EVIDENCE_OBSERVED,
        ))
        builder.bump("migrations")
        builder.bump(f"migration_phase[{migration.phase}]")
        resolved = source.module in builder.facts.modules
        builder.edge(
            "migration_in_module", "migration", migration_id, "module",
            source.module if resolved else None, source.module,
            "path.migrations", EVIDENCE_OBSERVED, source.rel_path, 1,
            binding="resolved" if resolved else "external",
        )


# --------------------------------------------------------------------------
# module conventions
# --------------------------------------------------------------------------

#: What Odoo itself will not load a module without. Not a choice of any project.
REQUIRED_BY_ODOO = {
    "manifest": ("__manifest__.py", "a module without a manifest is not a "
                 "module; Odoo will not see it"),
    "package_init": ("__init__.py", "a module is a Python package; without "
                     "__init__.py nothing in it imports"),
}

#: Directories worth counting. Presence is a fact; obligation is not.
COUNTED_DIRECTORIES = (
    ("models", "models/", "model definitions live in models/"),
    ("views", "views/", "view XML lives in views/"),
    ("security", "security/", "access rules live in security/"),
    ("data", "data/", "seed and configuration records live in data/"),
    ("tests", "tests/", "tests live in a tests/ package"),
    ("controllers", "controllers/", "HTTP controllers live in controllers/"),
    ("static_src", "static/src/", "frontend assets live in static/src/"),
    ("migrations", "migrations/", "upgrade scripts live in migrations/<version>/"),
    ("wizard", "wizard/", "transient wizards live in wizard/"),
    ("report", "report/", "report definitions live in report/"),
    ("i18n", "i18n/", "translations live in i18n/"),
    ("static_description", "static/description/",
     "the module icon and description live in static/description/"),
)

COUNTED_FILES = (
    ("access_csv", "security/ir.model.access.csv",
     "model access is declared in security/ir.model.access.csv"),
    ("readme", "README.md", "the module carries a README.md"),
)

def name_prefix(module_names) -> str:
    """The "<word>_" most project modules start with ("" when none is shared).

    Naming is counted, never asserted: the prefix is read from the modules, not chosen here. A word counts when at
    least two modules start with it.
    """
    counts: dict = {}
    for name in module_names:
        head, sep, _rest = str(name).partition("_")
        if sep and head:
            counts[head + "_"] = counts.get(head + "_", 0) + 1
    best = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return best[0][0] if best and best[0][1] >= 2 else ""


def _index_conventions(builder, sources, module_names) -> None:
    total = len(module_names)
    if not total:
        return
    owned = {name: set() for name in module_names}
    for source in sources:
        if source.module in owned:
            owned[source.module].add(source.rel_path)

    def record(key, category, statement, holders, status):
        convention_id = stable_id(builder.project_id, "convention", key)
        share = len(holders) / total if total else 0.0
        builder.layer.conventions.append(ConventionRow(
            convention_id=convention_id, key=key, category=category,
            statement=statement, status=status, module_count=len(holders),
            total_modules=total, share=round(share, 4),
            source_rel_path=None, source_line=None,
            evidence=EVIDENCE_OBSERVED if status != "required_by_odoo"
            else EVIDENCE_OBSERVED,
        ))
        builder.bump(f"conventions_{status}")
        for module in sorted(holders):
            builder.edge(
                "convention_observed_in_module", "convention", convention_id,
                "module", module if module in builder.facts.modules else None,
                module, "repository.count", EVIDENCE_OBSERVED,
                f"{module}/__manifest__.py", 1,
                binding="resolved" if module in builder.facts.modules
                else "external",
            )
        return convention_id

    for key, (filename, statement) in REQUIRED_BY_ODOO.items():
        holders = {
            module for module, paths in owned.items()
            if f"{module}/{filename}" in paths
        }
        record(key, "structure", statement, holders, "required_by_odoo")

    for key, directory, statement in COUNTED_DIRECTORIES:
        holders = {
            module for module, paths in owned.items()
            if any(path.startswith(f"{module}/{directory}") for path in paths)
        }
        share = len(holders) / total
        record(key, "structure", statement, holders,
               "observed_pattern" if share >= OBSERVED_THRESHOLD
               else "recommendation")

    for key, filename, statement in COUNTED_FILES:
        holders = {
            module for module, paths in owned.items()
            if f"{module}/{filename}" in paths
        }
        share = len(holders) / total
        record(key, "structure", statement, holders,
               "observed_pattern" if share >= OBSERVED_THRESHOLD
               else "recommendation")

    prefix = name_prefix(module_names)
    prefixed = {m for m in module_names if prefix and m.startswith(prefix)}
    record("name_prefix", "naming",
           f"a project module's technical name starts with {prefix!r}" if prefix
           else "a project module's technical name shares a common prefix",
           prefixed,
           "observed_pattern" if len(prefixed) / total >= OBSERVED_THRESHOLD
           else "recommendation")


#: The literal token a section must contain before it counts as mentioning a
#: convention. A path or a filename, not a word: "models" appears in every
#: other paragraph about Odoo, `models/` is a statement about layout.
CONVENTION_TOKENS = {
    "manifest": "__manifest__.py",
    "package_init": "__init__.py",
    "models": "models/",
    "views": "views/",
    "security": "security/",
    "data": "data/",
    "tests": "tests/",
    "controllers": "controllers/",
    "static_src": "static/src/",
    "migrations": "migrations/",
    "wizard": "wizard/",
    "report": "report/",
    "i18n": "i18n/",
    "static_description": "static/description/",
    "access_csv": "ir.model.access.csv",
    "readme": "README.md",
    "name_prefix": None,   # the project's own prefix, read from its modules when the documents are linked
}


def _link_conventions_to_documents(builder) -> None:
    """Point at documents that mention a convention. Never promote one.

    A section that names ``models/`` is evidence worth surfacing, and this
    records it as an edge so a person can go and read it. What it deliberately
    does **not** do is change the convention's status.

    An earlier version of this function did promote: a section whose text
    contained the first word of a convention's statement turned an
    `observed_pattern` into a `documented_rule`. That produced six
    "organisation rules" nobody had written -- "models/" appearing in a porting plan
    is not a rule that models live in models/. Promotion is a human decision,
    and the milestone says so: a recommendation may never be presented as an
    existing organisational rule without a documented source *and* later human
    approval. This function supplies the source; the approval is not its to
    give.
    """
    structure_sections = [
        section for section in builder.layer.sections
        if re.search(r"\b(struktur|structure|konvention|convention|"
                     r"modul(?:er|struktur)?|module layout|katalog|directory|"
                     r"layout|standard|riktlinj|guideline)\b",
                     section.title, re.IGNORECASE)
    ]
    if not structure_sections:
        return
    for convention in builder.layer.conventions:
        token = CONVENTION_TOKENS.get(convention.key)
        if convention.key == "name_prefix":
            token = name_prefix(builder.facts.modules)
        if not token:
            continue
        for section in structure_sections:
            body = f"{section.title}\n{section.excerpt}"
            if token.lower() not in body.lower():
                continue
            builder.edge(
                "convention_documented_in_section", "convention",
                convention.convention_id, "doc_section", section.section_id,
                section.anchor, "document.mentions_token", EVIDENCE_DOCUMENTED,
                section.rel_path, section.line_start,
            )
            builder.bump("conventions_with_a_document_mentioning_them")
            builder.note(
                "info", "convention_mentioned_in_a_document",
                f"{section.rel_path}:{section.line_start} mentions {token!r}. "
                f"That is a place to look, not a rule: {convention.key!r} is "
                f"still {convention.status!r} and only a person can decide "
                "whether this document states it as a requirement.",
                "convention", convention.convention_id, section.rel_path,
                section.line_start,
            )
            break


def _finalise(builder) -> None:
    _link_conventions_to_documents(builder)
    layer = builder.layer

    known = {
        "document": {r.doc_id for r in layer.documents},
        "doc_section": {r.section_id for r in layer.sections},
        "test_file": {r.test_file_id for r in layer.test_files},
        "test_class": {r.test_class_id for r in layer.test_classes},
        "test_case": {r.test_case_id for r in layer.test_cases},
        "migration": {r.migration_id for r in layer.migrations},
        "business_process": {r.process_id for r in layer.processes},
        "process_step": {r.step_id for r in layer.steps},
        "convention": {r.convention_id for r in layer.conventions},
    }
    layer.diagnostics[:] = [
        finding for finding in layer.diagnostics
        if finding.node_kind not in known or finding.node_id in known[finding.node_kind]
    ]

    # Sorted so the rows read in the order a person would expect, not in hash
    # order. Both are deterministic; only one is legible when something goes
    # wrong and somebody is reading the table by hand.
    layer.documents.sort(key=lambda row: row.rel_path)
    layer.sections.sort(key=lambda row: (row.rel_path, row.line_start))
    layer.test_files.sort(key=lambda row: row.rel_path)
    layer.test_classes.sort(key=lambda row: (row.rel_path, row.line))
    layer.test_cases.sort(key=lambda row: (row.rel_path, row.line, row.name))
    layer.migrations.sort(key=lambda row: (row.module, row.version, row.script))
    layer.processes.sort(key=lambda row: (row.rel_path, row.line, row.name))
    layer.steps.sort(key=lambda row: (row.process_id, row.position))
    layer.conventions.sort(key=lambda row: row.key)

    counts = builder.counts
    counts["documents"] = len(layer.documents)
    counts["doc_sections"] = len(layer.sections)
    counts["test_files"] = len(layer.test_files)
    counts["test_classes"] = len(layer.test_classes)
    counts["test_cases"] = len(layer.test_cases)
    counts["migrations"] = len(layer.migrations)
    counts["processes"] = len(layer.processes)
    counts["process_steps"] = len(layer.steps)
    counts["conventions"] = len(layer.conventions)
    counts["edges"] = len(layer.edges)
