// BrainSwipe browser mock: DOM, network and speech. The decisions (gaze, progress,
// layout, rates) live in logic.js as pure functions; this file only wires them to the
// page and to the server's API (/api/state, /api/events, /api/gaze, /api/swipe).
//
// Nothing here makes anything flicker: the squares are static and only their streak
// meters and outlines change, a few times a second at most.

import * as L from "./logic.js";

const $ = (id) => document.getElementById(id);
const body = document.body;
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const FLASH_MS = 350;      // swipe.toml [card] flash_s
const MATCH_MS = 2200;
const MUTE_KEY = "brainswipe.muted";
const NOTE_EEG = "Browser mock: simulated EEG board, no flicker. The live demo uses the full-screen stimulus.";
const NOTE_KEYS = "Browser mock in keyboard mode: no EEG, no flicker. The live demo uses the full-screen stimulus.";

const ui = {
  state: L.normalizeState(null),
  conn: "connecting",
  auto: false,
  hover: null,          // "pass" | "like" | null (pointer)
  keys: [],             // arrow keys held, in press order
  sentGaze: null,       // last gaze the server acknowledged (or we asked for)
  gazeChain: Promise.resolve(),
  muted: loadMuted(),
  voice: null,          // the Audio playing the latest voice line (Cartesia, via the server)
  lastSwipes: 0,
  firstSwipeT: null,    // performance clock (s) of the first swipe this page saw
  firstCount: 1,
  lastWindow: null,
  shownCard: null,
  preloaded: new Set(),
  flashTimers: [null, null],
  matchTimer: null,
};

const squares = [$("square-0"), $("square-1")];

// ---------------------------------------------------------------- persistence

function loadMuted() {
  try { return window.localStorage.getItem(MUTE_KEY) === "1"; } catch { return false; }
}

function saveMuted(muted) {
  try { window.localStorage.setItem(MUTE_KEY, muted ? "1" : "0"); } catch { /* private mode */ }
}

// ---------------------------------------------------------------- layout

function applyLayout() {
  const top = $("top").offsetHeight;
  const bottom = $("bottom").offsetHeight;
  const vw = document.documentElement.clientWidth;
  const lay = L.clampLayout(vw, window.innerHeight, { top, bottom });
  body.dataset.layout = lay.mode;
  const r = document.documentElement.style;
  r.setProperty("--sq", `${lay.square}px`);
  r.setProperty("--pitch", `${lay.pitch}px`);
  r.setProperty("--card-w", `${lay.cardW}px`);
  r.setProperty("--card-h", `${lay.cardH}px`);
  r.setProperty("--stage-h", `${lay.stageH}px`);
}

// ---------------------------------------------------------------- rendering

function setConn(conn) {
  ui.conn = conn;
  body.dataset.conn = conn;
  const text = conn === "live"
    ? (ui.state.decoder ? "live · simulated EEG" : "live · keyboard mode (no decoder)")
    : conn === "offline" ? "server not reachable, retrying…" : "connecting…";
  $("conn-text").textContent = text;
}

function renderPips() {
  const v = ui.state.votes;
  for (const sq of squares) {
    const box = sq.querySelector(".square__pips");
    if (box.childElementCount === v) continue;
    box.replaceChildren(...Array.from({ length: v }, () => {
      const pip = document.createElement("span");
      pip.className = "pip";
      return pip;
    }));
  }
}

function renderStatic() {
  const s = ui.state;
  const root = document.documentElement.style;
  root.setProperty("--pass", s.colors[L.PASS]);
  root.setProperty("--like", s.colors[L.LIKE]);
  body.dataset.decoder = String(s.decoder);
  // --no-decoder runs no simulated board, so the note must not claim one
  $("note").textContent = s.decoder ? NOTE_EEG : NOTE_KEYS;
  for (const t of [L.PASS, L.LIKE]) {
    $(`label-text-${t}`).textContent = s.labels[t];
    $(`label-${t}`).setAttribute("aria-label", `Swipe ${s.labels[t]} now`);
    $(`freq-${t}`).textContent = `${formatHz(s.freqs[t])} Hz`;
    $(`bar-name-${t}`).textContent = s.labels[t];
    squares[t].setAttribute("aria-label",
      `${s.labels[t]} square, ${formatHz(s.freqs[t])} Hz (static, no flicker)`);
  }
  $("btn-auto").disabled = !s.decoder;
  renderPips();
}

function formatHz(f) {
  return Number.isInteger(f) ? String(f) : f.toFixed(1);
}

function renderCounts(counts, bumped = []) {
  const rate = ui.firstSwipeT == null ? null
    : L.swipesPerMinute(counts.swipes, ui.firstSwipeT, performance.now() / 1000, ui.firstCount);
  for (const item of L.formatCounts(counts, rate)) {
    const el = $(`stat-${item.key}`);
    if (el && el.textContent !== item.value) el.textContent = item.value;
    if (bumped.includes(item.key)) bump(el.parentElement);
  }
}

function bump(el) {
  if (!el) return;
  el.classList.remove("is-bumped");
  void el.offsetWidth;   // restart the animation
  el.classList.add("is-bumped");
}

function renderGaze() {
  const g = ui.state.gaze;
  $("your-gaze").textContent = L.gazeLabel(g, ui.state.labels);
  const truth = ui.lastWindow ? Number(ui.lastWindow.true) : L.ABSTAIN;
  for (const t of [L.PASS, L.LIKE]) {
    const looking = g === L.SIDES[t] || (g === "auto" && truth === t);
    squares[t].classList.toggle("is-gazed", ui.state.decoder && looking);
  }
  $("btn-auto").setAttribute("aria-pressed", String(ui.auto));
}

function renderWindow(win) {
  const states = L.squareStates(win, ui.state.votes);
  for (const t of [L.PASS, L.LIKE]) {
    const sq = squares[t];
    const st = states[t];
    sq.style.setProperty("--fill", String(st.fill));
    sq.classList.toggle("is-settling", st.settling);
    sq.classList.toggle("is-cue", st.cue);
    sq.querySelectorAll(".pip").forEach((pip, i) => pip.classList.toggle("is-on", i < st.steps));
  }
  if (!win) return;
  const bars = L.scoreBars(win.scores);
  for (const t of [L.PASS, L.LIKE]) {
    $(`bar-${t}`).style.setProperty("--w", String(bars[t]));
    const v = Array.isArray(win.scores) ? Number(win.scores[t]) : NaN;
    $(`bar-val-${t}`).textContent = Number.isFinite(v) ? v.toFixed(2) : "–";
  }
  $("true-gaze").textContent = L.targetLabel(win.true, ui.state.labels, "card (no tone)");
  const p = Number(win.pending);
  $("swiper-state").textContent = win.stale ? "settling (cooldown)"
    : p === L.PASS || p === L.LIKE ? `${ui.state.labels[p]} ${win.streak}/${win.votes}`
    : "no streak";
}

// ---------------------------------------------------------------- cards

function makeCard(k) {
  const profile = ui.state.deck[k];
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.image = String(k);
  const img = document.createElement("img");
  img.src = L.cardUrl(k);
  img.alt = L.cardAlt(profile);
  img.width = 600;
  img.height = 900;
  img.decoding = "async";
  img.draggable = false;
  card.append(img);
  for (const t of [L.PASS, L.LIKE]) {
    const stamp = document.createElement("span");
    stamp.className = `card__stamp card__stamp--${L.SIDES[t]}`;
    stamp.setAttribute("aria-hidden", "true");
    stamp.textContent = ui.state.labels[t];
    card.append(stamp);
  }
  return card;
}

function preloadCards() {
  for (let k = 0; k < ui.state.n_cards; k++) {
    if (ui.preloaded.has(k)) continue;
    ui.preloaded.add(k);
    const img = new Image();
    img.decoding = "async";
    img.src = L.cardUrl(k);
  }
}

/** Show image k at once (first load, reconnect). */
function showCard(k) {
  if (ui.shownCard === k && $("cards").querySelector(".card")) return;
  const box = $("cards");
  box.querySelectorAll(".card").forEach((c) => c.remove());
  box.append(makeCard(k));
  ui.shownCard = k;
  $("placeholder").hidden = true;
}

/** Swipe the card on screen away toward `target`'s side and bring image k in. */
function swipeCard(target, k) {
  const box = $("cards");
  // a swipe during the previous animation: drop whatever is still leaving
  box.querySelectorAll(".card.is-leaving").forEach((c) => c.remove());
  const old = box.querySelector(".card");
  const next = makeCard(k);
  const cls = L.slideClasses(target, reducedMotion.matches);
  next.classList.add(cls.in);
  box.prepend(next);
  ui.shownCard = k;
  $("placeholder").hidden = true;
  if (!old) return;
  // `old` may still be arriving from the previous swipe: drop its entry class first
  const change = L.leavingClasses(target, reducedMotion.matches);
  old.classList.remove(...change.remove);
  old.classList.add(...change.add);
  old.setAttribute("aria-hidden", "true");   // screen readers get only the new card
  if (!reducedMotion.matches) old.classList.add("has-stamp");
  old.querySelectorAll(`.card__stamp:not(.card__stamp--${L.SIDES[target]})`).forEach((s) => s.remove());
  const done = () => old.remove();
  old.addEventListener("animationend", done, { once: true });
  window.setTimeout(done, 700);   // in case animationend never comes (hidden tab)
  next.addEventListener("animationend", () => next.classList.remove(cls.in), { once: true });
}

function flashSquare(target) {
  const sq = squares[target];
  if (!sq) return;
  window.clearTimeout(ui.flashTimers[target]);
  sq.classList.remove("is-flash");
  void sq.offsetWidth;
  sq.classList.add("is-flash");
  ui.flashTimers[target] = window.setTimeout(() => sq.classList.remove("is-flash"), FLASH_MS);
}

function showMatch(name) {
  const m = $("match");
  $("match-name").textContent = `${name} liked you back`;
  m.classList.add("is-shown");
  window.clearTimeout(ui.matchTimer);
  ui.matchTimer = window.setTimeout(() => m.classList.remove("is-shown"), MATCH_MS);
}

// ---------------------------------------------------------------- speech

function say(text) {
  // Cartesia, through the server (/api/speech keeps the key off the page). The newest
  // swipe wins: a line still playing stops, so speech never queues a backlog.
  if (ui.muted || !text) return;
  try {
    if (ui.voice) ui.voice.pause();
    ui.voice = new Audio(`/api/speech?text=${encodeURIComponent(text)}`);
    ui.voice.play().catch(() => { /* no voice: the banner and live region still say it */ });
  } catch { /* no audio element: the banner and live region still say it */ }
}

function renderMute() {
  const b = $("btn-mute");
  b.setAttribute("aria-pressed", String(ui.muted));
  $("mute-text").textContent = ui.muted ? "Muted" : "Mute";
  updateVoiceHint();
}

function updateVoiceHint() {
  const act = navigator.userActivation;
  const needs = !ui.muted && act && !act.hasBeenActive;   // audio waits for a first gesture
  $("voice-hint").hidden = !needs;
}

// ---------------------------------------------------------------- server events

/**
 * Take a full state. `fresh` is true for the `event: state` that opens each event
 * stream, which is authoritative (a restarted server counts from 0 again). A fetched
 * /api/state can arrive after a swipe event that is newer than it, so it is dropped
 * when it has fewer swipes than the page has already shown.
 */
function applyState(raw, fresh = false) {
  const s = L.normalizeState(raw);
  if (!fresh && s.counts.swipes < ui.lastSwipes) return;
  if (fresh && L.serverRestarted({ started: ui.state.started, counts: { swipes: ui.lastSwipes } }, s)) {
    // the rate is measured from this run's first swipe, not the old server's
    ui.firstSwipeT = null;
    ui.firstCount = 1;
  }
  const prevCard = ui.shownCard;
  ui.state = s;
  if (s.gaze === "auto") ui.auto = true;
  ui.sentGaze = s.gaze;
  ui.lastSwipes = s.counts.swipes;
  renderStatic();
  if (s.n_cards > 0 && (prevCard !== s.card || !$("cards").querySelector(".card"))) showCard(s.card);
  preloadCards();
  renderCounts(s.counts);
  renderGaze();
  renderWindow(null);
  applyLayout();
  syncGaze();   // a restarted server forgets our gaze
}

function onWindow(win) {
  ui.lastWindow = win;
  if (L.GAZES.includes(win.gaze) && win.gaze !== ui.state.gaze) ui.state.gaze = win.gaze;
  renderWindow(win);
  renderGaze();
  if (win.stale === true) {
    // counts.stale only arrives with state and swipes; count along in between (the next
    // swipe's counts replace this estimate)
    ui.state.counts.stale += 1;
    renderCounts(ui.state.counts);
  }
}

function onSwipe(p) {
  if (!L.isNewSwipe(ui.lastSwipes, p)) return;
  const counts = L.normalizeCounts(p.counts);
  if (ui.firstSwipeT == null) {
    ui.firstSwipeT = performance.now() / 1000;
    ui.firstCount = counts.swipes;
  }
  const before = ui.state.counts;
  const bumped = Object.keys(counts).filter((k) => counts[k] !== before[k]);
  if (bumped.includes("swipes")) bumped.push("rate");
  ui.lastSwipes = counts.swipes;
  ui.state.counts = counts;
  ui.state.card = Number(p.card);
  const target = Number(p.target) === L.LIKE ? L.LIKE : L.PASS;
  flashSquare(target);
  swipeCard(target, ui.state.card);
  renderWindow(null);   // the Swiper restarts its streak on every card change
  renderCounts(counts, bumped);
  say(p.voice);
  $("live").textContent = L.swipeAnnouncement(p, ui.state.deck);
  if (p.match) {
    const who = ui.state.deck[p.swiped];
    showMatch(who ? who.name : "Someone");
  }
}

function onGaze(p) {
  if (!p || !L.GAZES.includes(p.gaze)) return;
  ui.state.gaze = p.gaze;
  renderGaze();
}

// ---------------------------------------------------------------- connection

let source = null;
let attempt = 0;
let retryTimer = null;

function handler(fn) {
  return (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    try { fn(data); } catch (err) { console.error("BrainSwipe:", err); }
  };
}

async function fetchState() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    if (!r.ok) return false;
    applyState(await r.json());
    return true;
  } catch {
    return false;
  }
}

function connect() {
  window.clearTimeout(retryTimer);
  if (source) source.close();
  if (ui.conn !== "live") setConn(attempt > 2 ? "offline" : "connecting");
  fetchState();
  let es;
  try {
    es = new EventSource("/api/events");
  } catch {
    scheduleReconnect();
    return;
  }
  source = es;
  es.addEventListener("open", () => { attempt = 0; setConn("live"); });
  es.addEventListener("state", handler((d) => { applyState(d, true); setConn("live"); }));
  es.addEventListener("window", handler(onWindow));
  es.addEventListener("swipe", handler(onSwipe));
  es.addEventListener("gaze", handler(onGaze));
  es.addEventListener("error", () => {
    // reconnect ourselves (and re-fetch the state) rather than rely on the browser's
    // retry, which gives up for good on a 404 or a wrong content type
    if (source !== es) return;
    es.close();
    source = null;
    setConn(attempt >= 2 ? "offline" : "connecting");
    scheduleReconnect();
  });
}

function scheduleReconnect() {
  window.clearTimeout(retryTimer);
  retryTimer = window.setTimeout(connect, L.reconnectDelay(attempt++));
}

async function postJson(path, payload) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    cache: "no-store",
  });
  let data = null;
  try { data = await r.json(); } catch { /* empty or not JSON */ }
  return { ok: r.ok, status: r.status, data };
}

/** Send the gaze the inputs now imply, if it differs from the last one sent. */
function syncGaze() {
  if (!ui.state.decoder || ui.conn === "offline") return;
  const want = L.decideGaze({ auto: ui.auto, keys: ui.keys, hover: ui.hover });
  if (want === ui.sentGaze) return;
  ui.sentGaze = want;
  ui.state.gaze = want;   // show it at once; the server's `event: gaze` confirms it
  renderGaze();
  // one request at a time, in order, so a quick hover-out never lands before its hover-in
  ui.gazeChain = ui.gazeChain.then(async () => {
    try {
      const res = await postJson("/api/gaze", { gaze: want });
      if (!res.ok && ui.sentGaze === want) ui.sentGaze = null;   // retry on the next change
    } catch {
      if (ui.sentGaze === want) ui.sentGaze = null;
    }
  });
}

async function swipeNow(target) {
  // name the card on screen: if the decoder swiped it a moment ago, the server answers
  // 409 rather than swipe the next card, which nobody has seen (its swipe event is on
  // the way and moves the page on)
  const payload = ui.shownCard == null ? { target } : { target, card: ui.shownCard };
  try {
    const res = await postJson("/api/swipe", payload);
    if (res.ok && res.data) onSwipe(res.data);
  } catch { /* offline: the status dot already says so */ }
}

// ---------------------------------------------------------------- input

function onKeyDown(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const t = L.instantSwipeTarget(e.key, e.shiftKey, ui.state.decoder);
  if (t != null) {
    e.preventDefault();
    if (!e.repeat) swipeNow(t);
    return;
  }
  if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    e.preventDefault();
    if (e.repeat) return;
    ui.keys = ui.keys.filter((k) => k !== e.key).concat(e.key);
    syncGaze();
    return;
  }
  if (e.repeat || e.shiftKey) return;
  const key = e.key.toLowerCase();
  if (key === "a" && ui.state.decoder) toggleAuto();
  else if (key === "m") toggleMute();
}

function onKeyUp(e) {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") {
    updateVoiceHint();
    return;
  }
  ui.keys = ui.keys.filter((k) => k !== e.key);
  syncGaze();
}

function releaseAll() {
  ui.keys = [];
  ui.hover = null;
  syncGaze();
}

function onPointer(e) {
  const side = e.currentTarget.dataset.side;
  if (!ui.state.decoder) {
    if (e.type === "pointerup" && e.button === 0) swipeNow(L.SIDES.indexOf(side));
    return;
  }
  // mouse hovers; touch and pen have no hover, so they press and hold
  const isMouse = e.pointerType === "mouse";
  if (isMouse && (e.type === "pointerdown" || e.type === "pointerup")) return;
  if (!isMouse && (e.type === "pointerenter" || e.type === "pointermove")) return;
  const hover = L.nextGazeOnPointer(e.type, side, ui.hover);
  if (e.type === "pointermove" && hover === ui.hover) return;   // the common case: no news
  ui.hover = hover;
  syncGaze();
}

/** Focus is back: a mouse resting on a square looks at it again (no pointerenter comes). */
function onFocus() {
  if (!ui.state.decoder || ui.hover) return;
  const t = squares.findIndex((sq) => sq.matches(":hover"));
  if (t < 0) return;
  ui.hover = L.SIDES[t];
  syncGaze();
}

function toggleAuto() {
  ui.auto = !ui.auto;
  renderGaze();
  syncGaze();
}

function toggleMute() {
  ui.muted = !ui.muted;
  saveMuted(ui.muted);
  if (ui.muted && ui.voice) ui.voice.pause();
  renderMute();
}

function bindInputs() {
  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("keyup", onKeyUp);
  window.addEventListener("blur", releaseAll);
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", () => { if (document.hidden) releaseAll(); });
  for (const type of ["pointerdown", "click"]) {
    window.addEventListener(type, updateVoiceHint, { capture: true });
  }
  for (const sq of squares) {
    for (const type of ["pointerenter", "pointermove", "pointerleave", "pointerdown", "pointerup",
      "pointercancel"]) {
      sq.addEventListener(type, onPointer);
    }
    sq.addEventListener("contextmenu", (e) => e.preventDefault());
  }
  for (const t of [L.PASS, L.LIKE]) $(`label-${t}`).addEventListener("click", () => swipeNow(t));
  $("btn-auto").addEventListener("click", toggleAuto);
  $("btn-mute").addEventListener("click", toggleMute);
  let pending = 0;
  const relayout = () => {
    cancelAnimationFrame(pending);
    pending = requestAnimationFrame(applyLayout);
  };
  window.addEventListener("resize", relayout);
  // the header and footer change height as their text wraps; the stage gets the rest
  if ("ResizeObserver" in window) {
    const ro = new ResizeObserver(relayout);
    ro.observe($("top"));
    ro.observe($("bottom"));
  }
}

// ---------------------------------------------------------------- start

function start() {
  renderStatic();
  renderMute();
  renderCounts(ui.state.counts);
  renderGaze();
  renderWindow(null);
  setConn("connecting");
  applyLayout();
  bindInputs();
  connect();
  // the rate decays while nobody swipes, so refresh it every second
  window.setInterval(() => renderCounts(ui.state.counts), 1000);
}

start();
