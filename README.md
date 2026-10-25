# BrainSync

BrainSync is a two-target SSVEP brain-computer interface. Two squares flicker on the
laptop screen, one at 15 Hz and one at 17 Hz. You look at one. The visual cortex follows
that flicker, an 8-channel Cerelog X8 EEG board picks it up, and a training-free decoder
(CCA or filter-bank CCA) works out which square you are looking at. The cue box then moves
onto it and the label is spoken.

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

### Windows (an extra platform, optional)

Windows support is an **addition** from the `feat/windows-setup` branch. It does not
replace or change anything above: the demo Mac stays the main machine, macOS and Linux
keep this setup and `deploy/jetson`, and every code path they use is unchanged. On a
Windows laptop the SSVEP side runs in native Windows Python (not WSL: only Windows sees
the board's COM port, and only a native window flickers at a steady 60 Hz):

```powershell
py install 3.12                                                      # PsychoPy 2026.2 needs 3.10-3.12
powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1   # .venv-win, PsychoPy, BrainFlow
powershell -ExecutionPolicy Bypass -File deploy\windows\check.ps1   # charger, USB, COM port, 60 Hz
.venv-win\Scripts\python.exe -m brainsync.demo
```

The board's port is found by itself (`COM5` and so on). Details, including building the
Cerelog BrainFlow fork with Visual Studio: `deploy/windows/INSTALL.md`.

## Data
```bash
export BRAINSYNC_DATA_DIR=/Users/school/Documents/ssvep/data
```

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

## Flicker depth and the Flicker Lab

Every config flickers with a sine wave. `[stimulus] depth` (or `--depth` on
`brainsync.stimulus` and `brainsync.record`) sets how far it swings around mid-grey:
1.0, the default, is full black to white, and 0.5 is a gentler 25-75% grey flicker with
the same mean. A lower depth gives a weaker SSVEP, so no config lowers it yet.

`web/flicker-lab/index.html` compares the waveforms by eye. Open it in a browser, no
server needed. It draws with `extension/flicker.js`, the same luminance as the Reels
overlay, and has three views:
- **Side by side:** two squares, each with its own waveform, depth and frequency, and a
  trace of the grey level on every frame.
- **Blind comfort test:** a shuffled set of conditions, each rated 1-5 after it plays,
  with a per-condition table and a CSV.
- **Headset test:** the record and score commands for an accuracy A/B across depths.

### Live A/B with the headset

`python -m brainsync.ab` runs the real PsychoPy stimulus with the Cerelog X8 and compares
flicker conditions in one cued session. By default these are square 1, sine 1 and sine 0.5,
with 4 blocks × 2 targets × 3 conditions, so 24 trials in about 3.6 minutes.
- **Trial order:** each block shows every condition at every target once, shuffled, so
  fatigue and electrode drift affect all conditions equally.
- **Still screen:** it is the same mid-grey whatever comes next.
- **Scoring:** each trial is scored once its still window has been recorded. A
  per-condition table goes to the terminal and appears under the squares between
  flickers. It shows whether the decoder picked the cued square, the SNR at the cued
  frequency, the CCA r and, with `--rate`, the 1-5 comfort rating.
- **Blind ratings:** with `--rate`, nothing reveals a trial's condition until you have rated it.
- **End screen:** it holds the final table until you press space.
- **Saved file:** the session is saved as a `_cued.npz`.

```
python -m brainsync.ab --rate                       # headset + display
python -m brainsync.ab --conditions sine:1,sine:0.6,sine:0.4
python -m brainsync.ab --fake                       # synthetic, no board or display
python -m brainsync.ab --score <file>_cued.npz      # the same table from a saved session
```

The still-screen line should sit near 0 dB. With 8 trials per condition, differences
smaller than about twice the ± values are noise. At 60 Hz, 17 Hz cannot be an even square
wave (3.53 frames per cycle), so square at 17 Hz is irregular, and the tool warns about it.

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

## Phase 4: Instagram Reels

`brainsync.reels` scrolls Instagram Reels in Chrome with the same decoder. The BrainScroll
extension (`extension/`, details in `extension/README.md`) draws two squares to the right
of the reel on instagram.com/reels: the top one, "up", at 15 Hz goes to the previous reel,
the bottom one, "down", at 17 Hz to the next (config `reels`: squares 0.3 high at
x = 0.55, y = +/-0.325 screen heights, so they stay clear of the 9:16 video). Python is a
WebSocket server on `ws://127.0.0.1:8765`. It sends the layout, the phase (wait,
calibrate, run, stop), the cue box and each scroll. The extension answers with a hello
(after Start on its photosensitivity warning and a refresh measurement over 120 frames),
a frame report every 5 s, an ack per scroll and `bye` on Escape. The protocol is written
out in `brainsync/bridge.py`. Only the extension and pages served from this machine may
connect (the handshake's Origin is checked), so another web page cannot end the run or
fake a hello. Each tab has its own socket, so a tab that opens or reloads mid-run gets
the config and the current phase. After a reconnect the overlay greets the new socket
again without a second Start. Every connection gets every scroll, but only a visible tab
on Reels where Start was pressed moves the feed; the others ack `ok: false`, and Python
counts one ack per scroll id. Escape in any tab ends the run, also before calibration
(exit 0, "viewer quit").

Decisions count only while some tab shows the squares. Each tab says whether its squares
are in view in its hello and again in a `view` message each time that changes: they are
out of view in a background tab, on another Space, in a covered window or after leaving
Reels. While no tab shows them, decisions are still scored and logged (`"hidden": true`),
but none scrolls and the cue box is cleared. When the squares come back the votes are
reset, so a scroll needs fresh windows. The summary says how long they were out of view.
On the first headset run the viewer kept switching to the terminal. The decoder committed
15 times while Chrome was hidden, 12 of them "down", with nothing on screen to look at.
Only the 2 scrolls with the squares in view reached Instagram, and both moved the reel.
Keep Chrome and the terminal side by side, both visible: a tab only has to be visible,
not focused.

Decoder commits become scrolls in `brainsync/scroll.py` (`ScrollGate`):

- A new commit fires one scroll in that target's direction. Abstentions never fire, and
  watching the video abstains.
- Every decision for 1.5 s after a scroll is ignored (`--cooldown`).
- The first decision after the cooldown resets only the decoder's votes, not its sample
  buffer. Refilling the buffer (1.5 s window + 2 s pad) would blind it for 3.5 s.
  Someone who keeps looking at one square commits it again after 4 more agreeing
  windows, so it scrolls about every 1.5 + 1.0 = 2.5 s. At 250 Hz a step is 62 samples
  (0.248 s), so the reset falls on the 7th decision and the measured period is 2.73 s.
- After a reset a commit counts only once 4 decisions have arrived. One board read can
  produce several decisions, and a sooner commit was voted before the reset.

The rest baseline is calibrated while the viewer watches the reel with the squares
flickering in the periphery (`--calibrate-on watch`, the default). Here "not choosing"
means watching the video, not a still screen. `--calibrate-on still` holds the squares
still instead, as phase 3 does. This choice and the closer spacing below are not
validated on real EEG yet: one headset run so far (above), with too few scrolls in view
to judge accuracy.

Setup: install the `reels` extra (websockets). Load `extension/` unpacked at
`chrome://extensions` (Developer mode, Chrome 116 or newer). Put Chrome in full screen
(ctrl-cmd-F), because sizes are in screen heights and the overlay takes the window height
as the screen. Set the display to 60 Hz as in the hardware rules. The run stops if the
browser's measured refresh is more than 1 Hz off, unless you pass `--ignore-refresh`.
Either way the overlay holds its squares still while its own refresh is off, because
they would flicker at the wrong frequencies, and a later hello at the wrong refresh
(another tab, a reload) prints a warning.

```bash
.venv/bin/python -m pip install -e ".[reels]"
```

Without a browser or board (about 1.3 s, virtual time):

```bash
.venv/bin/python -m brainsync.reels --fake --fast --no-browser --no-voice --seconds 30
```

It decodes 107 windows (1% abstained) and scrolls 10 times: down at 4.24, 6.97 and
9.70 s, up at 12.43, 15.16 and 17.88 s, then down 4 times from 21.60 s. The repeats sit
2.73 s apart. The committed target matches the true one on 62% of windows, against 84%
for `brainsync.demo`, because every reset clears the committed target for about 1 s.

On the mock Reels page (no Instagram login), in two terminals:

```bash
.venv/bin/python -m http.server -d extension 8000 --bind 127.0.0.1
.venv/bin/python -m brainsync.reels --fake --no-voice --seconds 60 --calibrate 10
# open http://127.0.0.1:8000/test/reels_mock.html and press Start
```

Measured on 2026-10-24 in headless Chrome 156 on this Mac:

- The hello arrived with a 60.00 Hz refresh (59.99 Hz in a second run). The old median
  estimate read 59.88 Hz here, because Chrome rounds rAF timestamps to 100 us and the
  60 Hz intervals come out as 16.6 or 16.7 ms; the overlay now takes the mean of the
  intervals near the median, and the frame reports said 60.04 Hz.
- The HUD showed 10 s of calibrate (flicker), then run.
- The 15 Hz square took only the values 0, 127, 128 and 255, the four levels of a 15 Hz
  sine at 60 Hz. The 17 Hz square took 31.
- In 45.4 s of decoding there were 15 scrolls. 12 were acked ok and moved the mock feed
  by one reel each. The other 3 were "up" at the top of the feed: they were acked
  `ok: false` and nothing was tried.
- 11 frame reports arrived. The overlay counted 12 dropped frames in about 55 s of
  flicker.
- Escape sent `bye`, and Python ended with "viewer quit" and exit code 0.

`--manual` needs neither board nor decoder: after the hello, stdin lines `u` and `d`
scroll and `q` quits. That is how to check the extension on the real site:

```bash
.venv/bin/python -m brainsync.reels --manual
```

On the mock page, `d`, `d`, `u` moved the feed from reel 1 to 3 and back to 2, with 3 of
3 acks ok. A second `--manual` run started straight after got its hello from the same
tab without another Start.

The real run (board, Chrome on instagram.com/reels with the extension, 30 s baseline,
600 s of decoding):

```bash
.venv/bin/python -m brainsync.reels
```

It looks for the board's USB receiver before anything else. With none plugged in, the
terminal says "no EEG device connected" (listing the device names it looked for) and the
overlay shows "No EEG device connected. Plug in the Cerelog X8 USB receiver." It waits
`--wait-device` (120 s) for the receiver, then exits 1. If the receiver is there but the
headset does not answer, the overlay says "Could not connect to the EEG board". After
connecting it waits up to 5 s for the first samples, so a receiver whose headset is
switched off is reported as "no data" rather than as connected. The overlay's corner
panel has an EEG line that follows these steps (not connected, connecting, receiving
data, no data; "not used" in `--manual` mode).

One status line per window goes to stderr. Each scroll and a summary (scrolls, acks,
the last refresh and drop count) go to stdout. `--log FILE` writes JSON lines with every
decision, scroll, reset and browser report. Exit codes:

- 0: the run ended on time, Ctrl-C or Escape in the browser.
- 1: no EEG device was found within `--wait-device` (120 s) or it would not connect,
  no browser said hello within `--wait-browser` (120 s), the refresh was wrong, the
  port was taken, calibration failed, or the board sent nothing for 5 s.
- 2: bad arguments or config. `-c two_targets` is refused because its labels are not
  up/down.
- 130: Ctrl-C before decoding started.

The overlay is told to stop on every exit Python lives through: normal ends, errors,
Ctrl-C, and SIGTERM and SIGHUP (closing the terminal window), which count as Ctrl-C. For
the rest (SIGKILL, a crash) the overlay holds the squares still as soon as its socket
closes, until a server sends a flickering phase again. Both were checked on the mock
page: `kill -TERM` mid-run delivered the stop phase and Python ended with "interrupted",
exit 0; after `kill -9` the squares stood still and no more frame reports went out.
Escape during calibration stops the flicker at once, but Python ends the run only when
the baseline is finished.

Caveat on spacing: the Reels squares are 0.3 high and 0.65 screen heights apart, against
0.4 high and 1.0 apart in phase 2 and 3. No session has measured the response or the
crosstalk at that spacing. Before trusting it, record a cued session in the Reels layout
and score it (`brainsync.record -c reels`, then `brainsync.score_cued`). The cue box fits at
`cue_scale = 1.1` (1.2 would reach 0.505 screen heights, past the top edge). The extension
narrows the box's gap from 0.03 to 0.019 there for the same reason.

## BrainSwipe: doomscroll with your eyes

BrainSwipe runs the phase 3 demo with a dating-profile card drawn between the two
squares. Look at the left square (target 0, 15 Hz) to **Pass**. Look at the right
square (target 1, 17 Hz) to **Like**. Each square has its name written under its cue box
in its flash colour (static text, outside the square, so the flicker is unchanged). When a
swipe fires, that square's box flashes
(red for Pass, green for Like) for 0.35 s, the next card appears and a voice line is
spoken: "Pass", "Liked Priya", or on a match "Priya liked you back. It's a match!". If
you keep looking at the square, it keeps swiping. The feed never ends: it is a shuffled
order of the deck, shuffled again each time every card has been shown, and the same card
never appears twice in a row.

Every profile is made up. The 20 in the bundled deck (`brainsync/swipe_deck/profiles.toml`,
ages 23 to 38) are invented people with prompt-and-answer pairs in the style of dating
apps ("A shower thought I recently had", "My simple pleasures"). Each card is drawn, not
photographed: an abstract accent-colour panel with the person's initials in a circle. No
real photos and no app name or logo appear anywhere. The cards are 600x900 px PNGs (2:3)
on a dark background. Across the bundled deck, mean luma is 0.18 to 0.26 and the
brightest pixel is 0.87 (white is 1), so the card stays dimmer than the flicker it sits
next to.

The photosensitivity warning above applies unchanged. The flicker is the same 15 and
17 Hz.

Try it without the board or a window:

```bash
.venv/bin/python -m brainsync.swipe --fake --fast --no-stimulus --no-voice --seconds 60
```

It runs in a few seconds (30 s calibration plus 60 s decoding in virtual time). The fake
board switches the attended target every 10 s. One status line per window goes to
stderr, and the summary goes to stdout:

```
swipe: 59.8 s decoded, 18 swipes (18.1 per minute): 10 likes, 8 passes, 4 matches; 144 stale windows ignored; ended: time up
  swipes: 4.2s->Like  7.2s->Like  10.2s->Like  13.2s->Pass  16.1s->Pass  19.1s->Pass  22.1s->Like  25.1s->Like  28.1s->Like  32.0s->Pass  35.0s->Pass  38.0s->Pass  41.4s->Like  44.4s->Like  47.4s->Like  50.4s->Like  53.3s->Pass  56.3s->Pass
  fake board: 16 of 18 swipes (89%) went to the true target
```

Both misses (10.2 s and 50.4 s) fired within 0.4 s after the board switched targets.
Most of their window still came from the old target. "True target" is read at the moment
the swipe fires, so these count as misses. Over 8 fake-board seeds, a 60 s run gives 18
to 19 swipes, and 84% to 94% go to the true target.

The real thing (needs the board and opens the full-screen stimulus):

```bash
.venv/bin/python -m brainsync.swipe
```

The run follows the demo's sequence. First the deck is rendered to a temporary folder.
The stimulus opens with the squares still and the first card already showing. Keep still
and read it while the rest baseline calibrates for 30 s. Then the squares flicker and
decoding runs for 300 s (`--seconds`). The demo's flags all work here, as do its exit
codes and its Ctrl-C and Escape handling. BrainSwipe adds:

- `--cooldown` (default 0.5 s): how long after a card change before a window may vote.
- `--deck PATH`: a different profiles TOML.
- `--match-rate` (default 0.3): the chance that a Like is a match.
- `--seed`: sets the card order and the matches, plus the noise with `--fake`. By
  default each run on the real board picks a fresh random seed, so the feed starts on a
  different profile every session; with `--fake` it defaults to 0 so runs repeat. The
  seed in use is printed on the `deck:` line.

**Why swipes need fresh windows.** Each decoder window is 1.5 s long and a new one comes
every 0.25 s, so consecutive windows overlap. Just after a swipe, the decoder is still
scoring EEG recorded while you looked at the previous card. Your eyes also need a moment
to land again. So a window counts as stale, and is ignored, if it started before the card
change plus the cooldown. A swipe fires only after 4 fresh windows in a row (`--votes`)
name the same square. BrainSwipe counts these votes itself; it does not use the decoder's
own commit, which also counted the stale windows. The cue box moves onto a square after
2 fresh windows in a row, so you can see a swipe building up. The card change is counted
from the end of the board read in which the swipe fired, because the new card can only
go up after that read is decoded. So a board that lags and then delivers seconds of
backlog at once swipes at most once per backlog: the rest of it is EEG from the old
card.

With the defaults, this means swipes are at least 0.5 + 1.5 + 3 x 0.25 = 2.75 s apart.
The decoder only steps in whole 62-sample hops (0.248 s at 250 Hz), so the true minimum
is 744 samples, or 2.98 s. Holding your gaze gives one swipe about every 3 s (about 20
per minute), and the run above shows that spacing. After each swipe, the 8 windows that
come next are stale: that is the 144 stale windows (18 x 8).

**Untested.** No session has been run on the real board yet. The false-swipe rate while
reading a card is unmeasured. While you read, the flickering squares stay in your
peripheral vision. The rest baseline was calibrated with the squares still, so reading
could look like looking at a square. The planned test has two parts:

1. Look only at the card for 60 s and count the swipes. Every swipe is a false one.
2. Swipe on purpose for 2 min, saying each intended choice aloud. Count correct, wrong
   and missed swipes.

Until that has been done, the fake-board figures above say only that the logic works.
They do not show how well it works for a person.

The deck is TOML, made of `[[profile]]` tables:

```toml
[[profile]]
name = "Priya"
age = 29                      # integer, 18 to 99
color = "#c86b5a"             # accent for the drawn avatar
detail = "Product designer · Oakland"   # optional
prompts = [
    ["A shower thought I recently had", "If you microwave a burrito long enough it becomes a different burrito."],
    ["My simple pleasures", "Farmers market peaches, a fresh notebook, and the first sip of a too-hot chai."],
]
```

Each profile has 1 to 3 prompts. A bad entry stops the program with
`error: <file>: profile <i>: ...`. Answers longer than about 120 characters are set in a
smaller font or cut with an ellipsis. To preview the cards and their placement without a
window:

```bash
OUT="$(mktemp -d)"
.venv/bin/python -c "from brainsync.profiles import load_deck, render_deck; render_deck(load_deck(), '$OUT')"
.venv/bin/python -m brainsync.stimulus -c swipe --free --plan --cards "$OUT"
```

The plan reports `cards: 20 card(s) ... 0.6 screen heights high (0.400 wide at 2:3),
flash 0.35 s`. The card fills |x| < 0.2. The cue boxes start at 0.26 and the squares at
0.3, so nothing overlaps. The config validation warns if a `[card]` height would make the
card overlap the boxes or squares. The card and the flash are drawn as separate
stimuli, so they never change the squares' luminance. A test checks that the flicker
sequence is identical with and without them.

### Browser mock

`brainsync.swipe_web` shows BrainSwipe in a web browser, without the headset and without
any flicker. It is for showing the app when nobody is wearing the board. A local server
runs the same `Swiper`, `Feed` and FBCCA decoder as the live demo, on a fake board in
real time. You play the eyes. Point at a square with the mouse and the server sets the
fake board's tone to that square's frequency. The real `Swiper` then decides when a
swipe fires, so the page keeps the timing of the brain interface: a swipe builds up over
fresh windows, repeats about every 3 s while you keep looking, and the stale windows
after each card change are ignored.

```bash
.venv/bin/python -m brainsync.swipe_web --open
```

After about 1 s it prints `BrainSwipe mock at http://127.0.0.1:8780 (simulated EEG board,
no flicker); Ctrl-C stops`. The 30 s rest baseline (`--calibrate`) is fitted in virtual
time on a separate fake board, which is why startup is quick. The first decoder window
arrives about 3.5 s later, once the buffer is full. `--port 0` picks a free port and
prints its URL. The server uses only the standard library and the page loads only local
files, so it works offline.

The page shows the card between a still Pass square (left, 15 Hz) and a still Like
square (right, 17 Hz). Nothing on it flickers: browser frame timing has not been checked
for SSVEP, and a still page is safe for everyone in the room. The header says the page is
a mock driven by a simulated EEG board. The squares are 0.4 of the stage height. When the
window is wide enough they sit further apart than the stimulus's one stage height, so the
card can be as tall as the stage and stays readable on a laptop or a projector (434 px tall
at 1366x650, 384 px at 1024x600).

| Control | Effect |
|---|---|
| Hover a square or hold ← / → (touch: press and hold) | Look at that square: the fake board plays its tone |
| Leave the squares, release the key | Read the card: no tone |
| Shift+← / Shift+→, or click a label | Swipe at once, without the decoder (dropped if the decoder has just swiped that card) |
| Auto (A) | The fake board picks its own target and switches every 8 s (`--switch`) |
| Mute (M) | Stop the spoken lines |

Each square fills as fresh windows agree on it, out of 4. After a swipe both squares show
"settling" while the windows are stale. When a swipe fires, the square's outline flashes
for 0.35 s, the card slides off to that side (a crossfade with reduced motion), the voice
line is spoken by the browser, and a match shows a banner. Chrome speaks only after the
page has had a click or a key press. The footer shows swipes, swipes per minute, likes,
passes, matches and stale windows ignored, plus each window's two scores and the fake
board's true target.

An instant swipe names the card the page shows. If the decoder swiped that card a moment
before (the key press and the swipe event crossed), the server answers 409 and the page
moves on with the decoder's swipe, so the next card is never swiped unseen.

`--no-decoder` runs no board and no decoder: plain arrows and clicks swipe at once.
`--cooldown`, `--deck`, `--match-rate` and `--calibrate` work as in `brainsync.swipe`.
`--seed` defaults to 0, so the feed and the noise repeat from run to run. Ctrl-C,
`kill -INT` or SIGTERM stops the server and removes the rendered cards (exit 0), also when
it was started in the background, where the shell ignores SIGINT. A busy port exits 1
with `error: port 8780 is in use; try --port 0`, also when the other server listens on
0.0.0.0 (macOS would otherwise let both bind).

Measured on the fake board in real time, through the page and the HTTP API (seed 0,
default flags). These are fake-board numbers. They show that the logic and its timing
work, not how well a person can use it.

- From moving onto a square to the first swipe: 0.6 to 2.7 s over 8 tries, median
  about 1.7 s. In the 0.6 s case, noise had already started a streak while the gaze was
  on the card.
- Holding the gaze on a square: swipes 2.8 to 3.2 s apart. The minimum in theory is 744
  samples, or 2.98 s.
- Reading the card still swipes on its own, about 6 to 8 times a minute, Pass and Like
  mixed: 3 in a 30 s real-time run (121 windows), 8 to 16 per 120 s over 8 seeds in
  virtual time, and 26 in 8 x 30 s (6.5 a minute) in the test that guards this rate
  (it fails above 8 a minute; without the rest threshold the rate is 12.5). This is the real decoder (margin 0.5, threshold 0.5, 4 votes) on
  Gaussian noise with no tone, and the mock leaves it unchanged. When talking over a
  card, keep the gaze on Auto or on a square, or say that these are the decoder
  reacting to noise.

The mock is not the live demo. It has no EEG and no stimulus, and it says nothing about
the open questions under **Untested** above. The live demo is `brainsync.swipe` with the
board and the full-screen stimulus.

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

Stimulus configs: `two_targets` (the demo pair), `swipe` (the same pair labelled Pass and
Like, with a `[card]` table for BrainSwipe) and `one_target_12`, `one_target_15`,
`one_target_17` (single-target sessions for phase 0). Space starts a session, Escape
quits.

## Client/server (v3) and BrainPoint

The headset and the decoder can run as a server, so the screen the user looks at no longer
has to be on the machine that reads the board. The server owns the EEG board and the
decoder and never draws anything. A client draws the flickering targets and gets back what
the user picked; it never sees EEG. `PROTOCOL-v3.md` is the wire contract. The server is
meant for an NVIDIA Jetson Orin Nano next to the headset (see `deploy/jetson/`), and runs
on the Mac with `--board fake` while that is being set up.

> **Photosensitivity rule.** Every client shows the photosensitivity warning and draws no
> flicker until the user accepts it, and it sends `accepted: true` in its hello only after
> that. The server refuses a controller without it. Esc stops the flicker at once, and the
> flicker stops whenever the socket closes.

There are two kinds of client:

- **slots mode**: the client names its targets (`["yes", "no"]`), draws them where it
  likes, and gets a `selection` each time the decoder commits one. The reference page
  (`clients/web/`, served by the server) and the Python client
  (`python -m brainsync.server.client`) use it.
- **point mode (BrainPoint)**: the Chrome extension in `clients/chrome/` sends screenshots
  of the tab. The server asks Claude Haiku (`claude-haiku-5-5`) for the clickable elements
  and shows the best three as flickering patches plus "More" (strategy B, top-K plus
  more). Picking a patch clicks it; risky buttons (pay, delete, send) need a second pick of
  "Do it" first.

### Run the server without a headset

```bash
.venv/bin/python -m brainsync.server --board fake --no-auth
```

It prints `BrainSync server on ws://127.0.0.1:8765/ws (fake board, point mode off, freqs
15,17,12,20)` and the page address, `open http://127.0.0.1:8765/`. Open it, read the
warning, accept, and wait about two seconds while the page measures the display refresh
(set the display to 60 Hz with ProMotion off; `--ignore-refresh` skips the check). The
server calibrates for `--calibrate-s` seconds (default 10: look at the screen and relax),
then the two squares flicker. With `--board fake`, hovering a square makes the fake board
carry its tone, and a selection follows about 2.5 s later and repeats every 2.7 s or so
while the mouse stays.

Without `--no-auth` the server makes a token on the first run, in
`~/.config/brainsync/token` (mode 0600, `--token-file` to move it), and prints the page
address with it: `open http://127.0.0.1:8765/#token=...`. The token travels in the address
fragment, which the browser never sends to a server. `--no-auth` works only with a loopback
`--host`. `GET /health` answers `{"ok": true, "board": "streaming", "controller": false,
"viewers": 0}`.

```bash
.venv/bin/python -m brainsync.server --board fake --env-file .env   # point mode on
.venv/bin/python -m brainsync.server --board cerelog                # the Cerelog X8
.venv/bin/python -m brainsync.server.client --url ws://127.0.0.1:8765/ws --token-file \
    ~/.config/brainsync/token --slots yes,no --gaze yes --count 3     # headless client
```

Point mode needs `ANTHROPIC_API_KEY` on the server. `--env-file PATH` reads KEY=VALUE lines
without overriding the environment, so the key can live in a gitignored file; it is never
printed or logged. Without a key, or with `--no-detect`, the server still runs: slots mode
works and a point-mode client is told `detect_unavailable`. `--min-p-button` (default 0.5)
sets which elements are shown. `--log PATH` appends a JSON-lines record of the session
(phases, layouts, onsets, selections, scans, clicks; never the token or a screenshot).
Screenshots are kept only with `--log-scans DIR`, in the format `tools/screen_eval.py`
reads. Ctrl-C or SIGTERM tells every client `phase stop` and exits 0; a port in use or a
missing board exits 1, a bad argument 2.

### Load the BrainPoint extension

1. Start the server with point mode on (above).
2. Open `chrome://extensions`, turn on Developer mode, click Load unpacked and pick
   `clients/chrome/` (Chrome 116 or newer).
3. In the extension's Options, enter the server address (`ws://127.0.0.1:8765/ws`) and the
   token the server printed (empty with `--no-auth`).
4. Open the page to control, click the BrainPoint toolbar icon (or Alt+Shift+B), accept the
   warning. After calibration the server asks for a screenshot, Haiku finds the buttons, and
   the patches appear. With `--board fake`, hovering a patch picks it.

`clients/chrome/README.md` explains each file and permission. Password fields are blacked
out in the screenshot and their values never leave the page.

### Over the network: cloudflared

The server speaks plain `ws://` on `127.0.0.1`. Anything beyond this machine goes through a
Cloudflare quick tunnel: `cloudflared` connects outward and gives a random
`https://<name>.trycloudflare.com` address with HTTPS (and so `wss://`). Quick tunnels need
no Cloudflare account, and they work on Wi-Fi with client isolation. On the Jetson,
`brainsync-tunnel.service` does all of this at boot and writes the link to
`~/brainsync-link.txt` (`deploy/jetson/cloudflare/README.md`). On any other machine:

```bash
cloudflared tunnel --url http://127.0.0.1:8765          # prints https://<name>.trycloudflare.com
.venv/bin/python -m brainsync.server --board cerelog \
    --origin 'https://<name>\.trycloudflare\.com'      # still on 127.0.0.1:8765
```

The page is then at `https://<name>.trycloudflare.com/#token=<token>`, with the token the
server printed (the part after `#token=` in its address line; it is also in
`~/.config/brainsync/token`), and the extension's server address is
`wss://<name>.trycloudflare.com/ws`. `--origin` lets the page's own address through: without
it the page gets HTTP 403. Every `cloudflared` start gives a new address, so restart the
server with the new `--origin`. The address is public and the token is its only gate: never
run the server with `--no-auth` behind a tunnel, and do not post the link.

### On the Jetson

`sudo deploy/jetson/install.sh` sets the Jetson up in one command: Python 3.12, the venv,
the BrainFlow wheel and `brainsync-tunnel.service`, which runs the server behind a Cloudflare
quick tunnel from boot on (`deploy/jetson/README.md`). `--board cerelog` needs the Cerelog fork
of BrainFlow built for Linux aarch64: `deploy/jetson/brainflow/` builds the wheel (Docker on
the Mac, or natively on the Jetson) and `deploy/jetson/brainflow/INSTALL.md` installs and
checks it. The wheel was verified in an arm64 container, not yet on a real Jetson with the
headset; without it the server runs there only with `--board fake`. Only the server process
reads the board's serial port.

## Live brain map

A live scalp map of your own EEG in the browser. Every 0.25 s the last 2 s from the 8
electrodes become one frame: band power per electrode (delta, theta, alpha, beta) as a z-score
against your own rest baseline, 15/17 Hz SSVEP SNR and contact quality. A stdlib HTTP server
streams the frames as Server-Sent Events to `extension/brainmap/`, which interpolates them over
a drawing of the head. It needs only numpy, scipy and the standard library, so the same code
runs on the Jetson.

```bash
.venv/bin/python -m brainsync.brainmap --open                     # fake board, http://127.0.0.1:8140/
.venv/bin/python -m brainsync.brainmap --board cerelog --open     # the headset (port auto-detected)
.venv/bin/python -m brainsync.brainmap --board replay --recording run_cued.npz --speed 4
.venv/bin/python -m brainsync.brainmap --host 0.0.0.0             # headless Jetson, viewed over the LAN
```

Sit still with the eyes open for the first `--baseline-s` seconds (30 by default). Until then
the band views show dB relative to the mean across electrodes, not z-scores. Keys 1-6 pick
delta, theta, alpha, beta, SSVEP 15 and SSVEP 17 Hz. An electrode that is flat, noisy, clipped
or offset is greyed out and left out of the interpolation. Only one process can hold the board,
so close any other BrainSync tool first.

Measured on the M4 on 2026-10-24. The fake board (`--seconds 60`, default 30 s baseline) gave
232 frames at 4.00 Hz with no gaps, and the baseline was ready 32 s in (30 s of windows after
the first 2 s window). The fake board stays quiet through the baseline, then puts 10, 15 and
17 Hz tones on P7/P8/O1/O2 for 10 s each. In the 10 Hz phase the median posterior alpha z was
+3.4 to +4.3 per channel (never below +2.2) while the front four channels stayed near 0
(-0.3). The SSVEP SNR on those channels, averaged over the last 4 s, was +14.6 dB at 15 Hz
against -0.3 dB at 17 Hz in the 15 Hz phase, and +14.7 dB at 17 Hz against -0.9 dB at 15 Hz
in the 17 Hz phase. A synthetic 40 s recording (20 s noise, then 20 s of 10 Hz alpha on
O1/O2) replayed at `--speed 4 --baseline-s 15` gave all 153 frames. During the alpha phase O1
and O2 alpha z averaged +5.8 and +6.1, while the other six channels averaged -0.2. The run
ended with the "ended" status reaching the client, and exit 0. One frame costs about 0.44 ms
and 730 bytes of JSON.

The rest baseline leaves out blinks and movements (over 150 µV peak to peak after filtering,
plus the second after), and it uses the median and the median absolute deviation, so a few
artifacts barely move it. An electrode that was off or bad during the rest needs a full
`--baseline-s` of clean signal of its own before it gets z-scores; until then the page says
"no baseline yet" for it.

It is a scalp map from 8 electrodes, not a brain scan, and the page says so. The colours
between the dots are interpolated, not measured. They show which part of the head carries
which rhythm, not where in the cortex it comes from, which would need dozens of electrodes and
a head model. Blinks dominate Fp1/Fp2, and jaw or neck muscle shows up as beta on P7/P8. Noise
alone moves a z-score too: in the two runs above, channels carrying only noise were beyond
±1.7 to ±2.5 z (by band) in 5% of frames, and single frames reached ±5.6, mostly on the blue
side, so read one red or blue patch as a hint until it holds for a few seconds. Still to check on the headset: the table of actions in
[extension/brainmap/README.md](extension/brainmap/README.md) (eyes closed, blinks, jaw, the
15 Hz square), and decoder accuracy with the map on screen versus off.

## Predicted-brain heatmap (optional, Reels demo)

This is the offline, simulated alternative to the [live brain map](#live-brain-map): it shows
a model's prediction for an average brain, not your EEG.

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

## VEP paradigms: time, frequency and code modulation (Bin et al. 2009)

Everything above is an f-VEP (SSVEP) system: each target flickers at its own frequency.
Bin, Gao, Wang, Hong and Gao, "VEP-based brain-computer interfaces: time, frequency, and
code modulations" (IEEE Computational Intelligence Magazine, 2009) sort VEP BCIs into three
codings, like TDMA, FDMA and CDMA in radio, and `python -m brainsync.vep` runs a demo of
each on the same board and PsychoPy display:

| Paradigm | Stimulus | Decoder | Config | Paper |
|---|---|---|---|---|
| t-VEP | Brief flashes that never overlap; targets flash in rounds of one slot each, so each flashes once a second | Average the 0.5 s after each target's own flashes; the largest peak-to-valley response wins (`vep.tvep`) | `tvep_4` | under 30 bits/min; no online run |
| f-VEP | Each target at its own frequency; the paper's six: 15, 12, 10, 8.6, 7.5, 6 Hz (4 to 10 frames per cycle) | FFT, SNR of the peak against its neighbouring bins, argmax (`vep.fvep.SNR`, the paper's rule; `--classifier cca` / `fbcca` also work) | `fvep_6` | 85 %, 39.7 bits/min |
| c-VEP | One 63-bit m-sequence; every target shows it delayed by k lags (the paper: 16 targets, 4-frame lags) | One template trained while looking at square 0, shifted per target, Pearson correlation, argmax (`vep.cvep`) | `cvep_4` (16-frame lags), `cvep_16` (the paper's grid) | 91 %, 92.8 bits/min |

```bash
.venv/bin/python -m brainsync.stimulus -c cvep_4 --plan          # the sequences, no display
.venv/bin/python -m brainsync.vep --compare --fake               # all three on the synthetic board
.venv/bin/python -m brainsync.vep --paradigm code --fake --fast --no-stimulus   # instant dry run
.venv/bin/python -m brainsync.vep --paradigm code                # Cerelog X8 + display: train, then decode
.venv/bin/python -m brainsync.vep --paradigm time
.venv/bin/python -m brainsync.vep --paradigm freq                # brainsync.demo with the SNR rule
```

How it fits the code base. A stimulus config may carry a `[code]` table (m-sequence length,
lag in frames, frames per bit) or a `[time]` table (flash and gap frames, round order)
instead of frequencies; `brainsync.stimulus.coded` turns either into per-frame luminances
and the display draws them with the same `play` loop, stamping a `cycle:<n>` marker on
every m-sequence cycle's first frame and a `flash:<k>` marker on every flash onset,
because both decoders are onset-locked. `vep.sync.SyncDecoder` buffers samples and
markers, filters the stretch around each epoch (1 to 30 Hz, the 60 Hz notch) and decides
at cycle and flash boundaries with the same `Decision` and votes as the sliding f-VEP
decoder; a marker whose epoch is not in the buffer gives no decision. The c-VEP cycle is
63 frames, 1.05 s at 60 Hz, 262 samples at 250 Hz; each cycle is cut from its own marker,
so the half sample per cycle never accumulates, and the lag of 16 frames is 66.7 samples.

The live run starts the display in free-run mode with Unix markers and reads them from
its log. c-VEP trains first: `--train-cycles` cycles (default 30, about 32 s) with the cue
box on square 0, the leave-one-cycle-out accuracy printed (the paper's "training
accuracy", 95 % for c-VEP against 88 % for f-VEP), the template saved as
`<stamp>_cvep_template.npz` in the recordings folder and reusable with `--template`. Then
every committed change moves the cue box and speaks the label. `--fake` swaps the board
for `vep.fake.SequenceFakeBoard`: a made-up flash VEP (`synthetic.vep_kernel`, a damped
sine after a 60 ms latency) convolved with the fixated target's sequence, on the posterior
channels over Gaussian noise, with the markers coming from the fake board itself. It
checks the pipeline, not anyone's visual cortex. `--compare --fake` runs cued trials of
each paradigm and prints accuracy, abstain rate, seconds per selection and Wolpaw's ITR
beside the paper's figures; `--amp` and `--noise` set how easy the data are.

What to expect on the headset, and why the defaults differ from the paper:

- The paper had 47 channels, Oz with a tuned bipolar reference, and a CRT; this is four
  dry posterior electrodes. c-VEP leans on timing, so `cvep_4` spreads four targets 16
  frames (267 ms) apart, the widest four lags allow in a 63-frame cycle, where a one-frame
  slip costs nothing; the 16-target grid with 4-frame lags (67 ms) is `cvep_16` for the
  brave. The paper's wrap-around border is not drawn.
- Marker-to-sample alignment has not been measured on the demo Mac (PLAN.md V5). A constant
  offset is absorbed by training; jitter between cycles is not. Check the cycle markers'
  spacing in the stimulus log (1.0500 s apart) before trusting a live c-VEP run.
- t-VEP flashes under 4 Hz per target by construction (`config_warnings` says so if a
  config does not), so a decision needs at least `--min-flashes` rounds: slow, as in the
  paper.
- `fvep_6` is on/off flicker as on the paper's CRT, so its 5- and 7-frame cycles have an
  uneven duty cycle (warned), and 10 Hz sits on the user's alpha (PLAN.md section 14).
- Safety: an m-sequence is broadband and includes 15 to 25 Hz, and t-VEP is bright brief
  flashes. The photosensitivity warning and Escape apply unchanged.

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
| `brainsync/scroll.py` | `ScrollGate`: decoder commits to scrolls, cooldown and vote reset |
| `brainsync/bridge.py` | Localhost WebSocket server for the browser overlay, and its protocol |
| `brainsync/reels.py` | Phase 4 Instagram Reels run (also `--manual` and `--no-browser`) |
| `extension/` | BrainScroll Chrome extension: overlay, flicker, scroller, mock Reels page |
| `brainsync/demo.py` | Phase 3 live demo; its session loop is reused by BrainSwipe |
| `brainsync/profiles.py` | Invented dating profiles: load and check the deck TOML, draw the PNG cards |
| `brainsync/swipe_deck/profiles.toml` | The bundled deck of 20 invented profiles |
| `brainsync/swipe.py` | BrainSwipe: fresh-window swipe voting, the endless feed, CLI |
| `brainsync/swipe_web.py` | BrainSwipe browser mock: localhost server with Server-Sent Events, the real swipe logic on a real-time fake board |
| `brainsync/web/` | The mock's page: HTML, CSS, `app.js` (DOM, network, speech) and `logic.js` (pure helpers, tested with node) |
| `PROTOCOL-v3.md` | The client/server wire contract (v3) |
| `brainsync/server/protocol.py` | v3 messages: constants, parsing and checking client messages, building server messages |
| `brainsync/server/transport.py` | WebSocket + HTTP on one port: token, Origin, hello-first, controller and viewers |
| `brainsync/server/decoding.py` | `DecoderBank`: one decoder per frequency set over a shared history, onset gating |
| `brainsync/server/session.py` | The server's main loop: phases, calibration, selections, slots mode |
| `brainsync/server/app.py` | `python -m brainsync.server`: CLI, token file, board, logs, signals |
| `brainsync/server/client.py` | Synchronous Python client (`python -m brainsync.server.client`) |
| `brainsync/screen/detect.py` | Clickable elements of a screenshot with Claude Haiku (structured output) |
| `brainsync/screen/selector.py` | BrainPoint pages: top-K candidates plus "more", non-overlapping patches |
| `brainsync/screen/controller.py` | `PointMode`: scan, detect, pick, confirm, click, scan again |
| `brainsync/envfile.py` | KEY=VALUE env file loader that never overrides or prints |
| `clients/js/` | Browser SDK: `Connection`, `FlickerState`, refresh meter, and `flicker.js` |
| `clients/web/` | The reference page (slots mode), served by the server at `/` |
| `clients/chrome/` | BrainPoint, the point-mode Chrome extension |
| `deploy/jetson/` | One-command Jetson setup (`install.sh`), the Cloudflare tunnel service and the steps |
| `tools/screen_eval.py` | Scores Haiku's detection against DOM ground truth |
| `brainsync/heatmap/` | Optional TRIBE v2 predicted-brain panel frames for the Reels demo (own venv) |
| `brainsync/brainmap/` | Live scalp map: electrode layout, per-window band power, SNR and contact quality, frame stream and replay, stdlib SSE server, CLI |
| `extension/brainmap/` | The live map's page: `index.html` and `map.js` (interpolation, colour scales, message checks, DOM; tested with node) |
| `brainsync/vep/codes.py` | m-sequences (LFSR), lagged code matrices, non-overlapping flash schedules (Bin et al. 2009) |
| `brainsync/stimulus/coded.py` | c-VEP and t-VEP frames from `[code]` / `[time]` configs, `cycle:<n>` and `flash:<k>` markers |
| `brainsync/vep/fvep.py` | `SNR`: the paper's FFT peak-SNR f-VEP rule in the CCA interface |
| `brainsync/vep/cvep.py` | c-VEP `Template`: fit from cued cycles, shift per target, correlate, leave-one-out, save / load |
| `brainsync/vep/tvep.py` | `FlashAverager`: flash-locked averages, peak-to-valley amplitude, abstain until enough flashes |
| `brainsync/vep/sync.py` | `cut_epochs` at marker times; `SyncDecoder`, the live c-VEP / t-VEP decoder over samples and markers |
| `brainsync/vep/fake.py` | `SequenceFakeBoard`: synthetic board that follows a coded stimulus and emits its markers |
| `brainsync/vep/__main__.py` | `python -m brainsync.vep`: live t-VEP / f-VEP / c-VEP demos, c-VEP training, `--compare` table |

## Tests

```bash
env -u BRAINSYNC_DATA_DIR .venv/bin/python -m pytest -q
BRAINSYNC_DATA_DIR=/Users/school/Documents/ssvep/data .venv/bin/python -m pytest -q
node --test extension/test/*.test.mjs
```

The suite collects 997 tests. Without data the 9 real-data tests skip themselves, and the
BrainFlow board test skips where the Cerelog fork is not installed. No test opens a window
or touches the board. Phase 4 adds 117 of them: `test_scroll.py` 22, `test_bridge.py` 23
(a real server on a free port), `test_reels.py` 49 (fake board, a WebSocket client in place of the browser,
and SIGTERM/SIGHUP sent to a real `--manual` process), `test_config_reels.py` 11 and
`test_extension.py` 12. Without the `reels` extra the 14 browser tests in
`test_reels.py` and all of `test_bridge.py` skip. `test_extension.py`
checks the flicker fixture against `brainsync.stimulus.flicker`, the manifest and the
mock page, runs `node --check` on every script and runs the 84 node tests (the flicker
port within 1e-9 of the Python luminance, the overlay's geometry, refresh meter and
protocol state machine, the scroller, and the service worker against a fake `chrome`
and WebSocket). Node 24 reads `--test` arguments as globs, so
pass the files, not the folder. The node tests are skipped when node is not installed.

BrainSwipe adds 220: `test_swipe.py` 53, `test_swipe_cli.py` 29, `test_swipe_web.py` 37,
`test_profiles.py` 30, `test_stimulus_cards.py` 57 and `test_demo_session.py` 14. They
run the whole swipe loop on the fake board. They check that stale windows never vote, that the
voice line names the profile on screen, and that a card or flash command never changes a
square's luminance.

The VEP paradigms add 67: `test_vep_codes.py` 33 (every listed m-sequence has full period
and the N / -1 autocorrelation, lagged codes are mutually orthogonal, flashes never
overlap, the `[code]` / `[time]` tables validate), `test_vep_frames.py` 11 (frame
luminances equal the code matrix, cycle and flash markers land on the flip times, the CLI
plans and refuses flicker-only flags), `test_vep_fake.py` 6 (the kernel's shape, a stream
free of chunking artifacts, the response tells the fixated target apart),
`test_vep_decoders.py` 9 (SNR, Template, FlashAverager and SyncDecoder each above 90 %
on synthetic data, abstention without enough flashes or without a marker) and
`test_vep_cli.py` 8 (fake live runs of all three paradigms, the comparison table, the
argument checks).

The client/server split and BrainPoint add 929 Python tests and 153 node tests
(`node --test 'clients/js/test/*.test.mjs' 'clients/chrome/test/*.test.mjs'`):
`test_server_protocol.py` 270, `test_server_transport.py` 118 (a real server on port 0),
`test_server_decoding.py` 38, `test_server_session.py` 60, `test_server_client.py` 90,
`test_screen_detect.py` 70, `test_screen_selector.py` 95, `test_screen_controller.py` 58,
`test_screen_eval.py` 74, `test_clients_js.py` 7, `test_brainpoint_extension.py` 18 and
`test_server_e2e.py` 31. The end-to-end file runs `brainsync.server` on a real socket with a
real-time fake board (about 30 s): slots mode from the Python client, point mode through
scan, top-K pages, click, a risky button's confirm and the next scan, auth (4001, 4002,
takeover 4000), HTTP, the CLI's exit codes and SIGTERM, and the real JavaScript SDK, reference
page and extension agent against the server through Node's WebSocket
(`tests/server_e2e_client.mjs`). Point mode uses the real Detector with a stub `create`, so
no test calls the Anthropic API.

The filter tests check passband gain within 2% at 12, 15, 17 and 30 Hz, stopband gain
under 0.05 at 1, 2, 90 and 110 Hz, zero lag, and a 60 Hz notch deeper than -40 dB. The
real-data tests hold the Nakanishi and cued-session numbers above to minimum thresholds,
including the decoder's abstention on the eyes-closed run.
