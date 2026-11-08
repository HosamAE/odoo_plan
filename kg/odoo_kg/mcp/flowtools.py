"""The four M4.2 tools: workflows, states, transitions and process traversal.

Same contract as every tool here -- read-only, no file path, no SQL, every list
bounded, every truncation announced -- with one addition that matters more than
all the others.

**This layer describes code, not runs.** A transition is a line that would set
a state. The graph does not know whether it executed, in what order, or whether
the condition guarding it was true. So no answer here is ever phrased as a
sequence of events. ``kg_trace_process`` returns reachability over declared and
static edges and says so in the response; anyone reading it as "the process
goes A then B" has been told, in the payload, not to.

The three evidence kinds stay apart in every answer. ``declared`` is what the
source states outright, ``static`` is what an AST verified, ``heuristic`` is a
candidate inferred from a guard or a name. A heuristic transition is never
presented as a confirmed business process. ``documented`` is reserved for M4.3
and this build never writes it.
"""

from __future__ import annotations

from . import flowqueries
from .limits import clamp

DEFAULT_ROWS = 25
DEFAULT_DEPTH = 3
MAX_ROWS = flowqueries.MAX_ROWS
MAX_DEPTH = flowqueries.MAX_DEPTH

NOT_A_RUNTIME_NOTE = (
    "This describes code, not runs. A transition means a method contains a "
    "line that would set this state -- not that it executed, not that it is "
    "the next step, and not that its condition held. No ordering here is a "
    "runtime ordering."
)

ABSENCE_NOTE = (
    "An empty result means this graph holds no such stored edge. It is not "
    "evidence that no connection exists: a state set through a computed value, "
    "a method reached by inheritance, or a step taken by code this build does "
    "not read leaves no edge behind."
)

CONDITION_NOTE = (
    "A conditional transition sits inside a branch, or in a method that raises. "
    "The condition is given as written and is never evaluated, so a transition "
    "marked conditional is one whose guard this build did not fully interpret."
)


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


def _split_by_evidence(rows):
    """The same rows, grouped so declared, static and heuristic never blur."""
    buckets = {
        flowqueries.EVIDENCE_DECLARED: [],
        flowqueries.EVIDENCE_STATIC: [],
        flowqueries.EVIDENCE_HEURISTIC: [],
    }
    for row in rows:
        bucket = buckets.get(row.get("evidence"))
        if bucket is not None:
            bucket.append(row)
    return {name: len(rows) for name, rows in buckets.items()}


def _resolve_one(context, kind, name):
    """The single object of ``kind`` called ``name``, or an actionable error."""
    from .tools import ToolError

    rows = flowqueries.get_object(context.connection, kind, name)
    if not rows:
        suggestions = flowqueries.names_like(context.connection, kind, name)
        hint = (
            f" Did you mean: {', '.join(str(s) for s in suggestions)}?"
            if suggestions
            else " Use kg_find_workflows to see what this graph contains."
        )
        raise ToolError(f"no {kind} called {name!r} in this graph.{hint}")
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
# bounded traversal over the workflow edges
# --------------------------------------------------------------------------


def _steps(connection, kind, node_id, direction, cap):
    """One node's neighbours, as (step, hit_cap)."""
    steps = []
    capped = False
    if direction in ("forward", "both"):
        for table in sorted(flowqueries.FORWARD_TABLES.get(kind, ())):
            rows, more = flowqueries.edges_from(connection, table, node_id, cap)
            capped = capped or more
            for row in rows:
                steps.append({
                    "kind": row["dst_kind"], "id": row["dst_id"],
                    "sought": row["dst_unresolved"], "table": table,
                    "direction": "forward", "binding": row["binding"],
                    "confidence": row["confidence"],
                    "provenance": row["provenance"],
                    "src_file": row["src_file"], "src_line": row["src_line"],
                })
    if direction in ("backward", "both"):
        for table in sorted(flowqueries.REVERSE_TABLES.get(kind, ())):
            rows, more = flowqueries.edges_into(connection, table, node_id, cap)
            capped = capped or more
            for row in rows:
                steps.append({
                    "kind": row["src_kind"], "id": row["src_id"],
                    "sought": row["src_id"], "table": table,
                    "direction": "backward", "binding": row["binding"],
                    "confidence": row["confidence"],
                    "provenance": row["provenance"],
                    "src_file": row["src_file"], "src_line": row["src_line"],
                })
    return steps, capped


def traverse(connection, start_kind, start_id, start_name, *,
             direction, depth, limit):
    """Breadth-first over the stored workflow edges.

    Bounded, cycle-safe and deterministic, and re-clamped here as well as at
    the tool boundary because a bound that holds only when the caller remembers
    it is not a bound. A state cycle -- draft to confirm to draft -- is the
    normal shape of an Odoo workflow, so terminating on one is not an edge case
    here; it is the common case.
    """
    depth = max(1, min(int(depth), MAX_DEPTH))
    limit = max(1, min(int(limit), MAX_ROWS))
    if direction not in ("forward", "backward", "both"):
        raise ValueError(f"refusing to walk in unknown direction {direction!r}")

    def key(kind, node_id, sought):
        return (kind, node_id if node_id is not None else f"unresolved:{sought}")

    visited = {key(start_kind, start_id, start_name)}
    frontier = [(start_kind, start_id, start_name)]
    levels = []
    reached = 0
    truncated_by = None
    repeats = 0
    edge_cap_hit = False

    for current_depth in range(1, depth + 1):
        if not frontier or truncated_by:
            break
        found = []
        for kind, node_id, name in sorted(
            frontier,
            key=lambda item: (item[0], str(item[2] or ""), str(item[1] or "")),
        ):
            if node_id is None:
                continue
            steps, capped = _steps(connection, kind, node_id, direction, limit)
            edge_cap_hit = edge_cap_hit or capped
            for step in steps:
                identity = key(step["kind"], step["id"], step["sought"])
                if identity in visited:
                    repeats += 1
                    continue
                visited.add(identity)
                described = flowqueries.describe(
                    connection, step["kind"], step["id"]
                )
                row = {
                    "kind": step["kind"],
                    "id": step["id"],
                    "name": described["name"] or step["sought"],
                    "model_name": described["model_name"],
                    "rel_path": described["rel_path"],
                    "line": described["line"],
                    "traversable": step["id"] is not None,
                    "from": {"kind": kind, "id": node_id, "name": name},
                    "via": {
                        "relation": flowqueries.EDGE_RELATIONS[step["table"]],
                        "edge_table": f"edge_{step['table']}",
                        "direction": step["direction"],
                        "binding": step["binding"],
                        "confidence": step["confidence"],
                        "evidence": flowqueries.EDGE_EVIDENCE[step["table"]],
                        "provenance": step["provenance"],
                        "src_file": step["src_file"],
                        "src_line": step["src_line"],
                    },
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
            levels.append({"depth": current_depth, "count": len(found),
                           "nodes": found})
        frontier = [
            (row["kind"], row["id"], row["name"]) for row in found
            if row["traversable"]
        ]

    if truncated_by is None and frontier:
        truncated_by = "depth"

    return {
        "levels": levels, "reached": reached, "depth_walked": len(levels),
        "truncated_by": truncated_by, "edge_cap_hit": edge_cap_hit,
        "cycles_or_repeats_skipped": repeats,
    }


def _walk_payload(walk, depth, limit):
    direct = next(
        (level["nodes"] for level in walk["levels"] if level["depth"] == 1), []
    )
    onward = [level for level in walk["levels"] if level["depth"] > 1]
    evidence = {}
    for level in walk["levels"]:
        for node in level["nodes"]:
            kind = node["via"]["evidence"]
            evidence[kind] = evidence.get(kind, 0) + 1
    return {
        "direct": direct,
        "direct_count": len(direct),
        "onward": onward,
        "onward_count": sum(level["count"] for level in onward),
        "depths_returned": [level["depth"] for level in walk["levels"]],
        "reached": walk["reached"],
        "depth_walked": walk["depth_walked"],
        "steps_by_evidence": evidence,
        "cycles_or_repeats_skipped": walk["cycles_or_repeats_skipped"],
        "edge_cap_hit": walk["edge_cap_hit"],
        "truncated": (
            None if walk["truncated_by"] is None else {
                "by": walk["truncated_by"],
                "complete": False,
                "stopped_at": depth if walk["truncated_by"] == "depth" else limit,
                "hard_maximum": (
                    MAX_DEPTH if walk["truncated_by"] == "depth" else MAX_ROWS
                ),
                "note": (
                    f"the walk stopped after {depth} hop(s) with reachable "
                    f"objects still in hand; raise depth (hard maximum "
                    f"{MAX_DEPTH})."
                    if walk["truncated_by"] == "depth" else
                    f"the walk stopped at {limit} objects; raise limit (hard "
                    f"maximum {MAX_ROWS}) or narrow the direction."
                ) + " What is shown is correct; it is not everything.",
            }
        ),
    }


# --------------------------------------------------------------------------
# kg_find_workflows
# --------------------------------------------------------------------------


def kg_find_workflows(context, args):
    """Search the state-carrying fields this project declares."""
    limit, clamped = _rows_arg(args)
    query = (args.get("query") or "").strip()
    model = (args.get("model") or "").strip()
    state_source = (args.get("state_source") or "").strip()
    rows, truncated = flowqueries.find_workflows(
        context.connection, query, model, state_source, limit
    )
    total = flowqueries.count_workflows(
        context.connection, query, model, state_source
    )
    data = {
        "query": query or None,
        "model": model or None,
        "state_source": state_source or None,
        "max_limit": MAX_ROWS,
        "limit_clamped": clamped,
        "count": len(rows),
        "total_matching": total,
        "workflows": rows,
        "counts": flowqueries.counts(context.connection),
        "evidence_by_kind": _split_by_evidence(rows),
        "evidence_meaning": flowqueries.EVIDENCE_MEANING,
        "truncated": _truncation("workflows", truncated, len(rows), total),
        "state_source_meaning": {
            "selection": "the states are literals in a Selection field",
            "records": "the field points at a stage model, whose states are "
                       "data records this build does not read",
            "inferred": "no Selection was found; the field is known to be a "
                        "workflow only because code writes states to it",
        },
        "not_a_runtime_note": NOT_A_RUNTIME_NOTE,
        "absence_note": ABSENCE_NOTE,
    }
    lines = [
        f"{len(rows)} of {total} workflow(s)" + (" (truncated)" if truncated else "")
    ] + [
        f"  {r['model_name']}.{r['field_name']} — {r['state_count']} state(s), "
        f"{r['transition_count']} transition(s), {r['state_source']}"
        + (", partly computed" if r["has_dynamic_states"] else "")
        for r in rows
    ]
    return "\n".join(lines) or "no workflow matched", data


# --------------------------------------------------------------------------
# kg_get_workflow
# --------------------------------------------------------------------------


def kg_get_workflow(context, args):
    """One workflow: its states, its transitions, and what triggers them."""
    from .tools import ToolError

    model = args["model"].strip()
    field_name = (args.get("field") or "").strip()
    limit, clamped = _rows_arg(args)

    matches = flowqueries.get_workflow(context.connection, model, field_name)
    if not matches:
        suggestions = flowqueries.workflow_models_like(context.connection, model)
        hint = (
            f" Did you mean: {', '.join(suggestions)}?" if suggestions
            else " Use kg_find_workflows to list what this graph contains."
        )
        raise ToolError(
            f"no workflow on {model!r}"
            + (f" with field {field_name!r}" if field_name else "")
            + f" in this graph.{hint} A model has a workflow here only when it "
            "carries a state-like field that code declares or writes."
        )
    if len(matches) > 1 and not field_name:
        fields = ", ".join(sorted(row["field_name"] for row in matches))
        raise ToolError(
            f"{model!r} carries {len(matches)} workflow fields ({fields}). "
            "Name one with the `field` argument: they are separate workflows "
            "and merging them would describe a process that does not exist."
        )
    workflow = matches[0]

    states, states_cut = flowqueries.states_of(
        context.connection, workflow["workflow_id"], limit
    )
    transitions, transitions_cut = flowqueries.transitions_of(
        context.connection, workflow["workflow_id"], limit
    )
    method_ids = [t["method_id"] for t in transitions if t["method_id"]]
    triggers, triggers_cut = flowqueries.triggers_for_methods(
        context.connection, method_ids, limit
    )

    by_evidence = {
        flowqueries.EVIDENCE_DECLARED: [], flowqueries.EVIDENCE_STATIC: [],
        flowqueries.EVIDENCE_HEURISTIC: [],
    }
    for transition in transitions:
        # A transition read from an AST is `static`; one whose source state came
        # from a guard is only a candidate, and is reported as such.
        kind = (flowqueries.EVIDENCE_HEURISTIC
                if transition["from_evidence"] == flowqueries.EVIDENCE_HEURISTIC
                or transition["to_state"] is None
                else transition["evidence"])
        by_evidence.setdefault(kind, []).append(transition["transition_id"])

    limitations = [
        NOT_A_RUNTIME_NOTE,
        CONDITION_NOTE,
        ABSENCE_NOTE,
        "A transition's source state is only known when a guard in the same "
        "method names one, so most transitions have no from_state. That is a "
        "gap in what the code states, not a gap in the process.",
    ]
    if workflow["state_source"] == "records":
        limitations.append(
            "This workflow's states are data records in a stage model, which "
            "this build does not read, so it has no declared states here."
        )
    if workflow["has_dynamic_states"]:
        limitations.append(
            "Part of this field's Selection is produced at runtime, so the "
            "declared states are not the complete set."
        )

    data = {
        "workflow": workflow,
        "max_limit": MAX_ROWS,
        "limit_clamped": clamped,
        "states": states,
        "state_count": len(states),
        "transitions": transitions,
        "transition_count": len(transitions),
        "transitions_by_evidence": {k: v for k, v in by_evidence.items() if v},
        "triggers": triggers,
        "trigger_count": len(triggers),
        "evidence_meaning": flowqueries.EVIDENCE_MEANING,
        "truncated": [
            item for item in (
                _truncation("states", states_cut, len(states)),
                _truncation("transitions", transitions_cut, len(transitions)),
                _truncation("triggers", triggers_cut, len(triggers)),
            ) if item
        ],
        "limitations": limitations,
    }
    lines = [
        f"**{workflow['model_name']}.{workflow['field_name']}** — "
        f"{len(states)} state(s), {len(transitions)} transition(s), "
        f"{len(triggers)} trigger(s)",
        "states: " + ", ".join(
            f"{s['value']}({s['evidence'][0]})" for s in states
        ),
    ]
    return "\n".join(lines), data


# --------------------------------------------------------------------------
# kg_trace_process
# --------------------------------------------------------------------------


def kg_trace_process(context, args):
    """Follow a process outward from one object over stored edges only."""
    kind = args["kind"]
    name = args["name"].strip()
    direction = args.get("direction", "forward")
    depth, depth_clamped = clamp(args.get("depth", DEFAULT_DEPTH), 1, MAX_DEPTH)
    limit, clamped = _rows_arg(args)

    start = _resolve_one(context, kind, name)
    walk = traverse(
        context.connection, kind, start["id"], start["name"],
        direction=direction, depth=depth, limit=limit,
    )
    payload = _walk_payload(walk, depth, limit)

    data = {
        "start": {
            "kind": kind, "id": start["id"], "name": start["name"],
            "model_name": start.get("model_name"),
            "rel_path": start.get("rel_path"), "line": start.get("line"),
            "evidence": start.get("evidence"),
        },
        "direction": direction,
        "direction_meaning": {
            "forward": "edges followed as written: a workflow to its states, a "
                       "transition to the state it enters, a trigger to the "
                       "method it invokes",
            "backward": "edges followed in reverse: what reaches this object",
            "both": "both, with every hop labelled in via.direction",
        }[direction],
        "depth_requested": depth,
        "max_depth": MAX_DEPTH,
        "depth_clamped": depth_clamped,
        "max_limit": MAX_ROWS,
        "limit_clamped": clamped,
        **payload,
        "evidence_meaning": flowqueries.EVIDENCE_MEANING,
        "not_a_runtime_note": NOT_A_RUNTIME_NOTE,
        "ordering_note": (
            "Steps are grouped by how many edges away they are, not by when "
            "anything happens. Depth is distance in the graph; it is not "
            "sequence, and two objects at the same depth are not simultaneous."
        ),
        "cross_model_note": (
            "A hop to another model exists only where one model's code names "
            "the other literally. Two models are never joined because their "
            "state values happen to share a name.",
        )[0],
        "leaf_note": (
            "traversable=false means this graph holds no node for that target, "
            "so the walk stopped there. That is the edge of this graph, not the "
            "end of the process."
        ),
        "absence_note": ABSENCE_NOTE,
    }
    lines = [
        f"**{start['name']}** ({kind}) — {direction}, depth "
        f"{walk['depth_walked']}/{depth}, {walk['reached']} object(s)"
    ] + [
        f"  d{level['depth']}: " + ", ".join(
            f"[{n['kind']}] {n['name']}" for n in level["nodes"]
        )
        for level in walk["levels"]
    ]
    return "\n".join(lines), data


# --------------------------------------------------------------------------
# kg_process_impact
# --------------------------------------------------------------------------


def kg_process_impact(context, args):
    """What else is bound to a workflow object, in both directions."""
    kind = args["kind"]
    name = args["name"].strip()
    depth, depth_clamped = clamp(args.get("depth", 2), 1, MAX_DEPTH)
    limit, clamped = _rows_arg(args)

    start = _resolve_one(context, kind, name)
    walk = traverse(
        context.connection, kind, start["id"], start["name"],
        direction="both", depth=depth, limit=limit,
    )
    payload = _walk_payload(walk, depth, limit)

    by_kind = {}
    models = set()
    for level in walk["levels"]:
        for node in level["nodes"]:
            by_kind[node["kind"]] = by_kind.get(node["kind"], 0) + 1
            if node.get("model_name"):
                models.add(node["model_name"])

    data = {
        "start": {
            "kind": kind, "id": start["id"], "name": start["name"],
            "model_name": start.get("model_name"),
            "rel_path": start.get("rel_path"), "line": start.get("line"),
        },
        "depth_requested": depth,
        "max_depth": MAX_DEPTH,
        "depth_clamped": depth_clamped,
        "max_limit": MAX_ROWS,
        "limit_clamped": clamped,
        **payload,
        "reached_by_kind": by_kind,
        "models_involved": sorted(models),
        "evidence_meaning": flowqueries.EVIDENCE_MEANING,
        "not_a_verdict_note": (
            "This is reachability over declared and static edges. It is not a "
            "change-risk score, not an execution trace, and not a security "
            "judgement: what a method does beyond setting a state is not "
            "indexed."
        ),
        "not_a_runtime_note": NOT_A_RUNTIME_NOTE,
        "absence_note": ABSENCE_NOTE,
    }
    lines = [
        f"**{start['name']}** ({kind}) — {walk['reached']} object(s) within "
        f"depth {walk['depth_walked']}/{depth}",
        "by kind: " + ", ".join(f"{k}×{v}" for k, v in sorted(by_kind.items())),
    ]
    return "\n".join(lines), data
