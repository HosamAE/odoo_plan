# odoo_plan: the structure and tools of one Odoo version

Each branch is one Odoo version (`17.0`, `18.0`, `17.0`, `20.0`), checked out as that version's tools in `C:\odoo\odoo<NN>\tools`. It carries the structure and everything around the code - the CI, the knowledge graph, the AI rules, lint, the project template, the plan projects follow - so nothing has to be built again from the start. A change made on one branch changes that version only.

What it does **not** carry: Odoo, enterprise, the themes, third-party addons and the venv. You download and create those yourself, as usual; `odoo-ai setup --check` says which are missing and gives the command for each.

## Getting to work on a new machine

```powershell
# 1. the structure and tools of the version
git clone -b 17.0 https://github.com/HosamAE/odoo_plan C:\odoo\odoo17\tools

# 2. what you download yourself (setup --check lists the exact commands)
git clone --depth 1 -b 17.0 https://github.com/odoo/odoo.git C:\odoo\odoo17\odoo
git clone --depth 1 -b 17.0 https://github.com/odoo/enterprise.git C:\odoo\odoo17\enterprise      # with access
git clone --depth 1 -b 17.0 https://github.com/odoo/design-themes.git C:\odoo\odoo17\design-themes
#    third-party addons into C:\odoo\odoo17\third-party
py -3.10 -m venv C:\odoo\odoo17\venv
C:\odoo\odoo17\venv\Scripts\python.exe -m pip install -r C:\odoo\odoo17\odoo\requirements.txt -r C:\odoo\odoo17\tools\setup\requirements-extra.txt

# 3. the rest: odoo.conf, launcher on PATH, VS Code extension, rules files
python C:\odoo\odoo17\tools\cli.py setup
python C:\odoo\odoo17\tools\cli.py setup --db-role     # the PostgreSQL role (asks for postgres's password)

# 4. each project
odoo-ai new-project Acme --git https://github.com/<owner>/<repo> --branch <branch>
```

Then open `C:\odoo\odoo17\projects\Acme\Acme.code-workspace`.

## The version folder

```text
C:\odoo\
├── bin\                                odoo-ai launcher (setup puts it on PATH)
└── odoo17\
    ├── odoo\ enterprise\ design-themes\  downloaded by you
    ├── third-party\  venv\               yours too
    ├── odoo.conf                         written by setup (passwords generated, never committed)
    ├── AGENTS.md  ODOO17_*.md            this version's rules (setup copies them from tools\docs\version)
    ├── tools\                            THIS REPOSITORY, branch 17.0
    └── projects\<PROJECT>\               a project: its own git; its workspace, conf, odools.toml,
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

## Every day

| Task | Command |
| --- | --- |
| Is this machine whole? | `odoo-ai doctor` (`--fix` repairs what is safe), `odoo-ai setup --check` |
| A new project | `odoo-ai new-project <name> --git <url>` |
| Test before pushing | the `CI` button in VS Code, or `odoo-ai ci` |
| The version's gates in a project's own CI | `odoo-ai gates --install` |
| Run, upgrade and judge a project's server | `odoo-ai server dbs --conf <project>.conf` (the databases to pick from); `odoo-ai server upgrade --conf ... -d <db> [-m all\|a,b]` (refuses a database another session uses); `odoo-ai server drift --conf ... -d <db>` (stored fields the code has and the database lacks, and the `-u` that fixes it); `odoo-ai server health --log <file>` (starting / healthy / problems / failed, with what to do); `odoo-ai server command` prints the exact start line. The extension's **Start Odoo Server**, **Start with Options...**, **Upgrade...** and the Health row use these (`server_ctl.py`, `server_scan.py`, `server_dbs.py`); the **Cockpit** page (`odooCi.cockpit`) puts it all in front of you: the server and its health, every database (size, version, last login; upgrade, check, back up, drop), a database's modules against the code, the progress of a start or an upgrade, the errors and warnings, and the live log; `odoo-ai server inventory|modules|backup|drop` are what it asks of the tools; drop needs `--yes`, refuses a database in use, and with `--backup-dir` backs up first and stops if that fails; its **Errors & Warnings** view (every error and warning with its line: next/previous, pick, filter by kind/database/module, the log opened painted red and yellow, your own file when a traceback ends in it) and the upgrade's progress (module i of N, what is left, which modules were upgraded) read `server health --json` |
| Databases on other servers (over SSH) | `odoo-ai server remote-hosts` (the hosts of `~/.ssh/config`, the ones Remote-SSH uses); `remote-check --host H`; `remote-list --host H` (its databases with size, owner, sessions, and the Odoo instances of `/etc/odoo*/*.conf`: only `db_name`, `db_user`, `data_dir`, `http_port` are read); `remote-pull --conf ... --host H -d <db> [--as <local>]` (a remote database into a NEW local one: dump, restore, filestore, then `odoo-bin neutralize`; never over an existing one; a failed pull removes its half-made copy); `remote-push ... --owner <role> --instance <conf> --yes` (a local database up as a NEW one, owned by the instance's role, with its filestore); `remote-backup --host H -d <db> --dir D` (a dump file here); `remote-drop --host H -d <db> --confirm H/<db>` (refuses `postgres`/templates, a database with sessions or one an instance serves; `--backup-dir` backs up first and stops if that fails). Key or agent login only (`--interactive` lets ssh ask); the postgres commands run through `sudo -n -u postgres` (`--no-sudo`/`--sudo-user`); no password is stored or printed (`remote_dbs.py`). The **Cockpit** (bottom panel, `odooCi.cockpitPanel`, or an editor tab, `odooCi.cockpit`) has a **Remote servers** tab: pick a host, see its databases, Pull / Backup file / Drop each one, Push a local one, and a host that wants a password offers to put your key on it |
| The project's graph | `odoo-ai kg status` / `odoo-ai kg index`; all of them: `C:\odoo\bin\build_graphs.bat` |

## Changing the tools

Change them in the version's own `tools` folder and commit on its branch. Another version gets a change on purpose - never through a shared copy: `odoo-ai port <commit>` tries it on the other versions, `--apply` commits it there.
