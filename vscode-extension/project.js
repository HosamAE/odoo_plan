"use strict";
// Which project and which Odoo version this window is about, and where its Python, CLI and port are.
const vscode = require("vscode");
const path = require("path");
const fs = require("fs");
const http = require("http");

const DUAL_PROJECT_RX = /(?:odoo(\d+)[\\/]projects|ProjectsOdoo(\d+))[\\/]([^\\/]+)/i;

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

  // Any other folder: an Odoo project that does not follow this layout. Its version is read from its own manifests.
  if (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length > 0) {
    const root = vscode.workspace.workspaceFolders[0].uri.fsPath;
    const name = path.basename(root);
    const version = forcedVersion || seriesFromManifests(root) || "19";
    return { version, name, root, isGeneric: true };
  }
  return null;
}

const SKIP_DIRS = new Set([".git", "node_modules", "venv", ".venv", "__pycache__", "static", "i18n", "tests"]);

/** The Odoo series ("17") that the first manifest found under `root` declares ("17.0.1.0.0"), or null. */
function seriesFromManifests(root) {
  const queue = [[root, 0]];
  let looked = 0;
  while (queue.length && looked < 400) {
    const [dir, depth] = queue.shift();
    looked += 1;
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch (e) { continue; }
    if (entries.some((e) => e.isFile() && e.name === "__manifest__.py")) {
      try {
        const m = fs.readFileSync(path.join(dir, "__manifest__.py"), "utf8").match(/['"]version['"]\s*:\s*['"](\d{2})\.0/);
        if (m) return m[1];
      } catch (e) { /* unreadable: the next one */ }
    }
    if (depth < 3) for (const e of entries) if (e.isDirectory() && !SKIP_DIRS.has(e.name) && !e.name.startsWith(".")) queue.push([path.join(dir, e.name), depth + 1]);
  }
  return null;
}

/** The server configuration of a project: <name>.conf (the layout of these tools), else odoo.conf, else the only *.conf in its root. */
function findConf(project) {
  for (const name of [`${project.name}.conf`, "odoo.conf", ".odoorc"]) {
    const file = path.join(project.root, name);
    if (fs.existsSync(file)) return file;
  }
  try {
    const confs = fs.readdirSync(project.root).filter((f) => /\.conf$/i.test(f));
    if (confs.length === 1) return path.join(project.root, confs[0]);
  } catch (e) { /* no folder to read: the default name below */ }
  return path.join(project.root, `${project.name}.conf`);   // the name a project of this layout gets; it may not exist yet
}

function getProjectPort(project) {
  if (!project) return 8069;
  const cfg = vscode.workspace.getConfiguration("odooCi");
  const cfgPort = cfg.get("serverPort");
  if (cfgPort && typeof cfgPort === "number" && cfgPort > 0) {
    return cfgPort;
  }
  const confFile = findConf(project);
  if (fs.existsSync(confFile)) {
    try {
      const txt = fs.readFileSync(confFile, "utf8");
      const m = txt.match(/^\s*(?:http_port|xmlrpc_port)\s*=\s*(\d+)/m);
      if (m) return parseInt(m[1], 10);
    } catch (e) { /* unreadable: the default below */ }
  }
  return project.isGeneric ? 8069 : parseInt(`${project.version}00`, 10) || 8069;   // a project of these tools' layout takes NN00
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

/** Are the odoo-ai tools there for this project? {found, cli}. A CLI the person named in the settings is believed. */
function toolsStatus(project) {
  const configured = String(vscode.workspace.getConfiguration("odooCi").get("cliPath") || "").trim();
  if (configured) return { found: true, cli: configured };
  const cli = getCliPath(project);
  const real = path.isAbsolute(cli) && fs.existsSync(cli);
  return { found: real, cli: real ? cli : null };
}

module.exports = { findProject, getProjectPort, getOdooRoot, getPythonExe, getCliPath, checkPortOpen, findConf, seriesFromManifests, toolsStatus };
