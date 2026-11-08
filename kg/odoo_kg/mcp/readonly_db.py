"""The MCP server's read-only database access.

This used to be the only strictly read-only opener in the project, which is
precisely why the CLI's ``stats`` and ``verify`` were not covered by it: they
reached for the writing store instead, and no test noticed because every
read-only test imported this module and this module was only ever used by the
server.

The implementation now lives at :mod:`odoo_kg.readonly_db` so that the CLI can
use it without importing the MCP package, which ``cli.py`` deliberately loads
only for ``serve``. The server's path through it is unchanged: same function,
same guarantees, one caller more.
"""

from __future__ import annotations

from ..readonly_db import (  # noqa: F401  (re-exported for the MCP package)
    REQUIRED_TABLES,
    SIDECAR_SUFFIXES,
    SQLITE_MAGIC,
    GraphUnavailableError,
    ProjectMismatchError,
    header_versions,
    is_writable,
    open_readonly,
    sidecars_present,
)

__all__ = [
    "GraphUnavailableError",
    "ProjectMismatchError",
    "REQUIRED_TABLES",
    "SIDECAR_SUFFIXES",
    "SQLITE_MAGIC",
    "header_versions",
    "is_writable",
    "open_readonly",
    "sidecars_present",
]
