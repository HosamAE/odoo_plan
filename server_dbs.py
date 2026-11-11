"""What the cockpit shows about the databases and the modules, and the two things it lets you do to a database.

  inventory   every database: size, Odoo version, modules, last login, whether it can be opened
  modules     one database's modules against the code on disk: which are installed, which wait for an upgrade,
              which have a newer version in the code than in the database, which have no code any more
  backup      a dump with its filestore, before anything risky
  drop        delete a database - only when told to (--yes), only when nobody else is connected (or --force),
              and, when a backup folder is given, only after the backup succeeded

Read-only except `backup` (writes a file) and `drop`.
"""
import ast
import datetime
import json
import os
import re
import subprocess

import server_ctl

NAME = re.compile(r"[\w.-]+")


# ----------------------------------------------------------------------------- inventory

def inventory(options):
    """[{name, size_mb, version, modules, last_login, usable, reason}], by name. Never raises for one bad database."""
    conn = server_ctl._connect(options)
    try:
        cur = conn.cursor()
        cur.execute("SELECT datname, pg_database_size(datname) FROM pg_database "
                    "WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY datname")
        rows = cur.fetchall()
    finally:
        conn.close()
    result = []
    for name, size in rows:
        entry = {"name": name, "size_mb": round(size / 1048576), "version": None, "modules": None,
                 "last_login": None, "usable": True, "reason": None}
        try:
            db = server_ctl._connect(options, name)
        except Exception as exc:  # noqa: BLE001 - one unreadable database must not hide the others
            entry.update(usable=False, reason=type(exc).__name__)
            result.append(entry)
            continue
        try:
            cur = db.cursor()
            entry["version"] = _one(cur, "SELECT latest_version FROM ir_module_module WHERE name = 'base'")
            entry["modules"] = _one(cur, "SELECT count(*) FROM ir_module_module WHERE state = 'installed'")
            last = _one(cur, "SELECT max(create_date)::date FROM res_users_log")
            entry["last_login"] = str(last) if last else None
        finally:
            db.close()
        result.append(entry)
    return result


def _one(cur, sql):
    """The first value of a query, or None when the table is not there (not an Odoo database, or a fresh one)."""
    try:
        cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        cur.connection.rollback()
        return None


# ----------------------------------------------------------------------------- modules against the code

def parse_manifest_version(path):
    """The `version` of an Odoo manifest, read as data (never executed), or None."""
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Dict):
                data = ast.literal_eval(node.value)
                version = data.get("version")
                return str(version) if version else None
    except (OSError, SyntaxError, ValueError):
        pass
    return None


def addons_paths(options, conf_path):
    """The folders the conf's addons_path names, relative ones read from the conf's own folder; those that exist."""
    base = os.path.dirname(os.path.abspath(conf_path))
    paths = []
    for raw in (options.get("addons_path") or "").split(","):
        raw = raw.strip()
        if raw:
            path = os.path.normpath(raw if os.path.isabs(raw) else os.path.join(base, raw))
            if os.path.isdir(path):
                paths.append(path)
    return paths


def code_versions(paths):
    """{module: version or None} for every folder with a manifest, in the addons paths. The first path wins."""
    found = {}
    for folder in paths:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            manifest = os.path.join(folder, name, "__manifest__.py")
            if name not in found and os.path.isfile(manifest):
                found[name] = parse_manifest_version(manifest)
    return found


def expected_version(code_version, series):
    """What Odoo stores for a manifest version: it puts the series in front when the manifest has fewer parts."""
    if not code_version:
        return None
    return code_version if code_version.startswith(f"{series}.") else f"{series}.{code_version}"


def compare_modules(code, rows, series):
    """The database's modules against the code. rows: [(name, state, latest_version)].

    status: to_upgrade / to_install / to_remove (Odoo already knows), version_differs (the code's version is not
    the installed one: an upgrade is due), missing_code (installed, but no folder has it), ok, available (in the
    code, not installed)."""
    modules, counts = [], {}
    seen = set()
    for name, state, installed in sorted(rows):
        seen.add(name)
        if state == "uninstallable":
            continue   # an Enterprise or removed module the database remembers: nothing to do about it
        if state == "uninstalled":
            if name in code:
                status = "available"
            else:
                continue
        elif state in ("to upgrade", "to install", "to remove"):
            status = state.replace(" ", "_")
        elif state == "installed":
            if name not in code:
                status = "missing_code"
            else:
                want = expected_version(code[name], series)
                status = "version_differs" if want and installed and want != installed else "ok"
        else:
            status = state or "unknown"
        modules.append({"name": name, "state": state, "installed_version": installed,
                        "code_version": expected_version(code.get(name), series), "status": status})
        counts[status] = counts.get(status, 0) + 1
    for name in sorted(set(code) - seen):
        modules.append({"name": name, "state": "not_in_database", "installed_version": None,
                        "code_version": expected_version(code[name], series), "status": "available"})
        counts["available"] = counts.get("available", 0) + 1
    return {"series": series, "modules": modules, "counts": counts}


def modules_report(options, conf_path, database, extra_paths=()):
    conn = server_ctl._connect(options, database)
    try:
        cur = conn.cursor()
        base = _one(cur, "SELECT latest_version FROM ir_module_module WHERE name = 'base'") or ""
        series = ".".join(base.split(".")[:2]) or "19.0"
        cur.execute("SELECT name, state, latest_version FROM ir_module_module")
        rows = [tuple(r) for r in cur.fetchall()]
    finally:
        conn.close()
    paths = addons_paths(options, conf_path) + [p for p in extra_paths if os.path.isdir(p)]
    return compare_modules(code_versions(paths), rows, series)


# ----------------------------------------------------------------------------- backup and drop

def backup_path(directory, database, now=None):
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d_%H%M%S")
    return os.path.join(directory, f"{database}_{stamp}.zip")


def backup(python, odoo_bin, conf, database, directory):
    """Dump a database with its filestore. Returns (ok, path, message)."""
    if not NAME.fullmatch(database):
        raise ValueError(f"not a database name: {database!r}")
    os.makedirs(directory, exist_ok=True)
    target = backup_path(directory, database)
    command = [python, odoo_bin, "db", "-c", conf, "dump", database, target]
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=3600, check=False)
    ok = proc.returncode == 0 and os.path.isfile(target) and os.path.getsize(target) > 0
    if not ok:
        return False, target, (proc.stderr or proc.stdout or "the dump produced no file").strip().splitlines()[-1][:200]
    return True, target, f"{round(os.path.getsize(target) / 1048576)} MB"


def drop(python, odoo_bin, conf, database):
    command = [python, odoo_bin, "db", "-c", conf, "drop", database]
    proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=900, check=False)
    return proc.returncode == 0, (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]


# ----------------------------------------------------------------------------- the CLI

def cmd_inventory(args):
    options = server_ctl.read_conf(args.conf)
    try:
        data = inventory(options)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"databases": [], "error": type(exc).__name__}) if args.json
              else f"cannot list databases: {type(exc).__name__}")
        return 1
    if args.json:
        print(json.dumps({"databases": data}))
    else:
        for d in data:
            print(f"{d['name']:34}{d['size_mb']:>7} MB  {str(d['version'] or '-'):10}"
                  f"{str(d['modules'] or '-'):>5} modules  last login {d['last_login'] or '-'}"
                  f"{'' if d['usable'] else '  (cannot be opened)'}")
    return 0


def cmd_modules(args):
    if not args.db:
        print("modules needs a database (-d)")
        return 2
    options = server_ctl.read_conf(args.conf)
    try:
        # `base` lives in odoo/odoo/addons, a folder Odoo adds by itself and the conf never names
        core = os.path.join(os.path.dirname(server_ctl._odoo_bin(args)), "odoo", "addons")
        report = modules_report(options, args.conf, args.db, [core])
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": type(exc).__name__}) if args.json else f"cannot read {args.db}: {type(exc).__name__}")
        return 1
    if args.json:
        print(json.dumps(report))
    else:
        print(", ".join(f"{k}: {v}" for k, v in sorted(report["counts"].items())))
        for m in report["modules"]:
            if m["status"] not in ("ok", "available"):
                print(f"  {m['status']:16} {m['name']} (db {m['installed_version']}, code {m['code_version']})")
    return 0


def cmd_backup(args):
    if not args.db or not args.dir:
        print("backup needs a database (-d) and a folder (--dir)")
        return 2
    ok, target, message = backup(args.python, server_ctl._odoo_bin(args), args.conf, args.db, args.dir)
    print(json.dumps({"ok": ok, "path": target, "message": message}) if args.json else
          (f"Backed up to {target} ({message})" if ok else f"BACKUP FAILED: {message}"))
    return 0 if ok else 1


def cmd_drop(args):
    """Delete a database, safely. Exit 0 done, 2 not allowed, 3 in use, 4 the backup failed, 1 the drop failed."""
    if not args.db:
        print("drop needs a database (-d)")
        return 2
    if not NAME.fullmatch(args.db):
        print(f"not a database name: {args.db!r}")
        return 2
    if not args.yes:
        print("REFUSED: this deletes the database and its filestore; say --yes to confirm")
        return 2
    options = server_ctl.read_conf(args.conf)
    if not args.force:
        try:
            others = server_ctl.other_connections(options, args.db)
        except Exception as exc:  # noqa: BLE001
            print(f"cannot check who uses {args.db} ({type(exc).__name__}); use --force to go on")
            return 2
        if others:
            print(f"REFUSED: {others} session(s) are connected to {args.db} (a running server?). Stop it first, or --force.")
            return 3
    odoo_bin = server_ctl._odoo_bin(args)
    if args.backup_dir:
        ok, target, message = backup(args.python, odoo_bin, args.conf, args.db, args.backup_dir)
        if not ok:
            print(f"NOT DROPPED: the backup failed ({message})")
            return 4
        print(f"Backed up to {target} ({message})")
    done, tail = drop(args.python, odoo_bin, args.conf, args.db)
    print(f"Dropped {args.db}" if done else f"DROP FAILED: {tail[0]}")
    return 0 if done else 1
