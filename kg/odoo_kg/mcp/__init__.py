"""MCP server (milestone M2).

A local stdio server that lets a client ask small, bounded questions of the
graph M1 built. It is strictly a reader: it opens the database read-only and
never indexes, writes, or reaches the network.

Layout:

``limits``      the hard caps, in one place so tests can assert on them
``jsonrpc``     JSON-RPC 2.0 framing and error codes, transport-agnostic
``readonly_db`` the read-only SQLite connection
``queries``     every SQL statement, all static and parameterised
``tools``       tool schemas, argument validation, and handlers
``server``      MCP method dispatch and the stdio loop
"""

SERVER_NAME = "odoo-kg"

#: Protocol revisions this server actually implements.
#:
#: Deliberately a single entry. Responses use 2025-06-18 shapes -- notably
#: `structuredContent` on tool results and `title` on tool descriptors -- and
#: compatibility with the 2025-03-26 and 2024-11-05 schemas has not been
#: tested. Echoing an older version back would claim conformance this server
#: has not demonstrated, so any other well-formed version is answered with this
#: one and the client decides whether it can proceed.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18",)
PREFERRED_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
