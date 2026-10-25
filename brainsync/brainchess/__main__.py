"""BrainChess: play a whole game of chess by looking at the flickering board.

    python -m brainsync.brainchess                                # Cerelog X8 + board stimulus
    python -m brainsync.brainchess --fake --fast --no-stimulus    # simulated user, virtual time
    python -m brainsync.brainchess --paradigm freq                # the 15 / 17 Hz version

c-VEP (the default, the BrainSync standard of docs/CVEP-STANDARD.md): three targets show one
63-bit m-sequence, white and black, 21 frames (350 ms) apart. Every choice is drawn on the
board itself: the candidate squares are cut in two by one straight line, the squares of
group A all show target 0's code and those of group B target 1's, and the user keeps
looking at the piece they want until it is the only one left; then the same for its
destination, the promotion piece if any (two side panels), and PLAY / RESTART on the side
panels (--no-confirm skips it). A third panel, BACK (target 2), flickers at every step and
undoes the last pick, so a wrong decode costs one pick, not the move. Before the game the
decoder trains itself: --train-cycles code cycles (30, about 32 s) with the cue box on
panel A, then its held-out accuracy (chance 33 %); --template reuses a saved template
(--save-template keeps one). Each code cycle (1.05 s) gives a decision from the mean of
the last --cycles cycles, whose score per target (a correlation) is shown as a bar in the
side column with the lead and the choice; --votes agreeing decisions make a pick. After
every screen change the decoder starts afresh, and only cycles that began --cooldown s
after it count. The stimulus is run with Unix markers and this program reads its
cycle:<n> markers from its log.

f-VEP (--paradigm freq): only two frequencies decode reliably on dry electrodes (15 Hz =
target 0, 17 Hz = target 1), so every choice is a binary pick with no Back target, the
confirm panels are PLAY / BACK and BACK restarts the move. Decisions pass the fresh-window
gate (--cooldown, --votes; see brainsync.brainchess.gate). Instead of training it
calibrates the rest baseline on the still board (--calibrate s, as in brainsync.demo).

The opponent (Stockfish if found, else a built-in heuristic) replies while the board stands
still for --hold-s seconds with its move drawn as an arrow. When the game ends (or
--max-moves is reached) the final position and result stay on the board for --end-s
seconds. With --fake a simulated user picks the moves it intends (the heuristic engine with
its own seed) and points the fake board at the right target after every screen change
(c-VEP: vep.live.standard_fake_board, which also hands out the cycle markers); wrong decodes
make it look at BACK (c-VEP) or pick BACK at the confirm step (f-VEP). The run ends when the
game ends, after --max-moves user moves, after --seconds, or on Escape, Ctrl-C, a silent
board or a crashed stimulus. One status line per decision goes to stderr; the summary and
the PGN (also written to --pgn) go to stdout. Exit codes are the demo's: 0 for a run that
ended normally (game over, limits, Escape, Ctrl-C), 1 when the board stalled, the stimulus
failed or another runtime error occurred, 2 for bad arguments or setup, 130 when
interrupted before the game started.
"""
from __future__ import annotations

import argparse
import math
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import chess
import numpy as np

from ..board import CerelogBoard, FakeBoard
from ..classify import ABSTAIN, make
from ..demo import CALIBRATE_S, POLL_S, STALL_S, _channels, _names
from ..launch import Stimulus, launch_stimulus, speak
from ..online import TUNED, Decision, Decoder, calibrate, threshold_arg
from ..recordings import MONTAGE, POSTERIOR, Event, parse_events
from ..stimulus import load_config
from ..stimulus.config import config_warnings
from ..vep.cvep import Template
from ..vep.live import CodeVepDecoder, standard_fake_board
from .gate import CodeGate, MoveGate
from .game import (GROUP_NAMES, ChessSession, NullDisplay, SimulatedUser,
                   StimulusDisplay, game_outcome)
from .prior import Engine, find_stockfish
from .trace import TraceFeed

STIMULUS_MODULE = "brainsync.brainchess.stimulus"
PARADIGMS = ("code", "freq")
DEFAULT_CONFIGS = {"code": "chess_cvep", "freq": "chess"}
DEFAULT_CONFIG = DEFAULT_CONFIGS["code"]
CODE_VOTES = 2        # agreeing c-VEP decisions per pick (docs/CVEP-STANDARD.md, section 2)
CODE_CYCLES = 2       # cycles averaged per decision
TRAIN_CYCLES = 30
STILL_PROMPT = "Keep still and look at the middle of the board"
TRAIN_PROMPT = f"Look at the {GROUP_NAMES[0]} square and keep looking at it."
FIRST_PROMPT = "Look at the piece you want to move."
# the simulated user's engine seed is offset from --seed so it does not mirror the opponent
USER_SEED_OFFSET = 1000


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.brainchess", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--paradigm", choices=PARADIGMS, default="code",
                   help="code: c-VEP, three targets with Back (default); freq: 15 / 17 Hz")
    p.add_argument("-c", "--config", default=None,
                   help="board stimulus config name or .toml path (default "
                        f"{DEFAULT_CONFIGS['code']} for code, {DEFAULT_CONFIGS['freq']} "
                        "for freq)")
    p.add_argument("--votes", type=int, default=None,
                   help=f"agreeing decisions needed for a pick (default {CODE_VOTES} for code, "
                        f"{TUNED.votes} consecutive fresh windows for freq)")
    p.add_argument("--margin", type=float, default=None,
                   help="abstain unless the best target leads by this much (code: in "
                        "correlation, default 0; freq: in rest spreads, default "
                        f"{TUNED.margin:g})")
    p.add_argument("--channels", type=_channels, default=list(POSTERIOR),
                   help="analysis channels, names or indices (default " + ",".join(POSTERIOR) + ")")
    p.add_argument("--seconds", type=float, default=0.0,
                   help="cap on the playing time in seconds (default 0: no cap)")
    p.add_argument("--names", type=_names, default=list(MONTAGE),
                   help="comma-separated names of the 8 board channels, in order")
    p.add_argument("--port", default=None, help="serial port (default: auto-detect)")
    p.add_argument("--verbose", action="store_true", help="show BrainFlow's log messages")
    code = p.add_argument_group("c-VEP (--paradigm code)")
    code.add_argument("--train-cycles", type=int, default=TRAIN_CYCLES,
                      help=f"code cycles recorded on panel {GROUP_NAMES[0]} for the template "
                           f"(default {TRAIN_CYCLES}, about 32 s)")
    code.add_argument("--cycles", type=int, default=CODE_CYCLES,
                      help=f"cycles averaged per decision (default {CODE_CYCLES})")
    code.add_argument("--template", metavar="PATH", default=None,
                      help="decode with this saved template instead of training")
    code.add_argument("--save-template", metavar="PATH", default=None,
                      help="also write the trained template here (reuse with --template)")
    freq = p.add_argument_group("f-VEP (--paradigm freq)")
    freq.add_argument("--classifier", default=TUNED.classifier, choices=("cca", "fbcca"),
                      help=f"default {TUNED.classifier}")
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
                      help=f"still-board seconds for the rest baseline (default {CALIBRATE_S:g})")
    game = p.add_argument_group("game")
    game.add_argument("--fen", default=None, help="start from this position (default: the "
                                                  "initial position)")
    game.add_argument("--color", choices=("white", "black"), default="white",
                      help="the side you play (default white; black puts Black at the bottom "
                           "and the opponent moves first)")
    game.add_argument("--stockfish", metavar="PATH", default=None,
                      help="Stockfish binary (default: $BRAINCHESS_STOCKFISH, PATH, Homebrew; "
                           "the built-in heuristic when none is found)")
    game.add_argument("--skill", type=int, default=3,
                      help="Stockfish skill level 0..20 (default 3)")
    game.add_argument("--think-s", type=float, default=0.2,
                      help="Stockfish seconds per analysis and per reply (default 0.2)")
    game.add_argument("--hold-s", type=float, default=2.0,
                      help="seconds the board stands still after each opponent move (default 2)")
    game.add_argument("--end-s", type=float, default=5.0,
                      help="seconds the final position stays on the board after the game ends "
                           "(default 5; Escape closes it sooner)")
    game.add_argument("--cooldown", type=float, default=0.5,
                      help="seconds after a screen change before the EEG counts: the eyes "
                           "settle on the new split (default 0.5; code: cycles that begin "
                           "sooner are dropped, which with the default is the first one)")
    game.add_argument("--no-confirm", action="store_true",
                      help="play a move as soon as it is chosen, without the PLAY step")
    game.add_argument("--max-moves", type=int, default=0,
                      help="stop after this many of your moves (default 0: play to the end)")
    game.add_argument("--pgn", metavar="PATH", default=None,
                      help="also write the game to this PGN file")
    fake = p.add_argument_group("synthetic board")
    fake.add_argument("--fake", action="store_true",
                      help="a synthetic board and a simulated user instead of the Cerelog X8")
    fake.add_argument("--fast", action="store_true",
                      help="with --fake: virtual time, as fast as the decoder runs")
    fake.add_argument("--seed", type=int, default=0,
                      help="the heuristic opponent's seed, and with --fake the noise and the "
                           "simulated user (default 0)")
    p.add_argument("--no-stimulus", action="store_true", help="do not start the board window")
    p.add_argument("--ignore-refresh", action="store_true",
                   help="let the stimulus run when its measured refresh is not 60 Hz "
                        "(it refuses by default)")
    p.add_argument("--no-voice", action="store_true", help="no spoken prompts")
    return p


def _check(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.config is None:
        args.config = DEFAULT_CONFIGS[args.paradigm]
    code = args.paradigm == "code"
    if args.votes is None:
        args.votes = CODE_VOTES if code else TUNED.votes
    if args.margin is None:
        args.margin = 0.0 if code else TUNED.margin
    if args.fast and not args.fake:
        p.error("--fast needs --fake")
    if code and args.no_stimulus and not args.fake:
        p.error("--paradigm code needs the stimulus for its cycle markers; --no-stimulus "
                "works only with --fake")
    for flag, value in (("--calibrate", args.calibrate), ("--window", args.window),
                        ("--step", args.step), ("--think-s", args.think_s)):
        if not value > 0:
            p.error(f"{flag} must be positive, got {value}")
    for flag, value in (("--seconds", args.seconds), ("--hold-s", args.hold_s),
                        ("--end-s", args.end_s), ("--cooldown", args.cooldown)):
        if not (math.isfinite(value) and value >= 0):
            p.error(f"{flag} must be >= 0, got {value}")
    if args.votes < 1:
        p.error(f"--votes must be >= 1, got {args.votes}")
    if args.margin < 0 or args.pad < 0:
        p.error(f"--margin and --pad must be >= 0, got {args.margin}, {args.pad}")
    if args.train_cycles < 2:
        p.error(f"--train-cycles must be >= 2 (leave-one-out needs two), got "
                f"{args.train_cycles}")
    if args.cycles < 1:
        p.error(f"--cycles must be >= 1, got {args.cycles}")
    if not 0 <= args.skill <= 20:
        p.error(f"--skill must be in 0..20, got {args.skill}")
    if args.max_moves < 0:
        p.error(f"--max-moves must be >= 0, got {args.max_moves}")
    if args.seed < 0:   # numpy refuses negative seeds
        p.error(f"--seed must be >= 0, got {args.seed}")
    if len(args.names) != len(MONTAGE):
        p.error(f"--names needs {len(MONTAGE)} names, got {len(args.names)}: {args.names}")


def stimulus_args(args: argparse.Namespace) -> list[str]:
    """Command-line arguments for the board stimulus (c-VEP: Unix markers for the cycles)."""
    markers = "unix" if args.paradigm == "code" else "none"
    out = ["-c", str(args.config), "--markers", markers]
    if args.color == "black":
        out.append("--flip")
    if args.ignore_refresh:
        out.append("--ignore-refresh")
    return out


def _target_name(k: int, freqs: tuple[float, ...]) -> str:
    return "-" if k == ABSTAIN else f"{freqs[k]:g}"


def _status(d: Decision, elapsed: float, freqs: tuple[float, ...], session: ChessSession,
            stale: bool, fired: int, truth: int | None) -> str:
    gate = session.gate
    assert gate is not None
    scores = "  ".join(f"{f:g} {s:+.3f}" for f, s in zip(freqs, d.scores))
    line = (f"{elapsed:7.2f} s  {scores}  lead {d.gap:4.1f}  level {d.level:+4.1f}  "
            f"window {_target_name(d.raw, freqs):>2}  ")
    if fired != ABSTAIN:
        line += f"pick {_target_name(fired, freqs)}"
    elif session.phase != "user":
        line += "board still"
    elif stale:
        line += "stale (settling)"
    else:
        line += f"streak {_target_name(gate.pending, freqs):>2} {gate.streak}/{gate.votes}"
    if truth is not None and session.phase == "user":
        line += f"  (looking at {_target_name(truth, freqs)})"
    return line


def _code_name(k: int, labels: Sequence[str]) -> str:
    return "-" if k == ABSTAIN or not 0 <= k < len(labels) else labels[k]


def _code_status(d: Decision, elapsed: float, labels: Sequence[str], session: ChessSession,
                 stale: bool, fired: int, truth: int | None) -> str:
    """One stderr line per c-VEP decision: the score per target, lead, raw and the gate."""
    scores = "  ".join(f"{lab} {s:+.3f}" for lab, s in zip(labels, d.scores))
    gap = "  -  " if not np.isfinite(d.gap) else f"{d.gap:.3f}"
    line = (f"{elapsed:7.2f} s  {scores}  lead {gap}  raw {_code_name(d.raw, labels)}  ")
    if fired != ABSTAIN:
        line += f"pick {_code_name(fired, labels)}"
    elif session.phase != "user":
        line += "board still"
    elif stale:
        line += "stale (settling)"
    else:
        line += f"committed {_code_name(d.committed, labels)}"
    if truth is not None and session.phase == "user":
        line += f"  (looking at {_code_name(truth, labels)})"
    return line


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
        return parse_events("\n".join(lines), kinds=("cycle",))


class CodeFeed:
    """What ``play`` feeds for c-VEP: each board read with the display's cycle events since
    the last one, the events filtered by the gate (cycles of the settled current screen).
    ``n_seen`` counts samples read, the trace strip's stream clock; ``fs`` the board's."""

    def __init__(self, gate: CodeGate, events: Callable[[], list[Event]]) -> None:
        self.gate = gate
        self.events = events
        self.fs = gate.decoder.fs
        self.n_seen = 0

    def feed(self, X: np.ndarray, t: np.ndarray) -> list[Decision]:
        self.n_seen += int(np.asarray(t).size)
        self.gate.observe(t)
        return self.gate.decoder.feed(X, t, self.gate.fresh(self.events()))


def play(session: ChessSession, decoder: Any, board: Any, seconds: float,
         on_decision: Callable[[Decision], None], sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic, stop: Callable[[], str | None] | None = None,
         poll_s: float = POLL_S, stall_s: float = STALL_S,
         on_read: Callable[[np.ndarray], None] | None = None) -> str:
    """Decode and play until the session ends; returns why the run ended.

    ``decoder`` is a ``Decoder`` or a ``CodeFeed`` (``feed(X, t)``, ``n_seen``, ``fs``).
    ``seconds`` > 0 caps the data fed; ``stop()`` returning a reason ends the run with it;
    a display that stopped accepting lines ends it ("stimulus pipe closed"); no samples
    for ``stall_s`` by ``clock`` ends it with "board stalled". ``on_read(X)`` sees every
    non-empty read once the decoder has been fed it (the trace strip's EEG lanes).
    """
    limit = None if seconds <= 0 else decoder.n_seen + int(round(seconds * decoder.fs))
    heard = clock()
    while True:
        if session.over:
            return session.ended
        if not session.display_ok:
            return "stimulus pipe closed"
        if limit is not None and decoder.n_seen >= limit:
            return "time up"
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
        decisions = decoder.feed(X, t)
        if on_read is not None and X.shape[1]:
            on_read(X)
        for d in decisions:
            on_decision(d)
        session.tick()


def train(session: ChessSession, feed: CodeFeed, board: Any,
          sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic, stop: Callable[[], str | None] | None = None,
          poll_s: float = POLL_S, stall_s: float = STALL_S,
          on_read: Callable[[np.ndarray], None] | None = None) -> str | None:
    """Run the c-VEP training screen until the decoder has its template.

    Returns None once trained, else why it stopped ("board stalled", "stimulus pipe
    closed", or ``stop()``'s reason). Raises RuntimeError when the display sends no cycle
    markers for a few cycles (a stimulus without --markers unix, or not flickering).
    """
    dec: CodeVepDecoder = feed.gate.decoder
    cycle_s = dec.n_samples / dec.fs
    timeout = (dec.train_cycles + 5) * cycle_s + 10.0
    began = heard = clock()
    shown = -1
    while dec.training:
        done, of = dec.progress
        if done != shown:
            session.show_training(done, of)
            shown = done
        if not session.display_ok:
            return "stimulus pipe closed"
        reason = stop() if stop is not None else None
        if reason:
            return reason
        now = clock()
        if now - began > timeout:
            raise RuntimeError(f"saw {done} of {of} training cycles in {timeout:.0f} s; is the "
                               "stimulus flickering and printing cycle markers?")
        sleep(poll_s)
        X, t = board.read()
        now = clock()
        if X.shape[1]:
            heard = now
        elif now - heard >= stall_s:
            return "board stalled"
        feed.n_seen += int(np.asarray(t).size)
        feed.gate.observe(t)
        dec.feed(X, t, [e for e in feed.events() if e[1] == "cycle"])
        if on_read is not None and X.shape[1]:
            on_read(X)
    session.trained(dec.train_accuracy, dec.chance)
    return None


def linger(seconds: float, stop: Callable[[], str | None],
           sleep: Callable[[float], None] = time.sleep,
           clock: Callable[[], float] = time.monotonic, poll_s: float = POLL_S) -> str | None:
    """Keep the final screen up for ``seconds`` before the stimulus is closed.

    Closing the stimulus's stdin ends it within a frame, so without this the last move
    and the result would never be seen. ``stop()`` returning a reason (Escape, a closed
    stimulus) ends the wait early; that reason is returned, else None.
    """
    until = clock() + seconds
    while clock() < until:
        reason = stop()
        if reason:
            return reason
        sleep(min(poll_s, max(0.0, until - clock())))
    return None


def resolve_stockfish(path: str | None) -> str | None:
    """The Stockfish binary for ``--stockfish`` (a path or a command on PATH), else found.

    Raises ValueError when an explicit ``path`` is neither an executable file nor a
    command on PATH.
    """
    if not path:
        return find_stockfish(None)
    explicit = shutil.which(path) or path
    found = find_stockfish(explicit)
    if found != explicit:
        raise ValueError(f"--stockfish {path} is not an executable file")
    return found


def position_problem(board: chess.Board) -> str | None:
    """Why ``board`` cannot start a game (illegal or already over), or None."""
    status = board.status()
    if status != chess.STATUS_VALID:
        flags = [f.name.lower().replace("_", " ") for f in chess.Status if f and f in status]
        return f"the --fen position is not legal ({', '.join(flags)})"
    outcome = game_outcome(board)
    if outcome is not None:
        return f"the --fen position is already over ({outcome.result()})"
    return None


def _close(stim: Stimulus | None, display: Any) -> int | None:
    """End the stimulus (EOF on its stdin ends it); its exit code."""
    if display is not None:
        display.close()
    if stim is None:
        return None
    try:
        if stim.proc.stdin is not None:
            stim.proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    return stim.finish(timeout=5.0)


def _escaped(stim: Stimulus | None, code: int | None) -> bool:
    """True when the board stimulus ended because the user pressed Escape."""
    if stim is None or code != 1:
        return False
    try:
        return any(ln.strip() == "aborted" for ln in stim.text().splitlines())
    except OSError:
        return False


def _build(args: argparse.Namespace) -> tuple[Any, Any, Any, Any, str]:
    """(config, board, decoder, gate, description) for the chosen paradigm; raises the
    setup errors main() turns into exit code 2."""
    cfg = load_config(args.config)
    if args.paradigm == "code":
        if cfg.paradigm != "code" or cfg.n_targets != 3:
            raise ValueError(f"--paradigm code needs a three-target [code] config (c-VEP, "
                             f"e.g. {DEFAULT_CONFIGS['code']}); {cfg.name!r} is a "
                             f"{cfg.n_targets}-target {cfg.paradigm} config")
        if args.fake:
            eeg: Any = standard_fake_board(fixated=-1, seed=args.seed,
                                           clock=None if args.fast else time.time,
                                           refresh=cfg.refresh)
        else:
            eeg = CerelogBoard(serial_port=args.port, ch_names=args.names, verbose=args.verbose)
        template = Template.load(args.template) if args.template else None
        decoder: Any = CodeVepDecoder(eeg.fs, n_targets=3, lag_frames=cfg.code.lag_frames,
                                      cycle_frames=cfg.code.cycle_frames, refresh=cfg.refresh,
                                      channels=args.channels, ch_names=args.names,
                                      votes=args.votes, cycles=args.cycles, margin=args.margin,
                                      train_cycles=args.train_cycles, template=template)
        now: Callable[[], float] = (lambda: eeg.now) if args.fake else time.time
        gate: Any = CodeGate(decoder, cooldown_s=args.cooldown, clock=now)
        names = ",".join(decoder.ch_names[i] for i in decoder.rows)
        about = (f"c-VEP {cfg.name}: 3 targets, {cfg.code.cycle_frames}-frame cycles "
                 f"{cfg.code.lag_frames} frames apart; {args.cycles} cycles per decision, "
                 f"{args.votes} votes, cooldown {args.cooldown:g} s; channels {names}; "
                 + (f"template {args.template}" if template is not None
                    else f"training on {args.train_cycles} cycles"))
        return cfg, eeg, decoder, gate, about
    if cfg.n_targets != 2:
        raise ValueError(f"--paradigm freq needs a two-target config (15 and 17 Hz); "
                         f"{cfg.name!r} has {cfg.n_targets}")
    if cfg.paradigm != "freq":
        raise ValueError(f"--paradigm freq needs a frequency config; {cfg.name!r} is "
                         f"{cfg.paradigm}")
    if args.fake:
        eeg = FakeBoard(cfg.freqs, seed=args.seed, clock=None if args.fast else time.time,
                        ch_names=args.names)
    else:
        eeg = CerelogBoard(serial_port=args.port, ch_names=args.names, verbose=args.verbose)
    clf = make(args.classifier, freqs=cfg.freqs, fs=eeg.fs, harmonics=args.harmonics,
               phases=cfg.phases, margin=args.margin, threshold=args.threshold)
    decoder = Decoder(clf, eeg.fs, window_s=args.window, step_s=args.step,
                      votes=args.votes, channels=args.channels, pad_s=args.pad,
                      ch_names=args.names)
    gate = MoveGate(decoder, cooldown_s=args.cooldown)
    about = (f"{clf!r}; channels {','.join(decoder.channel_names)}; window {args.window:g} s, "
             f"step {args.step:g} s, {args.votes} votes, cooldown {args.cooldown:g} s")
    return cfg, eeg, decoder, gate, about


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    _check(p, args)
    voice = not args.no_voice
    code = args.paradigm == "code"

    def say(text: str) -> None:
        speak(text, voice)

    if voice:
        # the fixed prompts, synthesised while the board connects (cached on disk after)
        from .. import voice as tts
        tts.prefetch([TRAIN_PROMPT if code else STILL_PROMPT + ".", FIRST_PROMPT, "Back",
                      "Only one move"] + (["Restart", "Nothing to undo"] if code else []))

    try:
        position = chess.Board(args.fen) if args.fen else chess.Board()
    except ValueError as exc:
        print(f"error: bad --fen: {exc}", file=sys.stderr)
        return 2
    problem = position_problem(position)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    user_color = chess.WHITE if args.color == "white" else chess.BLACK
    try:
        engine_path = resolve_stockfish(args.stockfish)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        cfg, eeg, decoder, gate, about = _build(args)
    except (ValueError, KeyError, FileNotFoundError, ImportError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    freqs = () if code else tuple(float(f) for f in cfg.freqs)
    fast = args.fake and args.fast
    sleep: Callable[[float], None] = eeg.advance if fast else time.sleep
    clock: Callable[[], float] = (lambda: eeg.elapsed) if fast else time.monotonic
    for w in config_warnings(cfg):
        print(f"warning: {w}", file=sys.stderr)
    print(f"{'fake board' if args.fake else 'Cerelog X8'} at {eeg.fs:g} Hz; {about}",
          file=sys.stderr)
    if not code and args.calibrate < CALIBRATE_S:
        print(f"warning: a {args.calibrate:g} s baseline is noisy; rest decisions may lean "
              f"towards one target (default {CALIBRATE_S:g} s)", file=sys.stderr)

    stim: Stimulus | None = None
    display: Any = None
    session: ChessSession | None = None
    tmp: tempfile.TemporaryDirectory | None = None
    ended = "not started"
    exit_code = 0
    picks_true = picks_checked = 0
    try:
        with Engine(engine_path, skill=args.skill, think_s=args.think_s, seed=args.seed) as engine:
            print(f"opponent: {engine.name}; you play {args.color}"
                  + (f" from {position.fen()}" if args.fen else ""), file=sys.stderr)
            user = (SimulatedUser(Engine(None, seed=args.seed + USER_SEED_OFFSET))
                    if args.fake else None)

            def truth() -> int | None:
                if not args.fake:
                    return None
                return eeg.fixated if code else eeg.active_target()

            def look(s: ChessSession) -> None:
                if code:
                    # the fake board's code restarts with every screen, as the stimulus's
                    if s.holding:
                        eeg.stop_flicker()
                    else:
                        eeg.start_flicker()
                eeg.set_target(user.look(s))

            if not args.fake:
                print("connecting to the Cerelog X8 (can take 15 s)...", file=sys.stderr)
            with eeg:
                if args.no_stimulus:
                    display = NullDisplay()
                else:
                    tmp = tempfile.TemporaryDirectory(prefix="brainchess_pieces_")
                    print("starting the board stimulus...", file=sys.stderr)
                    sargs = stimulus_args(args)
                    stim = launch_stimulus(sargs, stdin=subprocess.PIPE,
                                           command=[sys.executable, "-m", STIMULUS_MODULE, *sargs])
                    display = StimulusDisplay(stim.proc.stdin)
                session = ChessSession(position, engine, display, user_color=user_color,
                                       gate=gate, confirm=not args.no_confirm,
                                       hold_s=args.hold_s, max_moves=args.max_moves,
                                       clock=clock, speak=say,
                                       pieces_dir=None if tmp is None else tmp.name,
                                       on_screen=look if user is not None else None, code=code)
                rows = decoder.rows
                display.lanes([args.names[i] for i in rows] if code else decoder.channel_names)
                trace = TraceFeed(rows, eeg.fs)
                if code:
                    if args.fake:
                        events: Callable[[], list[Event]] = eeg.read_events
                    else:
                        assert stim is not None
                        events = LogTail(stim.log).events
                    feed: Any = CodeFeed(gate, events)
                else:
                    feed = decoder

                def on_read(X: np.ndarray) -> None:
                    shown = trace.push(X, feed.n_seen)
                    if shown is not None:
                        display.eeg(shown[0], trace.dt, shown[1])

                def stimulus_gone() -> str | None:
                    if stim is not None and stim.proc.poll() is not None:
                        return f"stimulus exited (code {stim.proc.returncode})"
                    return None

                gone: str | None = None
                if code:
                    if decoder.training:
                        say(TRAIN_PROMPT)
                        if args.fake:
                            eeg.set_target(0)
                            eeg.start_flicker()
                        cycle_s = decoder.n_samples / decoder.fs
                        print(f"training: {args.train_cycles} cycles of {cycle_s:.2f} s on "
                              f"panel {GROUP_NAMES[0]}...", file=sys.stderr)
                        gone = train(session, feed, eeg, sleep=sleep, clock=clock,
                                     stop=stimulus_gone, on_read=on_read)
                        if gone is None:
                            print(f"template from {decoder.template.n_cycles} cycles, "
                                  f"leave-one-out accuracy {100 * decoder.train_accuracy:.0f} % "
                                  f"(chance {100 * decoder.chance:.0f} %)", file=sys.stderr)
                            if args.save_template:
                                path = decoder.template.save(args.save_template)
                                print(f"template saved to {path}", file=sys.stderr)
                    else:
                        session.show_still("Template loaded")
                    if args.fake:
                        eeg.stop_flicker()
                        eeg.set_target(ABSTAIN)
                else:
                    # the baseline is fit on the still board the user will see between picks
                    session.show_still(STILL_PROMPT)
                    say(STILL_PROMPT + ".")
                    if args.fake:
                        eeg.set_target(ABSTAIN)
                    cal = calibrate(decoder, eeg, args.calibrate,
                                    sleep=eeg.advance if fast else None)
                    print(f"baseline from {cal.n_windows} still windows ({cal.n_rejected} "
                          "rejected): " + " ".join(f"{f:g} {m:.4f}" for f, m in zip(freqs, cal.mean))
                          + f", spread {cal.spread:.4f}", file=sys.stderr)

                gone = gone or stimulus_gone() or (None if session.display_ok
                                                   else "stimulus pipe closed")
                if gone == "board stalled":
                    print(f"error: the board sent nothing for {STALL_S:g} s", file=sys.stderr)
                    exit_code = 1
                if gone:
                    ended = f"{gone} before the game started"
                else:
                    session.prefetch()   # Stockfish's first analysis, before the drop below
                    if code:
                        feed.feed(*eeg.read())   # drop what arrived during the prompt;
                        gate.screen_changed()    # the game's first screen resets anyway
                    else:
                        eeg.read()   # drop what arrived during the prompt
                    began = feed.n_seen / feed.fs
                    ended = "started"
                    say(FIRST_PROMPT)
                    session.start()
                    labels_at = session.target_labels

                    def on_decision(d: Decision) -> None:
                        nonlocal picks_true, picks_checked
                        looked = truth()
                        stale = gate.stale
                        labels = labels_at()
                        before = len(session.picks)
                        fired = session.push(d)
                        elapsed = gate.stream_time(d) - began
                        if code:
                            line = _code_status(d, elapsed, labels, session, gate.stale > stale,
                                                fired, looked)
                        else:
                            line = _status(d, elapsed, freqs, session, gate.stale > stale,
                                           fired, looked)
                        print(line, file=sys.stderr, flush=True)
                        if fired == ABSTAIN:
                            return
                        pick = session.picks[before]
                        if looked is not None:
                            picks_checked += 1
                            picks_true += fired == looked
                        now = session.selector.prompt() if session.selector else session.status()
                        what = _code_name(fired, labels) if code else \
                            f"{_target_name(fired, freqs)} Hz"
                        print(f"  pick: move {pick.move_no} {pick.stage} -> {what} of "
                              f"{pick.candidates} candidates at {pick.elapsed:.1f} s; now: {now}",
                              file=sys.stderr, flush=True)

                    try:
                        ended = play(session, feed, eeg, args.seconds, on_decision,
                                     sleep=sleep, clock=clock, stop=stimulus_gone,
                                     on_read=on_read)
                        if session.over and stim is not None and session.display_ok:
                            # the final position and result, on the wall clock even with
                            # --fast: the window is real
                            linger(args.end_s, stimulus_gone)
                    except KeyboardInterrupt:
                        ended = "interrupted"
                    if ended == "board stalled":
                        print(f"error: the board sent nothing for {STALL_S:g} s",
                              file=sys.stderr)
                        exit_code = 1
    except KeyboardInterrupt:
        if ended == "not started":   # connecting, launching, calibrating or training
            print("interrupted before the game started", file=sys.stderr)
            exit_code = 130
        else:
            ended = "interrupted"
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        ended = "error" if ended != "not started" else ended
        exit_code = 1
    finally:
        stim_code = _close(stim, display)
        if _escaped(stim, stim_code):
            if session is None or not session.over:   # Escape on the final screen just closes it
                ended = "escape"
        elif ended in ("interrupted", "not started") and stim_code == -signal.SIGINT:
            pass   # the terminal's Ctrl-C reached the stimulus too (same process group)
        elif stim is not None and stim_code not in (0, None):
            print(f"error: the stimulus exited with code {stim_code}; see {stim.log}",
                  file=sys.stderr)
            exit_code = exit_code or 1
        if tmp is not None:
            tmp.cleanup()
    if session is not None and ended != "not started":
        lines = session.summary()
        lines[0] += f"; run ended: {ended}" if ended != session.ended else ""
        if session.gate is not None:
            what = "decisions" if code else "windows"
            lines.append(f"  {session.gate.stale} stale {what} ignored, "
                         f"{len(session.picks)} picks in all")
        if picks_checked:
            what = "target" if code else "group"
            lines.append(f"  fake board: {picks_true} of {picks_checked} picks "
                         f"({100 * picks_true / picks_checked:.0f}%) went to the {what} the "
                         "simulated user looked at")
        pgn = session.pgn()
        print("\n".join(lines))
        print()
        print(pgn)
        if args.pgn:
            try:
                Path(args.pgn).write_text(pgn + "\n", encoding="utf-8")
            except OSError as exc:
                print(f"error: cannot write {args.pgn}: {exc}", file=sys.stderr)
                exit_code = exit_code or 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
