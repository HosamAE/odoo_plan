# What every Odoo 17 project gets

A project is a folder in `C:\odoo\odoo17\projects\<PROJECT>` with its own git repository. Next to its addons it carries a handful of files that belong to this machine and this developer, never to the repository. This folder is where they come from.

| File in the project | From | What it is for |
| --- | --- | --- |
| `<PROJECT>.code-workspace` | `project.code-workspace` here | VS Code: the Odoo source and the project as two folders, run/debug/test configurations, lint, OdooLS, the graph's MCP server, the CI and graph tasks |
| `<PROJECT>.conf` | `project.conf` here, filled from `C:\odoo\odoo17\odoo.conf` | the Odoo server config: DB user and passwords, addons path ending in the project, its own port |
| `odools.toml` | written by `odoo-ai workspace` | OdooLS (the Odoo language server): which addons to read, which warnings matter |
| `.mcp.json` | written by `odoo-ai workspace` | Claude Code's access to this project's knowledge graph |
| `CLAUDE.md` | written by `odoo-ai workspace` | the project's operating instructions for an AI assistant: the graph, how to run Odoo, the rules |

All five are listed in the project's `.git/info/exclude`, so they never reach its repository. The project's graph has its config in `tools\kg\config\local\<project>-odoo17.toml` and its data in `%LOCALAPPDATA%\odoo-kg\projects\<project>-odoo17` - outside every source tree, by the graph engine's own rule.

## Making a project

```powershell
odoo-ai new-project <PROJECT> --git <repository url> [--branch <branch>]
```

It clones the project, writes the five files and builds the graph. For a project already in `projects\`, leave out `--git`. By hand: copy `project.code-workspace` and `project.conf` into the project, replace `{{PROJECT}}`, `{{PROJECT_ID}}` (`<project>-odoo17`, lower case), `{{CONF}}` and the `<...>` placeholders, then run `odoo-ai workspace` inside it for the other three.

How a project is built on this structure - its modules, rules, CI and tests - is in `docs\PROJECT_PLAN.md`.

## Changing what every project gets

Edit the files here, on this branch: they are Odoo 17's. `odoo-ai workspace` keeps existing projects' generated files current; a hand-edited workspace keeps your edits.
