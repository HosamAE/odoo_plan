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
  CodeAction: class { constructor(title, kind) { this.title = title; this.kind = kind; } },
  CodeActionKind: { QuickFix: "quickfix" },
  WorkspaceEdit: class { replace() {} insert() {} },
  languages: {
    createDiagnosticCollection: () => ({ clear: noop, set: noop, dispose: noop }),
    registerCodeActionsProvider: (selector, provider) => { registered.codeActions = provider; return { dispose: noop }; },
  },
  workspace: {
    workspaceFolders: [{ uri: { fsPath: process.env.SMOKE_PROJECT || "C:\odoo\ProjectsOdoo19\Acme" } }],
    workspaceFile: null,
    getConfiguration: () => ({ get: () => undefined, update: noop }),
  },
  window: {
    createOutputChannel: () => ({ appendLine: noop, show: noop, dispose: noop }),
    createStatusBarItem: () => ({ show: noop, hide: noop, dispose: noop }),
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
// R3 quick fix: only where it is safe
const { fixLenSearch } = ext._test;
assert.strictEqual(fixLenSearch("n = len(self.env['x'].search([('a', '=', 1)]))"), "n = self.env['x'].search_count([('a', '=', 1)])");
assert.strictEqual(fixLenSearch("if len(rec.search([], limit=3)) > 1:"), "if rec.search_count([], limit=3) > 1:");
assert.strictEqual(fixLenSearch("n = len(rec.search([], order='id'))"), null, "search_count has no order");
assert.strictEqual(fixLenSearch("n = len(rec.mapped('x'))"), null, "not a search");
assert.strictEqual(fixLenSearch("n = len(self.search([]).filtered(lambda r: r.a))"), null, "search is not the whole argument");
assert.ok(registered.codeActions, "the quick-fix provider is registered");
const doc = { uri: "u", languageId: "python", lineAt: () => ({ text: "n = len(rec.search([]))", range: {} }) };
const diag = { source: "Odoo CI", code: "R3", range: { start: { line: 0 } } };
const actions = registered.codeActions.provideCodeActions(doc, null, { diagnostics: [diag, { source: "other", code: "R3" }] });
assert.deepStrictEqual(actions.map((a) => a.title), ["Use search_count()", "This line must break R3: add noqa"]);
// one control panel: the CI progress lives in it, the old separate view is gone
assert.ok(!pkg.contributes.views.odooStudio.some((v) => v.id === "odooCi.steps"), "the separate CI view must be gone");
assert.ok(!registered.trees["odooCi.steps"], "no tree registered for the removed view");
const panel = registered.trees["odooCi.actions"];
const groups = panel.getChildren();
assert.deepStrictEqual(groups.map((g) => g.label.replace(/ \(Port \d+\)/, "")), ["Server", "Quality & CI Gates", "Developer Tools"]);
const ci = panel.getChildren(groups[1]).map((i) => i.label);
assert.ok(ci.includes("Run Static Checks (Lint & Security)") && ci.includes("Run Full CI Suite (Clean DB)"), "CI buttons stay in the panel");
const server = panel.getChildren(groups[0]);
assert.ok(server.some((i) => /^Server: (Running|Stopped)$/.test(i.label)), "the server state is shown");
assert.ok(!JSON.stringify(pkg.contributes.menus).includes("odooCi.steps"), "no menu entry may target the removed view");
assert.ok(pkg.contributes.commands.every((c) => c.icon !== "$(camera)"), "$(camera) is not a VS Code icon");

// finding a server however it was started
const { parseOdooCommand, splitCommandLine } = ext._test;
const B = "\\";   // one backslash
const project19 = { root: ["C:", "odoo", "ProjectsOdoo19", "Acme"].join(B), name: "Acme", version: "19" };
const win = (...parts) => ["C:", ...parts].join(B);
assert.deepStrictEqual(
  splitCommandLine(`"${win("a b", "python.exe")}" odoo-bin -c "${win("x y", "z.conf")}"`),
  [win("a b", "python.exe"), "odoo-bin", "-c", win("x y", "z.conf")]);
const python = win("odoo", "odoo19", "venv", "Scripts", "python.exe");
const bin = win("odoo", "odoo19", "odoo", "odoo-bin");
const conf = win("odoo", "ProjectsOdoo19", "Acme", "Acme.conf");
let hit = parseOdooCommand(`"${python}" ${bin} -c ${conf} --http-port=1900 --dev=all`, project19);
assert.deepStrictEqual([hit.port, hit.mine], [1900, true], "started from a terminal, a task or the debugger: the same");
hit = parseOdooCommand(`python odoo-bin --config=${win("other", "Other.conf")} -p 8070`, project19);
assert.deepStrictEqual([hit.port, hit.mine], [8070, false], "another project's server is reported as another");
hit = parseOdooCommand(`python ${win("somewhere", "odoo-bin")} --addons-path=${project19.root},${win("odoo", "odoo19", "odoo", "addons")} --http-port 8071`, project19);
assert.deepStrictEqual([hit.port, hit.mine], [8071, true], "no --config, but the addons path is this project");
assert.strictEqual(parseOdooCommand("python odoo-bin", project19).port, 8069, "Odoo's default port");
assert.strictEqual(parseOdooCommand("python odoo-bin -u my_module --stop-after-init", project19), null, "an update is not a server");
assert.strictEqual(parseOdooCommand("python odoo-bin shell -d db", project19), null, "a shell is not a server");
assert.strictEqual(parseOdooCommand("python -m odoo_kg serve --config x.toml", project19), null, "the graph server is not Odoo");
assert.strictEqual(parseOdooCommand("python -m http.server 8069", project19), null, "not Odoo at all");
console.log(`smoke OK: ${Object.keys(registered.commands).length} commands, ${Object.keys(registered.trees).length} views, ${roots.length - 1} versions listed`);
subscriptions.forEach((s) => s.dispose && s.dispose());
process.exit(0);
