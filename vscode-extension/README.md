# Cockpit Odoo

**One control room for the Odoo projects on your machine: servers, databases, quality checks, errors, the project's
knowledge graph and its dependency map, tools, and remote databases over SSH.**

![Odoo 17 | 18 | 19 | 20](https://img.shields.io/badge/Odoo-17%20%7C%2018%20%7C%2019%20%7C%2020-714B67?logo=odoo)
![License MIT](https://img.shields.io/badge/License-MIT-blue.svg)

Cockpit Odoo is the face of a set of command-line tools (`odoo-ai`, the knowledge-graph engine, the gates and the lint
rules). Everything it does is also available from a terminal, so the same work runs in CI and from an AI assistant;
the cockpit is where you see it and press it.

## How it is laid out

**The sidebar** (the Cockpit Odoo icon in the Activity Bar) is a short list:

- **The project row** (`Project · Odoo 19`) opens the cockpit.
- **Server** shows whether Odoo is running, the health of its log, an upgrade in progress and the database in use.
  *Start*, *Stop*, *Restart* and *Open in browser* are buttons on that row.
- **Quality**, **Databases**, **Graph**, **Server log**, **Tools & Maintenance** and **Remote servers** each open their
  page of the cockpit. *Quality* carries the live steps of the last run; *Graph* shows whether the graph is fresh.
- **Errors & Warnings** and **Versions & Environment** are views of their own.

**The cockpit** is one view in the bottom panel, beside Terminal and Problems (drag it anywhere VS Code allows). It
opens on a page of cards, each with the buttons used every day, and has one tab for each area:

| Page | What it is for |
| --- | --- |
| **Cockpit** | The state of the project at a glance, and the buttons of every day |
| **Server** | Every Odoo server on the machine, however it was started: follow one, open it, stop it, start another, start with options (database, upgrade, mode), upgrade modules, check a database against the code |
| **Databases** | Every database with size, version, modules and last login; upgrade, back up (also before an upgrade), check against the code, drop; the modules of one database against the code on disk; snapshots and the fast test database |
| **Quality** | Static checks, fast database test, XML and XPath validation, full CI, audit of a module; the phases and steps of the last run, why a step failed, cancel |
| **Log** | The server's log with errors and warnings painted; every line opens the log at that line |
| **Errors & warnings** | The errors and warnings of the running server, tied to your code: press one to open the file at the line |
| **Graph** | The project's knowledge graph: is it fresh, update it, check how Claude Code is connected to it, and the **map of the module you are editing** (below) |
| **Tools** | Setup wizard, scaffold an addon, doctor, translations, secrets, gates, version maintenance |
| **Remote servers** | Databases on a host of your SSH config: pull to a new local database, push, back up, drop |

## The map of the module you are editing

Open a file of any module and the **Graph** page draws the whole tree of what that module depends on, down to `base`,
and the project's modules that need it. It follows you from file to file.

- Modules from Odoo are **hexagons**, Odoo Enterprise ones hexagons with a dashed outline, third-party modules dashed,
  your project's own modules plain rectangles, and the module in the middle has a heavy outline.
- Press any module, also one of Odoo's (`hr_attendance`), and its own whole tree is drawn: a module that is not in your
  project is read from its own `__manifest__.py`, which is read as data and never run. **Back** returns, and the trail
  is shown. **Open manifest** opens the file.
- **Show the Graph of This Module** (Command Palette) brings the page up on the file you are in.
- Limits: the modules that need an Odoo module are the project's only; a side is cut at 400 modules and says so.

## Quick fixes for the Odoo rules

A gate finding such as `R3 len(search(...))` shows in the Problems tab with its rule code. The light bulb offers
*Use search_count()* (only where it is safe) and, for a line that must break a rule on purpose, *add `noqa`*.

## What it needs

The cockpit runs the tools of the version you are working in, from `<root>\odoo<NN>\tools` (the `<NN>.0` branch of the
tools repository), with the project in `<root>\ProjectsOdoo<NN>\<project>` (or `<root>\odoo<NN>\projects\<project>`).
`<root>` is found from where the project is (or the environment variable `ODOO_ROOT`), and is `C:\odoo` on Windows and
`~/odoo` elsewhere when neither says; the setting `odooCi.odooRoot` overrides all of that.

- **First time:** *Odoo Studio: Set up an Odoo Version* clones the tools of a version and lists what you still download
  (*Check only* changes nothing). *Odoo Studio: Run Odoo-AI Doctor Check* says what is missing and how to repair it.
- **A project:** *Quick Project Setup Wizard* asks for the Python, the port and the translation language; the Graph
  page's *Check wiring* shows how a project is connected to its graph and what fixing it would change before it does.
- Windows is where it is tested. The code has paths for Linux and macOS, but they are not verified.

## Settings

| Setting | Meaning |
| --- | --- |
| `odooCi.python` | The Python that runs `odoo-ai` (empty: the interpreter chosen for the workspace, else the version's own venv) |
| `odooCi.cliPath` | The CLI script (empty: the version's `tools\cli.py`) |
| `odooCi.version` | Force an Odoo version instead of reading it from the folder |
| `odooCi.serverPort` | The port to open (empty: the project's `.conf`) |
| `odooCi.odooRoot` | The folder that holds the `odoo<NN>` folders |
| `odooCi.backupDir`, `odooCi.backupBeforeUpgrade` | Where backups go, and whether an upgrade backs up first |
| `odooCi.graph.autoUpdate` | Update the graph quietly when the project opens and it is behind the code |
| `odooCi.graph.followEditor` | The module map follows the file you are editing |
| `odooCi.graph.keepGenerations` | Every graph update leaves an old generation on disk that the engine never removes. `0` (the default) deletes nothing; a number keeps the published one and that many of the newest |
| `odooCi.defaultLang`, `odooCi.i18n.*` | Translation languages and the translator link |
| `odooCi.remoteSudoUser` | The one account a remote host opens to you for its databases |

## Working on the extension

| File | Role |
| --- | --- |
| `extension.js` | Activation, commands, the sidebar, the server and CI runners |
| `cockpit.js` | The state the page draws, and what its buttons ask of the editor |
| `media/cockpit.html` | The page: builds nodes (never markup strings), under a strict content-security policy |
| `graph.js` | The graph page: status, update, wiring, the map of a module |
| `project.js`, `detect.js`, `rulefixes.js` | Which project and version; finding running servers; the quick fixes |
| `remote.js`, `translations.js` | The Remote servers page and the Translations page |

To add a page: add its section and tab to `media/cockpit.html`, its state to `buildState` in `cockpit.js`, and a test.
The tests are plain Node scripts: `node test/<name>.test.js` (the page is checked in a real Chrome when one is found),
and `node test/vscode_host.test.js` starts a real VS Code with the extension under development. A command the page
starts by name must be declared in `package.json`; a test checks it. `build_vsix.py` packs the extension.

---

*Developed and maintained by [HosamAE](https://github.com/HosamAE) · MIT License*
