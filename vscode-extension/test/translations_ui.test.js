// The translations page in a real browser: it draws what it is given, shows a tick where nothing is left, sends the right
// command for each press, and treats every name as text. node test/translations_ui.test.js [--shot <folder>]
// (skipped when no Chrome is found)
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
const translations = require(path.join(__dirname, "..", "translations.js"));

// ---- the parts that need no browser
assert.strictEqual(translations.moduleOf("/p", "/q/x.py"), null, "a file outside the project belongs to no module");
assert.strictEqual(translations.isLocal("http://localhost:11434/v1"), true);
assert.strictEqual(translations.isLocal("http://127.0.0.1:1234/v1"), true);
assert.strictEqual(translations.isLocal("https://api.example.com/v1"), false, "a hosted model is not local");
assert.strictEqual(translations.isLocal("not a url"), false);

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
  console.log("translations ui SKIPPED: no Chrome or Edge found (set CHROME_BIN)");
  process.exit(0);
}

const lang = (code, total, translated, extra) => Object.assign({
  lang: code, file: `/p/m/i18n/${code}.po`, exists: true, total, translated, missing: total - translated, fuzzy: 0, obsolete: 0,
  percent: total ? Math.floor(100 * translated / total) : 100, complete: total > 0 && translated === total }, extra || {});
const MODULES = [
  { module: "done_mod", path: "/p/done_mod", terms: 40, languages: [lang("sv_SE", 40, 40), lang("ar", 40, 40)] },
  { module: "half_mod", path: "/p/half_mod", terms: 100, languages: [lang("sv_SE", 100, 54, { fuzzy: 2, obsolete: 3 }), lang("ar", 100, 0, { exists: false, file: null })] },
  { module: "<img src=x onerror=window.__xss=1>", path: "/p/x", terms: 3, languages: [] },
];
const state = (extra) => Object.assign({ project: "Demo", modules: MODULES, selected: "half_mod", languages: ["sv_SE"], common: ["sv_SE", "ar", "de_DE"],
  busy: false, job: null, last: null, translator: { configured: false }, error: null }, extra || {});
const STATES = {
  plain: state(),
  done: state({ selected: "done_mod" }),
  ready: state({ translator: { configured: true, url: "http://localhost:11434/v1", model: "qwen2.5:7b", local: true } }),
  hosted: state({ translator: { configured: true, url: "https://api.example.com/v1", model: "m", local: false } }),
  busy: state({ busy: true, job: { title: "Translating half_mod into sv_SE", step: 40, total: 100 }, last: { level: "ok", text: "half_mod: terms added." } }),
  unread: state({ modules: null }),
};

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
  function btn(root, label) { return Array.prototype.find.call(root.querySelectorAll("button"), function (b) { return b.textContent === label; }); }
  var S = ${JSON.stringify(STATES)};
  (async function () {
    await wait(50);
    post(S.unread); await wait(60);
    ok(/Reading the project/.test(document.getElementById("modules").textContent), "before the tools answer it says it is reading");
    post(S.plain); await wait(80);
    var rows = document.querySelectorAll("#modules .mod");
    ok(rows.length === 3, "every module is a row");
    ok(rows[0].querySelector(".check") && /\\u2713/.test(rows[0].textContent), "a module with nothing left has a tick");
    ok(/46 left/.test(rows[1].textContent), "a half done module says how many are left (the worst language)");
    ok(!rows[2].querySelector("img") && window.__xss === undefined, "markup in a module name stays text");
    ok(/no \\.po/.test(rows[2].textContent), "a module without any .po says so");
    ok(rows[1].className.indexOf("sel") >= 0, "the chosen module is marked");
    click(rows[0]);
    ok(window.__sent.some(function (m) { return m.cmd === "select" && m.module === "done_mod"; }), "a row asks for its module");

    // a module that is not done
    var detail = document.getElementById("detail");
    ok(/half_mod/.test(detail.textContent) && /100 terms/.test(detail.textContent), "the module's page names it and its terms");
    ok(detail.querySelector(".bar > i").style.width === "54%", "the bar is at 54%");
    ok(/54 of 100 translated/.test(detail.textContent) && /46 left/.test(detail.textContent) && /2 to review/.test(detail.textContent) && /3 no longer used/.test(detail.textContent), "it says translated, left, to review and no longer used");
    ok(/no file yet/.test(detail.textContent) && !!btn(detail, "Create .po"), "a language without a file offers to create it");
    var translate = Array.prototype.filter.call(detail.querySelectorAll("button"), function (b) { return b.textContent === "Translate automatically"; });
    ok(translate.length === 2 && translate.every(function (b) { return b.disabled; }), "without a translator the automatic button is off");
    ok(/not set up/.test(document.getElementById("engine").textContent) && /costs nothing/.test(document.getElementById("engine").textContent), "and the card says how to set one up, and that a local model is free");
    click(btn(detail, "Add missing terms"));
    ok(window.__sent.some(function (m) { return m.cmd === "addLanguage" && m.module === "half_mod" && m.lang === "sv_SE"; }), "Add missing terms asks for that language");
    click(btn(detail, "Create .po"));
    ok(window.__sent.some(function (m) { return m.cmd === "addLanguage" && m.lang === "ar"; }), "Create .po asks for that language");
    var typed = document.getElementById("newlang"); typed.value = "fr_FR";
    click(btn(detail, "Add"));
    ok(window.__sent.some(function (m) { return m.cmd === "addLanguage" && m.lang === "fr_FR"; }), "a typed language code is added");
    click(btn(detail, "Export .pot"));
    ok(window.__sent.some(function (m) { return m.cmd === "exportPot" && m.module === "half_mod"; }), "Export .pot asks for the template");
    click(Array.prototype.filter.call(detail.querySelectorAll("button"), function (b) { return b.textContent === "Open .po"; })[0]);
    ok(window.__sent.some(function (m) { return m.cmd === "openPo" && m.lang === "sv_SE"; }), "Open .po opens that file");
    click(btn(document.getElementById("modules"), "Pick a file\\u2026"));
    ok(window.__sent.some(function (m) { return m.cmd === "pick"; }), "Pick a file asks the editor for a file");

    // everything translated
    post(S.done); await wait(60);
    detail = document.getElementById("detail");
    ok(detail.querySelectorAll(".check").length >= 3, "the module and each of its languages show a tick");
    ok(/Nothing left to translate/.test(detail.textContent), "the page says nothing is left to translate");
    ok(!btn(detail, "Translate automatically") && !btn(detail, "Add missing terms"), "and offers nothing to do");
    ok(!!btn(detail, "Open .po"), "only to open the file");

    // a translator of your own
    post(S.ready); await wait(60);
    ok(/qwen2\\.5:7b/.test(document.getElementById("engine").textContent) && /nothing leaves it/.test(document.getElementById("engine").textContent), "a local model is shown as staying on this machine");
    var auto = Array.prototype.filter.call(document.getElementById("detail").querySelectorAll("button"), function (b) { return b.textContent === "Translate automatically"; });
    ok(!auto[0].disabled && auto[1].disabled, "automatic translation is on for a language with a file, off for one without");
    click(auto[0]);
    ok(window.__sent.some(function (m) { return m.cmd === "translate" && m.module === "half_mod" && m.lang === "sv_SE"; }), "it asks to translate that language");
    post(S.hosted); await wait(60);
    ok(/api\\.example\\.com/.test(document.getElementById("engine").textContent) && /sent there/.test(document.getElementById("engine").textContent), "a hosted model is shown with where the terms are sent");

    // working
    post(S.busy); await wait(60);
    ok(/40 of 100/.test(document.getElementById("detail").textContent) && Array.prototype.slice.call(document.querySelectorAll("#detail .bar > i")).pop().style.width === "40%", "a job shows its progress");
    ok(btn(document.getElementById("detail"), "Export .pot").disabled, "nothing else starts meanwhile");
    ok(/terms added/.test(document.getElementById("detail").textContent), "the last result is shown");

    post(S.plain); await wait(60);
    var pre = document.createElement("pre"); pre.id = "result";
    pre.textContent = JSON.stringify({ passed: out.length, failed: fail.concat(window.__errors.map(function (m) { return "PAGE ERROR " + m; })) });
    document.body.appendChild(pre);
  })().catch(function (e) {
    var pre = document.createElement("pre"); pre.id = "result"; pre.textContent = JSON.stringify({ passed: out.length, failed: fail.concat(["THREW " + e.message]).concat(window.__errors.map(function (m) { return "PAGE ERROR " + m; })) });
    document.body.appendChild(pre);
  });
})();
`;

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "translations-ui-"));
const template = fs.readFileSync(path.join(__dirname, "..", "media", "translations.html"), "utf8");
const page = cockpit.pageHtml(template);
const nonce = /nonce-([A-Za-z0-9]{32})/.exec(page)[1];
const mainScript = `<script nonce="${nonce}">`;
const assembled = page.replace(mainScript, `<script nonce="${nonce}">${harness}</script>\n${mainScript}`).replace("</body>", `<script nonce="${nonce}">${checks}</script>\n</body>`);
const file = path.join(dir, "translations.html");
fs.writeFileSync(file, assembled);

function run(args) {
  return cp.execFileSync(chrome, ["--headless=new", "--disable-gpu", "--no-sandbox", `--user-data-dir=${path.join(dir, "profile" + Math.random().toString(36).slice(2))}`, ...args],
    { timeout: 90000, encoding: "utf8", maxBuffer: 32 * 1024 * 1024, stdio: ["ignore", "pipe", "ignore"] });
}
const dom = run(["--virtual-time-budget=6000", "--dump-dom", `file:///${file.replace(/\\/g, "/")}`]);
const found = /<pre id="result">([^<]*)<\/pre>/.exec(dom);
assert.ok(found, "the page produced no result: it did not run to the end");
const result = JSON.parse(found[1].replace(/&quot;/g, '"').replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">"));
assert.deepStrictEqual(result.failed, [], `the translations page failed: ${result.failed.join(" | ")}`);

const shotIndex = process.argv.indexOf("--shot");
if (shotIndex > 0) {
  const folder = process.argv[shotIndex + 1];
  fs.mkdirSync(folder, { recursive: true });
  // a picture of the half done module with a translator, and of the finished one
  const withState = (name, key) => {
    const marker = "post(S.plain); await wait(60);" + String.fromCharCode(10) + "    var pre = document.createElement";
    const at = assembled.lastIndexOf(marker);
    fs.writeFileSync(path.join(dir, name + ".html"), assembled.slice(0, at) + marker.replace("S.plain", "S." + key) + assembled.slice(at + marker.length));
    run(["--virtual-time-budget=6000", "--window-size=1300,640", `--screenshot=${path.join(folder, `translations_${name}.png`)}`, `file:///${path.join(dir, name + ".html").split(path.sep).join("/")}`]);
  };
  withState("working", "ready");
  withState("finished", "done");
}
console.log(`translations ui OK: ${result.passed} checks in a real browser (draws every part, a tick where nothing is left, the right command for each press, names stay text)`);
fs.rmSync(dir, { recursive: true, force: true });
process.exit(0);
