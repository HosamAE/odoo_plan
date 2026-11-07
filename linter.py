"""
Odoo AI Linter Engine
Runs pylint-odoo, flake8, and XML syntax checks on target module.
"""

import os
import subprocess
import sys
import xml.etree.ElementTree as ET

def find_linter_tools(version="17"):
    venv_scripts = f"C:\\odoo\\odoo{version}\\venv\\Scripts"
    pylint_exe = os.path.join(venv_scripts, "pylint.exe")
    flake8_exe = os.path.join(venv_scripts, "flake8.exe")
    rcfile = f"C:\\odoo\\odoo{version}\\.pylintrc"
    
    return pylint_exe, flake8_exe, rcfile

def check_xml_syntax(module_path):
    xml_errors = []
    for root, _, files in os.walk(module_path):
        for file in files:
            if file.endswith(".xml"):
                full_path = os.path.join(root, file)
                try:
                    ET.parse(full_path)
                except ET.ParseError as pe:
                    xml_errors.append(f"[XML SYNTAX ERROR] {full_path}: {pe}")
    return xml_errors

def odoo_env(version):
    """The environment pylint needs to import odoo (no false import errors)."""
    env = dict(os.environ)
    source = f"C:\\odoo\\odoo{version}\\odoo"
    if os.path.isdir(source):
        env["PYTHONPATH"] = os.pathsep.join(p for p in (source, env.get("PYTHONPATH")) if p)
    return env


def run_linter(module_path, version="17"):
    pylint_exe, flake8_exe, rcfile = find_linter_tools(version)
    results = {"pylint": "", "flake8": "", "xml_errors": []}

    print(f"[LINTER] Linting Odoo Module at: '{module_path}' (Version {version})...")

    # 1. XML Syntax Check
    results["xml_errors"] = check_xml_syntax(module_path)
    
    # 2. Pylint Odoo Check
    if os.path.exists(pylint_exe):
        # Full paths, so an editor can open each finding.
        cmd = [pylint_exe, module_path, "--load-plugins=pylint_odoo",
               "--msg-template={abspath}:{line}:{column}: {msg_id}: {msg} ({symbol})"]
        if os.path.exists(rcfile):
            cmd.append(f"--rcfile={rcfile}")
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=odoo_env(version))
            results["pylint"] = res.stdout
        except Exception as e:
            results["pylint"] = f"Pylint Execution Error: {e}"
    else:
        results["pylint"] = f"Pylint not found at {pylint_exe}. Run setup to install pylint-odoo."

    # 3. Flake8 Check
    if os.path.exists(flake8_exe):
        cmd = [flake8_exe, module_path]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            results["flake8"] = res.stdout
        except Exception as e:
            results["flake8"] = f"Flake8 Execution Error: {e}"

    return results
