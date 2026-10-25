/*
 * BrainPoint hints: the page's own clickable elements, sent with each scan (PROTOCOL-v3 `scan`).
 *
 *     const got = BrainPointHints.collect(document, window, {exclude: overlayHost})
 *     // got.hints: [{id, x, y, w, h, role, text, tag, submit?}]   viewport CSS px, at most 200
 *     // got.masks: [{x, y, w, h}]                        password fields to black out
 *     // got.viewport: {w, h, dpr}
 *     // got.elements: [Element]                          the hints' elements, by id (not
 *     //                                                   enumerable, never sent)
 *
 * Clickable means a[href], button, input (not hidden), select, textarea, an element with
 * role button, link, tab, menuitem or checkbox, an [onclick] element, or a [tabindex] element
 * drawn with cursor: pointer. An element counts only when it can be seen: it has a box inside
 * the viewport, its style is not display none, visibility hidden or opacity 0, and no other
 * element covers its centre (document.elementFromPoint). Boxes are clipped to the viewport.
 * At most 200 hints are kept, in reading order (top to bottom, then left to right), with ids
 * 0..n-1. Elements inside `opts.exclude` (the BrainPoint overlay) are never hints. A hint
 * that submits a form (an input of type submit or image, or a submit button inside a form)
 * carries `submit: true`, so the server asks for a confirm pick before clicking it.
 * `elements[id]` is the element of hint `id`; it stays in the page, so a click can check that
 * the element under the point is still the one that was scanned.
 *
 * Privacy: `text` comes from aria-label, aria-labelledby, the element's own text, a form
 * control's <label>, placeholder, title or an image's alt, trimmed to 120 characters. It is
 * never taken from what the user typed: the value of an input, textarea or select is not read,
 * except the caption of a button-type input (submit, button, reset). Password fields (and
 * fields whose autocomplete is a password, card number, card code or one-time code) give no
 * text at all and are returned in `masks`, visible or covered, so the caller can black them
 * out in the screenshot.
 *
 * The functions take the document and window as arguments and use only a small part of the
 * DOM API, so clients/chrome/test/hints.test.mjs runs them on plain objects in node.
 */
(function (root) {
  "use strict";

  const MAX_HINTS = 200;
  const MAX_TEXT = 120;
  const SELECTOR = [
    "a[href]", "button", "input:not([type=hidden])", "select", "textarea",
    "[role=button]", "[role=link]", "[role=tab]", "[role=menuitem]", "[role=checkbox]",
    "[onclick]", "[tabindex]",
  ].join(",");
  const CLICK_ROLES = Object.freeze(["button", "link", "tab", "menuitem", "checkbox"]);
  const FORM_TAGS = Object.freeze(["input", "select", "textarea"]);
  const BUTTON_INPUTS = Object.freeze(["submit", "button", "reset", "image"]);
  // autocomplete tokens of fields whose content must never leave the page
  const SECRET_AUTOCOMPLETE = /(^|\s)(current-password|new-password|one-time-code|cc-number|cc-csc)(\s|$)/i;

  function attr(el, name) {
    if (!el || typeof el.getAttribute !== "function") return null;
    const v = el.getAttribute(name);
    return v === null || v === undefined ? null : String(v);
  }

  function tagOf(el) {
    return String((el && el.tagName) || "").toLowerCase();
  }

  function inputType(el) {
    // el.type is the browser's normalized type; the attribute covers plain objects.
    const t = typeof el.type === "string" && el.type ? el.type : attr(el, "type") || "text";
    return t.toLowerCase();
  }

  function collapse(s) {
    return String(s === null || s === undefined ? "" : s).replace(/\s+/g, " ").trim();
  }

  /** True for a password input, or a field whose autocomplete marks a secret. */
  function isSecret(el) {
    if (!el) return false;
    const tag = tagOf(el);
    if (tag === "input" && inputType(el) === "password") return true;
    if (FORM_TAGS.indexOf(tag) < 0) return false;
    return SECRET_AUTOCOMPLETE.test(attr(el, "autocomplete") || "");
  }

  /** True for a control that submits a form: input type submit or image, or a submit button in a form. */
  function isSubmit(el) {
    const tag = tagOf(el);
    if (tag === "input") {
      const type = inputType(el);
      return type === "submit" || type === "image";
    }
    if (tag !== "button") return false;
    const type = String((typeof el.type === "string" && el.type) || attr(el, "type") || "submit").toLowerCase();
    return type === "submit" && Boolean(el.form);
  }

  /** True when the element is one of the clickable kinds listed in the module docstring. */
  function isClickable(el, style) {
    const tag = tagOf(el);
    if (tag === "a") { if (attr(el, "href") !== null) return true; }
    else if (tag === "button" || tag === "select" || tag === "textarea") return true;
    else if (tag === "input") return inputType(el) !== "hidden";
    const role = (attr(el, "role") || "").toLowerCase();
    if (CLICK_ROLES.indexOf(role) >= 0) return true;
    if (attr(el, "onclick") !== null) return true;
    if (attr(el, "tabindex") !== null) return Boolean(style) && style.cursor === "pointer";
    return false;
  }

  /** The detector's role vocabulary: button, link, input, checkbox, tab, menu_item, icon_button, other. */
  function roleOf(el, text) {
    const aria = (attr(el, "role") || "").toLowerCase();
    const tag = tagOf(el);
    let role = "other";
    if (aria === "button") role = "button";
    else if (aria === "link") role = "link";
    else if (aria === "tab") role = "tab";
    else if (aria === "menuitem") role = "menu_item";
    else if (aria === "checkbox" || aria === "switch" || aria === "radio") role = "checkbox";
    else if (tag === "a") role = "link";
    else if (tag === "button") role = "button";
    else if (tag === "select" || tag === "textarea") role = "input";
    else if (tag === "input") {
      const type = inputType(el);
      if (type === "checkbox" || type === "radio") role = "checkbox";
      else if (BUTTON_INPUTS.indexOf(type) >= 0) role = "button";
      else role = "input";
    }
    if (role === "button" && !text) role = "icon_button";
    return role;
  }

  function labelledBy(el, doc) {
    const ids = collapse(attr(el, "aria-labelledby"));
    if (!ids || !doc || typeof doc.getElementById !== "function") return "";
    const parts = [];
    for (const id of ids.split(" ")) {
      const ref = doc.getElementById(id);
      if (ref && !isSecret(ref)) parts.push(collapse(ref.textContent));
    }
    return collapse(parts.join(" "));
  }

  function controlLabel(el) {
    const labels = el.labels;
    if (!labels || !labels.length) return "";
    return collapse(labels[0].innerText !== undefined ? labels[0].innerText : labels[0].textContent);
  }

  function imageAlt(el) {
    if (typeof el.querySelector !== "function") return "";
    let img = null;
    try { img = el.querySelector("img[alt]"); } catch (e) { img = null; }
    return img ? collapse(attr(img, "alt")) : "";
  }

  /**
   * The text of a hint, at most 120 characters; never a value the user typed and never
   * anything from a password field.
   */
  function textOf(el, doc) {
    if (isSecret(el)) return "";
    const tag = tagOf(el);
    const form = FORM_TAGS.indexOf(tag) >= 0;
    const candidates = [() => attr(el, "aria-label"), () => labelledBy(el, doc)];
    if (form) {
      if (tag === "input" && BUTTON_INPUTS.indexOf(inputType(el)) >= 0) {
        // A button's caption, set by the page, not typed by the user.
        candidates.push(() => (inputType(el) === "image" ? attr(el, "alt") : attr(el, "value")));
      }
      candidates.push(() => controlLabel(el), () => attr(el, "placeholder"));
    } else {
      candidates.push(() => (el.innerText !== undefined ? el.innerText : el.textContent));
    }
    candidates.push(() => attr(el, "title"), () => (form ? "" : imageAlt(el)));
    for (const get of candidates) {
      const t = collapse(get());
      if (t) return t.slice(0, MAX_TEXT);
    }
    return "";
  }

  /** True when the computed style hides the element. */
  function styleHidden(style) {
    if (!style) return false;
    if (style.display === "none") return true;
    if (style.visibility === "hidden" || style.visibility === "collapse") return true;
    const op = parseFloat(style.opacity);
    return Number.isFinite(op) && op <= 0;
  }

  /** The part of a client rect inside a vw x vh viewport, or null when nothing is left. */
  function clipBox(r, vw, vh) {
    if (!r) return null;
    const left = r.left !== undefined ? r.left : r.x;
    const top = r.top !== undefined ? r.top : r.y;
    const right = r.right !== undefined ? r.right : left + r.width;
    const bottom = r.bottom !== undefined ? r.bottom : top + r.height;
    if (![left, top, right, bottom].every(Number.isFinite)) return null;
    const x0 = Math.max(0, left), y0 = Math.max(0, top);
    const x1 = Math.min(vw, right), y1 = Math.min(vh, bottom);
    if (x1 - x0 < 1 || y1 - y0 < 1) return null;
    return { x0, y0, x1, y1 };
  }

  function contains(a, b) {
    return Boolean(a && b && typeof a.contains === "function" && a.contains(b));
  }

  /** True when another element covers the centre of box `c` (elementFromPoint), else false. */
  function covered(el, c, doc, vw, vh) {
    if (!doc || typeof doc.elementFromPoint !== "function") return false;
    const cx = Math.min(vw - 1, Math.max(0, (c.x0 + c.x1) / 2));
    const cy = Math.min(vh - 1, Math.max(0, (c.y0 + c.y1) / 2));
    const top = doc.elementFromPoint(cx, cy);
    if (!top) return true;
    return !(top === el || contains(el, top) || contains(top, el));
  }

  function rounded(c) {
    const x = Math.round(c.x0), y = Math.round(c.y0);
    return { x, y, w: Math.max(1, Math.round(c.x1) - x), h: Math.max(1, Math.round(c.y1) - y) };
  }

  function queryAll(doc, selector) {
    if (!doc || typeof doc.querySelectorAll !== "function") return [];
    return Array.from(doc.querySelectorAll(selector));
  }

  /**
   * Collect hints and password masks from `doc` as seen in `win`.
   * opts: max (200), exclude (an element whose subtree is ignored, the overlay host),
   * occlusion (true: skip covered elements).
   */
  function collect(doc, win, opts) {
    opts = opts || {};
    const max = Number.isInteger(opts.max) && opts.max >= 0 ? Math.min(opts.max, MAX_HINTS) : MAX_HINTS;
    const vw = win.innerWidth, vh = win.innerHeight;
    const viewport = { w: vw, h: vh, dpr: win.devicePixelRatio || 1 };
    const exclude = opts.exclude || null;
    const occlusion = opts.occlusion !== false;
    const styleOf = (el) => {
      try { return win.getComputedStyle ? win.getComputedStyle(el) : null; } catch (e) { return null; }
    };
    const rectOf = (el) => {
      try { return el.getBoundingClientRect(); } catch (e) { return null; }
    };
    const ours = (el) => exclude !== null && (el === exclude || contains(exclude, el));

    const masks = [];
    const maskSeen = new Set();
    for (const el of queryAll(doc, "input,textarea,select")) {
      if (!isSecret(el) || ours(el) || maskSeen.has(el)) continue;
      maskSeen.add(el);
      const style = styleOf(el);
      if (style && style.display === "none") continue;
      const c = clipBox(rectOf(el), vw, vh);
      if (c !== null) masks.push(rounded(c));
    }

    const found = [];
    const seen = new Set();
    const boxes = new Set();
    for (const el of queryAll(doc, SELECTOR)) {
      if (seen.has(el) || ours(el)) continue;
      seen.add(el);
      const style = styleOf(el);
      if (!isClickable(el, style) || styleHidden(style)) continue;
      if (typeof el.checkVisibility === "function" && !el.checkVisibility()) continue;
      const c = clipBox(rectOf(el), vw, vh);
      if (c === null) continue;
      if (occlusion && covered(el, c, doc, vw, vh)) continue;
      const box = rounded(c);
      // The same box twice is an element and its clickable child: keep the first (outer) one.
      const key = box.x + "," + box.y + "," + box.w + "," + box.h;
      if (boxes.has(key)) continue;
      boxes.add(key);
      const text = textOf(el, doc);
      found.push({ el, box, role: roleOf(el, text), text, tag: tagOf(el), submit: isSubmit(el) });
    }
    found.sort((a, b) => a.box.y - b.box.y || a.box.x - b.box.x);
    const kept = found.slice(0, max);
    const hints = kept.map((f, id) => {
      const h = { id, x: f.box.x, y: f.box.y, w: f.box.w, h: f.box.h, role: f.role, text: f.text, tag: f.tag };
      if (f.submit) h.submit = true;
      return h;
    });
    const out = { hints, masks, viewport };
    // Not enumerable: serializing the result (or a scan built from it) never walks the DOM.
    Object.defineProperty(out, "elements", { value: kept.map((f) => f.el), enumerable: false });
    return out;
  }

  const api = Object.freeze({
    MAX_HINTS, MAX_TEXT, SELECTOR, isSecret, isClickable, isSubmit, roleOf, textOf, styleHidden, clipBox,
    collect,
  });
  root.BrainPointHints = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
