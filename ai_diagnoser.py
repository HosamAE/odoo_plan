"""
Odoo AI Diagnoser Engine
Parses test failures & lint reports and generates structured prompt payloads for AI resolution.
"""

def generate_diagnostic_prompt(module_name, error_output, code_context="", version="17"):
    prompt = f"""
======================================================================
🚨 ODOO {version} AUTOMATED TEST / LINT DIAGNOSTIC PAYLOAD
======================================================================

Module Name: {module_name}
Odoo Version: Odoo {version}

----------------------------------------------------------------------
ERROR LOG & TRACEBACK SUMMARY:
----------------------------------------------------------------------
{error_output}

----------------------------------------------------------------------
CODE CONTEXT (If available):
----------------------------------------------------------------------
{code_context}

----------------------------------------------------------------------
INSTRUCTIONS FOR AI ASSISTANT (Gemini / Claude):
----------------------------------------------------------------------
1. Analyze the traceback or lint failure above.
2. Identify the root cause according to Odoo {version} ORM & View specifications.
3. Provide the exact fix as a concise code snippet or unified diff.
4. Do NOT rewrite unchanged parts of the module.
======================================================================
"""
    return prompt.strip()
