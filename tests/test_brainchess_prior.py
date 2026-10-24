"""Tests for the BrainChess move prior and opponent; Stockfish is replaced by a stub."""
from __future__ import annotations

import os
import stat

import pytest

chess = pytest.importorskip("chess")   # the optional "chess" extra
import chess.engine  # noqa: E402

from brainsync.brainchess import prior
from brainsync.brainchess.prior import (MATE_SCORE, Engine, find_stockfish, heuristic_scores,
                                        to_weights)

# 1.e4 e5 2.Bc4 Nc6 3.Qh5 Nf6?? -- white mates with Qxf7.
SCHOLAR = "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4"
# Black queen on d4 en prise to the knight on f3 (and nothing defends it).
FREE_QUEEN = "rnb1kbnr/pppp1ppp/8/4p3/3q4/5N2/PPPPPPPP/RNBQKB1R w KQkq - 0 3"


def _executable(path) -> str:
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


# --- to_weights ---------------------------------------------------------------------

def test_weights_sum_to_one_and_positive():
    board = chess.Board()
    w = to_weights(heuristic_scores(board))
    assert set(w) == set(board.legal_moves)
    assert sum(w.values()) == pytest.approx(1.0)
    assert all(x > 0 for x in w.values())


def test_weights_are_stable_for_huge_scores():
    moves = list(chess.Board().legal_moves)[:3]
    w = to_weights({moves[0]: 1e6, moves[1]: -1e6, moves[2]: 0.0}, temperature_cp=1.0, uniform_mix=0.3)
    assert sum(w.values()) == pytest.approx(1.0)
    assert w[moves[1]] == pytest.approx(0.1)
    assert w[moves[0]] == pytest.approx(0.7 + 0.1)


def test_uniform_mix_bounds():
    scores = {m: float(i * 100) for i, m in enumerate(chess.Board().legal_moves)}
    flat = to_weights(scores, uniform_mix=1.0)
    assert all(x == pytest.approx(1 / len(scores)) for x in flat.values())
    sharp = to_weights(scores, uniform_mix=0.0)
    assert sum(sharp.values()) == pytest.approx(1.0)
    assert max(sharp, key=sharp.get) == max(scores, key=scores.get)
    for bad in (-0.01, 1.01):
        with pytest.raises(ValueError, match="uniform_mix"):
            to_weights(scores, uniform_mix=bad)


def test_to_weights_rejects_bad_args():
    with pytest.raises(ValueError, match="at least one"):
        to_weights({})
    scores = {chess.Move.from_uci("e2e4"): 0.0}
    for bad in (0.0, -5.0, float("nan")):
        with pytest.raises(ValueError, match="temperature_cp"):
            to_weights(scores, temperature_cp=bad)
    with pytest.raises(ValueError, match="finite"):
        to_weights({chess.Move.from_uci("e2e4"): float("inf")})


# --- heuristic ----------------------------------------------------------------------

def test_mate_in_one_scores_top():
    board = chess.Board(SCHOLAR)
    scores = heuristic_scores(board)
    mate = chess.Move.from_uci("h5f7")
    assert scores[mate] == MATE_SCORE
    assert max(scores, key=scores.get) == mate
    assert board.fen() == SCHOLAR   # pure: the board is untouched


def test_free_queen_capture_beats_quiet_move():
    board = chess.Board(FREE_QUEEN)
    scores = heuristic_scores(board)
    take = chess.Move.from_uci("f3d4")
    assert max(scores, key=scores.get) == take
    assert scores[take] > scores[chess.Move.from_uci("a2a3")] + 500


def test_hanging_a_piece_is_penalised():
    board = chess.Board()
    for san in ("e4", "e5"):
        board.push_san(san)
    scores = heuristic_scores(board)
    # Ba6 hangs the bishop to the b7 pawn; Bc4 is safe.
    assert scores[board.parse_san("Ba6")] < scores[board.parse_san("Bc4")] - 200
    assert scores[board.parse_san("Nf3")] > scores[board.parse_san("a3")]   # development


def test_castling_and_promotion_bonuses():
    castle = chess.Board("r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 12")
    s = heuristic_scores(castle)
    assert s[chess.Move.from_uci("e1g1")] > s[chess.Move.from_uci("e1f1")]
    promo = chess.Board("8/P6k/8/8/8/8/8/K7 w - - 0 1")
    s = heuristic_scores(promo)
    assert s[chess.Move.from_uci("a7a8q")] > s[chess.Move.from_uci("a7a8n")] > s[chess.Move.from_uci("a1b1")]


def test_heuristic_is_deterministic_and_empty_when_over():
    board = chess.Board(FREE_QUEEN)
    assert heuristic_scores(board) == heuristic_scores(board)
    mated = chess.Board(SCHOLAR)
    mated.push_uci("h5f7")
    assert heuristic_scores(mated) == {}


# --- Engine (heuristic) -------------------------------------------------------------

def _self_play(seed: int, plies: int = 40) -> list[chess.Move]:
    board = chess.Board()
    with Engine(None, seed=seed) as engine:
        assert engine.name == "heuristic"
        while not board.is_game_over() and len(board.move_stack) < plies:
            move = engine.reply(board)
            assert board.is_legal(move)
            board.push(move)
    return board.move_stack


def test_heuristic_self_play_is_legal_and_reproducible():
    game = _self_play(seed=7)
    assert 0 < len(game) <= 40
    assert _self_play(seed=7) == game
    assert _self_play(seed=8) != game


def test_engine_none_context_manager_and_weights():
    board = chess.Board(SCHOLAR)
    with Engine(None) as engine:
        assert engine.scores(board) == heuristic_scores(board)
        w = engine.weights(board)
        assert sum(w.values()) == pytest.approx(1.0)
        assert max(w, key=w.get) == chess.Move.from_uci("h5f7")
    engine.close()   # closing twice is fine


def test_reply_raises_when_game_over():
    board = chess.Board(SCHOLAR)
    board.push_uci("h5f7")
    with Engine(None) as engine, pytest.raises(ValueError, match="game is over"):
        engine.reply(board)


def test_engine_rejects_bad_args():
    with pytest.raises(ValueError, match="skill"):
        Engine(None, skill=21)
    with pytest.raises(ValueError, match="think_s"):
        Engine(None, think_s=0)


# --- Engine (Stockfish stub) --------------------------------------------------------

class StubStockfish:
    """Stands in for chess.engine.SimpleEngine: ranks the first legal moves, plays the first."""

    def __init__(self, ranked: int = 3, fail_after: int | None = None):
        self.ranked = ranked
        self.fail_after = fail_after   # calls before every later call raises
        self.calls = 0
        self.options: dict = {}
        self.analyse_args: list[dict] = []
        self.quit_called = False

    def _tick(self):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise chess.engine.EngineTerminatedError("engine process died unexpectedly")

    def configure(self, options):
        self.options.update(options)

    def analyse(self, board, limit, multipv=None):
        self._tick()
        self.analyse_args.append({"limit": limit, "multipv": multipv})
        legal = list(board.legal_moves)
        infos = []
        for i, move in enumerate(legal[:self.ranked]):
            if i == 0:
                score = chess.engine.PovScore(chess.engine.Mate(2), board.turn)
            else:
                score = chess.engine.PovScore(chess.engine.Cp(-100 * i), board.turn)
            infos.append({"pv": [move], "score": score, "multipv": i + 1})
        return infos

    def play(self, board, limit):
        self._tick()
        return chess.engine.PlayResult(list(board.legal_moves)[-1], None)

    def quit(self):
        self.quit_called = True

    def close(self):
        pass


@pytest.fixture
def stub(monkeypatch):
    """Patch popen_uci to hand out a StubStockfish; returns a setter for its arguments."""
    made: dict = {"kwargs": {}, "engine": None, "path": None}

    def popen_uci(path, *args, **kwargs):
        made["path"] = path
        made["engine"] = StubStockfish(**made["kwargs"])
        return made["engine"]

    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", staticmethod(popen_uci))
    return made


def test_stockfish_scores_mates_and_missing_moves(stub):
    board = chess.Board()
    with Engine("/fake/stockfish", skill=5, think_s=0.1) as engine:
        sf = stub["engine"]
        assert stub["path"] == "/fake/stockfish"
        assert sf.options == {"Skill Level": 5}
        assert engine.name == "stockfish (skill 5)"
        scores = engine.scores(board)
        legal = list(board.legal_moves)
        assert set(scores) == set(legal)
        assert sf.analyse_args[0]["multipv"] == len(legal)
        assert sf.analyse_args[0]["limit"].time == 0.1
        assert scores[legal[0]] == MATE_SCORE - 2
        assert scores[legal[1]] == -100
        assert scores[legal[2]] == -200
        assert all(scores[m] == -250 for m in legal[3:])   # unranked: worst - 50
        w = engine.weights(board)
        assert sum(w.values()) == pytest.approx(1.0) and min(w.values()) > 0
        assert engine.reply(board) == legal[-1]
    assert sf.quit_called


def test_stockfish_scores_from_black_view(stub):
    board = chess.Board()
    board.push_san("e4")
    with Engine("/fake/stockfish") as engine:
        scores = engine.scores(board)
    assert max(scores.values()) == MATE_SCORE - 2


def test_stockfish_mid_game_failure_falls_back(stub, capsys):
    stub["kwargs"] = {"fail_after": 2}
    board = chess.Board()
    with Engine("/fake/stockfish", seed=3) as engine:
        engine.scores(board)                 # call 1: stockfish
        board.push(engine.reply(board))      # call 2: stockfish
        assert engine.name.startswith("stockfish")
        scores = engine.scores(board)        # call 3: dies -> heuristic
        assert engine.name == "heuristic"
        assert scores == heuristic_scores(board)
        for _ in range(6):                   # the game goes on without it
            move = engine.reply(board)
            assert board.is_legal(move)
            board.push(move)
    err = capsys.readouterr().err
    assert err.count("stockfish failed") == 1
    assert stub["engine"].calls == 3


def test_stockfish_startup_failure_falls_back(monkeypatch, capsys):
    def popen_uci(path, *args, **kwargs):
        raise FileNotFoundError(path)

    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", staticmethod(popen_uci))
    with Engine("/missing/stockfish") as engine:
        assert engine.name == "heuristic"
        assert board_is_playable(engine)
    assert "stockfish failed" in capsys.readouterr().err


def board_is_playable(engine: Engine) -> bool:
    board = chess.Board()
    return board.is_legal(engine.reply(board))


@pytest.mark.skipif(find_stockfish() is None, reason="stockfish is not installed")
def test_real_stockfish_plays_legal_moves():
    board = chess.Board()
    with Engine(find_stockfish(), think_s=0.05) as engine:
        assert engine.name.startswith("stockfish")
        assert set(engine.scores(board)) == set(board.legal_moves)
        assert board.is_legal(engine.reply(board))


# --- find_stockfish -----------------------------------------------------------------

@pytest.fixture
def no_stockfish(monkeypatch):
    """Hide every real Stockfish so only the paths a test sets up are found."""
    monkeypatch.delenv(prior.ENV_VAR, raising=False)
    monkeypatch.setattr(prior.shutil, "which", lambda name: None)
    monkeypatch.setattr(prior, "FALLBACK_PATHS", ())


def test_find_stockfish_none(no_stockfish):
    assert find_stockfish() is None
    assert find_stockfish("/does/not/exist") is None


def test_find_stockfish_explicit_path(no_stockfish, tmp_path, monkeypatch):
    explicit = _executable(tmp_path / "sf_explicit")
    env = _executable(tmp_path / "sf_env")
    monkeypatch.setenv(prior.ENV_VAR, env)
    assert find_stockfish(explicit) == explicit
    plain = tmp_path / "not_executable"
    plain.write_text("")
    os.chmod(plain, 0o644)
    assert find_stockfish(str(plain)) == env   # explicit path unusable: falls through


def test_find_stockfish_env_var_and_path(no_stockfish, tmp_path, monkeypatch):
    env = _executable(tmp_path / "sf_env")
    monkeypatch.setenv(prior.ENV_VAR, env)
    assert find_stockfish() == env
    monkeypatch.delenv(prior.ENV_VAR)
    on_path = _executable(tmp_path / "stockfish")
    monkeypatch.setattr(prior.shutil, "which", lambda name: on_path)
    assert find_stockfish() == on_path


def test_cli_prints_prior(capsys, monkeypatch):
    assert prior.main([SCHOLAR, "--heuristic", "--top", "3"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("heuristic: ") and "Qxf7#" in out
    assert prior.main(["not a fen", "--heuristic"]) == 2
