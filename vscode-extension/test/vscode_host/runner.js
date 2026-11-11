// Runs INSIDE a real VS Code (an extension host): the checks that need the real editor.
// Started by test/vscode_host.test.js with --extensionTestsPath. It writes what it found to ODOO_CI_TEST_OUT.
"use strict";
const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(condition, ms, step = 250) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try { const value = await condition(); if (value) return value; } catch (e) { /* not yet */ }
    await sleep(step);
  }
  return null;
}

exports.run = async function run() {
  const results = [];
  const check = (name, condition, detail) => results.push({ name, ok: !!condition, detail: condition ? undefined : String(detail === undefined ? "" : detail) });
  const out = process.env.ODOO_CI_TEST_OUT;
  const project = process.env.ODOO_CI_TEST_PROJECT;
  try {
    const extension = vscode.extensions.getExtension("hosamae.odoo-ci");
    check("the extension is found in the editor", extension, "not found");
    const api = await extension.activate();
    check("it activates", extension.isActive, "not active");
    check("activation had no startup problems", api && api._test && api._test.startupProblems.length === 0,
      JSON.stringify(api && api._test && api._test.startupProblems));

    // every command the extension declares is registered in the editor
    const registered = new Set(await vscode.commands.getCommands(true));
    const declared = extension.packageJSON.contributes.commands.map((c) => c.command);
    const missing = declared.filter((c) => !registered.has(c));
    check(`all ${declared.length} declared commands are registered`, missing.length === 0, `missing: ${missing.join(", ")}`);

    // every view is known to the editor itself - the check that failed on 2026-11-11 with "No view is registered"
    for (const id of ["odooCi.env", "odooCi.actions", "odooCi.issues"]) {
      let error = null;
      try { await vscode.commands.executeCommand(`${id}.focus`); } catch (e) { error = e; }
      check(`the editor knows the view ${id}`, !error, error && error.message);
    }

    // the Errors & Warnings view, fed by the real tools judging a real log file
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "odoo-ci-host-"));
    const log = path.join(dir, "server.log");
    const source = path.join(project, "acme_work_plan", "models", "daymap.py");
    fs.writeFileSync(log, [
      "2026-11-10 23:00:00,000 1 INFO hx_try odoo.modules.loading: Loading module base (1/3)",
      "2026-11-10 23:00:01,000 1 WARNING hx_try odoo.tools.convert: Skipping deletion for missing XML ID `'a'`",
      "2026-11-10 23:00:02,000 1 INFO hx_try odoo.modules.loading: Loading module web (2/3)",
      "2026-11-10 23:00:03,000 1 ERROR hx_try odoo.http: Exception during request handling.",
      "Traceback (most recent call last):",
      `  File "${source}", line 40, in hx_daymap_data`,
      "    boom()",
      "ValueError: boom",
      "2026-11-10 23:00:04,000 1 WARNING hx_try odoo.tools.convert: Skipping deletion for missing XML ID `'a'`",
      "2026-11-10 23:00:05,000 1 INFO hx_try odoo.modules.loading: Loading module sale (3/3)",
    ].join("\n") + "\n");
    let verdict = null;
    try {
      verdict = JSON.parse(cp.execFileSync(process.env.ODOO_CI_TEST_PYTHON, [process.env.ODOO_CI_TEST_CLI, "server", "health", "--log", log, "--json", "--root", project, "--db", "hx_try"], { encoding: "utf8" }));
    } catch (e) {
      verdict = JSON.parse(e.stdout || "null");   // the tools exit non-zero for problems, and still answer
    }
    check("the real tools judged the log", verdict && verdict.items && verdict.items.length === 2, JSON.stringify(verdict && verdict.items && verdict.items.length));
    const issues = api._test.issues;
    issues.setIssuesSource("Test", log, verdict);
    const tree = api._test.issuesTree;
    const groups = tree.getChildren();
    check("the view lists Errors (1) and Warnings (1)", groups.map((g) => String(g.label)).join("|").includes("Errors (1)") && groups.map((g) => String(g.label)).join("|").includes("Warnings (1)"), groups.map((g) => String(g.label)).join("|"));
    const errorNode = tree.getChildren(groups.find((g) => String(g.label).startsWith("Errors")))[0];
    check("the error ends in your code and says so", errorNode && errorNode.contextValue === "issueWithSource", errorNode && errorNode.contextValue);

    // walking through: the editor opens the log at the line
    await vscode.commands.executeCommand("odooCi.issueNext");
    const first = await until(() => vscode.window.activeTextEditor && vscode.window.activeTextEditor.document.uri.fsPath.toLowerCase() === log.toLowerCase() && vscode.window.activeTextEditor, 10000);
    check("Next opens the log in the editor", first, vscode.window.activeTextEditor && vscode.window.activeTextEditor.document.uri.fsPath);
    check("at the first error's line (4)", first && first.selection.active.line + 1 === 4, first && first.selection.active.line + 1);
    await vscode.commands.executeCommand("odooCi.issueNext");
    await sleep(500);
    check("the next stop is the first warning's line (2)", vscode.window.activeTextEditor && vscode.window.activeTextEditor.selection.active.line + 1 === 2, vscode.window.activeTextEditor && vscode.window.activeTextEditor.selection.active.line + 1);
    await vscode.commands.executeCommand("odooCi.issuePrev");
    await sleep(500);
    check("Previous goes back to the error (4)", vscode.window.activeTextEditor && vscode.window.activeTextEditor.selection.active.line + 1 === 4, vscode.window.activeTextEditor && vscode.window.activeTextEditor.selection.active.line + 1);

    // clicking an error that ends in your code opens that code at its line, and the log
    await vscode.commands.executeCommand("odooCi.issueOpen", errorNode.issue);
    await sleep(1200);
    const visible = vscode.window.visibleTextEditors;
    const codeEditor = visible.find((e) => e.document.uri.fsPath.toLowerCase() === source.toLowerCase());
    check("an error opens your file", codeEditor, visible.map((e) => e.document.uri.fsPath).join(", "));
    check("at line 40", codeEditor && codeEditor.selection.active.line + 1 === 40, codeEditor && codeEditor.selection.active.line + 1);
    check("and the log beside it", visible.some((e) => e.document.uri.fsPath.toLowerCase() === log.toLowerCase()), visible.map((e) => e.document.uri.fsPath).join(", "));

    // the cockpit, opened in the editor: the page runs under the editor's content-security policy and draws
    const cockpit = api._test.cockpit;
    await vscode.commands.executeCommand("odooCi.cockpit");
    check("the cockpit panel opens", cockpit.isOpen(), "no panel");
    const drawn = await until(() => { const r = cockpit.lastRender(); return r && r.dbRows > 0 ? r : null; }, 90000, 500);
    check("the page ran and drew the databases read by the real tools", drawn, JSON.stringify(cockpit.lastRender()));
    const first2 = drawn || {};
    check("it drew the log lines and the issues it was given", first2.logLines > 0 && first2.issues === 2, JSON.stringify(first2));
    check("it coloured the error and the two warning lines", first2.errorLines >= 1 && first2.warningLines === 2, JSON.stringify(first2));
    check("the header shows a state", first2.pill, JSON.stringify(first2));

    // the same page in the bottom panel, beside Terminal and Problems
    let viewError = null;
    try { await vscode.commands.executeCommand("workbench.view.extension.odooCockpit"); } catch (e) { viewError = e; }
    check("the editor opens the bottom-panel container", !viewError, viewError && viewError.message);
    const inPanel = await until(() => { const r = cockpit.lastRender("panel"); return r && r.dbRows > 0 ? r : null; }, 90000, 500);
    check("the page ran in the bottom panel and drew the databases", inPanel, JSON.stringify(cockpit.lastRender("panel")));
    check("in its compact layout", inPanel && inPanel.mode === "panel", JSON.stringify(inPanel));
    check("with its door and five pages as tabs", inPanel && inPanel.tabs === 6, JSON.stringify(inPanel));

    // one button opens it in the bottom panel; the page itself can move up to the editor and back
    await vscode.commands.executeCommand("odooCi.cockpitPanel");
    check("the one Open Cockpit command works in the editor", cockpit.isViewOpen(), "no bottom view");
    await cockpit.handle({ cmd: "move", to: "editor" }, "panel");
    check("the page can move up into an editor tab", cockpit.isOpen(), "no editor tab");
    await cockpit.handle({ cmd: "move", to: "panel" }, "editor");
    await sleep(800);
    check("and back down to the bottom panel (the tab is closed)", cockpit.isViewOpen() && !cockpit.isOpen(), `view ${cockpit.isViewOpen()} tab ${cockpit.isOpen()}`);

    // the translations page, fed by the real tools reading the real project (it changes nothing)
    const tr = api._test.translations;
    await vscode.commands.executeCommand("odooCi.translations");
    check("the translations page opens", tr.isOpen(), "no panel");
    const modulesRead = await until(() => { const st = tr.state(); return st.modules && st.modules.length ? st : null; }, 120000, 500);
    check("it read the project's modules with the real tools", modulesRead && modulesRead.modules.some((m) => m.module === "acme_work_plan"), JSON.stringify(tr.state().error));
    const plan = modulesRead && modulesRead.modules.find((m) => m.module === "acme_work_plan");
    check("with the languages of the setting (sv_SE) counted even before a .po exists", plan && plan.languages.some((l) => l.lang === "sv_SE") && plan.terms > 100, JSON.stringify(plan && { terms: plan.terms, languages: plan.languages.map((l) => l.lang) }));
    const trDrawn = await until(() => (tr._r.rendered && tr._r.rendered.modules > 0 ? tr._r.rendered : null), 30000, 500);
    check("and the page drew them", trDrawn, JSON.stringify(tr._r.rendered));

    // the remote servers: the hosts of the real ssh config, and (when told) one host read for real - read only
    const remote = cockpit.remote;
    await remote.loadHosts();
    const hosts = remote.state().hosts || [];
    check("the remote tab lists the hosts of ~/.ssh/config", hosts.length > 0 || !fs.existsSync(path.join(os.homedir(), ".ssh", "config")), JSON.stringify(hosts));
    const readHost = process.env.ODOO_CI_TEST_REMOTE_HOST;
    if (readHost) {
      if (process.env.ODOO_CI_TEST_REMOTE_ACCOUNT) {
        await vscode.workspace.getConfiguration("odooCi").update("remoteSudoUser", process.env.ODOO_CI_TEST_REMOTE_ACCOUNT, vscode.ConfigurationTarget.Global);
      }
      await remote.select(readHost);
      const st = remote.state();
      // a host may refuse (its own security rule): then the refusal must be shown in the host's own words
      check(`${readHost}: its databases were read over ssh, or its refusal is shown`, st.info ? st.info.databases.length > 0 : !!st.error, JSON.stringify(st.error));
      if (st.info) {
        await vscode.commands.executeCommand("odooCi.cockpitPanel");
        const drawnRemote = await until(() => { const r = cockpit.lastRender("panel"); return r && r.remoteRows > 0 ? r : null; }, 20000, 500);
        check("the page draws the remote databases", drawnRemote, JSON.stringify(cockpit.lastRender("panel")));
      }
    }

    // the control panel: the rows a person is meant to find
    const roots = api._test.actionsTree.getChildren();
    const serverGroup = roots.find((r) => String(r.label).startsWith("Server"));
    const rows = serverGroup ? api._test.actionsTree.getChildren(serverGroup).map((r) => String(r.label)) : [];
    for (const wanted of ["Open Cockpit", "Start Odoo Server", "Start with Options...", "Upgrade Modules...", "Check Database vs Code", "Stop Odoo Server", "Open in Browser"]) {
      check(`the control panel has "${wanted}"`, rows.includes(wanted), rows.join(" | "));
    }
    const startRow = api._test.actionsTree.getChildren(serverGroup).find((r) => String(r.label) === "Start Odoo Server");
    check("the Start row starts the server at once (no questions)", startRow && startRow.command && startRow.command.command === "odooCi.serverRunQuick", JSON.stringify(startRow && startRow.command));

    // two real servers at once, each with its own log; one is stopped and the other keeps running
    const scratch = process.env.ODOO_CI_TEST_SERVERS;
    if (scratch) {
      const m = api._test.multi;
      const pr = m.findProject();
      const first = await m.freePort(pr);
      await m.startServerWith(pr, { db: scratch, update: "", dev: false, port: first });
      const second = first + 1;
      await m.startServerWith(pr, { db: scratch, update: "", dev: false, port: second });
      const both = await until(async () => { const l = (await m.detectServers(pr)).filter((s) => [first, second].includes(s.port)); return l.length === 2 ? l : null; }, 120000, 1000);
      check("two real servers run at once on two ports", both, JSON.stringify(await m.detectServers(pr)));
      check("the pages follow the one started last", m.state.focusPort === second && m.serverRun.log && m.serverRun.log.endsWith(`.${second}.log`), `${m.state.focusPort} ${m.serverRun.log}`);
      m.focusServer(first);
      check("Follow switches the log to the other server", m.state.focusPort === first && m.serverRun.log.endsWith(`.${first}.log`), `${m.state.focusPort} ${m.serverRun.log}`);
      await m.stopServerAt(second);
      const left = await until(async () => { const l = (await m.detectServers(pr)).map((s) => s.port).filter((x) => [first, second].includes(x)); return l.length === 1 ? l : null; }, 30000, 500);
      check("stopping one leaves the other running", left && left[0] === first, JSON.stringify(left));
      await m.stopServerAt(first);
      await until(async () => (await m.detectServers(pr)).every((s) => ![first, second].includes(s.port)), 30000, 500);

      // another project's server: not shown, not controllable
      const decoyPort = (await m.freePort(pr)) + 5;
      const decoy = cp.spawn(process.env.ODOO_CI_TEST_PYTHON, ["-c", "import time; time.sleep(60)", "odoo-bin", "--config=C:/elsewhere/Other.conf", "-p", String(decoyPort)], { stdio: "ignore", windowsHide: true });
      try {
        await until(async () => (await m.detectServers(pr)).find((x) => x.port === decoyPort), 20000, 500);
        await m.pollServerStatus(true);
        check("another project's server is not in the cockpit's list", !m.state.servers.some((x) => x.port === decoyPort), JSON.stringify(m.state.servers.map((x) => x.port)));
        check("nor in what the page is given", !(cockpit.currentList ? cockpit.currentList() : []).some((x) => x.port === decoyPort), "listed");
        await m.stopServerAt(decoyPort);
        await sleep(1500);
        check("and Stop cannot end it", (await m.detectServers(pr)).some((x) => x.port === decoyPort), "it was stopped");
      } finally {
        try { decoy.kill(); } catch (e) { /* gone */ }
      }

      // a server of this project started OUTSIDE the editor (a terminal, a task, a service): found, and followed through its log file
      const outsideLog = path.join(dir, "outside_server.log");
      const port = await m.freePort(pr);
      const outside = cp.spawn(process.env.ODOO_CI_TEST_PYTHON, [path.join(process.env.ODOO_ROOT || "C:\\odoo", "odoo19", "odoo", "odoo-bin"), "-c", path.join(project, `${path.basename(project)}.conf`), "-d", scratch, `--http-port=${port}`, `--logfile=${outsideLog}`], { stdio: "ignore", windowsHide: true });
      try {
        const seen = await until(async () => (await m.detectServers(pr)).find((s) => s.port === port), 120000, 1000);
        check("a server started outside the editor is found and attributed to the project", seen && seen.mine, JSON.stringify(seen));
        check("with the log file it writes", seen && seen.logfile === outsideLog, JSON.stringify(seen));
        m.state.servers = await m.detectServers(pr);   // the window is not focused during the run, so the poll does not
        m.focusServer(port);
        await sleep(300);
        check("the cockpit follows it and reads its log", m.serverRun.log === outsideLog && m.state.focusPort === port, `${m.serverRun.log} ${m.state.focusPort}`);
      } finally {
        await m.stopServerAt(port);
        try { outside.kill(); } catch (e) { /* gone */ }
      }
    }

    // a real backup from the cockpit's own button: a dump with its filestore, in the folder the setting names
    const db = process.env.ODOO_CI_TEST_BACKUP_DB;
    if (db) {
      const backups = path.join(dir, "backups");
      await vscode.workspace.getConfiguration("odooCi").update("backupDir", backups, vscode.ConfigurationTarget.Global);
      await cockpit.handle({ cmd: "backup", db });
      const made = fs.existsSync(backups) ? fs.readdirSync(backups).filter((f) => f.startsWith(db + "_") && f.endsWith(".zip")) : [];
      check(`the Backup button wrote a dump of ${db}`, made.length === 1 && fs.statSync(path.join(backups, made[0])).size > 100000, JSON.stringify(made));
    }
    // for a picture: open the extension's own sidebar, say the cockpit is drawn, and keep the window for a while
    if (process.env.ODOO_CI_TEST_READY) {
      if (process.env.ODOO_CI_TEST_SHOT === "panel") {
        await vscode.commands.executeCommand("workbench.action.closeAllEditors");
        await vscode.commands.executeCommand("odooCi.cockpitPanel");        // the cockpit in the bottom panel
      } else {
        await vscode.commands.executeCommand("odooCi.cockpit");               // bring the cockpit forward
        await vscode.commands.executeCommand("workbench.action.closeOtherEditors");   // and give it the whole editor area
      }
      await vscode.commands.executeCommand("workbench.view.extension.odooStudio");
      cockpit.refresh();
      await sleep(1500);
      fs.writeFileSync(process.env.ODOO_CI_TEST_READY, "1");
      await sleep(Number(process.env.ODOO_CI_TEST_HOLD_MS || 0));
    }
    fs.rmSync(dir, { recursive: true, force: true });
  } catch (error) {
    check("the run itself did not crash", false, error && error.stack);
  }
  fs.writeFileSync(out, JSON.stringify(results, null, 1));
  const failed = results.filter((r) => !r.ok);
  if (failed.length) throw new Error(`${failed.length} check(s) failed`);
};
