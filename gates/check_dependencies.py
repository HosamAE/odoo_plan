"""Every module declares the modules it uses.

A module that uses a model, a field's comodel or an XML id from a module
missing from its `depends` (directly or through them) works only because
that other module happens to be installed too. The full CI installs all
of them together, so it never sees the gap; a customer installing a part
does - one project: a portal module's issue routes answered 500
without website_helpdesk_support_ticket, and nothing had said so.

What counts as USING another module, read statically (no database):
  Python  _inherit / _inherits, a relational field's comodel,
          env["model"] (self.env, request.env, env), env.ref("mod.xmlid")
  XML     <record model="...">, ref="mod.xmlid", inherit_id ref, parent=
          on menus, groups="mod.group"

Deliberately NOT a gap:
  - a model the code asks about first: "model" in self.env,
    self.env.get("model"), self.env.registry.get("model") in the file
  - a line marked `# noqa: depends` (with the reason beside it)
  - an XML file the manifest does not load (scratch, unused templates)

Which module DEFINES a model is read from the project and from the Odoo
source (--odoo, $ODOO_SRC, ./odoo, or C:/odoo/odoo<NN>/odoo from the
manifest version), so a model from core counts as well as our own.

Usage: python .github/scripts/check_dependencies.py [--odoo PATH] [--all]
  --all  also the modules marked skip in .github/modules.txt
Exit code 1 when a gap is found, one line per gap.
"""
# pylint: disable=print-used  # a CI step: printing is its output
import argparse
import ast
import os
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

from gate_common import ROOT, modules
NOQA = "# noqa: depends"
ALWAYS = {"base"}  # every module depends on base
# A test that names the modules it needs: @skip_unless_installed("other_module", ...)
SKIP_DECORATOR = re.compile(r"""skip_unless_installed\(([^()]*)\)""")
QUOTED_MODULE = re.compile(r"""['"]([a-z0-9_]+)['"]""")


def asks_first(text, model):
    """The file checks the model exists before using it: a soft dependency.

    `"m" in env`, `"m" not in self.env`, `env.get("m")`,
    `registry.get("m")`, or a helper named like it (`_model_exists(env, "m")`).
    """
    quoted = r"""['"]%s['"]""" % re.escape(model)
    return re.search(quoted + r"""\s+(?:not\s+)?in\s+\S*(?:env|registry)""", text) is not None \
        or re.search(r"""(?:env|registry)\S*\.get\(\s*""" + quoted, text) is not None \
        or re.search(r"""\w*(?:exist|install|has_model)\w*\([^()]*""" + quoted, text) is not None


def soft_ref(text, lineno):
    """env.ref("mod.x", raise_if_not_found=False): the caller handles its absence."""
    lines = text.splitlines()
    return "raise_if_not_found=False" in " ".join(lines[lineno - 1:lineno + 2])


# --------------------------------------------------------------- registry


def manifest(path):
    try:
        return ast.literal_eval((path / "__manifest__.py").read_text(encoding="utf-8"))
    except (OSError, ValueError, SyntaxError):
        return None


def addon_dirs(base):
    if not base.is_dir():
        return []
    return [d for d in base.iterdir() if d.is_dir() and (d / "__manifest__.py").is_file()]


def odoo_root():
    """The workspace root holding odoo<NN>/: $ODOO_ROOT, else the nearest parent of the project with one.

    The gates are copied into each project, so they cannot import the tools' env_root.
    """
    env = os.environ.get("ODOO_ROOT")
    if env and os.path.isdir(env):
        return pathlib.Path(env)
    for parent in ROOT.resolve().parents:
        if any((parent / f"odoo{v}" / "odoo").is_dir() for v in ("17", "18", "19", "20")):
            return parent
    return None


def find_odoo(explicit):
    # The project's own version first: <root>/odoo<NN>/projects/<project> is Odoo <NN>,
    # whatever its manifests say and wherever this runs from.
    root = odoo_root()
    by_path = re.search(r"[\\/](?:odoo|ProjectsOdoo)(\d+)(?:[\\/]|$)", str(ROOT))
    candidates = [explicit, os.environ.get("ODOO_SRC"), ROOT / "odoo"]
    if root and by_path:
        candidates.append(root / f"odoo{by_path.group(1)}" / "odoo")
    candidates += [ROOT.parent / "odoo", pathlib.Path.cwd() / "odoo"]
    version = None
    for d in addon_dirs(ROOT)[:20]:
        m = manifest(d)
        if m and str(m.get("version", "")).count(".") >= 2:
            version = str(m["version"]).split(".")[0]
            break
    if root and version:
        candidates.append(root / f"odoo{version}" / "odoo")
    for c in candidates:
        if c and (pathlib.Path(c) / "odoo" / "addons" / "base" / "__manifest__.py").is_file():
            return pathlib.Path(c)
    return None


# ---------------------------------------------------------- what defines
def model_names(node):
    """`_name` / `_inherit` of a class body: (names it defines, names it extends)."""
    name, inherit = None, []
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            key, val = stmt.targets[0].id, stmt.value
            if key == "_name" and isinstance(val, ast.Constant) and isinstance(val.value, str):
                name = val.value
            elif key == "_inherit":
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    inherit = [val.value]
                elif isinstance(val, (ast.List, ast.Tuple)):
                    inherit = [e.value for e in val.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    defines = [name] if name and name not in inherit else []
    return defines, inherit


def python_files(addon):
    for path in addon.rglob("*.py"):
        if "__pycache__" not in path.parts and "static" not in path.parts and path.name != "__manifest__.py":
            yield path


def index_definitions(addons):
    defined = {}
    for addon in addons:
        for path in python_files(addon):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for name in model_names(node)[0]:
                        defined.setdefault(name, set()).add(addon.name)
    return defined


# ------------------------------------------------------------- what uses
RELATIONAL = {"Many2one", "One2many", "Many2many", "Many2oneReference"}


def env_subscript(node):
    """'model' from env["model"] / self.env["model"] / request.env["model"]."""
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        target = node.value
        if (isinstance(target, ast.Attribute) and target.attr == "env") or (isinstance(target, ast.Name) and target.id == "env"):
            return node.slice.value
    return None


def python_uses(path, text):
    """[(line, kind, name)] where kind is 'model' or 'xmlid'."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            _defines, inherit = model_names(node)
            out += [(node.lineno, "model", n) for n in inherit]
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name) \
                        and stmt.targets[0].id == "_inherits" and isinstance(stmt.value, ast.Dict):
                    out += [(stmt.lineno, "model", k.value) for k in stmt.value.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        elif isinstance(node, ast.Call):
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if fname in RELATIONAL:
                comodel = None
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    comodel = node.args[0].value
                for kw in node.keywords:
                    if kw.arg == "comodel_name" and isinstance(kw.value, ast.Constant):
                        comodel = kw.value.value
                if comodel and "." in comodel:
                    out.append((node.lineno, "model", comodel))
            elif fname == "ref" and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str) and "." in node.args[0].value \
                    and isinstance(func, ast.Attribute) and "env" in ast.unparse(func.value):
                out.append((node.lineno, "xmlid", node.args[0].value))
        else:
            name = env_subscript(node)
            if name:
                out.append((node.lineno, "model", name))
    return out


def loaded_xml(addon, info):
    keys = ("data", "demo", "init_xml", "update_xml")
    for key in keys:
        for rel in info.get(key, []) or []:
            if rel.endswith(".xml"):
                path = addon / rel
                if path.is_file():
                    yield path


def xml_uses(path):
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return []
    out = []
    for el in tree.iter():
        if el.tag == "record" and el.get("model"):
            out.append((0, "model", el.get("model")))
        for attr in ("ref", "parent", "inherit_id", "action"):
            value = el.get(attr)
            if value and "." in value and " " not in value and "(" not in value:
                out.append((0, "xmlid", value))
        if el.tag == "field" and el.get("name") in ("inherit_id", "model_id", "groups_id") and el.get("ref"):
            out.append((0, "xmlid", el.get("ref")))
        groups = el.get("groups")
        if groups:
            out += [(0, "xmlid", g.strip().lstrip("!")) for g in groups.split(",") if "." in g]
    return out


# ------------------------------------------------------------------ check
def closure(name, manifests, cache):
    if name in cache:
        return cache[name]
    cache[name] = {name}
    seen = {name}
    for dep in (manifests.get(name) or {}).get("depends", []):
        seen |= closure(dep, manifests, cache)
    cache[name] = seen | ALWAYS
    return cache[name]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--odoo")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    odoo = find_odoo(args.odoo)
    if odoo is None:
        print("::error::Odoo source not found: pass --odoo, set ODOO_SRC, or check it out at ./odoo")
        return 2
    ours = addon_dirs(ROOT)
    core = addon_dirs(odoo / "addons") + addon_dirs(odoo / "odoo" / "addons")
    every = core + ours
    manifests = {d.name: manifest(d) for d in every}
    defined = index_definitions(every)
    names = {d.name for d in every}

    wanted = modules(args.all)
    gaps, cache = [], {}
    for addon in sorted(ours, key=lambda d: d.name):
        if addon.name not in wanted or not manifests.get(addon.name):
            continue
        reach = closure(addon.name, manifests, cache)
        uses = []
        for path in python_files(addon):
            rel_parts = path.relative_to(addon).parts
            text = path.read_text(encoding="utf-8", errors="ignore")
            # A script fed to `odoo-bin shell` by hand is not loaded by Odoo.
            if rel_parts[0] == "scripts" or "odoo-bin shell" in text[:2000]:
                continue
            lines = text.splitlines()
            found = python_uses(path, text)
            # A module the file asks about (any one of its models) is a soft
            # dependency for the whole file: the rest of its models and ids
            # are reached only past that check.
            soft_modules = set()
            for _ln, kind, name in found:
                if kind == "model" and asks_first(text, name):
                    soft_modules |= defined.get(name, set())
            for args in SKIP_DECORATOR.findall(text):
                soft_modules |= set(QUOTED_MODULE.findall(args))
            # ...and what those modules depend on is there whenever they are
            for module in list(soft_modules):
                soft_modules |= closure(module, manifests, cache)
            for lineno, kind, name in found:
                if lineno and NOQA in lines[lineno - 1]:
                    continue
                owners = defined.get(name, set()) if kind == "model" else {name.split(".", 1)[0]}
                if owners & soft_modules:
                    continue
                if kind == "xmlid" and lineno and soft_ref(text, lineno):
                    continue
                uses.append((path, lineno, kind, name))
        for path in loaded_xml(addon, manifests[addon.name]):
            uses += [(path, ln, kind, name) for ln, kind, name in xml_uses(path)]
        for path, lineno, kind, name in uses:
            if kind == "model":
                owners = defined.get(name)
                if not owners or owners & reach:
                    continue
                need = sorted(owners)
            else:
                module = name.split(".", 1)[0]
                if module not in names or module in reach:
                    continue
                need = [module]
            rel = path.relative_to(ROOT).as_posix()
            where = f"{rel}:{lineno}" if lineno else rel
            gaps.append(f"{where}: {addon.name} uses {kind} {name!r} from {' or '.join(need)}, "
                        f"which is not in its depends")
    seen = set()
    for g in gaps:
        if g not in seen:
            seen.add(g)
            print(g)
    if seen:
        mods = len({g.split(": ", 1)[1].split(" uses ", 1)[0] for g in seen})
        print(f"::error::{len(seen)} use(s) of a module that is not depended on, in {mods} module(s). "
              f"Add it to depends, ask first (\"model\" in self.env), or mark the line {NOQA!r} with the reason.")
        return 1
    print(f"Dependencies: {len(wanted & {d.name for d in ours})} modules use only what they depend on "
          f"(Odoo source: {odoo}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
