---
name: odoo-ai
description: Run this workspace's own tools (odoo-ai) correctly - doctor, gates, tests, graph, version branches, new Odoo versions, secret scan. Use before inventing a shell command for any of those.
---

# odoo-ai: the workspace's tools

`odoo-ai` is on PATH. Inside a project it picks the Odoo version from the folder (`ProjectsOdoo<NN>\<project>`);
anywhere else pass `-v <NN>`. Prefer these to hand-written commands.

| Need | Command | Changes anything? |
| --- | --- | --- |
| Is the machine whole? | `odoo-ai doctor` | no (`--fix` repairs what is safe) |
| Odoo 19 rules R1-R10 on this project | `odoo-ai gates` | no (`--install` copies the gates into the project) |
| Static checks / CI | `odoo-ai ci --static-only`, `odoo-ai ci` | no |
| Fast module test on a template DB | `odoo-ai test -m <module> --fast` | temp DB only |
| Knowledge graph | `odoo-ai kg status`, `odoo-ai kg index` | the graph only |
| The tools' own tests | `odoo-ai test-self` | no |
| Secrets in what is staged | `odoo-ai secrets --staged` | no |
| Version branches vs main | `odoo-ai sync-versions` (`fetch` first in a one-branch clone) | no; `apply` makes a local branch, never pushes |
| A new Odoo version | `odoo-ai new-version <NN> --check`, then without `--check` | `--check` changes nothing |

## Rules these tools enforce - do not work around them

- `main` of the tools repo is the only place a shared tool change is made; 17/18/20 are generated from it.
- Never put a password in a file git tracks. A real `admin_passwd` lives only in the project's own `.conf`.
- Git commands that write (`commit`, `push`, `reset`...) wait for the user's explicit yes; `--no-verify` is never used.
- `--stop-after-init` on every Odoo update or test run.
- `parent.id` in a view context is fine (Odoo's catalog buttons use it); `parent.<field>` is not - use an onchange.
