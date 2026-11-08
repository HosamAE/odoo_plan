"""Build the XML layer: data files, XML IDs, records and references.

The shape follows one rule the milestone insists on: **a definition and a
reference are not the same thing**. An XML ID is a node in its own right; the
element that defines it is a second node; the elements that point at it are
edges. That separation is what lets the graph answer "who defines this?" and
"who depends on this?" without conflating the two, and it is why a reference to
an ID nobody defines is still a first-class fact rather than a hole.

Scope is the manifest. Odoo loads the files a module declares in ``data``,
``demo`` and ``qweb``, in that declared order, and ignores every other XML file
in the tree. So the index follows the manifest rather than the filesystem: an
undeclared XML file is reported and not parsed, because indexing it would
describe records Odoo never creates.

What is deliberately absent: ACL CSV content, record-rule semantics, and any
interpretation of ``groups``. A ``groups`` attribute is recorded as what it
literally is -- a reference to an XML ID -- and what it *means* for access
control belongs to M3.3.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field as dc_field

from .store import (
    DataFileRow,
    GraphEdge,
    NodeDiagnostic,
    XmlIdRow,
    XmlRecordRow,
    stable_id,
)
from .xmlread import XmlNode, XmlReadError, classify_unparsed, read_xml

#: Manifest keys whose entries Odoo loads as data, in the order it loads them.
#: ``init_xml`` and ``update_xml`` are pre-8.0 spellings; they are read so a
#: vendor module using them is not silently skipped.
MANIFEST_SECTIONS = ("data", "demo", "qweb", "init_xml", "update_xml")

#: Elements that define an XML ID through their ``id`` attribute.
DEFINING_TAGS = frozenset({"record", "menuitem", "template", "act_window", "report"})

#: Exactly which attribute on which element is an XML-ID reference.
#:
#: Deliberately a whitelist. ``action`` looks like a reference but on a
#: ``<form>`` inside a QWeb template it is a URL -- testing19 has fourteen of
#: them, and a rule that matched on attribute name alone would report every one
#: as a broken XML ID.
_MENUITEM_REFS = {"parent": "menu_parent", "action": "menu_action"}

#: ``ref(...)`` inside an ``eval`` expression. Only literal single-argument
#: calls are read; anything computed is left alone rather than guessed at.
_EVAL_REF = re.compile(r"\bref\(\s*['\"]([^'\"]+)['\"]\s*\)")

#: An XML ID is ``module.local_id``; a local id may not contain a dot.
_LOCAL_ID = re.compile(r"^[^.]+$")

#: Provenance strings, so a consumer can tell one kind of reference from
#: another without a second column. The vocabulary is closed and documented
#: here rather than assembled ad hoc at each call site.
PROVENANCE_MENU_PARENT = "xml.menuitem@parent"
PROVENANCE_MENU_ACTION = "xml.menuitem@action"
PROVENANCE_INHERIT_ID = "xml.field[inherit_id]@ref"
PROVENANCE_TEMPLATE_INHERIT = "xml.template@inherit_id"
PROVENANCE_EVAL_REF = "xml.eval.ref"
PROVENANCE_GROUPS = "xml.@groups"


@dataclass
class DeclaredFile:
    """One entry from a manifest's data sections, in load order."""

    module: str
    section: str
    load_order: int
    rel_path: str
    #: Absolute path, or None when the manifest names a file that is not there.
    path: str | None
    file_key: str | None


@dataclass
class XmlLayer:
    files: list[DataFileRow] = dc_field(default_factory=list)
    xmlids: list[XmlIdRow] = dc_field(default_factory=list)
    records: list[XmlRecordRow] = dc_field(default_factory=list)
    edges: list[GraphEdge] = dc_field(default_factory=list)
    diagnostics: list[NodeDiagnostic] = dc_field(default_factory=list)
    counts: dict[str, int] = dc_field(default_factory=dict)


@dataclass
class _Definition:
    """One element that defines an XML ID, with everything the winner rule needs."""

    xmlid: str
    record_id: str
    module: str
    load_order: int
    line: int
    rel_path: str
    tag: str


def collect_declared_files(
    module: str, module_dir: str, manifest_data: dict, file_keys: dict[str, str]
) -> tuple[list[DeclaredFile], list[str]]:
    """Read a manifest's data sections into an ordered file list.

    ``load_order`` is a single counter across all sections, so it records the
    order Odoo would load the module's files in rather than the order within
    one key. Problems are returned as messages instead of raised: a malformed
    section must not cost the rest of the module.
    """
    declared: list[DeclaredFile] = []
    problems: list[str] = []
    order = 0
    for section in MANIFEST_SECTIONS:
        value = manifest_data.get(section)
        if value is None:
            continue
        if not isinstance(value, (list, tuple)):
            problems.append(
                f"manifest key {section!r} is {type(value).__name__}, expected a list; "
                "its entries are not indexed"
            )
            continue
        for entry in value:
            if not isinstance(entry, str) or not entry.strip():
                problems.append(
                    f"manifest key {section!r} holds a non-string entry; it is not indexed"
                )
                continue
            rel = entry.strip().replace("\\", "/")
            absolute = os.path.normpath(os.path.join(module_dir, rel.replace("/", os.sep)))
            key = file_keys.get(os.path.normcase(absolute).replace("\\", "/"))
            declared.append(
                DeclaredFile(
                    module=module,
                    section=section,
                    load_order=order,
                    rel_path=rel,
                    path=absolute if os.path.isfile(absolute) else None,
                    file_key=key,
                )
            )
            order += 1
    return declared, problems


def build_xml_layer(
    project_id: str,
    declared: list[DeclaredFile],
    read_bytes,
    known_models: dict[str, str],
    undeclared_xml: dict[str, list[str]],
) -> XmlLayer:
    """Build the whole XML layer.

    ``known_models`` maps a model name to the module that declares its ``_name``,
    which is what lets a reference to Odoo's auto-generated ``model_<name>`` XML
    ID resolve instead of looking broken. ``read_bytes`` is injected so this
    function never opens a file itself.
    """
    layer = XmlLayer()
    definitions: list[_Definition] = []
    references: list[tuple[GraphEdge, str]] = []
    counts = layer.counts

    synthetic = _synthetic_model_ids(known_models)
    counts["synthetic_model_ids"] = len(synthetic)
    model_names = frozenset(known_models)

    for entry in declared:
        _index_one_file(
            project_id, entry, read_bytes, layer, definitions, references,
            model_names, counts,
        )

    _emit_xmlids(project_id, layer, definitions, counts)
    defined = {row.xmlid for row in layer.xmlids}
    _bind_references(layer, references, defined, synthetic, counts)
    _report_undeclared(layer, undeclared_xml, counts)

    counts["data_files"] = len(layer.files)
    counts["xmlids"] = len(layer.xmlids)
    counts["records"] = len(layer.records)
    counts["edges"] = len(layer.edges)
    return layer


# --------------------------------------------------------------------------
# one file
# --------------------------------------------------------------------------


def data_file_id(project_id: str, module: str, rel_path: str) -> str:
    """The node id of a declared data file.

    Shared with the security layer so the two cannot compute it differently
    and end up hanging diagnostics on nodes that do not exist.
    """
    return stable_id(project_id, "data_file", module, rel_path)


def _index_one_file(
    project_id: str,
    entry: DeclaredFile,
    read_bytes,
    layer: XmlLayer,
    definitions: list[_Definition],
    references: list[tuple[GraphEdge, str]],
    model_names: frozenset[str],
    counts,
) -> None:
    kind = _file_kind(entry.rel_path)
    file_id = data_file_id(project_id, entry.module, entry.rel_path)
    row = DataFileRow(
        file_id=file_id,
        module=entry.module,
        section=entry.section,
        load_order=entry.load_order,
        rel_path=entry.rel_path,
        file_key=entry.file_key,
        kind=kind,
        parse_state="not_parsed",
        record_count=0,
    )
    layer.files.append(row)
    counts[f"declared_{kind}"] = counts.get(f"declared_{kind}", 0) + 1

    if entry.path is None:
        row.parse_state = "missing"
        counts["files_missing"] = counts.get("files_missing", 0) + 1
        layer.diagnostics.append(
            NodeDiagnostic(
                "error", "data_file_missing",
                f"{entry.module}: the manifest declares {entry.rel_path!r} but the "
                "file is not on disk; Odoo would fail to install this module",
                "data_file", file_id, entry.rel_path, None,
            )
        )
        return

    if entry.file_key is None:
        # The scanner did not accept it -- an exclusion rule matched. Recording
        # that is better than silently parsing a file the rest of the graph
        # deliberately never saw.
        row.parse_state = "excluded"
        counts["files_excluded"] = counts.get("files_excluded", 0) + 1
        layer.diagnostics.append(
            NodeDiagnostic(
                "info", "data_file_excluded",
                f"{entry.module}: {entry.rel_path!r} is declared in the manifest but "
                "is excluded from indexing; its contents are not in the graph",
                "data_file", file_id, entry.rel_path, None,
            )
        )
        return

    if kind != "xml":
        # CSV and anything else is recorded for its load order only. Reading
        # ACL content is M3.3's job.
        row.parse_state = "not_parsed"
        return

    try:
        source = read_bytes(entry.path)
    except OSError as exc:
        row.parse_state = "unreadable"
        layer.diagnostics.append(
            NodeDiagnostic(
                "error", "data_file_unreadable", f"{entry.rel_path}: {exc}",
                "data_file", file_id, entry.rel_path, None,
            )
        )
        return

    try:
        roots = read_xml(source)
    except XmlReadError as exc:
        # "No element found" is what expat says about a file that is empty or
        # entirely commented out, and it says it before any of them can be told
        # apart. Classifying first is what keeps ordinary housekeeping out of
        # the error bucket: report those at the same severity as broken markup
        # and a reader soon learns to ignore the whole category.
        reason = classify_unparsed(source) if exc.reason == "syntax_error" else None
        if reason in ("empty", "commented_out"):
            _record_no_elements(entry, row, file_id, reason, layer, counts)
            return
        row.parse_state = exc.reason
        counts[f"xml_{exc.reason}"] = counts.get(f"xml_{exc.reason}", 0) + 1
        layer.diagnostics.append(
            NodeDiagnostic(
                "error", f"xml_{exc.reason}",
                f"{entry.rel_path}: {exc.message}",
                "data_file", file_id, entry.rel_path, exc.line,
            )
        )
        return

    if not any(True for root in roots for _ in root.iter()):
        _record_no_elements(
            entry, row, file_id, classify_unparsed(source), layer, counts
        )
        return

    row.parse_state = "ok"
    counts["xml_parsed"] = counts.get("xml_parsed", 0) + 1

    def walk(node: XmlNode, enclosing: str | None) -> None:
        """Descend carrying the nearest enclosing defining record.

        A ``<field ref=...>`` is not itself a record, but the reference it
        makes belongs to the record around it -- that is what makes "which
        records reference this view?" answerable. Walking a flat element list
        would lose that, and every such reference would hang off nothing.
        """
        record_id = _index_element(
            project_id, entry, row, node, layer, definitions, references,
            model_names, enclosing, counts,
        )
        inner = record_id or enclosing
        for child in node.children:
            walk(child, inner)

    for root in roots:
        walk(root, None)


def _record_no_elements(
    entry: DeclaredFile,
    row: DataFileRow,
    file_id: str,
    reason: str,
    layer: XmlLayer,
    counts,
) -> None:
    """A declared file that contributes nothing, and why."""
    row.parse_state = "empty" if reason == "empty" else f"empty_{reason}"
    layer.diagnostics.append(
        NodeDiagnostic(
            "info", "xml_no_elements",
            f"{entry.rel_path}: no elements ({reason}); Odoo loads it and gets nothing",
            "data_file", file_id, entry.rel_path, None,
        )
    )
    counts["xml_no_elements"] = counts.get("xml_no_elements", 0) + 1


def _index_element(
    project_id: str,
    entry: DeclaredFile,
    file_row: DataFileRow,
    node: XmlNode,
    layer: XmlLayer,
    definitions: list[_Definition],
    references: list[tuple[GraphEdge, str]],
    model_names: frozenset[str],
    enclosing_record: str | None,
    counts,
) -> str | None:
    """Index one element. Returns its record id when it defines one."""
    record_id: str | None = None

    if node.tag in DEFINING_TAGS:
        record_id = stable_id(
            project_id, "xml_record", entry.module, entry.rel_path, node.line, node.tag
        )
        raw_id = (node.get("id") or "").strip()
        xmlid = _qualify(raw_id, entry.module) if raw_id else None
        record_model = (node.get("model") or "").strip() or None
        layer.records.append(
            XmlRecordRow(
                record_id=record_id,
                xmlid=xmlid,
                file_id=file_row.file_id,
                module=entry.module,
                rel_path=entry.rel_path,
                line=node.line,
                tag=node.tag,
                record_model=record_model,
                declared_id=raw_id or None,
            )
        )
        file_row.record_count += 1
        counts[f"tag_{node.tag}"] = counts.get(f"tag_{node.tag}", 0) + 1

        if xmlid:
            definitions.append(
                _Definition(
                    xmlid=xmlid, record_id=record_id, module=entry.module,
                    load_order=entry.load_order, line=node.line,
                    rel_path=entry.rel_path, tag=node.tag,
                )
            )
        elif node.tag == "record":
            # Legal in Odoo -- an anonymous record -- but nothing can ever
            # reference it, so it is worth saying out loud.
            layer.diagnostics.append(
                NodeDiagnostic(
                    "info", "record_without_id",
                    f"{entry.rel_path}:{node.line}: <record> has no id and can never "
                    "be referenced or updated",
                    "xml_record", record_id, entry.rel_path, node.line,
                )
            )

        if record_model:
            resolved = record_model in model_names
            layer.edges.append(
                GraphEdge(
                    table="record_instantiates",
                    src_kind="xml_record", src_id=record_id,
                    dst_kind="model",
                    dst_id=record_model if resolved else None,
                    dst_unresolved=record_model,
                    binding="resolved" if resolved else "external",
                    confidence="certain",
                    provenance=f"xml.{node.tag}@model",
                    src_file=entry.rel_path, src_line=node.line,
                )
            )
        elif node.tag == "record":
            layer.diagnostics.append(
                NodeDiagnostic(
                    "warning", "record_without_model",
                    f"{entry.rel_path}:{node.line}: <record> has no model attribute",
                    "xml_record", record_id, entry.rel_path, node.line,
                )
            )

    _collect_references(
        entry, node, record_id or enclosing_record, file_row.file_id,
        layer, references, counts,
    )
    return record_id


def _collect_references(
    entry: DeclaredFile,
    node: XmlNode,
    record_id: str | None,
    file_id: str,
    layer: XmlLayer,
    references: list[tuple[GraphEdge, str]],
    counts,
) -> None:
    """Every XML-ID reference this element makes, by the whitelist above.

    The source is the enclosing record when there is one, and the file
    otherwise -- both are nodes in the graph. Inventing a per-element id for
    the source would leave every such edge, and every diagnostic about it,
    pointing at something that does not exist.
    """
    source_kind = "xml_record" if record_id else "data_file"
    source_id = record_id or file_id

    def add(raw: str, provenance: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        target = _qualify(raw, entry.module)
        counts["references"] = counts.get("references", 0) + 1
        references.append((
            GraphEdge(
                table="xmlid_references",
                src_kind=source_kind,
                src_id=source_id,
                dst_kind="xmlid",
                dst_unresolved=target,
                binding="unresolved",
                confidence="certain",
                provenance=provenance,
                src_file=entry.rel_path,
                src_line=node.line,
            ),
            target,
        ))

    if node.tag == "menuitem":
        for attribute, _kind in _MENUITEM_REFS.items():
            value = node.get(attribute)
            if value:
                add(
                    value,
                    PROVENANCE_MENU_PARENT if attribute == "parent"
                    else PROVENANCE_MENU_ACTION,
                )

    if node.tag == "template" and node.get("inherit_id"):
        add(node.get("inherit_id"), PROVENANCE_TEMPLATE_INHERIT)

    if node.tag == "field":
        ref = node.get("ref")
        if ref:
            name = (node.get("name") or "").strip()
            provenance = (
                PROVENANCE_INHERIT_ID if name == "inherit_id"
                else f"xml.field[{name}]@ref" if name
                else "xml.field@ref"
            )
            add(ref, provenance)
    elif node.tag != "menuitem":
        # `ref` on any other element is still an XML-ID reference in Odoo data
        # files (e.g. <value ref="..."/>), but `action`/`parent` are not.
        ref = node.get("ref")
        if ref:
            add(ref, f"xml.{node.tag}@ref")

    groups = node.get("groups")
    if groups:
        for part in groups.split(","):
            # A leading - or ! negates the group; the reference is the same
            # either way, and what it *means* is M3.3's business.
            add(part.strip().lstrip("-!"), PROVENANCE_GROUPS)

    evaluated = node.get("eval")
    if evaluated:
        for match in _EVAL_REF.finditer(evaluated):
            add(match.group(1), PROVENANCE_EVAL_REF)


# --------------------------------------------------------------------------
# identity, winners, bindings
# --------------------------------------------------------------------------


def _qualify(raw: str, module: str) -> str:
    """``x`` -> ``module.x``; ``other.x`` stays as written."""
    return raw if "." in raw else f"{module}.{raw}"


def _file_kind(rel_path: str) -> str:
    suffix = os.path.splitext(rel_path)[1].lower()
    return {".xml": "xml", ".csv": "csv"}.get(suffix, "other")


def _synthetic_model_ids(known_models: dict[str, set[str]]) -> dict[str, str]:
    """The XML IDs Odoo generates for models, mapped back to the model.

    Odoo creates one ``ir.model`` record per model at install time with the id
    ``<module>.model_<name with dots replaced by underscores>``. Nothing writes
    those ids down, so a graph that only knows about ids it read in a file
    reports every reference to them as broken.

    The map is built **forwards**, from each known model to the id Odoo would
    generate, and matched exactly. Working backwards from an id instead would
    be a guess: ``model_foo_bar`` could be ``foo.bar`` or ``foo_bar``.

    A model may declare ``_name`` in more than one module -- testing19 has one,
    ``hx.conference.booking`` -- so every declaring module contributes a
    candidate. Keeping only one would make the choice arbitrary and leave the
    other module's own references looking broken.
    """
    generated: dict[str, str] = {}
    for model, modules in known_models.items():
        local = f"model_{model.replace('.', '_')}"
        for module in modules:
            if module:
                generated[f"{module}.{local}"] = model
    return generated


def looks_generated(xmlid: str) -> bool:
    """Whether an id has the shape Odoo generates for a model.

    Used only to explain an unresolved reference, never to resolve one: the
    shape alone cannot say which model is meant.
    """
    return xmlid.partition(".")[2].startswith("model_")


def _emit_xmlids(
    project_id: str, layer: XmlLayer, definitions: list[_Definition], counts
) -> None:
    """One node per XML ID, with a deterministic winner among its definitions.

    Odoo applies data files in manifest order and, within a file, in document
    order; a later ``<record id="x">`` overwrites an earlier one. So the winner
    is the **last** definition, ordered by (load position, line).

    When two *different* modules define the same id the real winner depends on
    install order, which this milestone does not model. The rule stays
    deterministic -- module name is the outer sort key -- but the node says
    ``ambiguous_across_modules`` so nobody reads the choice as authoritative.
    """
    by_id: dict[str, list[_Definition]] = {}
    for definition in definitions:
        by_id.setdefault(definition.xmlid, []).append(definition)

    for xmlid in sorted(by_id):
        group = sorted(
            by_id[xmlid], key=lambda d: (d.module, d.load_order, d.line, d.record_id)
        )
        winner = group[-1]
        modules = {d.module for d in group}
        if len(group) == 1:
            rule = "single"
        elif len(modules) == 1:
            rule = "last_in_module"
        else:
            rule = "ambiguous_across_modules"

        owner, _, local = xmlid.partition(".")
        layer.xmlids.append(
            XmlIdRow(
                xmlid=xmlid,
                module=owner,
                local_id=local,
                definition_count=len(group),
                winner_record_id=winner.record_id,
                winner_rule=rule,
            )
        )
        for definition in group:
            layer.edges.append(
                GraphEdge(
                    table="xmlid_defined_by",
                    src_kind="xmlid", src_id=xmlid,
                    dst_kind="xml_record", dst_id=definition.record_id,
                    dst_unresolved=definition.record_id,
                    binding="resolved", confidence="certain",
                    provenance=f"xml.{definition.tag}@id",
                    src_file=definition.rel_path, src_line=definition.line,
                )
            )
        if len(group) > 1:
            sites = ", ".join(f"{d.rel_path}:{d.line}" for d in group)
            layer.diagnostics.append(
                NodeDiagnostic(
                    "warning", "xmlid_defined_more_than_once",
                    f"{xmlid}: defined {len(group)} times ({sites}); "
                    f"{winner.rel_path}:{winner.line} wins by the {rule} rule"
                    + (
                        " -- but the modules load in an order this milestone does "
                        "not model, so treat the winner as provisional"
                        if rule == "ambiguous_across_modules" else ""
                    ),
                    "xmlid", xmlid, winner.rel_path, winner.line,
                )
            )
            counts["xmlids_multiply_defined"] = counts.get("xmlids_multiply_defined", 0) + 1


def _bind_references(
    layer: XmlLayer,
    references: list[tuple[GraphEdge, str]],
    defined: set[str],
    synthetic: dict[str, str],
    counts,
) -> None:
    """Resolve each reference against the ids this graph actually knows.

    Four outcomes, none of them a blank: the id is defined here (resolved), it
    is one Odoo generates from a model (synthetic), it names a module outside
    the project (external), or it names a module inside the project that never
    defines it (unresolved -- the one that is probably a bug).
    """
    project_modules = {row.module for row in layer.files}
    for edge, target in references:
        owner = target.partition(".")[0]
        if target in defined:
            binding, dst = "resolved", target
        elif target in synthetic:
            binding, dst = "synthetic", None
        elif owner not in project_modules:
            binding, dst = "external", None
        else:
            binding, dst = "unresolved", None

        layer.edges.append(
            GraphEdge(
                table=edge.table, src_kind=edge.src_kind, src_id=edge.src_id,
                dst_kind=edge.dst_kind, dst_id=dst, dst_unresolved=edge.dst_unresolved,
                binding=binding, confidence="certain", provenance=edge.provenance,
                src_file=edge.src_file, src_line=edge.src_line,
            )
        )
        counts[f"ref_{binding}"] = counts.get(f"ref_{binding}", 0) + 1
        if binding == "unresolved":
            if looks_generated(target):
                detail = (
                    "it has the shape of an id Odoo generates for a model, but this "
                    "project declares no model in that module whose generated id "
                    "matches; the model most likely lives in Odoo core, which is "
                    "not indexed"
                )
            else:
                detail = (
                    f"module {owner!r} is in this project and never defines it"
                )
            layer.diagnostics.append(
                NodeDiagnostic(
                    "warning", "xmlid_reference_unresolved",
                    f"{edge.src_file}:{edge.src_line}: references {target!r} via "
                    f"{edge.provenance}, but {detail}",
                    edge.src_kind, edge.src_id, edge.src_file, edge.src_line,
                )
            )


def _report_undeclared(layer: XmlLayer, undeclared: dict[str, list[str]], counts) -> None:
    """XML files present in a module but named by no manifest section.

    Odoo will not load them, so they are not indexed. Most are OWL templates
    under ``static/``, which are assets rather than data; the rest are usually
    dead files. Either way the graph should say it looked and chose not to.
    """
    total = 0
    for module in sorted(undeclared):
        for rel in sorted(undeclared[module]):
            total += 1
            layer.diagnostics.append(
                NodeDiagnostic(
                    "info", "xml_not_declared_in_manifest",
                    f"{module}: {rel} is an XML file the manifest does not declare; "
                    "Odoo does not load it and it is not indexed",
                    "module", module, f"{module}/{rel}", None,
                )
            )
    counts["xml_undeclared"] = total
