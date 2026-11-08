"""Bounded traversal over the stored frontend edges.

What this is
------------
A breadth-first walk over edges that were **written down** at index time. It
follows nothing it has to infer: no method body is read, no expression is
evaluated, no path is guessed. If an edge is not in the database, this walk
does not know about it -- which is a statement about the walk, never about the
system it describes.

The four invariants
-------------------
* **Bounded.** At most ``MAX_DEPTH`` levels and ``MAX_ROWS`` nodes, clamped
  here as well as at the tool boundary, because a bound that holds only when
  the caller remembers it is not a bound. A walk that hits either limit stops
  and says which one; it never returns a partial answer that reads whole.
* **Cycle-safe.** Every node is expanded once. ``a`` importing ``b`` importing
  ``a`` terminates, and the repeat is counted rather than silently dropped, so
  the shape of the graph stays visible in the answer.
* **Deterministic.** The frontier is sorted before expansion and each level is
  sorted before it is returned, so two identical calls agree byte for byte
  regardless of the order SQLite happened to hand back rows.
* **Honest where knowledge ends.** An edge whose target was never bound to a
  node -- an import of ``@odoo/owl``, a service Odoo itself provides -- is
  reported as a leaf naming what was sought, and the walk stops. That is the
  edge of this graph, not a dead end in the system, and the answer says so.

Direction
---------
``affects`` walks edges backwards: *what would be touched if this changed*.
``depends_on`` walks them forwards: *what this reaches*. ``both`` does each and
labels every hop, because a mixed answer that did not say which way it went
would be unreadable.
"""

from __future__ import annotations

from . import m4queries

MAX_DEPTH = m4queries.MAX_DEPTH
MAX_ROWS = m4queries.MAX_ROWS

DIRECTIONS = ("affects", "depends_on", "both")

DIRECTION_MEANING = {
    "affects": (
        "edges walked backwards: what names this object, and would therefore "
        "be touched if it changed"
    ),
    "depends_on": (
        "edges walked forwards: what this object names, and therefore reaches"
    ),
    "both": (
        "both directions walked; every hop names its own in via.direction"
    ),
}

#: Which edge tables can leave a node of each kind, and which can arrive at
#: one. Derived from the (src_kind, dst_kind) pairs the indexer actually
#: writes. Routing per kind rather than probing all ten tables keeps the walk
#: cheap, and stops an id that happens to look like another kind's id from
#: matching a table where it does not belong.
FORWARD_TABLES = {
    "asset_bundle": (),
    "asset_entry": ("asset_in_bundle", "asset_includes_file"),
    "js_file": ("js_imports", "js_registers", "js_patches", "js_uses_service",
                "js_calls_route", "js_references_model"),
    "owl_component": ("component_uses_template",),
    "owl_template": ("template_inherits",),
    "route": (),
    "model": (),
    "file": (),
}

REVERSE_TABLES = {
    "asset_bundle": ("asset_in_bundle", "asset_includes_file"),
    "asset_entry": (),
    "js_file": ("js_imports", "asset_includes_file"),
    "owl_component": (),
    "owl_template": ("component_uses_template", "template_inherits"),
    "route": ("js_calls_route",),
    "model": ("js_references_model",),
    "file": ("asset_includes_file",),
}

#: The one relation that is not an edge table: a stored, explicit column.
#: Reported with its source named, so nobody mistakes it for an edge the
#: indexer wrote. Modelled as pointing from the component to its file --
#: change the file, and the component defined in it is affected.
CONTAINMENT_RELATION = "defined_in_file"
CONTAINMENT_SOURCE = "node_owl_component.js_id"


def _node_key(kind, node_id, sought):
    """Identity for the visited set.

    An unbound target has no id, so it is keyed on what the edge was looking
    for: two edges seeking the same missing thing are the same leaf.
    """
    return (kind, node_id if node_id is not None else f"unresolved:{sought}")


def _resolve(connection, kind, node_id, fallback):
    """Name and source for a node the walk arrived at. One query, not two."""
    blank = {"name": fallback, "module": None, "rel_path": None, "line": None}
    if node_id is None:
        return blank
    if kind in m4queries.KINDS:
        rows = m4queries.get_in_kind(connection, kind, node_id)
        if rows:
            row = rows[0]
            return {
                "name": row.get("name") or fallback,
                "module": row.get("module"),
                "rel_path": row.get("rel_path"),
                "line": row.get("line"),
            }
        return blank
    if kind == "route":
        row = connection.execute(
            "SELECT path, module, rel_path, line FROM node_route "
            "WHERE route_id = ?", (node_id,)
        ).fetchone()
        if row:
            return {"name": row["path"] or fallback, "module": row["module"],
                    "rel_path": row["rel_path"], "line": row["line"]}
        return blank
    if kind == "model":
        row = connection.execute(
            "SELECT model_name FROM node_model WHERE model_name = ?", (node_id,)
        ).fetchone()
        if row:
            return {"name": row["model_name"], "module": None,
                    "rel_path": None, "line": None}
        return blank
    if kind == "file":
        row = connection.execute(
            "SELECT module, rel_path FROM node_file WHERE file_key = ?", (node_id,)
        ).fetchone()
        if row:
            return {"name": row["rel_path"], "module": row["module"],
                    "rel_path": row["rel_path"], "line": None}
        return blank
    return blank


def evidence_for(step) -> str:
    """How the relation this hop followed was established."""
    if step.get("stored_column"):
        return m4queries.EVIDENCE_JS_LEXER
    table = step.get("edge_table") or ""
    if table.startswith("edge_js_") or table == "edge_component_uses_template":
        return m4queries.EVIDENCE_JS_LEXER
    if table in ("edge_asset_in_bundle", "edge_asset_includes_file"):
        return m4queries.EVIDENCE_MANIFEST
    if table == "edge_template_inherits":
        return m4queries.EVIDENCE_XML
    return m4queries.EVIDENCE_FILE_SCAN


def _step_from_edge(row, relation, table, direction):
    if direction == "depends_on":
        kind, node_id, sought = row["dst_kind"], row["dst_id"], row["dst_unresolved"]
    else:
        kind, node_id, sought = row["src_kind"], row["src_id"], row["src_id"]
    return {
        "kind": kind, "id": node_id, "sought": sought,
        "relation": relation, "edge_table": f"edge_{table}",
        "edge_id": row["edge_id"], "direction": direction,
        "binding": row["binding"], "confidence": row["confidence"],
        "provenance": row["provenance"],
        "src_file": row["src_file"], "src_line": row["src_line"],
    }


def forward_steps(connection, kind, node_id, cap):
    """Everything this node points at. Returns (steps, hit_cap)."""
    steps = []
    capped = False
    for table in sorted(FORWARD_TABLES.get(kind, ())):
        rows, more = m4queries.edges_from(connection, table, node_id, cap)
        capped = capped or more
        relation = m4queries.EDGE_RELATIONS[table]
        steps.extend(_step_from_edge(r, relation, table, "depends_on") for r in rows)

    if kind == "owl_component":
        owner = m4queries.file_of_component(connection, node_id)
        if owner is not None:
            steps.append({
                "kind": "js_file", "id": owner["js_id"],
                "sought": owner["rel_path"], "relation": CONTAINMENT_RELATION,
                "edge_table": None, "edge_id": None,
                "stored_column": CONTAINMENT_SOURCE, "direction": "depends_on",
                "binding": "resolved", "confidence": "heuristic",
                "provenance": "js.class.declaration",
                "src_file": owner["rel_path"], "src_line": None,
            })
    return steps, capped


def reverse_steps(connection, kind, node_id, cap):
    """Everything that points at this node. Returns (steps, hit_cap)."""
    steps = []
    capped = False
    for table in sorted(REVERSE_TABLES.get(kind, ())):
        rows, more = m4queries.edges_into(connection, table, node_id, cap)
        capped = capped or more
        relation = m4queries.EDGE_RELATIONS[table]
        steps.extend(_step_from_edge(r, relation, table, "affects") for r in rows)

    if kind == "js_file":
        rows, more = m4queries.components_in_file(connection, node_id, cap)
        capped = capped or more
        for row in rows:
            steps.append({
                "kind": "owl_component", "id": row["component_id"],
                "sought": row["class_name"], "relation": CONTAINMENT_RELATION,
                "edge_table": None, "edge_id": None,
                "stored_column": CONTAINMENT_SOURCE, "direction": "affects",
                "binding": "resolved", "confidence": "heuristic",
                "provenance": "js.class.declaration",
                "src_file": row["rel_path"], "src_line": row["line"],
            })
    return steps, capped


def edge_backed_steps(connection, kind, edge_id, direction):
    """The neighbour of a registry entry, a service use, or a patch.

    These have no node: they exist only as an edge out of a JavaScript file.
    Their one neighbour is that file, and it lies in the ``affects`` direction
    -- the file declares them, so changing the file changes them.
    """
    # Checked here as well as at the tool boundary: this function interpolates
    # spec.table into SQL, so it validates its own argument rather than
    # trusting that every caller already did.
    spec = m4queries.check_kind(kind)
    if not spec.is_edge_backed:
        raise ValueError(f"{kind!r} has nodes of its own; use the edge tables")
    row = connection.execute(
        f"SELECT e.edge_id, e.src_id, e.dst_unresolved, e.binding, e.confidence, "
        f"e.provenance, e.src_file, e.src_line, j.rel_path "
        f"FROM {spec.table} e LEFT JOIN node_js_file j ON j.js_id = e.src_id "
        f"WHERE e.edge_id = ?", (edge_id,)
    ).fetchone()
    if row is None or row["src_id"] is None or direction == "depends_on":
        return []
    return [{
        "kind": "js_file", "id": row["src_id"], "sought": row["rel_path"],
        "relation": m4queries.EDGE_RELATIONS[spec.edge_table],
        "edge_table": f"edge_{spec.edge_table}", "edge_id": row["edge_id"],
        "direction": "affects", "binding": row["binding"],
        "confidence": row["confidence"], "provenance": row["provenance"],
        "src_file": row["src_file"], "src_line": row["src_line"],
    }]


def traverse(connection, start_kind, start_id, start_name, *,
             direction, depth, limit):
    """Walk out from one object and return the levels, deepest last."""
    depth = max(1, min(int(depth), MAX_DEPTH))
    limit = max(1, min(int(limit), MAX_ROWS))
    if direction not in DIRECTIONS:
        raise ValueError(f"refusing to walk in unknown direction {direction!r}")

    visited = {_node_key(start_kind, start_id, start_name)}
    frontier = [(start_kind, start_id, start_name)]
    levels = []
    reached = 0
    truncated_by = None
    edge_cap_hit = False
    repeats = 0

    for current_depth in range(1, depth + 1):
        if not frontier or truncated_by:
            break
        found = []
        # Sorted before expansion: which nodes survive the limit depends on
        # the order they are visited in, so that order must not come from
        # whatever sequence SQLite felt like returning.
        for kind, node_id, name in sorted(
            frontier,
            key=lambda item: (item[0], str(item[2] or ""), str(item[1] or "")),
        ):
            if node_id is None:
                continue        # a leaf: this graph has nothing to expand
            steps = []
            if kind in m4queries.KINDS and m4queries.KINDS[kind].is_edge_backed:
                steps.extend(
                    edge_backed_steps(connection, kind, node_id, direction)
                )
            else:
                if direction in ("depends_on", "both"):
                    forward, capped = forward_steps(connection, kind, node_id, limit)
                    edge_cap_hit = edge_cap_hit or capped
                    steps.extend(forward)
                if direction in ("affects", "both"):
                    backward, capped = reverse_steps(connection, kind, node_id, limit)
                    edge_cap_hit = edge_cap_hit or capped
                    steps.extend(backward)

            for step in steps:
                key = _node_key(step["kind"], step["id"], step["sought"])
                if key in visited:
                    repeats += 1
                    continue
                visited.add(key)
                resolved = _resolve(
                    connection, step["kind"], step["id"], step["sought"]
                )
                via = {
                    "relation": step["relation"],
                    "direction": step["direction"],
                    "edge_table": step["edge_table"],
                    "edge_id": step["edge_id"],
                    "binding": step["binding"],
                    "confidence": step["confidence"],
                    "evidence": evidence_for(step),
                    "provenance": step["provenance"],
                    "src_file": step["src_file"],
                    "src_line": step["src_line"],
                }
                if "stored_column" in step:
                    via["stored_column"] = step["stored_column"]
                row = {
                    "kind": step["kind"],
                    "id": step["id"],
                    "name": resolved["name"],
                    "module": resolved["module"],
                    "rel_path": resolved["rel_path"],
                    "line": resolved["line"],
                    # False means the walk stopped here because this graph
                    # holds no node for it -- never that nothing lies beyond.
                    "traversable": step["id"] is not None,
                    "from": {"kind": kind, "id": node_id, "name": name},
                    "via": via,
                }
                if step["id"] is None:
                    row["sought"] = step["sought"]
                found.append(row)

        found.sort(key=lambda item: (
            item["kind"], str(item["name"] or ""), str(item["id"] or "")
        ))
        if reached + len(found) > limit:
            found = found[: max(0, limit - reached)]
            truncated_by = "limit"
        reached += len(found)
        if found:
            levels.append(
                {"depth": current_depth, "count": len(found), "nodes": found}
            )
        frontier = [
            (row["kind"], row["id"], row["name"]) for row in found
            if row["traversable"]
        ]

    if truncated_by is None and frontier:
        # Stopped at the depth ceiling with expandable nodes still in hand.
        # This says the walk stopped looking, not that more certainly exists.
        truncated_by = "depth"

    return {
        "levels": levels,
        "reached": reached,
        "depth_walked": len(levels),
        "truncated_by": truncated_by,
        "edge_cap_hit": edge_cap_hit,
        "cycles_or_repeats_skipped": repeats,
    }
