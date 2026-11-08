"""SQLite graph store: schema, upserts, pruning, and the idempotency digest.

Schema v2 (M3.1) is strictly additive. Every M1 table keeps its exact columns,
constraints and digest semantics, because the modules digest must stay
bit-identical to the one M2 produced -- that equality is the regression test
proving the Python layer did not disturb the module layer. New node, edge and
node-diagnostic tables sit beside the old ones; nothing M1 wrote was reshaped.

Design rules for M1:

* No file contents are ever stored -- only hashes, paths, and metadata.
* Every node and edge has a deterministic primary key derived from stable
  identity, so re-running the indexer updates rows in place instead of
  appending duplicates.
* Rows carry ``last_run_id``. After a run, anything not touched by that run is
  deleted, so a module removed from disk disappears from the graph.
* ``graph_digest`` hashes every stable column (excluding per-run bookkeeping),
  which is how "two consecutive runs produce the same graph" is verified rather
  than assumed.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from . import SCHEMA_VERSION, __version__
from .errors import SchemaVersionError

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kg_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project (
    project_id     TEXT PRIMARY KEY,
    repo_root      TEXT NOT NULL,
    core_root      TEXT,
    branch         TEXT,
    base_commit    TEXT,
    dirty          INTEGER,
    git_available  INTEGER NOT NULL,
    git_error      TEXT,
    indexed_at     TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    indexer_version TEXT NOT NULL,
    milestone      TEXT NOT NULL,
    run_id         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_run (
    run_id         TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    base_commit    TEXT,
    dirty          INTEGER,
    files_seen     INTEGER DEFAULT 0,
    files_excluded INTEGER DEFAULT 0,
    modules        INTEGER DEFAULT 0,
    depends_edges  INTEGER DEFAULT 0,
    graph_digest   TEXT,
    status         TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS node_file (
    file_key      TEXT PRIMARY KEY,
    display_path  TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    include_root  TEXT NOT NULL,
    size_bytes    INTEGER,
    content_sha   TEXT,
    -- git_blob_sha is git's object id of the bytes ON DISK; index_blob_sha is
    -- what the git index holds. Under core.autocrlf they differ for every
    -- clean CRLF text file, so the two are NOT comparable. content_state comes
    -- from `git status` alone.
    git_blob_sha  TEXT,
    index_blob_sha TEXT,
    content_state TEXT NOT NULL,
    hash_state    TEXT NOT NULL,
    base_commit   TEXT,
    last_run_id   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_module (
    technical_name      TEXT PRIMARY KEY,
    manifest_name       TEXT,
    version             TEXT,
    version_series_ok   INTEGER,
    author              TEXT,
    license             TEXT,
    origin              TEXT NOT NULL,
    origin_rule         TEXT NOT NULL,
    present_in_source   INTEGER NOT NULL,
    installable         INTEGER,
    installable_declared INTEGER NOT NULL,
    installed_state     TEXT NOT NULL,
    application         INTEGER,
    application_declared INTEGER NOT NULL,
    auto_install        INTEGER,
    auto_install_raw    TEXT,
    modified_worktree   INTEGER NOT NULL,
    manifest_file       TEXT NOT NULL,
    manifest_line       INTEGER NOT NULL,
    module_dir          TEXT NOT NULL,
    content_state       TEXT NOT NULL,
    manifest_ok         INTEGER NOT NULL,
    manifest_error      TEXT,
    last_run_id         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edge_depends_on (
    edge_id            TEXT PRIMARY KEY,
    src_module         TEXT NOT NULL,
    dst_module         TEXT,
    dst_unresolved     TEXT NOT NULL,
    binding            TEXT NOT NULL,
    src_manifest_file  TEXT NOT NULL,
    src_manifest_line  INTEGER NOT NULL,
    provenance         TEXT NOT NULL,
    last_run_id        TEXT NOT NULL,
    UNIQUE (src_module, dst_unresolved)
);

CREATE TABLE IF NOT EXISTS diagnostic (
    run_id   TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    message  TEXT NOT NULL,
    path     TEXT
);

CREATE INDEX IF NOT EXISTS idx_edge_src ON edge_depends_on (src_module);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edge_depends_on (dst_module);
CREATE INDEX IF NOT EXISTS idx_edge_unresolved ON edge_depends_on (dst_unresolved);
CREATE INDEX IF NOT EXISTS idx_module_origin ON node_module (origin);
CREATE INDEX IF NOT EXISTS idx_file_state ON node_file (content_state);
"""

#: Bindings a schema-v2 edge may carry. ``edge_depends_on`` is deliberately NOT
#: given this constraint: it keeps its original two-value vocabulary so the
#: module layer is byte-for-byte what M2 wrote.
#:
#: resolved   -- target is a node in this graph
#: derived    -- target was computed, not read literally (a `related` path)
#: external   -- target is named and understood, but lives outside the project
#:               (Odoo core); no node is invented for it
#: unresolved -- target could not be determined; always paired with a diagnostic
#: synthetic  -- target is an identifier Odoo generates rather than one written
#:               down. Reserved for the XML layer in M3.2; unused in M3.1.
EDGE_BINDINGS = ("resolved", "derived", "external", "unresolved", "synthetic")

#: How much the indexer trusts an edge. ``heuristic`` exists so a name-matched
#: relationship can never be mistaken for one read out of the source.
EDGE_CONFIDENCE = ("certain", "derived", "heuristic")

#: Every edge table has the same shape, generated from one template so they
#: cannot drift apart. Order matters only for readability.
#:
#: Split by domain because each domain hashes separately. The XML tables carry
#: no columns of their own: what kind of reference an edge records lives in
#: ``provenance``, whose vocabulary odoo_kg.xmlindex defines. That keeps the
#: shared shape intact and keeps the Python layer's digest columns untouched by
#: a milestone that has nothing to do with it.
PYTHON_EDGE_TABLES = (
    "model_defined_by",
    "model_inherits",
    "model_delegates",
    "field_declared_on",
    "field_relation",
    "field_inverse_of",
    "method_declared_on",
)

XML_EDGE_TABLES = (
    "xmlid_defined_by",
    "xmlid_references",
    "record_instantiates",
)

SECURITY_EDGE_TABLES = (
    "group_implies",
    "group_in_privilege",
    "acl_on_model",
    "acl_grants_group",
    "rule_on_model",
    "rule_for_group",
)

#: The frontend layer. JS-derived edges are all `heuristic`; the manifest and
#: route ones are `certain`. See odoo_kg.frontindex for why.
FRONTEND_EDGE_TABLES = (
    "asset_in_bundle",
    "asset_includes_file",
    "js_imports",
    "js_registers",
    "js_patches",
    "js_uses_service",
    "component_uses_template",
    "template_inherits",
    "js_calls_route",
    "js_references_model",
)

#: v6 (M4.2). The workflow layer's edges. Every one of them is a relation the
#: source states outright or that an AST verifies; a name that merely looks
#: like a step never becomes an edge here.
WORKFLOW_EDGE_TABLES = (
    "workflow_on_model",
    "workflow_has_state",
    "transition_in_workflow",
    "transition_from_state",
    "transition_to_state",
    "method_performs_transition",
    "trigger_invokes_method",
    "cron_runs_method",
    "cron_on_model",
    "transition_touches_model",
    "route_invokes_method",
)

#: v7 (M4.3). Documentation. Everything a person wrote down about the code.
DOCUMENTATION_EDGE_TABLES = (
    "document_in_module",
    "section_in_document",
    "section_documents_subject",
)

#: v7 (M4.3). Tests and migrations: the repository's own engineering record.
TEST_EDGE_TABLES = (
    "test_in_module",
    "test_class_in_file",
    "test_case_in_class",
    "test_references_subject",
    "migration_in_module",
)

#: v7 (M4.3). Guidance: processes somebody described, and conventions this
#: build either read in a document or counted across the modules.
PROCESS_EDGE_TABLES = (
    "process_from_document",
    "process_for_workflow",
    "process_has_step",
    "step_supported_by",
    "test_supports_step",
    "convention_observed_in_module",
    "convention_documented_in_section",
)

EDGE_TABLES = (
    PYTHON_EDGE_TABLES + XML_EDGE_TABLES + SECURITY_EDGE_TABLES
    + FRONTEND_EDGE_TABLES + WORKFLOW_EDGE_TABLES
    + DOCUMENTATION_EDGE_TABLES + TEST_EDGE_TABLES + PROCESS_EDGE_TABLES
)

_EDGE_TABLE_TEMPLATE = """
CREATE TABLE IF NOT EXISTS edge_{name} (
    edge_id        TEXT PRIMARY KEY,
    src_kind       TEXT NOT NULL,
    src_id         TEXT NOT NULL,
    dst_kind       TEXT NOT NULL,
    dst_id         TEXT,
    dst_unresolved TEXT NOT NULL,
    binding        TEXT NOT NULL CHECK (binding IN {bindings}),
    confidence     TEXT NOT NULL CHECK (confidence IN {confidence}),
    provenance     TEXT NOT NULL,
    src_file       TEXT,
    src_line       INTEGER,
    last_run_id    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_{name}_src ON edge_{name} (src_id);
CREATE INDEX IF NOT EXISTS idx_{name}_dst ON edge_{name} (dst_id);
"""

SCHEMA_V2_SQL = """
CREATE TABLE IF NOT EXISTS node_model (
    model_name       TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    definition_count INTEGER NOT NULL,
    declares_name    INTEGER NOT NULL,
    field_count      INTEGER NOT NULL,
    method_count     INTEGER NOT NULL,
    last_run_id      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_model_def (
    def_id       TEXT PRIMARY KEY,
    model_name   TEXT,
    module       TEXT,
    file_key     TEXT NOT NULL,
    rel_path     TEXT NOT NULL,
    class_name   TEXT NOT NULL,
    line         INTEGER NOT NULL,
    end_line     INTEGER,
    base_kind    TEXT NOT NULL,
    declared_name TEXT,
    shape        TEXT NOT NULL,
    parse_state  TEXT NOT NULL,
    field_count  INTEGER NOT NULL,
    method_count INTEGER NOT NULL,
    last_run_id  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_field (
    field_id        TEXT PRIMARY KEY,
    def_id          TEXT NOT NULL,
    model_name      TEXT,
    field_name      TEXT NOT NULL,
    field_type      TEXT NOT NULL,
    is_relational   INTEGER NOT NULL,
    comodel_literal TEXT,
    comodel_state   TEXT NOT NULL,
    related_path    TEXT,
    compute_method  TEXT,
    inverse_name    TEXT,
    stored          INTEGER,
    required        INTEGER,
    readonly        INTEGER,
    groups_literal  TEXT,
    rel_path        TEXT NOT NULL,
    line            INTEGER NOT NULL,
    last_run_id     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_method (
    method_id       TEXT PRIMARY KEY,
    def_id          TEXT NOT NULL,
    model_name      TEXT,
    method_name     TEXT NOT NULL,
    kind            TEXT NOT NULL,
    is_private      INTEGER NOT NULL,
    decorators      TEXT,
    depends_literal TEXT,
    args_state      TEXT NOT NULL,
    rel_path        TEXT NOT NULL,
    line            INTEGER NOT NULL,
    last_run_id     TEXT NOT NULL
);

-- Node-scoped diagnostics. The M1 `diagnostic` table is untouched and still
-- carries scanner and manifest findings only; anything that belongs to a model,
-- field or method is recorded here so it can be joined back to its node instead
-- of only to a file path.
CREATE TABLE IF NOT EXISTS diagnostic_node (
    link_id   TEXT PRIMARY KEY,
    run_id    TEXT NOT NULL,
    severity  TEXT NOT NULL,
    category  TEXT NOT NULL,
    message   TEXT NOT NULL,
    node_kind TEXT NOT NULL,
    node_id   TEXT NOT NULL,
    rel_path  TEXT,
    line      INTEGER
);

-- Per-domain content hashes. index_run.graph_digest keeps its original meaning
-- (the module layer) so an M2 database and an M3 database of the same commit
-- report the same value there.
CREATE TABLE IF NOT EXISTS run_digest (
    run_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (run_id, domain)
);

CREATE INDEX IF NOT EXISTS idx_model_def_model ON node_model_def (model_name);
CREATE INDEX IF NOT EXISTS idx_model_def_file ON node_model_def (file_key);
CREATE INDEX IF NOT EXISTS idx_field_model ON node_field (model_name);
CREATE INDEX IF NOT EXISTS idx_field_def ON node_field (def_id);
CREATE INDEX IF NOT EXISTS idx_method_model ON node_method (model_name);
CREATE INDEX IF NOT EXISTS idx_method_def ON node_method (def_id);
CREATE INDEX IF NOT EXISTS idx_diagnostic_node_node ON diagnostic_node (node_kind, node_id);

-- ---- schema v3: the XML layer -------------------------------------------
-- One row per file a manifest declares, in the order Odoo would load it.
-- Non-XML entries (ACL CSVs) are recorded for their load order only; reading
-- their contents belongs to the security milestone.
CREATE TABLE IF NOT EXISTS node_data_file (
    file_id      TEXT PRIMARY KEY,
    module       TEXT NOT NULL,
    section      TEXT NOT NULL,
    load_order   INTEGER NOT NULL,
    rel_path     TEXT NOT NULL,
    file_key     TEXT,
    kind         TEXT NOT NULL,
    parse_state  TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    last_run_id  TEXT NOT NULL
);

-- The identity of an XML ID, separate from the elements that define it.
CREATE TABLE IF NOT EXISTS node_xmlid (
    xmlid            TEXT PRIMARY KEY,
    module           TEXT NOT NULL,
    local_id         TEXT NOT NULL,
    definition_count INTEGER NOT NULL,
    winner_record_id TEXT NOT NULL,
    winner_rule      TEXT NOT NULL,
    last_run_id      TEXT NOT NULL
);

-- One element that defines something: <record>, <menuitem>, <template>, ...
CREATE TABLE IF NOT EXISTS node_xml_record (
    record_id    TEXT PRIMARY KEY,
    xmlid        TEXT,
    declared_id  TEXT,
    file_id      TEXT NOT NULL,
    module       TEXT NOT NULL,
    rel_path     TEXT NOT NULL,
    line         INTEGER NOT NULL,
    tag          TEXT NOT NULL,
    record_model TEXT,
    last_run_id  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_data_file_module ON node_data_file (module, load_order);
CREATE INDEX IF NOT EXISTS idx_xmlid_module ON node_xmlid (module);
CREATE INDEX IF NOT EXISTS idx_xml_record_xmlid ON node_xml_record (xmlid);
CREATE INDEX IF NOT EXISTS idx_xml_record_file ON node_xml_record (file_id);
CREATE INDEX IF NOT EXISTS idx_xml_record_model ON node_xml_record (record_model);

-- ---- schema v4: the security layer --------------------------------------
-- Declared evidence only. Nothing here is an access decision, and no domain,
-- eval expression or Python fragment is ever executed to produce it.
CREATE TABLE IF NOT EXISTS node_group (
    group_xmlid     TEXT PRIMARY KEY,
    module          TEXT NOT NULL,
    local_id        TEXT NOT NULL,
    name            TEXT,
    privilege_xmlid TEXT,
    rel_path        TEXT NOT NULL,
    line            INTEGER NOT NULL,
    last_run_id     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_acl (
    acl_id       TEXT PRIMARY KEY,
    csv_id       TEXT NOT NULL,
    xmlid        TEXT NOT NULL,
    module       TEXT NOT NULL,
    name         TEXT,
    model_target TEXT NOT NULL,
    group_target TEXT,
    is_global    INTEGER NOT NULL,
    perm_read    INTEGER NOT NULL,
    perm_write   INTEGER NOT NULL,
    perm_create  INTEGER NOT NULL,
    perm_unlink  INTEGER NOT NULL,
    rel_path     TEXT NOT NULL,
    line         INTEGER NOT NULL,
    last_run_id  TEXT NOT NULL
);

-- domain_force is stored as the source text it is. It is never parsed as a
-- domain, never evaluated, and must never be presented as an access outcome.
CREATE TABLE IF NOT EXISTS node_rule (
    rule_id         TEXT PRIMARY KEY,
    xmlid           TEXT,
    module          TEXT NOT NULL,
    name            TEXT,
    model_target    TEXT NOT NULL,
    domain_force    TEXT,
    is_global       INTEGER NOT NULL,
    global_declared INTEGER,
    active_declared INTEGER,
    perm_read       INTEGER,
    perm_write      INTEGER,
    perm_create     INTEGER,
    perm_unlink     INTEGER,
    rel_path        TEXT NOT NULL,
    line            INTEGER NOT NULL,
    last_run_id     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_acl_model ON node_acl (model_target);
CREATE INDEX IF NOT EXISTS idx_acl_group ON node_acl (group_target);
CREATE INDEX IF NOT EXISTS idx_rule_model ON node_rule (model_target);
-- res.groups.privilege: organisational metadata that groups the access groups
-- shown on the user form. Odoo renders a group as "Privilege / Group" and
-- makes a group's name unique within its privilege, so this is an identity
-- dimension. It is NOT itself a permission: belonging to a privilege grants
-- nothing, and nothing here may be read as an access decision.
CREATE TABLE IF NOT EXISTS node_privilege (
    privilege_xmlid TEXT PRIMARY KEY,
    module          TEXT NOT NULL,
    local_id        TEXT NOT NULL,
    name            TEXT,
    sequence        INTEGER,
    description     TEXT,
    placeholder     TEXT,
    category_xmlid  TEXT,
    rel_path        TEXT NOT NULL,
    line            INTEGER NOT NULL,
    last_run_id     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_group_module ON node_group (module);
CREATE INDEX IF NOT EXISTS idx_privilege_module ON node_privilege (module);

-- ---- schema v5: the frontend layer --------------------------------------
-- Asset bundles, JavaScript, OWL components and templates, and HTTP routes.
-- Nothing here is executed: JS is lexed, never run, and routes are read from
-- decorators without touching a method body.
CREATE TABLE IF NOT EXISTS node_asset_bundle (
    bundle_name TEXT PRIMARY KEY,
    entry_count INTEGER NOT NULL,
    last_run_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_asset_entry (
    entry_id     TEXT PRIMARY KEY,
    module       TEXT NOT NULL,
    bundle       TEXT NOT NULL,
    position     INTEGER NOT NULL,
    operation    TEXT NOT NULL,
    target       TEXT NOT NULL,
    extra_target TEXT,
    is_glob      INTEGER NOT NULL,
    rel_path     TEXT NOT NULL,
    line         INTEGER NOT NULL,
    last_run_id  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_js_file (
    js_id        TEXT PRIMARY KEY,
    module       TEXT NOT NULL,
    rel_path     TEXT NOT NULL,
    file_key     TEXT NOT NULL,
    addon_path   TEXT NOT NULL,
    odoo_module  INTEGER NOT NULL,
    lex_state    TEXT NOT NULL,
    token_count  INTEGER NOT NULL,
    class_count  INTEGER NOT NULL,
    import_count INTEGER NOT NULL,
    last_run_id  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_owl_component (
    component_id  TEXT PRIMARY KEY,
    js_id         TEXT NOT NULL,
    module        TEXT NOT NULL,
    class_name    TEXT NOT NULL,
    base          TEXT NOT NULL,
    template_name TEXT,
    rel_path      TEXT NOT NULL,
    line          INTEGER NOT NULL,
    last_run_id   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_owl_template (
    template_name TEXT PRIMARY KEY,
    module        TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    line          INTEGER NOT NULL,
    inherits      TEXT,
    primary_mode  TEXT,
    last_run_id   TEXT NOT NULL
);

-- One row per declared path. auth and type are the most security-relevant
-- literals in an Odoo codebase, so they are stored exactly as written and are
-- NULL when not declared -- Odoo's runtime default is never assumed here.
CREATE TABLE IF NOT EXISTS node_route (
    route_id             TEXT PRIMARY KEY,
    path                 TEXT,
    module               TEXT NOT NULL,
    controller           TEXT NOT NULL,
    method               TEXT NOT NULL,
    base                 TEXT NOT NULL,
    auth                 TEXT,
    route_type           TEXT,
    methods              TEXT,
    website              INTEGER,
    csrf                 INTEGER,
    sitemap              TEXT,
    has_dynamic_argument INTEGER NOT NULL,
    rel_path             TEXT NOT NULL,
    line                 INTEGER NOT NULL,
    last_run_id          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_asset_entry_bundle ON node_asset_entry (bundle);
CREATE INDEX IF NOT EXISTS idx_asset_entry_module ON node_asset_entry (module);
CREATE INDEX IF NOT EXISTS idx_js_module ON node_js_file (module);
CREATE INDEX IF NOT EXISTS idx_js_addon_path ON node_js_file (addon_path);
CREATE INDEX IF NOT EXISTS idx_component_js ON node_owl_component (js_id);
CREATE INDEX IF NOT EXISTS idx_component_template ON node_owl_component (template_name);
CREATE INDEX IF NOT EXISTS idx_route_path ON node_route (path);
CREATE INDEX IF NOT EXISTS idx_route_module ON node_route (module);

-- ---------------------------------------------------------------------
-- schema v6 (M4.2): the workflow layer.
--
-- A workflow here is one state-carrying field on one model. That pairing is
-- the identity, which is why every state and transition id includes both: the
-- value 'draft' on sale.order and 'draft' on account.move are two different
-- things, and a graph that merged them would answer questions about a process
-- that does not exist.
--
-- Every row carries an evidence kind. `declared` means the source says it
-- outright -- a Selection value, an XML button. `static` means an AST verified
-- it -- an assignment, a write with a literal payload. `heuristic` means it
-- was inferred from a guard or a name and is a candidate, never a confirmed
-- business process. `documented` is reserved for M4.3 and is not written by
-- this build.
--
-- Nothing here records that a transition ran. A method that can set a state is
-- not a method that did, and the conditions under which it would are kept as
-- unevaluated source text rather than resolved.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS node_workflow (
    workflow_id      TEXT PRIMARY KEY,
    model_name       TEXT NOT NULL,
    field_name       TEXT NOT NULL,
    field_type       TEXT NOT NULL,
    module           TEXT,
    -- 'selection' when the states are literals in the code; 'records' when the
    -- field points at a stage model, whose states live in data this build does
    -- not read; 'inferred' when only transitions gave it away.
    state_source     TEXT NOT NULL,
    state_count      INTEGER NOT NULL DEFAULT 0,
    transition_count INTEGER NOT NULL DEFAULT 0,
    has_dynamic_states INTEGER NOT NULL DEFAULT 0,
    evidence         TEXT NOT NULL,
    rel_path         TEXT,
    line             INTEGER,
    last_run_id      TEXT NOT NULL,
    UNIQUE (model_name, field_name)
);

CREATE TABLE IF NOT EXISTS node_workflow_state (
    state_id     TEXT PRIMARY KEY,
    workflow_id  TEXT NOT NULL,
    model_name   TEXT NOT NULL,
    field_name   TEXT NOT NULL,
    value        TEXT NOT NULL,
    label        TEXT,
    position     INTEGER NOT NULL,
    -- 'declared' from a Selection literal; 'static' when only a write named it
    -- and no Selection declares it, which is worth knowing about.
    evidence     TEXT NOT NULL,
    rel_path     TEXT,
    line         INTEGER,
    last_run_id  TEXT NOT NULL,
    UNIQUE (model_name, field_name, value)
);

CREATE TABLE IF NOT EXISTS node_transition (
    transition_id  TEXT PRIMARY KEY,
    workflow_id    TEXT NOT NULL,
    model_name     TEXT NOT NULL,
    field_name     TEXT NOT NULL,
    from_state     TEXT,
    from_evidence  TEXT,
    to_state       TEXT,
    operation      TEXT NOT NULL,
    receiver       TEXT,
    method_name    TEXT,
    method_id      TEXT,
    -- A transition inside an if, a loop, or in a method that raises is not an
    -- unconditional step. The condition is kept as written and never evaluated.
    conditional    INTEGER NOT NULL DEFAULT 0,
    condition_kind TEXT,
    condition_text TEXT,
    -- Set when the value was not a literal. The expression is preserved so a
    -- reader can see what would decide it; the target stays unknown.
    dynamic_expression TEXT,
    evidence       TEXT NOT NULL,
    confidence     TEXT NOT NULL,
    module         TEXT,
    rel_path       TEXT NOT NULL,
    line           INTEGER NOT NULL,
    last_run_id    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_trigger (
    trigger_id     TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    name           TEXT NOT NULL,
    module         TEXT,
    target_model   TEXT,
    target_method  TEXT,
    -- An XML button's `invisible`/`states` as written. Read, never evaluated:
    -- where the shape is exactly `state != 'x'` the visible-in states are
    -- recorded beside it; anything else keeps only the raw text.
    guard_text     TEXT,
    guard_states   TEXT,
    guard_state_evidence TEXT,
    evidence       TEXT NOT NULL,
    rel_path       TEXT NOT NULL,
    line           INTEGER NOT NULL,
    last_run_id    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_scheduled_action (
    cron_id      TEXT PRIMARY KEY,
    xmlid        TEXT,
    name         TEXT,
    module       TEXT NOT NULL,
    model_ref    TEXT,
    target_model TEXT,
    method_name  TEXT,
    interval     TEXT,
    active       TEXT,
    code_text    TEXT,
    code_shape   TEXT NOT NULL,
    evidence     TEXT NOT NULL,
    rel_path     TEXT NOT NULL,
    line         INTEGER NOT NULL,
    last_run_id  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_model ON node_workflow (model_name);
CREATE INDEX IF NOT EXISTS idx_wf_state_workflow ON node_workflow_state (workflow_id);
CREATE INDEX IF NOT EXISTS idx_transition_workflow ON node_transition (workflow_id);
CREATE INDEX IF NOT EXISTS idx_transition_model ON node_transition (model_name);
CREATE INDEX IF NOT EXISTS idx_transition_method ON node_transition (method_id);
CREATE INDEX IF NOT EXISTS idx_trigger_kind ON node_trigger (kind);
CREATE INDEX IF NOT EXISTS idx_trigger_target ON node_trigger (target_model, target_method);
CREATE INDEX IF NOT EXISTS idx_cron_model ON node_scheduled_action (target_model);

-- ---------------------------------------------------------------------
-- schema v7 (M4.3): documentation, tests, migrations, processes, conventions.
--
-- Every row here is a claim somebody made, not a fact the compiler enforces,
-- so each carries an evidence kind and each is reported as such:
--
--   documented  a document says it, in words, at a line this graph names
--   tested      a test names it, statically -- never that it is covered
--   observed    a structure recurs across modules, counted, not mandated
--
-- Two things this layer must never be read as saying. A test naming a model
-- is not test coverage of that model, and no test naming it is not evidence
-- that it is untested. An observed pattern is not a rule: `share` says how
-- many modules follow it, and that is the whole claim.
--
-- Document text is UNTRUSTED CONTENT. It is stored, excerpted and returned as
-- data; nothing in it is ever followed. `injection_flags` marks text that
-- reads like an instruction, so a caller can see it was noticed rather than
-- silently removed.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS node_document (
    doc_id          TEXT PRIMARY KEY,
    module          TEXT,
    rel_path        TEXT NOT NULL,
    kind            TEXT NOT NULL,
    title           TEXT,
    section_count   INTEGER NOT NULL DEFAULT 0,
    line_count      INTEGER NOT NULL DEFAULT 0,
    char_count      INTEGER NOT NULL DEFAULT 0,
    injection_flags TEXT,
    evidence        TEXT NOT NULL,
    last_run_id     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_doc_section (
    section_id      TEXT PRIMARY KEY,
    doc_id          TEXT NOT NULL,
    module          TEXT,
    rel_path        TEXT NOT NULL,
    level           INTEGER NOT NULL,
    title           TEXT NOT NULL,
    anchor          TEXT NOT NULL,
    line_start      INTEGER NOT NULL,
    line_end        INTEGER NOT NULL,
    -- Bounded at read time. A section's whole text is never stored, so no
    -- answer can be made large by writing a large document.
    excerpt         TEXT NOT NULL,
    excerpt_truncated INTEGER NOT NULL DEFAULT 0,
    char_count      INTEGER NOT NULL DEFAULT 0,
    injection_flags TEXT,
    evidence        TEXT NOT NULL,
    last_run_id     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_test_file (
    test_file_id  TEXT PRIMARY KEY,
    module        TEXT,
    rel_path      TEXT NOT NULL,
    language      TEXT NOT NULL,
    framework     TEXT,
    class_count   INTEGER NOT NULL DEFAULT 0,
    case_count    INTEGER NOT NULL DEFAULT 0,
    parse_state   TEXT NOT NULL,
    evidence      TEXT NOT NULL,
    last_run_id   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_test_class (
    test_class_id TEXT PRIMARY KEY,
    test_file_id  TEXT NOT NULL,
    module        TEXT,
    class_name    TEXT NOT NULL,
    bases         TEXT,
    tags          TEXT,
    case_count    INTEGER NOT NULL DEFAULT 0,
    rel_path      TEXT NOT NULL,
    line          INTEGER NOT NULL,
    evidence      TEXT NOT NULL,
    last_run_id   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_test_case (
    test_case_id  TEXT PRIMARY KEY,
    test_class_id TEXT NOT NULL,
    module        TEXT,
    name          TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    line          INTEGER NOT NULL,
    evidence      TEXT NOT NULL,
    last_run_id   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_migration (
    migration_id TEXT PRIMARY KEY,
    module       TEXT NOT NULL,
    version      TEXT NOT NULL,
    phase        TEXT NOT NULL,
    script       TEXT NOT NULL,
    rel_path     TEXT NOT NULL,
    evidence     TEXT NOT NULL,
    last_run_id  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_business_process (
    process_id    TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    module        TEXT,
    doc_id        TEXT,
    section_id    TEXT,
    -- documented: a document describes it in words.
    -- incomplete: a document announces a flow this build could not bind to
    --             code, so the steps stand alone.
    -- unknown:    evidence exists but is not enough to say what it is.
    status        TEXT NOT NULL,
    step_count    INTEGER NOT NULL DEFAULT 0,
    matched_steps INTEGER NOT NULL DEFAULT 0,
    workflow_id   TEXT,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    evidence      TEXT NOT NULL,
    rel_path      TEXT NOT NULL,
    line          INTEGER NOT NULL,
    last_run_id   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_process_step (
    step_id      TEXT PRIMARY KEY,
    process_id   TEXT NOT NULL,
    position     INTEGER NOT NULL,
    label        TEXT NOT NULL,
    normalised   TEXT NOT NULL,
    -- The v6 state this step lines up with, when one does. NULL is the normal
    -- case and is not a defect: a document names a step, not a Selection key.
    state_id     TEXT,
    state_value  TEXT,
    match_kind   TEXT,
    evidence     TEXT NOT NULL,
    rel_path     TEXT NOT NULL,
    line         INTEGER NOT NULL,
    last_run_id  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_convention (
    convention_id TEXT PRIMARY KEY,
    key           TEXT NOT NULL,
    category      TEXT NOT NULL,
    statement     TEXT NOT NULL,
    -- required_by_odoo      the framework will not load the module without it
    -- documented_acme_rule  a document in this repository states it
    -- observed_pattern      counted across modules, mandated by nobody
    -- recommendation        this build's suggestion, pending human approval
    status        TEXT NOT NULL,
    module_count  INTEGER NOT NULL DEFAULT 0,
    total_modules INTEGER NOT NULL DEFAULT 0,
    share         REAL NOT NULL DEFAULT 0,
    source_rel_path TEXT,
    source_line   INTEGER,
    evidence      TEXT NOT NULL,
    last_run_id   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_module ON node_document (module);
CREATE INDEX IF NOT EXISTS idx_section_doc ON node_doc_section (doc_id);
CREATE INDEX IF NOT EXISTS idx_test_file_module ON node_test_file (module);
CREATE INDEX IF NOT EXISTS idx_test_class_file ON node_test_class (test_file_id);
CREATE INDEX IF NOT EXISTS idx_test_case_class ON node_test_case (test_class_id);
CREATE INDEX IF NOT EXISTS idx_migration_module ON node_migration (module);
CREATE INDEX IF NOT EXISTS idx_process_workflow ON node_business_process (workflow_id);
CREATE INDEX IF NOT EXISTS idx_step_process ON node_process_step (process_id);
CREATE INDEX IF NOT EXISTS idx_convention_status ON node_convention (status);
""" + "".join(
    _EDGE_TABLE_TEMPLATE.format(
        name=name,
        bindings=str(EDGE_BINDINGS),
        confidence=str(EDGE_CONFIDENCE),
    )
    for name in EDGE_TABLES
)

#: Tables each layer adds, newest layer last. Used by prune, drop and counting.
V2_NODE_TABLES = ("node_model", "node_model_def", "node_field", "node_method")
V3_NODE_TABLES = ("node_data_file", "node_xmlid", "node_xml_record")
V4_NODE_TABLES = ("node_group", "node_acl", "node_rule", "node_privilege")
V5_NODE_TABLES = (
    "node_asset_bundle", "node_asset_entry", "node_js_file",
    "node_owl_component", "node_owl_template", "node_route",
)
V6_NODE_TABLES = (
    "node_workflow", "node_workflow_state", "node_transition",
    "node_trigger", "node_scheduled_action",
)
V7_DOC_NODE_TABLES = ("node_document", "node_doc_section")
V7_TEST_NODE_TABLES = (
    "node_test_file", "node_test_class", "node_test_case", "node_migration",
)
V7_PROCESS_NODE_TABLES = (
    "node_business_process", "node_process_step", "node_convention",
)
V7_NODE_TABLES = (
    V7_DOC_NODE_TABLES + V7_TEST_NODE_TABLES + V7_PROCESS_NODE_TABLES
)
V2_EDGE_TABLES = tuple(f"edge_{name}" for name in EDGE_TABLES)
PYTHON_EDGE_TABLE_NAMES = tuple(f"edge_{name}" for name in PYTHON_EDGE_TABLES)
XML_EDGE_TABLE_NAMES = tuple(f"edge_{name}" for name in XML_EDGE_TABLES)
SECURITY_EDGE_TABLE_NAMES = tuple(f"edge_{name}" for name in SECURITY_EDGE_TABLES)
FRONTEND_EDGE_TABLE_NAMES = tuple(f"edge_{name}" for name in FRONTEND_EDGE_TABLES)
WORKFLOW_EDGE_TABLE_NAMES = tuple(f"edge_{name}" for name in WORKFLOW_EDGE_TABLES)
DOCUMENTATION_EDGE_TABLE_NAMES = tuple(
    f"edge_{name}" for name in DOCUMENTATION_EDGE_TABLES)
TEST_EDGE_TABLE_NAMES = tuple(f"edge_{name}" for name in TEST_EDGE_TABLES)
PROCESS_EDGE_TABLE_NAMES = tuple(f"edge_{name}" for name in PROCESS_EDGE_TABLES)
V7_EDGE_TABLE_NAMES = (
    DOCUMENTATION_EDGE_TABLE_NAMES + TEST_EDGE_TABLE_NAMES
    + PROCESS_EDGE_TABLE_NAMES
)

#: The only identifiers that may be interpolated into an edge INSERT. A
#: frozenset rather than the tuple so the check at the SQL sink is a membership
#: test against something no caller can extend.
_ALLOWED_EDGE_TABLES: frozenset[str] = frozenset(EDGE_TABLES)

#: Columns that describe the graph itself. Deliberately excludes last_run_id
#: and timestamps so the digest is comparable between runs.
_MODULE_DIGEST_COLUMNS = (
    "technical_name", "manifest_name", "version", "version_series_ok", "author",
    "license", "origin", "origin_rule", "present_in_source", "installable",
    "installable_declared", "installed_state", "application",
    "application_declared", "auto_install", "auto_install_raw",
    "modified_worktree", "manifest_file", "manifest_line", "module_dir",
    "content_state", "manifest_ok", "manifest_error",
)

_EDGE_DIGEST_COLUMNS = (
    "edge_id", "src_module", "dst_module", "dst_unresolved", "binding",
    "src_manifest_file", "src_manifest_line", "provenance",
)

_FILE_DIGEST_COLUMNS = (
    "file_key", "rel_path", "size_bytes", "content_sha", "git_blob_sha",
    "content_state", "hash_state",
)

#: The module layer, in the exact order and column set M1 used. Kept as its own
#: named spec so it is obvious that changing it breaks the M2 comparison.
_MODULES_DIGEST_SPEC = (
    ("node_module", _MODULE_DIGEST_COLUMNS, "technical_name"),
    ("edge_depends_on", _EDGE_DIGEST_COLUMNS, "edge_id"),
    ("node_file", _FILE_DIGEST_COLUMNS, "file_key"),
)

_MODEL_DIGEST_COLUMNS = (
    "model_name", "kind", "definition_count", "declares_name", "field_count",
    "method_count",
)
_MODEL_DEF_DIGEST_COLUMNS = (
    "def_id", "model_name", "module", "rel_path", "class_name", "line",
    "end_line", "base_kind", "declared_name", "shape", "parse_state",
    "field_count", "method_count",
)
_FIELD_DIGEST_COLUMNS = (
    "field_id", "def_id", "model_name", "field_name", "field_type",
    "is_relational", "comodel_literal", "comodel_state", "related_path",
    "compute_method", "inverse_name", "stored", "required", "readonly",
    "groups_literal", "rel_path", "line",
)
_METHOD_DIGEST_COLUMNS = (
    "method_id", "def_id", "model_name", "method_name", "kind", "is_private",
    "decorators", "depends_literal", "args_state", "rel_path", "line",
)
_EDGE_V2_DIGEST_COLUMNS = (
    "edge_id", "src_kind", "src_id", "dst_kind", "dst_id", "dst_unresolved",
    "binding", "confidence", "provenance", "src_file", "src_line",
)

#: The Python layer. Ordered by table name so the digest cannot depend on the
#: order rows happened to be written in.
_PYTHON_DIGEST_SPEC = (
    ("node_model", _MODEL_DIGEST_COLUMNS, "model_name"),
    ("node_model_def", _MODEL_DEF_DIGEST_COLUMNS, "def_id"),
    ("node_field", _FIELD_DIGEST_COLUMNS, "field_id"),
    ("node_method", _METHOD_DIGEST_COLUMNS, "method_id"),
) + tuple(
    (f"edge_{name}", _EDGE_V2_DIGEST_COLUMNS, "edge_id") for name in PYTHON_EDGE_TABLES
)

_DATA_FILE_DIGEST_COLUMNS = (
    "file_id", "module", "section", "load_order", "rel_path", "kind",
    "parse_state", "record_count",
)
_XMLID_DIGEST_COLUMNS = (
    "xmlid", "module", "local_id", "definition_count", "winner_record_id",
    "winner_rule",
)
_XML_RECORD_DIGEST_COLUMNS = (
    "record_id", "xmlid", "declared_id", "file_id", "module", "rel_path", "line",
    "tag", "record_model",
)

#: The XML layer. Its own domain so a change here cannot be mistaken for a
#: change in the Python layer, and vice versa.
_XML_DIGEST_SPEC = (
    ("node_data_file", _DATA_FILE_DIGEST_COLUMNS, "file_id"),
    ("node_xmlid", _XMLID_DIGEST_COLUMNS, "xmlid"),
    ("node_xml_record", _XML_RECORD_DIGEST_COLUMNS, "record_id"),
) + tuple(
    (f"edge_{name}", _EDGE_V2_DIGEST_COLUMNS, "edge_id") for name in XML_EDGE_TABLES
)

_GROUP_DIGEST_COLUMNS = (
    "group_xmlid", "module", "local_id", "name", "privilege_xmlid", "rel_path",
    "line",
)
_ACL_DIGEST_COLUMNS = (
    "acl_id", "csv_id", "xmlid", "module", "name", "model_target", "group_target",
    "is_global", "perm_read", "perm_write", "perm_create", "perm_unlink",
    "rel_path", "line",
)
_PRIVILEGE_DIGEST_COLUMNS = (
    "privilege_xmlid", "module", "local_id", "name", "sequence", "description",
    "placeholder", "category_xmlid", "rel_path", "line",
)
_RULE_DIGEST_COLUMNS = (
    "rule_id", "xmlid", "module", "name", "model_target", "domain_force",
    "is_global", "global_declared", "active_declared", "perm_read", "perm_write",
    "perm_create", "perm_unlink", "rel_path", "line",
)

#: The security layer. Its own domain, so a change to who may do what is never
#: hidden inside a digest that also covers unrelated things.
_SECURITY_DIGEST_SPEC = (
    ("node_group", _GROUP_DIGEST_COLUMNS, "group_xmlid"),
    ("node_acl", _ACL_DIGEST_COLUMNS, "acl_id"),
    ("node_rule", _RULE_DIGEST_COLUMNS, "rule_id"),
    ("node_privilege", _PRIVILEGE_DIGEST_COLUMNS, "privilege_xmlid"),
) + tuple(
    (f"edge_{name}", _EDGE_V2_DIGEST_COLUMNS, "edge_id") for name in SECURITY_EDGE_TABLES
)

_ASSET_BUNDLE_DIGEST_COLUMNS = ("bundle_name", "entry_count")
_ASSET_ENTRY_DIGEST_COLUMNS = (
    "entry_id", "module", "bundle", "position", "operation", "target",
    "extra_target", "is_glob", "rel_path", "line",
)
_JS_FILE_DIGEST_COLUMNS = (
    "js_id", "module", "rel_path", "addon_path", "odoo_module", "lex_state",
    "token_count", "class_count", "import_count",
)
_OWL_COMPONENT_DIGEST_COLUMNS = (
    "component_id", "js_id", "module", "class_name", "base", "template_name",
    "rel_path", "line",
)
_OWL_TEMPLATE_DIGEST_COLUMNS = (
    "template_name", "module", "rel_path", "line", "inherits", "primary_mode",
)
_ROUTE_DIGEST_COLUMNS = (
    "route_id", "path", "module", "controller", "method", "base", "auth",
    "route_type", "methods", "website", "csrf", "sitemap",
    "has_dynamic_argument", "rel_path", "line",
)

#: The frontend layer. Its own domain, so a change in JavaScript can never be
#: mistaken for a change in a model, a view or an access rule.
_FRONTEND_DIGEST_SPEC = (
    ("node_asset_bundle", _ASSET_BUNDLE_DIGEST_COLUMNS, "bundle_name"),
    ("node_asset_entry", _ASSET_ENTRY_DIGEST_COLUMNS, "entry_id"),
    ("node_js_file", _JS_FILE_DIGEST_COLUMNS, "js_id"),
    ("node_owl_component", _OWL_COMPONENT_DIGEST_COLUMNS, "component_id"),
    ("node_owl_template", _OWL_TEMPLATE_DIGEST_COLUMNS, "template_name"),
    ("node_route", _ROUTE_DIGEST_COLUMNS, "route_id"),
) + tuple(
    (f"edge_{name}", _EDGE_V2_DIGEST_COLUMNS, "edge_id") for name in FRONTEND_EDGE_TABLES
)

#: The only specs :meth:`GraphStore._digest_over` will act on, matched by
#: identity. Adding a domain means adding it here, deliberately.
_WORKFLOW_DIGEST_COLUMNS = (
    "workflow_id", "model_name", "field_name", "field_type", "module",
    "state_source", "state_count", "transition_count", "has_dynamic_states",
    "evidence", "rel_path", "line",
)
_WORKFLOW_STATE_DIGEST_COLUMNS = (
    "state_id", "workflow_id", "model_name", "field_name", "value", "label",
    "position", "evidence", "rel_path", "line",
)
_TRANSITION_DIGEST_COLUMNS = (
    "transition_id", "workflow_id", "model_name", "field_name", "from_state",
    "from_evidence", "to_state", "operation", "receiver", "method_name",
    "method_id", "conditional", "condition_kind", "condition_text",
    "dynamic_expression", "evidence", "confidence", "module", "rel_path", "line",
)
_TRIGGER_DIGEST_COLUMNS = (
    "trigger_id", "kind", "name", "module", "target_model", "target_method",
    "guard_text", "guard_states", "guard_state_evidence", "evidence",
    "rel_path", "line",
)
_CRON_DIGEST_COLUMNS = (
    "cron_id", "xmlid", "name", "module", "model_ref", "target_model",
    "method_name", "interval", "active", "code_text", "code_shape", "evidence",
    "rel_path", "line",
)

#: v6. Its own domain, so a change here cannot move an older domain's digest.
_WORKFLOW_DIGEST_SPEC = (
    ("node_workflow", _WORKFLOW_DIGEST_COLUMNS, "workflow_id"),
    ("node_workflow_state", _WORKFLOW_STATE_DIGEST_COLUMNS, "state_id"),
    ("node_transition", _TRANSITION_DIGEST_COLUMNS, "transition_id"),
    ("node_trigger", _TRIGGER_DIGEST_COLUMNS, "trigger_id"),
    ("node_scheduled_action", _CRON_DIGEST_COLUMNS, "cron_id"),
) + tuple(
    (f"edge_{name}", _EDGE_V2_DIGEST_COLUMNS, "edge_id") for name in WORKFLOW_EDGE_TABLES
)

_DOCUMENT_DIGEST_COLUMNS = (
    "doc_id", "module", "rel_path", "kind", "title", "section_count",
    "line_count", "char_count", "injection_flags", "evidence",
)
_DOC_SECTION_DIGEST_COLUMNS = (
    "section_id", "doc_id", "module", "rel_path", "level", "title", "anchor",
    "line_start", "line_end", "excerpt", "excerpt_truncated", "char_count",
    "injection_flags", "evidence",
)
_TEST_FILE_DIGEST_COLUMNS = (
    "test_file_id", "module", "rel_path", "language", "framework",
    "class_count", "case_count", "parse_state", "evidence",
)
_TEST_CLASS_DIGEST_COLUMNS = (
    "test_class_id", "test_file_id", "module", "class_name", "bases", "tags",
    "case_count", "rel_path", "line", "evidence",
)
_TEST_CASE_DIGEST_COLUMNS = (
    "test_case_id", "test_class_id", "module", "name", "rel_path", "line",
    "evidence",
)
_MIGRATION_DIGEST_COLUMNS = (
    "migration_id", "module", "version", "phase", "script", "rel_path",
    "evidence",
)
_PROCESS_DIGEST_COLUMNS = (
    "process_id", "name", "module", "doc_id", "section_id", "status",
    "step_count", "matched_steps", "workflow_id", "conflict_count", "evidence",
    "rel_path", "line",
)
_PROCESS_STEP_DIGEST_COLUMNS = (
    "step_id", "process_id", "position", "label", "normalised", "state_id",
    "state_value", "match_kind", "evidence", "rel_path", "line",
)
_CONVENTION_DIGEST_COLUMNS = (
    "convention_id", "key", "category", "statement", "status", "module_count",
    "total_modules", "share", "source_rel_path", "source_line", "evidence",
)

#: v7, documentation. Its own domain so that editing a README moves this digest
#: and nothing else.
_DOCUMENTATION_DIGEST_SPEC = (
    ("node_document", _DOCUMENT_DIGEST_COLUMNS, "doc_id"),
    ("node_doc_section", _DOC_SECTION_DIGEST_COLUMNS, "section_id"),
) + tuple(
    (f"edge_{name}", _EDGE_V2_DIGEST_COLUMNS, "edge_id")
    for name in DOCUMENTATION_EDGE_TABLES
)

#: v7, tests and migrations. Both are keyed to a module and a version and both
#: are the repository's record of its own engineering, so they move together
#: and independently of anything a person wrote in prose.
_TEST_DIGEST_SPEC = (
    ("node_test_file", _TEST_FILE_DIGEST_COLUMNS, "test_file_id"),
    ("node_test_class", _TEST_CLASS_DIGEST_COLUMNS, "test_class_id"),
    ("node_test_case", _TEST_CASE_DIGEST_COLUMNS, "test_case_id"),
    ("node_migration", _MIGRATION_DIGEST_COLUMNS, "migration_id"),
) + tuple(
    (f"edge_{name}", _EDGE_V2_DIGEST_COLUMNS, "edge_id")
    for name in TEST_EDGE_TABLES
)

#: v7, guidance -- and the reason it is a domain of its own rather than folded
#: into documentation. Processes and conventions are the only rows in this
#: graph that are *claims about how things work* rather than readings of what
#: the code contains. They change when somebody writes a document, when a test
#: starts covering a step, or when a convention crosses its threshold -- three
#: different sources, none of which is the source of the other two domains.
#: Keeping them apart makes "did the guidance change?" one comparison, and
#: stops a README edit from moving the tests digest.
_PROCESS_DIGEST_SPEC = (
    ("node_business_process", _PROCESS_DIGEST_COLUMNS, "process_id"),
    ("node_process_step", _PROCESS_STEP_DIGEST_COLUMNS, "step_id"),
    ("node_convention", _CONVENTION_DIGEST_COLUMNS, "convention_id"),
) + tuple(
    (f"edge_{name}", _EDGE_V2_DIGEST_COLUMNS, "edge_id")
    for name in PROCESS_EDGE_TABLES
)

_ALLOWED_DIGEST_SPECS = (
    _MODULES_DIGEST_SPEC, _PYTHON_DIGEST_SPEC, _XML_DIGEST_SPEC,
    _SECURITY_DIGEST_SPEC, _FRONTEND_DIGEST_SPEC, _WORKFLOW_DIGEST_SPEC,
    _DOCUMENTATION_DIGEST_SPEC, _TEST_DIGEST_SPEC, _PROCESS_DIGEST_SPEC,
)

#: The identifier vocabulary those specs are allowed to draw on, derived from
#: the specs themselves so the two can never disagree.
_DIGEST_TABLES: frozenset[str] = frozenset(
    table for spec in _ALLOWED_DIGEST_SPECS for table, _columns, _order in spec
)
_DIGEST_COLUMNS: frozenset[str] = frozenset(
    name
    for spec in _ALLOWED_DIGEST_SPECS
    for _table, columns, order in spec
    for name in (*columns, order)
)


@dataclass
class ModuleRow:
    technical_name: str
    manifest_name: str | None
    version: str | None
    version_series_ok: bool
    author: str | None
    license: str | None
    origin: str
    origin_rule: str
    present_in_source: bool
    installable: bool | None
    installable_declared: bool
    installed_state: str
    application: bool | None
    application_declared: bool
    auto_install: bool | None
    auto_install_raw: str | None
    modified_worktree: bool
    manifest_file: str
    manifest_line: int
    module_dir: str
    content_state: str
    manifest_ok: bool
    manifest_error: str | None


@dataclass
class FileRow:
    file_key: str
    display_path: str
    rel_path: str
    include_root: str
    size_bytes: int | None
    content_sha: str | None
    git_blob_sha: str | None
    index_blob_sha: str | None
    content_state: str
    hash_state: str
    base_commit: str | None


@dataclass
class DependsEdge:
    src_module: str
    dst_unresolved: str
    src_manifest_file: str
    src_manifest_line: int
    provenance: str = "manifest.depends"
    dst_module: str | None = None

    @property
    def binding(self) -> str:
        return "resolved" if self.dst_module else "unresolved"

    def edge_id(self, project_id: str) -> str:
        payload = f"{project_id}|{self.src_module}|depends_on|{self.dst_unresolved}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def stable_id(*parts: object) -> str:
    """A deterministic id for a node or edge.

    Identity is built from what the source says, never from iteration order or
    a counter, so the same repository always produces the same ids and the
    digest can be compared across runs and machines.
    """
    payload = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


@dataclass
class ModelRow:
    model_name: str
    kind: str
    definition_count: int
    declares_name: bool
    field_count: int
    method_count: int


@dataclass
class ModelDefRow:
    def_id: str
    model_name: str | None
    module: str | None
    file_key: str
    rel_path: str
    class_name: str
    line: int
    end_line: int | None
    base_kind: str
    declared_name: str | None
    shape: str
    parse_state: str
    field_count: int
    method_count: int


@dataclass
class FieldRow:
    field_id: str
    def_id: str
    model_name: str | None
    field_name: str
    field_type: str
    is_relational: bool
    comodel_literal: str | None
    comodel_state: str
    related_path: str | None
    compute_method: str | None
    inverse_name: str | None
    stored: bool | None
    required: bool | None
    readonly: bool | None
    groups_literal: str | None
    rel_path: str
    line: int


@dataclass
class MethodRow:
    method_id: str
    def_id: str
    model_name: str | None
    method_name: str
    kind: str
    is_private: bool
    decorators: str | None
    depends_literal: str | None
    args_state: str
    rel_path: str
    line: int


@dataclass
class DataFileRow:
    file_id: str
    module: str
    section: str
    load_order: int
    rel_path: str
    file_key: str | None
    kind: str
    parse_state: str
    record_count: int


@dataclass
class XmlIdRow:
    xmlid: str
    module: str
    local_id: str
    definition_count: int
    winner_record_id: str
    winner_rule: str


@dataclass
class XmlRecordRow:
    record_id: str
    xmlid: str | None
    file_id: str
    module: str
    rel_path: str
    line: int
    tag: str
    record_model: str | None
    declared_id: str | None


@dataclass
class AssetBundleRow:
    bundle_name: str
    entry_count: int


@dataclass
class AssetEntryRow:
    entry_id: str
    module: str
    bundle: str
    position: int
    operation: str
    target: str
    extra_target: str | None
    is_glob: bool
    rel_path: str
    line: int


@dataclass
class JsFileRow:
    js_id: str
    module: str
    rel_path: str
    file_key: str
    addon_path: str
    odoo_module: bool
    lex_state: str
    token_count: int
    class_count: int
    import_count: int


@dataclass
class OwlComponentRow:
    component_id: str
    js_id: str
    module: str
    class_name: str
    base: str
    template_name: str | None
    rel_path: str
    line: int


@dataclass
class OwlTemplateRow:
    template_name: str
    module: str
    rel_path: str
    line: int
    inherits: str | None
    primary: str | None


@dataclass
class RouteRow:
    route_id: str
    path: str | None
    module: str
    controller: str
    method: str
    base: str
    auth: str | None
    route_type: str | None
    methods: str | None
    website: bool | None
    csrf: bool | None
    sitemap: str | None
    has_dynamic_argument: bool
    rel_path: str
    line: int


@dataclass
class DocumentRow:
    doc_id: str
    module: str | None
    rel_path: str
    kind: str
    title: str | None
    section_count: int
    line_count: int
    char_count: int
    injection_flags: str | None
    evidence: str


@dataclass
class DocSectionRow:
    section_id: str
    doc_id: str
    module: str | None
    rel_path: str
    level: int
    title: str
    anchor: str
    line_start: int
    line_end: int
    excerpt: str
    excerpt_truncated: bool
    char_count: int
    injection_flags: str | None
    evidence: str


@dataclass
class TestFileRow:
    test_file_id: str
    module: str | None
    rel_path: str
    language: str
    framework: str | None
    class_count: int
    case_count: int
    parse_state: str
    evidence: str


@dataclass
class TestClassRow:
    test_class_id: str
    test_file_id: str
    module: str | None
    class_name: str
    bases: str | None
    tags: str | None
    case_count: int
    rel_path: str
    line: int
    evidence: str


@dataclass
class TestCaseRow:
    test_case_id: str
    test_class_id: str
    module: str | None
    name: str
    rel_path: str
    line: int
    evidence: str


@dataclass
class MigrationRow:
    migration_id: str
    module: str
    version: str
    phase: str
    script: str
    rel_path: str
    evidence: str


@dataclass
class BusinessProcessRow:
    process_id: str
    name: str
    module: str | None
    doc_id: str | None
    section_id: str | None
    status: str
    step_count: int
    matched_steps: int
    workflow_id: str | None
    conflict_count: int
    evidence: str
    rel_path: str
    line: int


@dataclass
class ProcessStepRow:
    step_id: str
    process_id: str
    position: int
    label: str
    normalised: str
    state_id: str | None
    state_value: str | None
    match_kind: str | None
    evidence: str
    rel_path: str
    line: int


@dataclass
class ConventionRow:
    convention_id: str
    key: str
    category: str
    statement: str
    status: str
    module_count: int
    total_modules: int
    share: float
    source_rel_path: str | None
    source_line: int | None
    evidence: str


@dataclass
class WorkflowRow:
    workflow_id: str
    model_name: str
    field_name: str
    field_type: str
    module: str | None
    state_source: str
    state_count: int
    transition_count: int
    has_dynamic_states: bool
    evidence: str
    rel_path: str | None
    line: int | None


@dataclass
class WorkflowStateRow:
    state_id: str
    workflow_id: str
    model_name: str
    field_name: str
    value: str
    label: str | None
    position: int
    evidence: str
    rel_path: str | None
    line: int | None


@dataclass
class TransitionRow:
    transition_id: str
    workflow_id: str
    model_name: str
    field_name: str
    from_state: str | None
    from_evidence: str | None
    to_state: str | None
    operation: str
    receiver: str | None
    method_name: str | None
    method_id: str | None
    conditional: bool
    condition_kind: str | None
    condition_text: str | None
    dynamic_expression: str | None
    evidence: str
    confidence: str
    module: str | None
    rel_path: str
    line: int


@dataclass
class TriggerRow:
    trigger_id: str
    kind: str
    name: str
    module: str | None
    target_model: str | None
    target_method: str | None
    guard_text: str | None
    guard_states: str | None
    guard_state_evidence: str | None
    evidence: str
    rel_path: str
    line: int


@dataclass
class ScheduledActionRow:
    cron_id: str
    xmlid: str | None
    name: str | None
    module: str
    model_ref: str | None
    target_model: str | None
    method_name: str | None
    interval: str | None
    active: str | None
    code_text: str | None
    code_shape: str
    evidence: str
    rel_path: str
    line: int


@dataclass
class GroupRow:
    group_xmlid: str
    module: str
    local_id: str
    name: str | None
    privilege_xmlid: str | None
    rel_path: str
    line: int


@dataclass
class AclRow:
    acl_id: str
    csv_id: str
    xmlid: str
    module: str
    name: str | None
    model_target: str
    group_target: str | None
    is_global: bool
    perm_read: bool
    perm_write: bool
    perm_create: bool
    perm_unlink: bool
    rel_path: str
    line: int


@dataclass
class PrivilegeRow:
    privilege_xmlid: str
    module: str
    local_id: str
    name: str | None
    sequence: int | None
    description: str | None
    placeholder: str | None
    category_xmlid: str | None
    rel_path: str
    line: int


@dataclass
class RuleRow:
    rule_id: str
    xmlid: str | None
    module: str
    name: str | None
    model_target: str
    domain_force: str | None
    is_global: bool
    global_declared: bool | None
    active_declared: bool | None
    perm_read: bool | None
    perm_write: bool | None
    perm_create: bool | None
    perm_unlink: bool | None
    rel_path: str
    line: int


@dataclass(frozen=True)
class GraphEdge:
    """One schema-v2 edge. ``table`` names the edge table it belongs in.

    Frozen because ``table`` reaches SQL as an identifier. Validation in
    ``__post_init__`` is only meaningful if the value cannot change afterwards;
    on a mutable dataclass it would check the value at construction and the
    writer would use whatever the attribute held later. The sink validates
    again regardless -- see :meth:`GraphStore.upsert_edges` -- because a single
    check is a single point of failure no matter how sound it looks.
    """

    table: str
    src_kind: str
    src_id: str
    dst_kind: str
    dst_unresolved: str
    binding: str
    confidence: str
    provenance: str
    dst_id: str | None = None
    src_file: str | None = None
    src_line: int | None = None

    def __post_init__(self) -> None:
        if self.table not in EDGE_TABLES:
            raise ValueError(f"unknown edge table {self.table!r}")
        if self.binding not in EDGE_BINDINGS:
            raise ValueError(f"unknown binding {self.binding!r}")
        if self.confidence not in EDGE_CONFIDENCE:
            raise ValueError(f"unknown confidence {self.confidence!r}")
        # An edge that resolved must point at something; one that did not must
        # not pretend to. Enforced here so no caller can write a contradiction.
        if self.binding in ("resolved", "derived") and not self.dst_id:
            raise ValueError(
                f"{self.table} edge is {self.binding} but carries no dst_id"
            )
        if self.binding in ("external", "unresolved") and self.dst_id:
            raise ValueError(
                f"{self.table} edge is {self.binding} but carries dst_id "
                f"{self.dst_id!r}; unresolved targets must not create nodes"
            )

    def edge_id(self, project_id: str) -> str:
        return stable_id(
            project_id, self.table, self.src_id, self.dst_unresolved, self.provenance
        )


@dataclass
class NodeDiagnostic:
    """A finding attached to a node rather than to a bare file path."""

    severity: str
    category: str
    message: str
    node_kind: str
    node_id: str
    rel_path: str | None = None
    line: int | None = None

    def link_id(self, run_id: str) -> str:
        # severity participates: two findings that differ only in how serious
        # they are are two findings, and writes use ON CONFLICT DO NOTHING, so
        # leaving it out would silently keep whichever arrived first.
        return stable_id(
            run_id, self.severity, self.category, self.node_kind, self.node_id,
            self.message, self.line,
        )


#: The first sixteen bytes of every SQLite file. Checked as raw bytes, before
#: sqlite3 is asked to open anything, so a file that is not a database at all is
#: turned away without a library ever touching it.
SQLITE_MAGIC = b"SQLite format 3\x00"

#: Tables a graph must already have for this build to accept an existing file.
#: Deliberately the two that carry identity rather than data: a file missing
#: either is not one of ours, whatever else it contains.
REQUIRED_IDENTITY_TABLES = ("kg_meta", "project")

#: Journal sidecars. Their presence means another connection may be mid-write,
#: and their absence afterwards is part of what "we did not touch it" means.
JOURNAL_SIDECARS = ("-wal", "-shm", "-journal")


def _read_only_uri(db_path: str) -> str:
    """A URI that cannot create, cannot write, and needs no shared memory.

    ``immutable=1`` is what keeps this from creating a ``-shm`` file: without
    it, SQLite needs shared memory to read a WAL-mode database even read-only,
    and creating that file would be a write to the directory we promised to
    leave alone. The trade-off is that an immutable open ignores any pending
    ``-wal``, so a file that has one is refused outright rather than read from
    a possibly stale snapshot.
    """
    # as_uri() rejects a relative path. Resolving first means a caller who
    # passes one gets the ordinary refusal rather than a ValueError from deep
    # inside pathlib, and the quoting of odd characters stays pathlib's job.
    return f"{Path(os.path.abspath(db_path)).as_uri()}?mode=ro&immutable=1"


def inspect_existing(db_path: str, *, rebuild: bool = False,
                     expect_project_id: str | None = None) -> int | None:
    """Decide whether an existing file is this build's to write.

    Opens strictly read-only and never as anything else. Returns the schema
    version found, or ``None`` when the file does not exist yet. Raises rather
    than return when the file exists but is not ours.

    Nothing here writes: no schema is created, no pragma that persists anything
    is issued, and no journal sidecar is left behind. That is the whole point.
    A refusal that modified the file it refused would not be a refusal.
    """
    if not os.path.exists(db_path):
        return None

    if not os.path.isfile(db_path):
        raise SchemaVersionError(
            f"refusing to use {db_path}: it exists but is not a regular file."
        )

    stray = [
        suffix for suffix in JOURNAL_SIDECARS
        if os.path.exists(db_path + suffix)
    ]
    if stray:
        raise SchemaVersionError(
            f"refusing to open {db_path}: journal files {stray} are present, so "
            "another process may be writing to it right now. Inspecting it "
            "safely would mean reading a snapshot that could already be stale. "
            "Let the other process finish, or remove the journals deliberately."
        )

    try:
        with open(db_path, "rb") as handle:
            header = handle.read(len(SQLITE_MAGIC))
    except OSError as exc:
        raise SchemaVersionError(f"cannot read {db_path}: {exc}") from exc

    if header != SQLITE_MAGIC:
        raise SchemaVersionError(
            f"refusing to open {db_path}: it is not a SQLite database "
            f"(header {header!r}). Point --config at the right file, or move "
            "this one out of the way."
        )

    try:
        connection = sqlite3.connect(_read_only_uri(db_path), uri=True,
                                     timeout=5.0)
    except sqlite3.Error as exc:
        raise SchemaVersionError(
            f"cannot open {db_path} read-only for inspection: {exc}"
        ) from exc

    connection.row_factory = sqlite3.Row
    try:
        # Belt and braces on a connection that is already mode=ro: a mutating
        # statement issued by mistake is rejected by SQLite itself.
        connection.execute("PRAGMA query_only = ON")
        try:
            present = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        except sqlite3.DatabaseError as exc:
            raise SchemaVersionError(
                f"refusing to open {db_path}: it is not a readable SQLite "
                f"database ({exc})."
            ) from exc

        missing = [name for name in REQUIRED_IDENTITY_TABLES
                   if name not in present]
        if missing:
            raise SchemaVersionError(
                f"refusing to open {db_path}: it is a SQLite database but not "
                f"an odoo-kg graph -- missing {missing}. Opening it for writing "
                "would add this build's tables to somebody else's file. If this "
                "really is a graph of yours that got damaged, delete the file "
                "and index again -- rebuilding cannot repair it, because "
                "without those tables there is no way to tell whose it is."
            )

        version = _schema_version_of(connection)
        if version is None:
            raise SchemaVersionError(
                f"refusing to open {db_path}: it has an odoo-kg shape but no "
                "readable schema_version in kg_meta, so which build wrote it "
                "cannot be established. Delete the file and index again if it "
                "is yours."
            )
        if version != SCHEMA_VERSION:
            action = "--rebuild" if rebuild else "open"
            consequence = (
                "Rebuilding would destroy a graph written by another build."
                if rebuild
                else "Opening it for writing would add this build's tables to it."
            )
            raise SchemaVersionError(
                f"refusing to {action} {db_path}: it holds schema version "
                f"{version} and this build writes {SCHEMA_VERSION}. "
                f"{consequence} Point --config at this milestone's own project, "
                "or delete the database deliberately first."
            )

        if expect_project_id is not None:
            found = _project_ids_of(connection)
            if found and expect_project_id not in found:
                raise SchemaVersionError(
                    f"refusing to open {db_path}: it holds project "
                    f"{sorted(found)} and this run is for "
                    f"{expect_project_id!r}. Two projects must never share a "
                    "database -- every node and edge id is derived from the "
                    "project id, so mixing them would corrupt both graphs."
                )
        return version
    finally:
        connection.close()


def _schema_version_of(connection) -> int | None:
    try:
        row = connection.execute(
            "SELECT value FROM kg_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def _project_ids_of(connection) -> set[str]:
    try:
        return {
            row["project_id"]
            for row in connection.execute("SELECT project_id FROM project")
            if row["project_id"]
        }
    except sqlite3.DatabaseError:
        return set()


class GraphReads:
    """Every query the graph answers, and nothing that writes.

    Split out of :class:`GraphStore` so that a read-only reader can serve the
    exact same methods over a ``mode=ro`` connection. The point is that
    ``stats`` and ``verify`` run the same digest and counting code whether they
    are handed a writer or a reader -- two implementations would drift, and the
    one nobody exercised would be the one that lied.

    The only attribute this needs is ``self.connection``. It never commits.
    """

    connection: sqlite3.Connection

    def count(self, table: str) -> int:
        if table not in {
            "node_file", "node_module", "edge_depends_on", "diagnostic",
            "index_run", "project", "diagnostic_node", "run_digest",
        } | set(V2_NODE_TABLES) | set(V3_NODE_TABLES) | set(V4_NODE_TABLES) | set(
            V5_NODE_TABLES
        ) | set(V6_NODE_TABLES) | set(V7_NODE_TABLES) | set(V2_EDGE_TABLES):
            raise ValueError(f"unknown table {table!r}")
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _digest_over(self, spec) -> str:
        """Hash the rows a digest spec names.

        Every table and column here is interpolated into SQL, so the spec is
        checked before a single identifier is formatted: it must be one of the
        two module-level specs by identity, and each name in it must appear in
        the vocabulary built from those same constants. A caller cannot reach
        this with an identifier of its own choosing, and a future spec that
        forgets to register itself fails loudly instead of quietly building SQL
        from whatever it was handed.
        """
        if not any(spec is allowed for allowed in _ALLOWED_DIGEST_SPECS):
            raise ValueError(
                "_digest_over accepts only the predefined digest specs; "
                "register a new one in _ALLOWED_DIGEST_SPECS rather than "
                "passing table or column names in"
            )
        for table, columns, order in spec:
            unknown = (
                {table} - _DIGEST_TABLES
                | set(columns) - _DIGEST_COLUMNS
                | {order} - _DIGEST_COLUMNS
            )
            if unknown:
                raise ValueError(f"digest spec names unknown identifiers: {sorted(unknown)}")

        digest = hashlib.sha256()
        for table, columns, order in spec:
            digest.update(table.encode("utf-8"))
            selected = ", ".join(columns)
            for row in self.connection.execute(
                f"SELECT {selected} FROM {table} ORDER BY {order}"
            ):
                digest.update(repr(tuple(row)).encode("utf-8"))
        return digest.hexdigest()

    def graph_digest(self) -> str:
        """A content hash of the module layer, ignoring per-run bookkeeping.

        Unchanged from M1 on purpose, including the table order. An M3 database
        and an M2 database built from the same commit must agree here, and that
        equality is what proves the Python layer was additive.
        """
        return self._digest_over(_MODULES_DIGEST_SPEC)

    def python_digest(self) -> str:
        """A content hash of the Python layer."""
        return self._digest_over(_PYTHON_DIGEST_SPEC)

    def xml_digest(self) -> str:
        """A content hash of the XML layer."""
        return self._digest_over(_XML_DIGEST_SPEC)

    def security_digest(self) -> str:
        """A content hash of the security layer."""
        return self._digest_over(_SECURITY_DIGEST_SPEC)

    def frontend_digest(self) -> str:
        """A content hash of the frontend layer."""
        return self._digest_over(_FRONTEND_DIGEST_SPEC)

    def workflow_digest(self) -> str:
        """Schema v6 only: workflows, states, transitions, triggers, crons."""
        return self._digest_over(_WORKFLOW_DIGEST_SPEC)

    def documentation_digest(self) -> str:
        """Schema v7: documents and their sections."""
        return self._digest_over(_DOCUMENTATION_DIGEST_SPEC)

    def tests_digest(self) -> str:
        """Schema v7: test files, classes, cases and migrations."""
        return self._digest_over(_TEST_DIGEST_SPEC)

    def process_digest(self) -> str:
        """Schema v7: documented processes, their steps, and conventions."""
        return self._digest_over(_PROCESS_DIGEST_SPEC)

    def domain_digests(self) -> dict[str, str]:
        """One digest per domain, plus their combination.

        ``total`` hashes the domain digests rather than re-reading every table,
        so a new domain cannot silently change what an existing one means. The
        names are written into the hashed text, so adding a domain changes
        ``total`` and nothing else -- which is exactly the signal wanted.
        """
        domains = {
            "modules": self.graph_digest(),
            "python": self.python_digest(),
            "xml": self.xml_digest(),
            "security": self.security_digest(),
            "frontend": self.frontend_digest(),
            "workflow": self.workflow_digest(),
            "documentation": self.documentation_digest(),
            "tests": self.tests_digest(),
            "process": self.process_digest(),
        }
        payload = "".join(f"{name}={domains[name]}\n" for name in sorted(domains))
        return {**domains, "total": hashlib.sha256(payload.encode("utf-8")).hexdigest()}

    def digests_for_run(self, run_id: str) -> dict[str, str]:
        return {
            row["domain"]: row["digest"]
            for row in self.connection.execute(
                "SELECT domain, digest FROM run_digest WHERE run_id = ?", (run_id,)
            )
        }

    def modules(self) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM node_module ORDER BY technical_name"
            )
        )

    def project_row(self, project_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM project WHERE project_id = ?", (project_id,)
        ).fetchone()


class GraphStore(GraphReads):
    """Thin, explicit wrapper over the SQLite graph database."""

    def __init__(self, db_path: str, *, rebuild: bool = False,
                 expect_project_id: str | None = None):
        self.db_path = db_path

        # An existing file is judged before this process ever holds a writable
        # handle on it. `inspect_existing` opens strictly read-only, and if it
        # refuses, no write connection is created at all.
        #
        # The order matters more than it looks. Opening read-write and checking
        # afterwards was not enough, because two things write before any of our
        # SQL runs: `PRAGMA journal_mode = DELETE` persists the journal mode in
        # header bytes 18-19, so a database left in WAL mode by another tool was
        # rewritten to rollback mode by the very call that then refused it. The
        # file came back byte-different from an operation that had declined to
        # touch it.
        inspect_existing(
            db_path, rebuild=rebuild, expect_project_id=expect_project_id
        )

        self.connection = sqlite3.connect(db_path)
        self.connection.row_factory = sqlite3.Row
        try:
            # Checked again, through the writable handle, and before anything
            # that writes. Between the read-only inspection and this line
            # another process could in principle have replaced the file, so the
            # re-read closes that window -- and it has to come first, because
            # `PRAGMA journal_mode` is itself a write to the header. Nothing
            # above this point has modified the file.
            self._assert_existing_schema_is_ours(rebuild=rebuild)
            self._assert_project_is_ours(expect_project_id)

            # Session-only, so it changes nothing on disk.
            self.connection.execute("PRAGMA foreign_keys = ON")
            # Persistent: this one lands in header bytes 18-19. Keeping the
            # default rollback journal leaves no stray -wal/-shm files in the
            # data directory between runs, and running it only here means it
            # can only ever touch a file already accepted as ours.
            self.connection.execute("PRAGMA journal_mode = DELETE")
            if rebuild:
                self._drop_all()
            # `executescript` issues an implicit COMMIT before it runs, so
            # `CREATE TABLE IF NOT EXISTS` lands in the file even if the
            # connection is abandoned a moment later. Nothing above may fail
            # after this point without the file already being ours.
            self.connection.executescript(SCHEMA_SQL)
            self.connection.executescript(SCHEMA_V2_SQL)
            self._record_versions()
            self.connection.commit()
        except BaseException:
            # A rejected database must not leave an open handle behind; on
            # Windows that would keep the file locked for the rest of the
            # process.
            self.connection.close()
            raise

    # -- lifecycle -------------------------------------------------------

    def _existing_schema_version(self) -> int | None:
        """The version in the file, through this connection.

        Returns None for a fresh file or one with no ``kg_meta`` yet.
        """
        return _schema_version_of(self.connection)

    def _assert_existing_schema_is_ours(self, *, rebuild: bool) -> None:
        """The second look, through the writable handle.

        :func:`inspect_existing` has already refused anything that is not ours,
        using a connection that could not write. This repeats the check once the
        writable connection exists, so a file swapped in between the two opens
        is still caught before the first DDL.
        """
        version = self._existing_schema_version()
        if version is None or version == SCHEMA_VERSION:
            return
        action = "--rebuild" if rebuild else "open"
        consequence = (
            "Rebuilding would destroy a graph written by another build."
            if rebuild
            else "Opening it for writing would add this build's tables to it."
        )
        raise SchemaVersionError(
            f"refusing to {action} {self.db_path}: it holds schema version "
            f"{version} and this build writes {SCHEMA_VERSION}. {consequence} "
            "Point --config at this milestone's own project, or delete the "
            "database deliberately first."
        )

    def _assert_project_is_ours(self, expect_project_id: str | None) -> None:
        """The same second look, for project identity.

        Every node and edge id is derived from the project id, so a run that
        wrote into another project's database would produce ids that belong to
        neither graph.
        """
        if expect_project_id is None:
            return
        found = _project_ids_of(self.connection)
        if found and expect_project_id not in found:
            raise SchemaVersionError(
                f"refusing to write to {self.db_path}: it holds project "
                f"{sorted(found)} and this run is for {expect_project_id!r}."
            )

    def _drop_all(self) -> None:
        for table in (
            "diagnostic", "edge_depends_on", "node_module", "node_file",
            "index_run", "project", "kg_meta",
        ) + V2_EDGE_TABLES + V2_NODE_TABLES + V3_NODE_TABLES + V4_NODE_TABLES + (
            V5_NODE_TABLES + ("diagnostic_node", "run_digest")
        ):
            self.connection.execute(f"DROP TABLE IF EXISTS {table}")
        self.connection.commit()

    def _record_versions(self) -> None:
        """Stamp the schema and indexer versions.

        Only ever reached once :meth:`_assert_existing_schema_is_ours` has
        confirmed the file is this build's to write, so there is no version
        check here any more -- doing it after the tables exist was the bug.
        """
        self.connection.execute(
            "INSERT OR IGNORE INTO kg_meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.connection.execute(
            "INSERT OR REPLACE INTO kg_meta (key, value) VALUES ('indexer_version', ?)",
            (__version__,),
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- writes ----------------------------------------------------------

    def start_run(self, run_id: str, project_id: str, started_at: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO index_run (run_id, project_id, started_at, status) "
            "VALUES (?, ?, ?, 'running')",
            (run_id, project_id, started_at),
        )
        # Diagnostics describe the current run only; keeping history here would
        # make the table grow without bound on every re-index.
        self.connection.execute("DELETE FROM diagnostic")
        self.connection.execute("DELETE FROM diagnostic_node")
        self.connection.commit()

    def upsert_project(self, values: dict) -> None:
        self.connection.execute(
            """
            INSERT INTO project (
                project_id, repo_root, core_root, branch, base_commit, dirty,
                git_available, git_error, indexed_at, schema_version,
                indexer_version, milestone, run_id
            ) VALUES (
                :project_id, :repo_root, :core_root, :branch, :base_commit, :dirty,
                :git_available, :git_error, :indexed_at, :schema_version,
                :indexer_version, :milestone, :run_id
            )
            ON CONFLICT (project_id) DO UPDATE SET
                repo_root = excluded.repo_root,
                core_root = excluded.core_root,
                branch = excluded.branch,
                base_commit = excluded.base_commit,
                dirty = excluded.dirty,
                git_available = excluded.git_available,
                git_error = excluded.git_error,
                indexed_at = excluded.indexed_at,
                schema_version = excluded.schema_version,
                indexer_version = excluded.indexer_version,
                milestone = excluded.milestone,
                run_id = excluded.run_id
            """,
            values,
        )

    def upsert_files(self, rows: Iterable[FileRow], run_id: str) -> int:
        payload = [
            (
                row.file_key, row.display_path, row.rel_path, row.include_root,
                row.size_bytes, row.content_sha, row.git_blob_sha,
                row.index_blob_sha, row.content_state, row.hash_state,
                row.base_commit, run_id,
            )
            for row in rows
        ]
        self.connection.executemany(
            """
            INSERT INTO node_file (
                file_key, display_path, rel_path, include_root, size_bytes,
                content_sha, git_blob_sha, index_blob_sha, content_state,
                hash_state, base_commit, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (file_key) DO UPDATE SET
                display_path = excluded.display_path,
                rel_path = excluded.rel_path,
                include_root = excluded.include_root,
                size_bytes = excluded.size_bytes,
                content_sha = excluded.content_sha,
                git_blob_sha = excluded.git_blob_sha,
                index_blob_sha = excluded.index_blob_sha,
                content_state = excluded.content_state,
                hash_state = excluded.hash_state,
                base_commit = excluded.base_commit,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_modules(self, rows: Iterable[ModuleRow], run_id: str) -> int:
        payload = [
            (
                row.technical_name, row.manifest_name, row.version,
                int(row.version_series_ok), row.author, row.license, row.origin,
                row.origin_rule, int(row.present_in_source),
                _opt_bool(row.installable), int(row.installable_declared),
                row.installed_state, _opt_bool(row.application),
                int(row.application_declared), _opt_bool(row.auto_install),
                row.auto_install_raw, int(row.modified_worktree),
                row.manifest_file, row.manifest_line, row.module_dir,
                row.content_state, int(row.manifest_ok), row.manifest_error,
                run_id,
            )
            for row in rows
        ]
        self.connection.executemany(
            """
            INSERT INTO node_module (
                technical_name, manifest_name, version, version_series_ok, author,
                license, origin, origin_rule, present_in_source, installable,
                installable_declared, installed_state, application,
                application_declared, auto_install, auto_install_raw,
                modified_worktree, manifest_file, manifest_line, module_dir,
                content_state, manifest_ok, manifest_error, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (technical_name) DO UPDATE SET
                manifest_name = excluded.manifest_name,
                version = excluded.version,
                version_series_ok = excluded.version_series_ok,
                author = excluded.author,
                license = excluded.license,
                origin = excluded.origin,
                origin_rule = excluded.origin_rule,
                present_in_source = excluded.present_in_source,
                installable = excluded.installable,
                installable_declared = excluded.installable_declared,
                installed_state = excluded.installed_state,
                application = excluded.application,
                application_declared = excluded.application_declared,
                auto_install = excluded.auto_install,
                auto_install_raw = excluded.auto_install_raw,
                modified_worktree = excluded.modified_worktree,
                manifest_file = excluded.manifest_file,
                manifest_line = excluded.manifest_line,
                module_dir = excluded.module_dir,
                content_state = excluded.content_state,
                manifest_ok = excluded.manifest_ok,
                manifest_error = excluded.manifest_error,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_depends(
        self, edges: Sequence[DependsEdge], project_id: str, run_id: str
    ) -> int:
        payload = [
            (
                edge.edge_id(project_id), edge.src_module, edge.dst_module,
                edge.dst_unresolved, edge.binding, edge.src_manifest_file,
                edge.src_manifest_line, edge.provenance, run_id,
            )
            for edge in edges
        ]
        self.connection.executemany(
            """
            INSERT INTO edge_depends_on (
                edge_id, src_module, dst_module, dst_unresolved, binding,
                src_manifest_file, src_manifest_line, provenance, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (edge_id) DO UPDATE SET
                src_module = excluded.src_module,
                dst_module = excluded.dst_module,
                binding = excluded.binding,
                src_manifest_file = excluded.src_manifest_file,
                src_manifest_line = excluded.src_manifest_line,
                provenance = excluded.provenance,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    # -- schema v2 writes ------------------------------------------------

    def upsert_models(self, rows: Iterable[ModelRow], run_id: str) -> int:
        payload = [
            (
                row.model_name, row.kind, row.definition_count,
                int(row.declares_name), row.field_count, row.method_count, run_id,
            )
            for row in rows
        ]
        self.connection.executemany(
            """
            INSERT INTO node_model (
                model_name, kind, definition_count, declares_name, field_count,
                method_count, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (model_name) DO UPDATE SET
                kind = excluded.kind,
                definition_count = excluded.definition_count,
                declares_name = excluded.declares_name,
                field_count = excluded.field_count,
                method_count = excluded.method_count,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_model_defs(self, rows: Iterable[ModelDefRow], run_id: str) -> int:
        payload = [
            (
                row.def_id, row.model_name, row.module, row.file_key, row.rel_path,
                row.class_name, row.line, row.end_line, row.base_kind,
                row.declared_name, row.shape, row.parse_state, row.field_count,
                row.method_count, run_id,
            )
            for row in rows
        ]
        self.connection.executemany(
            """
            INSERT INTO node_model_def (
                def_id, model_name, module, file_key, rel_path, class_name, line,
                end_line, base_kind, declared_name, shape, parse_state,
                field_count, method_count, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (def_id) DO UPDATE SET
                model_name = excluded.model_name,
                module = excluded.module,
                file_key = excluded.file_key,
                rel_path = excluded.rel_path,
                class_name = excluded.class_name,
                line = excluded.line,
                end_line = excluded.end_line,
                base_kind = excluded.base_kind,
                declared_name = excluded.declared_name,
                shape = excluded.shape,
                parse_state = excluded.parse_state,
                field_count = excluded.field_count,
                method_count = excluded.method_count,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_fields(self, rows: Iterable[FieldRow], run_id: str) -> int:
        payload = [
            (
                row.field_id, row.def_id, row.model_name, row.field_name,
                row.field_type, int(row.is_relational), row.comodel_literal,
                row.comodel_state, row.related_path, row.compute_method,
                row.inverse_name, _opt_bool(row.stored), _opt_bool(row.required),
                _opt_bool(row.readonly), row.groups_literal, row.rel_path,
                row.line, run_id,
            )
            for row in rows
        ]
        self.connection.executemany(
            """
            INSERT INTO node_field (
                field_id, def_id, model_name, field_name, field_type,
                is_relational, comodel_literal, comodel_state, related_path,
                compute_method, inverse_name, stored, required, readonly,
                groups_literal, rel_path, line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (field_id) DO UPDATE SET
                def_id = excluded.def_id,
                model_name = excluded.model_name,
                field_name = excluded.field_name,
                field_type = excluded.field_type,
                is_relational = excluded.is_relational,
                comodel_literal = excluded.comodel_literal,
                comodel_state = excluded.comodel_state,
                related_path = excluded.related_path,
                compute_method = excluded.compute_method,
                inverse_name = excluded.inverse_name,
                stored = excluded.stored,
                required = excluded.required,
                readonly = excluded.readonly,
                groups_literal = excluded.groups_literal,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_methods(self, rows: Iterable[MethodRow], run_id: str) -> int:
        payload = [
            (
                row.method_id, row.def_id, row.model_name, row.method_name,
                row.kind, int(row.is_private), row.decorators,
                row.depends_literal, row.args_state, row.rel_path, row.line,
                run_id,
            )
            for row in rows
        ]
        self.connection.executemany(
            """
            INSERT INTO node_method (
                method_id, def_id, model_name, method_name, kind, is_private,
                decorators, depends_literal, args_state, rel_path, line,
                last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (method_id) DO UPDATE SET
                def_id = excluded.def_id,
                model_name = excluded.model_name,
                method_name = excluded.method_name,
                kind = excluded.kind,
                is_private = excluded.is_private,
                decorators = excluded.decorators,
                depends_literal = excluded.depends_literal,
                args_state = excluded.args_state,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    # -- schema v3 writes ------------------------------------------------

    def upsert_data_files(self, rows: Iterable[DataFileRow], run_id: str) -> int:
        payload = [
            (
                row.file_id, row.module, row.section, row.load_order, row.rel_path,
                row.file_key, row.kind, row.parse_state, row.record_count, run_id,
            )
            for row in rows
        ]
        self.connection.executemany(
            """
            INSERT INTO node_data_file (
                file_id, module, section, load_order, rel_path, file_key, kind,
                parse_state, record_count, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (file_id) DO UPDATE SET
                module = excluded.module,
                section = excluded.section,
                load_order = excluded.load_order,
                rel_path = excluded.rel_path,
                file_key = excluded.file_key,
                kind = excluded.kind,
                parse_state = excluded.parse_state,
                record_count = excluded.record_count,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_xmlids(self, rows: Iterable[XmlIdRow], run_id: str) -> int:
        payload = [
            (
                row.xmlid, row.module, row.local_id, row.definition_count,
                row.winner_record_id, row.winner_rule, run_id,
            )
            for row in rows
        ]
        self.connection.executemany(
            """
            INSERT INTO node_xmlid (
                xmlid, module, local_id, definition_count, winner_record_id,
                winner_rule, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (xmlid) DO UPDATE SET
                module = excluded.module,
                local_id = excluded.local_id,
                definition_count = excluded.definition_count,
                winner_record_id = excluded.winner_record_id,
                winner_rule = excluded.winner_rule,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_xml_records(self, rows: Iterable[XmlRecordRow], run_id: str) -> int:
        payload = [
            (
                row.record_id, row.xmlid, row.declared_id, row.file_id, row.module,
                row.rel_path, row.line, row.tag, row.record_model, run_id,
            )
            for row in rows
        ]
        self.connection.executemany(
            """
            INSERT INTO node_xml_record (
                record_id, xmlid, declared_id, file_id, module, rel_path, line,
                tag, record_model, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (record_id) DO UPDATE SET
                xmlid = excluded.xmlid,
                declared_id = excluded.declared_id,
                file_id = excluded.file_id,
                module = excluded.module,
                rel_path = excluded.rel_path,
                line = excluded.line,
                tag = excluded.tag,
                record_model = excluded.record_model,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    # -- schema v4 writes ------------------------------------------------

    def upsert_groups(self, rows: Iterable[GroupRow], run_id: str) -> int:
        payload = [
            (
                row.group_xmlid, row.module, row.local_id, row.name,
                row.privilege_xmlid, row.rel_path, row.line, run_id,
            )
            for row in rows
        ]
        self.connection.executemany(
            """
            INSERT INTO node_group (
                group_xmlid, module, local_id, name, privilege_xmlid, rel_path,
                line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (group_xmlid) DO UPDATE SET
                module = excluded.module,
                local_id = excluded.local_id,
                name = excluded.name,
                privilege_xmlid = excluded.privilege_xmlid,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_acls(self, rows: Iterable[AclRow], run_id: str) -> int:
        payload = [
            (
                row.acl_id, row.csv_id, row.xmlid, row.module, row.name,
                row.model_target, row.group_target, int(row.is_global),
                int(row.perm_read), int(row.perm_write), int(row.perm_create),
                int(row.perm_unlink), row.rel_path, row.line, run_id,
            )
            for row in rows
        ]
        self.connection.executemany(
            """
            INSERT INTO node_acl (
                acl_id, csv_id, xmlid, module, name, model_target, group_target,
                is_global, perm_read, perm_write, perm_create, perm_unlink,
                rel_path, line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (acl_id) DO UPDATE SET
                csv_id = excluded.csv_id,
                xmlid = excluded.xmlid,
                module = excluded.module,
                name = excluded.name,
                model_target = excluded.model_target,
                group_target = excluded.group_target,
                is_global = excluded.is_global,
                perm_read = excluded.perm_read,
                perm_write = excluded.perm_write,
                perm_create = excluded.perm_create,
                perm_unlink = excluded.perm_unlink,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_rules(self, rows: Iterable[RuleRow], run_id: str) -> int:
        payload = [
            (
                row.rule_id, row.xmlid, row.module, row.name, row.model_target,
                row.domain_force, int(row.is_global), _opt_bool(row.global_declared),
                _opt_bool(row.active_declared), _opt_bool(row.perm_read),
                _opt_bool(row.perm_write), _opt_bool(row.perm_create),
                _opt_bool(row.perm_unlink), row.rel_path, row.line, run_id,
            )
            for row in rows
        ]
        self.connection.executemany(
            """
            INSERT INTO node_rule (
                rule_id, xmlid, module, name, model_target, domain_force,
                is_global, global_declared, active_declared, perm_read,
                perm_write, perm_create, perm_unlink, rel_path, line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (rule_id) DO UPDATE SET
                xmlid = excluded.xmlid,
                module = excluded.module,
                name = excluded.name,
                model_target = excluded.model_target,
                domain_force = excluded.domain_force,
                is_global = excluded.is_global,
                global_declared = excluded.global_declared,
                active_declared = excluded.active_declared,
                perm_read = excluded.perm_read,
                perm_write = excluded.perm_write,
                perm_create = excluded.perm_create,
                perm_unlink = excluded.perm_unlink,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_privileges(self, rows: Iterable[PrivilegeRow], run_id: str) -> int:
        payload = [
            (
                row.privilege_xmlid, row.module, row.local_id, row.name,
                row.sequence, row.description, row.placeholder,
                row.category_xmlid, row.rel_path, row.line, run_id,
            )
            for row in rows
        ]
        self.connection.executemany(
            """
            INSERT INTO node_privilege (
                privilege_xmlid, module, local_id, name, sequence, description,
                placeholder, category_xmlid, rel_path, line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (privilege_xmlid) DO UPDATE SET
                module = excluded.module,
                local_id = excluded.local_id,
                name = excluded.name,
                sequence = excluded.sequence,
                description = excluded.description,
                placeholder = excluded.placeholder,
                category_xmlid = excluded.category_xmlid,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)


    # -- schema v5 writes ------------------------------------------------

    def upsert_asset_bundles(self, rows, run_id: str) -> int:
        payload = [(row.bundle_name, row.entry_count, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_asset_bundle (
                bundle_name, entry_count, last_run_id
            ) VALUES (?, ?, ?)
            ON CONFLICT (bundle_name) DO UPDATE SET
                entry_count = excluded.entry_count,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_asset_entries(self, rows, run_id: str) -> int:
        payload = [(row.entry_id, row.module, row.bundle, row.position, row.operation, row.target, row.extra_target, int(row.is_glob), row.rel_path, row.line, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_asset_entry (
                entry_id, module, bundle, position, operation, target, extra_target, is_glob, rel_path, line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (entry_id) DO UPDATE SET
                module = excluded.module,
                bundle = excluded.bundle,
                position = excluded.position,
                operation = excluded.operation,
                target = excluded.target,
                extra_target = excluded.extra_target,
                is_glob = excluded.is_glob,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_js_files(self, rows, run_id: str) -> int:
        payload = [(row.js_id, row.module, row.rel_path, row.file_key, row.addon_path, int(row.odoo_module), row.lex_state, row.token_count, row.class_count, row.import_count, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_js_file (
                js_id, module, rel_path, file_key, addon_path, odoo_module, lex_state, token_count, class_count, import_count, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (js_id) DO UPDATE SET
                module = excluded.module,
                rel_path = excluded.rel_path,
                file_key = excluded.file_key,
                addon_path = excluded.addon_path,
                odoo_module = excluded.odoo_module,
                lex_state = excluded.lex_state,
                token_count = excluded.token_count,
                class_count = excluded.class_count,
                import_count = excluded.import_count,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_owl_components(self, rows, run_id: str) -> int:
        payload = [(row.component_id, row.js_id, row.module, row.class_name, row.base, row.template_name, row.rel_path, row.line, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_owl_component (
                component_id, js_id, module, class_name, base, template_name, rel_path, line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (component_id) DO UPDATE SET
                js_id = excluded.js_id,
                module = excluded.module,
                class_name = excluded.class_name,
                base = excluded.base,
                template_name = excluded.template_name,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_owl_templates(self, rows, run_id: str) -> int:
        payload = [(row.template_name, row.module, row.rel_path, row.line, row.inherits, row.primary, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_owl_template (
                template_name, module, rel_path, line, inherits, primary_mode, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (template_name) DO UPDATE SET
                module = excluded.module,
                rel_path = excluded.rel_path,
                line = excluded.line,
                inherits = excluded.inherits,
                primary_mode = excluded.primary_mode,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_routes(self, rows, run_id: str) -> int:
        payload = [(row.route_id, row.path, row.module, row.controller, row.method, row.base, row.auth, row.route_type, row.methods, _opt_bool(row.website), _opt_bool(row.csrf), row.sitemap, int(row.has_dynamic_argument), row.rel_path, row.line, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_route (
                route_id, path, module, controller, method, base, auth, route_type, methods, website, csrf, sitemap, has_dynamic_argument, rel_path, line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (route_id) DO UPDATE SET
                path = excluded.path,
                module = excluded.module,
                controller = excluded.controller,
                method = excluded.method,
                base = excluded.base,
                auth = excluded.auth,
                route_type = excluded.route_type,
                methods = excluded.methods,
                website = excluded.website,
                csrf = excluded.csrf,
                sitemap = excluded.sitemap,
                has_dynamic_argument = excluded.has_dynamic_argument,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)


    # -- schema v6 writes ------------------------------------------------

    def upsert_workflows(self, rows, run_id: str) -> int:
        payload = [(row.workflow_id, row.model_name, row.field_name, row.field_type, row.module, row.state_source, row.state_count, row.transition_count, int(row.has_dynamic_states), row.evidence, row.rel_path, row.line, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_workflow (
                workflow_id, model_name, field_name, field_type, module, state_source, state_count, transition_count, has_dynamic_states, evidence, rel_path, line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (workflow_id) DO UPDATE SET
                model_name = excluded.model_name,
                field_name = excluded.field_name,
                field_type = excluded.field_type,
                module = excluded.module,
                state_source = excluded.state_source,
                state_count = excluded.state_count,
                transition_count = excluded.transition_count,
                has_dynamic_states = excluded.has_dynamic_states,
                evidence = excluded.evidence,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_workflow_states(self, rows, run_id: str) -> int:
        payload = [(row.state_id, row.workflow_id, row.model_name, row.field_name, row.value, row.label, row.position, row.evidence, row.rel_path, row.line, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_workflow_state (
                state_id, workflow_id, model_name, field_name, value, label, position, evidence, rel_path, line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (state_id) DO UPDATE SET
                workflow_id = excluded.workflow_id,
                model_name = excluded.model_name,
                field_name = excluded.field_name,
                value = excluded.value,
                label = excluded.label,
                position = excluded.position,
                evidence = excluded.evidence,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_transitions(self, rows, run_id: str) -> int:
        payload = [(row.transition_id, row.workflow_id, row.model_name, row.field_name, row.from_state, row.from_evidence, row.to_state, row.operation, row.receiver, row.method_name, row.method_id, int(row.conditional), row.condition_kind, row.condition_text, row.dynamic_expression, row.evidence, row.confidence, row.module, row.rel_path, row.line, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_transition (
                transition_id, workflow_id, model_name, field_name, from_state, from_evidence, to_state, operation, receiver, method_name, method_id, conditional, condition_kind, condition_text, dynamic_expression, evidence, confidence, module, rel_path, line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (transition_id) DO UPDATE SET
                workflow_id = excluded.workflow_id,
                model_name = excluded.model_name,
                field_name = excluded.field_name,
                from_state = excluded.from_state,
                from_evidence = excluded.from_evidence,
                to_state = excluded.to_state,
                operation = excluded.operation,
                receiver = excluded.receiver,
                method_name = excluded.method_name,
                method_id = excluded.method_id,
                conditional = excluded.conditional,
                condition_kind = excluded.condition_kind,
                condition_text = excluded.condition_text,
                dynamic_expression = excluded.dynamic_expression,
                evidence = excluded.evidence,
                confidence = excluded.confidence,
                module = excluded.module,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_triggers(self, rows, run_id: str) -> int:
        payload = [(row.trigger_id, row.kind, row.name, row.module, row.target_model, row.target_method, row.guard_text, row.guard_states, row.guard_state_evidence, row.evidence, row.rel_path, row.line, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_trigger (
                trigger_id, kind, name, module, target_model, target_method, guard_text, guard_states, guard_state_evidence, evidence, rel_path, line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (trigger_id) DO UPDATE SET
                kind = excluded.kind,
                name = excluded.name,
                module = excluded.module,
                target_model = excluded.target_model,
                target_method = excluded.target_method,
                guard_text = excluded.guard_text,
                guard_states = excluded.guard_states,
                guard_state_evidence = excluded.guard_state_evidence,
                evidence = excluded.evidence,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_scheduled_actions(self, rows, run_id: str) -> int:
        payload = [(row.cron_id, row.xmlid, row.name, row.module, row.model_ref, row.target_model, row.method_name, row.interval, row.active, row.code_text, row.code_shape, row.evidence, row.rel_path, row.line, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_scheduled_action (
                cron_id, xmlid, name, module, model_ref, target_model, method_name, interval, active, code_text, code_shape, evidence, rel_path, line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (cron_id) DO UPDATE SET
                xmlid = excluded.xmlid,
                name = excluded.name,
                module = excluded.module,
                model_ref = excluded.model_ref,
                target_model = excluded.target_model,
                method_name = excluded.method_name,
                interval = excluded.interval,
                active = excluded.active,
                code_text = excluded.code_text,
                code_shape = excluded.code_shape,
                evidence = excluded.evidence,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)


    # -- schema v7 writes ------------------------------------------------

    def upsert_documents(self, rows, run_id: str) -> int:
        payload = [(row.doc_id, row.module, row.rel_path, row.kind, row.title, row.section_count, row.line_count, row.char_count, row.injection_flags, row.evidence, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_document (
                doc_id, module, rel_path, kind, title, section_count, line_count, char_count, injection_flags, evidence, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (doc_id) DO UPDATE SET
                module = excluded.module,
                rel_path = excluded.rel_path,
                kind = excluded.kind,
                title = excluded.title,
                section_count = excluded.section_count,
                line_count = excluded.line_count,
                char_count = excluded.char_count,
                injection_flags = excluded.injection_flags,
                evidence = excluded.evidence,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_doc_sections(self, rows, run_id: str) -> int:
        payload = [(row.section_id, row.doc_id, row.module, row.rel_path, row.level, row.title, row.anchor, row.line_start, row.line_end, row.excerpt, int(row.excerpt_truncated), row.char_count, row.injection_flags, row.evidence, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_doc_section (
                section_id, doc_id, module, rel_path, level, title, anchor, line_start, line_end, excerpt, excerpt_truncated, char_count, injection_flags, evidence, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (section_id) DO UPDATE SET
                doc_id = excluded.doc_id,
                module = excluded.module,
                rel_path = excluded.rel_path,
                level = excluded.level,
                title = excluded.title,
                anchor = excluded.anchor,
                line_start = excluded.line_start,
                line_end = excluded.line_end,
                excerpt = excluded.excerpt,
                excerpt_truncated = excluded.excerpt_truncated,
                char_count = excluded.char_count,
                injection_flags = excluded.injection_flags,
                evidence = excluded.evidence,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_test_files(self, rows, run_id: str) -> int:
        payload = [(row.test_file_id, row.module, row.rel_path, row.language, row.framework, row.class_count, row.case_count, row.parse_state, row.evidence, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_test_file (
                test_file_id, module, rel_path, language, framework, class_count, case_count, parse_state, evidence, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (test_file_id) DO UPDATE SET
                module = excluded.module,
                rel_path = excluded.rel_path,
                language = excluded.language,
                framework = excluded.framework,
                class_count = excluded.class_count,
                case_count = excluded.case_count,
                parse_state = excluded.parse_state,
                evidence = excluded.evidence,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_test_classes(self, rows, run_id: str) -> int:
        payload = [(row.test_class_id, row.test_file_id, row.module, row.class_name, row.bases, row.tags, row.case_count, row.rel_path, row.line, row.evidence, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_test_class (
                test_class_id, test_file_id, module, class_name, bases, tags, case_count, rel_path, line, evidence, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (test_class_id) DO UPDATE SET
                test_file_id = excluded.test_file_id,
                module = excluded.module,
                class_name = excluded.class_name,
                bases = excluded.bases,
                tags = excluded.tags,
                case_count = excluded.case_count,
                rel_path = excluded.rel_path,
                line = excluded.line,
                evidence = excluded.evidence,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_test_cases(self, rows, run_id: str) -> int:
        payload = [(row.test_case_id, row.test_class_id, row.module, row.name, row.rel_path, row.line, row.evidence, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_test_case (
                test_case_id, test_class_id, module, name, rel_path, line, evidence, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (test_case_id) DO UPDATE SET
                test_class_id = excluded.test_class_id,
                module = excluded.module,
                name = excluded.name,
                rel_path = excluded.rel_path,
                line = excluded.line,
                evidence = excluded.evidence,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_migrations(self, rows, run_id: str) -> int:
        payload = [(row.migration_id, row.module, row.version, row.phase, row.script, row.rel_path, row.evidence, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_migration (
                migration_id, module, version, phase, script, rel_path, evidence, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (migration_id) DO UPDATE SET
                module = excluded.module,
                version = excluded.version,
                phase = excluded.phase,
                script = excluded.script,
                rel_path = excluded.rel_path,
                evidence = excluded.evidence,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_business_processes(self, rows, run_id: str) -> int:
        payload = [(row.process_id, row.name, row.module, row.doc_id, row.section_id, row.status, row.step_count, row.matched_steps, row.workflow_id, row.conflict_count, row.evidence, row.rel_path, row.line, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_business_process (
                process_id, name, module, doc_id, section_id, status, step_count, matched_steps, workflow_id, conflict_count, evidence, rel_path, line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (process_id) DO UPDATE SET
                name = excluded.name,
                module = excluded.module,
                doc_id = excluded.doc_id,
                section_id = excluded.section_id,
                status = excluded.status,
                step_count = excluded.step_count,
                matched_steps = excluded.matched_steps,
                workflow_id = excluded.workflow_id,
                conflict_count = excluded.conflict_count,
                evidence = excluded.evidence,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_process_steps(self, rows, run_id: str) -> int:
        payload = [(row.step_id, row.process_id, row.position, row.label, row.normalised, row.state_id, row.state_value, row.match_kind, row.evidence, row.rel_path, row.line, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_process_step (
                step_id, process_id, position, label, normalised, state_id, state_value, match_kind, evidence, rel_path, line, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (step_id) DO UPDATE SET
                process_id = excluded.process_id,
                position = excluded.position,
                label = excluded.label,
                normalised = excluded.normalised,
                state_id = excluded.state_id,
                state_value = excluded.state_value,
                match_kind = excluded.match_kind,
                evidence = excluded.evidence,
                rel_path = excluded.rel_path,
                line = excluded.line,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def upsert_conventions(self, rows, run_id: str) -> int:
        payload = [(row.convention_id, row.key, row.category, row.statement, row.status, row.module_count, row.total_modules, row.share, row.source_rel_path, row.source_line, row.evidence, run_id) for row in rows]
        self.connection.executemany(
            """
            INSERT INTO node_convention (
                convention_id, key, category, statement, status, module_count, total_modules, share, source_rel_path, source_line, evidence, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (convention_id) DO UPDATE SET
                key = excluded.key,
                category = excluded.category,
                statement = excluded.statement,
                status = excluded.status,
                module_count = excluded.module_count,
                total_modules = excluded.total_modules,
                share = excluded.share,
                source_rel_path = excluded.source_rel_path,
                source_line = excluded.source_line,
                evidence = excluded.evidence,
                last_run_id = excluded.last_run_id
            """,
            payload,
        )
        return len(payload)

    def prune_v7(self, run_id: str) -> dict[str, int]:
        """Drop v7 rows no longer produced by the current run."""
        removed: dict[str, int] = {}
        for table in V7_EDGE_TABLE_NAMES + V7_NODE_TABLES:
            cursor = self.connection.execute(
                f"DELETE FROM {table} WHERE last_run_id <> ?", (run_id,)
            )
            removed[table] = cursor.rowcount if cursor.rowcount > 0 else 0
        return removed

    def prune_workflow(self, run_id: str) -> dict[str, int]:
        """Drop v6 rows no longer produced by the current run."""
        removed: dict[str, int] = {}
        for table in WORKFLOW_EDGE_TABLE_NAMES + V6_NODE_TABLES:
            cursor = self.connection.execute(
                f"DELETE FROM {table} WHERE last_run_id <> ?", (run_id,)
            )
            removed[table] = cursor.rowcount if cursor.rowcount > 0 else 0
        return removed

    def prune_frontend(self, run_id: str) -> dict[str, int]:
        """Prune the schema-v5 frontend layer, separately from the others."""
        removed: dict[str, int] = {}
        for table in FRONTEND_EDGE_TABLE_NAMES + V5_NODE_TABLES:
            cursor = self.connection.execute(
                f"DELETE FROM {table} WHERE last_run_id != ?", (run_id,)
            )
            removed[table] = cursor.rowcount
        return removed

    def prune_security(self, run_id: str) -> dict[str, int]:
        """Prune the schema-v4 security layer, separately from the others."""
        removed: dict[str, int] = {}
        for table in SECURITY_EDGE_TABLE_NAMES + V4_NODE_TABLES:
            cursor = self.connection.execute(
                f"DELETE FROM {table} WHERE last_run_id != ?", (run_id,)
            )
            removed[table] = cursor.rowcount
        return removed

    def prune_xml(self, run_id: str) -> dict[str, int]:
        """Prune the schema-v3 XML layer, separately from the other domains."""
        removed: dict[str, int] = {}
        for table in XML_EDGE_TABLE_NAMES + V3_NODE_TABLES:
            cursor = self.connection.execute(
                f"DELETE FROM {table} WHERE last_run_id != ?", (run_id,)
            )
            removed[table] = cursor.rowcount
        return removed

    def upsert_edges(
        self, edges: Sequence[GraphEdge], project_id: str, run_id: str
    ) -> dict[str, int]:
        """Write edges into their own tables; report how many landed in each."""
        written: dict[str, int] = {}
        by_table: dict[str, list[GraphEdge]] = {}
        for edge in edges:
            # Revalidate at the sink. GraphEdge already refuses an unknown table
            # at construction and is frozen, but this is the line that puts an
            # identifier into SQL, and the check that matters is the one next to
            # the risk -- not one several call frames away that a future
            # refactor could route around.
            if edge.table not in _ALLOWED_EDGE_TABLES:
                raise ValueError(
                    f"refusing to write an edge into unknown table {edge.table!r}; "
                    f"allowed: {sorted(_ALLOWED_EDGE_TABLES)}"
                )
            by_table.setdefault(edge.table, []).append(edge)
        for table, group in by_table.items():
            payload = [
                (
                    edge.edge_id(project_id), edge.src_kind, edge.src_id,
                    edge.dst_kind, edge.dst_id, edge.dst_unresolved, edge.binding,
                    edge.confidence, edge.provenance, edge.src_file, edge.src_line,
                    run_id,
                )
                for edge in group
            ]
            self.connection.executemany(
                f"""
                INSERT INTO edge_{table} (
                    edge_id, src_kind, src_id, dst_kind, dst_id, dst_unresolved,
                    binding, confidence, provenance, src_file, src_line,
                    last_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (edge_id) DO UPDATE SET
                    src_kind = excluded.src_kind,
                    src_id = excluded.src_id,
                    dst_kind = excluded.dst_kind,
                    dst_id = excluded.dst_id,
                    dst_unresolved = excluded.dst_unresolved,
                    binding = excluded.binding,
                    confidence = excluded.confidence,
                    provenance = excluded.provenance,
                    src_file = excluded.src_file,
                    src_line = excluded.src_line,
                    last_run_id = excluded.last_run_id
                """,
                payload,
            )
            written[f"edge_{table}"] = len(payload)
        return written

    def add_node_diagnostics(
        self, run_id: str, entries: Iterable[NodeDiagnostic]
    ) -> int:
        payload = [
            (
                entry.link_id(run_id), run_id, entry.severity, entry.category,
                entry.message, entry.node_kind, entry.node_id, entry.rel_path,
                entry.line,
            )
            for entry in entries
        ]
        self.connection.executemany(
            """
            INSERT INTO diagnostic_node (
                link_id, run_id, severity, category, message, node_kind, node_id,
                rel_path, line
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (link_id) DO NOTHING
            """,
            payload,
        )
        return len(payload)

    def record_digests(self, run_id: str, digests: dict[str, str]) -> None:
        self.connection.executemany(
            "INSERT INTO run_digest (run_id, domain, digest) VALUES (?, ?, ?) "
            "ON CONFLICT (run_id, domain) DO UPDATE SET digest = excluded.digest",
            [(run_id, domain, digest) for domain, digest in sorted(digests.items())],
        )

    def add_diagnostics(self, run_id: str, entries: Iterable[tuple]) -> None:
        self.connection.executemany(
            "INSERT INTO diagnostic (run_id, severity, category, message, path) "
            "VALUES (?, ?, ?, ?, ?)",
            [(run_id, *entry) for entry in entries],
        )

    def prune(self, run_id: str) -> dict[str, int]:
        """Delete rows not written by ``run_id`` and report how many went."""
        removed = {}
        for table in ("node_file", "node_module", "edge_depends_on"):
            cursor = self.connection.execute(
                f"DELETE FROM {table} WHERE last_run_id != ?", (run_id,)
            )
            removed[table] = cursor.rowcount
        return removed

    def prune_python(self, run_id: str) -> dict[str, int]:
        """Prune the schema-v2 Python layer.

        Deliberately separate from :meth:`prune` so the M1 return value keeps
        exactly the three keys it always had; callers that assert on it are
        asserting about the module layer and must not start seeing new tables.
        """
        removed: dict[str, int] = {}
        for table in PYTHON_EDGE_TABLE_NAMES + V2_NODE_TABLES:
            cursor = self.connection.execute(
                f"DELETE FROM {table} WHERE last_run_id != ?", (run_id,)
            )
            removed[table] = cursor.rowcount
        return removed

    def finish_run(self, run_id: str, values: dict) -> None:
        self.connection.execute(
            """
            UPDATE index_run SET
                finished_at = :finished_at,
                base_commit = :base_commit,
                dirty = :dirty,
                files_seen = :files_seen,
                files_excluded = :files_excluded,
                modules = :modules,
                depends_edges = :depends_edges,
                graph_digest = :graph_digest,
                status = :status
            WHERE run_id = :run_id
            """,
            {**values, "run_id": run_id},
        )
        # Keep a short run history; it is a log, not part of the graph.
        self.connection.execute(
            "DELETE FROM index_run WHERE run_id NOT IN "
            "(SELECT run_id FROM index_run ORDER BY started_at DESC LIMIT 20)"
        )
        # run_digest hangs off that history. Trimming index_run without
        # trimming this would leave a row per run forever, which is a slow leak
        # rather than a visible failure -- the kind that is only ever noticed
        # long after it started.
        self.connection.execute(
            "DELETE FROM run_digest WHERE run_id NOT IN (SELECT run_id FROM index_run)"
        )
        self.connection.commit()

    def commit(self) -> None:
        self.connection.commit()


def _opt_bool(value: bool | None) -> int | None:
    return None if value is None else int(value)
