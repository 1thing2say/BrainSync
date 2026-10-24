// map.js: interpolation, colour scales, message checks, and create() driven through a fake DOM.
//     node --test 'extension/brainmap/test/*.test.mjs'
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fakeDom, FakeEventSource, byRole, find, findAll, spyTimers } from "./fakedom.mjs";

const require = createRequire(import.meta.url);
const M = require("../map.js");

// The shared contract's 10-20 layout (brainsync/brainmap/layout.py).
const CHANNELS = ["Fp1", "Fp2", "C3", "C4", "P7", "P8", "O1", "O2"];
const POSITIONS = {
  Fp1: [-0.247, 0.761], Fp2: [0.247, 0.761], C3: [-0.4, 0.0], C4: [0.4, 0.0],
  P7: [-0.647, -0.470], P8: [0.647, -0.470], O1: [-0.247, -0.761], O2: [0.247, -0.761],
};
const REGIONS = {
  Fp1: "prefrontal", Fp2: "prefrontal", C3: "sensorimotor", C4: "sensorimotor",
  P7: "temporal-parietal", P8: "temporal-parietal", O1: "visual", O2: "visual",
};
const POINTS = CHANNELS.map((c) => POSITIONS[c]);

const hello = () => ({
  type: "hello", format: 1, source: "fake", channels: CHANNELS.slice(), positions: POSITIONS,
  regions: REGIONS, bands: { delta: [1, 4], theta: [4, 8], alpha: [8, 13], beta: [13, 30] },
  ssvep_hz: [15, 17], fs: 250, window_s: 2, hop_s: 0.25, baseline_s: 30,
  label: "Your EEG · 8 electrodes · scalp map, not a brain scan",
});

const fill = (v) => CHANNELS.map(() => v);
function frame(over) {
  const f = {
    type: "frame", seq: 1, t: 1_761_300_000.25,
    z: { delta: fill(0.1), theta: fill(-0.2), alpha: [0, 0, 0.5, 0.4, 1.2, 1.1, 2.5, 2.25], beta: fill(0.3) },
    db: { delta: fill(12), theta: fill(9), alpha: [6, 6, 7, 7, 9, 9, 13, 12], beta: fill(2) },
    ssvep: { 15: [0, 0, 1, 1, 3, 3, 8.5, 7.5], 17: fill(0.5) },
    quality: fill("ok"),
    baseline: { state: "ready", elapsed_s: 30, seconds: 30 },
  };
  return Object.assign(f, over || {});
}

const finite = (g) => Array.from(g).filter(Number.isFinite);
// The grid cell (row 0 at the nose) that contains head point [x, y].
const cellAt = (n, [x, y]) => Math.floor((1 - y) / 2 * n) * n + Math.floor((x + 1) / 2 * n);
const nearest = (x, y) => Math.min(...POINTS.map(([px, py]) => Math.hypot(x - px, y - py)));

// --- interpolate -----------------------------------------------------------------------

test("idw and interpolate return the exact value at each electrode", () => {
  const values = [-2.5, 1.25, 3, 0.5, -1, 2, 0.75, -0.25];
  POINTS.forEach(([x, y], k) => assert.equal(M.idw(POINTS, values, x, y), values[k], CHANNELS[k]));
  // n = 4 puts pixel centres at +-0.25 and +-0.75: electrodes there land exactly on cells.
  const pts = [[-0.25, 0.25], [0.25, 0.25], [-0.25, -0.75], [0.75, -0.25]];
  const vals = [1.5, -2, 0.25, 3];
  const g = M.interpolate(pts, vals, 4);
  const cell = ([x, y]) => (Math.round((1 - y) / 0.5 - 0.5)) * 4 + Math.round((x + 1) / 0.5 - 0.5);
  pts.forEach((p, k) => assert.equal(g[cell(p)], vals[k], `point ${p}`));
});

test("interpolate stays within the valid values, is NaN outside the head and beyond MAX_REACH", () => {
  const values = [-2.5, 1.25, 3, 0.5, -1, 2, 0.75, -0.25];
  const n = 64, g = M.interpolate(POINTS, values, n);
  assert.ok(g instanceof Float32Array);
  assert.equal(g.length, n * n);
  let inside = 0, blank = 0;
  for (let j = 0; j < n; j++) {
    for (let i = 0; i < n; i++) {
      const x = -1 + (i + 0.5) * 2 / n, y = 1 - (j + 0.5) * 2 / n, v = g[j * n + i];
      if (x * x + y * y > 1) assert.ok(Number.isNaN(v), `(${x}, ${y}) outside the head`);
      else if (nearest(x, y) > M.MAX_REACH) {
        inside++;
        blank++;
        assert.ok(Number.isNaN(v), `(${x}, ${y}) is farther than MAX_REACH from every electrode`);
        assert.ok(Math.abs(x) > 0.5, `(${x}, ${y}) only the temporal rims are out of reach`);
      } else {
        inside++;
        assert.ok(Number.isFinite(v), `(${x}, ${y}) inside the head`);
        assert.ok(v >= -2.5 && v <= 3, `${v} out of range`);
      }
    }
  }
  assert.ok(blank / inside < 0.1, `with every electrode good ${blank} of ${inside} cells are blank`);
  assert.ok(Number.isNaN(g[0]) && Number.isNaN(g[n * n - 1]), "corners are outside");
  assert.equal(finite(M.interpolate(POINTS, values, n, Infinity)).length, inside, "reach Infinity fills the head");
});

test("a bad or missing electrode's region stays blank instead of borrowing distant values", () => {
  // Only the posterior row reports: IDW alone would paint Fp1 with O1/O2's values.
  const posterior = [null, null, null, null, 1, 2, 3, 4];
  assert.ok(Number.isFinite(M.idw(POINTS, posterior, ...POSITIONS.Fp1)), "the raw estimator reaches anywhere");
  const n = 64, g = M.interpolate(POINTS, posterior, n);
  for (const c of ["Fp1", "Fp2", "C3", "C4"]) assert.ok(Number.isNaN(g[cellAt(n, POSITIONS[c])]), `${c} is blank`);
  assert.ok(Math.abs(g[cellAt(n, POSITIONS.O1)] - 3) < 0.01, "O1's own cell shows O1");
  assert.ok(Number.isFinite(g[cellAt(n, [-0.35, -0.6])]), "between P7 and O1 is drawn");
  // Every drawn cell is nearer a good electrode than any bad one, and within reach of it.
  for (let k = 0; k < g.length; k++) {
    if (!Number.isFinite(g[k])) continue;
    const x = -1 + ((k % n) + 0.5) * 2 / n, y = 1 - (Math.floor(k / n) + 0.5) * 2 / n;
    const d = POINTS.map(([px, py]) => Math.hypot(x - px, y - py));
    const goodD = Math.min(...d.slice(4)), badD = Math.min(...d.slice(0, 4));
    assert.ok(goodD <= badD && goodD <= M.MAX_REACH, `cell (${x}, ${y})`);
  }
  // The reach rule on its own: one good electrode lights only the disc around it.
  const one = M.interpolate([[0, 0]], [1], n);
  assert.equal(one[cellAt(n, [0.3, 0])], 1);
  assert.ok(Number.isNaN(one[cellAt(n, [0.6, 0])]));
  assert.equal(M.interpolate([[0, 0]], [1], n, Infinity)[cellAt(n, [0.6, 0])], 1);
});

test("interpolate skips null and NaN values; all null gives all NaN", () => {
  const values = [null, null, 1, 1, NaN, null, 1, 1];
  const g = M.interpolate(POINTS, values, 32);
  const vals = finite(g);
  assert.ok(vals.length > 0);
  assert.ok(vals.every((v) => v === 1), "only the valid electrodes count");
  // The raw estimator still fills a skipped Fp1 from its neighbours; the grid leaves it blank.
  assert.equal(M.idw(POINTS, [null, 4, 2, 2, 2, 2, 2, 2], ...POSITIONS.Fp1) > 2, true);
  assert.ok(Number.isNaN(M.interpolate(POINTS, [null, 4, 2, 2, 2, 2, 2, 2], 64)[cellAt(64, POSITIONS.Fp1)]));
  assert.ok(Number.isNaN(g[cellAt(32, POSITIONS.P7)]), "P7 is NaN: its region is blank");
  assert.equal(finite(M.interpolate(POINTS, fill(null), 32)).length, 0);
  assert.equal(finite(M.interpolate(POINTS, fill(NaN), 32)).length, 0);
  assert.throws(() => M.interpolate(POINTS, [1, 2], 8), /same length/);
});

// --- colour ----------------------------------------------------------------------------

test("colour is blue at vmin, dark in the middle, red at vmax, and clamps", () => {
  const blue = M.DIVERGING[0][1], red = M.DIVERGING[M.DIVERGING.length - 1][1];
  const mid = M.DIVERGING[2][1];
  assert.deepEqual(M.colour(-3), blue);
  assert.deepEqual(M.colour(0), mid);
  assert.deepEqual(M.colour(3), red);
  assert.deepEqual(M.colour(-99), blue);
  assert.deepEqual(M.colour(99), red);
  assert.equal(M.colour(-Infinity), null, "not a number: no colour");
  assert.equal(M.colour(NaN), null);
  assert.equal(M.colour(null), null);
  assert.ok(blue[2] > blue[0] && red[0] > red[2], "blue is blue and red is red");
  assert.deepEqual(M.colour(0, -6, 6), mid);
  assert.deepEqual(M.colour(6, -6, 6), red);
  assert.deepEqual(M.colour(2, 2, 10), blue);
  const c = M.colour(1.5);
  assert.ok(c.every((v) => Number.isInteger(v) && v >= 0 && v <= 255));
  assert.ok(c[0] > c[2], "positive z leans red");
});

test("a resting head is dim: the diverging middle is near the head background, ends get brighter", () => {
  const lum = (c) => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  const bg = [1, 3, 5].map((i) => parseInt(M.HEAD_BG.slice(i, i + 2), 16));
  const mid = M.colour(0);
  assert.ok(lum(mid) < 60, `z = 0 has luminance ${lum(mid)}`);
  assert.ok(Math.max(...mid.map((v, i) => Math.abs(v - bg[i]))) <= 24, `${mid} is close to ${bg}`);
  for (let z = 0; z < 3; z += 0.25) {
    assert.ok(lum(M.colour(z + 0.25)) > lum(M.colour(z)), `brighter from ${z} towards +3`);
    assert.ok(lum(M.colour(-z - 0.25)) > lum(M.colour(-z)), `brighter from ${-z} towards -3`);
  }
  assert.ok(lum(M.colour(3)) > 3 * lum(mid) && lum(M.colour(-3)) > 3 * lum(mid));
});

test("the SNR scale runs dark to bright from 0 to 12 dB and clamps", () => {
  const lum = (c) => 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  assert.deepEqual(M.sequential(0), M.SEQUENTIAL[0][1]);
  assert.deepEqual(M.sequential(12), M.SEQUENTIAL[M.SEQUENTIAL.length - 1][1]);
  assert.deepEqual(M.sequential(-5), M.sequential(0));
  assert.deepEqual(M.sequential(40), M.sequential(12));
  for (let v = 0; v < 12; v += 1) assert.ok(lum(M.sequential(v + 1)) > lum(M.sequential(v)), `${v} dB`);
});

// --- messageProblem --------------------------------------------------------------------

test("messageProblem accepts a valid hello, frame and status", () => {
  const h = hello();
  assert.equal(M.messageProblem(h, null), "");
  assert.equal(M.messageProblem(frame(), h), "");
  const collecting = frame({ baseline: { state: "collecting", elapsed_s: 12.3, seconds: 30 } });
  collecting.z = { delta: fill(null), theta: fill(null), alpha: fill(null), beta: fill(null) };
  assert.equal(M.messageProblem(collecting, h), "");
  for (const state of M.STATUS_STATES) assert.equal(M.messageProblem({ type: "status", state, text: "" }, h), "");
});

test("messageProblem rejects unknown types, wrong lengths, NaN and strings", () => {
  const h = hello();
  const bad = (over, pattern) => assert.match(M.messageProblem(frame(over), h), pattern);
  assert.match(M.messageProblem(null, h), /not an object/);
  assert.match(M.messageProblem([1], h), /not an object/);
  assert.match(M.messageProblem({ type: "frames" }, h), /unknown message type/);
  assert.match(M.messageProblem({}, h), /unknown message type/);
  assert.match(M.messageProblem(frame(), null), /before hello/);
  bad({ z: { ...frame().z, alpha: [1, 2, 3] } }, /z\.alpha/);
  bad({ z: { ...frame().z, beta: [...fill(0).slice(1), NaN] } }, /z\.beta/);
  bad({ db: { ...frame().db, theta: [...fill(1).slice(1), "3"] } }, /db\.theta/);
  bad({ db: { delta: fill(1), theta: fill(1), alpha: fill(1) } }, /db\.beta/);
  bad({ ssvep: { 15: fill(1) } }, /ssvep\.17/);
  bad({ ssvep: { 15: fill(1), 17: [...fill(1).slice(1), Infinity] } }, /ssvep\.17/);
  bad({ quality: fill("ok").slice(1) }, /quality/);
  bad({ quality: [...fill("ok").slice(1), 3] }, /quality/);
  bad({ baseline: { state: "warming", elapsed_s: 1, seconds: 30 } }, /baseline/);
  bad({ seq: 1.5 }, /seq/);
  bad({ t: NaN }, /\bt\b/);
  assert.match(M.messageProblem({ type: "status", state: "flying", text: "" }, h), /status state/);
  assert.match(M.messageProblem({ type: "status", state: "ended" }, h), /text/);
  const hbad = (over, pattern) => assert.match(M.messageProblem({ ...hello(), ...over }, null), pattern);
  hbad({ format: 2 }, /format/);
  hbad({ channels: [] }, /channel/);
  hbad({ positions: { ...POSITIONS, O2: [0.2] } }, /O2/);
  hbad({ positions: { ...POSITIONS, O1: [NaN, 0] } }, /O1/);
  hbad({ bands: { alpha: [13, 8] } }, /band alpha/);
  hbad({ ssvep_hz: ["15"] }, /ssvep_hz/);
  hbad({ ssvep_keys: ["15"] }, /ssvep_keys/);
  hbad({ ssvep_keys: ["15", 17] }, /ssvep_keys/);
  hbad({ ssvep_keys: ["15", "15"] }, /repeats an ssvep key/);
  hbad({ fs: 0 }, /fs/);
  hbad({ label: "" }, /label/);
});

test("ssvep_keys from hello name the frame.ssvep keys; without them String(Hz) is used", () => {
  const h = { ...hello(), ssvep_keys: ["15.0", "17.0"] };
  assert.equal(M.messageProblem(h, null), "");
  assert.deepEqual(M.ssvepKeys(h), ["15.0", "17.0"]);
  assert.deepEqual(M.ssvepKeys(hello()), ["15", "17"], "fallback for a hello without ssvep_keys");
  const f = frame({ ssvep: { "15.0": [0, 0, 1, 1, 3, 3, 8.5, 7.5], "17.0": fill(0.5) } });
  assert.equal(M.messageProblem(f, h), "");
  assert.match(M.messageProblem(frame(), h), /ssvep\.15\.0/, "String(Hz) keys do not satisfy ssvep_keys");
  const views = M.viewsFor(h);
  assert.deepEqual(views.slice(4).map((v) => [v.id, v.key, v.title]),
    [["ssvep15", "15.0", "SSVEP 15 Hz"], ["ssvep17", "17.0", "SSVEP 17 Hz"]]);
  assert.equal(M.viewValues(f, views[4]).values[6], 8.5);
  const { dom, es, page, cells } = mount();
  es.send(h);
  es.send(f);
  dom.doc.dispatch("keydown", { key: "5" });
  assert.equal(page.state().dropped, 0, page.state().problem);
  assert.deepEqual(cells("O1"), ["O1", "visual", "8.5 dB", "ok"]);
});

test("viewValues uses z when ready, dB relative to the channel mean while collecting, nulls bad sites", () => {
  const h = hello();
  const views = M.viewsFor(h);
  assert.deepEqual(views.map((v) => v.id), ["delta", "theta", "alpha", "beta", "ssvep15", "ssvep17"]);
  const ready = M.viewValues(frame({ quality: ["flat", ...fill("ok").slice(1)] }), views[2]);
  assert.equal(ready.kind, "z");
  assert.equal(ready.values[0], null);
  assert.equal(ready.values[6], 2.5);
  const f = frame({ baseline: { state: "collecting", elapsed_s: 3, seconds: 30 } });
  const rel = M.viewValues(f, views[3]);
  assert.equal(rel.kind, "rel");
  assert.deepEqual(rel.values, fill(0));
  const snr = M.viewValues(frame(), views[4]);
  assert.equal(snr.kind, "snr");
  assert.equal(snr.values[6], 8.5);
});

// --- create() with a fake DOM ----------------------------------------------------------

// layout: optional (canvas) => CSS width in px, set as clientWidth on each new canvas.
function mount(layout) {
  const dom = fakeDom();
  const canvases = [];
  const make = dom.doc.createElement;
  dom.doc.createElement = (tag) => {
    const e = make(tag);
    if (tag === "canvas") {
      canvases.push(e);
      if (layout) Object.defineProperty(e, "clientWidth", { get: () => layout(e) });
    }
    return e;
  };
  const page = M.create({ doc: dom.doc, win: dom.win });
  const es = FakeEventSource.last;
  const row = (name) => find(dom.root, (e) => e.getAttribute("data-ch") === name);
  const cells = (name) => row(name).children.map((c) => c.textContent);
  const mainCanvas = () => byRole(dom.root, "main");
  return { dom, page, es, row, cells, canvases, mainCanvas };
}

test("create() connects to events and shows the scalp-map label before any data", () => {
  const { dom, es, page } = mount();
  assert.equal(es.url, "events");
  assert.match(byRole(dom.root, "status").textContent, /Connecting/);
  assert.match(byRole(dom.root, "label").textContent, /not a brain scan/);
  assert.match(byRole(dom.root, "label").textContent, /scalp map/);
  assert.equal(page.state().draws, 0, "nothing to draw before hello");
  es.open();
  assert.match(byRole(dom.root, "status").textContent, /Connected/);
});

test("a hello then frames fill the electrode table with name, region and value", () => {
  const { dom, es, page, cells, row } = mount();
  es.send(hello());
  assert.deepEqual(findAll(dom.root, (e) => e.hasAttribute("data-ch")).map((e) => e.getAttribute("data-ch")), CHANNELS);
  assert.equal(byRole(dom.root, "label").textContent, hello().label);
  es.send(frame());
  assert.deepEqual(cells("O1"), ["O1", "visual", "+2.5", "ok"]);
  assert.deepEqual(cells("C4"), ["C4", "sensorimotor", "+0.4", "ok"]);
  assert.equal(row("O1").getAttribute("data-quality"), "ok");
  es.send(frame({ seq: 2, t: frame().t + 0.25, z: { ...frame().z, alpha: [0, 0, 0, 0, 0, 0, -1.25, 0] } }));
  assert.equal(cells("O1")[2], M.formatValue(-1.25, "z"));
  assert.match(cells("O1")[2], /^−1\.[23]$/);
  assert.match(byRole(dom.root, "main").getAttribute("aria-label"), /O1 −1\.\d/);
  assert.equal(page.state().dropped, 0);
  assert.equal(page.state().timeline.length, 2);
});

test("a bad channel shows its quality, no value, and is left out of the map", () => {
  const { dom, es, cells, row } = mount();
  es.send(hello());
  es.send(frame({ quality: ["ok", "flat", "ok", "ok", "noisy", "ok", "ok", "ok"] }));
  assert.deepEqual(cells("Fp2"), ["Fp2", "prefrontal", "—", "flat"]);
  assert.deepEqual(cells("P7"), ["P7", "temporal-parietal", "—", "noisy"]);
  assert.equal(row("Fp2").getAttribute("data-quality"), "flat");
  assert.match(byRole(dom.root, "main").getAttribute("aria-label"), /Fp2 flat/);
});

test("a good contact without a z-score yet says it has no baseline yet, not just '–'", () => {
  // The stream gives a channel that was bad during the rest period its own full baseline, so
  // after "ready" it can be "ok" with z null for a while (brainsync/brainmap/features.py).
  const { dom, es, cells, row } = mount();
  es.send(hello());
  const z = { ...frame().z, alpha: [null, 0, 0.5, 0.4, 1.2, 1.1, 2.5, 2.25] };
  es.send(frame({ z, quality: ["ok", "flat", "ok", "ok", "ok", "ok", "ok", "ok"] }));
  assert.deepEqual(cells("Fp1"), ["Fp1", "prefrontal", "no baseline yet", "ok"]);
  assert.equal(row("Fp1").getAttribute("data-quality"), "ok");
  assert.deepEqual(cells("Fp2"), ["Fp2", "prefrontal", "—", "flat"], "a bad contact keeps its own text");
  assert.equal(cells("O1")[2], "+2.5");
  const aria = byRole(dom.root, "main").getAttribute("aria-label");
  assert.match(aria, /Fp1 no baseline yet/);
  assert.match(aria, /Fp2 flat/);
  // While the whole baseline collects the views show relative dB, where the label never applies.
  es.send(frame({ seq: 2, baseline: { state: "collecting", elapsed_s: 3, seconds: 30 } }));
  assert.ok(!findAll(dom.root, (e) => e.hasAttribute("data-ch")).some((r) => /no baseline/.test(r.textContent)));
});

test("the drawn map fades out towards a blank region instead of a hard 64-cell staircase", () => {
  const { es, canvases } = mount();
  es.send(hello());
  const quality = ["flat", "flat", "flat", "flat", "ok", "ok", "ok", "ok"];
  es.send(frame({ quality }));
  const img = mainImageOf(canvases);
  const values = frame().z.alpha.map((v, i) => (quality[i] === "ok" ? v : null));
  const g = M.interpolate(POINTS, values, M.GRID, M.MAX_REACH);
  // The drawn grid pads 3 cells past the head circle; inside it, it matches interpolate().
  let soft = 0;
  for (let k = 0; k < g.length; k++) {
    const x = -1 + ((k % M.GRID) + 0.5) * 2 / M.GRID, y = 1 - (Math.floor(k / M.GRID) + 0.5) * 2 / M.GRID;
    if (x * x + y * y > 1) continue;
    const a = img.data[4 * k + 3];
    if (Number.isNaN(g[k])) assert.equal(a, 0, `blank cell (${x}, ${y}) is not drawn`);
    else if (a > 0 && a < 255) soft++;
  }
  assert.ok(soft >= 10, `${soft} cells fade along the edge`);
  assert.equal(img.data[4 * cellAt(M.GRID, POSITIONS.O1) + 3], 255, "fully opaque at a good electrode");
  assert.equal(img.data[4 * cellAt(M.GRID, POSITIONS.Fp1) + 3], 0, "nothing at a bad one");
});

test("while the baseline collects the page says so and shows dB relative to the mean", () => {
  const { dom, es, page, cells } = mount();
  es.send(hello());
  const z = { delta: fill(null), theta: fill(null), alpha: fill(null), beta: fill(null) };
  es.send(frame({ z, baseline: { state: "collecting", elapsed_s: 12.7, seconds: 30 } }));
  const b = byRole(dom.root, "baseline");
  assert.equal(b.hidden, false);
  assert.equal(b.textContent, "Sit still, eyes open — baseline 12 / 30 s");
  assert.match(byRole(dom.root, "legend").textContent, /relative to the mean across electrodes/);
  // alpha dB [6, 6, 7, 7, 9, 9, 13, 12] has mean 8.625: O1 is +4.4 dB above it.
  assert.deepEqual(cells("O1"), ["O1", "visual", "+4.4 dB", "ok"]);
  assert.equal(page.state().timeline[0].v, null, "no alpha z for the timeline yet");
  es.send(frame({ seq: 2 }));
  assert.equal(b.hidden, true);
  assert.match(byRole(dom.root, "legend").textContent, /z-score/);
});

test("status messages and connection errors update the status line", () => {
  const { dom, es } = mount();
  const line = () => byRole(dom.root, "status");
  es.open();
  es.send({ type: "status", state: "streaming", text: "fake board" });
  assert.match(line().textContent, /Streaming · fake board/);
  assert.equal(line().getAttribute("data-state"), "streaming");
  assert.equal(line().getAttribute("role"), "status");
  es.send({ type: "status", state: "no-data", text: "" });
  assert.match(line().textContent, /No data/);
  assert.equal(es.closed, false, "no data is not the end: keep listening");
  es.fail(false);
  assert.match(line().textContent, /Disconnected, reconnecting/);
  es.fail(true);
  assert.equal(line().getAttribute("data-state"), "closed");
});

test("a terminal status closes the stream, so the reconnect error cannot overwrite it", () => {
  for (const [state, text, pattern] of [["ended", "", /^Replay ended$/], ["error", "serial port gone", /^Error · serial port gone$/]]) {
    const { dom, es, page } = mount();
    const line = () => byRole(dom.root, "status");
    es.open();
    es.send(hello());
    es.send(frame());
    es.send({ type: "status", state, text });
    assert.ok(es.closed, `${state} closes the EventSource`);
    assert.equal(page.state().terminal, true);
    es.fail(false);                                    // what the server hanging up would fire
    es.fail(true);
    assert.equal(line().getAttribute("data-state"), state);
    assert.match(line().textContent, pattern);
  }
});

test("when data stops the last frame stays dimmed with values and contact as '–' until a new frame", () => {
  for (const stop of [(es) => es.fail(false), (es) => es.fail(true),
    (es) => es.send({ type: "status", state: "no-data", text: "" }),
    (es) => es.send({ type: "status", state: "error", text: "boom" }),
    (es) => es.send({ type: "status", state: "ended", text: "" })]) {
    const { dom, es, page, cells, row, mainCanvas } = mount();
    es.send(hello());
    es.send(frame({ quality: ["ok", "flat", "ok", "ok", "ok", "ok", "ok", "ok"] }));
    assert.equal(dom.root.getAttribute("data-stale"), "false");
    const draws = page.state().draws;
    const alphas = mainCanvas().getContext("2d").sets.globalAlpha;
    const before = alphas.length;
    stop(es);
    const st = page.state();
    assert.equal(st.stale, true);
    assert.equal(st.draws, draws + 1, "redrawn at once, not at the next frame");
    assert.equal(dom.root.getAttribute("data-stale"), "true");
    assert.deepEqual(cells("O1"), ["O1", "visual", "–", "–"]);
    assert.deepEqual(cells("Fp2"), ["Fp2", "prefrontal", "–", "–"], "a stale contact is not reported");
    assert.equal(row("Fp2").getAttribute("data-quality"), "");
    assert.ok(alphas.slice(before).includes(0.35), "the heads are drawn faded");
    assert.equal(alphas[alphas.length - 1], 1, "and the alpha is restored");
    const aria = mainCanvas().getAttribute("aria-label");
    assert.match(aria, /no live data/);
    assert.doesNotMatch(aria, /O1 \+2\.5/, "the stale value is not read out");
    assert.equal(byRole(dom.root, "timeline-now").textContent, "No live data");
    if (es.closed) continue;                           // terminal: nothing more comes
    es.send(frame({ seq: 2, t: frame().t + 0.25 }));
    assert.equal(page.state().stale, false);
    assert.equal(dom.root.getAttribute("data-stale"), "false");
    assert.deepEqual(cells("O1"), ["O1", "visual", "+2.5", "ok"]);
    assert.match(mainCanvas().getAttribute("aria-label"), /O1 \+2\.5/);
  }
});

test("a reconnect with the same hello keeps the last frame on screen; a different one drops it", () => {
  const { dom, es, page, cells, canvases } = mount();
  es.send(hello());
  es.send(frame());
  es.fail(false);
  const thumbs = canvases.filter((c) => c.width === 100);
  es.open();
  es.send(hello());                                    // a new but identical object
  let st = page.state();
  assert.ok(st.frame, "the frame survives the reconnect");
  assert.equal(st.stale, true, "still stale until a frame arrives");
  assert.equal(st.timeline.length, 2, "the frame and the gap marker");
  assert.deepEqual(canvases.filter((c) => c.width === 100), thumbs, "no new thumbnails");
  const img = mainImageOf(canvases);
  assert.ok(img.data.some((v, k) => k % 4 === 3 && v === 255), "the main head is not blank");
  es.send(frame({ seq: 2, t: frame().t + 5 }));
  assert.deepEqual(cells("O1"), ["O1", "visual", "+2.5", "ok"]);

  for (const change of [{ channels: CHANNELS.slice(0, 7) }, { positions: { ...POSITIONS, O1: [-0.3, -0.7] } },
    { bands: { alpha: [8, 12] } }, { ssvep_keys: ["15.0", "17.0"] }]) {
    es.send(hello());
    es.send(frame({ seq: 3 }));
    es.send({ ...hello(), ...change });
    st = page.state();
    assert.equal(st.frame, null, JSON.stringify(change));
    assert.equal(st.stale, false);
    assert.equal(cells("O1")[2], "–");
  }
  assert.equal(page.state().dropped, 0, page.state().problem);
});

test("the timeline breaks its line at gaps over 2 hops and at a disconnect", () => {
  const t0 = 100;
  const p = (dt, v) => ({ t: t0 + dt, v });
  const runs = M.timelineRuns([p(0, 1), p(0.25, 2), p(0.5, 1), p(3, 1), p(3.25, 2), p(3.25, null), p(3.5, 1), p(3.75, 0)], 0.5);
  assert.deepEqual(runs.map((r) => r.map((q) => q.t - t0)), [[0, 0.25, 0.5], [3, 3.25], [3.5, 3.75]]);
  assert.deepEqual(M.timelineRuns([p(0, 1), p(0.5, 1)], 0.5).length, 1, "exactly 2 hops still joins");
  assert.deepEqual(M.timelineRuns([p(0, null)], 0.5), []);

  const { es, page, canvases } = mount();
  es.send(hello());
  const t = frame().t;
  es.send(frame({ seq: 1, t }));
  es.send(frame({ seq: 2, t: t + 0.25 }));
  es.fail(false);
  assert.deepEqual(page.state().timeline.slice(-1)[0], { t: t + 0.25, v: null }, "a gap marker on disconnect");
  es.fail(false);
  assert.equal(page.state().timeline.length, 3, "one marker per outage");
  es.send(hello());
  es.send(frame({ seq: 3, t: t + 0.5 }));              // right after: only the marker breaks it
  es.send(frame({ seq: 4, t: t + 0.75 }));
  es.send(frame({ seq: 5, t: t + 9 }));                // a jump with no marker
  es.send(frame({ seq: 6, t: t + 9.25 }));
  const tl = canvases.find((c) => c.getAttribute("data-role") === "timeline").getContext("2d");
  const moves = tl.calls.moveTo, lines = tl.calls.lineTo;
  es.send(frame({ seq: 7, t: t + 9.5 }));
  // 3 grid lines, then runs [t, t+.25], [t+.5, t+.75], [t+9 .. t+9.5]: 3 + 3 moveTo, 3 + 1 + 1 + 2 lineTo.
  assert.equal(tl.calls.moveTo - moves, 6);
  assert.equal(tl.calls.lineTo - lines, 7);
});

test("canvas backing stores follow CSS size x devicePixelRatio, at start and on resize", () => {
  let css = 300;
  const { dom, es, page, canvases, mainCanvas } = mount((c) => (c.getAttribute("data-role") === "main" ? css : 80));
  // Before create() the fake window has no devicePixelRatio: 1.
  assert.equal(mainCanvas().width, 300);
  dom.win.devicePixelRatio = 2;
  es.send(hello());
  es.send(frame());
  const thumbs = () => findAll(dom.root, (e) => e.tagName === "CANVAS" && e.parent && e.parent.className === "thumb");
  dom.win.dispatch("resize");
  assert.equal(mainCanvas().width, 600);
  assert.equal(mainCanvas().height, 600);
  assert.ok(thumbs().length === 4 && thumbs().every((c) => c.width === 160 && c.height === 160), "thumbnails at 2x");
  const tl = canvases.find((c) => c.getAttribute("data-role") === "timeline");
  assert.equal(tl.width, 160);
  assert.equal(tl.height, 32);
  const draws = page.state().draws;
  css = 450;
  dom.win.devicePixelRatio = 3;
  const counts = spyTimers(() => dom.win.dispatch("resize"));
  assert.equal(mainCanvas().width, 1350);
  assert.equal(page.state().draws, draws + 1, "redrawn at once");
  assert.deepEqual(counts, { raf: 0, timers: 0 }, "no rAF or timer to redraw");
  assert.equal(canvases[3].width, M.GRID, "the main head's 64x64 offscreen grid is untouched");
  css = 100000;
  dom.win.dispatch("resize");
  assert.equal(mainCanvas().width, 2048, "capped");
  page.destroy();
  assert.equal(dom.win.listeners.resize.length, 0, "destroy() removes the resize listener");
});

test("keys 1-6 and the thumbnails select views; redraws happen only per message, never via rAF", () => {
  const { dom, es, page, cells } = mount();
  es.send(hello());
  es.send(frame());
  const draws = page.state().draws;
  dom.doc.dispatch("keydown", { key: "5" });
  assert.equal(page.state().view, "ssvep15");
  assert.equal(byRole(dom.root, "view-title").textContent, "SSVEP 15 Hz");
  assert.deepEqual(cells("O1"), ["O1", "visual", "8.5 dB", "ok"]);
  assert.match(byRole(dom.root, "legend").textContent, /signal-to-noise/);
  dom.doc.dispatch("keydown", { key: "1", metaKey: true });
  assert.equal(page.state().view, "ssvep15", "Cmd-1 is left to the browser");
  dom.doc.dispatch("keydown", { key: "9" });
  assert.equal(page.state().view, "ssvep15");
  const delta = find(dom.root, (e) => e.getAttribute("data-view") === "delta");
  assert.equal(delta.tagName, "BUTTON");
  delta.dispatch("click");
  assert.equal(page.state().view, "delta");
  assert.equal(delta.getAttribute("aria-pressed"), "true");
  assert.equal(findAll(dom.root, (e) => e.getAttribute("aria-pressed") === "true").length, 1);
  assert.equal(findAll(dom.root, (e) => e.getAttribute("data-view") && e.className === "thumb").length, 4);
  assert.equal(page.state().draws, draws + 2, "one redraw per view change");
  es.send(frame({ seq: 2 }));
  assert.equal(page.state().draws, draws + 3, "one redraw per frame");
  assert.equal(dom.counts.raf, 0, "no requestAnimationFrame");
  assert.equal(dom.counts.timers, 0, "no timers");
});

test("streaming, statuses, views and resizes schedule no rAF or timer, on the window or globally", () => {
  // The spy itself catches a regression: a bare setTimeout() or rAF inside fn is counted.
  assert.deepEqual(spyTimers(() => { setTimeout(() => {}, 0); globalThis.requestAnimationFrame(() => {}); }), { raf: 1, timers: 1 });
  assert.equal(typeof setTimeout, "function", "the real setTimeout is back");
  let dom;
  const counts = spyTimers(() => {
    const m = mount();
    dom = m.dom;
    m.es.open();
    m.es.send(hello());
    m.es.send({ type: "status", state: "streaming", text: "" });
    for (let k = 0; k < 40; k++) m.es.send(frame({ seq: k, t: frame().t + k * 0.25 }));
    for (let k = 1; k <= 6; k++) dom.doc.dispatch("keydown", { key: String(k) });
    dom.win.dispatch("resize");
    m.es.fail(false);
    m.es.send(hello());
    m.es.send(frame({ seq: 41, t: frame().t + 20 }));
    m.es.send({ type: "status", state: "ended", text: "" });
  });
  assert.deepEqual(counts, { raf: 0, timers: 0 });
  assert.deepEqual(dom.counts, { raf: 0, timers: 0 });
});

test("the page and map.js contain no CSS animation, transition or @keyframes", () => {
  const strip = (s) => s.replace(/<!--[\s\S]*?-->/g, "").replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
  for (const file of ["../index.html", "../map.js"]) {
    const code = strip(readFileSync(new URL(file, import.meta.url), "utf8"));
    assert.ok(code.length > 1000, `${file} was read`);
    for (const word of [/@keyframes/i, /animation/i, /transition/i, /requestAnimationFrame/, /\.animate\(/, /setInterval|setTimeout/]) {
      assert.doesNotMatch(code, word, `${file} uses ${word}`);
    }
  }
});

test("bad messages are dropped without breaking the page", () => {
  const { dom, es, page, cells } = mount();
  es.send(frame());
  assert.match(page.state().problem, /before hello/);
  es.send(hello());
  es.send(frame());
  es.send("{not json");
  es.send({ ...frame(), quality: ["ok"] });
  assert.equal(page.state().dropped, 3);
  assert.equal(dom.warnings.length, 3);
  assert.deepEqual(cells("O1"), ["O1", "visual", "+2.5", "ok"], "the last good frame stays up");
});

test("the occipital alpha timeline keeps 60 s and restarts when time goes back", () => {
  const { es, page } = mount();
  es.send(hello());
  const t0 = frame().t;
  for (let k = 0; k < 300; k++) es.send(frame({ seq: k, t: t0 + k * 0.25 }));
  const tl = page.state().timeline;
  assert.ok(tl[tl.length - 1].t - tl[0].t <= 60);
  assert.ok(tl.length >= 240 && tl.length <= 241);
  assert.equal(tl[0].v, (2.5 + 2.25) / 2, "mean of O1 and O2 alpha z");
  es.send(frame({ seq: 0, t: t0 }));
  assert.equal(page.state().timeline.length, 1);
});

function mainImageOf(canvases) {
  return canvases.find((c) => c.width === M.GRID).getContext("2d").last.putImageData[0];
}

test("destroy() closes the stream and empties the page", () => {
  const { dom, es, page } = mount();
  page.destroy();
  assert.ok(es.closed);
  assert.equal(dom.root.children.length, 0);
  assert.equal((dom.doc.listeners.keydown || []).length, 0);
});

test("formatValue signs z and relative dB, and never shows −0.0", () => {
  assert.equal(M.formatValue(1.84, "z"), "+1.8");
  assert.equal(M.formatValue(-1.84, "z"), "−1.8");
  assert.equal(M.formatValue(-0.04, "z"), "0.0");
  assert.equal(M.formatValue(2.3, "rel"), "+2.3 dB");
  assert.equal(M.formatValue(6.14, "snr"), "6.1 dB");
  assert.equal(M.formatValue(-3.5, "snr"), "−3.5 dB");
  assert.equal(M.formatValue(null, "z"), "–");
  assert.equal(M.formatValue(NaN, "snr"), "–");
});
