/*
 * BrainPoint service worker: one WebSocket to the BrainSync server per tab port, through the
 * SDK's Connection (brainsync-client.js), plus screenshots and the toolbar toggle.
 *
 *     chrome.storage.local.set({serverUrl: "ws://127.0.0.1:8765/ws", token: "..."})   // options page
 *
 * A content script that runs BrainPoint connects a runtime port named "brainpoint" and sends
 * {type: "_hello", fields}: the hello fields of PROTOCOL-v3 (role, mode "point", refresh_hz,
 * accepted, viewport, takeover). The worker then opens a Connection to serverUrl with the
 * token from storage; Connection adds type, protocol and the token to the hello, sends it on
 * every (re)connect, reconnects after 1, 2, 4, then every 5 s (never after close codes 4000,
 * 4001 or 4002), and pings every 10 s. takeover is used for one handshake only: after the
 * welcome, automatic reconnects send takeover false. {type: "_hello", fields, reconnect: true}
 * replaces the connection at once with a fresh one (a new hello, the token read again). Every
 * Connection of one port sends the same hello client.instance, so the server lets this tab
 * back in (closing its old socket) when that socket died on our side only, or when a
 * replacement's hello arrives before the old socket's close, instead of answering busy (4002),
 * which would stop the tab for good.
 *
 * To the port go every server message as is, {type: "_state", state} (connecting, open,
 * closed), {type: "_close", code, reason, reconnect}, {type: "_rtt", rtt_ms} after each pong
 * and {type: "_error", text} for a setting that cannot work. Port messages whose type does not
 * start with "_" go to the server; the internal ones are _hello, _capture and _bye. When the
 * port goes (the tab navigates or closes), its socket is closed.
 *
 * {type: "_capture", reqId} takes a screenshot of the sender's tab with
 * chrome.tabs.captureVisibleTab(windowId, {format: "jpeg", quality: 80}) and answers
 * {type: "_capture-result", reqId, dataUrl} or {type: "_capture-result", reqId, error}. The
 * tab must be the active tab of its window before and after the capture, so a screenshot of
 * another tab is never sent.
 *
 * {type: "_esc"} from a sub-frame's esc.js (Esc pressed while an iframe has the focus) sends
 * `pause` on that tab's connection and {type: "_esc"} to the tab's top frame.
 *
 * The toolbar icon (and its shortcut) sends {type: "_toggle"} to the tab's content script.
 * The worker remembers in chrome.storage.session which tab runs BrainPoint ("activeTab", with
 * its measured refresh) once the server welcomed it as the controller, so a page loaded in
 * that tab after a navigation asks ({type: "_query"}) and starts again without the warning.
 * Only one tab is active at a time; the record goes on _bye, on close codes 4000, 4001 and
 * 4002, and when the tab closes. The token stays in this worker and is never logged.
 *
 * MV3 lifetime: WebSocket traffic keeps the worker alive (Chrome 116+), and the server pings
 * every 20 s. If Chrome stops the worker anyway, the ports drop, the content scripts reconnect
 * and send their hello again.
 */
"use strict";

if (typeof importScripts === "function" && !globalThis.BrainSyncClient) {
  importScripts("flicker.js", "cvep.js", "brainsync-client.js");
}
const C = globalThis.BrainSyncClient;

const PORT_NAME = "brainpoint";
const DEFAULT_URL = "ws://127.0.0.1:8765/ws";
const ACTIVE_KEY = "activeTab";
const STOP_CODES = Object.freeze([4000, 4001, 4002]);

const conns = new Map();   // port -> {conn, fields, tabId, gen}

function warn(...args) {
  console.warn("[BrainPoint]", ...args);
}

function post(port, msg) {
  try { port.postMessage(msg); } catch (e) { drop(port); }
}

function live(port, c) {
  return conns.get(port) === c;
}

/** {url, token} from chrome.storage.local, with the defaults. */
async function loadSettings() {
  let got = {};
  try { got = await chrome.storage.local.get(["serverUrl", "token"]); } catch (e) { got = {}; }
  const url = typeof got.serverUrl === "string" && got.serverUrl.trim() ? got.serverUrl.trim() : DEFAULT_URL;
  const token = typeof got.token === "string" && got.token.trim() ? got.token.trim() : null;
  return { url, token };
}

async function getActive() {
  try {
    const got = await chrome.storage.session.get(ACTIVE_KEY);
    const a = got[ACTIVE_KEY];
    return a && typeof a === "object" && Number.isInteger(a.id) ? a : null;
  } catch (e) {
    return null;
  }
}

// Updates of the active-tab record run one after another, so a clear that read the old
// record cannot remove a newer one written in between.
let storageChain = Promise.resolve();
function serial(task) {
  storageChain = storageChain.then(task, task);
  return storageChain;
}

function setActive(tabId, refreshHz) {
  return serial(async () => {
    try {
      await chrome.storage.session.set({ [ACTIVE_KEY]: { id: tabId, refresh_hz: refreshHz } });
    } catch (e) { warn("could not remember the active tab:", e.message); }
  });
}

function clearActive(tabId) {
  return serial(async () => {
    const a = await getActive();
    if (a === null || a.id !== tabId) return;
    try { await chrome.storage.session.remove(ACTIVE_KEY); } catch (e) { /* nothing stored */ }
  });
}

function closeConn(c, bye) {
  const conn = c.conn;
  c.conn = null;
  if (conn) conn.close({ bye });
}

function drop(port) {
  const c = conns.get(port);
  if (!c) return;
  conns.delete(port);
  c.gen++;
  closeConn(c, false);
}

/** Open a fresh Connection for this port, closing the old one. */
function openConn(port, c, settings) {
  closeConn(c, false);
  let conn;
  try {
    conn = new C.Connection({
      url: settings.url, token: settings.token, hello: () => c.fields, instance: c.instance,
      log: (text) => warn(text),
    });
  } catch (e) {
    post(port, { type: "_error", text: e.message + ". Fix it in the BrainPoint options." });
    post(port, { type: "_state", state: "closed" });
    return;
  }
  c.conn = conn;
  const mine = () => c.conn === conn && live(port, c);
  conn.on("state", (state) => { if (mine()) post(port, { type: "_state", state }); });
  conn.on("close", (info) => {
    if (!mine()) return;
    post(port, { type: "_close", code: info.code, reason: info.reason, reconnect: info.reconnect });
    if (!info.reconnect && STOP_CODES.indexOf(info.code) >= 0) clearActive(c.tabId);
  });
  conn.on("message", (msg) => {
    if (!mine()) return;
    if (msg.type.startsWith("_")) {
      warn("ignored a server message with an internal type", msg.type);
      return;
    }
    if (msg.type === "welcome") {
      // takeover was for this handshake; automatic reconnects must not take over.
      c.fields = Object.assign({}, c.fields, { takeover: false });
      if (msg.role === "controller" && Number.isInteger(c.tabId)) setActive(c.tabId, c.fields.refresh_hz);
    }
    post(port, msg);
  });
  conn.on("pong", () => { if (mine()) post(port, { type: "_rtt", rtt_ms: conn.rttMs }); });
  conn.open();
}

async function onHello(port, c, msg) {
  if (msg.fields === null || typeof msg.fields !== "object" || Array.isArray(msg.fields)) {
    warn("ignored _hello without fields");
    return;
  }
  c.fields = Object.assign({}, msg.fields);
  if (c.conn !== null && msg.reconnect !== true) return;
  const gen = ++c.gen;
  const settings = await loadSettings();
  if (!live(port, c) || gen !== c.gen) return;   // the tab went, or a newer _hello won
  openConn(port, c, settings);
}

async function tabState(tabId) {
  try { return await chrome.tabs.get(tabId); } catch (e) { return null; }
}

async function capture(port, reqId) {
  const reply = (fields) => post(port, Object.assign({ type: "_capture-result", reqId }, fields));
  const tabId = port.sender && port.sender.tab ? port.sender.tab.id : undefined;
  if (!Number.isInteger(tabId)) return reply({ error: "no tab to capture" });
  const before = await tabState(tabId);
  if (!before || !before.active) return reply({ error: "the tab is not the visible tab of its window" });
  let dataUrl;
  try {
    dataUrl = await chrome.tabs.captureVisibleTab(before.windowId, { format: "jpeg", quality: 80 });
  } catch (e) {
    return reply({ error: "captureVisibleTab failed: " + e.message });
  }
  const after = await tabState(tabId);
  if (!after || !after.active || after.windowId !== before.windowId) {
    return reply({ error: "the tab stopped being visible during the screenshot" });
  }
  if (typeof dataUrl !== "string" || !dataUrl.startsWith("data:image/")) {
    return reply({ error: "captureVisibleTab returned no image" });
  }
  return reply({ dataUrl });
}

function onPortMessage(port, c, msg) {
  if (msg === null || typeof msg !== "object" || typeof msg.type !== "string") {
    warn("ignored a malformed port message");
    return;
  }
  switch (msg.type) {
    case "_hello": onHello(port, c, msg); return;
    case "_capture": capture(port, msg.reqId); return;
    case "_bye":
      c.gen++;
      closeConn(c, true);
      clearActive(c.tabId);
      return;
    default:
      break;
  }
  if (msg.type.startsWith("_")) {
    warn("ignored an unknown internal message", msg.type);
    return;
  }
  if (c.conn === null || !c.conn.send(msg)) warn("not connected to the server; dropped", msg.type);
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== PORT_NAME) return;
  if (port.sender && port.sender.frameId !== undefined && port.sender.frameId !== 0) {
    port.disconnect();   // only the top frame runs BrainPoint
    return;
  }
  const tabId = port.sender && port.sender.tab ? port.sender.tab.id : undefined;
  const c = { conn: null, fields: null, tabId, gen: 0, instance: C.newInstanceId() };
  conns.set(port, c);
  port.onMessage.addListener((msg) => onPortMessage(port, c, msg));
  port.onDisconnect.addListener(() => drop(port));
  post(port, { type: "_state", state: "closed" });
});

/**
 * Esc pressed in a sub-frame (esc.js): send pause on the tab's connection at once, and tell the
 * tab's top frame, whose overlay stops the flicker and shows Resume.
 */
function onEsc(sender) {
  const tabId = sender && sender.tab ? sender.tab.id : undefined;
  if (!Number.isInteger(tabId)) return;
  let running = false;
  for (const c of conns.values()) {
    if (c.tabId !== tabId || c.conn === null) continue;
    running = true;
    c.conn.send({ type: "pause" });
  }
  if (!running) return;
  try {
    const sent = chrome.tabs.sendMessage(tabId, { type: "_esc" }, { frameId: 0 });
    if (sent && typeof sent.catch === "function") sent.catch(() => {});
  } catch (e) { /* the tab went */ }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "_esc") { onEsc(sender); return false; }
  if (!msg || msg.type !== "_query") return false;
  const tabId = sender && sender.tab ? sender.tab.id : undefined;
  if (!Number.isInteger(tabId) || (sender.frameId !== undefined && sender.frameId !== 0)) {
    sendResponse({ active: false, refresh_hz: null });
    return false;
  }
  getActive().then((a) => {
    const active = a !== null && a.id === tabId;
    sendResponse({ active, refresh_hz: active && typeof a.refresh_hz === "number" ? a.refresh_hz : null });
  });
  return true;   // answered asynchronously
});

function toggle(tab) {
  if (!tab || !Number.isInteger(tab.id)) return;
  chrome.tabs.sendMessage(tab.id, { type: "_toggle" }).catch(() => {
    warn("BrainPoint cannot run in this tab: Chrome pages and the Web Store allow no extensions, " +
         "and a tab opened before BrainPoint was installed or reloaded needs a reload.");
  });
}

chrome.action.onClicked.addListener(toggle);

chrome.tabs.onRemoved.addListener((tabId) => { clearActive(tabId); });

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !(changes.serverUrl || changes.token)) return;
  // Reconnect every running tab with the new address or token.
  loadSettings().then((settings) => {
    for (const [port, c] of conns) {
      if (c.conn !== null && c.fields !== null) openConn(port, c, settings);
    }
  });
});
