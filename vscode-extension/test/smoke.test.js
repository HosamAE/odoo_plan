// Loads the extension against a stand-in `vscode` module and walks its trees: node test/smoke.test.js
"use strict";
const Module = require("module");
const assert = require("assert");
const path = require("path");

const registered = { commands: {}, trees: {}, views: {} };
class TreeItem { constructor(label, state) { this.label = label; this.collapsibleState = state || 0; } }
class ThemeIcon { constructor(id, color) { this.id = id; this.color = color; } }
class EventEmitter { constructor() { this.event = () => {}; } fire() {} }
const noop = () => {};
const vscodeMock = {
  TreeItem, ThemeIcon, EventEmitter,
  MarkdownString: class { appendText() { return this; } },
  OverviewRulerLane: { Full: 7 },
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
    workspaceFolders: [{ uri: { fsPath: process.env.SMOKE_PROJECT || ["C:", "odoo", "ProjectsOdoo19", "Demo"].join("\\") } }],
    workspaceFile: null,
    getConfiguration: () => ({ get: () => undefined, update: noop }),
  },
  window: {
    createOutputChannel: () => ({ appendLine: noop, show: noop, dispose: noop }),
    createStatusBarItem: () => ({ show: noop, hide: noop, dispose: noop }),
    registerTreeDataProvider: (id, provider) => { registered.trees[id] = provider; return { dispose: noop }; },
    registerWebviewViewProvider: (id, provider) => { registered.views[id] = provider; return { dispose: noop }; },
    onDidChangeVisibleTextEditors: () => ({ dispose: noop }), visibleTextEditors: [],
    onDidChangeActiveTextEditor: () => ({ dispose: noop }), activeTextEditor: undefined,
    createTextEditorDecorationType: () => ({ dispose: noop }),
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
for (const v of pkg.contributes.views.odooCockpit) assert.ok(registered.views[v.id], `no provider for the bottom view ${v.id}`);
assert.ok(pkg.contributes.viewsContainers.panel.some((c) => c.id === "odooCockpit"), "the cockpit has a container in the bottom panel");

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
const rows = panel.getChildren();
assert.deepStrictEqual(rows.map((r) => r.label.replace(/ \(Port \d+\)/, "")),
  ["Demo · Odoo 19", "Server", "Quality", "Databases", "Graph", "Server log", "Tools & Maintenance", "Remote servers"], "a slim sidebar: the project, then one row for each page of the cockpit");
assert.strictEqual(rows[0].command.command, "odooCi.cockpitPanel", "the project row opens the cockpit");
const pageOf = { Databases: "dbs", Quality: "quality", Graph: "graph", "Server log": "log", "Tools & Maintenance": "tools", "Remote servers": "remote" };
for (const row of rows.filter((r) => pageOf[r.label])) {
  assert.deepStrictEqual([row.command.command, row.command.arguments], ["odooCi.cockpitTab", [pageOf[row.label]]], `${row.label} opens its page of the cockpit`);
  assert.ok(registered.commands["odooCi.cockpitTab"], "and that command is registered");
}
assert.strictEqual(rows.find((r) => r.label === "Quality").contextValue, "quality");
assert.strictEqual(rows.find((r) => r.label === "Graph").contextValue, "graph");
const server = panel.getChildren(rows[1]);
assert.ok(server.some((i) => /^Server: (Running|Stopped)$/.test(i.label)), "the server state is shown");
assert.ok(server.every((i) => !/^(Start|Stop|Upgrade|Check|Open in)/.test(i.label)), "the buttons of the server are inline or on the cockpit's Server page, not rows again");
const statusRow = server.find((i) => /^Server: /.test(i.label));
assert.ok(["server-running", "server-stopped"].includes(statusRow.contextValue), "the state row says which buttons it takes");
// every inline button belongs to a row that exists, and starts a command that is declared and registered
const rowKinds = new Set(["server-running", "server-stopped", "quality", "graph", "issue", "issueWithSource"]);
for (const m of pkg.contributes.menus["view/item/context"]) {
  assert.ok(pkg.contributes.commands.some((c) => c.command === m.command) && registered.commands[m.command], `inline command ${m.command} is declared and registered`);
  const kind = (m.when.match(/viewItem (?:==|=~) \/?\^?([\w-]+)/) || [])[1];
  assert.ok(!kind || [...rowKinds].some((k) => k.startsWith(kind)), `inline button for a row that exists: ${m.when}`);
}
assert.ok(!JSON.stringify(pkg.contributes.menus).includes("odooCi.steps"), "no menu entry may target the removed view");
assert.ok(pkg.contributes.commands.every((c) => c.icon !== "$(camera)"), "$(camera) is not a VS Code icon");

// finding a server however it was started
const { parseOdooCommand, splitCommandLine } = ext._test;
const B = "\\";   // one backslash
const project19 = { root: ["C:", "odoo", "ProjectsOdoo19", "Demo"].join(B), name: "Demo", version: "19" };
const win = (...parts) => ["C:", ...parts].join(B);
assert.deepStrictEqual(
  splitCommandLine(`"${win("a b", "python.exe")}" odoo-bin -c "${win("x y", "z.conf")}"`),
  [win("a b", "python.exe"), "odoo-bin", "-c", win("x y", "z.conf")]);
const python = win("odoo", "odoo19", "venv", "Scripts", "python.exe");
const bin = win("odoo", "odoo19", "odoo", "odoo-bin");
const conf = win("odoo", "ProjectsOdoo19", "Demo", "Demo.conf");
let hit = parseOdooCommand(`"${python}" ${bin} -c ${conf} --http-port=1900 --dev=all`, project19);
assert.deepStrictEqual([hit.port, hit.mine], [1900, true], "started from a terminal, a task or the debugger: the same");
hit = parseOdooCommand(`python odoo-bin --config=${win("other", "Other.conf")} -p 8070`, project19);
assert.deepStrictEqual([hit.port, hit.mine], [8070, false], "another project's server is reported as another");
hit = parseOdooCommand(`python ${win("somewhere", "odoo-bin")} --addons-path=${project19.root},${win("odoo", "odoo19", "odoo", "addons")} --http-port 8071`, project19);
assert.deepStrictEqual([hit.port, hit.mine], [8071, true], "no --config, but the addons path is this project");
assert.strictEqual(parseOdooCommand("python odoo-bin", project19).port, 8069, "Odoo's default port");
// the Cockpit's own helper (the drift check) carries odoo-bin as the VALUE of --odoo-bin: it is not a server. In the
// real window it appeared as "a server on :8069" for as long as the check ran, with a Stop button that would have ended it.
const helper = `"${python}" ${win("odoo", "odoo19", "tools", "cli.py")} server drift --conf ${conf} --python ${python} --odoo-bin ${bin} -d dev_db`;
assert.strictEqual(parseOdooCommand(helper, project19), null, "a helper that is handed odoo-bin is not a server");
assert.strictEqual(parseOdooCommand("python odoo-bin -u my_module --stop-after-init", project19), null, "an update is not a server");
assert.strictEqual(parseOdooCommand("python odoo-bin shell -d db", project19), null, "a shell is not a server");
assert.strictEqual(parseOdooCommand("python -m odoo_kg serve --config x.toml", project19), null, "the graph server is not Odoo");
assert.strictEqual(parseOdooCommand("python -m http.server 8069", project19), null, "not Odoo at all");
assert.strictEqual(parseOdooCommand(`cmd.exe /c python odoo-bin -c ${conf} --http-port=1900`, project19), null, "a shell that mentions odoo-bin is not a server");
assert.strictEqual(parseOdooCommand(`bash -c "python odoo-bin --http-port=1900"`, project19), null, "neither is bash");
assert.strictEqual(parseOdooCommand(`${win("Users", "me", "Code", "Code.exe")} ${bin}`, project19), null, "nor an editor with odoo-bin open");
assert.strictEqual(parseOdooCommand(`"${python}" ${bin} -c ${conf} --http-port=1900`, project19).port, 1900, "a venv python is a server");
assert.strictEqual(parseOdooCommand(`python3.12 odoo-bin --http-port=1901`, project19).port, 1901, "python3.12 is a server");
// ---- the errors and warnings view: a list to walk through, a filter, and the progress line
{
  const { IssuesProvider, setIssuesSource, visibleIssues, occurrences, issueFilter, issueCursor, progressLine, duration, shortText } = ext._test.issues;
  const tree = registered.trees["odooCi.issues"];
  assert.ok(tree, "the Errors & Warnings view is registered");
  const empty = tree.getChildren();
  assert.strictEqual(empty.length, 1, "with no log yet it says so, once");
  assert.ok(String(empty[0].label).includes("No server log"), "and says what to do");

  const item = (kind, line, extra) => Object.assign({ kind, line, lines: [line], count: 1, db: "dbx", message: `${kind} at ${line}`, detail: null, module: null, location: null }, extra);
  const verdict = {
    state: "problems",
    items: [item("error", 10, { detail: "ValueError: boom", location: { file: "C:/p/a.py", line: 40 } }), item("warning", 2, { module: "mod_a" }),
      item("warning", 7, { count: 2, lines: [7, 8], module: "mod_b", db: "other" })],
    marks: { error: [10], warning: [2, 7, 8] }, progress: null,
  };
  setIssuesSource("Server", "C:/tmp/x.log", verdict);
  const groups = tree.getChildren();
  assert.deepStrictEqual(groups.map((g) => String(g.label)).slice(1), ["Errors (1)", "Warnings (2)"], "grouped, errors first");
  const warnings = tree.getChildren(groups[2]);
  assert.strictEqual(warnings.length, 2);
  assert.ok(String(warnings[1].description).includes("×2") && String(warnings[1].description).includes("line 7"), "a repeated warning says how often and where");
  assert.strictEqual(warnings[0].command.command, "odooCi.issueOpen", "a click opens it");
  assert.strictEqual(tree.getChildren(groups[1])[0].contextValue, "issueWithSource", "an error that ends in your code offers the code");
  assert.strictEqual(tree.getChildren(groups[2])[0].contextValue, "issue", "one that does not offers only the log");

  // walking through: errors first, then warnings, one stop per line they happened on
  assert.deepStrictEqual(occurrences().map((o) => o.line), [10, 2, 7, 8]);
  issueFilter.kind = "warning";
  assert.deepStrictEqual(visibleIssues().map((i) => i.line), [2, 7], "the filter narrows the list");
  assert.deepStrictEqual(occurrences().map((o) => o.line), [2, 7, 8]);
  issueFilter.kind = "all";
  issueFilter.db = "other";
  assert.deepStrictEqual(visibleIssues().map((i) => i.line), [7], "by database");
  issueFilter.db = null;
  issueFilter.module = "mod_a";
  assert.deepStrictEqual(visibleIssues().map((i) => i.line), [2], "by module");
  issueFilter.module = null;
  assert.strictEqual(issueCursor.at, -1);

  // how far it has got
  assert.strictEqual(duration(75), "1m 15s");
  assert.strictEqual(duration(9), "9s");
  assert.strictEqual(shortText("<b>Access</b>  <br/>Rights   x", 40), "Access Rights x", "markup and spaces are flattened");
  assert.strictEqual(progressLine({ index: 120, total: 324, percent: 37, module: "website", file: "views/a.xml", eta_s: 70, finished: false }),
    "120 of 324 modules (37%) · website/views/a.xml · about 1m 10s left");
  assert.strictEqual(progressLine({ index: 324, total: 324, finished: true, seconds: 121.26, updated: 301 }), "324 modules loaded in 2m 01s, 301 upgraded");
  assert.strictEqual(progressLine(null), "");
  setIssuesSource("Server", null, null);
}

console.log(`smoke OK: ${Object.keys(registered.commands).length} commands, ${Object.keys(registered.trees).length} views, ${roots.length - 1} versions listed`);
subscriptions.forEach((s) => s.dispose && s.dispose());
process.exit(0);
