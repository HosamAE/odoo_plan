import os
import unittest
import sys

# Ensure tools directory is in sys.path
TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from env_root import get_odoo_root, get_bin_dir, get_projects_dir, get_version_dir, get_venv_python


class TestEnvRoot(unittest.TestCase):
    def test_root_is_directory(self):
        root = get_odoo_root()
        self.assertTrue(os.path.isdir(root), f"Odoo root '{root}' must be a directory")

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

