"use strict";
// The project's knowledge graph in the cockpit: how fresh it is, the buttons that keep it fresh, and the wiring that
// lets Claude read it. Nothing here touches the editor: it runs `odoo-ai kg ...` and reads what comes back, so the
// same graph stays usable from a terminal and from Claude without VS Code.
const cp = require("child_process");
const fs = require("fs");
const path = require("path");

const FIELD_KEYS = new Set(["project", "source", "generation", "state", "detail", "graph commit", "working tree", "indexed_at", "config", "project_id", "data_dir"]);

/** `key<two or more spaces>value` lines of `odoo-ai kg status|doctor`, for the keys the page uses. */
function parseFields(text) {
  const out = {};
  for (const line of String(text || "").split(/\r?\n/)) {
    const m = line.match(/^([a-z_]+(?: [a-z]+)?)\s{2,}(.+?)\s*$/);
    if (m && FIELD_KEYS.has(m[1]) && !(m[1] in out)) out[m[1]] = m[2];
  }
  return out;
}

const shortCommit = (value) => String(value || "").split(/\s+/)[0].slice(0, 8) || null;
const branchOf = (value) => { const m = String(value || "").match(/branch=(\S+)/); return m ? m[1] : null; };

/** What `odoo-ai kg status` says. `state` is one of: fresh, stale, missing, unconfigured, unknown. */
function parseStatus(text) {
  const raw = String(text || "");
  if (/no graph config for/i.test(raw)) return { state: "unconfigured", detail: "this project has no graph config yet", indexedAt: null };
  const f = parseFields(raw);
  let state = "unknown";
  if (/no graph database|no published generation/i.test(raw) && !f.state) state = "missing";
  else if (f.state === "fresh") state = "fresh";
  else if (f.state && f.state.startsWith("stale")) state = "stale";
  else if (f.state) state = f.state;
  return {
    state, detail: f.detail || null, generation: f.generation || null, indexedAt: f.indexed_at || null,
    graphCommit: shortCommit(f["graph commit"]), workingTree: shortCommit(f["working tree"]), branch: branchOf(f["working tree"]) || branchOf(f["graph commit"]),
  };
}

/** The counts of `odoo-ai kg stats`. */
function parseStats(text) {
  const raw = String(text || "");
  const num = (key) => { const m = raw.match(new RegExp(`^\\s*${key}\\s+(\\d+)`, "m")); return m ? parseInt(m[1], 10) : null; };
  const out = { files: num("files"), modules: num("modules"), dependsOn: num("depends_on"), resolved: num("resolved"), unresolved: num("unresolved"), diagnostics: num("diagnostics") };
  return out.modules === null ? null : out;
}

/** Where `odoo-ai kg doctor` says the config and the data of this project are. */
function parseDoctor(text) {
  const f = parseFields(text);
  return { config: f.config || null, projectId: f.project_id || null, dataDir: f.data_dir || null };
}

const norm = (p) => String(p || "").replace(/\\/g, "/").toLowerCase();

/** Is Claude Code connected to this project's graph? Reads the project's .mcp.json. state: ok, other, empty, missing, broken. */
function mcpWiring(projectRoot, configPath) {
  const file = path.join(projectRoot, ".mcp.json");
  let text;
  try { text = fs.readFileSync(file, "utf8"); } catch (e) { return { state: "missing", detail: "no .mcp.json in the project" }; }
  let data;
  try { data = JSON.parse(text.replace(/^﻿/, "")); } catch (e) { return { state: "broken", detail: ".mcp.json is not valid JSON" }; }
  const servers = (data && data.mcpServers) || {};
  const kg = servers["odoo-kg"];
  if (!kg) return { state: Object.keys(servers).length ? "other" : "empty", detail: Object.keys(servers).length ? "no odoo-kg server in .mcp.json" : ".mcp.json lists no server: Claude cannot read the graph" };
  const args = (kg.args || []).map(String);
  const at = args.indexOf("--config");
  if (configPath && at >= 0 && norm(args[at + 1]) !== norm(configPath)) return { state: "other", detail: "the server reads another graph config" };
  return { state: "ok", detail: "Claude Code reads this project's graph" };
}

function lastLines(res, n = 3) {
  const lines = (String(res.stdout || "") + "\n" + String(res.stderr || "")).split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  return lines.slice(-n).join(" · ").slice(0, 400);
}

/** Every update leaves a new generation of the graph (about 40 MB for a project this size) and the engine never removes
 *  the old ones. Keep the published one and the newest few, remove the rest; how many were removed is returned.
 *  A generation a reader still holds open cannot be removed on Windows: it is left for the next time. */
function pruneGenerations(dataDir, keep = 3) {
  const dir = path.join(dataDir || "", "generations");
  let current;
  try { current = JSON.parse(fs.readFileSync(path.join(dataDir, "CURRENT"), "utf8")).generation; } catch (e) { return 0; }   // nothing published: touch nothing
  let found;
  try {
    found = fs.readdirSync(dir, { withFileTypes: true }).filter((d) => d.isDirectory() && d.name.startsWith("gen-"))
      .map((d) => ({ name: d.name, at: fs.statSync(path.join(dir, d.name)).mtimeMs })).sort((a, b) => b.at - a.at);
  } catch (e) { return 0; }
  const kept = new Set([current, ...found.slice(0, keep).map((d) => d.name)]);
  let removed = 0;
  for (const d of found) {
    if (kept.has(d.name)) continue;
    try { fs.rmSync(path.join(dir, d.name), { recursive: true, force: true }); removed += 1; } catch (e) { /* in use: next time */ }
  }
  return removed;
}

/** The module a file belongs to: the nearest folder above it that holds a __manifest__.py. {name, folder} or null. */
function moduleOfFile(file) {
  let dir = path.dirname(String(file || ""));
  for (let i = 0; i < 12; i++) {
    if (fs.existsSync(path.join(dir, "__manifest__.py"))) return { name: path.basename(dir), folder: dir };
    const up = path.dirname(dir);
    if (up === dir) break;
    dir = up;
  }
  return null;
}

/** The controller: one per window.
 *  `api` = { findProject, runCli, cliPath, pythonExe, odooRoot, openFile, activeFile, autoUpdateEnabled, changed }. */
function create(api) {
  const g = {
    loading: false, status: null, stats: null, config: null, projectId: null, dataDir: null, wiring: null, busy: null, last: null, report: null, canApply: false, project: null,
    // the map of one module: followed from the file being edited, or pinned by pressing a module on the map
    map: { module: null, data: null, loading: false, error: null, follow: true, trail: [], wanted: false, fileModule: null },
  };
  const mapCache = new Map();   // "generation:module" -> the map, so going back and forth costs nothing
  let autoRan = false;

  async function refresh() {
    const project = api.findProject();
    if (!project || g.loading) return;
    g.loading = true; g.project = project.name; api.changed();
    try {
      const [st, doc] = await Promise.all([
        api.runCli(project, ["kg", "status", "--path", project.root], 90000),
        api.runCli(project, ["kg", "doctor", "--path", project.root], 90000),
      ]);
      g.status = parseStatus(st.stdout + "\n" + st.stderr);
      const d = parseDoctor(doc.stdout);
      Object.assign(g, { config: d.config, projectId: d.projectId, dataDir: d.dataDir });
      if (g.status.state === "fresh" || g.status.state === "stale") {
        const stats = await api.runCli(project, ["kg", "stats", "--path", project.root], 90000);
        g.stats = parseStats(stats.stdout);
      } else g.stats = null;
      g.wiring = mcpWiring(project.root, d.config);
    } catch (error) {
      g.last = { level: "bad", text: `Could not read the graph: ${(error && error.message) || error}` };
    } finally {
      g.loading = false; api.changed();
    }
  }

  async function update() {
    const project = api.findProject();
    if (!project || g.busy) return;
    g.busy = { what: "Updating the graph", since: Date.now() }; g.last = null; g.report = null; api.changed();
    const res = await api.runCli(project, ["kg", "index", "--path", project.root], 15 * 60 * 1000);
    g.busy = null;
    const seconds = (String(res.stdout).match(/^duration\s+(.+)$/m) || [])[1];
    g.last = res.code === 0 ? { level: "ok", text: `Graph updated${seconds ? " in " + seconds : ""}.` } : { level: "bad", text: `The update failed: ${lastLines(res)}` };
    await refresh();
    if (res.code === 0) prune();
    mapCache.clear();   // a new generation: the maps drawn from the old one are history
    if (g.map.module) await loadMap(g.map.module);
  }

  // ------------------------------------------------------------------------------------------ the map of one module
  async function loadMap(name) {
    const project = api.findProject();
    if (!project || !name) return;
    g.map.module = name; g.map.error = null;
    if (!g.dataDir) await refresh();
    if (g.map.module !== name) return;
    if (!g.dataDir) { g.map.loading = false; g.map.data = null; g.map.error = "This project has no graph yet: build it first (Update graph)."; api.changed(); return; }
    const key = `${g.status && g.status.generation}:${name}`;
    if (mapCache.has(key)) { g.map.data = mapCache.get(key); g.map.loading = false; api.changed(); return; }
    g.map.loading = true; api.changed();
    const script = path.join(path.dirname(api.cliPath(project)), "kg_map.py");
    const run = await new Promise((resolve) => {
      cp.execFile(api.pythonExe(project), [script, "--data-dir", g.dataDir, "--module", name, "--odoo-root", api.odooRoot(project)],
        { timeout: 60000, windowsHide: true, maxBuffer: 32 * 1024 * 1024 }, (err, stdout) => resolve({ err, stdout: String(stdout || "") }));
    });
    if (g.map.module !== name) return;   // the person moved on while it ran: that answer is not wanted
    let data = null;
    try { data = JSON.parse(run.stdout); } catch (e) { data = null; }
    g.map.loading = false;
    if (!data) { g.map.data = null; g.map.error = `The map could not be read (${run.err ? run.err.message : "no answer"}).`; }
    else if (data.error) { g.map.data = null; g.map.error = data.error; }
    else {
      g.map.data = data;
      mapCache.set(key, data);
      if (mapCache.size > 60) mapCache.delete(mapCache.keys().next().value);
    }
    api.changed();
  }

  /** The editor moved to this file: draw the map of its module (when the person follows the editor and the page is open). */
  function followFile(file) {
    const m = moduleOfFile(file);
    g.map.fileModule = m ? m.name : null;
    if (!m || !g.map.follow || !g.map.wanted) return Promise.resolve();
    if (m.name === g.map.module && (g.map.data || g.map.loading)) return Promise.resolve();
    g.map.trail = [];
    return loadMap(m.name);
  }

  /** The Graph page is shown or hidden: nothing is computed for a page nobody looks at. */
  function wanted(on) {
    g.map.wanted = !!on;
    if (!on) return Promise.resolve();
    if (!g.map.fileModule && api.activeFile) { const m = moduleOfFile(api.activeFile()); g.map.fileModule = m ? m.name : null; }
    if (g.map.follow && g.map.fileModule && g.map.fileModule !== g.map.module) { g.map.trail = []; return loadMap(g.map.fileModule); }
    return Promise.resolve();
  }

  /** A module on the map was pressed: its own whole tree, and a way back. */
  function pin(name) {
    if (!name || name === g.map.module) return Promise.resolve();
    if (g.map.module) g.map.trail.push(g.map.module);
    g.map.follow = false;
    return loadMap(name);
  }

  function back() {
    const previous = g.map.trail.pop();
    if (!previous) return Promise.resolve();
    return loadMap(previous);
  }

  function follow(on) {
    g.map.follow = !!on;
    if (!on) { api.changed(); return Promise.resolve(); }
    g.map.trail = [];
    api.changed();
    return g.map.fileModule && g.map.fileModule !== g.map.module ? loadMap(g.map.fileModule) : Promise.resolve();
  }

  function openModule(name) {
    const node = g.map.data && g.map.data.nodes.find((n) => n.name === name);
    if (!node || !node.folder) return Promise.resolve();
    return Promise.resolve(api.openFile(path.join(path.normalize(node.folder), "__manifest__.py")));
  }

  async function doctor() {
    const project = api.findProject();
    if (!project) return;
    const res = await api.runCli(project, ["kg", "doctor", "--path", project.root], 90000);
    g.report = { title: "Graph doctor (writes nothing)", text: (res.stdout || res.stderr).trim().slice(0, 4000), level: res.code === 0 ? "ok" : "bad" };
    g.canApply = false; api.changed();
  }

  /** `odoo-ai workspace` for this project, as a dry run first: the page shows what it would change, then Apply. */
  async function wiring(apply) {
    const project = api.findProject();
    if (!project || g.busy) return;
    const args = ["workspace", "--path", project.root, ...(apply ? [] : ["--dry-run"])];
    const res = await api.runCli(project, args, 5 * 60 * 1000);
    const text = (res.stdout + "\n" + res.stderr).trim().slice(0, 4000);
    g.report = { title: apply ? "Wiring applied" : "What fixing the wiring would change (nothing is written yet)", text, level: res.code === 0 ? "ok" : "bad" };
    g.canApply = !apply && res.code === 0;
    api.changed();
    if (apply) await refresh();
  }

  /** Once per window, quietly: leave the graph fresh when the project opens, as the workspace task used to. */
  async function autoUpdate() {
    if (autoRan || !api.autoUpdateEnabled()) return;
    autoRan = true;
    await refresh();
    const project = api.findProject();
    if (!project || !g.config || !g.status || !(g.status.state === "stale" || g.status.state === "missing")) return;
    const toolsDir = path.dirname(api.cliPath(project));
    const opener = path.join(toolsDir, "kg_open.py");
    if (!fs.existsSync(opener)) return;
    g.busy = { what: "Updating the graph (the project was opened)", since: Date.now() }; api.changed();
    await new Promise((resolve) => {
      cp.execFile(api.pythonExe(project), [opener, "--config", g.config], { cwd: path.join(toolsDir, "kg"), timeout: 15 * 60 * 1000, windowsHide: true, maxBuffer: 8 * 1024 * 1024 },
        () => resolve());   // quiet on purpose: the next refresh shows the truth
    });
    g.busy = null;
    await refresh();
    prune();
  }

  /** Only when the person asked for it (the setting odooCi.graph.keepGenerations, off by default): this removes data. */
  function prune() {
    const keep = api.keepGenerations ? api.keepGenerations() : 0;
    if (keep > 0 && g.dataDir) pruneGenerations(g.dataDir, keep);
  }

  return { state: () => g, refresh, update, doctor, wiring, autoUpdate, followFile, wanted, pin, back, follow, openModule };
}

module.exports = { create, parseStatus, parseStats, parseDoctor, parseFields, mcpWiring, moduleOfFile, pruneGenerations };
