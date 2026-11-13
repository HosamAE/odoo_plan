"""Example of tools/local/workspace_local.py: what `odoo-ai workspace` should know about YOUR projects.

Copy this file to tools/local/workspace_local.py (that folder is git-ignored) and keep only what you need.
You need none of it for an ordinary project: without the file every project is found by its manifests, its graph
id is made from its name, and its port is the next free one.

Every name is optional.

  PROJECT_IDS       {(version, "name"): "graph project id"}   a repository that names its own graph id
  PATTERN_PROJECTS  {version: "name"}                         the project whose .conf and workspace new projects copy
  PORTS             {(version, "name"): port}                 for projects that have no .conf yet
  ADDON_REPOS       {"name", ...}                             projects that other projects load through addons_path
  FAMILIES          [{"name", "versions", "projects", "origins", "origin_rules"}]
                    projects that share rules; `origins` says which modules are vendor/core, `origin_rules`
                    does the same by name pattern (the graph keeps them out of what you may edit)
  claude_section(version, name, family, git, project_path)   markdown added to the project's CLAUDE.md, or None
"""

PROJECT_IDS = {("19", "my_project"): "my-project-main"}

PATTERN_PROJECTS = {"19": "my_project"}

PORTS = {("19", "shared_addons"): 1911}

ADDON_REPOS = {"shared_addons"}

FAMILIES = [
    {"name": "acme", "versions": ["19"], "projects": ["my_project"],
     "origins": {"some_vendor_module": "vendor"},
     "origin_rules": [("acme_*", "project"), ("om_*", "vendor")]},
]


def claude_section(version, name, family, git, project_path):
    if family != "acme":
        return None
    return "## What this repository is\n\nAcme's addons for Odoo %s. Branch: `%s`.\n" % (version, (git or {}).get("branch", "?"))
