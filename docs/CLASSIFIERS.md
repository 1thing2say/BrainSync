# Trained SSVEP classifiers: three options

Three pull requests each add a way to train an SSVEP classifier on the user's own cued
trials. All three are merged into main (#29, #32, #30, in that order, on 2026-10-25) and kept
side by side under distinct names; none is chosen yet. FBCCA (no training)
stays the live default everywhere. No PR has judged its method on held-out trials of a
4-target headset recording in the real layout (the 4-target gate, `PLAN.md` section 14). The
numbers the PRs report, with their conditions:

| PR | Data | Result (held out) |
| --- | --- | --- |
| #29 | Nakanishi 2015 public set, 1 s, raw templates | ITCCA 81 %, eCCA 94 %, eTRCA 98 % |
| #29 | Same, harmonic templates (the kind usable online) | eTRCA-R 92 %; 86 % from 2 training blocks |
| #29 | Three dry-electrode 15/17 Hz recordings, 1.5 s, pooled | FBCCA 80 %, eCCA 70 %, eTRCA-R 54 %, SFCCA 58 % |
| #30 | Recording 2026-10-24_235732 (4 targets, 11/13/15/17 Hz, 2x2 grid), P8,O1,O2, 2 s windows, 4 votes, margin and threshold 0.5, live replay, 29 trials | correct / wrong / commits on a still screen: FBCCA 9 / 5 / 6, TRCA phase-free 12 / 3 / 6, TRCA locked 18 / 2 / 9 |
| #32 | Nakanishi 2015, 1 s, leave-one-block-out, 10 subjects | CCA 56.8 %, FBCCA 53.6 %, eTRCA 97.8 % |
| #32 | Own 15/17 Hz recording 162029, live replay, correct commits of 20 | FBCCA 12, eTRCA 4 |

The methods reproduce the published results on the public dataset. On our 2-target
dry-electrode recordings, #29 and #32 found that no trained method beats FBCCA. #30 reports a
gain on one 4-target recording. That recording used channels P8,O1,O2 on a 2x2 grid, not the
diamond, and its lock was never run live with the headset. The gain has not been reproduced
on a second recording.

## Option A: #29, templates and spatial filters with onset-locked windows

**What it is.** `brainsync/templates.py`: ITCCA, eCCA (Nakanishi 2015), TRCA and eTRCA
(Nakanishi 2018), eTRCA-R (Wong 2020) and SFCCA (a learned spatial filter scored with a
plain CCA, which ignores phase). Classifier names: `itcca`, `ecca`, `trca`, `etrca`,
`etrcar`, `sfcca` (`brainsync.classify.TRAINED` on main). The templates are projected onto
the harmonics of their frequency, so a sliding window is scored at its offset from the
flicker onset (`Decoder.set_onset`).

**Record and train.** One command records the gate session, scores it, prints the gate
verdict and saves a model:

```
python -m brainsync.calibrate -c four_diamond                  # headset + display, 32 trials
python -m brainsync.calibrate --from <file>_cued.npz           # an existing recording
python -m brainsync.calibrate -c four_diamond --fake           # synthetic, no board or display
```

Useful flags: `--classifier` (which trained method to save, default `etrcar`), `--methods`
(default `fbcca ecca etrca etrcar sfcca`), `--windows` (default `1 1.5 2`), `--gate-window`
(default 1.5), `--channels` (default `P7,P8,O1,O2`), `--fb-bands N:UPPER`, `--out`,
`--no-save`. Scoring is leave-one-block-out.

Other held-out tools: `python -m brainsync.score_cued <file> --methods fbcca etrcar`
(leave-one-block-out), `python -m brainsync.nakanishi --methods ... --template raw|harmonic
--train-blocks N`, and the live replay `python -m brainsync.online --classifier etrcar
--train-blocks N <file>`. The replay trains on the first N blocks and scores the trials
after them.

**Model file.** `cache/templates/<recording>_<classifier>.npz` in the repo, or `--out`.

**Live use.**
- `python -m brainsync.demo -c four_diamond --model <file>`. `--model` replaces
  `--classifier` and `--harmonics`. The model's targets must match the config. The demo sets
  the onset from the newest board sample when the flicker starts, because the free-run
  display prints no onset marker on main. That estimate is a little late.
- `python -m brainsync.server --decoder-model <file> --freqs <the trained frequencies>`.
  Every layout must use exactly the trained frequencies and phases, or activating that
  layout fails. A smaller layout, such as the 2-target confirm stage, therefore fails too.
  This comes from reading `build_decoder`; it was not run.
- `brainsync.voice_bridge` / `app.server --bridge`: not supported. The bridge's
  `--classifier` takes only `cca` or `fbcca`.

**Needs.** At least 2 trials per target for the TRCA family and at least 2 blocks for
cross-validation. The default is 32 trials (8 per target).

**Tests.** `tests/test_templates.py`, `tests/test_calibrate.py`, `tests/test_online.py`
(also changes in `test_classify.py`, `test_score_cued.py`, `test_board.py`).

## Option B: #30, filter-bank eTRCA fused with FBCCA, phase lock, lean meter (`trca-phase`)

**What it is.** `brainsync/trca.py`, classifier name `trca-phase` (`classify.PHASE_LOCKED`;
it is `trca` in the PR, renamed in the merge because #29's `trca` is another class; saved
model files still say `trca`): a filter-bank ensemble TRCA (2 filters per target)
with harmonic templates. It is scored phase-free by default: each score is the best
correlation over a 48-step phase grid. It is fused with FBCCA, and an inner cross-validation
picks the weights (`--fusion cv`); a fusion of `(0, 1)` is plain FBCCA. With `--lock`, the
templates are scored at the phase the flicker has reached. That phase comes from the
display's `free_on` marker (the Unix time of the first flickering frame). The rest baseline
is then fitted at random phases. The demo also shows a lean meter: the square the scores
favour, a confidence bar and vote pips. To turn it off, use `--no-lean`. New configs:
`four_targets_low_first` (11/15/17/13 Hz, 2x2 grid; called `four_targets` in the PR, renamed in
the merge because #32's `four_targets` has another order) and `four_low` (11/13/14/12 Hz, phases 0/0/pi/pi, for users with
weak responses at 15 and 17 Hz).

**Record and train.**

```
python -m brainsync.record -c four_targets_low_first --trials 32
python -m brainsync.train <file>_cued.npz --channels P8,O1,O2 --lock
```

`brainsync.train` flags: `--channels` (default `P7,P8,O1,O2`), `--windows 1,1.5,2`
(comma-separated), `--window` (live window for the replay and the verdict, default 1.5),
`--step`, `--votes`, `--margin`, `--threshold`, `--reject`, `--n-comp`, `--harmonics`,
`--fusion cv|trca|fbcca|A:B`, `--out`, `--no-save`, `--no-replay`, `--lock`, `--json`.
Every trial is held out once. Folds go by each trial's rank within its target, which is
leave-one-block-out on a complete session. FBCCA is scored on the same folds. A verdict
line says whether TRCA beats FBCCA (an exact McNemar test or the binomial half-width).
Then a live replay counts correct, wrong and still-screen commits. Several files of one
setup can be passed together.

**Model file.** `data/models/<date>_<time>_trca.npz` (`$BRAINSYNC_DATA_DIR/models` if that
is set), or `--out`. The model is saved whatever the verdict says. `train` prints the matching
demo command.

**Live use.**
- `python -m brainsync.demo -c four_targets_low_first --classifier trca-phase --model latest
  --channels P8,O1,O2 --window 2 --lock`. With `--classifier trca-phase`, `--model` takes a
  path or `latest` (the newest `*_trca.npz`); `--model FILE` alone loads an Option A model.
  `--lock` reads the `free_on` marker of the demo's own free run; the bridge's JSON display
  path prints `flicker_on` instead, so the lock gets no onset there.
- `brainsync.server`: not supported (the PR does not touch the server).
- `brainsync.voice_bridge` / `app.server --bridge`: not supported.

**Needs.** At least 4 kept trials per target (`MIN_TRIALS`); 8 are recommended. The lock
needs the display's `free_on` marker. The PR body checked the marker on the real display
with no dropped frames.

**Tests.** `tests/test_trca.py`, `tests/test_train.py`, `tests/test_online.py`,
`tests/test_demo.py`, `tests/test_demo_session.py`, `tests/test_paths.py`,
`tests/test_stimulus.py`.

## Option C: #32, spatial filters held out, live, learning from confirmed picks (`-sf`)

**What it is.** In `brainsync/classify.py`: TRCA, eTRCA, their -R variants, and msCCA
(Wong et al. 2020). Names: `trca-sf`, `trca-r-sf`, `etrca-sf`, `etrca-r-sf`, `mscca`
(`classify.SPATIAL_FILTERS`; without the `-sf` in the PR, renamed in the merge because
`trca` and `etrca` are #29's). Templates that cover a steady-state period are folded into one
cycle. The decoder uses the same onset clock as Option A (`Decoder.set_onset`, seconds since
the onset; the PR's `align` was folded into it), and each window is scored at its offset; a
window with no onset abstains. New config `four_targets` (13/15/17/11 Hz on a 2x2 grid).

**Record and score.** There is no separate train step. The server fits the model from the
recording when it starts.

```
python -m brainsync.record -c four_targets --trials 32
python -m brainsync.score_cued <file>_cued.npz --methods fbcca etrca-sf mscca   # leave-one-trial-out, rows marked +
python -m brainsync.online --classifier etrca-sf <file>_cued.npz             # live replay, refitted without each trial
python -m brainsync.nakanishi --windows 0.5 1 --methods cca etrca-sf mscca   # public set, leave-one-block-out
```

**Model file.** None. `--trained` refits from the `_cued.npz` file at every server start.
The trials it learns during a session stay in memory; no code in the PR saves them.

**Live use.**
- `python -m brainsync.server --trained <file>_cued.npz [--trained-method etrca-sf]
  [--trained-window 2] --freqs <a subset of the trained frequencies>` (`--trained` and
  Option A's `--decoder-model` exclude each other). At startup, the server checks the sampling rate and that every `--freqs`
  frequency was trained. Each layout gets the model's subset of targets. It learns each
  confirmed pick, a `button` followed by `confirm` (PROTOCOL-v3 point mode), as a new
  training trial and writes a `learned` log record. `back` discards the pick.
- `brainsync.demo`: not supported. `brainsync.voice_bridge` / `app.server --bridge`: not
  supported.

**Needs.** At least 2 targets in the recording. Training windows are 2 s by default
(`TRAIN_WINDOW_S`). The PR body says the margin and threshold defaults were tuned for
FBCCA scores, and that correlation scores need their own tuning on the gate recording.

**Tests.** `tests/test_classify.py`, `tests/test_nakanishi.py`, `tests/test_online.py`,
`tests/test_score_cued.py`, `tests/test_server_decoding.py`,
`tests/test_server_session.py`, `tests/test_stimulus.py`.

## Comparison

| | A: #29 | B: #30 | C: #32 |
| --- | --- | --- | --- |
| Names | `itcca`, `ecca`, `trca`, `etrca`, `etrcar`, `sfcca` | `trca-phase` (filter-bank eTRCA fused with FBCCA) | `trca-sf`, `trca-r-sf`, `etrca-sf`, `etrca-r-sf`, `mscca` |
| Timing | onset-locked (`set_onset`); the demo's onset is an estimate | phase-free by default; phase-locked to `free_on` with `--lock` | onset-locked (`set_onset`, shared with A), templates folded to one period |
| Learns during use | no | no | yes, confirmed picks in `brainsync.server` (in memory) |
| Held-out scoring | leave-one-block-out (`calibrate`, `score_cued`, `nakanishi`); first N blocks (`online --train-blocks`) | rank folds, about leave-one-block-out, plus a live replay (`train`) | leave-one-trial-out (`score_cued`, `online`); leave-one-block-out (`nakanishi`) |
| Saved model | `cache/templates/*.npz` | `data/models/*_trca.npz` | none (refits from the recording) |
| Live path | `brainsync.demo --model`, `brainsync.server --decoder-model` | `brainsync.demo --classifier trca-phase --model --lock` | `brainsync.server --trained` |
| Voice app (`app.server --bridge`) | no | no | no |

The voice app's bridge (`brainsync.voice_bridge`) uses only `cca` or `fbcca`. To use any of
the three in the BCI Voice demo, the bridge needs a new flag. With `display.flicker:
browser`, its `stim_onset` is the bridge's own clock at `stim_start`, not the first
flickering frame. Options A and C (onset-locked) and B with `--lock` depend on that time
being accurate.

## How to choose

1. Record the 4-target gate session once, in the real layout, as `docs/DEMO.md` section 2
   says. Keep the headset on for 20 minutes first:
   `python -m brainsync.record -c four_diamond --trials 32`. Use this one file for every
   option. If possible, record a second session on another day to check the winner.
2. Score each option held out on that file, with the same channels (`--channels`) and the
   same windows (1.5 s and 2 s):
   - A: `python -m brainsync.calibrate --from <file> --windows 1.5 2 --no-save`, and
     `python -m brainsync.online --classifier etrcar --train-blocks 4 <file>`.
   - B: `python -m brainsync.train <file> --windows 1.5,2 --window 2 --lock --no-save`
     (not yet tried on a `four_diamond` recording: #30 was built on a 2x2 grid).
   - C: `python -m brainsync.score_cued <file> --methods fbcca etrca-sf mscca --windows 1.5 2`,
     and `python -m brainsync.online --classifier etrca-sf <file>`.
3. Compare each method with the FBCCA row from the same tool. The three tools hold trials
   out in different ways, so the gain over FBCCA compares better than the raw percentages.
   Count the live-replay commits too: correct, wrong, and on a still screen. A gap of a few
   trials is noise. With 32 trials, one trial is about 3 points.
4. Run the best option live on the headset (A or B with `brainsync.demo`, A or C with
   `brainsync.server`). Run it against FBCCA in the same session.
5. Write the decision, its numbers and the recording's name into `PLAN.md` (section 13,
   item 11, and the gate table in section 14). If the voice app should use the method, add it
   to `brainsync.voice_bridge`.

## Names after the merge

- `trca`, `etrca`, `etrcar`: Option A (`brainsync.templates`). `trca-phase`: Option B
  (`brainsync.trca`; its model files keep the `trca` kind and `*_trca.npz` names, so models
  saved before the merge still load). `trca-sf`, `trca-r-sf`, `etrca-sf`, `etrca-r-sf`,
  `mscca`: Option C.
- `classify.TRAINED` is Option A, `classify.SPATIAL_FILTERS` Option C,
  `classify.PHASE_LOCKED` Option B; `NAMES` / `METHODS` (what `online`, `score_cued` and
  `nakanishi` accept) hold A and C. B is trained only by `brainsync.train`.
- `brainsync.demo --model FILE`: an Option A model; with `--classifier trca-phase`, an
  Option B model.
- `four_targets.toml` is Option C's (13/15/17/11 Hz); Option B's is
  `four_targets_low_first.toml` (11/15/17/13 Hz).
- One onset clock on `online.Decoder`: `set_onset`. Option B's `--lock` uses it.
