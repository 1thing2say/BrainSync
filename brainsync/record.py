"""Record a cued SSVEP session from the Cerelog X8 while the stimulus runs.

    python -m brainsync.record                                   # two_targets, 20 cued trials
    python -m brainsync.record -c one_target_15 --trials 20      # single-target trials
    python -m brainsync.record --fake                            # synthetic, no board, no display

Each trial is a cue (--cue-s), flicker (--stim-s) and a still screen (--rest-s). The
stimulus prints unix-stamped stim_on/stim_off markers; they are saved with the raw µV data
as <YYYY-mm-dd_HHMMSS>_trials.npz (one target) or _cued.npz (several), the files
brainsync.recordings loads. Flicker windows start --latency s after stim_on and last
stim-s - latency s; still windows start the same delay after stim_off, so cue + rest must
cover that length. Ctrl-C during the session keeps and saves what was recorded; Ctrl-C
before the stimulus starts (connecting, warm-up) ends the program without starting it.
--fake sessions go to a temporary folder unless --save-dir is given, so they never mix
with real recordings.
"""
from __future__ import annotations

import argparse
import math
import shlex
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from .board import FakeBoard, record
from .launch import launch_stimulus, speak
from .paths import recordings_dir, unwritable_reason
from .recordings import MONTAGE, cut_windows, load_recording, save_recording
from .stimulus import load_config, make_trials
from .stimulus.display import format_marker

TAIL_S = 10.0  # extra seconds the recording may run past the planned session
END_MARGIN_S = 0.5  # recorded past the end of the last still window


def trailing_s(stim_s: float, rest_s: float, margin: float = END_MARGIN_S) -> float:
    """Seconds to keep recording after the last trial's rest so its still window is whole.

    The still window ends ``stim_s`` after stim_off (latency plus window), the rest only
    covers ``rest_s`` of that.
    """
    return max(0.0, stim_s - rest_s) + margin


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.record", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="two_targets",
                   help="stimulus config name or .toml path (default two_targets)")
    p.add_argument("--trials", type=int, default=20,
                   help="trials, rounded up to whole blocks of every target (default 20)")
    p.add_argument("--cue-s", type=float, default=1.0, help="cue seconds (default 1)")
    p.add_argument("--stim-s", type=float, default=4.0, help="flicker seconds (default 4)")
    p.add_argument("--rest-s", type=float, default=3.0, help="still seconds (default 3)")
    p.add_argument("--latency", type=float, default=0.14,
                   help="visual response delay in s; windows start this long after a marker")
    p.add_argument("--warmup", type=float, default=5.0,
                   help="seconds recorded and discarded before the stimulus starts (default 5)")
    p.add_argument("--names", default=",".join(MONTAGE),
                   help="comma-separated names of the 8 board channels, in order")
    p.add_argument("--save-dir", type=Path, default=None,
                   help="output directory (default: recordings_dir(); with --fake, "
                        "fake_dir())")
    p.add_argument("--hold-cue", action=argparse.BooleanOptionalAction, default=None,
                   help="keep the cue box up through the flicker (default: as the config says)")
    p.add_argument("--depth", type=float, default=None,
                   help="flicker modulation depth in (0, 1] (default: as the config says)")
    p.add_argument("--seed", type=int, help="trial order seed (default: random)")
    p.add_argument("--port", default=None, help="serial port (default: auto-detect)")
    p.add_argument("--no-voice", action="store_true", help="no spoken prompts")
    p.add_argument("--verbose", action="store_true", help="show BrainFlow's log messages")
    p.add_argument("--fake", action="store_true",
                   help="FakeBoard and synthetic markers: no board, no stimulus, instant")
    p.add_argument("--ignore-refresh", action="store_true",
                   help="let the stimulus run when its measured refresh is not 60 Hz "
                        "(it refuses by default)")
    return p


def fake_dir() -> Path:
    """Default folder for --fake sessions: a temporary one, never the real recordings."""
    return Path(tempfile.gettempdir()) / "brainsync_fake"


def _check_args(args: argparse.Namespace) -> list[str]:
    """Problems with the timing arguments, as messages."""
    bad = []
    if args.trials < 1:
        bad.append(f"--trials must be >= 1, got {args.trials}")
    if args.stim_s <= 0:
        bad.append(f"--stim-s must be positive, got {args.stim_s}")
    if args.cue_s < 0 or args.rest_s < 0:
        bad.append(f"--cue-s and --rest-s must be >= 0, got {args.cue_s}, {args.rest_s}")
    if args.latency < 0 or args.latency >= args.stim_s:
        bad.append(f"--latency must lie in [0, --stim-s), got {args.latency}")
    if args.warmup < 0:
        bad.append(f"--warmup must be >= 0, got {args.warmup}")
    if args.depth is not None and not 0.0 < args.depth <= 1.0:
        bad.append(f"--depth must be in (0, 1], got {args.depth}")
    window = args.stim_s - args.latency
    if args.cue_s + args.rest_s < window - 1e-9:
        bad.append(f"--cue-s + --rest-s = {args.cue_s + args.rest_s:g} s is shorter than the "
                   f"{window:g} s window (--stim-s - --latency), so the still window would "
                   "overlap the next flicker")
    return bad


def fake_session(board: FakeBoard, labels: list[int], cue_s: float, stim_s: float,
                 rest_s: float, lead_s: float = 1.0) -> tuple[np.ndarray, np.ndarray, str]:
    """Drive a virtual-time FakeBoard through a cued session; (X, t, marker log text).

    Like the live path, recording goes on past session_end until the last still window
    is complete.
    """
    lines = ["measured refresh: 60.00 Hz (fake)"]

    def mark(event: str) -> None:
        lines.append(format_marker(board.now, event))

    board.read()
    board.set_target(-1)
    board.advance(lead_s)
    mark("session_start")
    for k in labels:
        board.set_target(-1)
        mark(f"cue:{k}")
        board.advance(cue_s)
        mark(f"stim_on:{k}")
        board.set_target(k)
        board.advance(stim_s)
        mark(f"stim_off:{k}")
        board.set_target(-1)
        board.advance(rest_s)
    mark("session_end")
    lines.append(f"finished: {len(labels)} trials, 0 dropped frames (fake)")
    board.advance(max(lead_s, trailing_s(stim_s, rest_s)))
    X, t = board.read()
    return X, t, "\n".join(lines) + "\n"


def _stimulus_args(args: argparse.Namespace, blocks: int) -> list[str]:
    out = ["-c", str(args.config), "--blocks", str(blocks), "--cue-s", f"{args.cue_s:g}",
           "--stim-s", f"{args.stim_s:g}", "--rest-s", f"{args.rest_s:g}",
           "--markers", "unix", "--no-wait"]
    if getattr(args, "ignore_refresh", False):
        out.append("--ignore-refresh")
    if args.hold_cue is not None:
        out.append("--hold-cue" if args.hold_cue else "--no-hold-cue")
    if args.depth is not None:
        out += ["--depth", repr(args.depth)]
    if args.seed is not None:
        out += ["--seed", str(args.seed)]
    return out


def _record_live(args: argparse.Namespace, names: list[str], blocks: int,
                 session_s: float) -> tuple[np.ndarray, np.ndarray, float, str]:
    from .board import CerelogBoard

    voice = not args.no_voice
    board = CerelogBoard(serial_port=args.port, ch_names=names, verbose=args.verbose)
    print(f"connecting to the Cerelog X8 on {getattr(board, 'serial_port', '') or 'a default port'} "
          "(can take 15 s)...", file=sys.stderr)
    with board:
        if args.warmup > 0:
            print(f"warm-up: {args.warmup:g} s discarded", file=sys.stderr)
            record(board, args.warmup)
        board.read()
        print("starting the stimulus...", file=sys.stderr)
        launched = time.monotonic()
        stim = launch_stimulus(_stimulus_args(args, blocks))
        # samples buffered while the window came up count toward the cap, so add that time
        startup_s = time.monotonic() - launched
        speak(f"About {session_s / 60:.0f} minutes. Look at the square in the red box "
              "and keep still.", voice)

        def progress(elapsed: float, total: float) -> None:
            if int(elapsed) % 20 == 0:
                print(f"  {elapsed:4.0f} s of about {session_s:.0f} s", file=sys.stderr)

        # the last still window ends stim_s after the last stim_off, possibly after the
        # stimulus has exited, so keep recording a little past its exit
        after_exit = trailing_s(args.stim_s, args.rest_s)
        exited: list[float] = []

        def stop() -> bool:
            if stim.proc.poll() is None:
                return False
            if not exited:
                exited.append(time.monotonic())
            return time.monotonic() - exited[0] >= after_exit

        try:
            X, t = record(board, startup_s + session_s + TAIL_S + after_exit,
                          progress=progress, flush=False, stop=stop, keep_on_interrupt=True)
        finally:
            code = stim.finish(timeout=10.0)
        speak("Done.", voice)
        fs = board.fs
    if code != 0:
        print(f"warning: the stimulus exited with code {code}; see {stim.log}", file=sys.stderr)
    return X, t, fs, stim.text()


def score_command(path: Path, n_targets: int, python: str | None = None) -> str:
    """Shell command scoring ``path`` with the interpreter running this (the venv's)."""
    tool = "brainsync.score_cued" if n_targets > 1 else "brainsync.select_freqs"
    exe = python or sys.executable or "python"
    return f"{shlex.quote(exe)} -m {tool} {shlex.quote(str(path))}"


def fallback_dir() -> Path:
    """Where a recording goes when its save directory fails after the session."""
    return Path(tempfile.gettempdir()) / "brainsync_unsaved"


def reserve_path(directory: Path, start: float, kind: str, tries: int = 3600) -> Path:
    """Create and return an empty, unused ``<YYYY-mm-dd_HHMMSS>_<kind>.npz`` in ``directory``.

    The stamp is the local time ``start``; when that name is taken (two recordings in one
    second) the next free second is used, so names stay unique, sortable and in format.
    """
    for i in range(tries):
        stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(math.floor(start) + i))
        path = directory / f"{stamp}_{kind}.npz"
        try:
            with open(path, "x"):
                return path
        except FileExistsError:
            continue
    raise FileExistsError(f"no free {kind} file name in {directory} within {tries} s of "
                          f"{time.ctime(start)}")


def _save(directory: Path, start: float, kind: str, **kw: object) -> Path | None:
    """save_recording into ``directory``, or into fallback_dir() if that fails; None if both do."""
    for folder in (directory, fallback_dir()):
        target = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = reserve_path(folder, start, kind)
            save_recording(target, kind=kind, **kw)
            return target
        except OSError as exc:
            print(f"warning: could not save into {folder}: {exc}", file=sys.stderr)
            if target is not None:
                target.unlink(missing_ok=True)
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    problems = _check_args(args)
    names = [n.strip() for n in args.names.split(",") if n.strip()]
    if len(names) != 8:
        problems.append(f"--names needs 8 channel names, got {len(names)}: {names}")
    elif len(set(names)) != len(names):
        dup = sorted({n for n in names if names.count(n) > 1})
        problems.append(f"--names must not repeat a name, got {dup} more than once")
    for msg in problems:
        print(f"error: {msg}", file=sys.stderr)
    if problems:
        return 2
    try:
        cfg = load_config(args.config)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    n_targets = cfg.n_targets
    blocks = math.ceil(args.trials / n_targets)
    n_trials = blocks * n_targets
    session_s = n_trials * (args.cue_s + args.stim_s + args.rest_s)
    window_s = args.stim_s - args.latency
    freq_text = ", ".join(f"{f:g}" for f in cfg.freqs)
    print(f"{cfg.name}: {n_trials} trials ({blocks} block(s) x {n_targets} target(s) at "
          f"{freq_text} Hz), about {session_s:.0f} s", file=sys.stderr)

    if args.save_dir is not None:
        save_dir = args.save_dir
    else:
        save_dir = fake_dir() if args.fake else recordings_dir()
    reason = unwritable_reason(save_dir)
    if reason:
        print(f"error: cannot save into {save_dir} ({reason}); pass a writable --save-dir",
              file=sys.stderr)
        return 2

    start = time.time()
    if args.fake:
        board = FakeBoard(cfg.freqs, ch_names=names, seed=0 if args.seed is None else args.seed,
                          t0=float(math.floor(start)))
        order = make_trials(n_targets, blocks, cfg.session.shuffle, args.seed)
        if args.warmup > 0:
            board.advance(args.warmup)
        X, t, log_text = fake_session(board, [tr.target for tr in order], args.cue_s,
                                      args.stim_s, args.rest_s)
        fs = board.fs
    else:
        try:
            X, t, fs, log_text = _record_live(args, names, blocks, session_s)
        except (RuntimeError, ImportError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("interrupted before the session started; nothing recorded", file=sys.stderr)
            return 130
    if X.shape[1] == 0:
        print("error: no data arrived from the board; nothing saved", file=sys.stderr)
        return 1

    kind = "cued" if n_targets > 1 else "trials"
    path = _save(save_dir, start, kind, X=X, t=t, fs=fs, ch_names=names, freqs=cfg.freqs,
                 markers=log_text, window_s=window_s, latency=args.latency, phases=cfg.phases)
    if path is None:
        print("error: the recording could not be saved anywhere", file=sys.stderr)
        return 1
    print(f"saved {X.shape[1] / fs:.1f} s to {path}", file=sys.stderr)

    rec = load_recording(path)
    win = cut_windows(rec)
    counts = ", ".join(f"{int(np.sum(win.labels == k))} at {f:g} Hz"
                       for k, f in enumerate(rec.freqs))
    print(f"{len(win)} of {n_trials} trials complete ({counts})", file=sys.stderr)
    if len(win) < n_trials:
        print(f"warning: {n_trials - len(win)} trial(s) incomplete or missing markers",
              file=sys.stderr)
    print(path)
    print(f"score it with: {score_command(path, n_targets)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
