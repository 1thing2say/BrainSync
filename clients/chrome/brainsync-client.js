/*
 * BrainSync client SDK: the parts every client of the v3 server shares (PROTOCOL-v3.md).
 *
 *     const C = BrainSyncClient                      // browser, after flicker.js
 *     const C = require("./brainsync-client.js")     // node tests
 *     const flicker = new C.FlickerState()
 *     const conn = new C.Connection({url: "ws://127.0.0.1:8765/ws", token,
 *                                    hello: () => ({role: "controller", mode: "slots", ...})})
 *     conn.on("message", (msg) => flicker.handle(msg))
 *     conn.on("state", (s) => flicker.setOpen(s === "open"))
 *     conn.open()
 *     // each requestAnimationFrame(ts): lum = flicker.luminance(target, ts);
 *     //   const onset = flicker.onsetDue(ts); if (onset) conn.send(onset)
 *
 * RefreshMeter is the refresh estimate of extension/overlay.js: 20 unmeasured warm-up rAF
 * intervals, then the mean of the last 120 intervals, leaving out those more than half a
 * median from the median. The mean, not the median, because Chrome coarsens rAF timestamps
 * to 100 us on pages that are not cross-origin isolated, and the median of 16.6 / 16.7 ms
 * intervals reads 59.88 Hz.
 *
 * FlickerState applies the flicker safety rule (PROTOCOL-v3 section 4, rule 1): targets
 * flicker only while the user accepted the warning, the socket is open, the last phase has
 * flicker true, a layout has targets, and the user has not paused locally (Esc). Anything
 * else draws them still at luminance 0.5. When the socket closes, the phase and the layout
 * are forgotten, so a reconnect never flickers on stale state before the server's catch-up
 * phase and layout arrive. Luminance is frame-indexed as in flicker.js: frame n is the vsync
 * count since the layout's first flickering frame, round((t - t0) / period), where the period
 * is that of the refresh the luminance is computed at (below), so a fresh refresh estimate
 * never rescales n and moves the phase in the middle of a layout. The luminance is computed at the
 * welcome's refresh_hz (the nominal rate) while the measured refresh is within the welcome's
 * refresh_tolerance_hz of it. A display further off, at another of the welcome's
 * refresh_rates (90, 120 or 144 Hz by default) or anywhere with a server run with
 * --ignore-refresh, gets its luminance computed at its own measured refresh, so every target
 * still flickers at its own frequency in real time: 15 Hz is 4 frames per cycle at 60 Hz and
 * 8 at 120 Hz. Computing it at 60 Hz there would scale every frequency by measured / 60
 * (12-20 Hz becoming 15-25 Hz at 75 Hz). A target at or above half that refresh cannot be
 * shown without aliasing and is drawn still. A pause or a closed socket restarts the count.
 * The dropped count grows by one for every late frame (a rAF interval over 1.5 periods), and
 * the frames report carries its running total, so the server can tell a display that drops
 * many. onsetDue() hands out the onset message once per layout message, on its first
 * flickering frame; a resume in the same layout sends none.
 *
 * Connection is one WebSocket with the hello handshake, reconnecting after 1, 2, 4, then
 * every 5 s, except after close codes 4000 (replaced by another controller), 4001 (bad
 * token) and 4002 (busy): those need the user, so it stops. Every hello carries
 * client.instance, a random id this Connection keeps for its lifetime (or opts.instance), so
 * the server lets the same client back in when its old socket died on the client's side only,
 * instead of answering busy. Every 10 s it sends a ping and keeps the round-trip time and the
 * clock offset t_server - t_client (the median of the last 8 samples). The token goes into
 * hello only and is never logged.
 */
(function (root) {
  "use strict";

  const F = root.BrainScrollFlicker ||
    (typeof require === "function" ? require("./flicker.js") : null);
  if (!F) throw new Error("load flicker.js before brainsync-client.js");

  const PROTOCOL = 3;
  const WARMUP_FRAMES = 20;          // unmeasured, as PsychoPyRenderer.measure_refresh
  const REFRESH_FRAMES = 120;        // intervals in the estimate
  const STILL_LUM = 0.5;             // still targets are mid grey
  const FRAMES_EVERY_MS = 5000;      // "frames" report period while flickering
  const BACKOFF_MS = Object.freeze([1000, 2000, 4000, 5000]);   // the last one repeats
  const PING_EVERY_MS = 10000;
  const OFFSET_SAMPLES = 8;
  const NO_RECONNECT_CODES = Object.freeze([4000, 4001, 4002]);
  const CONN_STATES = Object.freeze(["connecting", "open", "closed"]);
  const WS_OPEN = 1;

  function finite(v) {
    return typeof v === "number" && Number.isFinite(v);
  }

  function round3(v) {
    return Math.round(v * 1000) / 1000;
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
   * Rolling refresh estimate from rAF intervals (ms): skips `warmup` intervals, then keeps
   * the last `size` in a ring. hz() is 1000 / the mean of the intervals within 0.5..1.5
   * times the median, once `size` were seen, else null.
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

    /** Add one interval; false when it was skipped (warm-up, not positive, not finite). */
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

    /** True on the pushes where a fresh estimate is due (every `size` intervals). */
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

  /** Vsync count since flicker onset t0 (ms timestamps, period in ms). */
  function frameIndex(t, t0, period) {
    return Math.round((t - t0) / period);
  }

  /** True when one rAF interval spans more than DROP_FACTOR refresh periods. */
  function isDropped(interval, period) {
    return interval > F.DROP_FACTOR * period;
  }

  /** A normalized viewport rect {x, y, w, h} in CSS px for a w x h viewport. */
  function rectToPx(rect, w, h) {
    if (!rect || !finite(rect.x) || !finite(rect.y) || !finite(rect.w) || !finite(rect.h)) {
      throw new TypeError("rect needs finite x, y, w and h (normalized to the viewport)");
    }
    if (!finite(w) || !finite(h) || w < 0 || h < 0) {
      throw new RangeError("viewport size must be finite and >= 0, got " + w + " x " + h);
    }
    return { x: rect.x * w, y: rect.y * h, w: rect.w * w, h: rect.h * h };
  }

  /** Parse one server frame (JSON text or object): {msg} or {error}. */
  function parseMessage(raw) {
    let msg = raw;
    if (typeof raw === "string") {
      try { msg = JSON.parse(raw); } catch (e) { return { error: "not JSON: " + raw.slice(0, 80) }; }
    } else if (raw !== null && typeof raw === "object" &&
               (raw instanceof ArrayBuffer || ArrayBuffer.isView(raw) ||
                (typeof Blob !== "undefined" && raw instanceof Blob))) {
      return { error: "binary frame" };
    }
    if (msg === null || typeof msg !== "object" || Array.isArray(msg)) {
      return { error: "not a JSON object" };
    }
    if (typeof msg.type !== "string") return { error: "no string \"type\"" };
    return { msg };
  }

  /** A random id for hello.client.instance (not a secret: the token is the gate). */
  function newInstanceId() {
    const c = root.crypto;
    if (c && typeof c.randomUUID === "function") return c.randomUUID();
    let s = "";
    for (let i = 0; i < 4; i++) s += Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, "0");
    return s;
  }

  function defaultWall(tFrame) {
    const perf = root.performance;
    if (perf && finite(perf.timeOrigin) && finite(tFrame)) return (perf.timeOrigin + tFrame) / 1000;
    return Date.now() / 1000;
  }

  // ------------------------------------------------------------------- FlickerState

  /**
   * What to draw on each frame, from the server's messages and the user's choices.
   * opts.wall(tFrame) turns a rAF timestamp (ms) into client wall time (s) for the onset;
   * the default uses performance.timeOrigin.
   */
  class FlickerState {
    constructor(opts) {
      opts = opts || {};
      this.wall = opts.wall || defaultWall;
      this.accepted = false;
      this.open = false;
      this.paused = false;          // local Esc pause, sticky until resume()
      this.phase = null;            // the last phase message
      this.layout = null;           // the last layout message
      this.refreshHz = 60.0;        // the welcome's refresh_hz: luminance is computed at it
      this.refreshTolHz = 1.0;      // the welcome's refresh_tolerance_hz
      this.refreshRates = [60.0];   // the welcome's refresh_rates: the displays it accepts
      this.waveform = "sine";
      this.fake = false;
      this.measuredHz = null;       // the display's measured refresh: frame period
      this.dropped = 0;
      this.t0 = null;               // rAF timestamp of the layout's first flickering frame
      this.lastTs = null;
      this.lastFramesAt = null;
      this.onsetSent = false;
    }

    accept() { this.accepted = true; }

    /** The socket opened (true) or closed (false). Closing forgets phase and layout. */
    setOpen(open) {
      this.open = Boolean(open);
      if (!this.open) {
        this.phase = null;
        this.layout = null;
        this.onsetSent = false;
      }
      this._sync();
    }

    /** Esc: stop flickering at once, until resume(). */
    pause() { this.paused = true; this._sync(); }

    resume() { this.paused = false; }

    setMeasuredHz(hz) {
      this.measuredHz = finite(hz) && hz > 0 ? hz : null;
    }

    /** Take in one server message; the ones that matter here are welcome, phase and layout. */
    handle(msg) {
      if (!msg || typeof msg !== "object") return;
      if (msg.type === "welcome") {
        if (finite(msg.refresh_hz) && msg.refresh_hz > 0) this.refreshHz = msg.refresh_hz;
        if (finite(msg.refresh_tolerance_hz) && msg.refresh_tolerance_hz >= 0) {
          this.refreshTolHz = msg.refresh_tolerance_hz;
        }
        const rates = Array.isArray(msg.refresh_rates)
          ? msg.refresh_rates.filter((r) => finite(r) && r > 0) : [];
        this.refreshRates = rates.length ? rates : [this.refreshHz];
        if (F.WAVEFORMS.indexOf(msg.waveform) >= 0) this.waveform = msg.waveform;
        this.fake = msg.fake === true;
      } else if (msg.type === "phase") {
        this.phase = msg;
      } else if (msg.type === "layout") {
        this.layout = msg;
        this.t0 = null;
        this.onsetSent = false;
      }
      this._sync();
    }

    targets() {
      const l = this.layout;
      return l && Array.isArray(l.targets) ? l.targets : [];
    }

    /** Rule 1: accepted, socket open, last phase flicker true, layout with targets, not paused. */
    shouldFlicker() {
      return this.accepted && this.open && !this.paused && this.phase !== null &&
        this.phase.flicker === true && this.targets().length > 0;
    }

    /**
     * The frame period (ms) frames are counted at: that of lumHz(), so frame n always falls at
     * t0 + n / lumHz(). Counting at the measured refresh while the luminance used the nominal
     * one rescaled n at every new measurement and jumped the phase late in a long layout.
     */
    periodMs() {
      return 1000 / this.lumHz();
    }

    /** Advance to the frame at rAF timestamp tFrame (ms). Repeating a timestamp is a no-op. */
    tick(tFrame) {
      if (tFrame === this.lastTs) return;
      const flick = this.shouldFlicker();
      if (flick && this.t0 !== null && this.lastTs !== null && tFrame > this.lastTs &&
          isDropped(tFrame - this.lastTs, this.periodMs())) {
        this.dropped++;
      }
      this.lastTs = tFrame;
      if (!flick) { this.t0 = null; return; }
      if (this.t0 === null) { this.t0 = tFrame; this.lastFramesAt = tFrame; }
    }

    /** Frame index of tFrame, or null while still. */
    frame(tFrame) {
      this.tick(tFrame);
      if (!this.shouldFlicker() || this.t0 === null) return null;
      return frameIndex(tFrame, this.t0, this.periodMs());
    }

    /**
     * The refresh the luminance is computed at: the welcome's refresh_hz, or the measured one
     * when that is further than the tolerance from it (another accepted rate, or any rate with
     * --ignore-refresh), so the frequencies stay true in time.
     */
    lumHz() {
      const m = this.measuredHz;
      return m !== null && Math.abs(m - this.refreshHz) > this.refreshTolHz ? m : this.refreshHz;
    }

    /** Luminance in [0, 1] of `target` ({freq, phase}) on the frame at tFrame; 0.5 while still. */
    luminance(target, tFrame) {
      const n = this.frame(tFrame);
      if (n === null) return STILL_LUM;
      const hz = this.lumHz();
      if (!(finite(target.freq) && 2 * target.freq < hz)) return STILL_LUM;   // would alias
      return F.luminance(target.freq, finite(target.phase) ? target.phase : 0.0, n,
                         hz, this.waveform);
    }

    /** The onset message, once per layout, on (or after) its first flickering frame; else null. */
    onsetDue(tFrame) {
      this.tick(tFrame);
      if (this.onsetSent || !this.shouldFlicker() || this.t0 === null) return null;
      this.onsetSent = true;
      return { type: "onset", layout_id: this.layout.layout_id, t_client: this.wall(this.t0) };
    }

    /**
     * The frames report, every FRAMES_EVERY_MS while flickering; else null. `dropped` is the
     * running total since this FlickerState began; the server compares consecutive reports.
     */
    framesDue(tFrame) {
      this.tick(tFrame);
      if (!this.shouldFlicker() || this.lastFramesAt === null) return null;
      if (tFrame - this.lastFramesAt < FRAMES_EVERY_MS) return null;
      this.lastFramesAt = tFrame;
      return { type: "frames", refresh_hz: round3(this.measuredHz || this.refreshHz),
               dropped: this.dropped };
    }

    _sync() {
      if (!this.shouldFlicker()) this.t0 = null;
    }
  }

  // --------------------------------------------------------------------- Connection

  /**
   * One WebSocket to the server. opts: url, token (null without auth), hello (an object or a
   * function returning the hello fields; type, protocol and token are filled in here),
   * WebSocket, timers ({setTimeout, clearTimeout, setInterval, clearInterval}), now (ms),
   * log (a function for notes; never given the token), backoffMs, pingEveryMs, instance (the
   * hello's client.instance; a random id by default).
   */
  class Connection {
    constructor(opts) {
      opts = opts || {};
      if (typeof opts.url !== "string" || !/^wss?:\/\//.test(opts.url)) {
        throw new TypeError("url must be a ws:// or wss:// address such as " +
                            "ws://127.0.0.1:8765/ws, got " + JSON.stringify(opts.url));
      }
      if (opts.token !== undefined && opts.token !== null && typeof opts.token !== "string") {
        throw new TypeError("token must be a string or null (paste the token the server printed)");
      }
      if (opts.instance !== undefined && (typeof opts.instance !== "string" || !opts.instance ||
                                          opts.instance.length > 64)) {
        throw new TypeError("instance must be a string of 1 to 64 characters");
      }
      this.url = opts.url;
      this.instance = opts.instance || newInstanceId();
      this._token = opts.token === undefined ? null : opts.token;
      this._hello = opts.hello || {};
      this._WS = opts.WebSocket || root.WebSocket;
      if (typeof this._WS !== "function") throw new TypeError("no WebSocket class; pass opts.WebSocket");
      const t = opts.timers || root;
      this._setTimeout = t.setTimeout.bind(t);
      this._clearTimeout = t.clearTimeout.bind(t);
      this._setInterval = t.setInterval.bind(t);
      this._clearInterval = t.clearInterval.bind(t);
      this._now = opts.now || Date.now;
      this._log = opts.log || function () {};
      this.backoffMs = opts.backoffMs || BACKOFF_MS;
      this.pingEveryMs = opts.pingEveryMs || PING_EVERY_MS;
      this.state = "closed";
      this.ws = null;
      this.welcome = null;
      this.rttMs = null;
      this.offset = null;           // seconds, t_server - t_client
      this.closeCode = null;
      this.stopped = false;         // no automatic reconnect any more
      this.attempt = 0;             // failed attempts since the last open
      this._listeners = new Map();
      this._retry = null;
      this._pingTimer = null;
      this._pingId = 0;
      this._pings = new Map();      // ping_id -> sent at (ms)
      this._offsets = [];
    }

    /** Listen for a message type, "message" (all), "state" or "close"; returns an unsubscribe. */
    on(type, cb) {
      if (!this._listeners.has(type)) this._listeners.set(type, new Set());
      this._listeners.get(type).add(cb);
      return () => { this._listeners.get(type).delete(cb); };
    }

    _emit(type, arg) {
      const set = this._listeners.get(type);
      if (!set) return;
      for (const cb of Array.from(set)) {
        try { cb(arg); } catch (e) { this._log("listener for " + type + " failed: " + e.message); }
      }
    }

    _setState(s) {
      if (this.state === s) return;
      this.state = s;
      this._emit("state", s);
    }

    /** Start connecting (again, also after a no-reconnect close). */
    open() {
      this.stopped = false;
      if (this._retry !== null) { this._clearTimeout(this._retry); this._retry = null; }
      if (this.ws !== null) return;
      this._connect();
    }

    _connect() {
      this._setState("connecting");
      let sock;
      try {
        sock = new this._WS(this.url);
      } catch (e) {
        this._log("could not open " + this.url + ": " + e.message);
        this._closed(sock, null, e.message);
        return;
      }
      this.ws = sock;
      sock.onopen = () => {
        if (this.ws !== sock) return;
        this.attempt = 0;
        this.welcome = null;
        this._setState("open");
        this._sendHello();
      };
      sock.onmessage = (ev) => {
        if (this.ws !== sock) return;
        this._onMessage(ev.data);
      };
      sock.onclose = (ev) => {
        if (this.ws !== sock) return;
        this._closed(sock, ev && finite(ev.code) ? ev.code : null, ev && ev.reason ? String(ev.reason) : "");
      };
      sock.onerror = () => {};   // onclose follows and handles it
    }

    _sendHello() {
      const fields = typeof this._hello === "function" ? this._hello() : this._hello;
      const msg = Object.assign({}, fields || {});
      const client = msg.client !== null && typeof msg.client === "object" ? msg.client : {};
      msg.client = Object.assign({}, client, { instance: this.instance });
      msg.type = "hello";
      msg.protocol = PROTOCOL;
      msg.token = this._token;
      this.send(msg);
    }

    _onMessage(data) {
      const got = parseMessage(data);
      if (got.error) {
        this._log("ignored a server frame: " + got.error);
        return;
      }
      const msg = got.msg;
      if (msg.type === "welcome") {
        this.welcome = msg;
        this._startPings();
      } else if (msg.type === "pong") {
        this._onPong(msg);
      }
      this._emit("message", msg);
      this._emit(msg.type, msg);
    }

    _startPings() {
      this._stopPings();
      this.ping();
      this._pingTimer = this._setInterval(() => this.ping(), this.pingEveryMs);
    }

    _stopPings() {
      if (this._pingTimer !== null) { this._clearInterval(this._pingTimer); this._pingTimer = null; }
      this._pings.clear();
    }

    /** Send one ping now; false when the socket is not open. */
    ping() {
      const id = ++this._pingId;
      const sentAt = this._now();
      if (!this.send({ type: "ping", ping_id: id, t_client: sentAt / 1000 })) return false;
      this._pings.set(id, sentAt);
      while (this._pings.size > OFFSET_SAMPLES) this._pings.delete(this._pings.keys().next().value);
      return true;
    }

    _onPong(msg) {
      const sentAt = this._pings.get(msg.ping_id);
      if (sentAt === undefined || !finite(msg.t_server)) return;
      this._pings.delete(msg.ping_id);
      const rtt = Math.max(0, this._now() - sentAt);
      this.rttMs = Math.round(rtt);
      this._offsets.push(msg.t_server - (sentAt + rtt / 2) / 1000);
      if (this._offsets.length > OFFSET_SAMPLES) this._offsets.shift();
      this.offset = median(this._offsets);
    }

    _closed(sock, code, reason) {
      if (sock !== undefined && this.ws !== sock) return;
      this.ws = null;
      this.welcome = null;
      this.closeCode = code;
      this._stopPings();
      const reconnect = !this.stopped && NO_RECONNECT_CODES.indexOf(code) < 0;
      if (!reconnect) this.stopped = true;
      this._setState("closed");
      this._emit("close", { code, reason, reconnect });
      if (reconnect) this._scheduleReconnect();
    }

    /** Delay before the next attempt: 1, 2, 4, then 5 s. */
    nextDelay() {
      return this.backoffMs[Math.min(this.attempt, this.backoffMs.length - 1)];
    }

    _scheduleReconnect() {
      if (this._retry !== null || this.stopped) return;
      const wait = this.nextDelay();
      this.attempt++;
      this._retry = this._setTimeout(() => {
        this._retry = null;
        if (!this.stopped && this.ws === null) this._connect();
      }, wait);
    }

    /** Send one message; false (and nothing sent) when the socket is not open. */
    send(msg) {
      const sock = this.ws;
      if (sock === null || this.state !== "open" || sock.readyState !== WS_OPEN) return false;
      try {
        sock.send(JSON.stringify(msg));
      } catch (e) {
        this._log("send of " + (msg && msg.type) + " failed: " + e.message);
        return false;
      }
      return true;
    }

    /** Close now and open a fresh connection at once, with a new hello. */
    reconnect() {
      this.stopped = false;
      if (this._retry !== null) { this._clearTimeout(this._retry); this._retry = null; }
      this.attempt = 0;
      const sock = this.ws;
      if (sock !== null) {
        this.ws = null;
        this._stopPings();
        try { sock.close(1000, "reconnect"); } catch (e) { /* already closing */ }
        this._setState("closed");
        this._emit("close", { code: 1000, reason: "reconnect", reconnect: true });
      }
      this._connect();
    }

    /** Leave for good: send bye (unless opts.bye is false), close, and stop reconnecting. */
    close(opts) {
      const bye = !opts || opts.bye !== false;
      this.stopped = true;
      if (this._retry !== null) { this._clearTimeout(this._retry); this._retry = null; }
      const sock = this.ws;
      if (sock === null) { this._setState("closed"); return; }
      if (bye) this.send({ type: "bye" });
      this.ws = null;
      this.welcome = null;
      this._stopPings();
      try { sock.close(1000, "bye"); } catch (e) { /* already closing */ }
      this._setState("closed");
      this._emit("close", { code: 1000, reason: "bye", reconnect: false });
    }
  }

  const api = Object.freeze({
    PROTOCOL, WARMUP_FRAMES, REFRESH_FRAMES, STILL_LUM, FRAMES_EVERY_MS, BACKOFF_MS,
    PING_EVERY_MS, OFFSET_SAMPLES, NO_RECONNECT_CODES, CONN_STATES,
    median, RefreshMeter, frameIndex, isDropped, rectToPx, parseMessage, newInstanceId,
    FlickerState, Connection, flicker: F,
  });
  root.BrainSyncClient = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
