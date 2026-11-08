"""The M4 tools: the frontend, and the routes that serve it.

Same contract as every tool here -- read-only, no file path, no SQL, every
list bounded, every truncation announced -- with two additions specific to
this layer.

**JavaScript facts are heuristic and say so.** :mod:`odoo_kg.jsread` is a
lexer, not a parser. It cannot see scope, does not know a grammar, and never
runs a line of the code it reads. Every row derived from it carries
``confidence: heuristic`` and an ``evidence`` kind naming how it was found, so
a reader never has to guess how much weight an answer will bear.

**A route's ``auth`` is reported as declared, never as effective.** Where the
source writes no ``auth=``, the answer is null, not Odoo's runtime default.
And no route is ever called safe or unsafe: what a route permits depends on
its method body, which this milestone does not read, and on the ACLs and
record rules the security layer holds separately.
"""

from __future__ import annotations

from . import m4impact, m4queries
from .limits import clamp

DEFAULT_ROWS = 25
DEFAULT_DEPTH = 2
MAX_ROWS = m4queries.MAX_ROWS
MAX_DEPTH = m4queries.MAX_DEPTH

#: On every response built from JavaScript.
JS_CONFIDENCE_NOTE = (
    "JavaScript here is read with a lexer, never parsed and never executed. "
    "Imports, registrations, patches, service uses and components are "
    "recognised by shape, so they are heuristic: reliable in practice, but not "
    "facts read out of a grammar. Anything absent may simply be written in a "
    "shape this build does not recognise."
)

#: On every response that mentions a route's auth.
ROUTE_AUTH_NOTE = (
    "auth and type are reported exactly as the decorator declares them. A null "
    "means the source writes none and Odoo's runtime default applies -- this "
    "graph does not assume it. Nothing here says whether a route is safe: that "
    "depends on the method body, which is not indexed, and on the access rules "
    "the security layer holds separately."
)

#: On every response that reports an absence.
ABSENCE_NOTE = (
    "An empty result means this graph holds no such stored edge. It is not "
    "evidence that no connection exists: a link made at runtime, built by "
    "string concatenation, or written in a shape this build does not "
    "recognise leaves no edge behind."
)

EVIDENCE_MEANING = {
    m4queries.EVIDENCE_MANIFEST: "read from a literal in a __manifest__.py",
    m4queries.EVIDENCE_FILE_SCAN: "the file exists in the scanned tree",
    m4queries.EVIDENCE_XML: "read from an XML attribute",
    m4queries.EVIDENCE_JS_LEXER: "recognised by shape in a JavaScript token stream",
    m4queries.EVIDENCE_PYTHON_AST: "read from a Python syntax tree",
}


def _rows_arg(args):
    return clamp(args.get("limit", DEFAULT_ROWS), 1, MAX_ROWS)


def _truncation(name, truncated, shown, total=None):
    if not truncated:
        return None
    return {
        "list": name, "shown": shown, "complete": False,
        "note": (
            f"more rows exist than the {shown} shown; raise limit (max "
            f"{MAX_ROWS}) or narrow the query. Any counts field is complete "
            "regardless."
        ),
        **({"total": total} if total is not None else {}),
    }


def _resolve_one(context, kind, name, tool_hint):
    """The single object of ``kind`` called ``name``, or an actionable error.

    Ambiguity is refused rather than resolved. Two components can share a class
    name; picking whichever sorted first would answer a question the caller did
    not ask, and would do it silently.
    """
    from .tools import ToolError

    rows = m4queries.get_in_kind(context.connection, kind, name)
    if not rows:
        suggestions = m4queries.names_like(context.connection, kind, name)
        hint = (
            f" Did you mean: {', '.join(str(s) for s in suggestions)}?"
            if suggestions else f" {tool_hint}"
        )
        raise ToolError(
            f"no {kind} called {name!r} in this graph.{hint}"
        )
    if len(rows) > 1:
        where = "; ".join(
            f"{r.get('rel_path') or '?'}:{r.get('line') or '?'} (id {r['id']})"
            for r in rows[:5]
        )
        raise ToolError(
            f"{len(rows)} objects of kind {kind!r} are called {name!r}: {where}"
            + (" ..." if len(rows) > 5 else "")
            + ". Call again with the stable id of the one you mean, so the "
            "answer is about a single object rather than an arbitrary one."
        )
    return rows[0]


# --------------------------------------------------------------------------
# kg_find_frontend
# --------------------------------------------------------------------------


def kg_find_frontend(context, args):
    """Bounded search across every frontend kind at once."""
    limit, clamped = _rows_arg(args)
    query = (args.get("query") or "").strip()
    module = (args.get("module") or "").strip()
    kinds = args.get("kind")
    kinds = (kinds,) if isinstance(kinds, str) else m4queries.KIND_NAMES

    results = []
    truncated_kinds = []
    counts = {}
    for kind in kinds:
        rows, truncated = m4queries.find_in_kind(
            context.connection, kind, query, module, limit
        )
        counts[kind] = m4queries.count_in_kind(
            context.connection, kind, query, module
        )
        if truncated:
            truncated_kinds.append(kind)
        for row in rows:
            results.append({
                "kind": row["kind"],
                "id": row["id"],
                "name": row["name"],
                "module": row.get("module"),
                "rel_path": row.get("rel_path"),
                "line": row.get("line"),
                "confidence": row["confidence"],
                "evidence": row["evidence"],
            })

    # One deterministic order across every kind: kind first (in the registry's
    # own order), then name, then id. Two identical calls must agree.
    order = {name: index for index, name in enumerate(m4queries.KIND_NAMES)}
    results.sort(key=lambda row: (
        order[row["kind"]], str(row["name"] or ""), str(row["id"] or "")
    ))
    total = sum(counts[kind] for kind in kinds)
    cut = len(results) > limit
    results = results[:limit]

    data = {
        "query": query or None,
        "kind": args.get("kind") or None,
        "module": module or None,
        "max_limit": MAX_ROWS,
        "limit_clamped": clamped,
        "kinds_searched": list(kinds),
        "count": len(results),
        "total_matching": total,
        "counts_by_kind": counts,
        "results": results,
        "truncated": _truncation(
            "results", cut or bool(truncated_kinds), len(results), total
        ),
        "evidence_meaning": EVIDENCE_MEANING,
        "confidence_note": JS_CONFIDENCE_NOTE,
        "absence_note": ABSENCE_NOTE,
        "scope_note": (
            "Only what this project declares. A bundle it never adds to, a "
            "core Odoo component, and a template owned by another addon have "
            "no node here even though Odoo has them."
        ),
    }
    lines = [
        f"{len(results)} of {total} frontend object(s)"
        + (" (truncated)" if data["truncated"] else "")
    ] + [
        f"  [{r['kind']}] {r['name']}"
        + (f" — {r['module']}" if r["module"] else "")
        + (f" at {r['rel_path']}:{r['line']}" if r["rel_path"] and r["line"]
           else f" at {r['rel_path']}" if r["rel_path"] else "")
        + f" ({r['confidence']}/{r['evidence']})"
        for r in results
    ]
    return "\n".join(lines) or "no frontend object matched", data


# --------------------------------------------------------------------------
# kg_get_frontend
# --------------------------------------------------------------------------


def kg_get_frontend(context, args):
    """One frontend object, its direct relations, sources and limits."""
    kind = args["kind"]
    name = args["name"].strip().replace("\\", "/")
    limit, clamped = _rows_arg(args)
    row = _resolve_one(
        context, kind, name,
        "Use kg_find_frontend to see what this graph contains.",
    )

    connection = context.connection
    node_id = row["id"]
    relations = {"outgoing": [], "incoming": []}
    truncations = []

    spec = m4queries.KINDS[kind]
    if spec.is_edge_backed:
        # No node of its own: it exists as an edge out of one JavaScript file.
        steps = m4impact.edge_backed_steps(connection, kind, node_id, "affects")
        relations["incoming"] = [{
            "kind": step["kind"], "id": step["id"], "name": step["sought"],
            "relation": step["relation"], "binding": step["binding"],
            "confidence": step["confidence"], "provenance": step["provenance"],
            "src_file": step["src_file"], "src_line": step["src_line"],
        } for step in steps]
    else:
        for label, fetch in (
            ("outgoing", m4impact.forward_steps),
            ("incoming", m4impact.reverse_steps),
        ):
            steps, capped = fetch(connection, kind, node_id, limit)
            relations[label] = [{
                "kind": step["kind"],
                "id": step["id"],
                "name": step["sought"],
                "relation": step["relation"],
                "edge_table": step["edge_table"],
                "binding": step["binding"],
                "confidence": step["confidence"],
                "evidence": m4impact.evidence_for(step),
                "provenance": step["provenance"],
                "src_file": step["src_file"],
                "src_line": step["src_line"],
                **({"stored_column": step["stored_column"]}
                   if "stored_column" in step else {}),
            } for step in steps]
            relations[label].sort(key=lambda item: (
                item["relation"], str(item["name"] or ""), str(item["id"] or "")
            ))
            item = _truncation(label, capped, len(relations[label]))
            if item:
                truncations.append(item)

    obj = {key: value for key, value in row.items() if key not in ("id", "name")}
    obj.update({"kind": kind, "id": node_id, "name": row["name"]})

    limitations = [
        "Method bodies and the call graph are not indexed, so this cannot say "
        "what any of this code does.",
        ABSENCE_NOTE,
    ]
    if row["evidence"] == m4queries.EVIDENCE_JS_LEXER:
        limitations.append(JS_CONFIDENCE_NOTE)
    if kind == "asset_entry":
        limitations.append(
            "Entries are given in the order their manifest declares them, "
            "which is the order Odoo applies them within one module. Across "
            "modules the order follows the dependency graph, which this tool "
            "does not resolve."
        )
    if kind == "owl_template":
        limitations.append(
            "Which definition of a template wins at load time depends on "
            "bundle order, which this graph does not model."
        )

    data = {
        "kind": kind,
        "requested": name,
        "max_limit": MAX_ROWS,
        "limit_clamped": clamped,
        "object": obj,
        "source": {
            "module": row.get("module"),
            "rel_path": row.get("rel_path"),
            "line": row.get("line"),
            "confidence": row["confidence"],
            "evidence": row["evidence"],
            "evidence_meaning": EVIDENCE_MEANING[row["evidence"]],
        },
        "relations": relations,
        "relation_counts": {
            "outgoing": len(relations["outgoing"]),
            "incoming": len(relations["incoming"]),
        },
        "truncated": truncations,
        "limitations": limitations,
    }
    lines = [
        f"**{row['name']}** — {kind}"
        + (f", {row['module']}" if row.get("module") else ""),
        f"{row['confidence']}/{row['evidence']}"
        + (f" at {row['rel_path']}:{row['line']}"
           if row.get("rel_path") and row.get("line")
           else f" at {row['rel_path']}" if row.get("rel_path") else ""),
        f"{len(relations['outgoing'])} outgoing, "
        f"{len(relations['incoming'])} incoming relation(s)",
    ]
    return "\n".join(lines), data


# --------------------------------------------------------------------------
# kg_frontend_impact
# --------------------------------------------------------------------------


def kg_frontend_impact(context, args):
    """Bounded, cycle-safe traversal over the stored frontend edges."""
    kind = args["kind"]
    name = args["name"].strip().replace("\\", "/")
    direction = args.get("direction", "affects")
    depth, depth_clamped = clamp(args.get("depth", DEFAULT_DEPTH), 1, MAX_DEPTH)
    limit, clamped = _rows_arg(args)

    row = _resolve_one(
        context, kind, name,
        "Use kg_find_frontend to see what this graph contains.",
    )
    walk = m4impact.traverse(
        context.connection, kind, row["id"], row["name"],
        direction=direction, depth=depth, limit=limit,
    )

    # Each node appears exactly once, in `direct` if it is one hop away and in
    # `transitive` otherwise. Carrying a combined `levels` list beside these
    # would trebled the payload -- a hundred nodes went from 70KB to 209KB --
    # for no information a reader does not already have here.
    direct = next(
        (level["nodes"] for level in walk["levels"] if level["depth"] == 1), []
    )
    transitive = [level for level in walk["levels"] if level["depth"] > 1]

    data = {
        "start": {
            "kind": kind, "id": row["id"], "name": row["name"],
            "module": row.get("module"), "rel_path": row.get("rel_path"),
            "line": row.get("line"),
        },
        "direction": direction,
        "direction_meaning": m4impact.DIRECTION_MEANING[direction],
        "depth_requested": depth,
        "depth_walked": walk["depth_walked"],
        "max_depth": MAX_DEPTH,
        "depth_clamped": depth_clamped,
        "max_limit": MAX_ROWS,
        "limit_clamped": clamped,
        "reached": walk["reached"],
        "direct": direct,
        "direct_count": len(direct),
        "transitive": transitive,
        "transitive_count": sum(level["count"] for level in transitive),
        "depths_returned": [level["depth"] for level in walk["levels"]],
        "cycles_or_repeats_skipped": walk["cycles_or_repeats_skipped"],
        "truncated": (
            None if walk["truncated_by"] is None else {
                "by": walk["truncated_by"],
                "complete": False,
                # Names the ceiling that actually stopped this walk, and the
                # hard one behind it. Quoting only the hard maximum would tell
                # a caller who asked for depth 1 the wrong thing about why
                # their answer ended where it did.
                "stopped_at": depth if walk["truncated_by"] == "depth" else limit,
                "hard_maximum": (
                    MAX_DEPTH if walk["truncated_by"] == "depth" else MAX_ROWS
                ),
                "note": (
                    f"the walk stopped after {depth} hop(s) with expandable "
                    f"objects still in hand; raise depth (hard maximum "
                    f"{MAX_DEPTH})."
                    if walk["truncated_by"] == "depth" else
                    f"the walk stopped at {limit} objects; raise limit (hard "
                    f"maximum {MAX_ROWS}) or narrow the direction."
                ) + " What is shown is correct; it is not everything.",
            }
        ),
        "edge_cap_hit": walk["edge_cap_hit"],
        "evidence_meaning": EVIDENCE_MEANING,
        "traversal_note": (
            "Only stored, explicit edges are followed. One relation, "
            f"{m4impact.CONTAINMENT_RELATION!r}, comes from the stored column "
            f"{m4impact.CONTAINMENT_SOURCE} rather than an edge table, and "
            "every hop that used it says so in via.stored_column."
        ),
        "leaf_note": (
            "traversable=false means this graph holds no node for that target "
            "-- an import of an Odoo package, a service Odoo provides -- so "
            "the walk stopped there. That is the edge of this graph, not a "
            "dead end in the system."
        ),
        "absence_note": ABSENCE_NOTE,
        "not_a_verdict_note": (
            "This is reachability over declared edges, not a change-risk "
            "score and not a security judgement. Method bodies are not "
            "indexed, and nothing here says whether any of it is safe."
        ),
        "confidence_note": JS_CONFIDENCE_NOTE,
    }
    lines = [
        f"**{row['name']}** ({kind}) — {direction}, depth {walk['depth_walked']}"
        f"/{depth}, {walk['reached']} object(s)"
    ] + [
        f"  d{level['depth']}: " + ", ".join(
            f"[{n['kind']}] {n['name']}" + ("" if n["traversable"] else " (leaf)")
            for n in level["nodes"]
        )
        for level in walk["levels"]
    ]
    return "\n".join(lines), data


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------


def kg_find_routes(context, args):
    limit, clamped = _rows_arg(args)
    rows, truncated = m4queries.find_routes(
        context.connection,
        (args.get("query") or "").strip(),
        (args.get("module") or "").strip(),
        (args.get("auth") or "").strip(),
        limit,
    )
    data = {
        "query": args.get("query") or None,
        "module": args.get("module") or None,
        "auth": args.get("auth") or None,
        "max_limit": MAX_ROWS,
        "limit_clamped": clamped,
        "count": len(rows),
        "routes": rows,
        "counts": m4queries.route_counts(context.connection),
        "truncated": _truncation("routes", truncated, len(rows)),
        "auth_note": ROUTE_AUTH_NOTE,
        "evidence": m4queries.EVIDENCE_PYTHON_AST,
    }
    lines = [f"{len(rows)} route(s)" + (" (truncated)" if truncated else "")] + [
        f"  {r['path'] or '<no literal path>'} — {r['module']}."
        f"{r['controller']}.{r['method']}, auth="
        f"{r['auth'] if r['auth'] is not None else 'not declared'}"
        + (" [partly computed]" if r["has_dynamic_argument"] else "")
        for r in rows
    ]
    return "\n".join(lines) or "no routes matched", data


def kg_get_route(context, args):
    from .tools import ToolError

    path = args["route"].strip()
    declarations = m4queries.get_route_by_path(context.connection, path)
    if not declarations:
        suggestions = m4queries.route_paths_like(context.connection, path)
        hint = (
            f" Did you mean: {', '.join(suggestions)}?" if suggestions
            else " Use kg_find_routes to list what this graph contains."
        )
        raise ToolError(
            f"no route declares the path {path!r} in this graph.{hint} A path "
            "with a converter such as '/x/<int:id>' is stored exactly as "
            "written, so search for the literal rather than an example URL."
        )

    limit, clamped = _rows_arg(args)
    callers = []
    caller_truncated = False
    for declaration in declarations:
        rows, truncated = m4queries.js_calling_route(
            context.connection, declaration["route_id"], limit
        )
        callers.extend(rows)
        caller_truncated = caller_truncated or truncated

    data = {
        "path": path,
        "max_limit": MAX_ROWS,
        "limit_clamped": clamped,
        "declaration_count": len(declarations),
        "declarations": declarations,
        "called_from_javascript": callers,
        "truncated": [
            item for item in (
                _truncation("called_from_javascript", caller_truncated, len(callers)),
            ) if item
        ],
        "auth_note": ROUTE_AUTH_NOTE,
        "evidence": m4queries.EVIDENCE_PYTHON_AST,
        "javascript_note": (
            "A JavaScript file is listed here when it contains this exact path "
            "as a string literal, found with a lexer. A URL built by "
            "concatenation is not found, so an empty list is not evidence that "
            "nothing calls it."
        ),
        "absence_note": ABSENCE_NOTE,
    }
    if len(declarations) > 1:
        data["duplicate_note"] = (
            "More than one controller method declares this path. Which one "
            "wins depends on module load order, which this graph does not "
            "model."
        )
    lines = [f"**{path}** — {len(declarations)} declaration(s)"] + [
        f"  {d['module']}.{d['controller']}.{d['method']} at {d['rel_path']}:"
        f"{d['line']}, auth={d['auth'] if d['auth'] is not None else 'not declared'}"
        for d in declarations
    ]
    return "\n".join(lines), data
