# BrainPoint

A Chrome extension (Manifest V3) for clicking any web page with an EEG headset (SSVEP). It is
the `point` mode client of the BrainSync server (`PROTOCOL-v3.md` at the repo root). The
extension sends the server a screenshot of the tab. The server asks Claude Haiku where the
clickable elements are and shows the most likely ones as flickering patches over the page.
With the BrainSync c-VEP standard (`docs/CVEP-STANDARD.md`, the server's default paradigm
`code`) that is three white patches: the best candidate, the second and "More"; with a
frequency server (`--paradigm freq`) up to three buttons plus "More". When the decoder
decides which patch you are looking at, the server sends `click`, and the extension clicks
there. Risky buttons (pay, delete, send) ask for a second look at "Do it" first.

> **Photosensitivity warning.** Flicker between 15 and 25 Hz is the band most likely to
> trigger a seizure in people with photosensitive epilepsy. The c-VEP code switches a patch
> up to 30 times a second, and the frequency defaults (15, 17, 12 and 20 Hz) are mostly
> inside that band too. Ask about epilepsy before every session. Stop at
> once if anyone feels unwell. The overlay shows this warning and draws nothing until you
> press Start. Escape stops the flicker at any time, in any frame of the page; if it ever
> does not, Alt+Shift+B (or the toolbar icon) stops BrainPoint in the tab.

## Install

1. Open `chrome://extensions` and turn on Developer mode (top right).
2. Click Load unpacked and pick this `clients/chrome/` folder.
3. Chrome 116 or newer is needed (`minimum_chrome_version`): from 116 on, WebSocket
   traffic keeps an MV3 service worker alive.
4. Right-click the BrainPoint toolbar icon, then Options. Enter the server address
   (default `ws://127.0.0.1:8765/ws`) and the token that `python -m brainsync.server` printed
   at startup (leave it empty with `--no-auth`).

After editing any file here, press the reload arrow on the BrainPoint card and reload the tab.

## Run

1. Start the server: `python -m brainsync.server --board fake --no-auth` to try it without a
   headset, or with `--board cerelog` and a token for the real thing. Point mode needs
   `ANTHROPIC_API_KEY` on the server (the HUD says so when it is missing).
2. Set the display to 60 Hz with ProMotion off. The overlay measures the refresh from
   `requestAnimationFrame`; the server refuses a display more than 1 Hz off.
3. Open the page to control and click the BrainPoint toolbar icon (or press Alt+Shift+B).
   Read the warning, ask about epilepsy, press Start.
4. After about two seconds of refresh measurement the tab connects. The server calibrates
   (look at the screen and relax), then asks for a screenshot. While Haiku looks at it, the
   banner says so and nothing flickers. Then the patches appear. Look at one.
5. With `--board fake`, moving the mouse over a patch makes the fake board carry its tone,
   so the whole loop can be tried without a headset.
6. Press Escape to pause (the flicker stops at once and the server is told); press Resume
   to go on. Click the toolbar icon again to stop BrainPoint in this tab.

Clicking a link that loads a new page keeps BrainPoint running in that tab: the new page
starts again at once, without a second warning, and the server scans it. Only one tab runs
BrainPoint at a time. Starting it in a second tab while the first one runs shows "Another tab
or app is the controller" and a Take over button.

A hidden tab (another tab in front, a minimized window) is paused and never clicked, and its
screenshot waits until it shows again. The HUD in the bottom-left corner shows the phase,
the connection and round-trip time, the EEG board, the measured refresh and dropped frames,
the last scan and the last click.

## How it works

| File | Role |
|---|---|
| `manifest.json` | MV3 manifest |
| `esc.js` | The Esc key in every frame from `document_start`: pauses even when an iframe has the focus |
| `flicker.js` | Frame-indexed luminance, a byte-identical copy of `clients/js/flicker.js` |
| `brainsync-client.js` | The client SDK (`FlickerState`, `Connection`, `RefreshMeter`), a byte-identical copy of `clients/js/brainsync-client.js` |
| `cvep.js` | The c-VEP standard (63-bit code, frame rule, confidence text), a byte-identical copy of `clients/js/cvep.js` |
| `hints.js` | The page's visible clickable elements and the password fields to black out |
| `clicker.js` | `clickAt(x, y)`: focus and pointer/mouse events at a viewport point |
| `point_overlay.js` | Canvas patches, HUD, warning, Esc/Resume, refresh meter, onset and frames, gaze |
| `content.js` | Wiring: port to the worker, scan pipeline, clicks, resume after navigation |
| `background.js` | Service worker: one `Connection` per tab, screenshots, the toolbar toggle |
| `options.html`, `options.js` | Server address and token, in `chrome.storage.local` |
| `test/` | Node tests of the pure parts, with fake DOM, chrome and WebSocket |

The content scripts load on every page but stay idle (no port, no overlay, no socket) until
the toolbar icon starts BrainPoint in that tab. Messages between the page and the worker use
a runtime port named `brainpoint`; types that start with `_` are internal:

- page to worker: `_hello` (the hello fields; the worker adds the token), `_capture`, `_bye`;
  everything else goes to the server.
- worker to page: every server message, `_state`, `_close`, `_rtt`, `_error`,
  `_capture-result`.

**Scan.** On `scan_request` the page waits `settle_ms` (and while the tab is hidden or
paused), hides the overlay for two frames, collects the hints, and the worker takes the
screenshot with `chrome.tabs.captureVisibleTab` (JPEG, quality 80) after checking that the
tab is the visible tab of its window, before and after. The page scales the screenshot so
its long edge is at most 1280 px, blacks out the password fields, encodes it as JPEG at 0.8
and sends `scan` with at most 200 hints, the viewport, the url and the title.

**Click.** On `click` the page hides the overlay, clicks the element at the point (into open
shadow roots and same-origin frames), shows the overlay again and answers `clicked`. It never
clicks the overlay itself, a cross-origin frame, or a paused or hidden tab. Nor does it click
a page that changed since the screenshot: an address change, a scroll, a resize, or (for an
element that was a hint) a different element under the point answer `clicked` with `ok:
false`, and the server scans again.

**Esc.** `esc.js` runs in every frame at `document_start`, before any page script, so Esc is
heard even while an iframe has the focus. In a sub-frame it sends `_esc` to the worker, which
sends `pause` on the tab's connection and forwards `_esc` to the top frame's overlay.

**Flicker.** `FlickerState` from the SDK: luminance depends on the vsync frame index since
the layout's first flickering frame, never on wall-clock time, and patches flicker only while
the warning was accepted, the socket is open, the phase says flicker, the layout has targets
and the user has not paused. The page sends `onset` on that first frame and `frames` every
5 s.

**Code mode (c-VEP).** When the welcome says `paradigm: "code"`, every layout target has a
`lag` (0, 21 or 42 frames) instead of a frequency. On flicker frame n, counted from the
layout's first flickering frame at one bit per frame, a patch with lag L is white when bit
(n - L) mod 63 of the welcome's code is 1 and black when 0 (`cvep.js`); a still patch is
white. Each cycle's first flip (every 63 frames) sends `cycle` with its Unix time, which the
decoder lines the EEG up with. In the `train` phase the patch named by `cue` gets a red cue
box and the banner says "Look at the … patch   cycle k/30"; `trained` shows the template's
held-out accuracy against chance (33 %). After every `decision` each patch's label becomes a
label line, `Sign in ███░░░░░ +0.42 <` (bar and correlation, `<` on the best), and the banner
shows the status line, `lead 0.31   choice: Sign in`. Scores are correlations, not
percentages.

## Privacy

- The screenshot covers only the visible part of the tab, and only of the tab where you
  started BrainPoint, while it is the visible tab of its window.
- Password fields (and fields marked as card numbers, card codes or one-time codes) are
  blacked out in the image before it leaves the page, and are never read for hints.
- Hint text comes from labels and captions, never from what was typed into a field.
- The url sent with a scan has no query string and no fragment, which can hold tokens.
- Screenshots go to your BrainSync server, which sends them to the Anthropic API for
  detection. The server stores them only with `--log-scans DIR`.
- The token stays in the service worker and `chrome.storage.local`; it is never logged and
  never reaches the page.

## Permissions

- `storage`: the server address and token (`chrome.storage.local`), and which tab runs
  BrainPoint (`chrome.storage.session`, cleared when the browser closes), so a page loaded
  in that tab after a click resumes.
- `<all_urls>` (host permission): the content scripts load on any page you may want to
  control, and `chrome.tabs.captureVisibleTab` needs host access to the page it captures.
  Nothing is read from a page until you start BrainPoint in that tab.

`tabs` is not needed: the worker gets the tab and window from the content script's port
(`port.sender.tab`) and checks visibility with `chrome.tabs.get`. `debugger` is not used:
clicks are dispatched as DOM events. The WebSocket needs no permission.

## Tests

```bash
node --test clients/chrome/test/*.test.mjs                        # 120 tests, no browser
.venv/bin/python -m pytest -q tests/test_brainpoint_extension.py  # also runs the node tests
```

Node 24 reads `--test` arguments as globs, so pass the files, not the folder.
`tests/test_brainpoint_extension.py` checks the manifest, that `flicker.js`,
`brainsync-client.js` and `cvep.js` are byte-identical to `clients/js/` (after changing the
SDK, copy them here), runs `node --check` on every script and runs the node tests.
