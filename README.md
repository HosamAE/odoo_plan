# odoo_plan: the structure and tools of one Odoo version

Each branch is one Odoo version (`17.0`, `18.0`, `19.0`, `20.0`), checked out as that version's tools in `C:\odoo\odoo<NN>\tools`. It carries the structure and everything around the code - the CI, the knowledge graph, the AI rules, lint, the project template, the plan projects follow - so nothing has to be built again from the start. The branches are generated from `main` (`odoo-ai sync-versions`): a change that every version shares is made once, in `main`; only what really differs between versions lives in `versions/<NN>/`.

What it does **not** carry: Odoo, enterprise, the themes, third-party addons and the venv. You download and create those yourself, as usual; `odoo-ai setup --check` says which are missing and gives the command for each.

## Getting to work on a new machine

```powershell
# 1. the structure and tools of the version
git clone -b 19.0 https://github.com/HosamAE/odoo_plan C:\odoo\odoo19\tools

# 2. what you download yourself (setup --check lists the exact commands)
git clone --depth 1 -b 19.0 https://github.com/odoo/odoo.git C:\odoo\odoo19\odoo
git clone --depth 1 -b 19.0 https://github.com/odoo/enterprise.git C:\odoo\odoo19\enterprise      # with access
git clone --depth 1 -b 19.0 https://github.com/odoo/design-themes.git C:\odoo\odoo19\design-themes
#    third-party addons into C:\odoo\odoo19\third-party
py -3.12 -m venv C:\odoo\odoo19\venv
C:\odoo\odoo19\venv\Scripts\python.exe -m pip install -r C:\odoo\odoo19\odoo\requirements.txt -r C:\odoo\odoo19\tools\setup\requirements-extra.txt

# 3. the rest: odoo.conf, launcher on PATH, VS Code extension, rules files
python C:\odoo\odoo19\tools\cli.py setup
python C:\odoo\odoo19\tools\cli.py setup --db-role     # the PostgreSQL role (asks for postgres's password)

# 4. each project
odoo-ai new-project Acme --git https://github.com/<owner>/<repo> --branch testing19
```

Then open `C:\odoo\ProjectsOdoo19\Acme\Acme.code-workspace`.

Another version, in one command: `odoo-ai new-version 20` (clones the `20.0` tools, makes `ProjectsOdoo20`, runs `setup`; add `--check` to change nothing).

## The version folder

```text
C:\odoo\
├── bin\                                odoo-ai launcher (setup puts it on PATH)
└── odoo19\
    ├── odoo\ enterprise\ design-themes\  downloaded by you
    ├── third-party\  venv\               yours too
    ├── odoo.conf                         written by setup (passwords generated, never committed)
    ├── AGENTS.md  ODOO19_*.md            this version's rules (setup copies them from tools\docs\version)
    ├── tools\                            THIS REPOSITORY, branch 19.0

C:\odoo\ProjectsOdoo19\<PROJECT>\         a project: its own git; its workspace, conf, odools.toml,
                                          .mcp.json and CLAUDE.md inside it, never committed
```

## What the branch carries

| Path | What |
| --- | --- |
| `cli.py` | `odoo-ai`: `setup`, `new-project`, `workspace`, `ci`, `gates`, `port`, `doctor`, `kg`, `lint`, `test`, `audit`, `move-projects` |
| `project\` | what every project gets: the workspace and conf templates, and what the generated files are for |
| `docs\PROJECT_PLAN.md` | how a project is built on this structure: its files, rules, gates, tests, the way to a push |
| `docs\version\` `docs\shared\` | the version's AI rules (`AGENTS.md`, framework notes) and the rules all versions share |
| `setup\version.json` `setup\requirements-extra.txt` | the version's Python, sources, addons path, DB user, and the packages beyond Odoo's |
| `gates\` | the version's gates (names, framework rules, dependencies, log): `odoo-ai ci` runs them on every project, `odoo-ai gates --install` puts them in a project's CI |
| `kg\` | the knowledge-graph engine: one graph per project |
| `ci_local.py` `vscode-extension\` | the project's CI on this machine (`odoo-ai ci`, `--at "<UTC moment>"`) and the VS Code button for it |
| `.pylintrc` `.flake8` `pylint_plugins\` | lint configuration of this version |
| `launcher\` | the `odoo-ai` launcher and `build_graphs.bat` for `C:\odoo\bin` |
| `docs\LAYOUT.md` | the layout and the everyday commands in more detail |
| `sync_versions.py` `versions\` | builds the `17.0`/`18.0`/`20.0` branches from `main`; `versions\<NN>` holds only what differs |
| `secret_scan.py` | refuses a commit that adds a password or token (`odoo-ai secrets`) |
| `env_root.py` | finds the workspace root (`ODOO_ROOT`, else relative to the tools): no `C:\odoo` in the code |
| `tests\` | the tools' own tests: `odoo-ai test-self` |

## Every day

| Task | Command |
| --- | --- |
| Is this machine whole? | `odoo-ai doctor` (`--fix` repairs what is safe), `odoo-ai setup --check` |
| A new Odoo version | `odoo-ai new-version <NN>` (`--check` changes nothing) |
| A new project | `odoo-ai new-project <name> --git <url>` |
| Test before pushing | the `CI` button in VS Code, or `odoo-ai ci` |
| The version's gates in a project's own CI | `odoo-ai gates --install` |
| Keep a password out of git | `odoo-ai secrets --install-hook` once per repository; `odoo-ai secrets --staged` by hand |
| Test the tools themselves | `odoo-ai test-self` |
| Claude Code follows the workspace rules (git asks first, secrets refused, edits checked) | `odoo-ai claude --install` in the project (`--check` changes nothing) |
| Fast tests, snapshots, translations, XML | `odoo-ai init-fast-db` once, then `odoo-ai test -m <module> --fast`; `odoo-ai db-snap save|restore`; `odoo-ai i18n scan -m <module>`; `odoo-ai xpath-check` |
| The project's graph | `odoo-ai kg status` / `odoo-ai kg index`; all of them: `C:\odoo\bin\build_graphs.bat` |

## Changing the tools

Change them in `main`. `odoo-ai sync-versions` shows what each version branch would become; `apply -v NN` builds it as a local branch `sync/NN.0` (on top of `NN.0`, so pushing it is a plain fast-forward). It never pushes: that is yours.

Something only one version needs goes in `versions/<NN>/files/` (a file that replaces main's), `remove` (a file the version must not have) or `replace` (a text substitution that fails loudly when main changes under it) in `versions/<NN>/MANIFEST.json`. A clone has one branch: `odoo-ai sync-versions fetch` gets the others.
