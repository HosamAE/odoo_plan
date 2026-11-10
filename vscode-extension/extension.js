// Odoo Studio & CI (local): Server control, template DB fast test, module scaffold & CI gates.
"use strict";

const vscode = require("vscode");
const cp = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");

const DUAL_PROJECT_RX = /(?:odoo(\d+)[\\/]projects|ProjectsOdoo(\d+))[\\/]([^\\/]+)/i;
const FILE_LINE_RX = /^\s*([\w./\\-]+\.(?:py|xml|csv|js|scss|po|yml|txt)):(\d+):\s*(.*)$/;

let stepsHelper;
let output, statusItem, serverItem, diagnostics, stepsTree, actionsTree, envTree, child = null;
const state = { steps: [], phases: [], logPath: null, result: null, project: null, mode: null, started: null, serverRunning: false, servers: [], portOpen: false };

// ------------------------------------------------------------ the project

function findProject() {
  const cfg = vscode.workspace.getConfiguration("odooCi");
  const forcedVersion = cfg.get("version");

  for (const folder of vscode.workspace.workspaceFolders || []) {
    const m = folder.uri.fsPath.match(DUAL_PROJECT_RX);
    if (m) {
      const version = forcedVersion || m[1] || m[2];
      const name = m[3];
      const root = folder.uri.fsPath.slice(0, m.index + m[0].length);
      return { version, name, root };
    }
  }
  if (vscode.workspace.workspaceFile) {
    const m = vscode.workspace.workspaceFile.fsPath.match(DUAL_PROJECT_RX);
    if (m) {
      const version = forcedVersion || m[1] || m[2];
      const name = m[3];
      const root = path.dirname(vscode.workspace.workspaceFile.fsPath);
      return { version, name, root };
    }
  }

  // Generic Fallback: Any open workspace folder containing Odoo files or custom addons
  if (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length > 0) {
    const root = vscode.workspace.workspaceFolders[0].uri.fsPath;
    const name = path.basename(root);
    const version = forcedVersion || "19";
    return { version, name, root, isGeneric: true };
  }
  return null;
}

function getProjectPort(project) {
  if (!project) return 8069;
  const cfg = vscode.workspace.getConfiguration("odooCi");
  const cfgPort = cfg.get("serverPort");
  if (cfgPort && typeof cfgPort === "number" && cfgPort > 0) {
    return cfgPort;
  }
  const confFile = path.join(project.root, `${project.name}.conf`);
  if (fs.existsSync(confFile)) {
    try {
      const txt = fs.readFileSync(confFile, "utf8");
      const m = txt.match(/^\s*(?:http_port|xmlrpc_port)\s*=\s*(\d+)/m);
      if (m) return parseInt(m[1], 10);
    } catch (e) {}
  }
  return parseInt(`${project.version}00`, 10) || 8069;
}

function getOdooRoot(project) {
  const cfg = vscode.workspace.getConfiguration("odooCi");
  const configured = cfg.get("odooRoot");
  if (configured && configured.trim()) return configured.trim();

  if (process.env.ODOO_ROOT && fs.existsSync(process.env.ODOO_ROOT)) {
    return process.env.ODOO_ROOT;
  }

  if (project && project.root) {
    const parent = path.dirname(project.root);
    const grandParent = path.dirname(parent);
    if (fs.existsSync(path.join(grandParent, `odoo${project.version || 19}`))) {
      return grandParent;
    }
    if (fs.existsSync(path.join(parent, `odoo${project.version || 19}`))) {
      return parent;
    }
  }

  const wsFolders = vscode.workspace.workspaceFolders || [];
  for (const wf of wsFolders) {
    const p1 = path.dirname(wf.uri.fsPath);
    const p2 = path.dirname(p1);
    for (const cand of [p1, p2]) {
      if (fs.existsSync(path.join(cand, "odoo19")) || fs.existsSync(path.join(cand, "bin"))) {
        return cand;
      }
    }
  }

  return process.platform === "win32" ? "C:\\odoo" : path.join(process.env.HOME || "", "odoo");
}

function getPythonExe(project) {
  const cfg = vscode.workspace.getConfiguration("odooCi");
  const configured = cfg.get("python");
  if (configured && configured.trim()) return configured.trim();

  const pyCfg = vscode.workspace.getConfiguration("python");
  const pyPath = pyCfg.get("defaultInterpreterPath");
  if (pyPath && pyPath.trim() && fs.existsSync(pyPath.trim())) return pyPath.trim();

  const version = (project && project.version) || "19";
  const root = getOdooRoot(project);
  const venvPy = process.platform === "win32"
    ? path.join(root, `odoo${version}`, "venv", "Scripts", "python.exe")
    : path.join(root, `odoo${version}`, "venv", "bin", "python");
  if (fs.existsSync(venvPy)) return venvPy;
  return "python";
}

function getCliPath(project) {
  const cfg = vscode.workspace.getConfiguration("odooCi");
  const configured = cfg.get("cliPath");
  if (configured && configured.trim()) return configured.trim();

  const root = getOdooRoot(project);
  if (project && project.version) {
    const defaultCli = path.join(root, `odoo${project.version}`, "tools", "cli.py");
    if (fs.existsSync(defaultCli)) return defaultCli;
  }
  const fallback = path.join(root, "odoo19", "tools", "cli.py");
  if (fs.existsSync(fallback)) return fallback;
  return "odoo-ai";
}

function checkPortOpen(port, cb) {
  const req = http.get(`http://127.0.0.1:${port}/web/health`, (res) => {
    res.resume();
    cb(true);
  });
  req.on("error", () => {
    // If connection was refused or reset, check generic socket
    const net = require("net");
    const socket = new net.Socket();
    socket.setTimeout(400);
    socket.on("connect", () => { socket.destroy(); cb(true); });
    socket.on("timeout", () => { socket.destroy(); cb(false); });
    socket.on("error", () => { socket.destroy(); cb(false); });
    socket.connect(port, "127.0.0.1");
  });
  req.setTimeout(400, () => { req.destroy(); cb(false); });
}

// ------------------------------------------------------------- actions tree view

class ActionsProvider {
  constructor() {
    this._emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._emitter.event;
  }
  refresh() { this._emitter.fire(); }
  getTreeItem(el) { return el; }

  serverItems(port) {
    const item = (label, icon, opts) => treeItem(label, Object.assign({ icon }, opts));
    const mine = state.servers.filter((s) => s.mine);
    const others = state.servers.filter((s) => !s.mine);
    const out = [];
    if (mine.length) {
      out.push(item("Server: Running", "pass", {
        color: "testing.iconPassed", description: `localhost:${mine[0].port} · pid ${mine[0].pid}`,
        tooltip: mine[0].conf ? `Started with ${mine[0].conf}` : "Started without --config",
      }));
    } else if (state.portOpen) {
      out.push(item("Server: Running", "pass", { color: "testing.iconPassed", description: `localhost:${port} · not a local process`,
        tooltip: "Something answers on this port (Docker, WSL or another machine)." }));
    } else {
      out.push(item("Server: Stopped", "circle-outline", { description: `localhost:${port}` }));
    }
    for (const other of others) {
      out.push(item("Another Odoo is running", "warning", {
        color: "list.warningForeground", description: `:${other.port} · pid ${other.pid}`,
        tooltip: other.conf ? `${other.conf}
Click to open it.` : "Started without --config. Click to open it.",
        command: { command: "odooCi.serverOpen", title: "Open", arguments: [other.port] },
      }));
    }
    out.push(item("Start Odoo Server", "play", { command: { command: "odooCi.serverRun", title: "Start Server" } }));
    out.push(item("Stop Odoo Server", "stop-circle", { command: { command: "odooCi.serverStop", title: "Stop Server" } }));
    out.push(item("Open in Browser", "globe", { description: state.serverRunning ? `:${serverPort(findProject())}` : "",
      command: { command: "odooCi.serverOpen", title: "Open in Browser" } }));
    return out;
  }

  getChildren(el) {
    if (el && (el._phase || el._details)) return stepsHelper.getChildren(el);   // the CI progress lives in this tree now
    const project = findProject();
    if (!project) {
      const item = new vscode.TreeItem("No active Odoo project detected", vscode.TreeItemCollapsibleState.None);
      item.description = "Open a workspace inside ProjectsOdoo<NN>/<project>";
      item.iconPath = new vscode.ThemeIcon("info");
      return [item];
    }

    const port = getProjectPort(project);

    if (!el) {
      const serverGroup = new vscode.TreeItem(`Server (Port ${port})`, vscode.TreeItemCollapsibleState.Expanded);
      serverGroup.iconPath = new vscode.ThemeIcon(state.serverRunning ? "radio-tower" : "server");
      serverGroup._kind = "server";

      const ciGroup = new vscode.TreeItem("Quality & CI Gates", vscode.TreeItemCollapsibleState.Expanded);
      ciGroup.iconPath = new vscode.ThemeIcon("shield");
      ciGroup._kind = "ci";

      const toolsGroup = new vscode.TreeItem("Developer Tools", vscode.TreeItemCollapsibleState.Expanded);
      toolsGroup.iconPath = new vscode.ThemeIcon("tools");
      toolsGroup._kind = "tools";

      return [serverGroup, ciGroup, toolsGroup];
    }

    if (el._kind === "server") return this.serverItems(port);

    if (el._kind === "ci") {
      const staticItem = new vscode.TreeItem("Run Static Checks (Lint & Security)", vscode.TreeItemCollapsibleState.None);
      staticItem.iconPath = new vscode.ThemeIcon("checklist");
      staticItem.description = "5s";
      staticItem.command = { command: "odooCi.static", title: "Static Checks" };

      const fastTestItem = new vscode.TreeItem("Run Fast DB Test (Template DB)", vscode.TreeItemCollapsibleState.None);
      fastTestItem.iconPath = new vscode.ThemeIcon("zap");
      fastTestItem.description = "3s";
      fastTestItem.command = { command: "odooCi.fastTest", title: "Fast Test" };

      const xpathItem = new vscode.TreeItem("Validate XML Views & XPath", vscode.TreeItemCollapsibleState.None);
      xpathItem.iconPath = new vscode.ThemeIcon("check");
      xpathItem.command = { command: "odooCi.xpathCheck", title: "Validate XPath" };

      const fullItem = new vscode.TreeItem("Run Full CI Suite (Clean DB)", vscode.TreeItemCollapsibleState.None);
      fullItem.iconPath = new vscode.ThemeIcon("beaker");
      fullItem.command = { command: "odooCi.full", title: "Full CI" };

      return [staticItem, fastTestItem, xpathItem, fullItem, ...stepsHelper.getChildren()];
    }

    if (el._kind === "tools") {
      const wizardItem = new vscode.TreeItem("Project Setup Wizard", vscode.TreeItemCollapsibleState.None);
      wizardItem.iconPath = new vscode.ThemeIcon("gear");
      wizardItem.command = { command: "odooCi.setupWizard", title: "Setup Wizard" };

      const initDbItem = new vscode.TreeItem("Initialize Fast Test DB", vscode.TreeItemCollapsibleState.None);
      initDbItem.iconPath = new vscode.ThemeIcon("database");
      initDbItem.command = { command: "odooCi.initFastDb", title: "Init Fast DB" };

      const snapSaveItem = new vscode.TreeItem("Take Instant DB Snapshot (~1s)", vscode.TreeItemCollapsibleState.None);
      snapSaveItem.iconPath = new vscode.ThemeIcon("device-camera");
      snapSaveItem.command = { command: "odooCi.dbSnapSave", title: "Take Snapshot" };

      const snapRestoreItem = new vscode.TreeItem("Restore DB Snapshot (~2s)", vscode.TreeItemCollapsibleState.None);
      snapRestoreItem.iconPath = new vscode.ThemeIcon("history");
      snapRestoreItem.command = { command: "odooCi.dbSnapRestore", title: "Restore Snapshot" };

      const i18nItem = new vscode.TreeItem("Scan Translations (i18n)", vscode.TreeItemCollapsibleState.None);
      i18nItem.iconPath = new vscode.ThemeIcon("comment-discussion");
      i18nItem.command = { command: "odooCi.i18nScan", title: "Scan Translations" };

      const scaffoldItem = new vscode.TreeItem("Scaffold New Addon", vscode.TreeItemCollapsibleState.None);
      scaffoldItem.iconPath = new vscode.ThemeIcon("add");
      scaffoldItem.command = { command: "odooCi.newAddon", title: "New Addon" };

      const doctorItem = new vscode.TreeItem("Run Odoo-AI Doctor", vscode.TreeItemCollapsibleState.None);
      doctorItem.iconPath = new vscode.ThemeIcon("pulse");
      doctorItem.command = { command: "odooCi.doctor", title: "Doctor Check" };

      return [wizardItem, initDbItem, snapSaveItem, snapRestoreItem, i18nItem, scaffoldItem, doctorItem];
    }

    return [];
  }
}

// ------------------------------------------------------------- steps tree view

const ICONS = {
  ok: () => new vscode.ThemeIcon("pass", new vscode.ThemeColor("testing.iconPassed")),
  fail: () => new vscode.ThemeIcon("error", new vscode.ThemeColor("testing.iconFailed")),
  running: () => new vscode.ThemeIcon("loading~spin"),
  skip: () => new vscode.ThemeIcon("debug-step-over", new vscode.ThemeColor("disabledForeground")),
  pending: () => new vscode.ThemeIcon("circle-large-outline"),
};

class StepsProvider {
  constructor() {
    this._emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._emitter.event;
  }
  refresh() { this._emitter.fire(); }
  getTreeItem(el) { return el; }

  getChildren(el) {
    if (!state.phases.length) return [];
    if (!el) {
      const items = [];
      if (state.result) {
        const r = new vscode.TreeItem(
          state.result.blocked ? "Blocked: an Odoo server is running"
            : state.result.passed ? (state.mode === "full" ? "Passed - safe to push" : "Static checks passed")
            : "Failed - fix before pushing",
          vscode.TreeItemCollapsibleState.None);
        r.iconPath = state.result.passed ? ICONS.ok() : ICONS.fail();
        r.description = state.project ? `${state.project.name} (Odoo ${state.project.version})` : "";
        items.push(r);
      }
      for (const phase of state.phases) {
        const steps = state.steps.filter((s) => s.phase === phase.phase);
        const status = steps.some((s) => s.status === "fail") ? "fail"
          : steps.some((s) => s.status === "running") ? "running"
          : steps.length && steps.every((s) => s.status === "ok" || s.status === "skip") ? "ok" : "pending";
        const item = new vscode.TreeItem(phase.name, vscode.TreeItemCollapsibleState.Expanded);
        item.iconPath = ICONS[status]();
        item.contextValue = "phase";
        item._phase = phase.phase;
        const done = steps.filter((s) => s.status === "ok").length;
        item.description = steps.length ? `${done}/${steps.length}` : "";
        items.push(item);
      }
      return items;
    }
    if (el._phase) {
      return state.steps.filter((s) => s.phase === el._phase).map((s) => {
        const hasDetails = (s.details || []).length > 0;
        const item = new vscode.TreeItem(s.name,
          hasDetails ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None);
        item.iconPath = (ICONS[s.status] || ICONS.pending)();
        if (s.seconds !== undefined) item.description = s.seconds >= 60 ? `${Math.round(s.seconds / 60)} min` : `${s.seconds}s`;
        item._details = s.details || [];
        return item;
      });
    }
    if (el._details) {
      return el._details.map((line) => {
        const item = new vscode.TreeItem(line, vscode.TreeItemCollapsibleState.None);
        item.iconPath = new vscode.ThemeIcon("chevron-right");
        const m = line.match(FILE_LINE_RX);
        if (m && state.project) {
          const file = path.join(state.project.root, m[1]);
          const lineNo = Math.max(0, +m[2] - 1);
          item.command = {
            command: "vscode.open",
            title: "Open file",
            arguments: [vscode.Uri.file(file), { selection: new vscode.Range(lineNo, 0, lineNo, 0) }],
          };
        }
        return item;
      });
    }
    return [];
  }
}

// ------------------------------------------------------------- status bar

function setStatus(kind, label) {
  if (!statusItem) return;
  const project = findProject();
  const projLabel = project ? `${project.name} (${project.version})` : "Odoo";
  if (kind === "running") {
    statusItem.text = `$(loading~spin) Odoo CI: ${label}`;
    statusItem.tooltip = "Click to cancel";
    statusItem.command = "odooCi.cancel";
    statusItem.backgroundColor = new vscode.ThemeColor("statusBarItem.warningBackground");
  } else if (kind === "ok") {
    statusItem.text = `$(pass) ${projLabel}`;
    statusItem.tooltip = "Odoo CI passed. Click for menu.";
    statusItem.command = "odooCi.menu";
    statusItem.backgroundColor = undefined;
  } else if (kind === "fail") {
    statusItem.text = `$(error) ${projLabel}`;
    statusItem.tooltip = "Odoo CI failed. Click to open Checks.";
    statusItem.command = "odooCi.actions.focus";
    statusItem.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
  } else {
    statusItem.text = `$(beaker) ${projLabel}`;
    statusItem.tooltip = project
      ? `Odoo ${project.version} · ${project.name} · port ${getProjectPort(project)}
Click for the Studio menu.`
      : "Odoo Studio & CI. Click for menu.";
    statusItem.command = "odooCi.menu";
    statusItem.backgroundColor = undefined;
  }
  statusItem.show();
}

// ------------------------------------------------------------- runners

const pendingDiagnostics = new Map();

function addDiagnostic(line) {
  const m = line.match(FILE_LINE_RX);
  if (!m || !state.project) return;
  const file = path.join(state.project.root, m[1]);
  const lineNo = Math.max(0, +m[2] - 1);
  const diag = new vscode.Diagnostic(new vscode.Range(lineNo, 0, lineNo, 200), m[3], vscode.DiagnosticSeverity.Error);
  diag.source = "Odoo CI";
  const rule = m[3].match(/^(R\d+)\s/);
  if (rule) diag.code = rule[1];   // the gates print "file:line: R3 message": the code lets a quick fix find it
  const list = pendingDiagnostics.get(file) || [];
  list.push(diag);
  pendingDiagnostics.set(file, list);
  diagnostics.set(vscode.Uri.file(file), list);
}

function onEvent(ev) {
  switch (ev.event) {
    case "plan":
      state.phases = ev.phases || [];
      state.steps = [];
      break;
    case "step": {
      const key = ev.key || ev.name;
      const existing = state.steps.find((s) => s.phase === ev.phase && (s.key || s.name) === key);
      if (existing) Object.assign(existing, ev);
      else state.steps.push(Object.assign({ key }, ev));
      if (ev.status === "running") setStatus("running", ev.phase === "static" ? "static checks" : "installing and testing");
      for (const line of ev.details || []) addDiagnostic(line);
      break;
    }
    case "log":
      state.logPath = ev.path;
      break;
    case "result":
      state.result = ev;
      break;
  }
  stepsTree.refresh();
}

function run(mode) {
  if (child) {
    vscode.window.showWarningMessage("Odoo CI is already running.");
    return;
  }
  const project = findProject();
  if (!project) {
    vscode.window.showErrorMessage("Odoo CI: no active Odoo project found in workspace.");
    return;
  }
  const python = getPythonExe(project);
  const cli = getCliPath(project);
  const args = [cli, "ci"];
  if (mode === "static") args.push("--static-only");
  if (mode === "fullStop") args.push("--stop-servers");

  Object.assign(state, { steps: [], phases: [], logPath: null, result: null, project, mode: mode === "static" ? "static" : "full", started: Date.now() });
  diagnostics.clear();
  pendingDiagnostics.clear();
  stepsTree.refresh();
  output.clear();
  output.appendLine(`> ${python} ${args.join(" ")}   (in ${project.root})`);
  setStatus("running", mode === "static" ? "static checks" : "starting");
  vscode.commands.executeCommand("setContext", "odooCi.running", true);

  child = cp.spawn(python, args, {
    cwd: project.root,
    env: Object.assign({}, process.env, { ODOO_AI_CI_EVENTS: "1", PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" }),
    windowsHide: true,
  });
  let buffer = "";
  const onData = (data) => {
    buffer += data.toString("utf8");
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).replace(/\r$/, "");
      buffer = buffer.slice(nl + 1);
      if (line.startsWith("::ci::")) {
        try { onEvent(JSON.parse(line.slice(6))); } catch (e) { output.appendLine(line); }
      } else {
        output.appendLine(line);
      }
    }
  };
  child.stdout.on("data", onData);
  child.stderr.on("data", onData);
  child.on("error", (err) => {
    output.appendLine(`[odoo-ci] could not start: ${err.message}`);
  });
  child.on("close", (code) => {
    child = null;
    vscode.commands.executeCommand("setContext", "odooCi.running", false);
    const minutes = Math.round((Date.now() - state.started) / 60000);
    if (!state.result) state.result = { passed: code === 0, mode: state.mode, cancelled: code === null };
    stepsTree.refresh();
    const label = state.mode === "full" ? "full" : "static";
    if (state.result.passed) {
      setStatus("ok", label);
      vscode.window.showInformationMessage(
        state.mode === "full" ? `Odoo CI passed (${minutes} min) - safe to push.` : "Odoo CI: static checks passed.");
    } else {
      setStatus("fail", label);
      vscode.window.showErrorMessage(
        state.result.blocked ? "Odoo CI blocked: an Odoo server is running on the project's port." : "Odoo CI: checks failed.");
    }
  });
}

// ------------------------------------------------------------- Server Commands

function serverTerminalName(project) {
  return `Odoo Server: ${project.name} (${project.version})`;
}

function startServer() {
  const project = findProject();
  if (!project) return vscode.window.showErrorMessage("No active Odoo project found.");
  const conf = path.join(project.root, `${project.name}.conf`);
  const python = getPythonExe(project);
  const defaultBin = `C:\\odoo\\odoo${project.version}\\odoo\\odoo-bin`;
  const bin = fs.existsSync(defaultBin) ? defaultBin : "odoo-bin";
  const port = getProjectPort(project);

  let term = vscode.window.terminals.find(t => t.name === serverTerminalName(project));
  if (term) term.dispose();

  const cwd = fs.existsSync(path.dirname(bin)) ? path.dirname(bin) : project.root;
  term = vscode.window.createTerminal({
    name: serverTerminalName(project),
    cwd: cwd
  });
  term.show();
  const confArg = fs.existsSync(conf) ? `-c "${conf}"` : `--http-port=${port}`;
  term.sendText(cliLine(python, bin, `${confArg} --dev=all`));
  vscode.window.showInformationMessage(`Starting Odoo ${project.version} server for ${project.name} on port ${port}...`);
  setTimeout(pollServerStatus, 2500);
}

// ------------------------------------------------------------- finding a running server, however it was started

/** Split a process command line into its arguments, keeping quoted paths whole. */
function splitCommandLine(cmd) {
  const tokens = [];
  const rx = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let m;
  while ((m = rx.exec(cmd)) !== null) tokens.push(m[1] !== undefined ? m[1] : m[2] !== undefined ? m[2] : m[3]);
  return tokens;
}

function normPath(p) {
  return String(p || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
}

function portFromConf(conf, project) {
  const candidates = [conf];
  if (project && project.root) candidates.push(path.join(project.root, conf));
  for (const file of candidates) {
    try {
      const m = fs.readFileSync(file, "utf8").match(/^\s*(?:http_port|xmlrpc_port)\s*=\s*(\d+)/m);
      if (m) return parseInt(m[1], 10);
    } catch (e) { /* not readable from here: the next candidate, then the default */ }
  }
  return null;
}

const ODOO_SUBCOMMANDS = new Set(["shell", "scaffold", "db", "deploy", "i18n", "populate", "cloc", "neutralize", "obfuscate", "module", "upgrade_code", "help", "server"]);

/** What a command line says about an Odoo server: {port, conf, mine}, or null when it is not a running server
 *  (not Odoo, an update/test run with --stop-after-init, or a subcommand such as `shell`). */
function parseOdooCommand(cmd, project) {
  if (!/(?:odoo-bin|odoo\.py|-m\s+odoo(?:\s|$))/i.test(cmd)) return null;
  if (/--stop-after-init/.test(cmd)) return null;
  const tokens = splitCommandLine(cmd);
  const bin = tokens.findIndex((t) => /odoo-bin$|odoo\.py$/i.test(t) || t === "odoo");
  const after = tokens.slice(bin + 1).find((t) => !t.startsWith("-"));
  if (bin >= 0 && after && ODOO_SUBCOMMANDS.has(after) && after !== "server") return null;
  let conf = null;
  let port = null;
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    let m;
    if ((m = t.match(/^--config=(.+)$/))) conf = m[1];
    else if (t === "-c" || t === "--config") conf = tokens[i + 1] || conf;
    else if ((m = t.match(/^--(?:http|xmlrpc)-port=(\d+)$/))) port = parseInt(m[1], 10);
    else if (t === "--http-port" || t === "--xmlrpc-port" || t === "-p") port = parseInt(tokens[i + 1], 10) || port;
  }
  if (port === null && conf) port = portFromConf(conf, project);
  if (port === null) port = 8069;
  let mine = false;
  if (project && project.root) {
    const root = normPath(project.root);
    mine = (conf !== null && (normPath(conf).startsWith(root + "/") || path.posix.basename(normPath(conf)) === `${project.name}.conf`.toLowerCase()))
      || normPath(cmd).includes(root + "/")
      || normPath(cmd).split(/[\s,;"']+/).some((word) => word.endsWith(root));   // e.g. the last entry of --addons-path=a,b
  }
  return { port, conf, mine };
}

/** Every process on this machine that looks like an Odoo server: [{pid, cmd}]. Works for a terminal, a task, a debugger, a service. */
function listOdooProcesses() {
  return new Promise((resolve) => {
    if (process.platform === "win32") {
      const script = "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'odoo-bin|odoo\\.py|-m odoo' } | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress";
      cp.execFile("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script],
        { timeout: 10000, windowsHide: true, maxBuffer: 4 * 1024 * 1024 }, (err, stdout) => {
          if (err || !String(stdout).trim()) return resolve([]);
          try {
            const data = JSON.parse(stdout);
            resolve((Array.isArray(data) ? data : [data])
              .map((p) => ({ pid: p.ProcessId, cmd: p.CommandLine || "" }))
              .filter((p) => !p.cmd.includes("Get-CimInstance")));
          } catch (e) { resolve([]); }
        });
    } else {
      cp.execFile("ps", ["-eo", "pid=,args="], { timeout: 5000, maxBuffer: 4 * 1024 * 1024 }, (err, stdout) => {
        if (err) return resolve([]);
        resolve(String(stdout).split("\n").map((line) => line.match(/^\s*(\d+)\s+(.*)$/))
          .filter((m) => m && /odoo-bin|odoo\.py|-m odoo/.test(m[2]))
          .map((m) => ({ pid: parseInt(m[1], 10), cmd: m[2] })));
      });
    }
  });
}

/** The Odoo servers running now, each marked `mine` when it serves this project: [{pid, port, conf, mine}]. */
async function detectServers(project) {
  const seen = new Map();
  for (const proc of await listOdooProcesses()) {
    const info = parseOdooCommand(proc.cmd, project);
    if (!info) continue;
    const key = `${proc.cmd}`;
    // a venv python starts a second process with the same command line: keep the parent (lowest pid)
    if (!seen.has(key) || seen.get(key).pid > proc.pid) seen.set(key, Object.assign({ pid: proc.pid }, info));
  }
  return [...seen.values()];
}

function killTree(pid) {
  return new Promise((resolve) => {
    if (process.platform === "win32") cp.execFile("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true }, () => resolve());
    else { try { process.kill(pid, "SIGTERM"); } catch (e) { /* already gone */ } resolve(); }
  });
}

/** The port to open in the browser: this project's server, else the configured port when it answers, else another Odoo. */
function serverPort(project) {
  const mine = state.servers.find((s) => s.mine);
  if (mine) return mine.port;
  if (state.portOpen) return getProjectPort(project);
  return state.servers.length ? state.servers[0].port : getProjectPort(project);
}

async function stopServer() {
  const project = findProject();
  if (!project) return;
  const term = vscode.window.terminals.find((t) => t.name === serverTerminalName(project));
  if (term) term.dispose();
  const servers = await detectServers(project);
  let targets = servers.filter((s) => s.mine);
  if (!targets.length && servers.length) {
    const list = servers.map((s) => `port ${s.port} (pid ${s.pid})`).join(", ");
    const answer = await vscode.window.showWarningMessage(
      `No server of ${project.name} is running, but another Odoo is: ${list}. Stop it?`, { modal: true }, "Stop it");
    if (answer !== "Stop it") return;
    targets = servers;
  }
  for (const s of targets) await killTree(s.pid);
  if (!targets.length) {   // answering on the port but not a local process we can see: the old way
    const port = getProjectPort(project);
    if (process.platform === "win32") {
      cp.exec(`powershell -Command "Get-NetTCPConnection -LocalPort ${port} -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"`, noop);
    }
  }
  vscode.window.showInformationMessage(targets.length
    ? `Stopped ${targets.length} Odoo server${targets.length === 1 ? "" : "s"}.` : "No Odoo server found to stop.");
  setTimeout(pollServerStatus, 1000);
}

function noop() {}

function openServer(port) {
  const project = findProject();
  const target = typeof port === "number" ? port : serverPort(project);
  vscode.env.openExternal(vscode.Uri.parse(`http://localhost:${target}`));
}

let polling = false;
async function pollServerStatus() {
  const project = findProject();
  if (polling || !project || (vscode.window.state && vscode.window.state.focused === false)) return;
  polling = true;
  try {
    const port = getProjectPort(project);
    const [servers, open] = await Promise.all([detectServers(project), new Promise((res) => checkPortOpen(port, res))]);
    state.servers = servers;
    state.portOpen = open;
    state.serverRunning = servers.some((s) => s.mine) || open;
  } finally {
    polling = false;
  }
  if (serverItem) {
    if (state.serverRunning) {
      serverItem.text = `$(server-process) :${serverPort(project)}`;
      serverItem.tooltip = `Odoo is running on port ${serverPort(project)}. Click to open it.`;
      serverItem.command = "odooCi.serverOpen";
      serverItem.show();
    } else {
      serverItem.hide();
    }
  }
  actionsTree.refresh();
}

function runFastTest() {
  const project = findProject();
  if (!project) return vscode.window.showErrorMessage("No active Odoo project found.");
  vscode.window.showInputBox({
    prompt: "Enter addon name to test on ephemeral template database",
    placeHolder: "e.g. sale_discount_pro or custom_crm"
  }).then(addon => {
    if (!addon) return;
    const term = vscode.window.createTerminal({ name: `Odoo Fast Test: ${addon}` });
    term.show();
    const python = getPythonExe(project);
    const cli = getCliPath(project);
    term.sendText(cliLine(python, cli, `test -m "${addon}" --fast`));
  });
}

function runNewAddon() {
  const project = findProject();
  if (!project) return vscode.window.showErrorMessage("No active Odoo project found.");
  vscode.window.showInputBox({
    prompt: "Enter technical name for new addon",
    placeHolder: "e.g. custom_portal_crm or hr_payroll_geo"
  }).then(addon => {
    if (!addon) return;
    const term = vscode.window.createTerminal({ name: `Odoo New Addon: ${addon}` });
    term.show();
    const python = getPythonExe(project);
    const cli = getCliPath(project);
    term.sendText(cliLine(python, cli, `new-addon "${addon}"`));
  });
}

function runDoctor() {
  const { python, cli, root } = toolsContext();
  runInTerminal("Odoo-AI Doctor", cliLine(python, cli, "doctor"), root);
}

function runXPathCheck() {
  const project = findProject();
  if (!project) return;
  const python = getPythonExe(project);
  const cli = getCliPath(project);

  output.clear();
  output.show(true);
  output.appendLine(`> Validating XML Views & XPath in ${project.name}...`);
  diagnostics.clear();
  pendingDiagnostics.clear();

  const proc = cp.spawn(python, [cli, "xpath-check", project.root], {
    cwd: project.root,
    windowsHide: true,
  });

  let errorCount = 0;
  let buffer = "";

  proc.stdout.on("data", (data) => {
    buffer += data.toString("utf8");
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).replace(/\r$/, "");
      buffer = buffer.slice(nl + 1);
      output.appendLine(line);
      if (line.includes("[XPATH ERROR]") || line.includes("[XML SYNTAX ERROR]")) {
        errorCount++;
        addDiagnostic(line);
      }
    }
  });

  proc.stderr.on("data", (data) => output.appendLine(data.toString("utf8")));

  proc.on("close", (code) => {
    if (code === 0 && errorCount === 0) {
      vscode.window.showInformationMessage("Odoo XPath Check: All XML views & XPath expressions are valid!");
    } else {
      vscode.window.showErrorMessage(`Odoo XPath Check found ${errorCount} error(s). See Problems tab.`);
      vscode.commands.executeCommand("workbench.actions.view.problems");
    }
  });
}

function runDbSnapSave() {
  const project = findProject();
  if (!project) return;
  vscode.window.showInputBox({
    prompt: "Enter snapshot label (e.g. before_update or milestone_1)",
    placeHolder: "before_update"
  }).then(label => {
    if (!label) return;
    const term = vscode.window.createTerminal({ name: `Odoo DB Snap: ${label}` });
    term.show();
    const python = getPythonExe(project);
    const cli = getCliPath(project);
    term.sendText(cliLine(python, cli, `db-snap save "${label}"`));
  });
}

function runDbSnapRestore() {
  const project = findProject();
  if (!project) return;
  vscode.window.showInputBox({
    prompt: "Enter snapshot label to restore",
    placeHolder: "before_update"
  }).then(label => {
    if (!label) return;
    const term = vscode.window.createTerminal({ name: `Odoo DB Restore: ${label}` });
    term.show();
    const python = getPythonExe(project);
    const cli = getCliPath(project);
    term.sendText(cliLine(python, cli, `db-snap restore "${label}"`));
  });
}

function runI18nScan() {
  const project = findProject();
  if (!project) return vscode.window.showErrorMessage("No active Odoo project found.");
  vscode.window.showInputBox({
    prompt: "Enter addon name to scan/sync translations (e.g. sale_portal)",
    placeHolder: "e.g. sale_portal or custom_crm"
  }).then(mod => {
    if (!mod) return;
    const config = vscode.workspace.getConfiguration("odooCi");
    const defaultLang = config.get("defaultLang") || "";
    vscode.window.showInputBox({
      prompt: "Target language code (leave blank to scan all existing PO files, or specify e.g. sv, ar, fr)",
      value: defaultLang,
      placeHolder: "e.g. sv, ar, de, fr (leave blank for all)"
    }).then(lang => {
      const targetLang = (lang !== undefined && lang !== null && lang.trim()) ? lang.trim() : defaultLang.trim();
      const term = vscode.window.createTerminal({ name: `Odoo i18n: ${mod}` });
      term.show();
      const python = getPythonExe(project);
      const cli = getCliPath(project);
      const langFlag = targetLang ? ` -l "${targetLang}"` : "";
      term.sendText(cliLine(python, cli, `i18n scan -m "${mod}"${langFlag}`));
    });
  });
}

async function runSetupWizard() {
  const project = findProject();
  const cfg = vscode.workspace.getConfiguration("odooCi");

  const versions = [
    { label: "Odoo 19.0 (Master / Cutting-Edge)", description: "Recommended for modern development", version: "19" },
    { label: "Odoo 18.0", description: "Standard LTS", version: "18" },
    { label: "Odoo 17.0", description: "LTS", version: "17" },
    { label: "Odoo 20.0", description: "Next-Gen Experimental", version: "20" },
  ];
  const currentVer = (project && project.version) || cfg.get("version") || "19";
  const selectedVer = await vscode.window.showQuickPick(versions, {
    placeHolder: `Select Odoo Version for this workspace (Current: Odoo ${currentVer})`
  });
  if (!selectedVer) return;
  await cfg.update("version", selectedVer.version, vscode.ConfigurationTarget.Workspace);

  const currentPy = getPythonExe(project);
  const selectedPy = await vscode.window.showInputBox({
    prompt: "Enter Python executable path (virtualenv or system python)",
    value: currentPy,
    placeHolder: "C:\\odoo\\odoo19\\venv\\Scripts\\python.exe"
  });
  if (selectedPy) {
    await cfg.update("python", selectedPy.trim(), vscode.ConfigurationTarget.Workspace);
  }

  const currentLang = cfg.get("defaultLang") || "sv";
  const selectedLang = await vscode.window.showInputBox({
    prompt: "Enter Default Target Language Code for i18n Sync (e.g. sv, ar, fr, de)",
    value: currentLang,
    placeHolder: "sv"
  });
  if (selectedLang !== undefined) {
    await cfg.update("defaultLang", selectedLang.trim(), vscode.ConfigurationTarget.Workspace);
  }

  const currentPort = project ? getProjectPort(project) : 8069;
  const selectedPort = await vscode.window.showInputBox({
    prompt: "Enter Odoo Server Port for this project",
    value: String(currentPort),
    placeHolder: "8069 or 1900"
  });
  if (selectedPort) {
    await cfg.update("serverPort", parseInt(selectedPort.trim(), 10) || 8069, vscode.ConfigurationTarget.Workspace);
  }

  const initDbChoice = await vscode.window.showQuickPick([
    { label: "$(zap) Yes, Initialize Fast Test Clean DB Now", description: "Prepares PostgreSQL template for 3s testing", action: true },
    { label: "$(close) Skip for now", description: "Can be initialized later at any time", action: false }
  ], { placeHolder: "Do you want to initialize the Clean Template Database (3s Fast Test)?" });

  if (initDbChoice && initDbChoice.action) {
    runInitFastDb();
  }

  vscode.window.showInformationMessage("Odoo Studio & CI: Project configuration updated successfully!");
  actionsTree.refresh();
}

function runInitFastDb() {
  const project = findProject();
  const python = getPythonExe(project);
  const ver = (project && project.version) || "19";
  const fastScript = `C:\\odoo\\odoo${ver}\\tools\\db_fast.py`;
  const term = vscode.window.createTerminal({ name: `Odoo Init Fast DB (${ver})` });
  term.show();
  term.sendText(cliLine(python, fastScript, `init -v "${ver}"`));
}


// ------------------------------------------------------------- shell-aware command line

/** `python cli args` for the terminal the user really has: PowerShell needs `&` before a quoted path, cmd/bash do not. */
function cliLine(python, script, args) {
  const shell = (vscode.env.shell || "").toLowerCase();
  const ps = shell.includes("powershell") || shell.includes("pwsh");
  return `${ps ? "& " : ""}"${python}" "${script}" ${args || ""}`.trim();
}

function runInTerminal(name, line, cwd) {
  const term = vscode.window.createTerminal({ name, cwd });
  term.show();
  term.sendText(line);
}

// ------------------------------------------------------------- versions & environment

const VERSIONS = ["17", "18", "19", "20"];

function readBranch(toolsDir) {
  try {
    const head = fs.readFileSync(path.join(toolsDir, ".git", "HEAD"), "utf8").trim();
    return head.startsWith("ref:") ? head.replace(/^ref:\s*refs\/heads\//, "") : head.slice(0, 8);
  } catch (e) {
    return null;
  }
}

function listProjects(root, v) {
  const found = [];
  for (const base of [path.join(root, `ProjectsOdoo${v}`), path.join(root, `odoo${v}`, "projects")]) {
    let names = [];
    try { names = fs.readdirSync(base, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name); } catch (e) { continue; }
    for (const name of names) {
      const ws = path.join(base, name, `${name}.code-workspace`);
      if (fs.existsSync(ws)) found.push({ name, ws });
    }
  }
  return found;
}

/** What one Odoo version has on this machine. */
function versionInfo(root, v) {
  const dir = path.join(root, `odoo${v}`);
  const tools = path.join(dir, "tools");
  const info = {
    v,
    tools: fs.existsSync(path.join(tools, "cli.py")),
    source: fs.existsSync(path.join(dir, "odoo", "odoo-bin")),
    venv: fs.existsSync(path.join(dir, "venv")),
    branch: readBranch(tools),
    projects: listProjects(root, v),
  };
  info.ready = info.tools && info.source && info.venv;
  return info;
}

function treeItem(label, opts) {
  const item = new vscode.TreeItem(label, opts.collapsible || vscode.TreeItemCollapsibleState.None);
  if (opts.icon) item.iconPath = new vscode.ThemeIcon(opts.icon, opts.color ? new vscode.ThemeColor(opts.color) : undefined);
  if (opts.description) item.description = opts.description;
  if (opts.tooltip) item.tooltip = opts.tooltip;
  if (opts.command) item.command = opts.command;
  Object.assign(item, opts.extra || {});
  return item;
}

class EnvProvider {
  constructor() {
    this._emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._emitter.event;
  }
  refresh() { this._emitter.fire(); }
  getTreeItem(el) { return el; }

  getChildren(el) {
    const root = getOdooRoot(findProject());
    if (!el) {
      const current = (findProject() || {}).version;
      const nodes = VERSIONS.map((v) => {
        const info = versionInfo(root, v);
        const bits = [info.branch ? `branch ${info.branch}` : "no tools", `${info.projects.length} project${info.projects.length === 1 ? "" : "s"}`];
        return treeItem(`Odoo ${v}${v === current ? "  (this window)" : ""}`, {
          collapsible: vscode.TreeItemCollapsibleState[v === current ? "Expanded" : "Collapsed"],
          icon: info.ready ? "pass" : "warning",
          color: info.ready ? "testing.iconPassed" : "list.warningForeground",
          description: bits.join(" · "),
          tooltip: info.ready ? `Odoo ${v} is ready` : `Odoo ${v} is incomplete: use "Set up this version"`,
          extra: { _kind: "version", _info: info },
        });
      });
      nodes.push(treeItem("Maintenance", { collapsible: vscode.TreeItemCollapsibleState.Expanded, icon: "tools", extra: { _kind: "maint" } }));
      return nodes;
    }
    if (el._kind === "version") return this.versionChildren(el._info);
    if (el._kind === "maint") return this.maintenanceItems();
    return [];
  }

  versionChildren(info) {
    const out = [];
    const mark = (ok, label, hint) => treeItem(label, {
      icon: ok ? "check" : "circle-slash",
      color: ok ? "testing.iconPassed" : "list.warningForeground",
      description: ok ? "" : hint,
    });
    out.push(mark(info.tools, "Tools", "odoo-ai new-version " + info.v));
    out.push(mark(info.source, "Odoo source", "download it (setup lists the command)"));
    out.push(mark(info.venv, "Python venv", "create it (setup lists the command)"));
    if (!info.ready) {
      out.push(treeItem(`Set up Odoo ${info.v}…`, {
        icon: "rocket", command: { command: "odooCi.newVersion", title: "Set up", arguments: [info.v] },
      }));
    }
    for (const p of info.projects) {
      out.push(treeItem(p.name, {
        icon: "folder-opened", description: "open in a new window",
        command: { command: "vscode.openFolder", title: "Open project", arguments: [vscode.Uri.file(p.ws), { forceNewWindow: true }] },
      }));
    }
    return out;
  }

  maintenanceItems() {
    const item = (label, icon, command, description) => treeItem(label, { icon, description, command: { command, title: label } });
    return [
      item("Set up an Odoo version…", "rocket", "odooCi.newVersion", "one command"),
      item("Doctor (health check)", "pulse", "odooCi.doctor"),
      item("Tools self-test", "beaker", "odooCi.selfTest"),
      item("Version branches vs main", "git-compare", "odooCi.syncVersions", "report only"),
      item("Scan staged files for secrets", "shield", "odooCi.secretScan"),
      item("Install secret pre-commit hook", "lock", "odooCi.secretHook", "this repository"),
      item("Project gates (Odoo rules)", "law", "odooCi.gates"),
      item("Open the layout guide", "book", "odooCi.openLayout"),
    ];
  }
}

function toolsContext() {
  const project = findProject();
  const root = getOdooRoot(project);
  const version = (project && project.version) || "19";
  const python = getPythonExe(project || { version });
  const cli = getCliPath(project || { version });
  const cwd = (vscode.workspace.workspaceFolders || [])[0] ? vscode.workspace.workspaceFolders[0].uri.fsPath : root;
  return { project, root, version, python, cli, cwd };
}

async function runNewVersion(preset) {
  const { root, python } = toolsContext();
  let nn = typeof preset === "string" ? preset : null;
  if (!nn) {
    const picks = VERSIONS.map((v) => {
      const info = versionInfo(root, v);
      return { label: `Odoo ${v}`, description: info.ready ? "ready" : info.tools ? "tools present, incomplete" : "not installed", v };
    });
    const choice = await vscode.window.showQuickPick(picks, { placeHolder: "Which Odoo version do you want a working environment for?" });
    if (!choice) return;
    nn = choice.v;
  }
  const mode = await vscode.window.showQuickPick(
    [{ label: "Check only", description: "list what is missing, change nothing", flag: " --check" },
     { label: "Set up", description: "clone the tools, create the folders, fill in what the tools own", flag: "" }],
    { placeHolder: `Odoo ${nn}` });
  if (!mode) return;
  const launcher = path.join(root, "bin", "odoo-ai.py");
  runInTerminal(`Odoo ${nn} setup`, cliLine(python, launcher, `new-version ${nn}${mode.flag}`), root);
  if (envTree) setTimeout(() => envTree.refresh(), 4000);
}

function runSelfTest() {
  const { python, cli, root } = toolsContext();
  runInTerminal("Odoo tools self-test", cliLine(python, cli, "test-self"), root);
}

function runSyncVersions() {
  const { python, cli, root } = toolsContext();
  runInTerminal("Odoo version branches", cliLine(python, cli, "sync-versions"), root);
}

function runSecretScan() {
  const { python, cli, cwd } = toolsContext();
  runInTerminal("Secret scan", cliLine(python, cli, "secrets --staged"), cwd);
}

function runSecretHook() {
  const { python, cli, cwd } = toolsContext();
  runInTerminal("Secret hook", cliLine(python, cli, "secrets --install-hook"), cwd);
}

function runGates() {
  const { project, python, cli } = toolsContext();
  if (!project) return vscode.window.showErrorMessage("Odoo CI: open a project first (ProjectsOdoo<NN>/<project>).");
  runInTerminal("Odoo gates", cliLine(python, cli, `gates --path "${project.root}"`), project.root);
}

function openLayoutGuide() {
  const { root } = toolsContext();
  const guide = path.join(root, "odoo19", "tools", "docs", "LAYOUT.md");
  if (fs.existsSync(guide)) vscode.commands.executeCommand("markdown.showPreview", vscode.Uri.file(guide));
  else vscode.window.showWarningMessage(`Not found: ${guide}`);
}

// ------------------------------------------------------------- quick fixes for the Odoo rules

/** Index of the parenthesis that closes the one opened at `open`, or -1. */
function matchingParen(text, open) {
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    if (text[i] === "(") depth++;
    else if (text[i] === ")" && --depth === 0) return i;
  }
  return -1;
}

/** R3: `len(X.search(ARGS))` -> `X.search_count(ARGS)`; null when it is not safe to rewrite (e.g. `order=`). */
function fixLenSearch(text) {
  const lenAt = text.search(/\blen\(/);
  if (lenAt < 0) return null;
  const lenOpen = text.indexOf("(", lenAt);
  const lenClose = matchingParen(text, lenOpen);
  if (lenClose < 0) return null;
  const inner = text.slice(lenOpen + 1, lenClose);
  let from = 0;
  for (;;) {
    const at = inner.indexOf(".search(", from);
    if (at < 0) return null;
    const open = at + ".search".length;
    if (matchingParen(inner, open) === inner.length - 1) {
      const args = inner.slice(open + 1, -1);
      if (/\border\s*=/.test(args)) return null;   // search_count has no order
      return text.slice(0, lenAt) + inner.slice(0, at) + ".search_count(" + args + ")" + text.slice(lenClose + 1);
    }
    from = at + 1;
  }
}

class RuleFixes {
  provideCodeActions(document, _range, context) {
    const actions = [];
    for (const d of context.diagnostics) {
      if (d.source !== "Odoo CI" || !/^R\d+$/.test(String(d.code || ""))) continue;
      const line = document.lineAt(d.range.start.line);
      if (d.code === "R3") {
        const fixed = fixLenSearch(line.text);
        if (fixed) {
          const fix = new vscode.CodeAction("Use search_count()", vscode.CodeActionKind.QuickFix);
          fix.edit = new vscode.WorkspaceEdit();
          fix.edit.replace(document.uri, line.range, fixed);
          fix.diagnostics = [d];
          fix.isPreferred = true;
          actions.push(fix);
        }
      }
      const xml = document.languageId === "xml";
      const mark = new vscode.CodeAction(`This line must break ${d.code}: add noqa`, vscode.CodeActionKind.QuickFix);
      mark.edit = new vscode.WorkspaceEdit();
      mark.edit.insert(document.uri, line.range.end, xml ? " <!-- noqa: odoo19 REASON -->" : "  # noqa: odoo19 REASON");
      mark.diagnostics = [d];
      actions.push(mark);
    }
    return actions;
  }
}

// ------------------------------------------------------------- activation

function activate(context) {
  output = vscode.window.createOutputChannel("Odoo CI");
  diagnostics = vscode.languages.createDiagnosticCollection("odoo-ci");
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 25);
  serverItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 24);
  stepsHelper = new StepsProvider();
  stepsTree = { refresh: () => actionsTree.refresh() };   // one tree now: the progress is part of the control panel
  actionsTree = new ActionsProvider();
  envTree = new EnvProvider();

  context.subscriptions.push(
    output,
    diagnostics,
    statusItem,
    serverItem,
    vscode.window.registerTreeDataProvider("odooCi.actions", actionsTree),
    vscode.window.registerTreeDataProvider("odooCi.env", envTree),
    vscode.languages.registerCodeActionsProvider([{ language: "python" }, { language: "xml" }], new RuleFixes(),
      { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] }),
    vscode.commands.registerCommand("odooCi.newVersion", runNewVersion),
    vscode.commands.registerCommand("odooCi.selfTest", runSelfTest),
    vscode.commands.registerCommand("odooCi.syncVersions", runSyncVersions),
    vscode.commands.registerCommand("odooCi.secretScan", runSecretScan),
    vscode.commands.registerCommand("odooCi.secretHook", runSecretHook),
    vscode.commands.registerCommand("odooCi.gates", runGates),
    vscode.commands.registerCommand("odooCi.openLayout", openLayoutGuide),
    vscode.commands.registerCommand("odooCi.refreshEnv", () => envTree.refresh()),
    vscode.commands.registerCommand("odooCi.static", () => run("static")),
    vscode.commands.registerCommand("odooCi.full", () => run("full")),
    vscode.commands.registerCommand("odooCi.fullStop", () => run("fullStop")),
    vscode.commands.registerCommand("odooCi.serverRun", startServer),
    vscode.commands.registerCommand("odooCi.serverStop", stopServer),
    vscode.commands.registerCommand("odooCi.serverRestart", () => { stopServer(); setTimeout(startServer, 1500); }),
    vscode.commands.registerCommand("odooCi.serverOpen", openServer),
    vscode.commands.registerCommand("odooCi.fastTest", runFastTest),
    vscode.commands.registerCommand("odooCi.xpathCheck", runXPathCheck),
    vscode.commands.registerCommand("odooCi.dbSnapSave", runDbSnapSave),
    vscode.commands.registerCommand("odooCi.dbSnapRestore", runDbSnapRestore),
    vscode.commands.registerCommand("odooCi.i18nScan", runI18nScan),
    vscode.commands.registerCommand("odooCi.newAddon", runNewAddon),
    vscode.commands.registerCommand("odooCi.doctor", runDoctor),
    vscode.commands.registerCommand("odooCi.setupWizard", runSetupWizard),
    vscode.commands.registerCommand("odooCi.initFastDb", runInitFastDb),
    vscode.commands.registerCommand("odooCi.refresh", () => { pollServerStatus(); actionsTree.refresh(); envTree.refresh(); }),
    vscode.commands.registerCommand("odooCi.cancel", () => {
      if (child) child.kill();
    }),
    vscode.commands.registerCommand("odooCi.showOutput", () => output.show(true)),
    vscode.commands.registerCommand("odooCi.openLog", () => {
      if (state.logPath) vscode.commands.executeCommand("vscode.open", vscode.Uri.file(state.logPath));
    }),
    vscode.commands.registerCommand("odooCi.menu", () => {
      const items = [
        { label: "$(gear) Project Setup Wizard", action: "setupWizard" },
        { label: "$(database) Initialize Fast Test DB (3s)", action: "initFastDb" },
        { label: "$(play) Start Odoo Server", action: "serverRun" },
        { label: "$(globe) Open in Browser", action: "serverOpen" },
        { label: "$(stop-circle) Stop Odoo Server", action: "serverStop" },
        { label: "$(zap) Fast DB Test (3s)", action: "fastTest" },
        { label: "$(check) Validate XML Views & XPath", action: "xpathCheck" },
        { label: "$(device-camera) Take Instant DB Snapshot (~1s)", action: "dbSnapSave" },
        { label: "$(history) Restore DB Snapshot (~2s)", action: "dbSnapRestore" },
        { label: "$(comment-discussion) Scan Translations (i18n)", action: "i18nScan" },
        { label: "$(checklist) Run static checks (seconds)", action: "static" },
        { label: "$(beaker) Run full CI (install and test)", action: "full" },
        { label: "$(add) Scaffold New Addon", action: "newAddon" },
        { label: "$(pulse) Run Doctor Health Check", action: "doctor" },
        { label: "$(rocket) Set up an Odoo version", action: "newVersion" },
        { label: "$(beaker) Tools self-test", action: "selfTest" },
        { label: "$(git-compare) Version branches vs main", action: "syncVersions" },
        { label: "$(shield) Scan staged files for secrets", action: "secretScan" },
        { label: "$(law) Project gates (Odoo rules)", action: "gates" },
      ];
      vscode.window.showQuickPick(items).then((choice) => {
        if (!choice) return;
        vscode.commands.executeCommand(`odooCi.${choice.action}`);
      });
    })
  );

  setStatus("ready");
  pollServerStatus();
  const timer = setInterval(pollServerStatus, 5000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
}

function deactivate() {
  if (child) child.kill();
}

module.exports = { activate, deactivate, _test: { fixLenSearch, parseOdooCommand, splitCommandLine, detectServers, listOdooProcesses } };
