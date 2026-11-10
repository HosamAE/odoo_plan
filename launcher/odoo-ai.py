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

A new Odoo version on this machine, in one command:  odoo-ai new-version 20
"""
import glob
import os
import re
import subprocess
import sys

def _root():
    """$ODOO_ROOT, else the parent of the bin folder this launcher is installed in, else C:\\odoo."""
    env = os.environ.get("ODOO_ROOT")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(here).lower() == "bin":
        return os.path.dirname(here)
    return r"C:\odoo"


ROOT = _root()
VERSION_RX = re.compile(r"[\\/](?:odoo|ProjectsOdoo)(\d{2})(?:[\\/]|$)", re.IGNORECASE)
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


def new_version(args):
    """odoo-ai new-version NN [setup options]: tools of branch NN.0 + projects folder + `setup`. Safe to repeat."""
    numbers = [a for a in args if a.isdigit()]
    if not numbers or numbers[0] not in ("17", "18", "19", "20"):
        sys.exit("usage: odoo-ai new-version <17|18|19|20> [--check]")
    nn, rest = numbers[0], [a for a in args if a != numbers[0]]
    remote = os.environ.get("ODOO_PLAN_REMOTE", "https://github.com/HosamAE/odoo_plan.git")
    tools = os.path.join(ROOT, f"odoo{nn}", "tools")
    if os.path.isfile(os.path.join(tools, "cli.py")):
        print(f"[1/3] tools of Odoo {nn} are already in {tools}")
    else:
        print(f"[1/3] cloning branch {nn}.0 of the tools into {tools}")
        os.makedirs(os.path.dirname(tools), exist_ok=True)
        code = subprocess.call(["git", "clone", "-b", f"{nn}.0", remote, tools])
        if code:
            sys.exit(f"odoo-ai: git clone failed ({code}); is the branch {nn}.0 there and do you have access?")
    projects = os.path.join(ROOT, f"ProjectsOdoo{nn}")
    os.makedirs(projects, exist_ok=True)
    print(f"[2/3] projects folder: {projects}")
    print("[3/3] odoo-ai setup (what is missing is listed with its command; --check changes nothing)")
    code = run(nn, ["setup"] + rest)
    print(f"\nNext: clone a project into {projects}, then `odoo-ai workspace -p <project>`.")
    sys.exit(code)


def _enter_chain():
    """Count this launcher into the chain of odoo-ai commands: a loop must stop, not multiply (see recursion_guard.py)."""
    depth = int(os.environ.get("ODOO_AI_DEPTH", "0") or 0) + 1
    limit = int(os.environ.get("ODOO_AI_MAX_DEPTH", "8") or 8)
    chain = (os.environ.get("ODOO_AI_CHAIN", "") + " > launcher " + " ".join(sys.argv[1:3])).strip(" >")
    if depth > limit:
        message = f"odoo-ai: refusing to start: it called itself {depth - 1} levels deep (limit {limit}), a loop.\n  chain: {chain}"
        log = os.environ.get("ODOO_AI_GUARD_LOG")
        if log:   # a child's stderr is often swallowed by whoever started it: leave a trace they cannot swallow
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        sys.exit(message)
    os.environ["ODOO_AI_DEPTH"] = str(depth)
    os.environ["ODOO_AI_CHAIN"] = chain


def main():
    _enter_chain()
    args = sys.argv[1:]
    if args and args[0] == "new-version":
        new_version(args[1:])
    if args and args[0] == "sync-versions":   # builds every version from main, which is the Odoo 19 tools
        sys.exit(run("19", args))
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
    sys.exit("odoo-ai: run it inside a project (ProjectsOdoo<NN>\\<project>) or pass -v <NN>. "
             f"Installed versions: {', '.join(installed()) or 'none'}")


if __name__ == "__main__":
    main()
