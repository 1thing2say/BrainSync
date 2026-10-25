/*
 * BrainPoint content script: the wiring between the page, the overlay and the service worker.
 *
 * Loaded in the top frame of every page after flicker.js, brainsync-client.js, hints.js,
 * clicker.js and point_overlay.js, and idle until asked: clicking the BrainPoint toolbar icon
 * (or Alt+Shift+B) sends {type: "_toggle"} to this tab, which mounts the overlay with the
 * photosensitivity warning, or stops BrainPoint when it runs. After Start the overlay
 * measures the display refresh (about two seconds), and then this script opens a runtime port
 * named "brainpoint" to background.js and sends {type: "_hello", fields}; the worker opens
 * this tab's WebSocket to the server and sends the hello. When this tab navigates, the new
 * page asks the worker ({type: "_query"}) whether BrainPoint runs in this tab, and if so it
 * starts again at once, without the warning and with the refresh measured before.
 *
 * On `scan_request` (PROTOCOL-v3 section 3): wait settle_ms, and while the tab is hidden or
 * the user paused (Esc); hide the overlay and let two frames pass; collect the hints and the
 * password rects (hints.js); ask the worker for a screenshot of the visible tab ("_capture");
 * show the overlay again; scale the screenshot so its long edge is at most 1280 px; black out
 * the password rects; encode it as JPEG at quality 0.8; send `scan` with the hints, the
 * viewport, the url (scheme, host and path only: no query, no fragment) and the title. A newer
 * scan_request makes an older one drop its result. A capture that fails is tried once more
 * 600 ms later; if that fails too, nothing is sent and the server asks again.
 *
 * Esc pressed in a sub-frame reaches the worker through esc.js, and the worker sends
 * {type: "_esc"} here; it pauses the session as Esc in this frame does.
 *
 * On `click`: refuse while the user paused or the tab is hidden; otherwise hide the overlay,
 * click at the point (clicker.js), show the overlay again, and answer `clicked`. The click is
 * for what the last screenshot showed, taken seconds earlier, so it is refused (`ok: false`,
 * and the server scans again) when the page changed since: no scan of this page was sent, the
 * address changed (an in-page route), the page scrolled by more than SCROLL_SLACK_PX, or the
 * window was resized. When the element is one of the page's hints (`hint_id`), the element
 * under the point must also be that hint's element or inside it, so a banner or late content
 * that moved something else under the point (a "Delete" where "Next" was) is never clicked.
 *
 * The port is reopened after 1, 2, 4, then every 5 s when it drops (Chrome stopped the
 * worker), with a fresh hello. The pure parts (scan sizing, mask mapping, data URLs, the
 * scan/click agent and the port transport) are exported for clients/chrome/test.
 */
(function (root) {
  "use strict";

  const H = root.BrainPointHints || (typeof require === "function" ? require("./hints.js") : null);
  const K = root.BrainPointClicker || (typeof require === "function" ? require("./clicker.js") : null);
  const O = root.BrainPointOverlay || (typeof require === "function" ? require("./point_overlay.js") : null);

  const PORT_NAME = "brainpoint";
  const MAX_EDGE = 1280;               // px, the long edge of the scan image
  const JPEG_QUALITY = 0.8;
  const MASK_PAD = 2;                  // CSS px around a password field
  const SETTLE_MS = 800;               // when scan_request has no usable settle_ms
  const MAX_SETTLE_MS = 10000;
  const WAIT_POLL_MS = 500;            // re-check a hidden or paused tab
  const CAPTURE_RETRY_MS = 600;        // captureVisibleTab allows about two calls a second
  const CAPTURE_TIMEOUT_MS = 5000;
  const FRAMES_WAIT_MS = 300;          // cap on waiting for two frames with the overlay hidden
  const PORT_RETRY_MS = Object.freeze([1000, 2000, 4000, 5000]);
  const SCROLL_SLACK_PX = 2;           // scroll since the screenshot that still counts as none

  function finite(v) {
    return typeof v === "number" && Number.isFinite(v);
  }

  function note(text) {
    if (root.console) root.console.warn("[BrainPoint] " + text);
  }

  // ---------------------------------------------------------------- pure helpers

  /** Size of the scan image for an iw x ih screenshot: long edge at most maxEdge, aspect kept. */
  function scanSize(iw, ih, maxEdge) {
    const edge = maxEdge === undefined ? MAX_EDGE : maxEdge;
    if (!finite(iw) || !finite(ih) || iw <= 0 || ih <= 0) {
      throw new RangeError("screenshot size must be positive, got " + iw + " x " + ih);
    }
    if (!finite(edge) || edge < 1) throw new RangeError("maxEdge must be at least 1 px, got " + edge);
    const scale = Math.min(1, edge / Math.max(iw, ih));
    const w = Math.max(1, Math.min(edge, Math.round(iw * scale)));
    const h = Math.max(1, Math.min(edge, Math.round(ih * scale)));
    return { w, h, scale };
  }

  /**
   * Password rects (viewport CSS px) mapped onto a w x h image of a vw x vh viewport, grown
   * by `pad` CSS px and rounded outward, so every pixel of the field is covered.
   */
  function masksToImage(masks, vw, vh, w, h, pad) {
    if (pad === undefined) pad = MASK_PAD;
    if (!(vw > 0) || !(vh > 0)) return [];
    const sx = w / vw, sy = h / vh;
    const out = [];
    for (const m of masks || []) {
      if (!m || !finite(m.x) || !finite(m.y) || !finite(m.w) || !finite(m.h)) continue;
      const x0 = Math.max(0, Math.floor((m.x - pad) * sx));
      const y0 = Math.max(0, Math.floor((m.y - pad) * sy));
      const x1 = Math.min(w, Math.ceil((m.x + m.w + pad) * sx));
      const y1 = Math.min(h, Math.ceil((m.y + m.h + pad) * sy));
      if (x1 > x0 && y1 > y0) out.push({ x: x0, y: y0, w: x1 - x0, h: y1 - y0 });
    }
    return out;
  }

  /** {media_type, data} of a JPEG or PNG data URL; Error for anything else. */
  function splitDataUrl(url) {
    const m = /^data:(image\/(?:jpeg|png));base64,([A-Za-z0-9+/]+={0,2})$/.exec(typeof url === "string" ? url : "");
    if (!m) throw new Error("the screenshot is not a base64 JPEG or PNG data URL");
    return { media_type: m[1], data: m[2] };
  }

  /** The page address without query or fragment, which can hold tokens. */
  function pageUrl(loc) {
    if (!loc) return "";
    const origin = loc.origin && loc.origin !== "null" ? loc.origin : (loc.protocol || "") + "//" + (loc.host || "");
    return origin + (loc.pathname || "");
  }

  function viewportOf(win) {
    return { w: win.innerWidth, h: win.innerHeight, dpr: win.devicePixelRatio || 1 };
  }

  /** What a click must find unchanged since the screenshot: address, scroll and viewport. */
  function pageState(win) {
    const loc = win.location || {};
    return { href: String(loc.href || ""), sx: Number(win.scrollX) || 0, sy: Number(win.scrollY) || 0,
             vw: win.innerWidth, vh: win.innerHeight };
  }

  /** Why the page at `now` is no longer the one of `then` (pageState values), or null. */
  function pageChange(then, now) {
    if (!then) return "no screenshot of this page was sent";
    if (then.href !== now.href) return "the page address changed";
    if (Math.abs(then.sx - now.sx) > SCROLL_SLACK_PX || Math.abs(then.sy - now.sy) > SCROLL_SLACK_PX) {
      return "the page scrolled";
    }
    if (then.vw !== now.vw || then.vh !== now.vh) return "the window was resized";
    return null;
  }

  /** The scan message (PROTOCOL-v3 section 2). */
  function buildScan(scanId, image, viewport, url, title, hints) {
    return {
      type: "scan", scan_id: scanId,
      image: { media_type: image.media_type, data: image.data, w: image.w, h: image.h },
      viewport: { w: viewport.w, h: viewport.h, dpr: viewport.dpr },
      url: String(url || "").slice(0, 2048), title: String(title || "").slice(0, 200),
      hints: (hints || []).slice(0, H ? H.MAX_HINTS : 200),
    };
  }

  // ------------------------------------------------------------------ the agent

  /**
   * Answers scan_request with scan and click with clicked. deps: send(msg) -> bool,
   * sleep(ms) -> Promise, blocked() -> reason string or null (paused, hidden), collect() ->
   * {hints, masks, viewport}, hide(bool), afterFrames() -> Promise, capture() -> Promise of a
   * data URL, encode(dataUrl, masks, viewport) -> Promise of {media_type, data, w, h},
   * clickAt(x, y, expect) -> {ok, method, detail} (expect: the hint's element or null),
   * page() -> pageState, url(), title(), activity(text), clicked(result), note(text).
   */
  function createAgent(deps) {
    const noop = () => {};
    const activity = deps.activity || noop;
    const warn = deps.note || noop;
    let latest = null;          // scan_id of the newest scan_request
    let stopped = false;
    let scanned = null;         // {page, elements} of the last scan sent
    const pageNow = deps.page || (() => null);

    const current = (id) => !stopped && latest === id;

    async function captureOnce() {
      try {
        return await deps.capture();
      } catch (e) {
        warn("screenshot failed (" + (e && e.message) + "); trying once more");
        await deps.sleep(CAPTURE_RETRY_MS);
        return deps.capture();
      }
    }

    /** One scan: resolves to the scan message sent, or null when dropped. */
    async function scan(req) {
      const id = req.scan_id;
      latest = id;
      const settle = finite(req.settle_ms) ? Math.max(0, Math.min(MAX_SETTLE_MS, req.settle_ms)) : SETTLE_MS;
      activity("Waiting for the page to settle…");
      await deps.sleep(settle);
      while (current(id) && deps.blocked() !== null) await deps.sleep(WAIT_POLL_MS);
      if (!current(id)) return null;
      activity("Taking a screenshot…");
      let got, dataUrl, page;
      deps.hide(true);
      try {
        await Promise.race([deps.afterFrames(), deps.sleep(FRAMES_WAIT_MS)]);
        got = deps.collect();
        page = pageNow();
        dataUrl = await captureOnce();
      } catch (e) {
        warn("no screenshot for scan " + id + ": " + (e && e.message) + "; the server will ask again");
        activity("");
        return null;
      } finally {
        deps.hide(false);
      }
      if (!current(id)) return null;
      let image;
      try {
        image = await deps.encode(dataUrl, got.masks, got.viewport);
      } catch (e) {
        warn("could not encode the screenshot for scan " + id + ": " + (e && e.message));
        activity("");
        return null;
      }
      if (!current(id)) return null;
      const msg = buildScan(id, image, got.viewport, deps.url(), deps.title(), got.hints);
      if (!deps.send(msg)) {
        warn("scan " + id + " could not be sent: the server connection is down");
        activity("");
        return null;
      }
      scanned = { page, elements: Array.isArray(got.elements) ? got.elements : [] };
      activity("Finding the buttons…");
      return msg;
    }

    function click(msg) {
      let result;
      const why = deps.blocked();
      const changed = why === null ? stale(msg) : null;
      if (why !== null) {
        result = { ok: false, method: "none", detail: "not clicked: " + why };
      } else if (changed !== null) {
        result = { ok: false, method: "none", detail: "not clicked: " + changed + " since the screenshot" };
      } else {
        activity("Clicking " + JSON.stringify(msg.element && msg.element.label ? String(msg.element.label) : "") + "…");
        deps.hide(true);
        try {
          result = deps.clickAt(msg.x, msg.y, expected(msg));
        } catch (e) {
          result = { ok: false, method: "none", detail: "click failed: " + (e && e.message) };
        } finally {
          deps.hide(false);
        }
      }
      result = { ok: result && result.ok === true,
                 method: result && typeof result.method === "string" ? result.method : "none",
                 detail: result && typeof result.detail === "string" ? result.detail.slice(0, 500) : "" };
      const reply = { type: "clicked", click_id: msg.click_id, ok: result.ok, method: result.method,
                      detail: result.detail };
      if (deps.clicked) deps.clicked(result);
      activity("");
      if (!deps.send(reply)) warn("clicked " + msg.click_id + " could not be sent: the server connection is down");
      return reply;
    }

    /** Why the page is no longer the one the click was chosen on, or null. */
    function stale(msg) {
      if (!deps.page) return null;
      return pageChange(scanned && scanned.page, pageNow());
    }

    /** The element of the click's hint, or null when it names none of the last scan's hints. */
    function expected(msg) {
      const hid = msg.element ? msg.element.hint_id : null;
      if (!scanned || !Number.isInteger(hid) || hid < 0) return null;
      return scanned.elements[hid] || null;
    }

    /** Take one server message; returns a Promise (scan), the reply (click) or null. */
    function handle(msg) {
      if (stopped || !msg || typeof msg !== "object") return null;
      if (msg.type === "scan_request") {
        if (!Number.isInteger(msg.scan_id) || msg.scan_id < 0) {
          warn("ignored scan_request without a valid scan_id");
          return null;
        }
        return scan(msg);
      }
      if (msg.type === "click") {
        if (!Number.isInteger(msg.click_id) || msg.click_id < 0) {
          warn("ignored click without a valid click_id");
          return null;
        }
        return click(msg);
      }
      return null;
    }

    function stop() {
      stopped = true;
      latest = null;
      scanned = null;
    }

    return { handle, stop, latest: () => latest };
  }

  // ------------------------------------------------------------- port transport

  /**
   * The runtime port to background.js. opts: chrome, timers ({setTimeout, clearTimeout}),
   * onMessage(msg), onState(s), onClose(info), onRtt(ms), onError(text), onReconnect().
   */
  function portTransport(opts) {
    const chromeApi = opts.chrome;
    const timers = opts.timers || root;
    const noop = () => {};
    const onMessage = opts.onMessage || noop, onState = opts.onState || noop;
    const onClose = opts.onClose || noop, onRtt = opts.onRtt || noop;
    const onError = opts.onError || noop, onReconnect = opts.onReconnect || noop;
    const pending = new Map();      // reqId -> {resolve, reject, timer}
    let port = null, attempt = 0, retry = null, stopped = false, nextReq = 1, opened = 0;

    function failPending(why) {
      for (const [id, p] of pending) {
        timers.clearTimeout(p.timer);
        pending.delete(id);
        p.reject(new Error(why));
      }
    }

    function route(m) {
      if (!m || typeof m !== "object" || typeof m.type !== "string") return;
      switch (m.type) {
        case "_state": attempt = 0; onState(m.state); return;
        case "_close": onClose({ code: m.code, reason: m.reason, reconnect: m.reconnect }); return;
        case "_rtt": onRtt(m.rtt_ms); return;
        case "_error": onError(String(m.text || "")); return;
        case "_capture-result": {
          const p = pending.get(m.reqId);
          if (!p) return;
          pending.delete(m.reqId);
          timers.clearTimeout(p.timer);
          if (typeof m.dataUrl === "string") p.resolve(m.dataUrl);
          else p.reject(new Error(m.error || "no screenshot"));
          return;
        }
        default:
          if (m.type.startsWith("_")) return;
          onMessage(m);
      }
    }

    function alive() {
      try { return Boolean(chromeApi.runtime && chromeApi.runtime.id); } catch (e) { return false; }
    }

    function connect() {
      if (stopped || port !== null) return;
      if (!alive()) { onState("closed"); onError("the extension was reloaded; reload this page"); return; }
      try {
        port = chromeApi.runtime.connect({ name: PORT_NAME });
      } catch (e) {
        port = null;
        onState("closed");
        schedule();
        return;
      }
      const mine = port;
      mine.onMessage.addListener((m) => { if (port === mine) route(m); });
      mine.onDisconnect.addListener(() => {
        void (chromeApi.runtime && chromeApi.runtime.lastError);
        if (port !== mine) return;
        port = null;
        failPending("the service worker went away");
        onState("closed");
        schedule();
      });
      if (opened++ > 0) onReconnect();
    }

    function schedule() {
      if (stopped || retry !== null) return;
      const wait = PORT_RETRY_MS[Math.min(attempt, PORT_RETRY_MS.length - 1)];
      attempt++;
      retry = timers.setTimeout(() => { retry = null; connect(); }, wait);
    }

    function send(obj) {
      if (port === null) return false;
      try { port.postMessage(obj); return true; } catch (e) { return false; }
    }

    /** A screenshot of this tab as a data URL (rejects after CAPTURE_TIMEOUT_MS). */
    function capture() {
      return new Promise((resolve, reject) => {
        const reqId = nextReq++;
        const timer = timers.setTimeout(() => {
          if (pending.delete(reqId)) reject(new Error("the screenshot took too long"));
        }, CAPTURE_TIMEOUT_MS);
        pending.set(reqId, { resolve, reject, timer });
        if (!send({ type: "_capture", reqId })) {
          pending.delete(reqId);
          timers.clearTimeout(timer);
          reject(new Error("no connection to the service worker"));
        }
      });
    }

    function close() {
      stopped = true;
      if (retry !== null) { timers.clearTimeout(retry); retry = null; }
      failPending("BrainPoint stopped");
      const p = port;
      port = null;
      if (p) { try { p.disconnect(); } catch (e) { /* already gone */ } }
    }

    return { connect, send, capture, close, connected: () => port !== null };
  }

  // ------------------------------------------------------------- the browser side

  function base64ToBytes(b64) {
    const bin = root.atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  /** Scale, mask and JPEG-encode a screenshot data URL in this page (no network, no CSP). */
  async function encodeJpeg(doc, dataUrl, masks, viewport) {
    const src = splitDataUrl(dataUrl);
    const bmp = await root.createImageBitmap(new root.Blob([base64ToBytes(src.data)], { type: src.media_type }));
    try {
      const size = scanSize(bmp.width, bmp.height);
      const canvas = doc.createElement("canvas");
      canvas.width = size.w;
      canvas.height = size.h;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(bmp, 0, 0, size.w, size.h);
      ctx.fillStyle = "#000";
      for (const m of masksToImage(masks, viewport.w, viewport.h, size.w, size.h)) ctx.fillRect(m.x, m.y, m.w, m.h);
      const out = splitDataUrl(canvas.toDataURL("image/jpeg", JPEG_QUALITY));
      return { media_type: out.media_type, data: out.data, w: size.w, h: size.h };
    } finally {
      if (typeof bmp.close === "function") bmp.close();
    }
  }

  /** One BrainPoint session in this tab: overlay, port, agent. Returns {quit, model, view}. */
  function startSession(win, doc, chromeApi, opts) {
    let view = null, agent = null, ended = false;
    const model = O.createModel({ send: (m) => transport.send(m) });
    const transport = portTransport({
      chrome: chromeApi, timers: win,
      onMessage: (m) => {
        model.onMessage(m);
        if (m.type === "scan_result" || (m.type === "layout" && Array.isArray(m.targets) && m.targets.length)) {
          model.state.activity = "";
        }
        const done = agent ? agent.handle(m) : null;
        if (done && typeof done.then === "function") {
          done.catch((e) => { model.state.note = "scan failed: " + (e && e.message); note(model.state.note); });
        }
        if (view) view.update();
      },
      onState: (st) => { model.onState(st); if (view) view.update(); },
      onClose: (info) => { model.onClose(info); if (view) view.update(); },
      onRtt: (ms) => { model.state.rttMs = finite(ms) ? ms : null; },
      onError: (text) => { model.state.note = text; note(text); if (view) view.update(); },
      // The worker restarted: it lost the socket. Join again, replacing our own stale
      // controller if the server has not seen it go yet.
      onReconnect: () => { if (!model.state.ended) hello(false, true); },
    });

    function hello(reconnect, takeover) {
      const fields = O.helloFields(model.state.refreshHz, viewportOf(win), takeover);
      return transport.send({ type: "_hello", fields, reconnect: Boolean(reconnect) });
    }

    function quit() {
      if (ended) return;
      ended = true;
      model.quit();
      if (agent) agent.stop();
      transport.send({ type: "_bye" });
      transport.close();
      if (view) view.destroy();
      if (opts.onEnd) opts.onEnd();
    }

    view = O.create({
      win, doc, model, resumed: Boolean(opts.resumed),
      onQuit: quit,
      onTakeover: (action) => { model.state.ended = null; hello(true, action === "takeover"); view.update(); },
      onMeasured: (action) => {
        if (action === "connect") {
          transport.connect();
          // A tab that ran BrainPoint before this page loaded replaces its own old socket.
          hello(false, Boolean(opts.resumed));
        } else if (action === "reconnect") {
          hello(true, false);
        }
      },
    });
    agent = createAgent({
      send: (m) => transport.send(m),
      sleep: (ms) => new Promise((resolve) => win.setTimeout(resolve, ms)),
      blocked: () => (model.state.paused ? "paused by the user (Esc)"
        : doc.visibilityState === "hidden" ? "the tab is hidden" : null),
      collect: () => H.collect(doc, win, { exclude: view.host }),
      hide: (hidden) => view.setHidden(hidden),
      afterFrames: () => view.afterFrames(2),
      capture: () => transport.capture(),
      encode: (dataUrl, masks, viewport) => encodeJpeg(doc, dataUrl, masks, viewport),
      clickAt: (x, y, expect) => K.clickAt(x, y, { win, doc, isOurs: view.isOurs, expect }),
      page: () => pageState(win),
      url: () => pageUrl(win.location),
      title: () => doc.title,
      activity: (text) => { model.state.activity = text; view.update(); },
      clicked: (r) => { model.state.lastClick = Object.assign({}, model.state.lastClick || { label: "" }, r); },
      note: (text) => { model.state.note = text; note(text); },
    });
    if (opts.resumed && finite(opts.refreshHz)) {
      const action = model.measured(opts.refreshHz);
      if (action === "connect") {
        transport.connect();
        hello(false, true);
      }
    }
    return { quit, model, view, transport, agent };
  }

  /** Listen for the toolbar toggle and resume a session this tab was running. */
  function boot(win, doc, chromeApi) {
    if (win.__brainPointLoaded) return null;
    win.__brainPointLoaded = true;
    let session = null;
    const start = (resumed, refreshHz) => {
      session = startSession(win, doc, chromeApi, {
        resumed, refreshHz, onEnd: () => { session = null; },
      });
      return session;
    };
    chromeApi.runtime.onMessage.addListener((msg, sender, reply) => {
      if (msg && msg.type === "_esc") {           // Esc in a sub-frame, forwarded by the worker
        if (session && session.view) session.view.escape();
        return false;
      }
      if (!msg || msg.type !== "_toggle") return false;
      if (session) session.quit();
      else start(false, null);
      reply({ active: session !== null });
      return false;
    });
    try {
      chromeApi.runtime.sendMessage({ type: "_query" }, (r) => {
        void chromeApi.runtime.lastError;
        if (r && r.active === true && session === null) start(true, r.refresh_hz);
      });
    } catch (e) {
      /* the extension was reloaded: this page's script is orphaned */
    }
    return { session: () => session };
  }

  const api = Object.freeze({
    PORT_NAME, MAX_EDGE, JPEG_QUALITY, MASK_PAD, SETTLE_MS, CAPTURE_RETRY_MS, CAPTURE_TIMEOUT_MS,
    PORT_RETRY_MS, SCROLL_SLACK_PX, scanSize, masksToImage, splitDataUrl, pageUrl, viewportOf, buildScan,
    pageState, pageChange,
    createAgent, portTransport, encodeJpeg, startSession, boot,
  });
  root.BrainPointContent = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else if (root.chrome && root.chrome.runtime && root.document) {
    boot(root, root.document, root.chrome);
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
