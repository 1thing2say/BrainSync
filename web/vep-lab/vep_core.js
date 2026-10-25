/*
 * VEP Lab core: the stimulus codes and the toy response model of the explainer page, as
 * plain functions, a port of brainsync/vep/codes.py and brainsync/synthetic.py.
 *
 *     VepCore.mSequence(63)                     // page (global)
 *     const V = require("./vep_core.js")        // node tests
 *
 * The page shows what Figures 2 to 4 of Bin et al. (2009) show, so the numbers must be the
 * ones the Python demo uses: the same LFSR (x^6 + x + 1, seed 1), the same lagged codes
 * (np.roll, target k delayed k * lag frames), the same flash VEP kernel (fs 250, 0.5 s,
 * 60 ms latency, a 6 Hz sine with a 0.1 s decay, peak 1) and the same stimulus signal the
 * fake board convolves it with (bit 1 is +0.5, bit 0 is -0.5: the luminance minus the still
 * grey). tests/test_vep_lab.py compares the m-sequence bit for bit and the kernel at 1e-9.
 * Nothing here touches the DOM; the DFT and the autocorrelation are direct sums (no
 * library), fine for a few seconds at 250 Hz.
 */
(function (root) {
  "use strict";

  // One primitive polynomial over GF(2) per degree, as the exponents of its non-constant
  // terms, the same table as codes.PRIMITIVE_TAPS.
  const PRIMITIVE_TAPS = Object.freeze({
    3: [3, 1], 4: [4, 1], 5: [5, 2], 6: [6, 1], 7: [7, 1], 8: [8, 6, 5, 4], 9: [9, 4],
    10: [10, 3],
  });
  const FVEP_FREQS = Object.freeze([15, 12, 10, 60 / 7, 7.5, 6]);   // the paper's six, Fig. 3
  const REFRESH = 60;
  const FS = 250;

  function isInt(v) { return typeof v === "number" && Number.isInteger(v); }

  /** The shift-register degree n with 2^n - 1 == length; refuses other lengths. */
  function degreeForLength(length) {
    if (!isInt(length) || length < 1) {
      throw new RangeError("m-sequence length must be a positive integer, got " + length);
    }
    const n = 32 - Math.clz32(length);   // bit length
    if (2 ** n - 1 !== length) {
      const ok = Object.keys(PRIMITIVE_TAPS).map((d) => 2 ** Number(d) - 1).join(", ");
      throw new RangeError("m-sequence length must be 2^n - 1 (" + ok + "), got " + length);
    }
    if (!(n in PRIMITIVE_TAPS)) {
      throw new RangeError("no primitive polynomial listed for degree " + n + " (length " +
                           length + ")");
    }
    return n;
  }

  /** One period of a binary m-sequence (0/1 ints): a Fibonacci LFSR started from `seed`. */
  function mSequence(length, seed) {
    if (length === undefined) length = 63;
    if (seed === undefined) seed = 1;
    const n = degreeForLength(length);
    if (!isInt(seed) || seed < 1 || seed >= 2 ** n) {
      throw new RangeError("seed must be an integer in [1, " + (2 ** n - 1) + "], got " + seed);
    }
    const taps = PRIMITIVE_TAPS[n];
    let state = [];
    for (let i = 0; i < n; i++) state.push((seed >> i) & 1);   // state[i] holds x^(i+1)
    const out = new Array(length);
    for (let i = 0; i < length; i++) {
      out[i] = state[n - 1];
      let feedback = 0;
      for (const e of taps) feedback ^= state[e - 1];
      state = [feedback].concat(state.slice(0, n - 1));
    }
    return out;
  }

  function bits(seq, what) {
    if (!Array.isArray(seq) || seq.length === 0) throw new RangeError((what || "sequence") + " is empty");
    for (const b of seq) {
      if (b !== 0 && b !== 1) throw new RangeError((what || "sequence") + " must hold 0/1 bits only, got " + b);
    }
    return seq;
  }

  /** Circular autocorrelation of the +/-1 version of a bit sequence, one value per lag:
   *  N at lag 0 and -1 everywhere else for an m-sequence. */
  function autocorrelation(seq) {
    const s = bits(seq).map((b) => 2 * b - 1);
    const n = s.length;
    const out = new Array(n);
    for (let lag = 0; lag < n; lag++) {
      let sum = 0;
      for (let i = 0; i < n; i++) sum += s[i] * s[(i + lag) % n];
      out[lag] = sum;
    }
    return out;
  }

  /** `a` rotated right by `shift` (np.roll): out[i] = a[(i - shift) mod n]. */
  function roll(a, shift) {
    const n = a.length;
    const out = new Array(n);
    const k = ((shift % n) + n) % n;
    for (let i = 0; i < n; i++) out[(i + k) % n] = a[i];
    return out;
  }

  /** Per-target frame bits: target k is the sequence delayed by k * lagFrames frames, each
   *  bit held for framesPerBit frames. */
  function laggedCodes(seq, nTargets, lagFrames, framesPerBit) {
    if (framesPerBit === undefined) framesPerBit = 1;
    const b = bits(seq);
    for (const [name, v] of [["nTargets", nTargets], ["lagFrames", lagFrames], ["framesPerBit", framesPerBit]]) {
      if (!isInt(v) || v < 1) throw new RangeError(name + " must be an integer >= 1, got " + v);
    }
    const cycle = [];
    for (const x of b) for (let r = 0; r < framesPerBit; r++) cycle.push(x);
    if ((nTargets - 1) * lagFrames >= cycle.length) {
      throw new RangeError(nTargets + " targets " + lagFrames + " frames apart need " +
                           ((nTargets - 1) * lagFrames + 1) + " frames, but one cycle has " + cycle.length);
    }
    const out = [];
    for (let k = 0; k < nTargets; k++) out.push(roll(cycle, k * lagFrames));
    return out;
  }

  // -- t-VEP ------------------------------------------------------------------------------

  /** A small seeded PRNG (mulberry32), so a shuffled schedule is the same on every load. */
  function makeRng(seed) {
    let a = (seed >>> 0) || 1;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function permutation(n, rng) {
    const a = [];
    for (let i = 0; i < n; i++) a.push(i);
    for (let i = n - 1; i > 0; i--) {
      const j = Math.floor(rng() * (i + 1));
      const tmp = a[i]; a[i] = a[j]; a[j] = tmp;
    }
    return a;
  }

  /** Non-overlapping flashes: every round flashes each target once, one slot of
   *  flashFrames + gapFrames each; `order` is "roundrobin" or "random" (shuffled per round).
   *  Returns {nTargets, flashFrames, gapFrames, onsets: [[frame, target], ...], nFrames}. */
  function timeSchedule(nTargets, flashFrames, gapFrames, rounds, order, seed) {
    if (order === undefined) order = "random";
    for (const [name, v, low] of [["nTargets", nTargets, 1], ["flashFrames", flashFrames, 1],
                                  ["gapFrames", gapFrames, 0], ["rounds", rounds, 1]]) {
      if (!isInt(v) || v < low) throw new RangeError(name + " must be an integer >= " + low + ", got " + v);
    }
    if (order !== "random" && order !== "roundrobin") {
      throw new RangeError("unknown order " + JSON.stringify(order) + "; expected roundrobin or random");
    }
    const rng = makeRng(seed === undefined ? 1 : seed);
    const slot = flashFrames + gapFrames;
    const onsets = [];
    let frame = 0;
    for (let r = 0; r < rounds; r++) {
      let targets = [];
      if (order === "random") targets = permutation(nTargets, rng);
      else for (let k = 0; k < nTargets; k++) targets.push(k);
      for (const k of targets) {
        onsets.push([frame, k]);
        frame += slot;
      }
    }
    return { nTargets, flashFrames, gapFrames, onsets, nFrames: frame };
  }

  /** Per-target frame bits (nTargets rows of nFrames) of a schedule. */
  function scheduleMatrix(schedule) {
    const m = [];
    for (let k = 0; k < schedule.nTargets; k++) m.push(new Array(schedule.nFrames).fill(0));
    for (const [frame, k] of schedule.onsets) {
      for (let f = frame; f < frame + schedule.flashFrames && f < schedule.nFrames; f++) m[k][f] = 1;
    }
    return m;
  }

  /** Onset frames of one target, ascending. */
  function onsetsOf(schedule, target) {
    return schedule.onsets.filter((o) => o[1] === target).map((o) => o[0]);
  }

  // -- f-VEP ------------------------------------------------------------------------------

  /** On/off frame bits of a square flicker at `freq` Hz (on for the first half of each
   *  cycle, counted in frames, as flicker.js and flicker.py do). */
  function onOffBit(freq, frame, refresh) {
    if (refresh === undefined) refresh = REFRESH;
    if (!(freq > 0) || !(refresh > 0)) throw new RangeError("freq and refresh must be positive, got " + freq + ", " + refresh);
    const cycles = (freq * frame) / refresh;
    return cycles - Math.floor(cycles) < 0.5 ? 1 : 0;
  }
  function onOffFrames(freq, nFrames, refresh) {
    const out = new Array(nFrames);
    for (let i = 0; i < nFrames; i++) out[i] = onOffBit(freq, i, refresh);
    return out;
  }

  // -- the toy response model ----------------------------------------------------------------

  /** A made-up flash VEP (impulse response), peak amplitude 1: nothing for `latency` s, then
   *  a damped sine; synthetic.vep_kernel with the same defaults and arithmetic. */
  function vepKernel(fs, seconds, latency, freq, decay) {
    if (fs === undefined) fs = FS;
    if (seconds === undefined) seconds = 0.5;
    if (latency === undefined) latency = 0.06;
    if (freq === undefined) freq = 6.0;
    if (decay === undefined) decay = 0.1;
    if (!(fs > 0 && seconds > 0 && decay > 0 && freq > 0)) {
      throw new RangeError("fs, seconds, decay and freq must be positive, got " + [fs, seconds, decay, freq].join(", "));
    }
    if (!(latency >= 0 && latency < seconds)) {
      throw new RangeError("latency must lie in [0, seconds), got " + latency + " with " + seconds + " s");
    }
    const n = Math.round(seconds * fs);
    const k = new Array(n);
    let peak = 0;
    for (let i = 0; i < n; i++) {
      const t = i / fs - latency;
      k[i] = t >= 0 ? Math.exp(-Math.max(t, 0) / decay) * Math.sin(2 * Math.PI * freq * t) : 0.0;
      peak = Math.max(peak, Math.abs(k[i]));
    }
    if (peak <= 0) throw new RangeError("kernel of " + seconds + " s at " + fs + " Hz has no samples after the latency");
    for (let i = 0; i < n; i++) k[i] /= peak;
    return k;
  }

  /** Causal convolution trimmed to the stimulus length: the evoked EEG of the linear model. */
  function sequenceResponse(stim, kernel) {
    if (!stim.length || !kernel.length) throw new RangeError("stimulus and kernel must not be empty");
    const out = new Array(stim.length).fill(0);
    for (let i = 0; i < stim.length; i++) {
      const s = stim[i];
      if (s === 0) continue;
      for (let j = 0; j < kernel.length && i + j < stim.length; j++) out[i + j] += s * kernel[j];
    }
    return out;
  }

  /** Frame bits as a sample-rate stimulus signal: sample j shows frame floor(j * refresh /
   *  fs), bit 1 is +0.5 and bit 0 is -0.5 (the fake board's luminance minus the still grey). */
  function framesToSignal(frameBits, nSamples, fs, refresh) {
    if (fs === undefined) fs = FS;
    if (refresh === undefined) refresh = REFRESH;
    const out = new Array(nSamples);
    for (let j = 0; j < nSamples; j++) {
      const f = Math.floor(j * refresh / fs);
      out[j] = f < frameBits.length ? frameBits[f] - 0.5 : 0;
    }
    return out;
  }

  // -- what the decoders look at ------------------------------------------------------------

  /** Power spectrum of x by a direct DFT (mean removed, Hann window): {freqs, power} for the
   *  bins 0 .. N/2, power in arbitrary units. */
  function dft(x, fs) {
    const n = x.length;
    if (n < 2) throw new RangeError("dft needs at least 2 samples, got " + n);
    let mean = 0;
    for (const v of x) mean += v;
    mean /= n;
    const w = new Array(n);
    for (let i = 0; i < n; i++) w[i] = (x[i] - mean) * (0.5 - 0.5 * Math.cos(2 * Math.PI * i / (n - 1)));
    const half = Math.floor(n / 2);
    const freqs = new Array(half + 1), power = new Array(half + 1);
    for (let k = 0; k <= half; k++) {
      let re = 0, im = 0;
      const step = -2 * Math.PI * k / n;
      for (let i = 0; i < n; i++) {
        const a = step * i;
        re += w[i] * Math.cos(a);
        im += w[i] * Math.sin(a);
      }
      freqs[k] = k * fs / n;
      power[k] = (re * re + im * im) / n;
    }
    return { freqs, power };
  }

  /** The paper's SNR at f: power in the nearest bin over the mean of the bins within
   *  `neighbourHz` on each side (not the bin itself). */
  /** Power of the bin nearest f over the mean power of its neighbours. The Hann window
   *  spreads a line over a main lobe two bins wide on each side, so the ring starts beyond
   *  that (or at `neighbourHz`, if larger) and spans at least four bins: the same rule as
   *  brainsync.vep.fvep.SNR.ring, without which the ratio saturates. */
  function peakSnr(spectrum, f, neighbourHz) {
    if (neighbourHz === undefined) neighbourHz = 1.0;
    const { freqs, power } = spectrum;
    const binHz = freqs[1] - freqs[0];
    const inner = 2 * binHz;
    const outer = Math.max(neighbourHz, inner + 4 * binHz);
    let best = 0;
    for (let i = 1; i < freqs.length; i++) if (Math.abs(freqs[i] - f) < Math.abs(freqs[best] - f)) best = i;
    let sum = 0, count = 0;
    for (let i = 0; i < freqs.length; i++) {
      const d = Math.abs(freqs[i] - freqs[best]);
      if (d > inner && d <= outer) { sum += power[i]; count++; }
    }
    if (!count) throw new RangeError("no neighbour bins " + inner + "-" + outer + " Hz from " + f + " Hz");
    return power[best] / (sum / count);
  }

  /** Mean of the `length`-sample epochs starting at each onset sample (the t-VEP average). */
  function epochAverage(x, onsetSamples, length) {
    const out = new Array(length).fill(0);
    let count = 0;
    for (const s of onsetSamples) {
      if (s + length > x.length) continue;
      for (let i = 0; i < length; i++) out[i] += x[s + i];
      count++;
    }
    if (!count) return out;
    for (let i = 0; i < length; i++) out[i] /= count;
    return out;
  }

  function peakToValley(x) {
    let lo = Infinity, hi = -Infinity;
    for (const v of x) { if (v < lo) lo = v; if (v > hi) hi = v; }
    return hi - lo;
  }

  /** Pearson correlation of two equal-length arrays (0 when either is flat). */
  function pearson(a, b) {
    if (a.length !== b.length || a.length < 2) throw new RangeError("pearson needs two equal arrays of 2+ samples");
    const n = a.length;
    let ma = 0, mb = 0;
    for (let i = 0; i < n; i++) { ma += a[i]; mb += b[i]; }
    ma /= n; mb /= n;
    let sab = 0, saa = 0, sbb = 0;
    for (let i = 0; i < n; i++) {
      const da = a[i] - ma, db = b[i] - mb;
      sab += da * db; saa += da * da; sbb += db * db;
    }
    return saa > 0 && sbb > 0 ? sab / Math.sqrt(saa * sbb) : 0;
  }

  /** Wolpaw's ITR in bits per selection for N targets at accuracy P. */
  function bitsPerSelection(n, p) {
    if (n < 2 || !(p >= 0 && p <= 1)) throw new RangeError("need n >= 2 targets and p in [0, 1], got " + n + ", " + p);
    let b = Math.log2(n);
    if (p > 0) b += p * Math.log2(p);
    if (p < 1) b += (1 - p) * Math.log2((1 - p) / (n - 1));
    return Math.max(0, b);
  }

  const api = Object.freeze({
    PRIMITIVE_TAPS, FVEP_FREQS, REFRESH, FS,
    degreeForLength, mSequence, autocorrelation, roll, laggedCodes,
    makeRng, timeSchedule, scheduleMatrix, onsetsOf, onOffBit, onOffFrames,
    vepKernel, sequenceResponse, framesToSignal,
    dft, peakSnr, epochAverage, peakToValley, pearson, bitsPerSelection,
  });
  root.VepCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
