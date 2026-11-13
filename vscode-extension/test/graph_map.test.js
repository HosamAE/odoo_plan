// The map of the module being edited: followed from the open file, pinned by pressing a module, nothing computed for a
// page nobody looks at. child_process is replaced, so no real tool runs: node test/graph_map.test.js
"use strict";
const Module = require("module");
const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");

const runs = [];
let answer = (module) => JSON.stringify({ module, generation: "gen-1", nodes: [{ name: module, kind: "focus", depth: 0, folder: "/x/" + module, openable: true }], edges: [], counts: {} });
let delay = () => 0;
const fakeCp = {
  execFile: (file, args, options, done) => {
    const module = args[args.indexOf("--module") + 1];
    runs.push({ file, args });
    setTimeout(() => done(null, answer(module), ""), delay(module));
  },
};
const realLoad = Module._load;
Module._load = (request, ...rest) => (request === "child_process" ? fakeCp : realLoad(request, ...rest));
const { create, moduleOfFile } = require(path.join(__dirname, "..", "graph.js"));

const root = fs.mkdtempSync(path.join(os.tmpdir(), "odoo-ci-map-"));
const makeModule = (...parts) => { fs.mkdirSync(path.join(root, ...parts, "models"), { recursive: true }); fs.writeFileSync(path.join(root, ...parts, "__manifest__.py"), "{}"); };
makeModule("alpha"); makeModule("beta"); makeModule("odoo", "addons", "hr_attendance");
fs.mkdirSync(path.join(root, "docs"), { recursive: true });
const file = (...parts) => path.join(root, ...parts);

const DOCTOR = "config          C:/kg/demo.toml\nproject_id      demo-odoo19\ndata_dir        C:/kg/data/demo-odoo19\n";
const STATUS = "state           fresh\ngeneration      gen-1\ndetail          ok\ngraph commit    aaaaaaaa  branch=main\nworking tree    aaaaaaaa  branch=main\n";
const opened = [];
function make() {
  const api = {
    findProject: () => ({ name: "Demo", version: "19", root }),
    runCli: async (p, args) => ({ code: 0, stdout: args[1] === "doctor" ? DOCTOR : args[1] === "status" ? STATUS : "modules         3\nfiles           9\n", stderr: "" }),
    cliPath: () => path.join(root, "tools", "cli.py"), pythonExe: () => "py", odooRoot: () => "C:/odoo/odoo19", autoUpdateEnabled: () => false,
    openFile: (p) => opened.push(p), activeFile: () => file("alpha", "models", "x.py"), changed: () => {},
  };
  return create(api);
}
const wait = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
  try {
    // which module a file belongs to
    assert.deepStrictEqual(moduleOfFile(file("alpha", "models", "x.py")), { name: "alpha", folder: file("alpha") }, "the nearest folder with a manifest above the file");
    assert.strictEqual(moduleOfFile(file("odoo", "addons", "hr_attendance", "models", "y.py")).name, "hr_attendance", "an Odoo module too");
    assert.strictEqual(moduleOfFile(file("docs", "readme.md")), null, "a file outside every module has no map");

    // the page is not shown: following the editor computes nothing
    let g = make();
    await g.followFile(file("alpha", "models", "x.py"));
    assert.strictEqual(runs.length, 0, "no map is read for a page nobody looks at");
    assert.strictEqual(g.state().map.fileModule, "alpha", "but the module of the file is remembered");

    // the page is shown: the map of the module being edited is read, once
    await g.wanted(true);
    assert.strictEqual(runs.length, 1);
    assert.deepStrictEqual(runs[0].args.slice(1, 7), ["--data-dir", "C:/kg/data/demo-odoo19", "--module", "alpha", "--odoo-root", "C:/odoo/odoo19"]);
    assert.ok(runs[0].args[0].endsWith("kg_map.py"));
    assert.strictEqual(g.state().map.module, "alpha");
    assert.strictEqual(g.state().map.data.module, "alpha");
    await g.followFile(file("alpha", "models", "z.py"));
    assert.strictEqual(runs.length, 1, "another file of the same module draws nothing again");

    // another module's file: its map replaces it; going back to the first one costs no new run (cache)
    await g.followFile(file("beta", "models", "x.py"));
    assert.strictEqual(g.state().map.module, "beta");
    assert.strictEqual(runs.length, 2);
    await g.followFile(file("alpha", "models", "x.py"));
    assert.strictEqual(runs.length, 2, "a map already read is not read again");
    assert.strictEqual(g.state().map.data.module, "alpha");

    // pressing a module on the map: its own tree, the follow switches off, and there is a way back
    await g.pin("hr_attendance");
    assert.strictEqual(g.state().map.module, "hr_attendance");
    assert.strictEqual(g.state().map.follow, false, "pressing a module stops the map from following the editor");
    assert.deepStrictEqual(g.state().map.trail, ["alpha"]);
    await g.followFile(file("beta", "models", "x.py"));
    assert.strictEqual(g.state().map.module, "hr_attendance", "a file opened meanwhile does not take the map away");
    await g.pin("hr");
    assert.deepStrictEqual(g.state().map.trail, ["alpha", "hr_attendance"]);
    await g.back();
    assert.strictEqual(g.state().map.module, "hr_attendance");
    await g.back();
    assert.strictEqual(g.state().map.module, "alpha");
    await g.back();
    assert.strictEqual(g.state().map.module, "alpha", "nothing to go back to: it stays");

    // following again: the module of the file being edited, and the trail is forgotten
    await g.follow(true);
    assert.deepStrictEqual(g.state().map.trail, []);
    assert.strictEqual(g.state().map.follow, true);

    // its manifest opens in the editor, from the folder the map gave
    await g.pin("odoo_thing");
    await g.openModule("odoo_thing");
    assert.strictEqual(opened.length, 1);
    assert.ok(opened[0].endsWith("__manifest__.py") && /odoo_thing/.test(opened[0]), opened[0]);

    // an answer that arrives after the person moved on is dropped
    g = make();
    await g.wanted(true);
    delay = (module) => (module === "slow" ? 80 : 5);
    const slow = g.pin("slow");
    await wait(10);
    const quick = g.pin("quick");
    await Promise.all([slow, quick]);
    await wait(120);
    assert.strictEqual(g.state().map.module, "quick");
    assert.strictEqual(g.state().map.data.module, "quick", "the slow answer did not overwrite the newer one");
    delay = () => 0;

    // the tool says no (a module nowhere) or says nothing readable: the page gets a sentence, not a crash
    g = make();
    await g.wanted(true);
    answer = (module) => JSON.stringify({ module, error: "this module is neither in the project's graph nor in Odoo's folders" });
    await g.pin("ghost");
    assert.strictEqual(g.state().map.data, null);
    assert.ok(/neither in the project/.test(g.state().map.error));
    answer = () => "not json";
    await g.pin("garbled");
    assert.ok(/could not be read/.test(g.state().map.error));
    console.log("graph map OK: the module of a file; nothing read for a hidden page; read once and cached; pressing a module pins it with a way back; a late answer is dropped; errors are sentences");
  } finally {
    try { fs.rmSync(root, { recursive: true, force: true }); } catch (e) { /* harmless */ }
  }
})().catch((error) => { console.error(error); process.exit(1); });
