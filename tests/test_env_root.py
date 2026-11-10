import importlib.util
import os
import shutil
import tempfile
import unittest
import sys
from unittest import mock

# Ensure tools directory is in sys.path
TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from env_root import get_odoo_root, get_bin_dir, get_projects_dir, get_version_dir, get_venv_python


class TestEnvRoot(unittest.TestCase):
    def test_root_is_an_absolute_path(self):
        # no C:\odoo is assumed: a clean CI runner has none, and the fallback is only a last resort
        self.assertTrue(os.path.isabs(get_odoo_root()))

    def load_env_root_from(self, folder):
        spec = importlib.util.spec_from_file_location("env_root_probe", os.path.join(folder, "env_root.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_root_is_found_from_where_the_tools_sit(self):
        # hermetic: a copy of env_root.py placed in <tmp>/odoo19/tools, whatever the real checkout is called
        with tempfile.TemporaryDirectory() as tmp:
            tools = os.path.join(tmp, "odoo19", "tools")
            os.makedirs(tools)
            shutil.copyfile(os.path.join(TOOLS_DIR, "env_root.py"), os.path.join(tools, "env_root.py"))
            env = {k: v for k, v in os.environ.items() if k != "ODOO_ROOT"}
            with mock.patch.dict(os.environ, env, clear=True):
                found = self.load_env_root_from(tools).get_odoo_root()
            self.assertEqual(os.path.normcase(os.path.realpath(found)), os.path.normcase(os.path.realpath(tmp)))

    def test_a_checkout_folder_called_odoo_something_is_not_mistaken_for_a_version_folder(self):
        # the CI checks out to <runner>/odoo_plan/odoo_plan: "odoo_plan" is not odoo<NN>
        with tempfile.TemporaryDirectory() as tmp:
            tools = os.path.join(tmp, "odoo_plan", "odoo_plan")
            os.makedirs(tools)
            shutil.copyfile(os.path.join(TOOLS_DIR, "env_root.py"), os.path.join(tools, "env_root.py"))
            env = {k: v for k, v in os.environ.items() if k != "ODOO_ROOT"}
            with mock.patch.dict(os.environ, env, clear=True):
                found = self.load_env_root_from(tools).get_odoo_root()
            self.assertNotEqual(os.path.normcase(os.path.realpath(found)), os.path.normcase(os.path.realpath(os.path.join(tmp, "odoo_plan"))))

    def test_bin_dir(self):
        bin_dir = get_bin_dir()
        root = get_odoo_root()
        self.assertEqual(bin_dir, os.path.join(root, "bin"))

    def test_projects_dir(self):
        for v in ("17", "18", "19", "20"):
            pdir = get_projects_dir(v)
            root = get_odoo_root()
            self.assertEqual(pdir, os.path.join(root, f"ProjectsOdoo{v}"))

    def test_version_dir(self):
        for v in ("17", "18", "19", "20"):
            vdir = get_version_dir(v)
            root = get_odoo_root()
            self.assertEqual(vdir, os.path.join(root, f"odoo{v}"))

    def test_env_override(self):
        # Setting custom env var overrides root
        fake_path = os.path.abspath(TOOLS_DIR)
        orig = os.environ.get("ODOO_ROOT")
        try:
            os.environ["ODOO_ROOT"] = fake_path
            self.assertEqual(get_odoo_root(), fake_path)
        finally:
            if orig is not None:
                os.environ["ODOO_ROOT"] = orig
            else:
                os.environ.pop("ODOO_ROOT", None)


if __name__ == "__main__":
    unittest.main()

