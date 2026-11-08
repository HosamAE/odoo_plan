"""The M3 tools: models, XML IDs, and declared security.

Same contract as the M2 tools -- read-only, no path or SQL argument, every
list bounded and every truncation announced -- with one addition that matters
more than any of them.

**No tool here concludes that anyone has access to anything.** The security
tool returns the ACL rows and record rules a model has, with file and line for
each, and states the Odoo semantics needed to read them. It does not combine
them, does not evaluate a domain, and does not answer "can this user write to
this record". That answer depends on the user's groups, their company, the
record's own values and Odoo's runtime rule combination, none of which a
static graph holds. Every security response carries that limit in the payload
so a model reading only the JSON cannot miss it.
"""

from __future__ import annotations

from . import m3queries
from .limits import MAX_LIMIT, MAX_NODES, clamp

#: What a caller may ask for in one detail list, and the ceiling regardless.
DEFAULT_ROWS = 25
MAX_ROWS = m3queries.MAX_ROWS

#: Stated in every security response. Written once, so the wording cannot
#: drift between tools.
SECURITY_DISCLAIMER = (
    "Declared evidence only. This is what the source files say, not an access "
    "decision. Whether a particular user may read, write, create or delete a "
    "particular record depends on that user's groups, their active company, "
    "the record's own values, and Odoo's runtime combination of every rule "
    "that applies -- none of which is in this graph."
)

SECURITY_SEMANTICS = {
    "acl_rights_are_additive": (
        "A user's access to a model is the union of the rights granted by every "
        "ACL row whose group they belong to. perm_write=0 does not deny writing; "
        "it grants none, and another row may grant it. Odoo has no deny row."
    ),
    "global_and_group_rules_combine_differently": (
        "Record rules with no group are global and are ANDed: every one must "
        "pass. Rules attached to groups are ORed across the groups the user "
        "has, and that result is ANDed with the global ones. Adding a group "
        "rule can widen access; adding a global rule can only narrow it."
    ),
    "a_missing_right_is_not_a_decision": (
        "Model access is the first gate, not the only one. Record rules, "
        "sudo(), field-level groups= and controller checks all sit around it, "
        "and this milestone does not index the last two."
    ),
    "domains_are_source_text": (
        "domain_force is stored exactly as written and is never evaluated. A "
        "domain that references user or env cannot be understood statically."
    ),
    "group_inheritance_is_direct_only": (
        "implied_ids is recorded as the direct edges the source writes. Odoo's "
        "own all_implied_ids is the reflexive transitive closure and is computed "
        "at runtime; this graph never computes it. A group listed here may imply "
        "others that are not listed."
    ),
    "a_privilege_is_not_a_permission": (
        "res.groups.privilege is organisational metadata. Odoo displays a group "
        "as 'Privilege / Group' and makes a group's name unique within its "
        "privilege. Belonging to a privilege grants nothing."
    ),
}


def _rows_arg(args, key="limit"):
    requested = args.get(key, DEFAULT_ROWS)
    value, clamped = clamp(requested, 1, MAX_ROWS)
    return value, clamped


def _truncation(name: str, truncated: bool, shown: int, total: int | None = None):
    """A uniform, explicit statement of what was left out."""
    if not truncated:
        return None
    return {
        "list": name,
        "shown": shown,
        "complete": False,
        "note": (
            f"more rows exist than the {shown} shown; raise limit (max {MAX_ROWS}) "
            "or narrow the query. The counts field is complete regardless."
        ),
        **({"total": total} if total is not None else {}),
    }


def _require_model(context, name: str) -> dict:
    from .tools import ToolError

    model = m3queries.get_model(context.connection, name)
    if model is not None:
        return model
    suggestions = m3queries.model_names_like(context.connection, name)
    hint = (
        f" Did you mean: {', '.join(suggestions)}?" if suggestions
        else " Use kg_find_models to list what this graph contains."
    )
    raise ToolError(
        f"unknown model {name!r} in this graph.{hint} Note that Odoo core models "
        "are not indexed: this graph holds only models this project declares or "
        "extends."
    )


# --------------------------------------------------------------------------
# handlers
# --------------------------------------------------------------------------


def kg_find_models(context, args):
    limit, clamped = _rows_arg(args)
    rows, truncated = m3queries.find_models(
        context.connection,
        (args.get("query") or "").strip(),
        (args.get("module") or "").strip(),
        (args.get("kind") or "").strip(),
        limit,
    )
    data = {
        "query": args.get("query") or None,
        "module": args.get("module") or None,
        "kind": args.get("kind") or None,
        "max_limit": MAX_ROWS,
        "limit_clamped": clamped,
        "count": len(rows),
        "models": rows,
        "truncated": _truncation("models", truncated, len(rows)),
        "scope_note": (
            "Only models this project declares or extends. Odoo core models are "
            "not indexed."
        ),
    }
    header = f"{len(rows)} model(s)" + (" (truncated)" if truncated else "")
    lines = [header] + [
        f"  {r['model_name']} — {r['kind']}, {r['definition_count']} definition(s), "
        f"{r['field_count']} field(s), {r['method_count']} method(s)"
        for r in rows
    ]
    return "\n".join(lines) or "no models matched", data


def kg_get_model(context, args):
    model = _require_model(context, args["model"])
    name = model["model_name"]
    limit, clamped = _rows_arg(args)
    counts = m3queries.model_counts(context.connection, name)

    definitions, def_trunc = m3queries.model_definitions(context.connection, name, limit)
    inherits, inh_trunc = m3queries.model_inherits(context.connection, name, limit)
    fields, field_trunc = m3queries.model_fields(context.connection, name, limit)
    methods, method_trunc = m3queries.model_methods(context.connection, name, limit)

    data = {
        "model": name,
        "kind": model["kind"],
        "declares_name": bool(model["declares_name"]),
        "max_limit": MAX_ROWS,
        "limit_clamped": clamped,
        "counts": counts,
        "definitions": definitions,
        "inherits": inherits,
        "fields": fields,
        "methods": methods,
        "truncated": [
            item for item in (
                _truncation("definitions", def_trunc, len(definitions), counts["definitions"]),
                _truncation("inherits", inh_trunc, len(inherits)),
                _truncation("fields", field_trunc, len(fields), counts["fields"]),
                _truncation("methods", method_trunc, len(methods), counts["methods"]),
            ) if item
        ],
        "security_note": (
            f"{counts['acl_rows']} ACL row(s) and {counts['record_rules']} record "
            "rule(s) name this model. Use kg_security_for_model for the detail; "
            "this tool deliberately does not summarise access."
        ),
        "not_indexed": [
            "method bodies and the call graph",
            "field-level groups= beyond the literal value",
        ],
    }
    lines = [
        f"**{name}** — {model['kind']}, {counts['definitions']} definition(s)",
        f"{counts['fields']} field(s), {counts['methods']} method(s), "
        f"extended by {counts['extended_by']}, {counts['incoming_relations']} "
        f"incoming relation(s), {counts['xml_records']} XML record(s)",
    ] + [f"  defined at {d['rel_path']}:{d['line']} ({d['class_name']}, {d['shape']})"
         for d in definitions]
    return "\n".join(lines), data


def kg_model_impact(context, args):
    model = _require_model(context, args["model"])
    name = model["model_name"]
    limit, clamped = _rows_arg(args)
    counts = m3queries.model_counts(context.connection, name)

    extended, ext_trunc = m3queries.model_extended_by(context.connection, name, limit)
    pointing, point_trunc = m3queries.fields_pointing_at(context.connection, name, limit)
    records, rec_trunc = m3queries.records_of_model(context.connection, name, limit)
    acls, acl_trunc = m3queries.acls_for_model(context.connection, name, limit)
    rules, rule_trunc = m3queries.rules_for_model(context.connection, name, limit)

    data = {
        "model": name,
        "max_limit": MAX_ROWS,
        "max_nodes": MAX_NODES,
        "limit_clamped": clamped,
        "counts": counts,
        "extended_by": extended,
        "referenced_by_fields": pointing,
        "xml_records": records,
        "acl_rows": [
            {k: v for k, v in row.items() if k in
             ("xmlid", "module", "rel_path", "line", "group_target", "is_global")}
            for row in acls
        ],
        "record_rules": [
            {k: v for k, v in row.items() if k in
             ("xmlid", "module", "rel_path", "line", "is_global")}
            for row in rules
        ],
        "truncated": [
            item for item in (
                _truncation("extended_by", ext_trunc, len(extended), counts["extended_by"]),
                _truncation("referenced_by_fields", point_trunc, len(pointing),
                            counts["incoming_relations"]),
                _truncation("xml_records", rec_trunc, len(records), counts["xml_records"]),
                _truncation("acl_rows", acl_trunc, len(acls), counts["acl_rows"]),
                _truncation("record_rules", rule_trunc, len(rules), counts["record_rules"]),
            ) if item
        ],
        "impact_note": (
            "What in this project names this model. It is not a change-risk "
            "score, and it cannot see method bodies, the call graph, or Odoo "
            "core, so absence here is not evidence of absence in the system."
        ),
        "security_note": SECURITY_DISCLAIMER,
    }
    lines = [
        f"**Impact of changing {name}**",
        f"extended by {counts['extended_by']} definition(s), referenced by "
        f"{counts['incoming_relations']} relational field(s), "
        f"{counts['xml_records']} XML record(s), {counts['acl_rows']} ACL row(s), "
        f"{counts['record_rules']} record rule(s)",
    ]
    return "\n".join(lines), data


def kg_find_xmlids(context, args):
    limit, clamped = _rows_arg(args)
    rows, truncated = m3queries.find_xmlids(
        context.connection,
        (args.get("query") or "").strip(),
        (args.get("module") or "").strip(),
        limit,
    )
    data = {
        "query": args.get("query") or None,
        "module": args.get("module") or None,
        "max_limit": MAX_ROWS,
        "limit_clamped": clamped,
        "count": len(rows),
        "xmlids": rows,
        "truncated": _truncation("xmlids", truncated, len(rows)),
        "scope_note": (
            "Only XML IDs defined by files this project's manifests declare. IDs "
            "Odoo generates for models, and IDs owned by core, are not listed "
            "here even when this project references them."
        ),
    }
    lines = [f"{len(rows)} XML ID(s)" + (" (truncated)" if truncated else "")] + [
        f"  {r['xmlid']} — {r['definition_count']} definition(s), {r['winner_rule']}"
        for r in rows
    ]
    return "\n".join(lines) or "no XML IDs matched", data


def kg_get_xmlid(context, args):
    from .tools import ToolError

    requested = args["xmlid"].strip()
    row = m3queries.get_xmlid(context.connection, requested)
    if row is None:
        suggestions = m3queries.xmlid_names_like(context.connection, requested)
        hint = (
            f" Did you mean: {', '.join(suggestions)}?" if suggestions
            else " Use kg_find_xmlids to list what this graph contains."
        )
        raise ToolError(
            f"unknown XML ID {requested!r} in this graph.{hint} An ID owned by "
            "Odoo core, or one Odoo generates for a model, is never defined here "
            "even when this project references it."
        )

    limit, clamped = _rows_arg(args)
    definitions, def_trunc = m3queries.xmlid_definitions(
        context.connection, requested, limit
    )
    references, ref_trunc = m3queries.xmlid_references(
        context.connection, requested, limit
    )
    reference_total = m3queries.xmlid_reference_count(context.connection, requested)

    data = {
        "xmlid": row["xmlid"],
        "module": row["module"],
        "local_id": row["local_id"],
        "definition_count": row["definition_count"],
        "winner_record_id": row["winner_record_id"],
        "winner_rule": row["winner_rule"],
        "max_limit": MAX_ROWS,
        "limit_clamped": clamped,
        "definitions": definitions,
        "references": references,
        "reference_count": reference_total,
        "truncated": [
            item for item in (
                _truncation("definitions", def_trunc, len(definitions),
                            row["definition_count"]),
                _truncation("references", ref_trunc, len(references), reference_total),
            ) if item
        ],
        "winner_rule_note": {
            "single": "Only one definition; nothing to resolve.",
            "last_in_module": (
                "Several definitions in one module. Odoo applies data files in "
                "manifest order and, within a file, in document order, so the "
                "last one wins."
            ),
            "ambiguous_across_modules": (
                "Defined in more than one module. The winner shown is chosen "
                "deterministically, but the real one depends on install order, "
                "which this milestone does not model. Treat it as provisional."
            ),
        }.get(row["winner_rule"], ""),
    }
    lines = [
        f"**{row['xmlid']}** — {row['definition_count']} definition(s), "
        f"{reference_total} reference(s), winner rule {row['winner_rule']}",
    ] + [f"  defined at {d['rel_path']}:{d['line']} (<{d['tag']}>"
         + (f" model={d['record_model']}" if d["record_model"] else "") + ")"
         for d in definitions]
    return "\n".join(lines), data


def kg_security_for_model(context, args):
    model = _require_model(context, args["model"])
    name = model["model_name"]
    limit, clamped = _rows_arg(args)
    counts = m3queries.security_counts(context.connection, name)

    acls, acl_trunc = m3queries.acls_for_model(context.connection, name, limit)
    rules, rule_trunc = m3queries.rules_for_model(context.connection, name, limit)
    for rule in rules:
        groups, groups_trunc = m3queries.groups_of_rule(
            context.connection, rule.pop("rule_id"), limit
        )
        rule["groups"] = groups
        rule["groups_truncated"] = groups_trunc

    # The distinct groups these rows name, each with its privilege. Bounded:
    # this is context for the rows above, not a directory of the group tree.
    named = [row["group_target"] for row in acls]
    for rule in rules:
        named.extend(group["group_xmlid"] for group in rule["groups"])
    groups, groups_trunc = m3queries.groups_with_privilege(
        context.connection, named, limit
    )

    data = {
        "model": name,
        "max_limit": MAX_ROWS,
        "limit_clamped": clamped,
        "counts": counts,
        "acl_rows": acls,
        "record_rules": rules,
        "groups": groups,
        "groups_note": (
            "Only the groups named above, with the privilege each belongs to. "
            "A group defined outside this project has no row here. Group "
            "inheritance is not expanded: see odoo_semantics."
        ),
        "truncated": [
            item for item in (
                _truncation("acl_rows", acl_trunc, len(acls), counts["acl_rows"]),
                _truncation("record_rules", rule_trunc, len(rules),
                            counts["record_rules"]),
                _truncation("groups", groups_trunc, len(groups)),
            ) if item
        ],
        "this_is_not_an_access_decision": SECURITY_DISCLAIMER,
        "odoo_semantics": SECURITY_SEMANTICS,
        "not_indexed": [
            "field-level groups= as an access effect",
            "controller and route level checks",
            "sudo() and with_user() in method bodies",
            "anything Odoo core declares about this model",
        ],
    }
    lines = [
        f"**Declared security for {name}**",
        f"{counts['acl_rows']} ACL row(s) ({counts['acl_rows_global']} global), "
        f"{counts['record_rules']} record rule(s) "
        f"({counts['record_rules_global']} global)",
        "",
        SECURITY_DISCLAIMER,
    ]
    return "\n".join(lines), data
