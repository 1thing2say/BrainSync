// point_overlay.js in code mode (the c-VEP standard, docs/CVEP-STANDARD.md): the code from
// the welcome, white/black patches per frame, cycle reports, the train phase, trained and
// decision confidence, and the view on a fake DOM. No browser.
//     node --test clients/chrome/test/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { makeFakePage } from "./fakes.mjs";

const require = createRequire(import.meta.url);
const K = require("../cvep.js");
const O = require("../point_overlay.js");

const STANDARD = "000001111110101011001101110110100100111000101111001010001100001";
const CODE = { bits: STANDARD.split("").map(Number), lag_frames: 21, cycle_frames: 63,
               train_cycles: 30, fill: "#ffffff" };
const WELCOME = { type: "welcome", protocol: 3, role: "controller", mode: "point", fake: true,
                  waveform: "sine", refresh_hz: 60.0, refresh_tolerance_hz: 1.0, freqs: [],
                  max_slots: 3, detect: true, paradigm: "code", code: CODE };
const RUN = { type: "phase", phase: "run", flicker: true, seconds: null, text: "" };
const TRAIN = { type: "phase", phase: "train", flicker: true, cue: "b1", cycles: 30,
                text: "Look at the Look here patch" };
const rect = (x) => [{ x, y: 0.4, w: 0.12, h: 0.2 }];
const LAYOUT = { type: "layout", layout_id: 7, stage: "pick", targets: [
  { slot: "b1", freq: null, lag: 0, phase: 0, label: "Sign in", kind: "button", rects: rect(0.1) },
  { slot: "b2", freq: null, lag: 21, phase: 0, label: "Search", kind: "button", rects: rect(0.4) },
  { slot: "more", freq: null, lag: 42, phase: 0, label: "More", kind: "more", rects: rect(0.8) }] };
const TRAIN_LAYOUT = { ...LAYOUT, layout_id: 3, stage: "train", targets: LAYOUT.targets.map((t, i) =>
  ({ ...t, label: ["Look here", "Second", "More"][i] })) };
const DECISION = { type: "decision", layout_id: 7, scores: { b1: 0.42, b2: 0.11, more: -0.03 },
                   raw: "b1", committed: "b1", lead: 0.31, changed: true };
const P = 1000 / 60;

function model() {
  const sent = [];
  let clock = 0;
  const m = O.createModel({ send: (msg) => { sent.push(msg); return true; },
                            now: () => clock, wall: (t) => 1_700_000_000 + t / 1000 });
  return { m, sent, tick: (ms) => { clock += ms; } };
}

function live(m, ...msgs) {
  m.accept();
  m.measured(60.0);
  m.onState("open");
  m.onMessage(WELCOME);
  for (const x of msgs) m.onMessage(x);
}

// ------------------------------------------------------------------ pure helpers

test("the extension's cvep.js has the standard 63 bits, lag 21 and 3 targets", () => {
  assert.equal(K.BITS.join(""), STANDARD);
  assert.equal(K.LAG_FRAMES, 21);
  assert.equal(K.CYCLE_FRAMES, 63);
  assert.equal(K.N_TARGETS, 3);
});

test("codeConfig reads a code welcome, falls back to the standard, and is null for freq", () => {
  const c = O.codeConfig(WELCOME);
  assert.deepEqual(c, { bits: CODE.bits, lagFrames: 21, cycleFrames: 63, trainCycles: 30,
                        fill: "#ffffff", standard: true, malformed: false });
  assert.equal(O.codeConfig({ type: "welcome", freqs: [15, 17] }), null);
  assert.equal(O.codeConfig({ type: "welcome", paradigm: "freq" }), null);
  assert.equal(O.codeConfig(null), null);
  const bad = O.codeConfig({ paradigm: "code", code: { bits: [0, 2], lag_frames: -1, fill: "red" } });
  assert.equal(bad.bits.join(""), STANDARD);
  assert.equal(bad.malformed, true);
  assert.equal(bad.lagFrames, 21);
  assert.equal(bad.fill, "#ffffff");
  const bare = O.codeConfig({ paradigm: "code" });
  assert.equal(bare.standard, true);
  assert.equal(bare.malformed, false);
  const other = O.codeConfig({ paradigm: "code", code: { bits: [1, 0, 1], cycle_frames: 3 } });
  assert.equal(other.standard, false);
  assert.equal(other.cycleFrames, 3);
});

test("codeBit is BITS[(n - lag) mod 63], and equals cvep.js bit(k, n) for lag 21 k", () => {
  const c = O.codeConfig(WELCOME);
  for (const n of [0, 1, 5, 20, 21, 62, 63, 64, 200, 1000]) {
    for (const k of [0, 1, 2]) {
      assert.equal(O.codeBit(c, 21 * k, n), K.bit(k, n), `k=${k} n=${n}`);
      assert.equal(O.codeBit(c, 21 * k, n), CODE.bits[(((n - 21 * k) % 63) + 63) % 63]);
    }
  }
});

test("training and trained lines follow the standard's wording", () => {
  assert.equal(O.trainLine(TRAIN, "Look here", { cycle: 12, of: 30 }, 30),
               "Look at the Look here patch   cycle 12/30");
  assert.equal(O.trainLine({ phase: "train" }, "Sign in", null, 30), "Look at the Sign in patch   cycle 0/30");
  assert.equal(O.trainLine({ phase: "train", cycles: 20 }, "X", null, 30), "Look at the X patch   cycle 0/20");
  assert.equal(O.trainedLine({ accuracy: 0.83, chance: 0.333, cycles: 30 }),
               "template ready: 83 % on held-out cycles (chance 33 %)");
  assert.equal(O.trainedLine({}), "template ready");
});

test("confidence lines and the status line come from cvep.js labelLine and statusLine", () => {
  const lines = O.confidenceLines(LAYOUT.targets, DECISION);
  assert.equal(lines.b1, "Sign in ███░░░░░ +0.42 <");
  assert.equal(lines.b2, "Search █░░░░░░░ +0.11");
  assert.equal(lines.more, "More ░░░░░░░░ -0.03");
  assert.equal(O.decisionLine(LAYOUT.targets, DECISION), "lead 0.31   choice: Sign in *");
  assert.equal(O.decisionLine(LAYOUT.targets, { ...DECISION, committed: null, changed: false }),
               "lead 0.31   no choice yet");
  assert.equal(O.decisionLine(LAYOUT.targets, { scores: {}, committed: "b2" }), "lead  -     choice: Search");
  assert.deepEqual(O.confidenceLines(LAYOUT.targets, null), { b1: "Sign in", b2: "Search", more: "More" });
  assert.equal(O.confidenceLines(LAYOUT.targets, { scores: { b1: "x" } }).b1, "Sign in ░░░░░░░░   -  ");
  assert.equal(O.decisionLine(LAYOUT.targets, null), "");
});

// ------------------------------------------------------------------ the model

test("code mode: each patch is white or black by its lag's bit on frames counted from the first flicker", () => {
  const { m } = model();
  live(m, RUN, LAYOUT);
  for (let i = 0; i < 130; i++) {
    const v = m.frame(1000 + i * P);
    assert.equal(v.mode, "flicker");
    assert.equal(v.code, true);
    v.items.forEach((it, k) => {
      const bit = K.bit(k, i);
      assert.equal(it.lum, bit, `frame ${i} target ${k}`);
      assert.equal(it.color, bit ? "#ffffff" : "#000000");
    });
  }
});

test("code mode: still patches are white, and a target without a lag stays white", () => {
  const { m } = model();
  live(m, { type: "phase", phase: "pause", flicker: false, text: "Clicking…" }, LAYOUT);
  const v = m.frame(100);
  assert.equal(v.mode, "still");
  assert.deepEqual(v.items.map((it) => it.color), ["#ffffff", "#ffffff", "#ffffff"]);
  m.onMessage(RUN);
  m.onMessage({ ...LAYOUT, layout_id: 8, targets: [{ ...LAYOUT.targets[0], lag: null }] });
  for (let i = 0; i < 10; i++) assert.equal(m.frame(200 + i * P).items[0].lum, 1);
});

test("code mode: a cycle message goes out on every cycle's first flip with its Unix time", () => {
  const { m, sent } = model();
  live(m, RUN, LAYOUT);
  for (let i = 0; i <= 200; i++) m.frame(1000 + i * P);
  const cycles = sent.filter((x) => x.type === "cycle");
  assert.deepEqual(cycles.map((x) => x.n), [0, 1, 2, 3]);
  assert.ok(cycles.every((x) => x.layout_id === 7));
  cycles.forEach((x, c) => {
    assert.ok(Math.abs(x.t_client - (1_700_000_001 + (c * 63 * P) / 1000)) < 1e-6, `cycle ${c}`);
  });
  // the onset still goes out first, once
  const types = sent.map((x) => x.type);
  assert.equal(types.filter((t) => t === "onset").length, 1);
  assert.ok(types.indexOf("onset") < types.indexOf("cycle"));
  assert.equal(m.state.cycles, 4);
});

test("code mode: a dropped cycle start is reported on the next frame with the time it was due", () => {
  const { m, sent } = model();
  live(m, RUN, LAYOUT);
  for (let i = 0; i <= 62; i++) m.frame(1000 + i * P);
  m.frame(1000 + 64 * P);                       // frame 63 never drew
  const c = sent.filter((x) => x.type === "cycle");
  assert.deepEqual(c.map((x) => x.n), [0, 1]);
  assert.ok(Math.abs(c[1].t_client - (1_700_000_001 + (63 * P) / 1000)) < 1e-6);
});

test("code mode: a new layout restarts frames and cycles at 0; pausing stops the reports", () => {
  const { m, sent } = model();
  live(m, RUN, LAYOUT);
  for (let i = 0; i < 70; i++) m.frame(1000 + i * P);
  m.onMessage({ ...LAYOUT, layout_id: 9 });
  const v = m.frame(3000);
  v.items.forEach((it, k) => assert.equal(it.lum, K.bit(k, 0)));
  const c = sent.filter((x) => x.type === "cycle");
  assert.deepEqual(c.map((x) => [x.layout_id, x.n]), [[7, 0], [7, 1], [9, 0]]);
  m.escape();
  const before = sent.length;
  for (let i = 1; i < 100; i++) assert.equal(m.frame(3000 + i * P).mode, "still");
  assert.equal(sent.filter((x, i) => i >= before && x.type === "cycle").length, 0);
});

test("freq mode sends no cycle messages and draws grey levels", () => {
  const { m, sent } = model();
  m.accept();
  m.measured(60);
  m.onState("open");
  m.onMessage({ ...WELCOME, paradigm: undefined, code: undefined, freqs: [15, 17, 12, 20] });
  m.onMessage(RUN);
  m.onMessage({ ...LAYOUT, targets: LAYOUT.targets.map((t, i) => ({ ...t, freq: [15, 17, 20][i], lag: undefined })) });
  for (let i = 0; i < 70; i++) {
    const v = m.frame(1000 + i * P);
    assert.equal(v.code, false);
    assert.equal(v.items[0].color, null);
  }
  assert.equal(sent.filter((x) => x.type === "cycle").length, 0);
  assert.equal(m.state.code, null);
});

test("train phase: the cue slot gets the cue box and the banner counts the cycles", () => {
  const { m, sent } = model();
  live(m, TRAIN_LAYOUT, TRAIN);
  const v = m.frame(1000);
  assert.equal(v.mode, "flicker", "the train phase flickers");
  assert.deepEqual(v.items.map((it) => it.cue), [true, false, false]);
  assert.equal(m.banner(), "Look at the Look here patch   cycle 0/30");
  m.onMessage({ type: "train", cycle: 12, of: 30 });
  assert.equal(m.banner(), "Look at the Look here patch   cycle 12/30");
  assert.ok(m.hud().some((l) => l.includes("training: keep looking at Look here   cycle 12/30")));
  for (let i = 1; i < 64; i++) m.frame(1000 + i * P);
  assert.deepEqual(sent.filter((x) => x.type === "cycle").map((x) => [x.layout_id, x.n]), [[3, 0], [3, 1]]);
});

test("trained: the accuracy shows in the banner for a while and stays in the HUD", () => {
  const { m, tick } = model();
  live(m, TRAIN_LAYOUT, TRAIN);
  m.onMessage({ type: "trained", accuracy: 0.83, chance: 0.333, cycles: 30 });
  m.onMessage(RUN);
  m.onMessage(LAYOUT);
  m.frame(1000);
  assert.equal(m.banner(), "template ready: 83 % on held-out cycles (chance 33 %)");
  assert.ok(m.hud().some((l) => l.includes("template ready: 83 %")));
  assert.equal(m.frame(1001).items.some((it) => it.cue), false, "no cue box outside training");
  tick(O.TRAINED_SHOW_MS + 1);
  assert.equal(m.banner(), "");
  assert.ok(m.hud().some((l) => l.includes("template ready: 83 %")));
});

test("decision: label lines under the patches and the status line, for the current layout only", () => {
  const { m } = model();
  live(m, RUN, LAYOUT);
  m.onMessage({ ...DECISION, layout_id: 6 });
  assert.equal(m.state.decision, null, "an old layout's decision is ignored");
  m.onMessage(DECISION);
  const v = m.frame(1000);
  assert.deepEqual(v.items.map((it) => it.line),
                   ["Sign in ███░░░░░ +0.42 <", "Search █░░░░░░░ +0.11", "More ░░░░░░░░ -0.03"]);
  assert.equal(v.status, "lead 0.31   choice: Sign in *");
  assert.equal(m.banner(), "lead 0.31   choice: Sign in *");
  assert.ok(m.hud().includes("lead 0.31   choice: Sign in *"));
  m.onMessage({ ...LAYOUT, layout_id: 8 });
  assert.equal(m.state.decision, null, "a new layout clears the confidence");
  assert.deepEqual(m.frame(1100).items.map((it) => it.line), ["Sign in", "Search", "More"]);
  m.onState("closed");
  m.onMessage(DECISION);
  assert.equal(m.state.decision, null);
});

// --------------------------------------------------------------------- the view

function mount() {
  const pg = makeFakePage({ w: 1000, h: 500, dpr: 2 });
  const sent = [];
  const m = O.createModel({ send: (x) => { sent.push(x); return true; }, wall: (t) => t / 1000 });
  const view = O.create({ win: pg.win, doc: pg.doc, model: m, resumed: true });
  return { pg, m, view, sent };
}

test("view: code patches are filled white or black, the cue box is drawn, the label lines are monospace", () => {
  const { pg, m, view, sent } = mount();
  pg.frames(200);                                  // measure the refresh
  m.onState("open");
  m.onMessage(WELCOME);
  m.onMessage(TRAIN_LAYOUT);
  m.onMessage(TRAIN);
  const canvas = view.host.find((n) => n.tagName === "CANVAS");
  canvas.ctx.calls.length = 0;
  pg.frames(70);
  const fills = canvas.ctx.calls.filter((c) => c[0] === "fillRect" && c[2] === 100 && c[3] === 200);
  const colors = new Set(fills.map((c) => c[1]));
  assert.ok(colors.has("#ffffff") && colors.has("#000000"), [...colors].join(","));
  assert.ok(![...colors].some((c) => /^rgb\(/.test(c)), "no grey levels in code mode");
  assert.ok(canvas.ctx.calls.some((c) => c[0] === "strokeRect" && c[1] === O.CUE_COLOR));
  assert.match(canvas.ctx.font, /monospace/);
  assert.ok(sent.filter((x) => x.type === "cycle").length >= 1);
  m.onMessage(RUN);
  m.onMessage({ ...LAYOUT, layout_id: 3 });
  m.onMessage({ ...DECISION, layout_id: 3 });
  canvas.ctx.calls.length = 0;
  pg.frames(2);
  assert.ok(canvas.ctx.calls.some((c) => c[0] === "fillText" && c[1] === "Sign in ███░░░░░ +0.42 <"));
  assert.ok(!canvas.ctx.calls.some((c) => c[0] === "strokeRect" && c[1] === O.CUE_COLOR));
  view.update();
  const banner = view.host.find((n) => n.className === "banner");
  assert.equal(banner.textContent, "lead 0.31   choice: Sign in *");
  view.destroy();
});
