// The overlay's c-VEP mode (docs/CVEP-STANDARD.md): config, per-frame code, cycle reports,
// training and confidence lines, pause / play. No DOM, no browser.
//     node --test extension/test/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const C = require("../cvep.js");
const O = require("../overlay.js");
const S = require("../scroller.js");

const P = 1000 / 60;
const OFFSET_MS = 1792809000000;   // wall clock minus performance time in these tests

const CODE = {
  type: "config", name: "reels_cvep", paradigm: "code", waveform: "sine", depth: 1.0,
  refresh_hz: 60.0, refresh_tolerance_hz: 1.0,
  code: { bits: Array.from(C.BITS), lag_frames: 21, cycle_frames: 63, train_cycles: 30,
          fill: "#ffffff" },
  targets: [
    { label: "up", freq: null, phase: 0.0, x: 0.58, y: 0.3, size: 0.2, lag: 0 },
    { label: "pause", freq: null, phase: 0.0, x: 0.58, y: 0.0, size: 0.2, lag: 21 },
    { label: "down", freq: null, phase: 0.0, x: 0.58, y: -0.3, size: 0.2, lag: 42 },
  ],
};

function controller(extra = {}) {
  const sent = [];
  const transport = { send: (obj) => sent.push(obj), onMessage() {}, onState() {} };
  const warnings = [];
  let clock = 0;
  const clockOffset = new O.ClockOffset(() => OFFSET_MS, () => 0);
  const c = O.createController({
    transport, agent: "extension", url: () => "https://www.instagram.com/reels/x/",
    now: () => clock, warn: (...a) => warnings.push(a.join(" ")), refreshFrames: 10,
    warmupFrames: 2, clockOffset, ...extra,
  });
  return { c, sent, warnings, tick: (ms) => { clock += ms; } };
}

// Feed rAF timestamps at 60 Hz until the meter has a reading; returns the next ts.
function measure(c, frames = 13) {
  for (let i = 0; i < frames; i++) c.frame(i * P);
  return frames * P;
}

function trainingController(extra) {
  const ctl = controller(extra);
  ctl.c.handle(CODE);
  ctl.c.accept();
  ctl.ts = measure(ctl.c);
  ctl.c.handle({ type: "phase", phase: "train", flicker: true, seconds: null,
                 text: "Training: look at the UP square" });
  return ctl;
}

// ------------------------------------------------------------------ the shared renderer

test("extension/cvep.js is a byte-for-byte copy of clients/js/cvep.js", () => {
  const ours = readFileSync(new URL("../cvep.js", import.meta.url));
  const shared = readFileSync(new URL("../../clients/js/cvep.js", import.meta.url));
  assert.ok(ours.equals(shared), "copy clients/js/cvep.js to extension/cvep.js");
});

// ------------------------------------------------------------------ config

test("a code config parses with its bits, lags and fill", () => {
  const cfg = O.parseConfig(CODE);
  assert.equal(cfg.paradigm, "code");
  assert.deepEqual(Array.from(cfg.code.bits), Array.from(C.BITS));
  assert.deepEqual(cfg.targets.map((t) => [t.label, t.lag, t.freq]),
                   [["up", 0, null], ["pause", 21, null], ["down", 42, null]]);
  assert.equal(cfg.code.fill, "#ffffff");
  assert.deepEqual(JSON.parse(JSON.stringify(cfg)), JSON.parse(JSON.stringify(O.CODE_CONFIG)));
  // lags default to k * lag_frames
  const noLags = { ...CODE, targets: CODE.targets.map(({ lag, ...t }) => t) };
  assert.deepEqual(O.parseConfig(noLags).targets.map((t) => t.lag), [0, 21, 42]);
  assert.equal(O.parseConfig({ ...CODE, paradigm: undefined, targets: [
    { label: "a", freq: 15, x: 0, y: 0, size: 0.2 }] }).paradigm, "freq");
});

test("bad code configs are refused, naming the field", () => {
  const bad = [
    [{ paradigm: "time" }, /paradigm/],
    [{ code: null }, /code must be an object/],
    [{ code: { ...CODE.code, bits: [0, 2, 1] } }, /bits/],
    [{ code: { ...CODE.code, lag_frames: 0 } }, /lag_frames/],
    [{ code: { ...CODE.code, cycle_frames: 126 } }, /cycle_frames/],
    [{ code: { ...CODE.code, train_cycles: 1 } }, /train_cycles/],
    [{ code: { ...CODE.code, fill: "white" } }, /fill/],
    [{ targets: [] }, /targets/],
    [{ targets: [{ ...CODE.targets[0], lag: -1 }] }, /lag/],
    [{ targets: [{ ...CODE.targets[0], size: 0 }] }, /size/],
  ];
  for (const [change, match] of bad) {
    assert.throws(() => O.parseConfig({ ...CODE, ...change }), match, JSON.stringify(change));
  }
  const { c, warnings } = controller();
  c.handle({ ...CODE, code: { ...CODE.code, bits: "01" } });
  assert.equal(c.state.config, O.DEFAULT_CONFIG);
  assert.match(warnings[0], /config: code bits/);
});

// ------------------------------------------------------------------ frames and cycles

test("code luminance is the standard's bit: white 1, black 0, lagged per target", () => {
  for (let n = -70; n < 200; n++) {
    for (let k = 0; k < 3; k++) {
      assert.equal(O.codeLum(C.BITS, 21 * k, n), C.bit(k, n), `target ${k} frame ${n}`);
    }
  }
});

test("training flickers the code from frame 0 and reports each cycle's first flip", () => {
  const { c, sent, ts } = trainingController();
  const t0 = ts;
  for (let n = 0; n < 63 * 3 + 5; n++) {
    assert.equal(c.frame(t0 + n * P), "flicker");
    assert.deepEqual(Array.from(c.lum()), C.frameBits(n));
  }
  const cycles = sent.filter((m) => m.type === "cycle");
  assert.deepEqual(cycles.map((m) => m.n), [0, 1, 2, 3]);
  cycles.forEach((m, i) => {
    assert.ok(Math.abs(m.t - (OFFSET_MS + t0 + 63 * i * P) / 1000) < 1e-6, JSON.stringify(m));
  });
  assert.equal(c.state.cycles, 4);
});

test("a cycle whose first frames were dropped is reported at its place on the grid", () => {
  const { c, sent, ts } = trainingController();
  const t0 = ts;
  for (const n of [0, 1, 60, 61, 64, 65, 100]) c.frame(t0 + n * P);     // 62 and 63 dropped
  for (const n of [140, 200]) c.frame(t0 + n * P);                     // 126..139 not shown
  const cycles = sent.filter((m) => m.type === "cycle");
  assert.deepEqual(cycles.map((m) => m.n), [0, 1]);                    // not 2: entered late
  assert.ok(Math.abs(cycles[1].t - (OFFSET_MS + t0 + 63 * P) / 1000) < 1e-6);
});

test("cyclesDue: the start frame, at most MAX_LATE_FRAMES late, once per cycle", () => {
  assert.deepEqual(O.cyclesDue(null, 0, 63), [{ n: 0, frame: 0 }]);
  assert.deepEqual(O.cyclesDue(0, 1, 63), []);
  assert.deepEqual(O.cyclesDue(62, 63, 63), [{ n: 1, frame: 63 }]);
  assert.deepEqual(O.cyclesDue(61, 63 + O.MAX_LATE_FRAMES, 63), [{ n: 1, frame: 63 }]);
  assert.deepEqual(O.cyclesDue(61, 63 + O.MAX_LATE_FRAMES + 1, 63), []);
  assert.deepEqual(O.cyclesDue(10, 500, 63), []);                       // a hidden tab
  assert.deepEqual(O.cyclesDue(10, 504, 63), [{ n: 8, frame: 504 }]);  // back on a start
});

test("train(flicker) to run(flicker) keeps the onset, so the cycle count runs on", () => {
  const { c, sent, ts } = trainingController();
  for (let n = 0; n < 70; n++) c.frame(ts + n * P);
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: null, text: "Look" });
  for (let n = 70; n < 130; n++) c.frame(ts + n * P);
  assert.deepEqual(sent.filter((m) => m.type === "cycle").map((m) => m.n), [0, 1, 2]);
  c.handle({ type: "phase", phase: "wait", flicker: false, seconds: null, text: "" });
  assert.equal(c.frame(ts + 130 * P), "still");
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: null, text: "" });
  c.frame(ts + 131 * P);                                                // a new onset: n = 0
  assert.deepEqual(sent.filter((m) => m.type === "cycle").map((m) => m.n), [0, 1, 2, 0]);
});

test("no cycles while still, hidden, refresh off or without a server's flicker", () => {
  const { c, sent } = controller();
  c.handle(CODE);
  c.accept();
  const ts = measure(c);
  c.handle({ type: "phase", phase: "wait", flicker: false, seconds: null, text: "" });
  for (let n = 0; n < 130; n++) assert.equal(c.frame(ts + n * P), "still");
  assert.equal(O.stillLum(c.state.config), 1);                            // still squares: white
  assert.equal(sent.filter((m) => m.type === "cycle").length, 0);
  // a 120 Hz display: refresh off, squares held still, nothing reported
  const { c: c2, sent: sent2 } = controller();
  c2.handle(CODE);
  c2.accept();
  for (let i = 0; i < 13; i++) c2.frame(i * 1000 / 120);
  c2.handle({ type: "phase", phase: "train", flicker: true, seconds: null, text: "" });
  for (let n = 0; n < 130; n++) assert.equal(c2.frame(200 + n * 1000 / 120), "still");
  assert.ok(c2.refreshOff());
  assert.equal(sent2.filter((m) => m.type === "cycle").length, 0);
});

test("the clock offset is the max of Date.now() - performance.now() over recent samples", () => {
  let wall = 1000, perf = 0.4;
  const off = new O.ClockOffset(() => wall, () => perf, 4);
  off.sample();                         // 999.6: Date.now() truncated 1000.4 down
  wall = 1001; perf = 0.9; off.sample();   // 1000.1, the best
  wall = 1002; perf = 2.0; off.sample();   // 1000.0
  assert.ok(Math.abs(off.unix(5000) - (1000.1 + 5000) / 1000) < 1e-9);
  wall = 3002; perf = 2.0;              // the wall clock stepped 2 s ahead
  for (let i = 0; i < 4; i++) off.sample();
  assert.ok(Math.abs(off.unix(0) - 3.0) < 1e-9);
  const lazy = new O.ClockOffset(() => 50, () => 10);
  assert.equal(lazy.unix(0), 0.04);     // samples once when asked first
});

// ------------------------------------------------------------------ training and confidence

test("training, trained and decision messages drive the label and status lines", () => {
  const { c } = trainingController();
  let lines = c.lines();
  assert.deepEqual(lines.labels, ["UP", "PAUSE", "DOWN"]);
  assert.equal(lines.status, "training: keep looking at UP   cycle 0/30");
  c.handle({ type: "train", cycle: 12, of: 30 });
  assert.equal(c.lines().status, "training: keep looking at UP   cycle 12/30");
  c.handle({ type: "trained", accuracy: 0.8333, chance: 0.3333, cycles: 30 });
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: null, text: "Look" });
  assert.equal(c.lines().status, "template ready: 83 % on held-out cycles (chance 33 %)");
  c.handle({ type: "decision", scores: { up: 0.42, pause: 0.11, down: -0.03 }, raw: "up",
             committed: null, lead: 0.31, changed: false });
  lines = c.lines();
  assert.deepEqual(lines.labels, ["UP    ███░░░░░ +0.42 <", "PAUSE █░░░░░░░ +0.11",
                                  "DOWN  ░░░░░░░░ -0.03"]);
  assert.equal(lines.status, "lead 0.31   no choice yet");
  c.handle({ type: "decision", scores: { up: 0.5, pause: 0.1, down: 0.0 }, raw: "up",
             committed: "up", lead: 0.4, changed: true });
  assert.equal(c.lines().status, "lead 0.40   choice: UP *");
  c.handle({ type: "decision", scores: { up: 0.5, pause: 0.1, down: 0.0 }, raw: null,
             committed: "up", lead: null, changed: false });
  assert.equal(c.lines().status, "lead  -     choice: UP");
  // labels and lines match the Python helpers' format (docs/CVEP-STANDARD.md section 3)
  assert.equal(C.labelLine("YES", 0.42, true, 4), "YES  ███░░░░░ +0.42 <");
});

test("a loaded template says so; a new training phase clears the old numbers", () => {
  const { c } = trainingController();
  c.handle({ type: "trained", accuracy: null, chance: 0.3333, cycles: 30 });
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: null, text: "" });
  assert.equal(c.lines().status, "template loaded");
  c.handle({ type: "decision", scores: { up: 0.5, pause: 0.1, down: 0.0 }, raw: "up",
             committed: "up", lead: 0.4, changed: true });
  c.handle({ type: "phase", phase: "train", flicker: true, seconds: null, text: "" });
  assert.equal(c.state.decision, null);
  assert.equal(c.state.trained, null);
  assert.deepEqual(c.lines().labels, ["UP", "PAUSE", "DOWN"]);
});

test("malformed train, trained and decision messages are ignored", () => {
  const { c, warnings } = trainingController();
  for (const m of [
    { type: "train", cycle: 31, of: 30 }, { type: "train", cycle: 1.5, of: 30 },
    { type: "trained", accuracy: 1.2, chance: 0.33 }, { type: "trained", accuracy: 0.5 },
    { type: "decision", scores: [1, 2, 3] },
    { type: "decision", scores: { up: "high" } },
    { type: "decision", scores: { up: 0.1 }, raw: "left" },
    { type: "decision", scores: { up: 0.1 }, lead: "big" },
  ]) c.handle(m);
  assert.equal(c.state.ignored, 8);
  assert.equal(warnings.length, 8);
  assert.equal(c.state.train, null);
  assert.equal(c.state.trained, null);
  assert.equal(c.state.decision, null);
  // a score left out shows as "-"
  c.handle({ type: "decision", scores: { up: 0.2 }, raw: "up", committed: null, lead: null });
  assert.equal(c.lines().labels[1], "PAUSE ░░░░░░░░   -  ");
});

test("a new config clears training and decisions", () => {
  const { c } = trainingController();
  c.handle({ type: "train", cycle: 3, of: 30 });
  c.handle(CODE);
  assert.equal(c.state.train, null);
});

// ------------------------------------------------------------------ pause / play

test("toggle pauses or plays through the toggler and acks with the new state", async () => {
  let playing = true;
  const toggler = async () => { playing = !playing; return { ok: true, playing, method: "video" }; };
  const { c, sent } = controller({ toggler });
  c.handle(CODE);
  c.accept();
  c.handle({ type: "toggle", id: 3 });
  c.handle({ type: "scroll", dir: "down", id: 4 });
  c.handle({ type: "toggle", id: 5 });
  await c.idle();
  assert.deepEqual(sent.filter((m) => m.type === "toggled" || m.type === "scrolled"), [
    { type: "toggled", id: 3, ok: true, playing: false, method: "video" },
    { type: "scrolled", id: 4, ok: false, method: "none" },
    { type: "toggled", id: 5, ok: true, playing: true, method: "video" },
  ]);
  assert.equal(c.state.lastToggle.id, 5);
});

test("a hidden, paused or unaccepted tab refuses to toggle; bad ids are ignored", async () => {
  let calls = 0;
  const toggler = async () => { calls++; return { ok: true, playing: false, method: "video" }; };
  const hidden = controller({ toggler, visible: () => false });
  hidden.c.accept();
  hidden.c.handle({ type: "toggle", id: 1 });
  const fresh = controller({ toggler });
  fresh.c.handle({ type: "toggle", id: 1 });                  // Start not pressed
  fresh.c.handle({ type: "toggle", id: "1" });
  const broken = controller({ toggler: async () => { throw new Error("no video"); } });
  broken.c.accept();
  broken.c.handle({ type: "toggle", id: 2 });
  await Promise.all([hidden.c.idle(), fresh.c.idle(), broken.c.idle()]);
  assert.equal(calls, 0);
  for (const { sent } of [hidden, fresh]) {
    assert.deepEqual(sent.filter((m) => m.type === "toggled"),
                     [{ type: "toggled", id: 1, ok: false, playing: null, method: "none" }]);
  }
  assert.deepEqual(broken.sent.filter((m) => m.type === "toggled"),
                   [{ type: "toggled", id: 2, ok: false, playing: null, method: "none" }]);
  assert.equal(fresh.c.state.ignored, 1);
});

function videoDoc(videos) {
  return {
    querySelectorAll: (q) => (q === "video" ? videos : []),
    defaultView: { innerWidth: 1600, innerHeight: 1000 },
  };
}

function video(paused, refuse = false) {
  const v = {
    paused,
    getBoundingClientRect: () => ({ left: 500, right: 1100, top: 0, bottom: 1000 }),
    pause() { v.paused = true; },
    play() {
      if (refuse) return Promise.reject(new Error("NotSupportedError"));
      v.paused = false;
      return Promise.resolve();
    },
  };
  return v;
}

test("togglePlay pauses the most visible video, or plays it when paused", async () => {
  const v = video(false);
  const doc = videoDoc([v]);
  assert.deepEqual(await S.togglePlay({ doc }), { ok: true, playing: false, method: "video" });
  assert.equal(v.paused, true);
  assert.deepEqual(await S.togglePlay({ doc }), { ok: true, playing: true, method: "video" });
  assert.deepEqual(await S.togglePlay({ doc: videoDoc([]) }),
                   { ok: false, playing: null, method: "none" });
  assert.deepEqual(await S.togglePlay({ doc: videoDoc([video(true, true)]) }),
                   { ok: false, playing: false, method: "video" });
});

// ------------------------------------------------------------------ layout and warning

test("the c-VEP squares sit right of the video, labels between them, all on screen", () => {
  const cfg = O.CODE_CONFIG;
  for (const [vw, vh] of [[1600, 1000], [1512, 982], [1500, 1000]]) {
    assert.equal(O.layoutOverflow(cfg, vw, vh), null, `${vw}x${vh}`);
    const video = { left: vw / 2 - 0.28 * vh, right: vw / 2 + 0.28 * vh };
    const rects = cfg.targets.map((t) => O.squareRect(t, vw, vh));
    for (let k = 0; k < rects.length; k++) {
      const r = rects[k];
      assert.ok(r.left > video.right, "clear of the video's 9:16 column");
      const label = O.labelPos(cfg.targets[k], vw, vh);
      const cue = O.cueRect(cfg.targets[k], vw, vh);
      assert.ok(label.top >= cue.top + cue.side + cue.line / 2 - 1e-9, "below the cue box");
      const bottom = label.top + 1.4 * label.font;
      const next = k + 1 < rects.length ? O.cueRect(cfg.targets[k + 1], vw, vh).top : vh;
      assert.ok(bottom <= next, `label ${k} ends at ${bottom}, the next box starts at ${next}`);
    }
  }
});

test("the warning names what will flicker, and says so before any config", () => {
  assert.match(O.warningHtml(O.CODE_CONFIG, true), /3 white squares in a coded/);
  assert.match(O.warningHtml(O.DEFAULT_CONFIG, true), /15 Hz and 17 Hz/);
  const early = O.warningHtml(O.DEFAULT_CONFIG, false);
  assert.match(early, /coded black-and-white patterns/);
  assert.match(early, /15 and 17 Hz/);
  for (const html of [O.warningHtml(O.CODE_CONFIG, true), early]) {
    assert.match(html, /epilepsy/);
    assert.match(html, /Escape/);
  }
});
