import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

os.environ.setdefault("ODOO_AI_WORKSPACE_LOCAL", "")   # setup_version imports workspace_sync: the estate of this machine is not under test
import setup_version  # noqa: E402

SPEC = {"version": "19", "python": "3.12", "requirements_extra": "setup/requirements-extra.txt", "repos": [
    {"path": "odoo", "url": "https://example.invalid/odoo.git", "branch": "19.0", "required": True},
    {"path": "enterprise", "url": "https://example.invalid/enterprise.git", "branch": "19.0", "required": False, "note": "needs an account"}]}


class TestSetupLeavesTheSourceAndTheVenvToYou(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def steps(self, check):
        run = setup_version.Run(check)
        out = io.StringIO()
        with mock.patch.object(setup_version.subprocess, "run") as runner, contextlib.redirect_stdout(out):
            setup_version.step_sources(run, SPEC, self.tmp.name)
            setup_version.step_venv(run, SPEC, self.tmp.name, "C:/Python312/python.exe")
        return run, out.getvalue(), runner

    def test_neither_mode_runs_a_clone_or_creates_a_venv(self):
        for check in (True, False):
            _, _, runner = self.steps(check)
            runner.assert_not_called()

    def test_they_are_named_as_yours_with_their_commands_and_not_counted_as_done(self):
        run, text, _ = self.steps(False)
        self.assertEqual((run.todo, run.left), (0, 2))   # the Odoo source and the venv
        self.assertIn("git clone --depth 1 -b 19.0 https://example.invalid/odoo.git", text)
        self.assertIn("-m venv", text)
        self.assertNotIn("[DO]", text)

    def test_an_optional_source_is_only_a_warning(self):
        _, text, _ = self.steps(False)
        self.assertIn("[WARN]  enterprise is missing", text)


if __name__ == "__main__":
    unittest.main()
