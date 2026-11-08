# How an Odoo 17 project is built on this structure

This is the plan every project under `C:\odoo\odoo17\projects\` follows: where it lives, what it carries, which rules its code keeps, and how a change goes from an idea to a push. The tools enforce most of it; what they cannot check, a reviewer does.

## 1. Where it lives

- One folder per project: `C:\odoo\odoo17\projects\<PROJECT>`, its own git repository.
- Next to its addons, five local files that never reach the repository: `<PROJECT>.code-workspace`, `<PROJECT>.conf`, `odools.toml`, `.mcp.json`, `CLAUDE.md`. They come from `tools\project\` (see its README) and `odoo-ai new-project` / `odoo-ai workspace` write them.
- Its knowledge graph: config `tools\kg\config\local\<project>-odoo17.toml`, data `%LOCALAPPDATA%\odoo-kg\projects\<project>-odoo17`.
- Open it through its workspace, never through the folder alone: the workspace carries the interpreter, lint, OdooLS, the run/test configurations and the graph.

## 2. What the repository carries

| Path | Purpose |
| --- | --- |
| `<module>\` | one folder per addon, `__manifest__.py` with a `17.0.x.y.z` version |
| `.github\modules.txt` | every module classified: `gate` (ours: installed, its tests decide the build), `ship` (third-party in production: installed; its tests run only if we wrote them), `skip` (not in production, with the reason) |
| `.github\workflows\ci.yml` | the CI: static checks, then a fresh install of every gate+ship module and every test |
| `.github\scripts\` | the gates the CI and `odoo-ai ci` run (below) |
| `<module>\help\en\`, `help\sv\` | the module's user documentation (gate modules must have it) |
| `<module>\tests\` | its tests (`test_*.py`) |

A new module is added to `modules.txt` in the same commit; the CI refuses a folder the register does not know.

## 3. The rules the code keeps

Checked by the CI (`.github\scripts\`), so they hold without anyone remembering them:

| Rule | Gate |
| --- | --- |
| no undefined names, no unused imports | `check_python_names.py` |
| translations through the environment: `self.env._()`, `request.env._()` in controllers, `env._()` with an env, `_lt = LazyTranslate(__name__)` with none; never `from odoo import _` | `check_python_names.py` |
| values passed to the translation, not formatted after it: `self.env._("%(name)s", name=x)` | `check_odoo17_rules.py` R10 |
| `Domain` from `odoo.fields`, not `odoo.osv.expression` | R1 |
| `search_count()`, not `len(search())` | R3 |
| `default_res_ids: [id]` for the mail composer | R2 |
| defaults as `lambda self: self._method()`, not the method itself | R4 |
| a compute writes only fields that name it as their `compute=` (and a `search=` if filtered on) | R5 |
| `<search>` groups hold only `<filter>`; no `parent.` in view contexts | R6, R7 |
| `_read_group` / `formatted_read_group`, `_compute_display_name` | R8, R9 |
| a module depends on every module whose models, fields or XML ids it uses - or asks first (`"model" in self.env`, `raise_if_not_found=False`) | `check_dependencies.py` |
| no framework warnings from our modules in the Odoo log | `check_test_log.py` |
| no secrets in tracked files; XML well formed; help/ present; versions in the 17.0 series | `ci.yml` steps |

Kept by review (from `C:\odoo\AGENTS.md` and `C:\odoo\odoo17\AGENTS.md`):

- No raw SQL where the ORM can do it; when SQL is needed, values are parameters, never pasted into the query.
- No hard-coded database ids: `env.ref("module.xmlid")`.
- No direct DOM manipulation in OWL: `useState`, `useRef`.
- Every company-owned model has a record rule (`('company_id', 'in', company_ids)`; in Acme also `user.hx_branch_company_ids`).
- "Today" is the user's or the company's day, never the server's: `fields.Date.context_today(record.with_context(tz=...))`.
- Inspect before changing: manifests, dependents (the graph: `kg_find_modules`, `kg_dependents`, `kg_impact`), nearby code, tests.

## 4. Tests

- Every fix comes with the test that would have caught it; every feature with tests of its rules.
- A test that needs a module its own module does not depend on says so: `@skip_unless_installed("other_module")` (`acme_base.tools.testing`). It skips, with the reason, where that module is absent.
- A test that means "today" uses the user's clock (`_today_of(user)` in the HR tests, `datetime.now(tz)` elsewhere), so it holds after midnight in Stockholm and on a server anywhere.
- `odoo-ai ci --at "2026-11-07 23:30"` runs the suite at a chosen UTC moment (the database's own clock stays real).

## 5. From a change to a push

1. Work on the project's branch; keep the graph fresh (`odoo-ai kg index` after commits that matter).
2. Before committing: the **CI** button in VS Code (static checks in seconds; the full run installs everything on a fresh database and runs every test, about 20 minutes) - or `odoo-ai ci` / `odoo-ai ci --static-only`.
3. Bump the version of every module the commit touches (`17.0.x.y.z+1`; modules not deployed anywhere yet may skip it). A version that carries a migration is the migration's folder name.
4. Commit in Odoo's form: `[FIX] module: summary`, `[ADD]`, `[IMP]`, `[REF]`, `[REM]`; the body says why.
5. Pull, then push. The CI on GitHub runs the same gates.

## 6. When the structure itself must change

- A rule every project should keep: add it to a gate in `.github\scripts\` (or `check_odoo17_rules.py`), fix what it finds, then it holds.
- What every project gets (workspace, conf): `tools\project\` on this branch.
- The tools, the CI runner, the graph engine, lint: on this branch only. Another version gets the change by `git cherry-pick`, on purpose.
