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

VERSION = "1.3.0"
SUPPORTED_VERSIONS = ["17", "18", "19", "20"]


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
    """Odoo version from a path: C:\\odoo\\odoo19\\... (projects included), else the fallback."""
    match = re.search(r"[\\/]odoo(1[0-9]|2[0-9])(?:[\\/]|$)", os.path.abspath(path), re.IGNORECASE)
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
    ci_parser.add_argument("--static-only", action="store_true", help="Only the static checks (seconds)")
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

    args = parser.parse_args()

    if getattr(args, "path", None) and args.command in ("lint", "audit"):
        # A file or a folder inside an addon: that addon. Absolute paths, so an
        # editor can open each finding.
        args.path = module_root(args.path)
    if getattr(args, "version", "x") is None:
        args.version = detect_version(getattr(args, "path", None) or os.getcwd())

    if args.command == "test":
        res = run_odoo_test(args.module, args.version, args.db, args.conf)
        if res["success"]:
            print("\n[SUCCESS] All Odoo Tests Passed Successfully!")
        else:
            print("\n[FAIL] Tests Failed or Encountered Errors!")
            print(res.get("failures", res.get("error")))
            if args.diagnose:
                print("\n[AI DIAGNOSER] Generating AI Diagnostic Prompt...")
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
        module_path = args.path
        sec_file = os.path.join(module_path, "security", "ir.model.access.csv")
        manifest_file = os.path.join(module_path, "__manifest__.py")
        
        print(f"[AUDIT] Auditing Odoo Module: {module_path}")
        if os.path.exists(sec_file):
            print("  [OK] Security CSV exists.")
        else:
            print("  [WARNING] 'security/ir.model.access.csv' is MISSING!")

        if os.path.exists(manifest_file):
            print("  [OK] __manifest__.py exists.")
        else:
            print("  [ERROR] '__manifest__.py' is MISSING!")

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
