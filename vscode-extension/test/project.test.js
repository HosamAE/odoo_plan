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
workspace.workspaceFolders = [folder("C:\\odoo\\ProjectsOdoo19\\Acme")];
assert.deepStrictEqual(findProject(), { version: "19", name: "Acme", root: "C:\\odoo\\ProjectsOdoo19\\Acme" });

// a folder inside the project: the root stops at the project
workspace.workspaceFolders = [folder("C:\\odoo\\ProjectsOdoo20\\Acme\\acme_base\\models")];
assert.deepStrictEqual(findProject(), { version: "20", name: "Acme", root: "C:\\odoo\\ProjectsOdoo20\\Acme" });

// the same with POSIX separators
workspace.workspaceFolders = [folder("/home/dev/odoo/ProjectsOdoo18/shop")];
assert.deepStrictEqual(findProject(), { version: "18", name: "shop", root: "/home/dev/odoo/ProjectsOdoo18/shop" });

// the other layout: odoo<NN>/projects/<name>
workspace.workspaceFolders = [folder("C:\\odoo\\odoo17\\projects\\core")];
assert.deepStrictEqual(findProject(), { version: "17", name: "core", root: "C:\\odoo\\odoo17\\projects\\core" });

// the Odoo source folder of a multi-root workspace does not count; the project folder next to it does
workspace.workspaceFolders = [folder("C:\\odoo\\odoo19\\odoo"), folder("C:\\odoo\\ProjectsOdoo19\\sample_engine")];
assert.deepStrictEqual(findProject(), { version: "19", name: "sample_engine", root: "C:\\odoo\\ProjectsOdoo19\\sample_engine" });

// nothing recognisable: the first folder, marked generic, version 19
workspace.workspaceFolders = [folder("D:\\work\\somewhere")];
const generic = findProject();
assert.strictEqual(generic.isGeneric, true);
assert.strictEqual(generic.name, "somewhere");
assert.strictEqual(generic.version, "19");

// no folder at all
workspace.workspaceFolders = [];
assert.strictEqual(findProject(), null);

console.log("project OK: ProjectsOdoo<NN> and odoo<NN>/projects are found with \\ and /, a subfolder resolves to its project, the Odoo source folder is skipped, the fallback is marked generic");
