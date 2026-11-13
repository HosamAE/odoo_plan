// The graph page's reading of `odoo-ai kg`: real outputs of 2026-11-13, and the check of Claude's connection: node test/graph.test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { parseStatus, parseStats, parseDoctor, mcpWiring } = require(path.join(__dirname, "..", "graph.js"));

const STALE = `project         acme-odoo19
source          generation
generation      gen-03dea867e8b84dd6aa90db72e49760eb
state           stale_commit
detail          HEAD moved from 8164198 to 1767376 since indexing
graph commit    81641986642a9e4cc96481eb0a89a05a1b86043c  branch=testing19
working tree    1767376789a191c1bf913fb2ad70dd3db6915b4e  branch=testing19
indexed_at      2026-11-12T00:16:04.522914+00:00

Stale means the graph describes an older tree than the one on disk.
[KG] Acme (Odoo 19): -m odoo_kg status --config C:/odoo/odoo19/tools/kg/config/local/acme-odoo19.toml`;
const FRESH = STALE.replace("stale_commit", "fresh").replace("graph commit    81641986642a9e4cc96481eb0a89a05a1b86043c", "graph commit    1767376789a191c1bf913fb2ad70dd3db6915b4e")
  .replace("HEAD moved from 8164198 to 1767376 since indexing", "graph matches HEAD 1767376 and the working tree");

const s = parseStatus(STALE);
assert.strictEqual(s.state, "stale");
assert.strictEqual(s.graphCommit, "81641986");
assert.strictEqual(s.workingTree, "17673767");
assert.strictEqual(s.branch, "testing19");
assert.ok(/HEAD moved/.test(s.detail) && s.indexedAt.startsWith("2026-11-12"));
assert.strictEqual(parseStatus(FRESH).state, "fresh");
assert.strictEqual(parseStatus(FRESH).graphCommit, parseStatus(FRESH).workingTree, "a fresh graph is at the commit of the tree");
assert.strictEqual(parseStatus("[KG] no graph config for Demo yet: run `odoo-ai workspace -p <project>` first (C:/x.toml)").state, "unconfigured");
assert.strictEqual(parseStatus("no graph database yet").state, "missing");
assert.strictEqual(parseStatus("").state, "unknown");

const STATS = `project         acme-odoo19  milestone=M4
repo_root       C:\\odoo\\ProjectsOdoo19\\Acme
files           4291
modules         152
depends_on      571
  resolved     311
  unresolved   260
  origin core-vendored   1
diagnostics     0`;
assert.deepStrictEqual(parseStats(STATS), { files: 4291, modules: 152, dependsOn: 571, resolved: 311, unresolved: 260, diagnostics: 0 });
assert.strictEqual(parseStats("nothing useful"), null);

const DOCTOR = `config          C:\\odoo\\odoo19\\tools\\kg\\config\\local\\acme-odoo19.toml
project_id      acme-odoo19
repo_root       C:\\odoo\\ProjectsOdoo19\\Acme  exists=True
data_dir        C:\\Users\\me\\AppData\\Local\\odoo-kg\\projects\\acme-odoo19
db_path         C:\\Users\\me\\AppData\\Local\\odoo-kg\\projects\\acme-odoo19\\graph.db  exists=False`;
assert.deepStrictEqual(parseDoctor(DOCTOR), { config: "C:\\odoo\\odoo19\\tools\\kg\\config\\local\\acme-odoo19.toml", projectId: "acme-odoo19", dataDir: "C:\\Users\\me\\AppData\\Local\\odoo-kg\\projects\\acme-odoo19" });

// Claude's connection: the project's .mcp.json
const root = fs.mkdtempSync(path.join(os.tmpdir(), "odoo-ci-graph-"));
const write = (text) => fs.writeFileSync(path.join(root, ".mcp.json"), text);
const config = "C:\\odoo\\odoo19\\tools\\kg\\config\\local\\acme-odoo19.toml";
try {
  assert.strictEqual(mcpWiring(root, config).state, "missing");
  write("{ not json");
  assert.strictEqual(mcpWiring(root, config).state, "broken");
  write('{\n  "mcpServers": {\n  }\n}\n');
  assert.strictEqual(mcpWiring(root, config).state, "empty", "the skeleton Acme had until 2026-11-13");
  write(JSON.stringify({ mcpServers: { other: { command: "x" } } }));
  assert.strictEqual(mcpWiring(root, config).state, "other");
  write(JSON.stringify({ mcpServers: { "odoo-kg": { args: ["-m", "odoo_kg", "serve", "--config", "C:/odoo/odoo19/tools/kg/config/local/acme-odoo19.toml"] } } }));
  assert.strictEqual(mcpWiring(root, config).state, "ok", "slashes and case do not matter");
  write(JSON.stringify({ mcpServers: { "odoo-kg": { args: ["--config", "C:/odoo/odoo19/tools/kg/config/local/another.toml"] } } }));
  assert.strictEqual(mcpWiring(root, config).state, "other", "a server that reads another graph is not this project's");
  write("\uFEFF" + JSON.stringify({ mcpServers: { "odoo-kg": { args: [] } } }));
  assert.strictEqual(mcpWiring(root, config).state, "ok", "a file saved with a BOM is still read");
} finally {
  try { fs.rmSync(root, { recursive: true, force: true }); } catch (e) { /* a temp folder left behind is harmless */ }
}

// the engine never removes old generations of the graph: the page keeps the published one and the newest few
const { pruneGenerations } = require(path.join(__dirname, "..", "graph.js"));
const data = fs.mkdtempSync(path.join(os.tmpdir(), "odoo-ci-gens-"));
try {
  const make = (name, ageMinutes) => {
    const dir = path.join(data, "generations", name);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, "graph.db"), "x");
    const at = new Date(Date.now() - ageMinutes * 60000);
    fs.utimesSync(dir, at, at);
  };
  ["gen-a", "gen-b", "gen-c", "gen-d", "gen-e", "gen-f"].forEach((n, i) => make(n, i * 10));   // gen-a is the newest
  fs.mkdirSync(path.join(data, "generations", "building"), { recursive: true });                  // not a generation: never touched
  assert.strictEqual(pruneGenerations(data), 0, "with nothing published, nothing is removed");
  fs.writeFileSync(path.join(data, "CURRENT"), JSON.stringify({ generation: "gen-e" }));          // the published one is an OLD one
  assert.strictEqual(pruneGenerations(data), 2, "the newest three stay, the published one stays, the rest go");
  const left = fs.readdirSync(path.join(data, "generations")).sort();
  assert.deepStrictEqual(left, ["building", "gen-a", "gen-b", "gen-c", "gen-e"], "published gen-e is kept although it is old; gen-d and gen-f are gone");
  assert.strictEqual(pruneGenerations(data), 0, "a second run has nothing left to do");
  assert.strictEqual(pruneGenerations(path.join(data, "no-such-folder")), 0, "a folder that is not there is not an error");
} finally {
  try { fs.rmSync(data, { recursive: true, force: true }); } catch (e) { /* harmless */ }
}

console.log("graph OK: status (fresh, stale, missing, no config), counts, doctor paths, Claude's connection (ok, empty, other, broken, missing), old generations pruned but the published one");
