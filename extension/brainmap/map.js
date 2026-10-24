/*
 * BrainSync live scalp map: draws the user's own EEG, streamed by `python -m brainsync.brainmap`
 * as Server-Sent Events on /events (hello, then one frame every hop, plus status messages).
 *
 * What it shows: per-electrode band power as a z-score against the user's own rest baseline,
 * or the 15/17 Hz SSVEP SNR, interpolated between the 8 electrodes of the Cerelog X8 with
 * inverse-distance weighting and clipped to the head circle. It is a SCALP map: 8 electrodes
 * cannot say where inside the brain a rhythm comes from, and the page says so.
 *
 * Kept quiet on purpose: there is no requestAnimationFrame loop, no timer and no CSS animation.
 * Everything is redrawn once per frame message (4 Hz), the 64x64 grid is scaled up with
 * smoothing, the Python side already smooths the values, and the diverging scale has a dark
 * middle close to the head background, so a resting head is dim and only strong deviations get
 * bright. That is not shown to be harmless next to the 15/17 Hz flicker: until a decoder-accuracy
 * A/B with the headset (map on screen versus off), the map stays on its own screen.
 *
 * A bad electrode (quality other than "ok") is drawn hollow grey with its quality text and is
 * left out of the interpolation. The cells it is nearest to, and cells farther than MAX_REACH
 * from any good electrode, stay blank: IDW would otherwise paint a loose contact's region with
 * the values of electrodes on the other side of the head.
 *
 * When the data stops (disconnected, no data, error, replay ended) the last frame stays on screen
 * dimmed, with its values and contact shown as "–", until the next frame arrives.
 *
 * Pure helpers (interpolate, colour, messageProblem, ...) are exported for
 * extension/brainmap/test/map.test.mjs; create() wires the page.
 */
(function (root) {
  "use strict";

  const FORMAT = 1;
  const GRID = 64;
  const Z_RANGE = Object.freeze([-3, 3]);
  const REL_DB_RANGE = Object.freeze([-6, 6]);    // while the baseline collects
  const SNR_RANGE = Object.freeze([0, 12]);
  const TIMELINE_S = 60;
  const TIMELINE_Z = 4;                          // the timeline's y axis is +-4 z
  const STATUS_STATES = Object.freeze(["starting", "streaming", "no-data", "ended", "error"]);
  // Statuses after which the frame on screen is no longer live, and those after which the
  // server will not send more (the EventSource is closed so it does not reconnect over them).
  const STALE_STATES = Object.freeze(["disconnected", "closed", "no-data", "ended", "error"]);
  const TERMINAL_STATES = Object.freeze(["ended", "error"]);
  // Head units (radius 1). Half the Fp1-C3 spacing is 0.39, so 0.5 reaches between neighbours
  // but not across a missing one; on the X8 it leaves the T7/T8 rims, where no electrode sits,
  // blank too (about 6% of the head).
  const MAX_REACH = 0.5;
  const FADE = 0.08;                             // head units over which a blank edge fades in
  const DEFAULT_BANDS = Object.freeze({ delta: [1, 4], theta: [4, 8], alpha: [8, 13], beta: [13, 30] });
  const DEFAULT_SSVEP = Object.freeze([15, 17]);
  const DEFAULT_HOP_S = 0.25;
  const DEFAULT_LABEL = "Your EEG · 8 electrodes · scalp map, not a brain scan";

  // Both ramps start near the head background: the diverging one has a dark middle (z = 0) and
  // gets brighter only towards blue (below baseline) and red (above), so a resting head is dim
  // instead of the bright white disc of a classic RdBu; the sequential one is dim at 0 dB SNR.
  const DIVERGING = Object.freeze([
    [0, [120, 185, 250]], [0.25, [52, 120, 196]], [0.5, [36, 40, 54]],
    [0.75, [196, 74, 62]], [1, [252, 170, 130]],
  ]);
  const SEQUENTIAL = Object.freeze([
    [0, [30, 26, 56]], [0.3, [92, 36, 112]], [0.6, [186, 58, 86]],
    [0.85, [242, 138, 44]], [1, [250, 232, 150]],
  ]);

  const HEAD_BG = "#1a1d27", OUTLINE = "#c9ccd6", BAD = "#8a8f9e", RING = "#eceef4";
  const STALE_ALPHA = 0.35;                      // how much of a stale head still shows
  const MAX_BACKING = 2048;                      // canvas backing-store cap, px per side

  function isNum(v) { return typeof v === "number" && Number.isFinite(v); }
  function clamp01(t) { return t < 0 ? 0 : t > 1 ? 1 : t; }

  function ramp(stops, t) {
    t = clamp01(t);
    for (let k = 1; k < stops.length; k++) {
      const [t1, c1] = stops[k];
      if (t <= t1) {
        const [t0, c0] = stops[k - 1];
        const u = t1 > t0 ? (t - t0) / (t1 - t0) : 0;
        return [0, 1, 2].map((i) => Math.round(c0[i] + u * (c1[i] - c0[i])));
      }
    }
    return stops[stops.length - 1][1].slice();
  }

  /** Diverging blue-dark-red [r, g, b] for z in [vmin, vmax] (clamped); null if not a number. */
  function colour(z, vmin, vmax) {
    if (!isNum(z)) return null;
    const lo = isNum(vmin) ? vmin : Z_RANGE[0], hi = isNum(vmax) ? vmax : Z_RANGE[1];
    return ramp(DIVERGING, (z - lo) / (hi - lo));
  }

  /** Sequential dark-to-warm [r, g, b] for SNR dB in [vmin, vmax] (clamped); null if not a number. */
  function sequential(v, vmin, vmax) {
    if (!isNum(v)) return null;
    const lo = isNum(vmin) ? vmin : SNR_RANGE[0], hi = isNum(vmax) ? vmax : SNR_RANGE[1];
    return ramp(SEQUENTIAL, (v - lo) / (hi - lo));
  }

  /**
   * Inverse-distance (power 2) estimate at (x, y) from the valid values; exactly the electrode
   * value on an electrode. The raw estimator: it reaches any distance. grid() decides where an
   * estimate is honest enough to draw.
   */
  function idw(points, values, x, y) {
    let num = 0, den = 0;
    for (let k = 0; k < points.length; k++) {
      const v = values[k];
      if (!isNum(v)) continue;
      const dx = x - points[k][0], dy = y - points[k][1];
      const d2 = dx * dx + dy * dy;
      if (d2 < 1e-12) return v;
      num += v / d2;
      den += 1 / d2;
    }
    return den > 0 ? num / den : NaN;
  }

  // Grid cells are pixel centres over [-1, 1]^2, row 0 at the top (towards the nose), so the
  // array can go straight into an n x n ImageData. limit > 1 pads past the head circle so the
  // smoothed, clipped drawing has no dark fringe; interpolate() itself stops at the circle.
  // A cell is left NaN when its nearest electrode has no valid value (a bad or missing site
  // owns its region) or when that nearest electrode is farther than reach. fade, if given,
  // receives each cell's opacity 0..1: it falls to 0 over FADE head units towards those two
  // edges, so the drawn cut-off is soft instead of a 64-cell staircase.
  function grid(points, values, n, limit, reach, fade) {
    const out = new Float32Array(n * n).fill(NaN);
    if (fade) fade.fill(0);
    if (!values.some(isNum)) return out;
    const lim2 = limit * limit, step = 2 / n;
    for (let j = 0; j < n; j++) {
      const y = 1 - (j + 0.5) * step;
      for (let i = 0; i < n; i++) {
        const x = -1 + (i + 0.5) * step;
        if (x * x + y * y > lim2) continue;
        let good = Infinity, bad = Infinity;
        for (let k = 0; k < points.length; k++) {
          const d = Math.hypot(x - points[k][0], y - points[k][1]);
          if (isNum(values[k])) good = Math.min(good, d); else bad = Math.min(bad, d);
        }
        if (!(good <= bad) || good > reach) continue;
        out[j * n + i] = idw(points, values, x, y);
        if (fade) fade[j * n + i] = Math.min(clamp01((reach - good) / FADE), clamp01((bad - good) / FADE));
      }
    }
    return out;
  }

  /**
   * n x n inverse-distance map (Float32Array, row-major, row 0 at the nose) of values at points
   * ([x, y] in head units). null/NaN values are skipped. NaN outside the head circle, where the
   * nearest electrode has no valid value, farther than reach (default MAX_REACH; Infinity for
   * none) from the nearest electrode, or everywhere when no value is valid.
   */
  function interpolate(points, values, n, reach) {
    n = n || GRID;
    if (!Array.isArray(points) || !Array.isArray(values) || points.length !== values.length) {
      throw new Error("points and values must be arrays of the same length");
    }
    return grid(points, values, n, 1, reach === undefined ? MAX_REACH : reach);
  }

  /**
   * Split timeline points ({t, v}) into runs to draw as separate lines: a null v (no value, or a
   * marker for a disconnect) or a step in t larger than maxGap seconds starts a new run, so the
   * line never bridges time with no data.
   */
  function timelineRuns(points, maxGap) {
    const runs = [];
    let run = null, prev = null;
    for (const p of points) {
      if (!isNum(p.v)) { run = null; prev = null; continue; }
      if (!run || (prev && p.t - prev.t > maxGap)) { run = []; runs.push(run); }
      run.push(p);
      prev = p;
    }
    return runs;
  }

  /** The key strings frame.ssvep uses, in ssvep_hz order: hello.ssvep_keys, else String(Hz). */
  function ssvepKeys(hello) {
    if (hello && Array.isArray(hello.ssvep_keys)) return hello.ssvep_keys;
    return ((hello && hello.ssvep_hz) || DEFAULT_SSVEP).map(String);
  }

  function numArray(a, n, allowNull) {
    return Array.isArray(a) && a.length === n &&
      a.every((v) => isNum(v) || (allowNull && v === null));
  }

  function helloProblem(m) {
    if (m.format !== FORMAT) return "hello format " + m.format + " is not " + FORMAT;
    const ch = m.channels;
    if (!Array.isArray(ch) || !ch.length || !ch.every((c) => typeof c === "string" && c)) {
      return "hello has no channel names";
    }
    if (new Set(ch).size !== ch.length) return "hello repeats a channel name";
    if (!m.positions || typeof m.positions !== "object") return "hello has no positions";
    for (const c of ch) if (!numArray(m.positions[c], 2, false)) return "no position for " + c;
    if (!m.regions || typeof m.regions !== "object") return "hello has no regions";
    if (!m.bands || typeof m.bands !== "object" || !Object.keys(m.bands).length) return "hello has no bands";
    for (const [b, r] of Object.entries(m.bands)) {
      if (!numArray(r, 2, false) || !(r[0] < r[1])) return "band " + b + " is not [low, high] Hz";
    }
    if (!Array.isArray(m.ssvep_hz) || !m.ssvep_hz.every((f) => isNum(f) && f > 0)) return "hello ssvep_hz is not a list of Hz";
    if (m.ssvep_keys !== undefined) {
      const k = m.ssvep_keys;
      if (!Array.isArray(k) || k.length !== m.ssvep_hz.length || !k.every((s) => typeof s === "string" && s)) {
        return "hello ssvep_keys is not one key string per ssvep_hz";
      }
      if (new Set(k).size !== k.length) return "hello repeats an ssvep key";
    }
    for (const k of ["fs", "window_s", "hop_s"]) if (!isNum(m[k]) || m[k] <= 0) return "hello " + k + " is not a positive number";
    if (!isNum(m.baseline_s) || m.baseline_s < 0) return "hello baseline_s is not a number";
    if (typeof m.label !== "string" || !m.label) return "hello has no label";
    return "";
  }

  function frameProblem(m, hello) {
    if (!hello) return "frame before hello";
    const n = hello.channels.length;
    if (!Number.isInteger(m.seq)) return "frame seq is not an integer";
    if (!isNum(m.t)) return "frame t is not a number";
    for (const key of ["z", "db"]) {
      const o = m[key];
      if (!o || typeof o !== "object") return "frame has no " + key;
      for (const b of Object.keys(hello.bands)) {
        // db is documented as always numbers; null is tolerated (a channel with no power)
        // so one odd channel cannot blank the whole map. NaN, strings, wrong lengths are not.
        if (!numArray(o[b], n, true)) return "frame " + key + "." + b + " is not " + n + " numbers or nulls";
      }
    }
    if (!m.ssvep || typeof m.ssvep !== "object") return "frame has no ssvep";
    for (const key of ssvepKeys(hello)) {
      if (!numArray(m.ssvep[key], n, true)) return "frame ssvep." + key + " is not " + n + " numbers or nulls";
    }
    if (!Array.isArray(m.quality) || m.quality.length !== n || !m.quality.every((q) => typeof q === "string")) {
      return "frame quality is not " + n + " strings";
    }
    const bl = m.baseline;
    if (!bl || (bl.state !== "collecting" && bl.state !== "ready") || !isNum(bl.elapsed_s) || !isNum(bl.seconds)) {
      return "frame baseline is not {state, elapsed_s, seconds}";
    }
    return "";
  }

  /** Why msg cannot be used (given the last hello), or "" if it is fine. */
  function messageProblem(msg, hello) {
    if (!msg || typeof msg !== "object" || Array.isArray(msg)) return "message is not an object";
    if (msg.type === "hello") return helloProblem(msg);
    if (msg.type === "frame") return frameProblem(msg, hello);
    if (msg.type === "status") {
      if (STATUS_STATES.indexOf(msg.state) < 0) return "unknown status state " + JSON.stringify(msg.state);
      if (typeof msg.text !== "string") return "status text is not a string";
      return "";
    }
    return "unknown message type " + JSON.stringify(msg.type);
  }

  function isBad(q) { return q !== "ok"; }

  // A good contact with no z-score yet: it was off or bad during the rest period, so the stream
  // is still collecting its own baseline (a full --baseline-s of clean windows).
  const NO_BASELINE = "no baseline yet";
  function noBaseline(e, kind) { return kind === "z" && !isBad(e.quality) && !isNum(e.value); }

  /** The views the page offers for a hello: one per band, then one per SSVEP frequency. */
  function viewsFor(hello) {
    const bands = (hello && hello.bands) || DEFAULT_BANDS;
    const hz = (hello && hello.ssvep_hz) || DEFAULT_SSVEP, keys = ssvepKeys(hello);
    const out = Object.keys(bands).map((b) => {
      const name = b.charAt(0).toUpperCase() + b.slice(1), range = bands[b][0] + "–" + bands[b][1] + " Hz";
      return { id: b, kind: "band", key: b, name, range, title: name + " " + range };
    });
    hz.forEach((f, k) => {
      out.push({ id: "ssvep" + f, kind: "ssvep", key: keys[k], name: "SSVEP", range: f + " Hz", title: "SSVEP " + f + " Hz" });
    });
    return out;
  }

  /**
   * True when hello b describes the same map as hello a (channels, positions, bands, SSVEP
   * keys), so a frame drawn for a still applies: a reconnect to the same stream keeps it up.
   */
  function sameLayout(a, b) {
    if (!a || !b) return false;
    const key = (m) => JSON.stringify([m.channels, m.channels.map((c) => m.positions[c]),
      Object.entries(m.bands), ssvepKeys(m)]);
    return key(a) === key(b);
  }

  /**
   * Values to draw for one view of a frame: {values, kind} with kind "z" (baseline ready),
   * "rel" (dB relative to the mean across good electrodes, while the baseline collects) or
   * "snr" (SSVEP dB). Bad electrodes are null.
   */
  function viewValues(frame, view) {
    const q = frame.quality;
    const good = (arr) => arr.map((v, i) => (isBad(q[i]) || !isNum(v) ? null : v));
    if (view.kind === "ssvep") return { kind: "snr", values: good(frame.ssvep[view.key]) };
    if (frame.baseline.state === "ready") return { kind: "z", values: good(frame.z[view.key]) };
    const db = good(frame.db[view.key]);
    const ok = db.filter(isNum);
    const mean = ok.length ? ok.reduce((a, b) => a + b, 0) / ok.length : 0;
    return { kind: "rel", values: db.map((v) => (v === null ? null : v - mean)) };
  }

  function scaleFor(kind) {
    if (kind === "snr") return (v) => sequential(v, SNR_RANGE[0], SNR_RANGE[1]);
    if (kind === "rel") return (v) => colour(v, REL_DB_RANGE[0], REL_DB_RANGE[1]);
    return (v) => colour(v, Z_RANGE[0], Z_RANGE[1]);
  }

  function signed(v) {
    const s = Math.abs(v).toFixed(1);
    if (Number(s) === 0) return "0.0";                 // no "−0.0" for a value that rounds to 0
    return (v < 0 ? "−" : "+") + s;
  }

  /** Text for one electrode value: "+1.8" (z), "+2.3 dB" (relative), "6.1 dB" (SNR), "–" if none. */
  function formatValue(v, kind) {
    if (!isNum(v)) return "–";
    if (kind === "z") return signed(v);
    if (kind === "rel") return signed(v) + " dB";
    return signed(v).replace("+", "") + " dB";
  }

  const LEGEND = {
    z: { range: Z_RANGE, unit: "z", text: "z-score against your rest baseline: blue below it, red above it" },
    rel: { range: REL_DB_RANGE, unit: "dB", text: "Baseline still collecting: dB relative to the mean across electrodes, not a z-score" },
    snr: { range: SNR_RANGE, unit: "dB", text: "SSVEP signal-to-noise: power at the flicker frequency over the neighbouring bins, dB" },
  };

  const STATUS_TEXT = {
    connecting: "Connecting to the map server…", connected: "Connected, waiting for the board",
    starting: "Starting", streaming: "Streaming", "no-data": "No data from the board",
    ended: "Replay ended", error: "Error", disconnected: "Disconnected, reconnecting…",
    closed: "Disconnected",
  };

  function headGeometry(S) { return { cx: S / 2, cy: S * 0.53, R: S * 0.39 }; }

  function drawOutline(ctx, g, lw) {
    const { cx, cy, R } = g;
    ctx.strokeStyle = OUTLINE;
    ctx.lineWidth = lw;
    ctx.beginPath();
    ctx.arc(cx, cy, R, 0, 2 * Math.PI);
    ctx.stroke();
    ctx.beginPath();                                   // nose, at the top
    ctx.moveTo(cx - 0.11 * R, cy - 0.994 * R);
    ctx.lineTo(cx, cy - 1.13 * R);
    ctx.lineTo(cx + 0.11 * R, cy - 0.994 * R);
    ctx.stroke();
    for (const s of [-1, 1]) {                         // ears
      ctx.beginPath();
      ctx.ellipse(cx + s * 1.035 * R, cy, 0.06 * R, 0.17 * R, 0, -Math.PI / 2, Math.PI / 2, s < 0);
      ctx.stroke();
    }
  }

  function pill(ctx, x, y, lines, S) {
    const big = S * 0.025, small = S * 0.02, pad = S * 0.008;
    ctx.font = "600 " + big + "px system-ui, -apple-system, sans-serif";
    const w0 = ctx.measureText(lines[0]).width;
    ctx.font = small + "px system-ui, -apple-system, sans-serif";
    const w1 = lines[1] ? ctx.measureText(lines[1]).width : 0;
    const w = Math.max(w0, w1) + 2 * pad, h = big + (lines[1] ? small + pad * 0.5 : 0) + 2 * pad;
    ctx.fillStyle = "rgba(11, 12, 16, 0.78)";
    ctx.fillRect(x - w / 2, y, w, h);
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillStyle = "#eceef4";
    ctx.font = "600 " + big + "px system-ui, -apple-system, sans-serif";
    ctx.fillText(lines[0], x, y + pad);
    if (lines[1]) {
      ctx.fillStyle = "#a4a9b8";
      ctx.font = small + "px system-ui, -apple-system, sans-serif";
      ctx.fillText(lines[1], x, y + pad + big + pad * 0.5);
    }
  }

  /**
   * Draw one head on a canvas. head: {canvas, ctx, off, offCtx, img, S, text}. electrodes:
   * [{name, region, x, y, value, quality}]; scale maps a value to [r, g, b]. stale draws the
   * map and dots faded, with "–" for every value: the last frame, no longer live.
   */
  function drawHead(head, electrodes, scale, kind, stale) {
    const { ctx, S } = head;
    const g = headGeometry(S);
    ctx.globalAlpha = 1;
    ctx.clearRect(0, 0, S, S);
    ctx.fillStyle = HEAD_BG;
    ctx.beginPath();
    ctx.arc(g.cx, g.cy, g.R, 0, 2 * Math.PI);
    ctx.fill();
    if (stale) ctx.globalAlpha = STALE_ALPHA;
    if (electrodes.length) {
      const pts = electrodes.map((e) => [e.x, e.y]);
      const vals = electrodes.map((e) => e.value);
      const fade = head.fade || (head.fade = new Float32Array(GRID * GRID));
      const cells = grid(pts, vals, GRID, 1 + 3 / GRID, MAX_REACH, fade);
      const data = head.img.data;
      for (let k = 0; k < cells.length; k++) {
        const c = Number.isNaN(cells[k]) ? null : scale(cells[k]);
        data[4 * k] = c ? c[0] : 0;
        data[4 * k + 1] = c ? c[1] : 0;
        data[4 * k + 2] = c ? c[2] : 0;
        data[4 * k + 3] = c ? Math.round(255 * fade[k]) : 0;
      }
      head.offCtx.putImageData(head.img, 0, 0);
      ctx.save();
      ctx.beginPath();
      ctx.arc(g.cx, g.cy, g.R, 0, 2 * Math.PI);
      ctx.clip();
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(head.off, g.cx - g.R, g.cy - g.R, 2 * g.R, 2 * g.R);
      ctx.restore();
    }
    drawOutline(ctx, g, Math.max(1.5, S * 0.006));
    const r = Math.max(3, S * (head.text ? 0.017 : 0.022));
    for (const e of electrodes) {
      const x = g.cx + e.x * g.R, y = g.cy - e.y * g.R;
      const c = scale(e.value);
      ctx.beginPath();
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      if (c && !isBad(e.quality)) {
        // Its own colour inside a light ring with a dark centre, so a good site reads as a
        // measured point even where it matches the map around it, or the dark z = 0 colour.
        ctx.fillStyle = "rgb(" + c.join(",") + ")";
        ctx.fill();
        ctx.strokeStyle = RING;
        ctx.lineWidth = Math.max(1, S * 0.004);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(x, y, Math.max(1, r * 0.28), 0, 2 * Math.PI);
        ctx.fillStyle = "#0b0c10";
        ctx.fill();
      } else {
        ctx.strokeStyle = BAD;                          // hollow grey and struck through: no value
        ctx.lineWidth = Math.max(1.5, S * 0.006);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(x - r * 0.7, y + r * 0.7);
        ctx.lineTo(x + r * 0.7, y - r * 0.7);
        ctx.stroke();
      }
      if (head.text) {
        const second = stale ? e.region : isBad(e.quality) ? e.quality + " · " + e.region :
          noBaseline(e, kind) ? NO_BASELINE + " · " + e.region : e.region;
        const first = e.name + "  " + (stale ? "–" : isBad(e.quality) ? "—" : formatValue(e.value, kind));
        pill(ctx, x, y + r + S * 0.006, [first, second], S);
      }
    }
    ctx.globalAlpha = 1;
  }

  // css is the CSS width used when the canvas has no layout yet (a fake DOM, display: none).
  function makeHead(doc, canvas, css, text) {
    canvas.width = css;
    canvas.height = css;
    const off = doc.createElement("canvas");
    off.width = GRID;
    off.height = GRID;
    const offCtx = off.getContext("2d");
    return { canvas, ctx: canvas.getContext("2d"), off, offCtx, img: offCtx.createImageData(GRID, GRID), S: css, css, text };
  }

  function h(doc, tag, attrs, kids) {
    const e = doc.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "text") e.textContent = v;
      else if (k === "className") e.className = v;
      else e.setAttribute(k, v);
    }
    for (const kid of kids || []) e.appendChild(kid);
    return e;
  }

  /**
   * Build the page inside root and connect to the server. opts: doc, win (default globals),
   * root (default #brainmap or body), url (default "events"), EventSource (default
   * win.EventSource; false to not connect). Returns { handle(msg), select(viewId), destroy(),
   * state() }; handle() is what the EventSource feeds, exposed for tests.
   */
  function create(opts) {
    opts = opts || {};
    const doc = opts.doc || root.document, win = opts.win || root;
    const host = opts.root || (doc.getElementById && doc.getElementById("brainmap")) || doc.body;
    const ES = opts.EventSource === undefined ? win.EventSource : opts.EventSource;
    const warn = (m) => { if (win.console) win.console.warn("[BrainSync map] " + m); };

    // stale: the frame on screen is no longer live (cleared by the next frame). terminal: the
    // server said it is done ("ended", "error"), the EventSource is closed and stays closed.
    const state = {
      hello: null, frame: null, view: "alpha", status: "connecting", problem: "", dropped: 0,
      draws: 0, timeline: [], legendKind: null, stale: false, terminal: false,
    };

    // Static skeleton.
    const title = h(doc, "h1", { text: "BrainSync · live scalp map" });
    const label = h(doc, "p", { className: "label", "data-role": "label", text: DEFAULT_LABEL });
    const source = h(doc, "p", { className: "source", "data-role": "source", text: "" });
    const status = h(doc, "p", { className: "status", role: "status", "aria-live": "polite", "data-role": "status" });
    const viewTitle = h(doc, "h2", { className: "view-title", "data-role": "view-title" });
    const baseline = h(doc, "p", { className: "baseline", "data-role": "baseline" });
    baseline.hidden = true;
    const mainCanvas = h(doc, "canvas", { className: "main", role: "img", "data-role": "main" });
    const bar = h(doc, "canvas", { className: "bar", "aria-hidden": "true" });
    const lmin = h(doc, "span", { "data-role": "legend-min" });
    const lmid = h(doc, "span", { "data-role": "legend-mid" });
    const lmax = h(doc, "span", { "data-role": "legend-max" });
    const legendText = h(doc, "p", { className: "legend-text", "data-role": "legend" });
    const legend = h(doc, "div", { className: "legend" }, [bar, h(doc, "div", { className: "ticks" }, [lmin, lmid, lmax]), legendText]);
    const thumbs = h(doc, "div", { className: "thumbs", role: "group", "aria-label": "Bands" });
    const ssvepBtns = h(doc, "div", { className: "ssvep-views", role: "group", "aria-label": "SSVEP" });
    const tbody = h(doc, "tbody", { "data-role": "electrodes" });
    const table = h(doc, "table", { className: "electrodes" }, [
      h(doc, "caption", { text: "Electrodes" }),
      h(doc, "thead", {}, [h(doc, "tr", {}, ["Site", "Region", "Value", "Contact"].map((t) => h(doc, "th", { scope: "col", text: t })))]),
      tbody,
    ]);
    const tlCanvas = h(doc, "canvas", { className: "timeline", role: "img", "data-role": "timeline" });
    const tlNow = h(doc, "p", { className: "timeline-now", "data-role": "timeline-now", text: "" });
    const limits = h(doc, "p", { className: "limits", text:
      "Colours are interpolated between 8 electrodes on the scalp. They show which part of the head " +
      "carries which rhythm, not where inside the brain it comes from: that needs dozens of electrodes " +
      "and a head model. Blinks, jaw and neck muscles show up too, mostly on Fp1/Fp2 and P7/P8." });

    const kids = [
      h(doc, "header", { className: "top" }, [title, label, h(doc, "div", { className: "meta" }, [status, source])]),
      h(doc, "section", { className: "stage" }, [
        h(doc, "div", { className: "mainwrap" }, [viewTitle, baseline, mainCanvas, legend]),
        h(doc, "aside", { className: "side" }, [
          thumbs, ssvepBtns, table,
          h(doc, "div", { className: "timeline-box" }, [
            h(doc, "h2", { text: "Occipital alpha, last 60 s (mean O1/O2, z)" }),
            tlCanvas, tlNow,
          ]),
        ]),
      ]),
      h(doc, "footer", { className: "limits-box" }, [limits]),
    ];
    for (const k of kids) host.appendChild(k);

    const main = makeHead(doc, mainCanvas, 640, true);
    bar.width = 240;
    bar.height = 12;
    const barCtx = bar.getContext("2d");
    tlCanvas.width = 600;
    tlCanvas.height = 120;
    const tlCtx = tlCanvas.getContext("2d");

    let views = [], thumbHeads = new Map(), buttons = new Map(), rows = [];

    // Backing stores follow the CSS size times devicePixelRatio, so text and lines stay sharp on
    // HiDPI screens; re-fitted on window resize (redrawn at once, no rAF). Setting a canvas size
    // clears it, so only a changed size is written.
    function dpr() { return isNum(win.devicePixelRatio) && win.devicePixelRatio > 0 ? win.devicePixelRatio : 1; }
    function backing(el, css) {
      const w = isNum(el.clientWidth) && el.clientWidth > 0 ? el.clientWidth : css;
      return Math.max(1, Math.min(MAX_BACKING, Math.round(w * dpr())));
    }
    function setSize(el, w, hgt) {
      if (el.width === w && el.height === hgt) return false;
      el.width = w;
      el.height = hgt;
      return true;
    }
    function fitHead(head) {
      const S = backing(head.canvas, head.css);
      setSize(head.canvas, S, S);
      head.S = S;
    }
    function fitAll() {
      fitHead(main);
      for (const head of thumbHeads.values()) fitHead(head);
      const W = backing(tlCanvas, 600);
      setSize(tlCanvas, W, Math.max(1, Math.round(W / 5)));
      const bh = Math.max(1, Math.round((isNum(bar.clientHeight) && bar.clientHeight > 0 ? bar.clientHeight : 12) * dpr()));
      if (setSize(bar, backing(bar, 240), bh)) state.legendKind = null;
    }

    function buildViews() {
      views = viewsFor(state.hello);
      thumbHeads = new Map();
      buttons = new Map();
      thumbs.replaceChildren();
      ssvepBtns.replaceChildren();
      views.forEach((v, k) => {
        const key = String(k + 1);
        if (v.kind === "band") {
          const c = h(doc, "canvas", { "aria-hidden": "true" });
          const b = h(doc, "button", { type: "button", className: "thumb", "data-view": v.id,
            "aria-keyshortcuts": key, title: v.title + " (key " + key + ")" },
            [c, h(doc, "span", { className: "name", text: key + " · " + v.name }),
              h(doc, "span", { className: "range", text: v.range })]);
          thumbHeads.set(v.id, makeHead(doc, c, 100, false));
          buttons.set(v.id, b);
          thumbs.appendChild(b);
        } else {
          const b = h(doc, "button", { type: "button", className: "ssvep-view", "data-view": v.id,
            "aria-keyshortcuts": key, text: key + " · " + v.title });
          buttons.set(v.id, b);
          ssvepBtns.appendChild(b);
        }
      });
      for (const [id, b] of buttons) b.addEventListener("click", () => select(id));
      if (!views.some((v) => v.id === state.view)) state.view = views.some((v) => v.id === "alpha") ? "alpha" : views[0].id;
      fitAll();                                        // the new thumbnails are laid out now
    }

    function buildRows() {
      tbody.replaceChildren();
      rows = state.hello.channels.map((name) => {
        const value = h(doc, "td", { className: "value", text: "–" });
        const contact = h(doc, "td", { className: "contact", text: "–" });
        const tr = h(doc, "tr", { "data-ch": name }, [
          h(doc, "th", { scope: "row", text: name }),
          h(doc, "td", { className: "region", text: state.hello.regions[name] || "" }),
          value, contact,
        ]);
        tbody.appendChild(tr);
        return { tr, value, contact };
      });
    }

    // A stale status redraws so the heads dim and the values go to "–" at once, and leaves a gap
    // marker in the timeline so its line does not join the frames either side of the outage.
    function setStatus(kind, text) {
      state.status = kind;
      status.setAttribute("data-state", kind);
      status.textContent = (STATUS_TEXT[kind] || kind) + (text ? " · " + text : "");
      if (STALE_STATES.indexOf(kind) < 0) return;
      state.stale = true;
      const tl = state.timeline;
      if (tl.length && tl[tl.length - 1].v !== null) tl.push({ t: tl[tl.length - 1].t, v: null });
      render();
    }

    function currentView() { return views.find((v) => v.id === state.view) || views[0]; }

    function electrodesFor(values) {
      const hello = state.hello, f = state.frame;
      return hello.channels.map((name, i) => ({
        name, region: hello.regions[name] || "", x: hello.positions[name][0], y: hello.positions[name][1],
        value: values ? values[i] : null, quality: f ? f.quality[i] : "ok",
      }));
    }

    function drawLegend(kind) {
      if (state.legendKind === kind) return;
      state.legendKind = kind;
      const L = LEGEND[kind], scale = scaleFor(kind);
      for (let x = 0; x < bar.width; x++) {
        const v = L.range[0] + (x + 0.5) / bar.width * (L.range[1] - L.range[0]);
        barCtx.fillStyle = "rgb(" + scale(v).join(",") + ")";
        barCtx.fillRect(x, 0, 1, bar.height);
      }
      const end = (v) => (kind === "snr" ? String(v) : signed(v).replace(".0", "")) + " " + L.unit;
      lmin.textContent = end(L.range[0]);
      lmid.textContent = kind === "snr" ? "" : "0";
      lmax.textContent = end(L.range[1]);
      legendText.textContent = L.text;
    }

    function drawTimeline() {
      const W = tlCanvas.width, H = tlCanvas.height, ctx = tlCtx, px = H / 120;
      ctx.clearRect(0, 0, W, H);
      const yOf = (v) => H / 2 - Math.max(-TIMELINE_Z, Math.min(TIMELINE_Z, v)) / TIMELINE_Z * (H / 2 - 4 * px);
      ctx.lineWidth = px;
      ctx.strokeStyle = "#2c303c";
      for (const z of [-2, 0, 2]) {
        ctx.beginPath();
        ctx.moveTo(0, yOf(z));
        ctx.lineTo(W, yOf(z));
        ctx.stroke();
      }
      const pts = state.timeline;
      if (!pts.length) return;
      const tNew = pts[pts.length - 1].t;
      const hop = isNum(state.hello.hop_s) ? state.hello.hop_s : DEFAULT_HOP_S;
      ctx.strokeStyle = "#f4a582";
      ctx.lineWidth = 2 * px;
      ctx.beginPath();
      for (const run of timelineRuns(pts, 2 * hop)) {
        run.forEach((p, k) => {
          const x = W * (1 - (tNew - p.t) / TIMELINE_S), y = yOf(p.v);
          if (k) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        });
      }
      ctx.stroke();
    }

    function occipital() {
      const hello = state.hello;
      const idx = hello.channels.map((c, i) => (hello.regions[c] === "visual" || /^O\d/.test(c) ? i : -1)).filter((i) => i >= 0);
      return idx;
    }

    function pushTimeline(f) {
      const tl = state.timeline;
      if (tl.length && f.t < tl[tl.length - 1].t) tl.length = 0;    // a restarted stream
      let v = null;
      if (f.baseline.state === "ready" && f.z.alpha) {
        const zs = occipital().filter((i) => !isBad(f.quality[i])).map((i) => f.z.alpha[i]).filter(isNum);
        if (zs.length) v = zs.reduce((a, b) => a + b, 0) / zs.length;
      }
      tl.push({ t: f.t, v });
      while (tl.length && tl[0].t < f.t - TIMELINE_S) tl.shift();
      if (f.baseline.state !== "ready") tlNow.textContent = "Starts once the baseline is ready";
      else tlNow.textContent = v === null ? "Now: no occipital value" : "Now: " + signed(v) + " z · close your eyes and it should rise";
    }

    function render() {
      const hello = state.hello, f = state.frame;
      if (!hello) return;
      state.draws++;
      const view = currentView();
      for (const [id, b] of buttons) b.setAttribute("aria-pressed", id === view.id ? "true" : "false");
      viewTitle.textContent = view.title;
      let vv = null;
      if (f) vv = viewValues(f, view);
      const kind = vv ? vv.kind : view.kind === "ssvep" ? "snr" : "z";
      const stale = Boolean(f && state.stale);
      host.setAttribute("data-stale", stale ? "true" : "false");
      const els = electrodesFor(vv && vv.values);
      drawHead(main, els, scaleFor(kind), kind, stale);
      drawLegend(kind);
      for (const [id, head] of thumbHeads) {
        const tv = f ? viewValues(f, views.find((v) => v.id === id)) : null;
        drawHead(head, electrodesFor(tv && tv.values), scaleFor(tv ? tv.kind : "z"), tv ? tv.kind : "z", stale);
      }
      if (stale) {
        mainCanvas.setAttribute("aria-label", view.title + " scalp map: no live data (" +
          (STATUS_TEXT[state.status] || state.status) + "). The last frame is shown dimmed; its values are hidden.");
      } else {
        const desc = els.map((e) => e.name + " " + (isBad(e.quality) ? e.quality :
          noBaseline(e, kind) ? NO_BASELINE : formatValue(e.value, kind)));
        mainCanvas.setAttribute("aria-label", view.title + " scalp map (" + LEGEND[kind].text + "): " + desc.join(", "));
      }
      els.forEach((e, i) => {
        const row = rows[i];
        if (!row) return;
        const live = f && !stale;
        row.value.textContent = !live ? "–" : isBad(e.quality) ? "—" :
          noBaseline(e, kind) ? NO_BASELINE : formatValue(e.value, kind);
        row.contact.textContent = live ? e.quality : "–";
        row.tr.setAttribute("data-quality", live ? e.quality : "");
      });
      if (stale) tlNow.textContent = "No live data";
      if (f && !stale && f.baseline.state === "collecting") {
        baseline.hidden = false;
        baseline.textContent = "Sit still, eyes open — baseline " + Math.floor(f.baseline.elapsed_s) +
          " / " + Math.round(f.baseline.seconds) + " s";
      } else {
        baseline.hidden = true;
        baseline.textContent = "";
      }
      drawTimeline();
      tlCanvas.setAttribute("aria-label", "Occipital alpha z over the last 60 s. " + tlNow.textContent);
    }

    function select(id) {
      if (!views.some((v) => v.id === id) || id === state.view) return;
      state.view = id;
      render();
    }

    // Every reconnect starts with a hello. If it describes the same map, the last frame stays
    // up (still stale until a new frame) instead of every head going blank; otherwise the views
    // are rebuilt and the old frame, which no longer fits, is dropped.
    function onHello(m) {
      const prev = state.hello;
      const keep = sameLayout(prev, m);
      state.hello = m;
      if (!keep) {
        state.frame = null;
        state.stale = false;
        if (!prev || prev.channels.join() !== m.channels.join()) state.timeline = [];
      }
      label.textContent = m.label;
      source.textContent = "Source: " + m.source + " · " + m.fs + " Hz · " + m.window_s + " s window every " + m.hop_s + " s";
      if (!keep) buildViews();
      buildRows();
      render();
    }

    /** Feed one parsed message; returns false (and counts it) if it is unusable. */
    function handle(msg) {
      const problem = messageProblem(msg, state.hello);
      if (problem) {
        state.dropped++;
        state.problem = problem;
        warn(problem);
        return false;
      }
      if (msg.type === "hello") onHello(msg);
      else if (msg.type === "status") {
        if (TERMINAL_STATES.indexOf(msg.state) >= 0) {
          // The server is done: without this the browser would reconnect and its "Disconnected,
          // reconnecting…" would overwrite "Replay ended" or the error text.
          state.terminal = true;
          if (es) es.close();
        }
        setStatus(msg.state, msg.text);
      } else {
        state.frame = msg;
        state.stale = false;
        pushTimeline(msg);
        render();
      }
      return true;
    }

    function onResize() {
      fitAll();
      render();
    }

    function onKey(e) {
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      const k = Number(e.key);
      if (Number.isInteger(k) && k >= 1 && k <= views.length) {
        select(views[k - 1].id);
        if (e.preventDefault) e.preventDefault();
      }
    }

    let es = null;
    buildViews();
    setStatus("connecting", "");
    doc.addEventListener("keydown", onKey);
    if (win.addEventListener) win.addEventListener("resize", onResize);

    if (ES) {
      es = new ES(opts.url || "events");
      es.onopen = () => setStatus("connected", "");
      es.onerror = () => {
        if (state.terminal) return;
        setStatus(es.readyState === 2 ? "closed" : "disconnected", "");
      };
      es.onmessage = (e) => {
        let msg;
        try { msg = JSON.parse(e.data); } catch (err) {
          state.dropped++;
          state.problem = "message is not JSON";
          warn(state.problem);
          return;
        }
        handle(msg);
      };
    }

    function destroy() {
      if (es) es.close();
      doc.removeEventListener("keydown", onKey);
      if (win.removeEventListener) win.removeEventListener("resize", onResize);
      host.replaceChildren();
    }

    return {
      handle, select, destroy,
      state: () => Object.assign({}, state, { timeline: state.timeline.slice() }),
    };
  }

  const api = Object.freeze({
    FORMAT, GRID, Z_RANGE, REL_DB_RANGE, SNR_RANGE, TIMELINE_S, STATUS_STATES, STALE_STATES,
    TERMINAL_STATES, MAX_REACH, DIVERGING, SEQUENTIAL, HEAD_BG,
    colour, sequential, idw, interpolate, timelineRuns, ssvepKeys, sameLayout, messageProblem,
    viewsFor, viewValues, formatValue, create,
  });
  root.BrainSyncMap = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;

  // index.html marks its <script> with data-autostart, so the page needs no inline script.
  const cur = root.document && root.document.currentScript;
  if (cur && cur.hasAttribute && cur.hasAttribute("data-autostart")) create();
})(typeof globalThis !== "undefined" ? globalThis : this);
