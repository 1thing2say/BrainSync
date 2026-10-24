# BrainSync

BrainSync is a two-target SSVEP brain-computer interface. Two squares flicker on the
laptop screen, one at 15 Hz and one at 17 Hz. You look at one. The visual cortex follows
that flicker, an 8-channel Cerelog X8 EEG board picks it up, and a training-free decoder
(CCA or filter-bank CCA) works out which square you are looking at. The cue box then moves
onto it and the label is spoken.

This repository is a from-scratch rebuild of an earlier demo. It reproduces each phase of
the original work from the data already recorded. Every number below was measured with
this code on 2026-10-23 (the phase 3 numbers on 2026-10-24, after the decoder gained its
rest threshold).

> **Photosensitivity warning.** Flicker between 15 and 25 Hz is the band most likely to
> trigger a seizure in people with photosensitive epilepsy. Both demo targets (15 and
> 17 Hz) lie inside it. Ask about epilepsy before every session. Stop at once if anyone
> feels unwell. The stimulus prints this warning to stderr each time it starts.

## Setup

The shell on the demo Mac runs under Rosetta (`arch` prints `i386`). The system
`python3` there is x86_64 and cannot import the arm64 numpy. Build the venv from an
arm64-only Python and always call `.venv/bin/python` directly:

```bash
/Users/school/.local/share/uv/python/cpython-3.12.11-macos-aarch64-none/bin/python3 -m venv .venv
.venv/bin/python -c "import platform; print(platform.machine())"   # arm64
.venv/bin/python -m pip install -e ".[dev,display]"
```

The last line ends with `Successfully installed brainsync-0.1.0`. The `display` extra
is PsychoPy (2026.2.4 here), needed only for the stimulus window. `dev` adds pytest.

The Cerelog X8 needs the Cerelog fork of BrainFlow, not the PyPI package. The fork sits
at `/Users/school/Documents/ssvep/third_party/brainflow-cerelog`. That folder is
read-only, so pip cannot build from it in place. Copy it first. The fork imports
`pkg_resources`, so setuptools must stay below 81:

```bash
.venv/bin/python -m pip install "setuptools<81"
BF="$(mktemp -d)/brainflow-cerelog"
cp -R /Users/school/Documents/ssvep/third_party/brainflow-cerelog "$BF"
.venv/bin/python -m pip install --no-build-isolation "$BF"
.venv/bin/python -c "from brainflow.board_shim import BoardShim, BoardIds; print(BoardShim.get_sampling_rate(BoardIds.CERELOG_X8_BOARD))"
```

The check prints `250` (plus a `pkg_resources` deprecation warning, which is harmless).
The fork's native libraries are macOS arm64 only.

## Data

The tools read data in place from one root folder, `$BRAINSYNC_DATA_DIR`. It defaults
to `<repo>/data` in this checkout (`~/brainsync_data` for a non-editable install). The
existing data lives elsewhere:

```bash
export BRAINSYNC_DATA_DIR=/Users/school/Documents/ssvep/data
```

The commands below assume this export. Under the root:

- `spectrum_checks/*_trials.npz`: single-target sessions (one flicker frequency each).
- `spectrum_checks/*_cued.npz`: two-target cued sessions (15 and 17 Hz).
- `nakanishi2015/s1.mat` .. `s10.mat`: the public 12-target benchmark.

There are 15 recordings: 12 trials files and 3 cued files. All of them load. Each has
the montage Fp1, Fp2, C3, C4, P7, P8, O1, O2 at 250 Hz. Analysis uses the posterior
channels P7, P8, O1 and O2. The default window is 3.86 s (965 samples), starting 0.14 s
after the flicker starts. After artifact rejection (window RMS over 3 times the median),
246 trial windows are kept. Do not write new files into the shared data folder by
accident: `brainsync.record` saves to `$BRAINSYNC_DATA_DIR/spectrum_checks` unless you
pass `--save-dir`. `record --fake` sessions go to a temporary `brainsync_fake` folder
instead, so synthetic files never mix with real ones.

## Hardware rules

- **60 Hz display, ProMotion off.** Set System Settings > Displays > Refresh rate to
  60 Hz. With ProMotion the panel changes its rate by itself, so the flicker on the glass
  is not the configured frequency. The first four sessions ran at about 120 Hz and showed
  nothing. The stimulus measures the refresh and exits with an error if it is more than
  1 Hz from 60 Hz. This holds when `brainsync.record` or `brainsync.demo` start it too:
  they then stop with that error. `--ignore-refresh` (all three tools) runs anyway.
- **Always full screen.** The stimulus has no windowed mode. Sizes and positions are in
  screen heights: the demo squares are 0.4 high and one screen height apart.
- **Let dry electrodes settle for about 25 minutes.** Contact noise is high at first.
  Run the electrode check (below) before recording.
- **Run on battery.** Unplug the laptop charger. It couples 60 Hz mains noise into the
  EEG.
- **Keep the cue box on.** Without a box around the target, gaze drifted after about
  2 s. The `two_targets` config holds it through the flicker.

## Phase 0: frequency selection

Which flicker frequencies does this user's EEG follow? Each single-target session
flickers one frequency f0. The tool tests the f0 templates against controls at f0 - 3 and
f0 + 3 Hz, on the flicker windows and again on the still-screen windows. A frequency that
also wins on a still screen follows a resting rhythm, not the flicker.

```bash
.venv/bin/python -m brainsync.select_freqs
```

Runs at about 120 Hz (ProMotion on) and the eyes-closed control are listed but left out.

| Run | f0 | CCA wins | Verdict |
|---|---|---|---|
| 121914 | 15 Hz | 11/15 | usable |
| 123700 | 15 Hz | 10/18 | usable |
| 145437 | 12 Hz | 7/18 | no response, yet phase-locked (PLV 0.81, p=3e-6) |
| 145858 | 10 Hz | 12/17 | rejected: still windows also win 13/17 (p=3e-4) |
| 150328 | 17 Hz | 15/17 | usable |
| 150837 | 12 Hz | 12/18 | usable |
| 151304 | 12 Hz | 0/18 | eyes-closed control, kept out |
| 154842 | 12 Hz | 1/20 | no response: 9 Hz alpha wins 19/20; phase-locked (p=0.022) |

So 10 Hz is unusable. It sits on the user's 9-10 Hz alpha. 12, 15 and 17 Hz are usable.
The pair table pools every eligible run at each frequency, one 3.86 s window per trial,
CCA with a still-screen baseline. The classes are imbalanced (12 Hz has 56 windows, 15 Hz
33, 17 Hz 17), so pairs are ranked by balanced accuracy (mean recall, chance 50% for a
pair, label-permutation p). Plain accuracy is shown beside the score of always naming the
larger class:

| Targets | Method | Balanced (eligible runs) | Accuracy | Always the larger class | Balanced, usable runs only | Balanced, original runs |
|---|---|---|---|---|---|---|
| 12/15 | CCA | 68% (n=89) | 65% | 63% | 81% | 64% |
| 12/17 | CCA | 82% (n=73) | 75% | 77% | 86% | 77% |
| 15/17 | CCA | 78% (n=50) | 78% | 66% | 78% | 86% |
| 12/15/17 | CCA | 65% (n=106) | 61% | 53% | 73% | 66% |
| 12/15/17 | FBCCA | 64% (n=106) | 61% | 53% | 68% | 62% |

Phase 0 does not settle the best pair. 12/17 Hz leads 15/17 Hz by 4 points of balanced
accuracy, but the 95% bootstrap interval of that lead is -11% to +20%, so the data do not
separate them. The tie holds at `--window 4` (+2%, -10% to +15%), `--reject 0` (+6%, -8% to
+20%) and `--latency 0`, where 15/17 leads by under 1 point (-14% to +14%). The bootstrap
resamples windows, which are not independent within a run, so the true uncertainty is
wider still. 12/17's plain 75% is no better than always naming 12 Hz (77%). The original
analysis's three runs pick 15/17 Hz (86%).

The demo keeps 15/17 Hz. The data allow either pair, and the arguments for 15/17 do not
come from this table: 12 Hz passed the f0 test in only 1 of its 3 eligible runs (15 Hz in
2 of 2), and 12 Hz sits closer to the user's alpha. The 1/f background favours low
templates at rest: the lowest template wins 81% of still windows for 12/15, which is why
every classifier subtracts a still-screen baseline.

## Phase 1: classifiers

CCA and FBCCA are checked on the Nakanishi 2015 benchmark (12 targets, 10 subjects,
256 Hz). The window starts 0.135 s after the stimulus onset in the file (sample 73).
Takes about 6 s.

```bash
.venv/bin/python -m brainsync.nakanishi --windows 1 2 4
```

Mean accuracy over the 10 subjects:

| Method | 1 s | 2 s | 4 s | Original (1/2/4 s) |
|---|---|---|---|---|
| CCA | 56.8% | 83.7% | 92.2% | 54/83/92% |
| FBCCA | 53.6% | 84.8% | 93.7% | 53/85/94% |

Per subject (CCA, FBCCA at 1/2/4 s) the results go to stderr. Subject 1 is the hardest
(CCA 24/44/64%), subject 8 the easiest (CCA 91/100/100%). Starting the window at sample 0
instead drops subject 8's 1 s CCA accuracy from 90.6% to 48.9%.

## Phase 2: two-target cued trials

Each cued session has 20 trials: a 1 s cue, 4 s of 15 and 17 Hz flicker with the cue box
on the target, then 3 s of still screen. Every trial gives a flicker window and a still
window. Each classifier gets that file's still windows as its rest baseline. The still
windows are also classified as a control: they should score near chance (50%).

```bash
.venv/bin/python -m brainsync.score_cued
```

| Session | Kept | Best (cell p, corrected p) | Highest still control |
|---|---|---|---|
| 162029 (reference, cue held) | 19/20 | FBCCA 17/19 (89%) at 2 s, p=0.0004, 0.004 | 14/19 (74%), shuffle p=0.19 |
| 172130 (smaller squares) | 17/20 | CCA 14/17 (82%) at 3.86 s, p=0.0064, 0.047 | 10/17 (59%), shuffle p=0.83 |
| 161131 (cue not held) | 14/20 | CCA 13/14 (93%) at 2 s, p=0.0009, 0.009 | 11/14 (79%), shuffle p=0.18 |

On the reference session, the still control at the best cell (FBCCA, 2 s) is 10/19 (53%).
"Best" and "highest still control" are each the maximum over 8 cells (2 methods x 4
window lengths), so each also gets a label-shuffle p for that maximum. The corrected p
of the best cell keeps 162029 and 161131 clearly significant. 172130 is borderline
(0.047). The still controls are consistent with chance in all three files. Options:
`--methods`, `--windows`, `--margin`, `--channels` (names or board indices) and
`--reject`.

## Phase 3: live demo

The online decoder takes the newest 1.5 s every 0.25 s and runs the same preprocessing
function as the offline tools on that segment plus 2 s of lead-in, which it then drops.
The lead-in removes the filter start-up at the old end. The zero-phase filter still has
an edge effect at the newest end, inside the window, and no lead-in can remove it, so
online windows are not identical to windows cut from a whole preprocessed recording (see
the agreement figures below). The window is scored with FBCCA minus a still-screen
baseline. The decoder abstains unless the best target both leads the runner-up by 0.5
rest spreads and sits 0.5 rest spreads above its own rest level. A target is committed
after 4 agreeing windows in a row.

The lead rule alone was not enough. Two independent rest scores differ by about 1.4
spreads, so a 0.5-spread lead abstained on only about a third of still-screen windows,
and with eyes closed too. The level rule asks whether there is any response at the
target frequencies at all. The user's 9.4 Hz eyes-closed alpha does not raise the 15 and
17 Hz scores, so it holds the decoder back there.

Replay the cued sessions through the decoder (about 6 s):

```bash
D=$BRAINSYNC_DATA_DIR/spectrum_checks
.venv/bin/python -m brainsync.online $D/2026-10-22_162029_cued.npz $D/2026-10-22_172130_cued.npz $D/2026-10-22_161131_cued.npz --idle $D/2026-10-22_151304_trials.npz
```

`--idle` runs each replayed decoder, with its baseline, over a target-free recording.
Here that is the eyes-closed control (166 s).

| Session | Correct commits | Wrong | No commit | Median time to a correct commit | Still screens with a commit | Eyes closed: abstained, commits |
|---|---|---|---|---|---|---|
| 162029 | 12/20 | 0 | 8 | 1.24 s | 5/20 | 73%, 7 |
| 172130 | 10/20 | 2 | 8 | 1.76 s | 6/20 | 80%, 3 |
| 161131 | 8/20 | 0 | 12 | 2.14 s | 6/20 | 78%, 5 |

With the lead rule alone (`--threshold none`) the same files give 18, 11 and 13 correct
commits but 2, 4 and 4 wrong ones. A target was then committed on 11, 14 and 9 of the
20 still screens, and about 27 times in the 166 s with eyes closed (abstaining on only
31-37% of windows). The threshold gives up 12 of those 42 correct commits for far
fewer commits when nobody is looking. Expect the cue box to stay off at first and to
take one or two tries.

On 162029 the baseline comes from 217 still windows (29 rejected), spread 0.080. The
real online-versus-offline comparison classifies the same windows cut from the whole
preprocessed recording, as score_cued does. It agrees on 93%, 99% and 94% of the 204,
202 and 202 windows fully inside the flicker. The edge effect above causes the
difference, so the phase 2 accuracies carry over only roughly. The tool also prints a
"self-check" at 100%. It re-runs the decoder's own preprocessing and only shows that
chunked streaming changes nothing.

The defaults came from a 576-setting grid over 162029 and 172130 (about 30 s):

```bash
.venv/bin/python -m brainsync.online --grid $D/2026-10-22_162029_cued.npz $D/2026-10-22_172130_cued.npz
```

The top row (FBCCA 1.5 s, step 0.25 s, 4 votes, margin 0, no threshold) gets 33 correct
and 7 wrong over both files. It was ruled out because it can never abstain. Margin 0.5
alone gets 29 correct and 6 wrong. Adding the 0.5 threshold gets 22 correct and 2 wrong,
and cuts still-screen commits from 25 to 11 of 40. The threshold was picked on these
same files plus 161131 and the eyes-closed run, so none of them is held out.

Try the whole demo loop without the board or a window. The fake board switches the
attended target every 10 s:

```bash
.venv/bin/python -m brainsync.demo --fake --fast --no-stimulus --no-voice --seconds 30
```

It runs in about 1 s (30 s calibration plus 30 s decoding in virtual time). It decodes
107 windows and commits 17 at 4.2 s, 15 at 11.9 s and 17 at 21.6 s. The committed target
matches the true one on 84% of windows.

The real demo (needs the board and opens the full-screen stimulus):

```bash
.venv/bin/python -m brainsync.demo
```

It connects to the board and opens the stimulus with the squares held still. It asks
you to keep still and calibrates the rest baseline on that still screen for 30 s
(`--calibrate`). Then the flicker starts and it decodes for 120 s. A 10 s baseline is
too short: it has only about 4 independent 1.5 s windows, and on a fake board its
15-vs-17 Hz rest offset was off by 0.58 spreads (SD over 40 seeds), more than the
margin. 30 s brings that to about 0.33. Ctrl-C or Escape ends the run. Ctrl-C during
calibration ends the program before any decoding (exit code 130). A stimulus that
crashes or refuses the display makes the demo exit with code 1.

## Recording and electrode check

Preview a session without opening a window:

```bash
.venv/bin/python -m brainsync.stimulus --plan --seed 3
```

It lists both targets (15 Hz at 4.000 frames per cycle, 17 Hz at 3.529) and 10 blocks
of 2 trials, 2 min 40 s in all. Each block cues each target once.

Record a synthetic session and score it (no board, no window, under 1 s):

```bash
OUT="$(mktemp -d)"
.venv/bin/python -m brainsync.record --fake --save-dir "$OUT" --seed 1
.venv/bin/python -m brainsync.score_cued "$OUT"/*_cued.npz
```

It saves 20 of 20 complete trials (10 at 15 Hz, 10 at 17 Hz). The synthetic file scores
20/20.

Synthetic electrode check (a 10 Hz tone on the posterior channels):

```bash
.venv/bin/python -m brainsync.check --fake --target 10 --no-show
```

P7, P8, O1 and O2 show about 18-20 dB SNR at 10 Hz. The frontal and central channels
stay near 0 dB. Over 6 dB is a clear SSVEP, 3 to 6 dB a weak one.

With the board attached, run the stimulus and the check in two terminals, then record:

```bash
.venv/bin/python -m brainsync.stimulus -c one_target_15 --free --seconds 60   # terminal 1
.venv/bin/python -m brainsync.check --target 15                                # terminal 2
.venv/bin/python -m brainsync.record                                           # 20 cued trials, 15/17 Hz
```

Stimulus configs: `two_targets` (the demo pair) and `one_target_12`, `one_target_15`,
`one_target_17` (single-target sessions for phase 0). Space starts a session, Escape
quits.

## Predicted-brain heatmap (optional, Reels demo)

A side panel for the Reels demo that shows what Meta's TRIBE v2 predicts an average brain's
fMRI response to the reel on screen would be. It is decoration: computed offline for our own
clips, never fed to the decoder, and labelled "Not your EEG" in the panel and on every frame.
TRIBE v2 is CC BY-NC 4.0 (non-commercial use only, credit Meta). Only use clips you own.

TRIBE needs its own venv: torch < 2.7 and Python 3.11+, on the arm64 Homebrew Python (the
Rosetta `/usr/local/bin/python3*` cannot install that torch). About 1.5 GB for the venv and
7 GB of models (TRIBE 0.7, V-JEPA2 ViT-g 4.1, w2v-BERT 2.3), all deletable:

```bash
/opt/homebrew/bin/python3.11 -m venv .venv-tribe
.venv-tribe/bin/pip install --no-cache-dir \
    "tribev2 @ git+https://github.com/facebookresearch/tribev2@af58661791a351a448a489042a28f6c37e1c14b7" \
    nilearn pytest
.venv-tribe/bin/pip install --no-cache-dir --no-deps -e .
# predict defaults HF_HOME to .tribe-cache/hf (git-ignored) and caps MPS memory so it fails
# instead of swapping; set either variable yourself to override
.venv-tribe/bin/python -m brainsync.heatmap predict beach.mp4 --clip-id beach   # auto: cuda, mps, cpu
.venv-tribe/bin/python -m brainsync.heatmap render beach --video beach.mp4
python3 -m http.server -d extension 8000    # http://127.0.0.1:8000/heatmap/demo.html
```

Only video and audio are used: the text path needs the gated Llama-3.2-3B and WhisperX, and
TRIBE drops it by itself when there are no words (it was trained with modality dropout). No
Hugging Face token is needed.

Measured on the M4 (24 GB) on 2026-10-24, 5 s test clip:

| Device | Predict time | Per second of video | Matches CPU fp32 |
|---|---|---|---|
| CPU, fp32 | 1030 s | ~206 s | reference |
| MPS, fp16, chunked attention | 316 s | ~63 s | r = 1.000 every second, 0.1% relative error |

Two fixes in `predict.py` make MPS work. torch 2.6's MPS attention builds the whole
8192 x 8192 score matrix for V-JEPA2 (a 6 GB allocation that pushed the Mac into swap), so
attention runs in exact 1024-query chunks. And neuralset's per-pass stack of 41 hidden
layers stayed on the GPU (5.9 to 13.8 GB in three passes, then the process died), so each
pass moves to the CPU and the GPU cache is emptied. Both apply on CUDA too.

Jetson Orin Nano 8 GB (not yet tried): one fp16 V-JEPA2 pass needed 5.2 GB of GPU memory on
the Mac. That is tight next to the OS on 8 GB of shared memory, so run predict there with
nothing else open, or keep predicting on the Mac and copy `extension/heatmap/clips/` over.
The panel itself is static files and runs anywhere Chrome does.

The panel (`extension/heatmap/`, see its README) sits in the left margin, mirroring the
squares, refuses any layout within 0.05 screen heights of a square, the reel column or the
HUD, does no per-frame work, and keeps its backdrop up for reels without a prediction so the
scene does not change after calibration. Still to measure on the headset: decoder
accuracy and dropped frames with the panel on versus off.

## Module map

| Module | Role |
|---|---|
| `brainsync/paths.py` | Data root (`BRAINSYNC_DATA_DIR`), recordings and Nakanishi folders |
| `brainsync/signal.py` | Zero-phase band-pass, 60 Hz notch, the one `preprocess` chain, filter bank |
| `brainsync/recordings.py` | Load and save `.npz` sessions, parse markers, cut windows, reject artifacts |
| `brainsync/synthetic.py` | Synthetic SSVEP trials and sessions for tests |
| `brainsync/metrics.py` | Accuracy with abstention, Wolpaw ITR, confusion matrix, binomial p |
| `brainsync/classify.py` | CCA and FBCCA with a still-screen baseline and abstain margin |
| `brainsync/nakanishi.py` | Nakanishi 2015 loader and accuracy table (phase 1) |
| `brainsync/select_freqs.py` | Phase 0 frequency selection report |
| `brainsync/score_cued.py` | Phase 2 scoring of cued sessions |
| `brainsync/stimulus/` | PsychoPy full-screen flicker, TOML configs, trial order, decoder feedback |
| `brainsync/board.py` | Cerelog X8 over BrainFlow, and a fake board |
| `brainsync/record.py` | Record a cued session while the stimulus runs |
| `brainsync/check.py` | Per-channel electrode check |
| `brainsync/online.py` | Ring buffer, sliding-window decoder, calibration, replay and tuning grid |
| `brainsync/launch.py` | Start the stimulus process; spoken prompts with macOS `say` |
| `brainsync/demo.py` | Phase 3 live demo |
| `brainsync/heatmap/` | Optional TRIBE v2 predicted-brain panel frames for the Reels demo (own venv) |

## Tests

```bash
env -u BRAINSYNC_DATA_DIR .venv/bin/python -m pytest -q
BRAINSYNC_DATA_DIR=/Users/school/Documents/ssvep/data .venv/bin/python -m pytest -q
```

Without data: 420 passed, 9 skipped (the real-data tests skip themselves), about 33 s.
With the data: 429 passed, about 45 s. No test opens a window or touches the board.
The filter tests check passband gain within 2% at 12, 15, 17 and 30 Hz, stopband gain
under 0.05 at 1, 2, 90 and 110 Hz, zero lag, and a 60 Hz notch deeper than -40 dB. The
real-data tests hold the Nakanishi and cued-session numbers above to minimum thresholds,
including the decoder's abstention on the eyes-closed run.
