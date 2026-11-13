// The quiet update when a project opens: it runs only for a graph that is behind (or missing), once, and only if allowed.
// child_process is replaced, so nothing real is started: node test/graph_auto.test.js
"use strict";
const Module = require("module");
const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");

const started = [];
const fakeCp = { execFile: (file, args, options, done) => { started.push({ file, args, cwd: options.cwd }); setImmediate(() => done(null, "", "")); } };
const realLoad = Module._load;
Module._load = (request, ...rest) => (request === "child_process" ? fakeCp : realLoad(request, ...rest));
const { create } = require(path.join(__dirname, "..", "graph.js"));

const tools = fs.mkdtempSync(path.join(os.tmpdir(), "odoo-ci-auto-"));
fs.writeFileSync(path.join(tools, "kg_open.py"), "# stand-in");
fs.writeFileSync(path.join(tools, "cli.py"), "# stand-in");
const CONFIG = "C:/kg/config/local/demo-odoo19.toml";
const DATA = path.join(tools, "data").split(path.sep).join("/");
const DOCTOR = `config          ${CONFIG}\nproject_id      demo-odoo19\ndata_dir        ${DATA}\n`;
const STATUS = (state) => `project         demo-odoo19\nstate           ${state}\ndetail          x\ngraph commit    aaaaaaaa11111111  branch=main\nworking tree    bbbbbbbb22222222  branch=main\nindexed_at      2026-11-12T00:00:00+00:00\n`;
const STATS = "files           10\nmodules         3\ndepends_on      5\n  resolved     2\n  unresolved   3\ndiagnostics     0\n";

function make(state, enabled = true, keep = 0) {
  const calls = [];
  const api = {
    keepGenerations: () => keep,
    findProject: () => ({ name: "Demo", version: "19", root: tools }),
    runCli: async (p, args) => {
      calls.push(args.slice(0, 2).join(" "));
      const sub = args[1];
      return { code: sub === "status" && state !== "fresh" ? 1 : 0, stdout: sub === "status" ? STATUS(state) : sub === "doctor" ? DOCTOR : sub === "stats" ? STATS : "", stderr: "" };
    },
    cliPath: () => path.join(tools, "cli.py"), pythonExe: () => "py", autoUpdateEnabled: () => enabled, changed: () => {},
  };
  return { g: create(api), calls };
}

(async () => {
  try {
    let t = make("stale_commit");
    await t.g.autoUpdate();
    assert.strictEqual(started.length, 1, "a graph behind the code is updated");
    assert.strictEqual(started[0].file, "py");
    assert.deepStrictEqual(started[0].args, [path.join(tools, "kg_open.py"), "--config", CONFIG], "by the same script the workspace task used, with this project's config");
    assert.strictEqual(started[0].cwd, path.join(tools, "kg"));
    assert.strictEqual(t.g.state().busy, null, "and the page is not left 'updating'");
    await t.g.autoUpdate();
    assert.strictEqual(started.length, 1, "only once per window");

    started.length = 0;
    t = make("fresh");
    await t.g.autoUpdate();
    assert.strictEqual(started.length, 0, "a fresh graph is left alone");
    assert.strictEqual(t.g.state().status.state, "fresh", "but it is read, so the page has it");

    t = make("stale_commit", false);
    await t.g.autoUpdate();
    assert.strictEqual(started.length, 0, "turned off by the setting: nothing runs");
    assert.strictEqual(t.calls.length, 0, "and nothing is even read");

    t = make("stale_commit");
    await t.g.update();
    assert.ok(t.calls.includes("kg index"), "the Update button runs `kg index`");
    assert.strictEqual(t.g.state().last.level, "ok", "and says it worked");
    // old generations are only removed when the person asked (the setting is 0 by default: nothing is deleted)
    const generations = path.join(DATA, "generations");
    fs.mkdirSync(generations, { recursive: true });
    fs.writeFileSync(path.join(DATA, "CURRENT"), JSON.stringify({ generation: "gen-3" }));
    ["gen-1", "gen-2", "gen-3"].forEach((name, i) => {
      fs.mkdirSync(path.join(generations, name));
      const at = new Date(Date.now() - (3 - i) * 60000);
      fs.utimesSync(path.join(generations, name), at, at);
    });
    t = make("stale_commit", true, 0);
    await t.g.update();
    assert.strictEqual(fs.readdirSync(generations).length, 3, "with the setting at 0 an update deletes nothing");
    t = make("stale_commit", true, 1);
    await t.g.update();
    assert.deepStrictEqual(fs.readdirSync(generations), ["gen-3"], "asked to keep one, an update keeps the published generation and removes the rest");
    console.log("graph auto OK: a graph behind the code is updated quietly, once, by kg_open.py with its config; fresh or switched off: nothing runs; Update runs kg index");
  } finally {
    try { fs.rmSync(tools, { recursive: true, force: true }); } catch (e) { /* harmless */ }
  }
})().catch((error) => { console.error(error); process.exit(1); });
