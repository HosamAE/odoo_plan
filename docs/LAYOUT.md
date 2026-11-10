# Odoo workspace layout

One folder per Odoo version, holding everything of that version: its source, its Python and its tools; its projects sit beside it in ProjectsOdoo<NN>. Versions differ (framework, Python, lint rules, CI), so the tools of one version may change without touching the others. What belongs to one project lives inside that project.

```text
C:\odoo\
├── bin\                         odoo-ai launcher, on PATH, shared by every version
├── backups\                     what a cleanup moved aside
├── odoo<NN>\                    one Odoo version, not in git
│   ├── odoo\  venv\  odoo.conf  Odoo source, its Python, its server config
│   ├── AGENTS.md  PROGRESS.md   rules and notes for this version
│   ├── tools\                   these tools: branch <NN>.0 of the tools repository
│   │   ├── cli.py               odoo-ai: lint, test, audit, kg, workspace, ci, doctor, move-projects
│   │   ├── .pylintrc  .flake8   lint configuration of this version
│   │   ├── pylint_plugins\      only where the version needs one (19, 20)
│   │   ├── vscode-extension\    Odoo CI extension (build output .vsix is not in git)
│   │   ├── kg\                  knowledge-graph engine; kg\config\local\<project>.toml per project
│   │   ├── launcher\            source of C:\odoo\bin
│   │   └── setup\               one-off setup scripts of this version, when it has any
│
└── ProjectsOdoo<NN>\          projects of that version, beside odoo<NN> (also accepted: odoo<NN>\projects)
    └── <project>\           a project: its own git. Inside it, never committed:
                                 <project>.conf, <project>.code-workspace, odools.toml,
                                 .mcp.json, CLAUDE.md (odoo-ai workspace writes them)
```

## The launcher

`odoo-ai` in `C:\odoo\bin` only picks the version, then runs `C:\odoo\odoo<NN>\tools\cli.py`:

1. `-v NN` on the command line;
2. else the version of `-p <path>`;
3. else the version of the current folder (`odoo<NN>\...` or `ProjectsOdoo<NN>\...`).

With no version anywhere, `workspace`, `doctor`, `info` and `move-projects` run once per installed version.

## Everyday commands

| Task | Command |
| --- | --- |
| Check a version's setup, repair what is safe | `odoo-ai doctor` / `odoo-ai doctor --fix` |
| Wire projects (workspace, graph config, `.mcp.json`, `CLAUDE.md`, `odools.toml`) | `odoo-ai workspace` |
| Test before pushing | the `CI` button in VS Code, or `odoo-ai ci --static-only` / `odoo-ai ci` |
| Knowledge graph of the project here | `odoo-ai kg status` / `odoo-ai kg index` |
| Rebuild every project's graph | `C:\odoo\bin\build_graphs.bat` |
| Optional: move projects from `C:\odoo\ProjectsOdoo<NN>` into `odoo<NN>\projects` (not done here) | `odoo-ai move-projects`, then `--apply` |

## The knowledge graph stays per project

Each project has its own graph: its config in its version's tools (`C:\odoo\odoo<NN>\tools\kg\config\local\<project>.toml`, never committed), its data in `%LOCALAPPDATA%\odoo-kg\projects\<project>`, and its own `.mcp.json`. The engine itself is part of every version's tools (`tools\kg`), so a version's graph is built by that version's engine. `odoo-ai workspace` writes them with the tools of the project's version; the graph is never committed.

## A new machine

1. Clone the Odoo source and create `venv` in `C:\odoo\odoo<NN>` (see `setup\` of a version that has one).
2. Clone the tools: `git clone -b <NN>.0 <tools repository> C:\odoo\odoo<NN>\tools`.
3. `python C:\odoo\odoo<NN>\tools\cli.py doctor --fix` (launcher, PATH, pylint path).
4. Install the extension: build it with `vscode-extension\build_vsix.py`, then `code --install-extension`.
5. Clone each project into `C:\odoo\ProjectsOdoo<NN>\<project>`, then `odoo-ai workspace` inside it.

## Moving a change to another version

The versions are branches of one repository. A change made for 19 reaches 20 only on purpose: `git cherry-pick <commit>` in `C:\odoo\odoo20\tools`, then `odoo-ai doctor` there.

## A new Odoo version, in one command

```text
odoo-ai new-version 20        # clones the 20.0 tools, makes ProjectsOdoo20, runs `setup`
odoo-ai new-version 20 --check  # same, but changes nothing and lists what is missing
```

You download Odoo, enterprise and the themes yourself (`setup` prints the exact commands), then clone a
project into `C:\odoo\ProjectsOdoo20\` and run `odoo-ai workspace -p <project>`.

## Keeping the versions in step: `main` is the only place you edit

The `<NN>.0` branches are generated from `main` (which is the Odoo 19 flavour). What differs per version
(`.pylintrc`, `.flake8`, the `gates/check_odoo<NN>_rules.py`, `docs/version`, `project/`, `setup/`, `pylint_plugins`)
lives in `versions/<NN>/`; everything else is shared.

| I want to | Command |
|---|---|
| see what each version branch would change | `odoo-ai sync-versions` |
| build a version's tools into a folder | `odoo-ai sync-versions build -v 17 --out DIR` |
| make a local review branch `sync/<NN>.0` (never pushed) | `odoo-ai sync-versions apply -v 17` |
| refresh `versions/<NN>` from that branch | `odoo-ai sync-versions snapshot -v 17` |

## Secrets

`odoo-ai secrets --install-hook` (once per repository) refuses a commit that adds a password or token and says
where, never what. `odoo-ai secrets FILE...` scans files; `# noqa: secret <reason>` marks a deliberate line.
