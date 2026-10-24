// Pure parts of overlay.js and scroller.js: geometry, vsync frame index, refresh meter,
// message parsing and the protocol state machine. No DOM, no browser.
//     node --test extension/test/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const F = require("../flicker.js");
const O = require("../overlay.js");
const S = require("../scroller.js");

const close = (a, b, tol = 1e-9) => assert.ok(Math.abs(a - b) <= tol, `${a} != ${b}`);

function fakeTransport() {
  const sent = [];
  const t = { sent, broken: false, onMessage() {}, onState() {} };
  t.send = (obj) => { if (t.broken) throw new Error("no port to the extension"); sent.push(obj); };
  return t;
}

function controller(extra = {}) {
  const transport = fakeTransport();
  const warnings = [];
  let clock = 0;
  const c = O.createController({
    transport, agent: "extension", url: () => "https://www.instagram.com/reels/x/",
    now: () => clock, warn: (...a) => warnings.push(a.join(" ")), refreshFrames: 10,
    warmupFrames: 2, ...extra,
  });
  return { c, sent: transport.sent, transport, warnings, tick: (ms) => { clock += ms; } };
}

// Feed rAF timestamps at exactly `hz` until the meter has a reading; returns the next ts.
function measure(c, hz, frames = 13, start = 0) {
  const p = 1000 / hz;
  for (let i = 0; i < frames; i++) c.frame(start + i * p);
  return start + frames * p;
}

const CONFIG = {
  type: "config", name: "reels", waveform: "sine", refresh_hz: 60.0, refresh_tolerance_hz: 1.0,
  targets: [{ label: "up", freq: 15.0, phase: 0.0, x: 0.55, y: 0.325, size: 0.3 },
            { label: "down", freq: 17.0, phase: 0.0, x: 0.55, y: -0.325, size: 0.3 }],
};

// ------------------------------------------------------------------ geometry

test("square centre is (W/2 + x H, H/2 - y H), side size H, y up", () => {
  const [up, down] = O.DEFAULT_CONFIG.targets;
  const r = O.squareRect(up, 1600, 1000);
  close(r.cx, 800 + 550); close(r.cy, 500 - 325); close(r.side, 300);
  close(r.left, 1350 - 150); close(r.top, 175 - 150);
  const d = O.squareRect(down, 1600, 1000);
  close(d.cy, 825);
});

test("cue outline is 0.006 H thick with its inner edge 0.03 H outside the square", () => {
  const t = { label: "c", x: 0.55, y: 0.2, size: 0.3 };
  const sq = O.squareRect(t, 1600, 1000);
  const cue = O.cueRect(t, 1600, 1000);
  close(cue.line, 6);
  const inner = cue.left + cue.line / 2;          // stroke is centred on the rect edge
  close(sq.left - inner, 30);
  close(cue.side - cue.line, sq.side + 60);
  close(O.backingRect(t, 1600, 1000).side, 300 + 30);
});

test("default squares sit right of a centred 9:16 video on a 16:10 screen", () => {
  for (const t of O.DEFAULT_CONFIG.targets) {
    const r = O.squareRect(t, 1600, 1000);
    const cue = O.cueRect(t, 1600, 1000);
    assert.ok(cue.left > 800 + 1000 * 9 / 16 / 2, "clears the video column");
    assert.ok(cue.left + cue.side <= 1600, "inside the screen");
    assert.ok(r.top >= 0 && r.top + r.side <= 1000);
  }
});

test("layout overflow names targets whose cue box leaves the screen", () => {
  const ok = { targets: [{ label: "a", x: 0.55, y: 0.3, size: 0.2 }] };
  assert.equal(O.layoutOverflow(ok, 1600, 1000), null);
  const wide = { targets: [{ label: "a", x: 0.75, y: 0, size: 0.2 }] };
  assert.match(O.layoutOverflow(wide, 1600, 1000), /a reaches x = 0\.856/);
  // gap 0 cannot save a square that is itself off screen
  assert.match(O.layoutOverflow({ targets: [{ label: "b", x: 0, y: 0.45, size: 0.2 }] }, 1600, 1000),
               /b reaches x = 0\.106, y = 0\.556/);
});

test("the cue gap shrinks so the reels cue boxes stay on screen", () => {
  // 0.5 - 0.325 - 0.15 - 0.006 = 0.019 H of room above the up square
  for (const t of O.DEFAULT_CONFIG.targets) {
    close(O.cueGap(t, 1600, 1000), 0.019, 1e-12);
    const cue = O.cueRect(t, 1600, 1000);
    assert.ok(cue.top - cue.line / 2 >= -1e-9 && cue.top + cue.side + cue.line / 2 <= 1000 + 1e-9);
  }
  assert.equal(O.layoutOverflow(O.DEFAULT_CONFIG, 1600, 1000), null);
  assert.equal(O.cueGap({ x: 0, y: 0, size: 0.3 }, 1600, 1000), O.CUE_GAP);
});

test("default config matches the protocol's reels config", () => {
  const cfg = O.parseConfig(CONFIG);
  assert.deepEqual(JSON.parse(JSON.stringify(cfg)), JSON.parse(JSON.stringify(O.DEFAULT_CONFIG)));
});

// ------------------------------------------------------------------ timing

test("frame index is the vsync count: a dropped frame skips an index, phase holds", () => {
  const p = 1000 / 60;
  assert.equal(O.frameIndex(1000, 1000, p), 0);
  assert.equal(O.frameIndex(1000 + 3 * p + 0.9, 1000, p), 3);     // jitter rounds away
  assert.equal(O.frameIndex(1000 + 5 * p, 1000, p), 5);           // frame 4 never came
  assert.ok(O.isDropped(2 * p, p));
  assert.ok(!O.isDropped(1.4 * p, p));
  assert.equal(F.DROP_FACTOR, 1.5);
});

test("median handles odd, even and partial buffers", () => {
  assert.equal(O.median([3, 1, 2]), 2);
  assert.equal(O.median([4, 1, 3, 2]), 2.5);
  assert.equal(O.median([5, 1, 9, 100], 3), 5);
  assert.ok(Number.isNaN(O.median([], 0)));
});

test("refresh meter skips warm-up and is robust to a stall", () => {
  const m = new O.RefreshMeter(120, 20);
  for (let i = 0; i < 20; i++) m.push(500);                      // page load stall, skipped
  assert.equal(m.hz(), null);
  for (let i = 0; i < 119; i++) m.push(i % 10 === 0 ? 33.4 : 1000 / 59.97);
  assert.ok(!m.ready());
  m.push(1000 / 59.97);
  assert.ok(m.ready() && m.due());
  close(m.hz(), 59.97, 1e-9);
  assert.equal(m.push(-1), false);
  assert.equal(m.push(NaN), false);
});

test("refresh meter is unbiased by Chrome's 100 us rAF timestamp coarsening", () => {
  // The median of 16.6/16.7 ms intervals reads 59.88 Hz on a 60 Hz display; the trimmed
  // mean cancels the rounding, so the frame index does not repeat a frame every 8 s.
  for (const hz of [60, 59.94, 60.003]) {
    for (const offset of [0, 0.03, 0.071]) {
      const m = new O.RefreshMeter(120, 20);
      const ts = (i) => Math.floor((offset + i * 1000 / hz) * 10) / 10;
      for (let i = 1; i <= 140; i++) m.push(ts(i) - ts(i - 1));
      close(m.hz(), hz, 0.01);
      const period = 1000 / m.hz();
      for (let n = 0; n < 3600; n++) assert.equal(O.frameIndex(ts(n + 1), ts(1), period), n);
    }
  }
});

// ------------------------------------------------------------------ parsing

test("malformed messages are ignored with a note, never thrown", () => {
  const { c, warnings, sent } = controller();
  for (const raw of ["not json", "[1,2]", "null", '{"no":"type"}', '{"type":"nope"}',
                     { type: "cue", target: 5 }, { type: "cue", target: 0.5 },
                     { type: "phase", phase: "dance", flicker: true },
                     { type: "phase", phase: "run", flicker: "yes" },
                     { type: "scroll", dir: "left", id: 1 }, { type: "scroll", dir: "up", id: "1" },
                     { type: "config", targets: [] }, { ...CONFIG, waveform: "triangle" },
                     { type: "status", text: 3 }]) {
    c.handle(raw);
  }
  assert.equal(warnings.length, 14);
  assert.equal(c.state.ignored, 14);
  assert.equal(c.state.phase, null);
  assert.equal(c.state.cue, -1);
  assert.deepEqual(sent, []);
});

test("ping is ignored silently; status shows in state", () => {
  const { c, warnings } = controller();
  c.handle('{"type":"ping"}');
  c.handle({ type: "status", text: "calibrating 12/30 s" });
  assert.deepEqual(warnings, []);
  assert.equal(c.state.status, "calibrating 12/30 s");
});

// ------------------------------------------------------------------ hello

test("hello waits for Start, a refresh reading and an open socket, once per socket", () => {
  const { c, sent } = controller();
  c.setConnection("open");
  assert.deepEqual(sent, []);
  c.accept();
  assert.deepEqual(sent, [], "no refresh yet");
  measure(c, 59.97);
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0], { type: "hello", agent: "extension",
                              url: "https://www.instagram.com/reels/x/", refresh_hz: 59.97,
                              dropped: 0, accepted: true, showing: true });
  measure(c, 59.97, 20, 10000);                                  // later readings: no repeat
  assert.equal(sent.filter((m) => m.type === "hello").length, 1);
  c.setConnection("closed");
  c.setConnection("connecting");
  c.setConnection("open");                                      // new socket: hello again
  assert.equal(sent.filter((m) => m.type === "hello").length, 2);
});

test("accepting before the socket opens sends hello as soon as it opens", () => {
  const { c, sent } = controller();
  measure(c, 60);
  c.accept();
  c.setConnection("connecting");
  assert.deepEqual(sent, []);
  c.setConnection("open");
  assert.equal(sent.length, 1);
  assert.equal(sent[0].type, "hello");
});

test("cancel never sends hello and keeps the squares hidden", () => {
  const { c, sent } = controller();
  c.setConnection("open");
  measure(c, 60);
  c.cancel();
  assert.equal(c.showSquares(), false);
  assert.equal(c.frame(99999), "hidden");
  assert.deepEqual(sent, []);
});

// ------------------------------------------------------------------ phases and drawing

test("nothing is drawn before Start; static squares after Start before any phase", () => {
  const { c } = controller();
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: 120, text: "Look" });
  assert.equal(c.frame(0), "hidden");
  c.accept();
  assert.equal(c.frame(16), "flicker");
  const { c: c2 } = controller();
  c2.accept();
  assert.equal(c2.frame(0), "still");
});

test("wait is still, run flickers from onset frame 0 at the config refresh", () => {
  const { c } = controller();
  c.accept();
  let ts = measure(c, 60);
  c.handle({ type: "phase", phase: "wait", flicker: false, seconds: null, text: "Keep still" });
  assert.equal(c.frame(ts), "still");
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: 120, text: "Look" });
  const p = 1000 / 60;
  ts += p;
  const t0 = ts;
  for (let n = 0; n < 30; n++) {
    if (n === 7) continue;                                       // frame 7 dropped
    assert.equal(c.frame(t0 + n * p + (n % 3 - 1) * 0.5), "flicker");
    const lum = c.lum();
    close(lum[0], F.luminance(15, 0, n, 60, "sine"));
    close(lum[1], F.luminance(17, 0, n, 60, "sine"));
  }
  assert.equal(c.state.dropped, 1);
});

test("calibrate(flicker) to run(flicker) keeps one onset; still in between resets it", () => {
  const { c } = controller();
  c.accept();
  let ts = measure(c, 60);
  c.handle({ type: "phase", phase: "calibrate", flicker: true, seconds: 30, text: "" });
  c.frame(ts);
  const onset = c.onset();
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: 120, text: "" });
  ts += 1000 / 60;
  c.frame(ts);
  assert.equal(c.onset(), onset);
  c.handle({ type: "phase", phase: "wait", flicker: false, seconds: null, text: "" });
  c.frame(ts += 1000 / 60);
  assert.equal(c.onset(), null);
});

test("stop hides the squares and stops the flicker", () => {
  const { c } = controller();
  c.accept();
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: 5, text: "" });
  assert.equal(c.frame(0), "flicker");
  c.handle({ type: "phase", phase: "stop", flicker: true, seconds: null, text: "Done" });
  assert.equal(c.flickering(), false);
  assert.equal(c.frame(16), "hidden");
  assert.equal(c.state.text, "Done");
});

test("frames report every 5 s while flickering, with cumulative drops", () => {
  const { c, sent } = controller();
  c.accept();
  let ts = measure(c, 60);
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: 20, text: "" });
  const p = 1000 / 60;
  for (let i = 0; i < 60 * 11; i++) {
    if (i === 100 || i === 400) { ts += p; }                    // two dropped frames
    c.frame(ts);
    ts += p;
  }
  const frames = sent.filter((m) => m.type === "frames");
  assert.equal(frames.length, 2);
  assert.equal(frames[0].dropped, 1);
  assert.equal(frames[1].dropped, 2);
  close(frames[0].refresh_hz, 60, 1e-3);
});

test("refresh off by more than the tolerance is flagged", () => {
  const { c } = controller();
  measure(c, 120);
  assert.ok(c.refreshOff());
  const { c: c2 } = controller();
  measure(c2, 60.5);
  assert.ok(!c2.refreshOff());
});

test("cue follows the committed target and resets on a smaller config", () => {
  const { c } = controller();
  c.handle({ type: "cue", target: 1 });
  assert.equal(c.state.cue, 1);
  c.handle({ type: "cue", target: -1 });
  assert.equal(c.state.cue, -1);
  c.handle({ type: "cue", target: 1 });
  c.handle({ ...CONFIG, targets: [CONFIG.targets[0]] });
  assert.equal(c.state.cue, -1);
  assert.equal(c.lum().length, 1);
});

test("config without phase or waveform gets the defaults; bad numbers are refused", () => {
  const cfg = O.parseConfig({ type: "config", targets: [{ freq: 12, x: 0, y: 0, size: 0.2 }] });
  assert.equal(cfg.waveform, "sine");
  assert.equal(cfg.targets[0].phase, 0);
  assert.equal(cfg.targets[0].label, "0");
  assert.throws(() => O.parseConfig({ targets: [{ freq: 0, x: 0, y: 0, size: 1 }] }), /freq/);
  assert.throws(() => O.parseConfig({ targets: [{ freq: 1, x: "a", y: 0, size: 1 }] }), /x/);
  assert.throws(() => O.parseConfig({ refresh_hz: -60, targets: [{ freq: 1, x: 0, y: 0, size: 1 }] }));
});

test("config depth defaults to 1 and must lie in (0, 1]", () => {
  assert.equal(O.DEFAULT_CONFIG.depth, 1);
  assert.equal(O.parseConfig(CONFIG).depth, 1);
  assert.equal(O.parseConfig({ ...CONFIG, depth: 0.5 }).depth, 0.5);
  assert.ok(Object.isFrozen(O.parseConfig({ ...CONFIG, depth: 0.5 })));
  for (const depth of [0, -0.5, 1.5, "0.5", null, NaN]) {
    assert.throws(() => O.parseConfig({ ...CONFIG, depth }), /depth/, String(depth));
  }
  const { c, warnings } = controller();
  c.handle({ ...CONFIG, depth: 2 });
  assert.equal(warnings.length, 1);
  assert.equal(c.state.config, O.DEFAULT_CONFIG);
});

test("run draws at the config depth", () => {
  const { c } = controller();
  c.handle({ ...CONFIG, depth: 0.5 });
  c.accept();
  let ts = measure(c, 60);
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: 120, text: "Look" });
  const p = 1000 / 60;
  ts += p;
  for (let n = 0; n < 30; n++) {
    assert.equal(c.frame(ts + n * p), "flicker");
    const lum = c.lum();
    assert.equal(lum[0], F.luminance(15, 0, n, 60, "sine", 0.5));
    assert.equal(lum[1], F.luminance(17, 0, n, 60, "sine", 0.5));
    assert.ok(lum.every((v) => v >= 0.25 && v <= 0.75));
  }
});

// ------------------------------------------------------------------ scroll and quit

test("scroll calls the scroller in order and acks each id", async () => {
  const calls = [];
  const scroller = async (dir) => { calls.push(dir); return { ok: dir === "down", method: "container" }; };
  const { c, sent } = controller({ scroller });
  c.accept();
  c.handle({ type: "scroll", dir: "down", id: 1 });
  c.handle('{"type":"scroll","dir":"up","id":2}');
  await c.idle();
  assert.deepEqual(calls, ["down", "up"]);
  assert.deepEqual(sent, [{ type: "scrolled", id: 1, ok: true, method: "container" },
                          { type: "scrolled", id: 2, ok: false, method: "container" }]);
  assert.deepEqual({ ...c.state.lastScroll, at: 0 }, { dir: "up", id: 2, ok: false, method: "container", at: 0 });
});

test("a throwing scroller or an unknown method acks ok false / none", async () => {
  const { c, sent } = controller({ scroller: async () => { throw new Error("boom"); } });
  c.accept();
  c.handle({ type: "scroll", dir: "down", id: 7 });
  await c.idle();
  assert.deepEqual(sent, [{ type: "scrolled", id: 7, ok: false, method: "none" }]);
  const { c: c2, sent: sent2 } = controller({ scroller: async () => ({ ok: true, method: "magic" }) });
  c2.accept();
  c2.handle({ type: "scroll", dir: "up", id: 8 });
  await c2.idle();
  assert.deepEqual(sent2, [{ type: "scrolled", id: 8, ok: true, method: "none" }]);
});

test("Escape (quit) sends bye once, stops the flicker and refuses later scrolls", async () => {
  let called = false;
  const { c, sent } = controller({ scroller: async () => { called = true; return { ok: true, method: "window" }; } });
  c.setConnection("open");
  c.accept();
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: 60, text: "" });
  c.quit();
  c.quit();
  assert.deepEqual(sent, [{ type: "bye" }]);
  assert.equal(c.frame(0), "hidden");
  c.handle({ type: "scroll", dir: "down", id: 3 });
  await c.idle();
  assert.equal(called, false);
  assert.deepEqual(sent[1], { type: "scrolled", id: 3, ok: false, method: "none" });
  c.accept();
  assert.equal(c.showSquares(), false, "Start after Escape does not restart");
});

test("bye that could not go out is sent on the next open socket, once", () => {
  const { c, sent, transport } = controller();
  c.setConnection("open");
  c.accept();
  c.setConnection("closed");                                    // worker restarting
  c.quit();
  assert.deepEqual(sent, [], "socket down: nothing sent yet");
  c.setConnection("connecting");
  c.setConnection("open");
  assert.deepEqual(sent, [{ type: "bye" }]);
  c.setConnection("closed");
  c.setConnection("open");                                      // a later server: no bye
  assert.equal(sent.length, 1);
  // the port itself gone: send throws, and the bye goes out on the next "open"
  const { c: c2, sent: sent2, transport: t2 } = controller();
  c2.setConnection("open");
  c2.accept();
  t2.broken = true;
  c2.quit();
  assert.deepEqual(sent2, []);
  t2.broken = false;
  c2.setConnection("closed");
  c2.setConnection("open");
  assert.deepEqual(sent2, [{ type: "bye" }], "and no hello after Escape");
  void transport;
});

test("a bye still pending after 15 s is dropped, not sent to the next run", () => {
  const { c, sent, tick } = controller();
  c.setConnection("open");
  c.accept();
  c.setConnection("closed");
  c.quit();
  tick(16000);                                                   // brainsync.reels restarted
  c.setConnection("open");
  assert.deepEqual(sent, []);
  c.setConnection("closed");
  c.setConnection("open");
  assert.deepEqual(sent, []);
});

test("flicker stops when the server connection drops and resumes on the bridge's phase", () => {
  const { c, sent } = controller();
  c.setConnection("open");
  c.accept();
  let ts = measure(c, 60);
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: null, text: "Look" });
  c.handle({ type: "cue", target: 1 });
  assert.equal(c.frame(ts), "flicker");
  const onset = c.onset();
  c.setConnection("closed");                                    // Python killed or crashed
  assert.equal(c.flickering(), false);
  assert.equal(c.state.cue, -1);
  assert.match(c.state.text, /disconnected/);
  const before = sent.length;
  const p = 1000 / 60;
  for (let i = 1; i <= 600; i++) assert.equal(c.frame(ts + i * p), "still");   // 10 s
  assert.equal(sent.length, before, "no frames reports without a server");
  ts += 601 * p;
  c.setConnection("connecting");
  c.setConnection("open");
  c.handle(CONFIG);                                              // what the bridge sends first
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: null, text: "Look" });
  assert.equal(c.frame(ts), "flicker");
  assert.ok(c.onset() > onset, "a new onset");
});

test("a dropped socket before any phase or after stop changes nothing", () => {
  const { c } = controller();
  c.setConnection("open");
  c.accept();
  c.setConnection("closed");
  assert.equal(c.state.text, "");
  c.setConnection("open");
  c.handle({ type: "phase", phase: "stop", flicker: false, seconds: null, text: "Done" });
  c.setConnection("closed");
  assert.equal(c.state.text, "Done");
});

test("squares hold still while the measured refresh is off", () => {
  const { c } = controller();
  c.accept();
  let ts = measure(c, 120);
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: null, text: "" });
  assert.equal(c.frame(ts), "still");
  assert.equal(c.flickering(), false);
  assert.equal(c.onset(), null);
  const { c: c2 } = controller();
  c2.accept();
  ts = measure(c2, 59.5);
  c2.handle({ type: "phase", phase: "run", flicker: true, seconds: null, text: "" });
  assert.equal(c2.frame(ts), "flicker");
});

test("only an accepted, visible tab on Reels scrolls; the others ack ok false / none", async () => {
  let calls = 0;
  const scroller = async () => { calls++; return { ok: true, method: "container" }; };
  let shown = true;
  const { c, sent } = controller({ scroller, visible: () => shown });
  c.handle({ type: "scroll", dir: "down", id: 1 });              // Start never pressed
  c.accept();
  shown = false;
  c.handle({ type: "scroll", dir: "down", id: 2 });              // background tab
  shown = true;
  c.setPaused(true);                                             // navigated to Home
  c.handle({ type: "scroll", dir: "down", id: 3 });
  await c.idle();
  assert.equal(calls, 0);
  assert.deepEqual(sent.map((m) => [m.id, m.ok, m.method]),
                   [[1, false, "none"], [2, false, "none"], [3, false, "none"]]);
  assert.equal(c.showSquares(), false);
  assert.equal(c.frame(0), "hidden");
  c.setPaused(false);
  c.handle({ type: "scroll", dir: "down", id: 4 });
  await c.idle();
  assert.equal(calls, 1);
  assert.deepEqual(sent[3], { type: "scrolled", id: 4, ok: true, method: "container" });
});

test("the server hears when the squares go out of view and come back, once per change", () => {
  let shown = false;
  const { c, sent } = controller({ visible: () => shown });
  const views = () => sent.filter((m) => m.type === "view").map((m) => m.showing);
  c.setConnection("open");
  c.accept();
  c.checkView();
  assert.deepEqual(sent, [], "nothing before the hello");
  shown = true;
  measure(c, 60);
  assert.equal(sent[0].type, "hello");
  assert.equal(sent[0].showing, true);
  shown = false;                                                 // another Space, a covered window
  c.checkView();
  c.checkView();
  shown = true;
  c.checkView();
  assert.deepEqual(views(), [false, true]);
  c.setPaused(true);                                             // navigated to Home
  c.setPaused(false);
  assert.deepEqual(views(), [false, true, false, true]);
  c.handle({ type: "phase", phase: "stop", flicker: false, seconds: null, text: "" });
  assert.deepEqual(views(), [false, true, false, true, false]);
  c.setConnection("closed");                                     // a new socket greets afresh
  c.setConnection("open");
  const hellos = sent.filter((m) => m.type === "hello");
  assert.equal(hellos.length, 2);
  assert.equal(hellos[1].showing, false);
  assert.equal(views().length, 5);
});

test("a hidden tab says so in its hello; nothing about the view after Escape", () => {
  let shown = false;
  const { c, sent } = controller({ visible: () => shown });
  c.setConnection("open");
  c.accept();
  measure(c, 60);
  assert.equal(sent[0].showing, false);
  c.quit();
  shown = true;
  c.checkView();
  assert.deepEqual(sent.map((m) => m.type), ["hello", "bye"]);
});

test("remaining seconds count down from the phase message", () => {
  const { c, tick } = controller();
  c.handle({ type: "phase", phase: "calibrate", flicker: false, seconds: 30, text: "" });
  tick(12500);
  close(c.remaining(), 17.5);
  tick(60000);
  assert.equal(c.remaining(), 0);
  c.handle({ type: "phase", phase: "run", flicker: true, seconds: null, text: "" });
  assert.equal(c.remaining(), null);
});

// ------------------------------------------------------------------ scroller pure parts

test("scroller judges movement on a new reel: URL or most visible video", () => {
  const v1 = {}, v2 = {};
  assert.ok(S.moved({ url: "/reels/a/", video: v1 }, { url: "/reels/b/", video: v1 }));
  assert.ok(S.moved({ url: "/reels/a/", video: v1 }, { url: "/reels/a/", video: v2 }));
  // scrolled a little and snapped back to the same reel: not a move (seen on instagram.com)
  assert.ok(!S.moved({ url: "/reels/a/", video: v1 }, { url: "/reels/a/", video: v1 }));
  assert.ok(!S.moved({ url: null, video: null }, { url: null, video: null }));
});

test("scroller neighbour: nearest reel above or below, never an overlapping one", () => {
  const r = (top) => ({ top, bottom: top + 1000 });
  const cur = r(0);
  const rects = [r(-2000), r(-1000), r(300), r(1000), r(2000)];
  assert.deepEqual(S.neighbour(cur, rects, "down"), r(1000));   // 300 overlaps the current reel
  assert.deepEqual(S.neighbour(cur, rects, "up"), r(-1000));
  assert.equal(S.neighbour(cur, [r(1000)], "up"), null);
  assert.equal(S.neighbour(cur, [], "down"), null);
});

/** A snap feed of ``n`` reels, each one <video> 1000 px tall; ``snapBack`` undoes scrolls. */
function fakeFeed(n, { snapBack = false, loaded = n } = {}) {
  const H = 1000;
  const loc = { href: "https://www.instagram.com/reels/r0/" };
  const feed = {
    scrollTop: 0, clientHeight: H, scrollHeight: n * H, parentElement: null, scrolls: [],
    contains: () => true,
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 600, bottom: H }),
    scrollBy({ top }) {
      this.scrolls.push(top);
      if (snapBack) return;
      this.scrollTop = Math.max(0, Math.min(this.scrollHeight - H, this.scrollTop + top));
      loc.href = "https://www.instagram.com/reels/r" + Math.round(this.scrollTop / H) + "/";
    },
  };
  const videos = Array.from({ length: loaded }, (_, i) => ({
    parentElement: feed,
    getBoundingClientRect: () => ({ left: 0, right: 600, top: i * H - feed.scrollTop,
                                    bottom: (i + 1) * H - feed.scrollTop }),
  }));
  const win = {
    innerWidth: 1500, innerHeight: H,
    getComputedStyle: () => ({ overflowY: "scroll", scrollSnapType: "y mandatory" }),
  };
  const doc = {
    location: loc, defaultView: win, scrollingElement: { scrollHeight: H, clientHeight: H },
    querySelectorAll: () => videos,
  };
  return { feed, doc, win, loc };
}

test("scroll moves exactly one reel and reports the new reel", async () => {
  const { feed, doc, win, loc } = fakeFeed(5);
  assert.deepEqual(await S.scroll("down", { doc, win, waitMs: 0 }),
    { ok: true, method: "container", edge: false });
  assert.equal(feed.scrollTop, 1000);
  assert.equal(loc.href, "https://www.instagram.com/reels/r1/");
  assert.deepEqual(await S.scroll("up", { doc, win, waitMs: 0 }),
    { ok: true, method: "container", edge: false });
  assert.equal(feed.scrollTop, 0);
});

test("up on the first reel is an edge: nothing scrolls, no debugger fallback", async () => {
  const { feed, doc, win } = fakeFeed(5);
  feed.scrollTop = 0;
  assert.deepEqual(await S.scroll("up", { doc, win, waitMs: 0 }),
    { ok: false, method: "container", edge: true });
  assert.deepEqual(feed.scrolls, []);
});

test("a scroll that snaps back to the same reel is not ok", async () => {
  const { doc, win } = fakeFeed(5, { snapBack: true });
  const r = await S.scroll("down", { doc, win, waitMs: 0 });
  assert.equal(r.ok, false);
  assert.equal(r.edge, false);   // so the content script may try the debugger key
});

test("down past the last loaded reel scrolls one screen so Instagram loads more", async () => {
  const { feed, doc, win } = fakeFeed(5, { loaded: 1 });
  const r = await S.scroll("down", { doc, win, waitMs: 0 });
  assert.deepEqual(feed.scrolls, [1000]);
  assert.equal(r.ok, true);   // the URL changed
});

test("scroller edges and visible area", () => {
  assert.ok(S.atEdge({ scrollTop: 0, clientHeight: 900, scrollHeight: 9000 }, "up"));
  assert.ok(!S.atEdge({ scrollTop: 0, clientHeight: 900, scrollHeight: 9000 }, "down"));
  assert.ok(S.atEdge({ scrollTop: 8100, clientHeight: 900, scrollHeight: 9000 }, "down"));
  assert.equal(S.visibleArea({ left: -10, top: 0, right: 90, bottom: 50 }, 100, 100), 4500);
  assert.equal(S.visibleArea({ left: 0, top: 200, right: 90, bottom: 250 }, 100, 100), 0);
});

// ------------------------------------------------------------------ EEG status line

test("eeg messages set the EEG state; bad ones are ignored", () => {
  const { c, warnings } = controller();
  assert.equal(c.state.eeg, "unknown");
  c.setConnection("open");
  c.handle({ type: "eeg", state: "absent", text: "plug in the Cerelog X8 USB receiver" });
  assert.equal(c.state.eeg, "absent");
  assert.equal(c.state.eegText, "plug in the Cerelog X8 USB receiver");
  c.handle({ type: "eeg", state: "streaming" });
  assert.equal(c.state.eeg, "streaming");
  assert.equal(c.state.eegText, "");
  c.handle({ type: "eeg", state: "happy" });
  c.handle({ type: "eeg", state: "absent", text: 3 });
  assert.equal(c.state.eeg, "streaming");
  assert.equal(warnings.length, 2);
});

test("losing the server makes the EEG state unknown until the bridge re-sends it", () => {
  const { c } = controller();
  c.setConnection("open");
  c.handle({ type: "eeg", state: "streaming", text: "/dev/cu.usbserial-1, 250 Hz" });
  c.setConnection("closed");
  assert.equal(c.state.eeg, "unknown");
  assert.equal(c.state.eegText, "");
  c.setConnection("open");
  c.handle({ type: "eeg", state: "streaming", text: "/dev/cu.usbserial-1, 250 Hz" });
  assert.equal(c.state.eeg, "streaming");
});

test("the HUD's EEG line names every state", () => {
  assert.deepEqual([...O.EEG_STATES], ["unknown", "absent", "connecting", "connected",
    "streaming", "no-data", "error", "unused"]);
  assert.equal(O.eegLine({ eeg: "absent", eegText: "plug in the Cerelog X8 USB receiver" }),
    "EEG: not connected · plug in the Cerelog X8 USB receiver");
  assert.equal(O.eegLine({ eeg: "streaming", eegText: "/dev/cu.usbserial-1, 250 Hz" }),
    "EEG: connected, receiving data · /dev/cu.usbserial-1, 250 Hz");
  assert.equal(O.eegLine({ eeg: "unused", eegText: "manual mode" }), "EEG: not used · manual mode");
  for (const st of O.EEG_STATES) assert.ok(!O.eegLine({ eeg: st, eegText: "" }).includes("undefined"), st);
});
