// cvep.js: the c-VEP standard's frame rule, cycle starts and confidence text.
//     node --test clients/js/test/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const C = require("../cvep.js");

test("63 bits with the m-sequence balance (32 ones) and the standard's constants", () => {
  assert.equal(C.BITS.length, 63);
  assert.equal(C.BITS.filter((b) => b === 1).length, 32);
  assert.deepEqual([C.N_TARGETS, C.MAX_TARGETS, C.LAG_FRAMES, C.CYCLE_FRAMES, C.TRAIN_CYCLES],
                   [2, 2, 31, 63, 30]);
  assert.ok(Object.isFrozen(C.BITS));
});

test("target k shows the sequence delayed by 31 k frames", () => {
  for (let n = 0; n < 200; n++) {
    assert.equal(C.bit(0, n), C.BITS[n % 63]);
    assert.equal(C.bit(1, n + 31), C.bit(0, n));
    assert.deepEqual(C.frameBits(n), [C.bit(0, n), C.bit(1, n)]);
    assert.equal(C.color(0, n), C.bit(0, n) ? "#ffffff" : "#000000");
  }
  assert.equal(C.bit(0, -1), C.BITS[62]);
  assert.throws(() => C.bit(-1, 0), RangeError);
  assert.throws(() => C.bit(0, 1.5), RangeError);
});

test("cycle starts every 63 frames", () => {
  assert.deepEqual([0, 62, 63, 126, 127].map((n) => C.cycleStart(n)), [true, false, true, true, false]);
  assert.deepEqual([0, 62, 63, 130].map((n) => C.cycleIndex(n)), [0, 0, 1, 2]);
});

test("bars, label lines and the status line", () => {
  assert.equal(C.bar(0.42), "███░░░░░");
  assert.equal(C.bar(-1), "░".repeat(8));
  assert.equal(C.bar(2), "█".repeat(8));
  assert.equal(C.bar(NaN), "░".repeat(8));
  assert.equal(C.labelLine("NO", 0.11, false, 4), "NO   █░░░░░░░ +0.11");
  assert.equal(C.labelLine("HELP", -0.03, true), "HELP ░".concat("░".repeat(7), " -0.03 <"));
  assert.equal(C.statusLine(0.31, "YES", true), "lead 0.31   choice: YES *");
  assert.equal(C.statusLine(undefined, null, false), "lead  -     no choice yet");
  assert.deepEqual([C.level(-0.2), C.level(0.5), C.level(3), C.level("x")], [0, 0.5, 1, 0]);
});
