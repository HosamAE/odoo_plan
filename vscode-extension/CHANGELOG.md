# Changelog

## 2.4.9

- The pictures of the listing (Open VSX, the extension's page in the editor) show: they pointed into the repository root, where they do not exist; they are now full addresses, and the repository entry names the folder of the extension.

## 2.4.8

- The front page of the repository says what the extension does for you and what it needs, and shows it; the Databases picture matches the page.
- A gallery banner colour for the listing of the extension.

## 2.4.7

- Set up arranges the tools inside the version's folder structure; it does not download Odoo or create the venv, and 2.4.6 was wrong to claim it did (that entry is withdrawn). The setup now lists those two as "for you" with their commands, and no longer counts a printed command as "done".

## 2.4.5

- One window, one project, one Odoo version: the "Versions & Environment" view (now "This Odoo Version") shows only the version of the open project and what it needs, no longer all four versions and their projects.
- Every server of the open project that is running gets its own row (port, pid, database); another project's servers are never listed.
- The Databases page hides databases written by another Odoo version, with a button that says how many and shows them.
- The typed-command code is its own file (`terminal_commands.js`, `inputs.js`) and the settings-file editing of `workspace_sync.py` is `jsonc_edit.py`.
- Restoring a snapshot is exact: only `snap_<db>_<label>` is used (a part of a name used to match the first other snapshot and replace the live database by it), database names are quoted as identifiers, and the command asks for confirmation.
- The labels, module names and language codes typed into the editor are checked before they go into a terminal command line.
- The extension declares that it needs a trusted workspace.
- The cockpit no longer sends an unchanged page to the webview every 3 seconds.
- The graph engine reads the module prefix from the project's modules instead of a fixed one.

## 2.4.4

- Nothing about a company or a customer is built into the tools any more: what `odoo-ai workspace` knows about an estate of projects (ids, ports, shared rules, the text of a CLAUDE.md) is read from `tools/local/workspace_local.py` when it exists, and every other project is simply found by its manifests.
- The translation key is passed as `ODOO_TRANSLATE_KEY` (`HX_TRANSLATE_KEY` is still read).
- The editor tests assume no project: `ODOO_CI_TEST_PROJECT` names the one to use.

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
