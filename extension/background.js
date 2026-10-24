/*
 * BrainScroll service worker: one WebSocket to the BrainSync bridge per Instagram tab.
 *
 *     chrome.storage.local.set({wsUrl: "ws://127.0.0.1:8765"})   // the default
 *
 * Each content script connects a runtime port named "brainscroll", and the worker opens a
 * socket to wsUrl for that port alone, reconnecting after 1, 2, 4, then every 5 s while
 * the port lives. Server messages on a socket go to its port only; port messages go to
 * its socket, except internal ones (type starting with "_"). One socket per tab makes
 * every tab its own server connection: a tab that opens or reloads mid-run gets the
 * bridge's config and current phase, its hello is not mistaken for a repeat from another
 * tab, and its acks, frame reports and bye carry their origin. (The bridge still sends
 * every scroll to every connection; the overlay moves the feed only in a visible tab where
 * Start was pressed, and Python counts one ack per scroll id.) The socket state reaches
 * the port as {type: "_state", state: "connecting" | "open" | "closed"}: on each "open"
 * the overlay sends a fresh hello, and a bye that could not go out, so nothing is cached
 * here. Client messages sent while the socket is down are dropped with a note.
 *
 * {type: "_trusted-key", key: "ArrowUp" | "ArrowDown", reqId} asks for a trusted key
 * press in the sender's tab through chrome.debugger (attach, keyDown + keyUp, detach); the
 * reply is {type: "_trusted-key-result", reqId, ok}. Chrome shows its "started debugging
 * this browser" bar while attached, so this is only the fallback when scrolling the feed
 * container did nothing.
 *
 * MV3 lifetime: since Chrome 116 WebSocket traffic resets the worker's idle timer, and the
 * bridge sends a ping every 20 s, inside the 30 s idle limit. If the worker is stopped
 * anyway, the ports disconnect, the content scripts reconnect (which restarts the worker)
 * and each new socket gets the bridge's current config and phase.
 */
"use strict";

const DEFAULT_URL = "ws://127.0.0.1:8765";
const BACKOFF_MIN_MS = 1000;
const BACKOFF_MAX_MS = 5000;
const KEYS = {
  ArrowUp: { code: "ArrowUp", windowsVirtualKeyCode: 38 },
  ArrowDown: { code: "ArrowDown", windowsVirtualKeyCode: 40 },
};

const conns = new Map();   // port -> {ws, state, backoff, retryTimer}
let wsUrl = DEFAULT_URL;

function post(port, msg) {
  try { port.postMessage(msg); } catch (e) { drop(port); }
}

function setState(port, c, state) {
  c.state = state;
  post(port, { type: "_state", state });
}

async function loadUrl() {
  try {
    const got = await chrome.storage.local.get("wsUrl");
    wsUrl = typeof got.wsUrl === "string" && got.wsUrl ? got.wsUrl : DEFAULT_URL;
  } catch (e) {
    wsUrl = DEFAULT_URL;
  }
  return wsUrl;
}

function live(port, c) {
  return conns.get(port) === c;
}

function scheduleReconnect(port, c) {
  if (c.retryTimer !== null || !live(port, c)) return;
  const wait = c.backoff;
  c.backoff = Math.min(c.backoff * 2, BACKOFF_MAX_MS);
  c.retryTimer = setTimeout(() => { c.retryTimer = null; openSocket(port, c); }, wait);
}

async function openSocket(port, c) {
  if (c.ws !== null || !live(port, c)) return;
  const url = await loadUrl();
  if (c.ws !== null || !live(port, c)) return;   // another call won the race, or the tab went
  setState(port, c, "connecting");
  let sock;
  try {
    sock = new WebSocket(url);
  } catch (e) {
    console.warn("[BrainScroll] bad wsUrl", url, e.message);
    setState(port, c, "closed");
    scheduleReconnect(port, c);
    return;
  }
  c.ws = sock;
  sock.onopen = () => {
    if (c.ws !== sock) return;
    c.backoff = BACKOFF_MIN_MS;
    setState(port, c, "open");
  };
  sock.onmessage = (ev) => {
    if (c.ws !== sock) return;
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { msg = null; }
    if (msg === null || typeof msg !== "object" || Array.isArray(msg) || typeof msg.type !== "string") {
      console.warn("[BrainScroll] ignored malformed server message:", String(ev.data).slice(0, 80));
      return;
    }
    if (msg.type.startsWith("_")) {
      console.warn("[BrainScroll] ignored server message with internal type", msg.type);
      return;
    }
    post(port, msg);
  };
  sock.onclose = () => {
    if (c.ws !== sock) return;
    c.ws = null;
    setState(port, c, "closed");
    scheduleReconnect(port, c);
  };
  sock.onerror = () => {};   // onclose follows and handles it
}

function closeSocket(c, reason) {
  if (c.retryTimer !== null) { clearTimeout(c.retryTimer); c.retryTimer = null; }
  c.backoff = BACKOFF_MIN_MS;
  if (c.ws !== null) {
    const sock = c.ws;
    c.ws = null;
    try { sock.close(1000, reason); } catch (e) { /* already closing */ }
  }
  c.state = "closed";
}

function drop(port) {
  const c = conns.get(port);
  if (!c) return;
  conns.delete(port);
  closeSocket(c, "BrainScroll tab gone");
}

async function trustedKey(tabId, key) {
  const spec = KEYS[key];
  if (!spec || typeof tabId !== "number") return false;
  const target = { tabId };
  try {
    await chrome.debugger.attach(target, "1.3");
  } catch (e) {
    console.warn("[BrainScroll] debugger attach failed:", e.message);
    return false;
  }
  try {
    const base = { key, code: spec.code, windowsVirtualKeyCode: spec.windowsVirtualKeyCode,
                   nativeVirtualKeyCode: spec.windowsVirtualKeyCode };
    // rawKeyDown: a key with no text, as Puppeteer sends arrows.
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", { type: "rawKeyDown", ...base });
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", { type: "keyUp", ...base });
    return true;
  } catch (e) {
    console.warn("[BrainScroll] key dispatch failed:", e.message);
    return false;
  } finally {
    try { await chrome.debugger.detach(target); } catch (e) { /* tab gone or already detached */ }
  }
}

function onPortMessage(port, msg) {
  if (msg === null || typeof msg !== "object" || typeof msg.type !== "string") {
    console.warn("[BrainScroll] ignored malformed port message");
    return;
  }
  if (msg.type === "_trusted-key") {
    const tabId = port.sender && port.sender.tab ? port.sender.tab.id : undefined;
    trustedKey(tabId, msg.key).then((ok) => {
      try { port.postMessage({ type: "_trusted-key-result", reqId: msg.reqId, ok }); } catch (e) { /* port gone */ }
    });
    return;
  }
  if (msg.type.startsWith("_")) {
    console.warn("[BrainScroll] ignored unknown internal message", msg.type);
    return;
  }
  const c = conns.get(port);
  if (c && c.ws !== null && c.ws.readyState === WebSocket.OPEN) {
    c.ws.send(JSON.stringify(msg));
  } else {
    console.warn("[BrainScroll] socket not open; dropped", msg.type);
  }
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "brainscroll") return;
  const c = { ws: null, state: "closed", backoff: BACKOFF_MIN_MS, retryTimer: null };
  conns.set(port, c);
  port.onMessage.addListener((msg) => onPortMessage(port, msg));
  port.onDisconnect.addListener(() => drop(port));
  post(port, { type: "_state", state: c.state });
  openSocket(port, c);
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.wsUrl) return;
  // Reconnect every tab to the new address.
  for (const [port, c] of conns) {
    closeSocket(c, "new wsUrl");
    setState(port, c, "closed");
    openSocket(port, c);
  }
});
