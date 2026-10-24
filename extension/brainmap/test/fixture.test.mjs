// map.js against the real Python stream: every message in frames_fixture.json (written by
// make_fixture.py from brainsync.brainmap.stream) must pass messageProblem() and render.
//     node --test 'extension/brainmap/test/*.test.mjs'
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fakeDom, FakeEventSource, byRole, find, spyTimers } from "./fakedom.mjs";

const require = createRequire(import.meta.url);
const M = require("../map.js");
const fixture = JSON.parse(readFileSync(new URL("./frames_fixture.json", import.meta.url), "utf8"));
const messages = fixture.messages;
const hello = messages[0];
const frames = messages.filter((m) => m.type === "frame");

function mount() {
  const dom = fakeDom();
  const canvases = [];
  const make = dom.doc.createElement;
  dom.doc.createElement = (tag) => {
    const e = make(tag);
    if (tag === "canvas") canvases.push(e);
    return e;
  };
  const page = M.create({ doc: dom.doc, win: dom.win });
  const es = FakeEventSource.last;
  const cells = (name) => find(dom.root, (e) => e.getAttribute("data-ch") === name).children.map((c) => c.textContent);
  // makeHead gives each head a GRID x GRID offscreen canvas; the main head's comes first.
  const mainImage = () => canvases.find((c) => c.width === M.GRID).getContext("2d").last.putImageData[0];
  return { dom, page, es, cells, mainImage };
}

test("the fixture holds a hello, the statuses of a replay, and collecting, ready and bad frames", () => {
  assert.equal(hello.type, "hello");
  assert.equal(messages.filter((m) => m.type === "hello").length, 1);
  assert.deepEqual(messages.filter((m) => m.type === "status").map((m) => m.state), ["starting", "streaming", "ended"]);
  assert.ok(frames.length >= 8);
  assert.ok(frames.some((f) => f.baseline.state === "collecting"));
  assert.ok(frames.some((f) => f.baseline.state === "ready"));
  const qualities = new Set(frames.flatMap((f) => f.quality));
  for (const q of ["ok", "flat", "clipped", "noisy"]) assert.ok(qualities.has(q), q);
  assert.match(hello.label, /scalp map/);
});

test("every fixture message passes messageProblem()", () => {
  let last = null;
  for (const m of messages) {
    assert.equal(M.messageProblem(m, last), "", `${m.type} ${m.seq ?? m.state ?? ""}`);
    if (m.type === "hello") last = m;
  }
});

test("frames carry what the page reads: one value per channel, SSVEP keys matching hello", () => {
  const n = hello.channels.length;
  const views = M.viewsFor(hello);
  assert.deepEqual(views.map((v) => v.id), ["delta", "theta", "alpha", "beta", "ssvep15", "ssvep17"]);
  // hello.ssvep_keys (when the stream sends it) or String(Hz) are exactly frame.ssvep's keys.
  const keys = M.ssvepKeys(hello);
  assert.equal(keys.length, hello.ssvep_hz.length);
  if (hello.ssvep_keys) assert.deepEqual(hello.ssvep_keys, keys);
  for (const f of frames) assert.deepEqual(Object.keys(f.ssvep).sort(), keys.slice().sort(), `seq ${f.seq}`);
  for (const f of frames) {
    for (const v of views) {
      const vv = M.viewValues(f, v);
      assert.equal(vv.values.length, n);
      f.quality.forEach((q, i) => {
        if (q !== "ok") assert.equal(vv.values[i], null, `${hello.channels[i]} is ${q}`);
      });
      if (f.baseline.state === "ready" || v.kind === "ssvep") {
        assert.ok(f.quality.every((q, i) => q !== "ok" || Number.isFinite(vv.values[i])), `${v.id} seq ${f.seq}`);
      }
    }
  }
});

test("the fixture streams through create(): no drops, one redraw per frame, bad sites greyed", () => {
  const { dom, page, es, cells } = mount();
  es.open();
  const lastFrame = frames[frames.length - 1];
  const counts = spyTimers(() => {
    for (const m of messages) {
      es.send(m);
      if (m !== lastFrame) continue;
      hello.channels.forEach((name, i) => {
        const row = cells(name);
        assert.equal(row[3], lastFrame.quality[i]);
        if (lastFrame.quality[i] === "ok") assert.equal(row[2], M.formatValue(lastFrame.z.alpha[i], "z"));
        else assert.equal(row[2], "—");
      });
    }
  });
  const st = page.state();
  assert.equal(st.dropped, 0, st.problem);
  assert.equal(dom.warnings.length, 0);
  assert.equal(st.draws, 1 + frames.length + 1, "the hello, one per frame, and one when the replay ends");
  assert.equal(st.timeline.length, frames.length + 1, "every frame, then the end marker");
  assert.equal(byRole(dom.root, "status").getAttribute("data-state"), "ended");
  assert.equal(byRole(dom.root, "label").textContent, hello.label);
  assert.equal(byRole(dom.root, "baseline").hidden, true);
  // "ended" is terminal: the stream is closed, the last frame stays dimmed with no values.
  assert.ok(es.closed);
  es.fail(false);
  assert.match(byRole(dom.root, "status").textContent, /^Replay ended/);
  assert.equal(st.stale, true);
  for (const name of hello.channels) assert.deepEqual(cells(name).slice(2), ["–", "–"], name);
  for (let k = 1; k <= 6; k++) dom.doc.dispatch("keydown", { key: String(k) });
  assert.equal(page.state().view, "ssvep17");
  assert.equal(page.state().dropped, 0);
  assert.equal(dom.counts.raf + dom.counts.timers, 0);
  assert.deepEqual(counts, { raf: 0, timers: 0 }, "nothing scheduled on the real globals either");
});

test("the drawn map leaves bad electrodes out: their regions are blank, good sites keep their colour", () => {
  const { es, mainImage } = mount();
  es.send(hello);
  const bad = frames.find((f) => f.baseline.state === "ready" && f.quality.includes("flat"));
  es.send(bad);
  const view = M.viewsFor(hello).find((v) => v.id === "alpha");
  const { values } = M.viewValues(bad, view);
  const points = hello.channels.map((c) => hello.positions[c]);
  const grid = M.interpolate(points, values, M.GRID);
  const withBad = M.interpolate(points, bad.z.alpha.map((v, i) => (bad.quality[i] === "ok" ? v : 0)), M.GRID);
  const img = mainImage();
  assert.equal(img.width, M.GRID);
  const at = ([x, y]) => Math.floor((1 - y) / 2 * M.GRID) * M.GRID + Math.floor((x + 1) / 2 * M.GRID);
  const pixel = (k) => Array.from(img.data.slice(4 * k, 4 * k + 4));
  let blank = 0, drawn = 0;
  hello.channels.forEach((name, i) => {
    const k = at(hello.positions[name]);
    if (bad.quality[i] !== "ok") {
      assert.ok(Number.isNaN(grid[k]), `${name} cell is NaN`);
      assert.equal(pixel(k)[3], 0, `${name} pixel is left to the head background`);
      assert.ok(Number.isFinite(withBad[k]), `${name} would be painted if it were treated as good`);
      blank++;
    } else {
      assert.deepEqual(pixel(k), [...M.colour(grid[k], M.Z_RANGE[0], M.Z_RANGE[1]), 255], `${name} pixel`);
      drawn++;
    }
  });
  assert.ok(blank >= 2 && drawn >= 2);
});
