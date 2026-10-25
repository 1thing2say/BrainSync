"""Choosing a chess move with a sequence of two-way picks, by straight cuts across the board.

The decoder can tell only two frequencies apart, so every pick is binary: the candidate
squares are split into two groups, one flickering as target 0 (15 Hz) and one as target 1
(17 Hz), and the decoder names the group the user is looking at. A group has to be easy to
see as one region, so each split is a single straight cut between two files or two ranks.

Cuts split the engine-prior weight of the remaining candidates as evenly as possible, so a
likely move takes few picks. Among nearly-as-even cuts, one that leaves an empty file or
rank between the groups is preferred: 15 and 17 Hz squares that touch blur into each other
in peripheral vision and in the EEG. The prior only weights moves; every legal move keeps a
positive weight and stays reachable.

``MoveSelector`` walks one move through the stages piece -> dest -> (promo) -> (confirm) ->
done and tells the stimulus what to draw at each step. Pure logic: no IO, no decoder.

With c-VEP (the BrainSync standard, docs/CVEP-STANDARD.md) there is a third target, Back:
``undo()`` takes back the last pick, so a wrong decode costs one more pick instead of the
whole move. The two groups stay targets 0 and 1 (``undo=True`` only renames the confirm
step's second choice, which still starts the move over, from BACK to RESTART); a split
into three groups would leave no target for Back.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import chess

__all__ = ["BOARD", "Cut", "cut_options", "gap", "imbalance", "split", "MoveSelector",
           "expected_picks", "STAGES", "PROMO_NAMES", "FLOOR_SHARE"]

BOARD = 8
AXES = ("file", "rank")
STAGES = ("piece", "dest", "promo", "confirm", "done")

# A legal move the prior left out (or gave zero) gets this share of the prior's total, so
# it costs a few extra picks but can always be chosen.
FLOOR_SHARE = 1e-3

PROMO_NAMES = {chess.QUEEN: "Queen", chess.ROOK: "Rook", chess.BISHOP: "Bishop",
               chess.KNIGHT: "Knight"}

_EPS = 1e-12   # float slack when comparing imbalances


@dataclass(frozen=True)
class Cut:
    """A straight cut: group 0 is every square whose file (or rank) is <= ``after``."""

    axis: str
    after: int

    def __post_init__(self) -> None:
        if self.axis not in AXES:
            raise ValueError(f"axis must be 'file' or 'rank', got {self.axis!r}")
        if not 0 <= self.after <= BOARD - 2:
            raise ValueError(f"after must be in 0..{BOARD - 2}, got {self.after}")

    def coord(self, sq: int) -> int:
        _check_square(sq)
        return sq % BOARD if self.axis == "file" else sq // BOARD

    def side(self, sq: int) -> int:
        return 0 if self.coord(sq) <= self.after else 1


def _check_square(sq: int) -> None:
    if isinstance(sq, bool) or not isinstance(sq, int) or not 0 <= sq < BOARD * BOARD:
        raise ValueError(f"square must be an int in 0..{BOARD * BOARD - 1}, got {sq!r}")


def cut_options(squares: Iterable[int]) -> list[Cut]:
    """Every straight cut leaving both groups non-empty; file cuts first, lower ``after`` first."""
    sqs = set(squares)
    for sq in sqs:
        _check_square(sq)
    out = []
    for axis in AXES:
        for after in range(BOARD - 1):
            cut = Cut(axis, after)
            sides = {cut.side(sq) for sq in sqs}
            if sides == {0, 1}:
                out.append(cut)
    return out


def gap(cut: Cut, squares: Iterable[int]) -> int:
    """Number of empty files (or ranks) strictly between the two groups of ``cut``."""
    sqs = list(squares)
    low = max(cut.coord(sq) for sq in sqs if cut.side(sq) == 0)
    high = min(cut.coord(sq) for sq in sqs if cut.side(sq) == 1)
    return high - low - 1


def imbalance(cut: Cut, weights: Mapping[int, float]) -> float:
    """|w0 - w1| / total for the two groups of ``cut``."""
    total = sum(weights.values())
    w0 = sum(w for sq, w in weights.items() if cut.side(sq) == 0)
    return abs(w0 - (total - w0)) / total


def _number(w: object) -> bool:
    """A finite real number (Python or numpy), not a bool."""
    try:
        return not isinstance(w, (bool, str, bytes)) and math.isfinite(float(w))
    except (TypeError, ValueError):
        return False


def _check_weights(weights: Mapping[int, float]) -> None:
    if len(weights) < 2:
        raise ValueError(f"need at least 2 squares to split, got {len(weights)}")
    for sq, w in weights.items():
        _check_square(sq)
        if not _number(w) or not w > 0:
            raise ValueError(f"weight of square {sq} must be positive and finite, got {w!r}")


def best_cut(weights: Mapping[int, float], balance_tol: float = 0.15, min_gap: int = 1) -> Cut:
    """The cut ``split`` uses; see there for the rule."""
    _check_weights(weights)
    if not (math.isfinite(balance_tol) and balance_tol >= 0):
        raise ValueError(f"balance_tol must be >= 0, got {balance_tol}")
    if min_gap < 0:
        raise ValueError(f"min_gap must be >= 0, got {min_gap}")
    cuts = cut_options(weights)   # never empty: two distinct squares differ in file or rank
    scored = [(imbalance(c, weights), c) for c in cuts]
    best = min(s for s, _ in scored)
    ok = [(s, c) for s, c in scored if s <= best + balance_tol + _EPS]
    # cut_options order is the deterministic tie-break (file cuts first, lower after first);
    # min() keeps the first of equal keys.
    return min(ok, key=lambda sc: (-min(gap(sc[1], weights), min_gap), round(sc[0], 9)))[1]


def split(weights: Mapping[int, float], balance_tol: float = 0.15,
          min_gap: int = 1) -> tuple[frozenset[int], frozenset[int]]:
    """Split weighted squares into (group0, group1) by one straight cut.

    The cut minimises |w0 - w1| / total. Among cuts within ``balance_tol`` of the best
    imbalance, the one with the most empty files/ranks between the groups wins (gaps beyond
    ``min_gap`` count as ``min_gap``), then the smaller imbalance, then file cuts before
    rank cuts and the lower ``after``.
    """
    cut = best_cut(weights, balance_tol, min_gap)
    g0 = frozenset(sq for sq in weights if cut.side(sq) == 0)
    return g0, frozenset(weights) - g0


def _resolve_weights(board: chess.Board, weights: Mapping[chess.Move, float]) -> dict[chess.Move, float]:
    """Weight for every legal move: the given ones, a small floor for missing or zero ones."""
    legal = list(board.legal_moves)
    if not legal:
        raise ValueError("the board has no legal moves (game over)")
    legal_set = set(legal)
    given: dict[chess.Move, float] = {}
    for move, w in weights.items():
        if move not in legal_set:
            raise ValueError(f"weight given for {move} which is not legal on {board.fen()}")
        if not _number(w) or w < 0:
            raise ValueError(f"weight of {move} must be >= 0 and finite, got {w!r}")
        if w > 0:
            given[move] = float(w)
    total = sum(given.values())
    floor = FLOOR_SHARE * total if total > 0 else 1.0
    return {m: given.get(m, floor) for m in legal}


class MoveSelector:
    """One move chosen by binary picks: piece, destination, promotion piece, confirm.

    Feed each decoded pick to ``choose``; draw ``groups()``, ``panels()``, ``arrow()`` and
    ``prompt()`` after each. ``move`` is set once ``stage == 'done'``.
    """

    def __init__(self, board: chess.Board, weights: Mapping[chess.Move, float], confirm: bool = True,
                 balance_tol: float = 0.15, min_gap: int = 1, undo: bool = False) -> None:
        if not (math.isfinite(balance_tol) and balance_tol >= 0):
            raise ValueError(f"balance_tol must be >= 0, got {balance_tol}")
        if min_gap < 0:
            raise ValueError(f"min_gap must be >= 0, got {min_gap}")
        self.board = board.copy(stack=False)
        self.weights = _resolve_weights(self.board, weights)
        self.confirm = confirm
        self.balance_tol = balance_tol
        self.min_gap = min_gap
        self.undo_target = bool(undo)   # c-VEP: a third target, Back, undoes the last pick
        self.picks = 0
        self.undos = 0
        self._restart()

    # -- state -------------------------------------------------------------------------

    def _restart(self) -> None:
        self._moves: list[chess.Move] = list(self.weights)
        self._origin: int | None = None
        self._dest: int | None = None
        self._groups: tuple[frozenset[int], frozenset[int]] | None = None
        self._promo: tuple[list[chess.Move], list[chess.Move]] | None = None
        self.stage = "piece"
        self.move: chess.Move | None = None
        self._history: list[tuple] = []   # the state before each pick, for undo()
        self._settle()

    def _settle(self) -> None:
        """Enter the next stage that needs a pick, skipping stages with one candidate."""
        while True:
            if self.stage == "piece":
                origins = self._square_weights(lambda m: m.from_square)
                if len(origins) > 1:
                    self._groups = split(origins, self.balance_tol, self.min_gap)
                    return
                self._origin = next(iter(origins))
                self.stage = "dest"
            elif self.stage == "dest":
                dests = self._square_weights(lambda m: m.to_square)
                if len(dests) > 1:
                    self._groups = split(dests, self.balance_tol, self.min_gap)
                    return
                self._dest = next(iter(dests))
                self._groups = None
                self.stage = "promo"
            elif self.stage == "promo":
                if len(self._moves) > 1:
                    self._promo = self._promo_split()
                    return
                self._promo = None
                self.stage = "confirm" if self.confirm else "done"
            elif self.stage == "confirm":
                return
            else:   # done
                self.move = self._moves[0]
                return

    def _square_weights(self, key) -> dict[int, float]:
        out: dict[int, float] = {}
        for m in self._moves:
            out[key(m)] = out.get(key(m), 0.0) + self.weights[m]
        return out

    def _promo_split(self) -> tuple[list[chess.Move], list[chess.Move]]:
        """Heaviest promotions first, cut where the two contiguous runs weigh most alike."""
        order = sorted(self._moves, key=lambda m: (-self.weights[m], -(m.promotion or 0)))
        ws = [self.weights[m] for m in order]
        total = sum(ws)
        best_k, best = 1, math.inf
        for k in range(1, len(order)):
            diff = abs(2 * sum(ws[:k]) - total)
            if diff < best - _EPS:
                best_k, best = k, diff
        return order[:best_k], order[best_k:]

    # -- picks -------------------------------------------------------------------------

    def choose(self, target: int) -> None:
        """Apply a decoded pick: keep the group the user looked at (0 or 1)."""
        if target not in (0, 1) or isinstance(target, bool):
            raise ValueError(f"target must be 0 or 1, got {target!r}")
        if self.stage == "done":
            raise ValueError("the move is already chosen; call back() to choose again")
        self.picks += 1
        if self.stage == "confirm" and target == 1:
            self._restart()
            return
        self._history.append(self._state())
        if self.stage in ("piece", "dest"):
            keep = self._groups[target]
            if self.stage == "piece":
                self._moves = [m for m in self._moves if m.from_square in keep]
            else:
                self._moves = [m for m in self._moves if m.to_square in keep]
        elif self.stage == "promo":
            keep_moves = set(self._promo[target])
            self._moves = [m for m in self._moves if m in keep_moves]
        else:   # confirm, target 0: play it
            self.stage = "done"
        self._settle()

    def back(self) -> None:
        """Start the move over from the piece stage; ``picks`` keeps counting."""
        self._restart()

    def _state(self) -> tuple:
        return (list(self._moves), self._origin, self._dest, self._groups, self._promo,
                self.stage)

    @property
    def can_undo(self) -> bool:
        """True when there is a pick to take back (not at a move's first pick, nor once
        the move is chosen)."""
        return bool(self._history) and self.stage != "done"

    def undo(self) -> bool:
        """The Back target: take back the last pick, back to the screen before it.

        Counts as a pick. Returns False, changing nothing else, when there is nothing to
        undo (the move's first pick, or right after RESTART). Undoing a pick that skipped
        stages (one piece or square left) goes back to the split before them.
        """
        if self.stage == "done":
            raise ValueError("the move is already chosen; there is nothing to undo")
        self.picks += 1
        if not self._history:
            return False
        moves, self._origin, self._dest, self._groups, self._promo, self.stage = \
            self._history.pop()
        self._moves = moves
        self.move = None
        self.undos += 1
        return True

    # -- what to draw ------------------------------------------------------------------

    @property
    def shown_move(self) -> chess.Move | None:
        """The single remaining move in confirm/done, else None."""
        return self._moves[0] if self.stage in ("confirm", "done") else None

    def groups(self) -> str:
        """64 chars for the stimulus 'groups' command: '.', '0', '1' or 's' per square."""
        chars = ["."] * (BOARD * BOARD)
        if self.stage in ("piece", "dest"):
            for target, group in enumerate(self._groups):
                for sq in group:
                    chars[sq] = str(target)
        if self._origin is not None:
            chars[self._origin] = "s"
        if self._dest is not None:
            chars[self._dest] = "s"
        return "".join(chars)

    def panels(self) -> tuple[str, str] | None:
        if self.stage == "confirm":
            return ("PLAY", "RESTART") if self.undo_target else ("PLAY", "BACK")
        if self.stage == "promo":
            return tuple(" / ".join(PROMO_NAMES[m.promotion] for m in grp) for grp in self._promo)
        return None

    def arrow(self) -> tuple[int, int] | None:
        m = self.shown_move
        return (m.from_square, m.to_square) if m is not None else None

    def prompt(self) -> str:
        if self.stage == "piece":
            return "Look at the piece you want to move"
        if self.stage == "dest":
            return "Look at its destination"
        if self.stage == "promo":
            return "Promote to?"
        san = self.board.san(self._moves[0])
        if self.stage == "confirm":
            if self.undo_target:
                return f"Play {san}? Look at PLAY"
            return f"Play {san}? Look at PLAY or BACK"
        return f"Playing {san}"

    def candidates(self) -> dict[int, float] | list[chess.Move]:
        """Candidate squares -> weight in piece/dest stage; the remaining moves otherwise."""
        if self.stage == "piece":
            return self._square_weights(lambda m: m.from_square)
        if self.stage == "dest":
            return self._square_weights(lambda m: m.to_square)
        return list(self._moves)

    def candidate_moves(self) -> dict[chess.Move, float]:
        """Every move still reachable in this attempt, with its weight."""
        return {m: self.weights[m] for m in self._moves}

    def reachable(self, move: chess.Move) -> bool:
        """Whether ``move`` can still be chosen without going back."""
        return move in self._moves

    # -- for the simulated user --------------------------------------------------------

    def side_of(self, square: int) -> int | None:
        """Group (0/1) of ``square`` at the current piece/dest pick, None if not a candidate."""
        _check_square(square)
        if self.stage not in ("piece", "dest"):
            return None
        for target, group in enumerate(self._groups):
            if square in group:
                return target
        return None

    def target_for(self, move: chess.Move) -> int:
        """The target a user who wants ``move`` should look at now."""
        if self.stage == "done":
            raise ValueError("the move is already chosen")
        if self.stage == "confirm":
            return 0 if move == self._moves[0] else 1
        if move not in self._moves:
            raise ValueError(f"{move} is no longer reachable at the {self.stage} stage; "
                             "wait for confirm and pick BACK")
        if self.stage == "piece":
            return self.side_of(move.from_square)
        if self.stage == "dest":
            return self.side_of(move.to_square)
        return 0 if move in self._promo[0] else 1


def expected_picks(board: chess.Board, weights: Mapping[chess.Move, float], confirm: bool = True,
                   balance_tol: float = 0.15, min_gap: int = 1) -> float:
    """Expected picks to choose a move drawn from the (normalised, floored) weights.

    Exact: every legal move is driven through a fresh selector by an error-free user.
    """
    resolved = MoveSelector(board, weights, confirm, balance_tol, min_gap).weights
    total = sum(resolved.values())
    out = 0.0
    for move, w in resolved.items():
        sel = MoveSelector(board, weights, confirm, balance_tol, min_gap)
        while sel.stage != "done":
            sel.choose(sel.target_for(move))
        if sel.move != move:   # cannot happen for an error-free user; guards the logic
            raise RuntimeError(f"selector chose {sel.move} instead of {move}")
        out += w / total * sel.picks
    return out
