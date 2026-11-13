# Odoo 19 AI System Rules (AGENTS.md)

Target Framework: **Odoo 19.0 (Master / Cutting-Edge)**
Python Environment: `C:\odoo\odoo19\venv\Scripts\python.exe`
Odoo Executable: `C:\odoo\odoo19\odoo\odoo-bin`

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
* **Manifest Version Bump**: Do NOT bump version in `__manifest__.py` during regular code edits. ONLY bump version (`19.0.x.y.z`) when explicitly requested by the user to commit or push to Git.
* **Module Registry Guard (`.github/modules.txt`)**: When adding, removing, renaming, or deprecating a module, you MUST immediately synchronize `.github/modules.txt` (`gate`, `ship`, or `skip`). If the project has a script that checks the register (`.github/scripts/<company>_modules.py`), run it.
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

## 🛠️ ODOO 19 SPECIFIC ARCHITECTURE & BREAKING CHANGES

Full Reference: See [ODOO19_BREAKING_CHANGES_AND_FRAMEWORK_RULES.md](file:///c:/odoo/odoo19/ODOO19_BREAKING_CHANGES_AND_FRAMEWORK_RULES.md)

1. **Translation API**: Use `self.env._("Text")` instead of static `from odoo import _`.
2. **Domain Expressions**: Use `from odoo.fields import Domain` instead of deprecated `odoo.osv.expression`.
3. **Mail Composer Context**: Use `default_res_ids: [id]` list instead of deprecated `default_res_id`.
4. **No Inline `parent.` Context**: Do not use `parent.field` in inline list contexts; use `@api.onchange`.
5. **Product Catalog Kwargs**: Always pass `**kwargs` to `_get_product_catalog_record_lines`.
6. **Strict Manifest Categories**: Use the categories the project has registered (its manifests already show them), e.g. `<Company>/Booking`; do not invent a new one.
7. **Search View Syntax**: `<group>` inside `<search>` MUST only contain `<filter>` tags with `group_by`.

---

## 🚀 ADVANCED RULES (SELF-VERIFY & NEGATIVE CONSTRAINTS)

* **Self-Verification**: Auto-verify XML/Python syntax via `odoo-ai lint` and verify the module register with the project's own script (`.github/scripts/<company>_modules.py`, when it has one) before marking tasks done.
* **Memory Bank**: Read/update `PROGRESS.md` in project root.
* **Diff & Patching**: Output targeted snippets instead of full files.
* ❌ **NO Raw SQL**, ❌ **NO `len(search())`**, ❌ **NO Hardcoded DB IDs**, ❌ **NO Direct DOM in OWL**, ❌ **NO Unused Imports (Zero unused symbols/modules)**.
