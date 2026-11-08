"""Build the security layer: groups, ACL entries, record rules.

What this layer is, and just as importantly what it is not.

**It is** an index of what the source *declares*: which groups exist, which
imply which, which ACL rows name which model and group, which record rules
apply to which model for which groups, and the text of each rule's domain.
Every one of those is a fact written down in a file, with a line number.

**It is not** an authorisation engine. It cannot say whether a given user may
read a given record, and it must never be read as saying so. Deciding that
requires the user's actual groups, company, the record's values, and Odoo's
runtime combination of every rule that applies -- none of which lives in a
static graph. The graph shows declared evidence so a person can reason; it
does not do the reasoning.

Three pieces of Odoo semantics matter enough to state here, because a reader
who assumes the wrong one will draw the wrong conclusion from correct data:

* **ACL rights are additive.** A user's access to a model is the union of the
  rights granted by every ACL row whose group the user belongs to. One row
  with ``perm_write=0`` does not deny writing; it simply grants none, and
  another row may grant it. There is no such thing as a deny row.

* **Global and group record rules combine differently.** Rules with no group
  are global and are ANDed: every one of them must pass. Rules attached to
  groups are ORed among the groups the user has, and that result is ANDed with
  the global ones. So adding a group rule can *widen* access, while adding a
  global rule can only narrow it.

* **A missing ACL right is not a complete access decision.** Model access is
  the first gate, not the only one. Record rules, ``sudo()``, field-level
  ``groups=``, and controller-level checks all sit around it.

* **``implied_ids`` is recorded as DIRECT edges only.** Odoo core documents its
  own ``all_implied_ids`` as "the reflexive transitive closure of implied_ids"
  and *computes it at runtime*. This layer stores the edges exactly as written
  and never computes that closure. Following the chain is a reader's job, done
  knowingly; materialising it here would quietly turn a structural index into a
  membership answer, which is the one thing this layer must not become.

A ``res.groups.privilege`` is organisational metadata, not a permission. Odoo
renders a group as ``"Privilege / Group"`` and makes a group's name unique
within its privilege, so it is a real identity dimension worth indexing --
but belonging to a privilege grants nothing at all.

Domains are stored as source text and never evaluated -- not with ``eval``,
not with ``safe_eval``, not by any other route. The same goes for the
``eval="[(4, ref('...'))]"`` expressions Odoo uses for ``implied_ids`` and a
rule's ``groups``: the referenced XML IDs are extracted lexically, exactly as
the XML layer does, and the arithmetic around them is left alone.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field as dc_field

from .csvread import PERMISSIONS, read_acl_csv
from .xmlindex import data_file_id
from .store import (
    AclRow, GraphEdge, GroupRow, NodeDiagnostic, PrivilegeRow, RuleRow, stable_id,
)
from .xmlread import XmlReadError, read_xml

#: The file name Odoo recognises as a model-access table. A module may declare
#: other CSVs as ordinary data -- testing19 declares one -- and reading those
#: as ACLs would invent access rules that do not exist.
ACL_FILENAME = "ir.model.access.csv"

#: Records this layer understands. Everything else in a data file belongs to
#: the XML layer and is left alone here.
GROUP_MODEL = "res.groups"
RULE_MODEL = "ir.rule"
PRIVILEGE_MODEL = "res.groups.privilege"

#: ``ref('...')`` inside an eval expression. Only literal single-argument calls
#: are read; the surrounding command tuples are never evaluated.
_EVAL_REF = re.compile(r"\bref\(\s*['\"]([^'\"]+)['\"]\s*\)")

#: How Odoo's ACL model target is written: the XML ID of an ``ir.model``
#: record, which Odoo generates as ``model_<name with dots as underscores>``.
_GENERATED_MODEL_PREFIX = "model_"

PROVENANCE_IMPLIED = "xml.field[implied_ids].eval.ref"
PROVENANCE_RULE_GROUPS = "xml.field[groups].eval.ref"
PROVENANCE_RULE_MODEL = "xml.field[model_id]@ref"
PROVENANCE_ACL_MODEL = "csv.model_id"
PROVENANCE_ACL_GROUP = "csv.group_id"
PROVENANCE_GROUP_PRIVILEGE = "xml.field[privilege_id]@ref"


@dataclass
class SecurityLayer:
    groups: list[GroupRow] = dc_field(default_factory=list)
    privileges: list[PrivilegeRow] = dc_field(default_factory=list)
    acls: list[AclRow] = dc_field(default_factory=list)
    rules: list[RuleRow] = dc_field(default_factory=list)
    edges: list[GraphEdge] = dc_field(default_factory=list)
    diagnostics: list[NodeDiagnostic] = dc_field(default_factory=list)
    counts: dict[str, int] = dc_field(default_factory=dict)


def build_security_layer(
    project_id: str,
    declared,
    read_bytes,
    known_models: dict[str, set[str]],
    project_modules: set[str],
) -> SecurityLayer:
    """Index every manifest-declared security file.

    ``declared`` is the same ordered list of manifest entries the XML layer
    uses, so this layer sees exactly the files Odoo would load -- no more, and
    in the same order.
    """
    layer = SecurityLayer()
    counts = layer.counts
    generated = _generated_model_ids(known_models)
    counts["generated_model_ids"] = len(generated)

    pending: list[tuple] = []
    for entry in declared:
        if entry.path is None or entry.file_key is None:
            # Missing or excluded. The XML layer already reported it; saying so
            # twice would only make the diagnostics harder to read.
            continue
        name = os.path.basename(entry.rel_path).lower()
        if name == ACL_FILENAME:
            _index_acl_file(project_id, entry, read_bytes, layer, pending, counts)
        elif entry.rel_path.lower().endswith(".xml"):
            _index_security_xml(project_id, entry, read_bytes, layer, pending, counts)

    defined_groups = {row.group_xmlid for row in layer.groups}
    defined_privileges = {row.privilege_xmlid for row in layer.privileges}
    _bind(
        layer, pending, defined_groups, defined_privileges, generated,
        project_modules, counts,
    )

    counts["groups"] = len(layer.groups)
    counts["privileges"] = len(layer.privileges)
    counts["acls"] = len(layer.acls)
    counts["rules"] = len(layer.rules)
    counts["edges"] = len(layer.edges)
    return layer


# --------------------------------------------------------------------------
# ACL CSV
# --------------------------------------------------------------------------


def _index_acl_file(project_id, entry, read_bytes, layer, pending, counts) -> None:
    # The same id the XML layer gave this file, so a diagnostic hung here
    # points at a node that exists rather than at a bare path.
    file_id = data_file_id(project_id, entry.module, entry.rel_path)
    try:
        source = read_bytes(entry.path)
    except OSError as exc:
        layer.diagnostics.append(
            NodeDiagnostic(
                "error", "acl_unreadable", f"{entry.rel_path}: {exc}",
                "data_file", file_id, entry.rel_path, None,
            )
        )
        return

    parsed = read_acl_csv(source)
    counts["acl_files"] = counts.get("acl_files", 0) + 1
    if not parsed.usable:
        counts["acl_files_unusable"] = counts.get("acl_files_unusable", 0) + 1

    for problem in parsed.problems:
        counts[f"acl_problem_{problem.category}"] = (
            counts.get(f"acl_problem_{problem.category}", 0) + 1
        )
        layer.diagnostics.append(
            NodeDiagnostic(
                problem.severity, problem.category,
                f"{entry.rel_path}: {problem.message}",
                "data_file", file_id, entry.rel_path, problem.line,
            )
        )

    for row in parsed.rows:
        acl_id = stable_id(project_id, "acl", entry.module, entry.rel_path, row.csv_id)
        layer.acls.append(
            AclRow(
                acl_id=acl_id,
                csv_id=row.csv_id,
                xmlid=_qualify(row.csv_id, entry.module),
                module=entry.module,
                name=row.name,
                model_target=row.model_target,
                group_target=row.group_target,
                is_global=row.is_global,
                perm_read=row.permissions["perm_read"],
                perm_write=row.permissions["perm_write"],
                perm_create=row.permissions["perm_create"],
                perm_unlink=row.permissions["perm_unlink"],
                rel_path=entry.rel_path,
                line=row.line,
            )
        )
        pending.append((
            "acl_on_model", "acl", acl_id, "model",
            _qualify(row.model_target, entry.module), PROVENANCE_ACL_MODEL,
            entry.rel_path, row.line, entry.module,
        ))
        if row.group_target:
            pending.append((
                "acl_grants_group", "acl", acl_id, "group",
                _qualify(row.group_target, entry.module), PROVENANCE_ACL_GROUP,
                entry.rel_path, row.line, entry.module,
            ))
        else:
            counts["acl_global"] = counts.get("acl_global", 0) + 1
            layer.diagnostics.append(
                NodeDiagnostic(
                    "info", "acl_is_global",
                    f"{entry.rel_path}:{row.line}: {row.csv_id!r} names no group, so "
                    "it grants its rights to every user. ACL rights are additive: "
                    "this widens access and cannot narrow it.",
                    "acl", acl_id, entry.rel_path, row.line,
                )
            )


# --------------------------------------------------------------------------
# groups and rules
# --------------------------------------------------------------------------


def _index_security_xml(project_id, entry, read_bytes, layer, pending, counts) -> None:
    try:
        source = read_bytes(entry.path)
    except OSError:
        return
    try:
        roots = read_xml(source)
    except XmlReadError:
        # Already reported by the XML layer, with its own classification.
        return

    for root in roots:
        for node in root.iter():
            if node.tag != "record":
                continue
            model = (node.get("model") or "").strip()
            if model == GROUP_MODEL:
                _index_group(project_id, entry, node, layer, pending, counts)
            elif model == RULE_MODEL:
                _index_rule(project_id, entry, node, layer, pending, counts)
            elif model == PRIVILEGE_MODEL:
                _index_privilege(project_id, entry, node, layer, counts)


def _index_group(project_id, entry, node, layer, pending, counts) -> None:
    raw_id = (node.get("id") or "").strip()
    if not raw_id:
        layer.diagnostics.append(
            NodeDiagnostic(
                "warning", "group_without_id",
                f"{entry.rel_path}:{node.line}: a res.groups record has no id, so "
                "nothing can grant access to it",
                "data_file", data_file_id(project_id, entry.module, entry.rel_path),
                entry.rel_path, node.line,
            )
        )
        return
    xmlid = _qualify(raw_id, entry.module)
    fields = _fields(node)
    layer.groups.append(
        GroupRow(
            group_xmlid=xmlid,
            module=entry.module,
            local_id=raw_id.rpartition(".")[2],
            name=_text_of(fields.get("name")),
            privilege_xmlid=_ref_of(fields.get("privilege_id"), entry.module),
            rel_path=entry.rel_path,
            line=node.line,
        )
    )
    counts["group_records"] = counts.get("group_records", 0) + 1

    privilege = fields.get("privilege_id")
    if privilege is not None:
        target = _ref_of(privilege, entry.module)
        if target:
            pending.append((
                "group_in_privilege", "group", xmlid, "privilege", target,
                PROVENANCE_GROUP_PRIVILEGE, entry.rel_path, privilege.line,
                entry.module,
            ))
        else:
            layer.diagnostics.append(
                NodeDiagnostic(
                    "info", "group_privilege_not_literal",
                    f"{entry.rel_path}:{privilege.line}: privilege_id names no "
                    "literal ref; the privilege is not statically known",
                    "group", xmlid, entry.rel_path, privilege.line,
                )
            )

    implied = fields.get("implied_ids")
    if implied is None:
        return
    targets = _referenced_xmlids(implied, entry.module)
    if not targets:
        counts["implied_unreadable"] = counts.get("implied_unreadable", 0) + 1
        layer.diagnostics.append(
            NodeDiagnostic(
                "info", "group_implied_not_literal",
                f"{entry.rel_path}:{implied.line}: implied_ids names no literal "
                "ref(); the inherited groups are not statically known",
                "group", xmlid, entry.rel_path, implied.line,
            )
        )
        return
    for target in targets:
        pending.append((
            "group_implies", "group", xmlid, "group", target,
            PROVENANCE_IMPLIED, entry.rel_path, implied.line, entry.module,
        ))


def _index_privilege(project_id, entry, node, layer, counts) -> None:
    """A res.groups.privilege record: identity metadata, never a permission."""
    raw_id = (node.get("id") or "").strip()
    if not raw_id:
        layer.diagnostics.append(
            NodeDiagnostic(
                "warning", "privilege_without_id",
                f"{entry.rel_path}:{node.line}: a res.groups.privilege record has "
                "no id, so no group can name it",
                "data_file", data_file_id(project_id, entry.module, entry.rel_path),
                entry.rel_path, node.line,
            )
        )
        return
    fields = _fields(node)
    layer.privileges.append(
        PrivilegeRow(
            privilege_xmlid=_qualify(raw_id, entry.module),
            module=entry.module,
            local_id=raw_id.rpartition(".")[2],
            name=_text_of(fields.get("name")),
            sequence=_int_of(fields.get("sequence")),
            description=_text_of(fields.get("description")),
            placeholder=_text_of(fields.get("placeholder")),
            # ir.module.category is not a node type in this milestone, so the
            # reference is kept verbatim rather than bound to nothing.
            category_xmlid=_ref_of(fields.get("category_id"), entry.module),
            rel_path=entry.rel_path,
            line=node.line,
        )
    )
    counts["privilege_records"] = counts.get("privilege_records", 0) + 1


def _index_rule(project_id, entry, node, layer, pending, counts) -> None:
    raw_id = (node.get("id") or "").strip()
    fields = _fields(node)
    xmlid = _qualify(raw_id, entry.module) if raw_id else None
    rule_id = xmlid or stable_id(
        project_id, "rule", entry.module, entry.rel_path, node.line
    )
    if not raw_id:
        layer.diagnostics.append(
            NodeDiagnostic(
                "warning", "rule_without_id",
                f"{entry.rel_path}:{node.line}: an ir.rule record has no id and "
                "cannot be overridden or removed by another module",
                "rule", rule_id, entry.rel_path, node.line,
            )
        )

    model_field = fields.get("model_id")
    domain_field = fields.get("domain_force")
    groups_field = fields.get("groups")
    group_targets = _referenced_xmlids(groups_field, entry.module) if groups_field else []
    # Odoo treats a rule with no groups as global. An explicit `global` field
    # exists too, but it is computed from the groups in modern Odoo; the
    # declared value is recorded and the absence of groups is what decides.
    is_global = not group_targets

    layer.rules.append(
        RuleRow(
            rule_id=rule_id,
            xmlid=xmlid,
            module=entry.module,
            name=_text_of(fields.get("name")),
            model_target=_ref_of(model_field, entry.module) or "",
            domain_force=_text_of(domain_field),
            is_global=is_global,
            global_declared=_declared_flag(fields.get("global")),
            active_declared=_declared_flag(fields.get("active")),
            perm_read=_declared_flag(fields.get("perm_read")),
            perm_write=_declared_flag(fields.get("perm_write")),
            perm_create=_declared_flag(fields.get("perm_create")),
            perm_unlink=_declared_flag(fields.get("perm_unlink")),
            rel_path=entry.rel_path,
            line=node.line,
        )
    )
    counts["rule_records"] = counts.get("rule_records", 0) + 1

    if model_field is None or not _ref_of(model_field, entry.module):
        layer.diagnostics.append(
            NodeDiagnostic(
                "warning", "rule_without_model",
                f"{entry.rel_path}:{node.line}: the rule names no model_id ref, so "
                "what it restricts is not statically known",
                "rule", rule_id, entry.rel_path, node.line,
            )
        )
    else:
        pending.append((
            "rule_on_model", "rule", rule_id, "model",
            _ref_of(model_field, entry.module), PROVENANCE_RULE_MODEL,
            entry.rel_path, node.line, entry.module,
        ))

    if domain_field is None:
        layer.diagnostics.append(
            NodeDiagnostic(
                "warning", "rule_without_domain",
                f"{entry.rel_path}:{node.line}: the rule declares no domain_force",
                "rule", rule_id, entry.rel_path, node.line,
            )
        )

    for target in group_targets:
        pending.append((
            "rule_for_group", "rule", rule_id, "group", target,
            PROVENANCE_RULE_GROUPS, entry.rel_path, groups_field.line, entry.module,
        ))
    if is_global:
        counts["rules_global"] = counts.get("rules_global", 0) + 1
        layer.diagnostics.append(
            NodeDiagnostic(
                "info", "rule_is_global",
                f"{entry.rel_path}:{node.line}: the rule names no groups, so it is "
                "global. Global rules are ANDed with each other and with the "
                "group rules that apply, so this can only narrow access.",
                "rule", rule_id, entry.rel_path, node.line,
            )
        )
    if groups_field is not None and not group_targets:
        counts["rule_groups_unreadable"] = counts.get("rule_groups_unreadable", 0) + 1
        layer.diagnostics.append(
            NodeDiagnostic(
                "warning", "rule_groups_not_literal",
                f"{entry.rel_path}:{groups_field.line}: groups names no literal "
                "ref(); which groups this rule applies to is not statically known, "
                "and it is therefore NOT safe to read it as global",
                "rule", rule_id, entry.rel_path, groups_field.line,
            )
        )


# --------------------------------------------------------------------------
# binding
# --------------------------------------------------------------------------


def _bind(
    layer, pending, defined_groups, defined_privileges, generated,
    project_modules, counts,
) -> None:
    """Resolve every pending edge against what this graph actually knows."""
    for (
        table, src_kind, src_id, dst_kind, target, provenance, rel_path, line, module
    ) in pending:
        if dst_kind == "model":
            binding, dst = _bind_model(target, generated, project_modules)
        elif dst_kind == "privilege":
            binding, dst = _bind_named(target, defined_privileges, project_modules)
        else:
            binding, dst = _bind_named(target, defined_groups, project_modules)

        layer.edges.append(
            GraphEdge(
                table=table, src_kind=src_kind, src_id=src_id, dst_kind=dst_kind,
                dst_id=dst, dst_unresolved=target,
                binding=binding, confidence="certain", provenance=provenance,
                src_file=rel_path, src_line=line,
            )
        )
        counts[f"{dst_kind}_{binding}"] = counts.get(f"{dst_kind}_{binding}", 0) + 1
        if binding == "unresolved":
            layer.diagnostics.append(
                NodeDiagnostic(
                    "warning", f"security_{dst_kind}_unresolved",
                    f"{rel_path}:{line}: names {target!r}, but module "
                    f"{target.partition('.')[0]!r} is in this project and defines "
                    "no such thing",
                    src_kind, src_id, rel_path, line,
                )
            )


def _bind_model(target: str, generated: dict[str, str], project_modules: set[str]):
    """An ACL or rule names a model by the XML ID Odoo generates for it."""
    model = generated.get(target)
    if model is not None:
        return "resolved", model
    owner = target.partition(".")[0]
    if owner not in project_modules:
        # A model owned by Odoo core. Named and understood; deliberately given
        # no node, exactly as the module layer treats a core dependency.
        return "external", None
    return "unresolved", None


def _bind_named(target: str, defined: set[str], project_modules: set[str]):
    """The shared rule for anything named by XML ID: a group or a privilege."""
    if target in defined:
        return "resolved", target
    owner = target.partition(".")[0]
    if owner not in project_modules:
        # Owned by Odoo core. Named and understood; no node is invented.
        return "external", None
    return "unresolved", None


def _generated_model_ids(known_models: dict[str, set[str]]) -> dict[str, str]:
    """Generated ``model_<name>`` XML IDs, built forwards from each model.

    The same rule the XML layer uses, and for the same reason: working
    backwards from an id is a guess, because ``model_foo_bar`` could be
    ``foo.bar`` or ``foo_bar``.
    """
    generated: dict[str, str] = {}
    for model, modules in known_models.items():
        local = f"{_GENERATED_MODEL_PREFIX}{model.replace('.', '_')}"
        for module in modules:
            if module:
                generated[f"{module}.{local}"] = model
    return generated


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _qualify(raw: str, module: str) -> str:
    raw = raw.strip()
    return raw if "." in raw else f"{module}.{raw}"


def _fields(node) -> dict:
    """Direct ``<field name=...>`` children, last one winning as Python would."""
    found = {}
    for child in node.children:
        if child.tag == "field":
            name = (child.get("name") or "").strip()
            if name:
                found[name] = child
    return found


def _text_of(node) -> str | None:
    if node is None:
        return None
    text = (node.text or "").strip()
    return text or None


def _ref_of(node, module: str) -> str | None:
    if node is None:
        return None
    ref = (node.get("ref") or "").strip()
    return _qualify(ref, module) if ref else None


def _int_of(node) -> int | None:
    """An integer field's declared value, or None. Nothing is evaluated."""
    if node is None:
        return None
    raw = (node.get("eval") or _text_of(node) or "").strip()
    try:
        return int(raw)
    except ValueError:
        return None


def _declared_flag(node) -> bool | None:
    """A boolean field's declared value, or None when it is not declared.

    ``eval`` is read only for the two literals Odoo uses here. Anything else is
    left as None rather than evaluated -- this module executes nothing.
    """
    if node is None:
        return None
    raw = (node.get("eval") or _text_of(node) or "").strip()
    if raw in ("True", "1", "true"):
        return True
    if raw in ("False", "0", "false"):
        return False
    return None


def _referenced_xmlids(node, module: str) -> list[str]:
    """XML IDs named by ``ref('...')`` inside an eval, plus a direct ref.

    Lexical, in document order, duplicates removed. The command tuples around
    the calls -- ``(4, ...)``, ``(6, 0, [...])`` -- are never evaluated, so
    what is recorded is "this expression names these ids", not "these groups
    are implied", which is a runtime question.
    """
    if node is None:
        return []
    found: list[str] = []
    direct = (node.get("ref") or "").strip()
    if direct:
        found.append(_qualify(direct, module))
    for match in _EVAL_REF.finditer(node.get("eval") or ""):
        found.append(_qualify(match.group(1), module))
    seen = set()
    ordered = []
    for item in found:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered
