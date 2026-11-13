"""Read the CI's Odoo log and stop on framework warnings our own modules cause.

Odoo reports a class of mistakes only as a WARNING in the log, and a
green test run says nothing about them. Each of these was found in the
log by hand on 2026-11-07 and cleaned up; this step keeps them out:

  - two fields of one model with the same label ("Two fields ... [Modules: ...]")
  - a module page that sanitises to nothing ("module X: description is empty!")
  - an inherited controller method without @route() ("is not decorated by @route()")
  - a DeprecationWarning raised from a file in one of our modules
    (odoo.tests.common.Form, read_group, odoo.osv ...)
  - an onchange on a field the model does not have ("parameters must be field names")
  - a selection redefined instead of extended ("use selection_add instead")
  - QWeb attributes the server ignores ("Unknown directives or unused attributes")
  - a framework warning about one of OUR models raised from Odoo's own code
    (2026-11-08: "Field 'hx.meeting.booking.sale_order_id' in dependency of ...
    should be searchable", logged from odoo/orm/fields.py at every start)

What it does NOT read: the WARNING and ERROR lines our own code logs on
purpose, and the ones tests provoke to prove a refusal (a Fortnox error, a
constraint violation, a mail server that is down). Those are behaviour,
not mistakes, and the tests already assert them.

Usage: python .github/scripts/check_test_log.py <odoo-log-file>
Exit code 1 when anything is found, with one line per distinct finding.
"""
import pathlib
import re
import sys

from gate_common import model_prefixes, modules




OURS = modules()
# A path inside one of our modules, in either slash direction, any case.
PATH_RX = re.compile(r"[\\/](%s)[\\/][^\s:]+\.py" % "|".join(sorted(map(re.escape, OURS))), re.I)
# A model of ours, quoted in a warning Odoo raises from its own files.
PREFIXES = model_prefixes()
OUR_MODEL_RX = re.compile(r"'((?:%s)\.[\w.]+)'" % "|".join(sorted(map(re.escape, PREFIXES)))) if PREFIXES else None
LEVEL_RX = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ \d+ (WARNING|ERROR) \S+ (\S+): (.*)$")


def finding(logger, message):
    """Return a short description when the line is one of ours, else None."""
    m = re.search(r"Two fields \(([^)]*)\) of (\S+) have the same label: (.*?)\. \[Modules: (.*?)\]", message)
    if m:
        mods = {x.strip() for x in m.group(4).split(" and ")}
        if mods & OURS:
            return f"same label {m.group(3)!r} on {m.group(1)} of {m.group(2)} ({', '.join(sorted(mods))})"
        return None
    m = re.search(r"module (\S+): description is empty!", message)
    if m:
        return f"{m.group(1)}: description is empty" if m.group(1) in OURS else None
    m = re.search(r"endpoint odoo\.addons\.(\w+)\.\S+ is not decorated by @route\(\)", message)
    if m:
        return f"{m.group(1)}: inherited endpoint without @route()" if m.group(1) in OURS else None
    if "DeprecationWarning" in message or logger == "py.warnings":
        p = PATH_RX.search(message)
        if p:
            where = p.group(0).replace("\\", "/").lstrip("/")
            what = re.sub(r"^.*?(DeprecationWarning|UserWarning|Warning): ", r"\1: ", message)
            return f"{where}: {what[:140]}"
        ours = OUR_MODEL_RX.search(message) if OUR_MODEL_RX else None
        if ours:
            what = re.sub(r"^.*?(DeprecationWarning|UserWarning|Warning): ", r"\1: ", message)
            return f"{ours.group(1)}: {what[:160]}"
        return None
    if "parameters must be field names" in message:
        return f"onchange on a field that does not exist: {message[:140]}"
    if "use selection_add instead" in message:
        return f"selection redefined: {message[:140]}"
    if "Unknown directives or unused attributes" in message:
        return f"QWeb: {message[:140]}"
    return None


def main(log_path):
    seen = {}
    for line in pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = LEVEL_RX.match(line)
        if not m:
            continue
        found = finding(m.group(2), m.group(3))
        if found:
            seen[found] = seen.get(found, 0) + 1
    for text, count in sorted(seen.items()):
        print(f"{text}  (x{count})")
    if seen:
        print(f"::error::{len(seen)} framework warning(s) from our own modules in the Odoo log. See above.")
        return 1
    print("Odoo log: no framework warnings from our own modules.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
