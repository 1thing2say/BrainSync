// ui/flicker_core.js: the session page's flicker, the same luminance as the PsychoPy display.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const FC = require("../../ui/flicker_core.js");

const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test("sine starts at mid-grey and matches brainsync/stimulus/flicker.py frame by frame", () => {
  close(FC.luminance(13, 0, 0, 60, "sine"), 0.5);
  for (const n of [1, 7, 59, 600]) {
    close(FC.luminance(17, 0, n, 60, "sine"), 0.5 * (1 + Math.sin((2 * Math.PI * 17 * n) / 60)));
  }
  close(FC.luminance(15, 90, 0, 60, "sine"), 1);  // phase_deg 90: starts at white
});

test("each target repeats at its own frequency", () => {
  // 15 Hz at 60 Hz: 4 frames a cycle; 13 Hz: back in phase after 60 frames (13 whole cycles).
  for (const n of [0, 3, 10]) close(FC.luminance(15, 0, n, 60, "sine"), FC.luminance(15, 0, n + 4, 60, "sine"));
  close(FC.luminance(13, 0, 5, 60, "sine"), FC.luminance(13, 0, 65, 60, "sine"));
  assert.notEqual(FC.luminance(13, 0, 5, 60, "sine").toFixed(6), FC.luminance(13, 0, 6, 60, "sine").toFixed(6));
});

test("square is on for the first half of each cycle", () => {
  // 15 Hz at 60 Hz: on, on, off, off.
  assert.deepEqual([0, 1, 2, 3, 4].map((n) => FC.luminance(15, 0, n, 60, "square")), [1, 1, 0, 0, 1]);
});

test("frame index comes from the timestamp, so a dropped frame keeps the phase", () => {
  const ms = 1000 / 60;
  assert.equal(FC.frameIndex(0, ms), 0);
  assert.equal(FC.frameIndex(3 * ms + 2, ms), 3);  // timestamps jitter by a millisecond or two
  assert.equal(FC.frameIndex(5 * ms, ms), 5);      // frame 4 never drawn: frame 5 is still frame 5
  assert.equal(FC.frameIndex(-1, ms), 0);
});

const stamps = (gapMs, count, every = 0, extra = 0) => {
  const out = [0];
  for (let i = 1; i < count; i++) out.push(out[i - 1] + gapMs + (every && i % every === 0 ? extra : 0));
  return out;
};

test("the refresh is measured from frame timestamps", () => {
  assert.equal(FC.measure(stamps(16.667, 10)), null);  // too few frames to say
  const m = FC.measure(stamps(1000 / 60, 90));
  close(m.hz, 60, 1e-6);
  assert.equal(m.uneven, 0);
  assert.equal(FC.refreshProblem(m, 60), "");
});

test("a 120 Hz or uneven screen is reported", () => {
  assert.match(FC.refreshProblem(FC.measure(stamps(1000 / 120, 90)), 60), /120 Hz: set it to 60 Hz/);
  const uneven = FC.measure(stamps(1000 / 60, 90, 4, 12));  // every 4th frame 12 ms late
  assert.ok(uneven.uneven > 0.1);
  assert.match(FC.refreshProblem(uneven, 60), /uneven/);
  assert.equal(FC.refreshProblem(null, 60), "");
});
