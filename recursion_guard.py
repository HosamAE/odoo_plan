"""A command that ends up starting itself again must stop, not multiply.

Once, `odoo-ai new-version` -> `cli.py setup` -> `doctor` -> the tools' tests -> the rehearsal -> `odoo-ai new-version`
looped for ever and filled a machine with python processes. Each launcher and cli.py counts how deep the chain of
odoo-ai commands already is (ODOO_AI_DEPTH, inherited by every child) and refuses past ODOO_AI_MAX_DEPTH, naming the
chain, so a loop dies at once and says where it turns.
"""
import os
import sys

MAX_DEPTH = 8


def enter(label):
    """Count this process into the chain; exit when the chain is too deep."""
    try:
        depth = int(os.environ.get("ODOO_AI_DEPTH", "0")) + 1
        limit = int(os.environ.get("ODOO_AI_MAX_DEPTH", str(MAX_DEPTH)))
    except ValueError:
        depth, limit = 1, MAX_DEPTH
    chain = (os.environ.get("ODOO_AI_CHAIN", "") + " > " + label).strip(" >")
    if depth > limit:
        message = (f"odoo-ai: refusing to start: it called itself {depth - 1} levels deep (limit {limit}), a loop.\n"
                   f"  chain: {chain}")
        log = os.environ.get("ODOO_AI_GUARD_LOG")
        if log:   # a child's stderr is often swallowed by whoever started it: leave a trace they cannot swallow
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        sys.exit(message)
    os.environ["ODOO_AI_DEPTH"] = str(depth)
    os.environ["ODOO_AI_CHAIN"] = chain
    return depth
