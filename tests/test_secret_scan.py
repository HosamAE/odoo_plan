import os
import sys
import unittest

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from secret_scan import scan_line, scan_text  # noqa: E402


class TestSecretScan(unittest.TestCase):
    def kinds(self, path, line):
        return scan_line(path, line)

    def test_real_admin_passwd_in_a_conf_is_found(self):
        self.assertTrue(self.kinds("odoo.conf", "admin_passwd = Sup3rS3cret!"))
        self.assertTrue(self.kinds("debian/odoo.conf", "; admin_passwd = Sup3rS3cret!"))

    def test_odoo_defaults_and_placeholders_are_fine(self):
        for line in ("admin_passwd = admin", "; admin_passwd = admin", "db_password = False", "db_password =",
                     "admin_passwd = ${ADMIN_PASSWD}", "password = <your password>"):
            self.assertEqual(self.kinds("odoo.conf", line), [], line)

    def test_finding_never_contains_the_value(self):
        for _line, kind in scan_text("a.conf", "admin_passwd = Sup3rS3cret!\n"):
            self.assertNotIn("Sup3rS3cret", kind)

    def test_tokens_are_found_in_any_file(self):
        self.assertTrue(self.kinds("a.py", "T = 'ghp_" + "a" * 36 + "'"))
        self.assertTrue(self.kinds("a.txt", "AKIA" + "A" * 16))
        self.assertTrue(self.kinds("k.pem", "-----BEGIN RSA PRIVATE KEY-----"))

    def test_python_field_names_are_not_conf_secrets(self):
        self.assertEqual(self.kinds("models.py", "password = fields.Char()"), [])

    def test_noqa_marks_a_deliberate_line(self):
        self.assertEqual(self.kinds("a.conf", "admin_passwd = Sup3rS3cret!  # noqa: secret test fixture"), [])


if __name__ == "__main__":
    unittest.main()
