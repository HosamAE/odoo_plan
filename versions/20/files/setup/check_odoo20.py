"""Odoo 20 health check: venv, editable install, addons paths, DB login as odoo20, and a real base install."""
import configparser, os, subprocess, sys

ROOT = r"C:\odoo\odoo20"
conf = configparser.ConfigParser()
conf.read(os.path.join(ROOT, "odoo.conf"))
o = conf["options"]

def step(name, ok, detail=""):
    print(f"[{'OK' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""), flush=True)
    return ok

print("== Odoo 20 health check ==")
step("python", sys.version_info[:2] >= (3, 12), sys.version.split()[0] + " " + sys.executable)
import odoo, odoo.release as r
step("odoo import (editable)", os.path.normcase(odoo.__path__[0]).startswith(os.path.normcase(ROOT)), f"{r.version} from {odoo.__path__[0]}")
for p in o["addons_path"].split(","):
    p = p.strip()
    step(f"addons path {p}", os.path.isdir(p), f"{len(os.listdir(p)) if os.path.isdir(p) else 0} entries")

import psycopg2
try:
    cn = psycopg2.connect(host=o["db_host"], port=o["db_port"], user=o["db_user"], password=o["db_password"], dbname="postgres")
    cur = cn.cursor()
    cur.execute("select version()"); ver = cur.fetchone()[0].split(",")[0]
    cur.execute("select rolcreatedb from pg_roles where rolname = current_user"); cdb = cur.fetchone()[0]
    cn.close()
    step("db login as " + o["db_user"], True, ver)
    step("role can create databases", cdb)
except Exception as e:
    step("db login as " + o["db_user"], False, str(e).strip())
    sys.exit(1)

db = "odoo20_check"

def drop_check_db():
    cn = psycopg2.connect(host=o["db_host"], port=o["db_port"], user=o["db_user"], password=o["db_password"], dbname="postgres")
    cn.autocommit = True
    cn.cursor().execute(f'DROP DATABASE IF EXISTS "{db}"')
    cn.close()

print("db_template = " + o.get("db_template", "template0 (default)"))
drop_check_db()  # a check database left by an earlier run
print(f"\n== Installing 'base' into a fresh database '{db}' (takes 1-3 min) ==", flush=True)
cmd = [sys.executable, os.path.join(ROOT, "odoo", "odoo-bin"), "-c", os.path.join(ROOT, "odoo.conf"),
       "-d", db, "-i", "base", "--stop-after-init", "--log-level=warn"]
res = subprocess.run(cmd, cwd=os.path.join(ROOT, "odoo"), capture_output=True, text=True)
out = (res.stdout + res.stderr).strip()
errors = [l for l in out.splitlines() if " ERROR " in l or " CRITICAL " in l or "Traceback" in l]
print("\n".join(out.splitlines()[-25:]))
step("odoo-bin -i base --stop-after-init", res.returncode == 0 and not errors, f"exit {res.returncode}, {len(errors)} error lines")
if res.returncode == 0 and not errors:
    drop_check_db()
    print("check database removed")
print("\nHEALTH-CHECK: " + ("PASSED" if res.returncode == 0 and not errors else "FAILED"))
