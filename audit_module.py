# pylint: disable=print-used  # a command-line tool: printing is its output
"""odoo-ai audit: what must hold for an addon to load and to be reachable, checked from the source.

For one addon, or every addon under a folder (a project):
  ERROR    __manifest__.py missing or unreadable
  ERROR    a file the manifest lists does not exist
  ERROR    a new concrete model (a `_name` that extends nothing) has no row in ir.model.access.csv
  WARNING  ir.model.access.csv exists but the manifest does not load it
  WARNING  the manifest version does not start with the Odoo version (e.g. 19.0.)

Abstract and transient models and `_name` + `_inherit` extensions need no access row, so an addon that
only extends other models is not asked for a CSV.
"""
import ast
import csv
import os
import sys

SKIP_DIRS = {"tests", "static", "__pycache__", "node_modules", ".git"}


def read_manifest(path):
    try:
        with open(os.path.join(path, "__manifest__.py"), encoding="utf-8") as handle:
            data = ast.literal_eval(handle.read())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, SyntaxError):
        return None


def _names(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def new_models(addon):
    """Names of the concrete (non-abstract, non-transient) models the addon introduces."""
    found = []
    for root, dirs, files in os.walk(addon):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if not name.endswith(".py"):
                continue
            try:
                with open(os.path.join(root, name), encoding="utf-8", errors="replace") as handle:
                    tree = ast.parse(handle.read())
            except SyntaxError:
                continue
            for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                bases = " ".join(ast.unparse(b) for b in cls.bases)
                if "AbstractModel" in bases or "TransientModel" in bases:
                    continue
                model, inherit = None, []
                for stmt in cls.body:
                    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                        if stmt.targets[0].id == "_name" and _names(stmt.value):
                            model = _names(stmt.value)[0]
                        elif stmt.targets[0].id == "_inherit":
                            inherit = _names(stmt.value)
                if model and model not in inherit:
                    found.append(model)
    return sorted(set(found))


def access_rows(addon):
    path = os.path.join(addon, "security", "ir.model.access.csv")
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            return {(row.get("model_id:id") or row.get("model_id/id") or "").split(".")[-1] for row in csv.DictReader(handle)}
    except OSError:
        return None


def audit_addon(addon, odoo_version=None):
    """(errors, warnings) of one addon folder."""
    manifest = read_manifest(addon)
    if manifest is None:
        return ["__manifest__.py is missing or not a plain dict"], []
    errors, warnings = [], []
    listed = [f for key in ("data", "demo", "init_xml", "update_xml") for f in manifest.get(key, []) if isinstance(f, str)]
    for rel in listed:
        if not os.path.isfile(os.path.join(addon, *rel.split("/"))):
            errors.append(f"the manifest lists '{rel}', which does not exist")
    rows = access_rows(addon)
    if rows is not None and "security/ir.model.access.csv" not in listed:
        warnings.append("security/ir.model.access.csv exists but the manifest does not load it")
    for model in new_models(addon):
        if rows is None or f"model_{model.replace('.', '_')}" not in rows:
            errors.append(f"model '{model}' has no row in security/ir.model.access.csv")
    version = str(manifest.get("version", ""))
    if odoo_version and version and not version.startswith(f"{odoo_version}."):
        warnings.append(f"version '{version}' does not start with {odoo_version}.")
    return errors, warnings


def addons_under(path):
    if os.path.isfile(os.path.join(path, "__manifest__.py")):
        return [path]
    return sorted(os.path.join(path, d) for d in os.listdir(path)
                  if os.path.isfile(os.path.join(path, d, "__manifest__.py")))


def main(path, odoo_version=None):
    addons = addons_under(path)
    if not addons:
        print(f"[AUDIT] no addon under {path}")
        return 2
    failed = 0
    for addon in addons:
        errors, warnings = audit_addon(addon, odoo_version)
        for message in errors:
            print(f"  [ERROR]   {os.path.basename(addon)}: {message}")
        for message in warnings:
            print(f"  [WARNING] {os.path.basename(addon)}: {message}")
        failed += bool(errors)
    print(f"[AUDIT] {len(addons)} addon(s), {failed} with errors")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
