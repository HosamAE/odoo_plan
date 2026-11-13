"use strict";
const assert = require("assert");
const { validateName, validateLang, safeLang, snapLabel } = require("../inputs");

for (const ok of ["sale_pro", "A1", " crm "]) assert.strictEqual(validateName(ok), null, ok);
for (const bad of ["", "a b", 'a"; calc', "a;b", "a$(x)", "a`x`", "a&b", "../x", "a\nb"]) assert.notStrictEqual(validateName(bad), null, JSON.stringify(bad));

for (const ok of ["", "sv", "fr_CH", "sr@latin", " ar "]) assert.strictEqual(validateLang(ok), null, ok);
for (const bad of ["s", "sv; x", 'sv"', "sv fr", "toolong_x_y!"]) assert.notStrictEqual(validateLang(bad), null, bad);

assert.strictEqual(safeLang(" fr_CH "), "fr_CH");
assert.strictEqual(safeLang('sv" & calc'), "", "a setting that is not a language code is dropped");
assert.strictEqual(safeLang(undefined), "");

assert.strictEqual(snapLabel('before"; DROP'), "before___DROP");
assert.strictEqual(snapLabel("  a-b.c "), "a_b_c");
assert.ok(!/["`$;&|<>\ ]/.test(snapLabel('x"`$;&|<>\ y')));
console.log("inputs OK");
