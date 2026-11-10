"""kg_open: the task VS Code runs when a project opens. A build killed half way leaves a lock; it must not
turn into "Task has errors" every morning - and a lock whose builder is alive must never be released."""
import os
import sys
import unittest
from unittest import mock

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import kg_open  # noqa: E402

# what `odoo_kg unlock --show` printed on the machine when this happened
ABANDONED = """project         acme-odoo19
lock file       C:\\Users\\x\\AppData\\Local\\odoo-kg\\projects\\acme-odoo19\\build.lock
lock_id         13769d161368406b9e2014eb196557ea
pid             135792
host            HoOsS  (this machine)
started         2026-11-10T17:18:42.005637+00:00
age             36.3 minutes
indexer         0.4.0
holder          gone
looks abandoned True
"""
ALIVE = ABANDONED.replace("holder          gone", "holder          alive").replace("abandoned True", "abandoned False")
OTHER_MACHINE = ABANDONED.replace("(this machine)", "(another machine)")
LOCKED = ("graph stale_worktree: rebuilding ...\nerror: another build already holds C:\\x\\build.lock. "
          "Lock 13769d16 was taken by pid 135792 on HoOsS 33.2 minutes ago; that process is gone.\n")
STALE = "state           stale_worktree\ngraph commit    abc\n"
PUBLISHED = "published       yes\nduration        18.6s\n"


class TestAbandonedLock(unittest.TestCase):
    def test_a_lock_whose_builder_is_gone_is_found(self):
        self.assertEqual(kg_open.abandoned_lock(ABANDONED), "13769d161368406b9e2014eb196557ea")

    def test_a_live_builder_is_never_touched(self):
        self.assertIsNone(kg_open.abandoned_lock(ALIVE))

    def test_a_lock_from_another_machine_is_never_touched(self):
        self.assertIsNone(kg_open.abandoned_lock(OTHER_MACHINE))

    def test_nothing_to_release_when_there_is_no_lock_output(self):
        self.assertIsNone(kg_open.abandoned_lock(""))


class TestMain(unittest.TestCase):
    def run_main(self, answers):
        """answers: {action: (code, output)} - the engine, mocked. Returns (exit code, actions run)."""
        calls = []

        def fake_run(action, config):
            calls.append(action)
            result = answers[action]
            return result.pop(0) if isinstance(result, list) else result

        with mock.patch.object(kg_open, "_run", fake_run):
            with mock.patch("builtins.print"):
                return kg_open.main(["--config", "x.toml"]), calls

    def test_a_dead_builders_lock_is_released_and_the_graph_rebuilt(self):
        code, calls = self.run_main({"status": (1, STALE), "index": [(1, LOCKED), (0, PUBLISHED)],
                                     "unlock --show": (0, ABANDONED),
                                     "unlock --lock-id 13769d161368406b9e2014eb196557ea": (0, "released")})
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["status", "index", "unlock --show", "unlock --lock-id 13769d161368406b9e2014eb196557ea", "index"])

    def test_a_live_builders_lock_is_left_and_the_task_fails_honestly(self):
        code, calls = self.run_main({"status": (1, STALE), "index": (1, LOCKED), "unlock --show": (0, ALIVE)})
        self.assertEqual(code, 1)
        self.assertNotIn("unlock --lock-id", " ".join(calls))

    def test_a_fresh_graph_needs_nothing(self):
        code, calls = self.run_main({"status": (0, "state           fresh\ngraph commit    abcdef123456789\n")})
        self.assertEqual((code, calls), (0, ["status"]))


if __name__ == "__main__":
    unittest.main()
