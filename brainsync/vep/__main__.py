"""Demos of the three VEP codings of Bin et al. (2009): t-VEP, f-VEP and c-VEP.

    python -m brainsync.vep --paradigm code                 # c-VEP: Cerelog X8 + PsychoPy (cvep_4)
    python -m brainsync.vep --paradigm time                 # t-VEP (tvep_4)
    python -m brainsync.vep --paradigm freq                 # f-VEP (fvep_6), the paper's SNR rule
    python -m brainsync.vep --paradigm code --fake --fast --no-stimulus   # synthetic, instant
    python -m brainsync.vep --compare --fake                # all three on the synthetic board

A live run connects the board, starts the stimulus in free-run mode (held still until the
first feedback line, Unix markers in its log, which this program tails) and decodes for
--seconds: one line per decision on stderr, a committed change moves the cue box onto the
decoded square and speaks its label, a summary at the end. The display carries each
square's label (signage) and, under the squares, a confidence line redrawn after every
decision: one bar per target from the decoder's scores, the lead, and the committed
choice (--no-scores for a bare display). c-VEP needs a template first:
--train-cycles cycles with the cue box on square 0 (look at it), cross-validated, saved as
<stamp>_cvep_template.npz and reusable with --template FILE. t-VEP decides once every
square has --min-flashes flashes in the last --flash-window seconds. f-VEP is brainsync.demo's
run with --classifier snr (the paper's FFT-SNR rule; cca and fbcca also work).

--fake replaces the board with vep.fake.SequenceFakeBoard (code / time) or board.FakeBoard
(freq): the fake user looks at a square that changes every --switch s, the markers come
from the fake board, and the display, if started, is only for show. --compare runs cued
trials of every paradigm on the synthetic board and prints accuracy, seconds per selection
and the information transfer rate beside the paper's figures; it checks the pipeline, not
anyone's visual cortex: --amp and --noise set how easy the fake data are.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..board import FS, CerelogBoard, FakeBoard
from ..classify import make
from ..demo import _names, _channels, _summary as demo_summary, Tally, send, stimulus_args
from ..demo import session as demo_session
from ..launch import Stimulus, launch_stimulus, speak
from ..metrics import ABSTAIN, accuracy, itr_bits, itr_bpm
from ..online import Decision, Decoder
from ..paths import recordings_dir
from ..record import reserve_path
from ..recordings import MONTAGE, POSTERIOR, Event, channel_indices, parse_events
from ..signal import preprocess
from ..stimulus import StimulusConfig, load_config
from ..stimulus.coded import sequence_for
from .cvep import Template, cross_validate, fit_template, lag_samples
from .fake import SequenceFakeBoard
from .fvep import SNR
from .sync import SyncDecoder, cut_epochs, first_commit
from .tvep import FlashAverager

PARADIGMS = ("time", "freq", "code")
DEFAULT_CONFIGS = {"code": "cvep_4", "time": "tvep_4", "freq": "fvep_6"}
BAND = (1.0, 30.0)
POLL_S = 0.1
STALL_S = 5.0
PAPER = {   # Bin et al. (2009), section 3.2 and 2.1: online accuracy and ITR
    "time": ("under 30 bits/min", "(section 2.1; no online run in the paper)"),
    "freq": ("85 %, 39.7 bits/min", "(6 targets, 12 subjects)"),
    "code": ("91 %, 92.8 bits/min", "(16 targets, 12 subjects)"),
}
PROMPTS = {
    "train": "Look at the {label} square and keep looking at it.",
    "still": "Keep still and look at the middle of the screen.",
    "go": "Look at one of the squares.",
}
BAR_CELLS = 12
BUILD_ERRORS = (ValueError, KeyError, FileNotFoundError, ImportError, OSError)


# -- arguments ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.vep", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--paradigm", choices=PARADIGMS, default="code",
                   help="which coding to run (default code)")
    p.add_argument("--compare", action="store_true",
                   help="cued trials of every paradigm on the synthetic board; a table")
    p.add_argument("-c", "--config", default=None,
                   help="stimulus config name or .toml path (default: the paradigm's: "
                        + ", ".join(f"{k} {v}" for k, v in DEFAULT_CONFIGS.items()) + ")")
    p.add_argument("--seconds", type=float, default=60.0,
                   help="decoding run length in seconds (default 60)")
    p.add_argument("--votes", type=int, default=2,
                   help="consecutive agreeing decisions needed to commit (default 2)")
    p.add_argument("--margin", type=float, default=0.0,
                   help="abstain unless the best target leads by this much (code: correlation; "
                        "time: fraction of the runner-up; freq: rest spreads; default 0)")
    code = p.add_argument_group("c-VEP")
    code.add_argument("--cycles", type=int, default=2,
                      help="cycles averaged before each decision (default 2)")
    code.add_argument("--train-cycles", type=int, default=30,
                      help="cycles recorded on square 0 for the template (default 30)")
    code.add_argument("--template", type=Path, default=None,
                      help="a saved template instead of training")
    code.add_argument("--save-template", type=Path, default=None,
                      help="where to save the trained template (default: the recordings "
                           "folder, or a temp folder with --fake)")
    tvep = p.add_argument_group("t-VEP")
    tvep.add_argument("--epoch", type=float, default=0.5,
                      help="seconds averaged after each flash (default 0.5)")
    tvep.add_argument("--min-flashes", type=int, default=3,
                      help="flashes of every square needed before a decision (default 3)")
    tvep.add_argument("--flash-window", type=float, default=6.0,
                      help="seconds of flashes kept (default 6)")
    p.add_argument("--pad", type=float, default=0.5,
                   help="seconds filtered around each epoch and dropped (code / time; default 0.5)")
    fvep = p.add_argument_group("f-VEP")
    fvep.add_argument("--classifier", default="snr", choices=("snr", "cca", "fbcca"),
                      help="default snr, the paper's rule")
    fvep.add_argument("--window", type=float, default=2.0, help="window seconds (default 2)")
    fvep.add_argument("--step", type=float, default=0.5,
                      help="seconds between decisions (default 0.5)")
    fvep.add_argument("--harmonics", type=int, default=1,
                      help="harmonics per frequency (default 1, the fundamental)")
    fvep.add_argument("--calibrate", type=float, default=15.0,
                      help="still-screen seconds for the rest baseline (default 15)")
    board = p.add_argument_group("board")
    board.add_argument("--names", type=_names, default=list(MONTAGE),
                       help="comma-separated names of the 8 board channels, in order")
    board.add_argument("--channels", type=_channels, default=list(POSTERIOR),
                       help="analysis channels (default " + ",".join(POSTERIOR) + ")")
    board.add_argument("--port", default=None, help="serial port (default: auto-detect)")
    board.add_argument("--verbose", action="store_true", help="show BrainFlow's log messages")
    fake = p.add_argument_group("synthetic board")
    fake.add_argument("--fake", action="store_true", help="a synthetic board, no headset")
    fake.add_argument("--fast", action="store_true",
                      help="with --fake: virtual time, as fast as the decoder runs")
    fake.add_argument("--switch", type=float, default=10.0,
                      help="with --fake: seconds before the fake user looks elsewhere (default 10)")
    fake.add_argument("--amp", type=float, default=3.0,
                      help="with --fake: response amplitude in uV (default 3)")
    fake.add_argument("--noise", type=float, default=8.0,
                      help="with --fake: noise in uV (default 8)")
    fake.add_argument("--seed", type=int, default=0, help="with --fake: noise seed")
    cmp_ = p.add_argument_group("--compare")
    cmp_.add_argument("--trials", type=int, default=6, help="test trials per target (default 6)")
    cmp_.add_argument("--train-trials", type=int, default=4,
                      help="c-VEP training trials per target (default 4)")
    cmp_.add_argument("--max-trial", type=float, default=10.0,
                      help="seconds a trial may take before it counts as abstained (default 10)")
    p.add_argument("--no-stimulus", action="store_true",
                   help="do not start the stimulus window (code / time: needs --fake)")
    p.add_argument("--ignore-refresh", action="store_true",
                   help="let the stimulus run when its measured refresh is not 60 Hz")
    p.add_argument("--no-voice", action="store_true", help="no spoken prompts")
    p.add_argument("--scores", action=argparse.BooleanOptionalAction, default=True,
                   help="label each square and show a confidence line (one bar per target, "
                        "the lead, the committed choice) under the squares after every "
                        "decision (default on)")
    return p


def check_arguments(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject inconsistent flags through ``p.error`` (exit code 2)."""
    if args.fast and not args.fake:
        p.error("--fast needs --fake")
    if args.compare and not args.fake:
        p.error("--compare runs on the synthetic board: add --fake")
    if args.no_stimulus and not args.fake and args.paradigm != "freq":
        p.error("--no-stimulus needs --fake for code / time: the markers come from the display")
    for flag, value in (("--seconds", args.seconds), ("--switch", args.switch),
                        ("--epoch", args.epoch), ("--flash-window", args.flash_window),
                        ("--window", args.window), ("--step", args.step),
                        ("--calibrate", args.calibrate), ("--max-trial", args.max_trial)):
        if not value > 0:
            p.error(f"{flag} must be positive, got {value}")
    for flag, value in (("--votes", args.votes), ("--cycles", args.cycles),
                        ("--train-cycles", args.train_cycles), ("--min-flashes", args.min_flashes),
                        ("--trials", args.trials), ("--train-trials", args.train_trials),
                        ("--harmonics", args.harmonics)):
        if value < 1:
            p.error(f"{flag} must be >= 1, got {value}")
    for flag, value in (("--margin", args.margin), ("--pad", args.pad), ("--amp", args.amp),
                        ("--noise", args.noise)):
        if value < 0:
            p.error(f"{flag} must be >= 0, got {value}")
    if len(args.names) != len(MONTAGE):
        p.error(f"--names needs {len(MONTAGE)} names, got {len(args.names)}: {args.names}")


def config_for(args: argparse.Namespace, paradigm: str | None = None) -> StimulusConfig:
    """The paradigm's config, checked to be of that paradigm."""
    paradigm = paradigm or args.paradigm
    cfg = load_config(args.config or DEFAULT_CONFIGS[paradigm])
    if cfg.paradigm != paradigm:
        raise ValueError(f"config {cfg.name!r} is a [{cfg.paradigm}] config, not {paradigm}; "
                         f"the default for {paradigm} is {DEFAULT_CONFIGS[paradigm]}")
    return cfg


# -- helpers -----------------------------------------------------------------------------

class LogTail:
    """New marker events from the stimulus's log file, each call."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._pos = 0
        self._rest = ""

    def events(self) -> list[Event]:
        with open(self.path, encoding="utf-8", errors="replace") as f:
            f.seek(self._pos)
            chunk = f.read()
            self._pos = f.tell()
        lines = (self._rest + chunk).split("\n")
        self._rest = lines.pop()
        return parse_events("\n".join(lines))


def template_path(args: argparse.Namespace) -> Path:
    if args.save_template is not None:
        return Path(args.save_template)
    folder = (Path(tempfile.gettempdir()) / "brainsync_fake" if args.fake else recordings_dir())
    folder.mkdir(parents=True, exist_ok=True)
    return reserve_path(folder, time.time(), "cvep_template")


def _name(labels: Sequence[str], k: int) -> str:
    return "-" if k == ABSTAIN or k is None else labels[k]


def _status(d: Decision, elapsed: float, labels: Sequence[str], truth: int | None) -> str:
    gap = "" if not np.isfinite(d.gap) else f"  lead {d.gap:.3f}"
    mark = " *" if d.changed else ""
    truth_s = "" if truth is None else f"  looking at {_name(labels, truth)}"
    return (f"{elapsed:6.1f} s  raw {_name(labels, d.raw):>3}  committed "
            f"{_name(labels, d.committed):>3}{mark}{gap}{truth_s}")


def _bar(value: float, cells: int = BAR_CELLS) -> str:
    """``cells`` characters filled in proportion to ``value`` clipped to 0..1."""
    v = 0.0 if not np.isfinite(value) else min(1.0, max(0.0, float(value)))
    n = int(round(v * cells))
    return "\u2588" * n + "\u2591" * (cells - n)


def confidence_text(d: Decision, labels: Sequence[str], scale: float = 1.0) -> str:
    """The status block under the squares: one line per target (label, bar, score), then
    the lead and the committed choice. Scores are divided by ``scale`` for the bars (c-VEP
    correlations are already 0..1; t-VEP amplitudes need their own scale)."""
    width = max(len(s) for s in labels)
    lines = []
    for k, label in enumerate(labels):
        s = float(d.scores[k]) if k < len(d.scores) else float("nan")
        best = " <" if k == d.raw else "  "
        shown = "   -  " if not np.isfinite(s) else f"{s:+.2f}"
        lines.append(f"{label:<{width}}  {_bar(s / scale)}  {shown}{best}")
    gap = "lead  -  " if not np.isfinite(d.gap) else f"lead {d.gap:.2f}"
    choice = ("no choice yet" if d.committed == ABSTAIN
              else f"choice: {labels[d.committed]}" + (" *" if d.changed else ""))
    lines.append(f"{gap}    {choice}")
    return "\n".join(lines)


def send_text(stim: Stimulus | None, text: str) -> bool:
    """Replace the display's status line (``{"text": ...}`` on its stdin); False when the
    pipe is gone."""
    return send(stim, json.dumps({"text": text}))


def _coded_summary(tally: Tally, labels: Sequence[str], ended: str, paradigm: str) -> list[str]:
    lines = [f"{paradigm}: {tally.windows} decisions in {tally.seconds:.1f} s, "
             f"{tally.abstained} abstained, {len(tally.commits)} commits ({ended})"]
    if tally.commits:
        lines.append("commits: " + " ".join(f"{t:.1f}s->{_name(labels, k)}"
                                              for t, k in tally.commits))
    if tally.truth_windows:
        lines.append(f"decisions agreeing with the fake user's gaze: {tally.truth_hits}/"
                     f"{tally.truth_windows}")
    return lines


# -- c-VEP training ---------------------------------------------------------------------------

def collect_cycles(board: Any, events: Callable[[], list[Event]], n_cycles: int, fs: float,
                   rows: Sequence[int], n_samples: int, sleep: Callable[[float], None],
                   clock: Callable[[], float], stop: Callable[[], str | None],
                   timeout_s: float,
                   progress: Callable[[int], None] | None = None) -> np.ndarray:
    """Record until ``n_cycles`` complete cycles have been marked; the cut, filtered cycles.
    ``progress(k)`` is called whenever the count of marked cycles changes."""
    Xs, ts, marks = [], [], []
    began = heard = clock()
    reported = -1
    while True:
        why = stop()
        if why:
            raise RuntimeError(why)
        sleep(POLL_S)
        X, t = board.read()
        now = clock()
        if X.shape[1]:
            Xs.append(X[list(rows)])
            ts.append(t)
            heard = now
        elif now - heard >= STALL_S:
            raise RuntimeError(f"the board sent nothing for {STALL_S:g} s")
        marks += [e for e in events() if e[1] == "cycle"]
        if progress is not None and len(marks) != reported:
            reported = len(marks)
            progress(min(reported, n_cycles))
        if ts and len(marks) > n_cycles:
            last_needed = marks[n_cycles - 1][0] + n_samples / fs + 1.0
            if ts[-1][-1] >= last_needed:
                break
        if now - began > timeout_s:
            raise RuntimeError(f"saw {len(marks)} cycle markers in {timeout_s:g} s, needed "
                               f"{n_cycles + 1}; is the display flickering?")
    X = preprocess(np.concatenate(Xs, axis=1), fs, BAND)
    t = np.concatenate(ts)
    cycles, _ = cut_epochs(X, t, [m[0] for m in marks[:n_cycles]], n_samples)
    if cycles.shape[0] < 2:
        raise RuntimeError(f"only {cycles.shape[0]} cycle(s) could be cut; need >= 2")
    return cycles


def train_template(cycles: np.ndarray, cfg: StimulusConfig, fs: float,
                   ch_names: Sequence[str]) -> tuple[Template, float]:
    """Fit on cycles recorded while looking at square 0; (template, leave-one-out accuracy)."""
    lag = lag_samples(cfg.code.lag_frames, fs, cfg.refresh)
    tpl = fit_template({0: cycles}, cfg.n_targets, lag, fs, base=0, ch_names=tuple(ch_names))
    y, pred = cross_validate({0: cycles}, cfg.n_targets, lag, fs)
    return tpl, accuracy(y, pred)


# -- the live run (code / time) ---------------------------------------------------------------

def build_coded(args: argparse.Namespace) -> tuple[StimulusConfig, Any, SyncDecoder | None]:
    """Config, board (not connected) and, unless a c-VEP template must be trained, the decoder."""
    cfg = config_for(args)
    seq = sequence_for(cfg, args.seed)
    if args.fake:
        board: Any = SequenceFakeBoard(seq, fs=FS, refresh=cfg.refresh, fixated=0, amp=args.amp,
                                       noise=args.noise, seed=args.seed,
                                       clock=None if args.fast else time.time,
                                       ch_names=args.names)
    else:
        board = CerelogBoard(serial_port=args.port, ch_names=args.names, verbose=args.verbose)
    decoder = None
    if cfg.paradigm == "time":
        avg = FlashAverager(cfg.n_targets, board.fs, epoch_s=args.epoch,
                            min_flashes=args.min_flashes, margin=args.margin)
        decoder = SyncDecoder(board.fs, averager=avg, channels=args.channels,
                              ch_names=args.names, band=BAND, votes=args.votes,
                              flash_window_s=args.flash_window, pad_s=args.pad)
    elif args.template is not None:
        tpl = Template.load(args.template)
        if tpl.n_targets != cfg.n_targets:
            raise ValueError(f"template has {tpl.n_targets} targets, config {cfg.name!r} "
                             f"{cfg.n_targets}")
        decoder = SyncDecoder(board.fs, template=tpl, channels=args.channels,
                              ch_names=args.names, band=BAND, votes=args.votes,
                              cycles=args.cycles, pad_s=args.pad, margin=args.margin)
    return cfg, board, decoder


def coded_stimulus_args(cfg: StimulusConfig, args: argparse.Namespace) -> list[str]:
    """The stimulus command line for a coded run: free run of the config, Unix markers in
    its log, feedback on stdin, held still until the first line (EOF on stdin ends it)."""
    out = ["-c", str(cfg.path or cfg.name), "--free", "--markers", "unix", "--feedback", "stdin",
           "--hold-still", "--no-wait"]
    if args.scores:
        out += ["--labels", "--status", "stdin"]
    if args.ignore_refresh:
        out.append("--ignore-refresh")
    return out


def run_coded(args: argparse.Namespace) -> int:
    try:
        cfg, board, decoder = build_coded(args)
    except BUILD_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    labels = cfg.labels
    voice = not args.no_voice
    fast = args.fake and args.fast
    sleep: Callable[[float], None] = board.advance if fast else time.sleep
    clock: Callable[[], float] = (lambda: board.elapsed) if fast else time.monotonic
    rows = channel_indices(tuple(args.names), args.channels)
    print(f"{cfg.paradigm}-VEP {cfg.name}: {cfg.n_targets} targets; "
          f"{'fake board' if args.fake else 'Cerelog X8'} at {board.fs:g} Hz; channels "
          + ",".join(args.names[i] for i in rows), file=sys.stderr)
    stim: Stimulus | None = None
    tally = Tally()
    ended, code = "not started", 0
    try:
        if not args.fake:
            print("connecting to the Cerelog X8 (can take 15 s)...", file=sys.stderr)
        with board:
            if not args.no_stimulus:
                print("starting the stimulus...", file=sys.stderr)
                stim = launch_stimulus(coded_stimulus_args(cfg, args), stdin=subprocess.PIPE)
            tail = LogTail(stim.log) if stim is not None else None
            events: Callable[[], list[Event]]
            if args.fake:
                events = board.read_events
            else:
                assert tail is not None
                events = tail.events

            def stimulus_gone() -> str | None:
                if stim is not None and stim.proc.poll() is not None:
                    return f"stimulus exited (code {stim.proc.returncode})"
                return None

            def start_flicker(cue: int) -> bool:
                if args.fake:
                    board.start_flicker()
                return send(stim, cue)

            show = send_text if args.scores else (lambda stim, text: True)
            speak(PROMPTS["still"], voice)
            board.read()
            if decoder is None:   # c-VEP without a template: train on square 0
                speak(PROMPTS["train"].format(label=labels[0]), voice)
                if args.fake:
                    board.set_target(0)
                if not start_flicker(0):
                    raise RuntimeError("stimulus pipe closed")
                n = int(round(cfg.code.cycle_frames / cfg.refresh * board.fs))
                cycle_s = cfg.code.cycle_frames / cfg.refresh
                print(f"training: {args.train_cycles} cycles of {cycle_s:.2f} s on square "
                      f"{labels[0]!r}...", file=sys.stderr)
                show(stim, f"training: keep looking at {labels[0]}")
                cycles = collect_cycles(board, events, args.train_cycles, board.fs, rows, n,
                                        sleep, clock, stimulus_gone,
                                        timeout_s=(args.train_cycles + 3) * cycle_s + 10,
                                        progress=lambda k: show(
                                            stim, f"training: keep looking at {labels[0]}"
                                                  f"   cycle {k}/{args.train_cycles}"))
                tpl, cv = train_template(cycles, cfg, board.fs, [args.names[i] for i in rows])
                path = tpl.save(template_path(args))
                print(f"template from {tpl.n_cycles} cycles, leave-one-out accuracy "
                      f"{100 * cv:.0f} % (chance {100 / cfg.n_targets:.0f} %), saved to {path}",
                      file=sys.stderr)
                show(stim, f"template ready: {100 * cv:.0f} % on held-out cycles "
                           f"(chance {100 / cfg.n_targets:.0f} %)")
                decoder = SyncDecoder(board.fs, template=tpl, channels=args.channels,
                                      ch_names=args.names, band=BAND, votes=args.votes,
                                      cycles=args.cycles, pad_s=args.pad, margin=args.margin)
                send(stim, ABSTAIN)
            elif not start_flicker(ABSTAIN):
                raise RuntimeError("stimulus pipe closed")
            speak(PROMPTS["go"], voice)
            show(stim, PROMPTS["go"])
            board.read()
            events()
            decoder.reset()
            ended = "started"
            began = heard = clock()
            n_switch = 0
            # c-VEP scores are correlations (0..1 bars); t-VEP amplitudes are scaled to the
            # first decision's best score, so the bars are relative
            score_scale = 1.0 if cfg.paradigm == "code" else float("nan")
            try:
                while True:
                    why = stimulus_gone()
                    if why:
                        ended = why
                        break
                    elapsed = clock() - began
                    if elapsed >= args.seconds:
                        ended = "time up"
                        break
                    if args.fake:
                        due = int(elapsed // args.switch)
                        if due != n_switch:
                            n_switch = due
                            board.set_target(due % cfg.n_targets)
                    sleep(POLL_S)
                    X, t = board.read()
                    now = clock()
                    if X.shape[1]:
                        heard = now
                    elif now - heard >= STALL_S:
                        ended = "board stalled"
                        code = 1
                        break
                    pipe_ok = True
                    for d in decoder.feed(X, t, events()):
                        truth = board.fixated if args.fake else None
                        at = clock() - began
                        tally.add(d, at, truth)
                        print(_status(d, at, labels, truth), file=sys.stderr, flush=True)
                        if not np.isfinite(score_scale):
                            top = float(np.nanmax(d.scores)) if len(d.scores) else float("nan")
                            score_scale = top if np.isfinite(top) and top > 0 else 1.0
                        if not show(stim, confidence_text(d, labels, score_scale)):
                            pipe_ok = False
                            break
                        if d.changed:
                            if d.committed != ABSTAIN:
                                speak(labels[d.committed], voice)
                            if not send(stim, d.committed):
                                pipe_ok = False
                                break
                    if not pipe_ok:
                        ended = "stimulus pipe closed"
                        break
            except KeyboardInterrupt:
                ended = "interrupted"
    except KeyboardInterrupt:
        print("interrupted before decoding started", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        ended, code = "error", 1
    finally:
        if stim is not None:
            try:
                if stim.proc.stdin is not None:
                    stim.proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            rc = stim.finish(timeout=5.0)
            if rc not in (0, None):
                print(f"error: the stimulus exited with code {rc}; see {stim.log}", file=sys.stderr)
                code = code or 1
    if ended != "not started":
        print("\n".join(_coded_summary(tally, labels, ended, cfg.paradigm)))
    return code


# -- f-VEP through brainsync.demo -------------------------------------------------------------

def build_freq(args: argparse.Namespace) -> tuple[StimulusConfig, Any, Decoder]:
    cfg = config_for(args, "freq")
    if args.fake:
        board: Any = FakeBoard(cfg.freqs, switch_s=args.switch, seed=args.seed,
                               amp=args.amp, noise=args.noise,
                               clock=None if args.fast else time.time, ch_names=args.names)
    else:
        board = CerelogBoard(serial_port=args.port, ch_names=args.names, verbose=args.verbose)
    if args.classifier == "snr":
        clf: Any = SNR(cfg.freqs, board.fs, harmonics=args.harmonics, phases=cfg.phases,
                       margin=args.margin)
    else:
        clf = make(args.classifier, freqs=cfg.freqs, fs=board.fs, harmonics=args.harmonics,
                   phases=cfg.phases, margin=args.margin)
    decoder = Decoder(clf, board.fs, window_s=args.window, step_s=args.step, votes=args.votes,
                      channels=args.channels, pad_s=1.0, ch_names=args.names)
    return cfg, board, decoder


def run_freq(args: argparse.Namespace) -> int:
    try:
        cfg, board, decoder = build_freq(args)
    except BUILD_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    args.config = str(cfg.path or cfg.name)   # demo.stimulus_args reads it
    labels = cfg.labels
    voice = not args.no_voice
    tally = Tally()
    stim: Stimulus | None = None

    def on_flicker(started: Stimulus | None) -> bool:
        nonlocal stim
        stim = started
        return True

    def on_decision(d: Decision, elapsed: float) -> bool:
        truth = board.active_target() if args.fake else None
        tally.add(d, elapsed, truth)
        print(_status(d, elapsed, labels, truth), file=sys.stderr, flush=True)
        if not d.changed:
            return True
        if d.committed != ABSTAIN:
            speak(labels[d.committed], voice)
        return send(stim, d.committed)

    ended, code = demo_session(args, cfg, board, decoder, stimulus_args=stimulus_args(args),
                               on_flicker=on_flicker, on_decision=on_decision)
    if ended != "not started":
        print("\n".join(demo_summary(tally, tally.seconds, labels, ended)))
    return code


# -- the comparison -------------------------------------------------------------------------

@dataclass
class Row:
    paradigm: str
    n_targets: int
    labels: tuple[str, ...]
    y: list[int] = field(default_factory=list)
    pred: list[int] = field(default_factory=list)
    delay: list[float] = field(default_factory=list)   # seconds to the first commit
    note: str = ""

    @property
    def n(self) -> int:
        return len(self.y)

    @property
    def acc(self) -> float:
        return accuracy(np.array(self.y), np.array(self.pred))

    @property
    def abstained(self) -> float:
        return float(np.mean(np.array(self.pred) == ABSTAIN))

    def mean_delay(self) -> float:
        d = [x for x, p in zip(self.delay, self.pred) if p != ABSTAIN]
        return float(np.mean(d)) if d else math.nan

    def selection_s(self, gaze_s: float, max_trial: float) -> float:
        """Seconds per selection: the decision delay (the whole trial when it abstained)
        plus the gaze shift between selections."""
        d = [x if p != ABSTAIN else max_trial for x, p in zip(self.delay, self.pred)]
        return float(np.mean(d)) + gaze_s


def _targets_order(n_targets: int, per_target: int, seed: int) -> list[int]:
    rng = np.random.default_rng(seed)
    out: list[int] = []
    for _ in range(per_target):
        out += rng.permutation(n_targets).tolist()
    return out


def _run_trial(board: Any, feed: Callable[[np.ndarray, np.ndarray], list[Decision]],
               max_s: float, step_s: float = 0.2) -> tuple[int, float]:
    """Advance the virtual board until the first commit or ``max_s``; (target, delay)."""
    t_on = board.now
    elapsed = 0.0
    while elapsed < max_s:
        board.advance(step_s)
        elapsed += step_s
        X, t = board.read()
        first = first_commit(feed(X, t))
        if first is not None:
            return first.committed, first.time - t_on
    return ABSTAIN, max_s


def compare_code(args: argparse.Namespace) -> Row:
    cfg = config_for(args, "code")
    board = SequenceFakeBoard(sequence_for(cfg, args.seed), fs=FS, refresh=cfg.refresh,
                              amp=args.amp, noise=args.noise, seed=args.seed, t0=0.0)
    row = Row("code", cfg.n_targets, cfg.labels)
    n = int(round(cfg.code.cycle_frames / cfg.refresh * FS))
    lag = lag_samples(cfg.code.lag_frames, FS, cfg.refresh)
    rows = channel_indices(tuple(MONTAGE), POSTERIOR)
    # training: cued trials of every target, each cycle shifted back to square 0's alignment
    cycles: dict[int, list[np.ndarray]] = {}
    for k in _targets_order(cfg.n_targets, args.train_trials, args.seed):
        board.set_target(k)
        board.start_flicker()
        t_on = board.now
        board.advance(cfg.session.stim_s)
        board.stop_flicker()
        board.advance(1.0)
        X, t = board.read()
        marks = [e[0] for e in board.read_events() if e[1] == "cycle"
                 and e[0] + n / FS <= t_on + cfg.session.stim_s]
        ep, _ = cut_epochs(preprocess(X[rows], FS, BAND), t, marks, n)
        cycles.setdefault(k, []).append(ep)
    train = {k: np.concatenate(v) for k, v in cycles.items()}
    tpl = fit_template(train, cfg.n_targets, lag, FS, base=0, ch_names=tuple(POSTERIOR))
    y, pred = cross_validate(train, cfg.n_targets, lag, FS)
    row.note = (f"template from {tpl.n_cycles} cycles ({args.train_trials} trials per target), "
                f"leave-one-out {100 * accuracy(y, pred):.0f} %; {args.cycles} cycles per "
                f"decision, {args.votes} votes")
    dec = SyncDecoder(FS, template=tpl, band=BAND, votes=args.votes, cycles=args.cycles,
                      pad_s=args.pad, margin=args.margin)
    for k in _targets_order(cfg.n_targets, args.trials, args.seed + 1):
        dec.reset()
        board.set_target(k)
        board.start_flicker()
        got, delay = _run_trial(board, lambda X, t: dec.feed(X, t, board.read_events()),
                                args.max_trial)
        board.stop_flicker()
        board.advance(1.0)
        board.read(), board.read_events()
        row.y.append(k), row.pred.append(got), row.delay.append(delay)
    return row


def compare_time(args: argparse.Namespace) -> Row:
    cfg = config_for(args, "time")
    board = SequenceFakeBoard(sequence_for(cfg, args.seed), fs=FS, refresh=cfg.refresh,
                              amp=args.amp, noise=args.noise, seed=args.seed, t0=0.0)
    row = Row("time", cfg.n_targets, cfg.labels)
    avg = FlashAverager(cfg.n_targets, FS, epoch_s=args.epoch, min_flashes=args.min_flashes,
                        margin=args.margin)
    dec = SyncDecoder(FS, averager=avg, band=BAND, votes=args.votes,
                      flash_window_s=args.flash_window, pad_s=args.pad)
    row.note = (f"no training; {args.epoch:g} s after each flash, decision once every square "
                f"has {args.min_flashes} flashes, {args.votes} votes")
    for k in _targets_order(cfg.n_targets, args.trials, args.seed + 1):
        dec.reset()
        board.set_target(k)
        board.start_flicker()
        got, delay = _run_trial(board, lambda X, t: dec.feed(X, t, board.read_events()),
                                args.max_trial)
        board.stop_flicker()
        board.advance(1.0)
        board.read(), board.read_events()
        row.y.append(k), row.pred.append(got), row.delay.append(delay)
    return row


def compare_freq(args: argparse.Namespace) -> Row:
    cfg = config_for(args, "freq")
    board = FakeBoard(cfg.freqs, fs=FS, switch_s=1e9, amp=args.amp, noise=args.noise,
                      seed=args.seed, t0=0.0)
    row = Row("freq", cfg.n_targets, cfg.labels)
    if args.classifier == "snr":
        clf: Any = SNR(cfg.freqs, FS, harmonics=args.harmonics, phases=cfg.phases,
                       margin=args.margin)
    else:
        clf = make(args.classifier, freqs=cfg.freqs, fs=FS, harmonics=args.harmonics,
                   phases=cfg.phases, margin=args.margin)
    dec = Decoder(clf, FS, window_s=args.window, step_s=args.step, votes=args.votes, pad_s=1.0)
    row.note = (f"no training; {clf.name} over {args.window:g} s windows every {args.step:g} s, "
                f"{args.votes} votes")
    for k in _targets_order(cfg.n_targets, args.trials, args.seed + 1):
        dec.reset()
        board.set_target(k)
        got, delay = _run_trial(board, dec.feed, args.max_trial)
        board.set_target(ABSTAIN)
        board.advance(1.0)
        board.read()
        row.y.append(k), row.pred.append(got), row.delay.append(delay)
    return row


def compare_table(rows: Sequence[Row], gaze_s: float, max_trial: float) -> list[str]:
    head = (f"{'paradigm':<8} {'targets':>7} {'trials':>6} {'accuracy':>8} {'abstain':>7} "
            f"{'s/decision':>10} {'s/select':>8} {'bits/sel':>8} {'bits/min':>8}   paper")
    out = [head, "-" * len(head)]
    for r in rows:
        sel = r.selection_s(gaze_s, max_trial)
        bits = itr_bits(r.n_targets, r.acc)
        bpm = itr_bpm(r.n_targets, r.acc, sel)
        delay = r.mean_delay()
        out.append(f"{r.paradigm + '-VEP':<8} {r.n_targets:>7} {r.n:>6} {100 * r.acc:>7.0f}% "
                   f"{100 * r.abstained:>6.0f}% {delay:>10.1f} {sel:>8.1f} {bits:>8.2f} "
                   f"{bpm:>8.1f}   {PAPER[r.paradigm][0]}")
    out.append("")
    for r in rows:
        out.append(f"{r.paradigm}-VEP: {r.note}")
    out.append(f"s/select adds a {gaze_s:g} s gaze shift to the decision delay (an abstained "
               f"trial counts {max_trial:g} s); bits per selection is Wolpaw's ITR, chance "
               "1/targets. Paper figures: Bin et al. 2009 " + "; ".join(
                   f"{p}-VEP {PAPER[p][0]} {PAPER[p][1]}" for p in ("time", "freq", "code")))
    out.append("Synthetic board (vep.fake / board.FakeBoard): the numbers check the pipeline, "
               "not anyone's visual cortex; --amp and --noise set how easy the data are.")
    return out


def compare(args: argparse.Namespace) -> int:
    rows = []
    try:
        for paradigm, fn in (("time", compare_time), ("freq", compare_freq), ("code", compare_code)):
            print(f"{paradigm}-VEP: {args.trials} trials per target...", file=sys.stderr,
                  flush=True)
            rows.append(fn(args))
    except BUILD_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("\n".join(compare_table(rows, gaze_s=1.0, max_trial=args.max_trial)))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    check_arguments(p, args)
    if args.compare:
        return compare(args)
    if args.paradigm == "freq":
        return run_freq(args)
    return run_coded(args)


if __name__ == "__main__":
    sys.exit(main())
