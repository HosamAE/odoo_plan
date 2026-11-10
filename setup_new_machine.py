"""
Odoo Studio & CI Pro — New Machine Automator (setup_new_machine.py)
Automates setup of the complete Odoo workspace architecture on any fresh machine in seconds.
Conceived & Architected by HosamAE.
"""
import argparse
import os
import shutil
import subprocess
import sys
from env_root import get_odoo_root

BASE_DIR = get_odoo_root()
BIN_DIR = os.path.join(BASE_DIR, "bin")

def log(msg, status="INFO"):
    colors = {
        "INFO": "\033[94m",
        "OK": "\033[92m",
        "WARN": "\033[93m",
        "ERR": "\033[91m",
    }
    reset = "\033[0m"
    print(f"{colors.get(status, '')}[{status}]{reset} {msg}")

def ensure_directory_structure(version="19"):
    log(f"Setting up unified directory layout for Odoo {version}...", "INFO")
    dirs = [
        BASE_DIR,
        BIN_DIR,
        os.path.join(BASE_DIR, f"odoo{version}"),
        os.path.join(BASE_DIR, f"ProjectsOdoo{version}"),
    ]
    for d in dirs:
        if not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
            log(f"Created: {d}", "OK")
        else:
            log(f"Verified: {d}", "OK")

def add_to_system_path():
    log("Verifying C:\\odoo\\bin in PATH...", "INFO")
    path_env = os.environ.get("PATH", "")
    if BIN_DIR.lower() not in path_env.lower():
        log(f"Adding {BIN_DIR} to User PATH...", "INFO")
        cmd = f'[Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path", "User") + ";{BIN_DIR}", "User")'
        subprocess.run(["powershell", "-Command", cmd], capture_output=True)
        log(f"Successfully added {BIN_DIR} to System/User PATH. (Restart terminal if needed)", "OK")
    else:
        log("C:\\odoo\\bin is already registered in PATH.", "OK")

def install_vscode_extension():
    log("Checking VS Code installation...", "INFO")
    code = shutil.which("code") or shutil.which("code.cmd")
    if not code:
        log("VS Code CLI ('code') not found in PATH. Skipping auto-extension install.", "WARN")
        return

    vsix_path = os.path.join(BASE_DIR, "odoo19", "tools", "vscode-extension", "odoo-ci-1.4.0.vsix")
    if os.path.exists(vsix_path):
        log(f"Installing Odoo Studio & CI Pro extension ({vsix_path})...", "INFO")
        res = subprocess.run([code, "--install-extension", vsix_path, "--force"], capture_output=True, text=True)
        if res.returncode == 0:
            log("VS Code extension (HosamAE.odoo-ci) installed successfully!", "OK")
        else:
            log(f"Extension install warning: {res.stderr or res.stdout}", "WARN")

def initialize_project_plans(version="19"):
    tmpl_dir = os.path.join(BASE_DIR, f"odoo{version}", "tools", "docs", "templates")
    roadmap_tmpl_p = os.path.join(tmpl_dir, "ROADMAP_TEMPLATE.md")
    task_tmpl_p = os.path.join(tmpl_dir, "TASK_QUEUE_TEMPLATE.md")

    if not (os.path.exists(roadmap_tmpl_p) and os.path.exists(task_tmpl_p)):
        return

    with open(roadmap_tmpl_p, "r", encoding="utf-8") as f:
        roadmap_tmpl = f.read()
    with open(task_tmpl_p, "r", encoding="utf-8") as f:
        task_tmpl = f.read()

    pdir = os.path.join(BASE_DIR, f"ProjectsOdoo{version}")
    if not os.path.exists(pdir):
        return

    for project in os.listdir(pdir):
        proj_path = os.path.join(pdir, project)
        if os.path.isdir(proj_path):
            plan_dir = os.path.join(proj_path, "plan")
            os.makedirs(plan_dir, exist_ok=True)
            r_path = os.path.join(plan_dir, "ROADMAP.md")
            t_path = os.path.join(plan_dir, "TASK_QUEUE.md")
            if not os.path.exists(r_path):
                with open(r_path, "w", encoding="utf-8") as f:
                    f.write(roadmap_tmpl.format(project_name=project, version=version))
            if not os.path.exists(t_path):
                with open(t_path, "w", encoding="utf-8") as f:
                    f.write(task_tmpl.format(project_name=project, version=version))
    log(f"Verified and synchronized plan/ protocols for ProjectsOdoo{version} (UTF-8).", "OK")

def main():
    parser = argparse.ArgumentParser(description="Odoo Studio & CI Pro — New Machine Automator")
    parser.add_argument("--version", "-v", default="19", choices=["17", "18", "19", "20"], help="Target Odoo version")
    args = parser.parse_args()

    print("\n" + "="*70)
    print("   Odoo Studio & CI Pro — Fresh Machine Setup & Architecture Initializer")
    print("   Engineered by HosamAE (https://github.com/HosamAE/odoo_plan)")
    print("="*70 + "\n")

    ensure_directory_structure(args.version)
    add_to_system_path()
    install_vscode_extension()
    initialize_project_plans(args.version)

    print("\n" + "-"*70)
    log("Setup completed! To verify health, run: odoo-ai doctor", "OK")
    print("-"*70 + "\n")

if __name__ == "__main__":
    main()

