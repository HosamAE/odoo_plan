"""JSON-RPC 2.0 framing for the stdio transport.

Kept deliberately free of MCP semantics so the wire format can be tested on its
own. The stdio framing rule that matters: one JSON object per line, no embedded
newlines, and nothing but protocol on stdout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Standard JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: Server-defined: a request arrived before `initialize`. Outside the reserved
#: -32768..-32000 pre-defined range's standard codes, in the implementation-
#: defined server-error band.
NOT_INITIALIZED = -32002


class JsonRpcError(Exception):
    """An error that belongs in the JSON-RPC `error` member."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_error(self) -> dict:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return error


@dataclass(frozen=True)
class Request:
    """One decoded JSON-RPC request or notification."""

    method: str
    params: dict
    id: Any = None
    is_notification: bool = False


def decode(line: str) -> Request:
    """Decode one line into a :class:`Request`, or raise :class:`JsonRpcError`."""
    try:
        payload = json.loads(line)
    except ValueError as exc:
        raise JsonRpcError(PARSE_ERROR, f"invalid JSON: {exc}") from exc

    if isinstance(payload, list):
        # Batching was removed from MCP; rejecting it explicitly beats
        # half-supporting it.
        raise JsonRpcError(INVALID_REQUEST, "JSON-RPC batching is not supported")
    if not isinstance(payload, dict):
        raise JsonRpcError(INVALID_REQUEST, "request must be a JSON object")
    if payload.get("jsonrpc") != "2.0":
        raise JsonRpcError(INVALID_REQUEST, "jsonrpc must be exactly '2.0'")

    method = payload.get("method")
    if not isinstance(method, str) or not method:
        raise JsonRpcError(INVALID_REQUEST, "method must be a non-empty string")

    params = payload.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        # MCP only uses by-name parameters; positional params are not valid here.
        raise JsonRpcError(INVALID_PARAMS, "params must be an object")

    # Absent id means notification. A null id is not a valid request id either,
    # so it is treated the same way: no response is sent.
    has_id = "id" in payload and payload["id"] is not None
    return Request(
        method=method,
        params=params,
        id=payload.get("id"),
        is_notification=not has_id,
    )


def result_message(request_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_message(request_id: Any, error: JsonRpcError) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": error.to_error()}


def encode(message: dict) -> str:
    """Serialise one message to a single line.

    ``ensure_ascii`` keeps the payload newline-free and byte-safe on a Windows
    console whose code page may not be UTF-8.
    """
    return json.dumps(message, ensure_ascii=True, separators=(",", ":"))
