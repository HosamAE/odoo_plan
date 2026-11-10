# pylint: disable=print-used  # a command-line tool: printing is its output
"""odoo-ai claude: give a project's Claude Code the workspace's rules as hooks, a skill and permissions.

  odoo-ai claude --install [-p PROJECT]    write/merge <project>/.claude/settings.json (+ guard config, skill)
  odoo-ai claude --check   [-p PROJECT]    say what is in place, change nothing

What it writes, all machine-local (paths of this machine), so each file is added to the project's
.git/info/exclude - like `odoo-ai workspace` does for the others:
  .claude/settings.json        hooks git-guard / secrets / check-edit (claude_hooks.py), read-only odoo-ai
                               commands pre-approved, project .conf files not readable by Claude
  .claude/odoo-ai-guard.json   protected branches / new branches (read from the project's CLAUDE.md only by you)
  .claude/skills/odoo-ai/      how to use the tools
Existing settings are merged, never replaced; running it again changes nothing.
"""
import argparse
import json
import os
import shutil
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
MARKER = "claude_hooks.py"
SKILL_SRC = os.path.join(TOOLS_DIR, "docs", "templates", "claude_skill_odoo_ai.md")
SAFE_COMMANDS = ("doctor", "info", "gates", "test-self", "secrets", "kg status", "sync-versions")
EXCLUDES = (".claude/settings.json", ".claude/odoo-ai-guard.json", ".claude/skills/odoo-ai/")


def hook_command(python, action):
    quote = lambda p: '"' + p.replace("\\", "/") + '"'   # forward slashes work in bash and PowerShell alike
    return f"{quote(python)} {quote(os.path.join(TOOLS_DIR, MARKER))} {action}"


def wanted_settings(python):
    def entry(matcher, action):
        return {"matcher": matcher, "hooks": [{"type": "command", "command": hook_command(python, action)}]}
    return {
        "hooks": {
            "PreToolUse": [entry("Bash|PowerShell", "git-guard"), entry("Write|Edit|MultiEdit", "secrets")],
            "PostToolUse": [entry("Write|Edit|MultiEdit", "check-edit")],
        },
        "permissions": {
            "allow": [f"Bash(odoo-ai {c}:*)" for c in SAFE_COMMANDS],
            "deny": ["Read(./*.conf)"],
        },
    }


def merge(existing, wanted):
    """Add what is missing; touch nothing that is there. Returns the list of additions."""
    added = []
    for event, entries in wanted["hooks"].items():
        have = existing.setdefault("hooks", {}).setdefault(event, [])
        for entry in entries:
            action = entry["hooks"][0]["command"].rsplit(" ", 1)[1]
            if not any(MARKER in h.get("command", "") and h.get("command", "").endswith(action)
                       for e in have for h in e.get("hooks", [])):
                have.append(entry)
                added.append(f"hook {event}: {action}")
    perms = existing.setdefault("permissions", {})
    for kind, values in wanted["permissions"].items():
        have = perms.setdefault(kind, [])
        for value in values:
            if value not in have:
                have.append(value)
                added.append(f"permission {kind}: {value}")
    return added


def exclude_lines(project, lines):
    info = os.path.join(project, ".git", "info")
    if not os.path.isdir(info):
        return []
    path = os.path.join(info, "exclude")
    current = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            current = handle.read()
    missing = [line for line in lines if line not in current.splitlines()]
    if missing:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(("" if current.endswith("\n") or not current else "\n") + "\n".join(missing) + "\n")
    return missing


def install(project, python, dry=False):
    claude = os.path.join(project, ".claude")
    settings_path = os.path.join(claude, "settings.json")
    existing = {}
    if os.path.isfile(settings_path):
        with open(settings_path, encoding="utf-8") as handle:
            existing = json.load(handle)
    added = merge(existing, wanted_settings(python))
    guard_path = os.path.join(claude, "odoo-ai-guard.json")
    skill_dir = os.path.join(claude, "skills", "odoo-ai")
    if not dry:
        os.makedirs(skill_dir, exist_ok=True)
        if added or not os.path.isfile(settings_path):
            with open(settings_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(existing, handle, indent=2)
                handle.write("\n")
        if not os.path.isfile(guard_path):
            with open(guard_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump({"protected_branches": [], "allow_new_branches": True}, handle, indent=2)
                handle.write("\n")
            added.append("guard config .claude/odoo-ai-guard.json")
        target = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(target):
            shutil.copyfile(SKILL_SRC, target)
            added.append("skill odoo-ai")
        added += [f".git/info/exclude: {line}" for line in exclude_lines(project, EXCLUDES)]
    return added


def check(project):
    path = os.path.join(project, ".claude", "settings.json")
    if not os.path.isfile(path):
        return ["missing: .claude/settings.json (run: odoo-ai claude --install)"]
    with open(path, encoding="utf-8") as handle:
        existing = json.load(handle)
    missing = merge(existing, wanted_settings(sys.executable))   # `existing` is a throwaway copy here
    return ["incomplete: " + "; ".join(missing)] if missing else []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("-p", "--path", default=".", help="the project (default: here)")
    args = parser.parse_args(argv)
    project = os.path.abspath(args.path)
    if not (args.install or args.check):
        parser.print_help()
        return 0
    if args.check:
        problems = check(project)
        print("\n".join(problems) if problems else "claude: hooks, permissions and skill are in place")
        return 1 if problems else 0
    added = install(project, sys.executable)
    print("\n".join(f"  + {a}" for a in added) if added else "claude: already in place, nothing changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
