# 🚀 Odoo 19 Framework Changes, Deprecations & Migration Rules

Target Framework: **Odoo 19.0 Community Edition (Master / Cutting-Edge)**
Comparison Target: **Odoo 17.0 & Odoo 18.0**

This document serves as the **authoritative reference** for AI agents and developers building modules on Odoo 19. It details mandatory rules, deprecated syntaxes, and structural differences compared to older versions (17 & 18).

---

## 1. 🌐 Translation API (`self.env._()` vs `from odoo import _`)
* ❌ **Old Way (Odoo 17 / 18)**:
  ```python
  from odoo import _
  name = _("Draft")
  ```
* ⚡ **Odoo 19 Modern Rule**:
  In Odoo 19 models, use **`self.env._("Text")`** dynamically:
  ```python
  name = self.env._("Draft")
  ```
  - `self.env._("Text")` carries the dynamic `env.lang` context from the active user session.
  - Prevents static import warnings in `pylint` and ensures multi-language context is respected in background jobs & ORM calls.

---

## 2. 🗂️ Manifest Categories (`category` field)
* ❌ **Old Way (Odoo 17 / 18)**:
  Loose or arbitrary category strings like `'category': 'Sales'` or `'category': 'Services/Booking'`.
* ⚡ **Odoo 19 Modern Rule**:
  Manifest categories are strictly structured under registered hierarchy nodes (e.g., `Acme/Booking`, `Acme/Conference`, `Acme/Coworking`, `Sales`, `Services`).
  - Arbitrary or invalid categories trigger fallback warning logs and mess up the App Store & Modules catalog classification.

---

## 3. 🔍 Search Views & `<group>` Filter Syntax
* ❌ **Old Way (Odoo 17 / 18)**:
  Nesting arbitrary tags or non-filter nodes inside `<group expand="0" string="Group By">` in `<search>` views.
* ⚡ **Odoo 19 Modern Rule**:
  Odoo 19 XML validator strictly enforces search view syntax:
  - `<group>` tags inside `<search>` views MUST only contain valid `<filter>` items with `context="{'group_by': 'field_name'}"`.
  - Placing non-filter tags inside `<group>` in search views raises XML schema parse errors.

---

## 4. 🧮 Domain Expressions (`odoo.fields.Domain` vs `odoo.osv.expression`)
* ❌ **Deprecated in Odoo 19**:
  ```python
  from odoo.osv import expression
  domain = expression.AND([d1, d2])
  ```
  *Raises DeprecationWarning: Since 19.0, odoo.osv is deprecated use odoo.fields.Domain*
* ⚡ **Odoo 19 Modern Rule**:
  Use **`from odoo.fields import Domain`**:
  ```python
  from odoo.fields import Domain
  domain = Domain.AND([d1, d2])
  ```

---

## 5. ✉️ Mail Composer Context (`default_res_ids` vs `default_res_id`)
* ❌ **Deprecated in Odoo 19**:
  ```python
  context = {"default_res_id": self.id}
  ```
  *Raises fatal ValueError: Deprecated usage of 'default_res_id', should use 'default_res_ids'*
* ⚡ **Odoo 19 Modern Rule**:
  Always pass a list of IDs to `default_res_ids`:
  ```python
  context = {"default_res_ids": [self.id], "default_model": "acme.booking"}
  ```

---

## 6. 🌳 Inline List Views & Context (`parent.` references)
* ❌ **Deprecated in Odoo 19**:
  ```xml
  <tree context="{'default_start_date': parent.start_date}">
  ```
  *Raises Client EvalError: Can not evaluate python expression: Name 'parent' is not defined*
* ⚡ **Odoo 19 Modern Rule**:
  Do NOT use `parent.` in inline list/tree contexts. Set default field values via Python `@api.onchange` in the line model or pass defaults from the main form context.

---

## 7. 🛍️ Product Catalog Overrides (`**kwargs`)
* ❌ **Deprecation Trap in Odoo 19**:
  Overriding `_get_product_catalog_record_lines(self, ...)` without `**kwargs`.
  *Raises TypeError: _get_product_catalog_record_lines() got an unexpected keyword argument 'child_field'*
* ⚡ **Odoo 19 Modern Rule**:
  Always accept `**kwargs` in catalog methods:
  ```python
  def _get_product_catalog_record_lines(self, **kwargs):
      res = super()._get_product_catalog_record_lines(**kwargs)
      return res
  ```

---

## 8. 🖼️ Custom View Registry (`ir.ui.view` & JS Registry)
* ❌ **Enterprise View Trap**:
  Using Enterprise view tags like `<gantt .../>` directly in Community XML without extending Python ORM.
  *Raises ParseError: Invalid view type: 'gantt'*
* ⚡ **Odoo 19 Modern Rule**:
  - Register JS view in `@web/core/registry`:
    ```javascript
    registry.category("views").add("gantt", acmeGanttView);
    ```
  - Extend Python ORM `ir.ui.view`:
    ```python
    type = fields.Selection(selection_add=[("gantt", "Gantt")], ondelete={"gantt": "cascade"})
    ```

---

## 9. ⚡ Mandatory Performance Rules
- ❌ **No `len(search())`**: Always use `search_count(domain)`.
- ❌ **No Raw SQL**: Use ORM methods to preserve security and multi-company domains.
- ❌ **No Hardcoded DB IDs**: Use XML IDs or `env.ref()`.
