// content.js: scan sizing (long edge <= 1280), password masks on the image, data URLs, the
// scan/click agent (scan_request -> scan, click -> clicked), the port transport and the
// session wiring with a fake chrome API and a fake DOM. No browser, no network.
//     node --test clients/chrome/test/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { makeFakePage, makeFakePort, makeFakeTimers, settle } from "./fakes.mjs";

const require = createRequire(import.meta.url);
const CT = require("../content.js");

// --------------------------------------------------------------- sizing and masks

test("scanSize keeps the aspect and the long edge at most 1280", () => {
  assert.deepEqual(CT.scanSize(3024, 1714), { w: 1280, h: 726, scale: 1280 / 3024 });
  assert.deepEqual(CT.scanSize(1512, 857), { w: 1280, h: 726, scale: 1280 / 1512 });
  const portrait = CT.scanSize(1170, 2532);
  assert.equal(portrait.h, 1280);
  assert.equal(portrait.w, Math.round(1170 * 1280 / 2532));
  assert.deepEqual(CT.scanSize(800, 600), { w: 800, h: 600, scale: 1 }, "small screenshots are not enlarged");
  assert.deepEqual(CT.scanSize(1280, 1280), { w: 1280, h: 1280, scale: 1 });
  assert.deepEqual(CT.scanSize(100000, 1), { w: 1280, h: 1, scale: 1280 / 100000 }, "never 0 px high");
  for (const [w, h] of [[0, 10], [10, -1], [NaN, 10], [10, Infinity]]) {
    assert.throws(() => CT.scanSize(w, h), RangeError);
  }
  assert.throws(() => CT.scanSize(10, 10, 0), /maxEdge/);
  for (let i = 0; i < 200; i++) {
    const w = 1 + Math.floor(Math.random() * 6000), h = 1 + Math.floor(Math.random() * 6000);
    const s = CT.scanSize(w, h);
    assert.ok(Math.max(s.w, s.h) <= 1280 && s.w >= 1 && s.h >= 1, `${w}x${h} -> ${s.w}x${s.h}`);
    assert.ok(Math.abs(s.w / s.h - w / h) <= (w / h) * (1 / Math.min(s.w, s.h)) + 1e-9);
  }
});

test("masksToImage scales password rects onto the image, grows them by the pad and clips them", () => {
  // viewport 1512 x 857 CSS px, image 1280 x 725
  const out = CT.masksToImage([{ x: 100, y: 200, w: 300, h: 40 }], 1512, 857, 1280, 725);
  const sx = 1280 / 1512, sy = 725 / 857;
  assert.deepEqual(out, [{ x: Math.floor(98 * sx), y: Math.floor(198 * sy),
                           w: Math.ceil(402 * sx) - Math.floor(98 * sx), h: Math.ceil(242 * sy) - Math.floor(198 * sy) }]);
  // covers every image pixel of the field
  assert.ok(out[0].x <= 100 * sx && out[0].x + out[0].w >= 400 * sx);
  const edge = CT.masksToImage([{ x: 0, y: 840, w: 50, h: 40 }], 1000, 857, 1000, 857, 2);
  assert.deepEqual(edge, [{ x: 0, y: 838, w: 52, h: 19 }]);
  assert.deepEqual(CT.masksToImage([{ x: NaN, y: 0, w: 1, h: 1 }, null], 100, 100, 100, 100), []);
  assert.deepEqual(CT.masksToImage([{ x: 5000, y: 0, w: 10, h: 10 }], 100, 100, 100, 100), []);
  assert.deepEqual(CT.masksToImage([{ x: 1, y: 1, w: 1, h: 1 }], 0, 100, 100, 100), []);
  assert.deepEqual(CT.masksToImage(undefined, 100, 100, 100, 100), []);
});

test("splitDataUrl takes JPEG and PNG data URLs only", () => {
  assert.deepEqual(CT.splitDataUrl("data:image/jpeg;base64,/9j/4AA="), { media_type: "image/jpeg", data: "/9j/4AA=" });
  assert.deepEqual(CT.splitDataUrl("data:image/png;base64,iVBORw=="), { media_type: "image/png", data: "iVBORw==" });
  for (const bad of ["", null, "data:image/gif;base64,R0lG", "data:image/jpeg,raw", "https://x/y.jpg",
                     "data:image/jpeg;base64,", "data:image/jpeg;base64,ab cd"]) {
    assert.throws(() => CT.splitDataUrl(bad), /base64 JPEG or PNG/);
  }
});

test("pageUrl drops the query and the fragment", () => {
  assert.equal(CT.pageUrl({ origin: "https://example.com", pathname: "/reset", search: "?token=x", hash: "#a" }),
               "https://example.com/reset");
  assert.equal(CT.pageUrl({ origin: "null", protocol: "file:", host: "", pathname: "/Users/me/a.html" }),
               "file:///Users/me/a.html");
  assert.equal(CT.pageUrl(null), "");
});

test("buildScan has exactly the PROTOCOL-v3 fields and at most 200 hints", () => {
  const hints = Array.from({ length: 250 }, (_, id) => ({ id, x: 0, y: 0, w: 1, h: 1, role: "button", text: "", tag: "button" }));
  const msg = CT.buildScan(4, { media_type: "image/jpeg", data: "/9j/", w: 1280, h: 725, extra: 1 },
                           { w: 1512, h: 857, dpr: 2, junk: true }, "https://example.com/" + "x".repeat(3000), "T".repeat(300), hints);
  assert.deepEqual(Object.keys(msg).sort(), ["hints", "image", "scan_id", "title", "type", "url", "viewport"]);
  assert.equal(msg.type, "scan");
  assert.deepEqual(msg.image, { media_type: "image/jpeg", data: "/9j/", w: 1280, h: 725 });
  assert.deepEqual(msg.viewport, { w: 1512, h: 857, dpr: 2 });
  assert.equal(msg.hints.length, 200);
  assert.equal(msg.url.length, 2048);
  assert.equal(msg.title.length, 200);
  assert.deepEqual(CT.buildScan(1, { media_type: "image/png", data: "iVBO", w: 1, h: 1 }, { w: 1, h: 1, dpr: 1 }).hints, []);
});

// ------------------------------------------------------------------ the agent

function agentWorld(over = {}) {
  const log = [];
  const sent = [];
  let blocked = null;
  let sleepCalls = [];
  const deps = {
    send: (m) => { sent.push(m); log.push("send:" + m.type); return true; },
    sleep: (ms) => { sleepCalls.push(ms); log.push("sleep:" + ms); return Promise.resolve(); },
    blocked: () => blocked,
    collect: () => { log.push("collect"); return { hints: [{ id: 0, x: 1, y: 2, w: 3, h: 4, role: "button", text: "OK", tag: "button" }],
                                                    masks: [{ x: 10, y: 10, w: 100, h: 20 }], viewport: { w: 1512, h: 857, dpr: 2 } }; },
    hide: (h) => log.push("hide:" + h),
    afterFrames: () => { log.push("frames"); return Promise.resolve(); },
    capture: async () => { log.push("capture"); return "data:image/jpeg;base64,/9j/BIG="; },
    encode: async (dataUrl, masks, viewport) => { log.push("encode"); return { media_type: "image/jpeg", data: "/9j/small", w: 1280, h: 725, masks, viewport, dataUrl }; },
    clickAt: (x, y) => { log.push("click:" + x + "," + y); return { ok: true, method: "events", detail: 'button "OK"' }; },
    url: () => "https://example.com/login",
    title: () => "Log in",
    activity: (t) => log.push("activity:" + t),
    clicked: (r) => log.push("clicked:" + r.ok),
    note: (t) => log.push("note:" + t),
    ...over,
  };
  const agent = CT.createAgent(deps);
  return { agent, deps, log, sent, block: (b) => { blocked = b; }, sleeps: () => sleepCalls };
}

test("scan_request -> settle, hide, two frames, hints, capture, show, encode with masks, scan", async () => {
  const w = agentWorld();
  const msg = await w.agent.handle({ type: "scan_request", scan_id: 4, settle_ms: 800 });
  assert.equal(w.sleeps()[0], 800);
  const order = w.log.filter((x) => !x.startsWith("activity") && !x.startsWith("sleep"));
  assert.deepEqual(order, ["hide:true", "frames", "collect", "capture", "hide:false", "encode", "send:scan"]);
  assert.deepEqual(w.sent, [msg]);
  assert.equal(msg.scan_id, 4);
  assert.deepEqual(msg.image, { media_type: "image/jpeg", data: "/9j/small", w: 1280, h: 725 });
  assert.deepEqual(msg.viewport, { w: 1512, h: 857, dpr: 2 });
  assert.equal(msg.url, "https://example.com/login");
  assert.equal(msg.title, "Log in");
  assert.equal(msg.hints.length, 1);
  assert.ok(w.log.includes("activity:Finding the buttons…"));
});

test("the encoder gets the password masks and the viewport of the same moment", async () => {
  let seen = null;
  const w = agentWorld({ encode: async (dataUrl, masks, viewport) => { seen = { dataUrl, masks, viewport }; return { media_type: "image/jpeg", data: "/9j/", w: 10, h: 5 }; } });
  await w.agent.handle({ type: "scan_request", scan_id: 1, settle_ms: 0 });
  assert.deepEqual(seen, { dataUrl: "data:image/jpeg;base64,/9j/BIG=", masks: [{ x: 10, y: 10, w: 100, h: 20 }],
                           viewport: { w: 1512, h: 857, dpr: 2 } });
});

test("settle_ms: missing or bad uses 800, it is clamped to 0..10000", async () => {
  for (const [given, want] of [[undefined, 800], ["5", 800], [-5, 0], [99999, 10000], [250, 250]]) {
    const w = agentWorld();
    await w.agent.handle({ type: "scan_request", scan_id: 1, settle_ms: given });
    assert.equal(w.sleeps()[0], want, String(given));
  }
});

test("a scan waits while the tab is hidden or paused, then goes on", async () => {
  let checks = 0;
  const w = agentWorld({ blocked: () => (++checks < 3 ? "the tab is hidden" : null) });
  const msg = await w.agent.handle({ type: "scan_request", scan_id: 2, settle_ms: 100 });
  assert.equal(msg.scan_id, 2);
  assert.deepEqual(w.sleeps(), [100, 500, 500, 300], "settle, two polls, then the frame wait cap");
});

test("a newer scan_request drops the older one's result", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  let first = true;
  const w = agentWorld({ capture: async () => { if (first) { first = false; await gate; } return "data:image/jpeg;base64,/9j/"; } });
  const p1 = w.agent.handle({ type: "scan_request", scan_id: 1, settle_ms: 0 });
  await settle();
  const p2 = w.agent.handle({ type: "scan_request", scan_id: 2, settle_ms: 0 });
  release();
  assert.equal(await p1, null);
  const m2 = await p2;
  assert.equal(m2.scan_id, 2);
  assert.deepEqual(w.sent.map((m) => m.scan_id), [2]);
  assert.equal(w.agent.latest(), 2);
});

test("a failed capture is tried once more; two failures send nothing and show the overlay again", async () => {
  let n = 0;
  const once = agentWorld({ capture: async () => { if (n++ === 0) throw new Error("quota"); return "data:image/jpeg;base64,/9j/"; } });
  const msg = await once.agent.handle({ type: "scan_request", scan_id: 3, settle_ms: 0 });
  assert.equal(msg.scan_id, 3);
  assert.ok(once.sleeps().includes(CT.CAPTURE_RETRY_MS));
  const twice = agentWorld({ capture: async () => { throw new Error("tab not visible"); } });
  assert.equal(await twice.agent.handle({ type: "scan_request", scan_id: 3, settle_ms: 0 }), null);
  assert.deepEqual(twice.sent, []);
  assert.equal(twice.log.filter((x) => x.startsWith("hide:")).at(-1), "hide:false");
  assert.ok(twice.log.some((x) => /^note:no screenshot for scan 3/.test(x)));
});

test("an encoding failure or a closed connection sends nothing", async () => {
  const bad = agentWorld({ encode: async () => { throw new Error("decode"); } });
  assert.equal(await bad.agent.handle({ type: "scan_request", scan_id: 1, settle_ms: 0 }), null);
  assert.deepEqual(bad.sent, []);
  const down = agentWorld({ send: () => false });
  assert.equal(await down.agent.handle({ type: "scan_request", scan_id: 1, settle_ms: 0 }), null);
  assert.ok(down.log.some((x) => /server connection is down/.test(x)));
});

test("click -> clicked, with the overlay hidden around the click", () => {
  const w = agentWorld();
  const reply = w.agent.handle({ type: "click", click_id: 2, x: 0.057, y: 0.1, element: { id: 3, label: "Sign in" } });
  assert.deepEqual(reply, { type: "clicked", click_id: 2, ok: true, method: "events", detail: 'button "OK"' });
  assert.deepEqual(w.sent, [reply]);
  const order = w.log.filter((x) => /^(hide|click):/.test(x));
  assert.deepEqual(order, ["hide:true", "click:0.057,0.1", "hide:false"]);
  assert.ok(w.log.includes('activity:Clicking "Sign in"…'));
});

test("click is refused while paused or hidden, and a throwing clicker still answers", () => {
  const w = agentWorld();
  w.block("paused by the user (Esc)");
  const r = w.agent.handle({ type: "click", click_id: 5, x: 0.5, y: 0.5, element: {} });
  assert.deepEqual(r, { type: "clicked", click_id: 5, ok: false, method: "none", detail: "not clicked: paused by the user (Esc)" });
  assert.ok(!w.log.some((x) => x.startsWith("click:")));
  const t = agentWorld({ clickAt: () => { throw new Error("boom"); } });
  const r2 = t.agent.handle({ type: "click", click_id: 6, x: 0.5, y: 0.5 });
  assert.equal(r2.ok, false);
  assert.match(r2.detail, /boom/);
  assert.equal(t.log.filter((x) => x.startsWith("hide:")).at(-1), "hide:false");
  const odd = agentWorld({ clickAt: () => ({ ok: "yes", method: 4, detail: "d".repeat(900) }) });
  const r3 = odd.agent.handle({ type: "click", click_id: 7, x: 0.5, y: 0.5 });
  assert.deepEqual([r3.ok, r3.method, r3.detail.length], [false, "none", 500]);
});

test("invalid scan_request and click messages are ignored; others pass through; stop ends it", async () => {
  const w = agentWorld();
  assert.equal(w.agent.handle({ type: "scan_request" }), null);
  assert.equal(w.agent.handle({ type: "scan_request", scan_id: -1 }), null);
  assert.equal(w.agent.handle({ type: "click", click_id: "2", x: 0, y: 0 }), null);
  assert.equal(w.agent.handle({ type: "layout" }), null);
  assert.equal(w.agent.handle(null), null);
  assert.deepEqual(w.sent, []);
  const p = w.agent.handle({ type: "scan_request", scan_id: 9, settle_ms: 0 });
  w.agent.stop();
  assert.equal(await p, null);
  assert.equal(w.agent.handle({ type: "click", click_id: 1, x: 0, y: 0 }), null);
  assert.deepEqual(w.sent, []);
});

// ------------------------------------------------------------- port transport

function fakeChrome() {
  const ports = [];
  const onMessage = [];
  const queries = [];
  const chrome = {
    runtime: {
      id: "abcdefghijklmnopabcdefghijklmnop", lastError: undefined,
      connect: ({ name }) => { const p = makeFakePort(name); ports.push(p); return p; },
      onMessage: { addListener: (f) => onMessage.push(f) },
      sendMessage: (msg, cb) => { queries.push({ msg, cb }); },
    },
  };
  // the worker posts to the content script through the port's onMessage listeners
  const fromWorker = (p, m) => { for (const f of p.onMsg) f(m); };
  return { chrome, ports, onMessage, queries, fromWorker };
}

test("transport routes internal messages and hands the rest to onMessage", () => {
  const fc = fakeChrome();
  const t = makeFakeTimers();
  const got = { msg: [], state: [], close: [], rtt: [], err: [] };
  const tr = CT.portTransport({ chrome: fc.chrome, timers: t.timers,
    onMessage: (m) => got.msg.push(m.type), onState: (s) => got.state.push(s), onClose: (c) => got.close.push(c.code),
    onRtt: (ms) => got.rtt.push(ms), onError: (e) => got.err.push(e) });
  assert.equal(tr.send({ type: "x" }), false, "no port yet");
  tr.connect();
  assert.equal(fc.ports.length, 1);
  assert.equal(fc.ports[0].name, "brainpoint");
  const p = fc.ports[0];
  fc.fromWorker(p, { type: "_state", state: "open" });
  fc.fromWorker(p, { type: "_close", code: 4002, reason: "", reconnect: false });
  fc.fromWorker(p, { type: "_rtt", rtt_ms: 12 });
  fc.fromWorker(p, { type: "_error", text: "bad url" });
  fc.fromWorker(p, { type: "_unknown" });
  fc.fromWorker(p, { type: "scan_request", scan_id: 1 });
  fc.fromWorker(p, "junk");
  assert.deepEqual(got, { msg: ["scan_request"], state: ["open"], close: [4002], rtt: [12], err: ["bad url"] });
  assert.equal(tr.send({ type: "pause" }), true);
  assert.deepEqual(p.got, [{ type: "pause" }]);
});

test("transport capture: request and answer, error answer, timeout, no port", async () => {
  const fc = fakeChrome();
  const t = makeFakeTimers();
  const tr = CT.portTransport({ chrome: fc.chrome, timers: t.timers });
  await assert.rejects(tr.capture(), /no connection/);
  tr.connect();
  const p = fc.ports[0];
  const c1 = tr.capture();
  const req = p.got.at(-1);
  assert.equal(req.type, "_capture");
  fc.fromWorker(p, { type: "_capture-result", reqId: req.reqId, dataUrl: "data:image/jpeg;base64,/9j/" });
  assert.equal(await c1, "data:image/jpeg;base64,/9j/");
  const c2 = tr.capture();
  fc.fromWorker(p, { type: "_capture-result", reqId: p.got.at(-1).reqId, error: "tab hidden" });
  await assert.rejects(c2, /tab hidden/);
  const c3 = tr.capture();
  t.advance(CT.CAPTURE_TIMEOUT_MS);
  await assert.rejects(c3, /too long/);
  fc.fromWorker(p, { type: "_capture-result", reqId: 999, dataUrl: "x" });   // stale: ignored
  const c4 = tr.capture();
  p.drop();
  await assert.rejects(c4, /went away/);
});

test("transport reopens a dropped port after 1, 2, 4, 5 s and says so", () => {
  const fc = fakeChrome();
  const t = makeFakeTimers();
  const states = [];
  let reconnects = 0;
  const tr = CT.portTransport({ chrome: fc.chrome, timers: t.timers, onState: (s) => states.push(s),
                                onReconnect: () => reconnects++ });
  tr.connect();
  tr.connect();
  assert.equal(fc.ports.length, 1, "connect is idempotent");
  fc.ports[0].drop();
  assert.deepEqual(states, ["closed"]);
  assert.deepEqual(t.timeouts(), [1000]);
  t.advance(1000);
  assert.equal(fc.ports.length, 2);
  assert.equal(reconnects, 1);
  fc.ports[1].drop();
  assert.deepEqual(t.timeouts(), [2000]);
  t.advance(2000);
  fc.ports[2].drop();
  assert.deepEqual(t.timeouts(), [4000]);
  t.advance(4000);
  fc.ports[3].drop();
  assert.deepEqual(t.timeouts(), [5000]);
  t.advance(5000);
  // a _state from the worker resets the backoff
  for (const f of fc.ports[4].onMsg) f({ type: "_state", state: "closed" });
  fc.ports[4].drop();
  assert.deepEqual(t.timeouts(), [1000]);
  tr.close();
  assert.equal(t.pending.size, 0);
  t.advance(10000);
  assert.equal(fc.ports.length, 5, "closed: no more ports");
  assert.equal(tr.connected(), false);
});

test("transport with a reloaded extension reports closed and an error", () => {
  const fc = fakeChrome();
  fc.chrome.runtime.id = undefined;
  const states = [], errs = [];
  const tr = CT.portTransport({ chrome: fc.chrome, timers: makeFakeTimers().timers,
                                onState: (s) => states.push(s), onError: (e) => errs.push(e) });
  tr.connect();
  assert.deepEqual(states, ["closed"]);
  assert.match(errs[0], /reload this page/);
});

// ------------------------------------------------------------- session wiring

function bootWorld() {
  const fc = fakeChrome();
  const pg = makeFakePage({ w: 1000, h: 500, dpr: 2 });
  const handle = CT.boot(pg.win, pg.doc, fc.chrome);
  const toggle = () => { let reply; for (const f of fc.onMessage) f({ type: "_toggle" }, {}, (r) => { reply = r; }); return reply; };
  return { fc, pg, handle, toggle };
}

test("boot asks the worker once and stays idle on a tab where BrainPoint does not run", () => {
  const w = bootWorld();
  assert.equal(w.fc.queries.length, 1);
  assert.deepEqual(w.fc.queries[0].msg, { type: "_query" });
  w.fc.queries[0].cb({ active: false });
  assert.equal(w.handle.session(), null);
  assert.equal(w.pg.html.children.length, 0, "no overlay on ordinary pages");
  assert.equal(w.fc.ports.length, 0, "no port, so no socket");
  assert.equal(CT.boot(w.pg.win, w.pg.doc, w.fc.chrome), null, "a second boot in the same page does nothing");
  w.fc.queries[0].cb(undefined);
});

test("toggle shows the warning; Start, then the refresh measurement, then _hello; toggle again sends _bye", () => {
  const w = bootWorld();
  w.fc.queries[0].cb({ active: false });
  assert.deepEqual(w.toggle(), { active: true });
  const s = w.handle.session();
  assert.ok(s);
  assert.equal(w.pg.html.children.length, 1);
  assert.equal(w.fc.ports.length, 0, "no connection before Start");
  s.view.host.find((n) => n.textContent === "Start").fire("click");
  w.pg.frames(150);
  assert.equal(w.fc.ports.length, 1);
  const p = w.fc.ports[0];
  const hello = p.got.find((m) => m.type === "_hello");
  assert.equal(hello.reconnect, false);
  assert.equal(hello.fields.mode, "point");
  assert.equal(hello.fields.takeover, false, "a first Start does not take over");
  assert.equal(hello.fields.accepted, true);
  assert.deepEqual(hello.fields.viewport, { w: 1000, h: 500, dpr: 2 });
  assert.ok(Math.abs(hello.fields.refresh_hz - 60) < 0.01);
  assert.equal(JSON.stringify(hello).includes("token"), false, "the page never sees the token");
  assert.deepEqual(w.toggle(), { active: false });
  assert.equal(p.got.at(-1).type, "_bye");
  assert.equal(p.disconnected, true);
  assert.equal(w.pg.html.children.length, 0);
  assert.equal(w.handle.session(), null);
});

test("a tab that runs BrainPoint resumes after navigation at once, with takeover for its old socket", () => {
  const w = bootWorld();
  w.fc.queries[0].cb({ active: true, refresh_hz: 59.95 });
  const s = w.handle.session();
  assert.ok(s);
  assert.equal(s.model.state.stage, "live");
  const modal = s.view.host.find((n) => n.className === "modal");
  assert.equal(modal.hidden, true, "no second warning in the same tab");
  const p = w.fc.ports[0];
  assert.deepEqual(p.got.map((m) => m.type), ["_hello"]);
  assert.equal(p.got[0].fields.takeover, true);
  assert.equal(p.got[0].fields.refresh_hz, 59.95);
});

test("session: server messages reach the model and the agent; the worker restart re-sends hello", async () => {
  const w = bootWorld();
  w.fc.queries[0].cb({ active: true, refresh_hz: 60 });
  const s = w.handle.session();
  const p = w.fc.ports[0];
  w.fc.fromWorker(p, { type: "_state", state: "open" });
  w.fc.fromWorker(p, { type: "welcome", protocol: 3, role: "controller", mode: "point", fake: false, waveform: "sine",
                       refresh_hz: 60, refresh_tolerance_hz: 1, freqs: [15, 17, 12, 20], max_slots: 4, detect: true });
  w.fc.fromWorker(p, { type: "phase", phase: "run", flicker: true, seconds: null, text: "" });
  w.fc.fromWorker(p, { type: "_rtt", rtt_ms: 21 });
  assert.equal(s.model.state.conn, "open");
  assert.equal(s.model.state.rttMs, 21);
  // a click while the user paused is answered with ok false and never clicks
  w.pg.win.fire("keydown", { key: "Escape" });
  assert.equal(p.got.at(-1).type, "pause");
  w.fc.fromWorker(p, { type: "click", click_id: 3, x: 0.5, y: 0.5, element: { label: "Buy" } });
  const clicked = p.got.at(-1);
  assert.deepEqual(clicked, { type: "clicked", click_id: 3, ok: false, method: "none", detail: "not clicked: paused by the user (Esc)" });
  // close codes reach the model
  w.fc.fromWorker(p, { type: "_state", state: "closed" });
  w.fc.fromWorker(p, { type: "_close", code: 4000, reason: "replaced", reconnect: false });
  assert.equal(s.model.state.ended.code, 4000);
  // Take over sends a fresh _hello with takeover
  s.view.host.find((n) => n.textContent === "Take over").fire("click");
  const again = p.got.at(-1);
  assert.equal(again.type, "_hello");
  assert.equal(again.reconnect, true);
  assert.equal(again.fields.takeover, true);
  // a worker restart drops the port; the new port re-sends hello with takeover
  p.drop();
  w.pg.timers.advance(1000);
  const p2 = w.fc.ports[1];
  assert.equal(p2.got[0].type, "_hello");
  assert.equal(p2.got[0].fields.takeover, true);
  await settle();
});

// ------------------------------------------------- clicks on a page that changed (review fix)

function pagedWorld() {
  let state = { href: "https://example.com/inbox", sx: 0, sy: 120, vw: 1512, vh: 857 };
  const hintEl = { name: "the OK button" };
  const expects = [];
  const w = agentWorld({
    page: () => ({ ...state }),
    collect: () => {
      const got = { hints: [{ id: 0, x: 1, y: 2, w: 3, h: 4, role: "button", text: "OK", tag: "button" }],
                    masks: [], viewport: { w: 1512, h: 857, dpr: 2 } };
      Object.defineProperty(got, "elements", { value: [hintEl], enumerable: false });
      return got;
    },
    clickAt: (x, y, expect) => { expects.push(expect); return { ok: true, method: "events", detail: "" }; },
  });
  return { w, hintEl, expects, set: (patch) => { state = { ...state, ...patch }; } };
}

const CLICK = (over = {}) => ({ type: "click", click_id: 9, x: 0.5, y: 0.5,
                                element: { id: 0, label: "OK", hint_id: 0, risky: false }, ...over });

test("a click passes the scanned hint's element on, so the clicker checks what is under the point", async () => {
  const { w, hintEl, expects } = pagedWorld();
  await w.agent.handle({ type: "scan_request", scan_id: 1, settle_ms: 0 });
  assert.equal(w.agent.handle(CLICK()).ok, true);
  assert.equal(w.agent.handle(CLICK({ element: { label: "x", hint_id: null } })).ok, true);
  assert.equal(w.agent.handle(CLICK({ element: { label: "x", hint_id: 7 } })).ok, true);
  assert.deepEqual(expects, [hintEl, null, null]);
});

test("a click is refused when the page scrolled, changed address or was resized since the scan", async () => {
  for (const [patch, why] of [[{ sy: 400 }, "the page scrolled"], [{ sx: 30 }, "the page scrolled"],
                              [{ href: "https://example.com/trash" }, "the page address changed"],
                              [{ vw: 800 }, "the window was resized"]]) {
    const { w, expects, set } = pagedWorld();
    await w.agent.handle({ type: "scan_request", scan_id: 1, settle_ms: 0 });
    set(patch);
    const r = w.agent.handle(CLICK());
    assert.equal(r.ok, false, why);
    assert.equal(r.method, "none");
    assert.match(r.detail, new RegExp(why + " since the screenshot"));
    assert.deepEqual(expects, [], "nothing was clicked");
  }
  const { w, set } = pagedWorld();
  await w.agent.handle({ type: "scan_request", scan_id: 1, settle_ms: 0 });
  set({ sy: 121 });                                          // within the slack
  assert.equal(w.agent.handle(CLICK()).ok, true);
});

test("a click with no scan of this page sent is refused", () => {
  const { w, expects } = pagedWorld();
  const r = w.agent.handle(CLICK());
  assert.equal(r.ok, false);
  assert.match(r.detail, /no screenshot of this page was sent/);
  assert.deepEqual(expects, []);
});

test("pageChange compares address, scroll and viewport", () => {
  const a = { href: "u", sx: 0, sy: 0, vw: 10, vh: 10 };
  assert.equal(CT.pageChange(a, { ...a }), null);
  assert.equal(CT.pageChange(a, { ...a, sy: CT.SCROLL_SLACK_PX }), null);
  assert.equal(CT.pageChange(a, { ...a, sy: CT.SCROLL_SLACK_PX + 1 }), "the page scrolled");
  assert.equal(CT.pageChange(null, a), "no screenshot of this page was sent");
  assert.deepEqual(CT.pageState({ location: { href: "https://x/y?q" }, scrollX: 3, scrollY: 4, innerWidth: 5, innerHeight: 6 }),
                   { href: "https://x/y?q", sx: 3, sy: 4, vw: 5, vh: 6 });
});

test("boot: an _esc forwarded by the worker (Esc in a sub-frame) pauses the running session", () => {
  const w = bootWorld();
  const esc = () => { for (const f of w.fc.onMessage) assert.equal(f({ type: "_esc" }, {}, () => {}), false); };
  esc();                                           // no session yet: ignored
  w.fc.queries[0].cb({ active: true, refresh_hz: 60 });
  const s = w.handle.session();
  const p = w.fc.ports[0];
  w.fc.fromWorker(p, { type: "_state", state: "open" });
  w.fc.fromWorker(p, { type: "welcome", protocol: 3, role: "controller", mode: "point", fake: false, waveform: "sine",
                       refresh_hz: 60, refresh_tolerance_hz: 1, freqs: [15, 17, 12, 20], max_slots: 4, detect: true });
  esc();
  assert.equal(s.model.state.paused, true);
  assert.equal(p.got.filter((m) => m.type === "pause").length, 1);
});
