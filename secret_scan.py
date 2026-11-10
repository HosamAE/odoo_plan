# pylint: disable=print-used  # a command-line tool: printing is its output
"""odoo-ai secrets: stop a password or token from being committed.

  odoo-ai secrets --staged            scan what `git commit` is about to record (the hook runs this)
  odoo-ai secrets FILE...             scan files
  odoo-ai secrets --install-hook      write .git/hooks/pre-commit in the repository you are in

A finding names the file and line and the kind of secret - never the value.
A line that is meant to hold one says why: `# noqa: secret <reason>` (`; ...` in a .conf).
Odoo's own conf files in git keep `admin_passwd = admin`; a project's real password
belongs in the project's own .conf, which is not committed.
"""
import argparse
import os
import re
import stat
import subprocess
import sys

NOQA = "noqa: secret"
PLACEHOLDERS = {"", "false", "admin", "odoo", "password", "changeme", "xxx", "***", "none", "true"}
CONF_SUFFIXES = (".conf", ".ini", ".cfg", ".env", ".toml", ".yml", ".yaml", ".properties")

#: (kind, regex). The last group of a CONF rule is the value.
TOKEN_RULES = (
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("API key (sk-)", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
)
CONF_RULE = re.compile(
    r"^\s*[;#]?\s*(admin_passwd|db_password|smtp_password|password|secret|api_key|token)\s*[=:]\s*(.*?)\s*$", re.I)


def is_placeholder(value):
    value = value.strip().strip("'\"").lower()
    return value in PLACEHOLDERS or value.startswith(("$", "<", "{", "%")) or "xxx" in value


def scan_line(path, line):
    """Kinds of secret on one line of `path`."""
    if NOQA in line:
        return []
    kinds = [kind for kind, rx in TOKEN_RULES if rx.search(line)]
    if path.lower().endswith(CONF_SUFFIXES):
        match = CONF_RULE.match(line)
        if match and not is_placeholder(match.group(2)):
            kinds.append(f"{match.group(1)} set to a real-looking value")
    return kinds


def scan_text(path, text):
    return [(n, kind) for n, line in enumerate(text.splitlines(), 1) for kind in scan_line(path, line)]


def staged_additions():
    """{path: [(lineno, text)]} of the lines the next commit adds."""
    diff = subprocess.run(["git", "diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", check=False).stdout
    added, path, lineno = {}, None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            path = raw[6:]
        elif raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            lineno = int(match.group(1)) - 1 if match else 0
        elif raw.startswith("+") and not raw.startswith("+++") and path:
            lineno += 1
            added.setdefault(path, []).append((lineno, raw[1:]))
    return added


HOOK = """#!/bin/sh
# written by `odoo-ai secrets --install-hook`
exec "{python}" "{script}" --staged
"""


def install_hook():
    top = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], capture_output=True, text=True, check=False)
    if top.returncode:
        print("secrets: not inside a git repository")
        return 2
    target = os.path.join(top.stdout.strip(), "pre-commit")
    if os.path.exists(target) and "odoo-ai secrets" not in open(target, encoding="utf-8", errors="ignore").read():
        print(f"secrets: {target} exists and is not ours; add `{sys.executable} {os.path.abspath(__file__)} --staged` to it")
        return 2
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(HOOK.format(python=sys.executable.replace("\\", "/"),
                                 script=os.path.abspath(__file__).replace("\\", "/")))
    os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR)
    print(f"secrets: pre-commit hook installed: {target}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*")
    parser.add_argument("--staged", action="store_true", help="scan the lines the next commit adds")
    parser.add_argument("--install-hook", action="store_true", help="write .git/hooks/pre-commit here")
    args = parser.parse_args(argv)
    if args.install_hook:
        return install_hook()
    findings = []
    if args.staged:
        for path, lines in staged_additions().items():
            for lineno, text in lines:
                findings += [(path, lineno, kind) for kind in scan_line(path, text)]
    for path in args.files:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            findings += [(path, n, kind) for n, kind in scan_text(path, handle.read())]
    for path, lineno, kind in findings:
        print(f"{path}:{lineno}: {kind}")
    if findings:
        print(f"secrets: {len(findings)} finding(s); nothing was printed of the value. Move it into the project's "
              f"own .conf (not committed), or mark the line '{NOQA} <reason>'.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
