// flicker.js against brainsync.stimulus.flicker.luminance (fixture from make_fixture.py).
//     node --test extension/test
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const F = require("../flicker.js");
const fixture = JSON.parse(readFileSync(new URL("./flicker_fixture.json", import.meta.url), "utf8"));

test("fixture covers 15/17/12 Hz, two phases, both waveforms, depths 1 and 0.5, frames 0..240", () => {
  assert.equal(fixture.cases.length, 24);
  assert.equal(fixture.frames, 241);
  const keys = new Set(fixture.cases.map((c) => `${c.waveform}:${c.freq}:${c.phase}:${c.depth}`));
  assert.equal(keys.size, 24);
  assert.deepEqual([...new Set(fixture.cases.map((c) => c.depth))].sort(), [0.5, 1]);
});

for (const c of fixture.cases) {
  const name = `${c.waveform} ${c.freq} Hz phase ${(c.phase / Math.PI).toFixed(2)} pi depth ${c.depth}`;
  test(`luminance matches python: ${name}`, () => {
    let worst = 0;
    c.values.forEach((want, n) => {
      const got = F.luminance(c.freq, c.phase, n, c.refresh, c.waveform, c.depth);
      worst = Math.max(worst, Math.abs(got - want));
    });
    assert.ok(worst < 1e-9, `max abs diff ${worst}`);
  });
}

test("waveform defaults to sine and bad arguments throw like flicker.py", () => {
  assert.equal(F.luminance(15, 0, 1, 60), F.luminance(15, 0, 1, 60, "sine"));
  assert.throws(() => F.luminance(15, 0, 1, 0), RangeError);
  assert.throws(() => F.luminance(15, 0, 1, 60, "triangle"), RangeError);
});

test("depth defaults to 1, which is the full black-white swing exactly", () => {
  for (const w of F.WAVEFORMS) {
    for (let n = 0; n < 120; n++) {
      assert.equal(F.luminance(17, 0.3, n, 60, w, 1), F.luminance(17, 0.3, n, 60, w));
    }
  }
  assert.equal(F.luminance(15, 0, 1, 60, "sine", 1), 1);
  assert.equal(F.luminance(15, 0, 3, 60, "sine", 1), 0);
});

test("depth 0.5 swings 0.25..0.75 around mid-grey", () => {
  const seq = [0, 1, 2, 3].map((n) => F.luminance(15, 0, n, 60, "square", 0.5));
  assert.deepEqual(seq, [0.75, 0.75, 0.25, 0.25]);
  let sum = 0;
  for (let n = 0; n < 600; n++) {
    const v = F.luminance(17, 0.3, n, 60, "sine", 0.5);
    assert.ok(v >= 0.25 && v <= 0.75, `${v} at frame ${n}`);
    sum += v;
  }
  assert.ok(Math.abs(sum / 600 - 0.5) < 1e-12);
});

test("depth outside (0, 1] throws like flicker.py", () => {
  for (const d of [0, -0.1, 1.2, NaN, Infinity, "0.5", null]) {
    assert.throws(() => F.luminance(15, 0, 1, 60, "sine", d), RangeError, String(d));
    assert.throws(() => F.luminance(15, 0, 1, 60, "square", d), RangeError, String(d));
  }
});

test("pyMod follows numpy: the result takes the divisor's sign", () => {
  assert.equal(F.pyMod(-0.25, 1), 0.75);
  assert.equal(F.pyMod(1.25, 1), 0.25);
  assert.equal(F.pyMod(2, 1), 0);
});

test("square flicker at 15 Hz on 60 Hz is two frames on, two off", () => {
  const seq = [0, 1, 2, 3, 4, 5, 6, 7].map((n) => F.luminance(15, 0, n, 60, "square"));
  assert.deepEqual(seq, [1, 1, 0, 0, 1, 1, 0, 0]);
});

test("photosensitive band is 15-25 Hz inclusive", () => {
  assert.deepEqual([12, 15, 17, 25, 26].map(F.photosensitive), [false, true, true, true, false]);
});

test("attaches to globalThis for content scripts", () => {
  assert.equal(globalThis.BrainScrollFlicker, F);
});
