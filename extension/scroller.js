/*
 * BrainScroll scroller: find the Reels feed's scroll container and move one reel.
 *
 *     await BrainScrollScroller.scroll("down")   // -> {ok, method, edge}
 *     BrainScrollScroller.find()                  // -> {el, method} or null
 *
 * Shared by the content script on instagram.com and by test/reels_mock.html. Candidates
 * are document.scrollingElement (when it scrolls at all) plus every element whose
 * computed overflow-y is auto or scroll, which scrolls by more than 50 px and which
 * contains a <video>. Only ancestors of <video> elements can contain one, so the search
 * walks up from each video instead of styling the whole Instagram DOM. A container with
 * scroll-snap on the y axis wins (desktop Reels snaps one reel per screen), then the
 * largest visible area. The scroll is +/- one clientHeight, smooth, and is checked after
 * 700 ms: scrollTop moved or the most visible <video> changed. Smooth scrolling one
 * screen takes about 300-500 ms in Chrome, so 700 ms leaves room. At the top (up) or the
 * bottom (down) nothing is tried and the result carries edge: true, so the content script
 * does not fall back to a debugger key press that cannot help.
 */
(function (root) {
  "use strict";

  const MIN_EXTRA_PX = 50;
  const VERIFY_MS = 700;
  const SNAP_Y = /(^|\s)(y|block|both)(\s|$)/;

  function visibleArea(rect, vw, vh) {
    const w = Math.min(rect.right, vw) - Math.max(rect.left, 0);
    const h = Math.min(rect.bottom, vh) - Math.max(rect.top, 0);
    return w > 0 && h > 0 ? w * h : 0;
  }

  function scrollsY(el, win) {
    if (el.scrollHeight <= el.clientHeight + MIN_EXTRA_PX) return false;
    const oy = win.getComputedStyle(el).overflowY;
    return oy === "auto" || oy === "scroll" || oy === "overlay";
  }

  /** Candidate containers, best first: [{el, method, snap, area}]. */
  function candidates(doc, win) {
    doc = doc || root.document;
    win = win || doc.defaultView || root;
    const vw = win.innerWidth, vh = win.innerHeight;
    const out = [];
    const seen = new Set();
    const top = doc.scrollingElement;
    if (top && top.scrollHeight > top.clientHeight + MIN_EXTRA_PX) {
      seen.add(top);
      out.push({ el: top, method: "window", snap: false, area: vw * vh });
    }
    for (const video of doc.querySelectorAll("video")) {
      for (let el = video.parentElement; el && el !== top; el = el.parentElement) {
        if (seen.has(el)) continue;
        seen.add(el);
        if (!scrollsY(el, win)) continue;
        const snap = SNAP_Y.test(win.getComputedStyle(el).scrollSnapType || "");
        out.push({ el, method: "container", snap,
                   area: visibleArea(el.getBoundingClientRect(), vw, vh) });
      }
    }
    out.sort((a, b) => (b.snap - a.snap) || (b.area - a.area));
    return out;
  }

  /** The container to scroll, or null when nothing on the page scrolls. */
  function find(doc, win) {
    const c = candidates(doc, win);
    return c.length ? { el: c[0].el, method: c[0].method } : null;
  }

  /** The <video> with the largest visible area, or null. */
  function mostVisibleVideo(doc, win) {
    doc = doc || root.document;
    win = win || doc.defaultView || root;
    let best = null, bestArea = 0;
    for (const v of doc.querySelectorAll("video")) {
      const a = visibleArea(v.getBoundingClientRect(), win.innerWidth, win.innerHeight);
      if (a > bestArea) { best = v; bestArea = a; }
    }
    return best;
  }

  /** What "moved" is judged on: the container's scrollTop and the most visible video. */
  function snapshot(target, doc, win) {
    return { top: target ? target.el.scrollTop : null, video: mostVisibleVideo(doc, win) };
  }

  function moved(before, after) {
    const scrolled = before.top !== null && after.top !== null && Math.abs(after.top - before.top) >= 1;
    return scrolled || before.video !== after.video;
  }

  function atEdge(el, dir) {
    if (dir === "up") return el.scrollTop <= 0;
    return el.scrollTop + el.clientHeight >= el.scrollHeight - 1;
  }

  function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  /**
   * Scroll one reel up or down. Resolves {ok, method, edge}: method "container" or
   * "window" for the element that was scrolled, "none" when no container was found.
   */
  async function scroll(dir, opts) {
    opts = opts || {};
    const doc = opts.doc || root.document;
    const win = opts.win || doc.defaultView || root;
    const wait = opts.waitMs === undefined ? VERIFY_MS : opts.waitMs;
    if (dir !== "up" && dir !== "down") throw new RangeError("dir must be up or down, got " + dir);
    const target = find(doc, win);
    if (!target) return { ok: false, method: "none", edge: false };
    if (atEdge(target.el, dir)) return { ok: false, method: target.method, edge: true };
    const before = snapshot(target, doc, win);
    const dy = (dir === "up" ? -1 : 1) * target.el.clientHeight;
    if (target.method === "window") win.scrollBy({ top: dy, behavior: "smooth" });
    else target.el.scrollBy({ top: dy, behavior: "smooth" });
    await delay(wait);
    return { ok: moved(before, snapshot(target, doc, win)), method: target.method, edge: false };
  }

  const api = Object.freeze({ VERIFY_MS, candidates, find, mostVisibleVideo, snapshot, moved,
                              atEdge, visibleArea, delay, scroll });
  root.BrainScrollScroller = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
