// The c-VEP code on the session page (display.flicker: browser, stim.paradigm: code), as pure
// functions, so node can test it (tests/js/code_core.test.js); app.js runs it on
// requestAnimationFrame. Loaded after cvep.js (BrainSyncCode, a byte-identical copy of
// clients/js/cvep.js) and before app.js.
//
// The standard (docs/CVEP-STANDARD.md): three white squares on black show one 63-bit
// m-sequence, the square of a slot with lag L frames white on flicker frame n when
// BITS[(n - L) mod 63] is 1. n counts frames from the first flickering frame of each training
// and each trial. The page reports the flip of every cycle's first frame (n mod 63 == 0) as a
// "cycle" message; the server relays it to the bridge, whose decoder epochs on it
// (INTERFACE-v2-draft.md section 9.7).
"use strict";

const CodeCore = (() => {
  const K = typeof BrainSyncCode !== "undefined" ? BrainSyncCode : require("./cvep.js");
  const MAX_LATE_FRAMES = 2;   // a cycle start drawn this many frames late is still reported
  const OFFSET_SAMPLES = 64;   // wall-clock offset: the max over this many samples

  function mod(a, b) {
    const m = a % b;
    return m < 0 ? m + b : m;
  }

  // 1 (white) or 0 (black): the square of a slot with lag ``lag`` frames on flicker frame n.
  function squareBit(lag, n, bits) {
    const b = bits || K.BITS;
    return b[mod(n - lag, b.length)];
  }

  // The square's colour on frame n: the standard's white or black.
  function squareColor(lag, n, bits) {
    return squareBit(lag, n, bits) ? K.WHITE : K.BLACK;
  }

  // The cycles to report on flicker frame n (the previous frame drawn was prevN, null right
  // after the onset): [{n: cycle index, frame: its first frame}] when frame n starts a new cycle
  // or lies at most MAX_LATE_FRAMES into it (its first frames were dropped: the cycle is
  // reported at its first frame's place on the frame grid). A longer gap (a stall, a hidden tab)
  // skips the cycles it spans: their starts were never shown, and an epoch cut there would hold
  // frames that were not. The same rule as the BrainScroll overlay (extension/overlay.js).
  function cyclesDue(prevN, n, cycleFrames, maxLate) {
    const len = cycleFrames || K.CYCLE_FRAMES;
    const late = maxLate === undefined ? MAX_LATE_FRAMES : maxLate;
    const c = Math.floor(n / len);
    const start = c * len;
    if (prevN !== null && prevN !== undefined && Math.floor(prevN / len) >= c) return [];
    if (n - start > late) return [];
    return [{ n: c, frame: start }];
  }

  // Unix time (s) of a requestAnimationFrame timestamp. Not performance.timeOrigin + ms:
  // performance time stops while the machine sleeps, so it drifts from the wall clock over a
  // long session. Each sample is Date.now() - performance.now(), low by up to 1 ms (Date.now()
  // truncates); the max of the last samples is the estimate (the BrainScroll overlay's way).
  class ClockOffset {
    constructor(wallNow, perfNow, size) {
      this.wallNow = wallNow || (() => Date.now());
      this.perfNow = perfNow || (() => performance.now());
      this.size = size || OFFSET_SAMPLES;
      this.buf = [];
    }

    sample() {
      this.buf.push(this.wallNow() - this.perfNow());
      if (this.buf.length > this.size) this.buf.shift();
    }

    unix(ms) {
      if (!this.buf.length) this.sample();
      return (Math.max(...this.buf) + ms) / 1000;
    }

    now() {
      return this.unix(this.perfNow());
    }
  }

  // Why the page cannot draw the server's code ("" when it can): /api/config's code object and
  // the slots' lags against the bits this page has.
  function codeProblem(code, lags) {
    if (!code) return "The server sent no code settings: reload the page.";
    if (code.length !== K.LENGTH || code.cycle_frames !== K.CYCLE_FRAMES) {
      return `The server's code (${code.length} bits, ${code.cycle_frames}-frame cycle) is not ` +
        `this page's (${K.LENGTH} bits): update the page.`;
    }
    const bad = Object.entries(lags || {}).filter(([, lag]) => !Number.isInteger(lag) || lag < 0);
    if (!Object.keys(lags || {}).length || bad.length) return "The server sent no code lags: reload the page.";
    return "";
  }

  // The squares in the user's order (slot_order: s1 left, s2 middle, s3 right). On a page or the
  // confirm screen, each slot of the show_page message keeps its role and text; a slot not in it
  // (the middle one on the confirm screen) is a still square. In training (``show`` null) every
  // square flickers, the cue on ``cueSlot``.
  function row(slotOrder, show, cueSlot) {
    const bySlot = {};
    for (const s of (show && show.slots) || []) bySlot[s.slot] = s;
    return slotOrder.map((slot) => {
      if (!show) return { slot, role: "train", label: "", utterance: "", cue: slot === cueSlot };
      const s = bySlot[slot];
      return s ? { slot, role: s.role, label: s.label, utterance: s.utterance || "", cue: false }
        : { slot, role: "still", label: "", utterance: "", cue: false };
    });
  }

  // The bar line under a square: an 8-cell bar of the correlation clipped to 0..1, the signed
  // score and "<" on this decision's best (brainsync/voice_bridge.py score_line, cvep.js bar).
  function barLine(score, best) {
    const s = Number(score);
    const shown = Number.isFinite(s) ? (s >= 0 ? "+" : "") + s.toFixed(2) : "  -  ";
    return `${K.bar(s)} ${shown}${best ? " <" : ""}`;
  }

  // "lead 0.31   choice: <the committed square's text>" (cvep.js statusLine).
  function statusText(msg, textBySlot) {
    if (!msg) return "";
    const lead = msg.lead === null || msg.lead === undefined ? NaN : Number(msg.lead);
    const committed = msg.committed ? ((textBySlot && textBySlot[msg.committed]) || msg.committed).slice(0, 40) : null;
    return K.statusLine(lead, committed, Boolean(msg.changed));
  }

  return { MAX_LATE_FRAMES, squareBit, squareColor, cyclesDue, ClockOffset, codeProblem, row,
           barLine, statusText };
})();

if (typeof module !== "undefined") module.exports = CodeCore;
