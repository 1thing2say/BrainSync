/*
 * BrainScroll content script: the Instagram glue between the overlay, the Reels feed and
 * the service worker.
 *
 * Loaded on https://www.instagram.com/* after flicker.js, cvep.js, scroller.js and
 * overlay.js. The
 * overlay starts once the page is on /reels/ or /reel/<id> and stays until Escape or a
 * reload. Instagram is a single-page app, so the path is checked once a second: while it
 * is anywhere else (Home, Messages, a profile) the squares are hidden and scrolls are
 * refused, so the decoder never scrolls the home feed or a chat by mistake.
 *
 * Transport: a runtime port named "brainscroll" to background.js, which opens this tab's
 * own WebSocket to the BrainSync bridge. The port is reopened after 1, 2, 4, then every
 * 5 s when it drops (for example when Chrome stops the service worker); the worker then
 * reports the socket state again and the overlay re-sends its hello on "open".
 *
 * Scroller: BrainScrollScroller.scroll (shared with the mock page). If no other reel is
 * showing within 1.5 s (same URL, same most visible video), and the feed is not at an end
 * (the first reel, for up), the worker presses ArrowUp / ArrowDown as a trusted key
 * through chrome.debugger and the ack says method "debugger"; if even that cannot be
 * sent, ok false and method "none".
 *
 * Toggler (the c-VEP middle square): BrainScrollScroller.togglePlay pauses or plays the
 * most visible reel video; no debugger fallback.
 */
(function () {
  "use strict";

  if (globalThis.__brainScrollLoaded) return;
  globalThis.__brainScrollLoaded = true;

  const S = globalThis.BrainScrollScroller;
  const REELS_PATH = /^\/reels?(\/|$)/;
  const PORT_RETRY_MIN_MS = 1000;
  const PORT_RETRY_MAX_MS = 5000;
  const KEY_TIMEOUT_MS = 3000;

  function alive() {
    try { return Boolean(chrome.runtime && chrome.runtime.id); } catch (e) { return false; }
  }

  function portTransport() {
    const msgCbs = [];
    const stateCbs = [];
    const pending = new Map();
    let port = null;
    let retry = PORT_RETRY_MIN_MS;
    let nextReq = 1;

    function emitState(st) {
      for (const cb of stateCbs) cb(st);
    }

    function connect() {
      if (!alive()) {
        // The extension was reloaded or removed: this page's script is orphaned.
        emitState("closed");
        return;
      }
      try {
        port = chrome.runtime.connect({ name: "brainscroll" });
      } catch (e) {
        console.warn("[BrainScroll] cannot reach the extension:", e.message);
        port = null;
        emitState("closed");
        return;
      }
      port.onMessage.addListener((m) => {
        if (m && m.type === "_state") {
          retry = PORT_RETRY_MIN_MS;
          emitState(m.state);
        } else if (m && m.type === "_trusted-key-result") {
          const done = pending.get(m.reqId);
          if (done) done(m.ok === true);
        } else {
          for (const cb of msgCbs) cb(m);
        }
      });
      port.onDisconnect.addListener(() => {
        void chrome.runtime.lastError;
        port = null;
        emitState("closed");
        for (const done of pending.values()) done(false);
        const wait = retry;
        retry = Math.min(retry * 2, PORT_RETRY_MAX_MS);
        setTimeout(connect, wait);
      });
    }

    function post(obj) {
      if (port === null) throw new Error("no port to the extension");
      port.postMessage(obj);
    }

    /** Ask the worker for a trusted key press; resolves true when it was dispatched. */
    function trustedKey(key) {
      return new Promise((resolve) => {
        const reqId = nextReq++;
        const done = (ok) => {
          if (!pending.has(reqId)) return;
          pending.delete(reqId);
          clearTimeout(timer);
          resolve(ok);
        };
        const timer = setTimeout(() => done(false), KEY_TIMEOUT_MS);
        pending.set(reqId, done);
        try { post({ type: "_trusted-key", key, reqId }); } catch (e) { done(false); }
      });
    }

    return {
      send: post,
      onMessage: (cb) => msgCbs.push(cb),
      onState: (cb) => stateCbs.push(cb),
      connect,
      trustedKey,
    };
  }

  function makeScroller(transport) {
    return async function scrollReels(dir) {
      const r = await S.scroll(dir);
      if (r.ok || r.edge) return { ok: r.ok, method: r.method };
      const target = S.find();
      const before = S.snapshot(target);
      const sent = await transport.trustedKey(dir === "up" ? "ArrowUp" : "ArrowDown");
      if (!sent) return { ok: false, method: "none" };
      return { ok: await S.waitMoved(before, target, undefined, undefined, S.VERIFY_MS),
               method: "debugger" };
    };
  }

  let overlay = null;
  function check() {
    const onReels = REELS_PATH.test(location.pathname);
    if (overlay !== null) {
      overlay.controller.setPaused(!onReels);
      return;
    }
    if (!onReels) return;
    const transport = portTransport();
    overlay = globalThis.BrainScrollOverlay.create({ transport, scroller: makeScroller(transport),
                                                     toggler: () => S.togglePlay(),
                                                     agent: "extension" });
    transport.connect();
  }

  check();
  const watch = setInterval(() => {
    // An orphaned script (extension reloaded) keeps pausing on navigation but starts nothing.
    if (overlay === null && !alive()) { clearInterval(watch); return; }
    check();
  }, 1000);
})();
