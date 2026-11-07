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
from workspace_sync import run_workspace_sync, resolve_project, KG_DIR, fs
from ci_local import run_ci

VERSION = "1.2.0"
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


def detect_version(path, fallback="17"):
    """Odoo version from a path: C:\\odoo\\odoo19\\..., ...\\ProjectsOdoo18\\..., else the fallback."""
    match = re.search(r"(?:ProjectsOdoo|[\\/]odoo)(1[0-9]|2[0-9])(?:[\\/]|$)", os.path.abspath(path), re.IGNORECASE)
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
        print(f"[KG] {os.path.abspath(path)} is not inside C:\\odoo\\ProjectsOdoo<version>\\<Project>")
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
                             help="Odoo version (default: from the current folder, else 17)")
    test_parser.add_argument("--db", "-d", default="test_db", help="Database name (default: test_db)")
    test_parser.add_argument("--conf", "-c", default=None, help="Path to .conf file")
    test_parser.add_argument("--diagnose", action="store_true", help="Generate AI diagnostic prompt if test fails")

    # 2. Lint Command
    lint_parser = subparsers.add_parser("lint", help="Lint an Odoo module using pylint-odoo and flake8")
    lint_parser.add_argument("--path", "-p", default=".", help="Addon or project directory (default: current folder)")
    lint_parser.add_argument("--version", "-v", default=None, choices=SUPPORTED_VERSIONS,
                             help="Odoo version (default: from the path, else 17)")

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
                           help="One project folder (default: every project in ProjectsOdoo17/18/19/20)")
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
        sys.exit(run_ci(args.path, args.static_only, args.stop_servers, args.keep_db))

    elif args.command == "workspace":
        run_workspace_sync(args.path, kg_python=args.kg_python, dry=args.dry_run)

    elif args.command == "info":
        print(f"Odoo AI CLI v{VERSION}")
        print("Workspace Root: C:\\odoo")
        print(f"Version detected here: Odoo {detect_version(os.getcwd())}")
        project = resolve_project(os.getcwd())
        if project:
            version, name, pid, config_win = project
            print(f"Project here: {name} (Odoo {version}), graph config: {config_win}")
            run_kg("status", os.getcwd())
        print("Supported Versions: " + ", ".join("Odoo " + v for v in SUPPORTED_VERSIONS))
        print("Registered Rules: AGENTS.md, .clinerules, .cursorrules")
        print("Graph: `odoo-ai kg status|index` in any project folder; `odoo-ai workspace` wires new projects")

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
