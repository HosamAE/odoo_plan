"""What every gate shares: where the project is, and which modules are ours.

The gates live in the tools (C:/odoo/odoo<NN>/tools/gates) and are copied into
a project's .github/scripts by `odoo-ai gates --install`, so that its GitHub CI
runs them too. Either way they find the project the same way:

  - $GATE_ROOT when it is set (odoo-ai ci runs the tools' copy like that), else
  - two folders up from the script (<project>/.github/scripts/<gate>.py).

Which modules are ours: `.github/modules.txt` when the project has one
(gate / ship / skip, one module a line); without it, every addon folder of the
project counts as a gate module.
"""
import os
import pathlib

ROOT = pathlib.Path(os.environ.get("GATE_ROOT") or pathlib.Path(__file__).resolve().parents[2]).resolve()
REGISTER = ROOT / ".github" / "modules.txt"


def addon_folders():
    return sorted(d.name for d in ROOT.iterdir()
                  if d.is_dir() and (d / "__manifest__.py").is_file() and not d.name.startswith("."))


def register():
    """{module: 'gate' | 'ship' | 'skip'}, from modules.txt or, without one, every addon as gate."""
    if not REGISTER.is_file():
        return {name: "gate" for name in addon_folders()}
    words = {}
    for raw in REGISTER.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].split()
        if len(line) == 2 and line[0] in ("gate", "ship", "skip"):
            words[line[1]] = line[0]
    return words


def model_prefixes():
    """The first word of our model names (`acme.sale.order` -> `acme`).

    The word most of our module names start with (`acme_sale`, `acme_hr` -> `acme`; a word shared by fewer than half of them
    is not a company's mark: `website_x` is Odoo's), and what the project's own file `.github/model_prefixes.txt` lists
    (one a line, `#` for comments) for models named differently from their modules.
    """
    names = [name.split("_", 1)[0] for name in modules() if "_" in name]
    found = set()
    if names:
        word = max(set(names), key=names.count)
        if names.count(word) * 2 >= len(names):
            found.add(word)
    file = ROOT / ".github" / "model_prefixes.txt"
    if file.is_file():
        for raw in file.read_text(encoding="utf-8").splitlines():
            entry = raw.split("#", 1)[0].strip()
            if entry:
                found.add(entry)
    return found


def modules(include_skip=False):
    """The modules the gates read: gate and ship (and skip on request)."""
    wanted = ("gate", "ship", "skip") if include_skip else ("gate", "ship")
    return {name for name, word in register().items() if word in wanted}
