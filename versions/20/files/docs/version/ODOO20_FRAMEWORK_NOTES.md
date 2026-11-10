# Odoo 20 Framework Notes

Target Framework: **Odoo 20.0**
Comparison Target: **Odoo 19.0** (see `C:\odoo\odoo19\ODOO19_BREAKING_CHANGES_AND_FRAMEWORK_RULES.md`)

Confirmed facts only. Each entry names the file in `C:\odoo\odoo20\odoo` it was checked against.
A rule that has not been checked against 20.0 source does not belong here yet.

---

## Confirmed

* **Python**: 3.12 minimum, 3.14 maximum (`odoo/release.py`: `MIN_PY_VERSION`, `MAX_PY_VERSION`).
* **Version**: `version_info = (20, 0, 0, FINAL, 0, '')` (`odoo/release.py`). Manifests use `20.0.x.y.z`.

## Carried over from 19 - still to verify against 20.0

1. Translation API `self.env._("Text")`.
2. `from odoo.fields import Domain` instead of `odoo.osv.expression`.
3. Mail composer `default_res_ids`.
4. No inline `parent.` in list contexts.
5. `**kwargs` on `_get_product_catalog_record_lines`.
6. Registered manifest categories.
7. `<group>` inside `<search>` holds only `group_by` filters.
