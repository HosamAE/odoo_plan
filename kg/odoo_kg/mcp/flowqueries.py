"""Read-only queries over the workflow layer, and its bounded traversal.

The rules every query module here follows: statements are literals, caller
input reaches the database only through ``?`` placeholders, identifiers are
interpolated only after passing a guard, and every list is bounded and says
when it was cut.

One rule specific to this layer. A workflow is a *reading of the code*, not a
record of what a system did. A transition means "this method contains a line
that would set this state", never "this ran" and never "this is the next step".
The traversal below walks stored edges only, so a chain it returns is a chain
somebody wrote down -- and a chain it does not return may still exist in a
runtime the graph cannot see.
"""

from __future__ import annotations

MAX_ROWS = 100
MAX_DEPTH = 5

EVIDENCE_DECLARED = "declared"
EVIDENCE_STATIC = "static"
EVIDENCE_HEURISTIC = "heuristic"

#: What each evidence kind means, returned with every answer so a reader never
#: has to guess how much weight a row will bear.
EVIDENCE_MEANING = {
    EVIDENCE_DECLARED: (
        "the source declares it outright -- a Selection value, an XML button, "
        "an ir.cron record"
    ),
    EVIDENCE_STATIC: (
        "an AST verified it -- an assignment or a write with a literal payload. "
        "The code can do this; it does not say that it did"
    ),
    EVIDENCE_HEURISTIC: (
        "inferred rather than read -- a source state taken from a nearby guard, "
        "or a target no Selection declares. A candidate, never a confirmed "
        "business process"
    ),
}

#: The kinds this layer can be addressed by.
KIND_TABLES = {
    "workflow": ("node_workflow", "workflow_id"),
    "workflow_state": ("node_workflow_state", "state_id"),
    "transition": ("node_transition", "transition_id"),
    "trigger": ("node_trigger", "trigger_id"),
    "cron": ("node_scheduled_action", "cron_id"),
}
KIND_NAMES = tuple(KIND_TABLES)

#: Which edge tables leave and arrive at each kind, from the (src_kind,
#: dst_kind) pairs the indexer actually writes. Routing per kind keeps the walk
#: cheap and stops an id matching a table where it does not belong.
FORWARD_TABLES = {
    "workflow": ("workflow_on_model", "workflow_has_state"),
    "workflow_state": (),
    "transition": ("transition_in_workflow", "transition_from_state",
                   "transition_to_state"),
    "trigger": ("trigger_invokes_method",),
    "cron": ("cron_runs_method", "cron_on_model"),
    "method": ("method_performs_transition", "transition_touches_model"),
    "route": ("route_invokes_method",),
    "model": (),
}
REVERSE_TABLES = {
    "workflow": ("transition_in_workflow",),
    "workflow_state": ("workflow_has_state", "transition_from_state",
                       "transition_to_state"),
    "transition": ("method_performs_transition",),
    "trigger": (),
    "cron": (),
    "method": ("trigger_invokes_method", "cron_runs_method",
               "route_invokes_method"),
    "model": ("workflow_on_model", "cron_on_model", "transition_touches_model"),
}

EDGE_RELATIONS = {
    "workflow_on_model": "applies_to_model",
    "workflow_has_state": "has_state",
    "transition_in_workflow": "belongs_to_workflow",
    "transition_from_state": "leaves_state",
    "transition_to_state": "enters_state",
    "method_performs_transition": "performs_transition",
    "trigger_invokes_method": "invokes_method",
    "cron_runs_method": "runs_method",
    "cron_on_model": "scheduled_on_model",
    "transition_touches_model": "touches_model",
    "route_invokes_method": "route_invokes_method",
}

_EDGE_TABLES = frozenset(EDGE_RELATIONS)

#: Where a hop's evidence comes from, by the table it crossed.
EDGE_EVIDENCE = {
    "workflow_on_model": EVIDENCE_DECLARED,
    "workflow_has_state": EVIDENCE_DECLARED,
    "transition_in_workflow": EVIDENCE_STATIC,
    "transition_from_state": EVIDENCE_HEURISTIC,
    "transition_to_state": EVIDENCE_STATIC,
    "method_performs_transition": EVIDENCE_STATIC,
    "trigger_invokes_method": EVIDENCE_DECLARED,
    "cron_runs_method": EVIDENCE_DECLARED,
    "cron_on_model": EVIDENCE_DECLARED,
    "transition_touches_model": EVIDENCE_STATIC,
    "route_invokes_method": EVIDENCE_STATIC,
}


def check_kind(kind: str) -> tuple[str, str]:
    """Refuse any kind that is not one of ours.

    The guard sits beside the sink because this module interpolates a kind's
    table and id column into SQL. Every caller currently passes a schema-
    validated enum; that is not a reason to trust the argument here.
    """
    if kind not in KIND_TABLES:
        raise ValueError(f"refusing to query unknown workflow kind {kind!r}")
    return KIND_TABLES[kind]


def check_edge_table(table: str) -> str:
    if table not in _EDGE_TABLES:
        raise ValueError(f"refusing to query unknown edge table {table!r}")
    return table


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _rows(connection, sql, params, limit):
    fetched = [dict(r) for r in connection.execute(sql, (*params, limit + 1))]
    return fetched[:limit], len(fetched) > limit


# --------------------------------------------------------------------------
# workflows
# --------------------------------------------------------------------------


def find_workflows(connection, query, model, state_source, limit):
    sql = """
        SELECT workflow_id, model_name, field_name, field_type, module,
               state_source, state_count, transition_count, has_dynamic_states,
               evidence, rel_path, line
        FROM node_workflow
        WHERE (? = '' OR model_name LIKE ? ESCAPE '\\'
               OR field_name LIKE ? ESCAPE '\\')
          AND (? = '' OR model_name = ?)
          AND (? = '' OR state_source = ?)
        ORDER BY model_name, field_name
        LIMIT ?
    """
    pattern = f"%{_escape_like(query)}%" if query else ""
    return _rows(
        connection, sql,
        (query or "", pattern, pattern, model or "", model or "",
         state_source or "", state_source or ""),
        limit,
    )


def count_workflows(connection, query, model, state_source) -> int:
    pattern = f"%{_escape_like(query)}%" if query else ""
    return int(connection.execute(
        "SELECT COUNT(*) FROM node_workflow "
        "WHERE (? = '' OR model_name LIKE ? ESCAPE '\\' "
        "       OR field_name LIKE ? ESCAPE '\\') "
        "  AND (? = '' OR model_name = ?) "
        "  AND (? = '' OR state_source = ?)",
        (query or "", pattern, pattern, model or "", model or "",
         state_source or "", state_source or ""),
    ).fetchone()[0])


def get_workflow(connection, model, field_name):
    """Every workflow matching a model and, when given, a field."""
    return [
        dict(r) for r in connection.execute(
            "SELECT workflow_id, model_name, field_name, field_type, module, "
            "state_source, state_count, transition_count, has_dynamic_states, "
            "evidence, rel_path, line FROM node_workflow "
            "WHERE (model_name = ? OR workflow_id = ?) "
            "AND (? = '' OR field_name = ?) "
            "ORDER BY field_name",
            (model, model, field_name or "", field_name or ""),
        )
    ]


def workflow_models_like(connection, model, limit=5):
    like = f"%{_escape_like(model)}%"
    return [
        r["model_name"] for r in connection.execute(
            "SELECT DISTINCT model_name FROM node_workflow "
            "WHERE model_name LIKE ? ESCAPE '\\' ORDER BY model_name LIMIT ?",
            (like, limit),
        )
    ]


def states_of(connection, workflow_id, limit):
    return _rows(
        connection,
        "SELECT state_id, value, label, position, evidence, rel_path, line "
        "FROM node_workflow_state WHERE workflow_id = ? "
        "ORDER BY position, value LIMIT ?",
        (workflow_id,), limit,
    )


def transitions_of(connection, workflow_id, limit):
    return _rows(
        connection,
        "SELECT transition_id, from_state, from_evidence, to_state, operation, "
        "receiver, method_name, method_id, conditional, condition_kind, "
        "condition_text, dynamic_expression, evidence, confidence, module, "
        "rel_path, line FROM node_transition WHERE workflow_id = ? "
        "ORDER BY IFNULL(to_state, ''), IFNULL(from_state, ''), rel_path, line, "
        "transition_id LIMIT ?",
        (workflow_id,), limit,
    )


def triggers_for_methods(connection, method_ids, limit):
    """The buttons, crons and routes that reach any of these methods."""
    if not method_ids:
        return [], False
    keys = sorted(set(method_ids))
    placeholders = ",".join("?" for _ in keys)
    sql = (
        "SELECT t.trigger_id AS id, t.kind AS kind, t.name AS name, "
        "t.module AS module, t.guard_text AS guard_text, "
        "t.guard_states AS guard_states, "
        "t.guard_state_evidence AS guard_state_evidence, "
        "t.evidence AS evidence, t.rel_path AS rel_path, t.line AS line, "
        "e.dst_id AS method_id "
        "FROM edge_trigger_invokes_method e "
        "JOIN node_trigger t ON t.trigger_id = e.src_id "
        f"WHERE e.dst_id IN ({placeholders}) "
        "UNION ALL "
        "SELECT c.cron_id, 'cron', IFNULL(c.xmlid, c.name), c.module, "
        "c.code_text, NULL, NULL, c.evidence, c.rel_path, c.line, e.dst_id "
        "FROM edge_cron_runs_method e "
        "JOIN node_scheduled_action c ON c.cron_id = e.src_id "
        f"WHERE e.dst_id IN ({placeholders}) "
        "UNION ALL "
        "SELECT r.route_id, 'route', r.path, r.module, NULL, NULL, NULL, "
        "'static', r.rel_path, r.line, e.dst_id "
        "FROM edge_route_invokes_method e "
        "JOIN node_route r ON r.route_id = e.src_id "
        f"WHERE e.dst_id IN ({placeholders}) "
        "ORDER BY kind, name, id LIMIT ?"
    )
    return _rows(connection, sql, (*keys, *keys, *keys), limit)


def counts(connection):
    def scalar(sql):
        return int(connection.execute(sql).fetchone()[0])

    return {
        "workflows": scalar("SELECT COUNT(*) FROM node_workflow"),
        "states": scalar("SELECT COUNT(*) FROM node_workflow_state"),
        "transitions": scalar("SELECT COUNT(*) FROM node_transition"),
        "triggers": scalar("SELECT COUNT(*) FROM node_trigger"),
        "scheduled_actions": scalar("SELECT COUNT(*) FROM node_scheduled_action"),
        "states_declared": scalar(
            "SELECT COUNT(*) FROM node_workflow_state WHERE evidence = 'declared'"),
        "states_seen_only_in_code": scalar(
            "SELECT COUNT(*) FROM node_workflow_state WHERE evidence = 'static'"),
        "transitions_conditional": scalar(
            "SELECT COUNT(*) FROM node_transition WHERE conditional = 1"),
        "transitions_without_target": scalar(
            "SELECT COUNT(*) FROM node_transition WHERE to_state IS NULL"),
        "transitions_with_source_state": scalar(
            "SELECT COUNT(*) FROM node_transition WHERE from_state IS NOT NULL"),
    }


# --------------------------------------------------------------------------
# addressing one object of any kind
# --------------------------------------------------------------------------


_KIND_SELECT = {
    "workflow": (
        "SELECT workflow_id AS id, model_name || '.' || field_name AS name, "
        "model_name, field_name, module, evidence, rel_path, line "
        "FROM node_workflow WHERE workflow_id = ? OR "
        "model_name || '.' || field_name = ?"
    ),
    "workflow_state": (
        "SELECT state_id AS id, model_name || '.' || field_name || ':' || value "
        "AS name, model_name, field_name, NULL AS module, evidence, rel_path, "
        "line FROM node_workflow_state WHERE state_id = ? OR "
        "model_name || '.' || field_name || ':' || value = ?"
    ),
    "transition": (
        "SELECT transition_id AS id, IFNULL(method_name, '?') || ' -> ' || "
        "IFNULL(to_state, '<dynamic>') AS name, model_name, field_name, module, "
        "evidence, rel_path, line FROM node_transition WHERE transition_id = ?  "
        "OR transition_id = ?"
    ),
    "trigger": (
        "SELECT trigger_id AS id, name, NULL AS model_name, NULL AS field_name, "
        "module, evidence, rel_path, line FROM node_trigger "
        "WHERE trigger_id = ? OR name = ?"
    ),
    "cron": (
        "SELECT cron_id AS id, IFNULL(xmlid, name) AS name, target_model AS "
        "model_name, NULL AS field_name, module, evidence, rel_path, line "
        "FROM node_scheduled_action WHERE cron_id = ? OR xmlid = ?"
    ),
}


def get_object(connection, kind, name):
    """Every object of one kind matching an id or a name. A list on purpose."""
    check_kind(kind)
    return [dict(r) for r in connection.execute(_KIND_SELECT[kind], (name, name))]


def names_like(connection, kind, name, limit=5):
    check_kind(kind)
    column = {
        "workflow": "model_name || '.' || field_name",
        "workflow_state": "model_name || '.' || field_name || ':' || value",
        "transition": "transition_id",
        "trigger": "name",
        "cron": "IFNULL(xmlid, name)",
    }[kind]
    table = KIND_TABLES[kind][0]
    like = f"%{_escape_like(name)}%"
    return [
        r[0] for r in connection.execute(
            f"SELECT DISTINCT {column} FROM {table} "
            f"WHERE {column} LIKE ? ESCAPE '\\' ORDER BY 1 LIMIT ?",
            (like, limit),
        )
    ]


def edges_from(connection, table, src_id, limit):
    return _rows(
        connection,
        "SELECT edge_id, src_kind, src_id, dst_kind, dst_id, dst_unresolved, "
        "binding, confidence, provenance, src_file, src_line "
        f"FROM edge_{check_edge_table(table)} WHERE src_id = ? "
        "ORDER BY dst_unresolved, src_line, edge_id LIMIT ?",
        (src_id,), limit,
    )


def edges_into(connection, table, dst_id, limit):
    return _rows(
        connection,
        "SELECT edge_id, src_kind, src_id, dst_kind, dst_id, dst_unresolved, "
        "binding, confidence, provenance, src_file, src_line "
        f"FROM edge_{check_edge_table(table)} WHERE dst_id = ? "
        "ORDER BY src_id, src_line, edge_id LIMIT ?",
        (dst_id,), limit,
    )


def describe(connection, kind, node_id):
    """Name and source for whatever a walk arrived at, across every layer."""
    blank = {"name": None, "model_name": None, "rel_path": None, "line": None,
             "evidence": None}
    if node_id is None:
        return blank
    if kind in KIND_TABLES:
        rows = get_object(connection, kind, node_id)
        if rows:
            row = rows[0]
            return {"name": row["name"], "model_name": row["model_name"],
                    "rel_path": row["rel_path"], "line": row["line"],
                    "evidence": row["evidence"]}
        return blank
    if kind == "method":
        row = connection.execute(
            "SELECT model_name, method_name, rel_path, line FROM node_method "
            "WHERE method_id = ?", (node_id,)
        ).fetchone()
        if row:
            return {"name": f"{row['model_name']}.{row['method_name']}",
                    "model_name": row["model_name"],
                    "rel_path": row["rel_path"], "line": row["line"],
                    "evidence": EVIDENCE_STATIC}
        return blank
    if kind == "model":
        row = connection.execute(
            "SELECT model_name FROM node_model WHERE model_name = ?", (node_id,)
        ).fetchone()
        if row:
            return {"name": row["model_name"], "model_name": row["model_name"],
                    "rel_path": None, "line": None,
                    "evidence": EVIDENCE_DECLARED}
        return blank
    if kind == "route":
        row = connection.execute(
            "SELECT path, module, rel_path, line FROM node_route "
            "WHERE route_id = ?", (node_id,)
        ).fetchone()
        if row:
            return {"name": row["path"], "model_name": None,
                    "rel_path": row["rel_path"], "line": row["line"],
                    "evidence": EVIDENCE_DECLARED}
    return blank
