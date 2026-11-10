import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_launcher():
    spec = importlib.util.spec_from_file_location("odoo_ai_launcher", os.path.join(TOOLS_DIR, "launcher", "odoo-ai.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestLauncher(unittest.TestCase):
    def test_version_comes_from_either_layout(self):
        launcher = load_launcher()
        self.assertEqual(launcher.version_of(r"C:\odoo\ProjectsOdoo19\Acme\acme_base"), "19")
        self.assertEqual(launcher.version_of("C:/odoo/odoo18/projects/demo"), "18")
        self.assertEqual(launcher.version_of(r"C:\odoo\ProjectsOdoo20"), "20")
        self.assertIsNone(launcher.version_of(r"C:\work\somewhere"))
        self.assertIsNone(launcher.version_of(r"C:\odoo\bin"))

    def test_root_prefers_the_environment_then_the_bin_folder(self):
        with mock.patch.dict(os.environ, {"ODOO_ROOT": "/somewhere"}):
            self.assertEqual(load_launcher()._root(), "/somewhere")
        env = {k: v for k, v in os.environ.items() if k != "ODOO_ROOT"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertTrue(load_launcher()._root())   # the tools folder is not a bin folder: the fallback

    def test_installed_lists_versions_that_have_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            for version in ("17", "19"):
                os.makedirs(os.path.join(tmp, f"odoo{version}", "tools"))
                open(os.path.join(tmp, f"odoo{version}", "tools", "cli.py"), "w").close()
            os.makedirs(os.path.join(tmp, "odoo18", "tools"))   # no cli.py: not installed
            with mock.patch.dict(os.environ, {"ODOO_ROOT": tmp}):
                self.assertEqual(load_launcher().installed(), ["17", "19"])

    def test_new_version_refuses_an_unknown_version(self):
        with self.assertRaises(SystemExit) as caught:
            load_launcher().new_version(["99"])
        self.assertIn("usage", str(caught.exception))
        with self.assertRaises(SystemExit) as caught:
            load_launcher().new_version([])
        self.assertIn("usage", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
