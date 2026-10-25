// Partner page: renders what the server says and reports events back. All state is server-side.
"use strict";

const $ = (id) => document.getElementById(id);
const RECONNECT_MS = 1000;
const REPLACED = 4000;  // close code: another partner page took over (app/server.py)

let ws = null;
let shown = null;        // last show_page
let phase = "";          // last trial phase
let accepted = false;    // flicker warning accepted
let keyboardMode = false;
let lastSpoken = null;   // utterance_id of the last spoken reply, for Correct / Wrong
let userName = "User";   // the user's first name in the conversation, from /api/config

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
  ws.onopen = () => setConnected(true);
  ws.onmessage = (ev) => handle(JSON.parse(ev.data));
  ws.onclose = (ev) => {
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
    case "trial": renderPhase(msg.phase); break;
    case "highlight": markPicked(msg.slot); break;
    case "speak": speakOut(msg); break;
    case "listen": listenFor(msg); break;
    case "state": renderState(msg); break;
    case "transcript": addLine(msg.who, msg.text); break;
    case "status": renderStatus(msg); break;
  }
}

// Diamond positions by slot (s1 top, s2 left, s3 right, s4 bottom, as on the user's screen).
const POSITIONS = { s1: "top", s2: "left", s3: "right", s4: "bottom" };
const HEADINGS = { page: "Pick a reply", confirm: "Say this?", list: "Highlighted reply" };

function renderPage(msg) {
  shown = msg;
  $("option-count").textContent = msg.mode === "list"
    ? `Page ${msg.page} · option ${msg.highlight_index + 1} of ${msg.options.length}`
    : `Page ${msg.page} · ${HEADINGS[msg.mode]}`;
  $("option-text").textContent = msg.mode === "page" ? "" : msg.text;
  renderList(msg);
  const targets = $("targets");
  targets.className = `targets ${msg.mode === "list" ? "row" : "diamond"}`;
  targets.replaceChildren(...msg.slots.map((s) => {
    // In keyboard mode the boxes are buttons too (a click = the slot's key), for touch screens.
    const el = document.createElement(keyboardMode ? "button" : "div");
    const key = s.slot.replace("s", "");
    el.className = `target ${s.role} pos-${POSITIONS[s.slot] || "top"}`;
    el.dataset.slot = s.slot;
    // The sentence is what gets spoken, so it is the big text; the short label is its tag.
    // Targets without a sentence (More options, Say it, Back) show the label big instead.
    el.innerHTML = `<span class="target-tag"></span><span class="target-text"></span><span class="target-key"></span>`;
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

function renderPhase(next) {
  if (next === "flicker" && phase !== "flicker") startTimer();
  if (next !== "flicker") stopTimer();
  phase = next;
  const el = $("phase");
  el.textContent = phase === "flicker" ? "flickering" : phase;
  el.className = `phase ${phase}`;
  document.querySelectorAll(".target").forEach((t) => {
    t.classList.toggle("active", phase === "flicker");
    if (phase === "cue") t.classList.remove("picked");
    if (t.tagName === "BUTTON") t.disabled = phase !== "flicker";
  });
}

// The selection timer: how long the user has been looking, out of stim.max_trial_s. A steady
// fill, updated a few times a second; nothing on this page flashes.
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
  document.querySelectorAll(".target").forEach((t) => t.classList.toggle("picked", t.dataset.slot === slot));
}

function renderState(msg) {
  const b = ViewCore.banner(msg.name, msg.detail, shown && shown.mode);
  $("state").textContent = b.text;
  $("banner").className = `banner tone-${b.tone}`;
  const box = $("speaking");
  box.hidden = msg.name !== "SPEAKING";
  if (msg.name === "SPEAKING") $("speaking-text").textContent = `“${msg.detail}”`;
  $("option").hidden = msg.name === "SPEAKING";  // the sentence is in the Speaking box then
  if (msg.name === "PAUSED" || msg.name === "IDLE") {
    Speech.cancel();
    speaking = false;  // a cancelled clip never reports its end: without this the mic stayed off
  }
}

function speakOut(msg) {
  lastSpoken = msg.utterance_id;
  setJudgment("");
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
  $("conversation").append(li);
  li.scrollIntoView({ block: "end" });
}

function renderStatus(s) {
  const modeChanged = keyboardMode !== s.keyboard_mode;
  keyboardMode = s.keyboard_mode;
  const badges = [
    [s.decoder_connected ? (s.decoder_ok ? "ok" : "bad") : "bad",
      s.decoder_connected ? (s.decoder_ok ? "DECODER OK" : "DECODER NOT OK") : "NO DECODER"],
    [s.display_ok ? "ok" : "bad", s.display_ok ? "DISPLAY OK" : "DISPLAY OFF"],
    [s.board_ok ? "ok" : "bad", s.board_ok ? "BOARD OK" : "BOARD ?"],
    { ok: ["ok", "LLM OK"], waiting: ["", "LLM READY"], failed: ["bad", "LLM FAILED"] }[s.llm]
      || ["", "LLM OFF"],
  ];
  if (s.mic_muted) badges.push(["flag", "MIC MUTED"]);
  else if (s.mic === "listening") badges.push(["ok", "MIC ON"]);
  else if (["denied", "nomic", "offline", "unsupported", "error", "needs_tap"].includes(s.mic)) {
    badges.push(["bad", `MIC: ${s.mic.toUpperCase()}`]);
  }
  const pill = ([cls, text]) => {
    const b = document.createElement("span");
    b.className = `badge ${cls}`;
    b.textContent = text;
    return b;
  };
  $("badges").replaceChildren(...badges.map(pill));
  // One line for the operator; the details open on a click. A fake decoder or keyboard mode is
  // always shown on the mirror too, so nobody mistakes the demo for live EEG.
  const h = ViewCore.health(s);
  $("health").className = `health level-${h.level}`;
  $("health-text").textContent = h.text;
  $("flags").replaceChildren(...h.flags.map((f) => pill(["flag", f])));
  if (s.decoder_name) {
    const name = document.createElement("span");
    name.className = "decoder-name";
    name.textContent = `Decoder: ${s.decoder_name}`;
    $("decoder-message").replaceChildren(name, s.decoder_message ? ` · ${s.decoder_message}` : "");
  } else {
    $("decoder-message").textContent = s.decoder_message || "";
  }
  const resume = $("resume");
  resume.disabled = !s.resume_ready;
  resume.title = s.resume_ready ? "" : "Not ready: see the state line";
  $("hint").textContent = s.keyboard_mode
    ? "Keyboard mode: tap a box (or its key, 1 = top … 4 = bottom) while it flickers. C / W mark the last reply."
    : "C / W mark the last spoken reply correct / wrong.";
  if (shown && modeChanged) renderPage(shown);
}

// Correct / Wrong: the partner's mark for the last spoken reply (logged for the summary).
function judge(key) {
  if (!lastSpoken) return;
  if (send({ type: "key", key })) setJudgment(key === "c" ? "Marked correct" : "Marked wrong");
}

function setJudgment(text) {
  $("judged").textContent = text;
  $("correct").disabled = $("wrong").disabled = !lastSpoken;
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
  send({ type: "resume" });
});
$("stop").addEventListener("click", () => {
  if (!send({ type: "stop" })) $("state").textContent = "Not connected: press Esc on the display";
});
$("resume").addEventListener("click", () => {
  if (!accepted) return;
  Speech.unlock();
  keepAwake();
  send({ type: "resume" });
});
// The laptop's page offers the phone links (python -m app.server --tunnel, or on the LAN): the web
// page and the iPhone app's pairing link. A phone never gets them.
function copyLink(sourceId, copiedText) {
  const url = $(sourceId).textContent;
  const done = (text) => { $("phone-copied").textContent = text; };
  if (navigator.clipboard) navigator.clipboard.writeText(url).then(() => done(copiedText), () => done("Select the link and copy it"));
  else done("Select the link and copy it");
}
$("copy-phone").addEventListener("click", () => copyLink("phone-url", "Copied: send it to the phone"));
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
$("health").addEventListener("click", () => {
  const details = $("health-details");
  details.hidden = !details.hidden;
  $("health").setAttribute("aria-expanded", String(!details.hidden));
});
$("correct").addEventListener("click", () => judge("c"));
$("wrong").addEventListener("click", () => judge("w"));

$("say-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const input = $("partner-input");
  const text = input.value.trim();
  if (text) send({ type: "partner_text", text, source: "typed" });
  input.value = "";
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") { send({ type: "stop" }); return; }
  if (ev.target.closest("input, textarea") || ev.repeat || !accepted) return;
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
    if (c.user_name) userName = c.user_name;
    if (c.phone_url) {
      $("phone-url").textContent = c.phone_url;
      $("phone").hidden = false;
    }
    if (c.app_link) {
      $("app-url").textContent = c.app_link;
      $("app-url").hidden = false;
      $("copy-app").hidden = false;
    }
    $("ptt").textContent = sttMode === "always" ? "Restart mic" : "Listen";
    Listener.configure({ gapMs: (c.stt_gap_s || 1.2) * 1000, maxLineMs: (c.stt_max_line_s || 15) * 1000,
                         echoMs: (c.stt_echo_s || 4) * 1000 });
  })
  .catch(() => {});
setJudgment("");
setConnected(false);
connect();
