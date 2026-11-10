"""
Central Root & Path Resolution Engine for Odoo Studio & CI Pro.
Dynamically resolves the Odoo root directory without any hardcoded 'C:\\odoo'.
Supports:
  1. ODOO_ROOT environment variable (if set)
  2. Automatic derivation relative to this tools package (root/odoo<NN>/tools -> root)
  3. Fallback to C:\\odoo if completely detached
"""
import os
import sys

def get_odoo_root():
    """Return the absolute path to the Odoo root directory."""
    # 1. Environment variable override
    env_root = os.environ.get("ODOO_ROOT")
    if env_root and os.path.isdir(env_root):
        return os.path.abspath(env_root)

    # 2. Derive relative to this file:
    # This file is at: <root>/odoo<NN>/tools/env_root.py
    # Up 1 level: tools
    # Up 2 levels: odoo<NN>
    # Up 3 levels: <root>
    try:
        tools_dir = os.path.dirname(os.path.abspath(__file__))
        version_dir = os.path.dirname(tools_dir)
        candidate_root = os.path.dirname(version_dir)
        if os.path.isdir(candidate_root) and any(
            os.path.exists(os.path.join(candidate_root, f"odoo{v}")) for v in ("17", "18", "19", "20")
        ):
            return os.path.abspath(candidate_root)
    except Exception:
        pass

    # 3. Fallback
    if os.name == "nt":
        return r"C:\odoo"
    return os.path.expanduser("~/odoo")

def get_bin_dir():
    """Return the shared bin directory."""
    return os.path.join(get_odoo_root(), "bin")

def get_version_dir(version="19"):
    """Return the directory for a specific Odoo version (e.g. root/odoo19)."""
    return os.path.join(get_odoo_root(), f"odoo{version}")

def get_projects_dir(version="19"):
    """Return the projects root directory (e.g. root/ProjectsOdoo19)."""
    return os.path.join(get_odoo_root(), f"ProjectsOdoo{version}")

def get_venv_python(version="19"):
    """Return the virtualenv python interpreter path for the given version."""
    vdir = get_version_dir(version)
    if os.name == "nt":
        return os.path.join(vdir, "venv", "Scripts", "python.exe")
    return os.path.join(vdir, "venv", "bin", "python")

if __name__ == "__main__":
    print("Resolved ODOO_ROOT:", get_odoo_root())
    print("Bin Dir:", get_bin_dir())
    print("Odoo 19 Dir:", get_version_dir("19"))
    print("Projects 19 Dir:", get_projects_dir("19"))
    print("Python 19 Path:", get_venv_python("19"))

