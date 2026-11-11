"""server_dbs: the databases and modules the cockpit shows, and the two things it lets you do to a database."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import server_ctl  # noqa: E402
import server_dbs  # noqa: E402


class TestFindOdooBin(unittest.TestCase):
    def test_it_is_found_from_the_confs_own_place_on_any_drive(self):
        with tempfile.TemporaryDirectory() as root:
            binary = os.path.join(root, "odoo19", "odoo", "odoo-bin")
            os.makedirs(os.path.dirname(binary))
            open(binary, "w").close()
            conf = os.path.join(root, "ProjectsOdoo19", "Demo", "Demo.conf")
            self.assertEqual(server_ctl.find_odoo_bin(conf, environ={}), binary)
            old_style = os.path.join(root, "odoo19", "projects", "Demo", "Demo.conf")
            self.assertEqual(server_ctl.find_odoo_bin(old_style, environ={}), binary)

    def test_odoo_root_is_tried_and_the_path_is_the_last_resort(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as elsewhere:
            binary = os.path.join(elsewhere, "odoo19", "odoo", "odoo-bin")
            os.makedirs(os.path.dirname(binary))
            open(binary, "w").close()
            conf = os.path.join(root, "ProjectsOdoo19", "Demo", "Demo.conf")
            self.assertEqual(server_ctl.find_odoo_bin(conf, environ={"ODOO_ROOT": elsewhere}), binary)
            self.assertEqual(server_ctl.find_odoo_bin(conf, environ={}), "odoo-bin")
            self.assertEqual(server_ctl.find_odoo_bin(os.path.join(root, "x.conf"), environ={}), "odoo-bin")


class TestManifests(unittest.TestCase):
    def make(self, root, name, text):
        folder = os.path.join(root, name)
        os.makedirs(folder)
        with open(os.path.join(folder, "__manifest__.py"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_a_version_is_read_as_data_and_nothing_is_run(self):
        with tempfile.TemporaryDirectory() as root:
            marker = os.path.join(root, "ran.txt")
            self.make(root, "good", '# -*- coding: utf-8 -*-\n{"name": "G", "version": "19.0.1.2.3", "depends": ["base"]}\n')
            self.make(root, "evil", f'__import__("builtins").open({marker!r}, "w")\n{{"version": "19.0.9"}}\n')
            self.make(root, "short", '{"name": "S", "version": "21.10.40"}')
            self.make(root, "none", '{"name": "N"}')
            self.make(root, "broken", "{this is not python")
            versions = server_ctl_versions = server_dbs.code_versions([root])
            self.assertEqual(versions["good"], "19.0.1.2.3")
            self.assertEqual(versions["short"], "21.10.40")
            self.assertIsNone(versions["none"])
            self.assertIsNone(versions["broken"])
            self.assertFalse(os.path.exists(marker), "a manifest is parsed, never executed")
            self.assertTrue(server_ctl_versions)

    def test_the_first_addons_path_wins_and_relative_ones_follow_the_conf(self):
        with tempfile.TemporaryDirectory() as root:
            a, b = os.path.join(root, "a"), os.path.join(root, "b")
            self.make(a, "dup", '{"version": "19.0.1.0.0"}')
            self.make(b, "dup", '{"version": "19.0.2.0.0"}')
            os.makedirs(os.path.join(root, "proj"))
            conf = os.path.join(root, "proj", "p.conf")
            paths = server_dbs.addons_paths({"addons_path": f"{a},../b, ,{os.path.join(root, 'nope')}"}, conf)
            self.assertEqual(paths, [os.path.normpath(a), os.path.normpath(b)])
            self.assertEqual(server_dbs.code_versions(paths)["dup"], "19.0.1.0.0")


class TestCompare(unittest.TestCase):
    CODE = {"ok_mod": "19.0.1.0.0", "newer": "19.0.1.5.0", "short": "21.10.40", "avail": "19.0.1.0.0"}
    ROWS = [("ok_mod", "installed", "19.0.1.0.0"), ("newer", "installed", "19.0.1.0.0"),
            ("short", "installed", "19.0.21.10.40"), ("gone", "installed", "19.0.1.0.0"),
            ("waiting", "to upgrade", "19.0.1.0.0"), ("avail", "uninstalled", None),
            ("foreign", "uninstalled", None), ("enterprise_only", "uninstallable", None)]

    def report(self):
        return server_dbs.compare_modules(self.CODE, self.ROWS, "19.0")

    def status(self, name):
        return next(m for m in self.report()["modules"] if m["name"] == name)["status"]

    def test_every_status(self):
        self.assertEqual(self.status("ok_mod"), "ok")
        self.assertEqual(self.status("newer"), "version_differs", "the code is ahead: an upgrade is due")
        self.assertEqual(self.status("short"), "ok", "Odoo prefixes the series to a short manifest version")
        self.assertEqual(self.status("gone"), "missing_code", "installed but no folder has it")
        self.assertEqual(self.status("waiting"), "to_upgrade")
        self.assertEqual(self.status("avail"), "available")

    def test_a_module_with_no_code_that_is_not_installed_is_left_out(self):
        names = [m["name"] for m in self.report()["modules"]]
        self.assertNotIn("foreign", names)
        self.assertNotIn("enterprise_only", names, "an uninstallable one is nothing to act on")

    def test_core_modules_are_found_through_the_extra_path(self):
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "base")
            os.makedirs(folder)
            with open(os.path.join(folder, "__manifest__.py"), "w", encoding="utf-8") as handle:
                handle.write('{"version": "1.3"}')
            conn = mock.MagicMock()
            cur = conn.cursor.return_value
            cur.fetchone.return_value = ("19.0.1.3",)
            cur.fetchall.return_value = [("base", "installed", "19.0.1.3")]
            with mock.patch.object(server_ctl, "_connect", return_value=conn):
                report = server_dbs.modules_report({}, os.path.join(root, "x.conf"), "db", [root])
        self.assertEqual(report["modules"][0]["status"], "ok", "base is in the folder Odoo adds by itself")

    def test_a_module_in_the_code_but_not_in_the_database_is_available(self):
        report = server_dbs.compare_modules({"brand_new": "19.0.1.0.0"}, [], "19.0")
        self.assertEqual(report["modules"][0]["status"], "available")
        self.assertEqual(report["modules"][0]["state"], "not_in_database")

    def test_counts_add_up(self):
        counts = self.report()["counts"]
        self.assertEqual((counts["ok"], counts["version_differs"], counts["missing_code"], counts["to_upgrade"], counts["available"]),
                         (2, 1, 1, 1, 1))

    def test_expected_version(self):
        self.assertEqual(server_dbs.expected_version("21.10.40", "19.0"), "19.0.21.10.40")
        self.assertEqual(server_dbs.expected_version("19.0.1.2.3", "19.0"), "19.0.1.2.3")
        self.assertIsNone(server_dbs.expected_version(None, "19.0"))


class FakeDb:
    """Stands in for a psycopg2 connection: answers the queries inventory() asks."""
    def __init__(self, name, answers):
        self.name, self.answers = name, answers

    def cursor(self):
        db = self

        class Cursor:
            connection = db
            last = None

            def execute(self, sql):
                self.last = sql

            def fetchone(self):
                for key, value in db.answers.items():
                    if key in self.last:
                        return (value,)
                return (None,)

            def fetchall(self):
                return [("good", 150 * 1048576), ("broken", 8 * 1048576), ("empty", 9 * 1048576)]
        return Cursor()

    def rollback(self):
        pass

    def close(self):
        pass


class TestInventory(unittest.TestCase):
    def test_one_unreadable_database_does_not_hide_the_others(self):
        def connect(options, database="postgres"):
            if database == "broken":
                raise RuntimeError("FATAL: collations with different collate and ctype")
            if database == "good":
                return FakeDb(database, {"name = 'base'": "19.0.1.3", "state = 'installed'": 311, "res_users_log": "2026-11-04"})
            return FakeDb(database, {})
        with mock.patch.object(server_ctl, "_connect", connect):
            data = {d["name"]: d for d in server_dbs.inventory({})}
        self.assertEqual((data["good"]["size_mb"], data["good"]["version"], data["good"]["modules"], data["good"]["last_login"]),
                         (150, "19.0.1.3", 311, "2026-11-04"))
        self.assertFalse(data["broken"]["usable"])
        self.assertEqual(data["broken"]["reason"], "RuntimeError")
        self.assertTrue(data["empty"]["usable"])
        self.assertIsNone(data["empty"]["version"], "not an Odoo database, or a fresh one: no values, no failure")


class TestBackupAndDrop(unittest.TestCase):
    def args(self, **kw):
        base = {"conf": "p.conf", "python": "py", "odoo_bin": "ob", "db": "db1", "yes": True, "force": False, "backup_dir": None}
        base.update(kw)
        return mock.MagicMock(**base)

    def run_cmd(self, fn, args):
        with mock.patch("builtins.print") as shown:
            code = fn(args)
        return code, "\n".join(str(c.args[0]) for c in shown.call_args_list if c.args)

    def test_the_backup_name_carries_the_database_and_the_time(self):
        self.assertEqual(server_dbs.backup_path("dir", "db1", datetime(2030, 3, 6, 10, 5, 7)),
                         os.path.join("dir", "db1_20300306_100507.zip"))

    def test_a_drop_without_yes_is_refused(self):
        with mock.patch.object(server_dbs.subprocess, "run") as run:
            code, out = self.run_cmd(server_dbs.cmd_drop, self.args(yes=False))
        self.assertEqual(code, 2)
        self.assertIn("--yes", out)
        run.assert_not_called()

    def test_a_drop_of_a_database_in_use_is_refused_unless_forced(self):
        with mock.patch.object(server_ctl, "other_connections", return_value=4), \
                mock.patch.object(server_dbs.subprocess, "run") as run:
            code, out = self.run_cmd(server_dbs.cmd_drop, self.args())
        self.assertEqual(code, 3)
        self.assertIn("4 session", out)
        run.assert_not_called()

    def test_a_bad_name_never_reaches_a_command(self):
        with mock.patch.object(server_dbs.subprocess, "run") as run:
            code, _ = self.run_cmd(server_dbs.cmd_drop, self.args(db="db; rm"))
        self.assertEqual(code, 2)
        run.assert_not_called()

    def test_a_drop_runs_odoos_own_command_when_all_is_in_order(self):
        done = mock.MagicMock(returncode=0, stdout="DROP DB: db1", stderr="")
        with mock.patch.object(server_ctl, "other_connections", return_value=0), \
                mock.patch.object(server_dbs.subprocess, "run", return_value=done) as run:
            code, out = self.run_cmd(server_dbs.cmd_drop, self.args())
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], ["py", "ob", "db", "-c", "p.conf", "drop", "db1"])
        self.assertIn("Dropped db1", out)

    def test_if_the_backup_fails_nothing_is_dropped(self):
        with mock.patch.object(server_ctl, "other_connections", return_value=0), \
                mock.patch.object(server_dbs, "backup", return_value=(False, "x.zip", "disk full")), \
                mock.patch.object(server_dbs, "drop") as drop:
            code, out = self.run_cmd(server_dbs.cmd_drop, self.args(backup_dir="bk"))
        self.assertEqual(code, 4)
        self.assertIn("NOT DROPPED", out)
        drop.assert_not_called()

    def test_with_a_good_backup_the_drop_follows(self):
        with mock.patch.object(server_ctl, "other_connections", return_value=0), \
                mock.patch.object(server_dbs, "backup", return_value=(True, "bk/db1.zip", "120 MB")), \
                mock.patch.object(server_dbs, "drop", return_value=(True, [""])) as drop:
            code, out = self.run_cmd(server_dbs.cmd_drop, self.args(backup_dir="bk"))
        self.assertEqual(code, 0)
        self.assertIn("Backed up to bk/db1.zip", out)
        drop.assert_called_once()

    def test_a_backup_that_produced_no_file_is_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = mock.MagicMock(returncode=0, stdout="", stderr="")
            with mock.patch.object(server_dbs.subprocess, "run", return_value=proc):
                ok, _path, message = server_dbs.backup("py", "ob", "p.conf", "db1", directory)
        self.assertFalse(ok)
        self.assertIn("no file", message)

    def test_a_good_backup_reports_its_size(self):
        with tempfile.TemporaryDirectory() as directory:
            def fake_run(command, **kw):
                with open(command[-1], "wb") as handle:
                    handle.write(b"x" * 2_000_000)
                return mock.MagicMock(returncode=0, stdout="", stderr="")
            with mock.patch.object(server_dbs.subprocess, "run", side_effect=fake_run):
                ok, path, message = server_dbs.backup("py", "ob", "p.conf", "db1", directory)
            self.assertTrue(ok)
            self.assertTrue(path.startswith(directory) and path.endswith(".zip"))
            self.assertEqual(message, "2 MB")


class TestCli(unittest.TestCase):
    def test_the_new_commands_are_reachable_through_server_ctl(self):
        fake = [{"name": "good", "size_mb": 1, "version": "19.0.1.3", "modules": 3, "last_login": None, "usable": True, "reason": None}]
        with mock.patch.object(server_dbs, "inventory", return_value=fake), mock.patch("builtins.print") as shown:
            code = server_ctl.main(["inventory", "--conf", "p.conf", "--json"])
        self.assertEqual((code, json.loads(shown.call_args.args[0])["databases"][0]["name"]), (0, "good"))

    def test_modules_needs_a_database(self):
        with mock.patch("builtins.print"):
            self.assertEqual(server_ctl.main(["modules", "--conf", "p.conf"]), 2)


if __name__ == "__main__":
    unittest.main()
