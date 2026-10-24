// BrainSwipe browser mock: pure helpers (no DOM, no network), imported by app.js and
// tested with `node --test tests/web/`. Everything here takes plain values and returns
// plain values so the page logic can be checked without a browser.

export const PASS = 0;
export const LIKE = 1;
export const ABSTAIN = -1;
export const GAZES = Object.freeze(["auto", "card", "pass", "like"]);
export const SIDES = Object.freeze(["pass", "like"]);   // index = target
export const ICONS = Object.freeze(["✕", "♥"]);   // ✕ Pass, ♥ Like

/** Fallbacks used before /api/state arrives (or when the server is missing). */
export const DEFAULT_STATE = Object.freeze({
  name: "BrainSwipe",
  decoder: true,
  labels: ["Pass", "Like"],
  freqs: [15.0, 17.0],
  colors: ["#d0605a", "#5aae6e"],
  votes: 4,
  window_s: 1.5,
  step_s: 0.25,
  cooldown_s: 0.5,
  switch_s: 8,
  gaze: "card",
  card: 0,
  n_cards: 0,
  deck: [],
  counts: { swipes: 0, likes: 0, passes: 0, matches: 0, stale: 0 },
  started: 0,
});

// the full-screen stimulus moves its cue box onto a square after this many fresh windows
export const CUE_STREAK = 2;
const COUNT_KEYS = ["swipes", "likes", "passes", "matches", "stale"];

function finite(x, fallback) {
  const v = Number(x);
  return Number.isFinite(v) ? v : fallback;
}

function nonNegInt(x, fallback = 0) {
  const v = Math.floor(finite(x, fallback));
  return v >= 0 ? v : fallback;
}

/** Counts with every key present as a non-negative integer. */
export function normalizeCounts(counts) {
  const c = counts && typeof counts === "object" ? counts : {};
  const out = {};
  for (const k of COUNT_KEYS) out[k] = nonNegInt(c[k], 0);
  return out;
}

/**
 * A /api/state (or `event: state`) payload with defaults filled in and types checked, so
 * a partial or malformed reply never breaks the page.
 */
export function normalizeState(raw) {
  const s = raw && typeof raw === "object" ? raw : {};
  const d = DEFAULT_STATE;
  const pair = (v, fb, conv) =>
    Array.isArray(v) && v.length === 2 ? v.map((x, i) => conv(x, fb[i])) : [...fb];
  const deck = Array.isArray(s.deck)
    ? s.deck.map((p) => ({
        name: String(p && p.name != null ? p.name : "?"),
        age: p && p.age != null ? p.age : "",
        detail: p && p.detail ? String(p.detail) : "",
        color: p && typeof p.color === "string" ? p.color : "#888888",
      }))
    : [];
  const nCards = nonNegInt(s.n_cards, deck.length);
  return {
    name: typeof s.name === "string" && s.name ? s.name : d.name,
    decoder: s.decoder === undefined ? d.decoder : Boolean(s.decoder),
    labels: pair(s.labels, d.labels, (x, fb) => (typeof x === "string" && x ? x : fb)),
    freqs: pair(s.freqs, d.freqs, (x, fb) => finite(x, fb)),
    colors: pair(s.colors, d.colors, (x, fb) => (isHexColor(x) ? x : fb)),
    votes: Math.max(1, nonNegInt(s.votes, d.votes)),
    window_s: finite(s.window_s, d.window_s),
    step_s: finite(s.step_s, d.step_s),
    cooldown_s: finite(s.cooldown_s, d.cooldown_s),
    switch_s: finite(s.switch_s, d.switch_s),
    gaze: GAZES.includes(s.gaze) ? s.gaze : d.gaze,
    card: nonNegInt(s.card, 0),
    n_cards: nCards,
    deck,
    counts: normalizeCounts(s.counts),
    started: finite(s.started, 0),
  };
}

export function isHexColor(x) {
  return typeof x === "string" && /^#[0-9a-fA-F]{6}$/.test(x);
}

/** "#d0605a", 0.25 -> "rgba(208, 96, 90, 0.25)" (for tinted fills and glows). */
export function hexToRgba(hex, alpha) {
  if (!isHexColor(hex)) return `rgba(128, 128, 128, ${alpha})`;
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

/** Fraction of the way to a swipe: streak / votes, clamped to [0, 1]. */
export function progress(streak, votes) {
  const v = finite(votes, 0);
  const s = finite(streak, 0);
  if (v <= 0 || s <= 0) return 0;
  return Math.min(1, s / v);
}

/**
 * How full each square's streak meter is after one `event: window`: the pending
 * target's square shows streak / votes, the other one is empty. Stale windows are
 * ignored by the Swiper, so both squares show "settling" and no progress.
 */
export function squareStates(win, votes) {
  const out = [
    { fill: 0, steps: 0, settling: false, cue: false },
    { fill: 0, steps: 0, settling: false, cue: false },
  ];
  if (!win || typeof win !== "object") return out;
  const v = Math.max(1, nonNegInt(win.votes, votes || 1));
  if (win.stale) {
    for (const s of out) s.settling = true;
    return out;
  }
  const p = Number(win.pending);
  if (p === PASS || p === LIKE) {
    const steps = Math.min(v, nonNegInt(win.streak, 0));
    out[p].steps = steps;
    out[p].fill = progress(steps, v);
    out[p].cue = steps >= CUE_STREAK;
  }
  return out;
}

/**
 * Gaze from the arrow keys held, in the order they were pressed: the most recently
 * pressed one wins (holding ← then also → looks right). null when no arrow is held.
 */
export function gazeFromKeys(heldKeys) {
  const keys = Array.from(heldKeys || []);
  for (let i = keys.length - 1; i >= 0; i--) {
    if (keys[i] === "ArrowLeft") return "pass";
    if (keys[i] === "ArrowRight") return "like";
  }
  return null;
}

/**
 * The square the pointer is "looking" at after one pointer event on `side` ("pass" or
 * "like"), given the square it was on before. Mouse: enter/leave, and any move over a
 * square (after the window loses and regains focus with the mouse resting on a square,
 * no pointerenter comes; a move is the only sign it is there). Touch and pen have no
 * hover, so press-and-hold looks and lifting the finger stops.
 */
export function nextGazeOnPointer(type, side, current = null) {
  if (!SIDES.includes(side)) return current;
  switch (type) {
    case "pointerenter":
    case "pointerdown":
    case "pointermove":
      return side;
    case "pointerleave":
    case "pointerup":
    case "pointercancel":
      return current === side ? null : current;
    default:
      return current;
  }
}

/**
 * The gaze to send to the server: a held key beats the pointer, which beats the Auto
 * schedule; with none of them the presenter is reading the card.
 */
export function decideGaze({ auto = false, keys = [], hover = null } = {}) {
  const k = gazeFromKeys(keys);
  if (k) return k;
  if (SIDES.includes(hover)) return hover;
  return auto ? "auto" : "card";
}

/** Shift+Arrow (or an arrow with no decoder) means an instant swipe; else null. */
export function instantSwipeTarget(key, shiftKey, decoder) {
  if (key !== "ArrowLeft" && key !== "ArrowRight") return null;
  if (!shiftKey && decoder) return null;
  return key === "ArrowLeft" ? PASS : LIKE;
}

/** Which way the old card leaves: left for Pass, right for Like. */
export function cardSlideDirection(target) {
  return Number(target) === LIKE ? "right" : "left";
}

/** "left"/"right" -> CSS class pair for the leaving and entering card. */
export function slideClasses(target, reducedMotion) {
  if (reducedMotion) return { out: "card--fade-out", in: "card--fade-in" };
  const dir = cardSlideDirection(target);
  return { out: `card--out-${dir}`, in: "card--in" };
}

/** Every class that animates a card in (slide or crossfade). */
export const CARD_IN_CLASSES = Object.freeze(["card--in", "card--fade-in"]);

/**
 * Class changes that send the card on screen away toward `target`'s side. A card can
 * still be arriving (a second swipe within the entry animation); its entry class must go,
 * or it keeps playing the entry animation and covers the new card instead of leaving.
 */
export function leavingClasses(target, reducedMotion) {
  return { remove: [...CARD_IN_CLASSES], add: ["is-leaving", slideClasses(target, reducedMotion).out] };
}

/**
 * Whether a fresh `event: state` comes from a restarted server: its start time changed,
 * or it has counted fewer swipes than the page saw. Rates measured against the old run
 * must start over then.
 */
export function serverRestarted(prev, next) {
  if (!prev || !next) return false;
  const a = finite(prev.started, 0);
  const b = finite(next.started, 0);
  if (a > 0 && b > 0 && a !== b) return true;
  const prevSwipes = nonNegInt(prev.counts && prev.counts.swipes, 0);
  const nextSwipes = nonNegInt(next.counts && next.counts.swipes, 0);
  return nextSwipes < prevSwipes;
}

/**
 * Swipes per minute since the first swipe this page saw: the swipes after it over the
 * time since it, so steady swiping every 3 s reads 20.0 however long the page has been
 * open. `firstCount` is counts.swipes at that first swipe (1 unless the page was opened
 * mid-run). null until there are two swipes to measure between.
 */
export function swipesPerMinute(count, firstSwipeT, nowT, firstCount = 1) {
  const n = nonNegInt(count, 0) - nonNegInt(firstCount, 1);
  const first = Number(firstSwipeT);
  const now = Number(nowT);
  if (n < 1 || !Number.isFinite(first) || !Number.isFinite(now)) return null;
  const elapsed = now - first;
  if (!(elapsed > 0)) return null;
  return (n * 60) / elapsed;
}

/** One decimal, "–" for null: what the stats bar shows for the rate. */
export function formatRate(rate) {
  return rate == null || !Number.isFinite(rate) ? "–" : rate.toFixed(1);
}

/** The stats bar items, in display order. */
export function formatCounts(counts, rate = null) {
  const c = normalizeCounts(counts);
  return [
    { key: "swipes", label: "Swipes", value: String(c.swipes) },
    { key: "rate", label: "Per min", value: formatRate(rate) },
    { key: "likes", label: "Likes", value: String(c.likes) },
    { key: "passes", label: "Passes", value: String(c.passes) },
    { key: "matches", label: "Matches", value: String(c.matches) },
    { key: "stale", label: "Stale ignored", value: String(c.stale) },
  ];
}

/**
 * Bar widths (0..1) for the live Pass/Like score readout. Scores are baseline-corrected
 * (in baseline spreads), so they can be negative; bars start at 0 and share one scale
 * that never drops below `floor`, so noise near zero stays visibly small.
 */
export function scoreBars(scores, floor = 4) {
  const s = Array.isArray(scores) ? scores.map((x) => finite(x, 0)) : [0, 0];
  while (s.length < 2) s.push(0);
  const scale = Math.max(floor, ...s.map(Math.abs));
  return s.slice(0, 2).map((x) => Math.max(0, Math.min(1, x / scale)));
}

/** Label for a target index (-1 or anything else: the fallback). */
export function targetLabel(target, labels = DEFAULT_STATE.labels, none = "card") {
  const t = Number(target);
  return t === PASS || t === LIKE ? labels[t] : none;
}

/** What the gaze chip says for a server gaze value. */
export function gazeLabel(gaze, labels = DEFAULT_STATE.labels) {
  switch (gaze) {
    case "pass": return `looking at ${labels[PASS]}`;
    case "like": return `looking at ${labels[LIKE]}`;
    case "auto": return "auto schedule";
    default: return "reading the card";
  }
}

/**
 * Whether a swipe payload is new. A key swipe arrives twice (the POST reply and the
 * broadcast `event: swipe`), and counts.swipes numbers swipes, so only a higher count
 * is applied.
 */
export function isNewSwipe(lastSwipes, payload) {
  if (!payload || !payload.counts) return false;
  const n = Number(payload.counts.swipes);
  return Number.isFinite(n) && n > lastSwipes;
}

/** Text for the aria-live region after a swipe. */
export function swipeAnnouncement(payload, deck = []) {
  if (!payload) return "";
  const next = deck[payload.card];
  const said = payload.voice || payload.label || "";
  const nextText = next ? ` Next: ${next.name}, ${next.age}.` : "";
  return `${said}.${nextText}`.replace(/\.\./g, ".").replace(/!\./g, "!");
}

/** Accessible name for card image k. */
export function cardAlt(profile) {
  if (!profile) return "Profile card";
  const bits = [`${profile.name}, ${profile.age}`];
  if (profile.detail) bits.push(profile.detail);
  return `Profile card: ${bits.join(". ")}`;
}

/** Path of the PNG for image k. */
export function cardUrl(k) {
  return `/cards/${nonNegInt(k, 0)}.png`;
}

/** Reconnect delay after `attempt` failures: 0.5 s doubling to a 5 s cap. */
export function reconnectDelay(attempt) {
  const a = Math.max(0, nonNegInt(attempt, 0));
  return Math.min(5000, 500 * 2 ** Math.min(a, 10));
}

/**
 * Sizes (px) for the stage. Wide windows follow the stimulus's proportions in units of
 * the stage height H: squares 0.4 H, centres at least 1.0 H apart, so 1.4 H of width is
 * needed (H shrinks when the window is narrower). The 2:3 card fills the gap between the
 * squares' inner edges; when the window has the width, the centres move further apart
 * so the card can be as tall as the stage (at 1.0 H it would be about 0.6 H wide and
 * unreadable on a laptop or projector). The mock does not flicker, so the exact spacing
 * matters less than a card judges can read. Narrow or portrait windows stack: card on
 * top, the two squares side by side below, everything within the width (the page may
 * scroll vertically).
 */
export function clampLayout(viewportW, viewportH, opts = {}) {
  const {
    top = 76,      // header
    bottom = 132,  // stats bar and legend
    gutter = 16,
    minWide = 760,
    cardMargin = 28,   // card to square, each side
    labelRoom = 64,    // label and caption under the squares
  } = opts;
  const vw = Math.max(240, finite(viewportW, 1024));
  const vh = Math.max(320, finite(viewportH, 768));
  const wide = vw >= minWide && vw >= vh * 1.05;
  if (wide) {
    const stageH = Math.max(200, vh - top - bottom);
    const unit = Math.min(stageH - labelRoom * 0.5, (vw - 2 * gutter) / 1.4);
    const square = Math.max(80, Math.round(0.4 * unit));
    const roomy = square + 2 * cardMargin + Math.floor((stageH - 8) / 1.5);   // a full-height card
    const widest = Math.floor(vw - 2 * gutter - square);   // pitch + square fits the width
    const pitch = Math.max(square + 2 * cardMargin + 120, Math.round(1.0 * unit),
                           Math.min(roomy, widest));
    const cardW0 = pitch - square - 2 * cardMargin;
    const cardH = Math.round(Math.min(stageH - 8, cardW0 * 1.5));
    const cardW = Math.round(cardH / 1.5);
    return { mode: "wide", square, pitch, cardW, cardH, stageH: Math.round(stageH) };
  }
  const inner = vw - 2 * gutter;
  const square = Math.round(Math.max(64, Math.min((inner - 32) / 2, vh * 0.18, 200)));
  const pitch = Math.round(Math.min(inner - square, square * 2));
  const room = vh - top - square - labelRoom - 24;
  const cardH = Math.round(Math.max(240, Math.min(inner * 1.5, room, 720)));
  const cardW = Math.round(cardH / 1.5);
  return { mode: "stack", square, pitch, cardW, cardH, stageH: cardH + square + labelRoom + 24 };
}
