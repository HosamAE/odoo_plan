import os
import unittest
import sys

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from xpath_lint import validate_xpath_expr, VALID_POSITIONS


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


if __name__ == "__main__":
    unittest.main()

