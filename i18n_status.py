"""Translations, for a person: what is left to translate in a module, and (when you have a model of your own) doing it.

  status      per module and language: how many terms, how many translated, how many missing, whether nothing is left
  translate   fill the empty msgstr of one language with a model of YOUR choice (an OpenAI-compatible endpoint:
              a local Ollama or LM Studio costs nothing, any hosted one works with its key)

The terms are the ones this scanner finds (see `terms`): translated strings in Python and XML, field labels and help,
selection values, model descriptions, and the text of views. It is the same scanner `update` and `export` use, so the
three always agree. It is not Odoo's own export (that needs an installed database): a term only Odoo finds is not counted.

Nothing is sent anywhere unless `translate` is run, and it sends the module's untranslated terms to the endpoint it is
given and nowhere else. The key is read from the environment (HX_TRANSLATE_KEY), never from the command line.
"""
import argparse
import ast
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import i18n_sync

def _read(path):
    with open(path, encoding="utf-8", errors="ignore") as handle:
        return handle.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


FIELD_KEYS = {"string", "help"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", "tests", "lib"}
PLACEHOLDER = re.compile(r"%\([\w.]+\)[sdif]|%[sdif]|\{\{.*?\}\}|\{[\w.]*\}|</?[\w-]+[^>]*>")


# ----------------------------------------------------------------------------- terms

def _py_terms(path):
    """Field labels and help, selection values, model descriptions: what Odoo puts in the .pot besides _()."""
    found = []
    try:
        tree = ast.parse(_read(path))
    except (SyntaxError, ValueError):
        return found

    def add(node, line=None):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip():
            found.append((node.value.strip(), line or node.lineno))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_description" for t in node.targets):
            add(node.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "fields":
            for kw in node.keywords:
                if kw.arg in FIELD_KEYS:
                    add(kw.value)
            if node.func.attr in ("Char", "Text", "Html", "Integer", "Float", "Boolean", "Date", "Datetime", "Monetary") \
                    and node.args:
                add(node.args[0])
            if node.func.attr == "Selection":
                values = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "selection"), None)
                if isinstance(values, (ast.List, ast.Tuple)):
                    for pair in values.elts:
                        if isinstance(pair, (ast.Tuple, ast.List)) and len(pair.elts) == 2:
                            add(pair.elts[1])
    return found


def _xml_text_terms(path):
    """The visible text of views and templates (the attributes are already found by i18n_sync)."""
    found = []
    try:
        root = ET.fromstring(_read(path))
    except ET.ParseError:
        return found
    for elem in root.iter():
        if elem.tag in ("script", "style") or elem.tag.startswith("{"):
            continue
        for text in (elem.text, elem.tail):
            value = " ".join((text or "").split())
            if len(value) > 1 and re.search(r"[^\W\d_]", value) and not value.startswith(("<", "{", "%", "(")) \
                    and elem.tag in ("span", "div", "p", "h1", "h2", "h3", "h4", "label", "button", "a", "li", "td", "th",
                                     "strong", "b", "i", "em", "small", "t", "group", "page", "separator", "title"):
                found.append((value, 1))
    return found


def terms(module_dir):
    """{term: [(file, line)]} for a module: i18n_sync's terms plus the field labels and the view text."""
    result = i18n_sync.scan_module_terms(module_dir)
    for folder, dirs, files in os.walk(module_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, module_dir)
            if name.endswith(".py") and not name.startswith("test_"):
                for term, line in _py_terms(path):
                    result.setdefault(term, []).append((rel, line))
            elif name.endswith(".xml") and "static" not in rel.split(os.sep)[:1]:
                for term, line in _xml_text_terms(path):
                    result.setdefault(term, []).append((rel, line))
    return result


# ----------------------------------------------------------------------------- .po files

ENTRY = re.compile(r'^(?P<head>(?:#[^\n]*\n)*)(?:msgctxt\s+(?:"(?:[^"\\]|\\.)*"\s*)+)?msgid\s+(?P<id>(?:"(?:[^"\\]|\\.)*"\s*)+)msgstr\s+(?P<str>(?:"(?:[^"\\]|\\.)*"\s*)+)',
                   re.M)


def _unquote(chunks):
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', chunks)
    return "".join(parts).replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")


def _quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def read_po(path):
    """{msgid: (msgstr, fuzzy)}; the header entry (empty msgid) is not a term."""
    if not os.path.isfile(path):
        return {}
    text = _read(path)
    entries = {}
    for m in ENTRY.finditer(text):
        msgid = _unquote(m.group("id"))
        if msgid:
            entries[msgid] = (_unquote(m.group("str")), "#, fuzzy" in m.group("head"))
    return entries


def write_translations(path, translations):
    """Put msgstr into the entries that are empty; every other byte of the file stays as it was."""
    text = _read(path)

    def fill(m):
        msgid = _unquote(m.group("id"))
        if msgid in translations and not _unquote(m.group("str")).strip():
            trailing = re.search(r"\s*$", m.group("str")).group(0)
            return m.group(0)[:m.start("str") - m.start()] + _quote(translations[msgid]) + trailing
        return m.group(0)
    _write(path, ENTRY.sub(fill, text))


def language_files(module_dir):
    folder = os.path.join(module_dir, "i18n")
    return sorted(f[:-3] for f in os.listdir(folder) if f.endswith(".po")) if os.path.isdir(folder) else []


# ----------------------------------------------------------------------------- status

def language_status(module_terms, module_dir, lang):
    path = os.path.join(module_dir, "i18n", f"{lang}.po")
    entries = read_po(path)
    total = len(module_terms)
    translated = sum(1 for t in module_terms if entries.get(t, ("", False))[0].strip() and not entries[t][1])
    fuzzy = sum(1 for t in module_terms if t in entries and entries[t][1])
    missing = total - translated
    return {
        "lang": lang, "file": path if os.path.isfile(path) else None, "exists": os.path.isfile(path), "total": total,
        "translated": translated, "missing": missing, "fuzzy": fuzzy,
        "obsolete": sum(1 for t in entries if t not in module_terms),
        "percent": 100 if total == 0 else translated * 100 // total, "complete": total > 0 and missing == 0,
    }


def module_status(module_dir, languages=()):
    found = terms(module_dir)
    langs = sorted(set(language_files(module_dir)) | set(languages))
    return {"module": os.path.basename(module_dir), "path": module_dir, "terms": len(found),
            "languages": [language_status(found, module_dir, lang) for lang in langs]}


def project_modules(root):
    names = []
    for entry in sorted(os.listdir(root)):
        if os.path.isfile(os.path.join(root, entry, "__manifest__.py")):
            names.append(entry)
    return names


# ----------------------------------------------------------------------------- translate

class TranslateError(Exception):
    pass


def placeholders(text):
    return sorted(PLACEHOLDER.findall(text))


def ask_model(endpoint, model, key, lang, batch, timeout=180):
    """{index: translation} for a batch of terms, from an OpenAI-compatible chat endpoint."""
    prompt = (
        f"Translate these user-interface strings of an Odoo module into the language with code {lang}. "
        "Keep every placeholder exactly as it is (%s, %(name)s, {name}, HTML tags). Reply with ONLY a JSON object "
        "that maps each number to its translation.\n" + json.dumps({str(i): t for i, t in enumerate(batch)}, ensure_ascii=False))
    body = json.dumps({"model": model, "temperature": 0, "messages": [
        {"role": "system", "content": "You are a careful translator of software interfaces."},
        {"role": "user", "content": prompt}]}).encode("utf-8")
    request = urllib.request.Request(endpoint.rstrip("/") + "/chat/completions", data=body,
                                     headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read().decode("utf-8"))
        content = answer["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
        data = json.loads(content)
    except (urllib.error.URLError, OSError) as exc:
        raise TranslateError(f"cannot reach the translator ({getattr(exc, 'reason', exc)})") from exc
    except (KeyError, IndexError, ValueError) as exc:
        raise TranslateError("the translator did not answer in the expected form") from exc
    return {int(k): str(v) for k, v in data.items() if str(k).isdigit()}


def translate_module(module_dir, lang, endpoint, model, key, batch_size=20, ask=ask_model, progress=None):
    """Fill the empty msgstr of one language. Returns {translated, skipped}; a translation that loses a placeholder is skipped."""
    path = os.path.join(module_dir, "i18n", f"{lang}.po")
    if not os.path.isfile(path):
        raise TranslateError(f"there is no {lang}.po yet: add the language first")
    found = terms(module_dir)
    entries = read_po(path)
    todo = [t for t in found if t in entries and not entries[t][0].strip()]
    done, skipped = {}, []
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        answer = ask(endpoint, model, key, lang, batch)
        for index, source in enumerate(batch):
            target = (answer.get(index) or "").strip()
            if target and placeholders(source) == placeholders(target):
                done[source] = target
            else:
                skipped.append(source)
        if progress:
            progress(min(start + batch_size, len(todo)), len(todo))
    if done:
        write_translations(path, done)
    return {"translated": len(done), "skipped": skipped}


# ----------------------------------------------------------------------------- the commands

def add_commands(subparsers):
    st = subparsers.add_parser("status", help="what is left to translate, per module and language (--json)")
    st.add_argument("--path", default=".", help="the project folder")
    st.add_argument("-m", "--module", help="one module (default: every module of the project)")
    st.add_argument("-l", "--lang", default="", help="languages to include even if there is no .po yet (comma separated)")
    st.add_argument("--json", action="store_true")
    tr = subparsers.add_parser("translate", help="fill a language's empty msgstr with a model of your own")
    tr.add_argument("-m", "--module", required=True)
    tr.add_argument("-l", "--lang", required=True)
    tr.add_argument("--path", default=".")
    tr.add_argument("--endpoint", required=True, help="an OpenAI-compatible base URL, e.g. http://localhost:11434/v1")
    tr.add_argument("--model", required=True)
    tr.add_argument("--json", action="store_true")


def run(args):
    root = os.path.abspath(args.path)
    languages = [x for x in (getattr(args, "lang", "") or "").split(",") if x.strip()] if args.action == "status" else []
    if args.action == "status":
        names = [args.module] if args.module else project_modules(root)
        data = {"modules": [module_status(os.path.join(root, n), languages) for n in names if os.path.isdir(os.path.join(root, n))]}
        if args.json:
            print(json.dumps(data))
        else:
            for m in data["modules"]:
                print(f"{m['module']}: {m['terms']} terms")
                for item in m["languages"]:
                    print(f"  {item['lang']:8} {'OK - nothing left' if item['complete'] else str(item['missing']) + ' missing'}")
        return 0
    module_dir = os.path.join(root, args.module)
    try:
        result = translate_module(module_dir, args.lang, args.endpoint, args.model, os.environ.get("HX_TRANSLATE_KEY", ""),
                                  progress=lambda a, b: print(f"STEP {a}/{b} translating", file=sys.stderr, flush=True))
    except TranslateError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}) if args.json else f"ERROR: {exc}")
        return 1
    print(json.dumps({"ok": True, **result}) if args.json else f"translated {result['translated']}, skipped {len(result['skipped'])}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="i18n_status")
    sub = parser.add_subparsers(dest="action", required=True)
    add_commands(sub)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
