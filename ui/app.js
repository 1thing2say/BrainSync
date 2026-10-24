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
    el.innerHTML = `<span class="role"></span><span class="sentence"></span><span class="slot"></span>`;
    el.querySelector(".role").textContent = s.label;
    el.querySelector(".sentence").textContent = s.utterance;
    el.querySelector(".slot").textContent = s.slot + (keyboardMode ? ` · key ${key}` : "");
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

function markPicked(slot) {
  document.querySelectorAll(".target").forEach((t) => t.classList.toggle("picked", t.dataset.slot === slot));
}

function renderState(msg) {
  const labels = {
    IDLE: "Waiting to start", BASELINE: "Baseline: keep still", PREPARING: "Finding replies…",
    MENU_CUE: "Read the option",
    STIMULATING: shown && shown.mode === "list" ? "Look at SAY or NEXT" : "Look at a reply or More options",
    CONFIRMING: "Look at Say it or Back", SELECTED: "Selected", SPEAKING: "Speaking",
    REST: "Rest", NOT_CAUGHT: "Didn't catch that", PAUSED: "Paused",
  };
  const base = labels[msg.name] || msg.name;
  const detail = msg.detail && msg.name !== "SPEAKING" ? ` — ${msg.detail}` : "";
  $("state").textContent = base + detail;
  const speaking = $("speaking");
  speaking.hidden = msg.name !== "SPEAKING";
  if (msg.name === "SPEAKING") speaking.textContent = `“${msg.detail}”`;
  if (msg.name === "PAUSED" || msg.name === "IDLE") Speech.cancel();
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

// --- listening to the partner (hands-free: the server says when it is their turn) -----------

let speaking = false;  // a reply is playing: the mic stays off
let listenWindow = 10;  // seconds, from /api/config

function listenFor(msg) {
  if (msg.seconds > 0 && accepted && !speaking) Listener.start(msg.seconds, msg.delay_s);
  else if (msg.seconds === 0) Listener.stop(msg.reason);
}

const LISTEN_TEXT = {
  listening: "Listening…",
  sent: "Heard:",
  timeout: "Not listening. Tap Listen to speak.",
  stopped: "Not listening. Tap Listen to speak.",
  speaking: "Mic off while the reply plays.",
  paused: "Mic off (paused).",
  denied: "Microphone blocked: allow it in Chrome's address bar, or type below.",
  nomic: "No microphone found: plug one in, or type below.",
  offline: "Speech recognition needs internet: type below instead.",
  unsupported: "No speech recognition in this browser (use Chrome): type below.",
  error: "Speech recognition failed: tap Listen to try again, or type below.",
};

Listener.onLine((text) => send({ type: "partner_text", text, source: "stt" }));
Listener.onUpdate((state, text) => {
  const line = $("listen-status");
  line.textContent = `${LISTEN_TEXT[state] || ""}${text ? ` “${text}”` : ""}`;
  line.classList.toggle("on", state === "listening");
  $("ptt").textContent = state === "listening" ? "Listening…" : "Listen";
});

function addLine(who, text) {
  const li = document.createElement("li");
  li.className = who;
  li.innerHTML = `<span class="who"></span>`;
  li.querySelector(".who").textContent = who === "partner" ? "Partner" : "User";
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
    [s.llm_ok ? "ok" : "", s.llm_ok ? "LLM OK" : "LLM OFF"],
  ];
  if (s.fake_decoder) badges.push(["flag", "FAKE DECODER"]);
  if (s.keyboard_mode) badges.push(["flag", "KEYBOARD"]);
  $("badges").replaceChildren(...badges.map(([cls, text]) => {
    const b = document.createElement("span");
    b.className = `badge ${cls}`;
    b.textContent = text;
    return b;
  }));
  $("decoder-message").textContent = s.decoder_message || "";
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

$("accept").addEventListener("click", () => {
  accepted = true;
  $("warning").hidden = true;
  send({ type: "resume" });
});
$("stop").addEventListener("click", () => {
  if (!send({ type: "stop" })) $("state").textContent = "Not connected: press Esc on the display";
});
$("resume").addEventListener("click", () => { if (accepted) send({ type: "resume" }); });
$("take-over").addEventListener("click", connect);
$("ptt").addEventListener("click", () => {
  if (Listener.listening) Listener.stop("stopped");
  else if (!speaking) Listener.start(listenWindow);
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
  })
  .catch(() => {});
setJudgment("");
setConnected(false);
connect();
