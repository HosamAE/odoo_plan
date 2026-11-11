"""server_scan: every error and warning of a log with its line, and how far a start or an upgrade has got.

The fixtures are lines of real logs (2026-11-10): an upgrade of 324 modules, a traceback inside a request, and the
warnings Odoo tolerates."""
import os
import sys
import unittest

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import server_ctl  # noqa: E402
import server_scan  # noqa: E402

UPGRADE = """\
2026-11-10 23:14:03,242 45616 INFO db odoo.modules.loading: Loading module base (1/1)
2026-11-10 23:14:07,712 45616 INFO db odoo.modules.loading: loading 324 modules...
2026-11-10 23:14:07,713 45616 INFO db odoo.modules.loading: Loading module rpc (2/324)
2026-11-10 23:14:09,000 45616 INFO db odoo.registry: module rpc: creating or updating database tables
2026-11-10 23:14:10,000 45616 INFO db odoo.modules.loading: Loading module web (3/324)
2026-11-10 23:14:19,000 45616 INFO db odoo.modules.loading: Loading module account (4/324)
2026-11-10 23:14:20,000 45616 INFO db odoo.registry: module account: creating or updating database tables
2026-11-10 23:14:21,000 45616 INFO db odoo.modules.loading: loading account/views/move_views.xml
2026-11-10 23:14:30,000 45616 INFO db odoo.modules.loading: Loading module sale (5/324)
"""

FINISHED = UPGRADE + """\
2026-11-10 23:16:08,968 45616 INFO db odoo.modules.loading: 324 modules loaded in 121.26s, 186971 queries (+186971 extra)
"""

PROBLEMS = """\
2026-11-10 23:15:43,409 1 INFO db odoo.modules.loading: loading acme_planning/views/board_menus.xml
2026-11-10 23:15:43,424 1 WARNING db odoo.tools.convert: Skipping deletion for missing XML ID `'action_gantt_timeoff'`
Traceback (most recent call last):
  File "C:/odoo/odoo19/odoo/odoo/tools/convert.py", line 261, in _tag_delete
    x
ValueError: External ID not found in the system: acme_planning.action_gantt_timeoff
2026-11-10 23:15:50,000 1 WARNING db odoo.addons.base.models.ir_ui_view: Access Rights Inconsistency view a
2026-11-10 23:15:51,000 1 WARNING db odoo.addons.base.models.ir_ui_view: Access Rights Inconsistency view a
2026-11-10 23:15:52,000 1 WARNING db odoo.tools.config: option db_name reads 'False' in the config file
2026-11-10 23:15:53,000 1 ERROR db odoo.http: Exception during request handling.
Traceback (most recent call last):
  File "C:/odoo/odoo19/odoo/odoo/http.py", line 2856, in __call__
    response = request._serve_db()
  File "C:/work/Proj/mod_a/models/thing.py", line 40, in compute
    boom()
psycopg2.errors.UndefinedColumn: column res_partner.x does not exist
LINE 1: SELECT
2026-11-10 23:15:54,000 1 INFO db odoo.service.server: back to normal
"""


class TestItems(unittest.TestCase):
    def scan(self, text=PROBLEMS, **kw):
        return server_scan.scan_log(text, **kw)

    def test_every_distinct_problem_is_an_item_with_its_line(self):
        items = self.scan()["items"]
        self.assertEqual([(i["kind"], i["line"]) for i in items], [("error", 10), ("warning", 2), ("warning", 7)],
                         "errors first, then warnings, each at the line of its first record")
        by_line = {i["line"]: i for i in items}
        self.assertEqual(by_line[2]["module"], "acme_planning")
        self.assertEqual(by_line[2]["file"], "views/board_menus.xml")

    def test_the_same_words_are_one_item_with_a_count_and_all_their_lines(self):
        items = {i["line"]: i for i in self.scan()["items"]}
        twice = items[7]
        self.assertEqual((twice["count"], twice["lines"]), (2, [7, 8]))

    def test_noise_is_neither_listed_nor_painted(self):
        scan = self.scan()
        self.assertFalse([i for i in scan["items"] if "db_name reads" in i["message"]])
        self.assertNotIn(9, scan["marks"]["warning"])

    def test_the_root_cause_and_the_place_in_your_code_belong_to_the_item(self):
        scan = self.scan(roots=["C:/work/Proj"])
        error = next(i for i in scan["items"] if i["kind"] == "error")
        self.assertIn("UndefinedColumn", error["detail"])
        if os.name == "nt":
            self.assertEqual(error["location"]["line"], 40)
            self.assertTrue(error["location"]["file"].replace("\\", "/").endswith("mod_a/models/thing.py"))

    def test_a_tolerated_warning_keeps_its_root_cause_but_is_a_warning(self):
        item = next(i for i in self.scan()["items"] if i["line"] == 2)
        self.assertEqual(item["kind"], "warning")
        self.assertIn("External ID not found", item["detail"])

    def test_lines_of_a_record_are_marked_with_its_colour(self):
        marks = self.scan()["marks"]
        self.assertEqual(marks["error"], list(range(10, 18)), "the error line, its traceback and its detail")
        self.assertEqual(marks["warning"], [2, 3, 4, 5, 6, 7, 8], "the warning and its traceback, then the next")
        self.assertNotIn(18, marks["error"], "the next record is not part of it")

    def test_line_numbers_are_the_files_own_when_a_tail_was_read(self):
        tail = "\n".join(PROBLEMS.splitlines()[7:])
        full = {i["message"]: i["line"] for i in self.scan()["items"]}
        shifted = {i["message"]: i["line"] for i in server_scan.scan_log(tail, line_offset=7)["items"]}
        self.assertEqual(shifted["Exception during request handling."], full["Exception during request handling."])


class TestProgress(unittest.TestCase):
    def test_where_it_is_out_of_how_many_and_how_much_is_left(self):
        progress = server_scan.scan_log(UPGRADE)["progress"]
        self.assertEqual((progress["module"], progress["index"], progress["total"]), ("sale", 5, 324))
        self.assertEqual(progress["remaining"], 319)
        self.assertEqual(progress["file"], None, "a new module has not opened a file yet")
        self.assertFalse(progress["finished"])
        self.assertEqual(progress["elapsed_s"], 23)

    def test_the_file_in_progress_is_named(self):
        text = UPGRADE.rsplit("2026-11-10 23:14:30", 1)[0]
        progress = server_scan.scan_log(text)["progress"]
        self.assertEqual((progress["module"], progress["file"]), ("account", "views/move_views.xml"))

    def test_an_estimate_only_once_there_is_something_to_estimate_from(self):
        early = "\n".join(UPGRADE.splitlines()[:5])
        self.assertIsNone(server_scan.scan_log(early)["progress"]["eta_s"])
        later = server_scan.scan_log(UPGRADE)["progress"]
        self.assertGreater(later["eta_s"], 0, "four modules done, 319 to go")

    def test_which_modules_were_really_upgraded(self):
        progress = server_scan.scan_log(UPGRADE)["progress"]
        self.assertEqual((progress["updated"], progress["updated_modules"]), (2, ["rpc", "account"]))

    def test_the_slowest_modules_are_named(self):
        progress = server_scan.scan_log(UPGRADE)["progress"]
        self.assertEqual(progress["slowest"][0], {"module": "account", "seconds": 11.0})

    def test_finished_says_how_long_it_took(self):
        progress = server_scan.scan_log(FINISHED)["progress"]
        self.assertTrue(progress["finished"])
        self.assertEqual(progress["seconds"], 121.26)
        self.assertIsNone(progress["eta_s"])

    def test_no_module_loaded_no_progress(self):
        self.assertIsNone(server_scan.scan_log("")["progress"])
        self.assertIsNone(server_scan.scan_log(PROBLEMS)["progress"])


class TestThroughAnalyze(unittest.TestCase):
    def test_the_verdict_carries_the_list_the_marks_and_the_progress(self):
        result = server_ctl.analyze_log(UPGRADE + PROBLEMS)
        self.assertTrue(result["items"])
        self.assertTrue(result["marks"]["error"])
        self.assertEqual(result["progress"]["total"], 324)

    def test_with_a_database_chosen_the_list_holds_only_its_items(self):
        mixed = PROBLEMS.replace(" db odoo.http", " other_db odoo.http")
        result = server_ctl.analyze_log(mixed, database="db")
        self.assertFalse([i for i in result["items"] if i["db"] == "other_db"])


if __name__ == "__main__":
    unittest.main()
