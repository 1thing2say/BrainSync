# BCI Voice — Decoder ↔ UI Interface (v2 DRAFT)

**Status: implemented on both sides in this repo, waiting for the teammate's confirmation at
SYNC 1.** Since 2026-10-23 (`d4c1af9`) the server and the fake decoder speak only protocol 2 as
written here; v1 (`INTERFACE.md`) is superseded, and this file replaces it once confirmed. The
whole server side is built: hello check, `hello_ack` / `error`, nothing sent before `hello_ack`,
waiting for `baseline_done`, pausing on `display_ok: false`, and `tools/contract_check.py` for
testing the bridge. A bridge now exists in this repo too: `python -m brainsync.voice_bridge`
(`brainsync/voice_bridge.py`; `python -m app.server --bridge cerelog` starts it), driving the
PsychoPy display in `brainsync/stimulus` with the commands of section 7 and passing
`tools/contract_check.py`. It has not yet run on the headset or the real display window. Under
each open question below, "Our bridge" says how it answers it; the questions stay open for the
teammate. The teammate's own bridge, if they build one, must pass the same
`tools/contract_check.py`. Background: `PLAN.md` sections 12–14 and System design. Elijah's decisions of 2026-10-23
(`PLAN.md` section 13) are already written in.

**Added 2026-10-25, proposed: section 9, c-VEP (code) mode.** Additions only: a `hello` with
`paradigm: "code"` (slots with `lag_frames`), `train` / `train_progress` / `trained` in place of
`baseline` / `baseline_done`, and a `decision` message per decoder decision for the confidence
bars. Everything above stays valid for a frequency-coded bridge; our server runs it with
`--config config/fvep_page.yaml`, while its default config and our bridge now use code mode.

**Since 2026-10-25 the flicker the user looks at can be drawn by our page in Chrome**
(`display.flicker: browser`, `PLAN.md` status), not by the PsychoPy display. The protocol is
unchanged: the bridge runs with `--no-stimulus`, still gets `display`, `stim_start` and
`stim_stop`, and answers `stim_onset` from its own clock at `stim_start`; the display commands
of section 7 apply only with `display.flicker: psychopy`. Code mode needs each cycle's flip
time from whoever draws the flicker: the page reports it, and the server relays it to the
bridge as `cycle` (section 9.7, proposed), so the default code-mode config draws the code in
the page too.

## Open questions for the teammate (SYNC 1)

> **Claude session on the decoder side:** if you are working for the teammate (the `ssvep` repo or
> the bridge), bring these questions to the teammate before building the bridge, and record each
> answer here (or in a reply commit) so Elijah's side can follow it. Our server already behaves as
> the "proposed" answer says unless noted.

**Confirm the contract**

1. Is protocol 2 as written in this file OK for the bridge, and are the section 6 values right
   (4 targets at 13/15/17/11 Hz in page mode, as in your `four_diamond.toml`, 2 at 15/17 Hz as the list-mode fallback, sine, cue
   1 s, rest 1 s, max trial 10 s, baseline 10 s, scores sent)?
   *Our bridge (`brainsync/voice_bridge.py`):* It speaks protocol 2 as written and
   passes `tools/contract_check.py` (25/25 checks on the fake board). Its `hello` carries
   `four_diamond.toml`'s 13/15/17/11 Hz, sine, and the display's refresh. It differs on the
   decoder: 1.5 s windows every 0.25 s, 4 votes, margin and threshold 0.5
   (`brainsync.online.TUNED`), not 4 s / 0.5 s / 3 votes; scores are sent, `score_kind`
   `fbcca_minus_baseline`. Cue, rest, max trial and baseline lengths are the server's. The slots
   come from the stimulus config, so `-c two_targets` would send the 2-slot list-mode `hello`; not
   yet run against the server in list mode.
2. Where does the bridge live: `ssvep/online/bridge.py` in `1thing2say/ssvep`, or the `brainsync/`
   rebuild on branch `check-implemtation-calhacks-desktop` (`PLAN.md` section 13, item 9)?
   *Our bridge (`brainsync/voice_bridge.py`):* It lives in this repo, on the `brainsync/`
   decoder (the rebuild, now merged here).
   Elijah's decision of 2026-10-24, pending the teammate: the demo runs this one (`python -m
   app.server --bridge cerelog`), and `../ssvep` stays the research repo. A bridge the teammate
   writes in `ssvep/online/bridge.py` can replace it if it passes the same
   `tools/contract_check.py`.
3. Decoder window: 4 s, or 2 s if the replay tuning agrees? Can you run the 4-target gate
   recording (13/15/17/11 Hz in the diamond of your `four_diamond.toml`, `PLAN.md` section 14)
   first? Its result decides page mode or the list-mode fallback.
   *Our bridge (`brainsync/voice_bridge.py`):* 1.5 s (`--window`), from the replay grid on the cued
   recordings (README, phase 3); not yet checked live or with 4 targets. The gate recording is not
   done: `brainsync/stimulus/configs/four_diamond.toml` has the same targets and a 32-trial cued
   session (`python -m brainsync.record -c four_diamond --trials 32`, then `python -m
   brainsync.score_cued <file> --windows 2`; `docs/DEMO.md` section 2).

**What the display needs to do (section 7)**

4. Free-run mode flickers from the first frame today (`ssvep/stimulus/display.py`, `run_free`).
   Can it start still and flicker only between `{"flicker": true}` and `{"flicker": false}`? The
   bridge must also launch it without `--seconds`, or it quits by itself.
   *Our bridge (`brainsync/voice_bridge.py`):* Done in `brainsync/stimulus` (`feedback.py`,
   `display.py`). With `--hold-still` the free run starts still, and once a JSON line has arrived
   only `{"flicker": true}` / `{"flicker": false}` turn the flicker on and off; integer lines only
   move the box. The bridge launches `-c four_diamond --free --feedback stdin --hold-still
   --markers unix`, with no `--seconds`, and its first stdin line is always `{"flicker": false}`,
   so a highlight can never start the flicker. The run lasts until stdin closes or `Esc`.
5. Can the display print the Unix time of the first flickering frame after `{"flicker": true}`, so
   the bridge can send `stim_onset`? Today markers are printed for cued trials, not in free mode.
   *Our bridge (`brainsync/voice_bridge.py`):* Done: `[marker <unix seconds>] flicker_on` on stderr
   right after the flip of the first flickering frame, and `flicker_off` after the first still
   frame. The bridge sends `stim_onset` from the `flicker_on` marker and ignores one more than 50
   ms older than the trial's `stim_start`. Headless (`--no-stimulus`), the onset is `time.time()`.
6. `Esc` in free mode quits PsychoPy. Who relaunches the display, and does the bridge send `hello`
   again afterwards (the refresh rate may change)? Proposed: the bridge sends `status` with
   `display_ok: false`, relaunches the display, then `display_ok: true`; our server waits for the
   partner to press Resume and never restarts the flicker by itself.
   *Our bridge (`brainsync/voice_bridge.py`):* As proposed: `display_ok: false` at once, the bridge
   relaunches the display (every 2 s until it is up), restores the last screen held still, then
   sends `display_ok: true`. A new `hello` goes out only if the relaunched display's refresh is
   more than 0.5 Hz from the one in the first `hello`. Without `--no-wait` the relaunched display
   shows its press-space start screen, and `display_ok: true` goes out before Space is pressed
   (`docs/DEMO.md` says to press Space before Resume).
7. Can option text be drawn during the flicker without dropping frames (SYNC 1.5)? Page mode
   draws 4 labels, one full sentence beside each square. If not, the display hides the text on
   `{"flicker": true}` itself, because the server never sends `display` during a trial.
   *Our bridge (`brainsync/voice_bridge.py`):* Not measured yet (`docs/DEMO.md` section 2 has the
   two runs to compare). The sentences stay up during the flicker by default;
   `labels_during_flicker = false` in `four_diamond.toml` hides them while flickering. The bridge
   also ignores a `display` that arrives during a trial.

**Proposed rule additions (say yes, no, or change them)**

8. A `selection` or `no_selection` comes only after that trial's `stim_onset`. Proposed: the server
   ignores an answer without an onset (otherwise a SAY could be spoken while nothing flickered).
   `tools/contract_check.py` already fails it.
   *Our bridge (`brainsync/voice_bridge.py`):* Yes: a commit that comes before the onset is held,
   then sent right after `stim_onset`, with `t_unix` no earlier than the onset.
9. The bridge sends `hello` only once the board is streaming and the display's refresh check passed.
   *Our bridge (`brainsync/voice_bridge.py`):* Yes: `hello` goes out once the board is streaming
   and the display printed its refresh line (its refresh check passed). Without `--no-wait` that is
   before the display's start screen is dismissed with Space. On a connection made while the
   display is down (relaunching after `Esc`), `hello` waits until it is back up; only `status`
   goes out meanwhile.
10. A `stim_start` the bridge cannot carry out (before `hello_ack`, during a baseline, after `pause`,
    display down): proposed answer `no_selection` with reason `not_ready`, so the server does not
    wait the whole `max_trial_s`.
    *Our bridge (`brainsync/voice_bridge.py`):* Yes: `no_selection` `not_ready` before `hello_ack`,
    before the first `baseline_done`, while a baseline runs, after `pause` until the next baseline,
    and while the display is down. Two more reasons: `unknown_slot` for an active slot it does not
    have, and `board_stalled` when the board sends nothing for 5 s during a trial.
11. A second `hello` on an acked connection restarts the handshake: the server stops any trial
    (`stim_stop` paused), checks it again, and runs a new baseline before the next trial.
    *Our bridge (`brainsync/voice_bridge.py`):* The bridge sends a second `hello` only after a
    display relaunch with a different refresh; it then needs a new `hello_ack` and a new baseline
    before trials (`not_ready` until then). After `error` it sends nothing, not even `status`,
    until its next `hello`, which comes with a reconnect or after a display relaunch at a
    different refresh: fix its config and restart it.
12. A baseline cut short by `pause` or a disconnect answers `baseline_done` with `ok: false` and
    message `interrupted`; `baseline_done` never comes before `seconds` have passed.
    *Our bridge (`brainsync/voice_bridge.py`):* Yes: `ok: false`, `interrupted` on `pause` or a
    newer `baseline`, never before `seconds` have passed. A baseline it cannot start (before
    `hello_ack`, display down) also answers `ok: false`, with message `not_ready` or
    `display_down`, after `seconds`. A disconnect answers nothing (there is no server to
    answer), and an answer never goes to a later connection. It records at least window +
    lead-in + 8 steps of data (5.5 s with the defaults), so a `baseline` shorter than that
    answers after 5.5 s.
13. `active_slots` lists the slots that flicker in this trial: every slot on a page, but only two
    on the confirm screen ("Say it" on s1, "Back" on s4). The other squares stay drawn still, and
    the decoder classifies only among the active slots, so a `selection` is always one of them.
    `tools/contract_check.py` fails a selection of an inactive slot.
    *Our bridge (`brainsync/voice_bridge.py`):* Yes: `{"active": [...]}` then `{"flicker": true}`
    to the display, and the votes are counted only among the active targets (the margin and
    threshold rules use their scores alone), so a `selection` is always an active slot. `scores`
    cover every slot.
14. `refresh_hz` in `hello` is the rate the display's frame counts use. With `--refresh auto` a
    120 Hz (ProMotion) screen would pass the display's own check, but our server rejects anything
    more than 1 Hz from 60 Hz.
    *Our bridge (`brainsync/voice_bridge.py`):* Not strictly: `refresh_hz` is the display's
    measured rate (for example 59.94; the `using refresh` line with `--refresh auto`), while its
    frame counts use the config's 60 Hz unless `--refresh auto` is given. Both pass the server's ±1
    Hz check, and the example in section 2 says 59.94. To follow the rule word for word it would
    send the frame-count rate; the teammate's call.
15. `selection.t_unix` is `time.time()` when the decoder commits (`Decision.time` is stream seconds
    under `--fake`, so it should not be used directly).
    *Our bridge (`brainsync/voice_bridge.py`):* Yes: `time.time()` at the commit, never
    `Decision.time`.
16. Optional `stim_offset {trial_id, t_unix}` (first still frame) from the bridge, for the timing
    check in `PLAN.md` V5. Wanted, or skip?
    *Our bridge (`brainsync/voice_bridge.py`):* Not sent. The display already prints `flicker_off`,
    so it is a few lines if wanted.
17. Close code 4000 from our server means "a newer bridge replaced you": the old bridge should stop
    reconnecting instead of taking the connection back.
    *Our bridge (`brainsync/voice_bridge.py`):* Yes: on close code 4000 it stops reconnecting and
    exits 0.

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
| Cue before each trial | the server's: 2 s + 0.2 s per word on a new page, up to 6 s; 2 s for a page seen again; 1.5 s before Say it / Back (changed from 1.0 s on 2026-10-25, reading time) | |
| Rest after each trial | 1.0 s | |
| Max trial length | 10.0 s | |
| Baseline length | 10.0 s | |
| `score` / `scores` sent | yes, `fbcca_minus_baseline` | |
| Display text during the flicker | yes (4 sentences), unless it drops frames (SYNC 1.5) | |

## 7. Bridge ↔ display (informational, the display side)

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

**As built in this repo** (`brainsync/stimulus/feedback.py` and `display.py`, driven by
`brainsync/voice_bridge.py`):

- Launch: `python -m brainsync.stimulus -c four_diamond --free --feedback stdin --hold-still
  --markers unix`, no `--seconds`; it runs until stdin closes or `Esc`. It reads stdin from its
  start screen on, so lines never pile up in the pipe before Space and EOF there closes it too
  (exit code 0). Target indices follow
  `four_diamond.toml`: 0 = s1 top 13 Hz, 1 = s2 left 15 Hz, 2 = s3 right 17 Hz, 3 = s4 bottom
  11 Hz.
- stdin takes the five lines above plus the integer box line. `labels` needs one string per
  target (`""` clears one); `active` takes distinct valid indices; `flicker` must be a real
  true / false. `{"flicker": true}` restarts the phase at 0. A bad or unknown line (`scores`
  included, for now) is ignored with a note on stderr; nothing crashes the display. A rejected
  line that starts with `{` still counts as JSON, so integer lines never flicker after it.
- `--hold-still`: still until the first `{"flicker": true}`. A driver that sends only integer
  lines (`brainsync.demo`) still ends it with its first integer, so the bridge's first line is
  always `{"flicker": false}`.
- stderr: `measured refresh: <x> Hz` (or `using refresh <x> Hz`) once the refresh check passed,
  then `[marker <unix seconds>] flicker_on` after the flip of the first flickering frame and
  `[marker <unix seconds>] flicker_off` after the first still frame.
- `Esc` closes the display (exit code 0); the bridge treats any exit as display down. Every
  launch writes `{"flicker": false}` to the new process before anything else; the bridge
  writes from its own thread (a display that stops reading never blocks it), and when it stops
  it closes the display: EOF, then terminate after 2 s and kill after 1 s more.

## 8. Testing each side alone

- Our side without the bridge: `python tools/fake_decoder.py --random` (or `--keyboard`). It must
  speak v2: send `hello` first, answer `baseline` with `baseline_done`, send `stim_onset`.
- The bridge without our server: `python tools/contract_check.py` is a stand-in server on
  `:8000/ws/decoder`. Start it, then the bridge (ours: `python -m brainsync.voice_bridge --fake
  --no-stimulus`, fake EEG and no window; the teammate's, if built: `python -m
  ssvep.online.bridge --fake`). It runs a handshake, a baseline and a few trials (every
  second one a confirm trial with only s1 and s4 active), checks
  every message the bridge sends against the v2 schemas and the rules above, and prints pass or
  fail per message. Passing it is the SYNC 2 gate.

## 9. Proposed: c-VEP (code) mode

**Status: proposed (2026-10-25), additive.** Nothing in sections 1–8 changes: a bridge that
speaks them as written (frequency-coded, the teammate's) still works against a server started
with `--config config/fvep_page.yaml` (4 targets) or `config/fvep_list.yaml` (2 targets). Code
mode is what our own server (`config/default.yaml`) and bridge (`python -m
brainsync.voice_bridge`, default `--paradigm code`) now use by default: the BrainSync c-VEP
standard (`docs/CVEP-STANDARD.md`), three white squares on black, one 63-bit m-sequence, square
k delayed by k × 21 frames, trained on the first square, with the decoder's confidence shown
under every square.

**Targets.** 3 in a row: s1 left (option 1, and the training square), s2 middle (option 2), s3
right (NEXT, "More options"). The confirm screen flickers only s1 ("Say it") and s3 ("Back");
s2 stays still. Roles in `hello_ack`: `{"s1": "option", "s2": "option", "s3": "next"}`; the
labels are the two sentences and "More options". Stimulus config: `voice_cvep3`.

**What is added** (a server or bridge that does not speak code mode never sees any of it):

| Message | Direction | Instead of / beside |
| --- | --- | --- |
| `hello` with `paradigm: "code"`, slots with `lag_frames`, `code` {length, cycle_frames, train_cycles} | bridge → server | the frequency `hello` (no `paradigm` means `"freq"`) |
| `train` {train_id, slot, label, cycles, retrain} | server → bridge | instead of `baseline` |
| `train_progress` {train_id, cycle, of} | bridge → server | new |
| `trained` {train_id, ok, accuracy, chance, cycles, message} | bridge → server | instead of `baseline_done` |
| `decision` {trial_id, scores, raw, committed, lead, changed} | bridge → server | new, beside `selection` |
| `cycle` {train_id or trial_id, n, t_unix} | server → bridge | new, only when the page draws the code (9.7) |

### 9.1 Handshake

```json
{"type": "hello", "protocol": 2, "name": "brainsync-cvep", "paradigm": "code",
 "refresh_hz": 59.94,
 "slots": [
   {"slot": "s1", "lag_frames": 0},
   {"slot": "s2", "lag_frames": 21},
   {"slot": "s3", "lag_frames": 42}],
 "code": {"length": 63, "cycle_frames": 63, "train_cycles": 30},
 "decoder": {"window_s": 2.1, "step_s": 1.05, "votes": 2},
 "score_kind": "correlation"}
```

- `waveform` is left out (it does not apply); `decoder` says a decision comes every cycle
  (`step_s` 1.05 s) from the mean of the last 2 cycles (`window_s` 2.1 s), committed after 2
  agreeing decisions.
- The server checks: `protocol` 2; `paradigm` equals its config's `stim.paradigm` (a freq hello to
  a code server, or the other way round, gets `error` naming the config to start it with); the
  slot ids; each slot's `lag_frames` exactly; `code.length` and `code.cycle_frames` against
  `stim.code`; `refresh_hz` within 1 Hz of 60. `train_cycles` is the bridge's default and is
  not checked.

### 9.2 Training (instead of the baseline)

Server → bridge, where a frequency-coded session sends `baseline` (after `hello_ack`, and after
every pause, before the first trial):

```json
{"type": "train", "train_id": 3, "slot": "s1", "label": "left", "cycles": 30, "retrain": true}
```

- The bridge flickers every square, puts the cue box on `slot`'s square and shows "Look at the
  left square", and records `cycles` complete m-sequence cycles (about 32 s for 30) while the
  user looks at it. They become the template (shifted by each slot's lag for the other
  squares).
- `retrain: false` (every training after the session's first): a bridge that already has a
  template may answer `trained` at once. `retrain: true` (the session's first start, from the
  warning screen) always records anew.
- After every complete cycle:

```json
{"type": "train_progress", "train_id": 3, "cycle": 12, "of": 30}
```

- Then, with the leave-one-cycle-out accuracy of the template (0..1) against chance (1/3):

```json
{"type": "trained", "train_id": 3, "ok": true, "accuracy": 0.83, "chance": 0.333, "cycles": 30, "message": ""}
```

- `ok: false` means no template: `message` `interrupted` (a `pause` or a newer `train` cut it
  short), `not_ready` (before `hello_ack`), `display_down`, `unknown_slot`, or the decoder's
  reason (too few cycles could be cut, so the cycle markers are not arriving). The server
  pauses and shows it; Resume sends `train` again.
- The server waits up to `train.timeout_s` (60 s) for `trained`, then pauses. The bridge turns
  the training's flicker off by itself after its cycles × 1.5 + 15 s.
- A `baseline` sent to a code bridge is answered `baseline_done` `ok: false`, message
  `code mode: send train`, after its `seconds` (rule 12 as before).
- Until a `trained` with `ok: true` since the last `hello_ack` or `pause`, `stim_start` gets
  `no_selection` `not_ready` (question 10).

### 9.3 Trials

`display`, `stim_start` (with `active_slots`), `stim_stop`, `pause`, `stim_onset`, `selection`
and `no_selection` are unchanged. In addition, after every decision of the decoder during a
trial (one per complete cycle, about 1.05 s, the first about 2.6 s after the onset), and only
after the trial's `stim_onset`:

```json
{"type": "decision", "trial_id": 42,
 "scores": {"s1": 0.42, "s2": 0.11, "s3": -0.03},
 "raw": "s1", "committed": null, "lead": 0.31, "changed": false}
```

- `scores`: the Pearson correlation of each slot's shifted template with the mean of the last 2
  cycles: the confidence the bars show (clipped to 0..1). Not probabilities.
- `raw`: this decision's best **active** slot (null: it led the runner-up by less than the
  margin); `committed`: the slot committed so far (null before the first commit); `lead`: the
  best active slot's lead over the runner-up; `changed`: this decision moved `committed`.
- The decision that commits is followed at once by the `selection` (its `scores` the same
  correlations, `score_kind` `correlation`), and decoding stops, as before.
- The server relays each one to the partner page (`confidence`), whose mirror draws a bar under
  each option; the bridge draws the same bars on the user's display (below).

### 9.4 Bridge ↔ display in code mode (informational)

The display stdin protocol of section 7 is unchanged (`docs/CVEP-STANDARD.md` section 4.1). Our
bridge launches `python -m brainsync.stimulus -c voice_cvep3 --free --feedback stdin
--hold-still --markers unix` and:

- reads `[marker <unix seconds>] cycle:<n>` on stderr (every cycle's first flip) besides
  `flicker_on` / `flicker_off`; the cycle times, on the board's clock, are what the decoder
  epochs on;
- training: `{"active": [0, 1, 2]}`, `{"text": "Look at the left square"}`, `0` (the cue box on
  the training square), `{"flicker": true}`; `{"text": "training: keep looking at the left square
  cycle 12/30"}` per cycle; then `{"flicker": false}`, the cue box back where the server had it,
  and `{"text": "template ready: 83 % on held-out cycles (chance 33 %)"}`;
- trials: after every decision, `{"labels": [...]}` with each active square's sentence and a
  second line `███░░░░░ +0.42 <` (an 8-cell bar of the score clipped to 0..1, the signed score,
  `<` on the best), and, when the server's `text` is empty (a page, not the confirm screen),
  `{"text": "lead 0.31   choice: <sentence>"}`. The sentences themselves never change during a
  trial (rule 5); only the bar lines do. `--no-scores` turns the bars off (the `decision`
  messages still go out). Whether redrawing them once a cycle drops frames is not measured yet
  (SYNC 1.5).

### 9.5 Values (proposed)

| Question | Proposed |
| --- | --- |
| Targets | 3: s1 option 1 (left), s2 option 2 (middle), s3 NEXT (right); confirm: s1 Say it, s3 Back |
| Code | 63-bit m-sequence (LFSR x^6 + x + 1, seed 1), lags 0 / 21 / 42 frames, 1 bit per frame, 60 Hz |
| Training | 30 cycles on s1 at the session's first start (about 32 s); reused after a pause |
| Decision | every cycle, mean of the last 2 cycles, 2 agreeing decisions to commit, margin 0 |
| Max trial length | 10 s (a commit takes about 2.6 s at best) |
| Cue / rest | 1.0 s / 1.0 s, as before |
| `score_kind` | `correlation` |

### 9.6 Testing code mode

- `python tools/fake_decoder.py` speaks code mode with the default config (`--paradigm code`):
  `train_progress` per cycle over `--train-s` seconds (default the cycles × 1.05 s), `trained`
  with a fake accuracy, a `decision` about every cycle before each selection.
- `python tools/contract_check.py` follows the bridge's `hello`: a code hello is checked against
  `config/default.yaml` with two trainings (the first `retrain: true`) in place of the baselines,
  and every `decision` is checked (for the trial in progress, after its onset, scoring the
  active slots, `raw` / `committed` among them; at least one before each selection). A freq
  hello is checked against `config/fvep_page.yaml` as in section 8; `--config` picks one for
  both. Our bridge: `python -m brainsync.voice_bridge --fake --fast --no-stimulus` (code) or
  `--paradigm freq` (sections 1–8).
- `python tools/contract_check.py --page-cycles` also plays the page of section 9.7: a `cycle`
  per cycle during each training and each trial (from its `stim_onset`). The fake decoder counts
  them; our fake-board bridge ignores them (its board makes its own cycles).

### 9.7 Proposed: the session page draws the code (`display.flicker: browser`)

**Status: proposed (2026-10-25), additive; the default of our server and bridge.** When the
user's screen is our session page in Chrome rather than the PsychoPy display, the page draws the
three squares from the same bits (`ui/cvep.js`, a copy of `clients/js/cvep.js`: square k of
slot lag L is white on flicker frame n when `BITS[(n - L) mod 63]` is 1), counting n from the
first flickering frame of each training and each trial, and stamps the flip of every cycle's
first frame (n mod 63 == 0). The bridge runs headless (`--no-stimulus`) and has no display
markers, so the server relays those times:

```json
{"type": "cycle", "train_id": 3, "n": 12, "t_unix": 1792809214.112}
{"type": "cycle", "trial_id": 42, "n": 0, "t_unix": 1792809201.512}
```

- Server → bridge, one per cycle (about 1.05 s apart), exactly one of `train_id` (during a
  `train`, from its start) or `trial_id` (during a trial, after its `stim_onset`: the page
  starts flickering when the server has the onset). `n` counts cycles from 0 within that
  training or trial; `t_unix` is the flip on the server's clock, which is the board's
  (same Mac).
- The bridge treats each one like a display's `cycle:<n>` marker: the decoder epochs on
  `t_unix`. It ignores a `cycle` for a training or trial that is not in progress, and one with
  both or neither id. A bridge that draws the code itself (a PsychoPy display) ignores every
  `cycle`; so does a fake board, which makes its own.
- `stim_onset` stays the bridge's own clock at `stim_start` (logging only); the cycles carry
  the timing. A training that gets no cycles fails after its time limit with "no template in
  time (are the page's cycle messages arriving?)".
- Where they come from (server ↔ page, not part of this contract): the page sends
  `{"type": "cycle", "phase": "train" | "flicker", "trial_id": <its trial message's>, "n": 12,
  "t_client": <Unix s>}` per cycle, `t_client` the requestAnimationFrame timestamp on the wall
  clock (`Date.now() - performance.now()`, the maximum over recent samples, plus the rAF time,
  as the BrainScroll extension does), and `{"type": "clock", "t_client": …, "refresh_hz": …}`
  every 5 s. The server maps page times with the median offset of the last 8 clock messages
  (receipt minus `t_client`, as `brainsync/server` does with its pings), never later than the
  receipt, and drops a cycle stamped before its flicker was asked for. A page that measures a
  refresh more than 1 Hz from 60 Hz cannot start a session (one bit per frame).
- Not measured yet: Chrome's flip-time jitter against the board on the demo Mac (`PLAN.md` V5);
  a constant offset is learnt into the template, jitter is not.
