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

module.exports = { findProject, getProjectPort, getOdooRoot, getPythonExe, getCliPath, checkPortOpen };
