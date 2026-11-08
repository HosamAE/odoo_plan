"""Every SQL statement the server can issue.

Two rules hold throughout, and they are the reason this module exists at all:

* **No arbitrary SQL.** There is no code path from a tool argument to a SQL
  string. Statements are literals in this file; caller input only ever arrives
  through ``?`` placeholders.
* **Nothing unbounded.** Every query that can return more than one row takes a
  limit, and traversals additionally stop at a total-node cap.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .limits import MAX_NODES, SUMMARY_TOP_N

#: Columns a module row exposes. Fixed list, so a schema change cannot silently
#: start leaking new fields into responses.
MODULE_COLUMNS = (
    "technical_name", "manifest_name", "version", "version_series_ok", "author",
    "license", "origin", "origin_rule", "installable", "installable_declared",
    "installed_state", "application", "auto_install", "auto_install_raw",
    "modified_worktree", "manifest_file", "manifest_line", "module_dir",
    "content_state", "present_in_source",
)

_MODULE_SELECT = ", ".join(MODULE_COLUMNS)

#: Filters find_modules accepts. Mapping a caller's filter name to a fixed SQL
#: fragment is what keeps the WHERE clause out of caller control.
_FILTERS = {
    "origin": "origin = ?",
    "application": "application = ?",
    "installable": "installable = ?",
    "series_ok": "version_series_ok = ?",
    "modified_worktree": "modified_worktree = ?",
}


def project(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM project WHERE project_id = ?", (project_id,)
    ).fetchone()


def any_project(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM project LIMIT 1").fetchone()


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        "modules": _scalar(connection, "SELECT COUNT(*) FROM node_module"),
        "files": _scalar(connection, "SELECT COUNT(*) FROM node_file"),
        "depends_on_edges": _scalar(connection, "SELECT COUNT(*) FROM edge_depends_on"),
    }


def summary(connection: sqlite3.Connection) -> dict:
    """A bounded overview. Every list here is capped; none grows with the graph."""
    return {
        "counts": counts(connection),
        "modules_by_origin": _group(
            connection, "SELECT origin, COUNT(*) FROM node_module GROUP BY origin"
        ),
        "modules_by_version_series": {
            "matching_expected_series": _scalar(
                connection, "SELECT COUNT(*) FROM node_module WHERE version_series_ok = 1"
            ),
            "other": _scalar(
                connection, "SELECT COUNT(*) FROM node_module WHERE version_series_ok = 0"
            ),
        },
        "modules_off_series": [
            {"module": row["technical_name"], "version": row["version"]}
            for row in connection.execute(
                "SELECT technical_name, version FROM node_module "
                "WHERE version_series_ok = 0 ORDER BY technical_name LIMIT ?",
                (SUMMARY_TOP_N,),
            )
        ],
        "dependency_bindings": _group(
            connection, "SELECT binding, COUNT(*) FROM edge_depends_on GROUP BY binding"
        ),
        "top_external_dependencies": [
            {"target": row["dst_unresolved"], "referenced_by": row["n"]}
            for row in connection.execute(
                "SELECT dst_unresolved, COUNT(*) AS n FROM edge_depends_on "
                "WHERE dst_module IS NULL GROUP BY dst_unresolved "
                "ORDER BY n DESC, dst_unresolved LIMIT ?",
                (SUMMARY_TOP_N,),
            )
        ],
        "most_depended_on_in_project": [
            {"module": row["dst_module"], "dependent_count": row["n"]}
            for row in connection.execute(
                "SELECT dst_module, COUNT(*) AS n FROM edge_depends_on "
                "WHERE dst_module IS NOT NULL GROUP BY dst_module "
                "ORDER BY n DESC, dst_module LIMIT ?",
                (SUMMARY_TOP_N,),
            )
        ],
        "modules_with_worktree_changes": _scalar(
            connection, "SELECT COUNT(*) FROM node_module WHERE modified_worktree = 1"
        ),
    }


def find_modules(
    connection: sqlite3.Connection,
    *,
    query: str | None,
    filters: dict[str, object],
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    """Search modules. Returns ``(rows, total_matching)``.

    ``total`` is computed with a separate COUNT so the caller can tell "20 of
    66" from "20 of 20" without the server ever materialising all matches.
    """
    clauses: list[str] = []
    params: list[object] = []

    if query:
        # ESCAPE makes a literal % or _ in the search string match itself
        # instead of acting as a wildcard.
        pattern = f"%{_escape_like(query)}%"
        clauses.append(
            "(technical_name LIKE ? ESCAPE '\\' OR manifest_name LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern])

    for name, value in filters.items():
        fragment = _FILTERS.get(name)
        if fragment is None:
            # Unreachable via the tool layer, which validates names first.
            raise ValueError(f"unsupported filter {name!r}")
        clauses.append(fragment)
        params.append(int(value) if isinstance(value, bool) else value)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    total = _scalar(connection, f"SELECT COUNT(*) FROM node_module{where}", tuple(params))
    rows = connection.execute(
        f"SELECT {_MODULE_SELECT} FROM node_module{where} "
        "ORDER BY technical_name LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [dict(row) for row in rows], total


def get_module(connection: sqlite3.Connection, name: str) -> dict | None:
    row = connection.execute(
        f"SELECT {_MODULE_SELECT} FROM node_module WHERE technical_name = ?", (name,)
    ).fetchone()
    return dict(row) if row else None


def module_names_like(
    connection: sqlite3.Connection, name: str, limit: int = 5
) -> list[str]:
    """Near-miss suggestions for an unknown module name."""
    pattern = f"%{_escape_like(name)}%"
    return [
        row["technical_name"]
        for row in connection.execute(
            "SELECT technical_name FROM node_module WHERE technical_name LIKE ? "
            "ESCAPE '\\' ORDER BY technical_name LIMIT ?",
            (pattern, limit),
        )
    ]


def direct_dependencies(connection: sqlite3.Connection, name: str) -> list[dict]:
    """What ``name`` declares in its manifest, resolved or not."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT dst_unresolved AS target, dst_module, binding, "
            "src_manifest_file, src_manifest_line "
            "FROM edge_depends_on WHERE src_module = ? "
            "ORDER BY dst_unresolved",
            (name,),
        )
    ]


def direct_dependents(connection: sqlite3.Connection, name: str) -> list[dict]:
    """Modules in this project whose manifest depends on ``name``."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT src_module AS module, dst_unresolved AS declared_as, "
            "src_manifest_file, src_manifest_line "
            "FROM edge_depends_on WHERE dst_module = ? "
            "ORDER BY src_module",
            (name,),
        )
    ]


@dataclass
class TraversalNode:
    module: str
    depth: int
    via: str
    resolved: bool
    manifest_file: str | None = None
    manifest_line: int | None = None

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "depth": self.depth,
            "via": self.via,
            "in_project": self.resolved,
            "declared_in": self.manifest_file,
            "declared_at_line": self.manifest_line,
        }


@dataclass
class Traversal:
    nodes: list[TraversalNode] = field(default_factory=list)
    truncated: bool = False
    truncated_reason: str = ""
    depth_reached: int = 0
    #: External targets (not modules in this project) found along the way.
    external: list[str] = field(default_factory=list)

    def by_depth(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for node in self.nodes:
            out[node.depth] = out.get(node.depth, 0) + 1
        return out


def traverse(
    connection: sqlite3.Connection,
    start: str,
    *,
    direction: str,
    depth: int,
    max_nodes: int = MAX_NODES,
) -> Traversal:
    """Breadth-first walk of depends_on edges, bounded by depth and node count.

    ``direction`` is ``"dependencies"`` (what this module needs) or
    ``"dependents"`` (what needs this module).

    Breadth-first matters: when the cap is hit, what has been collected is the
    closest, most relevant part of the neighbourhood rather than one arbitrary
    deep branch. A cycle cannot loop forever because a module is expanded at
    most once.
    """
    if direction not in ("dependencies", "dependents"):
        raise ValueError(f"unsupported direction {direction!r}")

    result = Traversal()
    seen = {start}
    frontier = [start]

    for level in range(1, depth + 1):
        if not frontier:
            break
        next_frontier: list[str] = []
        for current in frontier:
            if direction == "dependencies":
                edges = direct_dependencies(connection, current)
                for edge in edges:
                    target = edge["target"]
                    resolved = edge["dst_module"] is not None
                    if not resolved and target not in result.external:
                        result.external.append(target)
                    if target in seen:
                        continue
                    seen.add(target)
                    if len(result.nodes) >= max_nodes:
                        result.truncated = True
                        result.truncated_reason = (
                            f"stopped at the {max_nodes}-node cap; narrow the "
                            "question or reduce depth"
                        )
                        return result
                    result.nodes.append(
                        TraversalNode(
                            module=target,
                            depth=level,
                            via=current,
                            resolved=resolved,
                            manifest_file=edge["src_manifest_file"],
                            manifest_line=edge["src_manifest_line"],
                        )
                    )
                    result.depth_reached = level
                    # An external target has no node in this graph, so there is
                    # nothing further to expand from it.
                    if resolved:
                        next_frontier.append(target)
            else:
                for edge in direct_dependents(connection, current):
                    module = edge["module"]
                    if module in seen:
                        continue
                    seen.add(module)
                    if len(result.nodes) >= max_nodes:
                        result.truncated = True
                        result.truncated_reason = (
                            f"stopped at the {max_nodes}-node cap; narrow the "
                            "question or reduce depth"
                        )
                        return result
                    result.nodes.append(
                        TraversalNode(
                            module=module,
                            depth=level,
                            via=current,
                            resolved=True,
                            manifest_file=edge["src_manifest_file"],
                            manifest_line=edge["src_manifest_line"],
                        )
                    )
                    result.depth_reached = level
                    next_frontier.append(module)
        frontier = next_frontier

    if frontier:
        # There was more to explore, but the depth budget ran out.
        result.truncated = True
        result.truncated_reason = (
            f"stopped at depth {depth}; {len(frontier)} module(s) were not expanded"
        )
    return result


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _scalar(connection: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(connection.execute(sql, params).fetchone()[0])


def _group(connection: sqlite3.Connection, sql: str) -> dict[str, int]:
    return {str(row[0]): int(row[1]) for row in connection.execute(sql)}
