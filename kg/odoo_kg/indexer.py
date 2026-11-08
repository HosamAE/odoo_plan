"""Indexing pipeline for M1.

Order of operations, and why:

1. Guard the platform and the write target, before anything is created.
2. Collect the git baseline, so every node can be stamped with the commit it
   describes and with how far the working tree has drifted from it.
3. Scan the include roots with exclusions applied during traversal.
4. Parse manifests, producing Module nodes.
5. Resolve dependency targets against the modules found in *this* project.
   An unresolved target keeps its original name and does not create a node --
   depending on ``sale`` must not invent a ``sale`` module in a project graph
   that does not contain one.
6. Write, prune anything the run did not touch, and record the digest.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import MILESTONE, SCHEMA_VERSION, __version__
from . import generations
from .config import Config
from .gitbaseline import GitBaseline, collect as collect_git
from .manifest import ParsedManifest, parse_manifest_bytes
from .pyast import parse_python_source
from .pyresolve import SourceFile, build_python_layer
from .safety import assert_write_target_allowed, ensure_data_dir, open_source_readonly
from .reader import GraphReader
from .scanner import MANIFEST_FILENAME, ScanResult, ScannedFile, read_manifest_bytes, scan
from .verify import run_checks
from .docindex import DocSource, GraphFacts, build_doc_layer
from .flowindex import (
    FlowSource, MethodIndex, RouteIndex, build_workflow_layer,
)
from .frontindex import ScannedSource, build_frontend_layer
from .secindex import build_security_layer
from .store import (
    DOCUMENTATION_EDGE_TABLE_NAMES,
    FRONTEND_EDGE_TABLE_NAMES,
    PROCESS_EDGE_TABLE_NAMES,
    TEST_EDGE_TABLE_NAMES,
    V2_EDGE_TABLES,
    WORKFLOW_EDGE_TABLE_NAMES,
    PYTHON_EDGE_TABLE_NAMES,
    SECURITY_EDGE_TABLE_NAMES,
    XML_EDGE_TABLE_NAMES,
    DependsEdge,
    FileRow,
    GraphStore,
    ModuleRow,
)
from .xmlindex import DeclaredFile, build_xml_layer, collect_declared_files
from .winpath import is_within, normalize

INSTALLED_STATE_UNKNOWN = "unknown"


@dataclass
class IndexSummary:
    """Everything a caller needs to report on a run without re-querying."""

    run_id: str
    project_id: str
    db_path: str
    #: The generation this run wrote, and whether it became the published one.
    #: A build that fails its acceptance checks is kept on disk and named here
    #: so it can be inspected, but the pointer is not moved to it.
    generation: str
    published: bool
    publish_error: str | None
    acceptance_failures: list[str]
    branch: str | None
    base_commit: str | None
    dirty: bool | None
    git_available: bool
    files_indexed: int
    files_excluded: int
    dirs_excluded: int
    modules: int
    modules_series_ok: int
    depends_edges: int
    depends_resolved: int
    depends_unresolved: int
    pruned: dict[str, int]
    graph_digest: str
    duration_seconds: float
    diagnostics: list[tuple] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # -- schema v2, the Python layer ------------------------------------
    python_files_parsed: int = 0
    python_files_failed: int = 0
    python_counts: dict[str, int] = field(default_factory=dict)
    python_edges: dict[str, int] = field(default_factory=dict)
    python_diagnostics: int = 0
    pruned_python: dict[str, int] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)
    # -- schema v3, the XML layer ---------------------------------------
    xml_counts: dict[str, int] = field(default_factory=dict)
    xml_edges: dict[str, int] = field(default_factory=dict)
    xml_diagnostics: int = 0
    pruned_xml: dict[str, int] = field(default_factory=dict)
    # -- schema v4, the security layer ----------------------------------
    security_counts: dict[str, int] = field(default_factory=dict)
    security_edges: dict[str, int] = field(default_factory=dict)
    security_diagnostics: int = 0
    pruned_security: dict[str, int] = field(default_factory=dict)
    # -- schema v5, the frontend layer ----------------------------------
    frontend_counts: dict[str, int] = field(default_factory=dict)
    frontend_edges: dict[str, int] = field(default_factory=dict)
    frontend_diagnostics: int = 0
    pruned_frontend: dict[str, int] = field(default_factory=dict)
    # -- schema v6, the workflow layer ----------------------------------
    workflow_counts: dict[str, int] = field(default_factory=dict)
    workflow_edges: dict[str, int] = field(default_factory=dict)
    workflow_diagnostics: int = 0
    pruned_workflow: dict[str, int] = field(default_factory=dict)
    # -- schema v7: documentation, tests, guidance ----------------------
    doc_counts: dict[str, int] = field(default_factory=dict)
    documentation_edges: dict[str, int] = field(default_factory=dict)
    test_edges: dict[str, int] = field(default_factory=dict)
    process_edges: dict[str, int] = field(default_factory=dict)
    doc_diagnostics: int = 0
    pruned_v7: dict[str, int] = field(default_factory=dict)
    #: How many edge rows each table *holds* after the run. Distinct rows, read
    #: back from the database, so this agrees with `stats` and `verify`.
    edge_rows: dict[str, int] = field(default_factory=dict)
    #: How many write operations were issued at each table. Always >= the row
    #: count: two derivations of one edge are two writes and one row. Kept
    #: because a growing gap is a real signal about the extractors, but never
    #: presented as a number of edges.
    edge_write_operations: dict[str, int] = field(default_factory=dict)


def run_index(config: Config, *, rebuild: bool = False,
              publish: bool = True) -> IndexSummary:
    """Build a new generation and, unless told not to, publish it.

    The published graph is never the file being written. This run creates
    ``generations/gen-<run_id>/graph.db``, fills it, checks it, and only then
    moves the pointer. A reader holding the previous generation is unaffected
    at every step, because that file is not opened here at all.

    ``publish=False`` builds and checks without moving the pointer. It exists
    for tests that need a finished generation to inspect, and for a caller that
    wants to see whether a build would pass its checks.

    ``rebuild`` is kept for compatibility and no longer means "destroy the
    existing database": every run writes a new file, so every run is a rebuild.
    """
    config.validate_platform()
    started = datetime.now(timezone.utc)
    run_id = uuid.uuid4().hex

    ensure_data_dir(config)
    with generations.BuildLock(config):
        return _build_generation(config, run_id, started, publish=publish)


def _build_generation(config: Config, run_id: str, started, *,
                      publish: bool) -> IndexSummary:
    generation = generations.generation_name(run_id)
    target_dir = assert_write_target_allowed(
        normalize(generations.generation_dir(config, generation)), config
    )
    os.makedirs(target_dir.display, exist_ok=True)
    db_path = assert_write_target_allowed(
        normalize(generations.generation_db(config, generation)), config
    )
    rebuild = True

    git = collect_git(config.repo_root.display, config.repo_root.key)
    scan_result = scan(config)

    diagnostics: list[tuple] = list(scan_result.diagnostics)
    file_rows, state_by_key = _build_file_rows(scan_result, git)
    module_rows, edges, module_diagnostics = _build_modules(
        config, scan_result, state_by_key
    )
    diagnostics.extend(module_diagnostics)

    known = {row.technical_name for row in module_rows}
    resolved = 0
    for edge in edges:
        if edge.dst_unresolved in known:
            # dst_unresolved deliberately keeps the original target name even
            # once dst_module is set, so provenance survives resolution.
            edge.dst_module = edge.dst_unresolved
            resolved += 1

    # Both derived layers are built entirely in memory before the store is
    # opened, so a parse failure cannot leave a half-written graph behind.
    sources, parse_failures = _parse_python_sources(config, scan_result, module_rows)
    python = build_python_layer(config.project_id, sources)

    # The XML layer needs the Python layer's models: a reference to Odoo's
    # generated `model_<name>` XML ID can only be resolved by knowing which
    # module declares that model's _name.
    #
    # A set per model, not one module: a model may be declared in several
    # modules, and keeping only the last would make one module's own references
    # to its own model look broken.
    known_models: dict[str, set[str]] = {}
    for row in python.defs:
        if row.model_name and row.declared_name == row.model_name and row.module:
            known_models.setdefault(row.model_name, set()).add(row.module)
    xml, declared_files = _build_xml_layer(
        config, scan_result, known_models, diagnostics
    )
    security = build_security_layer(
        config.project_id,
        declared_files,
        _read_source_bytes,
        known_models,
        {row.technical_name for row in module_rows},
    )
    frontend = _build_frontend_layer(
        config, scan_result, module_rows, set(known_models)
    )
    workflow = _build_workflow_layer(
        config, scan_result, module_rows, set(known_models), python, frontend
    )
    docs = _build_doc_layer(
        config, scan_result, module_rows, set(known_models), python, xml,
        frontend, workflow,
    )

    with GraphStore(db_path.display, rebuild=rebuild,
                    expect_project_id=config.project_id) as store:
        store.start_run(run_id, config.project_id, started.isoformat())
        store.upsert_project(
            {
                "project_id": config.project_id,
                "repo_root": config.repo_root.display,
                "core_root": config.core_root.display if config.core_root else None,
                "branch": git.branch,
                "base_commit": git.base_commit,
                "dirty": None if git.dirty is None else int(git.dirty),
                "git_available": int(git.available),
                "git_error": git.error,
                "indexed_at": started.isoformat(),
                "schema_version": SCHEMA_VERSION,
                "indexer_version": __version__,
                "milestone": MILESTONE,
                "run_id": run_id,
            }
        )
        store.upsert_files(file_rows, run_id)
        store.upsert_modules(module_rows, run_id)
        store.upsert_depends(edges, config.project_id, run_id)
        store.upsert_models(python.models, run_id)
        store.upsert_model_defs(python.defs, run_id)
        store.upsert_fields(python.fields, run_id)
        store.upsert_methods(python.methods, run_id)
        store.upsert_data_files(xml.files, run_id)
        store.upsert_xml_records(xml.records, run_id)
        store.upsert_xmlids(xml.xmlids, run_id)
        store.upsert_asset_bundles(frontend.bundles, run_id)
        store.upsert_asset_entries(frontend.entries, run_id)
        store.upsert_js_files(frontend.js_files, run_id)
        store.upsert_owl_components(frontend.components, run_id)
        store.upsert_owl_templates(frontend.templates, run_id)
        store.upsert_routes(frontend.routes, run_id)
        store.upsert_documents(docs.documents, run_id)
        store.upsert_doc_sections(docs.sections, run_id)
        store.upsert_test_files(docs.test_files, run_id)
        store.upsert_test_classes(docs.test_classes, run_id)
        store.upsert_test_cases(docs.test_cases, run_id)
        store.upsert_migrations(docs.migrations, run_id)
        store.upsert_business_processes(docs.processes, run_id)
        store.upsert_process_steps(docs.steps, run_id)
        store.upsert_conventions(docs.conventions, run_id)
        store.upsert_workflows(workflow.workflows, run_id)
        store.upsert_workflow_states(workflow.states, run_id)
        store.upsert_transitions(workflow.transitions, run_id)
        store.upsert_triggers(workflow.triggers, run_id)
        store.upsert_scheduled_actions(workflow.crons, run_id)
        store.upsert_privileges(security.privileges, run_id)
        store.upsert_groups(security.groups, run_id)
        store.upsert_acls(security.acls, run_id)
        store.upsert_rules(security.rules, run_id)
        write_operations = store.upsert_edges(
            list(python.edges) + list(xml.edges) + list(security.edges)
            + list(frontend.edges) + list(workflow.edges) + list(docs.edges),
            config.project_id, run_id,
        )
        pruned = store.prune(run_id)
        pruned_python = store.prune_python(run_id)
        pruned_xml = store.prune_xml(run_id)
        pruned_security = store.prune_security(run_id)
        pruned_frontend = store.prune_frontend(run_id)
        pruned_workflow = store.prune_workflow(run_id)
        pruned_v7 = store.prune_v7(run_id)

        # Counted from the tables, not from what was offered to them.
        #
        # `upsert_edges` reports how many edge objects it wrote, and an edge
        # whose id already exists is an ON CONFLICT update rather than a new
        # row. Two call sites that derive the same edge -- the same method
        # touching the same model twice, say -- are two write operations and
        # one row. Reporting the write count made the CLI say 79 where the
        # table held 74, and made it disagree with `stats` and `verify`, which
        # both read the tables. Reading the tables here is what makes all three
        # report the same number.
        edge_rows = {name: store.count(name) for name in V2_EDGE_TABLES}
        depends_rows = store.count("edge_depends_on")
        python_edges = {
            name: edge_rows[name] for name in PYTHON_EDGE_TABLE_NAMES
        }
        xml_edges = {name: edge_rows[name] for name in XML_EDGE_TABLE_NAMES}
        security_edges = {
            name: edge_rows[name] for name in SECURITY_EDGE_TABLE_NAMES
        }
        frontend_edges = {
            name: edge_rows[name] for name in FRONTEND_EDGE_TABLE_NAMES
        }
        workflow_edges = {
            name: edge_rows[name] for name in WORKFLOW_EDGE_TABLE_NAMES
        }
        documentation_edges = {
            name: edge_rows[name] for name in DOCUMENTATION_EDGE_TABLE_NAMES
        }
        test_edges = {name: edge_rows[name] for name in TEST_EDGE_TABLE_NAMES}
        process_edges = {
            name: edge_rows[name] for name in PROCESS_EDGE_TABLE_NAMES
        }
        store.add_diagnostics(run_id, diagnostics)
        store.add_node_diagnostics(run_id, python.diagnostics)
        store.add_node_diagnostics(run_id, xml.diagnostics)
        store.add_node_diagnostics(run_id, security.diagnostics)
        store.add_node_diagnostics(run_id, frontend.diagnostics)
        store.add_node_diagnostics(run_id, workflow.diagnostics)
        store.add_node_diagnostics(run_id, docs.diagnostics)
        digests = store.domain_digests()
        store.record_digests(run_id, digests)
        # index_run.graph_digest keeps its M1 meaning -- the module layer -- so
        # an M2 database and an M3 database of the same commit agree there.
        digest = digests["modules"]
        finished = datetime.now(timezone.utc)
        store.finish_run(
            run_id,
            {
                "finished_at": finished.isoformat(),
                "base_commit": git.base_commit,
                "dirty": None if git.dirty is None else int(git.dirty),
                "files_seen": len(file_rows),
                "files_excluded": scan_result.excluded_count,
                "modules": len(module_rows),
                "depends_edges": len(edges),
                "graph_digest": digest,
                "status": "ok",
            },
        )

    # -- pre-publication gate ------------------------------------------
    #
    # Everything below runs against the finished generation, read-only, before
    # the pointer moves. A build that fails any of it never becomes current and
    # the previously published graph is left exactly as it was.
    acceptance: list[str] = []
    with GraphReader(db_path.display, expect_schema=SCHEMA_VERSION,
                     expect_project_id=config.project_id) as reader:
        for check in run_checks(config, reader):
            if not check.passed:
                acceptance.append(f"{check.name}: {check.detail}")

    published = None
    publish_error = None
    if acceptance:
        publish_error = (
            f"{len(acceptance)} acceptance check(s) failed, so "
            f"{generation} was built but not published: "
            + "; ".join(acceptance[:5])
            + ("; ..." if len(acceptance) > 5 else "")
        )
    elif publish:
        published = generations.publish(
            config, generation=generation, run_id=run_id, git=git,
            started_at=started.isoformat(),
        )

    return IndexSummary(
        run_id=run_id,
        project_id=config.project_id,
        db_path=db_path.display,
        generation=generation,
        published=published is not None,
        publish_error=publish_error,
        acceptance_failures=acceptance,
        branch=git.branch,
        base_commit=git.base_commit,
        dirty=git.dirty,
        git_available=git.available,
        files_indexed=len(file_rows),
        files_excluded=scan_result.excluded_count,
        dirs_excluded=len(scan_result.excluded_dirs),
        modules=len(module_rows),
        modules_series_ok=sum(1 for row in module_rows if row.version_series_ok),
        depends_edges=depends_rows,
        depends_resolved=resolved,
        depends_unresolved=len(edges) - resolved,
        pruned=pruned,
        graph_digest=digest,
        duration_seconds=(finished - started).total_seconds(),
        diagnostics=diagnostics,
        warnings=list(config.warnings),
        python_files_parsed=len(sources),
        python_files_failed=parse_failures,
        python_counts=dict(python.counts),
        python_edges=python_edges,
        python_diagnostics=len(python.diagnostics),
        pruned_python=pruned_python,
        digests=digests,
        xml_counts=dict(xml.counts),
        xml_edges=xml_edges,
        xml_diagnostics=len(xml.diagnostics),
        pruned_xml=pruned_xml,
        security_counts=dict(security.counts),
        security_edges=security_edges,
        security_diagnostics=len(security.diagnostics),
        pruned_security=pruned_security,
        frontend_counts=dict(frontend.counts),
        frontend_edges=frontend_edges,
        frontend_diagnostics=len(frontend.diagnostics),
        pruned_frontend=pruned_frontend,
        workflow_counts=dict(workflow.counts),
        workflow_edges=workflow_edges,
        workflow_diagnostics=len(workflow.diagnostics),
        pruned_workflow=pruned_workflow,
        doc_counts=dict(docs.counts),
        documentation_edges=documentation_edges,
        test_edges=test_edges,
        process_edges=process_edges,
        doc_diagnostics=len(docs.diagnostics),
        pruned_v7=pruned_v7,
        edge_rows=edge_rows,
        edge_write_operations=write_operations,
    )


def _build_doc_layer(config, scan_result, module_rows, known_models, python,
                     xml, frontend, workflow):
    """Read documents, tests and migrations, and derive the guidance layer.

    Handed what every earlier layer already holds, so a document that names a
    model binds to *that* model node rather than to a string that looks like
    one, and a documented process binds to the workflow M4.2 actually found.
    """
    module_names = {row.technical_name for row in module_rows}
    facts = GraphFacts(
        models=set(known_models),
        routes={row.path: row.route_id for row in frontend.routes if row.path},
        xmlids={row.xmlid for row in xml.xmlids},
        modules=module_names,
        workflows={
            (row.model_name, row.field_name): row.workflow_id
            for row in workflow.workflows
        },
        workflow_module={
            row.workflow_id: row.module for row in workflow.workflows
        },
    )
    for row in python.methods:
        facts.methods.setdefault(row.model_name, {}).setdefault(
            row.method_name, row.method_id
        )

    # A state is findable by its value or by its Selection label, both reduced
    # to the same normal form a documented step is reduced to.
    from .docread import normalise_label

    for state in workflow.states:
        bucket = facts.workflow_states.setdefault(state.workflow_id, {})
        bucket.setdefault(
            normalise_label(state.value), (state.state_id, state.value, "value")
        )
        if state.label:
            bucket.setdefault(
                normalise_label(state.label),
                (state.state_id, state.value, "label"),
            )

    module_by_prefix = _module_lookup(module_rows)
    sources = []
    for scanned in scan_result.files:
        module = _module_for(scanned.path.key, module_by_prefix)
        sources.append(DocSource(
            module=module,
            rel_path=scanned.rel_display,
            file_key=scanned.path.key,
            disk_path=scanned.path.display,
        ))

    return build_doc_layer(
        config.project_id, sources, _read_source_bytes, facts,
        sorted(module_names),
    )


def _build_workflow_layer(config, scan_result, module_rows, known_models,
                          python, frontend):
    """Read method bodies, XML buttons and ir.cron records for the workflows.

    Handed the Python layer's methods and the frontend layer's routes rather
    than re-deriving them: a transition must bind to the same method node the
    rest of the graph already holds, or the two layers would describe different
    code with the same names.
    """
    method_index = MethodIndex(
        (row.model_name, row.method_name, row.method_id) for row in python.methods
    )
    route_index = RouteIndex(
        (row.rel_path, row.controller, row.method, row.route_id)
        for row in frontend.routes
    )

    module_by_prefix = _module_lookup(module_rows)
    sources = []
    for scanned in scan_result.files:
        module = _module_for(scanned.path.key, module_by_prefix)
        if module is None:
            continue
        sources.append(FlowSource(
            module=module,
            rel_path=scanned.rel_display,
            file_key=scanned.path.key,
            disk_path=scanned.path.display,
        ))

    return build_workflow_layer(
        config.project_id, sources, _read_source_bytes, known_models,
        method_index, route_index,
    )


def _build_frontend_layer(config, scan_result, module_rows, known_models):
    """Read manifests for assets, and every scanned file for JS, OWL and routes.

    Files reach this layer only through the M1 scan result, so every exclusion
    rule applies for free: a file under `.git`, a `*.conf`, or anything the
    rules kept out cannot be seen here at all.
    """
    module_by_prefix = _module_lookup(module_rows)
    sources: list[ScannedSource] = []
    for scanned in scan_result.files:
        module = _module_for(scanned.path.key, module_by_prefix)
        if module is None:
            continue
        prefix = next(
            key for key, name in module_by_prefix if name == module
            and scanned.path.key.startswith(key + "/")
        )
        relative = scanned.path.key[len(prefix) + 1:]
        sources.append(
            ScannedSource(
                module=module,
                rel_path=scanned.rel_display,
                # How a manifest addresses a file: `<module>/<path inside it>`.
                # Folded, because the scanner's keys are and because Windows
                # does not distinguish case; the manifest's own spelling is
                # kept on the edge as provenance.
                addon_path=f"{module}/{relative}".lower(),
                file_key=scanned.path.key,
                disk_path=scanned.path.display,
            )
        )

    manifests = []
    for manifest_file in scan_result.manifests():
        module_dir = normalize(os.path.dirname(manifest_file.path.display))
        if not module_dir.name:
            continue
        try:
            raw = read_manifest_bytes(manifest_file)
        except OSError:
            continue
        parsed = parse_manifest_bytes(raw)
        manifests.append((
            module_dir.name, manifest_file.rel_display, parsed.data,
            parsed.dict_line,
        ))

    return build_frontend_layer(
        config.project_id, manifests, sources, _read_source_bytes, known_models
    )


def _build_xml_layer(
    config: Config,
    scan_result: ScanResult,
    known_models: dict[str, str],
    diagnostics: list[tuple],
):
    """Read every manifest's data sections and index the XML they declare.

    Scope follows the manifest, not the filesystem: Odoo loads what a module
    declares and ignores the rest, so indexing an undeclared file would put
    records in the graph that no database ever gets.
    """
    # Absolute path (normalised) -> the scanner's key, so the XML layer can
    # only reach files the scanner already accepted. A declared file that is
    # missing from this map was excluded, and is reported rather than read.
    file_keys = {
        os.path.normcase(scanned.path.display).replace("\\", "/"): scanned.path.key
        for scanned in scan_result.files
    }
    declared: list[DeclaredFile] = []
    undeclared: dict[str, list[str]] = {}

    for manifest_file in scan_result.manifests():
        module_dir = normalize(os.path.dirname(manifest_file.path.display))
        module = module_dir.name
        if not module:
            continue
        try:
            raw = read_manifest_bytes(manifest_file)
        except OSError:
            continue
        parsed = parse_manifest_bytes(raw)
        entries, problems = collect_declared_files(
            module, module_dir.display, parsed.data, file_keys
        )
        declared.extend(entries)
        for problem in problems:
            diagnostics.append(
                ("warning", "manifest_data", problem, manifest_file.path.display)
            )
        declared_paths = {entry.rel_path for entry in entries}
        prefix = module_dir.key.rstrip("/") + "/"
        for scanned in scan_result.files:
            if not scanned.path.key.startswith(prefix):
                continue
            if not scanned.rel_key.endswith(".xml"):
                continue
            relative = scanned.path.key[len(prefix):]
            if relative not in declared_paths:
                undeclared.setdefault(module, []).append(relative)

    declared.sort(key=lambda entry: (entry.module, entry.load_order))
    layer = build_xml_layer(
        config.project_id, declared, _read_source_bytes, known_models, undeclared
    )
    # The declared list travels back out so the security layer sees exactly the
    # files Odoo would load, in the same order, without re-reading manifests.
    return layer, declared


def _read_source_bytes(path: str) -> bytes:
    with open_source_readonly(path) as handle:
        return handle.read()


def _module_lookup(module_rows: list[ModuleRow]) -> list[tuple[str, str]]:
    """(module directory key, technical name), longest path first.

    Longest first so a module nested inside another is matched before its
    parent. Nothing in testing19 nests today, but a wrong answer here would
    silently attribute a model to the wrong module.
    """
    pairs = [
        (normalize(row.module_dir).key.rstrip("/"), row.technical_name)
        for row in module_rows
    ]
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return pairs


def _parse_python_sources(
    config: Config, scan_result: ScanResult, module_rows: list[ModuleRow]
) -> tuple[list[SourceFile], int]:
    """Parse every scanned Python file. Reads bytes; imports nothing.

    Only files the M1 scanner already accepted are considered, so the Python
    layer inherits every exclusion rule for free: a file under ``.git`` or a
    ``*.conf`` never reached the scan result and cannot reach the parser.

    Manifests are skipped -- they are data, already parsed by the manifest
    reader, and are not model definitions.
    """
    lookup = _module_lookup(module_rows)
    sources: list[SourceFile] = []
    failures = 0

    for scanned in scan_result.files:
        if not scanned.rel_key.endswith(".py"):
            continue
        if scanned.name.lower() == MANIFEST_FILENAME:
            continue
        try:
            with open_source_readonly(scanned.path.display) as handle:
                raw = handle.read()
        except OSError:
            # The scanner already recorded the unreadable file; the Python
            # layer simply has nothing to say about it.
            failures += 1
            continue
        parsed = parse_python_source(raw)
        if not parsed.parsed:
            failures += 1
        sources.append(
            SourceFile(
                file_key=scanned.path.key,
                rel_path=scanned.rel_display,
                module=_module_for(scanned.path.key, lookup),
                parsed=parsed,
            )
        )
    return sources, failures


def _module_for(file_key: str, lookup: list[tuple[str, str]]) -> str | None:
    for module_key, name in lookup:
        if file_key.startswith(module_key + "/"):
            return name
    return None


def _build_file_rows(
    scan_result: ScanResult, git: GitBaseline
) -> tuple[list[FileRow], dict[str, str]]:
    rows: list[FileRow] = []
    state_by_key: dict[str, str] = {}
    for scanned in scan_result.files:
        index_blob = git.blob_for(scanned.path.key)
        # git status is authoritative and is the ONLY source of content_state.
        #
        # It is tempting to cross-check by comparing our own blob hash against
        # the index sha, but that is wrong: under `core.autocrlf=true` (the
        # default on Windows, and what testing19 uses) the working tree holds
        # CRLF while the index holds LF, so the two shas differ for every text
        # file that is perfectly clean. Reproducing git's normalization would
        # mean reimplementing .gitattributes, autocrlf and text/binary
        # detection. Both hashes are stored for later use; they are simply not
        # comparable to each other.
        state = git.state_for(scanned.path.key)
        state_by_key[scanned.path.key] = state
        rows.append(
            FileRow(
                file_key=scanned.path.key,
                display_path=scanned.path.display,
                rel_path=scanned.rel_display,
                include_root=scanned.include_root.key,
                size_bytes=scanned.size_bytes,
                content_sha=scanned.content_sha,
                git_blob_sha=scanned.git_blob_sha,
                index_blob_sha=index_blob,
                content_state=state,
                hash_state=scanned.hash_state,
                base_commit=git.base_commit,
            )
        )
    return rows, state_by_key


def _build_modules(
    config: Config, scan_result: ScanResult, state_by_key: dict[str, str]
) -> tuple[list[ModuleRow], list[DependsEdge], list[tuple]]:
    diagnostics: list[tuple] = []
    rows: list[ModuleRow] = []
    edges: list[DependsEdge] = []
    claimed: dict[str, str] = {}

    for manifest_file in scan_result.manifests():
        module_dir = normalize(os.path.dirname(manifest_file.path.display))
        technical_name = module_dir.name
        if not technical_name:
            diagnostics.append(
                ("error", "module", "manifest has no parent directory", manifest_file.path.display)
            )
            continue
        if technical_name in claimed:
            diagnostics.append(
                (
                    "error",
                    "module",
                    f"duplicate module name {technical_name!r}; "
                    f"keeping {claimed[technical_name]}",
                    manifest_file.path.display,
                )
            )
            continue
        claimed[technical_name] = manifest_file.path.display

        parsed = _parse(manifest_file, diagnostics)
        origin, origin_rule = config.resolve_origin(technical_name)
        content_state = state_by_key.get(manifest_file.path.key, "unknown")
        modified = _module_is_modified(module_dir.key, state_by_key)

        version = parsed.get_str("version")
        installable, installable_declared = parsed.get_bool("installable", True)
        application, application_declared = parsed.get_bool("application", False)
        auto_install, auto_install_raw = _auto_install(parsed)

        rows.append(
            ModuleRow(
                technical_name=technical_name,
                manifest_name=parsed.get_str("name"),
                version=version,
                version_series_ok=bool(version and version.startswith(config.expected_series + ".")),
                author=_author(parsed),
                license=parsed.get_str("license"),
                origin=origin,
                origin_rule=origin_rule,
                present_in_source=True,
                installable=installable if parsed.ok else None,
                installable_declared=installable_declared,
                # No PostgreSQL connection exists in M1, so the installed state
                # is genuinely unknown rather than assumed from the manifest.
                installed_state=INSTALLED_STATE_UNKNOWN,
                application=application if parsed.ok else None,
                application_declared=application_declared,
                auto_install=auto_install,
                auto_install_raw=auto_install_raw,
                modified_worktree=modified,
                manifest_file=manifest_file.path.display,
                manifest_line=parsed.dict_line,
                module_dir=module_dir.display,
                content_state=content_state,
                manifest_ok=parsed.ok,
                manifest_error="; ".join(parsed.errors) or None,
            )
        )

        for dependency in parsed.dependencies:
            edges.append(
                DependsEdge(
                    src_module=technical_name,
                    dst_unresolved=dependency.name,
                    src_manifest_file=manifest_file.path.display,
                    src_manifest_line=dependency.line,
                )
            )

        for warning in parsed.warnings:
            diagnostics.append(("warning", "manifest", warning, manifest_file.path.display))

    rows.sort(key=lambda row: row.technical_name)
    edges.sort(key=lambda edge: (edge.src_module, edge.dst_unresolved))
    return rows, edges, diagnostics


def _parse(manifest_file: ScannedFile, diagnostics: list[tuple]) -> ParsedManifest:
    try:
        raw = read_manifest_bytes(manifest_file)
    except OSError as exc:
        diagnostics.append(("error", "manifest", f"unreadable: {exc}", manifest_file.path.display))
        return ParsedManifest(ok=False, errors=[f"unreadable: {exc}"])
    parsed = parse_manifest_bytes(raw)
    for error in parsed.errors:
        diagnostics.append(("error", "manifest", error, manifest_file.path.display))
    return parsed


def _author(parsed: ParsedManifest) -> str | None:
    """Odoo allows author to be a string or a list of strings."""
    value = parsed.data.get("author")
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item).strip() for item in value if str(item).strip()) or None
    return parsed.get_str("author")


def _auto_install(parsed: ParsedManifest) -> tuple[bool | None, str | None]:
    """Normalize ``auto_install``, which Odoo allows to be a bool or a list.

    A list means "auto-install once these specific dependencies are present",
    which is not a boolean. Rather than flatten it to True or False, the
    boolean column is left NULL and the declaration is kept verbatim.
    """
    if "auto_install" not in parsed.data:
        return None, None
    value = parsed.data["auto_install"]
    if isinstance(value, bool):
        return value, repr(value)
    if isinstance(value, (list, tuple)):
        return None, repr(list(value))
    return None, repr(value)


def _module_is_modified(module_dir_key: str, state_by_key: dict[str, str]) -> bool:
    prefix = module_dir_key.rstrip("/") + "/"
    for file_key, state in state_by_key.items():
        if state in ("modified", "untracked") and file_key.startswith(prefix):
            return True
    return False


def resolve_db_path(config: Config) -> str:
    """The database path, validated but without creating anything."""
    if not is_within(config.data_dir.key, config.db_path.key):
        raise ValueError("db_path escaped data_dir")
    return config.db_path.display
