// ui/view_core.js: the state line, health line, mic card and selection timer the session page shows.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const VC = require("../../ui/view_core.js");

const READY = { decoder_connected: true, decoder_ok: true, display_ok: true, board_ok: true,
  llm_ok: true, mic: "listening", mic_muted: false, fake_decoder: false, keyboard_mode: false };

test("the banner names what the user does now, with a tone", () => {
  assert.deepEqual(VC.banner("STIMULATING", "", "page"), { text: "Look at a reply, or More options", tone: "look" });
  assert.equal(VC.banner("STIMULATING", "", "list").text, "Look at SAY or NEXT");
  assert.equal(VC.banner("PAUSED", "Stop pressed", "page").text, "Paused — Stop pressed");
  assert.equal(VC.banner("SPEAKING", "Yes, let's go.", "page").text, "Speaking");  // shown elsewhere
  assert.equal(VC.banner("SOMETHING_NEW", "", "page").text, "SOMETHING_NEW");
});

test("the state line in the diamond's centre is empty while the user picks", () => {
  for (const name of ["MENU_CUE", "STIMULATING", "CONFIRMING", "SELECTED", "SPEAKING", "REST"]) {
    assert.equal(VC.status(name, "", "page").text, "", name);
  }
  assert.deepEqual(VC.status("PAUSED", "Decoder disconnected", "page"),
                   { text: "Paused — Decoder disconnected", tone: "stop" });
  assert.deepEqual(VC.status("NOT_CAUGHT", "", "page"), { text: "Didn't catch that: look again", tone: "warn" });
  assert.equal(VC.status("IDLE", "", "page").text, "Waiting to start");
});

test("all fine is Ready", () => {
  assert.deepEqual(VC.health(READY), { level: "ok", text: "Ready", problems: [], warnings: [], flags: [] });
});

test("problems come first and name what is wrong", () => {
  const h = VC.health({ ...READY, decoder_connected: false, mic: "denied" });
  assert.equal(h.level, "bad");
  assert.deepEqual(h.problems, ["No decoder", "Mic: microphone blocked"]);
  assert.equal(VC.health({ ...READY, display_ok: false }).text, "Display off");
});

test("a fallback that still works is a warning, not a problem", () => {
  const h = VC.health({ ...READY, llm: "off", llm_ok: false, mic_muted: true });
  assert.equal(h.level, "warn");
  assert.equal(h.text, "Ready · Fixed replies only (no Claude) · Mic muted");
  assert.equal(VC.health({ ...READY, llm: "failed", llm_ok: false }).text,
    "Ready · Claude's last page failed: fixed list shown");
});

test("Claude not asked yet is no warning", () => {
  assert.equal(VC.health({ ...READY, llm: "waiting", llm_ok: false }).level, "ok");
  assert.equal(VC.health({ ...READY, llm_ok: false }).level, "warn");  // an older server: no "llm"
});

test("keyboard mode needs no decoder; fake and keyboard are always flagged", () => {
  const h = VC.health({ ...READY, decoder_connected: false, display_ok: false, keyboard_mode: true });
  assert.equal(h.level, "ok");
  assert.deepEqual(h.flags, ["KEYBOARD"]);
  assert.deepEqual(VC.health({ ...READY, fake_decoder: true }).flags, ["FAKE DECODER"]);
});

test("the mic card shows the partner's words while they talk", () => {
  assert.deepEqual(VC.micCard("listening", "do you want", false),
    { title: "Listening", detail: "", heard: "do you want", tone: "on" });
  assert.equal(VC.micCard("listening", "", false).detail.startsWith("Speak normally"), true);
  assert.equal(VC.micCard("sent", "Do you want tea.", false).title, "Heard");
});

test("muted wins over any listener state, and problems are warnings", () => {
  assert.deepEqual(VC.micCard("listening", "words", true),
    { title: "Muted", detail: "Nothing is heard or sent. Tap Unmute to listen.", heard: "", tone: "muted" });
  assert.equal(VC.micCard("denied", "", false).tone, "warn");
  assert.equal(VC.micCard("speaking", "", false).tone, "off");
  assert.equal(VC.micCard("unknown-state", "", false).title, "Not listening");
});

test("the selection timer fills to the trial limit and never past it", () => {
  assert.deepEqual(VC.progress(2.6, 10), { fraction: 0.26, label: "2 s" });
  assert.deepEqual(VC.progress(14, 10), { fraction: 1, label: "14 s" });
  assert.deepEqual(VC.progress(-1, 10), { fraction: 0, label: "0 s" });
  assert.equal(VC.progress(3, 0).fraction, 0);
});

// --- c-VEP (code mode): the training card and the confidence bars ------------------------------

test("training shows the square to look at, the cycles, then the accuracy", () => {
  assert.deepEqual(VC.training({ slot: "s1", label: "left", cycle: 12, of: 30 }),
    { title: "Training: look at the left square", detail: "cycle 12 of 30", fraction: 0.4, tone: "look", done: false });
  const done = VC.training({ slot: "s1", label: "left", cycle: 30, of: 30, accuracy: 0.83, chance: 1 / 3 });
  assert.deepEqual(done, { title: "Template ready", detail: "83 % on held-out cycles (chance 33 %)",
    fraction: 1, tone: "ok", done: true });
  const failed = VC.training({ slot: "s1", label: "left", cycle: 4, of: 30, accuracy: null, message: "interrupted" });
  assert.equal(failed.title, "Training failed");
  assert.equal(failed.detail, "interrupted");
  assert.equal(failed.tone, "bad");
  assert.equal(VC.training({ slot: "s1", cycle: 0, of: 30 }).title, "Training: look at the first square");
});

test("the confidence bar clips the correlation to 0..1 and marks the best and committed", () => {
  const bars = VC.confidence({ scores: { s1: 0.42, s2: 1.4, s3: -0.03 }, raw: "s1", committed: "s2" });
  assert.deepEqual(bars.s1, { fraction: 0.42, text: "+0.42", best: true, committed: false });
  assert.deepEqual(bars.s2, { fraction: 1, text: "+1.40", best: false, committed: true });
  assert.deepEqual(bars.s3, { fraction: 0, text: "-0.03", best: false, committed: false });
  assert.deepEqual(VC.confidence({ scores: {} }), {});
  assert.deepEqual(VC.confidence(null), {});
});

test("the banner shows the training detail in place of the baseline", () => {
  assert.deepEqual(VC.banner("BASELINE", "Training: look at the left square, cycle 12/30", "page"),
    { text: "Training: look at the left square, cycle 12/30", tone: "look" });
  assert.equal(VC.banner("BASELINE", "Keep still and look at the screen", "page").text,
    "Baseline: keep still — Keep still and look at the screen");
});

test("the state line between the squares stays empty while training (the card counts)", () => {
  assert.equal(VC.status("BASELINE", "Training: look at the left square, cycle 12/30", "page").text, "");
  assert.equal(VC.status("BASELINE", "Keep still and look at the screen", "page").text,
    "Baseline: keep still — Keep still and look at the screen");
  assert.equal(VC.status("PAUSED", "Training failed: interrupted", "page").text,
    "Paused — Training failed: interrupted");
});
