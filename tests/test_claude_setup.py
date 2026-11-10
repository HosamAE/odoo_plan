import json
import os
import sys
import tempfile
import unittest

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import claude_setup  # noqa: E402


class TestClaudeSetup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = self._tmp.name
        os.makedirs(os.path.join(self.project, ".git", "info"))

    def settings(self):
        with open(os.path.join(self.project, ".claude", "settings.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def test_install_writes_hooks_permissions_guard_and_skill(self):
        added = claude_setup.install(self.project, sys.executable)
        self.assertTrue(added)
        hooks = self.settings()["hooks"]
        self.assertEqual(len(hooks["PreToolUse"]), 2)
        self.assertEqual(len(hooks["PostToolUse"]), 1)
        self.assertIn("claude_hooks.py", hooks["PreToolUse"][0]["hooks"][0]["command"])
        self.assertIn("Bash(odoo-ai doctor:*)", self.settings()["permissions"]["allow"])
        self.assertIn("Read(./*.conf)", self.settings()["permissions"]["deny"])
        self.assertTrue(os.path.isfile(os.path.join(self.project, ".claude", "odoo-ai-guard.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.project, ".claude", "skills", "odoo-ai", "SKILL.md")))

    def test_running_it_again_changes_nothing(self):
        claude_setup.install(self.project, sys.executable)
        first = self.settings()
        self.assertEqual(claude_setup.install(self.project, sys.executable), [])
        self.assertEqual(self.settings(), first)

    def test_existing_settings_are_merged_not_replaced(self):
        os.makedirs(os.path.join(self.project, ".claude"))
        mine = {"model": "sonnet", "permissions": {"allow": ["Bash(npm test:*)"]},
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}]}}
        with open(os.path.join(self.project, ".claude", "settings.json"), "w", encoding="utf-8") as handle:
            json.dump(mine, handle)
        claude_setup.install(self.project, sys.executable)
        merged = self.settings()
        self.assertEqual(merged["model"], "sonnet")
        self.assertIn("Bash(npm test:*)", merged["permissions"]["allow"])
        self.assertEqual(merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "echo mine")
        self.assertEqual(len(merged["hooks"]["PreToolUse"]), 3)

    def test_machine_files_are_kept_out_of_git(self):
        claude_setup.install(self.project, sys.executable)
        with open(os.path.join(self.project, ".git", "info", "exclude"), encoding="utf-8") as handle:
            excluded = handle.read().splitlines()
        for line in claude_setup.EXCLUDES:
            self.assertIn(line, excluded)

    def test_check_reports_what_is_missing(self):
        self.assertIn("missing", claude_setup.check(self.project)[0])
        claude_setup.install(self.project, sys.executable)
        self.assertEqual(claude_setup.check(self.project), [])


if __name__ == "__main__":
    unittest.main()
