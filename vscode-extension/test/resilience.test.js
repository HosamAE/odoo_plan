// The extension must not die because the editor refuses one of its registrations.
//
// 2026-11-11: the extension was updated to a version with a new view, the window was not reloaded, and the editor
// answered "No view is registered with id: odooCi.issues". The activation threw, and everything registered after
// that line - every command - was gone. Now the refusal is caught, the rest is registered, and the person is told
// to reload the window, in words. node test/resilience.test.js
"use strict";
const Module = require("module");
const assert = require("assert");
const path = require("path");

const registered = { commands: {}, trees: {} };
const warnings = [];
const statusItems = [];
class TreeItem { constructor(label, state) { this.label = label; this.collapsibleState = state || 0; } }
class ThemeIcon { constructor(id, color) { this.id = id; this.color = color; } }
class EventEmitter { constructor() { this.event = () => {}; } fire() {} }
const noop = () => {};

function makeVscode(refusedViews) {
  return {
    TreeItem, ThemeIcon, EventEmitter,
    MarkdownString: class { appendText() { return this; } },
    OverviewRulerLane: { Full: 7 },
    ThemeColor: class { constructor(id) { this.id = id; } },
    TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
    StatusBarAlignment: { Left: 1 },
    ConfigurationTarget: { Workspace: 2 },
    Uri: { file: (f) => ({ fsPath: f }) },
    env: { shell: "powershell.exe" },
    CodeAction: class {}, CodeActionKind: { QuickFix: "quickfix" }, WorkspaceEdit: class {},
    languages: { createDiagnosticCollection: () => ({ clear: noop, set: noop, dispose: noop }), registerCodeActionsProvider: () => ({ dispose: noop }) },
    workspace: { workspaceFolders: [{ uri: { fsPath: process.env.SMOKE_PROJECT || "C:\\odoo\\ProjectsOdoo19\\Acme" } }], workspaceFile: null, getConfiguration: () => ({ get: () => undefined, update: noop }) },
    window: {
      createOutputChannel: () => ({ appendLine: noop, append: noop, show: noop, clear: noop, dispose: noop }),
      createStatusBarItem: () => { const item = { show: () => { item.shown = true; }, hide: noop, dispose: noop }; statusItems.push(item); return item; },
      registerTreeDataProvider: (id, provider) => {
        if (refusedViews.includes(id)) throw new Error(`No view is registered with id: ${id}`);
        registered.trees[id] = provider;
        return { dispose: noop };
      },
      onDidChangeVisibleTextEditors: () => ({ dispose: noop }), visibleTextEditors: [],
      createTextEditorDecorationType: () => ({ dispose: noop }),
      showErrorMessage: noop, showWarningMessage: (text, ...buttons) => { warnings.push({ text, buttons }); return { then: noop }; },
      createTerminal: () => ({ show: noop, sendText: noop }),
    },
    commands: { registerCommand: (id, fn) => { registered.commands[id] = fn; return { dispose: noop }; }, executeCommand: noop },
  };
}

let current = makeVscode(["odooCi.issues"]);
const realLoad = Module._load;
Module._load = (request, ...rest) => (request === "vscode" ? current : realLoad(request, ...rest));
const ext = require(path.join(__dirname, "..", "extension.js"));

const subscriptions = [];
assert.doesNotThrow(() => ext.activate({ subscriptions }), "a refused view must not stop the activation");
assert.ok(registered.trees["odooCi.actions"] && registered.trees["odooCi.env"], "the views the window does know are registered");
assert.ok(!registered.trees["odooCi.issues"], "the refused one is not");
for (const command of ["odooCi.serverRun", "odooCi.serverRunQuick", "odooCi.serverUpgrade", "odooCi.serverDrift", "odooCi.issueNext", "odooCi.issuePick", "odooCi.static"]) {
  assert.ok(registered.commands[command], `${command} is still registered, after the refused view`);
}
// no pop-up: a quiet status-bar item says what to do, and one click reloads the window
assert.strictEqual(warnings.length, 0, "the person is not interrupted by a pop-up");
const reload = statusItems.find((i) => i.shown && /Reload/i.test(i.text || ""));
assert.ok(reload, "a status-bar item offers the reload");
assert.strictEqual(reload.command, "workbench.action.reloadWindow", "and one click does it");
console.log("resilience OK: a refused view is caught, every command is still registered, and a quiet status-bar item offers the reload");
subscriptions.forEach((s) => s.dispose && s.dispose());
process.exit(0);
