// A minimal fake DOM for map.js create(): just the calls it makes. Canvas contexts accept any
// call and count them, and record every property written (e.g. each globalAlpha). The fake
// window counts requestAnimationFrame and timers; spyTimers() does the same for the real
// globals, which is where a regression calling bare setTimeout() would land.
const NOOP_RESULTS = {
  measureText: (s) => ({ width: String(s).length * 7 }),
  createImageData: (w, h) => ({ width: w, height: h, data: new Uint8ClampedArray(w * h * 4) }),
  createLinearGradient: () => ({ addColorStop() {} }),
};

function fakeContext() {
  const props = {}, calls = {}, last = {}, sets = {};
  return new Proxy(props, {
    get(target, name) {
      if (name === "calls") return calls;
      if (name === "last") return last;     // last arguments per method, e.g. the ImageData drawn
      if (name === "sets") return sets;     // every value written per property, in order
      if (name in target) return target[name];
      return (...args) => {
        calls[name] = (calls[name] || 0) + 1;
        last[name] = args;
        return NOOP_RESULTS[name] ? NOOP_RESULTS[name](...args) : undefined;
      };
    },
    set(target, name, value) { (sets[name] = sets[name] || []).push(value); target[name] = value; return true; },
  });
}

export class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.style = {};
    this.listeners = {};
    this.parent = null;
    this.hidden = false;
    this.className = "";
    this._text = "";
    this._ctx = null;
  }
  get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join("") : this._text; }
  set textContent(v) { this.children = []; this._text = String(v); }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  replaceChildren(...kids) { this.children = []; kids.forEach((k) => this.appendChild(k)); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  hasAttribute(k) { return k in this.attributes; }
  addEventListener(n, f) { (this.listeners[n] = this.listeners[n] || []).push(f); }
  removeEventListener(n, f) { this.listeners[n] = (this.listeners[n] || []).filter((g) => g !== f); }
  dispatch(n, ev) { (this.listeners[n] || []).forEach((f) => f(ev || {})); }
  getContext() { return (this._ctx = this._ctx || fakeContext()); }
}

/** Depth-first search for elements matching pred. */
export function findAll(root, pred) {
  const out = [];
  (function walk(e) { if (pred(e)) out.push(e); e.children.forEach(walk); })(root);
  return out;
}

export function find(root, pred) { return findAll(root, pred)[0]; }

export const byRole = (root, role) => find(root, (e) => e.getAttribute("data-role") === role);

export class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.closed = false;
    FakeEventSource.last = this;
  }
  open() { this.readyState = 1; this.onopen && this.onopen({}); }
  send(msg) { this.onmessage({ data: typeof msg === "string" ? msg : JSON.stringify(msg) }); }
  fail(closed) { this.readyState = closed ? 2 : 0; this.onerror && this.onerror({}); }
  close() { this.readyState = 2; this.closed = true; }
}

export function fakeDom() {
  const root = new FakeElement("main");
  const counts = { raf: 0, timers: 0 };
  const doc = new FakeElement("#document");
  doc.createElement = (tag) => new FakeElement(tag);
  doc.getElementById = (id) => (id === "brainmap" ? root : null);
  doc.body = new FakeElement("body");
  const warnings = [];
  const listeners = {};
  const win = {
    console: { warn: (m) => warnings.push(m) },
    requestAnimationFrame: () => { counts.raf++; return 0; },
    setInterval: () => { counts.timers++; return 0; },
    setTimeout: () => { counts.timers++; return 0; },
    EventSource: FakeEventSource,
    addEventListener(n, f) { (listeners[n] = listeners[n] || []).push(f); },
    removeEventListener(n, f) { listeners[n] = (listeners[n] || []).filter((g) => g !== f); },
    dispatch(n, ev) { (listeners[n] || []).forEach((f) => f(ev || {})); },
    listeners,
  };
  return { doc, win, root, counts, warnings };
}

const TIMER_GLOBALS = ["requestAnimationFrame", "setTimeout", "setInterval"];

/**
 * Run fn synchronously with globalThis requestAnimationFrame, setTimeout and setInterval
 * replaced by counters that schedule nothing, then put the originals back (also on a throw).
 * Returns {raf, timers}: what fn and everything it called tried to schedule.
 */
export function spyTimers(fn) {
  const g = globalThis, saved = {}, counts = { raf: 0, timers: 0 };
  for (const k of TIMER_GLOBALS) saved[k] = Object.getOwnPropertyDescriptor(g, k);
  g.requestAnimationFrame = () => { counts.raf++; return 0; };
  g.setTimeout = () => { counts.timers++; return 0; };
  g.setInterval = () => { counts.timers++; return 0; };
  try {
    fn();
  } finally {
    for (const k of TIMER_GLOBALS) {
      if (saved[k]) Object.defineProperty(g, k, saved[k]);
      else delete g[k];
    }
  }
  return counts;
}
