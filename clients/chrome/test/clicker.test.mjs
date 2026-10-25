// clicker.js on plain objects: point mapping, the event sequence, focus, shadow roots, frames,
// and the refusals (overlay, nothing there, cross-origin frame). No DOM, no browser.
//     node --test clients/chrome/test/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const K = require("../clicker.js");

class FakeEvent {
  constructor(type, init) { this.type = type; Object.assign(this, init); this.kind = "mouse"; }
}
class FakePointerEvent extends FakeEvent {
  constructor(type, init) { super(type, init); this.kind = "pointer"; }
}

function node(tag, { attrs = {}, text = "", parent = null, focusable = false, rect } = {}) {
  const n = {
    tagName: tag.toUpperCase(), attrs, innerText: text, parent, events: [], focused: 0, focusable, rect,
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    dispatchEvent(ev) { this.events.push(ev); for (let p = this.parent; p; p = p.parent) p.events.push(ev); return true; },
    focus(opts) { this.focused++; this.focusOpts = opts; },
    closest() { for (let p = this; p; p = p.parent) if (p.focusable) return p; return null; },
    contains(o) { for (; o; o = o.parent) if (o === this) return true; return false; },
    getBoundingClientRect() { return this.rect || { left: 0, top: 0 }; },
    clicks: 0,
    click() { this.clicks++; },
  };
  return n;
}

function world(hitFn, { w = 1000, h = 500 } = {}) {
  const calls = [];
  const doc = { elementFromPoint(x, y) { calls.push([x, y]); return hitFn(x, y); } };
  const win = { innerWidth: w, innerHeight: h, document: doc, MouseEvent: FakeEvent, PointerEvent: FakePointerEvent };
  return { doc, win, calls };
}

test("toPx maps normalized points and keeps them on the last pixel", () => {
  assert.deepEqual(K.toPx(0.5, 0.25, 1000, 500), { x: 500, y: 125 });
  assert.deepEqual(K.toPx(1, 1, 1000, 500), { x: 999, y: 499 });
  assert.deepEqual(K.toPx(0, 0, 1000, 500), { x: 0, y: 0 });
  for (const [x, y] of [[-0.01, 0.5], [0.5, 1.01], [NaN, 0], [0, Infinity], ["0.5", 0.5]]) {
    assert.throws(() => K.toPx(x, y, 1000, 500), RangeError);
  }
  assert.throws(() => K.toPx(0.5, 0.5, 0, 500), /positive size/);
});

test("clickAt focuses and fires pointer and mouse events with the point's coordinates", () => {
  const form = node("form");
  const button = node("button", { parent: form, focusable: true, text: "Sign in" });
  const span = node("span", { parent: button, text: "Sign in" });
  const { doc, win, calls } = world(() => span);
  const r = K.clickAt(0.057, 0.1, { win, doc });
  assert.deepEqual(r, { ok: true, method: "events", detail: 'span "Sign in"' });
  assert.deepEqual(calls, [[57, 50]]);
  assert.deepEqual(span.events.map((e) => [e.type, e.kind]), [
    ["pointerdown", "pointer"], ["mousedown", "mouse"], ["pointerup", "pointer"], ["mouseup", "mouse"],
    ["click", "mouse"]]);
  const click = span.events[4];
  assert.equal(click.clientX, 57);
  assert.equal(click.clientY, 50);
  assert.equal(click.bubbles, true);
  assert.equal(click.cancelable, true);
  assert.equal(click.button, 0);
  assert.equal(span.events[0].buttons, 1);
  assert.equal(span.events[0].pointerType, "mouse");
  assert.equal(button.focused, 1, "the closest focusable ancestor gets focus");
  assert.deepEqual(button.focusOpts, { preventScroll: true });
  assert.equal(form.events.length, 5, "the events bubble");
  assert.equal(span.clicks, 0, "no extra el.click() after the events");
});

test("clickAt refuses the overlay and fires nothing", () => {
  const host = node("div", { attrs: { id: "brainpoint-overlay" } });
  const { doc, win } = world(() => host);
  const r = K.clickAt(0.5, 0.5, { win, doc, isOurs: (el) => el === host });
  assert.equal(r.ok, false);
  assert.equal(r.method, "none");
  assert.match(r.detail, /overlay; not clicked/);
  assert.equal(host.events.length, 0);
  assert.equal(host.focused, 0);
});

test("clickAt refuses an empty point and a bad point", () => {
  const { doc, win } = world(() => null);
  const r = K.clickAt(0.2, 0.2, { win, doc });
  assert.deepEqual(r, { ok: false, method: "none", detail: "nothing on the page at (200, 100)" });
  const bad = K.clickAt(1.5, 0.2, { win, doc });
  assert.equal(bad.ok, false);
  assert.match(bad.detail, /\[0, 1\]/);
});

test("clickAt follows open shadow roots", () => {
  const inner = node("button", { text: "Deep" });
  const host = node("my-widget");
  host.shadowRoot = { elementFromPoint: () => inner };
  const { doc, win } = world(() => host);
  const r = K.clickAt(0.5, 0.5, { win, doc });
  assert.equal(r.ok, true);
  assert.equal(inner.events.length, 5);
  assert.equal(host.events.length, 0);
});

test("clickAt follows a same-origin iframe with frame coordinates and refuses a cross-origin one", () => {
  const target = node("a", { attrs: { href: "/x" }, text: "Inside" });
  const fcalls = [];
  const fdoc = { elementFromPoint(x, y) { fcalls.push([x, y]); return target; } };
  const frame = node("iframe", { rect: { left: 100, top: 50 } });
  frame.clientLeft = 2; frame.clientTop = 3;
  frame.contentDocument = fdoc;
  frame.contentWindow = { MouseEvent: FakeEvent, PointerEvent: FakePointerEvent };
  const { doc, win } = world(() => frame);
  const r = K.clickAt(0.3, 0.4, { win, doc });
  assert.equal(r.ok, true);
  assert.deepEqual(fcalls, [[300 - 100 - 2, 200 - 50 - 3]]);
  assert.equal(target.events[4].clientX, 198);
  assert.equal(target.events[4].view, frame.contentWindow);

  const foreign = node("iframe", { rect: { left: 0, top: 0 } });
  Object.defineProperty(foreign, "contentDocument", { get() { throw new Error("SecurityError"); } });
  const w2 = world(() => foreign);
  const r2 = K.clickAt(0.3, 0.4, { win: w2.win, doc: w2.doc });
  assert.equal(r2.ok, false);
  assert.match(r2.detail, /cross-origin frame/);
  assert.equal(foreign.events.length, 0);

  const nullDoc = node("iframe", { rect: { left: 0, top: 0 } });
  nullDoc.contentDocument = null;
  const w3 = world(() => nullDoc);
  assert.match(K.clickAt(0.1, 0.1, { win: w3.win, doc: w3.doc }).detail, /cross-origin/);
});

test("clickAt falls back to el.click() when events cannot be dispatched", () => {
  const b = node("button", { text: "Old" });
  b.dispatchEvent = () => { throw new Error("blocked"); };
  const { doc, win } = world(() => b);
  assert.deepEqual(K.clickAt(0.5, 0.5, { win, doc }), { ok: true, method: "element.click", detail: 'button "Old"' });
  assert.equal(b.clicks, 1);

  const c = node("button", { text: "No events" });
  const w2 = world(() => c);
  delete w2.win.MouseEvent;
  delete w2.win.PointerEvent;
  assert.equal(K.clickAt(0.5, 0.5, { win: w2.win, doc: w2.doc }).method, "element.click");

  const d = node("div", { text: "Nothing works" });
  d.dispatchEvent = () => { throw new Error("blocked"); };
  d.click = () => { throw new Error("also blocked"); };
  const w3 = world(() => d);
  const r = K.clickAt(0.5, 0.5, { win: w3.win, doc: w3.doc });
  assert.equal(r.ok, false);
  assert.match(r.detail, /also blocked/);
});

test("describe names the element but never a field's value", () => {
  const input = node("input", { attrs: { type: "password", value: "hunter2" } });
  input.value = "hunter2";
  input.innerText = "hunter2";
  assert.equal(K.describe(input), 'input "password field"');
  const text = node("textarea", { text: "private words" });
  assert.equal(K.describe(text), 'textarea "field"');
  const labelled = node("div", { attrs: { role: "button", "aria-label": "Close" }, text: "x" });
  assert.equal(K.describe(labelled), 'div[role=button] "Close"');
  const long = node("p", { text: "a  b\n".repeat(40) });
  assert.ok(K.describe(long).length <= 'p ""'.length + 60);
  assert.equal(K.describe(null), "");
  const { doc, win } = world(() => input);
  const r = K.clickAt(0.5, 0.5, { win, doc });
  assert.ok(!JSON.stringify(r).includes("hunter2"));
});

test("METHODS lists the methods clicked may report", () => {
  assert.deepEqual([...K.METHODS], ["events", "element.click", "none"]);
});

// ------------------------------------------- expect: the scanned element must be hit (review fix)

function linked(tag, parent, opts = {}) {
  const n = node(tag, { parent, ...opts });
  n.parentNode = parent;
  n.nodeType = 1;
  return n;
}

test("expect: a click lands only on the scanned element or inside it", () => {
  const body = linked("body", null);
  const next = linked("a", body, { attrs: { href: "/2" }, text: "Next" });
  const span = linked("span", next, { text: "Next" });
  const del = linked("button", body, { text: "Delete" });
  let at = span;
  const { doc, win } = world(() => at);
  assert.equal(K.clickAt(0.5, 0.5, { win, doc, expect: next }).ok, true);   // a child of it
  at = next;
  assert.equal(K.clickAt(0.5, 0.5, { win, doc, expect: next }).ok, true);
  at = del;                                                                  // something moved in
  const r = K.clickAt(0.5, 0.5, { win, doc, expect: next });
  assert.equal(r.ok, false);
  assert.equal(r.method, "none");
  assert.match(r.detail, /page changed: button "Delete" .* not the scanned a "Next"; not clicked/);
  assert.equal(del.events.length, 0, "the new element got no events");
  at = body;                                                                 // an ancestor, bigger box
  body.rect = { left: 0, top: 0, right: 1000, bottom: 500 };
  next.rect = { left: 400, top: 200, right: 480, bottom: 230 };
  assert.equal(K.clickAt(0.5, 0.5, { win, doc, expect: next }).ok, false);
  next.isConnected = false;                                                  // removed from the page
  at = span;
  assert.match(K.clickAt(0.5, 0.5, { win, doc, expect: next }).detail, /no longer on the page/);
});

test("expect: a label of the control, a same-box wrapper and shadow content count as the element", () => {
  const body = linked("body", null);
  const input = linked("input", body, { attrs: { type: "checkbox" } });
  const label = linked("label", body, { text: "Remember me" });
  label.control = input;
  const { doc, win } = world(() => label);
  assert.equal(K.clickAt(0.5, 0.5, { win, doc, expect: input }).ok, true);
  const wrap = linked("div", body, { rect: { left: 10, top: 10, right: 90, bottom: 40 } });
  const btn = linked("button", wrap, { rect: { left: 11, top: 10, right: 90, bottom: 41 } });
  const w2 = world(() => wrap);
  assert.equal(K.clickAt(0.5, 0.5, { win: w2.win, doc: w2.doc, expect: btn }).ok, true);
  const host = linked("my-button", body);
  const root = { nodeType: 11, parentNode: null, host };
  const inner = linked("span", null);
  inner.parentNode = root;
  assert.equal(K.within(inner, host), true);
  assert.equal(K.within(host, inner), false);
});
