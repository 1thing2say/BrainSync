// vep_core.js: the codes and the toy model behind web/vep-lab/index.html.
//     node --test web/vep-lab/test/vep_core.test.mjs
// tests/test_vep_lab.py runs this and compares the m-sequence and the kernel with Python.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const V = require("../vep_core.js");

function dot(a, b) { return a.reduce((s, x, i) => s + x * b[i], 0); }

test("the m-sequence from x^6 + x + 1 has 63 bits, 32 of them ones, and a full period", () => {
  const s = V.mSequence(63);
  assert.equal(s.length, 63);
  assert.ok(s.every((b) => b === 0 || b === 1));
  assert.equal(s.reduce((a, b) => a + b, 0), 32);
  for (let k = 1; k < 63; k++) assert.notDeepEqual(V.roll(s, k), s, "shift " + k + " maps onto itself");
  assert.deepEqual(V.mSequence(), s);   // the defaults are 63, seed 1
});

test("every listed degree gives a maximal sequence", () => {
  for (const d of Object.keys(V.PRIMITIVE_TAPS).map(Number)) {
    const n = 2 ** d - 1;
    const r = V.autocorrelation(V.mSequence(n));
    assert.equal(r[0], n, "degree " + d);
    assert.ok(r.slice(1).every((v) => v === -1), "degree " + d + " off-peak");
  }
});

test("bad lengths and seeds are refused with the value in the message", () => {
  for (const bad of [0, 1, 2, 10, 62, 64, 7.5, 2 ** 11 - 1]) assert.throws(() => V.mSequence(bad), RangeError);
  assert.throws(() => V.mSequence(63, 0), /seed.*got 0/);
  assert.throws(() => V.mSequence(63, 64), /seed/);
  assert.throws(() => V.autocorrelation([0, 2, 1]), /0\/1/);
  assert.throws(() => V.autocorrelation([]), /empty/);
});

test("autocorrelation of the 63-bit sequence is N at lag 0 and -1 at every other lag", () => {
  const r = V.autocorrelation(V.mSequence(63));
  assert.equal(r.length, 63);
  assert.equal(r[0], 63);
  assert.deepEqual([...new Set(r.slice(1))], [-1]);
});

test("lagged codes are delayed copies (np.roll) and mutually near-orthogonal", () => {
  const s = V.mSequence(63);
  const codes = V.laggedCodes(s, 4, 16);
  assert.equal(codes.length, 4);
  assert.ok(codes.every((c) => c.length === 63));
  assert.deepEqual(codes[0], s);
  for (let k = 0; k < 4; k++) {
    for (let i = 0; i < 63; i++) assert.equal(codes[k][i], s[(i - 16 * k + 63 * 4) % 63]);
  }
  const pm = codes.map((c) => c.map((b) => 2 * b - 1));
  for (let a = 0; a < 4; a++) for (let b = 0; b < 4; b++) assert.equal(dot(pm[a], pm[b]), a === b ? 63 : -1);
  const paper = V.laggedCodes(s, 16, 4);
  assert.equal(paper.length, 16);
  assert.deepEqual(paper[1], V.roll(s, 4));
  assert.throws(() => V.laggedCodes(s, 16, 5), /need 76 frames/);
  assert.throws(() => V.laggedCodes(s, 0, 4), /nTargets/);
  assert.deepEqual(V.laggedCodes([1, 0, 1], 1, 1, 2)[0], [1, 1, 0, 0, 1, 1]);
});

test("flashes never overlap, every round flashes each target once, a slot apart", () => {
  for (const order of ["random", "roundrobin"]) {
    const s = V.timeSchedule(4, 2, 13, 10, order, 3);
    assert.equal(s.nFrames, 10 * 4 * 15);
    assert.equal(s.onsets.length, 40);
    const m = V.scheduleMatrix(s);
    for (let f = 0; f < s.nFrames; f++) {
      assert.ok(m.reduce((a, row) => a + row[f], 0) <= 1, "frame " + f + " lit twice");
    }
    for (let i = 1; i < s.onsets.length; i++) assert.equal(s.onsets[i][0] - s.onsets[i - 1][0], 15);
    for (let r = 0; r < 10; r++) {
      const round = s.onsets.slice(r * 4, r * 4 + 4).map((o) => o[1]).sort();
      assert.deepEqual(round, [0, 1, 2, 3]);
    }
    for (let k = 0; k < 4; k++) assert.equal(V.onsetsOf(s, k).length, 10);
  }
  const rr = V.timeSchedule(3, 1, 0, 2, "roundrobin");
  assert.deepEqual(rr.onsets, [[0, 0], [1, 1], [2, 2], [3, 0], [4, 1], [5, 2]]);
  assert.deepEqual(V.timeSchedule(4, 2, 13, 5, "random", 9), V.timeSchedule(4, 2, 13, 5, "random", 9));
  assert.throws(() => V.timeSchedule(4, 2, 13, 1, "sorted"), /unknown order/);
  assert.throws(() => V.timeSchedule(4, 0, 13, 1), /flashFrames/);
});

test("on/off frames: 15 Hz at 60 Hz is 2 on, 2 off; 60/7 Hz is 7 frames a cycle", () => {
  assert.deepEqual(V.onOffFrames(15, 8), [1, 1, 0, 0, 1, 1, 0, 0]);
  const f = V.onOffFrames(60 / 7, 14);
  assert.deepEqual(f.slice(0, 7), f.slice(7));
  assert.equal(f.slice(0, 7).reduce((a, b) => a + b, 0), 4);
  assert.equal(V.onOffBit(15, 5), 1);
  assert.throws(() => V.onOffFrames(0, 4), /positive/);
});

test("the kernel peaks at 1 near 100 ms, is zero through the latency and quiet by 400 ms", () => {
  const k = V.vepKernel(250);
  assert.equal(k.length, 125);
  const peak = k.indexOf(Math.max(...k));
  assert.equal(Math.max(...k), 1);
  assert.ok(Math.abs(peak / 250 - 0.1) <= 0.012, "peak at " + peak / 250 + " s");
  assert.ok(k.slice(0, 15).every((v) => v === 0));          // 60 ms latency = 15 samples
  assert.ok(Math.min(...k) < -0.3 && Math.min(...k) > -1);  // a trough, smaller than the peak
  assert.ok(k.slice(100).every((v) => Math.abs(v) < 0.05)); // quiet after 400 ms (exp(-3.4) = 0.033)
  assert.throws(() => V.vepKernel(250, 0.5, 0.6), /latency/);
  assert.throws(() => V.vepKernel(0), /positive/);
});

test("sequenceResponse is the causal convolution trimmed to the stimulus length", () => {
  assert.deepEqual(V.sequenceResponse([1, 0, 0, 0, 0], [1, 2, 3]), [1, 2, 3, 0, 0]);
  assert.deepEqual(V.sequenceResponse([1, 1, 0], [1, 1]), [1, 2, 1]);
  assert.throws(() => V.sequenceResponse([], [1]), /empty/);
  const sig = V.framesToSignal([1, 0], 9, 250, 60);   // 250/60 = 4.17 samples a frame
  assert.deepEqual(sig, [0.5, 0.5, 0.5, 0.5, 0.5, -0.5, -0.5, -0.5, -0.5]);
  assert.equal(V.framesToSignal([1], 6, 250, 60)[5], 0);  // past the last frame: nothing
});

test("the DFT of a 10 Hz sine peaks at 10 Hz", () => {
  // a little seeded noise gives the neighbour ring a floor to divide by
  const rng = V.makeRng(7);
  const x = [];
  for (let i = 0; i < 500; i++) x.push(Math.sin(2 * Math.PI * 10 * i / 250) + 0.05 * (rng() - 0.5));
  const spec = V.dft(x, 250);
  assert.equal(spec.freqs.length, 251);
  assert.equal(spec.freqs[1], 0.5);
  let best = 0;
  spec.power.forEach((p, i) => { if (p > spec.power[best]) best = i; });
  assert.equal(spec.freqs[best], 10);
  assert.ok(V.peakSnr(spec, 10, 1.0) > 100);
  assert.ok(V.peakSnr(spec, 20, 1.0) < 5);
  assert.throws(() => V.dft([1], 250), /2 samples/);
});

test("epoch average, peak-to-valley, pearson and ITR", () => {
  const x = [0, 1, 2, 0, 0, 1, 2, 0, 9];
  assert.deepEqual(V.epochAverage(x, [0, 4], 3), [0, 1, 2]);
  assert.deepEqual(V.epochAverage(x, [8], 3), [0, 0, 0]);   // an epoch past the end is skipped
  assert.equal(V.peakToValley([-1, 0.5, 2]), 3);
  assert.ok(Math.abs(V.pearson([1, 2, 3], [2, 4, 6]) - 1) < 1e-12);
  assert.ok(Math.abs(V.pearson([1, 2, 3], [3, 2, 1]) + 1) < 1e-12);
  assert.equal(V.pearson([1, 1, 1], [1, 2, 3]), 0);
  assert.ok(Math.abs(V.bitsPerSelection(16, 1) - 4) < 1e-12);
  assert.ok(Math.abs(V.bitsPerSelection(2, 0.5)) < 1e-12);
  assert.ok(V.bitsPerSelection(6, 0.85) > 1.5 && V.bitsPerSelection(6, 0.85) < 2.6);
  assert.throws(() => V.bitsPerSelection(1, 0.5), /targets/);
});
