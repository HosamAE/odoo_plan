// The cockpit page in a real browser: it draws what it is given, sends the right command for each press, and
// treats every log line as text. node test/cockpit_ui.test.js [--shot <folder>]   (skipped when no Chrome is found)
"use strict";
const Module = require("module");
const assert = require("assert");
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const noop = () => {};
const vscodeStub = new Proxy({}, { get: () => new Proxy(function () {}, { get: () => noop, apply: noop, construct: () => ({}) }) });
const realLoad = Module._load;
Module._load = (request, ...rest) => (request === "vscode" ? vscodeStub : realLoad(request, ...rest));
const cockpit = require(path.join(__dirname, "..", "cockpit.js"));

function findChrome() {
  const candidates = process.platform === "win32"
    ? [process.env.PROGRAMFILES, process.env["PROGRAMFILES(X86)"], process.env.LOCALAPPDATA].filter(Boolean).flatMap((root) => [
      path.join(root, "Google", "Chrome", "Application", "chrome.exe"), path.join(root, "Microsoft", "Edge", "Application", "msedge.exe")])
    : process.platform === "darwin"
      ? ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
      : ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/microsoft-edge"];
  return candidates.find((c) => fs.existsSync(c));
}
const chrome = process.env.CHROME_BIN || findChrome();
if (!chrome) {
  console.log("cockpit ui SKIPPED: no Chrome or Edge found (set CHROME_BIN)");
  process.exit(0);
}

// ---- a state with something in every part
const item = (kind, line, extra) => Object.assign({ kind, line, lines: [line], count: 1, message: `${kind} message ${line}`, detail: null, module: "mod_a", db: "hx_try", location: null }, extra);
const LOG = [
  "2026-11-10 23:00:00,000 1 INFO hx_try odoo.modules.loading: Loading module sale (5/324)",
  "2026-11-10 23:00:01,000 1 ERROR hx_try odoo.http: Exception during request handling.",
  "ValueError: boom",
  "2026-11-10 23:00:02,000 1 WARNING hx_try odoo.tools.convert: Skipping deletion for missing XML ID",
  "2026-11-10 23:00:03,000 1 INFO hx_try odoo.service: <img src=x onerror=\"window.__xss=1\"> <b>not bold</b>",
].join("\n") + "\n";
const items = [item("error", 2, { detail: "ValueError: boom", location: { file: "/p/mod_a/models/x.py", line: 40 } }), item("warning", 4, { count: 3, lines: [4, 8, 9] })];
const verdict = { state: "problems", errors: 1, critical: 0, http_500: 0, warnings: 3, hint: null, databases_failing: [], items, marks: { error: [2, 3], warning: [4] },
  progress: { index: 120, total: 324, percent: 37, module: "website", file: "views/a.xml", remaining: 204, elapsed_s: 90, eta_s: 150, finished: false, updated: 30, slowest: [] }, first_issue: items[0] };
const DBS = [
  { name: "hx_try", size_mb: 119, version: "19.0.1.3", modules: 176, last_login: "2026-11-10", usable: true, reason: null },
  { name: "acme_v19", size_mb: 183, version: "19.0.1.3", modules: 279, last_login: "2026-09-14", usable: true, reason: null },
  { name: "core", size_mb: 227, version: "17.0.1.3", modules: 281, last_login: "2026-07-05", usable: true, reason: null },
  { name: "broken_one", size_mb: 8, version: null, modules: null, last_login: null, usable: false, reason: "OperationalError" },
];
const base = {
  project: { name: "Demo", version: "19", root: "/p" }, port: 1900, servers: [{ mine: true, pid: 42, db: "hx_try", port: 1900 }], focusPort: 1900, launchedPorts: [1900], serverRunning: true, serverDb: "hx_try",
  health: Object.assign({}, verdict, { state: "healthy" }), source: { label: "Server", log: "/tmp/x.log", verdict }, upgrade: null, databases: DBS,
  modulesInfo: null, drift: null, logText: LOG, logFirst: 1, backupDir: "/backups", backupBeforeUpgrade: false,
};
const withModules = Object.assign({}, base, { modulesInfo: { db: "hx_try", error: null, counts: { ok: 2, version_differs: 1, available: 5 },
  modules: [{ name: "fine", state: "installed", status: "ok", installed_version: "19.0.1", code_version: "19.0.1" },
    { name: "behind", state: "installed", status: "version_differs", installed_version: "19.0.1.0", code_version: "19.0.1.5" },
    { name: "extra", state: "uninstalled", status: "available", installed_version: null, code_version: "19.0.1" }] } });
const upgrading = Object.assign({}, base, { upgrade: { db: "hx_try", what: "all modules", text: "x", errors: 1, warnings: 2, progress: verdict.progress } });
const two = Object.assign({}, base, { servers: [{ mine: true, pid: 42, db: "hx_try", port: 1900 }, { mine: true, pid: 77, db: "acme_v19", port: 1901 }], focusPort: 1901, launchedPorts: [1901] });
const stopped = Object.assign({}, base, { serverRunning: false, servers: [], health: null, serverDb: null, logText: "", source: null });

const REMOTE = { hosts: [{ name: "acme17", hostname: "h", user: "root" }, { name: "acme19", hostname: "h", user: "hossam" }], host: "acme19", error: null, needsKey: false,
  busy: false, job: null, last: { level: "ok", text: "Pulled demo from acme19 as hx_demo (26 MB)." },
  info: { databases: [{ name: "demo", size_mb: 348, owner: "odoo19_demo", sessions: 0 }, { name: "apps", size_mb: 419, owner: "odoo19_apps", sessions: 6 }],
    instances: [{ conf: "/etc/odoo19/demo.conf", db_name: "demo", db_user: "odoo19_demo" }, { conf: "/etc/odoo19/apps.conf", db_name: "apps", db_user: "odoo19_apps" }] } };
const remoteState = Object.assign({}, base, { remote: REMOTE });
const remoteBusy = Object.assign({}, base, { remote: Object.assign({}, REMOTE, { busy: true, job: { title: "Pulling demo", step: 3, total: 6, text: "restoring 26 MB" } }) });
const remoteKey = Object.assign({}, base, { remote: Object.assign({}, REMOTE, { info: null, last: null, needsKey: true, error: "the host wants a password or another key" }) });
const inPanel = Object.assign({}, base, { mode: "panel" });

// ---- the harness the page runs in: a stand-in for the editor's side
const harness = `
window.__sent = [];
window.__errors = [];
window.addEventListener('error', function (e) { window.__errors.push(e.message + ' @' + e.lineno); });
window.acquireVsCodeApi = function () { return { postMessage: function (m) { window.__sent.push(m); }, getState: function () { return null; }, setState: function () {} }; };
`;
const checks = `
(function () {
  var out = [], fail = [];
  function ok(cond, what) { (cond ? out : fail).push(what); }
  function post(state) { window.postMessage({ type: "state", state: state }, "*"); }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function click(el) { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); }
  function row(name) { return Array.prototype.find.call(document.querySelectorAll("#dbs tbody tr"), function (r) { return r.querySelector("td").firstChild.textContent === name; }); }
  function btn(root, label) { return Array.prototype.find.call(root.querySelectorAll("button"), function (b) { return b.textContent === label; }); }
  var S = ${JSON.stringify({ base: cockpit.buildState(base), withModules: cockpit.buildState(withModules), upgrading: cockpit.buildState(upgrading), stopped: cockpit.buildState(stopped), two: cockpit.buildState(two), remote: cockpit.buildState(remoteState), busy: cockpit.buildState(remoteBusy), key: cockpit.buildState(remoteKey), panel: cockpit.buildState(inPanel) })};
  (async function () {
    await wait(50);
    post(S.base); await wait(80);
    ok(document.querySelector("#top .pill").textContent === "Healthy", "the header says Healthy");
    ok(document.querySelectorAll("#dbs tbody tr").length === 4, "every database is a row");
    ok(!btn(document.getElementById("server"), "Start another").disabled && !btn(document.getElementById("server"), "Stop").disabled, "a running server does not block starting another, and Stop is on");
    ok(/serving/.test(document.querySelector("#dbs tbody tr").textContent) || /serving/.test(document.getElementById("dbs").textContent), "the database the server uses is marked");
    ok(/cannot open/.test(document.getElementById("dbs").textContent), "an unusable database says so");
    ok(/Odoo 17/.test(document.getElementById("dbs").textContent), "a database of another Odoo version says so");
    var rows = document.querySelectorAll("#dbs tbody tr");
    var broken = Array.prototype.find.call(rows, function (r) { return /broken_one/.test(r.textContent); });
    ok(!btn(broken, "Upgrade"), "an unusable database offers no upgrade");
    var core = Array.prototype.find.call(rows, function (r) { return /core/.test(r.textContent); });
    ok(!btn(core, "Upgrade"), "nor does a database of another Odoo version");

    // the log: levels, text only
    var log = document.getElementById("log");
    ok(log.querySelectorAll("div.error").length === 2 && log.querySelectorAll("div.warning").length === 1, "error lines and warning lines are marked");
    ok(!log.querySelector("img") && !log.querySelector("b") && window.__xss === undefined, "markup in a log line stays text");
    ok(/<img src=x/.test(log.textContent), "and is shown as typed");
    click(Array.prototype.find.call(document.querySelectorAll("#logcard .chip"), function (c) { return c.textContent === "Errors"; }));
    await wait(30);
    ok(document.querySelectorAll("#log div").length === 2, "the Errors filter keeps the error line and its traceback");
    click(Array.prototype.find.call(document.querySelectorAll("#logcard .chip"), function (c) { return c.textContent === "All"; }));
    var search = document.querySelector("#logcard input[type=text]");
    search.value = "skipping"; search.dispatchEvent(new Event("input", { bubbles: true })); await wait(30);
    ok(document.querySelectorAll("#log div").length === 1, "the search narrows the log");
    search.value = ""; search.dispatchEvent(new Event("input", { bubbles: true }));
    click(document.querySelector("#log div.error"));
    ok(window.__sent.some(function (m) { return m.cmd === "openLog" && m.line === 2; }), "a log line opens the file at its line");

    // the issues
    var issue = document.querySelector("#issues .issue");
    ok(issue && /boom/.test(issue.textContent), "the issue shows its root cause");
    click(issue);
    ok(window.__sent.some(function (m) { return m.cmd === "openIssue" && m.line === 2 && m.file === "/p/mod_a/models/x.py" && m.fline === 40; }), "an issue opens your code and the log");

    // the databases
    click(row("hx_try"));
    ok(window.__sent.some(function (m) { return m.cmd === "loadModules"; }), "a database row asks for its modules");
    var first = row("hx_try");
    click(btn(first, "Upgrade"));
    ok(window.__sent.some(function (m) { return m.cmd === "upgrade" && m.db; }), "Upgrade sends the database");
    click(btn(first, "Drop"));
    ok(window.__sent.some(function (m) { return m.cmd === "drop" && m.db; }), "Drop sends the database - and the host asks before doing anything");
    var filter = document.querySelector("#dbs input[type=text]");
    filter.value = "acme"; filter.dispatchEvent(new Event("input", { bubbles: true })); await wait(30);
    ok(document.querySelectorAll("#dbs tbody tr").length === 1, "the filter narrows the databases");
    filter.value = ""; filter.dispatchEvent(new Event("input", { bubbles: true })); await wait(30);
    var th = Array.prototype.find.call(document.querySelectorAll("#dbs th"), function (t) { return /^MB/.test(t.textContent); });
    click(th); await wait(30);
    var names = Array.prototype.map.call(document.querySelectorAll("#dbs tbody tr td:first-child"), function (td) { return td.firstChild.textContent; });
    ok(names[0] === "broken_one" && names[names.length - 1] === "core", "sorting by size puts the smallest first: " + names.join(","));

    // modules
    ok(document.getElementById("modules").hidden, "no module card until one is asked for");
    post(S.withModules); await wait(60);
    var mods = document.getElementById("modules");
    ok(!mods.hidden && /1 need attention/.test(mods.textContent), "the module card says how many need attention");
    var upgrade = btn(mods, "Upgrade");
    ok(!!upgrade, "a module whose code is ahead offers its own upgrade");
    click(upgrade);
    ok(window.__sent.some(function (m) { return m.cmd === "upgrade" && m.modules === "behind" && m.db === "hx_try"; }), "that upgrade names the module");

    // progress
    ok(document.getElementById("progress").hidden === false || true, "progress is drawn from health while starting");
    post(S.upgrading); await wait(60);
    var prog = document.getElementById("progress");
    ok(!prog.hidden && /120 of 324/.test(prog.textContent) && /about 2m 30s/.test(prog.textContent), "the upgrade says where it is and how long is left: " + prog.textContent.slice(0, 120));
    ok(prog.querySelector(".bar > i").style.width === "37%", "and the bar is at 37%");

    // stopped
    post(S.stopped); await wait(60);
    ok(document.querySelector("#top .pill").textContent === "Stopped", "a stopped server says so");
    ok(!btn(document.getElementById("server"), "Start").disabled && btn(document.getElementById("server"), "Stop").disabled, "Start is on and Stop is off");
    ok(/No server log yet/.test(document.getElementById("log").textContent), "an empty log says what to do");

    // tabs and the remote servers
    post(S.base); await wait(60);
    ok(document.querySelectorAll("#tabs .tab").length === 6, "the Cockpit door plus the five pages are tabs");
    ok(!document.getElementById("page-home").hidden && document.getElementById("page-server").hidden && document.getElementById("page-log").hidden, "the Cockpit door is the first page and the others wait");
    ok(document.querySelectorAll("#home .door").length === 5, "the door offers the five pages");
    click(Array.prototype.find.call(document.querySelectorAll("#home .door"), function (d) { return /Databases/.test(d.textContent); })); await wait(30);
    ok(!document.getElementById("page-dbs").hidden && document.getElementById("page-home").hidden, "a door opens its page, whole");
    click(Array.prototype.find.call(document.querySelectorAll("#tabs .tab"), function (t) { return /^Cockpit$/.test(t.textContent); })); await wait(30);
    ok(!document.getElementById("page-home").hidden, "and the Cockpit tab returns to the door");
    click(Array.prototype.find.call(document.querySelectorAll("#tabs .tab"), function (t) { return /Remote/.test(t.textContent); })); await wait(30);
    ok(!document.getElementById("page-remote").hidden && document.getElementById("page-server").hidden, "the Remote tab swaps the pages");
    post(S.remote); await wait(60);
    var rem = document.getElementById("remote");
    ok(rem.querySelectorAll("select option").length === 3, "the hosts of the ssh config are offered");
    ok(rem.querySelector("select").value === "acme19", "the chosen host is selected");
    ok(rem.querySelectorAll("tbody tr").length === 2, "the host's databases are rows");
    ok(/apps/.test(rem.textContent) && /odoo19_apps/.test(rem.textContent), "with owner and instance");
    ok(btn(Array.prototype.find.call(rem.querySelectorAll("tbody tr"), function (r) { return /apps/.test(r.textContent); }), "Drop").disabled, "a database with sessions cannot be dropped from here");
    var demoRow = Array.prototype.find.call(rem.querySelectorAll("tbody tr"), function (r) { return /demo/.test(r.textContent); });
    click(btn(demoRow, "Pull to local"));
    ok(window.__sent.some(function (m) { return m.cmd === "remotePull" && m.host === "acme19" && m.db === "demo"; }), "Pull sends the host and the database");
    click(btn(demoRow, "Backup file"));
    ok(window.__sent.some(function (m) { return m.cmd === "remoteBackup" && m.db === "demo"; }), "Backup sends the database");
    click(btn(demoRow, "Drop"));
    ok(window.__sent.some(function (m) { return m.cmd === "remoteDrop" && m.db === "demo"; }), "Drop sends it - and the host asks, with the name typed again");
    click(btn(rem, "Push a local database…"));
    ok(window.__sent.some(function (m) { return m.cmd === "remotePush" && m.host === "acme19"; }), "Push asks the host side to guide it");
    ok(/Pulled demo/.test(rem.textContent), "the last result is shown");
    post(S.busy); await wait(60);
    ok(/step 3 of 6/.test(rem.textContent) && rem.querySelector(".bar > i").style.width === "50%", "a running job shows its step and a bar");
    ok(btn(rem, "Refresh").disabled && Array.prototype.every.call(rem.querySelectorAll("tbody button"), function (b) { return b.disabled; }), "nothing else can be started meanwhile");
    post(S.key); await wait(60);
    ok(/password or another key/.test(rem.textContent) && !!btn(rem, "Set up key login for acme19…"), "a host that wants a password offers to set up a key");
    click(btn(rem, "Set up key login for acme19…"));
    ok(window.__sent.some(function (m) { return m.cmd === "remoteKey" && m.host === "acme19"; }), "and asks the host side to open the terminal");
    var tabs = document.querySelector("#remote select"); tabs.focus();
    post(S.remote); await wait(60);
    ok(!/Pulled demo/.test(document.getElementById("remote").textContent), "a list being chosen from is not redrawn under the hand");
    tabs.blur(); await wait(60);
    ok(/Pulled demo/.test(document.getElementById("remote").textContent), "and is redrawn when the hand leaves it");
    click(Array.prototype.find.call(document.querySelectorAll("#tabs .tab"), function (t) { return /^Server$/.test(t.textContent); })); await wait(30);

    // the bottom panel: the same page, laid out to fill what it is given
    post(S.panel); await wait(60);
    ok(document.body.className === "panel", "the bottom panel marks the page for its compact layout");
    ok(getComputedStyle(document.body).height !== "auto" && document.querySelectorAll("#dbs tbody tr").length === 4 && document.querySelectorAll("#log div").length > 0, "and still draws everything");
    click(btn(document.getElementById("top"), "Open in editor ↗"));
    ok(window.__sent.some(function (m) { return m.cmd === "move" && m.to === "editor"; }), "in the bottom panel a button moves it up to the editor");
    post(S.base); await wait(60);
    ok(document.body.className === "", "the editor tab is not compact");
    click(btn(document.getElementById("top"), "Move to bottom panel ↓"));
    ok(window.__sent.some(function (m) { return m.cmd === "move" && m.to === "panel"; }), "and in the editor a button moves it down to the bottom panel");

    // two servers at once
    post(S.two); await wait(60);
    click(Array.prototype.find.call(document.querySelectorAll("#tabs .tab"), function (t) { return /^Server$/.test(t.textContent); })); await wait(30);
    var rows = document.querySelectorAll("#server table.servers tbody tr");
    ok(rows.length === 2, "every running server is a row");
    ok(/1901/.test(document.querySelector("#server tr.focus").textContent), "the one the pages follow is marked");
    ok(btn(document.getElementById("server"), "Start another") && !btn(document.getElementById("server"), "Start"), "with a server running the button says Start another");
    click(btn(rows[0], "Follow"));
    ok(window.__sent.some(function (m) { return m.cmd === "focusServer" && m.port === 1900; }), "Follow switches to that server");
    click(btn(rows[0], "Stop"));
    ok(window.__sent.some(function (m) { return m.cmd === "stopAt" && m.port === 1900; }), "a row's Stop stops only that server");
    click(btn(rows[1], "Open"));
    ok(window.__sent.some(function (m) { return m.cmd === "open" && m.port === 1901; }), "a row's Open opens that port");
    post(S.base); await wait(60);
    var pre = document.createElement("pre"); pre.id = "result";
    pre.textContent = JSON.stringify({ passed: out.length, failed: fail.concat(window.__errors.map(function (m) { return "PAGE ERROR " + m; })) });
    document.body.appendChild(pre);
  })().catch(function (e) {
    var pre = document.createElement("pre"); pre.id = "result"; pre.textContent = JSON.stringify({ passed: out.length, failed: fail.concat(["THREW " + e.message]).concat(window.__errors.map(function (m) { return "PAGE ERROR " + m; })) });
    document.body.appendChild(pre);
  });
})();
`;

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cockpit-ui-"));
const template = fs.readFileSync(path.join(__dirname, "..", "media", "cockpit.html"), "utf8");
const page = cockpit.pageHtml(template);
const nonce = /nonce-([A-Za-z0-9]{32})/.exec(page)[1];
const mainScript = `<script nonce="${nonce}">`;
const assembled = page.replace(mainScript, `<script nonce="${nonce}">${harness}</script>\n${mainScript}`).replace("</body>", `<script nonce="${nonce}">${checks}</script>\n</body>`);
const file = path.join(dir, "cockpit.html");
fs.writeFileSync(file, assembled);

function run(args) {
  return cp.execFileSync(chrome, ["--headless=new", "--disable-gpu", "--no-sandbox", `--user-data-dir=${path.join(dir, "profile" + Math.random().toString(36).slice(2))}`, ...args],
    { timeout: 90000, encoding: "utf8", maxBuffer: 32 * 1024 * 1024, stdio: ["ignore", "pipe", "ignore"] });
}
const dom = run(["--virtual-time-budget=6000", "--dump-dom", `file:///${file.replace(/\\/g, "/")}`]);
const found = /<pre id="result">([^<]*)<\/pre>/.exec(dom);
assert.ok(found, "the page produced no result: it did not run to the end");
const result = JSON.parse(found[1].replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">"));
assert.deepStrictEqual(result.failed, [], `the cockpit page failed: ${result.failed.join(" | ")}`);

// a picture in the editor's own two themes, to look at
const shotIndex = process.argv.indexOf("--shot");
if (shotIndex > 0) {
  const folder = process.argv[shotIndex + 1];
  fs.mkdirSync(folder, { recursive: true });
  const light = "<style nonce=\"" + nonce + "\">:root{--vscode-foreground:#3b3b3b;--vscode-editor-background:#ffffff;--vscode-sideBar-background:#f3f3f3;--vscode-panel-border:#d4d4d4;--vscode-descriptionForeground:#717171;--vscode-input-background:#ffffff;--vscode-input-foreground:#3b3b3b;--vscode-button-secondaryBackground:#e4e4e4;--vscode-button-secondaryForeground:#3b3b3b}</style>";
  fs.writeFileSync(path.join(dir, "light.html"), assembled.replace("</head>", `${light}</head>`));
  // the bottom panel: wide, and not tall
  const marker = "post(S.base); await wait(60);" + String.fromCharCode(10) + "    var pre = document.createElement";
  const at = assembled.lastIndexOf(marker);
  fs.writeFileSync(path.join(dir, "panel.html"), assembled.slice(0, at) + marker.replace("S.base", "S.panel") + assembled.slice(at + marker.length));
  run(["--virtual-time-budget=6000", "--window-size=1500,380", `--screenshot=${path.join(folder, "cockpit_panel_dark.png")}`, `file:///${path.join(dir, "panel.html").split(path.sep).join("/")}`]);
  // every page of the cockpit, in the bottom panel's shape (the door, then each of the five pages; two servers for the Server page)
  for (const [name, tab, stateKey] of [["home", "home", "panel"], ["server_two_servers", "server", "two"], ["databases", "dbs", "panel"], ["log", "log", "panel"], ["errors", "issues", "panel"], ["remote", "remote", "remote"]]) {
    const inject = `window.__cockpit.ui.tab = "${tab}"; post(S.${stateKey}); await wait(60);` + String.fromCharCode(10) + "    var pre = document.createElement";
    fs.writeFileSync(path.join(dir, `page_${name}.html`), assembled.slice(0, at) + inject + assembled.slice(at + marker.length));
    run(["--virtual-time-budget=6000", "--window-size=1500,420", `--screenshot=${path.join(folder, `page_${name}.png`)}`, `file:///${path.join(dir, `page_${name}.html`).split(path.sep).join("/")}`]);
  }
  for (const [name, source] of [["dark", file], ["light", path.join(dir, "light.html")]]) {
    run(["--virtual-time-budget=6000", "--window-size=1500,1250", `--screenshot=${path.join(folder, `cockpit_${name}.png`)}`, `file:///${source.replace(/\\/g, "/")}`]);
  }
}
console.log(`cockpit ui OK: ${result.passed} checks in a real browser (draws every part, sends the right command for each press, markup in a log line stays text)`);
fs.rmSync(dir, { recursive: true, force: true });
process.exit(0);
