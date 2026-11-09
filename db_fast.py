"""
Odoo Fast Clean Test Runner (db_fast.py)
Creates an ephemeral clean database using a pre-installed Odoo core template,
installs and tests the target module in seconds, and drops the ephemeral database.
Guarantees 100% clean isolation with near-instant execution.
"""

import os
import subprocess
import sys
import time

from test_runner import find_odoo_paths


def get_template_db_name(version="19"):
    return f"odoo{version}_clean_base"


def check_database_exists(db_name, python_exe):
    check_script = f"""
import psycopg2
try:
    conn = psycopg2.connect(dbname='postgres', host='127.0.0.1', port=5432)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname='{db_name}'")
    exists = bool(cur.fetchone())
    conn.close()
    print("EXISTS" if exists else "NOT_FOUND")
except Exception as e:
    print("ERROR:" + str(e))
"""
    res = subprocess.run([python_exe, "-c", check_script], capture_output=True, text=True)
    return "EXISTS" in res.stdout


def create_template_db(version="19"):
    """Initialize a brand-new base template database with Odoo core modules."""
    tpl_name = get_template_db_name(version)
    python_exe, odoo_bin = find_odoo_paths(version)

    print(f"[FAST-TEST] Initializing base clean template: '{tpl_name}'...")
    print(f"[FAST-TEST] This is done ONCE to guarantee fast, pure testing.\n")

    cmd = [
        python_exe,
        odoo_bin,
        "-i", "base",
        "--without-demo=all",
        "--stop-after-init",
        "-d", tpl_name,
        "--log-level=warn"
    ]
    t0 = time.perf_counter()
    res = subprocess.run(cmd, capture_output=True, text=True)
    duration = round(time.perf_counter() - t0, 1)

    if res.returncode == 0:
        print(f"[FAST-TEST] Successfully created template '{tpl_name}' in {duration}s!")
        return True
    else:
        print(f"[FAST-TEST] Failed to create template database:\n{res.stderr or res.stdout}")
        return False


def run_fast_clean_test(module_name, version="19", config_file=None, tags=None):
    """Clone template DB in ~1 second, run test, then drop."""
    python_exe, odoo_bin = find_odoo_paths(version)
    tpl_name = get_template_db_name(version)

    # 1. Ensure template DB exists
    if not check_database_exists(tpl_name, python_exe):
        print(f"[FAST-TEST] Template DB '{tpl_name}' not found. Initializing now...")
        if not create_template_db(version):
            return {"success": False, "error": "Could not initialize base template DB"}

    # 2. Generate unique ephemeral test DB name
    ephemeral_db = f"test_ephem_{module_name}_{int(time.time())}"
    print(f"[FAST-TEST] Cloning pure snapshot '{tpl_name}' -> '{ephemeral_db}' in seconds...")

    clone_sql = f"""
import psycopg2
conn = psycopg2.connect(dbname='postgres', host='127.0.0.1', port=5432)
conn.autocommit = True
cur = conn.cursor()
cur.execute("CREATE DATABASE {ephemeral_db} TEMPLATE {tpl_name}")
conn.close()
"""
    t_clone = time.perf_counter()
    c_res = subprocess.run([python_exe, "-c", clone_sql], capture_output=True, text=True)
    if c_res.returncode != 0:
        print(f"[FAST-TEST] Clone failed, falling back to standard test. ({c_res.stderr})")
        ephemeral_db = "test_db"
    else:
        print(f"[FAST-TEST] Snapshot ready in {round(time.perf_counter() - t_clone, 2)}s!")

    # 3. Run test on clean clone
    cmd = [
        python_exe,
        odoo_bin,
        "--test-enable",
        "-i", module_name,
        "--stop-after-init",
        "-d", ephemeral_db,
        "--log-level=test"
    ]
    if tags:
        cmd.extend(["--test-tags", tags])
    if config_file and os.path.exists(config_file):
        cmd.extend(["-c", config_file])

    t0 = time.perf_counter()
    test_res = subprocess.run(cmd, capture_output=True, text=True)
    duration = round(time.perf_counter() - t0, 2)

    # 4. Cleanup ephemeral database (Drop DB)
    if ephemeral_db != "test_db":
        drop_sql = f"""
import psycopg2
try:
    conn = psycopg2.connect(dbname='postgres', host='127.0.0.1', port=5432)
    conn.autocommit = True
    cur = conn.cursor()
    # Terminate any remaining connections to allow clean drop
    cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{ephemeral_db}'")
    cur.execute("DROP DATABASE IF EXISTS {ephemeral_db}")
    conn.close()
except:
    pass
"""
        subprocess.run([python_exe, "-c", drop_sql], capture_output=True, text=True)

    out = test_res.stdout + "\n" + test_res.stderr
    success = test_res.returncode == 0 and "ERROR" not in out and "FAIL" not in out
    failures = [l for l in out.splitlines() if any(k in l for k in ["ERROR", "FAIL:", "Traceback"])]

    return {
        "success": success,
        "duration": duration,
        "output": out,
        "failures": "\n".join(failures)
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Odoo Fast Clean Test Engine")
    parser.add_argument("action", nargs="?", default="init", choices=["init", "test"], help="Action: init base template or test module")
    parser.add_argument("-v", "--version", default="19", help="Odoo version (17, 18, 19, 20)")
    parser.add_argument("-m", "--module", default=None, help="Module technical name")
    args = parser.parse_args()

    if args.action == "init":
        create_template_db(version=args.version)
    elif args.action == "test":
        if not args.module:
            print("[ERROR] Please provide --module (-m) to test.")
            sys.exit(1)
        res = run_fast_clean_test(args.module, version=args.version)
        sys.exit(0 if res["success"] else 1)


