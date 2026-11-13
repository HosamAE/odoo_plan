"""Read-only queries over the v7 layer: documents, tests, processes, guidance.

Same rules as every query module here: literal statements, ``?`` placeholders,
guarded identifiers, bounded lists that say when they were cut.

Two rules specific to this layer.

**Document text is untrusted content.** Every excerpt that leaves here is
marked as such by the tool layer, and any instruction-like shape found in it is
reported beside it. Nothing in a document is ever acted on.

**A reference is not coverage.** ``test_references_subject`` says a test file
contains a literal. It does not say the subject is tested, and the absence of a
row does not say it is untested.
"""

from __future__ import annotations

MAX_ROWS = 100
MAX_DEPTH = 5

EVIDENCE_DOCUMENTED = "documented"
EVIDENCE_TESTED = "tested"
EVIDENCE_OBSERVED = "observed"

EVIDENCE_MEANING = {
    EVIDENCE_DOCUMENTED: (
        "a document says it, in words, at a line this graph names -- a claim "
        "by whoever wrote it, which may be out of date with the code"
    ),
    EVIDENCE_TESTED: (
        "a test names this subject in its source. Not coverage: the test may "
        "assert nothing about it, and a subject no test names may still be "
        "exercised through a fixture"
    ),
    EVIDENCE_OBSERVED: (
        "a structure recurs across modules. The claim is the arithmetic and "
        "nothing more; it is not a rule"
    ),
}

CONVENTION_STATUS_MEANING = {
    "required_by_odoo": "the framework will not load the module without it",
    "documented_rule": (
        "a document in this repository states it as a rule, and a person "
        "confirmed that reading"
    ),
    "observed_pattern": (
        "counted across modules and found common. Mandated by nobody"
    ),
    "recommendation": (
        "this build's suggestion. Never an existing organisational rule: it "
        "has no documented source and no human approval"
    ),
}

PROCESS_STATUS_MEANING = {
    "documented": (
        "a document describes it and its steps matched a workflow this build "
        "indexed"
    ),
    "incomplete": (
        "a document describes it as a flow, but its steps could not be tied to "
        "any workflow. Recorded as described, not as verified"
    ),
    "unknown": "evidence exists but is not enough to say what the process is",
}

#: How a process relates to code, kept separate from where it came from.
#:
#: Every process in this graph comes from a document -- that is
#: ``source_evidence`` and it is always ``documented``. Whether the document's
#: claim was tied to anything in the code is a different question, and merging
#: the two is the specific misreading this vocabulary exists to prevent: a
#: reader who sees ``evidence: documented`` on a process that was never matched
#: to a workflow could conclude the flow was verified. It was not.
BINDING_STATUS_MEANING = {
    "bound": (
        "the document's steps matched the states of a workflow this build "
        "indexed, in the same module. The document and the code agree on at "
        "least the states named here -- not on behaviour, which this build "
        "never executes"
    ),
    "incomplete": (
        "NOT verified against code. A document describes this as a flow and "
        "the steps are reproduced as written, but no workflow in the graph "
        "matched them. The flow may exist, may be implemented elsewhere, or "
        "may be out of date -- this build cannot tell which"
    ),
    "conflict": (
        "sources disagree. The document, the tests or the code describe "
        "different states, and the graph keeps every source rather than "
        "choosing one. Read the conflicts before relying on any of them"
    ),
}

PROCESS_INTERPRETATION_NOTE = (
    "Read 'source_evidence' and 'binding_status' together. 'source_evidence' "
    "is always 'documented': every process here was written by a person in a "
    "document in this repository, and none was inferred from code or from name "
    "similarity. 'binding_status' is the separate question of whether that "
    "description was tied to indexed code. Only 'bound' means a workflow "
    "matched. 'incomplete' means described but unverified, and must never be "
    "reported as verified, implemented, or confirmed. No status here means the "
    "process was ever executed: this build reads code, it does not run it."
)


#: Provenance -> the binding mode a caller sees. The stored provenance is the
#: authority; this is the name it is answered under.
BINDING_MODE_BY_PROVENANCE = {
    "document.text.model": "named_in_text",
    "document.text.route": "named_in_text",
    "document.text.xmlid": "named_in_text",
    "document.text.workflow": "named_in_text",
    "document.text.method.qualified": "qualified_reference",
    "document.text.method.unique_bare_name": "unique_bare_name",
    "document.text.method.ambiguous_bare_name": "ambiguous_bare_name",
    "document.text.method.unresolved": "unresolved_name",
}

BINDING_MODE_MEANING = {
    "named_in_text": (
        "the document contains this exact name and the graph holds a node with "
        "it. The document says so; that is all 'documented' ever means"
    ),
    "qualified_reference": (
        "the document wrote model.method, so both halves are in the text. The "
        "strongest binding this layer produces -- and still a reference in "
        "prose, not a call this build observed"
    ),
    "unique_bare_name": (
        "the document wrote a bare method name that exactly one model defines "
        "today, and this build inferred the model from that. A guess: the "
        "author never named a model. Confidence is heuristic, and a second "
        "model defining the same name turns this into 'ambiguous_bare_name' at "
        "the next rebuild"
    ),
    "ambiguous_bare_name": (
        "several models define this method name, so the reference is real and "
        "the target is unknown. Unresolved on purpose: naming one of the "
        "candidates would be a coin toss presented as a fact"
    ),
    "unresolved_name": (
        "the document names something shaped like an Odoo method that no model "
        "in this graph defines. Recorded so documentation drift is visible. It "
        "may be a method that was removed, renamed, never written, or one that "
        "lives outside what this build indexes"
    ),
}

SUBJECT_INTERPRETATION_NOTE = (
    "'source_evidence' is always 'documented': the text is in a document in "
    "this repository. It is not a claim that the link to a code object is "
    "certain. 'binding_mode' is that separate question and 'binding_confidence' "
    "grades it. Only 'qualified_reference' and 'named_in_text' are backed by "
    "the author naming the target; 'unique_bare_name' is this build's "
    "inference and must never be reported as the author's statement. An "
    "'ambiguous_bare_name' or 'unresolved_name' edge names no node at all."
)


def binding_mode_of(row) -> str:
    """The mode a stored subject edge was bound under."""
    return BINDING_MODE_BY_PROVENANCE.get(
        row.get("provenance") or "", "named_in_text"
    )


def annotate_subject(row: dict) -> dict:
    """Add source_evidence, binding_mode and binding_confidence to a subject row."""
    annotated = dict(row)
    annotated["source_evidence"] = EVIDENCE_DOCUMENTED
    annotated["binding_mode"] = binding_mode_of(row)
    annotated["binding_confidence"] = row.get("confidence") or "derived"
    annotated["binds_to_a_node"] = row.get("binding") == "resolved"
    return annotated


def subject_binding_counts(connection) -> dict:
    """How the subject edges in this graph were bound, by mode.

    A caller reading a handful of rows cannot tell whether the layer is mostly
    explicit or mostly inferred. These four numbers say so outright.
    """
    rows = connection.execute(
        "SELECT provenance, binding, COUNT(*) AS n "
        "FROM edge_section_documents_subject GROUP BY provenance, binding"
    ).fetchall()
    counts = {
        "explicitly_bound": 0,
        "heuristically_bound": 0,
        "ambiguous": 0,
        "unresolved": 0,
        "total": 0,
    }
    for row in rows:
        mode = BINDING_MODE_BY_PROVENANCE.get(row["provenance"], "named_in_text")
        number = int(row["n"])
        counts["total"] += number
        if mode == "ambiguous_bare_name":
            counts["ambiguous"] += number
        elif mode == "unresolved_name":
            counts["unresolved"] += number
        elif mode == "unique_bare_name":
            counts["heuristically_bound"] += number
        else:
            counts["explicitly_bound"] += number
    return counts


def binding_status_of(row) -> str:
    """bound / incomplete / conflict, derived from stored columns.

    Kept in the query layer so both process tools answer identically, and
    computed rather than stored so the schema did not have to move for it.
    Conflict wins over a match: a disagreement the caller has not seen is worse
    than a binding it has.
    """
    if row.get("conflict_count"):
        return "conflict"
    if row.get("status") == "documented" and row.get("workflow_id"):
        return "bound"
    return "incomplete"


def annotate_process(row: dict) -> dict:
    """Add the two fields that keep a documented process from reading as verified."""
    annotated = dict(row)
    annotated["source_evidence"] = EVIDENCE_DOCUMENTED
    annotated["binding_status"] = binding_status_of(row)
    annotated["verified_against_code"] = annotated["binding_status"] == "bound"
    return annotated

#: What a caller may address by kind.
KIND_TABLES = {
    "document": ("node_document", "doc_id"),
    "doc_section": ("node_doc_section", "section_id"),
    "test_file": ("node_test_file", "test_file_id"),
    "business_process": ("node_business_process", "process_id"),
    "convention": ("node_convention", "convention_id"),
}
KIND_NAMES = tuple(KIND_TABLES)

_SUBJECT_KINDS = ("module", "model", "route", "xmlid", "method", "workflow")


def check_kind(kind: str) -> tuple[str, str]:
    if kind not in KIND_TABLES:
        raise ValueError(f"refusing to query unknown v7 kind {kind!r}")
    return KIND_TABLES[kind]


def check_subject_kind(kind: str) -> str:
    if kind not in _SUBJECT_KINDS:
        raise ValueError(f"refusing to query unknown subject kind {kind!r}")
    return kind


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _rows(connection, sql, params, limit):
    fetched = [dict(r) for r in connection.execute(sql, (*params, limit + 1))]
    return fetched[:limit], len(fetched) > limit


# --------------------------------------------------------------------------
# documents
# --------------------------------------------------------------------------


def find_documents(connection, query, module, limit):
    sql = """
        SELECT doc_id, module, rel_path, kind, title, section_count,
               line_count, char_count, injection_flags, evidence
        FROM node_document
        WHERE (? = '' OR rel_path LIKE ? ESCAPE '\\'
               OR IFNULL(title, '') LIKE ? ESCAPE '\\')
          AND (? = '' OR IFNULL(module, '') = ?)
        ORDER BY rel_path
        LIMIT ?
    """
    pattern = f"%{_escape_like(query)}%" if query else ""
    return _rows(
        connection, sql,
        (query or "", pattern, pattern, module or "", module or ""), limit,
    )


def find_sections(connection, query, module, limit):
    sql = """
        SELECT section_id, doc_id, module, rel_path, level, title, anchor,
               line_start, line_end, excerpt, excerpt_truncated, char_count,
               injection_flags, evidence
        FROM node_doc_section
        WHERE (? = '' OR title LIKE ? ESCAPE '\\'
               OR excerpt LIKE ? ESCAPE '\\')
          AND (? = '' OR IFNULL(module, '') = ?)
        ORDER BY rel_path, line_start
        LIMIT ?
    """
    pattern = f"%{_escape_like(query)}%" if query else ""
    return _rows(
        connection, sql,
        (query or "", pattern, pattern, module or "", module or ""), limit,
    )


def sections_of(connection, doc_id, limit):
    return _rows(
        connection,
        "SELECT section_id, level, title, anchor, line_start, line_end, "
        "excerpt, excerpt_truncated, char_count, injection_flags "
        "FROM node_doc_section WHERE doc_id = ? ORDER BY line_start LIMIT ?",
        (doc_id,), limit,
    )


def document_paths_like(connection, value, limit=5):
    like = f"%{_escape_like(value.rsplit('/', 1)[-1])}%"
    return [
        r["rel_path"] for r in connection.execute(
            "SELECT rel_path FROM node_document WHERE rel_path LIKE ? "
            "ESCAPE '\\' ORDER BY rel_path LIMIT ?", (like, limit)
        )
    ]


def sections_documenting(connection, subject_kind, subject, limit):
    check_subject_kind(subject_kind)
    return _rows(
        connection,
        "SELECT s.section_id, s.rel_path, s.title, s.anchor, s.line_start, "
        "s.line_end, s.excerpt, s.excerpt_truncated, s.injection_flags, "
        "e.dst_kind, e.dst_unresolved, e.binding, e.confidence, e.provenance "
        "FROM edge_section_documents_subject e "
        "JOIN node_doc_section s ON s.section_id = e.src_id "
        "WHERE e.dst_kind = ? AND e.dst_unresolved = ? "
        "ORDER BY s.rel_path, s.line_start LIMIT ?",
        (subject_kind, subject), limit,
    )


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------


def tests_referencing(connection, subject_kind, subject, limit):
    """Test files whose source names this subject. Not a coverage claim."""
    check_subject_kind(subject_kind)
    return _rows(
        connection,
        "SELECT t.test_file_id, t.module, t.rel_path, t.language, t.framework, "
        "t.class_count, t.case_count, e.dst_kind, e.dst_unresolved, "
        "e.src_line, e.provenance "
        "FROM edge_test_references_subject e "
        "JOIN node_test_file t ON t.test_file_id = e.src_id "
        "WHERE e.dst_kind = ? AND e.dst_unresolved = ? "
        "ORDER BY t.rel_path, e.src_line LIMIT ?",
        (subject_kind, subject), limit,
    )


def classes_in(connection, test_file_id, limit):
    return _rows(
        connection,
        "SELECT test_class_id, class_name, bases, tags, case_count, line "
        "FROM node_test_class WHERE test_file_id = ? ORDER BY line LIMIT ?",
        (test_file_id,), limit,
    )


def cases_in(connection, test_class_id, limit):
    return _rows(
        connection,
        "SELECT test_case_id, name, line FROM node_test_case "
        "WHERE test_class_id = ? ORDER BY line LIMIT ?",
        (test_class_id,), limit,
    )


def tests_in_module(connection, module, limit):
    return _rows(
        connection,
        "SELECT test_file_id, rel_path, language, framework, class_count, "
        "case_count FROM node_test_file WHERE IFNULL(module, '') = ? "
        "ORDER BY rel_path LIMIT ?",
        (module,), limit,
    )


def migrations_in_module(connection, module, limit):
    return _rows(
        connection,
        "SELECT migration_id, version, phase, script, rel_path "
        "FROM node_migration WHERE module = ? "
        "ORDER BY version, phase, script LIMIT ?",
        (module,), limit,
    )


# --------------------------------------------------------------------------
# processes
# --------------------------------------------------------------------------


def find_processes(connection, query, module, status, limit):
    sql = """
        SELECT process_id, name, module, doc_id, section_id, status,
               step_count, matched_steps, workflow_id, conflict_count,
               evidence, rel_path, line
        FROM node_business_process
        WHERE (? = '' OR name LIKE ? ESCAPE '\\'
               OR rel_path LIKE ? ESCAPE '\\')
          AND (? = '' OR IFNULL(module, '') = ?)
          AND (? = '' OR status = ?)
        ORDER BY status, rel_path, line
        LIMIT ?
    """
    pattern = f"%{_escape_like(query)}%" if query else ""
    return _rows(
        connection, sql,
        (query or "", pattern, pattern, module or "", module or "",
         status or "", status or ""),
        limit,
    )


def get_process(connection, name):
    return [
        dict(r) for r in connection.execute(
            "SELECT process_id, name, module, doc_id, section_id, status, "
            "step_count, matched_steps, workflow_id, conflict_count, evidence, "
            "rel_path, line FROM node_business_process "
            "WHERE process_id = ? OR name = ? ORDER BY rel_path, line",
            (name, name),
        )
    ]


def process_names_like(connection, value, limit=5):
    like = f"%{_escape_like(value)}%"
    return [
        r["name"] for r in connection.execute(
            "SELECT DISTINCT name FROM node_business_process "
            "WHERE name LIKE ? ESCAPE '\\' ORDER BY name LIMIT ?", (like, limit)
        )
    ]


def steps_of(connection, process_id, limit):
    return _rows(
        connection,
        "SELECT step_id, position, label, normalised, state_id, state_value, "
        "match_kind, evidence, rel_path, line FROM node_process_step "
        "WHERE process_id = ? ORDER BY position LIMIT ?",
        (process_id,), limit,
    )


def support_for_step(connection, step_id, limit):
    return _rows(
        connection,
        "SELECT dst_kind, dst_id, dst_unresolved, binding, confidence, "
        "provenance, src_file, src_line FROM edge_step_supported_by "
        "WHERE src_id = ? "
        "UNION ALL "
        "SELECT 'test_file', src_id, dst_unresolved, binding, confidence, "
        "provenance, src_file, src_line FROM edge_test_supports_step "
        "WHERE dst_id = ? "
        "ORDER BY dst_kind, dst_unresolved LIMIT ?",
        (step_id, step_id), limit,
    )


def workflow_of(connection, workflow_id):
    row = connection.execute(
        "SELECT workflow_id, model_name, field_name, state_count, "
        "transition_count, state_source FROM node_workflow WHERE workflow_id = ?",
        (workflow_id,),
    ).fetchone()
    return dict(row) if row else None


def workflow_states_not_in_process(connection, workflow_id, process_id, limit):
    """States the code declares that the document's steps never mention.

    A gap, reported as a gap. It is not a defect in either the document or the
    code -- a document may summarise deliberately -- but a reader comparing the
    two should be able to see it without going and diffing them by hand.
    """
    return _rows(
        connection,
        "SELECT value, label, position FROM node_workflow_state "
        "WHERE workflow_id = ? AND state_id NOT IN "
        "(SELECT IFNULL(state_id, '') FROM node_process_step WHERE process_id = ?) "
        "ORDER BY position LIMIT ?",
        (workflow_id, process_id), limit,
    )


# --------------------------------------------------------------------------
# conventions
# --------------------------------------------------------------------------


def find_conventions(connection, status, limit):
    return _rows(
        connection,
        "SELECT convention_id, key, category, statement, status, module_count, "
        "total_modules, share, source_rel_path, source_line, evidence "
        "FROM node_convention WHERE (? = '' OR status = ?) "
        "ORDER BY CASE status "
        "  WHEN 'required_by_odoo' THEN 0 "
        "  WHEN 'documented_rule' THEN 1 "
        "  WHEN 'observed_pattern' THEN 2 ELSE 3 END, share DESC, key "
        "LIMIT ?",
        (status or "", status or ""), limit,
    )


def modules_following(connection, convention_id, limit):
    return _rows(
        connection,
        "SELECT dst_unresolved AS module, binding FROM "
        "edge_convention_observed_in_module WHERE src_id = ? "
        "ORDER BY dst_unresolved LIMIT ?",
        (convention_id,), limit,
    )


def sections_mentioning(connection, convention_id, limit):
    return _rows(
        connection,
        "SELECT s.section_id, s.rel_path, s.title, s.line_start, s.excerpt, "
        "s.excerpt_truncated, s.injection_flags "
        "FROM edge_convention_documented_in_section e "
        "JOIN node_doc_section s ON s.section_id = e.dst_id "
        "WHERE e.src_id = ? ORDER BY s.rel_path, s.line_start LIMIT ?",
        (convention_id,), limit,
    )


def counts(connection):
    def scalar(sql):
        return int(connection.execute(sql).fetchone()[0])

    return {
        "documents": scalar("SELECT COUNT(*) FROM node_document"),
        "doc_sections": scalar("SELECT COUNT(*) FROM node_doc_section"),
        "documents_with_flagged_text": scalar(
            "SELECT COUNT(*) FROM node_document WHERE injection_flags IS NOT NULL"),
        "test_files": scalar("SELECT COUNT(*) FROM node_test_file"),
        "test_classes": scalar("SELECT COUNT(*) FROM node_test_class"),
        "test_cases": scalar("SELECT COUNT(*) FROM node_test_case"),
        "migrations": scalar("SELECT COUNT(*) FROM node_migration"),
        "business_processes": scalar("SELECT COUNT(*) FROM node_business_process"),
        "processes_documented": scalar(
            "SELECT COUNT(*) FROM node_business_process WHERE status='documented'"),
        "processes_incomplete": scalar(
            "SELECT COUNT(*) FROM node_business_process WHERE status='incomplete'"),
        "process_steps": scalar("SELECT COUNT(*) FROM node_process_step"),
        "conventions": scalar("SELECT COUNT(*) FROM node_convention"),
    }
