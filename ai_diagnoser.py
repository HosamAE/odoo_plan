"""
Odoo AI Diagnoser Engine
Parses test failures & lint reports, enriches them with Knowledge Graph AST & Model Context,
and generates structured prompt payloads for AI resolution.
"""

import glob
import os
import re
import sqlite3


def _find_kg_db(project_name, version):
    """Find the most recent graph.db for this project in localappdata."""
    local_app = os.path.expandvars(r"%LOCALAPPDATA%\odoo-kg\projects")
    candidate_dirs = [
        f"{project_name.lower()}-odoo{version}",
        f"{project_name.lower()}-testing{version}",
        f"{project_name.lower()}",
    ]
    for c in candidate_dirs:
        p = os.path.join(local_app, c, "generations")
        if os.path.isdir(p):
            gens = sorted(glob.glob(os.path.join(p, "gen-*", "graph.db")), key=os.path.getmtime, reverse=True)
            if gens:
                return gens[0]
    return None


def _extract_models(text):
    """Extract potential Odoo model names from error text (e.g. res.partner, sale.order)."""
    candidates = set(re.findall(r"\b[a-z][a-z0-9_]+(?:\.[a-z][a-z0-9_]+)+\b", text))
    blacklist = {
        "os.path", "sys.path", "datetime.now", "fields.Date", "fields.Datetime",
        "api.model", "api.depends", "api.onchange", "api.constrains", "unittest.case"
    }
    return candidates - blacklist


def _extract_traceback_frames(text):
    """Extract (file_path, line_no, func_name) from python traceback."""
    pattern = re.compile(r'File "([^"]+)", line (\d+), in (\w+)')
    return pattern.findall(text)


def _get_code_snippet(file_path, line_no, radius=5):
    """Read snippet around the failure line."""
    if not os.path.isfile(file_path):
        return ""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        line_idx = int(line_no) - 1
        start = max(0, line_idx - radius)
        end = min(len(lines), line_idx + radius + 1)
        out = []
        for i in range(start, end):
            prefix = ">> " if i == line_idx else "   "
            out.append(f"{prefix}{i+1:4d}: {lines[i].rstrip()}")
        return "\n".join(out)
    except Exception:
        return ""


def _query_model_context(db_path, model_name):
    """Query KG SQLite DB for model fields and definitions."""
    if not db_path or not os.path.isfile(db_path):
        return ""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        
        # Check model definition
        cur.execute(
            "SELECT module, rel_path, class_name, line FROM node_model_def WHERE model_name = ? LIMIT 3",
            (model_name,)
        )
        defs = cur.fetchall()
        
        # Check fields
        cur.execute(
            "SELECT field_name, field_type, compute_method, required, readonly, comodel_literal FROM node_field WHERE model_name = ? LIMIT 25",
            (model_name,)
        )
        fields = cur.fetchall()
        conn.close()
        
        if not defs and not fields:
            return ""
        
        lines = [f"Model: '{model_name}':"]
        if defs:
            for mod, rp, cls, ln in defs:
                lines.append(f"  [Defined in] {mod} -> {rp}:{ln} (class {cls})")
        if fields:
            lines.append("  [Fields Schema]:")
            for fn, ft, comp, req, ro, comod in fields:
                parts = [ft]
                if comod: parts.append(f"-> {comod}")
                if comp: parts.append(f"compute={comp}")
                if req: parts.append("required")
                if ro: parts.append("readonly")
                lines.append(f"    - {fn} ({', '.join(parts)})")
        return "\n".join(lines)
    except Exception:
        return ""


def generate_diagnostic_prompt(module_name, error_output, code_context="", version="19", project_name=""):
    proj = project_name or module_name
    db_path = _find_kg_db(proj, version)
    
    # 1. Parse traceback frames & code snippets
    frames = _extract_traceback_frames(error_output)
    snippets = []
    if frames:
        last_file, last_line, last_func = frames[-1]
        snip = _get_code_snippet(last_file, last_line)
        if snip:
            snippets.append(f"File: {last_file} (Line {last_line} in {last_func}):\n{snip}")

    # 2. Extract models and query knowledge graph
    models = _extract_models(error_output)
    kg_contexts = []
    if db_path and models:
        for m in list(models)[:3]:  # query top 3 mentioned models
            ctx = _query_model_context(db_path, m)
            if ctx:
                kg_contexts.append(ctx)

    # 3. Assemble prompt payload
    prompt_parts = [
        "======================================================================",
        f"🚨 ODOO {version} AI DIAGNOSTIC & ROOT-CAUSE RESOLUTION PAYLOAD",
        "======================================================================",
        f"Module: {module_name}",
        f"Project: {proj}",
        f"Target Framework: Odoo {version}.0 (Master / Cutting-Edge)",
        "",
        "----------------------------------------------------------------------",
        "1. ERROR LOG & TRACEBACK:",
        "----------------------------------------------------------------------",
        error_output.strip(),
    ]

    if snippets:
        prompt_parts.extend([
            "",
            "----------------------------------------------------------------------",
            "2. CODE SNIPPET AT FAILURE POINT:",
            "----------------------------------------------------------------------",
            "\n\n".join(snippets)
        ])

    if kg_contexts:
        prompt_parts.extend([
            "",
            "----------------------------------------------------------------------",
            "3. KNOWLEDGE GRAPH CONTEXT (AST Model & Field Schema):",
            "----------------------------------------------------------------------",
            "\n\n".join(kg_contexts)
        ])

    if code_context and not snippets:
        prompt_parts.extend([
            "",
            "----------------------------------------------------------------------",
            "ADDITIONAL CODE CONTEXT:",
            "----------------------------------------------------------------------",
            code_context.strip()
        ])

    prompt_parts.extend([
        "",
        "----------------------------------------------------------------------",
        "INSTRUCTIONS FOR AI ASSISTANT (Gemini / Claude / Cursor):",
        "----------------------------------------------------------------------",
        f"1. Analyze the exact line failure and Knowledge Graph schema above.",
        f"2. Check for Odoo {version} breaking changes (e.g. self.env._(), Domain, default_res_ids).",
        f"3. Provide the minimal, surgical fix as a unified diff or targeted code snippet.",
        f"4. Preserve all existing docstrings and logic without rewriting unchanged parts.",
        "======================================================================"
    ])

    return "\n".join(prompt_parts)
