// ui/code_core.js: the session page's c-VEP squares, cycle reports and clock (display.flicker:
// browser, stim.paradigm: code).
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const K = require("../../ui/cvep.js");
const CC = require("../../ui/code_core.js");

test("each slot shows the m-sequence delayed by its lag, as the standard's target k", () => {
  for (const [k, lag] of [[0, 0], [1, 21], [2, 42]]) {
    for (const n of [0, 1, 20, 21, 62, 63, 64, 125, 630]) {
      assert.equal(CC.squareBit(lag, n), K.bit(k, n), `lag ${lag}, frame ${n}`);
    }
  }
  // Frame 0 of lag 0 is the sequence's first bit; one cycle later it repeats.
  assert.equal(CC.squareBit(0, 0), K.BITS[0]);
  assert.equal(CC.squareBit(0, 5), K.BITS[5]);
  assert.equal(CC.squareBit(21, 0), K.BITS[63 - 21]);
  assert.equal(CC.squareColor(0, 5), K.BITS[5] ? "#ffffff" : "#000000");
});

test("a cycle is reported on its first frame, or up to two frames late, never twice", () => {
  assert.deepEqual(CC.cyclesDue(null, 0, 63), [{ n: 0, frame: 0 }]);  // the onset frame
  assert.deepEqual(CC.cyclesDue(0, 1, 63), []);
  assert.deepEqual(CC.cyclesDue(62, 63, 63), [{ n: 1, frame: 63 }]);
  // Frames 63 and 64 dropped: frame 65 still reports cycle 1, at frame 63's place.
  assert.deepEqual(CC.cyclesDue(62, 65, 63), [{ n: 1, frame: 63 }]);
  // A longer stall: cycle 1's start was never shown, so it is not reported.
  assert.deepEqual(CC.cyclesDue(62, 66, 63), []);
  assert.deepEqual(CC.cyclesDue(63, 64, 63), []);
  // Over a long run: one report per cycle, numbered in order.
  const got = [];
  let prev = null;
  for (let n = 0; n < 63 * 5; n++) {
    got.push(...CC.cyclesDue(prev, n, 63).map((c) => c.n));
    prev = n;
  }
  assert.deepEqual(got, [0, 1, 2, 3, 4]);
});

test("the clock maps rAF timestamps to Unix seconds with the largest wall - perf sample", () => {
  let wall = 1792809200000;
  let perf = 1000;
  const clock = new CC.ClockOffset(() => wall, () => perf, 3);
  clock.sample();                     // offset 1792809199000
  wall += 10; perf += 11;             // Date.now() truncated a ms low: offset 1792809198999
  clock.sample();
  assert.equal(clock.unix(2000), (1792809199000 + 2000) / 1000);
  assert.equal(clock.now(), (1792809199000 + perf) / 1000);
  // Only the last ``size`` samples count: offsets ...997, ...995, ...993 (the clock drifts).
  for (let i = 0; i < 3; i++) { wall += 10; perf += 12; clock.sample(); }
  assert.equal(clock.unix(0), 1792809198997 / 1000);
});

test("the page refuses a code it cannot draw", () => {
  const lags = { s1: 0, s2: 21, s3: 42 };
  assert.equal(CC.codeProblem({ length: 63, cycle_frames: 63, train_cycles: 30 }, lags), "");
  assert.match(CC.codeProblem({ length: 31, cycle_frames: 31 }, lags), /31 bits/);
  assert.match(CC.codeProblem(null, lags), /no code settings/);
  assert.match(CC.codeProblem({ length: 63, cycle_frames: 63 }, { s1: null }), /no code lags/);
  assert.match(CC.codeProblem({ length: 63, cycle_frames: 63 }, {}), /no code lags/);
});

test("the row keeps every square in its place: a page, the confirm screen, training", () => {
  const order = ["s1", "s2", "s3"];
  const page = { slots: [{ slot: "s1", role: "option", label: "Yes", utterance: "Yes, please." },
                         { slot: "s2", role: "option", label: "No", utterance: "No, thanks." },
                         { slot: "s3", role: "next", label: "More options", utterance: "" }] };
  assert.deepEqual(CC.row(order, page).map((s) => [s.slot, s.role, s.utterance]),
    [["s1", "option", "Yes, please."], ["s2", "option", "No, thanks."], ["s3", "next", ""]]);
  const confirm = { slots: [{ slot: "s1", role: "say", label: "Say it" },
                            { slot: "s3", role: "back", label: "Back" }] };
  assert.deepEqual(CC.row(order, confirm).map((s) => [s.slot, s.role, s.label]),
    [["s1", "say", "Say it"], ["s2", "still", ""], ["s3", "back", "Back"]]);
  assert.deepEqual(CC.row(order, null, "s1").map((s) => [s.slot, s.role, s.cue]),
    [["s1", "train", true], ["s2", "train", false], ["s3", "train", false]]);
});

test("the bar line and the status line follow the standard's text", () => {
  assert.equal(CC.barLine(0.42, true), "███░░░░░ +0.42 <");
  assert.equal(CC.barLine(-0.03, false), "░░░░░░░░ -0.03");
  assert.equal(CC.barLine(1.7, false), "████████ +1.70");
  assert.equal(CC.barLine(NaN, false), "░░░░░░░░   -  ");
  const texts = { s1: "Yes, please.", s3: "More options" };
  assert.equal(CC.statusText({ lead: 0.31, committed: null }, texts), "lead 0.31   no choice yet");
  assert.equal(CC.statusText({ lead: 0.37, committed: "s1" }, texts), "lead 0.37   choice: Yes, please.");
  assert.equal(CC.statusText({ lead: null, committed: "s2" }, texts), "lead  -     choice: s2");
  assert.equal(CC.statusText(null, texts), "");
});
