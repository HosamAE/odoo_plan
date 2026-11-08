"""Reading documentation as data, never as instruction.

The rule this module exists to enforce
--------------------------------------
Everything it returns is **untrusted content**. A document in the repository is
written by people, sometimes pasted from elsewhere, and a knowledge graph that
fed it back as if it were guidance would be a way to steer whoever reads the
graph. So:

* Nothing here executes. No Markdown is rendered, no HTML is parsed, no
  JavaScript is run, no link is followed, no external resource is fetched. The
  input is decoded to text and matched with regular expressions.
* Text that looks like an instruction to a reader -- "ignore previous
  instructions", a ``<script>`` tag, a ``javascript:`` URL -- is *stored*, and
  flagged, and never obeyed. Flagging is a service to the caller, not a filter:
  the excerpt still comes through, marked.
* Excerpts are bounded. A section returns at most :data:`MAX_EXCERPT_CHARS`
  characters, so no answer can be made large by writing a large document.

What a heading is
-----------------
ATX (``## Title``) and setext (``Title`` over ``-----``) for Markdown, and the
underline convention for reStructuredText. A section runs from its heading to
the next heading of the same or higher level, which is what makes "the section
about X" a range a reader can open rather than a whole file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

#: The most text any single excerpt may carry. A caller asking about a section
#: gets its opening, never its entirety: the graph points at documents, it does
#: not replace reading them.
MAX_EXCERPT_CHARS = 480

#: What a truncated excerpt ends with. Counted against the ceiling rather than
#: added on top of it, so ``len(excerpt) <= MAX_EXCERPT_CHARS`` always holds.
TRUNCATION_SUFFIX = " ..."

#: Shapes that read as an instruction rather than as prose. Matching one says
#: nothing about intent -- a security document may quote an attack verbatim --
#: so the finding is advisory and the text is kept either way.
INJECTION_SHAPES = (
    ("instruction_override", re.compile(
        r"ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+instructions?"
        r"|disregard\s+(?:the\s+)?(?:above|previous|prior)"
        r"|forget\s+(?:everything|all)\s+(?:above|before)", re.IGNORECASE)),
    ("role_reassignment", re.compile(
        r"you\s+are\s+now\s+(?:a|an|the)\b|act\s+as\s+(?:a|an|the)\b"
        r"|system\s*prompt|<\s*\|?\s*(?:system|assistant|user)\s*\|?\s*>",
        re.IGNORECASE)),
    ("embedded_script", re.compile(
        r"<\s*script\b|javascript\s*:|data:text/html|on(?:error|load)\s*=",
        re.IGNORECASE)),
    ("tool_directive", re.compile(
        r"\b(?:run|execute|eval)\s+(?:this|the following)\s+(?:code|command)"
        r"|curl\s+-s\s+https?://", re.IGNORECASE)),
)

_ATX = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SETEXT_UNDERLINE = re.compile(r"^(=+|-+)\s*$")
#: reStructuredText section underlines. Any of these punctuation runs.
_RST_UNDERLINE = re.compile(r"^([=\-`:.'\"~^_*+#])\1{2,}\s*$")

#: ``A -> B -> C``, in the three arrow spellings the repository actually uses.
_ARROW = re.compile(r"\s*(?:->|=>|→)\s*")
_ARROW_PRESENT = re.compile(r"->|=>|→")

#: A step label: short, human, and not a sentence. Anything longer is prose
#: that happens to contain an arrow, and turning it into a process step would
#: invent a process nobody wrote.
MAX_STEP_LABEL_CHARS = 40
MIN_CHAIN_STEPS = 2
MAX_CHAIN_STEPS = 12


class DocReadError(Exception):
    """The document could not be decoded. Never raised for odd content."""


@dataclass
class Section:
    """One heading and the text under it.

    Two representations of the same text, kept strictly apart:

    ``excerpt``
        Bounded, stored in the graph, returned by MCP. A presentation of the
        section, never the section.
    ``search_text``
        The whole section, normalised, held only while the indexer is running.
        Used to decide what the section is *about* and then dropped. It is
        never written to the database, never returned by a tool, never logged
        and never put in a diagnostic.

    Deciding what a section documents from the excerpt was the defect this
    split exists to prevent: 53% of the sections in testing19 are longer than
    the excerpt, so a display bound was silently deciding which references the
    graph could see. ``repr=False`` keeps the body out of a traceback or a
    debug print, which is one of the few ways transient text leaks.
    """

    level: int
    title: str
    anchor: str
    line_start: int
    line_end: int
    excerpt: str
    excerpt_truncated: bool
    char_count: int
    injection_flags: tuple[str, ...] = ()
    search_text: str = dc_field(default="", repr=False, compare=False)


@dataclass
class ArrowChain:
    """A ``A -> B -> C`` line, kept as the labels its author wrote."""

    steps: tuple[str, ...]
    line: int
    raw: str
    section_anchor: str | None = None
    #: What the author wrote before the colon, if anything: "Appraisal
    #: lifecycle", "Statusflöde". Used to name the process.
    label_prefix: str = ""
    #: Everything on the line before the first arrow. Wider than the prefix on
    #: purpose: "- 17:s statusrad (Quotation -> ..." announces a flow in text
    #: the colon split would have thrown away.
    context: str = ""


@dataclass
class Document:
    title: str | None
    line_count: int
    char_count: int
    sections: list[Section] = dc_field(default_factory=list)
    chains: list[ArrowChain] = dc_field(default_factory=list)
    injection_flags: tuple[str, ...] = ()


def anchor_for(title: str, seen: dict[str, int]) -> str:
    """A stable, readable id for a heading, unique within its document."""
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "section"
    count = seen.get(base, 0)
    seen[base] = count + 1
    return base if count == 0 else f"{base}-{count}"


def scan_injection(text: str) -> tuple[str, ...]:
    """Which instruction-like shapes appear. Advisory, never a filter."""
    return tuple(
        name for name, pattern in INJECTION_SHAPES if pattern.search(text)
    )


#: Characters that look like nothing and break an exact match. A model name
#: split by a soft hyphen or padded with a non-breaking space is the same model
#: name to a reader, so it must be the same to the matcher.
_INVISIBLES = {
    " ": " ",   # non-breaking space
    " ": " ",   # figure space
    " ": " ",   # narrow no-break space
    "​": "",    # zero-width space
    "‌": "",    # zero-width non-joiner
    "‍": "",    # zero-width joiner
    "­": "",    # soft hyphen
    "﻿": "",    # byte-order mark
}


def normalise_search_text(text: str) -> str:
    """The section's own text, made safe to match exactly against.

    Deliberately conservative. Line breaks are kept and runs of whitespace are
    not collapsed, because collapsing would join two adjacent words into a
    token that appears nowhere in the document -- inventing a reference is
    worse than missing one. Case is kept because every identifier this matches
    is case-sensitive. All that changes is that invisible characters stop
    hiding a name from an exact match.

    This is untrusted content, exactly like the excerpt. It is matched against
    and then discarded; nothing here is executed, followed, or fetched.
    """
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    for character, replacement in _INVISIBLES.items():
        if character in normalised:
            normalised = normalised.replace(character, replacement)
    return normalised


def excerpt_of(lines, start_index: int, end_index: int) -> tuple[str, bool, int]:
    """The opening of a section, bounded. Returns (text, truncated, full_size).

    The result is never longer than :data:`MAX_EXCERPT_CHARS`, suffix included.
    The suffix used to be appended after the cut, so a truncated excerpt came
    out at up to ``MAX_EXCERPT_CHARS + 4`` -- a ceiling that the truncation
    itself broke. It is budgeted for now instead.
    """
    body = "\n".join(lines[start_index:end_index]).strip()
    full = len(body)
    if full <= MAX_EXCERPT_CHARS:
        return body, False, full
    budget = MAX_EXCERPT_CHARS - len(TRUNCATION_SUFFIX)
    # Cut on a word boundary so the excerpt reads as text rather than as a
    # string that was chopped.
    cut = body[:budget]
    space = cut.rfind(" ")
    if space > budget // 2:
        cut = cut[:space]
    return cut.rstrip() + TRUNCATION_SUFFIX, True, full


def _heading_positions(lines, kind: str):
    """(line_index, level, title) for every heading, in document order."""
    found = []
    in_fence = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        # A fenced code block can contain anything, including '# not a heading'.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        if kind == "md":
            match = _ATX.match(line)
            if match:
                found.append((index, len(match.group(1)), match.group(2).strip()))
                continue
            if index and _SETEXT_UNDERLINE.match(line) and lines[index - 1].strip() \
                    and not _ATX.match(lines[index - 1]):
                level = 1 if line.strip().startswith("=") else 2
                found.append((index - 1, level, lines[index - 1].strip()))
        else:
            if index and _RST_UNDERLINE.match(line) and lines[index - 1].strip():
                character = line.strip()[0]
                # reStructuredText has no fixed level per character; the order
                # of first appearance defines the hierarchy.
                found.append((index - 1, character, lines[index - 1].strip()))

    if kind == "rst":
        order: dict[str, int] = {}
        levelled = []
        for index, character, title in found:
            if character not in order:
                order[character] = len(order) + 1
            levelled.append((index, order[character], title))
        return levelled
    return found


def read_document(source: bytes, *, kind: str = "md") -> Document:
    """Split a document into sections with bounded excerpts.

    ``kind`` is ``md`` or ``rst``. Anything else is read as plain text: one
    section covering the file, so a ``.txt`` still gets an excerpt and a size.
    """
    try:
        text = source.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = source.decode("latin-1")
        except UnicodeDecodeError as exc:      # pragma: no cover - defensive
            raise DocReadError(f"cannot decode: {exc}") from exc

    lines = text.splitlines()
    document = Document(
        title=None, line_count=len(lines), char_count=len(text),
        injection_flags=scan_injection(text),
    )

    headings = _heading_positions(lines, kind) if kind in ("md", "rst") else []
    if not headings:
        body, truncated, full = excerpt_of(lines, 0, len(lines))
        whole = "\n".join(lines)
        document.sections.append(Section(
            level=1, title="(no headings)", anchor="document",
            line_start=1, line_end=len(lines) or 1, excerpt=body,
            excerpt_truncated=truncated, char_count=full,
            injection_flags=scan_injection(whole),
            search_text=normalise_search_text(whole),
        ))
    else:
        seen: dict[str, int] = {}
        for position, (index, level, title) in enumerate(headings):
            # A section ends where the next heading of the same or higher rank
            # begins; a deeper heading is part of it.
            end = len(lines)
            for later_index, later_level, _title in headings[position + 1:]:
                if later_level <= level:
                    end = later_index
                    break
            body, truncated, full = excerpt_of(lines, index + 1, end)
            # The heading line included: a section is about what its title
            # names as much as what its body says. The slice stops at `end`,
            # which is where the next section of the same or higher rank
            # begins, so a reference in the next section stays in the next
            # section.
            whole = "\n".join(lines[index:end])
            document.sections.append(Section(
                level=level, title=title, anchor=anchor_for(title, seen),
                line_start=index + 1, line_end=end,
                excerpt=body, excerpt_truncated=truncated, char_count=full,
                injection_flags=scan_injection(whole),
                search_text=normalise_search_text(whole),
            ))
        top = [s for s in document.sections if s.level == 1]
        document.title = (top[0].title if top else document.sections[0].title)

    document.chains = _arrow_chains(lines, document.sections)
    return document


#: A step that names a file is a mapping, not a stage of a process.
_FILE_STEP = re.compile(r"\.(?:py|js|ts|xml|csv|json|md|po|pot|scss|css)$",
                        re.IGNORECASE)


def _looks_like_a_sequence(line: str, parts) -> bool:
    """Whether an arrow line reads as stages rather than as a mapping.

    Documents use the same arrow for two different jobs. ``Draft -> Confirmed
    -> Done`` is a sequence of states. ``name -> display_label,
    number_of_people -> capacity`` is a table of field renames that happens to
    share the notation, and a porting plan is full of them. Turning the second
    into a business process would put stages into the graph that describe
    nothing anyone does.

    Three tells separate them: a comma inside a step means the line is really a
    list of pairs; a step naming a source file means it is a file mapping; and
    a heading is a title, not a step sequence.
    """
    if line.lstrip().startswith("#"):
        return False
    for part in parts:
        if "," in part or ";" in part:
            return False
        if _FILE_STEP.search(part.strip()):
            return False
    return True


def _arrow_chains(lines, sections) -> list[ArrowChain]:
    """Every ``A -> B -> C`` line, with the section it sits in."""
    by_line = {}
    for section in sections:
        for line_number in range(section.line_start, section.line_end + 1):
            # Plain assignment, not setdefault: sections arrive in document
            # order, so a nested one comes later and overwrites its parent.
            # A line belongs to the most specific section containing it.
            by_line[line_number] = section.anchor

    chains = []
    for index, line in enumerate(lines, 1):
        if not _ARROW_PRESENT.search(line):
            continue
        # Strip Markdown decoration and any leading list marker or label, so
        # "- **Appraisal lifecycle**: To Start -> Done" yields the steps.
        cleaned = line.strip()
        cleaned = re.sub(r"^[-*+]\s+", "", cleaned)
        cleaned = re.sub(r"^\d+[.)]\s+", "", cleaned)
        cleaned = cleaned.replace("**", "").replace("`", "")
        prefix = ""
        if ":" in cleaned and _ARROW_PRESENT.search(cleaned.split(":", 1)[1]):
            prefix, cleaned = (part.strip() for part in cleaned.split(":", 1))
        parts = [part.strip(" .;,|") for part in _ARROW.split(cleaned)]
        parts = [part for part in parts if part]
        if not (MIN_CHAIN_STEPS <= len(parts) <= MAX_CHAIN_STEPS):
            continue
        if any(len(part) > MAX_STEP_LABEL_CHARS for part in parts):
            # Prose that happens to contain an arrow. Recording it would
            # manufacture a process out of a sentence.
            continue
        if not _looks_like_a_sequence(line, parts):
            continue
        before_arrow = _ARROW_PRESENT.split(line, 1)[0]
        chains.append(ArrowChain(
            steps=tuple(parts), line=index, raw=line.strip()[:200],
            section_anchor=by_line.get(index), label_prefix=prefix[:80],
            context=before_arrow.strip()[:160],
        ))
    return chains


#: Words that make a line an announcement of a flow rather than a sentence
#: containing an arrow. Swedish and English, because the repository uses both.
PROCESS_LABEL_WORDS = re.compile(
    r"\b(?:lifecycle|livscykel|workflow|arbetsfl[oö]de|statusfl[oö]de|"
    r"fl[oö]de|process|cykel|cycle|steg|steps?|stadie[rn]?|"
    r"status(?:rad|bar|flow)?|tillst[aå]nd|states?|fas(?:er|en)?|phases?|"
    r"pipeline|portal)\b", re.IGNORECASE)


def announces_a_process(chain: "ArrowChain", section_title: str = "") -> bool:
    """Whether a human labelled this line, or its section, as a flow.

    Three places are consulted, widest last: the text before the colon, the
    whole run-up to the first arrow, and the title of the section the line sits
    in. A chain under a heading called "Statusflöde" is announced even when the
    line itself is bare, and ``- 17:s statusrad (Quotation -> ...`` is announced
    by text the colon split would have thrown away.

    None of this is proof that a business process exists. It is the difference
    between a sentence that happens to contain an arrow and a line somebody
    wrote in order to describe a sequence -- which is the least a document must
    do before this graph will call something a process.
    """
    for text in (chain.label_prefix, chain.context, section_title):
        if text and PROCESS_LABEL_WORDS.search(text):
            return True
    return False


def normalise_label(label: str) -> str:
    """A label reduced for comparison: lowercase, alphanumerics only.

    Used to line a documented step up with a Selection value or its label.
    Comparison happens on this form; the original is always what is stored and
    shown, because the author's wording is the evidence.
    """
    cleaned = re.sub(r"\([^)]*\)", " ", label)
    cleaned = re.sub(r"[^0-9a-zA-ZåäöÅÄÖ]+", " ", cleaned)
    return " ".join(cleaned.lower().split())
