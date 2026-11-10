// A server started anywhere else (here: a decoy process with an odoo-bin command line) must be found and attributed. Windows/Linux.
"use strict";
const Module = require("module");
const assert = require("assert");
const cp = require("child_process");
const path = require("path");

const noop = () => {};
const vscodeStub = new Proxy({}, { get: () => new Proxy(function () {}, { get: () => noop, apply: noop, construct: () => ({}) }) });
const realLoad = Module._load;
Module._load = (request, ...rest) => (request === "vscode" ? vscodeStub : realLoad(request, ...rest));
const { _test } = require(path.join(__dirname, "..", "extension.js"));

const B = "\\";
const root = process.platform === "win32" ? ["C:", "odoo", "ProjectsOdoo19", "DetectDemo"].join(B) : "/srv/odoo/ProjectsOdoo19/DetectDemo";
const project = { root, name: "DetectDemo", version: "19" };
const conf = path.join(root, "DetectDemo.conf");

async function main() {
  // the decoy sleeps; its command line is what an Odoo server's looks like, started from outside VS Code
  const decoy = cp.spawn(process.execPath, ["-e", "setTimeout(() => {}, 20000)", "odoo-bin", "-c", conf, "--http-port=1999"], { stdio: "ignore" });
  const other = cp.spawn(process.execPath, ["-e", "setTimeout(() => {}, 20000)", "odoo-bin", "--config=/elsewhere/Other.conf", "-p", "2999"], { stdio: "ignore" });
  try {
    await new Promise((r) => setTimeout(r, 800));
    const servers = await _test.detectServers(project);
    const mine = servers.find((s) => s.pid === decoy.pid);
    const another = servers.find((s) => s.pid === other.pid);
    assert.ok(mine, `the decoy server (pid ${decoy.pid}) was not found among ${JSON.stringify(servers)}`);
    assert.deepStrictEqual([mine.port, mine.mine], [1999, true], "found and attributed to this project");
    assert.ok(another, "another project's server is found too");
    assert.deepStrictEqual([another.port, another.mine], [2999, false], "and reported as someone else's");
    console.log(`detect OK: found pid ${mine.pid} (:${mine.port}, this project) and pid ${another.pid} (:${another.port}, another)`);
  } finally {
    decoy.kill();
    other.kill();
  }
}
main().then(() => process.exit(0), (error) => { console.error(error); process.exit(1); });
