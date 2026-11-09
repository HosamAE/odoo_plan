# Odoo Studio & CI Pro

<p align="center">
  <img src="media/icon.png" width="128" height="128" alt="Odoo Studio & CI Pro Logo" />
</p>

**The Unified Odoo Multi-Server Studio & Local CI Suite for Professional Odoo Developers.**

* **Conceived, Architected & Developed by:** [HosamAE](https://github.com/HosamAE)
* **Target Frameworks:** Odoo 17.0, 18.0, 19.0, 20.0
* **Repository:** [https://github.com/HosamAE/odoo_plan](https://github.com/HosamAE/odoo_plan)
* **License:** MIT (All original architecture and design rights reserved to HosamAE)

---

## 🌟 Key Highlights

### ⚡ 1. Ephemeral Clean Template DB Testing (3s)
Run unit tests on a pristine, isolated PostgreSQL database clone in **3 seconds** instead of the typical 20-minute core install. The test database is cloned via PostgreSQL `TEMPLATE` and dropped automatically upon completion.

### 🖥️ 2. Integrated Project Server Manager
Dedicated server control per workspace with automatic `xxyy` port recognition:
- **`[▶ Start Server]`**: Launches Odoo server in an integrated terminal with `--dev=all`.
- **`[⏹ Stop Server]`**: Terminating active processes bound to the project port.
- **`[🌐 Open in Browser]`**: One-click direct navigation to `http://localhost:<port>`.

### 🎯 3. Static XML & XPath Validator
Detects XML and XPath structural errors before runtime:
- Verifies `expr` syntax, unbalanced brackets, missing `@` on attribute tests, and trailing slashes.
- Integrates with the native VS Code **Problems panel** (`Ctrl+Shift+M`) with red squiggly underlines on the exact XML line.

### 📸 4. Instant Database Snapshots & Rollbacks (1-2s)
Take instant checkpoints before testing schema modifications or addon upgrades:
- `odoo-ai db-snap save <label>`: Takes a block-level database clone in ~1 second.
- `odoo-ai db-snap restore <label>`: Reverts database to the saved state in ~2 seconds.

### 🌐 5. Translation & i18n Synchronization
Engineered for multilingual projects across any target language (e.g. Swedish, Arabic, French, German, Spanish):
- Scans `_("...")`, `self.env._("...")`, and XML `string="..."` attributes.
- Compares with existing PO files to report untranslated strings.
- Appends missing terms cleanly into `.po` files without altering existing translations.

### ➕ 6. Addon Scaffolding (`scaffold_plus`)
Generate standard-compliant Odoo addons in seconds:
- Clean MVC structure compliant with Odoo 19 R1-R10 rules.
- Fully formed Search views, Form views, List views, Actions, and Menus.
- Security `ir.model.access.csv` and auto-registration in `.github/modules.txt`.

---

## 🚀 Quick Setup & Usage

### Inside any Odoo Project Workspace:
1. Open the project's `.code-workspace` in VS Code or Antigravity IDE.
2. Click the **Odoo Studio & CI** icon in the Activity Bar.
3. Use the **Project Control Panel** for server management, testing, and developer tools.
4. Click the **Odoo Status Bar** item in the bottom-left corner for quick-action shortcuts.

### Build and Package:
```bat
python C:\odoo\odoo19\tools\vscode-extension\build_vsix.py
code --install-extension C:\odoo\odoo19\tools\vscode-extension\odoo-ci-1.4.0.vsix --force
```

---

## 👤 Author & Copyright

**HosamAE**  
GitHub: [https://github.com/HosamAE](https://github.com/HosamAE)  
Project Plan & Master Repository: [https://github.com/HosamAE/odoo_plan](https://github.com/HosamAE/odoo_plan)
