# Heatmap panel

A side panel for the Reels demo: the precomputed Meta TRIBE v2 prediction of an average
brain's fMRI response to the reel on screen. It is decoration only. It never talks to the
decoder or the bridge, and every frame and the panel label say "Not your EEG".

The frames come from `brainsync.heatmap` (see "Predicted-brain heatmap" in the main README):

```
clips/index.json               {"format": 1, "clips": [{"clip_id": "beach", "manifest": "beach/manifest.json"}]}
clips/<id>/manifest.json       frame_times_s, frames, vmax, label, credit, model
clips/<id>/frames/0000.png     one 480x400 frame per second of video
clips/<id>/clip.mp4            the clip itself, for the demo page (only clips you own)
```

`clips/` is git-ignored: render it on the demo machine.

## Try it

```bash
python3 -m http.server -d extension 8000
open http://127.0.0.1:8000/heatmap/demo.html     # ?squares=0 hides the square outlines
```

`demo.html` plays the rendered clips in a 9:16 column with the panel in the left margin and
dashed outlines where `overlay.js` draws the 15/17 Hz squares. Nothing flickers on this page.

## Adding it to the Reels mock page

`panel.js` has no dependencies. In `extension/test/reels_mock.html`:

```html
<script src="../heatmap/panel.js"></script>
<script>
  const heatmap = BrainSyncHeatmap.create({ base: "../heatmap/clips/" });
  // whenever the reel on screen changes:
  heatmap.show(reel.querySelector("video"), reel.dataset.clipId || null);
</script>
```

Give each mock reel `data-clip-id` and a `<video src>` pointing at `../heatmap/clips/<id>/clip.mp4`.
The page must be served over http, not opened as a file, so `fetch` works. It is not wired
into the Instagram content scripts: on the live feed the next reel is almost never one of
ours, so the panel would only show its placeholder.

## How it keeps clear of the SSVEP stimulus

- **Place:** centred at x = -0.55 screen heights (the squares sit at +0.55), top at +0.45,
  0.40 wide. `layoutProblem()` hides the panel rather than let it come within 0.05 screen
  heights of a square's cue box or the reel column, or overlap the overlay HUD (bottom left).
  The default fits 1440x900 to 2560x1440 and the 16:10 MacBook screens.
- **Time:** there is no `requestAnimationFrame` loop. The panel updates on the video's
  `timeupdate`/`seeked` events, about 1 Hz worth of changes, and swaps two `<img>` layers
  with a 400 ms CSS opacity cross-fade that the compositor runs off the main thread.
- **Scene:** a reel without a prediction keeps the same dark backdrop with a placeholder
  line, so nothing appears or disappears after calibration.
- **Stacking:** z-index 2147483646, one under `overlay.js`, never above the squares.

Not measured yet: decoder accuracy and the overlay's dropped-frame count with the panel on
versus off, on the headset.

## Tests

```bash
node --test 'extension/heatmap/test/*.test.mjs'
```

`panel.test.mjs` covers frame lookup (the same table as `tests/test_heatmap.py`), the layout
keep-out and manifest checks. `controller.test.mjs` drives `create()` with a small fake DOM:
frames follow `currentTime`, one layer is shown at a time, a late manifest from the previous
reel is ignored, and a too-narrow screen hides the panel.
