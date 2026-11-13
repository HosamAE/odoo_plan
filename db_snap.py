"""Database Snapshot & Instant Rollback Manager for Odoo.

Leverages PostgreSQL fast block-level cloning (CREATE DATABASE ... TEMPLATE)
to take instant development snapshots and rollbacks in 1-2 seconds.

Usage:
  python db_snap.py save [label] [--db <name>]
  python db_snap.py restore <label> [--db <name>]
  python db_snap.py list [--db <name>]
  python db_snap.py drop <label> [--db <name>]
"""
import argparse
import datetime
import os
import re
import sys


def read_conf_credentials():
    """Scan current directory and nearby paths for .conf to read DB credentials."""
    creds = {}
    cwd = os.getcwd()
    search_dirs = [cwd, os.path.dirname(cwd)]
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.endswith(".conf"):
                conf_p = os.path.join(d, f)
                try:
                    with open(conf_p, "r", encoding="utf-8") as fin:
                        for line in fin:
                            m_user = re.match(r"^\s*db_user\s*=\s*(.+)$", line)
                            if m_user:
                                creds["db_user"] = m_user.group(1).strip()
                            m_pwd = re.match(r"^\s*db_password\s*=\s*(.+)$", line)
                            if m_pwd:
                                creds["db_password"] = m_pwd.group(1).strip()
                            m_name = re.match(r"^\s*db_name\s*=\s*(.+)$", line)
                            if m_name and m_name.group(1).strip().lower() != "false":
                                creds["db_name"] = m_name.group(1).strip()
                    if creds:
                        return creds
                except Exception:
                    pass
    return creds

def get_pg_connection(db_name="postgres", db_user=None, db_password=None, db_host="127.0.0.1", db_port=5432):
    import psycopg2   # here, not at the top: `db-snap --help` and every import work on a machine with no Odoo packages
    conf = read_conf_credentials()
    user = db_user or conf.get("db_user")
    password = db_password or conf.get("db_password")

    kwargs = {"dbname": db_name, "host": db_host, "port": db_port}
    if user:
        kwargs["user"] = user
    if password:
        kwargs["password"] = password

    try:
        conn = psycopg2.connect(**kwargs)
    except psycopg2.OperationalError:
        # Fallback to direct local connection without explicit auth
        conn = psycopg2.connect(dbname=db_name, host=db_host, port=db_port)

    conn.autocommit = True
    return conn

def detect_project_db():
    conf = read_conf_credentials()
    return conf.get("db_name", "odoo")

class SnapshotError(ValueError):
    """A name or label that cannot be used safely, or a snapshot that cannot be identified."""


def ident(name):
    """A database name as a quoted SQL identifier (the quote doubled), or SnapshotError when it cannot be one."""
    name = str(name or "")
    if not name or len(name.encode("utf-8")) > 63 or any(ord(c) < 32 or c == "\x7f" for c in name):
        raise SnapshotError(f"'{name}' is not a usable database name (empty, longer than 63 bytes, or has control characters)")
    return '"' + name.replace('"', '""') + '"'


def snapshot_name(db_name, label):
    """snap_<db>_<label>, with the label cleaned. The name must fit PostgreSQL's 63 bytes: a longer one is silently cut
    by the server, and two different labels would become the same snapshot."""
    clean = re.sub(r"[^a-zA-Z0-9_]", "_", label)
    name = f"snap_{db_name}_{clean}"
    ident(name)
    return name, clean


def terminate_connections(cur, db_name):
    cur.execute("""
        SELECT pg_terminate_backend(pid)
        FROM pg_stat_activity
        WHERE datname = %s AND pid <> pg_backend_pid();
    """, (db_name,))

def save_snapshot(label, db_name, conn):
    if not label:
        label = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_db, clean_label = snapshot_name(db_name, label)
    ident(db_name)

    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (db_name,))
    if not cur.fetchone():
        print(f"[ERROR] Database '{db_name}' does not exist.")
        return False

    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (snap_db,))
    if cur.fetchone():
        print(f"[WARNING] Snapshot '{snap_db}' already exists. Overwriting...")
        terminate_connections(cur, snap_db)
        cur.execute(f"DROP DATABASE {ident(snap_db)};")

    print(f"[*] Taking instant snapshot of '{db_name}' -> '{snap_db}'...")
    terminate_connections(cur, db_name)
    cur.execute(f"CREATE DATABASE {ident(snap_db)} WITH TEMPLATE {ident(db_name)};")
    print(f"[SUCCESS] Snapshot '{clean_label}' saved successfully in ~1s!")
    return True

def restore_snapshot(label, db_name, conn):
    """Replace db_name by a snapshot of it. Only the snapshot named exactly by the label is used: a guess would drop the
    live database and bring back another one."""
    snap_db, clean_label = snapshot_name(db_name, label)
    ident(db_name)

    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (snap_db,))
    if not cur.fetchone():
        print(f"[ERROR] Snapshot '{snap_db}' not found. Nothing was changed. Snapshots: `db-snap list --db {db_name}`.")
        return False

    print(f"[*] Restoring '{db_name}' from snapshot '{snap_db}'...")
    terminate_connections(cur, db_name)
    terminate_connections(cur, snap_db)

    cur.execute(f"DROP DATABASE IF EXISTS {ident(db_name)};")
    cur.execute(f"CREATE DATABASE {ident(db_name)} WITH TEMPLATE {ident(snap_db)};")
    print(f"[SUCCESS] Database '{db_name}' restored to snapshot '{clean_label}'!")
    return True

def list_snapshots(db_name, conn):
    cur = conn.cursor()
    pattern = f"snap_{db_name}_%" if db_name else "snap_%"
    cur.execute("""
        SELECT datname, pg_size_pretty(pg_database_size(datname)) as size
        FROM pg_database
        WHERE datname LIKE %s
        ORDER BY datname;
    """, (pattern,))
    rows = cur.fetchall()
    if not rows:
        print(f"No snapshots found for database '{db_name or 'any'}'.")
        return

    print(f"\n{'Snapshot Name':<45} {'Size':<10}")
    print("-" * 57)
    for r in rows:
        print(f"{r[0]:<45} {r[1]:<10}")
    print("-" * 57)
    print(f"Total: {len(rows)} snapshot(s)\n")

def drop_snapshot(label, db_name, conn):
    clean_label = re.sub(r"[^a-zA-Z0-9_]", "_", label)
    snap_db = clean_label if clean_label.startswith("snap_") else snapshot_name(db_name, label)[0]
    ident(snap_db)

    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (snap_db,))
    if not cur.fetchone():
        print(f"[ERROR] Snapshot '{snap_db}' not found.")
        return False

    terminate_connections(cur, snap_db)
    cur.execute(f"DROP DATABASE {ident(snap_db)};")
    print(f"[SUCCESS] Dropped snapshot '{snap_db}'.")
    return True

def main():
    parser = argparse.ArgumentParser(description="Odoo Fast Database Snapshots & Rollbacks")
    subparsers = parser.add_subparsers(dest="action", required=True)

    p_save = subparsers.add_parser("save", help="Save a database snapshot")
    p_save.add_argument("label", nargs="?", default="", help="Snapshot label/name")
    p_save.add_argument("--db", default="", help="Target database name")

    p_restore = subparsers.add_parser("restore", help="Restore database from snapshot")
    p_restore.add_argument("label", help="Snapshot label/name")
    p_restore.add_argument("--db", default="", help="Target database name")

    p_list = subparsers.add_parser("list", help="List available snapshots")
    p_list.add_argument("--db", default="", help="Filter by database name")

    p_drop = subparsers.add_parser("drop", help="Delete a snapshot")
    p_drop.add_argument("label", help="Snapshot label/name")
    p_drop.add_argument("--db", default="", help="Target database name")

    args = parser.parse_args()

    db_name = args.db or detect_project_db()
    try:
        conn = get_pg_connection()
    except Exception as e:
        print(f"[ERROR] Could not connect to PostgreSQL on port 5432: {e}")
        sys.exit(1)

    try:
        if args.action in ("save", "restore", "drop"):
            ident(db_name)
        if args.action == "save":
            ok = save_snapshot(args.label, db_name, conn)
            sys.exit(0 if ok else 1)
        elif args.action == "restore":
            ok = restore_snapshot(args.label, db_name, conn)
            sys.exit(0 if ok else 1)
        elif args.action == "list":
            list_snapshots(args.db, conn)
        elif args.action == "drop":
            ok = drop_snapshot(args.label, db_name, conn)
            sys.exit(0 if ok else 1)
    except SnapshotError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
