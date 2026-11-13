"use strict";
// The commands that open a terminal and type a tools command into it. They need the open project and the tools' paths,
// which the extension hands over; what a person types is checked in inputs.js before it reaches a command line.
const vscode = require("vscode");
const { validateName, validateLang, safeLang, snapLabel } = require("./inputs");

/** deps: {findProject, getPythonExe, getCliPath, toolsContext, moduleOfFile} */
function create(deps) {
  const { findProject, getPythonExe, getCliPath, toolsContext, moduleOfFile } = deps;

  function runFastTest() {
    const project = findProject();
    if (!project) return vscode.window.showErrorMessage("No active Odoo project found.");
    vscode.window.showInputBox({
      prompt: "Enter addon name to test on ephemeral template database",
      placeHolder: "e.g. sale_discount_pro or custom_crm",
      validateInput: validateName,
    }).then(addon => {
      if (!addon) return;
      const term = vscode.window.createTerminal({ name: `Odoo Fast Test: ${addon}` });
      term.show();
      const python = getPythonExe(project);
      const cli = getCliPath(project);
      term.sendText(cliLine(python, cli, `test -m "${addon}" --fast`));
    });
  }

  function runNewAddon() {
    const project = findProject();
    if (!project) return vscode.window.showErrorMessage("No active Odoo project found.");
    vscode.window.showInputBox({
      prompt: "Enter technical name for new addon",
      placeHolder: "e.g. custom_portal_crm or hr_payroll_geo",
      validateInput: validateName,
    }).then(addon => {
      if (!addon) return;
      const term = vscode.window.createTerminal({ name: `Odoo New Addon: ${addon}` });
      term.show();
      const python = getPythonExe(project);
      const cli = getCliPath(project);
      term.sendText(cliLine(python, cli, `new-addon "${addon}"`));
    });
  }

  function runDoctor() {
    const { python, cli, root } = toolsContext();
    runInTerminal("Odoo-AI Doctor", cliLine(python, cli, "doctor"), root);
  }

  /** `odoo-ai audit` (the module's security CSV and manifest) on the module of the file being edited. */
  function runAudit() {
    const project = findProject();
    if (!project) return vscode.window.showErrorMessage("No active Odoo project found.");
    const editor = vscode.window.activeTextEditor;
    const owner = editor && editor.document.uri.scheme === "file" ? moduleOfFile(editor.document.uri.fsPath) : null;
    if (!owner) return vscode.window.showWarningMessage("Open a file of the module to audit: its security CSV and manifest are checked.");
    runInTerminal(`Odoo audit: ${owner.name}`, cliLine(getPythonExe(project), getCliPath(project), `audit -p "${owner.folder}"`), project.root);
  }

  function runDbSnapSave() {
    const project = findProject();
    if (!project) return;
    vscode.window.showInputBox({
      prompt: "Enter snapshot label (e.g. before_update or milestone_1)",
      placeHolder: "before_update"
    }).then(label => {
      if (!label) return;
      label = snapLabel(label);
      const term = vscode.window.createTerminal({ name: `Odoo DB Snap: ${label}` });
      term.show();
      const python = getPythonExe(project);
      const cli = getCliPath(project);
      term.sendText(cliLine(python, cli, `db-snap save "${label}"`));
    });
  }

  function runDbSnapRestore() {
    const project = findProject();
    if (!project) return;
    vscode.window.showInputBox({
      prompt: "Enter snapshot label to restore",
      placeHolder: "before_update"
    }).then(async (typed) => {
      if (!typed) return;
      const label = snapLabel(typed);
      // a restore drops the live database and brings the snapshot back: the person confirms it
      const sure = await vscode.window.showWarningMessage(`Replace the project's database by the snapshot "${label}"? The current data is dropped.`, { modal: true }, "Restore");
      if (sure !== "Restore") return;
      const term = vscode.window.createTerminal({ name: `Odoo DB Restore: ${label}` });
      term.show();
      const python = getPythonExe(project);
      const cli = getCliPath(project);
      term.sendText(cliLine(python, cli, `db-snap restore "${label}"`));
    });
  }

  function runI18nScan() {
    const project = findProject();
    if (!project) return vscode.window.showErrorMessage("No active Odoo project found.");
    vscode.window.showInputBox({
      prompt: "Enter addon name to scan/sync translations (e.g. sale_portal)",
      placeHolder: "e.g. sale_portal or custom_crm",
      validateInput: validateName,
    }).then(mod => {
      if (!mod) return;
      const config = vscode.workspace.getConfiguration("odooCi");
      const defaultLang = config.get("defaultLang") || "";
      vscode.window.showInputBox({
        prompt: "Target language code (leave blank to scan all existing PO files, or specify e.g. sv, ar, fr)",
        value: defaultLang,
        placeHolder: "e.g. sv, ar, de, fr (leave blank for all)",
        validateInput: validateLang,
      }).then(lang => {
        const targetLang = safeLang((lang !== undefined && lang !== null && lang.trim()) ? lang : defaultLang);
        const term = vscode.window.createTerminal({ name: `Odoo i18n: ${mod}` });
        term.show();
        const python = getPythonExe(project);
        const cli = getCliPath(project);
        const langFlag = targetLang ? ` -l "${targetLang}"` : "";
        term.sendText(cliLine(python, cli, `i18n scan -m "${mod}"${langFlag}`));
      });
    });
  }

  function runInitFastDb() {
    const project = findProject();
    const python = getPythonExe(project);
    const ver = (project && project.version) || "19";
    const fastScript = `C:\\odoo\\odoo${ver}\\tools\\db_fast.py`;
    const term = vscode.window.createTerminal({ name: `Odoo Init Fast DB (${ver})` });
    term.show();
    term.sendText(cliLine(python, fastScript, `init -v "${ver}"`));
  }

  /** `python cli args` for the terminal the user really has: PowerShell needs `&` before a quoted path, cmd/bash do not. */
  function cliLine(python, script, args) {
    const shell = (vscode.env.shell || "").toLowerCase();
    const ps = shell.includes("powershell") || shell.includes("pwsh");
    return `${ps ? "& " : ""}"${python}" "${script}" ${args || ""}`.trim();
  }

  function runInTerminal(name, line, cwd) {
    const term = vscode.window.createTerminal({ name, cwd });
    term.show();
    term.sendText(line);
  }

  function runSelfTest() {
    const { python, cli, root } = toolsContext();
    // tools-ci builds every version and tests it the way the CI does (no Odoo packages, no C:\odoo); test-self would not
    runInTerminal("Odoo tools CI (before push)", cliLine(python, cli, "tools-ci"), root);
  }

  function runSyncVersions() {
    const { python, cli, root } = toolsContext();
    runInTerminal("Odoo version branches", cliLine(python, cli, "sync-versions"), root);
  }

  function runSecretScan() {
    const { python, cli, cwd } = toolsContext();
    runInTerminal("Secret scan", cliLine(python, cli, "secrets --staged"), cwd);
  }

  function runSecretHook() {
    const { python, cli, cwd } = toolsContext();
    runInTerminal("Secret hook", cliLine(python, cli, "secrets --install-hook"), cwd);
  }

  function runGates() {
    const { project, python, cli } = toolsContext();
    if (!project) return vscode.window.showErrorMessage("Odoo CI: open a project first (ProjectsOdoo<NN>/<project>).");
    runInTerminal("Odoo gates", cliLine(python, cli, `gates --path "${project.root}"`), project.root);
  }

  return { cliLine, runInTerminal, runFastTest, runNewAddon, runDoctor, runAudit, runDbSnapSave, runDbSnapRestore, runI18nScan, runInitFastDb, runSelfTest, runSyncVersions, runSecretScan, runSecretHook, runGates };
}

module.exports = { create };
