"""Live demo: look at a square and the decoder puts the cue box on it and says its label.

    python -m brainsync.demo                                  # c-VEP: Cerelog X8 + full-screen stimulus
    python -m brainsync.demo --fake --fast --no-stimulus      # synthetic board, faster than real time
    python -m brainsync.demo --paradigm freq                  # the old f-VEP demo (two_targets)

c-VEP, the default (the BrainSync standard, docs/CVEP-STANDARD.md, config cvep_3): three
white squares on black, YES / NO / HELP, each showing one 63-bit m-sequence 21 frames
(350 ms) later than the one before. Sequence: connect to the board, start the stimulus in
free-run mode (squares still until {"flicker": true} on its stdin, a cycle:<n> marker in
its log on every cycle's first flip, which this program tails), then training: the cue box
sits on target 0 ("Look at the YES square") for --train-cycles cycles (30, about 32 s),
which become the decoder's template; the leave-one-cycle-out accuracy is shown against
chance. Then every cycle gives a decision (vep.live.CodeVepDecoder): under each square
its label, a confidence bar and its correlation score, and a status line with the lead
and the committed choice; a committed change moves the cue box onto that square and
speaks its label. --template FILE skips training with a saved template (--save-template
FILE keeps one). Decoding runs for --seconds after training.

f-VEP (--paradigm freq, or a frequency config with -c; default config two_targets):
connect, start the stimulus with the squares held still, ask the user to keep still and
calibrate the rest baseline on that still screen (--calibrate s, default 30: shorter
baselines misjudge the per-target rest levels by about the abstain margin), then start
the flicker and decode for --seconds with the sliding-window FBCCA decoder. Every
committed change is written to the stimulus (-1 clears the box) and the target's label
is spoken; every window also sends the lean meter (unless --no-lean): an amber box on the
target the scores favour, its confidence and how many of the --votes agreeing windows a
commit needs are in.

Both: one status line per decision goes to stderr, a summary to stdout. Ctrl-C, the
stimulus exiting, its pipe closing or the board going silent for 5 s ends the run
cleanly; Ctrl-C before decoding starts (calibration or training) ends the program before
any decoding. Exit code 0 for a run that ended by time, Ctrl-C or Escape; 1 when the
board stalled, the stimulus failed or exited with an error; 130 when interrupted before
decoding; 2 for bad arguments or a bad config.

    python -m brainsync.demo --paradigm freq -c four_targets_low_first --classifier trca-phase --model latest

--model FILE alone loads a classifier trained by brainsync.calibrate (brainsync.templates),
which replaces --classifier. The phase-locked TRCA (--classifier trca-phase, brainsync.trca)
needs a model fitted on the user's own cued trials with the same targets, channels and
preprocessing: record them with
``python -m brainsync.record -c four_targets_low_first --trials 32``, train with
``python -m brainsync.train <file>_cued.npz`` (it says whether TRCA beats FBCCA on held-out
trials and saves the model to data/models), then pass --model PATH or --model latest. The
model's rest baseline is not saved: it is fitted on this session's still screen like
FBCCA's, so recalibrating every session costs nothing extra. With --lock the model's
templates are locked to the flicker's phase: the stimulus marks the unix time of its first
flickering frame ("free_on" in its log), and each window is scored at the phase the
flicker has reached (brainsync.trca.phase_offsets). Offline this decoded the
2026-10-24_235732 recording's 2 s windows at 76 % against 62 % phase-free; the rest
baseline is then fitted at random phases, and windows before the onset is known are
scored phase-free ("locked" in the status line says which).
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import trca
from .board import Board, CerelogBoard, FakeBoard
from .classify import ABSTAIN, PHASE_LOCKED, TRAINING_FREE, make
from .launch import Stimulus, launch_stimulus, speak
from .online import TUNED, Decision, Decoder, calibrate, threshold_arg
from .recordings import MONTAGE, POSTERIOR, Event, channel_indices
from .stimulus import StimulusConfig, load_config
from .stimulus.display import free_onset
from .templates import load_model
from .vep import live

# brainsync.trca's phase-locked TRCA, trained by brainsync.train. Not "trca": that name is
# brainsync.templates' TRCA, which a --model from brainsync.calibrate brings.
PHASE_TRCA = PHASE_LOCKED[0]
STALL_S = 5.0
POLL_S = 0.1
ONSET_WAIT_S = 1.0     # the free_on marker follows the first feedback line within a frame
# A 10 s baseline (about 4 independent 1.5 s windows) got the 15-vs-17 Hz rest offset wrong
# by 0.58 spreads (SD over 40 FakeBoard seeds), more than the 0.5 margin; 30 s gives ~0.33.
CALIBRATE_S = 30.0
PROMPTS = ("Keep still and look at the middle of the screen.", "Look at one of the squares.")
# c-VEP: the training prompt (target 0's label goes in) and the one after training
CODE_PROMPTS = ("Look at the {label} square and keep looking at it.", "Look at one of the squares.")
# what build() raises for a bad config, channel or classifier setting, or a missing driver
BUILD_ERRORS = (ValueError, KeyError, FileNotFoundError, ImportError)
PARADIGMS = ("code", "freq")
CODE_CONFIG = live.CONFIG      # the c-VEP standard: cvep_3, YES / NO / HELP
FREQ_CONFIG = "two_targets"    # the f-VEP demo pair, 15 and 17 Hz


def _names(text: str) -> list[str]:
    names = [c.strip() for c in text.split(",") if c.strip()]
    if not names:
        raise argparse.ArgumentTypeError(f"expected comma-separated names, got {text!r}")
    return names


def _channels(text: str) -> list[str | int]:
    return [int(c) if c.isdigit() else c for c in _names(text)]


def add_live_arguments(p: argparse.ArgumentParser, config: str = FREQ_CONFIG,
                       seconds: float = 120.0, code_config: str = CODE_CONFIG) -> None:
    """Add the decoder, board and stimulus flags every live app shares to ``p``.

    ``config`` and ``code_config`` are the -c/--config defaults of the f-VEP and the c-VEP
    paradigm, ``seconds`` that of --seconds. Check the parsed namespace with
    check_arguments() and resolve() (which picks the paradigm and fills the
    paradigm-dependent defaults); build() / build_code() and session() / code_session()
    read these flags.
    """
    p.set_defaults(freq_config=config, code_config=code_config)
    p.add_argument("--paradigm", choices=PARADIGMS, default=None,
                   help="code: the c-VEP standard; freq: the f-VEP flicker (default: the "
                        "-c config's paradigm, else code)")
    p.add_argument("-c", "--config", default=None,
                   help=f"stimulus config name or .toml path (default {code_config} for "
                        f"code, {config} for freq)")
    p.add_argument("--votes", type=int, default=None,
                   help="consecutive agreeing decisions needed to commit (default "
                        f"{live.VOTES} for code, {TUNED.votes} for freq)")
    p.add_argument("--margin", type=float, default=None,
                   help="abstain unless the best target leads by this much: a correlation "
                        f"for code (default 0), rest spreads for freq (default {TUNED.margin:g})")
    code = p.add_argument_group("c-VEP (--paradigm code)")
    code.add_argument("--train-cycles", type=int, default=live.TRAIN_CYCLES,
                      help="cycles recorded on target 0 for the template (default "
                           f"{live.TRAIN_CYCLES}, about {live.TRAIN_CYCLES * 1.05:.0f} s)")
    code.add_argument("--cycles", type=int, default=live.CYCLES,
                      help=f"cycles averaged per decision (default {live.CYCLES})")
    code.add_argument("--template", type=Path, default=None,
                      help="a saved template (vep.cvep.Template) instead of training")
    code.add_argument("--save-template", type=Path, default=None, metavar="FILE",
                      help="save the trained template to FILE (.npz)")
    freq = p.add_argument_group("f-VEP (--paradigm freq)")
    freq.add_argument("--classifier", default=TUNED.classifier,
                      choices=(*TRAINING_FREE, PHASE_TRCA),
                      help=f"training-free classifier (default {TUNED.classifier}); a trained "
                           f"one comes from --model, and {PHASE_TRCA} (brainsync.trca) needs one")
    freq.add_argument("--model", default=None, metavar="FILE",
                      help="trained model saved by brainsync.calibrate (its targets must match "
                           "the config; replaces --classifier and --harmonics), or with "
                           f"--classifier {PHASE_TRCA} a .npz from brainsync.train or 'latest' "
                           "(the newest in data/models)")
    freq.add_argument("--lock", action="store_true",
                      help=f"with --classifier {PHASE_TRCA}: lock the templates to the flicker's "
                           "phase, from the onset the stimulus reports")
    freq.add_argument("--window", type=float, default=TUNED.window_s,
                      help=f"window seconds (default {TUNED.window_s:g})")
    freq.add_argument("--step", type=float, default=TUNED.step_s,
                      help=f"seconds between decisions (default {TUNED.step_s:g})")
    freq.add_argument("--threshold", type=threshold_arg, default=TUNED.threshold,
                      help="abstain unless the best target is this many rest spreads above "
                           f"its rest level, or 'none' (default {TUNED.threshold})")
    freq.add_argument("--pad", type=float, default=2.0,
                      help="seconds filtered before each window and dropped (default 2)")
    freq.add_argument("--harmonics", type=int, default=3, help="template harmonics (default 3)")
    freq.add_argument("--calibrate", type=float, default=CALIBRATE_S,
                      help=f"still-screen seconds for the rest baseline (default {CALIBRATE_S:g})")
    p.add_argument("--seconds", type=float, default=seconds,
                   help=f"decoding run length in seconds (default {seconds:g}; c-VEP: after "
                        "training)")
    p.add_argument("--names", type=_names, default=list(MONTAGE),
                   help="comma-separated names of the 8 board channels, in order")
    p.add_argument("--channels", type=_channels, default=list(POSTERIOR),
                   help="analysis channels, names or indices (default " + ",".join(POSTERIOR) + ")")
    p.add_argument("--port", default=None, help="serial port (default: auto-detect)")
    p.add_argument("--verbose", action="store_true", help="show BrainFlow's log messages")
    fake = p.add_argument_group("synthetic board")
    fake.add_argument("--fake", action="store_true",
                      help="a synthetic board instead of the Cerelog X8 (c-VEP: "
                           "vep.live.standard_fake_board; f-VEP: FakeBoard)")
    fake.add_argument("--fast", action="store_true",
                      help="with --fake: virtual time, as fast as the decoder runs")
    fake.add_argument("--switch", type=float, default=10.0,
                      help="with --fake: seconds before the attended target changes (default 10)")
    fake.add_argument("--seed", type=int, default=0, help="with --fake: noise seed")
    p.add_argument("--no-stimulus", action="store_true",
                   help="do not start the stimulus window (c-VEP: needs --fake, the cycle "
                        "markers come from the display)")
    p.add_argument("--ignore-refresh", action="store_true",
                   help="let the stimulus run when its measured refresh is not 60 Hz "
                        "(it refuses by default)")
    p.add_argument("--no-voice", action="store_true", help="no spoken prompts")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.demo", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    add_live_arguments(p)
    p.add_argument("--no-lean", action="store_true",
                   help="do not show which target the decoder leans towards, or its confidence")
    return p


def check_arguments(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject inconsistent live flags through ``p.error`` (exit code 2)."""
    if args.fast and not args.fake:
        p.error("--fast needs --fake")
    for flag, value in (("--seconds", args.seconds), ("--calibrate", args.calibrate),
                        ("--window", args.window), ("--step", args.step),
                        ("--switch", args.switch)):
        if not value > 0:
            p.error(f"{flag} must be positive, got {value}")
    if args.votes is not None and args.votes < 1:
        p.error(f"--votes must be >= 1, got {args.votes}")
    if (args.margin is not None and not args.margin >= 0) or args.pad < 0:
        p.error(f"--margin and --pad must be >= 0, got {args.margin}, {args.pad}")
    if args.train_cycles < 2:
        p.error(f"--train-cycles must be >= 2 (leave-one-out needs two cycles), got "
                f"{args.train_cycles}")
    if args.cycles < 1:
        p.error(f"--cycles must be >= 1, got {args.cycles}")
    if args.template is not None and args.save_template is not None:
        p.error("--save-template needs a training stage; drop --template")
    if args.paradigm == "freq" and (args.template is not None or args.save_template is not None):
        p.error("--template and --save-template are for --paradigm code")
    if len(args.names) != len(MONTAGE):
        p.error(f"--names needs {len(MONTAGE)} names, got {len(args.names)}: {args.names}")
    if args.classifier == PHASE_TRCA and args.model is None:
        p.error(f"--classifier {PHASE_TRCA} needs --model PATH or --model latest "
                "(train one with python -m brainsync.train)")
    if args.lock and args.classifier != PHASE_TRCA:
        p.error(f"--lock needs --classifier {PHASE_TRCA} (only its trained templates have a "
                f"phase to lock), got --classifier {args.classifier}")


def wants_freq(args: argparse.Namespace) -> bool:
    """True when a flag only the f-VEP decoder reads is set (a trained --model, --lock, a
    --classifier other than the default): with no --paradigm and no -c, that picks f-VEP."""
    return bool(getattr(args, "model", None) or getattr(args, "lock", False)
                or getattr(args, "classifier", TUNED.classifier) != TUNED.classifier)


def config_name(args: argparse.Namespace) -> str:
    """The -c config, else the default of the paradigm asked for (c-VEP when none is, unless
    an f-VEP-only flag is set)."""
    if getattr(args, "config", None):
        return str(args.config)
    if getattr(args, "paradigm", None) == "freq" or (
            getattr(args, "paradigm", None) is None and wants_freq(args)):
        return str(getattr(args, "freq_config", FREQ_CONFIG))
    return str(getattr(args, "code_config", CODE_CONFIG))


def resolve(args: argparse.Namespace) -> StimulusConfig:
    """Load the config and settle the paradigm: --paradigm, else the config's.

    Sets ``args.paradigm`` ("code" or "freq"), ``args.config`` (the config used) and the
    paradigm-dependent defaults of --votes and --margin. Raises ValueError (one of
    BUILD_ERRORS) for a config of another paradigm, a t-VEP config, or --no-stimulus on
    the real board with c-VEP.
    """
    name = config_name(args)
    cfg = load_config(name)
    if cfg.paradigm == "time":
        raise ValueError(f"config {cfg.name!r} is a t-VEP config; run it with "
                         "python -m brainsync.vep --paradigm time")
    paradigm = args.paradigm or cfg.paradigm
    if cfg.paradigm != paradigm:
        raise ValueError(f"config {cfg.name!r} is a [{cfg.paradigm}] config, not "
                         f"--paradigm {paradigm}")
    if paradigm == "code" and args.no_stimulus and not args.fake:
        raise ValueError("--no-stimulus needs --fake with c-VEP: the cycle markers come "
                         "from the display")
    args.paradigm, args.config = paradigm, name
    if args.votes is None:
        args.votes = live.VOTES if paradigm == "code" else TUNED.votes
    if args.margin is None:
        args.margin = 0.0 if paradigm == "code" else TUNED.margin
    return cfg


def build(args: argparse.Namespace) -> tuple[StimulusConfig, Any, Decoder]:
    """The stimulus config, the board (not yet connected) and the decoder ``args`` ask for.

    Raises one of BUILD_ERRORS on a bad setting; main() reports it and exits 2.
    """
    cfg = load_config(config_name(args))
    if cfg.coded:
        raise ValueError(f"config {cfg.name!r} is a [{cfg.paradigm}] config; the f-VEP "
                         "decoder needs flicker frequencies")
    margin = TUNED.margin if args.margin is None else args.margin
    votes = TUNED.votes if args.votes is None else args.votes
    if args.fake:
        board: Any = FakeBoard(cfg.freqs, switch_s=args.switch, seed=args.seed,
                               clock=None if args.fast else time.time, ch_names=args.names)
    else:
        board = CerelogBoard(serial_port=args.port, ch_names=args.names, verbose=args.verbose)
    if args.classifier == PHASE_TRCA:
        clf: Any = _trained(args, cfg, board.fs)
    elif args.model:
        clf = load_model(args.model)
        if (clf.freqs.size != cfg.n_targets or np.any(np.abs(clf.freqs - cfg.freqs) > 0.01)
                or abs(clf.fs - board.fs) > 1e-6):
            raise ValueError(f"model {args.model} was trained for {clf.freqs.tolist()} Hz at "
                             f"{clf.fs:g} Hz, not {cfg.freqs.tolist()} Hz at {board.fs:g} Hz")
        clf.margin, clf.threshold = margin, args.threshold
    else:
        clf = make(args.classifier, freqs=cfg.freqs, fs=board.fs, harmonics=args.harmonics,
                   phases=cfg.phases, margin=margin, threshold=args.threshold)
    decoder = Decoder(clf, board.fs, window_s=args.window, step_s=args.step,
                      votes=votes, channels=args.channels, pad_s=args.pad,
                      ch_names=args.names, lock=args.lock)
    return cfg, board, decoder


def _trained(args: argparse.Namespace, cfg: StimulusConfig, fs: float) -> trca.TRCA:
    """The --model TRCA, checked against this session's targets, channels and filters."""
    path = trca.latest_model() if args.model == "latest" else Path(args.model)
    clf = trca.load_model(path, margin=args.margin, threshold=args.threshold)
    names = [args.names[i] for i in channel_indices(args.names, args.channels)]
    # (1, 100) Hz and 60 Hz are the Decoder's defaults; pass the flags if they ever exist
    trca.check_compatible(clf, cfg.freqs, fs, names, (1.0, 100.0), 60.0, args.harmonics)
    return clf


def confidence(scores: np.ndarray, spread: float) -> np.ndarray:
    """Softmax of the baseline-corrected scores in rest spreads: one share per target,
    summing to 1. Chance is 1 / n_targets; on the 2026-10-24_235732 recording (FBCCA,
    P8,O1,O2, 1.5 s) the leading share had a median of 49 % on still screens and 60 % on
    correctly led flicker windows, so it is a lean, not a probability of being right."""
    z = np.asarray(scores, dtype=float) / (spread if spread > 0 else 1.0)
    e = np.exp(z - z.max())
    return e / e.sum()


class Lean:
    """The meter line for each decision: the target the scores favour, its confidence in
    whole percent, and how many consecutive windows have named it (capped at ``votes``;
    ``votes`` once it is committed)."""

    def __init__(self, votes: int) -> None:
        self.votes = votes
        self.target = ABSTAIN
        self.streak = 0

    def update(self, d: Decision, spread: float) -> tuple[int, int, int]:
        share = confidence(d.scores, spread)
        k = int(np.argmax(share))
        if d.committed == k:
            self.streak = self.votes
        elif d.raw == k:
            self.streak = min(self.votes, self.streak + 1 if self.target == k else 1)
        else:
            self.streak = 0
        self.target = k
        pct = share[k] * 100.0
        return k, 0 if not math.isfinite(pct) else int(round(pct)), self.streak

    def line(self, d: Decision, spread: float) -> str:
        k, pct, n = self.update(d, spread)
        return f"lean {k} {pct} {n} {self.votes}"


@dataclass
class Tally:
    """What happened during a run."""

    windows: int = 0
    abstained: int = 0
    commits: list[tuple[float, int]] = field(default_factory=list)
    truth_windows: int = 0
    truth_hits: int = 0
    seconds: float = 0.0   # decoded so far, to the newest window's end

    def add(self, d: Decision, elapsed: float, truth: int | None) -> None:
        self.windows += 1
        self.seconds = elapsed
        self.abstained += d.raw == ABSTAIN
        if d.changed:
            self.commits.append((elapsed, d.committed))
        if truth is not None:
            self.truth_windows += 1
            self.truth_hits += d.committed == truth


def run(decoder: Decoder, board: Board, seconds: float,
        on_decision: Callable[[Decision], bool], sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic, stop: Callable[[], str | None] | None = None,
        poll_s: float = POLL_S, stall_s: float = STALL_S) -> str:
    """Decode until ``seconds`` of data have been fed; returns why the run ended.

    ``on_decision`` returning False ends the run ("feedback failed"); ``stop()``
    returning a reason ends it with that reason; no samples for ``stall_s`` by ``clock``
    ends it with "board stalled".
    """
    limit = decoder.n_seen + int(round(seconds * decoder.fs))
    heard = clock()
    while decoder.n_seen < limit:
        reason = stop() if stop is not None else None
        if reason:
            return reason
        sleep(poll_s)
        X, t = board.read()
        now = clock()
        if X.shape[1]:
            heard = now
        elif now - heard >= stall_s:
            return "board stalled"
        for d in decoder.feed(X, t):
            if not on_decision(d):
                return "feedback failed"
    return "time up"


def _status(d: Decision, elapsed: float, labels: tuple[str, ...], truth: int | None) -> str:
    def name(k: int) -> str:
        return "-" if k == ABSTAIN else labels[k]

    scores = "  ".join(f"{lab} {s:+.3f}" for lab, s in zip(labels, d.scores))
    line = (f"{elapsed:7.2f} s  {scores}  lead {d.gap:4.1f}  level {d.level:+4.1f}  "
            f"window {name(d.raw):>3}  "
            f"committed {name(d.committed):>3}")
    if d.locked:
        line += "  locked"
    if d.changed:
        line += "  <- new"
    if truth is not None:
        line += f"  (true {name(truth)})"
    return line


def _summary(tally: Tally, seconds: float, labels: tuple[str, ...], ended: str) -> list[str]:
    counts = Counter(k for _, k in tally.commits)
    per = ", ".join(f"{'none' if k == ABSTAIN else labels[k]}: {counts[k]}" for k in sorted(counts))
    rate = f"{100 * tally.abstained / tally.windows:.0f}%" if tally.windows else "n/a"
    lines = [f"demo: {seconds:.1f} s decoded, {tally.windows} windows, {rate} abstained, "
             f"{len(tally.commits)} commit changes" + (f" ({per})" if per else "")
             + f"; ended: {ended}"]
    if tally.commits:
        lines.append("  commits: " + "  ".join(
            f"{t:.1f}s->{'none' if k == ABSTAIN else labels[k]}" for t, k in tally.commits))
    if tally.truth_windows:
        lines.append(f"  fake board: the committed target was the true one on "
                     f"{100 * tally.truth_hits / tally.truth_windows:.0f}% of "
                     f"{tally.truth_windows} windows")
    return lines


def stimulus_args(args: argparse.Namespace, extra: Sequence[str] = ()) -> list[str]:
    """The stimulus command line for a live run: free-run, feedback on stdin, held still
    until the first feedback line, unix markers (a free run prints one, its onset);
    ``extra`` is appended (e.g. ["--cards", DIR])."""
    out = ["-c", config_name(args), "--free", "--seconds", f"{args.seconds:g}",
           "--markers", "unix", "--feedback", "stdin", "--hold-still", "--no-wait"]
    if args.ignore_refresh:
        out.append("--ignore-refresh")
    out.extend(str(a) for a in extra)
    return out


_stimulus_args = stimulus_args   # the name tests/test_record.py uses


def send(stim: Stimulus | None, line: str | int) -> bool:
    """Write one feedback line (a target index, -1, or a command such as "card 3");
    False when the pipe is gone. Without a stimulus there is nothing to fail: True."""
    if stim is None:
        return True
    pipe: Any = stim.proc.stdin
    try:
        pipe.write(f"{line}\n")
        pipe.flush()
    except (BrokenPipeError, OSError, ValueError):
        return False
    return True


def find_onset(stim: Stimulus | None, decoder: Decoder) -> bool:
    """Give a locking decoder the free run's onset from the stimulus log, once it is
    there; True when the decoder has one."""
    if decoder.lock and decoder.onset is None and stim is not None:
        decoder.set_onset(free_onset(stim.text()))
    return decoder.onset is not None


def _await_onset(stim: Stimulus | None, decoder: Decoder, wait_s: float = ONSET_WAIT_S) -> None:
    """Poll the stimulus log for the onset for up to ``wait_s`` and report the result."""
    deadline = time.monotonic() + wait_s
    while not find_onset(stim, decoder) and stim is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    if decoder.onset is not None:
        print(f"flicker onset at {decoder.onset:.3f}: templates locked to the flicker's phase",
              file=sys.stderr)
    else:
        why = "no stimulus" if stim is None else "the stimulus logged no free_on marker yet"
        print(f"warning: {why}; decoding phase-free until an onset arrives", file=sys.stderr)


def _close(stim: Stimulus | None) -> int | None:
    """End the stimulus (EOF on its stdin ends a free run); its exit code."""
    if stim is None:
        return None
    try:
        if stim.proc.stdin is not None:
            stim.proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    return stim.finish(timeout=5.0)


def session(args: argparse.Namespace, cfg: StimulusConfig, board: Any, decoder: Decoder, *,
            stimulus_args: Sequence[str], on_decision: Callable[[Decision, float], bool],
            on_flicker: Callable[[Stimulus | None], bool] | None = None,
            prompts: tuple[str, str] = PROMPTS) -> tuple[str, int]:
    """One live run of the sequence in this module's docstring; returns (why it ended, exit code).

    Prints the setup line, connects ``board``, launches the stimulus with ``stimulus_args``
    (unless --no-stimulus), speaks ``prompts[0]``, fits the rest baseline on the still
    screen, sends -1 (which starts the flicker), speaks ``prompts[1]`` and decodes for
    --seconds. ``on_flicker(stim)`` runs once just before decoding (``decoder.n_seen`` is
    then the first decoded sample), e.g. to send extra feedback lines with send();
    ``on_decision(d, elapsed)`` gets every decision with its seconds since the flicker
    started. Either returning False ends the run as "stimulus pipe closed". Status lines
    and the summary are the caller's; it prints no summary when this returns "not started"
    (Ctrl-C before decoding, code 130). Code 1: board stalled, an error, or the stimulus
    exited non-zero. The stimulus is always closed before this returns.
    """
    labels = cfg.labels
    voice = not args.no_voice
    fast = args.fake and args.fast
    sleep: Callable[[float], None] = board.advance if fast else time.sleep
    clock: Callable[[], float] = (lambda: board.elapsed) if fast else time.monotonic
    print(f"{'fake board' if args.fake else 'Cerelog X8'} at {board.fs:g} Hz; "
          f"{decoder.classifier!r}; channels {','.join(decoder.channel_names)}; window "
          f"{args.window:g} s, step {args.step:g} s, {args.votes} votes", file=sys.stderr)
    if args.calibrate < CALIBRATE_S:
        print(f"warning: a {args.calibrate:g} s baseline is noisy; rest decisions may lean "
              f"towards one target (default {CALIBRATE_S:g} s)", file=sys.stderr)

    stim: Stimulus | None = None
    ended = "not started"
    code = 0
    try:
        if not args.fake:
            print("connecting to the Cerelog X8 (can take 15 s)...", file=sys.stderr)
        with board:
            if not args.no_stimulus:
                # the squares stand still until the first feedback line, so the baseline
                # is fit on the same still screen the replay tuning used
                print("starting the stimulus...", file=sys.stderr)
                stim = launch_stimulus(list(stimulus_args), stdin=subprocess.PIPE)
            speak(prompts[0], voice)
            if args.fake:
                board.set_target(ABSTAIN)
            cal = calibrate(decoder, board, args.calibrate,
                            sleep=board.advance if fast else None)
            if args.fake:
                board.set_target(None)
            print(f"baseline from {cal.n_windows} still windows ({cal.n_rejected} rejected): "
                  + " ".join(f"{lab} {m:.4f}" for lab, m in zip(labels, cal.mean))
                  + f", spread {cal.spread:.4f}", file=sys.stderr)

            def stimulus_gone() -> str | None:
                find_onset(stim, decoder)       # a late free_on marker still locks the rest
                if stim is not None and stim.proc.poll() is not None:
                    return f"stimulus exited (code {stim.proc.returncode})"
                return None

            # the first feedback line starts the flicker
            gone = stimulus_gone() or (None if send(stim, ABSTAIN) else "stimulus pipe closed")
            if gone:
                ended = f"{gone} before the flicker started"
            else:
                speak(prompts[1], voice)
                if decoder.lock:
                    _await_onset(stim, decoder)
                _, t_read = board.read()  # drop what arrived during the prompt
                if not decoder.lock:
                    # the flicker began with that line; trained templates are locked to the
                    # board's clock at this moment (estimated: the newest sample, a prompt's
                    # length late; a locking decoder takes the stimulus's free_on marker)
                    decoder.set_onset(float(t_read[-1]) if len(t_read) else None)
                began = decoder.n_seen
                ended = "started"

                def timed(d: Decision) -> bool:
                    return on_decision(d, (d.end - began) / decoder.fs)

                try:
                    if on_flicker is not None and not on_flicker(stim):
                        ended = "feedback failed"
                    else:
                        ended = run(decoder, board, args.seconds, timed, sleep=sleep,
                                    clock=clock, stop=stimulus_gone)
                except KeyboardInterrupt:
                    ended = "interrupted"
                if ended == "feedback failed":
                    ended = "stimulus pipe closed"
                if ended == "board stalled":
                    print(f"error: the board sent nothing for {STALL_S:g} s", file=sys.stderr)
                    code = 1
    except KeyboardInterrupt:
        if ended == "not started":   # connecting, launching or calibrating: nothing decoded
            print("interrupted before decoding started", file=sys.stderr)
            code = 130
        else:
            ended = "interrupted"
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        ended = "error"
        code = 1
    finally:
        stim_code = _close(stim)
        if stim is not None and stim_code not in (0, None):
            print(f"error: the stimulus exited with code {stim_code}; see {stim.log}",
                  file=sys.stderr)
            code = code or 1
    return ended, code


# -- c-VEP: the standard (docs/CVEP-STANDARD.md) ---------------------------------------------

def is_standard_code(cfg: StimulusConfig) -> bool:
    """True when ``cfg`` flickers the standard's code: three targets of one 63-bit
    m-sequence, 21 frames apart, one frame per bit (cvep_3, swipe_cvep)."""
    c = cfg.code
    return (c is not None and cfg.n_targets == live.N_TARGETS and c.length == live.LENGTH
            and c.lag_frames == live.LAG_FRAMES and c.frames_per_bit == 1)


def build_code(args: argparse.Namespace, cfg: StimulusConfig) -> tuple[Any, live.CodeVepDecoder]:
    """The board (not yet connected) and the self-training c-VEP decoder for ``cfg``.

    --fake gives the standard's synthetic board (vep.live.standard_fake_board) for a
    config with the standard's code, and the same SequenceFakeBoard over its own code for
    any other [code] config. --template loads a saved template, so the decoder skips
    training. Raises one of BUILD_ERRORS.
    """
    if cfg.code is None:
        raise ValueError(f"config {cfg.name!r} has no [code] table; c-VEP needs one")
    if args.fake:
        clock = None if args.fast else time.time
        if is_standard_code(cfg):
            board: Any = live.standard_fake_board(0, seed=args.seed, clock=clock,
                                                  refresh=cfg.refresh)
        else:
            from .board import FS
            from .stimulus.coded import sequence_for
            from .vep.fake import SequenceFakeBoard
            board = SequenceFakeBoard(sequence_for(cfg, args.seed), fs=FS, refresh=cfg.refresh,
                                      fixated=0, amp=3.0, noise=8.0, seed=args.seed,
                                      clock=clock)
        names = tuple(board.ch_names)
    else:
        board = CerelogBoard(serial_port=args.port, ch_names=args.names, verbose=args.verbose)
        names = tuple(args.names)
    template = None
    if args.template is not None:
        from .vep.cvep import Template
        template = Template.load(args.template)
    decoder = live.CodeVepDecoder(
        board.fs, n_targets=cfg.n_targets, lag_frames=cfg.code.lag_frames,
        cycle_frames=cfg.code.cycle_frames, refresh=cfg.refresh, channels=args.channels,
        ch_names=names, votes=live.VOTES if args.votes is None else args.votes,
        cycles=args.cycles, margin=0.0 if args.margin is None else args.margin,
        train_cycles=args.train_cycles, train_target=0, template=template)
    return board, decoder


def code_stimulus_args(args: argparse.Namespace, cfg: StimulusConfig,
                       extra: Sequence[str] = ()) -> list[str]:
    """The stimulus command line for a c-VEP run: free run of ``cfg``, Unix markers (the
    cycle:<n> lines this program tails) in its log, feedback on stdin, held still until
    {"flicker": true}, the config's labels under the squares from the first frame (EOF on
    stdin ends it); ``extra`` is appended."""
    out = ["-c", str(cfg.path or cfg.name), "--free", "--markers", "unix", "--feedback",
           "stdin", "--hold-still", "--no-wait", "--labels"]
    if args.ignore_refresh:
        out.append("--ignore-refresh")
    out.extend(str(a) for a in extra)
    return out


def send_text(stim: Stimulus | None, text: str) -> bool:
    """Replace the display's status line (``{"text": ...}``); False when the pipe is gone."""
    return send(stim, json.dumps({"text": text}))


def send_labels(stim: Stimulus | None, lines: Sequence[str]) -> bool:
    """Replace the lines under the squares (``{"labels": [...]}``); False when the pipe is
    gone."""
    return send(stim, json.dumps({"labels": list(lines)}))


def training_text(label: str, cycle: int, of: int) -> str:
    """The status line while training (docs/CVEP-STANDARD.md section 3)."""
    return f"training: keep looking at {label}   cycle {cycle}/{of}"


def trained_text(accuracy: float, chance: float) -> str:
    """The status line once the template is ready: held-out accuracy against chance."""
    acc = "-" if not math.isfinite(accuracy) else f"{100 * accuracy:.0f}"
    return f"template ready: {acc} % on held-out cycles (chance {100 * chance:.0f} %)"


def default_gaze(args: argparse.Namespace, n_targets: int) -> Callable[[float], int]:
    """The fake user's gaze after training: target 0, 1, 2, ... for --switch s each."""
    return lambda elapsed: int(elapsed // args.switch) % n_targets


def code_session(args: argparse.Namespace, cfg: StimulusConfig, board: Any,
                 decoder: live.CodeVepDecoder, *, stimulus_args: Sequence[str],
                 on_decision: Callable[[Decision, float], bool],
                 on_start: Callable[[Stimulus | None], bool] | None = None,
                 feed: Callable[[np.ndarray, np.ndarray, list[Event]], list[Decision]] | None = None,
                 gaze: Callable[[float], int | None] | None = None,
                 labels: Sequence[str] | None = None,
                 prompts: tuple[str, str] = CODE_PROMPTS) -> tuple[str, int]:
    """One live c-VEP run; returns (why it ended, exit code), as session() does.

    Connects ``board``, launches the stimulus with ``stimulus_args`` (unless
    --no-stimulus), starts the flicker ({"flicker": true}) and, while ``decoder`` trains,
    keeps the cue box on target 0 with the training text and cycle count as the status
    line; then shows the held-out accuracy, clears the box, speaks ``prompts[1]`` and
    decodes for --seconds. ``on_start(stim)`` runs once just before decoding; every
    decision goes to ``on_decision(d, elapsed)`` with its seconds since decoding began.
    Either returning False ends the run as "stimulus pipe closed". ``feed`` replaces
    ``decoder.feed`` (BrainSwipe filters the cycles of a settling card); ``gaze(elapsed)``
    moves the fake board's gaze (default: every --switch s to the next target). Cycle
    events come from the fake board, or from the stimulus log's cycle:<n> markers.
    Returns "not started" with code 130 for Ctrl-C before decoding; code 1 for a stalled
    board, an error or a stimulus that exited non-zero. The stimulus is always closed.
    """
    from .vep.__main__ import LogTail   # lazy: vep.__main__ imports this module

    labels = tuple(labels or cfg.labels)
    voice = not args.no_voice
    fast = args.fake and args.fast
    sleep: Callable[[float], None] = board.advance if fast else time.sleep
    clock: Callable[[], float] = (lambda: board.elapsed) if fast else time.monotonic
    feed = feed or decoder.feed
    gaze = gaze or default_gaze(args, cfg.n_targets)
    cycle_s = cfg.code.cycle_frames / cfg.refresh if cfg.code is not None else 1.05
    chans = ",".join(decoder.ch_names[i] for i in decoder.rows)
    print(f"c-VEP {cfg.name}: {cfg.n_targets} targets {decoder.lag_frames} frames apart, "
          f"{cycle_s:.2f} s cycles; {'fake board' if args.fake else 'Cerelog X8'} at "
          f"{board.fs:g} Hz; channels {chans}; {decoder.votes} votes on the mean of "
          f"{decoder.cycles} cycles", file=sys.stderr)
    stim: Stimulus | None = None
    ended = "not started"
    code = 0
    try:
        if not args.fake:
            print("connecting to the Cerelog X8 (can take 15 s)...", file=sys.stderr)
        with board:
            if not args.no_stimulus:
                print("starting the stimulus...", file=sys.stderr)
                stim = launch_stimulus(list(stimulus_args), stdin=subprocess.PIPE)
            if args.fake:
                events: Callable[[], list[Event]] = board.read_events
            else:
                assert stim is not None   # resolve() refuses --no-stimulus on the real board
                events = LogTail(stim.log).events

            def stimulus_gone() -> str | None:
                if stim is not None and stim.proc.poll() is not None:
                    return f"stimulus exited (code {stim.proc.returncode})"
                return None

            train_label = labels[decoder.train_target]
            if decoder.training:
                speak(prompts[0].format(label=train_label), voice)
                if args.fake:
                    board.set_target(decoder.train_target)
            board.read()      # samples from before the flicker are not news
            events()
            if args.fake:
                board.start_flicker()
            # JSON from the first line on: integer lines then only move the cue box, and
            # only {"flicker": ...} turns the flicker on or off (stimulus.feedback)
            ok = send(stim, json.dumps({"flicker": True}))
            if decoder.training:
                k, n = decoder.progress
                ok = ok and send(stim, decoder.train_target) and send_text(
                    stim, training_text(train_label, k, n))
                print(f"training: {decoder.train_cycles} cycles of {cycle_s:.2f} s on "
                      f"{train_label!r}...", file=sys.stderr)
            gone = stimulus_gone() or (None if ok else "stimulus pipe closed")
            if gone:
                ended = f"{gone} before the flicker started"
                return ended, code
            began: float | None = None
            train_began = heard = clock()
            shown = decoder.progress[0] if decoder.training else -1
            timeout = (decoder.train_cycles + 3) * cycle_s + 10.0

            def start_decoding() -> bool:
                nonlocal began, ended
                ok = send(stim, ABSTAIN)
                speak(prompts[1], voice)
                decoder.reset()
                if on_start is not None:
                    ok = on_start(stim) and ok
                began = clock()
                ended = "started"
                return ok

            if not decoder.training and not start_decoding():
                ended = "stimulus pipe closed"
                return ended, code
            try:
                while True:
                    why = stimulus_gone()
                    if why:
                        ended = why if began is not None else f"{why} during training"
                        break
                    now = clock()
                    if began is not None:
                        elapsed = now - began
                        if elapsed >= args.seconds:
                            ended = "time up"
                            break
                        if args.fake:
                            want = gaze(elapsed)
                            if want is not None and want != board.fixated:
                                board.set_target(want)
                    elif now - train_began > timeout:
                        raise RuntimeError(f"saw {decoder.progress[0]} cycle markers in "
                                           f"{timeout:g} s, needed {decoder.train_cycles}; "
                                           "is the display flickering?")
                    sleep(POLL_S)
                    X, t = board.read()
                    now = clock()
                    if X.shape[1]:
                        heard = now
                    elif now - heard >= STALL_S:
                        ended = "board stalled"
                        print(f"error: the board sent nothing for {STALL_S:g} s",
                              file=sys.stderr)
                        code = 1
                        break
                    was_training = decoder.training
                    decisions = feed(X, t, events())
                    ok = True
                    if was_training:
                        if decoder.training:
                            k, n = decoder.progress
                            if k != shown:
                                shown = k
                                ok = send_text(stim, training_text(train_label, k, n))
                        else:
                            print(f"template from {decoder.train_cycles} cycles: "
                                  f"{trained_text(decoder.train_accuracy, decoder.chance)}",
                                  file=sys.stderr)
                            if args.save_template is not None and decoder.template is not None:
                                path = decoder.template.save(args.save_template)
                                print(f"template saved to {path}", file=sys.stderr)
                            ok = send_text(stim, trained_text(decoder.train_accuracy,
                                                              decoder.chance))
                            ok = start_decoding() and ok
                    else:
                        assert began is not None
                        for d in decisions:
                            if not on_decision(d, clock() - began):
                                ok = False
                                break
                    if not ok:
                        ended = "stimulus pipe closed"
                        break
            except KeyboardInterrupt:
                if began is None:
                    raise
                ended = "interrupted"
    except KeyboardInterrupt:
        if ended == "not started":
            print("interrupted before decoding started", file=sys.stderr)
            code = 130
        else:
            ended = "interrupted"
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        ended = "error"
        code = 1
    finally:
        stim_code = _close(stim)
        if stim is not None and stim_code not in (0, None):
            print(f"error: the stimulus exited with code {stim_code}; see {stim.log}",
                  file=sys.stderr)
            code = code or 1
    return ended, code


def _name(labels: Sequence[str], k: int) -> str:
    return "-" if k == ABSTAIN or not 0 <= k < len(labels) else labels[k]


def _code_status(d: Decision, elapsed: float, labels: Sequence[str], truth: int | None) -> str:
    scores = "  ".join(f"{lab} {s:+.3f}" for lab, s in zip(labels, d.scores))
    lead = "  -  " if not np.isfinite(d.gap) else f"{d.gap:.3f}"
    line = (f"{elapsed:7.2f} s  {scores}  lead {lead}  best {_name(labels, d.raw):>4}  "
            f"committed {_name(labels, d.committed):>4}")
    if d.changed:
        line += "  <- new"
    if truth is not None:
        line += f"  (true {_name(labels, truth)})"
    return line


def _code_summary(tally: Tally, labels: Sequence[str], ended: str,
                  decoder: live.CodeVepDecoder) -> list[str]:
    counts = Counter(k for _, k in tally.commits)
    per = ", ".join(f"{_name(labels, k) if k != ABSTAIN else 'none'}: {counts[k]}"
                    for k in sorted(counts))
    lines = [f"demo: c-VEP, {tally.seconds:.1f} s decoded, {tally.windows} decisions, "
             f"{len(tally.commits)} commit changes" + (f" ({per})" if per else "")
             + f"; ended: {ended}"]
    if math.isfinite(decoder.train_accuracy):
        lines.append(f"  {trained_text(decoder.train_accuracy, decoder.chance)}")
    if tally.commits:
        lines.append("  commits: " + "  ".join(
            f"{t:.1f}s->{'none' if k == ABSTAIN else labels[k]}" for t, k in tally.commits))
    if tally.truth_windows:
        lines.append(f"  fake board: the committed target was the true one on "
                     f"{100 * tally.truth_hits / tally.truth_windows:.0f}% of "
                     f"{tally.truth_windows} decisions")
    return lines


def run_code(args: argparse.Namespace, cfg: StimulusConfig) -> int:
    """The c-VEP demo: train on target 0, then show the confidence and speak commits."""
    try:
        board, decoder = build_code(args, cfg)
    except BUILD_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    labels = cfg.labels
    voice = not args.no_voice
    tally = Tally()
    stim: Stimulus | None = None

    def on_start(started: Stimulus | None) -> bool:
        nonlocal stim
        stim = started
        return True

    def on_decision(d: Decision, elapsed: float) -> bool:
        truth = board.fixated if args.fake else None
        tally.add(d, elapsed, truth)
        print(_code_status(d, elapsed, labels, truth), file=sys.stderr, flush=True)
        ok = (send_labels(stim, live.confidence_labels(d, labels))
              and send_text(stim, live.confidence_line(d, labels)))
        if ok and d.changed:
            if d.committed != ABSTAIN:
                speak(labels[d.committed], voice)
            ok = send(stim, d.committed)
        return ok

    ended, code = code_session(args, cfg, board, decoder,
                               stimulus_args=code_stimulus_args(args, cfg),
                               on_start=on_start, on_decision=on_decision)
    if ended != "not started":
        print("\n".join(_code_summary(tally, labels, ended, decoder)))
    return code


def run_freq(args: argparse.Namespace) -> int:
    """The f-VEP demo: rest baseline on the still screen, then the sliding FBCCA decoder."""
    try:
        cfg, board, decoder = build(args)
    except BUILD_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    labels = cfg.labels
    voice = not args.no_voice
    tally = Tally()
    lean = None if args.no_lean else Lean(decoder.votes)
    stim: Stimulus | None = None

    def on_flicker(started: Stimulus | None) -> bool:
        nonlocal stim
        stim = started
        return True

    def on_decision(d: Decision, elapsed: float) -> bool:
        truth = board.active_target() if args.fake else None
        tally.add(d, elapsed, truth)
        print(_status(d, elapsed, labels, truth), file=sys.stderr, flush=True)
        if lean is not None and not send(stim, lean.line(d, decoder.classifier.baseline_spread)):
            return False
        if not d.changed:
            return True
        if d.committed != ABSTAIN:
            speak(labels[d.committed], voice)
        return send(stim, d.committed)

    ended, code = session(args, cfg, board, decoder, stimulus_args=stimulus_args(args),
                          on_flicker=on_flicker, on_decision=on_decision)
    if ended != "not started":
        print("\n".join(_summary(tally, tally.seconds, labels, ended)))
    return code


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    check_arguments(p, args)
    try:
        cfg = resolve(args)
    except BUILD_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.paradigm == "freq":
        return run_freq(args)
    return run_code(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
