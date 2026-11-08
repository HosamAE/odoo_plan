"""odoo-ai port <commit> [--to 17,18,20] [--apply]: carry a tools change to other versions.

Every version keeps its own tools, so a change made here reaches another
version only on purpose. This is that purpose made cheap: it fetches the
commit from these tools into each target version's tools and cherry-picks it.

Without --apply it tries each one and puts it back: it says which versions
take the change cleanly and which conflict, and changes nothing.
"""
import glob
import os
import re
import subprocess

from workspace_sync import FS_ROOT, TOOLS_DIR, TOOLS_VERSION


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def _versions():
    found = glob.glob(os.path.join(FS_ROOT, "odoo[0-9][0-9]", "tools", ".git"))
    return sorted(re.search(r"odoo(\d\d)", p).group(1) for p in found)


def run_port(commit, to=None, apply=False):
    sha = _git(TOOLS_DIR, "rev-parse", "--verify", f"{commit}^{{commit}}")
    if sha.returncode:
        raise SystemExit(f"no such commit in the Odoo {TOOLS_VERSION} tools: {commit}")
    sha = sha.stdout.strip()
    title = _git(TOOLS_DIR, "log", "-1", "--format=%s", sha).stdout.strip()
    targets = [v.strip() for v in to.split(",")] if to else [v for v in _versions() if v != TOOLS_VERSION]
    print(f"[PORT] {sha[:8]} {title}\n       from Odoo {TOOLS_VERSION} to {', '.join(targets)}"
          + ("" if apply else "   (trial: nothing is kept; --apply commits)"))
    bad = 0
    for v in targets:
        repo = os.path.join(FS_ROOT, f"odoo{v}", "tools")
        if not os.path.isdir(os.path.join(repo, ".git")):
            print(f"  {v}: no tools checkout at {repo}")
            bad += 1
            continue
        if _git(repo, "status", "--porcelain").stdout.strip():
            print(f"  {v}: uncommitted changes in {repo} - commit or stash them first")
            bad += 1
            continue
        _git(repo, "fetch", "-q", TOOLS_DIR, sha)
        res = _git(repo, "cherry-pick", *([] if apply else ["--no-commit"]), sha)
        if res.returncode:
            conflicts = _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.split()
            _git(repo, "cherry-pick", "--abort")
            _git(repo, "reset", "-q", "--hard", "HEAD")
            print(f"  {v}: CONFLICT in {', '.join(conflicts) or '?'} - port it by hand there")
            bad += 1
            continue
        if apply:
            print(f"  {v}: committed {_git(repo, 'log', '-1', '--format=%h').stdout.strip()}"
                  f" (push: git -C {repo} push origin {v}.0)")
        else:
            changed = _git(repo, "diff", "--cached", "--stat").stdout.strip().splitlines()
            _git(repo, "reset", "-q", "--hard", "HEAD")
            print(f"  {v}: applies cleanly ({changed[-1].strip() if changed else 'no change'})")
    return 1 if bad else 0
