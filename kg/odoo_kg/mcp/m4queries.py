"""Read-only queries over the frontend layer: assets, JS, OWL, routes.

Same two rules as every query module here: every statement is a literal,
caller input reaches the database only through ``?`` placeholders, and every
list is bounded and reports whether it was cut.

Two rules specific to this layer.

**Routes are declarations.** ``auth`` and ``type`` are returned exactly as
written, and are NULL when the source does not write them -- Odoo's runtime
default is never filled in here. A route whose decorator is partly computed
says so rather than pretending the declaration is complete.

**Nothing read out of JavaScript is certain.** :mod:`odoo_kg.jsread` is a
lexer, so every row derived from it carries ``heuristic`` confidence and an
evidence kind naming how it was found. The kind registry below is the single
place that mapping lives, so a tool cannot accidentally present a guess and a
parsed fact the same way.
"""

from __future__ import annotations

MAX_ROWS = 100
MAX_DEPTH = 5

#: How a fact was established. Not a quality score -- a statement about the
#: reader that produced it, so a caller can weigh the answer itself.
EVIDENCE_MANIFEST = "manifest"          # read from a __manifest__.py literal
EVIDENCE_FILE_SCAN = "file_scan"        # the file exists in the scanned tree
EVIDENCE_XML = "xml"                    # read from an XML attribute
EVIDENCE_JS_LEXER = "js_lexer"          # recognised by shape in a token stream
EVIDENCE_PYTHON_AST = "python_ast"      # read from a Python syntax tree


class Kind:
    """One searchable frontend kind, and everything needed to answer about it.

    Node-backed kinds live in a ``node_*`` table. Edge-backed kinds (a registry
    entry, a service use, a patch) have no node of their own: they exist only
    as an edge out of a JavaScript file, so their identity is the edge id and
    their name is what the edge was looking for.
    """

    def __init__(self, name, *, table, id_column, name_column, module_column,
                 path_column, line_column, confidence, evidence, extra=(),
                 edge_table=None):
        self.name = name
        self.table = table
        self.id_column = id_column
        self.name_column = name_column
        self.module_column = module_column
        self.path_column = path_column
        self.line_column = line_column
        self.confidence = confidence
        self.evidence = evidence
        self.extra = tuple(extra)
        self.edge_table = edge_table

    @property
    def is_edge_backed(self) -> bool:
        return self.edge_table is not None


#: The eight kinds ``kg_find_frontend`` searches, in the order it reports them.
#: Ordering is part of the contract: two calls must agree, so the search sorts
#: by this sequence and then by name and id.
KINDS: dict[str, Kind] = {
    "asset_bundle": Kind(
        "asset_bundle", table="node_asset_bundle", id_column="bundle_name",
        name_column="bundle_name", module_column=None, path_column=None,
        line_column=None, confidence="certain", evidence=EVIDENCE_MANIFEST,
        extra=("entry_count",),
    ),
    "asset_entry": Kind(
        "asset_entry", table="node_asset_entry", id_column="entry_id",
        name_column="target", module_column="module", path_column="rel_path",
        line_column="line", confidence="certain", evidence=EVIDENCE_MANIFEST,
        extra=("bundle", "position", "operation", "extra_target", "is_glob"),
    ),
    "js_file": Kind(
        "js_file", table="node_js_file", id_column="js_id",
        name_column="rel_path", module_column="module", path_column="rel_path",
        line_column=None, confidence="certain", evidence=EVIDENCE_FILE_SCAN,
        extra=("addon_path", "odoo_module", "lex_state", "token_count",
               "class_count", "import_count"),
    ),
    "owl_component": Kind(
        "owl_component", table="node_owl_component", id_column="component_id",
        name_column="class_name", module_column="module", path_column="rel_path",
        line_column="line", confidence="heuristic", evidence=EVIDENCE_JS_LEXER,
        extra=("base", "template_name", "js_id"),
    ),
    "owl_template": Kind(
        "owl_template", table="node_owl_template", id_column="template_name",
        name_column="template_name", module_column="module",
        path_column="rel_path", line_column="line", confidence="certain",
        evidence=EVIDENCE_XML, extra=("inherits", "primary_mode"),
    ),
    "registry_entry": Kind(
        "registry_entry", table="edge_js_registers", id_column="edge_id",
        name_column="dst_unresolved", module_column="module",
        path_column="src_file", line_column="src_line", confidence="heuristic",
        evidence=EVIDENCE_JS_LEXER, edge_table="js_registers",
    ),
    "service": Kind(
        "service", table="edge_js_uses_service", id_column="edge_id",
        name_column="dst_unresolved", module_column="module",
        path_column="src_file", line_column="src_line", confidence="heuristic",
        evidence=EVIDENCE_JS_LEXER, edge_table="js_uses_service",
    ),
    "patch": Kind(
        "patch", table="edge_js_patches", id_column="edge_id",
        name_column="dst_unresolved", module_column="module",
        path_column="src_file", line_column="src_line", confidence="heuristic",
        evidence=EVIDENCE_JS_LEXER, edge_table="js_patches",
    ),
}

KIND_NAMES = tuple(KINDS)

#: Kinds a traversal can reach but not search: they belong to other layers, or
#: to no layer at all. Reported as leaves, with what the edge was looking for.
LEAF_KINDS = {
    "route": ("node_route", "route_id", "path", EVIDENCE_PYTHON_AST),
    "model": ("node_model", "model_name", "model_name", EVIDENCE_PYTHON_AST),
    "file": ("node_file", "file_key", "rel_path", EVIDENCE_FILE_SCAN),
}

#: Every frontend edge table, with the relation name a traversal reports. The
#: kinds are read from each row rather than hard-coded here, because a single
#: table can legitimately point at more than one kind: an asset entry includes
#: a JS file, a plain file, or another bundle.
EDGE_RELATIONS = {
    "asset_in_bundle": "declared_into",
    "asset_includes_file": "includes",
    "js_imports": "imports",
    "js_registers": "registers",
    "js_patches": "patches",
    "js_uses_service": "uses_service",
    "component_uses_template": "uses_template",
    "template_inherits": "inherits_template",
    "js_calls_route": "calls_route",
    "js_references_model": "references_model",
}

_EDGE_TABLE_NAMES = frozenset(EDGE_RELATIONS)


def _check_edge_table(table: str) -> str:
    """Refuse any table name that is not one of ours.

    Table names are never caller input -- every caller passes a literal from
    ``EDGE_RELATIONS`` -- but the check sits here, at the sink, so a future
    caller cannot make this the one place that trusted its argument.
    """
    if table not in _EDGE_TABLE_NAMES:
        raise ValueError(f"refusing to query unknown edge table {table!r}")
    return table


def check_kind(kind: str) -> Kind:
    """Refuse any kind that is not one of ours.

    The pair to ``_check_edge_table``. Every function that interpolates a
    kind's table or column names into SQL passes through here first, so the
    guard lives beside the sink rather than depending on each caller.
    """
    if kind not in KINDS:
        raise ValueError(f"refusing to query unknown frontend kind {kind!r}")
    return KINDS[kind]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _rows(connection, sql, params, limit):
    fetched = [dict(r) for r in connection.execute(sql, (*params, limit + 1))]
    return fetched[:limit], len(fetched) > limit


# --------------------------------------------------------------------------
# the unified frontend view
# --------------------------------------------------------------------------


def _select_for(kind: Kind) -> str:
    """The projection that turns any kind's row into one common shape."""
    if kind.is_edge_backed:
        columns = [
            f"e.{kind.id_column} AS id",
            f"e.{kind.name_column} AS name",
            "j.module AS module",
            f"e.{kind.path_column} AS rel_path",
            f"e.{kind.line_column} AS line",
            "e.binding AS binding",
            "e.confidence AS confidence",
            "e.provenance AS provenance",
            "e.src_id AS js_id",
        ]
        return (
            f"SELECT {', '.join(columns)} FROM {kind.table} e "
            "LEFT JOIN node_js_file j ON j.js_id = e.src_id"
        )

    columns = [
        f"{kind.id_column} AS id",
        f"{kind.name_column} AS name",
        (f"{kind.module_column} AS module" if kind.module_column
         else "NULL AS module"),
        (f"{kind.path_column} AS rel_path" if kind.path_column
         else "NULL AS rel_path"),
        (f"{kind.line_column} AS line" if kind.line_column else "NULL AS line"),
    ]
    columns.extend(kind.extra)
    return f"SELECT {', '.join(columns)} FROM {kind.table}"


def find_in_kind(connection, kind_name, query, module, limit):
    """One kind's matches for a substring, bounded and ordered."""
    kind = check_kind(kind_name)
    select = _select_for(kind)
    if kind.is_edge_backed:
        where = (
            " WHERE (? = '' OR e.dst_unresolved LIKE ? ESCAPE '\\')"
            " AND (? = '' OR j.module = ?)"
            f" ORDER BY e.{kind.name_column}, e.{kind.id_column} LIMIT ?"
        )
    else:
        module_clause = (
            f" AND (? = '' OR {kind.module_column} = ?)" if kind.module_column
            # A bundle has no module of its own: it is a name that several
            # modules add into. A module filter must therefore exclude every
            # bundle rather than silently ignore the filter and answer a
            # question the caller did not ask. Both placeholders take the same
            # value, so this is true only when no module was given.
            else " AND ? = '' AND ? = ''"
        )
        where = (
            f" WHERE (? = '' OR {kind.name_column} LIKE ? ESCAPE '\\')"
            + module_clause
            + f" ORDER BY {kind.name_column}, {kind.id_column} LIMIT ?"
        )
    pattern = f"%{_escape_like(query)}%" if query else ""
    rows, truncated = _rows(
        connection, select + where,
        (query or "", pattern, module or "", module or ""), limit,
    )
    for row in rows:
        row["kind"] = kind.name
        row.setdefault("confidence", kind.confidence)
        row["evidence"] = kind.evidence
    return rows, truncated


def count_in_kind(connection, kind_name, query, module) -> int:
    """How many rows a kind holds for this filter. Never truncated."""
    kind = check_kind(kind_name)
    if kind.is_edge_backed:
        sql = (
            f"SELECT COUNT(*) FROM {kind.table} e "
            "LEFT JOIN node_js_file j ON j.js_id = e.src_id "
            "WHERE (? = '' OR e.dst_unresolved LIKE ? ESCAPE '\\') "
            "AND (? = '' OR j.module = ?)"
        )
    else:
        module_clause = (
            f"AND (? = '' OR {kind.module_column} = ?)" if kind.module_column
            # See find_in_kind: a module filter excludes bundles outright.
            else "AND ? = '' AND ? = ''"
        )
        sql = (
            f"SELECT COUNT(*) FROM {kind.table} "
            f"WHERE (? = '' OR {kind.name_column} LIKE ? ESCAPE '\\') "
            + module_clause
        )
    pattern = f"%{_escape_like(query)}%" if query else ""
    return int(connection.execute(
        sql, (query or "", pattern, module or "", module or "")
    ).fetchone()[0])


def get_in_kind(connection, kind_name, name):
    """Every object of one kind matching a name or a stable id.

    Returns a list on purpose. Two components can share a class name, and two
    files can register the same key; answering with whichever row sorted first
    would hide that. The caller decides what to do with more than one.
    """
    kind = check_kind(kind_name)
    select = _select_for(kind)
    if kind.is_edge_backed:
        where = (
            f" WHERE e.{kind.id_column} = ? OR e.{kind.name_column} = ?"
            f" ORDER BY e.{kind.path_column}, e.{kind.line_column}, e.{kind.id_column}"
        )
    else:
        where = (
            f" WHERE {kind.id_column} = ? OR {kind.name_column} = ?"
            f" ORDER BY {kind.name_column}, {kind.id_column}"
        )
    rows = [dict(r) for r in connection.execute(select + where, (name, name))]
    for row in rows:
        row["kind"] = kind.name
        row.setdefault("confidence", kind.confidence)
        row["evidence"] = kind.evidence
    return rows


def names_like(connection, kind_name, name, limit=5):
    """Near misses for an unknown name, to put in the error message."""
    kind = check_kind(kind_name)
    #: The last path segment, so 'src/board.js' still suggests 'board.js'.
    needle = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    like = f"%{_escape_like(needle)}%"
    return [
        r["name"] for r in connection.execute(
            f"SELECT DISTINCT {kind.name_column} AS name FROM {kind.table} "
            f"WHERE {kind.name_column} LIKE ? ESCAPE '\\' "
            f"ORDER BY {kind.name_column} LIMIT ?",
            (like, limit),
        )
    ]


def leaf_row(connection, dst_kind, dst_id):
    """A node in another layer that a frontend edge points at."""
    spec = LEAF_KINDS.get(dst_kind)
    if spec is None or dst_id is None:
        return None
    table, id_column, name_column, _evidence = spec
    row = connection.execute(
        f"SELECT {id_column} AS id, {name_column} AS name FROM {table} "
        f"WHERE {id_column} = ?", (dst_id,)
    ).fetchone()
    return dict(row) if row else None


def frontend_counts(connection):
    def scalar(sql):
        return int(connection.execute(sql).fetchone()[0])

    return {
        "asset_bundles": scalar("SELECT COUNT(*) FROM node_asset_bundle"),
        "asset_entries": scalar("SELECT COUNT(*) FROM node_asset_entry"),
        "js_files": scalar("SELECT COUNT(*) FROM node_js_file"),
        "owl_components": scalar("SELECT COUNT(*) FROM node_owl_component"),
        "owl_templates": scalar("SELECT COUNT(*) FROM node_owl_template"),
        "registry_entries": scalar("SELECT COUNT(*) FROM edge_js_registers"),
        "services": scalar("SELECT COUNT(*) FROM edge_js_uses_service"),
        "patches": scalar("SELECT COUNT(*) FROM edge_js_patches"),
        "routes": scalar("SELECT COUNT(*) FROM node_route"),
    }


# --------------------------------------------------------------------------
# relations
# --------------------------------------------------------------------------


def edges_from(connection, table, src_id, limit):
    """Edges leaving a node, in a named table."""
    return _rows(
        connection,
        f"SELECT edge_id, src_kind, src_id, dst_kind, dst_id, dst_unresolved, "
        f"binding, confidence, provenance, src_file, src_line "
        f"FROM edge_{_check_edge_table(table)} WHERE src_id = ? "
        "ORDER BY dst_unresolved, src_line, edge_id LIMIT ?",
        (src_id,), limit,
    )


def edges_into(connection, table, dst_id, limit):
    """Edges arriving at a node, in a named table."""
    return _rows(
        connection,
        f"SELECT edge_id, src_kind, src_id, dst_kind, dst_id, dst_unresolved, "
        f"binding, confidence, provenance, src_file, src_line "
        f"FROM edge_{_check_edge_table(table)} WHERE dst_id = ? "
        "ORDER BY src_id, src_line, edge_id LIMIT ?",
        (dst_id,), limit,
    )


def components_in_file(connection, js_id, limit):
    """The components a JS file defines.

    A stored column (``node_owl_component.js_id``), not an edge table. Callers
    report it as such rather than folding it in with the edges.
    """
    return _rows(
        connection,
        "SELECT component_id, class_name, base, template_name, module, "
        "rel_path, line FROM node_owl_component WHERE js_id = ? "
        "ORDER BY line, class_name LIMIT ?",
        (js_id,), limit,
    )


def file_of_component(connection, component_id):
    row = connection.execute(
        "SELECT j.js_id, j.rel_path, j.module FROM node_owl_component c "
        "JOIN node_js_file j ON j.js_id = c.js_id WHERE c.component_id = ?",
        (component_id,)
    ).fetchone()
    return dict(row) if row else None


def entries_naming(connection, addon_path, limit):
    """The asset entries whose resolved target is this file."""
    return _rows(
        connection,
        "SELECT DISTINCT a.entry_id, a.bundle, a.module, a.rel_path, a.line "
        "FROM edge_asset_includes_file e "
        "JOIN node_asset_entry a ON a.entry_id = e.src_id "
        "WHERE e.dst_unresolved = ? ORDER BY a.bundle, a.entry_id LIMIT ?",
        (addon_path,), limit,
    )


def entries_in_bundle(connection, bundle, limit):
    return _rows(
        connection,
        "SELECT entry_id, module, position, operation, target, rel_path, line "
        "FROM node_asset_entry WHERE bundle = ? "
        "ORDER BY module, position, entry_id LIMIT ?",
        (bundle,), limit,
    )


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------


def find_routes(connection, query, module, auth, limit):
    sql = """
        SELECT route_id, path, module, controller, method, base, auth,
               route_type, methods, website, csrf, has_dynamic_argument,
               rel_path, line
        FROM node_route
        WHERE (? = '' OR path LIKE ? ESCAPE '\\')
          AND (? = '' OR module = ?)
          AND (? = '' OR IFNULL(auth, '<undeclared>') = ?)
        ORDER BY IFNULL(path, ''), module, controller, method
        LIMIT ?
    """
    pattern = f"%{_escape_like(query)}%" if query else ""
    return _rows(
        connection, sql,
        (query or "", pattern, module or "", module or "", auth or "", auth or ""),
        limit,
    )


def get_route_by_path(connection, path):
    return [
        dict(r) for r in connection.execute(
            "SELECT route_id, path, module, controller, method, base, auth, "
            "route_type, methods, website, csrf, sitemap, has_dynamic_argument, "
            "rel_path, line FROM node_route WHERE path = ? "
            "ORDER BY module, controller, method", (path,)
        )
    ]


def route_paths_like(connection, path, limit=5):
    like = f"%{_escape_like(path)}%"
    return [
        r["path"] for r in connection.execute(
            "SELECT DISTINCT path FROM node_route WHERE path LIKE ? ESCAPE '\\' "
            "AND path IS NOT NULL ORDER BY path LIMIT ?", (like, limit)
        )
    ]


def js_calling_route(connection, route_id, limit):
    return _rows(
        connection,
        "SELECT j.rel_path, j.module, e.src_line, e.confidence "
        "FROM edge_js_calls_route e "
        "JOIN node_js_file j ON j.js_id = e.src_id WHERE e.dst_id = ? "
        "ORDER BY j.rel_path, e.src_line LIMIT ?",
        (route_id,), limit,
    )


def route_counts(connection):
    def scalar(sql, params=()):
        return int(connection.execute(sql, params).fetchone()[0])

    return {
        "routes": scalar("SELECT COUNT(*) FROM node_route"),
        "with_declared_auth": scalar(
            "SELECT COUNT(*) FROM node_route WHERE auth IS NOT NULL"),
        "auth_not_declared": scalar(
            "SELECT COUNT(*) FROM node_route WHERE auth IS NULL"),
        "partly_computed": scalar(
            "SELECT COUNT(*) FROM node_route WHERE has_dynamic_argument = 1"),
    }
