"use strict";
// Translations: pick a module (or any file of it), see for every language what is left, add the missing terms to the
// .po file, and - only if you have set up a model of your own - have them translated. The work is done by the tools
// (`odoo-ai i18n status|update|export|translate`); this page asks and shows. It sends nothing anywhere by itself.

const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const path = require("path");

const KEY = "odooCi.i18n.translatorKey";
const LANGUAGES = ["sv_SE", "ar", "ar_001", "en_GB", "en_US", "de_DE", "fr_FR", "es_ES", "nb_NO", "da_DK", "fi_FI"];

/** The module a path belongs to: the first folder under the project root that holds a __manifest__.py. */
function moduleOf(root, file) {
  const rel = path.relative(root, file);
  if (!rel || rel.startsWith("..") || path.isAbsolute(rel)) return null;
  const name = rel.split(path.sep)[0];
  return fs.existsSync(path.join(root, name, "__manifest__.py")) ? name : null;
}

/** Whether an endpoint is on this machine (nothing leaves it) - anything else is asked about once. */
function isLocal(url) {
  try {
    const host = new URL(url).hostname;
    return host === "localhost" || host === "127.0.0.1" || host === "::1" || host === "[::1]";
  } catch (e) {
    return false;
  }
}

function slim(r) {
  return {
    project: r.project, modules: r.modules, selected: r.selected, languages: r.languages, common: LANGUAGES, busy: r.busy, job: r.job,
    last: r.last, translator: r.translator, error: r.error,
  };
}

function create(api, context) {
  const r = { project: null, modules: null, selected: null, languages: [], busy: false, job: null, last: null, translator: { configured: false }, error: null };
  let panel = null;

  const config = () => vscode.workspace.getConfiguration("odooCi");
  const project = () => api.findProject();

  function translator() {
    const url = String(config().get("i18n.translatorUrl") || "").trim();
    const model = String(config().get("i18n.translatorModel") || "").trim();
    return { configured: !!(url && model), url, model, local: !!url && isLocal(url) };
  }

  function post() {
    if (!panel) return;
    r.languages = (config().get("i18n.languages") || ["sv_SE"]).map(String);
    r.translator = translator();
    panel.webview.postMessage({ type: "state", state: slim(r) });
  }

  function run(args, env, timeout = 3600000) {
    return new Promise((resolve) => {
      const p = project();
      const { file, args: full } = api.cliCommand(p, args);
      let out = "", err = "";
      const child = cp.spawn(file, full, { cwd: p.root, windowsHide: true, env: Object.assign({}, process.env, env || {}) });
      const timer = setTimeout(() => child.kill(), timeout);
      child.stdout.on("data", (d) => { out += d; });
      child.stderr.on("data", (d) => {
        err += d;
        for (const line of String(d).split(/\r?\n/)) {
          const m = /^STEP (\d+)\/(\d+) (.*)$/.exec(line);
          if (m && r.job) { r.job = Object.assign({}, r.job, { step: Number(m[1]), total: Number(m[2]) }); post(); }
        }
      });
      child.on("error", (e) => { clearTimeout(timer); resolve({ code: 1, out: "", err: e.message }); });
      child.on("close", (code) => { clearTimeout(timer); resolve({ code, out, err }); });
    });
  }

  async function refresh() {
    const p = project();
    if (!p) return;
    r.project = p.name;
    const langs = (config().get("i18n.languages") || ["sv_SE"]).join(",");
    const res = await run(["i18n", "status", "--path", p.root, "--lang", langs, "--json"], null, 300000);
    try {
      r.modules = JSON.parse(res.out).modules;
      r.error = null;
    } catch (e) {
      r.modules = [];
      r.error = String(res.err || res.out || "no answer").trim().split("\n").slice(-1)[0].slice(0, 200);
    }
    if (!r.selected || !r.modules.some((m) => m.module === r.selected)) r.selected = r.modules.length ? r.modules[0].module : null;
    post();
  }

  async function working(title, task) {
    if (r.busy) return;
    r.busy = true; r.job = { title, step: 0, total: 0 }; post();
    try { await task(); } finally { r.busy = false; r.job = null; await refresh(); }
  }

  const act = {
    ready: refresh,
    refresh,
    select: async (m) => { r.selected = m.module; post(); },
    pick: async () => {
      const p = project();
      const picked = await vscode.window.showOpenDialog({ defaultUri: vscode.Uri.file(p.root), canSelectMany: false, title: "Pick any file of the module to translate" });
      const name = picked && picked[0] && moduleOf(p.root, picked[0].fsPath);
      if (picked && !name) vscode.window.showWarningMessage("That file is not inside a module of this project (no __manifest__.py above it).");
      if (name) { r.selected = name; post(); }
    },
    addLanguage: async (m) => {
      if (!/^[a-z]{2,3}(_[A-Za-z0-9]{2,4})?$/.test(m.lang || "")) { r.last = { level: "bad", text: "A language code looks like sv_SE or ar." }; return post(); }
      await working(`Adding the missing terms of ${m.module} to ${m.lang}`, async () => {
        const res = await run(["i18n", "update", "-m", m.module, "-l", m.lang]);
        r.last = res.code === 0 ? { level: "ok", text: `${m.module}: terms added to i18n/${m.lang}.po. Translate them now, or open the file.` } : { level: "bad", text: `Could not update ${m.lang}.po: ${res.err.trim().split("\n").slice(-1)[0]}` };
      });
    },
    exportPot: async (m) => {
      await working(`Exporting the template of ${m.module}`, async () => {
        const res = await run(["i18n", "export", "-m", m.module]);
        r.last = res.code === 0 ? { level: "ok", text: `${m.module}: i18n/${m.module}.pot written.` } : { level: "bad", text: "The export failed." };
      });
    },
    openPo: async (m) => {
      const file = path.join(project().root, m.module, "i18n", `${m.lang}.po`);
      if (fs.existsSync(file)) await vscode.window.showTextDocument(vscode.Uri.file(file));
    },
    translate: async (m) => {
      const t = translator();
      if (!t.configured) {
        r.last = { level: "warn", text: "No translator is set up. Set odooCi.i18n.translatorUrl and odooCi.i18n.translatorModel (a local model such as Ollama costs nothing)." };
        return post();
      }
      if (!t.local) {
        const host = new URL(t.url).host;
        const known = context.globalState.get("odooCi.i18n.allowedHosts") || [];
        if (!known.includes(host)) {
          const ok = await vscode.window.showWarningMessage(`The untranslated terms of ${m.module} (field names, labels, messages) will be sent to ${host}. Allow this host?`, { modal: true }, "Allow");
          if (!ok) return;
          await context.globalState.update("odooCi.i18n.allowedHosts", [...known, host]);
        }
      }
      const key = (await context.secrets.get(KEY)) || "";
      await working(`Translating ${m.module} into ${m.lang}`, async () => {
        const res = await run(["i18n", "translate", "--path", project().root, "-m", m.module, "-l", m.lang, "--endpoint", t.url, "--model", t.model, "--json"], { HX_TRANSLATE_KEY: key });
        let data = null;
        try { data = JSON.parse(res.out.trim().split(/\r?\n/).pop()); } catch (e) { /* not JSON */ }
        if (data && data.ok) r.last = { level: data.skipped.length ? "warn" : "ok", text: `${m.module}/${m.lang}: ${data.translated} translated${data.skipped.length ? `, ${data.skipped.length} left for you (the model changed a placeholder or gave no answer)` : ""}.` };
        else r.last = { level: "bad", text: `The translation stopped: ${(data && data.error) || res.err.trim().split("\n").slice(-1)[0] || "no answer"}` };
      });
    },
    setKey: async () => {
      const value = await vscode.window.showInputBox({ title: "Translator API key", prompt: "Kept in the editor's secret storage; never written to a file. Leave empty to remove it.", password: true });
      if (value === undefined) return;
      if (value) await context.secrets.store(KEY, value); else await context.secrets.delete(KEY);
      r.last = { level: "ok", text: value ? "The key is stored." : "The key is removed." };
      post();
    },
    settings: () => vscode.commands.executeCommand("workbench.action.openSettings", "odooCi.i18n"),
  };

  async function onMessage(m) {
    if (m.cmd === "rendered") { r.rendered = m; return; }
    const fn = act[m.cmd];
    if (!fn) return;
    try { await fn(m); } catch (error) { r.last = { level: "bad", text: `${m.cmd} failed: ${(error && error.message) || error}` }; post(); }
  }

  /** Open the page; `uri` (a file or folder picked in the explorer) preselects its module. */
  function open(extensionPath, uri) {
    const p = project();
    const wanted = p && uri && uri.fsPath ? moduleOf(p.root, uri.fsPath) : null;
    if (wanted) r.selected = wanted;
    if (panel) { panel.reveal(); post(); return; }
    panel = vscode.window.createWebviewPanel("odooTranslations", "Odoo Translations", vscode.ViewColumn.Active, { enableScripts: true, retainContextWhenHidden: true, localResourceRoots: [] });
    let template;
    try { template = fs.readFileSync(path.join(extensionPath, "media", "translations.html"), "utf8"); } catch (e) { template = "<html><body>The translations page is missing from this installation.</body></html>"; }
    panel.webview.html = require("./cockpit").pageHtml(template);
    panel.webview.onDidReceiveMessage(onMessage);
    panel.onDidDispose(() => { panel = null; });
  }

  return { open, handle: onMessage, state: () => slim(r), isOpen: () => !!panel, _r: r };
}

module.exports = { create, moduleOf, isLocal };
