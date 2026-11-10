"""XPath & XML View Validator for Odoo.

Statically validates XPath expressions and view inheritance structure before runtime:
- Syntax validity of `expr` (unbalanced brackets, missing `@` for attributes, trailing slashes)
- Valid `position` attributes ('before', 'after', 'inside', 'replace', 'attributes', 'move')
- `<attribute>` tags structure inside `position="attributes"`
- Detection of `<xpath>` without `inherit_id` in the view
- Detection of empty `<xpath>` nodes
"""
import os
import re
import sys
import xml.etree.ElementTree as ET

VALID_POSITIONS = {"before", "after", "inside", "replace", "attributes", "move"}

def validate_xpath_expr(expr):
    """Returns a list of error messages for an xpath expr string."""
    errors = []
    expr = expr.strip()
    if not expr:
        return ["Empty xpath expression"]

    # 1. Unbalanced brackets or parentheses
    if expr.count("[") != expr.count("]"):
        errors.append(f"Unbalanced brackets in expr: '{expr}'")
    if expr.count("(") != expr.count(")"):
        errors.append(f"Unbalanced parentheses in expr: '{expr}'")
    if expr.count("'") % 2 != 0 and expr.count('"') % 2 != 0:
        errors.append(f"Unmatched quote in expr: '{expr}'")

    # 2. Missing '@' in attribute test, e.g. [name='foo'] instead of [@name='foo']
    # Check words before '=' inside square brackets that don't start with @ or function calls
    for m in re.finditer(r"\[\s*([a-zA-Z_][a-zA-Z0-9_-]*)\s*=", expr):
        attr_name = m.group(1)
        if attr_name not in ("position", "last"):
            errors.append(f"Missing '@' before attribute '{attr_name}' (did you mean '@{attr_name}'?): '{expr}'")

    # 3. Trailing slashes
    if len(expr) > 1 and expr.endswith("/"):
        errors.append(f"Illegal trailing slash in expr: '{expr}'")

    # 4. Multiple consecutive slashes > 2
    if "///" in expr:
        errors.append(f"Illegal triple slash in expr: '{expr}'")

    return errors


def listed_in_manifest(filepath):
    """True when the __manifest__.py of the module holding `filepath` names it (Odoo would then load it)."""
    folder = os.path.dirname(os.path.abspath(filepath))
    while True:
        manifest = os.path.join(folder, "__manifest__.py")
        if os.path.isfile(manifest):
            rel = os.path.relpath(filepath, folder).replace(os.sep, "/")
            with open(manifest, encoding="utf-8", errors="replace") as handle:
                return rel in handle.read()
        parent = os.path.dirname(folder)
        if parent == folder:
            return False
        folder = parent


def has_no_root(content):
    """An XML document with nothing but whitespace and comments has no root element."""
    return not re.sub(r"<!--.*?-->|<\?xml[^>]*\?>", "", content, flags=re.S).strip()


def check_xml_file(filepath):
    findings = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        # Odoo reads data files as UTF-8: fatal only for a file a manifest loads (a test fixture is not)
        kind = "[XML SYNTAX ERROR]" if listed_in_manifest(filepath) else "[XML WARNING]"
        return [f"{filepath}:0: {kind} Could not read file as UTF-8: {e}"]
    if has_no_root(content):
        # no root element: Odoo cannot load it, so it is fatal when a manifest lists it and a dead file when not
        kind = "[XML SYNTAX ERROR]" if listed_in_manifest(filepath) else "[XML WARNING]"
        return [f"{filepath}:0: {kind} No root element (empty or only comments)"
                + ("" if kind == "[XML SYNTAX ERROR]" else ": no manifest loads it, it is a dead file")]

    # We also keep line lookup
    lines = content.splitlines()

    def get_lineno(snippet):
        for i, l in enumerate(lines, 1):
            if snippet in l:
                return i
        return 1

    try:
        # Strip comments to avoid parsing issues, but keep structure
        root = ET.fromstring(content)
    except ET.ParseError as e:
        # Standard XML syntax error
        return [f"{filepath}:{e.lineno if hasattr(e, 'lineno') else 0}: [XML SYNTAX ERROR] {e}"]

    # Inspect all records with model="ir.ui.view"
    for record in root.iter("record"):
        model = record.get("model")
        if model != "ir.ui.view":
            continue

        record_id = record.get("id", "unknown")
        has_inherit_id = False
        for field in record.findall("field"):
            if field.get("name") == "inherit_id":
                has_inherit_id = True
                break

        # Check arch
        for arch in record.iter("field"):
            if arch.get("name") != "arch":
                continue

            # Look for xpath elements
            xpaths = list(arch.iter("xpath"))
            if xpaths and not has_inherit_id:
                lineno = get_lineno(record_id)
                findings.append(f"{filepath}:{lineno}: [XPATH WARNING] View '{record_id}' uses <xpath> but does not define 'inherit_id'.")

            for xp in xpaths:
                expr = xp.get("expr")
                pos = xp.get("position")
                xp_str = ET.tostring(xp, encoding="utf-8").decode("utf-8", errors="ignore").splitlines()[0][:60]
                lineno = get_lineno(expr or "xpath")

                if not expr:
                    findings.append(f"{filepath}:{lineno}: [XPATH ERROR] <xpath> missing 'expr' attribute in view '{record_id}'.")
                    continue

                # Validate expr syntax
                expr_errors = validate_xpath_expr(expr)
                for err in expr_errors:
                    findings.append(f"{filepath}:{lineno}: [XPATH ERROR] {err} in view '{record_id}'.")

                # Validate position
                if pos and pos not in VALID_POSITIONS:
                    findings.append(f"{filepath}:{lineno}: [XPATH ERROR] Invalid position='{pos}'. Expected one of {sorted(VALID_POSITIONS)} in view '{record_id}'.")

                # If position="attributes", children must be <attribute>
                if pos == "attributes":
                    children = list(xp)
                    for child in children:
                        if child.tag != "attribute":
                            findings.append(f"{filepath}:{lineno}: [XPATH ERROR] <xpath position='attributes'> should only contain <attribute> tags, found <{child.tag}>.")
                        elif not child.get("name"):
                            findings.append(f"{filepath}:{lineno}: [XPATH ERROR] <attribute> tag inside xpath missing 'name' attribute.")

                # Check empty xpath
                # replace = remove, move = the target is named by the xpath itself: both are valid when empty
                if pos not in ("replace", "move") and len(xp) == 0 and not (xp.text and xp.text.strip()):
                    findings.append(f"{filepath}:{lineno}: [XPATH WARNING] Empty <xpath expr='{expr}'> has no child nodes.")

    return findings


def scan_directory(target_path):
    all_findings = []
    if os.path.isfile(target_path):
        if target_path.endswith(".xml"):
            all_findings.extend(check_xml_file(target_path))
    else:
        for root, dirs, files in os.walk(target_path):
            if any(skip in root for skip in [".git", "__pycache__", "node_modules", "static"]):
                continue
            for f in files:
                if f.endswith(".xml"):
                    fp = os.path.join(root, f)
                    all_findings.extend(check_xml_file(fp))
    return all_findings


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Odoo XML XPath and View Static Validator")
    parser.add_argument("path", nargs="?", default=".", help="Path to XML file or module directory")
    args = parser.parse_args()

    findings = scan_directory(args.path)
    if findings:
        for f in findings:
            print(f)
        errors = [f for f in findings if "[XPATH ERROR]" in f or "[XML SYNTAX ERROR]" in f]
        sys.exit(1 if errors else 0)
    else:
        print("[XPATH CHECK] All XML views and XPath expressions passed validation.")
        sys.exit(0)


if __name__ == "__main__":
    main()

