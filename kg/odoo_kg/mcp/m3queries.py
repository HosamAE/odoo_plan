"""Read-only queries over the M3 layers: models, XML IDs, security.

The same two rules as :mod:`odoo_kg.mcp.queries`, and for the same reasons:

* **No arbitrary SQL.** Every statement is a literal here. Caller input reaches
  the database only through ``?`` placeholders. No tool accepts a database
  path, a table name, or a fragment of SQL.
* **Nothing unbounded.** Every list takes a limit and reports whether it hit
  it, so a caller is never handed a truncated answer that looks complete.

One rule specific to the security queries: they return what the source
*declares*. They do not combine ACL rows, do not evaluate domains, and do not
answer "may this user do this". That question needs the user, their groups,
their company and the record -- none of which is here.
"""

from __future__ import annotations

import sqlite3

#: A hard ceiling on any detail list, independent of what a caller asks for.
#: Bounds the answer even when a model is referenced by hundreds of things.
MAX_ROWS = 100


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _rows(connection, sql, params=(), limit=None):
    """Run a bounded query. Returns (rows, truncated).

    One extra row is fetched so truncation is detected rather than guessed at.
    """
    if limit is None:
        return [dict(r) for r in connection.execute(sql, params)], False
    fetched = [dict(r) for r in connection.execute(sql, (*params, limit + 1))]
    return fetched[:limit], len(fetched) > limit


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


def find_models(connection, query, module, kind, limit):
    sql = """
        SELECT m.model_name, m.kind, m.definition_count, m.declares_name,
               m.field_count, m.method_count
        FROM node_model m
        WHERE (? = '' OR m.model_name LIKE ? ESCAPE '\\')
          AND (? = '' OR EXISTS (
                SELECT 1 FROM node_model_def d
                WHERE d.model_name = m.model_name AND d.module = ?))
          AND (? = '' OR m.kind = ?)
        ORDER BY m.model_name
        LIMIT ?
    """
    pattern = f"%{_escape_like(query)}%" if query else ""
    return _rows(
        connection, sql,
        (query or "", pattern, module or "", module or "", kind or "", kind or ""),
        limit,
    )


def get_model(connection, model):
    row = connection.execute(
        "SELECT model_name, kind, definition_count, declares_name, field_count, "
        "method_count FROM node_model WHERE model_name = ?", (model,)
    ).fetchone()
    return dict(row) if row else None


def model_names_like(connection, model, limit=5):
    like = f"%{_escape_like(model)}%"
    return [
        r["model_name"] for r in connection.execute(
            "SELECT model_name FROM node_model WHERE model_name LIKE ? ESCAPE '\\' "
            "ORDER BY model_name LIMIT ?", (like, limit)
        )
    ]


def model_definitions(connection, model, limit):
    return _rows(
        connection,
        "SELECT def_id, module, rel_path, line, class_name, base_kind, shape, "
        "parse_state, field_count, method_count FROM node_model_def "
        "WHERE model_name = ? ORDER BY module, rel_path, line LIMIT ?",
        (model,), limit,
    )


def model_inherits(connection, model, limit):
    return _rows(
        connection,
        "SELECT e.dst_unresolved AS target, e.binding, d.module, e.src_file, "
        "e.src_line FROM edge_model_inherits e "
        "JOIN node_model_def d ON d.def_id = e.src_id "
        "WHERE d.model_name = ? ORDER BY e.dst_unresolved LIMIT ?",
        (model,), limit,
    )


def model_extended_by(connection, model, limit):
    """Definitions that inherit this model, i.e. who extends it."""
    return _rows(
        connection,
        "SELECT d.model_name, d.module, d.rel_path, d.line, d.class_name, d.shape "
        "FROM edge_model_inherits e JOIN node_model_def d ON d.def_id = e.src_id "
        "WHERE e.dst_id = ? ORDER BY d.module, d.rel_path, d.line LIMIT ?",
        (model,), limit,
    )


def model_fields(connection, model, limit):
    return _rows(
        connection,
        "SELECT field_name, field_type, is_relational, comodel_literal, "
        "comodel_state, related_path, compute_method, rel_path, line "
        "FROM node_field WHERE model_name = ? ORDER BY field_name LIMIT ?",
        (model,), limit,
    )


def model_methods(connection, model, limit):
    return _rows(
        connection,
        "SELECT method_name, kind, decorators, depends_literal, rel_path, line "
        "FROM node_method WHERE model_name = ? ORDER BY method_name LIMIT ?",
        (model,), limit,
    )


def fields_pointing_at(connection, model, limit):
    """Relational fields on other models whose comodel is this one."""
    return _rows(
        connection,
        "SELECT f.model_name, f.field_name, f.field_type, e.binding, "
        "f.rel_path, f.line FROM edge_field_relation e "
        "JOIN node_field f ON f.field_id = e.src_id "
        "WHERE e.dst_id = ? ORDER BY f.model_name, f.field_name LIMIT ?",
        (model,), limit,
    )


def records_of_model(connection, model, limit):
    """XML records that instantiate this model."""
    return _rows(
        connection,
        "SELECT r.xmlid, r.module, r.rel_path, r.line, r.tag FROM "
        "edge_record_instantiates e JOIN node_xml_record r ON r.record_id = e.src_id "
        "WHERE e.dst_id = ? ORDER BY r.module, r.rel_path, r.line LIMIT ?",
        (model,), limit,
    )


def model_counts(connection, model):
    """Fixed-size counts, so a summary never depends on a list being complete."""
    def scalar(sql, params):
        return int(connection.execute(sql, params).fetchone()[0])

    return {
        "definitions": scalar(
            "SELECT COUNT(*) FROM node_model_def WHERE model_name = ?", (model,)),
        "fields": scalar(
            "SELECT COUNT(*) FROM node_field WHERE model_name = ?", (model,)),
        "methods": scalar(
            "SELECT COUNT(*) FROM node_method WHERE model_name = ?", (model,)),
        "extended_by": scalar(
            "SELECT COUNT(*) FROM edge_model_inherits WHERE dst_id = ?", (model,)),
        "incoming_relations": scalar(
            "SELECT COUNT(*) FROM edge_field_relation WHERE dst_id = ?", (model,)),
        "xml_records": scalar(
            "SELECT COUNT(*) FROM edge_record_instantiates WHERE dst_id = ?", (model,)),
        "acl_rows": scalar(
            "SELECT COUNT(*) FROM edge_acl_on_model WHERE dst_id = ?", (model,)),
        "record_rules": scalar(
            "SELECT COUNT(*) FROM edge_rule_on_model WHERE dst_id = ?", (model,)),
    }


# --------------------------------------------------------------------------
# XML IDs
# --------------------------------------------------------------------------


def find_xmlids(connection, query, module, limit):
    sql = """
        SELECT xmlid, module, local_id, definition_count, winner_rule
        FROM node_xmlid
        WHERE (? = '' OR xmlid LIKE ? ESCAPE '\\')
          AND (? = '' OR module = ?)
        ORDER BY xmlid
        LIMIT ?
    """
    pattern = f"%{_escape_like(query)}%" if query else ""
    return _rows(
        connection, sql,
        (query or "", pattern, module or "", module or ""), limit,
    )


def get_xmlid(connection, xmlid):
    row = connection.execute(
        "SELECT xmlid, module, local_id, definition_count, winner_record_id, "
        "winner_rule FROM node_xmlid WHERE xmlid = ?", (xmlid,)
    ).fetchone()
    return dict(row) if row else None


def xmlid_names_like(connection, xmlid, limit=5):
    like = f"%{_escape_like(xmlid.rpartition('.')[2] or xmlid)}%"
    return [
        r["xmlid"] for r in connection.execute(
            "SELECT xmlid FROM node_xmlid WHERE xmlid LIKE ? ESCAPE '\\' "
            "ORDER BY xmlid LIMIT ?", (like, limit)
        )
    ]


def xmlid_definitions(connection, xmlid, limit):
    return _rows(
        connection,
        "SELECT r.record_id, r.module, r.rel_path, r.line, r.tag, r.record_model "
        "FROM edge_xmlid_defined_by e JOIN node_xml_record r ON r.record_id = e.dst_id "
        "WHERE e.src_id = ? ORDER BY r.module, r.rel_path, r.line LIMIT ?",
        (xmlid,), limit,
    )


def xmlid_references(connection, xmlid, limit):
    return _rows(
        connection,
        "SELECT src_kind, src_id, provenance, binding, src_file, src_line "
        "FROM edge_xmlid_references WHERE dst_unresolved = ? "
        "ORDER BY src_file, src_line LIMIT ?",
        (xmlid,), limit,
    )


def xmlid_reference_count(connection, xmlid) -> int:
    return int(connection.execute(
        "SELECT COUNT(*) FROM edge_xmlid_references WHERE dst_unresolved = ?",
        (xmlid,)
    ).fetchone()[0])


# --------------------------------------------------------------------------
# security
# --------------------------------------------------------------------------


def acls_for_model(connection, model, limit):
    return _rows(
        connection,
        "SELECT a.xmlid, a.csv_id, a.name, a.module, a.group_target, a.is_global, "
        "a.perm_read, a.perm_write, a.perm_create, a.perm_unlink, a.rel_path, "
        "a.line, e.binding AS model_binding "
        "FROM edge_acl_on_model e JOIN node_acl a ON a.acl_id = e.src_id "
        "WHERE e.dst_id = ? ORDER BY a.module, a.rel_path, a.line LIMIT ?",
        (model,), limit,
    )


def rules_for_model(connection, model, limit):
    return _rows(
        connection,
        "SELECT r.rule_id, r.xmlid, r.name, r.module, r.is_global, r.domain_force, "
        "r.active_declared, r.perm_read, r.perm_write, r.perm_create, r.perm_unlink, "
        "r.rel_path, r.line "
        "FROM edge_rule_on_model e JOIN node_rule r ON r.rule_id = e.src_id "
        "WHERE e.dst_id = ? ORDER BY r.module, r.rel_path, r.line LIMIT ?",
        (model,), limit,
    )


def groups_of_rule(connection, rule_id, limit):
    return _rows(
        connection,
        "SELECT dst_unresolved AS group_xmlid, binding FROM edge_rule_for_group "
        "WHERE src_id = ? ORDER BY dst_unresolved LIMIT ?",
        (rule_id,), limit,
    )


def groups_with_privilege(connection, xmlids, limit):
    """The named groups, each with the privilege it belongs to.

    A privilege is organisational metadata -- Odoo shows a group as
    "Privilege / Group" and makes the name unique within it. It grants nothing,
    and this returns it as identity, never as a right.
    """
    wanted = sorted({x for x in xmlids if x})[:limit]
    if not wanted:
        return [], False
    placeholders = ", ".join("?" for _ in wanted)
    rows = [
        dict(r) for r in connection.execute(
            "SELECT g.group_xmlid, g.name, g.rel_path, g.line, "
            "       p.privilege_xmlid, p.name AS privilege_name, p.sequence "
            "FROM node_group g "
            "LEFT JOIN edge_group_in_privilege e ON e.src_id = g.group_xmlid "
            "LEFT JOIN node_privilege p ON p.privilege_xmlid = e.dst_id "
            f"WHERE g.group_xmlid IN ({placeholders}) ORDER BY g.group_xmlid",
            tuple(wanted),
        )
    ]
    return rows, len({x for x in xmlids if x}) > len(wanted)


def get_privilege(connection, privilege_xmlid):
    row = connection.execute(
        "SELECT privilege_xmlid, module, local_id, name, sequence, description, "
        "placeholder, category_xmlid, rel_path, line FROM node_privilege "
        "WHERE privilege_xmlid = ?", (privilege_xmlid,)
    ).fetchone()
    return dict(row) if row else None


def get_group(connection, group_xmlid):
    row = connection.execute(
        "SELECT group_xmlid, module, local_id, name, privilege_xmlid, rel_path, line "
        "FROM node_group WHERE group_xmlid = ?", (group_xmlid,)
    ).fetchone()
    return dict(row) if row else None


def group_implies(connection, group_xmlid, limit):
    return _rows(
        connection,
        "SELECT dst_unresolved AS implied, binding, src_file, src_line "
        "FROM edge_group_implies WHERE src_id = ? ORDER BY dst_unresolved LIMIT ?",
        (group_xmlid,), limit,
    )


def groups_implying(connection, group_xmlid, limit):
    return _rows(
        connection,
        "SELECT src_id AS group_xmlid, src_file, src_line FROM edge_group_implies "
        "WHERE dst_id = ? ORDER BY src_id LIMIT ?",
        (group_xmlid,), limit,
    )


def security_counts(connection, model):
    def scalar(sql, params):
        return int(connection.execute(sql, params).fetchone()[0])

    return {
        "acl_rows": scalar(
            "SELECT COUNT(*) FROM edge_acl_on_model WHERE dst_id = ?", (model,)),
        "acl_rows_global": scalar(
            "SELECT COUNT(*) FROM edge_acl_on_model e JOIN node_acl a "
            "ON a.acl_id = e.src_id WHERE e.dst_id = ? AND a.is_global = 1", (model,)),
        "record_rules": scalar(
            "SELECT COUNT(*) FROM edge_rule_on_model WHERE dst_id = ?", (model,)),
        "record_rules_global": scalar(
            "SELECT COUNT(*) FROM edge_rule_on_model e JOIN node_rule r "
            "ON r.rule_id = e.src_id WHERE e.dst_id = ? AND r.is_global = 1", (model,)),
    }
