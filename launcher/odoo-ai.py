"""odoo-ai: run the tools of the right Odoo version.

Every Odoo version keeps its own tools in C:/odoo/odoo<NN>/tools; they may
differ from version to version. This launcher (installed in C:/odoo/bin, which
is on PATH) only picks the version and hands over:

  1. -v/--version NN on the command line;
  2. else the version of -p/--path;
  3. else the version of the current folder (odoo<NN>/..., its projects included).

With no version anywhere, `workspace`, `doctor`, `info` and `move-projects` run once per
installed version; any other command says how to choose one.

Source: launcher/odoo-ai.py in the tools repository. `odoo-ai doctor --fix`
copies it to C:/odoo/bin.
"""
import glob
import os
import re
import subprocess
import sys

ROOT = os.environ.get("ODOO_ROOT", r"C:\odoo")
VERSION_RX = re.compile(r"[\\/]odoo(\d{2})(?:[\\/]|$)", re.IGNORECASE)
EVERY_VERSION = {"workspace", "doctor", "info", "move-projects"}


def installed():
    found = glob.glob(os.path.join(ROOT, "odoo[0-9][0-9]", "tools", "cli.py"))
    return sorted(re.search(r"odoo(\d{2})", path).group(1) for path in found)


def option(args, *names):
    for i, arg in enumerate(args):
        for name in names:
            if arg == name and i + 1 < len(args):
                return args[i + 1]
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None


def version_of(path):
    match = VERSION_RX.search(os.path.abspath(path))
    return match.group(1) if match else None


def run(version, args):
    cli = os.path.join(ROOT, f"odoo{version}", "tools", "cli.py")
    if not os.path.isfile(cli):
        sys.exit(f"odoo-ai: no tools for Odoo {version} ({cli}). Installed: {', '.join(installed()) or 'none'}")
    return subprocess.call([sys.executable, cli] + args)


def main():
    args = sys.argv[1:]
    version = option(args, "-v", "--version")
    if not version:
        path = option(args, "-p", "--path")
        version = version_of(path) if path else version_of(os.getcwd())
    if version:
        sys.exit(run(version, args))
    command = next((a for a in args if not a.startswith("-")), None)
    if command in EVERY_VERSION:
        codes = []
        for each in installed():
            print(f"\n######## Odoo {each} tools ########", flush=True)
            codes.append(run(each, args))
        sys.exit(max(codes or [0]))
    if not args or command is None or args[0] in ("-h", "--help"):
        print(__doc__)
        print("Installed versions:", ", ".join(installed()) or "none")
        sys.exit(0)
    sys.exit("odoo-ai: run it inside a project (odoo<NN>\\projects\\<project>) or pass -v <NN>. "
             f"Installed versions: {', '.join(installed()) or 'none'}")


if __name__ == "__main__":
    main()
