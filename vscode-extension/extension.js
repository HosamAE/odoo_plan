// Odoo CI (local): the project's CI as a view, a status-bar button and Problems.
//
// The work is done by `odoo-ai ci` of the project's Odoo version
// (C:/odoo/odoo<NN>/tools/ci_local.py: every version has its own tools); this
// extension starts it with ODOO_AI_CI_EVENTS=1, reads the "::ci::<json>" lines
// it prints for every step, and draws them. Everything else it prints goes to
// the "Odoo CI" output channel unchanged.
//
// No setup per workspace: the project is found from the folder path
// (...\odoo<NN>\projects\<project>), the same rule odoo-ai uses.
"use strict";

const vscode = require("vscode");
const cp = require("child_process");
const path = require("path");

const PROJECT_RX = /odoo(\d+)[\\/]projects[\\/]([^\\/]+)/i;
const FILE_LINE_RX = /^\s*([\w./\\-]+\.(?:py|xml|csv|js|scss|po|yml|txt)):(\d+):\s*(.*)$/;

let output, statusItem, diagnostics, tree, child = null;
const state = { steps: [], phases: [], logPath: null, result: null, project: null, mode: null, started: null };

// ------------------------------------------------------------ the project

function findProject() {
  for (const folder of vscode.workspace.workspaceFolders || []) {
    const m = folder.uri.fsPath.match(PROJECT_RX);
    if (m) {
      const root = folder.uri.fsPath.slice(0, m.index + m[0].length);
      return { version: m[1], name: m[2], root };
    }
  }
  return null;
}

// ------------------------------------------------------------- the view

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
        if (state.result.blocked) {
          r.tooltip = "Stop the server, or use 'Run full CI, stopping running Odoo servers first'.";
          r.command = { command: "odooCi.fullStop", title: "Run with --stop-servers" };
        }
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
        const item = new vscode.TreeItem(line.trim(), vscode.TreeItemCollapsibleState.None);
        item.tooltip = line;
        const m = line.match(FILE_LINE_RX);
        if (m && state.project) {
          const file = vscode.Uri.file(path.join(state.project.root, m[1]));
          item.iconPath = new vscode.ThemeIcon("go-to-file");
          item.command = { command: "vscode.open", title: "Open", arguments: [file, { selection: new vscode.Range(+m[2] - 1, 0, +m[2] - 1, 0) }] };
        } else {
          item.iconPath = new vscode.ThemeIcon("dash");
        }
        return item;
      });
    }
    return [];
  }
}

// -------------------------------------------------------- the status bar

function setStatus(kind, text) {
  statusItem.backgroundColor = undefined;
  if (kind === "idle") {
    statusItem.text = "$(beaker) CI";
    statusItem.tooltip = "Odoo CI: run the project's checks before pushing";
  } else if (kind === "running") {
    statusItem.text = `$(sync~spin) CI: ${text}`;
    statusItem.tooltip = "Odoo CI is running - click to choose (cancel)";
  } else if (kind === "ok") {
    statusItem.text = `$(pass) CI ${text}`;
    statusItem.tooltip = "Odoo CI passed - click to run again";
  } else {
    statusItem.text = `$(error) CI ${text}`;
    statusItem.tooltip = "Odoo CI failed - click for the details";
    statusItem.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
  }
  statusItem.show();
}

// --------------------------------------------------------------- running

function onEvent(ev) {
  switch (ev.type) {
    case "start":
      state.mode = ev.mode;
      break;
    case "phase":
      if (!state.phases.find((p) => p.phase === ev.phase)) state.phases.push({ phase: ev.phase, name: ev.name });
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
  tree.refresh();
}

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

function run(mode) {
  if (child) {
    vscode.window.showWarningMessage("Odoo CI is already running.");
    return;
  }
  const project = findProject();
  if (!project) {
    vscode.window.showErrorMessage("Odoo CI: this workspace is not inside odoo<NN>\\projects\\<project>.");
    return;
  }
  const cfg = vscode.workspace.getConfiguration("odooCi");
  // The project's own venv has what odoo-ai needs (PyYAML, pyflakes...); a bare
  // `python` is whatever VS Code's PATH finds, which may not.
  const python = cfg.get("python") || `C:/odoo/odoo${project.version}/venv/Scripts/python.exe`;
  // Each Odoo version has its own tools: C:/odoo/odoo<NN>/tools.
  const cli = cfg.get("cliPath") || `C:/odoo/odoo${project.version}/tools/cli.py`;
  const args = [cli, "ci"];
  if (mode === "static") args.push("--static-only");
  if (mode === "fullStop") args.push("--stop-servers");

  Object.assign(state, { steps: [], phases: [], logPath: null, result: null, project, mode: mode === "static" ? "static" : "full", started: Date.now() });
  diagnostics.clear();
  pendingDiagnostics.clear();
  tree.refresh();
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
    tree.refresh();
    const label = state.mode === "full" ? "full" : "static";
    if (state.result.passed) {
      setStatus("ok", label);
      vscode.window.showInformationMessage(
        state.mode === "full" ? `Odoo CI passed (${minutes} min) - safe to push.` : "Odoo CI: static checks passed.");
    } else {
      setStatus("fail", label);
      const actions = ["Show details", "Show output"].concat(state.logPath ? ["Open log"] : []);
      const msg = state.result.blocked
        ? "Odoo CI: an Odoo server is running and would write to the test database."
        : "Odoo CI failed - fix before pushing.";
      const extra = state.result.blocked ? ["Run and stop servers"] : [];
      vscode.window.showErrorMessage(msg, ...extra.concat(actions)).then((pick) => {
        if (pick === "Show details") vscode.commands.executeCommand("odooCi.steps.focus");
        if (pick === "Show output") output.show(true);
        if (pick === "Open log") vscode.commands.executeCommand("odooCi.openLog");
        if (pick === "Run and stop servers") run("fullStop");
      });
    }
  });
  vscode.commands.executeCommand("odooCi.steps.focus");
}

function cancel() {
  if (!child) return;
  // The run spawns odoo-bin and bash: end the whole tree.
  cp.exec(`taskkill /PID ${child.pid} /T /F`);
  output.appendLine("[odoo-ci] cancelled");
}

async function menu() {
  const items = child
    ? [{ label: "$(debug-stop) Cancel the running check", id: "cancel" }, { label: "$(output) Show the output", id: "output" }]
    : [
        { label: "$(checklist) Static checks", description: "seconds", detail: "Every static step of ci.yml, as GitHub runs it", id: "static" },
        { label: "$(beaker) Full CI", description: "about 20 minutes", detail: "Fresh database, every module installed, every test, the log gate", id: "full" },
        { label: "$(beaker) Full CI, stopping running Odoo servers first", description: "about 20 minutes", detail: "Their crons would write to the test database", id: "fullStop" },
        { label: "$(output) Show the last output", id: "output" },
      ];
  const pick = await vscode.window.showQuickPick(items, { placeHolder: "Odoo CI" });
  if (!pick) return;
  if (pick.id === "cancel") cancel();
  else if (pick.id === "output") output.show(true);
  else run(pick.id);
}

// -------------------------------------------------------------- activate

function activate(context) {
  output = vscode.window.createOutputChannel("Odoo CI");
  diagnostics = vscode.languages.createDiagnosticCollection("odoo-ci");
  tree = new StepsProvider();
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  statusItem.command = "odooCi.menu";

  context.subscriptions.push(
    output, diagnostics, statusItem,
    vscode.window.registerTreeDataProvider("odooCi.steps", tree),
    vscode.commands.registerCommand("odooCi.static", () => run("static")),
    vscode.commands.registerCommand("odooCi.full", () => run("full")),
    vscode.commands.registerCommand("odooCi.fullStop", () => run("fullStop")),
    vscode.commands.registerCommand("odooCi.cancel", cancel),
    vscode.commands.registerCommand("odooCi.menu", menu),
    vscode.commands.registerCommand("odooCi.showOutput", () => output.show(true)),
    vscode.commands.registerCommand("odooCi.openLog", () => {
      if (state.logPath) vscode.window.showTextDocument(vscode.Uri.file(state.logPath));
      else vscode.window.showInformationMessage("Odoo CI: no test log yet - run the full CI first.");
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => refreshVisibility()),
  );
  vscode.commands.executeCommand("setContext", "odooCi.running", false);
  refreshVisibility();
}

function refreshVisibility() {
  if (findProject()) setStatus("idle");
  else statusItem.hide();
}

function deactivate() {
  cancel();
}

module.exports = { activate, deactivate };
