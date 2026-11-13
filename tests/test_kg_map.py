"""kg_map: the whole tree of a module, from the published graph and, below the project, from the manifests on disk."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import kg_map  # noqa: E402


def write_manifest(folder, name, depends, body=None):
    os.makedirs(os.path.join(folder, name))
    default = "# -*- coding: utf-8 -*-\n{\n    'name': '%s',\n    'version': '1.0',\n    'depends': %r,\n}\n"
    text = body if body is not None else default % (name.title(), depends)
    with open(os.path.join(folder, name, "__manifest__.py"), "w", encoding="utf-8") as handle:
        handle.write(text)


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        core = os.path.join(self.root, "odoo", "addons")
        for name, depends in (("base", []), ("web", ["base"]), ("mail", ["base", "web"]), ("hr", ["mail"]),
                              ("barcodes", ["web"]), ("hr_attendance", ["hr", "barcodes"])):
            write_manifest(core, name, depends)
        write_manifest(os.path.join(self.root, "enterprise"), "ent_payroll", ["hr_attendance"])
        write_manifest(os.path.join(self.root, "third-party", "vendor_pkg"), "vendor_mod", ["base"])
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.con.execute("create table node_module (technical_name text, manifest_name text, version text, module_dir text)")
        self.con.execute("create table edge_depends_on (src_module text, dst_module text, dst_unresolved text)")
        self.con.executemany("insert into node_module values (?, ?, ?, ?)", [
            ("my_mod", "My Module", "19.0.1", "/p/my_mod"), ("sibling", "Sibling", "19.0.1", "/p/sibling"),
            ("user_mod", "User", "19.0.1", "/p/user_mod")])
        self.con.executemany("insert into edge_depends_on values (?, ?, ?)", [
            ("my_mod", None, "hr_attendance"), ("my_mod", None, "ent_payroll"), ("my_mod", None, "vendor_mod"),
            ("my_mod", "sibling", None), ("sibling", None, "base"), ("user_mod", "my_mod", None)])

    def build(self, module):
        return kg_map.build_map(self.con, module, self.root)


class TestTheWholeTree(Fixture):
    def test_a_project_module_brings_the_tree_below_odoo_modules_too(self):
        result = self.build("my_mod")
        kinds = {n["name"]: n["kind"] for n in result["nodes"]}
        self.assertEqual(kinds["my_mod"], "focus")
        self.assertEqual(kinds["sibling"], "project")
        self.assertEqual((kinds["hr_attendance"], kinds["ent_payroll"], kinds["vendor_mod"]), ("core", "enterprise", "third_party"))
        # not only what my_mod declares: what hr_attendance needs, down to the foundation
        for name in ("hr", "mail", "barcodes", "web", "base"):
            self.assertEqual(kinds[name], "core", name)
        self.assertEqual(kinds["user_mod"], "project")

    def test_every_dependency_points_one_way_and_the_foundation_is_the_deepest(self):
        result = self.build("my_mod")
        depth = {n["name"]: n["depth"] for n in result["nodes"]}
        for src, dst in result["edges"]:
            if depth[src] <= 0 and depth[dst] <= 0 and src != dst and not (src == "user_mod" or dst == "user_mod"):
                self.assertLess(depth[dst], depth[src], f"{src} -> {dst}")
        self.assertEqual(min(depth.values()), depth["base"], "base is at the far end")
        self.assertGreater(depth["user_mod"], 0, "the module that needs it is on the other side")
        # the longest path, not the shortest: base is needed by sibling (2 steps) and by web, below hr_attendance (6 steps)
        self.assertEqual(depth["base"], -6)

    def test_every_module_says_where_it_is_so_its_manifest_can_be_opened(self):
        by_name = {n["name"]: n for n in self.build("my_mod")["nodes"]}
        self.assertEqual(by_name["my_mod"]["folder"], "/p/my_mod")
        self.assertEqual(by_name["sibling"]["folder"], "/p/sibling")
        self.assertEqual(by_name["hr_attendance"]["folder"], os.path.join(self.root, "odoo", "addons", "hr_attendance"))
        self.assertTrue(all(by_name[n]["openable"] for n in ("hr_attendance", "ent_payroll", "vendor_mod")))

    def test_a_module_missing_on_disk_is_drawn_but_cannot_be_opened(self):
        self.con.execute("insert into edge_depends_on values ('my_mod', null, 'ghost_module')")
        ghost = {n["name"]: n for n in self.build("my_mod")["nodes"]}["ghost_module"]
        self.assertEqual((ghost["kind"], ghost["openable"], ghost["folder"]), ("unknown", False, None))

    def test_every_edge_has_both_ends_on_the_map(self):
        result = self.build("my_mod")
        names = {n["name"] for n in result["nodes"]}
        self.assertTrue(all(a in names and b in names for a, b in result["edges"]))

    def test_an_odoo_module_can_be_the_one_in_the_middle(self):
        result = self.build("hr_attendance")
        by_name = {n["name"]: n for n in result["nodes"]}
        self.assertEqual(by_name["hr_attendance"]["kind"], "focus")
        self.assertEqual(by_name["hr_attendance"]["origin"], "core")
        self.assertEqual(by_name["hr_attendance"]["label"], "Hr_Attendance", "name and version come from its own manifest")
        for name in ("hr", "mail", "web", "base", "barcodes"):
            self.assertLess(by_name[name]["depth"], 0, name)
        self.assertEqual(by_name["my_mod"]["depth"], 1, "the project modules that need it")
        self.assertEqual(by_name["user_mod"]["depth"], 2)
        self.assertNotIn("ent_payroll", by_name, "the users are the project's modules: the rest of Odoo is not scanned for them")
        self.assertNotIn("sibling", by_name, "what the project module also needs is not part of this module's map")
        self.assertEqual(result["counts"]["dependencies"], 5)

    def test_a_module_that_exists_nowhere_says_so(self):
        result = self.build("no_such_module")
        self.assertIn("error", result)
        self.assertNotIn("nodes", result)

    def test_a_dependency_cycle_does_not_loop(self):
        core = os.path.join(self.root, "odoo", "addons")
        write_manifest(core, "cyc_a", ["cyc_b"])
        write_manifest(core, "cyc_b", ["cyc_a"])
        result = self.build("cyc_a")
        self.assertEqual({n["name"] for n in result["nodes"]}, {"cyc_a", "cyc_b"})

    def test_a_manifest_is_read_as_data_and_never_run(self):
        folder = os.path.join(self.root, "odoo", "addons")
        write_manifest(folder, "sneaky", [], body="{'name': 'x', 'depends': [__import__('os').getcwd()]}\n")
        self.assertEqual(kg_map.read_manifest(os.path.join(folder, "sneaky")), {})

    def test_the_map_stops_at_its_cap_and_says_so(self):
        core = os.path.join(self.root, "odoo", "addons")
        write_manifest(core, "chain0", ["chain1"])
        for i in range(1, 8):
            write_manifest(core, f"chain{i}", [f"chain{i + 1}"])
        write_manifest(core, "chain8", [])
        with mock.patch.object(kg_map, "CAP", 3):
            result = self.build("chain0")
        self.assertTrue(result["truncated"])
        self.assertLess(len(result["nodes"]), 9)


class TestTheCommandLine(unittest.TestCase):
    def test_it_reads_the_generation_named_by_CURRENT_and_prints_one_json_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "data")
            os.makedirs(os.path.join(data, "generations", "gen-1"))
            with open(os.path.join(data, "CURRENT"), "w", encoding="utf-8") as handle:
                json.dump({"generation": "gen-1"}, handle)
            con = sqlite3.connect(os.path.join(data, "generations", "gen-1", "graph.db"))
            con.execute("create table node_module (technical_name text, manifest_name text, version text, module_dir text)")
            con.execute("create table edge_depends_on (src_module text, dst_module text, dst_unresolved text)")
            con.execute("insert into node_module values ('solo', 'Solo', '1.0', '/p/solo')")
            con.commit()
            con.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = kg_map.main(["--data-dir", data, "--module", "solo"])
            result = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual((result["module"], result["generation"], len(result["nodes"])), ("solo", "gen-1", 1))

    def test_an_unreadable_graph_is_reported_not_raised(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = kg_map.main(["--data-dir", os.path.join(tempfile.gettempdir(), "no-such-graph-dir"), "--module", "x"])
        self.assertEqual(code, 0)
        self.assertIn("cannot be read", json.loads(out.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
