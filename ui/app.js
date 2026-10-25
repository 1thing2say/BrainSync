// The session page: renders what the server says and reports events back. All state is server-side.
"use strict";

const $ = (id) => document.getElementById(id);
const RECONNECT_MS = 1000;
const REPLACED = 4000;  // close code: another page took over (app/server.py)

let ws = null;
let shown = null;        // last show_page
let stateName = "";      // last state name
let phase = "";          // last trial phase
let accepted = false;    // flicker warning accepted
let keyboardMode = false;
let lastSpoken = null;   // utterance_id of the last spoken reply, for Correct / Wrong
let userName = "User";   // the user's first name in the conversation, from /api/config
const CONVERSATION_LINES = 4;  // partner + user lines kept on screen (the server keeps the full log)

// Returns false when the server is not connected, so callers can say so.
function send(msg) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  ws.send(JSON.stringify(msg));
  return true;
}

function connect() {
  $("replaced").hidden = true;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/ui`);
  // No resume on (re)connect: the server re-sends its state, and a reconnect after STOP must
  // never restart the flicker. Only the warning's start button and Resume send "resume".
  ws.onopen = () => {
    setConnected(true);
    $("state").textContent = "";  // the server sends its state now
    sendClock();  // the page's clock (and refresh) before any cycle it reports
  };
  ws.onmessage = (ev) => handle(JSON.parse(ev.data));
  ws.onclose = (ev) => {
    stopFlicker();  // no server, no trial: never flicker on (INTERFACE rule 3)
    setConnected(false);
    Listener.stop("disconnected");  // the server re-sends the mic state when we are back
    if (ev.code === REPLACED) {
      // Another tab or phone has the session; reconnecting here would just steal it back.
      $("state").textContent = "Open on another page";
      $("replaced").hidden = false;
      Speech.cancel();
      return;
    }
    $("state").textContent = "Server disconnected, reconnecting…";
    setTimeout(connect, RECONNECT_MS);
  };
}

function setConnected(up) {
  const stop = $("stop");
  stop.textContent = up ? "Stop" : "Not connected: press Esc on the display";
  stop.classList.toggle("offline", !up);
}

function handle(msg) {
  switch (msg.type) {
    case "show_page": renderPage(msg); break;
    case "trial": renderPhase(msg.phase, msg.trial_id); break;
    case "highlight": markPicked(msg.slot); break;
    case "speak": speakOut(msg); break;
    case "listen": listenFor(msg); break;
    case "state": renderState(msg); break;
    case "transcript": addLine(msg.who, msg.text); break;
    case "status": renderStatus(msg); break;
    case "training": renderTraining(msg); break;
    case "confidence": renderConfidence(msg); break;
  }
}

// Diamond positions by slot (s1 top, s2 left, s3 right, s4 bottom, as on the user's screen).
const POSITIONS = { s1: "top", s2: "left", s3: "right", s4: "bottom" };
// The user's screen, from /api/config: "cvep" (c-VEP's three squares in a row, in slot order),
// "diamond" (f-VEP page mode) or "row" (list mode).
let layout = "diamond";
let slotOrder = ["s1", "s2", "s3", "s4"];

function renderPage(msg) {
  shown = msg;
  if (layout === "cvep" && msg.mode !== "list") {
    renderCodeRow(msg);
    renderPhase(phase);
    return;
  }
  // The confirm screen's sentence sits in the empty centre of the diamond, between Say it (top)
  // and Back (bottom), as on the flicker display; list mode keeps it above the targets.
  $("option-text").textContent = msg.mode === "list" ? msg.text : "";
  renderList(msg);
  const targets = $("targets");
  targets.className = `targets ${msg.mode === "list" ? "row" : "diamond"}`;
  $("centre-text").textContent = msg.mode === "confirm" ? msg.text : "";
  $("centre-text").hidden = stateName === "SPEAKING";  // the Speaking box shows it then
  targets.replaceChildren($("centre"), ...msg.slots.map((s) => {
    // In keyboard mode the boxes are buttons too (a click = the slot's key), for touch screens.
    const el = document.createElement(keyboardMode ? "button" : "div");
    const key = s.slot.replace("s", "");
    el.className = `target ${s.role} pos-${POSITIONS[s.slot] || "top"}`;
    el.dataset.slot = s.slot;
    // The sentence is what gets spoken, so it is the big text; the short label is its tag.
    // Targets without a sentence (More options, Say it, Back) show the label big instead.
    // .flicker is the white layer whose opacity the flicker loop drives (display.flicker: browser).
    el.innerHTML = `<span class="flicker" aria-hidden="true"></span><span class="target-tag"></span>`
      + `<span class="target-text"></span><span class="target-key"></span>`;
    const sentence = s.utterance && s.utterance.trim();
    el.querySelector(".target-tag").textContent = sentence ? s.label : "";
    el.querySelector(".target-text").textContent = sentence || s.label;
    el.querySelector(".target-key").textContent = keyboardMode ? `key ${key}` : "";
    if (keyboardMode) {
      el.type = "button";
      el.addEventListener("click", () => send({ type: "key", key }));
    }
    return el;
  }));
  renderPhase(phase);
}

// c-VEP: the three squares in a row, each in its place (s1 left, s2 middle, s3 right), white on
// black, its sentence (or label) and the decoder's bar line under it. ``msg`` null: the training
// row (every square flickers, the cue on ``cueSlot``). A square that does not flicker in the next
// trial (the middle one on the confirm screen) is drawn still and white, with no text.
let cueSlot = "s1";

function renderCodeRow(msg) {
  $("option-text").textContent = "";
  renderList({ mode: "page" });
  $("centre-text").textContent = msg && msg.mode === "confirm" ? msg.text : "";
  $("centre-text").hidden = stateName === "SPEAKING";
  $("code-status").textContent = "";
  const targets = $("targets");
  targets.className = "targets cvep";
  targets.style.setProperty("--cols", String(slotOrder.length));
  const squares = CodeCore.row(slotOrder, msg, cueSlot);
  targets.replaceChildren($("centre"), ...squares.map((s, i) => {
    const button = keyboardMode && msg && s.role !== "still";
    const el = document.createElement(button ? "button" : "div");
    const key = s.slot.replace("s", "");
    el.className = `target code ${s.role}${s.cue ? " cue" : ""}`;
    el.dataset.slot = s.slot;
    el.style.gridColumn = String(i + 1);
    // .square is the code itself: app.js sets its colour every frame while it flickers.
    el.innerHTML = `<span class="square" aria-hidden="true"></span><span class="target-tag"></span>`
      + `<span class="target-text"></span><span class="target-bar"></span><span class="target-key"></span>`;
    const sentence = s.utterance && s.utterance.trim();
    el.querySelector(".target-tag").textContent = sentence ? s.label : "";
    el.querySelector(".target-text").textContent = s.role === "train" ? (s.cue ? "Look here" : "")
      : sentence || s.label;
    el.querySelector(".target-key").textContent = button ? `key ${key}` : "";
    if (button) {
      el.type = "button";
      el.addEventListener("click", () => send({ type: "key", key }));
    }
    return el;
  }));
}

// List mode (the 2-target fallback): every option on the page, the highlighted one marked.
function renderList(msg) {
  const list = $("list");
  list.hidden = msg.mode !== "list";
  list.replaceChildren(...(msg.options || []).map((o, i) => {
    const li = document.createElement("li");
    li.textContent = o.utterance;
    li.classList.toggle("current", i === msg.highlight_index);
    return li;
  }));
}

function renderPhase(next, trialId) {
  if (next === "flicker" && phase !== "flicker") startTimer();
  if (next !== "flicker") stopTimer();
  if (next === "train" && phase !== "train" && layout === "cvep") renderCodeRow(null);
  phase = next;
  if (trialId !== undefined && trialId !== null) flickerTrial = trialId;
  if (next === "cue") clearBars();
  if (next === "flicker") $("training").hidden = true;
  const flickering = phase === "flicker" || phase === "train";
  document.querySelectorAll(".target").forEach((t) => {
    t.classList.toggle("active", flickering && !t.classList.contains("still"));
    if (phase !== "train") t.classList.remove("cue");
    if (phase === "cue") t.classList.remove("picked");
    if (t.tagName === "BUTTON") t.disabled = phase !== "flicker";
  });
  if (flickering) startFlicker();
  else stopFlicker();
}

// --- the flicker (display.flicker: browser) ---------------------------------------------------
// f-VEP: every target on screen flickers at its slot's frequency. c-VEP: every square that
// flickers in this trial (all of them in training) shows the m-sequence delayed by its slot's
// lag, white or black per frame, and the flip of each cycle's first frame goes to the server
// (cycle), which relays it to the bridge. The frame counter n starts at the first flickering
// frame. Only the server starts it (its flicker or train phase, in a trial or training state);
// any other state, Stop, Esc, a lost connection or a hidden tab stop it here at once, without
// waiting for the server.

let flickerOn = false;     // from /api/config: this page draws the flicker
let codeMode = false;      // ... and it is the c-VEP code (stim.paradigm: code)
let codeBlocked = "";      // why this page cannot draw the server's code ("" when it can)
let cycleFrames = 63;
let waveform = "sine";
let slotFreqs = {};        // slot -> {freq_hz, phase_deg, lag_frames}
let expectedHz = 60;
let refreshHz = 60;        // measured at load; frame counts use it
let measuredHz = null;     // the measurement, reported to the server (it checks the rate)
let flickerFrame = null;   // the requestAnimationFrame id while flickering
let onsetMs = null;        // the rAF timestamp of the first flickering frame
let lastN = null;          // c-VEP: the last frame drawn, for the cycle reports
let flickerTrial = null;   // the trial message's trial_id: cycles say which flicker they belong to
const pageClock = new CodeCore.ClockOffset();
// The server's states in which something flickers: a trial, and c-VEP's training (BASELINE).
const FLICKER_STATES = new Set(["STIMULATING", "CONFIRMING", "BASELINE"]);

function startFlicker() {
  if (!flickerOn || !accepted || document.hidden || flickerFrame !== null) return;
  if (!FLICKER_STATES.has(stateName) || (codeMode && codeBlocked)) return;
  onsetMs = null;
  lastN = null;
  flickerFrame = requestAnimationFrame(drawFlicker);
}

function drawFlicker(ms) {
  if (onsetMs === null) onsetMs = ms;
  const period = 1000 / refreshHz;
  const n = FlickerCore.frameIndex(ms - onsetMs, period);
  if (codeMode) drawCode(ms, n, period);
  else {
    document.querySelectorAll(".target").forEach((t) => {
      const slot = slotFreqs[t.dataset.slot];
      const layer = t.querySelector(".flicker");
      if (slot && layer) {
        layer.style.opacity = FlickerCore.luminance(slot.freq_hz, slot.phase_deg, n, refreshHz, waveform);
      }
    });
  }
  flickerFrame = requestAnimationFrame(drawFlicker);
}

// One c-VEP frame: the active squares' colours, then the cycle starts this frame shows.
function drawCode(ms, n, period) {
  document.querySelectorAll(".target.active .square").forEach((sq) => {
    const slot = slotFreqs[sq.parentElement.dataset.slot];
    if (slot) sq.style.background = CodeCore.squareColor(slot.lag_frames, n);
  });
  pageClock.sample();
  for (const c of CodeCore.cyclesDue(lastN, n, cycleFrames)) {
    const at = c.frame === n ? ms : onsetMs + c.frame * period;
    send({ type: "cycle", phase: phase === "train" ? "train" : "flicker", trial_id: flickerTrial,
           n: c.n, t_client: Math.round(pageClock.unix(at) * 1e6) / 1e6 });
  }
  lastN = n;
}

function stopFlicker() {
  if (flickerFrame !== null) cancelAnimationFrame(flickerFrame);
  flickerFrame = null;
  document.querySelectorAll(".target .flicker").forEach((l) => { l.style.opacity = 0; });
  // A still c-VEP square is white (the standard).
  document.querySelectorAll(".target .square").forEach((sq) => { sq.style.background = ""; });
}

// The page's clock, every few seconds, so the server can put the cycle times on its own; with
// the measured refresh (one code bit per frame: the server refuses to start at another rate).
const CLOCK_EVERY_MS = 5000;

function sendClock() {
  if (!flickerOn) return;
  pageClock.sample();
  const msg = { type: "clock", t_client: Math.round(pageClock.now() * 1e6) / 1e6 };
  if (measuredHz) msg.refresh_hz = Math.round(measuredHz * 1000) / 1000;
  send(msg);
}
setInterval(sendClock, CLOCK_EVERY_MS);

// The screen's real refresh, from a second and a half of frames while the page is visible (the
// warning is still up then); measured again if the tab was hidden meanwhile.
function measureRefresh() {
  const stamps = [];
  const step = (ms) => {
    if (document.hidden) { setTimeout(measureRefresh, 500); return; }
    stamps.push(ms);
    if (stamps.length < 90) { requestAnimationFrame(step); return; }
    const m = FlickerCore.measure(stamps);
    if (m) {
      refreshHz = m.hz;
      measuredHz = m.hz;
      sendClock();
    }
    const problem = codeMode && codeBlocked ? codeBlocked
      : flickerOn ? FlickerCore.refreshProblem(m, expectedHz) : "";
    $("refresh-note").textContent = problem;
    $("refresh-note").hidden = !problem;
  };
  requestAnimationFrame(step);
}

// c-VEP: the decoder's latest decision as a bar line under each square, and the status line.
function renderConfidence(msg) {
  const bars = ViewCore.confidence(msg);
  const texts = {};
  document.querySelectorAll(".target").forEach((t) => {
    const b = bars[t.dataset.slot];
    const bar = t.querySelector(".target-bar");
    texts[t.dataset.slot] = (t.querySelector(".target-text") || {}).textContent || "";
    t.classList.toggle("best", Boolean(b && b.best));
    t.classList.toggle("committed", Boolean(b && b.committed));
    if (bar) bar.textContent = b ? CodeCore.barLine(msg.scores[t.dataset.slot], b.best) : "";
  });
  $("code-status").textContent = shown && shown.mode === "confirm" ? "" : CodeCore.statusText(msg, texts);
}

function clearBars() {
  document.querySelectorAll(".target").forEach((t) => {
    t.classList.remove("best", "committed");
    const bar = t.querySelector(".target-bar");
    if (bar) bar.textContent = "";
  });
  $("code-status").textContent = "";
}

// c-VEP: the training card (where to look, the cycles, then the template's accuracy) and the cue
// on the square to look at.
function renderTraining(msg) {
  const v = ViewCore.training(msg);
  const card = $("training");
  card.hidden = false;
  card.className = `training tone-${v.tone}`;
  $("training-title").textContent = v.title;
  $("training-detail").textContent = v.detail;
  $("training-fill").style.width = `${(v.fraction * 100).toFixed(1)}%`;
  cueSlot = msg.slot || cueSlot;
  document.querySelectorAll(".target").forEach((t) => {
    t.classList.toggle("cue", !v.done && phase === "train" && t.dataset.slot === cueSlot);
  });
}

// The selection timer: how long the user has been looking, out of stim.max_trial_s. A steady
// fill, updated a few times a second.
let maxTrialS = 10;  // from /api/config
let timer = null;
let trialStarted = 0;

function startTimer() {
  stopTimer();
  trialStarted = performance.now();
  $("progress").hidden = false;
  drawTimer();
  timer = setInterval(drawTimer, 200);
}

function drawTimer() {
  const p = ViewCore.progress((performance.now() - trialStarted) / 1000, maxTrialS);
  $("progress-fill").style.width = `${(p.fraction * 100).toFixed(1)}%`;
  $("progress-text").textContent = p.label;
}

function stopTimer() {
  if (timer !== null) clearInterval(timer);
  timer = null;
  $("progress").hidden = true;
}

function markPicked(slot) {
  stopFlicker();  // the decoder has picked: the trial is over (the server ends it next)
  document.querySelectorAll(".target").forEach((t) => t.classList.toggle("picked", t.dataset.slot === slot));
}

function renderState(msg) {
  stateName = msg.name;
  if (!FLICKER_STATES.has(msg.name)) stopFlicker();  // only a trial or a training flickers
  const st = ViewCore.status(msg.name, msg.detail, shown && shown.mode);
  $("state").textContent = st.text;
  $("state").className = `state tone-${st.tone}`;
  const box = $("speaking");
  box.hidden = msg.name !== "SPEAKING";
  if (msg.name === "SPEAKING") $("speaking-text").textContent = `“${msg.detail}”`;
  // The sentence is in the Speaking box then.
  $("option").hidden = msg.name === "SPEAKING";
  $("centre-text").hidden = msg.name === "SPEAKING";
  if (msg.name === "PAUSED" || msg.name === "IDLE") {
    Speech.cancel();
    speaking = false;  // a cancelled clip never reports its end: without this the mic stayed off
  }
}

function speakOut(msg) {
  lastSpoken = msg.utterance_id;
  speaking = true;
  Listener.stop("speaking");  // never listen while the laptop talks
  Listener.heardSpoken(msg.text);
  Speech.speak(msg.text, () => {
    speaking = false;
    send({ type: "tts_done", utterance_id: msg.utterance_id });
  }, msg.audio_url);
}

// --- listening to the partner (hands-free; the server owns the mic state) ------------------

let speaking = false;  // a reply is playing: the mic stays off
let listenWindow = 10;  // seconds, from /api/config
let sttMode = "always";  // from /api/config
let mic = { muted: false, until_stopped: false, reason: "idle" };  // the server's last listen
let reported = "";  // the last mic state sent to the server

function listenFor(msg) {
  mic = msg;
  renderMute();
  if (msg.until_stopped) {
    if (!speaking) Listener.want(true, msg.delay_s);
  } else if (msg.seconds > 0) {
    if (!speaking) Listener.windowFor(msg.seconds, msg.delay_s);
  } else {
    Listener.stop(msg.muted ? "muted" : msg.reason);
  }
}

const REPORTABLE = new Set(["listening", "needs_tap", "denied", "nomic", "offline", "unsupported",
                            "error", "stopped", "hidden", "muted", "speaking", "paused"]);

function micReport(state) {
  if (!REPORTABLE.has(state) || state === reported) return;
  if (send({ type: "mic_report", state, restarts: Listener.restarts })) reported = state;
}

Listener.onLine((text) => send({ type: "partner_text", text, source: "stt" }));
let micState = "idle";  // the listener's last state, and what it heard so far
let micText = "";

Listener.onState((state, text) => {
  micState = state;
  micText = text || "";
  renderMic();
  micReport(state === "sent" ? "listening" : state);
});

function renderMic() {
  const c = ViewCore.micCard(micState, micText, Boolean(mic.muted));
  $("mic-card").className = `mic-card tone-${c.tone}`;
  $("mic-title").textContent = c.title;
  $("mic-heard").textContent = c.heard ? `“${c.heard}”` : "";
  $("mic-heard").classList.toggle("final", micState === "sent");
  $("listen-status").textContent = c.detail;
}

function renderMute() {
  const b = $("mute");
  b.textContent = mic.muted ? "Unmute" : "Mute";
  b.setAttribute("aria-pressed", String(Boolean(mic.muted)));
  b.classList.toggle("muted", Boolean(mic.muted));
  renderMic();
}

function addLine(who, text) {
  const li = document.createElement("li");
  li.className = who;
  li.innerHTML = `<span class="who"></span>`;
  li.querySelector(".who").textContent = who === "partner" ? "Partner" : userName;
  li.append(document.createTextNode(text));
  const list = $("conversation");
  list.append(li);
  // Only the latest lines: the partner needs what was just said, not the whole log.
  while (list.children.length > CONVERSATION_LINES) list.firstElementChild.remove();
  // Scroll the list only: scrollIntoView also scrolled the page, carrying the targets off screen.
  list.scrollTop = list.scrollHeight;
}

function renderStatus(s) {
  const modeChanged = keyboardMode !== s.keyboard_mode;
  keyboardMode = s.keyboard_mode;
  // A fake decoder or keyboard mode shows in the server's terminal, /api/health,
  // tools/preflight.py and the log, not on the user's screen.
  const resume = $("resume");
  resume.disabled = !s.resume_ready;
  resume.title = s.resume_ready ? "" : "Not ready: see the state line";
  if (shown && modeChanged) renderPage(shown);
}

// Keys C / W: the operator's mark for the last spoken reply (logged for the summary).
function judge(key) {
  if (lastSpoken) send({ type: "key", key });
}

// --- controls -------------------------------------------------------------

// A phone's screen would sleep mid-conversation and stop the mic: keep it on while the page shows
// (the Screen Wake Lock API; Safari 16.4+, Chrome). Released by the browser when hidden.
async function keepAwake() {
  if (!accepted || document.hidden || !("wakeLock" in navigator)) return;
  try {
    await navigator.wakeLock.request("screen");
  } catch (err) { /* not allowed now (battery saver): the screen just sleeps as usual */ }
}

$("accept").addEventListener("click", () => {
  accepted = true;
  $("warning").hidden = true;
  Speech.unlock();  // this tap lets iOS play the replies later
  keepAwake();
  // A newly opened page starts a fresh session: the home page and an empty conversation.
  $("conversation").replaceChildren();
  send({ type: "resume", start: true });
});
$("stop").addEventListener("click", () => {
  stopFlicker();
  if (!send({ type: "stop" })) $("state").textContent = "Not connected: press Esc on the display";
});
$("resume").addEventListener("click", () => {
  if (!accepted) return;
  Speech.unlock();
  keepAwake();
  send({ type: "resume" });
});
// The laptop's page offers the iPhone app's pairing link (python -m app.server --tunnel, or on the
// LAN). A phone never gets it.
function copyLink(sourceId, copiedText) {
  const url = $(sourceId).textContent;
  const done = (text) => { $("phone-copied").textContent = text; };
  if (navigator.clipboard) navigator.clipboard.writeText(url).then(() => done(copiedText), () => done("Select the link and copy it"));
  else done("Select the link and copy it");
}
$("copy-app").addEventListener("click", () => copyLink("app-url", "Copied: open it on the iPhone"));
$("take-over").addEventListener("click", connect);
$("ptt").addEventListener("click", () => {
  if (speaking) return;
  // Always mode: restart the mic now; otherwise (paused, turn or push-to-talk) one window.
  if (!(sttMode === "always" && Listener.untilStopped && Listener.kick())) {
    if (mic.muted) return;
    if (Listener.wanted && !Listener.untilStopped) Listener.stop("stopped");
    else Listener.windowFor(listenWindow);
  }
});
$("mute").addEventListener("click", () => send({ type: "mic_set", muted: !mic.muted }));
// Chrome may want a click before the mic starts after a reload; any tap does it.
document.addEventListener("pointerdown", () => { if (Listener.state === "needs_tap") Listener.kick(); });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    // A hidden tab draws nothing: stop the trial, so the decoder never picks from a still screen.
    if (flickerFrame !== null) send({ type: "stop" });
    stopFlicker();
    micState = "hidden";
    micText = "";
    renderMic();
    micReport("hidden");
  } else {
    keepAwake();
    if (Listener.wanted) Listener.kick();
  }
});
if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
  navigator.mediaDevices.addEventListener("devicechange", () => Listener.kick());  // lapel plugged in
}
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") { stopFlicker(); send({ type: "stop" }); return; }
  if (ev.repeat || !accepted) return;
  const key = ev.key.toLowerCase();
  if (key === "c" || key === "w") judge(key);
  else if (["1", "2", "3", "4"].includes(key)) send({ type: "key", key });
});

fetch("/api/config")
  .then((r) => r.json())
  .then((c) => {
    Speech.setRate(c.tts_rate);
    if (c.listen_window_s) listenWindow = c.listen_window_s;
    if (c.stt_mode) sttMode = c.stt_mode;
    if (c.max_trial_s) maxTrialS = c.max_trial_s;
    flickerOn = Boolean(c.flicker);
    if (c.waveform) waveform = c.waveform;
    if (c.slots) slotFreqs = c.slots;
    if (c.refresh_hz) expectedHz = refreshHz = c.refresh_hz;
    if (c.layout) layout = c.layout;
    if (Array.isArray(c.slot_order) && c.slot_order.length) slotOrder = c.slot_order;
    codeMode = flickerOn && c.paradigm === "code";
    if (codeMode) {
      const lags = Object.fromEntries(Object.entries(slotFreqs).map(([s, v]) => [s, v.lag_frames]));
      codeBlocked = CodeCore.codeProblem(c.code, lags);
      cycleFrames = c.code ? c.code.cycle_frames : cycleFrames;
    }
    if (shown) renderPage(shown);
    measureRefresh();
    sendClock();
    if (c.user_name) userName = c.user_name;
    if (c.app_link) {
      $("app-url").textContent = c.app_link;
      $("phone").hidden = false;
    }
    $("ptt").textContent = sttMode === "always" ? "Restart mic" : "Listen";
    Listener.configure({ gapMs: (c.stt_gap_s || 1.2) * 1000, maxLineMs: (c.stt_max_line_s || 15) * 1000,
                         echoMs: (c.stt_echo_s || 4) * 1000 });
  })
  .catch(() => {});
setConnected(false);
connect();
