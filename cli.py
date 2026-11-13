"""
Unified Odoo AI CLI Tool (odoo-ai)
Main Command Line Interface for testing, linting, auditing, and diagnosing Odoo 17/18/19/20 modules.
"""

import argparse
import os
import re
import sys

from test_runner import run_odoo_test
from linter import run_linter
from ai_diagnoser import generate_diagnostic_prompt
from workspace_sync import run_workspace_sync, resolve_project, KG_DIR, TOOLS_DIR, TOOLS_VERSION, fs
from ci_local import run_ci
from doctor import run_doctor
from move_projects import run_move_projects
from setup_version import run_setup
from new_project import run_new_project
from gates_cmd import run_gates
from port_cmd import run_port
from db_fast import run_fast_clean_test
from scaffold_plus import create_scaffold_module

VERSION = "1.3.0"
SUPPORTED_VERSIONS = ["17", "18", "19", "20"]
#: commands whose module has its own main(): name -> (module, function)
#: commands that take their own options (`--staged`, `--install`...): argparse would refuse a leading `-`
PASSTHROUGH = ("secrets", "sync-versions", "claude", "xpath-check", "db-snap", "i18n", "init-fast-db", "tools-ci", "server")
DELEGATED = {"xpath-check": ("xpath_lint", "main"), "db-snap": ("db_snap", "main"), "i18n": ("i18n_sync", "main"),
             "server": ("server_ctl", "main")}


def module_root(path):
    """The addon a path belongs to (the nearest folder with a __manifest__.py),
    or the path itself (a whole project) when it is in no addon."""
    path = os.path.abspath(path)
    probe = path if os.path.isdir(path) else os.path.dirname(path)
    while True:
        if os.path.isfile(os.path.join(probe, "__manifest__.py")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return path if os.path.isdir(path) else os.path.dirname(path)
        probe = parent


def detect_version(path, fallback=TOOLS_VERSION):
    """Odoo version from a path: C:\\odoo\\odoo19\\... or C:\\odoo\\ProjectsOdoo19\\..."""
    match = re.search(r"[\\/](?:odoo|ProjectsOdoo)(1[0-9]|2[0-9])(?:[\\/]|$)", os.path.abspath(path), re.IGNORECASE)
    return match.group(1) if match else fallback

def kg_python():
    """The Python 3.11+ that runs odoo-kg, as `odoo-ai workspace` recorded it."""
    import json
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local.json")
    if os.path.isfile(local):
        with open(local, encoding="utf-8") as handle:
            return json.load(handle).get("kg_python") or sys.executable
    return sys.executable


def run_kg(action, path):
    """Run odoo-kg for the project `path` belongs to; the same config the VS Code tasks and .mcp.json use."""
    import subprocess
    project = resolve_project(path)
    if not project:
        print(f"[KG] {os.path.abspath(path)} is not inside C:\\odoo\\odoo<version>\\projects\\<Project>")
        return 2
    version, name, pid, config_win = project
    if not os.path.isfile(fs(config_win)):
        print(f"[KG] no graph config for {name} yet: run `odoo-ai workspace -p <project>` first ({config_win})")
        return 2
    cmd = [kg_python(), "-m", "odoo_kg", action, "--config", config_win]
    print(f"[KG] {name} (Odoo {version}): {' '.join(cmd[1:])}")
    return subprocess.run(cmd, cwd=fs(KG_DIR)).returncode


def main():
    from recursion_guard import enter
    enter("cli " + " ".join(sys.argv[1:3]))   # a command that starts itself again stops here, naming the chain
    parser = argparse.ArgumentParser(
        description="Unified Odoo AI CLI Helper (Test, Lint, Audit & AI Diagnose)"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # 1. Test Command
    test_parser = subparsers.add_parser("test", help="Run Odoo unittests for a module")
    test_parser.add_argument("--module", "-m", required=True, help="Module technical name (e.g. hoss_dashboard)")
    test_parser.add_argument("--version", "-v", default=None, choices=SUPPORTED_VERSIONS,
                             help="Odoo version (default: from the current folder, else these tools' version)")
    test_parser.add_argument("--db", "-d", default="test_db", help="Database name (default: test_db)")
    test_parser.add_argument("--conf", "-c", default=None, help="Path to .conf file")
    test_parser.add_argument("--tags", "-t", default=None, help="Filter tests by tag or method (e.g. /module:TestClass.test_func)")
    test_parser.add_argument("--fast", action="store_true", help="Run on clean cloned template DB in seconds (pure & isolated)")
    test_parser.add_argument("--diagnose", action="store_true", help="Generate AI diagnostic prompt if test fails")

    # 2. Lint Command
    lint_parser = subparsers.add_parser("lint", help="Lint an Odoo module using pylint-odoo and flake8")
    lint_parser.add_argument("--path", "-p", default=".", help="Addon or project directory (default: current folder)")
    lint_parser.add_argument("--version", "-v", default=None, choices=SUPPORTED_VERSIONS,
                             help="Odoo version (default: from the path, else these tools' version)")

    # 3. Audit Command
    audit_parser = subparsers.add_parser("audit", help="Audit Odoo security CSV & manifest files")
    audit_parser.add_argument("--path", "-p", default=".", help="Path to addon directory (default: current folder)")

    # 4. Info Command
    subparsers.add_parser("info", help="Display Odoo workspace status")

    # 5. Knowledge-graph Command (the project is found from the folder, like the version)
    kg_parser = subparsers.add_parser(
        "kg", help="Knowledge graph of the current project: index | status | doctor | stats | verify | published")
    kg_parser.add_argument("action", choices=["index", "status", "doctor", "stats", "verify", "published", "generations"])
    kg_parser.add_argument("--path", "-p", default=".", help="Any folder or file inside the project (default: here)")

    # 6. Workspace Command
    ws_parser = subparsers.add_parser(
        "workspace", help="Wire projects into the knowledge graph (odoo-kg config, VS Code tasks, .mcp.json, CLAUDE.md)")
    ws_parser.add_argument("--path", "-p", default=None,
                           help="One project folder (default: every project of this Odoo version)")
    ws_parser.add_argument("--kg-python", default=None, help="Python 3.11+ for odoo-kg (default: auto-detect)")
    ws_parser.add_argument("--dry-run", action="store_true", help="Show what would change, write nothing")

    # 7. CI Command
    ci_parser = subparsers.add_parser(
        "ci", help="Run the project's CI here before pushing: the static checks of ci.yml, then install and test")
    ci_parser.add_argument("--path", "-p", default=".", help="Any folder inside the project (default: here)")
    ci_parser.add_argument("--static-only", action="store_true", help="Only the static checks")
    ci_parser.add_argument("--stop-servers", action="store_true",
                           help="Stop running Odoo servers first (their crons would write to the test database)")
    ci_parser.add_argument("--keep-db", action="store_true", help="Keep the test database afterwards")
    ci_parser.add_argument("--at", metavar="\"YYYY-MM-DD HH:MM\"",
                           help="Run the tests as if it were this UTC moment, e.g. just after midnight in Stockholm: \"2026-11-07 23:30\"")

    # 8. Doctor Command
    doctor_parser = subparsers.add_parser(
        "doctor", help="Check this Odoo version's setup: tools, lint, launcher, PATH, extension, project wiring")
    doctor_parser.add_argument("--fix", action="store_true",
                               help="Repair what is safe (launcher, PATH, pylint .pth); never deletes")

    # 10. Setup Command (a new machine, or checking one)
    setup_parser = subparsers.add_parser(
        "setup", help="Make this Odoo version ready: sources, venv, packages, conf, DB role, launcher, extension")
    setup_parser.add_argument("--check", action="store_true", help="Only report what is missing")
    setup_parser.add_argument("--db-role", action="store_true",
                              help="Also create/update the PostgreSQL role (asks for the postgres password)")

    # 11. New-project Command
    new_parser = subparsers.add_parser(
        "new-project", help="Clone a project into odoo<NN>/projects and wire it (conf, workspace, graph)")
    new_parser.add_argument("name", help="Folder name, e.g. Acme")
    new_parser.add_argument("--git", help="Repository to clone (omit when the folder is already there)")
    new_parser.add_argument("--branch", help="Branch to check out")
    new_parser.add_argument("--no-graph", action="store_true", help="Do not build the knowledge graph now")

    # 12. Gates Command
    gates_parser = subparsers.add_parser(
        "gates", help="The version's gates in the project's .github/scripts (report; --install copies them)")
    gates_parser.add_argument("--path", "-p", default=".", help="Any folder inside the project (default: here)")
    gates_parser.add_argument("--install", action="store_true", help="Copy the missing or older gates into the project")

    # 13. Port Command
    port_parser = subparsers.add_parser(
        "port", help="Carry a tools commit to other versions' tools (a trial unless --apply)")
    port_parser.add_argument("commit", help="The commit in these tools, e.g. HEAD or a hash")
    port_parser.add_argument("--to", help="Versions, e.g. 18,20 (default: every other installed version)")
    port_parser.add_argument("--apply", action="store_true", help="Cherry-pick and commit")

    # 9. Move-projects Command (once per machine, 2026-11-08 layout)
    move_parser = subparsers.add_parser(
        "move-projects", help="Move C:/odoo/ProjectsOdoo<NN>/* into C:/odoo/odoo<NN>/projects/ (dry run unless --apply)")
    move_parser.add_argument("--apply", action="store_true", help="Do it (close VS Code and stop Odoo first)")

    # 14. New-Addon Command (Scaffold Plus)
    addon_parser = subparsers.add_parser(
        "new-addon", help="Generate standard-compliant Odoo module with compliant views, models, and ACLs")
    addon_parser.add_argument("name", help="Technical name of the addon (e.g. my_custom_feature)")
    addon_parser.add_argument("--path", "-p", default=".", help="Project path to create the addon in (default: here)")
    addon_parser.add_argument("--summary", "-s", default="Custom business addon", help="Short summary")

    # 15. Self-test command for Tools Suite
    subparsers.add_parser("test-self", help="Run automated test suite for Cockpit Odoo tools")

    # 16. Secret scanner / pre-commit hook, and building the version branches from main
    for name, text in (("secrets", "Scan for passwords/tokens (--staged, FILE..., --install-hook)"),
                       ("sync-versions", "Build the 17/18/19/20 tools branches from main (report|build|snapshot|apply)"),
                       ("claude", "Give a project's Claude Code the workspace rules: hooks, skill, permissions (--install)"),
                       ("xpath-check", "Statically validate XML views and XPath expressions"),
                       ("db-snap", "Instant PostgreSQL snapshot and rollback (save|restore|list|drop)"),
                       ("i18n", "Translation sync (scan|update|export -m <module> [-l <lang>])"),
                       ("init-fast-db", "Build the clean template database for `test --fast` [-v <NN>]"),
                       ("tools-ci", "Run every version's tools tests under the CI's conditions (--install-hook: pre-push)"),
                       ("server", "A project's server: dbs | inventory | modules -d <db> | command | upgrade -d <db> [-m all|a,b] | drift -d <db> | backup -d <db> --dir <folder> | drop -d <db> --yes | health --log <file>")):
        extra = subparsers.add_parser(name, help=text, add_help=False)
        extra.add_argument("rest", nargs=argparse.REMAINDER)

    if len(sys.argv) > 1 and sys.argv[1] in PASSTHROUGH:
        args = argparse.Namespace(command=sys.argv[1], rest=sys.argv[2:])
    else:
        args = parser.parse_args()

    if getattr(args, "path", None) and args.command in ("lint", "audit"):
        # A file or a folder inside an addon: that addon. Absolute paths, so an
        # editor can open each finding.
        args.path = module_root(args.path)
    if getattr(args, "version", "x") is None:
        args.version = detect_version(getattr(args, "path", None) or os.getcwd())

    if args.command == "test":
        tags = getattr(args, "tags", None)
        if getattr(args, "fast", False):
            print(f"[CLI] Running FAST CLEAN TEST on ephemeral clone...")
            res = run_fast_clean_test(args.module, version=args.version, config_file=args.conf, tags=tags)
        else:
            res = run_odoo_test(args.module, args.version, args.db, args.conf, tags=tags)
        dur = res.get("duration", 0)
        if res["success"]:
            print(f"\n[SUCCESS] All Odoo Tests Passed Successfully in {dur}s!")
        else:
            print(f"\n[FAIL] Tests Failed or Encountered Errors after {dur}s!")
            print(res.get("failures", res.get("error")))
            if args.diagnose:
                print("\n[AI DIAGNOSER] Generating Knowledge Graph Enriched AI Diagnostic Prompt...")
                prompt = generate_diagnostic_prompt(args.module, res.get("output", ""), version=args.version)
                print("\n" + prompt)

    elif args.command == "lint":
        res = run_linter(args.path, args.version)
        if res["xml_errors"]:
            print("\nXML Errors Found:")
            for err in res["xml_errors"]:
                print(err)
        else:
            print("\n[SUCCESS] XML Syntax Clean.")

        print("\n--- PYLINT ODOO REPORT ---")
        print(res["pylint"] or "No Pylint warnings.")

        print("\n--- FLAKE8 REPORT ---")
        print(res["flake8"] or "No Flake8 warnings.")

    elif args.command == "audit":
        from audit_module import main as audit_main
        sys.exit(audit_main(os.path.abspath(args.path), TOOLS_VERSION))

    elif args.command == "new-addon":
        target = os.path.abspath(args.path)
        sys.exit(0 if create_scaffold_module(args.name, target, version=args.version, summary=args.summary) else 1)

    elif args.command == "kg":
        sys.exit(run_kg(args.action, args.path))

    elif args.command == "ci":
        sys.exit(run_ci(args.path, args.static_only, args.stop_servers, args.keep_db, args.at))

    elif args.command == "port":
        sys.exit(run_port(args.commit, args.to, args.apply))

    elif args.command == "gates":
        sys.exit(run_gates(args.path, args.install))

    elif args.command == "setup":
        sys.exit(run_setup(args.check, args.db_role))

    elif args.command == "new-project":
        sys.exit(run_new_project(args.name, args.git, args.branch, not args.no_graph))

    elif args.command == "move-projects":
        sys.exit(run_move_projects(args.apply))

    elif args.command == "doctor":
        sys.exit(run_doctor(args.fix))

    elif args.command == "secrets":
        from secret_scan import main as secrets_main
        sys.exit(secrets_main(args.rest))

    elif args.command in DELEGATED:   # the module's own main() parses the rest of the line
        import importlib
        sys.argv = [args.command] + args.rest
        sys.exit(getattr(importlib.import_module(DELEGATED[args.command][0]), DELEGATED[args.command][1])() or 0)

    elif args.command == "init-fast-db":
        from db_fast import create_template_db
        fast = argparse.ArgumentParser(prog="odoo-ai init-fast-db",
                                       description="Build the clean template database that `odoo-ai test --fast` clones "
                                                   "(runs Odoo once: `-i base --stop-after-init`). Takes about a minute.")
        fast.add_argument("-v", "--version", choices=SUPPORTED_VERSIONS, default=None, help="Odoo version (default: from the folder)")
        chosen = fast.parse_args(args.rest)   # --help ends here: it must never start building a database
        create_template_db(version=chosen.version or detect_version(os.getcwd()) or "19")

    elif args.command == "tools-ci":
        from tools_ci import main as tools_ci_main
        sys.exit(tools_ci_main(args.rest))

    elif args.command == "claude":
        from claude_setup import main as claude_main
        sys.exit(claude_main(args.rest))

    elif args.command == "sync-versions":
        from sync_versions import main as sync_main
        sys.exit(sync_main(args.rest or ["report"]))

    elif args.command == "test-self":
        import unittest
        tests_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests")
        suite = unittest.defaultTestLoader.discover(tests_dir, pattern="test_*.py")
        runner = unittest.TextTestRunner(verbosity=2)
        res = runner.run(suite)
        sys.exit(0 if res.wasSuccessful() else 1)

    elif args.command == "workspace":
        run_workspace_sync(args.path, kg_python=args.kg_python, dry=args.dry_run)

    elif args.command == "info":
        print(f"Odoo AI CLI v{VERSION}: the Odoo {TOOLS_VERSION} tools ({TOOLS_DIR})")
        print("Workspace Root: C:\\odoo")
        print(f"Version detected here: Odoo {detect_version(os.getcwd())}")
        project = resolve_project(os.getcwd())
        if project:
            version, name, pid, config_win = project
            print(f"Project here: {name} (Odoo {version}), graph config: {config_win}")
            run_kg("status", os.getcwd())
        print("Every version has its own tools: C:/odoo/odoo<NN>/tools (the odoo-ai launcher picks them)")
        print("Registered Rules: AGENTS.md, .clinerules, .cursorrules")
        print("Graph: `odoo-ai kg status|index` in any project folder; `odoo-ai workspace` wires new projects")

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
