# pylint: disable=print-used  # a hook: its stdout/stderr is the message to Claude Code
"""Claude Code hooks for an Odoo project: the AGENTS.md rules, enforced instead of remembered.

Wired into <project>/.claude/settings.json by `odoo-ai claude --install`. Claude Code runs the
hook before/after a tool and passes the call as JSON on stdin:

  git-guard   (PreToolUse, Bash)         a git command that writes asks the user first; --no-verify,
                                         a push to a protected branch or a new branch (when the
                                         project forbids them) is refused
  secrets     (PreToolUse, Write/Edit)   refuses text that holds a password or token (never echoed);
                                         files git ignores - the project's own .conf - are left alone
  check-edit  (PostToolUse, Write/Edit)  after a .py/.xml edit: syntax, and the version's Odoo rules,
                                         reported back to Claude as `file:line: Rn message`

The project's .claude/odoo-ai-guard.json:
  {"protected_branches": ["core"], "allow_new_branches": false}
"""
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

GIT = re.compile(r"(?:^|[\s;&|(])git\s+((?:-C\s+\S+\s+|-c\s+\S+\s+|--no-pager\s+)*)([a-z][a-z-]*)([^;&|\n]*)")
ALWAYS_ASK = {"push", "commit", "reset", "rebase", "merge", "cherry-pick", "revert", "clean", "am", "update-ref",
              "filter-branch", "restore", "switch", "checkout", "worktree", "pull"}
DESTRUCTIVE = (
    (re.compile(r"\breset\b.*--hard"), "git reset --hard discards work"),
    (re.compile(r"\bclean\b.*-[a-z]*f"), "git clean -f deletes untracked files"),
    (re.compile(r"\bcheckout\b.*\s--\s"), "git checkout -- discards changes"),
    (re.compile(r"\bbranch\b.*\s-D\b"), "git branch -D deletes a branch"),
    (re.compile(r"\bpush\b.*(?:--force\b|\s-f\b|--force-with-lease)"), "a forced push rewrites history"),
)
READ_ONLY_BRANCH_FLAGS = ("--list", "-l", "-a", "-r", "--show-current", "--contains", "-v", "-vv", "--merged")


def load_config(project_dir):
    path = os.path.join(project_dir, ".claude", "odoo-ai-guard.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    return {"protected_branches": list(data.get("protected_branches", [])),
            "allow_new_branches": bool(data.get("allow_new_branches", True))}


def writes_to_repo(sub, rest):
    """Does `git <sub> <rest>` change the repository, the index, the working tree or a ref?"""
    words = rest.split()
    if sub in ALWAYS_ASK:
        return True
    if sub == "branch":
        return bool(words) and not any(flag in words for flag in READ_ONLY_BRANCH_FLAGS)
    if sub == "tag":
        return bool(words) and not any(flag in words for flag in ("-l", "--list"))
    if sub == "stash":
        return not words or words[0] in ("push", "save", "drop", "clear", "pop", "apply", "branch")
    if sub == "apply":
        return not re.search(r"--(?:check|stat|numstat)", rest)
    return False


def makes_new_branch(sub, rest):
    words = rest.split()
    if sub in ("checkout", "switch"):
        return any(flag in words for flag in ("-b", "-B", "-c", "-C"))
    if sub == "branch":
        return bool(words) and not words[0].startswith("-") and not any(f in words for f in READ_ONLY_BRANCH_FLAGS)
    return False


def decide_git(command, config=None):
    """('allow'|'ask'|'deny', reason) for one shell command line."""
    config = config or load_config(os.getcwd())
    reasons = []
    for _prefix, sub, rest in GIT.findall(command):
        if "--no-verify" in rest or "--no-gpg-sign" in rest:
            return "deny", "git hooks and signing are never skipped (AGENTS.md). Fix what the hook reports."
        if sub == "push":
            for branch in config["protected_branches"]:
                if re.search(rf"(?:[\s:/]|^){re.escape(branch)}(?:\s|$)", rest):
                    return "deny", f"'{branch}' is protected in this project: it is reached only through the release process"
        if not config["allow_new_branches"] and makes_new_branch(sub, rest):
            return "deny", "this project does not allow new branches (CLAUDE.md)"
        line = f"{sub} {rest}"
        reasons += [why for rx, why in DESTRUCTIVE if rx.search(line)]
        if writes_to_repo(sub, rest):
            reasons.append(f"git {sub} writes to the repository")
    if reasons:
        return "ask", "; ".join(dict.fromkeys(reasons)) + " - confirm with the user (AGENTS.md Git guard)"
    return "allow", ""


def ignored_by_git(path, cwd):
    result = subprocess.run(["git", "-C", cwd, "check-ignore", "-q", path], capture_output=True, check=False)
    return result.returncode == 0


def written_texts(tool_input):
    """The text a Write/Edit/MultiEdit call puts into the file."""
    texts = [tool_input.get("content"), tool_input.get("new_string")]
    texts += [edit.get("new_string") for edit in tool_input.get("edits", []) if isinstance(edit, dict)]
    return [t for t in texts if isinstance(t, str)]


def secret_findings(tool_input, cwd):
    from secret_scan import scan_line
    path = tool_input.get("file_path", "")
    if not path or ignored_by_git(path, cwd):
        return []
    found = []
    for text in written_texts(tool_input):
        for n, line in enumerate(text.splitlines(), 1):
            found += [(n, kind) for kind in scan_line(path, line)]
    return found


def rules_module():
    """The version's own check_odoo<NN>_rules module (each version's tools carry one)."""
    candidates = sorted(glob.glob(os.path.join(TOOLS_DIR, "gates", "check_odoo*_rules.py")))
    if not candidates:
        return None
    spec = importlib.util.spec_from_file_location("odoo_rules", candidates[0])
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, os.path.join(TOOLS_DIR, "gates"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def edit_problems(path):
    """['path:line: message'] for a .py/.xml file as it is on disk now."""
    if not os.path.isfile(path) or not path.endswith((".py", ".xml")):
        return []
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    try:
        if path.endswith(".py"):
            compile(text, path, "exec")
        else:
            ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as error:   # first: it is a SyntaxError too
        return [f"{path}:{error.position[0]}: XML is not well-formed: {error}"]
    except SyntaxError as error:
        return [f"{path}:{error.lineno or 1}: syntax error: {error.msg}"]
    module = rules_module()
    if module is None:
        return []
    found = module.python_findings(path, text) if path.endswith(".py") else module.xml_findings(path, text)
    lines = text.splitlines()
    enabled = getattr(module, "ENABLED", None) or set()
    noqa = getattr(module, "NOQA", "noqa: odoo")
    return [f"{path}:{n}: {rule} {message}" for n, rule, message in found
            if (not enabled or rule in enabled) and not (0 < n <= len(lines) and noqa in lines[n - 1])]


def respond(decision, reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}))


def main(argv=None, stdin=None):
    argv = sys.argv[1:] if argv is None else argv
    action = argv[0] if argv else ""
    try:
        payload = json.load(stdin or sys.stdin)
    except ValueError:
        return 0   # a hook must never break the tool on bad input
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    if action == "git-guard":
        decision, reason = decide_git(tool_input.get("command", ""), load_config(cwd))
        if decision != "allow":
            respond(decision, reason)
        return 0
    if action == "secrets":
        findings = secret_findings(tool_input, cwd)
        if findings:
            kinds = ", ".join(sorted({kind for _n, kind in findings}))
            respond("deny", f"this text holds a secret ({kinds}). Put it in the project's own .conf (git-ignored) "
                            f"yourself; do not write it through Claude. Mark a deliberate line '# noqa: secret <why>'.")
        return 0
    if action == "check-edit":
        problems = edit_problems(tool_input.get("file_path", ""))
        if problems:
            print("\n".join(problems), file=sys.stderr)
            return 2
        return 0
    print(f"claude_hooks: unknown action '{action}'", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
