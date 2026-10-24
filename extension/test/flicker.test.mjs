// flicker.js against brainsync.stimulus.flicker.luminance (fixture from make_fixture.py).
//     node --test extension/test
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const F = require("../flicker.js");
const fixture = JSON.parse(readFileSync(new URL("./flicker_fixture.json", import.meta.url), "utf8"));

test("fixture covers 15/17/12 Hz, two phases, both waveforms, frames 0..240", () => {
  assert.equal(fixture.cases.length, 12);
  assert.equal(fixture.frames, 241);
  const keys = new Set(fixture.cases.map((c) => `${c.waveform}:${c.freq}:${c.phase}`));
  assert.equal(keys.size, 12);
});

for (const c of fixture.cases) {
  const name = `${c.waveform} ${c.freq} Hz phase ${(c.phase / Math.PI).toFixed(2)} pi`;
  test(`luminance matches python: ${name}`, () => {
    let worst = 0;
    c.values.forEach((want, n) => {
      worst = Math.max(worst, Math.abs(F.luminance(c.freq, c.phase, n, c.refresh, c.waveform) - want));
    });
    assert.ok(worst < 1e-9, `max abs diff ${worst}`);
  });
}

test("waveform defaults to sine and bad arguments throw like flicker.py", () => {
  assert.equal(F.luminance(15, 0, 1, 60), F.luminance(15, 0, 1, 60, "sine"));
  assert.throws(() => F.luminance(15, 0, 1, 0), RangeError);
  assert.throws(() => F.luminance(15, 0, 1, 60, "triangle"), RangeError);
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
