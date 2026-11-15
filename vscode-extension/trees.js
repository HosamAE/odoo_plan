"use strict";
// The three tree views of the sidebar - the control panel (server, quality, pages), the CI steps inside it, and the
// environment/maintenance tree - and the helpers they draw with. Split out of extension.js. It reads the extension's own
// `state` and `serverRun` objects in place; what changes (the cockpit, the CI child) comes through getters.
const vscode = require("vscode");
const path = require("path");
const fs = require("fs");

const FILE_LINE_RX = /^\s*([\w./\-]+\.(?:py|xml|csv|js|scss|po|yml|txt)):(\d+):\s*(.*)$/;

/** deps: {state, serverRun, findProject, getProjectPort, getOdooRoot, toolsStatus, progressLine, getCockpit, isCiRunning} */
function create({ state, serverRun, findProject, getProjectPort, getOdooRoot, toolsStatus, progressLine, getCockpit, isCiRunning }) {

  class ActionsProvider {
    constructor() {
      this._emitter = new vscode.EventEmitter();
      this.onDidChangeTreeData = this._emitter.event;
    }
    refresh() { this._emitter.fire(); }
    getTreeItem(el) { return el; }

    /** The state of the server: running or stopped (with its buttons inline), an upgrade in progress, the health of the log, the database. */
    serverItems(port) {
      const item = (label, icon, opts) => treeItem(label, Object.assign({ icon }, opts));
      const mine = state.servers.filter((s) => s.mine);
      const out = [];
      if (mine.length) {
        for (const s of mine) {   // any number of servers of this project: each one is shown, with its own port and database
          out.push(item(mine.length > 1 ? `Server :${s.port}: Running` : "Server: Running", "pass", {
            color: "testing.iconPassed", description: `localhost:${s.port} · pid ${s.pid}${s.db ? ` · ${s.db}` : ""}`,
            tooltip: s.conf ? `Started with ${s.conf}` : "Started without --config", extra: { contextValue: "server-running" },
          }));
        }
      } else if (state.portOpen) {
        out.push(item("Server: Running", "pass", { color: "testing.iconPassed", description: `localhost:${port} · not a local process`,
          tooltip: "Something answers on this port (Docker, WSL or another machine).", extra: { contextValue: "server-running" } }));
      } else {
        out.push(item("Server: Stopped", "circle-outline", { description: `localhost:${port}`, extra: { contextValue: "server-stopped" } }));
      }
      if (state.upgrade) {
        out.unshift(item(`Upgrading ${state.upgrade.what} on ${state.upgrade.db}`, "sync~spin", {
          description: state.upgrade.text, tooltip: `${state.upgrade.errors || 0} error(s), ${state.upgrade.warnings || 0} warning(s) so far. Click to see them.`,
          command: { command: "odooCi.issueShow", title: "Show errors and warnings" } }));
      }
      const h = state.health;
      if (h && (state.serverRunning || h.state === "failed")) {
        const label = { healthy: "Health: Healthy", starting: h.progress && !h.progress.finished ? `Health: Starting... ${h.progress.index}/${h.progress.total}` : "Health: Starting...", problems: `Health: Problems (${(h.errors || 0) + (h.critical || 0) + (h.http_500 || 0)})`, failed: "Health: Failed" }[h.state] || "Health";
        const icon = { healthy: "pass-filled", starting: "loading~spin", problems: "warning", failed: "error" }[h.state] || "question";
        const color = { healthy: "testing.iconPassed", starting: undefined, problems: "list.warningForeground", failed: "testing.iconFailed" }[h.state];
        const issue = h.first_issue;
        out.push(item(label, icon, {
          color, description: issue ? issue.message.slice(0, 60) : (h.progress && h.state === "starting" ? progressLine(h.progress) : (h.warnings ? `${h.warnings} warning(s)` : "")),
          tooltip: issue ? `${issue.message}${h.hint ? `\n\n${h.hint}` : ""}${issue.location ? `\n\nIn your code: ${issue.location.file}:${issue.location.line}` : ""}` : "No errors in the server's log.",
          command: { command: "odooCi.serverHealth", title: "Server health" },
        }));
      }
      const serving = mine[0] && mine[0].db ? mine[0].db : serverRun.db;
      if (serving && state.serverRunning) out.push(item("Database", "database", { description: serving, tooltip: "The database the running server uses." }));
      return out;
    }

    /** A row that opens one page of the cockpit. */
    page(label, icon, tab, description, extra) {
      return treeItem(label, { icon, description, extra, command: { command: "odooCi.cockpitTab", title: label, arguments: [tab] } });
    }

    getChildren(el) {
      if (el && (el._phase || el._details)) return getStepsHelper().getChildren(el);   // the CI progress lives in this tree now
      const project = findProject();
      if (!project) {
        const item = new vscode.TreeItem("No active Odoo project detected", vscode.TreeItemCollapsibleState.None);
        item.description = "Open a workspace inside ProjectsOdoo<NN>/<project>";
        item.iconPath = new vscode.ThemeIcon("info");
        return [item];
      }

      const port = getProjectPort(project);

      if (!el) {
        const up = state.servers.some((s) => s.mine) || state.portOpen;
        const rows = [treeItem(`${project.name} · Odoo ${project.version}`, {
          icon: "dashboard", description: (up ? `running :${port}` : "stopped") + (toolsStatus(project).found ? "" : " · tools missing"), tooltip: "Open the Cockpit Odoo: server, databases, quality, errors, graph, tools",
          command: { command: "odooCi.cockpitPanel", title: "Open Cockpit" } })];
        const running = state.servers.filter((s) => s.mine).map((s) => s.port);
        const server = new vscode.TreeItem(running.length > 1 ? `Servers (${running.map((p) => `:${p}`).join(" ")})` : `Server (Port ${running[0] || port})`, vscode.TreeItemCollapsibleState.Expanded);
        server.iconPath = new vscode.ThemeIcon(state.serverRunning ? "radio-tower" : "server");
        server._kind = "server";
        rows.push(server);
        const quality = new vscode.TreeItem("Quality", state.phases.length ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None);
        quality.iconPath = new vscode.ThemeIcon("shield");
        quality.description = ciSummary();
        quality.contextValue = "quality";
        quality.command = { command: "odooCi.cockpitTab", title: "Quality", arguments: ["quality"] };
        quality._kind = "ci";
        rows.push(quality);
        rows.push(this.page("Databases", "database", "dbs"));
        rows.push(this.page("Graph", "type-hierarchy", "graph", graphSummary(), { contextValue: "graph" }));
        rows.push(this.page("Server log", "output", "log"));
        rows.push(this.page("Tools & Maintenance", "tools", "tools"));
        rows.push(this.page("Remote servers", "remote", "remote"));
        return rows;
      }

      if (el._kind === "server") return this.serverItems(port);
      if (el._kind === "ci") return getStepsHelper().getChildren();
      return [];
    }
  }

  /** One word for the last CI run, for the Quality row. */
  function ciSummary() {
    if (isCiRunning()) return "running";
    if (!state.result) return "";
    if (state.result.blocked) return "blocked";
    if (state.result.cancelled) return "cancelled";
    return state.result.passed ? "passed" : "failed";
  }

  /** How the project's graph stands, for the Graph row: fresh, behind the code, not built. */
  function graphSummary() {
    const cockpit = getCockpit();
    const g = cockpit && cockpit.graph ? cockpit.graph.state() : null;
    if (!g) return "";
    if (g.busy) return "updating";
    const s = g.status && g.status.state;
    return { fresh: "fresh", stale: "behind the code", missing: "not built", unconfigured: "no config" }[s] || "";
  }


  const ICONS = {
    ok: () => new vscode.ThemeIcon("pass", new vscode.ThemeColor("testing.iconPassed")),
    fail: () => new vscode.ThemeIcon("error", new vscode.ThemeColor("testing.iconFailed")),
    running: () => new vscode.ThemeIcon("loading~spin"),
    skip: () => new vscode.ThemeIcon("debug-step-over", new vscode.ThemeColor("disabledForeground")),
    pending: () => new vscode.ThemeIcon("circle-large-outline"),
  };

  class StepsProvider {
    constructor() {
      this._emitter = new vscode.EventEmitter();
      this.onDidChangeTreeData = this._emitter.event;
    }
    refresh() { this._emitter.fire(); }
    getTreeItem(el) { return el; }

    getChildren(el) {
      if (!state.phases.length) return [];
      if (!el) {
        const items = [];
        if (state.result) {
          const r = new vscode.TreeItem(
            state.result.blocked ? "Blocked: an Odoo server is running"
              : state.result.passed ? (state.mode === "full" ? "Passed - safe to push" : "Static checks passed")
              : "Failed - fix before pushing",
            vscode.TreeItemCollapsibleState.None);
          r.iconPath = state.result.passed ? ICONS.ok() : ICONS.fail();
          r.description = state.project ? `${state.project.name} (Odoo ${state.project.version})` : "";
          items.push(r);
        }
        for (const phase of state.phases) {
          const steps = state.steps.filter((s) => s.phase === phase.phase);
          const status = steps.some((s) => s.status === "fail") ? "fail"
            : steps.some((s) => s.status === "running") ? "running"
            : steps.length && steps.every((s) => s.status === "ok" || s.status === "skip") ? "ok" : "pending";
          const item = new vscode.TreeItem(phase.name, vscode.TreeItemCollapsibleState.Expanded);
          item.iconPath = ICONS[status]();
          item.contextValue = "phase";
          item._phase = phase.phase;
          const done = steps.filter((s) => s.status === "ok").length;
          item.description = steps.length ? `${done}/${steps.length}` : "";
          items.push(item);
        }
        return items;
      }
      if (el._phase) {
        return state.steps.filter((s) => s.phase === el._phase).map((s) => {
          const hasDetails = (s.details || []).length > 0;
          const item = new vscode.TreeItem(s.name,
            hasDetails ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None);
          item.iconPath = (ICONS[s.status] || ICONS.pending)();
          if (s.seconds !== undefined) item.description = s.seconds >= 60 ? `${Math.round(s.seconds / 60)} min` : `${s.seconds}s`;
          item._details = s.details || [];
          return item;
        });
      }
      if (el._details) {
        return el._details.map((line) => {
          const item = new vscode.TreeItem(line, vscode.TreeItemCollapsibleState.None);
          item.iconPath = new vscode.ThemeIcon("chevron-right");
          const m = line.match(FILE_LINE_RX);
          if (m && state.project) {
            const file = path.join(state.project.root, m[1]);
            const lineNo = Math.max(0, +m[2] - 1);
            item.command = {
              command: "vscode.open",
              title: "Open file",
              arguments: [vscode.Uri.file(file), { selection: new vscode.Range(lineNo, 0, lineNo, 0) }],
            };
          }
          return item;
        });
      }
      return [];
    }
  }

  function readBranch(toolsDir) {
    try {
      const head = fs.readFileSync(path.join(toolsDir, ".git", "HEAD"), "utf8").trim();
      return head.startsWith("ref:") ? head.replace(/^ref:\s*refs\/heads\//, "") : head.slice(0, 8);
    } catch (e) {
      return null;
    }
  }

  /** What one Odoo version has on this machine. */
  function versionInfo(root, v) {
    const dir = path.join(root, `odoo${v}`);
    const tools = path.join(dir, "tools");
    const info = {
      v,
      tools: fs.existsSync(path.join(tools, "cli.py")),
      source: fs.existsSync(path.join(dir, "odoo", "odoo-bin")),
      venv: fs.existsSync(path.join(dir, "venv")),
      branch: readBranch(tools),
    };
    info.ready = info.tools && info.source && info.venv;
    return info;
  }

  function treeItem(label, opts) {
    const item = new vscode.TreeItem(label, opts.collapsible || vscode.TreeItemCollapsibleState.None);
    if (opts.icon) item.iconPath = new vscode.ThemeIcon(opts.icon, opts.color ? new vscode.ThemeColor(opts.color) : undefined);
    if (opts.description) item.description = opts.description;
    if (opts.tooltip) item.tooltip = opts.tooltip;
    if (opts.command) item.command = opts.command;
    Object.assign(item, opts.extra || {});
    return item;
  }

  class EnvProvider {
    constructor() {
      this._emitter = new vscode.EventEmitter();
      this.onDidChangeTreeData = this._emitter.event;
    }
    refresh() { this._emitter.fire(); }
    getTreeItem(el) { return el; }

    /** The Odoo version of the open project and what it needs. Other versions and other projects are not this window's. */
    getChildren(el) {
      if (!el) {
        const project = findProject();
        const nodes = [];
        if (project) {
          const info = versionInfo(getOdooRoot(project), project.version);
          nodes.push(...this.versionChildren(info));
        } else {
          nodes.push(treeItem("No active Odoo project detected", { icon: "info", description: "Open a workspace inside ProjectsOdoo<NN>/<project>" }));
        }
        nodes.push(treeItem("Maintenance", { collapsible: vscode.TreeItemCollapsibleState.Expanded, icon: "tools", extra: { _kind: "maint" } }));
        return nodes;
      }
      if (el._kind === "maint") return this.maintenanceItems();
      return [];
    }

    versionChildren(info) {
      const out = [];
      const mark = (ok, label, hint) => treeItem(label, {
        icon: ok ? "check" : "circle-slash",
        color: ok ? "testing.iconPassed" : "list.warningForeground",
        description: ok ? "" : hint,
      });
      out.push(treeItem(`Odoo ${info.v}`, {
        icon: info.ready ? "pass" : "warning", color: info.ready ? "testing.iconPassed" : "list.warningForeground",
        description: info.branch ? `tools: branch ${info.branch}` : "no tools",
        tooltip: info.ready ? `Odoo ${info.v} is ready` : `Odoo ${info.v} is incomplete: use "Set up this version"`,
      }));
      out.push(mark(info.tools, "Tools", "odoo-ai new-version " + info.v));
      out.push(mark(info.source, "Odoo source", "download it (setup lists the command)"));
      out.push(mark(info.venv, "Python venv", "create it (setup lists the command)"));
      if (!info.ready) {
        out.push(treeItem(`Set up Odoo ${info.v}…`, {
          icon: "rocket", command: { command: "odooCi.newVersion", title: "Set up", arguments: [info.v] },
        }));
      }
      return out;
    }

    maintenanceItems() {
      const item = (label, icon, command, description) => treeItem(label, { icon, description, command: { command, title: label } });
      return [
        item("Set up an Odoo version…", "rocket", "odooCi.newVersion", "one command"),
        item("Doctor (health check)", "pulse", "odooCi.doctor"),
        ...(vscode.workspace.getConfiguration("odooCi").get("maintainerTools") ? [
          item("Tools CI (before push)", "beaker", "odooCi.selfTest", "all 4 versions, CI conditions"),
          item("Version branches vs main", "git-compare", "odooCi.syncVersions", "report only")] : []),
        item("Scan staged files for secrets", "shield", "odooCi.secretScan"),
        item("Translations: scan, update, export", "globe", "odooCi.translations"),
        item("Install secret pre-commit hook", "lock", "odooCi.secretHook", "this repository"),
        item("Project gates (Odoo rules)", "law", "odooCi.gates"),
        item("Open the layout guide", "book", "odooCi.openLayout"),
      ];
    }
  }


  let stepsHelper = null;
  function getStepsHelper() {
    if (!stepsHelper) stepsHelper = new StepsProvider();
    return stepsHelper;
  }

  return { ActionsProvider, EnvProvider, versionInfo };
}

module.exports = { create };
