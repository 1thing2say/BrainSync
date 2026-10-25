// options.js: address checks, token cleaning, and the form against a fake chrome.storage.
//     node --test clients/chrome/test/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { FakeNode, settle } from "./fakes.mjs";

const require = createRequire(import.meta.url);
const P = require("../options.js");

test("checkUrl accepts ws:// and wss://, fills in /ws, and says how to fix anything else", () => {
  assert.deepEqual(P.checkUrl(""), { url: "ws://127.0.0.1:8765/ws" });
  assert.deepEqual(P.checkUrl(undefined), { url: P.DEFAULT_URL });
  assert.deepEqual(P.checkUrl("  ws://127.0.0.1:8765/ws  "), { url: "ws://127.0.0.1:8765/ws" });
  assert.deepEqual(P.checkUrl("ws://127.0.0.1:9000"), { url: "ws://127.0.0.1:9000/ws" });
  assert.deepEqual(P.checkUrl("wss://ripe-banana.trycloudflare.com/ws"), { url: "wss://ripe-banana.trycloudflare.com/ws" });
  assert.match(P.checkUrl("http://127.0.0.1:8765/ws").error, /ws:\/\/ or wss:\/\//);
  assert.match(P.checkUrl("127.0.0.1:8765").error, /ws:\/\/ or wss:\/\/|not an address/);
  assert.match(P.checkUrl("not a url").error, /not an address/);
  assert.match(P.checkUrl("ws://user:pw@host/ws").error, /Token field/);
  assert.match(P.checkUrl("ws://host/ws#token=abc").error, /Token field/);
});

test("cleanToken trims and turns blank into null", () => {
  assert.equal(P.cleanToken("  abc  "), "abc");
  assert.equal(P.cleanToken("   "), null);
  assert.equal(P.cleanToken(null), null);
  assert.equal(P.cleanToken(undefined), null);
});

function form(stored) {
  const nodes = {};
  for (const id of ["serverUrl", "token", "status", "form", "reset"]) { nodes[id] = new FakeNode("x"); nodes[id].value = ""; }
  const doc = { getElementById: (id) => nodes[id] };
  const store = { ...stored };
  const removed = [];
  const chrome = { storage: { local: {
    get: async (keys) => Object.fromEntries(keys.filter((k) => k in store).map((k) => [k, store[k]])),
    set: async (o) => { Object.assign(store, o); },
    remove: async (k) => { removed.push(k); delete store[k]; },
  } } };
  P.start(doc, chrome);
  return { nodes, store, removed };
}

test("the form loads the settings and saves a checked address and the token", async () => {
  const { nodes, store } = form({ serverUrl: "ws://10.0.0.2:8765/ws", token: "tok" });
  await settle();
  assert.equal(nodes.serverUrl.value, "ws://10.0.0.2:8765/ws");
  assert.equal(nodes.token.value, "tok");
  nodes.serverUrl.value = "wss://ripe-banana.trycloudflare.com";
  nodes.token.value = "  new-token ";
  nodes.form.fire("submit");
  await settle();
  assert.deepEqual(store, { serverUrl: "wss://ripe-banana.trycloudflare.com/ws", token: "new-token" });
  assert.equal(nodes.serverUrl.value, "wss://ripe-banana.trycloudflare.com/ws");
  assert.match(nodes.status.textContent, /^Saved/);
  assert.ok(!nodes.status.textContent.includes("new-token"), "the token is never shown back");
  assert.equal(nodes.status.className, "ok");
});

test("a blank token is removed, a bad address is refused, and reset fills in the default", async () => {
  const { nodes, store, removed } = form({ token: "old" });
  await settle();
  assert.equal(nodes.serverUrl.value, P.DEFAULT_URL);
  nodes.token.value = "";
  nodes.form.fire("submit");
  await settle();
  assert.deepEqual(removed, ["token"]);
  assert.deepEqual(store, { serverUrl: P.DEFAULT_URL });
  nodes.serverUrl.value = "http://x";
  nodes.form.fire("submit");
  await settle();
  assert.equal(nodes.status.className, "bad");
  assert.equal(store.serverUrl, P.DEFAULT_URL);
  nodes.serverUrl.value = "ws://elsewhere/ws";
  nodes.reset.fire("click");
  assert.equal(nodes.serverUrl.value, P.DEFAULT_URL);
});
