"""i18n_status: what is left to translate, and translating with a model of your own (the model is replaced here)."""
import os
import sys
import tempfile
import unittest

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import i18n_status  # noqa: E402

MODEL = '''from odoo import fields, models


class Thing(models.Model):
    _name = "x.thing"
    _description = "A thing"

    name = fields.Char("Name", help="Say it")
    kind = fields.Selection([("a", "Small"), ("b", "Large")], string="Kind")

    def go(self):
        return self.env._("Done %s", 1)
'''
VIEW = '''<odoo><record id="v" model="ir.ui.view"><field name="name">v</field><field name="arch" type="xml">
<form string="The form"><group><label for="name" string="Label here"/><span>Some visible text</span></group></form>
</field></record></odoo>'''
def put(path, text):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def get(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


PO_HEADER = 'msgid ""\nmsgstr ""\n"Language: sv_SE\\n"\n\n'


def make_module(root, name="mod_a"):
    folder = os.path.join(root, name)
    os.makedirs(os.path.join(folder, "models"))
    os.makedirs(os.path.join(folder, "views"))
    put(os.path.join(folder, "__manifest__.py"), "{'name': 'A'}")
    put(os.path.join(folder, "models", "thing.py"), MODEL)
    put(os.path.join(folder, "views", "v.xml"), VIEW)
    return folder


class TestTerms(unittest.TestCase):
    def test_labels_help_selection_description_code_and_view_text_are_terms(self):
        with tempfile.TemporaryDirectory() as root:
            found = i18n_status.terms(make_module(root))
        for wanted in ("A thing", "Name", "Say it", "Small", "Large", "Kind", "Done %s", "The form", "Label here", "Some visible text"):
            self.assertIn(wanted, found, wanted)

    def test_tests_are_not_terms(self):
        with tempfile.TemporaryDirectory() as root:
            folder = make_module(root)
            os.makedirs(os.path.join(folder, "tests"))
            put(os.path.join(folder, "tests", "test_x.py"), 'x = fields.Char("Only in a test")\n')
            self.assertNotIn("Only in a test", i18n_status.terms(folder))


class TestPo(unittest.TestCase):
    def test_a_po_is_read_with_multiline_entries_and_fuzzy_marks(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "sv.po")
            put(path, 
                PO_HEADER + 'msgid "Name"\nmsgstr "Namn"\n\n#, fuzzy\nmsgid "Kind"\nmsgstr "Sort"\n\n'
                'msgid ""\n"A long "\n"sentence"\nmsgstr ""\n"En lång "\n"mening"\n')
            entries = i18n_status.read_po(path)
        self.assertEqual(entries["Name"], ("Namn", False))
        self.assertEqual(entries["Kind"], ("Sort", True))
        self.assertEqual(entries["A long sentence"], ("En lång mening", False))

    def test_filling_changes_only_empty_entries_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "sv.po")
            text = PO_HEADER + '#: models/thing.py:7\nmsgid "Name"\nmsgstr "Namn"\n\n#: models/thing.py:8\nmsgid "Kind"\nmsgstr ""\n\n'
            put(path, text)
            i18n_status.write_translations(path, {"Name": "SHOULD NOT REPLACE", "Kind": 'Sort "x"'})
            after = get(path)
        self.assertIn('msgstr "Namn"', after)
        self.assertNotIn("SHOULD NOT REPLACE", after)
        self.assertIn('msgstr "Sort \\"x\\""', after)
        self.assertTrue(after.startswith(PO_HEADER) and after.endswith("\n\n"))


class TestStatus(unittest.TestCase):
    def test_a_language_with_no_file_is_all_missing_and_not_complete(self):
        with tempfile.TemporaryDirectory() as root:
            status = i18n_status.module_status(make_module(root), ["sv_SE"])
        item = status["languages"][0]
        self.assertEqual((item["exists"], item["translated"], item["complete"]), (False, 0, False))
        self.assertEqual(item["missing"], status["terms"])

    def test_when_every_term_is_translated_nothing_is_left(self):
        with tempfile.TemporaryDirectory() as root:
            folder = make_module(root)
            os.makedirs(os.path.join(folder, "i18n"))
            terms = i18n_status.terms(folder)
            body = "".join(f'msgid {i18n_status._quote(t)}\nmsgstr {i18n_status._quote("t-" + t)}\n\n' for t in terms)
            put(os.path.join(folder, "i18n", "sv_SE.po"), PO_HEADER + body)
            item = i18n_status.module_status(folder)["languages"][0]
        self.assertEqual((item["missing"], item["percent"], item["complete"]), (0, 100, True))

    def test_empty_and_fuzzy_entries_are_not_translated(self):
        with tempfile.TemporaryDirectory() as root:
            folder = make_module(root)
            os.makedirs(os.path.join(folder, "i18n"))
            put(os.path.join(folder, "i18n", "sv_SE.po"), 
                PO_HEADER + 'msgid "Name"\nmsgstr ""\n\n#, fuzzy\nmsgid "Kind"\nmsgstr "Sort"\n\nmsgid "Gone term"\nmsgstr "Borta"\n')
            item = i18n_status.module_status(folder)["languages"][0]
        self.assertEqual((item["translated"], item["fuzzy"], item["obsolete"]), (0, 1, 1))


class TestTranslate(unittest.TestCase):
    def setup_module(self, root):
        folder = make_module(root)
        os.makedirs(os.path.join(folder, "i18n"))
        terms = sorted(i18n_status.terms(folder))
        put(os.path.join(folder, "i18n", "sv_SE.po"), 
            PO_HEADER + "".join(f"msgid {i18n_status._quote(t)}\nmsgstr \"\"\n\n" for t in terms))
        return folder

    def test_translations_are_written_and_a_lost_placeholder_is_skipped(self):
        def model(endpoint, name, key, lang, batch):
            return {i: ("Klart %d" if t == "Done %s" else "ö " + t) for i, t in enumerate(batch)}   # "Done %s" lost its %s
        with tempfile.TemporaryDirectory() as root:
            folder = self.setup_module(root)
            result = i18n_status.translate_module(folder, "sv_SE", "http://x", "m", "", batch_size=4, ask=model)
            item = i18n_status.module_status(folder)["languages"][0]
        self.assertIn("Done %s", result["skipped"])
        self.assertEqual(item["missing"], 1)
        self.assertEqual(result["translated"], item["total"] - 1)

    def test_a_language_without_a_file_must_be_added_first(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(i18n_status.TranslateError):
                i18n_status.translate_module(make_module(root), "sv_SE", "http://x", "m", "")

    def test_an_unreachable_model_is_an_error_in_words(self):
        with tempfile.TemporaryDirectory() as root:
            folder = self.setup_module(root)
            with self.assertRaises(i18n_status.TranslateError) as raised:
                i18n_status.translate_module(folder, "sv_SE", "http://127.0.0.1:9/v1", "m", "")
        self.assertIn("cannot reach", str(raised.exception))

    def test_already_complete_asks_the_model_nothing(self):
        calls = []
        with tempfile.TemporaryDirectory() as root:
            folder = self.setup_module(root)
            i18n_status.translate_module(folder, "sv_SE", "x", "m", "", batch_size=1000, ask=lambda *a: {i: "x " + t for i, t in enumerate(a[4])})
            i18n_status.translate_module(folder, "sv_SE", "x", "m", "", ask=lambda *a: calls.append(a) or {})
        self.assertEqual(calls, [])

    def test_placeholders_are_compared_as_sets_of_tokens(self):
        self.assertEqual(i18n_status.placeholders("Hi %(name)s, %d <b>x</b>"), i18n_status.placeholders("Hej %d, <b>y</b> %(name)s"))
        self.assertNotEqual(i18n_status.placeholders("Hi %s"), i18n_status.placeholders("Hej"))


class TestOverHttp(unittest.TestCase):
    """The real request to an OpenAI-compatible endpoint, answered by a server of our own on this machine."""

    def serve(self, reply):
        import http.server
        import json
        import threading
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["path"], seen["auth"] = self.path, self.headers.get("Authorization")
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                data = json.dumps(reply(seen["body"])).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/v1", seen

    def test_the_request_and_the_answer_with_a_fenced_json_reply(self):
        import json
        fence = chr(96) * 3
        reply = fence + "json" + chr(10) + json.dumps({"0": "Namn", "1": "Hej %s"}) + chr(10) + fence
        endpoint, seen = self.serve(lambda body: {"choices": [{"message": {"content": reply}}]})
        answer = i18n_status.ask_model(endpoint, "m1", "secret-key", "sv_SE", ["Name", "Hi %s"])
        self.assertEqual(answer, {0: "Namn", 1: "Hej %s"})
        self.assertEqual((seen["path"], seen["auth"], seen["body"]["model"]), ("/v1/chat/completions", "Bearer secret-key", "m1"))
        self.assertIn("Name", seen["body"]["messages"][1]["content"])

    def test_no_key_sends_no_authorization(self):
        endpoint, seen = self.serve(lambda body: {"choices": [{"message": {"content": "{}"}}]})
        i18n_status.ask_model(endpoint, "m", "", "sv_SE", ["x"])
        self.assertIsNone(seen["auth"])

    def test_an_answer_that_is_not_json_is_an_error_in_words(self):
        endpoint, _ = self.serve(lambda body: {"choices": [{"message": {"content": "Sure! Here you go."}}]})
        with self.assertRaises(i18n_status.TranslateError) as raised:
            i18n_status.ask_model(endpoint, "m", "", "sv_SE", ["x"])
        self.assertIn("expected form", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
