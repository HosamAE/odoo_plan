"use strict";
// What a person types into the editor and the terminal command line it ends up in. No `vscode` here, so it is tested alone.

const NAME = /^[A-Za-z0-9_]+$/;
const LANG = /^[A-Za-z]{2,3}([_@-][A-Za-z0-9]+)?$/;

/** An input-box validator for a module or addon name: null when it is fine, else the message. */
function validateName(value) {
  return NAME.test(String(value || "").trim()) ? null : "Letters, digits and _ only";
}

/** An input-box validator for a language code (blank is allowed). */
function validateLang(value) {
  return !value || LANG.test(String(value).trim()) ? null : "A language code such as sv, ar or fr_CH";
}

/** A language code that is safe in a command line, or "" (a setting is a command line too). */
function safeLang(value) {
  const text = String(value || "").trim();
  return LANG.test(text) ? text : "";
}

/** A snapshot label as the tool names it: anything but letters, digits and _ becomes _. */
function snapLabel(text) {
  return String(text).trim().replace(/[^a-zA-Z0-9_]/g, "_");
}

module.exports = { validateName, validateLang, safeLang, snapLabel };
