# BrainScroll

A Chrome extension (Manifest V3) that scrolls Instagram Reels with the BrainSync c-VEP
standard (`docs/CVEP-STANDARD.md`). It draws three white squares on black, stacked to the
right of the reel: the top one, UP, goes to the previous reel; the middle one, PAUSE,
pauses or plays the reel showing; the bottom one, DOWN, goes to the next. All three show
one 63-bit m-sequence, each 21 frames (350 ms) behind the one above. The BrainSync decoder
(`python -m brainsync.reels`, `brainsync.vep.live.CodeVepDecoder`) trains on your own EEG
for about 32 s, then decides which square you are looking at and drives the extension
over a WebSocket on `ws://127.0.0.1:8765`. Under each square the overlay writes its label,
a confidence bar and the decoder's score.

`python -m brainsync.reels --paradigm freq` runs the older f-VEP pair instead: two grey
squares, "up" at 15 Hz and "down" at 17 Hz, decoded by `brainsync.online.Decoder` after a
rest baseline. The extension draws whichever the server's config asks for.

> **Photosensitivity warning.** Flicker between 15 and 25 Hz is the band most likely to
> trigger a seizure in people with photosensitive epilepsy. The coded squares change up to
> 60 times a second, which covers that band, and both f-VEP squares (15 and 17 Hz) lie
> inside it. Ask about epilepsy before every session. Stop at once if anyone feels
> unwell. The overlay shows this warning before anything flickers and draws nothing
> until you press Start. Escape stops it at any time.

## Install

1. Open `chrome://extensions` and turn on Developer mode (top right).
2. Click Load unpacked and pick this `extension/` folder.
3. Chrome 116 or newer is needed (`minimum_chrome_version`): from 116 on, WebSocket
   traffic keeps an MV3 service worker alive.

After editing any file here, press the reload arrow on the BrainScroll card and reload
the Instagram tab.

## Run

1. Set the display to 60 Hz with ProMotion off (System Settings > Displays). The overlay
   measures the refresh from `requestAnimationFrame` and turns the HUD red when it is more
   than 1 Hz off. The Python side decides whether to refuse.
2. Put Chrome in full screen (ctrl-cmd-F). Sizes and positions are in screen heights, and
   the overlay takes the window height as the screen height.
3. Open <https://www.instagram.com/reels/>. The warning appears once the page is on
   `/reels/` (or `/reel/<id>`). Read it, ask about epilepsy, press Start.
4. Start the Python side (`python -m brainsync.reels`; see its `--help`). It sends the
   config, then trains: the squares flicker with a white box around the top one and the
   banner says "training: keep looking at UP   cycle 12/30". Keep looking at the top
   square for the 30 cycles (about 32 s). The banner then shows the template's accuracy on
   held-out cycles against chance (33 %), and decoding starts: every cycle (1.05 s) the
   labels under the squares show each one's confidence, the banner the lead and the
   choice, and each commit scrolls up, scrolls down or pauses / plays the reel. With
   `--paradigm freq` it calibrates a rest baseline instead (squares flickering while you
   watch the reel, or held still with `--calibrate-on still`) and then scrolls.
5. Press Escape to stop: the flicker stops, the squares go away, and the server is told
   (`bye`) and ends the run. Reload the page to start again.

The squares never flicker without a server: if the connection drops (Python killed or
crashed, the extension reloaded) they are held still until a server sends a flickering
phase again. On any other Instagram page (Home, Messages) the squares are hidden and
scrolls are refused. Only a visible tab where Start was pressed scrolls.

The server is told whether the squares are in view: in the hello (`showing`) and in a
`{"type": "view", "showing": true|false}` message each time that changes (the page's
`visibilitychange`, leaving or coming back to Reels, the stop phase). While no tab shows
them the decoder's decisions are ignored, because with nothing on screen to look at they
are noise.

The HUD in the bottom-left corner shows the server connection, the phase and its prompt,
the measured refresh and dropped frames, the last scroll result, and the EEG board:
"EEG: not connected" (no USB receiver), "connecting…", "connected, waiting for data",
"connected, receiving data", "no data", "could not connect", or "not used" in `--manual`
mode. Everything but "receiving data" and "not used" is shown in red. Without a server
the EEG line reads "unknown". It never takes clicks and stays clear of the video.

To use another address: in the service worker console (chrome://extensions > BrainScroll
> service worker) run `chrome.storage.local.set({wsUrl: "ws://127.0.0.1:9000"})`.

## How it works

| File | Role |
|---|---|
| `manifest.json` | MV3 manifest |
| `flicker.js` | Exact port of `brainsync/stimulus/flicker.py` `luminance()` (f-VEP) |
| `cvep.js` | Byte-for-byte copy of `clients/js/cvep.js`: the standard's bits, frame rule, bar and label text |
| `scroller.js` | Finds the Reels scroll container and moves one reel, verified; pauses or plays the most visible video |
| `overlay.js` | Canvas squares and cue box, vsync frame counter, cycle reports, refresh meter, label lines, HUD, warning, protocol state machine |
| `content.js` | Instagram glue: port transport to the worker, debugger fallback for scrolling |
| `background.js` | Service worker: one WebSocket per Instagram tab; trusted key presses |
| `test/` | Mock Reels page, node tests, the flicker fixture and its generator |

In code mode frame n of target k is white when `bits[(n - lag_k) mod 63]` is 1 and black
when 0, one bit per frame; still squares are white. On every cycle's first frame
(n mod 63 == 0) the overlay sends `{"type": "cycle", "n": <cycle>, "t": <Unix s>}`, the
rAF timestamp of that frame on the wall clock (`Date.now() - performance.now()`, the max
over the last 64 frames, not `performance.timeOrigin`, which drifts after the Mac
sleeps). If that frame was dropped, the cycle is reported at its place on the vsync grid
when the overlay is at most 2 frames past it, and skipped after a longer stall. The
decoder cuts its epochs at these times on the board's clock; a constant offset (the flip
lands a frame after the rAF timestamp) is learnt into the template. A second tab showing
the squares sends its own cycles, which the server ignores while it follows the first.
Cycles must be within 5 s of the board's newest sample, so the browser and Python must
run on the same machine.

Flicker timing follows the PsychoPy stimulus: luminance depends on the frame index, never
on wall-clock time. The frame index is the vsync count since flicker onset,
`round((t - t0) / period)`, taken from rAF timestamps, so a dropped frame skips one index
instead of shifting every later frame. The period comes from the mean rAF interval over
120 frames after 20 warm-up frames (the same counts the stimulus uses), leaving out
intervals more than half a period from the median. Not the median itself: Chrome rounds
rAF timestamps to 100 us on pages that are not cross-origin isolated, such as
instagram.com, so 60 Hz intervals are 16.6 or 16.7 ms and their median reads 59.88 Hz.
While the measured refresh is more than the tolerance off, the squares are held still. Frames whose rAF
interval exceeds 1.5 periods count as dropped, and every 5 s of flicker the overlay
reports the refresh and drop count to the server.

The c-VEP squares are 0.2 screen heights at x = 0.58, y = +0.3, 0 and -0.3, right of a
9:16 video (which spans x = +/-0.28). Each label line sits under its square's cue box, in
the 0.1 between squares. Pause / play calls the most visible `<video>`'s own `pause()` or
`play()` and acks `{"type": "toggled", "id", "ok", "playing", "method": "video"}`; the
decoder fires it once per look (dwelling does not toggle it back and forth).

The cue box is a white outline 0.006 screen heights thick, 0.03 outside the square, or
closer where the screen edge leaves less room. With the reels layout (squares 0.3 high
at y = +/-0.325) only 0.025 is left above the top square and below the bottom one, so
the gap there is 0.019 and the outline stays on screen. A layout that does not fit even
with no gap logs a warning in the page console.

Scrolling: the feed container is the scrollable ancestor of the reel `<video>` elements
that snaps on the y axis (else the largest visible one, else the page). It is scrolled,
smoothly, so that the reel above or below the one showing takes its place, and it counts
only when another reel shows within 1.5 s: the URL changed (`/reels/<id>/`) or the most
visible video is another one. On instagram.com, scrolling one screen height instead
sometimes snapped back to the same reel and was still reported as a success. If no
other reel shows, the worker presses ArrowUp or ArrowDown as a trusted key through
`chrome.debugger`, and the ack says `debugger`. On the first reel an "up" is not tried
at all (`ok: false`), so the debugger is not attached for nothing.

The tab must be the active tab of a window that can be seen. Chrome marks a covered or
background tab hidden (also a window on another Space). A hidden tab neither flickers nor
scrolls (its ack is `ok: false`, method `none`), and the decoder pauses until it is back.
Put Chrome and the terminal side by side rather than switching between them: the tab only
needs to be visible, not focused.

## Permissions

- `debugger`: the fallback scroll. When scrolling the feed container does nothing,
  `chrome.debugger` sends a trusted ArrowUp/ArrowDown key press to the Instagram tab.
  It attaches only for that one key press and detaches straight away. Chrome shows its
  "started debugging this browser" bar meanwhile, which can briefly change the window
  height.
- `storage`: reads the optional `wsUrl` setting (`chrome.storage.local`).
- `https://www.instagram.com/*` (host permission): where the content scripts are
  injected. Nothing is read from the page except the layout of the feed.

`tabs` is not needed: the worker gets the tab id for the debugger from the content
script's port (`port.sender.tab.id`). The WebSocket to 127.0.0.1 needs no permission.

## Mock page

`test/reels_mock.html` is a stand-in for desktop Reels that needs no login: a centred 9:16
column of 20 coloured reels that snaps one per screen, each with a `<video>` element. It
loads `flicker.js`, `cvep.js`, `scroller.js` and `overlay.js` directly and talks to the
bridge over its own WebSocket (agent `page`, no debugger fallback). Its videos have
nothing loaded, so pause / play stops and restarts the reel's sweeping bar instead.

```bash
.venv/bin/python -m http.server -d extension 8000
# open http://127.0.0.1:8000/test/reels_mock.html  (add ?ws=ws://127.0.0.1:9000 for another port)
```

In the page console, `await BrainScrollScroller.scroll("down")` moves to the next reel
and `currentReel()` says which one is showing.

## Tests

```bash
node --test extension/test/*.test.mjs                 # flicker port + overlay/scroller logic
.venv/bin/python -m pytest -q tests/test_extension.py  # also runs the node tests
.venv/bin/python extension/test/make_fixture.py        # rebuild the flicker fixture
```

Node 24 reads `--test` arguments as globs, so pass the files, not the folder.
`cvep.test.mjs` covers code mode: the config, the per-frame bits against `cvep.js`, the
cycle reports and their times, training and confidence lines, pause / play, and that the
squares and labels fit beside the video. `tests/test_extension.py` also checks that
`cvep.js` is still the same bytes as `clients/js/cvep.js` and that the overlay's built-in
c-VEP layout matches the `reels_cvep` config.
`flicker.test.mjs` checks `flicker.js` against values computed by
`brainsync.stimulus.flicker.luminance` (15, 17 and 12 Hz, phases 0 and pi/2, frames
0..240, sine and square, modulation depths 1 and 0.5) to within 1e-9. `tests/test_extension.py` rebuilds that fixture
and compares it with the file, checks the manifest and the mock page, and runs
`node --check` on every script.
