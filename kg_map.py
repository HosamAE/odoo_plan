"""The map of one module: its whole tree of dependencies and the project modules that depend on it. Read-only.

The cockpit draws this when a file of the module is open, and again for any module on the map that is pressed.
The project's own modules come from the published graph (the generation named by CURRENT). A module that is not
in the project (Odoo's own, Enterprise, themes, a third party) has no edges in the graph, so its dependencies are
read from its own __manifest__.py on disk, and followed the same way down to the foundation (base). Nothing is
written; the engine in tools/kg is not touched.

Usage: python tools/kg_map.py --data-dir <graph data dir> --module <name> [--odoo-root <odooNN folder>]
Prints one JSON object. Exit 0 also when the module is unknown (the object then carries "error").
"""
import argparse
import ast
import json
import os
import sqlite3
import sys

CAP = 400   # nodes on each side: a whole tree is wanted, but a map nobody can read helps nobody


def generation_db(data_dir):
    with open(os.path.join(data_dir, "CURRENT"), encoding="utf-8") as handle:
        current = json.load(handle)
    return os.path.join(data_dir, "generations", current["generation"], "graph.db"), current


def locate(name, odoo_root):
    """(kind, folder) of a module that is not in the project: core, enterprise, themes, third_party, or ("unknown", None)."""
    if not odoo_root:
        return "unknown", None
    for kind, paths in (
        ("core", [os.path.join(odoo_root, "odoo", "addons", name), os.path.join(odoo_root, "odoo", "odoo", "addons", name)]),
        ("enterprise", [os.path.join(odoo_root, "enterprise", name)]),
        ("themes", [os.path.join(odoo_root, "design-themes", name)]),
    ):
        for folder in paths:
            if os.path.isdir(folder):
                return kind, folder
    third = os.path.join(odoo_root, "third-party")
    if os.path.isdir(os.path.join(third, name)):
        return "third_party", os.path.join(third, name)
    if os.path.isdir(third):
        for entry in os.listdir(third):
            folder = os.path.join(third, entry, name)
            if os.path.isdir(folder):
                return "third_party", folder
    return "unknown", None


def read_manifest(folder):
    """The manifest dict of a module folder, read as data (never run); {} when it cannot be read."""
    try:
        with open(os.path.join(folder, "__manifest__.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Dict):
                return ast.literal_eval(node.value)
    except (OSError, SyntaxError, ValueError):
        pass
    return {}


def layers(nodes, edges, focus, sign):
    """Longest path from the focus along the edges, so that every edge points the same way: depth * sign."""
    forward = {}
    for src, dst in edges:
        a, b = (src, dst) if sign < 0 else (dst, src)   # dependencies walk src -> dst; users walk dst -> src
        forward.setdefault(a, []).append(b)
    indegree = {n: 0 for n in nodes}
    for a, targets in forward.items():
        for b in targets:
            indegree[b] = indegree.get(b, 0) + 1
    level = {focus: 0}
    ready = [n for n, d in indegree.items() if d == 0]
    seen = set()
    while ready:
        n = ready.pop()
        seen.add(n)
        for b in forward.get(n, []):
            level[b] = max(level.get(b, 0), level.get(n, 0) + 1)
            indegree[b] -= 1
            if indegree[b] == 0:
                ready.append(b)
    for n in nodes:   # a dependency cycle (it happens): the nodes Kahn's walk could not order keep their shortest distance
        if n not in seen:
            level.setdefault(n, 1)
    return {n: sign * level.get(n, 1) for n in nodes if n != focus}


def build_map(con, module, odoo_root):
    project = {row[0]: {"label": row[1], "version": row[2], "folder": row[3]} for row in con.execute(
        "select technical_name, manifest_name, version, module_dir from node_module")}
    out, inn = {}, {}
    for src, dst, unresolved in con.execute("select src_module, dst_module, dst_unresolved from edge_depends_on"):
        target = dst or unresolved
        if not target:
            continue
        out.setdefault(src, []).append(target)
        inn.setdefault(target, []).append(src)   # an Odoo module is "unresolved" in the graph: it still has users
    manifests = {}

    def where(name):
        if name in project:
            return "project", None
        return locate(name, odoo_root)

    def manifest(name):
        if name not in manifests:
            kind, folder = where(name)
            manifests[name] = read_manifest(folder) if folder else {}
        return manifests[name]

    def depends_of(name):
        if name in project:
            return out.get(name, [])
        return [str(d) for d in (manifest(name).get("depends") or [])]

    kind, folder = where(module)
    if kind == "project":
        info = {"label": project[module]["label"], "version": project[module]["version"], "origin": "project",
                "folder": project[module]["folder"]}
    elif folder:
        m = manifest(module)
        info = {"label": m.get("name"), "version": m.get("version"), "origin": kind, "folder": folder}
    else:
        return {"module": module, "error": "this module is neither in the project's graph nor in Odoo's folders"}

    # everything it needs, down to the foundation
    dep_nodes, dep_edges, queue = {module}, [], [module]
    truncated = False
    while queue:
        src = queue.pop(0)
        for dst in depends_of(src):
            dep_edges.append((src, dst))
            if dst not in dep_nodes:
                if len(dep_nodes) > CAP:
                    truncated = True
                    continue
                dep_nodes.add(dst)
                queue.append(dst)
    # the project modules that need it, and the ones that need those
    use_nodes, use_edges, queue = {module}, [], [module]
    while queue:
        dst = queue.pop(0)
        for src in inn.get(dst, []):
            use_edges.append((src, dst))
            if src not in use_nodes:
                if len(use_nodes) > CAP:
                    truncated = True
                    continue
                use_nodes.add(src)
                queue.append(src)

    depth = {}
    depth.update(layers(dep_nodes, [e for e in dep_edges if e[0] in dep_nodes and e[1] in dep_nodes], module, -1))
    depth.update(layers(use_nodes, [e for e in use_edges if e[0] in use_nodes and e[1] in use_nodes], module, 1))

    nodes = [{"name": module, "kind": "focus", "origin": info["origin"], "depth": 0,
              "label": info["label"], "version": info["version"], "folder": info["folder"], "openable": True}]
    for name, level in depth.items():
        k, f = where(name)
        if k == "project":
            nodes.append({"name": name, "kind": "project", "depth": level, "label": project[name]["label"],
                          "version": project[name]["version"], "folder": project[name]["folder"], "openable": True})
        else:
            m = manifest(name) if f else {}
            nodes.append({"name": name, "kind": k, "depth": level, "label": m.get("name"), "version": m.get("version"),
                          "folder": f, "openable": bool(f)})
    names = {n["name"] for n in nodes}
    edges = sorted({(a, b) for a, b in dep_edges + use_edges if a in names and b in names})
    nodes.sort(key=lambda n: (n["depth"], n["kind"] not in ("focus", "project"), n["name"]))
    return {
        "module": module,
        "nodes": nodes,
        "edges": [list(e) for e in edges],
        "counts": {
            "dependencies": len([n for n in nodes if n["depth"] < 0]),
            "dependents": len([n for n in nodes if n["depth"] > 0]),
            "outside": len([n for n in nodes if n["kind"] not in ("focus", "project")]),
        },
        "truncated": truncated,
    }


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("--odoo-root", default=None)
    args = parser.parse_args(argv)
    try:
        db, current = generation_db(args.data_dir)
        con = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True)
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        print(json.dumps({"module": args.module, "error": f"the graph cannot be read: {error}"}))
        return 0
    try:
        result = build_map(con, args.module, args.odoo_root)
        result["generation"] = current.get("generation")
    finally:
        con.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
