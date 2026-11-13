"""Reading and editing JSON-with-comments settings files (VS Code's) without losing their comments or layout.

Pure text functions: no file is touched here.
"""
import json
import re

KG_TASK_LABELS = ("odoo-kg: build graph", "odoo-kg: status", "odoo-kg: doctor")


def strip_jsonc_comments(text):
    """Drop // and /* */ comments outside strings (VS Code files allow them)."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def loads_jsonc(text):
    """VS Code files allow comments and trailing commas; json does neither."""
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", strip_jsonc_comments(text)))


def _object_span(text, inside):
    """(start, end) of the JSON object around index `inside`, strings respected."""
    depth, i, start = 0, inside, None
    while i >= 0:
        if text[i] == "}":
            depth += 1
        elif text[i] == "{":
            if depth == 0:
                start = i
                break
            depth -= 1
        i -= 1
    if start is None:
        return None
    depth, i, in_str = 0, start, False
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return start, i + 1
        i += 1
    return None


def _cut(text, start, end):
    """Remove text[start:end] - an item of an array or a member of an object - with its comma and its own line."""
    line_start = text.rfind("\n", 0, start) + 1
    if not text[line_start:start].strip():
        start = line_start
    tail = end
    while tail < len(text) and text[tail] in " \t":
        tail += 1
    if tail < len(text) and text[tail] == ",":
        tail += 1
        while tail < len(text) and text[tail] in " \t":
            tail += 1
        if text.startswith("\r\n", tail):
            tail += 2
        elif text.startswith("\n", tail):
            tail += 1
        return text[:start] + text[tail:]
    head = start   # the last one of its list: the comma to drop is the one before it
    while head > 0 and text[head - 1] in " \t\r\n":
        head -= 1
    if head > 0 and text[head - 1] == ",":
        head -= 1
    return text[:head] + text[end:]


def remove_kg_tasks(text):
    """Drop the three odoo-kg tasks that older versions of this tool wrote. The Cockpit's Graph page builds,
    checks and keeps the graph fresh when the project opens; a task for each showed up as a separate thing."""
    removed = 0
    for label in KG_TASK_LABELS:
        while True:
            m = re.search(r'"label"\s*:\s*"' + re.escape(label) + '"', text)
            span = _object_span(text, m.start()) if m else None
            if not span:
                break
            text = _cut(text, span[0], span[1])
            removed += 1
    if removed:
        loads_jsonc(text)   # must still parse
    return text, removed


def remove_graph_registration(text):
    """VS Code's own MCP list (Copilot agent mode) no longer gets odoo-kg: it appeared there as a separate thing, and
    Claude Code reads the graph from .mcp.json. An mcp block that holds other servers keeps them."""
    m = re.search(r'"odoo-kg"\s*:\s*\{', text)
    if not m or "odoo-kg" not in loads_jsonc(text).get("settings", {}).get("mcp", {}).get("servers", {}):
        return text, []
    span = _object_span(text, m.end() - 1)
    if not span:
        return text, []
    text = _cut(text, m.start(), span[1])
    changed = ["odoo-kg removed from VS Code's MCP list"]
    block = loads_jsonc(text).get("settings", {}).get("mcp", {})
    if block.get("servers") == {} and set(block) == {"servers"}:   # nothing else in it: the block goes too
        at = re.search(r'"mcp"\s*:\s*\{', text)
        mspan = _object_span(text, at.end() - 1) if at else None
        if mspan:
            text = _cut(text, at.start(), mspan[1])
            changed.append("the empty mcp block removed")
    loads_jsonc(text)
    return text, changed


def _value_end(text, i):
    """End offset of the JSON value starting at text[i]."""
    c = text[i]
    if c in "[{":
        depth, in_str = 0, False
        while i < len(text):
            ch = text[i]
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise ValueError("unbalanced value")
    if c == '"':
        i += 1
        while text[i] != '"':
            i += 2 if text[i] == "\\" else 1
        return i + 1
    return re.compile(r"[,\n}]").search(text, i).start()


def _find_setting(text, key):
    m = re.search(r'^[ \t]*"' + re.escape(key) + r'"\s*:\s*', text, re.M)
    if not m:
        return None
    return m.start(), m.end(), _value_end(text, m.end())


def set_setting(text, key, value):
    """Set one key of "settings" to a JSON value; keeps the rest of the file as written."""
    rendered = json.dumps(value, ensure_ascii=False)
    found = _find_setting(text, key)
    if found:
        _, vs, ve = found
        try:
            if loads_jsonc('{"v": ' + text[vs:ve] + "}")["v"] == value:
                return text, False
        except ValueError:
            pass
        return text[:vs] + rendered + text[ve:], True
    m = re.search(r'"settings"\s*:\s*\{', text)
    return text[:m.end()] + "\n    " + f'"{key}": {rendered},' + text[m.end():], True


def drop_setting(text, key):
    found = _find_setting(text, key)
    if not found:
        return text, False
    start, _, ve = found
    end = re.compile(r"[ \t]*,?[ \t]*\r?\n?").match(text, ve).end()
    return text[:start] + text[end:], True


#: Extensions the workspaces must not propose (owner's decisions): Copilot (2026-11-08,
#: "the message must not show again") and the old Odoo extension OdooLS replaced.
