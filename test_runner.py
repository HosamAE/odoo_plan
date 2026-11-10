"""
Odoo AI Test Runner Engine
Runs Odoo unittests via correct python virtual environment, captures tracebacks & logs,
supports test filtering via --test-tags, and measures precise execution duration.
"""

import os
import subprocess
import sys
import time
from env_root import get_odoo_root


def find_odoo_paths(version="19"):
    base_dir = os.path.join(get_odoo_root(), f"odoo{version}")
    python_exe = os.path.join(base_dir, "venv", "Scripts", "python.exe")
    odoo_bin = os.path.join(base_dir, "odoo", "odoo-bin")
    
    if not os.path.exists(python_exe):
        python_exe = sys.executable
        
    return python_exe, odoo_bin


def run_odoo_test(module_name, version="19", db_name="test_db", config_file=None, tags=None):
    python_exe, odoo_bin = find_odoo_paths(version)
    
    if not os.path.exists(odoo_bin):
        return {
            "success": False,
            "error": f"Odoo bin executable not found at {odoo_bin}",
            "traceback": "",
            "duration": 0
        }

    cmd = [
        python_exe,
        odoo_bin,
        "--test-enable",
        "-i", module_name,
        "--stop-after-init",
        "-d", db_name,
        "--log-level=test"
    ]
    
    if tags:
        cmd.extend(["--test-tags", tags])

    if config_file and os.path.exists(config_file):
        cmd.extend(["-c", config_file])

    tag_info = f" [tags: {tags}]" if tags else ""
    print(f"[TEST RUNNER] Running Odoo {version} Unit Tests for module: '{module_name}'{tag_info}...")
    print(f"Command: {' '.join(cmd)}\n")

    t0 = time.perf_counter()
    try:
        process = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=300
        )
        duration = round(time.perf_counter() - t0, 2)
        
        output = process.stdout + "\n" + process.stderr
        success = process.returncode == 0 and "ERROR" not in output and "FAIL" not in output
        
        failures = []
        for line in output.splitlines():
            if "ERROR" in line or "FAIL:" in line or "Traceback" in line:
                failures.append(line)
                
        return {
            "success": success,
            "returncode": process.returncode,
            "duration": duration,
            "output": output,
            "failures": "\n".join(failures)
        }
    except subprocess.TimeoutExpired:
        duration = round(time.perf_counter() - t0, 2)
        return {
            "success": False,
            "duration": duration,
            "error": "Test execution timed out after 300 seconds.",
            "failures": "Timeout"
        }
    except Exception as e:
        duration = round(time.perf_counter() - t0, 2)
        return {
            "success": False,
            "duration": duration,
            "error": str(e),
            "failures": str(e)
        }
