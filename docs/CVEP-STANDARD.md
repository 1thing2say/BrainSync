# The BrainSync c-VEP standard: three white squares with confidence

Every BrainSync app that offers the user a choice uses this one configuration by default.
It came out of the headset sessions of 2026-10-25 (PR #28): code-modulated VEP (c-VEP,
Bin et al. 2009) with three targets, white squares on black, and the decoder's confidence
shown live under each square.

Reference code: `brainsync/vep/live.py` (Python: constants, `CodeVepDecoder`, confidence
helpers, `standard_fake_board`) and `clients/js/cvep.js` (browser: the same bits, frame
rule, bar and label text). The PsychoPy stimulus config is `cvep_3`.

## 1. Stimulus

| Item | Value |
| --- | --- |
| Targets | 3 (target 0, 1, 2) |
| Code | one 63-bit m-sequence, `brainsync.vep.codes.m_sequence(63)` (LFSR x^6 + x + 1, seed 1): `000001111110101011001101110110100100111000101111001010001100001` |
| Lags | target k shows the sequence delayed by k × 21 frames (0, 350, 700 ms at 60 Hz) |
| Frame rule | frame n of target k is **white** when `BITS[(n - 21 k) mod 63]` is 1, **black** when 0 |
| Cycle | 63 frames = 1.05 s at 60 Hz; n counts frames since the layout's first flickering frame |
| Colours | white squares `#ffffff` on a black background; a still (not flickering) square is drawn white |
| Display | fixed 60 Hz (ProMotion off). A browser that measures another rate keeps one bit per frame and reports its rate; the server checks it |
| Labels | under each square, with its confidence bar and score (section 3) |
| Safety | the photosensitivity warning and an explicit accept before any flicker; Esc stops it at once |

A display reports the time of every cycle's first flip (n mod 63 == 0): the PsychoPy display
prints the marker `cycle:<n>`; a browser sends a `cycle` message (section 4).

## 2. Training and decoding

- **Training.** The first 30 complete cycles (about 32 s) are recorded while the user looks
  at target 0, with the cue box on it and the text "Look at the <label> square". They become
  the template; the leave-one-cycle-out accuracy is shown against chance (33 %). A saved
  template can be reused (`Template.save` / `load`) for the same wearer and headset fit.
- **Decoding.** Every complete cycle gives a decision from the mean of the last 2 cycles:
  the Pearson correlation of each target's shifted template (its **score**, the confidence
  value), the best target (`raw`), and the **committed** target after 2 agreeing decisions.
  A new layout resets the votes.
- Python: `CodeVepDecoder(fs).feed(X, t, events)` does both; `events` are
  `(t_unix, "cycle", n)` tuples on the board's clock.

## 3. Confidence display

After every decision the display shows, under each square, one label line:

```
YES  ███░░░░░ +0.42 <
NO   █░░░░░░░ +0.11
HELP ░░░░░░░░ -0.03
```

the label, an 8-cell bar of the score clipped to 0..1, the signed score, and `<` on this
decision's best target; and one status line: `lead 0.31   choice: YES` (`*` after the choice
on the decision that changed it, `no choice yet` before the first commit). While training it
shows `training: keep looking at YES   cycle 12/30`, then
`template ready: 83 % on held-out cycles (chance 33 %)`. Scores are correlations, not
probabilities; do not show them as percentages. Python: `confidence_labels`,
`confidence_line`, `confidence(d, labels).message()`; JS: `labelLine`, `statusLine`, `bar`,
`level`.

## 4. Messages per transport

### 4.1 PsychoPy display (stdin of `python -m brainsync.stimulus --free --feedback stdin`)

Unchanged lines plus the ones PR #28 added: a target index (cue box), `{"text": "..."}` (the
status line, with `--status stdin`), `{"labels": ["...", "...", "..."]}` (the label lines,
with `--labels`). Cycle times are read from the stimulus log (`cycle:<n>` markers).

### 4.2 Server ↔ browser (PROTOCOL-v3, `brainsync.server --paradigm code`, the default)

- `welcome` adds `"paradigm": "code"` and
  `"code": {"bits": [0, 0, 0, 0, 0, 1, …], "lag_frames": 21, "cycle_frames": 63, "train_cycles": 30, "fill": "#ffffff"}`;
  `max_slots` is 3 and `freqs` is `[]`. `--paradigm freq` keeps the old behaviour.
- `layout` targets carry `"lag": 0 | 21 | 42` (frames) and `"freq": null`.
- New phase **`train`** (`flicker: true`): the targets flicker, the client draws the cue box
  on the slot named `"cue"` in the phase message, and the text says where to look:
  `{"type": "phase", "phase": "train", "flicker": true, "cue": "yes", "cycles": 30, "text": "Look at the YES square"}`.
  It comes after `calibrate` (or instead of it) and before the first `run`. In point mode the
  server shows a `stage: "train"` layout of three patches in a centred row.
- Client → server **`cycle`** on every cycle's first flip:
  `{"type": "cycle", "layout_id": 7, "n": 12, "t_client": 1792809201.512}` (Unix seconds,
  mapped with the same clock offset as `onset`).
- Server → clients **`train`** progress `{"type": "train", "cycle": 12, "of": 30}` and
  **`trained`** `{"type": "trained", "accuracy": 0.83, "chance": 0.333, "cycles": 30}`.
- Server → clients **`decision`** after every decision:
  `{"type": "decision", "layout_id": 7, "scores": {"yes": 0.42, "no": 0.11, "help": -0.03}, "raw": "yes", "committed": "yes", "lead": 0.31, "changed": false}`.
- `selection` is unchanged; its `score` / `scores` are now correlations.
- `gaze` (fake board) points the fake board's response at that slot's lag.

### 4.3 Local bridge ↔ browser extension (`brainsync/bridge.py`, BrainScroll)

The same names on `ws://127.0.0.1:8765`: the config message adds `"paradigm": "code"` and the
`code` object of 4.2; the extension sends `{"type": "cycle", "n": 12, "t": 1792809201.512}`;
the decoder sends `{"type": "train", …}`, `{"type": "trained", …}` and
`{"type": "decision", "scores": {…}, "raw": …, "committed": …, "lead": …, "changed": …}`.

## 5. Mapping each app onto three targets

| App | Target 0 | Target 1 | Target 2 |
| --- | --- | --- | --- |
| Demo / VEP demo (`cvep_3`) | YES | NO | HELP |
| BrainScroll Reels | Up (previous) | Like / pause | Down (next) |
| BrainSwipe | Pass | Super like | Like |
| BrainChess | first group | second group | Back (undo the last pick) |
| BrainPoint (point mode) | best candidate | second candidate | More |
| Voice partner (page mode) | option 1 | option 2 | NEXT (more options); confirm screen: Say it / Back on targets 0 and 2 |

The training target is always target 0, labelled with its app's name for it.
