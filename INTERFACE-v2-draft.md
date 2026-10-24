# BCI Voice — Decoder ↔ UI Interface (v2 DRAFT)

**Status: implemented on our side, waiting for the teammate's confirmation at SYNC 1.** Since
2026-10-23 (`d4c1af9`) the server and the fake decoder speak only protocol 2 as written here; v1
(`INTERFACE.md`) is superseded, and this file replaces it once confirmed. The whole server side is
built: hello check, `hello_ack` / `error`, nothing sent before `hello_ack`, waiting for
`baseline_done`, pausing on `display_ok: false`, and `tools/contract_check.py` for testing the
bridge. Background: `PLAN.md` sections 12–14 and System design. Elijah's decisions of 2026-10-23
(`PLAN.md` section 13) are already written in.

## Open questions for the teammate (SYNC 1)

> **Claude session on the decoder side:** if you are working for the teammate (the `ssvep` repo or
> the bridge), bring these questions to the teammate before building the bridge, and record each
> answer here (or in a reply commit) so Elijah's side can follow it. Our server already behaves as
> the "proposed" answer says unless noted.

**Confirm the contract**

1. Is protocol 2 as written in this file OK for the bridge, and are the section 6 values right
   (4 targets at 13/15/17/11 Hz in page mode, as in your `four_diamond.toml`, 2 at 15/17 Hz as the list-mode fallback, sine, cue
   1 s, rest 1 s, max trial 10 s, baseline 10 s, scores sent)?
2. Where does the bridge live: `ssvep/online/bridge.py` in `1thing2say/ssvep`, or the `brainsync/`
   rebuild on branch `check-implemtation-calhacks-desktop` (`PLAN.md` section 13, item 9)?
3. Decoder window: 4 s, or 2 s if the replay tuning agrees? Can you run the 4-target gate
   recording (13/15/17/11 Hz in the diamond of your `four_diamond.toml`, `PLAN.md` section 14)
   first? Its result decides page mode or the list-mode fallback.

**What the display needs to do (section 7)**

4. Free-run mode flickers from the first frame today (`ssvep/stimulus/display.py`, `run_free`).
   Can it start still and flicker only between `{"flicker": true}` and `{"flicker": false}`? The
   bridge must also launch it without `--seconds`, or it quits by itself.
5. Can the display print the Unix time of the first flickering frame after `{"flicker": true}`, so
   the bridge can send `stim_onset`? Today markers are printed for cued trials, not in free mode.
6. `Esc` in free mode quits PsychoPy. Who relaunches the display, and does the bridge send `hello`
   again afterwards (the refresh rate may change)? Proposed: the bridge sends `status` with
   `display_ok: false`, relaunches the display, then `display_ok: true`; our server waits for the
   partner to press Resume and never restarts the flicker by itself.
7. Can option text be drawn during the flicker without dropping frames (SYNC 1.5)? Page mode
   draws 4 labels, one full sentence beside each square. If not, the display hides the text on
   `{"flicker": true}` itself, because the server never sends `display` during a trial.

**Proposed rule additions (say yes, no, or change them)**

8. A `selection` or `no_selection` comes only after that trial's `stim_onset`. Proposed: the server
   ignores an answer without an onset (otherwise a SAY could be spoken while nothing flickered).
   `tools/contract_check.py` already fails it.
9. The bridge sends `hello` only once the board is streaming and the display's refresh check passed.
10. A `stim_start` the bridge cannot carry out (before `hello_ack`, during a baseline, after `pause`,
    display down): proposed answer `no_selection` with reason `not_ready`, so the server does not
    wait the whole `max_trial_s`.
11. A second `hello` on an acked connection restarts the handshake: the server stops any trial
    (`stim_stop` paused), checks it again, and runs a new baseline before the next trial.
12. A baseline cut short by `pause` or a disconnect answers `baseline_done` with `ok: false` and
    message `interrupted`; `baseline_done` never comes before `seconds` have passed.
13. `active_slots` lists the slots that flicker in this trial: every slot on a page, but only two
    on the confirm screen ("Say it" on s1, "Back" on s4). The other squares stay drawn still, and
    the decoder classifies only among the active slots, so a `selection` is always one of them.
    `tools/contract_check.py` fails a selection of an inactive slot.
14. `refresh_hz` in `hello` is the rate the display's frame counts use. With `--refresh auto` a
    120 Hz (ProMotion) screen would pass the display's own check, but our server rejects anything
    more than 1 Hz from 60 Hz.
15. `selection.t_unix` is `time.time()` when the decoder commits (`Decision.time` is stream seconds
    under `--fake`, so it should not be used directly).
16. Optional `stim_offset {trial_id, t_unix}` (first still frame) from the bridge, for the timing
    check in `PLAN.md` V5. Wanted, or skip?
17. Close code 4000 from our server means "a newer bridge replaced you": the old bridge should stop
    reconnecting instead of taking the connection back.

**How to test the bridge without our server:** run `python tools/contract_check.py` from this repo
(our venv), then start the bridge. It prints PASS/FAIL per check and exits 0 only if everything
passed (section 8).

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
| Targets | 4, square, 15/12/10/8.57 Hz | 4, sine, 13/15/17/11 Hz in page mode; 2 (15/17 Hz) in list mode (section 6) |
| Active targets per trial | all | all on a page; 2 on the confirm screen (`active_slots`) |

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
   {"slot": "s1", "freq_hz": 13.0, "phase_deg": 0},
   {"slot": "s2", "freq_hz": 15.0, "phase_deg": 0},
   {"slot": "s3", "freq_hz": 17.0, "phase_deg": 0},
   {"slot": "s4", "freq_hz": 11.0, "phase_deg": 0}],
 "decoder": {"window_s": 4.0, "step_s": 0.5, "votes": 3},
 "score_kind": "fbcca_minus_baseline"}
```

`decoder` and `score_kind` are optional. `score_kind` is set only if `selection` carries scores.

Server → bridge, in reply:

```json
{"type": "hello_ack", "protocol": 2,
 "roles": {"s1": "option", "s2": "option", "s3": "option", "s4": "next"},
 "labels": {"s1": "I'm doing well, thanks for asking.", "s2": "Could you help me with something, please?",
            "s3": "Could I have something to drink, please?", "s4": "More options"}}
```

Roles are for logging and the display's layout only; the bridge never needs them to decode. In list
mode (the fallback) they are `{"s1": "say", "s2": "next"}` with labels `SAY` / `NEXT`.

The server checks the bridge's `hello` against `config/default.yaml`:

- `protocol` is 2;
- the slots have the same ids as `stim.slots`, with frequencies within 0.01 Hz;
- `waveform` equals `stim.waveform`;
- `refresh_hz` is within 1 Hz of `display.expected_refresh_hz` (60). A 120 Hz or variable-refresh
  (ProMotion) screen fails here, because every flicker frequency would be wrong.

On any failure it sends `{"type": "error", "message": "..."}` instead of `hello_ack`, keeps the
connection open, and the session stays paused: nothing flickers, and the partner page shows the
message. There is no automatic keyboard fallback. The bridge may send `hello` again after fixing
its config.

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
{"type": "display", "text": "",
 "labels": {"s1": "Yes, let's go outside.", "s2": "Not right now.",
            "s3": "Maybe after lunch.", "s4": "More options"}, "highlight": null}
```

The confirm screen after a pick of s1:

```json
{"type": "display", "text": "Yes, let's go outside.",
 "labels": {"s1": "Say it", "s2": "", "s3": "", "s4": "Back"}, "highlight": null}
```

- `text`: one line drawn at the centre (the picked sentence on the confirm screen, the highlighted
  option in list mode). `""` clears it.
- `labels`: per-slot text beside each target (page mode: each option's full sentence). `""`
  clears a slot's label.
- `highlight`: a slot to draw the box on (the picked target), or `null` to clear it.
- Never sent between `stim_start` and `stim_stop`, so the shown option cannot change during a
  trial.

**`stim_start`**: start flickering now and start decoding.

```json
{"type": "stim_start", "trial_id": 42, "active_slots": ["s1", "s2", "s3", "s4"]}
```

- The bridge turns the flicker on for `active_slots` only, calls `decoder.reset()`, drops samples
  read before this message, and steps the decoder until `stim_stop`.
- The confirm screen sends `"active_slots": ["s1", "s4"]`: the other squares stay drawn still, and
  the decoder chooses only between s1 and s4.

**`stim_stop`**: stop flickering and stop decoding.

```json
{"type": "stim_stop", "trial_id": 42, "reason": "selected"}
```

`reason`: `selected`, `no_selection`, `timeout`, `context_changed`, `paused`.

**`pause`**: STOP from the partner page. Flicker off at once, decoding off, until the next
`baseline`.

```json
{"type": "pause", "reason": "stop_button"}
```

STOP during a trial sends `stim_stop` (reason `paused`) and then `pause`; outside a trial it sends
`pause` only.

## 4. Bridge → server

**`stim_onset`**: the display's first flickering frame after `stim_start` (flip time from
`--markers unix`).

```json
{"type": "stim_onset", "trial_id": 42, "t_unix": 1792809201.512}
```

**`selection`**: the decoder's first commit after `stim_start`, at most once per `trial_id`.

```json
{"type": "selection", "trial_id": 42, "slot": "s1", "t_unix": 1792809206.530,
 "score": 0.041, "scores": {"s1": 0.041, "s2": 0.008, "s3": -0.004, "s4": 0.012}}
```

`slot` is always one of that trial's `active_slots`; `scores` cover at least the active slots.

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
`board_ok: false` is shown as a badge on the partner page; the session keeps running.

## 5. Rules

1. Only answer the current `trial_id`. Late answers for old trials are logged and ignored.
2. The flicker is on only between `stim_start` and `stim_stop`, and only for `active_slots`.
   Outside a trial the targets are drawn still.
3. **Safety:** if the server connection drops, the bridge turns the flicker off at once and keeps
   it off until a new `hello_ack` and `stim_start`. `Esc` on the display always stops the flicker
   locally, whatever the server says.
4. If nothing arrives by `max_trial_s` after `stim_start`, the server ends the trial
   (`stim_stop`, reason `timeout`).
5. The display text never changes during a trial (`display` is never sent then).
6. Only the bridge reads the board's serial port.

## 6. Agreed values (fill in at SYNC 1)

| Question | Proposed | Decoder owner's answer |
| --- | --- | --- |
| Number of targets | 4 in page mode (s1–s3 options, s4 NEXT; confirm: s1 Say it, s4 Back); 2 in list mode (SAY, NEXT), if the 4-target gate fails | |
| Frequencies | s1 13, s2 15, s3 17, s4 11 Hz (NEXT on 11 Hz; your `four_diamond.toml`); list mode 15 / 17 Hz (`PLAN.md` section 14) | |
| Layout | diamond: s1 top, s2 left, s3 right, s4 bottom; fixation mark in each square, sentence beside it | |
| Waveform | sine (17 Hz does not divide 60) | |
| Decoder window | 4 s now; 2 s if the replay tuning agrees | |
| Cue before each trial | 1.0 s | |
| Rest after each trial | 1.0 s | |
| Max trial length | 10.0 s | |
| Baseline length | 10.0 s | |
| `score` / `scores` sent | yes, `fbcca_minus_baseline` | |
| Display text during the flicker | yes (4 sentences), unless it drops frames (SYNC 1.5) | |

## 7. Bridge ↔ display (informational, the teammate's side)

Not part of the contract; listed so `display` maps cleanly onto the display's stdin. Today's
free-run feedback reads one integer per line (target index, `-1` clears) and ignores anything else.
Proposed additions, one JSON object per line, alongside the integers:

```
{"text": "Yes, let's go outside."}
{"labels": ["Yes, let's go outside.", "Not right now.", "Maybe after lunch.", "More options"]}
{"active": [0, 3]}
{"flicker": true}
{"flicker": false}
```

- `{"active": [...]}`: which squares flicker on the next `{"flicker": true}` (target indices, from
  `stim_start.active_slots`); the others stay still.

- After `{"flicker": true}` the display prints the Unix time of the first flickering frame, which
  the bridge sends as `stim_onset`.
- If the confidence bars (teammate's plan, phase D) also use this stdin, reserve `{"scores": [...]}`
  for them.

## 8. Testing each side alone

- Our side without the bridge: `python tools/fake_decoder.py --random` (or `--keyboard`). It must
  speak v2: send `hello` first, answer `baseline` with `baseline_done`, send `stim_onset`.
- The bridge without our server: `python tools/contract_check.py` is a stand-in server on
  `:8000/ws/decoder`. Start it, then the bridge (`python -m ssvep.online.bridge --fake` uses
  `FakeSource` instead of the headset). It runs a handshake, a baseline and a few trials (every
  second one a confirm trial with only s1 and s4 active), checks
  every message the bridge sends against the v2 schemas and the rules above, and prints pass or
  fail per message. Passing it is the SYNC 2 gate.
