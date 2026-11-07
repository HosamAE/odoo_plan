# Odoo CI (local)

Run a project's CI on this machine before you push, from VS Code.

- **Status bar** (bottom left): `CI`. Click it to choose a check. It spins while a check runs, then shows a green tick or a red cross.
- **Odoo CI view** (activity bar): every step with its state. Failure lines that name a file and line open that line when clicked.
- **Problems panel**: the same failures, with the source "Odoo CI".
- **Commands** (Command Palette, `Odoo CI:`): static checks, full CI, full CI stopping running Odoo servers, cancel, show output, open the Odoo test log.

| Check | What it runs | Time |
|---|---|---|
| Static checks | Every `run:` step of the `checks` job in `.github/workflows/ci.yml`, as written | seconds |
| Full CI | The static checks, then a fresh database with every shipped module installed (no demo data), every test, and the project's log gate | about 20 minutes |

No setup per workspace. The project is found from the folder path (`...\ProjectsOdoo<NN>\<project>`), and each project's own venv, `.conf` and `ci.yml` are used. The checks are read from `ci.yml` on every run, so a gate added there runs here without changing the extension.

The work is done by `odoo-ai ci` (`C:\odoo\odoo_ai_cli\ci_local.py`). The extension starts it and draws the `::ci::` events it prints.

## Build and install

```
python C:\odoo\odoo_ai_cli\vscode-extension\build_vsix.py
code --install-extension C:\odoo\odoo_ai_cli\vscode-extension\odoo-ci-1.0.0.vsix
```

Then reload the VS Code window.
