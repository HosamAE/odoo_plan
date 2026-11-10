import os
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

    def test_root_is_found_from_the_tools_location_when_they_sit_in_a_version_folder(self):
        version_dir = os.path.dirname(TOOLS_DIR)
        if not os.path.basename(version_dir).lower().startswith("odoo"):
            self.skipTest("these tools are not checked out in <root>/odoo<NN>/tools")
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "ODOO_ROOT"}, clear=True):
            self.assertEqual(os.path.normcase(get_odoo_root()), os.path.normcase(os.path.dirname(version_dir)))

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

