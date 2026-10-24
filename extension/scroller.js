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
 * largest visible area.
 *
 * One reel is one <video>. The scroll brings the nearest video above (up) or below (down)
 * the most visible one to where that one is now; the snap then settles on its reel. On
 * real instagram.com (2026-10-24) scrolling one clientHeight instead sometimes moved the
 * feed a little and snapped back to the same reel, and that counted as success because
 * scrollTop had changed. So success is now a different reel: the URL changed (Instagram
 * puts /reels/<id>/ in it for each reel) or the most visible <video> is another element.
 * It is polled every 100 ms for up to 1.5 s, because Instagram updates the URL after the
 * smooth scroll and snap have settled. With no video above (the first reel, up) nothing
 * is tried and the result carries edge: true, so the content script does not fall back to
 * a debugger key press that cannot help. With no video below yet (Instagram loads reels
 * as you go) it scrolls one clientHeight, which makes Instagram load the next one.
 */
(function (root) {
  "use strict";

  const MIN_EXTRA_PX = 50;
  const VERIFY_MS = 1500;   // longest wait for the reel to change
  const POLL_MS = 100;
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

  /** What "moved" is judged on: the page URL and the most visible video. */
  function snapshot(target, doc, win) {
    doc = doc || root.document;
    const loc = doc.location || root.location;
    return { url: loc ? loc.href : null, video: mostVisibleVideo(doc, win) };
  }

  /** True when another reel is showing: a new URL or another most visible video. */
  function moved(before, after) {
    return (before.url !== after.url) || (before.video !== after.video);
  }

  /**
   * The rect of the nearest reel above (dir "up") or below ("down") ``current``, or null.
   * ``rects`` are the videos' bounding rects; a neighbour's centre must be at least half
   * the current video's height away, so a video overlapping the current one never counts.
   */
  function neighbour(current, rects, dir) {
    const mid = (r) => (r.top + r.bottom) / 2;
    const c = mid(current);
    const min = Math.max(1, (current.bottom - current.top) / 2);
    let best = null;
    for (const r of rects) {
      const d = (mid(r) - c) * (dir === "up" ? -1 : 1);
      if (d >= min && (best === null || d < best.d)) best = { r, d };
    }
    return best ? best.r : null;
  }

  /** Poll until another reel shows or ``ms`` pass; true when it moved. */
  async function waitMoved(before, target, doc, win, ms) {
    const end = Date.now() + ms;
    for (;;) {
      if (moved(before, snapshot(target, doc, win))) return true;
      if (Date.now() >= end) return false;
      await delay(Math.min(POLL_MS, Math.max(0, end - Date.now())));
    }
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
    const before = snapshot(target, doc, win);
    let dy = null;
    if (before.video) {
      const cur = before.video.getBoundingClientRect();
      const rects = [];
      for (const v of doc.querySelectorAll("video")) {
        if (v !== before.video && target.el.contains(v)) rects.push(v.getBoundingClientRect());
      }
      const next = neighbour(cur, rects, dir);
      if (next) dy = (next.top + next.bottom) / 2 - (cur.top + cur.bottom) / 2;
      else if (dir === "up") return { ok: false, method: target.method, edge: true };
    }
    if (dy === null) {   // no video to aim at (none showing, or the next one not loaded yet)
      if (atEdge(target.el, dir)) return { ok: false, method: target.method, edge: true };
      dy = (dir === "up" ? -1 : 1) * target.el.clientHeight;
    }
    if (target.method === "window") win.scrollBy({ top: dy, behavior: "smooth" });
    else target.el.scrollBy({ top: dy, behavior: "smooth" });
    const ok = await waitMoved(before, target, doc, win, wait);
    return { ok, method: target.method, edge: false };
  }

  const api = Object.freeze({ VERIFY_MS, candidates, find, mostVisibleVideo, snapshot, moved,
                              neighbour, waitMoved, atEdge, visibleArea, delay, scroll });
  root.BrainScrollScroller = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
