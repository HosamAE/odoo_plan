# pylint: disable=print-used  # a command-line tool: printing is its output
"""odoo-ai sync-versions: build every version's tools from `main`.

`main` holds the shared tools, in the flavour of MAIN_VERSION (19). What differs
per Odoo version lives only in versions/<NN>/:

    versions/<NN>/MANIFEST.json   {"remove": [paths main has and <NN> must not],
                                   "replace": {path: [[old, new], ...]}}
    versions/<NN>/files/<path>    files that replace (or add to) main's

The version branch <NN>.0 is *generated*: main's tree, minus MANIFEST "remove",
plus versions/<NN>/files, plus MANIFEST "replace". Nothing here pushes: --apply
writes a local branch sync/<NN>.0 for review; pushing is a human decision.

  python sync_versions.py fetch                         get the 17.0/18.0/20.0 branches (a clone has only one)
  python sync_versions.py report   [--version 17] [--check]   what each branch would change (--check: exit 1 on drift)
  python sync_versions.py build    --version 17 --out DIR
  python sync_versions.py snapshot --version 17 --ref origin/17.0   (re)make versions/17 from a branch
  python sync_versions.py apply    --version 17        local branch sync/17.0, never pushed
"""
import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_VERSION = "19"
VERSIONS = ("17", "18", "19", "20")

#: What is version-specific. Everything else is shared and comes from main.
OVERLAY_PATTERNS = (
    ".pylintrc", ".flake8", "README.md",
    "docs/version/*", "gates/check_odoo*_rules.py", "project/*", "pylint_plugins/*", "setup/*",
)
#: Never part of the tools tree.
SKIP_DIRS = {".git", "__pycache__", "versions", ".venv", "node_modules"}
SKIP_FILES = {"local.json"}
SKIP_SUFFIXES = (".pyc", ".vsix", ".log")


def is_overlay(path):
    return any(fnmatch.fnmatch(path, pattern) for pattern in OVERLAY_PATTERNS)


def tools_files(root):
    """Relative posix paths of the tools tree under `root` (no caches, no machine-local files)."""
    found = []
    for base, dirs, files in os.walk(root):
        rel_base = os.path.relpath(base, root).replace(os.sep, "/")
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS
                   and not (rel_base == "kg/config" and d == "local")]
        for name in files:
            if name in SKIP_FILES or name.endswith(SKIP_SUFFIXES):
                continue
            rel = name if rel_base == "." else f"{rel_base}/{name}"
            found.append(rel)
    return sorted(found)


def read_manifest(root, version):
    path = os.path.join(root, "versions", version, "MANIFEST.json")
    if not os.path.isfile(path):
        return {"remove": [], "replace": {}}
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return {"remove": data.get("remove", []), "replace": data.get("replace", {})}


def build_tree(root, version, out, with_versions=False):
    """Write the tools of `version` into the empty directory `out`, from the main tree at `root`.

    with_versions: also copy versions/ - main (the 19.0 branch) carries it, a generated branch does not."""
    os.makedirs(out, exist_ok=True)
    if with_versions:
        versions_dir = os.path.join(root, "versions")
        if os.path.isdir(versions_dir):
            for rel in tools_files(versions_dir):
                copy(os.path.join(versions_dir, *rel.split("/")), os.path.join(out, "versions", *rel.split("/")))
    manifest = read_manifest(root, version)
    removed = set(manifest["remove"])
    for rel in tools_files(root):
        if version != MAIN_VERSION and rel in removed:
            continue
        copy(os.path.join(root, *rel.split("/")), os.path.join(out, *rel.split("/")))
    overlay_root = os.path.join(root, "versions", version, "files")
    if version != MAIN_VERSION and os.path.isdir(overlay_root):
        for rel in tools_files(overlay_root):
            copy(os.path.join(overlay_root, *rel.split("/")), os.path.join(out, *rel.split("/")))
    for rel, pairs in manifest["replace"].items():
        target = os.path.join(out, *rel.split("/"))
        with open(target, "rb") as handle:
            text = handle.read().decode("utf-8")
        for old, new in pairs:
            if old not in text:
                raise SystemExit(f"versions/{version}/MANIFEST.json: '{old}' is not in {rel} any more")
            text = text.replace(old, new)
        with open(target, "wb") as handle:
            handle.write(text.encode("utf-8"))
    return tools_files(out)


def copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)


# ----------------------------------------------------------------- git (read-only unless `apply`)
def git(repo, *args, env=None, text=True):
    result = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=text, env=env, check=False)
    if result.returncode:
        raise SystemExit(f"git {' '.join(args)}: {(result.stderr or '').strip() if text else result.stderr.decode()}")
    return result.stdout


def ref_files(repo, ref):
    if subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref], capture_output=True,
                      check=False).returncode:
        raise SystemExit(f"sync-versions: {ref} is not in this clone. Run `odoo-ai sync-versions fetch` first "
                         f"(it only downloads the other version branches).")
    return [p for p in git(repo, "ls-tree", "-r", "--name-only", ref).splitlines() if p]


def ref_blob(repo, ref, path):
    return subprocess.run(["git", "-C", repo, "show", f"{ref}:{path}"], capture_output=True, check=True).stdout


def snapshot(root, version, ref, repo):
    """Make versions/<version> from the branch `ref` of `repo`: its overlay files and what main has but it lacks."""
    branch_files = [p for p in ref_files(repo, ref) if is_overlay(p)]
    main_overlay = [p for p in tools_files(root) if is_overlay(p)]
    target = os.path.join(root, "versions", version)
    replace = read_manifest(root, version)["replace"]   # hand-written, survives a new snapshot
    shutil.rmtree(target, ignore_errors=True)
    for rel in branch_files:
        dest = os.path.join(target, "files", *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as handle:
            handle.write(ref_blob(repo, ref, rel))
    remove = sorted(set(main_overlay) - set(branch_files))
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "MANIFEST.json"), "w", encoding="utf-8") as handle:
        json.dump({"remove": remove, "replace": replace}, handle, indent=2)
        handle.write("\n")
    return len(branch_files), len(remove)


def compare_with_ref(repo, ref, built_dir):
    """(only_in_ref, only_in_built, changed) path lists between the branch and the built tree."""
    # versions/ lives in main (and so in the 19.0 branch); it is never part of a built tree
    ref_set = {p for p in ref_files(repo, ref) if not p.startswith("versions/")}
    built = set(tools_files(built_dir))
    changed = []
    for rel in sorted(ref_set & built):
        with open(os.path.join(built_dir, *rel.split("/")), "rb") as handle:
            if handle.read().replace(b"\r\n", b"\n") != ref_blob(repo, ref, rel).replace(b"\r\n", b"\n"):
                changed.append(rel)
    return sorted(ref_set - built), sorted(built - ref_set), changed


def assert_tree_passes(built_dir, version):
    """No branch is made from a tree whose own tests fail under the CI's conditions (tools_ci)."""
    import tools_ci   # imported here: tools_ci imports this module
    ok, text, output = tools_ci.run_tree(built_dir)
    if not ok:
        print("\n".join(output.splitlines()[-40:]))
        raise SystemExit(f"sync-versions: the {version}.0 tools fail their own tests ({text}); no branch was made. "
                         f"Fix main first (`odoo-ai tools-ci` shows the same run).")
    print(f"   {version}.0 tools pass under the CI's conditions ({text})")


def apply_local_branch(repo, ref, built_dir, version):
    """Commit the built tree on top of `ref` as local branch sync/<version>.0. Never pushes, never touches HEAD."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmp, "index"), GIT_WORK_TREE=built_dir)
        git(repo, "read-tree", "--empty", env=env)
        git(repo, "add", "-A", "--force", ".", env=dict(env, GIT_DIR=git(repo, "rev-parse", "--absolute-git-dir").strip()))
        tree = git(repo, "write-tree", env=env).strip()
    message = f"[REF] tools: {version}.0 generated from main (sync-versions)"
    commit = git(repo, "commit-tree", tree, "-p", ref, "-m", message).strip()
    git(repo, "update-ref", f"refs/heads/sync/{version}.0", commit)
    return commit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["report", "build", "snapshot", "apply", "fetch"])
    parser.add_argument("--version", "-v", choices=VERSIONS)
    parser.add_argument("--root", default=TOOLS_DIR, help="the main tree (default: these tools)")
    parser.add_argument("--repo", default=None, help="git repository holding the <NN>.0 branches (default: --root)")
    parser.add_argument("--ref", default=None, help="branch to compare with / snapshot from (default: origin/<NN>.0)")
    parser.add_argument("--out", default=None, help="build: output directory")
    parser.add_argument("--check", action="store_true", help="report: exit 1 when a branch differs from what main builds")
    parser.add_argument("--no-test", action="store_true", help="apply: do not run the tree's tests first (not advised)")
    args = parser.parse_args(argv)
    repo = args.repo or args.root
    versions = [args.version] if args.version else list(VERSIONS)

    if args.action == "fetch":
        git(repo, "fetch", "origin", "+refs/heads/*:refs/remotes/origin/*")
        print("fetched: " + ", ".join(sorted(b.strip() for b in git(repo, "branch", "-r").splitlines()
                                              if "->" not in b)))
        return 0
    if args.action == "snapshot":
        if not args.version or args.version == MAIN_VERSION:
            raise SystemExit(f"snapshot needs --version of 17, 18 or 20: {MAIN_VERSION} is main itself")
        files, removed = snapshot(args.root, args.version, args.ref or f"origin/{args.version}.0", repo)
        print(f"versions/{args.version}: {files} overlay files, {removed} removed")
        return 0
    if args.action == "build":
        if not args.version or not args.out:
            raise SystemExit("build needs --version and --out")
        print(f"{len(build_tree(args.root, args.version, args.out))} files in {args.out}")
        return 0
    drift = 0
    for version in versions:
        ref = args.ref or f"origin/{version}.0"
        with tempfile.TemporaryDirectory() as out:
            build_tree(args.root, version, out)
            gone, new, changed = compare_with_ref(repo, ref, out)
            drift += len(gone) + len(new) + len(changed)
            print(f"{version}.0 vs {ref}: {len(changed)} changed, {len(new)} new, {len(gone)} not in main-built tree")
            if args.action == "report":
                for label, paths in (("changed", changed), ("new", new), ("dropped", gone)):
                    for path in paths:
                        print(f"   {label:8} {path}")
            else:
                if not args.no_test:
                    assert_tree_passes(out, version)
                print(f"   local branch sync/{version}.0 = {apply_local_branch(repo, ref, out, version)[:8]} (not pushed)")
    return 1 if args.check and drift else 0


if __name__ == "__main__":
    sys.exit(main())
