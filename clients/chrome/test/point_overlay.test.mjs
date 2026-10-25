// point_overlay.js: hello fields, patch geometry, gaze hit-testing, label placement, the model
// (flicker safety, onset, Esc, close codes, gaze) and the view on a fake DOM. No browser.
//     node --test clients/chrome/test/*.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { makeFakePage } from "./fakes.mjs";

const require = createRequire(import.meta.url);
const C = require("../brainsync-client.js");
const O = require("../point_overlay.js");

const WELCOME = { type: "welcome", protocol: 3, role: "controller", mode: "point", fake: true,
                  waveform: "sine", refresh_hz: 60.0, refresh_tolerance_hz: 1.0,
                  freqs: [15, 17, 12, 20], max_slots: 4, detect: true };
const RUN = { type: "phase", phase: "run", flicker: true, seconds: null, text: "" };
const LAYOUT = { type: "layout", layout_id: 7, stage: "pick", targets: [
  { slot: "b1", freq: 15.0, phase: 0.0, label: "Sign in", kind: "button",
    rects: [{ x: 0.1, y: 0.1, w: 0.1, h: 0.2 }] },
  { slot: "more", freq: 20.0, phase: 0.0, label: "More", kind: "more",
    rects: [{ x: 0.86, y: 0.8, w: 0.12, h: 0.18 }] }] };
const IDLE = { type: "layout", layout_id: 8, stage: "idle", targets: [] };

const close = (a, b, tol = 1e-9) => assert.ok(Math.abs(a - b) <= tol, `${a} != ${b}`);

// ------------------------------------------------------------------ pure helpers

test("helloFields: a point-mode controller hello", () => {
  const f = O.helloFields(59.98123, { w: 1512, h: 857, dpr: 2 }, false);
  assert.deepEqual(f, {
    role: "controller", takeover: false, client: { name: "brainpoint-extension", kind: "extension", version: "0.1.0" },
    mode: "point", slots: null, refresh_hz: 59.981, accepted: true, viewport: { w: 1512, h: 857, dpr: 2 } });
  assert.equal(O.helloFields(60, { w: 1, h: 1, dpr: 1 }, 1).takeover, true);
  assert.equal(f.token, undefined, "Connection adds the token, not the page");
});

test("patchBoxes converts normalized rects to CSS px and skips bad rects", () => {
  const boxes = O.patchBoxes(LAYOUT.targets, 1000, 500);
  assert.equal(boxes.length, 2);
  assert.equal(boxes[0].slot, "b1");
  assert.equal(boxes[0].label, "Sign in");
  const r = boxes[0].rects[0];
  close(r.x, 100); close(r.y, 50); close(r.w, 100); close(r.h, 100);
  const weird = O.patchBoxes([{ slot: "x", rects: [{ x: NaN, y: 0, w: 1, h: 1 }, { x: 0, y: 0, w: 0.5, h: 0.5 }] },
                              null, { slot: 3 }, { slot: "nolabel" }], 100, 100);
  assert.equal(weird.length, 2);
  assert.equal(weird[0].rects.length, 1);
  assert.equal(weird[1].label, "nolabel");
  assert.deepEqual(weird[1].rects, []);
  assert.deepEqual(O.patchBoxes(undefined, 10, 10), []);
});

test("hitTest finds the patch under the pointer, edges included", () => {
  const boxes = O.patchBoxes(LAYOUT.targets, 1000, 500);
  assert.equal(O.hitTest(boxes, 150, 100), "b1");
  assert.equal(O.hitTest(boxes, 100, 50), "b1");
  assert.equal(O.hitTest(boxes, 200, 150), "b1");
  assert.equal(O.hitTest(boxes, 201, 150), null);
  assert.equal(O.hitTest(boxes, 900, 450), "more");
  assert.equal(O.hitTest(boxes, 500, 250), null);
  assert.equal(O.hitTest(boxes, NaN, 1), null);
  assert.equal(O.hitTest([], 1, 1), null);
});

test("labelBox goes below, above near the bottom, inside when neither fits, and stays on screen", () => {
  const r = { x: 100, y: 100, w: 100, h: 100 };
  const pad = O.borderPx(r) + O.LABEL_PAD;
  const below = O.labelBox(r, 60, 20, 1000, 500);
  assert.deepEqual(below, { x: 120, y: 200 + pad, where: "below" });
  const low = { x: 100, y: 400, w: 100, h: 90 };
  const above = O.labelBox(low, 60, 20, 1000, 500);
  assert.equal(above.where, "above");
  assert.equal(above.y, 400 - (O.borderPx(low) + O.LABEL_PAD) - 20);
  const tall = { x: 0, y: 5, w: 100, h: 490 };
  const inside = O.labelBox(tall, 60, 20, 1000, 500);
  assert.equal(inside.where, "inside");
  assert.ok(inside.y >= 0 && inside.y + 20 <= 500);
  assert.equal(O.labelBox({ x: 980, y: 10, w: 20, h: 20 }, 80, 20, 1000, 500).x, 920);
  assert.equal(O.labelBox({ x: 0, y: 10, w: 20, h: 20 }, 80, 20, 1000, 500).x, 0);
  assert.equal(O.borderPx({ w: 10, h: 10 }), 3);
  assert.equal(O.borderPx({ w: 500, h: 200 }), 12);
});

// ------------------------------------------------------------------ the model

function model(extra = {}) {
  const sent = [];
  let clock = 0;
  let canSend = true;
  const m = O.createModel({
    send: (msg) => { if (!canSend) return false; sent.push(msg); return true; },
    now: () => clock, wall: (t) => 1_700_000_000 + t / 1000, ...extra,
  });
  return { m, sent, tick: (ms) => { clock += ms; }, block: (b) => { canSend = !b; } };
}

function live(m) {
  m.accept();
  assert.equal(m.measured(60.0), "connect");
  m.onState("connecting");
  m.onState("open");
  m.onMessage(WELCOME);
  m.onMessage(RUN);
  m.onMessage(LAYOUT);
}

test("nothing is drawn before Start, and nothing flickers without accept", () => {
  const { m, sent } = model();
  m.onState("open");
  m.onMessage(WELCOME);
  m.onMessage(RUN);
  m.onMessage(LAYOUT);
  const v = m.frame(16.7);
  assert.equal(v.mode, "hidden");
  assert.equal(m.flicker.shouldFlicker(), false);
  assert.deepEqual(sent, [], "no onset before the warning was accepted");
  assert.equal(m.measured(60), null, "no connect before Start");
  assert.equal(m.banner(), "");
});

test("live: patches flicker, the onset goes out once per layout, frames every 5 s", () => {
  const { m, sent } = model();
  live(m);
  const lums = [];
  for (let i = 0; i < 6; i++) {
    const v = m.frame(1000 + i * (1000 / 60));
    assert.equal(v.mode, "flicker");
    lums.push(v.items[0].lum);
    assert.deepEqual(v.items[1].rects, LAYOUT.targets[1].rects);
  }
  close(lums[0], 0.5);
  close(lums[1], C.flicker.luminance(15, 0, 1, 60));
  const onsets = sent.filter((x) => x.type === "onset");
  assert.equal(onsets.length, 1);
  assert.equal(onsets[0].layout_id, 7);
  close(onsets[0].t_client, 1_700_000_001);
  m.frame(1000 + 5001);
  assert.equal(sent.filter((x) => x.type === "frames").length, 1);
  // a new layout means a new onset
  m.onMessage({ ...LAYOUT, layout_id: 9 });
  m.frame(7000);
  assert.deepEqual(sent.filter((x) => x.type === "onset").map((x) => x.layout_id), [7, 9]);
});

test("rule 1: an idle layout, a non-flicker phase, a closed socket or stop phase keep everything still", () => {
  const { m } = model();
  live(m);
  assert.equal(m.frame(100).mode, "flicker");
  m.onMessage(IDLE);
  assert.equal(m.frame(117).mode, "hidden", "idle: no targets, nothing to draw");
  m.onMessage(LAYOUT);
  m.onMessage({ type: "phase", phase: "pause", flicker: false, text: "Clicking…" });
  const still = m.frame(134);
  assert.equal(still.mode, "still");
  assert.equal(still.items[0].lum, 0.5);
  m.onMessage(RUN);
  assert.equal(m.frame(150).mode, "flicker");
  m.onState("closed");
  assert.equal(m.frame(167).mode, "hidden", "a closed socket forgets the layout");
  assert.equal(m.flicker.shouldFlicker(), false);
  m.onState("open");
  m.onMessage(WELCOME);
  m.onMessage({ type: "phase", phase: "stop", flicker: false, text: "Server stopped" });
  m.onMessage(LAYOUT);
  assert.equal(m.frame(184).mode, "hidden");
  assert.equal(m.banner(), "Server stopped");
});

test("Esc stops the flicker at once and sends pause; Resume sends resume", () => {
  const { m, sent } = model();
  live(m);
  m.frame(100);
  m.escape();
  assert.equal(m.frame(117).mode, "still");
  assert.deepEqual(sent.filter((x) => x.type === "pause").length, 1);
  m.escape();
  assert.equal(sent.filter((x) => x.type === "pause").length, 1, "a second Esc sends nothing more");
  assert.match(m.banner(), /paused/);
  assert.match(m.hud()[0], /paused \(Esc\)/);
  // the server's own run phase does not override a local pause
  m.onMessage(RUN);
  assert.equal(m.frame(134).mode, "still");
  m.resume();
  assert.equal(sent.filter((x) => x.type === "resume").length, 1);
  assert.equal(m.frame(150).mode, "flicker");
  m.resume();
  assert.equal(sent.filter((x) => x.type === "resume").length, 1);
});

test("a hidden tab pauses and a shown tab resumes; combined with Esc only one pause goes out", () => {
  const { m, sent } = model();
  live(m);
  m.setVisible(false);
  assert.deepEqual(sent.filter((x) => x.type === "pause" || x.type === "resume").map((x) => x.type), ["pause"]);
  assert.equal(m.frame(100).mode, "still");
  m.escape();
  m.setVisible(true);
  assert.deepEqual(sent.filter((x) => x.type === "pause" || x.type === "resume").map((x) => x.type), ["pause"],
                   "still paused by Esc");
  m.resume();
  assert.deepEqual(sent.filter((x) => x.type === "pause" || x.type === "resume").map((x) => x.type), ["pause", "resume"]);
  m.setVisible(true);
  assert.equal(sent.filter((x) => x.type === "resume").length, 1);
});

test("a pause that could not be sent goes out after the next welcome", () => {
  const { m, sent, block } = model();
  live(m);
  block(true);
  m.escape();
  block(false);
  assert.equal(sent.filter((x) => x.type === "pause").length, 0);
  m.onState("closed");
  m.onState("connecting");
  m.onState("open");
  m.onMessage(WELCOME);
  assert.equal(sent.filter((x) => x.type === "pause").length, 1);
});

test("close codes 4000, 4001 and 4002 say what to do and offer a button", () => {
  for (const [code, action, re] of [[4000, "takeover", /took over/], [4001, "retry", /token/],
                                    [4002, "takeover", /controller/]]) {
    const { m } = model();
    live(m);
    m.onState("closed");
    m.onClose({ code, reason: "", reconnect: false });
    assert.equal(m.state.ended.action, action);
    assert.match(m.banner(), re);
    m.onState("connecting");
    assert.equal(m.state.ended, null);
  }
  const { m } = model();
  live(m);
  m.onClose({ code: 1006, reason: "", reconnect: true });
  assert.equal(m.state.ended, null);
  m.onClose({ code: 1000, reason: "bye", reconnect: false });
  assert.equal(m.state.ended, null);
  m.onClose(null);
});

test("gaze goes out only on the fake board, on change, while open", () => {
  const { m, sent } = model();
  live(m);
  assert.equal(m.hover("b1"), true);
  assert.equal(m.hover("b1"), false);
  assert.equal(m.hover(null), true);
  assert.equal(m.hover(undefined), false);
  assert.deepEqual(sent.filter((x) => x.type === "gaze").map((x) => x.slot), ["b1", null]);
  const real = model();
  real.m.accept(); real.m.measured(60); real.m.onState("open");
  real.m.onMessage({ ...WELCOME, fake: false });
  assert.equal(real.m.hover("b1"), false);
  const closed = model();
  assert.equal(closed.m.hover("b1"), false);
});

test("selection flashes the chosen patch for FLASH_MS, only for the current layout", () => {
  const { m, tick } = model();
  live(m);
  m.onMessage({ type: "selection", selection_id: 1, layout_id: 6, slot: "b1", t_server: 1, score: 1, scores: {} });
  assert.equal(m.frame(100).items[0].flash, false, "a stale layout's selection is ignored");
  m.onMessage({ type: "selection", selection_id: 2, layout_id: 7, slot: "b1", t_server: 1, score: 1, scores: {} });
  const v = m.frame(117);
  assert.equal(v.items[0].flash, true);
  assert.equal(v.items[1].flash, false);
  tick(O.FLASH_MS + 1);
  assert.equal(m.frame(134).items[0].flash, false);
});

test("a refused refresh reconnects once the display reports a good rate", () => {
  const { m } = model();
  live(m);
  m.onMessage({ type: "error", code: "refresh", message: "Set the display to 60 Hz", fatal: false });
  assert.equal(m.banner(), "Set the display to 60 Hz");
  assert.equal(m.measured(120), null);
  assert.equal(m.measured(60.4), "reconnect");
  assert.equal(m.measured(60.4), null, "only once");
  assert.equal(m.measured(-1), null);
  assert.equal(m.measured(NaN), null);
});

test("a refused refresh reconnects at any rate the welcome lists in refresh_rates", () => {
  const { m } = model();
  live(m);
  m.onMessage({ ...WELCOME, refresh_rates: [60, 90, 120, 144] });
  m.onMessage({ type: "error", code: "refresh", message: "Set the display to 60, 90, 120 or 144 Hz",
                fatal: false });
  assert.equal(m.measured(75), null, "75 Hz is not listed");
  assert.equal(m.measured(119.6), "reconnect", "120 Hz is listed");
  assert.match(m.hud().join("\n"), /refresh 119.60 Hz \(want 60, 90, 120 or 144 ± 1\)/);
});

test("HUD and banner follow scans, clicks, status and errors", () => {
  const { m } = model();
  live(m);
  m.onMessage({ type: "status", board: "streaming", ok: true, text: "posterior RMS 14 uV", rtt_ms: 30 });
  m.onMessage({ type: "scan_result", scan_id: 4, ok: true, ms: 1840, n_elements: 23, n_candidates: 9,
                summary: "Login page", elements: [] });
  m.onMessage({ type: "click", click_id: 2, x: 0.1, y: 0.1, element: { id: 3, label: "Sign in", role: "button" } });
  m.state.rttMs = 38;
  const hud = m.hud().join("\n");
  assert.match(hud, /phase run · flickering/);
  assert.match(hud, /rtt 38 ms · fake board/);
  assert.match(hud, /board streaming · posterior RMS 14 uV/);
  assert.match(hud, /refresh 60.00 Hz \(want 60 ± 1\) · dropped 0/);
  assert.match(hud, /scan #4: 9 targets of 23 elements, 1840 ms · Login page/);
  assert.match(hud, /click "Sign in" …/);
  assert.deepEqual(m.warnings(), { conn: false, refresh: false, error: false, board: false });
  m.onMessage({ type: "scan_result", scan_id: 5, ok: false, ms: 20000, n_elements: 0, n_candidates: 0, elements: [] });
  assert.match(m.hud().join("\n"), /scan #5: failed/);
  m.onMessage({ type: "error", code: "detect_unavailable", message: "No API key on the server", fatal: false });
  assert.equal(m.banner(), "No API key on the server");
  assert.match(m.hud().join("\n"), /error detect_unavailable/);
  m.onMessage({ type: "welcome", ...WELCOME });
  m.onMessage(IDLE);
  m.state.activity = "Taking a screenshot…";
  assert.equal(m.banner(), "Taking a screenshot…");
  m.state.activity = "";
  assert.equal(m.banner(), "Looking at the page…");
  m.onMessage({ type: "phase", phase: "calibrate", flicker: false, seconds: 10, text: "Relax" });
  assert.equal(m.banner(), "Relax (10 s)");
  m.onMessage("garbage");
  m.onMessage(null);
});

test("quit holds everything still for good", () => {
  const { m } = model();
  live(m);
  m.quit();
  assert.equal(m.state.stage, "quit");
  assert.equal(m.frame(100).mode, "hidden");
  assert.equal(m.banner(), "");
});

// --------------------------------------------------------------------- the view

function mount(opts = {}) {
  const pg = makeFakePage({ w: 1000, h: 500, dpr: 2 });
  const sent = [];
  const m = O.createModel({ send: (x) => { sent.push(x); return true; }, wall: (t) => t / 1000 });
  const calls = { accept: 0, quit: 0, takeover: [], measured: [] };
  const view = O.create({ win: pg.win, doc: pg.doc, model: m, ...opts,
    onAccept: () => calls.accept++, onQuit: () => calls.quit++,
    onTakeover: (a) => calls.takeover.push(a), onMeasured: (a) => calls.measured.push(a) });
  return { pg, m, view, sent, calls };
}

const findText = (root, text) => root.find((n) => n.textContent === text);

test("view: mounts a closed shadow root with the warning, then measures, connects and draws patches", () => {
  const { pg, m, view, sent, calls } = mount();
  assert.equal(pg.html.children[0], view.host);
  assert.equal(view.host.id, "brainpoint-overlay");
  assert.equal(view.host._shadow.mode, "closed");
  assert.match(view.host.style.cssText, /pointer-events:none/);
  assert.match(view.host.style.cssText, /z-index:2147483647/);
  const modal = view.host.find((n) => n.className === "modal");
  assert.equal(modal.hidden, false);
  assert.match(modal.allText(), /Photosensitivity warning/);
  assert.match(modal.allText(), /15 and 25 Hz/);
  const canvas = view.host.find((n) => n.tagName === "CANVAS");
  assert.equal(canvas.width, 2000);
  assert.equal(canvas.height, 1000);
  // nothing measured or drawn before Start
  pg.frames(200);
  assert.deepEqual(calls.measured, []);
  findText(view.host, "Start").fire("click");
  assert.equal(calls.accept, 1);
  assert.equal(modal.hidden, true);
  pg.frames(C.WARMUP_FRAMES + C.REFRESH_FRAMES + 1);
  assert.deepEqual(calls.measured, ["connect"]);
  close(m.state.refreshHz, 60, 1e-6);
  m.onState("open");
  m.onMessage(WELCOME);
  m.onMessage(RUN);
  m.onMessage(LAYOUT);
  canvas.ctx.calls.length = 0;
  pg.frames(3);
  const fills = canvas.ctx.calls.filter((c) => c[0] === "fillRect");
  // per patch: black border, grey patch, label backing
  assert.ok(fills.some((c) => c[1] === "#000" && c[2] < 100 && c[3] < 50), "black border around the b1 patch");
  assert.ok(fills.some((c) => /^rgb\(/.test(c[1]) && c[2] === 100 && c[3] === 50 && c[4] === 100 && c[5] === 100));
  assert.ok(canvas.ctx.calls.some((c) => c[0] === "fillText" && c[1] === "Sign in"));
  assert.ok(canvas.ctx.calls.some((c) => c[0] === "fillText" && c[1] === "More"));
  assert.equal(sent.filter((x) => x.type === "onset").length, 1);
  assert.equal(view.boxes().length, 2);
});

test("view: mousemove over a patch sends gaze on the fake board; leaving the page sends null", () => {
  const { pg, m, view, sent } = mount({ resumed: true });
  m.measured(60);
  m.onState("open");
  m.onMessage(WELCOME);
  m.onMessage(RUN);
  m.onMessage(LAYOUT);
  pg.frames(2);
  pg.doc.fire("mousemove", { clientX: 150, clientY: 100 });
  pg.doc.fire("mousemove", { clientX: 151, clientY: 101 });
  pg.doc.fire("mousemove", { clientX: 500, clientY: 250 });
  pg.doc.fire("mousemove", { clientX: 900, clientY: 450 });
  pg.doc.fire("mouseout", { relatedTarget: {} });
  pg.doc.fire("mouseout", { relatedTarget: null });
  assert.deepEqual(sent.filter((x) => x.type === "gaze").map((x) => x.slot), ["b1", null, "more", null]);
  view.destroy();
});

test("view: Esc cancels the warning, pauses when live; Resume and Stop show only while paused", () => {
  const first = mount();
  first.pg.win.fire("keydown", { key: "Escape" });
  assert.equal(first.calls.quit, 1, "Esc on the warning cancels");
  first.pg.win.fire("keydown", { key: "a" });
  assert.equal(first.calls.quit, 1);

  const { pg, m, view, sent, calls } = mount({ resumed: true });
  m.measured(60);
  m.onState("open");
  m.onMessage(WELCOME);
  view.update();
  const resume = findText(view.host, "Resume");
  const stop = findText(view.host, "Stop BrainPoint");
  assert.equal(resume.hidden, true);
  assert.equal(stop.hidden, true, "no buttons over the page while running");
  pg.win.fire("keydown", { key: "Escape" });
  assert.equal(m.state.paused, true);
  assert.equal(sent.filter((x) => x.type === "pause").length, 1);
  assert.equal(resume.hidden, false);
  assert.equal(stop.hidden, false);
  resume.fire("click");
  assert.equal(m.state.paused, false);
  assert.equal(sent.filter((x) => x.type === "resume").length, 1);
  pg.win.fire("keydown", { key: "Escape" });
  stop.fire("click");
  assert.equal(calls.quit, 1);
});

test("view: Take over / Retry button after a no-reconnect close", () => {
  const { m, view, calls } = mount({ resumed: true });
  m.measured(60);
  m.onState("closed");
  m.onClose({ code: 4002, reason: "", reconnect: false });
  view.update();
  const btn = findText(view.host, "Take over");
  assert.equal(btn.hidden, false);
  btn.fire("click");
  assert.deepEqual(calls.takeover, ["takeover"]);
  m.onClose({ code: 4001, reason: "", reconnect: false });
  view.update();
  assert.equal(btn.textContent, "Retry");
  btn.fire("click");
  assert.deepEqual(calls.takeover, ["takeover", "retry"]);
});

test("view: a hidden tab pauses, setHidden hides the host, afterFrames resolves, destroy cleans up", async () => {
  const { pg, m, view } = mount({ resumed: true });
  pg.doc.visibilityState = "hidden";
  pg.doc.fire("visibilitychange");
  assert.equal(m.state.hiddenTab, true);
  pg.doc.visibilityState = "visible";
  pg.doc.fire("visibilitychange");
  assert.equal(m.state.hiddenTab, false);
  view.setHidden(true);
  assert.equal(view.host.style.visibility, "hidden");
  view.setHidden(false);
  assert.equal(view.host.style.visibility, "visible");
  let done = false;
  const p = view.afterFrames(2).then(() => { done = true; });
  pg.frames(1);
  await Promise.resolve();
  assert.equal(done, false);
  pg.frames(1);
  await p;
  assert.equal(done, true);
  assert.equal(view.isOurs(view.host), true);
  assert.equal(view.isOurs({}), false);
  assert.equal(view.isOurs(null), false);
  view.destroy();
  view.destroy();
  assert.equal(pg.html.children.length, 0);
  assert.equal((pg.win.listeners.keydown || []).length, 0);
  assert.equal((pg.doc.listeners.mousemove || []).length, 0);
  assert.equal(pg.rafCount(), 0);
  assert.equal(pg.timers.pending.size, 0, "the HUD timer is gone");
});

test("view: a tab that starts hidden is paused from the start", () => {
  const pg = makeFakePage();
  pg.doc.visibilityState = "hidden";
  const m = O.createModel({ send: () => true });
  O.create({ win: pg.win, doc: pg.doc, model: m, resumed: true });
  assert.equal(m.state.hiddenTab, true);
});

// ------------------------------------------------------------------ review fixes

const E = require("../esc.js");

test("view: Esc through the esc.js hub pauses; one key press seen twice acts once; destroy unhooks", () => {
  const hubWin = makeFakePage();
  const hub = E.install({ win: hubWin.win, doc: hubWin.doc, top: true });
  const first = mount({ esc: hub });
  const ev = { key: "Escape" };
  hub.handle(ev);
  first.pg.win.listeners.keydown[0](ev);           // the same event reaches the window listener
  assert.equal(first.calls.quit, 1, "Esc on the warning cancels, once");
  first.view.destroy();

  const { m, view, sent } = mount({ resumed: true, esc: hub });
  m.measured(60);
  m.onState("open");
  m.onMessage(WELCOME);
  hub.handle({ key: "Escape" });
  assert.equal(m.state.paused, true);
  assert.equal(sent.filter((x) => x.type === "pause").length, 1);
  view.destroy();
  m.resume();
  hub.handle({ key: "Escape" });
  assert.equal(m.state.paused, false, "a destroyed view no longer listens");
});

test("view: escape() is the Esc action for a key pressed in a sub-frame", () => {
  const { m, view, sent } = mount({ resumed: true });
  m.measured(60);
  m.onState("open");
  m.onMessage(WELCOME);
  view.escape();
  assert.equal(m.state.paused, true);
  assert.deepEqual(sent.filter((x) => x.type === "pause").length, 1);
});

test("view: a new layout is hit-tested at the resting pointer; no patches send gaze null", () => {
  const { pg, m, view, sent } = mount({ resumed: true });
  m.measured(60);
  m.onState("open");
  m.onMessage(WELCOME);
  m.onMessage(RUN);
  m.onMessage(LAYOUT);
  pg.frames(2);
  pg.doc.fire("mousemove", { clientX: 150, clientY: 100 });   // on b1
  const gazes = () => sent.filter((x) => x.type === "gaze").map((x) => x.slot);
  assert.deepEqual(gazes(), ["b1"]);
  // The next page: b1 is another element elsewhere, and "more" now sits under the pointer.
  m.onMessage({ type: "layout", layout_id: 9, stage: "pick", targets: [
    { slot: "b1", freq: 15.0, phase: 0.0, label: "Other", kind: "button", rects: [{ x: 0.5, y: 0.1, w: 0.1, h: 0.2 }] },
    { slot: "more", freq: 20.0, phase: 0.0, label: "More", kind: "more", rects: [{ x: 0.1, y: 0.1, w: 0.1, h: 0.2 }] }] });
  pg.frames(1);
  assert.deepEqual(gazes(), ["b1", "more"]);
  m.onMessage(IDLE);                                        // nothing drawn: looking at nothing
  pg.frames(1);
  assert.deepEqual(gazes(), ["b1", "more", null]);
  m.onMessage({ ...LAYOUT, layout_id: 10 });                // b1 back under the still pointer
  pg.frames(2);
  assert.deepEqual(gazes(), ["b1", "more", null, "b1"]);
  pg.frames(5);
  assert.equal(gazes().length, 4, "no repeats while nothing changes");
  view.destroy();
});
