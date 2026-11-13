// The server controls: a running server is attributed to its database, and the new commands are declared.
"use strict";
const Module = require("module");
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const noop = () => {};
const vscodeStub = new Proxy({}, { get: () => new Proxy(function () {}, { get: () => noop, apply: noop, construct: () => ({}) }) });
const realLoad = Module._load;
Module._load = (request, ...rest) => (request === "vscode" ? vscodeStub : realLoad(request, ...rest));
const { _test } = require(path.join(__dirname, "..", "extension.js"));

const project = { root: "C:\\odoo\\ProjectsOdoo19\\Demo", name: "Demo", version: "19" };
const line = (args) => `"C:\\odoo\\odoo19\\venv\\Scripts\\python.exe" C:\\odoo\\odoo19\\odoo\\odoo-bin ${args}`;

// the database a server serves is read from -d / --database, in any form
for (const [args, db] of [
  ["-c C:\\odoo\\ProjectsOdoo19\\Demo\\Demo.conf -d dev_full_ci", "dev_full_ci"],
  ["-c C:\\odoo\\ProjectsOdoo19\\Demo\\Demo.conf --database=other_db --dev=all", "other_db"],
  ["-c C:\\odoo\\ProjectsOdoo19\\Demo\\Demo.conf --database other", "other"],
  ["-c C:\\odoo\\ProjectsOdoo19\\Demo\\Demo.conf --dev=all", null],
]) {
  const info = _test.parseOdooCommand(line(args), project);
  assert.ok(info, `${args}: a server`);
  assert.strictEqual(info.db, db, `${args}: database`);
  assert.strictEqual(info.mine, true, `${args}: this project's`);
}

// an upgrade run is not a server, whatever its database
assert.strictEqual(_test.parseOdooCommand(line("-c Demo.conf -d x -u all --stop-after-init"), project), null);

// every command the new controls register is declared for the user, and every command declared is registered
const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
const declared = new Set(pkg.contributes.commands.map((c) => c.command));
for (const command of ["odooCi.serverUpgrade", "odooCi.serverDrift", "odooCi.serverHealth", "odooCi.serverRun", "odooCi.serverRunQuick"]) {
  assert.ok(declared.has(command), `${command} must be declared in package.json`);
}
const source = fs.readFileSync(path.join(__dirname, "..", "extension.js"), "utf8");
for (const command of declared) {
  assert.ok(source.includes(`"${command}"`), `${command} is declared but never registered`);
}
// the extension asks the tools for these things, and the tools have them
const server = fs.readFileSync(path.join(__dirname, "..", "..", "server_ctl.py"), "utf8");
for (const sub of ["dbs", "command", "upgrade", "drift", "health"]) {
  assert.ok(server.includes(`sub.add_parser("${sub}"`), `server_ctl has no '${sub}'`);
}
console.log("server OK: the database of a running server is read from -d/--database; commands declared and registered; tools have dbs/command/upgrade/drift/health");
