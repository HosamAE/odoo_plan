import os
import sys
import unittest

KG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kg")
if KG_DIR not in sys.path:
    sys.path.insert(0, KG_DIR)

from odoo_kg.docindex import name_prefix  # noqa: E402


class TestNamePrefix(unittest.TestCase):
    def test_the_most_common_prefix_wins(self):
        self.assertEqual(name_prefix(["acme_sale", "acme_stock", "acme_hr", "website_x", "web_y"]), "acme_")

    def test_one_module_is_not_a_convention(self):
        self.assertEqual(name_prefix(["acme_sale", "other"]), "")

    def test_a_tie_is_broken_by_name_so_the_graph_is_stable(self):
        self.assertEqual(name_prefix(["b_x", "b_y", "a_x", "a_y"]), "a_")

    def test_no_underscore_no_prefix(self):
        self.assertEqual(name_prefix(["sale", "stock"]), "")
        self.assertEqual(name_prefix([]), "")


if __name__ == "__main__":
    unittest.main()
