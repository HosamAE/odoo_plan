"""remote_dbs: other servers' databases over ssh. The ssh and postgres calls are replaced; what is tested is what the
tool decides: what it refuses, what it reads, and that nothing is ever written over a database that exists."""
import argparse
import io
import os
import sys
import tarfile
import unittest
from unittest import mock

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import remote_dbs  # noqa: E402
import server_ctl  # noqa: E402

CONFIG = """
Host *
    ServerAliveInterval 30

Host acme17
    HostName 10.0.0.1
    User root

Host cloudhost acme19
  HostName 10.0.0.2
  User dev
  IdentityFile ~/.ssh/id_ed25519

Host *.internal bad!name
    User nobody
"""

DB_LIST = "apps|419|odoo19_apps|6\nold_customer|174|postgres|0\nnoise line\n"
INSTANCES = """== /etc/odoo19/apps.conf
db_name = apps
db_user = odoo19_apps
data_dir = /odoo19/customers/apps/data
http_port = 8101
db_password = must-never-be-read
== /etc/odoo19/idhub.conf
db_name = False
db_user = odoo19_idhub
"""


def ns(**values):
    base = dict(host="h1", db="d1", as_name=None, owner="odoo19_x", instance=None, yes=False, force=False, confirm=None,
                backup_dir=None, interactive=False, no_sudo=False, sudo_user=None, json=True, dir=None,
                conf="x.conf", python="python", odoo_bin=None, no_filestore=False, no_neutralize=False, keep_dump=None)
    base.update(values)
    return argparse.Namespace(**base)


class TestHosts(unittest.TestCase):
    def test_only_plain_host_names_are_hosts(self):
        hosts = remote_dbs.parse_ssh_config(CONFIG)
        self.assertEqual([h["name"] for h in hosts], ["acme17", "cloudhost", "acme19"])

    def test_each_host_has_its_own_user_and_name(self):
        hosts = {h["name"]: h for h in remote_dbs.parse_ssh_config(CONFIG)}
        self.assertEqual((hosts["acme17"]["user"], hosts["acme17"]["hostname"]), ("root", "10.0.0.1"))
        self.assertEqual(hosts["cloudhost"]["user"], hosts["acme19"]["user"])

    def test_a_missing_config_is_no_hosts(self):
        self.assertEqual(remote_dbs.ssh_hosts("no/such/file"), [])

    def test_a_host_name_with_shell_characters_is_refused(self):
        with self.assertRaises(remote_dbs.RemoteError):
            remote_dbs.ssh_command("h; rm -rf /", "echo")

    def test_ssh_asks_nothing_unless_told_to(self):
        self.assertIn("BatchMode=yes", remote_dbs.ssh_command("h", "echo"))
        self.assertNotIn("BatchMode=yes", remote_dbs.ssh_command("h", "echo", interactive=True))


class TestParsing(unittest.TestCase):
    def test_databases_with_size_owner_and_sessions(self):
        rows = remote_dbs.parse_databases(DB_LIST)
        self.assertEqual([r["name"] for r in rows], ["apps", "old_customer"])
        self.assertEqual((rows[0]["size_mb"], rows[0]["owner"], rows[0]["sessions"]), (419, "odoo19_apps", 6))

    def test_instances_carry_only_the_four_keys_and_never_a_password(self):
        found = remote_dbs.parse_instances(INSTANCES)
        self.assertEqual(found[0], {"conf": "/etc/odoo19/apps.conf", "db_name": "apps", "db_user": "odoo19_apps",
                                    "data_dir": "/odoo19/customers/apps/data", "http_port": "8101"})
        self.assertIsNone(found[1]["db_name"])   # `False` means no fixed database
        self.assertNotIn("must-never-be-read", repr(found))

    def test_the_conf_script_asks_for_no_secret(self):
        self.assertNotIn("password", remote_dbs.INSTANCE_SCRIPT.replace("db_password", "X").lower().replace("passwd", ""))

    def test_failures_are_explained_in_a_sentence(self):
        self.assertIn("password", remote_dbs.explain("user@h: Permission denied (publickey,password)."))
        self.assertIn("cannot reach", remote_dbs.explain("ssh: Could not resolve hostname x"))
        self.assertIn("sudo", remote_dbs.explain("sudo: a password is required"))
        self.assertIn("key", remote_dbs.explain("Host key verification failed."))


class TestPrefix(unittest.TestCase):
    def test_postgres_commands_go_through_a_sudo_that_never_asks(self):
        self.assertEqual(remote_dbs.pg_prefix(ns()), "sudo -n -u postgres ")

    def test_no_sudo_and_another_user(self):
        self.assertEqual(remote_dbs.pg_prefix(ns(no_sudo=True)), "")
        self.assertEqual(remote_dbs.pg_prefix(ns(sudo_user="pg2")), "sudo -n -u pg2 ")

    def test_a_user_name_with_shell_characters_is_refused(self):
        with self.assertRaises(remote_dbs.RemoteError):
            remote_dbs.pg_prefix(ns(sudo_user="x; id"))


class TestRestrictedHost(unittest.TestCase):
    """A host can open one account only (its own rule): files are then read as that account, never wider."""

    def test_files_are_read_as_root_unless_one_account_is_given(self):
        self.assertEqual(remote_dbs.files_prefix(ns()), "sudo -n ")
        self.assertEqual(remote_dbs.files_prefix(ns(sudo_user="odoo19_demo")), "sudo -n -u odoo19_demo ")
        self.assertEqual(remote_dbs.files_prefix(ns(sudo_user="postgres")), "sudo -n ")
        self.assertEqual(remote_dbs.files_prefix(ns(no_sudo=True)), "")

    def test_the_list_asks_only_for_databases_the_account_may_connect_to(self):
        self.assertIn("has_database_privilege(d.datname, 'CONNECT')", remote_dbs.LIST_SQL)

    def test_a_refusal_is_shown_in_the_hosts_own_words(self):
        with mock.patch.object(remote_dbs, "run_remote", return_value=(1, "", "ACCESS DENIED" + chr(10) + "Only the demo environment is open to you.")):
            with self.assertRaises(remote_dbs.RemoteError) as raised:
                remote_dbs.list_remote("h1", ns())
        self.assertIn("demo environment", str(raised.exception))


class TestTarSafety(unittest.TestCase):
    def member(self, name):
        return tarfile.TarInfo(name)

    def test_files_are_renamed_into_the_new_database(self):
        self.assertEqual(remote_dbs.safe_member(self.member("demo/ab/cdef"), "demo", "copy").name, "copy/ab/cdef")

    def test_a_member_that_would_leave_the_folder_is_dropped(self):
        for bad in ("../x", "demo/../../x", "/etc/passwd", "other/ab"):
            self.assertIsNone(remote_dbs.safe_member(self.member(bad), "demo", "copy"), bad)

    def test_a_name_that_only_starts_like_the_source_is_not_the_source(self):
        self.assertIsNone(remote_dbs.safe_member(self.member("demo2/ab"), "demo", "copy"))


class TestPullRefusals(unittest.TestCase):
    def test_a_local_name_that_exists_is_never_overwritten(self):
        with mock.patch.object(server_ctl, "read_conf", return_value={}), \
                mock.patch.object(server_ctl, "list_databases", return_value=["mine"]), \
                mock.patch.object(remote_dbs, "stream_to_file") as dump:
            with self.assertRaises(remote_dbs.RemoteError) as raised:
                remote_dbs.pull(ns(db="demo", as_name="mine"))
        self.assertIn("already exists", str(raised.exception))
        dump.assert_not_called()

    def test_a_bad_name_is_refused_before_anything_runs(self):
        with mock.patch.object(remote_dbs, "stream_to_file") as dump:
            with self.assertRaises(remote_dbs.RemoteError):
                remote_dbs.pull(ns(db="a;b"))
        dump.assert_not_called()

    def test_a_failed_dump_leaves_no_local_database_behind(self):
        with mock.patch.object(server_ctl, "read_conf", return_value={}), \
                mock.patch.object(server_ctl, "list_databases", return_value=[]), \
                mock.patch.object(remote_dbs, "stream_to_file", return_value=(255, "Permission denied (publickey)")), \
                mock.patch.object(remote_dbs, "create_local_database") as create:
            with self.assertRaises(remote_dbs.RemoteError) as raised:
                remote_dbs.pull(ns(db="demo"))
        self.assertIn("password", str(raised.exception))
        create.assert_not_called()

    def test_a_failed_restore_removes_the_half_made_copy(self):
        def fake_dump(command, path):
            with open(path, "wb") as handle:
                handle.write(b"x" * 10)
            return 0, ""
        with mock.patch.object(server_ctl, "read_conf", return_value={}), \
                mock.patch.object(server_ctl, "list_databases", return_value=[]), \
                mock.patch.object(remote_dbs, "stream_to_file", side_effect=fake_dump), \
                mock.patch.object(remote_dbs, "create_local_database"), \
                mock.patch.object(remote_dbs, "restore_local", side_effect=remote_dbs.RemoteError("boom")), \
                mock.patch.object(remote_dbs, "drop_local_database") as drop:
            with self.assertRaises(remote_dbs.RemoteError):
                remote_dbs.pull(ns(db="demo"))
        drop.assert_called_once()


HOST_DBS = {"host": "h1", "databases": [{"name": "apps", "size_mb": 1, "owner": "odoo19_apps", "sessions": 0},
                                        {"name": "busy", "size_mb": 1, "owner": "odoo19_x", "sessions": 3}],
            "instances": [{"conf": "/etc/odoo19/apps.conf", "db_name": "apps", "db_user": "odoo19_apps", "data_dir": "/d", "http_port": "1"}]}


class TestPushRefusals(unittest.TestCase):
    def test_it_needs_yes(self):
        with self.assertRaises(remote_dbs.RemoteError) as raised:
            remote_dbs.push(ns())
        self.assertTrue(str(raised.exception).startswith("REFUSED"))

    def test_it_needs_an_owner(self):
        with self.assertRaises(remote_dbs.RemoteError):
            remote_dbs.push(ns(yes=True, owner=None))

    def test_a_remote_name_that_exists_is_never_overwritten(self):
        with mock.patch.object(server_ctl, "read_conf", return_value={}), \
                mock.patch.object(remote_dbs, "list_remote", return_value=HOST_DBS), \
                mock.patch.object(remote_dbs, "run_remote") as run:
            with self.assertRaises(remote_dbs.RemoteError) as raised:
                remote_dbs.push(ns(yes=True, db="local", as_name="apps", owner="odoo19_apps"))
        self.assertIn("already exists", str(raised.exception))
        run.assert_not_called()

    def test_an_owner_no_instance_uses_is_refused(self):
        with mock.patch.object(server_ctl, "read_conf", return_value={}), \
                mock.patch.object(remote_dbs, "list_remote", return_value=HOST_DBS), \
                mock.patch.object(remote_dbs, "run_remote") as run:
            with self.assertRaises(remote_dbs.RemoteError):
                remote_dbs.push(ns(yes=True, db="local", as_name="new", owner="odoo19_ghost"))
        run.assert_not_called()

    def test_protected_names_are_refused(self):
        with self.assertRaises(remote_dbs.RemoteError):
            remote_dbs.push(ns(yes=True, as_name="postgres"))


class TestDropRefusals(unittest.TestCase):
    def drop(self, **values):
        with mock.patch.object(remote_dbs, "list_remote", return_value=HOST_DBS), \
                mock.patch.object(remote_dbs, "run_remote", return_value=(0, "", "")) as run:
            try:
                return remote_dbs.drop(ns(**values)), run
            except remote_dbs.RemoteError as exc:
                return exc, run

    def test_it_needs_the_host_and_name_typed_again(self):
        error, run = self.drop(db="apps")
        self.assertTrue(str(error).startswith("REFUSED"))
        error, run = self.drop(db="apps", confirm="h1/other")
        self.assertTrue(str(error).startswith("REFUSED"))
        run.assert_not_called()

    def test_postgres_and_templates_are_never_dropped(self):
        for name in ("postgres", "template0", "template1"):
            error, run = self.drop(db=name, confirm=f"h1/{name}")
            self.assertIsInstance(error, remote_dbs.RemoteError)
            run.assert_not_called()

    def test_a_database_with_sessions_is_refused(self):
        error, run = self.drop(db="busy", confirm="h1/busy")
        self.assertIn("connected", str(error))
        run.assert_not_called()

    def test_a_database_an_instance_serves_is_refused(self):
        rows = {"host": "h1", "databases": [{"name": "apps", "size_mb": 1, "owner": "o", "sessions": 0}], "instances": HOST_DBS["instances"]}
        with mock.patch.object(remote_dbs, "list_remote", return_value=rows), mock.patch.object(remote_dbs, "run_remote") as run:
            with self.assertRaises(remote_dbs.RemoteError) as raised:
                remote_dbs.drop(ns(db="apps", confirm="h1/apps"))
        self.assertIn("configured to serve", str(raised.exception))
        run.assert_not_called()

    def test_a_confirmed_free_database_is_dropped(self):
        rows = {"host": "h1", "databases": [{"name": "old", "size_mb": 1, "owner": "o", "sessions": 0}], "instances": []}
        with mock.patch.object(remote_dbs, "list_remote", return_value=rows), \
                mock.patch.object(remote_dbs, "run_remote", return_value=(0, "", "")) as run:
            result = remote_dbs.drop(ns(db="old", confirm="h1/old"))
        self.assertTrue(result["ok"])
        self.assertIn("dropdb old", run.call_args[0][1])

    def test_the_backup_comes_first_and_a_failed_one_stops_the_drop(self):
        rows = {"host": "h1", "databases": [{"name": "old", "size_mb": 1, "owner": "o", "sessions": 0}], "instances": []}
        with mock.patch.object(remote_dbs, "list_remote", return_value=rows), \
                mock.patch.object(remote_dbs, "backup", side_effect=remote_dbs.RemoteError("the dump failed")), \
                mock.patch.object(remote_dbs, "run_remote") as run:
            with self.assertRaises(remote_dbs.RemoteError):
                remote_dbs.drop(ns(db="old", confirm="h1/old", backup_dir="b"))
        run.assert_not_called()


class TestCli(unittest.TestCase):
    def test_the_remote_commands_are_registered(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        remote_dbs.add_commands(sub, server_ctl._project_args)
        for command in ("remote-hosts", "remote-check", "remote-list", "remote-pull", "remote-push", "remote-backup", "remote-drop"):
            self.assertIn(command, sub.choices)

    def test_a_refusal_exits_with_2_and_says_why_in_json(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = remote_dbs._guard(remote_dbs.drop)(ns(db="apps"))
        self.assertEqual(code, 2)
        self.assertIn('"ok": false', out.getvalue())


if __name__ == "__main__":
    unittest.main()
