/*
 * BrainScroll flicker: frame-indexed luminance, an exact port of
 * brainsync/stimulus/flicker.py luminance().
 *
 *     BrainScrollFlicker.luminance(15, 0, n, 60, "sine")   // content script / page
 *     const F = require("./flicker.js")                    // node tests
 *
 * Luminance is a function of the frame count since flicker onset, never of the wall
 * clock: frame n of a target at f Hz on a refresh Hz display shows
 * 0.5 * (1 + sin(2 pi f n / refresh + phase)) for the sine waveform. The square waveform
 * is on for the first half of every cycle of that same phase. The arithmetic keeps
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

  /** Luminance in [0, 1] of one target on frame index `frame`. */
  function luminance(freq, phase, frame, refresh, waveform) {
    if (waveform === undefined) waveform = "sine";
    if (!(refresh > 0)) throw new RangeError("refresh must be positive, got " + refresh);
    if (waveform === "sine") {
      return 0.5 * (1.0 + Math.sin(2.0 * Math.PI * freq * frame / refresh + phase));
    }
    if (waveform === "square") {
      const cycles = (freq * frame) / refresh + phase / (2.0 * Math.PI);
      return pyMod(cycles, 1.0) < 0.5 ? 1.0 : 0.0;
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
