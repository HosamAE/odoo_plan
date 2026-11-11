"""Run, upgrade and judge an Odoo server: `odoo-ai server <dbs|command|upgrade|health> ...`.

The extension asks four things of a project's server and this module answers them, so that the answer is
tested in Python and the extension stays a thin screen:

  dbs      which databases can I pick                         (names only; the password never leaves the conf)
  command  the exact command line for these choices           (database, -u / -i, port, dev mode)
  upgrade  run `-u all` or `-u a,b` on ONE database and say what broke - refusing when another server is using it
  health   read a server log and say: starting, healthy, problems or failed - and what to do about it
  drift    which stored fields the code has and the database lacks - the module that needs `-u`

THE UPGRADE REFUSES A DATABASE SOMEONE ELSE IS USING. Found on 2026-11-10: `-u all` against a database a
running server was also serving died with "could not serialize access due to concurrent update", which looks
like a broken module and is not. The check counts the other connections to that database first.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

from server_scan import own_frame as _own_frame
from server_scan import scan_log

#: What Odoo writes at the start of every log line: date time pid LEVEL database logger: message
LOG_LINE = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ \d+ (?P<level>[A-Z]+) (?P<db>\S+) (?P<logger>\S+): (?P<msg>.*)$")
LOADING = re.compile(r"odoo\.modules\.loading: loading (?P<module>[\w]+)/(?P<file>\S+)")
HTTP_500 = re.compile(r'werkzeug: .*" 500 - ')
#: A frame of a traceback, and the exception line that ends it (not indented, unlike the frames above it)
FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)')
EXCEPTION_LINE = re.compile(r"^(?P<name>[A-Za-z_][\w.]*)(?::\s*(?P<text>.*))?$")

#: Warnings that are the machine's, not the project's. They are counted nowhere.
NOISE = ("pdfminer.six", "db_name reads", "http-interface", "Transient module states were reset")
#: The two lines that mean the server is ready to be used.
READY = ("HTTP service (werkzeug) running on", "Modules loaded.", "odoo.modules.loading: Modules loaded")

#: What a known error means and what to do. Matched against the error text; the first hit wins.
HINTS = (
    (re.compile(r"UndefinedColumn|column .* does not exist"),
     "The database is older than the code: a field exists in the code and its column does not in the database. "
     "Upgrade the module that owns the field (-u <module>), or all of them."),
    (re.compile(r"could not serialize access due to concurrent update"),
     "Another process is writing to this database at the same moment (usually a second Odoo server). "
     "Stop the other server and run again - this is not a broken module."),
    (re.compile(r"database \"?[\w-]+\"? does not exist|FATAL:\s+database"),
     "That database does not exist. Pick one from the list or create it with -i base."),
    (re.compile(r"collations with different collate and ctype"),
     "A database in this PostgreSQL cluster has a collation this platform cannot open (its collate and ctype "
     "differ), and the database selector tries every database, so one bad database breaks the page for all. "
     "Start the server with a database chosen (-d <name>) instead of using the selector, or drop and recreate "
     "that database: `odoo-ai server dbs` names it."),
    (re.compile(r"password authentication failed|no password supplied"),
     "PostgreSQL refused the login. Check db_user / db_password in the project's .conf."),
    (re.compile(r"Address already in use|only one usage of each socket|WinError 10048"),
     "The port is taken by another process. Stop that server or choose another port."),
    (re.compile(r"ModuleNotFoundError|ImportError"),
     "A Python package is missing in the interpreter that started the server. Use the project's venv."),
    (re.compile(r"ParseError|while parsing"),
     "A data file (XML/CSV) of a module did not load. The file named above is the one."),
)


# ----------------------------------------------------------------------------- the conf

def read_conf(path):
    """The options of an Odoo .conf as a dict. The password is read but never returned by `dbs` or `command`."""
    options = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                match = re.match(r"^\s*([A-Za-z_]+)\s*=\s*(.*?)\s*$", line)
                if match and not line.lstrip().startswith(("#", ";")):
                    options[match.group(1)] = match.group(2)
    except OSError:
        pass
    return options


def conf_port(options):
    for key in ("http_port", "xmlrpc_port"):
        if options.get(key, "").isdigit():
            return int(options[key])
    return 8069


def _connect(options, database="postgres"):
    import psycopg2   # here, not at the top: every other command works on a machine with no Odoo packages
    kwargs = {"dbname": database, "host": options.get("db_host") or "127.0.0.1",
              "port": int(options.get("db_port") or 5432)}
    for conf_key, kw in (("db_user", "user"), ("db_password", "password")):
        if options.get(conf_key) and options[conf_key].lower() != "false":
            kwargs[kw] = options[conf_key]
    conn = psycopg2.connect(**kwargs)
    conn.autocommit = True
    return conn


def database_report(options):
    """({usable names}, [{name, reason}]): the databases the project's user can open, and the ones it cannot.

    One database whose collate and ctype differ cannot be opened on Windows, and Odoo's database selector
    opens every database to list them: that one breaks the page for the rest (2026-11-10: `test_db`).
    Only a database whose two values differ is tried, so the report costs one query in the usual case."""
    conn = _connect(options)
    try:
        cur = conn.cursor()
        cur.execute("SELECT datname, datcollate, datctype FROM pg_database "
                    "WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY datname")
        rows = cur.fetchall()
    finally:
        conn.close()
    usable, unusable = [], []
    for name, collate, ctype in rows:
        if collate == ctype:
            usable.append(name)
            continue
        try:
            _connect(options, name).close()
            usable.append(name)
        except Exception:  # noqa: BLE001 - any refusal means "cannot be opened"
            unusable.append({"name": name,
                             "reason": f"collate {collate!r} and ctype {ctype!r} differ; this platform cannot open it"})
    return usable, unusable


def list_databases(options):
    """Names of the databases the project's PostgreSQL user can open, templates and `postgres` left out."""
    return database_report(options)[0]


def other_connections(options, database):
    """How many OTHER sessions are connected to a database right now (a running server holds several)."""
    conn = _connect(options)
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database,))
        return cur.fetchone()[0]
    finally:
        conn.close()


# ----------------------------------------------------------------------------- the command

def _modules(value):
    names = [m.strip() for m in (value or "").split(",") if m.strip()]
    for name in names:
        if name != "all" and not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError(f"not a module name: {name!r}")
    return names


def build_command(python, odoo_bin, conf, database=None, update=None, install=None, port=None,
                  dev=False, stop_after_init=False, extra=()):
    """The argument list that starts Odoo with these choices. Never a shell string: nothing here is quoted by hand."""
    command = [python, odoo_bin, "-c", conf]
    if database:
        if not re.fullmatch(r"[\w.-]+", database):
            raise ValueError(f"not a database name: {database!r}")
        command += ["-d", database]
    for flag, value in (("-u", update), ("-i", install)):
        names = _modules(value)
        if names:
            if not database:
                raise ValueError(f"{flag} needs a database")
            command += [flag, ",".join(names)]
    if port:
        command += [f"--http-port={int(port)}"]
    if dev:
        command += ["--dev=all"]
    if stop_after_init:
        command += ["--stop-after-init"]
    return command + list(extra)


# ----------------------------------------------------------------------------- the health of a log

def analyze_log(text, roots=(), database=None, line_offset=0):
    """Read an Odoo log and judge it.

    state: `failed` (it could not start or load), `problems` (running, with errors or HTTP 500s),
    `healthy` (ready, nothing wrong) or `starting` (not ready yet, nothing wrong so far).

    `roots` are folders of the project's own code. A traceback is followed down to its deepest frame inside one
    of them, and that file and line are the issue's `location` - the place an editor should open.

    `database` is the one the server was started for. A server started WITHOUT one lets Odoo's background jobs
    visit every database in the cluster, and each old database fails to load on its own: those failures are
    reported as `problems` and named, not as the server having failed. With a database given, only its own
    errors (and the ones logged without a database) count."""
    errors, criticals, warnings, seen = [], [], 0, set()
    http_500, ready = 0, False
    last_loading = None
    first_loading = None
    pending_traceback = False
    frames = []
    last_issue = None
    last_level = "INFO"
    last_db = "?"
    for raw in text.splitlines():
        line = raw.rstrip()
        match = LOG_LINE.match(line)
        if match:
            pending_traceback = False
            level, message = match["level"], match["msg"]
            last_level = level
            last_db = match["db"]
            loading = LOADING.search(line)
            if loading:
                last_loading = (loading["module"], loading["file"])
            if any(token in line for token in READY):
                ready = True
            if HTTP_500.search(line):
                http_500 += 1
            if level in ("ERROR", "CRITICAL"):
                key = (match["db"], message[:160])   # the same words from another database are another problem
                if key not in seen:
                    seen.add(key)
                    entry = {"level": level, "message": message[:300], "module": None, "file": None,
                             "location": None, "db": match["db"]}
                    if last_loading:
                        entry["module"], entry["file"] = last_loading
                    (criticals if level == "CRITICAL" else errors).append(entry)
                    last_issue = entry
                    first_loading = first_loading or last_loading
            elif level == "WARNING" and not any(token in line for token in NOISE):
                warnings += 1
        elif line.startswith("ERROR:") and last_issue is not None:
            # PostgreSQL's own words follow a "bad query" line on the next line; they are what explains it
            last_issue["message"] = f"{last_issue['message']} | {line}"[:300]
        elif line.startswith("Traceback"):
            pending_traceback = True
            frames = []
        elif pending_traceback:
            frame = FRAME.match(line)
            if frame:
                frames.append({"file": frame["file"], "line": int(frame["line"])})
                continue
            found = EXCEPTION_LINE.match(line) if line and not line[0].isspace() else None
            if found:
                pending_traceback = False
                if last_level not in ("ERROR", "CRITICAL"):
                    # Odoo attaches tracebacks to warnings it tolerates (a <delete> of a record that is not
                    # there, a skipped menu). The record's own level decides, not the presence of a traceback.
                    warnings += 1
                    continue
                text_ = f"{found['name']}: {found['text'] or ''}".rstrip(": ")[:300]
                if (last_db, text_[:160]) not in seen:
                    seen.add((last_db, text_[:160]))
                    entry = {"level": "ERROR", "message": text_, "module": None, "file": None,
                             "location": _own_frame(frames, roots), "db": last_db}
                    if last_loading:
                        entry["module"], entry["file"] = last_loading
                    errors.append(entry)
    other_databases = []
    if database:
        mine = lambda issue: issue["db"] in (database, "?")  # noqa: E731
        other_databases = sorted({i["db"] for i in criticals + errors if not mine(i)})
        criticals = [i for i in criticals if mine(i)]
        errors = [i for i in errors if mine(i)]
    # The generic "it failed" lines say nothing; the exception behind them does. Specific ones come first.
    registry_lines = ("Failed to load registry", "Failed to initialize database")
    generic = registry_lines + ("Exception during",)
    all_issues = sorted(criticals + errors, key=lambda i: i["message"].startswith(generic))
    registry_failed = {i["db"] for i in all_issues if i["message"].startswith(registry_lines)}
    if database:
        failed = bool(criticals) or bool(registry_failed)
    else:
        # nothing chosen and the server is answering: a database that will not load is a problem, not the end
        failed = (bool(criticals) or bool(registry_failed)) and not ready
    if failed:
        state = "failed"
    elif all_issues or http_500:
        state = "problems"
    elif ready:
        state = "healthy"
    else:
        state = "starting"
    failing_databases = sorted({i["db"] for i in all_issues if i["db"] != "?"})
    hint = None
    if not database and len(failing_databases) > 1:
        hint = (f"No database was chosen, so Odoo's background jobs visit every database in the cluster, and "
                f"{len(failing_databases)} of them fail to load ({', '.join(failing_databases[:6])}"
                f"{' ...' if len(failing_databases) > 6 else ''}). Start with -d <database> so only that one is "
                "loaded, and upgrade or drop the old ones.")
    for issue in all_issues:
        if hint:
            break
        for pattern, advice in HINTS:
            if pattern.search(issue["message"]):
                hint = advice
                break
    scan = scan_log(text, roots, line_offset)
    if database:
        keep = scan["items"]
        scan["items"] = [i for i in keep if i["db"] in (database, "?")]
    return {"state": state, "errors": len(errors), "critical": len(criticals), "http_500": http_500,
            "items": scan["items"], "marks": scan["marks"], "progress": scan["progress"],
            "warnings": warnings, "ready": ready, "hint": hint, "databases_failing": failing_databases,
            "other_databases": other_databases,
            "first_issue": (all_issues[0] if all_issues else None), "issues": all_issues[:20]}


#: Run inside `odoo-bin shell`: every stored field the CODE declares, against the columns the DATABASE has.
DRIFT_SCRIPT = """import json
from odoo.tools.sql import column_exists, table_exists
missing = {}
for name, model in env.registry.models.items():
    if getattr(model, "_abstract", False) or not getattr(model, "_auto", True):
        continue
    if not table_exists(env.cr, model._table):
        missing.setdefault("?", []).append(name + " (table " + model._table + ")")
        continue
    for fname, field in model._fields.items():
        if field.store and field.column_type and not column_exists(env.cr, model._table, fname):
            missing.setdefault(getattr(field, "_module", None) or "?", []).append(name + "." + fname)
print("HX_DRIFT_JSON:" + json.dumps(missing))
"""
DRIFT_MARK = "HX_DRIFT_JSON:"


def parse_drift(output):
    """{module: [model.field, ...]} from the shell's output, or None when the script never answered."""
    for line in output.splitlines():
        if line.startswith(DRIFT_MARK):
            try:
                return json.loads(line[len(DRIFT_MARK):])
            except ValueError:
                return None
    return None


def check_drift(python, odoo_bin, conf, database):
    """Fields the code has and the database lacks, by the module that owns them: the "-u" the database needs."""
    command = build_command(python, odoo_bin, conf, database, extra=["--log-level=warn"])
    command.insert(2, "shell")   # odoo-bin shell -c conf -d db
    proc = subprocess.run(command, input=DRIFT_SCRIPT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=600, check=False)
    return parse_drift(proc.stdout + proc.stderr)


def drift_advice(missing):
    """The one line that says what to run."""
    modules = sorted(m for m in missing if m != "?")
    if not missing:
        return "The database matches the code."
    if "?" in missing and not modules:
        return "Fields are missing and their module is unknown: upgrade all (-u all)."
    return "Upgrade: -u " + ",".join(modules)


def check_http(port, timeout=2):
    """Does the server answer /web/health? True/False - only the answer, never the page."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/web/health", timeout=timeout) as reply:
            return reply.status == 200
    except Exception:  # noqa: BLE001 - any failure is "no"
        return False


def summarize(result):
    """The one-screen description of analyze_log's answer."""
    labels = {"healthy": "HEALTHY", "starting": "STARTING", "problems": "PROBLEMS", "failed": "FAILED"}
    lines = [f"Server: {labels[result['state']]} - {result['critical']} critical, {result['errors']} error(s), "
             f"{result['http_500']} HTTP 500, {result['warnings']} warning(s)"]
    issue = result["first_issue"]
    if issue:
        where = f" (while loading {issue['module']}/{issue['file']})" if issue["module"] else ""
        lines.append(f"First problem{where}: {issue['message']}")
        if issue.get("location"):
            lines.append(f"In your code: {issue['location']['file']}:{issue['location']['line']}")
    if len(result.get("databases_failing", [])) > 1:
        lines.append(f"Databases that fail to load: {', '.join(result['databases_failing'][:8])}")
    if result.get("other_databases"):
        lines.append(f"(errors of other databases left out: {', '.join(result['other_databases'][:8])})")
    if result["hint"]:
        lines.append(f"What it means: {result['hint']}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- the CLI

def _project_args(parser):
    parser.add_argument("--conf", required=True, help="the project's .conf")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--odoo-bin", default=None)


def find_odoo_bin(conf, environ=None):
    """Where odoo-bin is, found from the conf's own place - never from a fixed drive or folder.

    A project's conf sits in <root>/ProjectsOdooNN/<project> (or <root>/odooNN/projects/<project>), and the
    server in <root>/odooNN/odoo/odoo-bin. ODOO_ROOT, when set, is tried too. Returns "odoo-bin" (the PATH's)
    when nothing is found."""
    environ = os.environ if environ is None else environ
    text = os.path.abspath(conf).replace(chr(92), "/")
    found = re.search(r"^(.*?)/(?:ProjectsOdoo|odoo)(\d\d)(?:/|$)", text, re.I)
    candidates = []
    if found:
        candidates.append(os.path.join(found.group(1), f"odoo{found.group(2)}", "odoo", "odoo-bin"))
        if environ.get("ODOO_ROOT"):
            candidates.append(os.path.join(environ["ODOO_ROOT"], f"odoo{found.group(2)}", "odoo", "odoo-bin"))
    return next((os.path.normpath(c) for c in candidates if os.path.exists(c)), "odoo-bin")


def _odoo_bin(args):
    return args.odoo_bin or find_odoo_bin(args.conf)


def cmd_dbs(args):
    options = read_conf(args.conf)
    try:
        names, unusable = database_report(options)
    except Exception as exc:  # noqa: BLE001 - say why, without the connection string
        print(json.dumps({"databases": [], "unusable": [], "error": type(exc).__name__}) if args.json
              else f"cannot list databases: {type(exc).__name__}")
        return 1
    conf_db = options.get("db_name") if options.get("db_name", "").lower() != "false" else None
    if args.json:
        print(json.dumps({"databases": names, "unusable": unusable, "conf_db": conf_db}))
    else:
        print("\n".join(names))
        for bad in unusable:
            print(f"(cannot be opened: {bad['name']} - {bad['reason']})")
    return 0


def cmd_command(args):
    command = build_command(args.python, _odoo_bin(args), args.conf, args.db, args.update, args.install,
                            args.port, args.dev, args.stop_after_init)
    print(json.dumps(command) if args.json else subprocess.list2cmdline(command))
    return 0


def cmd_upgrade(args):
    options = read_conf(args.conf)
    if not args.db:
        print("upgrade needs a database (-d)")
        return 2
    if not args.force:
        try:
            others = other_connections(options, args.db)
        except Exception as exc:  # noqa: BLE001
            print(f"cannot check the database's connections ({type(exc).__name__}); use --force to go on")
            return 2
        if others:
            print(f"REFUSED: {others} other session(s) are connected to {args.db}. A running Odoo server is the "
                  "usual one: stop it first (a concurrent upgrade fails with 'could not serialize access'), "
                  "or use --force.")
            return 3
    command = build_command(args.python, _odoo_bin(args), args.conf, args.db, args.modules or "all",
                            None, None, False, True)
    print("Running:", subprocess.list2cmdline(command), flush=True)
    log = args.log
    handle = open(log, "w", encoding="utf-8") if log else None
    collected = []
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        for line in proc.stdout:
            collected.append(line)
            if handle:
                handle.write(line)
                handle.flush()
            if not args.quiet:
                print(line, end="")
        proc.wait()
    finally:
        if handle:
            handle.close()
    result = analyze_log("".join(collected), roots=args.root or (), database=args.db)
    print("\n" + summarize(result))
    if proc.returncode != 0 and result["state"] in ("healthy", "starting"):
        print(f"(Odoo exited with code {proc.returncode} without logging an error)")
    return 0 if proc.returncode == 0 and result["state"] in ("healthy", "starting") else 1


def cmd_drift(args):
    if not args.db:
        print("drift needs a database (-d)")
        return 2
    missing = check_drift(args.python, _odoo_bin(args), args.conf, args.db)
    if missing is None:
        print(json.dumps({"error": "no answer"}) if args.json else
              f"Could not read the database {args.db}: the check never answered (does it exist, is the server stopped?)")
        return 2
    if args.json:
        print(json.dumps({"drift": missing, "advice": drift_advice(missing)}))
    else:
        print(drift_advice(missing))
        for module in sorted(missing):
            print(f"  {module}: {', '.join(missing[module][:12])}{' ...' if len(missing[module]) > 12 else ''}")
    return 1 if missing else 0


def cmd_health(args):
    try:
        with open(args.log, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        text = ""
    offset = 0
    if len(text) > 4_000_000:   # a big log: judge its tail, but say which line the tail starts at
        newline = chr(10)
        cut = text.index(newline, len(text) - 4_000_000) + 1
        offset, text = text[:cut].count(newline), text[cut:]
    result = analyze_log(text, roots=args.root or (), database=args.db, line_offset=offset)
    if args.port:
        answers = check_http(args.port)
        result["http_answers"] = answers
        if result["state"] == "starting" and answers:
            result["state"] = "healthy"
        if result["state"] == "healthy" and not answers:
            result["state"] = "starting"
    if args.json:
        print(json.dumps(result))
    else:
        print(summarize(result))
    return {"healthy": 0, "starting": 0, "problems": 1, "failed": 2}[result["state"]]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="odoo-ai server", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)

    dbs = sub.add_parser("dbs", help="the databases to pick from")
    _project_args(dbs)
    dbs.add_argument("--json", action="store_true")
    dbs.set_defaults(run=cmd_dbs)

    command = sub.add_parser("command", help="the command line for these choices")
    _project_args(command)
    command.add_argument("-d", "--db")
    command.add_argument("-u", "--update", help="all, or module,module")
    command.add_argument("-i", "--install", help="module,module")
    command.add_argument("--port", type=int)
    command.add_argument("--dev", action="store_true")
    command.add_argument("--stop-after-init", action="store_true")
    command.add_argument("--json", action="store_true")
    command.set_defaults(run=cmd_command)

    upgrade = sub.add_parser("upgrade", help="-u all, or -u a,b, on one database; refuses a database in use")
    _project_args(upgrade)
    upgrade.add_argument("-d", "--db", required=False)
    upgrade.add_argument("-m", "--modules", help="all (default) or module,module")
    upgrade.add_argument("--force", action="store_true", help="go on although other sessions use the database")
    upgrade.add_argument("--log", help="also write the output to this file")
    upgrade.add_argument("--quiet", action="store_true", help="print only the verdict")
    upgrade.add_argument("--root", action="append", help="a folder of the project's own code (repeatable)")
    upgrade.set_defaults(run=cmd_upgrade)

    drift = sub.add_parser("drift", help="fields the code has and the database lacks: which module needs -u")
    _project_args(drift)
    drift.add_argument("-d", "--db")
    drift.add_argument("--json", action="store_true")
    drift.set_defaults(run=cmd_drift)

    health = sub.add_parser("health", help="judge a server log")
    health.add_argument("--log", required=True)
    health.add_argument("--port", type=int, help="also ask /web/health on this port")
    health.add_argument("--root", action="append", help="a folder of the project's own code (repeatable)")
    health.add_argument("--db", help="the database the server was started for: other databases' errors are set aside")
    health.add_argument("--json", action="store_true")
    health.set_defaults(run=cmd_health)

    import server_dbs   # here: server_dbs imports this module, so it is loaded once this one is complete

    inventory = sub.add_parser("inventory", help="every database: size, version, modules, last login, can it be opened")
    _project_args(inventory)
    inventory.add_argument("--json", action="store_true")
    inventory.set_defaults(run=server_dbs.cmd_inventory)

    modules = sub.add_parser("modules", help="one database's modules against the code on disk")
    _project_args(modules)
    modules.add_argument("-d", "--db")
    modules.add_argument("--json", action="store_true")
    modules.set_defaults(run=server_dbs.cmd_modules)

    backup = sub.add_parser("backup", help="dump a database with its filestore into a folder")
    _project_args(backup)
    backup.add_argument("-d", "--db")
    backup.add_argument("--dir", help="the folder the dump goes to")
    backup.add_argument("--json", action="store_true")
    backup.set_defaults(run=server_dbs.cmd_backup)

    drop = sub.add_parser("drop", help="delete a database (needs --yes; refuses one in use; --backup-dir backs up first)")
    _project_args(drop)
    drop.add_argument("-d", "--db")
    drop.add_argument("--yes", action="store_true", help="confirm: this deletes the database and its filestore")
    drop.add_argument("--force", action="store_true", help="go on although other sessions use the database")
    drop.add_argument("--backup-dir", help="back up into this folder first, and stop if the backup fails")
    drop.set_defaults(run=server_dbs.cmd_drop)

    import remote_dbs
    remote_dbs.add_commands(sub, _project_args)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
