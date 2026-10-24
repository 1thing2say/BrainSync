"""BrainChess game logic: whose turn it is, what the board stimulus shows, and the opponent.

Kept apart from the board, decoder and stimulus process so a whole game can be played in a
test. ``ChessSession`` owns the position, the engine (move prior and opponent), the
current ``MoveSelector`` and the ``MoveGate``. Each decoded pick goes to ``on_pick``;
the session redraws the board through a ``Display`` after every pick, and once a move is
chosen it plays it, says it, holds the flicker still, lets the opponent reply (drawn as an
arrow on a still board for ``hold_s`` seconds, so the user can see what happened before
the next split starts flickering) and starts the next user turn from ``tick``.

``StimulusDisplay`` writes the board stimulus's stdin protocol (see
brainsync.brainchess.stimulus) and sends only what changed; ``NullDisplay`` records the
calls for tests and headless runs. Besides the board, the session reports the stage it is
at, and every decoder window and the gate's vote count go to the display for its trace
strip and vote meters (``window``, ``votes``); the run loop adds the EEG lanes
(``lanes``, ``eeg``). ``SimulatedUser`` stands in for the user with
``--fake``: it decides on a move at the start of each turn and after every screen change
looks at the group holding it, or at BACK once a wrong pick has ruled it out.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

import chess
import chess.pgn
import numpy as np

from ..classify import ABSTAIN
from ..online import Decision
from .gate import MoveGate
from .prior import Engine
from .render import render_pieces
from .split import MoveSelector

__all__ = ["Display", "StimulusDisplay", "NullDisplay", "Pick", "PlayedMove", "ChessSession",
           "SimulatedUser", "PHASES", "game_outcome"]

N_SQUARES = 64
PLAIN = "." * N_SQUARES
# idle: before start(); user: picks are being made; opponent: the still board after the
# opponent's move; over: the game ended or the move limit was reached
PHASES = ("idle", "user", "opponent", "over")
# what the stage row shows: the selector's stages, or none while the board is still
STAGES = ("piece", "dest", "promo", "confirm", "none")

Arrow = tuple[int, int] | None
Panels = tuple[str, str] | None


class Display(Protocol):
    """What the session draws on. Each call returns False once the display is gone."""

    def show(self, groups: str, pieces_png: Path | None, arrow: Arrow, panels: Panels,
             text: str, stage: str = "none") -> bool:
        """Draw a board state; ``pieces_png`` None keeps the pieces layer already shown.
        ``stage`` is the selector's stage (piece, dest, promo, confirm) or "none"."""
        ...

    def window(self, t: float, raw: int, fresh: bool, fired: bool) -> bool:
        """One decoder window ending at stream time ``t`` (s): the target it named
        (ABSTAIN for none), whether the gate counted it and whether it fired a pick."""
        ...

    def votes(self, target: int, n: int, of: int) -> bool:
        """``n`` of ``of`` agreeing fresh windows for ``target`` (ABSTAIN: none yet)."""
        ...

    def lanes(self, names: Sequence[str]) -> bool:
        """Names of the EEG lanes ``eeg`` will send, in order."""
        ...

    def eeg(self, t_end: float, dt: float, samples: np.ndarray) -> bool:
        """Display samples (k, lanes), ``dt`` apart, the last at stream time ``t_end``."""
        ...

    def hold(self) -> bool: ...

    def go(self) -> bool: ...

    def close(self) -> None: ...


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _target(k: int) -> str:
    return "-" if k == ABSTAIN else str(int(k))


class StimulusDisplay:
    """Writes protocol lines to the board stimulus's stdin, only for fields that changed.

    The stimulus starts with a plain board, no pieces, arrow, panels or caption, holding
    still, and this mirrors that. Each call writes one batch and flushes it. Once a write
    fails (the stimulus exited or closed its stdin) ``closed`` is set and every later call
    returns False without writing.
    """

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.closed = False
        self.lines = 0
        self._groups = PLAIN
        self._pieces: Path | None = None
        self._arrow: Arrow = None
        self._panels: Panels = None
        self._text = ""
        self._stage = "none"
        self._votes: tuple[int, int, int] | None = None
        self._holding = True

    def _write(self, lines: list[str]) -> bool:
        if self.closed:
            return False
        if not lines:
            return True
        try:
            self.stream.write("".join(f"{ln}\n" for ln in lines))
            self.stream.flush()
        except (BrokenPipeError, OSError, ValueError):   # ValueError: write to a closed file
            self.closed = True
            return False
        self.lines += len(lines)
        return True

    def show(self, groups: str, pieces_png: Path | None, arrow: Arrow, panels: Panels,
             text: str, stage: str = "none") -> bool:
        if len(groups) != N_SQUARES:
            raise ValueError(f"groups needs {N_SQUARES} characters, got {len(groups)}")
        if stage not in STAGES:
            raise ValueError(f"stage must be one of {', '.join(STAGES)}, got {stage!r}")
        text = _one_line(text)
        if panels is not None:
            # '|' separates the two labels in the protocol
            panels = tuple(_one_line(lab).replace("|", "/") for lab in panels)
        lines = []
        if groups != self._groups:
            lines.append(f"groups {groups}")
        if pieces_png is not None and pieces_png != self._pieces:
            lines.append(f"pieces {pieces_png}")
        if arrow != self._arrow:
            lines.append("arrow none" if arrow is None else f"arrow {arrow[0]} {arrow[1]}")
        if panels != self._panels:
            lines.append("panels none" if panels is None else f"panels {panels[0]}|{panels[1]}")
        if text != self._text:
            lines.append(f"text {text}" if text else "text")
        if stage != self._stage:
            lines.append(f"stage {stage}")
        if not self._write(lines):
            return False
        self._groups, self._arrow, self._panels, self._text = groups, arrow, panels, text
        self._stage = stage
        if pieces_png is not None:
            self._pieces = pieces_png
        return True

    def window(self, t: float, raw: int, fresh: bool, fired: bool) -> bool:
        return self._write([f"window {t:.4f} {_target(raw)} {int(fresh)} {int(fired)}"])

    def votes(self, target: int, n: int, of: int) -> bool:
        state = (int(target), int(n), int(of))
        if state == self._votes:
            return not self.closed
        if not self._write([f"votes {_target(target)} {n} {of}"]):
            return False
        self._votes = state
        return True

    def lanes(self, names: Sequence[str]) -> bool:
        names = [_one_line(n).replace(",", " ") for n in names]
        return self._write([f"lanes {','.join(names)}"])

    def eeg(self, t_end: float, dt: float, samples: np.ndarray) -> bool:
        values = " ".join(f"{v:.1f}" for v in np.asarray(samples, dtype=float).ravel())
        return self._write([f"eeg {t_end:.4f} {dt:.5f} {values}"])

    def _set_holding(self, holding: bool) -> bool:
        if holding == self._holding:
            return not self.closed
        if not self._write(["hold" if holding else "go"]):
            return False
        self._holding = holding
        return True

    def hold(self) -> bool:
        return self._set_holding(True)

    def go(self) -> bool:
        return self._set_holding(False)

    def close(self) -> None:
        """Close the stream; EOF ends the stimulus."""
        self.closed = True
        try:
            self.stream.close()
        except (BrokenPipeError, OSError):
            pass


class NullDisplay:
    """Draws nothing; keeps the latest state and every call (for tests and --no-stimulus)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.groups = PLAIN
        self.pieces: Path | None = None
        self.arrow: Arrow = None
        self.panels: Panels = None
        self.text = ""
        self.stage = "none"
        self.holding = True
        self.closed = False
        self.windows: list[tuple[float, int, bool, bool]] = []
        self.vote_state: tuple[int, int, int] | None = None
        self.lane_names: tuple[str, ...] = ()
        self.eeg_samples = 0       # counted, not kept: a long headless run sends many

    def show(self, groups: str, pieces_png: Path | None, arrow: Arrow, panels: Panels,
             text: str, stage: str = "none") -> bool:
        if len(groups) != N_SQUARES:
            raise ValueError(f"groups needs {N_SQUARES} characters, got {len(groups)}")
        self.calls.append(("show", {"groups": groups, "pieces": pieces_png, "arrow": arrow,
                                    "panels": panels, "text": text, "stage": stage}))
        self.groups, self.arrow, self.panels, self.text = groups, arrow, panels, text
        self.stage = stage
        if pieces_png is not None:
            self.pieces = pieces_png
        return True

    def window(self, t: float, raw: int, fresh: bool, fired: bool) -> bool:
        self.windows.append((t, raw, fresh, fired))
        return True

    def votes(self, target: int, n: int, of: int) -> bool:
        self.vote_state = (target, n, of)
        return True

    def lanes(self, names: Sequence[str]) -> bool:
        self.lane_names = tuple(names)
        return True

    def eeg(self, t_end: float, dt: float, samples: np.ndarray) -> bool:
        self.eeg_samples += len(samples)
        return True

    def hold(self) -> bool:
        self.calls.append(("hold", {}))
        self.holding = True
        return True

    def go(self) -> bool:
        self.calls.append(("go", {}))
        self.holding = False
        return True

    def close(self) -> None:
        self.calls.append(("close", {}))
        self.closed = True


@dataclass(frozen=True)
class Pick:
    """One decoded pick: during which user move, at which stage, out of how many candidates."""

    move_no: int        # 1 for the user's first move
    stage: str
    target: int
    candidates: int     # squares in piece/dest, moves in promo/confirm
    elapsed: float      # seconds since this move's turn started


@dataclass(frozen=True)
class PlayedMove:
    """A move the user played and what it took."""

    san: str
    picks: int
    backs: int
    seconds: float


def _silent(text: str) -> None:
    pass


def _color_name(color: chess.Color) -> str:
    return "White" if color == chess.WHITE else "Black"


def game_outcome(board: chess.Board) -> chess.Outcome | None:
    """How the game has ended, or None while it goes on.

    The automatic endings (mate, stalemate, insufficient material, fivefold repetition,
    the 75-move rule) plus a threefold repetition or 50-move draw that has actually
    happened. Not python-chess's ``claim_draw=True``: that also counts a draw the side to
    move could reach with its next move, which would end the game for a player who never
    chose to repeat.
    """
    outcome = board.outcome()
    if outcome is not None:
        return outcome
    if board.is_repetition(3):
        return chess.Outcome(chess.Termination.THREEFOLD_REPETITION, None)
    if board.is_fifty_moves():
        return chess.Outcome(chess.Termination.FIFTY_MOVES, None)
    return None


class ChessSession:
    """One game: the user picks moves through a ``MoveSelector``, the engine replies.

    ``clock`` gives seconds (the decoding clock: virtual with a fast FakeBoard). ``gate``
    is told about every redraw; None without a decoder (tests that pick directly).
    ``pieces_dir`` None skips rendering the pieces PNG (no stimulus to show it).
    ``on_screen(session)`` runs after every change of what is on screen (the simulated
    user looks again). ``max_moves`` > 0 ends the session after that many user moves.
    """

    def __init__(self, board: chess.Board, engine: Engine, display: Display | None = None, *,
                 user_color: chess.Color = chess.WHITE, gate: MoveGate | None = None,
                 confirm: bool = True, hold_s: float = 2.0, max_moves: int = 0,
                 clock: Callable[[], float] = time.monotonic,
                 speak: Callable[[str], None] = _silent, pieces_dir: str | Path | None = None,
                 flip: bool | None = None,
                 on_screen: Callable[[ChessSession], None] | None = None,
                 balance_tol: float = 0.15, min_gap: int = 1) -> None:
        if not hold_s >= 0:   # also rejects NaN
            raise ValueError(f"hold_s must be >= 0, got {hold_s}")
        if isinstance(max_moves, bool) or int(max_moves) != max_moves or max_moves < 0:
            raise ValueError(f"max_moves must be an integer >= 0, got {max_moves}")
        if user_color not in (chess.WHITE, chess.BLACK):
            raise ValueError(f"user_color must be chess.WHITE or chess.BLACK, got {user_color!r}")
        self.board = board
        self.engine = engine
        self.display: Display = display if display is not None else NullDisplay()
        self.user_color = user_color
        self.gate = gate
        self.confirm = confirm
        self.hold_s = float(hold_s)
        self.max_moves = int(max_moves)
        self.clock = clock
        self.speak = speak
        self.pieces_dir = None if pieces_dir is None else Path(pieces_dir)
        self.flip = (user_color == chess.BLACK) if flip is None else bool(flip)
        self.on_screen = on_screen
        self.balance_tol = balance_tol
        self.min_gap = min_gap
        self.phase = "idle"
        self.ended = ""             # why the session is over: "game over" or "move limit"
        self.selector: MoveSelector | None = None
        self.display_ok = True
        self.picks: list[Pick] = []
        self.moves: list[PlayedMove] = []
        self.backs = 0
        self.last_reply: chess.Move | None = None
        self._resume_at = 0.0
        self._turn_started = 0.0
        self._turn_picks = 0
        self._turn_backs = 0
        self._pieces_ply: int | None = None
        self._pieces_path: Path | None = None
        self._prior: tuple[str, dict[chess.Move, float]] | None = None   # (fen, weights)

    # -- state -------------------------------------------------------------------------

    @property
    def over(self) -> bool:
        return self.phase == "over"

    @property
    def waiting(self) -> bool:
        """True while the board holds still after the opponent's move."""
        return self.phase == "opponent"

    @property
    def user_moves(self) -> int:
        return len(self.moves)

    def game_over(self) -> bool:
        """The game has ended (see ``game_outcome``)."""
        return game_outcome(self.board) is not None

    def result(self) -> str:
        """'1-0', '0-1', '1/2-1/2', or '*' while the game goes on."""
        outcome = game_outcome(self.board)
        return "*" if outcome is None else outcome.result()

    def status(self) -> str:
        """One line on the state of the game."""
        outcome = game_outcome(self.board)
        if outcome is None:
            return f"{_color_name(self.board.turn)} to move"
        how = outcome.termination.name.replace("_", " ").lower()
        if outcome.winner is None:
            return f"Draw by {how} ({outcome.result()})"
        return f"{how.capitalize()}: {_color_name(outcome.winner)} wins ({outcome.result()})"

    # -- drawing -----------------------------------------------------------------------

    def _pieces(self) -> Path | None:
        """The pieces PNG for the current position, rendered once per position."""
        if self.pieces_dir is None:
            return None
        ply = len(self.board.move_stack)
        if ply != self._pieces_ply:
            path = self.pieces_dir / f"pieces_{ply:03d}.png"
            self._pieces_path = render_pieces(self.board, path, flip=self.flip)
            self._pieces_ply = ply
        return self._pieces_path

    def _show(self, groups: str, arrow: Arrow, panels: Panels, text: str,
              stage: str = "none") -> None:
        self.display_ok = self.display.show(groups, self._pieces(), arrow, panels, text,
                                            stage) and self.display_ok

    def _votes(self) -> None:
        gate = self.gate
        if gate is not None:
            self.display_ok = self.display.votes(gate.pending, gate.streak, gate.votes) \
                and self.display_ok

    def _hold(self) -> None:
        self.display_ok = self.display.hold() and self.display_ok

    def _go(self) -> None:
        self.display_ok = self.display.go() and self.display_ok

    def _screen_changed(self) -> None:
        if self.gate is not None:
            self.gate.screen_changed()
            self._votes()
        if self.on_screen is not None:
            self.on_screen(self)

    def _draw_selector(self) -> None:
        sel = self.selector
        assert sel is not None
        self._show(sel.groups(), sel.arrow(), sel.panels(), sel.prompt(), sel.stage)

    def show_still(self, text: str) -> None:
        """The position on a still board with a caption (calibration, before start)."""
        arrow = None if self.last_reply is None else (self.last_reply.from_square,
                                                      self.last_reply.to_square)
        self._show(PLAIN, arrow, None, text)
        self._hold()

    # -- flow --------------------------------------------------------------------------

    def start(self) -> None:
        """Begin: the user's turn, or the opponent's first move if the user plays Black."""
        if self.phase != "idle":
            raise ValueError(f"the session has already started (phase {self.phase!r})")
        if self.game_over():
            self._end("game over")
        elif self.board.turn == self.user_color:
            self.start_user_turn()
        else:
            self._opponent_move()

    def prefetch(self) -> None:
        """Work out the prior for the user's coming turn now, while the board stands still.

        Stockfish blocks for ``think_s`` here. Done inside ``start_user_turn`` that time
        would pass between the last board read and the screen change the gate stamps at
        the decoder's sample count, so windows recorded on the still board, before the
        flicker began, would count as settled.
        """
        if self.board.turn != self.user_color or self.game_over():
            return
        fen = self.board.fen()
        if self._prior is None or self._prior[0] != fen:
            self._prior = (fen, self.engine.weights(self.board))

    def _weights(self) -> dict[chess.Move, float]:
        self.prefetch()
        assert self._prior is not None
        weights = self._prior[1]
        self._prior = None
        return weights

    def start_user_turn(self) -> None:
        """A new selector over the engine's prior; the split flickers from here.

        A move that needs no pick (one legal move and no confirm step) is played at once.
        """
        if self.game_over():
            self._end("game over")
            return
        self.selector = MoveSelector(self.board, self._weights(), confirm=self.confirm,
                                     balance_tol=self.balance_tol, min_gap=self.min_gap)
        self.phase = "user"
        self._turn_started = self.clock()
        self._turn_picks = 0
        self._turn_backs = 0
        if self.selector.stage == "done":
            self.speak("Only one move")
            self._play_user_move(self.selector.move)
            return
        self._draw_selector()
        self._go()
        self._screen_changed()

    def on_pick(self, target: int) -> None:
        """Apply one decoded pick (0 or 1) to the current selector."""
        if self.phase != "user" or self.selector is None:
            raise ValueError(f"no pick is expected now (phase {self.phase!r})")
        sel = self.selector
        stage = sel.stage
        n = len(sel.candidates())
        back = stage == "confirm" and target == 1
        sel.choose(target)
        self._turn_picks += 1
        self.picks.append(Pick(self.user_moves + 1, stage, int(target), n,
                               self.clock() - self._turn_started))
        if back:
            self.backs += 1
            self._turn_backs += 1
            self.speak("Back")
        if sel.stage == "done":
            self._play_user_move(sel.move)
            return
        self._draw_selector()
        self._screen_changed()

    def _play_user_move(self, move: chess.Move) -> None:
        san = self.board.san(move)
        self.board.push(move)
        self.moves.append(PlayedMove(san, self._turn_picks, self._turn_backs,
                                     self.clock() - self._turn_started))
        self.selector = None
        self.last_reply = None
        self.speak(f"You play {san}")
        self._show(PLAIN, (move.from_square, move.to_square), None, f"You play {san}")
        self._hold()
        if self.game_over():
            self._end("game over")
        elif self.max_moves and self.user_moves >= self.max_moves:
            self._end("move limit")
        else:
            self._opponent_move()

    def _opponent_move(self) -> None:
        reply = self.engine.reply(self.board)
        san = self.board.san(reply)
        self.board.push(reply)
        self.last_reply = reply
        self.speak(f"{_color_name(not self.user_color)} plays {san}")
        if self.game_over():
            self._end("game over")
            return
        self.phase = "opponent"
        self._show(PLAIN, (reply.from_square, reply.to_square), None,
                   f"{_color_name(not self.user_color)} played {san}")
        self._hold()
        self.prefetch()
        # the still board lasts hold_s from when it is up, whatever the prior took
        self._resume_at = self.clock() + self.hold_s
        self._screen_changed()
        if self.hold_s <= 0:
            self.start_user_turn()

    def tick(self) -> None:
        """Call regularly: ends the still board after the opponent's move once hold_s passed."""
        if self.phase == "opponent" and self.clock() >= self._resume_at:
            self.start_user_turn()

    def push(self, d: Decision) -> int:
        """Pass one decoder decision through the gate; the pick it fired, or ABSTAIN.

        Decisions outside the user's turn are dropped (the board is still then). Every
        decision goes to the display's trace strip, marked fresh only when the gate
        counted it, and the vote meter follows the gate.
        """
        if self.gate is None:
            return ABSTAIN
        t = d.end / self.gate.decoder.fs
        if self.phase != "user":
            self.display_ok = self.display.window(t, int(d.raw), False, False) \
                and self.display_ok
            return ABSTAIN
        stale = self.gate.stale
        target = self.gate.push(d)
        self.display_ok = self.display.window(t, int(d.raw), self.gate.stale == stale,
                                              target != ABSTAIN) and self.display_ok
        if target != ABSTAIN:
            self.on_pick(target)
        self._votes()
        return target

    def _end(self, why: str) -> None:
        self.phase = "over"
        self.ended = why
        self.selector = None
        status = self.status() if why == "game over" else f"Stopped after {self.user_moves} moves"
        last = self.board.peek() if self.board.move_stack else None
        self._show(PLAIN, None if last is None else (last.from_square, last.to_square), None,
                   status)
        self._hold()
        self.speak(status)
        if self.on_screen is not None:
            self.on_screen(self)

    # -- records -----------------------------------------------------------------------

    def pgn(self, white: str | None = None, black: str | None = None) -> str:
        """The game so far as PGN (with a FEN header when it began from one)."""
        game = chess.pgn.Game.from_board(self.board)
        user, engine = "BrainChess user", f"BrainChess {self.engine.name}"
        game.headers["Event"] = "BrainChess"
        game.headers["White"] = white or (user if self.user_color == chess.WHITE else engine)
        game.headers["Black"] = black or (user if self.user_color == chess.BLACK else engine)
        game.headers["Result"] = self.result()
        return str(game)

    def summary(self) -> list[str]:
        """Lines for the end of a run: moves, picks per move, BACKs, seconds per move."""
        n = len(self.moves)
        picks = sum(m.picks for m in self.moves)
        per = f"{picks / n:.1f}" if n else "n/a"
        secs = f"{sum(m.seconds for m in self.moves) / n:.1f} s" if n else "n/a"
        lines = [f"brainchess: {n} moves played, {picks} picks ({per} per move), "
                 f"{self.backs} BACK, {secs} per move; {self.status()}"
                 + (f"; ended: {self.ended}" if self.ended else "")]
        if self.moves:
            lines.append("  moves: " + "  ".join(
                f"{m.san} ({m.picks} picks{', ' + str(m.backs) + ' BACK' if m.backs else ''}, "
                f"{m.seconds:.1f} s)" for m in self.moves))
        return lines


class SimulatedUser:
    """A user for --fake runs and tests: knows what it wants and looks at it.

    At the start of every user turn it decides on ``engine.reply`` for its side (an
    Engine with its own seed, so it plays sensible moves). ``look(session)`` is the target
    it looks at now: the group holding its move; after a wrong pick has ruled that move
    out, the group of the heaviest move still reachable (any pick moves the selection on),
    and then BACK at confirm. ABSTAIN while the board is still.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.intended: chess.Move | None = None
        self._selector: MoveSelector | None = None

    def look(self, session: ChessSession) -> int:
        sel = session.selector
        if session.phase != "user" or sel is None:
            return ABSTAIN
        if sel is not self._selector:
            self._selector = sel
            self.intended = self.engine.reply(session.board)
        assert self.intended is not None
        if sel.stage == "confirm":
            return sel.target_for(self.intended)
        move = self.intended
        if not sel.reachable(move):
            cands = sel.candidate_moves()
            move = max(cands, key=cands.__getitem__)
        return sel.target_for(move)
