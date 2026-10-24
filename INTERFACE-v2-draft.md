# BCI Voice — Decoder ↔ UI Interface (v2 DRAFT)

**Status: proposal, not agreed.** `INTERFACE.md` (v1) stays the contract until the teammate agrees
at SYNC 1; then this file replaces it and `protocol` becomes 2. Background: `PLAN.md` sections 12–14
and System design.

**What changed from v1, and why:** the flicker moved from the browser to the teammate's PsychoPy
display, so the decoder side now owns the screen the user looks at. Its process, the **bridge**
(`ssvep/online/bridge.py`), runs the board, the decoder and the display. Our server still decides
what is shown and when trials start and stop; the bridge does it and reports back.

| | v1 | v2 |
| --- | --- | --- |
| Who draws the flicker | browser | PsychoPy display, through the bridge |
| Who sends the stimulus settings | server `hello` | bridge `hello` (from its stimulus config); server checks them |
| `stim_start` | report: flicker began | command: start flickering now |
| Flicker onset time | browser, in `stim_start` | display's first flickering frame, in `stim_onset` |
| Text on the user's screen | browser | `display` message, drawn by PsychoPy |
| Resting baseline | not in the contract | `baseline` / `baseline_done` |
| STOP | — | `pause`; flicker off until the next `baseline` |
| Targets | 4, square, 15/12/10/8.57 Hz | 2, sine, 15/17 Hz (section 6) |

## 1. Connection

- WebSocket. The server listens; the bridge connects as a client to
  `ws://127.0.0.1:8000/ws/decoder`, and reconnects every 1 s if the connection drops.
- The bridge connects the board **once** at startup (about 16 s) and keeps it across reconnects.
- One JSON object per message, always with a `type` field. Unknown fields are ignored.
- Times are Unix seconds (float) on the same machine's clock (`time.time()`, BrainFlow timestamps).
- Slots are ids (`s1`, `s2`); the bridge never needs to know what a slot means.

## 2. Handshake

Bridge → server, right after connecting, with values read from its stimulus config and the
display's refresh check:

```json
{"type": "hello", "protocol": 2, "name": "ssvep-fbcca",
 "refresh_hz": 59.94,
 "waveform": "sine",
 "slots": [
   {"slot": "s1", "freq_hz": 15.0, "phase_deg": 0},
   {"slot": "s2", "freq_hz": 17.0, "phase_deg": 0}],
 "decoder": {"window_s": 4.0, "step_s": 0.5, "votes": 3},
 "score_kind": "fbcca_minus_baseline"}
```

`decoder` and `score_kind` are optional. `score_kind` is set only if `selection` carries scores.

Server → bridge, in reply:

```json
{"type": "hello_ack", "protocol": 2,
 "roles": {"s1": "say", "s2": "next"},
 "labels": {"s1": "SAY", "s2": "NEXT"}}
```

The server checks the bridge's slots against `stim.slots` in `config/default.yaml` (same ids and
frequencies within 0.01 Hz). On a mismatch or a different `protocol` it sends
`{"type": "error", "message": "..."}` instead of `hello_ack`, keeps the connection open, and stays
in keyboard mode. The bridge may send `hello` again after fixing its config.

## 3. Server → bridge

**`baseline`**: hold the flicker off (targets drawn still) and record the decoder's resting
baseline.

```json
{"type": "baseline", "baseline_id": 3, "seconds": 10.0}
```

The bridge answers with `baseline_done` (section 4). Sent after `hello_ack` and after every
`pause`, before the first trial.

**`display`**: what the user's screen shows. Any field left out is unchanged.

```json
{"type": "display", "text": "Yes, let's go outside",
 "labels": {"s1": "SAY", "s2": "NEXT"}, "highlight": null}
```

- `text`: the current option, drawn above the targets. `""` clears it.
- `labels`: per-slot labels inside or under the targets.
- `highlight`: a slot to draw the box on (the picked target), or `null` to clear it.
- Never sent between `stim_start` and `stim_stop`, so the shown option cannot change during a
  trial.

**`stim_start`**: start flickering now and start decoding.

```json
{"type": "stim_start", "trial_id": 42, "active_slots": ["s1", "s2"],
 "mode": "live", "cued_slot": null}
```

- The bridge turns the flicker on, calls `decoder.reset()`, drops samples read before this
  message, and steps the decoder until `stim_stop`.
- `mode`: `"live"` or `"calibrate"` (labelled data; `cued_slot` is the slot the user was told to
  look at, and the display draws the box on it for the whole trial).

**`stim_stop`**: stop flickering and stop decoding.

```json
{"type": "stim_stop", "trial_id": 42, "reason": "selected"}
```

`reason`: `selected`, `no_selection`, `timeout`, `context_changed`, `paused`, `calibration_done`.

**`pause`**: STOP from the partner page. Flicker off at once, decoding off, until the next
`baseline`.

```json
{"type": "pause", "reason": "stop_button"}
```

## 4. Bridge → server

**`stim_onset`**: the display's first flickering frame after `stim_start` (flip time from
`--markers unix`).

```json
{"type": "stim_onset", "trial_id": 42, "t_unix": 1792809201.512}
```

**`selection`**: the decoder's first commit after `stim_start`, at most once per `trial_id`.

```json
{"type": "selection", "trial_id": 42, "slot": "s1", "t_unix": 1792809206.530,
 "score": 0.041, "scores": {"s1": 0.041, "s2": 0.008}}
```

`score` and `scores` are optional: `Decision.scores` after the baseline is subtracted. They are
not probabilities.

**`no_selection`**: the decoder gave up on this trial (for example the signal was too poor).

```json
{"type": "no_selection", "trial_id": 42, "reason": "low_signal"}
```

**`baseline_done`**: the baseline was recorded.

```json
{"type": "baseline_done", "baseline_id": 3, "ok": true, "message": ""}
```

With `"ok": false`, the server shows the message on the partner page and sends `baseline` again
after the user is ready.

**`status`**: health, about once a second and on every change.

```json
{"type": "status", "ok": true, "board_ok": true, "display_ok": true,
 "message": "posterior RMS 12-20 uV"}
```

`display_ok: false` means the display exited or the user pressed `Esc`. The flicker is then off;
the server pauses and waits for `display_ok: true` before sending `baseline` again.

## 5. Rules

1. Only answer the current `trial_id`. Late answers for old trials are logged and ignored.
2. The flicker is on only between `stim_start` and `stim_stop`. Outside a trial the targets are
   drawn still.
3. **Safety:** if the server connection drops, the bridge turns the flicker off at once and keeps
   it off until a new `hello_ack` and `stim_start`. `Esc` on the display always stops the flicker
   locally, whatever the server says.
4. If nothing arrives by `max_trial_s` after `stim_start`, the server ends the trial
   (`stim_stop`, reason `timeout`).
5. The display text never changes during a trial (`display` is never sent then).
6. In calibration mode the server never speaks anything; selections are logged and used for a
   running accuracy.
7. Only the bridge reads the board's serial port.

## 6. Agreed values (fill in at SYNC 1)

| Question | Proposed | Decoder owner's answer |
| --- | --- | --- |
| Number of targets | 2 (SAY, NEXT) | |
| Frequencies | 15 and 17 Hz (12/17 under test, `PLAN.md` section 14) | |
| Waveform | sine (17 Hz does not divide 60) | |
| Decoder window | 4 s now; 2 s if the replay tuning agrees | |
| Cue before each trial | 1.0 s | |
| Rest after each trial | 1.0 s | |
| Max trial length | 10.0 s | |
| Baseline length | 10.0 s | |
| `score` / `scores` sent | yes, `fbcca_minus_baseline` | |
| Display text during the flicker | yes, unless it drops frames (SYNC 1.5) | |

## 7. Bridge ↔ display (informational, the teammate's side)

Not part of the contract; listed so `display` maps cleanly onto the display's stdin. Today's
free-run feedback reads one integer per line (target index, `-1` clears) and ignores anything else.
Proposed additions, one JSON object per line, alongside the integers:

```
{"text": "Yes, let's go outside"}
{"labels": ["SAY", "NEXT"]}
{"flicker": true}
{"flicker": false}
```

- After `{"flicker": true}` the display prints the Unix time of the first flickering frame, which
  the bridge sends as `stim_onset`.
- If the confidence bars (teammate's plan, phase D) also use this stdin, reserve `{"scores": [...]}`
  for them.

## 8. Testing each side alone

- Our side without the bridge: `python tools/fake_decoder.py --random` (or `--keyboard`). It must
  speak v2: send `hello` first, answer `baseline` with `baseline_done`, send `stim_onset`.
- The bridge without our server: `python tools/contract_check.py` flags schema errors in what the
  bridge sends. `python -m ssvep.online.bridge --fake` uses `FakeSource` instead of the headset.
