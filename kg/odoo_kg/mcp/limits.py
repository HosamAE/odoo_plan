"""Hard limits.

Every bound the server enforces lives here so that the tool schemas, the
handlers, and the tests all read the same numbers. A caller may ask for less
than a cap but never more: values above a cap are clamped, and the response
says so rather than silently returning a truncated answer that looks complete.
"""

from __future__ import annotations

#: Result-set size for listing tools.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

#: Traversal depth for dependency/dependent/impact walks.
DEFAULT_DEPTH = 1
MAX_DEPTH = 5

#: Total nodes any single traversal may return, regardless of depth. This is
#: the backstop that keeps a wide graph from filling the client's context.
MAX_NODES = 200

#: Longest accepted free-text search string.
MAX_QUERY_LENGTH = 100

#: Longest accepted module name. Odoo technical names are far shorter; this
#: only exists to reject absurd input early.
MAX_MODULE_NAME_LENGTH = 128

#: How many entries bounded summaries include (e.g. top unresolved targets).
SUMMARY_TOP_N = 10


def clamp(value: int, low: int, high: int) -> tuple[int, bool]:
    """Clamp ``value`` into ``[low, high]``; also report whether it moved."""
    clamped = max(low, min(high, value))
    return clamped, clamped != value
