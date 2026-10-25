// background.js in a vm context with a fake chrome API, a fake WebSocket and fake timers: one
// Connection per tab port, the hello and its token, forwarding both ways, screenshots of the
// sender's tab only, the toolbar toggle and the active-tab record. No browser, no network.
//     node --test clients/chrome/test/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { makeFakePort, makeFakeTimers, makeFakeWS, settle } from "./fakes.mjs";

// Objects made inside the vm have the vm's prototypes; compare them as plain JSON.
const plain = (x) => (x === undefined ? undefined : JSON.parse(JSON.stringify(x)));
const read = (name) => readFileSync(new URL("../" + name, import.meta.url), "utf8");
const TOKEN = "s3cret-token-xyz";
const WELCOME = { type: "welcome", protocol: 3, role: "controller", mode: "point", fake: true,
                  waveform: "sine", refresh_hz: 60.0, refresh_tolerance_hz: 1.0,
                  freqs: [15, 17, 12, 20], max_slots: 4, detect: true };
const FIELDS = { role: "controller", takeover: false, client: { name: "brainpoint-extension", kind: "extension", version: "0.1.0" },
                 mode: "point", slots: null, refresh_hz: 59.98, accepted: true, viewport: { w: 1512, h: 857, dpr: 2 } };

function load({ local = { serverUrl: "ws://box.tail:8765/ws", token: TOKEN }, tabs } = {}) {
  const L = { connect: [], message: [], action: [], removed: [], changed: [] };
  const session = {};
  const captures = [];
  const sentToTabs = [];
  const warnings = [];
  const tabTable = tabs || { 7: { id: 7, windowId: 1, active: true }, 8: { id: 8, windowId: 2, active: true } };
  let captureImpl = async () => "data:image/jpeg;base64,/9j/4AAQ";
  let sendImpl = async () => ({ active: true });
  const chrome = {
    runtime: { id: "x".repeat(32), onConnect: { addListener: (f) => L.connect.push(f) },
               onMessage: { addListener: (f) => L.message.push(f) } },
    storage: {
      local: { get: async (keys) => Object.fromEntries((Array.isArray(keys) ? keys : [keys]).filter((k) => k in local).map((k) => [k, local[k]])) },
      session: {
        get: async (k) => (k in session ? { [k]: session[k] } : {}),
        set: async (o) => { Object.assign(session, JSON.parse(JSON.stringify(o))); },
        remove: async (k) => { delete session[k]; },
      },
      onChanged: { addListener: (f) => L.changed.push(f) },
    },
    tabs: {
      get: async (id) => { if (!(id in tabTable)) throw new Error("No tab with id " + id); return { ...tabTable[id] }; },
      captureVisibleTab: (windowId, opts) => { captures.push(plain({ windowId, opts })); return captureImpl(windowId, opts); },
      sendMessage: (id, msg) => { sentToTabs.push(plain({ id, msg })); return sendImpl(id, msg); },
      onRemoved: { addListener: (f) => L.removed.push(f) },
    },
    action: { onClicked: { addListener: (f) => L.action.push(f) } },
  };
  const { FakeWebSocket, sockets } = makeFakeWS();
  const t = makeFakeTimers();
  const ctx = vm.createContext({
    chrome, WebSocket: FakeWebSocket, console: { warn: (...a) => warnings.push(a.join(" ")), log() {} },
    setTimeout: t.timers.setTimeout, clearTimeout: t.timers.clearTimeout,
    setInterval: t.timers.setInterval, clearInterval: t.timers.clearInterval,
  });
  const loaded = [];
  ctx.importScripts = (...names) => { for (const n of names) { loaded.push(n); vm.runInContext(read(n), ctx, { filename: n }); } };
  vm.runInContext(read("background.js"), ctx, { filename: "background.js" });
  function port(tabId = 7, extra = {}) {
    const p = makeFakePort("brainpoint", { tab: { id: tabId, windowId: tabTable[tabId] ? tabTable[tabId].windowId : 0 }, frameId: 0, ...extra });
    for (const f of L.connect) f(p);
    return p;
  }
  function query(tabId, frameId = 0) {
    return new Promise((resolve) => {
      let async = false;
      for (const f of L.message) {
        const r = f({ type: "_query" }, { tab: { id: tabId }, frameId }, (v) => resolve(plain(v)));
        if (r === true) async = true;
      }
      if (!async) setImmediate(() => resolve("no answer"));
    });
  }
  return { L, session, local, captures, sentToTabs, warnings, sockets, timers: t, loaded, port, query, tabTable,
           setCapture: (f) => { captureImpl = f; }, setSend: (f) => { sendImpl = f; } };
}

async function connected(w, tabId = 7, fields = FIELDS) {
  const p = w.port(tabId);
  p.say({ type: "_hello", fields });
  await settle();
  const ws = w.sockets.at(-1);
  ws.accept();
  return { p, ws };
}

test("loads the SDK with importScripts and posts closed on a new port; nothing opens before _hello", async () => {
  const w = load();
  assert.deepEqual(w.loaded, ["flicker.js", "brainsync-client.js"]);
  const p = w.port();
  await settle();
  assert.deepEqual(p.got, [{ type: "_state", state: "closed" }]);
  assert.equal(w.sockets.length, 0);
  p.say({ type: "pause" });
  assert.match(w.warnings.at(-1), /not connected to the server; dropped pause/);
});

test("_hello opens the stored address and sends hello with protocol 3 and the token", async () => {
  const w = load();
  const { p, ws } = await connected(w);
  assert.equal(ws.url, "ws://box.tail:8765/ws");
  const instance = ws.sent[0].client.instance;
  assert.equal(typeof instance, "string");
  assert.deepEqual(ws.sent[0], { ...FIELDS, client: { ...FIELDS.client, instance }, type: "hello",
                                 protocol: 3, token: TOKEN });
  assert.deepEqual(p.types().slice(0, 3), ["_state", "_state", "_state"]);
  assert.deepEqual(p.got.slice(1, 3).map((m) => m.state), ["connecting", "open"]);
  assert.ok(!JSON.stringify(p.got).includes(TOKEN), "the token never reaches the page");
  assert.ok(!w.warnings.join("\n").includes(TOKEN), "the token is never logged");
});

test("defaults: no settings means ws://127.0.0.1:8765/ws and a null token", async () => {
  const w = load({ local: {} });
  const { ws } = await connected(w);
  assert.equal(ws.url, "ws://127.0.0.1:8765/ws");
  assert.equal(ws.sent[0].token, null);
  const blank = load({ local: { serverUrl: "  ", token: "   " } });
  const b = await connected(blank);
  assert.equal(b.ws.url, "ws://127.0.0.1:8765/ws");
  assert.equal(b.ws.sent[0].token, null);
});

test("a bad server address is reported to the page, not thrown", async () => {
  const w = load({ local: { serverUrl: "http://nope" } });
  const p = w.port();
  p.say({ type: "_hello", fields: FIELDS });
  await settle();
  assert.equal(w.sockets.length, 0);
  const err = p.got.find((m) => m.type === "_error");
  assert.match(err.text, /ws:\/\/ or wss:\/\//);
  assert.match(err.text, /BrainPoint options/);
});

test("server messages go to the port as is; page messages go to the server; internal types stay inside", async () => {
  const w = load();
  const { p, ws } = await connected(w);
  ws.message(WELCOME);
  ws.message({ type: "scan_request", scan_id: 4, settle_ms: 800 });
  ws.message({ type: "_capture-result", reqId: 1, dataUrl: "evil" });
  ws.message("not json");
  const types = p.types().filter((t) => !t.startsWith("_"));
  assert.deepEqual(types, ["welcome", "scan_request"]);
  assert.ok(!p.got.some((m) => m.dataUrl === "evil"), "a server cannot fake an internal message");
  p.say({ type: "scan", scan_id: 4, image: {} });
  p.say({ type: "_nonsense" });
  p.say(null);
  p.say({ type: 5 });
  assert.equal(ws.sent.filter((m) => m.type === "scan").length, 1);
  assert.equal(ws.sent.filter((m) => m.type === "_nonsense").length, 0);
});

test("takeover goes in one handshake only; automatic reconnects send takeover false", async () => {
  const w = load();
  const { ws } = await connected(w, 7, { ...FIELDS, takeover: true });
  assert.equal(ws.sent[0].takeover, true);
  ws.message(WELCOME);
  ws.drop(1006);
  w.timers.advance(1000);
  const ws2 = w.sockets.at(-1);
  assert.notEqual(ws2, ws);
  ws2.accept();
  assert.equal(ws2.sent[0].type, "hello");
  assert.equal(ws2.sent[0].takeover, false);
});

test("pings start on welcome and every pong reports the round trip", async () => {
  const w = load();
  const { p, ws } = await connected(w);
  ws.message(WELCOME);
  const ping = ws.sent.find((m) => m.type === "ping");
  assert.ok(ping);
  ws.message({ type: "pong", ping_id: ping.ping_id, t_client: ping.t_client, t_server: 1 });
  const rtt = p.got.find((m) => m.type === "_rtt");
  assert.ok(Number.isInteger(rtt.rtt_ms) && rtt.rtt_ms >= 0 && rtt.rtt_ms < 1000, String(rtt.rtt_ms));
  ws.message({ type: "pong", ping_id: 999, t_client: 0, t_server: 1 });
  assert.equal(p.got.filter((m) => m.type === "_rtt").length, 2, "every pong is reported, even an unknown one");
});

test("one tab keeps one client.instance across replaced sockets and settings changes; tabs differ", async () => {
  const w = load();
  const { p, ws } = await connected(w);
  const id = ws.sent[0].client.instance;
  p.say({ type: "_hello", fields: FIELDS, reconnect: true });          // explicit replace
  await settle();
  w.sockets[1].accept();
  for (const f of w.L.changed) f({ token: { newValue: "t2" } }, "local");   // settings changed
  await settle();
  w.sockets[2].accept();
  w.sockets[2].drop();                                                    // automatic reconnect
  w.timers.advance(1000);
  w.sockets[3].accept();
  assert.deepEqual(w.sockets.map((s) => s.sent[0].client.instance), [id, id, id, id]);
  const other = await connected(w, 8);
  assert.notEqual(other.ws.sent[0].client.instance, id);
});

test("_hello again only updates the fields; reconnect: true replaces the socket with a fresh hello", async () => {
  const w = load();
  const { p, ws } = await connected(w);
  p.say({ type: "_hello", fields: { ...FIELDS, refresh_hz: 60.01 } });
  await settle();
  assert.equal(w.sockets.length, 1);
  p.say({ type: "_hello", fields: { ...FIELDS, refresh_hz: 60.02, takeover: true }, reconnect: true });
  await settle();
  assert.equal(w.sockets.length, 2);
  assert.deepEqual(ws.closedWith, { code: 1000, reason: "bye" });
  assert.equal(ws.sent.filter((m) => m.type === "bye").length, 0, "a replaced socket sends no bye");
  const ws2 = w.sockets[1];
  ws2.accept();
  assert.equal(ws2.sent[0].refresh_hz, 60.02);
  assert.equal(ws2.sent[0].takeover, true);
  // events of the old socket no longer reach the page
  const before = p.got.length;
  ws.message({ type: "phase", phase: "run", flicker: true });
  assert.equal(p.got.length, before);
  p.say({ type: "_hello" });
  assert.match(w.warnings.at(-1), /without fields/);
});

test("_capture photographs the sender's window as JPEG 80, only while its tab is active", async () => {
  const w = load();
  const { p } = await connected(w);
  p.say({ type: "_capture", reqId: 1 });
  await settle();
  assert.deepEqual(w.captures, [{ windowId: 1, opts: { format: "jpeg", quality: 80 } }]);
  assert.deepEqual(p.got.at(-1), { type: "_capture-result", reqId: 1, dataUrl: "data:image/jpeg;base64,/9j/4AAQ" });

  w.tabTable[7].active = false;
  p.say({ type: "_capture", reqId: 2 });
  await settle();
  assert.equal(w.captures.length, 1, "no capture of another tab");
  assert.match(p.got.at(-1).error, /not the visible tab/);

  w.tabTable[7].active = true;
  w.setCapture(async () => { w.tabTable[7].active = false; return "data:image/jpeg;base64,/9j/"; });
  p.say({ type: "_capture", reqId: 3 });
  await settle();
  assert.equal(p.got.at(-1).dataUrl, undefined, "a capture taken while the tab was switched away is dropped");
  assert.match(p.got.at(-1).error, /stopped being visible/);

  w.tabTable[7].active = true;
  w.setCapture(async () => { throw new Error("MAX_CAPTURE_VISIBLE_TAB_CALLS_PER_SECOND"); });
  p.say({ type: "_capture", reqId: 4 });
  await settle();
  assert.match(p.got.at(-1).error, /captureVisibleTab failed: MAX_CAPTURE/);

  w.setCapture(async () => undefined);
  p.say({ type: "_capture", reqId: 5 });
  await settle();
  assert.match(p.got.at(-1).error, /no image/);

  const orphan = w.port(99);
  orphan.say({ type: "_capture", reqId: 6 });
  await settle();
  assert.match(orphan.got.at(-1).error, /not the visible tab/);
});

test("welcome as controller remembers the tab; _query answers per tab; _bye forgets it", async () => {
  const w = load();
  assert.deepEqual(await w.query(7), { active: false, refresh_hz: null });
  const { p, ws } = await connected(w);
  ws.message(WELCOME);
  await settle();
  assert.deepEqual(w.session.activeTab, { id: 7, refresh_hz: 59.98 });
  assert.deepEqual(await w.query(7), { active: true, refresh_hz: 59.98 });
  assert.deepEqual(await w.query(8), { active: false, refresh_hz: null });
  assert.deepEqual(await w.query(7, 3), { active: false, refresh_hz: null }, "frames never resume");
  p.say({ type: "_bye" });
  await settle();
  assert.deepEqual(ws.sent.at(-1), { type: "bye" });
  assert.deepEqual(ws.closedWith, { code: 1000, reason: "bye" });
  assert.equal(w.session.activeTab, undefined);
  w.timers.advance(60000);
  assert.equal(w.sockets.length, 1, "no reconnect after bye");
});

test("a viewer welcome does not mark the tab active", async () => {
  const w = load();
  const { ws } = await connected(w);
  ws.message({ ...WELCOME, role: "viewer" });
  await settle();
  assert.equal(w.session.activeTab, undefined);
});

test("only one active tab: the newest controller wins, and a takeover close clears only the loser", async () => {
  const w = load();
  const a = await connected(w, 7);
  a.ws.message(WELCOME);
  await settle();
  const b = await connected(w, 8, { ...FIELDS, takeover: true });
  b.ws.message(WELCOME);
  a.ws.drop(4000, "replaced");
  await settle();
  assert.deepEqual(w.session.activeTab, { id: 8, refresh_hz: 59.98 });
  const close = a.p.got.find((m) => m.type === "_close");
  assert.deepEqual(close, { type: "_close", code: 4000, reason: "replaced", reconnect: false });
  w.timers.advance(60000);
  assert.equal(w.sockets.length, 2, "4000 never reconnects");
});

test("close codes 4001 and 4002 clear the record and stop; 1006 reconnects", async () => {
  for (const code of [4001, 4002]) {
    const w = load();
    const { p, ws } = await connected(w);
    ws.message(WELCOME);
    await settle();
    ws.drop(code);
    await settle();
    assert.equal(w.session.activeTab, undefined, String(code));
    assert.equal(p.got.find((m) => m.type === "_close").reconnect, false);
    w.timers.advance(60000);
    assert.equal(w.sockets.length, 1);
  }
  const w = load();
  const { p, ws } = await connected(w);
  ws.message(WELCOME);
  await settle();
  ws.drop(1006);
  await settle();
  assert.deepEqual(w.session.activeTab, { id: 7, refresh_hz: 59.98 }, "a network drop keeps the tab active");
  assert.equal(p.got.find((m) => m.type === "_close").reconnect, true);
  w.timers.advance(1000);
  assert.equal(w.sockets.length, 2);
});

test("the port going away (navigation) closes the socket but keeps the tab active for the next page", async () => {
  const w = load();
  const { p, ws } = await connected(w);
  ws.message(WELCOME);
  await settle();
  p.drop();
  assert.equal(ws.readyState, 3);
  assert.equal(ws.sent.filter((m) => m.type === "bye").length, 0);
  w.timers.advance(60000);
  assert.equal(w.sockets.length, 1, "no reconnect for a gone port");
  assert.deepEqual(await w.query(7), { active: true, refresh_hz: 59.98 });
  // a _hello still on its way when the port went opens nothing
  const q = w.port(7);
  q.say({ type: "_hello", fields: FIELDS });
  q.drop();
  await settle();
  assert.equal(w.sockets.length, 1);
});

test("closing the tab forgets it", async () => {
  const w = load();
  const { ws } = await connected(w);
  ws.message(WELCOME);
  await settle();
  for (const f of w.L.removed) f(8);
  await settle();
  assert.equal(w.session.activeTab.id, 7);
  for (const f of w.L.removed) f(7);
  await settle();
  assert.equal(w.session.activeTab, undefined);
});

test("the toolbar icon toggles BrainPoint in its tab, and a tab without the script is only a note", async () => {
  const w = load();
  for (const f of w.L.action) f({ id: 7, windowId: 1 });
  assert.deepEqual(w.sentToTabs, [{ id: 7, msg: { type: "_toggle" } }]);
  w.setSend(async () => { throw new Error("Could not establish connection. Receiving end does not exist."); });
  for (const f of w.L.action) f({ id: 9 });
  await settle();
  assert.match(w.warnings.at(-1), /cannot run in this tab/);
  for (const f of w.L.action) f({});
  assert.equal(w.sentToTabs.length, 2);
});

test("a new address or token in the options reconnects every running tab", async () => {
  const w = load();
  const { ws } = await connected(w);
  const idle = w.port(8);   // a port that never sent _hello keeps no socket
  w.local.serverUrl = "wss://new.host/ws";
  w.local.token = "other-token";
  for (const f of w.L.changed) f({ serverUrl: { newValue: "wss://new.host/ws" } }, "local");
  await settle();
  assert.equal(w.sockets.length, 2);
  assert.equal(ws.readyState, 3);
  const ws2 = w.sockets[1];
  assert.equal(ws2.url, "wss://new.host/ws");
  ws2.accept();
  assert.equal(ws2.sent[0].token, "other-token");
  for (const f of w.L.changed) f({ other: {} }, "local");
  for (const f of w.L.changed) f({ token: {} }, "sync");
  await settle();
  assert.equal(w.sockets.length, 2);
  assert.ok(idle);
});

test("ports with another name or from a subframe are not served", async () => {
  const w = load();
  const other = makeFakePort("brainscroll", { tab: { id: 7 }, frameId: 0 });
  for (const f of w.L.connect) f(other);
  assert.deepEqual(other.got, []);
  const sub = w.port(7, { frameId: 2 });
  assert.equal(sub.disconnected, true);
  assert.deepEqual(sub.got, []);
});

test("Esc in a sub-frame: pause goes to the server on that tab's socket and _esc to its top frame", async () => {
  const w = load();
  const { ws } = await connected(w, 7);
  const esc = (tabId, frameId) => { for (const f of w.L.message) f({ type: "_esc" }, { tab: { id: tabId }, frameId }, () => {}); };
  esc(7, 4);
  assert.deepEqual(ws.sent.filter((m) => m.type === "pause"), [{ type: "pause" }]);
  assert.deepEqual(w.sentToTabs, [{ id: 7, msg: { type: "_esc" } }]);
  esc(8, 2);                                     // a tab without BrainPoint: nothing happens
  esc(undefined, 2);
  assert.equal(w.sentToTabs.length, 1);
  assert.equal(ws.sent.filter((m) => m.type === "pause").length, 1);
});
