"use strict";
// Quick fixes for the Odoo rules shown as problems: a one-click rewrite where it is safe, and a noqa marker.
const vscode = require("vscode");

/** Index of the parenthesis that closes the one opened at `open`, or -1. */
function matchingParen(text, open) {
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    if (text[i] === "(") depth++;
    else if (text[i] === ")" && --depth === 0) return i;
  }
  return -1;
}

/** R3: `len(X.search(ARGS))` -> `X.search_count(ARGS)`; null when it is not safe to rewrite (e.g. `order=`). */
function fixLenSearch(text) {
  const lenAt = text.search(/\blen\(/);
  if (lenAt < 0) return null;
  const lenOpen = text.indexOf("(", lenAt);
  const lenClose = matchingParen(text, lenOpen);
  if (lenClose < 0) return null;
  const inner = text.slice(lenOpen + 1, lenClose);
  let from = 0;
  for (;;) {
    const at = inner.indexOf(".search(", from);
    if (at < 0) return null;
    const open = at + ".search".length;
    if (matchingParen(inner, open) === inner.length - 1) {
      const args = inner.slice(open + 1, -1);
      if (/\border\s*=/.test(args)) return null;   // search_count has no order
      return text.slice(0, lenAt) + inner.slice(0, at) + ".search_count(" + args + ")" + text.slice(lenClose + 1);
    }
    from = at + 1;
  }
}

class RuleFixes {
  provideCodeActions(document, _range, context) {
    const actions = [];
    for (const d of context.diagnostics) {
      if (d.source !== "Odoo CI" || !/^R\d+$/.test(String(d.code || ""))) continue;
      const line = document.lineAt(d.range.start.line);
      if (d.code === "R3") {
        const fixed = fixLenSearch(line.text);
        if (fixed) {
          const fix = new vscode.CodeAction("Use search_count()", vscode.CodeActionKind.QuickFix);
          fix.edit = new vscode.WorkspaceEdit();
          fix.edit.replace(document.uri, line.range, fixed);
          fix.diagnostics = [d];
          fix.isPreferred = true;
          actions.push(fix);
        }
      }
      const xml = document.languageId === "xml";
      const mark = new vscode.CodeAction(`This line must break ${d.code}: add noqa`, vscode.CodeActionKind.QuickFix);
      mark.edit = new vscode.WorkspaceEdit();
      mark.edit.insert(document.uri, line.range.end, xml ? " <!-- noqa: odoo19 REASON -->" : "  # noqa: odoo19 REASON");
      mark.diagnostics = [d];
      actions.push(mark);
    }
    return actions;
  }
}

module.exports = { RuleFixes, fixLenSearch, matchingParen };
