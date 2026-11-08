"""odoo-ai move-projects: C:/odoo/ProjectsOdoo<NN>/* -> C:/odoo/odoo<NN>/projects/*

The layout agreed on 2026-11-08: one folder per Odoo version, holding its
source, venv, tools AND its projects. Run once per machine; dry run unless
--apply. It never deletes a file: everything is moved (a rename on the same
drive), and the old folder is removed only when it is empty.

What it does for this tools' version:
  1. moves every project folder into odoo<NN>/projects/
  2. moves <Project>.odools.toml (beside the project) into the project as
     odools.toml; any other file beside the projects goes along unchanged
  3. rewrites the old path in each project's local, untracked files that
     carry absolute paths: *.conf, .claude/*.json, .vscode/*.json - and in
     odoo<NN>/odoo.conf (secrets stay where they are: only the path changes)
  4. `git worktree repair` in every moved repository
Afterwards: `odoo-ai workspace` rewrites workspaces, graph configs and
CLAUDE.md for the new paths, and `odoo-ai kg index` in each project
refreshes its graph.

Close VS Code (and stop Odoo servers) first: Windows refuses to move a
folder another program has open.
"""
import os
import re
import shutil
import subprocess

from workspace_sync import FS_ROOT, LEGACY_PROJECTS, TOOLS_VERSION, fs, projects_root

LOCAL_FILES = ("*.conf", ".claude/*.json", ".vscode/*.json")


def _legacy_rx(version):
    # C:/odoo/ProjectsOdoo19/  C:\odoo\ProjectsOdoo19\  C:\\odoo\\ProjectsOdoo19\\ (any case)
    return re.compile(r"([Cc]:)([\\/]{1,2})odoo\2" + LEGACY_PROJECTS + version + r"(?=\2)", re.IGNORECASE)


def rewrite_paths(path, version, apply):
    """Old project paths in one text file -> new ones. True when it changed."""
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return False
    new = _legacy_rx(version).sub(lambda m: f"{m.group(1)}{m.group(2)}odoo{m.group(2)}odoo{version}"
                                             f"{m.group(2)}projects", text)
    if new == text:
        return False
    if apply:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(new)
    return True


def tracked(repo, rel):
    if not os.path.isdir(os.path.join(repo, ".git")):
        return False
    res = subprocess.run(["git", "-C", repo, "ls-files", "--error-unmatch", rel], capture_output=True)
    return res.returncode == 0


def local_files(project):
    import glob
    for pattern in LOCAL_FILES:
        for path in glob.glob(os.path.join(project, pattern)):
            if os.path.isfile(path) and not tracked(project, os.path.relpath(path, project)):
                yield path


def run_move_projects(apply=False):
    v = TOOLS_VERSION
    src = os.path.join(FS_ROOT, f"{LEGACY_PROJECTS}{v}")
    dst = fs(projects_root(v))
    mode = "" if apply else "   (dry run: nothing is changed; --apply does it)"
    print(f"[MOVE] Odoo {v}: {src} -> {dst}{mode}")
    if not os.path.isdir(src):
        print("  nothing to move: the old folder does not exist")
        return 0
    entries = sorted(os.listdir(src))
    clash = [e for e in entries if os.path.exists(os.path.join(dst, e))]
    if clash:
        print(f"  STOP: already in {dst}: {', '.join(clash)}")
        return 1
    if apply:
        os.makedirs(dst, exist_ok=True)
    folders = [e for e in entries if os.path.isdir(os.path.join(src, e))]
    odools = {e[:-len(".odools.toml")]: e for e in entries if e.endswith(".odools.toml")}
    others = [e for e in entries if e not in folders and e not in odools.values()]
    failed = []
    for name in folders:
        print(f"  move     {name}/")
        if apply:
            try:
                os.rename(os.path.join(src, name), os.path.join(dst, name))
            except OSError as err:
                failed.append(name)
                print(f"           FAILED: {err.strerror} (is it open in VS Code or a running Odoo?)")
    for project, fname in odools.items():
        target = os.path.join(dst, project, "odools.toml") if project in folders else os.path.join(dst, fname)
        print(f"  move     {fname} -> {os.path.relpath(target, dst)}")
        if apply and project not in failed:
            shutil.move(os.path.join(src, fname), target)
    for fname in others:
        print(f"  move     {fname}")
        if apply:
            shutil.move(os.path.join(src, fname), os.path.join(dst, fname))
    # 3. absolute paths in local files
    base = dst if apply else src
    for name in folders:
        project = os.path.join(base, name)
        for path in local_files(project):
            if rewrite_paths(path, v, apply):
                print(f"  paths    {name}/{os.path.relpath(path, project)}")
    version_conf = os.path.join(FS_ROOT, f"odoo{v}", "odoo.conf")
    if os.path.isfile(version_conf) and rewrite_paths(version_conf, v, apply):
        print(f"  paths    odoo{v}/odoo.conf")
    # 4. worktrees know their main repository by absolute path
    for name in folders:
        project = os.path.join(base, name)
        if apply and os.path.isdir(os.path.join(project, ".git")):
            subprocess.run(["git", "-C", project, "worktree", "repair"], capture_output=True)
            subprocess.run(["git", "-C", project, "worktree", "prune"], capture_output=True)
    if apply and not failed:
        try:
            os.rmdir(src)
            print(f"  removed  {src} (empty)")
        except OSError:
            print(f"  kept     {src}: not empty")
    if failed:
        print(f"\n[MOVE] {len(failed)} not moved: {', '.join(failed)}. Close what holds them and run again.")
        return 1
    print("\n[MOVE] done." + (" Next: odoo-ai workspace, then odoo-ai kg index in each project." if apply else ""))
    return 0
