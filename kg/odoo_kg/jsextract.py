"""Odoo shapes recognised in a JavaScript token stream.

Everything here works on the tokens :mod:`odoo_kg.jsread` produces, so a
pattern can never match inside a comment or a string body -- which is the whole
reason the lexer exists. An Odoo codebase is full of lines like
``// registry.category("old_thing")`` and of strings containing example code.

What is recognised, and nothing else:

* ``import ... from "spec"`` and bare ``import "spec"``
* ``export class Name extends Base``
* ``static template = "module.Name"``
* ``registry.category("cat")`` -- directly, or through a ``const x =`` alias
* ``.add("key")`` on such a category
* ``patch(Target.prototype, ...)`` and ``patch(target, ...)``
* ``useService("name")``

Everything produced here is **heuristic**. A lexer is not a grammar: it cannot
tell an ``import`` statement from the word ``import`` used as a property name,
and it does not know scope. The shapes above are unambiguous enough in practice
to be worth recording, and calling them heuristic is what keeps a reader from
treating them as parsed facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .jsread import NAME, STRING, Token, at, dotted_name, is_name, is_punct

#: Base classes that make a class an OWL component. Odoo's own is ``Component``
#: from ``@odoo/owl``; project code routinely extends an intermediate class, so
#: the base is recorded verbatim and the "is a component" judgement is made
#: from the name alone.
OWL_BASES = frozenset({"Component"})


@dataclass
class JsClass:
    name: str
    base: str | None
    line: int
    template: str | None = None

    @property
    def is_component(self) -> bool:
        return bool(self.base) and self.base.split(".")[-1] in OWL_BASES


@dataclass
class JsRegistration:
    category: str
    key: str | None
    line: int


@dataclass
class JsFacts:
    module_annotation: bool = False
    imports: list[tuple[str, int]] = field(default_factory=list)
    classes: list[JsClass] = field(default_factory=list)
    registrations: list[JsRegistration] = field(default_factory=list)
    patches: list[tuple[str, int]] = field(default_factory=list)
    services: list[tuple[str, int]] = field(default_factory=list)
    #: Every string literal, with its line. Used only to look for references to
    #: things this graph already knows -- a model name, a route path -- never to
    #: invent one.
    strings: list[tuple[str, int]] = field(default_factory=list)


def extract(tokens: list[Token], raw_head: str = "") -> JsFacts:
    """Read the Odoo shapes out of a token stream."""
    facts = JsFacts()
    facts.module_annotation = "@odoo-module" in raw_head

    #: const NAME = registry.category("cat") -- so a later NAME.add("key") can
    #: be attributed to the right category.
    aliases: dict[str, str] = {}

    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]

        if token.kind == STRING:
            facts.strings.append((token.value, token.line))
            index += 1
            continue

        if token.kind != NAME:
            index += 1
            continue

        if token.value == "import":
            index = _read_import(tokens, index, facts)
            continue

        if token.value == "class":
            index = _read_class(tokens, index, facts)
            continue

        if token.value == "static":
            index = _read_static_member(tokens, index, facts)
            continue

        if token.value in ("const", "let", "var"):
            index = _read_alias(tokens, index, aliases)
            continue

        if token.value == "registry":
            index = _read_registry_chain(tokens, index, facts, aliases)
            continue

        if token.value == "patch" and is_punct(at(tokens, index + 1), "("):
            target, _ = dotted_name(tokens, index + 2)
            if target:
                facts.patches.append((target, token.line))
            index += 2
            continue

        if token.value == "useService" and is_punct(at(tokens, index + 1), "("):
            argument = at(tokens, index + 2)
            if argument is not None and argument.kind == STRING:
                facts.services.append((argument.value, token.line))
            index += 3
            continue

        # A bare `alias.add("key")` for a category captured earlier.
        if token.value in aliases and is_punct(at(tokens, index + 1), ".") \
                and is_name(at(tokens, index + 2), "add") \
                and is_punct(at(tokens, index + 3), "("):
            key = at(tokens, index + 4)
            facts.registrations.append(
                JsRegistration(
                    category=aliases[token.value],
                    key=key.value if key is not None and key.kind == STRING else None,
                    line=token.line,
                )
            )
            index += 5
            continue

        index += 1
    return facts


def _read_import(tokens, index, facts: JsFacts) -> int:
    """``import ... from "spec"`` or ``import "spec"``, on one logical line."""
    line = tokens[index].line
    cursor = index + 1
    limit = min(len(tokens), cursor + 80)
    # `import` used as a property (`x.import`) is not a statement; the previous
    # token being a dot rules that out.
    if is_punct(at(tokens, index - 1), "."):
        return index + 1
    while cursor < limit:
        token = tokens[cursor]
        if token.kind == STRING:
            facts.imports.append((token.value, line))
            return cursor + 1
        if token.kind == NAME and token.value in ("class", "const", "let", "var", "function"):
            break
        if is_punct(token, ";"):
            break
        cursor += 1
    return index + 1


def _read_class(tokens, index, facts: JsFacts) -> int:
    """``class Name extends Base``. The base may be dotted."""
    name_token = at(tokens, index + 1)
    if name_token is None or name_token.kind != NAME:
        return index + 1
    cursor = index + 2
    base = None
    if is_name(at(tokens, cursor), "extends"):
        base, cursor = dotted_name(tokens, cursor + 1)
    facts.classes.append(
        JsClass(name=name_token.value, base=base, line=tokens[index].line)
    )
    return cursor


def _read_static_member(tokens, index, facts: JsFacts) -> int:
    """``static template = "..."`` inside the most recent class."""
    member = at(tokens, index + 1)
    if member is None or member.kind != NAME or member.value != "template":
        return index + 1
    if not is_punct(at(tokens, index + 2), "="):
        return index + 1
    value = at(tokens, index + 3)
    if value is not None and value.kind == STRING and facts.classes:
        facts.classes[-1].template = value.value
    return index + 4


def _read_alias(tokens, index, aliases: dict[str, str]) -> int:
    """``const x = registry.category("cat")`` -- remember x means cat."""
    name_token = at(tokens, index + 1)
    if name_token is None or name_token.kind != NAME:
        return index + 1
    if not is_punct(at(tokens, index + 2), "="):
        return index + 1
    category = _category_at(tokens, index + 3)
    if category is not None:
        aliases[name_token.value] = category
    return index + 3


def _category_at(tokens, index) -> str | None:
    """``registry.category("cat")`` starting exactly at ``index``."""
    if not is_name(at(tokens, index), "registry"):
        return None
    if not (is_punct(at(tokens, index + 1), ".")
            and is_name(at(tokens, index + 2), "category")
            and is_punct(at(tokens, index + 3), "(")):
        return None
    argument = at(tokens, index + 4)
    if argument is None or argument.kind != STRING:
        return None
    return argument.value


def _read_registry_chain(tokens, index, facts: JsFacts, aliases) -> int:
    """``registry.category("cat")`` and an ``.add("key")`` chained onto it."""
    category = _category_at(tokens, index)
    if category is None:
        return index + 1
    cursor = index + 6  # registry . category ( "cat" )
    key = None
    if is_punct(at(tokens, cursor), ".") and is_name(at(tokens, cursor + 1), "add") \
            and is_punct(at(tokens, cursor + 2), "("):
        argument = at(tokens, cursor + 3)
        if argument is not None and argument.kind == STRING:
            key = argument.value
        cursor += 4
    facts.registrations.append(
        JsRegistration(category=category, key=key, line=tokens[index].line)
    )
    return cursor
