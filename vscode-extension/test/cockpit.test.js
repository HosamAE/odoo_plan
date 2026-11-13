// The cockpit's host side and the rules its page must keep. node test/cockpit.test.js
"use strict";
const Module = require("module");
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const noop = () => {};
const vscodeStub = new Proxy({}, { get: () => new Proxy(function () {}, { get: () => noop, apply: noop, construct: () => ({}) }) });
const realLoad = Module._load;
Module._load = (request, ...rest) => (request === "vscode" ? vscodeStub : realLoad(request, ...rest));
const cockpit = require(path.join(__dirname, "..", "cockpit.js"));
const HERE = path.join(__dirname, "..");

// ---- the level of a log line
assert.strictEqual(cockpit.levelOfLine("2026-11-10 23:15:43,424 45616 WARNING db odoo.tools.convert: x"), "warning");
assert.strictEqual(cockpit.levelOfLine("2026-11-10 23:15:43,424 45616 ERROR db odoo.http: x"), "error");
assert.strictEqual(cockpit.levelOfLine("2026-11-10 23:15:43,424 45616 CRITICAL db odoo.service.server: x"), "error");
assert.strictEqual(cockpit.levelOfLine("2026-11-10 23:15:43,424 45616 INFO db odoo.modules.loading: x"), "info");
assert.strictEqual(cockpit.levelOfLine("  File \"x.py\", line 3, in f"), null, "a traceback line has no level of its own");

// ---- the log lines: numbered, levelled, a traceback taking the colour of its record
const LOG = [
  "2026-11-10 23:00:00,000 1 INFO db a: fine",
  "2026-11-10 23:00:01,000 1 ERROR db odoo.http: Exception during request handling.",
  "Traceback (most recent call last):",
  "  File \"x.py\", line 1, in f",
  "ValueError: boom",
  "2026-11-10 23:00:02,000 1 WARNING db b: careful",
  "2026-11-10 23:00:03,000 1 INFO db c: fine again",
].join("\n") + "\n";
const lines = cockpit.logLines(LOG, null, 400, 1);
assert.deepStrictEqual(lines.map((l) => l.level), ["info", "error", "error", "error", "error", "warning", "info"], "without marks the level is read, and a record's lines share it");
assert.deepStrictEqual(lines.map((l) => l.n), [1, 2, 3, 4, 5, 6, 7], "numbered as the file is");
const marked = cockpit.logLines(LOG, { error: [2, 3, 4, 5], warning: [6] }, 400, 1);
assert.deepStrictEqual(marked.map((l) => l.level), lines.map((l) => l.level), "with the tools' marks the same");
assert.strictEqual(cockpit.logLines(LOG, null, 3, 1).length, 3, "only the last lines");
assert.deepStrictEqual(cockpit.logLines(LOG, null, 3, 1).map((l) => l.n), [5, 6, 7], "with their own numbers");
assert.strictEqual(cockpit.logLines(LOG, null, 400, null)[0].n, null, "a tail of a huge file has no numbers, and does not invent them");
assert.ok(cockpit.logLines("x".repeat(5000), null)[0].text.length < 800, "a very long line is cut");
assert.strictEqual(cockpit.logLines("", null).length, 0);

// ---- what the page gets of a verdict
const verdict = {
  state: "problems", errors: 2, critical: 0, http_500: 1, warnings: 5, hint: "choose a database", databases_failing: ["a", "b"],
  progress: { index: 3, total: 10 }, first_issue: { message: "boom", db: "dbx" }, items: new Array(500).fill(0), marks: { error: [1], warning: [2] },
};
const slim = cockpit.slimHealth(verdict);
assert.strictEqual(slim.items, undefined, "the heavy parts stay behind");
assert.strictEqual(slim.marks, undefined);
assert.deepStrictEqual([slim.state, slim.errors, slim.hint, slim.first_issue.message], ["problems", 2, "choose a database", "boom"]);
assert.strictEqual(cockpit.slimHealth(null), null);

const item = (kind, line, extra) => Object.assign({ kind, line, count: 1, message: `${kind} ${line}`, detail: null, module: null, location: null }, extra);
const many = [item("error", 1, { location: { file: "a.py", line: 9 } })].concat(Array.from({ length: 100 }, (_, i) => item("warning", i + 2)));
const issues = cockpit.slimIssues({ label: "Server", verdict: { items: many } });
assert.deepStrictEqual([issues.errors, issues.warnings, issues.items.length], [1, 100, 60], "counts are the whole, the list is the first sixty");
assert.deepStrictEqual(issues.items[0].location, { file: "a.py", line: 9 });
assert.deepStrictEqual(cockpit.slimIssues(null), { has: false, label: null, errors: 0, warnings: 0, items: [] });

// ---- the whole state
const state = cockpit.buildState({
  project: { name: "Demo", version: "19", root: "/p" }, port: 1900, servers: [{ mine: true, pid: 42, db: "dbx" }], serverRunning: true,
  serverDb: null, health: verdict, source: { label: "Server", log: "/tmp/x.log", verdict: { items: many, marks: { error: [2, 3, 4, 5], warning: [6] } } },
  upgrade: { db: "dbx", what: "all modules", text: "3 of 10", errors: 1, warnings: 2, progress: { index: 3, total: 10 } },
  databases: [{ name: "dbx" }], modulesInfo: null, drift: null, logText: LOG, logFirst: 1, backupDir: "/b", backupBeforeUpgrade: true,
});
assert.deepStrictEqual([state.server.running, state.server.pid, state.server.db], [true, 42, "dbx"], "the database is taken from the process when it was not remembered");
assert.strictEqual(state.upgrade.progress.index, 3);
assert.deepStrictEqual(state.log.lines.map((l) => l.level), ["info", "error", "error", "error", "error", "warning", "info"], "the tools' marks colour the log");
assert.strictEqual(state.backupBeforeUpgrade, true);
const idle = cockpit.buildState({ project: { name: "D", version: "19", root: "/p" }, port: 1900, servers: [], serverRunning: false, source: null, logText: "", logFirst: 1 });
assert.deepStrictEqual([idle.server.running, idle.server.db, idle.databases, idle.upgrade, idle.log.lines.length], [false, null, null, null, 0], "an idle project is drawn too");

// ---- the page
const html = fs.readFileSync(path.join(HERE, "media", "cockpit.html"), "utf8");
const page = cockpit.pageHtml(html);
assert.ok(!page.includes("{{nonce}}"), "the nonce is put in everywhere");
const nonces = [...page.matchAll(/nonce[-=]['"]?([A-Za-z0-9]{32})/g)].map((m) => m[1]);
assert.ok(nonces.length >= 3 && new Set(nonces).size === 1, "one nonce for the policy, the style and the script");
assert.notStrictEqual(cockpit.pageHtml(html), page, "a new nonce every time");
assert.ok(/default-src 'none'/.test(page), "the content-security policy denies by default");
assert.ok(!/<script[^>]*src=/i.test(page) && !/<link[^>]*href=/i.test(page), "nothing is loaded from anywhere");
assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(/.test(html), "log lines are data: the page builds nodes, never markup");
// every colour is the editor's own WITH a fallback: an undefined variable drops the whole declaration
const bare = [...html.matchAll(/var\(--vscode-[\w-]+\)/g)].map((m) => m[0]);
assert.deepStrictEqual(bare, [], `a theme variable without a fallback: ${bare.join(", ")}`);
// every command the page can start by name is one this extension declares (a typo would be a button that does nothing)
const declaredCommands = new Set(JSON.parse(fs.readFileSync(path.join(HERE, "package.json"), "utf8")).contributes.commands.map((c) => c.command));
const usedByPage = [...new Set([...html.matchAll(/"(odooCi\.\w+)"/g)].map((m) => m[1]))];
assert.ok(usedByPage.length >= 25, `the page names its commands (found ${usedByPage.length})`);
assert.deepStrictEqual(usedByPage.filter((id) => !declaredCommands.has(id)), [], "a command the page starts is not declared in package.json");

// ---- what the extension packs is what it uses
const build = fs.readFileSync(path.join(HERE, "build_vsix.py"), "utf8");
const source = fs.readFileSync(path.join(HERE, "extension.js"), "utf8") + fs.readFileSync(path.join(HERE, "cockpit.js"), "utf8");
for (const [, local] of source.matchAll(/require\("\.\/([\w-]+)"\)/g)) {
  assert.ok(build.includes(`"${local}.js"`), `${local}.js is required but not packed (build_vsix.py FILES)`);
}
assert.ok(build.includes('"media/cockpit.html"'), "the cockpit page is packed");
console.log("cockpit OK: log lines numbered and coloured; states built; the page has a nonce policy, builds nodes not markup, every theme colour has a fallback; the package holds what is used");
process.exit(0);
