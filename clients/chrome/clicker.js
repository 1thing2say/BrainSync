/*
 * BrainPoint clicker: click the page at a normalized viewport point (PROTOCOL-v3 `click`).
 *
 *     BrainPointClicker.clickAt(0.057, 0.1, {win: window, doc: document, isOurs})
 *     // -> {ok: true, method: "events", detail: "button \"Sign in\""}
 *
 * The point (x right, y down, both in [0, 1]) becomes CSS px of the window's viewport. The
 * element there is found with elementFromPoint, followed into open shadow roots and into
 * same-origin iframes. That element gets focus (its closest focusable ancestor does) and then
 * the events a real click makes, with the point's coordinates: pointerdown, mousedown,
 * pointerup, mouseup and click. If dispatching fails, el.click() is the fallback.
 *
 * It refuses, with ok false and method "none", when nothing is at the point, when the point
 * is on the BrainPoint overlay itself (`isOurs(el)` is true), when the point is inside a
 * cross-origin frame, which a content script cannot reach, and, given `expect` (the element
 * the scan saw there), when that element left the page or the element now at the point is
 * neither it nor inside it (through shadow roots and frames), nor a label of it or a wrapper
 * of the same box. `detail` names the element (tag, role and a short label) and never
 * includes what a field holds.
 *
 * clients/chrome/test/clicker.test.mjs runs it on plain objects in node.
 */
(function (root) {
  "use strict";

  const METHODS = Object.freeze(["events", "element.click", "none"]);
  const MAX_DEPTH = 8;            // shadow roots and frames followed at most
  const FOCUSABLE = "a[href],button,input,select,textarea,[tabindex],[contenteditable]";

  function finite(v) {
    return typeof v === "number" && Number.isFinite(v);
  }

  /** CSS px of the normalized point in a vw x vh viewport, kept on the last pixel row/column. */
  function toPx(xNorm, yNorm, vw, vh) {
    if (!finite(xNorm) || !finite(yNorm) || xNorm < 0 || xNorm > 1 || yNorm < 0 || yNorm > 1) {
      throw new RangeError("click point must be normalized to the viewport, x and y in [0, 1]; got (" +
                           xNorm + ", " + yNorm + ")");
    }
    if (!finite(vw) || !finite(vh) || vw <= 0 || vh <= 0) {
      throw new RangeError("viewport must have a positive size, got " + vw + " x " + vh);
    }
    return { x: Math.min(xNorm * vw, vw - 1), y: Math.min(yNorm * vh, vh - 1) };
  }

  function tagOf(el) {
    return String((el && el.tagName) || "").toLowerCase();
  }

  /**
   * The deepest element at (x, y) of `doc`: {el, x, y, doc, win, error}. x and y are in the
   * coordinates of the frame that holds el. error is set for a cross-origin frame.
   */
  function deepElementAt(doc, win, x, y) {
    let el = typeof doc.elementFromPoint === "function" ? doc.elementFromPoint(x, y) : null;
    for (let depth = 0; el && depth < MAX_DEPTH; depth++) {
      const shadow = el.shadowRoot;
      if (shadow && typeof shadow.elementFromPoint === "function") {
        const inner = shadow.elementFromPoint(x, y);
        if (inner && inner !== el) { el = inner; continue; }
      }
      const tag = tagOf(el);
      if (tag === "iframe" || tag === "frame") {
        let fdoc = null, fwin = null;
        try { fdoc = el.contentDocument; fwin = el.contentWindow; } catch (e) { fdoc = null; }
        if (!fdoc) return { el, x, y, doc, win, error: "cross-origin frame" };
        const r = el.getBoundingClientRect();
        const fx = x - r.left - (el.clientLeft || 0), fy = y - r.top - (el.clientTop || 0);
        const inner = typeof fdoc.elementFromPoint === "function" ? fdoc.elementFromPoint(fx, fy) : null;
        if (!inner) return { el, x, y, doc, win, error: null };
        el = inner; x = fx; y = fy; doc = fdoc; win = fwin || win;
        continue;
      }
      break;
    }
    return { el, x, y, doc, win, error: null };
  }

  /** A short description: tag, role and label. Never a field's value. */
  function describe(el) {
    if (!el) return "";
    const tag = tagOf(el) || "node";
    const role = el.getAttribute ? el.getAttribute("role") : null;
    let label = el.getAttribute ? el.getAttribute("aria-label") || "" : "";
    const form = tag === "input" || tag === "textarea" || tag === "select";
    if (!label && !form) label = String(el.innerText !== undefined ? el.innerText : el.textContent || "");
    if (!label && form) {
      const type = el.getAttribute ? el.getAttribute("type") : null;
      label = (type ? type + " " : "") + "field";
    }
    label = label.replace(/\s+/g, " ").trim().slice(0, 60);
    return tag + (role ? "[role=" + role + "]" : "") + (label ? " " + JSON.stringify(label) : "");
  }

  /** True when `node` is `ancestor` or inside it, across shadow roots and same-origin frames. */
  function within(node, ancestor) {
    for (let n = node, i = 0; n && i < 256; i++) {
      if (n === ancestor) return true;
      if (n.nodeType === 9) {                       // a frame's document: continue at its iframe
        let frame = null;
        try { frame = n.defaultView ? n.defaultView.frameElement : null; } catch (e) { frame = null; }
        n = frame;
      } else {
        n = n.parentNode || n.host || null;         // a ShadowRoot has no parentNode, only a host
      }
    }
    return false;
  }

  function sameBox(a, b) {
    try {
      const r = a.getBoundingClientRect(), q = b.getBoundingClientRect();
      return ["left", "top", "right", "bottom"].every((k) => Math.abs(r[k] - q[k]) <= 4);
    } catch (e) {
      return false;
    }
  }

  /** True when a click on `hit` is a click on `expect`: it, inside it, its label, or a same-box wrapper. */
  function hitsExpected(hit, expect) {
    if (within(hit, expect)) return true;
    if (hit && hit.control === expect) return true;
    return within(expect, hit) && sameBox(hit, expect);
  }

  function focusTarget(el) {
    let f = null;
    try { f = typeof el.closest === "function" ? el.closest(FOCUSABLE) : null; } catch (e) { f = null; }
    return f || el;
  }

  function fire(target, Ctor, type, init) {
    const ev = new Ctor(type, init);
    target.dispatchEvent(ev);
  }

  /**
   * Click at the normalized point (xNorm, yNorm). opts: win, doc (default: the globals),
   * isOurs(el) (true for the overlay's elements) and expect (the element that must be hit, or
   * null). Returns {ok, method, detail}.
   */
  function clickAt(xNorm, yNorm, opts) {
    opts = opts || {};
    const win = opts.win || root;
    const doc = opts.doc || win.document;
    const isOurs = opts.isOurs || (() => false);
    let p;
    try {
      p = toPx(xNorm, yNorm, win.innerWidth, win.innerHeight);
    } catch (e) {
      return { ok: false, method: "none", detail: e.message };
    }
    const hit = deepElementAt(doc, win, p.x, p.y);
    const where = "(" + Math.round(p.x) + ", " + Math.round(p.y) + ")";
    if (!hit.el) return { ok: false, method: "none", detail: "nothing on the page at " + where };
    if (isOurs(hit.el)) {
      return { ok: false, method: "none", detail: "the point " + where + " is on the BrainPoint overlay; not clicked" };
    }
    if (hit.error) {
      return { ok: false, method: "none",
               detail: "the point " + where + " is inside a " + hit.error + ", which BrainPoint cannot click" };
    }
    const el = hit.el;
    const detail = describe(el);
    const expect = opts.expect || null;
    if (expect !== null) {
      if (expect.isConnected === false) {
        return { ok: false, method: "none", detail: "the scanned element is no longer on the page; not clicked" };
      }
      if (!hitsExpected(el, expect)) {
        return { ok: false, method: "none",
                 detail: "the page changed: " + detail + " is at " + where + " now, not the scanned " +
                         describe(expect) + "; not clicked" };
      }
    }
    const f = focusTarget(el);
    try { if (typeof f.focus === "function") f.focus({ preventScroll: true }); } catch (e) { /* not focusable */ }
    const w = hit.win || win;
    const Mouse = w.MouseEvent || win.MouseEvent;
    const Pointer = w.PointerEvent || win.PointerEvent || Mouse;
    const base = { bubbles: true, cancelable: true, composed: true, clientX: hit.x, clientY: hit.y,
                   screenX: hit.x, screenY: hit.y, button: 0, view: w };
    const ptr = { pointerId: 1, pointerType: "mouse", isPrimary: true };
    try {
      if (typeof Mouse !== "function") throw new Error("no MouseEvent");
      fire(el, Pointer, "pointerdown", Object.assign({}, base, ptr, { buttons: 1 }));
      fire(el, Mouse, "mousedown", Object.assign({}, base, { buttons: 1, detail: 1 }));
      fire(el, Pointer, "pointerup", Object.assign({}, base, ptr, { buttons: 0 }));
      fire(el, Mouse, "mouseup", Object.assign({}, base, { buttons: 0, detail: 1 }));
      fire(el, Mouse, "click", Object.assign({}, base, { buttons: 0, detail: 1 }));
      return { ok: true, method: "events", detail };
    } catch (e) {
      if (typeof el.click === "function") {
        try {
          el.click();
          return { ok: true, method: "element.click", detail };
        } catch (e2) {
          return { ok: false, method: "none", detail: detail + ": " + e2.message };
        }
      }
      return { ok: false, method: "none", detail: detail + ": " + e.message };
    }
  }

  const api = Object.freeze({ METHODS, toPx, deepElementAt, describe, within, hitsExpected, clickAt });
  root.BrainPointClicker = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
