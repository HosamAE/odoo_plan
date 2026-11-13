import os
import re
import unittest
import sys

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import db_snap
from db_fast import get_template_db_name


class TestDbFastAndSnap(unittest.TestCase):
    def test_template_db_name(self):
        tmpl19 = get_template_db_name("19")
        self.assertEqual(tmpl19, "odoo19_clean_base")
        tmpl18 = get_template_db_name("18")
        self.assertEqual(tmpl18, "odoo18_clean_base")

    def test_snapshot_db_name_cleaning(self):
        label = "test-label.before update!"
        clean_label = re.sub(r"[^a-zA-Z0-9_]", "_", label)
        db_name = "test_db"
        snap_db = f"snap_{db_name}_{clean_label}"
        self.assertEqual(snap_db, "snap_test_db_test_label_before_update_")

    def test_clean_label_regex(self):
        label = "my_snapshot_1"
        clean = re.sub(r"[^a-zA-Z0-9_]", "_", label)
        self.assertEqual(clean, "my_snapshot_1")


class FakeCursor:
    """Answers `SELECT 1 FROM pg_database WHERE datname = x` from a set of names and records every statement."""

    def __init__(self, existing):
        self.existing, self.statements, self.row = set(existing), [], None

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if "FROM pg_database WHERE datname = %s" in sql:
            self.row = (1,) if params[0] in self.existing else None

    def fetchone(self):
        return self.row

    def fetchall(self):
        raise AssertionError("a restore must not look for a snapshot by pattern")


class FakeConn:
    def __init__(self, existing):
        self.cur = FakeCursor(existing)

    def cursor(self):
        return self.cur


class TestSnapshotsAreSafe(unittest.TestCase):
    def test_a_quote_in_a_name_cannot_leave_the_identifier(self):
        self.assertEqual(db_snap.ident('a"; DROP DATABASE "b'), '"a""; DROP DATABASE ""b"')

    def test_names_that_cannot_be_identifiers_are_refused(self):
        for bad in ("", "x" * 64, "a\x00b", "a\nb"):
            with self.assertRaises(db_snap.SnapshotError, msg=repr(bad)):
                db_snap.ident(bad)

    def test_a_too_long_snapshot_name_is_refused_not_cut_by_the_server(self):
        with self.assertRaises(db_snap.SnapshotError):
            db_snap.snapshot_name("a_long_database_name", "x" * 60)

    def test_restore_of_an_unknown_label_changes_nothing(self):
        conn = FakeConn({"mydb", "snap_mydb_before_upgrade"})
        self.assertFalse(db_snap.restore_snapshot("before", "mydb", conn))   # a part of another label: no guess
        statements = " ".join(sql for sql, _ in conn.cur.statements)
        self.assertNotIn("DROP", statements)
        self.assertNotIn("CREATE", statements)

    def test_restore_of_the_exact_label_replaces_the_database(self):
        conn = FakeConn({"mydb", "snap_mydb_before"})
        self.assertTrue(db_snap.restore_snapshot("before", "mydb", conn))
        sql = [s for s, _ in conn.cur.statements]
        self.assertIn('DROP DATABASE IF EXISTS "mydb";', sql)
        self.assertIn('CREATE DATABASE "mydb" WITH TEMPLATE "snap_mydb_before";', sql)

    def test_a_hostile_database_name_is_refused_before_any_statement(self):
        conn = FakeConn({"mydb"})
        with self.assertRaises(db_snap.SnapshotError):
            db_snap.save_snapshot("x", "my" + "x" * 70, conn)
        self.assertEqual([s for s, _ in conn.cur.statements if "DROP" in s or "CREATE" in s], [])


if __name__ == "__main__":
    unittest.main()
