"use strict";
// Finding the Odoo servers running on this machine, however they were started, and ending one.
// Pure of editor state: it reads process lists and .conf files only.
const cp = require("child_process");
const path = require("path");
const fs = require("fs");

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

/** The log file a server was told to write (its --logfile, else the .conf's logfile), or null: stdout in a terminal cannot be read. */
function logfileOf(tokens, conf, project) {
  let file = null;
  for (let i = 0; i < tokens.length; i++) {
    const m = tokens[i].match(/^--logfile=(.+)$/);
    if (m) file = m[1];
    else if (tokens[i] === "--logfile") file = tokens[i + 1] || file;
  }
  if (!file && conf) {
    for (const candidate of [conf, project && project.root ? path.join(project.root, conf) : null].filter(Boolean)) {
      try {
        const m = fs.readFileSync(candidate, "utf8").match(/^\s*logfile\s*=\s*(.+?)\s*$/m);
        if (m && !/^(false|none|)$/i.test(m[1])) { file = m[1]; break; }
      } catch (e) { /* not readable from here */ }
    }
  }
  return file && file.trim() ? file.trim() : null;
}

const ODOO_SUBCOMMANDS = new Set(["shell", "scaffold", "db", "deploy", "i18n", "populate", "cloc", "neutralize", "obfuscate", "module", "upgrade_code", "help", "server"]);

/** What a command line says about an Odoo server: {port, conf, mine}, or null when it is not a running server
 *  (not Odoo, an update/test run with --stop-after-init, or a subcommand such as `shell`). */
function parseOdooCommand(cmd, project) {
  if (!/(?:odoo-bin|odoo\.py|-m\s+odoo(?:\s|$))/i.test(cmd)) return null;
  if (/--stop-after-init/.test(cmd)) return null;
  const tokens = splitCommandLine(cmd);
  // The program must be a Python interpreter. A shell, a terminal or an editor whose command line merely mentions
  // odoo-bin is not a server, and Stop must never end one (it would close a terminal or the editor itself).
  if (!/^pythonw?[\d.]*(?:\.exe)?$/i.test((tokens[0] || "").split(/[\\/]/).pop())) return null;
  // odoo-bin must be the PROGRAM being run, not the value of an option of some other program: `odoo-ai server drift
  // --odoo-bin C:/.../odoo-bin ...` carries the path and is not a server (it showed up as one, on port 8069, while
  // the Cockpit's own drift check ran).
  const bin = tokens.findIndex((t, i) => !t.startsWith("-") && (/odoo-bin$|odoo\.py$/i.test(t) || t === "odoo") && !(i > 0 && /^--[\w-]+$/.test(tokens[i - 1])));
  if (bin < 0) return null;
  const after = tokens.slice(bin + 1).find((t) => !t.startsWith("-"));
  if (bin >= 0 && after && ODOO_SUBCOMMANDS.has(after) && after !== "server") return null;
  let conf = null;
  let port = null;
  let db = null;
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    let m;
    if ((m = t.match(/^--config=(.+)$/))) conf = m[1];
    else if (t === "-c" || t === "--config") conf = tokens[i + 1] || conf;
    else if ((m = t.match(/^--database=(.+)$/))) db = m[1];
    else if (t === "-d" || t === "--database") db = tokens[i + 1] || db;
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
  return { port, conf, mine, db, logfile: logfileOf(tokens, conf, project) };
}

/** Every process on this machine that looks like an Odoo server: [{pid, cmd}]. Works for a terminal, a task, a debugger, a service. */
function listOdooProcesses() {
  return new Promise((resolve) => {
    if (process.platform === "win32") {
      const script = "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?[\\d.]*\\.exe$' -and $_.CommandLine -match 'odoo-bin|odoo\\.py|-m odoo' } | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress";
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
      cp.execFile("ps", ["-eo", "pid=,comm=,args="], { timeout: 5000, maxBuffer: 4 * 1024 * 1024 }, (err, stdout) => {
        if (err) return resolve([]);
        resolve(String(stdout).split("\n").map((line) => line.match(/^\s*(\d+)\s+(\S+)\s+(.*)$/))
          .filter((m) => m && /^python/i.test(m[2]) && /odoo-bin|odoo\.py|-m odoo/.test(m[3]))
          .map((m) => ({ pid: parseInt(m[1], 10), cmd: m[3] })));
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
    // A venv's python.exe starts the real interpreter as a second process: same arguments, a different program
    // path. They are one server, counted once; the parent (lowest pid) is kept, and ending it ends the child.
    const key = splitCommandLine(proc.cmd).slice(1).join("\u0000");
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

module.exports = { splitCommandLine, parseOdooCommand, listOdooProcesses, detectServers, killTree };
