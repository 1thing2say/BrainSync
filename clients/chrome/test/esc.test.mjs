// esc.js on plain objects: Esc heard on the window and the document in the capture phase, one
// action per key press, handlers in the top frame, a message to the worker from a sub-frame.
//     node --test clients/chrome/test/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { makeFakePage } from "./fakes.mjs";

const require = createRequire(import.meta.url);
const E = require("../esc.js");

function captureListeners(target) {
  const got = [];
  const add = target.addEventListener.bind(target);
  target.addEventListener = (type, f, capture) => { got.push([type, capture]); add(type, f, capture); };
  return got;
}

test("top frame: Esc on the window or the document calls the handlers once per key press", () => {
  const pg = makeFakePage();
  const onWin = captureListeners(pg.win), onDoc = captureListeners(pg.doc);
  const hub = E.install({ win: pg.win, doc: pg.doc, top: true, send: () => assert.fail("no message from the top") });
  assert.deepEqual(onWin, [["keydown", true]]);
  assert.deepEqual(onDoc, [["keydown", true]]);
  const calls = [];
  const off = hub.on(() => calls.push("a"));
  hub.on(() => { throw new Error("broken"); });
  hub.on(() => calls.push("b"));
  const ev = { key: "Escape" };
  pg.win.listeners.keydown[0](ev);
  pg.doc.listeners.keydown[0](ev);                       // the same event, further down the path
  assert.deepEqual(calls, ["a", "b"]);
  pg.doc.fire("keydown", { key: "Esc" });
  pg.doc.fire("keydown", { keyCode: 27 });
  pg.doc.fire("keydown", { key: "a" });
  assert.deepEqual(calls, ["a", "b", "a", "b", "a", "b"]);
  off();
  pg.doc.fire("keydown", { key: "Escape" });
  assert.deepEqual(calls.slice(6), ["b"]);
  hub.remove();
  assert.equal(pg.win.listeners.keydown.length, 0);
  assert.equal(pg.doc.listeners.keydown.length, 0);
});

test("sub-frame: Esc sends _esc to the worker and nothing else", () => {
  const pg = makeFakePage();
  const sent = [];
  const hub = E.install({ win: pg.win, doc: pg.doc, top: false, send: (m) => sent.push(m) });
  hub.on(() => assert.fail("handlers run in the top frame only"));
  pg.win.fire("keydown", { key: "Escape" });
  pg.win.fire("keydown", { key: "Enter" });
  assert.deepEqual(sent, [{ type: "_esc" }]);
  const broken = E.install({ win: pg.win, doc: pg.doc, top: false, send: () => { throw new Error("reloaded"); } });
  assert.equal(broken.handle({ key: "Escape" }), true);  // a dead extension context does not throw
});

test("isTop and isEsc", () => {
  const w = {};
  w.top = w;
  assert.equal(E.isTop(w), true);
  assert.equal(E.isTop({ top: {} }), false);
  assert.equal(E.isTop({ get top() { throw new Error("cross-origin"); } }), false);
  assert.equal(E.isEsc({ key: "Escape" }), true);
  assert.equal(E.isEsc({ key: "Tab" }), false);
  assert.equal(E.isEsc(null), false);
});
