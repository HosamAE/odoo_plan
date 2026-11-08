"""odoo-ai gates [--install]: the version's gates in a project's own repository.

`odoo-ai ci` already runs tools/gates on every project of this version. For
GitHub's CI to run them too, the project needs its own copy in .github/scripts
and a step in ci.yml - that is what --install does (the copy; the ci.yml lines
are printed, because a workflow is the project's to write).

Without --install it only says which gates the project has, which are older
than the tools' and which are missing.
"""
import filecmp
import os
import shutil

from workspace_sync import TOOLS_DIR, fs, resolve_project, project_dir

GATES = os.path.join(TOOLS_DIR, "gates")

CI_STEPS = {
    "check_python_names.py": ("No undefined names, no unused imports, the right translator",
                              "pip install --quiet pyflakes==3.4.0\n          python .github/scripts/check_python_names.py"),
    "check_odoo19_rules.py": ("The Odoo 19 rules hold", "python .github/scripts/check_odoo19_rules.py"),
    "check_dependencies.py": ("Every module depends on what it uses (needs the Odoo source at ./odoo)",
                              "python .github/scripts/check_dependencies.py --odoo odoo"),
    "check_test_log.py": ("No framework warnings from our modules in the Odoo log (tests job, after the run)",
                          "python .github/scripts/check_test_log.py odoo-test.log"),
}


def run_gates(path=".", install=False):
    project = resolve_project(path)
    if not project:
        raise SystemExit(f"{os.path.abspath(path)} is not inside C:\\odoo\\odoo<NN>\\projects\\<project>")
    version, name, _pid, _cfg = project
    repo = fs(project_dir(version, name))
    target = os.path.join(repo, ".github", "scripts")
    files = sorted(f for f in os.listdir(GATES) if f.endswith(".py"))
    print(f"[GATES] Odoo {version} / {name}: tools/gates -> .github/scripts" + ("" if install else "   (report only)"))
    changed = []
    for f in files:
        mine = os.path.join(target, f)
        if not os.path.isfile(mine):
            state = "missing"
        elif filecmp.cmp(os.path.join(GATES, f), mine, shallow=False):
            state = "same"
        else:
            state = "differs"
        print(f"  {state:8} {f}")
        if state != "same":
            changed.append(f)
    if install and changed:
        os.makedirs(target, exist_ok=True)
        for f in changed:
            shutil.copy2(os.path.join(GATES, f), os.path.join(target, f))
        print(f"\n  copied {len(changed)} file(s). Commit them with the project.")
        print("  ci.yml steps for the ones the workflow does not run yet:")
        for f in changed:
            key = "check_odoo19_rules.py" if f.startswith("check_odoo") and f.endswith("_rules.py") else f
            if key in CI_STEPS:
                title, run = CI_STEPS[key]
                run = run.replace("check_odoo19_rules.py", f)
                print(f"\n      - name: {title}\n        run: |\n          {run}")
    elif not changed:
        print("\n  the project carries the tools' gates as they are.")
    return 0
