import io
import json
import os
import sys
import tempfile
import unittest

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import claude_hooks  # noqa: E402

FREE = {"protected_branches": [], "allow_new_branches": True}
STRICT = {"protected_branches": ["core"], "allow_new_branches": False}


def decision(command, config=FREE):
    return claude_hooks.decide_git(command, config)[0]


class TestGitGuard(unittest.TestCase):
    def test_reading_is_free(self):
        for command in ("git status", "git log --oneline -5", "git diff --stat", "git fetch origin", "git branch",
                        "git branch -r", "git branch --list", "git -C /x status", "git ls-remote origin",
                        "git stash list", "git apply --check p.patch", "git tag", "ls -la", "python x.py"):
            self.assertEqual(decision(command), "allow", command)

    def test_anything_that_writes_asks(self):
        for command in ("git commit -m x", "git push origin testing19", "git pull", "git reset --hard HEAD~1",
                        "git rebase main", "git checkout testing19", "git stash", "git tag v1", "git branch old",
                        "git status && git push origin x", "git clean -fd", "git push --force origin x"):
            self.assertEqual(decision(command), "ask", command)

    def test_destructive_commands_say_why(self):
        reason = claude_hooks.decide_git("git reset --hard HEAD", FREE)[1]
        self.assertIn("discards work", reason)

    def test_skipping_hooks_is_refused(self):
        self.assertEqual(decision("git commit --no-verify -m x"), "deny")
        self.assertEqual(decision("git push --no-verify origin x"), "deny")

    def test_protected_branch_is_refused(self):
        self.assertEqual(decision("git push origin core", STRICT), "deny")
        self.assertEqual(decision("git push origin HEAD:core", STRICT), "deny")
        self.assertEqual(decision("git push origin testing19", STRICT), "ask")
        self.assertEqual(decision("git push origin core-fix", STRICT), "ask")

    def test_new_branches_are_refused_when_the_project_says_so(self):
        for command in ("git checkout -b feat", "git switch -c feat", "git branch feat"):
            self.assertEqual(decision(command, STRICT), "deny", command)
            self.assertEqual(decision(command, FREE), "ask", command)
        self.assertEqual(decision("git branch --list", STRICT), "allow")


class TestHookProtocol(unittest.TestCase):
    def run_hook(self, action, payload):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = claude_hooks.main([action], io.StringIO(json.dumps(payload)))
        finally:
            sys.stdout, sys.stderr = old
        return code, out.getvalue(), err.getvalue()

    def test_git_guard_answers_in_claude_codes_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _err = self.run_hook("git-guard", {"tool_input": {"command": "git push origin x"}, "cwd": tmp})
        self.assertEqual(code, 0)
        data = json.loads(out)["hookSpecificOutput"]
        self.assertEqual((data["hookEventName"], data["permissionDecision"]), ("PreToolUse", "ask"))

    def test_git_guard_stays_silent_for_a_harmless_command(self):
        code, out, _err = self.run_hook("git-guard", {"tool_input": {"command": "git status"}, "cwd": os.getcwd()})
        self.assertEqual((code, out), (0, ""))

    def test_secret_is_denied_and_never_echoed(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"cwd": tmp, "tool_input": {"file_path": os.path.join(tmp, "odoo.conf"),
                                                  "content": "admin_passwd = Sup3rS3cretValue\n"}}
            code, out, _err = self.run_hook("secrets", payload)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("Sup3rS3cretValue", out)

    def test_clean_text_passes_the_secret_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"cwd": tmp, "tool_input": {"file_path": os.path.join(tmp, "odoo.conf"),
                                                  "content": "admin_passwd = admin\nhttp_port = 1900\n"}}
            self.assertEqual(self.run_hook("secrets", payload)[1], "")

    def test_bad_input_never_breaks_the_tool(self):
        out, old = io.StringIO(), sys.stdout
        sys.stdout = out
        try:
            self.assertEqual(claude_hooks.main(["git-guard"], io.StringIO("not json")), 0)
        finally:
            sys.stdout = old


class TestCheckEdit(unittest.TestCase):
    def problems(self, name, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            return claude_hooks.edit_problems(path)

    def test_python_syntax_error_is_reported_with_its_line(self):
        found = self.problems("m.py", "x = 1\ndef broken(:\n")
        self.assertEqual(len(found), 1)
        self.assertRegex(found[0], r"m\.py:2: syntax error")

    def test_xml_that_is_not_well_formed_is_reported(self):
        found = self.problems("v.xml", "<odoo><record></odoo>")
        self.assertEqual(len(found), 1)
        self.assertIn("not well-formed", found[0])

    def test_good_files_are_quiet(self):
        self.assertEqual(self.problems("m.py", "x = 1\n"), [])
        self.assertEqual(self.problems("v.xml", "<odoo/>"), [])

    def test_other_files_are_ignored(self):
        self.assertEqual(self.problems("notes.md", "def broken(:"), [])

    @unittest.skipUnless(claude_hooks.rules_module() is not None and getattr(claude_hooks.rules_module(), "VERSION", "") == "19",
                         "Odoo 19 rules only")
    def test_the_versions_odoo_rules_are_applied(self):
        found = self.problems("m.py", "n = len(self.env['x'].search([]))\n")
        self.assertTrue(any("R3" in line for line in found), found)
        self.assertEqual(self.problems("m.py", "n = len(self.env['x'].search([]))  # noqa: odoo19 test\n"), [])


if __name__ == "__main__":
    unittest.main()
