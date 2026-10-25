// BrainSwipe browser mock: pure helpers (no DOM, no network), imported by app.js and
// tested with `node --test tests/web/`. Everything here takes plain values and returns
// plain values so the page logic can be checked without a browser.
//
// The mock runs the c-VEP standard (docs/CVEP-STANDARD.md): three targets, Pass (0),
// Super like (1) and Like (2), a decision every code cycle whose scores (correlations,
// not probabilities) fill a confidence bar under each square, and a training stage on
// Pass first. The squares are drawn static white: the mock never flickers.

export const PASS = 0;
export const SUPER = 1;
export const LIKE = 2;
export const ABSTAIN = -1;
export const TARGETS = Object.freeze([PASS, SUPER, LIKE]);
export const GAZES = Object.freeze(["auto", "card", "pass", "super", "like"]);
export const SIDES = Object.freeze(["pass", "super", "like"]);   // index = target
export const ICONS = Object.freeze(["✕", "★", "♥"]);   // ✕ Pass, ★ Super like, ♥ Like
export const BAR_CELLS = 8;   // the standard's text bar (brainsync.vep.live.bar)

/** Fallbacks used before /api/state arrives (or when the server is missing). */
export const DEFAULT_STATE = Object.freeze({
  name: "BrainSwipe",
  decoder: true,
  paradigm: "code",
  labels: ["Pass", "Super like", "Like"],
  colors: ["#d0605a", "#5a9bd0", "#5aae6e"],
  lags: [0, 21, 42],
  cycle_s: 1.05,
  votes: 2,
  margin: 0.2,
  cycles: 2,
  training: { active: false, cycle: 0, of: 30, accuracy: null, chance: 1 / 3, target: PASS },
  cooldown_s: 0.5,
  switch_s: 8,
  gaze: "card",
  card: 0,
  n_cards: 0,
  deck: [],
  counts: { swipes: 0, likes: 0, super_likes: 0, passes: 0, matches: 0, stale: 0 },
  started: 0,
});

const COUNT_KEYS = ["swipes", "likes", "super_likes", "passes", "matches", "stale"];

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

/** The training block of a state, with defaults and types checked. */
export function normalizeTraining(raw) {
  const t = raw && typeof raw === "object" ? raw : {};
  const d = DEFAULT_STATE.training;
  const of = Math.max(1, nonNegInt(t.of, d.of));
  const acc = t.accuracy === null || t.accuracy === undefined ? null : finite(t.accuracy, null);
  const target = TARGETS.includes(Number(t.target)) ? Number(t.target) : d.target;
  return {
    active: Boolean(t.active),
    cycle: Math.min(of, nonNegInt(t.cycle, 0)),
    of,
    accuracy: acc,
    chance: finite(t.chance, d.chance),
    target,
  };
}

/**
 * A /api/state (or `event: state`) payload with defaults filled in and types checked, so
 * a partial or malformed reply never breaks the page.
 */
export function normalizeState(raw) {
  const s = raw && typeof raw === "object" ? raw : {};
  const d = DEFAULT_STATE;
  const triple = (v, fb, conv) =>
    Array.isArray(v) && v.length === 3 ? v.map((x, i) => conv(x, fb[i])) : [...fb];
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
    paradigm: d.paradigm,
    labels: triple(s.labels, d.labels, (x, fb) => (typeof x === "string" && x ? x : fb)),
    colors: triple(s.colors, d.colors, (x, fb) => (isHexColor(x) ? x : fb)),
    lags: triple(s.lags, d.lags, (x, fb) => nonNegInt(x, fb)),
    cycle_s: finite(s.cycle_s, d.cycle_s),
    votes: Math.max(1, nonNegInt(s.votes, d.votes)),
    margin: Math.max(0, finite(s.margin, d.margin)),
    cycles: Math.max(1, nonNegInt(s.cycles, d.cycles)),
    training: normalizeTraining(s.training),
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

// ---------------------------------------------------------------- confidence

/** A score's bar width in [0, 1]: the correlation clipped to 0..1 (cvep.js level). */
export function meterLevel(score) {
  const v = Number(score);
  return Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : 0;
}

/** The standard's text bar: BAR_CELLS cells filled in proportion (cvep.js bar). */
export function textBar(score, cells = BAR_CELLS) {
  const n = Math.round(meterLevel(score) * cells);
  return "█".repeat(n) + "░".repeat(cells - n);
}

/** A score as the standard shows it: "+0.42", "-0.03", or "–" for none. */
export function scoreText(score) {
  if (score === null || score === undefined) return "–";
  const v = Number(score);
  if (!Number.isFinite(v)) return "–";
  return (v >= 0 ? "+" : "") + v.toFixed(2);
}

/**
 * "lead 0.31   choice: Like" after a decision ("*" when it moved the choice; "no choice
 * yet" before a commit), as brainsync.vep.live.confidence_line and cvep.js statusLine.
 */
export function statusText(decision, labels = DEFAULT_STATE.labels) {
  if (!decision || typeof decision !== "object") return "no decision yet";
  const lead = decision.lead === null || decision.lead === undefined ? NaN : Number(decision.lead);
  const gap = Number.isFinite(lead) ? `lead ${lead.toFixed(2)}` : "lead  -  ";
  const k = Number(decision.committed);
  const choice = TARGETS.includes(k)
    ? `choice: ${labels[k]}` + (decision.changed ? " *" : "")
    : "no choice yet";
  return `${gap}   ${choice}`;
}

/** The training status line (docs/CVEP-STANDARD.md section 3), as brainsync.demo. */
export function trainingText(training, labels = DEFAULT_STATE.labels) {
  const t = normalizeTraining(training);
  if (t.active) return `training: keep looking at ${labels[t.target]}   cycle ${t.cycle}/${t.of}`;
  if (t.accuracy === null) return "";
  const pct = (x) => Math.round(100 * x);
  return `template ready: ${pct(t.accuracy)} % on held-out cycles (chance ${pct(t.chance)} %)`;
}

/** Training progress in [0, 1]. */
export function trainingProgress(training) {
  const t = normalizeTraining(training);
  return t.active ? Math.min(1, t.cycle / t.of) : 1;
}

/**
 * What each square shows after one `event: decision` (or none): its bar level and score,
 * whether it is this decision's best (`raw`), the committed choice, and the cue box
 * (on the training target while training; on the swiped square right after a swipe).
 * `settling` marks every square while a new card settles (its cycles are ignored).
 */
export function squareStates(decision, { training = false, target = PASS, settling = false,
                                         cue = ABSTAIN } = {}) {
  const out = TARGETS.map(() => ({ level: 0, score: null, best: false, committed: false,
                                   cue: false, settling: false }));
  if (training) {
    out[TARGETS.includes(target) ? target : PASS].cue = true;
    return out;
  }
  for (const s of out) s.settling = Boolean(settling);
  if (TARGETS.includes(Number(cue))) out[Number(cue)].cue = true;
  if (!decision || typeof decision !== "object" || settling) return out;
  const scores = Array.isArray(decision.scores) ? decision.scores : [];
  for (const k of TARGETS) {
    const v = scores[k] === null || scores[k] === undefined ? NaN : Number(scores[k]);
    out[k].score = Number.isFinite(v) ? v : null;
    out[k].level = meterLevel(v);
    out[k].best = Number(decision.raw) === k;
    out[k].committed = Number(decision.committed) === k;
  }
  return out;
}

// ---------------------------------------------------------------- gaze and input

const KEY_SIDE = Object.freeze({ ArrowLeft: "pass", ArrowUp: "super", ArrowRight: "like" });

/** Whether a key looks at a square: ← Pass, ↑ Super like, → Like. */
export function isGazeKey(key) {
  return Object.prototype.hasOwnProperty.call(KEY_SIDE, key);
}

/**
 * Gaze from the arrow keys held, in the order they were pressed: the most recently
 * pressed one wins (holding ← then also → looks right). null when no arrow is held.
 */
export function gazeFromKeys(heldKeys) {
  const keys = Array.from(heldKeys || []);
  for (let i = keys.length - 1; i >= 0; i--) {
    if (isGazeKey(keys[i])) return KEY_SIDE[keys[i]];
  }
  return null;
}

/**
 * The square the pointer is "looking" at after one pointer event on `side`, given the
 * square it was on before. Mouse: enter/leave, and any move over a square (after the
 * window loses and regains focus with the mouse resting on a square, no pointerenter
 * comes; a move is the only sign it is there). Touch and pen have no hover, so
 * press-and-hold looks and lifting the finger stops.
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
  if (!isGazeKey(key)) return null;
  if (!shiftKey && decoder) return null;
  return SIDES.indexOf(KEY_SIDE[key]);
}

// ---------------------------------------------------------------- card motion

/** Which way the old card leaves: left for Pass, up for Super like, right for Like. */
export function cardSlideDirection(target) {
  const t = Number(target);
  return t === LIKE ? "right" : t === SUPER ? "up" : "left";
}

/** Target -> CSS class pair for the leaving and entering card. */
export function slideClasses(target, reducedMotion) {
  if (reducedMotion) return { out: "card--fade-out", in: "card--fade-in" };
  return { out: `card--out-${cardSlideDirection(target)}`, in: "card--in" };
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

// ---------------------------------------------------------------- counts and rates

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
    { key: "super_likes", label: "Super likes", value: String(c.super_likes) },
    { key: "passes", label: "Passes", value: String(c.passes) },
    { key: "matches", label: "Matches", value: String(c.matches) },
    { key: "stale", label: "Stale cycles", value: String(c.stale) },
  ];
}

/** Label for a target index (-1 or anything else: the fallback). */
export function targetLabel(target, labels = DEFAULT_STATE.labels, none = "card") {
  const t = Number(target);
  return TARGETS.includes(t) ? labels[t] : none;
}

/** What the gaze chip says for a server gaze value. */
export function gazeLabel(gaze, labels = DEFAULT_STATE.labels) {
  const k = SIDES.indexOf(gaze);
  if (k >= 0) return `looking at ${labels[k]}`;
  return gaze === "auto" ? "auto schedule" : "reading the card";
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

/** The match overlay's title and line for a swipe, or null when it is no match. */
export function matchBanner(payload, deck = []) {
  if (!payload || !payload.match) return null;
  const who = deck[payload.swiped];
  const name = who ? who.name : "Someone";
  return payload.super_like
    ? { title: "Super like!", line: `${name} is a match` }
    : { title: "It’s a match!", line: `${name} liked you back` };
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

// ---------------------------------------------------------------- layout

/**
 * Sizes (px) for the stage, like the swipe_cvep stimulus: the 2:3 card in the middle,
 * Pass on its left, Like on its right and Super like above it, each square with its
 * label and confidence bar under it (`meta` px). Wide windows: the squares are about a
 * fifth of the stage height; the card takes the height left under the Super like square
 * and the centres of Pass and Like move apart to fit it, or the card shrinks when the
 * window is too narrow. Narrow or portrait windows stack: the card on top, the three
 * squares in a row below it, everything within the width (the page may scroll).
 */
export function clampLayout(viewportW, viewportH, opts = {}) {
  const {
    top = 76,      // header
    bottom = 150,  // stats bar, readout and legend
    gutter = 16,
    minWide = 760,
    cardMargin = 28,   // card to square, each side
    meta = 100,        // label, confidence bar and caption under each square
  } = opts;
  const vw = Math.max(240, finite(viewportW, 1024));
  const vh = Math.max(320, finite(viewportH, 768));
  const wide = vw >= minWide && vw >= vh * 1.05;
  if (wide) {
    const stageH = Math.max(240, vh - top - bottom);
    const inner = vw - 2 * gutter;
    const square = Math.max(64, Math.round(Math.min(0.2 * stageH, inner / 7)));
    const superH = square + meta + 12;              // the Super like row
    const roomH = Math.max(120, stageH - superH - 8);
    let cardH = Math.round(roomH);
    let cardW = Math.round(cardH / 1.5);
    const widest = Math.floor(inner - square);      // pitch + square fits the width
    let pitch = cardW + square + 2 * cardMargin;
    if (pitch > widest) {
      pitch = widest;
      cardW = Math.max(60, pitch - square - 2 * cardMargin);
      cardH = Math.round(cardW * 1.5);
      cardW = Math.round(cardH / 1.5);
    }
    return { mode: "wide", square, pitch, cardW, cardH, superH, stageH: Math.round(stageH) };
  }
  const inner = vw - 2 * gutter;
  const gap = 12;
  const square = Math.round(Math.max(56, Math.min((inner - 2 * gap) / 3, vh * 0.16, 160)));
  const pitch = square + gap;
  const room = vh - top - square - meta - 24;
  const cardH = Math.round(Math.max(240, Math.min(inner * 1.5, room, 720)));
  const cardW = Math.round(cardH / 1.5);
  return { mode: "stack", square, pitch, cardW, cardH, superH: 0,
           stageH: cardH + square + meta + 24 };
}
