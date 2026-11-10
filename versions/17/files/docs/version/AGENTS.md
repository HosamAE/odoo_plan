# Odoo 17 AI System Rules (AGENTS.md)

Target Framework: **Odoo 17.0**
Python Environment: `C:\odoo\odoo17\venv\Scripts\python.exe`
Odoo Executable: `C:\odoo\odoo17\odoo\odoo-bin`

---

## 🚨 MANDATORY PROTOCOLS & BLIND IMPLEMENTATION GUARD

### 1. INSPECTION BEFORE IMPLEMENTATION & BLIND IMPLEMENTATION GUARD
* **Inspect System First**: Inspect manifests, dependencies, DB schemas, migrations, configuration, tests, and nearby code before adding or changing functionality.
* **NEVER Invent Repository Contents**: Never invent schemas, modules, or architecture. If required evidence is missing, mark tasks as `BLOCKED BY MISSING PREFLIGHT` and state exactly which files or decisions are required.

### 2. 16-POINT COLLISION & CHANGE-IMPACT MATRIX
* Search the complete system before creating a new: **Module, Model, Field, Method, API route, Controller, View, Menu, Action, XML ID, Registry entry, Asset, Cron job, Configuration key, Database constraint, Integration**.
* Determine change strategy: **Extend**, **Replace**, **Migrate**, **Coexist**, or **Remove**.

### 3. GIT GUARD & ODOO COMMIT GUIDELINES
* **NEVER** run any Git command without explicit user confirmation (`Yes/No`).
* **Manifest Version Bump**: Automatically bump version in `__manifest__.py` (`17.0.x.y.z`) before committing.
* **Odoo Commit Format**: `[FIX] module: summary`, `[ADD] module: summary`, `[IMP] module: summary`, `[REF] module: summary`, `[REM] module: summary`.

### 4. RECAP, SECURITY & PROCESS TERMINATION
* Restate request understanding in 2-3 bullet points before writing code.
* ZERO hardcoded secrets/API keys in source code or logs.
* Always enforce `--stop-after-init` on Odoo updates/tests to terminate process immediately.

### 5. STRICT SEQUENTIAL WORKFLOW & NO STEP-JUMPING
* **Step-by-Step Execution**: Complete and empirically verify the current phase (e.g., Code Fix & Odoo DB Test) BEFORE moving or proposing the next phase.
* **No Premature Manifest Edits**: NEVER touch or bump `"version"` in `__manifest__.py` during coding/debugging. Version bumps are strictly executed ONLY in the final release step after explicit user approval to commit.
* **Explicit Gate Notification**: Always inform the user upon completing a phase before initiating the next workflow phase.

---

## 🛠️ ODOO 17 SPECIFIC ARCHITECTURE
1. **OWL 2.0 Components**: Use modern OWL syntax (`import { Component, useState, onWillStart } from "@odoo/owl";`).
2. **Assets Backend**: Register web assets under `'assets': {'web.assets_backend': [...]}` in `__manifest__.py`.
3. **ORM & Security**: Python 3.10+ ORM standards. Include `security/ir.model.access.csv` for custom models.

---

## 🚀 ADVANCED RULES (SELF-VERIFY & NEGATIVE CONSTRAINTS)
* **Self-Verification**: Auto-verify XML/Python syntax via `odoo-ai lint` before marking tasks done.
* **Memory Bank**: Read/update `PROGRESS.md` in project root.
* **Diff & Patching**: Output targeted snippets instead of full files.
* ❌ **NO Raw SQL**, ❌ **NO `len(search())`**, ❌ **NO Hardcoded DB IDs**, ❌ **NO Direct DOM in OWL**.
