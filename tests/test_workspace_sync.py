"""The workspace sync must follow the layout that is on disk: ProjectsOdoo<NN>\\<project> or odoo<NN>\\projects\\<project>."""
import os
import sys
import tempfile
import unittest
from unittest import mock

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import workspace_sync  # noqa: E402

V = workspace_sync.TOOLS_VERSION
WS = '{"folders": [{"name": "odoo", "path": "%s"}, {"name": "Project", "path": "."}], "settings": {}}'


class TestLayouts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        patch = mock.patch.multiple(workspace_sync, FS_ROOT=self.root, WIN_ROOT=self.root.replace("\\", "/"))
        patch.start()
        self.addCleanup(patch.stop)

    def layout(self, external):
        os.makedirs(os.path.join(self.root, f"odoo{V}", "odoo"))
        os.makedirs(os.path.join(self.root, f"ProjectsOdoo{V}" if external else os.path.join(f"odoo{V}", "projects")))

    def test_odoo_source_path_from_a_project_in_projects_odoo(self):
        self.layout(external=True)
        project = workspace_sync.project_dir(V, "demo")
        self.assertEqual(workspace_sync.odoo_source_rel(V, project), f"../../odoo{V}/odoo")

    def test_odoo_source_path_from_a_project_inside_the_version_folder(self):
        self.layout(external=False)
        project = workspace_sync.project_dir(V, "demo")
        self.assertEqual(workspace_sync.odoo_source_rel(V, project), "../../odoo")

    def test_fix_folders_keeps_a_correct_workspace_in_projects_odoo(self):
        self.layout(external=True)
        text = WS % f"../../odoo{V}/odoo"
        new, changed = workspace_sync.fix_folders(text, V, "demo", inner=True)
        self.assertEqual((new, changed), (text, []))

    def test_fix_folders_repairs_the_old_inside_path_in_projects_odoo(self):
        self.layout(external=True)
        new, changed = workspace_sync.fix_folders(WS % "../../odoo", V, "demo", inner=True)
        self.assertIn(f"../../odoo{V}/odoo", new)
        self.assertEqual(len(changed), 1)

    def test_fix_folders_for_a_workspace_beside_the_project(self):
        self.layout(external=True)
        new, _changed = workspace_sync.fix_folders(WS % "../odoo", V, "demo", inner=False)
        self.assertIn(f"../odoo{V}/odoo", new)

    def test_projects_odoo_paths_are_left_alone(self):
        self.layout(external=True)
        root = workspace_sync.WIN_ROOT
        text = '{"a": "%s/ProjectsOdoo%s/demo/demo.conf", "b": "%s"}' % (
            root, V, (root + f"/ProjectsOdoo{V}/demo").replace("/", "\\\\"))
        new, changed = workspace_sync.move_tool_paths(text, V)
        self.assertEqual((new, changed), (text, []), "nothing to move, nothing to report")

    def test_legacy_paths_move_to_the_projects_root_that_exists(self):
        self.layout(external=False)   # only odoo<NN>/projects exists: that is where legacy paths go
        root = workspace_sync.WIN_ROOT
        text = '{"a": "%s/ProjectsOdoo%s/demo/demo.conf"}' % (root, V)
        new, changed = workspace_sync.move_tool_paths(text, V)
        self.assertIn(f"{root}/odoo{V}/projects/demo/demo.conf", new)
        self.assertTrue(changed)


if __name__ == "__main__":
    unittest.main()
