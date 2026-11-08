# How an Odoo 19 project is built on this structure

This is the plan every project under `C:\odoo\odoo19\projects\` follows: where it lives, what it carries, which rules its code keeps, and how a change goes from an idea to a push. The tools enforce most of it; what they cannot check, a reviewer does.

## 1. Where it lives

- One folder per project: `C:\odoo\odoo19\projects\<PROJECT>`, its own git repository.
- Next to its addons, five local files that never reach the repository: `<PROJECT>.code-workspace`, `<PROJECT>.conf`, `odools.toml`, `.mcp.json`, `CLAUDE.md`. They come from `tools\project\` (see its README) and `odoo-ai new-project` / `odoo-ai workspace` write them.
- Its knowledge graph: config `tools\kg\config\local\<project>-odoo19.toml`, data `%LOCALAPPDATA%\odoo-kg\projects\<project>-odoo19`.
- Open it through its workspace, never through the folder alone: the workspace carries the interpreter, lint, OdooLS, the run/test configurations and the graph.

## 2. What the repository carries

| Path | Purpose |
| --- | --- |
| `<module>\` | one folder per addon, `__manifest__.py` with a `19.0.x.y.z` version |
| `.github\modules.txt` | every module classified: `gate` (ours: installed, its tests decide the build), `ship` (third-party in production: installed; its tests run only if we wrote them), `skip` (not in production, with the reason) |
| `.github\workflows\ci.yml` | the CI: static checks, then a fresh install of every gate+ship module and every test |
| `.github\scripts\` | the gates the CI and `odoo-ai ci` run (below) |
| `<module>\help\en\`, `help\sv\` | the module's user documentation (gate modules must have it) |
| `<module>\tests\` | its tests (`test_*.py`) |

A new module is added to `modules.txt` in the same commit; the CI refuses a folder the register does not know.

## 3. The rules the code keeps

Checked by the version's gates (`tools\gates\`, run by `odoo-ai ci` on every project of this version; a project that also carries them in `.github\scripts` - `odoo-ai gates --install` - has GitHub's CI run them too, and from then on they fail the run instead of warning):

| Rule | Gate |
| --- | --- |
| no undefined names, no unused imports | `check_python_names.py` |
| translations through the environment: `self.env._()`, `request.env._()` in controllers, `env._()` with an env, `_lt = LazyTranslate(__name__)` with none; never `from odoo import _` | `check_python_names.py` |
| values passed to the translation, not formatted after it: `_("%(name)s", name=x)` | `check_odoo19_rules.py` R10 |
| `Domain` from `odoo.fields`, not `odoo.osv.expression` | R1 |
| `search_count()`, not `len(search())` | R3 |
| `default_res_ids: [id]` for the mail composer (`default_res_id` raises) | R2 |
| defaults as `lambda self: self._method()`, not the method itself | R4 |
| a compute writes only fields that name it as their `compute=` (and a `search=` if filtered on) | R5 |
| `<search>` groups hold only `<filter>`; no `parent.` in view contexts | R6, R7 |
| `_read_group` / `formatted_read_group`, not `read_group` | R8 |
| `_compute_display_name`, not `name_get` | R9 |
| a module depends on every module whose models, fields or XML ids it uses - or asks first (`"model" in self.env`, `raise_if_not_found=False`) | `check_dependencies.py` |
| no framework warnings from our modules in the Odoo log | `check_test_log.py` |

Kept by review (from `C:\odoo\AGENTS.md` and `C:\odoo\odoo19\AGENTS.md`):

- No raw SQL where the ORM can do it; when SQL is needed, values are parameters, never pasted into the query.
- No hard-coded database ids: `env.ref("module.xmlid")`.
- No direct DOM manipulation in OWL: `useState`, `useRef`.
- Every company-owned model has a record rule (`('company_id', 'in', company_ids)`; in Acme also `user.hx_branch_company_ids`).
- "Today" is the user's or the company's day, never the server's: `fields.Date.context_today(record.with_context(tz=...))`.
- Inspect before changing: manifests, dependents (the graph: `kg_find_modules`, `kg_dependents`, `kg_impact`), nearby code, tests.

## 4. Tests

- Every fix comes with the test that would have caught it; every feature with tests of its rules.
- A test that needs a module its own module does not depend on says so and skips where that module is absent (Acme: `@skip_unless_installed("other_module")` from `acme_base.tools.testing`; another project gives itself the same helper).
- A test that means "today" uses the user's clock (`fields.Date.context_today(user.with_context(tz=user.tz))`, `datetime.now(tz)`), so it holds after midnight in Stockholm and on a server anywhere.
- `odoo-ai ci --at "2026-11-07 23:30"` runs the suite at a chosen UTC moment (the database's own clock stays real).

## 5. From a change to a push

1. Once per project: `odoo-ai gates --install`, and add the printed steps to its `ci.yml`, so the version's gates bind it on GitHub too.
2. Work on the project's branch; keep the graph fresh (`odoo-ai kg index` after commits that matter).
3. Before committing: the **CI** button in VS Code (static checks in seconds; the full run installs everything on a fresh database and runs every test, about 20 minutes) - or `odoo-ai ci` / `odoo-ai ci --static-only`.
4. Bump the version of every module the commit touches (`19.0.x.y.z+1`; modules not deployed anywhere yet may skip it). A version that carries a migration is the migration's folder name.
5. Commit in Odoo's form: `[FIX] module: summary`, `[ADD]`, `[IMP]`, `[REF]`, `[REM]`; the body says why.
6. Pull, then push. The CI on GitHub runs the same gates.

## 6. When the structure itself must change

- A rule every project should keep: add it to a gate in `tools\gates\` (`check_odoo19_rules.py` for a framework rule), fix what it finds, then `odoo-ai gates --install` in each project.
- What every project gets (workspace, conf): `tools\project\` on this branch.
- The tools, the CI runner, the graph engine, lint: on this branch only. Another version gets the change by `git cherry-pick`, on purpose.
