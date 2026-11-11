// The extension in REAL editors: each one is started with this extension under development and a runner inside it
// (test/vscode_host/runner.js). A separate profile per run, so a window you are using is untouched; a window does
// open for a minute. node test/vscode_host.test.js
//
// Editors tried, when found: a portable VS Code kept in <LOCALAPPDATA>/odoo-ci/vscode-test (the installed VS Code
// refuses a second instance while one of its updates is pending), the installed VS Code, and Antigravity.
// CODE_EXE=<path> tries only that one. Skipped, and says so, when none is found.
"use strict";
const assert = require("assert");
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

function editors() {
  if (process.env.CODE_EXE) return fs.existsSync(process.env.CODE_EXE) ? [{ name: "CODE_EXE", exe: process.env.CODE_EXE }] : [];
  const local = process.env.LOCALAPPDATA || "";
  const list = process.platform === "win32"
    ? [["VS Code (portable)", path.join(local, "odoo-ci", "vscode-test", "Code.exe")],
      ["VS Code", path.join(local, "Programs", "Microsoft VS Code", "Code.exe")],
      ["Antigravity", path.join(local, "Programs", "Antigravity IDE", "Antigravity IDE.exe")]]
    : process.platform === "darwin"
      ? [["VS Code", "/Applications/Visual Studio Code.app/Contents/MacOS/Electron"]]
      : [["VS Code", "/usr/share/code/code"], ["VS Code", "/usr/bin/code"]];
  return list.filter(([, exe]) => fs.existsSync(exe)).map(([name, exe]) => ({ name, exe }));
}

const project = process.env.ODOO_CI_TEST_PROJECT || path.join(process.env.ODOO_ROOT || "C:\\odoo", "ProjectsOdoo19", "Acme");
const found = editors();
if (!found.length || !fs.existsSync(project)) {
  console.log(`vscode host SKIPPED: ${!found.length ? "no editor found (set CODE_EXE)" : `no project at ${project} (set ODOO_CI_TEST_PROJECT)`}`);
  process.exit(0);
}
const extensionDir = path.join(__dirname, "..");
const python = process.env.ODOO_CI_TEST_PYTHON || path.join(path.dirname(path.dirname(extensionDir)), "venv", "Scripts", "python.exe");
const cli = path.join(path.dirname(extensionDir), "cli.py");
// the parent may itself run inside an editor: its variables would send the new window to THAT editor, or make it a plain node
const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([k]) => !/^(VSCODE_|ELECTRON_)/.test(k)));

function runIn(editor) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "odoo-ci-editor-"));
  const out = path.join(tmp, "result.json");
  const env = Object.assign({}, cleanEnv, { ODOO_CI_TEST_OUT: out, ODOO_CI_TEST_PROJECT: project, ODOO_CI_TEST_CLI: cli,
    ODOO_CI_TEST_PYTHON: fs.existsSync(python) ? python : "python" });
  const args = [project, `--extensionDevelopmentPath=${extensionDir}`, `--extensionTestsPath=${path.join(__dirname, "vscode_host", "runner.js")}`,
    `--user-data-dir=${path.join(tmp, "user")}`, `--extensions-dir=${path.join(tmp, "extensions")}`, "--disable-workspace-trust", "--skip-welcome",
    "--skip-release-notes", "--disable-updates", "--disable-telemetry", "--new-window"];
  const run = cp.spawnSync(editor.exe, args, { env, timeout: 300000, encoding: "utf8", stdio: ["ignore", "ignore", "pipe"] });
  let results = null;
  try { results = JSON.parse(fs.readFileSync(out, "utf8")); } catch (e) { /* reported by the caller */ }
  const stderr = String(run.stderr || "");
  fs.rmSync(tmp, { recursive: true, force: true });
  return { results, status: run.status, refused: /currently being updated/.test(stderr) };
}

const report = [];
let ran = 0;
for (const editor of found) {
  const { results, status, refused } = runIn(editor);
  if (!results && refused) {
    report.push(`${editor.name}: not run (an update of it is pending, so it will not start a second window)`);
    continue;
  }
  assert.ok(results, `${editor.name} produced no result (exit ${status}): the runner did not finish`);
  const failed = results.filter((r) => !r.ok);
  assert.deepStrictEqual(failed.map((r) => `${r.name}: ${r.detail}`), [], `${editor.name}: ${failed.length} check(s) failed in the real editor`);
  ran += 1;
  report.push(`${editor.name}: ${results.length} checks passed`);
}
assert.ok(ran > 0, `no editor could be started: ${report.join("; ")}`);
console.log(`vscode host OK - ${report.join(" | ")}`);
process.exit(0);
