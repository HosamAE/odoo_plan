# pylint: disable=print-used  # a CI step: printing is its output
"""The Odoo 19 rules, checked instead of remembered.

ODOO19_BREAKING_CHANGES_AND_FRAMEWORK_RULES.md and AGENTS.md write them
down; written down alone they were broken 79 times for the translator
(2026-11-08). Each rule here is one an Odoo 19 module can break silently -
it installs, the tests pass, and it misbehaves or stops working in 20.

  R1  odoo.osv.expression         -> odoo.fields.Domain
  R2  default_res_id in a context -> default_res_ids: [id] (mail composer)
  R3  len(...search(...))         -> search_count()
  R4  default=<method itself>     -> default=lambda self: self._method()
  R5  a compute assigning a field that has no compute=   (the field is
      stored and written as a side effect: filters read a stale value)
  R6  <search> <group> holding anything but <filter>
  R7  context="... parent.x ..." in a view   -> an onchange
  R8  read_group(                 -> _read_group / formatted_read_group
  R9  def name_get(               -> _compute_display_name
  R10 env._("... %s") % x         -> env._("... %s", x): formatted after the call,
      the translation is looked up before the values exist (not lazy)

The translator (`from odoo import _`) is check_python_names.py's.
A line that must break a rule says why: `# noqa: odoo19 <reason>` (XML:
<!-- noqa: odoo19 <reason> --> on the line).

Usage: python .github/scripts/check_odoo19_rules.py [--all]
Exit code 1 when anything is found, one line per finding.
"""
import argparse
import ast
import re
import sys

from gate_common import ROOT, modules as registered_modules
NOQA = "noqa: odoo19"
#: The rules this version keeps. Each version's tools carry their own list:
#: Odoo 17 has no Domain, env._ or _read_group; 18 no Domain or _read_group.
ENABLED = {"R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10"}
VERSION = "19"




# --------------------------------------------------------------- python
FIELD_TYPES = {"Char", "Text", "Html", "Integer", "Float", "Monetary", "Boolean", "Date", "Datetime",
               "Selection", "Many2one", "One2many", "Many2many", "Binary", "Image", "Json", "Reference",
               "Many2oneReference", "Properties"}


def _kw(call, name):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def python_findings(path, text):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        # R1
        if isinstance(node, ast.ImportFrom) and node.module and (
                node.module == "odoo.osv.expression"
                or (node.module == "odoo.osv" and any(a.name == "expression" for a in node.names))):
            out.append((node.lineno, "R1", "odoo.osv.expression: use odoo.fields.Domain"))
        # R3
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len" \
                and node.args and isinstance(node.args[0], ast.Call) \
                and isinstance(node.args[0].func, ast.Attribute) and node.args[0].func.attr == "search":
            out.append((node.lineno, "R3", "len(search(...)): use search_count(...)"))
        # R8
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read_group" \
                and not (isinstance(node.func.value, ast.Call) and isinstance(node.func.value.func, ast.Name)
                         and node.func.value.func.id == "super"):
            out.append((node.lineno, "R8", "read_group(): use _read_group() or formatted_read_group()"))
        # R10
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Call):
            fn = node.left.func
            if (fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")) in ("_", "_lt"):
                out.append((node.lineno, "R10", 'translation formatted with %: pass the values to _("...", ...)'))
        if isinstance(node, ast.ClassDef):
            out += class_findings(node)
    # R2 (a string key anywhere: dicts, context strings)
    for i, line in enumerate(text.splitlines(), 1):
        if re.search(r"""['"]default_res_id['"]""", line):
            out.append((i, "R2", "default_res_id: the composer reads default_res_ids: [id]"))
    return out


def class_findings(cls):
    out = []
    methods = {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    fields = {}   # name -> (lineno, compute name or None)
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) \
                and isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Attribute) \
                and stmt.value.func.attr in FIELD_TYPES:
            call = stmt.value
            compute = _kw(call, "compute")
            fields[stmt.targets[0].id] = (stmt.lineno, compute.value if isinstance(compute, ast.Constant) else
                                          (compute.id if isinstance(compute, ast.Name) else None))
            default = _kw(call, "default")
            # R4
            if isinstance(default, ast.Name) and default.id in methods:
                out.append((stmt.lineno, "R4", f"default={default.id}: use default=lambda self: self.{default.id}()"))
    if "name_get" in methods:
        out.append((methods["name_get"].lineno, "R9", "name_get(): Odoo 19 uses _compute_display_name"))
    # R5: inside a compute method, `rec.<field> = ...` for a field of this class with no compute
    computes = {c for _ln, c in fields.values() if c}
    for name in computes:
        method = methods.get(name)
        if method is None:
            continue
        for node in ast.walk(method):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr in fields \
                            and fields[target.attr][1] is None:
                        out.append((node.lineno, "R5", f"{name} writes {target.attr}, which has no compute=: "
                                    f"give it compute='{name}' (and a search= if it is filtered on)"))
    return out


# ------------------------------------------------------------------ xml
def xml_findings(path, text):
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        # parent.id is allowed: Odoo's own catalog buttons pass context="{'order_id': parent.id}"
        if re.search(r"""context=(?:"[^"]*\bparent\.(?!id\b)|'[^']*\bparent\.(?!id\b))""", line):
            out.append((i, "R7", "parent.<field> in a view context: compute it in an onchange instead"))
        if "default_res_id'" in line or 'default_res_id"' in line or "default_res_id:" in line:
            out.append((i, "R2", "default_res_id: the composer reads default_res_ids: [id]"))
    # R6: <search> ... <group> ... </group> with non-filter children. Comments
    # go first (kept as blank lines, so line numbers hold): a comment that
    # talks about "<group>" is not one.
    text = re.sub(r"<!--.*?-->", lambda c: "\n" * c.group(0).count("\n"), text, flags=re.S)
    for m in re.finditer(r"<search\b.*?</search>", text, re.S):
        for g in re.finditer(r"<group\b[^>]*>(.*?)</group>", m.group(0), re.S):
            inner = re.sub(r"<!--.*?-->", "", g.group(1), flags=re.S)
            tags = set(re.findall(r"<\s*([a-zA-Z_]+)", inner))
            if tags - {"filter", "separator"}:
                lineno = text[:m.start() + g.start()].count("\n") + 1
                out.append((lineno, "R6", f"<group> in <search> holds {sorted(tags - {'filter', 'separator'})}: "
                            f"only <filter> may sit there"))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    found = []
    modules = sorted(registered_modules(args.all))
    for module in modules:
        base = ROOT / module
        for path in sorted(base.rglob("*")):
            if "__pycache__" in path.parts or "static" in path.parts or not path.is_file():
                continue
            if path.suffix not in (".py", ".xml"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()
            results = python_findings(path, text) if path.suffix == ".py" else xml_findings(path, text)
            rel = path.relative_to(ROOT).as_posix()
            for lineno, rule, message in results:
                if rule not in ENABLED:
                    continue
                if 0 < lineno <= len(lines) and NOQA in lines[lineno - 1]:
                    continue
                found.append(f"{rel}:{lineno}: {rule} {message}")
    for f in sorted(set(found)):
        print(f)
    if found:
        print(f"::error::{len(set(found))} Odoo {VERSION} rule break(s). Fix them, or mark the line "
              f"'# {NOQA} <reason>' when the old form is right there.")
        return 1
    print(f"Odoo {VERSION} rules: {len(modules)} modules keep all {len(ENABLED)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
