"use strict";
// Databases on other servers, from the cockpit: pick a host of ~/.ssh/config (the ones Remote-SSH uses), see its
// databases, pull one into a new local one, push a local one up as a new one, back one up, drop one.
// The work is done by the tools (server_ctl.py remote-*); this asks the questions, shows the progress, and keeps
// what the cockpit draws. Nothing is ever written over a database that exists - the tools refuse - and every write
// to a host is asked for in words, with the host and the database typed again for the destructive ones.

const vscode = require("vscode");
const cp = require("child_process");
const os = require("os");
const path = require("path");

const NAME = /^[\w.-]+$/;

/** What the page needs: no raw output, no commands, just the plain state. */
function slimRemote(r) {
  return {
    hosts: r.hosts, host: r.host, error: r.error, needsKey: !!r.needsKey, refused: !!r.refused, account: r.account || null, busy: r.busy, job: r.job, last: r.last,
    info: r.info ? { databases: r.info.databases, instances: r.info.instances.map((i) => ({ conf: i.conf, db_name: i.db_name, db_user: i.db_user })) } : null,
  };
}

function create(api) {
  const r = { hosts: null, host: null, info: null, error: null, needsKey: false, refused: false, busy: false, job: null, last: null };
  const changed = () => api.changed();

  /** The account the host opens for postgres and files, when it opens only one (the setting; empty: postgres, as root). */
  const account = () => {
    const user = String(vscode.workspace.getConfiguration("odooCi").get("remoteSudoUser") || "").trim();
    return /^[\w.-]+$/.test(user) ? ["--sudo-user", user] : [];
  };
  const cli = (project, sub, extra, withProject) => (withProject ? api.serverArgs(project, sub, [...account(), ...extra]) : ["server", sub, ...account(), ...extra]);

  /** Run a tools command. Progress lines (STEP i/n text) come on stderr; the answer is the JSON on stdout. */
  function run(project, args, timeout = 7200000) {
    return new Promise((resolve) => {
      const { file, args: full } = api.cliCommand(project, args);
      let out = "", err = "";
      const child = cp.spawn(file, full, { cwd: project.root, windowsHide: true });
      const timer = setTimeout(() => child.kill(), timeout);
      child.stdout.on("data", (d) => { out += d; });
      child.stderr.on("data", (d) => {
        err += d;
        for (const line of String(d).split(/\r?\n/)) {
          const m = /^STEP (\d+)\/(\d+) (.*)$/.exec(line);
          if (m) { r.job = Object.assign({}, r.job, { step: Number(m[1]), total: Number(m[2]), text: m[3] }); changed(); }
        }
      });
      child.on("error", (e) => { clearTimeout(timer); resolve({ ok: false, error: e.message }); });
      child.on("close", (code) => {
        clearTimeout(timer);
        const lines = out.trim().split(/\r?\n/).filter(Boolean);
        let data = null;
        try { data = JSON.parse(lines[lines.length - 1]); } catch (e) { /* not JSON */ }
        resolve(data || { ok: false, error: String(err || out || `exit ${code}`).trim().split("\n").slice(-1)[0].slice(0, 240) });
      });
    });
  }

  async function job(title, task) {
    const project = api.findProject();
    if (!project || r.busy) return null;
    r.busy = true; r.job = { title, step: 0, total: 0, text: "starting" }; changed();
    try {
      return await task(project);
    } finally {
      r.busy = false; r.job = null; changed();
    }
  }

  async function loadHosts() {
    const project = api.findProject();
    if (!project) return;
    const res = await run(project, cli(project, "remote-hosts", ["--json"], false), 30000);
    r.hosts = res.hosts || [];
    if (!r.host && r.hosts.length) r.host = (api.remembered && api.remembered("remoteHost")) || null;
    changed();
  }

  /** Choose a host and read it. A host that wants a password is told apart from one that cannot be reached. */
  async function select(host) {
    const project = api.findProject();
    if (!project || !host) return;
    r.host = host; r.info = null; r.error = null; r.needsKey = false; r.refused = false;
    if (api.remember) api.remember("remoteHost", host);
    changed();
    await job(`Reading ${host}`, async () => {
      const res = await run(project, cli(project, "remote-list", ["--host", host, "--json"], false), 120000);
      if (res.databases) r.info = res;
      else {
        r.error = res.error || "no answer";
        r.needsKey = /password|another key/i.test(r.error);
        r.refused = /ACCESS DENIED|not allowed|denied/i.test(r.error);
      }
    });
  }

  async function refresh() { if (r.host) await select(r.host); }

  /** A terminal that puts this machine's public key on the host: one password, then no more questions. */
  async function setupKey(host) {
    const ok = await vscode.window.showWarningMessage(
      `Put your public key on ${host}? A terminal opens and ssh asks for the host's password once; afterwards the cockpit connects without one.`,
      { modal: true }, "Open the terminal");
    if (!ok) return;
    const win = process.platform === "win32";
    const terminal = vscode.window.createTerminal(win ? { name: `Key for ${host}`, shellPath: "powershell.exe" } : { name: `Key for ${host}` });
    terminal.show();
    const pub = path.join(os.homedir(), ".ssh", "id_ed25519.pub");
    if (win) {
      terminal.sendText(`if (-not (Test-Path '${pub}')) { ssh-keygen -t ed25519 -f '${pub.replace(/\.pub$/, "")}' }`);
      terminal.sendText(`Get-Content '${pub}' | ssh ${host} "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"`);
    } else {
      terminal.sendText(`ssh-copy-id ${host}`);
    }
  }

  const known = (db) => (r.info ? r.info.databases.find((d) => d.name === db) : null);
  const ownerOf = (conf) => (r.info ? r.info.instances.find((i) => i.conf === conf) : null);

  async function pull(host, db) {
    const row = known(db);
    const name = await vscode.window.showInputBox({
      title: `Pull ${db} from ${host}`, prompt: `Name of the NEW local database (${row ? row.size_mb : "?"} MB on the host). An existing one is never overwritten.`,
      value: db, validateInput: (v) => (NAME.test(v) ? null : "letters, digits, _ . - only"),
    });
    if (!name) return;
    const choice = await vscode.window.showInformationMessage(
      `Copy ${db} from ${host} into the local database ${name}? It is read from the host (nothing changes there) and made safe here: scheduled actions and outgoing mail are switched off.`,
      { modal: true }, "Database and files", "Database only");
    if (!choice) return;
    const result = await job(`Pulling ${db}`, (project) => run(project, cli(project, "remote-pull",
      ["--host", host, "-d", db, "--as", name, "--json", ...(choice === "Database only" ? ["--no-filestore"] : [])], true)));
    if (!result) return;
    if (result.ok) {
      r.last = { level: "ok", text: `Pulled ${db} from ${host} as ${result.database} (${result.size_mb} MB${result.files !== null && result.files !== undefined ? `, ${result.files} files` : ""}). ${result.neutralized ? "Neutralized." : ""}${(result.warnings || []).length ? " " + result.warnings.join(" ") : ""}` };
      vscode.window.showInformationMessage(r.last.text);
    } else {
      r.last = { level: "bad", text: `The pull failed: ${result.error}` };
      vscode.window.showErrorMessage(r.last.text);
    }
    if (api.localChanged) await api.localChanged();
    changed();
  }

  async function push(host) {
    const project = api.findProject();
    if (!project || !r.info) return;
    const local = await api.listDatabases(project);
    const dbs = (local.databases || []);
    if (!dbs.length) { vscode.window.showWarningMessage("There is no local database to push."); return; }
    const source = await vscode.window.showQuickPick(dbs.map((d) => ({ label: `$(database) ${d}`, value: d })), { title: `Push which local database to ${host}?` });
    if (!source) return;
    const name = await vscode.window.showInputBox({
      title: `Name on ${host}`, prompt: "A NEW database name. One that exists on the host is refused.", value: source.value,
      validateInput: (v) => (!NAME.test(v) ? "letters, digits, _ . - only" : known(v) ? `${v} already exists on ${host}` : null),
    });
    if (!name) return;
    const instances = r.info.instances.filter((i) => i.db_user);
    const picked = await vscode.window.showQuickPick(instances.map((i) => ({
      label: `$(server-process) ${path.posix.basename(i.conf, ".conf")}`, description: `role ${i.db_user}`, detail: i.conf, value: i })),
      { title: "Which Odoo instance on the host will use it?", placeHolder: "its role will own the database; its filestore gets the files" });
    if (!picked) return;
    const typed = await vscode.window.showInputBox({
      title: `Create ${name} on ${host}`, prompt: `This writes to ${host}. Type the host name to confirm: ${host}`, validateInput: (v) => (v === host ? null : "type the host name exactly"),
    });
    if (typed !== host) return;
    const result = await job(`Pushing ${source.value}`, (p) => run(p, cli(p, "remote-push",
      ["--host", host, "-d", source.value, "--as", name, "--owner", picked.value.db_user, "--instance", picked.value.conf, "--yes", "--json"], true)));
    if (!result) return;
    if (result.ok) {
      r.last = { level: "ok", text: `Pushed ${source.value} to ${host} as ${result.database} (${result.size_mb} MB${result.files !== null && result.files !== undefined ? `, ${result.files} files` : ""}). Point an instance at it to use it.` };
      vscode.window.showInformationMessage(r.last.text);
      await refresh();
    } else {
      r.last = { level: "bad", text: `The push failed: ${result.error}` };
      vscode.window.showErrorMessage(r.last.text);
    }
    changed();
  }

  async function backup(host, db) {
    const result = await job(`Backing up ${db} from ${host}`, (project) => run(project, cli(project, "remote-backup",
      ["--host", host, "-d", db, "--dir", api.getBackupDir(project), "--json"], false)));
    if (!result) return;
    r.last = result.ok ? { level: "ok", text: `Saved ${db} from ${host}: ${result.path} (${result.size_mb} MB)` } : { level: "bad", text: `The backup failed: ${result.error}` };
    (result.ok ? vscode.window.showInformationMessage : vscode.window.showErrorMessage)(r.last.text);
    changed();
  }

  async function drop(host, db) {
    const row = known(db);
    if (row && row.sessions) { vscode.window.showWarningMessage(`${db} has ${row.sessions} session(s) on ${host}: an instance is using it.`); return; }
    const choice = await vscode.window.showWarningMessage(`Delete ${db} on ${host}? It is gone from the host and cannot be undone there.`,
      { modal: true }, "Back up here, then delete", "Delete without a backup");
    if (!choice) return;
    const typed = await vscode.window.showInputBox({ title: `Delete ${db} on ${host}`, prompt: `Type ${host}/${db} to confirm`, validateInput: (v) => (v === `${host}/${db}` ? null : `type ${host}/${db} exactly`) });
    if (typed !== `${host}/${db}`) return;
    const result = await job(`Deleting ${db} on ${host}`, (project) => run(project, cli(project, "remote-drop",
      ["--host", host, "-d", db, "--confirm", `${host}/${db}`, "--json", ...(choice.startsWith("Back up") ? ["--backup-dir", api.getBackupDir(project)] : [])], false)));
    if (!result) return;
    r.last = result.ok ? { level: "ok", text: `Deleted ${db} on ${host}${result.backup ? ` (backup: ${result.backup})` : ""}.` } : { level: "bad", text: `Not deleted: ${result.error}` };
    (result.ok ? vscode.window.showInformationMessage : vscode.window.showWarningMessage)(r.last.text);
    if (result.ok) await refresh();
    changed();
  }

  return { state: () => slimRemote(Object.assign({}, r, { account: account()[1] || null })), loadHosts, select, refresh, pull, push, backup, drop, setupKey, _r: r };
}

module.exports = { create, slimRemote };
