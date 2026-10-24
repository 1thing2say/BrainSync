"""Live flicker A/B with the headset: square, full sine and gentle sine in one session.

    python -m brainsync.ab                          # Cerelog X8 + PsychoPy, 24 trials
    python -m brainsync.ab --rate                   # also rate each trial's comfort, 1-5
    python -m brainsync.ab --conditions sine:1,sine:0.6,sine:0.4
    python -m brainsync.ab --fake                   # synthetic board, no display, instant
    python -m brainsync.ab --score FILE_cued.npz    # score a saved A/B session again

One cued session mixes the flicker conditions (default square:1, sine:1, sine:0.5): every
block cues each (condition, target) pair once in a shuffled order, so electrode drift and
fatigue hit every condition alike, and the still screen between trials is the same
mid-grey for all of them. While it runs, each trial is scored once its still window has
been recorded, and a per-condition scoreboard goes to the terminal and under the targets
on screen (between flickers only):

- decoded: the classifier (FBCCA by default, with the session's still windows as its rest
  baseline) picked the cued square, forced choice over the whole flicker window;
- SNR: power at the cued frequency against the bins within 1 Hz (brainsync.check), on the
  posterior channels' mean spectrum over the flicker window, in dB;
- CCA r: the canonical correlation between those channels and the cued frequency;
- comfort: the 1-5 rating, with --rate. Ratings stay blind: nothing on screen or in the
  terminal gives away a trial's condition until that trial is rated.

The still-screen line is the same SNR on the still windows, which should sit near 0 dB.
With a handful of trials per condition, differences under about twice the +/- values
(standard errors) are noise. The session is saved as a _cued.npz; score_cued pools its
conditions, --score splits them. The scoreboard is recomputed over the whole recording
each time, so the final numbers are the ones --score gives. --fake uses a made-up response
(amplitude proportional to depth, square 4/pi times sine, its fundamental): it checks the
pipeline, not the eye.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .board import FS, FakeBoard, record
from .check import (SNR_GUARD_BINS, SNR_SPAN_HZ, local_snr_db, segment_problem,
                    welch_psd)
from .classify import make
from .launch import launch_stimulus, speak
from .paths import recordings_dir, unwritable_reason
from .record import TAIL_S, _save, fake_dir, trailing_s
from .recordings import MONTAGE, POSTERIOR, channel_indices, load_recording
from .signal import preprocess
from .stimulus import (Condition, StimulusConfig, config_warnings, load_config, make_trials,
                       override, parse_conditions)
from .stimulus.display import format_marker

DEFAULT_CONDITIONS = "square:1,sine:1,sine:0.5"
METHODS = ("cca", "fbcca")
EDGE_S = 1.0          # live: data needed past a still window before its trial is scored
POLL_S = 0.5          # live: how often the stimulus log is read
RATE_ALLOW_S = 30.0   # recording budget per trial for a comfort rating
END_ALLOW_S = 600.0   # and for reading the end screen
SQUARE_GAIN = 4.0 / math.pi   # fake board: a square wave's fundamental against a sine's

_EVENT_RE = re.compile(
    r"^\s*\[marker\s+([-+0-9.eE]+)\]\s+(cond|stim_on|stim_off|rate):(\d+)\s*$")
_CONDITIONS_RE = re.compile(r"^conditions: (.+)$", re.MULTILINE)


# -- the session's markers ------------------------------------------------------------------

@dataclass
class TrialLog:
    """One trial as the stimulus log describes it; times in unix seconds, nan until seen."""

    condition: int
    target: int = -1
    t_on: float = math.nan
    t_off: float = math.nan
    rating: int | None = None


def parse_session(text: str) -> tuple[tuple[Condition, ...], list[TrialLog]]:
    """The "conditions:" line and every trial in a stimulus log.

    A cond marker starts a trial; the stim_on, stim_off and rate markers after it belong
    to it. An empty tuple means the log has no conditions line (not an A/B session).
    """
    m = _CONDITIONS_RE.search(text)
    conditions = parse_conditions(",".join(m.group(1).split())) if m else ()
    trials: list[TrialLog] = []
    for line in text.splitlines():
        hit = _EVENT_RE.match(line)
        if hit is None:
            continue
        stamp, kind, value = float(hit.group(1)), hit.group(2), int(hit.group(3))
        if kind == "cond":
            trials.append(TrialLog(condition=value))
        elif not trials:
            continue
        elif kind == "stim_on":
            trials[-1].target, trials[-1].t_on = value, stamp
        elif kind == "stim_off" and value == trials[-1].target:
            trials[-1].t_off = stamp
        elif kind == "rate":
            trials[-1].rating = value
    return conditions, trials


# -- scoring --------------------------------------------------------------------------------

@dataclass(frozen=True)
class TrialScore:
    index: int            # position in the session, from 0
    condition: int
    target: int
    decoded: int          # the classifier's forced choice
    snr_db: float         # flicker window, cued frequency
    still_snr_db: float   # still window, same frequency
    corr: float           # CCA r with the cued frequency's templates
    rating: int | None

    @property
    def hit(self) -> bool:
        return self.decoded == self.target


def score_session(X: np.ndarray, t: np.ndarray, fs: float, ch_names: Sequence[str],
                  freqs: Sequence[float], phases: Sequence[float] | None,
                  trials: Sequence[TrialLog], window_s: float, latency: float,
                  method: str = "fbcca", edge_s: float = 0.0) -> list[TrialScore]:
    """Score every trial whose flicker and still windows (plus ``edge_s``) are in the data.

    Windows are cut the way brainsync.recordings.cut_windows cuts them: the whole
    recording is preprocessed, the flicker window starts at stim_on + latency and the
    still window at stim_off + latency, both ``window_s`` long. Posterior channels only
    (all channels if none is named in ``ch_names``). The classifier's baseline is fitted
    on every scored trial's still window, once there are at least two.
    """
    n_win = int(round(window_s * fs))
    need = n_win + int(round(edge_s * fs))
    tol = 0.01 / fs
    picked: list[tuple[int, TrialLog, int, int]] = []
    for i, tr in enumerate(trials):
        if not (math.isfinite(tr.t_on) and math.isfinite(tr.t_off)):
            continue
        if not 0 <= tr.target < len(freqs) or t.size == 0 or tr.t_on + latency < t[0] - tol:
            continue
        s_on = int(np.searchsorted(t, tr.t_on + latency - tol))
        s_off = int(np.searchsorted(t, tr.t_off + latency - tol))
        if s_off + need <= t.size:
            picked.append((i, tr, s_on, s_off))
    if not picked:
        return []
    posterior = [c for c in POSTERIOR if c in ch_names]
    rows = channel_indices(ch_names, posterior or None)
    Y = preprocess(np.asarray(X, dtype=float)[rows], fs)
    span = np.arange(n_win)
    on = np.stack([Y[:, s + span] for _, _, s, _ in picked])
    off = np.stack([Y[:, s + span] for _, _, _, s in picked])
    clf = make(method, freqs=freqs, fs=fs, phases=phases)
    if len(picked) >= 2:
        try:
            clf.fit_baseline(off)
        except ValueError:   # flat still windows: decide on raw scores
            clf.clear_baseline()
    decoded = clf.scores(on).argmax(axis=1)
    corr = make("cca", freqs=freqs, fs=fs, phases=phases).raw_scores(on)
    f, p_on = welch_psd(on, fs, segment_s=window_s)
    _, p_off = welch_psd(off, fs, segment_s=window_s)
    p_on, p_off = p_on.mean(axis=1), p_off.mean(axis=1)   # mean spectrum over channels
    out = []
    for j, (i, tr, _, _) in enumerate(picked):
        fk = float(freqs[tr.target])
        out.append(TrialScore(index=i, condition=tr.condition, target=tr.target,
                              decoded=int(decoded[j]),
                              snr_db=float(local_snr_db(f, p_on[j], fk)),
                              still_snr_db=float(local_snr_db(f, p_off[j], fk)),
                              corr=float(corr[j, tr.target]), rating=tr.rating))
    return out


@dataclass(frozen=True)
class ConditionSummary:
    condition: Condition
    n: int
    hits: int
    snr_db: float
    snr_sem: float
    corr: float
    comfort: float
    n_rated: int


def _mean(x: Sequence[float]) -> float:
    return float(np.mean(x)) if len(x) else math.nan


def _sem(x: Sequence[float]) -> float:
    return float(np.std(x, ddof=1) / math.sqrt(len(x))) if len(x) > 1 else math.nan


def summarize(scores: Sequence[TrialScore],
              conditions: Sequence[Condition]) -> list[ConditionSummary]:
    out = []
    for j, cond in enumerate(conditions):
        mine = [s for s in scores if s.condition == j]
        snr = [s.snr_db for s in mine]
        rated = [s.rating for s in mine if s.rating is not None]
        out.append(ConditionSummary(cond, len(mine), sum(s.hit for s in mine), _mean(snr),
                                    _sem(snr), _mean([s.corr for s in mine]),
                                    _mean(rated), len(rated)))
    return out


def table(scores: Sequence[TrialScore], conditions: Sequence[Condition],
          n_targets: int) -> list[str]:
    """The per-condition scoreboard, monospaced, plus the still-screen control line."""
    lines = [f"{'condition':<11}{'decoded':>13}{'SNR dB':>14}{'CCA r':>7}  comfort"]
    for r in summarize(scores, conditions):
        decoded = f"{r.hits}/{r.n} {r.hits / r.n:4.0%}" if r.n else "-"
        snr = (f"{r.snr_db:+.1f} +/- {r.snr_sem:.1f}" if r.n > 1
               else f"{r.snr_db:+.1f}" if r.n else "-")
        corr = f"{r.corr:.2f}" if r.n else "-"
        comfort = f"{r.comfort:.1f} (n={r.n_rated})" if r.n_rated else "-"
        lines.append(f"{r.condition.label:<11}{decoded:>13}{snr:>14}{corr:>7}  {comfort}")
    still = [s.still_snr_db for s in scores]
    if still:
        lines.append(f"still screen: SNR {_mean(still):+.1f} dB at the cued frequency "
                     f"(no flicker); chance {1 / n_targets:.0%}")
    return lines


def trial_line(s: TrialScore, conditions: Sequence[Condition], freqs: Sequence[float],
               n_trials: int) -> str:
    verdict = "right" if s.hit else f"wrong: {freqs[s.decoded]:g} Hz"
    rating = f", comfort {s.rating}" if s.rating is not None else ""
    return (f"trial {s.index + 1}/{n_trials}  {conditions[s.condition].label} at "
            f"{freqs[s.target]:g} Hz: {verdict}, SNR {s.snr_db:+.1f} dB, r {s.corr:.2f}"
            f"{rating}")


def caveat(scores: Sequence[TrialScore], conditions: Sequence[Condition]) -> str:
    counts = sorted({r.n for r in summarize(scores, conditions)})
    n = "/".join(map(str, counts))
    return (f"{n} trials per condition: differences under about twice the +/- values "
            "(standard errors) are noise, and one trial moves accuracy a lot.")


class Scoreboard:
    """Scores a growing session; says which trials became visible since the last update.

    With ``blind`` (comfort ratings on), a trial is visible only once it is rated.
    """

    def __init__(self, cfg: StimulusConfig, fs: float, ch_names: Sequence[str],
                 window_s: float, latency: float, method: str = "fbcca",
                 blind: bool = False, edge_s: float = 0.0) -> None:
        self.cfg = cfg
        self.fs = fs
        self.ch_names = tuple(ch_names)
        self.window_s = window_s
        self.latency = latency
        self.method = method
        self.blind = blind
        self.edge_s = edge_s
        self.conditions: tuple[Condition, ...] = cfg.conditions
        self.shown: list[TrialScore] = []

    def _ready(self, trials: Sequence[TrialLog], t_end: float) -> int:
        """How many trials could be visible with data up to ``t_end``."""
        due = self.latency + self.window_s + self.edge_s
        return sum(1 for tr in trials
                   if math.isfinite(tr.t_off) and tr.t_off + due <= t_end
                   and (not self.blind or tr.rating is not None))

    def update(self, X: np.ndarray, t: np.ndarray, text: str,
               force: bool = False) -> list[TrialScore]:
        """Re-score when another trial is ready (or ``force``); the newly visible trials."""
        conditions, trials = parse_session(text)
        if conditions:
            self.conditions = conditions
        if t.size == 0 or (not force and self._ready(trials, float(t[-1])) <= len(self.shown)):
            return []
        scores = score_session(X, t, self.fs, self.ch_names, self.cfg.freqs, self.cfg.phases,
                               trials, self.window_s, self.latency, self.method, self.edge_s)
        visible = [s for s in scores if not self.blind or s.rating is not None]
        before = {s.index for s in self.shown}
        self.shown = visible
        return [s for s in visible if s.index not in before]

    def lines(self) -> list[str]:
        return table(self.shown, self.conditions, self.cfg.n_targets)


# -- running a session ----------------------------------------------------------------------

class Tap:
    """A board whose reads are also kept, so the session can be scored while it records."""

    def __init__(self, board: object) -> None:
        self.board = board
        self.fs = board.fs
        self.ch_names = board.ch_names
        self._chunks: list[tuple[np.ndarray, np.ndarray]] = []

    def read(self) -> tuple[np.ndarray, np.ndarray]:
        X, t = self.board.read()
        if t.size:
            self._chunks.append((X, t))
        return X, t

    def data(self) -> tuple[np.ndarray, np.ndarray]:
        """Everything read so far: (X (n_ch, n), t (n,))."""
        if not self._chunks:
            return np.empty((len(self.ch_names), 0)), np.empty(0)
        if len(self._chunks) > 1:
            self._chunks = [(np.concatenate([c[0] for c in self._chunks], axis=1),
                             np.concatenate([c[1] for c in self._chunks]))]
        return self._chunks[0]


def fake_response(cond: Condition) -> float:
    """The fake board's tone amplitude for a condition, relative to full-depth sine."""
    return cond.depth * (SQUARE_GAIN if cond.waveform == "square" else 1.0)


def fake_session(board: FakeBoard, cfg: StimulusConfig, seed: int | None, cue_s: float,
                 stim_s: float, rest_s: float,
                 after_trial: Callable[[np.ndarray, np.ndarray, str], None] | None = None,
                 lead_s: float = 1.0) -> tuple[np.ndarray, np.ndarray, str]:
    """Drive a virtual-time FakeBoard through a mixed-condition session; (X, t, log text).

    The tone's amplitude follows fake_response; ``after_trial(X, t, log)`` sees the data
    and log so far after every trial's rest, as the live scoreboard would.
    """
    base = board.amp
    lines = ["measured refresh: 60.00 Hz (fake)",
             "conditions: " + " ".join(c.spec for c in cfg.conditions)]
    chunks: list[tuple[np.ndarray, np.ndarray]] = []

    def mark(event: str) -> None:
        lines.append(format_marker(board.now, event))

    def data() -> tuple[np.ndarray, np.ndarray]:
        chunks.append(board.read())
        return (np.concatenate([c[0] for c in chunks], axis=1),
                np.concatenate([c[1] for c in chunks]))

    board.read()
    board.set_target(-1)
    board.advance(lead_s)
    order = make_trials(cfg.n_targets, cfg.session.blocks, True, seed, len(cfg.conditions))
    for tr in order:
        mark(f"cond:{tr.condition}")
        mark(f"cue:{tr.target}")
        board.advance(cue_s)
        board.amp = base * fake_response(cfg.conditions[tr.condition])
        mark(f"stim_on:{tr.target}")
        board.set_target(tr.target)
        board.advance(stim_s)
        mark(f"stim_off:{tr.target}")
        board.set_target(-1)
        board.advance(rest_s)
        if after_trial is not None:
            after_trial(*data(), "\n".join(lines) + "\n")
    board.advance(max(lead_s, trailing_s(stim_s, rest_s)))
    board.amp = base
    X, t = data()
    return X, t, "\n".join(lines) + "\n"


def _stimulus_args(args: argparse.Namespace) -> list[str]:
    out = ["-c", str(args.config), "--blocks", str(args.blocks), "--cue-s", f"{args.cue_s:g}",
           "--stim-s", f"{args.stim_s:g}", "--rest-s", f"{args.rest_s:g}",
           "--conditions", args.conditions, "--status", "stdin", "--markers", "unix",
           "--no-wait"]
    if args.rate:
        out.append("--rate")
    if args.ignore_refresh:
        out.append("--ignore-refresh")
    if args.seed is not None:
        out += ["--seed", str(args.seed)]
    return out


def _report(board: Scoreboard, new: Sequence[TrialScore], cfg: StimulusConfig,
            n_trials: int) -> None:
    for s in new:
        print(trial_line(s, board.conditions, cfg.freqs, n_trials), file=sys.stderr,
              flush=True)


def _run_live(args: argparse.Namespace, cfg: StimulusConfig, names: list[str],
              n_trials: int, session_s: float) -> tuple[np.ndarray, np.ndarray, float, str]:
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
        stim = launch_stimulus(_stimulus_args(args), stdin=subprocess.PIPE)
        startup_s = time.monotonic() - launched
        speak(f"About {session_s / 60:.0f} minutes. Look at the square in the red box and "
              "keep still." + (" After each one, press 1 to 5 for how comfortable it was."
                               if args.rate else ""), voice)
        tap = Tap(board)
        scores = Scoreboard(cfg, board.fs, names, args.stim_s - args.latency, args.latency,
                            args.method, blind=args.rate, edge_s=EDGE_S)

        def show(text: str) -> None:
            try:
                stim.proc.stdin.write(json.dumps({"text": text}) + "\n")
                stim.proc.stdin.flush()
            except (OSError, ValueError):   # the stimulus has gone
                pass

        show("scores appear here as trials finish")
        after_exit = trailing_s(args.stim_s, args.rest_s) + EDGE_S
        polled = [0.0]
        exited: list[float] = []

        def stop() -> bool:
            now = time.monotonic()
            if now - polled[0] >= POLL_S:
                polled[0] = now
                log = stim.text()
                X, t = tap.data()
                new = scores.update(X, t, log[:log.rfind("\n") + 1])
                if new:
                    _report(scores, new, cfg, n_trials)
                    show("\n".join(scores.lines()
                                   + [f"{len(scores.shown)} of {n_trials} trials scored"]))
            if stim.proc.poll() is None:
                return False
            if not exited:
                exited.append(now)
            return now - exited[0] >= after_exit

        budget = (startup_s + session_s + TAIL_S + after_exit + END_ALLOW_S
                  + (RATE_ALLOW_S * n_trials if args.rate else 0.0))
        try:
            record(tap, budget, flush=False, stop=stop, keep_on_interrupt=True)
        finally:
            try:
                stim.proc.stdin.close()   # EOF ends the end screen
            except OSError:
                pass
            code = stim.finish(timeout=3.0)
        speak("Done.", voice)
        X, t = tap.data()
        fs = board.fs
    if code != 0:
        print(f"warning: the stimulus exited with code {code}; see {stim.log}", file=sys.stderr)
    return X, t, fs, stim.text()


# -- command line ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.ab", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default="two_targets",
                   help="stimulus config name or .toml path (default two_targets)")
    p.add_argument("--conditions", default=DEFAULT_CONDITIONS,
                   help=f"comma-separated <waveform>:<depth> (default {DEFAULT_CONDITIONS})")
    p.add_argument("--blocks", type=int, default=4,
                   help="blocks; each cues every (condition, target) pair once (default 4)")
    p.add_argument("--cue-s", type=float, default=1.0, help="cue seconds (default 1)")
    p.add_argument("--stim-s", type=float, default=4.0, help="flicker seconds (default 4)")
    p.add_argument("--rest-s", type=float, default=4.0,
                   help="still seconds (default 4, so the still window ends with the rest)")
    p.add_argument("--latency", type=float, default=0.14,
                   help="visual response delay in s; windows start this long after a marker")
    p.add_argument("--method", choices=METHODS, default="fbcca",
                   help="classifier for 'decoded' (default fbcca)")
    p.add_argument("--rate", action="store_true",
                   help="after each trial's rest, ask for a 1-5 comfort rating (keys 1-5)")
    p.add_argument("--warmup", type=float, default=5.0,
                   help="seconds recorded and discarded before the stimulus starts (default 5)")
    p.add_argument("--names", default=",".join(MONTAGE),
                   help="comma-separated names of the 8 board channels, in order")
    p.add_argument("--save-dir", type=Path, default=None,
                   help="output directory (default: recordings_dir(); with --fake, "
                        "fake_dir())")
    p.add_argument("--seed", type=int, help="trial order seed (default: random)")
    p.add_argument("--port", default=None, help="serial port (default: auto-detect)")
    p.add_argument("--no-voice", action="store_true", help="no spoken prompts")
    p.add_argument("--verbose", action="store_true", help="show BrainFlow's log messages")
    p.add_argument("--ignore-refresh", action="store_true",
                   help="let the stimulus run when its measured refresh is not 60 Hz")
    p.add_argument("--fake", action="store_true",
                   help="FakeBoard and synthetic markers: no board, no display, instant")
    p.add_argument("--score", type=Path, metavar="NPZ",
                   help="score a saved A/B session (_cued.npz) instead of recording one")
    return p


def _check_args(args: argparse.Namespace) -> list[str]:
    bad = []
    if args.blocks < 1:
        bad.append(f"--blocks must be >= 1, got {args.blocks}")
    if args.stim_s <= 0:
        bad.append(f"--stim-s must be positive, got {args.stim_s}")
    if args.cue_s < 0 or args.rest_s < 0:
        bad.append(f"--cue-s and --rest-s must be >= 0, got {args.cue_s}, {args.rest_s}")
    if args.latency < 0 or args.latency >= args.stim_s:
        bad.append(f"--latency must lie in [0, --stim-s), got {args.latency}")
    if args.warmup < 0:
        bad.append(f"--warmup must be >= 0, got {args.warmup}")
    window = args.stim_s - args.latency
    if args.stim_s > 0 and segment_problem(window, FS):
        bad.append(f"the {window:g} s window (--stim-s - --latency) is too short to measure "
                   f"an SNR within {SNR_SPAN_HZ:g} Hz of the target; use --stim-s of at "
                   f"least {(SNR_GUARD_BINS + 0.5) / SNR_SPAN_HZ + args.latency:g}")
    if args.cue_s + args.rest_s < window - 1e-9:
        bad.append(f"--cue-s + --rest-s = {args.cue_s + args.rest_s:g} s is shorter than the "
                   f"{window:g} s window (--stim-s - --latency), so the still window would "
                   "overlap the next flicker")
    if args.rate and args.fake:
        bad.append("--rate needs the display; --fake has none")
    return bad


def _print_result(scores: Sequence[TrialScore], conditions: Sequence[Condition],
                  n_targets: int) -> None:
    print("\n".join(table(scores, conditions, n_targets)))
    if scores:
        print(caveat(scores, conditions))


def _score_file(path: Path, method: str) -> int:
    try:
        rec = load_recording(path)
        conditions, trials = parse_session(rec.marker_text)
    except (OSError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not conditions:
        print(f"error: {path} has no 'conditions:' line; it is not an A/B session",
              file=sys.stderr)
        return 1
    scores = score_session(rec.X, rec.t, rec.fs, rec.ch_names, rec.freqs, rec.phases, trials,
                           rec.window_s, rec.latency, method)
    print(f"{path.name}: {len(scores)} of {len(trials)} trials scored, "
          f"{', '.join(f'{f:g}' for f in rec.freqs)} Hz, {method}")
    _print_result(scores, conditions, rec.n_targets)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.score is not None:
        return _score_file(args.score, args.method)
    problems = _check_args(args)
    names = [n.strip() for n in args.names.split(",") if n.strip()]
    if len(names) != 8 or len(set(names)) != len(names):
        problems.append(f"--names needs 8 different channel names, got {names}")
    try:
        cfg = override(load_config(args.config), blocks=args.blocks, cue_s=args.cue_s,
                       stim_s=args.stim_s, rest_s=args.rest_s,
                       conditions=parse_conditions(args.conditions))
    except (ValueError, FileNotFoundError) as exc:
        problems.append(str(exc))
        cfg = None
    for msg in problems:
        print(f"error: {msg}", file=sys.stderr)
    if problems or cfg is None:
        return 2
    for w in config_warnings(cfg):
        print(f"warning: {w}", file=sys.stderr)

    n_trials = args.blocks * cfg.n_targets * len(cfg.conditions)
    session_s = n_trials * (args.cue_s + args.stim_s + args.rest_s)
    window_s = args.stim_s - args.latency
    print(f"{cfg.name}: {n_trials} trials ({args.blocks} block(s) x {cfg.n_targets} target(s) "
          f"x {len(cfg.conditions)} condition(s): "
          f"{', '.join(c.label for c in cfg.conditions)}), about {session_s / 60:.1f} min"
          + (" plus ratings" if args.rate else ""), file=sys.stderr)

    save_dir = args.save_dir or (fake_dir() if args.fake else recordings_dir())
    reason = unwritable_reason(save_dir)
    if reason:
        print(f"error: cannot save into {save_dir} ({reason}); pass a writable --save-dir",
              file=sys.stderr)
        return 2

    start = time.time()
    if args.fake:
        board = FakeBoard(cfg.freqs, ch_names=names, seed=0 if args.seed is None else args.seed,
                          t0=float(math.floor(start)))
        if args.warmup > 0:
            board.advance(args.warmup)
        live = Scoreboard(cfg, board.fs, names, window_s, args.latency, args.method)

        def after_trial(X: np.ndarray, t: np.ndarray, log: str) -> None:
            _report(live, live.update(X, t, log), cfg, n_trials)

        X, t, log_text = fake_session(board, cfg, args.seed, args.cue_s, args.stim_s,
                                      args.rest_s, after_trial)
        fs = board.fs
    else:
        try:
            X, t, fs, log_text = _run_live(args, cfg, names, n_trials, session_s)
        except (RuntimeError, ImportError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("interrupted before the session started; nothing recorded", file=sys.stderr)
            return 130
    if X.shape[1] == 0:
        print("error: no data arrived from the board; nothing saved", file=sys.stderr)
        return 1

    conditions, trials = parse_session(log_text)
    scores = score_session(X, t, fs, names, cfg.freqs, cfg.phases, trials, window_s,
                           args.latency, args.method)
    print(f"{len(scores)} of {n_trials} trials scored ({args.method})")
    _print_result(scores, conditions or cfg.conditions, cfg.n_targets)
    path = _save(save_dir, start, "cued", X=X, t=t, fs=fs, ch_names=names, freqs=cfg.freqs,
                 markers=log_text, window_s=window_s, latency=args.latency, phases=cfg.phases)
    if path is None:
        print("error: the recording could not be saved anywhere", file=sys.stderr)
        return 1
    print(path)
    print(f"score it again with: {sys.executable or 'python'} -m brainsync.ab --score {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
