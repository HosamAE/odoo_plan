"""The graph task that runs when a workspace opens: leave it fresh, quietly.

`odoo_kg status` exits 1 whenever the graph is behind the tree - which is
after every commit - so a status task on folderOpen showed "Task has errors"
almost every morning. This runs status, rebuilds when the graph is stale or
was never built, and fails only when something is really wrong (a broken
config, an index that does not publish).

Usage: python tools/kg_open.py --config <project>.toml   (cwd: tools/kg)
"""
import os
import subprocess
import sys

KG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kg")


def _run(action, config):
    env = dict(os.environ, PYTHONPATH=KG_DIR, PYTHONIOENCODING="utf-8")
    res = subprocess.run([sys.executable, "-m", "odoo_kg", action, "--config", config],
                         cwd=KG_DIR, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return res.returncode, res.stdout + res.stderr


def _line(text, key):
    for line in text.splitlines():
        if line.startswith(key):
            return line[len(key):].strip()
    return ""


def main(argv):
    if len(argv) != 2 or argv[0] != "--config":
        print(__doc__)
        return 2
    config = argv[1]
    code, out = _run("status", config)
    state = _line(out, "state")
    if code == 0:
        print(f"graph fresh ({_line(out, 'graph commit')[:12]})")
        return 0
    if not (state.startswith("stale") or "no graph database" in out or "no published generation" in out):
        print(out.strip())
        print("\nthe graph has a problem that a rebuild would not fix - see above")
        return 1
    print(f"graph {state or 'not built yet'}: rebuilding ...", flush=True)
    code, out = _run("index", config)
    published = _line(out, "published")
    if code == 0 and published.startswith("yes"):
        print(f"graph rebuilt and published ({_line(out, 'duration')})")
        return 0
    print(out.strip()[-3000:])
    print("\nthe rebuild did not publish - see above")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
