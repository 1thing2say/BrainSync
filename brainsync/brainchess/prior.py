"""Move priors and the opponent: Stockfish when it is installed, a heuristic otherwise.

The two-target picker splits the candidate squares so that each pick halves the *prior
weight* of the remaining moves, not their count, so likely moves take fewer picks. The
prior only orders and weights moves: ``to_weights`` mixes the engine's softmax with a
uniform share, so every legal move keeps a weight above zero and stays reachable.

Stockfish is optional (it is not always installable on the demo machine), so the
heuristic is a full fallback, not a stub: it scores captures, promotions, checks,
castling, development, centralisation, hanging pieces and mate in one. The same scores drive a weak but
sensible opponent, sampled with a seeded generator so a game replays exactly. If
Stockfish dies or errors mid-game, ``Engine`` warns once on stderr and uses the
heuristic for the rest of the session rather than ending the game.

    python -m brainsync.brainchess.prior [FEN]   # print the prior over the legal moves
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import chess
import chess.engine
import numpy as np

__all__ = ["MATE_SCORE", "PIECE_VALUES", "find_stockfish", "heuristic_scores", "to_weights", "Engine"]

ENV_VAR = "BRAINCHESS_STOCKFISH"
# Homebrew, then Debian/Ubuntu's stockfish package (/usr/games is not on a systemd unit's PATH)
FALLBACK_PATHS = ("/opt/homebrew/bin/stockfish", "/usr/local/bin/stockfish", "/usr/games/stockfish")

MATE_SCORE = 10000.0
MAX_MULTIPV = 64
MISSING_PENALTY = 50.0     # moves Stockfish did not rank score this far below its worst one

PIECE_VALUES = {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
                chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}

# Heuristic bonuses in centipawns. Small next to material so they only break ties among
# moves that neither win nor lose anything.
CHECK_BONUS = 40.0
CASTLE_BONUS = 60.0
DEVELOP_BONUS = 30.0
EARLY_KING_PENALTY = 40.0  # moving the king (not castling) while the queens are still on
EARLY_QUEEN_PENALTY = 15.0
STALEMATE_PENALTY = 300.0
CENTRE_CP = 10.0           # per step towards the centre (pieces other than king and queen)
OPENING_MOVES = 10         # development bonuses apply up to this full move number

# The opponent: much sharper than the picker's prior so it plays the good moves it sees
# most of the time, with a little noise so it is beatable and not the same every game.
REPLY_TEMPERATURE_CP = 60.0
REPLY_UNIFORM_MIX = 0.05


def _executable(path: str | os.PathLike) -> bool:
    p = Path(path)
    return p.is_file() and os.access(p, os.X_OK)


def find_stockfish(path: str | None = None) -> str | None:
    """The Stockfish binary to use, or None if there is none.

    In order: ``path`` if it exists and is executable, the ``BRAINCHESS_STOCKFISH``
    environment variable, ``stockfish`` on PATH, then the usual Homebrew locations.
    """
    candidates: list[str] = []
    if path:
        candidates.append(path)
    env = os.environ.get(ENV_VAR)
    if env:
        candidates.append(env)
    on_path = shutil.which("stockfish")
    if on_path:
        candidates.append(on_path)
    candidates.extend(FALLBACK_PATHS)
    for candidate in candidates:
        if _executable(candidate):
            return str(candidate)
    return None


def _captured_value(board: chess.Board, move: chess.Move) -> int:
    if board.is_en_passant(move):
        return PIECE_VALUES[chess.PAWN]
    victim = board.piece_type_at(move.to_square)
    return PIECE_VALUES[victim] if victim is not None else 0


def _centre_distance(square: chess.Square) -> float:
    return max(abs(chess.square_file(square) - 3.5), abs(chess.square_rank(square) - 3.5))


def _worst_loss(board: chess.Board, side: chess.Color) -> float:
    """The most material ``side`` stands to lose to one capture by the other side."""
    worst = 0
    for piece_type in (chess.PAWN, chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN):
        value = PIECE_VALUES[piece_type]
        for square in board.pieces(piece_type, side):
            attackers = board.attackers(not side, square)
            if not attackers:
                continue
            if not board.attackers(side, square):
                worst = max(worst, value)
            else:
                cheapest = min(PIECE_VALUES[board.piece_type_at(sq)] for sq in attackers)
                worst = max(worst, value - cheapest)
    return float(worst)


def _score_move(board: chess.Board, move: chess.Move) -> float:
    """One move's heuristic score; ``board`` is left as it was."""
    us = board.turn
    mover = board.piece_type_at(move.from_square)
    assert mover is not None, "legal moves start on a piece"
    gained = _captured_value(board, move)
    castling = board.is_castling(move)
    queens_on = bool(board.pieces(chess.QUEEN, chess.WHITE) or board.pieces(chess.QUEEN, chess.BLACK))
    opening = board.fullmove_number <= OPENING_MOVES
    home_rank = 0 if us == chess.WHITE else 7

    board.push(move)
    try:
        if board.is_checkmate():
            return MATE_SCORE
        score = float(gained)
        if move.promotion is not None:
            score += PIECE_VALUES[move.promotion] - PIECE_VALUES[chess.PAWN]
        if board.is_stalemate():
            score -= STALEMATE_PENALTY
        if board.is_check():
            score += CHECK_BONUS
        if castling:
            score += CASTLE_BONUS
        elif mover == chess.KING and queens_on:
            score -= EARLY_KING_PENALTY
        if mover not in (chess.KING, chess.QUEEN):
            score += CENTRE_CP * (_centre_distance(move.from_square) - _centre_distance(move.to_square))
        if opening:
            if mover in (chess.KNIGHT, chess.BISHOP) and chess.square_rank(move.from_square) == home_rank:
                score += DEVELOP_BONUS
            if mover == chess.QUEEN:
                score -= EARLY_QUEEN_PENALTY

        # Hanging a piece: after the move the opponent takes whichever of our pieces wins
        # it the most. An attacked, undefended piece is lost; a defended one is still lost
        # to a cheaper attacker (an exchange netting the difference). One-ply static view,
        # no x-rays or pins, so moving an attacked piece to safety scores above leaving it.
        score -= _worst_loss(board, us)
        return score
    finally:
        board.pop()


def heuristic_scores(board: chess.Board) -> dict[chess.Move, float]:
    """A centipawn-like score per legal move, from the side to move's point of view.

    Counts material won, promotion gain, check, castling, minor-piece development in the
    opening, steps towards the centre and the worst loss from leaving a piece en prise;
    mate in one scores ``MATE_SCORE``. Deterministic, and ``board`` is not changed (empty when game over).
    """
    work = board.copy(stack=False)
    return {move: _score_move(work, move) for move in work.legal_moves}


def to_weights(scores: Mapping[chess.Move, float], temperature_cp: float = 100.0,
               uniform_mix: float = 0.3) -> dict[chess.Move, float]:
    """Scores to a prior: ``(1 - uniform_mix) * softmax(scores / temperature) + uniform_mix / n``.

    Sums to 1, and every weight is above zero whenever ``uniform_mix > 0`` (the softmax
    itself is computed after subtracting the best score, so it never overflows).
    """
    if not scores:
        raise ValueError("to_weights needs at least one move")
    if not (temperature_cp > 0 and math.isfinite(temperature_cp)):
        raise ValueError(f"temperature_cp must be a positive finite number, got {temperature_cp}")
    if not 0.0 <= uniform_mix <= 1.0:
        raise ValueError(f"uniform_mix must be in [0, 1], got {uniform_mix}")
    moves = list(scores)
    values = np.array([scores[m] for m in moves], dtype=float)
    if not np.all(np.isfinite(values)):
        raise ValueError("to_weights needs finite scores")
    z = np.exp((values - values.max()) / temperature_cp)
    w = (1.0 - uniform_mix) * z / z.sum() + uniform_mix / len(moves)
    w /= w.sum()
    return {m: float(x) for m, x in zip(moves, w)}


class Engine:
    """Scores moves and plays the opponent: Stockfish at ``path``, or the heuristic if None.

    Use as a context manager (or call ``close``) so the Stockfish process is shut down.
    """

    def __init__(self, path: str | None = None, skill: int = 3, think_s: float = 0.2, seed: int = 0):
        if not 0 <= skill <= 20:
            raise ValueError(f"skill must be in 0..20 (Stockfish 'Skill Level'), got {skill}")
        if not think_s > 0:
            raise ValueError(f"think_s must be > 0, got {think_s}")
        self.skill = skill
        self.think_s = think_s
        self.seed = seed
        self._rng = np.random.default_rng(seed)
        self._engine: chess.engine.SimpleEngine | None = None
        if path is not None:
            try:
                self._engine = chess.engine.SimpleEngine.popen_uci(path)
                self._engine.configure({"Skill Level": skill})
            except Exception as exc:  # a broken binary must not stop the game starting
                self._fail(exc)

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        engine, self._engine = self._engine, None
        if engine is not None:
            try:
                engine.quit()
            except Exception:  # already dead; nothing left to shut down
                pass

    @property
    def name(self) -> str:
        return f"stockfish (skill {self.skill})" if self._engine is not None else "heuristic"

    def _fail(self, exc: BaseException) -> None:
        """Warn once and drop to the heuristic for the rest of the session."""
        print(f"brainchess: stockfish failed ({type(exc).__name__}: {exc}); "
              "using the heuristic from now on", file=sys.stderr)
        engine, self._engine = self._engine, None
        if engine is not None:
            try:
                engine.close()
            except Exception:  # the process is already gone
                pass

    def scores(self, board: chess.Board) -> dict[chess.Move, float]:
        """A centipawn score per legal move from the side to move's view (empty when game over)."""
        legal = list(board.legal_moves)
        if not legal:
            return {}
        if self._engine is not None:
            try:
                return self._stockfish_scores(board, legal)
            except Exception as exc:  # any engine failure must not end the game
                self._fail(exc)
        return heuristic_scores(board)

    def _stockfish_scores(self, board: chess.Board, legal: Sequence[chess.Move]) -> dict[chess.Move, float]:
        assert self._engine is not None
        infos = self._engine.analyse(board, chess.engine.Limit(time=self.think_s),
                                     multipv=min(len(legal), MAX_MULTIPV))
        if isinstance(infos, Mapping):   # a single InfoDict when multipv is 1
            infos = [infos]
        ranked: dict[chess.Move, float] = {}
        legal_set = set(legal)
        for info in infos:
            pv, score = info.get("pv"), info.get("score")
            if not pv or score is None or pv[0] not in legal_set or pv[0] in ranked:
                continue
            ranked[pv[0]] = float(score.pov(board.turn).score(mate_score=int(MATE_SCORE)))
        if not ranked:
            return heuristic_scores(board)
        floor = min(ranked.values()) - MISSING_PENALTY
        return {m: ranked.get(m, floor) for m in legal}

    def weights(self, board: chess.Board, temperature_cp: float = 100.0,
                uniform_mix: float = 0.3) -> dict[chess.Move, float]:
        """The prior over the legal moves (``to_weights`` of ``scores``)."""
        return to_weights(self.scores(board), temperature_cp, uniform_mix)

    def reply(self, board: chess.Board) -> chess.Move:
        """The opponent's move in ``board``; always legal."""
        if board.is_game_over():
            raise ValueError(f"the game is over ({board.result()}); there is no move to play")
        if self._engine is not None:
            try:
                result = self._engine.play(board, chess.engine.Limit(time=self.think_s))
                if result.move is not None and board.is_legal(result.move):
                    return result.move
            except Exception as exc:  # any engine failure must not end the game
                self._fail(exc)
        weights = to_weights(heuristic_scores(board), REPLY_TEMPERATURE_CP, REPLY_UNIFORM_MIX)
        moves = list(weights)
        p = np.array([weights[m] for m in moves])
        return moves[int(self._rng.choice(len(moves), p=p / p.sum()))]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("fen", nargs="?", default=chess.STARTING_FEN, help="position (default: start)")
    parser.add_argument("--stockfish", default=None, help="Stockfish binary (default: search)")
    parser.add_argument("--heuristic", action="store_true", help="ignore Stockfish even if found")
    parser.add_argument("--top", type=int, default=10, help="moves to print")
    args = parser.parse_args(argv)
    try:
        board = chess.Board(args.fen)
    except ValueError as exc:
        print(f"bad FEN: {exc}", file=sys.stderr)
        return 2
    path = None if args.heuristic else find_stockfish(args.stockfish)
    with Engine(path) as engine:
        scores = engine.scores(board)
        if not scores:
            print(f"game over: {board.result()}")
            return 0
        weights = to_weights(scores)
        print(f"{engine.name}: {len(scores)} legal moves")
        for move in sorted(weights, key=weights.get, reverse=True)[:args.top]:
            print(f"  {board.san(move):8s} {scores[move]:8.0f} cp  {weights[move]:.3f}")
        print(f"  reply: {board.san(engine.reply(board))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
