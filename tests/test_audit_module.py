import os
import sys
import tempfile
import unittest

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import audit_module  # noqa: E402

MODEL = "from odoo import models\nclass A(models.Model):\n    _name = '%s'\n"


class TestAudit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addon = os.path.join(self._tmp.name, "m")
        os.makedirs(os.path.join(self.addon, "models"))

    def write(self, rel, text):
        path = os.path.join(self.addon, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def manifest(self, data="", version="19.0.1.0.0"):
        self.write("__manifest__.py", "{'name': 'm', 'version': '%s', 'data': [%s]}" % (version, data))

    def test_an_addon_that_only_extends_needs_no_access_file(self):
        self.manifest()
        self.write("models/a.py", "from odoo import models\nclass A(models.Model):\n    _inherit = 'res.partner'\n")
        self.assertEqual(audit_module.audit_addon(self.addon, "19"), ([], []))

    def test_a_name_that_equals_its_inherit_is_an_extension(self):
        self.manifest()
        self.write("models/a.py", "from odoo import models\nclass A(models.Model):\n    _name = 'hr.leave'\n"
                                  "    _inherit = ['hr.leave', 'my.mixin']\n")
        self.assertEqual(audit_module.new_models(self.addon), [])

    def test_abstract_and_transient_models_need_no_row(self):
        self.manifest()
        self.write("models/a.py", "from odoo import models\nclass A(models.AbstractModel):\n    _name = 'x.abs'\n"
                                  "class B(models.TransientModel):\n    _name = 'x.wiz'\n")
        self.assertEqual(audit_module.new_models(self.addon), [])

    def test_a_new_model_without_a_row_is_an_error(self):
        self.manifest()
        self.write("models/a.py", MODEL % "x.thing")
        errors, _warnings = audit_module.audit_addon(self.addon, "19")
        self.assertEqual(len(errors), 1)
        self.assertIn("x.thing", errors[0])

    def test_a_new_model_with_its_row_passes(self):
        self.manifest("'security/ir.model.access.csv'")
        self.write("models/a.py", MODEL % "x.thing")
        self.write("security/ir.model.access.csv",
                   "id,name,model_id:id,group_id:id,perm_read\naccess_x,x,model_x_thing,base.group_user,1\n")
        self.assertEqual(audit_module.audit_addon(self.addon, "19"), ([], []))

    def test_an_access_file_the_manifest_does_not_load_is_a_warning(self):
        self.manifest()
        self.write("security/ir.model.access.csv", "id,name,model_id:id\n")
        self.assertIn("does not load it", audit_module.audit_addon(self.addon, "19")[1][0])

    def test_a_listed_file_that_does_not_exist_is_an_error(self):
        self.manifest("'views/gone.xml'")
        self.assertIn("gone.xml", audit_module.audit_addon(self.addon, "19")[0][0])

    def test_a_missing_manifest_is_an_error(self):
        self.assertIn("__manifest__.py", audit_module.audit_addon(self.addon, "19")[0][0])

    def test_a_version_outside_the_series_is_only_a_warning(self):
        self.manifest(version="21.10.40")
        errors, warnings = audit_module.audit_addon(self.addon, "19")
        self.assertEqual(errors, [])
        self.assertIn("21.10.40", warnings[0])

    def test_a_folder_of_addons_is_audited_addon_by_addon(self):
        self.manifest()
        self.assertEqual(audit_module.addons_under(self._tmp.name), [self.addon])
        self.assertEqual(audit_module.addons_under(self.addon), [self.addon])


if __name__ == "__main__":
    unittest.main()
