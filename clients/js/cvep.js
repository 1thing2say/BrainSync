/*
 * BrainSync c-VEP standard for browser displays: an exact port of brainsync/vep/live.py.
 *
 *     BrainSyncCode.bit(k, n)            // 1 (white) or 0 (black): target k on flicker frame n
 *     BrainSyncCode.cycleStart(n)        // true on every cycle's first frame (report its flip)
 *     BrainSyncCode.bar(0.42)            // "███░░░░░" confidence bar, 8 cells
 *     const C = require("./cvep.js")     // node tests
 *
 * The standard (docs/CVEP-STANDARD.md): three white squares on black show one 63-bit
 * m-sequence, target k delayed by k * 21 frames (350 ms at 60 Hz). Frame n of target k is
 * white when BITS[(n - k * LAG_FRAMES) mod 63] is 1. n counts frames since the layout's
 * first flickering frame, so a cycle starts on every n with n mod 63 == 0: the display
 * stamps that frame's flip time (performance.timeOrigin + rAF timestamp, Unix ms / 1000)
 * and reports it as a "cycle" message. BITS equal brainsync.vep.codes.m_sequence(63)
 * (tests/test_vep_live.py checks this file against Python). web/vep-lab/vep_core.js is the
 * explainer page's simulation core; this file is the small runtime the apps load.
 */
(function (root) {
  "use strict";

  const BITS = Object.freeze([
    0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 0, 1, 0, 1, 0, 1, 1, 0, 0, 1,
    1, 0, 1, 1, 1, 0, 1, 1, 0, 1, 0, 0, 1, 0, 0, 1, 1, 1, 0, 0, 0,
    1, 0, 1, 1, 1, 1, 0, 0, 1, 0, 1, 0, 0, 0, 1, 1, 0, 0, 0, 0, 1]);
  const LENGTH = 63;
  const N_TARGETS = 3;
  const LAG_FRAMES = 21;
  const CYCLE_FRAMES = 63;
  const TRAIN_CYCLES = 30;
  const BAR_CELLS = 8;
  const WHITE = "#ffffff";
  const BLACK = "#000000";

  function mod(a, b) {
    const m = a % b;
    return m < 0 ? m + b : m;
  }

  /** Bit (1 white, 0 black) target `target` shows on flicker frame `frame`. */
  function bit(target, frame, lagFrames, bits) {
    if (lagFrames === undefined) lagFrames = LAG_FRAMES;
    if (bits === undefined) bits = BITS;
    if (!Number.isInteger(target) || target < 0) {
      throw new RangeError("target must be a non-negative integer, got " + target);
    }
    if (!Number.isInteger(frame)) throw new RangeError("frame must be an integer, got " + frame);
    return bits[mod(frame - target * lagFrames, bits.length)];
  }

  /** Every target's bit on frame `frame`. */
  function frameBits(frame, nTargets, lagFrames, bits) {
    if (nTargets === undefined) nTargets = N_TARGETS;
    const out = [];
    for (let k = 0; k < nTargets; k++) out.push(bit(k, frame, lagFrames, bits));
    return out;
  }

  /** The CSS colour of target `target` on frame `frame`: white or black. */
  function color(target, frame, lagFrames, bits) {
    return bit(target, frame, lagFrames, bits) ? WHITE : BLACK;
  }

  /** True on each cycle's first frame (frame 0, 63, 126, ...). */
  function cycleStart(frame, cycleFrames) {
    if (cycleFrames === undefined) cycleFrames = CYCLE_FRAMES;
    return mod(frame, cycleFrames) === 0;
  }

  /** The cycle number of frame `frame`. */
  function cycleIndex(frame, cycleFrames) {
    if (cycleFrames === undefined) cycleFrames = CYCLE_FRAMES;
    return Math.floor(frame / cycleFrames);
  }

  /** `cells` characters filled in proportion to `value` clipped to 0..1 (a correlation). */
  function bar(value, cells) {
    if (cells === undefined) cells = BAR_CELLS;
    let v = Number(value);
    if (!Number.isFinite(v)) v = 0;
    v = Math.min(1, Math.max(0, v));
    const n = Math.round(v * cells);
    return "█".repeat(n) + "░".repeat(cells - n);
  }

  /** "YES  ███░░░░░ +0.42 <": a label line, as brainsync.vep.live.confidence_labels. */
  function labelLine(label, score, best, width) {
    if (width === undefined) width = label.length;
    const s = Number(score);
    const shown = Number.isFinite(s) ? (s >= 0 ? "+" : "") + s.toFixed(2) : "  -  ";
    return label.padEnd(width) + " " + bar(s) + " " + shown + (best ? " <" : "");
  }

  /** "lead 0.31   choice: YES *", as brainsync.vep.live.confidence_line. */
  function statusLine(lead, committed, changed) {
    const gap = Number.isFinite(lead) ? "lead " + Number(lead).toFixed(2) : "lead  -  ";
    const choice = committed == null ? "no choice yet"
      : "choice: " + committed + (changed ? " *" : "");
    return gap + "   " + choice;
  }

  /** Width of a bar in [0, 1] for a graphical meter (negative scores show empty). */
  function level(score) {
    const v = Number(score);
    return Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : 0;
  }

  const api = Object.freeze({
    BITS, LENGTH, N_TARGETS, LAG_FRAMES, CYCLE_FRAMES, TRAIN_CYCLES, BAR_CELLS, WHITE, BLACK,
    bit, frameBits, color, cycleStart, cycleIndex, bar, labelLine, statusLine, level,
  });
  root.BrainSyncCode = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
