# odoo_plan: everything one Odoo version needs to be worked on

Each branch is one Odoo version (`17.0`, `18.0`, `19.0`, `20.0`) and is checked out as that version's tools, in `C:\odoo\odoo<NN>\tools`. A branch carries what its version needs and nothing shared with the others: versions differ in Python, packages, lint rules and CI, so each one is free to change on its own.

## A new machine, from nothing

Install once: **Git**, **Python** (the version in `setup\version.json`: 3.10 for Odoo 17, 3.12 for 18-20, with the `py` launcher), **PostgreSQL** (16 or newer), **wkhtmltopdf** 0.12.6 (patched Qt), and **VS Code**.

Then, for each Odoo version you work on (19 here):

```powershell
git clone -b 19.0 https://github.com/HosamAE/odoo_plan C:\odoo\odoo19\tools
python C:\odoo\odoo19\tools\cli.py setup            # sources, venv, packages, odoo.conf, launcher, extension
python C:\odoo\odoo19\tools\cli.py setup --db-role  # the PostgreSQL role (asks for postgres's password)
odoo-ai new-project Acme --git https://github.com/<owner>/<repo> --branch testing19
```

Open `C:\odoo\odoo19\projects\Acme\Acme.code-workspace`, and work.

`odoo-ai setup --check` reports what a machine is missing without changing anything. Every step looks first, so running `setup` again is safe.

## What a version folder looks like

```text
C:\odoo\
├── bin\                         odoo-ai launcher (setup puts it on PATH)
└── odoo19\
    ├── odoo\ enterprise\ design-themes\   sources (setup clones them; enterprise needs access)
    ├── venv\  odoo.conf                   Python and server config (setup writes them; never in git)
    ├── third-party\                       -> tools\third-party (the addons this branch carries)
    ├── AGENTS.md  ODOO19_*.md             this version's rules (from tools\docs\version)
    ├── tools\                             THIS REPOSITORY, branch 19.0
    └── projects\<project>\                each project: its own git; conf, workspace, odools.toml,
                                           .mcp.json and CLAUDE.md inside it, never committed
```

## What the branch carries

| Path | What |
| --- | --- |
| `cli.py` | `odoo-ai`: `setup`, `new-project`, `workspace`, `ci`, `doctor`, `kg`, `lint`, `test`, `audit`, `move-projects` |
| `setup\version.json` | the version's Odoo branch, Python, repositories, extra packages, addons path, DB user |
| `kg\` | the knowledge-graph engine: one graph per project (`kg\config\local\`, never committed) |
| `ci_local.py` | the project's CI on this machine (`odoo-ai ci`, `--static-only`, `--at "<UTC moment>"`) |
| `.pylintrc` `.flake8` `pylint_plugins\` | lint configuration of this version |
| `vscode-extension\` | the Odoo CI extension (a CI button, a steps view, Problems) |
| `third-party\` | addons the version needs that live in no other repository |
| `docs\version\` `docs\shared\` | the version's rules (AGENTS.md, framework notes) and the rules shared by all versions |
| `launcher\` | the `odoo-ai` launcher and `build_graphs.bat` for `C:\odoo\bin` |
| `docs\LAYOUT.md` | the layout and the everyday commands in more detail |

## Every day

| Task | Command |
| --- | --- |
| Is this machine whole? | `odoo-ai doctor` (`--fix` repairs what is safe) |
| A new project | `odoo-ai new-project <name> --git <url>` |
| Test before pushing | the `CI` button in VS Code, or `odoo-ai ci` |
| The project's graph | `odoo-ai kg status` / `odoo-ai kg index`; all of them: `C:\odoo\bin\build_graphs.bat` |

## Changing the tools

Change them in the version's own `tools` folder and commit on its branch. A change that other versions need too reaches them by `git cherry-pick`, on purpose - never by sharing one copy.
