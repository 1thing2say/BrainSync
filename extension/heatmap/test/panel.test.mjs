// panel.js pure helpers: frame lookup (as brainsync/heatmap/manifest.py), layout keep-out.
//     node --test 'extension/heatmap/test/*.test.mjs'
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const H = require("../panel.js");

const SCREENS = [[1728, 1117], [1512, 982], [1920, 1080], [2560, 1440], [1440, 900]];

test("frameAt picks the last frame starting at or before t, like manifest.frame_at", () => {
  const times = [0, 1, 2, 3, 4];
  const cases = [[-1, 0], [0, 0], [0.99, 0], [1, 1], [2.5, 2], [4, 4], [99, 4], [NaN, 0], [Infinity, 0]];
  for (const [t, want] of cases) assert.equal(H.frameAt(t, times), want, `t = ${t}`);
  assert.equal(H.frameAt(5, [2, 7]), 0);
  assert.equal(H.frameAt(7, [2, 7]), 1);
  assert.throws(() => H.frameAt(1, []), /no frames/);
});

test("the default layout fits every demo screen shape without touching squares, column or HUD", () => {
  for (const [vw, vh] of SCREENS) {
    assert.equal(H.layoutProblem(H.DEFAULT_LAYOUT, H.DEFAULT_TARGETS, vw, vh), null, `${vw}x${vh}`);
  }
});

test("the panel stays more than KEEP_OUT from both flicker squares", () => {
  const [vw, vh] = [1728, 1117];
  const p = H.panelRect(H.DEFAULT_LAYOUT, vw, vh);
  for (const s of H.squareRects(H.DEFAULT_TARGETS, vw, vh)) {
    const gap = s.left - (p.left + p.width);
    assert.ok(gap > H.KEEP_OUT * vh, `${s.label}: gap ${gap.toFixed(0)} px`);
  }
});

test("layoutProblem refuses a panel near a square, over the column, off screen or on the HUD", () => {
  const [vw, vh] = [1728, 1117];
  const at = (o) => H.layoutProblem({ ...H.DEFAULT_LAYOUT, ...o }, H.DEFAULT_TARGETS, vw, vh);
  assert.match(at({ x: 0.55 }), /square/);
  assert.match(at({ x: 0.0, width: 0.2 }), /column/);
  assert.match(at({ x: -0.9 }), /does not fit/);
  assert.match(at({ top: -0.1, width: 0.3 }), /HUD/);
  assert.match(H.layoutProblem(H.DEFAULT_LAYOUT, H.DEFAULT_TARGETS, 1024, 1024), /does not fit|column/);
});

test("manifestProblems accepts a good manifest and names what is wrong otherwise", () => {
  const good = { format: 1, clip_id: "a", frame_times_s: [0, 1], frames: ["frames/0000.png", "frames/0001.png"],
    label: "Predicted ... Not your EEG.", credit: "TRIBE v2 by Meta FAIR, CC BY-NC 4.0" };
  assert.deepEqual(H.manifestProblems(good), []);
  assert.deepEqual(H.manifestProblems(null), ["manifest is not an object"]);
  const bad = (o) => H.manifestProblems({ ...good, ...o });
  assert.ok(bad({ format: 2 }).some((m) => /format/.test(m)));
  assert.ok(bad({ frames: [] }).some((m) => /no frames/.test(m)));
  assert.ok(bad({ frame_times_s: [0] }).some((m) => /differ/.test(m)));
  assert.ok(bad({ frame_times_s: [1, 1] }).some((m) => /increasing/.test(m)));
  assert.ok(bad({ label: "Your brain" }).some((m) => /not your EEG/.test(m)));
  assert.ok(bad({ credit: "" }).some((m) => /credit/.test(m)));
});

test("the panel sits just under the overlay's z-index", () => {
  assert.equal(H.Z_PANEL, 2147483646);
});
