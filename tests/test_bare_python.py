"""Every tool must import, and every command must show its help, on a Python with none of Odoo's packages.

`db_snap.py` imported psycopg2 at the top, so `odoo-ai db-snap --help` died on a clean CI runner while it worked on the
machine that has Odoo installed. A clean runner is the normal case for anyone setting up a new version, so this is
tested for all tools at once: the Odoo packages are blocked and nothing may fail on them at import or `--help`.
"""
import glob
import os
import re
import subprocess
import sys
import tempfile
import unittest

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import tools_ci  # noqa: E402


def commands():
    with open(os.path.join(TOOLS_DIR, "cli.py"), encoding="utf-8") as handle:
        source = handle.read()
    names = set(re.findall(r'add_parser\(\s*"([a-z0-9-]+)"', source))
    names |= set(re.findall(r'\(\s*"([a-z0-9-]+)",\s*"[A-Z`][^"]*"\)', source))
    return sorted(names)


class TestBarePython(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.shim = tools_ci.make_shim(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_python(self, *args):
        env = tools_ci.clean_env()
        env["PYTHONPATH"] = self.shim          # a normal (not isolated) run: the script's own folder is on the path
        return subprocess.run([sys.executable, *args], cwd=TOOLS_DIR, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False)

    def test_the_shim_really_blocks_what_a_runner_lacks(self):
        for name in tools_ci.BLOCKED:
            result = self.run_python("-c", f"import {name}")
            self.assertNotEqual(result.returncode, 0, f"{name} was importable: the check below would prove nothing")
            self.assertIn("blocked", result.stderr)

    def test_every_top_level_tool_imports(self):
        modules = sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(TOOLS_DIR, "*.py")))
        code = ("import importlib, sys; sys.path.insert(0, '.'); bad = []\n"
                "for name in sys.argv[1:]:\n"
                "    try:\n        importlib.import_module(name)\n"
                "    except BaseException as error:\n        bad.append(f'{name}: {type(error).__name__}: {error}')\n"
                "print('\\n'.join(bad)); sys.exit(1 if bad else 0)")
        result = self.run_python("-c", code, *modules)
        self.assertEqual(result.returncode, 0, f"these tools need Odoo's packages just to be imported:\n{result.stdout}")

    def test_every_command_shows_its_help(self):
        failed = []
        for command in commands():
            result = self.run_python("cli.py", command, "--help")
            if result.returncode != 0:
                failed.append(f"{command}: {result.stderr.strip().splitlines()[-1] if result.stderr.strip() else '?'}")
        self.assertEqual(failed, [], "commands that cannot even print their help without Odoo's packages")


class TestRunTree(unittest.TestCase):
    """run_tree is what sync-versions apply and the pre-push hook trust."""

    def tree(self, test_body):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        os.makedirs(os.path.join(folder.name, "tests"))
        with open(os.path.join(folder.name, "tests", "test_x.py"), "w", encoding="utf-8") as handle:
            handle.write("import os, unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n" + test_body + "\n")
        return folder.name

    def test_a_passing_tree_passes(self):
        ok, text, _out = tools_ci.run_tree(self.tree("        self.assertTrue(True)"))
        self.assertTrue(ok)
        self.assertIn("1 test", text)

    def test_the_tree_itself_is_left_untouched(self):
        # the tree may become a branch: running its tests must not leave caches or anything else in it
        tree = self.tree("        open('written_by_a_test.txt', 'w').close()")
        before = sorted(os.listdir(tree))
        tools_ci.run_tree(tree)
        self.assertEqual(sorted(os.listdir(tree)), before)
        self.assertFalse([d for d, _s, _f in os.walk(tree) if os.path.basename(d) == "__pycache__"])

    def test_a_failing_tree_fails(self):
        self.assertFalse(tools_ci.run_tree(self.tree("        self.fail('boom')"))[0])

    def test_a_test_that_needs_psycopg2_fails_like_it_would_on_a_runner(self):
        self.assertFalse(tools_ci.run_tree(self.tree("        import psycopg2"))[0])

    def test_the_runners_environment_is_clean(self):
        body = "        self.assertNotIn('ODOO_ROOT', os.environ)\n        self.assertNotIn('ODOO_AI_SKIP_REHEARSAL', os.environ)"
        os.environ["ODOO_ROOT"] = "C:/somewhere"
        self.addCleanup(os.environ.pop, "ODOO_ROOT", None)
        self.assertTrue(tools_ci.run_tree(self.tree(body))[0], "the machine's ODOO_ROOT leaked into the check")


class TestApplyIsGuarded(unittest.TestCase):
    def test_a_failing_tree_makes_no_branch(self):
        from unittest import mock
        import sync_versions
        with mock.patch.object(tools_ci, "run_tree", return_value=(False, "3 tests", "FAILED: boom")):
            with self.assertRaises(SystemExit) as caught:
                sync_versions.assert_tree_passes("somewhere", "17")
        self.assertIn("no branch was made", str(caught.exception))

    def test_a_passing_tree_goes_through(self):
        from unittest import mock
        import sync_versions
        with mock.patch.object(tools_ci, "run_tree", return_value=(True, "100 tests", "OK")):
            sync_versions.assert_tree_passes("somewhere", "17")


if __name__ == "__main__":
    unittest.main()
