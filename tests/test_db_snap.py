import os
import re
import unittest
import sys

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

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


if __name__ == "__main__":
    unittest.main()
