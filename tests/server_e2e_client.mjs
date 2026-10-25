// End-to-end driver for tests/test_server_e2e.py: the real JavaScript clients against a real
// BrainSync server, over a real socket, with Node's built-in WebSocket.
//
//     node tests/server_e2e_client.mjs <repo root> <ws url> <token or ""> slots|point
//
// slots: the reference page's hello (clients/web/app.js), the SDK's Connection and
//   FlickerState (onset, frames), then gaze "yes" until a selection of yes and gaze "no"
//   until a selection of no.
// point: the extension's hello (clients/chrome/point_overlay.js) and its scan/click agent
//   (clients/chrome/content.js) with stub page access: scan_request -> scan, pick layout ->
//   gaze b1 -> click -> clicked -> the next scan_request.
//
// Each event goes to stdout as one JSON line. Exit 0 when the scenario finished, 1 otherwise.
// The token is passed through and never printed.
import { createRequire } from "node:module";
import path from "node:path";

const [root, url, tokenArg, scenario] = process.argv.slice(2);
const require = createRequire(import.meta.url);
const C = require(path.join(root, "clients/js/brainsync-client.js"));
const token = tokenArg ? tokenArg : null;
const VIEWPORT = { w: 1000, h: 600, dpr: 1 };
// A JPEG's first bytes are enough for the server; the stub detector never decodes it.
const JPEG_B64 = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 16, 0x4a, 0x46, 0x49, 0x46]).toString("base64");

const say = (event, fields) => process.stdout.write(JSON.stringify(Object.assign({ event }, fields || {})) + "\n");
const fail = (why) => { say("fail", { why }); process.exit(1); };
setTimeout(() => fail("timed out"), 45000).unref();

function makeHello() {
  if (scenario === "slots") {
    const W = require(path.join(root, "clients/web/app.js"));
    return W.helloFields(60, VIEWPORT, true);
  }
  const O = require(path.join(root, "clients/chrome/point_overlay.js"));
  return O.helloFields(60, VIEWPORT, true);
}

const conn = new C.Connection({ url, token, hello: makeHello, WebSocket: globalThis.WebSocket });
const flicker = new C.FlickerState({});
flicker.accept();
const gaze = (slot) => { conn.send({ type: "gaze", slot }); say("gaze", { slot }); };

conn.on("state", (s) => { flicker.setOpen(s === "open"); say("state", { state: s }); });
let finished = false;
const finish = (fields) => { finished = true; say("done", fields); conn.close(); process.exit(0); };
conn.on("close", (info) => { if (!info.reconnect && !finished) fail("closed " + info.code + " " + info.reason); });
conn.on("message", (msg) => {
  flicker.handle(msg);
  if (msg.type === "error") say("server_error", { code: msg.code, message: msg.message });
});
conn.on("welcome", (msg) => say("welcome", { fake: msg.fake, detect: msg.detect, max_slots: msg.max_slots }));

// The page's animation loop: onset on the first flickering frame, frames every 5 s.
let onsets = 0;
setInterval(() => {
  const t = performance.now();
  flicker.tick(t);
  const onset = flicker.onsetDue(t);
  if (onset && conn.send(onset)) { onsets++; say("onset", { layout_id: onset.layout_id }); }
  const frames = flicker.framesDue(t);
  if (frames) conn.send(frames);
}, 16);

if (scenario === "slots") {
  let looking = null;
  conn.on("layout", (msg) => say("layout", { stage: msg.stage, slots: msg.targets.map((t) => t.slot) }));
  conn.on("phase", (msg) => {
    say("phase", { phase: msg.phase });
    if (msg.phase === "run" && looking === null) { looking = "yes"; gaze("yes"); }
  });
  conn.on("selection", (msg) => {
    say("selection", { slot: msg.slot });
    if (msg.slot === "yes" && looking === "yes") { looking = "no"; gaze("no"); }
    else if (msg.slot === "no" && looking === "no") {
      gaze(null);
      finish({ onsets, rtt_ms: conn.rttMs });
    }
  });
} else if (scenario === "point") {
  const K = require(path.join(root, "clients/chrome/content.js"));
  const hints = [{ id: 0, x: 100, y: 100, w: 120, h: 40, role: "button", text: "Sign in", tag: "button" }];
  let clicks = 0;
  const agent = K.createAgent({
    send: (msg) => { if (msg.type === "scan" || msg.type === "clicked") say("sent", { type: msg.type }); return conn.send(msg); },
    sleep: (ms) => new Promise((r) => setTimeout(r, Math.min(ms, 50))),
    blocked: () => null,
    collect: () => ({ hints, masks: [], viewport: Object.assign({}, VIEWPORT) }),
    hide: () => {},
    afterFrames: () => Promise.resolve(),
    capture: () => Promise.resolve("data:image/jpeg;base64," + JPEG_B64),
    encode: () => Promise.resolve({ media_type: "image/jpeg", data: JPEG_B64, w: 1000, h: 600 }),
    clickAt: (x, y) => { say("clickAt", { x, y }); return { ok: true, method: "element.click", detail: "" }; },
    url: () => "https://example.com/login",
    title: () => "Example login",
  });
  conn.on("scan_request", (msg) => {
    say("scan_request", { scan_id: msg.scan_id });
    if (clicks > 0) { finish({ onsets }); return; }
    agent.handle(msg);
  });
  conn.on("scan_result", (msg) => say("scan_result", { ok: msg.ok, n_candidates: msg.n_candidates }));
  conn.on("layout", (msg) => {
    say("layout", { stage: msg.stage, slots: msg.targets.map((t) => t.slot) });
    if (msg.stage === "pick" && clicks === 0) gaze("b1");
  });
  conn.on("click", (msg) => {
    clicks++;
    say("click", { label: msg.element.label, x: msg.x, y: msg.y });
    gaze(null);
    agent.handle(msg);
  });
} else {
  fail("unknown scenario " + scenario);
}

conn.open();
