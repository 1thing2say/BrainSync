// panel.js create(): frames follow video.currentTime, layers cross-fade, stale loads are ignored.
// A minimal fake DOM: just the calls panel.js makes.
//     node --test 'extension/heatmap/test/*.test.mjs'
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const H = require("../panel.js");

function el() {
  const classes = new Set();
  const e = {
    style: {}, textContent: "", onload: null, _src: "",
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c) },
    setAttribute() {}, remove() { e.removed = true; },
    appendChild() {},
    get src() { return e._src; },
    set src(v) { e._src = v; queueMicrotask(() => e.onload && e.onload()); },
  };
  return e;
}

function fakeDom() {
  const parts = { ".panel": el(), ".stage": el(), ".empty": el(), ".what": el(), ".credit": el() };
  const imgs = [el(), el()];
  const host = el();
  host.attachShadow = () => ({
    set innerHTML(_) {},
    querySelector: (q) => parts[q],
    querySelectorAll: (q) => (q === ".stage img" ? imgs : []),
  });
  const listeners = {};
  const win = {
    innerWidth: 1728, innerHeight: 1117, console: { warn() {} },
    addEventListener: (n, f) => { listeners[n] = f; }, removeEventListener: (n) => { delete listeners[n]; },
  };
  const doc = { createElement: () => host, body: el() };
  return { doc, win, host, parts, imgs, listeners };
}

function fakeVideo() {
  const handlers = {};
  return {
    currentTime: 0,
    addEventListener: (n, f) => { (handlers[n] = handlers[n] || new Set()).add(f); },
    removeEventListener: (n, f) => handlers[n] && handlers[n].delete(f),
    fire(n) { (handlers[n] || []).forEach((f) => f()); },
    count: (n) => (handlers[n] ? handlers[n].size : 0),
  };
}

const manifest = (id, n) => ({
  format: 1, clip_id: id, duration_s: n, frame_times_s: [...Array(n).keys()],
  frames: [...Array(n).keys()].map((k) => `frames/${String(k).padStart(4, "0")}.png`),
  label: "Predicted average-brain fMRI response (Meta TRIBE v2, video + audio only). Not your EEG.",
  credit: "TRIBE v2 by Meta FAIR, CC BY-NC 4.0",
});

const flush = () => new Promise((r) => setTimeout(r, 0));

function front(dom) {
  return dom.imgs.find((i) => i.classList.contains("on"));
}

test("the frame follows currentTime and the two layers swap", async () => {
  const dom = fakeDom();
  const fetchJson = async (url) => { assert.equal(url, "clips/smoke/manifest.json"); return manifest("smoke", 5); };
  const panel = H.create({ doc: dom.doc, win: dom.win, base: "clips/", fetchJson });
  const video = fakeVideo();
  await panel.show(video, "smoke");
  await flush();
  assert.equal(panel.state().frame, 0);
  assert.equal(front(dom).src, "clips/smoke/frames/0000.png");
  assert.match(dom.parts[".what"].textContent, /Not your EEG/);

  video.currentTime = 2.4;
  video.fire("timeupdate");
  await flush();
  assert.equal(panel.state().frame, 2);
  assert.equal(front(dom).src, "clips/smoke/frames/0002.png");
  assert.equal(dom.imgs.filter((i) => i.classList.contains("on")).length, 1, "exactly one layer shown");

  video.currentTime = 2.9;            // same frame: no reload
  const before = front(dom).src;
  video.fire("timeupdate");
  await flush();
  assert.equal(front(dom).src, before);

  video.currentTime = 0.5;            // seeking back
  video.fire("seeked");
  await flush();
  assert.equal(front(dom).src, "clips/smoke/frames/0000.png");
});

test("a clip without a prediction keeps the backdrop with a placeholder", async () => {
  const dom = fakeDom();
  const panel = H.create({ doc: dom.doc, win: dom.win, fetchJson: async () => { throw new Error("HTTP 404"); } });
  await panel.show(fakeVideo(), "nope");
  await flush();
  assert.equal(panel.state().manifest, null);
  assert.equal(dom.parts[".empty"].textContent, "No prediction for this reel");
  assert.ok(!dom.parts[".panel"].classList.contains("off"), "panel stays up");
  assert.equal(front(dom), undefined);
  await panel.show(fakeVideo(), null);
  assert.equal(dom.parts[".empty"].textContent, "No prediction for this reel");
});

test("switching reels detaches the old video and ignores its late manifest", async () => {
  const dom = fakeDom();
  let release;
  const slow = new Promise((r) => { release = r; });
  const fetchJson = (url) => (url.includes("/a/") ? slow : Promise.resolve(manifest("b", 3)));
  const panel = H.create({ doc: dom.doc, win: dom.win, fetchJson });
  const va = fakeVideo(), vb = fakeVideo();
  const pending = panel.show(va, "a");
  await panel.show(vb, "b");
  assert.equal(va.count("timeupdate"), 0, "old video detached");
  assert.equal(vb.count("timeupdate"), 1);
  release(manifest("a", 3));
  await pending;
  await flush();
  assert.equal(panel.state().clipId, "b");
  assert.equal(front(dom).src, "clips/b/frames/0000.png");
});

test("a screen too narrow hides the panel instead of crowding the squares", () => {
  const dom = fakeDom();
  dom.win.innerWidth = 1000; dom.win.innerHeight = 1000;
  const panel = H.create({ doc: dom.doc, win: dom.win, fetchJson: async () => manifest("x", 1) });
  assert.ok(dom.parts[".panel"].classList.contains("off"));
  assert.ok(panel.state().problem);
  panel.destroy();
  assert.ok(dom.host.removed);
  assert.equal(dom.listeners.resize, undefined);
});
