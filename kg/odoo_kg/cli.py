"""Command-line interface.

Every command is classified, and the classification decides how it may open the
database. This is not documentation of an intention: :data:`COMMAND_KINDS` is
the list, and a test walks it and proves each command's actual behaviour.

===============  ========  ========================================================
command          kind      database access
===============  ========  ========================================================
``index``        writing   builds a NEW generation, then publishes it. It never
                           opens the published database at all.
``doctor``       reading   none: it reports configuration and stats files.
``stats``        reading   ``GraphReader`` on the published generation.
``status``       reading   ``GraphReader``; fresh/stale verdict.
``published``    reading   reads the pointer and re-checks the file it names.
``generations``  reading   lists directories. Deletes nothing, ever.
``verify``       reading   ``GraphReader`` on the published generation.
``serve``        server    the MCP server's read-only connection, same opener.
===============  ========  ========================================================

Reads resolve through the project's ``CURRENT`` pointer (see
:mod:`odoo_kg.generations`). A build in progress is writing a different file, so
a reader is never looking at something that is being rewritten underneath it.

A reading command must leave the database byte-identical. That is stronger than
"does not change the data": ``GraphStore.__init__`` runs the schema script,
writes the version row and commits, which rewrites pages even when every value
is already correct. A command that only prints numbers must not do that, so
reading commands never construct a ``GraphStore``.

Every command takes ``--config``. Nothing here reaches the network, connects to
PostgreSQL, or writes outside the configured data directory.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import LAYER_SUMMARY, MILESTONE, __version__, generations
from .config import allowed_data_root, load_config, require_supported_runtime
from .errors import OdooKgError
from .freshness import FreshnessChecker
from .indexer import run_index
from .reader import open_reader
from .verify import run_checks, summarize

#: How each command may reach the database. See the module docstring.
#:
#: ``writing``  may construct a ``GraphStore`` and modify the graph.
#: ``reading``  must leave the file byte-identical; opens read-only or not at all.
#: ``server``   long-running, serves the graph over the read-only connection.
#:
#: ``index`` is the only writing command. Any maintenance command added later
#: that needs write access has to be listed here as ``writing`` deliberately,
#: which is the point: the classification is a decision, not an accident of
#: which import somebody reached for.
COMMAND_KINDS = {
    "doctor": "reading",
    "index": "writing",
    "stats": "reading",
    "status": "reading",
    "published": "reading",
    "generations": "reading",
    # Administrative: they change which generation is published, or release a
    # lock. Neither opens a database for writing, and neither is a "reading"
    # command -- a reader must leave everything byte-identical, and these two
    # deliberately change one small file each.
    "publish": "administrative",
    "unlock": "administrative",
    "verify": "reading",
    "serve": "server",
}

_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"
)

#: What a command uses when no --config is given: ODOO_KG_CONFIG when set, else the sample project. A real
#: project's config lives in config/local/ (git-ignored) and is named with --config; `odoo-ai kg` always does.
DEFAULT_CONFIG = os.environ.get("ODOO_KG_CONFIG") or os.path.join(_CONFIG_DIR, "sample-project.toml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odoo-kg",
        description=f"Odoo knowledge-graph indexer ({MILESTONE}, v{__version__})",
    )
    parser.add_argument("--version", action="version", version=f"odoo-kg {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("doctor", "validate configuration and environment without writing"),
        ("index", "build a new generation and publish it"),
        ("stats", "report what the published graph contains"),
        ("status", "report whether the published graph is fresh or stale"),
        ("published", "show which generation is currently published"),
        ("generations", "list the generations on disk; deletes nothing"),
        ("publish", "publish an already-built generation, without indexing"),
        ("unlock", "inspect or release an abandoned build lock"),
        ("verify", "check the acceptance criteria against the published graph"),
        ("serve", "run the read-only MCP server over stdio"),
    ):
        child = sub.add_parser(name, help=help_text)
        child.add_argument(
            "--config", default=DEFAULT_CONFIG, help="path to the project TOML config"
        )
        if name == "publish":
            child.add_argument(
                "--generation", required=True,
                help="exact generation id to publish, e.g. gen-<run id>",
            )
        if name == "unlock":
            child.add_argument(
                "--show", action="store_true",
                help="report the lock and exit without changing anything",
            )
            child.add_argument(
                "--lock-id", default="",
                help="the lock's exact id, required to release it",
            )
            child.add_argument(
                "--force", action="store_true",
                help="release a lock whose holder cannot be verified from here",
            )
        if name == "index":
            child.add_argument(
                "--rebuild",
                action="store_true",
                help="drop and recreate the schema before indexing",
            )
            child.add_argument(
                "--quiet", action="store_true", help="print the summary only"
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    require_supported_runtime()
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except OdooKgError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        handler = {
            "doctor": _doctor,
            "index": _index,
            "stats": _stats,
            "status": _status,
            "published": _published,
            "generations": _generations,
            "publish": _publish,
            "unlock": _unlock,
            "verify": _verify,
            "serve": _serve,
        }[args.command]
        return handler(config, args)
    except OdooKgError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _doctor(config, args) -> int:
    """Report configuration and environment. Never opens the database.

    Reviewed as part of the read-only work and left as it was, because it was
    already the safest of the three: the graph is inspected with
    :func:`os.path.isfile` and nothing else. It does not open a connection, run
    a writability probe, or create a temporary file anywhere -- not beside the
    graph and not in the data directory, which it also does not create.
    ``ensure_data_dir`` is called by the indexer alone.

    If a future check wants to prove the data directory is writable, it must
    say so in its output and do it in a uniquely named temporary file under
    ``data_dir``, never in or beside ``graph.db``.
    """
    print(f"config          {config.source_file.display if config.source_file else '<inline>'}")
    print(f"project_id      {config.project_id}")
    print(f"repo_root       {config.repo_root.display}  exists={os.path.isdir(config.repo_root.display)}")
    if config.core_root:
        print(
            f"core_root       {config.core_root.display}  "
            f"exists={os.path.isdir(config.core_root.display)}  (recorded, not indexed in M1)"
        )
    for root in config.include_roots:
        print(f"include_root    {root.display}")
    print(f"data_dir        {config.data_dir.display}")
    print(f"db_path         {config.db_path.display}  exists={os.path.isfile(config.db_path.display)}")
    print(f"allowed root    {allowed_data_root().display}")
    print(f"expected series {config.expected_series}")
    print(f"acceptance      {config.acceptance_profile}")
    print(f"origins set     {len(config.module_origins)} explicit, {len(config.origin_rules)} rule(s)")
    print(f"python          {sys.version.split()[0]}  os.name={os.name}")
    for warning in config.warnings:
        print(f"warning         {warning}")
    return 0


def _index(config, args) -> int:
    summary = run_index(config, rebuild=args.rebuild)
    if not args.quiet:
        for severity, category, message, path in summary.diagnostics[:50]:
            print(f"[{severity}] {category}: {message} ({path})")
        if len(summary.diagnostics) > 50:
            print(f"... {len(summary.diagnostics) - 50} more diagnostics in the database")
    print(f"run_id          {summary.run_id}")
    print(f"generation      {summary.generation}")
    print(f"database        {summary.db_path}")
    if summary.published:
        print("published       yes; readers pick it up on their next open")
    elif summary.publish_error:
        print(f"published       NO -- {summary.publish_error}")
        print("                the previously published generation is unchanged")
    else:
        print("published       no (publish was not requested)")
    print(f"branch          {summary.branch}  commit={summary.base_commit}  dirty={summary.dirty}")
    print(f"files indexed   {summary.files_indexed}  excluded_files={summary.files_excluded}  excluded_dirs={summary.dirs_excluded}")
    print(f"modules         {summary.modules}  on {config.expected_series}.*={summary.modules_series_ok}")
    print(
        f"depends_on      {summary.depends_edges} "
        f"(resolved={summary.depends_resolved}, unresolved={summary.depends_unresolved})"
    )
    print(f"pruned          {summary.pruned}")
    counts = summary.python_counts
    print(
        f"python files    {summary.python_files_parsed} parsed, "
        f"{summary.python_files_failed} unreadable/unparsable"
    )
    print(
        f"python nodes    models={counts.get('models', 0)} "
        f"defs={counts.get('model_defs', 0)} fields={counts.get('fields', 0)} "
        f"methods={counts.get('methods', 0)}"
    )
    for table, written in sorted(summary.python_edges.items()):
        print(f"  {table:<26} {written}")
    print(
        f"related pass 2  derived={counts.get('related_derived', 0)} "
        f"unresolved={counts.get('related_unresolved', 0)}"
    )
    xml = summary.xml_counts
    print(
        f"data files      {xml.get('data_files', 0)} declared "
        f"(xml={xml.get('declared_xml', 0)} csv={xml.get('declared_csv', 0)} "
        f"other={xml.get('declared_other', 0)})"
    )
    print(
        f"xml parsed      {xml.get('xml_parsed', 0)} ok, "
        f"{xml.get('xml_syntax_error', 0)} malformed, "
        f"{xml.get('xml_dtd_rejected', 0)} dtd-rejected, "
        f"{xml.get('xml_no_elements', 0)} empty, "
        f"{xml.get('files_missing', 0)} missing"
    )
    print(
        f"xml nodes       xmlids={xml.get('xmlids', 0)} "
        f"records={xml.get('records', 0)} "
        f"multiply_defined={xml.get('xmlids_multiply_defined', 0)}"
    )
    for table, written in sorted(summary.xml_edges.items()):
        print(f"  {table:<26} {written}")
    print(
        f"xml references  resolved={xml.get('ref_resolved', 0)} "
        f"synthetic={xml.get('ref_synthetic', 0)} "
        f"external={xml.get('ref_external', 0)} "
        f"unresolved={xml.get('ref_unresolved', 0)}"
    )
    sec = summary.security_counts
    print(
        f"security        groups={sec.get('groups', 0)} privileges={sec.get('privileges', 0)} "
        f"acls={sec.get('acls', 0)} "
        f"rules={sec.get('rules', 0)} "
        f"(acl files={sec.get('acl_files', 0)}, global acls={sec.get('acl_global', 0)}, "
        f"global rules={sec.get('rules_global', 0)})"
    )
    for table, written in sorted(summary.security_edges.items()):
        print(f"  {table:<26} {written}")
    print(
        f"security bind   model resolved={sec.get('model_resolved', 0)} "
        f"external={sec.get('model_external', 0)} "
        f"unresolved={sec.get('model_unresolved', 0)} | "
        f"group resolved={sec.get('group_resolved', 0)} "
        f"external={sec.get('group_external', 0)} "
        f"unresolved={sec.get('group_unresolved', 0)}"
    )
    front = summary.frontend_counts
    print(
        f"frontend        bundles={front.get('bundles', 0)} "
        f"entries={front.get('asset_entries', 0)} js={front.get('js_files', 0)} "
        f"components={front.get('components', 0)} "
        f"templates={front.get('templates', 0)} routes={front.get('routes', 0)}"
    )
    for table, written in sorted(summary.frontend_edges.items()):
        print(f"  {table:<26} {written}")
    print(
        f"frontend links  js->route={front.get('js_route_links', 0)} "
        f"js->model={front.get('js_model_links', 0)} "
        f"imports resolved={front.get('js_imports_resolved', 0)} | "
        f"assets matched={front.get('asset_target_matched', 0)} "
        f"unmatched={front.get('asset_target_unmatched', 0)}"
    )
    flow = summary.workflow_counts
    print(
        f"workflow        workflows={flow.get('workflows', 0)} "
        f"states={flow.get('states', 0)} "
        f"transitions={flow.get('transitions', 0)} "
        f"triggers={flow.get('triggers', 0)} "
        f"crons={flow.get('scheduled_actions', 0)}"
    )
    for table, written in sorted(summary.workflow_edges.items()):
        print(f"  {table:<26} {written}")
    print(
        f"workflow evidence  states declared={flow.get('states_declared', 0)} "
        f"seen-only-in-code={flow.get('states_only_seen_in_code', 0)} | "
        f"transitions static={flow.get('transitions_static', 0)} "
        f"dynamic-target={flow.get('transitions_dynamic_target', 0)} "
        f"cross-model={flow.get('transitions_cross_model', 0)}"
    )
    print(
        f"workflow triggers  buttons={flow.get('xml_buttons', 0)} "
        f"bound={flow.get('button_bound_to_one_method', 0)} "
        f"ambiguous={flow.get('button_method_ambiguous', 0)} "
        f"outside-project={flow.get('button_method_not_in_project', 0)} | "
        f"cron->method={flow.get('cron_calls_method', 0)} "
        f"route->method={flow.get('route_method_calls', 0)}"
    )
    doc = summary.doc_counts
    print(
        f"documentation   documents={doc.get('documents', 0)} "
        f"sections={doc.get('doc_sections', 0)} "
        f"flagged={doc.get('documents_with_injection_like_text', 0)}"
    )
    print(
        f"tests           files={doc.get('test_files', 0)} "
        f"(py={doc.get('test_files_python', 0)} "
        f"js={doc.get('test_files_javascript', 0)}) "
        f"classes={doc.get('test_classes', 0)} cases={doc.get('test_cases', 0)} "
        f"migrations={doc.get('migrations', 0)}"
    )
    print(
        f"subject edges   explicit="
        f"{doc.get('section_documents_model', 0) + doc.get('section_documents_route', 0) + doc.get('section_documents_xmlid', 0) + doc.get('section_documents_workflow', 0) + doc.get('section_documents_method_qualified', 0)} "
        f"heuristic={doc.get('section_documents_method_by_bare_name', 0)} "
        f"ambiguous={doc.get('section_documents_method_ambiguous', 0)} "
        f"unresolved={doc.get('section_documents_method_unresolved', 0)} "
        f"(dropped as prose={doc.get('bare_name_not_written_as_code', 0)})"
    )
    print(
        f"guidance        processes={doc.get('processes', 0)} "
        f"(documented={doc.get('processes_documented', 0)} "
        f"incomplete={doc.get('processes_incomplete', 0)}) "
        f"steps={doc.get('process_steps', 0)} "
        f"conflicts={doc.get('process_conflicts', 0)} "
        f"conventions={doc.get('conventions', 0)}"
    )
    for table, written in sorted(summary.documentation_edges.items()):
        print(f"  {table:<34} {written}")
    for table, written in sorted(summary.test_edges.items()):
        print(f"  {table:<34} {written}")
    for table, written in sorted(summary.process_edges.items()):
        print(f"  {table:<34} {written}")
    print(
        "node diagnostics "
        f"{summary.python_diagnostics + summary.xml_diagnostics + summary.security_diagnostics + summary.frontend_diagnostics + summary.workflow_diagnostics + summary.doc_diagnostics}"
    )
    print(f"pruned (python) {summary.pruned_python}")
    print(f"pruned (xml)    {summary.pruned_xml}")
    print(f"pruned (sec)    {summary.pruned_security}")
    print(f"pruned (front)  {summary.pruned_frontend}")
    print(f"pruned (flow)   {summary.pruned_workflow}")
    print(f"pruned (v7)     {summary.pruned_v7}")
    for domain in ("modules", "python", "xml", "security", "frontend",
                   "workflow", "documentation", "tests", "process", "total"):
        print(f"digest {domain:<9} {summary.digests.get(domain, '-')}")
    print(f"duration        {summary.duration_seconds:.2f}s")
    _print_layer_scope()
    # A build that did not publish is not a successful build. Returning 0 would
    # tell a script -- or a person reading only the exit code -- that the graph
    # was refreshed when the published one is still the old one.
    return 0 if summary.published else 1


def _print_layer_scope() -> None:
    """State what this milestone did and did not look at.

    Printed after every index and every stats call because the alternative is a
    reader who assumes a missing edge means a missing relationship.
    """
    print(f"scope           {MILESTONE}")
    for line in LAYER_SUMMARY:
        print(f"  - {line}")


def _stats(config, args) -> int:
    """Report what the graph already holds. Reads stored values, repairs nothing.

    Opened through :func:`~odoo_kg.reader.open_reader`, so there is no schema
    script, no version row and no commit -- the file comes out byte-identical.
    """
    with open_reader(config) as store:
        project = store.project_row(config.project_id)
        if project is None:
            print(f"error: no project row for {config.project_id}", file=sys.stderr)
            return 2
        print(f"project         {project['project_id']}  milestone={project['milestone']}")
        print(f"repo_root       {project['repo_root']}")
        print(f"branch          {project['branch']}  commit={project['base_commit']}  dirty={project['dirty']}")
        print(f"indexed_at      {project['indexed_at']}  schema={project['schema_version']}")
        print(f"files           {store.count('node_file')}")
        print(f"modules         {store.count('node_module')}")
        print(f"depends_on      {store.count('edge_depends_on')}")
        resolved = store.connection.execute(
            "SELECT binding, COUNT(*) AS n FROM edge_depends_on GROUP BY binding ORDER BY binding"
        ).fetchall()
        for row in resolved:
            print(f"  {row['binding']:<12} {row['n']}")
        origins = store.connection.execute(
            "SELECT origin, COUNT(*) AS n FROM node_module GROUP BY origin ORDER BY origin"
        ).fetchall()
        for row in origins:
            print(f"  origin {row['origin']:<15} {row['n']}")
        print(f"diagnostics     {store.count('diagnostic')}")
        print(f"graph digest    {store.graph_digest()}")
    return 0


def _published(config, args) -> int:
    """Which generation is published, and does it still match its manifest.

    Reads the pointer and re-checks the file it names: size, sha256, SQLite
    integrity, project, schema and run. Everything is a read; nothing here can
    change what is published.
    """
    try:
        resolution = generations.resolve(config)
    except OdooKgError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"project         {config.project_id}")
    print(f"data_dir        {config.data_dir.display}")
    print(f"source          {resolution.kind}")
    for note in resolution.notes:
        print(f"note            {note}")
    if resolution.pointer is None:
        print(f"database        {resolution.db_path}")
        print("published       nothing; this is a pre-generation database")
        return 0

    pointer = resolution.pointer
    print(f"generation      {pointer.generation}")
    print(f"database        {resolution.db_path}")
    print(f"published_at    {pointer.published_at}")
    print(f"run_id          {pointer.run_id}")
    print(f"branch          {pointer.branch}  commit={pointer.base_commit}  "
          f"dirty={pointer.dirty}")
    print(f"schema          {pointer.schema_version}  "
          f"indexer={pointer.indexer_version}")
    print(f"size            {pointer.db_size}")
    print(f"sha256          {pointer.db_sha256}")

    _pointer, problems = generations.verify_published(config)
    if problems:
        print("integrity       FAILED")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("integrity       ok (sha256, size, SQLite integrity_check, project, "
          "schema, run)")
    return 0


def _generations(config, args) -> int:
    """List the generations on disk. Deletes nothing, ever.

    Removing a generation is not automated here on purpose: on Windows a file
    another process has open cannot be deleted, and a tool that deletes graphs
    a server might be reading is a tool that eventually deletes the wrong one.
    """
    rows = generations.list_generations(config)
    print(f"project         {config.project_id}")
    print(f"generations dir {generations.generations_dir(config)}")
    if not rows:
        print("no generations on disk")
        legacy = config.db_path.display
        if os.path.isfile(legacy):
            print(f"pre-generation database present: {legacy}")
        return 0
    for row in rows:
        mark = "*" if row["published"] else " "
        size = row["size"] if row["size"] is not None else "-"
        print(f" {mark} {row['generation']}  size={size}  "
              f"present={row['present']}"
              + (f"  sidecars={row['sidecars']}" if row["sidecars"] else ""))
    print(f"\n{len(rows)} generation(s); * marks the published one.")
    print("Nothing is deleted by this command. To reclaim space, stop every "
          "MCP server for this project, confirm which generation is published "
          "with `published`, and remove the other directories by hand.")
    return 0


def _publish(config, args) -> int:
    """Publish a generation that already exists, without indexing again.

    The recovery command for the one failure the publishing model can still
    have: the generation was built and checked, and only the pointer swap was
    blocked. This repeats that last step, under the same lock, with the same
    validation, and without opening the generation's database for writing.
    """
    generation = (args.generation or "").strip()
    before = None
    try:
        before = generations.read_pointer(config).generation
    except OdooKgError:
        before = None

    try:
        pointer = generations.publish_existing(config, generation)
    except OdooKgError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if before is not None:
            print(f"still published: {before}", file=sys.stderr)
        return 2

    print(f"published       {pointer.generation}")
    print(f"was             {before or 'nothing'}")
    print(f"database        {generations.generation_db(config, pointer.generation)}")
    print(f"run_id          {pointer.run_id}  commit={pointer.base_commit}")
    print(f"sha256          {pointer.db_sha256}")
    print("Readers pick it up on their next open. No database was modified.")
    return 0


def _unlock(config, args) -> int:
    """Inspect, and only on an exact match release, an abandoned build lock.

    A crashed build must not block a team permanently, and releasing a lock
    must not become a reflex. So this reports first, requires the lock's own id
    second, and refuses outright while the holder is demonstrably alive.
    """
    info = generations.read_lock(config)
    print(f"project         {config.project_id}")
    print(f"lock file       {info.path}")
    if not info.present:
        print("state           no lock; no build is holding this project")
        return 0
    if not info.readable:
        print("state           PRESENT BUT UNREADABLE")
        print("                A lock that cannot be parsed is not treated as "
              "valid, and not as absent either.")
        print(f"contents        {info.raw[:200]!r}")
    else:
        age = info.age_seconds
        print(f"lock_id         {info.lock_id}")
        print(f"pid             {info.pid}")
        print(f"host            {info.host}"
              + ("  (this machine)" if info.is_local else "  (another machine)"))
        print(f"started         {info.started_display}")
        print(f"age             {age / 60:.1f} minutes" if age is not None
              else "age             unknown")
        print(f"indexer         {info.indexer_version}")
        print(f"holder          {info.holder_state}")
        print(f"looks abandoned {info.looks_abandoned}")

    if args.show:
        return 0
    if not args.lock_id:
        print("\nNothing released: pass --lock-id with the id printed above. "
              "Requiring it means a lock can only be released by somebody who "
              "looked at it first.", file=sys.stderr)
        return 2

    try:
        released = generations.release_lock(
            config, lock_id=args.lock_id, force=args.force
        )
    except OdooKgError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 2
    print(f"\nreleased        {released.lock_id or 'unreadable lock'}")
    print("No database, generation or pointer was touched.")
    return 0


def _status(config, args) -> int:
    """Fresh or stale: does the published graph still describe the working tree.

    The same verdict the ``kg_status`` MCP tool gives, on the command line, so
    a developer can answer the question without starting a server.
    """
    try:
        with open_reader(config) as store:
            checker = FreshnessChecker(config.repo_root.display, config.exclusions)
            verdict = checker.check(store.connection, config.project_id)
            resolution = store.resolution
    except OdooKgError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"project         {config.project_id}")
    if resolution is not None:
        print(f"source          {resolution.kind}")
        if resolution.pointer is not None:
            print(f"generation      {resolution.pointer.generation}")
    print(f"state           {verdict.state}")
    print(f"detail          {verdict.detail}")
    print(f"graph commit    {verdict.graph_commit}  branch={verdict.graph_branch}")
    print(f"working tree    {verdict.current_commit}  branch={verdict.current_branch}")
    print(f"indexed_at      {verdict.graph_indexed_at}")
    if verdict.drifted_count:
        print(f"drifted files   {verdict.drifted_count}")
        for path in verdict.drifted_sample:
            print(f"  - {path}")
    if verdict.state != "fresh":
        print("\nStale means the graph describes an older tree than the one on "
              "disk. Answers about the changed files may be wrong. Fix it with "
              "`python -m odoo_kg index`, which builds a new generation and "
              "publishes it; readers keep serving the current one until it "
              "does.")
    return 0 if verdict.state == "fresh" else 1


def _verify(config, args) -> int:
    """Evaluate the acceptance criteria against the graph, read-only.

    A checker that could modify what it is checking is not a checker. The
    checks take a reader interface and never see a writable connection.
    """
    with open_reader(config) as store:
        checks = run_checks(config, store)
    for check in checks:
        print(f"[{check.symbol}] {check.name}: {check.detail}")
    passed, total = summarize(checks)
    print(f"\n{passed}/{total} acceptance checks passed")
    return 0 if passed == total else 1


def _serve(config, args) -> int:
    # Imported here so that `index`/`verify` do not pay for the MCP module, and
    # so an error inside it cannot break the M1 commands.
    from .mcp.server import serve

    return serve(config)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
