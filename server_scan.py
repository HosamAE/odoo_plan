"""Read an Odoo log for what a person needs to FIND and FOLLOW: every error and warning with its line, and how far
a start or an upgrade has got.

`server_ctl.analyze_log` judges a log (healthy / problems / failed). This module lists it: the extension shows the
list as a tree to walk through, opens the log at a line, opens the project's own file when a traceback ends in it,
and paints the error and warning lines red and yellow. Line numbers are the file's own, so a tail read of a big
log passes the number of lines it left out (`line_offset`).

PROGRESS comes from two lines Odoo already writes for every module:
    odoo.modules.loading: Loading module <name> (<i>/<N>)         where it is, out of how many
    odoo.registry: module <name>: creating or updating database tables   which modules were really upgraded
"""
import os
import re
from datetime import datetime

LOG_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \d+ (?P<level>[A-Z]+) (?P<db>\S+) (?P<logger>\S+): (?P<msg>.*)$")
FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)')
EXCEPTION_LINE = re.compile(r"^(?P<name>[A-Za-z_][\w.]*)(?::\s*(?P<text>.*))?$")
PASS_START = re.compile(r"odoo\.modules\.loading: loading (?P<n>\d+) modules\.\.\.")
LOADING_MODULE = re.compile(r"odoo\.modules\.loading: Loading module (?P<module>\w+) \((?P<i>\d+)/(?P<n>\d+)\)")
LOADING_FILE = re.compile(r"odoo\.modules\.loading: loading (?P<module>\w+)/(?P<file>\S+)")
LOADED = re.compile(r"odoo\.modules\.loading: (?P<n>\d+) modules loaded in (?P<seconds>[\d.]+)s")
TABLES = re.compile(r"odoo\.registry: module (?P<module>\w+): creating or updating database tables")

#: Warnings that are the machine's, not the project's. They are not listed and not painted.
NOISE = ("pdfminer.six", "db_name reads", "http-interface", "Transient module states were reset")
MAX_ITEMS = 1500
MAX_LINES_PER_ITEM = 25


def _time(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def own_frame(frames, roots):
    """The deepest frame of a traceback that lies in the project's own code, or None."""
    wanted = [os.path.normcase(os.path.abspath(r)) for r in roots if r]
    for frame in reversed(frames):
        path = os.path.normcase(os.path.abspath(frame["file"]))
        if any(path == root or path.startswith(root + os.sep) for root in wanted):
            return frame
    return None


def scan_log(text, roots=(), line_offset=0):
    """{"items": [...], "marks": {"error": [lines], "warning": [lines]}, "progress": {...} or None}.

    An item is one distinct problem: {kind, line, lines, count, time, db, logger, message, detail, module, file,
    location}. The same words from the same database are one item with a count; from another database they are
    another. `detail` is the root cause when the record carries a traceback or a PostgreSQL message."""
    items, order = {}, []
    marks = {"error": [], "warning": []}
    passes = []
    updated = []
    current = None          # the item whose record we are inside
    kind = None             # "error" / "warning" / None: the colour of the record we are inside
    frames, in_traceback = [], False
    last_loading = None
    last_ts = None
    for number, raw in enumerate(text.splitlines(), start=1 + line_offset):
        line = raw.rstrip()
        match = LOG_LINE.match(line)
        if match:
            in_traceback, frames = False, []
            level, message, db, ts = match["level"], match["msg"], match["db"], match["ts"]
            last_ts = ts
            kind = "error" if level in ("ERROR", "CRITICAL") else "warning" if level == "WARNING" else None
            current = None

            started = PASS_START.search(line)
            if started:
                passes.append({"total": int(started["n"]), "start": ts, "index": 0, "module": None, "file": None,
                               "since": ts, "durations": {}, "finished": False, "seconds": None})
            loading = LOADING_MODULE.search(line)
            if loading:
                if not passes or passes[-1]["finished"] or passes[-1]["total"] != int(loading["n"]):
                    passes.append({"total": int(loading["n"]), "start": ts, "index": 0, "module": None,
                                   "file": None, "since": ts, "durations": {}, "finished": False, "seconds": None})
                current_pass = passes[-1]
                if current_pass["module"]:
                    spent = (_time(ts) - _time(current_pass["since"])).total_seconds()
                    current_pass["durations"][current_pass["module"]] = spent
                current_pass.update(index=int(loading["i"]), module=loading["module"], since=ts, file=None)
            file_line = LOADING_FILE.search(line)
            if file_line:
                last_loading = (file_line["module"], file_line["file"])
                if passes and not passes[-1]["finished"]:
                    passes[-1]["file"] = file_line["file"]
            loaded = LOADED.search(line)
            if loaded and passes:
                passes[-1].update(finished=True, seconds=float(loaded["seconds"]))
            tables = TABLES.search(line)
            if tables:
                updated.append(tables["module"])

            if kind and not (kind == "warning" and any(token in line for token in NOISE)):
                marks[kind].append(number)
                key = (kind, db, message[:160])
                if key in items:
                    items[key]["count"] += 1
                    if len(items[key]["lines"]) < MAX_LINES_PER_ITEM:
                        items[key]["lines"].append(number)
                else:
                    item = {"kind": kind, "line": number, "lines": [number], "count": 1, "time": ts, "db": db,
                            "logger": match["logger"], "message": message[:300], "detail": None,
                            "module": last_loading[0] if last_loading else None,
                            "file": last_loading[1] if last_loading else None, "location": None}
                    items[key] = item
                    order.append(key)
                current = items[key]
            elif kind:
                kind = None   # noise: neither listed nor painted, and neither is its traceback
            continue
        # a continuation of the record we are inside: a traceback, a PostgreSQL message, a wrapped line
        if kind and line.strip():
            marks[kind].append(number)
        if current is None:
            continue
        if line.startswith("Traceback"):
            in_traceback, frames = True, []
        elif in_traceback:
            frame = FRAME.match(line)
            if frame:
                frames.append({"file": frame["file"], "line": int(frame["line"])})
            elif line and not line[0].isspace():
                found = EXCEPTION_LINE.match(line)
                if found:
                    in_traceback = False
                    current["detail"] = f"{found['name']}: {found['text'] or ''}".rstrip(": ")[:300]
                    current["location"] = current["location"] or own_frame(frames, roots)
        elif line.startswith("ERROR:") and current["detail"] is None:
            current["detail"] = line[:300]

    ordered = [items[key] for key in order][:MAX_ITEMS]
    ordered.sort(key=lambda i: (i["kind"] != "error", i["line"]))
    return {"items": ordered, "marks": marks, "progress": _progress(passes, updated, last_ts)}


def _progress(passes, updated, last_ts):
    """Where a start or an upgrade has got, from the last pass over the modules; None when no module has loaded."""
    passes = [p for p in passes if p["index"]]
    if not passes:
        return None
    current = passes[-1]
    total, index = current["total"], current["index"]
    elapsed = (_time(last_ts) - _time(current["start"])).total_seconds() if last_ts else 0.0
    done = max(index - 1, 0)
    eta = None
    if not current["finished"] and done >= 3 and elapsed > 0:
        eta = round(elapsed / done * (total - done))
    slowest = sorted(current["durations"].items(), key=lambda kv: -kv[1])[:5]
    distinct = list(dict.fromkeys(updated))
    return {
        "total": total, "index": index, "module": current["module"], "file": current["file"],
        "remaining": max(total - index, 0), "percent": round(index * 100 / total) if total else 0,
        "elapsed_s": round(elapsed), "eta_s": eta, "finished": current["finished"],
        "seconds": current["seconds"],
        "slowest": [{"module": name, "seconds": round(sec, 1)} for name, sec in slowest if sec >= 0.5],
        "updated": len(distinct), "updated_modules": distinct[:400],
    }
