# Cockpit Odoo

<p align="center">
  <img src="https://files.catbox.moe/xz2xln.png" width="128" height="128" alt="Cockpit Odoo Logo" />
</p>

<p align="center">
  <strong>The Unified Odoo Multi-Server Studio & Local CI Suite for Professional Developers.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Odoo-17%20%7C%2018%20%7C%2019%20%7C%2020-714B67?logo=odoo" alt="Odoo Versions" />
  <img src="https://img.shields.io/badge/Fast%20DB%20Test-3s-0ea5e9" alt="Fast Test" />
  <img src="https://img.shields.io/badge/DB%20Snapshots-1s-22c55e" alt="DB Snapshots" />
  <img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License" />
</p>

---

<p align="center">
  <img src="https://files.catbox.moe/8e9emh.png" width="100%" alt="Cockpit Odoo Architecture" />
</p>

## 🌟 Key Features

### ⚡ 1. Ephemeral Clean Template DB Testing (3 Seconds)
Run unit tests on a pristine, isolated PostgreSQL database clone in **3 seconds** instead of the typical 20-minute core install. The test database is cloned via PostgreSQL `TEMPLATE` and dropped automatically upon completion.

<p align="center">
  <img src="https://files.catbox.moe/69cvmt.png" width="100%" alt="Fast Test Benchmark Comparison" />
</p>

### 🖥️ 2. Integrated Project Server Manager
Dedicated server control per workspace with automatic port recognition:
- **`[▶ Start Server]`**: Launches Odoo server in an integrated terminal with `--dev=all`.
- **`[⏹ Stop Server]`**: Terminates active processes bound to the project port.
- **`[🌐 Open in Browser]`**: One-click direct navigation to `http://localhost:<port>`.

### ⚙️ 3. Quick Project Setup Wizard (Works on ANY Project)
No complex setup needed. Run **`Odoo Studio: Quick Project Setup Wizard`** to configure any Odoo repository in seconds:
- Automatically detects Python virtualenvs (`venv`) and Odoo binaries.
- Configures dedicated server port (`8069`, `1900`, etc.).
- Sets target translation language (`sv`, `ar`, `fr`, `de`, etc.).
- Prepares the Clean Template DB for instant 3s testing in one click.

### 🎯 4. Static XML & XPath Validator
Detects XML and XPath structural errors before runtime:
- Verifies `expr` syntax, unbalanced brackets, missing `@` on attribute tests, and trailing slashes.
- Integrates with the native VS Code **Problems panel** (`Ctrl+Shift+M`) with red squiggly underlines on the exact XML line.

### 📸 5. Instant Database Snapshots & Rollbacks (1-2 Seconds)
Take instant checkpoints before testing schema modifications or addon upgrades:
- **Save Snapshot**: Takes a block-level database clone in ~1 second.
- **Restore Snapshot**: Reverts database to the saved state in ~2 seconds.

### 🌐 6. Multilingual Translation & i18n Synchronization
Engineered for multilingual projects across any target language (e.g. Swedish, Arabic, French, German, Spanish):
- Scans `_("...")`, `self.env._("...")`, and XML `string="..."` attributes.
- Compares with existing PO files to report untranslated strings.
- Appends missing terms cleanly into `.po` files without altering existing translations.

### ➕ 7. Addon Scaffolding (`scaffold_plus`)
Generate standard-compliant Odoo addons in seconds:
- Clean MVC structure compliant with Odoo 19 R1-R10 rules.
- Fully formed Search views, Form views, List views, Actions, and Menus.
- Security `ir.model.access.csv` and auto-registration in `.github/modules.txt`.

---

### 🧭 8. Versions & Environment view (new in 1.5)
One panel lists **Odoo 17, 18, 19 and 20** on this machine: tools, source and venv ticked or flagged, the branch of the tools, and every project of that version (click to open it in a new window). The version of the window you are in is marked.

* **Set up an Odoo version** runs `odoo-ai new-version NN`: it clones the `NN.0` tools, makes `ProjectsOdooNN` and lists what you still download. *Check only* changes nothing.
* **Maintenance**: Doctor, tools self-test, version branches vs `main`, secret scan of the staged files, the secret pre-commit hook, the project's Odoo gates, and the layout guide.
* Works with no project open. Terminal commands adapt to PowerShell, cmd or bash.

### 🛠️ 9. Quick fixes for the Odoo rules (new in 1.6)
A gate finding such as `R3 len(search(...))` shows in the Problems tab with its rule code. The light bulb offers **Use search_count()** (only where it is safe: not when `order=` is passed) and, for a line that must break a rule on purpose, **add `noqa: odoo19 REASON`**.

### 🔎 10. One control panel, and a server it finds however it was started (new in 1.7)
**Project Control Panel** is now the only project view: *Server*, *Quality & CI Gates* (with the live progress of the last run under its buttons) and *Developer Tools*. The separate "CI Checks & Progress" window is gone; its buttons moved to the panel's title bar.

The server is not recognised only when this extension started it. It looks at the running Odoo processes of the machine, so a server started from any terminal, a task, the debugger, a script or a service shows as **Running** with its port and pid, and a status-bar item appears (`:1900`, click to open). A server of another project or another configuration is listed as **Another Odoo is running** with its own port. *Stop* ends the server whichever way it was started (and asks before stopping one that is not this project's); *Open in Browser* uses the port actually in use. Something answering on the configured port that is not a local process (Docker, WSL) also counts as running.

## 🚀 Quick Setup & Usage

1. Open any Odoo project folder in VS Code or Antigravity IDE.
2. Click the **Cockpit Odoo** icon in the Activity Bar.
3. Click **`[⚙ Setup Wizard]`** to confirm Python path, Server Port, and Default Language.
4. Use the **Project Control Panel** for server management, testing, and developer tools.

---

<p align="center">
  <sub>Developed & Maintained by <a href="https://github.com/HosamAE">HosamAE</a> • Licensed under the MIT License</sub>
</p>
