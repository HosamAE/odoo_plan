# Odoo 20 AI System Rules (AGENTS.md)

Target Framework: **Odoo 20.0**
Python Environment: `C:\odoo\odoo20\venv\Scripts\python.exe`
Odoo Executable: `C:\odoo\odoo20\odoo\odoo-bin`

---

## 🚨 MANDATORY PROTOCOLS & BLIND IMPLEMENTATION GUARD

### 1. INSPECTION BEFORE IMPLEMENTATION & BLIND IMPLEMENTATION GUARD

* **Inspect System First**: Inspect manifests, dependencies, DB schemas, migrations, configuration, tests, and nearby code before adding or changing functionality.
* **NEVER Invent Repository Contents**: Never invent schemas, modules, or architecture. If required evidence is missing, mark tasks as `BLOCKED BY MISSING PREFLIGHT` and state exactly which files or decisions are required.

### 2. 16-POINT COLLISION & CHANGE-IMPACT MATRIX

* Search the complete system before creating a new: **Module, Model, Field, Method, API route, Controller, View, Menu, Action, XML ID, Registry entry, Asset, Cron job, Configuration key, Database constraint, Integration**.
* Use the knowledge graph first (`kg_find_modules`, `kg_dependents`, `kg_impact`), then confirm in source.
* Determine change strategy: **Extend**, **Replace**, **Migrate**, **Coexist**, or **Remove**.

### 3. GIT GUARD & ODOO COMMIT GUIDELINES

* **NEVER** run any Git command without explicit user confirmation (`Yes/No`).
* **Manifest Version Bump**: Do NOT bump version in `__manifest__.py` during regular code edits. ONLY bump version (`20.0.x.y.z`) when explicitly requested by the user to commit or push to Git.
* **Odoo Commit Format**: `[FIX] module: summary`, `[ADD] module: summary`, `[IMP] module: summary`, `[REF] module: summary`, `[REM] module: summary`.

### 4. RECAP, SECURITY & PROCESS TERMINATION

* Restate request understanding in 2-3 bullet points before writing code.
* ZERO hardcoded secrets/API keys in source code or logs.
* Always enforce `--stop-after-init` on Odoo updates/tests to terminate process immediately.

### 5. STRICT SEQUENTIAL WORKFLOW & NO STEP-JUMPING

* **Step-by-Step Execution**: Complete and empirically verify the current phase before moving or proposing the next phase.
* **No Premature Manifest Edits**: NEVER touch `"version"` in `__manifest__.py` during coding/debugging.
* **Explicit Gate Notification**: Always inform the user upon completing a phase before initiating the next.

---

## 🧠 KNOWLEDGE GRAPH (odoo-kg)

* Run `kg_status` before any larger analysis. If it reports `stale`, treat every answer as **historical**.
* Check the relevant source even when the graph agrees with you: it records static structure, not runtime behaviour.
* Rebuild after commits that matter: `python -m odoo_kg index --config C:\odoo\odoo-kg\config\<project>.toml`.
* The graph is per developer and per checkout. Never commit it. Do not modify `odoo-kg` during an ordinary Odoo task.

---

## 🛠️ ODOO 20 ARCHITECTURE

Reference: [ODOO20_FRAMEWORK_NOTES.md](file:///c:/odoo/odoo20/ODOO20_FRAMEWORK_NOTES.md)

* Python 3.12 - 3.14 (`odoo/release.py`).
* The Odoo 19 rules are the baseline. Confirm each against `C:\odoo\odoo20\odoo` before relying on it, and record what changed.

---

## 🚀 ADVANCED RULES (SELF-VERIFY & NEGATIVE CONSTRAINTS)

* **Self-Verification**: Auto-verify XML/Python syntax via `odoo-ai lint` before marking tasks done.
* **Memory Bank**: Read/update `PROGRESS.md` in project root.
* **Diff & Patching**: Output targeted snippets instead of full files.
* ❌ **NO Raw SQL**, ❌ **NO `len(search())`**, ❌ **NO Hardcoded DB IDs**, ❌ **NO Direct DOM in OWL**, ❌ **NO Unused Imports**.
