# Live scalp map

A live map of your own EEG: the Cerelog X8's 8 electrodes (Fp1 Fp2 C3 C4 P7 P8 O1 O2) drawn
on a head, updated 4 times a second. `python -m brainsync.brainmap` reads the board, computes
band power, SSVEP SNR and contact quality per electrode over the last 2 s, and streams them as
Server-Sent Events to this page. It replaces the simulated TRIBE panel for anything that should
show *your* signal; the TRIBE heatmap (`extension/heatmap/`) stays as the offline prediction.

## Run it

```bash
python -m brainsync.brainmap --open                     # fake board, http://127.0.0.1:8140/
python -m brainsync.brainmap --board cerelog --open     # the headset (serial port auto-detected)
python -m brainsync.brainmap --board cerelog --serial-port /dev/cu.usbserial-XXXX
python -m brainsync.brainmap --board replay --recording "$BRAINSYNC_DATA_DIR"/2026-10-22_151304*.npz --speed 1 --loop
python -m brainsync.brainmap --baseline-s 20 --seconds 120
python -m brainsync.brainmap --host 0.0.0.0             # headless Jetson, viewed from a laptop on the LAN
```

Use the venv that has numpy and scipy (`.venv-tribe/bin/python` on the M4). The page is plain
HTML with no dependencies and must be served by that process (it connects to `/events`), so do
not open `index.html` as a file. Only one process can hold the board: close any other BrainSync
tool that uses the headset first. `--host 0.0.0.0` lets anyone on that network watch.

## What the page shows

- **Large head**, nose up, ears at the sides: the selected view, interpolated between the
  electrodes (inverse-distance weighting on a 64x64 grid, clipped to the head circle). Each
  electrode has a dot with its name, region and value. A part of the head is only coloured
  when its nearest electrode is good and within 0.5 head radii; elsewhere it stays the plain
  head background. So a bad or missing electrode's region is blank rather than painted with
  values from the far side of the head. Even with all 8 good, the rims at T7/T8 (no electrode
  there, about 6% of the head) stay blank.
- **Four small heads**: delta 1-4, theta 4-8, alpha 8-13 and beta 13-30 Hz. Click one, or press
  **1-4**, to make it the large view.
- **SSVEP 15 Hz / 17 Hz** (keys **5** and **6**): signal-to-noise at the flicker frequency, in
  dB against the neighbouring bins of the spectrum averaged over the last 4 s, on a
  dark-to-bright scale from 0 to 12 dB. Read it as a
  comparison, not against a fixed dB threshold: looking at the 15 Hz square should raise O1/O2
  in the 15 Hz view and not in the 17 Hz view, and above what the same electrodes show with
  the eyes open and no flicker. The absolute numbers depend on how the
  stream estimates the noise floor.
- **Band views** use a diverging scale from -3 to +3 with a dark middle: a z-score of that
  electrode's band power against your own rest baseline. Near 0 (rest) the head stays dark,
  close to its background; it turns brighter blue below the baseline and brighter red above it.
- **Baseline**: for the first `--baseline-s` seconds (30 by default) the page says
  "Sit still, eyes open — baseline 12 / 30 s". Until it is ready there are no z-scores, so the
  band views show each electrode's dB relative to the mean across electrodes (-6..+6 dB) and the
  legend says so. Blinks and movements (over 150 µV peak to peak, and the second after) are
  left out of the baseline. An electrode that was off or bad at some point during the rest needs
  a full `--baseline-s` of clean signal of its own afterwards: until then it reads
  "no baseline yet" and its region stays blank.
- **Contact**: an electrode whose quality is not `ok` (flat, noisy, clipped, offset) is drawn as
  a hollow, struck-through grey ring with its quality text, and is left out of the
  interpolation, so a loose contact never paints its region. The electrode table lists every
  site's region, value and contact.
- **Occipital alpha timeline**: the mean O1/O2 alpha z over the last 60 s. Close your eyes and
  it should rise within a second or two (the Berger effect).
- **Status line**: connecting, streaming, no data from the board, replay ended, error,
  disconnected. After a disconnect the browser's EventSource reconnects by itself when the
  server comes back; after "replay ended" or an error the page stops listening (reload to
  start again), so the reconnect message never hides them.
- **No live data**: when the stream stops (disconnected, no data, error, replay ended) the last
  frame stays on screen dimmed, every value and contact reads "–", the timeline breaks its
  line, and screen readers hear "no live data". The next frame brings it back. A reconnect to
  the same stream (same electrodes, positions, bands and SSVEP keys) keeps that frame up until
  the next one, instead of blanking the heads.

## Honest limits

This is a scalp map from 8 electrodes, not a brain scan. The colours between the dots are
interpolated, not measured, and they say which part of the head carries which rhythm, not where
inside the brain it comes from: placing activity in the cortex needs dozens of electrodes and a
head model. The regions are the usual labels for those sites (prefrontal, sensorimotor,
temporal-parietal, visual), not localised sources. Eye blinks dominate Fp1/Fp2 and jaw or neck
muscle shows up as broadband beta on P7/P8: these are artifacts and the map shows them as such.
A z-score is only as good as the baseline, so record it sitting still with the eyes open.

## Keep it on its own screen for now

The map is not shown to be safe beside the 15/17 Hz flicker squares: a moving, coloured patch in
peripheral vision could change the SSVEP response or attention. Keep it on a separate screen
(or a separate device, via `--host 0.0.0.0`) until a decoder-accuracy A/B with the headset,
map on screen versus off, says otherwise.

It is built to be quiet anyway: there is no `requestAnimationFrame` loop, no timer and no CSS
animation or transition. It redraws only when a frame message arrives (4 Hz) or the window is
resized, the Python side smooths the values, the 64x64 grid is scaled up with image smoothing,
and a resting head is dark, so colours drift instead of jumping. The baseline banner keeps its
space when it hides, so the head never moves. Canvases are drawn at the screen's
`devicePixelRatio`, so text stays sharp on HiDPI displays.

## Headset checks

Offline first. A recording held in one state the whole time (all eyes closed, say) shows alpha z
near 0: the baseline is the first `--baseline-s` seconds of that same state, so there is
nothing to rise above. The Berger check needs eyes-open then eyes-closed periods in one run,
with the baseline taken during the eyes-open part, or the fake board (`python -m
brainsync.brainmap`), which is plain noise through the baseline and then puts a 10 Hz tone on
the posterior sites. For a single-state run such as the eyes-closed 2026-10-22_151304, read
the alpha view while the baseline is still collecting: its "dB relative to the mean across
electrodes" shows the occipital alpha excess of O1/O2 over the other sites (give a
`--baseline-s` as long as the run to keep that view up). Then replay a cued run from `$BRAINSYNC_DATA_DIR` for the SSVEP views, and with
the headset on:

| Action | Expected on the map |
|---|---|
| Eyes closed 20 s / open 20 s, x3 | O1/O2 alpha z rises with eyes closed (Berger effect) |
| Blink hard 5x | Fp1/Fp2 delta spikes |
| Clench jaw | broadband beta on P7/P8 (muscle) |
| Look at the 15 Hz square | O1/O2 15 Hz SNR up compared with the 17 Hz view and with eyes open on no flicker |
| Squeeze right hand | C3 alpha/mu drops more than C4 (weakest; may not show) |

## Files

- `index.html`: the page and its styles; loads `map.js`, which builds the page and connects with
  `new EventSource("events")`.
- `map.js`: UMD global `BrainSyncMap`, no dependencies. Pure helpers `interpolate`, `idw`,
  `colour`, `sequential`, `timelineRuns`, `ssvepKeys`, `messageProblem`, `viewValues`,
  `formatValue`, and `create()` for the page. Messages that fail `messageProblem` are dropped
  with a console warning. SSVEP values are looked up by `hello.ssvep_keys` (the exact keys of
  `frame.ssvep`, in `ssvep_hz` order), or by `String(Hz)` for a hello without them.

## Tests

```bash
node --test 'extension/brainmap/test/*.test.mjs'
```

`map.test.mjs` covers the interpolation (exact at each electrode, within the valid range, NaN
outside the head, blank where the nearest electrode is bad or out of reach), the colour scales
(dark diverging middle), the message checks including `ssvep_keys`, the timeline gaps, and
`create()` driven through the small fake DOM in `fakedom.mjs`: electrode text, bad channels,
the baseline banner, the status line, the dimmed no-live-data state, reconnects that keep the
frame, terminal statuses closing the stream, HiDPI canvas sizes, key and click view selection.
`spyTimers()` replaces the global `requestAnimationFrame`, `setTimeout` and `setInterval` while
frames stream and checks nothing was scheduled, and a static check fails if `index.html` or
`map.js` uses CSS animation, transition or `@keyframes`.

`fixture.test.mjs` feeds the page what Python really sends: `frames_fixture.json` holds the
hello, the statuses and 10 frames of a 14 s FakeBoard session replayed through
`brainsync.brainmap.stream` (collecting and ready baseline; Fp2 flat, C3 clipped and P7 noisy
from 10 s on). Every message must pass `messageProblem()` and render through `create()`, and
the drawn ImageData must match an interpolation that leaves the bad electrodes out. Rebuild the
fixture after changing the stream (`tests/test_brainmap.py` fails while it is stale):

```bash
.venv-tribe/bin/python extension/brainmap/test/make_fixture.py           # rewrite
.venv-tribe/bin/python extension/brainmap/test/make_fixture.py --check   # exit 1 if stale
```
