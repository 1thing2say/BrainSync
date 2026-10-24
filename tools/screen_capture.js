() => {
  // Collect the visible clickable elements of the page, in viewport CSS pixels.
  // This is the function body passed to the chrome-devtools evaluate_script tool when the
  // evaluation set in data/screens/ was captured (see tools/screen_capture.md). It follows the
  // same rules as clients/chrome/hints.js: at most 200 hints, text at most 120 characters and
  // never taken from password inputs.
  const MAX_HINTS = 200;
  const MAX_TEXT = 120;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const SELECTOR = [
    "a[href]", "button", "input:not([type=hidden])", "select", "textarea",
    "[role=button]", "[role=link]", "[role=tab]", "[role=menuitem]", "[role=checkbox]",
    "[onclick]", "[tabindex]",
  ].join(",");

  const collapse = (s) => String(s || "").replace(/\s+/g, " ").trim();

  const roleOf = (el) => {
    const aria = (el.getAttribute("role") || "").toLowerCase();
    if (aria === "button") return "button";
    if (aria === "link") return "link";
    if (aria === "tab") return "tab";
    if (aria === "menuitem") return "menu_item";
    if (aria === "checkbox" || aria === "switch" || aria === "radio") return "checkbox";
    const tag = el.tagName.toLowerCase();
    if (tag === "a") return "link";
    if (tag === "button") return "button";
    if (tag === "select" || tag === "textarea") return "input";
    if (tag === "input") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (type === "checkbox" || type === "radio") return "checkbox";
      if (["submit", "button", "reset", "image"].includes(type)) return "button";
      return "input";
    }
    return "other";
  };

  const textOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "input" && type === "password") return "";
    let t = el.getAttribute("aria-label") || "";
    if (!collapse(t)) t = el.innerText || "";
    if (!collapse(t) && (tag === "input" || tag === "textarea" || tag === "select")) {
      t = tag === "select" ? (el.selectedOptions[0] || {}).text || "" : el.value || "";
      if (!collapse(t)) t = el.getAttribute("placeholder") || "";
    }
    if (!collapse(t)) t = el.getAttribute("title") || "";
    if (!collapse(t)) {
      const img = el.querySelector && el.querySelector("img[alt]");
      if (img) t = img.getAttribute("alt") || "";
    }
    return collapse(t).slice(0, MAX_TEXT);
  };

  const hints = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (hints.length >= MAX_HINTS) break;
    if (seen.has(el)) continue;
    const style = window.getComputedStyle(el);
    if (el.matches("[tabindex]") && !el.matches(SELECTOR.replace(",[tabindex]", ""))) {
      if (style.cursor !== "pointer") continue;
    }
    if (style.visibility === "hidden" || style.display === "none") continue;
    if (parseFloat(style.opacity) <= 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const x0 = Math.max(0, r.left);
    const y0 = Math.max(0, r.top);
    const x1 = Math.min(vw, r.right);
    const y1 = Math.min(vh, r.bottom);
    if (x1 <= x0 || y1 <= y0) continue;
    const cx = Math.min(vw - 1, Math.max(0, (x0 + x1) / 2));
    const cy = Math.min(vh - 1, Math.max(0, (y0 + y1) / 2));
    const top = document.elementFromPoint(cx, cy);
    if (!top || !(top === el || el.contains(top))) continue;
    const w = Math.round(x1 - x0);
    const h = Math.round(y1 - y0);
    if (w <= 0 || h <= 0) continue;
    seen.add(el);
    hints.push({
      id: hints.length,
      x: Math.round(x0),
      y: Math.round(y0),
      w,
      h,
      role: roleOf(el),
      text: textOf(el),
      tag: el.tagName.toLowerCase(),
    });
  }

  return {
    viewport: { w: vw, h: vh, dpr: window.devicePixelRatio },
    url: location.href,
    title: document.title,
    hints,
  };
}
