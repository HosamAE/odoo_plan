"""``__manifest__.py`` parsing.

Manifests are evaluated with :func:`ast.literal_eval`, never ``eval`` or
``import``. A manifest that contains anything other than a literal -- a
function call, a name lookup, string concatenation -- is reported as an error
instead of being executed. Indexing must never run repository code.

``literal_eval`` discards position information, so the source is parsed twice:
once as an AST to recover the line number of each ``depends`` entry, and once
through ``literal_eval`` for the values themselves. The AST pass reads line
numbers only; it never evaluates anything.
"""

from __future__ import annotations

import ast
import codecs
from dataclasses import dataclass, field

_MANIFEST_FILENAME = "__manifest__.py"


@dataclass
class Dependency:
    """One entry of the manifest's ``depends`` list."""

    name: str
    line: int


@dataclass
class ParsedManifest:
    """Outcome of parsing one manifest file."""

    ok: bool
    data: dict = field(default_factory=dict)
    dependencies: list[Dependency] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    encoding: str = "utf-8"
    dict_line: int = 1

    # -- typed accessors -------------------------------------------------
    # Odoo tolerates missing keys; the graph records what was declared and
    # what was defaulted, so downstream consumers can tell them apart.

    def get_str(self, key: str) -> str | None:
        value = self.data.get(key)
        if isinstance(value, str):
            return value.strip() or None
        if value is None:
            return None
        self.warnings.append(f"{key} is {type(value).__name__}, expected str")
        return None

    def get_bool(self, key: str, default: bool) -> tuple[bool, bool]:
        """Return ``(value, was_declared)``."""
        if key not in self.data:
            return default, False
        value = self.data[key]
        if isinstance(value, bool):
            return value, True
        self.warnings.append(f"{key} is {type(value).__name__}, expected bool")
        return bool(value), True


def parse_manifest_bytes(raw: bytes) -> ParsedManifest:
    """Parse manifest content that has already been read from disk."""
    encoding = "utf-8"
    if raw.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
        raw = raw[len(codecs.BOM_UTF8):]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        encoding = "latin-1"
        text = raw.decode("latin-1")
    result = parse_manifest_text(text)
    result.encoding = encoding
    if encoding != "utf-8":
        result.warnings.append(f"manifest decoded as {encoding}, not plain utf-8")
    return result


def parse_manifest_text(text: str) -> ParsedManifest:
    """Parse manifest source text into a :class:`ParsedManifest`."""
    result = ParsedManifest(ok=False)

    try:
        tree = ast.parse(text, filename=_MANIFEST_FILENAME, mode="eval")
    except SyntaxError as exc:
        result.errors.append(f"syntax error at line {exc.lineno}: {exc.msg}")
        return result

    body = tree.body
    if not isinstance(body, ast.Dict):
        result.errors.append(
            f"manifest must be a dict literal, found {type(body).__name__}"
        )
        return result
    result.dict_line = getattr(body, "lineno", 1)

    try:
        data = ast.literal_eval(tree)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError) as exc:
        result.errors.append(f"not a Python literal: {exc}")
        return result

    if not isinstance(data, dict):
        result.errors.append(f"manifest evaluated to {type(data).__name__}, expected dict")
        return result

    non_str_keys = [key for key in data if not isinstance(key, str)]
    if non_str_keys:
        result.warnings.append(f"non-string manifest keys ignored: {non_str_keys!r}")

    result.ok = True
    result.data = data
    result.dependencies = _extract_dependencies(body, data, result)
    return result


def _extract_dependencies(
    dict_node: ast.Dict, data: dict, result: ParsedManifest
) -> list[Dependency]:
    """Recover ``depends`` entries with their source line numbers.

    Duplicates are collapsed to their first occurrence: the graph stores one
    edge per (module, dependency) pair, so a repeated entry must not become a
    second edge and break re-index idempotency.
    """
    declared = data.get("depends")
    if declared is None:
        return []
    if not isinstance(declared, (list, tuple)):
        result.warnings.append(
            f"depends is {type(declared).__name__}, expected list; ignored"
        )
        return []

    line_by_index = _depends_line_numbers(dict_node, len(declared), result.dict_line)

    dependencies: list[Dependency] = []
    seen: set[str] = set()
    for index, entry in enumerate(declared):
        if not isinstance(entry, str):
            result.warnings.append(
                f"depends[{index}] is {type(entry).__name__}, expected str; ignored"
            )
            continue
        name = entry.strip()
        if not name:
            result.warnings.append(f"depends[{index}] is empty; ignored")
            continue
        if name in seen:
            result.warnings.append(f"duplicate dependency {name!r} collapsed")
            continue
        seen.add(name)
        dependencies.append(Dependency(name=name, line=line_by_index.get(index, result.dict_line)))
    return dependencies


def _depends_line_numbers(
    dict_node: ast.Dict, count: int, fallback_line: int
) -> dict[int, int]:
    """Map each ``depends`` element index to its line in the source."""
    for key_node, value_node in zip(dict_node.keys, dict_node.values):
        if not isinstance(key_node, ast.Constant) or key_node.value != "depends":
            continue
        if not isinstance(value_node, (ast.List, ast.Tuple)):
            return {}
        return {
            index: getattr(element, "lineno", fallback_line)
            for index, element in enumerate(value_node.elts)
            if index < count
        }
    return {}
