"""BrainChess: play a whole game of chess by looking at the flickering board.

    python -m brainsync.brainchess                                # Cerelog X8 + board stimulus
    python -m brainsync.brainchess --fake --fast --no-stimulus    # simulated user, virtual time

Only two frequencies decode reliably on dry electrodes (15 Hz = target 0, 17 Hz = target
1), so every choice is a binary pick drawn on the board itself: the candidate squares are
cut in two by one straight line, one side flickers at 15 Hz and the other at 17 Hz, and
the user keeps looking at the piece they want until it is the only one left. Then the
same for its destination, the promotion piece if any (two side panels), and a PLAY / BACK
confirm on the side panels (--no-confirm skips it). BACK restarts the move. The opponent
(Stockfish if found, else a built-in heuristic) replies while the board stands still for
--hold-s seconds with its move drawn as an arrow. When the game ends (or --max-moves is
reached) the final position and result stay on the board for --end-s seconds.

Sequence: connect to the board, start the board stimulus holding still, ask the user to
keep still and calibrate the rest baseline on the still board (--calibrate s, as in
brainsync.demo), then play: each board read is decoded, decisions pass the fresh-window
gate (--cooldown, --votes; see brainsync.brainchess.gate) and every pick redraws the board.
With --fake a simulated user picks the moves it intends (the heuristic engine with its
own seed) and points the fake board at the right group after every screen change; wrong
decodes make it pick BACK at the confirm step. The run ends when the game ends, after
--max-moves user moves, after --seconds, or on Escape, Ctrl-C, a silent board or a
crashed stimulus. One status line per window goes to stderr; the summary and the PGN
(also written to --pgn) go to stdout. Exit codes are the demo's: 0 for a run that ended
normally (game over, limits, Escape, Ctrl-C), 1 when the board stalled, the stimulus
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
from collections.abc import Callable
from pathlib import Path
from typing import Any

import chess
import numpy as np

from ..board import CerelogBoard, FakeBoard
from ..classify import ABSTAIN, make
from ..demo import CALIBRATE_S, POLL_S, STALL_S, _channels, _names
from ..launch import Stimulus, launch_stimulus, speak
from ..online import TUNED, Decision, Decoder, calibrate, threshold_arg
from ..recordings import MONTAGE, POSTERIOR
from ..stimulus import load_config
from ..stimulus.config import config_warnings
from .gate import MoveGate
from .game import ChessSession, NullDisplay, SimulatedUser, StimulusDisplay, game_outcome
from .prior import Engine, find_stockfish
from .trace import TraceFeed

STIMULUS_MODULE = "brainsync.brainchess.stimulus"
DEFAULT_CONFIG = "chess"
STILL_PROMPT = "Keep still and look at the middle of the board"
# the simulated user's engine seed is offset from --seed so it does not mirror the opponent
USER_SEED_OFFSET = 1000


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m brainsync.brainchess", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                   help=f"board stimulus config name or .toml path (default {DEFAULT_CONFIG})")
    p.add_argument("--classifier", default=TUNED.classifier, choices=("cca", "fbcca"),
                   help=f"default {TUNED.classifier}")
    p.add_argument("--window", type=float, default=TUNED.window_s,
                   help=f"window seconds (default {TUNED.window_s:g})")
    p.add_argument("--step", type=float, default=TUNED.step_s,
                   help=f"seconds between decisions (default {TUNED.step_s:g})")
    p.add_argument("--votes", type=int, default=TUNED.votes,
                   help=f"consecutive agreeing fresh windows needed for a pick (default "
                        f"{TUNED.votes})")
    p.add_argument("--margin", type=float, default=TUNED.margin,
                   help="abstain unless the best target leads by this many rest spreads "
                        f"(default {TUNED.margin:g})")
    p.add_argument("--threshold", type=threshold_arg, default=TUNED.threshold,
                   help="abstain unless the best target is this many rest spreads above its "
                        f"rest level, or 'none' (default {TUNED.threshold})")
    p.add_argument("--pad", type=float, default=2.0,
                   help="seconds filtered before each window and dropped (default 2)")
    p.add_argument("--harmonics", type=int, default=3, help="template harmonics (default 3)")
    p.add_argument("--calibrate", type=float, default=CALIBRATE_S,
                   help=f"still-board seconds for the rest baseline (default {CALIBRATE_S:g})")
    p.add_argument("--seconds", type=float, default=0.0,
                   help="cap on the playing time in seconds (default 0: no cap)")
    p.add_argument("--names", type=_names, default=list(MONTAGE),
                   help="comma-separated names of the 8 board channels, in order")
    p.add_argument("--channels", type=_channels, default=list(POSTERIOR),
                   help="analysis channels, names or indices (default " + ",".join(POSTERIOR) + ")")
    p.add_argument("--port", default=None, help="serial port (default: auto-detect)")
    p.add_argument("--verbose", action="store_true", help="show BrainFlow's log messages")
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
                      help="seconds after a screen change before a window may vote: the eyes "
                           "settle on the new split (default 0.5)")
    game.add_argument("--no-confirm", action="store_true",
                      help="play a move as soon as it is chosen, without PLAY / BACK")
    game.add_argument("--max-moves", type=int, default=0,
                      help="stop after this many of your moves (default 0: play to the end)")
    game.add_argument("--pgn", metavar="PATH", default=None,
                      help="also write the game to this PGN file")
    fake = p.add_argument_group("synthetic board")
    fake.add_argument("--fake", action="store_true",
                      help="FakeBoard and a simulated user instead of the Cerelog X8")
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
    if args.fast and not args.fake:
        p.error("--fast needs --fake")
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
    if not 0 <= args.skill <= 20:
        p.error(f"--skill must be in 0..20, got {args.skill}")
    if args.max_moves < 0:
        p.error(f"--max-moves must be >= 0, got {args.max_moves}")
    if args.seed < 0:   # numpy refuses negative seeds
        p.error(f"--seed must be >= 0, got {args.seed}")
    if len(args.names) != len(MONTAGE):
        p.error(f"--names needs {len(MONTAGE)} names, got {len(args.names)}: {args.names}")


def stimulus_args(args: argparse.Namespace) -> list[str]:
    """Command-line arguments for the board stimulus."""
    out = ["-c", str(args.config), "--markers", "none"]
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


def play(session: ChessSession, decoder: Decoder, board: Any, seconds: float,
         on_decision: Callable[[Decision], None], sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic, stop: Callable[[], str | None] | None = None,
         poll_s: float = POLL_S, stall_s: float = STALL_S,
         on_read: Callable[[np.ndarray], None] | None = None) -> str:
    """Decode and play until the session ends; returns why the run ended.

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


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    _check(p, args)
    voice = not args.no_voice

    def say(text: str) -> None:
        speak(text, voice)

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
        cfg = load_config(args.config)
        if cfg.n_targets != 2:
            raise ValueError(f"BrainChess needs a two-target config (15 and 17 Hz); "
                             f"{cfg.name!r} has {cfg.n_targets}")
        if args.fake:
            eeg: Any = FakeBoard(cfg.freqs, seed=args.seed,
                                 clock=None if args.fast else time.time, ch_names=args.names)
        else:
            eeg = CerelogBoard(serial_port=args.port, ch_names=args.names, verbose=args.verbose)
        clf = make(args.classifier, freqs=cfg.freqs, fs=eeg.fs, harmonics=args.harmonics,
                   phases=cfg.phases, margin=args.margin, threshold=args.threshold)
        decoder = Decoder(clf, eeg.fs, window_s=args.window, step_s=args.step,
                          votes=args.votes, channels=args.channels, pad_s=args.pad,
                          ch_names=args.names)
        gate = MoveGate(decoder, cooldown_s=args.cooldown)
    except (ValueError, KeyError, FileNotFoundError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    freqs = tuple(float(f) for f in cfg.freqs)
    fast = args.fake and args.fast
    sleep: Callable[[float], None] = eeg.advance if fast else time.sleep
    clock: Callable[[], float] = (lambda: eeg.elapsed) if fast else time.monotonic
    for w in config_warnings(cfg):
        print(f"warning: {w}", file=sys.stderr)
    print(f"{'fake board' if args.fake else 'Cerelog X8'} at {eeg.fs:g} Hz; {clf!r}; "
          f"channels {','.join(decoder.channel_names)}; window {args.window:g} s, step "
          f"{args.step:g} s, {args.votes} votes, cooldown {args.cooldown:g} s", file=sys.stderr)
    if args.calibrate < CALIBRATE_S:
        print(f"warning: a {args.calibrate:g} s baseline is noisy; rest decisions may lean "
              f"towards one target (default {CALIBRATE_S:g} s)", file=sys.stderr)

    stim: Stimulus | None = None
    display: Any = None
    session: ChessSession | None = None
    tmp: tempfile.TemporaryDirectory | None = None
    ended = "not started"
    code = 0
    picks_true = picks_checked = 0
    try:
        with Engine(engine_path, skill=args.skill, think_s=args.think_s, seed=args.seed) as engine:
            print(f"opponent: {engine.name}; you play {args.color}"
                  + (f" from {position.fen()}" if args.fen else ""), file=sys.stderr)
            user = (SimulatedUser(Engine(None, seed=args.seed + USER_SEED_OFFSET))
                    if args.fake else None)

            def look(s: ChessSession) -> None:
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
                                       on_screen=look if user is not None else None)
                display.lanes(decoder.channel_names)
                feed = TraceFeed(decoder.rows, decoder.fs)

                def on_read(X: np.ndarray) -> None:
                    shown = feed.push(X, decoder.n_seen)
                    if shown is not None:
                        display.eeg(shown[0], feed.dt, shown[1])

                # the baseline is fit on the still board the user will see between picks
                session.show_still(STILL_PROMPT)
                say(STILL_PROMPT + ".")
                if args.fake:
                    eeg.set_target(ABSTAIN)
                cal = calibrate(decoder, eeg, args.calibrate,
                                sleep=eeg.advance if fast else None)
                print(f"baseline from {cal.n_windows} still windows ({cal.n_rejected} rejected): "
                      + " ".join(f"{f:g} {m:.4f}" for f, m in zip(freqs, cal.mean))
                      + f", spread {cal.spread:.4f}", file=sys.stderr)

                def stimulus_gone() -> str | None:
                    if stim is not None and stim.proc.poll() is not None:
                        return f"stimulus exited (code {stim.proc.returncode})"
                    return None

                gone = stimulus_gone() or (None if session.display_ok else "stimulus pipe closed")
                if gone:
                    ended = f"{gone} before the game started"
                else:
                    session.prefetch()   # Stockfish's first analysis, before the drop below
                    eeg.read()   # drop what arrived during the prompt
                    began = decoder.n_seen
                    ended = "started"
                    say("Look at the piece you want to move.")
                    session.start()

                    def on_decision(d: Decision) -> None:
                        nonlocal picks_true, picks_checked
                        elapsed = (d.end - began) / decoder.fs
                        truth = eeg.active_target() if args.fake else None
                        stale = gate.stale
                        before = len(session.picks)
                        fired = session.push(d)
                        print(_status(d, elapsed, freqs, session, gate.stale > stale, fired,
                                      truth), file=sys.stderr, flush=True)
                        if fired == ABSTAIN:
                            return
                        pick = session.picks[before]
                        if truth is not None:
                            picks_checked += 1
                            picks_true += fired == truth
                        now = session.selector.prompt() if session.selector else session.status()
                        print(f"  pick: move {pick.move_no} {pick.stage} -> "
                              f"{_target_name(fired, freqs)} Hz of {pick.candidates} "
                              f"candidates at {pick.elapsed:.1f} s; now: {now}",
                              file=sys.stderr, flush=True)

                    try:
                        ended = play(session, decoder, eeg, args.seconds, on_decision,
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
                        code = 1
    except KeyboardInterrupt:
        if ended == "not started":   # connecting, launching or calibrating: no game yet
            print("interrupted before the game started", file=sys.stderr)
            code = 130
        else:
            ended = "interrupted"
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        ended = "error" if ended != "not started" else ended
        code = 1
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
            code = code or 1
        if tmp is not None:
            tmp.cleanup()
    if session is not None and ended != "not started":
        lines = session.summary()
        lines[0] += f"; run ended: {ended}" if ended != session.ended else ""
        if session.gate is not None:
            lines.append(f"  {session.gate.stale} stale windows ignored, "
                         f"{len(session.picks)} picks in all")
        if picks_checked:
            lines.append(f"  fake board: {picks_true} of {picks_checked} picks "
                         f"({100 * picks_true / picks_checked:.0f}%) went to the group the "
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
                code = code or 1
    return code


if __name__ == "__main__":
    sys.exit(main())
