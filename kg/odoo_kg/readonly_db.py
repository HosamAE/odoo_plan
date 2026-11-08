"""Read-only access to a graph database.

Every command that only reports or validates opens the graph through this
module. It exists because "read-only" was previously an intention rather than a
property: ``stats`` and ``verify`` opened the writing store, and
``GraphStore.__init__`` runs the schema script, records the version row and
commits on every open. On a database whose schema version already matched, that
rewrote pages -- same size, new bytes, new mtime -- for a command that was only
supposed to print numbers. That is how M4.2's v6 graph was modified during M4.3.

Four independent locks, because any one of them alone has a hole:

1. The connection is opened through a ``file:...?mode=ro`` URI, so SQLite
   refuses to create the file and opens it without write intent.
2. ``immutable=1`` tells SQLite the file will not change underneath it, which
   removes its reason to create a ``-shm``, ``-wal`` or journal sidecar. Without
   this a WAL-mode database opened read-only still creates ``-shm`` next to it.
3. ``PRAGMA query_only = ON`` makes the connection reject every mutating
   statement even if one were somehow issued.
4. No schema script, no ``_record_versions()``, no ``commit()``. There is no
   code path from here that could write, so the guarantee does not depend on
   the caller behaving.

``immutable=1`` is a promise the caller makes to SQLite, so it is only safe
when nothing is mid-write. That is checked rather than assumed: a ``-wal``,
``-shm`` or ``-journal`` sidecar next to the database means either an active
WAL or a hot journal awaiting rollback, and this module refuses to open in that
state instead of reading a version of the data that no writer ever committed.

The path never comes from a tool argument. It is resolved from the
configuration file the command or server was started with, so a client cannot
point this at an arbitrary file on disk.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from . import SCHEMA_VERSION
from .errors import OdooKgError, SchemaVersionError


class GraphUnavailableError(OdooKgError):
    """The graph database is missing, unreadable, or not an odoo-kg graph."""


class ProjectMismatchError(GraphUnavailableError):
    """A real odoo-kg graph, but one belonging to a different project.

    Kept apart from its parent because the two failures deserve opposite
    handling. *No graph* is a state a client should be able to connect through
    -- somebody has installed the server and not indexed yet, and the way they
    find that out is a tool answering "run index". *The wrong graph* is not
    like that: it answers every question fluently about another repository, so
    the session must not start at all.
    """


#: Files SQLite puts next to a database while it is being written or read in
#: WAL mode. Their presence means this module must not claim the file is
#: immutable.
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")

#: The first sixteen bytes of every SQLite database file.
SQLITE_MAGIC = b"SQLite format 3\x00"

#: Tables an odoo-kg graph must have, at every schema version this build has
#: ever written. Checked on open so a wrong or truncated file fails with a
#: clear message instead of a confusing "no such table" much later.
REQUIRED_TABLES = frozenset(
    {"project", "node_module", "edge_depends_on", "node_file", "kg_meta"}
)


def sidecars_present(db_path: str) -> list[str]:
    """Which SQLite sidecar files sit next to ``db_path`` right now."""
    return [suffix for suffix in SIDECAR_SUFFIXES if os.path.exists(db_path + suffix)]


def header_versions(db_path: str) -> tuple[int, int]:
    """Bytes 18 and 19 of the file header: (write_version, read_version).

    ``1`` means the legacy rollback journal, ``2`` means WAL. Read as bytes
    rather than through SQLite, because opening the file is the thing being
    guarded -- and because these two bytes are exactly what a careless
    read-write open flips, so a test can watch them.
    """
    try:
        with open(db_path, "rb") as handle:
            head = handle.read(20)
    except OSError as exc:
        raise GraphUnavailableError(f"cannot read {db_path}: {exc}") from exc
    if len(head) < 20 or not head.startswith(SQLITE_MAGIC):
        raise GraphUnavailableError(
            f"{db_path} is not a SQLite database: the file header is missing "
            f"or truncated."
        )
    return head[18], head[19]


def _read_only_uri(db_path: str) -> str:
    """A ``mode=ro&immutable=1`` URI for an absolute path.

    ``Path.as_uri()`` requires an absolute path and does the percent-encoding,
    which matters on Windows where a data directory can contain spaces.
    """
    return f"{Path(os.path.abspath(db_path)).as_uri()}?mode=ro&immutable=1"


def _schema_version_of(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT value FROM kg_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _assert_schema(connection, db_path: str, expect_schema: int | None) -> None:
    if expect_schema is None:
        return
    found = _schema_version_of(connection)
    if found == expect_schema:
        return
    raise SchemaVersionError(
        f"refusing to read {db_path}: it holds schema version {found} and this "
        f"build reads {expect_schema}. Its tables are not the ones this build "
        f"queries, so the answers would be wrong or the query would fail "
        f"halfway. Point --config at this milestone's own project."
    )


def _assert_project(connection, db_path: str, expect_project_id: str | None) -> None:
    if expect_project_id is None:
        return
    found = [
        row[0] for row in connection.execute("SELECT project_id FROM project")
    ]
    if expect_project_id in found:
        return
    raise ProjectMismatchError(
        f"refusing to read {db_path}: it holds project "
        f"{found or 'nothing'} and this configuration is for "
        f"{expect_project_id!r}. Point --config at the matching project."
    )


def open_readonly(
    db_path: str,
    *,
    expect_schema: int | None = None,
    expect_project_id: str | None = None,
) -> sqlite3.Connection:
    """Open ``db_path`` strictly read-only, or refuse.

    ``expect_schema`` and ``expect_project_id`` are optional so the MCP server
    keeps the behaviour it has always had. The CLI read commands pass both,
    because a command that reports numbers should say "wrong graph" rather than
    print numbers from one.
    """
    if not os.path.isfile(db_path):
        raise GraphUnavailableError(
            f"no graph database at {db_path}. Run `python -m odoo_kg index` first."
        )

    # Reads the header as bytes. Refuses a non-SQLite file before any part of
    # SQLite sees the path.
    header_versions(db_path)

    hot = sidecars_present(db_path)
    if hot:
        raise GraphUnavailableError(
            f"refusing to read {db_path}: {', '.join(db_path + s for s in hot)} "
            f"present. That means an active WAL or a journal awaiting rollback, "
            f"and reading around it would return data no writer committed. Let "
            f"the writer finish, then try again."
        )

    try:
        connection = sqlite3.connect(_read_only_uri(db_path), uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise GraphUnavailableError(f"cannot open {db_path} read-only: {exc}") from exc

    connection.row_factory = sqlite3.Row
    try:
        # Both are connection-scoped. Neither is written into the database
        # file, unlike journal_mode, which is why journal_mode is not set here.
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        present = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = REQUIRED_TABLES - present
        if missing:
            raise GraphUnavailableError(
                f"{db_path} is not an odoo-kg graph: missing tables "
                f"{sorted(missing)}. Re-run `python -m odoo_kg index --rebuild`."
            )
        _assert_schema(connection, db_path, expect_schema)
        _assert_project(connection, db_path, expect_project_id)
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise GraphUnavailableError(
            f"{db_path} is not a readable SQLite database: {exc}"
        ) from exc
    except BaseException:
        connection.close()
        raise
    return connection


def open_graph_readonly(config) -> sqlite3.Connection:
    """The strict open a CLI read command wants: this build's schema, this project."""
    return open_readonly(
        config.db_path.display,
        expect_schema=SCHEMA_VERSION,
        expect_project_id=config.project_id,
    )


def is_writable(connection: sqlite3.Connection) -> bool:
    """True if the connection would accept a write.

    Used to prove the read-only guarantee. The probe runs inside a savepoint
    that is always rolled back, so even against a writable connection this
    leaves nothing behind -- a module dedicated to read-only access should not
    contain a function that can durably modify a database.
    """
    try:
        connection.execute("SAVEPOINT odoo_kg_write_probe")
    except sqlite3.Error:
        # A connection that will not even open a savepoint certainly will not
        # accept a write.
        return False
    try:
        connection.execute("CREATE TABLE _odoo_kg_write_probe (x INTEGER)")
        return True
    except sqlite3.Error:
        return False
    finally:
        try:
            connection.execute("ROLLBACK TO odoo_kg_write_probe")
            connection.execute("RELEASE odoo_kg_write_probe")
        except sqlite3.Error:
            pass
