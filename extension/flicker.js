/*
 * BrainScroll flicker: frame-indexed luminance, an exact port of
 * brainsync/stimulus/flicker.py luminance().
 *
 *     BrainScrollFlicker.luminance(15, 0, n, 60, "sine", 1)   // content script / page
 *     const F = require("./flicker.js")                       // node tests
 *
 * Luminance is a function of the frame count since flicker onset, never of the wall
 * clock: frame n of a target at f Hz on a refresh Hz display shows
 * 0.5 * (1 + depth * sin(2 pi f n / refresh + phase)) for the sine waveform. The square
 * waveform is high for the first half of every cycle of that same phase:
 * 0.5 * (1 + depth * (2 on - 1)) with on 1 then 0. The modulation depth in (0, 1] swings
 * the luminance around mid-grey 0.5 by depth / 2; the default 1 is the full black-to-white
 * flicker, the same bits as without a depth. The arithmetic keeps
 * numpy's operation order, and the square waveform uses numpy's mod (result takes the
 * divisor's sign), so extension/test/flicker_fixture.json matches to the last bit or two.
 */
(function (root) {
  "use strict";

  const WAVEFORMS = Object.freeze(["sine", "square"]);
  // An rAF interval longer than this many refresh periods is a dropped frame (flicker.py).
  const DROP_FACTOR = 1.5;

  // numpy.mod for floats: fmod, then shifted into the divisor's sign.
  function pyMod(a, b) {
    let m = a % b;
    if (m !== 0 && (m < 0) !== (b < 0)) m += b;
    return m;
  }

  /** Luminance in [0.5 - depth/2, 0.5 + depth/2] of one target on frame index `frame`. */
  function luminance(freq, phase, frame, refresh, waveform, depth) {
    if (waveform === undefined) waveform = "sine";
    if (depth === undefined) depth = 1.0;
    if (!(refresh > 0)) throw new RangeError("refresh must be positive, got " + refresh);
    // also refuses NaN and non-numbers such as "0.5"
    if (!(typeof depth === "number" && depth > 0 && depth <= 1)) {
      throw new RangeError("depth must be in (0, 1], got " + depth);
    }
    if (waveform === "sine") {
      return 0.5 * (1.0 + depth * Math.sin(2.0 * Math.PI * freq * frame / refresh + phase));
    }
    if (waveform === "square") {
      const cycles = (freq * frame) / refresh + phase / (2.0 * Math.PI);
      const on = pyMod(cycles, 1.0) < 0.5 ? 1.0 : 0.0;
      return 0.5 * (1.0 + depth * (2.0 * on - 1.0));
    }
    throw new RangeError("unknown waveform " + JSON.stringify(waveform) +
                         "; expected one of " + WAVEFORMS.join(", "));
  }

  /** 15-25 Hz flicker is the band most likely to trigger photosensitive seizures. */
  function photosensitive(freq) {
    return freq >= 15.0 && freq <= 25.0;
  }

  const api = Object.freeze({ WAVEFORMS, DROP_FACTOR, luminance, photosensitive, pyMod });
  root.BrainScrollFlicker = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
