/*
 * BrainSync heatmap panel: shows the precomputed Meta TRIBE v2 prediction for the reel on
 * screen, next to the feed. Decoration only; it never talks to the decoder or the bridge.
 *
 * Frames come from brainsync.heatmap render (clips/<id>/manifest.json + frames/*.png, one
 * per second of video, already shifted for the haemodynamic lag), so the frame for video
 * time t is the last one starting at or before t (frameAt, as brainsync/heatmap/manifest.py).
 *
 * Kept away from the SSVEP squares: the panel sits in the left margin, mirroring the squares
 * (x = +0.55 screen heights), and layoutProblem() refuses any layout that comes within
 * KEEP_OUT of a square's cue box, the reel column or the overlay HUD. It never runs per
 * animation frame: it updates on the video's timeupdate/seeked events and swaps two <img>
 * layers with a CSS opacity cross-fade, which the compositor does off the main thread. The
 * backdrop stays up for clips without a prediction, so the scene does not change after
 * calibration.
 *
 * Pure helpers are exported for extension/heatmap/test/panel.test.mjs.
 */
(function (root) {
  "use strict";

  // Geometry in screen heights from the screen centre, y up (as overlay.js).
  const DEFAULT_LAYOUT = Object.freeze({ x: -0.55, top: 0.45, width: 0.40, aspect: 480 / 400,
    label: 0.075 });
  // The reels targets of overlay.js DEFAULT_CONFIG: their cue boxes reach size / 2 + CUE_REACH.
  const DEFAULT_TARGETS = Object.freeze([
    Object.freeze({ label: "up", x: 0.55, y: 0.325, size: 0.3 }),
    Object.freeze({ label: "down", x: 0.55, y: -0.325, size: 0.3 }),
  ]);
  const CUE_REACH = 0.036;       // overlay.js CUE_GAP 0.03 + CUE_LINE 0.006
  const KEEP_OUT = 0.05;         // clearance from squares, column and HUD
  const COLUMN_ASPECT = 9 / 16;  // the reel column is 100vh tall, 9:16
  const HUD_PX = { left: 12, bottom: 12, height: 110, width_vw: 0.30 };  // overlay.js .hud
  const FADE_MS = 400;
  const Z_PANEL = 2147483646;    // just under overlay.js Z_TOP, never above the squares
  const FORMAT = 1;

  /** Index of the frame at video time t: the last one starting at or before t. */
  function frameAt(t, times) {
    if (!times.length) throw new Error("no frames");
    if (!Number.isFinite(t) || t < times[0]) return 0;
    let lo = 0, hi = times.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (times[mid] <= t) lo = mid; else hi = mid - 1;
    }
    return lo;
  }

  /** Panel rectangle in CSS px for a vw x vh viewport. */
  function panelRect(layout, vw, vh) {
    const w = layout.width * vh;
    const h = (layout.width / layout.aspect + layout.label) * vh;
    const left = vw / 2 + layout.x * vh - w / 2;
    const top = vh / 2 - layout.top * vh;
    return { left, top, width: w, height: h };
  }

  /** Cue-box rectangles of the flicker squares, CSS px. */
  function squareRects(targets, vw, vh) {
    return targets.map((t) => {
      const half = (t.size / 2 + CUE_REACH) * vh;
      const cx = vw / 2 + t.x * vh, cy = vh / 2 - t.y * vh;
      return { label: t.label, left: cx - half, top: cy - half, width: 2 * half, height: 2 * half };
    });
  }

  function grow(r, by) {
    return { left: r.left - by, top: r.top - by, width: r.width + 2 * by, height: r.height + 2 * by };
  }

  function overlaps(a, b) {
    return a.left < b.left + b.width && b.left < a.left + a.width &&
      a.top < b.top + b.height && b.top < a.top + a.height;
  }

  /** Why the panel cannot go there (off screen, or near a square, the column or the HUD), else null. */
  function layoutProblem(layout, targets, vw, vh) {
    const p = panelRect(layout, vw, vh);
    if (p.left < 0 || p.top < 0 || p.left + p.width > vw || p.top + p.height > vh) {
      return "panel does not fit this " + (vw / vh).toFixed(3) + ":1 screen";
    }
    const near = grow(p, KEEP_OUT * vh);
    for (const s of squareRects(targets, vw, vh)) {
      if (overlaps(near, s)) return "panel comes within " + KEEP_OUT + " screen heights of the " + s.label + " square";
    }
    const colW = COLUMN_ASPECT * vh;
    if (overlaps(near, { left: vw / 2 - colW / 2, top: 0, width: colW, height: vh })) {
      return "panel overlaps the reel column";
    }
    const hud = { left: HUD_PX.left, top: vh - HUD_PX.bottom - HUD_PX.height,
      width: HUD_PX.width_vw * vw, height: HUD_PX.height };
    if (overlaps(p, hud)) return "panel overlaps the overlay HUD";
    return null;
  }

  /** Problems with a manifest (the checks the panel needs; the full ones are in manifest.py). */
  function manifestProblems(m) {
    if (!m || typeof m !== "object") return ["manifest is not an object"];
    const out = [];
    if (m.format !== FORMAT) out.push("format " + m.format + " is not " + FORMAT);
    if (!Array.isArray(m.frames) || !m.frames.length) out.push("no frames");
    if (!Array.isArray(m.frame_times_s)) out.push("no frame times");
    else if (Array.isArray(m.frames) && m.frames.length !== m.frame_times_s.length) out.push("frames and frame times differ in length");
    else if (m.frame_times_s.some((t, i) => !Number.isFinite(t) || (i && t <= m.frame_times_s[i - 1]))) {
      out.push("frame times must be finite and increasing");
    }
    if (typeof m.label !== "string" || m.label.indexOf("Not your EEG") < 0) out.push("label must say it is not your EEG");
    if (typeof m.credit !== "string" || !m.credit) out.push("no credit");
    return out;
  }

  const CSS = `
    :host { all: initial; }
    .panel { position: fixed; box-sizing: border-box; pointer-events: none; overflow: hidden;
             background: #0b0b0f; border-radius: 10px; font-family: -apple-system, system-ui, sans-serif; }
    .panel.off { display: none; }
    .stage { position: absolute; left: 0; top: 0; width: 100%; }
    .stage img { position: absolute; left: 0; top: 0; width: 100%; height: 100%; opacity: 0;
                 transition: opacity ${FADE_MS}ms ease-in-out; will-change: opacity; }
    .stage img.on { opacity: 1; }
    .empty { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
             color: #6c6c76; font-size: 13px; text-align: center; padding: 0 12%; }
    .label { position: absolute; left: 0; bottom: 0; width: 100%; box-sizing: border-box;
             padding: 6px 10px 8px; color: #b9b9c3; font-size: 11px; line-height: 1.35;
             border-top: 1px solid #23232b; }
    .label b { color: #e4e4ea; font-weight: 600; }
    .label .credit { color: #7d7d88; }
  `;

  /**
   * Mount the panel. opts: doc, win (default globals), base (URL of the clips directory,
   * default "clips/"), layout, targets, fetchJson (url -> Promise<object>, default fetch).
   * Returns { show(video, clipId), clear(), destroy(), host, state() }.
   */
  function create(opts) {
    opts = opts || {};
    const doc = opts.doc || root.document, win = opts.win || root;
    const base = opts.base || "clips/";
    const layout = opts.layout || DEFAULT_LAYOUT, targets = opts.targets || DEFAULT_TARGETS;
    const fetchJson = opts.fetchJson || ((url) => win.fetch(url).then((r) => {
      if (!r.ok) throw new Error(url + ": HTTP " + r.status);
      return r.json();
    }));

    const host = doc.createElement("div");
    host.setAttribute("data-brainsync-heatmap", "");
    host.style.cssText = "position:fixed;left:0;top:0;width:0;height:0;pointer-events:none;z-index:" + Z_PANEL + ";";
    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `<style>${CSS}</style>
      <div class="panel"><div class="stage"><img alt=""><img alt=""><div class="empty"></div></div>
      <div class="label"><b class="what"></b><br><span class="credit"></span></div></div>`;
    (doc.body || doc.documentElement).appendChild(host);
    const panel = shadow.querySelector(".panel"), stage = shadow.querySelector(".stage");
    const layers = Array.from(shadow.querySelectorAll(".stage img"));
    const empty = shadow.querySelector(".empty");
    const what = shadow.querySelector(".what"), credit = shadow.querySelector(".credit");
    const manifests = new Map();
    const state = { clipId: null, manifest: null, frame: -1, front: 0, problem: null, token: 0 };
    let video = null;

    function setEmpty(text) {
      layers.forEach((img) => img.classList.remove("on"));
      empty.textContent = text;
      what.textContent = "Predicted average-brain fMRI response. Not your EEG.";
      credit.textContent = "";
      state.frame = -1;
    }

    function place() {
      const vw = win.innerWidth, vh = win.innerHeight;
      state.problem = layoutProblem(layout, targets, vw, vh);
      if (state.problem) {
        panel.classList.add("off");
        if (win.console) win.console.warn("[BrainSync heatmap] " + state.problem);
        return;
      }
      const r = panelRect(layout, vw, vh);
      panel.classList.remove("off");
      panel.style.left = r.left + "px";
      panel.style.top = r.top + "px";
      panel.style.width = r.width + "px";
      panel.style.height = r.height + "px";
      stage.style.height = (layout.width / layout.aspect) * vh + "px";
    }

    function showFrame(k) {
      const m = state.manifest;
      if (!m || k === state.frame) return;
      state.frame = k;
      const token = ++state.token;
      const back = layers[1 - state.front];
      back.onload = () => {
        if (token !== state.token) return;
        back.classList.add("on");
        layers[state.front].classList.remove("on");
        state.front = 1 - state.front;
      };
      back.src = base + m.clip_id + "/" + m.frames[k];
    }

    function update() {
      if (state.manifest && video) showFrame(frameAt(video.currentTime, state.manifest.frame_times_s));
    }

    const EVENTS = ["timeupdate", "seeked", "loadeddata", "play"];
    function detach() {
      if (video) EVENTS.forEach((e) => video.removeEventListener(e, update));
      video = null;
    }

    /** Follow this video, showing the prediction for clipId (placeholder if there is none). */
    function show(nextVideo, clipId) {
      detach();
      video = nextVideo || null;
      if (video) EVENTS.forEach((e) => video.addEventListener(e, update));
      if (clipId === state.clipId && state.manifest) return Promise.resolve(update());
      state.clipId = clipId || null;
      state.manifest = null;
      const token = ++state.token;
      if (!clipId) { setEmpty("No prediction for this reel"); return Promise.resolve(); }
      if (!manifests.has(clipId)) manifests.set(clipId, fetchJson(base + clipId + "/manifest.json"));
      return manifests.get(clipId).then((m) => {
        if (token !== state.token) return;
        const bad = manifestProblems(m);
        if (bad.length) throw new Error(bad.join("; "));
        state.manifest = m;
        empty.textContent = "";
        what.textContent = m.label;
        credit.textContent = m.credit;
        state.frame = -1;
        update();
      }).catch((err) => {
        if (token !== state.token) return;
        manifests.delete(clipId);
        if (win.console) win.console.warn("[BrainSync heatmap] " + clipId + ": " + err.message);
        setEmpty("No prediction for this reel");
      });
    }

    function clear() { detach(); state.clipId = null; state.manifest = null; state.token++; setEmpty("No prediction for this reel"); }

    function destroy() {
      detach();
      win.removeEventListener("resize", place);
      host.remove();
    }

    win.addEventListener("resize", place);
    place();
    setEmpty("No prediction for this reel");
    return { show, clear, destroy, host, state: () => Object.assign({}, state) };
  }

  const api = Object.freeze({
    DEFAULT_LAYOUT, DEFAULT_TARGETS, CUE_REACH, KEEP_OUT, FADE_MS, Z_PANEL, FORMAT,
    frameAt, panelRect, squareRects, overlaps, layoutProblem, manifestProblems, create,
  });
  root.BrainSyncHeatmap = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
