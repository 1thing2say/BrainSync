/*
 * BrainScroll overlay: two SSVEP squares, a cue box, a HUD and the photosensitivity
 * warning, drawn over the page and driven by the BrainSync decoder.
 *
 *     BrainScrollOverlay.create({transport, scroller, agent: "extension"})
 *       transport = {send(obj), onMessage(cb), onState(cb)}   // cb(state): connecting|open|closed
 *       scroller  = async (dir) => ({ok, method})             // dir: "up" | "down"
 *
 * The protocol (config, phase, cue, scroll, status, eeg, ping from the server; hello, view,
 * frames, scrolled, bye from here) is the one brainsync/bridge.py serves on ws://127.0.0.1:8765.
 *
 * Timing follows brainsync/stimulus/flicker.py: luminance is a function of the frame
 * index, never of free-running wall-clock time. The frame index is the vsync count since
 * flicker onset, n = round((t - t0) / period), from requestAnimationFrame timestamps,
 * which Chrome aligns to vsync. A dropped frame therefore skips an index instead of
 * shifting the phase of every later frame. The period comes from the measured refresh:
 * the mean rAF interval over 120 frames after 20 unmeasured warm-up frames (the same
 * counts the PsychoPy stimulus uses), leaving out intervals more than half a period from
 * the median. Not the median itself: Chrome coarsens rAF timestamps to 100 us on pages
 * that are not cross-origin isolated, such as instagram.com, so at 60 Hz the intervals are
 * 16.6 or 16.7 ms and the median, 16.7, reads 59.88 Hz; the mean cancels the rounding.
 * Luminance itself is computed at the config's refresh_hz and modulation depth (default 1,
 * full black-white; still squares sit at 0.5, the flicker's mean at any depth), as the
 * stimulus does. Python refuses a display that is off by more than refresh_tolerance_hz;
 * here the HUD turns red and the squares are held still, because the frequencies would be
 * wrong.
 *
 * Nothing is drawn until the viewer presses Start on the warning. Escape stops the
 * flicker, removes the squares and tells the server ("bye"). A bye that could not go out
 * (socket or port down) goes out on the next open socket, once, and only within 15 s:
 * later it would reach a new run, not the one the viewer left. The squares never flicker without a server: when the
 * socket drops they are held still until the bridge, which sends the current phase to
 * every new connection, says to flicker again. Only an accepted, visible tab on Reels
 * scrolls; any other acks ok false, method "none". The server is told whether the squares
 * are in view (that, and the tab visible) in the hello and in a "view" message each time it
 * changes, and ignores the decoder while no tab shows them: with nothing on screen to look
 * at, its decisions are noise. The canvas and HUD never take pointer events, so the page
 * underneath keeps every click and its focus.
 *
 * The pure parts (geometry, frame index, refresh meter, message parsing and the
 * controller state machine) are exported for extension/test/overlay.test.mjs.
 */
(function (root) {
  "use strict";

  const F = root.BrainScrollFlicker ||
    (typeof require === "function" ? require("./flicker.js") : null);

  const DEFAULT_CONFIG = Object.freeze({
    name: "reels",
    waveform: "sine",
    depth: 1.0,
    refresh_hz: 60.0,
    refresh_tolerance_hz: 1.0,
    targets: Object.freeze([
      Object.freeze({ label: "up", freq: 15.0, phase: 0.0, x: 0.55, y: 0.325, size: 0.3 }),
      Object.freeze({ label: "down", freq: 17.0, phase: 0.0, x: 0.55, y: -0.325, size: 0.3 }),
    ]),
  });
  const PHASES = Object.freeze(["wait", "calibrate", "run", "stop"]);
  const CONN_STATES = Object.freeze(["connecting", "open", "closed"]);
  const EEG_STATES = Object.freeze(["unknown", "absent", "connecting", "connected", "streaming",
    "no-data", "error", "unused"]);
  // HUD wording per EEG state; streaming and unused are the only states that are not a warning
  const EEG_LABELS = Object.freeze({
    unknown: "unknown", absent: "not connected", connecting: "connecting…",
    connected: "connected, waiting for data", streaming: "connected, receiving data",
    "no-data": "no data", error: "could not connect", unused: "not used",
  });
  const SCROLL_METHODS = Object.freeze(["container", "window", "debugger", "none"]);
  const WARMUP_FRAMES = 20;      // unmeasured, as PsychoPyRenderer.measure_refresh
  const REFRESH_FRAMES = 120;    // intervals in the estimate
  const FRAMES_EVERY_MS = 5000;  // "frames" report period while flickering
  const BYE_RETRY_MS = 15000;    // a worker restart reconnects in 1-5 s
  const STILL_LUM = 0.5;         // still screen: squares at mid grey
  // Geometry in screen heights: cue outline 0.006 thick, its inner edge 0.03 outside the
  // square, or less where the screen edge is closer (the reels squares at y = +/-0.325,
  // size 0.3, leave 0.025: the gap shrinks to 0.019 so the outline is not clipped); a
  // black backing 0.015 wider than the square so it contrasts with any page.
  const CUE_GAP = 0.03;
  const CUE_LINE = 0.006;
  const BACKING = 0.015;
  const Z_TOP = 2147483647;

  const GREYS = [];
  for (let i = 0; i < 256; i++) GREYS.push("rgb(" + i + "," + i + "," + i + ")");

  // ---------------------------------------------------------------- pure helpers

  /** Square of target t in CSS px for a vw x vh viewport (y up, units of screen height). */
  function squareRect(t, vw, vh) {
    const side = t.size * vh;
    const cx = vw / 2 + t.x * vh;
    const cy = vh / 2 - t.y * vh;
    return { cx, cy, side, left: cx - side / 2, top: cy - side / 2 };
  }

  /** Gap between square and cue outline, screen heights: CUE_GAP, less if the edge is closer. */
  function cueGap(t, vw, vh) {
    const room = Math.min(0.5 - Math.abs(t.y), vw / vh / 2 - Math.abs(t.x)) - t.size / 2 - CUE_LINE;
    return Math.max(0, Math.min(CUE_GAP, room));
  }

  /** Cue outline of target t: the stroke's centre-line square and its line width, CSS px. */
  function cueRect(t, vw, vh) {
    const sq = squareRect(t, vw, vh);
    const line = CUE_LINE * vh;
    const side = sq.side + 2 * (cueGap(t, vw, vh) * vh) + line;
    return { left: sq.cx - side / 2, top: sq.cy - side / 2, side, line };
  }

  /** Black backing square behind target t, CSS px. */
  function backingRect(t, vw, vh) {
    const sq = squareRect(t, vw, vh);
    const side = sq.side + 2 * BACKING * vh;
    return { left: sq.cx - side / 2, top: sq.cy - side / 2, side };
  }

  /**
   * Warning text when a target's cue box spills past the viewport (as layout_overflow in
   * brainsync/stimulus/display.py), else null. Units: screen heights from the centre.
   */
  function layoutOverflow(cfg, vw, vh) {
    const halfW = vw / vh / 2;
    const bad = [];
    for (const t of cfg.targets) {
      const reach = t.size / 2 + cueGap(t, vw, vh) + CUE_LINE;
      const x = Math.abs(t.x) + reach, y = Math.abs(t.y) + reach;
      if (x > halfW + 1e-9 || y > 0.5 + 1e-9) {
        bad.push(t.label + " reaches x = " + x.toFixed(3) + ", y = " + y.toFixed(3));
      }
    }
    if (!bad.length) return null;
    return "cue boxes spill off this " + (2 * halfW).toFixed(3) + ":1 screen (edges at x = " +
      halfW.toFixed(3) + ", y = 0.5): " + bad.join("; ");
  }

  /** Vsync count since flicker onset t0 (ms timestamps, period in ms). */
  function frameIndex(ts, t0, period) {
    return Math.round((ts - t0) / period);
  }

  /** True when one rAF interval spans more than DROP_FACTOR refresh periods. */
  function isDropped(interval, period) {
    return interval > F.DROP_FACTOR * period;
  }

  /** Median of the first n values of a numeric array (sorts `scratch` in place). */
  function median(values, n, scratch) {
    if (n === undefined) n = values.length;
    if (n <= 0) return NaN;
    const s = scratch || new Float64Array(n);
    for (let i = 0; i < n; i++) s[i] = values[i];
    const view = s.length === n ? s : s.subarray(0, n);
    view.sort();
    return n % 2 ? view[(n - 1) / 2] : 0.5 * (view[n / 2 - 1] + view[n / 2]);
  }

  /**
   * Rolling refresh estimate from rAF intervals: skips `warmup` intervals, then keeps the
   * last `size` in a ring. hz() is 1000 / the mean of the intervals within 0.5..1.5 times
   * the median (no dropped frames or stalls), once `size` were seen.
   */
  class RefreshMeter {
    constructor(size, warmup) {
      this.size = size || REFRESH_FRAMES;
      this.warmup = warmup === undefined ? WARMUP_FRAMES : warmup;
      this.buf = new Float64Array(this.size);
      this.scratch = new Float64Array(this.size);
      this.skipped = 0;
      this.total = 0;
    }

    push(interval) {
      if (!(interval > 0) || !Number.isFinite(interval)) return false;
      if (this.skipped < this.warmup) { this.skipped++; return false; }
      this.buf[this.total % this.size] = interval;
      this.total++;
      return true;
    }

    ready() {
      return this.total >= this.size;
    }

    /** True on the frames where a fresh estimate is due (every `size` intervals). */
    due() {
      return this.ready() && this.total % this.size === 0;
    }

    hz() {
      if (!this.ready()) return null;
      const med = median(this.buf, this.size, this.scratch);
      let sum = 0, n = 0;
      for (let i = 0; i < this.size; i++) {
        const v = this.buf[i];
        if (v > 0.5 * med && v < 1.5 * med) { sum += v; n++; }
      }
      return 1000 / (sum / n);   // n >= 1: the median itself is inside the band
    }
  }

  /** Parse one server message (JSON text or object): {msg} or {error}. */
  function parseMessage(raw) {
    let msg = raw;
    if (typeof raw === "string") {
      try { msg = JSON.parse(raw); } catch (e) { return { error: "not JSON: " + raw.slice(0, 80) }; }
    }
    if (msg === null || typeof msg !== "object" || Array.isArray(msg)) {
      return { error: "not a JSON object" };
    }
    if (typeof msg.type !== "string") return { error: "no string \"type\"" };
    return { msg };
  }

  function finite(v) {
    return typeof v === "number" && Number.isFinite(v);
  }

  /** Validated copy of a config message; throws Error naming the bad field. */
  function parseConfig(msg) {
    const waveform = msg.waveform === undefined ? "sine" : msg.waveform;
    if (F.WAVEFORMS.indexOf(waveform) < 0) throw new Error("unknown waveform " + JSON.stringify(waveform));
    const depth = msg.depth === undefined ? DEFAULT_CONFIG.depth : msg.depth;
    if (!finite(depth) || depth <= 0 || depth > 1) throw new Error("depth must be a number in (0, 1]");
    const refresh = msg.refresh_hz === undefined ? DEFAULT_CONFIG.refresh_hz : msg.refresh_hz;
    if (!finite(refresh) || refresh <= 0) throw new Error("refresh_hz must be a positive number");
    const tol = msg.refresh_tolerance_hz === undefined ? DEFAULT_CONFIG.refresh_tolerance_hz
      : msg.refresh_tolerance_hz;
    if (!finite(tol) || tol < 0) throw new Error("refresh_tolerance_hz must be a number >= 0");
    if (!Array.isArray(msg.targets) || msg.targets.length === 0) throw new Error("targets must be a non-empty list");
    const targets = msg.targets.map((t, k) => {
      if (t === null || typeof t !== "object") throw new Error("target " + k + " is not an object");
      const phase = t.phase === undefined ? 0 : t.phase;
      for (const [key, v] of [["freq", t.freq], ["phase", phase], ["x", t.x], ["y", t.y], ["size", t.size]]) {
        if (!finite(v)) throw new Error("target " + k + " " + key + " must be a number");
      }
      if (t.freq <= 0) throw new Error("target " + k + " freq must be positive");
      if (t.size <= 0) throw new Error("target " + k + " size must be positive");
      return Object.freeze({ label: String(t.label === undefined ? k : t.label), freq: t.freq,
                             phase, x: t.x, y: t.y, size: t.size });
    });
    return Object.freeze({ name: String(msg.name || "config"), waveform, depth, refresh_hz: refresh,
                           refresh_tolerance_hz: tol, targets: Object.freeze(targets) });
  }

  // ------------------------------------------------------------------ controller

  /**
   * Protocol state machine with no DOM. The view calls frame(ts) once per rAF and reads
   * state, lum() and the predicates; transport callbacks go to handle() and setConnection().
   */
  function createController(opts) {
    opts = opts || {};
    const transport = opts.transport;
    const scroller = opts.scroller || (async () => ({ ok: false, method: "none" }));
    const agent = opts.agent || "page";
    const getUrl = opts.url || (() => (root.location ? String(root.location.href) : ""));
    const now = opts.now || (() => Date.now());
    const warn = opts.warn || ((...a) => console.warn("[BrainScroll]", ...a));
    const visible = opts.visible ||
      (() => !(root.document && root.document.visibilityState === "hidden"));
    const meter = new RefreshMeter(opts.refreshFrames, opts.warmupFrames);

    const state = {
      conn: "closed", config: DEFAULT_CONFIG, phase: null, flicker: false, seconds: null,
      text: "", phaseAt: null, cue: -1, status: "", eeg: "unknown", eegText: "",
      accepted: false, cancelled: false,
      quit: false, quitAt: null, refreshHz: null, dropped: 0, helloSent: false, byeSent: false,
      viewSent: null, paused: false, lastScroll: null, sent: 0, ignored: 0, version: 0,
    };
    let lum = new Float64Array(state.config.targets.length).fill(STILL_LUM);
    let t0 = null, period = null, lastTs = null, lastFramesAt = null;
    let scrollChain = Promise.resolve();

    function changed() { state.version++; maybeView(); }

    function send(obj) {
      try { transport.send(obj); state.sent++; } catch (e) { warn("send failed:", e && e.message); }
    }

    function showSquares() {
      return state.accepted && !state.quit && !state.paused && state.phase !== "stop";
    }

    /** The squares are in front of the viewer: drawn, and the tab can be seen. */
    function showing() {
      return showSquares() && visible();
    }

    function flickering() {
      return showSquares() && state.flicker && !refreshOff();
    }

    function refreshOff() {
      return state.refreshHz !== null &&
        Math.abs(state.refreshHz - state.config.refresh_hz) > state.config.refresh_tolerance_hz;
    }

    function maybeHello() {
      if (!state.accepted || state.quit || state.refreshHz === null) return;
      if (state.conn !== "open" || state.helloSent) return;
      state.helloSent = true;
      state.viewSent = showing();
      send({ type: "hello", agent, url: getUrl(), refresh_hz: round3(state.refreshHz),
             dropped: state.dropped, accepted: true, showing: state.viewSent });
    }

    /** Tell the server when the squares come into or go out of view (after the hello). */
    function maybeView() {
      if (state.conn !== "open" || !state.helloSent || state.quit) return;
      const inView = showing();
      if (inView === state.viewSent) return;
      state.viewSent = inView;
      send({ type: "view", showing: inView });
    }

    function maybeBye() {
      if (!state.quit || state.byeSent || state.conn !== "open") return;
      if (now() - state.quitAt > BYE_RETRY_MS) {
        // Seen live: an Escaped tab left open greeted the next brainsync.reels with its
        // old bye and ended that run before it started.
        state.byeSent = true;
        warn("bye not delivered within " + BYE_RETRY_MS / 1000 + " s; dropped");
        return;
      }
      try {
        transport.send({ type: "bye" });
        state.sent++;
        state.byeSent = true;
      } catch (e) {
        warn("send failed:", e && e.message);
      }
    }

    function ignore(reason) {
      state.ignored++;
      warn("ignored message:", reason);
    }

    async function doScroll(dir, id) {
      let r;
      // Every tab gets every scroll: only an accepted tab on Reels that can be seen moves.
      if (state.quit || !state.accepted || state.paused || !visible()) {
        r = { ok: false, method: "none" };
      } else {
        try {
          r = (await scroller(dir)) || {};
        } catch (e) {
          warn("scroll failed:", e && e.message);
          r = { ok: false, method: "none" };
        }
      }
      const method = SCROLL_METHODS.indexOf(r.method) >= 0 ? r.method : "none";
      const ok = r.ok === true;
      send({ type: "scrolled", id, ok, method });
      state.lastScroll = { dir, id, ok, method, at: now() };
      changed();
    }

    function handle(raw) {
      const parsed = parseMessage(raw);
      if (parsed.error) return ignore(parsed.error);
      const m = parsed.msg;
      switch (m.type) {
        case "config": {
          let cfg;
          try { cfg = parseConfig(m); } catch (e) { return ignore("config: " + e.message); }
          state.config = cfg;
          lum = new Float64Array(cfg.targets.length).fill(STILL_LUM);
          if (state.cue >= cfg.targets.length) state.cue = -1;
          t0 = null;
          changed();
          return;
        }
        case "phase": {
          if (PHASES.indexOf(m.phase) < 0) return ignore("phase: unknown phase " + JSON.stringify(m.phase));
          if (typeof m.flicker !== "boolean") return ignore("phase: flicker must be true or false");
          const seconds = m.seconds === undefined ? null : m.seconds;
          if (seconds !== null && !finite(seconds)) return ignore("phase: seconds must be a number or null");
          const wasFlickering = state.flicker && state.phase !== "stop";
          state.phase = m.phase;
          state.flicker = m.flicker && m.phase !== "stop";
          state.seconds = seconds;
          state.text = typeof m.text === "string" ? m.text : "";
          state.phaseAt = now();
          // calibrate(flicker) -> run(flicker) keeps one onset, so the phase runs on.
          if (!state.flicker || !wasFlickering) t0 = null;
          changed();
          return;
        }
        case "cue": {
          const k = m.target;
          if (!Number.isInteger(k) || k < -1 || k >= state.config.targets.length) {
            return ignore("cue: target must be -1.." + (state.config.targets.length - 1));
          }
          state.cue = k;
          changed();
          return;
        }
        case "scroll": {
          if (m.dir !== "up" && m.dir !== "down") return ignore("scroll: dir must be up or down");
          if (!Number.isInteger(m.id)) return ignore("scroll: id must be an integer");
          // One at a time: the scroller waits up to 1.5 s to verify, and two smooth scrolls at
          // once would land between reels.
          scrollChain = scrollChain.then(() => doScroll(m.dir, m.id));
          return;
        }
        case "status":
          if (typeof m.text !== "string") return ignore("status: text must be a string");
          state.status = m.text;
          changed();
          return;
        case "eeg":
          if (EEG_STATES.indexOf(m.state) < 0) return ignore("eeg: unknown state " + JSON.stringify(m.state));
          if (m.text !== undefined && typeof m.text !== "string") return ignore("eeg: text must be a string");
          state.eeg = m.state;
          state.eegText = m.text || "";
          changed();
          return;
        case "ping":
          return;
        default:
          return ignore("unknown type " + JSON.stringify(m.type));
      }
    }

    function setConnection(conn) {
      if (CONN_STATES.indexOf(conn) < 0) return ignore("unknown connection state " + JSON.stringify(conn));
      if (conn === state.conn) return;
      const wasOpen = state.conn === "open";
      state.conn = conn;
      // Without a server nothing reports on the EEG board; the bridge re-sends its state
      // on reconnect.
      if (conn !== "open") { state.eeg = "unknown"; state.eegText = ""; }
      // Each new socket is a new server connection: it needs its own hello. A bye goes
      // out once per page (maybeBye).
      state.helloSent = false;
      state.viewSent = null;
      if (wasOpen && state.phase !== null && state.phase !== "stop") {
        // No server drives the decoder now (killed, crashed, extension reloaded): hold the
        // squares still. On reconnect the bridge sends config and its current phase, and
        // that restarts the flicker with a new onset if the run is still going.
        state.flicker = false;
        t0 = null;
        state.seconds = null;
        state.cue = -1;
        state.text = "BrainSync server disconnected: flicker stopped. It resumes when the " +
          "server is back.";
      }
      maybeHello();
      maybeBye();
      changed();
    }

    function accept() {
      if (state.quit) return;
      state.accepted = true;
      state.cancelled = false;
      maybeHello();
      changed();
    }

    function cancel() {
      state.cancelled = true;
      changed();
    }

    function quit() {
      if (state.quit) return;
      state.quit = true;
      state.quitAt = now();
      t0 = null;
      maybeBye();
      changed();
    }

    /** Hide the squares (and refuse scrolls) while the page is not Reels. */
    function setPaused(paused) {
      if (state.paused === Boolean(paused)) return;
      state.paused = Boolean(paused);
      t0 = null;
      changed();
    }

    /**
     * One rAF tick at timestamp ts (ms). Returns "hidden", "still" or "flicker"; in the
     * flicker case lum() holds this frame's luminance per target.
     */
    function frame(ts) {
      const flick = flickering();
      if (lastTs !== null) {
        const dt = ts - lastTs;
        const counted = meter.push(dt);
        if (flick && t0 !== null && isDropped(dt, period)) state.dropped++;
        if (counted && meter.due()) {
          state.refreshHz = meter.hz();
          maybeHello();
          changed();
        }
      }
      lastTs = ts;
      if (!showSquares()) { t0 = null; return "hidden"; }
      if (!flick) { t0 = null; return "still"; }
      const cfg = state.config;
      if (t0 === null) {
        t0 = ts;
        period = 1000 / (state.refreshHz || cfg.refresh_hz);
        lastFramesAt = ts;
      }
      const n = frameIndex(ts, t0, period);
      for (let k = 0; k < cfg.targets.length; k++) {
        const t = cfg.targets[k];
        lum[k] = F.luminance(t.freq, t.phase, n, cfg.refresh_hz, cfg.waveform, cfg.depth);
      }
      if (ts - lastFramesAt >= FRAMES_EVERY_MS) {
        lastFramesAt = ts;
        send({ type: "frames", refresh_hz: round3(state.refreshHz || cfg.refresh_hz),
               dropped: state.dropped });
      }
      return "flicker";
    }

    function remaining() {
      if (state.seconds === null || state.phaseAt === null) return null;
      return Math.max(0, state.seconds - (now() - state.phaseAt) / 1000);
    }

    return {
      state, handle, setConnection, accept, cancel, quit, setPaused, frame, showSquares,
      showing, flickering, refreshOff, remaining,
      checkView: maybeView,   // the view calls it when the page's visibility changes
      lum: () => lum,
      onset: () => t0,
      idle: () => scrollChain,
    };
  }

  function round3(v) {
    return Math.round(v * 1000) / 1000;
  }

  // -------------------------------------------------------------------- the view

  const CSS = `
    :host { all: initial; }
    canvas { position: fixed; left: 0; top: 0; pointer-events: none; }
    .hud { position: fixed; left: 12px; bottom: 12px; max-width: 30vw; pointer-events: none;
           font: 12px/1.45 ui-monospace, Menlo, monospace; color: #ddd;
           background: rgba(0,0,0,0.6); border-radius: 6px; padding: 6px 9px; white-space: pre-wrap; }
    .hud .warn { color: #ff5b5b; font-weight: bold; }
    .banner { position: fixed; left: 50%; top: 16px; transform: translateX(-50%); max-width: 60vw;
              pointer-events: none; font: 16px/1.4 -apple-system, system-ui, sans-serif; color: #fff;
              background: rgba(0,0,0,0.7); border-radius: 8px; padding: 8px 14px; text-align: center; }
    .banner:empty { display: none; }
    .modal { position: fixed; left: 50%; top: 50%; transform: translate(-50%, -50%); width: min(520px, 90vw);
             pointer-events: auto; font: 15px/1.5 -apple-system, system-ui, sans-serif; color: #111;
             background: #fff; border-radius: 12px; padding: 22px 24px; box-shadow: 0 10px 40px rgba(0,0,0,0.5); }
    .modal h2 { margin: 0 0 10px; font-size: 19px; color: #b00020; }
    .modal p { margin: 0 0 10px; }
    .modal .row { display: flex; gap: 10px; justify-content: flex-end; margin-top: 16px; }
    .modal button { font: inherit; padding: 7px 18px; border-radius: 8px; border: 1px solid #888;
                    background: #f2f2f2; color: #111; cursor: pointer; }
    .modal button.start { background: #0a66c2; border-color: #0a66c2; color: #fff; }
    [hidden] { display: none !important; }
  `;

  function warningHtml(cfg) {
    const freqs = cfg.targets.map((t) => t.freq + " Hz").join(" and ");
    const risky = cfg.targets.some((t) => F.photosensitive(t.freq));
    return `
      <h2>Photosensitivity warning</h2>
      <p>BrainScroll flickers ${cfg.targets.length} squares at ${freqs}.
         Flicker between 15 and 25 Hz is the band most likely to trigger a seizure in people
         with photosensitive epilepsy${risky ? ", and these squares are inside it" : ""}.</p>
      <p><b>Do you, or does anyone who can see this screen, have epilepsy or a history of
         seizures?</b> If so, press Cancel.</p>
      <p>Press Escape at any time to stop the flicker. Stop at once if anyone feels unwell.</p>
      <div class="row"><button class="cancel" type="button">Cancel</button>
        <button class="start" type="button">Start</button></div>`;
  }

  /** The HUD's EEG line, e.g. "EEG: connected, receiving data · /dev/cu.usbserial-110". */
  function eegLine(s) {
    return "EEG: " + (EEG_LABELS[s.eeg] || s.eeg) + (s.eegText ? " · " + s.eegText : "");
  }

  /** Mount the overlay on the page. Returns {controller, destroy}. */
  function create(opts) {
    opts = opts || {};
    const win = opts.win || root;
    const doc = opts.doc || win.document;
    const controller = createController(opts);
    const s = controller.state;

    const host = doc.createElement("div");
    host.id = "brainscroll-overlay";
    host.style.cssText = "position:fixed;left:0;top:0;width:0;height:0;pointer-events:none;" +
      "z-index:" + Z_TOP + ";";
    // A shadow root keeps Instagram's CSS off the overlay and the overlay's off Instagram.
    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `<style>${CSS}</style><canvas></canvas><div class="banner"></div>
      <div class="hud"><div class="l1"></div><div class="l2"></div><div class="l3"></div><div class="l4"></div><div class="l5"></div></div>
      <div class="modal" role="dialog" aria-modal="false"></div>`;
    const canvas = shadow.querySelector("canvas");
    const banner = shadow.querySelector(".banner");
    const hudLines = [".l1", ".l2", ".l3", ".l4", ".l5"].map((q) => shadow.querySelector(q));
    const modal = shadow.querySelector(".modal");
    modal.innerHTML = warningHtml(s.config);
    (doc.documentElement || doc.body).appendChild(host);
    const ctx = canvas.getContext("2d", { alpha: true });

    let dpr = 1, vw = 0, vh = 0, rects = [], drawnVersion = -1, geomDirty = true;
    let rafId = 0, hudTimer = 0, destroyed = false;
    const hudCache = ["", "", "", ""];

    function devRect(r) {
      const left = Math.round(r.left * dpr), top = Math.round(r.top * dpr);
      const side = Math.round(r.side * dpr);
      return { left, top, side };
    }

    function layout() {
      dpr = win.devicePixelRatio || 1;
      vw = win.innerWidth;
      vh = win.innerHeight;   // Chrome is expected full screen: H is the window height
      canvas.style.width = vw + "px";
      canvas.style.height = vh + "px";
      canvas.width = Math.round(vw * dpr);
      canvas.height = Math.round(vh * dpr);
      geomDirty = true;
    }

    function computeRects() {
      rects = s.config.targets.map((t) => {
        const cue = cueRect(t, vw, vh);
        return { square: devRect(squareRect(t, vw, vh)), backing: devRect(backingRect(t, vw, vh)),
                 cue: { ...devRect(cue), line: Math.max(1, Math.round(cue.line * dpr)) } };
      });
    }

    let warnedLayout = null;
    function fullDraw(lumNow) {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (!controller.showSquares()) return;
      computeRects();
      const problem = layoutOverflow(s.config, vw, vh);
      if (problem && problem !== warnedLayout) console.warn("[BrainScroll] warning:", problem);
      warnedLayout = problem;
      for (let k = 0; k < rects.length; k++) {
        const r = rects[k];
        ctx.fillStyle = "#000";
        ctx.fillRect(r.backing.left, r.backing.top, r.backing.side, r.backing.side);
        ctx.fillStyle = GREYS[Math.round(lumNow[k] * 255)];
        ctx.fillRect(r.square.left, r.square.top, r.square.side, r.square.side);
      }
      if (s.cue >= 0 && s.cue < rects.length) {
        const c = rects[s.cue].cue;
        ctx.strokeStyle = "#fff";
        ctx.lineWidth = c.line;
        ctx.strokeRect(c.left, c.top, c.side, c.side);
      }
    }

    let still = new Float64Array(0);
    function stillLums(n) {
      if (still.length !== n) still = new Float64Array(n).fill(STILL_LUM);
      return still;
    }

    function loop(ts) {
      if (destroyed) return;
      rafId = win.requestAnimationFrame(loop);
      const mode = controller.frame(ts);
      const lumNow = mode === "flicker" ? controller.lum() : stillLums(s.config.targets.length);
      if (geomDirty || s.version !== drawnVersion) {
        drawnVersion = s.version;
        geomDirty = false;
        fullDraw(lumNow);
      } else if (mode === "flicker") {
        // Per frame only the square interiors change: two fillRects, no allocation.
        for (let k = 0; k < rects.length; k++) {
          const r = rects[k].square;
          ctx.fillStyle = GREYS[Math.round(lumNow[k] * 255)];
          ctx.fillRect(r.left, r.top, r.side, r.side);
        }
      }
    }

    function setLine(i, text, warnOn) {
      if (hudCache[i] === text + (warnOn ? "!" : "")) return;
      hudCache[i] = text + (warnOn ? "!" : "");
      hudLines[i].textContent = text;
      hudLines[i].className = "l" + (i + 1) + (warnOn ? " warn" : "");
    }

    function updateHud() {
      const cfg = s.config;
      setLine(0, "BrainScroll · server " + s.conn + (s.status ? " · " + s.status : ""), s.conn !== "open");
      let phase;
      if (s.quit) phase = "stopped (Escape)";
      else if (s.cancelled && !s.accepted) phase = "cancelled: reload the page to start";
      else if (!s.accepted) phase = "waiting for Start" + (s.text ? ": " + s.text : "");
      else if (s.paused) phase = "paused: not on Reels";
      else if (s.phase === null) phase = "waiting for the server";
      else {
        const left = controller.remaining();
        phase = s.phase + (controller.flickering() ? " (flicker)"
          : s.flicker && controller.refreshOff() ? " (still: refresh off)" : " (still)") +
          (left !== null ? " " + Math.ceil(left) + " s" : "") + (s.text ? ": " + s.text : "");
      }
      setLine(1, phase, false);
      setLine(4, eegLine(s), s.eeg !== "streaming" && s.eeg !== "unused");
      const ref = s.refreshHz === null ? "refresh measuring…"
        : "refresh " + s.refreshHz.toFixed(2) + " Hz (want " + cfg.refresh_hz + " ± " +
          cfg.refresh_tolerance_hz + ")" + (controller.refreshOff() ? " OFF: set the display to " +
          cfg.refresh_hz + " Hz" : "");
      setLine(2, ref + " · dropped " + s.dropped, controller.refreshOff());
      const ls = s.lastScroll;
      setLine(3, ls ? "scroll #" + ls.id + " " + ls.dir + ": " + (ls.ok ? "ok" : "failed") +
        " via " + ls.method : "no scroll yet", ls ? !ls.ok : false);
      let text = "";
      if (s.quit) text = "BrainScroll stopped. Reload the page to start again.";
      else if (s.accepted && s.phase !== null && s.text) text = s.text;
      if (banner.textContent !== text) banner.textContent = text;
      if (modal.hidden !== (s.accepted || s.cancelled || s.quit)) {
        modal.hidden = s.accepted || s.cancelled || s.quit;
      }
    }

    function onKey(e) {
      if (e.key !== "Escape") return;
      if (!modal.hidden) controller.cancel();
      else if (s.accepted && !s.quit) controller.quit();
      updateHud();
    }

    function onResize() {
      layout();
    }

    function onVisibility() {
      controller.checkView();
    }

    modal.addEventListener("click", (e) => {
      const button = e.target.closest ? e.target.closest("button") : null;
      if (!button) return;
      e.stopPropagation();
      if (button.classList.contains("start")) controller.accept();
      else controller.cancel();
      button.blur();
      updateHud();
    });
    let shownConfig = s.config;
    function refreshWarning() {
      // Keep the warning's frequencies in step with a config that arrives while it is up.
      if (!modal.hidden && shownConfig !== s.config) {
        shownConfig = s.config;
        modal.innerHTML = warningHtml(s.config);
      }
    }

    if (opts.transport) {
      opts.transport.onMessage((m) => controller.handle(m));
      opts.transport.onState((st) => controller.setConnection(st));
    }
    win.addEventListener("keydown", onKey, true);
    win.addEventListener("resize", onResize);
    doc.addEventListener("visibilitychange", onVisibility);
    layout();
    updateHud();
    hudTimer = win.setInterval(() => { refreshWarning(); updateHud(); }, 250);
    rafId = win.requestAnimationFrame(loop);

    function destroy() {
      destroyed = true;
      win.cancelAnimationFrame(rafId);
      win.clearInterval(hudTimer);
      win.removeEventListener("keydown", onKey, true);
      win.removeEventListener("resize", onResize);
      doc.removeEventListener("visibilitychange", onVisibility);
      host.remove();
    }

    return { controller, destroy, host };
  }

  const api = Object.freeze({
    DEFAULT_CONFIG, PHASES, WARMUP_FRAMES, REFRESH_FRAMES, FRAMES_EVERY_MS, STILL_LUM,
    CUE_GAP, CUE_LINE, BACKING, squareRect, cueRect, backingRect, cueGap, layoutOverflow, frameIndex, isDropped,
    EEG_STATES, eegLine, median, RefreshMeter, parseMessage, parseConfig, createController, create,
  });
  root.BrainScrollOverlay = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
