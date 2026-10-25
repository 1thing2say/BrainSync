# BrainSync client ↔ server protocol (v3)

The **server** owns the EEG headset and the decoder. It is meant to run on an NVIDIA Jetson Orin
Nano and is developed on a Mac with `--board fake`. A **client** is any app that draws the
flickering targets on the screen the user looks at and receives what the user picked. The server
never draws anything, and the client never sees EEG.

Two client modes:

- **`slots`**: the client names its own targets (`["yes", "no"]`) and draws them wherever it likes.
  The server gives each slot a frequency and sends a `selection` each time the decoder commits
  one. The reference page `clients/web/` and the Python client `brainsync/server/client.py`
  use this mode.
- **`point`** (BrainPoint): the client sends screenshots of the app it controls. The server asks
  Claude Haiku for the clickable elements and shows the most likely ones as flickering patches
  (top-K plus "more"). It sends `click` once the user has picked one; risky clicks need a confirm
  pick first. The Chrome extension `clients/chrome/` uses this mode.

> **Photosensitivity.** Flicker between 15 and 25 Hz is the band most likely to trigger a seizure
> in people with photosensitive epilepsy. A client must show the warning and get an explicit
> accept before it draws any flicker, and it sends `accepted: true` in `hello` only after that.

## 1. Connection

- One port (default 8765) serves both HTTP and WebSocket.
  - `GET /` serves the reference page and `GET /static/<name>` its files.
  - `GET /health` returns `{"ok": true, "board": "<state>", "controller": bool, "viewers": n}`.
  - `GET /api/speech?text=…` returns the text as WAV (v3.1, section 5).
  - `GET /ws` upgrades to WebSocket.
- The default bind is `127.0.0.1`. Other machines reach the server through a Cloudflare quick
  tunnel (`cloudflared tunnel --url http://127.0.0.1:8765`), which gives an
  `https://<name>.trycloudflare.com` address and so `wss://`; the server itself speaks plain
  `ws://`, and allows the tunnel's Origin with `--origin`. On the Jetson,
  `brainsync-tunnel.service` runs both (`deploy/jetson/cloudflare/README.md`).
- Messages are UTF-8 JSON text frames, one object per frame, each with a string `type`. Unknown
  fields are ignored. Unknown types are answered with `error` (`code: "protocol"`,
  `fatal: false`) and otherwise ignored. Binary frames are ignored. A frame over 4 MB closes the
  connection (code 1009).
- **Auth.** The first client frame must be `hello`, carrying `token`. The server compares it in
  constant time against the token from `--token-file` (generated with mode 0600 on first run).
  - Wrong token: `error` `bad_token`, then close code **4001**.
  - Any other frame before `hello`: close **4003**.
  - No `hello` within 10 s: close **4008**.
  - `--no-auth` is allowed only with a loopback `--host`.
- **Origin.** Browsers send `Origin`. These are allowed: `chrome-extension://<32 a-p>`,
  `http(s)://localhost|127.0.0.1[:port]`, the server's own host, and any `--origin REGEX`. Any
  other origin gets HTTP 403. No `Origin` header (native apps, the Python client) is allowed;
  the token is the real gate.
- **Roles.** A headset belongs to one user, so there is one **controller** at a time.
  - Viewers get every broadcast (`phase`, `layout`, `selection`, `click`, `status`) but may send
    only `ping` and `bye`; anything else gets `error` `not_allowed`.
  - A second `role: "controller"` hello gets `error` `busy` and close **4002**, unless it sends
    `takeover: true`. In that case the old controller is closed with **4000** ("replaced"), and
    a client closed with 4000 must not reconnect by itself.
  - A controller hello whose `client.instance` equals the current controller's is the same
    client reconnecting (its old socket died on its side only, and the server has not noticed
    yet). It replaces the old connection as a takeover does, instead of getting `busy`.
- Keepalive: the server sends `{"type": "ping"}` every 20 s. Chrome stops an MV3 service worker
  after 30 s without events. It also sends WebSocket ping frames every 10 s and closes a
  connection that has not answered within 10 s, so a dead controller frees its place.
- Times: `t_server` is the server's `time.time()`, the same clock as the board's timestamps.
  `t_client` is the client's `Date.now() / 1000` or `time.time()`. The server estimates
  `offset = t_server - t_client` from `ping`/`pong` round-trips (the median of the last 8). The
  decoder doesn't depend on phase, so an error of tens of ms doesn't matter.

## 2. Client → server

**`hello`**: the first message, and again after every reconnect.

```json
{"type": "hello", "protocol": 3, "token": "…", "role": "controller", "takeover": false,
 "client": {"name": "brainpoint-extension", "kind": "extension", "version": "0.1.0"},
 "mode": "point", "slots": null,
 "refresh_hz": 59.98, "accepted": true,
 "viewport": {"w": 1512, "h": 857, "dpr": 2}}
```

- `role`: `"controller"` or `"viewer"`.
- `client`: `name`, `kind`, `version` and `instance`, strings of at most 64 characters, all
  optional. `instance` is a random id the client keeps for its lifetime and sends in every
  hello, so the server can tell the same client reconnecting from another one (section 1).
- `mode`: `"slots"` or `"point"`.
- `slots` is used in `slots` mode only: 1 to `max_slots` unique strings of 1–32 characters.
  `point` mode sends `null`.
- `refresh_hz` is the client's measured display refresh. It must lie within
  `refresh_tolerance_hz` (1 Hz) of one of the welcome's `refresh_rates`, set with
  `--refresh-rates` (default `60,90,120,144`). A controller at none of them gets `error`
  `refresh` (not fatal), whose message lists the accepted rates. The session then stays in
  `wait` with the flicker off until a new `hello` reports a good rate. `--ignore-refresh`
  turns this check off.
- `accepted` must be `true` for a controller, otherwise `error` `bad_hello`.
- A viewer may leave out `mode`, `slots`, `refresh_hz` and `accepted`.

**`onset`**: the first frame drawn with flicker on for a layout. The decoder counts this layout's
windows only after it arrives (section 4).

```json
{"type": "onset", "layout_id": 7, "t_client": 1792809201.512}
```

**`frames`**: every 5 s while flickering: `{"type": "frames", "refresh_hz": 59.98, "dropped": 0}`.
`refresh_hz` is the measured refresh and `dropped` the running count of late frames (a frame
interval over 1.5 refresh periods) since the client started. The server compares consecutive
reports of the controller: when the dropped frames since the previous report exceed 10 % of the
frames expected (`refresh_hz` times the seconds between the reports), `status` turns
`ok: false` with a sentence such as "the display dropped 20% of frames; close other apps or
plug in". A later report under 10 % clears it. The first report only sets the baseline, and so
does a report less than 2.5 s or more than 10 s after the previous one (a pause or new layouts
in between: its count also holds late frames from before them), which leaves the status as it
was. Decoding is not affected.

**`ping`**: `{"type": "ping", "ping_id": 3, "t_client": 1792809201.0}`. Answered with `pong`.

**`pause`** / **`resume`**: the user pressed Esc or Resume. `pause` sets phase `pause` (flicker
off). `resume` returns to `run`, without recalibrating if the pause lasted under 120 s and
recalibrating otherwise.

**`rescan`** (point mode, v3.1): the focused app or window changed; see section 5.

**`bye`**: the user is leaving. The server treats it like a disconnect.

**`gaze`**: accepted only when the server runs `--board fake` (`welcome.fake` is true).
`{"type": "gaze", "slot": "b1"}` makes the fake board carry that slot's tone, and `"slot": null`
means looking at nothing. Pages send it on hover so the whole loop can be tried without a headset.

**`scan`** (point mode only): a screenshot of the viewport, sent in answer to `scan_request`.

```json
{"type": "scan", "scan_id": 4,
 "image": {"media_type": "image/jpeg", "data": "<base64>", "w": 1280, "h": 725},
 "viewport": {"w": 1512, "h": 857, "dpr": 2},
 "url": "https://example.com/", "title": "Example",
 "hints": [{"id": 0, "x": 12, "y": 40, "w": 80, "h": 32, "role": "button", "text": "Sign in",
            "tag": "button"}]}
```

- The image covers exactly the visible viewport, scaled down so that its long edge is at most
  1280 px (JPEG, quality about 0.8).
- `hints` are the page's own clickable elements: optional, at most 200, in viewport CSS pixels,
  only the visible ones. A hint may add `"submit": true` when it submits a form (an input of
  type submit or image, or a submit button inside a form); detection then marks it risky. The values of password fields and `input[type=password]` contents must
  never appear in `text`, and the client blacks out password fields in the image before sending.

**`clicked`** (point mode only): the result of a `click`.

```json
{"type": "clicked", "click_id": 2, "ok": true, "method": "element.click", "detail": ""}
```

## 3. Server → client

**`welcome`**: the answer to a valid `hello`.

```json
{"type": "welcome", "protocol": 3, "role": "controller", "mode": "point",
 "server": {"name": "brainsync", "version": "0.1.0"}, "fake": false,
 "waveform": "sine", "refresh_hz": 60.0, "refresh_tolerance_hz": 1.0,
 "refresh_rates": [60.0, 90.0, 120.0, 144.0],
 "freqs": [15.0, 17.0, 12.0, 20.0], "max_slots": 4,
 "detect": true}
```

`refresh_hz` is the nominal display rate (the first of `--refresh-rates`) and `refresh_rates`
every rate a controller may have, each ± `refresh_tolerance_hz`. A client ignores
`refresh_rates` if it does not use it; an older server leaves it out, which means
`[refresh_hz]`. The server refuses a `--refresh-rates` whose lowest rate cannot show the
highest of `--freqs` (a display shows less than half its refresh).

`detect` is false when point mode can't work (no API key, or `--no-detect`). A point-mode
controller then also gets `error` `detect_unavailable` (not fatal).

**`error`**: `{"type": "error", "code": "busy", "message": "…", "fatal": true}`. When `fatal` is
true the server closes the connection after sending it. The codes are `bad_token`, `busy`,
`bad_hello`, `refresh`, `too_many_slots`, `protocol`, `not_allowed`, `detect_unavailable`,
`scan_failed` and `internal`.

**`phase`**: sent to every client on change, and to each new client after `welcome`.

```json
{"type": "phase", "phase": "calibrate", "flicker": false, "seconds": 10.0,
 "text": "Look at the screen and relax"}
```

| `phase` | `flicker` | What it means |
|---|---|---|
| `wait` | false | No controller, or the controller is not ready (refresh, detect). |
| `calibrate` | false | Rest baseline: targets drawn still at luminance 0.5, `seconds` long. |
| `run` | true | Targets flicker at their frequencies; selections can happen. |
| `pause` | false | The user paused (Esc), or a click is in flight. |
| `stop` | false | The server is shutting down; hide everything and show `text`. |

**`layout`**: what to draw. It replaces the previous layout as a whole.

```json
{"type": "layout", "layout_id": 7, "stage": "pick",
 "targets": [
   {"slot": "b1", "freq": 15.0, "phase": 0.0, "label": "Sign in", "kind": "button",
    "rects": [{"x": 0.012, "y": 0.04, "w": 0.09, "h": 0.12}]},
   {"slot": "more", "freq": 20.0, "phase": 0.0, "label": "More", "kind": "more",
    "rects": [{"x": 0.86, "y": 0.84, "w": 0.12, "h": 0.14}]}]}
```

- `kind` is one of `slot`, `button`, `more`, `confirm` or `back`.
- `rects` are normalized to the viewport: x right, y down, all values in [0, 1]. They are empty
  in `slots` mode, where the client places its own targets.
- In point mode, rects of different targets never overlap.
- `stage` is `slots`, `pick`, `confirm` or `idle`. `idle` has no targets: nothing to show, for
  example while a scan is being analyzed.
- `phase` is the wave's starting phase in radians. Luminance follows `brainsync/stimulus/flicker.py`
  and `flicker.js`: `0.5 * (1 + sin(2π f n / refresh + phase))`, where `n` counts frames since
  this layout's first flickering frame, and `refresh` is the welcome's `refresh_hz`. A client
  whose measured refresh is further than `refresh_tolerance_hz` from it (another of
  `refresh_rates`, or any rate with `--ignore-refresh`) uses its measured refresh as `refresh`
  instead, so every target still flickers at `f` in real time: 15 Hz is 4 frames per cycle at
  60 Hz and 8 at 120 Hz. It draws still any target with `f` at or above half of it. It counts
  `n` at that same `refresh` (`n = round((t - t0) * refresh)`), so a new refresh estimate
  within the tolerance never moves the phase in the middle of a layout.

**`selection`**: the decoder committed a target of the current layout.

```json
{"type": "selection", "selection_id": 12, "layout_id": 7, "slot": "b1", "t_server": 1792809204.9,
 "score": 1.8, "scores": {"b1": 1.8, "b2": 0.2, "b3": -0.1, "more": 0.4}}
```

The scores are baseline-corrected decoder scores, not probabilities.

**`scan_request`** (point mode only): `{"type": "scan_request", "scan_id": 4, "settle_ms": 800}`.
The client waits `settle_ms` (for the page to settle) and then sends `scan`.

**`scan_result`** (point mode only): for the HUD and logs.

```json
{"type": "scan_result", "scan_id": 4, "ok": true, "ms": 1840, "n_elements": 23,
 "n_candidates": 9, "summary": "Login page", "elements": [ … ]}
```

**`click`** (point mode only, broadcast): the user chose an element. The controller clicks at
(`x`, `y`), the normalized viewport point at the element's center, and answers with `clicked`.
It answers `ok: false` without clicking when the page changed since the screenshot it sent
(another address, a scroll, a resize, or for an element with a `hint_id`, a different element
under the point now); the server then scans again.

```json
{"type": "click", "click_id": 2, "x": 0.057, "y": 0.1,
 "element": {"id": 3, "label": "Sign in", "role": "button", "hint_id": 0, "risky": false}}
```

**`status`**: about once a second and on change.

```json
{"type": "status", "board": "streaming", "ok": true, "text": "posterior RMS 14 uV",
 "rtt_ms": 38}
```

`ok` is false when the board is not streaming, or while the controller's display drops too
many frames (see `frames`); `text` then says why.

`board` is one of `unknown`, `connecting`, `streaming`, `no-data` or `error`.

**`pong`**: `{"type": "pong", "ping_id": 3, "t_client": 1792809201.0, "t_server": 1792809201.02}`.

## 4. Rules

1. **Flicker safety.** A client flickers only while all of these hold: the user accepted the
   warning, the socket is open, the last `phase` has `flicker: true`, and a layout has targets.
   When the socket closes, the flicker stops at once. Esc always stops it locally and sends
   `pause`.
2. **Onset gating.** Each new layout resets the decoder's votes. A decision counts for that layout
   only if its window started at least `latency_s` (0.14 s) after the onset. The onset is
   `onset.t_client + offset`, or the time the onset arrived if no offset is known yet. If no
   `onset` arrives within 2 s of the `layout`, the server uses the time it sent the layout. The
   EEG buffer is never emptied, so a new layout costs no refill time, only the window: the first
   valid decision comes about 1.64 s after the onset.
3. A `selection` names a slot of the current layout only. In `slots` mode a slot can be selected
   again after a 1.5 s cooldown if the user keeps looking (dwell to repeat), as BrainScroll does.
4. **Point mode (strategy B: top-K plus "more").** Let `K = len(freqs)` (4 by default).
   - Candidates are the elements with `p_button ≥ 0.5` (`--min-p-button`) and `enabled`, ranked
     by `p_button × p_next`.
   - A **page** shows up to `K - 1` candidates plus one **more** target. The candidate ranked
     highest gets `freqs[0]`, the next `freqs[1]`, and so on; "more" gets `freqs[K-1]`.
   - Each patch is the element's rect, grown to at least `min_patch` (0.10 of the viewport
     height, square in pixels) around its center and clamped inside the viewport.
   - Patches on one page never overlap and keep a gap of at least 0.02. A candidate whose patch
     would collide with one already on the page waits for a later page.
   - The "more" patch sits in the bottom-right corner. A candidate that collides with it moves
     to a later page.
   - Picking "more" shows the next page. After the last page, "more" sends a fresh
     `scan_request`.
   - Picking a button that is not `risky` sends `click` at once. Picking a `risky` button shows
     the **confirm** stage: "Do it" (`freqs[0]`) over that button's patch and "Back"
     (`freqs[1]`) in the corner. "Back" returns to the same page.
   - After `click`, the phase is `pause` until `clicked` arrives. The server then sends
     `scan_request` (with `settle_ms`), the layout is `idle` until `scan_result`, and then the
     phase goes back to `run`.
5. **Detection.** `claude-haiku-5-5` (`--model`) receives the image and the hints and returns
   elements in structured JSON: `label`, `role`, box in image pixels, `hint_id`, `p_button`,
   `p_next`, `risky` and `enabled`.
   - `risky` is the model's flag OR a fixed rule: the label, or the text of the element's hint
     or of a hint under its center, names an irreversible action (send, pay, buy, order,
     delete, post, submit, log out, ...), or that hint has `submit: true`. The model can add
     risk but never remove it.
   - When `hint_id` names a hint, the hint's exact box is used.
   - The last 5 clicked labels go along as history, so `p_next` can follow the task.
   - A failed or timed-out detection is retried once, then answered with `scan_result`
     `ok: false` and `error` `scan_failed`. The layout stays `idle` and the server sends a new
     `scan_request` after 3 s.
   - Screenshots are not stored unless `--log-scans DIR` is given.
6. Only the server process reads the board's serial port.

## 5. Version 3.1: desktop clients

These additions only add fields and routes; `protocol` stays 3, and a v3 client works
unchanged. They let a native desktop client (BrainPoint Desktop on macOS) send the focused
window's accessibility tree instead of a screenshot.

**`scan.source`**: `"screenshot"` (the default; section 2 as before) or `"ax"`.

```json
{"type": "scan", "scan_id": 4, "source": "ax",
 "viewport": {"w": 900, "h": 600, "dpr": 2},
 "app": {"bundle_id": "com.apple.Notes", "name": "Notes", "pid": 412},
 "window": {"title": "All iCloud"},
 "hints": [{"id": 0, "x": 12, "y": 40, "w": 80, "h": 24, "role": "AXButton",
            "tag": "", "text": "New Note", "actions": ["AXPress"], "enabled": true}]}
```

- `viewport` is the size in points of the **screen holding the focused window**, and the hints
  are in points from that screen's top-left corner. Rects in `layout` and `scan_result` are
  normalized to that screen. The desktop client (`clients/macos/`) sends, in this order: the
  items of an open menu, the focused window's pressable items, two scroll strips for its main
  scroll area (role `AXScrollArea`, tag `BPScrollUp` / `BPScrollDown`), the front app's
  menu-bar items (`AXMenuBarItem`) and the visible Dock's items (`AXDockItem`, the app
  switcher). An older client may send only the window, with the window as the viewport.
- `app` (`bundle_id` and `name` strings, which may be empty, and an integer `pid`) and
  `window.title` are required. `image` is optional (a screenshot of the viewport, as in v3).
- A hint's `role` holds the AX role and `tag` the subrole. A hint may add `actions` (AX
  action names, at most 8 of at most 40 characters; more are cut) and `enabled`. At most 200
  hints, as before.
- A screenshot scan keeps neither `actions` nor `enabled`.

**Ranking.** With 5 or more hints, or with no image, the server ranks the hints with one
text-only Claude Haiku call (no image) in structured JSON. Each element names a `hint_id`
and gets `label`, `p_button`, `p_next`, `risky` and `enabled`. The box is always the hint's
own, and an element without a valid `hint_id` is dropped. `role` comes from the AX role
(`button`, `link`, `checkbox`, `tab`, `menu_item`, `input` or `other`), and `enabled` from
the hint when it has one. Rule 5's `risky` rule still applies, and at most 15 elements come
back. With fewer than 5 hints and an image, the screenshot path (rule 5) runs instead. With
no hints and no image (no usable window), the answer is `scan_result` `ok: true` with no
elements, the layout stays `idle`, and a new `scan_request` follows after 3 s.

**Rank cache.** The server keeps the last 64 rankings (least recently used dropped first),
keyed on the bundle id, the window title, the hints' sorted (role, label, box rounded to
whole points) and the click history. A hit makes no model call, and its hint ids are mapped
onto the new scan's.

**`scan_result.cached`**: every `scan_result` now carries `"cached": true` or `false`. It is
true when the ranking came from the cache (`ms` is then a few milliseconds).

**Streaming gate.** A decoder commit becomes a `selection`, and so a `click`, only while the
board streams: no board error, and samples delivered within 1.0 s before the read that
produced the commit. Otherwise the pick is dropped and logged (`selection_dropped`), and the
voter is re-armed, so a gaze still held commits again once the board streams.

**`GET /api/speech?text=…`**: the text (whitespace collapsed) as WAV (`audio/wav`) in the
server's Cartesia voice, so a client can speak status changes ("headset disconnected")
without the API key. It returns 400 when the text is empty or over 300 characters, and 503
when the server has no voice (no `CARTESIA_API_KEY`, `BRAINSYNC_TTS=off`, or the synthesis
failed or took over 12 s). The Origin rule of section 1 applies. With auth on, the request
must carry the server token as `Authorization: Bearer <token>` (else 401): a clip costs
Cartesia credits, and behind a tunnel anyone could reach the URL.

**`rescan`** (client → server, controller in point mode only): `{"type": "rescan"}`, sent
when the focused app or window changed. The server drops the pages, the confirm stage, a
pending scan and a click in flight (a late `clicked` for it is ignored), and answers with a
`scan_request` (`settle_ms` 300) and an `idle` layout; a held `pause` for a click goes back
to `run`. At most one rescan goes out every 0.5 s: one that comes sooner is answered when the
0.5 s are up. A viewer gets `not_allowed`, and so does a slots-mode controller. A `resume` in
point mode also asks for a fresh scan, because the screen may have changed while paused.

**`click`** is unchanged. A desktop client presses `element.hint_id` from its own registry of
the last walk, and `x` and `y` are informational.

## 6. Testing

- `python -m brainsync.server --board fake --no-auth` runs the server, and `http://127.0.0.1:8765/`
  opens the reference page (`slots` mode). Hovering a square sends `gaze`.
- `python -m brainsync.server.client --url ws://127.0.0.1:8765/ws --token … --slots yes,no`
  prints selections.
- `tools/screen_eval.py` scores Haiku detection against DOM ground truth, and with `--ax DIR`
  the text-only ranking of accessibility dumps (see its docstring).
