// A server started anywhere else (here: decoy Python processes with an odoo-bin command line) must be found,
// attributed and counted once - and nothing that is not a Python server may be taken for one. Windows/Linux.
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

function pythonExe() {
  for (const candidate of ["python", "python3"]) {
    if (cp.spawnSync(candidate, ["--version"], { stdio: "ignore" }).status === 0) return candidate;
  }
  throw new Error("these tests start decoy servers with Python: none found");
}

async function main() {
  const python = pythonExe();
  const sleeper = ["-c", "import time; time.sleep(30)"];
  const spawn = (extra) => cp.spawn(python, [...sleeper, "odoo-bin", ...extra], { stdio: "ignore" });
  // a server of this project, a server of another project, and a SHELL whose command line mentions odoo-bin
  const mine = spawn(["-c", conf, "--http-port=1999"]);
  const other = spawn(["--config=/elsewhere/Other.conf", "-p", "2999"]);
  const shellLine = `${python} -c "import time; time.sleep(30)" odoo-bin -c ${conf} --http-port=1888`;
  const shell = process.platform === "win32"
    ? cp.spawn("cmd.exe", ["/d", "/s", "/c", `"${shellLine}"`], { stdio: "ignore", windowsHide: true, windowsVerbatimArguments: true })
    : cp.spawn("sh", ["-c", shellLine], { stdio: "ignore" });
  try {
    await new Promise((r) => setTimeout(r, 1200));
    const servers = await _test.detectServers(project);

    const found = servers.filter((s) => s.port === 1999);
    assert.strictEqual(found.length, 1, `one server on :1999 expected, got ${JSON.stringify(found)}`);
    assert.strictEqual(found[0].mine, true, "found and attributed to this project");

    const another = servers.find((s) => s.port === 2999);
    assert.ok(another, "another project's server is found too");
    assert.strictEqual(another.mine, false, "and reported as someone else's");

    // the shell's own command line contains odoo-bin, but the shell is not a server: the python it started is the
    // only thing on :1888, once - never the shell itself, whose end (Stop) would close a terminal
    const viaShell = servers.filter((s) => s.port === 1888);
    assert.strictEqual(viaShell.length, 1, `the shell and its python are ONE server on :1888, got ${JSON.stringify(viaShell)}`);
    assert.notStrictEqual(viaShell[0].pid, shell.pid, "the shell wrapper must never be listed as a server");

    // what the Stop button does to a server it did not start: end it, whatever started it
    await _test.killTree(found[0].pid);
    await new Promise((r) => setTimeout(r, 800));
    const after = await _test.detectServers(project);
    assert.ok(!after.some((s) => s.port === 1999), "the :1999 server was still running after Stop");
    assert.ok(after.some((s) => s.port === 2999), "stopping one server must not stop another project's");
    console.log(`detect OK: ${servers.length} servers counted once each (:1999 this project, :2999 another, :1888 via a shell, never the shell); Stop ended one and left the others`);
  } finally {
    for (const child of [mine, other, shell]) { try { child.kill(); } catch (e) { /* gone */ } }
  }
}
main().then(() => process.exit(0), (error) => { console.error(error); process.exit(1); });
