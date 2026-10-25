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

// The phone's Look to Speak screen (INTERFACE-v2-draft.md section 9): halves, confirm, calibrate.
const OPTIONS = [
  { label: "Tea", utterance: "Could I have some tea, please?" },
  { label: "Outside", utterance: "Let's go outside." },
  { label: "Rest", utterance: "I'd like to rest now." },
  { label: "Thanks", utterance: "Thank you." },
  { label: "More options", utterance: "" },
];
const SIDE_SLOTS = [{ slot: "s1", role: "left", label: "", utterance: "" },
                    { slot: "s2", role: "right", label: "", utterance: "" }];
const HALVES = { mode: "halves", options: OPTIONS, split: 3, slots: SIDE_SLOTS };

test("halves, calibrate and their confirm screen use the two side cards", () => {
  assert.equal(VC.layout(HALVES), "sides");
  assert.equal(VC.layout({ mode: "calibrate", cue: "s1", slots: SIDE_SLOTS }), "sides");
  assert.equal(VC.layout({ mode: "confirm", slots: [{ slot: "s1", role: "say" }, { slot: "s2", role: "back" }] }), "sides");
  // Page mode and its confirm screen (Back on s4) keep the diamond; list mode the row.
  assert.equal(VC.layout({ mode: "confirm", slots: [{ slot: "s1", role: "say" }, { slot: "s4", role: "back" }] }), "diamond");
  assert.equal(VC.layout({ mode: "page", slots: [{ slot: "s1" }, { slot: "s2" }, { slot: "s3" }, { slot: "s4" }] }), "diamond");
  assert.equal(VC.layout({ mode: "list", slots: SIDE_SLOTS }), "row");
  assert.equal(VC.layout(null), "diamond");
  assert.equal(VC.position("sides", "s1"), "left");
  assert.equal(VC.position("sides", "s2"), "right");
  assert.equal(VC.position("diamond", "s2"), "left");
  assert.equal(VC.position("diamond", "s4"), "bottom");
});

test("the first split phrases go on the left card, the rest on the right", () => {
  const h = VC.halves(HALVES);
  assert.deepEqual(h.s1.map((i) => i.text), ["Could I have some tea, please?", "Let's go outside.", "I'd like to rest now."]);
  assert.deepEqual(h.s2, [{ text: "Thank you.", more: false }, { text: "More options", more: true }]);
  // No split: ceil(n / 2) on the left.
  assert.equal(VC.halves({ ...HALVES, split: null }).s1.length, 3);
  // The roles say which slot is which side.
  const swapped = VC.halves({ ...HALVES, slots: [{ slot: "s2", role: "left" }, { slot: "s1", role: "right" }] });
  assert.equal(swapped.s2.length, 3);
  assert.equal(swapped.s1.length, 2);
});

test("More options has no utterance and shows muted, with its label or a default", () => {
  const h = VC.halves({ mode: "halves", split: 1, slots: SIDE_SLOTS,
    options: [{ label: "Yes", utterance: "Yes, please." }, { label: "", utterance: "  " }] });
  assert.deepEqual(h.s2, [{ text: "More options", more: true }]);
});

test("each card shows its phrases, the confirm words, or the calibration cue", () => {
  assert.equal(VC.card(HALVES, SIDE_SLOTS[0]).items.length, 3);
  assert.equal(VC.card(HALVES, SIDE_SLOTS[0]).text, "");
  const say = VC.card({ mode: "confirm" }, { slot: "s1", role: "say", label: "Say it", utterance: "" });
  assert.deepEqual(say, { tag: "", text: "Say it", items: [], cue: false });
  const cal = { mode: "calibrate", cue: "s1", text: "Look at the left card", slots: SIDE_SLOTS };
  assert.deepEqual(VC.card(cal, SIDE_SLOTS[0]), { tag: "", text: "Look here", items: [], cue: true });
  assert.deepEqual(VC.card(cal, SIDE_SLOTS[1]), { tag: "", text: "", items: [], cue: false });
  // Page mode: the sentence big, the label as its tag.
  assert.deepEqual(VC.card({ mode: "page" }, { slot: "s1", role: "option", label: "Tea", utterance: "Tea, please." }),
    { tag: "Tea", text: "Tea, please.", items: [], cue: false });
});

test("the calibration line shows the score, and warns below 70 % or on a failure", () => {
  assert.deepEqual(VC.calibration({ trained: true, calibration_accuracy: 0.85 }), { text: "Calibrated 85 %", tone: "ok" });
  assert.deepEqual(VC.calibration({ trained: true, calibration_accuracy: 0.62 }),
    { text: "Calibrated 62 % · low: picks may be wrong", tone: "warn" });
  assert.deepEqual(VC.calibration({ trained: false, calibration_accuracy: 0.4 }), { text: "Calibration failed · 40 %", tone: "warn" });
  assert.equal(VC.calibration({ trained: true, calibration_accuracy: null }).text, "Template loaded");
  // SSVEP, or before the first calibration: nothing.
  assert.equal(VC.calibration({ trained: false, calibration_accuracy: null }).text, "");
  assert.equal(VC.calibration({}).text, "");
});

test("halves mode and calibration have their own words, quiet while the user looks", () => {
  assert.equal(VC.banner("STIMULATING", "", "halves").text, "Look at the side with your phrase");
  assert.equal(VC.status("STIMULATING", "", "halves").text, "");
  assert.equal(VC.status("CALIBRATING", "", "calibrate").text, "");
});
