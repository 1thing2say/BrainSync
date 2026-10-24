// ui/voice_core.js: which browser voice speaks when a reply has no Cartesia clip.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const VC = require("../../ui/voice_core.js");

const v = (name, lang, localService, extra = {}) => ({ name, lang, localService, ...extra });

// What Chrome on the demo Mac lists, give or take.
const MAC = [
  v("Albert", "en-US", true),
  v("Samantha", "en-US", true, { default: true }),
  v("Daniel", "en-GB", true),
  v("Google US English", "en-US", false),
  v("Google UK English Female", "en-GB", false),
  v("Thomas", "fr-FR", true),
];

test("a local voice beats Chrome's network voices", () => {
  assert.equal(VC.best(MAC).name, "Samantha");
});

test("a downloaded Premium or Enhanced voice beats the compact default", () => {
  assert.equal(VC.best([...MAC, v("Ava (Enhanced)", "en-US", true)]).name, "Ava (Enhanced)");
  assert.equal(VC.best([...MAC, v("Ava (Enhanced)", "en-US", true), v("Zoe (Premium)", "en-US", true)]).name,
    "Zoe (Premium)");
});

test("an Online (Natural) voice gets no bonus: it needs the internet", () => {
  const edge = [v("Microsoft Aria Online (Natural) - English (United States)", "en-US", false),
    v("Microsoft Zira - English (United States)", "en-US", true)];
  assert.equal(VC.best(edge).name, "Microsoft Zira - English (United States)");
});

test("en-US beats other English, and novelty voices lose", () => {
  assert.equal(VC.best([v("Daniel", "en-GB", true), v("Samantha", "en_US", true)]).name, "Samantha");
  assert.equal(VC.best([v("Zarvox", "en-US", true), v("Daniel", "en-GB", true)]).name, "Daniel");
});

test("only network or non-English voices: an English one if there is any, else none", () => {
  assert.equal(VC.best([v("Google US English", "en-US", false), v("Thomas", "fr-FR", true)]).name,
    "Google US English");
  assert.equal(VC.best([v("Thomas", "fr-FR", true)]), null);
  assert.equal(VC.best([]), null);
  assert.equal(VC.best(undefined), null);
});
