# BrainPoint Desktop (macOS)

A menu-bar agent that lets the user press buttons in the app in front by looking at flickering
patches, through the BrainSync server and its SSVEP decoder. It speaks `PROTOCOL-v3.md` point
mode with the v3.1 `"ax"` scans: instead of a screenshot it sends the Accessibility elements
of the screen that holds the focused window, the server ranks them and shows the likeliest as
patches, and a pick comes back as `click`, which BrainPoint carries out with `AXPress`.

A scan covers, in this order (at most 200 hints; the window keeps at least 100, and otherwise
whatever the others leave): the front app's open menus, the focused window's pressable items,
"Scroll up" / "Scroll down" strips in the window's largest scroll area (AXScrollUpByPage /
AXScrollDownByPage, else the scroll bar's AXDecrement / AXIncrement, else a scroll-wheel event
posted to the app), the front app's menu-bar items (pressing one opens its menu, and the next
scan offers its items), and the Dock's app and minimized-window items as the app switcher (only
when the Dock shows on that screen; never BrainPoint's own). The scan's viewport is that
screen's frame in points, and every hint and protocol rect is relative to it. A denied front
app gets no scan at all, so its menus and the Dock are off too.

> **Photosensitivity.** With the c-VEP standard the three patches switch between white and
> black up to 30 times a second; with a frequency server they flicker at 12 to 20 Hz. 15 to
> 25 Hz is the band most likely to trigger seizures. BrainPoint asks for an explicit Accept
> every launch before it draws any flicker, and Esc stops it at once.

## Code mode (the c-VEP standard)

A server started with the default `--paradigm code` (`docs/CVEP-STANDARD.md`) sends a welcome
with `paradigm: "code"` and the code: 63 bits, 21-frame lags, 63-frame cycles, 30 training
cycles. Its layouts give each target a `lag` (0, 21, 42) instead of a frequency: three white
patches, the best candidate, the second and More (confirm: "Do it" and "Back" on lags 0 and
42). On frame n since the layout's first flickering frame, a patch with lag L is white when
bit (n - L) mod 63 is 1 and black when 0 (`CodeVEP.bit`, checked against
`brainsync.vep.live` in `Tests/BrainPointCoreTests/Fixtures/cvep_fixture.json`, made by
`scripts/make_cvep_fixture.py`); a still patch is white. Every cycle's first flip is reported
as `cycle {layout_id, n, t_client}`. The `train` phase draws its training row on the main
screen with a red cue box on the `cue` patch (menu title **TRAIN**, nothing is pressed) and
the strip counts "cycle k/30"; `trained` puts the held-out accuracy against chance (33 %) on
the strip; every `decision` writes a label line under each patch
(`Sign in ███░░░░░ +0.42 <`, monospaced) and the status line on the strip
(`lead 0.31   choice: Sign in`). Scores are correlations, not percentages. A frequency server
is drawn as before.

## Build and run

```bash
cd clients/macos
swift build && swift test                 # Core unit tests (luminance fixture, gate, codec, model)
scripts/build-app.sh                      # -> build/BrainPoint.app (release, ad-hoc signed)
open build/BrainPoint.app                 # or run the binary to see its arguments work:
build/BrainPoint.app/Contents/MacOS/BrainPoint --server ws://127.0.0.1:8765/ws --verbose
```

Start a server first, e.g. `.venv/bin/python -m brainsync.server --board fake --no-auth` from the
repo root (point mode needs `ANTHROPIC_API_KEY`; without it the server answers
`detect_unavailable` and BrainPoint shows that as the reason it is off).

Launch arguments (this run only; the settings window persists its values):

| Argument | Meaning |
|---|---|
| `--server URL` | the server's WebSocket URL (default `ws://127.0.0.1:8765/ws`) |
| `--token TOKEN` | the server token (prefer the settings window) |
| `--allow-fake-control` | let the SIMULATED board press (developers only) |
| `--accept-warning` | skip the photosensitivity alert (developers only) |
| `--verbose` | also print every log line to stderr |

The first launch asks for the Accessibility permission (System Settings, Privacy & Security,
Accessibility). `build-app.sh` signs with the stable identifier `ai.brainsync.brainpoint`, so the
permission usually survives rebuilds; if it stops working, remove and re-add the app there.

## What the menu bar says

The title is the control state: **ARMED** (a pick presses), **DRY RUN** (the SIMULATED board:
picks are shown, nothing is pressed), **CALIB**, **TRAIN** (c-VEP training: the patches
flicker, nothing is pressed), **PAUSED** or **OFF**. The menu shows the
server, the headset (board state, round-trip time), the control state with the one-sentence
reason, the front app, the last scan (hints, walk ms, server ms, cached), the last press, and
Pause / Resume, Settings…, Open Log Folder and Quit. The same state and reason are on a thin
strip at the top of the main screen, in a notification on every change, and in the log.

BrainPoint is **armed** only when all of these hold, and otherwise the reason names the first
that fails: connected as the controller; the board streaming, ok, for at least 2 s;
Accessibility granted; the warning accepted; no pause (Esc, the menu, a locked screen, secure
input); the front app not on the deny list (System Settings, Keychain Access and BrainPoint
itself by default); the server's phase `run`. A simulated board is a dry run unless
`--allow-fake-control` is set. With a simulated board, hovering the mouse over a patch sends
`gaze`, so the loop can be tried without a headset.

Arming, disarming and losing or regaining the headset are also spoken with the server's voice
(`GET /api/speech`); a server without a voice (503) stays silent.

## Logs

`~/Library/Logs/BrainPoint/brainpoint-YYYYMMDD.jsonl`, one JSON object per line: every gate
change, server message type, scan (app, window, hint count, walk time), overlay, click and press
result. The token is never logged.

## Layout

- `Sources/BrainPointCore`: pure logic, unit-tested. `Luminance` (bit-exact with
  `brainsync/stimulus/flicker.py`), `CodeVEP` (the c-VEP code, frame rule, confidence text and
  `CycleCounter`), `RefreshMeter`, `Geometry`, `Hints`, `ControlGate`, the
  protocol codec (`Codec`, `ServerMessage`) and `AppModel`, the app's state as a reducer.
- `Sources/BrainPointPlatform`: AppKit and Accessibility: focus watching, the AX walk and
  presses, the overlay, safety and Esc monitors.
- `Sources/BrainPoint`: the menu-bar app that wires them to the server.
- `Sources/bp-probe`: Phase 0 measurements (`bp-probe flicker`, `bp-probe axdump`).
