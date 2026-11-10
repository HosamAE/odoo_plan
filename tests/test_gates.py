"""The Odoo 19 gates run against sample code: each rule must fire on the bad form and stay quiet on the good one."""
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

GATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gates")
if GATES_DIR not in sys.path:
    sys.path.insert(0, GATES_DIR)

import check_dependencies  # noqa: E402
try:
    from check_odoo19_rules import ENABLED, VERSION, python_findings, xml_findings  # noqa: E402
    HAS_19 = True
except ImportError:   # the 17/18/20 tools carry their own check_odoo<NN>_rules.py
    HAS_19 = False


def rules(findings):
    return sorted(rule for _line, rule, _msg in findings)


@unittest.skipUnless(HAS_19, "Odoo 19 rules only")
class TestPythonRules(unittest.TestCase):
    def test_rules_enabled_for_19(self):
        self.assertEqual(ENABLED, {f"R{n}" for n in range(1, 11)})
        self.assertEqual(VERSION, "19")

    def test_r1_expression_import(self):
        self.assertEqual(rules(python_findings("m.py", "from odoo.osv import expression\n")), ["R1"])
        self.assertEqual(rules(python_findings("m.py", "from odoo.osv.expression import AND\n")), ["R1"])
        self.assertEqual(rules(python_findings("m.py", "from odoo.fields import Domain\n")), [])

    def test_r2_default_res_id(self):
        self.assertEqual(rules(python_findings("m.py", "ctx = {'default_res_id': 4}\n")), ["R2"])
        self.assertEqual(rules(python_findings("m.py", "ctx = {'default_res_ids': [4]}\n")), [])

    def test_r3_len_search(self):
        self.assertEqual(rules(python_findings("m.py", "n = len(self.env['x'].search([]))\n")), ["R3"])
        self.assertEqual(rules(python_findings("m.py", "n = self.env['x'].search_count([])\n")), [])

    def test_r4_default_is_a_method_itself(self):
        bad = (
            "class A:\n"
            "    x = fields.Char(default=_default_x)\n"
            "    def _default_x(self):\n        return 1\n"
        )
        good = bad.replace("default=_default_x", "default=lambda self: self._default_x()")
        self.assertEqual(rules(python_findings("m.py", bad)), ["R4"])
        self.assertEqual(rules(python_findings("m.py", good)), [])

    def test_r5_compute_writes_field_without_compute(self):
        bad = (
            "class A:\n"
            "    total = fields.Float()\n"
            "    other = fields.Float(compute='_compute_other')\n"
            "    def _compute_other(self):\n"
            "        for rec in self:\n            rec.total = 1\n            rec.other = 2\n"
        )
        fixed = bad.replace("total = fields.Float()", "total = fields.Float(compute='_compute_other')")
        self.assertEqual(rules(python_findings("m.py", bad)), ["R5"])
        self.assertEqual(rules(python_findings("m.py", fixed)), [])

    def test_r8_read_group(self):
        self.assertEqual(rules(python_findings("m.py", "self.env['x'].read_group([], [], [])\n")), ["R8"])
        self.assertEqual(rules(python_findings("m.py", "self.env['x']._read_group([], [], [])\n")), [])
        self.assertEqual(rules(python_findings("m.py", "super().read_group([], [], [])\n")), [])

    def test_r9_name_get(self):
        self.assertEqual(rules(python_findings("m.py", "class A:\n    def name_get(self):\n        return []\n")), ["R9"])

    def test_r10_translation_formatted_after_call(self):
        self.assertEqual(rules(python_findings("m.py", "x = self.env._('Hi %s') % name\n")), ["R10"])
        self.assertEqual(rules(python_findings("m.py", "x = self.env._('Hi %s', name)\n")), [])

    def test_syntax_error_is_not_a_crash(self):
        self.assertEqual(python_findings("m.py", "def (:\n"), [])


@unittest.skipUnless(HAS_19, "Odoo 19 rules only")
class TestXmlRules(unittest.TestCase):
    def test_r6_search_group_only_filters(self):
        bad = "<search><group><field name='a'/></group></search>"
        good = "<search><group><filter name='f' context=\"{'group_by': 'a'}\"/></group></search>"
        self.assertEqual(rules(xml_findings("v.xml", bad)), ["R6"])
        self.assertEqual(rules(xml_findings("v.xml", good)), [])

    def test_r6_ignores_group_in_a_comment(self):
        commented = "<search><!-- a <group> here --><filter name='f'/></search>"
        self.assertEqual(rules(xml_findings("v.xml", commented)), [])

    def test_r7_parent_in_context(self):
        self.assertEqual(rules(xml_findings("v.xml", '<field name="a" context="{\'x\': parent.y}"/>')), ["R7"])
        self.assertEqual(rules(xml_findings("v.xml", '<field name="a" context="{\'x\': 1}"/>')), [])

    def test_r7_parent_id_is_allowed(self):
        # Odoo's own catalog buttons: context="{'order_id': parent.id}"
        self.assertEqual(rules(xml_findings("v.xml", '<button context="{\'order_id\': parent.id}"/>')), [])
        self.assertEqual(rules(xml_findings("v.xml", '<button context="{\'x\': parent.identifier}"/>')), ["R7"])

    def test_r2_in_xml(self):
        self.assertEqual(rules(xml_findings("v.xml", "<x context=\"{'default_res_id': 1}\"/>")), ["R2"])


class TestFindOdoo(unittest.TestCase):
    """find_odoo derives the source from the project's location: no C:/odoo in the code."""

    def make_root(self, tmp):
        root = pathlib.Path(tmp)
        base = root / "odoo19" / "odoo" / "odoo" / "addons" / "base"
        base.mkdir(parents=True)
        (base / "__manifest__.py").write_text("{}")
        proj = root / "ProjectsOdoo19" / "demo"
        proj.mkdir(parents=True)
        return root, proj

    def clean_env(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ODOO_ROOT", "ODOO_SRC")}
        return mock.patch.dict(os.environ, env, clear=True)

    def test_derived_from_project_location(self):
        with tempfile.TemporaryDirectory() as tmp, self.clean_env():
            root, proj = self.make_root(tmp)
            with mock.patch.object(check_dependencies, "ROOT", proj):
                self.assertEqual(check_dependencies.odoo_root().resolve(), root.resolve())
                self.assertEqual(check_dependencies.find_odoo(None).resolve(), (root / "odoo19" / "odoo").resolve())

    def test_env_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _proj = self.make_root(tmp)
            with mock.patch.dict(os.environ, {"ODOO_ROOT": str(root)}), \
                    mock.patch.object(check_dependencies, "ROOT", pathlib.Path(tmp) / "elsewhere"):
                self.assertEqual(check_dependencies.odoo_root(), root)

    def test_no_root_found_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp, self.clean_env():
            with mock.patch.object(check_dependencies, "ROOT", pathlib.Path(tmp)):
                self.assertIsNone(check_dependencies.odoo_root())


if __name__ == "__main__":
    unittest.main()
