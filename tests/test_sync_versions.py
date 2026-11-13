import json
import os
import subprocess
import sys
import tempfile
import unittest

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import sync_versions  # noqa: E402


def write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def read(root, rel):
    with open(os.path.join(root, *rel.split("/")), encoding="utf-8", newline="") as handle:
        return handle.read()


class TestWhatIsPublished(unittest.TestCase):
    """A file the .gitignore keeps out of git must never reach a generated branch (an estate file and a dashboard of
    local projects once did, on three public branches)."""

    GITIGNORE = "__pycache__/\n*.log\ndocs/index.html\n/local/\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        write(self.root, ".gitignore", self.GITIGNORE)
        write(self.root, "cli.py", "shared\n")
        write(self.root, "docs/INDEX.md", "public\n")
        write(self.root, "docs/index.html", "a dashboard of this machine's projects\n")
        write(self.root, "local/workspace_local.py", "PROJECT_IDS = {}\n")
        write(self.root, "tools/run.log", "log\n")

    def test_without_git_the_gitignore_decides(self):
        self.assertEqual(sync_versions.tools_files(self.root), [".gitignore", "cli.py", "docs/INDEX.md"])

    def test_in_a_git_work_tree_git_decides(self):
        subprocess.run(["git", "-C", self.root, "init", "-q"], check=True)
        write(self.root, "new_tool.py", "not added yet, not ignored\n")
        self.assertEqual(sync_versions.tools_files(self.root), [".gitignore", "cli.py", "docs/INDEX.md", "new_tool.py"])

    def test_a_built_tree_has_no_gitignore_and_is_listed_whole(self):
        out = os.path.join(self._tmp.name, "built")
        write(out, "docs/index.html", "a generated file that happens to have this name\n")
        self.assertEqual(sync_versions.tools_files(out), ["docs/index.html"])


class TestBuildTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.main = os.path.join(self._tmp.name, "main")
        self.out = os.path.join(self._tmp.name, "out")
        write(self.main, "cli.py", "shared\n")
        write(self.main, ".pylintrc", "rc 19\n")
        write(self.main, "gates/check_odoo19_rules.py", "R19\n")
        write(self.main, "gates/check_python_names.py", "TRANSLATOR_RULE = True\n")
        write(self.main, "__pycache__/x.pyc", "junk")
        write(self.main, "local.json", "machine")
        write(self.main, "versions/17/files/.pylintrc", "rc 17\n")
        write(self.main, "versions/17/files/gates/check_odoo17_rules.py", "R17\n")
        write(self.main, "versions/17/MANIFEST.json", json.dumps({
            "remove": ["gates/check_odoo19_rules.py"],
            "replace": {"gates/check_python_names.py": [["TRANSLATOR_RULE = True", "TRANSLATOR_RULE = False"]]},
        }))

    def test_main_version_is_main_as_it_is(self):
        files = sync_versions.build_tree(self.main, sync_versions.MAIN_VERSION, self.out)
        self.assertIn("gates/check_odoo19_rules.py", files)
        self.assertEqual(read(self.out, ".pylintrc"), "rc 19\n")

    def test_other_version_gets_overlay_removal_and_replacement(self):
        files = sync_versions.build_tree(self.main, "17", self.out)
        self.assertEqual(read(self.out, ".pylintrc"), "rc 17\n")
        self.assertIn("gates/check_odoo17_rules.py", files)
        self.assertNotIn("gates/check_odoo19_rules.py", files)
        self.assertEqual(read(self.out, "gates/check_python_names.py"), "TRANSLATOR_RULE = False\n")
        self.assertEqual(read(self.out, "cli.py"), "shared\n")

    def test_caches_machine_files_and_versions_dir_never_ship(self):
        files = sync_versions.build_tree(self.main, "17", self.out)
        self.assertFalse([f for f in files if f.startswith(("__pycache__", "versions")) or f == "local.json"])

    def test_a_stale_replace_fails_loudly(self):
        write(self.main, "gates/check_python_names.py", "TRANSLATOR_RULE = 'changed'\n")
        with self.assertRaises(SystemExit):
            sync_versions.build_tree(self.main, "17", self.out)

    def test_overlay_patterns(self):
        self.assertTrue(sync_versions.is_overlay("gates/check_odoo20_rules.py"))
        self.assertTrue(sync_versions.is_overlay("setup/version.json"))
        self.assertFalse(sync_versions.is_overlay("cli.py"))
        self.assertFalse(sync_versions.is_overlay("gates/check_dependencies.py"))


if __name__ == "__main__":
    unittest.main()
