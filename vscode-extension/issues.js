"use strict";
// The errors and warnings of a server or upgrade log: the tree to pick from, next/previous, the filter, the log opened
// at a line and painted. Split out of extension.js; it shares the extension's `state` and `serverRun` objects.
const vscode = require("vscode");
const path = require("path");
const fs = require("fs");

/** deps: {state, serverRun} - the extension's own objects, read and written in place. */
function create({ state, serverRun }) {
  // ------------------------------------------------------------- the issues: errors and warnings to walk through
  //
  // The health row says HOW MANY. This is the way to each one: a tree to pick from, next/previous to step through, a
  // filter, the log file opened at the line with error lines red and warning lines yellow (and marks on the scroll
  // bar), and the project's own file when a traceback ends in it. The data is `items`/`marks` from server_scan.py.

  const issueFilter = { kind: "all", db: null, module: null };
  const issueCursor = { at: -1 };
  let issuesTree, errorDeco, warningDeco;

  /** What the Issues view lists now: the last log judged (the server's, or an upgrade's), seen through the filter. */
  function issuesSource() {
    return state.issuesSource || (state.health && serverRun.log ? { label: "Server", log: serverRun.log, verdict: state.health } : null);
  }

  function setIssuesSource(label, log, verdict) {
    state.issuesSource = verdict ? { label, log, verdict } : null;
    issueCursor.at = -1;
    if (issuesTree) issuesTree.refresh();
    decorateVisibleLogs();
  }

  function visibleIssues() {
    const src = issuesSource();
    const all = (src && src.verdict && src.verdict.items) || [];
    return all.filter((i) => (issueFilter.kind === "all" || i.kind === issueFilter.kind)
      && (!issueFilter.db || i.db === issueFilter.db) && (!issueFilter.module || i.module === issueFilter.module));
  }

  /** Every line an issue happened on, in the order a person walks them: errors first, then warnings, by line. */
  function occurrences() {
    const out = [];
    for (const issue of visibleIssues()) for (const line of issue.lines && issue.lines.length ? issue.lines : [issue.line]) out.push({ issue, line });
    return out;
  }

  function shortText(text, n) {
    const flat = String(text || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
    return flat.length > n ? `${flat.slice(0, n - 1)}…` : flat;
  }

  class IssuesProvider {
    constructor() {
      this._emitter = new vscode.EventEmitter();
      this.onDidChangeTreeData = this._emitter.event;
    }
    refresh() { this._emitter.fire(); }
    getTreeItem(el) { return el; }
    getChildren(el) {
      const src = issuesSource();
      if (!src) {
        const none = new vscode.TreeItem("No server log yet", vscode.TreeItemCollapsibleState.None);
        none.description = "Start the server from here, or run an upgrade";
        none.iconPath = new vscode.ThemeIcon("info");
        return el ? [] : [none];
      }
      const items = visibleIssues();
      if (!el) {
        const groups = [];
        const filtered = issueFilter.kind !== "all" || issueFilter.db || issueFilter.module;
        const head = new vscode.TreeItem(`${src.label}: ${src.verdict.state}`, vscode.TreeItemCollapsibleState.None);
        head.description = filtered ? `filter: ${[issueFilter.kind !== "all" ? issueFilter.kind : "", issueFilter.db || "", issueFilter.module || ""].filter(Boolean).join(" · ")}  (click to change)` : "click to filter";
        head.iconPath = new vscode.ThemeIcon("filter");
        head.command = { command: "odooCi.issueFilter", title: "Filter" };
        groups.push(head);
        for (const kind of ["error", "warning"]) {
          const of = items.filter((i) => i.kind === kind);
          if (!of.length && issueFilter.kind !== "all") continue;
          const occurrencesCount = of.reduce((n, i) => n + i.count, 0);
          const g = new vscode.TreeItem(`${kind === "error" ? "Errors" : "Warnings"} (${of.length})`,
            of.length ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None);
          g.description = of.length ? `${occurrencesCount} occurrence${occurrencesCount === 1 ? "" : "s"}` : "none";
          g.iconPath = new vscode.ThemeIcon(kind === "error" ? "error" : "warning",
            new vscode.ThemeColor(kind === "error" ? "testing.iconFailed" : "list.warningForeground"));
          g._kind = kind;
          groups.push(g);
        }
        return groups;
      }
      return items.filter((i) => i.kind === el._kind).map((issue) => {
        const label = shortText(issue.detail || issue.message, 100);
        const node = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
        node.issue = issue;
        node.description = [issue.module ? `${issue.module}` : "", issue.db && issue.db !== "?" ? issue.db : "",
          issue.count > 1 ? `×${issue.count}` : "", `line ${issue.line}`].filter(Boolean).join(" · ");
        node.iconPath = new vscode.ThemeIcon(issue.kind === "error" ? "error" : "warning",
          new vscode.ThemeColor(issue.kind === "error" ? "testing.iconFailed" : "list.warningForeground"));
        const tip = new vscode.MarkdownString();
        tip.appendText(`${issue.message}\n`);
        if (issue.detail) tip.appendText(`\n${issue.detail}\n`);
        if (issue.location) tip.appendText(`\nIn your code: ${issue.location.file}:${issue.location.line}\n`);
        if (issue.count > 1) tip.appendText(`\nHappened ${issue.count} times (lines ${issue.lines.slice(0, 8).join(", ")}${issue.lines.length > 8 ? ", ..." : ""}).`);
        node.tooltip = tip;
        node.contextValue = issue.location ? "issueWithSource" : "issue";
        node.command = { command: "odooCi.issueOpen", title: "Open", arguments: [issue] };
        return node;
      });
    }
  }

  function issueOf(arg) {
    return arg && arg.issue ? arg.issue : arg;
  }

  function logFileOf(src) {
    return (src && src.log) || serverRun.log;
  }

  /** Open the log in an editor at a line, painted. */
  async function openLogAt(line, src) {
    const file = logFileOf(src || issuesSource());
    if (!file || !fs.existsSync(file)) return vscode.window.showWarningMessage("There is no server log to open yet.");
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
    const editor = await vscode.window.showTextDocument(doc, { preview: false, viewColumn: vscode.ViewColumn.Beside, preserveFocus: false });
    decorateLog(editor);
    if (line) {
      const pos = new vscode.Position(Math.max(0, line - 1), 0);
      editor.selection = new vscode.Selection(pos, pos);
      editor.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
    }
  }

  async function openSourceAt(location) {
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(location.file));
    const editor = await vscode.window.showTextDocument(doc, { preview: false, viewColumn: vscode.ViewColumn.One });
    const pos = new vscode.Position(Math.max(0, location.line - 1), 0);
    editor.selection = new vscode.Selection(pos, pos);
    editor.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
  }

  /** Click on an issue: your code if the traceback ends in it, otherwise the log at its line. */
  async function openIssue(arg) {
    const issue = issueOf(arg);
    if (!issue) return;
    if (issue.location && fs.existsSync(issue.location.file)) {
      await openSourceAt(issue.location);
      await openLogAt(issue.line);
      return;
    }
    await openLogAt(issue.line);
  }

  async function stepIssue(direction) {
    const list = occurrences();
    if (!list.length) return vscode.window.showInformationMessage("No errors or warnings to go to (check the filter).");
    issueCursor.at = (issueCursor.at + direction + list.length) % list.length;
    const { issue, line } = list[issueCursor.at];
    await openLogAt(line);
    vscode.window.setStatusBarMessage(`$(${issue.kind === "error" ? "error" : "warning"}) ${issue.kind} ${issueCursor.at + 1} of ${list.length}: ${shortText(issue.detail || issue.message, 80)}`, 6000);
  }

  async function pickIssue() {
    const list = visibleIssues();
    if (!list.length) return vscode.window.showInformationMessage("No errors or warnings to pick (check the filter).");
    const picked = await vscode.window.showQuickPick(list.map((issue) => ({
      label: `$(${issue.kind === "error" ? "error" : "warning"}) ${shortText(issue.detail || issue.message, 110)}`,
      description: [issue.module, issue.db !== "?" ? issue.db : "", issue.count > 1 ? `×${issue.count}` : "", `line ${issue.line}`].filter(Boolean).join(" · "),
      detail: issue.location ? `In your code: ${issue.location.file}:${issue.location.line}` : shortText(issue.message, 160),
      issue,
    })), { title: "Errors and warnings", placeHolder: "Type to search; pick one to open it", matchOnDescription: true, matchOnDetail: true });
    if (picked) await openIssue(picked.issue);
  }

  async function filterIssues() {
    const all = (issuesSource() && issuesSource().verdict.items) || [];
    const dbs = [...new Set(all.map((i) => i.db).filter((d) => d && d !== "?"))];
    const modules = [...new Set(all.map((i) => i.module).filter(Boolean))];
    const choices = [
      { label: "$(list-flat) Everything", action: "all" },
      { label: "$(error) Errors only", action: "error" },
      { label: "$(warning) Warnings only", action: "warning" },
    ];
    if (dbs.length > 1) choices.push({ label: "$(database) One database...", action: "db" });
    if (modules.length) choices.push({ label: "$(package) One module...", action: "module" });
    const picked = await vscode.window.showQuickPick(choices, { title: "Filter the errors and warnings" });
    if (!picked) return;
    if (picked.action === "all") Object.assign(issueFilter, { kind: "all", db: null, module: null });
    else if (picked.action === "error" || picked.action === "warning") issueFilter.kind = picked.action;
    else if (picked.action === "db") {
      const db = await vscode.window.showQuickPick(dbs, { title: "Database" });
      if (db) issueFilter.db = db;
    } else {
      const mod = await vscode.window.showQuickPick(modules, { title: "Module" });
      if (mod) issueFilter.module = mod;
    }
    issueCursor.at = -1;
    issuesTree.refresh();
  }

  // the log file, painted: error lines red, warning lines yellow, a mark on the scroll bar for each
  function decorationTypes() {
    if (!errorDeco) {
      errorDeco = vscode.window.createTextEditorDecorationType({
        isWholeLine: true, backgroundColor: "rgba(244, 71, 71, 0.18)", overviewRulerColor: "#f44747",
        overviewRulerLane: vscode.OverviewRulerLane.Full, borderWidth: "0 0 0 3px", borderStyle: "solid", borderColor: "#f44747",
      });
      warningDeco = vscode.window.createTextEditorDecorationType({
        isWholeLine: true, backgroundColor: "rgba(204, 167, 0, 0.16)", overviewRulerColor: "#cca700",
        overviewRulerLane: vscode.OverviewRulerLane.Full, borderWidth: "0 0 0 3px", borderStyle: "solid", borderColor: "#cca700",
      });
    }
    return { errorDeco, warningDeco };
  }

  function decorateLog(editor) {
    const src = issuesSource();
    if (!editor || !src || !src.verdict || !src.verdict.marks) return;
    if (path.resolve(editor.document.uri.fsPath).toLowerCase() !== path.resolve(logFileOf(src) || "").toLowerCase()) return;
    const { errorDeco: red, warningDeco: yellow } = decorationTypes();
    const range = (n) => new vscode.Range(Math.max(0, n - 1), 0, Math.max(0, n - 1), 0);
    editor.setDecorations(red, src.verdict.marks.error.map(range));
    editor.setDecorations(yellow, src.verdict.marks.warning.map(range));
  }

  function decorateVisibleLogs() {
    for (const editor of vscode.window.visibleTextEditors) decorateLog(editor);
  }

  function createTree() {
    issuesTree = new IssuesProvider();
    return issuesTree;
  }

  function dispose() {
    if (errorDeco) errorDeco.dispose();
    if (warningDeco) warningDeco.dispose();
  }

  return {
    createTree, dispose, IssuesProvider, issueFilter, issueCursor, issuesSource, setIssuesSource, visibleIssues, occurrences, shortText,
    issueOf, openLogAt, openSourceAt, openIssue, stepIssue, pickIssue, filterIssues, decorateLog, decorateVisibleLogs,
  };
}

module.exports = { create };
