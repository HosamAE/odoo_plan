"""Databases on other servers, reached over SSH - the same hosts your editor's Remote-SSH uses (~/.ssh/config).

  hosts     the hosts of ~/.ssh/config
  check     can this host be reached without a question (key or agent)?
  list      the host's databases: size, owner, sessions, and the Odoo instances (their .conf) on it
  pull      a remote database into a NEW local one (dump + filestore, then neutralized: no crons, no mail)
  push      a local database to the host as a NEW one (never over an existing one)
  backup    a remote database into a file on this machine
  drop      delete a remote database - only with the host and name typed again (--confirm host/db)

Nothing is ever written over a database that exists, on either side. Reading a server is `pg_dump`; the
only remote writes are `push` (a new database, a new filestore folder) and `drop`.
Passwords are never stored or printed: the connection uses your key or agent. Where the host asks for a password,
run the command in a terminal with --interactive and ssh asks you itself.
The postgres commands run as the postgres user through `sudo -n` (--sudo-user, or --no-sudo when your ssh
user is itself a postgres role). A sudo that asks for a password is reported, not worked around.
"""
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile

import server_ctl

NAME = re.compile(r"[\w.-]+")
HOST = re.compile(r"[\w.@-]+")
PROTECTED = {"postgres", "template0", "template1"}
SSH = os.environ.get("ODOO_AI_SSH", "ssh")
SSH_CONFIG = os.path.join(os.path.expanduser("~"), ".ssh", "config")
CONF_GLOB = "/etc/odoo*/*.conf"
LIST_SQL = ("SELECT d.datname, (pg_database_size(d.datname)/1048576)::int, pg_get_userbyid(d.datdba), "
            "(SELECT count(*) FROM pg_stat_activity a WHERE a.datname = d.datname) "
            "FROM pg_database d WHERE NOT d.datistemplate AND d.datname <> 'postgres' ORDER BY 1")


class RemoteError(Exception):
    """Something the person can act on (not a crash)."""


def step(number, total, text):
    print(f"STEP {number}/{total} {text}", file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------- hosts and ssh

def parse_ssh_config(text):
    """[{name, hostname, user}] for the plain host names of an ssh config (a pattern like `*` is not a host)."""
    hosts, current = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        key, value = key.lower().rstrip("="), value.strip().lstrip("=").strip()
        if key == "host":
            current = []
            for name in value.split():
                if not any(c in name for c in "*?!") and HOST.fullmatch(name):
                    entry = {"name": name, "hostname": None, "user": None}
                    hosts.append(entry)
                    current.append(entry)
        elif key in ("hostname", "user"):
            for entry in current:
                entry[key] = entry[key] or value
    return hosts


def ssh_hosts(config=None):
    try:
        with open(config or SSH_CONFIG, encoding="utf-8") as handle:
            return parse_ssh_config(handle.read())
    except OSError:
        return []


def ssh_command(host, remote, interactive=False):
    """The local command that runs `remote` on host. Quiet and question-free unless --interactive."""
    if not HOST.fullmatch(host):
        raise RemoteError(f"not a host name: {host!r}")
    options = ["-o", "ConnectTimeout=15"] + ([] if interactive else ["-o", "BatchMode=yes"])
    return shlex.split(SSH, posix=False) + options + [host, remote]


def pg_prefix(args):
    if getattr(args, "no_sudo", False):
        return ""
    user = getattr(args, "sudo_user", None) or "postgres"
    if not NAME.fullmatch(user):
        raise RemoteError(f"not a user name: {user!r}")
    return f"sudo -n -u {user} "


def explain(stderr):
    """What a failed ssh/sudo said, in a sentence a person can act on."""
    text = (stderr or "").strip()
    low = text.lower()
    if "permission denied" in low and "publickey" in low:
        return "the host wants a password or another key (ssh asked, and --batch cannot answer): use --interactive in a terminal"
    if "host key verification failed" in low:
        return "the host's key is not known (or has changed): connect once with ssh in a terminal and check it"
    if "could not resolve hostname" in low or "connection timed out" in low or "no route" in low or "connection refused" in low:
        return "cannot reach the host (name, network or VPN)"
    if "a password is required" in low or "sudo:" in low and "password" in low:
        return "sudo asks for a password on that host: allow it without one for the postgres commands, or use --no-sudo"
    if "role" in low and "does not exist" in low:
        return "that ssh user has no postgres role: use sudo (the default) or pick the right --sudo-user"
    return (text.splitlines() or ["no answer"])[-1][:240]


def run_remote(host, remote, interactive=False, input_bytes=None, timeout=300):
    """(returncode, stdout, stderr) of a short remote command."""
    proc = subprocess.run(ssh_command(host, remote, interactive), input=input_bytes, capture_output=True,
                          timeout=timeout, check=False)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")


def check(host, interactive=False):
    code, out, err = run_remote(host, "echo hx-ok", interactive, timeout=40)
    return {"host": host, "ok": code == 0 and "hx-ok" in out, "message": "reachable" if code == 0 else explain(err)}


# ----------------------------------------------------------------------------- reading a host

def parse_databases(text):
    rows = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) == 4 and NAME.fullmatch(parts[0]):
            rows.append({"name": parts[0], "size_mb": int(parts[1] or 0), "owner": parts[2], "sessions": int(parts[3] or 0)})
    return rows


INSTANCE_SCRIPT = (
    "for f in %s; do [ -r \"$f\" ] || sudo -n test -r \"$f\" 2>/dev/null || continue; "
    "echo \"== $f\"; (cat \"$f\" 2>/dev/null || sudo -n cat \"$f\") | grep -E '^(db_name|db_user|data_dir|http_port) *=' ; done" % CONF_GLOB)


def parse_instances(text):
    """[{conf, db_name, db_user, data_dir, http_port}] from the script's output. Nothing else of a conf is read."""
    instances, current = [], None
    for line in text.splitlines():
        if line.startswith("== "):
            current = {"conf": line[3:].strip(), "db_name": None, "db_user": None, "data_dir": None, "http_port": None}
            instances.append(current)
        elif current is not None:
            match = re.match(r"^(db_name|db_user|data_dir|http_port)\s*=\s*(.*?)\s*$", line)
            if match and match.group(2).lower() not in ("", "false"):
                current[match.group(1)] = match.group(2)
    return instances


def list_remote(host, args):
    code, out, err = run_remote(host, f"{pg_prefix(args)}psql -At -F'|' -c {shlex.quote(LIST_SQL)}", getattr(args, "interactive", False))
    if code != 0:
        raise RemoteError(explain(err))
    databases = parse_databases(out)
    code, out, _ = run_remote(host, INSTANCE_SCRIPT, getattr(args, "interactive", False))
    return {"host": host, "databases": databases, "instances": parse_instances(out) if code == 0 else []}


def find_filestore(host, database, args):
    """The remote folder holding a database's files (<data_dir>/filestore/<db>), or None."""
    script = (f"for f in {CONF_GLOB}; do d=$( (cat \"$f\" 2>/dev/null || sudo -n cat \"$f\" 2>/dev/null) | "
              f"sed -n 's/^data_dir *= *//p' | head -1); [ -n \"$d\" ] && sudo -n test -d \"$d/filestore/{database}\" && echo \"$d/filestore/{database}\" && break; done")
    code, out, _ = run_remote(host, script, getattr(args, "interactive", False))
    return out.strip().splitlines()[0] if code == 0 and out.strip() else None


# ----------------------------------------------------------------------------- local tools

def find_pg_tool(name, environ=None):
    """pg_dump / pg_restore of the newest PostgreSQL here: a dump of version N is restored by N or newer."""
    exe = name + (".exe" if os.name == "nt" else "")
    found = shutil.which(name)
    candidates = []
    if os.name == "nt":
        base = os.path.join((environ or os.environ).get("ProgramFiles", r"C:\Program Files"), "PostgreSQL")
        if os.path.isdir(base):
            for version in sorted((v for v in os.listdir(base) if v.isdigit()), key=int, reverse=True):
                candidates.append(os.path.join(base, version, "bin", exe))
    else:
        candidates += [f"/usr/lib/postgresql/{v}/bin/{exe}" for v in range(25, 11, -1)]
    return next((c for c in candidates if os.path.isfile(c)), found)


def local_env(options):
    env = dict(os.environ)
    if options.get("db_password") and options["db_password"].lower() != "false":
        env["PGPASSWORD"] = options["db_password"]
    return env


def local_pg_args(options):
    args = ["-h", options.get("db_host") or "127.0.0.1", "-p", str(options.get("db_port") or 5432)]
    if options.get("db_user") and options["db_user"].lower() != "false":
        args += ["-U", options["db_user"]]
    return args


def local_data_dir(options):
    if options.get("data_dir"):
        return options["data_dir"]
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "OpenERP S.A.", "Odoo")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "Odoo")


def safe_member(member, source, target):
    """A tar member renamed from <source>/... to <target>/..., or None when it would leave its folder."""
    name = member.name.replace("\\", "/")
    if name.startswith("/") or ".." in name.split("/") or not (name == source or name.startswith(source + "/")):
        return None
    member.name = target + name[len(source):]
    return member


# ----------------------------------------------------------------------------- pull

def stream_to_file(command, path):
    """Run a command that writes a dump on stdout; the dump goes to `path`. Returns (returncode, stderr)."""
    with open(path, "wb") as handle:
        proc = subprocess.run(command, stdout=handle, stderr=subprocess.PIPE, check=False, timeout=7200)
    return proc.returncode, proc.stderr.decode("utf-8", "replace")


def pull_filestore(host, remote_dir, source, local_root, target, interactive=False):
    """Copy a remote filestore folder into <local_root>/<target>. Returns the number of files."""
    command = ssh_command(host, f"sudo -n tar -C {shlex.quote(os.path.dirname(remote_dir))} -cf - {shlex.quote(source)}", interactive)
    os.makedirs(local_root, exist_ok=True)
    count = 0
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            for member in archive:
                member = safe_member(member, source, target)
                if member is None or not (member.isfile() or member.isdir()):
                    continue
                archive.extract(member, local_root, filter="data") if sys.version_info >= (3, 12) else archive.extract(member, local_root)
                count += member.isfile()
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read().decode("utf-8", "replace")
        proc.wait()
    if proc.returncode not in (0, None) and count == 0:
        raise RemoteError(f"the filestore could not be copied: {explain(stderr)}")
    return count


def create_local_database(options, name):
    conn = server_ctl._connect(options)
    try:
        conn.cursor().execute(f'CREATE DATABASE "{name}" ENCODING \'UTF8\' TEMPLATE template0')
    finally:
        conn.close()


def drop_local_database(options, name):
    conn = server_ctl._connect(options)
    try:
        conn.cursor().execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        conn.close()


def restore_local(options, dump, name):
    """pg_restore into the new local database. Returns warnings (restore reports harmless ones, e.g. an extension)."""
    tool = find_pg_tool("pg_restore")
    if not tool:
        raise RemoteError("pg_restore was not found on this machine (install PostgreSQL's client tools)")
    proc = subprocess.run([tool, "--no-owner", "--no-acl", *local_pg_args(options), "-d", name, dump],
                          env=local_env(options), capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    conn = server_ctl._connect(options, name)
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.ir_module_module') IS NOT NULL")
        looks_like_odoo = cur.fetchone()[0]
    finally:
        conn.close()
    if not looks_like_odoo:
        raise RemoteError("the restore did not produce an Odoo database: " + ((proc.stderr or "").strip().splitlines() or ["no message"])[-1][:200])
    return [line for line in (proc.stderr or "").splitlines() if line.startswith("pg_restore: error")][:5]


def neutralize(python, odoo_bin, conf, name):
    proc = subprocess.run([python, odoo_bin, "neutralize", "-c", conf, "-d", name], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900, check=False)
    return proc.returncode == 0, ((proc.stderr or proc.stdout or "").strip().splitlines() or [""])[-1][:200]


def pull(args):
    """Returns the result dict; raises RemoteError for what the person must fix."""
    host, db, target = args.host, args.db, args.as_name or args.db
    for value in (db, target):
        if not NAME.fullmatch(value or ""):
            raise RemoteError(f"not a database name: {value!r}")
    options = server_ctl.read_conf(args.conf)
    total = 6
    step(1, total, "checking the local database name")
    if target in server_ctl.list_databases(options) or target in PROTECTED:
        raise RemoteError(f"a local database called {target} already exists (nothing is ever overwritten): pick another name with --as")
    tmp = tempfile.mkdtemp(prefix="hx-pull-")
    created = False
    try:
        dump = os.path.join(tmp, f"{db}.dump")
        step(2, total, f"dumping {db} on {host}")
        code, err = stream_to_file(ssh_command(host, f"{pg_prefix(args)}pg_dump -Fc --no-owner --no-acl {db}", args.interactive), dump)
        if code != 0 or not os.path.isfile(dump) or os.path.getsize(dump) == 0:
            raise RemoteError(f"the dump failed: {explain(err)}")
        size = round(os.path.getsize(dump) / 1048576)
        step(3, total, f"creating {target} and restoring {size} MB")
        create_local_database(options, target)
        created = True
        warnings = restore_local(options, dump, target)
        files = None
        if not args.no_filestore:
            step(4, total, "copying the filestore")
            remote_dir = find_filestore(host, db, args)
            if remote_dir:
                files = pull_filestore(host, remote_dir, db, os.path.join(local_data_dir(options), "filestore"), target, args.interactive)
            else:
                warnings.append("no filestore folder found on the host: attachments will be missing")
        neutral = None
        if not args.no_neutralize:
            step(5, total, "neutralizing (crons and outgoing mail off)")
            ok, message = neutralize(args.python, server_ctl._odoo_bin(args), args.conf, target)
            neutral = ok
            if not ok:
                warnings.append(f"neutralize did not finish ({message}): do not run this database online")
        step(6, total, "done")
        if args.keep_dump:
            os.makedirs(args.keep_dump, exist_ok=True)
            shutil.copy(dump, os.path.join(args.keep_dump, f"{host}_{db}_{datetime.datetime.now():%Y%m%d_%H%M%S}.dump"))
        return {"ok": True, "host": host, "source": db, "database": target, "size_mb": size, "files": files,
                "neutralized": neutral, "warnings": warnings}
    except BaseException:
        if created:   # a half-made copy is worse than none
            try:
                drop_local_database(options, target)
            except Exception:  # noqa: BLE001
                pass
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------------- push, backup, drop

def remote_exists(host, name, args):
    return any(d["name"] == name for d in list_remote(host, args)["databases"])


def push(args):
    host, source, name = args.host, args.db, args.as_name or args.db
    for value in (source, name):
        if not NAME.fullmatch(value or ""):
            raise RemoteError(f"not a database name: {value!r}")
    if name in PROTECTED:
        raise RemoteError(f"{name} is not a name to push to")
    if not args.owner or not NAME.fullmatch(args.owner):
        raise RemoteError("push needs --owner: the postgres role of the Odoo instance that will use the database")
    if not args.yes:
        raise RemoteError(f"REFUSED: this creates {name} on {host}; say --yes to confirm")
    options = server_ctl.read_conf(args.conf)
    total = 5
    step(1, total, f"checking that {name} does not exist on {host}")
    info = list_remote(host, args)
    if any(d["name"] == name for d in info["databases"]):
        raise RemoteError(f"{name} already exists on {host} (nothing is ever overwritten): pick another name with --as")
    if not any(d["owner"] == args.owner for d in info["databases"]) and not any(i["db_user"] == args.owner for i in info["instances"]):
        raise RemoteError(f"no database or instance on {host} uses the role {args.owner}: check --owner")
    tool = find_pg_tool("pg_dump")
    if not tool:
        raise RemoteError("pg_dump was not found on this machine (install PostgreSQL's client tools)")
    tmp = tempfile.mkdtemp(prefix="hx-push-")
    created = False
    prefix = pg_prefix(args)
    try:
        dump = os.path.join(tmp, f"{source}.dump")
        step(2, total, f"dumping local {source}")
        with open(dump, "wb") as handle:
            proc = subprocess.run([tool, "-Fc", "--no-owner", "--no-acl", *local_pg_args(options), source], stdout=handle,
                                  stderr=subprocess.PIPE, env=local_env(options), check=False, timeout=7200)
        if proc.returncode != 0:
            raise RemoteError("the local dump failed: " + ((proc.stderr.decode("utf-8", "replace").strip().splitlines()) or ["no message"])[-1][:200])
        size = round(os.path.getsize(dump) / 1048576)
        step(3, total, f"creating {name} on {host} and sending {size} MB")
        code, _, err = run_remote(host, f"{prefix}createdb -O {args.owner} -T template0 -E UTF8 {name}", args.interactive)
        if code != 0:
            raise RemoteError(f"the database could not be created: {explain(err)}")
        created = True
        with open(dump, "rb") as handle:
            proc = subprocess.run(ssh_command(host, f"{prefix}pg_restore --no-owner --no-acl --role={args.owner} -d {name}", args.interactive),
                                  stdin=handle, capture_output=True, check=False, timeout=7200)
        restore_err = proc.stderr.decode("utf-8", "replace")
        code, out, _ = run_remote(host, f"{prefix}psql -At -d {name} -c \"SELECT to_regclass('public.ir_module_module') IS NOT NULL\"", args.interactive)
        if code != 0 or out.strip() != "t":
            raise RemoteError(f"the restore on the host did not produce an Odoo database: {explain(restore_err)}")
        step(4, total, "sending the filestore" if args.instance else "no instance given: filestore not sent")
        files = None
        if args.instance:
            files = push_filestore(args, options, source, name)
        step(5, total, "done")
        return {"ok": True, "host": host, "source": source, "database": name, "size_mb": size, "files": files,
                "warnings": ([] if args.instance else ["the filestore was not sent (give --instance <conf> to send it)"])}
    except BaseException:
        if created:
            run_remote(host, f"{prefix}dropdb {name}", args.interactive)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def push_filestore(args, options, source, name):
    root = os.path.join(local_data_dir(options), "filestore", source)
    if not os.path.isdir(root):
        return 0
    if not re.fullmatch(r"/etc/[\w./-]+\.conf", args.instance):
        raise RemoteError(f"not an instance conf path: {args.instance!r}")
    script = (f"d=$( (cat {args.instance} 2>/dev/null || sudo -n cat {args.instance}) | sed -n 's/^data_dir *= *//p' | head -1); "
              f"[ -n \"$d\" ] && sudo -n test -d \"$d/filestore\" && sudo -n mkdir -p \"$d/filestore/{name}\" && "
              f"sudo -n tar -C \"$d/filestore/{name}\" -xf - && sudo -n chown -R --reference=\"$d/filestore\" \"$d/filestore/{name}\"")
    proc = subprocess.Popen(ssh_command(args.host, script, args.interactive), stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    count = 0
    with tarfile.open(fileobj=proc.stdin, mode="w|") as archive:
        for folder, _, names in os.walk(root):
            for filename in names:
                path = os.path.join(folder, filename)
                archive.add(path, arcname=os.path.relpath(path, root).replace(os.sep, "/"))
                count += 1
    proc.stdin.close()
    stderr = proc.stderr.read().decode("utf-8", "replace")
    proc.wait()
    if proc.returncode != 0:
        raise RemoteError(f"the filestore could not be sent: {explain(stderr)}")
    return count


def backup(args):
    host, db = args.host, args.db
    if not NAME.fullmatch(db or ""):
        raise RemoteError(f"not a database name: {db!r}")
    if not args.dir:
        raise RemoteError("backup needs a folder (--dir)")
    os.makedirs(args.dir, exist_ok=True)
    target = os.path.join(args.dir, f"{host}_{db}_{datetime.datetime.now():%Y%m%d_%H%M%S}.dump")
    code, err = stream_to_file(ssh_command(host, f"{pg_prefix(args)}pg_dump -Fc --no-owner --no-acl {db}", args.interactive), target)
    if code != 0 or not os.path.isfile(target) or os.path.getsize(target) == 0:
        if os.path.isfile(target):
            os.remove(target)
        raise RemoteError(f"the dump failed: {explain(err)}")
    return {"ok": True, "host": host, "database": db, "path": target, "size_mb": round(os.path.getsize(target) / 1048576)}


def drop(args):
    host, db = args.host, args.db
    if not NAME.fullmatch(db or "") or db in PROTECTED:
        raise RemoteError(f"{db!r} is not a database to drop")
    if args.confirm != f"{host}/{db}":
        raise RemoteError(f"REFUSED: to delete {db} on {host}, say --confirm {host}/{db}")
    info = list_remote(host, args)
    row = next((d for d in info["databases"] if d["name"] == db), None)
    if not row:
        raise RemoteError(f"{db} does not exist on {host}")
    if row["sessions"] and not args.force:
        raise RemoteError(f"REFUSED: {row['sessions']} session(s) are connected to {db} on {host} (a running instance?). Stop it first, or --force.")
    served = [i["conf"] for i in info["instances"] if i["db_name"] == db]
    if served and not args.force:
        raise RemoteError(f"REFUSED: {', '.join(served)} is configured to serve {db}. Change it first, or --force.")
    saved = None
    if args.backup_dir:
        saved = backup(argparse_namespace(args, dir=args.backup_dir))["path"]
    code, _, err = run_remote(host, f"{pg_prefix(args)}dropdb {db}", args.interactive)
    if code != 0:
        raise RemoteError(f"the drop failed: {explain(err)}")
    return {"ok": True, "host": host, "database": db, "backup": saved}


def argparse_namespace(args, **changes):
    import argparse
    values = dict(vars(args))
    values.update(changes)
    return argparse.Namespace(**values)


# ----------------------------------------------------------------------------- the CLI

def _answer(args, result):
    if args.json:
        print(json.dumps(result))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")


def _guard(function):
    def run(args):
        try:
            result = function(args)
        except RemoteError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}) if args.json else f"ERROR: {exc}")
            return 2 if str(exc).startswith("REFUSED") else 1
        except subprocess.TimeoutExpired:
            print(json.dumps({"ok": False, "error": "timed out"}) if args.json else "ERROR: timed out")
            return 1
        _answer(args, result)
        return 0
    return run


def cmd_hosts(args):
    hosts = ssh_hosts()
    print(json.dumps({"hosts": hosts}) if args.json else "\n".join(f"{h['name']:20}{h['user'] or '-':12}{h['hostname'] or '-'}" for h in hosts))
    return 0


def cmd_check(args):
    result = check(args.host, args.interactive)
    print(json.dumps(result) if args.json else f"{result['host']}: {result['message']}")
    return 0 if result["ok"] else 1


def add_commands(sub, project_args):
    """Register the `remote` family on the server parser."""
    def common(parser, conf=True):
        if conf:
            project_args(parser)
        parser.add_argument("--host", required=True, help="a host of ~/.ssh/config")
        parser.add_argument("--interactive", action="store_true", help="let ssh ask (a password); run it in a terminal")
        parser.add_argument("--no-sudo", action="store_true", help="the ssh user is itself a postgres role")
        parser.add_argument("--sudo-user", default=None, help="the user the postgres commands run as (default postgres)")
        parser.add_argument("--json", action="store_true")

    hosts = sub.add_parser("remote-hosts", help="the hosts of ~/.ssh/config")
    hosts.add_argument("--json", action="store_true")
    hosts.set_defaults(run=cmd_hosts)

    chk = sub.add_parser("remote-check", help="can the host be reached without a question?")
    common(chk, conf=False)
    chk.set_defaults(run=cmd_check)

    lst = sub.add_parser("remote-list", help="a host's databases and Odoo instances")
    common(lst, conf=False)
    lst.set_defaults(run=_guard(lambda a: list_remote(a.host, a)))

    pl = sub.add_parser("remote-pull", help="a remote database into a NEW local one (neutralized)")
    common(pl)
    pl.add_argument("-d", "--db", required=True)
    pl.add_argument("--as", dest="as_name", help="the local name (default: the same)")
    pl.add_argument("--no-filestore", action="store_true")
    pl.add_argument("--no-neutralize", action="store_true", help="keep crons and outgoing mail ON (never for a copy of production)")
    pl.add_argument("--keep-dump", help="also keep the dump in this folder")
    pl.set_defaults(run=_guard(pull))

    ps = sub.add_parser("remote-push", help="a local database to the host as a NEW one")
    common(ps)
    ps.add_argument("-d", "--db", required=True, help="the local database")
    ps.add_argument("--as", dest="as_name", help="the name on the host (default: the same)")
    ps.add_argument("--owner", help="the postgres role that will own it (that of the instance using it)")
    ps.add_argument("--instance", help="the instance's .conf on the host: its filestore gets the files")
    ps.add_argument("--yes", action="store_true")
    ps.set_defaults(run=_guard(push))

    bk = sub.add_parser("remote-backup", help="a remote database into a file here")
    common(bk, conf=False)
    bk.add_argument("-d", "--db", required=True)
    bk.add_argument("--dir", required=True)
    bk.set_defaults(run=_guard(backup))

    dr = sub.add_parser("remote-drop", help="delete a remote database (--confirm host/db)")
    common(dr, conf=False)
    dr.add_argument("-d", "--db", required=True)
    dr.add_argument("--confirm", help="host/database, typed again")
    dr.add_argument("--force", action="store_true")
    dr.add_argument("--backup-dir", help="dump into this folder first, and stop if that fails")
    dr.set_defaults(run=_guard(drop))
