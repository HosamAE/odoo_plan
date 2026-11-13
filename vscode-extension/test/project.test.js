// Which project a window is about: the path rules of project.js, with Windows and POSIX separators: node test/project.test.js
"use strict";
const Module = require("module");
const assert = require("assert");
const path = require("path");

const workspace = { workspaceFolders: [], workspaceFile: null, getConfiguration: () => ({ get: () => undefined }) };
const realLoad = Module._load;
Module._load = (request, ...rest) => (request === "vscode" ? { workspace } : realLoad(request, ...rest));
const { findProject } = require(path.join(__dirname, "..", "project.js"));

const folder = (fsPath) => ({ uri: { fsPath } });

// a project folder under ProjectsOdoo<NN>, Windows separators (the case a wrong [\\/] class silently loses)
workspace.workspaceFolders = [folder("C:\\odoo\\ProjectsOdoo19\\Demo")];
assert.deepStrictEqual(findProject(), { version: "19", name: "Demo", root: "C:\\odoo\\ProjectsOdoo19\\Demo" });

// a folder inside the project: the root stops at the project
workspace.workspaceFolders = [folder("C:\\odoo\\ProjectsOdoo20\\Demo\\demo_base\\models")];
assert.deepStrictEqual(findProject(), { version: "20", name: "Demo", root: "C:\\odoo\\ProjectsOdoo20\\Demo" });

// the same with POSIX separators
workspace.workspaceFolders = [folder("/home/dev/odoo/ProjectsOdoo18/shop")];
assert.deepStrictEqual(findProject(), { version: "18", name: "shop", root: "/home/dev/odoo/ProjectsOdoo18/shop" });

// the other layout: odoo<NN>/projects/<name>
workspace.workspaceFolders = [folder("C:\\odoo\\odoo17\\projects\\core")];
assert.deepStrictEqual(findProject(), { version: "17", name: "core", root: "C:\\odoo\\odoo17\\projects\\core" });

// the Odoo source folder of a multi-root workspace does not count; the project folder next to it does
workspace.workspaceFolders = [folder("C:\\odoo\\odoo19\\odoo"), folder("C:\\odoo\\ProjectsOdoo19\\demo_project")];
assert.deepStrictEqual(findProject(), { version: "19", name: "demo_project", root: "C:\\odoo\\ProjectsOdoo19\\demo_project" });

// nothing recognisable: the first folder, marked generic, version 19
workspace.workspaceFolders = [folder("D:\\work\\somewhere")];
const generic = findProject();
assert.strictEqual(generic.isGeneric, true);
assert.strictEqual(generic.name, "somewhere");
assert.strictEqual(generic.version, "19");

// no folder at all
workspace.workspaceFolders = [];
assert.strictEqual(findProject(), null);

// a project that does not follow this layout: its version comes from its own manifests and its port from its own conf,
// while a project of the layout keeps its <name>.conf and its xxyy rule (nothing here assigns a port to it)
const os = require("os");
const fs = require("fs");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "odoo-ci-generic-"));
Module._load = (request, ...rest) => (request === "vscode" ? { workspace } : realLoad(request, ...rest));
const { seriesFromManifests, findConf, getProjectPort, toolsStatus } = require(path.join(__dirname, "..", "project.js"));
try {
  fs.mkdirSync(path.join(root, "addons", "my_module"), { recursive: true });
  fs.writeFileSync(path.join(root, "addons", "my_module", "__manifest__.py"), "{'name': 'X', 'version': '17.0.1.0.0', 'depends': ['base']}");
  assert.strictEqual(seriesFromManifests(root), "17", "the series comes from the first manifest found");
  assert.strictEqual(seriesFromManifests(path.join(root, "addons")), "17", "and from any folder above a module");
  fs.mkdirSync(path.join(root, "empty"));
  assert.strictEqual(seriesFromManifests(path.join(root, "empty")), null, "no manifest, no series");
  workspace.workspaceFolders = [folder(root)];
  const found = findProject();
  assert.deepStrictEqual([found.version, found.isGeneric], ["17", true], "an Odoo 17 project outside the layout is Odoo 17, not 19");

  const project = { name: path.basename(root), root, version: "17", isGeneric: true };
  assert.strictEqual(getProjectPort(project), 8069, "with no configuration, a plain project takes Odoo's own default port");
  fs.writeFileSync(path.join(root, "odoo.conf"), "[options]\nhttp_port = 8070\n");
  assert.strictEqual(path.basename(findConf(project)), "odoo.conf");
  assert.strictEqual(getProjectPort(project), 8070, "the port is the one its odoo.conf says");
  fs.writeFileSync(path.join(root, `${project.name}.conf`), "[options]\nhttp_port = 1911\n");
  assert.strictEqual(path.basename(findConf(project)), `${project.name}.conf`, "<name>.conf comes first, as in the layout of these tools");
  assert.strictEqual(getProjectPort(project), 1911, "so a project with its own conf keeps the port its conf gives it");
  // the layout rule: a project of the layout with no conf port gets NN00 (the main project of its version); the extension invents no other port
  const layout = { name: "Demo", root: path.join(root, "nowhere"), version: "19" };
  assert.strictEqual(getProjectPort(layout), 1900, "a project of the layout without a port in its conf: the xx00 of its version");
  // the tools: found when the CLI exists, believed when the person names it, missing otherwise
  const nowhere = path.join(root, "no-odoo-root");   // not this machine's C:\odoo, which has tools
  workspace.getConfiguration = () => ({ get: (key) => (key === "odooRoot" ? nowhere : undefined) });
  assert.strictEqual(toolsStatus(project).found, false, "a root with no tools in it: missing");
  fs.mkdirSync(path.join(nowhere, "odoo17", "tools"), { recursive: true });
  fs.writeFileSync(path.join(nowhere, "odoo17", "tools", "cli.py"), "# stand-in");
  assert.strictEqual(toolsStatus(project).found, true, "the version's tools folder of the root: found");
  workspace.getConfiguration = () => ({ get: (key) => (key === "cliPath" ? "C:/anywhere/odoo-ai" : undefined) });
  assert.strictEqual(toolsStatus(project).found, true, "a CLI named in the settings is believed");
} finally {
  try { fs.rmSync(root, { recursive: true, force: true }); } catch (e) { /* harmless */ }
}

console.log("project OK: ProjectsOdoo<NN> and odoo<NN>/projects are found with \\ and /, a subfolder resolves to its project, the Odoo source folder is skipped, the fallback is marked generic");
