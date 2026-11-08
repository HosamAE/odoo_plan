"""MCP method dispatch and the stdio loop.

The stdio contract is strict: stdout carries protocol messages and nothing
else. To make that structurally true rather than a matter of discipline, the
loop takes a private handle on the real stdout and then points ``sys.stdout``
at stderr, so a stray ``print()`` anywhere in the process cannot corrupt the
stream.

The graph is opened during ``initialize``, and what happens next depends on
which way it fails.

A graph that is real but not ours -- a different schema version, another
project's data -- stops the handshake. Advertising 27 tools is telling the
client they work, and a graph from another repository answers every one of them
fluently and wrongly. That failure has to arrive before the tool list, not
inside an answer.

A graph that is missing or unreadable does not stop the handshake. A client can
connect, initialize, and list tools before anything has been indexed; the
missing database then surfaces as an actionable tool error naming the command
to run. Somebody who has just installed the server needs to get that far.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any, TextIO

from .. import SCHEMA_VERSION, __version__, generations
from ..config import Config
from ..errors import OdooKgError, SchemaVersionError
from ..freshness import FreshnessChecker
from . import (
    PREFERRED_PROTOCOL_VERSION,
    SERVER_NAME,
    SUPPORTED_PROTOCOL_VERSIONS,
    jsonrpc,
)
from .jsonrpc import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    NOT_INITIALIZED,
    JsonRpcError,
    Request,
)
from .readonly_db import (
    GraphUnavailableError, ProjectMismatchError, open_readonly,
)
from .tools import TOOLS, TOOLS_BY_NAME, ToolContext, ToolError, validate_arguments


class McpServer:
    """Stateless-per-request MCP server over one read-only graph."""

    def __init__(self, config: Config):
        self.config = config
        self.freshness = FreshnessChecker(config.repo_root.display, config.exclusions)
        self._connection: sqlite3.Connection | None = None
        #: How the served graph was chosen: a published generation
        #: or a pre-generation database. Set on first open.
        self.resolution = None
        #: Set when the `initialize` request arrives. Everything except
        #: `initialize`, `ping`, and notifications is refused until then.
        self.initialize_received = False
        #: Set when the client's `notifications/initialized` arrives.
        self.initialized = False
        self.negotiated_version = PREFERRED_PROTOCOL_VERSION

    # -- graph access ----------------------------------------------------

    def connection(self) -> sqlite3.Connection:
        """The read-only connection, opened on demand and validated on open.

        The expectations are passed here rather than left to the query layer:
        a graph at the wrong schema version has tables this build's SQL does
        not name, and a graph belonging to another project answers every
        question about the wrong repository. Both are refused at the door,
        with the file untouched.
        """
        if self._connection is None:
            resolution = generations.resolve(
                self.config, expect_schema=SCHEMA_VERSION
            )
            self._connection = open_readonly(
                resolution.db_path,
                expect_schema=SCHEMA_VERSION,
                expect_project_id=self.config.project_id,
            )
            self.resolution = resolution
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    # -- dispatch --------------------------------------------------------

    def handle(self, request: Request) -> dict | None:
        """Handle one request; return the result payload, or None to be silent."""
        method = request.method
        if method == "initialize":
            if request.is_notification:
                # A well-formed initialize sent without an id must not start
                # the session: the handshake needs a response, and this one
                # can never receive it. Raising keeps the session closed;
                # handle_line then swallows the error, as it must for any
                # notification.
                raise JsonRpcError(
                    INVALID_REQUEST,
                    "initialize must be a request with an id, not a notification",
                )
            return self._initialize(request.params)
        if method == "notifications/initialized":
            self.initialized = True
            return None
        if method.startswith("notifications/"):
            return None
        # `ping` is explicitly allowed at any time, including before the
        # handshake, so a client can check liveness.
        if method == "ping":
            return {}
        if not self.initialize_received:
            # The lifecycle requires `initialize` first. Serving real work
            # before it would mean answering without an agreed protocol
            # version.
            raise JsonRpcError(
                NOT_INITIALIZED,
                f"received {method!r} before 'initialize'; "
                "send an initialize request first",
            )
        if method == "tools/list":
            return self._tools_list(request.params)
        if method == "tools/call":
            return self._tools_call(request.params)
        raise JsonRpcError(
            METHOD_NOT_FOUND,
            f"unknown method {method!r}",
            {"supported": ["initialize", "ping", "tools/list", "tools/call"]},
        )

    def _initialize(self, params: dict) -> dict:
        requested = _validate_initialize_params(params)
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            self.negotiated_version = requested
        else:
            # A well-formed version string we do not implement. Answering with
            # our own lets the client decide whether it can proceed; claiming
            # theirs would be a conformance promise we cannot keep.
            self.negotiated_version = PREFERRED_PROTOCOL_VERSION
        # Open the graph now, before the handshake succeeds, so that a graph
        # this build must not answer from never gets a tool list: handing a
        # client 27 tools is telling it the tools work.
        #
        # Only a graph that is real but *wrong* stops the session. A missing or
        # unreadable one does not, and that is deliberate: somebody who has
        # installed the server and not yet run `index` has to be able to
        # connect far enough for a tool to tell them so. Those keep the lazy
        # path and surface as an actionable tool error.
        try:
            self.connection()
        except (SchemaVersionError, ProjectMismatchError,
                generations.WrongGraphError) as exc:
            raise JsonRpcError(
                INVALID_REQUEST,
                f"refusing to serve this graph: {exc}",
                {"db_path": self.config.db_path.display,
                 "project_id": self.config.project_id,
                 "schema_version": SCHEMA_VERSION},
            ) from exc
        except (GraphUnavailableError, generations.NoGraphError):
            # No graph, no pointer, or a pointer naming a generation
            # that is gone. Nothing can be answered either way, so
            # the session starts and the tools name the command to
            # run. Only a graph that is real but wrong stops here.
            pass

        self.initialize_received = True
        return {
            "protocolVersion": self.negotiated_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": SERVER_NAME,
                "title": "Odoo Knowledge Graph",
                "version": __version__,
            },
            "instructions": (
                "Read-only access to a pre-built Odoo knowledge graph. Call "
                "kg_status first to check whether the graph is still fresh. "
                "Dependency data comes from module manifests only; code-level "
                "coupling is not indexed."
            ),
        }

    def _tools_list(self, params: dict) -> dict:
        if params.get("cursor") is not None:
            # We never issue a nextCursor, so any cursor we are handed is stale
            # or fabricated.
            raise JsonRpcError(
                INVALID_PARAMS,
                "this server returns all tools in one page and issues no cursor",
            )
        return {"tools": [tool.describe() for tool in TOOLS]}

    def _tools_call(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise JsonRpcError(INVALID_PARAMS, "tools/call requires a 'name' string")
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            raise JsonRpcError(
                INVALID_PARAMS,
                f"unknown tool {name!r}",
                {"available": sorted(TOOLS_BY_NAME)},
            )

        try:
            arguments = validate_arguments(tool.input_schema, params.get("arguments"))
            connection = self.connection()
            context = ToolContext(
                connection=connection,
                project_id=self.config.project_id,
                repo_root=self.config.repo_root.display,
                freshness=self.freshness,
            )
            text, data = tool.handler(context, arguments)
            verdict = self.freshness.check(connection, self.config.project_id)
            if "freshness" not in data:
                data["graph_freshness"] = verdict.to_dict()
            warning = verdict.warning()
            if warning and name != "kg_status":
                text = f"{warning}\n\n{text}"
            # The human-readable rendering travels inside the payload rather
            # than replacing it, so a client that reads only the text block
            # still gets the full structured answer.
            data["summary"] = text
            return {
                "content": [{"type": "text", "text": _serialize(data)}],
                "structuredContent": data,
                "isError": False,
            }
        except (ToolError, GraphUnavailableError,
                generations.PublishError) as exc:
            # Tool-level failures belong in the result, not in the JSON-RPC
            # error member: the model should see them and adjust.
            return _tool_error(str(exc))
        except sqlite3.Error as exc:
            return _tool_error(
                f"the graph database could not be read: {exc}. "
                "Re-run `python -m odoo_kg index --rebuild`."
            )


def _validate_initialize_params(params: dict) -> str:
    """Validate an `initialize` params object; return its protocolVersion.

    The handshake is the one place where a malformed request must not be
    quietly tolerated: everything afterwards depends on having agreed a
    protocol version with a client that identified itself. Each failure names
    the offending field so the client can fix it without guessing.
    """
    if not isinstance(params, dict):
        raise JsonRpcError(INVALID_PARAMS, "initialize params must be an object")

    if "protocolVersion" not in params:
        raise JsonRpcError(INVALID_PARAMS, "initialize params require 'protocolVersion'")
    version = params["protocolVersion"]
    if not isinstance(version, str):
        raise JsonRpcError(
            INVALID_PARAMS,
            "protocolVersion must be a string, got "
            f"{'null' if version is None else type(version).__name__}",
        )
    if not version.strip():
        raise JsonRpcError(INVALID_PARAMS, "protocolVersion must not be empty")

    if "capabilities" not in params:
        raise JsonRpcError(INVALID_PARAMS, "initialize params require 'capabilities'")
    if not isinstance(params["capabilities"], dict):
        raise JsonRpcError(
            INVALID_PARAMS,
            f"capabilities must be an object, got {type(params['capabilities']).__name__}",
        )

    if "clientInfo" not in params:
        raise JsonRpcError(INVALID_PARAMS, "initialize params require 'clientInfo'")
    client_info = params["clientInfo"]
    if not isinstance(client_info, dict):
        raise JsonRpcError(
            INVALID_PARAMS,
            f"clientInfo must be an object, got {type(client_info).__name__}",
        )
    for field in ("name", "version"):
        if field not in client_info:
            raise JsonRpcError(INVALID_PARAMS, f"clientInfo requires '{field}'")
        if not isinstance(client_info[field], str):
            raise JsonRpcError(
                INVALID_PARAMS,
                f"clientInfo.{field} must be a string, got "
                f"{type(client_info[field]).__name__}",
            )
    return version


def _serialize(data: dict) -> str:
    """Render the structured result as the text block's content.

    A successful tool result carries the same information twice: as
    ``structuredContent`` for clients that read it, and serialized into the
    TextContent block for those that do not. ``default=str`` keeps an
    unexpected type from turning a good answer into an internal error.
    """
    return json.dumps(data, indent=2, ensure_ascii=False, default=str, sort_keys=False)


def _tool_error(message: str) -> dict:
    """Tool failures stay plain prose: the message is the whole payload."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "isError": True,
    }


# --------------------------------------------------------------------------
# stdio transport
# --------------------------------------------------------------------------

def handle_line(server: McpServer, line: str) -> str | None:
    """Process one input line and return the line to write back, if any.

    A notification never produces output -- not a result, and not an error
    either. That second half is easy to get wrong: if dispatching a
    notification raises, the natural `except` would emit an error response with
    a null id, which is exactly the message a notification must never receive.
    ``is_notification`` is therefore captured before dispatch and checked in
    every exit path.
    """
    request_id: Any = None
    is_notification = False
    try:
        request = jsonrpc.decode(line)
        request_id = request.id
        is_notification = request.is_notification
        result = server.handle(request)
        if is_notification:
            return None
        if result is None:
            result = {}
        return jsonrpc.encode(jsonrpc.result_message(request_id, result))
    except JsonRpcError as exc:
        if is_notification:
            log(f"notification {line[:120]!r} failed: {exc.message}")
            return None
        if exc.code in (jsonrpc.PARSE_ERROR, INVALID_REQUEST) and request_id is None:
            # Malformed enough that we cannot know whether an id was intended;
            # the spec prescribes answering with a null id here.
            return jsonrpc.encode(jsonrpc.error_message(None, exc))
        return jsonrpc.encode(jsonrpc.error_message(request_id, exc))
    except Exception as exc:  # noqa: BLE001 - the loop must never die
        log(f"internal error handling {line[:200]!r}: {exc!r}")
        if is_notification:
            return None
        return jsonrpc.encode(
            jsonrpc.error_message(
                request_id, JsonRpcError(jsonrpc.INTERNAL_ERROR, "internal server error")
            )
        )


def log(message: str) -> None:
    """Diagnostics go to stderr. Never stdout."""
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def serve(config: Config, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Run the stdio loop until end of input."""
    protocol_out = stdout if stdout is not None else sys.stdout
    protocol_in = stdin if stdin is not None else sys.stdin

    replaced_stdout = None
    if stdout is None:
        # From here on, anything that writes to sys.stdout lands on stderr.
        replaced_stdout = sys.stdout
        sys.stdout = sys.stderr

    server = McpServer(config)
    log(
        f"serving {config.project_id} from {config.db_path.display} "
        f"(protocol {PREFERRED_PROTOCOL_VERSION}, read-only)"
    )
    try:
        for line in protocol_in:
            line = line.strip()
            if not line:
                continue
            response = handle_line(server, line)
            if response is not None:
                protocol_out.write(response + "\n")
                protocol_out.flush()
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        server.close()
        if replaced_stdout is not None:
            sys.stdout = replaced_stdout
    return 0
