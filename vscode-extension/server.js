"use strict";
// Server control: pick a database, start/stop/restart servers, upgrade, back up, drop, the health of a running server
// tied to the code, and the CI/XPath/setup runners that sit beside them. Split out of extension.js unchanged. The logic
// is in tools/server_ctl.py (`odoo-ai server ...`); this is the screen on it.
// `ctx` reaches the extension's few variables that are assigned over time (output channels, status item, cockpit, the CI
// child process, ...) so that this file always sees the current value.
const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const path = require("path");
const http = require("http");
const { findProject, getProjectPort, getOdooRoot, getPythonExe, getCliPath, checkPortOpen, findConf } = require("./project");
const { detectServers, killTree } = require("./detect");

/** deps: addDiagnostic, focusIssues, pendingDiagnostics, run, runInitFastDb, serverRun, serverTerminalName, setIssuesSource, state, ctx */
function create({ addDiagnostic, focusIssues, pendingDiagnostics, run, runInitFastDb, serverRun, serverTerminalName, setIssuesSource, state, ctx }) {
  // ------------------------------------------------------------- server control: pick, start, upgrade, judge
  //
  // The logic lives in tools/server_ctl.py (`odoo-ai server ...`) where it is tested; this is the screen on it.
  // A server started here writes its log to a file, so its health can be read, judged and tied to the code.


  function confPath(project) {
    return findConf(project);   // <name>.conf first (the layout of these tools, with its own xxyy port), else odoo.conf
  }

  /** The name a project's log files start with: its name and its Odoo version. */
  function logStem(project) {
    return project.version ? `${project.name}.${project.version}` : project.name;
  }

  function serverLogPath(project, port) {
    const dir = ctx.extContext && ctx.extContext.globalStorageUri ? ctx.extContext.globalStorageUri.fsPath : require("os").tmpdir();
    fs.mkdirSync(dir, { recursive: true });
    // The version is part of the name: one project name on Odoo 19 and on Odoo 20 is two projects, and they must not read each other's log.
    return path.join(dir, `${logStem(project)}.server${port ? `.${port}` : ""}.log`);
  }

  /** The servers this window started, by port: {proc, db, log, exited}. Several can run at once, each with its own log. */
  const launched = new Map();

  /** The first port from the project's own that nothing answers on and nothing of ours is starting on. */
  async function freePort(project) {
    const taken = new Set((await detectServers(project)).map((s) => s.port));   // every project's: a port is a port
    for (const [port, info] of launched) if (!info.exited) taken.add(port);
    let port = getProjectPort(project);
    while (taken.has(port)) port += 1;
    return port;
  }

  /** Make this server the one the Server, Log and Errors pages are about. */
  function focusServer(port, automatic) {
    const seen = state.servers.find((s) => s.port === port);
    const known = launched.get(port);
    const info = known && (!known.exited || !seen) ? known : null;   // an old entry of a server that ended does not speak for whatever runs on that port now
    state.focusPort = port;
    const outside = !info && seen && seen.logfile && fs.existsSync(seen.logfile) ? seen.logfile : null;   // started elsewhere, but writing a log we can read
    Object.assign(serverRun, { db: (info && info.db) || (seen && seen.db) || null, log: info ? info.log : outside, driftChecked: false, notified: "", signature: null });
    if (ctx.serverDiagnostics) ctx.serverDiagnostics.clear();
    // picked by the program (nothing was followed yet), not by the person: what they are looking at stays as it is
    if (!automatic || !state.health) state.health = info ? { state: info.exited ? "failed" : "starting" } : (outside ? { state: "starting" } : null);
    if (!automatic) state.issuesSource = null;
    setTimeout(pollServerStatus, 100);
    if (ctx.cockpit) ctx.cockpit.refresh();
  }

  /** Stop ONE server, by its port; the others keep running. */
  async function stopServerAt(port) {
    const project = findProject();
    if (!project) return;
    const targets = (await detectServers(project)).filter((s) => s.port === port && s.mine);   // only this project's
    for (const s of targets) await killTree(s.pid);
    const info = launched.get(port);
    if (info && info.proc && !info.exited) { try { info.proc.kill(); } catch (e) { /* already gone */ } }
    vscode.window.showInformationMessage(targets.length || info ? `Stopped the server on port ${port}.` : `No server found on port ${port}.`);
    setTimeout(pollServerStatus, 1000);
  }

  /** The program and arguments that run `odoo-ai <args>` for a project (for a caller that wants to stream its output). */
  function cliCommand(project, args) {
    const cliPath = getCliPath(project);
    const isScript = /\.py$/i.test(cliPath);
    return { file: isScript ? getPythonExe(project) : cliPath, args: isScript ? [cliPath, ...args] : args };
  }

  /** Run `odoo-ai <args>` and collect it: {code, stdout, stderr}. Never rejects. */
  function runCli(project, args, timeout = 600000) {
    return new Promise((resolve) => {
      const cli = getCliPath(project);
      const isScript = /\.py$/i.test(cli);
      cp.execFile(isScript ? getPythonExe(project) : cli, isScript ? [cli, ...args] : args,
        { timeout, windowsHide: true, maxBuffer: 16 * 1024 * 1024, cwd: project.root },
        (err, stdout, stderr) => resolve({ code: err ? (typeof err.code === "number" ? err.code : 1) : 0, stdout: String(stdout || ""), stderr: String(stderr || "") }));
    });
  }

  function serverArgs(project, sub, extra) {
    const args = ["server", sub, "--conf", confPath(project), "--python", getPythonExe(project)];
    const bin = path.join(getOdooRoot(project), `odoo${project.version}`, "odoo", "odoo-bin");
    if (fs.existsSync(bin)) args.push("--odoo-bin", bin);   // from the setting or the layout, not from a fixed drive
    return [...args, ...extra];
  }

  async function listDatabases(project) {
    const res = await runCli(project, serverArgs(project, "dbs", ["--json"]), 30000);
    try { return JSON.parse(res.stdout); } catch (e) { return { databases: [], error: "could not list databases" }; }
  }

  const LAST = "odooCi.serverChoices";

  function remembered(project) {
    return (ctx.extContext && ctx.extContext.workspaceState.get(`${LAST}.${project.name}`)) || {};
  }

  /** The database to use: one of the project's, the .conf's own, or a name typed in. undefined = cancelled. */
  async function pickDatabase(project, { required = false, title = "Database" } = {}) {
    const info = await listDatabases(project);
    const last = remembered(project).db;
    const items = [];
    if (!required) items.push({ label: "$(circle-slash) The .conf's own choice", description: info.conf_db ? `db_name = ${info.conf_db}` : "no -d: Odoo shows its database list", value: null });
    for (const name of info.databases || []) {
      items.push({ label: `$(database) ${name}`, description: name === last ? "used last time" : "", value: name });
    }
    // a database this machine cannot open is shown, apart and not pickable: it is the reason Odoo's own database
    // selector fails with an Internal Server Error, and picking it here would fail the same way
    if ((info.unusable || []).length) {
      items.push({ label: `Cannot be opened: ${info.unusable.map((u) => u.name).join(", ")}`, kind: vscode.QuickPickItemKind.Separator });
    }
    items.push({ label: "$(edit) Type a database name...", value: "__typed__" });
    if (info.error) vscode.window.showWarningMessage(`Could not list databases (${info.error}); type a name instead.`);
    const picked = await vscode.window.showQuickPick(items, { title, placeHolder: "Choose the database", matchOnDescription: true });
    if (!picked) return undefined;
    if (picked.value !== "__typed__") return { db: picked.value };
    const typed = await vscode.window.showInputBox({ prompt: "Database name", validateInput: (v) => (/^[\w.-]+$/.test(v) ? null : "letters, digits, _ . - only") });
    return typed ? { db: typed } : undefined;
  }

  /** What to upgrade: "" (nothing), "all", or "module,module". undefined = cancelled. */
  async function pickUpgrade({ allowNone = true } = {}) {
    const items = [];
    if (allowNone) items.push({ label: "$(close) No upgrade", value: "" });
    items.push({ label: "$(sync) Upgrade all modules", description: "-u all", value: "all" },
      { label: "$(package) Upgrade chosen modules...", description: "-u module,module", value: "__typed__" });
    const picked = await vscode.window.showQuickPick(items, { title: "Upgrade", placeHolder: "Upgrade modules before the server starts?" });
    if (!picked) return undefined;
    if (picked.value !== "__typed__") return picked.value;
    const typed = await vscode.window.showInputBox({ prompt: "Modules to upgrade, comma separated", placeHolder: "sale_extra,stock_extra",
      validateInput: (v) => (/^[a-z][a-z0-9_]*(\s*,\s*[a-z][a-z0-9_]*)*$/.test(v.trim()) ? null : "module names, separated by commas") });
    return typed ? typed.split(",").map((s) => s.trim()).join(",") : undefined;
  }

  /** Start Odoo with these choices, its output into a panel and a log file the health check reads. */
  async function startServerWith(project, choices) {
    const port = choices.port || getProjectPort(project);
    const args = serverArgs(project, "command", ["--json", "--port", String(port)]);
    if (choices.db) args.push("-d", choices.db);
    if (choices.update) args.push("-u", choices.update);
    if (choices.dev) args.push("--dev");
    const built = await runCli(project, args, 30000);
    let argv;
    try { argv = JSON.parse(built.stdout); } catch (e) { return vscode.window.showErrorMessage(`Could not build the start command: ${built.stdout || built.stderr}`); }

    ctx.serverOutput = ctx.serverOutput || vscode.window.createOutputChannel("Odoo Server", "log");
    ctx.serverOutput.show(true);
    ctx.serverOutput.appendLine(`> [port ${port}] ${argv.join(" ")}`);
    const log = serverLogPath(project, port);
    fs.writeFileSync(log, "");

    const bin = argv[1];
    const proc = cp.spawn(argv[0], argv.slice(1), { cwd: fs.existsSync(path.dirname(bin)) ? path.dirname(bin) : project.root, windowsHide: true });
    serverRun.proc = proc;
    const entry = { proc, db: choices.db || null, log, exited: false };
    launched.set(port, entry);
    focusServer(port);   // the pages now follow the server just started; the others keep running
    const sink = (chunk) => { const text = chunk.toString(); ctx.serverOutput.append(text); try { fs.appendFileSync(log, text); } catch (e) { /* the panel still has it */ } };
    proc.stdout.on("data", sink);
    proc.stderr.on("data", sink);
    proc.on("exit", (code) => {
      ctx.serverOutput.appendLine(`\n[server on port ${port} exited with code ${code}]`);
      entry.exited = true;
      if (serverRun.proc === proc) serverRun.proc = null;
      setTimeout(pollServerStatus, 500);
    });
    const what = [choices.db ? `database ${choices.db}` : "the .conf's database", choices.update ? `upgrading ${choices.update}` : ""].filter(Boolean).join(", ");
    vscode.window.showInformationMessage(`Starting Odoo ${project.version} for ${project.name} on port ${port} (${what})...`);
    setTimeout(pollServerStatus, 2500);
  }

  /**
   * False when the project cannot start a server now. A server already running does not stop a new one: the new one
   * takes the next free port, and the others keep running (stop any of them from the Cockpit).
   */
  async function readyToStart(project) {
    if (!project) { vscode.window.showErrorMessage("No active Odoo project found."); return false; }
    if (!fs.existsSync(confPath(project))) { vscode.window.showErrorMessage(`${path.basename(confPath(project))} was not found in ${project.root}: the server needs its configuration file (<project>.conf or odoo.conf).`); return false; }
    return true;
  }

  /** The port a new server takes, said out loud when it is not the project's own. */
  async function portForNewServer(project) {
    const port = await freePort(project);
    if (port !== getProjectPort(project)) vscode.window.showInformationMessage(`Port ${getProjectPort(project)} is taken: the new server uses port ${port}. The other keeps running.`);
    return port;
  }

  /** Start Odoo Server: at once, no questions - with the database chosen last time, else as the .conf says. */
  async function startServerQuick() {
    const project = findProject();
    if (!(await readyToStart(project))) return;
    const db = remembered(project).db || null;
    await startServerWith(project, { db, update: "", dev: true, port: await portForNewServer(project) });
    if (!db) {
      // With no database Odoo's background jobs visit EVERY database of the cluster, and each one older than the code
      // reports errors: the server is fine, the noise is not. Say so once, and offer the one-time choice.
      vscode.window.showInformationMessage(
        "Started without a database: Odoo's background jobs will visit every database on this PostgreSQL and report errors for the old ones. Choose one once, and Start Odoo Server will remember it.",
        "Choose a database...").then((a) => { if (a) startServer(); });
    }
  }

  /** Start with Options...: database, upgrade, port and dev mode, remembered for next time. */
  async function startServer() {
    const project = findProject();
    if (!(await readyToStart(project))) return;
    const db = await pickDatabase(project, { title: "Start Odoo Server (1/3): database" });
    if (db === undefined) return;
    let update = "";
    if (db.db) {
      update = await pickUpgrade();
      if (update === undefined) return;
    }
    const dev = await vscode.window.showQuickPick(
      [{ label: "$(debug) Developer mode (--dev=all)", description: "reload on change", value: true }, { label: "$(rocket) Normal", value: false }],
      { title: "Start Odoo Server (3/3): mode", placeHolder: "Developer mode?" });
    if (!dev) return;
    const choices = { db: db.db, update, dev: dev.value, port: await portForNewServer(project) };
    if (ctx.extContext) await ctx.extContext.workspaceState.update(`${LAST}.${project.name}`, { db: choices.db, update, dev: choices.dev });
    await startServerWith(project, choices);
  }

  /** Restart with the last choices, without asking (and without upgrading again). */
  async function restartServer() {
    const project = findProject();
    if (!project) return;
    const last = remembered(project);
    const port = state.focusPort || getProjectPort(project);
    const info = launched.get(port);
    await stopServerAt(port);
    setTimeout(() => startServerWith(project, Object.assign({ dev: true }, last, { update: "", port }, info && info.db ? { db: info.db } : {})), 1800);
  }

  /** Upgrade modules of ONE database, say what broke, and tie it to the code. */
  async function upgradeDatabase(preset) {
    const project = findProject();
    if (!project) return vscode.window.showErrorMessage("No active Odoo project found.");
    let db, modules;
    if (preset) {
      db = preset.db;
      modules = preset.modules || (await pickUpgrade({ allowNone: false }));
      if (!modules) return;
    } else {
      const picked = await pickDatabase(project, { required: true, title: "Upgrade (1/2): database" });
      if (!picked || !picked.db) return;
      db = picked.db;
      modules = await pickUpgrade({ allowNone: false });
      if (!modules) return;
    }
    if (backupBeforeUpgrade()) {   // the setting asks for a safety copy first, and nothing goes on without it
      const saved = await backupDatabase(db);
      if (!saved || !saved.ok) return;
    }
    ctx.serverOutput = ctx.serverOutput || vscode.window.createOutputChannel("Odoo Server", "log");
    ctx.serverOutput.clear();
    ctx.serverOutput.show(true);
    const log = path.join(path.dirname(serverLogPath(project)), `${logStem(project)}.upgrade.log`);
    const args = serverArgs(project, "upgrade", ["-d", db, "-m", modules, "--log", log, "--root", project.root]);
    const what = modules === "all" ? "all modules" : modules;
    let code = 1;
    let cancelled = false;
    state.upgrade = { db, what, text: "starting..." };
    ctx.actionsTree.refresh();
    await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: `Upgrading ${what} on ${db}`, cancellable: true }, async (progress, token) => {
      const cli = getCliPath(project);
      const isScript = /\.py$/i.test(cli);
      const proc = cp.spawn(isScript ? getPythonExe(project) : cli, isScript ? [cli, ...args] : args, { cwd: project.root, windowsHide: true });
      token.onCancellationRequested(() => { cancelled = true; killTree(proc.pid); });
      const sink = (chunk) => ctx.serverOutput.append(chunk.toString());
      proc.stdout.on("data", sink);
      proc.stderr.on("data", sink);
      // "close" waits for every pipe to end, and a grandchild that keeps one open (or a process killed from outside) can
      // leave it hanging for ever: the tree then says "Upgrading" with nothing running. "exit" settles it after a short grace.
      let settle;
      const finished = new Promise((resolve) => { settle = resolve; });
      proc.on("close", (c) => settle(c === null ? 1 : c));
      proc.on("exit", (c) => setTimeout(() => settle(c === null ? 1 : c), 3000));
      proc.on("error", () => settle(1));
      // while it runs, read its log every two seconds: which module, out of how many, what is left, what has gone wrong
      let reported = 0;
      let busy = false;
      const timer = setInterval(async () => {
        if (busy || !fs.existsSync(log)) return;
        busy = true;
        try {
          const verdict = await judgeLog(project, log, null, db);
          const p = verdict && verdict.progress;
          if (p) {
            const line = progressLine(p);
            progress.report({ message: line, increment: Math.max(0, p.percent - reported) });
            reported = Math.max(reported, p.percent);
            state.upgrade = { db, what, text: line, errors: (verdict.errors || 0) + (verdict.critical || 0), warnings: verdict.warnings || 0, progress: p };
            ctx.actionsTree.refresh();
          }
        } finally {
          busy = false;
        }
      }, 2000);
      code = await finished;
      clearInterval(timer);
    });
    state.upgrade = null;
    ctx.actionsTree.refresh();
    if (cancelled) return vscode.window.showWarningMessage(`The upgrade of ${db} was cancelled.`);
    if (code === 3) {   // refused: another session uses the database
      const answer = await vscode.window.showWarningMessage(`${db} is in use by another session (usually a running Odoo server). An upgrade now would fail with "could not serialize access".`, "Stop the server and upgrade", "Cancel");
      if (answer === "Stop the server and upgrade") {
        for (const s of (await detectServers(project)).filter((x) => x.mine || x.db === db)) await killTree(s.pid);
        await new Promise((r) => setTimeout(r, 1500));
        return upgradeDatabase({ db, modules });
      }
      return;
    }
    const verdict = await judgeLog(project, log, null, db);
    publishIssues(verdict);
    setIssuesSource(`Upgrade of ${db}`, log, verdict);
    const p = verdict && verdict.progress;
    const summary = p ? `${p.total} modules loaded in ${duration(p.seconds || p.elapsed_s)}${p.updated ? `, ${p.updated} upgraded` : ""}` : "";
    const counts = verdict ? `${(verdict.errors || 0) + (verdict.critical || 0)} error(s), ${verdict.warnings || 0} warning(s)` : "";
    if (code === 0 && verdict && verdict.state !== "failed") {
      const buttons = verdict.items && verdict.items.length ? ["Show errors and warnings", "Show output"] : ["Show output"];
      vscode.window.showInformationMessage(`Upgrade finished: ${what} on ${db}. ${summary}. ${counts}.`, ...buttons).then((a) => {
        if (a === "Show errors and warnings") focusIssues();
        else if (a) ctx.serverOutput.show(true);
      });
    } else {
      notifyProblem(verdict, `Upgrade of ${db} failed`);
      if (p && !p.finished) vscode.window.showErrorMessage(`It stopped at module ${p.module} (${p.index} of ${p.total}); ${p.remaining} were not reached.`);
    }
    setTimeout(pollServerStatus, 500);
  }

  /** Does the database have every column the code declares? If not, which -u fixes it. */
  async function checkDrift(preset) {
    const project = findProject();
    if (!project) return;
    let db = preset;
    if (!db) {
      const picked = await pickDatabase(project, { required: true, title: "Check the database against the code" });
      if (!picked || !picked.db) return;
      db = picked.db;
    }
    let res;
    await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: `Comparing ${db} with the code...` }, async () => {
      res = await runCli(project, serverArgs(project, "drift", ["-d", db, "--json"]), 900000);
    });
    let data;
    try { data = JSON.parse(res.stdout); } catch (e) { vscode.window.showWarningMessage(`Could not compare ${db}: ${res.stdout || res.stderr || "no answer"}`.slice(0, 300)); return null; }
    if (data.error) { vscode.window.showWarningMessage(`Could not compare ${db}: ${data.error}`); return null; }
    const modules = Object.keys(data.drift || {}).filter((m) => m !== "?");
    const result = { db, advice: data.advice, clean: !Object.keys(data.drift || {}).length, drift: data.drift || {} };
    if (result.clean) {
      vscode.window.showInformationMessage(`${db} matches the code.`);
      return result;
    }
    const detail = Object.entries(data.drift).map(([m, f]) => `${m}: ${f.slice(0, 3).join(", ")}${f.length > 3 ? " ..." : ""}`).join("  |  ");
    const answer = await vscode.window.showWarningMessage(`${db} is older than the code. ${data.advice}. ${detail}`.slice(0, 450), "Upgrade now", "Dismiss");
    if (answer === "Upgrade now") await upgradeDatabase({ db, modules: modules.length ? modules.join(",") : "all" });
    return result;
  }

  // ------------------------------------------------------------- backup and drop

  function getBackupDir(project) {
    const configured = vscode.workspace.getConfiguration("odooCi").get("backupDir");
    if (configured && configured.trim()) return configured.trim();
    return path.join(getOdooRoot(project), "backups");
  }

  function backupBeforeUpgrade() {
    return !!vscode.workspace.getConfiguration("odooCi").get("backupBeforeUpgrade");
  }

  async function setBackupBeforeUpgrade(value) {
    await vscode.workspace.getConfiguration("odooCi").update("backupBeforeUpgrade", !!value, vscode.ConfigurationTarget.Workspace);
  }

  /** Dump a database with its filestore into the backup folder: {ok, path, message}. */
  async function backupDatabase(db) {
    const project = findProject();
    if (!project) return null;
    let result = null;
    await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: `Backing up ${db}...` }, async () => {
      const res = await runCli(project, serverArgs(project, "backup", ["-d", db, "--dir", getBackupDir(project), "--json"]), 3600000);
      try { result = JSON.parse(res.stdout); } catch (e) { result = { ok: false, path: "", message: String(res.stdout || res.stderr || "no answer").slice(0, 200) }; }
    });
    if (result && result.ok) vscode.window.showInformationMessage(`Backed up ${db}: ${result.path} (${result.message})`);
    else vscode.window.showErrorMessage(`The backup of ${db} failed: ${result && result.message}`);
    return result;
  }

  /** Delete a database: asked in words, a backup offered, and the name typed. Returns true when it is gone. */
  async function dropDatabase(db) {
    const project = findProject();
    if (!project) return false;
    const mine = state.servers.find((s) => s.mine);
    if (state.serverRunning && (serverRun.db === db || (mine && mine.db === db))) {
      vscode.window.showWarningMessage(`${db} is the database the running server uses. Stop the server first.`);
      return false;
    }
    const choice = await vscode.window.showWarningMessage(`Delete the database ${db} and its filestore? This cannot be undone.`,
      { modal: true }, "Back up, then delete", "Delete without a backup");
    if (!choice) return false;
    const typed = await vscode.window.showInputBox({ prompt: `Type the database name to confirm: ${db}`, validateInput: (v) => (v === db ? null : "the name must match exactly") });
    if (typed !== db) return false;
    const args = serverArgs(project, "drop", ["-d", db, "--yes"]);
    if (choice === "Back up, then delete") args.push("--backup-dir", getBackupDir(project));
    let res;
    await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: `Deleting ${db}...` }, async () => {
      res = await runCli(project, args, 3600000);
    });
    if (res.code === 0) {
      vscode.window.showInformationMessage(`Deleted ${db}.`);
      return true;
    }
    const tail = String(res.stdout || res.stderr || "").trim().split("\n").slice(-2).join(" ").slice(0, 250);
    vscode.window.showErrorMessage(`${db} was not deleted: ${tail}`);
    return false;
  }


  // ------------------------------------------------------------- how far a start or an upgrade has got

  function duration(seconds) {
    if (seconds === null || seconds === undefined) return "";
    const s = Math.round(seconds);
    return s >= 60 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`;
  }

  /** "120 of 324 (37%) · website · about 1m 10s left" - the one line that says where it is. */
  function progressLine(p) {
    if (!p) return "";
    const where = `${p.index} of ${p.total} modules (${p.percent}%)`;
    if (p.finished) return `${p.total} modules loaded in ${duration(p.seconds)}${p.updated ? `, ${p.updated} upgraded` : ""}`;
    return `${where} · ${p.module || ""}${p.file ? `/${p.file}` : ""}${p.eta_s !== null && p.eta_s !== undefined ? ` · about ${duration(p.eta_s)} left` : ""}`;
  }

  // ------------------------------------------------------------- the health of a running server, tied to the code

  /** Judge a log file: {state, errors, critical, http_500, warnings, hint, first_issue, issues[]} or null. */
  async function judgeLog(project, log, port, db) {
    const args = serverArgs(project, "health", ["--log", log, "--json", "--root", project.root]).filter((a, i, all) => !(a === "--python" || all[i - 1] === "--python" || a === "--conf" || all[i - 1] === "--conf" || a === "--odoo-bin" || all[i - 1] === "--odoo-bin"));
    if (port) args.push("--port", String(port));
    if (db) args.push("--db", db);   // the errors of other databases are set aside, not blamed on this server
    const res = await runCli(project, args, 20000);
    try { return JSON.parse(res.stdout); } catch (e) { return null; }
  }

  /** Put the issues that point at the project's own code into the Problems panel: click, and the file opens at the line. */
  function publishIssues(verdict) {
    ctx.serverDiagnostics.clear();
    if (!verdict) return;
    const byFile = new Map();
    for (const issue of verdict.issues || []) {
      if (!issue.location) continue;
      const range = new vscode.Range(Math.max(0, issue.location.line - 1), 0, Math.max(0, issue.location.line - 1), 200);
      const text = `${issue.message}${verdict.hint ? `\n${verdict.hint}` : ""}`;
      const d = new vscode.Diagnostic(range, text, vscode.DiagnosticSeverity.Error);
      d.source = "Odoo server";
      if (!byFile.has(issue.location.file)) byFile.set(issue.location.file, []);
      byFile.get(issue.location.file).push(d);
    }
    for (const [file, list] of byFile) ctx.serverDiagnostics.set(vscode.Uri.file(file), list);
  }

  /** Tell the developer once per distinct problem: what it is, what it means, and a way to the line. */
  function notifyProblem(verdict, title) {
    if (!verdict || !verdict.first_issue) return;
    const issue = verdict.first_issue;
    const key = `${verdict.state}:${issue.message}`;
    if (serverRun.notified === key) return;
    serverRun.notified = key;
    const where = issue.module ? ` (loading ${issue.module}/${issue.file})` : "";
    const buttons = [];
    if (issue.location) buttons.push("Open the line");
    buttons.push("Show output");
    const text = `${title || "Server problem"}: ${issue.message.slice(0, 160)}${where}${verdict.hint ? `\n${verdict.hint}` : ""}`;
    vscode.window.showErrorMessage(text, ...buttons).then(async (a) => {
      if (a === "Open the line") {
        const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(issue.location.file));
        const editor = await vscode.window.showTextDocument(doc);
        const pos = new vscode.Position(Math.max(0, issue.location.line - 1), 0);
        editor.selection = new vscode.Selection(pos, pos);
        editor.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
      } else if (a === "Show output") {
        (ctx.serverOutput || ctx.output).show(true);
      }
    });
  }

  /** Read the running server's log; update the Health row, the Problems panel and, once, check the database against the code. */
  async function refreshHealth(project, port) {
    if (serverRun.healthBusy || !serverRun.log || !fs.existsSync(serverRun.log)) return;
    serverRun.healthBusy = true;
    try {
      const seen = state.servers.find((s) => s.port === port);
      const verdict = await judgeLog(project, serverRun.log, port, serverRun.db || (seen && seen.db) || null);
      if (!verdict) return;
      const changed = !state.health || state.health.state !== verdict.state || state.health.errors !== verdict.errors || state.health.http_500 !== verdict.http_500;
      const signature = `${verdict.marks ? verdict.marks.error.length : 0}/${verdict.marks ? verdict.marks.warning.length : 0}`;
      const grew = serverRun.signature !== signature;
      serverRun.signature = signature;
      state.health = verdict;
      if ((changed || grew) && (!state.issuesSource || state.issuesSource.label === "Server")) setIssuesSource("Server", serverRun.log, verdict);
      if (changed) publishIssues(verdict);
      if (verdict.state === "problems" || verdict.state === "failed") notifyProblem(verdict, verdict.state === "failed" ? "The server failed" : "The server has a problem");
      if (verdict.state === "healthy" && !serverRun.driftChecked && serverRun.db) {
        serverRun.driftChecked = true;   // once per start: is the database older than the code?
        checkDrift(serverRun.db);
      }
    } finally {
      serverRun.healthBusy = false;
    }
  }

  async function showHealth() {
    const h = state.health;
    if (!h) return vscode.window.showInformationMessage("No server of this project has been started from here yet.");
    if (h.first_issue) return notifyProblem(Object.assign({}, h), "Server");
    vscode.window.showInformationMessage(`Server: ${h.state}. ${h.warnings || 0} warning(s), no errors.`);
  }

  /** The port to open in the browser: this project's server, else the configured port when it answers, else another Odoo. */
  function serverPort(project) {
    const focused = state.focusPort && state.servers.find((s) => s.port === state.focusPort);
    if (focused) return focused.port;
    const mine = state.servers.find((s) => s.mine);
    if (mine) return mine.port;
    if (state.portOpen) return getProjectPort(project);
    return getProjectPort(project);
  }

  async function stopServer() {
    const project = findProject();
    if (!project) return;
    const term = vscode.window.terminals.find((t) => t.name === serverTerminalName(project));
    if (term) term.dispose();
    const servers = await detectServers(project);
    const targets = servers.filter((s) => s.mine);   // never another project's server
    for (const s of targets) await killTree(s.pid);
    const answering = !targets.length && state.portOpen ? getProjectPort(project) : null;
    vscode.window.showInformationMessage(targets.length
      ? `Stopped ${targets.length} Odoo server${targets.length === 1 ? "" : "s"}.`
      : answering
        ? `Port ${answering} answers, but not from a process of this project (Docker, WSL, another machine, or another project's server), so it cannot be stopped from here.`
        : "No Odoo server found to stop.");
    setTimeout(pollServerStatus, 1000);
  }

  function openServer(port) {
    const project = findProject();
    const target = typeof port === "number" ? port : serverPort(project);
    vscode.env.openExternal(vscode.Uri.parse(`http://localhost:${target}`));
  }

  let polling = false;
  async function pollServerStatus(force) {
    const project = findProject();
    if (polling || !project || (force !== true && vscode.window.state && vscode.window.state.focused === false)) return;
    polling = true;
    try {
      const port = getProjectPort(project);
      const [servers, open] = await Promise.all([detectServers(project), new Promise((res) => checkPortOpen(port, res))]);
      state.servers = servers.filter((s) => s.mine);   // another project's server is not this window's to show or control
      state.otherPorts = servers.filter((s) => !s.mine).map((s) => s.port);   // known only so that a new server avoids its port
      servers.splice(0, servers.length, ...state.servers);
      state.portOpen = open;
      state.serverRunning = servers.some((s) => s.mine) || open;
      // the focused server went away (stopped from the Cockpit or elsewhere): follow another one, or none
      const focus = state.focusPort;
      const alive = (p) => servers.some((s) => s.port === p) || (launched.get(p) && (!launched.get(p).exited || (state.health && state.health.state === "failed")));
      if (focus && !alive(focus)) {
        const next = servers.find((s) => s.mine) || servers[0];
        if (next) focusServer(next.port, true);
        else { state.focusPort = null; if (state.health && state.health.state !== "failed") state.health = null; }
      } else if (!focus && servers.length) {
        focusServer((servers.find((s) => s.mine) || servers[0]).port, true);
      }
      if (state.focusPort && (state.serverRunning || (state.health && state.health.state !== "failed"))) await refreshHealth(project, state.focusPort);
    } finally {
      polling = false;
    }
    if (ctx.serverItem) {
      if (state.serverRunning) {
        ctx.serverItem.text = `$(server-process) :${serverPort(project)}`;
        ctx.serverItem.tooltip = `Odoo is running on port ${serverPort(project)}. Click to open it.`;
        ctx.serverItem.command = "odooCi.serverOpen";
        ctx.serverItem.show();
      } else {
        ctx.serverItem.hide();
      }
    }
    ctx.actionsTree.refresh();
    if (ctx.cockpit) ctx.cockpit.refresh();
  }

  /** What the cockpit may start with a press: only the commands this extension declares, by id. */
  function runExtensionCommand(id) {
    const declared = new Set(require("./package.json").contributes.commands.map((c) => c.command));
    return declared.has(id) ? vscode.commands.executeCommand(id) : Promise.resolve();
  }

  /** The last CI run, for the Quality page: the same phases and steps the sidebar shows, as plain data. */
  function ciView() {
    return {
      running: !!ctx.child, mode: state.mode, result: state.result ? { passed: !!state.result.passed, blocked: !!state.result.blocked, cancelled: !!state.result.cancelled } : null,
      phases: state.phases.map((p) => ({ phase: p.phase, name: p.name })),
      steps: state.steps.map((s) => ({ phase: s.phase, name: s.name, status: s.status, seconds: s.seconds === undefined ? null : s.seconds, details: (s.details || []).slice(0, 8).map((d) => String(d).slice(0, 240)) })),
    };
  }

  function runXPathCheck() {
    const project = findProject();
    if (!project) return;
    const python = getPythonExe(project);
    const cli = getCliPath(project);

    ctx.output.clear();
    ctx.output.show(true);
    ctx.output.appendLine(`> Validating XML Views & XPath in ${project.name}...`);
    ctx.diagnostics.clear();
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
        ctx.output.appendLine(line);
        if (line.includes("[XPATH ERROR]") || line.includes("[XML SYNTAX ERROR]")) {
          errorCount++;
          addDiagnostic(line);
        }
      }
    });

    proc.stderr.on("data", (data) => ctx.output.appendLine(data.toString("utf8")));

    proc.on("close", (code) => {
      if (code === 0 && errorCount === 0) {
        vscode.window.showInformationMessage("Odoo XPath Check: All XML views & XPath expressions are valid!");
      } else {
        vscode.window.showErrorMessage(`Odoo XPath Check found ${errorCount} error(s). See Problems tab.`);
        vscode.commands.executeCommand("workbench.actions.view.problems");
      }
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
      { label: "$(zap) Yes, Initialize Fast Test Clean DB Now", description: "Prepares a PostgreSQL template for fast testing", action: true },
      { label: "$(close) Skip for now", description: "Can be initialized later at any time", action: false }
    ], { placeHolder: "Do you want to initialize the Clean Template Database (Fast Test)?" });

    if (initDbChoice && initDbChoice.action) {
      runInitFastDb();
    }

    vscode.window.showInformationMessage("Cockpit Odoo: Project configuration updated successfully!");
    ctx.actionsTree.refresh();
  }



  return {
    backupBeforeUpgrade,
    backupDatabase,
    checkDrift,
    ciView,
    cliCommand,
    dropDatabase,
    duration,
    focusServer,
    freePort,
    getBackupDir,
    launched,
    listDatabases,
    openServer,
    pollServerStatus,
    progressLine,
    remembered,
    restartServer,
    runCli,
    runExtensionCommand,
    runSetupWizard,
    runXPathCheck,
    serverArgs,
    serverLogPath,
    setBackupBeforeUpgrade,
    showHealth,
    startServer,
    startServerQuick,
    startServerWith,
    stopServer,
    stopServerAt,
    upgradeDatabase,
  };
}

module.exports = { create };
