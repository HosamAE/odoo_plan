"""The workspace sync must follow the layout that is on disk: ProjectsOdoo<NN>\\<project> or odoo<NN>\\projects\\<project>."""
import os
import sys
import tempfile
import unittest
from unittest import mock

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

os.environ["ODOO_AI_WORKSPACE_LOCAL"] = ""   # these tests are about the tools, not about the estate of the machine they run on
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


KG_WORKSPACE = """{
  "folders": [{"name": "odoo", "path": "../../odoo19/odoo"}, {"name": "Project", "path": "."}],
  // a comment that has to survive
  "tasks": {
    "version": "2.0.0",
    "tasks": [
      {"label": "odoo-ai: lint this module", "type": "process"},
      {
        "label": "odoo-kg: build graph",
        "type": "process"
      },
      {
        "label": "odoo-kg: status",
        "type": "process",
        "runOptions": {"runOn": "folderOpen"}
      },
      {
        "label": "odoo-kg: doctor",
        "type": "process"
      }
    ]
  },
  "settings": {
    "mcp": {
      "servers": {
        "odoo-kg": {"type": "stdio", "command": "py", "args": ["-m", "odoo_kg", "serve"]},
        "other": {"type": "stdio", "command": "x"}
      }
    },
    "python.languageServer": "None"
  }
}
"""


class TestKgPythonIsOfItsOwnVersion(unittest.TestCase):
    """`odoo-ai workspace` printed (and recorded in local.json) the Python of another version: whichever interpreter
    started the tool. Each version builds its graphs with its own venv."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        for patch in (mock.patch.multiple(workspace_sync, FS_ROOT=self.root, WIN_ROOT=self.root.replace("\\", "/"), TOOLS_VERSION="19"),
                      mock.patch.dict(os.environ, {"LOCALAPPDATA": os.path.join(self.root, "nowhere")}, clear=False)):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("ODOO_KG_PYTHON", None)

    def venv(self, version, python_version):
        folder = os.path.join(self.root, f"odoo{version}", "venv")
        exe = os.path.join(folder, "Scripts" if os.name == "nt" else "bin", "python.exe" if os.name == "nt" else "python")
        os.makedirs(os.path.dirname(exe))
        open(exe, "w").close()
        with open(os.path.join(folder, "pyvenv.cfg"), "w") as handle:
            handle.write(f"home = x\nversion = {python_version}\n")
        return exe.replace("\\", "/")

    def test_its_own_venv_is_used_and_another_versions_is_not(self):
        other = self.venv("20", "3.12.10")   # the interpreter that started the tool may be this one
        own = self.venv("19", "3.12.10")
        self.assertEqual(workspace_sync.find_kg_python(), own)
        self.assertNotEqual(workspace_sync.find_kg_python(), other)

    def test_a_venv_older_than_3_11_cannot_run_the_graph_and_is_skipped(self):
        self.venv("19", "3.10.11")
        fallback = mock.patch.object(workspace_sync.sys, "executable", "C:/fallback/python.exe")
        with mock.patch.object(workspace_sync.sys, "version_info", (3, 12, 1)), fallback:
            self.assertEqual(workspace_sync.find_kg_python(), "C:/fallback/python.exe")

    def test_an_installed_python_comes_before_the_one_that_started_the_tool(self):
        installed = os.path.join(self.root, "nowhere", "Programs", "Python", "Python312", "python.exe")
        os.makedirs(os.path.dirname(installed))
        open(installed, "w").close()
        with mock.patch.object(workspace_sync.sys, "executable", "C:/some/other/odoo20/venv/python.exe"):
            self.assertEqual(workspace_sync.find_kg_python(), installed.replace("\\", "/"))

    def test_an_explicit_choice_and_the_environment_still_win(self):
        self.venv("19", "3.12.10")
        self.assertEqual(workspace_sync.find_kg_python("D:\\mine\\python.exe"), "D:/mine/python.exe")
        with mock.patch.dict(os.environ, {"ODOO_KG_PYTHON": "E:\\env\\python.exe"}):
            self.assertEqual(workspace_sync.find_kg_python(), "E:/env/python.exe")


class TestGraphOutOfTheWorkspace(unittest.TestCase):
    """The graph is built and refreshed by the Cockpit's Graph page and read by Claude from .mcp.json: the workspace
    no longer carries odoo-kg tasks or a registration in VS Code's MCP list (they showed up as separate things)."""

    def test_the_three_tasks_go_and_everything_else_stays(self):
        new, removed = workspace_sync.remove_kg_tasks(KG_WORKSPACE)
        self.assertEqual(removed, 3)
        data = workspace_sync.loads_jsonc(new)
        self.assertEqual([t["label"] for t in data["tasks"]["tasks"]], ["odoo-ai: lint this module"])
        self.assertIn("// a comment that has to survive", new)
        self.assertNotIn("odoo-kg: ", new)
        self.assertEqual(data["settings"]["python.languageServer"], "None")

    def test_removing_the_tasks_twice_changes_nothing(self):
        once, _ = workspace_sync.remove_kg_tasks(KG_WORKSPACE)
        again, removed = workspace_sync.remove_kg_tasks(once)
        self.assertEqual(removed, 0)
        self.assertEqual(again, once)

    def test_a_workspace_without_them_is_left_as_written(self):
        text = '{"tasks": {"tasks": [{"label": "mine"}]}}'
        self.assertEqual(workspace_sync.remove_kg_tasks(text), (text, 0))

    def test_the_server_leaves_VS_Codes_mcp_list_and_other_servers_stay(self):
        new, changed = workspace_sync.remove_graph_registration(KG_WORKSPACE)
        servers = workspace_sync.loads_jsonc(new)["settings"]["mcp"]["servers"]
        self.assertEqual(list(servers), ["other"])
        self.assertEqual(changed, ["odoo-kg removed from VS Code's MCP list"])

    def test_a_block_that_held_only_odoo_kg_goes_with_it(self):
        text = '{"settings": {"mcp": {"servers": {"odoo-kg": {"command": "py"}}}, "python.languageServer": "None"}}'
        new, changed = workspace_sync.remove_graph_registration(text)
        settings = workspace_sync.loads_jsonc(new)["settings"]
        self.assertNotIn("mcp", settings)
        self.assertEqual(settings["python.languageServer"], "None")
        self.assertEqual(len(changed), 2)

    def test_an_empty_block_is_not_touched(self):
        # Odoo 19 and 20 projects had one: the old tool stopped the whole sync on it ("add the server by hand")
        for text in ('{"settings": {"mcp": {"servers": {}}}}', '{"settings": {"mcp": {"servers": {},},},}'):
            self.assertEqual(workspace_sync.remove_graph_registration(text), (text, []))

    def test_the_generators_of_the_tasks_and_the_registration_are_gone(self):
        self.assertFalse(hasattr(workspace_sync, "ensure_graph_settings"))
        self.assertFalse(hasattr(workspace_sync, "kg_tasks"))

    def test_the_rules_file_points_to_the_cockpit(self):
        self.assertIn("Graph page of the Cockpit Odoo", workspace_sync.KG_SECTION)
        self.assertNotIn("VS Code task", workspace_sync.KG_SECTION)


class TestNothingAboutAnEstateIsBuiltIn(unittest.TestCase):
    """The tools know no company and no project: what is known about an estate comes from tools/local/workspace_local.py."""

    def test_without_a_local_file_every_project_is_alike(self):
        self.assertEqual(workspace_sync.family("17", "anything"), "other")
        self.assertEqual(workspace_sync.family("19", "Acme"), "other")
        self.assertEqual((workspace_sync.PROJECT_ID_OVERRIDES, workspace_sync.PATTERN_PROJECT, workspace_sync.NEW_PORTS), ({}, {}, {}))
        self.assertEqual(workspace_sync.project_id("My Project", "19"), workspace_sync.project_id("My Project", "19"))

    def test_the_shared_sources_name_no_company_or_customer(self):
        with open(workspace_sync.__file__, encoding="utf-8") as handle:
            source = handle.read().lower()
        for word in ("acme", "hoss", "clientb", "clientc", "sample_engine", "sample_helper", "sample_shared"):
            self.assertNotIn(word, source, word)

    def with_local(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "workspace_local.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        patch = mock.patch.object(workspace_sync, "LOCAL_FILE", path)
        patch.start()
        self.addCleanup(patch.stop)
        loaded = workspace_sync._load_local()
        patch2 = mock.patch.object(workspace_sync, "LOCAL", loaded)
        patch2.start()
        self.addCleanup(patch2.stop)

    def test_a_local_file_declares_families_ids_and_addon_repos(self):
        self.with_local(
            'FAMILIES = [{"name": "acme", "versions": ["17"], "projects": ["Core", "web"], "origins": {"x": "vendor"}}]\n'
            'ADDON_REPOS = {"shared_addons"}\n'
            'PROJECT_IDS = {("17", "core"): "acme-core"}\n')
        with mock.patch.object(workspace_sync, "FAMILIES", list(workspace_sync.LOCAL.FAMILIES)), \
                mock.patch.object(workspace_sync, "ADDON_REPOS", set(workspace_sync.LOCAL.ADDON_REPOS)):
            self.assertEqual(workspace_sync.family("17", "core"), "acme")
            self.assertEqual(workspace_sync.family("19", "core"), "other", "a family is for the versions it names")
            self.assertEqual(workspace_sync.family("17", "Shared_Addons"), "addon-repo")
            self.assertEqual(workspace_sync.family_of("17", "WEB")["origins"], {"x": "vendor"})

    def test_a_broken_local_file_is_loud_not_ignored(self):
        with self.assertRaises(SyntaxError):
            self.with_local("FAMILIES = [")

    def test_the_claude_section_of_a_family_comes_from_the_local_file(self):
        self.with_local('def claude_section(version, name, family, git, project_path):\n'
                        '    return "## About %s" % name if family == "acme" else None\n')
        with mock.patch.object(workspace_sync, "family", lambda v, n: "acme"):
            text = workspace_sync.claude_md("17", "core", "core-odoo17", "acme", None, None, [])
        self.assertIn("## About core", text)


if __name__ == "__main__":
    unittest.main()
