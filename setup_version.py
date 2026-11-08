"""odoo-ai setup: make this Odoo version ready to work on, on any machine.

Everything a version needs is in setup/version.json of these tools, so a new
machine needs only Git, the right Python, PostgreSQL and wkhtmltopdf, then:

    git clone -b 19.0 https://github.com/HosamAE/odoo_plan C:\\odoo\\odoo19\\tools
    python C:\\odoo\\odoo19\\tools\\cli.py setup

Each step looks first and does only what is missing, so running it again (or on
a machine that is already set up) changes nothing. `--check` only reports.

  1. prerequisites   git, Python <version.json python>, PostgreSQL, wkhtmltopdf, VS Code
  2. sources         odoo (required), design-themes, enterprise (when access is granted)
  3. venv            venv with that Python; Odoo's requirements + pip_extra
  4. third-party     odoo<NN>/third-party -> tools/third-party (the addons these tools carry)
  5. odoo.conf       written once, with generated passwords (never printed, never committed)
  6. database role   the PostgreSQL role of odoo.conf (asks for the postgres password)
  7. launcher        C:/odoo/bin on PATH, pylint plugin path (doctor --fix)
  8. VS Code         the Odoo CI extension, built from these tools
  9. rules           AGENTS.md and the version notes next to the version, when missing
"""
import json
import os
import secrets
import shutil
import subprocess

from workspace_sync import FS_ROOT, TOOLS_DIR, TOOLS_VERSION, fs

SPEC = os.path.join(TOOLS_DIR, "setup", "version.json")


class Run:
    def __init__(self, check):
        self.check = check
        self.todo = 0

    def ok(self, text):
        print(f"  [OK]    {text}")

    def do(self, text):
        print(f"  [{'TODO' if self.check else 'DO'}]  {text}")
        self.todo += 1
        return not self.check

    def warn(self, text):
        print(f"  [WARN]  {text}")


def _find_python(want):
    """python.exe of the wanted major.minor, through the py launcher."""
    try:
        out = subprocess.run(["py", f"-{want}", "-c", "import sys; print(sys.executable)"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _pg_bin():
    base = r"C:\Program Files\PostgreSQL"
    if not os.path.isdir(base):
        return None
    versions = sorted((d for d in os.listdir(base) if d.isdigit()), key=int, reverse=True)
    for d in versions:
        path = os.path.join(base, d, "bin")
        if os.path.isfile(os.path.join(path, "psql.exe")):
            return path
    return None


def step_prerequisites(run, spec):
    print("\n== 1. prerequisites")
    found = {}
    for tool in ("git",):
        found[tool] = shutil.which(tool)
        (run.ok if found[tool] else run.warn)(f"{tool}: {found[tool] or 'NOT FOUND - install it first'}")
    found["python"] = _find_python(spec["python"])
    (run.ok if found["python"] else run.warn)(
        f"Python {spec['python']}: {found['python'] or 'NOT FOUND - install it from python.org (with the py launcher)'}")
    found["pg"] = _pg_bin()
    (run.ok if found["pg"] else run.warn)(f"PostgreSQL: {found['pg'] or 'NOT FOUND - ' + spec.get('postgresql', '')}")
    wk = shutil.which("wkhtmltopdf") or (r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe"
                                          if os.path.isfile(r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe") else None)
    (run.ok if wk else run.warn)(f"wkhtmltopdf: {wk or 'NOT FOUND - ' + spec.get('wkhtmltopdf', '')} (PDF reports)")
    code = shutil.which("code") or shutil.which("code.cmd")
    (run.ok if code else run.warn)(f"VS Code: {code or 'not found (optional)'}")
    return found


def step_sources(run, spec, root):
    print("\n== 2. sources")
    for repo in spec["repos"]:
        path = os.path.join(root, repo["path"])
        if os.path.isdir(os.path.join(path, ".git")) or (os.path.isdir(path) and os.listdir(path)):
            run.ok(f"{repo['path']} ({repo['branch']})")
            continue
        if run.do(f"clone {repo['url']} {repo['branch']} -> {repo['path']}"):
            res = subprocess.run(["git", "clone", "--depth", "1", "-b", repo["branch"], repo["url"], path])
            if res.returncode:
                (run.warn if not repo["required"] else run.warn)(
                    f"{repo['path']} could not be cloned" + (f" ({repo.get('note')})" if repo.get("note") else ""))


def step_venv(run, spec, root, python):
    print("\n== 3. venv")
    venv_py = os.path.join(root, "venv", "Scripts", "python.exe")
    if not os.path.isfile(venv_py):
        if not python:
            run.warn("no venv and no Python " + spec["python"] + ": install Python first")
            return None
        if run.do(f"create venv with {python}"):
            subprocess.run([python, "-m", "venv", os.path.join(root, "venv")], check=True)
    else:
        run.ok(f"venv ({venv_py})")
    if not os.path.isfile(venv_py):
        return None
    req = os.path.join(root, "odoo", "requirements.txt")
    missing = _missing_packages(venv_py, spec["pip_extra"])
    if os.path.isfile(req):
        # requirements: trust pip to be idempotent; extras: install only what is missing
        if missing and run.do(f"pip install {len(missing)} package(s): {', '.join(missing)}"):
            subprocess.run([venv_py, "-m", "pip", "install", "-q", "-r", req], check=False)
            subprocess.run([venv_py, "-m", "pip", "install", "-q", *missing], check=False)
        elif not missing:
            run.ok(f"packages ({len(spec['pip_extra'])} extra beyond Odoo's requirements)")
    else:
        run.warn("no odoo/requirements.txt: clone the sources first")
    return venv_py


def _missing_packages(venv_py, wanted):
    res = subprocess.run([venv_py, "-m", "pip", "list", "--format=json"], capture_output=True, text=True)
    try:
        have = {p["name"].lower().replace("_", "-") for p in json.loads(res.stdout)}
    except ValueError:
        return list(wanted)
    return [w for w in wanted if w.split("==")[0].lower().replace("_", "-") not in have]


def step_third_party(run, root):
    print("\n== 4. third-party addons")
    source = os.path.join(TOOLS_DIR, "third-party")
    target = os.path.join(root, "third-party")
    if not os.path.isdir(source):
        run.ok("these tools carry no third-party addons")
        return
    if os.path.isdir(target):
        same = sorted(os.listdir(source)) == sorted(n for n in os.listdir(target) if n != "__pycache__")
        (run.ok if same else run.warn)(
            f"third-party exists ({', '.join(sorted(os.listdir(target))) or 'empty'})"
            + ("" if same else f" - the tools carry {', '.join(sorted(os.listdir(source)))}"))
        return
    if run.do(f"link {target} -> {source}"):
        subprocess.run(["cmd", "/c", "mklink", "/J", target, source], check=True, capture_output=True)


def step_conf(run, spec, root, pg):
    print("\n== 5. odoo.conf")
    path = os.path.join(root, "odoo.conf")
    if os.path.isfile(path):
        run.ok(f"{path} (kept as it is)")
        return path
    if run.do(f"write {path} (generated passwords, not printed)"):
        addons = ",".join(f"{FS_ROOT}/odoo{spec['version']}/{p}".replace("\\", "/") for p in spec["addons_path"]
                          if os.path.isdir(os.path.join(root, p)))
        lines = ["[options]",
                 f"pg_path = {pg or ''}",
                 f"admin_passwd = {secrets.token_urlsafe(24)}",
                 "db_host = 127.0.0.1", "db_port = 5432",
                 f"db_user = {spec['db_user']}",
                 f"db_password = {secrets.token_urlsafe(24)}",
                 "; PostgreSQL 18 on Windows rejects collate C != ctype (template0); template1 keeps the cluster's.",
                 "db_template = template1",
                 f"addons_path = {addons}", ""]
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
    return path


def _conf_value(path, key):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.split("=", 1)[0].strip() == key:
                return line.split("=", 1)[1].strip()
    return None


def step_db_role(run, spec, conf, pg, with_role):
    print("\n== 6. database role")
    user = _conf_value(conf, "db_user") if conf and os.path.isfile(conf) else spec["db_user"]
    if not with_role:
        print(f"  [--]    role {user}: run `odoo-ai setup --db-role` to create or update it (asks for postgres's password)")
        return
    if not pg:
        run.warn("no PostgreSQL found")
        return
    password = _conf_value(conf, "db_password")
    if run.do(f"create/update PostgreSQL role {user} (LOGIN CREATEDB, password from odoo.conf)"):
        quoted = password.replace("'", "''")
        sql = (f"DO $$BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{user}') THEN "
               f"CREATE ROLE \"{user}\" LOGIN CREATEDB PASSWORD '{quoted}'; ELSE "
               f"ALTER ROLE \"{user}\" LOGIN CREATEDB PASSWORD '{quoted}'; END IF; END$$;")
        # through stdin: the password never appears on a command line
        subprocess.run([os.path.join(pg, "psql.exe"), "-U", "postgres", "-h", "127.0.0.1", "-v", "ON_ERROR_STOP=1",
                        "-f", "-"], input=sql, text=True, check=False)


def step_launcher(run):
    print("\n== 7. launcher, PATH, pylint plugin")
    from doctor import run_doctor
    if run.check:
        print("  (doctor reports below)")
        run_doctor(False)
    else:
        run_doctor(True)


def step_extension(run):
    print("\n== 8. VS Code extension")
    code = shutil.which("code") or shutil.which("code.cmd")
    if not code:
        run.warn("VS Code not found: skip")
        return
    pkg = json.load(open(os.path.join(TOOLS_DIR, "vscode-extension", "package.json"), encoding="utf-8"))
    listed = subprocess.run([code, "--list-extensions", "--show-versions"], capture_output=True, text=True,
                            shell=code.endswith(".cmd")).stdout
    if f"acme.odoo-ci@{pkg['version']}" in listed:
        run.ok(f"acme.odoo-ci {pkg['version']}")
        return
    if run.do(f"build and install acme.odoo-ci {pkg['version']}"):
        subprocess.run(["python", os.path.join(TOOLS_DIR, "vscode-extension", "build_vsix.py")], check=True)
        vsix = os.path.join(TOOLS_DIR, "vscode-extension", f"odoo-ci-{pkg['version']}.vsix")
        subprocess.run([code, "--install-extension", vsix, "--force"], shell=code.endswith(".cmd"), check=False)


def step_rules(run, root):
    print("\n== 9. rules and notes")
    pairs = [(os.path.join(TOOLS_DIR, "docs", "version"), root),
             (os.path.join(TOOLS_DIR, "docs", "shared"), FS_ROOT)]
    for source, target in pairs:
        if not os.path.isdir(source):
            continue
        for name in sorted(os.listdir(source)):
            dst = os.path.join(target, name)
            if os.path.isfile(dst):
                run.ok(f"{dst}")
            elif run.do(f"copy {name} -> {target}"):
                shutil.copy2(os.path.join(source, name), dst)


def run_setup(check=False, db_role=False):
    spec = json.load(open(SPEC, encoding="utf-8"))
    root = fs(f"C:/odoo/odoo{TOOLS_VERSION}")
    run = Run(check)
    print(f"[SETUP] Odoo {spec['version']} in {root}" + ("   (--check: nothing is changed)" if check else ""))
    os.makedirs(os.path.join(root, "projects"), exist_ok=True)
    found = step_prerequisites(run, spec)
    step_sources(run, spec, root)
    step_venv(run, spec, root, found.get("python"))
    step_third_party(run, root)
    conf = step_conf(run, spec, root, found.get("pg"))
    step_db_role(run, spec, conf, found.get("pg"), db_role)
    step_launcher(run)
    step_extension(run)
    step_rules(run, root)
    print(f"\n[SETUP] {'nothing to do' if not run.todo else str(run.todo) + (' to do' if check else ' done')}."
          " Next: `odoo-ai new-project <name> --git <url>` for each project.")
    return 0
