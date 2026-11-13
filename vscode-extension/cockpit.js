"use strict";
// The cockpit: one page with the whole of the Odoo server in front of you. This is the host side - it gathers what
// the extension already knows (server, health, progress, issues, log), asks the tools for what it does not
// (databases, modules), sends it all to media/cockpit.html as one state, and carries out what the page asks.
// The page draws nothing it was not given and does nothing but send a command back.

const vscode = require("vscode");
const fs = require("fs");
const path = require("path");

const MAX_WHOLE_LOG = 8 * 1024 * 1024;     // a log larger than this is read from its tail
const TAIL_BYTES = 2 * 1024 * 1024;
const LOG_LINES = 400;
const LINE_WIDTH = 700;

// ----------------------------------------------------------------------------- pure parts (tested)

/** The level of a raw Odoo log line, from its own text: error, warning or info. */
function levelOfLine(line) {
  const m = / (CRITICAL|ERROR|WARNING|INFO|DEBUG) /.exec(String(line).slice(0, 80));
  if (!m) return null;
  return m[1] === "CRITICAL" || m[1] === "ERROR" ? "error" : m[1] === "WARNING" ? "warning" : "info";
}

/**
 * The last lines of a log, each with its number and level.
 * `marks` ({error: [lines], warning: [lines]}) comes from the tools and is exact; without it (a tail of a huge
 * file, or no verdict yet) the level is read from the line itself. A line that is not a log line (a traceback, a
 * wrapped message) takes the level of the record before it.
 */
function logLines(text, marks, max = LOG_LINES, firstNumber = 1) {
  const raw = String(text).split("\n");
  if (raw.length && raw[raw.length - 1] === "") raw.pop();
  const errors = marks && marks.error ? new Set(marks.error) : null;
  const warnings = marks && marks.warning ? new Set(marks.warning) : null;
  let inherited = "info";
  const all = raw.map((line, i) => {
    const n = firstNumber === null ? null : firstNumber + i;
    const clean = line.replace(/\r$/, "");
    let level;
    if (errors && n !== null) level = errors.has(n) ? "error" : warnings && warnings.has(n) ? "warning" : "info";
    else {
      const own = levelOfLine(clean);
      if (own) inherited = own;
      level = own || inherited;
    }
    return { n, level, text: clean.length > LINE_WIDTH ? `${clean.slice(0, LINE_WIDTH)}…` : clean };
  });
  return all.slice(-max);
}

/** Read a log file for display: [text, firstNumber]. firstNumber is null when only a tail was read. */
function readLog(file) {
  try {
    const size = fs.statSync(file).size;
    if (size <= MAX_WHOLE_LOG) return [fs.readFileSync(file, "utf8"), 1];
    const fd = fs.openSync(file, "r");
    try {
      const buffer = Buffer.alloc(TAIL_BYTES);
      fs.readSync(fd, buffer, 0, TAIL_BYTES, size - TAIL_BYTES);
      const text = buffer.toString("utf8");
      return [text.slice(text.indexOf("\n") + 1), null];
    } finally {
      fs.closeSync(fd);
    }
  } catch (error) {
    return ["", 1];
  }
}

/** What the page needs of a verdict, without the heavy parts (every item, every marked line). */
function slimHealth(verdict) {
  if (!verdict) return null;
  const issue = verdict.first_issue;
  return {
    state: verdict.state, errors: verdict.errors || 0, critical: verdict.critical || 0, http_500: verdict.http_500 || 0,
    warnings: verdict.warnings || 0, hint: verdict.hint || null, progress: verdict.progress || null,
    databases_failing: verdict.databases_failing || [],
    first_issue: issue ? { message: issue.message, db: issue.db } : null,
  };
}

/** The issues for the page: counts of distinct problems, and the first ones, errors first. */
function slimIssues(source) {
  const verdict = source && source.verdict;
  const items = (verdict && verdict.items) || [];
  return {
    has: !!verdict, label: source ? source.label : null,
    errors: items.filter((i) => i.kind === "error").length, warnings: items.filter((i) => i.kind === "warning").length,
    items: items.slice(0, 60).map((i) => ({
      kind: i.kind, line: i.line, count: i.count, message: String(i.message || "").slice(0, 300), detail: i.detail ? String(i.detail).slice(0, 300) : null,
      module: i.module || null, location: i.location ? { file: i.location.file, line: i.location.line } : null,
    })),
  };
}

/** The whole state the page draws. `inputs` are plain values; nothing here touches the editor. */
function buildState(inputs) {
  const { project, port, servers, focusPort, launchedPorts, serverRunning, serverDb, health, source, upgrade, databases, modulesInfo, drift, logText, logFirst, backupDir, backupBeforeUpgrade, remote, graph, ci, tools, mode } = inputs;
  const mine = (servers || []).find((s) => s.mine);
  const marks = source && source.verdict ? source.verdict.marks : null;
  return {
    project: { name: project.name, version: project.version, root: project.root, port },
    server: {
      running: !!serverRunning, pid: mine ? mine.pid : null, db: serverDb || (mine && mine.db) || null, health: slimHealth(health), focus: focusPort || null,
      // every Odoo server on this machine: any of them can be followed, opened or stopped without touching the others
      list: (servers || []).map((s) => ({ port: s.port, pid: s.pid, db: s.db || null, mine: !!s.mine, focus: s.port === focusPort, hasLog: (launchedPorts || []).includes(s.port) })),
    },
    upgrade: upgrade ? { db: upgrade.db, what: upgrade.what, text: upgrade.text, errors: upgrade.errors || 0, warnings: upgrade.warnings || 0, progress: upgrade.progress || null } : null,
    issues: slimIssues(source),
    databases: databases || null,
    modulesInfo: modulesInfo || null,
    drift: drift || null,
    log: { path: source && source.log ? source.log : null, lines: logText ? logLines(logText, logFirst === 1 ? marks : null, LOG_LINES, logFirst) : [] },
    backupDir: backupDir || "",
    backupBeforeUpgrade: !!backupBeforeUpgrade,
    remote: remote || null,
    graph: graph || null,
    ci: ci || null,
    tools: tools || { found: true, cli: null },   // are the odoo-ai tools there? the page says so when they are not
    mode: mode || "editor",
  };
}

function nonce() {
  const chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
  let out = "";
  for (let i = 0; i < 32; i++) out += chars[Math.floor(Math.random() * chars.length)];
  return out;
}

/** The page, with the content-security nonce put in. */
function pageHtml(template) {
  const n = nonce();
  return template.split("{{nonce}}").join(n);
}

// ----------------------------------------------------------------------------- the panel and the bottom view
//
// The cockpit is one view in the bottom panel, beside Terminal and Problems ("panel"); VS Code lets the person drag it
// anywhere else. A tab of the editor area ("editor") exists only as the way out for a window that has not been
// reloaded since the extension was updated and does not know the panel yet. Both are fed the same state; `mode`
// only tells the page how to lay itself out.

function create(api) {
  const surfaces = new Map();          // mode -> { webview, visible: () => boolean }
  let panel = null;
  let databases = null;
  let modulesInfo = null;
  let drift = null;
  let loadingDatabases = false;
  let timer = null;
  const renders = {};                  // mode -> what that page last said it drew
  let template = null;
  const remote = require("./remote").create(Object.assign({}, api, { changed: () => post(), localChanged: async () => { databases = null; post(); await loadDatabases(); } }));
  const graph = require("./graph").create(Object.assign({}, api, { changed: () => { post(); if (api.graphChanged) api.graphChanged(); } }));   // the sidebar's Graph row follows it

  const project = () => api.findProject();

  function currentState(mode) {
    const p = project();
    const source = api.issuesSource();
    const [logText, logFirst] = source && source.log && fs.existsSync(source.log) ? readLog(source.log) : ["", 1];
    return buildState({
      project: p, port: api.getProjectPort(p), servers: api.state.servers, focusPort: api.state.focusPort, launchedPorts: api.launchedPorts(), serverRunning: api.state.serverRunning,
      serverDb: api.serverRun.db, health: api.state.health, source, upgrade: api.state.upgrade, databases, modulesInfo, drift,
      logText, logFirst, backupDir: api.getBackupDir(p), backupBeforeUpgrade: api.backupBeforeUpgrade(), remote: remote.state(), graph: graph.state(), ci: api.ci(), tools: api.tools ? api.tools(p) : null, mode,
    });
  }

  function post() {
    for (const [mode, s] of surfaces) if (s.visible()) s.webview.postMessage({ type: "state", state: currentState(mode) });
    startTimer();
  }

  function startTimer() {
    if (!timer && surfaces.size) timer = setInterval(post, 3000);   // the log and the progress move; the databases are read on demand
    if (timer && !surfaces.size) { clearInterval(timer); timer = null; }
  }

  function toast(level, text) {
    for (const s of surfaces.values()) s.webview.postMessage({ type: "toast", level, text });
  }

  async function loadDatabases() {
    if (loadingDatabases || !project()) return;
    loadingDatabases = true;
    try {
      const res = await api.runCli(project(), api.serverArgs(project(), "inventory", ["--json"]), 120000);
      const data = JSON.parse(res.stdout);
      databases = (data.databases || []).map((d) => Object.assign({}, d));
      if (data.error) toast("bad", `Could not read the databases (${data.error}).`);
    } catch (error) {
      databases = [];
      toast("bad", "Could not read the databases.");
    } finally {
      loadingDatabases = false;
      post();
    }
  }

  async function loadModules(db) {
    modulesInfo = { db, counts: null, modules: [], error: null };
    post();
    try {
      const res = await api.runCli(project(), api.serverArgs(project(), "modules", ["-d", db, "--json"]), 180000);
      const data = JSON.parse(res.stdout);
      modulesInfo = data.error ? { db, counts: null, modules: [], error: data.error } : { db, counts: data.counts, modules: data.modules, error: null };
    } catch (error) {
      modulesInfo = { db, counts: null, modules: [], error: "no answer" };
    }
    post();
  }

  async function onMessage(m, mode = "editor") {
    if (m.cmd === "rendered") { renders[mode] = m; return; }
    const act = {
      ready: async () => { if (!databases) loadDatabases(); if (!remote.state().hosts) remote.loadHosts(); if (!graph.state().status) graph.refresh(); },
      run: () => api.runCommand(m.command),   // a button of the Quality, Tools and Databases pages: one of this extension's own commands
      graphRefresh: () => graph.refresh(),
      graphUpdate: () => graph.update(),
      graphDoctor: () => graph.doctor(),
      graphWiring: () => graph.wiring(false),
      graphWiringApply: () => graph.wiring(true),
      graphMapVisible: () => graph.wanted(!!m.on),
      graphMap: () => graph.pin(m.module),
      graphBack: () => graph.back(),
      graphFollow: () => graph.follow(!!m.on),
      graphOpenModule: () => graph.openModule(m.module),
      start: () => api.startServerQuick(),
      startOptions: () => api.startServer(),
      stop: () => (api.state.focusPort ? api.stopServerAt(api.state.focusPort) : api.stopServer()),
      restart: () => api.restartServer(),
      open: () => api.openServer(typeof m.port === "number" ? m.port : undefined),
      focusServer: () => api.focusServer(m.port),
      stopAt: () => api.stopServerAt(m.port),
      refreshDbs: async () => { databases = null; post(); await loadDatabases(); },
      loadModules: () => loadModules(m.db),
      upgrade: async () => { await api.upgradeDatabase({ db: m.db, modules: m.modules }); await loadDatabases(); if (modulesInfo && modulesInfo.db === m.db) await loadModules(m.db); },
      drift: async () => {
        const result = await api.checkDrift(m.db);
        if (result) { drift = result; post(); }
      },
      backup: async () => { const r = await api.backupDatabase(m.db); if (r && r.ok) toast("ok", `Backed up ${m.db}: ${r.path} (${r.message})`); else if (r) toast("bad", `Backup failed: ${r.message}`); },
      drop: async () => { const done = await api.dropDatabase(m.db); if (done) { if (modulesInfo && modulesInfo.db === m.db) modulesInfo = null; await loadDatabases(); } },
      setBackupBeforeUpgrade: async () => { await api.setBackupBeforeUpgrade(!!m.value); post(); },
      openIssue: async () => {
        if (m.file && fs.existsSync(m.file)) await api.openSourceAt({ file: m.file, line: m.fline });
        await api.openLogAt(m.line);
      },
      openLog: () => api.openLogAt(m.line || 0),
      issuesView: () => api.focusIssues(),
      issuePick: () => api.pickIssue(),
      remoteHosts: () => remote.loadHosts(),
      remoteSelect: () => remote.select(m.host),
      remoteRefresh: () => remote.refresh(),
      remotePull: () => remote.pull(m.host, m.db),
      remotePush: () => remote.push(m.host),
      remoteBackup: () => remote.backup(m.host, m.db),
      remoteDrop: () => remote.drop(m.host, m.db),
      remoteKey: () => remote.setupKey(m.host),
    }[m.cmd];
    if (!act) return;
    try {
      await act();
    } catch (error) {
      toast("bad", `${m.cmd} failed: ${(error && error.message) || error}`);
    }
    post();
  }

  function html(extensionPath) {
    if (template === null) {
      try {
        template = fs.readFileSync(path.join(extensionPath, "media", "cockpit.html"), "utf8");
      } catch (error) {
        template = "<!DOCTYPE html><html><body>The cockpit page is missing from this installation. Reinstall the extension.</body></html>";
      }
    }
    return pageHtml(template);
  }

  /** Show the page in a tab of the editor area. */
  function open(extensionPath) {
    if (panel) { panel.reveal(); post(); return; }
    panel = vscode.window.createWebviewPanel("odooCockpit", "Cockpit Odoo", vscode.ViewColumn.Active, {
      enableScripts: true, retainContextWhenHidden: true, localResourceRoots: [],
    });
    panel.webview.html = html(extensionPath);
    panel.webview.onDidReceiveMessage((m) => onMessage(m, "editor"));
    panel.onDidChangeViewState(() => post());
    surfaces.set("editor", { webview: panel.webview, visible: () => !!panel && panel.visible });
    panel.onDidDispose(() => { panel = null; surfaces.delete("editor"); startTimer(); });
    startTimer();
  }

  /** The provider of the bottom-panel view: VS Code calls it when the view is first shown. */
  function viewProvider(extensionPath) {
    return {
      resolveWebviewView(view) {
        view.webview.options = { enableScripts: true, localResourceRoots: [] };
        view.webview.html = html(extensionPath);
        view.webview.onDidReceiveMessage((m) => onMessage(m, "panel"));
        surfaces.set("panel", { webview: view.webview, visible: () => view.visible });
        view.onDidChangeVisibility(() => post());
        view.onDidDispose(() => { surfaces.delete("panel"); startTimer(); });
        startTimer();
      },
    };
  }

  /** Show one page of the Cockpit in every surface that is open (the door, server, dbs, log, issues, remote). */
  function showTab(tab) {
    for (const s of surfaces.values()) s.webview.postMessage({ type: "tab", tab });
  }

  return {
    showTab,
    currentList: () => currentState("editor").server.list,
    open, viewProvider, refresh: post, isOpen: () => !!panel, isViewOpen: () => surfaces.has("panel"), handle: onMessage,
    lastRender: (mode = "editor") => renders[mode] || null, remote, graph, dispose() { if (panel) panel.dispose(); if (timer) clearInterval(timer); },
  };
}

module.exports = { create, levelOfLine, logLines, readLog, slimHealth, slimIssues, buildState, pageHtml };
