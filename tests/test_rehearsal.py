"""Rehearsal: the everyday scenarios of these tools, end to end, in a throwaway root with a real (local) git remote.

It stands in for weeks of use: set up a new Odoo version, change `main` and regenerate the version branches,
try to commit a password, give Claude Code the project's rules. Nothing outside the temp folder is touched
and nothing is pushed anywhere real. Skip it with ODOO_AI_SKIP_REHEARSAL=1.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import sync_versions  # noqa: E402

GIT_ID = ["-c", "user.name=Rehearsal", "-c", "user.email=rehearsal@example.invalid",
          "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]
PY = sys.executable


def git(cwd, *args, check=True):
    result = subprocess.run(["git", *GIT_ID, *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and result.returncode:
        raise AssertionError(f"git {' '.join(args)} in {cwd}: {result.stderr.strip()}")
    return result


def python(*args, cwd=None, env=None, input_text=None):
    # a child that runs doctor/setup must not start the rehearsal again (it would call itself for ever)
    full_env = dict(os.environ, PYTHONIOENCODING="utf-8", ODOO_AI_SKIP_REHEARSAL="1", **(env or {}))
    full_env.setdefault("ODOO_AI_MAX_DEPTH", "6")   # a loop dies at once and says where (recursion_guard.py)
    return subprocess.run([PY, *args], cwd=cwd, env=full_env, capture_output=True, text=True, timeout=300,
                          input=input_text, encoding="utf-8", errors="replace", check=False)


def copy_tools(dst):
    """The tools tree including versions/, without caches or machine-local files."""
    for rel in sync_versions.tools_files(TOOLS_DIR):
        target = os.path.join(dst, *rel.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(os.path.join(TOOLS_DIR, *rel.split("/")), target)
    versions = os.path.join(TOOLS_DIR, "versions")
    for rel in sync_versions.tools_files(versions):
        target = os.path.join(dst, "versions", *rel.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(os.path.join(versions, *rel.split("/")), target)


@unittest.skipIf(os.environ.get("ODOO_AI_SKIP_REHEARSAL"), "ODOO_AI_SKIP_REHEARSAL is set")
class TestRehearsal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = cls._tmp.name
        cls.remote = os.path.join(cls.tmp, "remote.git")
        seed = os.path.join(cls.tmp, "seed")
        os.makedirs(seed)
        copy_tools(seed)
        git(seed, "init", "-q", "-b", "19.0")
        git(seed, "add", "-A", "--force")
        git(seed, "commit", "-q", "-m", "seed main")
        git(cls.tmp, "init", "-q", "--bare", cls.remote)
        git(seed, "push", "-q", cls.remote, "19.0:19.0", "19.0:main")
        for version in ("17", "18", "20"):   # the branches as a first sync would have left them
            built = os.path.join(cls.tmp, f"built{version}")
            sync_versions.build_tree(seed, version, built)
            git(seed, "checkout", "-q", "--orphan", f"gen{version}")
            git(seed, "rm", "-rq", "--cached", ".")
            for entry in os.listdir(seed):
                if entry != ".git":
                    path = os.path.join(seed, entry)
                    shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
            shutil.copytree(built, seed, dirs_exist_ok=True)
            git(seed, "add", "-A", "--force")
            git(seed, "commit", "-q", "-m", f"{version}.0")
            git(seed, "push", "-q", cls.remote, f"gen{version}:{version}.0")

    @classmethod
    def tearDownClass(cls):
        def writable(func, path, _exc):   # git packs are read-only on Windows
            os.chmod(path, 0o700)
            func(path)
        shutil.rmtree(cls.tmp, onerror=writable, ignore_errors=True)
        cls._tmp.cleanup() if os.path.isdir(cls.tmp) else None

    # 1 ------------------------------------------------------------------ a new Odoo version, one command
    def test_1_new_version_from_nothing(self):
        root = os.path.join(self.tmp, "root")
        os.makedirs(root)
        env = {"ODOO_ROOT": root, "ODOO_PLAN_REMOTE": self.remote}
        launcher = os.path.join(TOOLS_DIR, "launcher", "odoo-ai.py")
        first = python(launcher, "new-version", "20", "--check", cwd=self.tmp, env=env)
        out = first.stdout + first.stderr
        self.assertNotIn("Traceback", out, out)
        self.assertTrue(os.path.isfile(os.path.join(root, "odoo20", "tools", "cli.py")), out)
        self.assertTrue(os.path.isdir(os.path.join(root, "ProjectsOdoo20")))
        branch = git(os.path.join(root, "odoo20", "tools"), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertEqual(branch, "20.0")
        second = python(launcher, "new-version", "20", "--check", cwd=self.tmp, env=env)
        self.assertIn("already in", second.stdout)

    # 2 ------------------------------------------------------------------ change main, regenerate a version
    @unittest.skipUnless(os.path.isdir(os.path.join(TOOLS_DIR, "versions")),
                         "versions/ lives in main (19.0); the generated 17/18/20 branches do not carry it")
    def test_2_change_main_then_regenerate_a_version_branch(self):
        dev = os.path.join(self.tmp, "dev")
        git(self.tmp, "clone", "-q", "-b", "19.0", self.remote, dev)
        with open(os.path.join(dev, "i18n_sync.py"), "a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n# rehearsal: a change every version shares\n")
        git(dev, "commit", "-qam", "[IMP] tools: shared change")
        tool = os.path.join(dev, "sync_versions.py")
        self.assertEqual(python(tool, "fetch", cwd=dev).returncode, 0)
        drift = python(tool, "report", "--check", cwd=dev)
        self.assertEqual(drift.returncode, 1, "the other versions must be reported as behind main")
        self.assertIn("i18n_sync.py", drift.stdout)
        # --no-test: the rehearsal covers apply's mechanics; the guard that runs a tree's tests has its own tests
        applied = python(tool, "apply", "-v", "17", "--no-test", cwd=dev)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        parent = git(dev, "rev-parse", "sync/17.0^").stdout.strip()
        self.assertEqual(parent, git(dev, "rev-parse", "origin/17.0").stdout.strip(), "built on top of the branch")
        self.assertEqual(git(dev, "push", "-q", "origin", "sync/17.0:17.0", check=False).returncode, 0,
                         "pushing the generated branch is a plain fast-forward")
        python(tool, "fetch", cwd=dev)
        self.assertEqual(python(tool, "report", "--check", "-v", "17", cwd=dev).returncode, 0)
        shared = git(dev, "show", "origin/17.0:i18n_sync.py").stdout
        self.assertIn("a change every version shares", shared)
        with open(os.path.join(dev, "versions", "17", "files", ".pylintrc"), encoding="utf-8", newline="") as handle:
            own = handle.read().replace("\r\n", "\n")
        self.assertEqual(git(dev, "show", "origin/17.0:.pylintrc").stdout.replace("\r\n", "\n"), own,
                         "17 keeps its own lint configuration")

    # 3 ------------------------------------------------------------------ a password must not get committed
    def test_3_the_hook_refuses_a_password_and_allows_a_clean_commit(self):
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(proj)
        git(proj, "init", "-q")
        installed = python(os.path.join(TOOLS_DIR, "secret_scan.py"), "--install-hook", cwd=proj)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        conf = os.path.join(proj, "app.conf")
        with open(conf, "w", encoding="utf-8") as handle:
            handle.write("[options]\nadmin_passwd = Sup3rS3cretValue\n")
        git(proj, "add", "app.conf")
        refused = git(proj, "commit", "-m", "add conf", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("admin_passwd", refused.stdout + refused.stderr)
        self.assertNotIn("Sup3rS3cretValue", refused.stdout + refused.stderr, "the value is never shown")
        with open(conf, "w", encoding="utf-8") as handle:
            handle.write("[options]\nadmin_passwd = admin\n")
        git(proj, "add", "app.conf")
        self.assertEqual(git(proj, "commit", "-q", "-m", "add conf", check=False).returncode, 0)

    # 4 ------------------------------------------------------------------ Claude Code gets the project's rules
    def test_4_claude_hooks_installed_in_a_project_really_fire(self):
        proj = os.path.join(self.tmp, "proj2")
        os.makedirs(proj)
        git(proj, "init", "-q")
        done = python(os.path.join(TOOLS_DIR, "claude_setup.py"), "--install", "-p", proj)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        with open(os.path.join(proj, ".claude", "settings.json"), encoding="utf-8") as handle:
            hooks = json.load(handle)["hooks"]

        def fire(event, matcher_has, payload):
            entry = next(e for e in hooks[event] if matcher_has in e["matcher"])
            command = entry["hooks"][0]["command"]
            return subprocess.run(command, shell=True, input=json.dumps(dict(payload, cwd=proj)), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", check=False)

        pushed = fire("PreToolUse", "Bash", {"tool_name": "Bash", "tool_input": {"command": "git push origin x"}})
        self.assertEqual(json.loads(pushed.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")
        status = fire("PreToolUse", "Bash", {"tool_name": "Bash", "tool_input": {"command": "git status"}})
        self.assertEqual(status.stdout.strip(), "")
        secret = fire("PreToolUse", "Write", {"tool_name": "Write", "tool_input": {
            "file_path": os.path.join(proj, "x.conf"), "content": "admin_passwd = Sup3rS3cretValue\n"}})
        self.assertEqual(json.loads(secret.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertNotIn("Sup3rS3cretValue", secret.stdout)
        broken = os.path.join(proj, "broken.py")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("def broken(:\n")
        checked = fire("PostToolUse", "Write", {"tool_name": "Write", "tool_input": {"file_path": broken}})
        self.assertEqual(checked.returncode, 2)
        self.assertIn("syntax error", checked.stderr)


if __name__ == "__main__":
    unittest.main()
