"""Two questions about every Python file, asked with pyflakes.

1. UNDEFINED NAMES - in every module that ships (gate and ship).
   A name the file uses but never defines or imports is a crash waiting
   for the first user who reaches that line. 2026-11-07: `_` was deleted
   from the imports of website_helpdesk_support_ticket while the file
   still called `_()` four times; compileall accepted it and every test
   stayed green, because no test deleted a ticket. This step would have
   stopped it in a second.

2. UNUSED IMPORTS - in every module that ships, third-party ones we
   carry included: nothing in the project may carry errors or warnings
   (owner's decision 2026-11-07). Zero unused imports is a project rule
   (AGENTS.md). `__init__.py` is
   exempt: `from . import x` is how a module registers its files, and so
   is a line marked `# noqa` - kept on purpose, with the reason beside it.

3. THE STATIC TRANSLATOR - `from odoo import _` (or `_lt`, or the same
   from odoo.tools / odoo.tools.translate). Odoo 19 translates with the
   environment: `self.env._("...")` in a method, `request.env._("...")`
   in a controller, `env._("...")` where an env is passed, and
   `_lt = LazyTranslate(__name__)` where there is none (Odoo's own
   tools/image.py does the same). Rule 1 of
   ODOO19_BREAKING_CHANGES_AND_FRAMEWORK_RULES.md; 2026-11-08 the project
   still had 79 such imports.

Nothing else. Style - line length, naming, f-strings - is not asked here;
a gate that argues about taste is a gate people learn to ignore.

Usage: python .github/scripts/check_python_names.py   (from the repo root)
Exit code 1 when anything is found, with one line per finding.
"""
import ast
import sys

from pyflakes import api, messages, reporter

from gate_common import ROOT, register




class Collect(reporter.Reporter):
    def __init__(self):
        super().__init__(sys.stdout, sys.stderr)
        self.found = []

    def flake(self, message):
        self.found.append(message)

    def syntaxError(self, filename, msg, lineno, offset, text):
        self.found.append(("syntax", filename, lineno, msg))

    def unexpectedError(self, filename, msg):
        self.found.append(("syntax", filename, 0, msg))


TRANSLATOR_MODULES = {"odoo", "odoo.tools", "odoo.tools.translate"}


#: env._ exists from Odoo 18 on: 17's tools turn this rule off.
TRANSLATOR_RULE = True


def static_translator(text):
    """Line numbers of `from odoo import _` / `_lt` (rule 3)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []  # reported by pyflakes already
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module in TRANSLATOR_MODULES
            and any(alias.name in ("_", "_lt") for alias in node.names)]


def main():
    words = register()
    gate = {n for n, w in words.items() if w == 'gate'}
    ship = {n for n, w in words.items() if w == 'ship'}
    ours = gate | ship
    problems = []
    for module in sorted(gate | ship):
        for path in sorted((ROOT / module).rglob("*.py")):
            if "__pycache__" in path.parts or "static" in path.parts:
                continue
            rep = Collect()
            api.checkPath(str(path), rep)
            rel = path.relative_to(ROOT).as_posix()
            # A script fed to `odoo-bin shell` (it says so in its header) gets
            # `env` from the shell, not from an import.
            text = path.read_text(encoding="utf-8", errors="ignore")
            shell_script = "odoo-bin shell" in text[:2000]
            source_lines = text.splitlines()
            for lineno in (static_translator(text) if TRANSLATOR_RULE else []):
                problems.append(f"{rel}:{lineno}: static translator import: use self.env._() "
                                f"(request.env._() in a controller, LazyTranslate with no env)")
            for m in rep.found:
                if isinstance(m, tuple):
                    problems.append(f"{rel}:{m[2]}: does not parse: {m[3]}")
                elif isinstance(m, messages.UndefinedName):
                    if shell_script and m.message_args[0] == "env":
                        continue
                    problems.append(f"{rel}:{m.lineno}: undefined name {m.message_args[0]!r}")
                elif (isinstance(m, messages.UnusedImport) and module in ours
                        and path.name != "__init__.py"
                        and "# noqa" not in source_lines[m.lineno - 1]):
                    problems.append(f"{rel}:{m.lineno}: unused import {m.message_args[0]!r}")
    for p in problems:
        print(p)
    if problems:
        print(f"::error::{len(problems)} undefined name(s) / unused import(s) / static translator(s). "
              f"See above.")
        return 1
    print(f"Python names: {len(gate | ship)} modules checked for undefined names, "
          f"unused imports and the static translator - all clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
