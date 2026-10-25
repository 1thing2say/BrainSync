// brainsync-client.js without a browser: RefreshMeter, frame index, FlickerState (rule 1,
// onset, luminance) and Connection over a fake WebSocket with fake timers.
//     node --test clients/js/test/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { makeFakeWS, makeFakeTimers } from "./fakes.mjs";

const require = createRequire(import.meta.url);
const F = require("../flicker.js");
const C = require("../brainsync-client.js");

const FIXTURE = JSON.parse(readFileSync(
  new URL("../../../extension/test/flicker_fixture.json", import.meta.url), "utf8"));
const close = (a, b, tol = 1e-9) => assert.ok(Math.abs(a - b) <= tol, `${a} != ${b}`);

// ------------------------------------------------------------------ module shape

test("exports and loads as a browser global after flicker.js", () => {
  for (const name of ["RefreshMeter", "FlickerState", "Connection", "frameIndex", "rectToPx",
                      "parseMessage", "median"]) {
    assert.equal(typeof C[name], "function", name);
  }
  assert.equal(C.PROTOCOL, 3);
  assert.deepEqual([...C.NO_RECONNECT_CODES], [4000, 4001, 4002]);
  assert.ok(Object.isFrozen(C));
  const ctx = { performance: { timeOrigin: 0 }, Date };
  ctx.globalThis = ctx;
  vm.runInNewContext(readFileSync(new URL("../flicker.js", import.meta.url), "utf8"), ctx);
  vm.runInNewContext(readFileSync(new URL("../brainsync-client.js", import.meta.url), "utf8"), ctx);
  assert.equal(typeof ctx.BrainSyncClient.FlickerState, "function");
  assert.equal(ctx.BrainSyncClient.flicker, ctx.BrainScrollFlicker);
});

test("refuses to load without flicker.js in a browser", () => {
  const ctx = {};
  ctx.globalThis = ctx;
  assert.throws(() => vm.runInNewContext(
    readFileSync(new URL("../brainsync-client.js", import.meta.url), "utf8"), ctx), /flicker\.js/);
});

// ------------------------------------------------------------------ RefreshMeter

test("RefreshMeter skips 20 warm-up intervals and averages the next 120", () => {
  const m = new C.RefreshMeter();
  for (let i = 0; i < 20; i++) assert.equal(m.push(1000), false);   // warm-up, any value
  for (let i = 0; i < 119; i++) { assert.equal(m.push(i % 2 ? 16.6 : 16.7), true); }
  assert.equal(m.ready(), false);
  assert.equal(m.hz(), null);
  m.push(16.6);
  assert.equal(m.ready(), true);
  assert.equal(m.due(), true);
  close(m.hz(), 1000 / 16.65, 1e-9);   // the mean cancels 100 us rounding; the median would not
  m.push(16.6);
  assert.equal(m.due(), false);
});

test("RefreshMeter leaves out stalls and dropped frames, and ignores bad intervals", () => {
  const m = new C.RefreshMeter(10, 0);
  for (const bad of [0, -5, NaN, Infinity, undefined, "16"]) assert.equal(m.push(bad), false);
  for (let i = 0; i < 8; i++) m.push(16.0);
  m.push(33.0);    // a dropped frame: > 1.5 x median
  m.push(5.0);     // a short one: < 0.5 x median
  close(m.hz(), 62.5);
  // the ring keeps only the last 10
  for (let i = 0; i < 10; i++) m.push(20.0);
  close(m.hz(), 50.0);
});

test("median of odd, even and empty inputs", () => {
  assert.equal(C.median([3, 1, 2]), 2);
  assert.equal(C.median([4, 1, 2, 3]), 2.5);
  assert.ok(Number.isNaN(C.median([])));
  assert.equal(C.median([9, 1, 5, 7], 2), 5);
});

// ------------------------------------------------------------------ helpers

test("frameIndex rounds to the nearest vsync and skips an index on a dropped frame", () => {
  const p = 1000 / 60;
  assert.equal(C.frameIndex(0, 0, p), 0);
  assert.equal(C.frameIndex(16.6, 0, p), 1);
  assert.equal(C.frameIndex(16.7 * 3, 0, p), 3);
  assert.equal(C.frameIndex(1000 + 8.4, 1000, p), 1);
  assert.equal(C.frameIndex(1000 + 8.2, 1000, p), 0);
  assert.equal(C.isDropped(2 * p, p), true);
  assert.equal(C.isDropped(1.4 * p, p), false);
});

test("rectToPx scales normalized rects and rejects bad input", () => {
  assert.deepEqual(C.rectToPx({ x: 0.5, y: 0.25, w: 0.1, h: 0.2 }, 1000, 800),
                   { x: 500, y: 200, w: 100, h: 160 });
  assert.deepEqual(C.rectToPx({ x: 0, y: 0, w: 1, h: 1 }, 0, 0), { x: 0, y: 0, w: 0, h: 0 });
  assert.throws(() => C.rectToPx(null, 10, 10), TypeError);
  assert.throws(() => C.rectToPx({ x: 0, y: 0, w: NaN, h: 1 }, 10, 10), TypeError);
  assert.throws(() => C.rectToPx({ x: 0, y: 0, w: 1, h: "1" }, 10, 10), TypeError);
  assert.throws(() => C.rectToPx({ x: 0, y: 0, w: 1, h: 1 }, -1, 10), RangeError);
  assert.throws(() => C.rectToPx({ x: 0, y: 0, w: 1, h: 1 }, 10, Infinity), RangeError);
});

test("parseMessage accepts JSON objects with a string type only", () => {
  assert.deepEqual(C.parseMessage('{"type":"ping"}'), { msg: { type: "ping" } });
  assert.deepEqual(C.parseMessage({ type: "x" }), { msg: { type: "x" } });
  assert.match(C.parseMessage("{oops").error, /not JSON/);
  assert.match(C.parseMessage("[1]").error, /not a JSON object/);
  assert.match(C.parseMessage("null").error, /not a JSON object/);
  assert.match(C.parseMessage('{"type":3}').error, /type/);
  assert.match(C.parseMessage(new ArrayBuffer(4)).error, /binary/);
  assert.match(C.parseMessage(new Uint8Array(2)).error, /binary/);
});

// ------------------------------------------------------------------ FlickerState

const PHASE_RUN = { type: "phase", phase: "run", flicker: true, seconds: null, text: "" };
const PHASE_CAL = { type: "phase", phase: "calibrate", flicker: false, seconds: 10, text: "relax" };
const WELCOME = { type: "welcome", protocol: 3, role: "controller", mode: "slots", fake: true,
                  waveform: "sine", refresh_hz: 60.0, refresh_tolerance_hz: 1.0,
                  freqs: [15, 17, 12, 20], max_slots: 4, detect: false };
const layoutMsg = (id, targets) => ({
  type: "layout", layout_id: id, stage: "slots",
  targets: targets || [
    { slot: "yes", freq: 15.0, phase: 0.0, label: "yes", kind: "slot", rects: [] },
    { slot: "no", freq: 17.0, phase: 0.0, label: "no", kind: "slot", rects: [] }],
});

function liveState(opts) {
  const s = new C.FlickerState({ wall: (t) => 1_700_000_000 + t / 1000, ...opts });
  s.accept();
  s.setOpen(true);
  s.handle(WELCOME);
  s.handle(PHASE_RUN);
  s.handle(layoutMsg(7));
  return s;
}

test("rule 1: flicker only when every condition holds (all 64 combinations)", () => {
  const conds = ["accepted", "open", "flickerPhase", "targets", "notPaused", "phaseKnown"];
  for (let mask = 0; mask < 1 << conds.length; mask++) {
    const on = Object.fromEntries(conds.map((c, i) => [c, Boolean(mask & (1 << i))]));
    const s = new C.FlickerState({ wall: () => 0 });
    s.handle(WELCOME);
    if (on.accepted) s.accept();
    s.setOpen(on.open);
    if (on.phaseKnown) s.handle(on.flickerPhase ? PHASE_RUN : PHASE_CAL);
    s.handle(layoutMsg(1, on.targets ? undefined : []));
    if (!on.notPaused) s.pause();
    const want = on.accepted && on.open && on.phaseKnown && on.flickerPhase && on.targets &&
      on.notPaused;
    assert.equal(s.shouldFlicker(), want, JSON.stringify(on));
    const lum = s.luminance({ freq: 15, phase: Math.PI / 2 }, 1000);
    if (!want) assert.equal(lum, C.STILL_LUM, JSON.stringify(on));
    else assert.equal(lum, 1.0);   // frame 0 at phase pi/2: sin = 1
    if (!want) assert.equal(s.onsetDue(1000), null);
  }
});

test("rule 1: flicker needs phase.flicker to be exactly true", () => {
  for (const flicker of [1, "true", null, undefined, false]) {
    const s = liveState();
    s.handle({ type: "phase", phase: "run", flicker });
    assert.equal(s.shouldFlicker(), false, String(flicker));
  }
  const s = liveState();
  s.handle({ type: "layout", layout_id: 8, stage: "idle" });   // no targets array
  assert.equal(s.shouldFlicker(), false);
  s.handle({ type: "layout", layout_id: 9, stage: "slots", targets: "nope" });
  assert.equal(s.shouldFlicker(), false);
});

test("rule 1: a closed socket stops at once and forgets phase and layout", () => {
  const s = liveState();
  assert.equal(s.luminance({ freq: 15, phase: Math.PI / 2 }, 0), 1);
  s.setOpen(false);
  assert.equal(s.shouldFlicker(), false);
  assert.equal(s.t0, null);
  assert.equal(s.luminance({ freq: 15, phase: Math.PI / 2 }, 16.7), 0.5);
  // the reopened socket must not flicker on the old phase and layout before catch-up
  s.setOpen(true);
  assert.equal(s.shouldFlicker(), false);
  assert.equal(s.phase, null);
  assert.equal(s.layout, null);
  s.handle(PHASE_RUN);
  assert.equal(s.shouldFlicker(), false);   // still no layout
  s.handle(layoutMsg(7));
  assert.equal(s.shouldFlicker(), true);
});

test("rule 1: Esc pause is local and sticky until resume, even if the server says run", () => {
  const s = liveState();
  s.pause();
  assert.equal(s.shouldFlicker(), false);
  s.handle(PHASE_RUN);
  s.handle(layoutMsg(8));
  assert.equal(s.shouldFlicker(), false);
  s.resume();
  assert.equal(s.shouldFlicker(), true);
});

test("rule 1: a phase with flicker false between two frames restarts the frame count", () => {
  const s = liveState();
  const p = 1000 / 60;
  s.luminance({ freq: 15, phase: 0 }, 1000);
  assert.equal(s.frame(1000 + 3 * p), 3);
  s.handle(PHASE_CAL);
  s.handle(PHASE_RUN);       // no frame drawn while still
  assert.equal(s.frame(1000 + 10 * p), 0);
});

test("onset: once per layout message, on its first flickering frame", () => {
  const s = new C.FlickerState({ wall: (t) => 1_700_000_000 + t / 1000 });
  s.accept();
  s.setOpen(true);
  s.handle(WELCOME);
  s.handle(layoutMsg(7));
  assert.equal(s.onsetDue(100), null);      // no phase yet
  s.handle(PHASE_CAL);
  assert.equal(s.onsetDue(116.7), null);    // calibrating: still
  s.handle(PHASE_RUN);
  const first = s.onsetDue(133.3);
  assert.deepEqual(first, { type: "onset", layout_id: 7, t_client: 1_700_000_000 + 0.1333 });
  for (let i = 1; i < 300; i++) assert.equal(s.onsetDue(133.3 + i * 16.7), null);
  // pause / resume in the same layout: no second onset
  s.pause();
  s.resume();
  assert.equal(s.onsetDue(10000), null);
  // a new layout (even a catch-up with the same id) gets its own onset, once
  s.handle(layoutMsg(8));
  const second = s.onsetDue(20000);
  assert.equal(second.layout_id, 8);
  close(second.t_client, 1_700_000_020);
  assert.equal(s.onsetDue(20016.7), null);
  s.handle(layoutMsg(8));
  assert.equal(s.onsetDue(30000).layout_id, 8);
});

test("onset: t_client is the first flickering frame even when asked later", () => {
  const s = liveState();
  s.luminance({ freq: 15, phase: 0 }, 500);
  s.luminance({ freq: 15, phase: 0 }, 516.7);
  const o = s.onsetDue(533.3);
  close(o.t_client, 1_700_000_000.5);
});

test("onset: a reconnect re-arms it with the catch-up layout", () => {
  const s = liveState();
  assert.ok(s.onsetDue(0));
  s.setOpen(false);
  s.setOpen(true);
  s.handle(PHASE_RUN);
  s.handle(layoutMsg(7));
  assert.equal(s.onsetDue(5000).layout_id, 7);
});

test("onset: default wall clock uses performance.timeOrigin", () => {
  const s = new C.FlickerState();
  s.accept(); s.setOpen(true); s.handle(PHASE_RUN); s.handle(layoutMsg(1));
  const o = s.onsetDue(performance.now());
  assert.ok(Math.abs(o.t_client - Date.now() / 1000) < 1, String(o.t_client));
});

test("luminance equals flicker.js frame by frame, and the Python fixture", () => {
  // Protocol v3 has no modulation depth: FlickerState always draws the full flicker, so only
  // the fixture's depth-1 cases apply (a case without a depth predates the knob).
  for (const c of FIXTURE.cases.filter((c) => (c.depth ?? 1) === 1)) {
    const s = liveState();
    s.handle({ ...WELCOME, waveform: c.waveform, refresh_hz: c.refresh });
    const target = { freq: c.freq, phase: c.phase };
    const p = 1000 / c.refresh;
    const t0 = 12345.6;
    for (let n = 0; n < c.values.length; n++) {
      // rAF jitter of +-3 ms must not change the frame index
      const t = t0 + n * p + (n === 0 ? 0 : ((n * 5) % 7 - 3));
      const got = s.luminance(target, t);
      assert.equal(got, F.luminance(c.freq, c.phase, n, c.refresh, c.waveform), `${c.freq} ${n}`);
      close(got, c.values[n], 1e-12);
    }
  }
});

test("luminance: a dropped frame skips an index instead of shifting the phase", () => {
  const s = liveState();
  const p = 1000 / 60;
  const tg = { freq: 17, phase: 0 };
  s.luminance(tg, 0);
  s.luminance(tg, p);
  assert.equal(s.dropped, 0);
  assert.equal(s.luminance(tg, 3 * p), F.luminance(17, 0, 3, 60, "sine"));   // frame 2 dropped
  assert.equal(s.dropped, 1);
  assert.equal(s.luminance(tg, 4 * p), F.luminance(17, 0, 4, 60, "sine"));
});

test("luminance: at the welcome's refresh while the measured one is within its tolerance", () => {
  const s = liveState();
  s.setMeasuredHz(60.5);
  const tg = { freq: 15, phase: 0 };
  const p = 1000 / 60;
  s.luminance(tg, 0);
  assert.equal(s.periodMs(), p);                    // frames counted at the luminance's rate
  assert.equal(s.frame(3 * p), 3);
  assert.equal(s.lumHz(), 60);
  assert.equal(s.luminance(tg, 3 * p), F.luminance(15, 0, 3, 60, "sine"));
});

test("luminance: a re-measured refresh within the tolerance does not jump the phase", () => {
  // RefreshMeter gives a new estimate every ~2 s; five minutes into one layout each one used to
  // rescale the frame index (n = elapsed * measured) and move a 15 Hz phase by cycles.
  const s = liveState();
  const tg = { freq: 15, phase: 0 };
  s.setMeasuredHz(60.03);
  s.luminance(tg, 0);
  const t = 300000;                                 // 5 min, still frame 18000 of the layout
  const before = s.frame(t);
  s.setMeasuredHz(59.97);
  assert.equal(s.frame(t), before);
  assert.equal(before, 18000);
  assert.equal(s.luminance(tg, t + 1000 / 60), F.luminance(15, 0, 18001, 60, "sine"));
  // 15 Hz in time: 15 rising crossings per second at a 60 Hz display measured as 59.97-60.03
  let prev = null, ups = 0;
  for (let n = 0; n <= 600; n++) {
    s.setMeasuredHz(n % 120 < 60 ? 59.97 : 60.03);
    const v = s.luminance(tg, t + 2 * 1000 / 60 + n * 1000 / 60) - 0.5;
    if (prev !== null && prev < 0 && v >= 0) ups++;
    prev = v;
  }
  assert.equal(ups, 150);
});

test("luminance: an off-rate display (--ignore-refresh) flickers at the true frequencies", () => {
  // Computed at 60 Hz, a 75 Hz display would show 12/15/17/20 Hz as 15/18.75/21.25/25 Hz.
  for (const [measured, freq] of [[75, 12], [75, 20], [120, 20], [50, 15], [144, 17]]) {
    const s = liveState();
    s.setMeasuredHz(measured);
    assert.equal(s.lumHz(), measured);
    const p = 1000 / measured;
    const seconds = 10;
    let prev = null, ups = 0;
    for (let n = 0; n <= seconds * measured; n++) {
      const v = s.luminance({ freq, phase: 0 }, n * p) - 0.5;
      if (prev !== null && prev < 0 && v >= 0) ups++;
      prev = v;
    }
    assert.ok(Math.abs(ups / seconds - freq) <= 0.15, `${freq} Hz at ${measured} Hz shows ${ups / seconds} Hz`);
  }
});

test("luminance: an accepted 120 Hz display shows 15 Hz as 8 frames per cycle", () => {
  const s = liveState();
  s.handle({ ...WELCOME, refresh_rates: [60, 90, 120, 144] });
  assert.deepEqual(s.refreshRates, [60, 90, 120, 144]);
  s.setMeasuredHz(119.97);
  assert.equal(s.lumHz(), 119.97);                  // not the nominal 60: measured is used
  const p = 1000 / 119.97;
  const tg = { freq: 15, phase: 0 };
  const lum = [];
  for (let n = 0; n < 24; n++) {
    const t = n * p + (n === 0 ? 0 : ((n * 3) % 5 - 2) * 0.5);   // +-1 ms of rAF jitter
    assert.equal(s.frame(t), n);
    lum.push(s.luminance(tg, t));
    assert.equal(lum[n], F.luminance(15, 0, n, 119.97, "sine"));
  }
  // one cycle of 15 Hz spans 8 frames at 120 Hz (4 at 60): the wave repeats every 8 frames
  for (let n = 0; n + 8 < lum.length; n++) close(lum[n], lum[n + 8], 2e-3);
  close(lum[0], 0.5); close(lum[2], 1.0, 1e-3); close(lum[4], 0.5, 2e-3); close(lum[6], 0.0, 1e-3);
  const exact = liveState();
  exact.setMeasuredHz(120);
  exact.luminance(tg, 0);
  for (let n = 0; n < 16; n++) {
    close(exact.luminance(tg, n * 1000 / 120), 0.5 * (1 + Math.sin(2 * Math.PI * n / 8)), 1e-12);
  }
});

test("welcome without refresh_rates (an older server) keeps the nominal rate only", () => {
  const s = new C.FlickerState();
  s.handle({ ...WELCOME, refresh_hz: 60 });
  assert.deepEqual(s.refreshRates, [60]);
  s.handle({ ...WELCOME, refresh_rates: ["120", -1, NaN] });
  assert.deepEqual(s.refreshRates, [60]);
});

test("luminance: a target at or above half the refresh is drawn still instead of aliasing", () => {
  const s = liveState();
  s.setMeasuredHz(30);                              // a 30 Hz display: 20 Hz would show as 10 Hz
  s.luminance({ freq: 12 }, 0);
  for (let n = 1; n < 10; n++) assert.equal(s.luminance({ freq: 20 }, n * 1000 / 30), 0.5);
  assert.notEqual(s.luminance({ freq: 12 }, 1000 / 30), 0.5);
  s.setMeasuredHz(NaN);
  assert.equal(s.measuredHz, null);
  s.setMeasuredHz(-1);
  assert.equal(s.measuredHz, null);
});

test("luminance: square waveform, missing phase, unknown waveform kept as sine", () => {
  const s = liveState();
  s.handle({ ...WELCOME, waveform: "triangle", refresh_hz: -3 });
  assert.equal(s.waveform, "sine");
  assert.equal(s.refreshHz, 60);
  s.handle({ ...WELCOME, waveform: "square" });
  assert.equal(s.luminance({ freq: 15 }, 0), 1.0);
  assert.equal(s.luminance({ freq: 15 }, 1000 / 60 * 2), 0.0);
});

test("frames: every 5 s while flickering, with the dropped count", () => {
  const s = liveState();
  s.setMeasuredHz(59.98123);
  const p = 1000 / 60;
  let t = 0;
  for (; t < 5000 - p / 2; t += p) assert.equal(s.framesDue(t), null);
  assert.deepEqual(s.framesDue(5000), { type: "frames", refresh_hz: 59.981, dropped: 0 });
  assert.equal(s.framesDue(5000 + p), null);
  assert.equal(s.framesDue(5000 + 3 * p), null);   // one frame missed
  assert.deepEqual(s.framesDue(10000 + 3 * p), { type: "frames", refresh_hz: 59.981, dropped: 2 });
  s.pause();
  assert.equal(s.framesDue(20000), null);
});

test("handle ignores junk and unrelated messages", () => {
  const s = liveState();
  for (const m of [null, undefined, 3, "x", { type: "selection", slot: "yes" }, { type: "status" }]) {
    s.handle(m);
  }
  assert.equal(s.shouldFlicker(), true);
});

// ------------------------------------------------------------------ Connection

function makeConn(extra = {}) {
  const { FakeWebSocket, sockets } = makeFakeWS();
  const clock = makeFakeTimers();
  const notes = [];
  const conn = new C.Connection({
    url: "ws://127.0.0.1:8765/ws", token: "s3cret-token",
    hello: { role: "controller", mode: "slots", slots: ["yes", "no"], refresh_hz: 59.98,
             accepted: true, takeover: false, client: { name: "t", kind: "test", version: "0" },
             viewport: { w: 800, h: 600, dpr: 2 } },
    WebSocket: FakeWebSocket, timers: clock.timers, now: clock.now,
    log: (s) => notes.push(s), ...extra,
  });
  const states = [];
  const closes = [];
  conn.on("state", (s) => states.push(s));
  conn.on("close", (c) => closes.push(c));
  return { conn, sockets, clock, states, closes, notes, FakeWebSocket };
}

test("Connection: constructor errors say how to fix them", () => {
  const { FakeWebSocket } = makeFakeWS();
  assert.throws(() => new C.Connection({ url: "http://x/ws", WebSocket: FakeWebSocket }), /ws:\/\//);
  assert.throws(() => new C.Connection({ WebSocket: FakeWebSocket }), /url/);
  assert.throws(() => new C.Connection({ url: "ws://x/ws", token: 5, WebSocket: FakeWebSocket }), /token/);
  const saved = globalThis.WebSocket;
  try {
    globalThis.WebSocket = undefined;
    assert.throws(() => new C.Connection({ url: "ws://x/ws" }), /WebSocket/);
  } finally {
    globalThis.WebSocket = saved;
  }
});

test("Connection: hello on open has the protocol, token and the given fields", () => {
  const { conn, sockets, states } = makeConn();
  conn.open();
  assert.equal(sockets.length, 1);
  assert.equal(sockets[0].url, "ws://127.0.0.1:8765/ws");
  assert.deepEqual(states, ["connecting"]);
  assert.equal(conn.send({ type: "pause" }), false);   // not open yet
  sockets[0].accept();
  assert.deepEqual(states, ["connecting", "open"]);
  assert.deepEqual(sockets[0].sent, [{
    type: "hello", protocol: 3, token: "s3cret-token", role: "controller", mode: "slots",
    slots: ["yes", "no"], refresh_hz: 59.98, accepted: true, takeover: false,
    client: { name: "t", kind: "test", version: "0", instance: conn.instance },
    viewport: { w: 800, h: 600, dpr: 2 } }]);
  assert.match(conn.instance, /^[0-9a-f-]{8,}$/);
});

test("Connection: every hello of one Connection carries the same client.instance", () => {
  const { conn, sockets, clock } = makeConn();
  conn.open();
  sockets[0].accept();
  sockets[0].drop();
  clock.advance(1000);
  sockets[1].accept();
  conn.reconnect();
  sockets[2].accept();
  const ids = sockets.map((s) => s.sent[0].client.instance);
  assert.deepEqual(ids, [conn.instance, conn.instance, conn.instance]);
  assert.notEqual(makeConn().conn.instance, conn.instance);         // another client, another id
  assert.equal(makeConn({ instance: "tab-7" }).conn.instance, "tab-7");
  assert.throws(() => makeConn({ instance: "" }), /instance must be a string/);
  assert.throws(() => makeConn({ instance: "x".repeat(65) }), /instance must be a string/);
});

test("Connection: hello fields from a function are read on every open, and cannot override type/protocol/token", () => {
  let hz = 59.0;
  const { conn, sockets, clock } = makeConn({
    token: null,
    hello: () => ({ type: "nope", protocol: 99, token: "forged", role: "viewer", refresh_hz: hz }),
  });
  conn.open();
  sockets[0].accept();
  assert.deepEqual(sockets[0].sent[0], { type: "hello", protocol: 3, token: null, role: "viewer",
                                         refresh_hz: 59.0, client: { instance: conn.instance } });
  hz = 60.0;
  sockets[0].drop();
  clock.advance(1000);
  sockets[1].accept();
  assert.equal(sockets[1].sent[0].refresh_hz, 60.0);
});

test("Connection: messages go to type listeners and 'message'; junk is noted and dropped", () => {
  const { conn, sockets, notes } = makeConn();
  const got = [];
  const all = [];
  const off = conn.on("selection", (m) => got.push(m.slot));
  conn.on("message", (m) => all.push(m.type));
  conn.on("phase", () => { throw new Error("listener bug"); });
  conn.open();
  sockets[0].accept();
  sockets[0].message({ type: "selection", slot: "yes" });
  sockets[0].message("{nope");
  sockets[0].message("[]");
  sockets[0].message({ type: "phase", phase: "run", flicker: true });
  off();
  sockets[0].message({ type: "selection", slot: "no" });
  assert.deepEqual(got, ["yes"]);
  assert.deepEqual(all, ["selection", "phase", "selection"]);
  assert.equal(notes.filter((n) => /ignored/.test(n)).length, 2);
  assert.ok(notes.some((n) => /listener for phase failed: listener bug/.test(n)));
  assert.ok(!notes.join(" ").includes("s3cret-token"));
});

test("Connection: reconnects after 1, 2, 4, then every 5 s, and resets after an open", () => {
  const { conn, sockets, clock, states } = makeConn();
  conn.open();
  const delays = [];
  for (let i = 0; i < 7; i++) {
    sockets[sockets.length - 1].drop(1006);
    const [ms] = clock.timeouts();
    delays.push(ms);
    assert.equal(conn.state, "closed");
    clock.advance(ms - 1);
    assert.equal(sockets.length, i + 1, "too early");
    clock.advance(1);
    assert.equal(sockets.length, i + 2);
  }
  assert.deepEqual(delays, [1000, 2000, 4000, 5000, 5000, 5000, 5000]);
  sockets[sockets.length - 1].accept();
  sockets[sockets.length - 1].drop(1001);
  assert.deepEqual(clock.timeouts(), [1000]);
  assert.equal(states.filter((s) => s === "open").length, 1);
});

test("Connection: reconnects after 4003, 4008, 1000 and 1011", () => {
  for (const code of [4003, 4008, 1000, 1011, 1009]) {
    const { conn, sockets, clock, closes } = makeConn();
    conn.open();
    sockets[0].accept();
    sockets[0].drop(code);
    assert.deepEqual(closes, [{ code, reason: "", reconnect: true }]);
    clock.advance(1000);
    assert.equal(sockets.length, 2, String(code));
    assert.equal(conn.stopped, false);
  }
});

test("Connection: no reconnect after 4000, 4001 or 4002", () => {
  for (const code of [4000, 4001, 4002]) {
    const { conn, sockets, clock, closes, states } = makeConn();
    conn.open();
    sockets[0].accept();
    sockets[0].message({ type: "error", code: "busy", message: "x", fatal: true });
    sockets[0].drop(code, "replaced");
    assert.deepEqual(closes, [{ code, reason: "replaced", reconnect: false }]);
    assert.equal(conn.stopped, true);
    assert.equal(conn.closeCode, code);
    assert.equal(states.at(-1), "closed");
    assert.deepEqual(clock.timeouts(), []);
    clock.advance(60_000);
    assert.equal(sockets.length, 1, String(code));
    // the user can start again by hand
    conn.open();
    assert.equal(sockets.length, 2);
  }
});

test("Connection: a throwing WebSocket constructor counts as a failed attempt", () => {
  const { conn, sockets, clock, FakeWebSocket, notes } = makeConn();
  FakeWebSocket.throwNext = true;
  conn.open();
  assert.equal(sockets.length, 0);
  assert.equal(conn.state, "closed");
  assert.ok(notes.some((n) => /could not open/.test(n)));
  clock.advance(1000);
  assert.equal(sockets.length, 1);
});

test("Connection: ping every 10 s after welcome, rtt and median clock offset", () => {
  const { conn, sockets, clock } = makeConn();
  conn.open();
  const ws = sockets[0];
  ws.accept();
  clock.advance(30_000);
  assert.deepEqual(ws.types(), ["hello"]);             // no pings before welcome
  ws.message({ type: "welcome", refresh_hz: 60 });
  assert.equal(conn.welcome.refresh_hz, 60);
  let ping = ws.sent.at(-1);
  assert.equal(ping.type, "ping");
  assert.equal(ping.ping_id, 1);
  assert.equal(ping.t_client, clock.now() / 1000);
  // the server clock runs 2.5 s ahead; 40 ms round trip
  clock.advance(40);
  ws.message({ type: "pong", ping_id: 1, t_client: ping.t_client, t_server: ping.t_client + 0.02 + 2.5 });
  assert.equal(conn.rttMs, 40);
  close(conn.offset, 2.5, 1e-6);
  // a stale or unknown pong is ignored
  ws.message({ type: "pong", ping_id: 1, t_server: 0 });
  ws.message({ type: "pong", ping_id: 99, t_server: 0 });
  ws.message({ type: "pong", ping_id: 2, t_server: "x" });
  close(conn.offset, 2.5, 1e-6);
  const offsets = [];
  for (let i = 0; i < 10; i++) {
    clock.advance(10_000 - 40 * (i === 0 ? 1 : 1));
    ping = ws.sent.at(-1);
    assert.equal(ping.type, "ping");
    clock.advance(40);
    const off = i === 3 ? 100 : 2.5;    // one outlier: the median ignores it
    offsets.push(off);
    ws.message({ type: "pong", ping_id: ping.ping_id, t_server: ping.t_client + 0.02 + off });
  }
  close(conn.offset, 2.5, 1e-6);
  assert.equal(ws.types().filter((t) => t === "ping").length, 11);
  // the server's own keepalive ping gets no answer
  const n = ws.sent.length;
  ws.message({ type: "ping" });
  assert.equal(ws.sent.length, n);
});

test("Connection: pings stop when the socket closes", () => {
  const { conn, sockets, clock } = makeConn();
  conn.open();
  sockets[0].accept();
  sockets[0].message({ type: "welcome" });
  sockets[0].drop(4001);
  const before = sockets[0].sent.length;
  clock.advance(60_000);
  assert.equal(sockets[0].sent.length, before);
  assert.equal(conn.ping(), false);
  assert.equal(clock.pending.size, 0);
});

test("Connection: close sends bye, closes 1000 and stops reconnecting", () => {
  const { conn, sockets, clock, closes } = makeConn();
  conn.open();
  sockets[0].accept();
  conn.close();
  assert.deepEqual(sockets[0].types(), ["hello", "bye"]);
  assert.deepEqual(sockets[0].closedWith, { code: 1000, reason: "bye" });
  assert.deepEqual(closes, [{ code: 1000, reason: "bye", reconnect: false }]);
  sockets[0].drop(1000);   // a late onclose from the old socket is ignored
  clock.advance(60_000);
  assert.equal(sockets.length, 1);
  assert.equal(conn.state, "closed");
});

test("Connection: close while a retry is pending cancels it; close without bye", () => {
  const { conn, sockets, clock } = makeConn();
  conn.open();
  sockets[0].drop();
  conn.close();
  clock.advance(60_000);
  assert.equal(sockets.length, 1);
  const b = makeConn();
  b.conn.open();
  b.sockets[0].accept();
  b.conn.close({ bye: false });
  assert.deepEqual(b.sockets[0].types(), ["hello"]);
});

test("Connection: reconnect() opens a fresh socket at once with a new hello", () => {
  const { conn, sockets, clock, closes } = makeConn();
  conn.open();
  sockets[0].accept();
  sockets[0].message({ type: "welcome" });
  conn.reconnect();
  assert.equal(sockets.length, 2);
  assert.deepEqual(sockets[0].closedWith, { code: 1000, reason: "reconnect" });
  assert.deepEqual(closes, [{ code: 1000, reason: "reconnect", reconnect: true }]);
  sockets[0].drop(1000);                 // ignored: not the current socket
  sockets[1].accept();
  assert.equal(sockets[1].sent[0].type, "hello");
  clock.advance(60_000);
  assert.equal(sockets.length, 2);
  // reconnect() after a stop starts again too
  sockets[1].drop(4000);
  conn.reconnect();
  assert.equal(sockets.length, 3);
});

test("Connection: open() twice does not open two sockets; send survives a throwing socket", () => {
  const { conn, sockets, notes } = makeConn();
  conn.open();
  conn.open();
  assert.equal(sockets.length, 1);
  sockets[0].accept();
  sockets[0].send = () => { throw new Error("boom"); };
  assert.equal(conn.send({ type: "pause" }), false);
  assert.ok(notes.some((n) => /send of pause failed/.test(n)));
});

test("Connection: the state events are connecting, open, closed", () => {
  const { conn, sockets, states } = makeConn();
  conn.open();
  sockets[0].accept();
  sockets[0].drop();
  assert.deepEqual(states, ["connecting", "open", "closed"]);
  for (const s of states) assert.ok(C.CONN_STATES.includes(s));
});

// ------------------------------------------------------------------ code mode (c-VEP)

const K = require("../cvep.js");
const CODE = { bits: [...K.BITS], lag_frames: 21, cycle_frames: 63, train_cycles: 30,
               fill: "#ffffff" };
const CODE_WELCOME = { ...WELCOME, paradigm: "code", freqs: [], max_slots: 3, code: CODE };
const PHASE_TRAIN = { type: "phase", phase: "train", flicker: true, seconds: null,
                      text: "Look at the YES square", cue: "yes", cycles: 30 };
const codeLayout = (id) => ({
  type: "layout", layout_id: id, stage: "slots",
  targets: ["yes", "no", "help"].map((s, i) => (
    { slot: s, freq: null, lag: 21 * i, phase: 0.0, label: s, kind: "slot", rects: [] })),
});
const PERIOD = 1000 / 60;

function codeState(phase) {
  const s = new C.FlickerState({ wall: (t) => 1_700_000_000 + t / 1000 });
  s.accept();
  s.setOpen(true);
  s.handle(CODE_WELCOME);
  s.handle(phase || PHASE_RUN);
  s.handle(codeLayout(7));
  return s;
}

test("code: the SDK exposes cvep.js and the code helpers", () => {
  assert.equal(C.code, K);
  assert.equal(C.CODE_STILL_LUM, 1);
  assert.equal(typeof C.ConfidenceState, "function");
  for (let n = -70; n < 200; n++) {
    for (const lag of [0, 21, 42]) assert.equal(C.codeBit(K.BITS, n, lag), K.bit(lag / 21, n));
  }
});

test("code: luminance is the m-sequence bit of each lag, white still, frame by frame", () => {
  const s = codeState();
  assert.ok(s.isCode() && s.shouldFlicker());
  const ts = s.layout.targets;
  for (let n = 0; n < 200; n++) {
    const t = 1000 + n * PERIOD;
    for (let k = 0; k < 3; k++) assert.equal(s.luminance(ts[k], t), K.bit(k, n), `n=${n} k=${k}`);
  }
  s.pause();
  assert.equal(s.luminance(ts[1], 5000), 1);            // still: white
  const off = codeState({ ...PHASE_CAL });
  assert.equal(off.luminance(ts[0], 0), 1);
  assert.equal(codeState().luminance({ slot: "x", lag: -1 }, 0), 1);   // no valid lag: still
});

test("code: the train phase flickers and names the cue; run does not", () => {
  const s = codeState(PHASE_TRAIN);
  assert.ok(s.shouldFlicker());
  assert.equal(s.cue(), "yes");
  s.handle(PHASE_RUN);
  assert.equal(s.cue(), null);
  assert.equal(liveState().isCode(), false);
});

test("code: a cycle message on every cycle's first flip, with that flip's time", () => {
  const s = codeState();
  const sent = [];
  for (let n = 0; n <= 3 * 63; n++) sent.push(...s.cycleDue(1000 + n * PERIOD));
  assert.deepEqual(sent.map((m) => m.n), [0, 1, 2, 3]);
  for (const m of sent) {
    assert.deepEqual(Object.keys(m), ["type", "layout_id", "n", "t_client"]);
    assert.equal(m.type, "cycle");
    assert.equal(m.layout_id, 7);
    close(m.t_client, 1_700_000_000 + (1000 + m.n * 63 * PERIOD) / 1000, 1e-6);
  }
  assert.deepEqual(s.cycleDue(1000 + 3 * 63 * PERIOD), []);   // the same frame again: nothing
});

test("code: a dropped first frame still reports its cycle at the due time; a gap does not", () => {
  const s = codeState();
  s.cycleDue(1000);                                           // cycle 0 at the onset
  const late = s.cycleDue(1000 + 64 * PERIOD);                // frame 63 dropped, 64 drawn
  assert.equal(late.length, 1);
  close(late[0].t_client, 1_700_000_000 + (1000 + 63 * PERIOD) / 1000, 1e-6);
  const gap = s.cycleDue(1000 + 200 * PERIOD);                // a stall past cycle 2's start
  assert.deepEqual(gap, []);
  assert.equal(s.missedCycles, 2);                           // cycles 2 and 3
  assert.equal(s.cycleDue(1000 + 252 * PERIOD)[0].n, 4);
});

test("code: a new layout, a pause or a socket drop restarts the cycle count", () => {
  const s = codeState();
  for (let n = 0; n < 70; n++) s.cycleDue(1000 + n * PERIOD);
  s.handle(codeLayout(8));
  const after = s.cycleDue(5000);
  assert.deepEqual(after.map((m) => [m.layout_id, m.n]), [[8, 0]]);
  s.pause();
  assert.deepEqual(s.cycleDue(5100), []);
  s.resume();
  assert.deepEqual(s.cycleDue(5200).map((m) => m.n), [0]);
  s.setOpen(false);
  assert.deepEqual(s.cycleDue(5300), []);
  assert.deepEqual(liveState().cycleDue(1000), []);          // freq mode: never
});

test("code: a malformed code object falls back to the standard's", () => {
  const s = new C.FlickerState({ wall: () => 0 });
  s.handle({ ...CODE_WELCOME, code: { bits: [0, 2], lag_frames: 21, cycle_frames: 2 } });
  assert.deepEqual(s.code.bits, [...K.BITS]);
  assert.equal(s.code.lag_frames, 21);
  s.handle({ ...CODE_WELCOME, code: { ...CODE, bits: [1, 0, 1], cycle_frames: 3, fill: "" } });
  assert.deepEqual(s.code, { bits: [1, 0, 1], lag_frames: 21, cycle_frames: 3,
                             train_cycles: 30, fill: "#ffffff" });
  s.handle(WELCOME);
  assert.equal(s.isCode(), false);
  assert.equal(s.code, null);
});

test("ConfidenceState: training text, label lines, bars and the status line", () => {
  const c = new C.ConfidenceState();
  c.handle(CODE_WELCOME);
  c.handle(codeLayout(7));
  c.handle(PHASE_TRAIN);
  assert.equal(c.status(), "training: keep looking at YES   cycle 0/30");
  c.handle({ type: "train", cycle: 12, of: 30 });
  assert.equal(c.status(), "training: keep looking at YES   cycle 12/30");
  assert.equal(c.trainedText(), "");
  c.handle({ type: "trained", accuracy: 0.83, chance: 0.333, cycles: 30 });
  c.handle(PHASE_RUN);
  assert.equal(c.status(), "template ready: 83 % on held-out cycles (chance 33 %)");
  assert.deepEqual(c.lines().map((l) => l.line),
                   ["YES  ░░░░░░░░   -  ", "NO   ░░░░░░░░   -  ", "HELP ░░░░░░░░   -  "]);
  c.handle({ type: "decision", layout_id: 6, scores: { yes: 0.9 }, raw: "yes",
             committed: "yes", lead: 0.5, changed: true });           // another layout
  assert.equal(c.decision, null);
  c.handle({ type: "decision", layout_id: 7, scores: { yes: 0.42, no: 0.11, help: -0.03 },
             raw: "yes", committed: "yes", lead: 0.31, changed: false });
  assert.deepEqual(c.lines().map((l) => l.line),
                   ["YES  ███░░░░░ +0.42 <", "NO   █░░░░░░░ +0.11", "HELP ░░░░░░░░ -0.03"]);
  assert.deepEqual(c.lines().map((l) => [l.slot, l.best, l.level]),
                   [["yes", true, 0.42], ["no", false, 0.11], ["help", false, 0]]);
  assert.equal(c.status(), "lead 0.31   choice: YES");
  c.handle({ type: "decision", layout_id: 7, scores: { yes: 0.1, no: 0.2, help: 0.6 },
             raw: "help", committed: "help", lead: 0.4, changed: true });
  assert.equal(c.status(), "lead 0.40   choice: HELP *");
  c.handle({ type: "decision", layout_id: 7, scores: { yes: 0.1, no: 0.2, help: 0.3 },
             raw: "help", committed: null, lead: null, changed: false });
  assert.equal(c.status(), "lead  -     no choice yet");
  c.handle(codeLayout(8));                                     // a new layout clears it
  assert.equal(c.decision, null);
  c.handle({ type: "trained", accuracy: null, chance: 0.333, cycles: 30 });
  assert.equal(c.trainedText(), "template ready: ? % on held-out cycles (chance 33 %)");
});

test("code: the SDK loads in a browser after flicker.js and cvep.js, and without cvep.js", () => {
  const load = (names) => {
    const ctx = { performance: { timeOrigin: 0 }, Date };
    ctx.globalThis = ctx;
    for (const n of names) {
      vm.runInNewContext(readFileSync(new URL(`../${n}`, import.meta.url), "utf8"), ctx);
    }
    return ctx.BrainSyncClient;
  };
  const full = load(["flicker.js", "cvep.js", "brainsync-client.js"]);
  assert.ok(full.code && new full.ConfidenceState());
  const bare = load(["flicker.js", "brainsync-client.js"]);
  assert.equal(bare.code, null);
  assert.throws(() => new bare.ConfidenceState(), /cvep\.js/);
  const s = new bare.FlickerState({ wall: () => 0 });
  s.accept(); s.setOpen(true);
  s.handle(CODE_WELCOME); s.handle(PHASE_RUN); s.handle(codeLayout(1));
  assert.equal(s.luminance({ lag: 21 }, 0), K.bit(1, 0));    // draws from the welcome's bits
});
