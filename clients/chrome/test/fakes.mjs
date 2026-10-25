// Test doubles for the BrainPoint extension: fake timers, a fake WebSocket, a fake DOM for the
// overlay, fake page elements for hints/clicker, and a fake chrome API. No browser, no network.

export function makeFakeTimers() {
  let now = 1_000_000;
  let nextId = 1;
  const pending = new Map();   // id -> {at, f, every, ms}
  const timers = {
    setTimeout(f, ms = 0) { const id = nextId++; pending.set(id, { at: now + ms, f, every: null, ms }); return id; },
    clearTimeout(id) { pending.delete(id); },
    setInterval(f, ms) { const id = nextId++; pending.set(id, { at: now + ms, f, every: ms, ms }); return id; },
    clearInterval(id) { pending.delete(id); },
  };
  function advance(ms) {
    const end = now + ms;
    for (;;) {
      let best = null;
      for (const [id, t] of pending) if (t.at <= end && (best === null || t.at < best[1].at)) best = [id, t];
      if (best === null) break;
      const [id, t] = best;
      now = t.at;
      if (t.every === null) pending.delete(id); else t.at += t.every;
      t.f();
    }
    now = end;
  }
  const timeouts = () => [...pending.values()].filter((t) => t.every === null).map((t) => t.ms);
  return { timers, advance, now: () => now, pending, timeouts };
}

export function makeFakeWS() {
  const sockets = [];
  class FakeWebSocket {
    static OPEN = 1;
    constructor(url) {
      if (FakeWebSocket.throwNext) { FakeWebSocket.throwNext = false; throw new SyntaxError("bad url"); }
      this.url = url;
      this.readyState = 0;
      this.sent = [];
      this.closedWith = null;
      sockets.push(this);
    }
    send(text) {
      if (this.readyState !== 1) throw new Error("not open");
      this.sent.push(JSON.parse(text));
    }
    close(code, reason) { this.readyState = 3; this.closedWith = { code, reason }; }
    accept() { this.readyState = 1; this.onopen && this.onopen({}); }
    message(obj) { this.onmessage({ data: typeof obj === "string" ? obj : JSON.stringify(obj) }); }
    drop(code = 1006, reason = "") { this.readyState = 3; this.onclose && this.onclose({ code, reason }); }
    types() { return this.sent.map((m) => m.type); }
  }
  FakeWebSocket.throwNext = false;
  return { FakeWebSocket, sockets };
}

export const flush = () => new Promise((r) => setImmediate(r));
export async function settle(n = 5) { for (let i = 0; i < n; i++) await flush(); }

// ------------------------------------------------------------------ fake DOM (overlay)

export function makeCtx() {
  const calls = [];
  const ctx = {
    calls, fillStyle: "#000", strokeStyle: "#000", lineWidth: 1, font: "", textAlign: "", textBaseline: "",
    clearRect(...a) { calls.push(["clearRect", ...a]); },
    fillRect(...a) { calls.push(["fillRect", ctx.fillStyle, ...a]); },
    strokeRect(...a) { calls.push(["strokeRect", ctx.strokeStyle, ...a]); },
    fillText(text, x, y) { calls.push(["fillText", text, x, y]); },
    measureText(text) { return { width: String(text).length * 7 }; },
    setTransform(...a) { calls.push(["setTransform", ...a]); },
    setLineDash() {},
    drawImage(...a) { calls.push(["drawImage", ...a]); },
  };
  return ctx;
}

export class FakeNode {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parent = null;
    this.style = {};
    this.attrs = {};
    this.listeners = {};
    this.hidden = false;
    this.textContent = "";
    this.className = "";
    this.id = "";
    if (this.tagName === "CANVAS") { this.ctx = makeCtx(); this.width = 300; this.height = 150; }
  }
  appendChild(c) { c.remove(); c.parent = this; this.children.push(c); return c; }
  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter((x) => x !== this);
    this.parent = null;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  removeEventListener(t, f) { this.listeners[t] = (this.listeners[t] || []).filter((g) => g !== f); }
  fire(t, ev = {}) {
    for (const f of [...(this.listeners[t] || [])]) f({ stopPropagation() {}, preventDefault() {}, target: this, ...ev });
  }
  contains(n) { for (; n; n = n.parent) if (n === this) return true; return false; }
  attachShadow(opts) { const r = new FakeNode("#shadow-root"); r.host = this; r.mode = opts.mode; this._shadow = r; return r; }
  getContext() { return this.ctx; }
  toDataURL() { return "data:image/jpeg;base64,/9j/4AAQ"; }
  // every text below this node, for assertions
  allText() {
    return [this.textContent, ...this.children.map((c) => c.allText())].filter(Boolean).join("\n");
  }
  find(pred) {
    if (pred(this)) return this;
    for (const c of this.children) { const f = c.find(pred); if (f) return f; }
    if (this._shadow) return this._shadow.find(pred);
    return null;
  }
}

/** A fake window + document for point_overlay.create and content.startSession. */
export function makeFakePage({ w = 1280, h = 800, dpr = 2, timers } = {}) {
  const t = timers || makeFakeTimers();
  const raf = new Map();
  let rafId = 0;
  const html = new FakeNode("html");
  const doc = {
    documentElement: html, body: null, title: "Example page", visibilityState: "visible", listeners: {},
    createElement: (tag) => new FakeNode(tag),
    addEventListener(type, f) { (this.listeners[type] ||= []).push(f); },
    removeEventListener(type, f) { this.listeners[type] = (this.listeners[type] || []).filter((g) => g !== f); },
    fire(type, ev = {}) { for (const f of [...(this.listeners[type] || [])]) f(ev); },
    querySelectorAll() { return []; },
    elementFromPoint() { return null; },
  };
  const win = {
    innerWidth: w, innerHeight: h, devicePixelRatio: dpr, listeners: {}, document: doc,
    location: { href: "https://example.com/login?next=%2Fhome#top", origin: "https://example.com",
                protocol: "https:", host: "example.com", pathname: "/login" },
    addEventListener(type, f) { (this.listeners[type] ||= []).push(f); },
    removeEventListener(type, f) { this.listeners[type] = (this.listeners[type] || []).filter((g) => g !== f); },
    fire(type, ev = {}) { for (const f of [...(this.listeners[type] || [])]) f({ stopPropagation() {}, ...ev }); },
    requestAnimationFrame(f) { raf.set(++rafId, f); return rafId; },
    cancelAnimationFrame(id) { raf.delete(id); },
    setTimeout: t.timers.setTimeout, clearTimeout: t.timers.clearTimeout,
    setInterval: t.timers.setInterval, clearInterval: t.timers.clearInterval,
    getComputedStyle: () => ({}),
  };
  let ts = 0;
  /** Run n animation frames, period ms apart. */
  function frames(n, period = 1000 / 60) {
    for (let i = 0; i < n; i++) {
      ts += period;
      const fs = [...raf.values()];
      raf.clear();
      for (const f of fs) f(ts);
    }
    return ts;
  }
  return { win, doc, html, timers: t, frames, rafCount: () => raf.size, now: () => ts };
}

// --------------------------------------------------------- fake page elements (hints)

/** A plain-object page element. rect is {x, y, w, h} in viewport CSS px. */
export function el(tag, { attrs = {}, rect = { x: 0, y: 0, w: 0, h: 0 }, style = {}, text, value, type,
                         labels, parent = null, checkVisibility } = {}) {
  const e = {
    tagName: tag.toUpperCase(), attrs: { ...attrs }, rect, style, parent, children: [],
    innerText: text, value, events: [], focused: false,
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    getBoundingClientRect() {
      const r = this.rect;
      return { left: r.x, top: r.y, right: r.x + r.w, bottom: r.y + r.h, width: r.w, height: r.h, x: r.x, y: r.y };
    },
    contains(n) { for (; n; n = n.parent) if (n === this) return true; return false; },
    querySelector(sel) {
      if (sel !== "img[alt]") return null;
      const walk = (n) => { for (const c of n.children) { if (c.tagName === "IMG" && c.getAttribute("alt") !== null) return c; const f = walk(c); if (f) return f; } return null; };
      return walk(this);
    },
  };
  if (type !== undefined) { e.type = type; e.attrs.type = type; }
  if (labels) e.labels = labels;
  if (checkVisibility) e.checkVisibility = checkVisibility;
  if (parent) parent.children.push(e);
  return e;
}

/** A fake document holding `elements` in paint order (later ones are on top). */
export function page(elements, { w = 1000, h = 800, dpr = 2 } = {}) {
  const all = elements;
  const inside = (e, x, y) => { const r = e.rect; return x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h; };
  const doc = {
    calls: [],
    querySelectorAll(sel) {
      this.calls.push(sel);
      if (sel === "input,textarea,select") return all.filter((e) => ["INPUT", "TEXTAREA", "SELECT"].includes(e.tagName));
      return all;
    },
    getElementById(id) { return all.find((e) => e.attrs.id === id) || null; },
    elementFromPoint(x, y) {
      for (let i = all.length - 1; i >= 0; i--) {
        const e = all[i];
        if (e.style.pointerEvents === "none" || e.style.display === "none") continue;
        if (inside(e, x, y)) return e;
      }
      return null;
    },
  };
  const win = {
    innerWidth: w, innerHeight: h, devicePixelRatio: dpr,
    getComputedStyle: (e) => ({ display: "block", visibility: "visible", opacity: "1", cursor: "auto", ...e.style }),
  };
  return { doc, win };
}

// ------------------------------------------------------------------ fake chrome API

export function makeFakePort(name = "brainpoint", sender = { tab: { id: 7, windowId: 1 }, frameId: 0 }) {
  const p = {
    name, sender, got: [], onMsg: [], onDisc: [], disconnected: false,
    postMessage(m) { if (this.disconnected) throw new Error("port closed"); this.got.push(JSON.parse(JSON.stringify(m))); },
    onMessage: { addListener: (f) => p.onMsg.push(f) },
    onDisconnect: { addListener: (f) => p.onDisc.push(f) },
    disconnect() { this.disconnected = true; },
    // driven by the tests:
    say(m) { for (const f of p.onMsg) f(m); },
    drop() { this.disconnected = true; for (const f of p.onDisc) f(); },
    types() { return this.got.map((m) => m.type); },
  };
  return p;
}
