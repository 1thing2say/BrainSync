// ui/view_core.js: the banner, health line, mic card and selection timer the partner page shows.
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
  assert.equal(h.text, "Ready · Fixed replies only (no LLM) · Mic muted");
  assert.equal(VC.health({ ...READY, llm: "failed", llm_ok: false }).text,
    "Ready · Last reply page failed: fixed list shown");
  // The server names each backend that failed.
  assert.equal(VC.health({ ...READY, llm: "failed", llm_ok: false,
                           llm_error: "local: timeout; claude: APIConnectionError" }).text,
    "Ready · Last reply page failed (local: timeout; claude: APIConnectionError): fixed list shown");
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
