"""odoo-ai ci: run the project's CI on this machine before pushing.

Two parts, in the order GitHub runs them:

1. STATIC - every `run:` step of the `checks` job in .github/workflows/ci.yml,
   executed as written (bash, from the repository root, the venv's python
   first on PATH). The steps are read from the file each time, so a gate
   added to ci.yml is run here without touching this tool. The base ref the
   steps compare against is what a push would be compared against:
   origin/<current branch>.

2. TESTS - what the `tests` job does: a fresh database, every module the
   project ships installed without demo data, every test of ours run, each
   result line required to read "0 failed, 0 error(s)", then the project's
   own log gate (.github/scripts/check_test_log.py) when it has one.

The test part needs the database to itself: an Odoo server running against
the same PostgreSQL runs its crons on every database, and two writers on the
new one fail the run with "could not serialize access". When one is running
the tool stops and says so; --stop-servers stops them first.

With ODOO_AI_CI_EVENTS=1 every step is also reported as one line
"::ci::<json>" - what the VS Code extension (vscode-extension/) reads to draw
its view. The human-readable lines stay as they are.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from workspace_sync import WIN_ROOT, fs, read_conf, resolve_project


def _say(text=""):
    print(text, flush=True)


EVENTS = os.environ.get("ODOO_AI_CI_EVENTS") == "1"


def _event(**data):
    """One machine-readable line for the VS Code extension."""
    if EVENTS:
        print("::ci::" + json.dumps(data, ensure_ascii=False), flush=True)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout.strip()


def _odoo_processes():
    """PIDs of running odoo-bin processes (Windows)."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Where-Object { $_.CommandLine -like '*odoo-bin*' } | "
          "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        if "\t" in line:
            pid, cmd = line.split("\t", 1)
            rows.append((int(pid), cmd.strip()))
    return rows


def _git_bash():
    """Git for Windows' bash - never the WSL launcher.

    Outside Git Bash, `bash` on PATH is usually C:\\Windows\\System32\\bash.exe
    or ...\\WindowsApps\\bash.exe: the WSL launcher, which runs the steps in
    Linux (or fails when no distribution is installed). VS Code starts this
    tool from such an environment.
    """
    candidates = []
    git = shutil.which("git")
    if git:
        root = os.path.dirname(os.path.dirname(os.path.realpath(git)))  # ...\\Git\\cmd\\git.exe -> ...\\Git
        candidates += [os.path.join(root, "bin", "bash.exe"), os.path.join(root, "usr", "bin", "bash.exe")]
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            candidates.append(os.path.join(base, "Git", "bin", "bash.exe"))
    found = shutil.which("bash")
    if found and not any(x in found.lower() for x in ("windowsapps", "system32")):
        candidates.append(found)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _static_steps(ci_yml, version):
    """The `run:` steps of the `checks` job, read with the project's venv.

    PyYAML is in every Odoo venv but not necessarily in the python that runs
    this tool (VS Code starts it with whatever `python` its PATH finds).
    """
    py = fs(f"{WIN_ROOT}/odoo{version}/venv/Scripts/python.exe")
    code = ("import json, sys, yaml\n"
            "d = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))\n"
            "job = (d.get('jobs') or {}).get('checks') or {}\n"
            "print(json.dumps([[st.get('name') or 'step', st['run']] for st in job.get('steps', []) if 'run' in st]))")
    res = subprocess.run([py if os.path.isfile(py) else sys.executable, "-c", code, ci_yml],
                         capture_output=True, text=True, encoding="utf-8")
    if res.returncode:
        raise SystemExit("could not read ci.yml:\n" + res.stderr.strip())
    return [tuple(step) for step in json.loads(res.stdout)]


def run_static(repo, version, base):
    ci_yml = os.path.join(repo, ".github", "workflows", "ci.yml")
    if not os.path.isfile(ci_yml):
        _say("  (no .github/workflows/ci.yml - nothing to run)")
        return True
    bash = _git_bash()
    if not bash:
        _say("  [ERROR] Git Bash not found (Git for Windows provides it; WSL's bash is not used)")
        _event(type="step", phase="static", name="Git Bash is needed to run the ci.yml steps", status="fail",
               details=["Install Git for Windows: https://git-scm.com/download/win"])
        return False
    env = dict(os.environ)
    venv_scripts = fs(f"{WIN_ROOT}/odoo{version}/venv/Scripts")
    env["PATH"] = venv_scripts + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    ok = True
    for name, script in _static_steps(ci_yml, version):
        if "steps.base.outputs.ref" not in script and "${{" in script:
            if "github.event_name" in script or "GITHUB_OUTPUT" in script:
                continue  # the step that works out the base ref: done here instead
            _say(f"  [SKIP] {name} (uses GitHub-only values)")
            _event(type="step", phase="static", name=name, status="skip")
            continue
        script = script.replace("${{ steps.base.outputs.ref }}", base)
        _event(type="step", phase="static", name=name, status="running")
        started = time.time()
        res = subprocess.run([bash, "-e", "-o", "pipefail", "-c", script], cwd=repo, env=env,
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        took = time.time() - started
        if res.returncode == 0:
            _say(f"  [OK]   {name}  ({took:.0f}s)")
            _event(type="step", phase="static", name=name, status="ok", seconds=round(took))
        else:
            ok = False
            _say(f"  [FAIL] {name}")
            tail = [l for l in (res.stdout + res.stderr).strip().splitlines()
                    if l.strip() and not l.startswith("[notice]")][-25:]
            for line in tail:
                _say("         " + line)
            _event(type="step", phase="static", name=name, status="fail", seconds=round(took), details=tail)
    return ok


def _modules_and_tags(repo, version):
    py = fs(f"{WIN_ROOT}/odoo{version}/venv/Scripts/python.exe")
    script = os.path.join(repo, ".github", "scripts", "acme_modules.py")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if os.path.isfile(script):
        mods = subprocess.run([py, script], cwd=repo, capture_output=True, text=True, env=env)
        tags = subprocess.run([py, script, "--tags"], cwd=repo, capture_output=True, text=True, env=env)
        if mods.returncode or tags.returncode:
            raise SystemExit("acme_modules.py refused the module register:\n" + mods.stdout + mods.stderr)
        return mods.stdout.strip(), tags.stdout.strip()
    names = sorted(d for d in os.listdir(repo)
                   if os.path.isfile(os.path.join(repo, d, "__manifest__.py")))
    return ",".join(names), ",".join("/" + n for n in names)


def run_tests(repo, version, name, conf_file, keep_db):
    py = fs(f"{WIN_ROOT}/odoo{version}/venv/Scripts/python.exe")
    odoo_bin = fs(f"{WIN_ROOT}/odoo{version}/odoo/odoo-bin")
    db = re.sub(r"[^a-z0-9_]", "_", f"{name.lower()}_ci_local")
    modules, tags = _modules_and_tags(repo, version)
    log_dir = os.path.join(tempfile.gettempdir(), "odoo-ai-ci", f"{version}-{name}")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "odoo-test.log")
    _say(f"  database   {db} (dropped and created fresh)")
    _say(f"  modules    {len(modules.split(','))}")
    _say(f"  log        {log_path}")
    _event(type="log", path=log_path)
    _event(type="step", phase="tests", key="tests", status="running",
           name=f"Install {len(modules.split(','))} modules and run their tests")
    subprocess.run([py, odoo_bin, "db", "-c", conf_file, "drop", db], capture_output=True)
    started = time.time()
    with open(log_path, "w", encoding="utf-8") as log:
        subprocess.run([py, odoo_bin, "-c", conf_file, "-d", db, f"--db-filter=^{db}$",
                        "--without-demo=all", "-i", modules, "--test-enable", "--test-tags", tags,
                        "--stop-after-init", "--http-port=1994", "--log-level=warn",
                        "--log-handler=odoo.tests:INFO"],
                       stdout=log, stderr=subprocess.STDOUT, cwd=os.path.dirname(odoo_bin))
    _say(f"  took       {(time.time() - started) / 60:.0f} min")
    text = open(log_path, encoding="utf-8", errors="replace").read()
    results = [l for l in text.splitlines() if "odoo.tests.result" in l]
    ok = True
    if not results:
        _say("  [FAIL] no test result line: the run did not reach the tests - see the log")
        ok = False
    summaries = []
    for line in results:
        clean = "0 failed, 0 error(s)" in line
        ok &= clean
        summary = line.split("odoo.tests.result: ", 1)[-1].strip()
        summaries.append(summary)
        _say(f"  [{'OK' if clean else 'FAIL'}]   {summary}")
    failed_tests = []
    if not ok:
        for line in text.splitlines():
            if re.search(r"(FAIL|ERROR): Test", line) or re.search(r": (FAIL|ERROR): ", line):
                failed_tests.append(line.split(" odoo.", 1)[-1][:180])
                _say("         " + failed_tests[-1])
    _event(type="step", phase="tests", key="tests", status="ok" if ok else "fail",
           name=(summaries[-1] if summaries else "The run did not reach the tests"),
           seconds=round(time.time() - started), details=failed_tests[:40])
    gate = os.path.join(repo, ".github", "scripts", "check_test_log.py")
    if os.path.isfile(gate):
        res = subprocess.run([py, gate, log_path], cwd=repo, capture_output=True, text=True,
                             env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        gate_ok = res.returncode == 0
        ok &= gate_ok
        _say(f"  [{'OK' if gate_ok else 'FAIL'}]   framework warnings in the log")
        found = res.stdout.strip().splitlines()[-20:] if not gate_ok else []
        for line in found:
            _say("         " + line)
        _event(type="step", phase="tests", key="logwarnings", status="ok" if gate_ok else "fail",
               name="No framework warnings from our modules in the log", details=found)
    if not keep_db:
        subprocess.run([py, odoo_bin, "db", "-c", conf_file, "drop", db], capture_output=True)
    return ok


def run_ci(path=".", static_only=False, stop_servers=False, keep_db=False):
    project = resolve_project(os.path.abspath(path))
    if not project:
        raise SystemExit(f"not inside a project (expected ...\\ProjectsOdoo<version>\\<Project>): {path}")
    version, name = project[0], project[1]
    repo = fs(f"{WIN_ROOT}/ProjectsOdoo{version}/{name}")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo, "fetch", "--quiet", "origin")
    base = f"origin/{branch}" if _git(repo, "rev-parse", "--verify", "--quiet", f"origin/{branch}") else "HEAD"
    dirty = len([l for l in _git(repo, "status", "--porcelain").splitlines() if l.strip()])
    _say(f"[CI] Odoo {version} / {name}  branch {branch}, compared with {base}"
         + (f", {dirty} uncommitted file(s) included" if dirty else ""))

    _event(type="start", project=name, version=version, branch=branch, base=base, dirty=dirty,
           mode="static" if static_only else "full")
    _say("\n== Static checks (the `checks` job of ci.yml)")
    _event(type="phase", phase="static", name="Static checks (ci.yml)")
    static_ok = run_static(repo, version, base)
    if static_only:
        _say("\n[CI] " + ("PASSED" if static_ok else "FAILED") + " (static checks only)")
        _event(type="result", passed=static_ok, mode="static")
        return 0 if static_ok else 1

    _say("\n== Install and test (the `tests` job)")
    _event(type="phase", phase="tests", name="Install and test")
    running = _odoo_processes()
    if running:
        if not stop_servers:
            _say("  [STOP] an Odoo server is running; its crons would write to the test database:")
            for pid, cmd in running:
                _say(f"         pid {pid}: {cmd[:110]}")
            _say("         Stop it, or run again with --stop-servers.")
            _event(type="blocked", reason="server-running", pids=[pid for pid, _ in running])
            _event(type="result", passed=False, mode="full", blocked=True)
            return 1
        for pid, _cmd in running:
            subprocess.run(["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"],
                           capture_output=True)
        _say(f"  stopped    {len(running)} Odoo process(es)")
    conf = read_conf(repo, name)
    if not conf:
        raise SystemExit(f"no .conf in {repo}")
    tests_ok = run_tests(repo, version, name, os.path.join(repo, conf["file"]), keep_db)

    passed = static_ok and tests_ok
    _say("\n[CI] " + ("PASSED - safe to push" if passed else "FAILED - fix the above before pushing"))
    _event(type="result", passed=passed, mode="full")
    return 0 if passed else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", "-p", default=".")
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--stop-servers", action="store_true")
    parser.add_argument("--keep-db", action="store_true")
    a = parser.parse_args()
    sys.exit(run_ci(a.path, a.static_only, a.stop_servers, a.keep_db))
