// Partner page: renders what the server says and reports events back. All state is server-side.
"use strict";

const $ = (id) => document.getElementById(id);
const RECONNECT_MS = 1000;

let ws = null;
let shown = null;        // last show_option
let accepted = false;    // flicker warning accepted
let keyboardMode = false;

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/ui`);
  ws.onopen = () => { if (accepted) send({ type: "resume" }); };
  ws.onmessage = (ev) => handle(JSON.parse(ev.data));
  ws.onclose = () => {
    $("state").textContent = "Server disconnected, reconnecting…";
    setTimeout(connect, RECONNECT_MS);
  };
}

function handle(msg) {
  switch (msg.type) {
    case "show_option": renderOption(msg); break;
    case "trial": renderPhase(msg.phase); break;
    case "highlight": markPicked(msg.slot); break;
    case "speak": speakOut(msg); break;
    case "state": renderState(msg); break;
    case "transcript": addLine(msg.who, msg.text); break;
    case "status": renderStatus(msg); break;
  }
}

function renderOption(msg) {
  shown = msg;
  $("option-count").textContent = `Option ${msg.index + 1} of ${msg.count}`;
  $("option-text").textContent = msg.utterance;
  const targets = $("targets");
  targets.replaceChildren(...msg.slots.map((s, i) => {
    const el = document.createElement("div");
    el.className = `target ${s.role}`;
    el.dataset.slot = s.slot;
    const key = keyboardMode ? ` · key ${i + 1}` : "";
    el.innerHTML = `<span class="role"></span><span class="slot"></span>`;
    el.querySelector(".role").textContent = s.label;
    el.querySelector(".slot").textContent = `${s.slot}${key}`;
    return el;
  }));
}

function renderPhase(phase) {
  const el = $("phase");
  el.textContent = phase === "flicker" ? "flickering" : phase;
  el.className = `phase ${phase}`;
  document.querySelectorAll(".target").forEach((t) => {
    t.classList.toggle("active", phase === "flicker");
    if (phase === "cue") t.classList.remove("picked");
  });
}

function markPicked(slot) {
  document.querySelectorAll(".target").forEach((t) => t.classList.toggle("picked", t.dataset.slot === slot));
}

function renderState(msg) {
  const labels = {
    IDLE: "Waiting to start", BASELINE: "Baseline: keep still", MENU_CUE: "Read the option",
    STIMULATING: "Look at SAY or NEXT", SELECTED: "Selected", SPEAKING: "Speaking",
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
  Speech.speak(msg.text, () => send({ type: "tts_done", utterance_id: msg.utterance_id }));
}

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
  keyboardMode = s.keyboard_mode;
  const badges = [
    [s.decoder_connected ? (s.decoder_ok ? "ok" : "bad") : "bad",
      s.decoder_connected ? (s.decoder_ok ? "DECODER OK" : "DECODER NOT OK") : "NO DECODER"],
    [s.display_ok ? "ok" : "bad", s.display_ok ? "DISPLAY OK" : "DISPLAY ?"],
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
  $("hint").textContent = s.keyboard_mode
    ? "Keyboard mode: 1 = SAY, 2 = NEXT while flickering. C / W mark the last reply correct / wrong."
    : "C / W mark the last spoken reply correct / wrong.";
  if (shown) renderOption(shown);
}

// --- controls -------------------------------------------------------------

$("accept").addEventListener("click", () => {
  accepted = true;
  $("warning").hidden = true;
  send({ type: "resume" });
});
$("stop").addEventListener("click", () => send({ type: "stop" }));
$("resume").addEventListener("click", () => { if (accepted) send({ type: "resume" }); });

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
  if (["1", "2", "c", "w"].includes(key)) send({ type: "key", key });
});

fetch("/api/config")
  .then((r) => r.json())
  .then((c) => { Speech.setRate(c.tts_rate); keyboardMode = c.keyboard_mode; })
  .catch(() => {});
connect();
