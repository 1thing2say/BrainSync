/*
 * BrainSync reference page: three squares, "yes", "no" and "help", in slots mode.
 *
 *     python -m brainsync.server --board fake --no-auth
 *     open http://127.0.0.1:8765/#token=<token>        (add &takeover=1 to replace a controller)
 *
 * The server serves this file as /static/app.js after /static/flicker.js and
 * /static/brainsync-client.js. The page shows the photosensitivity warning first and draws
 * nothing until the user accepts it. It then measures the display refresh over about two
 * seconds, and only then connects and sends hello with slots ["yes", "no"] and the measured
 * refresh_hz. The squares are drawn on one canvas with FlickerState, so they flicker only
 * under the safety rule of PROTOCOL-v3 section 4 and are still (mid grey) otherwise. The HUD
 * shows the phase, the selections, the round-trip time and the board state.
 *
 * When the server runs --board fake (welcome.fake), hovering a square with the mouse, or
 * touching it, sends gaze for its slot, and leaving it (or lifting the finger) sends gaze
 * null, so the whole loop can be tried without a headset. Esc, the Pause button (always on
 * screen once the warning is accepted, since phones have no Esc) and the page going to the
 * background all stop the flicker here at once and send pause. Nothing resumes by itself: the
 * Resume button sends resume, and it ignores presses for RESUME_GUARD_MS after a pause, so a
 * double tap on Pause cannot restart the flicker. After a refresh error the meter keeps
 * running, and once the display reports a good rate the page reconnects with a new hello.
 * After close code 4000, 4001 or 4002 it stops and says what to do. The token comes from the
 * address (#token=...) or a prompt and is never shown.
 *
 * On a phone (https://<name>.trycloudflare.com/#token=..., through the cloudflared tunnel)
 * the squares go in a column when the screen is portrait, keep out of the notch, the HUD and
 * the buttons, and are laid out again on resize and rotation. The page holds a screen wake
 * lock while the phase is run and it is not paused, and a Full screen button appears where
 * the browser allows it. Both are feature-detected and their failures are ignored.
 *
 * Code mode (the server's default, docs/CVEP-STANDARD.md): the squares are white on black
 * and show the server's m-sequence one bit a frame (white or black), a "cycle" message goes
 * out on every cycle's first frame, and under each square a label line shows its confidence
 * ("YES  ███░░░░░ +0.42 <"), with the status line below them all: "training: keep looking at
 * YES   cycle 12/30" while training (the cued square has a red box and the banner says where
 * to look), then "lead 0.31   choice: YES". The HUD keeps the template's held-out accuracy.
 * A server run with --paradigm freq gets the grey sine flicker and plain labels as before.
 *
 * The pure parts (hash parsing, square geometry, hit testing, the page model, the wake lock
 * and the HUD text) are exported for clients/js/test/web_app.test.mjs.
 */
(function (root) {
  "use strict";

  const C = root.BrainSyncClient ||
    (typeof require === "function" ? require("../js/brainsync-client.js") : null);
  if (!C) throw new Error("load brainsync-client.js before app.js");

  const SLOTS = Object.freeze(["yes", "no", "help"]);
  const CLIENT = Object.freeze({ name: "brainsync-web", kind: "web", version: "0.1.0" });
  const FLASH_MS = 700;          // outline around a selected square
  const CUE_COLOR = "#ff3b30";   // code mode: the box on the square to look at while training
  const MAX_SELECTIONS = 6;      // shown in the HUD
  const RESUME_GUARD_MS = 800;   // Resume does nothing this soon after a pause (double taps)
  const LAYOUT_MARGIN = 8;       // CSS px between the squares and the HUD or the buttons
  // What to tell the user after a close that does not reconnect by itself.
  const CLOSE_TEXT = Object.freeze({
    4000: "Another page took over the headset. Reload this page to take it back.",
    4001: "The server refused the token. Reload with #token=<the token the server printed>.",
    4002: "Another page is the controller. Reload with &takeover=1 in the address to take over.",
  });

  // ---------------------------------------------------------------- pure helpers

  /** {token, takeover} from location.hash such as "#token=abc&takeover=1". */
  function parseHash(hash) {
    const params = new URLSearchParams(String(hash || "").replace(/^#/, ""));
    const token = params.get("token");
    const takeover = params.get("takeover");
    return { token: token ? token : null, takeover: takeover === "1" || takeover === "true" };
  }

  /** The server's WebSocket address for a page served at `loc` (window.location). */
  function wsUrlFor(loc) {
    const scheme = loc.protocol === "https:" ? "wss://" : "ws://";
    return scheme + loc.host + "/ws";
  }

  function nonNegative(v) {
    return typeof v === "number" && v > 0 ? v : 0;
  }

  /**
   * n squares, centred, in CSS px for a w x h viewport: [{x, y, side}] with x, y the top-left
   * corner. They keep half a side apart. By default they go in one row, each at most 0.4 of
   * the height. opts.inset ({top, right, bottom, left}) keeps them that far from the edges
   * (the safe area, the HUD, the buttons). When the space left is wider than tall, a row takes
   * up to 0.8 of its height but never more than 0.4 of the whole height, so a landscape phone
   * whose HUD and buttons take most of the height still gets squares as big as fit, and none
   * is bigger than without insets. With opts.stack they go in one column instead when
   * the space left is taller than wide (a portrait phone), each at most 0.6 of its width, so
   * the squares are sized from the smaller side either way.
   */
  function squareLayout(n, w, h, opts) {
    if (!(n > 0) || !(w > 0) || !(h > 0)) return [];
    const o = opts || {};
    const ins = o.inset || {};
    const left = nonNegative(ins.left), top = nonNegative(ins.top);
    const aw = w - left - nonNegative(ins.right), ah = h - top - nonNegative(ins.bottom);
    if (!(aw > 0) || !(ah > 0)) return [];
    const span = n + 0.5 * (n - 1);      // the row (or column) length in sides, gaps included
    const out = [];
    if (o.stack && ah > aw) {
      const side = Math.min(0.6 * aw, (0.9 * ah) / span);
      const x = left + (aw - side) / 2;
      const y0 = top + (ah - span * side) / 2;
      for (let i = 0; i < n; i++) out.push({ x, y: y0 + i * 1.5 * side, side });
      return out;
    }
    const cap = ah > aw ? 0.4 * ah : Math.min(0.4 * h, 0.8 * ah);   // without inset: 0.4 * h
    const side = Math.min(cap, (0.9 * aw) / span);
    const x0 = left + (aw - span * side) / 2;
    const y = top + Math.max(0, (ah - side) / 2 - 0.04 * ah);
    for (let i = 0; i < n; i++) out.push({ x: x0 + i * 1.5 * side, y, side });
    return out;
  }

  /**
   * The insets {top, right, bottom, left} that keep the squares of a w x h viewport inside
   * `safe` (the safe-area rectangle), below the rects in `above` (the HUD) and above the
   * rects in `below` (the banner and the buttons), LAYOUT_MARGIN px clear of each. Rects are
   * DOMRect-like; null and empty ones (hidden elements) are skipped.
   */
  function layoutInsets(w, h, safe, above, below) {
    const shown = (r) => Boolean(r) && (r.width > 0 || r.height > 0);
    const s = shown(safe) ? safe : { top: 0, right: w, bottom: h, left: 0 };
    let top = Math.max(0, s.top), bottom = Math.max(0, h - s.bottom);
    for (const r of above || []) if (shown(r)) top = Math.max(top, r.bottom + LAYOUT_MARGIN);
    for (const r of below || []) if (shown(r)) bottom = Math.max(bottom, h - r.top + LAYOUT_MARGIN);
    return { top, right: Math.max(0, w - s.right), bottom, left: Math.max(0, s.left) };
  }

  /** Rates for the HUD: "60", "60 or 120", "60, 90, 120 or 144". */
  function rateList(rates) {
    const r = rates.map(String);
    return r.length < 2 ? r.join("") : r.slice(0, -1).join(", ") + " or " + r[r.length - 1];
  }

  /** Index of the square under (x, y), or -1. */
  function hitTest(squares, x, y) {
    for (let i = 0; i < squares.length; i++) {
      const s = squares[i];
      if (x >= s.x && x <= s.x + s.side && y >= s.y && y <= s.y + s.side) return i;
    }
    return -1;
  }

  /** The hello fields (type, protocol and token are added by Connection). */
  function helloFields(refreshHz, viewport, takeover) {
    return {
      role: "controller", takeover: Boolean(takeover), client: Object.assign({}, CLIENT),
      mode: "slots", slots: SLOTS.slice(), refresh_hz: Math.round(refreshHz * 1000) / 1000,
      accepted: true, viewport,
    };
  }

  /**
   * The screen wake lock of `nav` (navigator), kept to what the page wants: sync(true)
   * requests one unless it is held or on its way, sync(false) releases it. Without the API
   * nothing happens. Failures are ignored, and a refused request is not retried until the
   * page has stopped wanting the lock once (a pause and a resume), so a browser that says no
   * is not asked four times a second. When the browser drops the lock itself (the page was
   * hidden), the next sync(true) asks again.
   */
  function createWakeLock(nav) {
    const api = nav && nav.wakeLock && typeof nav.wakeLock.request === "function" ? nav.wakeLock : null;
    let sentinel = null;
    let pending = false;
    let wanted = false;
    let refused = false;

    function drop(s) {
      try { Promise.resolve(s.release()).catch(() => {}); } catch (e) { /* already released */ }
    }

    function request() {
      pending = true;
      let p;
      try { p = Promise.resolve(api.request("screen")); } catch (e) { p = Promise.reject(e); }
      p.then((s) => {
        pending = false;
        if (!s) return;
        if (!wanted) { drop(s); return; }
        sentinel = s;
        if (typeof s.addEventListener === "function") {
          s.addEventListener("release", () => { if (sentinel === s) sentinel = null; });
        }
      }, () => {
        pending = false;
        if (wanted) refused = true;   // refused after the page let go (hidden): ask again
      });
    }

    function sync(want) {
      wanted = Boolean(want);
      if (api === null) return;
      if (!wanted) {
        refused = false;
        if (sentinel !== null) { const s = sentinel; sentinel = null; drop(s); }
      } else if (sentinel === null && !pending && !refused) {
        request();
      }
    }

    return { sync, supported: api !== null, held: () => sentinel !== null };
  }

  /**
   * The Fullscreen API of `doc` ({enter, exit, active, change}), standard or webkit-prefixed,
   * or null where the browser does not offer it for the page (an iPhone, an iframe).
   */
  function fullscreenApi(doc) {
    const el = doc && doc.documentElement;
    if (!el) return null;
    if (doc.fullscreenEnabled && typeof el.requestFullscreen === "function") {
      return { enter: () => el.requestFullscreen(), exit: () => doc.exitFullscreen(),
               active: () => Boolean(doc.fullscreenElement), change: "fullscreenchange" };
    }
    if (doc.webkitFullscreenEnabled && typeof el.webkitRequestFullscreen === "function") {
      return { enter: () => el.webkitRequestFullscreen(), exit: () => doc.webkitExitFullscreen(),
               active: () => Boolean(doc.webkitFullscreenElement), change: "webkitfullscreenchange" };
    }
    return null;
  }

  // ------------------------------------------------------------------ the model

  /**
   * The page's state machine, without DOM. opts: send(msg) -> bool, now() in ms, wall (for
   * FlickerState). The view calls accept, measured, onState, onClose, onMessage, pause,
   * resume, hover and frame, and draws what frame returns.
   */
  function createModel(opts) {
    opts = opts || {};
    const send = opts.send || (() => false);
    const now = opts.now || Date.now;
    const flicker = new C.FlickerState({ wall: opts.wall });
    const confidence = C.ConfidenceState && C.code ? new C.ConfidenceState() : null;
    const state = {
      stage: "warning",          // warning | measuring | live
      conn: "closed",
      welcome: null,
      phase: null,
      phaseAt: null,
      selections: [],            // newest last: {slot, score}
      flash: null,               // {slot, until}
      error: null,               // the last error message from the server
      status: null,              // the last status message
      refreshHz: null,
      refreshError: false,
      ended: null,               // text after a close that does not reconnect
      paused: false,
      pausedAt: null,            // now() of the last pause
      gazeSent: undefined,       // the slot of the last gaze sent on this connection
    };

    function accept() {
      if (state.stage !== "warning") return;
      flicker.accept();
      state.stage = "measuring";
    }

    /** True when hz is within the welcome's tolerance of one of the rates it accepts. */
    function acceptedRate(hz) {
      const w = state.welcome;
      return Boolean(w) && flicker.refreshRates.some((r) => Math.abs(hz - r) <= w.refresh_tolerance_hz);
    }

    /**
     * A fresh refresh estimate. Returns "connect" for the first one, "reconnect" when the
     * server refused the last rate and this one is one of the rates the welcome accepts
     * (refresh_rates, or refresh_hz alone from an older server), else null.
     */
    function measured(hz) {
      if (!(hz > 0) || !Number.isFinite(hz)) return null;
      state.refreshHz = hz;
      flicker.setMeasuredHz(hz);
      if (state.stage === "measuring") { state.stage = "live"; return "connect"; }
      if (state.refreshError && acceptedRate(hz)) {
        state.refreshError = false;
        return "reconnect";
      }
      return null;
    }

    function onState(s) {
      state.conn = s;
      flicker.setOpen(s === "open");
      if (s !== "open") { state.gazeSent = undefined; state.phase = null; }
      if (s === "connecting") state.ended = null;
    }

    function onClose(info) {
      if (info && !info.reconnect && CLOSE_TEXT[info.code]) state.ended = CLOSE_TEXT[info.code];
    }

    function onMessage(msg) {
      flicker.handle(msg);
      if (confidence) confidence.handle(msg);
      switch (msg.type) {
        case "welcome":
          state.welcome = msg;
          state.error = null;
          state.refreshError = false;
          if (state.paused) send({ type: "pause" });
          break;
        case "phase":
          state.phase = msg;
          state.phaseAt = now();
          break;
        case "selection": {
          const l = flicker.layout;
          if (!l || msg.layout_id !== l.layout_id) break;
          state.selections.push({ slot: msg.slot, score: msg.score });
          if (state.selections.length > MAX_SELECTIONS) state.selections.shift();
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
        default:
          break;
      }
    }

    /**
     * Esc, the Pause button or a hidden page: stop the flicker here at once and tell the
     * server. Returns false when already paused (nothing is sent again).
     */
    function pause() {
      flicker.pause();
      if (state.paused) return false;
      state.paused = true;
      state.pausedAt = now();
      send({ type: "pause" });
      return true;
    }

    /** True while Resume would be ignored: within RESUME_GUARD_MS of the pause. */
    function resumeBlocked() {
      return state.paused && state.pausedAt !== null && now() - state.pausedAt < RESUME_GUARD_MS;
    }

    /** The Resume button. Returns true when it resumed. */
    function resume() {
      if (!state.paused || resumeBlocked()) return false;
      state.paused = false;
      flicker.resume();
      send({ type: "resume" });
      return true;
    }

    /** The screen should stay awake: live, phase run, not paused. */
    function wantsWakeLock() {
      return state.stage === "live" && !state.paused && state.conn === "open" &&
        state.phase !== null && state.phase.phase === "run";
    }

    /** The pointer is over `slot` (null: over nothing). Sends gaze on change, fake board only. */
    function hover(slot) {
      const w = state.welcome;
      if (!w || w.fake !== true || state.conn !== "open") return false;
      if (slot === undefined) slot = null;
      if (slot === state.gazeSent) return false;
      if (!send({ type: "gaze", slot })) return false;
      state.gazeSent = slot;
      return true;
    }

    function showSquares() {
      return state.stage !== "warning" && !(state.phase && state.phase.phase === "stop") &&
        flicker.targets().length > 0;
    }

    /**
     * One animation frame at rAF time ts (ms): sends onset and frames when due and returns
     * {mode: "hidden" | "still" | "flicker", items: [{slot, label, lum, flash}]}.
     */
    function frame(ts) {
      const targets = flicker.targets();
      const code = flicker.isCode();
      const lines = code && confidence ? confidence.lines(targets) : null;
      const cue = flicker.cue();
      const items = [];
      targets.forEach((t, i) => {
        const it = { slot: t.slot, label: t.label || t.slot, lum: flicker.luminance(t, ts),
                     flash: state.flash !== null && state.flash.slot === t.slot &&
                       now() < state.flash.until, cue: code && cue === t.slot };
        if (lines) { it.line = lines[i].line; it.level = lines[i].level; it.best = lines[i].best; }
        items.push(it);
      });
      const onset = flicker.onsetDue(ts);
      if (onset) send(onset);
      for (const c of flicker.cycleDue(ts)) send(c);
      const frames = flicker.framesDue(ts);
      if (frames) send(frames);
      if (!showSquares()) return { mode: "hidden", items: [], code, status: "" };
      return { mode: flicker.shouldFlicker() ? "flicker" : "still", items, code,
               fill: code && flicker.code ? flicker.code.fill : null,
               status: code && confidence ? confidence.status() : "" };
    }

    /** Seconds left in a timed phase (calibrate), or null. */
    function remaining() {
      const p = state.phase;
      if (!p || typeof p.seconds !== "number" || state.phaseAt === null) return null;
      return Math.max(0, p.seconds - (now() - state.phaseAt) / 1000);
    }

    /** The big centred line: what the user should do now, or "". */
    function banner() {
      if (state.stage === "warning") return "";
      if (state.ended) return state.ended;
      if (state.stage === "measuring") return "Measuring the display refresh…";
      if (state.paused) return "Paused. Press Resume to continue.";
      if (state.conn !== "open") return "Connecting to the server…";
      if (state.error && state.error.code === "refresh") return state.error.message;
      const p = state.phase;
      if (!p) return "Waiting for the server…";
      if (p.phase === "calibrate") {
        const left = remaining();
        return (p.text || "Calibrating") + (left === null ? "" : " (" + Math.ceil(left) + " s)");
      }
      if (p.phase === "train") return p.text || "Look at the marked square";
      if (p.phase === "wait") return p.text || "Waiting for the server…";
      if (p.phase === "stop") return p.text || "The server stopped.";
      if (p.phase === "pause") return p.text || "Paused by the server.";
      return p.text || "";
    }

    /** HUD lines, given the connection's rtt (ms, or null). */
    function hud(rttMs) {
      const p = state.phase;
      const lines = [];
      lines.push("phase " + (p ? p.phase : "-") + (flicker.shouldFlicker() ? " · flickering" : " · still") +
                 (state.paused ? " · paused" : ""));
      lines.push("server " + state.conn + (rttMs === null || rttMs === undefined ? "" : " · rtt " + rttMs + " ms") +
                 (state.welcome && state.welcome.fake ? " · fake board (hover or touch a square)" : ""));
      const st = state.status;
      lines.push("board " + (st ? st.board + (st.text ? " · " + st.text : "") : "-"));
      const w = state.welcome;
      lines.push("refresh " + (state.refreshHz === null ? "measuring…" : state.refreshHz.toFixed(2) + " Hz") +
                 (w ? " (want " + rateList(flicker.refreshRates) + " ± " + w.refresh_tolerance_hz + ")" : ""));
      if (flicker.isCode()) {
        const tpl = confidence ? confidence.trainedText() : "";
        const tr = confidence && confidence.train;
        lines.push("c-VEP code · " + (tpl || (tr ? "training " + tr.cycle + "/" + tr.of : "no template yet")));
      }
      lines.push("selections " + (state.selections.length
        ? state.selections.map((s) => s.slot).join(", ") : "-"));
      if (state.error) lines.push("error " + state.error.code + ": " + state.error.message);
      return lines;
    }

    return {
      state, flicker, confidence, accept, measured, onState, onClose, onMessage, pause, escape: pause,
      resume, resumeBlocked, wantsWakeLock, hover, frame, showSquares, remaining, banner, hud,
    };
  }

  // -------------------------------------------------------------------- the view

  const GREYS = [];
  for (let i = 0; i < 256; i++) GREYS.push("rgb(" + i + "," + i + "," + i + ")");

  /**
   * Wire the page in `win` / `doc`: warning gate, rAF loop, connection, input, HUD, wake lock.
   * opts.WebSocket, opts.timers and opts.now (ms) replace win's and Date.now (tests).
   */
  function start(win, doc, opts) {
    opts = opts || {};
    const $ = (id) => doc.getElementById(id);
    const canvas = $("stage");
    const ctx = canvas.getContext("2d");
    const hudEl = $("hud");
    const bannerEl = $("banner");
    const dockEl = $("dock");
    const safeEl = $("safe");
    const warning = $("warning");
    const pauseBtn = $("pause");
    const resumeBtn = $("resume");
    const fullscreenBtn = $("fullscreen");
    const timers = opts.timers || win;
    const hash = parseHash(win.location.hash);
    let token = hash.token;
    let conn = null;
    let squares = [];
    let lastTs = null;
    let reserve = { top: 0, right: 0, bottom: 0, left: 0 };   // kept clear of squares
    let tooSmall = false;         // paused because the squares had no room
    let focusNext = null;         // the button to focus once it can be (Resume after its guard)
    const meter = new C.RefreshMeter();
    const model = createModel({ send: (m) => (conn ? conn.send(m) : false), now: opts.now });
    const wake = createWakeLock(win.navigator);
    const fullscreen = fullscreenApi(doc);

    function rectOf(el) {
      return el && typeof el.getBoundingClientRect === "function" ? el.getBoundingClientRect() : null;
    }

    /**
     * Measure the safe area, the HUD and the dock. A fresh measure (resize) replaces the
     * reserve; the others only grow it, so the squares do not jump while the HUD or the
     * banner text changes length.
     */
    function measure(fresh) {
      const m = layoutInsets(win.innerWidth, win.innerHeight, rectOf(safeEl), [rectOf(hudEl)],
                             [rectOf(dockEl)]);
      if (fresh) { reserve = m; return; }
      for (const k of Object.keys(reserve)) reserve[k] = Math.max(reserve[k], m[k]);
    }

    function resize() {
      const dpr = win.devicePixelRatio || 1;
      canvas.width = Math.round(win.innerWidth * dpr);
      canvas.height = Math.round(win.innerHeight * dpr);
      canvas.style.width = win.innerWidth + "px";
      canvas.style.height = win.innerHeight + "px";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      measure(true);
    }

    function connect() {
      if (token === null) {
        const typed = win.prompt("Paste the BrainSync token the server printed " +
                                 "(leave empty if it runs with --no-auth):", "");
        token = typed ? typed.trim() : null;
      }
      conn = new C.Connection({
        url: wsUrlFor(win.location), token,
        hello: () => helloFields(model.state.refreshHz, {
          w: win.innerWidth, h: win.innerHeight, dpr: win.devicePixelRatio || 1 }, hash.takeover),
        log: (s) => win.console.warn("[BrainSync]", s),
        WebSocket: opts.WebSocket || win.WebSocket, timers,
      });
      conn.on("state", (s) => { model.onState(s); wake.sync(model.wantsWakeLock()); });
      conn.on("close", (info) => model.onClose(info));
      conn.on("message", (msg) => model.onMessage(msg));
      conn.open();
    }

    function labelPx() {
      return Math.max(16, Math.round(0.035 * Math.min(win.innerHeight, 1.5 * win.innerWidth)));
    }

    /** The squares for n targets in the current viewport, or [] when they have no room. */
    function layoutFor(n) {
      // code mode keeps a second line free under the labels for the status line
      const lines = model.flicker.isCode() ? 2.8 : 1.4;
      const inset = Object.assign({}, reserve, { bottom: reserve.bottom + lines * labelPx() });
      const sq = squareLayout(n, win.innerWidth, win.innerHeight, { inset, stack: true });
      return sq.length === n ? sq : [];
    }

    function draw(view) {
      const w = win.innerWidth, h = win.innerHeight;
      ctx.clearRect(0, 0, w, h);
      if (view.mode === "hidden") { squares = []; return; }
      const fontPx = labelPx();
      squares = layoutFor(view.items.length);
      if (squares.length === 0) return;   // no room at all (loop pauses before flicker)
      if (view.code) {                     // the standard: white squares on black
        ctx.fillStyle = "#000";
        ctx.fillRect(0, 0, w, h);
      }
      // code mode: label lines in a monospace font small enough to fit under a square
      const longest = view.items.reduce((m, it) => Math.max(m, (it.line || "").length), 0);
      const monoPx = longest ? Math.max(10, Math.min(fontPx, (1.4 * squares[0].side) /
                                                     (0.62 * longest))) : fontPx;
      ctx.font = (view.code ? monoPx + "px ui-monospace, Menlo, Consolas, monospace"
                            : fontPx + "px system-ui, sans-serif");
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      let bottom = 0;
      for (let i = 0; i < view.items.length; i++) {
        const it = view.items[i], sq = squares[i];
        const pad = 0.04 * sq.side;
        ctx.fillStyle = "#000";
        ctx.fillRect(sq.x - pad, sq.y - pad, sq.side + 2 * pad, sq.side + 2 * pad);
        ctx.fillStyle = view.code ? (it.lum >= 0.5 ? view.fill || "#fff" : "#000")
          : GREYS[Math.max(0, Math.min(255, Math.round(it.lum * 255)))];
        ctx.fillRect(sq.x, sq.y, sq.side, sq.side);
        bottom = Math.max(bottom, sq.y + sq.side + 3 * pad);
        if (it.cue) {
          ctx.strokeStyle = CUE_COLOR;
          ctx.lineWidth = Math.max(4, 0.04 * sq.side);
          ctx.strokeRect(sq.x - 2 * pad, sq.y - 2 * pad, sq.side + 4 * pad, sq.side + 4 * pad);
        } else if (it.flash) {
          ctx.strokeStyle = "#3ddc84";
          ctx.lineWidth = Math.max(4, 0.03 * sq.side);
          ctx.strokeRect(sq.x - 2 * pad, sq.y - 2 * pad, sq.side + 4 * pad, sq.side + 4 * pad);
        } else if (model.state.gazeSent === it.slot) {
          ctx.strokeStyle = "#7aa7ff";
          ctx.lineWidth = 2;
          ctx.setLineDash([8, 6]);
          ctx.strokeRect(sq.x - 2 * pad, sq.y - 2 * pad, sq.side + 4 * pad, sq.side + 4 * pad);
          ctx.setLineDash([]);
        }
        ctx.fillStyle = it.best ? "#ffffff" : "#e8e8e8";
        ctx.fillText(view.code && it.line ? it.line : it.label, sq.x + sq.side / 2,
                     sq.y + sq.side + 3 * pad);
      }
      if (view.code && view.status) {      // the status line, centred under all the squares
        const stack = squares.length > 1 && squares[1].x === squares[0].x;
        const y = stack ? bottom + 1.4 * monoPx
          : squares.reduce((m, sq) => Math.max(m, sq.y + sq.side), 0) +
            0.12 * squares[0].side + 1.4 * monoPx;
        ctx.fillStyle = "#e8e8e8";
        ctx.fillText(view.status, w / 2, y);
      }
    }

    function loop(ts) {
      if (lastTs !== null && meter.push(ts - lastTs) && meter.due()) {
        const action = model.measured(meter.hz());
        if (action === "connect") connect();
        else if (action === "reconnect" && conn) conn.reconnect();
      }
      lastTs = ts;
      // Squares with no room would flicker unseen while the server decodes: pause instead.
      const n = model.flicker.targets().length;
      if (n > 0 && model.flicker.shouldFlicker() && layoutFor(n).length === 0) {
        tooSmall = true;
        pauseNow();
      }
      draw(model.frame(ts));
      win.requestAnimationFrame(loop);
    }

    function updateHud() {
      const st = model.state;
      if (!st.paused) tooSmall = false;
      hudEl.textContent = model.hud(conn ? conn.rttMs : null).join("\n");
      bannerEl.textContent = tooSmall && !st.ended
        ? "Paused: the window is too small for the squares. Make it bigger, then press Resume."
        : model.banner();
      const focused = doc.activeElement;
      pauseBtn.hidden = st.stage === "warning" || st.paused;
      resumeBtn.hidden = !st.paused;
      resumeBtn.disabled = model.resumeBlocked();
      // Keep the keyboard on the control: the button that had focus hides, its twin takes over
      // (once enabled; a disabled button cannot take focus).
      if (focused === pauseBtn && pauseBtn.hidden) focusNext = resumeBtn;
      else if (focused === resumeBtn && resumeBtn.hidden) focusNext = pauseBtn;
      else if (focused && focused !== doc.body && focused !== pauseBtn && focused !== resumeBtn) focusNext = null;
      if (focusNext !== null && !focusNext.hidden && !focusNext.disabled &&
          typeof focusNext.focus === "function") {
        focusNext.focus();
        focusNext = null;
      }
      if (fullscreenBtn) {
        fullscreenBtn.hidden = fullscreen === null || st.stage === "warning";
        if (fullscreen !== null) fullscreenBtn.textContent = fullscreen.active() ? "Exit full screen" : "Full screen";
      }
      wake.sync(model.wantsWakeLock());
      measure(false);
    }

    /** Esc, the Pause button, a hidden page: still at once, pause sent, lock released. */
    function pauseNow() {
      if (model.state.stage === "warning") return;
      // enable Resume as soon as the guard is over, not on the next HUD tick
      if (model.pause()) timers.setTimeout(updateHud, RESUME_GUARD_MS);
      updateHud();
    }

    function pointer(ev) {
      if (ev.isPrimary === false) return;          // a second finger
      // squares are from the last frame; the layout may have changed since
      const t = model.flicker.targets()[hitTest(squares, ev.clientX, ev.clientY)];
      model.hover(t && model.showSquares() ? t.slot : null);
    }

    function pointerEnd(ev) {
      if (ev.isPrimary === false) return;
      if (ev.type === "pointerup" && ev.pointerType === "mouse") return;   // still hovering
      model.hover(null);
    }

    $("accept").addEventListener("click", () => {
      warning.hidden = true;
      model.accept();
      updateHud();
      win.requestAnimationFrame(loop);
    });
    pauseBtn.addEventListener("click", pauseNow);
    resumeBtn.addEventListener("click", () => { model.resume(); updateHud(); });
    if (fullscreenBtn && fullscreen !== null) {
      fullscreenBtn.addEventListener("click", () => {
        try {
          Promise.resolve(fullscreen.active() ? fullscreen.exit() : fullscreen.enter()).catch(() => {});
        } catch (e) { /* refused: stay as we are */ }
      });
      // Esc in full screen only leaves full screen (the page never sees the key), so leaving
      // full screen any way pauses, as the warning promises for Esc.
      doc.addEventListener(fullscreen.change, () => {
        if (!fullscreen.active()) pauseNow();
        resize();
        updateHud();
      });
    }
    win.addEventListener("keydown", (ev) => { if (ev.key === "Escape") pauseNow(); }, true);
    // A phone locking or switching apps pauses; coming back does not resume by itself.
    doc.addEventListener("visibilitychange", () => {
      if (doc.visibilityState === "hidden") pauseNow();
    });
    win.addEventListener("pagehide", () => { pauseNow(); if (conn) conn.close(); });
    // Back from the back-forward cache: reconnect, still paused (welcome re-sends pause).
    win.addEventListener("pageshow", (ev) => { if (ev && ev.persisted && conn) conn.open(); });
    win.addEventListener("resize", resize);
    // Some phones report the old size at orientationchange: lay out again a little later.
    win.addEventListener("orientationchange", () => { resize(); timers.setTimeout(resize, 300); });
    if (typeof win.PointerEvent === "function") {
      canvas.addEventListener("pointerdown", pointer);
      canvas.addEventListener("pointermove", pointer);
      for (const type of ["pointerup", "pointercancel", "pointerleave"]) {
        canvas.addEventListener(type, pointerEnd);
      }
    } else {
      canvas.addEventListener("mousemove", pointer);
      canvas.addEventListener("mouseleave", () => model.hover(null));
    }
    resize();
    updateHud();
    win.setInterval(updateHud, 250);
    return { model, connection: () => conn, squares: () => squares, wakeLock: wake };
  }

  const api = Object.freeze({
    SLOTS, CLIENT, CLOSE_TEXT, FLASH_MS, RESUME_GUARD_MS, LAYOUT_MARGIN, parseHash, wsUrlFor,
    squareLayout, layoutInsets, hitTest, helloFields, createWakeLock, fullscreenApi, createModel,
    start,
  });
  root.BrainSyncWeb = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else if (root.document) {
    if (root.document.readyState === "loading") {
      root.document.addEventListener("DOMContentLoaded", () => start(root, root.document));
    } else {
      start(root, root.document);
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
