"""Safe XML reading for Odoo data files.

Built directly on :mod:`xml.parsers.expat` rather than on ElementTree, for two
reasons that both matter more than convenience:

* **Line numbers.** Provenance is only useful if it points somewhere. Under
  CPython 3.14 ElementTree runs in its C accelerator and never calls the Python
  hooks the usual line-tracking recipe patches, so an ElementTree-based reader
  would silently record ``None`` for every element. expat reports the current
  line for each event, so every node carries a real one.

* **Refusing DTDs outright.** ElementTree accepts a DOCTYPE with internal
  entity declarations, which is the door to entity-expansion attacks. Here the
  DOCTYPE handler raises before any entity is declared, so external DTDs,
  internal entities and billion-laughs are all refused at one place rather than
  three.

There is no network access: no handler fetches anything, and the external
entity handler returns an error rather than resolving. The reader is fed bytes
that the M1 scanner has already accepted, so it can never see a file the
exclusion rules kept out.

Malformed XML raises :class:`XmlReadError`, which the indexer records as a
diagnostic. A bad file never stops a run.
"""

from __future__ import annotations

import xml.parsers.expat as expat
from dataclasses import dataclass, field


class XmlReadError(Exception):
    """A file could not be read. ``reason`` classifies why, for diagnostics."""

    def __init__(self, reason: str, message: str, line: int | None = None):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.line = line


@dataclass
class XmlNode:
    """One element: its tag, attributes, line, text and children.

    Attribute order is whatever expat reports, which follows document order.
    Nothing here holds a reference to the parser, so a parsed tree is inert
    data and cannot be made to read anything else.
    """

    tag: str
    attrs: dict[str, str]
    line: int
    children: list["XmlNode"] = field(default_factory=list)
    text: str = ""

    def iter(self):
        """Depth-first, document order. Includes this node."""
        yield self
        for child in self.children:
            yield from child.iter()

    def get(self, name: str) -> str | None:
        return self.attrs.get(name)


def read_xml(source: bytes) -> list[XmlNode]:
    """Parse ``source`` into top-level nodes. Raises :class:`XmlReadError`.

    Returns a list rather than a single root because a file may legitimately be
    empty, and because refusing to assume one root keeps the reader honest
    about what it actually found.
    """
    roots: list[XmlNode] = []
    stack: list[XmlNode] = []
    parser = expat.ParserCreate()

    def start(tag: str, attrs: dict[str, str]) -> None:
        node = XmlNode(tag=tag, attrs=dict(attrs), line=parser.CurrentLineNumber)
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)

    def end(_tag: str) -> None:
        if stack:
            stack.pop()

    def characters(data: str) -> None:
        if stack:
            stack[-1].text += data

    def refuse_doctype(name, system_id, public_id, has_internal_subset):
        # One choke point for the whole family: an external DTD, an internal
        # entity declaration and an expansion bomb all begin here.
        raise XmlReadError(
            "dtd_rejected",
            f"refusing a document type declaration ({name!r}); odoo-kg does not "
            "process DTDs or entity declarations",
            parser.CurrentLineNumber,
        )

    def refuse_entity(*_args):
        raise XmlReadError(
            "entity_rejected",
            "refusing an entity declaration",
            parser.CurrentLineNumber,
        )

    def refuse_external(*_args) -> bool:
        # Returning False makes expat raise rather than resolve. Nothing is
        # fetched; there is no code path here that opens a URL or a file.
        return False

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    parser.StartDoctypeDeclHandler = refuse_doctype
    parser.EntityDeclHandler = refuse_entity
    parser.UnparsedEntityDeclHandler = refuse_entity
    parser.ExternalEntityRefHandler = refuse_external

    try:
        parser.Parse(source, True)
    except XmlReadError:
        raise
    except expat.ExpatError as exc:
        raise XmlReadError(
            "syntax_error",
            f"{expat.ErrorString(exc.code)} at line {exc.lineno}, column {exc.offset}",
            exc.lineno,
        ) from exc
    return roots


def classify_unparsed(source: bytes) -> str:
    """Why a file has no elements, for a file that read_xml returned empty for.

    An Odoo project accumulates files that are empty or entirely commented out.
    Both are ordinary housekeeping, not defects, and reporting them at the same
    severity as broken markup would train a reader to ignore the category.
    """
    text = source.decode("utf-8", "replace")
    if not text.strip():
        return "empty"
    stripped = text
    while True:
        start = stripped.find("<!--")
        if start < 0:
            break
        end = stripped.find("-->", start + 4)
        if end < 0:
            stripped = stripped[:start]
            break
        stripped = stripped[:start] + stripped[end + 3:]
    if not stripped.strip():
        return "commented_out"
    return "no_elements"
