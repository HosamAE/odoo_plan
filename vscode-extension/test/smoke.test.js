// Loads the extension against a stand-in `vscode` module and walks its trees: node test/smoke.test.js
"use strict";
const Module = require("module");
const assert = require("assert");
const path = require("path");

const registered = { commands: {}, trees: {} };
class TreeItem { constructor(label, state) { this.label = label; this.collapsibleState = state || 0; } }
class ThemeIcon { constructor(id, color) { this.id = id; this.color = color; } }
class EventEmitter { constructor() { this.event = () => {}; } fire() {} }
const noop = () => {};
const vscodeMock = {
  TreeItem, ThemeIcon, EventEmitter,
  ThemeColor: class { constructor(id) { this.id = id; } },
  TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
  StatusBarAlignment: { Left: 1 },
  ConfigurationTarget: { Workspace: 2 },
  Uri: { file: (f) => ({ fsPath: f }) },
  env: { shell: "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" },
  languages: { createDiagnosticCollection: () => ({ clear: noop, set: noop, dispose: noop }) },
  workspace: {
    workspaceFolders: [{ uri: { fsPath: process.env.SMOKE_PROJECT || "C:\odoo\ProjectsOdoo19\Acme" } }],
    workspaceFile: null,
    getConfiguration: () => ({ get: () => undefined, update: noop }),
  },
  window: {
    createOutputChannel: () => ({ appendLine: noop, show: noop, dispose: noop }),
    createStatusBarItem: () => ({ show: noop, dispose: noop }),
    registerTreeDataProvider: (id, provider) => { registered.trees[id] = provider; return { dispose: noop }; },
    showErrorMessage: noop, showWarningMessage: noop, createTerminal: () => ({ show: noop, sendText: noop }),
  },
  commands: { registerCommand: (id, fn) => { registered.commands[id] = fn; return { dispose: noop }; }, executeCommand: noop },
};
const realLoad = Module._load;
Module._load = (request, ...rest) => (request === "vscode" ? vscodeMock : realLoad(request, ...rest));

const ext = require(path.join(__dirname, "..", "extension.js"));
const subscriptions = [];
ext.activate({ subscriptions });

const pkg = require(path.join(__dirname, "..", "package.json"));
for (const c of pkg.contributes.commands) assert.ok(registered.commands[c.command], `command not registered: ${c.command}`);
for (const id of Object.keys(registered.commands)) {
  if (id.startsWith("odooCi.")) assert.ok(pkg.contributes.commands.some((c) => c.command === id) || id === "odooCi.steps.focus", `registered but not in package.json: ${id}`);
}
for (const v of pkg.contributes.views.odooStudio) assert.ok(registered.trees[v.id], `no tree for view ${v.id}`);

const env = registered.trees["odooCi.env"];
const roots = env.getChildren();
assert.deepStrictEqual(roots.slice(0, 4).map((r) => r.label.replace("  (this window)", "")), ["Odoo 17", "Odoo 18", "Odoo 19", "Odoo 20"]);
assert.strictEqual(roots[roots.length - 1].label, "Maintenance");
const maintenance = env.getChildren(roots[roots.length - 1]);
assert.ok(maintenance.length >= 7 && maintenance.every((m) => m.command && registered.commands[m.command.command]), "maintenance items must call registered commands");
const v19 = roots.find((r) => r.label.startsWith("Odoo 19"));
assert.ok(v19.label.includes("this window"), "the window's own version is marked");
assert.ok(env.getChildren(v19).length >= 3, "a version shows tools/source/venv");
console.log(`smoke OK: ${Object.keys(registered.commands).length} commands, ${Object.keys(registered.trees).length} views, ${roots.length - 1} versions listed`);
subscriptions.forEach((s) => s.dispose && s.dispose());
process.exit(0);
