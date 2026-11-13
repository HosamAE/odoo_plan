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


class Recorder:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, argv, cwd=None, check=False):
        self.calls.append(list(argv))
        code = 1 if self.fail_on and self.fail_on in " ".join(argv) else 0
        return mock.Mock(returncode=code, stdout="", stderr="")


class TestSetupDoesTheWork(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def run_steps(self, check, recorder):
        run = setup_version.Run(check)
        with mock.patch.object(setup_version.subprocess, "run", recorder), mock.patch.object(setup_version, "_missing_packages", return_value=[]):
            setup_version.step_sources(run, SPEC, self.root)
            setup_version.step_venv(run, SPEC, self.root, "C:/Python312/python.exe")
        return run

    def test_check_changes_nothing(self):
        recorder = Recorder()
        run = self.run_steps(True, recorder)
        self.assertEqual(recorder.calls, [])
        self.assertEqual(run.todo, 2)   # the clone and the venv are announced as to do

    def test_setup_clones_the_required_source_and_not_the_optional_one(self):
        recorder = Recorder()
        self.run_steps(False, recorder)
        clones = [c for c in recorder.calls if c[:2] == ["git", "clone"]]
        self.assertEqual(len(clones), 1)
        self.assertIn("https://example.invalid/odoo.git", clones[0])
        self.assertFalse(any("enterprise" in " ".join(c) for c in recorder.calls))

    def test_setup_creates_the_venv_then_installs_the_packages(self):
        recorder = Recorder()
        os.makedirs(os.path.join(self.root, "odoo"))
        with open(os.path.join(self.root, "odoo", "requirements.txt"), "w") as handle:
            handle.write("lxml\n")
        os.makedirs(os.path.join(self.root, "enterprise"))
        with open(os.path.join(self.root, "enterprise", "x"), "w") as handle:
            handle.write("x")
        self.run_steps(False, recorder)
        venv = [c for c in recorder.calls if "venv" in c]
        self.assertEqual(len(venv), 1)
        self.assertEqual(venv[0][:3], ["C:/Python312/python.exe", "-m", "venv"])

    def test_a_failed_step_is_counted_not_called_done(self):
        recorder = Recorder(fail_on="clone")
        run = self.run_steps(False, recorder)
        self.assertEqual(run.failed, 1)


if __name__ == "__main__":
    unittest.main()
