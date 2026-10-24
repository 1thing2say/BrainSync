"""Tests for brainsync.brainchess.split: straight cuts and the move-selection stages."""
from __future__ import annotations

import math

import numpy as np
import pytest

chess = pytest.importorskip("chess")   # the optional "chess" extra

from brainsync.brainchess.split import (BOARD, FLOOR_SHARE, Cut, MoveSelector, best_cut,
                                        cut_options, expected_picks, gap, imbalance, split)

START = chess.STARTING_FEN
MIDGAME = "r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 4 4"
CASTLE_BOTH = "r3k2r/pppqbppp/2np1n2/4p3/4P3/2NP1N2/PPPQBPPP/R3K2R w KQkq - 0 1"
EN_PASSANT = "rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 3"
PROMOTION = "1r2k3/2P5/8/8/8/8/8/4K3 w - - 0 1"
ONE_MOVE = "7k/7p/8/7P/8/8/8/K5R1 b - - 0 1"
IN_CHECK = "rnbqkbnr/ppp2ppp/8/1B1pp3/4P3/8/PPPP1PPP/RNBQK1NR b KQkq - 1 3"
ONE_PIECE = "7k/8/8/8/8/8/8/K3N3 w - - 0 1"   # one knight and the king; used for skip tests
ALL_FENS = [START, MIDGAME, CASTLE_BOTH, EN_PASSANT, PROMOTION, ONE_MOVE, IN_CHECK, ONE_PIECE]


def _uniform(board: chess.Board) -> dict[chess.Move, float]:
    return {m: 1.0 for m in board.legal_moves}


def _random_weights(board: chess.Board, seed: int) -> dict[chess.Move, float]:
    rng = np.random.default_rng(seed)
    return {m: float(rng.exponential()) + 1e-6 for m in board.legal_moves}


def _drive(sel: MoveSelector, move: chess.Move, limit: int = 64) -> int:
    """Let an error-free user pick ``move``; return the picks it took."""
    start = sel.picks
    while sel.stage != "done":
        sel.choose(sel.target_for(move))
        assert sel.picks - start <= limit
    return sel.picks - start


def sq(name: str) -> int:
    return chess.parse_square(name)


# -- cuts ----------------------------------------------------------------------------------

def test_cut_side_and_validation():
    cut = Cut("file", 3)
    assert cut.side(sq("d1")) == 0 and cut.side(sq("e8")) == 1
    cut = Cut("rank", 0)
    assert cut.side(sq("h1")) == 0 and cut.side(sq("a2")) == 1
    with pytest.raises(ValueError, match="axis"):
        Cut("diag", 1)
    with pytest.raises(ValueError, match="after"):
        Cut("file", 7)
    with pytest.raises(ValueError, match="after"):
        Cut("rank", -1)
    with pytest.raises(ValueError, match="square"):
        Cut("file", 3).side(64)


def test_cut_options_separate_and_are_ordered():
    squares = [sq("a1"), sq("c1"), sq("c3")]
    cuts = cut_options(squares)
    assert cuts == [Cut("file", 0), Cut("file", 1), Cut("rank", 0), Cut("rank", 1)]
    for cut in cuts:
        assert {cut.side(s) for s in squares} == {0, 1}
    assert cut_options([sq("e4")]) == []
    assert cut_options([]) == []
    # two distinct squares always have a cut
    for a in range(64):
        for b in (0, 27, 63):
            if a != b:
                assert cut_options([a, b])
    with pytest.raises(ValueError):
        cut_options([0, 64])


def test_gap_and_imbalance():
    w = {sq("a1"): 1.0, sq("b1"): 1.0, sq("e1"): 2.0}
    assert gap(Cut("file", 1), w) == 2           # c and d files empty
    assert gap(Cut("file", 3), w) == 2           # same groups, another cut position
    assert gap(Cut("file", 0), w) == 0
    assert imbalance(Cut("file", 1), w) == 0.0
    assert imbalance(Cut("file", 0), w) == pytest.approx(0.5)


# -- split ---------------------------------------------------------------------------------

def test_split_returns_cover_of_disjoint_nonempty_groups():
    rng = np.random.default_rng(0)
    for _ in range(200):
        n = int(rng.integers(2, 20))
        squares = rng.choice(64, size=n, replace=False)
        weights = {int(s): float(rng.exponential()) + 0.01 for s in squares}
        g0, g1 = split(weights)
        assert g0 and g1 and not (g0 & g1) and g0 | g1 == set(weights)
        # groups lie on either side of one straight cut
        assert any({c.side(s) for s in g0} == {0} and {c.side(s) for s in g1} == {1}
                   for c in cut_options(weights))


def test_split_minimises_imbalance_without_gap_preference():
    rng = np.random.default_rng(1)
    for _ in range(100):
        squares = rng.choice(64, size=int(rng.integers(2, 16)), replace=False)
        weights = {int(s): float(rng.exponential()) + 0.01 for s in squares}
        cut = best_cut(weights, balance_tol=0.0, min_gap=0)
        assert imbalance(cut, weights) == pytest.approx(min(imbalance(c, weights) for c in cut_options(weights)))


def test_split_prefers_a_gap_within_tolerance():
    # a1 b1 c1 e1: the most even split a-b / c-e (imbalance 0.05) has no gap; a-c / e
    # leaves the d file empty but is less even (imbalance 0.58).
    w = {sq("a1"): 1.0, sq("b1"): 1.0, sq("c1"): 1.0, sq("e1"): 0.8}
    g0, g1 = split(w, balance_tol=0.0, min_gap=1)
    assert g0 == {sq("a1"), sq("b1")}              # best balance only
    g0, g1 = split(w, balance_tol=0.5, min_gap=1)
    assert g0 == {sq("a1"), sq("b1")}              # gapped cut is outside the tolerance
    g0, g1 = split(w, balance_tol=0.6, min_gap=1)
    assert g0 == {sq("a1"), sq("b1"), sq("c1")} and g1 == {sq("e1")}   # gap d
    g0, g1 = split(w, balance_tol=0.6, min_gap=0)
    assert g0 == {sq("a1"), sq("b1")}              # gap not wanted


def test_split_gap_preference_is_capped_at_min_gap():
    # Both cuts within tolerance have gaps >= 1; with min_gap=1 they tie on gap and the
    # better balanced (smaller gap) wins.
    w = {sq("a1"): 1.0, sq("c1"): 1.0, sq("h1"): 1.1}
    g0, _ = split(w, balance_tol=0.5, min_gap=1)
    assert g0 == {sq("a1"), sq("c1")}              # imbalance ~0.03, gap 4 (capped to 1)
    w = {sq("a1"): 1.0, sq("b1"): 1.0, sq("h1"): 1.0, sq("d1"): 1.0}
    g0, _ = split(w, balance_tol=0.0, min_gap=1)
    assert g0 == {sq("a1"), sq("b1")}              # even; gap c


def test_split_respects_min_gap_on_full_ranks():
    # Start position from-squares: pawns rank 2, knights b1/g1. Uniform weights.
    board = chess.Board()
    sel = MoveSelector(board, _uniform(board))
    g0, g1 = sel._groups
    files0 = {s % 8 for s in g0}
    files1 = {s % 8 for s in g1}
    ranks0 = {s // 8 for s in g0}
    ranks1 = {s // 8 for s in g1}
    separated = (max(files0) + 1 < min(files1)) or (max(ranks0) + 1 < min(ranks1))
    gap_exists = any(gap(c, sel.candidates()) >= 1 and imbalance(c, sel.candidates()) <= 0.15 + 1e-9
                     for c in cut_options(sel.candidates()))
    assert separated == gap_exists


def test_split_is_deterministic_and_tie_breaks_file_first():
    w = {sq("a1"): 1.0, sq("b2"): 1.0}
    # file cut after a and rank cut after 1 both split evenly with no gap; file wins
    assert split(w) == (frozenset({sq("a1")}), frozenset({sq("b2")}))
    assert best_cut(w) == Cut("file", 0)
    w = {sq("a1"): 1.0, sq("a2"): 1.0}
    assert best_cut(w) == Cut("rank", 0)
    rng = np.random.default_rng(2)
    weights = {int(s): float(rng.exponential()) + 0.01 for s in rng.choice(64, 12, replace=False)}
    assert split(weights) == split(dict(reversed(list(weights.items()))))


@pytest.mark.parametrize("weights, match", [
    ({}, "at least 2"),
    ({3: 1.0}, "at least 2"),
    ({3: 1.0, 4: 0.0}, "positive"),
    ({3: 1.0, 4: -1.0}, "positive"),
    ({3: 1.0, 4: math.nan}, "positive"),
    ({3: 1.0, 4: math.inf}, "positive"),
    ({3: 1.0, 64: 1.0}, "square"),
    ({-1: 1.0, 4: 1.0}, "square"),
])
def test_split_rejects_bad_weights(weights, match):
    with pytest.raises(ValueError, match=match):
        split(weights)


def test_split_rejects_bad_settings():
    with pytest.raises(ValueError, match="balance_tol"):
        split({0: 1.0, 1: 1.0}, balance_tol=-0.1)
    with pytest.raises(ValueError, match="min_gap"):
        split({0: 1.0, 1: 1.0}, min_gap=-1)


def test_split_accepts_numpy_weights():
    g0, g1 = split({0: np.float32(1.0), 7: np.float64(2.0)})
    assert g0 == {0} and g1 == {7}


# -- MoveSelector: reachability ------------------------------------------------------------

@pytest.mark.parametrize("fen", ALL_FENS)
@pytest.mark.parametrize("confirm", [True, False])
def test_every_legal_move_is_reachable_uniform(fen, confirm):
    board = chess.Board(fen)
    for move in board.legal_moves:
        sel = MoveSelector(board, _uniform(board), confirm=confirm)
        _drive(sel, move)
        assert sel.move == move


@pytest.mark.parametrize("fen", ALL_FENS)
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_every_legal_move_is_reachable_weighted(fen, seed):
    board = chess.Board(fen)
    weights = _random_weights(board, seed)
    for move in board.legal_moves:
        sel = MoveSelector(board, weights, balance_tol=0.3, min_gap=2)
        _drive(sel, move)
        assert sel.move == move


@pytest.mark.parametrize("fen", ALL_FENS)
def test_moves_missing_from_prior_stay_reachable(fen):
    board = chess.Board(fen)
    legal = list(board.legal_moves)
    favourite = legal[0]
    sel = MoveSelector(board, {favourite: 1.0})
    assert sel.weights[favourite] == 1.0
    assert all(sel.weights[m] == pytest.approx(FLOOR_SHARE) for m in legal[1:])
    for move in legal:
        sel = MoveSelector(board, {favourite: 1.0, legal[-1]: 0.0})
        _drive(sel, move)
        assert sel.move == move


def test_special_moves_are_selectable():
    board = chess.Board(CASTLE_BOTH)
    for uci in ("e1g1", "e1c1"):
        sel = MoveSelector(board, _uniform(board))
        _drive(sel, chess.Move.from_uci(uci))
        assert board.is_castling(sel.move)
    board = chess.Board(EN_PASSANT)
    sel = MoveSelector(board, _uniform(board))
    _drive(sel, chess.Move.from_uci("e5f6"))
    assert board.is_en_passant(sel.move)


def test_every_random_sequence_of_picks_ends_in_a_legal_move():
    board = chess.Board(MIDGAME)
    rng = np.random.default_rng(3)
    legal = set(board.legal_moves)
    for _ in range(200):
        sel = MoveSelector(board, _random_weights(board, 4))
        while sel.stage != "done":
            sel.choose(int(rng.integers(2)))
            assert sel.picks < 200
        assert sel.move in legal


# -- MoveSelector: stages and drawing -----------------------------------------------------

def test_start_position_walkthrough():
    board = chess.Board()
    sel = MoveSelector(board, _uniform(board))
    assert sel.stage == "piece" and sel.picks == 0
    assert sel.prompt() == "Look at the piece you want to move"
    assert sel.panels() is None and sel.arrow() is None
    assert set(sel.candidates()) == {sq("b1"), sq("g1")} | set(range(8, 16))
    assert sel.candidates()[sq("e2")] == 2.0 and sel.candidates()[sq("b1")] == 2.0
    e4 = chess.Move.from_uci("e2e4")
    while sel.stage == "piece":
        sel.choose(sel.target_for(e4))
    assert sel.stage == "dest" and sel.prompt() == "Look at its destination"
    assert set(sel.candidates()) == {sq("e3"), sq("e4")}
    g = sel.groups()
    assert g[sq("e2")] == "s" and {g[sq("e3")], g[sq("e4")]} == {"0", "1"}
    sel.choose(sel.target_for(e4))
    assert sel.stage == "confirm"
    assert sel.panels() == ("PLAY", "BACK")
    assert sel.arrow() == (sq("e2"), sq("e4"))
    assert sel.prompt() == "Play e4? Look at PLAY or BACK"
    g = sel.groups()
    assert g[sq("e2")] == g[sq("e4")] == "s" and g.count("s") == 2 and set(g) == {".", "s"}
    assert sel.target_for(e4) == 0 and sel.target_for(chess.Move.from_uci("d2d4")) == 1
    sel.choose(0)
    assert sel.stage == "done" and sel.move == e4
    assert sel.arrow() == (sq("e2"), sq("e4")) and sel.panels() is None
    assert sel.groups().count("s") == 2
    with pytest.raises(ValueError, match="already chosen"):
        sel.choose(0)
    with pytest.raises(ValueError, match="already chosen"):
        sel.target_for(e4)


@pytest.mark.parametrize("fen", [START, MIDGAME, PROMOTION, IN_CHECK])
def test_groups_strings_match_side_of(fen):
    board = chess.Board(fen)
    weights = _random_weights(board, 5)
    for move in board.legal_moves:
        sel = MoveSelector(board, weights)
        while sel.stage != "done":
            g = sel.groups()
            assert len(g) == BOARD * BOARD and set(g) <= set(".01s")
            if sel.stage in ("piece", "dest"):
                cands = sel.candidates()
                for s in range(64):
                    side = sel.side_of(s)
                    if s in cands:
                        assert g[s] == str(side)
                    else:
                        assert side is None and g[s] in ".s"
                assert "0" in g and "1" in g
            else:
                assert "0" not in g and "1" not in g
                assert all(sel.side_of(s) is None for s in range(64))
            sel.choose(sel.target_for(move))


def test_promotion_stage():
    board = chess.Board(PROMOTION)
    weights = _uniform(board)
    weights[chess.Move.from_uci("c7c8q")] = 10.0
    sel = MoveSelector(board, weights)
    knight = chess.Move.from_uci("c7c8n")
    while sel.stage in ("piece", "dest"):
        sel.choose(sel.target_for(knight))
    assert sel.stage == "promo" and sel.prompt() == "Promote to?"
    assert sel.panels() == ("Queen", "Rook / Bishop / Knight")
    assert sel.arrow() is None
    g = sel.groups()
    assert g[sq("c7")] == g[sq("c8")] == "s" and set(g) == {".", "s"}
    assert sel.candidates() == [chess.Move.from_uci(u) for u in ("c7c8q", "c7c8r", "c7c8b", "c7c8n")]
    assert sel.target_for(knight) == 1
    sel.choose(1)
    assert sel.panels() == ("Rook", "Bishop / Knight")   # uniform: heaviest-first order Q R B N
    sel.choose(1)
    assert sel.panels() == ("Bishop", "Knight")
    sel.choose(1)
    assert sel.stage == "confirm" and sel.prompt() == "Play c8=N? Look at PLAY or BACK"
    sel.choose(0)
    assert sel.move == knight


def test_promotion_uniform_splits_two_and_two():
    board = chess.Board(PROMOTION)
    sel = MoveSelector(board, _uniform(board))
    q = chess.Move.from_uci("c7b8q")
    while sel.stage in ("piece", "dest"):
        sel.choose(sel.target_for(q))
    assert sel.panels() == ("Queen / Rook", "Bishop / Knight")


def test_single_candidate_stages_are_skipped():
    board = chess.Board(ONE_MOVE)
    sel = MoveSelector(board, _uniform(board))
    assert sel.stage == "confirm" and sel.picks == 0
    assert sel.arrow() == (sq("h7"), sq("h6"))
    sel = MoveSelector(board, {}, confirm=False)
    assert sel.stage == "done" and sel.move == chess.Move.from_uci("h7h6") and sel.picks == 0


def test_only_one_movable_piece_starts_in_dest():
    board = chess.Board("k7/8/8/8/8/8/1p6/K7 w - - 0 1")   # in check: Ka2, Kb1, Kxb2
    assert {m.from_square for m in board.legal_moves} == {sq("a1")}
    sel = MoveSelector(board, {})
    assert sel.stage == "dest" and sel.picks == 0
    assert sel.groups()[sq("a1")] == "s"
    assert set(sel.candidates()) == {sq("a2"), sq("b1"), sq("b2")}


def test_only_one_destination_goes_to_confirm():
    board = chess.Board("4k3/8/8/7p/8/7P/8/K7 w - - 0 1")   # h3 pawn can only go to h4
    move = chess.Move.from_uci("h3h4")
    sel = MoveSelector(board, {})
    assert sel.stage == "piece"
    sel.choose(sel.target_for(move))
    assert sel.stage == "confirm" and sel.picks == 1
    assert sel.arrow() == (sq("h3"), sq("h4"))


def test_choose_rejects_bad_targets():
    board = chess.Board()
    sel = MoveSelector(board, {})
    for bad in (2, -1, "0", None, True, 0.5):
        with pytest.raises(ValueError, match="target"):
            sel.choose(bad)
    assert sel.picks == 0
    sel.choose(np.int64(1))
    assert sel.picks == 1


def test_target_for_unreachable_move_raises():
    board = chess.Board()
    sel = MoveSelector(board, _uniform(board))
    e4 = chess.Move.from_uci("e2e4")
    sel.choose(1 - sel.target_for(e4))
    assert not sel.reachable(e4)
    with pytest.raises(ValueError, match="no longer reachable"):
        sel.target_for(e4)


def test_back_resets_but_keeps_counting():
    board = chess.Board(MIDGAME)
    weights = _random_weights(board, 6)
    fresh = MoveSelector(board, weights)
    sel = MoveSelector(board, weights)
    move = chess.Move.from_uci("e1g1")
    while sel.stage != "confirm":
        sel.choose(sel.target_for(move))
    n = sel.picks
    sel.choose(1)   # BACK
    assert sel.picks == n + 1
    assert sel.stage == fresh.stage and sel.groups() == fresh.groups()
    assert sel.candidates() == fresh.candidates() and sel.move is None and sel.arrow() is None
    sel.choose(0)
    sel.back()
    assert sel.picks == n + 2 and sel.groups() == fresh.groups()
    _drive(sel, move)
    assert sel.move == move
    sel.back()   # also allowed once done
    assert sel.stage == fresh.stage and sel.move is None


def test_confirm_false_ends_without_play_back():
    board = chess.Board()
    sel = MoveSelector(board, _uniform(board), confirm=False)
    e4 = chess.Move.from_uci("e2e4")
    picks = _drive(sel, e4)
    with_confirm = _drive(MoveSelector(board, _uniform(board)), e4)
    assert sel.move == e4 and picks == with_confirm - 1
    assert sel.panels() is None and sel.arrow() == (sq("e2"), sq("e4"))
    assert sel.prompt() == "Playing e4"


def test_selector_does_not_mutate_board():
    board = chess.Board(MIDGAME)
    fen = board.fen()
    sel = MoveSelector(board, {})
    _drive(sel, next(iter(board.legal_moves)))
    assert board.fen() == fen


def test_selector_is_deterministic():
    board = chess.Board(CASTLE_BOTH)
    weights = _random_weights(board, 7)
    a, b = MoveSelector(board, weights), MoveSelector(board, dict(reversed(list(weights.items()))))
    for move in list(board.legal_moves)[:10]:
        a.back(), b.back()
        while a.stage != "done":
            assert a.groups() == b.groups() and a.stage == b.stage
            t = a.target_for(move)
            a.choose(t), b.choose(t)
        assert a.move == b.move == move


def test_selector_rejects_bad_arguments():
    board = chess.Board()
    with pytest.raises(ValueError, match="not legal"):
        MoveSelector(board, {chess.Move.from_uci("e2e5"): 1.0})
    with pytest.raises(ValueError, match="e2e4"):
        MoveSelector(board, {chess.Move.from_uci("e2e4"): -1.0})
    with pytest.raises(ValueError, match="e2e4"):
        MoveSelector(board, {chess.Move.from_uci("e2e4"): math.nan})
    with pytest.raises(ValueError, match="no legal moves"):
        MoveSelector(chess.Board("7k/8/6Q1/8/8/8/8/K6R b - - 0 1"), {})   # checkmate
    with pytest.raises(ValueError, match="no legal moves"):
        MoveSelector(chess.Board("k7/8/1QK5/8/8/8/8/8 b - - 0 1"), {})    # stalemate
    with pytest.raises(ValueError, match="balance_tol"):
        MoveSelector(board, {}, balance_tol=-1)
    with pytest.raises(ValueError, match="min_gap"):
        MoveSelector(board, {}, min_gap=-1)
    with pytest.raises(ValueError, match="square"):
        MoveSelector(board, {}).side_of(64)


def test_empty_prior_is_uniform():
    board = chess.Board()
    sel = MoveSelector(board, {})
    assert set(sel.weights.values()) == {1.0} and len(sel.weights) == 20


# -- expected_picks ------------------------------------------------------------------------

def test_expected_picks_start_position():
    board = chess.Board()
    uniform = expected_picks(board, _uniform(board))
    assert 4.0 <= uniform <= 8.0
    peaked = {m: 0.1 / 19 for m in board.legal_moves}
    peaked[chess.Move.from_uci("e2e4")] = 0.9
    assert expected_picks(board, peaked) < uniform
    assert expected_picks(board, _uniform(board), confirm=False) == pytest.approx(uniform - 1)


def test_expected_picks_single_move():
    board = chess.Board(ONE_MOVE)
    assert expected_picks(board, {}) == 1.0
    assert expected_picks(board, {}, confirm=False) == 0.0


@pytest.mark.parametrize("fen", [MIDGAME, PROMOTION, IN_CHECK])
def test_expected_picks_matches_direct_average(fen):
    board = chess.Board(fen)
    weights = _random_weights(board, 8)
    total = sum(weights.values())
    direct = sum(w / total * _drive(MoveSelector(board, weights), m) for m, w in weights.items())
    assert expected_picks(board, weights) == pytest.approx(direct)
    # never worse than ceil(log2(n)) + confirm by much: straight cuts cost at most a few picks
    assert expected_picks(board, weights) <= math.ceil(math.log2(len(weights))) + 1 + 4


@pytest.mark.parametrize("min_gap", [1, 2])
def test_split_gap_property_random(min_gap):
    rng = np.random.default_rng(9)
    for _ in range(300):
        squares = rng.choice(64, size=int(rng.integers(2, 12)), replace=False)
        weights = {int(s): float(rng.exponential()) + 0.01 for s in squares}
        cuts = cut_options(weights)
        best = min(imbalance(c, weights) for c in cuts)
        ok = [c for c in cuts if imbalance(c, weights) <= best + 0.15 + 1e-9]
        chosen = best_cut(weights, 0.15, min_gap)
        assert chosen in ok
        assert min(gap(chosen, weights), min_gap) == max(min(gap(c, weights), min_gap) for c in ok)
        g0, g1 = split(weights, 0.15, min_gap)
        assert g0 == {s for s in weights if chosen.side(s) == 0}
