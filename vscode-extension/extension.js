// Cockpit Odoo (local): Server control, template DB fast test, module scaffold & CI gates.
"use strict";

const vscode = require("vscode");
const cp = require("child_process");
const terminalCommands = require("./terminal_commands");
const path = require("path");
const fs = require("fs");
const { RuleFixes, fixLenSearch } = require("./rulefixes");
const { moduleOfFile } = require("./graph");
const { findProject, getProjectPort, getOdooRoot, getPythonExe, getCliPath, toolsStatus } = require("./project");
const { splitCommandLine, parseOdooCommand, listOdooProcesses, detectServers, killTree } = require("./detect");

const FILE_LINE_RX = /^\s*([\w./\\-]+\.(?:py|xml|csv|js|scss|po|yml|txt)):(\d+):\s*(.*)$/;

let issuesTree;
let output, statusItem, serverItem, diagnostics, stepsTree, actionsTree, envTree, child = null;
let extContext = null;
let cockpit = null;
let translations = null;
const state = { steps: [], phases: [], logPath: null, result: null, project: null, mode: null, started: null, serverRunning: false, servers: [], portOpen: false };

/** The cockpit's bottom panel, on one of its pages. A window that does not know the panel yet gets the tab and the reload offer. */
async function showCockpitPage(extensionPath, tab) {
  await openCockpitInPanel(extensionPath);
  for (let i = 0; i < 20 && !cockpit.isViewOpen() && !cockpit.isOpen(); i++) await new Promise((resolve) => setTimeout(resolve, 150));
  cockpit.showTab(tab || "home");
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
      : "Cockpit Odoo. Click for menu.";
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

const serverRun = { proc: null, db: null, log: null, healthBusy: false, driftChecked: false, notified: "" };
const { ActionsProvider, EnvProvider, versionInfo } = require("./trees").create({
  state, serverRun, findProject, getProjectPort, getOdooRoot, toolsStatus, progressLine: (p) => progressLine(p),
  getCockpit: () => cockpit, isCiRunning: () => !!child,
});
const issuesApi = require("./issues").create({ state, serverRun });
const { IssuesProvider, issueFilter, issueCursor, issuesSource, setIssuesSource, visibleIssues, occurrences, shortText, issueOf, openLogAt, openSourceAt,
  openIssue, stepIssue, pickIssue, filterIssues, decorateVisibleLogs } = issuesApi;
let serverOutput, serverDiagnostics;

// The server control (server.js) reads and sets these through `ctx`, so it always sees the current value.
const serverCtx = {
    get actionsTree() { return actionsTree; }, set actionsTree(x) { actionsTree = x; },
    get child() { return child; }, set child(x) { child = x; },
    get cockpit() { return cockpit; }, set cockpit(x) { cockpit = x; },
    get diagnostics() { return diagnostics; }, set diagnostics(x) { diagnostics = x; },
    get extContext() { return extContext; }, set extContext(x) { extContext = x; },
    get output() { return output; }, set output(x) { output = x; },
    get serverDiagnostics() { return serverDiagnostics; }, set serverDiagnostics(x) { serverDiagnostics = x; },
    get serverItem() { return serverItem; }, set serverItem(x) { serverItem = x; },
    get serverOutput() { return serverOutput; }, set serverOutput(x) { serverOutput = x; },
};
const {
  backupBeforeUpgrade, backupDatabase, checkDrift, ciView, cliCommand, dropDatabase, duration, focusServer, freePort, getBackupDir, launched, listDatabases, openServer, pollServerStatus, progressLine, remembered, restartServer, runCli, runExtensionCommand, runSetupWizard, runXPathCheck, serverArgs, serverLogPath, setBackupBeforeUpgrade, showHealth, startServer, startServerQuick, startServerWith, stopServer, stopServerAt, upgradeDatabase,
} = require("./server").create({
  state, serverRun, addDiagnostic, focusIssues, pendingDiagnostics, run, serverTerminalName, setIssuesSource,
  runInitFastDb: (...args) => runInitFastDb(...args), ctx: serverCtx,
});

// ------------------------------------------------------------- shell-aware command line

// ------------------------------------------------------------- versions & environment

const VERSIONS = ["17", "18", "19", "20"];

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

/**
 * The Cockpit in the bottom panel. The panel's container is read by the editor when the window starts, so a window
 * that has not been reloaded since the extension was updated does not know it: the Cockpit then opens as an editor tab
 * and the person is told why, instead of seeing "command not found".
 */
async function openCockpitInPanel(extensionPath) {
  try {
    await vscode.commands.executeCommand("workbench.view.extension.odooCockpit");
  } catch (error) {
    cockpit.open(extensionPath);   // it works as an editor tab right now; the panel needs a reload, offered quietly in the status bar
    offerReload("Reload to finish the update");
  }
}

/** A quiet status-bar item instead of a pop-up: nobody is interrupted, and one click reloads the window. */
let reloadItem = null;
function offerReload(text) {
  if (!reloadItem) {
    reloadItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 0);
    reloadItem.command = "workbench.action.reloadWindow";
    reloadItem.tooltip = "Cockpit Odoo was updated: this window still shows the old interface. Click to reload it.";
  }
  reloadItem.text = "$(refresh) " + text;
  reloadItem.show();
}

function openLayoutGuide() {
  const { root } = toolsContext();
  const guide = path.join(root, "odoo19", "tools", "docs", "LAYOUT.md");
  if (fs.existsSync(guide)) vscode.commands.executeCommand("markdown.showPreview", vscode.Uri.file(guide));
  else vscode.window.showWarningMessage(`Not found: ${guide}`);
}

// ------------------------------------------------------------- terminal commands

const { cliLine, runInTerminal, runFastTest, runNewAddon, runDoctor, runAudit, runDbSnapSave, runDbSnapRestore, runI18nScan, runInitFastDb, runSelfTest, runSyncVersions, runSecretScan, runSecretHook, runGates } = terminalCommands.create({
  findProject, getPythonExe, getCliPath, toolsContext, moduleOfFile: (file) => moduleOfFile(file),
});

// ------------------------------------------------------------- activation

/** Run a registration; if the editor refuses it, remember why and carry on - the rest of the extension still works. */
const startupProblems = [];
function guarded(what, make) {
  try {
    return make();
  } catch (error) {
    startupProblems.push({ what, message: String((error && error.message) || error) });
    return { dispose() {} };
  }
}

/** Say once, clearly, what to do. The usual cause: the extension was updated but the window was not reloaded. */
function reportStartupProblems() {
  if (!startupProblems.length) return;
  for (const p of startupProblems) output.appendLine(`[startup] ${p.what}: ${p.message}`);
  const stale = startupProblems.some((p) => /No view is registered|not registered|unknown view/i.test(p.message));
  if (stale) return offerReload("Reload to finish the update");   // no pop-up: the cause is known and one click fixes it
  vscode.window.showWarningMessage(`Cockpit Odoo started with ${startupProblems.length} problem(s): ${startupProblems.map((p) => p.what).join(", ")}. See the 'Odoo CI' output.`);
}

/** Focus the Errors & Warnings view; if this window does not know it yet, offer the picker instead of an error. */
async function focusIssues() {
  try {
    await vscode.commands.executeCommand("odooCi.issues.focus");
  } catch (error) {
    await pickIssue();
  }
}

function activate(context) {
  extContext = context;
  output = vscode.window.createOutputChannel("Odoo CI");
  serverOutput = vscode.window.createOutputChannel("Odoo Server", "log");
  serverDiagnostics = vscode.languages.createDiagnosticCollection("odoo-server");
  diagnostics = vscode.languages.createDiagnosticCollection("odoo-ci");
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 25);
  serverItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 24);
  stepsTree = { refresh: () => actionsTree.refresh() };   // one tree now: the progress is part of the control panel
  actionsTree = new ActionsProvider();
  envTree = new EnvProvider();
  issuesTree = issuesApi.createTree();
  translations = require("./translations").create({ findProject, cliCommand }, context);
  cockpit = require("./cockpit").create({
    findProject, getProjectPort, state, serverRun, issuesSource, runCli, serverArgs,
    startServerQuick, startServer, stopServer, restartServer, openServer: (port) => openServer(port),
    focusServer, stopServerAt, launchedPorts: () => [...new Set([...[...launched].filter(([, i]) => !i.exited).map(([p]) => p), ...state.servers.filter((x) => x.logfile && fs.existsSync(x.logfile)).map((x) => x.port)])],
    upgradeDatabase, checkDrift, backupDatabase, dropDatabase, openLogAt, openSourceAt, focusIssues, pickIssue,
    getBackupDir, backupBeforeUpgrade, setBackupBeforeUpgrade,
    listDatabases,
    extensionPath: context.extensionPath,
    cliCommand,
    cliPath: getCliPath, pythonExe: getPythonExe,
    runCommand: runExtensionCommand, ci: ciView, graphChanged: () => actionsTree.refresh(), tools: toolsStatus,
    odooRoot: (project) => path.join(getOdooRoot(project), `odoo${project.version}`),
    openFile: (file) => vscode.window.showTextDocument(vscode.Uri.file(file), { preview: true }),
    activeFile: () => { const e = vscode.window.activeTextEditor; return e && e.document.uri.scheme === "file" ? e.document.uri.fsPath : null; },
    // the graph is left fresh quietly when the project opens, as the workspace task did; off in the test editor, and by a setting
    autoUpdateEnabled: () => !process.env.ODOO_CI_TEST_OUT && vscode.workspace.getConfiguration("odooCi").get("graph.autoUpdate") !== false,
    maintainerTools: () => !!vscode.workspace.getConfiguration("odooCi").get("maintainerTools"),
    keepGenerations: () => Number(vscode.workspace.getConfiguration("odooCi").get("graph.keepGenerations")) || 0,
    remembered: (key) => extContext && extContext.workspaceState.get(`odooCi.remote.${key}`),
    remember: (key, value) => extContext && extContext.workspaceState.update(`odooCi.remote.${key}`, value),
  });

  context.subscriptions.push(
    output,
    serverOutput,
    serverDiagnostics,
    diagnostics,
    statusItem,
    serverItem,
    guarded("view odooCi.actions", () => vscode.window.registerTreeDataProvider("odooCi.actions", actionsTree)),
    guarded("view odooCi.issues", () => vscode.window.registerTreeDataProvider("odooCi.issues", issuesTree)),
    guarded("log painting", () => vscode.window.onDidChangeVisibleTextEditors(decorateVisibleLogs)),
    { dispose: issuesApi.dispose },
    guarded("view odooCi.env", () => vscode.window.registerTreeDataProvider("odooCi.env", envTree)),
    vscode.languages.registerCodeActionsProvider([{ language: "python" }, { language: "xml" }], new RuleFixes(),
      { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] }),
    vscode.commands.registerCommand("odooCi.newVersion", runNewVersion),
    vscode.commands.registerCommand("odooCi.selfTest", runSelfTest),
    vscode.commands.registerCommand("odooCi.syncVersions", runSyncVersions),
    vscode.commands.registerCommand("odooCi.secretScan", runSecretScan),
    vscode.commands.registerCommand("odooCi.secretHook", runSecretHook),
    vscode.commands.registerCommand("odooCi.gates", runGates),
    vscode.commands.registerCommand("odooCi.openLayout", openLayoutGuide),
    vscode.commands.registerCommand("odooCi.translations", (uri) => translations.open(context.extensionPath, uri)),
    vscode.commands.registerCommand("odooCi.refreshEnv", () => envTree.refresh()),
    vscode.commands.registerCommand("odooCi.static", () => run("static")),
    vscode.commands.registerCommand("odooCi.full", () => run("full")),
    vscode.commands.registerCommand("odooCi.fullStop", () => run("fullStop")),
    vscode.commands.registerCommand("odooCi.serverRun", startServer),
    vscode.commands.registerCommand("odooCi.serverRunQuick", startServerQuick),
    vscode.commands.registerCommand("odooCi.serverStop", stopServer),
    vscode.commands.registerCommand("odooCi.serverRestart", restartServer),
    vscode.commands.registerCommand("odooCi.serverUpgrade", () => upgradeDatabase()),
    vscode.commands.registerCommand("odooCi.serverDrift", () => checkDrift()),
    vscode.commands.registerCommand("odooCi.serverHealth", showHealth),
    vscode.commands.registerCommand("odooCi.issueOpen", openIssue),
    vscode.commands.registerCommand("odooCi.issueOpenLog", (arg) => { const i = issueOf(arg); return i && openLogAt(i.line); }),
    vscode.commands.registerCommand("odooCi.issueOpenSource", (arg) => { const i = issueOf(arg); return i && i.location && openSourceAt(i.location); }),
    vscode.commands.registerCommand("odooCi.cockpit", () => openCockpitInPanel(context.extensionPath)),
    guarded("view odooCi.cockpitView", () => vscode.window.registerWebviewViewProvider("odooCi.cockpitView", cockpit.viewProvider(context.extensionPath), { webviewOptions: { retainContextWhenHidden: true } })),
    vscode.commands.registerCommand("odooCi.cockpitPanel", () => openCockpitInPanel(context.extensionPath)),
    vscode.commands.registerCommand("odooCi.cockpitTab", (tab) => showCockpitPage(context.extensionPath, typeof tab === "string" ? tab : "home")),
    vscode.commands.registerCommand("odooCi.graphUpdate", () => cockpit.graph.update()),
    vscode.commands.registerCommand("odooCi.graphModule", async () => {   // the Graph page, on the module of the file being edited
      await showCockpitPage(context.extensionPath, "graph");
      const editor = vscode.window.activeTextEditor;
      await cockpit.graph.follow(true);
      if (editor && editor.document.uri.scheme === "file") await cockpit.graph.followFile(editor.document.uri.fsPath);
    }),
    vscode.commands.registerCommand("odooCi.issueShow", focusIssues),
    vscode.commands.registerCommand("odooCi.issueNext", () => stepIssue(1)),
    vscode.commands.registerCommand("odooCi.issuePrev", () => stepIssue(-1)),
    vscode.commands.registerCommand("odooCi.issuePick", pickIssue),
    vscode.commands.registerCommand("odooCi.issueFilter", filterIssues),
    vscode.commands.registerCommand("odooCi.openServerLog", () => openLogAt(0)),
    vscode.commands.registerCommand("odooCi.serverOpen", openServer),
    vscode.commands.registerCommand("odooCi.fastTest", runFastTest),
    vscode.commands.registerCommand("odooCi.xpathCheck", runXPathCheck),
    vscode.commands.registerCommand("odooCi.dbSnapSave", runDbSnapSave),
    vscode.commands.registerCommand("odooCi.dbSnapRestore", runDbSnapRestore),
    vscode.commands.registerCommand("odooCi.i18nScan", runI18nScan),
    vscode.commands.registerCommand("odooCi.newAddon", runNewAddon),
    vscode.commands.registerCommand("odooCi.doctor", runDoctor),
    vscode.commands.registerCommand("odooCi.audit", runAudit),
    vscode.commands.registerCommand("odooCi.openGuide", () => vscode.commands.executeCommand("markdown.showPreview", vscode.Uri.file(path.join(context.extensionPath, "README.md")))),
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
        { label: "$(database) Initialize Fast Test DB", action: "initFastDb" },
        { label: "$(dashboard) Open the Cockpit", action: "cockpit" },
        { label: "$(play) Start Odoo Server", action: "serverRunQuick" },
        { label: "$(settings-gear) Start Odoo Server with Options...", action: "serverRun" },
        { label: "$(sync) Upgrade Modules...", action: "serverUpgrade" },
        { label: "$(globe) Open in Browser", action: "serverOpen" },
        { label: "$(stop-circle) Stop Odoo Server", action: "serverStop" },
        { label: "$(zap) Fast DB Test", action: "fastTest" },
        { label: "$(check) Validate XML Views & XPath", action: "xpathCheck" },
        { label: "$(device-camera) Take Instant DB Snapshot", action: "dbSnapSave" },
        { label: "$(history) Restore DB Snapshot", action: "dbSnapRestore" },
        { label: "$(comment-discussion) Scan Translations (i18n)", action: "i18nScan" },
        { label: "$(checklist) Run static checks", action: "static" },
        { label: "$(beaker) Run full CI (install and test)", action: "full" },
        { label: "$(add) Scaffold New Addon", action: "newAddon" },
        { label: "$(pulse) Run Doctor Health Check", action: "doctor" },
        { label: "$(rocket) Set up an Odoo version", action: "newVersion" },
        ...(vscode.workspace.getConfiguration("odooCi").get("maintainerTools") ? [
          { label: "$(beaker) Tools CI (before push)", action: "selfTest" },
          { label: "$(git-compare) Version branches vs main", action: "syncVersions" }] : []),
        { label: "$(shield) Scan staged files for secrets", action: "secretScan" },
        { label: "$(globe) Translations: scan, update, export", action: "translations" },
        { label: "$(law) Project gates (Odoo rules)", action: "gates" },
      ];
      vscode.window.showQuickPick(items).then((choice) => {
        if (!choice) return;
        vscode.commands.executeCommand(`odooCi.${choice.action}`);
      });
    })
  );

  setStatus("ready");
  // a poll that fails is logged and tried again in five seconds; it never becomes an unhandled error
  const poll = () => pollServerStatus().catch((error) => output.appendLine(`[poll] ${(error && error.message) || error}`));
  poll();
  const timer = setInterval(poll, 5000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
  // the map of the module of the file being edited: the Graph page follows the editor (unless the person pinned a module)
  let followTimer = null;
  const followEditor = (editor) => {
    if (!editor || editor.document.uri.scheme !== "file" || vscode.workspace.getConfiguration("odooCi").get("graph.followEditor") === false) return;
    clearTimeout(followTimer);
    followTimer = setTimeout(() => { cockpit.graph.followFile(editor.document.uri.fsPath).catch(() => {}); }, 400);   // not on every tab flick
  };
  context.subscriptions.push(guarded("follow the editor", () => vscode.window.onDidChangeActiveTextEditor(followEditor)), { dispose: () => clearTimeout(followTimer) });
  followEditor(vscode.window.activeTextEditor);
  const graphTimer = setTimeout(() => {   // after the window has settled
    cockpit.graph.autoUpdate().then(() => (cockpit.graph.state().status || process.env.ODOO_CI_TEST_OUT ? null : cockpit.graph.refresh())).catch(() => {});
  }, 8000);
  graphTimer.unref();
  context.subscriptions.push({ dispose: () => clearTimeout(graphTimer) });
  reportStartupProblems();
  return { _test: { cockpit, translations, issuesTree, actionsTree, envTree, issues: module.exports._test.issues, startupProblems,
    multi: { listOdooProcesses, pollServerStatus, startServerWith, launched, freePort, focusServer, stopServerAt, state, serverRun, detectServers, findProject } } };
}

function deactivate() {
  if (child) child.kill();
}

module.exports = { activate, deactivate, _test: { fixLenSearch, parseOdooCommand, splitCommandLine, detectServers, listOdooProcesses, killTree,
    startServerWith, launched, freePort, focusServer, stopServerAt, serverState: state, serverRun, serverLogPath,
    issues: { IssuesProvider, setIssuesSource, visibleIssues, occurrences, issueFilter, issueCursor, progressLine, duration, shortText, state } } };
