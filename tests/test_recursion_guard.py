"""A loop of odoo-ai commands must stop by itself and say where it turns (it once filled a machine with processes)."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import recursion_guard  # noqa: E402

CLEAN = {k: v for k, v in os.environ.items() if not k.startswith("ODOO_AI_")}


class TestGuard(unittest.TestCase):
    def test_a_normal_chain_is_counted_and_named(self):
        with mock.patch.dict(os.environ, CLEAN, clear=True):
            self.assertEqual(recursion_guard.enter("launcher new-version"), 1)
            self.assertEqual(recursion_guard.enter("cli setup"), 2)
            self.assertEqual(os.environ["ODOO_AI_CHAIN"], "launcher new-version > cli setup")

    def test_a_loop_stops_at_the_limit_and_names_the_chain(self):
        env = dict(CLEAN, ODOO_AI_MAX_DEPTH="3")
        with mock.patch.dict(os.environ, env, clear=True):
            for label in ("a", "b", "c"):
                recursion_guard.enter(label)
            with self.assertRaises(SystemExit) as caught:
                recursion_guard.enter("d")
        self.assertIn("a loop", str(caught.exception))
        self.assertIn("a > b > c > d", str(caught.exception))

    def test_a_refusal_leaves_a_trace_nobody_can_swallow(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "guard.log")
            env = dict(CLEAN, ODOO_AI_MAX_DEPTH="1", ODOO_AI_GUARD_LOG=log)
            with mock.patch.dict(os.environ, env, clear=True):
                recursion_guard.enter("first")
                with self.assertRaises(SystemExit):
                    recursion_guard.enter("second")
            with open(log, encoding="utf-8") as handle:
                self.assertIn("first > second", handle.read())

    def test_the_real_commands_refuse_too_deep_a_chain(self):
        env = dict(CLEAN, ODOO_AI_DEPTH="50", ODOO_AI_MAX_DEPTH="8")
        for argv in ([os.path.join(TOOLS_DIR, "cli.py"), "doctor"],
                     [os.path.join(TOOLS_DIR, "launcher", "odoo-ai.py"), "doctor"]):
            done = subprocess.run([sys.executable, *argv], env=dict(os.environ, **env), capture_output=True, text=True,
                                  timeout=60, check=False)
            self.assertNotEqual(done.returncode, 0, argv)
            self.assertIn("refusing to start", done.stderr + done.stdout, argv)


class TestInitFastDbHelp(unittest.TestCase):
    """`init-fast-db --help` once started `odoo-bin -i base` for real, against the machine's PostgreSQL."""

    def test_help_shows_usage_and_builds_nothing(self):
        done = subprocess.run([sys.executable, os.path.join(TOOLS_DIR, "cli.py"), "init-fast-db", "--help"],
                              capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage: odoo-ai init-fast-db", done.stdout)
        self.assertNotIn("[FAST-TEST]", done.stdout, "it started building the template database")


if __name__ == "__main__":
    unittest.main()
