# brainsync on Windows (an extra platform)

**Windows is an addition, not a move.** This folder comes from the `feat/windows-setup`
branch. The demo Mac stays the main machine. The macOS / Linux setup in the main README,
the vendored macOS BrainFlow and `deploy/jetson` are unchanged, and every code path they
use still works as before. The Windows paths sit next to them (`sys.platform == "win32"`
branches, PowerShell scripts in this folder).

What runs here is the SSVEP side (`brainsync/`: board, decoder, PsychoPy stimulus,
`brainsync.demo` and friends). The voice / UI server (`app/`) already ran on Windows and
needs nothing from this folder.

## Why native Windows and not WSL

- Only Windows sees the board's USB serial port (`COM5` for the CH340 on an X8 V1).
  WSL could borrow it with `usbipd`, but every USB drop would also break that attach.
- A WSL window goes through WSLg's remote-desktop compositor and is not synced to the
  real screen, so a frame-locked 15 / 17 Hz flicker would jitter. The stimulus has to be
  a native window.

## Files

| file | what it is |
| --- | --- |
| `setup.ps1` | `.venv-win` with brainsync (editable), PsychoPy and the BrainFlow wheel, then checks without hardware |
| `check.ps1` | pre-flight: charger, power plan, USB selective suspend, the board's COM port, a measured 60 Hz |
| `psychopy-lean.txt` | the PsychoPy dependencies the stimulus needs (`setup.ps1 -FullPsychoPy` installs all of them) |
| `brainflow/build_brainflow.ps1` | the Windows counterpart of `deploy/jetson/brainflow/build_brainflow.sh`: clone, patch a copy, build with MSVC, wheel |
| `brainflow/msvc-compat.patch` | Windows-only patch so current MSVC compiles the fork (see the patch header) |

The BrainFlow build reuses the Jetson's `cerelog-serial-timeout.patch` and
`setuptools-pin.patch` and its hardware-free `check_brainflow.py`, so all three platforms
build the same source (Shared_brainflow-cerelog `932b3944`) with the same changes.

## Prerequisites (once)

1. **Python 3.12**: `py install 3.12`. PsychoPy 2026.2 supports Python 3.10-3.12 and
   brainsync needs 3.11+, so 3.13 / 3.14 do not work for the stimulus.
2. **Visual Studio 2022 or later** (Community is fine) with the workload
   "Desktop development with C++". The build uses its compiler and its bundled CMake and
   Ninja; nothing else needs installing.
3. **git** on PATH (Git for Windows).
4. The USB-serial driver for the board. Windows 10/11 installs the CH340 driver by
   itself; the board then shows up as "USB-SERIAL CH340 (COMn)" in Device Manager.

## Set up

From the repo root in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1
```

It creates `.venv-win` (gitignored, next to the macOS `.venv`), installs brainsync and
PsychoPy, builds BrainFlow the first time (`%LOCALAPPDATA%\BrainSync\brainflow`, a few
minutes) and installs the wheel, then runs three checks: `check_brainflow.py` (Cerelog
X8 id, 250 Hz, a synthetic session, a filter), the PsychoPy window backend, and the
board's COM port. Run it again after a `git pull`; it reuses the built wheel.

The first build fetches the fork (one commit, but a large pack: over 10 minutes on weak
Wi-Fi). An interrupted fetch is simply fetched again on the next run.

Options: `-Wheel <file>` installs a wheel built elsewhere, `-FullPsychoPy` installs
PsychoPy with all its dependencies, `-SkipBrainFlow` skips the board library.

## Before each session

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\check.ps1
```

It prints PASS / WARN / FAIL per line and ends with GO or NO-GO. It opens a black
full-screen window for about 6 s to measure frame timing.

What it looks for, and why:

- **Charger in.** On battery the board dropped off USB twice in a few minutes, and
  frames took up to 20 ms instead of 16.7 ms.
- **USB selective suspend off** on AC (the WARN line prints the `powercfg` command).
- **60 Hz, steady.** Settings > System > Display > Advanced display: choose 60 Hz and
  turn off "Dynamic refresh rate". The stimulus refuses to run more than 1 Hz off.
- **The board present** on a COM port. If it is missing: re-plug, use a cable that
  carries data, plug straight into the laptop, check the board's power light.

## Run

Same commands as on the Mac, with the Windows venv's Python:

```powershell
.venv-win\Scripts\python.exe -m brainsync.demo --fake --fast --no-stimulus --no-voice --seconds 30   # no hardware
.venv-win\Scripts\python.exe -m brainsync.demo                                                     # board + squares
```

The port is detected (pass `--port COM5` to force one). Spoken prompts play through
PowerShell's built-in WAV player when Cartesia is configured; otherwise they are printed.

## Tests on Windows

`.venv-win\Scripts\python.exe -m pytest` runs the same suite as on the Mac. Measured on
2026-10-24 (Python 3.12, lean PsychoPy):

- **The SSVEP path passes**: board, record, check, demo, online, stimulus, classify,
  score_cued, voice and the rest: no failures.
- **Other features have Windows-only test failures**, not regressions from this branch,
  and nothing on the demo path uses them: `test_brainmap_server` (its fixture creates a
  symlink, which needs Developer Mode or admin rights), `test_profiles`, `test_swipe_web`,
  `test_server_e2e`, `test_extension`, `test_brainpoint_extension`, `test_clients_js`,
  `test_heatmap`, `test_server_protocol` (2), `test_server` (1), `test_fake_decoder`
  (parent-process check) and `test_reels` (SIGHUP and signal tests;
  `test_silent_board_reports_no_data_on_the_overlay` ends the whole pytest run, so run
  `test_reels.py` on its own). Run the full suite on macOS or Linux as before.
