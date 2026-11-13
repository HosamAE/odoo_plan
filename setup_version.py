"""odoo-ai setup: check this Odoo version's folder, and fill in what the tools own.

The tools (this repository) are the structure and everything around the code.
Odoo itself and the venv are cloned and created here when they are missing (`--check` only says what is missing
and the command for it). Enterprise, the themes and third-party addons need an account or are optional: setup says
so and gives the command, and never forces them:

    git clone -b 19.0 https://github.com/HosamAE/odoo_plan C:\\odoo\\odoo19\\tools
    python C:\\odoo\\odoo19\\tools\\cli.py setup --check

  1. prerequisites   git, Python <version.json python>, PostgreSQL, wkhtmltopdf, VS Code   (check)
  2. sources         odoo cloned when missing; design-themes, enterprise, third-party named (check)
  3. venv            created when missing, with Odoo's requirements and setup/requirements-extra.txt
  4. projects        the projects folder                                                 (made)
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
        self.failed = 0

    def ok(self, text):
        print(f"  [OK]    {text}")

    def do(self, text):
        print(f"  [{'TODO' if self.check else 'DO'}]  {text}")
        self.todo += 1
        return not self.check

    def warn(self, text):
        print(f"  [WARN]  {text}")

    def execute(self, argv, what, cwd=None):
        """Run a command for a step that `do` announced (only outside --check). True when it succeeded."""
        print(f"          $ {' '.join(argv)}", flush=True)
        try:
            code = subprocess.run(argv, cwd=cwd, check=False).returncode
        except OSError as exc:
            print(f"  [FAIL]  {what}: {exc}")
            self.failed += 1
            return False
        if code != 0:
            print(f"  [FAIL]  {what}: the command ended with code {code}; fix it and run the setup again (it is safe to repeat)")
            self.failed += 1
            return False
        return True


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
    print("\n== 2. sources (cloned here when missing)")
    for repo in spec["repos"]:
        path = os.path.join(root, repo["path"])
        if os.path.isdir(path) and os.listdir(path):
            run.ok(f"{repo['path']}")
            continue
        command = ["git", "clone", "--depth", "1", "-b", repo["branch"], repo["url"], path]
        text = f"{repo['path']} is missing: {' '.join(command)}" + (f"  ({repo.get('note')})" if repo.get("note") else "")
        if not repo["required"]:
            run.warn(text)   # needs an account or is optional: said, never forced
        elif run.do(text):
            run.execute(command, f"clone {repo['path']}")
    third = os.path.join(root, "third-party")
    if os.path.isdir(third):
        run.ok(f"third-party ({', '.join(sorted(os.listdir(third))) or 'empty'})")
    else:
        run.warn(f"no {third}: create it for the third-party addons the projects use (it is in addons_path)")


def step_venv(run, spec, root, python):
    print("\n== 3. venv (created here when missing)")
    venv_py = os.path.join(root, "venv", "Scripts", "python.exe")
    extra = os.path.join(TOOLS_DIR, spec.get("requirements_extra", "setup/requirements-extra.txt"))
    requirements = os.path.join(root, "odoo", "requirements.txt")
    install_argv = [venv_py, "-m", "pip", "install", "-r", requirements, "-r", extra]
    install = " ".join(install_argv)
    if not os.path.isfile(venv_py):
        if not python:
            run.warn(f"no venv, and Python {spec['python']} was not found: install it, then run the setup again")
            return None
        if run.do(f"no venv: {python} -m venv {os.path.join(root, 'venv')}   then   {install}"):
            if not run.execute([python, "-m", "venv", os.path.join(root, "venv")], "create the venv"):
                return None
            if not os.path.isfile(requirements):
                run.warn(f"{requirements} is missing (the Odoo source was not cloned): packages are not installed")
                return venv_py
            run.execute(install_argv, "install the packages")
            return venv_py
        return None
    run.ok(f"venv ({venv_py})")
    wanted = [l.strip() for l in open(extra, encoding="utf-8") if l.strip() and not l.startswith("#")]
    missing = _missing_packages(venv_py, wanted)
    if missing:
        if run.do(f"{len(missing)} package(s) missing ({', '.join(missing)}):   {install}") and os.path.isfile(requirements):
            run.execute(install_argv, "install the packages")
    else:
        run.ok(f"packages ({len(wanted)} beyond Odoo's requirements, {os.path.basename(extra)})")
    return venv_py


def _missing_packages(venv_py, wanted):
    res = subprocess.run([venv_py, "-m", "pip", "list", "--format=json"], capture_output=True, text=True)
    try:
        have = {p["name"].lower().replace("_", "-") for p in json.loads(res.stdout)}
    except ValueError:
        return list(wanted)
    return [w for w in wanted if w.split("==")[0].lower().replace("_", "-") not in have]


def step_projects(run, root):
    print("\n== 4. projects folder")
    path = os.path.join(root, "projects")
    if os.path.isdir(path):
        run.ok(f"{path}")
    elif run.do(f"create {path}"):
        os.makedirs(path)


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
    if f"HosamAE.odoo-ci@{pkg['version']}" in listed:
        run.ok(f"HosamAE.odoo-ci {pkg['version']}")
        return
    if run.do(f"build and install HosamAE.odoo-ci {pkg['version']}"):
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
    found = step_prerequisites(run, spec)
    step_sources(run, spec, root)
    step_venv(run, spec, root, found.get("python"))
    step_projects(run, root)
    conf = step_conf(run, spec, root, found.get("pg"))
    step_db_role(run, spec, conf, found.get("pg"), db_role)
    step_launcher(run)
    step_extension(run)
    step_rules(run, root)
    if check:
        print(f"\n[SETUP] {'nothing to do' if not run.todo else str(run.todo) + ' to do'}.")
        return 0
    done = run.todo - run.failed
    print(f"\n[SETUP] {'nothing to do' if not run.todo else str(done) + ' done'}" + (f", {run.failed} FAILED (see above)" if run.failed else "")
          + ". Next: `odoo-ai new-project <name> --git <url>` for each project.")
    return 1 if run.failed else 0
