"""Build the frontend layer: asset bundles, JS, OWL, and HTTP routes.

Four things Odoo keeps in four different places, joined here because a question
about the frontend almost always crosses them: a manifest declares that a file
belongs to a bundle, the file defines a component, the component names a
template that lives in a different file, and the whole thing talks to a route
declared in Python.

Scope, and how it is decided:

* **Assets follow the manifest.** A file is in a bundle because a manifest says
  so, including through a glob, which is expanded against the files the scanner
  actually found rather than guessed at.
* **OWL templates come in through the bundles.** They live under ``static/``
  and are therefore invisible to the XML layer, which follows ``data``. M3.2
  reported fifty-two of them as "declared by no manifest section"; this layer
  picks up exactly the ones a bundle names, which is the set Odoo loads.
* **Routes come from Python decorators**, read by AST.

Confidence is calibrated per source, and the difference matters:

* ``certain`` -- manifest entries, route declarations, ``t-name``/``t-inherit``.
  These come from a real parser over a real grammar.
* ``heuristic`` -- everything read from JavaScript. :mod:`odoo_kg.jsread` is a
  lexer, not a parser; it cannot see scope and does not know a grammar. The
  shapes it matches are unambiguous in practice, and saying so is what stops a
  reader from treating them as parsed facts.

Cross-layer links are only recorded when the target is something this graph
already knows: a JS string becomes a route reference when it matches a route
path exactly, and a model reference when it matches an indexed model name. A
string that looks plausible but matches nothing is not a link, and no link is
ever invented from a shape alone.

Nothing is executed. No JavaScript runs, no Node is invoked, nothing is
installed, and no network call is made.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field as dc_field

from .jsextract import extract
from .jsread import JsReadError, read_js
from .routeread import parse_controllers
from .store import (
    AssetBundleRow, AssetEntryRow, GraphEdge, JsFileRow, NodeDiagnostic,
    OwlComponentRow, OwlTemplateRow, RouteRow, stable_id,
)
from .xmlread import XmlReadError, read_xml

#: Extensions this layer reads as JavaScript. TypeScript is included because a
#: project may adopt it; testing19 currently has none, and the tests cover the
#: extension handling so the first .ts file is not a surprise.
JS_SUFFIXES = (".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx")

#: Asset operations Odoo understands in a manifest bundle entry.
ASSET_OPERATIONS = frozenset({"before", "after", "replace", "remove", "include"})

#: A model name as written in JS: dotted, lower case. Used only to *look up*
#: an indexed model, never to decide that something is one.
_MODEL_SHAPE = re.compile(r"^[a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)+$")


@dataclass
class FrontendLayer:
    bundles: list[AssetBundleRow] = dc_field(default_factory=list)
    entries: list[AssetEntryRow] = dc_field(default_factory=list)
    js_files: list[JsFileRow] = dc_field(default_factory=list)
    components: list[OwlComponentRow] = dc_field(default_factory=list)
    templates: list[OwlTemplateRow] = dc_field(default_factory=list)
    routes: list[RouteRow] = dc_field(default_factory=list)
    edges: list[GraphEdge] = dc_field(default_factory=list)
    diagnostics: list[NodeDiagnostic] = dc_field(default_factory=list)
    counts: dict[str, int] = dc_field(default_factory=dict)


@dataclass
class ScannedSource:
    """One file the scanner accepted, addressed the way a manifest would."""

    module: str
    rel_path: str
    addon_path: str
    file_key: str
    disk_path: str


def build_frontend_layer(
    project_id: str,
    manifests,
    sources: list[ScannedSource],
    read_bytes,
    known_models: set[str],
) -> FrontendLayer:
    """Build the whole frontend layer.

    ``manifests`` is an iterable of (module, manifest rel_path, manifest data,
    line). ``sources`` is every file the M1 scanner accepted, so this layer can
    never reach a file the exclusion rules kept out.
    """
    layer = FrontendLayer()
    counts = layer.counts
    by_addon_path = {source.addon_path: source for source in sources}

    _index_assets(project_id, manifests, by_addon_path, layer, counts)
    # Literals collected during the JS pass, consumed once every node exists.
    # Threaded rather than stashed in a module global: two builds in one
    # process must not be able to see each other's data.
    pending: dict[str, tuple] = {}
    js_by_addon = _index_js(project_id, sources, read_bytes, layer, pending, counts)
    _index_templates(project_id, sources, read_bytes, layer, counts)
    _index_routes(project_id, sources, read_bytes, layer, counts)

    _link_bundle_contents(layer, by_addon_path, js_by_addon, counts)
    _link_components_and_templates(layer, counts)
    _link_js_cross_references(layer, pending, known_models, counts)

    counts["bundles"] = len(layer.bundles)
    counts["asset_entries"] = len(layer.entries)
    counts["js_files"] = len(layer.js_files)
    counts["components"] = len(layer.components)
    counts["templates"] = len(layer.templates)
    counts["routes"] = len(layer.routes)
    counts["edges"] = len(layer.edges)
    return layer


# --------------------------------------------------------------------------
# assets
# --------------------------------------------------------------------------


def _index_assets(project_id, manifests, by_addon_path, layer, counts) -> None:
    seen_bundles: dict[str, int] = {}
    for module, manifest_rel, data, manifest_line in manifests:
        assets = data.get("assets")
        if assets is None:
            continue
        if not isinstance(assets, dict):
            layer.diagnostics.append(
                NodeDiagnostic(
                    "warning", "assets_not_a_mapping",
                    f"{module}: manifest 'assets' is {type(assets).__name__}, "
                    "expected a mapping of bundle to entries; nothing is indexed",
                    "module", module, manifest_rel, manifest_line,
                )
            )
            continue
        for bundle, entries in assets.items():
            if not isinstance(bundle, str) or not bundle.strip():
                counts["asset_bundle_not_a_string"] = counts.get(
                    "asset_bundle_not_a_string", 0) + 1
                continue
            bundle = bundle.strip()
            seen_bundles.setdefault(bundle, 0)
            if not isinstance(entries, (list, tuple)):
                layer.diagnostics.append(
                    NodeDiagnostic(
                        "warning", "asset_entries_not_a_list",
                        f"{module}: bundle {bundle!r} holds "
                        f"{type(entries).__name__}, expected a list",
                        "module", module, manifest_rel, manifest_line,
                    )
                )
                continue
            for position, entry in enumerate(entries):
                row = _read_asset_entry(
                    project_id, module, manifest_rel, manifest_line, bundle,
                    position, entry, layer,
                )
                if row is not None:
                    layer.entries.append(row)
                    seen_bundles[bundle] += 1

    for bundle in sorted(seen_bundles):
        layer.bundles.append(
            AssetBundleRow(bundle_name=bundle, entry_count=seen_bundles[bundle])
        )


def _read_asset_entry(
    project_id, module, manifest_rel, manifest_line, bundle, position, entry, layer
):
    entry_id = stable_id(project_id, "asset_entry", module, bundle, position)

    def row(operation, target, extra=None, is_glob=False):
        return AssetEntryRow(
            entry_id=entry_id, module=module, bundle=bundle, position=position,
            operation=operation, target=target, extra_target=extra,
            is_glob=is_glob, rel_path=manifest_rel, line=manifest_line,
        )

    if isinstance(entry, str):
        target = entry.strip()
        if not target:
            return None
        return row("plain", target, is_glob=_looks_like_glob(target))

    if isinstance(entry, (list, tuple)) and entry:
        operation = str(entry[0]).strip()
        if operation not in ASSET_OPERATIONS:
            layer.diagnostics.append(
                NodeDiagnostic(
                    "warning", "asset_operation_unknown",
                    f"{module}: bundle {bundle!r} entry {position} uses operation "
                    f"{operation!r}, which this build does not recognise; the entry "
                    "is recorded but its effect is not interpreted",
                    "asset_entry", entry_id, manifest_rel, manifest_line,
                )
            )
        parts = [str(x).strip() for x in entry[1:] if isinstance(x, str)]
        target = parts[0] if parts else ""
        extra = parts[1] if len(parts) > 1 else None
        if not target:
            layer.diagnostics.append(
                NodeDiagnostic(
                    "warning", "asset_operation_without_target",
                    f"{module}: bundle {bundle!r} entry {position} is an "
                    f"{operation!r} operation with no literal target",
                    "asset_entry", entry_id, manifest_rel, manifest_line,
                )
            )
            return None
        return row(operation, target, extra, _looks_like_glob(target))

    layer.diagnostics.append(
        NodeDiagnostic(
            "warning", "asset_entry_unknown_shape",
            f"{module}: bundle {bundle!r} entry {position} is "
            f"{type(entry).__name__}, expected a string or an operation tuple",
            "asset_entry", entry_id, manifest_rel, manifest_line,
        )
    )
    return None


#: Wildcards this build implements. Character classes are **not** among them.
#:
#: Odoo's asset globs use ``*`` within a segment and ``**`` across segments, and
#: that is what is supported here. ``[abc]`` was previously counted as making a
#: pattern a glob while the translation escaped the brackets as literal
#: characters, so ``foo[ab].js`` compiled to a regex matching a file named
#: exactly ``foo[ab].js`` -- a file that does not exist. The pattern therefore
#: matched nothing and was reported as "names no file", which blames the
#: manifest for a syntax this build never implemented.
#:
#: The semantics are now stated rather than half-implemented: ``*`` and ``?``
#: are operators, a bracket is not, and a pattern containing one is reported as
#: using unsupported syntax instead of quietly matching nothing.
GLOB_OPERATORS = "*?"

#: Characters that mean something in a glob dialect this build does not
#: implement. Their presence is reported, never guessed at.
UNSUPPORTED_GLOB_CHARACTERS = "[]"


def _looks_like_glob(target: str) -> bool:
    return any(character in target for character in GLOB_OPERATORS)


def _unsupported_glob_syntax(target: str) -> str:
    """Which unsupported glob characters a pattern uses, if any."""
    return "".join(
        sorted({c for c in target if c in UNSUPPORTED_GLOB_CHARACTERS})
    )


def _expand(pattern: str, addon_paths) -> list[str]:
    """Every scanned file an asset pattern names.

    Odoo's asset globs use ``*`` within a segment and ``**`` across segments.
    Translating to a regex keeps the two apart, which ``fnmatch`` does not: it
    lets ``*`` cross a slash and would pull in far more than Odoo would.

    Matching is case-insensitive. The scanner's keys are folded, Windows does
    not distinguish case, and a manifest writing ``KanbanController.js`` for a
    file the graph holds as ``kanbancontroller.js`` is the same file -- treating
    it as missing would report two working assets as broken.
    """
    folded = pattern.lower()
    if not _looks_like_glob(pattern):
        return [folded] if folded in addon_paths else []
    regex = _glob_to_regex(folded)
    return sorted(path for path in addon_paths if regex.match(path))


#: The wildcard pieces, as tokens rather than as regex fragments.
#:
#: Emitting regex text directly meant adjacent wildcards concatenated into
#: chains: ``****`` became ``.*.*`` and ``**/**/**/x`` became three nested
#: optional groups in a row. Every one of those is a pattern the regex engine
#: can backtrack through exponentially, for input that adds no meaning -- two
#: adjacent "match anything" wildcards say exactly what one says.
#:
#: Tokens can be compared and collapsed; regex strings cannot, safely.
_ANY = "any"            # .*          crosses separators
_ANY_DIR = "anydir"     # (?:.*/)?    zero or more whole directory segments
_SEGMENT = "segment"    # [^/]*       anything within one segment
_ONE = "one"            # [^/]        exactly one character, not a separator

_TOKEN_REGEX = {
    _ANY: r".*",
    _ANY_DIR: r"(?:.*/)?",
    _SEGMENT: r"[^/]*",
    _ONE: r"[^/]",
}

#: (first, second) -> what the pair means when written once.
#:
#: Only pairs that are genuinely equivalent are listed. ``(?:.*/)?[^/]*`` is
#: *not* collapsed to ``.*``: they differ on a string ending in a separator,
#: and a rule that is right for the inputs we happen to have is the kind of
#: rule that is wrong later.
_COLLAPSE = {
    (_ANY, _ANY): _ANY,
    (_ANY, _ANY_DIR): _ANY,
    (_ANY_DIR, _ANY): _ANY,
    (_ANY, _SEGMENT): _ANY,
    (_SEGMENT, _ANY): _ANY,
    (_ANY_DIR, _ANY_DIR): _ANY_DIR,
    (_SEGMENT, _SEGMENT): _SEGMENT,
}


def _glob_tokens(pattern: str) -> list:
    """The pattern as a token list: wildcard markers and literal characters."""
    tokens: list = []
    index = 0
    size = len(pattern)
    while index < size:
        char = pattern[index]
        if pattern.startswith("**/", index):
            tokens.append(_ANY_DIR)
            index += 3
        elif pattern.startswith("**", index):
            tokens.append(_ANY)
            index += 2
        elif char == "*":
            tokens.append(_SEGMENT)
            index += 1
        elif char == "?":
            tokens.append(_ONE)
            index += 1
        else:
            tokens.append(("literal", char))
            index += 1
    return tokens


def _collapse_wildcards(tokens: list) -> list:
    """Fold adjacent wildcards that say the same thing as one.

    A single pass is not enough: collapsing ``ANY_DIR ANY`` into ``ANY`` can
    put that ``ANY`` next to another one, so this runs until nothing changes.
    It terminates because every collapse removes a token.

    Valid Odoo asset patterns -- ``module/static/src/**/*.js`` and the like --
    have no adjacent wildcards at all, so this leaves them exactly as they
    were. Only the pathological shapes change.
    """
    while True:
        for position in range(len(tokens) - 1):
            pair = (tokens[position], tokens[position + 1])
            folded = _COLLAPSE.get(pair)
            if folded is not None:
                tokens[position:position + 2] = [folded]
                break
        else:
            return tokens


#: How many wildcard groups a pattern may still contain after normalisation.
#:
#: Folding removes the wildcards that were redundant. What it cannot remove is
#: a pattern whose wildcards each mean something and which is still expensive:
#: ``**/*/**/*/...`` requires one more directory per repetition, so it is not
#: reducible, and the alternation of "any depth" with "exactly one segment" is
#: precisely the shape a backtracking engine explodes on.
#:
#: Twelve is far past anything a manifest contains -- the deepest real pattern
#: in testing19 has two -- and a pattern beyond it is refused out loud rather
#: than either matched slowly or dropped quietly.
MAX_GLOB_WILDCARDS = 12


class GlobTooComplexError(ValueError):
    """An asset pattern with more wildcard groups than this build will evaluate."""


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Compile an Odoo asset glob, with adjacent wildcards normalised first.

    ``*`` stays inside a segment and ``**`` crosses separators, which is the
    distinction ``fnmatch`` does not make and the reason this exists.
    """
    tokens = _collapse_wildcards(_glob_tokens(pattern))
    wildcards = sum(1 for token in tokens if not isinstance(token, tuple))
    if wildcards > MAX_GLOB_WILDCARDS:
        raise GlobTooComplexError(
            f"{pattern!r} still has {wildcards} wildcard groups after "
            f"normalisation, more than the {MAX_GLOB_WILDCARDS} this build "
            f"evaluates. Deciding it could take unbounded time, so it is "
            f"reported rather than matched."
        )
    out = [
        re.escape(token[1]) if isinstance(token, tuple) else _TOKEN_REGEX[token]
        for token in tokens
    ]
    return re.compile("^" + "".join(out) + "$")


# --------------------------------------------------------------------------
# JavaScript
# --------------------------------------------------------------------------


def _index_js(project_id, sources, read_bytes, layer, pending, counts) -> dict[str, str]:
    """One node per JS/TS file, plus its components and what it references."""
    js_by_addon: dict[str, str] = {}
    for source in sources:
        if not source.rel_path.lower().endswith(JS_SUFFIXES):
            continue
        js_id = stable_id(project_id, "js_file", source.file_key)
        js_by_addon[source.addon_path] = js_id
        try:
            raw = read_bytes(source.disk_path)
        except OSError as exc:
            layer.diagnostics.append(
                NodeDiagnostic(
                    "error", "js_unreadable", f"{source.rel_path}: {exc}",
                    "js_file", js_id, source.rel_path, None,
                )
            )
            continue
        try:
            tokens = read_js(raw)
        except JsReadError as exc:
            counts["js_undecodable"] = counts.get("js_undecodable", 0) + 1
            layer.js_files.append(
                JsFileRow(
                    js_id=js_id, module=source.module, rel_path=source.rel_path,
                    file_key=source.file_key, addon_path=source.addon_path,
                    odoo_module=False, lex_state="undecodable",
                    token_count=0, class_count=0, import_count=0,
                )
            )
            layer.diagnostics.append(
                NodeDiagnostic(
                    "error", "js_undecodable", f"{source.rel_path}: {exc}",
                    "js_file", js_id, source.rel_path, None,
                )
            )
            continue

        head = raw[:512].decode("utf-8", "replace")
        facts = extract(tokens, head)
        layer.js_files.append(
            JsFileRow(
                js_id=js_id, module=source.module, rel_path=source.rel_path,
                file_key=source.file_key, addon_path=source.addon_path,
                odoo_module=facts.module_annotation, lex_state="ok",
                token_count=len(tokens), class_count=len(facts.classes),
                import_count=len(facts.imports),
            )
        )
        counts["js_parsed"] = counts.get("js_parsed", 0) + 1
        _record_js_facts(project_id, source, js_id, facts, layer, pending, counts)
    return js_by_addon


def _record_js_facts(project_id, source, js_id, facts, layer, pending, counts) -> None:
    for specifier, line in facts.imports:
        # A bare or scoped specifier is an Odoo/npm module path, not a file in
        # this project. Relative ones are resolved later against the scanned
        # files, once every JS node exists.
        layer.edges.append(
            GraphEdge(
                table="js_imports", src_kind="js_file", src_id=js_id,
                dst_kind="js_file", dst_id=None, dst_unresolved=specifier,
                binding="unresolved" if specifier.startswith(".") else "external",
                confidence="heuristic", provenance="js.import",
                src_file=source.rel_path, src_line=line,
            )
        )

    for js_class in facts.classes:
        if not js_class.is_component:
            continue
        component_id = stable_id(
            project_id, "owl_component", source.file_key, js_class.name, js_class.line
        )
        layer.components.append(
            OwlComponentRow(
                component_id=component_id, js_id=js_id, module=source.module,
                class_name=js_class.name, base=js_class.base or "",
                template_name=js_class.template, rel_path=source.rel_path,
                line=js_class.line,
            )
        )
        if js_class.template is None:
            layer.diagnostics.append(
                NodeDiagnostic(
                    "info", "owl_component_without_static_template",
                    f"{source.rel_path}:{js_class.line}: {js_class.name} extends "
                    f"{js_class.base} but declares no literal static template; the "
                    "template it renders is not statically known",
                    "owl_component", component_id, source.rel_path, js_class.line,
                )
            )

    for registration in facts.registrations:
        target = (
            f"{registration.category}:{registration.key}"
            if registration.key else f"{registration.category}:*"
        )
        layer.edges.append(
            GraphEdge(
                table="js_registers", src_kind="js_file", src_id=js_id,
                dst_kind="registry", dst_id=None, dst_unresolved=target,
                binding="external", confidence="heuristic",
                provenance="js.registry.add" if registration.key
                else "js.registry.category",
                src_file=source.rel_path, src_line=registration.line,
            )
        )
        if registration.key is None:
            counts["registry_without_literal_key"] = counts.get(
                "registry_without_literal_key", 0) + 1

    for target, line in facts.patches:
        layer.edges.append(
            GraphEdge(
                table="js_patches", src_kind="js_file", src_id=js_id,
                dst_kind="symbol", dst_id=None, dst_unresolved=target,
                binding="external", confidence="heuristic",
                provenance="js.patch", src_file=source.rel_path, src_line=line,
            )
        )

    for name, line in facts.services:
        layer.edges.append(
            GraphEdge(
                table="js_uses_service", src_kind="js_file", src_id=js_id,
                dst_kind="service", dst_id=None, dst_unresolved=name,
                binding="external", confidence="heuristic",
                provenance="js.useService", src_file=source.rel_path, src_line=line,
            )
        )

    # Kept for the cross-reference pass, which needs every node to exist first.
    pending[js_id] = (source, facts.strings)


# --------------------------------------------------------------------------
# OWL templates
# --------------------------------------------------------------------------


def _index_templates(project_id, sources, read_bytes, layer, counts) -> None:
    """``t-name`` templates in XML files under a module's static tree.

    Scoped to files an asset bundle names, because that is the set Odoo loads.
    A template in an XML file no bundle mentions is dead, and indexing it would
    describe components that never render.
    """
    named_by_assets = {
        path
        for entry in layer.entries
        for path in _entry_paths(entry)
    }
    addon_paths = {source.addon_path for source in sources}
    reachable = set()
    for pattern in named_by_assets:
        try:
            reachable.update(_expand(pattern, addon_paths))
        except GlobTooComplexError:
            # Reported where the entry is indexed; nothing to add here.
            continue

    seen: dict[str, OwlTemplateRow] = {}
    for source in sources:
        if not source.rel_path.lower().endswith(".xml"):
            continue
        if source.addon_path not in reachable:
            continue
        try:
            raw = read_bytes(source.disk_path)
        except OSError:
            continue
        try:
            roots = read_xml(raw)
        except XmlReadError as exc:
            counts["template_xml_unreadable"] = counts.get(
                "template_xml_unreadable", 0) + 1
            layer.diagnostics.append(
                NodeDiagnostic(
                    "error", "owl_template_xml_unreadable",
                    f"{source.rel_path}: {exc.message}",
                    "module", source.module, source.rel_path, exc.line,
                )
            )
            continue
        counts["template_files"] = counts.get("template_files", 0) + 1
        for root in roots:
            for node in root.iter():
                name = (node.get("t-name") or "").strip()
                if not name:
                    continue
                row = OwlTemplateRow(
                    template_name=name, module=source.module,
                    rel_path=source.rel_path, line=node.line,
                    inherits=(node.get("t-inherit") or "").strip() or None,
                    primary=(node.get("t-inherit-mode") or "").strip() or None,
                )
                if name in seen:
                    layer.diagnostics.append(
                        NodeDiagnostic(
                            "warning", "owl_template_defined_more_than_once",
                            f"{name}: also defined at {seen[name].rel_path}:"
                            f"{seen[name].line}; the later definition wins at load "
                            "time and the order depends on bundle order",
                            "owl_template", name, source.rel_path, node.line,
                        )
                    )
                seen[name] = row

    for name in sorted(seen):
        layer.templates.append(seen[name])


def _entry_paths(entry) -> list[str]:
    """The file patterns an asset entry names. ``include`` names a bundle."""
    if entry.operation == "include":
        return []
    paths = [entry.target]
    if entry.extra_target:
        paths.append(entry.extra_target)
    return [p for p in paths if p]


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------


def _index_routes(project_id, sources, read_bytes, layer, counts) -> None:
    for source in sources:
        if not source.rel_path.lower().endswith(".py"):
            continue
        if os.path.basename(source.rel_path).lower() == "__manifest__.py":
            continue
        try:
            raw = read_bytes(source.disk_path)
        except OSError:
            continue
        parsed = parse_controllers(raw)
        for severity, category, message, line in parsed.findings:
            layer.diagnostics.append(
                NodeDiagnostic(
                    severity, category, f"{source.rel_path}: {message}",
                    "module", source.module, source.rel_path, line,
                )
            )
        if parsed.controllers:
            counts["controller_classes"] = counts.get("controller_classes", 0) + len(
                parsed.controllers
            )
        for route in parsed.routes:
            counts["route_declarations"] = counts.get("route_declarations", 0) + 1
            # One node per declared path: a decorator may name several, and
            # each is a distinct entry point with its own URL.
            paths = route.paths or ("",)
            for path in paths:
                route_id = stable_id(
                    project_id, "route", source.file_key, route.controller,
                    route.method, path, route.decorator_line,
                )
                layer.routes.append(
                    RouteRow(
                        route_id=route_id, path=path or None, module=source.module,
                        controller=route.controller, method=route.method,
                        base=route.base, auth=route.auth, route_type=route.route_type,
                        methods=", ".join(route.methods) or None,
                        website=route.website, csrf=route.csrf,
                        sitemap=route.sitemap,
                        has_dynamic_argument=route.has_dynamic_argument,
                        rel_path=source.rel_path, line=route.line,
                    )
                )
                if path:
                    counts["route_paths"] = counts.get("route_paths", 0) + 1


# --------------------------------------------------------------------------
# links
# --------------------------------------------------------------------------


def _link_bundle_contents(layer, by_addon_path, js_by_addon, counts) -> None:
    """Bind every asset entry to the bundle and the files it names."""
    known_bundles = {row.bundle_name for row in layer.bundles}
    addon_paths = set(by_addon_path)

    for entry in layer.entries:
        layer.edges.append(
            GraphEdge(
                table="asset_in_bundle", src_kind="asset_entry", src_id=entry.entry_id,
                dst_kind="asset_bundle", dst_id=entry.bundle,
                dst_unresolved=entry.bundle, binding="resolved",
                confidence="certain", provenance=f"manifest.assets[{entry.bundle}]",
                src_file=entry.rel_path, src_line=entry.line,
            )
        )

        if entry.operation == "include":
            target = entry.target
            resolved = target in known_bundles
            layer.edges.append(
                GraphEdge(
                    table="asset_includes_file", src_kind="asset_entry",
                    src_id=entry.entry_id, dst_kind="asset_bundle",
                    dst_id=target if resolved else None, dst_unresolved=target,
                    binding="resolved" if resolved else "external",
                    confidence="certain", provenance="manifest.assets.include",
                    src_file=entry.rel_path, src_line=entry.line,
                )
            )
            continue

        for pattern in _entry_paths(entry):
            unsupported = _unsupported_glob_syntax(pattern)
            if unsupported:
                # Named for what it is. A character class is a glob feature
                # this build does not implement, and reporting it as "matches
                # no file" would blame the manifest for our gap.
                layer.edges.append(
                    GraphEdge(
                        table="asset_includes_file", src_kind="asset_entry",
                        src_id=entry.entry_id, dst_kind="file", dst_id=None,
                        dst_unresolved=f"unknown:unsupported_pattern_syntax:{pattern}",
                        binding="unresolved", confidence="certain",
                        provenance=f"manifest.assets.{entry.operation}",
                        src_file=entry.rel_path, src_line=entry.line,
                    )
                )
                counts["asset_pattern_unsupported_syntax"] = counts.get(
                    "asset_pattern_unsupported_syntax", 0) + 1
                layer.diagnostics.append(
                    NodeDiagnostic(
                        "warning", "asset_pattern_unsupported_syntax",
                        f"{entry.module}: bundle {entry.bundle!r} names "
                        f"{pattern!r}, which uses {unsupported!r}. This build "
                        f"implements only {GLOB_OPERATORS!r} in asset "
                        f"patterns, so the entry is recorded as unresolved "
                        f"rather than matched or reported as missing.",
                        "asset_entry", entry.entry_id, entry.rel_path, entry.line,
                    )
                )
                continue
            try:
                matches = _expand(pattern, addon_paths)
            except GlobTooComplexError as exc:
                # Refused, not silently unmatched. A pattern this build will
                # not evaluate must never look like a pattern that named
                # nothing -- the first is our limit, the second is the
                # manifest's mistake.
                layer.edges.append(
                    GraphEdge(
                        table="asset_includes_file", src_kind="asset_entry",
                        src_id=entry.entry_id, dst_kind="file", dst_id=None,
                        dst_unresolved=f"unknown:pattern_too_complex:{pattern}",
                        binding="unresolved", confidence="certain",
                        provenance=f"manifest.assets.{entry.operation}",
                        src_file=entry.rel_path, src_line=entry.line,
                    )
                )
                counts["asset_pattern_too_complex"] = counts.get(
                    "asset_pattern_too_complex", 0) + 1
                layer.diagnostics.append(
                    NodeDiagnostic(
                        "warning", "asset_pattern_too_complex",
                        f"{entry.module}: bundle {entry.bundle!r} names a "
                        f"pattern this build refuses to evaluate: {exc}",
                        "asset_entry", entry.entry_id, entry.rel_path, entry.line,
                    )
                )
                continue
            if not matches:
                # A path Odoo would resolve against another addon, or one that
                # names nothing. Either way no file in this project matches, and
                # inventing one would be worse than saying so.
                layer.edges.append(
                    GraphEdge(
                        table="asset_includes_file", src_kind="asset_entry",
                        src_id=entry.entry_id, dst_kind="file", dst_id=None,
                        dst_unresolved=pattern,
                        binding="external" if _outside_project(pattern, addon_paths)
                        else "unresolved",
                        confidence="certain", provenance=f"manifest.assets.{entry.operation}",
                        src_file=entry.rel_path, src_line=entry.line,
                    )
                )
                counts["asset_target_unmatched"] = counts.get(
                    "asset_target_unmatched", 0) + 1
                if not _outside_project(pattern, addon_paths):
                    layer.diagnostics.append(
                        NodeDiagnostic(
                            "warning", "asset_target_matches_nothing",
                            f"{entry.module}: bundle {entry.bundle!r} names "
                            f"{pattern!r}, which matches no file this build scanned",
                            "asset_entry", entry.entry_id, entry.rel_path, entry.line,
                        )
                    )
                continue
            for match in matches:
                js_id = js_by_addon.get(match)
                scanned = by_addon_path.get(match)
                # A matched file is in the graph either way: as a JS node when
                # it is JavaScript, and as the M1 file node otherwise. Calling
                # a stylesheet that is right there "external" would be wrong.
                if js_id:
                    dst_kind, dst_id = "js_file", js_id
                elif scanned is not None:
                    dst_kind, dst_id = "file", scanned.file_key
                else:
                    dst_kind, dst_id = "file", None
                layer.edges.append(
                    GraphEdge(
                        table="asset_includes_file", src_kind="asset_entry",
                        src_id=entry.entry_id, dst_kind=dst_kind, dst_id=dst_id,
                        dst_unresolved=match,
                        binding="resolved" if dst_id else "external",
                        confidence="certain",
                        provenance=f"manifest.assets.{entry.operation}"
                        + ("[glob]" if entry.is_glob else ""),
                        src_file=entry.rel_path, src_line=entry.line,
                    )
                )
                counts["asset_target_matched"] = counts.get(
                    "asset_target_matched", 0) + 1


def _outside_project(pattern: str, addon_paths) -> bool:
    """Whether a pattern names a module this project does not contain."""
    module = pattern.split("/", 1)[0].lower()
    return not any(path.startswith(module + "/") for path in addon_paths)


def _link_components_and_templates(layer, counts) -> None:
    known = {row.template_name for row in layer.templates}
    modules = {row.module for row in layer.templates} | {
        row.module for row in layer.js_files
    }

    for component in layer.components:
        if not component.template_name:
            continue
        name = component.template_name
        resolved = name in known
        owner = name.split(".", 1)[0]
        binding = (
            "resolved" if resolved
            else "external" if owner not in modules
            else "unresolved"
        )
        layer.edges.append(
            GraphEdge(
                table="component_uses_template", src_kind="owl_component",
                src_id=component.component_id, dst_kind="owl_template",
                dst_id=name if resolved else None, dst_unresolved=name,
                binding=binding, confidence="heuristic",
                provenance="js.static.template", src_file=component.rel_path,
                src_line=component.line,
            )
        )
        counts[f"component_template_{binding}"] = counts.get(
            f"component_template_{binding}", 0) + 1
        if binding == "unresolved":
            layer.diagnostics.append(
                NodeDiagnostic(
                    "warning", "owl_template_unresolved",
                    f"{component.rel_path}:{component.line}: {component.class_name} "
                    f"names template {name!r}, and module {owner!r} is in this "
                    "project but no bundle-reachable file defines it",
                    "owl_component", component.component_id, component.rel_path,
                    component.line,
                )
            )

    for template in layer.templates:
        if not template.inherits:
            continue
        name = template.inherits
        resolved = name in known
        owner = name.split(".", 1)[0]
        binding = (
            "resolved" if resolved
            else "external" if owner not in modules
            else "unresolved"
        )
        layer.edges.append(
            GraphEdge(
                table="template_inherits", src_kind="owl_template",
                src_id=template.template_name, dst_kind="owl_template",
                dst_id=name if resolved else None, dst_unresolved=name,
                binding=binding, confidence="certain",
                provenance="xml.template@t-inherit", src_file=template.rel_path,
                src_line=template.line,
            )
        )
        counts[f"template_inherit_{binding}"] = counts.get(
            f"template_inherit_{binding}", 0) + 1


def _link_js_cross_references(layer, pending, known_models, counts):
    """Links from JS to things this graph already knows.

    Only exact matches count. A literal becomes a route reference when it *is*
    a declared route path, and a model reference when it *is* an indexed model
    name. A string that merely looks like either is not a link: the whole value
    of these edges is that the far end was verified to exist.
    """
    routes_by_path = {}
    for route in layer.routes:
        if route.path:
            routes_by_path.setdefault(route.path, route.route_id)

    by_js = {row.js_id: row for row in layer.js_files}
    relative_targets = {row.addon_path: row.js_id for row in layer.js_files}

    for js_id, (source, strings) in sorted(pending.items()):
        if js_id not in by_js:
            continue
        seen_routes: set[str] = set()
        seen_models: set[str] = set()
        for literal, line in strings:
            value = literal.strip()
            if not value:
                continue
            if value in routes_by_path and value not in seen_routes:
                seen_routes.add(value)
                layer.edges.append(
                    GraphEdge(
                        table="js_calls_route", src_kind="js_file", src_id=js_id,
                        dst_kind="route", dst_id=routes_by_path[value],
                        dst_unresolved=value, binding="resolved",
                        confidence="heuristic", provenance="js.string.route_path",
                        src_file=source.rel_path, src_line=line,
                    )
                )
                counts["js_route_links"] = counts.get("js_route_links", 0) + 1
            elif (
                value in known_models
                and _MODEL_SHAPE.match(value)
                and value not in seen_models
            ):
                seen_models.add(value)
                layer.edges.append(
                    GraphEdge(
                        table="js_references_model", src_kind="js_file",
                        src_id=js_id, dst_kind="model", dst_id=value,
                        dst_unresolved=value, binding="resolved",
                        confidence="heuristic", provenance="js.string.model_name",
                        src_file=source.rel_path, src_line=line,
                    )
                )
                counts["js_model_links"] = counts.get("js_model_links", 0) + 1

    # Relative imports, now that every JS node exists.
    resolved_imports = 0
    for edge in list(layer.edges):
        if edge.table != "js_imports" or edge.binding != "unresolved":
            continue
        source_row = by_js.get(edge.src_id)
        if source_row is None:
            continue
        target = _resolve_relative(source_row.addon_path, edge.dst_unresolved,
                                  relative_targets)
        if target is None:
            continue
        layer.edges.remove(edge)
        layer.edges.append(
            GraphEdge(
                table="js_imports", src_kind="js_file", src_id=edge.src_id,
                dst_kind="js_file", dst_id=relative_targets[target],
                dst_unresolved=edge.dst_unresolved, binding="resolved",
                confidence="heuristic", provenance="js.import.relative",
                src_file=edge.src_file, src_line=edge.src_line,
            )
        )
        resolved_imports += 1
    counts["js_imports_resolved"] = resolved_imports


def _resolve_relative(from_path: str, specifier: str, targets) -> str | None:
    """``./x`` or ``../y/z`` against the importing file's addon path."""
    base = os.path.dirname(from_path)
    joined = os.path.normpath(os.path.join(base, specifier)).replace("\\", "/")
    for candidate in (joined, *(joined + suffix for suffix in JS_SUFFIXES),
                      *(f"{joined}/index{suffix}" for suffix in JS_SUFFIXES)):
        if candidate in targets:
            return candidate
    return None

