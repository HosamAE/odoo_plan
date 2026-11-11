// A picture of the extension inside a real editor window, in the editor's own themes. Not a test.
//   node test/vscode_host/screenshot.js <folder> [dark|light ...]        (Windows; captures ONLY the window it started)
"use strict";
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

if (process.platform !== "win32") { console.log("screenshot: Windows only"); process.exit(0); }
const folder = process.argv[2];
const themes = process.argv.slice(3).length ? process.argv.slice(3) : ["dark", "light"];
if (!folder) { console.error("usage: screenshot.js <folder> [dark|light ...]"); process.exit(2); }
fs.mkdirSync(folder, { recursive: true });

const local = process.env.LOCALAPPDATA || "";
const exe = process.env.CODE_EXE || [path.join(local, "odoo-ci", "vscode-test", "Code.exe"), path.join(local, "Programs", "Antigravity IDE", "Antigravity IDE.exe")].find((p) => fs.existsSync(p));
if (!exe) { console.error("no editor found (set CODE_EXE)"); process.exit(2); }
const extensionDir = path.join(__dirname, "..", "..");
const project = process.env.ODOO_CI_TEST_PROJECT || "C:\\odoo\\ProjectsOdoo19\\Acme";
const python = process.env.ODOO_CI_TEST_PYTHON || path.join(path.dirname(path.dirname(extensionDir)), "venv", "Scripts", "python.exe");
const cli = path.join(path.dirname(extensionDir), "cli.py");
const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([k]) => !/^(VSCODE_|ELECTRON_)/.test(k)));

// PrintWindow of the one window that belongs to a process: it works for a window that is covered, not for a minimised one
const CAPTURE = `
param([int]$ProcessId, [string]$Out)
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System; using System.Runtime.InteropServices; using System.Collections.Generic; using System.Text;
public class Win {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc p, IntPtr l);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint f);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  public struct RECT { public int L, T, R, B; }
  public static List<IntPtr> Of(uint pid) {
    var found = new List<IntPtr>();
    EnumWindows((h, x) => { uint p; GetWindowThreadProcessId(h, out p); if (p == pid && IsWindowVisible(h)) { var s = new StringBuilder(300); GetWindowText(h, s, 300); if (s.Length > 0) found.Add(h); } return true; }, IntPtr.Zero);
    return found;
  }
}
"@
$handles = [Win]::Of([uint32]$ProcessId)
if ($handles.Count -eq 0) { Write-Output "NOWINDOW"; exit 3 }
$h = $handles[0]
[Win]::ShowWindow($h, 3) | Out-Null
Start-Sleep -Milliseconds 400
$r = New-Object Win+RECT
[Win]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.R - $r.L; $ht = $r.B - $r.T
$bmp = New-Object System.Drawing.Bitmap $w, $ht
$g = [System.Drawing.Graphics]::FromImage($bmp)
$dc = $g.GetHdc()
[Win]::PrintWindow($h, $dc, 2) | Out-Null
$g.ReleaseHdc($dc)
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
Write-Output "OK $w x $ht"
`;
const capture = path.join(os.tmpdir(), "odoo-ci-capture.ps1");
fs.writeFileSync(capture, CAPTURE);

async function one(theme) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "odoo-ci-shot-"));
  const user = path.join(tmp, "user", "User");
  fs.mkdirSync(user, { recursive: true });
  fs.writeFileSync(path.join(user, "settings.json"), JSON.stringify({
    "workbench.colorTheme": theme === "light" ? "Default Light Modern" : "Default Dark Modern", "window.zoomLevel": -2,
    "workbench.startupEditor": "none", "window.restoreWindows": "none", "workbench.sideBar.location": "left", "update.mode": "none",
    "telemetry.telemetryLevel": "off", "window.titleBarStyle": "native" }));
  const ready = path.join(tmp, "ready.flag");
  const env = Object.assign({}, cleanEnv, { ODOO_CI_TEST_OUT: path.join(tmp, "result.json"), ODOO_CI_TEST_PROJECT: project, ODOO_CI_TEST_CLI: cli,
    ODOO_CI_TEST_PYTHON: fs.existsSync(python) ? python : "python", ODOO_CI_TEST_READY: ready, ODOO_CI_TEST_HOLD_MS: "30000" });
  const args = [project, `--extensionDevelopmentPath=${extensionDir}`, `--extensionTestsPath=${path.join(__dirname, "runner.js")}`,
    `--user-data-dir=${path.join(tmp, "user")}`, `--extensions-dir=${path.join(tmp, "extensions")}`, "--disable-workspace-trust", "--skip-welcome",
    "--skip-release-notes", "--disable-updates", "--disable-telemetry", "--new-window"];
  const child = cp.spawn(exe, args, { env, stdio: "ignore", windowsHide: false });
  const end = Date.now() + (process.env.ODOO_CI_TEST_REAL_SERVER ? 900000 : 180000);
  let message = "the editor never reached the cockpit";
  // pictures of real work: one per page, each taken when the window says that page is drawn
  const pages = process.env.ODOO_CI_TEST_REAL_SERVER ? String(process.env.ODOO_CI_TEST_TABS || "home").split(",") : [];
  const taken = [];
  for (const tab of pages) {
    while (!fs.existsSync(`${ready}.${tab}`) && Date.now() < end) await new Promise((r) => setTimeout(r, 500));
    if (!fs.existsSync(`${ready}.${tab}`)) break;
    const target = path.join(folder, `real_${theme}_${tab}.png`);
    const shot = cp.spawnSync("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", capture, "-ProcessId", String(child.pid), "-Out", target], { encoding: "utf8" });
    taken.push(`${tab}: ${String(shot.stdout).trim()}`);
    fs.writeFileSync(`${ready}.${tab}.done`, "1");
  }
  while (!fs.existsSync(ready) && Date.now() < end) await new Promise((r) => setTimeout(r, 500));
  if (taken.length) message = `${theme}: ${taken.join("; ")}`;
  else if (fs.existsSync(ready)) {
    await new Promise((r) => setTimeout(r, 2500));   // let the page settle after its last state
    const target = path.join(folder, `vscode_${theme}.png`);
    const shot = cp.spawnSync("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", capture, "-ProcessId", String(child.pid), "-Out", target], { encoding: "utf8" });
    message = `${theme}: ${String(shot.stdout).trim()} -> ${target}`;
  }
  try { child.kill(); } catch (e) { /* already gone */ }
  await new Promise((r) => setTimeout(r, 1500));
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) { /* the editor may still hold a file */ }
  return message;
}

(async () => {
  for (const theme of themes) console.log(await one(theme));
  process.exit(0);
})();
