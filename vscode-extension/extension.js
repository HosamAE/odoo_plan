// Odoo Studio & CI (local): Server control, template DB fast test, module scaffold & CI gates.
"use strict";

const vscode = require("vscode");
const cp = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");

const DUAL_PROJECT_RX = /(?:odoo(\d+)[\\/]projects|ProjectsOdoo(\d+))[\\/]([^\\/]+)/i;
const FILE_LINE_RX = /^\s*([\w./\\-]+\.(?:py|xml|csv|js|scss|po|yml|txt)):(\d+):\s*(.*)$/;

let output, statusItem, diagnostics, stepsTree, actionsTree, child = null;
const state = { steps: [], phases: [], logPath: null, result: null, project: null, mode: null, started: null, serverRunning: false };

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

function getPythonExe(project) {
  const cfg = vscode.workspace.getConfiguration("odooCi");
  const configured = cfg.get("python");
  if (configured && configured.trim()) return configured.trim();

  const pyCfg = vscode.workspace.getConfiguration("python");
  const pyPath = pyCfg.get("defaultInterpreterPath");
  if (pyPath && pyPath.trim() && fs.existsSync(pyPath.trim())) return pyPath.trim();

  if (project && project.version) {
    const venvPy = `C:\\odoo\\odoo${project.version}\\venv\\Scripts\\python.exe`;
    if (fs.existsSync(venvPy)) return venvPy;
  }
  return "python";
}

function getCliPath(project) {
  const cfg = vscode.workspace.getConfiguration("odooCi");
  const configured = cfg.get("cliPath");
  if (configured && configured.trim()) return configured.trim();

  if (project && project.version) {
    const defaultCli = `C:\\odoo\\odoo${project.version}\\tools\\cli.py`;
    if (fs.existsSync(defaultCli)) return defaultCli;
  }
  return "C:\\odoo\\odoo19\\tools\\cli.py";
}

function checkPortOpen(port, cb) {
  const req = http.get(`http://127.0.0.1:${port}/web/health`, (res) => {
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

  getChildren(el) {
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

    if (el._kind === "server") {
      const statusItem = new vscode.TreeItem(state.serverRunning ? "Server: Running" : "Server: Stopped", vscode.TreeItemCollapsibleState.None);
      statusItem.iconPath = state.serverRunning ? new vscode.ThemeIcon("pass", new vscode.ThemeColor("testing.iconPassed")) : new vscode.ThemeIcon("circle-outline");
      statusItem.description = `localhost:${port}`;

      const runItem = new vscode.TreeItem("Start Odoo Server", vscode.TreeItemCollapsibleState.None);
      runItem.iconPath = new vscode.ThemeIcon("play");
      runItem.command = { command: "odooCi.serverRun", title: "Start Server" };

      const stopItem = new vscode.TreeItem("Stop Odoo Server", vscode.TreeItemCollapsibleState.None);
      stopItem.iconPath = new vscode.ThemeIcon("stop-circle");
      stopItem.command = { command: "odooCi.serverStop", title: "Stop Server" };

      const openItem = new vscode.TreeItem("Open in Browser", vscode.TreeItemCollapsibleState.None);
      openItem.iconPath = new vscode.ThemeIcon("globe");
      openItem.command = { command: "odooCi.serverOpen", title: "Open in Browser" };

      return [statusItem, runItem, stopItem, openItem];
    }

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

      return [staticItem, fastTestItem, xpathItem, fullItem];
    }

    if (el._kind === "tools") {
      const wizardItem = new vscode.TreeItem("Project Setup Wizard", vscode.TreeItemCollapsibleState.None);
      wizardItem.iconPath = new vscode.ThemeIcon("gear");
      wizardItem.command = { command: "odooCi.setupWizard", title: "Setup Wizard" };

      const initDbItem = new vscode.TreeItem("Initialize Fast Test DB", vscode.TreeItemCollapsibleState.None);
      initDbItem.iconPath = new vscode.ThemeIcon("database");
      initDbItem.command = { command: "odooCi.initFastDb", title: "Init Fast DB" };

      const snapSaveItem = new vscode.TreeItem("Take Instant DB Snapshot (~1s)", vscode.TreeItemCollapsibleState.None);
      snapSaveItem.iconPath = new vscode.ThemeIcon("camera");
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
    statusItem.command = "odooCi.steps.focus";
    statusItem.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
  } else {
    statusItem.text = `$(beaker) ${projLabel}`;
    statusItem.tooltip = "Odoo Studio & CI. Click for menu.";
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
  term.sendText(`& "${python}" "${bin}" ${confArg} --dev=all`);
  vscode.window.showInformationMessage(`Starting Odoo ${project.version} server for ${project.name} on port ${port}...`);
  setTimeout(pollServerStatus, 2500);
}

function stopServer() {
  const project = findProject();
  if (!project) return;
  const term = vscode.window.terminals.find(t => t.name === serverTerminalName(project));
  if (term) {
    term.dispose();
  }
  const port = getProjectPort(project);
  cp.exec(`powershell -Command "Get-NetTCPConnection -LocalPort ${port} -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"`, () => {
    vscode.window.showInformationMessage(`Stopped Odoo server on port ${port}.`);
    setTimeout(pollServerStatus, 1000);
  });
}

function openServer() {
  const project = findProject();
  const port = getProjectPort(project);
  vscode.env.openExternal(vscode.Uri.parse(`http://localhost:${port}`));
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
    term.sendText(`& "${python}" "${cli}" test -m "${addon}" --fast`);
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
    term.sendText(`& "${python}" "${cli}" new-addon "${addon}"`);
  });
}

function runDoctor() {
  const project = findProject();
  if (!project) return;
  const term = vscode.window.createTerminal({ name: "Odoo-AI Doctor" });
  term.show();
  const python = getPythonExe(project);
  const cli = getCliPath(project);
  term.sendText(`& "${python}" "${cli}" doctor`);
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
    term.sendText(`& "${python}" "${cli}" db-snap save "${label}"`);
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
    term.sendText(`& "${python}" "${cli}" db-snap restore "${label}"`);
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
      term.sendText(`& "${python}" "${cli}" i18n scan -m "${mod}"${langFlag}`);
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
  term.sendText(`& "${python}" "${fastScript}" init -v "${ver}"`);
}

function pollServerStatus() {
  const project = findProject();
  if (!project) return;
  const port = getProjectPort(project);
  checkPortOpen(port, (isOpen) => {
    state.serverRunning = isOpen;
    actionsTree.refresh();
  });
}

// ------------------------------------------------------------- activation

function activate(context) {
  output = vscode.window.createOutputChannel("Odoo CI");
  diagnostics = vscode.languages.createDiagnosticCollection("odoo-ci");
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 25);
  stepsTree = new StepsProvider();
  actionsTree = new ActionsProvider();

  context.subscriptions.push(
    output,
    diagnostics,
    statusItem,
    vscode.window.registerTreeDataProvider("odooCi.steps", stepsTree),
    vscode.window.registerTreeDataProvider("odooCi.actions", actionsTree),
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
    vscode.commands.registerCommand("odooCi.refresh", () => { pollServerStatus(); actionsTree.refresh(); }),
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
        { label: "$(camera) Take Instant DB Snapshot (~1s)", action: "dbSnapSave" },
        { label: "$(history) Restore DB Snapshot (~2s)", action: "dbSnapRestore" },
        { label: "$(comment-discussion) Scan Translations (i18n)", action: "i18nScan" },
        { label: "$(checklist) Run static checks (seconds)", action: "static" },
        { label: "$(beaker) Run full CI (install and test)", action: "full" },
        { label: "$(add) Scaffold New Addon", action: "newAddon" },
        { label: "$(pulse) Run Doctor Health Check", action: "doctor" },
      ];
      vscode.window.showQuickPick(items).then((choice) => {
        if (!choice) return;
        vscode.commands.executeCommand(`odooCi.${choice.action}`);
      });
    })
  );

  setStatus("ready");
  pollServerStatus();
  setInterval(pollServerStatus, 5000);
}

function deactivate() {
  if (child) child.kill();
}

module.exports = { activate, deactivate };
