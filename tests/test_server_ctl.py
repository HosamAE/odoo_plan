"""server_ctl: what the extension asks of a project's server - a command line, a verdict on a log, a safe upgrade.

The logs below are the real ones that made this module necessary (2026-11-10): a column the database lacks,
and an upgrade that died because another server was using the database."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import server_ctl  # noqa: E402

HEALTHY = """\
2026-11-10 23:00:01,000 100 INFO ? odoo: Odoo version 19.0
2026-11-10 23:00:05,000 100 INFO db1 odoo.modules.loading: loading acme_work_plan/views/x.xml
2026-11-10 23:00:09,000 100 INFO db1 odoo.modules.loading: Modules loaded.
2026-11-10 23:00:09,100 100 INFO db1 odoo.service.server: HTTP service (werkzeug) running on 0.0.0.0:1900
2026-11-10 23:00:10,000 100 WARNING db1 odoo.addons.attachment_indexation.models.ir_attachment: Attachment indexation of PDF documents is unavailable because the 'pdfminer.six' Python library
"""

# a request that crashed because the database is older than the code
UNDEFINED_COLUMN = HEALTHY + """\
2026-11-10 23:07:44,400 5376 ERROR ax_full_ci3 odoo.http: Exception during request handling.
Traceback (most recent call last):
  File "C:\\odoo\\odoo19\\odoo\\odoo\\sql_db.py", line 440, in execute
    self._obj.execute(query, params)
psycopg2.errors.UndefinedColumn: column res_partner.ax_parakey_external_user_id does not exist
LINE 1: ...ignage_name", "res_partner"."ax_home_company_id", "res_partn...

2026-11-10 23:07:44,469 5376 INFO ax_full_ci3 werkzeug: 127.0.0.1 - - [10/Nov/2026 23:07:44] "GET /odoo HTTP/1.1" 500 - 4 0.003 0.007
"""

# an upgrade that failed: the other server held the same database
CONCURRENT = """\
2026-11-10 23:11:08,000 73360 INFO ? odoo: Odoo version 19.0
2026-11-10 23:11:14,557 73360 WARNING ax_full_ci3 odoo.addons.attachment_indexation.models.ir_attachment: Attachment indexation of PDF documents is unavailable because the 'pdfminer.six' Python librar
2026-11-10 23:11:15,596 73360 ERROR ax_full_ci3 odoo.sql_db: bad query: b'UPDATE ir_module_module SET demo = false WHERE id = 84'
ERROR: could not serialize access due to concurrent update

2026-11-10 23:11:15,597 73360 WARNING ax_full_ci3 odoo.modules.loading: Transient module states were reset
2026-11-10 23:11:15,597 73360 ERROR ax_full_ci3 odoo.registry: Failed to load registry
2026-11-10 23:11:15,597 73360 CRITICAL ax_full_ci3 odoo.service.server: Failed to initialize database `ax_full_ci3`.
"""

BROKEN_VIEW = """\
2026-11-10 23:20:01,000 1 INFO db odoo.modules.loading: loading acme_work_plan/security/ir.model.access.csv
2026-11-10 23:20:02,000 1 INFO db odoo.modules.loading: loading acme_work_plan/views/daymap_views.xml
2026-11-10 23:20:03,000 1 ERROR db odoo.modules.registry: Failed to load registry
Traceback (most recent call last):
  File "x.py", line 1, in y
odoo.tools.convert.ParseError: while parsing file:/c:/x/daymap_views.xml:3
"""


class TestAnalyzeLog(unittest.TestCase):
    def test_a_clean_start_is_healthy_and_the_machine_noise_is_not_counted(self):
        result = server_ctl.analyze_log(HEALTHY)
        self.assertEqual(result["state"], "healthy")
        self.assertEqual((result["errors"], result["critical"], result["http_500"], result["warnings"]),
                         (0, 0, 0, 0), "the pdfminer warning is the machine's")

    def test_nothing_ready_yet_is_starting_not_healthy(self):
        self.assertEqual(server_ctl.analyze_log(HEALTHY.split("odoo.modules.loading: Modules loaded.")[0])["state"],
                         "starting")
        self.assertEqual(server_ctl.analyze_log("")["state"], "starting")

    def test_a_missing_column_is_problems_and_says_what_to_do(self):
        result = server_ctl.analyze_log(UNDEFINED_COLUMN)
        self.assertEqual(result["state"], "problems")
        self.assertEqual(result["http_500"], 1)
        self.assertIn("ax_parakey_external_user_id", result["first_issue"]["message"])
        self.assertIn("-u", result["hint"])
        self.assertIn("older than the code", result["hint"])

    def test_a_concurrent_upgrade_is_failed_and_is_not_blamed_on_a_module(self):
        result = server_ctl.analyze_log(CONCURRENT)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["critical"], 1)
        self.assertIn("Stop the other server", result["hint"])
        self.assertIn("not a broken module", result["hint"])

    def test_a_broken_data_file_names_the_module_and_file_being_loaded(self):
        result = server_ctl.analyze_log(BROKEN_VIEW)
        self.assertEqual(result["state"], "failed")
        issue = result["first_issue"]
        self.assertEqual((issue["module"], issue["file"]), ("acme_work_plan", "views/daymap_views.xml"))
        self.assertIn("ParseError", issue["message"])
        self.assertIn("data file", result["hint"])

    def test_the_same_error_is_listed_once(self):
        line = CONCURRENT.splitlines()[2]   # the ERROR line of the failed query
        self.assertTrue(" ERROR " in line)
        self.assertEqual(server_ctl.analyze_log(HEALTHY + line + "\n" + line + "\n")["errors"], 1)

    def test_the_summary_is_one_readable_block(self):
        text = server_ctl.summarize(server_ctl.analyze_log(UNDEFINED_COLUMN))
        self.assertIn("PROBLEMS", text)
        self.assertIn("First problem", text)
        self.assertIn("What it means", text)


# a traceback Odoo attaches to a WARNING it tolerates: this is in the log of a SUCCESSFUL `-u all`
TOLERATED = """2026-11-10 23:15:43,409 45616 INFO ax_full_ci3 odoo.modules.loading: loading acme_planning/views/board_menus.xml
2026-11-10 23:15:43,424 45616 WARNING ax_full_ci3 odoo.tools.convert: Skipping deletion for missing XML ID `'action_gantt_timeoff'`
Traceback (most recent call last):
  File "C:/odoo/odoo19/odoo/odoo/tools/convert.py", line 261, in _tag_delete
    x
ValueError: External ID not found in the system: acme_planning.action_gantt_timeoff
2026-11-10 23:16:10,567 45616 INFO ax_full_ci3 odoo.modules.loading: Modules loaded.
"""


class TestTolerated(unittest.TestCase):
    def test_a_traceback_on_a_warning_is_a_warning_not_a_failure(self):
        result = server_ctl.analyze_log(TOLERATED)
        self.assertEqual(result["state"], "healthy")
        self.assertEqual((result["errors"], result["critical"]), (0, 0))
        self.assertEqual(result["warnings"], 2, "the warning line and its traceback are counted, as warnings")

    def test_the_same_traceback_on_an_error_is_an_error(self):
        result = server_ctl.analyze_log(TOLERATED.replace("WARNING", "ERROR"))
        self.assertEqual(result["state"], "problems")
        self.assertIn("ValueError", result["issues"][-1]["message"])


class TestBuildCommand(unittest.TestCase):
    def build(self, **kw):
        return server_ctl.build_command("py", "odoo-bin", "p.conf", **kw)

    def test_a_plain_start(self):
        self.assertEqual(self.build(), ["py", "odoo-bin", "-c", "p.conf"])

    def test_database_upgrade_port_and_dev(self):
        self.assertEqual(self.build(database="db1", update="a,b", port=1999, dev=True),
                         ["py", "odoo-bin", "-c", "p.conf", "-d", "db1", "-u", "a,b", "--http-port=1999", "--dev=all"])

    def test_upgrade_all_and_stop_after_init(self):
        command = self.build(database="db1", update="all", stop_after_init=True)
        self.assertEqual(command[-3:], ["-u", "all", "--stop-after-init"])

    def test_an_upgrade_needs_a_database(self):
        with self.assertRaises(ValueError):
            self.build(update="all")

    def test_nothing_that_is_not_a_name_gets_through(self):
        for bad in ({"database": "db; drop"}, {"database": "db", "update": "a b"}, {"database": "db", "update": "a,$(x)"},
                    {"database": "db", "install": "../x"}):
            with self.assertRaises(ValueError):
                self.build(**bad)


class TestConf(unittest.TestCase):
    def test_options_and_port(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False, encoding="utf-8") as handle:
            handle.write("[options]\n; comment = 1\nhttp_port = 1900\ndb_name = False\ndb_password = secret\n")
        try:
            options = server_ctl.read_conf(handle.name)
            self.assertEqual(server_ctl.conf_port(options), 1900)
            self.assertNotIn("comment", options)
        finally:
            os.unlink(handle.name)

    def test_a_missing_conf_is_empty_and_the_default_port(self):
        self.assertEqual(server_ctl.conf_port(server_ctl.read_conf("/no/such.conf")), 8069)


class TestCli(unittest.TestCase):
    def run_cli(self, argv):
        with mock.patch("builtins.print") as printed:
            code = server_ctl.main(argv)
        return code, "\n".join(str(call.args[0]) for call in printed.call_args_list if call.args)

    def test_upgrade_refuses_a_database_another_session_uses(self):
        with mock.patch.object(server_ctl, "other_connections", return_value=3), \
                mock.patch.object(server_ctl.subprocess, "Popen") as popen:
            code, out = self.run_cli(["upgrade", "--conf", "p.conf", "-d", "db1"])
        self.assertEqual(code, 3)
        self.assertIn("REFUSED", out)
        popen.assert_not_called()

    def test_upgrade_runs_when_nobody_else_is_connected_and_reports(self):
        proc = mock.MagicMock()
        proc.stdout = iter(HEALTHY.splitlines(keepends=True))
        proc.returncode = 0
        with mock.patch.object(server_ctl, "other_connections", return_value=0), \
                mock.patch.object(server_ctl.subprocess, "Popen", return_value=proc) as popen:
            code, out = self.run_cli(["upgrade", "--conf", "p.conf", "-d", "db1", "-m", "a,b", "--quiet"])
        self.assertEqual(code, 0)
        command = popen.call_args.args[0]
        self.assertIn("--stop-after-init", command)
        self.assertEqual(command[command.index("-u") + 1], "a,b")
        self.assertIn("HEALTHY", out)

    def test_a_failed_upgrade_exits_nonzero_with_the_hint(self):
        proc = mock.MagicMock()
        proc.stdout = iter(CONCURRENT.splitlines(keepends=True))
        proc.returncode = 0
        with mock.patch.object(server_ctl, "other_connections", return_value=0), \
                mock.patch.object(server_ctl.subprocess, "Popen", return_value=proc):
            code, out = self.run_cli(["upgrade", "--conf", "p.conf", "-d", "ax_full_ci3", "--quiet"])
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)

    def test_an_upgrade_is_judged_by_its_own_database_only(self):
        """The same log, but the upgrade was for another database: that one's errors are not this upgrade's."""
        proc = mock.MagicMock()
        proc.stdout = iter(CONCURRENT.splitlines(keepends=True))
        proc.returncode = 0
        with mock.patch.object(server_ctl, "other_connections", return_value=0), \
                mock.patch.object(server_ctl.subprocess, "Popen", return_value=proc):
            code, out = self.run_cli(["upgrade", "--conf", "p.conf", "-d", "db1", "--quiet"])
        self.assertEqual(code, 0)
        self.assertIn("errors of other databases left out: ax_full_ci3", out)

    def test_upgrade_without_a_database_is_refused(self):
        code, _ = self.run_cli(["upgrade", "--conf", "p.conf"])
        self.assertEqual(code, 2)

    def test_health_reads_a_log_file_and_exits_by_state(self):
        for text, expected in ((HEALTHY, 0), (UNDEFINED_COLUMN, 1), (CONCURRENT, 2)):
            with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as handle:
                handle.write(text)
            try:
                code, out = self.run_cli(["health", "--log", handle.name, "--json"])
                self.assertEqual(code, expected)
                self.assertIn(json.loads(out)["state"], ("healthy", "problems", "failed"))
            finally:
                os.unlink(handle.name)

    def test_health_of_a_missing_log_is_starting(self):
        code, out = self.run_cli(["health", "--log", "/no/such.log", "--json"])
        self.assertEqual((code, json.loads(out)["state"]), (0, "starting"))

    def test_command_prints_a_json_argument_list(self):
        code, out = self.run_cli(["command", "--conf", "p.conf", "-d", "db1", "-u", "all", "--json",
                                  "--python", "py", "--odoo-bin", "ob"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[:4], ["py", "ob", "-c", "p.conf"])

    def test_a_bad_name_is_an_error_not_a_command(self):
        code, out = self.run_cli(["command", "--conf", "p.conf", "-d", "db; x", "--python", "py"])
        self.assertEqual(code, 2)
        self.assertIn("not a database name", out)


if __name__ == "__main__":
    unittest.main()


class TestDrift(unittest.TestCase):
    OUT = 'noise\nHX_DRIFT_JSON:{"acme_key_parakey": ["res.partner.ax_parakey_external_user_id"], "?": ["x.y"]}\n'

    def test_the_answer_is_found_among_the_shells_noise(self):
        self.assertEqual(server_ctl.parse_drift(self.OUT)["acme_key_parakey"],
                         ["res.partner.ax_parakey_external_user_id"])
        self.assertEqual(server_ctl.parse_drift('HX_DRIFT_JSON:{}\n'), {})

    def test_no_answer_is_none_not_an_empty_database(self):
        self.assertIsNone(server_ctl.parse_drift("Traceback ...\nOperationalError"))
        self.assertIsNone(server_ctl.parse_drift("HX_DRIFT_JSON:{broken"))

    def test_the_advice_names_the_modules_to_upgrade(self):
        self.assertEqual(server_ctl.drift_advice({}), "The database matches the code.")
        self.assertEqual(server_ctl.drift_advice({"b_mod": ["a.b"], "a_mod": ["c.d"]}), "Upgrade: -u a_mod,b_mod")
        self.assertIn("-u all", server_ctl.drift_advice({"?": ["x.y"]}))

    def run_cli(self, argv, drift):
        with mock.patch.object(server_ctl, "check_drift", return_value=drift), mock.patch("builtins.print") as shown:
            code = server_ctl.main(argv)
        return code, "\n".join(str(c.args[0]) for c in shown.call_args_list if c.args)

    def test_drift_exits_by_what_it_found(self):
        base = ["drift", "--conf", "p.conf", "-d", "db1", "--python", "py", "--odoo-bin", "ob"]
        self.assertEqual(self.run_cli(base, {})[0], 0)
        code, out = self.run_cli(base + ["--json"], {"m": ["a.b"]})
        self.assertEqual((code, json.loads(out)["advice"]), (1, "Upgrade: -u m"))
        self.assertEqual(self.run_cli(base, None)[0], 2)
        self.assertEqual(self.run_cli(["drift", "--conf", "p.conf"], {})[0], 2)

    def test_the_shell_is_started_for_the_chosen_database_only(self):
        proc = mock.MagicMock(stdout='HX_DRIFT_JSON:{}\n', stderr="")
        with mock.patch.object(server_ctl.subprocess, "run", return_value=proc) as run:
            self.assertEqual(server_ctl.check_drift("py", "ob", "p.conf", "db1"), {})
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["py", "ob", "shell"])
        self.assertEqual(command[command.index("-d") + 1], "db1")
        self.assertEqual(run.call_args.kwargs["input"], server_ctl.DRIFT_SCRIPT)


class TestLocation(unittest.TestCase):
    TRACE = ('2026-11-10 23:00:00,000 1 ERROR db odoo.http: Exception during request handling.\nTraceback (most recent call last):\n'
             '  File "C:\\odoo\\odoo19\\odoo\\odoo\\http.py", line 2856, in __call__\n    x\n'
             '  File "c:\\odoo\\projectsodoo19\\acme\\acme_work_plan\\models\\daymap.py", line 164, in ax_daymap_data\n    y\n'
             '  File "C:\\odoo\\odoo19\\odoo\\odoo\\orm\\fields.py", line 1700, in __get__\n    z\n'
             'AttributeError: \'hr.leave\' object has no attribute \'request_unit_hour\'\n')

    def test_the_deepest_frame_in_the_projects_own_code_is_the_place_to_open(self):
        project = r"C:\odoo\ProjectsOdoo19\Acme"
        result = server_ctl.analyze_log(self.TRACE.replace("\\\\", "\\"), roots=[project])
        location = result["first_issue"]["location"]
        if os.name == "nt":
            self.assertTrue(location["file"].lower().endswith("daymap.py"))
            self.assertEqual(location["line"], 164)
            self.assertIn("In your code:", server_ctl.summarize(result))

    def test_no_roots_no_location(self):
        self.assertIsNone(server_ctl.analyze_log(self.TRACE)["first_issue"]["location"])


class TestDatabaseReport(unittest.TestCase):
    """2026-11-10: `test_db` (collate C, ctype Arabic_Egypt.1256) cannot be opened on Windows; Odoo's database
    selector opens every database, so that one broke the page for all of them."""

    def fake_connect(self, rows, refuse):
        def connect(options, database="postgres"):
            if database in refuse:
                raise RuntimeError("FATAL: collations with different collate and ctype values are not supported")
            conn = mock.MagicMock()
            conn.cursor.return_value.fetchall.return_value = rows
            return conn
        return connect

    ROWS = [("good", "C", "C"), ("mixed_but_fine", "C", "en_US.UTF-8"), ("test_db", "C", "Arabic_Egypt.1256")]

    def test_only_a_database_that_cannot_be_opened_is_set_aside(self):
        with mock.patch.object(server_ctl, "_connect", self.fake_connect(self.ROWS, {"test_db"})):
            usable, unusable = server_ctl.database_report({})
        self.assertEqual(usable, ["good", "mixed_but_fine"])
        self.assertEqual([u["name"] for u in unusable], ["test_db"])
        self.assertIn("Arabic_Egypt.1256", unusable[0]["reason"])

    def test_a_database_with_different_values_that_opens_fine_stays_usable(self):
        with mock.patch.object(server_ctl, "_connect", self.fake_connect(self.ROWS, set())):
            self.assertEqual(server_ctl.database_report({})[1], [])

    def test_the_cli_lists_them_apart(self):
        with mock.patch.object(server_ctl, "_connect", self.fake_connect(self.ROWS, {"test_db"})), \
                mock.patch("builtins.print") as shown:
            code = server_ctl.main(["dbs", "--conf", "p.conf", "--json"])
        data = json.loads(shown.call_args.args[0])
        self.assertEqual((code, data["databases"], [u["name"] for u in data["unusable"]]),
                         (0, ["good", "mixed_but_fine"], ["test_db"]))

    def test_the_failure_it_causes_is_recognised_and_explained(self):
        log = ('2026-11-10 23:36:06,640 64824 ERROR ? odoo.http: Exception during request handling.\n'
               'Traceback (most recent call last):\n  File "x.py", line 1, in f\n    y\n'
               'psycopg2.OperationalError: connection to server at "localhost" (::1), port 5432 failed: FATAL:  '
               'collations with different collate and ctype values are not supported on this platform\n')
        result = server_ctl.analyze_log(log)
        self.assertEqual(result["state"], "problems")
        self.assertIn("-d <name>", result["hint"])
        self.assertIn("odoo-ai server dbs", result["hint"])


# What the log of a server started WITHOUT a database looks like (2026-11-10, real): Odoo's cron threads visit every
# database of the cluster, and each one older than the code fails to load - while the server itself answers.
NO_DATABASE = """\
2026-11-10 23:39:30,027 121120 INFO ? odoo.service.server: HTTP service (werkzeug) running on HoOsS.home:1900
2026-11-10 23:40:31,191 121120 ERROR acme_help_test odoo.sql_db: bad query: b'SELECT "res_company"."id" FROM res_company'
ERROR: column res_company.ax_show_all_branches does not exist
2026-11-10 23:40:31,202 121120 WARNING ? odoo.service.server: cron0 encountered an Exception:
2026-11-10 23:40:31,722 121120 ERROR acme_testing19 odoo.sql_db: bad query: b'SELECT "res_company"."id" FROM res_company'
ERROR: column res_company.ax_show_all_branches does not exist
2026-11-10 23:40:32,100 121120 ERROR ax_ci_v3 odoo.registry: Failed to load registry
2026-11-10 23:40:33,000 121120 INFO ax_try odoo.modules.loading: Modules loaded.
"""


class TestNoDatabaseChosen(unittest.TestCase):
    def test_other_databases_failing_is_a_problem_not_a_dead_server(self):
        result = server_ctl.analyze_log(NO_DATABASE)
        self.assertEqual(result["state"], "problems", "the server answers: it has not failed")
        self.assertEqual(result["databases_failing"], ["acme_help_test", "acme_testing19", "ax_ci_v3"])
        self.assertIn("No database was chosen", result["hint"])
        self.assertIn("-d <database>", result["hint"])
        self.assertIn("Databases that fail to load", server_ctl.summarize(result))

    def test_with_a_database_chosen_only_its_own_errors_count(self):
        result = server_ctl.analyze_log(NO_DATABASE, database="ax_try")
        self.assertEqual(result["state"], "healthy")
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["other_databases"], ["acme_help_test", "acme_testing19", "ax_ci_v3"])
        self.assertIn("left out", server_ctl.summarize(result))

    def test_the_chosen_database_failing_is_still_a_failure(self):
        result = server_ctl.analyze_log(NO_DATABASE, database="ax_ci_v3")
        self.assertEqual(result["state"], "failed")

    def test_the_cli_takes_the_database(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as handle:
            handle.write(NO_DATABASE)
        try:
            with mock.patch("builtins.print") as shown:
                code = server_ctl.main(["health", "--log", handle.name, "--json", "--db", "ax_try"])
            self.assertEqual((code, json.loads(shown.call_args.args[0])["state"]), (0, "healthy"))
        finally:
            os.unlink(handle.name)
