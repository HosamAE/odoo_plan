import os
import unittest
import sys

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import tempfile

from xpath_lint import check_xml_file, validate_xpath_expr, VALID_POSITIONS


class TestXPathLint(unittest.TestCase):
    def test_valid_expressions(self):
        valid = [
            "//field[@name='partner_id']",
            "//notebook/page[@name='details']",
            "//header/button[@name='action_confirm']",
            "//group[1]/field[@name='date']",
            "//div[hasclass('oe_title')]"
        ]
        for expr in valid:
            errors = validate_xpath_expr(expr)
            self.assertEqual(errors, [], f"Expected '{expr}' to be valid, got: {errors}")

    def test_unbalanced_brackets(self):
        bad = "//field[@name='partner_id'"
        errors = validate_xpath_expr(bad)
        self.assertTrue(any("Unbalanced brackets" in e for e in errors))

    def test_missing_at_symbol(self):
        bad = "//field[name='partner_id']"
        errors = validate_xpath_expr(bad)
        self.assertTrue(any("Missing '@'" in e for e in errors))

    def test_trailing_slash(self):
        bad = "//field[@name='partner_id']/"
        errors = validate_xpath_expr(bad)
        self.assertTrue(any("trailing slash" in e for e in errors))

    def test_valid_positions_set(self):
        expected = {"before", "after", "inside", "replace", "attributes", "move"}
        self.assertEqual(VALID_POSITIONS, expected)

VIEW = """<odoo><record id="v" model="ir.ui.view"><field name="inherit_id" ref="base.view"/><field name="arch" type="xml">
<form>%s</form></field></record></odoo>"""
NO_ROOT = "<!--<odoo>--><!--</odoo>-->\n"


class TestCheckXmlFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.module = self._tmp.name

    def write(self, rel, text, manifest=""):
        path = os.path.join(self.module, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        with open(os.path.join(self.module, "__manifest__.py"), "w", encoding="utf-8") as handle:
            handle.write("{'name': 'm', 'data': [%s]}" % manifest)
        return path

    def xpath(self, position):
        return VIEW % f'<xpath expr="//field[@name=&quot;a&quot;]" position="{position}"/>'.replace("&quot;", "'")

    def test_empty_move_and_replace_xpaths_are_fine(self):
        for position in ("move", "replace"):
            path = self.write("views/v.xml", self.xpath(position))
            self.assertEqual(check_xml_file(path), [], position)

    def test_empty_inside_xpath_is_still_reported(self):
        path = self.write("views/v.xml", self.xpath("inside"))
        self.assertTrue(any("Empty <xpath" in f for f in check_xml_file(path)))

    def test_a_comment_only_file_nobody_loads_is_a_warning_not_an_error(self):
        path = self.write("views/old.xml", NO_ROOT)
        findings = check_xml_file(path)
        self.assertEqual(len(findings), 1)
        self.assertIn("[XML WARNING]", findings[0])
        self.assertIn("dead file", findings[0])

    def test_the_same_file_listed_in_the_manifest_is_an_error(self):
        path = self.write("views/old.xml", NO_ROOT, manifest="'views/old.xml'")
        self.assertIn("[XML SYNTAX ERROR]", check_xml_file(path)[0])

    def test_a_real_syntax_error_is_still_an_error(self):
        path = self.write("views/bad.xml", "<odoo><record></odoo>")
        self.assertIn("[XML SYNTAX ERROR]", check_xml_file(path)[0])

    def test_a_non_utf8_fixture_is_a_warning_unless_loaded(self):
        path = os.path.join(self.module, "tests", "fixture.xml")
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as handle:
            handle.write(b"<odoo>" + bytes([0xF6]) + b"</odoo>")
        with open(os.path.join(self.module, "__manifest__.py"), "w", encoding="utf-8") as handle:
            handle.write("{'name': 'm'}")
        self.assertIn("[XML WARNING]", check_xml_file(path)[0])


if __name__ == "__main__":
    unittest.main()
