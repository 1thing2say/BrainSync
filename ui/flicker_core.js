// The flicker of the user's screen (display.flicker: browser), as pure functions, so node can test
// it (tests/js/flicker_core.test.js); app.js runs it on requestAnimationFrame. Loaded before app.js.
//
// The same luminance as the PsychoPy display (brainsync/stimulus/flicker.py): frame n after the
// onset shows 0.5 * (1 + sin(2 pi f n / refresh + phase)) for sine, and the first half of each
// cycle on for square. n comes from the frame's own timestamp rounded to the frame period, so a
// late or dropped frame never shifts the frequency or the phase of the frames after it.
"use strict";

const FlickerCore = (() => {
  // The frame index of a requestAnimationFrame timestamp ``ms`` after the onset frame's.
  function frameIndex(ms, frameMs) {
    return Math.max(0, Math.round(ms / frameMs));
  }

  // Luminance 0..1 of a target on frame ``n``.
  function luminance(freqHz, phaseDeg, n, refreshHz, waveform) {
    const phase = (phaseDeg * Math.PI) / 180;
    if (waveform === "square") {
      const cycles = (freqHz * n) / refreshHz + phase / (2 * Math.PI);
      return cycles - Math.floor(cycles) < 0.5 ? 1 : 0;
    }
    return 0.5 * (1 + Math.sin((2 * Math.PI * freqHz * n) / refreshHz + phase));
  }

  // The screen's refresh from requestAnimationFrame timestamps (ms): the median frame interval as
  // a rate, and the share of intervals more than 25 % off it (dropped frames, or a variable
  // refresh such as ProMotion, which blurs every frequency). null until there are enough frames.
  function measure(stamps) {
    const gaps = [];
    for (let i = 1; i < stamps.length; i++) gaps.push(stamps[i] - stamps[i - 1]);
    if (gaps.length < 30) return null;
    const sorted = [...gaps].sort((a, b) => a - b);
    const median = sorted[Math.floor(sorted.length / 2)];
    const uneven = gaps.filter((g) => Math.abs(g - median) > 0.25 * median).length / gaps.length;
    return { hz: 1000 / median, uneven };
  }

  // What to tell the user about a measured refresh; "" when it suits the flicker. Each target's
  // frequency is drawn from frame counts, so the rate must be steady and the one in the config.
  function refreshProblem(m, expectedHz) {
    if (!m) return "";
    if (Math.abs(m.hz - expectedHz) > 1) {
      return `This screen runs at ${m.hz.toFixed(0)} Hz: set it to ${expectedHz} Hz (ProMotion off).`;
    }
    if (m.uneven > 0.1) return "This screen's frames are uneven: close other apps and windows.";
    return "";
  }

  // Whether this page draws the flicker, from /api/config: only with ``flicker`` true, and never
  // when another screen draws it (``display`` psychopy or phone) or for c-VEP (``paradigm`` code,
  // drawn by the iPhone app). An older server sends neither key: ``flicker`` alone decides.
  function drawsFlicker(config) {
    if (!config || !config.flicker) return false;
    if (config.display && config.display !== "browser") return false;
    if (config.paradigm && config.paradigm !== "freq") return false;
    return true;
  }

  // The slots that have a frequency, {slot: {freq_hz, phase_deg}}; a c-VEP slot ({lag_frames})
  // has none and is left out, so the flicker loop never reads a missing freq_hz.
  function freqSlots(slots) {
    const out = {};
    for (const [slot, spec] of Object.entries(slots || {})) {
      if (spec && Number.isFinite(spec.freq_hz) && spec.freq_hz > 0) {
        out[slot] = { freq_hz: spec.freq_hz, phase_deg: Number.isFinite(spec.phase_deg) ? spec.phase_deg : 0 };
      }
    }
    return out;
  }

  return { frameIndex, luminance, measure, refreshProblem, drawsFlicker, freqSlots };
})();

if (typeof module !== "undefined") module.exports = FlickerCore;
