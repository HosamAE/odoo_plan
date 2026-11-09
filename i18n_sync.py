"""Odoo Translation & i18n Synchronization Engine.

Extracts translatable strings from Python and XML files, compares them against
language .po files (e.g. sv_SE, ar, en), identifies untranslated terms, and syncs
or updates .po and .pot files.

Usage:
  python i18n_sync.py scan -m <module_path_or_name> [-l <lang>]
  python i18n_sync.py update -m <module_path_or_name> -l <lang>
  python i18n_sync.py export -m <module_path_or_name>
"""
import argparse
import ast
import datetime
import os
import re
import sys
import xml.etree.ElementTree as ET

TRANSLATABLE_XML_ATTRS = {"string", "help", "placeholder", "title", "alt", "confirm", "name"}
TRANSLATABLE_XML_TAGS = {"menuitem", "label"}

def parse_po_file(po_path):
    """Simple robust PO parser returning dict of {msgid: msgstr} and raw entries."""
    translations = {}
    if not os.path.exists(po_path):
        return translations

    with open(po_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # Match msgid and msgstr pairs
    pattern = re.compile(r'msgid\s+"(.*?(?<!\\))"\s*\nmsgstr\s+"(.*?(?<!\\))"', re.DOTALL)
    for m in pattern.finditer(content):
        msgid = m.group(1).replace(r'\"', '"').replace(r'\n', '\n')
        msgstr = m.group(2).replace(r'\"', '"').replace(r'\n', '\n')
        if msgid:
            translations[msgid] = msgstr

    return translations

def extract_strings_from_py(py_path):
    """Extract strings wrapped in _(...) or self.env._(...)"""
    terms = []
    try:
        with open(py_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        tree = ast.parse(text)
    except Exception:
        return terms

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            is_trans = False
            # _("Text")
            if isinstance(node.func, ast.Name) and node.func.id in ("_", "_lt"):
                is_trans = True
            # self.env._("Text")
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "_":
                is_trans = True

            if is_trans and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                val = node.args[0].value.strip()
                if val:
                    terms.append((val, py_path, node.lineno))
    return terms

def extract_strings_from_xml(xml_path):
    """Extract translatable attributes and tag contents from XML."""
    terms = []
    try:
        with open(xml_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        root = ET.fromstring(text)
    except Exception:
        return terms

    lines = text.splitlines()
    def find_lineno(val):
        for idx, l in enumerate(lines, 1):
            if val in l:
                return idx
        return 1

    for elem in root.iter():
        # Check attributes
        for attr, val in elem.attrib.items():
            if attr in TRANSLATABLE_XML_ATTRS and val and not val.startswith("%") and not val.startswith("{"):
                # Avoid field name="x" being treated as string, only menuitem name="x"
                if attr == "name" and elem.tag != "menuitem":
                    continue
                val_clean = val.strip()
                if val_clean and not val_clean.isdigit():
                    terms.append((val_clean, xml_path, find_lineno(val_clean)))

    return terms

def scan_module_terms(module_dir):
    """Scan all terms across an addon directory."""
    all_terms = {} # {text: [(file, line)]}
    for root, dirs, files in os.walk(module_dir):
        if any(skip in root for skip in [".git", "__pycache__", "static/lib", "tests"]):
            continue
        for f in files:
            fp = os.path.join(root, f)
            if f.endswith(".py"):
                for term, p, line in extract_strings_from_py(fp):
                    all_terms.setdefault(term, []).append((os.path.relpath(p, module_dir), line))
            elif f.endswith(".xml"):
                for term, p, line in extract_strings_from_xml(fp):
                    all_terms.setdefault(term, []).append((os.path.relpath(p, module_dir), line))
    return all_terms

def resolve_module_dir(mod_arg):
    if os.path.isdir(mod_arg):
        return os.path.abspath(mod_arg)
    # Search in cwd
    cwd = os.getcwd()
    for root, dirs, files in os.walk(cwd):
        if mod_arg in dirs and os.path.isfile(os.path.join(root, mod_arg, "__manifest__.py")):
            return os.path.join(root, mod_arg)
    # Search in ProjectsOdoo19
    for p_root in [r"C:\odoo\ProjectsOdoo19", r"C:\odoo\ProjectsOdoo18", r"C:\odoo\ProjectsOdoo17", r"C:\odoo\ProjectsOdoo20"]:
        if os.path.isdir(p_root):
            for root, dirs, files in os.walk(p_root):
                if mod_arg in dirs and os.path.isfile(os.path.join(root, mod_arg, "__manifest__.py")):
                    return os.path.join(root, mod_arg)
    raise FileNotFoundError(f"Could not find module directory for '{mod_arg}'.")

def cmd_scan(module_dir, lang):
    mod_name = os.path.basename(module_dir)
    terms = scan_module_terms(module_dir)
    print(f"\n[*] Scanning i18n terms for module: {mod_name}")
    print(f"[*] Total unique source terms found: {len(terms)}")

    i18n_dir = os.path.join(module_dir, "i18n")
    po_files = []
    if os.path.isdir(i18n_dir):
        po_files = [f for f in os.listdir(i18n_dir) if f.endswith(".po")]

    if lang:
        target_po = f"{lang}.po" if not lang.endswith(".po") else lang
        po_files = [target_po] if target_po in po_files else []

    if not po_files:
        print(f"[-] No .po files found in {i18n_dir} for target lang.")
        print(f"[i] To create/update a translation file, run: odoo-ai i18n update -m {mod_name} -l <lang>\n")
        return

    for pof in po_files:
        po_path = os.path.join(i18n_dir, pof)
        existing = parse_po_file(po_path)
        translated = {k: v for k, v in existing.items() if v.strip()}
        missing = [t for t in terms if t not in existing or not existing[t].strip()]

        print(f"\n--- Language: {pof} ---")
        print(f"  Existing entries:   {len(existing)}")
        print(f"  Translated terms:   {len(translated)} ({len(translated)*100//(len(terms) or 1)}%)")
        print(f"  Missing / Untranslated: {len(missing)}")

        if missing:
            print("\n  Sample missing terms:")
            for m in missing[:10]:
                locs = ", ".join(f"{f}:{l}" for f, l in terms[m][:2])
                print(f"    - \"{m}\" ({locs})")
            if len(missing) > 10:
                print(f"    ... and {len(missing) - 10} more.")
    print()

def cmd_update(module_dir, lang):
    mod_name = os.path.basename(module_dir)
    terms = scan_module_terms(module_dir)
    i18n_dir = os.path.join(module_dir, "i18n")
    os.makedirs(i18n_dir, exist_ok=True)

    po_name = f"{lang}.po" if not lang.endswith(".po") else lang
    po_path = os.path.join(i18n_dir, po_name)
    existing = parse_po_file(po_path)

    missing = [t for t in terms if t not in existing]
    if not missing:
        print(f"[SUCCESS] All {len(terms)} terms already exist in {po_name}.")
        return

    is_new = not os.path.exists(po_path)
    with open(po_path, "a", encoding="utf-8") as f:
        if is_new:
            f.write(f"""# Translation of Odoo Server.
# This file contains the translation of the following modules:
# \t* {mod_name}
#
msgid ""
msgstr ""
"Project-Id-Version: Odoo Server\\n"
"Report-Msgid-Bugs-To: \\n"
"POT-Creation-Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M+0000')}\\n"
"PO-Revision-Date: \\n"
"Last-Translator: \\n"
"Language-Team: \\n"
"MIME-Version: 1.0\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Content-Transfer-Encoding: 8bit\\n"
"Language: {lang}\\n"

""")
        for m in sorted(missing):
            locs = "\n#: ".join(f"{f}:{l}" for f, l in terms[m])
            esc_m = m.replace('"', r'\"').replace('\n', '\\n"\n"')
            f.write(f"#. module: {mod_name}\n#: {locs}\nmsgid \"{esc_m}\"\nmsgstr \"\"\n\n")

    print(f"[SUCCESS] Appended {len(missing)} untranslated terms into '{po_path}'.")

def cmd_export(module_dir):
    mod_name = os.path.basename(module_dir)
    terms = scan_module_terms(module_dir)
    i18n_dir = os.path.join(module_dir, "i18n")
    os.makedirs(i18n_dir, exist_ok=True)

    pot_path = os.path.join(i18n_dir, f"{mod_name}.pot")
    with open(pot_path, "w", encoding="utf-8") as f:
        f.write(f"""# Translation template for {mod_name}.
msgid ""
msgstr ""
"Project-Id-Version: Odoo Server\\n"
"POT-Creation-Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M+0000')}\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Content-Transfer-Encoding: 8bit\\n"

""")
        for m in sorted(terms.keys()):
            locs = "\n#: ".join(f"{f}:{l}" for f, l in terms[m])
            esc_m = m.replace('"', r'\"').replace('\n', '\\n"\n"')
            f.write(f"#. module: {mod_name}\n#: {locs}\nmsgid \"{esc_m}\"\nmsgstr \"\"\n\n")

    print(f"[SUCCESS] Exported {len(terms)} terms into template '{pot_path}'.")

def main():
    parser = argparse.ArgumentParser(description="Odoo Translation & i18n Sync Engine")
    subparsers = parser.add_subparsers(dest="action", required=True)

    p_scan = subparsers.add_parser("scan", help="Scan and compare module terms against PO files")
    p_scan.add_argument("-m", "--module", required=True, help="Module name or path")
    p_scan.add_argument("-l", "--lang", default="", help="Specific language code (e.g. sv_SE, ar)")

    p_upd = subparsers.add_parser("update", help="Update/append missing terms into a language PO file")
    p_upd.add_argument("-m", "--module", required=True, help="Module name or path")
    p_upd.add_argument("-l", "--lang", required=True, help="Language code (e.g. sv_SE, ar)")

    p_exp = subparsers.add_parser("export", help="Export a clean POT template for a module")
    p_exp.add_argument("-m", "--module", required=True, help="Module name or path")

    args = parser.parse_args()

    try:
        mod_dir = resolve_module_dir(args.module)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    if args.action == "scan":
        cmd_scan(mod_dir, args.lang)
    elif args.action == "update":
        cmd_update(mod_dir, args.lang)
    elif args.action == "export":
        cmd_export(mod_dir)

if __name__ == "__main__":
    main()

