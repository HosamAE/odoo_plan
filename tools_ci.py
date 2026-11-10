# pylint: disable=print-used  # a command-line tool: printing is its output
"""odoo-ai tools-ci: run the tools' tests the way the CI runs them, before anything is generated or pushed.

The CI failed on three tests that passed on this machine, because this machine has things a clean runner has
not: C:\\odoo, the Odoo packages (psycopg2...), and - for a generated branch - no versions/ folder. The cause was
never the three tests: nothing ran the suite under the runner's conditions before a push. This does:

  * every version's tools are BUILT from main into a temp folder (main as it is, 17/18/20 as sync-versions
    would generate them) - the exact trees the branches will hold;
  * each tree's tests run with Odoo's packages blocked (a shim that raises ImportError), no ODOO_ROOT,
    no leftover environment, in isolated mode, from the tree's own folder.

  odoo-ai tools-ci                     all four versions (about a minute)
  odoo-ai tools-ci --versions 17,20    some
  odoo-ai tools-ci --install-hook      refuse `git push` when it fails (.git/hooks/pre-push; skip once with
                                       ODOO_AI_SKIP_TOOLS_CI=1)
`sync-versions apply` runs the same check on the tree it is about to turn into a branch.
"""
import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import sync_versions  # noqa: E402

#: What a clean CI runner does not have. A tool that needs one at import time breaks there.
BLOCKED = ("psycopg2", "lxml", "odoo")
#: The environment a runner does not carry.
DROPPED = ("ODOO_ROOT", "ODOO_SRC", "ODOO_PLAN_REMOTE", "ODOO_AI_SKIP_REHEARSAL", "GATE_ROOT", "PYTHONPATH")
RUNNER = """
import sys, unittest
sys.path.insert(0, sys.argv[1])          # the shim: blocked packages raise ImportError first
result = unittest.main(module=None, argv=["unittest", "discover", "-s", "tests"], exit=False, verbosity=1).result
sys.exit(0 if result.wasSuccessful() else 1)
"""
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "ci", "GIT_AUTHOR_EMAIL": "ci@example.invalid",
                "GIT_COMMITTER_NAME": "ci", "GIT_COMMITTER_EMAIL": "ci@example.invalid"}


def make_shim(folder):
    """A folder whose modules shadow the blocked packages with an ImportError."""
    os.makedirs(folder, exist_ok=True)
    for name in BLOCKED:
        with open(os.path.join(folder, f"{name}.py"), "w", encoding="utf-8") as handle:
            handle.write(f"raise ImportError('{name} is blocked: a CI runner does not have it (tools_ci)')\n")
    return folder


def clean_env():
    env = {k: v for k, v in os.environ.items() if k not in DROPPED}
    env.update(GIT_IDENTITY)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def summary(text):
    """'N tests, M skipped' from unittest's last lines."""
    ran = re.search(r"Ran (\d+) tests?", text)
    skipped = re.search(r"skipped=(\d+)", text)
    return f"{ran.group(1) if ran else '?'} tests" + (f", {skipped.group(1)} skipped" if skipped else "")


def run_tree(tree, timeout=900):
    """(ok, summary, output) of the tests of one built tree, under the runner's conditions."""
    # Tests run on a COPY: running them writes (caches, temp files) and `tree` may be what becomes a branch.
    with tempfile.TemporaryDirectory() as scratch:
        work = os.path.join(scratch, "tree")
        shutil.copytree(tree, work, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shim = make_shim(os.path.join(scratch, "shim"))
        done = subprocess.run([sys.executable, "-I", "-c", RUNNER, shim], cwd=work, env=clean_env(), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    output = (done.stdout or "") + (done.stderr or "")
    return done.returncode == 0, summary(output), output


def check_versions(versions, root=TOOLS_DIR):
    """Build each version from main and test it. Returns {version: (ok, summary, output)}."""
    results = {}
    for version in versions:
        with tempfile.TemporaryDirectory() as out:
            sync_versions.build_tree(root, version, out, with_versions=(version == sync_versions.MAIN_VERSION))
            results[version] = run_tree(out)
    return results


HOOK = """#!/bin/sh
# written by `odoo-ai tools-ci --install-hook`
[ -n "$ODOO_AI_SKIP_TOOLS_CI" ] && exit 0
exec "{python}" "{script}"
"""


def install_hook():
    found = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], capture_output=True, text=True, check=False)
    if found.returncode:
        print("tools-ci: not inside a git repository")
        return 2
    target = os.path.join(found.stdout.strip(), "pre-push")
    if os.path.exists(target):
        with open(target, encoding="utf-8", errors="ignore") as handle:
            if "odoo-ai tools-ci" not in handle.read():
                print(f"tools-ci: {target} exists and is not ours; add `{sys.executable} {os.path.abspath(__file__)}` to it")
                return 2
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(HOOK.format(python=sys.executable.replace("\\", "/"), script=os.path.abspath(__file__).replace("\\", "/")))
    os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR)
    print(f"tools-ci: pre-push hook installed: {target}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--versions", default=",".join(sync_versions.VERSIONS), help="comma separated (default: all)")
    parser.add_argument("--install-hook", action="store_true")
    parser.add_argument("--tail", type=int, default=40, help="lines of a failing run to show (default 40)")
    args = parser.parse_args(argv)
    if args.install_hook:
        return install_hook()
    versions = [v.strip() for v in args.versions.split(",") if v.strip()]
    failed = 0
    for version, (ok, text, output) in check_versions(versions).items():
        print(f"  [{'OK' if ok else 'FAIL'}]  Odoo {version} tools: {text}")
        if not ok:
            failed += 1
            print("\n".join("      " + line for line in output.splitlines()[-args.tail:]))
    print("tools-ci: " + ("every version's tools pass under the CI's conditions" if not failed
                          else f"{failed} version(s) fail - nothing should be pushed"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
