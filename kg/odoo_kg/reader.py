"""A graph opened for reading only.

:class:`GraphReader` answers exactly the questions :class:`~odoo_kg.store.GraphStore`
answers -- it inherits them, so there is one implementation of counting and
digesting rather than two -- over a connection that physically cannot write.

Use this for every command that reports or validates. ``GraphStore`` is for
``index`` and for deliberate maintenance, and its constructor is not a passive
observer: it runs the schema script, records the version row and commits. On a
database whose schema version already matched, that quietly rewrote pages for a
command that only printed numbers.
"""

from __future__ import annotations

import sqlite3

from . import SCHEMA_VERSION, generations
from .readonly_db import GraphUnavailableError, is_writable, open_readonly
from .store import GraphReads

__all__ = ["GraphReader", "GraphUnavailableError", "open_reader"]


class GraphReader(GraphReads):
    """Read-only access to a graph, with the store's full query surface."""

    def __init__(
        self,
        db_path: str,
        *,
        expect_schema: int | None = SCHEMA_VERSION,
        expect_project_id: str | None = None,
    ):
        self.db_path = db_path
        #: Set by open_reader: how this database was chosen. None when a
        #: caller opened a path directly.
        self.resolution = None
        self.connection: sqlite3.Connection = open_readonly(
            db_path,
            expect_schema=expect_schema,
            expect_project_id=expect_project_id,
        )

    @property
    def writable(self) -> bool:
        """Whether this connection would accept a write. Always False.

        Exposed so a test can assert the guarantee against a live reader
        instead of inferring it from the URI string.
        """
        return is_writable(self.connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "GraphReader":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def open_reader(config) -> GraphReader:
    """The reader a CLI command wants: the published generation, read-only.

    The path is resolved from the project's ``CURRENT`` pointer, so a rebuild
    running right now is writing somewhere else entirely and this file cannot
    change underneath the reader. A data directory from before generations
    existed resolves to its own ``graph.db`` instead, read and never modified.
    """
    resolution = generations.resolve(config, expect_schema=SCHEMA_VERSION)
    reader = GraphReader(
        resolution.db_path,
        expect_schema=SCHEMA_VERSION,
        expect_project_id=config.project_id,
    )
    reader.resolution = resolution
    return reader
