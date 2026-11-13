# Changelog

## 2.4.3

- Light themes: the warning pill and the outlines of the module map reach the contrast WCAG asks for; the theme class of the editor is no longer lost when the page redraws.
- A map with very many arrows (a module that everything depends on) fades them and highlights the ones of the module under the pointer.
- The module map reads manifests saved with a BOM, tolerates entries computed in code, and ignores a module that lists itself.
- The two commands for people who maintain the tools (Tools CI, Version branches vs main) are hidden unless `odooCi.maintainerTools` is on.
- The real-editor test assumes nothing about a project: set `ODOO_CI_TEST_PROJECT` (and `ODOO_CI_TEST_MODULE`) to run it.

## 2.4.2

- Works on an Odoo project of any layout: the Odoo version is read from the project's own manifests (it was assumed to be 19), the server configuration is `<project>.conf` or `odoo.conf`, and the port is the one that file gives.
- When the `odoo-ai` tools are not found the cockpit says so once, at the top, with a button to set them up and a guide to read; the Graph page says "Tools missing" instead of offering an update that cannot run.
- The module map shrinks to fit when it is a little wider than its box, instead of cutting off its left side.
- README rewritten for any user, with pictures; LICENSE added.

## 2.4.1

- A shorter description.

## 2.4.0

- One cockpit in the bottom panel with nine pages, and a slim sidebar (the project, the state of the server with its buttons, a row for each page).
- Graph page: is the project's knowledge graph fresh, update it, how Claude Code is connected, and the whole dependency tree of the module being edited (Odoo modules read from their manifests).
- Quality and Tools pages; snapshots on Databases; Upgrade and Check on Server.
- `Show the Graph of This Module` and `Audit This Module` commands.
- Fixed: the cockpit ignored the request to show a page added after the first six.

## 2.3.x and earlier

Servers (several at once), databases, remote databases over SSH, translations, fast tests, XPath validation, DB snapshots and quick fixes for the Odoo rules.
