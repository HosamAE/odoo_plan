"""
Odoo AI Workspace Sync Engine
Wires every project under C:\\odoo\\odoo<version>\\projects\\ into the knowledge graph (odoo-kg)
and keeps its VS Code workspace on the shared layout.

For each project it:
  1. writes an odoo-kg config:   C:\\odoo\\odoo<NN>\\tools\\kg\\config\\local\\<project-id>.toml  (never committed)
  2. keeps the odoo-kg tasks and the VS Code MCP registration out of the project's .code-workspace file(s)
     (the Cockpit's Graph page builds and refreshes the graph; Claude Code reads it from .mcp.json)
  3. writes a Claude Code .mcp.json in the project root and excludes it in .git/info/exclude
  4. writes a CLAUDE.md with the knowledge-graph rules when the project has none
  5. creates <Project>.code-workspace when a project with addons has none (new projects, Odoo 20)

Everything is idempotent: run it again and nothing that is already there changes.
"""

import json
import os
import re
import sys

from env_root import get_odoo_root

# Dynamic Root Resolution: preserves the exact directory structure while supporting any root drive/folder
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
FS_ROOT = get_odoo_root()
WIN_ROOT = FS_ROOT.replace("\\", "/")

# A project can live in <ROOT>/ProjectsOdoo<NN>/<name> or <ROOT>/odoo<NN>/projects/<name>
PROJECT_RX = re.compile(r"(?:odoo|ProjectsOdoo)(\d+)(?:/projects)?/([^/]+)", re.IGNORECASE)
LEGACY_PROJECTS = "ProjectsOdoo"
# These tools live in <ROOT>/odoo<NN>/tools: one copy per Odoo version, each free to differ.
_TOOLS_MATCH = re.search(r"odoo(\d+)[\\/]tools$", TOOLS_DIR, re.IGNORECASE)
TOOLS_VERSION = _TOOLS_MATCH.group(1) if _TOOLS_MATCH else ""  # "" outside odoo<NN>/tools
# The knowledge-graph engine lives in each version's tools (since 2026-11-08), next to
# the rest of them; LEGACY_KG_DIR is where it stood alone before, read only to move paths.
KG_DIR = f"{WIN_ROOT}/odoo{TOOLS_VERSION}/tools/kg"
LEGACY_KG_DIR = f"{WIN_ROOT}/odoo-kg"

KG_TASK_LABELS = ("odoo-kg: build graph", "odoo-kg: status", "odoo-kg: doctor")

# What belongs to one estate of projects (names, ports, rules of a company) is not in these tools: it is read from
# tools/local/workspace_local.py when that file exists (git-ignored; its contract is in its docstring). Without it
# every project is treated alike, found by its manifests.
LOCAL_FILE = os.environ.get("ODOO_AI_WORKSPACE_LOCAL", os.path.join(TOOLS_DIR, "local", "workspace_local.py"))   # "" turns it off


def _load_local():
    if not os.path.isfile(LOCAL_FILE):
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("workspace_local", LOCAL_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LOCAL = _load_local()


def _local(name, default):
    return getattr(LOCAL, name, default) if LOCAL else default


SKIP_DIRS = {".git", ".history", "venv", ".venv", "node_modules", "__pycache__", "sessions", ".claude"}


# --------------------------------------------------------------------- helpers

def projects_root(version):
    external = f"{WIN_ROOT}/ProjectsOdoo{version}"
    if os.path.isdir(fs(external)):
        return external
    return f"{WIN_ROOT}/odoo{version}/projects"


def project_dir(version, name):
    return f"{projects_root(version)}/{name}"


def fs(win_path):
    """C:/odoo/x -> the real path of x on this machine."""
    rel = win_path.replace("\\", "/")[len(WIN_ROOT):].lstrip("/")
    return os.path.join(FS_ROOT, *rel.split("/")) if rel else FS_ROOT


def read_text(path):
    with open(path, "rb") as handle:
        raw = handle.read().decode("utf-8-sig")
    return raw, ("\r\n" in raw)


def write_text(path, text, crlf=False, dry=False):
    if crlf:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    if dry:
        return
    with open(path, "wb") as handle:
        handle.write(text.encode("utf-8"))


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


def project_id(name, version):
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._")
    return f"{slug}-odoo{version}"[:64]


def count_manifests(path, limit=3):
    total = 0
    for root, dirs, files in os.walk(path):
        depth = root[len(path):].count(os.sep)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and depth < limit]
        if "__manifest__.py" in files:
            total += 1
    return total


def own_venv_python():
    """The Python of THIS version's venv when it can run odoo-kg (3.11+), else None. Each version builds its graphs
    with its own interpreter: the Python that happened to start this tool may belong to another version."""
    if not TOOLS_VERSION:
        return None
    venv = os.path.join(FS_ROOT, f"odoo{TOOLS_VERSION}", "venv")
    exe = os.path.join(venv, "Scripts", "python.exe") if os.name == "nt" else os.path.join(venv, "bin", "python")
    cfg = os.path.join(venv, "pyvenv.cfg")
    if not (os.path.isfile(exe) and os.path.isfile(cfg)):
        return None
    with open(cfg, encoding="utf-8", errors="replace") as handle:
        m = re.search(r"^version\s*=\s*(\d+)\.(\d+)", handle.read(), re.MULTILINE)
    return exe.replace("\\", "/") if m and (int(m.group(1)), int(m.group(2))) >= (3, 11) else None


def find_kg_python(explicit=None):
    """--kg-python, then ODOO_KG_PYTHON, then this version's own venv, then an installed Python 3.11+, and the
    Python that runs this tool only as the last resort."""
    if explicit:
        return explicit.replace("\\", "/")
    if os.environ.get("ODOO_KG_PYTHON"):
        return os.environ["ODOO_KG_PYTHON"].replace("\\", "/")
    own = own_venv_python()
    if own:
        return own
    local = os.environ.get("LOCALAPPDATA", "")
    for v in ("314", "313", "312", "311"):
        candidate = os.path.join(local, "Programs", "Python", f"Python{v}", "python.exe")
        if local and os.path.isfile(candidate):
            return candidate.replace("\\", "/")
    if sys.version_info >= (3, 11):
        return sys.executable.replace("\\", "/")
    raise SystemExit("odoo-kg needs Python 3.11+. Pass --kg-python <path to python.exe>.")


def find_localappdata(explicit=None):
    value = explicit or os.environ.get("LOCALAPPDATA")
    if not value:
        raise SystemExit("LOCALAPPDATA is not set. Pass --localappdata C:/Users/<you>/AppData/Local.")
    return value.replace("\\", "/").rstrip("/")


# ---------------------------------------------------------------- the pieces

#: Marker in every file this script writes. A file without it belongs to the project and is never touched.
MARKER = "Written by `odoo-ai workspace`"

#: Project ids a repository names itself ({(version, "name"): id}); every other project's id is made from its name.
PROJECT_ID_OVERRIDES = dict(_local("PROJECT_IDS", {}))

#: Projects other projects load through their addons_path.
ADDON_REPOS = set(_local("ADDON_REPOS", ()))

#: Projects that share rules: [{"name", "versions", "projects", "origins", "origin_rules"}]
FAMILIES = list(_local("FAMILIES", ()))


def family_of(version, name):
    """The declared family of a project (a dict), or None."""
    n = name.lower()
    for fam in FAMILIES:
        if (not fam.get("versions") or version in fam["versions"]) and n in {p.lower() for p in fam.get("projects", ())}:
            return fam
    return None


def family(version, name):
    if name.lower() in {r.lower() for r in ADDON_REPOS}:
        return "addon-repo"
    fam = family_of(version, name)
    return fam["name"] if fam else "other"


def read_conf(pdir, name):
    """Non-secret facts from the project's .conf: port, db, addons_path."""
    import configparser
    confs = [f for f in os.listdir(pdir) if f.endswith(".conf")]
    if not confs:
        return None
    preferred = f"{name}.conf" if f"{name}.conf" in confs else sorted(confs)[0]
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(os.path.join(pdir, preferred), encoding="utf-8")
    o = cp["options"] if cp.has_section("options") else {}
    return {
        "file": preferred,
        "port": o.get("http_port", "8069"),
        "db_user": o.get("db_user", "?"),
        "db_name": o.get("db_name", "False"),
        "addons": [p.strip() for p in o.get("addons_path", "").split(",") if p.strip()],
    }


def git_facts(pdir):
    import subprocess
    def g(*args):
        try:
            return subprocess.run(["git", *args], cwd=pdir, capture_output=True, text=True, timeout=15).stdout.strip()
        except Exception:
            return ""
    if not os.path.isdir(os.path.join(pdir, ".git")):
        return None
    return {"branch": g("rev-parse", "--abbrev-ref", "HEAD"), "remote": g("remote", "get-url", "origin")}


def kg_config_text(pid, version, repo_root, data_dir, profile, origins, rules=()):
    origin_lines = "\n".join(f'{k} = "{v}"' for k, v in sorted(origins.items())) or "# (none)"
    if rules:
        rule_lines = "# Prefixes from the project's ONBOARDING.md (\"Module families\").\n" + "\n".join(
            f'[[origin.rules]]\npattern = "{p}"\norigin = "{o}"\n' for p, o in rules)
    else:
        rule_lines = '# [[origin.rules]]\n# pattern = "<prefix>_*"\n# origin = "project"\n'
    return f"""# odoo-kg project configuration -- {pid}
#
# Written by `odoo-ai workspace`. Local to this machine: config/local/ is never
# committed, and data_dir points at this developer's own %LOCALAPPDATA%.
# Build:  python -m odoo_kg index --config config\\local\\{pid}.toml
#
# This file must never contain a secret.

[project]
project_id = "{pid}"
repo_root = "{repo_root}"
core_root = "{WIN_ROOT}/odoo{version}/odoo"

[storage]
data_dir = "{data_dir}"
db_filename = "graph.db"

[odoo]
expected_series = "{version}.0"

[acceptance]
# "testing19" enforces the floors measured on the canonical testing19 checkout.
# "generic" keeps every integrity check and drops those floors.
profile = "{profile}"

[origin]
default = "unknown"

[origin.modules]
{origin_lines}

{rule_lines}
[exclusions]
extra_dir_names = []
extra_dir_path_suffixes = []
extra_file_globs = []

[limits]
max_hash_bytes = 8388608
"""


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


def mcp_json(kg_python, config_win):
    return json.dumps({
        "mcpServers": {
            "odoo-kg": {
                "type": "stdio",
                "command": kg_python,
                "args": ["-m", "odoo_kg", "serve", "--config", config_win],
                "env": {"PYTHONPATH": KG_DIR},
            }
        }
    }, indent=2) + "\n"


KG_SECTION = """## The knowledge graph

- Run `kg_status` (MCP server `odoo-kg`, from `.mcp.json`) before any larger analysis.
- If it reports `stale`, treat every answer as **historical**. It describes the
  commit it was built at, not the tree in front of you.
- Check the relevant source even when the graph agrees with you. The graph
  records manifests and static structure; it does not prove runtime behaviour.
  It indexes this folder only: Odoo core and other addon repos on the
  addons_path are not in it, so their modules show as unresolved dependencies.
- Rebuild after commits that matter: the Graph page of the Cockpit Odoo (it also does it when the project opens), or
  `python -m odoo_kg index --config C:\\odoo\\odoo{v}\\tools\\kg\\config\\local\\{pid}.toml`
  from `C:\\odoo\\odoo{v}\\tools\\kg`, or `odoo-ai kg index` inside the project.
- The graph is never shared through Git. It is per-developer, per-checkout.
- **Do not modify `odoo-kg` as part of an ordinary Odoo task.**
"""

RUN_SECTION = """## Running Odoo locally

```
{py} {bin} -c {conf_path}
```

Port **{port}**, database user `{db_user}`{db_line}. The conf is git-ignored: it holds passwords.
Updates and tests always end with `--stop-after-init`. Addons path:

{addons}
"""

ADDON_REPO_SECTION = """## What this repository is

`{name}` ({remote}); this checkout is `{branch}`. Other projects load it through their `addons_path`, so a change here
reaches every project of Odoo {version} that lists this folder:

{users}

Keep the series branches in step: a fix on one branch is ported to the others in the same session
or noted in `README.md`. Manifest versions stay `{version}.0.x.y.z`.
"""


def claude_md(version, name, pid, fam, conf, git, users):
    v = version
    parts = [f"<!-- {MARKER}. Local file: excluded in .git/info/exclude. Delete this line to keep it as your own. -->",
             "# Project operating instructions", "",
             f"Odoo {v}.0 project `{name}`. Shared rules: `C:\\odoo\\AGENTS.md` and "
             f"`C:\\odoo\\odoo{v}\\AGENTS.md`.", ""]
    hook = _local("claude_section", None)
    section = hook(version, name, fam, git, fs(project_dir(v, name))) if hook else None
    if section:
        parts.append(section)
    if fam == "addon-repo":
        parts.append(ADDON_REPO_SECTION.format(
            name=name, version=v, remote=(git or {}).get("remote", "?"), branch=(git or {}).get("branch", "?"),
            users="\n".join(f"- `{u}`" for u in users) or "- (no project lists it yet)"))
    if conf:
        db = conf["db_name"]
        parts.append(RUN_SECTION.format(
            py=f"C:\\odoo\\odoo{v}\\venv\\Scripts\\python.exe", bin=f"C:\\odoo\\odoo{v}\\odoo\\odoo-bin",
            conf_path=f"C:\\odoo\\odoo{v}\\projects\\{name}\\{conf['file']}", port=conf["port"],
            db_user=conf["db_user"], db_line="" if db in ("False", "", None) else f", database `{db}`",
            addons="\n".join(f"- `{a}`" for a in conf["addons"])))
    parts.append(KG_SECTION.format(pid=pid, v=version))
    parts.append("## Never in Git\n\n- Databases, dumps, filestores, `*.conf`, keys, tokens, passwords.\n"
                 "- Generated graph data, `.mcp.json`, this file.\n")
    return "\n".join(parts)


def projects_listing(addon_dir_win, version):
    """Projects of this version whose .conf addons_path names addon_dir_win."""
    users = []
    base = fs(projects_root(version))
    key = addon_dir_win.replace("\\", "/").lower().rstrip("/")
    for n in sorted(os.listdir(base)):
        d = os.path.join(base, n)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.endswith(".conf"):
                text = open(os.path.join(d, f), encoding="utf-8", errors="ignore").read().replace("\\", "/").lower()
                if key in text:
                    users.append(f"odoo{version}\\projects\\{n}\\{f}")
    return users


def odoo_source_rel(version, from_win_dir):
    """Relative path (forward slashes) from a folder to the Odoo source of `version`: ../../odoo beside
    odoo<NN>/projects/<name>, ../../odoo<NN>/odoo from ProjectsOdoo<NN>/<name> - never assumed."""
    source = fs(f"{WIN_ROOT}/odoo{version}/odoo")
    return os.path.relpath(source, fs(from_win_dir)).replace(os.sep, "/")


def new_workspace(version, name):
    v = version
    proj = project_dir(v, name)
    py = f"C:\\odoo\\odoo{v}\\venv\\Scripts\\python.exe"
    lint = {
        "label": "odoo-ai: lint this module", "type": "process", "command": py,
        "args": [tools_cli(v), "lint", "-p", "${file}", "-v", v],
        "options": {"cwd": "${workspaceFolder:Project}"},
        "presentation": {"reveal": "always", "panel": "dedicated", "clear": True},
        "problemMatcher": [
            {"owner": "odoo-ai", "source": "pylint", "fileLocation": "absolute", "severity": "error",
             "pattern": {"regexp": "^(.+?):(\\d+):(\\d+): ([EF]\\d{4}): (.*)$",
                         "file": 1, "line": 2, "column": 3, "code": 4, "message": 5}},
            {"owner": "odoo-ai", "source": "pylint", "fileLocation": "absolute", "severity": "warning",
             "pattern": {"regexp": "^(.+?):(\\d+):(\\d+): (W\\d{4}): (.*)$",
                         "file": 1, "line": 2, "column": 3, "code": 4, "message": 5}},
            {"owner": "odoo-ai", "source": "flake8", "fileLocation": "absolute", "severity": "warning",
             "pattern": {"regexp": "^(.+?):(\\d+):(\\d+): (F\\d{3}) (.*)$",
                         "file": 1, "line": 2, "column": 3, "code": 4, "message": 5}},
            {"owner": "odoo-ai", "source": "xml", "fileLocation": "absolute", "severity": "error",
             "pattern": {"regexp": "^\\[XML SYNTAX ERROR\\] (.+?): (.*line (\\d+).*)$",
                         "file": 1, "message": 2, "line": 3}},
        ],
        "detail": "pylint-odoo, flake8 and XML check of the addon of the open file",
        "group": {"kind": "test", "isDefault": True},
    }
    lint_project = dict(lint, label="odoo-ai: lint the project",
                        args=[tools_cli(v), "lint", "-p", "${workspaceFolder:Project}", "-v", v],
                        detail="The same for every addon of the project (slow)")
    lint_project.pop("group")
    audit = dict(lint, label="odoo-ai: audit this module",
                 args=[tools_cli(v), "audit", "-p", "${file}"],
                 problemMatcher=[], detail="Manifest and security CSV of the addon of the open file")
    audit.pop("group")
    run = lambda label, extra, console: {
        "name": label, "type": "debugpy", "request": "launch", "stopOnEntry": False,
        "python": py, "program": f"{WIN_ROOT}/odoo{v}/odoo/odoo-bin",
        "args": [f"--config={proj}/{name}.conf", "--dev=all"] + extra,
        "cwd": f"{WIN_ROOT}/odoo{v}/odoo", "console": console, "justMyCode": False,
    }
    return {
        "folders": [{"name": "odoo", "path": odoo_source_rel(v, proj)}, {"name": "Project", "path": "."}],
        "extensions": {"recommendations": [
            "odoo.odoo", "HosamAE.odoo-ci", "ms-python.python", "ms-python.debugpy",
            "ms-python.vscode-pylance", "ms-python.black-formatter", "ms-python.pylint", "ms-python.isort",
            "esbenp.prettier-vscode", "redhat.vscode-xml", "mechatroner.rainbow-csv", "eamodio.gitlens",
        ]},
        "launch": {"version": "0.2.0", "configurations": [
            run("Odoo: Run", [], "integratedTerminal"),
            run("Odoo: Debug", [], "internalConsole"),
        ]},
        "tasks": {"version": "2.0.0", "tasks": [lint, lint_project, audit]},
        "settings": {
            "python.languageServer": "None",
            "python.analysis.typeCheckingMode": "off",
            "python.defaultInterpreterPath": py.replace("/", "\\\\"),
            # The odoo source only: an addons folder on PYTHONPATH shadows the stdlib.
            "python.analysis.extraPaths": [f"{WIN_ROOT}/odoo{v}/odoo"],
            "python.analysis.diagnosticMode": "openFilesOnly",
            "Odoo.serverConfigPath": odools_path(v, name),
            "Odoo.selectedProfile": "default",
            "flake8.enabled": False,
            "cSpell.diagnosticLevel": "Hint",
            "pylint.enabled": True,
            "pylint.interpreter": [py],
            "pylint.args": [f"--rcfile={WIN_ROOT}/odoo{v}/tools/.pylintrc",
                            f"--init-hook=\"import sys; sys.path.append('{WIN_ROOT}/odoo{v}/odoo')\""],
            "pylint.cwd": proj,
            "pylint.importStrategy": "fromEnvironment",
            "pylint.severity": {"convention": "Hint", "refactor": "Hint", "warning": "Warning",
                                "error": "Error", "fatal": "Error"},
            "flake8.severity": {"E": "Hint", "W": "Hint", "F": "Warning"},
            "isort.args": ["--profile", "black"],
            "black-formatter.args": ["--line-length=120"],
            "[python]": {"editor.defaultFormatter": "ms-python.black-formatter", "editor.formatOnSave": True},
            "[xml]": {"editor.defaultFormatter": "redhat.vscode-xml"},
            "xml.validation.enabled": False,
            "files.exclude": {"**/__pycache__": True, "**/sessions": True},
            "files.watcherExclude": {"**/.git/objects/**": True, "**/__pycache__/**": True,
                                     "**/venv/**": True, "**/sessions/**": True},
        },
    }


def exclude_locally(pdir, pattern, dry):
    exclude = os.path.join(pdir, ".git", "info", "exclude")
    if not os.path.isdir(os.path.join(pdir, ".git")):
        return False
    current = ""
    if os.path.isfile(exclude):
        current, _ = read_text(exclude)
    if pattern in current.splitlines():
        return False
    gitignore = os.path.join(pdir, ".gitignore")
    if os.path.isfile(gitignore) and pattern in read_text(gitignore)[0].replace("\r\n", "\n").split("\n"):
        return False  # the repository already ignores it
    if not dry:
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        with open(exclude, "a", encoding="utf-8") as handle:
            if current and not current.endswith("\n"):
                handle.write("\n")
            handle.write(pattern + "\n")
    return True



# ----------------------------------------------------- conf / workspace cloning

#: The project of each version whose .conf and workspace are the pattern for new ones.
PATTERN_PROJECT = dict(_local("PATTERN_PROJECTS", {}))

#: Ports for projects that had no .conf ({(version, "name"): port}); the others get the next free one.
NEW_PORTS = dict(_local("PORTS", {}))


def conf_name(pdir, name):
    confs = sorted(f for f in os.listdir(pdir) if f.endswith(".conf"))
    if f"{name}.conf" in confs:
        return f"{name}.conf"
    return confs[0] if confs else None


def next_free_port(version):
    """One above the highest http_port among this version's projects."""
    used = []
    for _v, other in iter_projects((version,)):
        pdir = fs(project_dir(version, other))
        conf = conf_name(pdir, other)
        if conf:
            m = re.search(r"^http_port\s*=\s*(\d+)", read_text(os.path.join(pdir, conf))[0], re.M)
            if m:
                used.append(int(m.group(1)))
    return max(used) + 1 if used else 8000 + int(version) * 10


def clone_conf(version, name, dry):
    """<name>.conf for a project without one: the pattern project's conf (same DB user and
    passwords), with this project's addons_path and its own port. On a new machine, where
    there is no pattern project yet, the version's own odoo.conf (`odoo-ai setup` writes it)."""
    sib = PATTERN_PROJECT.get(version)
    sib_dir = fs(project_dir(version, sib)) if sib else ""
    src_name = conf_name(sib_dir, sib) if sib and os.path.isdir(sib_dir) else None
    if src_name:
        source = os.path.join(sib_dir, src_name)
    else:
        source = fs(f"{WIN_ROOT}/odoo{version}/odoo.conf")
        sib, src_name = f"odoo{version}", "odoo.conf"
        if not os.path.isfile(source):
            return None
    raw, crlf = read_text(source)
    s = raw.replace("\r\n", "\n")
    m = re.search(r"^addons_path\s*=\s*(.*)$", s, re.M)
    parts = [p.strip() for p in m.group(1).split(",") if p.strip()]
    # Odoo, enterprise, themes and third-party - never the pattern project or any other one.
    keep = [p for p in parts if not re.search(r"projectsodoo|/projects/", p.replace("\\", "/").lower())]
    mine = project_dir(version, name)
    s = s[:m.start(1)] + ", ".join(keep + [mine]) + s[m.end(1):]
    port = NEW_PORTS.get((version, name), None) or next_free_port(version)
    if port:
        if re.search(r"^http_port\s*=", s, re.M) is None:
            s = s.rstrip("\n") + f"\nhttp_port = {port}\n"
        s = re.sub(r"^http_port\s*=.*$", f"http_port = {port}", s, flags=re.M)
    s = re.sub(r"^db_name\s*=.*$", "db_name = False", s, flags=re.M)
    s = re.sub(r"^dbfilter\s*=.*\n", "", s, flags=re.M)
    path = os.path.join(fs(mine), f"{name}.conf")
    write_text(path, s, crlf=crlf, dry=dry)
    return f"{name}.conf (from {sib}/{src_name}, port {port or 'same as pattern'})"


def workspace_from_template(version, name, pid, dry):
    """<name>.code-workspace from tools/project/project.code-workspace, the version's
    template every project is built on ({{PROJECT}}, {{PROJECT_ID}}, {{CONF}})."""
    template = os.path.join(TOOLS_DIR, "project", "project.code-workspace")
    if not os.path.isfile(template):
        return None
    t, crlf = read_text(template)
    conf = conf_name(fs(project_dir(version, name)), name) or f"{name}.conf"
    t = t.replace("{{PROJECT}}", name).replace("{{PROJECT_ID}}", pid).replace("{{CONF}}", conf)
    # this machine's Python 3.11+ for the graph (never the template author's path)
    t = t.replace("{{KG_PYTHON}}", find_kg_python(None).replace("\\", "/"))
    loads_jsonc(t)
    dst = os.path.join(fs(project_dir(version, name)), f"{name}.code-workspace")
    write_text(dst, t, crlf=crlf, dry=dry)
    return os.path.relpath(dst, FS_ROOT) + " (from tools/project)"


def clone_workspace(version, name, pid, inner, dry):
    """<name>.code-workspace from the template in tools/project, else from the pattern
    project's own workspace, paths and names swapped."""
    if inner:
        made = workspace_from_template(version, name, pid, dry)
        if made:
            return made
    sib = PATTERN_PROJECT.get(version)
    if not sib:
        return None
    sib_dir = fs(project_dir(version, sib))
    src = os.path.join(sib_dir, f"{sib}.code-workspace") if inner else \
        fs(f"{projects_root(version)}/{sib}.code-workspace")
    if not os.path.isfile(src):
        src = os.path.join(sib_dir, f"{sib}.code-workspace")
        if not os.path.isfile(src):
            return None
        from_inner = True
    else:
        from_inner = inner
    t, crlf = read_text(src)
    sib_pid = PROJECT_ID_OVERRIDES.get((version, sib.lower()), project_id(sib, version))
    for a, b in ((f"odoo{version}/projects/{sib}", f"odoo{version}/projects/{name}"),
                 (f"odoo{version}\\\\projects\\\\{sib}", f"odoo{version}\\\\projects\\\\{name}"),
                 (f"{sib_pid}.toml", f"{pid}.toml")):
        t = t.replace(a, b)
    sib_conf = conf_name(sib_dir, sib)
    my_conf = conf_name(fs(project_dir(version, name)), name) or f"{name}.conf"
    if sib_conf:
        t = t.replace(f"/{name}/{sib_conf}", f"/{name}/{my_conf}")
    t = re.sub(r'(\s*)"-u",\s*"[^"]*",', "", t)        # the pattern's own module list
    if inner and not from_inner:
        t = t.replace(f'"path": "./{name}"', '"path": "."')
    if not inner:
        t = t.replace('"name": "Project",\n      "path": "."', f'"name": "Project",\n      "path": "./{name}"')
        t = re.sub(r'("name":\s*"Project",\s*"path":\s*)"\."', rf'\1"./{name}"', t)
    loads_jsonc(t)
    dst = os.path.join(fs(project_dir(version, name)), f"{name}.code-workspace") if inner else \
        fs(f"{projects_root(version)}/{name}.code-workspace")
    write_text(dst, t, crlf=crlf, dry=dry)
    return os.path.relpath(dst, FS_ROOT)



def fix_folders(text, version, name, inner):
    """The two folders of a workspace: Odoo source and the project. Beside the project
    (odooNN/projects/<name>.code-workspace) they are ../odoo and ./<name>; inside it ../../odoo and '.'."""
    changed = []
    # The Odoo source, not the version folder: that folder holds the projects too,
    # and VS Code would show (and index) the project twice.
    odoo_path = odoo_source_rel(version, project_dir(version, name) if inner else projects_root(version))
    proj_path = "." if inner else f"./{name}"
    def sub(label, want, t):
        pat = re.compile(r'("name"\s*:\s*"' + label + r'"\s*,\s*"path"\s*:\s*)"([^"]*)"')
        m = pat.search(t)
        if m and m.group(2) != want:
            changed.append(f"{label} folder {m.group(2)} -> {want}")
            t = t[:m.start(2) - 1] + '"' + want + '"' + t[m.end(2) + 1:]
        return t
    text = sub("odoo", odoo_path, text)
    text = sub("Project", proj_path, text)
    loads_jsonc(text)
    return text, changed


# --------------------------------------------- language server and linters

#: Words every Odoo project uses; the spell checker should not flag them.
ODOO_WORDS = ["odoo", "Odoo", "odools", "pylint", "pylintrc", "ormcache", "onchange", "xmlid", "xpath",
              "qweb", "QWeb", "owl", "OWL", "hr", "res", "uom", "kanban", "sudo", "ondelete"]
#: Odoo's own code, the venv and translations are not the project's text.
SPELL_IGNORE = ["**/odoo{v}/odoo/**", "**/odoo{v}/enterprise/**", "**/odoo{v}/venv/**",
                "**/i18n/**", "**/*.po", "**/*.pot", "**/static/lib/**", "**/help/sv/**"]
#: Settings of extensions that no longer read them (OdooLS reads odools.toml only).
DEAD_SETTINGS = ("odoo.interpreter", "odoo.pythonPath", "odoo.addons",
                 "trinhanhngoc.vscode-odoo.validation.enabled", "vscode-odoo.validation.enabled")


def odools_path(version, name):
    """Inside the project, kept out of its repository (.git/info/exclude): per developer."""
    return f"{project_dir(version, name)}/odools.toml"


def odools_text(version, name, conf):
    """OdooLS config for one project (odoo.odoo 1.6+ reads only odools.toml).

    The venv's Python (the system one lacks dateutil, babel, markupsafe...),
    the project's own addons path without enterprise, and diagnostics for the
    project only: Odoo itself is analysed but never ours to fix. Third-party
    modules inside the project are shown like our own (owner, 2026-11-07).
    """
    v = version
    core = {f"{WIN_ROOT}/odoo{v}/odoo/addons".lower(), f"{WIN_ROOT}/odoo{v}/odoo/odoo/addons".lower()}
    addons = []
    for raw in (conf or {}).get("addons", []):
        path = raw.replace("\\", "/").rstrip("/")
        if path.lower() in core or path.lower() in (a.lower() for a in addons):
            continue
        if os.path.isdir(fs(path)) or path.lower().startswith(f"{WIN_ROOT}/projectsodoo{v}/{name}".lower()):
            addons.append(path)
    project = project_dir(v, name)
    if project.lower() not in (a.lower() for a in addons):
        addons.append(project)
    tsserver = os.path.expandvars(r"%APPDATA%\npm\tsserver.cmd")
    lines = [
        f"# {MARKER}: OdooLS (Odoo VS Code extension) for {name}, Odoo {v}.0.",
        "# The workspace points \"Odoo.serverConfigPath\" here. Rewritten by the tool while",
        "# this marker stays; delete the marker line to keep hand edits.",
        "#",
        "# Severity meaning, the same in every tool of the workspace:",
        "#   Error   broken: crashes or wrong behaviour - fix it.",
        "#   Warning probably a problem - read it.",
        "#   Info    worth a look, often intended (an optional integration guarded",
        "#           by `if \"model\" in env`).",
        "#   Hint    style and good practice only.",
        "",
        "[[config]]",
        'name = "default"',
        f'odoo_path = "{WIN_ROOT}/odoo{v}/odoo"',
        "addons_paths = [",
        *[f'    "{a}",' for a in addons],
        "]",
        f'python_path = "{WIN_ROOT}/odoo{v}/venv/Scripts/python.exe"',
        'diag_missing_imports = "all"',
    ]
    if os.path.isfile(tsserver):
        lines.append(f"tsserver_command = '{tsserver}'")
    lines += [
        "",
        "[config.diagnostic_settings]",
        'OLS03001 = "Info"   # model not in the module\'s dependencies',
        'OLS03003 = "Info"   # module used but not in depends',
        'OLS03024 = "Hint"   # model class declared inside a function (tests)',
        "",
        "# The project only: Odoo itself and the venv are analysed, not reported.",
        "[[config.diagnostic_filters]]",
        f'paths = ["**/odoo{v}/projects/{name}/**"]',
        'path_type = "notin"',
        "",
        "# Tests touch models of modules installed in the test DB without declaring them.",
        "[[config.diagnostic_filters]]",
        'paths = ["**/tests/**"]',
        'codes = ["OLS03001"]',
        "",
    ]
    return "\n".join(lines)


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
UNWANTED_EXTENSIONS = ("github.copilot", "github.copilot-chat", "trinhanhngoc.vscode-odoo")
WANTED_EXTENSIONS = ("HosamAE.odoo-ci",)


def tidy_recommendations(text):
    """Drop the unwanted recommendations, name them as unwanted, add the wanted ones."""
    changed = []
    block = re.search(r'("recommendations"\s*:\s*\[)(.*?)(\])', text, re.S)
    if not block:
        return text, changed
    body = block.group(2)
    for ext in UNWANTED_EXTENSIONS:
        new = re.sub(r'\n[ \t]*"%s",?' % re.escape(ext), "", body)
        if new != body:
            body = new
            changed.append(f"-{ext}")
    indent = re.search(r'\n([ \t]*)"', body)
    indent = indent.group(1) if indent else "      "
    for ext in WANTED_EXTENSIONS:
        if f'"{ext}"' not in body:
            body = body.rstrip() + ("" if body.rstrip().endswith(",") or not body.strip() else ",") \
                + f'\n{indent}"{ext}",\n' + indent[:-2]
            changed.append(f"+{ext}")
    text = text[:block.start(2)] + body + text[block.end(2):]
    if '"unwantedRecommendations"' not in text:
        end = re.search(r'"recommendations"\s*:\s*\[.*?\]\s*,?', text, re.S).end()
        closing = text[re.search(r'"recommendations"\s*:\s*\[.*?\]', text, re.S).end():end]
        sep = "" if "," in closing else ","
        unwanted = ", ".join(f'"{e}"' for e in UNWANTED_EXTENSIONS)
        outer = indent[:-2] if len(indent) >= 2 else ""
        text = text[:end] + f'{sep}\n{outer}"unwantedRecommendations": [{unwanted}],' + text[end:]
        changed.append("unwanted list")
    loads_jsonc(text)
    return text, changed


def ensure_lint_settings(text, version, name):
    """The workspace settings agreed on 2026-11-07, for every project.

    - extraPaths: the odoo source only. The Python tool extensions put this
      list on PYTHONPATH, and an addons folder there shadows the standard
      library (Odoo's "calendar" app hid Python's calendar: Flake8/isort died
      with "write EPIPE").
    - OdooLS reads odools.toml only: point it there, drop the dead keys, and
      leave Python language features to it (python.languageServer None).
    - One Python linter (Pylint): Flake8 repeated it and showed style as
      errors. Spelling is cosmetic: hints.
    """
    v, changed = version, []
    def note(flag, what):
        if flag:
            changed.append(what)
    data = loads_jsonc(text).get("settings", {})
    text, f = set_setting(text, "python.languageServer", "None"); note(f, "python LS off (OdooLS)")
    text, f = set_setting(text, "python.analysis.extraPaths", [f"{WIN_ROOT}/odoo{v}/odoo"]); note(f, "extraPaths")
    if "python.autoComplete.extraPaths" in data:
        text, f = set_setting(text, "python.autoComplete.extraPaths", [f"{WIN_ROOT}/odoo{v}/odoo"])
        note(f, "autoComplete paths")
    for key in DEAD_SETTINGS:
        text, f = drop_setting(text, key); note(f, f"-{key}")
    text, f = set_setting(text, "Odoo.serverConfigPath", odools_path(v, name)); note(f, "odools.toml")
    text, f = set_setting(text, "Odoo.selectedProfile", "default"); note(f, "OdooLS profile")
    # Extensions that add to the terminal's environment (Python Debugger, Claude Code) made
    # every open terminal show "want to relaunch the terminal": relaunch it when safe, quietly.
    text, f = set_setting(text, "terminal.integrated.environmentChangesRelaunch", True); note(f, "terminal relaunch")
    text, f = set_setting(text, "terminal.integrated.environmentChangesIndicator", "off"); note(f, "terminal notice off")
    # ...and the notice's root: terminals restored from the last session open before the
    # extensions are ready. Start fresh ones instead (they get the full environment).
    text, f = set_setting(text, "terminal.integrated.enablePersistentSessions", False); note(f, "no restored terminals")
    text, f = set_setting(text, "terminal.integrated.persistentSessionReviveProcess", "never"); note(f, "no revived processes")
    # OdooLS asks to turn off VS Code's own JS/TS features; we keep them (go-to, rename, imports)
    text, f = set_setting(text, "Odoo.disableJsLanguageServerPopup", True); note(f, "no OdooLS JS popup")
    text, f = set_setting(text, "flake8.enabled", False); note(f, "flake8 off")
    text, f = set_setting(text, "cSpell.diagnosticLevel", "Hint"); note(f, "spelling as hints")
    words = list(data.get("cSpell.words", []))
    words += [w for w in ODOO_WORDS if w not in words]
    text, f = set_setting(text, "cSpell.words", words); note(f, "spelling words")
    ignore = list(data.get("cSpell.ignorePaths", []))
    ignore += [p.format(v=v) for p in SPELL_IGNORE if p.format(v=v) not in ignore]
    text, f = set_setting(text, "cSpell.ignorePaths", ignore); note(f, "spelling ignores")
    loads_jsonc(text)
    return text, changed


def tools_cli(version):
    """The odoo-ai of an Odoo version: its own tools folder."""
    return f"{WIN_ROOT}/odoo{version}/tools/cli.py"


# Paths from before the tools moved into C:/odoo/odoo<NN>/tools (2026-11-08)
def move_tool_paths(text, version):
    """Rewrite paths from before the tools moved (2026-11-08) to where they are now. Where they are now is read
    from the disk (projects_root), never assumed: a project in ProjectsOdoo<NN> keeps its paths."""
    changed = []
    esc = lambda path: path.replace("/", "\\" * 2)   # the form inside JSON text: C:\odoo\...
    now = projects_root(version)
    old_projects = f"{WIN_ROOT}/{LEGACY_PROJECTS}{version}"
    for old, new in ((f"{LEGACY_KG_DIR}/", f"{KG_DIR}/"), (f'"{LEGACY_KG_DIR}"', f'"{KG_DIR}"'),
                     (f"{old_projects}/", f"{now}/"),
                     (esc(old_projects) + "\\" * 2, esc(now) + "\\" * 2),
                     (f"{WIN_ROOT}/odoo_ai_cli/cli.py", tools_cli(version)),
                     (f"--rcfile={WIN_ROOT}/odoo{version}/.pylintrc", f"--rcfile={WIN_ROOT}/odoo{version}/tools/.pylintrc")):
        if old != new and old in text:
            text = text.replace(old, new)
            changed.append(f"{old.rstrip('/').rsplit('/', 1)[-1]} -> new path")
    loads_jsonc(text)
    return text, changed


def write_local_settings(kg_python, localappdata, dry):
    """What `odoo-ai kg` needs to find the graph on this machine."""
    path = os.path.join(TOOLS_DIR, "local.json")
    data = {"kg_python": kg_python, "localappdata": localappdata,
            "note": "Written by odoo-ai workspace. Machine-specific: not for sharing."}
    if not dry:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)


def resolve_project(path):
    """(version, name, pid, config_win) of the project a path belongs to, or None."""
    norm = os.path.abspath(path).replace("\\", "/")
    m = PROJECT_RX.search(norm)
    if not m:
        return None
    version, name = m.group(1), m.group(2)
    pid = PROJECT_ID_OVERRIDES.get((version, name.lower()), project_id(name, version))
    return version, name, pid, f"{KG_DIR}/config/local/{pid}.toml"


# ------------------------------------------------------------------- driver

def sync_project(version, name, kg_python, localappdata, dry=False, create_workspace=True):
    report = []
    win_dir = project_dir(version, name)
    pdir = fs(win_dir)
    manifests = count_manifests(pdir)
    if manifests == 0:
        return [f"skip {win_dir}: no addon (__manifest__.py) found"]

    has_ws = os.path.isfile(os.path.join(pdir, f"{name}.code-workspace"))
    if not os.path.isdir(os.path.join(pdir, ".git")) and not has_ws:
        return [f"skip {win_dir}: neither a git checkout nor a workspace (a copy, not a project)"]

    pid = PROJECT_ID_OVERRIDES.get((version, name.lower()), project_id(name, version))
    fam = family(version, name)
    profile = "generic"  # testing19 floors belong to the canonical config only
    declared = family_of(version, name) or {}
    origins = declared.get("origins", {})
    rules = declared.get("origin_rules", ())
    config_win = f"{KG_DIR}/config/local/{pid}.toml"
    config_fs = fs(config_win)
    data_dir = f"{localappdata}/odoo-kg/projects/{pid}"

    # 1. odoo-kg config: written when missing, rewritten when it is ours and out of date
    wanted = kg_config_text(pid, version, win_dir, data_dir, profile, origins, rules)
    current = read_text(config_fs)[0] if os.path.isfile(config_fs) else None
    if current is None or (MARKER in current and current.replace("\r\n", "\n") != wanted):
        # keep a hand-set profile line (e.g. a comment explaining why) when the rest is ours
        if current and 'profile = "generic"' in current:
            wanted = re.sub(r'^profile = .*$', re.search(r'^profile = .*$', current, re.M).group(0), wanted,
                            count=1, flags=re.M)
        if current is None or current.replace("\r\n", "\n") != wanted:
            if not dry:
                os.makedirs(os.path.dirname(config_fs), exist_ok=True)
            write_text(config_fs, wanted, dry=dry)
            report.append(f"kg config  {config_win} ({profile}, {manifests} addons"
                          + (f", {len(rules)} origin rules" if rules else "") + ")")

    # 2. every project has its own .conf ...
    if conf_name(pdir, name) is None:
        made = clone_conf(version, name, dry)
        if made:
            report.append(f"conf       {made}")
            if exclude_locally(pdir, "*.conf", dry):
                report.append("git        *.conf added to .git/info/exclude")

    # ... and one workspace, inside the project
    candidates = [os.path.join(pdir, f"{name}.code-workspace")]
    if create_workspace and not os.path.isfile(candidates[0]):
        made = clone_workspace(version, name, pid, True, dry)
        if not made:
            # No pattern project to copy (a new machine): one written from scratch.
            write_text(candidates[0], json.dumps(new_workspace(version, name),
                                                 indent=2) + "\n", dry=dry)
            made = os.path.relpath(candidates[0], FS_ROOT) + " (new)"
        if made:
            report.append(f"workspace  created {made}")
        if exclude_locally(pdir, "*.code-workspace", dry):
            report.append("git        *.code-workspace added to .git/info/exclude")
    # ... and its OdooLS config, inside the project but never in its repository
    odools_fs = fs(odools_path(version, name))
    wanted_ls = odools_text(version, name, read_conf(pdir, name))
    current_ls = read_text(odools_fs)[0] if os.path.isfile(odools_fs) else None
    if current_ls is None or (MARKER in current_ls and current_ls.replace("\r\n", "\n") != wanted_ls):
        write_text(odools_fs, wanted_ls, dry=dry)
        report.append(f"odools     {odools_path(version, name)}")
    if exclude_locally(pdir, "odools.toml", dry):
        report.append("git        odools.toml added to .git/info/exclude")
    for path in candidates:
        if not os.path.isfile(path):
            continue
        text, crlf = read_text(path)
        new, removed = remove_kg_tasks(text)
        new, wired = remove_graph_registration(new)
        new, folders = fix_folders(new, version, name, inner=(path == candidates[0]))
        wired += folders
        new, moved = move_tool_paths(new, version)
        wired += moved
        new, recs = tidy_recommendations(new)
        wired += recs
        new, lint = ensure_lint_settings(new, version, name)
        wired += lint
        if removed or wired:
            write_text(path, new, crlf=crlf, dry=dry)
            what = ([f"-{removed} odoo-kg tasks (the Cockpit's Graph page does their work)"] if removed else []) + wired
            report.append(f"workspace  {', '.join(what)} in {os.path.relpath(path, FS_ROOT)}")

    # 3. Claude Code MCP server, local to this machine
    mcp_path = os.path.join(pdir, ".mcp.json")
    if not os.path.isfile(mcp_path):
        write_text(mcp_path, mcp_json(kg_python, config_win), dry=dry)
        report.append("mcp        .mcp.json (odoo-kg server)")
    elif LEGACY_KG_DIR in read_text(mcp_path)[0]:
        text, crlf = read_text(mcp_path)
        write_text(mcp_path, text.replace(f"{LEGACY_KG_DIR}/", f"{KG_DIR}/").replace(
            f'"{LEGACY_KG_DIR}"', f'"{KG_DIR}"'), crlf=crlf, dry=dry)
        report.append("mcp        .mcp.json -> the engine in tools/kg")
    if exclude_locally(pdir, ".mcp.json", dry):
        report.append("git        .mcp.json added to .git/info/exclude")

    # 4. CLAUDE.md: the project's own file is never touched; ours is kept current
    path = os.path.join(pdir, "CLAUDE.md")
    current = read_text(path)[0] if os.path.isfile(path) else None
    ours = current is not None and (MARKER in current or current.startswith(
        f"# Project operating instructions\n\nOdoo {version}.0 project `{name}`. Shared rules"))
    if current is None or ours:
        users = projects_listing(win_dir, version) if fam == "addon-repo" else []
        text = claude_md(version, name, pid, fam, read_conf(pdir, name), git_facts(pdir), users)
        if current is None or current.replace("\r\n", "\n") != text:
            write_text(path, text, dry=dry)
            report.append(f"claude     CLAUDE.md ({fam})")
        if exclude_locally(pdir, "CLAUDE.md", dry):
            report.append("git        CLAUDE.md added to .git/info/exclude")

    return report or [f"ok   {win_dir}: already wired"]


def iter_projects(versions):
    for v in versions:
        base = fs(projects_root(v))
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if os.path.isdir(os.path.join(base, name)) and not name.startswith("."):
                yield v, name


def run_workspace_sync(path=None, versions=(TOOLS_VERSION,), kg_python=None, localappdata=None,
                       dry=False):
    kg_python = find_kg_python(kg_python)
    localappdata = find_localappdata(localappdata)
    if path:
        norm = path.replace("\\", "/")
        m = PROJECT_RX.search(norm)
        if not m:
            raise SystemExit(f"not a project folder: {path} (expected ...\\ProjectsOdoo<version>\\<Project>)")
        if m.group(1) != TOOLS_VERSION:
            raise SystemExit(f"{path} is an Odoo {m.group(1)} project: these are the Odoo {TOOLS_VERSION} tools "
                             f"(run `odoo-ai`, it picks C:\\odoo\\odoo{m.group(1)}\\tools)")
        targets = [(m.group(1), m.group(2))]
    else:
        targets = list(iter_projects(versions))
    print(f"[WORKSPACE] odoo-kg python: {kg_python}" + ("   (dry run)" if dry else ""))
    write_local_settings(kg_python, localappdata, dry)
    for version, name in targets:
        print(f"\n== Odoo {version} / {name}")
        for line in sync_project(version, name, kg_python, localappdata, dry=dry):
            print("  " + line)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", "-p")
    parser.add_argument("--kg-python")
    parser.add_argument("--localappdata")
    parser.add_argument("--dry-run", action="store_true")
    a = parser.parse_args()
    run_workspace_sync(a.path, kg_python=a.kg_python, localappdata=a.localappdata, dry=a.dry_run)
