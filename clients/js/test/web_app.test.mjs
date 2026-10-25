// clients/web/app.js without a browser: hash parsing, square geometry (phones included), the
// wake lock, the page model, and the whole page wired to a fake DOM, a fake WebSocket, fake
// timers and a fake navigator.wakeLock.
//     node --test clients/js/test/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { makeFakeWS, makeFakeTimers } from "./fakes.mjs";

const require = createRequire(import.meta.url);
const F = require("../flicker.js");
const W = require("../../web/app.js");

// A navigator.wakeLock that records requests and releases. mode: "ok", "reject" or "throw".
function fakeWakeLock({ mode = "ok" } = {}) {
  const f = { log: [], sentinels: [], mode };
  f.api = {
    request(type) {
      f.log.push(["request", type]);
      if (f.mode === "throw") throw new Error("not allowed");
      if (f.mode === "reject") return Promise.reject(new Error("NotAllowedError"));
      const s = {
        released: false, listeners: {},
        addEventListener(t, cb) { (this.listeners[t] ||= []).push(cb); },
        release() { this.released = true; f.log.push(["release"]); return Promise.resolve(); },
        systemRelease() { this.released = true; for (const cb of this.listeners.release || []) cb({}); },
      };
      f.sentinels.push(s);
      return Promise.resolve(s);
    },
  };
  return f;
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

const WELCOME = { type: "welcome", protocol: 3, role: "controller", mode: "slots", fake: true,
                  waveform: "sine", refresh_hz: 60.0, refresh_tolerance_hz: 1.0,
                  freqs: [15, 17, 12, 20], max_slots: 4, detect: false };
const RUN = { type: "phase", phase: "run", flicker: true, seconds: null, text: "" };
const LAYOUT = { type: "layout", layout_id: 3, stage: "slots", targets: [
  { slot: "yes", freq: 15.0, phase: 0.0, label: "yes", kind: "slot", rects: [] },
  { slot: "no", freq: 17.0, phase: 0.0, label: "no", kind: "slot", rects: [] }] };

// ------------------------------------------------------------------ pure helpers

test("parseHash reads the token and takeover", () => {
  assert.deepEqual(W.parseHash("#token=abc-DEF_1"), { token: "abc-DEF_1", takeover: false });
  assert.deepEqual(W.parseHash("#token=a%2Bb&takeover=1"), { token: "a+b", takeover: true });
  assert.deepEqual(W.parseHash("takeover=true"), { token: null, takeover: true });
  assert.deepEqual(W.parseHash("#token="), { token: null, takeover: false });
  assert.deepEqual(W.parseHash(""), { token: null, takeover: false });
  assert.deepEqual(W.parseHash(undefined), { token: null, takeover: false });
  assert.deepEqual(W.parseHash("#takeover=0&token=x"), { token: "x", takeover: false });
});

test("wsUrlFor follows the page's scheme and host", () => {
  assert.equal(W.wsUrlFor({ protocol: "http:", host: "127.0.0.1:8765" }), "ws://127.0.0.1:8765/ws");
  assert.equal(W.wsUrlFor({ protocol: "https:", host: "box.tail.ts.net" }), "wss://box.tail.ts.net/ws");
});

test("squareLayout centres n squares that fit and never overlap", () => {
  for (const [w, h] of [[1512, 857], [390, 844], [3000, 600], [100, 100]]) {
    for (const n of [1, 2, 3, 4]) {
      const sq = W.squareLayout(n, w, h);
      assert.equal(sq.length, n);
      for (const s of sq) {
        assert.ok(s.side > 0 && s.side <= 0.4 * h + 1e-9);
        assert.ok(s.x >= 0 && s.x + s.side <= w + 1e-9, `${w}x${h} n=${n}`);
        assert.ok(s.y >= 0 && s.y + s.side <= h + 1e-9);
      }
      for (let i = 1; i < n; i++) assert.ok(sq[i].x >= sq[i - 1].x + sq[i - 1].side);
      const left = sq[0].x, right = w - (sq[n - 1].x + sq[n - 1].side);
      assert.ok(Math.abs(left - right) < 1e-6);
    }
  }
  assert.deepEqual(W.squareLayout(0, 100, 100), []);
  assert.deepEqual(W.squareLayout(2, 0, 100), []);
});

test("hitTest finds the square under the pointer, edges included", () => {
  const sq = [{ x: 10, y: 10, side: 20 }, { x: 40, y: 10, side: 20 }];
  assert.equal(W.hitTest(sq, 10, 10), 0);
  assert.equal(W.hitTest(sq, 30, 30), 0);
  assert.equal(W.hitTest(sq, 35, 20), -1);
  assert.equal(W.hitTest(sq, 50, 20), 1);
  assert.equal(W.hitTest(sq, 50, 31), -1);
  assert.equal(W.hitTest([], 0, 0), -1);
});

test("helloFields is a slots-mode controller hello with the measured refresh", () => {
  const h = W.helloFields(59.98234, { w: 800, h: 600, dpr: 2 }, true);
  assert.deepEqual(h, {
    role: "controller", takeover: true, client: { name: "brainsync-web", kind: "web", version: "0.1.0" },
    mode: "slots", slots: ["yes", "no"], refresh_hz: 59.982, accepted: true,
    viewport: { w: 800, h: 600, dpr: 2 } });
  h.slots.push("x");
  assert.deepEqual(W.SLOTS, ["yes", "no"]);
  assert.equal(W.helloFields(60, null, undefined).takeover, false);
});

// The squares must fit inside the viewport minus the inset, centred, without overlapping.
function assertFits(sq, w, h, ins, what) {
  const [l, t, r, b] = [ins.left || 0, ins.top || 0, ins.right || 0, ins.bottom || 0];
  for (const s of sq) {
    assert.ok(s.side > 0, what);
    assert.ok(s.x >= l - 1e-9 && s.x + s.side <= w - r + 1e-9, what);
    assert.ok(s.y >= t - 1e-9 && s.y + s.side <= h - b + 1e-9, what);
  }
  for (let i = 0; i < sq.length; i++) {
    for (let j = i + 1; j < sq.length; j++) {
      const a = sq[i], c = sq[j];
      assert.ok(a.x + a.side < c.x || c.x + c.side < a.x || a.y + a.side < c.y || c.y + c.side < a.y,
                what + ` squares ${i} and ${j} overlap`);
    }
  }
}

test("squareLayout without opts is the original one-row layout", () => {
  for (const [w, h] of [[1512, 857], [390, 844], [844, 390], [100, 100]]) {
    for (const n of [1, 2, 3, 4]) {
      const side = Math.min(0.4 * h, (0.9 * w) / (n + 0.5 * (n - 1)));
      const x0 = (w - (n * side + (n - 1) * 0.5 * side)) / 2;
      const y = Math.max(0, (h - side) / 2 - 0.04 * h);
      W.squareLayout(n, w, h).forEach((s, i) => {
        assert.ok(Math.abs(s.side - side) < 1e-9 && Math.abs(s.y - y) < 1e-9);
        assert.ok(Math.abs(s.x - (x0 + i * 1.5 * side)) < 1e-9);
      });
      assert.deepEqual(W.squareLayout(n, w, h, { stack: false }), W.squareLayout(n, w, h));
    }
  }
});

test("squareLayout stacks a column on a portrait phone, sized from the width", () => {
  // iPhone 15, a small Android, a big iPhone, a Pixel; the HUD on top, the dock below
  for (const [w, h] of [[390, 844], [360, 640], [430, 932], [412, 915]]) {
    const inset = { top: 0.18 * h, right: 0, bottom: 0.22 * h, left: 0 };
    const aw = w, ah = h - inset.top - inset.bottom;
    for (const n of [1, 2, 3, 4]) {
      const what = `${w}x${h} n=${n}`;
      const sq = W.squareLayout(n, w, h, { inset, stack: true });
      assert.equal(sq.length, n, what);
      assertFits(sq, w, h, inset, what);
      for (const s of sq) {
        assert.ok(Math.abs(s.x + s.side / 2 - w / 2) < 1e-6, what);   // one centred column
        assert.ok(s.side <= 0.6 * aw + 1e-9, what);
      }
      for (let i = 1; i < n; i++) {
        assert.ok(Math.abs(sq[i].y - sq[i - 1].y - 1.5 * sq[0].side) < 1e-6, what);   // half a side apart
      }
      const above = sq[0].y - inset.top, below = h - inset.bottom - (sq[n - 1].y + sq[n - 1].side);
      assert.ok(Math.abs(above - below) < 1e-6, what);
      // never smaller than the same squares in a row would be
      const row = W.squareLayout(n, w, h, { inset });
      assert.ok(sq[0].side >= row[0].side - 1e-9, what);
    }
    // two squares on a phone: a usable size, not the 0.9 w / 2.5 of a row
    const two = W.squareLayout(2, w, h, { inset, stack: true });
    assert.ok(two[0].side > (0.9 * aw) / 2.5, `${w}x${h}`);
    assert.ok(two[0].side <= 0.9 * ah / 2.5 + 1e-9);
  }
  // a portrait iPad whose space left over is wider than tall keeps the row
  const inset = { top: 180, right: 0, bottom: 230, left: 0 };
  const pad = W.squareLayout(2, 768, 1024, { inset, stack: true });
  assert.deepEqual(pad, W.squareLayout(2, 768, 1024, { inset }));
  assert.equal(pad[0].y, pad[1].y);
});

test("squareLayout keeps a row in landscape and honours the insets", () => {
  for (const [w, h] of [[844, 390], [1512, 857], [1024, 768]]) {
    const inset = { top: 60, right: 47, bottom: 90, left: 47 };   // a landscape notch
    for (const n of [1, 2, 3, 4]) {
      const what = `${w}x${h} n=${n}`;
      const sq = W.squareLayout(n, w, h, { inset, stack: true });
      assert.equal(sq.length, n);
      assertFits(sq, w, h, inset, what);
      assert.ok(sq.every((s) => s.y === sq[0].y), what);
      const left = sq[0].x - inset.left, right = w - inset.right - (sq[n - 1].x + sq[n - 1].side);
      assert.ok(Math.abs(left - right) < 1e-6, what);
      assert.ok(sq[0].side <= Math.min(0.4 * h, 0.8 * (h - inset.top - inset.bottom)) + 1e-9, what);
      assert.ok(sq[0].side <= W.squareLayout(n, w, h)[0].side + 1e-9, what);   // never bigger
    }
  }
  assert.deepEqual(W.squareLayout(2, 400, 800, { inset: { top: 500, bottom: 300 } }), []);   // no room
  assert.deepEqual(W.squareLayout(2, 400, 800, { inset: { left: 400 } }), []);
  // nonsense insets count as 0
  assert.deepEqual(W.squareLayout(2, 800, 400, { inset: { top: -5, left: NaN, right: "9" } }),
                   W.squareLayout(2, 800, 400));
});

test("squareLayout fills a landscape phone's strip between the HUD and the buttons", () => {
  // iPhone 15 on its side: notch 47 px each side, HUD down to 114, buttons and label from
  // 247.6. Capped at 0.4 of that strip the squares were 53 px; now 0.8 of it, about 107 px.
  const inset = { top: 114, right: 47, bottom: 142.4, left: 47 };
  const ah = 390 - inset.top - inset.bottom;
  for (const n of [1, 2, 3, 4]) {
    const sq = W.squareLayout(n, 844, 390, { inset, stack: true });
    assertFits(sq, 844, 390, inset, `n=${n}`);
    assert.ok(Math.abs(sq[0].side - 0.8 * ah) < 1e-9, `n=${n} side ${sq[0].side}`);
    assert.ok(sq[0].side > 100);
  }
  // a short landscape window (Android split screen) is limited by its width instead
  const wide = W.squareLayout(4, 568, 320, { inset: { top: 40, bottom: 60 } });
  assert.ok(Math.abs(wide[0].side - Math.min(0.4 * 320, 0.8 * 220, (0.9 * 568) / 5.5)) < 1e-9);
});

test("layoutInsets keeps clear of the safe area, the HUD and the dock", () => {
  const rect = (left, top, right, bottom) => ({ left, top, right, bottom, width: right - left, height: bottom - top });
  const M = W.LAYOUT_MARGIN;
  assert.deepEqual(W.layoutInsets(390, 844, null, [], []), { top: 0, right: 0, bottom: 0, left: 0 });
  const safe = rect(0, 47, 390, 810);
  const hud = rect(12, 59, 300, 170);
  const dock = rect(0, 700, 390, 844);
  assert.deepEqual(W.layoutInsets(390, 844, safe, [hud], [dock]),
                   { top: 170 + M, right: 0, bottom: 144 + M, left: 0 });
  // hidden elements measure 0 x 0 and are skipped
  const hidden = rect(0, 0, 0, 0);
  assert.deepEqual(W.layoutInsets(390, 844, safe, [hidden, null], [hidden]),
                   { top: 47, right: 0, bottom: 34, left: 0 });
  // landscape with the notch on the left
  assert.deepEqual(W.layoutInsets(844, 390, rect(47, 0, 797, 369), [], []),
                   { top: 0, right: 47, bottom: 21, left: 47 });
});

test("createWakeLock does nothing without the API", () => {
  for (const nav of [undefined, null, {}, { wakeLock: {} }, { wakeLock: { request: 1 } }]) {
    const wl = W.createWakeLock(nav);
    assert.equal(wl.supported, false);
    wl.sync(true);
    wl.sync(false);
    assert.equal(wl.held(), false);
  }
});

test("createWakeLock requests once, releases, and survives refusals", async () => {
  const f = fakeWakeLock();
  const wl = W.createWakeLock({ wakeLock: f.api });
  assert.equal(wl.supported, true);
  wl.sync(true);
  wl.sync(true);                                 // on its way: not asked twice
  await flush();
  assert.deepEqual(f.log, [["request", "screen"]]);
  assert.equal(wl.held(), true);
  wl.sync(true);
  assert.equal(f.log.length, 1);
  wl.sync(false);
  assert.equal(wl.held(), false);
  assert.deepEqual(f.log.at(-1), ["release"]);
  wl.sync(false);
  assert.equal(f.log.length, 2);
  // the browser drops it (page hidden): asked again on the next sync(true)
  wl.sync(true);
  await flush();
  f.sentinels.at(-1).systemRelease();
  assert.equal(wl.held(), false);
  wl.sync(true);
  await flush();
  assert.equal(wl.held(), true);
  assert.equal(f.log.filter((e) => e[0] === "request").length, 3);
  // a lock that arrives after the page stopped wanting it is given back at once
  wl.sync(false);
  wl.sync(true);
  wl.sync(false);
  await flush();
  assert.equal(wl.held(), false);
  assert.equal(f.sentinels.at(-1).released, true);
});

test("createWakeLock does not retry a refusal until the lock was let go once", async () => {
  for (const mode of ["reject", "throw"]) {
    const f = fakeWakeLock({ mode });
    const wl = W.createWakeLock({ wakeLock: f.api });
    wl.sync(true);
    await flush();
    for (let i = 0; i < 5; i++) { wl.sync(true); await flush(); }
    assert.equal(f.log.length, 1, mode);
    assert.equal(wl.held(), false);
    wl.sync(false);
    f.mode = "ok";
    wl.sync(true);
    await flush();
    assert.equal(wl.held(), true, mode);
  }
});

test("createWakeLock asks again on resume when a request is refused after the page let go", async () => {
  // run starts and asks; the phone locks before the answer; the answer is no (hidden page)
  const log = [];
  let refuse = null;
  const api = {
    request() {
      log.push("request");
      if (log.length === 1) return new Promise((_, reject) => { refuse = reject; });
      return Promise.resolve({ release() { return Promise.resolve(); } });
    },
  };
  const wl = W.createWakeLock({ wakeLock: api });
  wl.sync(true);
  wl.sync(false);
  refuse(new Error("NotAllowedError"));
  await flush();
  wl.sync(true);                                 // Resume
  await flush();
  assert.deepEqual(log, ["request", "request"]);
  assert.equal(wl.held(), true);
});

test("fullscreenApi finds the standard or the webkit API, or none", () => {
  assert.equal(W.fullscreenApi({}), null);
  assert.equal(W.fullscreenApi({ documentElement: {} }), null);
  assert.equal(W.fullscreenApi({ fullscreenEnabled: false, documentElement: { requestFullscreen() {} } }), null);
  const calls = [];
  const std = { fullscreenEnabled: true, fullscreenElement: null, exitFullscreen: () => calls.push("exit"),
                documentElement: { requestFullscreen: () => calls.push("enter") } };
  const a = W.fullscreenApi(std);
  assert.equal(a.change, "fullscreenchange");
  assert.equal(a.active(), false);
  a.enter();
  std.fullscreenElement = std.documentElement;
  assert.equal(a.active(), true);
  a.exit();
  assert.deepEqual(calls, ["enter", "exit"]);
  const wk = { webkitFullscreenEnabled: true, webkitExitFullscreen() {},
               documentElement: { webkitRequestFullscreen() {} } };
  assert.equal(W.fullscreenApi(wk).change, "webkitfullscreenchange");
});

// ------------------------------------------------------------------ the model

function model(extra = {}) {
  const sent = [];
  let clock = 0;
  let canSend = true;
  const m = W.createModel({ send: (msg) => { if (!canSend) return false; sent.push(msg); return true; },
                            now: () => clock, wall: (t) => 1000 + t / 1000, ...extra });
  return { m, sent, tick: (ms) => { clock += ms; }, setSend: (v) => { canSend = v; } };
}

function live() {
  const ctx = model();
  ctx.m.accept();
  assert.equal(ctx.m.measured(60.0), "connect");
  ctx.m.onState("connecting");
  ctx.m.onState("open");
  ctx.m.onMessage(WELCOME);
  ctx.m.onMessage(RUN);
  ctx.m.onMessage(LAYOUT);
  return ctx;
}

test("model: nothing is drawn before the warning is accepted", () => {
  const { m, sent } = model();
  assert.equal(m.measured(60), null);        // no connect before accept
  m.onState("open");
  m.onMessage(WELCOME);
  m.onMessage(RUN);
  m.onMessage(LAYOUT);
  for (let t = 0; t < 200; t += 16.7) assert.deepEqual(m.frame(t), { mode: "hidden", items: [] });
  assert.deepEqual(sent, []);
  assert.equal(m.banner(), "");
});

test("model: accept, then measuring, then connect on the first estimate only", () => {
  const { m } = model();
  m.accept();
  assert.equal(m.state.stage, "measuring");
  assert.match(m.banner(), /Measuring/);
  assert.equal(m.measured(NaN), null);
  assert.equal(m.measured(0), null);
  assert.equal(m.measured(59.9), "connect");
  assert.equal(m.measured(60.1), null);
  assert.equal(m.state.refreshHz, 60.1);
  assert.equal(m.flicker.measuredHz, 60.1);
  m.accept();                                // a second accept changes nothing
  assert.equal(m.state.stage, "live");
});

test("model: flickers in run with the flicker.js values, still in calibrate", () => {
  const { m, sent } = live();
  const p = 1000 / 60;
  for (let n = 0; n < 30; n++) {
    const v = m.frame(500 + n * p);
    assert.equal(v.mode, "flicker");
    assert.deepEqual(v.items.map((i) => i.slot), ["yes", "no"]);
    assert.equal(v.items[0].lum, F.luminance(15, 0, n, 60, "sine"));
    assert.equal(v.items[1].lum, F.luminance(17, 0, n, 60, "sine"));
  }
  assert.deepEqual(sent.filter((s) => s.type === "onset"), [{ type: "onset", layout_id: 3, t_client: 1000.5 }]);
  m.onMessage({ type: "phase", phase: "calibrate", flicker: false, seconds: 10, text: "Relax" });
  const v = m.frame(2000);
  assert.equal(v.mode, "still");
  assert.ok(v.items.every((i) => i.lum === 0.5));
});

test("model: the calibrate banner counts down", () => {
  const { m, tick } = live();
  m.onMessage({ type: "phase", phase: "calibrate", flicker: false, seconds: 10, text: "Relax" });
  assert.equal(m.banner(), "Relax (10 s)");
  tick(3500);
  assert.equal(m.banner(), "Relax (7 s)");
  tick(60000);
  assert.equal(m.banner(), "Relax (0 s)");
  assert.equal(m.remaining(), 0);
});

test("model: phase stop hides the squares and shows its text", () => {
  const { m } = live();
  m.onMessage({ type: "phase", phase: "stop", flicker: false, text: "Server shutting down" });
  assert.equal(m.frame(0).mode, "hidden");
  assert.equal(m.banner(), "Server shutting down");
});

test("model: a closed socket hides the squares at once and forgets the phase", () => {
  const { m } = live();
  assert.equal(m.frame(0).mode, "flicker");
  m.onState("closed");
  assert.equal(m.frame(16.7).mode, "hidden");
  assert.equal(m.state.phase, null);
  m.onState("connecting");
  assert.match(m.banner(), /Connecting/);
  m.onState("open");
  assert.equal(m.frame(33.3).mode, "hidden");     // no stale layout before catch-up
  m.onMessage(RUN);
  m.onMessage(LAYOUT);
  assert.equal(m.frame(50).mode, "flicker");
});

test("model: Esc stops locally and sends pause; Resume sends resume", () => {
  const { m, sent, tick } = live();
  m.frame(0);
  m.escape();
  assert.equal(m.frame(16.7).mode, "still");
  assert.equal(sent.at(-1).type, "pause");
  assert.match(m.banner(), /Paused/);
  assert.match(m.hud(null)[0], /· paused$/);
  m.onMessage(RUN);                              // the server cannot restart it
  assert.equal(m.frame(33.3).mode, "still");
  tick(W.RESUME_GUARD_MS);
  m.resume();
  assert.equal(sent.at(-1).type, "resume");
  assert.equal(m.frame(50).mode, "flicker");
  const n = sent.length;
  m.resume();                                    // not paused: nothing sent
  assert.equal(sent.length, n);
});

test("model: a pause that could not be sent goes out after the next welcome", () => {
  const { m, sent, setSend } = live();
  setSend(false);
  m.escape();
  setSend(true);
  m.onState("closed");
  m.onState("open");
  m.onMessage(WELCOME);
  assert.equal(sent.at(-1).type, "pause");
});

test("model: hover sends gaze on change, only with a fake board and an open socket", () => {
  const { m, sent } = live();
  const gazes = () => sent.filter((s) => s.type === "gaze").map((s) => s.slot);
  assert.equal(m.hover("yes"), true);
  assert.equal(m.hover("yes"), false);
  m.hover("no");
  m.hover(null);
  m.hover(undefined);
  m.hover(null);
  assert.deepEqual(gazes(), ["yes", "no", null]);
  // after a reconnect the same slot is sent again
  m.onState("closed");
  assert.equal(m.hover("yes"), false);
  m.onState("open");
  m.hover("yes");
  assert.deepEqual(gazes(), ["yes", "no", null, "yes"]);
  // a real board: never
  m.onMessage({ ...WELCOME, fake: false });
  m.hover("no");
  assert.deepEqual(gazes(), ["yes", "no", null, "yes"]);
});

test("model: hover does not record a gaze that could not be sent", () => {
  const { m, sent, setSend } = live();
  setSend(false);
  assert.equal(m.hover("yes"), false);
  setSend(true);
  assert.equal(m.hover("yes"), true);
  assert.equal(sent.filter((s) => s.type === "gaze").length, 1);
});

test("model: selections of the current layout flash and go to the HUD", () => {
  const { m, tick } = live();
  m.onMessage({ type: "selection", selection_id: 1, layout_id: 99, slot: "no", score: 1 });   // stale
  assert.deepEqual(m.state.selections, []);
  for (let i = 0; i < 8; i++) {
    m.onMessage({ type: "selection", selection_id: i, layout_id: 3, slot: i % 2 ? "no" : "yes", score: 1.5 });
  }
  assert.equal(m.state.selections.length, 6);
  assert.match(m.hud(38).join("\n"), /selections yes, no, yes, no, yes, no/);
  const v = m.frame(0);
  assert.deepEqual(v.items.map((i) => i.flash), [false, true]);
  tick(W.FLASH_MS);
  assert.deepEqual(m.frame(16.7).items.map((i) => i.flash), [false, false]);
});

test("model: the HUD shows phase, connection, rtt, board, refresh and errors", () => {
  const { m } = live();
  m.onMessage({ type: "status", board: "streaming", ok: true, text: "posterior RMS 14 uV", rtt_ms: 38 });
  m.frame(0);
  const lines = m.hud(42);
  assert.equal(lines[0], "phase run · flickering");
  assert.equal(lines[1], "server open · rtt 42 ms · fake board (hover or touch a square)");
  assert.equal(lines[2], "board streaming · posterior RMS 14 uV");
  assert.equal(lines[3], "refresh 60.00 Hz (want 60 ± 1)");
  m.onMessage({ type: "error", code: "not_allowed", message: "viewers may only ping", fatal: false });
  assert.equal(m.hud(null).at(-1), "error not_allowed: viewers may only ping");
  assert.equal(m.hud(null)[1], "server open · fake board (hover or touch a square)");
  const fresh = model().m;
  assert.deepEqual(fresh.hud(null), ["phase - · still", "server closed", "board -",
                                     "refresh measuring…", "selections -"]);
});

test("model: a refresh error waits for a good rate, then asks for a reconnect", () => {
  const { m } = live();
  const msg = "The display refreshes at 75.00 Hz, but the flicker needs 60 ± 1 Hz.";
  m.onMessage({ type: "error", code: "refresh", message: msg, fatal: false });
  m.onMessage({ type: "phase", phase: "wait", flicker: false, text: "" });
  assert.equal(m.banner(), msg);
  assert.equal(m.measured(75.0), null);
  assert.equal(m.measured(61.2), null);
  assert.equal(m.measured(60.4), "reconnect");
  assert.equal(m.measured(60.4), null);          // once
  m.onMessage(WELCOME);
  assert.equal(m.state.error, null);
});

test("model: a refresh error reconnects at any rate the welcome accepts, and the HUD lists them", () => {
  const { m } = live();
  m.onMessage({ ...WELCOME, refresh_rates: [60, 90, 120, 144] });
  m.onMessage({ type: "error", code: "refresh", fatal: false,
                message: "The display refreshes at 75.00 Hz, but the flicker needs one of 60, 90, " +
                         "120 or 144 Hz (± 1 Hz)." });
  assert.equal(m.measured(75.0), null);
  assert.equal(m.hud(null)[3], "refresh 75.00 Hz (want 60, 90, 120 or 144 ± 1)");
  assert.equal(m.measured(121.5), null);
  assert.equal(m.measured(119.6), "reconnect");  // the user set the display to 120 Hz
  assert.equal(m.measured(119.6), null);
  // an older server without refresh_rates: only its refresh_hz
  const old = live().m;
  old.onMessage({ type: "error", code: "refresh", message: "x", fatal: false });
  assert.equal(old.measured(120.0), null);
  assert.equal(old.hud(null)[3], "refresh 120.00 Hz (want 60 ± 1)");
  assert.equal(old.measured(59.5), "reconnect");
});

test("model: closes that do not reconnect say what to do", () => {
  for (const [code, re] of [[4000, /took over/], [4001, /token/], [4002, /takeover=1/]]) {
    const { m } = live();
    m.onState("closed");
    m.onClose({ code, reason: "", reconnect: false });
    assert.match(m.banner(), re);
    assert.equal(m.frame(0).mode, "hidden");
  }
  const { m } = live();
  m.onState("closed");
  m.onClose({ code: 1006, reason: "", reconnect: true });
  assert.equal(m.state.ended, null);
  m.onClose({ code: 4000, reason: "", reconnect: true });   // reconnecting: no message
  assert.equal(m.state.ended, null);
});

test("model: frames reports go out every 5 s while flickering", () => {
  const { m, sent } = live();
  const p = 1000 / 60;
  for (let n = 0; n <= 301; n++) m.frame(n * p);
  const frames = sent.filter((s) => s.type === "frames");
  assert.deepEqual(frames, [{ type: "frames", refresh_hz: 60, dropped: 0 }]);
});

test("model: banners for wait, server pause and no phase", () => {
  const { m } = live();
  m.onMessage({ type: "phase", phase: "wait", flicker: false, text: "" });
  assert.match(m.banner(), /Waiting/);
  m.onMessage({ type: "phase", phase: "pause", flicker: false, text: "" });
  assert.match(m.banner(), /Paused by the server/);
  m.onMessage({ type: "phase", phase: "run", flicker: true, text: "" });
  assert.equal(m.banner(), "");
  const b = model().m;
  b.accept();
  b.measured(60);
  b.onState("open");
  assert.match(b.banner(), /Waiting for the server/);
});

test("model: a second pause sends nothing more and Resume waits out the guard", () => {
  const { m, sent, tick } = live();
  assert.equal(m.pause(), true);
  assert.equal(m.pause(), false);               // Pause button and page hidden at once
  m.escape();
  assert.deepEqual(sent.filter((s) => s.type === "pause").length, 1);
  assert.equal(m.frame(0).mode, "still");
  // a double tap on Pause lands on Resume: ignored
  tick(W.RESUME_GUARD_MS - 1);
  assert.equal(m.resumeBlocked(), true);
  assert.equal(m.resume(), false);
  assert.equal(m.state.paused, true);
  assert.equal(m.frame(16.7).mode, "still");
  assert.ok(!sent.some((s) => s.type === "resume"));
  tick(1);
  assert.equal(m.resumeBlocked(), false);
  assert.equal(m.resume(), true);
  assert.equal(sent.at(-1).type, "resume");
  assert.equal(m.resumeBlocked(), false);       // not paused
});

test("model: the wake lock is wanted in run only, live and not paused", () => {
  const { m, tick } = live();
  assert.equal(m.wantsWakeLock(), true);
  m.pause();
  assert.equal(m.wantsWakeLock(), false);
  tick(W.RESUME_GUARD_MS);
  m.resume();
  assert.equal(m.wantsWakeLock(), true);
  m.onMessage({ type: "phase", phase: "calibrate", flicker: false, seconds: 10, text: "" });
  assert.equal(m.wantsWakeLock(), false);
  m.onMessage(RUN);
  m.onState("closed");                          // closing forgets the phase
  assert.equal(m.wantsWakeLock(), false);
  const fresh = model().m;
  assert.equal(fresh.wantsWakeLock(), false);
  fresh.accept();
  assert.equal(fresh.wantsWakeLock(), false);
});

// ------------------------------------------------------------------ the page, wired

const PAGE_IDS = ["stage", "hud", "banner", "dock", "safe", "warning", "pause", "resume",
                  "fullscreen", "accept"];

function fakeElement(id) {
  const listeners = {};
  return {
    id, hidden: false, disabled: false, textContent: "", style: {}, width: 0, height: 0, listeners,
    addEventListener(type, f) { (listeners[type] ||= []).push(f); },
    fire(type, ev = {}) { for (const f of listeners[type] || []) f({ type, ...ev }); },
  };
}

function fakeCanvasContext() {
  const ops = [];
  const c = { ops, fillStyle: "", strokeStyle: "", lineWidth: 1, font: "", textAlign: "", textBaseline: "" };
  for (const name of ["clearRect", "strokeRect", "fillText", "setLineDash", "setTransform"]) {
    c[name] = (...a) => ops.push([name, ...a]);
  }
  c.fillRect = (...a) => ops.push(["fillRect", c.fillStyle, ...a]);
  return c;
}

const rect = (left, top, right, bottom) => ({ left, top, right, bottom, width: right - left,
                                              height: bottom - top });

/**
 * The page wired to fakes. o: hash, w, h, dpr, nav (navigator), rects ({id: DOMRect-like},
 * read on every measure, so a test may swap them), pointer (false: no PointerEvent), doc
 * (extra document fields, such as the Fullscreen API).
 */
function page(o = {}) {
  if (typeof o === "string") o = { hash: o };
  const { hash = "#token=tok123", w = 1000, h = 600, dpr = 2, nav, rects = {}, pointer = true } = o;
  const { FakeWebSocket, sockets } = makeFakeWS();
  const clock = makeFakeTimers();
  const els = Object.fromEntries(PAGE_IDS.map((id) => [id, fakeElement(id)]));
  const ctx2d = fakeCanvasContext();
  els.stage.getContext = () => ctx2d;
  for (const id of ["pause", "resume", "fullscreen"]) els[id].hidden = true;
  for (const id of ["hud", "dock", "safe"]) {
    els[id].getBoundingClientRect = () => rects[id] || rect(0, 0, 0, 0);
  }
  const docListeners = {};
  const doc = { getElementById: (id) => els[id], visibilityState: "visible",
                addEventListener: (type, f) => { (docListeners[type] ||= []).push(f); },
                ...(o.doc || {}) };
  const winListeners = {};
  let raf = null;
  const prompts = [];
  const win = {
    location: { hash, protocol: "http:", host: "127.0.0.1:8765" },
    innerWidth: w, innerHeight: h, devicePixelRatio: dpr, navigator: nav,
    requestAnimationFrame: (f) => { raf = f; return 1; },
    addEventListener: (type, f) => { (winListeners[type] ||= []).push(f); },
    prompt: (q) => { prompts.push(q); return "  typed-token "; },
    console: { warn() {} },
    setInterval: clock.timers.setInterval,
  };
  if (pointer) win.PointerEvent = function PointerEvent() {};
  const app = W.start(win, doc, { WebSocket: FakeWebSocket, timers: clock.timers, now: clock.now });
  const fireWin = (type, ev) => { for (const f of winListeners[type] || []) f(ev); };
  const fireDoc = (type, ev = {}) => { for (const f of docListeners[type] || []) f(ev); };
  let ts = 0;
  const frames = (n) => { for (let i = 0; i < n; i++) { const f = raf; raf = null; ts += 1000 / 60; f(ts); } };
  return { app, els, ctx2d, sockets, clock, win, doc, rects, fireWin, fireDoc, frames, prompts,
           rafPending: () => raf !== null };
}

/** Accept, measure, connect, and get to run with LAYOUT drawn once. Returns the socket. */
function run(p) {
  p.els.accept.fire("click");
  p.frames(141);
  const ws = p.sockets.at(-1);
  ws.accept();
  ws.message(WELCOME);
  ws.message(RUN);
  ws.message(LAYOUT);
  p.frames(1);
  return ws;
}

/** The greys drawn into the squares (not the black borders) since the ops were cleared. */
const squareGreys = (p) => p.ctx2d.ops.filter((o) => o[0] === "fillRect" && o[1] !== "#000").map((o) => o[1]);
const gazes = (ws) => ws.sent.filter((m) => m.type === "gaze").map((m) => m.slot);

test("page: warning gate, refresh measurement, then hello with the measured rate", () => {
  const p = page("#token=tok123&takeover=1");
  assert.equal(p.rafPending(), false);          // nothing runs before accept
  assert.equal(p.els.warning.hidden, false);
  assert.equal(p.els.stage.width, 2000);        // dpr 2
  p.els.accept.fire("click");
  assert.equal(p.els.warning.hidden, true);
  p.frames(140);                                // 1 + 20 warm-up + 119 intervals
  assert.equal(p.sockets.length, 0);
  p.frames(1);
  assert.equal(p.sockets.length, 1);
  assert.equal(p.sockets[0].url, "ws://127.0.0.1:8765/ws");
  p.sockets[0].accept();
  const hello = p.sockets[0].sent[0];
  assert.equal(hello.type, "hello");
  assert.equal(hello.protocol, 3);
  assert.equal(hello.token, "tok123");
  assert.equal(hello.role, "controller");
  assert.equal(hello.takeover, true);
  assert.equal(hello.mode, "slots");
  assert.deepEqual(hello.slots, ["yes", "no"]);
  assert.equal(hello.accepted, true);
  assert.ok(Math.abs(hello.refresh_hz - 60) < 0.01, String(hello.refresh_hz));
  assert.deepEqual(hello.viewport, { w: 1000, h: 600, dpr: 2 });
  assert.deepEqual(p.prompts, []);
  assert.ok(!p.els.hud.textContent.includes("tok123"));
});

test("page: no token in the address asks for one", () => {
  const p = page("");
  p.els.accept.fire("click");
  p.frames(141);
  p.sockets[0].accept();
  assert.equal(p.prompts.length, 1);
  assert.equal(p.sockets[0].sent[0].token, "typed-token");
});

test("page: draws flickering squares, sends onset, gaze on hover, Esc and Resume", () => {
  const p = page();
  p.els.accept.fire("click");
  p.frames(141);
  const ws = p.sockets[0];
  ws.accept();
  ws.message(WELCOME);
  ws.message(RUN);
  ws.message(LAYOUT);
  p.ctx2d.ops.length = 0;
  p.frames(4);
  const greys = p.ctx2d.ops.filter((o) => o[0] === "fillRect" && o[1] !== "#000").map((o) => o[1]);
  // frames 0..3 of 15 Hz at 60 Hz: 0.5, 1, 0.5, 0 -> greys 128, 255, 128, 0 (yes square)
  assert.deepEqual(greys.filter((_, i) => i % 2 === 0),
                   ["rgb(128,128,128)", "rgb(255,255,255)", "rgb(128,128,128)", "rgb(0,0,0)"]);
  assert.equal(ws.types().filter((t) => t === "onset").length, 1);
  assert.ok(ws.types().includes("ping"));
  const labels = p.ctx2d.ops.filter((o) => o[0] === "fillText").map((o) => o[1]);
  assert.deepEqual(labels.slice(0, 2), ["yes", "no"]);
  // hover the first square with the mouse, then nothing
  const [sq, sq2] = p.app.squares();
  const mouse = { pointerType: "mouse", isPrimary: true };
  p.els.stage.fire("pointermove", { ...mouse, clientX: sq.x + 5, clientY: sq.y + 5 });
  p.els.stage.fire("pointermove", { ...mouse, clientX: sq.x + 6, clientY: sq.y + 6 });
  p.els.stage.fire("pointerleave", mouse);
  assert.deepEqual(gazes(ws), ["yes", null]);
  // a layout that changed since the last frame: the stale square maps to nothing
  ws.message({ ...LAYOUT, layout_id: 4, targets: [] });
  p.els.stage.fire("pointermove", { ...mouse, clientX: sq2.x + 5, clientY: sq2.y + 5 });
  assert.deepEqual(gazes(ws), ["yes", null]);
  ws.message(LAYOUT);
  p.frames(1);
  // Esc: still at once, pause sent, Resume shown
  p.fireWin("keydown", { key: "Escape" });
  assert.equal(ws.sent.at(-1).type, "pause");
  assert.equal(p.els.resume.hidden, false);
  p.ctx2d.ops.length = 0;
  p.frames(3);
  const still = p.ctx2d.ops.filter((o) => o[0] === "fillRect" && o[1] !== "#000").map((o) => o[1]);
  assert.ok(still.every((g) => g === "rgb(128,128,128)"), still.join());
  p.clock.advance(W.RESUME_GUARD_MS);
  p.els.resume.fire("click");
  assert.equal(ws.sent.at(-1).type, "resume");
  assert.equal(p.els.resume.hidden, true);
  // HUD refresh on the 250 ms timer
  p.clock.advance(250);
  assert.match(p.els.hud.textContent, /phase run/);
  // leaving the page says bye
  p.fireWin("pagehide");
  assert.equal(ws.sent.at(-1).type, "bye");
});

test("page: a socket drop clears the canvas at once", () => {
  const p = page();
  p.els.accept.fire("click");
  p.frames(141);
  const ws = p.sockets[0];
  ws.accept();
  ws.message(WELCOME);
  ws.message(RUN);
  ws.message(LAYOUT);
  p.frames(2);
  ws.drop(1006);
  p.ctx2d.ops.length = 0;
  p.frames(1);
  assert.deepEqual(p.ctx2d.ops.map((o) => o[0]), ["clearRect"]);
  p.clock.advance(1000);
  assert.equal(p.sockets.length, 2);             // reconnecting
});

test("page: Escape, Pause or a hidden page before accepting does nothing", () => {
  const p = page();
  p.fireWin("keydown", { key: "Escape" });
  p.doc.visibilityState = "hidden";
  p.fireDoc("visibilitychange");
  assert.equal(p.els.resume.hidden, true);
  assert.equal(p.els.pause.hidden, true);
  assert.equal(p.app.model.state.paused, false);
  assert.equal(p.app.model.state.stage, "warning");
});

test("page: the Pause button shows once accepted, stops the flicker at once and sends pause", () => {
  const p = page();
  assert.equal(p.els.pause.hidden, true);        // not over the warning
  const ws = run(p);
  assert.equal(p.els.pause.hidden, false);
  assert.equal(p.els.resume.hidden, true);
  p.ctx2d.ops.length = 0;
  p.frames(2);
  assert.ok(squareGreys(p).some((g) => g !== "rgb(128,128,128)"));   // flickering
  p.els.pause.fire("click");
  assert.equal(ws.sent.at(-1).type, "pause");
  assert.equal(p.els.pause.hidden, true);
  assert.equal(p.els.resume.hidden, false);
  assert.equal(p.els.resume.disabled, true);
  p.ctx2d.ops.length = 0;
  p.frames(3);
  assert.ok(squareGreys(p).every((g) => g === "rgb(128,128,128)"));
  // a double tap: the second tap lands on Resume, which waits out the guard
  p.els.resume.fire("click");
  assert.equal(p.app.model.state.paused, true);
  assert.ok(!ws.types().includes("resume"));
  p.clock.advance(W.RESUME_GUARD_MS);
  assert.equal(p.els.resume.disabled, false);
  p.els.resume.fire("click");
  assert.equal(ws.sent.at(-1).type, "resume");
  assert.equal(p.els.pause.hidden, false);
  assert.equal(p.els.resume.hidden, true);
  // the button is there in every phase, flickering or not, until paused
  ws.message({ type: "phase", phase: "calibrate", flicker: false, seconds: 10, text: "" });
  p.clock.advance(250);
  assert.equal(p.els.pause.hidden, false);
});

test("page: going to the background pauses, and coming back does not resume", async () => {
  const f = fakeWakeLock();
  const p = page({ nav: { wakeLock: f.api } });
  const ws = run(p);
  p.clock.advance(250);
  await flush();
  assert.equal(p.app.wakeLock.held(), true);
  p.doc.visibilityState = "hidden";
  p.fireDoc("visibilitychange");
  assert.equal(ws.sent.at(-1).type, "pause");
  assert.equal(p.app.wakeLock.held(), false);
  assert.deepEqual(f.log.at(-1), ["release"]);
  p.doc.visibilityState = "visible";
  p.fireDoc("visibilitychange");
  p.clock.advance(5000);
  await flush();
  p.ctx2d.ops.length = 0;
  p.frames(3);
  assert.ok(squareGreys(p).every((g) => g === "rgb(128,128,128)"));
  assert.ok(!ws.types().includes("resume"));
  assert.equal(ws.types().filter((t) => t === "pause").length, 1);
  assert.match(p.els.banner.textContent, /Paused/);
  assert.equal(p.els.resume.hidden, false);
  assert.equal(f.log.filter((e) => e[0] === "request").length, 1);   // not while paused
  // the user presses Resume: the flicker and the wake lock come back
  p.els.resume.fire("click");
  assert.equal(ws.sent.at(-1).type, "resume");
  await flush();
  assert.equal(f.log.filter((e) => e[0] === "request").length, 2);
  assert.equal(p.app.wakeLock.held(), true);
  p.ctx2d.ops.length = 0;
  p.frames(2);
  assert.ok(squareGreys(p).some((g) => g !== "rgb(128,128,128)"));
});

test("page: the wake lock is held in run only and let go on pause and on leaving", async () => {
  const f = fakeWakeLock();
  const p = page({ nav: { wakeLock: f.api } });
  p.els.accept.fire("click");
  p.frames(141);
  const ws = p.sockets[0];
  ws.accept();
  ws.message(WELCOME);
  ws.message({ type: "phase", phase: "calibrate", flicker: false, seconds: 10, text: "" });
  p.clock.advance(500);
  await flush();
  assert.deepEqual(f.log, []);                   // not before run
  ws.message(RUN);
  ws.message(LAYOUT);
  p.clock.advance(250);
  await flush();
  assert.deepEqual(f.log, [["request", "screen"]]);
  assert.equal(p.app.wakeLock.held(), true);
  p.clock.advance(1000);
  await flush();
  assert.equal(f.log.length, 1);                 // held: not asked again
  p.fireWin("keydown", { key: "Escape" });
  assert.deepEqual(f.log.at(-1), ["release"]);
  p.clock.advance(W.RESUME_GUARD_MS);
  p.els.resume.fire("click");
  await flush();
  assert.equal(p.app.wakeLock.held(), true);
  // the socket drops: no phase, no lock
  ws.drop(1006);
  assert.equal(p.app.wakeLock.held(), false);
  p.clock.advance(1000);                         // reconnecting
  const ws2 = p.sockets[1];
  ws2.accept();
  ws2.message(WELCOME);
  ws2.message(RUN);
  ws2.message(LAYOUT);
  p.clock.advance(250);
  await flush();
  assert.equal(p.app.wakeLock.held(), true);
  // leaving: paused, the lock let go, then bye
  p.fireWin("pagehide");
  assert.equal(p.app.wakeLock.held(), false);
  assert.deepEqual(f.log.at(-1), ["release"]);
  assert.deepEqual(ws2.types().slice(-2), ["pause", "bye"]);
});

test("page: without the Wake Lock API, or when it refuses, the page runs as before", async () => {
  for (const nav of [undefined, {}, { wakeLock: fakeWakeLock({ mode: "reject" }).api },
                     { wakeLock: fakeWakeLock({ mode: "throw" }).api }]) {
    const p = page({ nav });
    const ws = run(p);
    p.clock.advance(1000);
    await flush();
    assert.equal(p.app.wakeLock.held(), false);
    p.ctx2d.ops.length = 0;
    p.frames(2);
    assert.ok(squareGreys(p).some((g) => g !== "rgb(128,128,128)"));
    p.els.pause.fire("click");
    assert.equal(ws.sent.at(-1).type, "pause");
  }
});

test("page: touching a square sends gaze, lifting the finger sends null", () => {
  const p = page();
  const ws = run(p);
  const [yes, no] = p.app.squares();
  const at = (sq, ev = {}) => ({ clientX: sq.x + sq.side / 2, clientY: sq.y + sq.side / 2,
                                 pointerType: "touch", isPrimary: true, ...ev });
  const stage = p.els.stage;
  stage.fire("pointerdown", at(yes));
  stage.fire("pointermove", at(yes));
  stage.fire("pointermove", at(no));             // the finger slides to the other square
  stage.fire("pointerup", at(no));
  assert.deepEqual(gazes(ws), ["yes", "no", null]);
  stage.fire("pointerdown", at(no));
  stage.fire("pointercancel", at(no));           // the browser took the touch
  assert.deepEqual(gazes(ws), ["yes", "no", null, "no", null]);
  // between the squares: nothing; a second finger is ignored
  stage.fire("pointerdown", { clientX: 1, clientY: 1, pointerType: "touch", isPrimary: true });
  stage.fire("pointerdown", at(yes, { isPrimary: false }));
  stage.fire("pointerup", at(yes, { isPrimary: false }));
  assert.deepEqual(gazes(ws), ["yes", "no", null, "no", null]);
  // the mouse hovers: a click (pointerup) does not end the gaze, leaving does
  stage.fire("pointermove", at(yes, { pointerType: "mouse" }));
  stage.fire("pointerdown", at(yes, { pointerType: "mouse" }));
  stage.fire("pointerup", at(yes, { pointerType: "mouse" }));
  assert.equal(gazes(ws).at(-1), "yes");
  stage.fire("pointerleave", { pointerType: "mouse", isPrimary: true });
  assert.deepEqual(gazes(ws), ["yes", "no", null, "no", null, "yes", null]);
  // a real board: touching sends nothing
  ws.message({ ...WELCOME, fake: false });
  stage.fire("pointerdown", at(no));
  assert.equal(gazes(ws).length, 7);
});

test("page: without pointer events the mouse still hovers", () => {
  const p = page({ pointer: false });
  const ws = run(p);
  const [yes] = p.app.squares();
  p.els.stage.fire("mousemove", { clientX: yes.x + 1, clientY: yes.y + 1 });
  p.els.stage.fire("mouseleave");
  assert.deepEqual(gazes(ws), ["yes", null]);
  assert.equal(p.els.stage.listeners.pointermove, undefined);
});

test("page: a portrait phone gets a column clear of the notch, the HUD and the buttons", () => {
  const M = W.LAYOUT_MARGIN;
  const rects = { safe: rect(0, 47, 390, 810), hud: rect(12, 59, 330, 175), dock: rect(0, 690, 390, 844) };
  const p = page({ w: 390, h: 844, dpr: 3, rects });
  const ws = run(p);
  assert.equal(p.els.stage.width, 1170);
  assert.equal(p.els.stage.style.width, "390px");
  const sq = p.app.squares();
  assert.equal(sq.length, 2);
  assert.equal(sq[0].x, sq[1].x);                // one column
  assert.ok(sq[1].y > sq[0].y + sq[0].side);
  for (const s of sq) {
    assert.ok(s.x >= 0 && s.x + s.side <= 390);  // no horizontal overflow
    assert.ok(s.y >= 175 + M, String(s.y));      // below the HUD
    assert.ok(s.y + s.side <= 690 - M, String(s.y + s.side));   // above the Pause button
  }
  assert.ok(sq[0].side > (0.9 * 390) / 2.5, String(sq[0].side));   // bigger than a row's
  // the HUD grows (a longer line wraps): the squares move clear of it on the next measure
  rects.hud = rect(12, 59, 330, 205);
  p.clock.advance(250);
  p.frames(1);
  assert.ok(p.app.squares()[0].y >= 205 + M);
  // ... and do not jump back when it shrinks again
  const y = p.app.squares()[0].y;
  rects.hud = rect(12, 59, 330, 150);
  p.clock.advance(250);
  p.frames(1);
  assert.equal(p.app.squares()[0].y, y);
  // rotate to landscape: laid out again at once, as a row
  p.win.innerWidth = 844;
  p.win.innerHeight = 390;
  Object.assign(rects, { safe: rect(47, 0, 797, 369), hud: rect(59, 12, 420, 110),
                         dock: rect(47, 270, 797, 390) });
  p.fireWin("orientationchange");
  assert.equal(p.els.stage.width, 2532);
  p.frames(1);
  const row = p.app.squares();
  assert.equal(row[0].y, row[1].y);
  for (const s of row) {
    assert.ok(s.x >= 47 && s.x + s.side <= 797);
    assert.ok(s.y >= 110 + M && s.y + s.side <= 270 - M);
  }
  // as big as the strip allows: 0.8 of it (16 px labels below), not 0.4
  const strip = 390 - (110 + M) - (390 - 270 + M) - 1.4 * 16;
  assert.ok(Math.abs(row[0].side - 0.8 * strip) < 1e-6, String(row[0].side));
  // a phone that reports the new size only after the event: the second layout catches it
  p.win.innerWidth = 390;
  p.win.innerHeight = 844;
  Object.assign(rects, { safe: rect(0, 47, 390, 810), hud: rect(12, 59, 330, 175),
                         dock: rect(0, 690, 390, 844) });
  p.clock.advance(300);
  assert.equal(p.els.stage.width, 1170);
  p.frames(1);
  assert.equal(p.app.squares()[0].x, p.app.squares()[1].x);
  // and the touch gaze uses the new squares
  const [yes] = p.app.squares();
  p.els.stage.fire("pointerdown", { clientX: yes.x + 2, clientY: yes.y + 2, pointerType: "touch",
                                    isPrimary: true });
  assert.equal(gazes(ws).at(-1), "yes");
});

test("page: a Full screen button only where the browser offers it", async () => {
  const p = page();
  run(p);
  assert.equal(p.els.fullscreen.hidden, true);
  const calls = [];
  const el = { requestFullscreen: () => { calls.push("enter"); return Promise.reject(new Error("denied")); } };
  const q = page({ doc: { fullscreenEnabled: true, fullscreenElement: null, documentElement: el,
                          exitFullscreen: () => { calls.push("exit"); return Promise.resolve(); } } });
  assert.equal(q.els.fullscreen.hidden, true);   // not over the warning
  run(q);
  assert.equal(q.els.fullscreen.hidden, false);
  assert.equal(q.els.fullscreen.textContent, "Full screen");
  q.els.fullscreen.fire("click");
  await flush();                                 // the refusal is swallowed
  q.doc.fullscreenElement = el;
  q.win.innerWidth = 1200;
  q.fireDoc("fullscreenchange");
  assert.equal(q.els.stage.width, 2400);         // laid out again
  assert.equal(q.els.fullscreen.textContent, "Exit full screen");
  q.els.fullscreen.fire("click");
  assert.deepEqual(calls, ["enter", "exit"]);
});

test("page: leaving full screen pauses, since Esc there never reaches the page", () => {
  const el = { requestFullscreen: () => Promise.resolve() };
  const p = page({ doc: { fullscreenEnabled: true, fullscreenElement: null, documentElement: el,
                          exitFullscreen: () => Promise.resolve() } });
  p.doc.fullscreenElement = el;                  // over the warning: nothing to pause
  p.fireDoc("fullscreenchange");
  p.doc.fullscreenElement = null;
  p.fireDoc("fullscreenchange");
  assert.equal(p.app.model.state.paused, false);
  const ws = run(p);
  p.els.fullscreen.fire("click");
  p.doc.fullscreenElement = el;
  p.fireDoc("fullscreenchange");                 // entering keeps the flicker going
  assert.ok(!ws.types().includes("pause"));
  assert.equal(p.app.model.flicker.shouldFlicker(), true);
  // Esc: the browser leaves full screen and sends no keydown
  p.doc.fullscreenElement = null;
  p.fireDoc("fullscreenchange");
  assert.equal(ws.sent.at(-1).type, "pause");
  assert.equal(p.app.model.flicker.shouldFlicker(), false);
  assert.equal(p.els.resume.hidden, false);
  p.ctx2d.ops.length = 0;
  p.frames(3);
  assert.ok(squareGreys(p).every((g) => g === "rgb(128,128,128)"));
});

test("page: focus moves from Pause to Resume once it is enabled, and back", () => {
  const p = page();
  p.doc.body = { id: "body" };
  for (const id of ["pause", "resume"]) p.els[id].focus = () => { p.doc.activeElement = p.els[id]; };
  const ws = run(p);
  p.els.pause.focus();
  p.els.pause.fire("click");                     // Enter on the focused Pause button
  assert.equal(ws.sent.at(-1).type, "pause");
  p.doc.activeElement = p.doc.body;              // the browser drops focus off the hidden button
  p.clock.advance(250);
  assert.equal(p.doc.activeElement, p.doc.body); // Resume is still disabled: it cannot take focus
  p.clock.advance(W.RESUME_GUARD_MS);
  assert.equal(p.els.resume.disabled, false);
  assert.equal(p.doc.activeElement, p.els.resume);
  p.els.resume.fire("click");                    // Enter again
  assert.equal(ws.sent.at(-1).type, "resume");
  assert.equal(p.doc.activeElement, p.els.pause);
  // focus the user moved elsewhere is left alone
  p.els.pause.fire("click");
  const other = { id: "other" };
  p.doc.activeElement = other;
  p.clock.advance(W.RESUME_GUARD_MS + 250);
  assert.equal(p.doc.activeElement, other);
});

test("page: a window with no room for the squares pauses instead of flickering unseen", () => {
  // the HUD and the buttons leave no strip between them: nothing could be drawn
  const rects = { hud: rect(12, 12, 400, 150), dock: rect(0, 160, 568, 320) };
  const p = page({ w: 568, h: 320, rects });
  const ws = run(p);
  assert.ok(ws.types().includes("pause"));
  assert.ok(!ws.types().includes("onset"));
  assert.ok(!ws.types().includes("frames"));
  assert.equal(p.app.squares().length, 0);
  assert.match(p.els.banner.textContent, /too small for the squares/);
  // made bigger, then Resume: drawn and flickering
  p.win.innerHeight = 700;
  Object.assign(rects, { dock: rect(0, 560, 568, 700) });
  p.fireWin("resize");
  p.clock.advance(W.RESUME_GUARD_MS);
  p.els.resume.fire("click");
  assert.equal(ws.sent.at(-1).type, "resume");
  assert.doesNotMatch(p.els.banner.textContent, /too small/);
  p.frames(2);
  assert.equal(p.app.squares().length, 2);
  assert.equal(ws.types().filter((t) => t === "onset").length, 1);
});

test("page: back from the back-forward cache it reconnects, still paused", () => {
  const p = page();
  const ws = run(p);
  p.fireWin("pagehide");
  assert.deepEqual(ws.types().slice(-2), ["pause", "bye"]);
  p.fireWin("pageshow", { persisted: false });
  assert.equal(p.sockets.length, 1);
  p.fireWin("pageshow", { persisted: true });
  assert.equal(p.sockets.length, 2);
  const ws2 = p.sockets[1];
  ws2.accept();
  ws2.message(WELCOME);
  assert.equal(ws2.sent.at(-1).type, "pause");
  ws2.message(RUN);
  ws2.message(LAYOUT);
  p.ctx2d.ops.length = 0;
  p.frames(3);
  assert.ok(squareGreys(p).every((g) => g === "rgb(128,128,128)"));
  assert.equal(p.els.resume.hidden, false);
});

test("the page fits a phone: 48 px buttons, no gestures on the canvas, safe areas, host-relative paths", () => {
  const css = readFileSync(new URL("../../web/style.css", import.meta.url), "utf8");
  const html = readFileSync(new URL("../../web/index.html", import.meta.url), "utf8");
  assert.match(css, /\nbutton \{[^}]*min-height: 48px;[^}]*min-width: 48px;/);
  assert.match(css, /#stage \{[^}]*touch-action: none;/);
  for (const side of ["top", "right", "bottom", "left"]) assert.match(css, new RegExp(`env\\(safe-area-inset-${side}`));
  assert.match(html, /<meta name="viewport" content="[^"]*width=device-width[^"]*viewport-fit=cover/);
  assert.match(html, /<button id="pause" type="button" hidden>Pause<\/button>/);
  // no scheme or host in any file reference: the same page works behind any TLS proxy
  const refs = [...html.matchAll(/(?:src|href)="([^"]+)"/g)].map((m) => m[1]);
  assert.ok(refs.length >= 4);
  for (const r of refs) assert.ok(!/^[a-z]+:|^\/\//i.test(r), r);
  assert.match(html, /Pause<\/strong> button/);  // the warning names the phone's way to stop
});

test("app.js loads in a browser-like global after the SDK and starts itself", () => {
  const els = {};
  for (const id of PAGE_IDS) els[id] = fakeElement(id);
  els.stage.getContext = () => fakeCanvasContext();
  const ctx = {
    performance: { timeOrigin: 0 }, Date, URLSearchParams,
    location: { hash: "", protocol: "http:", host: "x" }, innerWidth: 10, innerHeight: 10,
    addEventListener() {}, setInterval() { return 1; }, setTimeout() { return 1; },
    clearTimeout() {}, clearInterval() {}, requestAnimationFrame() { return 1; },
    console: { warn() {} }, WebSocket: class {},
  };
  ctx.document = { readyState: "complete", getElementById: (id) => els[id], addEventListener() {} };
  ctx.globalThis = ctx;
  for (const f of ["../flicker.js", "../brainsync-client.js", "../../web/app.js"]) {
    vm.runInNewContext(readFileSync(new URL(f, import.meta.url), "utf8"), ctx);
  }
  assert.equal(typeof ctx.BrainSyncWeb.createModel, "function");
  assert.equal(els.accept.listeners.click.length, 1);   // start() ran
});
