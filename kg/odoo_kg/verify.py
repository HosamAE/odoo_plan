"""Acceptance checks, expressed as data rather than prose.

The same checks back both the ``verify`` CLI command and the acceptance test
suite, so "the tool says it passed" and "the tests say it passed" cannot drift
apart.

Every check takes a :class:`~odoo_kg.store.GraphReads`, which is the query
surface without any of the writing methods, and reaches the database only
through ``store.connection``, ``store.count`` and ``store.modules``. That is
what lets ``verify`` run against a strictly read-only connection: a checker
that can modify the thing it is checking is not a checker, and until this
milestone ``verify`` opened the writing store and rewrote pages of the graph it
was validating.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .docindex import SUBJECT_PROVENANCE
from .docread import MAX_EXCERPT_CHARS
from .exclusions import BUILTIN_DIR_NAMES
from .store import (
    DOCUMENTATION_EDGE_TABLES,
    EDGE_TABLES,
    FRONTEND_EDGE_TABLES,
    PROCESS_EDGE_TABLES,
    TEST_EDGE_TABLES,
    SECURITY_EDGE_TABLES,
    WORKFLOW_EDGE_TABLES,
    GraphReads,
)

#: (module, expected version) pairs the milestone calls out explicitly.
EXPECTED_MODULE_VERSIONS = {"website_helpdesk_support_ticket": "21.10.35"}

#: (module, expected origin) pairs the milestone calls out explicitly.
EXPECTED_MODULE_ORIGINS = {
    "website_helpdesk_support_ticket": "vendor",
    "membership": "core-vendored",
    "base_accounting_kit": "vendor",
    "base_account_budget": "vendor",
    "dynamic_accounts_report": "vendor",
}

#: A floor, not an equality. testing19 is a live checkout: people add modules
#: while this runs, and an exact count would turn somebody else's new module
#: into a failing acceptance check. The floor still catches a module vanishing.
MIN_MODULE_COUNT = 66

#: Modules known to sit off the expected series. Everything else must be on it,
#: which is stricter than the old fixed count: a new module that arrives on the
#: wrong series now fails instead of merely shifting a total.
KNOWN_OFF_SERIES = frozenset({"website_helpdesk_support_ticket"})

# Kept for callers that still import them.
EXPECTED_MODULE_COUNT = MIN_MODULE_COUNT
EXPECTED_SERIES_OK_COUNT = MIN_MODULE_COUNT - len(KNOWN_OFF_SERIES)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    @property
    def symbol(self) -> str:
        return "PASS" if self.passed else "FAIL"


def _floor(name: str, count: int, minimum: int, enforce: bool) -> Check:
    """A row-count floor. Outside the testing19 profile it is reported, not enforced:
    the floors were measured on that checkout and mean nothing for another one."""
    if enforce:
        return Check(name, count >= minimum, f"{count} rows (expected at least {minimum})")
    return Check(name, True, f"{count} rows (floor not enforced: generic profile)")


def run_checks(config: Config, store: GraphReads) -> list[Check]:
    """Evaluate every M1 acceptance criterion against a built graph."""
    checks: list[Check] = []
    modules = {row["technical_name"]: row for row in store.modules()}
    floors = getattr(config, "acceptance_profile", "testing19") == "testing19"

    if floors:
        checks.append(
            Check(
                "module_count",
                len(modules) >= MIN_MODULE_COUNT,
                f"{len(modules)} Module nodes (expected at least {MIN_MODULE_COUNT}"
                + (
                    f"; {len(modules) - MIN_MODULE_COUNT} added since the floor was set)"
                    if len(modules) > MIN_MODULE_COUNT else ")"
                ),
            )
        )
    else:
        # A graph with no module in it is a wrong repo_root, not a small project.
        checks.append(
            Check("module_count", len(modules) >= 1,
                  f"{len(modules)} Module nodes (expected at least 1)")
        )

    off_series = {
        name for name, row in modules.items() if not row["version_series_ok"]
    }
    if floors:
        unexpected = off_series - KNOWN_OFF_SERIES
        checks.append(
            Check(
                "version_series",
                not unexpected,
                f"{len(modules) - len(off_series)} modules on {config.expected_series}.*; "
                + (
                    f"off-series: {sorted(off_series)} (all known)"
                    if not unexpected
                    else f"unexpectedly off-series: {sorted(unexpected)}"
                ),
            )
        )
        for name, expected in EXPECTED_MODULE_VERSIONS.items():
            row = modules.get(name)
            actual = row["version"] if row else "<missing module>"
            checks.append(
                Check(f"version[{name}]", actual == expected, f"{actual!r} (expected {expected!r})")
            )
        for name, expected in EXPECTED_MODULE_ORIGINS.items():
            row = modules.get(name)
            actual = row["origin"] if row else "<missing module>"
            checks.append(
                Check(f"origin[{name}]", actual == expected, f"{actual!r} (expected {expected!r})")
            )
    else:
        # Reported, not enforced: vendor modules often carry their own numbering.
        checks.append(
            Check(
                "version_series",
                True,
                f"{len(modules) - len(off_series)} modules on {config.expected_series}.*"
                + (f"; off-series (reported only): {sorted(off_series)[:10]}" if off_series else ""),
            )
        )
        # Every module the config names explicitly must carry that origin.
        for name, expected in sorted(config.module_origins.items()):
            row = modules.get(name)
            if row is None:
                continue
            checks.append(
                Check(f"origin[{name}]", row["origin"] == expected,
                      f"{row['origin']!r} (expected {expected!r})")
            )

    bad_state = [
        name for name, row in modules.items() if row["installed_state"] != "unknown"
    ]
    checks.append(
        Check(
            "installed_state_unknown",
            not bad_state,
            "all modules unknown" if not bad_state else f"non-unknown: {bad_state}",
        )
    )

    checks.extend(_file_hygiene_checks(config, store))
    checks.extend(_python_layer_checks(store, floors))
    checks.extend(_xml_layer_checks(store, floors))
    checks.extend(_security_layer_checks(store, floors))
    checks.extend(_frontend_layer_checks(store, floors))
    checks.extend(_workflow_layer_checks(store, floors))
    checks.extend(_v7_layer_checks(store, floors))
    return checks


#: Lower bounds for v7. Measured at 1868561: 55 documents, 450 sections,
#: 150 test files, 173 classes, 1489 cases, 44 migrations, 17 conventions.
MIN_DOCUMENTS = 48
MIN_DOC_SECTIONS = 390
MIN_TEST_FILES = 130
MIN_TEST_CLASSES = 150
MIN_TEST_CASES = 1300
MIN_MIGRATIONS = 38
MIN_CONVENTIONS = 15


def _v7_layer_checks(store: GraphReads, enforce_floors: bool = True) -> list[Check]:
    """Schema-v7 criteria.

    These defend three sentences the layer must never stop being able to say.
    Document text is data, not instruction. Naming a subject in a test is not
    coverage of it. An observed pattern is not a rule. Everything below exists
    to make one of those hold.
    """
    checks: list[Check] = []

    for name, table, minimum in (
        ("v7_documents", "node_document", MIN_DOCUMENTS),
        ("v7_doc_sections", "node_doc_section", MIN_DOC_SECTIONS),
        ("v7_test_files", "node_test_file", MIN_TEST_FILES),
        ("v7_test_classes", "node_test_class", MIN_TEST_CLASSES),
        ("v7_test_cases", "node_test_case", MIN_TEST_CASES),
        ("v7_migrations", "node_migration", MIN_MIGRATIONS),
        ("v7_conventions", "node_convention", MIN_CONVENTIONS),
    ):
        count = store.count(table)
        checks.append(_floor(name, count, minimum, enforce_floors))

    # -- documents are data -------------------------------------------

    longest = store.connection.execute(
        "SELECT IFNULL(MAX(LENGTH(excerpt)), 0) FROM node_doc_section"
    ).fetchone()[0]
    # The ceiling is the ceiling. This allowed MAX_EXCERPT_CHARS + 8 while the
    # truncation appended its suffix after the cut, so it passed at 483 against
    # a documented maximum of 480 -- a check written around the defect rather
    # than at it. The suffix is budgeted for now, so the slack is gone.
    over = store.connection.execute(
        "SELECT COUNT(*) FROM node_doc_section WHERE LENGTH(excerpt) > ?",
        (MAX_EXCERPT_CHARS,),
    ).fetchone()[0]
    checks.append(
        Check(
            "doc_excerpts_are_bounded",
            longest <= MAX_EXCERPT_CHARS,
            f"longest excerpt {longest} chars (ceiling {MAX_EXCERPT_CHARS})"
            if not over
            else f"{over} section(s) over {MAX_EXCERPT_CHARS} chars, longest {longest}",
        )
    )

    unbounded = store.connection.execute(
        "SELECT COUNT(*) FROM node_doc_section "
        "WHERE excerpt_truncated = 0 AND LENGTH(excerpt) > char_count"
    ).fetchone()[0]
    checks.append(
        Check(
            "an_untruncated_excerpt_is_the_whole_section",
            unbounded == 0,
            "every untruncated excerpt is no longer than its section"
            if unbounded == 0 else f"{unbounded} excerpts exceed their section",
        )
    )

    # A flagged document must still be stored. Flagging is a note to the
    # reader, and a filter that quietly dropped text would hide the very thing
    # somebody needs to see.
    dropped = store.connection.execute(
        "SELECT COUNT(*) FROM node_document WHERE injection_flags IS NOT NULL "
        "AND doc_id NOT IN (SELECT doc_id FROM node_doc_section)"
    ).fetchone()[0]
    checks.append(
        Check(
            "flagged_documents_are_kept_not_filtered",
            dropped == 0,
            "every flagged document still has its sections"
            if dropped == 0 else f"{dropped} flagged documents lost their text",
        )
    )

    # -- the evidence vocabulary --------------------------------------

    allowed = {"documented", "tested", "observed"}
    stray = []
    for table in ("node_document", "node_doc_section", "node_test_file",
                  "node_test_class", "node_test_case", "node_migration",
                  "node_business_process", "node_process_step",
                  "node_convention"):
        for row in store.connection.execute(
            f"SELECT DISTINCT evidence AS value FROM {table}"
        ):
            if row["value"] not in allowed:
                stray.append(f"{table}={row['value']!r}")
    checks.append(
        Check(
            "v7_evidence_vocabulary_is_closed",
            not stray,
            "every v7 row is documented, tested or observed"
            if not stray else f"unexpected evidence: {stray}",
        )
    )

    # -- processes are never invented ---------------------------------

    orphan = store.connection.execute(
        "SELECT COUNT(*) FROM node_business_process WHERE doc_id IS NULL "
        "OR doc_id NOT IN (SELECT doc_id FROM node_document)"
    ).fetchone()[0]
    checks.append(
        Check(
            "every_process_comes_from_a_document",
            orphan == 0,
            "every business process names the document it was read from"
            if orphan == 0 else f"{orphan} processes have no document",
        )
    )

    statuses = {
        row["status"] for row in store.connection.execute(
            "SELECT DISTINCT status FROM node_business_process"
        )
    }
    checks.append(
        Check(
            "process_status_vocabulary_is_closed",
            statuses <= {"documented", "incomplete", "unknown"},
            f"statuses in use: {sorted(statuses) or 'none'}",
        )
    )

    # A process is only `documented` when it actually bound to a workflow.
    overclaimed = store.connection.execute(
        "SELECT COUNT(*) FROM node_business_process "
        "WHERE status = 'documented' AND workflow_id IS NULL"
    ).fetchone()[0]
    checks.append(
        Check(
            "a_documented_process_is_bound_to_code",
            overclaimed == 0,
            "every documented process names the workflow it matched"
            if overclaimed == 0
            else f"{overclaimed} processes claim documented with no workflow",
        )
    )

    dangling = store.connection.execute(
        "SELECT COUNT(*) FROM node_business_process WHERE workflow_id IS NOT NULL "
        "AND workflow_id NOT IN (SELECT workflow_id FROM node_workflow)"
    ).fetchone()[0]
    checks.append(
        Check(
            "a_bound_process_points_at_a_real_workflow",
            dangling == 0,
            "every bound process points at a workflow that exists"
            if dangling == 0 else f"{dangling} point at nothing",
        )
    )

    # A step that matched a state must point at one that exists.
    bad_state = store.connection.execute(
        "SELECT COUNT(*) FROM node_process_step WHERE state_id IS NOT NULL "
        "AND state_id NOT IN (SELECT state_id FROM node_workflow_state)"
    ).fetchone()[0]
    checks.append(
        Check(
            "a_matched_step_points_at_a_real_state",
            bad_state == 0,
            "every matched step names a state that exists"
            if bad_state == 0 else f"{bad_state} steps point at nothing",
        )
    )

    # -- conventions are never promoted on their own ------------------

    convention_statuses = {
        row["status"] for row in store.connection.execute(
            "SELECT DISTINCT status FROM node_convention"
        )
    }
    checks.append(
        Check(
            "convention_status_vocabulary_is_closed",
            convention_statuses <= {
                "required_by_odoo", "documented_rule",
                "observed_pattern", "recommendation",
            },
            f"statuses in use: {sorted(convention_statuses) or 'none'}",
        )
    )

    # The rule the milestone states outright: a recommendation may never be
    # presented as an existing organisational rule without a documented source
    # and human approval. This build never promotes, so a documented rule can
    # only exist if a person put it there.
    unsourced = store.connection.execute(
        "SELECT COUNT(*) FROM node_convention "
        "WHERE status = 'documented_rule' AND "
        "(source_rel_path IS NULL OR TRIM(source_rel_path) = '')"
    ).fetchone()[0]
    checks.append(
        Check(
            "a_documented_rule_names_its_source",
            unsourced == 0,
            "no convention claims to be a documented rule without a source"
            if unsourced == 0
            else f"{unsourced} claim to be documented rules with no source",
        )
    )

    # Every convention must show its arithmetic, because the count is the
    # entire claim. A share without a denominator says nothing.
    silent = store.connection.execute(
        "SELECT COUNT(*) FROM node_convention WHERE total_modules <= 0"
    ).fetchone()[0]
    checks.append(
        Check(
            "every_convention_shows_its_arithmetic",
            silent == 0,
            "every convention reports how many modules it counted"
            if silent == 0 else f"{silent} report no denominator",
        )
    )

    # -- edges --------------------------------------------------------

    v7_tables = (
        DOCUMENTATION_EDGE_TABLES + TEST_EDGE_TABLES + PROCESS_EDGE_TABLES
    )
    blank = sum(
        store.connection.execute(
            f"SELECT COUNT(*) FROM edge_{name} "
            "WHERE dst_unresolved IS NULL OR TRIM(dst_unresolved) = ''"
        ).fetchone()[0]
        for name in v7_tables
    )
    checks.append(
        Check(
            "v7_edges_name_their_target",
            blank == 0,
            "no v7 edge carries an empty target"
            if blank == 0 else f"{blank} v7 edges carry an empty target",
        )
    )

    dangling_edges = []
    for table, target_table, target_column in (
        ("document_in_module", "node_module", "technical_name"),
        ("section_in_document", "node_document", "doc_id"),
        ("test_in_module", "node_module", "technical_name"),
        ("test_class_in_file", "node_test_file", "test_file_id"),
        ("test_case_in_class", "node_test_class", "test_class_id"),
        ("migration_in_module", "node_module", "technical_name"),
        ("process_from_document", "node_document", "doc_id"),
        ("process_for_workflow", "node_workflow", "workflow_id"),
        ("process_has_step", "node_process_step", "step_id"),
        ("convention_documented_in_section", "node_doc_section", "section_id"),
    ):
        count = store.connection.execute(
            f"SELECT COUNT(*) FROM edge_{table} WHERE binding = 'resolved' "
            f"AND dst_id NOT IN (SELECT {target_column} FROM {target_table})"
        ).fetchone()[0]
        if count:
            dangling_edges.append(f"edge_{table}: {count}")
    checks.append(
        Check(
            "resolved_v7_edges_point_at_real_nodes",
            not dangling_edges,
            "every resolved v7 edge has a target node"
            if not dangling_edges else f"pointing at nothing: {dangling_edges}",
        )
    )

    # -- the boundary -------------------------------------------------

    # A column that read like a coverage verdict would mean somebody started
    # turning "this test names it" into "this is tested".
    verdict_columns = []
    for table in ("node_test_file", "node_test_class", "node_test_case"):
        for row in store.connection.execute(f"PRAGMA table_info({table})"):
            name = row["name"].lower()
            if any(word in name for word in
                   ("coverage", "covered", "passing", "passed", "failing",
                    "green", "verified", "asserts")):
                verdict_columns.append(f"{table}.{row['name']}")
    checks.append(
        Check(
            "no_test_row_claims_coverage",
            not verdict_columns,
            "the test layer stores references, never coverage"
            if not verdict_columns
            else f"columns that look like coverage: {verdict_columns}",
        )
    )

    # A bare method name bound by uniqueness is this build inferring which
    # model an author meant. It may well be right; it is still a guess, and a
    # guess stored as `derived` is indistinguishable from a reference the
    # author spelled out. The confidence column is the only thing keeping the
    # two apart, so nothing may quietly promote one.
    overconfident = store.connection.execute(
        "SELECT COUNT(*) FROM edge_section_documents_subject "
        "WHERE provenance = ? AND confidence != 'heuristic'",
        (SUBJECT_PROVENANCE["method_unique_bare_name"],),
    ).fetchone()[0]
    checks.append(
        Check(
            "unique_bare_name_bindings_stay_heuristic",
            overconfident == 0,
            "every binding inferred from a bare method name is heuristic"
            if overconfident == 0
            else f"{overconfident} unique_bare_name edge(s) claim more than "
                 "heuristic confidence",
        )
    )

    # The other half of the same rule: a mode that cannot name a target must
    # not appear to have one.
    unbindable = (
        SUBJECT_PROVENANCE["method_ambiguous_bare_name"],
        SUBJECT_PROVENANCE["method_unresolved"],
    )
    wrongly_bound = store.connection.execute(
        "SELECT COUNT(*) FROM edge_section_documents_subject "
        f"WHERE provenance IN ({','.join('?' * len(unbindable))}) "
        "AND (binding != 'unresolved' OR dst_id IS NOT NULL)",
        unbindable,
    ).fetchone()[0]
    checks.append(
        Check(
            "unbindable_method_references_name_no_node",
            wrongly_bound == 0,
            "ambiguous and unresolved method references bind to nothing"
            if wrongly_bound == 0
            else f"{wrongly_bound} edge(s) claim a target they cannot have",
        )
    )

    return checks


#: Lower bounds for the workflow layer. Measured at 1868561: 51 workflows,
#: 133 states, 127 transitions, 273 triggers, 37 scheduled actions. Floors, not
#: equalities -- people add state machines while this runs.
MIN_WORKFLOWS = 44
MIN_WORKFLOW_STATES = 115
MIN_TRANSITIONS = 110
MIN_TRIGGERS = 240
MIN_SCHEDULED_ACTIONS = 32


def _workflow_layer_checks(store: GraphReads, enforce_floors: bool = True) -> list[Check]:
    """Schema-v6 criteria.

    Most of these defend a distinction rather than a count. This layer is only
    worth having if declared, static and heuristic stay apart, if a conditional
    transition keeps its condition, and if nothing here ever says a transition
    ran. A workflow layer that quietly flattened those would be worse than no
    workflow layer at all, because it would be believed.
    """
    checks: list[Check] = []

    for name, table, minimum in (
        ("workflow_workflows", "node_workflow", MIN_WORKFLOWS),
        ("workflow_states", "node_workflow_state", MIN_WORKFLOW_STATES),
        ("workflow_transitions", "node_transition", MIN_TRANSITIONS),
        ("workflow_triggers", "node_trigger", MIN_TRIGGERS),
        ("workflow_scheduled_actions", "node_scheduled_action",
         MIN_SCHEDULED_ACTIONS),
    ):
        count = store.count(table)
        checks.append(_floor(name, count, minimum, enforce_floors))

    # The evidence vocabulary is the whole contract of this layer.
    allowed = {"declared", "static", "heuristic"}
    stray = []
    for table in ("node_workflow", "node_workflow_state", "node_transition",
                  "node_trigger", "node_scheduled_action"):
        for row in store.connection.execute(
            f"SELECT DISTINCT evidence AS value FROM {table}"
        ):
            if row["value"] not in allowed:
                stray.append(f"{table}.evidence={row['value']!r}")
    checks.append(
        Check(
            "workflow_evidence_vocabulary_is_closed",
            not stray,
            "every row is declared, static or heuristic"
            if not stray else f"unexpected evidence: {stray}",
        )
    )

    documented = sum(
        store.connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE evidence = 'documented'"
        ).fetchone()[0]
        for table in ("node_workflow", "node_workflow_state", "node_transition",
                      "node_trigger", "node_scheduled_action")
    )
    checks.append(
        Check(
            "documented_evidence_is_reserved_for_later",
            documented == 0,
            "no row claims documented evidence"
            if documented == 0
            else f"{documented} rows claim documented evidence, which M4.3 owns",
        )
    )

    # A transition whose target is an expression must never be certain: the
    # graph does not know where it goes.
    overconfident = store.connection.execute(
        "SELECT COUNT(*) FROM node_transition "
        "WHERE to_state IS NULL AND confidence = 'certain'"
    ).fetchone()[0]
    checks.append(
        Check(
            "a_transition_with_no_target_is_never_certain",
            overconfident == 0,
            "every transition without a target state is marked heuristic"
            if overconfident == 0
            else f"{overconfident} targetless transitions claim certainty",
        )
    )

    silent = store.connection.execute(
        "SELECT COUNT(*) FROM node_transition "
        "WHERE to_state IS NULL AND (dynamic_expression IS NULL "
        "OR TRIM(dynamic_expression) = '')"
    ).fetchone()[0]
    checks.append(
        Check(
            "a_dynamic_target_keeps_its_expression",
            silent == 0,
            "every targetless transition preserves the expression it could not "
            "read" if silent == 0
            else f"{silent} targetless transitions dropped the expression",
        )
    )

    # Flattening a condition away is exactly the failure this layer must avoid.
    unexplained = store.connection.execute(
        "SELECT COUNT(*) FROM node_transition "
        "WHERE conditional = 1 AND (condition_kind IS NULL "
        "OR TRIM(condition_kind) = '')"
    ).fetchone()[0]
    checks.append(
        Check(
            "a_conditional_transition_says_why",
            unexplained == 0,
            "every conditional transition names its condition kind"
            if unexplained == 0
            else f"{unexplained} conditional transitions explain nothing",
        )
    )

    # A source state is inferred from a guard, so it is only ever heuristic.
    wrong_source = store.connection.execute(
        "SELECT COUNT(*) FROM node_transition "
        "WHERE from_state IS NOT NULL AND from_evidence <> 'heuristic'"
    ).fetchone()[0]
    checks.append(
        Check(
            "a_source_state_is_only_ever_heuristic",
            wrong_source == 0,
            "every from_state is marked heuristic"
            if wrong_source == 0
            else f"{wrong_source} transitions claim a firmer source state",
        )
    )

    # The identity rule: a state belongs to one model and one field.
    shared = store.connection.execute(
        "SELECT COUNT(*) FROM (SELECT state_id FROM node_workflow_state "
        "GROUP BY state_id HAVING COUNT(DISTINCT model_name) > 1)"
    ).fetchone()[0]
    reused = store.connection.execute(
        "SELECT COUNT(*) FROM (SELECT value FROM node_workflow_state "
        "GROUP BY value HAVING COUNT(DISTINCT model_name) > 1)"
    ).fetchone()[0]
    checks.append(
        Check(
            "the_same_state_name_on_two_models_is_two_states",
            shared == 0,
            f"no state id spans two models ({reused} values do occur on more "
            "than one model, each with its own identity)"
            if shared == 0 else f"{shared} state ids span more than one model",
        )
    )

    blank = sum(
        store.connection.execute(
            f"SELECT COUNT(*) FROM edge_{name} "
            "WHERE dst_unresolved IS NULL OR TRIM(dst_unresolved) = ''"
        ).fetchone()[0]
        for name in WORKFLOW_EDGE_TABLES
    )
    checks.append(
        Check(
            "workflow_edges_name_their_target",
            blank == 0,
            "no workflow edge carries an empty target"
            if blank == 0 else f"{blank} workflow edges carry an empty target",
        )
    )

    dangling = []
    for table, target_table, target_column in (
        ("workflow_on_model", "node_model", "model_name"),
        ("workflow_has_state", "node_workflow_state", "state_id"),
        ("transition_in_workflow", "node_workflow", "workflow_id"),
        ("transition_from_state", "node_workflow_state", "state_id"),
        ("transition_to_state", "node_workflow_state", "state_id"),
        ("method_performs_transition", "node_transition", "transition_id"),
        ("trigger_invokes_method", "node_method", "method_id"),
        ("cron_runs_method", "node_method", "method_id"),
        ("cron_on_model", "node_model", "model_name"),
        ("transition_touches_model", "node_model", "model_name"),
        ("route_invokes_method", "node_method", "method_id"),
    ):
        count = store.connection.execute(
            f"SELECT COUNT(*) FROM edge_{table} WHERE binding = 'resolved' "
            f"AND dst_id NOT IN (SELECT {target_column} FROM {target_table})"
        ).fetchone()[0]
        if count:
            dangling.append(f"edge_{table}: {count}")
    checks.append(
        Check(
            "resolved_workflow_edges_point_at_real_nodes",
            not dangling,
            "every resolved workflow edge has a target node"
            if not dangling else f"pointing at nothing: {dangling}",
        )
    )

    orphans = store.connection.execute(
        "SELECT COUNT(*) FROM node_transition WHERE workflow_id NOT IN "
        "(SELECT workflow_id FROM node_workflow)"
    ).fetchone()[0]
    sourceless = store.connection.execute(
        "SELECT COUNT(*) FROM node_transition "
        "WHERE rel_path IS NULL OR TRIM(rel_path) = '' OR line IS NULL"
    ).fetchone()[0]
    checks.append(
        Check(
            "every_transition_has_a_workflow_and_a_source_line",
            orphans == 0 and sourceless == 0,
            "every transition belongs to a workflow and names its line"
            if orphans == 0 and sourceless == 0
            else f"{orphans} orphans, {sourceless} without a source line",
        )
    )

    # The boundary. A column reading like a runtime fact would mean somebody
    # started recording executions rather than readings of code.
    runtime_columns = []
    for table in ("node_workflow", "node_workflow_state", "node_transition",
                  "node_trigger", "node_scheduled_action"):
        for row in store.connection.execute(f"PRAGMA table_info({table})"):
            name = row["name"].lower()
            if any(word in name for word in
                   ("executed", "ran_at", "occurred", "actual", "runtime_",
                    "observed", "instance_count")):
                runtime_columns.append(f"{table}.{row['name']}")
    checks.append(
        Check(
            "no_workflow_row_records_an_execution",
            not runtime_columns,
            "the workflow layer stores readings of code only"
            if not runtime_columns
            else f"columns that look like runtime facts: {runtime_columns}",
        )
    )
    return checks


#: Lower bounds for the frontend layer. Measured at 1868561: 13 bundles,
#: 161 entries, 75 JS files, 32 components, 62 templates, 102 routes. Floors
#: again, not equalities -- people add components while this runs.
MIN_ASSET_BUNDLES = 11
MIN_ASSET_ENTRIES = 140
MIN_JS_FILES = 65
MIN_OWL_COMPONENTS = 25
MIN_OWL_TEMPLATES = 50
MIN_ROUTES = 90


def _frontend_layer_checks(store: GraphReads, enforce_floors: bool = True) -> list[Check]:
    """Schema-v5 criteria.

    Two things are being defended here. One is the ordinary integrity of the
    layer: nothing empty, nothing dangling, nothing silently dropped. The other
    is the honesty of it -- JavaScript is lexed rather than parsed, so its edges
    must never claim to be certain, and a route must never carry a column that
    looks like a decision about who may call it.
    """
    checks: list[Check] = []

    for name, table, minimum in (
        ("frontend_asset_bundles", "node_asset_bundle", MIN_ASSET_BUNDLES),
        ("frontend_asset_entries", "node_asset_entry", MIN_ASSET_ENTRIES),
        ("frontend_js_files", "node_js_file", MIN_JS_FILES),
        ("frontend_owl_components", "node_owl_component", MIN_OWL_COMPONENTS),
        ("frontend_owl_templates", "node_owl_template", MIN_OWL_TEMPLATES),
        ("frontend_routes", "node_route", MIN_ROUTES),
    ):
        count = store.count(table)
        checks.append(_floor(name, count, minimum, enforce_floors))

    # A file the lexer could not finish is a file whose facts are missing
    # rather than absent. It must be visible, not swallowed.
    unlexed = [
        f"{row['rel_path']} ({row['lex_state']})"
        for row in store.connection.execute(
            "SELECT rel_path, lex_state FROM node_js_file WHERE lex_state <> 'ok' "
            "ORDER BY rel_path LIMIT 10"
        )
    ]
    checks.append(
        Check(
            "every_js_file_lexed_cleanly",
            not unlexed,
            "every JavaScript file lexed to completion"
            if not unlexed else f"unfinished: {unlexed}",
        )
    )

    # No frontend edge may carry a blank target: an edge that does not say what
    # it was looking for cannot be audited later.
    blank_edges = sum(
        store.connection.execute(
            f"SELECT COUNT(*) FROM edge_{name} "
            "WHERE dst_unresolved IS NULL OR TRIM(dst_unresolved) = ''"
        ).fetchone()[0]
        for name in FRONTEND_EDGE_TABLES
    )
    checks.append(
        Check(
            "frontend_edges_name_their_target",
            blank_edges == 0,
            "no frontend edge carries an empty target"
            if blank_edges == 0
            else f"{blank_edges} frontend edges carry an empty target",
        )
    )

    # A resolved edge must point at a row that exists, in the table its kind
    # implies. This is what separates "resolved" from "we hoped".
    dangling = []
    for table, target_table, target_column in (
        ("js_imports", "node_js_file", "js_id"),
        ("js_calls_route", "node_route", "route_id"),
        ("js_references_model", "node_model", "model_name"),
        ("component_uses_template", "node_owl_template", "template_name"),
        ("template_inherits", "node_owl_template", "template_name"),
        ("asset_in_bundle", "node_asset_bundle", "bundle_name"),
    ):
        count = store.connection.execute(
            f"SELECT COUNT(*) FROM edge_{table} WHERE binding = 'resolved' "
            f"AND dst_id NOT IN (SELECT {target_column} FROM {target_table})"
        ).fetchone()[0]
        if count:
            dangling.append(f"edge_{table}: {count}")
    checks.append(
        Check(
            "resolved_frontend_edges_point_at_real_nodes",
            not dangling,
            "every resolved frontend edge has a target node"
            if not dangling else f"pointing at nothing: {dangling}",
        )
    )

    # Every manifest entry must sit in exactly one bundle, and each bundle's
    # stored entry_count must agree with the edges. A drift here means the
    # asset picture is quietly incomplete.
    entries = store.count("node_asset_entry")
    in_bundle = store.count("edge_asset_in_bundle")
    mismatched = [
        f"{row['bundle_name']}: says {row['entry_count']}, has {row['actual']}"
        for row in store.connection.execute(
            "SELECT b.bundle_name, b.entry_count, "
            "(SELECT COUNT(*) FROM edge_asset_in_bundle e "
            " WHERE e.dst_id = b.bundle_name) AS actual "
            "FROM node_asset_bundle b WHERE b.entry_count <> actual"
        )
    ]
    checks.append(
        Check(
            "every_asset_entry_sits_in_its_bundle",
            entries == in_bundle and not mismatched,
            f"{entries} entries, {in_bundle} bundle edges"
            + ("" if not mismatched else f"; counts disagree: {mismatched}"),
        )
    )

    # A component must belong to a JS file that exists, and a route must name
    # the module, controller and method it was read from.
    orphan_components = store.connection.execute(
        "SELECT COUNT(*) FROM node_owl_component "
        "WHERE js_id NOT IN (SELECT js_id FROM node_js_file)"
    ).fetchone()[0]
    nameless_routes = store.connection.execute(
        "SELECT COUNT(*) FROM node_route WHERE module IS NULL OR TRIM(module) = '' "
        "OR controller IS NULL OR TRIM(controller) = '' "
        "OR method IS NULL OR TRIM(method) = '' "
        "OR rel_path IS NULL OR line IS NULL"
    ).fetchone()[0]
    checks.append(
        Check(
            "frontend_nodes_name_their_source",
            orphan_components == 0 and nameless_routes == 0,
            "every component has a file and every route a method and line"
            if orphan_components == 0 and nameless_routes == 0
            else f"{orphan_components} orphan components, "
                 f"{nameless_routes} routes without a source",
        )
    )

    # JavaScript is lexed, not parsed. Nothing read out of it may claim to be
    # certain -- if this ever passes 'certain', a reader would take a guess for
    # a fact.
    overconfident = [
        f"edge_{name}: {count}"
        for name, count in (
            (name, store.connection.execute(
                f"SELECT COUNT(*) FROM edge_{name} WHERE confidence = 'certain'"
            ).fetchone()[0])
            for name in ("js_imports", "js_registers", "js_patches",
                         "js_uses_service", "js_calls_route",
                         "js_references_model", "component_uses_template")
        ) if count
    ]
    checks.append(
        Check(
            "javascript_facts_are_never_certain",
            not overconfident,
            "every JavaScript-derived edge is marked heuristic or derived"
            if not overconfident else f"claiming certainty: {overconfident}",
        )
    )

    # A route stores what the decorator declares. `auth` is NULL when the
    # source writes none: filling in Odoo's runtime default would turn a
    # reading of the code into a claim about the running system.
    empty_auth = store.connection.execute(
        "SELECT COUNT(*) FROM node_route WHERE auth IS NOT NULL AND TRIM(auth) = ''"
    ).fetchone()[0]
    checks.append(
        Check(
            "undeclared_auth_is_null_not_blank",
            empty_auth == 0,
            "a route without a declared auth stores NULL, not an empty string"
            if empty_auth == 0
            else f"{empty_auth} routes store a blank auth",
        )
    )

    # The MCP layer answers about eight frontend kinds through one registry of
    # (table, id column, name column). A schema change that renamed a column
    # would leave the registry pointing at nothing, and the failure would show
    # up as a tool erroring in front of a user rather than here.
    from .mcp import m4queries

    broken = []
    for name, kind in m4queries.KINDS.items():
        columns = {
            row["name"] for row in
            store.connection.execute(f"PRAGMA table_info({kind.table})")
        }
        if not columns:
            broken.append(f"{name}: no table {kind.table}")
            continue
        for label, column in (("id", kind.id_column), ("name", kind.name_column),
                              ("path", kind.path_column),
                              ("line", kind.line_column)):
            if column and column not in columns:
                broken.append(f"{name}: {label} column {kind.table}.{column}")
    checks.append(
        Check(
            "frontend_kind_registry_matches_schema",
            not broken,
            f"all {len(m4queries.KINDS)} searchable kinds resolve against the "
            "schema" if not broken else f"registry points at nothing: {broken}",
        )
    )

    # Every edge table the traversal knows about must exist, and every table
    # that exists must be reachable from some kind -- otherwise a whole class
    # of relation is silently unwalkable.
    from .mcp import m4impact

    routed = set()
    for mapping in (m4impact.FORWARD_TABLES, m4impact.REVERSE_TABLES):
        for tables in mapping.values():
            routed.update(tables)
    unrouted = sorted(set(FRONTEND_EDGE_TABLES) - routed)
    unknown = sorted(routed - set(FRONTEND_EDGE_TABLES))
    checks.append(
        Check(
            "every_frontend_edge_table_is_traversable",
            not unrouted and not unknown,
            "every frontend edge table is reachable from some kind"
            if not unrouted and not unknown
            else f"unrouted: {unrouted}; unknown: {unknown}",
        )
    )

    # The boundary, restated for routes. Whether a request is permitted depends
    # on the method body, the ACLs and the record rules. A column here that
    # looked like an answer would mean somebody started deciding.
    verdict_columns = [
        f"node_route.{row['name']}"
        for row in store.connection.execute("PRAGMA table_info(node_route)")
        if any(word in row["name"].lower() for word in
               ("effective", "allowed", "is_safe", "granted", "has_access",
                "verdict", "permitted"))
    ]
    checks.append(
        Check(
            "no_route_access_verdict_is_stored",
            not verdict_columns,
            "routes store the declaration only"
            if not verdict_columns
            else f"columns that look like access decisions: {verdict_columns}",
        )
    )
    return checks


#: Lower bounds for the security layer. Measured at 4add1cf: 41 groups,
#: 376 ACL rows, 81 record rules.
MIN_GROUPS = 38
MIN_ACL_ROWS = 360
MIN_RULES = 75


def _security_layer_checks(store: GraphReads, enforce_floors: bool = True) -> list[Check]:
    """Schema-v4 criteria.

    Several of these assert an absence rather than a presence, because the way
    this layer fails is by starting to answer a question it must not.
    """
    checks: list[Check] = []

    for name, table, minimum in (
        ("security_groups", "node_group", MIN_GROUPS),
        ("security_acl_rows", "node_acl", MIN_ACL_ROWS),
        ("security_record_rules", "node_rule", MIN_RULES),
    ):
        count = store.count(table)
        checks.append(_floor(name, count, minimum, enforce_floors))

    # Every ACL row and rule must say which model it is about.
    for name, table, column in (
        ("acl_rows_name_a_model", "node_acl", "model_target"),
        ("rules_name_a_model", "node_rule", "model_target"),
    ):
        blank = store.connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL OR TRIM({column}) = ''"
        ).fetchone()[0]
        checks.append(
            Check(
                name, blank == 0,
                "every row names a model" if blank == 0
                else f"{blank} rows name no model",
            )
        )

    # A global ACL or rule has no group edge; a non-global one must have at
    # least one. Getting this backwards would silently widen or narrow what the
    # graph reports.
    contradictions = store.connection.execute(
        "SELECT COUNT(*) FROM node_acl a WHERE a.is_global = 1 AND EXISTS "
        "(SELECT 1 FROM edge_acl_grants_group e WHERE e.src_id = a.acl_id)"
    ).fetchone()[0] + store.connection.execute(
        "SELECT COUNT(*) FROM node_rule r WHERE r.is_global = 1 AND EXISTS "
        "(SELECT 1 FROM edge_rule_for_group e WHERE e.src_id = r.rule_id)"
    ).fetchone()[0]
    checks.append(
        Check(
            "global_means_no_group_edge",
            contradictions == 0,
            "global rows carry no group edge"
            if contradictions == 0
            else f"{contradictions} global rows also name a group",
        )
    )

    orphaned = store.connection.execute(
        "SELECT COUNT(*) FROM node_acl a WHERE a.is_global = 0 AND NOT EXISTS "
        "(SELECT 1 FROM edge_acl_grants_group e WHERE e.src_id = a.acl_id)"
    ).fetchone()[0]
    checks.append(
        Check(
            "non_global_acls_name_a_group",
            orphaned == 0,
            "every non-global ACL row names a group"
            if orphaned == 0
            else f"{orphaned} non-global ACL rows name no group",
        )
    )

    # A resolved security edge must point at something that exists.
    dangling = store.connection.execute(
        "SELECT COUNT(*) FROM edge_acl_on_model WHERE binding = 'resolved' "
        "AND dst_id NOT IN (SELECT model_name FROM node_model)"
    ).fetchone()[0] + store.connection.execute(
        "SELECT COUNT(*) FROM edge_acl_grants_group WHERE binding = 'resolved' "
        "AND dst_id NOT IN (SELECT group_xmlid FROM node_group)"
    ).fetchone()[0]
    checks.append(
        Check(
            "resolved_security_edges_point_at_real_nodes",
            dangling == 0,
            "every resolved security edge has a target node"
            if dangling == 0
            else f"{dangling} resolved security edges point at nothing",
        )
    )

    # No security edge may carry a blank target.
    blank_edges = sum(
        store.connection.execute(
            f"SELECT COUNT(*) FROM edge_{name} "
            "WHERE dst_unresolved IS NULL OR TRIM(dst_unresolved) = ''"
        ).fetchone()[0]
        for name in SECURITY_EDGE_TABLES
    )
    checks.append(
        Check(
            "security_edges_name_their_target",
            blank_edges == 0,
            "no security edge carries an empty target"
            if blank_edges == 0
            else f"{blank_edges} security edges carry an empty target",
        )
    )

    # -- res.groups.privilege ------------------------------------------

    # Every privilege record a data file declares must have a node.
    declared_privileges = store.connection.execute(
        "SELECT COUNT(*) FROM node_xml_record WHERE record_model = 'res.groups.privilege' "
        "AND xmlid IS NOT NULL"
    ).fetchone()[0]
    privilege_nodes = store.count("node_privilege")
    checks.append(
        Check(
            "every_declared_privilege_has_a_node",
            privilege_nodes >= declared_privileges
            if declared_privileges else privilege_nodes == 0,
            f"{privilege_nodes} privilege node(s) for {declared_privileges} declared "
            "record(s)"
            + (
                "" if privilege_nodes >= declared_privileges
                else " -- a declared privilege was not indexed"
            ),
        )
    )

    # A group's privilege_id must resolve to a named target, never to a blank.
    unnamed = store.connection.execute(
        "SELECT COUNT(*) FROM edge_group_in_privilege "
        "WHERE dst_unresolved IS NULL OR TRIM(dst_unresolved) = ''"
    ).fetchone()[0]
    checks.append(
        Check(
            "group_privileges_name_their_target",
            unnamed == 0,
            "every privilege_id edge names a target"
            if unnamed == 0
            else f"{unnamed} privilege edges carry an empty target",
        )
    )

    # Every group row declaring a privilege must have produced an edge, and
    # every resolved edge must point at a node that exists.
    missing_edge = store.connection.execute(
        "SELECT COUNT(*) FROM node_group g WHERE g.privilege_xmlid IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM edge_group_in_privilege e "
        "WHERE e.src_id = g.group_xmlid)"
    ).fetchone()[0]
    dangling_privilege = store.connection.execute(
        "SELECT COUNT(*) FROM edge_group_in_privilege WHERE binding = 'resolved' "
        "AND dst_id NOT IN (SELECT privilege_xmlid FROM node_privilege)"
    ).fetchone()[0]
    checks.append(
        Check(
            "group_privilege_edges_are_complete_and_bound",
            missing_edge == 0 and dangling_privilege == 0,
            "every declared privilege_id has an edge, and every resolved edge a node"
            if missing_edge == 0 and dangling_privilege == 0
            else f"{missing_edge} groups without an edge, "
                 f"{dangling_privilege} edges pointing at nothing",
        )
    )

    # Provenance: a privilege edge must say where the claim was written.
    no_provenance = store.connection.execute(
        "SELECT COUNT(*) FROM edge_group_in_privilege WHERE provenance IS NULL "
        "OR TRIM(provenance) = '' OR src_file IS NULL OR src_line IS NULL"
    ).fetchone()[0]
    checks.append(
        Check(
            "privilege_edges_carry_provenance",
            no_provenance == 0,
            "every privilege edge names its file, line and provenance"
            if no_provenance == 0
            else f"{no_provenance} privilege edges lack provenance",
        )
    )

    # The boundary again, for privileges specifically. A privilege is
    # organisational metadata; a column or edge implying it confers rights
    # would mean somebody started treating it as a permission.
    privilege_verdicts = []
    for row in store.connection.execute("PRAGMA table_info(node_privilege)"):
        name = row["name"].lower()
        if any(word in name for word in
               ("perm_", "allowed", "access", "granted", "effective", "right")):
            privilege_verdicts.append(f"node_privilege.{row['name']}")
    checks.append(
        Check(
            "a_privilege_is_not_stored_as_a_permission",
            not privilege_verdicts,
            "privileges hold identity metadata only"
            if not privilege_verdicts
            else f"columns implying rights: {privilege_verdicts}",
        )
    )

    # The boundary. A column named like a verdict would mean somebody started
    # computing access, which this layer must never do.
    verdict_columns = []
    for table in ("node_acl", "node_rule", "node_group"):
        for row in store.connection.execute(f"PRAGMA table_info({table})"):
            name = row["name"].lower()
            if any(word in name for word in
                   ("effective", "allowed", "granted_to", "has_access", "verdict")):
                verdict_columns.append(f"{table}.{row['name']}")
    checks.append(
        Check(
            "no_access_verdict_is_stored",
            not verdict_columns,
            "the security layer stores declared evidence only"
            if not verdict_columns
            else f"columns that look like access decisions: {verdict_columns}",
        )
    )
    return checks


#: Lower bounds for the XML layer, alongside the Python ones and for the same
#: reason: the project grows while the milestone is being built.
MIN_DATA_FILES = 430
MIN_XMLIDS = 1750
MIN_XML_RECORDS = 1780


def _xml_layer_checks(store: GraphReads, enforce_floors: bool = True) -> list[Check]:
    """Schema-v3 criteria."""
    checks: list[Check] = []

    for name, table, minimum in (
        ("xml_data_files", "node_data_file", MIN_DATA_FILES),
        ("xml_ids", "node_xmlid", MIN_XMLIDS),
        ("xml_records", "node_xml_record", MIN_XML_RECORDS),
    ):
        count = store.count(table)
        checks.append(_floor(name, count, minimum, enforce_floors))

    # Every declared XML file must have been read or have said why not.
    unparsed = store.connection.execute(
        "SELECT COUNT(*) FROM node_data_file WHERE kind = 'xml' "
        "AND parse_state = 'not_parsed'"
    ).fetchone()[0]
    checks.append(
        Check(
            "declared_xml_was_read",
            unparsed == 0,
            "every declared XML file was read or reported"
            if unparsed == 0
            else f"{unparsed} declared XML files were neither read nor reported",
        )
    )

    # An XML ID's winner must be one of its own definitions.
    bad_winner = store.connection.execute(
        "SELECT COUNT(*) FROM node_xmlid x WHERE x.winner_record_id NOT IN "
        "(SELECT dst_id FROM edge_xmlid_defined_by WHERE src_id = x.xmlid)"
    ).fetchone()[0]
    checks.append(
        Check(
            "xmlid_winner_is_one_of_its_definitions",
            bad_winner == 0,
            "every winner is a real definition"
            if bad_winner == 0
            else f"{bad_winner} XML IDs point at a definition that is not theirs",
        )
    )

    # Definition count must match the edges that record the definitions.
    mismatched = store.connection.execute(
        "SELECT COUNT(*) FROM node_xmlid x WHERE x.definition_count != "
        "(SELECT COUNT(*) FROM edge_xmlid_defined_by WHERE src_id = x.xmlid)"
    ).fetchone()[0]
    checks.append(
        Check(
            "xmlid_definition_counts_agree",
            mismatched == 0,
            "definition counts match their edges"
            if mismatched == 0
            else f"{mismatched} XML IDs disagree with their definition edges",
        )
    )

    # A resolved reference must point at an XML ID that exists.
    dangling = store.connection.execute(
        "SELECT COUNT(*) FROM edge_xmlid_references e WHERE e.binding = 'resolved' "
        "AND e.dst_id NOT IN (SELECT xmlid FROM node_xmlid)"
    ).fetchone()[0]
    checks.append(
        Check(
            "resolved_references_point_at_real_ids",
            dangling == 0,
            "every resolved reference has a target node"
            if dangling == 0
            else f"{dangling} resolved references point at nothing",
        )
    )

    # Every unresolved reference must be explained.
    unexplained = store.connection.execute(
        "SELECT COUNT(*) FROM edge_xmlid_references e WHERE e.binding = 'unresolved' "
        "AND e.src_id NOT IN (SELECT node_id FROM diagnostic_node "
        "WHERE category = 'xmlid_reference_unresolved')"
    ).fetchone()[0]
    checks.append(
        Check(
            "unresolved_references_are_explained",
            unexplained == 0,
            "every unresolved reference has a diagnostic"
            if unexplained == 0
            else f"{unexplained} unresolved references have no diagnostic",
        )
    )

    # The security layer is M3.3. Nothing here may have started on it.
    security = store.connection.execute(
        "SELECT COUNT(*) FROM node_data_file WHERE kind = 'csv' "
        "AND parse_state NOT IN ('not_parsed', 'missing', 'excluded')"
    ).fetchone()[0]
    checks.append(
        Check(
            "acl_csv_content_is_not_indexed",
            security == 0,
            "no ACL CSV was parsed (that is M3.3)"
            if security == 0
            else f"{security} ACL CSVs were parsed; that is out of scope",
        )
    )
    return checks


#: Lower bounds for the Python layer, mirroring the acceptance suite. Bounds
#: rather than exact counts: testing19 gains models while this is being built,
#: and a growing project must not read as a broken indexer.
MIN_MODEL_DEFS = 470
MIN_MODELS = 295
MIN_FIELDS = 2650
MIN_METHODS = 1800


def _python_layer_checks(store: GraphReads, enforce_floors: bool = True) -> list[Check]:
    """Schema-v2 criteria, so `verify` and the test suite cannot disagree."""
    checks: list[Check] = []

    for name, table, minimum in (
        ("python_model_defs", "node_model_def", MIN_MODEL_DEFS),
        ("python_models", "node_model", MIN_MODELS),
        ("python_fields", "node_field", MIN_FIELDS),
        ("python_methods", "node_method", MIN_METHODS),
    ):
        count = store.count(table)
        checks.append(_floor(name, count, minimum, enforce_floors))

    # Every relational field whose comodel was not written down must have been
    # through the second pass and come out with a verdict.
    pending = store.connection.execute(
        "SELECT COUNT(*) FROM node_field WHERE is_relational = 1 "
        "AND comodel_literal IS NULL AND related_path IS NOT NULL "
        "AND comodel_state NOT IN ('derived', 'unresolved')"
    ).fetchone()[0]
    checks.append(
        Check(
            "related_comodels_resolved_or_reported",
            pending == 0,
            "every related field has a second-pass verdict"
            if pending == 0
            else f"{pending} related fields never reached the second pass",
        )
    )

    # An edge that could not be bound must never carry a target node, and an
    # unbound comodel must always be explained.
    contradictions = store.connection.execute(
        "SELECT COUNT(*) FROM edge_field_relation "
        "WHERE binding IN ('external', 'unresolved') AND dst_id IS NOT NULL"
    ).fetchone()[0]
    checks.append(
        Check(
            "unbound_edges_invent_no_nodes",
            contradictions == 0,
            "no unbound edge points at a node"
            if contradictions == 0
            else f"{contradictions} unbound edges carry a dst_id",
        )
    )

    unexplained = store.connection.execute(
        "SELECT COUNT(*) FROM edge_field_relation e WHERE e.binding = 'unresolved' "
        "AND e.src_id NOT IN (SELECT node_id FROM diagnostic_node "
        "WHERE category = 'field_comodel_unresolved')"
    ).fetchone()[0]
    checks.append(
        Check(
            "unresolved_comodels_are_explained",
            unexplained == 0,
            "every unresolved comodel has a diagnostic"
            if unexplained == 0
            else f"{unexplained} unresolved comodels have no diagnostic",
        )
    )

    phantom = store.connection.execute(
        "SELECT COUNT(*) FROM node_model WHERE model_name IN "
        "(SELECT dst_unresolved FROM edge_model_inherits WHERE binding = 'external')"
    ).fetchone()[0]
    checks.append(
        Check(
            "external_models_are_not_invented",
            phantom == 0,
            "no external inherit target became a node"
            if phantom == 0
            else f"{phantom} external targets were turned into model nodes",
        )
    )

    # An edge that could not bind is still evidence; an empty target is not.
    blank = sum(
        store.connection.execute(
            f"SELECT COUNT(*) FROM edge_{name} "
            "WHERE dst_unresolved IS NULL OR TRIM(dst_unresolved) = ''"
        ).fetchone()[0]
        for name in EDGE_TABLES
    )
    checks.append(
        Check(
            "every_edge_names_its_target",
            blank == 0,
            "no edge carries an empty target"
            if blank == 0
            else f"{blank} edges carry an empty dst_unresolved",
        )
    )

    errors = store.connection.execute(
        "SELECT COUNT(*) FROM diagnostic_node WHERE severity = 'error'"
    ).fetchone()[0]
    # Under testing19 a finding is a defect in a tree that has none. In any other
    # project it is a fact about that project (a manifest naming a missing file),
    # and the graph is how people find it: reported, never hidden, but it does
    # not stop the graph from being published.
    checks.append(
        Check(
            "no_error_level_python_diagnostic",
            errors == 0 or not enforce_floors,
            "no error-level finding" if errors == 0
            else f"{errors} error-level findings"
            + ("" if enforce_floors else " (reported, not enforced: generic profile; see diagnostic_node)"),
        )
    )
    return checks


def _file_hygiene_checks(config: Config, store: GraphReads) -> list[Check]:
    """No excluded directory or forbidden pattern may appear in node_file."""
    checks: list[Check] = []
    rows = store.connection.execute("SELECT file_key, rel_path FROM node_file").fetchall()

    offenders: list[str] = []
    for row in rows:
        segments = row["file_key"].split("/")
        for excluded in BUILTIN_DIR_NAMES:
            if excluded in segments:
                offenders.append(f"{row['rel_path']} (in {excluded})")
                break
    checks.append(
        Check(
            "no_excluded_directories",
            not offenders,
            "no excluded directory contents indexed"
            if not offenders
            else f"{len(offenders)} offenders, e.g. {offenders[:3]}",
        )
    )

    pattern_offenders = [
        row["rel_path"]
        for row in rows
        if config.exclusions.check_file(row["rel_path"].rsplit("/", 1)[-1]).excluded
    ]
    checks.append(
        Check(
            "no_forbidden_patterns",
            not pattern_offenders,
            "no forbidden file pattern indexed"
            if not pattern_offenders
            else f"{len(pattern_offenders)} offenders, e.g. {pattern_offenders[:3]}",
        )
    )

    root_keys = config.include_root_keys
    outside = [
        row["rel_path"]
        for row in rows
        if not any(
            row["file_key"] == root.rstrip("/") or row["file_key"].startswith(root.rstrip("/") + "/")
            for root in root_keys
        )
    ]
    checks.append(
        Check(
            "all_files_inside_include_roots",
            not outside,
            "every indexed file is inside an include root"
            if not outside
            else f"{len(outside)} outside, e.g. {outside[:3]}",
        )
    )

    core_root = config.core_root
    if core_root is not None:
        core_prefix = core_root.key.rstrip("/") + "/"
        from_core = [
            row["rel_path"] for row in rows if row["file_key"].startswith(core_prefix)
        ]
        checks.append(
            Check(
                "no_core_files_indexed",
                not from_core,
                "no Odoo core file indexed (out of scope for M1)"
                if not from_core
                else f"{len(from_core)} core files indexed",
            )
        )
    return checks


def summarize(checks: list[Check]) -> tuple[int, int]:
    passed = sum(1 for check in checks if check.passed)
    return passed, len(checks)
