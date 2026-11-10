"""The commands the extension, the docs and the AI rules call must exist in cli.py (a missing one was found by hand)."""
import os
import re
import subprocess
import sys
import unittest

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(TOOLS_DIR, *parts), encoding="utf-8") as handle:
        return handle.read()


def registered_commands():
    source = read("cli.py")
    names = set(re.findall(r'add_parser\(\s*"([a-z0-9-]+)"', source))
    names |= set(re.findall(r'\(\s*"([a-z0-9-]+)",\s*"[A-Z`][^"]*"\)', source))   # the pass-through table
    return names


class TestCliRegistry(unittest.TestCase):
    def test_every_command_the_extension_runs_exists(self):
        source = read("vscode-extension", "extension.js")
        called = set(re.findall(r"cliLine\(python, cli, [`\"]([a-z0-9-]+)", source))
        called |= set(re.findall(r"cliLine\(python, launcher, `([a-z0-9-]+)", source))
        called |= set(re.findall(r"cli\}\" ([a-z0-9-]+)", source))
        self.assertTrue(called, "the extension calls no command: the pattern is stale")
        missing = sorted(called - registered_commands() - {"new-version"})   # new-version lives in the launcher
        self.assertEqual(missing, [], f"the extension calls commands cli.py does not have: {missing}")

    def test_every_command_is_in_the_docs(self):
        docs = "".join(read(*p) for p in (("README.md",), ("docs", "INDEX.md"), ("docs", "LAYOUT.md"), ("docs", "PROJECT_PLAN.md")))
        missing = sorted(n for n in registered_commands() if f"odoo-ai {n}" not in docs and f"`{n}`" not in docs)
        self.assertEqual(missing, [], f"commands without a word in README/INDEX/LAYOUT/PROJECT_PLAN: {missing}")

    def test_the_four_commands_that_once_went_missing_stay(self):
        self.assertTrue({"db-snap", "xpath-check", "i18n", "init-fast-db"} <= registered_commands())


class TestPassThroughOptions(unittest.TestCase):
    """`odoo-ai secrets --install-hook` once failed: argparse refused the leading dash. Test through cli.py itself."""

    def cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(TOOLS_DIR, "cli.py"), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", check=False)

    def test_options_reach_the_command(self):
        for command, option in (("secrets", "--install-hook"), ("claude", "--install"), ("sync-versions", "--check"),
                                ("secrets", "--staged")):
            result = self.cli(command, "--help")
            self.assertEqual(result.returncode, 0, f"{command}: {result.stderr}")
            self.assertIn(option, result.stdout, f"{command} --help does not list {option}")

    def test_the_old_commands_still_answer(self):
        for command in ("db-snap", "i18n", "xpath-check"):
            result = self.cli(command, "--help")
            self.assertEqual(result.returncode, 0, f"{command}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
