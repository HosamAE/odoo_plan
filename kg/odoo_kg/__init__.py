"""odoo-kg: deterministic Odoo knowledge-graph indexer.

Milestone M3.1 scope: everything M1 and M2 shipped -- project configuration,
safe Windows path handling, exclusion rules, git/worktree baseline, SQLite
schema, manifest parsing, Module nodes, depends_on relations and the read-only
MCP query server -- plus a static Python layer: Model, ModelDef, Field and
Method nodes with their inheritance, declaration and relation edges.

What is indexed and what is not is published rather than implied, because a
client that cannot see a relationship must be able to tell "it does not exist"
apart from "this milestone does not look for it". See ``LAYER_STATUS``.
"""

#: The single source of truth for this project's version.
#:
#: `pyproject.toml` reads it from here via setuptools' dynamic metadata, and
#: the MCP server reports it as `serverInfo.version`. Keeping one definition
#: means a released package and a running server can never disagree about
#: which build a client is talking to.
#: 0.4.0 is the first stable release: M4 complete at schema version 7, with
#: generation-based publishing so a build never rewrites the graph a reader
#: holds. The pre-releases that led here (0.4.0.dev1 .. 0.4.0rc1) sort below it
#: under PEP 440, so no earlier build can be mistaken for this one.
__version__ = "0.4.0"

# Bumped whenever the SQLite schema changes shape. An existing database with a
# different value must be rebuilt (`index --rebuild`) rather than migrated,
# because no migration promises are made yet.
#
# v2 (M3.1) is additive: every v1 table keeps its columns, constraints and
# digest semantics, and the Python layer sits in new tables beside them.
#
# v3 (M3.2) adds the XML layer beside the other two: data files in manifest
# load order, XML IDs, the elements that define them, and the references
# between them.
#
# v4 (M3.3) adds the security layer: groups and their implications, ACL rows,
# record rules, and what each applies to. Declared evidence only -- the graph
# never decides whether a user has access.
#
# v5 (M4.1) adds the frontend layer: asset bundles, JavaScript and TypeScript,
# OWL components and templates, and HTTP controllers and routes.
#
# v6 (M4.2) adds the workflow layer: state-carrying fields as workflows, their
# declared states, the transitions that set them, and the buttons, scheduled
# actions and routes that trigger them.
#
# v7 (M4.3) adds documentation, tests, migrations, documented business
# processes and module conventions. Every row there is a claim somebody made,
# never a fact the compiler enforces, so each carries its own evidence kind.
SCHEMA_VERSION = 7

#: Which milestone this build belongs to. Reported in the CLI banner, written
#: into the project row and every index run, and read back by `kg_status` and
#: `kg_stats`. It moved to M4 when the frontend layer shipped; it had lagged
#: behind the version for one milestone, which meant a graph built by an M4
#: build described itself as M3 to anyone reading the project row.
MILESTONE = "M4"

#: What this build actually looked at. Ordered most-complete first. Anything
#: absent here is absent from the graph by design, not by accident -- a caller
#: must never read "no edge" as "no relationship in the source".
LAYER_STATUS: tuple[tuple[str, str], ...] = (
    ("modules", "indexed"),
    ("python", "indexed"),
    ("xml", "indexed"),
    ("security", "indexed"),
    ("frontend", "indexed"),
    ("workflow", "indexed"),
    ("documentation", "indexed"),
    ("tests", "indexed"),
    ("process_and_conventions", "indexed"),
    # M4.2 reads method bodies for one purpose only -- finding state writes.
    # It does not build a call graph and does not interpret what a method does.
    ("method_bodies", "read for state writes only"),
    ("call_graph", "not indexed"),
)

#: The same statement in the words the milestone uses, for humans reading CLI
#: output or an MCP status response.
LAYER_SUMMARY: tuple[str, ...] = (
    "Python layer indexed",
    "XML layer indexed (data files, XML IDs, records, references)",
    "security layer indexed as DECLARED EVIDENCE ONLY (groups, ACL rows, "
    "record rules) - never as an access decision",
    "frontend layer indexed (asset bundles, JS/TS, OWL, HTTP routes) - "
    "JavaScript is lexed, never executed, and its facts are heuristic",
    "workflow layer indexed (state fields, declared states, transitions, "
    "XML buttons, scheduled actions) - declared/static/heuristic evidence is "
    "kept apart and a heuristic transition is never a confirmed process",
    "method bodies read only for state writes; no call graph, no method "
    "semantics, and no claim that a transition actually ran",
    "documentation indexed as UNTRUSTED CONTENT - excerpts are bounded, "
    "instruction-like text is flagged and stored, and nothing in a document "
    "is ever followed",
    "tests indexed as references only - naming a subject is not coverage of "
    "it, and no test naming it is not evidence that it is untested",
    "processes and conventions carry documented/tested/observed apart; an "
    "observed pattern is never presented as a rule",
)

#: Known limitations this build ships with, and what has to happen before the
#: final release. Written here rather than in a note somewhere because the list
#: is part of what the build is, and because the alternative is remembering.
#:
#: Each entry is (area, limitation, what a release needs).
OPEN_RELEASE_ITEMS: tuple[tuple[str, str, str], ...] = ()

#: Limitations that are accepted and documented rather than fixed, with the
#: conditions that make each one acceptable. A limitation without conditions is
#: not documented, it is excused.
OPERATING_LIMITATIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "pointer swap while another process holds CURRENT open",
        "Publishing moves one small file, CURRENT, with os.replace. On Windows "
        "that fails while any process holds the target open, because open() "
        "there does not grant delete sharing. This program's own readers do "
        "not hold it -- read_pointer opens, parses and closes -- so the window "
        "belongs to somebody else: a virus scanner walking the data directory, "
        "a backup agent, an editor left open on the file. The publish retries "
        "for five seconds and then refuses.",
        (
            "the published generation stays current and usable",
            "the finished new generation is kept on disk, complete and checked",
            "it can be published later with `odoo-kg publish --generation <id>`, "
            "which repeats the full validation and does not index again",
            "the operator is told the recovery command by name",
        ),
    ),
)

#: Release items that were open and are now closed, with what closed them.
#: Kept rather than deleted: "we decided this was fine" and "we fixed this" are
#: different states, and a reader of the next milestone should be able to tell
#: which one applies.
CLOSED_RELEASE_ITEMS: tuple[tuple[str, str], ...] = (
    (
        "writes following a reparse point out of the data directory",
        "Closed by resolving both sides before comparing them. The approved "
        "root is resolved once (odoo_kg.safety.resolved_data_root) and every "
        "program-managed path -- CURRENT, its temporary file, build.lock, the "
        "generations directory, a generation directory and its database -- must "
        "resolve to somewhere under it, checked before anything is created and "
        "again immediately before the pointer moves. A redirected "
        "%LOCALAPPDATA% or a data_dir that is itself a junction still works, "
        "because both sides resolve; a junction inside the tree that leads out "
        "of it does not. Proved by tests/test_generation_recovery.py, with real "
        "directory junctions where the platform allows creating them and "
        "hermetic tests of the decision everywhere.",
    ),
    (
        "a crashed build blocking the project permanently",
        "Closed by `odoo-kg unlock`. The lock records id, pid, host, start time "
        "and the holder process's own start time, written to a temporary file "
        "and renamed so a half-written lock cannot be read as valid. Recovery "
        "reports before it acts, requires the lock's exact id, refuses outright "
        "while the holder is running locally, detects a reused pid by comparing "
        "process start times, and needs --force for a lock from another host "
        "whose holder cannot be checked. Proved by "
        "tests/test_generation_recovery.py.",
    ),
    (
        "concurrent rebuild while a reader holds the graph",
        "Closed by generation-based publishing (odoo_kg.generations). A build "
        "no longer rewrites the published database: it writes "
        "generations/gen-<run_id>/graph.db, checks it, and swaps a pointer. "
        "The file a reader holds is never the file a build writes, so "
        "immutable=1 is no longer a promise this program can break. Two builds "
        "at once are refused by an O_EXCL lock rather than interleaved, and an "
        "interrupted build cannot change what is published. Proved by "
        "tests/test_generations.py, including a reader held open across a full "
        "index run and a Windows file handle on the previous generation.",
    ),
)
