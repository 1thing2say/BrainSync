// background.js in a vm context with a fake chrome API and a fake WebSocket: one socket per
// tab's port, and each socket talks only to its own port. No browser.
//     node --test extension/test/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SRC = readFileSync(new URL("../background.js", import.meta.url), "utf8");
const flush = () => new Promise((r) => setImmediate(r));

function load() {
  const onConnect = [];
  const sockets = [];
  const timers = [];
  class FakeWebSocket {
    static OPEN = 1;
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.sent = [];
      this.closed = false;
      sockets.push(this);
    }
    send(text) { this.sent.push(JSON.parse(text)); }
    close() { this.readyState = 3; this.closed = true; }
    open() { this.readyState = 1; this.onopen(); }
    message(obj) { this.onmessage({ data: typeof obj === "string" ? obj : JSON.stringify(obj) }); }
    drop() { this.readyState = 3; this.onclose(); }
  }
  const chrome = {
    runtime: { onConnect: { addListener: (f) => onConnect.push(f) } },
    storage: { local: { get: async () => ({}) }, onChanged: { addListener() {} } },
    debugger: {},
  };
  const ctx = {
    chrome, WebSocket: FakeWebSocket, console: { warn() {}, log() {} },
    setTimeout: (f, ms) => { timers.push({ f, ms }); return timers.length; },
    clearTimeout: () => {},
  };
  vm.runInNewContext(SRC, ctx);
  function port(tabId) {
    const p = {
      name: "brainscroll", sender: { tab: { id: tabId } }, got: [], onMsg: [], onDisc: [],
      postMessage(m) { this.got.push(m); },
      onMessage: { addListener: (f) => p.onMsg.push(f) },
      onDisconnect: { addListener: (f) => p.onDisc.push(f) },
      say(m) { for (const f of p.onMsg) f(m); },
      disconnect() { for (const f of p.onDisc) f(); },
    };
    for (const f of onConnect) f(p);
    return p;
  }
  return { sockets, timers, port };
}

const states = (p) => p.got.filter((m) => m.type === "_state").map((m) => m.state);

test("each tab gets its own socket, and a late tab its own config and phase", async () => {
  const { sockets, port } = load();
  const a = port(1);
  await flush();
  assert.equal(sockets.length, 1);
  sockets[0].open();
  sockets[0].message({ type: "config", name: "reels" });
  sockets[0].message({ type: "phase", phase: "run", flicker: true });
  const b = port(2);                                             // opened mid-run
  await flush();
  assert.equal(sockets.length, 2);
  sockets[1].open();
  sockets[1].message({ type: "config", name: "reels" });       // the bridge greets each one
  sockets[1].message({ type: "phase", phase: "run", flicker: true });
  assert.deepEqual(states(a), ["closed", "connecting", "open"]);
  assert.deepEqual(states(b), ["closed", "connecting", "open"]);
  assert.deepEqual(a.got.filter((m) => m.type !== "_state").map((m) => m.type), ["config", "phase"]);
  assert.deepEqual(b.got.filter((m) => m.type !== "_state").map((m) => m.type), ["config", "phase"]);
  sockets[0].message({ type: "scroll", dir: "down", id: 1 });
  assert.equal(b.got.filter((m) => m.type === "scroll").length, 0, "a's scroll stays in a");
});

test("port messages go to that port's socket only; internal ones never leave", async () => {
  const { sockets, port } = load();
  const a = port(1), b = port(2);
  await flush();
  sockets[0].open();
  sockets[1].open();
  a.say({ type: "hello", agent: "extension" });
  b.say({ type: "bye" });
  b.say({ type: "_nope" });
  b.say(null);
  assert.deepEqual(sockets[0].sent, [{ type: "hello", agent: "extension" }]);
  assert.deepEqual(sockets[1].sent, [{ type: "bye" }]);
});

test("closing a tab closes its socket only; a dropped socket reconnects with backoff", async () => {
  const { sockets, timers, port } = load();
  const a = port(1), b = port(2);
  await flush();
  sockets[0].open();
  sockets[1].open();
  a.disconnect();
  assert.equal(sockets[0].closed, true);
  assert.equal(sockets[1].closed, false);
  sockets[1].drop();                                             // Python stopped
  assert.deepEqual(states(b).slice(-1), ["closed"]);
  assert.deepEqual(timers.map((t) => t.ms), [1000]);
  timers[0].f();
  await flush();
  assert.equal(sockets.length, 3);
  assert.equal(sockets[2].url, "ws://127.0.0.1:8765");
});

test("malformed or internal server messages are not relayed", async () => {
  const { sockets, port } = load();
  const a = port(1);
  await flush();
  sockets[0].open();
  sockets[0].message("not json");
  sockets[0].message({ type: "_state", state: "open" });
  sockets[0].message([1, 2]);
  assert.deepEqual(a.got.filter((m) => m.type !== "_state"), []);
  assert.deepEqual(states(a), ["closed", "connecting", "open"]);
});
