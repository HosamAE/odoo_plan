"""odoo-ai new-project <name> --git <url> [--branch <b>]: a project, ready to work on.

  1. clones it into C:/odoo/ProjectsOdoo<NN>/<name> (or odoo<NN>/projects/<name>; or uses the folder if it is there)
  2. odoo-ai workspace for it: <name>.conf (from the version's odoo.conf, its own
     port), the workspace, odools.toml, the graph config, .mcp.json, CLAUDE.md -
     all inside the project and kept out of its git
  3. builds its knowledge graph

After it: open <name>.code-workspace in VS Code.
"""
import os
import subprocess

from workspace_sync import TOOLS_VERSION, fs, project_dir, run_workspace_sync


def run_new_project(name, git_url=None, branch=None, graph=True):
    target = fs(project_dir(TOOLS_VERSION, name))
    print(f"[NEW] Odoo {TOOLS_VERSION} project {name}: {target}")
    if os.path.isdir(target) and os.listdir(target):
        print("  the folder is there: wiring it as it is")
    elif not git_url:
        raise SystemExit("  no such folder: pass --git <url> to clone it")
    else:
        cmd = ["git", "clone"] + (["-b", branch] if branch else []) + [git_url, target]
        print("  " + " ".join(cmd))
        if subprocess.run(cmd).returncode:
            return 1
    run_workspace_sync(path=project_dir(TOOLS_VERSION, name))
    if graph:
        from cli import run_kg
        print("\n[NEW] building its knowledge graph")
        run_kg("index", target)
    print(f"\n[NEW] ready: open {os.path.join(target, name + '.code-workspace')}")
    return 0
