# BCI Voice — Decoder ↔ UI Interface (v1)

> **Superseded.** The server no longer speaks v1: since 2026-10-23 (`d4c1af9`) it speaks only
> protocol 2, specified in **`INTERFACE-v2-draft.md`**, which replaces this file once the teammate
> confirms it at SYNC 1. Build the bridge against the draft, not this file.

The shared contract between the **SSVEP decoder** (teammate, `ssvep/`) and the **voice + UI server**
(Elijah, `app/`). Both Claude Code sessions should load this file. Change it only when both people
agree, and bump `protocol` when a change breaks the other side.

**Split of work:** the UI draws the flickering targets and decides when trials start and stop. The
decoder reads EEG and reports which target the user looked at. The decoder never needs to know what
a target means; it only knows slot ids (`s1`–`s4`).

## 1. Connection

- WebSocket. The UI server listens; the decoder connects as a client to
  `ws://127.0.0.1:8000/ws/decoder` (later possibly `ws://<laptop-ip>:8000/ws/decoder` from the Jetson).
- One JSON object per message, always with a `type` field. Unknown fields are ignored, so either
  side can add optional fields without breaking the other.
- The decoder reconnects automatically (e.g. every 1 s) if the connection drops.
- **Times are Unix seconds (float), on the same laptop clock.** Python: `time.time()`. BrainFlow's
  timestamp channel is also Unix time (verify on the board), so stimulus times can be matched to
  EEG samples directly.

## 2. Handshake

Decoder → server, right after connecting:

```json
{"type": "hello", "protocol": 1, "name": "ssvep-cca"}
```

Server → decoder, in reply (and again if the display or stimulus settings change):

```json
{"type": "hello", "protocol": 1,
 "refresh_hz": 60.0,
 "waveform": "square",
 "slots": [
   {"slot": "s1", "freq_hz": 15.0, "phase_deg": 0},
   {"slot": "s2", "freq_hz": 12.0, "phase_deg": 0},
   {"slot": "s3", "freq_hz": 10.0, "phase_deg": 0},
   {"slot": "s4", "freq_hz": 8.57, "phase_deg": 0}]}
```

`refresh_hz` is measured in the browser. The slot values come from `config/default.yaml`, which the
decoder owner sets (see section 6).

## 3. Server → decoder

**`stim_start`**: flicker began on screen. Sent when the browser reports the first flickering frame.

```json
{"type": "stim_start", "trial_id": 42, "menu_id": "m17",
 "t_unix": 1761268201.512,
 "active_slots": ["s1", "s2", "s3", "s4"],
 "cued_slot": null, "mode": "live"}
```

- `active_slots`: only these are flickering (a confirm step may use 2).
- `mode`: `"live"` (normal use) or `"calibrate"` (labeled data collection).
- `cued_slot`: in calibration mode, the slot the user was told to look at; `null` in live mode.

**`stim_stop`**: flicker ended.

```json
{"type": "stim_stop", "trial_id": 42, "t_unix": 1761268204.930,
 "reason": "selected"}
```

`reason` is one of `selected`, `timeout`, `context_changed`, `paused`, `calibration_done`.

## 4. Decoder → server

**`selection`**: the decoder's answer for a trial. Send it when you are ready (the decoder chooses
its own window length), at most once per `trial_id`.

```json
{"type": "selection", "trial_id": 42, "slot": "s2", "t_unix": 1761268204.900}
```

Optional fields, only if available (the UI works without them):

```json
{"score": 0.71, "scores": {"s1": 0.31, "s2": 0.71, "s3": 0.22, "s4": 0.18}}
```

If `score`/`scores` are sent, the UI can ask the user to confirm low-confidence picks. Raw CCA
correlations are fine; say in `hello` which kind they are if you add them.

**`no_selection`**: no clear answer for this trial.

```json
{"type": "no_selection", "trial_id": 42, "reason": "low_signal"}
```

**`status`**: signal health, any time (about once a second is plenty).

```json
{"type": "status", "ok": true, "message": "8/8 channels ok"}
```

## 5. Rules

1. Only answer the current `trial_id`. Late answers for old trials are logged and ignored.
2. Don't send `selection` outside a trial (between `stim_stop` and the next `stim_start`).
3. If the decoder has said nothing by `max_trial_s` after `stim_start` (default 8 s), the server
   ends the trial as not caught.
4. In calibration mode the server never speaks anything; any selections are only logged and used to
   show a running accuracy, so the decoder can run its normal live code during calibration.
5. Expect the browser's onset time to be within about one frame (17 ms at 60 Hz) of the true screen
   onset, plus display lag. Tell Elijah if you need a better timing check.

## 6. To agree at SYNC 1 (hour 2)

| Question | Default in the UI | Decoder owner's answer |
| --- | --- | --- |
| Number of targets | 4 (3 options + "More options") | |
| Frequency-coded or phase-coded at 15 Hz? | Frequency-coded: 15, 12, 10, 8.57 Hz | |
| Waveform | square | |
| Calibration: blocks × stimulus length | 10 × 4.0 s | |
| Rest between trials | 0.5 s | |
| Max trial length before "not caught" | 8.0 s | |
| Can the decoder send `score`? | no | |

Notes for this choice:

- Frequency-coded targets at 60 Hz must be whole frames per cycle: 15 (4 frames), 12 (5), 10 (6),
  8.57 (7), 7.5 (8). Don't pair 15 Hz with 7.5 Hz or 5 Hz, because their harmonics land on 15 Hz.
- Phase-coded targets (all 15 Hz, phases 0/90/180/270°, one frame apart) need a phase-aware method
  such as template matching on calibration data. Plain CCA with sine/cosine references cannot tell
  same-frequency targets apart.

## 7. Testing each side alone

- UI side without the decoder: `python tools/fake_decoder.py --random` (or `--keyboard`).
- Decoder side without the full UI: run the server and open `?mode=calibrate` (flicker + events), or
  watch messages with `python tools/contract_check.py`, which flags schema errors in what the decoder
  sends.
