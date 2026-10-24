"""Tests for BrainChess play: the fresh-window gate, the game session and the CLI, headless."""
from __future__ import annotations

import io
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

chess = pytest.importorskip("chess")   # the optional "chess" extra
import chess.pgn  # noqa: E402

from brainsync import launch
from brainsync.board import FakeBoard
from brainsync.brainchess import __main__ as cli
from brainsync.brainchess.game import (ChessSession, NullDisplay, SimulatedUser,
                                       StimulusDisplay, game_outcome)
from brainsync.brainchess.gate import MoveGate
from brainsync.brainchess.prior import Engine, find_stockfish
from brainsync.brainchess.stimulus import parse_command
from brainsync.classify import ABSTAIN, make
from brainsync.online import Decision, Decoder

FS = 250.0
FREQS = (15.0, 17.0)
HEADLESS = ["--fake", "--fast", "--no-stimulus", "--no-voice"]


@pytest.fixture(autouse=True)
def heuristic_opponent(monkeypatch):
    """The CLI tests play the heuristic even where Stockfish is installed (hermetic runs)."""
    monkeypatch.setattr(cli, "find_stockfish", lambda path=None: None)


# -- MoveGate: the Swiper semantics on hand-made decisions --------------------------------

def small_decoder(votes: int = 3) -> Decoder:
    """Window 250 samples, step 50, so stream positions in the tests are round numbers."""
    clf = make("cca", freqs=FREQS, fs=FS)
    return Decoder(clf, FS, window_s=1.0, step_s=0.2, votes=votes, pad_s=0.5)


def decision(end: int, raw: int, committed: int = ABSTAIN, changed: bool = False) -> Decision:
    return Decision(time=end / FS, scores=np.zeros(2), raw=raw, committed=committed,
                    changed=changed, end=end)


def push_all(gate: MoveGate, ends, raws) -> list[int]:
    return [gate.push(decision(e, r)) for e, r in zip(ends, raws)]


def test_gate_votes_default_to_the_decoder_and_cooldown_is_in_samples():
    dec = small_decoder(votes=4)
    g = MoveGate(dec)
    assert g.votes == 4 and MoveGate(dec, votes=2).votes == 2
    assert g.cooldown == 125                       # round(0.5 s * 250 Hz)
    assert g.switch == 0 and g.fresh_from == 125   # the screen shown counts from n_seen
    assert (g.pending, g.streak, g.stale, g.fired) == (ABSTAIN, 0, 0, 0)


def test_gate_fires_after_votes_fresh_agreeing_windows():
    g = MoveGate(small_decoder(votes=3))
    # window starts end - 250: 150, 200, 250 are all >= the 125 fresh limit
    assert push_all(g, [400, 450, 500], [1] * 3) == [ABSTAIN, ABSTAIN, 1]
    assert g.fired == 1 and g.stale == 0


def test_gate_ignores_stale_windows():
    g = MoveGate(small_decoder(votes=3))
    # windows starting before 125 (ends < 375) carry EEG from before the screen settled
    assert push_all(g, [300, 350, 400, 450], [0] * 4) == [ABSTAIN] * 4
    assert g.stale == 2 and (g.pending, g.streak) == (0, 2)
    assert g.push(decision(500, 0)) == 0


def test_gate_window_starting_exactly_at_the_limit_is_fresh():
    g = MoveGate(small_decoder(votes=1))
    assert g.is_stale(decision(374, 1)) and not g.is_stale(decision(375, 1))
    assert g.push(decision(374, 1)) == ABSTAIN
    assert g.push(decision(375, 1)) == 1


def test_gate_fires_once_then_needs_fully_fresh_windows():
    g = MoveGate(small_decoder(votes=3))
    assert push_all(g, [400, 450, 500], [1] * 3)[-1] == 1
    assert g.switch == 500 and g.fresh_from == 625   # push moved the screen change itself
    stale_ends = list(range(550, 875, 50))
    assert push_all(g, stale_ends, [1] * len(stale_ends)) == [ABSTAIN] * len(stale_ends)
    assert g.stale == len(stale_ends) and (g.pending, g.streak) == (ABSTAIN, 0)
    assert push_all(g, [900, 950, 1000], [1] * 3) == [ABSTAIN, ABSTAIN, 1]
    assert g.fired == 2


def test_gate_ignores_the_decoders_committed_field():
    g = MoveGate(small_decoder(votes=2))
    for end in (400, 450, 500):
        assert g.push(decision(end, ABSTAIN, committed=1, changed=True)) == ABSTAIN
    assert g.fired == 0


def test_gate_abstains_and_switches_break_the_streak():
    g = MoveGate(small_decoder(votes=3))
    seen = []
    for end, raw in zip(range(400, 800, 50), [0, 0, ABSTAIN, 0, 0, 1, 1, 1]):
        fired = g.push(decision(end, raw))
        seen.append((fired, g.pending, g.streak))
    assert seen == [(ABSTAIN, 0, 1), (ABSTAIN, 0, 2), (ABSTAIN, ABSTAIN, 0),
                    (ABSTAIN, 0, 1), (ABSTAIN, 0, 2), (ABSTAIN, 1, 1),
                    (ABSTAIN, 1, 2), (1, ABSTAIN, 0)]


def test_gate_runs_of_abstains_never_fire():
    g = MoveGate(small_decoder(votes=2))
    assert push_all(g, range(400, 1000, 50), [ABSTAIN] * 12) == [ABSTAIN] * 12
    assert g.fired == 0 and g.stale == 0


def test_gate_screen_changed_resets_the_votes():
    g = MoveGate(small_decoder(votes=3))
    push_all(g, [400, 450], [0, 0])
    g.screen_changed(at=450)
    assert (g.switch, g.fresh_from, g.pending, g.streak) == (450, 575, ABSTAIN, 0)
    assert push_all(g, [500, 800, 850, 900, 950], [0] * 5) == [ABSTAIN] * 4 + [0]
    assert g.stale == 2


def test_gate_screen_changed_defaults_to_samples_seen():
    dec = small_decoder()
    dec.feed(np.zeros((8, 100)))             # fewer than needed: no decisions yet
    g = MoveGate(dec)
    assert g.switch == 100
    dec.feed(np.zeros((8, 40)))
    g.screen_changed()
    assert g.switch == 140


def test_gate_zero_cooldown_needs_only_a_window_after_the_change():
    g = MoveGate(small_decoder(votes=1), cooldown_s=0.0)
    g.screen_changed(at=1000)
    assert g.push(decision(1249, 1)) == ABSTAIN
    assert g.push(decision(1250, 1)) == 1


@pytest.mark.parametrize("cooldown", [-0.1, float("nan"), float("inf")])
def test_gate_bad_cooldown_raises(cooldown):
    with pytest.raises(ValueError, match="cooldown_s"):
        MoveGate(small_decoder(), cooldown_s=cooldown)


@pytest.mark.parametrize("votes", [0, -1, 1.5])
def test_gate_bad_votes_raise(votes):
    with pytest.raises(ValueError, match="votes"):
        MoveGate(small_decoder(), votes=votes)


# -- ChessSession with a scripted user ----------------------------------------------------

class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make_session(board: chess.Board | None = None, **kw) -> tuple[ChessSession, NullDisplay]:
    display = NullDisplay()
    kw.setdefault("hold_s", 0.0)
    session = ChessSession(board if board is not None else chess.Board(), Engine(None, seed=2),
                           display, **kw)
    return session, display


def play_out(session: ChessSession, user: SimulatedUser, clock: Clock | None = None,
             max_picks: int = 5000) -> int:
    """Pick what ``user`` looks at until the session ends; returns the picks made."""
    picks = 0
    while not session.over:
        if session.waiting:
            assert clock is not None, "a hold needs a clock to pass"
            clock.t += 0.5
            session.tick()
            continue
        session.on_pick(user.look(session))
        picks += 1
        assert picks < max_picks, "the session did not end"
    return picks


def test_full_game_against_the_heuristic_ends_with_a_legal_pgn():
    session, display = make_session()
    user = SimulatedUser(Engine(None, seed=5))
    session.start()
    picks = play_out(session, user)
    assert session.ended == "game over" and session.game_over()
    assert session.result() in ("1-0", "0-1", "1/2-1/2")
    assert picks == len(session.picks) == sum(m.picks for m in session.moves)
    assert session.backs == 0 and all(m.picks >= 1 for m in session.moves)
    game = chess.pgn.read_game(io.StringIO(session.pgn()))
    assert game is not None and not game.errors
    assert game.headers["Result"] == session.result()
    replay = game.board()
    for move in game.mainline_moves():
        assert replay.is_legal(move)
        replay.push(move)
    assert replay.fen() == session.board.fen()
    # the user (White) played every other move
    assert len(session.moves) == (len(session.board.move_stack) + 1) // 2
    assert display.holding and display.text == session.status()
    assert session.summary()[0].startswith(f"brainchess: {len(session.moves)} moves played")


def test_every_screen_is_a_valid_board_command():
    session, display = make_session(max_moves=3)
    session.start()
    play_out(session, SimulatedUser(Engine(None, seed=1)))
    shows = [kw for name, kw in display.calls if name == "show"]
    assert shows
    for kw in shows:
        parse_command(f"groups {kw['groups']}")
        if kw["panels"] is not None:
            assert len(kw["panels"]) == 2
    # flicker runs only during the user's turns and every turn starts with a go
    names = [name for name, _ in display.calls]
    assert names.count("go") == 3 and names[-1] == "hold"


def test_back_restarts_the_move_from_the_piece_stage():
    session, display = make_session(max_moves=1)
    said: list[str] = []
    session.speak = said.append
    session.start()
    want = chess.Move.from_uci("e2e4")
    sel = session.selector
    session.on_pick(1 - sel.target_for(want))           # a wrong decode
    assert not session.selector.reachable(want)
    # the user keeps picking among what is left, then sees the wrong move at confirm
    while session.selector.stage != "confirm":
        cands = session.selector.candidate_moves()
        session.on_pick(session.selector.target_for(max(cands, key=cands.get)))
    assert session.selector.shown_move != want
    assert display.panels == ("PLAY", "BACK") and display.arrow is not None
    assert session.selector.target_for(want) == 1
    session.on_pick(1)                                   # BACK
    assert session.backs == 1 and "Back" in said
    assert session.selector.stage == "piece" and session.selector.reachable(want)
    assert display.panels is None and display.text == "Look at the piece you want to move"
    while not session.over:
        session.on_pick(session.selector.target_for(want))
    assert [m.san for m in session.moves] == ["e4"]
    assert session.moves[0].backs == 1
    assert session.ended == "move limit"
    assert len(session.board.move_stack) == 1            # no reply after the last move
    stages = [p.stage for p in session.picks]
    assert stages.count("confirm") == 2 and stages[0] == "piece"


def test_promotion_uses_the_side_panels():
    board = chess.Board("7k/P7/8/8/8/8/8/K7 w - - 0 1")
    session, display = make_session(board, max_moves=1)
    session.start()
    want = chess.Move.from_uci("a7a8n")
    seen_panels = []
    while not session.over:
        if session.selector.stage == "promo":
            seen_panels.append(display.panels)
        session.on_pick(session.selector.target_for(want))
    assert seen_panels and all(p is not None for p in seen_panels)
    assert any("Knight" in label for p in seen_panels for label in p)
    assert session.moves[0].san == "a8=N"
    assert session.board.piece_at(chess.A8) == chess.Piece(chess.KNIGHT, chess.WHITE)


def test_user_plays_black_and_the_opponent_moves_first():
    clock = Clock()
    session, display = make_session(user_color=chess.BLACK, hold_s=2.0, max_moves=2,
                                    clock=clock)
    assert session.flip
    looks: list[str] = []
    session.on_screen = lambda s: looks.append(s.phase)
    session.start()
    assert len(session.board.move_stack) == 1 and session.waiting
    reply = session.board.peek()
    assert display.holding and display.arrow == (reply.from_square, reply.to_square)
    assert display.text.startswith("White played ")
    clock.t = 1.9
    session.tick()
    assert session.waiting
    clock.t = 2.0
    session.tick()
    assert session.phase == "user" and not display.holding
    assert looks[:2] == ["opponent", "user"]
    play_out(session, SimulatedUser(Engine(None, seed=3)), clock)
    assert session.ended == "move limit" and len(session.board.move_stack) == 4
    pgn = session.pgn()
    assert '[Black "BrainChess user"]' in pgn and "1. " in pgn


def test_no_confirm_plays_the_move_at_once():
    session, _ = make_session(confirm=False, max_moves=1)
    session.start()
    want = chess.Move.from_uci("g1f3")
    while not session.over:
        session.on_pick(session.selector.target_for(want))
    assert session.moves[0].san == "Nf3"
    assert all(p.stage in ("piece", "dest") for p in session.picks)


FORCED = "k7/8/8/8/8/8/1q6/K7 w - - 0 1"     # White's only legal move is Kxb2


def test_no_confirm_plays_a_forced_move_without_picks():
    # regression: with one legal move and no confirm the selector is born 'done', and the
    # session used to wait for a pick that then raised 'the move is already chosen'
    session, display = make_session(chess.Board(FORCED), confirm=False)
    said: list[str] = []
    session.speak = said.append
    session.start()
    assert [m.san for m in session.moves] == ["Kxb2"] and session.moves[0].picks == 0
    assert session.picks == [] and "go" not in [name for name, _ in display.calls]
    assert "Only one move" in said
    assert session.over and session.result() == "1/2-1/2"    # bare kings


def test_no_confirm_forced_move_mid_game_then_the_game_goes_on():
    # Black checks with the queen; the only evasion is to take it, then play continues
    board = chess.Board("k7/8/8/8/8/8/2q5/K7 b - - 0 1")

    class Scripted(Engine):
        def reply(self, b):
            return chess.Move.from_uci("c2b2") if b.turn == chess.BLACK else super().reply(b)

    session = ChessSession(board, Scripted(None, seed=0), NullDisplay(), confirm=False,
                           hold_s=0.0, user_color=chess.WHITE)
    session.start()
    assert [m.san for m in session.moves] == ["Kxb2"]
    assert session.over and session.status().startswith("Draw by insufficient material")


def test_cli_no_confirm_forced_move_is_played(capsys):
    assert cli.main([*HEADLESS, "--no-confirm", "--fen", FORCED]) == 0
    out = capsys.readouterr().out
    assert "1 moves played, 0 picks" in out and "1. Kxb2" in out


class ScriptedEngine(Engine):
    """The heuristic prior, with the replies given in UCI order."""

    def __init__(self, replies: list[str]) -> None:
        super().__init__(None, seed=0)
        self.replies = [chess.Move.from_uci(u) for u in replies]
        self.weight_phases: list[str] = []
        self.session: ChessSession | None = None

    def reply(self, board):
        return self.replies.pop(0)

    def weights(self, board, *args, **kw):
        if self.session is not None:
            self.weight_phases.append(self.session.phase)
        return super().weights(board, *args, **kw)


def play_moves(session: ChessSession, ucis: list[str]) -> None:
    """The user picks each move in turn, error-free (the opponent replies in between)."""
    for uci in ucis:
        want = chess.Move.from_uci(uci)
        assert session.phase == "user", session.phase
        played = session.user_moves
        while session.user_moves == played:
            session.on_pick(session.selector.target_for(want))


def test_a_repetition_the_opponent_could_claim_does_not_end_the_game():
    # regression: claim_draw=True ended the game after White's 4th move, when Black only
    # *could* repeat a third time
    engine = ScriptedEngine(["g8f6", "f6g8", "g8f6", "f6g8"])
    session = ChessSession(chess.Board(), engine, NullDisplay(), confirm=False, hold_s=0.0)
    session.start()
    play_moves(session, ["g1f3", "f3g1", "g1f3", "f3g1"])
    # after White's 4th move and Black's reply the start position stands for the 3rd time
    assert len(session.board.move_stack) == 8
    assert session.over and session.status() == "Draw by threefold repetition (1/2-1/2)"


def test_threefold_counts_only_once_it_has_happened():
    board = chess.Board()
    for uci in ["g1f3", "g8f6", "f3g1", "f6g8", "g1f3", "g8f6", "f3g1"]:
        board.push_uci(uci)
    assert board.can_claim_threefold_repetition() and game_outcome(board) is None
    board.push_uci("f6g8")
    assert game_outcome(board).termination == chess.Termination.THREEFOLD_REPETITION


def test_fifty_moves_end_the_game_only_at_100_halfmoves():
    # regression: at a halfmove clock of 99 the user's move ended the game before Black moved
    engine = ScriptedEngine(["e8f8"])
    session = ChessSession(chess.Board("4k3/8/8/8/8/8/8/3QK3 w - - 98 80"), engine,
                           NullDisplay(), confirm=False, hold_s=0.0, max_moves=2)
    session.start()
    play_moves(session, ["d1d2"])
    assert session.board.halfmove_clock == 100 and len(session.board.move_stack) == 2
    assert session.over and session.status() == "Draw by fifty moves (1/2-1/2)"
    assert session.result() == "1/2-1/2" and session.pgn().rstrip().endswith("1/2-1/2")


def test_the_opponent_mating_ends_the_game_on_its_move():
    clock = Clock()
    engine = ScriptedEngine(["d8h4"])               # fool's mate: 1. f3 e5 2. g4 Qh4#
    board = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/8/5P2/PPPPP1PP/RNBQKBNR w KQkq - 0 2")
    session = ChessSession(board, engine, NullDisplay(), hold_s=2.0, clock=clock)
    display = session.display
    session.start()
    play_moves(session, ["g2g4"])
    assert session.ended == "game over" and session.result() == "0-1"
    assert display.text == "Checkmate: Black wins (0-1)"
    assert display.arrow == (chess.D8, chess.H4) and display.holding
    names = [name for name, _ in display.calls]
    assert names.count("go") == 1 and names[-1] == "hold"   # no turn after the mate
    assert session.pgn().rstrip().endswith("Qh4# 0-1")


def test_the_prior_is_worked_out_during_the_opponent_hold():
    # regression: Stockfish's analysis ran between the last board read and the screen
    # change, so the gate stamped the change too early and stale windows looked settled
    clock = Clock()
    engine = ScriptedEngine(["e7e5"])
    session = ChessSession(chess.Board(), engine, NullDisplay(), hold_s=2.0, clock=clock,
                           max_moves=2)
    engine.session = session
    session.start()
    assert engine.weight_phases == ["idle"]           # the first turn (main prefetches it)
    play_moves(session, ["e2e4"])
    assert session.waiting and engine.weight_phases == ["idle", "opponent"]
    clock.t = 2.0
    session.tick()                                     # the turn starts: no analysis now
    assert session.phase == "user" and engine.weight_phases == ["idle", "opponent"]


def test_prefetch_is_used_once_and_only_for_its_position():
    engine = ScriptedEngine([])
    session = ChessSession(chess.Board(), engine, NullDisplay(), hold_s=0.0)
    engine.session = session
    session.prefetch()
    session.prefetch()
    assert engine.weight_phases == ["idle"]
    session.start()
    assert engine.weight_phases == ["idle"]


def test_simulated_user_backs_out_of_a_wrong_selection_without_deadlock():
    session, _ = make_session(max_moves=4)
    user = SimulatedUser(Engine(None, seed=9))
    session.start()
    rng = np.random.default_rng(0)
    picks = 0
    while not session.over:
        target = user.look(session)
        if rng.random() < 0.2:            # a wrong decode now and then
            target = 1 - target
        session.on_pick(target)
        picks += 1
        assert picks < 2000
    assert session.user_moves == 4
    # it really backed out: BACK was picked and a move was still played after it
    assert session.backs >= 1 and any(m.backs > 0 for m in session.moves)
    assert sum(m.backs for m in session.moves) == session.backs


def test_session_rejects_picks_out_of_turn_and_bad_arguments():
    session, _ = make_session()
    with pytest.raises(ValueError, match="no pick"):
        session.on_pick(0)
    session.start()
    with pytest.raises(ValueError, match="already started"):
        session.start()
    with pytest.raises(ValueError, match="hold_s"):
        make_session(hold_s=-1.0)
    with pytest.raises(ValueError, match="max_moves"):
        make_session(max_moves=-1)


def test_game_already_over_ends_at_start():
    board = chess.Board("7k/5QQ1/8/8/8/8/8/K7 b - - 0 1")   # black is mated
    session, display = make_session(board)
    session.start()
    assert session.over and session.result() == "1-0"
    assert display.text.startswith("Checkmate: White wins")


def test_pieces_are_rendered_once_per_position(tmp_path):
    session, display = make_session(max_moves=1, pieces_dir=tmp_path)
    session.start()
    first = display.pieces
    assert first == tmp_path / "pieces_000.png" and first.is_file()
    session.on_pick(session.selector.target_for(chess.Move.from_uci("e2e4")))
    assert display.calls[-1][1]["pieces"] == first
    while not session.over:
        session.on_pick(session.selector.target_for(chess.Move.from_uci("e2e4")))
    assert display.pieces == tmp_path / "pieces_001.png"
    assert sorted(p.name for p in tmp_path.glob("*.png")) == ["pieces_000.png",
                                                              "pieces_001.png"]


# -- StimulusDisplay ----------------------------------------------------------------------

def test_stimulus_display_writes_only_what_changed():
    out = io.StringIO()
    d = StimulusDisplay(out)
    groups = "0" * 16 + "." * 32 + "1" * 16
    assert d.show(groups, Path("/tmp/p 0.png"), None, None, "Look\nhere")
    assert d.go() and d.go()
    assert d.show(groups, Path("/tmp/p 0.png"), (12, 28), ("Queen", "Rook|Bishop"), "Look here")
    assert d.show(groups, None, None, None, "")
    assert d.hold()
    lines = out.getvalue().splitlines()
    assert lines == [f"groups {groups}", "pieces /tmp/p 0.png", "text Look here", "go",
                     "arrow 12 28", "panels Queen|Rook/Bishop", "arrow none", "panels none",
                     "text", "hold"]
    for ln in lines:
        parse_command(ln)
    assert d.lines == len(lines)


def test_stimulus_display_reports_a_closed_pipe():
    class Broken(io.StringIO):
        def write(self, s):
            raise BrokenPipeError("gone")

    d = StimulusDisplay(Broken())
    assert not d.go()
    assert d.closed and not d.show("." * 64, None, None, None, "x") and not d.hold()
    session = ChessSession(chess.Board(), Engine(None), StimulusDisplay(Broken()), hold_s=0)
    session.start()
    assert not session.display_ok


# -- the CLI ------------------------------------------------------------------------------

def read_pgn(out: str) -> chess.pgn.Game:
    game = chess.pgn.read_game(io.StringIO(out[out.index("[Event "):]))
    assert game is not None and not game.errors
    return game


def test_end_to_end_fake_fast_three_moves(capsys):
    began = time.monotonic()
    assert cli.main([*HEADLESS, "--max-moves", "3", "--seed", "1"]) == 0
    assert time.monotonic() - began < 60
    cap = capsys.readouterr()
    assert "brainchess: 3 moves played" in cap.out and "ended: move limit" in cap.out
    game = read_pgn(cap.out)
    moves = list(game.mainline_moves())
    assert len(moves) == 5                  # 3 user moves and the 2 replies between them
    assert "[speak] Keep still and look at the middle of the board." in cap.err
    assert "fake board:" in cap.out
    assert "baseline from" in cap.err


def test_end_to_end_as_black_with_short_calibration_writes_the_pgn(capsys, tmp_path):
    pgn = tmp_path / "game.pgn"
    assert cli.main([*HEADLESS, "--color", "black", "--max-moves", "2", "--seed", "4",
                     "--calibrate", "10", "--pgn", str(pgn)]) == 0
    cap = capsys.readouterr()
    game = read_pgn(pgn.read_text())
    assert len(list(game.mainline_moves())) == 4
    assert game.headers["Black"] == "BrainChess user"
    assert str(game) in cap.out


def test_end_to_end_seconds_cap_stops_the_game(capsys):
    assert cli.main([*HEADLESS, "--seconds", "8", "--calibrate", "10"]) == 0
    cap = capsys.readouterr()
    assert "run ended: time up" in cap.out


def test_help_works(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "BrainChess" in out and "--max-moves" in out and "--stockfish" in out


@pytest.mark.parametrize("argv", [
    ["--fast"], ["--skill", "21"], ["--hold-s", "-1"], ["--cooldown", "nan"],
    ["--max-moves", "-1"], ["--votes", "0"], ["--color", "red"], ["--think-s", "0"],
])
def test_bad_arguments_exit_2(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def test_bad_fen_config_or_stockfish_return_2(capsys, tmp_path):
    assert cli.main([*HEADLESS, "--fen", "not a fen"]) == 2
    assert cli.main([*HEADLESS, "--fen", "7k/5QQ1/8/8/8/8/8/K7 b - - 0 1"]) == 2
    assert cli.main([*HEADLESS, "--config", "five_targets_missing"]) == 2
    assert cli.main([*HEADLESS, "--stockfish", str(tmp_path / "nope")]) == 2
    err = capsys.readouterr().err
    assert "bad --fen" in err and "already over" in err and "not an executable" in err


@pytest.mark.parametrize("fen, why", [
    ("4k3/4R3/8/8/8/8/8/4K3 w - - 0 1", "opposite check"),   # the king could be taken
    ("4k3/8/8/8/8/8/8/R7 w - - 0 1", "no white king"),
    ("P3k3/8/8/8/8/8/8/4K3 w - - 0 1", "pawns on backrank"),
])
def test_illegal_fen_returns_2(fen, why, capsys):
    # regression: positions python-chess flags as invalid used to start a game
    assert cli.main([*HEADLESS, "--fen", fen]) == 2
    assert f"not legal ({why})" in capsys.readouterr().err


def test_fen_at_99_halfmoves_is_not_over(capsys):
    # regression: claim_draw=True rejected a halfmove clock of 99 as already drawn
    assert cli.main([*HEADLESS, "--fen", "4k3/8/8/8/8/8/8/3QK3 w - - 99 80",
                     "--max-moves", "1"]) == 0
    assert "1 moves played" in capsys.readouterr().out


def test_stockfish_may_be_a_command_on_path(monkeypatch, tmp_path):
    # regression: a bare name was compared against the absolute path PATH resolved it to
    exe = tmp_path / "stockfish"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("BRAINCHESS_STOCKFISH", raising=False)
    monkeypatch.setattr(cli, "find_stockfish", find_stockfish)
    assert cli.resolve_stockfish("stockfish") == str(exe)
    assert cli.resolve_stockfish(str(exe)) == str(exe)
    with pytest.raises(ValueError, match="not an executable"):
        cli.resolve_stockfish("no-such-stockfish-here")
    with pytest.raises(ValueError, match="not an executable"):
        cli.resolve_stockfish(str(tmp_path))          # a directory


# Stand-in for the board stimulus: reports its window, then copies each line to a file
# (and its arrival time to <file>.times, with the EOF last) until EOF. Other modes: exit
# like Escape (code 1 and "aborted") at the first "go" or once a checkmate caption is
# shown, crash (code 1, no "aborted") at the first "go", or die at EOF from a Ctrl-C
# that reached it too (an uncaught KeyboardInterrupt).
FAKE_STIMULUS = r"""
import sys, time
mode = sys.argv[2]
print("measured refresh: 60.00 Hz", flush=True)
with open(sys.argv[1], "w") as out, open(sys.argv[1] + ".times", "w") as times:
    for line in sys.stdin:
        out.write(line)
        out.flush()
        times.write(f"{time.monotonic():.4f} {line}")
        times.flush()
        if line.strip() == "go":
            if mode == "escape":
                print("aborted", flush=True)
                sys.exit(1)
            if mode == "crash":
                sys.exit(1)
        if mode == "escape_at_result" and line.startswith("text Checkmate"):
            print("aborted", flush=True)
            sys.exit(1)
    times.write(f"{time.monotonic():.4f} EOF\n")
if mode == "sigint":
    raise KeyboardInterrupt   # at EOF, so it never races the game's own Ctrl-C
"""


@pytest.fixture
def fake_stimulus(monkeypatch, tmp_path):
    calls: list[dict] = []
    lines = tmp_path / "lines.txt"
    mode = {"value": "copy"}

    def fake_launch(args, stdin=None, command=None, **kw):
        cmd = [sys.executable, "-c", FAKE_STIMULUS, str(lines), mode["value"]]
        stim = launch.launch_stimulus(args, stdin=stdin, command=cmd, log_dir=tmp_path)
        calls.append({"args": list(args), "command": list(command or []), "stim": stim})
        return stim

    monkeypatch.setattr(cli, "launch_stimulus", fake_launch)
    return calls, lines, mode


def test_board_commands_reach_the_stimulus(fake_stimulus, capsys):
    calls, lines, _ = fake_stimulus
    assert cli.main(["--fake", "--fast", "--no-voice", "--max-moves", "1", "--seed", "2",
                     "--calibrate", "10", "--color", "black", "--end-s", "0"]) == 0
    assert len(calls) == 1
    assert calls[0]["command"][1:3] == ["-m", "brainsync.brainchess.stimulus"]
    assert "--flip" in calls[0]["args"]
    sent = lines.read_text().splitlines()
    cmds = [parse_command(ln) for ln in sent]
    verbs = [c.verb for c in cmds]
    # the still board for calibration first, then the opponent's move, then flicker
    assert verbs[0] == "pieces" and "go" in verbs
    assert [v for v in verbs if v in ("hold", "go")][-1] == "hold"   # still at the end
    assert verbs.index("arrow") < verbs.index("go")
    pieces = [c.value for c in cmds if c.verb == "pieces"]
    assert [p.name for p in pieces] == ["pieces_000.png", "pieces_001.png", "pieces_002.png"]
    assert not pieces[0].parent.exists()            # the temp dir is gone after the run
    assert any(c.verb == "panels" and c.value == ("PLAY", "BACK") for c in cmds)
    assert calls[0]["stim"].proc.returncode == 0


def test_escape_in_the_stimulus_ends_the_run_cleanly(fake_stimulus, capsys):
    _, _, mode = fake_stimulus
    mode["value"] = "escape"
    assert cli.main(["--fake", "--fast", "--no-voice", "--calibrate", "10"]) == 0
    assert "run ended: escape" in capsys.readouterr().out


def read_times(lines: Path) -> list[tuple[float, str]]:
    out = []
    for row in Path(str(lines) + ".times").read_text().splitlines():
        t, _, text = row.partition(" ")
        out.append((float(t), text))
    return out


@pytest.mark.parametrize("argv, caption", [
    (["--max-moves", "1"], "text Stopped after 1 moves"),
    (["--fen", "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"], "text Checkmate: White wins (1-0)"),
])
def test_the_final_screen_stays_up_before_the_stimulus_closes(fake_stimulus, capsys, argv,
                                                             caption):
    # regression: EOF used to follow the result caption within a millisecond, so the last
    # move and the result were never on screen
    _, lines, _ = fake_stimulus
    assert cli.main(["--fake", "--fast", "--no-voice", "--calibrate", "10", "--end-s", "0.6",
                     *argv]) == 0
    times = read_times(lines)
    assert times[-1][1] == "EOF"
    shown = [t for t, text in times if text == caption]
    assert shown, times[-4:]
    assert times[-1][0] - shown[-1] >= 0.5


def test_no_linger_without_a_finished_game(fake_stimulus, capsys):
    _, lines, _ = fake_stimulus
    began = time.monotonic()
    assert cli.main(["--fake", "--fast", "--no-voice", "--calibrate", "10", "--seconds", "4",
                     "--end-s", "30"]) == 0
    assert time.monotonic() - began < 25
    assert "run ended: time up" in capsys.readouterr().out


def test_linger_waits_and_stops_on_a_reason():
    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    assert cli.linger(2.0, lambda: None, sleep=sleep, clock=lambda: now[0]) is None
    assert 2.0 <= now[0] < 2.01
    now[0] = 0.0
    reasons = iter([None, None, "stimulus exited (code 1)"])
    assert cli.linger(5.0, lambda: next(reasons), sleep=sleep,
                      clock=lambda: now[0]) == "stimulus exited (code 1)"
    assert now[0] < 1.0


def test_escape_on_the_final_screen_closes_it_and_keeps_the_result(fake_stimulus, capsys):
    _, _, mode = fake_stimulus
    mode["value"] = "escape_at_result"
    began = time.monotonic()
    assert cli.main(["--fake", "--fast", "--no-voice", "--calibrate", "10", "--end-s", "20",
                     "--fen", "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"]) == 0
    assert time.monotonic() - began < 15                # did not wait out --end-s
    out = capsys.readouterr().out
    assert "Checkmate: White wins (1-0)" in out and "run ended" not in out


def test_stimulus_crash_mid_game_exits_1(fake_stimulus, capsys):
    # the demo's regression: a crashed stimulus must not leave the exit code at 0
    _, _, mode = fake_stimulus
    mode["value"] = "crash"
    assert cli.main(["--fake", "--fast", "--no-voice", "--calibrate", "10"]) == 1
    cap = capsys.readouterr()
    assert "run ended: stimulus" in cap.out
    assert "error: the stimulus exited with code 1" in cap.err


class InterruptingBoard(FakeBoard):
    """FakeBoard whose read raises KeyboardInterrupt (Ctrl-C) after ``reads`` reads."""

    reads = 400

    def read(self):
        type(self).reads -= 1
        if type(self).reads < 0:
            raise KeyboardInterrupt
        return super().read()


def test_ctrl_c_mid_game_exits_0_even_when_it_reached_the_stimulus(fake_stimulus,
                                                                  monkeypatch, capsys):
    # regression: the terminal's Ctrl-C also kills the stimulus (code -SIGINT), which used
    # to be reported as a stimulus error with exit 1
    calls, _, mode = fake_stimulus
    mode["value"] = "sigint"
    InterruptingBoard.reads = 500            # past calibration (10 s = 400 reads)
    monkeypatch.setattr(cli, "FakeBoard", InterruptingBoard)
    assert cli.main(["--fake", "--fast", "--no-voice", "--calibrate", "10"]) == 0
    cap = capsys.readouterr()
    assert "run ended: interrupted" in cap.out and "stimulus exited with code" not in cap.err
    assert calls[0]["stim"].proc.returncode == -signal.SIGINT


def test_ctrl_c_during_calibration_exits_130(fake_stimulus, monkeypatch, capsys):
    calls, _, _ = fake_stimulus
    InterruptingBoard.reads = 100
    monkeypatch.setattr(cli, "FakeBoard", InterruptingBoard)
    assert cli.main(["--fake", "--fast", "--no-voice", "--calibrate", "10"]) == 130
    cap = capsys.readouterr()
    assert "interrupted before the game started" in cap.err and cap.out == ""
    assert calls[0]["stim"].proc.returncode == 0


class SilentBoard(FakeBoard):
    """FakeBoard that stops delivering after calibration."""

    def read(self):
        X, t = super().read()
        assert self.elapsed < 300.0, "the run should have ended on the stall"
        if self.elapsed > 12.0:
            return X[:, :0], t[:0]
        return X, t


def test_board_stall_ends_the_run_with_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(cli, "FakeBoard", SilentBoard)
    assert cli.main([*HEADLESS, "--calibrate", "10"]) == 1
    cap = capsys.readouterr()
    assert "run ended: board stalled" in cap.out and "sent nothing for 5 s" in cap.err


def test_a_runtime_error_mid_game_exits_1(monkeypatch, capsys):
    def broken(self, target):
        raise RuntimeError("the decoder fell over")

    monkeypatch.setattr(ChessSession, "on_pick", broken)
    assert cli.main([*HEADLESS, "--calibrate", "10"]) == 1
    cap = capsys.readouterr()
    assert "error: the decoder fell over" in cap.err and "run ended: error" in cap.out


class EmptyBoard:
    def read(self):
        return np.empty((8, 0)), np.empty(0)


def test_play_reports_a_stall_and_a_closed_pipe():
    session, _ = make_session()
    session.start()
    dec = small_decoder()
    now = [0.0]

    def sleep(s: float) -> None:
        now[0] += s

    assert cli.play(session, dec, EmptyBoard(), 0.0, lambda d: None, sleep=sleep,
                    clock=lambda: now[0]) == "board stalled"
    assert 5.0 <= now[0] < 5.3
    session.display_ok = False
    assert cli.play(session, dec, EmptyBoard(), 0.0, lambda d: None, sleep=sleep,
                    clock=lambda: now[0]) == "stimulus pipe closed"
    session.display_ok = True
    assert cli.play(session, dec, EmptyBoard(), 0.0, lambda d: None, sleep=sleep,
                    clock=lambda: now[0], stop=lambda: "stimulus exited (code 3)") \
        == "stimulus exited (code 3)"


def test_modules_do_not_import_psychopy():
    code = ("import sys, brainsync.brainchess.__main__, brainsync.brainchess.game; "
            "assert 'psychopy' not in sys.modules and 'matplotlib.pyplot' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True)
