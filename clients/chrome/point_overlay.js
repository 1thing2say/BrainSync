/*
 * BrainPoint overlay: the flickering patches over the page, the HUD, the photosensitivity
 * warning, Esc to pause and the Resume button.
 *
 *     const model = BrainPointOverlay.createModel({send: (msg) => port.send(msg)})
 *     const view = BrainPointOverlay.create({win: window, doc: document, model,
 *                                            onAccept, onQuit, onTakeover, onMeasured})
 *     // server messages: model.onMessage(msg); socket state: model.onState("open")
 *
 * The server sends a `layout` whose targets carry rects normalized to the viewport (point
 * mode, PROTOCOL-v3 section 3). Each rect is drawn as a black-and-white patch whose grey level
 * is FlickerState's frame-indexed luminance, on a black border, with the target's label below
 * it (above it near the bottom edge). Everything is drawn on one full-viewport canvas fixed
 * over the page, with pointer-events none and the highest z-index, inside a closed shadow
 * root so neither the page's CSS nor its scripts reach it. The page keeps every click.
 *
 * Flicker safety (PROTOCOL-v3 section 4, rule 1) is FlickerState's: patches flicker only
 * after the user accepted the warning, while the socket is open, the last phase has flicker
 * true and the layout has targets, and never while paused. Nothing is drawn before Start.
 * Esc stops the flicker at once and sends `pause`; Resume sends `resume`. Esc is heard through
 * esc.js (BrainPointEsc), which listens in every frame from document_start, so it works while
 * an iframe has the focus or a page script swallows the key; the view also listens on the
 * window itself, and view.escape() is the same action for an Esc forwarded from a sub-frame.
 * When the tab is hidden the session is paused the same way and resumed when it shows again.
 * A pause that could not be sent goes out after the next welcome.
 *
 * The view measures the display refresh from requestAnimationFrame (RefreshMeter) and calls
 * onMeasured("connect") with the first estimate, and onMeasured("reconnect") when the server
 * refused the rate and the display now reports one inside the tolerance. It sends `onset`
 * on a layout's first flickering frame and `frames` every 5 s. When the server runs the fake
 * board (welcome.fake), moving the mouse over a patch sends `gaze` for its slot; the canvas
 * takes no pointer events, so the view listens to mousemove on the document and hit-tests.
 * A new layout is hit-tested again at the last pointer position, and a view with no patches
 * sends `gaze` null, because the server forgets a gaze whose patch moved (point mode reuses
 * the slot names: "b1" is a different element on every page) and the mouse may not move.
 *
 * The pure parts (hello fields, patch geometry, hit testing, label placement and the model)
 * are exported for clients/chrome/test/point_overlay.test.mjs.
 */
(function (root) {
  "use strict";

  const C = root.BrainSyncClient ||
    (typeof require === "function" ? require("./brainsync-client.js") : null);
  if (!C) throw new Error("load brainsync-client.js before point_overlay.js");
  const F = C.flicker;

  const CLIENT = Object.freeze({ name: "brainpoint-extension", kind: "extension", version: "0.1.0" });
  const HOST_ID = "brainpoint-overlay";
  const FLASH_MS = 700;           // outline around a selected patch
  const Z_TOP = 2147483647;
  const LABEL_PAD = 4;            // px between a patch's border and its label
  // What to say after a close that does not reconnect by itself, and the button that helps.
  const CLOSE_TEXT = Object.freeze({
    4000: Object.freeze({ text: "Another tab or app took over the headset. Press Take over to take it back.",
                          action: "takeover" }),
    4001: Object.freeze({ text: "The server refused the token. Set it in the BrainPoint options " +
                          "(right-click the toolbar icon, then Options) and press Retry.", action: "retry" }),
    4002: Object.freeze({ text: "Another tab or app is the controller. Press Take over to replace it.",
                          action: "takeover" }),
  });

  function finite(v) {
    return typeof v === "number" && Number.isFinite(v);
  }

  // ---------------------------------------------------------------- pure helpers

  /** The hello fields of a point-mode controller (Connection adds type, protocol and token). */
  function helloFields(refreshHz, viewport, takeover) {
    return {
      role: "controller", takeover: Boolean(takeover), client: Object.assign({}, CLIENT),
      mode: "point", slots: null, refresh_hz: Math.round(refreshHz * 1000) / 1000,
      accepted: true, viewport: Object.assign({}, viewport),
    };
  }

  /**
   * The layout's targets in CSS px of a vw x vh viewport: [{slot, label, kind, freq, phase,
   * rects: [{x, y, w, h}]}]. Rects that are not finite are left out.
   */
  function patchBoxes(targets, vw, vh) {
    const out = [];
    for (const t of targets || []) {
      if (!t || typeof t.slot !== "string") continue;
      const rects = [];
      for (const r of Array.isArray(t.rects) ? t.rects : []) {
        try { rects.push(C.rectToPx(r, vw, vh)); } catch (e) { /* malformed rect: skip */ }
      }
      out.push({ slot: t.slot, label: typeof t.label === "string" && t.label ? t.label : t.slot,
                 kind: t.kind, freq: t.freq, phase: t.phase, rects });
    }
    return out;
  }

  /** The slot whose patch holds (x, y) in CSS px, or null. Edges count as inside. */
  function hitTest(boxes, x, y) {
    if (!finite(x) || !finite(y)) return null;
    for (const b of boxes) {
      for (const r of b.rects) {
        if (x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h) return b.slot;
      }
    }
    return null;
  }

  /** Width of the black border around a patch, px. */
  function borderPx(r) {
    return Math.max(3, Math.round(0.06 * Math.min(r.w, r.h)));
  }

  /**
   * Where a label of textW x textH px goes for patch r in a vw x vh viewport: {x, y, where}
   * with (x, y) the label box's top-left. Below the patch if it fits, else above, else inside
   * the patch's bottom edge; always kept inside the viewport horizontally.
   */
  function labelBox(r, textW, textH, vw, vh) {
    const gap = borderPx(r) + LABEL_PAD;
    let where = "below";
    let y = r.y + r.h + gap;
    if (y + textH > vh) {
      where = "above";
      y = r.y - gap - textH;
      if (y < 0) { where = "inside"; y = Math.max(0, r.y + r.h - textH - LABEL_PAD); }
    }
    const x = Math.max(0, Math.min(vw - textW, r.x + r.w / 2 - textW / 2));
    return { x, y, where };
  }

  // ------------------------------------------------------------------ the model

  /**
   * The overlay's state machine, without DOM. opts: send(msg) -> bool, now() in ms, wall
   * (FlickerState's rAF-to-wall-clock mapping, for onset times).
   */
  /** "60", "60 or 90", "60, 90, 120 or 144": the accepted rates for the HUD. */
  function rateList(rates) {
    const r = rates.map(String);
    return r.length < 2 ? r.join("") : r.slice(0, -1).join(", ") + " or " + r[r.length - 1];
  }

  function createModel(opts) {
    opts = opts || {};
    const send = opts.send || (() => false);
    const now = opts.now || Date.now;
    const flicker = new C.FlickerState({ wall: opts.wall });
    const state = {
      stage: "warning",          // warning | measuring | live | quit
      conn: "closed",
      welcome: null,
      phase: null,
      phaseAt: null,
      error: null,               // the last error message from the server
      note: null,                // the last local problem (worker, capture), for the HUD
      status: null,              // the last status message
      scan: null,                // the last scan_result
      activity: "",              // what the content script is doing: scanning, clicking
      lastClick: null,           // {label, ok, method, detail}
      flash: null,               // {slot, until}
      refreshHz: null,
      refreshError: false,
      ended: null,               // {code, text, action} after a close that does not reconnect
      paused: false,             // Esc, until Resume
      hiddenTab: false,          // the tab is hidden: paused until it shows
      gazeSent: undefined,       // the slot of the last gaze sent on this connection
      rttMs: null,
    };

    function held() {
      return state.paused || state.hiddenTab;
    }

    /** Apply a change to the pause reasons; send pause or resume when the sum changed. */
    function setHeld(change) {
      const was = held();
      change();
      const is = held();
      if (is) flicker.pause(); else flicker.resume();
      if (was !== is) send({ type: is ? "pause" : "resume" });
    }

    function accept() {
      if (state.stage !== "warning") return;
      flicker.accept();
      state.stage = "measuring";
    }

    /** True when `hz` is within tolerance of a rate the welcome accepts (as clients/web/app.js). */
    function acceptedRate(hz) {
      const w = state.welcome;
      return Boolean(w) && flicker.refreshRates.some((r) => Math.abs(hz - r) <= w.refresh_tolerance_hz);
    }

    /**
     * A fresh refresh estimate (Hz). Returns "connect" for the first one, "reconnect" when the
     * server refused the last rate and this one is one of the rates the welcome accepts
     * (refresh_rates, or refresh_hz alone from an older server), else null.
     */
    function measured(hz) {
      if (!finite(hz) || hz <= 0) return null;
      state.refreshHz = hz;
      flicker.setMeasuredHz(hz);
      if (state.stage === "measuring") { state.stage = "live"; return "connect"; }
      if (state.stage === "live" && state.refreshError && acceptedRate(hz)) {
        state.refreshError = false;
        return "reconnect";
      }
      return null;
    }

    function onState(s) {
      if (C.CONN_STATES.indexOf(s) < 0) return;
      state.conn = s;
      flicker.setOpen(s === "open");
      if (s !== "open") { state.gazeSent = undefined; state.phase = null; state.welcome = null; }
      if (s === "connecting") state.ended = null;
    }

    function onClose(info) {
      if (!info || info.reconnect) return;
      const known = CLOSE_TEXT[info.code];
      if (known) state.ended = { code: info.code, text: known.text, action: known.action };
    }

    function onMessage(msg) {
      if (!msg || typeof msg !== "object" || typeof msg.type !== "string") return;
      flicker.handle(msg);
      switch (msg.type) {
        case "welcome":
          state.welcome = msg;
          state.error = null;
          state.refreshError = false;
          state.ended = null;
          if (held()) send({ type: "pause" });
          break;
        case "phase":
          state.phase = msg;
          state.phaseAt = now();
          break;
        case "selection": {
          const l = flicker.layout;
          if (!l || msg.layout_id !== l.layout_id) break;
          state.flash = { slot: msg.slot, until: now() + FLASH_MS };
          break;
        }
        case "error":
          state.error = msg;
          if (msg.code === "refresh") state.refreshError = true;
          break;
        case "status":
          state.status = msg;
          break;
        case "scan_result":
          state.scan = msg;
          break;
        case "click":
          state.lastClick = { label: msg.element && msg.element.label ? String(msg.element.label) : "",
                              ok: null, method: "", detail: "" };
          break;
        default:
          break;
      }
    }

    /** Esc: stop the flicker here at once and tell the server. */
    function escape() {
      setHeld(() => { state.paused = true; });
    }

    function resume() {
      if (!state.paused) return;
      setHeld(() => { state.paused = false; });
    }

    /** The tab was hidden (false) or shown (true). A hidden tab is paused. */
    function setVisible(visible) {
      setHeld(() => { state.hiddenTab = !visible; });
    }

    /** The pointer is over `slot` (null: over nothing). Sends gaze on change, fake board only. */
    function hover(slot) {
      const w = state.welcome;
      if (!w || w.fake !== true || state.conn !== "open") return false;
      if (slot === undefined) slot = null;
      // A new connection starts looking at nothing: the server drops the gaze of a controller
      // that left, so a first null is never needed.
      if (slot === (state.gazeSent === undefined ? null : state.gazeSent)) return false;
      if (!send({ type: "gaze", slot })) return false;
      state.gazeSent = slot;
      return true;
    }

    function quit() {
      state.stage = "quit";
      state.paused = true;
      flicker.pause();
    }

    function showTargets() {
      return (state.stage === "live" || state.stage === "measuring") &&
        !(state.phase && state.phase.phase === "stop") && flicker.targets().length > 0;
    }

    /**
     * One animation frame at rAF time ts (ms): sends onset and frames when due and returns
     * {mode: "hidden" | "still" | "flicker", items: [{slot, label, kind, lum, flash, gaze, rects}]}
     * with rects still normalized.
     */
    function frame(ts) {
      const targets = flicker.targets();
      const items = [];
      const flashing = state.flash !== null && now() < state.flash.until;
      for (const t of targets) {
        items.push({ slot: t.slot, label: t.label || t.slot, kind: t.kind, rects: t.rects || [],
                     lum: flicker.luminance(t, ts), flash: flashing && state.flash.slot === t.slot,
                     gaze: state.gazeSent === t.slot });
      }
      const onset = flicker.onsetDue(ts);
      if (onset) send(onset);
      const frames = flicker.framesDue(ts);
      if (frames) send(frames);
      if (!showTargets()) return { mode: "hidden", items: [] };
      return { mode: flicker.shouldFlicker() ? "flicker" : "still", items };
    }

    /** Seconds left in a timed phase (calibrate), or null. */
    function remaining() {
      const p = state.phase;
      if (!p || !finite(p.seconds) || state.phaseAt === null) return null;
      return Math.max(0, p.seconds - (now() - state.phaseAt) / 1000);
    }

    /** The centred line: what the user should do now, or "". */
    function banner() {
      if (state.stage === "warning" || state.stage === "quit") return "";
      if (state.ended) return state.ended.text;
      if (state.stage === "measuring") return "Measuring the display refresh…";
      if (state.paused) return "BrainPoint is paused. Press Resume to continue.";
      if (state.conn !== "open") return "Connecting to the BrainSync server…";
      if (state.error && state.error.code === "refresh") return state.error.message;
      if (state.error && state.error.code === "detect_unavailable") return state.error.message;
      const p = state.phase;
      if (!p) return "Waiting for the server…";
      if (p.phase === "calibrate") {
        const left = remaining();
        return (p.text || "Calibrating") + (left === null ? "" : " (" + Math.ceil(left) + " s)");
      }
      if (p.phase === "stop") return p.text || "The server stopped.";
      if (p.phase === "wait") return p.text || "Waiting for the server…";
      if (p.phase === "pause") return p.text || (state.activity || "Paused by the server.");
      if (state.activity) return state.activity;
      if (flicker.targets().length === 0) return p.text || "Looking at the page…";
      return p.text || "";
    }

    /** HUD lines. */
    function hud() {
      const p = state.phase;
      const lines = [];
      lines.push("BrainPoint · phase " + (p ? p.phase : "-") +
                 (flicker.shouldFlicker() ? " · flickering" : " · still") +
                 (state.paused ? " · paused (Esc)" : state.hiddenTab ? " · paused (tab hidden)" : ""));
      lines.push("server " + state.conn + (state.rttMs === null ? "" : " · rtt " + state.rttMs + " ms") +
                 (state.welcome && state.welcome.fake ? " · fake board (hover a patch)" : ""));
      const st = state.status;
      lines.push("board " + (st ? st.board + (st.text ? " · " + st.text : "") : "-"));
      const w = state.welcome;
      lines.push("refresh " + (state.refreshHz === null ? "measuring…" : state.refreshHz.toFixed(2) + " Hz") +
                 (w ? " (want " + rateList(flicker.refreshRates) + " ± " + w.refresh_tolerance_hz + ")" : "") +
                 " · dropped " + flicker.dropped);
      const s = state.scan;
      if (s) {
        lines.push("scan #" + s.scan_id + (s.ok ? ": " + s.n_candidates + " targets of " + s.n_elements +
                   " elements, " + s.ms + " ms" + (s.summary ? " · " + s.summary : "") : ": failed"));
      }
      const c = state.lastClick;
      if (c) {
        lines.push("click " + JSON.stringify(c.label) + (c.ok === null ? " …" : c.ok ? " ok via " + c.method
          : " failed" + (c.detail ? ": " + c.detail : "")));
      }
      if (state.error) lines.push("error " + state.error.code + ": " + state.error.message);
      if (state.note) lines.push("note: " + state.note);
      return lines;
    }

    function warnings() {
      return {
        conn: state.conn !== "open",
        refresh: state.refreshError,
        error: state.error !== null,
        board: state.status !== null && state.status.board !== "streaming",
      };
    }

    return {
      state, flicker, accept, measured, onState, onClose, onMessage, escape, resume, setVisible,
      hover, quit, showTargets, frame, remaining, banner, hud, warnings, held,
    };
  }

  // -------------------------------------------------------------------- the view

  const GREYS = [];
  for (let i = 0; i < 256; i++) GREYS.push("rgb(" + i + "," + i + "," + i + ")");

  const CSS = `
    :host { all: initial; }
    canvas { position: fixed; left: 0; top: 0; pointer-events: none; }
    .hud { position: fixed; left: 12px; bottom: 12px; max-width: min(46vw, 560px); pointer-events: none;
           font: 12px/1.45 ui-monospace, Menlo, monospace; color: #ddd; background: rgba(0,0,0,0.7);
           border-radius: 6px; padding: 6px 9px; white-space: pre-wrap; }
    .hud .warn { color: #ff6b6b; font-weight: bold; }
    .banner { position: fixed; left: 50%; top: 16px; transform: translateX(-50%); max-width: 70vw;
              pointer-events: none; font: 16px/1.4 -apple-system, system-ui, sans-serif; color: #fff;
              background: rgba(0,0,0,0.75); border-radius: 8px; padding: 8px 14px; text-align: center; }
    .panel { position: fixed; right: 12px; top: 12px; display: flex; gap: 8px; pointer-events: auto; }
    .panel button, .modal button { font: 14px/1.2 -apple-system, system-ui, sans-serif; padding: 7px 16px;
              border-radius: 8px; border: 1px solid #888; background: #f2f2f2; color: #111; cursor: pointer; }
    .panel button.primary, .modal button.primary { background: #0a66c2; border-color: #0a66c2; color: #fff; }
    .modal { position: fixed; left: 50%; top: 50%; transform: translate(-50%, -50%); width: min(540px, 90vw);
             pointer-events: auto; font: 15px/1.5 -apple-system, system-ui, sans-serif; color: #111;
             background: #fff; border-radius: 12px; padding: 22px 24px; box-shadow: 0 10px 40px rgba(0,0,0,0.5); }
    .modal h2 { margin: 0 0 10px; font-size: 19px; color: #b00020; }
    .modal p { margin: 0 0 10px; }
    .modal .row { display: flex; gap: 10px; justify-content: flex-end; margin-top: 16px; }
    [hidden] { display: none !important; }
  `;

  const WARNING = [
    "BrainPoint flickers up to four patches over this page, at the frequencies the BrainSync " +
      "server picks (15, 17, 12 and 20 Hz by default). Flicker between 15 and 25 Hz is the band " +
      "most likely to trigger a seizure in people with photosensitive epilepsy.",
    "Do you, or does anyone who can see this screen, have epilepsy or a history of seizures? " +
      "If so, press Cancel.",
    "Press Escape at any time to stop the flicker; if Escape does not work, press Alt+Shift+B " +
      "(or the BrainPoint toolbar button), which stops BrainPoint in this tab. Stop at once if " +
      "anyone feels unwell. " +
      "BrainPoint sends screenshots of this tab to your BrainSync server, which asks Claude " +
      "where the buttons are; password fields are blacked out first.",
  ];

  function el(doc, tag, cls, text) {
    const e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  /**
   * Mount the overlay in `doc`. opts: win, doc, model, resumed (skip the warning: the user
   * accepted it earlier in this tab), onAccept(), onQuit(), onTakeover(action),
   * onMeasured(action), esc (the Esc hub; default the global BrainPointEsc of esc.js).
   * Returns {host, model, update, setHidden, afterFrames, isOurs, escape, destroy}.
   */
  function create(opts) {
    const win = opts.win || root;
    const doc = opts.doc || win.document;
    const model = opts.model;
    const s = model.state;
    const noop = () => {};
    const onAccept = opts.onAccept || noop, onQuit = opts.onQuit || noop;
    const onTakeover = opts.onTakeover || noop, onMeasured = opts.onMeasured || noop;

    const host = el(doc, "div");
    host.id = HOST_ID;
    host.setAttribute("data-brainpoint", "1");
    host.style.cssText = "position:fixed;left:0;top:0;width:0;height:0;pointer-events:none;" +
      "z-index:" + Z_TOP + ";";
    // Closed: page scripts cannot read the HUD, and clicks never land inside it.
    const shadow = host.attachShadow({ mode: "closed" });
    const style = el(doc, "style", null, CSS);
    const canvas = el(doc, "canvas");
    const banner = el(doc, "div", "banner");
    const hudEl = el(doc, "div", "hud");
    const panel = el(doc, "div", "panel");
    const resumeBtn = el(doc, "button", "primary", "Resume");
    const takeoverBtn = el(doc, "button", "primary", "Take over");
    const quitBtn = el(doc, "button", null, "Stop BrainPoint");
    for (const b of [resumeBtn, takeoverBtn, quitBtn]) { b.type = "button"; panel.appendChild(b); }
    const modal = el(doc, "div", "modal");
    modal.setAttribute("role", "dialog");
    modal.appendChild(el(doc, "h2", null, "Photosensitivity warning"));
    for (const p of WARNING) modal.appendChild(el(doc, "p", null, p));
    const row = el(doc, "div", "row");
    const cancelBtn = el(doc, "button", null, "Cancel");
    const startBtn = el(doc, "button", "primary", "Start");
    for (const b of [cancelBtn, startBtn]) { b.type = "button"; row.appendChild(b); }
    modal.appendChild(row);
    for (const c of [style, canvas, banner, hudEl, panel, modal]) shadow.appendChild(c);
    (doc.documentElement || doc.body).appendChild(host);
    const ctx = canvas.getContext("2d", { alpha: true });

    const meter = new C.RefreshMeter();
    let dpr = 1, vw = 0, vh = 0, lastTs = null, rafId = 0, hudTimer = 0, destroyed = false;
    let boxes = [], lastX = null, lastY = null, gazeLayout;
    const hudLines = [];

    function resize() {
      dpr = win.devicePixelRatio || 1;
      vw = win.innerWidth;
      vh = win.innerHeight;
      canvas.style.width = vw + "px";
      canvas.style.height = vh + "px";
      canvas.width = Math.round(vw * dpr);
      canvas.height = Math.round(vh * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function draw(view) {
      ctx.clearRect(0, 0, vw, vh);
      if (view.mode === "hidden") { boxes = []; return; }
      boxes = [];
      const fontPx = Math.max(13, Math.round(0.022 * vh));
      ctx.font = "600 " + fontPx + "px -apple-system, system-ui, sans-serif";
      ctx.textBaseline = "top";
      ctx.textAlign = "left";
      for (const it of view.items) {
        const b = patchBoxes([it], vw, vh)[0];
        if (!b) continue;
        boxes.push(b);
        const grey = GREYS[Math.max(0, Math.min(255, Math.round(it.lum * 255)))];
        for (const r of b.rects) {
          const pad = borderPx(r);
          ctx.fillStyle = "#000";
          ctx.fillRect(r.x - pad, r.y - pad, r.w + 2 * pad, r.h + 2 * pad);
          ctx.fillStyle = grey;
          ctx.fillRect(r.x, r.y, r.w, r.h);
          if (it.flash || it.gaze) {
            ctx.strokeStyle = it.flash ? "#3ddc84" : "#7aa7ff";
            ctx.lineWidth = it.flash ? Math.max(4, pad) : 2;
            ctx.strokeRect(r.x - 2 * pad, r.y - 2 * pad, r.w + 4 * pad, r.h + 4 * pad);
          }
        }
        if (!b.rects.length) continue;
        const r = b.rects[0];
        const text = b.label;
        const tw = ctx.measureText(text).width + 12, th = fontPx + 8;
        const at = labelBox(r, tw, th, vw, vh);
        ctx.fillStyle = "rgba(0,0,0,0.8)";
        ctx.fillRect(at.x, at.y, tw, th);
        ctx.fillStyle = "#fff";
        ctx.fillText(text, at.x + 6, at.y + 4);
      }
    }

    function loop(ts) {
      if (destroyed) return;
      rafId = win.requestAnimationFrame(loop);
      if (lastTs !== null && meter.push(ts - lastTs) && meter.due()) {
        const action = model.measured(meter.hz());
        if (action) onMeasured(action);
      }
      lastTs = ts;
      const view = model.frame(ts);
      draw(view);
      syncGaze(view);
    }

    /** Send the gaze for the pointer over a new layout's patches, and null when none show. */
    function syncGaze(view) {
      if (view.mode === "hidden") {
        gazeLayout = undefined;
        model.hover(null);
        return;
      }
      const l = model.flicker.layout;
      const id = l ? l.layout_id : null;
      if (id === gazeLayout) return;
      gazeLayout = id;
      model.hover(lastX === null ? null : hitTest(boxes, lastX, lastY));
    }

    function setLine(i, text, warn) {
      while (hudLines.length <= i) { const d = el(doc, "div"); hudEl.appendChild(d); hudLines.push(d); }
      const line = hudLines[i];
      if (line.textContent !== text) line.textContent = text;
      const cls = warn ? "warn" : "";
      if (line.className !== cls) line.className = cls;
      line.hidden = false;
    }

    function update() {
      if (destroyed) return;
      const lines = model.hud();
      const w = model.warnings();
      lines.forEach((text, i) => setLine(i, text, (i === 1 && w.conn) || (i === 2 && w.board) ||
        (i === 3 && w.refresh) || text.startsWith("error ") || text.startsWith("note: ")));
      for (let i = lines.length; i < hudLines.length; i++) hudLines[i].hidden = true;
      const text = model.banner();
      if (banner.textContent !== text) banner.textContent = text;
      banner.hidden = !text;
      modal.hidden = s.stage !== "warning";
      hudEl.hidden = s.stage === "warning";
      resumeBtn.hidden = !(s.stage === "live" && s.paused);
      takeoverBtn.hidden = !(s.ended && s.ended.action);
      takeoverBtn.textContent = s.ended && s.ended.action === "retry" ? "Retry" : "Take over";
      // Buttons show only while paused or stopped, so the panel never covers the page's own
      // buttons while patches are being picked (it takes pointer events).
      quitBtn.hidden = !((s.stage === "live" || s.stage === "measuring") && (s.paused || s.ended));
    }

    /** Esc, from any frame: quit at the warning, else pause at once. */
    function escape() {
      if (destroyed) return;
      if (s.stage === "warning") onQuit();
      else if (s.stage !== "quit") model.escape();
      update();
    }

    const keysSeen = new WeakSet();   // one key press reaches us through the hub and the window
    function onKey(e) {
      if (!e || (e.key !== "Escape" && e.key !== "Esc" && e.keyCode !== 27)) return;
      if (typeof e === "object") {
        if (keysSeen.has(e)) return;
        keysSeen.add(e);
      }
      escape();
    }

    function onPointer(e) {
      lastX = e.clientX; lastY = e.clientY;
      model.hover(hitTest(boxes, lastX, lastY));
    }

    function onLeave(e) {
      if (!e.relatedTarget) { lastX = lastY = null; model.hover(null); }
    }

    function onVisibility() {
      model.setVisible(doc.visibilityState !== "hidden");
      update();
    }

    startBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      model.accept();
      update();
      onAccept();
    });
    cancelBtn.addEventListener("click", (e) => { e.stopPropagation(); onQuit(); });
    resumeBtn.addEventListener("click", (e) => { e.stopPropagation(); model.resume(); update(); });
    takeoverBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const action = s.ended ? s.ended.action : null;
      if (action) onTakeover(action);
    });
    quitBtn.addEventListener("click", (e) => { e.stopPropagation(); onQuit(); });
    const hub = opts.esc || root.BrainPointEsc || null;
    const offEsc = hub && typeof hub.on === "function" ? hub.on(onKey) : null;
    win.addEventListener("keydown", onKey, true);
    win.addEventListener("resize", resize);
    doc.addEventListener("mousemove", onPointer, { capture: true, passive: true });
    doc.addEventListener("mouseout", onLeave, { capture: true, passive: true });
    doc.addEventListener("visibilitychange", onVisibility);

    if (opts.resumed) model.accept();
    resize();
    if (doc.visibilityState === "hidden") model.setVisible(false);
    update();
    hudTimer = win.setInterval(update, 250);
    rafId = win.requestAnimationFrame(loop);

    /** Hide everything (true) or show it again (false); used around a screenshot. */
    function setHidden(hidden) {
      host.style.visibility = hidden ? "hidden" : "visible";
    }

    /** Resolves after n animation frames, so a style change has reached the screen. */
    function afterFrames(n) {
      return new Promise((resolve) => {
        let left = n === undefined ? 2 : n;
        const step = () => { if (--left <= 0 || destroyed) resolve(); else win.requestAnimationFrame(step); };
        win.requestAnimationFrame(step);
      });
    }

    /** True for the overlay's host (the page sees nothing deeper in a closed shadow root). */
    function isOurs(node) {
      return node === host || Boolean(node && typeof host.contains === "function" && host.contains(node));
    }

    function destroy() {
      if (destroyed) return;
      destroyed = true;
      win.cancelAnimationFrame(rafId);
      win.clearInterval(hudTimer);
      if (offEsc) offEsc();
      win.removeEventListener("keydown", onKey, true);
      win.removeEventListener("resize", resize);
      doc.removeEventListener("mousemove", onPointer, { capture: true });
      doc.removeEventListener("mouseout", onLeave, { capture: true });
      doc.removeEventListener("visibilitychange", onVisibility);
      host.remove();
    }

    return { host, model, update, setHidden, afterFrames, isOurs, escape, destroy,
             boxes: () => boxes };
  }

  const api = Object.freeze({
    CLIENT, HOST_ID, FLASH_MS, CLOSE_TEXT, LABEL_PAD, helloFields, patchBoxes, hitTest, borderPx,
    labelBox, createModel, create, photosensitive: F.photosensitive,
  });
  root.BrainPointOverlay = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
