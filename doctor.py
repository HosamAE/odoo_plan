"""odoo-ai doctor: is this Odoo version's setup whole?

Checks the layout agreed on 2026-11-08 for the version these tools belong to:

  C:/odoo/odoo<NN>/            Odoo source, venv, odoo.conf (not in git)
  C:/odoo/odoo<NN>/tools/      these tools, branch <NN>.0 of the tools repository
  C:/odoo/bin/                 the odoo-ai launcher (on PATH), shared by every version
  C:/odoo/ProjectsOdoo<NN>/<project>/  one workspace inside, its graph config, .mcp.json, odools.toml

`--fix` repairs what is safe to repair (launcher, PATH, pylint .pth); it never
deletes anything, it only names what is left over.
"""
import json
import os
import shutil
import subprocess

from workspace_sync import (FS_ROOT, WIN_ROOT, KG_DIR, TOOLS_DIR, TOOLS_VERSION, count_manifests, fs, iter_projects,
                            loads_jsonc, project_dir, project_id, projects_root, PROJECT_ID_OVERRIDES)

BIN = os.path.join(FS_ROOT, "bin")
LAUNCHER = os.path.join(TOOLS_DIR, "launcher")
LEGACY = ("odoo_ai_cli/cli.py", "odoo_ai_cli\\\\cli.py")


class Report:
    def __init__(self):
        self.problems = 0

    def ok(self, text):
        print(f"  [OK]   {text}")

    def bad(self, text, hint=""):
        self.problems += 1
        print(f"  [FIX]  {text}" + (f"\n         -> {hint}" if hint else ""))

    def check(self, flag, good, bad, hint=""):
        if flag:
            self.ok(good)
        else:
            self.bad(bad, hint)
        return flag


def _text(path):
    """A file's content with line endings ignored (git may check them out as CRLF), or None."""
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as handle:
        return handle.read().replace(b"\r\n", b"\n")


def _user_path():
    res = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "[Environment]::GetEnvironmentVariable('Path','User')"],
                         capture_output=True, text=True)
    return [p for p in res.stdout.strip().split(";") if p]


def _set_user_path(parts):
    value = ";".join(parts).replace("'", "''")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"[Environment]::SetEnvironmentVariable('Path','{value}','User')"], check=True)


def check_tools(r, v, fix):
    print(f"\n== Odoo {v}: {WIN_ROOT}/odoo{v}")
    r.check(os.path.isfile(fs(f"{WIN_ROOT}/odoo{v}/venv/Scripts/python.exe")), "venv",
            f"no venv at {WIN_ROOT}/odoo{v}/venv", "create it (see docs/LAYOUT.md)")
    r.check(os.path.isfile(fs(f"{WIN_ROOT}/odoo{v}/odoo/odoo-bin")), "Odoo source", "no Odoo source")
    branch = subprocess.run(["git", "-C", TOOLS_DIR, "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    r.check(branch == f"{v}.0", f"tools on branch {branch}", f"tools on branch '{branch}', expected {v}.0")
    rc = os.path.join(TOOLS_DIR, ".pylintrc")
    r.check(os.path.isfile(rc), "tools/.pylintrc", "tools/.pylintrc missing")
    plugins = os.path.join(TOOLS_DIR, "pylint_plugins")
    if os.path.isdir(plugins):
        want = f"{WIN_ROOT}/odoo{v}/tools/pylint_plugins"
        pth = fs(f"{WIN_ROOT}/odoo{v}/venv/Lib/site-packages/acme_pylint_plugins.pth")
        have = open(pth, encoding="utf-8").read().strip() if os.path.isfile(pth) else ""
        if have != want and fix and os.path.isdir(os.path.dirname(pth)):
            with open(pth, "w", encoding="utf-8") as handle:
                handle.write(want + "\n")
            have = want
        r.check(have == want, "venv finds tools/pylint_plugins", f"venv .pth is '{have}'", "odoo-ai doctor --fix")
        r.check(want in open(rc, encoding="utf-8").read(), ".pylintrc init-hook -> tools/pylint_plugins",
                ".pylintrc init-hook does not name tools/pylint_plugins")
    for left in (f"{WIN_ROOT}/odoo{v}/vscode_extension", f"{WIN_ROOT}/odoo{v}/pylint_plugins",
                 f"{WIN_ROOT}/odoo{v}/.pylintrc", f"{WIN_ROOT}/odoo{v}/.flake8"):
        if os.path.exists(fs(left)):
            r.bad(f"left over: {left}", "now in tools/; remove it")
    r.check(os.path.isfile(os.path.join(TOOLS_DIR, "kg", "odoo_kg", "__main__.py")), "graph engine in tools/kg",
            "no graph engine in tools/kg", "it ships with the tools: git pull in tools/")
    r.check(not os.path.isdir(fs(f"{WIN_ROOT}/odoo-kg")), f"no stand-alone {WIN_ROOT}/odoo-kg",
            f"left over: {WIN_ROOT}/odoo-kg", "the engine is in every version's tools/kg now; move it aside")
    ext_proj = os.path.isdir(fs(f"{WIN_ROOT}/ProjectsOdoo{v}"))
    int_proj = os.path.isdir(fs(f"{WIN_ROOT}/odoo{v}/projects"))
    r.check(ext_proj or int_proj, f"projects root active ({'ProjectsOdoo' + v if ext_proj else 'odoo' + v + '/projects'})",
            f"no projects directory found for version {v}", f"create {WIN_ROOT}/ProjectsOdoo{v}")

    # Quick self-test of the tools. The rehearsal (tests/test_rehearsal.py) runs `setup`, which runs this doctor:
    # it is skipped here - `odoo-ai test-self` runs it - or the two would call each other for ever.
    import unittest
    tests_dir = os.path.join(TOOLS_DIR, "tests")
    if os.path.isdir(tests_dir):
        os.environ["ODOO_AI_SKIP_REHEARSAL"] = "1"
        suite = unittest.defaultTestLoader.discover(tests_dir, pattern="test_*.py")
        with open(os.devnull, "w", encoding="utf-8") as sink:
            res = unittest.TextTestRunner(stream=sink).run(suite)
        r.check(res.wasSuccessful(), f"tools quick self-tests passed ({res.testsRun} tests)", "tools self-test failed")
    
    # Network & PostgreSQL health check
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(('127.0.0.1', 5432))
        s.close()
        r.ok("PostgreSQL service active on port 5432")
    except Exception:
        r.bad("PostgreSQL service not reachable on port 5432", "Start PostgreSQL service in Windows Services")

    s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s2.settimeout(0.3)
    try:
        s2.connect(('127.0.0.1', 8069))
        s2.close()
        r.ok("Port 8069 is in use (Odoo server running)")
    except Exception:
        r.ok("Port 8069 is free (ready for Odoo server)")


def check_launcher(r, fix):
    print(f"\n== Shared: {FS_ROOT}/bin")
    names = [n for n in os.listdir(LAUNCHER) if not n.endswith(".md")]
    stale = [n for n in names if _text(os.path.join(LAUNCHER, n)) != _text(os.path.join(BIN, n))]
    if stale and fix:
        os.makedirs(BIN, exist_ok=True)
        for n in stale:
            shutil.copy2(os.path.join(LAUNCHER, n), os.path.join(BIN, n))
        stale = []
    r.check(not stale, "launcher up to date", f"launcher missing or older: {', '.join(stale)}", "odoo-ai doctor --fix")
    path = _user_path()
    want = [p for p in path if os.path.normcase(p.rstrip("\\/")) != os.path.normcase(fs(f"{WIN_ROOT}/odoo_ai_cli"))]
    if not any(os.path.normcase(p.rstrip("\\/")) == os.path.normcase(BIN) for p in want):
        want.append(BIN)
    if want != path and fix:
        _set_user_path(want)
        path = want
        print("         PATH changed: open a new terminal (or restart VS Code) to see it")
    r.check(path == want, f"PATH has {BIN}", f"PATH lacks {BIN} or still has legacy CLI",
            "odoo-ai doctor --fix")
    r.check(not os.path.exists(fs(f"{WIN_ROOT}/odoo_ai_cli")), f"no old {WIN_ROOT}/odoo_ai_cli",
            f"left over: {WIN_ROOT}/odoo_ai_cli", f"the tools are in {WIN_ROOT}/odoo<NN>/tools now; remove it")
    pkg = json.load(open(os.path.join(TOOLS_DIR, "vscode-extension", "package.json"), encoding="utf-8"))
    code = shutil.which("code") or shutil.which("code.cmd")
    if code:
        listed = subprocess.run([code, "--list-extensions", "--show-versions"], capture_output=True, text=True,
                                shell=code.endswith(".cmd")).stdout
        mine = next((line for line in listed.splitlines() if line.lower().startswith("hosamae.odoo-ci@")), "")
        r.check(bool(mine), f"VS Code extension {mine}", "Odoo CI extension not installed",
                "build it with vscode-extension/build_vsix.py, then code --install-extension")
        if mine and mine.split("@")[1] != pkg["version"]:
            print(f"  [INFO] these tools carry odoo-ci {pkg['version']}; VS Code has {mine.split('@')[1]}"
                  " (one copy serves every version: install the newest)")


def check_projects(r, v):
    for version, name in iter_projects((v,)):
        pdir = fs(project_dir(version, name))
        if count_manifests(pdir) == 0:
            continue
        if not os.path.isdir(os.path.join(pdir, ".git")) and not os.path.isfile(
                os.path.join(pdir, f"{name}.code-workspace")):
            continue  # a copy, not a project: `odoo-ai workspace` skips it too
        print(f"\n== Odoo {version} / {name}")
        ws = os.path.join(pdir, f"{name}.code-workspace")
        if r.check(os.path.isfile(ws), f"{name}.code-workspace", "no workspace inside the project",
                   "odoo-ai workspace"):
            text = open(ws, encoding="utf-8").read()
            loads_jsonc(text)
            r.check(not any(old in text for old in LEGACY), "workspace runs tools/cli.py",
                    "workspace still runs C:/odoo/odoo_ai_cli", "odoo-ai workspace")
        outer = fs(f"{projects_root(version)}/{name}.code-workspace")
        r.check(not os.path.isfile(outer), "one workspace (none beside the project)",
                f"second workspace beside the project: {outer}", "remove it; the one inside is the workspace")
        pid = PROJECT_ID_OVERRIDES.get((version, name.lower()), project_id(name, version))
        r.check(os.path.isfile(fs(f"{KG_DIR}/config/local/{pid}.toml")), f"graph config {pid}.toml",
                "no graph config", "odoo-ai workspace")
        r.check(os.path.isfile(os.path.join(pdir, ".mcp.json")), ".mcp.json (graph for Claude Code)",
                "no .mcp.json", "odoo-ai workspace")


def run_doctor(fix=False):
    r = Report()
    print(f"[DOCTOR] Odoo {TOOLS_VERSION} tools: {TOOLS_DIR}" + ("   (--fix)" if fix else ""))
    check_tools(r, TOOLS_VERSION, fix)
    check_launcher(r, fix)
    check_projects(r, TOOLS_VERSION)
    print(f"\n[DOCTOR] {'all good' if not r.problems else str(r.problems) + ' to fix'}")
    return 1 if r.problems else 0
