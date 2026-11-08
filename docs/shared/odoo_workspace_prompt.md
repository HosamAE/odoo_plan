# Odoo Developer Workspace Context & Setup Prompt

Copy and paste the prompt below when starting a conversation with any AI assistant to immediately contextualize it on your Odoo structure, setup commands, and workspace conventions.

***

```markdown
You are an expert Odoo AI developer assistant. I am working on custom Odoo modules. Below is the exact layout of my Odoo workspace, guidelines on how to download Odoo, and project settings. Please use this context for all subsequent tasks.

---

## 1. Directory Structure Layout

All Odoo-related source code and custom projects are organized under `C:\odoo\`.

```text
C:\odoo\
├── odoo<version>\                  # Odoo Source Code & Core Addons
│   ├── odoo\                        # Actual Python Package (cloned repository)
│   │   ├── addons\                  # Core Odoo Addons
│   │   └── odoo-bin                 # Startup executable
│   ├── venv\                        # Python Virtual Environment
│   ├── odoo.conf                    # Server config of this version (not in git)
│   ├── tools\                       # This version's own tools (branch <NN>.0 of the tools repo)
│   │   ├── cli.py                   # odoo-ai: lint, test, audit, kg, workspace, ci, doctor
│   │   ├── .pylintrc  .flake8       # Lint configuration of this version
│   │   ├── pylint_plugins\          # Only where the version needs one (19, 20)
│   │   ├── vscode-extension\        # Odoo CI extension
│   │   └── docs\LAYOUT.md           # The full layout and everyday commands
│   └── projects\                    # This version's projects
│       └── <ProjectName>\           # A project: its own git repository
│           ├── <ProjectName>.code-workspace  # VS Code workspace (not committed)
│           ├── <ProjectName>.conf            # Odoo configuration (not committed)
│           ├── odools.toml                   # OdooLS configuration (not committed)
│           ├── acme_floorplan\            # A custom addon
│           └── ...                           # Other custom addons
│
└── bin\                             # odoo-ai launcher (on PATH): picks the version, runs its tools
```

*For example, Odoo 19 is stored in `C:\odoo\odoo19\` and its projects in `C:\odoo\odoo19\projects\`. Versions in use: 17, 18, 19, 20.*

---

## 2. Setting Up a New Odoo Environment (Download & Venv)

When I ask you to set up a new Odoo version (e.g., Odoo 17, 19 or 20), perform the following steps:

1. **Clone Odoo Source Code**:
   Clone the Odoo repository from GitHub for the target version into `C:\odoo\odoo<version>\odoo` using a shallow clone:
   ```bash
   git clone https://github.com/odoo/odoo.git --depth 1 --branch <version>.0 C:\odoo\odoo<version>\odoo
   ```
2. **Create Python Virtual Environment**:
   Create a virtual environment named `venv` under `C:\odoo\odoo<version>\`:
   ```bash
   python -m venv C:\odoo\odoo<version>\venv
   ```
3. **Install Dependencies**:
   Upgrade pip and install Odoo's dependencies using the virtual environment's pip:
   ```bash
   C:\odoo\odoo<version>\venv\Scripts\python.exe -m pip install --upgrade pip
   C:\odoo\odoo<version>\venv\Scripts\pip.exe install -r C:\odoo\odoo<version>\odoo\requirements.txt
   ```
4. **Install Development Tools**:
   Install formatting and linting packages (Black, isort, pylint, pylint-odoo, flake8):
   ```bash
   C:\odoo\odoo<version>\venv\Scripts\pip.exe install black isort pylint pylint-odoo flake8
   ```

---

## 3. VS Code Workspace Configuration Rules

Every project under `odoo<version>\projects\<ProjectName>\` must have a `<ProjectName>.code-workspace` file containing two main folders:
1. `odoo` mapping to `../../odoo` (the Odoo source)
2. `Project` mapping to `.`

### Essential Workspace Settings:
* **Language server**: OdooLS (`Odoo.serverConfigPath` -> `C:/odoo/odoo<version>/projects/<Project>/odools.toml`) gives Python features; `"python.languageServer": "None"` so Pylance does not report the same things twice. `python.analysis.extraPaths` holds the Odoo source only (an addons folder there shadows the standard library).
* **Linter Severity Mapping**:
  * Style conventions must not trigger warning highlights. Map Pylint `convention` and `refactor` to `"Hint"`.
  * Map Flake8 PEP-8 codes (`E`, `W`) to `"Hint"`, keeping Pyflakes logical checks (`F`) as `"Warning"`.
* **Pylint Configuration File**: Points to `--rcfile=C:/odoo/odoo<version>/tools/.pylintrc`.
* **Pylint init-hook** (in `.pylintrc`): puts `'C:/odoo/odoo<version>/odoo'` (and, for 19+, `tools/pylint_plugins`) on `sys.path`.
* **One workspace per project**, inside the project folder; `odoo-ai workspace` writes and updates it.

---

## 4. Knowledge Graph & Project Wiring

Every project is indexed by the graph engine of its version's tools, `C:\odoo\odoo<version>\tools\kg` (read-only SQLite graph + MCP server): one graph per project.

* `odoo-ai workspace -p C:\odoo\odoo<version>\projects\<Project>` writes the project's `.code-workspace`, its graph config `C:\odoo\odoo<version>\tools\kg\config\local\<project-id>.toml`, a `<Project>.conf` when missing, and a `CLAUDE.md` knowledge-graph section if missing.
* Build: `odoo-ai kg index` inside the project (or the workspace task **odoo-kg: build graph**); status: `odoo-ai kg status`, also run automatically when the workspace opens.
* VS Code's own MCP client gets the same `odoo-kg` server through the workspace `settings.mcp`; Claude Code through `.mcp.json`.
* Before analysis call `kg_status`; `stale` means every answer is historical.
* Graph data lives under `%LOCALAPPDATA%\odoo-kg\projects\<project-id>` and is never committed.

---

## 5. Odoo Pylint Rules (.pylintrc)

The version's `C:\odoo\odoo<version>\tools\.pylintrc` file must load `pylint_odoo` and disable all strict OCA-specific manifest lints (e.g. `manifest-required-author`, `license-allowed`, `missing-readme`) and dynamic false positives (e.g. `abstract-method` and `attribute-string-redundant`) for commercial/custom development, while keeping actual logical check warnings active.
```